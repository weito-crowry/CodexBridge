from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ..paths import AllowedPathPolicy, PathPolicyError
from .diagnostic_log import append_lifecycle_event
from .manager import WorkflowManagerError, missing_workflow_dependencies
from .startup_diagnostics import (
    WorkflowStartupDiagnostic,
    WorkflowStartupFailureReason,
    WorkflowStartupStage,
    current_startup_diagnostic,
    startup_diagnostic_scope,
)

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_CODEX_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_WORKFLOW_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
        "unknown",
    }
)
_ACTIVE_STATES = frozenset({"queued", "starting", "running", "needs_reconciliation", "unknown"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "failed_to_start"})
_MAX_WORKFLOW_YAML_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 64 * 1024
_MAX_RUN_FILE_BYTES = 2 * 1024 * 1024
_MAX_ACTIVE_CHECKS = 100
_WORKER_START_TIMEOUT_SECONDS = 10.0


class WorkflowWorkerStarter(Protocol):
    state_root: Path
    allowed_roots: tuple[str, ...]

    def create_run_directory(self) -> Path: ...

    async def start_worker(
        self,
        run_dir: Path,
        *,
        mode: Literal["start", "recover"],
        workspace_root: Path | None = None,
        max_live_codex_jobs: int = 1,
    ) -> object: ...


class WorkflowRequestError(ValueError):
    """A request does not match the fixed live codex-job contract."""


class WorkflowRequestConflict(RuntimeError):
    """A request ID was reused for different content."""


class WorkflowConcurrencyConflict(RuntimeError):
    """A different live workflow already occupies the single-job slot."""


class WorkflowNotFoundError(LookupError):
    """A workflow ID is absent from the durable request ledger."""


class WorkflowServiceUnavailable(RuntimeError):
    """The durable service or worker manager cannot accept the request."""


class WorkflowReconciliationRequired(RuntimeError):
    """The workflow may have started and must not be submitted again."""

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__("workflow start needs reconciliation")
        self.snapshot = snapshot


class WorkflowStartFailed(RuntimeError):
    """The worker manager confirmed that a workflow worker did not start."""

    def __init__(self, snapshot: WorkflowSnapshot) -> None:
        super().__init__("workflow worker did not start")
        self.snapshot = snapshot


