from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..paths import AllowedPathPolicy, PathPolicyError
from ..runtime_mode import WORKFLOW_WORKER_FLAG
from .http_client import CONTROL_TOKEN_ENV
from .startup_diagnostics import (
    WorkflowStartupStage,
    current_startup_diagnostic,
)

WorkerMode = Literal["start", "recover"]
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")
_WORKFLOW_DEPENDENCIES = ("dbos", "jq", "jsonschema", "yaml")
_CHILD_ENVIRONMENT_KEYS = (
    "PATH",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PYTHONHOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)


class WorkflowManagerError(RuntimeError):
    """A requested workflow worker could not be prepared or managed."""


@dataclass(frozen=True, slots=True)
class WorkflowWorkerStatus:
    run_id: str
    state: Literal["running", "completed", "failed", "stopped", "shutdown_failed"]
    pid: int | None
    returncode: int | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowShutdownReport:
    owned_turns: frozenset[tuple[str, str]]
    ownership_complete: bool
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class _ManagedWorker:
    run_dir: Path
    mode: WorkerMode
    live: bool
    shutdown_file: Path
    process: asyncio.subprocess.Process | None = None
    exit_task: asyncio.Task[int] | None = None
    state: Literal["running", "completed", "failed", "stopped", "shutdown_failed"] = "running"
    returncode: int | None = None
    error: str | None = None
    shutdown_requested: bool = False


@dataclass(slots=True)
class WorkflowWorkerHandle:
    _manager: WorkflowWorkerManager = field(repr=False)
    run_id: str
    pid: int

    @property
    def status(self) -> WorkflowWorkerStatus:
        status = self._manager.get_status(self.run_id)
        if status is None:
            raise WorkflowManagerError("managed worker status is unavailable")
        return status

    async def wait(self, timeout_seconds: float | None = None) -> WorkflowWorkerStatus:
        record = self._manager._records.get(self.run_id)
        if record is None or record.exit_task is None:
            raise WorkflowManagerError("managed worker process is unavailable")
        wait_task = asyncio.shield(record.exit_task)
        if timeout_seconds is None:
            await wait_task
        else:
            await asyncio.wait_for(wait_task, timeout_seconds)
        return self.status


