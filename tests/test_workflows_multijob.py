from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.codex_job_adapter import (
    CodexJobAdapter,
    CodexJobConflictError,
    CodexJobFailedError,
    CodexJobReconciliationRequiredError,
    CodexJobRequestError,
    CodexJobTimeoutError,
)
from codex_bridge.workflows.interpreter import (
    Interpreter,
    OwsError,
    compute_codex_job_request,
    compute_task_action,
    validate_official_schema,
    validate_three_live_codex_job_input,
    validate_workflow,
)


def _two_job_workflow(prompt_from: str = "${ .result.final_text }") -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "two-sequential-jobs",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {"first": {"call": "codex-job", "with": {"prompt": "日本の首都を答えて"}}},
            {
                "second": {
                    "call": "codex-job",
                    "with": {
                        "prompt": "翻訳して: ",
                        "prompt_from": prompt_from,
                    },
                }
            },
        ],
    }


def _three_job_workflow() -> dict[str, Any]:
    fixture = (
        Path(__file__).parents[1]
        / "poc"
        / "ows"
        / "fixtures"
        / ("three_codex_job_loop_workflow.yaml")
    )
    return yaml.safe_load(fixture.read_text(encoding="utf-8"))


def _three_item_workflow() -> dict[str, Any]:
    fixture = (
        Path(__file__).parents[1]
        / "poc"
        / "ows"
        / "fixtures"
        / "conditional_items_codex_workflow.yaml"
    )
    return yaml.safe_load(fixture.read_text(encoding="utf-8"))


_THREE_JOB_INPUT = {
    "issues": [
        {"number": 17, "enabled": True},
        {"number": 5, "enabled": False},
        {"number": 9, "enabled": True},
        {"number": 21, "enabled": True},
    ]
}

_THREE_ITEM_INPUT = {
    "items": [
        {"id": "gamma", "enabled": True, "prompt": "reply gamma"},
        {"id": "beta", "enabled": False, "prompt": "reply beta"},
        {"id": "alpha", "enabled": True, "prompt": "reply alpha"},
    ]
}


def test_conditional_items_fixture_uses_official_ows_schema() -> None:
    schema_path = Path(__file__).parents[1] / "poc" / "ows" / "schema" / "workflow.yaml"
    validate_official_schema(_three_job_workflow(), schema_path=schema_path)
    validate_official_schema(_three_item_workflow(), schema_path=schema_path)


def test_three_item_subset_requires_explicit_mode_and_exact_template() -> None:
    definition = _three_item_workflow()

    with pytest.raises(OwsError):
        validate_workflow(definition, codex_job_live=True)
    with pytest.raises(OwsError):
        validate_workflow(definition, max_live_codex_jobs=3)

    validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)


