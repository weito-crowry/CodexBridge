from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from recovery_config import APPLICATION_NAME, APPLICATION_VERSION, EXECUTOR_ID

WORKER_SCRIPT = Path(__file__).with_name("recovery_worker.py")
PHASE_TIMEOUT_SECONDS = 30.0
STEP2_TIMEOUT_SECONDS = 120.0
CHILD_REAP_TIMEOUT_SECONDS = 5.0
HEARTBEAT_QUIET_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.05


def _read_json_lines(path: Path, *, ignore_incomplete_tail: bool = False) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    if ignore_incomplete_tail and contents and not contents.endswith("\n"):
        lines = lines[:-1]
    for line in lines:
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
        "completed_steps": [
            operation["function_name"]
            for operation in operations
            if operation["completed_at_epoch_ms"] is not None
        ],
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        json.dump(value, output_file, indent=2, sort_keys=True)
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _child_command(
    *,
    mode: str,
    database_path: Path,
    event_log: Path,
    heartbeat_log: Path,
    continue_marker: Path,
    startup_metadata: Path,
    phase_timeout_seconds: float,
    workflow_id: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--mode",
        mode,
        "--database",
        str(database_path),
        "--event-log",
        str(event_log),
        "--heartbeat-log",
        str(heartbeat_log),
        "--continue-marker",
        str(continue_marker),
        "--startup-metadata",
        str(startup_metadata),
        "--step2-timeout-seconds",
        str(STEP2_TIMEOUT_SECONDS),
        "--completion-timeout-seconds",
        str(phase_timeout_seconds),
    ]
    if workflow_id is not None:
        command.extend(("--workflow-id", workflow_id))
    return command


def _process_error(process: subprocess.Popen[bytes], stdout_path: Path, stderr_path: Path) -> str:
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    return (
        f"child pid={process.pid} exit={process.poll()}\n"
        f"stdout ({stdout_path}):\n{stdout}\n"
        f"stderr ({stderr_path}):\n{stderr}"
    )


def _wait_for_pre_recovery_state(
    process: subprocess.Popen[bytes],
    worker_pid: int,
    database_path: Path,
    event_log: Path,
    heartbeat_log: Path,
    workflow_id: str,
    deadline: float,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    last_database_error: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "initial worker exited before reaching the interrupted step\n"
                + _process_error(process, stdout_path, stderr_path)
            )
        events = _read_json_lines(event_log)
        heartbeats = _read_json_lines(heartbeat_log, ignore_incomplete_tail=True)
        try:
            snapshot = _database_snapshot(database_path, workflow_id)
        except sqlite3.OperationalError as error:
            last_database_error = str(error)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        event_pairs = [(event["step"], event["pid"]) for event in events]
        if event_pairs[:2] == [("step1", worker_pid), ("step2", worker_pid)]:
            workflow = snapshot["workflow"]
            completed_steps = snapshot["completed_steps"]
            step2_incomplete = not any(
                operation["function_name"] == "step2"
                and operation["completed_at_epoch_ms"] is not None
                for operation in snapshot["operations"]
            )
            heartbeat_ready = len(heartbeats) >= 2 and all(
                heartbeat["pid"] == worker_pid for heartbeat in heartbeats
            )
            if (
                heartbeat_ready
                and snapshot["workflow_count"] == 1
                and workflow is not None
                and workflow["status"] == "PENDING"
                and completed_steps == ["step1"]
                and step2_incomplete
            ):
                return snapshot, events
            if workflow is not None and workflow["status"] not in ("PENDING", "SUCCESS"):
                raise RuntimeError(f"unexpected workflow state before kill: {snapshot!r}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        "initial worker did not reach step2 with only step1 checkpointed; "
        f"last_database_error={last_database_error!r}, events={_read_json_lines(event_log)!r}"
    )


def _validate_interrupted_snapshot(snapshot: dict[str, Any]) -> None:
    workflow = snapshot["workflow"]
    if (
        snapshot["workflow_count"] != 1
        or workflow is None
        or workflow["status"] != "PENDING"
        or snapshot["completed_steps"] != ["step1"]
    ):
        raise RuntimeError(f"expected one PENDING workflow with only step1 complete: {snapshot!r}")
    step2_complete = any(
        operation["function_name"] == "step2" and operation["completed_at_epoch_ms"] is not None
        for operation in snapshot["operations"]
    )
    if step2_complete:
        raise RuntimeError(f"step2 unexpectedly has a completion checkpoint: {snapshot!r}")