class WorkflowWorkerManager:
    """Own explicit DBOS worker children without starting one during construction."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        allowed_roots: tuple[str, ...] = (),
        control_token: str | None = None,
        python_executable: str | None = None,
        frozen: bool | None = None,
        graceful_shutdown_seconds: float = 2.0,
    ) -> None:
        if not 0.05 <= graceful_shutdown_seconds <= 30:
            raise ValueError("workflow worker shutdown wait must be from 0.05 to 30 seconds")
        self.state_root = (
            Path(state_root or default_workflow_state_root()).expanduser().resolve(strict=False)
        )
        self.allowed_roots = tuple(allowed_roots)
        self._control_token = control_token
        self._python_executable = python_executable or sys.executable
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._graceful_shutdown_seconds = graceful_shutdown_seconds
        self._accepting = True
        self._lifecycle_lock = asyncio.Lock()
        self._shutdown_report: WorkflowShutdownReport | None = None
        self._shutdown_task: asyncio.Task[WorkflowShutdownReport] | None = None
        self._records: dict[str, _ManagedWorker] = {}

    def __repr__(self) -> str:
        return (
            "WorkflowWorkerManager("
            f"state_root={str(self.state_root)!r}, "
            f"allowed_roots={self.allowed_roots!r}, accepting={self._accepting})"
        )

    def create_run_directory(self) -> Path:
        if not self._accepting:
            raise WorkflowManagerError("workflow worker manager is shutting down")
        self.state_root.mkdir(parents=True, exist_ok=True)
        if self.state_root.is_symlink():
            raise WorkflowManagerError("workflow state root may not be a symlink")
        for _ in range(5):
            run_id = f"run-{uuid.uuid4().hex}"
            run_dir = self.state_root / run_id
            try:
                run_dir.mkdir(mode=0o700)
            except FileExistsError:
                continue
            if os.name != "nt":
                os.chmod(run_dir, 0o700)
            return run_dir.resolve(strict=True)
        raise WorkflowManagerError("could not allocate a unique workflow run directory")

    async def start_worker(
        self,
        run_dir: Path,
        *,
        mode: WorkerMode,
        workspace_root: Path | None = None,
        max_live_codex_jobs: int = 1,
    ) -> WorkflowWorkerHandle:
        diagnostic = current_startup_diagnostic()
        if diagnostic is not None:
            diagnostic.begin(WorkflowStartupStage.MANAGER_VALIDATION)
        try:
            async with self._lifecycle_lock:
                if not self._accepting:
                    raise WorkflowManagerError("workflow worker manager is shutting down")
                if mode not in {"start", "recover"}:
                    raise WorkflowManagerError("worker mode must be start or recover")
                canonical_run_dir = self._validate_run_directory(run_dir)
                run_id = canonical_run_dir.name
                current = self._records.get(run_id)
                if current is not None and current.state == "running":
                    raise WorkflowManagerError("a worker is already running for this workflow run")
                manifest = self._read_manifest(canonical_run_dir)
                live = _codex_job_live(
                    manifest,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
                canonical_workspace = self._validate_workspace(
                    canonical_run_dir, workspace_root, live
                )
                if live and not self._control_token:
                    raise WorkflowManagerError("live codex-job requires the Bridge control token")
                if diagnostic is not None:
                    diagnostic.complete_if_active(WorkflowStartupStage.MANAGER_VALIDATION)
                    diagnostic.begin(WorkflowStartupStage.WORKER_CONFIG_WRITE)

                launch_id = uuid.uuid4().hex
                config_path = canonical_run_dir / f"worker-config-{launch_id}.json"
                shutdown_file = canonical_run_dir / f"worker-shutdown-{launch_id}.request"
                worker_config = {
                    "version": 1,
                    "mode": mode,
                    "workspace_root": (
                        None if canonical_workspace is None else str(canonical_workspace)
                    ),
                    "allowed_roots": list(self.allowed_roots),
                    "shutdown_file": shutdown_file.name,
                }
                _write_private_json(config_path, worker_config)
                if diagnostic is not None:
                    diagnostic.complete_if_active(WorkflowStartupStage.WORKER_CONFIG_WRITE)
                    diagnostic.begin(WorkflowStartupStage.WORKER_SPAWN)
                record = _ManagedWorker(
                    run_dir=canonical_run_dir,
                    mode=mode,
                    live=live,
                    shutdown_file=shutdown_file,
                )
                self._records[run_id] = record
                command = self._command(mode, canonical_run_dir, config_path)
                environment = build_worker_environment(
                    os.environ,
                    control_token=self._control_token if live else None,
                )
                if diagnostic is not None:
                    diagnostic.mark_worker_spawned(None)
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=str(canonical_run_dir),
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except (OSError, ValueError) as error:
                    if diagnostic is not None:
                        diagnostic.mark_worker_spawned(False)
                        diagnostic.fail(error)
                    record.state = "failed"
                    record.error = "workflow worker process could not be started"
                    raise WorkflowManagerError(record.error) from None
                record.process = process
                if diagnostic is not None:
                    diagnostic.mark_worker_spawned(True)
                    diagnostic.complete_if_active(WorkflowStartupStage.WORKER_SPAWN)
                record.exit_task = asyncio.create_task(self._observe_exit(record))
                return WorkflowWorkerHandle(self, run_id, process.pid)
        except BaseException as error:
            if diagnostic is not None:
                diagnostic.fail(error)
            raise

    def get_status(self, run_id: str) -> WorkflowWorkerStatus | None:
        record = self._records.get(run_id)
        if record is None:
            return None
        return WorkflowWorkerStatus(
            run_id=run_id,
            state=record.state,
            pid=None if record.process is None else record.process.pid,
            returncode=record.returncode,
            error=record.error,
        )

    async def shutdown(self) -> WorkflowShutdownReport:
        async with self._lifecycle_lock:
            if self._shutdown_report is not None:
                return self._shutdown_report
            if self._shutdown_task is None:
                self._accepting = False
                records = tuple(self._records.values())
                running = tuple(record for record in records if record.state == "running")
                self._shutdown_task = asyncio.create_task(self._finish_shutdown(running, records))
            shutdown_task = self._shutdown_task
        return await asyncio.shield(shutdown_task)

    async def _finish_shutdown(
        self,
        running: tuple[_ManagedWorker, ...],
        records: tuple[_ManagedWorker, ...],
    ) -> WorkflowShutdownReport:
        for record in running:
            record.shutdown_requested = True
            try:
                _write_shutdown_signal(record.shutdown_file)
            except OSError:
                record.error = "workflow worker shutdown signal could not be written"

        tasks = [record.exit_task for record in running if record.exit_task is not None]
        pending = await _wait_tasks(tasks, self._graceful_shutdown_seconds)
        for record in running:
            if record.exit_task not in pending or record.process is None:
                continue
            try:
                record.process.terminate()
            except ProcessLookupError:
                continue
        pending = await _wait_tasks(pending, min(1.0, self._graceful_shutdown_seconds))
        for record in running:
            if record.exit_task not in pending or record.process is None:
                continue
            try:
                record.process.kill()
            except ProcessLookupError:
                continue
        pending = await _wait_tasks(pending, 1.0)
        for record in running:
            if record.exit_task in pending:
                record.state = "shutdown_failed"
                record.error = "workflow worker did not exit after terminate and kill"

        owned_turns: set[tuple[str, str]] = set()
        ownership_complete = True
        errors: list[str] = []
        ownership_runs: dict[Path, bool] = {record.run_dir: record.live for record in records}
        persisted_runs, persisted_complete = _persisted_live_run_directories(self.state_root)
        if not persisted_complete:
            ownership_complete = False
            errors.append("persisted workflow run list is incomplete")
        for run_dir in persisted_runs:
            ownership_runs.setdefault(run_dir, True)

        for run_dir, live in ownership_runs.items():
            if not live:
                continue
            database_path = run_dir / "codex_jobs.sqlite"
            turns: set[tuple[str, str]]
            try:
                from .codex_job_adapter import CodexJobAdapter

                if database_path.is_symlink() or not database_path.is_file():
                    turns, complete = set(), False
                else:
                    turns, complete = CodexJobAdapter.read_shutdown_ownership(database_path)
            except Exception:
                turns, complete = set(), False
            owned_turns.update(turns)
            if not complete:
                ownership_complete = False
                errors.append("workflow turn ownership is incomplete")
        self._shutdown_report = WorkflowShutdownReport(
            frozenset(owned_turns), ownership_complete, tuple(dict.fromkeys(errors))
        )
        return self._shutdown_report

    async def _observe_exit(self, record: _ManagedWorker) -> int:
        process = record.process
        if process is None:
            raise WorkflowManagerError("managed worker process is unavailable")
        returncode = await process.wait()
        record.returncode = returncode
        if record.shutdown_requested:
            record.state = "stopped"
            if returncode != 0 and record.error is None:
                record.error = "workflow worker exited during shutdown"
        elif returncode == 0:
            record.state = "completed"
        else:
            record.state = "failed"
        if returncode != 0 and record.error is None:
            record.error = f"workflow worker exited with code {returncode}"
        return returncode

    def _validate_run_directory(self, run_dir: Path) -> Path:
        candidate = Path(run_dir).expanduser()
        if candidate.is_symlink():
            raise WorkflowManagerError("workflow run directory may not be a symlink")
        try:
            canonical = candidate.resolve(strict=True)
            root = self.state_root.resolve(strict=True)
        except OSError:
            raise WorkflowManagerError(
                "workflow run directory or state root is unavailable"
            ) from None
        if (
            not canonical.is_dir()
            or not _RUN_ID_RE.fullmatch(canonical.name)
            or canonical.parent != root
        ):
            raise WorkflowManagerError(
                "workflow run directory must be a unique child of state root"
            )
        return canonical

    def _read_manifest(self, run_dir: Path) -> dict[str, object]:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise WorkflowManagerError("workflow run manifest is unavailable")
        try:
            value = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
        except (OSError, ValueError):
            raise WorkflowManagerError("workflow run manifest is invalid") from None
        if not isinstance(value, dict):
            raise WorkflowManagerError("workflow run manifest must be an object")
        return value

    def _validate_workspace(
        self,
        run_dir: Path,
        workspace_root: Path | None,
        live: bool,
    ) -> Path | None:
        if workspace_root is None:
            if live:
                raise WorkflowManagerError("live codex-job requires an explicit allowed workspace")
            return None
        try:
            policy = AllowedPathPolicy(self.allowed_roots)
            canonical_text = policy.validate_cwd(str(workspace_root))
        except (PathPolicyError, OSError):
            raise WorkflowManagerError(
                "workflow workspace is outside an allowed existing root"
            ) from None
        canonical = Path(canonical_text)
        if not os.access(canonical, os.W_OK):
            raise WorkflowManagerError("workflow workspace is not writable")
        if (
            canonical == run_dir
            or canonical.is_relative_to(run_dir)
            or run_dir.is_relative_to(canonical)
        ):
            raise WorkflowManagerError("workflow state and job workspace must be separate")
        return canonical

    def _command(self, mode: WorkerMode, run_dir: Path, config_path: Path) -> tuple[str, ...]:
        arguments = (
            "--mode",
            mode,
            "--run-dir",
            str(run_dir),
            "--config",
            str(config_path),
        )
        if self._frozen:
            return (self._python_executable, WORKFLOW_WORKER_FLAG, *arguments)
        return (
            self._python_executable,
            "-m",
            "codex_bridge.workflows.worker_entry",
            *arguments,
        )


def default_workflow_state_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise WorkflowManagerError("LOCALAPPDATA is required for workflow state storage")
    return Path(local_app_data) / "CodexBridge" / "workflows"


def _persisted_live_run_directories(state_root: Path) -> tuple[tuple[Path, ...], bool]:
    root = Path(state_root)
    if root.is_symlink():
        return (), False
    if not root.exists():
        return (), True
    try:
        canonical_root = root.resolve(strict=True)
    except OSError:
        return (), False
    if not canonical_root.is_dir():
        return (), False
    request_database = canonical_root / "requests.sqlite"
    if request_database.is_symlink():
        return (), False
    if not request_database.exists():
        return (), True
    if not request_database.is_file():
        return (), False
    connection: sqlite3.Connection | None = None
    try:
        uri = request_database.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only = ON")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "workflow_requests" not in tables:
            return (), False
        rows = connection.execute(
            "SELECT workflow_id, run_id, request_digest, state FROM workflow_requests "
            "WHERE state IN ('starting','running','needs_reconciliation') "
            "ORDER BY created_at DESC LIMIT 101"
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return (), False
    finally:
        if connection is not None:
            connection.close()
    if len(rows) > 100:
        return (), False

    persisted: list[Path] = []
    complete = True
    for raw_workflow_id, raw_run_id, raw_digest, raw_state in rows:
        workflow_id = str(raw_workflow_id)
        run_id = str(raw_run_id)
        request_digest = str(raw_digest)
        state = str(raw_state)
        if state not in {"starting", "running", "needs_reconciliation"}:
            complete = False
            continue
        try:
            parsed_workflow_id = str(uuid.UUID(workflow_id))
        except ValueError:
            complete = False
            continue
        if (
            parsed_workflow_id != workflow_id
            or _RUN_ID_RE.fullmatch(run_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", request_digest, re.ASCII) is None
        ):
            complete = False
            continue
        run_dir = canonical_root / run_id
        if run_dir.is_symlink():
            complete = False
            continue
        try:
            resolved = run_dir.resolve(strict=True)
        except OSError:
            complete = False
            continue
        if not resolved.is_dir() or resolved.parent != canonical_root:
            complete = False
            continue
        manifest_path = resolved / "manifest.json"
        try:
            if manifest_path.is_symlink() or not manifest_path.is_file():
                complete = False
                continue
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
            if not isinstance(manifest, dict):
                complete = False
                continue
            control = manifest.get("control")
            declared_limit = (
                control.get("max_live_codex_jobs", 1) if isinstance(control, dict) else 1
            )
            live = _codex_job_live(
                manifest,
                max_live_codex_jobs=declared_limit,
            )
        except (OSError, ValueError, WorkflowManagerError):
            complete = False
            continue
        if (
            not live
            or manifest.get("workflow_id") != workflow_id
            or manifest.get("run_id") != run_id
            or manifest.get("request_digest") != request_digest
            or manifest.get("mode") != "codex-job-live"
        ):
            complete = False
            continue
        if declared_limit in {2, 3} and manifest.get("workflow_source") != "ui-api":
            complete = False
            continue
        persisted.append(resolved)
    return tuple(persisted), complete


def build_worker_environment(
    parent_environment: Mapping[str, str],
    *,
    control_token: str | None,
) -> dict[str, str]:
    environment = {
        key: parent_environment[key] for key in _CHILD_ENVIRONMENT_KEYS if key in parent_environment
    }
    environment["PYTHONUTF8"] = "1"
    if control_token and control_token.strip():
        environment[CONTROL_TOKEN_ENV] = control_token
    return environment


def missing_workflow_dependencies() -> tuple[str, ...]:
    from importlib.util import find_spec

    return tuple(name for name in _WORKFLOW_DEPENDENCIES if find_spec(name) is None)


def _codex_job_live(
    manifest: Mapping[str, object],
    *,
    max_live_codex_jobs: object = 1,
) -> bool:
    control = manifest.get("control", {})
    if not isinstance(control, dict):
        raise WorkflowManagerError("workflow manifest control must be an object")
    if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
        raise WorkflowManagerError("worker max_live_codex_jobs must be the integer 1, 2, or 3")
    declared_limit = control.get("max_live_codex_jobs", 1)
    if type(declared_limit) is not int or declared_limit not in {1, 2, 3}:
        raise WorkflowManagerError("workflow manifest max_live_codex_jobs is invalid")
    if declared_limit != max_live_codex_jobs:
        raise WorkflowManagerError("worker max_live_codex_jobs does not match the manifest")
    if declared_limit == 1 and "max_live_codex_jobs" in control:
        raise WorkflowManagerError("single-job manifests must omit max_live_codex_jobs")
    if declared_limit in {2, 3}:
        expected_control = {
            "codex_job_live",
            "codex_bridge_port",
            "codex_job_max_polls",
            "codex_job_poll_interval_seconds",
            "max_live_codex_jobs",
        }
        bridge_port = control.get("codex_bridge_port")
        if (
            set(control) != expected_control
            or control.get("codex_job_live") is not True
            or type(bridge_port) is not int
            or not 1 <= bridge_port <= 65535
            or manifest.get("codex_bridge_port") != bridge_port
            or control.get("codex_job_max_polls") != 600
            or control.get("codex_job_poll_interval_seconds") != 0.2
            or manifest.get("max_tasks") != 10_000
        ):
            message = (
                "two-job workflow manifest control is invalid"
                if declared_limit == 2
                else "three-job workflow manifest control is invalid"
            )
            raise WorkflowManagerError(message)
    live = control.get("codex_job_live", False)
    if not isinstance(live, bool):
        raise WorkflowManagerError("workflow manifest codex_job_live must be a boolean")
    return live


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("workflow manifest contains a duplicate key")
        result[key] = value
    return result


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        os.chmod(path, 0o600)


def _write_shutdown_signal(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise OSError("worker shutdown signal already exists")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("shutdown\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        os.chmod(path, 0o600)


async def _wait_tasks(
    tasks: list[asyncio.Task[int]] | set[asyncio.Task[int]], timeout: float
) -> set[asyncio.Task[int]]:
    if not tasks:
        return set()
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    return set(pending)
