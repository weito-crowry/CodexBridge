from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codex_bridge import console_entry
from codex_bridge.runtime_mode import WORKFLOW_WORKER_FLAG
from codex_bridge.workflows import worker_entry
from codex_bridge.workflows.worker import canonical_json, sha256_json, sha256_text

ROOT = Path(__file__).resolve().parents[1]
POC_FIXTURE = ROOT / "poc" / "ows" / "fixtures" / "workflow.yaml"


def _make_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run-0123456789abcdef0123456789abcdef"
    run_dir.mkdir()
    definition_text = POC_FIXTURE.read_text(encoding="utf-8")
    input_data = {"sign": 1, "offset": 2, "values": [3, 5]}
    input_json = canonical_json(input_data)
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(input_json + "\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_source": str(POC_FIXTURE),
                "workflow_id": None,
                "definition_sha256": sha256_text(definition_text),
                "input_sha256": sha256_json(input_data),
                "max_tasks": 100,
                "control": {},
            }
        ),
        encoding="utf-8",
    )
    config_path = run_dir / "worker-config-test.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "start",
                "workspace_root": None,
                "allowed_roots": [],
                "shutdown_file": f"worker-shutdown-{'a' * 32}.request",
            }
        ),
        encoding="utf-8",
    )
    return run_dir, config_path


def test_worker_entry_reports_missing_workflows_extra_without_loading_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir, config_path = _make_run(tmp_path)
    monkeypatch.setattr(worker_entry, "missing_workflow_dependencies", lambda: ("dbos",))

    result = worker_entry.main(
        ["--mode", "start", "--run-dir", str(run_dir), "--config", str(config_path)]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "uv sync --extra workflows" in captured.err
    assert "Traceback" not in captured.err
    status = json.loads((run_dir / "worker-entry-status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"


def test_console_entry_routes_frozen_worker_flag_before_gui(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        console_entry, "_run_workflow_worker", lambda args: calls.append(args) or 23
    )
    monkeypatch.setattr(
        console_entry,
        "_load_gui",
        lambda: (_ for _ in ()).throw(AssertionError("GUI must not be imported")),
    )

    result = console_entry.main([WORKFLOW_WORKER_FLAG, "--mode", "recover", "--run-dir", "R"])

    assert result == 23
    assert calls == [["--mode", "recover", "--run-dir", "R"]]


@pytest.mark.asyncio
async def test_source_worker_entry_runs_an_offline_fixed_workflow(tmp_path: Path) -> None:
    run_dir, config_path = _make_run(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_bridge.workflows.worker_entry",
            "--mode",
            "start",
            "--run-dir",
            str(run_dir),
            "--config",
            str(config_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    status = json.loads((run_dir / "worker-entry-status.json").read_text(encoding="utf-8"))
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert status["state"] == "completed"
    assert status["exit_code"] == 0
    assert result["result"] == {
        "branch": "positive",
        "indexes": [0, 1],
        "items": [3, 5],
        "total": 7,
    }
