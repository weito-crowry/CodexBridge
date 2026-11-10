from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SMOKE_SCRIPT = Path(__file__).with_name("smoke.py")


def test_smoke_runs_three_steps_and_persists_dbos_state(tmp_path: Path) -> None:
    database_path = tmp_path / "dbos-test.sqlite"
    completed = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT), "--db-path", str(database_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, (
        f"exit={completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["steps"] == ["step_one", "step_two", "step_three"]
    assert result["value"] == 6
    assert result["workflow_id"]
    assert database_path.is_file() and database_path.stat().st_size > 0

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        assert "workflow_status" in tables
        assert "operation_outputs" in tables
        workflow_count = connection.execute("SELECT COUNT(*) FROM workflow_status").fetchone()[0]
        step_names = [
            row[0]
            for row in connection.execute(
                "SELECT function_name FROM operation_outputs ORDER BY function_id"
            )
        ]

    assert workflow_count == 1
    assert step_names == ["step_one", "step_two", "step_three"]