@dataclass(frozen=True, slots=True)
class WorkflowJobSnapshot:
    job_id: str
    task_identity: str
    state: str
    thread_id: str | None
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    workflow_id: str
    run_id: str
    status: str
    created_at: str
    updated_at: str
    worker_state: str
    terminal: bool
    job_id: str | None = None
    thread_id: str | None = None
    max_live_codex_jobs: int = 1
    jobs: tuple[WorkflowJobSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowResultSnapshot:
    workflow: WorkflowSnapshot
    result: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    client_request_id: str
    definition_text: str
    definition_sha256: str
    input_text: str
    input_sha256: str
    input_data: Any
    workspace_root: str
    request_digest: str
    max_live_codex_jobs: int


@dataclass(frozen=True, slots=True)
class _RequestRow:
    client_request_id: str
    workflow_id: str
    run_id: str
    request_digest: str
    state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class _RunInspection:
    status: str
    worker_state: str
    job_id: str | None
    thread_id: str | None
    result: dict[str, Any] | None
    updated_at: str
    max_live_codex_jobs: int = 1
    jobs: tuple[WorkflowJobSnapshot, ...] = ()


class WorkflowService:
    """Durable request admission and read-only workflow state projection."""

    def __init__(
        self,
        worker_manager: WorkflowWorkerStarter,
        *,
        codex_bridge_port: int,
    ) -> None:
        if type(codex_bridge_port) is not int or not 1 <= codex_bridge_port <= 65535:
            raise ValueError("workflow CodexBridge port is invalid")
        self._worker_manager = worker_manager
        self._state_root = Path(worker_manager.state_root)
        self._allowed_roots = tuple(worker_manager.allowed_roots)
        self._codex_bridge_port = codex_bridge_port
        self._start_lock = asyncio.Lock()

    async def start_workflow(
        self,
        *,
        client_request_id: str,
        workflow_yaml: str,
        input_data: Any,
        workspace_root: str,
        max_live_codex_jobs: int = 1,
    ) -> WorkflowSnapshot:
        missing = await asyncio.to_thread(missing_workflow_dependencies)
        if missing:
            raise WorkflowServiceUnavailable("workflow dependencies are unavailable")
        try:
            prepared = await asyncio.to_thread(
                self._prepare_request,
                client_request_id,
                workflow_yaml,
                input_data,
                workspace_root,
                max_live_codex_jobs,
                False,
            )
        except WorkflowRequestError:
            raise
        except (ImportError, ModuleNotFoundError):
            raise WorkflowServiceUnavailable("workflow dependencies are unavailable") from None
        async with self._start_lock:
            try:
                existing = await asyncio.to_thread(
                    _find_request, self._state_root, prepared.client_request_id
                )
            except (OSError, sqlite3.Error, ValueError):
                raise WorkflowServiceUnavailable("workflow request ledger is unavailable") from None
            if existing is not None:
                if existing.request_digest != prepared.request_digest:
                    append_lifecycle_event(
                        self._state_root / existing.run_id,
                        source="bridge",
                        event="request_conflict",
                        run_id=existing.run_id,
                        workflow_id=existing.workflow_id,
                        request_digest=prepared.request_digest,
                        state=existing.state,
                        reason_code="request_digest_mismatch",
                    )
                    raise WorkflowRequestConflict(
                        "request ID is already bound to different workflow content"
                    )
                try:
                    await asyncio.to_thread(self._validate_prepared_request, prepared)
                except WorkflowRequestError:
                    raise
                except (ImportError, ModuleNotFoundError):
                    raise WorkflowServiceUnavailable(
                        "workflow dependencies are unavailable"
                    ) from None
                return await self._replay(existing, prepared.request_digest)

            try:
                await asyncio.to_thread(self._validate_prepared_request, prepared)
            except WorkflowRequestError:
                raise
            except (ImportError, ModuleNotFoundError):
                raise WorkflowServiceUnavailable("workflow dependencies are unavailable") from None

            try:
                row, inserted = await asyncio.to_thread(
                    _reserve_request,
                    self._worker_manager,
                    self._state_root,
                    self._codex_bridge_port,
                    prepared,
                )
            except WorkflowRequestConflict:
                raise
            except WorkflowConcurrencyConflict:
                raise
            except WorkflowServiceUnavailable:
                raise
            except (OSError, sqlite3.Error, WorkflowManagerError, ValueError):
                raise WorkflowServiceUnavailable(
                    "workflow request could not be persisted"
                ) from None

            if not inserted:
                return await self._replay(row, prepared.request_digest)

            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_reserved",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                request_digest=row.request_digest,
            )

            diagnostic = WorkflowStartupDiagnostic(
                row.workflow_id,
                row.run_id,
                self._state_root / row.run_id,
            )
            with startup_diagnostic_scope(diagnostic):
                diagnostic.begin(WorkflowStartupStage.REQUEST_RESERVED)
                diagnostic.complete_if_active(WorkflowStartupStage.REQUEST_RESERVED)
                return await self._start_reserved_workflow(row, prepared, diagnostic)

    async def _start_reserved_workflow(
        self,
        row: _RequestRow,
        prepared: _PreparedRequest,
        diagnostic: WorkflowStartupDiagnostic,
    ) -> WorkflowSnapshot:
        starting_state_persisted = False
        try:
            diagnostic.begin(WorkflowStartupStage.STARTING_STATE_PERSISTED)
            await self._transition_state(
                row.run_id,
                "starting",
                previous_state=row.state,
                workflow_id=row.workflow_id,
            )
            starting_state_persisted = True
            diagnostic.complete_if_active(WorkflowStartupStage.STARTING_STATE_PERSISTED)
            diagnostic.begin(WorkflowStartupStage.MANAGER_VALIDATION)
            if prepared.max_live_codex_jobs in {2, 3}:
                await self._worker_manager.start_worker(
                    self._run_directory(row.run_id),
                    mode="start",
                    workspace_root=Path(prepared.workspace_root),
                    max_live_codex_jobs=prepared.max_live_codex_jobs,
                )
            else:
                await self._worker_manager.start_worker(
                    self._run_directory(row.run_id),
                    mode="start",
                    workspace_root=Path(prepared.workspace_root),
                )
            diagnostic.complete_if_active(WorkflowStartupStage.MANAGER_VALIDATION)
        except WorkflowStartFailed as error:
            diagnostic.fail(error)
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state=error.snapshot.status,
                reason_code="manager_start_failed",
                exception_class=type(error).__name__,
            )
            raise
        except WorkflowManagerError as error:
            diagnostic.fail(error)
            await self._transition_state(
                row.run_id,
                "failed_to_start",
                previous_state="starting",
                workflow_id=row.workflow_id,
                reason_code="manager_start_failed",
            )
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state="failed_to_start",
                reason_code="manager_start_failed",
                exception_class=type(error).__name__,
            )
            raise WorkflowStartFailed(
                await self._snapshot_for_row(row, forced_state="failed_to_start")
            ) from None
        except asyncio.CancelledError as error:
            diagnostic.fail(error)
            await asyncio.shield(
                self._transition_state(
                    row.run_id,
                    "needs_reconciliation",
                    previous_state=("starting" if starting_state_persisted else None),
                    workflow_id=row.workflow_id,
                    reason_code="request_start_cancelled",
                )
            )
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state="needs_reconciliation",
                reason_code="request_start_cancelled",
                exception_class=type(error).__name__,
            )
            raise
        except BaseException as error:
            diagnostic.fail(error)
            await self._transition_state(
                row.run_id,
                "needs_reconciliation",
                previous_state=("starting" if starting_state_persisted else None),
                workflow_id=row.workflow_id,
                reason_code="unexpected_start_failure",
            )
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state="needs_reconciliation",
                reason_code="unexpected_start_failure",
                exception_class=type(error).__name__,
            )
            raise WorkflowReconciliationRequired(
                await self._snapshot_for_row(row, forced_state="needs_reconciliation")
            ) from None

        diagnostic.begin(WorkflowStartupStage.WORKER_READINESS_WAIT)
        try:
            startup_state = await self._wait_for_worker_start(row)
        except BaseException as error:
            diagnostic.fail(error)
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state="starting",
                reason_code="readiness_wait_failed",
                exception_class=type(error).__name__,
            )
            raise
        if startup_state == "running":
            diagnostic.complete_if_active(WorkflowStartupStage.WORKER_READINESS_WAIT)
            await self._transition_state(
                row.run_id,
                "running",
                previous_state="starting",
                workflow_id=row.workflow_id,
            )
            return await self._snapshot_for_row(row)
        if startup_state in {"succeeded", "failed"}:
            diagnostic.complete_if_active(WorkflowStartupStage.WORKER_READINESS_WAIT)
            await self._transition_state(
                row.run_id,
                startup_state,
                previous_state="starting",
                workflow_id=row.workflow_id,
            )
            return await self._snapshot_for_row(row)
        if startup_state == "failed_to_start":
            diagnostic.fail(None, reason=WorkflowStartupFailureReason.WORKER_FAILED)
            await self._transition_state(
                row.run_id,
                "failed_to_start",
                previous_state="starting",
                workflow_id=row.workflow_id,
                reason_code="worker_failed_before_ready",
            )
            append_lifecycle_event(
                self._run_directory(row.run_id),
                source="bridge",
                event="request_start_failed",
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                state="failed_to_start",
                reason_code="worker_failed_before_ready",
            )
            raise WorkflowStartFailed(
                await self._snapshot_for_row(row, forced_state="failed_to_start")
            )
        diagnostic.fail(None, reason=WorkflowStartupFailureReason.READINESS_UNCONFIRMED)
        await self._transition_state(
            row.run_id,
            "needs_reconciliation",
            previous_state="starting",
            workflow_id=row.workflow_id,
            reason_code="readiness_unconfirmed",
        )
        append_lifecycle_event(
            self._run_directory(row.run_id),
            source="bridge",
            event="request_start_failed",
            run_id=row.run_id,
            workflow_id=row.workflow_id,
            state="needs_reconciliation",
            reason_code="readiness_unconfirmed",
        )
        raise WorkflowReconciliationRequired(
            await self._snapshot_for_row(row, forced_state="needs_reconciliation")
        )

    async def list_workflows(self, *, limit: int = 50) -> list[WorkflowSnapshot]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkflowRequestError("workflow list limit must be between 1 and 100")
        try:
            return await asyncio.to_thread(_list_workflows, self._state_root, limit)
        except WorkflowServiceUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise WorkflowServiceUnavailable("workflow request ledger is unavailable") from None

    async def get_workflow(self, workflow_id: str) -> WorkflowSnapshot:
        normalized = _normalize_workflow_id(workflow_id)
        try:
            row = await asyncio.to_thread(_find_workflow, self._state_root, normalized)
            if row is None:
                raise WorkflowNotFoundError("workflow was not found")
            return await asyncio.to_thread(_snapshot, self._state_root, row)
        except WorkflowNotFoundError:
            raise
        except WorkflowServiceUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise WorkflowServiceUnavailable("workflow state is unavailable") from None

    async def get_result(self, workflow_id: str) -> WorkflowResultSnapshot:
        normalized = _normalize_workflow_id(workflow_id)
        try:
            row = await asyncio.to_thread(_find_workflow, self._state_root, normalized)
            if row is None:
                raise WorkflowNotFoundError("workflow was not found")
            inspection = await asyncio.to_thread(_inspect_run, self._state_root, row)
            snapshot = _snapshot_from_inspection(row, inspection)
            return WorkflowResultSnapshot(snapshot, inspection.result)
        except WorkflowNotFoundError:
            raise
        except WorkflowServiceUnavailable:
            raise
        except (OSError, sqlite3.Error, ValueError):
            raise WorkflowServiceUnavailable("workflow result is unavailable") from None

    async def _replay(self, row: _RequestRow, request_digest: str) -> WorkflowSnapshot:
        if row.request_digest != request_digest:
            raise WorkflowRequestConflict(
                "request ID is already bound to different workflow content"
            )
        replay_reason = (
            "queued_replay_failed_to_start" if row.state == "queued" else "same_request_id"
        )
        append_lifecycle_event(
            self._run_directory(row.run_id),
            source="bridge",
            event="request_replayed",
            run_id=row.run_id,
            workflow_id=row.workflow_id,
            request_digest=request_digest,
            state=row.state,
            reason_code=replay_reason,
        )
        if row.state == "queued":
            await self._transition_state(
                row.run_id,
                "failed_to_start",
                previous_state="queued",
                workflow_id=row.workflow_id,
                reason_code="queued_replay_failed_to_start",
            )
            return await self._snapshot_for_row(row, forced_state="failed_to_start")
        return await self._snapshot_for_row(row)

    async def _wait_for_worker_start(self, row: _RequestRow) -> str:
        diagnostic = current_startup_diagnostic()
        deadline = time.monotonic() + _WORKER_START_TIMEOUT_SECONDS
        while True:
            try:
                inspection = await asyncio.to_thread(_inspect_run, self._state_root, row)
            except (OSError, sqlite3.Error, ValueError) as error:
                if diagnostic is not None:
                    diagnostic.fail(
                        error,
                        reason=WorkflowStartupFailureReason.READINESS_INSPECTION_FAILED,
                    )
                return "needs_reconciliation"
            if inspection.status in {"running", "succeeded", "failed", "failed_to_start"}:
                return inspection.status
            if inspection.status == "needs_reconciliation" and inspection.worker_state == "failed":
                if diagnostic is not None:
                    diagnostic.fail(None, reason=WorkflowStartupFailureReason.WORKER_FAILED)
                return "needs_reconciliation"
            if time.monotonic() >= deadline:
                if diagnostic is not None:
                    diagnostic.fail(None, reason=WorkflowStartupFailureReason.READINESS_TIMEOUT)
                return "needs_reconciliation"
            await asyncio.sleep(0.05)

    async def _snapshot_for_row(
        self,
        row: _RequestRow,
        *,
        forced_state: str | None = None,
    ) -> WorkflowSnapshot:
        try:
            inspection = await asyncio.to_thread(_inspect_run, self._state_root, row)
        except (OSError, sqlite3.Error, ValueError):
            inspection = _RunInspection(
                "needs_reconciliation", "unknown", None, None, None, row.updated_at
            )
        if forced_state is not None:
            inspection = _RunInspection(
                forced_state,
                inspection.worker_state,
                inspection.job_id,
                inspection.thread_id,
                inspection.result,
                inspection.updated_at,
                inspection.max_live_codex_jobs,
                inspection.jobs,
            )
        return _snapshot_from_inspection(row, inspection)

    async def _set_state(self, run_id: str, state: str) -> None:
        try:
            await asyncio.to_thread(_update_request_state, self._state_root, run_id, state)
        except (OSError, sqlite3.Error, ValueError) as error:
            if state == "starting":
                diagnostic = current_startup_diagnostic()
                if diagnostic is not None:
                    diagnostic.fail(error)
            raise WorkflowServiceUnavailable("workflow request state could not be saved") from None

    async def _transition_state(
        self,
        run_id: str,
        state: str,
        *,
        previous_state: str | None,
        workflow_id: str,
        reason_code: str | None = None,
    ) -> None:
        await self._set_state(run_id, state)
        append_lifecycle_event(
            self._run_directory(run_id),
            source="bridge",
            event="workflow_state_changed",
            run_id=run_id,
            workflow_id=workflow_id,
            previous_state=previous_state,
            state=state,
            reason_code=reason_code,
        )

    def _run_directory(self, run_id: str) -> Path:
        if _RUN_ID.fullmatch(run_id) is None:
            raise WorkflowServiceUnavailable("workflow run identity is invalid")
        return self._state_root / run_id

    def _prepare_request(
        self,
        client_request_id: str,
        workflow_yaml: str,
        input_data: Any,
        workspace_root: str,
        max_live_codex_jobs: int = 1,
        validate_definition: bool = True,
    ) -> _PreparedRequest:
        if (
            not isinstance(client_request_id, str)
            or _REQUEST_ID.fullmatch(client_request_id) is None
        ):
            raise WorkflowRequestError("client_request_id is invalid")
        if not isinstance(workflow_yaml, str):
            raise WorkflowRequestError("workflow_yaml must be a string")
        try:
            encoded_yaml = workflow_yaml.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkflowRequestError("workflow_yaml is invalid UTF-8") from None
        if not encoded_yaml or len(encoded_yaml) > _MAX_WORKFLOW_YAML_BYTES:
            raise WorkflowRequestError("workflow_yaml must not exceed 64 KiB")
        if not isinstance(workspace_root, str):
            raise WorkflowRequestError("workspace_root must be an absolute allowed directory")
        if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
            raise WorkflowRequestError("max_live_codex_jobs must be the integer 1, 2, or 3")

        from .interpreter import (
            OwsError,
            parse_workflow_text,
            three_live_codex_job_input_kind,
            validate_three_live_codex_job_input,
            validate_workflow,
        )

        try:
            definition = parse_workflow_text(workflow_yaml)
            if validate_definition:
                validate_workflow(
                    definition,
                    codex_job_live=True,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
            if max_live_codex_jobs == 3:
                input_kind = three_live_codex_job_input_kind(definition)
                validate_three_live_codex_job_input(input_data, input_kind=input_kind)
            definition_text = _canonical_json(definition)
            input_text = _canonical_json(input_data)
        except (OwsError, TypeError, ValueError, RecursionError):
            raise WorkflowRequestError("workflow YAML or input is invalid") from None
        try:
            definition_text.encode("utf-8")
            input_text.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkflowRequestError("workflow YAML or input is invalid") from None
        try:
            policy = AllowedPathPolicy(self._allowed_roots)
            canonical_workspace = policy.validate_cwd(workspace_root)
        except (PathPolicyError, OSError):
            raise WorkflowRequestError("workspace_root is outside allowed roots") from None

        state_root = self._state_root.resolve(strict=False)
        workspace_path = Path(canonical_workspace)
        if (
            state_root == workspace_path
            or state_root.is_relative_to(workspace_path)
            or workspace_path.is_relative_to(state_root)
        ):
            raise WorkflowRequestError("workflow state and workspace must be separate")

        definition_hash = hashlib.sha256((definition_text + "\n").encode("utf-8")).hexdigest()
        input_hash = hashlib.sha256(_canonical_json(input_data).encode("utf-8")).hexdigest()
        digest_payload = {
            "workflow": definition,
            "input": input_data,
            "workspace_root": canonical_workspace,
        }
        if max_live_codex_jobs in {2, 3}:
            digest_payload["max_live_codex_jobs"] = max_live_codex_jobs
        digest = hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
        return _PreparedRequest(
            client_request_id=client_request_id,
            definition_text=definition_text,
            definition_sha256=definition_hash,
            input_text=input_text,
            input_sha256=input_hash,
            input_data=input_data,
            workspace_root=canonical_workspace,
            request_digest=digest,
            max_live_codex_jobs=max_live_codex_jobs,
        )

    @staticmethod
    def _validate_prepared_request(prepared: _PreparedRequest) -> None:
        from .interpreter import (
            OwsError,
            three_live_codex_job_input_kind,
            validate_three_live_codex_job_input,
            validate_workflow,
        )

        try:
            definition = _strict_json(prepared.definition_text.encode("utf-8"))
            validate_workflow(
                definition,
                codex_job_live=True,
                max_live_codex_jobs=prepared.max_live_codex_jobs,
            )
            if prepared.max_live_codex_jobs == 3:
                input_kind = three_live_codex_job_input_kind(definition)
                validate_three_live_codex_job_input(prepared.input_data, input_kind=input_kind)
        except (OwsError, TypeError, ValueError, RecursionError):
            raise WorkflowRequestError("workflow YAML or input is invalid") from None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _ensure_state_root(state_root: Path, *, create: bool) -> Path | None:
    candidate = Path(state_root).expanduser()
    if candidate.is_symlink():
        raise WorkflowServiceUnavailable("workflow state root is invalid")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.exists():
        return None
    try:
        canonical = candidate.resolve(strict=True)
    except OSError:
        raise WorkflowServiceUnavailable("workflow state root is unavailable") from None
    if not canonical.is_dir() or canonical != candidate.resolve(strict=False):
        raise WorkflowServiceUnavailable("workflow state root is invalid")
    return canonical


def _request_database(state_root: Path) -> Path:
    root = _ensure_state_root(state_root, create=True)
    if root is None:
        raise WorkflowServiceUnavailable("workflow state root is unavailable")
    database = root / "requests.sqlite"
    if database.is_symlink():
        raise WorkflowServiceUnavailable("workflow request ledger is invalid")
    return database


def _connect_write(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_requests (
            client_request_id TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN (
                    'queued','starting','running','succeeded','failed',
                    'failed_to_start','needs_reconciliation'
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS workflow_requests_created_idx "
        "ON workflow_requests(created_at DESC, workflow_id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS workflow_requests_state_idx ON workflow_requests(state)"
    )
    return connection


def _connect_read(database: Path) -> sqlite3.Connection:
    if database.is_symlink() or not database.is_file():
        raise WorkflowServiceUnavailable("workflow request ledger is unavailable")
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _row_from_sql(row: sqlite3.Row) -> _RequestRow:
    result = _RequestRow(
        client_request_id=str(row["client_request_id"]),
        workflow_id=str(row["workflow_id"]),
        run_id=str(row["run_id"]),
        request_digest=str(row["request_digest"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
    if (
        _REQUEST_ID.fullmatch(result.client_request_id) is None
        or _normalize_workflow_id(result.workflow_id) != result.workflow_id
        or _RUN_ID.fullmatch(result.run_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", result.request_digest, re.ASCII) is None
        or result.state not in _WORKFLOW_STATES
    ):
        raise WorkflowServiceUnavailable("workflow request ledger row is invalid")
    return result


def _find_request(state_root: Path, client_request_id: str) -> _RequestRow | None:
    root = _ensure_state_root(state_root, create=False)
    if root is None:
        return None
    database = root / "requests.sqlite"
    if database.is_symlink():
        raise WorkflowServiceUnavailable("workflow request ledger is invalid")
    if not database.exists():
        return None
    connection = _connect_read(database)
    try:
        row = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _row_from_sql(row)


def _find_workflow(state_root: Path, workflow_id: str) -> _RequestRow | None:
    root = _ensure_state_root(state_root, create=False)
    if root is None:
        return None
    database = root / "requests.sqlite"
    if database.is_symlink():
        raise WorkflowServiceUnavailable("workflow request ledger is invalid")
    if not database.exists():
        return None
    connection = _connect_read(database)
    try:
        row = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else _row_from_sql(row)


def _list_workflows(state_root: Path, limit: int) -> list[WorkflowSnapshot]:
    root = _ensure_state_root(state_root, create=False)
    if root is None:
        return []
    database = root / "requests.sqlite"
    if database.is_symlink():
        raise WorkflowServiceUnavailable("workflow request ledger is invalid")
    if not database.exists():
        return []
    connection = _connect_read(database)
    try:
        rows = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests "
            "ORDER BY created_at DESC, workflow_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    snapshots: list[WorkflowSnapshot] = []
    for raw in rows:
        row = _row_from_sql(raw)
        snapshots.append(_snapshot(state_root, row))
    return snapshots


def _reserve_request(
    worker_manager: WorkflowWorkerStarter,
    state_root: Path,
    codex_bridge_port: int,
    prepared: _PreparedRequest,
) -> tuple[_RequestRow, bool]:
    database = _request_database(state_root)
    connection = _connect_write(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_sql = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests WHERE client_request_id = ?",
            (prepared.client_request_id,),
        ).fetchone()
        if existing_sql is not None:
            existing = _row_from_sql(existing_sql)
            if existing.request_digest != prepared.request_digest:
                raise WorkflowRequestConflict(
                    "request ID is already bound to different workflow content"
                )
            connection.execute("COMMIT")
            return existing, False

        candidates = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests "
            "WHERE state IN ('queued','starting','running','needs_reconciliation') "
            "ORDER BY created_at DESC LIMIT ?",
            (_MAX_ACTIVE_CHECKS + 1,),
        ).fetchall()
        if len(candidates) > _MAX_ACTIVE_CHECKS:
            raise WorkflowConcurrencyConflict("workflow capacity is occupied")
        for candidate_sql in candidates:
            candidate = _row_from_sql(candidate_sql)
            if candidate.state == "queued":
                raise WorkflowConcurrencyConflict("workflow capacity is occupied")
            inspection = _inspect_run(state_root, candidate)
            if inspection.status in _TERMINAL_STATES:
                connection.execute(
                    "UPDATE workflow_requests SET state = ?, updated_at = ? WHERE run_id = ?",
                    (inspection.status, _utc_now(), candidate.run_id),
                )
            elif inspection.status in _ACTIVE_STATES:
                raise WorkflowConcurrencyConflict("workflow capacity is occupied")

        workflow_id = str(uuid.uuid4())
        run_dir = Path(worker_manager.create_run_directory())
        run_id = run_dir.name
        if _RUN_ID.fullmatch(run_id) is None or _validated_run_directory(
            state_root, run_id
        ) != run_dir.resolve(strict=True):
            raise WorkflowServiceUnavailable("workflow manager returned an invalid run directory")
        now = _utc_now()
        control: dict[str, object] = {
            "codex_job_live": True,
            "codex_bridge_port": codex_bridge_port,
            "codex_job_max_polls": 600,
            "codex_job_poll_interval_seconds": 0.2,
        }
        if prepared.max_live_codex_jobs in {2, 3}:
            control["max_live_codex_jobs"] = prepared.max_live_codex_jobs
        manifest = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "workflow_source": "ui-api",
            "mode": "codex-job-live",
            "workspace_root": prepared.workspace_root,
            "request_digest": prepared.request_digest,
            "definition_sha256": prepared.definition_sha256,
            "input_sha256": prepared.input_sha256,
            "max_tasks": 10_000,
            "codex_bridge_port": codex_bridge_port,
            "control": control,
            "created_at": now,
        }
        _write_new_file(run_dir / "workflow.yaml", prepared.definition_text.encode("utf-8"))
        _write_new_file(run_dir / "input.json", prepared.input_text.encode("utf-8"))
        _write_new_file(run_dir / "manifest.json", _canonical_json(manifest).encode("utf-8"))
        connection.execute(
            "INSERT INTO workflow_requests "
            "(client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (
                prepared.client_request_id,
                workflow_id,
                run_id,
                prepared.request_digest,
                now,
                now,
            ),
        )
        connection.execute("COMMIT")
        return (
            _RequestRow(
                prepared.client_request_id,
                workflow_id,
                run_id,
                prepared.request_digest,
                "queued",
                now,
                now,
            ),
            True,
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _write_new_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if path.exists() or path.is_symlink() or temporary.exists() or temporary.is_symlink():
        raise WorkflowServiceUnavailable("workflow snapshot path is already occupied")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _update_request_state(state_root: Path, run_id: str, state: str) -> None:
    if not _RUN_ID.fullmatch(run_id) or state not in _WORKFLOW_STATES:
        raise ValueError("workflow state update is invalid")
    database = _request_database(state_root)
    connection = _connect_write(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE workflow_requests SET state = ?, updated_at = ? WHERE run_id = ?",
            (state, _utc_now(), run_id),
        )
        if cursor.rowcount != 1:
            raise WorkflowServiceUnavailable("workflow request row is unavailable")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _normalize_workflow_id(workflow_id: str) -> str:
    if not isinstance(workflow_id, str):
        raise ValueError("workflow_id is invalid")
    try:
        parsed = uuid.UUID(workflow_id)
    except (ValueError, AttributeError):
        raise ValueError("workflow_id is invalid") from None
    if str(parsed) != workflow_id.lower():
        raise ValueError("workflow_id is invalid")
    return str(parsed)


def _validated_run_directory(state_root: Path, run_id: str) -> Path:
    if _RUN_ID.fullmatch(run_id) is None:
        raise WorkflowServiceUnavailable("workflow run identity is invalid")
    root = _ensure_state_root(state_root, create=False)
    if root is None:
        raise WorkflowServiceUnavailable("workflow state root is unavailable")
    candidate = root / run_id
    if candidate.is_symlink():
        raise WorkflowServiceUnavailable("workflow run directory is invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise WorkflowServiceUnavailable("workflow run directory is unavailable") from None
    if not resolved.is_dir() or resolved.parent != root or resolved.name != run_id:
        raise WorkflowServiceUnavailable("workflow run directory is invalid")
    return resolved


def _read_regular_file(path: Path, *, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("workflow file is unavailable")
    try:
        if path.stat().st_size > limit:
            raise ValueError("workflow file exceeds its size limit")
        return path.read_bytes()
    except OSError:
        raise ValueError("workflow file is unavailable") from None


def _strict_json(payload: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON value: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    return json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_object)


def _inspect_run(state_root: Path, row: _RequestRow) -> _RunInspection:
    base_updated = row.updated_at
    try:
        run_dir = _validated_run_directory(state_root, row.run_id)
        manifest_value = _strict_json(
            _read_regular_file(run_dir / "manifest.json", limit=32 * 1024)
        )
        if not isinstance(manifest_value, dict):
            raise ValueError("manifest is invalid")
        if (
            manifest_value.get("workflow_id") != row.workflow_id
            or manifest_value.get("run_id") != row.run_id
            or manifest_value.get("request_digest") != row.request_digest
            or manifest_value.get("mode") != "codex-job-live"
            or manifest_value.get("workflow_source") != "ui-api"
            or manifest_value.get("max_tasks") != 10_000
        ):
            raise ValueError("manifest identity does not match the request ledger")
        control = manifest_value.get("control")
        base_control_keys = {
            "codex_job_live",
            "codex_bridge_port",
            "codex_job_max_polls",
            "codex_job_poll_interval_seconds",
        }
        if not isinstance(control, dict):
            raise ValueError("manifest live control is invalid")
        max_live_codex_jobs = 1
        if set(control) == base_control_keys | {"max_live_codex_jobs"}:
            if type(control.get("max_live_codex_jobs")) is not int:
                raise ValueError("manifest live job limit is invalid")
            max_live_codex_jobs = control["max_live_codex_jobs"]
        elif set(control) != base_control_keys:
            raise ValueError("manifest live control is invalid")
        bridge_port = control.get("codex_bridge_port")
        if (
            max_live_codex_jobs not in {1, 2, 3}
            or control.get("codex_job_live") is not True
            or type(bridge_port) is not int
            or not 1 <= bridge_port <= 65535
            or manifest_value.get("codex_bridge_port") != bridge_port
            or control.get("codex_job_max_polls") != 600
            or control.get("codex_job_poll_interval_seconds") != 0.2
            or (max_live_codex_jobs == 1 and "max_live_codex_jobs" in control)
        ):
            raise ValueError("manifest live control is invalid")
        definition_text = _read_regular_file(
            run_dir / "workflow.yaml", limit=_MAX_WORKFLOW_YAML_BYTES
        ).decode("utf-8")
        input_data = _strict_json(
            _read_regular_file(run_dir / "input.json", limit=_MAX_RUN_FILE_BYTES)
        )
        definition = _strict_json(definition_text.encode("utf-8"))
        expected_task_identities: tuple[str, ...] = ()
        three_job_input_kind = "issues"
        if max_live_codex_jobs in {2, 3}:
            from .interpreter import (
                OwsError,
                three_live_codex_job_input_kind,
                validate_three_live_codex_job_input,
                validate_workflow,
            )

            try:
                validate_workflow(
                    definition,
                    codex_job_live=True,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
                if max_live_codex_jobs == 3:
                    three_job_input_kind = three_live_codex_job_input_kind(definition)
                    validate_three_live_codex_job_input(input_data, input_kind=three_job_input_kind)
            except (OwsError, TypeError, ValueError, RecursionError):
                raise ValueError("multi-job workflow definition or input is invalid") from None
            if max_live_codex_jobs == 2:
                expected_task_identities = _two_job_task_identities(definition)
            else:
                expected_task_identities = _three_job_task_identities(definition, input_data)
        workspace = manifest_value.get("workspace_root")
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise ValueError("manifest workspace is invalid")
        digest_payload = {
            "workflow": definition,
            "input": input_data,
            "workspace_root": workspace,
        }
        if max_live_codex_jobs in {2, 3}:
            digest_payload["max_live_codex_jobs"] = max_live_codex_jobs
        digest = hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
        if (
            digest != row.request_digest
            or hashlib.sha256(definition_text.encode("utf-8")).hexdigest()
            != manifest_value.get("definition_sha256")
            or hashlib.sha256(_canonical_json(input_data).encode("utf-8")).hexdigest()
            != manifest_value.get("input_sha256")
        ):
            raise ValueError("workflow request digest does not match stored files")
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        sqlite3.Error,
        WorkflowServiceUnavailable,
    ):
        return _RunInspection("needs_reconciliation", "unknown", None, None, None, base_updated)

    worker_state = "not_started"
    worker_status_path = run_dir / "worker-entry-status.json"
    if worker_status_path.exists() or worker_status_path.is_symlink():
        try:
            raw_status = _strict_json(_read_regular_file(worker_status_path, limit=4096))
            if not isinstance(raw_status, dict) or raw_status.get("state") not in {
                "running",
                "completed",
                "failed",
            }:
                raise ValueError("worker status is invalid")
            worker_state = str(raw_status["state"])
            base_updated = _latest_file_time(base_updated, worker_status_path)
        except (OSError, ValueError, TypeError):
            return _RunInspection("needs_reconciliation", "unknown", None, None, None, base_updated)

    job_record: dict[str, Any] | None = None
    job_records: tuple[dict[str, Any], ...] = ()
    reconciliation_marker = False
    public_jobs: tuple[WorkflowJobSnapshot, ...] = ()
    jobs_path = run_dir / "codex_jobs.sqlite"
    if jobs_path.exists() or jobs_path.is_symlink():
        try:
            if max_live_codex_jobs in {2, 3}:
                job_records, reconciliation_marker = _read_codex_jobs(
                    jobs_path,
                    row.workflow_id,
                    expected_task_identities,
                    max_jobs=max_live_codex_jobs,
                )
                public_jobs = tuple(
                    WorkflowJobSnapshot(
                        job_id=str(job["job_id"]),
                        task_identity=str(job["task_identity"]),
                        state=str(job["state"]),
                        thread_id=_optional_string(job.get("thread_id")),
                        turn_id=_optional_string(job.get("turn_id")),
                    )
                    for job in job_records
                )
            else:
                job_record = _read_codex_job(jobs_path, row.workflow_id)
        except (OSError, sqlite3.Error, ValueError, WorkflowServiceUnavailable):
            return _RunInspection(
                "needs_reconciliation",
                worker_state,
                None,
                None,
                None,
                base_updated,
                max_live_codex_jobs=max_live_codex_jobs,
            )
    job_state = None if job_record is None else job_record.get("state")
    job_id = None if job_record is None else _optional_string(job_record.get("job_id"))
    thread_id = None if job_record is None else _optional_string(job_record.get("thread_id"))
    if max_live_codex_jobs in {2, 3} and public_jobs:
        job_id = public_jobs[-1].job_id
        thread_id = public_jobs[-1].thread_id
        job_state = public_jobs[-1].state
    if max_live_codex_jobs == 1 and job_state == "MAYBE_SENT":
        return _RunInspection(
            "needs_reconciliation", worker_state, job_id, thread_id, None, base_updated
        )

    dbos_status: str | None = None
    dbos_invalid = False
    dbos_path = run_dir / "dbos.sqlite"
    if dbos_path.exists() or dbos_path.is_symlink():
        try:
            dbos_status = _read_dbos_status(dbos_path, row.workflow_id)
        except (OSError, sqlite3.Error, ValueError, WorkflowServiceUnavailable):
            if max_live_codex_jobs == 1:
                return _RunInspection(
                    "needs_reconciliation", worker_state, job_id, thread_id, None, base_updated
                )
            dbos_invalid = True

    result_value: dict[str, Any] | None = None
    result_invalid = False
    result_path = run_dir / "result.json"
    if result_path.exists() or result_path.is_symlink():
        try:
            raw_result = _read_regular_file(result_path, limit=_MAX_RESULT_BYTES)
            parsed_result = _strict_json(raw_result)
            if (
                not isinstance(parsed_result, dict)
                or parsed_result.get("workflow_id") != row.workflow_id
                or parsed_result.get("definition_sha256") != manifest_value.get("definition_sha256")
                or parsed_result.get("input_sha256") != manifest_value.get("input_sha256")
                or "result" not in parsed_result
            ):
                raise ValueError("workflow result identity is invalid")
            result_value = parsed_result
            base_updated = _latest_file_time(base_updated, result_path)
        except (OSError, ValueError, TypeError):
            if max_live_codex_jobs == 1:
                return _RunInspection(
                    "unknown", worker_state, job_id, thread_id, None, base_updated
                )
            result_invalid = True

    if max_live_codex_jobs == 2:
        return _project_two_job_run(
            row,
            worker_state=worker_state,
            dbos_status=dbos_status,
            result_value=result_value,
            updated_at=base_updated,
            jobs=public_jobs,
            records=job_records,
            reconciliation_marker=reconciliation_marker,
            dbos_invalid=dbos_invalid,
            result_invalid=result_invalid,
        )

    if max_live_codex_jobs == 3:
        return _project_three_job_run(
            row,
            worker_state=worker_state,
            dbos_status=dbos_status,
            result_value=result_value,
            updated_at=base_updated,
            jobs=public_jobs,
            records=job_records,
            expected_job_count=len(expected_task_identities),
            input_kind=three_job_input_kind,
            input_data=input_data,
            reconciliation_marker=reconciliation_marker,
            dbos_invalid=dbos_invalid,
            result_invalid=result_invalid,
        )

    if job_state == "FAILED" or dbos_status in {"ERROR", "CANCELLED", "FAILED"}:
        return _RunInspection("failed", worker_state, job_id, thread_id, None, base_updated)
    if worker_state == "failed":
        status = "failed" if job_state is not None else "failed_to_start"
        return _RunInspection(status, worker_state, job_id, thread_id, None, base_updated)
    if result_value is not None:
        if worker_state == "completed" and dbos_status in {"SUCCESS", "SUCCEEDED"}:
            return _RunInspection(
                "succeeded", worker_state, job_id, thread_id, result_value, base_updated
            )
        if worker_state == "running":
            return _RunInspection("running", worker_state, job_id, thread_id, None, base_updated)
        return _RunInspection("unknown", worker_state, job_id, thread_id, None, base_updated)
    if worker_state == "running" and dbos_status in {"SUCCESS", "SUCCEEDED"}:
        return _RunInspection("running", worker_state, job_id, thread_id, None, base_updated)
    if worker_state == "completed" or dbos_status in {"SUCCESS", "SUCCEEDED"}:
        return _RunInspection("unknown", worker_state, job_id, thread_id, None, base_updated)
    if (
        worker_state == "running"
        or dbos_status
        in {
            "PENDING",
            "ENQUEUED",
            "RETRIES_EXCEEDED",
            "RUNNING",
        }
        or job_state in {"PREPARED", "ACCEPTED", "RUNNING"}
    ):
        return _RunInspection("running", worker_state, job_id, thread_id, None, base_updated)
    if row.state == "queued":
        return _RunInspection("queued", worker_state, job_id, thread_id, None, base_updated)
    if row.state == "failed_to_start":
        return _RunInspection(
            "failed_to_start", worker_state, job_id, thread_id, None, base_updated
        )
    return _RunInspection(
        "needs_reconciliation", worker_state, job_id, thread_id, None, base_updated
    )


def _two_job_task_identities(definition: Any) -> tuple[str, str]:
    tasks = definition.get("do") if isinstance(definition, dict) else None
    if not isinstance(tasks, list) or len(tasks) != 2:
        raise ValueError("two-job workflow task list is invalid")
    names: list[str] = []
    for task in tasks:
        if not isinstance(task, dict) or len(task) != 1:
            raise ValueError("two-job workflow task is invalid")
        name = next(iter(task))
        if not isinstance(name, str) or not name:
            raise ValueError("two-job workflow task name is invalid")
        names.append(name)
    return (f"do[0].{names[0]}", f"do[1].{names[1]}")


def _three_job_task_identities(definition: Any, input_data: Any) -> tuple[str, ...]:
    from .interpreter import (
        three_live_codex_job_input_kind,
        validate_three_live_codex_job_input,
    )

    tasks = definition.get("do") if isinstance(definition, dict) else None
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        raise ValueError("three-job workflow task list is invalid")
    for_name = next(iter(tasks[0]))
    for_task = tasks[0].get(for_name)
    body = for_task.get("do") if isinstance(for_task, dict) else None
    if not isinstance(body, list) or len(body) != 3 or not isinstance(body[1], dict):
        raise ValueError("three-job workflow loop body is invalid")
    dispatch_name = next(iter(body[1]))
    if not isinstance(for_name, str) or not isinstance(dispatch_name, str):
        raise ValueError("three-job workflow task names are invalid")
    input_kind = three_live_codex_job_input_kind(definition)
    items = validate_three_live_codex_job_input(input_data, input_kind=input_kind)
    return tuple(
        f"do[0].{for_name}.iteration[{index}].do[1].{dispatch_name}"
        for index, item in enumerate(items)
        if item["enabled"]
    )


def _read_codex_jobs(
    database: Path,
    workflow_id: str,
    expected_task_identities: tuple[str, ...],
    *,
    max_jobs: int = 2,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    if type(max_jobs) is not int or max_jobs not in {2, 3}:
        raise ValueError("multi-job ledger limit is invalid")
    mode_name = "three-job" if max_jobs == 3 else "two-job"
    if (
        database.is_symlink()
        or not database.is_file()
        or database.stat().st_size > _MAX_RUN_FILE_BYTES * 8
    ):
        raise ValueError("codex job ledger is invalid")
    connection = _connect_read(database)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not {"jobs", "job_reconciliation"} <= tables:
            raise ValueError(f"{mode_name} ledger schema is incomplete")
        unique_indexes: set[tuple[str, ...]] = set()
        for index in connection.execute("PRAGMA index_list(jobs)"):
            if not bool(index[2]):
                continue
            index_name = str(index[1]).replace('"', '""')
            columns = tuple(
                str(column[2])
                for column in connection.execute(f'PRAGMA index_info("{index_name}")')
                if column[2] is not None
            )
            unique_indexes.add(columns)
        if ("workflow_id", "task_identity") not in unique_indexes:
            raise ValueError("two-job ledger must use the V2 workflow/task uniqueness")
        rows = connection.execute(
            "SELECT job_id, workflow_id, task_identity, state, thread_id, turn_id, "
            "remote_state, receipt_json FROM jobs "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (max_jobs + 1,),
        ).fetchall()
        markers = connection.execute(
            "SELECT job_id, marker FROM job_reconciliation LIMIT ?",
            (max_jobs + 1,),
        ).fetchall()
    finally:
        connection.close()

    if len(rows) > max_jobs or len(rows) > len(expected_task_identities) or len(markers) > max_jobs:
        raise ValueError(f"{mode_name} ledger contains too many rows")
    records: list[dict[str, Any]] = []
    job_ids: set[str] = set()
    for index, row in enumerate(rows):
        job_id = row["job_id"]
        task_identity = row["task_identity"]
        state = row["state"]
        if (
            row["workflow_id"] != workflow_id
            or not isinstance(job_id, str)
            or _CODEX_JOB_ID.fullmatch(job_id) is None
            or job_id in job_ids
            or task_identity != expected_task_identities[index]
            or state not in {"PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"}
        ):
            raise ValueError(f"{mode_name} ledger identity or order is invalid")
        job_ids.add(job_id)
        thread_id = row["thread_id"]
        turn_id = row["turn_id"]
        if state in {"PREPARED", "MAYBE_SENT"}:
            if thread_id is not None or turn_id is not None:
                raise ValueError(f"unaccepted {mode_name} row has a remote identity")
        elif (
            not isinstance(thread_id, str)
            or _CODEX_JOB_ID.fullmatch(thread_id) is None
            or not isinstance(turn_id, str)
            or _CODEX_JOB_ID.fullmatch(turn_id) is None
        ):
            raise ValueError(f"accepted {mode_name} row has an invalid remote identity")

        remote_state = row["remote_state"]
        receipt_json = row["receipt_json"]
        receipt: dict[str, Any] | None = None
        if state in {"PREPARED", "MAYBE_SENT"}:
            if remote_state is not None or receipt_json is not None:
                raise ValueError(f"unaccepted {mode_name} row has a remote receipt")
        elif state in {"ACCEPTED", "RUNNING"}:
            if (
                remote_state
                not in {
                    "in_progress",
                    "needs_approval",
                    "needs_input",
                    "completed",
                    "interrupted",
                    "failed",
                    "not_loaded",
                    "unknown",
                }
                or receipt_json is not None
            ):
                raise ValueError(f"active {mode_name} row has an invalid receipt")
        elif state == "SUCCEEDED":
            if (
                not isinstance(receipt_json, str)
                or len(receipt_json.encode("utf-8")) > _MAX_RESULT_BYTES
            ):
                raise ValueError(f"succeeded {mode_name} row has no bounded receipt")
            receipt = _strict_json(receipt_json.encode("utf-8"))
            result = receipt.get("result") if isinstance(receipt, dict) else None
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"result"}
                or not isinstance(result, dict)
                or set(result) != {"final_text"}
                or not isinstance(result.get("final_text"), str)
                or remote_state != "completed"
            ):
                raise ValueError(f"succeeded {mode_name} row receipt is invalid")
        else:
            if not isinstance(receipt_json, str) or len(receipt_json.encode("utf-8")) > 4096:
                raise ValueError(f"failed {mode_name} row has no bounded receipt")
            receipt = _strict_json(receipt_json.encode("utf-8"))
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"remote_state"}
                or receipt.get("remote_state") != remote_state
                or remote_state not in {"failed", "interrupted"}
            ):
                raise ValueError(f"failed {mode_name} row receipt is invalid")
        records.append(
            {
                "job_id": job_id,
                "task_identity": task_identity,
                "state": state,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "remote_state": remote_state,
                "receipt": receipt,
            }
        )

    reconciliation_marker = False
    for marker_row in markers:
        marker_job_id = marker_row["job_id"]
        if marker_job_id not in job_ids or marker_row["marker"] != "NEEDS_RECONCILIATION":
            raise ValueError(f"{mode_name} reconciliation marker is invalid")
        reconciliation_marker = True
    return tuple(records), reconciliation_marker


def _project_two_job_run(
    row: _RequestRow,
    *,
    worker_state: str,
    dbos_status: str | None,
    result_value: dict[str, Any] | None,
    updated_at: str,
    jobs: tuple[WorkflowJobSnapshot, ...],
    records: tuple[dict[str, Any], ...],
    reconciliation_marker: bool,
    dbos_invalid: bool,
    result_invalid: bool,
) -> _RunInspection:
    job_id = jobs[-1].job_id if jobs else None
    thread_id = jobs[-1].thread_id if jobs else None

    def inspection(status: str, result: dict[str, Any] | None = None) -> _RunInspection:
        return _RunInspection(
            status,
            worker_state,
            job_id,
            thread_id,
            result,
            updated_at,
            max_live_codex_jobs=2,
            jobs=jobs,
        )

    if reconciliation_marker or any(job["state"] == "MAYBE_SENT" for job in records):
        return inspection("needs_reconciliation")
    if any(job["state"] == "FAILED" for job in records) or dbos_status in {
        "ERROR",
        "CANCELLED",
        "FAILED",
    }:
        return inspection("failed")
    if worker_state == "failed":
        return inspection("failed")
    if len(records) == 2 and records[0]["state"] != "SUCCEEDED":
        return inspection("needs_reconciliation")
    if dbos_invalid:
        return inspection("needs_reconciliation")
    if result_invalid:
        return inspection("unknown")

    if len(records) == 2 and all(job["state"] == "SUCCEEDED" for job in records):
        if result_value is not None and not _two_job_result_matches(row, records[1], result_value):
            return inspection("unknown")
        if (
            worker_state == "completed"
            and dbos_status in {"SUCCESS", "SUCCEEDED"}
            and result_value is not None
        ):
            return inspection("succeeded", result_value)
        if worker_state == "running":
            return inspection("running")
        return inspection("unknown")

    if result_value is not None:
        return inspection("unknown")
    if worker_state == "completed" or dbos_status in {"SUCCESS", "SUCCEEDED"}:
        return inspection("unknown")
    if (
        worker_state == "running"
        or dbos_status
        in {
            "PENDING",
            "ENQUEUED",
            "RETRIES_EXCEEDED",
            "RUNNING",
        }
        or records
    ):
        return inspection("running")
    if row.state == "queued":
        return inspection("queued")
    if row.state == "failed_to_start":
        return inspection("failed_to_start")
    return inspection("needs_reconciliation")


def _project_three_job_run(
    row: _RequestRow,
    *,
    worker_state: str,
    dbos_status: str | None,
    result_value: dict[str, Any] | None,
    updated_at: str,
    jobs: tuple[WorkflowJobSnapshot, ...],
    records: tuple[dict[str, Any], ...],
    expected_job_count: int,
    input_kind: str,
    input_data: Any,
    reconciliation_marker: bool,
    dbos_invalid: bool,
    result_invalid: bool,
) -> _RunInspection:
    job_id = jobs[-1].job_id if jobs else None
    thread_id = jobs[-1].thread_id if jobs else None

    def inspection(status: str, result: dict[str, Any] | None = None) -> _RunInspection:
        return _RunInspection(
            status,
            worker_state,
            job_id,
            thread_id,
            result,
            updated_at,
            max_live_codex_jobs=3,
            jobs=jobs,
        )

    if reconciliation_marker:
        return inspection("needs_reconciliation")
    maybe_sent_indexes = [
        index for index, job in enumerate(records) if job["state"] == "MAYBE_SENT"
    ]
    if maybe_sent_indexes:
        transient_last_maybe_sent = (
            len(maybe_sent_indexes) == 1
            and maybe_sent_indexes[0] == len(records) - 1
            and 1 <= len(records) <= expected_job_count
            and all(job["state"] == "SUCCEEDED" for job in records[:-1])
            and worker_state == "running"
            and result_value is None
            and not dbos_invalid
            and not result_invalid
            and dbos_status not in {"SUCCESS", "SUCCEEDED", "ERROR", "CANCELLED", "FAILED"}
        )
        return inspection("running" if transient_last_maybe_sent else "needs_reconciliation")
    if any(job["state"] == "FAILED" for job in records) or dbos_status in {
        "ERROR",
        "CANCELLED",
        "FAILED",
    }:
        return inspection("failed")
    if worker_state == "failed":
        return inspection("failed")
    if any(job["state"] != "SUCCEEDED" for job in records[:-1]):
        return inspection("needs_reconciliation")
    if dbos_invalid:
        return inspection("needs_reconciliation")
    if result_invalid:
        return inspection("unknown")

    all_expected_jobs_succeeded = len(records) == expected_job_count and all(
        job["state"] == "SUCCEEDED" for job in records
    )
    if all_expected_jobs_succeeded:
        if result_value is not None and not _three_job_result_matches(
            row, records, input_data, result_value, input_kind=input_kind
        ):
            return inspection("unknown")
        if (
            worker_state == "completed"
            and dbos_status in {"SUCCESS", "SUCCEEDED"}
            and result_value is not None
        ):
            return inspection("succeeded", result_value)
        if worker_state == "running":
            return inspection("running")
        return inspection("unknown")

    if result_value is not None:
        return inspection("unknown")
    if worker_state == "completed" or dbos_status in {"SUCCESS", "SUCCEEDED"}:
        return inspection("unknown")
    if (
        worker_state == "running"
        or dbos_status
        in {
            "PENDING",
            "ENQUEUED",
            "RETRIES_EXCEEDED",
            "RUNNING",
        }
        or records
    ):
        return inspection("running")
    if row.state == "queued":
        return inspection("queued")
    if row.state == "failed_to_start":
        return inspection("failed_to_start")
    return inspection("needs_reconciliation")


def _three_job_result_matches(
    row: _RequestRow,
    records: tuple[dict[str, Any], ...],
    input_data: Any,
    result_value: dict[str, Any],
    *,
    input_kind: str = "issues",
) -> bool:
    from .interpreter import OwsError, validate_three_live_codex_job_input

    try:
        items = validate_three_live_codex_job_input(input_data, input_kind=input_kind)
    except OwsError:
        return False
    if input_kind == "items":
        if (
            set(result_value)
            != {
                "workflow_id",
                "definition_sha256",
                "input_sha256",
                "result",
            }
            or result_value.get("workflow_id") != row.workflow_id
        ):
            return False
        expected_rows: list[dict[str, Any]] = []
        record_index = 0
        for item in items:
            if not item["enabled"]:
                expected_rows.append(
                    {
                        "id": item["id"],
                        "status": "skipped",
                        "job_id": None,
                        "request_key": None,
                        "final_text": None,
                    }
                )
                continue
            if record_index >= len(records):
                return False
            record = records[record_index]
            receipt = record.get("receipt")
            job_result = receipt.get("result") if isinstance(receipt, dict) else None
            final_text = job_result.get("final_text") if isinstance(job_result, dict) else None
            if record.get("state") != "SUCCEEDED" or not isinstance(final_text, str):
                return False
            expected_rows.append(
                {
                    "id": item["id"],
                    "status": "SUCCEEDED",
                    "job_id": record["job_id"],
                    "request_key": f"{row.workflow_id}::{record['task_identity']}",
                    "final_text": final_text,
                }
            )
            record_index += 1
        if record_index != len(records):
            return False
        expected_result = {
            "results": expected_rows,
            "summary": {
                "total": len(items),
                "succeeded": record_index,
                "skipped": len(items) - record_index,
            },
        }
        try:
            return _canonical_json(result_value.get("result")) == _canonical_json(expected_result)
        except (TypeError, ValueError):
            return False

    expected: list[dict[str, Any]] = []
    record_index = 0
    for issue in items:
        if not issue["enabled"]:
            expected.append(
                {
                    "number": issue["number"],
                    "status": "skipped",
                    "job_id": None,
                    "request_key": None,
                    "final_text": None,
                }
            )
            continue
        if record_index >= len(records):
            return False
        record = records[record_index]
        receipt = record.get("receipt")
        job_result = receipt.get("result") if isinstance(receipt, dict) else None
        final_text = job_result.get("final_text") if isinstance(job_result, dict) else None
        if record.get("state") != "SUCCEEDED" or not isinstance(final_text, str):
            return False
        expected.append(
            {
                "number": issue["number"],
                "status": "SUCCEEDED",
                "job_id": record["job_id"],
                "request_key": f"{row.workflow_id}::{record['task_identity']}",
                "final_text": final_text,
            }
        )
        record_index += 1
    return record_index == len(records) and result_value.get("result") == expected


def _two_job_result_matches(
    row: _RequestRow,
    second_job: dict[str, Any],
    result_value: dict[str, Any],
) -> bool:
    receipt = second_job.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("result"), dict):
        return False
    expected = {
        "job_id": second_job["job_id"],
        "request_key": f"{row.workflow_id}::{second_job['task_identity']}",
        "thread_id": second_job["thread_id"],
        "turn_id": second_job["turn_id"],
        "status": "SUCCEEDED",
        "result": receipt["result"],
    }
    return result_value.get("result") == expected


def _read_codex_job(database: Path, workflow_id: str) -> dict[str, Any] | None:
    if (
        database.is_symlink()
        or not database.is_file()
        or database.stat().st_size > _MAX_RUN_FILE_BYTES * 8
    ):
        raise ValueError("codex job ledger is invalid")
    connection = _connect_read(database)
    try:
        row = connection.execute(
            "SELECT job_id, workflow_id, state, thread_id, turn_id FROM jobs LIMIT 2"
        ).fetchall()
        if len(row) > 1:
            raise ValueError("codex job ledger contains multiple workflow rows")
        if not row:
            return None
        values = dict(row[0])
        if values.get("workflow_id") != workflow_id:
            raise ValueError("codex job ledger belongs to a different workflow")
        if values.get("state") not in {
            "PREPARED",
            "MAYBE_SENT",
            "ACCEPTED",
            "RUNNING",
            "SUCCEEDED",
            "FAILED",
        }:
            raise ValueError("codex job state is invalid")
        return values
    finally:
        connection.close()


def _read_dbos_status(database: Path, workflow_id: str) -> str | None:
    if (
        database.is_symlink()
        or not database.is_file()
        or database.stat().st_size > _MAX_RUN_FILE_BYTES * 8
    ):
        raise ValueError("DBOS database is invalid")
    connection = _connect_read(database)
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workflow_status'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT workflow_uuid, status FROM workflow_status LIMIT 2"
        ).fetchall()
        if len(row) > 1:
            raise ValueError("DBOS database contains multiple workflow rows")
        if not row:
            return None
        if row[0]["workflow_uuid"] != workflow_id:
            raise ValueError("DBOS database belongs to a different workflow")
        status = row[0]["status"]
        if not isinstance(status, str):
            raise ValueError("DBOS workflow status is invalid")
        return status.upper()
    finally:
        connection.close()


def _latest_file_time(previous: str, path: Path) -> str:
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        return max(previous, modified)
    except OSError:
        return previous


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _snapshot(state_root: Path, row: _RequestRow) -> WorkflowSnapshot:
    return _snapshot_from_inspection(row, _inspect_run(state_root, row))


def _snapshot_from_inspection(row: _RequestRow, inspection: _RunInspection) -> WorkflowSnapshot:
    status = inspection.status if inspection.status in _WORKFLOW_STATES else "unknown"
    return WorkflowSnapshot(
        workflow_id=row.workflow_id,
        run_id=row.run_id,
        status=status,
        created_at=row.created_at,
        updated_at=inspection.updated_at,
        worker_state=inspection.worker_state,
        terminal=status in _TERMINAL_STATES,
        job_id=inspection.job_id,
        thread_id=inspection.thread_id,
        max_live_codex_jobs=inspection.max_live_codex_jobs,
        jobs=inspection.jobs,
    )
