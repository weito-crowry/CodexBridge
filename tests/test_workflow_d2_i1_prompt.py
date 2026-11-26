from __future__ import annotations

from typing import Any

import pytest

from codex_bridge.workflows.agent_prompt import (
    AgentPromptError,
    collect_agent_operation_expressions,
    parse_prompt_template,
    render_prompt_template,
    validate_prompt_bindings,
)
from codex_bridge.workflows.contracts import DRAFT_2020_12, compile_contract
from codex_bridge.workflows.interpreter import OwsError, evaluate_jq, evaluate_jq_single_result
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowSnapshotError,
    _parse_workflow_source,
)


def _binding(expression: str = "${ . }", format: str = "json") -> dict[str, str]:
    return {"from": expression, "format": format}


def _strict_contract():
    object_schema = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "additionalProperties": True,
    }
    return compile_contract(
        {
            "contract_version": 1,
            "input_schema": object_schema,
            "output_schema": object_schema,
            "variables": [
                {
                    "declaration_id": "count",
                    "name": "count",
                    "owner_scope": "workflow",
                    "value_schema": {"type": "integer"},
                    "sensitivity": "public",
                }
            ],
        }
    )


def test_literal_prompt_is_preserved_exactly() -> None:
    assert render_prompt_template("literal text\n  ", {}, lambda _value: None) == "literal text\n  "


def test_repeated_placeholder_evaluates_its_binding_once() -> None:
    calls: list[str] = []

    def evaluate(expression: str) -> Any:
        calls.append(expression)
        return "Ada"

    rendered = render_prompt_template(
        "Hello {{name}}; again {{name}}.",
        {"name": _binding("${ .name }", "text")},
        evaluate,
    )

    assert rendered == "Hello Ada; again Ada."
    assert calls == ["${ .name }"]


def test_utf8_text_and_prompt_newlines_and_trailing_space_are_preserved() -> None:
    rendered = render_prompt_template(
        "質問:\n{{answer}}\n\n ",
        {"answer": _binding("${ .answer }", "text")},
        lambda _expression: "日本語の回答\n二行目",
    )

    assert rendered == "質問:\n日本語の回答\n二行目\n\n "


def test_escape_and_single_braces_and_unmatched_closing_braces_are_literal() -> None:
    assert (
        render_prompt_template("{{{{name}} {one} and }}", {}, lambda _value: None)
        == "{{name}} {one} and }}"
    )


@pytest.mark.parametrize(
    ("prompt", "bindings", "code"),
    [
        ("{{unknown}}", {}, "unknown_binding"),
        ("plain", {"unused": _binding()}, "unused_binding"),
        ("{{bad-name}}", {}, "invalid_placeholder"),
        ("unfinished {{name", {}, "unclosed_placeholder"),
    ],
)
def test_template_reference_errors_are_rejected(
    prompt: str,
    bindings: object,
    code: str,
) -> None:
    with pytest.raises(AgentPromptError) as error:
        render_prompt_template(prompt, bindings, lambda _value: None)

    assert error.value.code == code


@pytest.mark.parametrize(
    ("bindings", "code"),
    [
        (None, "invalid_bindings"),
        ({"x": None}, "invalid_binding"),
        ({"x": {"from": "${ . }"}}, "invalid_binding_shape"),
        ({"x": {"format": "text"}}, "invalid_binding_shape"),
        ({"x": {"from": "${ . }", "format": "text", "extra": "secret"}}, "invalid_binding_shape"),
        ({"x": {"from": 5, "format": "text"}}, "invalid_from"),
        ({"x": {"from": ".", "format": "text"}}, "invalid_from"),
        ({"x": {"from": "${ . }", "format": "yaml"}}, "invalid_format"),
        ({"bad-name": _binding()}, "invalid_binding_name"),
    ],
)
def test_binding_definitions_are_closed_and_strict(
    bindings: object,
    code: str,
) -> None:
    with pytest.raises(AgentPromptError) as error:
        validate_prompt_bindings(
            bindings, {"x"} if isinstance(bindings, dict) and "bad-name" not in bindings else set()
        )

    assert error.value.code == code


def test_text_binding_rejects_non_string_results() -> None:
    with pytest.raises(AgentPromptError) as error:
        render_prompt_template(
            "{{value}}",
            {"value": _binding("${ . }", "text")},
            lambda _expression: 42,
        )

    assert error.value.code == "text_binding_type"


def test_json_binding_uses_canonical_json_for_json_values() -> None:
    values = {
        "${ .null }": None,
        "${ .bool }": False,
        "${ .number }": 2.5,
        "${ .string }": "日本語",
        "${ .array }": ["x", None, True],
        "${ .object }": {"z": 1, "a": "語"},
    }
    bindings = {
        name: _binding(expression)
        for name, expression in zip(
            ("null", "bool", "number", "string", "array", "object"),
            values,
            strict=True,
        )
    }
    prompt = "|".join(f"{{{{{name}}}}}" for name in bindings)

    rendered = render_prompt_template(prompt, bindings, values.__getitem__)

    assert rendered == 'null|false|2.5|"日本語"|["x",null,true]|{"a":"語","z":1}'


def test_inserted_placeholder_syntax_is_not_rescanned() -> None:
    rendered = render_prompt_template(
        "before {{value}} after",
        {"value": _binding("${ . }", "text")},
        lambda _expression: "{{other}} {{{{",
    )

    assert rendered == "before {{other}} {{{{ after"


