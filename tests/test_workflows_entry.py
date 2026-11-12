from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_bridge import console_entry
from codex_bridge.runtime_mode import WORKFLOW_WORKER_FLAG
from codex_bridge.workflows import worker as worker_module
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
    lifecycle_events = [
        json.loads(line)
        for line in (run_dir / "worker-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in lifecycle_events] == [
        "worker_bootstrap",
        "dbos_ready",
        "workflow_started",
        "result_persisted",
    ]
    assert all(event["run_id"] == run_dir.name for event in lifecycle_events)
    assert all("workflow_id" not in event for event in lifecycle_events[:2])
    assert all(event["workflow_id"] == result["workflow_id"] for event in lifecycle_events[2:])
    assert all(event["launch_id"] == "a" * 32 for event in lifecycle_events)


def test_worker_failure_log_keeps_exception_class_and_omits_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _config_path = _make_run(tmp_path)
    secret = "sk-secret-token prompt input result body"

    class FailingDBOS:
        def __init__(self, *, config: object) -> None:
            del config

        @staticmethod
        def launch() -> None:
            raise RuntimeError(secret)

        @staticmethod
        def destroy() -> None:
            return None

    monkeypatch.setattr(worker_module, "DBOS", FailingDBOS)
    monkeypatch.setattr(worker_module, "_dbos_config", lambda _path: object())
    monkeypatch.setattr(
        worker_module,
        "get_process_identity",
        lambda _pid: {
            "active": True,
            "pid": 1234,
            "parent_pid": 100,
            "creation_time_100ns": 1,
            "image_path": "python.exe",
        },
    )
    shutdown_file = run_dir / f"worker-shutdown-{'b' * 32}.request"

    with pytest.raises(RuntimeError, match="sk-secret-token"):
        worker_module.main(
            [
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--shutdown-file",
                str(shutdown_file),
            ]
        )

    log_text = (run_dir / "worker-lifecycle.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in log_text.splitlines()]
    assert [event["event"] for event in events] == ["worker_bootstrap", "worker_failed"]
    assert events[-1]["reason_code"] == "dbos_launch_failed"
    assert events[-1]["exception_class"] == "RuntimeError"
    assert secret not in log_text


def test_worker_records_shutdown_observed_without_result_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _config_path = _make_run(tmp_path)

    class FakeDBOS:
        def __init__(self, *, config: object) -> None:
            del config

        @staticmethod
        def launch() -> None:
            return None

        @staticmethod
        def start_workflow(*_args: object) -> SimpleNamespace:
            return SimpleNamespace(workflow_id="01234567-89ab-4cde-8012-3456789abcde")

        @staticmethod
        def destroy() -> None:
            return None

    monkeypatch.setattr(worker_module, "DBOS", FakeDBOS)
    monkeypatch.setattr(worker_module, "_dbos_config", lambda _path: object())
    monkeypatch.setattr(
        worker_module,
        "get_process_identity",
        lambda _pid: {
            "active": True,
            "pid": 1234,
            "parent_pid": 100,
            "creation_time_100ns": 1,
            "image_path": "python.exe",
        },
    )

    def shutdown_requested(*_args: object) -> object:
        raise worker_module.WorkerShutdownRequested("test-only shutdown")

    monkeypatch.setattr(worker_module, "_wait_for_result", shutdown_requested)
    shutdown_file = run_dir / f"worker-shutdown-{'c' * 32}.request"
    with pytest.raises(worker_module.WorkerShutdownRequested):
        worker_module.main(
            [
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--shutdown-file",
                str(shutdown_file),
            ]
        )

    events = [
        json.loads(line)
        for line in (run_dir / "worker-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "worker_bootstrap",
        "dbos_ready",
        "workflow_started",
        "shutdown_observed",
    ]
    assert events[-1]["reason_code"] == "shutdown_requested"
    assert not (run_dir / "result.json").exists()


def test_worker_recovery_records_attached_workflow_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _config_path = _make_run(tmp_path)
    workflow_id = str(uuid.uuid4())
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_id"] = workflow_id
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class FakeDBOS:
        def __init__(self, *, config: object) -> None:
            del config

        @staticmethod
        def launch() -> None:
            return None

        @staticmethod
        def destroy() -> None:
            return None

    monkeypatch.setattr(worker_module, "DBOS", FakeDBOS)
    monkeypatch.setattr(worker_module, "_dbos_config", lambda _path: object())
    monkeypatch.setattr(
        worker_module,
        "get_process_identity",
        lambda _pid: {
            "active": True,
            "pid": 1234,
            "parent_pid": 100,
            "creation_time_100ns": 1,
            "image_path": "python.exe",
        },
    )
    monkeypatch.setattr(worker_module, "_wait_for_result", lambda *_args: {"recovered": True})
    shutdown_file = run_dir / f"worker-shutdown-{'d' * 32}.request"

    assert (
        worker_module.main(
            [
                "--mode",
                "recover",
                "--run-dir",
                str(run_dir),
                "--shutdown-file",
                str(shutdown_file),
            ]
        )
        == 0
    )

    events = [
        json.loads(line)
        for line in (run_dir / "worker-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == [
        "worker_bootstrap",
        "dbos_ready",
        "workflow_recovery_attached",
        "result_persisted",
    ]
    assert all(event["workflow_id"] == workflow_id for event in events[1:])
