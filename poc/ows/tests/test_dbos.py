from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

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
