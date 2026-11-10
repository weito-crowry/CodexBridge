from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

OWS_DIR = Path(__file__).resolve().parents[1]
STATE_DIR = OWS_DIR / ".state"


def _invoke(*args: str, timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=OWS_DIR.parent.parent,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_enqueued_status_waits_for_persisted_success(monkeypatch: Any) -> None:
    import worker

    statuses = iter(["ENQUEUED", "SUCCESS"])
    monkeypatch.setattr(
        worker,
        "_read_workflow_status",
        lambda _database_path, _workflow_id: next(statuses),
    )

    class CompletedHandle:
        def get_result(self) -> dict[str, bool]:
            return {"completed": True}

    monkeypatch.setattr(
        worker.DBOS,
        "retrieve_workflow",
        lambda *_args: CompletedHandle(),
    )
    assert worker._wait_for_result(Path("unused.sqlite"), "workflow-1", 1.0) == {"completed": True}


def test_cli_runs_ows_tasks_as_dbos_checkpoints() -> None:
    output = _invoke(
        str(OWS_DIR / "cli.py"),
        "run",
        "--workflow",
        str(OWS_DIR / "fixtures" / "workflow.yaml"),
        "--input-json",
        '{"sign":1,"offset":2,"values":[3,5]}',
    )
    assert output["result"] == {
        "branch": "positive",
        "indexes": [0, 1],
        "items": [3, 5],
        "total": 7,
    }
    assert output["definition_sha256"]
    assert output["input_sha256"]

    database = Path(output["run_dir"]) / "dbos.sqlite"
    database_uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        workflows = connection.execute(
            "SELECT workflow_uuid, status FROM workflow_status"
        ).fetchall()
        steps = connection.execute(
            "SELECT function_name, completed_at_epoch_ms FROM operation_outputs "
            "WHERE workflow_uuid = ? ORDER BY function_id",
            (output["workflow_id"],),
        ).fetchall()
    assert workflows == [(output["workflow_id"], "SUCCESS")]
    assert len(steps) >= 8
    assert all(completed_at is not None for _, completed_at in steps)


def test_cli_replays_dataflow_transformations_around_dbos_checkpoints() -> None:
    output = _invoke(
        str(OWS_DIR / "cli.py"),
        "run",
        "--workflow",
        str(OWS_DIR / "fixtures" / "dataflow_workflow.yaml"),
        "--input-json",
        '{"request":{"value":8,"discarded":true},"discarded":"raw"}',
    )

    assert output["result"] == {
        "result": 9,
        "seen_context": 9,
        "context": {"value": 8, "discarded": True, "published": 9, "source": 8},
    }

    database = Path(output["run_dir"]) / "dbos.sqlite"
    database_uri = f"{database.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        steps = connection.execute(
            "SELECT function_name, completed_at_epoch_ms FROM operation_outputs "
            "WHERE workflow_uuid = ? ORDER BY function_id",
            (output["workflow_id"],),
        ).fetchall()
    assert len(steps) == 2
    assert all(completed_at is not None for _, completed_at in steps)


def test_mock_job_fixture_uses_durable_jobs_and_preserves_loop_dataflow() -> None:
    output = _invoke(
        str(OWS_DIR / "cli.py"),
        "run",
        "--workflow",
        str(OWS_DIR / "fixtures" / "mock_job_workflow.yaml"),
        "--input-json",
        '{"values":[3,5]}',
    )
    run_dir = Path(output["run_dir"])
    assert run_dir.parent == STATE_DIR / "phase4b-runs"
    assert run_dir.name.startswith("run-")
    assert output["result"]["result_values"] == [3, 5]
    assert output["result"]["job_ids"][-1] == output["result"]["last_job_id"]

    with sqlite3.connect(run_dir / "jobs.sqlite") as connection:
        jobs = connection.execute(
            "SELECT job_id, request_key, workflow_id, task_identity, state, "
            "dispatch_count, service_execution_count, result_json, progress_json "
            "FROM jobs ORDER BY task_identity"
        ).fetchall()
    assert len(jobs) == 2
    assert len({row[0] for row in jobs}) == 2
    assert len({row[1] for row in jobs}) == 2
    assert all(row[2] == output["workflow_id"] for row in jobs)
    assert [row[3] for row in jobs] == [
        "do[1].jobs.iteration[0].do[0].start",
        "do[1].jobs.iteration[1].do[0].start",
    ]
    assert all(row[4:7] == ("SUCCEEDED", 1, 1) for row in jobs)
    assert [json.loads(row[7])["payload"] for row in jobs] == [3, 5]
    assert all(json.loads(row[8])["percent"] == 100 for row in jobs)

    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        status = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
            (output["workflow_id"],),
        ).fetchone()
    assert status == ("SUCCESS",)