def test_three_item_interpreter_preserves_order_skips_and_builds_item_prompts() -> None:
    calls: list[tuple[str, str, str]] = []

    def step_runner(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        if operation != "call":
            return compute_task_action(operation, task_spec, task_input, variables)
        assert task_name == "processItem"
        request = compute_codex_job_request(
            task_spec,
            task_input,
            variables,
            max_live_codex_jobs=3,
        )
        request_key = f"workflow::{identity}"
        calls.append((identity, request["prompt"], request_key))
        return {
            "job_id": f"job-{task_input['id']}",
            "request_key": request_key,
            "status": "SUCCEEDED",
            "result": {"final_text": f"done-{task_input['id']}"},
        }

    result = Interpreter(
        _three_item_workflow(),
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=3,
    ).run(_THREE_ITEM_INPUT)

    assert result == {
        "results": [
            {
                "id": "gamma",
                "status": "SUCCEEDED",
                "job_id": "job-gamma",
                "request_key": calls[0][2],
                "final_text": "done-gamma",
            },
            {
                "id": "beta",
                "status": "skipped",
                "job_id": None,
                "request_key": None,
                "final_text": None,
            },
            {
                "id": "alpha",
                "status": "SUCCEEDED",
                "job_id": "job-alpha",
                "request_key": calls[1][2],
                "final_text": "done-alpha",
            },
        ],
        "summary": {"total": 3, "succeeded": 2, "skipped": 1},
    }
    assert [call[0] for call in calls] == [
        "do[0].processItems.iteration[0].do[1].processItem",
        "do[0].processItems.iteration[2].do[1].processItem",
    ]
    assert [call[1] for call in calls] == ["reply gamma", "reply alpha"]


@pytest.mark.parametrize(
    ("items", "summary", "expected_ids"),
    [
        ([], {"total": 0, "succeeded": 0, "skipped": 0}, []),
        (
            [{"id": "only", "enabled": False, "prompt": "keep required"}],
            {"total": 1, "succeeded": 0, "skipped": 1},
            ["only"],
        ),
        (
            [{"id": "one", "enabled": True, "prompt": "one"}],
            {"total": 1, "succeeded": 1, "skipped": 0},
            ["one"],
        ),
        (
            [
                {"id": "one", "enabled": True, "prompt": "one"},
                {"id": "two", "enabled": True, "prompt": "two"},
            ],
            {"total": 2, "succeeded": 2, "skipped": 0},
            ["one", "two"],
        ),
        (
            [
                {"id": "one", "enabled": True, "prompt": "one"},
                {"id": "two", "enabled": True, "prompt": "two"},
                {"id": "three", "enabled": True, "prompt": "three"},
                {"id": "four", "enabled": False, "prompt": "four"},
            ],
            {"total": 4, "succeeded": 3, "skipped": 1},
            ["one", "two", "three", "four"],
        ),
    ],
    ids=["empty", "skip-only", "one-enabled", "two-enabled", "max-four-items"],
)
def test_three_item_interpreter_aggregates_bounded_enabled_counts(
    items: list[dict[str, Any]], summary: dict[str, int], expected_ids: list[str]
) -> None:
    dispatched: list[str] = []

    def step_runner(
        identity: str,
        _task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        if operation != "call":
            return compute_task_action(operation, task_spec, task_input, variables)
        dispatched.append(task_input["id"])
        return {
            "job_id": f"job-{task_input['id']}",
            "request_key": f"workflow::{identity}",
            "status": "SUCCEEDED",
            "result": {"final_text": task_input["id"]},
        }

    result = Interpreter(
        _three_item_workflow(),
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=3,
    ).run({"items": items})

    assert [row["id"] for row in result["results"]] == expected_ids
    assert result["summary"] == summary
    assert result["summary"]["total"] == (
        result["summary"]["succeeded"] + result["summary"]["skipped"]
    )
    assert dispatched == [item["id"] for item in items if item["enabled"]]


@pytest.mark.parametrize(
    "input_data",
    [
        {"items": [{"id": "x", "enabled": True, "prompt": "p"}] * 5},
        {"items": [{"id": str(index), "enabled": index < 4, "prompt": "p"} for index in range(5)]},
        {
            "items": [
                {"id": "duplicate", "enabled": False, "prompt": "p"},
                {"id": "duplicate", "enabled": True, "prompt": "p"},
            ]
        },
        {"items": [{"id": "_bad", "enabled": True, "prompt": "p"}]},
        {"items": [{"id": "bad id", "enabled": True, "prompt": "p"}]},
        {"items": [{"id": "x" * 65, "enabled": True, "prompt": "p"}]},
        {"items": [{"id": "x", "enabled": 1, "prompt": "p"}]},
        {"items": [{"id": "x", "enabled": True, "prompt": ""}]},
        {"items": [{"id": "x", "enabled": False, "prompt": " \t "}]},
        {"items": [{"id": "x", "enabled": True, "prompt": "p" * (16 * 1024 + 1)}]},
        {"items": [{"id": "x", "enabled": True, "prompt": "bad\x01prompt"}]},
        {"items": [{"id": "x", "enabled": True, "prompt": "p", "extra": 1}]},
        {"items": [{"id": "x", "enabled": True}]},
        {"items": [], "issues": []},
        {"issues": []},
        {"items": "not-an-array"},
        {"items": [], "extra": True},
    ],
    ids=[
        "five-items",
        "four-enabled",
        "duplicate-id",
        "invalid-leading-character",
        "invalid-character",
        "id-too-long",
        "non-boolean-enabled",
        "empty-prompt",
        "whitespace-prompt",
        "prompt-too-long",
        "prompt-control-character",
        "extra-item-key",
        "missing-prompt",
        "mixed-issues-and-items",
        "issues-input-to-items-template",
        "non-array-items",
        "extra-input-key",
    ],
)
def test_three_item_input_validation_fails_before_any_step(input_data: Any) -> None:
    steps: list[str] = []

    def step_runner(identity: str, *_args: Any) -> Any:
        steps.append(identity)
        return None

    with pytest.raises(OwsError):
        Interpreter(
            _three_item_workflow(),
            step_runner=step_runner,
            codex_job_live=True,
            max_live_codex_jobs=3,
        ).run(input_data)

    assert steps == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda workflow: workflow["do"][0]["processItems"]["do"][0]["choose"]["switch"][0][
            "enabled"
        ].update({"when": "${ true }"}),
        lambda workflow: workflow["do"][0]["processItems"]["do"][1]["processItem"]["with"].update(
            {"prompt_from": "${ .unsafe }"}
        ),
        lambda workflow: workflow["do"][0]["processItems"]["do"][1]["processItem"]["output"].update(
            {"as": "${ . }"}
        ),
        lambda workflow: workflow["output"].update({"as": "${ .results }"}),
        lambda workflow: workflow["do"][0]["processItems"]["do"][2]["skipItem"]["set"].update(
            {"unexpected": True}
        ),
    ],
    ids=["condition", "prompt-expression", "row-expression", "aggregate-expression", "skip-shape"],
)
def test_three_item_subset_rejects_unapproved_condition_and_result_shapes(
    mutate: Any,
) -> None:
    definition = _three_item_workflow()
    mutate(definition)

    with pytest.raises(OwsError):
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)


def test_three_job_subset_requires_explicit_mode_and_pinned_schema() -> None:
    definition = _three_job_workflow()

    with pytest.raises(OwsError, match="may not be nested under for"):
        validate_workflow(definition, codex_job_live=True)
    with pytest.raises(OwsError, match="requires codex-job live mode"):
        validate_workflow(definition, max_live_codex_jobs=3)

    validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda workflow: workflow["do"].append({"extra": {"set": {"x": 1}}}),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][1]["processIssue"].update(
            {"call": "mock-job"}
        ),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][1]["processIssue"].pop("then"),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][0]["choose"]["switch"][0][
            "enabled"
        ].update({"when": "${ true }"}),
        lambda workflow: workflow["do"][0]["processIssues"]["for"].update({"while": "${ true }"}),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][1]["processIssue"].update(
            {"call": "unsupported-target"}
        ),
        lambda workflow: workflow.update({"output": {"as": "${ . }"}}),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][1]["processIssue"][
            "output"
        ].update({"as": "${ . }"}),
        lambda workflow: workflow["do"][0]["processIssues"]["do"][1]["processIssue"]["with"].update(
            {"prompt_from": "${ .unsafe }"}
        ),
    ],
    ids=[
        "top-level-task",
        "mock-mix",
        "missing-exit",
        "switch-shape",
        "loop-while",
        "other-call-target",
        "top-level-output",
        "output-expression",
        "invalid-prompt-from",
    ],
)
def test_three_job_subset_rejects_unapproved_shapes(mutate: Any) -> None:
    definition = _three_job_workflow()
    mutate(definition)

    with pytest.raises(OwsError):
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)


