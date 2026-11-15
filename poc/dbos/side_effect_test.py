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

from side_effect_service import (
    EFFECT_NAME,
    EFFECT_VALUE,
    LOGICAL_OPERATION,
    initialize_adapter_database,
    initialize_external_database,
)
from side_effect_worker import (
    APPLICATION_NAME,
    APPLICATION_VERSION,
    EXECUTOR_ID,
    STEP_NAME,
)

WORKER_SCRIPT = Path(__file__).with_name("side_effect_worker.py")
POLL_INTERVAL_SECONDS = 0.05
CHILD_REAP_TIMEOUT_SECONDS = 5.0
HEARTBEAT_STABLE_SECONDS = 0.35
CASE_NAMES = ("idempotent_recovery", "transient_retry", "unqueryable_unknown")


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


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    contents = path.read_text(encoding="utf-8")
    lines = contents.splitlines()
    if contents and not contents.endswith("\n"):
        lines = lines[:-1]
    return [json.loads(line) for line in lines]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)


def _workflow_snapshot(database_path: Path) -> dict[str, Any]:
    with _read_only_connection(database_path) as connection:
        workflows = connection.execute(
            "SELECT workflow_uuid, status, executor_id, application_version, application_name "
            "FROM workflow_status ORDER BY workflow_uuid"
        ).fetchall()
        operations = connection.execute(
            "SELECT workflow_uuid, function_name, output, error, completed_at_epoch_ms "
            "FROM operation_outputs ORDER BY workflow_uuid, function_id"
        ).fetchall()
    return {
        "workflow_count": len(workflows),
        "workflows": [
            {
                "workflow_id": row[0],
                "status": row[1],
                "executor_id": row[2],
                "application_version": row[3],
                "application_name": row[4],
            }
            for row in workflows
        ],
        "operations": [
            {
                "workflow_id": row[0],
                "function_name": row[1],
                "output_present": row[2] is not None,
                "error_present": row[3] is not None,
                "completed_at_epoch_ms": row[4],
            }
            for row in operations
        ],
    }


def _external_snapshot(database_path: Path) -> dict[str, Any]:
    with _read_only_connection(database_path) as connection:
        capabilities = connection.execute(
            "SELECT supports_lookup, supports_idempotency FROM service_capabilities "
            "WHERE singleton = 1"
        ).fetchone()
        totals = connection.execute(
            "SELECT total_effect_count, total_effect_value FROM service_totals WHERE singleton = 1"
        ).fetchone()
        effects = connection.execute(
            "SELECT request_key, workflow_id, logical_operation, effect_name, effect_value "
            "FROM effect_ledger ORDER BY effect_id"
        ).fetchall()
        calls = connection.execute(
            "SELECT operation, outcome, request_key, error_kind FROM service_calls ORDER BY id"
        ).fetchall()
    return {
        "capabilities": None if capabilities is None else list(capabilities),
        "totals": None if totals is None else list(totals),
        "effects": [list(row) for row in effects],
        "calls": [list(row) for row in calls],
    }


def _adapter_snapshot(database_path: Path | None) -> list[list[object]]:
    if database_path is None or not database_path.is_file():
        return []
    with _read_only_connection(database_path) as connection:
        rows = connection.execute(
            "SELECT request_key, workflow_id, status, receipt, reason "
            "FROM adapter_state ORDER BY request_key"
        ).fetchall()
    return [list(row) for row in rows]