def test_mock_job_survives_worker_kill_and_completed_checkpoint_replay() -> None:
    input_json = '{"payload":{"value":42},"duration_ms":250}'
    normal = _invoke(
        str(OWS_DIR / "cli.py"),
        "run",
        "--workflow",
        str(OWS_DIR / "fixtures" / "mock_job_recovery_workflow.yaml"),
        "--input-json",
        input_json,
    )
    output = _invoke(
        str(OWS_DIR / "phase4b_recovery_test.py"),
        "--normal-result-path",
        str(Path(normal["run_dir"]) / "result.json"),
        timeout=240,
    )
    assert output["result"] == "PASS"
    assert output["service_survived_initial_worker_kill"] is True
    assert (
        output["service_heartbeats_after_worker_kill"] > output["service_heartbeats_at_worker_kill"]
    )
    assert output["checkpoint_absent_at_first_barrier"] is True
    assert output["checkpoint_absent_after_worker_kill"] is True
    assert output["job_state_before_checkpoint"] == "SUCCEEDED"
    assert output["initial_worker_parent_pid"] == output["initial_popen_pid"]
    assert output["recovery_worker_parent_pid"] == output["recovery_popen_pid"]
    assert output["final_worker_parent_pid"] == output["final_popen_pid"]
    assert output["initial_worker_pid"] != output["recovery_worker_pid"]
    assert output["recovery_worker_pid"] != output["final_worker_pid"]
    assert output["heartbeat_updates_at_kill"] >= 2
    assert output["heartbeat_updates_at_kill"] >= output["heartbeat_updates_before_kill"]
    assert output["heartbeat_updates_after_kill"] == output["heartbeat_updates_at_kill"]
    assert output["heartbeat_pid_matches_worker"] is True
    assert output["service_execution_count"] == 1
    assert output["job_dispatch_count_after_checkpoint_replay"] == 2
    assert output["ledger_row_count"] == 1
    assert output["request_key_stable_across_recovery"] is True
    assert output["call_step_starts"] == 2
    assert output["call_step_completions"] == 1
    assert output["final_status"] == "SUCCESS"
    assert output["workflow_count"] == 1
    assert output["normal_workflow_id"] != output["workflow_id"]
    assert output["result_matches_uninterrupted_semantics"] is True


def _run_mock_job_outcome(
    *, payload: Any, timeout_seconds: float, start_service: bool
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    import cli

    run_dir = cli._new_run(
        OWS_DIR / "fixtures" / "mock_job_recovery_workflow.yaml",
        json.dumps({"payload": payload, "duration_ms": 0}, separators=(",", ":")),
        10_000,
    )
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["control"] = {"mock_job_timeout_seconds": timeout_seconds}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    from job_service import start_mock_job_service

    service = start_mock_job_service(run_dir) if start_service else None
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(OWS_DIR / "worker.py"),
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--timeout-seconds",
                "20",
            ],
            cwd=OWS_DIR.parent.parent,
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    finally:
        if service is not None:
            assert service.stop() == 0
    assert time.monotonic() - started < 20
    return run_dir, completed


