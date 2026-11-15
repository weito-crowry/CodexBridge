from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).with_name("side_effect_test.py")
STATE_ROOT = Path(__file__).with_name(".state")
APPLICATION_NAME = "codexbridge-dbos-side-effects-poc"
APPLICATION_VERSION = "2.0.0"
EXECUTOR_ID = "codexbridge-dbos-side-effects-poc-executor"
LOGICAL_OPERATION = "charge"
EFFECT_NAME = "invoice_charge"
EFFECT_VALUE = 41


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


@pytest.mark.parametrize(
    ("case_name", "expected_status", "expected_entries", "expected_retries"),
    [
        ("idempotent_recovery", "SUCCESS", 2, 0),
        ("transient_retry", "SUCCESS", 3, 2),
        ("unqueryable_unknown", "ERROR", 2, 0),
    ],
)
def test_side_effect_case_has_independent_database_evidence(
    case_name: str,
    expected_status: str,
    expected_entries: int,
    expected_retries: int,
) -> None:
    state_root = STATE_ROOT / f"phase2-pytest-{case_name}"
    completed = subprocess.run(
        [
            sys.executable,
            str(DRIVER),
            "--case",
            case_name,
            "--state-dir",
            str(state_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"exit={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["case"] == case_name
    assert result["status"] == "PASS"
    assert result["workflow_status"] == expected_status
    assert result["workflow_count"] == 1
    assert result["worker_step_entries"] == expected_entries
    assert result["external_effect_count"] == 1
    assert result["external_total_value"] == EFFECT_VALUE
    assert result["retry_count"] == expected_retries

    database_path = Path(result["database"])
    external_database_path = Path(result["external_database"])
    event_log_path = Path(result["event_log"])
    assert database_path.parent == external_database_path.parent
    assert database_path != external_database_path
    assert "phase2-" in database_path.as_posix()
    assert database_path.parent.name.startswith("run-")
    events = _read_json_lines(event_log_path)
    step_entries = [event for event in events if event["event"] == "step_entered"]
    assert len(step_entries) == expected_entries
    assert {event["workflow_id"] for event in step_entries} == {result["workflow_id"]}

    with _read_only_connection(database_path) as connection:
        workflows = connection.execute(
            "SELECT workflow_uuid, status, application_name, application_version, executor_id "
            "FROM workflow_status"
        ).fetchall()
        operations = connection.execute(
            "SELECT function_name, output, error, completed_at_epoch_ms "
            "FROM operation_outputs "
            "WHERE workflow_uuid = ? ORDER BY function_id",
            (result["workflow_id"],),
        ).fetchall()
    assert workflows == [
        (
            result["workflow_id"],
            expected_status,
            APPLICATION_NAME,
            APPLICATION_VERSION,
            EXECUTOR_ID,
        )
    ]
    assert len(operations) == 1
    operation_name, operation_output, operation_error, operation_completed_at = operations[0]
    assert operation_name == "side_effect_step"
    if case_name == "unqueryable_unknown":
        assert result["status_before_recovery"] == "PENDING"
        assert result["checkpoint_complete_before_recovery"] is False
        assert result["workflow_id_before_recovery"] == result["workflow_id"]
        assert operation_output is None
        assert operation_error is not None
        assert operation_completed_at is not None
    else:
        assert operation_output is not None
        assert operation_error is None
        assert operation_completed_at is not None

    with _read_only_connection(external_database_path) as connection:
        effects = connection.execute(
            "SELECT request_key, workflow_id, logical_operation, effect_name, effect_value "
            "FROM effect_ledger"
        ).fetchall()
        totals = connection.execute(
            "SELECT total_effect_count, total_effect_value FROM service_totals"
        ).fetchall()
        calls = connection.execute(
            "SELECT operation, outcome, request_key, error_kind FROM service_calls ORDER BY id"
        ).fetchall()
    request_key = f"{result['workflow_id']}:{LOGICAL_OPERATION}"
    assert effects == [
        (request_key, result["workflow_id"], LOGICAL_OPERATION, EFFECT_NAME, EFFECT_VALUE)
    ]
    assert totals == [(1, EFFECT_VALUE)]

    if case_name == "idempotent_recovery":
        assert [(operation, outcome) for operation, outcome, _, _ in calls] == [
            ("lookup", "MISS"),
            ("send_idempotent", "APPLIED"),
            ("lookup", "HIT"),
        ]
        assert result["initial_killed_by_parent"] is True
        assert result["initial_worker_exited"] is True
        assert result["heartbeat_stopped_after_kill"] is True
        assert result["status_before_recovery"] == "PENDING"
        assert result["checkpoint_complete_before_recovery"] is False
        assert result["workflow_id_before_recovery"] == result["workflow_id"]
    elif case_name == "transient_retry":
        assert len(calls) == 3
        assert [outcome for _, outcome, _, _ in calls] == [
            "TRANSIENT_ERROR",
            "TRANSIENT_ERROR",
            "APPLIED",
        ]
        assert [error_kind for _, _, _, error_kind in calls] == [
            "TransientServiceError",
            "TransientServiceError",
            None,
        ]
        assert result["retry_error_kinds"] == ["TransientServiceError"] * 2
    else:
        assert calls == [("send_unqueryable", "APPLIED", request_key, None)]
        assert result["initial_killed_by_parent"] is True
        assert result["initial_worker_exited"] is True
        assert result["heartbeat_stopped_after_kill"] is True
        assert result["unknown_rejected"] is True
        assert result["adapter_status"] == "UNKNOWN"
        assert "no retry" in str(result["unknown_evidence"]).lower()

        adapter_database_path = Path(result["adapter_database"])
        with _read_only_connection(adapter_database_path) as connection:
            adapter_rows = connection.execute(
                "SELECT request_key, status, receipt, reason FROM adapter_state"
            ).fetchall()
        assert adapter_rows == [
            (
                request_key,
                "UNKNOWN",
                None,
                "dispatch intent exists without a local receipt; no retry",
            )
        ]
