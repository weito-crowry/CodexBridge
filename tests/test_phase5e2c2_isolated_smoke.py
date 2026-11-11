from __future__ import annotations

import hashlib
import importlib
from types import ModuleType
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.interpreter import validate_workflow


def _load_runner() -> ModuleType | None:
    try:
        return importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    except ImportError:
        return None


def test_two_job_builder_uses_pinned_fixture_and_runtime_prompt() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    first_job_fixture = yaml.safe_load(module.FIRST_JOB_FIXTURE_PATH.read_text(encoding="utf-8"))
    first_prompt = first_job_fixture["do"][0]["calculate"]["with"]["prompt"]
    runtime_prompt = "runtime supplied prompt"
    workflow_yaml = module.build_two_job_workflow_yaml(second_prompt=runtime_prompt)
    workflow = yaml.safe_load(workflow_yaml)

    validate_workflow(workflow, codex_job_live=True, max_live_codex_jobs=2)
    assert workflow["do"][0]["first"]["with"]["prompt"] == first_prompt
    assert workflow["do"][1]["second"]["with"] == {
        "prompt": runtime_prompt,
        "prompt_from": "${ .result.final_text }",
    }

    payload = module.build_two_job_payload(
        workflow_yaml=workflow_yaml,
        client_request_id="phase5e2c2-fixed-id",
        workspace_root="C:\\isolated\\workspace",
    )
    assert payload == {
        "client_request_id": "phase5e2c2-fixed-id",
        "workflow_yaml": workflow_yaml,
        "input": {},
        "workspace_root": "C:\\isolated\\workspace",
        "max_live_codex_jobs": 2,
    }


def test_runtime_prompt_environment_rejects_an_unapproved_value() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"

    with pytest.raises(module.SmokeAbort) as error:
        module.approved_second_prompt(
            {"CODEXBRIDGE_PHASE5E2C2_SECOND_PROMPT": "unapproved test prompt"}
        )

    assert error.value.code == "second_prompt_hash_mismatch"


def test_job_event_window_counts_sequential_jobs_as_one_active_job() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    events: list[dict[str, Any]] = [
        {"event_id": 1, "job_id": "first", "state": "MAYBE_SENT"},
        {"event_id": 2, "job_id": "first", "state": "ACCEPTED"},
        {"event_id": 3, "job_id": "first", "state": "SUCCEEDED"},
        {"event_id": 4, "job_id": "second", "state": "MAYBE_SENT"},
        {"event_id": 5, "job_id": "second", "state": "RUNNING"},
        {"event_id": 6, "job_id": "second", "state": "SUCCEEDED"},
    ]

    assert module.maximum_active_codex_jobs(events) == 1
    overlapping = [
        events[0],
        events[1],
        {"event_id": 3, "job_id": "second", "state": "MAYBE_SENT"},
        {"event_id": 4, "job_id": "first", "state": "SUCCEEDED"},
        events[5],
    ]
    assert module.maximum_active_codex_jobs(overlapping) == 2


def test_definition_hash_uses_the_exact_stored_snapshot_bytes() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    stored_definition = '{"document":{"name":"fixture"}}\n'

    assert (
        module.definition_sha256(stored_definition)
        == hashlib.sha256(stored_definition.encode("utf-8")).hexdigest()
    )
    assert (
        module.definition_sha256(stored_definition)
        != hashlib.sha256((stored_definition + "\n").encode("utf-8")).hexdigest()
    )