def test_mock_service_failure_persists_failed_receipt_and_dbos_error() -> None:
    run_dir, completed = _run_mock_job_outcome(
        payload={"mock_failure": True}, timeout_seconds=5, start_service=True
    )
    assert completed.returncode != 0
    startup = json.loads((run_dir / "start-worker.json").read_text(encoding="utf-8"))
    workflow_id = startup["workflow_id"]
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        status = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?", (workflow_id,)
        ).fetchone()
    with sqlite3.connect(run_dir / "jobs.sqlite") as connection:
        jobs = connection.execute(
            "SELECT state, dispatch_count, service_execution_count, error FROM jobs"
        ).fetchall()
    events = [
        json.loads(line)
        for line in (run_dir / "job-service.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert status == ("ERROR",)
    assert len(jobs) == 1
    assert jobs[0][0:3] == ("FAILED", 1, 1)
    assert jobs[0][3] == "mock service deterministic failure"
    assert sum(event["event"] == "job_failed" for event in events) == 1


def test_mock_job_timeout_is_bounded_without_cancel_or_redispatch() -> None:
    run_dir, completed = _run_mock_job_outcome(
        payload={"value": 17}, timeout_seconds=0.2, start_service=False
    )
    assert completed.returncode != 0
    assert "job was not cancelled" in completed.stderr
    startup = json.loads((run_dir / "start-worker.json").read_text(encoding="utf-8"))
    workflow_id = startup["workflow_id"]
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        status = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?", (workflow_id,)
        ).fetchone()
    with sqlite3.connect(run_dir / "jobs.sqlite") as connection:
        jobs = connection.execute(
            "SELECT state, dispatch_count, service_execution_count, result_json FROM jobs"
        ).fetchall()
    assert status == ("ERROR",)
    assert len(jobs) == 1
    assert jobs[0] == ("QUEUED", 1, 0, None)


@pytest.mark.parametrize(
    ("task_input", "variables", "message"),
    [
        (
            {"value": 3},
            {"context": {"run": 1}, "item": 4},
            "input digest mismatch",
        ),
        (
            {"value": 2},
            {"context": {"run": 2}, "item": 4},
            "context digest mismatch",
        ),
        (
            {"value": 2},
            {"context": {"run": 1}, "item": 5},
            "runtime variables digest mismatch",
        ),
    ],
)
def test_checkpoint_replay_rejects_changed_input_or_context_digest(
    task_input: dict[str, int], variables: dict[str, Any], message: str
) -> None:
    import worker

    definition_sha256 = "definition-hash"
    checkpoint = {
        "identity": "do[0].task",
        "task_name": "task",
        "operation": "set",
        "definition_sha256": definition_sha256,
        "input_sha256": worker.sha256_json({"value": 2}),
        "context_sha256": worker.sha256_json({"run": 1}),
        "variables_sha256": worker.sha256_json({"context": {"run": 1}, "item": 4}),
        "result": {"value": 4},
    }

    with pytest.raises(worker.OwsError, match=message):
        worker._validate_task_checkpoint(
            checkpoint,
            identity="do[0].task",
            task_name="task",
            operation="set",
            definition_sha256=definition_sha256,
            task_input=task_input,
            variables=variables,
        )


def test_interrupted_run_manifest_resumes_through_cli_and_reuses_steps() -> None:
    state_root = STATE_DIR / "test-recovery" / f"run-{uuid.uuid4().hex}"
    output = _invoke(
        str(OWS_DIR / "recovery_test.py"),
        "--state-dir",
        str(state_root),
    )
    assert output["result"] == "PASS"
    assert output["initial_killed_by_parent"] is True
    assert output["same_workflow_id"] is True
    assert output["manifest_id_persisted_before_kill"] is True
    assert output["workflow_count"] == 1
    assert output["final_status"] == "SUCCESS"
    assert output["result_matches_uninterrupted"] is True
    assert output["recovered_result"] == {"final": 2}
    assert output["task_start_counts"] == {"first": 1, "second": 2, "finish": 1}
    assert output["initial_popen_pid"] != output["recovery_popen_pid"]
    assert output["initial_heartbeat_pid"] == output["initial_worker_pid"]
    assert output["initial_heartbeat_updates_before_kill"] >= 2
    assert (
        output["initial_heartbeat_updates_at_kill"]
        >= output["initial_heartbeat_updates_before_kill"]
    )
    assert (
        output["initial_heartbeat_updates_after_kill"]
        == output["initial_heartbeat_updates_at_kill"]
    )
    assert output["initial_heartbeat_quiet_seconds"] >= 0.5
    assert output["initial_heartbeat_stopped"] is True
