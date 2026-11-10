from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

RECOVERY_SCRIPT = Path(__file__).with_name("recovery_test.py")
STATE_ROOT = Path(__file__).with_name(".state") / "pytest-recovery"


def test_recovery_replays_only_the_interrupted_step_after_process_kill() -> None:
    state_dir = STATE_ROOT / uuid.uuid4().hex
    completed = subprocess.run(
        [sys.executable, str(RECOVERY_SCRIPT), "--state-dir", str(state_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, (
        f"exit={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["workflow_id"]
    assert result["initial_killed_by_parent"] is True
    assert result["initial_worker_exited"] is True
    assert result["heartbeat_stopped_after_kill"] is True
    assert result["initial_pid"] != result["recovery_pid"]
    assert result["initial_popen_pid"] != result["recovery_popen_pid"]
    assert result["workflow_count_before_recovery"] == 1
    assert result["status_before_recovery"] == "PENDING"
    assert result["completed_steps_before_recovery"] == ["step1"]
    assert result["step2_completed_before_recovery"] is False
    assert result["events_before_recovery"] == 2
    assert result["workflow_count_after_recovery"] == 1
    assert result["status_after_recovery"] == "SUCCESS"
    assert result["completed_operation_count"] == 3

    event_path = Path(result["event_log"])
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert [(event["step"], event["pid"]) for event in events] == [
        ("step1", result["initial_pid"]),
        ("step2", result["initial_pid"]),
        ("step2", result["recovery_pid"]),
        ("step3", result["recovery_pid"]),
    ]

    database_path = Path(result["database"])
    with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        workflows = connection.execute(
            "SELECT workflow_uuid, status, executor_id, application_version, application_name "
            "FROM workflow_status"
        ).fetchall()
        operations = connection.execute(
            "SELECT function_name, completed_at_epoch_ms FROM operation_outputs "
            "WHERE workflow_uuid = ? ORDER BY function_id",
            (result["workflow_id"],),
        ).fetchall()

    assert workflows == [
        (
            result["workflow_id"],
            "SUCCESS",
            result["executor_id"],
            result["application_version"],
            result["application_name"],
        )
    ]
    assert [name for name, _ in operations] == ["step1", "step2", "step3"]
    assert all(completed_at is not None for _, completed_at in operations)
