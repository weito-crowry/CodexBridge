from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from job_adapter import DurableJobAdapter
from job_service import MockJobServiceProcess, start_mock_job_service
from phase4b_recovery_test import (
    WorkerProcess,
    _spawn_worker,
    _wait_for_event,
    _wait_for_json_pid,
    _wait_for_process_identity,
)
from worker import (
    _read_workflow_status,
    _write_json,
    canonical_json,
    sha256_json,
    sha256_text,
)

OWS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OWS_DIR.parents[1]
STATE_ROOT = OWS_DIR / ".state" / "phase4c-runs"
FIXTURE_PATH = OWS_DIR / "fixtures" / "mock_job_recovery_workflow.yaml"


def _new_run_dir() -> Path:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = STATE_ROOT / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=False, exist_ok=False)
    definition_text = FIXTURE_PATH.read_text(encoding="utf-8")
    input_data = {"payload": {"value": 42}, "duration_ms": 2000}
    input_json = canonical_json(input_data)
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8", newline="\n")
    (run_dir / "input.json").write_text(input_json + "\n", encoding="utf-8", newline="\n")
    _write_json(
        run_dir / "manifest.json",
        {
            "workflow_source": str(FIXTURE_PATH.resolve()),
            "workflow_id": None,
            "definition_sha256": sha256_text(definition_text),
            "input_sha256": sha256_json(input_data),
            "max_tasks": 10_000,
            "control": {
                "mock_job_durable_wait": True,
                "mock_job_max_polls": 100,
                "mock_job_poll_interval_seconds": 0.25,
                "mock_job_reconciliation_ttl_seconds": 0.5,
            },
        },
    )
    return run_dir


def _named_checkpoint(
    database_path: Path, workflow_id: str, function_name: str
) -> tuple[str, int | None] | None:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT function_name, completed_at_epoch_ms FROM operation_outputs "
            "WHERE workflow_uuid = ? AND function_name = ?",
            (workflow_id, function_name),
        ).fetchone()


def _wait_for_job_state(
    adapter: DurableJobAdapter, job_id: str, expected_state: str, timeout_seconds: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = adapter.get_job(job_id)
        if job is not None and job["state"] == expected_state:
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not reach {expected_state}")


def _semantic_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    return {key: result[key] for key in ("status", "result") if key in result}


def _stop_child(child: WorkerProcess) -> None:
    if child.process.poll() is None:
        child.stop(kill=True)
    elif not child.closed:
        child.stdout_file.close()
        child.stderr_file.close()
        child.closed = True


def run_recovery_experiment(normal_result_path: Path) -> dict[str, Any]:
    normal_record = json.loads(normal_result_path.read_text(encoding="utf-8"))
    run_dir = _new_run_dir()
    service: MockJobServiceProcess | None = None
    workers: list[WorkerProcess] = []
    try:
        service = start_mock_job_service(run_dir)
        initial = _spawn_worker(run_dir, "start", "initial-worker")
        workers.append(initial)
        _wait_for_process_identity(initial)
        initial_meta = _wait_for_json_pid(initial.startup_path, initial)
        workflow_id = initial_meta["workflow_id"]

        _wait_for_event(
            run_dir / "task-events.jsonl",
            "durable_job_started_before_wait",
            "do[0].process",
        )
        adapter = DurableJobAdapter(run_dir / "jobs.sqlite")
        job = adapter.get_job_by_request_key(f"{workflow_id}::do[0].process")
        if job is None or job["state"] not in {"QUEUED", "RUNNING"}:
            raise AssertionError("job finished before the worker-kill recovery window")
        start_name = "ows4c_" + hashlib.sha256(b"do[0].process\x00start\x000").hexdigest()
        start_checkpoint = _named_checkpoint(run_dir / "dbos.sqlite", workflow_id, start_name)
        start_checkpointed = start_checkpoint is not None and start_checkpoint[1] is not None
        if not start_checkpointed:
            raise AssertionError("durable job start step had no completed DBOS checkpoint")

        job_state_at_kill = job["state"]
        if job_state_at_kill not in {"QUEUED", "RUNNING"}:
            raise AssertionError("worker kill did not occur while the mock job was unfinished")
        initial.stop(kill=True)
        service_survived = service.is_running()
        if not service_survived:
            raise AssertionError("mock service did not survive the DBOS worker termination")

        completed_job = _wait_for_job_state(adapter, job["job_id"], "SUCCEEDED")
        if not service.is_running():
            raise AssertionError("mock service exited before its owned job completed")
        recovery = _spawn_worker(run_dir, "recover", "recovery-worker")
        workers.append(recovery)
        _wait_for_process_identity(recovery)
        recovery_meta = _wait_for_json_pid(recovery.startup_path, recovery)
        if recovery_meta["workflow_id"] != workflow_id:
            raise AssertionError("DBOS recovery used a different workflow ID")
        recovery_exit_code = recovery.stop()
        if recovery_exit_code != 0:
            raise RuntimeError(f"recovery worker exited with {recovery_exit_code}")

        result_record = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        final_job = adapter.get_job(job["job_id"])
        events = adapter.read_events(after_id=0, limit=1000)
        workflow_status = _read_workflow_status(run_dir / "dbos.sqlite", workflow_id)
        with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
            workflow_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT workflow_uuid) FROM workflow_status"
                ).fetchone()[0]
            )
        request_key_stable = final_job["request_key"] == job["request_key"]
        semantic_match = _semantic_result(result_record["result"]) == _semantic_result(
            normal_record["result"]
        )
        event_states = [event["state"] for event in events if event["job_id"] == job["job_id"]]
        if final_job["state"] != "SUCCEEDED" or workflow_status != "SUCCESS":
            raise AssertionError("recovered workflow or job did not reach success")
        if final_job["dispatch_count"] != 1 or final_job["service_execution_count"] != 1:
            raise AssertionError("recovery redispatched or re-executed the mock job")
        if event_states[0] != "QUEUED" or event_states[-1] != "SUCCEEDED":
            raise AssertionError("durable job event history did not include queued and success")
        if not request_key_stable or not semantic_match or workflow_count != 1:
            raise AssertionError("recovery identity or semantic output did not match baseline")

        evidence = {
            "result": "PASS",
            "run_dir": str(run_dir),
            "initial_workflow_id": workflow_id,
            "recovered_workflow_id": recovery_meta["workflow_id"],
            "job_id": job["job_id"],
            "request_key": job["request_key"],
            "request_key_stable": request_key_stable,
            "job_state_at_worker_kill": job_state_at_kill,
            "job_state_after_service_completion": completed_job["state"],
            "start_step_checkpointed_before_kill": start_checkpointed,
            "dispatch_count": final_job["dispatch_count"],
            "service_execution_count": final_job["service_execution_count"],
            "ledger_row_count": adapter.count_jobs(),
            "event_count": len(events),
            "event_states": event_states,
            "service_survived_worker_kill": service_survived,
            "final_status": workflow_status,
            "workflow_count": workflow_count,
            "result_matches_uninterrupted_semantics": semantic_match,
            "final_result": result_record["result"],
        }
        _write_json(run_dir / "phase4c-recovery-evidence.json", evidence)
        return evidence
    finally:
        for child in reversed(workers):
            _stop_child(child)
        if service is not None and service.process.poll() is None:
            service.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 4C durable-wait recovery experiment")
    parser.add_argument("--normal-result-path", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        evidence = run_recovery_experiment(args.normal_result_path.resolve())
    except Exception as error:
        print(f"Phase 4C recovery failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