@pytest.mark.parametrize(
    "input_data",
    [
        {"issues": [{"number": index + 1, "enabled": False} for index in range(5)]},
        {"issues": [{"number": index + 1, "enabled": True} for index in range(4)]},
        {"issues": [{"number": 1, "enabled": True}, {"number": 1, "enabled": False}]},
        {"issues": [{"number": 0, "enabled": True}]},
        {"issues": [{"number": -1, "enabled": True}]},
        {"issues": [{"number": True, "enabled": True}]},
        {"issues": [{"number": 1, "enabled": 1}]},
        {"issues": [{"number": 1, "enabled": True, "prompt": "unsafe"}]},
        {"issues": [], "prompt": "unsafe"},
        {"issues": {}},
        {"issues": [{"number": 1}]},
    ],
    ids=[
        "five-items",
        "four-enabled",
        "duplicate-number",
        "zero-number",
        "negative-number",
        "boolean-number",
        "non-boolean-enabled",
        "extra-item-key",
        "extra-input-key",
        "non-array-issues",
        "missing-enabled",
    ],
)
def test_three_job_input_validation_fails_closed(input_data: Any) -> None:
    with pytest.raises(OwsError):
        validate_three_live_codex_job_input(input_data)


def test_three_job_interpreter_rechecks_input_before_first_step() -> None:
    steps: list[str] = []

    def step_runner(identity: str, *_args: Any) -> Any:
        steps.append(identity)
        return None

    with pytest.raises(OwsError, match="at most four items"):
        Interpreter(
            _three_job_workflow(),
            step_runner=step_runner,
            codex_job_live=True,
            max_live_codex_jobs=3,
        ).run({"issues": [{"number": index + 1, "enabled": False} for index in range(5)]})

    assert steps == []


def test_three_job_interpreter_sorts_skips_and_builds_unique_prompts_and_identities() -> None:
    calls: list[tuple[str, str, str]] = []

    def step_runner(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        if operation != "call":
            return compute_task_action(operation, task_spec, task_input, variables)
        assert task_name == "processIssue"
        request = compute_codex_job_request(
            task_spec,
            task_input,
            variables,
            max_live_codex_jobs=3,
        )
        number = task_input["number"]
        calls.append((identity, request["prompt"], f"workflow::{identity}"))
        return {
            "job_id": f"job-{number}",
            "request_key": f"workflow::{identity}",
            "status": "SUCCEEDED",
            "result": {"final_text": str(number)},
        }

    result = Interpreter(
        _three_job_workflow(),
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=3,
    ).run(_THREE_JOB_INPUT)

    assert [item["number"] for item in result] == [5, 9, 17, 21]
    assert [item["status"] for item in result] == ["skipped", "SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert result[0] == {
        "number": 5,
        "status": "skipped",
        "job_id": None,
        "request_key": None,
        "final_text": None,
    }
    assert [item["final_text"] for item in result[1:]] == ["9", "17", "21"]
    assert [call[0] for call in calls] == [
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    ]
    assert [call[1] for call in calls] == [
        "Reply with only the synthetic issue number: 9",
        "Reply with only the synthetic issue number: 17",
        "Reply with only the synthetic issue number: 21",
    ]
    assert len({call[0] for call in calls}) == len({call[2] for call in calls}) == 3


def test_three_job_input_allows_zero_enabled_and_empty_array() -> None:
    calls: list[str] = []

    def step_runner(*args: Any) -> Any:
        identity, _, operation, task_spec, task_input, variables = args
        if operation != "call":
            return compute_task_action(operation, task_spec, task_input, variables)
        calls.append(str(identity))
        raise AssertionError("disabled issues must not dispatch")

    definition = _three_job_workflow()
    empty_result = Interpreter(
        definition,
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=3,
    ).run({"issues": []})
    skipped_result = Interpreter(
        definition,
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=3,
    ).run({"issues": [{"number": 8, "enabled": False}, {"number": 3, "enabled": False}]})

    assert empty_result == []
    assert [item["number"] for item in skipped_result] == [3, 8]
    assert calls == []


def test_two_codex_jobs_require_explicit_opt_in_and_pass_pinned_schema() -> None:
    definition = _two_job_workflow()

    with pytest.raises(OwsError, match="exactly one"):
        validate_workflow(definition, codex_job_live=True)

    validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=2)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda workflow: workflow["do"].append(
            {"third": {"call": "codex-job", "with": {"prompt": "third"}}}
        ),
        lambda workflow: workflow["do"][1].update({"second": {"call": "mock-job", "with": {}}}),
        lambda workflow: workflow["do"][0].update(
            {"first": {"call": "codex-job", "then": "continue", "with": {"prompt": "first"}}}
        ),
        lambda workflow: workflow["do"][1]["second"].update({"then": "continue"}),
        lambda workflow: workflow["do"].insert(1, {"middle": {"set": {"value": 1}}}),
        lambda workflow: workflow["do"].insert(
            1, {"middle": {"switch": [{"default": {"then": "continue"}}]}}
        ),
        lambda workflow: workflow["do"].insert(
            1, {"middle": {"do": [{"set-value": {"set": {"value": 1}}}]}}
        ),
        lambda workflow: workflow["do"][0].update(
            {
                "repeat": {
                    "for": {"in": "${ .items }"},
                    "do": [{"nested": {"call": "codex-job", "with": {"prompt": "nested"}}}],
                }
            }
        ),
        lambda workflow: workflow["do"][1]["second"].update({"input": {"from": "${ . }"}}),
        lambda workflow: workflow["do"][1]["second"].update({"output": {"as": "${ . }"}}),
        lambda workflow: workflow["do"][1]["second"].update({"export": {"as": "${ . }"}}),
        lambda workflow: workflow["do"][0]["first"]["with"].update(
            {"prompt_from": "${ .result.final_text }"}
        ),
        lambda workflow: workflow["do"][1]["second"]["with"].pop("prompt_from"),
        lambda workflow: workflow["do"][1].update({"first": workflow["do"][1]["second"]}),
        lambda workflow: workflow["do"][1]["second"]["with"].update({"extra": "no"}),
        lambda workflow: workflow.update({"input": {"from": "${ . }"}}),
        lambda workflow: workflow.update({"output": {"as": "${ . }"}}),
    ],
    ids=[
        "third-job",
        "mock-mix",
        "then",
        "second-then",
        "middle-set",
        "middle-switch",
        "middle-do",
        "nested-for",
        "task-input",
        "task-output",
        "task-export",
        "first-prompt-from",
        "missing-prompt-from",
        "duplicate-name",
        "extra-prompt-key",
        "top-input",
        "top-output",
    ],
)
def test_two_job_subset_rejects_unapproved_shapes(mutate: Any) -> None:
    definition = _two_job_workflow()
    mutate(definition)

    with pytest.raises(OwsError):
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=2)


