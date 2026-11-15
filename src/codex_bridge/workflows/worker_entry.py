from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from ..paths import AllowedPathPolicy, PathPolicyError
from .http_client import CONTROL_TOKEN_ENV
from .manager import missing_workflow_dependencies

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
        manifest = _read_manifest(run_dir)
        control = manifest.get("control", {})
        if not isinstance(control, dict):
            raise WorkflowWorkerEntryError("workflow manifest control must be an object")
        live = control.get("codex_job_live", False)
        if not isinstance(live, bool):
            raise WorkflowWorkerEntryError("workflow manifest codex_job_live must be a boolean")
        max_live_codex_jobs = _max_live_codex_jobs(control)
        if max_live_codex_jobs in {2, 3} and not live:
            raise WorkflowWorkerEntryError(
                f"manifest max_live_codex_jobs={max_live_codex_jobs} requires codex_job_live"
            )
        worker_args = _validate_worker_configuration(run_dir, args.mode, config, live)
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


def _read_manifest(run_dir: Path) -> dict[str, object]:
    path = run_dir / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise WorkflowWorkerEntryError("workflow run manifest is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise WorkflowWorkerEntryError("workflow run manifest is invalid") from None
    if not isinstance(value, dict):
        raise WorkflowWorkerEntryError("workflow run manifest must be an object")
    return value


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
    expected_keys = {"version", "mode", "workspace_root", "allowed_roots", "shutdown_file"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise WorkflowWorkerEntryError("workflow worker config has an invalid shape")
    if type(value.get("version")) is not int or value["version"] != 1:
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