def _heartbeat_signature(path: Path) -> tuple[int, int, int]:
    heartbeats = _read_json_lines(path, ignore_incomplete_tail=True)
    stat = path.stat()
    return (len(heartbeats), stat.st_size, stat.st_mtime_ns)


def _verify_worker_stopped(
    process: subprocess.Popen[bytes], heartbeat_log: Path, worker_pid: int
) -> None:
    if process.poll() is None:
        raise RuntimeError("Popen.kill() did not terminate the initial worker")
    first_signature = _heartbeat_signature(heartbeat_log)
    heartbeats = _read_json_lines(heartbeat_log, ignore_incomplete_tail=True)
    if not heartbeats or any(heartbeat["pid"] != worker_pid for heartbeat in heartbeats):
        raise RuntimeError(f"worker heartbeat PID mismatch: {heartbeats!r}")
    time.sleep(HEARTBEAT_QUIET_SECONDS)
    second_signature = _heartbeat_signature(heartbeat_log)
    if first_signature != second_signature:
        raise RuntimeError(
            "initial worker heartbeat continued after Popen.kill(); "
            f"before={first_signature!r}, after={second_signature!r}"
        )


def _stop_owned_process(process: subprocess.Popen[bytes] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.kill()
    try:
        return process.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)


def _validate_final_evidence(
    snapshot: dict[str, Any], events: list[dict[str, Any]], initial_pid: int, recovery_pid: int
) -> None:
    workflow = snapshot["workflow"]
    if snapshot["workflow_count"] != 1 or workflow is None:
        raise RuntimeError(f"expected one workflow row, got {snapshot!r}")
    if workflow["status"] != "SUCCESS":
        raise RuntimeError(f"workflow did not reach SUCCESS: {snapshot!r}")
    if (
        workflow["executor_id"] != EXECUTOR_ID
        or workflow["application_version"] != APPLICATION_VERSION
        or workflow["application_name"] != APPLICATION_NAME
    ):
        raise RuntimeError(f"workflow recovery identity changed: {workflow!r}")
    operation_names = [operation["function_name"] for operation in snapshot["operations"]]
    if operation_names != ["step1", "step2", "step3"]:
        raise RuntimeError(f"expected three completed DBOS operations, got {snapshot!r}")
    if any(operation["completed_at_epoch_ms"] is None for operation in snapshot["operations"]):
        raise RuntimeError(f"found an operation without a completion checkpoint: {snapshot!r}")
    event_pairs = [(event["step"], event["pid"]) for event in events]
    expected_events = [
        ("step1", initial_pid),
        ("step2", initial_pid),
        ("step2", recovery_pid),
        ("step3", recovery_pid),
    ]
    if event_pairs != expected_events:
        raise RuntimeError(f"unexpected step-start event sequence: {event_pairs!r}")