@pytest.mark.parametrize(
    "prompt_from",
    ["${ .result[", "${ $private.result }", "prefix ${ .result.final_text }", 4],
)
def test_two_job_subset_rejects_invalid_prompt_from(prompt_from: Any) -> None:
    with pytest.raises(OwsError):
        validate_workflow(
            _two_job_workflow(prompt_from),
            codex_job_live=True,
            max_live_codex_jobs=2,
        )


@pytest.mark.parametrize("limit", [True, False, 0, 4, 9, "2", None])
def test_workflow_rejects_invalid_max_live_codex_jobs(limit: Any) -> None:
    with pytest.raises(OwsError, match="max_live_codex_jobs"):
        validate_workflow(
            _two_job_workflow(),
            codex_job_live=True,
            max_live_codex_jobs=limit,
        )


def test_interpreter_passes_first_final_text_literally_into_second_prompt() -> None:
    definition = _two_job_workflow()
    calls: list[tuple[str, str]] = []
    second_result = {"job_id": "second-id", "status": "SUCCEEDED"}

    def step_runner(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        assert operation == "call"
        request = compute_codex_job_request(
            task_spec,
            task_input,
            variables,
            max_live_codex_jobs=2,
        )
        calls.append((identity, request["prompt"]))
        if task_name == "first":
            return {
                "job_id": "first-id",
                "status": "SUCCEEDED",
                "result": {"final_text": "東京\n😊"},
            }
        return second_result

    result = Interpreter(
        definition,
        step_runner=step_runner,
        codex_job_live=True,
        max_live_codex_jobs=2,
    ).run(None)

    assert calls == [
        ("do[0].first", "日本の首都を答えて"),
        ("do[1].second", "翻訳して: 東京\n😊"),
    ]
    assert result is second_result


@pytest.mark.parametrize(
    ("first_result", "expression"),
    [
        ({"result": {}}, "${ .result.final_text }"),
        ({"result": {"final_text": None}}, "${ .result.final_text }"),
        ({"result": {"final_text": ""}}, "${ .result.final_text }"),
        ({"result": {"final_text": ["a"]}}, "${ .result.final_text }"),
        ({"result": {"final_text": "a"}}, '${ "a", "b" }'),
        ({"result": {"final_text": "a"}}, '${ error("PRIVATE-RESULT") }'),
        ({"result": {"final_text": "x" * (16 * 1024)}}, "${ .result.final_text }"),
        ({"result": {"final_text": "bad\x01text"}}, "${ .result.final_text }"),
    ],
    ids=[
        "missing",
        "null",
        "empty",
        "array",
        "multiple-outputs",
        "jq-error",
        "over-limit",
        "control-character",
    ],
)
def test_invalid_prompt_from_never_runs_second_step(first_result: Any, expression: str) -> None:
    definition = _two_job_workflow(expression)
    started_names: list[str] = []

    def step_runner(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        request = compute_codex_job_request(
            task_spec,
            task_input,
            variables,
            max_live_codex_jobs=2,
        )
        started_names.append(task_name)
        return first_result if task_name == "first" else {"prompt": request["prompt"]}

    with pytest.raises(OwsError) as error:
        Interpreter(
            definition,
            step_runner=step_runner,
            codex_job_live=True,
            max_live_codex_jobs=2,
        ).run(None)

    assert started_names == ["first"]
    assert "PRIVATE-RESULT" not in str(error.value)
    assert "bad\x01text" not in str(error.value)


def test_second_prompt_accepts_exact_utf8_limit_without_normalizing() -> None:
    definition = _two_job_workflow()
    prefix = "訳: "
    definition["do"][1]["second"]["with"]["prompt"] = prefix
    final_text = "x" * (16 * 1024 - len(prefix.encode("utf-8")))
    request = compute_codex_job_request(
        definition["do"][1]["second"],
        {"result": {"final_text": final_text}},
        {},
        max_live_codex_jobs=2,
    )

    assert request["prompt"] == prefix + final_text
    assert len(request["prompt"].encode("utf-8")) == 16 * 1024


def test_single_job_keeps_template_looking_prompt_literal() -> None:
    prompt = "${ . }"
    request = compute_codex_job_request({"with": {"prompt": prompt}}, {"value": 1})

    assert request == {"prompt": prompt}


def test_v2_adapter_allows_two_jobs_only_with_explicit_limit(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")

    first = adapter.prepare_job(
        "workflow-12345678",
        "do[0].first",
        "first prompt",
        run_dir,
        port=18001,
        max_jobs_per_workflow=2,
    )
    second = adapter.prepare_job(
        "workflow-12345678",
        "do[1].second",
        "second prompt",
        run_dir,
        port=18001,
        max_jobs_per_workflow=2,
    )
    replay = adapter.prepare_job(
        "workflow-12345678",
        "do[1].second",
        "second prompt",
        run_dir,
        port=18001,
        max_jobs_per_workflow=2,
    )

    assert replay["job_id"] == second["job_id"]
    assert replay["request_digest"] == second["request_digest"]
    assert second["job_id"] != first["job_id"]
    assert [job["task_identity"] for job in adapter.get_jobs_for_workflow("workflow-12345678")] == [
        "do[0].first",
        "do[1].second",
    ]
    with pytest.raises(CodexJobConflictError, match="workflow job limit"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[2].third",
            "third prompt",
            run_dir,
            port=18001,
            max_jobs_per_workflow=2,
        )
    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[1].second",
            "changed prompt",
            run_dir,
            port=18001,
            max_jobs_per_workflow=2,
        )
    with pytest.raises(CodexJobConflictError, match="one live job is allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[3].default-limit",
            "default prompt",
            run_dir,
            port=18001,
        )

    with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE workflow_id = ?", ("workflow-12345678",)
            ).fetchone()[0]
            == 2
        )


