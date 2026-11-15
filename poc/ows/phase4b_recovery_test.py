from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

from interpreter import load_workflow
from job_adapter import DurableJobAdapter
from job_service import MockJobServiceProcess, start_mock_job_service
from process_identity import (
    get_process_identity,
    process_is_active,
    terminate_spawned_python,
)
from worker import _read_workflow_status, _write_json, canonical_json, sha256_json, sha256_text

OWS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OWS_DIR.parents[1]
PHASE4B_STATE_ROOT = OWS_DIR / ".state" / "phase4b-runs"
WORKER_PATH = OWS_DIR / "worker.py"
FIXTURE_PATH = OWS_DIR / "fixtures" / "mock_job_recovery_workflow.yaml"


class WorkerProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stdout_file: TextIO,
        stderr_file: TextIO,
        run_dir: Path,
        expected_argv: list[str],
        identity_path: Path,
        startup_path: Path,
    ):
        self.process = process
        self.stdout_file = stdout_file
        self.stderr_file = stderr_file
        self.run_dir = run_dir.resolve()
        self.expected_argv = expected_argv
        self.identity_path = identity_path
        self.startup_path = startup_path
        self.closed = False
        self.worker_pid: int | None = None
        self.worker_creation_time_100ns: int | None = None
        self.worker_image_path: str | None = None

    def worker_is_running(self) -> bool:
        if self.worker_pid is None:
            return self.process.poll() is None
        if self.worker_creation_time_100ns is None or self.worker_image_path is None:
            raise RuntimeError("worker process identity is incomplete")
        return process_is_active(
            self.worker_pid,
            expected_creation_time_100ns=self.worker_creation_time_100ns,
        )

    def stop(self, *, kill: bool = False, timeout_seconds: float = 20.0) -> int:
        if kill and self.worker_pid is None:
            raise RuntimeError("refusing to kill a worker without its verified runtime PID")
        if kill and self.worker_is_running():
            if self.worker_creation_time_100ns is None or self.worker_image_path is None:
                raise RuntimeError("refusing to kill a worker without its creation-time identity")
            terminate_spawned_python(
                self.worker_pid,
                self.process.pid,
                expected_creation_time_100ns=self.worker_creation_time_100ns,
                expected_image_path=self.worker_image_path,
            )
        deadline = time.monotonic() + timeout_seconds
        while self.worker_is_running():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"verified worker PID {self.worker_pid} did not stop")
            time.sleep(0.02)
        while self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.process.poll() is None:
            raise TimeoutError(
                f"Popen launcher PID {self.process.pid} did not exit after its worker stopped"
            )
        if not self.closed:
            self.stdout_file.close()
            self.stderr_file.close()
            self.closed = True
        return int(self.process.returncode or 0)


def _new_run_dir() -> Path:
    PHASE4B_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = PHASE4B_STATE_ROOT / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=False, exist_ok=False)
    definition_text = FIXTURE_PATH.read_text(encoding="utf-8")
    input_data = {"payload": {"value": 42}, "duration_ms": 250}
    definition = load_workflow(FIXTURE_PATH)
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
                "gate_after_job_identity": "do[0].process",
                "post_job_continue_marker": str(run_dir / "after-job.marker"),
                "gate_task_name": "finish",
                "continue_marker": str(run_dir / "finish.marker"),
                "gate_timeout_seconds": 60,
                "mock_job_timeout_seconds": 5,
            },
        },
    )
    if definition["do"][0]["process"]["call"] != "mock-job":
        raise RuntimeError("recovery fixture was not validated against the mock-job subset")
    return run_dir


