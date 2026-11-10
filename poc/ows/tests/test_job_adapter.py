from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from job_adapter import (
    DurableJobAdapter,
    JobConflictError,
    JobReconciliationRequiredError,
    JobRequestError,
    validate_mock_job_request,
)
from job_service import start_mock_job_service


def test_same_request_key_and_payload_reuses_one_queued_job(tmp_path) -> None:
    adapter = DurableJobAdapter(tmp_path / "jobs.sqlite")
    request = {"operation": "process", "payload": {"value": 7}, "duration_ms": 0}

    first = adapter.submit_or_get_job("workflow-1", "do[0].submit", request)
    second = adapter.submit_or_get_job("workflow-1", "do[0].submit", request)

    assert second["job_id"] == first["job_id"]
    assert second["request_key"] == "workflow-1::do[0].submit"
    assert second["state"] == "QUEUED"
    assert adapter.count_jobs() == 1
    assert adapter.get_job(first["job_id"])["dispatch_count"] == 2
    assert adapter.get_job(first["job_id"])["service_execution_count"] == 0


def test_same_request_key_with_different_payload_fails_closed(tmp_path) -> None:
    adapter = DurableJobAdapter(tmp_path / "jobs.sqlite")
    adapter.submit_or_get_job(
        "workflow-1",
        "do[0].submit",
        {"operation": "process", "payload": {"value": 7}, "duration_ms": 0},
    )

    with pytest.raises(JobConflictError, match="request digest mismatch"):
        adapter.submit_or_get_job(
            "workflow-1",
            "do[0].submit",
            {"operation": "process", "payload": {"value": 8}, "duration_ms": 0},
        )
    assert adapter.count_jobs() == 1


def test_adapter_persists_the_canonical_request_and_job_identity(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite"
    adapter = DurableJobAdapter(database)
    job = adapter.submit_or_get_job(
        "workflow-2",
        "do[1].loop.iteration[3].do[0].submit",
        {"duration_ms": 2, "payload": {"b": 2, "a": 1}, "operation": "process"},
    )

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT workflow_id, task_identity, request_key, request_json, state "
            "FROM jobs WHERE job_id = ?",
            (job["job_id"],),
        ).fetchone()

    assert row == (
        "workflow-2",
        "do[1].loop.iteration[3].do[0].submit",
        "workflow-2::do[1].loop.iteration[3].do[0].submit",
        '{"duration_ms":2,"operation":"process","payload":{"a":1,"b":2}}',
        "QUEUED",
    )


def test_concurrent_submits_use_one_request_key_and_one_job(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite"
    request = {"operation": "process", "payload": {"value": 9}, "duration_ms": 0}

    def submit(_: int) -> dict[str, object]:
        return DurableJobAdapter(database).submit_or_get_job(
            "workflow-race", "do[0].submit", request
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = list(executor.map(submit, range(8)))

    assert len({job["job_id"] for job in jobs}) == 1
    adapter = DurableJobAdapter(database)
    assert adapter.count_jobs() == 1
    assert adapter.get_job(jobs[0]["job_id"])["dispatch_count"] == 8
    assert adapter.get_job(jobs[0]["job_id"])["service_execution_count"] == 0


def test_independent_service_completes_one_claim_and_persists_receipt(tmp_path) -> None:
    run_dir = Path(tmp_path)
    service = start_mock_job_service(run_dir)
    adapter = DurableJobAdapter(run_dir / "jobs.sqlite")
    try:
        submitted = adapter.submit_or_get_job(
            "workflow-service",
            "do[0].submit",
            {"operation": "process", "payload": {"value": 11}, "duration_ms": 80},
        )
        duplicate = adapter.submit_or_get_job(
            "workflow-service",
            "do[0].submit",
            {"operation": "process", "payload": {"value": 11}, "duration_ms": 80},
        )
        assert duplicate["job_id"] == submitted["job_id"]
        assert adapter.count_jobs() == 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = adapter.get_job(submitted["job_id"])
            if current["state"] in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)
    finally:
        assert service.stop() == 0

    final = adapter.get_job(submitted["job_id"])
    assert final["state"] == "SUCCEEDED"
    assert final["result"] == {
        "operation": "process",
        "payload": {"value": 11},
        "processed": True,
    }
    assert final["service_execution_count"] == 1
    assert final["dispatch_count"] == 2
    assert final["progress"] == {"completed_ms": 0, "percent": 100}


@pytest.mark.parametrize(
    "job_request",
    [
        {"operation": "http", "payload": {}, "duration_ms": 0},
        {"operation": "process", "payload": {}, "duration_ms": True},
        {"operation": "process", "payload": {}, "duration_ms": 2001},
        {"operation": "process", "payload": {}, "duration_ms": 0, "extra": 1},
        {"operation": "process", "payload": float("nan"), "duration_ms": 0},
    ],
)
def test_mock_job_request_rejects_unlisted_or_invalid_values(job_request) -> None:
    with pytest.raises(JobRequestError):
        validate_mock_job_request(job_request)


def test_existing_unknown_ledger_state_requires_reconciliation_without_dispatch(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite"
    adapter = DurableJobAdapter(database)
    job = adapter.submit_or_get_job(
        "workflow-unknown",
        "do[0].submit",
        {"operation": "process", "payload": {"value": 1}, "duration_ms": 0},
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE jobs SET state = 'INDETERMINATE' WHERE job_id = ?", (job["job_id"],)
        )

    with pytest.raises(JobReconciliationRequiredError, match="NEEDS_RECONCILIATION"):
        adapter.submit_or_get_job(
            "workflow-unknown",
            "do[0].submit",
            {"operation": "process", "payload": {"value": 1}, "duration_ms": 0},
        )
    with sqlite3.connect(database) as connection:
        dispatch_count = connection.execute(
            "SELECT dispatch_count FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()[0]
    assert dispatch_count == 1
