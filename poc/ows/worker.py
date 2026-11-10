from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig
from interpreter import (
    Interpreter,
    OwsError,
    compute_mock_job_request,
    compute_task_action,
    parse_workflow_text,
    validate_workflow,
)
from job_adapter import DurableJobAdapter, submit_and_wait
from process_identity import get_process_identity
from sqlalchemy.engine import URL

APPLICATION_NAME = "codexbridge-ows-phase3-poc"
APPLICATION_VERSION = "1.0.3-subset"
EXECUTOR_ID = "codexbridge-ows-phase3-executor"


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise OwsError(f"value is not canonical JSON: {error}") from error


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        json.dump(value, output_file, ensure_ascii=False, indent=2, sort_keys=True)
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, path)


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as event_file:
        event_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        event_file.flush()
        os.fsync(event_file.fileno())


def _heartbeat(stop_event: threading.Event, path: Path) -> None:
    sequence = 0
    while not stop_event.wait(0.1):
        _append_event(
            path,
            {
                "event": "worker_heartbeat",
                "pid": os.getpid(),
                "sequence": sequence,
                "timestamp_utc": datetime.now(UTC).isoformat(),
            },
        )
        sequence += 1


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot read run manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise OwsError("run manifest must be a JSON object")
    return value


def load_run_request(run_dir: Path) -> tuple[dict[str, Any], str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    definition_path = run_dir / "workflow.yaml"
    input_path = run_dir / "input.json"
    try:
        definition_text = definition_path.read_text(encoding="utf-8")
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot read immutable workflow snapshot: {error}") from error

    actual_definition_hash = sha256_text(definition_text)
    actual_input_hash = sha256_json(input_data)
    if actual_definition_hash != manifest.get("definition_sha256"):
        raise OwsError("workflow snapshot hash does not match manifest")
    if actual_input_hash != manifest.get("input_sha256"):
        raise OwsError("workflow input hash does not match manifest")
    return manifest, definition_text, input_data


def _dbos_config(database_path: Path) -> DBOSConfig:
    database_url = URL.create(
        "sqlite", database=database_path.resolve().as_posix()
    ).render_as_string(hide_password=False)
    return {
        "name": APPLICATION_NAME,
        "application_version": APPLICATION_VERSION,
        "executor_id": EXECUTOR_ID,
        "system_database_url": database_url,
        "log_level": "ERROR",
        "console_log_level": "ERROR",
    }


def _output_digest(value: Any) -> str:
    return sha256_json(value)


def _validate_task_checkpoint(
    checkpoint: Any,
    *,
    identity: str,
    task_name: str,
    operation: str,
    definition_sha256: str,
    task_input: Any,
    variables: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise OwsError(f"persisted task checkpoint is invalid for {identity}")
    expected = {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": sha256_json(task_input),
        "context_sha256": sha256_json(variables.get("context")),
        "variables_sha256": sha256_json(variables),
    }
    for field, expected_value in expected.items():
        if checkpoint.get(field) != expected_value:
            label = "input digest" if field == "input_sha256" else field
            if field == "context_sha256":
                label = "context digest"
            elif field == "variables_sha256":
                label = "runtime variables digest"
            raise OwsError(f"persisted task {label} mismatch for {identity}")
    if "result" not in checkpoint:
        raise OwsError(f"persisted task checkpoint has no result for {identity}")
    return checkpoint


def _run_task_step(
    identity: str,
    task_name: str,
    operation: str,
    task_spec: dict[str, Any],
    input_data: Any,
    variables: dict[str, Any],
    definition_sha256: str,
    trace_path: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    event_path = Path(trace_path)
    started_at = datetime.now(UTC).isoformat()
    _append_event(
        event_path,
        {
            "event": "step_started",
            "identity": identity,
            "task_name": task_name,
            "operation": operation,
            "definition_sha256": definition_sha256,
            "pid": os.getpid(),
            "started_at": started_at,
        },
    )

    if control.get("gate_task_name") == task_name:
        marker_value = control.get("continue_marker")
        if not isinstance(marker_value, str):
            raise OwsError("test gate requires a continue marker path")
        marker = Path(marker_value)
        deadline = time.monotonic() + float(control.get("gate_timeout_seconds", 90))
        while not marker.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"test gate timed out for task {task_name!r}")
            time.sleep(0.05)

    input_sha256 = sha256_json(input_data)
    context_sha256 = sha256_json(variables.get("context"))
    variables_sha256 = sha256_json(variables)
    if operation == "call":
        request = compute_mock_job_request(task_spec, input_data, variables)
        workflow_id = DBOS.workflow_id
        if not isinstance(workflow_id, str) or not workflow_id:
            raise OwsError("mock-job adapter requires an active DBOS workflow")
        adapter = DurableJobAdapter(event_path.parent / "jobs.sqlite")
        timeout_seconds = control.get("mock_job_timeout_seconds", 10.0)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise OwsError("mock_job_timeout_seconds must be numeric")
        result = submit_and_wait(
            adapter,
            workflow_id,
            identity,
            request,
            timeout_seconds=float(timeout_seconds),
        )
        if control.get("gate_after_job_identity") == identity:
            marker_value = control.get("post_job_continue_marker")
            if not isinstance(marker_value, str):
                raise OwsError("post-job test gate requires a continue marker path")
            _append_event(
                event_path,
                {
                    "event": "job_succeeded_before_checkpoint",
                    "identity": identity,
                    "job_id": result["job_id"],
                    "request_key": result["request_key"],
                    "pid": os.getpid(),
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                },
            )
            marker = Path(marker_value)
            deadline = time.monotonic() + float(control.get("gate_timeout_seconds", 90))
            while not marker.is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError("post-job test gate timed out before DBOS checkpoint")
                time.sleep(0.05)
    else:
        result = compute_task_action(operation, task_spec, input_data, variables)
    _append_event(
        event_path,
        {
            "event": "step_completed",
            "identity": identity,
            "task_name": task_name,
            "operation": operation,
            "definition_sha256": definition_sha256,
            "output_sha256": _output_digest(result),
            "pid": os.getpid(),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    return {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": input_sha256,
        "context_sha256": context_sha256,
        "variables_sha256": variables_sha256,
        "result": result,
    }


@cache
def _named_step(identity: str) -> Any:
    step_name = f"ows_{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    return DBOS.step(name=step_name)(_run_task_step)


@DBOS.workflow()
def execute_ows_workflow(
    definition_text: str,
    definition_sha256: str,
    input_data: Any,
    input_sha256: str,
    trace_path: str,
    control: dict[str, Any],
    max_tasks: int,
) -> Any:
    if sha256_text(definition_text) != definition_sha256:
        raise OwsError("persisted OWS definition hash mismatch")
    if sha256_json(input_data) != input_sha256:
        raise OwsError("persisted OWS input hash mismatch")
    definition = parse_workflow_text(definition_text)
    validate_workflow(definition)

    def run_named_step(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        checkpoint = _named_step(identity)(
            identity,
            task_name,
            operation,
            task_spec,
            task_input,
            variables,
            definition_sha256,
            trace_path,
            control,
        )
        checkpoint = _validate_task_checkpoint(
            checkpoint,
            identity=identity,
            task_name=task_name,
            operation=operation,
            definition_sha256=definition_sha256,
            task_input=task_input,
            variables=variables,
        )
        return checkpoint["result"]

    interpreter = Interpreter(
        definition,
        max_tasks=max_tasks,
        step_runner=run_named_step,
    )
    return interpreter.run(input_data)


def _read_workflow_status(database_path: Path, workflow_id: str) -> str | None:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
        row = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
            (workflow_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def _wait_for_result(database_path: Path, workflow_id: str, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _read_workflow_status(database_path, workflow_id)
        if status == "SUCCESS":
            handle = DBOS.retrieve_workflow(workflow_id)
            return handle.get_result()
        if status in {"ERROR", "CANCELLED"}:
            raise RuntimeError(f"workflow {workflow_id} ended with status {status}")
        time.sleep(0.05)
    status = _read_workflow_status(database_path, workflow_id)
    raise TimeoutError(f"workflow {workflow_id} timed out with status {status!r}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OWS subset DBOS worker")
    parser.add_argument("--mode", choices=("start", "recover"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--startup-file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    run_dir = args.run_dir.resolve()
    manifest, definition_text, input_data = load_run_request(run_dir)
    startup_override = args.startup_file.resolve() if args.startup_file is not None else None
    if startup_override is not None:
        try:
            startup_override.relative_to(run_dir)
        except ValueError as error:
            raise OwsError("--startup-file must be inside the run directory") from error
    database_path = run_dir / "dbos.sqlite"
    control = manifest.get("control", {})
    if not isinstance(control, dict):
        raise OwsError("manifest control must be an object")

    worker_identity = get_process_identity(os.getpid())
    if worker_identity is None or not worker_identity["active"]:
        raise OwsError("DBOS worker could not confirm its Windows process identity")
    identity_record = {
        "mode": args.mode,
        "pid": worker_identity["pid"],
        "parent_pid": worker_identity["parent_pid"],
        "run_dir": str(run_dir),
        "argv": sys.argv,
        "creation_time_100ns": worker_identity["creation_time_100ns"],
        "image_path": worker_identity["image_path"],
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    identity_path = (
        startup_override.with_suffix(".identity.json")
        if startup_override is not None
        else run_dir / f"{args.mode}-worker-identity.json"
    )
    _write_json(identity_path, identity_record)

    DBOS(config=_dbos_config(database_path))
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    try:
        DBOS.launch()
        if args.mode == "start":
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                args=(heartbeat_stop, run_dir / "initial-worker-heartbeat.jsonl"),
                name="ows-poc-worker-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            handle = DBOS.start_workflow(
                execute_ows_workflow,
                definition_text,
                manifest["definition_sha256"],
                input_data,
                manifest["input_sha256"],
                str(run_dir / "task-events.jsonl"),
                control,
                int(manifest.get("max_tasks", 10_000)),
            )
            workflow_id = handle.workflow_id
            manifest["workflow_id"] = workflow_id
            _write_json(run_dir / "manifest.json", manifest)
            startup_path = run_dir / "start-worker.json"
        else:
            workflow_id = manifest.get("workflow_id")
            if not isinstance(workflow_id, str) or not workflow_id:
                raise OwsError("recover mode requires the persisted workflow_id")
            startup_path = run_dir / "recovery-worker.json"

        startup_path = startup_override if startup_override is not None else startup_path
        _write_json(
            startup_path,
            {**identity_record, "workflow_id": workflow_id},
        )

        if args.mode == "start":
            result = handle.get_result()
        else:
            result = _wait_for_result(database_path, workflow_id, args.timeout_seconds)

        _write_json(
            run_dir / "result.json",
            {
                "workflow_id": workflow_id,
                "result": result,
                "definition_sha256": manifest["definition_sha256"],
                "input_sha256": manifest["input_sha256"],
            },
        )
        print(json.dumps({"workflow_id": workflow_id, "result": result}, ensure_ascii=False))
        return 0
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        DBOS.destroy()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"OWS DBOS worker failed: {error}", file=sys.stderr, flush=True)
        raise