def _spawn_worker(run_dir: Path, mode: str, label: str) -> WorkerProcess:
    stdout_file = (run_dir / f"{label}.stdout.log").open("w", encoding="utf-8", newline="\n")
    stderr_file = (run_dir / f"{label}.stderr.log").open("w", encoding="utf-8", newline="\n")
    startup_path = run_dir / f"{label}.startup.json"
    identity_path = startup_path.with_suffix(".identity.json")
    command = [
        sys.executable,
        str(WORKER_PATH),
        "--mode",
        mode,
        "--run-dir",
        str(run_dir.resolve()),
        "--timeout-seconds",
        "60",
        "--startup-file",
        str(startup_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    return WorkerProcess(
        process,
        stdout_file,
        stderr_file,
        run_dir,
        command[1:],
        identity_path,
        startup_path,
    )


def _accept_process_identity(
    value: dict[str, Any], expected_pid: int, child: WorkerProcess
) -> None:
    worker_pid = value.get("pid")
    parent_pid = value.get("parent_pid")
    if (
        value.get("run_dir") != str(child.run_dir)
        or value.get("argv") != child.expected_argv
        or not isinstance(worker_pid, int)
        or (worker_pid != expected_pid and parent_pid != expected_pid)
    ):
        raise RuntimeError(
            f"worker identity is outside its Popen process tree: "
            f"run_dir={value.get('run_dir')}, expected_run_dir={child.run_dir}, "
            f"popen_pid={expected_pid}, worker_pid={worker_pid}, parent_pid={parent_pid}"
        )
    process_identity = get_process_identity(worker_pid)
    if (
        process_identity is None
        or not process_identity["active"]
        or process_identity["creation_time_100ns"] != value.get("creation_time_100ns")
        or process_identity["image_path"] != value.get("image_path")
        or str(process_identity["image_path"]).rsplit("\\", 1)[-1].casefold() != "python.exe"
        or (worker_pid != expected_pid and process_identity["parent_pid"] != expected_pid)
    ):
        raise RuntimeError("runtime worker PID does not match its startup identity")
    child.worker_pid = worker_pid
    child.worker_creation_time_100ns = int(value["creation_time_100ns"])
    child.worker_image_path = str(value["image_path"])


def _wait_for_process_identity(child: WorkerProcess) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if child.identity_path.is_file():
            try:
                value = json.loads(child.identity_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                _accept_process_identity(value, child.process.pid, child)
                return value
        if child.process.poll() is not None:
            raise RuntimeError("worker launcher exited before writing its process identity")
        time.sleep(0.02)
    raise TimeoutError(f"worker did not write its process identity to {child.identity_path}")


def _wait_for_json_pid(path: Path, child: WorkerProcess) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict) and value.get("pid") == child.worker_pid:
                if (
                    value.get("run_dir") != str(child.run_dir)
                    or value.get("argv") != child.expected_argv
                    or value.get("creation_time_100ns") != child.worker_creation_time_100ns
                    or value.get("image_path") != child.worker_image_path
                ):
                    raise RuntimeError("worker startup record differs from its verified identity")
                return value
        if child.process.poll() is not None and not path.is_file():
            raise RuntimeError(f"worker exited before writing {path.name}")
        time.sleep(0.02)
    raise TimeoutError(f"worker did not write its startup record to {path}")


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wait_for_event(path: Path, event: str, identity: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for record in _events(path):
            if record.get("event") == event and record.get("identity") == identity:
                return record
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {event} at {identity}")


def _heartbeat_count(path: Path) -> int:
    return len(_events(path))


def _service_heartbeat_count(path: Path) -> int:
    return sum(record.get("event") == "service_heartbeat" for record in _events(path))


def _wait_for_heartbeats(path: Path, count: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _heartbeat_count(path) >= count:
            return
        time.sleep(0.02)
    raise TimeoutError(f"worker heartbeat did not reach {count} updates")


def _dbos_step_row(
    database_path: Path, workflow_id: str, identity: str
) -> tuple[str, int | None] | None:
    function_name = "ows_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            "SELECT function_name, completed_at_epoch_ms FROM operation_outputs "
            "WHERE workflow_uuid = ? AND function_name = ?",
            (workflow_id, function_name),
        ).fetchone()


def _semantic_result(result: dict[str, Any]) -> dict[str, Any]:
    return {"status": result["status"], "result": result["result"]}


def run_recovery_experiment(
    normal_result: dict[str, Any], normal_workflow_id: str
) -> dict[str, Any]:
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
            run_dir / "task-events.jsonl", "job_succeeded_before_checkpoint", "do[0].process"
        )
        adapter = DurableJobAdapter(run_dir / "jobs.sqlite")
        job = adapter.get_job_by_request_key(f"{workflow_id}::do[0].process")
        if job is None or job["state"] != "SUCCEEDED":
            raise AssertionError("service job did not finish before the checkpoint barrier")
        checkpoint_absent_at_first_barrier = (
            _dbos_step_row(run_dir / "dbos.sqlite", workflow_id, "do[0].process") is None
        )
        if not checkpoint_absent_at_first_barrier:
            raise AssertionError("call checkpoint was already complete at the post-job barrier")

        heartbeat_path = run_dir / "initial-worker-heartbeat.jsonl"
        _wait_for_heartbeats(heartbeat_path, 2)
        heartbeats_before_kill = _heartbeat_count(heartbeat_path)
        heartbeat_pid_matches_worker = {
            record.get("pid") for record in _events(heartbeat_path)
        } == {initial_meta["pid"]}
        if not heartbeat_pid_matches_worker:
            raise AssertionError("worker heartbeats do not match the verified child PID")
        initial_return_code = initial.stop(kill=True)
        heartbeats_at_kill = _heartbeat_count(heartbeat_path)
        time.sleep(0.6)
        heartbeats_after_kill = _heartbeat_count(heartbeat_path)
        if heartbeats_after_kill != heartbeats_at_kill:
            raise AssertionError(
                "initial DBOS worker heartbeat continued after its Popen was killed"
            )
        checkpoint_absent_after_kill = (
            _dbos_step_row(run_dir / "dbos.sqlite", workflow_id, "do[0].process") is None
        )
        if not checkpoint_absent_after_kill:
            raise AssertionError("call checkpoint completed after worker death")
        service_heartbeats_at_kill = _service_heartbeat_count(run_dir / "job-service.jsonl")
        service_running_after_kill = service.is_running()
        time.sleep(0.6)
        service_heartbeats_after_kill = _service_heartbeat_count(run_dir / "job-service.jsonl")
        if (
            not service_running_after_kill
            or service_heartbeats_after_kill <= service_heartbeats_at_kill
        ):
            raise AssertionError("independent mock service stopped with the DBOS worker")

        (run_dir / "after-job.marker").write_text("continue\n", encoding="utf-8")
        replay = _spawn_worker(run_dir, "recover", "recovery-worker-1")
        workers.append(replay)
        _wait_for_process_identity(replay)
        replay_meta = _wait_for_json_pid(replay.startup_path, replay)
        _wait_for_event(run_dir / "task-events.jsonl", "step_started", "do[1].finish")
        completed_call_checkpoint = _dbos_step_row(
            run_dir / "dbos.sqlite", workflow_id, "do[0].process"
        )
        if completed_call_checkpoint is None or completed_call_checkpoint[1] is None:
            raise AssertionError("recovered call step was not checkpointed before the next gate")
        dispatches_before_checkpoint_replay = adapter.get_job(job["job_id"])["dispatch_count"]
        replay_return_code = replay.stop(kill=True)

        (run_dir / "finish.marker").write_text("continue\n", encoding="utf-8")
        final_worker = _spawn_worker(run_dir, "recover", "recovery-worker-2")
        workers.append(final_worker)
        _wait_for_process_identity(final_worker)
        final_meta = _wait_for_json_pid(final_worker.startup_path, final_worker)
        final_return_code = final_worker.stop(timeout_seconds=60)
        if final_return_code != 0:
            raise RuntimeError("final DBOS recovery worker exited unsuccessfully")

        final_result_record = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        final_job = adapter.get_job(job["job_id"])
        all_jobs = []
        with sqlite3.connect(run_dir / "jobs.sqlite") as connection:
            all_jobs = connection.execute(
                "SELECT job_id, request_key, workflow_id FROM jobs ORDER BY created_at, job_id"
            ).fetchall()
        final_status = _read_workflow_status(run_dir / "dbos.sqlite", workflow_id)
        task_events = _events(run_dir / "task-events.jsonl")
        call_starts = [
            record
            for record in task_events
            if record.get("event") == "step_started" and record.get("identity") == "do[0].process"
        ]
        call_completions = [
            record
            for record in task_events
            if record.get("event") == "step_completed" and record.get("identity") == "do[0].process"
        ]
        with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
            workflow_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid = ?", (workflow_id,)
                ).fetchone()[0]
            )
        normalized_final = _semantic_result(final_result_record["result"])
        if normalized_final != _semantic_result(normal_result):
            raise AssertionError("recovered output differs from uninterrupted mock-job semantics")
        if final_meta["workflow_id"] != workflow_id or replay_meta["workflow_id"] != workflow_id:
            raise AssertionError("recovery did not retain the original workflow ID")
        if workflow_id == normal_workflow_id:
            raise AssertionError("baseline and recovery were expected to use distinct workflow IDs")
        if final_status != "SUCCESS" or workflow_count != 1:
            raise AssertionError("recovered workflow did not finish SUCCESS under the original ID")
        if final_job is None or final_job["job_id"] != job["job_id"]:
            raise AssertionError("recovery did not retain the original durable job ID")
        if (
            final_job["request_key"] != job["request_key"]
            or len(all_jobs) != 1
            or all_jobs[0] != (job["job_id"], job["request_key"], workflow_id)
        ):
            raise AssertionError("recovery changed its request key or created another ledger row")
        if final_job["dispatch_count"] != dispatches_before_checkpoint_replay:
            raise AssertionError("completed call checkpoint replay invoked the adapter again")
        if (
            final_job["service_execution_count"] != 1
            or len(call_starts) != 2
            or len(call_completions) != 1
        ):
            raise AssertionError("job effect or call checkpoint count was not exactly once")

        evidence = {
            "result": "PASS",
            "run_dir": str(run_dir.resolve()),
            "workflow_id": workflow_id,
            "normal_workflow_id": normal_workflow_id,
            "initial_worker_pid": initial_meta["pid"],
            "initial_worker_parent_pid": initial_meta["parent_pid"],
            "initial_popen_pid": initial.process.pid,
            "recovery_worker_pid": replay_meta["pid"],
            "recovery_worker_parent_pid": replay_meta["parent_pid"],
            "recovery_popen_pid": replay.process.pid,
            "final_worker_pid": final_meta["pid"],
            "final_worker_parent_pid": final_meta["parent_pid"],
            "final_popen_pid": final_worker.process.pid,
            "initial_worker_return_code": initial_return_code,
            "first_recovery_worker_return_code": replay_return_code,
            "final_recovery_worker_return_code": final_return_code,
            "service_pid": service.service_pid,
            "service_popen_pid": service.pid,
            "service_survived_initial_worker_kill": service_running_after_kill,
            "service_heartbeats_at_worker_kill": service_heartbeats_at_kill,
            "service_heartbeats_after_worker_kill": service_heartbeats_after_kill,
            "job_id": job["job_id"],
            "request_key": job["request_key"],
            "ledger_row_count": len(all_jobs),
            "request_key_stable_across_recovery": final_job["request_key"] == job["request_key"],
            "job_state_before_checkpoint": job["state"],
            "job_dispatch_count_after_checkpoint_replay": final_job["dispatch_count"],
            "service_execution_count": final_job["service_execution_count"],
            "heartbeat_updates_at_kill": heartbeats_at_kill,
            "heartbeat_updates_before_kill": heartbeats_before_kill,
            "heartbeat_updates_after_kill": heartbeats_after_kill,
            "heartbeat_pid_matches_worker": heartbeat_pid_matches_worker,
            "call_step_starts": len(call_starts),
            "call_step_completions": len(call_completions),
            "checkpoint_absent_at_first_barrier": checkpoint_absent_at_first_barrier,
            "checkpoint_absent_after_worker_kill": checkpoint_absent_after_kill,
            "final_status": final_status,
            "workflow_count": workflow_count,
            "result_matches_uninterrupted_semantics": normalized_final
            == _semantic_result(normal_result),
            "business_result": final_result_record["result"],
        }
        _write_json(run_dir / "phase4b-recovery-evidence.json", evidence)
        return evidence
    finally:
        for child in reversed(workers):
            if child.process.poll() is None:
                child.stop(kill=True, timeout_seconds=5)
        if service is not None and service.process.poll() is None:
            service.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4B mock-job crash recovery experiment")
    parser.add_argument("--normal-result-path", type=Path, required=True)
    args = parser.parse_args()
    normal_record = json.loads(args.normal_result_path.read_text(encoding="utf-8"))
    if (
        not isinstance(normal_record, dict)
        or not isinstance(normal_record.get("result"), dict)
        or not isinstance(normal_record.get("workflow_id"), str)
    ):
        raise ValueError("normal result file must contain its workflow_id and result object")
    normal_result = normal_record["result"]
    evidence = run_recovery_experiment(normal_result, normal_record["workflow_id"])
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
