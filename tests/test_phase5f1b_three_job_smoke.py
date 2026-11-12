from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "phase5f1b_three_job_isolated_smoke.py"


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    if not SCRIPT_PATH.is_file():
        pytest.skip("the Phase 5F-1B isolated runner is not implemented")
    spec = importlib.util.spec_from_file_location(
        "scripts.phase5f1b_three_job_isolated_smoke", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase5f1b_runner_exists() -> None:
    assert SCRIPT_PATH.is_file(), "the Phase 5F-1B isolated runner is not implemented"


def test_fixed_fixture_and_input_are_pinned(smoke: ModuleType) -> None:
    fixture_text, definition, issues = smoke.load_fixed_workflow()

    normalized = fixture_text.replace("\r\n", "\n").encode("utf-8")
    assert hashlib.sha256(normalized).hexdigest() == smoke.FIXTURE_SHA256
    assert definition["document"]["name"] == "three-codex-job-loop"
    assert issues == (
        {"number": 5, "enabled": False},
        {"number": 9, "enabled": True},
        {"number": 17, "enabled": True},
        {"number": 21, "enabled": True},
    )


def test_offline_preflight_rejects_all_five_invalid_cases(smoke: ModuleType) -> None:
    _fixture_text, definition, _issues = smoke.load_fixed_workflow()

    assert smoke._preflight_negative_cases(definition) == (
        "five_issues",
        "four_enabled",
        "duplicate_number",
        "wrong_type",
        "extra_task",
    )


def test_request_body_uses_fixed_three_job_mode(smoke: ModuleType) -> None:
    fixture_text, _definition, _issues = smoke.load_fixed_workflow()
    encoded = smoke.build_three_job_request_body(
        workflow_yaml=fixture_text,
        client_request_id="phase5f1b-test-request",
        workspace_root="C:/isolated/workspace",
    )

    request = json.loads(encoded)
    assert request == {
        "client_request_id": "phase5f1b-test-request",
        "workflow_yaml": fixture_text,
        "input": smoke.FIXED_INPUT,
        "workspace_root": "C:/isolated/workspace",
        "max_live_codex_jobs": 3,
    }


def test_result_contract_requires_exact_skip_and_three_job_projection(
    smoke: ModuleType,
) -> None:
    jobs = [
        {
            "job_id": f"job-{number}",
            "task_identity": (f"do[0].processIssues.iteration[{index}].do[1].processIssue"),
            "request_key": (
                f"workflow-1::do[0].processIssues.iteration[{index}].do[1].processIssue"
            ),
        }
        for index, number in ((1, 9), (2, 17), (3, 21))
    ]
    result = {
        "workflow_id": "workflow-1",
        "definition_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "result": [
            {
                "number": 5,
                "status": "skipped",
                "job_id": None,
                "request_key": None,
                "final_text": None,
            },
            {
                "number": 9,
                "status": "SUCCEEDED",
                "job_id": "job-9",
                "request_key": jobs[0]["request_key"],
                "final_text": "9",
            },
            {
                "number": 17,
                "status": "SUCCEEDED",
                "job_id": "job-17",
                "request_key": jobs[1]["request_key"],
                "final_text": "17",
            },
            {
                "number": 21,
                "status": "SUCCEEDED",
                "job_id": "job-21",
                "request_key": jobs[2]["request_key"],
                "final_text": "21",
            },
        ],
    }

    assert smoke.validate_three_job_result(result, workflow_id="workflow-1", jobs=jobs) == [
        "9",
        "17",
        "21",
    ]
    result["result"].pop()
    with pytest.raises(smoke.SmokeAbort):
        smoke.validate_three_job_result(result, workflow_id="workflow-1", jobs=jobs)


def test_event_order_allows_only_one_sequential_dispatch(smoke: ModuleType) -> None:
    jobs = [{"job_id": f"job-{number}"} for number in (9, 17, 21)]
    events = []
    for number in (9, 17, 21):
        job_id = f"job-{number}"
        events.extend(
            {"event_id": len(events) + 1, "job_id": job_id, "state": state}
            for state in ("PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED")
        )

    maximum_active, dispatch_counts = smoke.validate_job_event_order(events, jobs)

    assert maximum_active == 1
    assert dispatch_counts == {"job-9": 1, "job-17": 1, "job-21": 1}

    overlapping = [
        *events[:4],
        {"event_id": 5, "job_id": "job-17", "state": "MAYBE_SENT"},
        *events[4:],
    ]
    with pytest.raises(smoke.SmokeAbort):
        smoke.validate_job_event_order(overlapping, jobs)


def test_event_order_rejects_unknown_states_and_invalid_transitions(smoke: ModuleType) -> None:
    jobs = [{"job_id": f"job-{number}"} for number in (9, 17, 21)]
    events = []
    for number in (9, 17, 21):
        job_id = f"job-{number}"
        events.extend(
            {"event_id": len(events) + 1, "job_id": job_id, "state": state}
            for state in ("PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED")
        )

    unknown_state = [
        {"event_id": event_id, "job_id": event["job_id"], "state": event["state"]}
        for event_id, event in enumerate(
            [*events[:2], {"job_id": "job-9", "state": "SURPRISE"}, *events[2:]],
            start=1,
        )
    ]
    invalid_transition = [
        events[0],
        events[1],
        {"event_id": 3, "job_id": "job-9", "state": "SUCCEEDED"},
        *events[3:],
    ]
    for invalid_events in (unknown_state, invalid_transition):
        with pytest.raises(smoke.SmokeAbort):
            smoke.validate_job_event_order(invalid_events, jobs)
