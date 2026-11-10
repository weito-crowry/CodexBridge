from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from job_adapter import (
    DurableJobAdapter,
    JobConflictError,
    JobReconciliationRequiredError,
    JobRequestError,
    claim_next_job,
    finish_job,
    update_job_progress,
    validate_mock_job_request,
)
from job_service import start_mock_job_service
from process_identity import terminate_spawned_python


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


def test_start_job_returns_before_service_finishes_and_reuses_the_request_key(tmp_path) -> None:
    run_dir = Path(tmp_path)
    adapter = DurableJobAdapter(run_dir / "jobs.sqlite")
    service = start_mock_job_service(run_dir)
    request = {"operation": "process", "payload": {"value": 7}, "duration_ms": 1200}
    try:
        started_at = time.monotonic()
        first = adapter.start_job("workflow-async", "do[0].submit", request)
        second = adapter.start_job("workflow-async", "do[0].submit", request)
        start_elapsed = time.monotonic() - started_at

        assert start_elapsed < 0.5
        assert first["job_id"] == second["job_id"]
        assert first["request_key"] == "workflow-async::do[0].submit"
        assert first["status"] in {"QUEUED", "RUNNING"}
        with pytest.raises(JobConflictError, match="request digest mismatch"):
            adapter.start_job(
                "workflow-async",
                "do[0].submit",
                {"operation": "process", "payload": {"value": 8}, "duration_ms": 1200},
            )
        assert adapter.count_jobs() == 1
        assert adapter.get_job(first["job_id"])["service_execution_count"] == 0
        assert adapter.get_job(first["job_id"])["dispatch_count"] == 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            final = adapter.get_job_status(job_id=first["job_id"])
            if final["state"] in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.02)
    finally:
        assert service.stop() == 0

    final = adapter.get_job(first["job_id"])
    assert final["state"] == "SUCCEEDED"
    assert final["service_execution_count"] == 1