def run_recovery(
    state_root: Path, phase_timeout_seconds: float = PHASE_TIMEOUT_SECONDS
) -> dict[str, Any]:
    if phase_timeout_seconds <= 0:
        raise ValueError("phase timeout must be positive")
    state_root = state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    run_dir = state_root / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir()

    database_path = run_dir / "dbos-recovery.sqlite"
    event_log = run_dir / "step-events.jsonl"
    heartbeat_log = run_dir / "initial-worker-heartbeat.jsonl"
    continue_marker = run_dir / "continue-step2.marker"
    initial_metadata_path = run_dir / "initial-worker.json"
    recovery_metadata_path = run_dir / "recovery-worker.json"
    initial_stdout_path = run_dir / "initial.stdout.log"
    initial_stderr_path = run_dir / "initial.stderr.log"
    recovery_stdout_path = run_dir / "recovery.stdout.log"
    recovery_stderr_path = run_dir / "recovery.stderr.log"

    initial_process: subprocess.Popen[bytes] | None = None
    recovery_process: subprocess.Popen[bytes] | None = None
    initial_stdout_file = initial_stdout_path.open("wb")
    initial_stderr_file = initial_stderr_path.open("wb")
    recovery_stdout_file = recovery_stdout_path.open("wb")
    recovery_stderr_file = recovery_stderr_path.open("wb")
    initial_killed_by_parent = False
    initial_return_code: int | None = None
    workflow_id: str | None = None
    initial_popen_pid: int | None = None
    initial_pid: int | None = None
    recovery_popen_pid: int | None = None
    recovery_pid: int | None = None
    pre_kill_snapshot: dict[str, Any] | None = None
    pre_recovery_snapshot: dict[str, Any] | None = None

    try:
        initial_process = subprocess.Popen(
            _child_command(
                mode="start",
                database_path=database_path,
                event_log=event_log,
                heartbeat_log=heartbeat_log,
                continue_marker=continue_marker,
                startup_metadata=initial_metadata_path,
                phase_timeout_seconds=phase_timeout_seconds,
            ),
            cwd=WORKER_SCRIPT.parent,
            stdout=initial_stdout_file,
            stderr=initial_stderr_file,
        )
        initial_popen_pid = initial_process.pid
        initial_deadline = time.monotonic() + phase_timeout_seconds
        metadata: dict[str, Any] | None = None
        while time.monotonic() < initial_deadline:
            metadata = _read_json(initial_metadata_path)
            if metadata is not None:
                break
            if initial_process.poll() is not None:
                raise RuntimeError(
                    "initial worker exited before startup metadata was written\n"
                    + _process_error(initial_process, initial_stdout_path, initial_stderr_path)
                )
            time.sleep(POLL_INTERVAL_SECONDS)
        if metadata is None:
            raise TimeoutError("initial worker did not publish startup metadata")
        if metadata.get("mode") != "start" or not isinstance(metadata.get("pid"), int):
            raise RuntimeError(f"initial worker metadata is invalid: {metadata!r}")
        initial_pid = metadata["pid"]
        workflow_id = str(metadata.get("workflow_id", ""))
        if not workflow_id:
            raise RuntimeError(f"initial worker did not provide a workflow ID: {metadata!r}")

        pre_kill_snapshot, _ = _wait_for_pre_recovery_state(
            initial_process,
            initial_pid,
            database_path,
            event_log,
            heartbeat_log,
            workflow_id,
            initial_deadline,
            initial_stdout_path,
            initial_stderr_path,
        )
        if continue_marker.exists():
            raise RuntimeError("continue marker unexpectedly exists before the first kill")
        if initial_process.poll() is not None:
            raise RuntimeError("initial worker exited before the parent could kill it")
        _write_json(run_dir / "before-kill.json", pre_kill_snapshot)

        initial_process.kill()
        initial_return_code = initial_process.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)
        initial_killed_by_parent = True
        if initial_return_code is None:
            raise RuntimeError("initial worker was not reaped after Popen.kill()")
        _verify_worker_stopped(initial_process, heartbeat_log, initial_pid)
        events_before_recovery = _read_json_lines(event_log)
        _validate_interrupted_snapshot(pre_kill_snapshot)
        pre_recovery_snapshot = _database_snapshot(database_path, workflow_id)
        _validate_interrupted_snapshot(pre_recovery_snapshot)
        if [(event["step"], event["pid"]) for event in events_before_recovery] != [
            ("step1", initial_pid),
            ("step2", initial_pid),
        ]:
            raise RuntimeError(
                "unexpected step events between initial kill and recovery start: "
                f"{events_before_recovery!r}"
            )
        _write_json(run_dir / "before-recovery.json", pre_recovery_snapshot)

        with continue_marker.open("w", encoding="utf-8", newline="\n") as marker_file:
            marker_file.write("continue\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())

        recovery_process = subprocess.Popen(
            _child_command(
                mode="recover",
                database_path=database_path,
                event_log=event_log,
                heartbeat_log=heartbeat_log,
                continue_marker=continue_marker,
                startup_metadata=recovery_metadata_path,
                phase_timeout_seconds=phase_timeout_seconds,
                workflow_id=workflow_id,
            ),
            cwd=WORKER_SCRIPT.parent,
            stdout=recovery_stdout_file,
            stderr=recovery_stderr_file,
        )
        recovery_popen_pid = recovery_process.pid
        try:
            recovery_return_code = recovery_process.wait(timeout=phase_timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(
                f"recovery worker exceeded {phase_timeout_seconds:.1f}s; pid={recovery_process.pid}"
            ) from error
        if recovery_return_code != 0:
            raise RuntimeError(
                "recovery-only worker failed\n"
                + _process_error(recovery_process, recovery_stdout_path, recovery_stderr_path)
            )

        recovery_metadata = _read_json(recovery_metadata_path)
        if (
            recovery_metadata is None
            or recovery_metadata.get("mode") != "recover"
            or not isinstance(recovery_metadata.get("pid"), int)
            or recovery_metadata.get("workflow_id") != workflow_id
        ):
            raise RuntimeError(f"recovery worker metadata mismatch: {recovery_metadata!r}")
        recovery_pid = recovery_metadata["pid"]
        if recovery_pid == initial_pid or recovery_popen_pid == initial_popen_pid:
            raise RuntimeError("recovery worker did not run under a different child PID")

        final_snapshot = _database_snapshot(database_path, workflow_id)
        final_events = _read_json_lines(event_log)
        _validate_final_evidence(final_snapshot, final_events, initial_pid, recovery_pid)
        _write_json(run_dir / "after-recovery.json", final_snapshot)
        summary = {
            "status": "PASS",
            "workflow_id": workflow_id,
            "initial_pid": initial_pid,
            "initial_popen_pid": initial_popen_pid,
            "initial_killed_by_parent": initial_killed_by_parent,
            "initial_worker_exited": initial_process.poll() is not None,
            "heartbeat_stopped_after_kill": True,
            "initial_return_code_after_kill": initial_return_code,
            "recovery_pid": recovery_pid,
            "recovery_popen_pid": recovery_popen_pid,
            "parent_popen_vs_worker_python_pid_mismatch": (
                initial_pid != initial_popen_pid or recovery_pid != recovery_popen_pid
            ),
            "executor_id": EXECUTOR_ID,
            "application_version": APPLICATION_VERSION,
            "application_name": APPLICATION_NAME,
            "workflow_count_before_recovery": pre_recovery_snapshot["workflow_count"],
            "status_before_recovery": pre_recovery_snapshot["workflow"]["status"],
            "completed_steps_before_recovery": pre_recovery_snapshot["completed_steps"],
            "step2_completed_before_recovery": any(
                operation["function_name"] == "step2"
                and operation["completed_at_epoch_ms"] is not None
                for operation in pre_recovery_snapshot["operations"]
            ),
            "events_before_recovery": len(events_before_recovery),
            "workflow_count_after_recovery": final_snapshot["workflow_count"],
            "status_after_recovery": final_snapshot["workflow"]["status"],
            "completed_operation_count": len(final_snapshot["operations"]),
            "event_counts": {
                name: sum(event["step"] == name for event in final_events)
                for name in ("step1", "step2", "step3")
            },
            "database": str(database_path),
            "event_log": str(event_log),
            "heartbeat_log": str(heartbeat_log),
            "run_dir": str(run_dir),
        }
        _write_json(run_dir / "result.json", summary)
        return summary
    except Exception as error:
        failure: dict[str, Any] = {
            "error": str(error),
            "traceback": traceback.format_exc(),
            "workflow_id": workflow_id,
            "initial_pid": initial_pid,
            "initial_popen_pid": initial_popen_pid,
            "recovery_pid": recovery_pid,
            "recovery_popen_pid": recovery_popen_pid,
            "initial_killed_by_parent": initial_killed_by_parent,
            "pre_kill_snapshot": pre_kill_snapshot,
            "pre_recovery_snapshot": pre_recovery_snapshot,
            "events": _read_json_lines(event_log),
            "database": str(database_path),
            "event_log": str(event_log),
            "heartbeat_log": str(heartbeat_log),
            "initial_stdout": str(initial_stdout_path),
            "initial_stderr": str(initial_stderr_path),
            "recovery_stdout": str(recovery_stdout_path),
            "recovery_stderr": str(recovery_stderr_path),
        }
        if workflow_id is not None and database_path.is_file():
            try:
                failure["database_snapshot"] = _database_snapshot(database_path, workflow_id)
            except sqlite3.Error as database_error:
                failure["database_snapshot_error"] = str(database_error)
        _write_json(run_dir / "failure.json", failure)
        raise RuntimeError(f"recovery experiment failed; evidence: {run_dir}; {error}") from error
    finally:
        _stop_owned_process(recovery_process)
        _stop_owned_process(initial_process)
        initial_stdout_file.close()
        initial_stderr_file.close()
        recovery_stdout_file.close()
        recovery_stderr_file.close()


def main() -> int:
    default_state_root = Path(__file__).resolve().parent / ".state" / "recovery-runs"
    parser = argparse.ArgumentParser(description="Run the DBOS kill-and-recovery experiment.")
    parser.add_argument("--state-dir", type=Path, default=default_state_root)
    parser.add_argument("--phase-timeout-seconds", type=float, default=PHASE_TIMEOUT_SECONDS)
    args = parser.parse_args()

    try:
        summary = run_recovery(args.state_dir, args.phase_timeout_seconds)
    except Exception as error:
        print(f"recovery experiment failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