def test_v2_adapter_allows_three_jobs_with_stable_replay_and_capacity_limit(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run-three"
    run_dir.mkdir()
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    workflow_id = "workflow-three-12345678"
    identities = (
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    )
    jobs = [
        adapter.prepare_job(
            workflow_id,
            identity,
            f"prompt {index}",
            run_dir,
            port=18001,
            max_jobs_per_workflow=3,
        )
        for index, identity in enumerate(identities, start=1)
    ]
    replay = adapter.prepare_job(
        workflow_id,
        identities[1],
        "prompt 2",
        run_dir,
        port=18001,
        max_jobs_per_workflow=3,
    )

    assert replay["job_id"] == jobs[1]["job_id"]
    assert replay["request_digest"] == jobs[1]["request_digest"]
    assert len({job["job_id"] for job in jobs}) == 3
    assert len({job["request_key"] for job in jobs}) == 3
    assert [job["task_identity"] for job in adapter.get_jobs_for_workflow(workflow_id)] == list(
        identities
    )
    with pytest.raises(CodexJobConflictError, match="workflow job limit"):
        adapter.prepare_job(
            workflow_id,
            "do[0].processIssues.iteration[4].do[1].processIssue",
            "prompt 4",
            run_dir,
            port=18001,
            max_jobs_per_workflow=3,
        )
    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            workflow_id,
            identities[1],
            "changed prompt",
            run_dir,
            port=18001,
            max_jobs_per_workflow=3,
        )


@pytest.mark.parametrize("limit", [True, False, 0, 4, 7, "2", None])
def test_adapter_rejects_invalid_per_workflow_job_limit(tmp_path: Path, limit: Any) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="max_jobs_per_workflow"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].first",
            "first prompt",
            run_dir,
            port=18001,
            max_jobs_per_workflow=limit,
        )


