from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_smoke_module() -> ModuleType | None:
    path = Path(__file__).parents[1] / "scripts" / "phase5e_smoke_support.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("phase5e_smoke_support", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_runner_module() -> ModuleType | None:
    try:
        return importlib.import_module("scripts.phase5e_isolated_smoke")
    except ImportError:
        return None


def test_child_environment_discards_inherited_bridge_secrets(tmp_path: Path) -> None:
    module = _load_smoke_module()
    assert module is not None, "the Phase 5E isolated smoke runner must exist"

    child = module.build_isolated_environment(
        {
            "PATH": "C:\\Windows\\System32",
            "CODEX_BRIDGE_CONTROL_TOKEN": "inherited-secret-marker",
            "CODEX_BRIDGE_PORT": "18000",
            "LOCALAPPDATA": "C:\\Users\\someone\\AppData\\Local",
            "APPDATA": "C:\\Users\\someone\\AppData\\Roaming",
            "CODEX_HOME": "C:\\Users\\someone\\.codex",
            "OPENAI_API_KEY": "unrelated-secret-marker",
        },
        temp_root=tmp_path,
        config_path=tmp_path / "bridge.toml",
        control_token="test-only-control-token-value",
    )

    bridge_keys = {key for key in child if key.startswith("CODEX_BRIDGE_")}
    assert bridge_keys == {"CODEX_BRIDGE_CONFIG", "CODEX_BRIDGE_CONTROL_TOKEN"}
    assert child["CODEX_BRIDGE_CONFIG"] == str(tmp_path / "bridge.toml")
    assert child["CODEX_BRIDGE_CONTROL_TOKEN"] == "test-only-control-token-value"
    assert child["PATH"] == "C:\\Windows\\System32"
    assert Path(child["LOCALAPPDATA"]) == tmp_path / "localappdata"
    assert Path(child["APPDATA"]) == tmp_path / "appdata"
    assert Path(child["CODEX_HOME"]) == tmp_path / "codex_home"
    assert "OPENAI_API_KEY" not in child


def test_state_and_workspace_roots_must_not_contain_each_other(tmp_path: Path) -> None:
    module = _load_smoke_module()
    assert module is not None, "the Phase 5E isolated smoke runner must exist"

    state = tmp_path / "localappdata" / "CodexBridge" / "workflows"
    workspace = tmp_path / "workspace"
    assert module.roots_are_separate(state, workspace)
    assert not module.roots_are_separate(tmp_path, workspace)
    assert not module.roots_are_separate(state, state / "nested")


def test_port_pair_rejects_duplicates_and_production_ports() -> None:
    module = _load_smoke_module()
    assert module is not None, "the Phase 5E isolated smoke runner must exist"

    assert module.ports_are_safe(23017, 23018)
    assert not module.ports_are_safe(23017, 23017)
    assert not module.ports_are_safe(18000, 23018)
    assert not module.ports_are_safe(23017, 18001)


def test_workflow_poll_continues_get_only_through_transient_unknown(
    monkeypatch: Any,
) -> None:
    module = _load_runner_module()
    assert module is not None, "the Phase 5E isolated smoke runner must exist"
    observations = iter(
        [
            (
                (200, {"workflows": []}),
                (200, {"workflow_id": "workflow-1", "status": "unknown"}),
                (404, {"error": "not_ready"}),
            ),
            (
                (
                    200,
                    {
                        "workflows": [
                            {
                                "workflow_id": "workflow-1",
                                "status": "succeeded",
                            }
                        ]
                    },
                ),
                (200, {"workflow_id": "workflow-1", "status": "succeeded"}),
                (200, {"workflow_id": "workflow-1", "result": {"final_text": "4"}}),
            ),
        ]
    )
    calls: list[str] = []

    def read_only_views(ui_port: int, token: str, workflow_id: str) -> Any:
        calls.append("GET")
        return next(observations)

    monkeypatch.setattr(module, "_read_workflow_views", read_only_views)
    list_item, detail, result = module._wait_for_success(
        23018,
        "verification-token",
        "workflow-1",
        timeout_seconds=2.0,
    )

    assert len(calls) == 2
    assert list_item["status"] == "succeeded"
    assert detail["status"] == "succeeded"
    assert result["result"]["final_text"] == "4"


def test_direct_success_accepts_terminal_run_with_running_request_cache() -> None:
    module = _load_runner_module()
    assert module is not None, "the Phase 5E isolated smoke runner must exist"
    module._assert_direct_success(
        {
            "request_count": 1,
            "request": {
                "client_request_id": "phase5e0-test",
                "workflow_id": "workflow-1",
                "run_id": "run-1",
                "state": "running",
            },
            "job_count": 1,
            "job_event_count": 5,
            "post_claim_count": 1,
            "job": {
                "job_id": "job-1",
                "workflow_id": "workflow-1",
                "state": "SUCCEEDED",
                "thread_id": "thread-1",
            },
            "dbos_status": "SUCCESS",
            "saved_result": {
                "workflow_id": "workflow-1",
                "result": {"result": {"final_text": "4"}},
            },
        },
        client_request_id="phase5e0-test",
        workflow_id="workflow-1",
        run_id="run-1",
        job_id="job-1",
        thread_id="thread-1",
    )