def test_job_events_are_durable_bounded_and_catch_up_across_status_changes(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite"
    adapter = DurableJobAdapter(database)
    job = adapter.start_job(
        "workflow-events",
        "do[0].submit",
        {"operation": "process", "payload": {"value": 11}, "duration_ms": 100},
    )

    before = adapter.get_job_status(job_id=job["job_id"])
    first_page = adapter.read_events(after_id=0, limit=1)
    assert [event["state"] for event in first_page] == ["QUEUED"]
    assert adapter.read_events(after_id=0, limit=1) == first_page

    claimed = claim_next_job(database)
    assert claimed["job_id"] == job["job_id"]
    update_job_progress(database, job["job_id"], 50, 100)
    finish_job(
        database,
        job["job_id"],
        result={"operation": "process", "payload": {"value": 11}, "processed": True},
    )
    after = adapter.get_job_status(job_id=job["job_id"])
    caught_up = adapter.read_events(after_id=first_page[-1]["event_id"], limit=10)

    assert before["state"] == "QUEUED"
    assert before["latest_event_id"] == first_page[-1]["event_id"]
    assert after["state"] == "SUCCEEDED"
    assert after["latest_event_id"] == caught_up[-1]["event_id"]
    assert [event["state"] for event in first_page + caught_up] == [
        "QUEUED",
        "RUNNING",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert [event["event_id"] for event in first_page + caught_up] == sorted(
        event["event_id"] for event in first_page + caught_up
    )
    assert adapter.read_events(after_id=caught_up[-1]["event_id"], limit=10) == []
    with sqlite3.connect(database) as connection:
        persisted = connection.execute(
            "SELECT state, COUNT(*) FROM job_events GROUP BY state ORDER BY MIN(event_id)"
        ).fetchall()
    assert persisted == [("QUEUED", 1), ("RUNNING", 2), ("SUCCEEDED", 1)]


def test_failed_job_writes_terminal_event_and_missing_status_requires_reconciliation(
    tmp_path,
) -> None:
    database = tmp_path / "jobs.sqlite"
    adapter = DurableJobAdapter(database)
    job = adapter.start_job(
        "workflow-failed-event",
        "do[0].submit",
        {"operation": "process", "payload": {}, "duration_ms": 0},
    )
    claim_next_job(database)
    finish_job(database, job["job_id"], error="injected failure")

    assert [event["state"] for event in adapter.read_events(after_id=0, limit=10)] == [
        "QUEUED",
        "RUNNING",
        "FAILED",
    ]
    assert adapter.get_job_status(job_id=job["job_id"])["error"] == "injected failure"
    with pytest.raises(JobReconciliationRequiredError, match="missing"):
        adapter.get_job_status(job_id="unknown-job")


def test_killed_service_marks_stale_running_without_reclaiming_it() -> None:
    ows_dir = Path(__file__).resolve().parents[1]
    state_root = ows_dir / ".state" / "phase4c-runs"
    state_root.mkdir(parents=True, exist_ok=True)
    run_dir = state_root / f"run-{uuid.uuid4().hex}"
    run_dir.mkdir(parents=False, exist_ok=False)
    fixture_path = ows_dir / "fixtures" / "mock_job_recovery_workflow.yaml"
    definition_text = fixture_path.read_text(encoding="utf-8")
    input_data = {"payload": {"value": 42}, "duration_ms": 2000}
    input_json = json.dumps(
        input_data, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8", newline="\n")
    (run_dir / "input.json").write_text(input_json + "\n", encoding="utf-8", newline="\n")
    workflow_id = f"phase4c-crash-{uuid.uuid4().hex}"
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_source": str(fixture_path.resolve()),
                "workflow_id": workflow_id,
                "definition_sha256": hashlib.sha256(definition_text.encode("utf-8")).hexdigest(),
                "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
                "max_tasks": 10_000,
                "control": {"mock_job_durable_wait": True},
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    database = run_dir / "jobs.sqlite"
    adapter = DurableJobAdapter(database)
    service = start_mock_job_service(run_dir)
    service_stop_exit_code: int | None = None
    submitted = adapter.start_job(
        workflow_id,
        "do[0].submit",
        {"operation": "process", "payload": {"value": 42}, "duration_ms": 2000},
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = adapter.get_job(submitted["job_id"])
            if current["state"] == "RUNNING" and current["progress"].get("percent", 0) > 0:
                break
            time.sleep(0.02)
        assert current["state"] == "RUNNING"
        assert current["progress"]["percent"] < 100
        active_snapshot = adapter.get_job_status(job_id=submitted["job_id"], stale_ttl_seconds=0.2)
        assert active_snapshot["adapter_marker"] is None
        assert service.service_pid is not None
        assert service.service_creation_time_100ns is not None
        assert service.service_image_path is not None
        terminate_spawned_python(
            service.service_pid,
            service.process.pid,
            expected_creation_time_100ns=service.service_creation_time_100ns,
            expected_image_path=service.service_image_path,
        )
        service_stop_exit_code = service.stop()
        assert service_stop_exit_code == 1

        time.sleep(0.3)
        snapshot = adapter.get_job_status(job_id=submitted["job_id"], stale_ttl_seconds=0.2)
        assert snapshot["state"] == "RUNNING"
        assert snapshot["status"] == "NEEDS_RECONCILIATION"
        assert snapshot["adapter_marker"] == "NEEDS_RECONCILIATION"
        assert snapshot["result"] is None
        assert snapshot["error"] is None

        replacement = start_mock_job_service(run_dir)
        try:
            time.sleep(0.15)
            still_stale = adapter.get_job_status(job_id=submitted["job_id"], stale_ttl_seconds=0.2)
            assert still_stale["adapter_marker"] == "NEEDS_RECONCILIATION"
            assert adapter.get_job(submitted["job_id"])["state"] == "RUNNING"
            assert adapter.get_job(submitted["job_id"])["service_execution_count"] == 1
            assert adapter.count_jobs() == 1
            assert replacement.is_running()
        finally:
            assert replacement.stop() == 0
        events = adapter.read_events(after_id=0, limit=1000)
        evidence = {
            "result": "PASS",
            "run_dir": str(run_dir),
            "workflow_id": workflow_id,
            "job_id": submitted["job_id"],
            "request_key": submitted["request_key"],
            "initial_service_pid": service.service_pid,
            "initial_service_creation_time_100ns": service.service_creation_time_100ns,
            "initial_service_image_path": service.service_image_path,
            "initial_service_run_dir": str(run_dir),
            "initial_service_exit_code": service_stop_exit_code,
            "replacement_service_id": replacement.service_id,
            "ledger_state": adapter.get_job(submitted["job_id"])["state"],
            "adapter_status": still_stale["status"],
            "ledger_row_count": adapter.count_jobs(),
            "service_execution_count": adapter.get_job(submitted["job_id"])[
                "service_execution_count"
            ],
            "event_count": len(events),
            "event_states": [
                event["state"] for event in events if event["job_id"] == submitted["job_id"]
            ],
            "automatic_reclaim": False,
        }
        (run_dir / "phase4c-service-crash-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        assert evidence["result"] == "PASS"
    finally:
        if service.process.poll() is None:
            service.stop()


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
