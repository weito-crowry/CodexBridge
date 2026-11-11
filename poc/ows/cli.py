from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from _product_imports import prepare_product_import

prepare_product_import()

from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter  # noqa: E402
from codex_bridge.workflows.interpreter import (  # noqa: E402
    OwsError,
    parse_workflow_text,
    validate_workflow,
)
from codex_bridge.workflows.job_adapter import DurableJobAdapter  # noqa: E402
from codex_bridge.workflows.job_service import start_mock_job_service  # noqa: E402
from codex_bridge.workflows.worker import (  # noqa: E402
    _read_workflow_status,
    _write_json,
    canonical_json,
    load_run_request,
    sha256_json,
    sha256_text,
)

OWS_DIR = Path(__file__).resolve().parent
WORKER_PATH = OWS_DIR / "worker.py"
DEFAULT_STATE_ROOT = OWS_DIR / ".state" / "runs"
PHASE4B_STATE_ROOT = OWS_DIR / ".state" / "phase4b-runs"
PHASE4C_STATE_ROOT = OWS_DIR / ".state" / "phase4c-runs"
PHASE4D_STATE_ROOT = OWS_DIR / ".state" / "phase4d-runs"


def _invoke_worker(run_dir: Path, mode: str, timeout_seconds: int) -> dict[str, Any]:
    command = [
        sys.executable,
        str(WORKER_PATH),
        "--mode",
        mode,
        "--run-dir",
        str(run_dir),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    completed = subprocess.run(
        command,
        cwd=OWS_DIR.parent.parent,
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
        check=False,
    )
    _write_json(
        run_dir / f"{mode}-worker-logs.json",
        {"stdout": completed.stdout, "stderr": completed.stderr},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"DBOS worker exited with {completed.returncode}; "
            f"logs: {run_dir / f'{mode}-worker-logs.json'}"
        )
    return json.loads((run_dir / "result.json").read_text(encoding="utf-8"))


def _new_run(
    workflow_path: Path,
    input_json: str,
    max_tasks: int,
    *,
    mock_job_durable_wait: bool = False,
    codex_job_live: bool = False,
    codex_bridge_port: int = 8001,
) -> Path:
    try:
        definition_text = workflow_path.resolve().read_text(encoding="utf-8")
        input_data = json.loads(input_json)
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot load workflow or JSON input: {error}") from error
    definition = parse_workflow_text(definition_text)
    validate_workflow(definition, codex_job_live=codex_job_live)
    if codex_job_live and mock_job_durable_wait:
        raise OwsError("codex-job live mode cannot be mixed with mock-job durable-wait mode")
    if codex_job_live and (
        type(codex_bridge_port) is not int or not 1 <= codex_bridge_port <= 65535
    ):
        raise OwsError("--codex-bridge-port must be an integer from 1 to 65535")
    canonical_input = canonical_json(input_data)

    if codex_job_live:
        state_root = PHASE4D_STATE_ROOT
    elif _workflow_uses_mock_job(definition):
        state_root = PHASE4C_STATE_ROOT if mock_job_durable_wait else PHASE4B_STATE_ROOT
    else:
        state_root = DEFAULT_STATE_ROOT
    run_uuid = uuid.uuid4()
    run_dir = state_root / f"run-{run_uuid.hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(canonical_input + "\n", encoding="utf-8", newline="\n")
    manifest = {
        "workflow_source": str(workflow_path.resolve()),
        "workflow_id": str(run_uuid) if codex_job_live else None,
        "definition_sha256": sha256_text(definition_text),
        "input_sha256": sha256_json(input_data),
        "max_tasks": max_tasks,
        "control": ({"mock_job_durable_wait": True} if mock_job_durable_wait else {}),
    }
    if codex_job_live:
        manifest["mode"] = "codex-job-live"
        manifest["control"] = {
            "codex_job_live": True,
            "codex_bridge_port": codex_bridge_port,
            "codex_job_max_polls": 600,
            "codex_job_poll_interval_seconds": 0.2,
        }
    _write_json(run_dir / "manifest.json", manifest)
    return run_dir


def _workflow_uses_call(definition: dict[str, Any], target: str) -> bool:
    def has_call(tasks: list[dict[str, Any]]) -> bool:
        for task in tasks:
            task_spec = next(iter(task.values()))
            if task_spec.get("call") == target:
                return True
            nested = task_spec.get("do")
            if isinstance(nested, list) and has_call(nested):
                return True
        return False

    return has_call(definition["do"])


def _workflow_uses_mock_job(definition: dict[str, Any]) -> bool:
    return _workflow_uses_call(definition, "mock-job")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_tasks < 1:
        raise OwsError("--max-tasks must be positive")
    codex_job_live = bool(getattr(args, "codex_job_live", False))
    codex_bridge_port = getattr(args, "codex_bridge_port", 8001)
    run_dir = _new_run(
        args.workflow,
        args.input_json,
        args.max_tasks,
        mock_job_durable_wait=args.mock_job_durable_wait,
        codex_job_live=codex_job_live,
        codex_bridge_port=codex_bridge_port,
    )
    manifest, definition_text, _ = load_run_request(run_dir)
    definition = parse_workflow_text(definition_text)
    service = start_mock_job_service(run_dir) if _workflow_uses_mock_job(definition) else None
    try:
        result_record = _invoke_worker(run_dir, "start", args.timeout_seconds)
    finally:
        service_exit_code = service.stop() if service is not None else 0
    if service_exit_code != 0:
        raise RuntimeError(f"mock-job service exited with {service_exit_code}")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_id"] = result_record["workflow_id"]
    _write_json(manifest_path, manifest)
    return {
        "workflow_id": result_record["workflow_id"],
        "run_dir": str(run_dir.resolve()),
        "result": result_record["result"],
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
        **({"mode": "codex-job-live"} if codex_job_live else {}),
    }


def _resume(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    manifest, definition_text, _ = load_run_request(run_dir)
    control = manifest.get("control", {})
    if not isinstance(control, dict):
        raise OwsError("manifest control must be an object")
    codex_job_live = manifest.get("mode") == "codex-job-live"
    if control.get("codex_job_live", False) is not codex_job_live:
        raise OwsError("codex-job live mode does not match the saved manifest")
    validate_workflow(parse_workflow_text(definition_text), codex_job_live=codex_job_live)
    workflow_id = manifest.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise OwsError("resume requires a workflow_id saved after initial start")
    definition = parse_workflow_text(definition_text)
    service = start_mock_job_service(run_dir) if _workflow_uses_mock_job(definition) else None
    try:
        result_record = _invoke_worker(run_dir, "recover", args.timeout_seconds)
    finally:
        service_exit_code = service.stop() if service is not None else 0
    if service_exit_code != 0:
        raise RuntimeError(f"mock-job service exited with {service_exit_code}")
    if result_record["workflow_id"] != workflow_id:
        raise OwsError("recovery result workflow_id differs from the saved ID")
    return {
        "workflow_id": workflow_id,
        "run_dir": str(run_dir),
        "result": result_record["result"],
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
    }


def _status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    manifest, _, _ = load_run_request(run_dir)
    workflow_id = manifest.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise OwsError("status requires an initial workflow_id")
    return {
        "workflow_id": workflow_id,
        "status": _read_workflow_status(run_dir / "dbos.sqlite", workflow_id),
        "run_dir": str(run_dir),
    }


def _job_run_dir(run_dir: Path) -> Path:
    candidate = run_dir.resolve()
    phase_roots = {
        PHASE4B_STATE_ROOT.resolve(): "jobs.sqlite",
        PHASE4C_STATE_ROOT.resolve(): "jobs.sqlite",
        PHASE4D_STATE_ROOT.resolve(): "codex_jobs.sqlite",
    }
    database_name = phase_roots.get(candidate.parent)
    if database_name is None or not candidate.name.startswith("run-"):
        raise OwsError("job status and events require a run directory inside poc/ows/.state")
    database_path = candidate / database_name
    if not database_path.is_file() or database_path.resolve().parent != candidate:
        raise OwsError("job status and events require the run-local jobs.sqlite database")
    manifest, _, _ = load_run_request(candidate)
    is_live_run = candidate.parent == PHASE4D_STATE_ROOT.resolve()
    if (manifest.get("mode") == "codex-job-live") is not is_live_run:
        raise OwsError("job database mode does not match the run directory")
    return candidate


def _job_status(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _job_run_dir(args.run_dir)
    if run_dir.parent == PHASE4D_STATE_ROOT.resolve():
        job = CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_job_status(
            job_id=args.job_id,
            request_key=args.request_key,
        )
    else:
        job = DurableJobAdapter(run_dir / "jobs.sqlite").get_job_status(
            job_id=args.job_id,
            request_key=args.request_key,
        )
    return {"run_dir": str(run_dir), "job": job}


def _job_events(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = _job_run_dir(args.run_dir)
    if run_dir.parent == PHASE4D_STATE_ROOT.resolve():
        events = CodexJobAdapter(run_dir / "codex_jobs.sqlite").read_events(
            after_id=args.after_id,
            limit=args.limit,
        )
    else:
        events = DurableJobAdapter(run_dir / "jobs.sqlite").read_events(
            after_id=args.after_id,
            limit=args.limit,
        )
    return {"run_dir": str(run_dir), "events": events}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OWS v1.0.3 subset workflows on DBOS")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start one persisted workflow")
    run_parser.add_argument("--workflow", type=Path, required=True)
    run_parser.add_argument("--input-json", required=True)
    run_parser.add_argument("--max-tasks", type=int, default=10_000)
    run_parser.add_argument("--timeout-seconds", type=int, default=120)
    run_parser.add_argument("--mock-job-durable-wait", action="store_true")
    run_parser.add_argument("--codex-job-live", action="store_true")
    run_parser.add_argument("--codex-bridge-port", type=int, default=8001)

    resume_parser = subparsers.add_parser(
        "resume", help="call DBOS.launch to recover a saved workflow"
    )
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    resume_parser.add_argument("--timeout-seconds", type=int, default=120)

    status_parser = subparsers.add_parser("status", help="read saved workflow status")
    status_parser.add_argument("--run-dir", type=Path, required=True)

    job_status_parser = subparsers.add_parser("job-status", help="read one durable job status")
    job_status_parser.add_argument("--run-dir", type=Path, required=True)
    job_identifier = job_status_parser.add_mutually_exclusive_group(required=True)
    job_identifier.add_argument("--job-id")
    job_identifier.add_argument("--request-key")

    job_events_parser = subparsers.add_parser("job-events", help="read durable job events")
    job_events_parser.add_argument("--run-dir", type=Path, required=True)
    job_events_parser.add_argument("--after-id", type=int, default=0)
    job_events_parser.add_argument("--limit", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "run":
            result = _run(args)
        elif args.command == "resume":
            result = _resume(args)
        elif args.command == "job-status":
            result = _job_status(args)
        elif args.command == "job-events":
            result = _job_events(args)
        else:
            result = _status(args)
    except Exception as error:
        print(f"OWS CLI failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
