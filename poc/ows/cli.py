from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from interpreter import OwsError, parse_workflow_text, validate_workflow
from job_service import start_mock_job_service
from worker import (
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


def _new_run(workflow_path: Path, input_json: str, max_tasks: int) -> Path:
    try:
        definition_text = workflow_path.resolve().read_text(encoding="utf-8")
        input_data = json.loads(input_json)
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot load workflow or JSON input: {error}") from error
    definition = parse_workflow_text(definition_text)
    validate_workflow(definition)
    canonical_input = canonical_json(input_data)

    state_root = PHASE4B_STATE_ROOT if _workflow_uses_mock_job(definition) else DEFAULT_STATE_ROOT
    run_dir = state_root / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(canonical_input + "\n", encoding="utf-8", newline="\n")
    _write_json(
        run_dir / "manifest.json",
        {
            "workflow_source": str(workflow_path.resolve()),
            "workflow_id": None,
            "definition_sha256": sha256_text(definition_text),
            "input_sha256": sha256_json(input_data),
            "max_tasks": max_tasks,
            "control": {},
        },
    )
    return run_dir


def _workflow_uses_mock_job(definition: dict[str, Any]) -> bool:
    def has_call(tasks: list[dict[str, Any]]) -> bool:
        for task in tasks:
            task_spec = next(iter(task.values()))
            if "call" in task_spec:
                return True
            nested = task_spec.get("do")
            if isinstance(nested, list) and has_call(nested):
                return True
        return False

    return has_call(definition["do"])


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_tasks < 1:
        raise OwsError("--max-tasks must be positive")
    run_dir = _new_run(args.workflow, args.input_json, args.max_tasks)
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
    }


def _resume(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    manifest, definition_text, _ = load_run_request(run_dir)
    validate_workflow(parse_workflow_text(definition_text))
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OWS v1.0.3 subset workflows on DBOS")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start one persisted workflow")
    run_parser.add_argument("--workflow", type=Path, required=True)
    run_parser.add_argument("--input-json", required=True)
    run_parser.add_argument("--max-tasks", type=int, default=10_000)
    run_parser.add_argument("--timeout-seconds", type=int, default=120)

    resume_parser = subparsers.add_parser(
        "resume", help="call DBOS.launch to recover a saved workflow"
    )
    resume_parser.add_argument("--run-dir", type=Path, required=True)
    resume_parser.add_argument("--timeout-seconds", type=int, default=120)

    status_parser = subparsers.add_parser("status", help="read saved workflow status")
    status_parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "run":
            result = _run(args)
        elif args.command == "resume":
            result = _resume(args)
        else:
            result = _status(args)
    except Exception as error:
        print(f"OWS CLI failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