def test_shutdown_ownership_is_incomplete_when_either_job_may_have_been_sent(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    first = adapter.prepare_job(
        "workflow-12345678",
        "do[0].first",
        "first prompt",
        run_dir,
        port=18001,
        max_jobs_per_workflow=2,
    )
    second = adapter.prepare_job(
        "workflow-12345678",
        "do[1].second",
        "second prompt",
        run_dir,
        port=18001,
        max_jobs_per_workflow=2,
    )
    assert adapter.claim_post(second["job_id"])

    turns, complete = CodexJobAdapter.read_shutdown_ownership(run_dir / "codex_jobs.sqlite")

    assert turns == set()
    assert not complete
    assert first["state"] == "PREPARED"


class _FakeDBOS:
    workflow_id = "workflow-multijob-12345678"

    @staticmethod
    def sleep(seconds: float) -> None:
        del seconds

    @staticmethod
    def step(*, name: str):
        del name

        def decorate(function: Any) -> Any:
            return function

        return decorate


class _FakeCodexHTTPClient:
    events: list[tuple[str, Any]] = []
    outcomes: dict[str, dict[str, Any]] = {}
    threads: dict[str, tuple[str, str]] = {}

    def __init__(self, *, port: int) -> None:
        self.port = port

    def preflight(self) -> None:
        self.events.extend(
            [
                ("GET", "/healthz"),
                ("GET", "/ui-api/status"),
                ("GET", "/ui-api/control/capabilities"),
            ]
        )

    def create_thread(self, request: dict[str, Any]) -> dict[str, str]:
        self.events.append(("POST", request.copy()))
        outcome = self.outcomes.get(request["prompt"], {})
        if outcome.get("post_error"):
            from codex_bridge.workflows.http_client import CodexHTTPError

            raise CodexHTTPError("fake ambiguous POST")
        number = len(self.threads) + 1
        thread_id = f"thread-{number:08d}"
        turn_id = f"turn-{number:08d}"
        self.threads[thread_id] = (turn_id, request["prompt"])
        return {"thread_id": thread_id, "turn_id": turn_id, "state": "in_progress"}

    def observe_thread(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.events.append(("GET", (thread_id, turn_id)))
        expected_turn, prompt = self.threads[thread_id]
        assert turn_id == expected_turn
        outcome = self.outcomes.get(prompt, {})
        state = outcome.get("state", "completed")
        final_text = outcome.get("final_text", "result for " + prompt)
        return {
            "status": {"thread_id": thread_id, "turn_id": turn_id, "state": state},
            "turns": [{"id": turn_id, "status": state}],
            "items": (
                [
                    {
                        "turn_id": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": final_text,
                        },
                    }
                ]
                if state == "completed"
                else []
            ),
        }


class _FakeDurableSteps:
    def __init__(self) -> None:
        self.cache: dict[tuple[str, str, int], tuple[tuple[Any, ...], Any]] = {}
        self.executed: list[tuple[str, str, int]] = []
        self.cache_hits = 0

    def factory(self, identity: str, stage: str, poll_index: int = 0):
        key = (identity, stage, poll_index)

        def run(*arguments: Any) -> Any:
            cached = self.cache.get(key)
            if cached is not None:
                previous_arguments, result = cached
                assert previous_arguments == arguments
                self.cache_hits += 1
                return result
            from codex_bridge.workflows import worker

            function = (
                worker._durable_codex_job_start_step
                if stage == "start"
                else worker._durable_codex_job_poll_step
            )
            result = function(*arguments)
            self.cache[key] = (arguments, result)
            self.executed.append(key)
            return result

        return run


def _run_fake_worker(
    run_dir: Path,
    workspace_root: Path,
    *,
    max_polls: int = 2,
    max_live_codex_jobs: int = 2,
    definition: dict[str, Any] | None = None,
    input_data: Any = None,
    steps: _FakeDurableSteps | None = None,
    monkeypatch: pytest.MonkeyPatch,
    reset_http: bool = True,
) -> Any:
    from codex_bridge.workflows import worker

    definition = _two_job_workflow() if definition is None else definition
    definition_text = yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
    definition_sha256 = worker.sha256_text(definition_text)
    input_sha256 = worker.sha256_json(input_data)
    if reset_http:
        _FakeCodexHTTPClient.events = []
        _FakeCodexHTTPClient.threads = {}
    _FakeDBOS.workflow_id = "workflow-multijob-12345678"
    selected_steps = steps or _FakeDurableSteps()
    monkeypatch.setattr(worker, "DBOS", _FakeDBOS)
    monkeypatch.setattr(worker, "CodexHTTPClient", _FakeCodexHTTPClient)
    monkeypatch.setattr(worker, "_durable_codex_job_step", selected_steps.factory)
    control = {
        "codex_job_live": True,
        "max_live_codex_jobs": max_live_codex_jobs,
        "codex_bridge_port": 18001,
        "codex_job_max_polls": max_polls,
        "codex_job_poll_interval_seconds": 0.05,
        "workspace_root": str(workspace_root),
        "allowed_roots": [str(workspace_root.parent)],
    }
    return worker.execute_ows_workflow.__wrapped__(
        definition_text,
        definition_sha256,
        input_data,
        input_sha256,
        str(run_dir / "task-events.jsonl"),
        control,
        10,
    )


def test_worker_completes_first_receipt_before_starting_second_and_replays_without_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    run_dir.mkdir()
    workspace_root.mkdir()
    _FakeCodexHTTPClient.outcomes = {"日本の首都を答えて": {"final_text": "東京\n"}}
    steps = _FakeDurableSteps()

    first_result = _run_fake_worker(run_dir, workspace_root, steps=steps, monkeypatch=monkeypatch)
    first_post = next(event for kind, event in _FakeCodexHTTPClient.events if kind == "POST")
    second_post = [event for kind, event in _FakeCodexHTTPClient.events if kind == "POST"][1]
    events = _FakeCodexHTTPClient.events
    first_post_index = next(
        index for index, item in enumerate(events) if item == ("POST", first_post)
    )
    first_receipt_index = next(
        index
        for index, item in enumerate(events)
        if item[0] == "GET" and isinstance(item[1], tuple)
    )
    second_post_index = next(
        index for index, item in enumerate(events) if item == ("POST", second_post)
    )
    jobs = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        _FakeDBOS.workflow_id
    )
    post_count = sum(kind == "POST" for kind, _ in events)
    second_result = _run_fake_worker(
        run_dir, workspace_root, steps=steps, monkeypatch=monkeypatch, reset_http=False
    )

    assert first_result["status"] == "SUCCEEDED"
    assert first_result["result"]["final_text"] == "result for 翻訳して: 東京\n"
    assert first_post["prompt"] == "日本の首都を答えて"
    assert second_post["prompt"] == "翻訳して: 東京\n"
    assert first_post_index < first_receipt_index < second_post_index
    assert steps.executed == [
        ("do[0].first", "start", 0),
        ("do[0].first", "status", 0),
        ("do[1].second", "start", 0),
        ("do[1].second", "status", 0),
    ]
    assert [job["task_identity"] for job in jobs] == ["do[0].first", "do[1].second"]
    assert [job["state"] for job in jobs] == ["SUCCEEDED", "SUCCEEDED"]
    assert [job["thread_id"] for job in jobs] == ["thread-00000001", "thread-00000002"]
    assert [job["turn_id"] for job in jobs] == ["turn-00000001", "turn-00000002"]
    assert all(job["receipt"]["result"]["final_text"] for job in jobs)
    assert all(event[1]["sandbox_mode"] == "inherit" for event in events if event[0] == "POST")
    assert all(event[1]["confirm_full_access"] is False for event in events if event[0] == "POST")
    assert all(event[1]["cwd"] == str(workspace_root) for event in events if event[0] == "POST")
    assert events.count(("GET", "/healthz")) == 2
    assert events.count(("GET", "/ui-api/status")) == 2
    assert events.count(("GET", "/ui-api/control/capabilities")) == 2
    assert second_result == first_result
    assert sum(kind == "POST" for kind, _ in _FakeCodexHTTPClient.events) == post_count
    assert steps.cache_hits >= 4


