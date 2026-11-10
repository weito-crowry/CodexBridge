from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from interpreter import Interpreter, parse_workflow_text, validate_workflow
from worker import _write_json, canonical_json, sha256_json, sha256_text

OWS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OWS_DIR.parent.parent
WORKER_PATH = OWS_DIR / "worker.py"
CLI_PATH = OWS_DIR / "cli.py"
RECOVERY_FIXTURE = OWS_DIR / "fixtures" / "recovery_workflow.yaml"
POLL_SECONDS = 0.05


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    contents = path.read_text(encoding="utf-8")
    events: list[dict[str, Any]] = []
    for line in contents.splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _database_snapshot(database_path: Path, workflow_id: str) -> dict[str, Any]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
        workflow_count = int(
            connection.execute("SELECT COUNT(*) FROM workflow_status").fetchone()[0]
        )
        workflow_row = connection.execute(
            "SELECT workflow_uuid, status, executor_id, application_version, application_name "
            "FROM workflow_status WHERE workflow_uuid = ?",
            (workflow_id,),
        ).fetchone()
        operation_rows = connection.execute(
            "SELECT function_name, started_at_epoch_ms, completed_at_epoch_ms "
            "FROM operation_outputs WHERE workflow_uuid = ? ORDER BY function_id",
            (workflow_id,),
        ).fetchall()
    operations = [
        {
            "function_name": str(name),
            "started_at_epoch_ms": started,
            "completed_at_epoch_ms": completed,
        }
        for name, started, completed in operation_rows
    ]
    return {
        "workflow_count": workflow_count,
        "workflow": None
        if workflow_row is None
        else {
            "workflow_id": str(workflow_row[0]),
            "status": str(workflow_row[1]),
            "executor_id": workflow_row[2],
            "application_version": workflow_row[3],
            "application_name": workflow_row[4],
        },
        "operations": operations,
        "completed_step_count": sum(
            operation["completed_at_epoch_ms"] is not None for operation in operations
        ),
    }


def _wait_for_interrupted_state(
    process: subprocess.Popen[bytes],
    run_dir: Path,
    deadline: float,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    startup_path = run_dir / "start-worker.json"
    database_path = run_dir / "dbos.sqlite"
    event_path = run_dir / "task-events.jsonl"
    last_error: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("start worker exited before the gate task started")
        startup = _read_json(startup_path)
        events = _read_events(event_path)
        heartbeats = _read_events(run_dir / "initial-worker-heartbeat.jsonl")
        if startup is None or not isinstance(startup.get("workflow_id"), str):
            time.sleep(POLL_SECONDS)
            continue
        workflow_id = startup["workflow_id"]
        try:
            snapshot = _database_snapshot(database_path, workflow_id)
        except sqlite3.OperationalError as error:
            last_error = str(error)
            time.sleep(POLL_SECONDS)
            continue
        started_tasks = [
            event["task_name"] for event in events if event.get("event") == "step_started"
        ]
        completed_tasks = [
            event["task_name"] for event in events if event.get("event") == "step_completed"
        ]
        heartbeat_events = [
            event for event in heartbeats if event.get("event") == "worker_heartbeat"
        ]
        workflow = snapshot["workflow"]
        if (
            started_tasks[:2] == ["first", "second"]
            and completed_tasks == ["first"]
            and snapshot["workflow_count"] == 1
            and workflow is not None
            and workflow["status"] == "PENDING"
            and snapshot["completed_step_count"] == 1
            and len(heartbeat_events) >= 2
            and all(event.get("pid") == startup.get("pid") for event in heartbeat_events)
            and not (run_dir / "continue.marker").exists()
        ):
            return startup, snapshot, events, heartbeat_events
        time.sleep(POLL_SECONDS)
    raise TimeoutError(
        "start worker did not reach second task with exactly one completed DBOS step; "
        f"last_database_error={last_error!r}, events={_read_events(event_path)!r}"
    )


def _stop_owned_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=5)