def _child_command(
    *,
    case_name: str,
    mode: str,
    database_path: Path,
    external_database_path: Path,
    adapter_database_path: Path,
    event_log_path: Path,
    heartbeat_log_path: Path,
    continue_marker_path: Path,
    metadata_path: Path,
    expected_status: str,
    completion_timeout_seconds: float,
    barrier_timeout_seconds: float,
    workflow_id: str | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--case",
        case_name,
        "--mode",
        mode,
        "--database",
        str(database_path),
        "--external-database",
        str(external_database_path),
        "--adapter-database",
        str(adapter_database_path),
        "--event-log",
        str(event_log_path),
        "--heartbeat-log",
        str(heartbeat_log_path),
        "--continue-marker",
        str(continue_marker_path),
        "--startup-metadata",
        str(metadata_path),
        "--expected-status",
        expected_status,
        "--completion-timeout-seconds",
        str(completion_timeout_seconds),
        "--barrier-timeout-seconds",
        str(barrier_timeout_seconds),
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


def _wait_for_metadata(
    process: subprocess.Popen[bytes],
    metadata_path: Path,
    deadline: float,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        metadata = _read_json(metadata_path)
        if metadata is not None:
            return metadata
        if process.poll() is not None:
            raise RuntimeError(
                "worker exited before publishing metadata\n"
                + _process_error(process, stdout_path, stderr_path)
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError("worker did not publish startup metadata before timeout")


def _heartbeat_signature(path: Path) -> tuple[int, int, str] | None:
    try:
        stat = path.stat()
        last_line = path.read_text(encoding="utf-8").splitlines()[-1]
    except (FileNotFoundError, IndexError):
        return None
    return stat.st_size, stat.st_mtime_ns, last_line


def _verify_worker_stopped(
    process: subprocess.Popen[bytes], heartbeat_path: Path, worker_pid: int
) -> bool:
    if process.poll() is None:
        raise RuntimeError("parent-owned Popen child is still running after kill")
    first_signature = _heartbeat_signature(heartbeat_path)
    heartbeats = _read_json_lines(heartbeat_path)
    if not heartbeats or any(record.get("pid") != worker_pid for record in heartbeats):
        raise RuntimeError(f"initial worker heartbeat evidence is inconsistent: {heartbeats!r}")
    time.sleep(HEARTBEAT_STABLE_SECONDS)
    second_signature = _heartbeat_signature(heartbeat_path)
    if first_signature != second_signature:
        raise RuntimeError("initial heartbeat continued after its Popen child exited")
    return True


def _write_continue_marker(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as marker_file:
        marker_file.write("continue\n")
        marker_file.flush()
        os.fsync(marker_file.fileno())


def _wait_for_interrupted_state(
    *,
    case_name: str,
    process: subprocess.Popen[bytes],
    workflow_id: str,
    database_path: Path,
    external_database_path: Path,
    adapter_database_path: Path | None,
    event_log_path: Path,
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any], list[list[object]]]:
    request_key = f"{workflow_id}:{LOGICAL_OPERATION}"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"initial worker exited before the parent kill point; exit={process.returncode}"
            )
        try:
            workflow_snapshot = _workflow_snapshot(database_path)
            external_snapshot = _external_snapshot(external_database_path)
            adapter_rows = _adapter_snapshot(adapter_database_path)
        except sqlite3.Error:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        events = _read_json_lines(event_log_path)
        step_entries = [event for event in events if event.get("event") == "step_entered"]
        has_effect = external_snapshot["effects"] == [
            [request_key, workflow_id, LOGICAL_OPERATION, EFFECT_NAME, EFFECT_VALUE]
        ]
        operation_checkpointed = any(
            row["workflow_id"] == workflow_id
            and row["function_name"] == STEP_NAME
            and (row["output_present"] or row["error_present"])
            for row in workflow_snapshot["operations"]
        )
        adapter_ready = case_name != "unqueryable_unknown" or adapter_rows == [
            [request_key, workflow_id, "DISPATCH_INTENT", None, None]
        ]
        if (
            workflow_snapshot["workflow_count"] == 1
            and workflow_snapshot["workflows"][0]["workflow_id"] == workflow_id
            and workflow_snapshot["workflows"][0]["status"] == "PENDING"
            and len(step_entries) == 1
            and has_effect
            and not operation_checkpointed
            and adapter_ready
        ):
            return workflow_snapshot, external_snapshot, adapter_rows
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        "worker did not reach the external-effect-committed, DBOS-step-uncheckpointed state"
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


def _start_child(
    command: list[str],
    stdout_path: Path,
    stderr_path: Path,
    log_handles: list[Any],
) -> subprocess.Popen[bytes]:
    stdout_file = stdout_path.open("wb")
    stderr_file = stderr_path.open("wb")
    log_handles.extend((stdout_file, stderr_file))
    return subprocess.Popen(
        command,
        cwd=WORKER_SCRIPT.parent,
        stdin=subprocess.DEVNULL,
        stdout=stdout_file,
        stderr=stderr_file,
    )


def _validate_final_evidence(
    *,
    case_name: str,
    expected_status: str,
    workflow_id: str,
    database_path: Path,
    external_database_path: Path,
    adapter_database_path: Path | None,
    event_log_path: Path,
) -> dict[str, Any]:
    workflow_snapshot = _workflow_snapshot(database_path)
    external_snapshot = _external_snapshot(external_database_path)
    adapter_rows = _adapter_snapshot(adapter_database_path)
    request_key = f"{workflow_id}:{LOGICAL_OPERATION}"
    if workflow_snapshot["workflow_count"] != 1 or workflow_snapshot["workflows"] != [
        {
            "workflow_id": workflow_id,
            "status": expected_status,
            "executor_id": EXECUTOR_ID,
            "application_version": APPLICATION_VERSION,
            "application_name": APPLICATION_NAME,
        }
    ]:
        raise RuntimeError(f"unexpected DBOS workflow readback: {workflow_snapshot!r}")
    if external_snapshot["effects"] != [
        [request_key, workflow_id, LOGICAL_OPERATION, EFFECT_NAME, EFFECT_VALUE]
    ]:
        raise RuntimeError(f"unexpected external effect ledger: {external_snapshot!r}")
    if external_snapshot["totals"] != [1, EFFECT_VALUE]:
        raise RuntimeError(f"unexpected external effect counter: {external_snapshot!r}")
    expected_capabilities = [1, 1] if case_name == "idempotent_recovery" else [0, 0]
    if external_snapshot["capabilities"] != expected_capabilities:
        raise RuntimeError(f"unexpected fake service capabilities: {external_snapshot!r}")

    events = _read_json_lines(event_log_path)
    step_entries = [event for event in events if event.get("event") == "step_entered"]
    retry_errors = [event for event in events if event.get("event") == "retry_error"]
    if any(event.get("workflow_id") != workflow_id for event in step_entries):
        raise RuntimeError(f"step entries crossed workflow identity: {step_entries!r}")

    completed_steps = {
        row["function_name"]
        for row in workflow_snapshot["operations"]
        if row["workflow_id"] == workflow_id
        and row["output_present"]
        and not row["error_present"]
        and row["completed_at_epoch_ms"] is not None
    }
    if case_name == "unqueryable_unknown":
        failed_step_rows = [
            row
            for row in workflow_snapshot["operations"]
            if row["workflow_id"] == workflow_id and row["function_name"] == STEP_NAME
        ]
        if (
            len(failed_step_rows) != 1
            or failed_step_rows[0]["output_present"]
            or not failed_step_rows[0]["error_present"]
            or failed_step_rows[0]["completed_at_epoch_ms"] is None
        ):
            raise RuntimeError(
                "DBOS did not preserve the recovered UNKNOWN step as an ERROR checkpoint: "
                f"{workflow_snapshot!r}"
            )
        expected_calls = [["send_unqueryable", "APPLIED", request_key, None]]
        unknown_events = [event for event in events if event.get("event") == "unknown_rejected"]
        if len(unknown_events) != 1 or unknown_events[0].get("send_attempted") is not False:
            raise RuntimeError(f"UNKNOWN rejection evidence is missing or unsafe: {events!r}")
        if unknown_events[0].get("service_query_attempted") is not False:
            raise RuntimeError("UNKNOWN recovery queried the unqueryable external service")
        if adapter_rows != [
            [
                request_key,
                workflow_id,
                "UNKNOWN",
                None,
                "dispatch intent exists without a local receipt; no retry",
            ]
        ]:
            raise RuntimeError(f"UNKNOWN adapter state was not persisted: {adapter_rows!r}")
    elif case_name == "idempotent_recovery":
        if completed_steps != {STEP_NAME}:
            raise RuntimeError(
                f"idempotent step checkpoint did not complete: {workflow_snapshot!r}"
            )
        expected_calls = [
            ["lookup", "MISS", request_key, None],
            ["send_idempotent", "APPLIED", request_key, None],
            ["lookup", "HIT", request_key, None],
        ]
    else:
        if completed_steps != {STEP_NAME}:
            raise RuntimeError(f"retry step checkpoint did not complete: {workflow_snapshot!r}")
        expected_calls = [
            ["send_transient", "TRANSIENT_ERROR", request_key, "TransientServiceError"],
            ["send_transient", "TRANSIENT_ERROR", request_key, "TransientServiceError"],
            ["send_transient", "APPLIED", request_key, None],
        ]
        if len(retry_errors) != 2 or any(
            event.get("error_kind") != "TransientServiceError" for event in retry_errors
        ):
            raise RuntimeError(f"bounded retry evidence is incorrect: {events!r}")
    if external_snapshot["calls"] != expected_calls:
        raise RuntimeError(f"unexpected fake service call history: {external_snapshot!r}")

    return {
        "workflow_snapshot": workflow_snapshot,
        "external_snapshot": external_snapshot,
        "adapter_rows": adapter_rows,
        "events": events,
        "step_entries": len(step_entries),
        "retry_errors": [event.get("error_kind") for event in retry_errors],
        "unknown_event": (unknown_events[0] if case_name == "unqueryable_unknown" else None),
    }


def run_case(
    case_name: str,
    state_root: Path,
    *,
    phase_timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    if case_name not in CASE_NAMES:
        raise ValueError(f"unsupported case: {case_name}")
    if phase_timeout_seconds <= 0:
        raise ValueError("phase timeout must be positive")
    state_root = state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    run_dir = state_root / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir()

    database_path = run_dir / "workflow.sqlite"
    external_database_path = run_dir / "fake-external.sqlite"
    adapter_database_path = run_dir / "adapter-state.sqlite"
    event_log_path = run_dir / "worker-events.jsonl"
    initial_heartbeat_path = run_dir / "initial-heartbeat.jsonl"
    recovery_heartbeat_path = run_dir / "recovery-heartbeat.jsonl"
    continue_marker_path = run_dir / "continue.marker"
    initial_metadata_path = run_dir / "initial-worker.json"
    recovery_metadata_path = run_dir / "recovery-worker.json"
    initial_stdout_path = run_dir / "initial.stdout.log"
    initial_stderr_path = run_dir / "initial.stderr.log"
    recovery_stdout_path = run_dir / "recovery.stdout.log"
    recovery_stderr_path = run_dir / "recovery.stderr.log"

    initialize_external_database(
        external_database_path,
        case_name=case_name,
        transient_failures=2 if case_name == "transient_retry" else 0,
    )
    if case_name == "unqueryable_unknown":
        initialize_adapter_database(adapter_database_path)
    adapter_path_for_worker = adapter_database_path
    adapter_path_for_result = adapter_database_path if case_name == "unqueryable_unknown" else None

    initial_process: subprocess.Popen[bytes] | None = None
    recovery_process: subprocess.Popen[bytes] | None = None
    log_handles: list[Any] = []
    initial_killed_by_parent = False
    heartbeat_stopped_after_kill = False
    initial_pid: int | None = None
    recovery_pid: int | None = None
    initial_popen_pid: int | None = None
    recovery_popen_pid: int | None = None
    workflow_id: str | None = None
    status_before_recovery: str | None = None
    checkpoint_complete_before_recovery: bool | None = None
    workflow_id_before_recovery: str | None = None
    workflow_started_in_recovery: bool | None = None
    try:
        initial_command = _child_command(
            case_name=case_name,
            mode="start",
            database_path=database_path,
            external_database_path=external_database_path,
            adapter_database_path=adapter_path_for_worker,
            event_log_path=event_log_path,
            heartbeat_log_path=initial_heartbeat_path,
            continue_marker_path=continue_marker_path,
            metadata_path=initial_metadata_path,
            expected_status="SUCCESS",
            completion_timeout_seconds=phase_timeout_seconds,
            barrier_timeout_seconds=max(phase_timeout_seconds * 3.0, 60.0),
        )
        initial_process = _start_child(
            initial_command, initial_stdout_path, initial_stderr_path, log_handles
        )
        initial_popen_pid = initial_process.pid
        initial_deadline = time.monotonic() + phase_timeout_seconds
        initial_metadata = _wait_for_metadata(
            initial_process,
            initial_metadata_path,
            initial_deadline,
            initial_stdout_path,
            initial_stderr_path,
        )
        if initial_metadata.get("mode") != "start" or not isinstance(
            initial_metadata.get("pid"), int
        ):
            raise RuntimeError(f"initial worker metadata is invalid: {initial_metadata!r}")
        initial_pid = int(initial_metadata["pid"])
        workflow_id = str(initial_metadata.get("workflow_id", ""))
        if not workflow_id or initial_metadata.get("workflow_started_here") is not True:
            raise RuntimeError(
                f"initial worker did not start exactly one workflow: {initial_metadata!r}"
            )

        if case_name == "transient_retry":
            try:
                initial_process.wait(timeout=phase_timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("transient retry workflow exceeded the phase timeout") from error
            if initial_process.returncode != 0:
                raise RuntimeError(
                    "transient retry worker failed\n"
                    + _process_error(initial_process, initial_stdout_path, initial_stderr_path)
                )
        else:
            before_kill, before_kill_external, before_kill_adapter = _wait_for_interrupted_state(
                case_name=case_name,
                process=initial_process,
                workflow_id=workflow_id,
                database_path=database_path,
                external_database_path=external_database_path,
                adapter_database_path=adapter_path_for_result,
                event_log_path=event_log_path,
                deadline=initial_deadline,
            )
            status_before_recovery = before_kill["workflows"][0]["status"]
            workflow_id_before_recovery = before_kill["workflows"][0]["workflow_id"]
            checkpoint_complete_before_recovery = any(
                row["workflow_id"] == workflow_id
                and row["function_name"] == STEP_NAME
                and row["completed_at_epoch_ms"] is not None
                for row in before_kill["operations"]
            )
            if status_before_recovery != "PENDING" or checkpoint_complete_before_recovery:
                raise RuntimeError(
                    "kill point did not have a pending workflow and incomplete step checkpoint"
                )
            _write_json(
                run_dir / "before-kill.json",
                {
                    "workflow": before_kill,
                    "external": before_kill_external,
                    "adapter": before_kill_adapter,
                    "worker_events": _read_json_lines(event_log_path),
                },
            )
            if continue_marker_path.exists():
                raise RuntimeError("continue marker existed before the initial process kill")
            if initial_process.poll() is not None:
                raise RuntimeError("initial worker exited before the parent kill point")
            initial_process.kill()
            initial_process.wait(timeout=CHILD_REAP_TIMEOUT_SECONDS)
            initial_killed_by_parent = True
            heartbeat_stopped_after_kill = _verify_worker_stopped(
                initial_process, initial_heartbeat_path, initial_pid
            )
            after_kill = _workflow_snapshot(database_path)
            if after_kill != before_kill:
                raise RuntimeError(f"workflow checkpoint state changed after kill: {after_kill!r}")
            if _external_snapshot(external_database_path) != before_kill_external:
                raise RuntimeError("external service state changed after initial worker stopped")
            if _adapter_snapshot(adapter_path_for_result) != before_kill_adapter:
                raise RuntimeError("adapter state changed after initial worker stopped")
            _write_json(run_dir / "before-recovery.json", after_kill)

            _write_continue_marker(continue_marker_path)
            recovery_expected_status = "ERROR" if case_name == "unqueryable_unknown" else "SUCCESS"
            recovery_command = _child_command(
                case_name=case_name,
                mode="recover",
                database_path=database_path,
                external_database_path=external_database_path,
                adapter_database_path=adapter_path_for_worker,
                event_log_path=event_log_path,
                heartbeat_log_path=recovery_heartbeat_path,
                continue_marker_path=continue_marker_path,
                metadata_path=recovery_metadata_path,
                expected_status=recovery_expected_status,
                completion_timeout_seconds=phase_timeout_seconds,
                barrier_timeout_seconds=max(phase_timeout_seconds * 3.0, 60.0),
                workflow_id=workflow_id,
            )
            recovery_process = _start_child(
                recovery_command,
                recovery_stdout_path,
                recovery_stderr_path,
                log_handles,
            )
            recovery_popen_pid = recovery_process.pid
            try:
                recovery_process.wait(timeout=phase_timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("recovery worker exceeded the phase timeout") from error
            if recovery_process.returncode != 0:
                raise RuntimeError(
                    "recovery-only worker failed\n"
                    + _process_error(recovery_process, recovery_stdout_path, recovery_stderr_path)
                )
            recovery_metadata = _read_json(recovery_metadata_path)
            if (
                recovery_metadata is None
                or recovery_metadata.get("mode") != "recover"
                or recovery_metadata.get("workflow_id") != workflow_id
                or recovery_metadata.get("workflow_started_here") is not False
                or not isinstance(recovery_metadata.get("pid"), int)
            ):
                raise RuntimeError(
                    "recovery worker metadata did not prove recovery-only mode: "
                    f"{recovery_metadata!r}"
                )
            recovery_pid = int(recovery_metadata["pid"])
            workflow_started_in_recovery = recovery_metadata["workflow_started_here"]

        if workflow_id is None:
            raise RuntimeError("workflow identity was not captured")
        expected_status = "ERROR" if case_name == "unqueryable_unknown" else "SUCCESS"
        final_evidence = _validate_final_evidence(
            case_name=case_name,
            expected_status=expected_status,
            workflow_id=workflow_id,
            database_path=database_path,
            external_database_path=external_database_path,
            adapter_database_path=adapter_path_for_result,
            event_log_path=event_log_path,
        )
        summary = {
            "status": "PASS",
            "case": case_name,
            "workflow_id": workflow_id,
            "workflow_status": expected_status,
            "workflow_count": final_evidence["workflow_snapshot"]["workflow_count"],
            "worker_step_entries": final_evidence["step_entries"],
            "external_effect_count": final_evidence["external_snapshot"]["totals"][0],
            "external_total_value": final_evidence["external_snapshot"]["totals"][1],
            "retry_count": len(final_evidence["retry_errors"]),
            "retry_error_kinds": final_evidence["retry_errors"],
            "initial_killed_by_parent": initial_killed_by_parent,
            "initial_worker_exited": (
                initial_process is not None and initial_process.poll() is not None
            ),
            "heartbeat_stopped_after_kill": heartbeat_stopped_after_kill,
            "initial_popen_pid": initial_popen_pid,
            "initial_worker_pid": initial_pid,
            "recovery_popen_pid": recovery_popen_pid,
            "recovery_worker_pid": recovery_pid,
            "parent_popen_worker_pid_mismatch": (
                initial_popen_pid != initial_pid
                or (
                    recovery_popen_pid is not None
                    and recovery_pid is not None
                    and recovery_popen_pid != recovery_pid
                )
            ),
            "workflow_started_in_recovery": workflow_started_in_recovery,
            "workflow_id_before_recovery": workflow_id_before_recovery,
            "status_before_recovery": status_before_recovery,
            "checkpoint_complete_before_recovery": checkpoint_complete_before_recovery,
            "unknown_rejected": final_evidence["unknown_event"] is not None,
            "unknown_evidence": (
                None
                if final_evidence["unknown_event"] is None
                else final_evidence["unknown_event"]["reason"]
            ),
            "adapter_status": (
                None if not final_evidence["adapter_rows"] else final_evidence["adapter_rows"][0][2]
            ),
            "database": str(database_path),
            "external_database": str(external_database_path),
            "adapter_database": (
                None if adapter_path_for_result is None else str(adapter_path_for_result)
            ),
            "event_log": str(event_log_path),
            "initial_heartbeat_log": str(initial_heartbeat_path),
            "recovery_heartbeat_log": (
                str(recovery_heartbeat_path) if recovery_pid is not None else None
            ),
            "run_dir": str(run_dir),
        }
        _write_json(run_dir / "result.json", summary)
        _write_json(run_dir / "final-evidence.json", final_evidence)
        return summary
    except Exception as error:
        _write_json(
            run_dir / "failure.json",
            {
                "error": str(error),
                "traceback": traceback.format_exc(),
                "case": case_name,
                "workflow_id": workflow_id,
                "initial_popen_pid": initial_popen_pid,
                "initial_worker_pid": initial_pid,
                "recovery_popen_pid": recovery_popen_pid,
                "recovery_worker_pid": recovery_pid,
                "initial_killed_by_parent": initial_killed_by_parent,
                "workflow": (
                    _workflow_snapshot(database_path) if database_path.is_file() else None
                ),
                "external": _external_snapshot(external_database_path),
                "adapter": _adapter_snapshot(adapter_path_for_result),
                "worker_events": _read_json_lines(event_log_path),
                "run_dir": str(run_dir),
            },
        )
        raise RuntimeError(f"Phase 2 {case_name} failed; evidence: {run_dir}; {error}") from error
    finally:
        _stop_owned_process(recovery_process)
        _stop_owned_process(initial_process)
        for handle in log_handles:
            handle.close()


def main() -> int:
    default_state_root = Path(__file__).resolve().parent / ".state" / "phase2-runs"
    parser = argparse.ArgumentParser(description="Run one isolated Phase 2 DBOS case.")
    parser.add_argument("--case", choices=CASE_NAMES, required=True)
    parser.add_argument("--state-dir", type=Path, default=default_state_root)
    parser.add_argument("--phase-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    try:
        summary = run_case(
            args.case,
            args.state_dir,
            phase_timeout_seconds=args.phase_timeout_seconds,
        )
    except Exception as error:
        print(f"Phase 2 experiment failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