def test_worker_completes_three_sorted_jobs_and_replays_without_new_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state-three"
    workspace_root = tmp_path / "workspace-three"
    run_dir.mkdir()
    workspace_root.mkdir()
    _FakeCodexHTTPClient.outcomes = {
        "Reply with only the synthetic issue number: 9": {"final_text": "9"},
        "Reply with only the synthetic issue number: 17": {"final_text": "17"},
        "Reply with only the synthetic issue number: 21": {"final_text": "21"},
    }
    steps = _FakeDurableSteps()
    first_result = _run_fake_worker(
        run_dir,
        workspace_root,
        definition=_three_job_workflow(),
        input_data=_THREE_JOB_INPUT,
        max_live_codex_jobs=3,
        steps=steps,
        monkeypatch=monkeypatch,
    )
    events = list(_FakeCodexHTTPClient.events)
    replayed = _run_fake_worker(
        run_dir,
        workspace_root,
        definition=_three_job_workflow(),
        input_data=_THREE_JOB_INPUT,
        max_live_codex_jobs=3,
        steps=steps,
        monkeypatch=monkeypatch,
        reset_http=False,
    )
    adapter = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    jobs = adapter.get_jobs_for_workflow(_FakeDBOS.workflow_id)
    job_statuses = [adapter.get_job_status(job_id=job["job_id"]) for job in jobs]
    post_requests = [event for kind, event in events if kind == "POST"]
    observe_indexes = [
        index
        for index, (kind, value) in enumerate(events)
        if kind == "GET" and isinstance(value, tuple)
    ]
    post_indexes = [index for index, (kind, _value) in enumerate(events) if kind == "POST"]

    assert [item["number"] for item in first_result] == [5, 9, 17, 21]
    assert [item["status"] for item in first_result] == [
        "skipped",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    assert [item["final_text"] for item in first_result[1:]] == ["9", "17", "21"]
    assert [request["prompt"] for request in post_requests] == [
        "Reply with only the synthetic issue number: 9",
        "Reply with only the synthetic issue number: 17",
        "Reply with only the synthetic issue number: 21",
    ]
    assert len({job["job_id"] for job in jobs}) == 3
    assert len({job["request_key"] for job in job_statuses}) == 3
    assert [job["task_identity"] for job in jobs] == [
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    ]
    assert [job["state"] for job in jobs] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert len(post_indexes) == len(observe_indexes) == 3
    assert all(post_indexes[index] < observe_indexes[index] for index in range(3))
    assert replayed == first_result
    assert sum(kind == "POST" for kind, _event in _FakeCodexHTTPClient.events) == 3
    assert steps.cache_hits >= 6


def test_worker_completes_three_items_in_input_order_and_replays_without_new_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state-items"
    workspace_root = tmp_path / "workspace-items"
    run_dir.mkdir()
    workspace_root.mkdir()
    _FakeCodexHTTPClient.outcomes = {
        "reply gamma": {"final_text": "gamma answer"},
        "reply alpha": {"final_text": "alpha answer"},
    }
    steps = _FakeDurableSteps()

    first_result = _run_fake_worker(
        run_dir,
        workspace_root,
        definition=_three_item_workflow(),
        input_data=_THREE_ITEM_INPUT,
        max_live_codex_jobs=3,
        steps=steps,
        monkeypatch=monkeypatch,
    )
    post_count = sum(kind == "POST" for kind, _event in _FakeCodexHTTPClient.events)
    replayed = _run_fake_worker(
        run_dir,
        workspace_root,
        definition=_three_item_workflow(),
        input_data=_THREE_ITEM_INPUT,
        max_live_codex_jobs=3,
        steps=steps,
        monkeypatch=monkeypatch,
        reset_http=False,
    )
    jobs = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        _FakeDBOS.workflow_id
    )
    events = list(_FakeCodexHTTPClient.events)
    post_requests = [event for kind, event in _FakeCodexHTTPClient.events if kind == "POST"]
    post_indexes = [index for index, (kind, _event) in enumerate(events) if kind == "POST"]
    observe_indexes = [
        index
        for index, (kind, event) in enumerate(events)
        if kind == "GET" and isinstance(event, tuple)
    ]

    assert first_result == {
        "results": [
            {
                "id": "gamma",
                "status": "SUCCEEDED",
                "job_id": jobs[0]["job_id"],
                "request_key": f"{_FakeDBOS.workflow_id}::{jobs[0]['task_identity']}",
                "final_text": "gamma answer",
            },
            {
                "id": "beta",
                "status": "skipped",
                "job_id": None,
                "request_key": None,
                "final_text": None,
            },
            {
                "id": "alpha",
                "status": "SUCCEEDED",
                "job_id": jobs[1]["job_id"],
                "request_key": f"{_FakeDBOS.workflow_id}::{jobs[1]['task_identity']}",
                "final_text": "alpha answer",
            },
        ],
        "summary": {"total": 3, "succeeded": 2, "skipped": 1},
    }
    assert [request["prompt"] for request in post_requests] == ["reply gamma", "reply alpha"]
    assert len(post_indexes) == len(observe_indexes) == 2
    assert post_indexes[0] < observe_indexes[0] < post_indexes[1] < observe_indexes[1]
    assert [job["task_identity"] for job in jobs] == [
        "do[0].processItems.iteration[0].do[1].processItem",
        "do[0].processItems.iteration[2].do[1].processItem",
    ]
    assert replayed == first_result
    assert sum(kind == "POST" for kind, _event in _FakeCodexHTTPClient.events) == post_count == 2


@pytest.mark.parametrize(
    ("outcome", "expected_error", "expected_state"),
    [
        ({"state": "needs_approval"}, CodexJobReconciliationRequiredError, "ACCEPTED"),
        ({"state": "failed"}, CodexJobFailedError, "FAILED"),
        ({"post_error": True}, CodexJobReconciliationRequiredError, "MAYBE_SENT"),
    ],
    ids=["needs-approval", "failed", "maybe-sent"],
)
def test_worker_does_not_post_later_item_after_second_item_is_not_succeeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: dict[str, Any],
    expected_error: type[Exception],
    expected_state: str,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state-items-stop"
    workspace_root = tmp_path / "workspace-items-stop"
    run_dir.mkdir()
    workspace_root.mkdir()
    input_data = {
        "items": [
            {"id": "first", "enabled": True, "prompt": "reply first"},
            {"id": "second", "enabled": True, "prompt": "reply second"},
            {"id": "third", "enabled": True, "prompt": "reply third"},
        ]
    }
    _FakeCodexHTTPClient.outcomes = {
        "reply first": {"final_text": "first done"},
        "reply second": outcome,
    }

    with pytest.raises(expected_error):
        _run_fake_worker(
            run_dir,
            workspace_root,
            definition=_three_item_workflow(),
            input_data=input_data,
            max_live_codex_jobs=3,
            monkeypatch=monkeypatch,
        )

    jobs = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        _FakeDBOS.workflow_id
    )
    post_requests = [event for kind, event in _FakeCodexHTTPClient.events if kind == "POST"]
    assert len(jobs) == 2
    assert [job["state"] for job in jobs] == ["SUCCEEDED", expected_state]
    assert [request["prompt"] for request in post_requests] == ["reply first", "reply second"]
    assert "reply third" not in [request["prompt"] for request in post_requests]