def run_experiment(state_dir: Path, timeout_seconds: int = 90) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    run_dir = state_dir / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    definition_text = RECOVERY_FIXTURE.read_text(encoding="utf-8")
    definition = parse_workflow_text(definition_text)
    validate_workflow(definition)
    input_data = {"value": 0}
    baseline_result = Interpreter(definition).run(input_data)
    definition_hash = sha256_text(definition_text)
    input_hash = sha256_json(input_data)
    continue_marker = run_dir / "continue.marker"
    manifest: dict[str, Any] = {
        "workflow_source": str(RECOVERY_FIXTURE),
        "workflow_id": None,
        "definition_sha256": definition_hash,
        "input_sha256": input_hash,
        "max_tasks": 100,
        "control": {
            "gate_task_name": "second",
            "continue_marker": str(continue_marker),
            "gate_timeout_seconds": timeout_seconds,
        },
    }
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(
        canonical_json(input_data) + "\n", encoding="utf-8", newline="\n"
    )
    _write_json(run_dir / "manifest.json", manifest)

    initial_stdout_path = run_dir / "initial.stdout.log"
    initial_stderr_path = run_dir / "initial.stderr.log"
    initial_process: subprocess.Popen[bytes] | None = None
    recovery_process: subprocess.Popen[bytes] | None = None
    initial_stdout = initial_stdout_path.open("wb")
    initial_stderr = initial_stderr_path.open("wb")
    recovery_stdout = None
    recovery_stderr = None
    try:
        initial_process = subprocess.Popen(
            [
                sys.executable,
                str(WORKER_PATH),
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--timeout-seconds",
                str(timeout_seconds),
            ],
            cwd=REPO_ROOT,
            stdout=initial_stdout,
            stderr=initial_stderr,
        )
        initial_popen_pid = initial_process.pid
        startup, before_kill, events_before_kill, heartbeats_before_kill = (
            _wait_for_interrupted_state(
                initial_process,
                run_dir,
                time.monotonic() + timeout_seconds,
            )
        )
        workflow_id = str(startup["workflow_id"])
        initial_worker_pid = int(startup["pid"])
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("workflow_id") != workflow_id:
            raise RuntimeError("start worker did not atomically persist workflow_id")
        manifest_id_persisted_before_kill = True
        _write_json(run_dir / "before-kill.json", before_kill)
        _write_json(run_dir / "events-before-kill.json", {"events": events_before_kill})
        heartbeat_path = run_dir / "initial-worker-heartbeat.jsonl"
        pre_kill_heartbeats = [
            event
            for event in _read_events(heartbeat_path)
            if event.get("event") == "worker_heartbeat"
        ]
        if len(pre_kill_heartbeats) < 2 or not all(
            event.get("pid") == initial_worker_pid for event in pre_kill_heartbeats
        ):
            raise RuntimeError("initial worker heartbeat was not active before kill")

        if initial_process.poll() is not None:
            raise RuntimeError("start worker exited before parent could terminate it")
        initial_process.kill()
        initial_return_code = initial_process.wait(timeout=5)
        if initial_process.poll() is None:
            raise RuntimeError("Popen.kill() did not terminate the owned start worker")
        initial_stdout.close()
        initial_stderr.close()

        heartbeats_at_kill = [
            event
            for event in _read_events(heartbeat_path)
            if event.get("event") == "worker_heartbeat"
        ]
        heartbeat_quiet_started = time.monotonic()
        time.sleep(0.55)
        heartbeat_quiet_seconds = time.monotonic() - heartbeat_quiet_started
        heartbeats_after_kill = _read_events(heartbeat_path)
        heartbeat_events_after_kill = [
            event for event in heartbeats_after_kill if event.get("event") == "worker_heartbeat"
        ]
        heartbeat_stopped = (
            len(heartbeats_at_kill) >= len(pre_kill_heartbeats)
            and len(heartbeat_events_after_kill) == len(heartbeats_at_kill)
            and all(
                event.get("pid") == initial_worker_pid
                for event in heartbeats_at_kill + heartbeat_events_after_kill
            )
            and heartbeat_quiet_seconds >= 0.5
        )
        if not heartbeat_stopped:
            raise RuntimeError("initial worker heartbeat did not stop for at least 0.5 seconds")

        after_kill = _database_snapshot(run_dir / "dbos.sqlite", workflow_id)
        events_after_kill = _read_events(run_dir / "task-events.jsonl")
        if after_kill["workflow_count"] != 1 or after_kill["completed_step_count"] != 1:
            raise RuntimeError(f"unexpected DBOS state after kill: {after_kill!r}")
        if events_after_kill != events_before_kill:
            raise RuntimeError("task events changed after the owned worker exited")
        _write_json(run_dir / "before-recovery.json", after_kill)
        (run_dir / "continue.marker").write_text("continue\n", encoding="utf-8")

        recovery_stdout_path = run_dir / "resume-cli.stdout.log"
        recovery_stderr_path = run_dir / "resume-cli.stderr.log"
        recovery_stdout = recovery_stdout_path.open("wb")
        recovery_stderr = recovery_stderr_path.open("wb")
        recovery_process = subprocess.Popen(
            [
                sys.executable,
                str(CLI_PATH),
                "resume",
                "--run-dir",
                str(run_dir),
                "--timeout-seconds",
                str(timeout_seconds),
            ],
            cwd=REPO_ROOT,
            stdout=recovery_stdout,
            stderr=recovery_stderr,
        )
        recovery_popen_pid = recovery_process.pid
        recovery_return_code = recovery_process.wait(timeout=timeout_seconds + 15)
        recovery_stdout.close()
        recovery_stderr.close()
        if recovery_return_code != 0:
            raise RuntimeError(
                "cli.py resume failed; "
                f"stdout={recovery_stdout_path.read_text(encoding='utf-8', errors='replace')!r}, "
                f"stderr={recovery_stderr_path.read_text(encoding='utf-8', errors='replace')!r}"
            )

        recovery_result = json.loads(
            recovery_stdout_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        recovery_metadata = _read_json(run_dir / "recovery-worker.json")
        if recovery_metadata is None:
            raise RuntimeError("recovery worker did not record startup metadata")
        final_result_record = _read_json(run_dir / "result.json")
        if final_result_record is None:
            raise RuntimeError("recovery did not write result.json")
        final_snapshot = _database_snapshot(run_dir / "dbos.sqlite", workflow_id)
        final_events = _read_events(run_dir / "task-events.jsonl")
        starts = Counter(
            event["task_name"] for event in final_events if event.get("event") == "step_started"
        )
        counts = {name: starts[name] for name in ("first", "second", "finish")}
        final_workflow = final_snapshot["workflow"]
        evidence = {
            "run_dir": str(run_dir.resolve()),
            "workflow_id": workflow_id,
            "recovery_workflow_id": recovery_result["workflow_id"],
            "same_workflow_id": recovery_result["workflow_id"] == workflow_id,
            "manifest_id_persisted_before_kill": manifest_id_persisted_before_kill,
            "initial_popen_pid": initial_popen_pid,
            "initial_worker_pid": initial_worker_pid,
            "initial_return_code": initial_return_code,
            "initial_killed_by_parent": True,
            "initial_heartbeat_pid": initial_worker_pid,
            "initial_heartbeat_updates_before_kill": len(pre_kill_heartbeats),
            "initial_heartbeat_updates_at_kill": len(heartbeats_at_kill),
            "initial_heartbeat_updates_after_kill": len(heartbeat_events_after_kill),
            "initial_heartbeat_quiet_seconds": heartbeat_quiet_seconds,
            "initial_heartbeat_stopped": heartbeat_stopped,
            "recovery_popen_pid": recovery_popen_pid,
            "recovery_worker_pid": recovery_metadata["pid"],
            "initial_recovery_pids_differ": initial_worker_pid != recovery_metadata["pid"],
            "workflow_count": final_snapshot["workflow_count"],
            "initial_completed_step_count": before_kill["completed_step_count"],
            "post_kill_completed_step_count": after_kill["completed_step_count"],
            "final_completed_step_count": final_snapshot["completed_step_count"],
            "final_status": None if final_workflow is None else final_workflow["status"],
            "task_start_counts": counts,
            "result_matches_uninterrupted": final_result_record["result"] == baseline_result,
            "baseline_result": baseline_result,
            "recovered_result": final_result_record["result"],
            "definition_sha256": definition_hash,
            "input_sha256": input_hash,
            "final_operations": final_snapshot["operations"],
            "task_events": final_events,
        }
        _write_json(
            run_dir / "heartbeat-evidence.json",
            {
                "worker_pid": initial_worker_pid,
                "updates_before_kill": len(pre_kill_heartbeats),
                "updates_at_kill": len(heartbeats_at_kill),
                "updates_after_quiet_period": len(heartbeat_events_after_kill),
                "quiet_seconds": heartbeat_quiet_seconds,
                "stopped": heartbeat_stopped,
            },
        )
        passed = (
            evidence["same_workflow_id"]
            and evidence["workflow_count"] == 1
            and evidence["final_status"] == "SUCCESS"
            and evidence["initial_completed_step_count"] == 1
            and evidence["post_kill_completed_step_count"] == 1
            and evidence["final_completed_step_count"] == 3
            and evidence["task_start_counts"] == {"first": 1, "second": 2, "finish": 1}
            and evidence["result_matches_uninterrupted"]
        )
        evidence["result"] = "PASS" if passed else "FAIL"
        _write_json(run_dir / "final-evidence.json", evidence)
        if not passed:
            raise RuntimeError(f"recovery evidence did not meet expectations: {evidence!r}")
        return evidence
    finally:
        _stop_owned_process(initial_process)
        _stop_owned_process(recovery_process)
        if not initial_stdout.closed:
            initial_stdout.close()
        if not initial_stderr.closed:
            initial_stderr.close()
        if recovery_stdout is not None and not recovery_stdout.closed:
            recovery_stdout.close()
        if recovery_stderr is not None and not recovery_stderr.closed:
            recovery_stderr.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OWS DBOS process recovery experiment")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        evidence = run_experiment(args.state_dir.resolve(), args.timeout_seconds)
    except Exception as error:
        print(json.dumps({"result": "FAIL", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
