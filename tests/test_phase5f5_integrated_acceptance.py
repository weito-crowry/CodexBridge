from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "phase5f5_integrated_isolated_smoke.py"
SINGLE_JOB_FIXTURE = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"


def _load_runner() -> ModuleType:
    assert SCRIPT_PATH.is_file(), "Phase 5F-5 integrated runner is missing"
    spec = importlib.util.spec_from_file_location("phase5f5_integrated_isolated_smoke", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_item_input_keeps_the_approved_dispatch_order_and_capacity() -> None:
    runner = _load_runner()

    assert runner.build_item_input() == {
        "items": [
            {"id": "alpha", "enabled": True, "prompt": "Reply with exactly ALPHA"},
            {"id": "skip", "enabled": False, "prompt": "Reply with exactly SKIP"},
            {"id": "gamma", "enabled": True, "prompt": "Reply with exactly GAMMA"},
        ]
    }
    assert runner.ITEM_MAX_LIVE_CODEX_JOBS == 3


def test_single_job_template_uses_the_approved_bravo_prompt() -> None:
    runner = _load_runner()

    workflow_yaml = runner.build_single_job_workflow(SINGLE_JOB_FIXTURE.read_text(encoding="utf-8"))
    definition = yaml.safe_load(workflow_yaml)

    assert definition["do"][0]["calculate"]["with"]["prompt"] == "Reply with exactly BRAVO"
    assert len(definition["do"]) == 2


def test_repository_workflow_fixtures_pass_the_runner_preflight() -> None:
    runner = _load_runner()

    item_yaml, item_definition, single_yaml, single_definition = runner._validate_templates()

    assert yaml.safe_load(item_yaml) == item_definition
    assert yaml.safe_load(single_yaml) == single_definition
    assert item_definition["do"][0]["processItems"]
    assert single_definition["do"][0]["calculate"]["with"]["prompt"] == ("Reply with exactly BRAVO")


def test_isolation_preflight_allows_state_under_its_dedicated_temp_root(tmp_path: Path) -> None:
    runner = _load_runner()
    temp_root = tmp_path / "isolated"
    temp_root.mkdir()
    state_root = temp_root / "localappdata" / "CodexBridge" / "workflows"
    state_root.mkdir(parents=True)
    workspaces = tuple(temp_root / f"workspace-{label}" for label in ("A", "B", "C"))
    for workspace in workspaces:
        workspace.mkdir()

    runner._ensure_workspace_preflight(
        temp_root=temp_root,
        state_root=state_root,
        workspaces=workspaces,
    )


def test_item_result_checks_skip_summary_order_and_job_projection() -> None:
    runner = _load_runner()
    workflow_id = "9cf8fa22-19f7-42e2-a5d1-49f839973820"
    alpha_key = f"{workflow_id}::do[0].processItems.iteration[0].do[0].processItem"
    gamma_key = f"{workflow_id}::do[0].processItems.iteration[2].do[0].processItem"
    jobs: list[dict[str, Any]] = [
        {"job_id": "job-alpha", "request_key": alpha_key},
        {"job_id": "job-gamma", "request_key": gamma_key},
    ]
    result = {
        "workflow_id": workflow_id,
        "result": {
            "results": [
                {
                    "id": "alpha",
                    "status": "SUCCEEDED",
                    "job_id": "job-alpha",
                    "request_key": alpha_key,
                    "final_text": "ALPHA",
                },
                {
                    "id": "skip",
                    "status": "skipped",
                    "job_id": None,
                    "request_key": None,
                    "final_text": None,
                },
                {
                    "id": "gamma",
                    "status": "SUCCEEDED",
                    "job_id": "job-gamma",
                    "request_key": gamma_key,
                    "final_text": "GAMMA",
                },
            ],
            "summary": {"total": 3, "succeeded": 2, "skipped": 1},
        },
    }

    assert runner.validate_item_result(result, workflow_id=workflow_id, jobs=jobs) == [
        "ALPHA",
        "GAMMA",
    ]
    result["result"]["summary"]["skipped"] = 0
    with pytest.raises(runner.SmokeAbort):
        runner.validate_item_result(result, workflow_id=workflow_id, jobs=jobs)


def test_single_job_api_readback_uses_legacy_projection_fields() -> None:
    runner = _load_runner()
    workflow_id = "9cf8fa22-19f7-42e2-a5d1-49f839973820"
    run_id = "run-9cf8fa22-19f7-42e2-a5d1-49f839973820"
    job = {
        "job_id": "job-bravo",
        "task_identity": "do[0].calculate",
        "state": "SUCCEEDED",
        "thread_id": "thread-bravo",
        "turn_id": "turn-bravo",
        "request_key": f"{workflow_id}::do[0].calculate",
    }
    result = {
        "workflow_id": workflow_id,
        "definition_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "result": {
            "job_id": job["job_id"],
            "request_key": job["request_key"],
            "result": {"final_text": "BRAVO"},
        },
    }
    runner._verify_api_readback(
        ui_port=12345,
        token="test-only-token",
        label="B",
        workflow_id=workflow_id,
        run_id=run_id,
        max_live_codex_jobs=1,
        saved={"jobs": [job], "stored_result": result},
        listing={
            "workflow_id": workflow_id,
            "status": "succeeded",
            "job_id": job["job_id"],
            "thread_id": job["thread_id"],
        },
        detail={
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": "succeeded",
            "worker_state": "completed",
            "terminal": True,
            "job_id": job["job_id"],
            "thread_id": job["thread_id"],
        },
        result=result,
    )