@pytest.mark.parametrize(
    ("second_outcome", "expected_error", "expected_state", "expected_status"),
    [
        (
            {"state": "needs_approval"},
            CodexJobReconciliationRequiredError,
            "ACCEPTED",
            "NEEDS_RECONCILIATION",
        ),
        ({"state": "failed"}, CodexJobFailedError, "FAILED", "FAILED"),
        (
            {"post_error": True},
            CodexJobReconciliationRequiredError,
            "MAYBE_SENT",
            "NEEDS_RECONCILIATION",
        ),
    ],
    ids=["second-needs-reconciliation", "second-failed", "second-post-ambiguous"],
)
def test_worker_does_not_dispatch_third_job_after_second_is_not_succeeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_outcome: dict[str, Any],
    expected_error: type[Exception],
    expected_state: str,
    expected_status: str,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state-unknown-second"
    workspace_root = tmp_path / "workspace-unknown-second"
    run_dir.mkdir()
    workspace_root.mkdir()
    _FakeCodexHTTPClient.outcomes = {
        "Reply with only the synthetic issue number: 9": {"final_text": "9"},
        "Reply with only the synthetic issue number: 17": second_outcome,
    }

    with pytest.raises(expected_error):
        _run_fake_worker(
            run_dir,
            workspace_root,
            definition=_three_job_workflow(),
            input_data=_THREE_JOB_INPUT,
            max_live_codex_jobs=3,
            monkeypatch=monkeypatch,
        )

    jobs = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        _FakeDBOS.workflow_id
    )
    second_status = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_job_status(
        job_id=jobs[1]["job_id"]
    )
    post_requests = [event for kind, event in _FakeCodexHTTPClient.events if kind == "POST"]
    assert len(jobs) == 2
    assert [job["state"] for job in jobs] == ["SUCCEEDED", expected_state]
    assert len(post_requests) == 2
    assert "21" not in post_requests[-1]["prompt"]
    assert second_status["status"] == expected_status


@pytest.mark.parametrize(
    ("outcome", "expected_error", "expected_state"),
    [
        ({"state": "failed"}, CodexJobFailedError, "FAILED"),
        ({"state": "needs_approval"}, CodexJobReconciliationRequiredError, "ACCEPTED"),
        ({"state": "in_progress"}, CodexJobTimeoutError, "RUNNING"),
        ({"post_error": True}, CodexJobReconciliationRequiredError, "MAYBE_SENT"),
    ],
    ids=["failed", "needs-approval", "timeout", "ambiguous-post"],
)
def test_worker_never_creates_second_job_when_first_is_not_succeeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: dict[str, Any],
    expected_error: type[Exception],
    expected_state: str,
) -> None:
    from codex_bridge.workflows import worker

    run_dir = tmp_path / "state"
    workspace_root = tmp_path / "workspace"
    run_dir.mkdir()
    workspace_root.mkdir()
    _FakeCodexHTTPClient.outcomes = {"日本の首都を答えて": outcome}
    with pytest.raises(expected_error):
        _run_fake_worker(run_dir, workspace_root, max_polls=1, monkeypatch=monkeypatch)

    jobs = worker.CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_jobs_for_workflow(
        _FakeDBOS.workflow_id
    )
    assert len(jobs) == 1
    assert sum(kind == "POST" for kind, _ in _FakeCodexHTTPClient.events) == 1
    assert jobs[0]["state"] == expected_state
    assert all(
        event[1].get("prompt") != "翻訳して: 日本の首都を答えて"
        for event in _FakeCodexHTTPClient.events
        if event[0] == "POST"
    )


@pytest.mark.parametrize("value", [True, 0, 4, 9, "2", None])
def test_worker_rejects_invalid_max_live_codex_jobs_control(
    tmp_path: Path,
    value: Any,
) -> None:
    from codex_bridge.workflows import worker

    definition_text = yaml.safe_dump(_two_job_workflow(), allow_unicode=True, sort_keys=False)
    with pytest.raises(OwsError, match="max_live_codex_jobs"):
        worker.execute_ows_workflow.__wrapped__(
            definition_text,
            worker.sha256_text(definition_text),
            None,
            worker.sha256_json(None),
            str(tmp_path / "events.jsonl"),
            {"codex_job_live": True, "max_live_codex_jobs": value},
            10,
        )


def test_worker_entry_defaults_manifest_limit_to_one_and_accepts_internal_two() -> None:
    from codex_bridge.workflows.worker_entry import _max_live_codex_jobs

    assert _max_live_codex_jobs({}) == 1
    assert _max_live_codex_jobs({"max_live_codex_jobs": 2}) == 2
    assert _max_live_codex_jobs({"max_live_codex_jobs": 3}) == 3


@pytest.mark.parametrize("value", [True, 0, 4, 10, "2", None])
def test_worker_entry_rejects_invalid_manifest_job_limit(value: Any) -> None:
    from codex_bridge.workflows.worker_entry import (
        WorkflowWorkerEntryError,
        _max_live_codex_jobs,
    )

    with pytest.raises(WorkflowWorkerEntryError, match="max_live_codex_jobs"):
        _max_live_codex_jobs({"max_live_codex_jobs": value})


def test_public_worker_manager_rejects_internal_multijob_manifest_control() -> None:
    from codex_bridge.workflows.manager import WorkflowManagerError, _codex_job_live

    with pytest.raises(WorkflowManagerError, match="max_live_codex_jobs"):
        _codex_job_live(
            {
                "control": {
                    "codex_job_live": True,
                    "max_live_codex_jobs": 2,
                }
            }
        )

    three_job_manifest = {
        "control": {
            "codex_job_live": True,
            "codex_bridge_port": 18001,
            "codex_job_max_polls": 600,
            "codex_job_poll_interval_seconds": 0.2,
            "max_live_codex_jobs": 3,
        },
        "codex_bridge_port": 18001,
        "max_tasks": 10_000,
    }
    assert _codex_job_live(three_job_manifest, max_live_codex_jobs=3)