@pytest.mark.parametrize("prompt", ["", " \t\n　"])
def test_empty_or_whitespace_only_rendered_prompt_is_rejected(prompt: str) -> None:
    with pytest.raises(AgentPromptError) as error:
        render_prompt_template(prompt, {}, lambda _value: None)

    assert error.value.code == "empty_prompt"


def test_invalid_unicode_in_template_and_rendered_text_is_rejected() -> None:
    with pytest.raises(AgentPromptError) as prompt_error:
        parse_prompt_template("bad\ud800")
    assert prompt_error.value.code == "invalid_unicode"

    with pytest.raises(AgentPromptError) as rendered_error:
        render_prompt_template(
            "{{value}}",
            {"value": _binding("${ . }", "text")},
            lambda _value: "bad\ud800",
        )
    assert rendered_error.value.code == "invalid_unicode"


def test_prompt_larger_than_the_legacy_16_kibibyte_limit_is_valid() -> None:
    prompt = "x" * 17_000

    assert render_prompt_template(prompt, {}, lambda _value: None) == prompt


def test_renderer_does_not_expose_binding_values_in_errors() -> None:
    secret = "private-token-7dbd"

    def fail(_expression: str) -> Any:
        raise RuntimeError(secret)

    with pytest.raises(AgentPromptError) as error:
        render_prompt_template("{{value}}", {"value": _binding()}, fail)

    assert error.value.code == "binding_evaluation_failed"
    assert secret not in str(error.value)


def test_single_result_jq_rejects_zero_and_multiple_but_accepts_array_result() -> None:
    with pytest.raises(OwsError, match="exactly one"):
        evaluate_jq_single_result("${ empty }", {})
    with pytest.raises(OwsError, match="exactly one"):
        evaluate_jq_single_result("${ 1, 2, 3 }", {})

    assert evaluate_jq_single_result("${ [1, 2, 3] }", {}) == [1, 2, 3]


def test_single_result_jq_distinguishes_one_null_from_zero_results() -> None:
    assert evaluate_jq_single_result("${ null }", {}) is None
    assert evaluate_jq_single_result("${ .missing }", {}) is None
    with pytest.raises(OwsError, match="exactly one"):
        evaluate_jq_single_result("${ empty }", {})


def test_single_result_jq_uses_existing_variables_and_strict_cbvars_guards() -> None:
    assert evaluate_jq_single_result("${ $input.name }", {}, {"input": {"name": "Mika"}}) == "Mika"

    contract = _strict_contract()
    assert (
        evaluate_jq_single_result(
            "${ $cbvars.workflow.count }",
            {},
            {"cbvars": {"workflow": {"count": 9}}},
            strict_contract=contract,
        )
        == 9
    )
    with pytest.raises(OwsError, match="strict workflow jq evaluation failed"):
        evaluate_jq_single_result(
            "${ $cbvars.workflow.count }",
            {},
            {"cbvars": {"workflow": {}}},
            strict_contract=contract,
        )


def test_new_jq_helper_does_not_change_existing_zero_one_many_normalization() -> None:
    assert evaluate_jq("${ empty }", {}) is None
    assert evaluate_jq("${ 7 }", {}) == 7
    assert evaluate_jq("${ 7, 8, 9 }", {}) == [7, 8, 9]


def test_single_result_jq_error_does_not_include_expression_secret() -> None:
    secret = "private-jq-expression-marker"

    with pytest.raises(OwsError) as error:
        evaluate_jq_single_result(f'${{ error("{secret}") }}', {})

    assert secret not in str(error.value)


def test_collector_extracts_only_explicit_operation_expressions() -> None:
    operation = {
        "prompt": "${ .prompt_must_stay_literal }",
        "bindings": {"source": {"from": "${ $input.source }", "format": "json"}},
        "session": {"mode": "continue", "thread_id": "${ .thread }"},
        "handle": {"thread_id": "${ .literal_thread }", "turn_id": "${ .literal_turn }"},
        "options": {
            "title": "${ .title }",
            "model": "fixed-model",
            "nested": {"ignored": "${ .nested_must_stay_literal }"},
        },
        "timeout_seconds": "${ .timeout }",
        "wait": "${ .wait }",
        "response": {"format": "json", "schema": {"const": "${ .schema_must_stay_literal }"}},
    }

    assert set(collect_agent_operation_expressions(operation)) == {
        "${ $input.source }",
        "${ .thread }",
        "${ .title }",
        "${ .timeout }",
        "${ .wait }",
    }


def test_collector_accepts_whole_handle_expression_without_recursing_literal_maps() -> None:
    assert collect_agent_operation_expressions({"handle": "${ .handle }"}) == ("${ .handle }",)
    assert (
        collect_agent_operation_expressions(
            {"handle": {"thread_id": "${ .thread }", "turn_id": "${ .turn }"}}
        )
        == ()
    )


def test_existing_strict_yaml_parser_rejects_duplicate_binding_keys() -> None:
    source = b"""prompt: hello
bindings:
  value:
    from: '${ . }'
    format: text
    format: json
"""

    with pytest.raises(StrictWorkflowSnapshotError):
        _parse_workflow_source(source)
