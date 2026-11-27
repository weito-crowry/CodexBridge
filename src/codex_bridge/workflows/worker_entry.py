from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from ..paths import AllowedPathPolicy, PathPolicyError
from .agent_runtime import (
    build_codex_agent_runtime,
    open_agent_ledger_for_manifest,
    validate_pinned_agent_definition,
)
from .http_client import CONTROL_TOKEN_ENV
from .manager import missing_workflow_dependencies
from .strict_manifest import StrictWorkflowSnapshotVerification, _is_symlink_or_junction
from .strict_reader_gate import (
    RunFormatVersion,
    StrictReaderGateError,
    _decode_manifest,
    classify_readonly,
    verify_strict_reserved_readonly,
)
from .task_tree import (
    StrictTaskTreeKind,
    TaskTreeClassificationError,
    classify_task_tree,
    definition_has_call_target,
)

_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{32}$")
_SHUTDOWN_FILE_RE = re.compile(r"^worker-shutdown-[0-9a-f]{32}\.request$")


class WorkflowWorkerEntryError(ValueError):
    """A worker invocation or its non-secret run configuration is invalid."""


def _max_live_codex_jobs(control: dict[str, object]) -> int:
    value = control.get("max_live_codex_jobs", 1)
    if type(value) is not int or value not in {1, 2, 3}:
        raise WorkflowWorkerEntryError(
            "manifest max_live_codex_jobs must be the integer 1, 2, or 3"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexbridge-workflow-worker")
    parser.add_argument("--mode", choices=("start", "recover"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_dir = _validate_run_directory(args.run_dir)
        config = _read_config(args.config, run_dir, args.mode)
        raw_roots = config.get("allowed_roots")
        allowed_roots = (
            tuple(root for root in raw_roots if type(root) is str)
            if isinstance(raw_roots, list) and all(type(root) is str for root in raw_roots)
            else ()
        )
        manifest = _read_manifest(run_dir, allowed_roots=allowed_roots)
        strict_execution_mode: str | None = None
        codex_bridge_port: int | None = None
        if manifest.get("run_format_version") == 2:
            if config.get("version") != 2 or config.get("strict_runtime") is not True:
                raise WorkflowWorkerEntryError("strict workflow runtime is not supported")
            verification = _verify_strict_reserved(run_dir, allowed_roots)
            definition_revision = verification._definition
            if definition_revision is None:
                raise WorkflowWorkerEntryError("workflow request binding is invalid")
            try:
                classification = classify_task_tree(
                    definition_revision.definition, strict_contract=True
                )
            except TaskTreeClassificationError:
                raise WorkflowWorkerEntryError("strict workflow definition is invalid") from None
            strict_execution_mode = {
                StrictTaskTreeKind.STRICT_AGENT: "authorized_agent",
                StrictTaskTreeKind.STRICT_CODEX_JOB: "authorized_codex",
                StrictTaskTreeKind.STRICT_PURE: "isolated",
            }.get(classification.kind)
            if strict_execution_mode is None:
                raise WorkflowWorkerEntryError("strict execution mode is invalid")
            if config.get("execution_mode") != strict_execution_mode:
                raise WorkflowWorkerEntryError("strict execution mode is invalid")
            raw_workspace = config.get("workspace_root")
            if not isinstance(raw_workspace, str) or os.path.normcase(
                os.path.abspath(raw_workspace)
            ) != os.path.normcase(os.path.abspath(verification.manifest.workspace_root)):
                raise WorkflowWorkerEntryError("workflow workspace does not match its manifest")
            if strict_execution_mode in {"authorized_codex", "authorized_agent"}:
                raw_codex_bridge_port = config.get("codex_bridge_port")
                if (
                    type(raw_codex_bridge_port) is not int
                    or not 1 <= raw_codex_bridge_port <= 65535
                ):
                    raise WorkflowWorkerEntryError("strict codex-job configuration is invalid")
                codex_bridge_port = raw_codex_bridge_port
            if strict_execution_mode == "authorized_agent":
                if (
                    config.get("strict_agent_enabled") is not True
                    or os.environ.get("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED") != "1"
                ):
                    raise WorkflowWorkerEntryError("strict Agent runtime is not enabled")
                raw_workspace = config.get("workspace_root")
                if type(raw_workspace) is not str or codex_bridge_port is None:
                    raise WorkflowWorkerEntryError("strict Agent configuration is invalid")
                try:
                    _provider, registry, _context = build_codex_agent_runtime(
                        port=codex_bridge_port,
                        allowed_roots=allowed_roots,
                        workspace_root=verification.manifest.workspace_root,
                    )
                    validate_pinned_agent_definition(definition_revision.definition, registry)
                    open_agent_ledger_for_manifest(
                        run_dir,
                        verification.manifest,
                        expected_port=codex_bridge_port,
                    )
                except Exception:
                    raise WorkflowWorkerEntryError(
                        "strict Agent ledger or target binding is invalid"
                    ) from None
            elif config.get("strict_agent_enabled") is not None:
                raise WorkflowWorkerEntryError("strict worker config has unexpected Agent settings")
            if strict_execution_mode == "isolated" and config.get("codex_bridge_port") is not None:
                raise WorkflowWorkerEntryError("strict worker config has unexpected job settings")
        elif config.get("version") != 1:
            raise WorkflowWorkerEntryError("legacy workflow config version is unsupported")
        control = manifest.get("control", {})
        if strict_execution_mode is None and not isinstance(control, dict):
            raise WorkflowWorkerEntryError("workflow manifest control must be an object")
        if strict_execution_mode is None:
            assert isinstance(control, dict)
            live = control.get("codex_job_live", False)
            if not isinstance(live, bool):
                raise WorkflowWorkerEntryError("workflow manifest codex_job_live must be a boolean")
            max_live_codex_jobs = _max_live_codex_jobs(control)
            if max_live_codex_jobs in {2, 3} and not live:
                raise WorkflowWorkerEntryError(
                    f"manifest max_live_codex_jobs={max_live_codex_jobs} requires codex_job_live"
                )
        else:
            live = strict_execution_mode in {"authorized_codex", "authorized_agent"}
            raw_max_live_codex_jobs = manifest.get("max_live_codex_jobs")
            if type(raw_max_live_codex_jobs) is not int or raw_max_live_codex_jobs not in {
                1,
                2,
                3,
            }:
                raise WorkflowWorkerEntryError("strict codex-job limit is invalid")
            max_live_codex_jobs = raw_max_live_codex_jobs
        worker_args = _validate_worker_configuration(run_dir, args.mode, config, live)
        if strict_execution_mode is not None:
            worker_args.extend(("--strict-runtime-mode", strict_execution_mode))
            if codex_bridge_port is not None:
                worker_args.extend(("--codex-bridge-port", str(codex_bridge_port)))
    except WorkflowWorkerEntryError as error:
        _write_status(args.run_dir, state="failed", exit_code=2, error_code="invalid_config")
        print(str(error), file=sys.stderr, flush=True)
        return 2

    missing = missing_workflow_dependencies()
    if missing:
        message = (
            "Workflow worker requires the optional dependencies; install with: "
            "uv sync --extra workflows"
        )
        _write_status(run_dir, state="failed", exit_code=2, error_code="missing_workflows_extra")
        print(message, file=sys.stderr, flush=True)
        return 2
    if live and not _control_token_configured():
        message = "Live codex-job requires the Bridge control token in the worker environment"
        _write_status(run_dir, state="failed", exit_code=2, error_code="missing_control_token")
        print(message, file=sys.stderr, flush=True)
        return 2

    _write_status(run_dir, state="running", exit_code=None, error_code=None)
    try:
        from .worker import main as worker_main

        exit_code = int(worker_main(worker_args))
    except Exception as error:
        exit_code = 1
        _write_status(
            run_dir,
            state="failed",
            exit_code=exit_code,
            error_code=type(error).__name__,
        )
        print(
            f"Workflow worker failed ({type(error).__name__})",
            file=sys.stderr,
            flush=True,
        )
        return exit_code
    _write_status(
        run_dir,
        state="completed" if exit_code == 0 else "failed",
        exit_code=exit_code,
        error_code=None if exit_code == 0 else "worker_exit_nonzero",
    )
    return exit_code


def _validate_run_directory(value: Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise WorkflowWorkerEntryError("workflow run directory may not be a symlink")
    try:
        run_dir = candidate.resolve(strict=True)
    except OSError:
        raise WorkflowWorkerEntryError("workflow run directory is unavailable") from None
    if not run_dir.is_dir() or not _RUN_ID_RE.fullmatch(run_dir.name):
        raise WorkflowWorkerEntryError("workflow run directory has an invalid identity")
    return run_dir


def _read_manifest(
    run_dir: Path,
    *,
    allowed_roots: tuple[str, ...] = (),
) -> dict[str, object]:
    path = run_dir / "manifest.json"
    if _is_symlink_or_junction(path):
        raise WorkflowWorkerEntryError("workflow run manifest is unavailable")
    try:
        resolved = path.resolve(strict=True)
        if resolved.parent != run_dir or not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("manifest is not a regular file")
        manifest_bytes = resolved.read_bytes()
        value, _ = _decode_manifest(manifest_bytes)
    except Exception:
        raise WorkflowWorkerEntryError("workflow run manifest is invalid") from None
    if "run_format_version" in value:
        if type(value.get("run_format_version")) is not int or value["run_format_version"] != 2:
            raise WorkflowWorkerEntryError("workflow run format is unsupported")
        workflow_id = value.get("workflow_id")
        request_digest = value.get("request_digest")
        if type(workflow_id) is not str or type(request_digest) is not str:
            raise WorkflowWorkerEntryError("workflow request binding is invalid")
        try:
            verification = verify_strict_reserved_readonly(
                run_dir.parent,
                run_dir.name,
                manifest_bytes,
                expected_workflow_id=workflow_id,
                expected_request_digest=request_digest,
                allowed_roots=allowed_roots,
            )
        except StrictReaderGateError:
            raise WorkflowWorkerEntryError("workflow request binding is invalid") from None
        if verification is None:
            raise WorkflowWorkerEntryError("workflow request binding is invalid")
        return verification.manifest.to_dict()
    _validate_managed_legacy_manifest(run_dir, value, manifest_bytes)
    return value


def _validate_managed_legacy_manifest(
    run_dir: Path,
    manifest: dict[str, object],
    manifest_bytes: bytes,
) -> None:
    request_database = run_dir.parent / "requests.sqlite"
    if _is_symlink_or_junction(request_database):
        raise WorkflowWorkerEntryError("workflow request binding is invalid")
    try:
        metadata = request_database.stat()
    except FileNotFoundError:
        return
    except OSError:
        raise WorkflowWorkerEntryError("workflow request binding is invalid") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise WorkflowWorkerEntryError("workflow request binding is invalid")

    workflow_id = manifest.get("workflow_id")
    manifest_run_id = manifest.get("run_id")
    request_digest = manifest.get("request_digest")
    if (
        type(workflow_id) is not str
        or not workflow_id
        or type(manifest_run_id) is not str
        or manifest_run_id != run_dir.name
        or type(request_digest) is not str
    ):
        raise WorkflowWorkerEntryError("workflow request binding is invalid")
    try:
        run_format = classify_readonly(
            run_dir.parent,
            run_dir.name,
            manifest_bytes,
            expected_workflow_id=workflow_id,
            expected_request_digest=request_digest,
        )
    except StrictReaderGateError:
        raise WorkflowWorkerEntryError("workflow request binding is invalid") from None
    if run_format is not RunFormatVersion.LEGACY_V1:
        raise WorkflowWorkerEntryError("workflow request binding is invalid")


def _read_config(config_path: Path, run_dir: Path, mode: str) -> dict[str, object]:
    candidate = Path(config_path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise WorkflowWorkerEntryError("workflow worker config is unavailable")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(run_dir)
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise WorkflowWorkerEntryError("workflow worker config is invalid") from None
    legacy_keys = {"version", "mode", "workspace_root", "allowed_roots", "shutdown_file"}
    strict_keys = legacy_keys | {
        "strict_runtime",
        "execution_mode",
        "codex_bridge_port",
    }
    agent_strict_keys = strict_keys | {"strict_agent_enabled"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_keys),
        frozenset(strict_keys),
        frozenset(agent_strict_keys),
    }:
        raise WorkflowWorkerEntryError("workflow worker config has an invalid shape")
    if type(value.get("version")) is not int or value["version"] not in {1, 2}:
        raise WorkflowWorkerEntryError("workflow worker config version is unsupported")
    if (value["version"] == 1 and set(value) != legacy_keys) or (
        value["version"] == 2
        and (
            set(value) != strict_keys
            or value.get("strict_runtime") is not True
            or type(value.get("execution_mode")) is not str
            or value.get("execution_mode")
            not in {"isolated", "authorized_codex", "authorized_agent"}
            or (
                value.get("execution_mode") == "authorized_agent"
                and (
                    set(value) != agent_strict_keys or value.get("strict_agent_enabled") is not True
                )
            )
            or (value.get("execution_mode") != "authorized_agent" and set(value) != strict_keys)
            or (
                value.get("codex_bridge_port") is not None
                and (
                    type(value.get("codex_bridge_port")) is not int
                    or not 1 <= value["codex_bridge_port"] <= 65535
                )
            )
        )
    ):
        raise WorkflowWorkerEntryError("workflow worker config version is unsupported")
    if value.get("mode") != mode:
        raise WorkflowWorkerEntryError("workflow worker mode does not match its config")
    return value


def _validate_worker_configuration(
    run_dir: Path,
    mode: str,
    config: dict[str, object],
    live: bool,
) -> list[str]:
    raw_roots = config.get("allowed_roots")
    if not isinstance(raw_roots, list) or not all(isinstance(root, str) for root in raw_roots):
        raise WorkflowWorkerEntryError("workflow allowed_roots config is invalid")
    raw_workspace = config.get("workspace_root")
    workspace: Path | None = None
    if raw_workspace is not None:
        if not isinstance(raw_workspace, str):
            raise WorkflowWorkerEntryError("workflow workspace config is invalid")
        try:
            policy = AllowedPathPolicy(tuple(raw_roots))
            workspace = Path(policy.validate_cwd(raw_workspace))
        except (PathPolicyError, OSError):
            raise WorkflowWorkerEntryError(
                "workflow workspace is outside an allowed existing root"
            ) from None
        if not os.access(workspace, os.W_OK):
            raise WorkflowWorkerEntryError("workflow workspace is not writable")
        if (
            workspace == run_dir
            or workspace.is_relative_to(run_dir)
            or run_dir.is_relative_to(workspace)
        ):
            raise WorkflowWorkerEntryError("workflow state and job workspace must be separate")
    if live and workspace is None:
        raise WorkflowWorkerEntryError("live codex-job requires an explicit allowed workspace")
    shutdown_name = config.get("shutdown_file")
    if not isinstance(shutdown_name, str) or not _SHUTDOWN_FILE_RE.fullmatch(shutdown_name):
        raise WorkflowWorkerEntryError("workflow shutdown signal config is invalid")
    shutdown_file = run_dir / shutdown_name
    if shutdown_file.is_symlink():
        raise WorkflowWorkerEntryError("workflow shutdown signal may not be a symlink")
    arguments = ["--mode", mode, "--run-dir", str(run_dir)]
    if workspace is not None:
        arguments.extend(("--workspace-root", str(workspace)))
    for root in raw_roots:
        arguments.extend(("--allowed-root", root))
    arguments.extend(("--shutdown-file", str(shutdown_file)))
    return arguments


def _verify_strict_reserved(
    run_dir: Path, allowed_roots: tuple[str, ...]
) -> StrictWorkflowSnapshotVerification:
    try:
        manifest_path = run_dir / "manifest.json"
        if _is_symlink_or_junction(manifest_path):
            raise ValueError
        manifest_bytes = manifest_path.read_bytes()
        value, _ = _decode_manifest(manifest_bytes)
        verification = verify_strict_reserved_readonly(
            run_dir.parent,
            run_dir.name,
            manifest_bytes,
            expected_workflow_id=str(value["workflow_id"]),
            expected_request_digest=str(value["request_digest"]),
            allowed_roots=allowed_roots,
        )
        if verification is None or verification.reservation_verified is not True:
            raise ValueError
        return verification
    except Exception:
        raise WorkflowWorkerEntryError("workflow request binding is invalid") from None


def _definition_contains_codex_job(value: object) -> bool:
    return definition_has_call_target(value, "codex-job")


def _control_token_configured() -> bool:
    token = os.environ.get(CONTROL_TOKEN_ENV)
    return isinstance(token, str) and bool(token.strip())


def _write_status(
    run_dir: Path,
    *,
    state: str,
    exit_code: int | None,
    error_code: str | None,
) -> None:
    try:
        run_dir = Path(run_dir).resolve(strict=True)
        status_path = run_dir / "worker-entry-status.json"
        temporary_path = run_dir / "worker-entry-status.json.tmp"
        if status_path.is_symlink() or temporary_path.is_symlink():
            return
        payload = json.dumps(
            {"state": state, "exit_code": exit_code, "error_code": error_code},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, status_path)
        if os.name != "nt":
            os.chmod(status_path, 0o600)
    except OSError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
