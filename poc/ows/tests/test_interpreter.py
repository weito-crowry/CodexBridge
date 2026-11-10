from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from interpreter import (
    Interpreter,
    OwsError,
    evaluate_jq,
    load_workflow,
    parse_workflow_text,
    validate_official_schema,
    validate_workflow,
)

OWS_DIR = Path(__file__).resolve().parents[1]
FIXTURE = OWS_DIR / "fixtures" / "workflow.yaml"


def minimal(*tasks: dict[str, Any]) -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "minimal",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": list(tasks),
    }


def run_fixture(sign: int, values: list[int] | None = None) -> Any:
    definition = load_workflow(FIXTURE)
    return Interpreter(definition).run(
        {"sign": sign, "offset": 2, "values": [3, 5] if values is None else values}
    )


def test_fixture_parses_and_passes_official_schema() -> None:
    definition = load_workflow(FIXTURE)
    validate_workflow(definition)
    assert definition["document"]["dsl"] == "1.0.3"


def test_mock_job_fixture_is_official_schema_valid_but_standalone_call_has_no_adapter() -> None:
    fixture = OWS_DIR / "fixtures" / "mock_job_workflow.yaml"
    definition = parse_workflow_text(fixture.read_text(encoding="utf-8"))
    validate_official_schema(definition)

    with pytest.raises(OwsError, match="call requires the mock-job DBOS adapter"):
        Interpreter(definition).run({"values": [3, 5]})


def test_nested_do_set_dataflow_switch_and_for_output() -> None:
    result = run_fixture(1)
    assert result == {
        "branch": "positive",
        "indexes": [0, 1],
        "items": [3, 5],
        "total": 7,
    }


@pytest.mark.parametrize(
    ("sign", "branch"),
    [(1, "positive"), (-1, "negative"), (0, "neutral")],
)
def test_switch_true_false_and_default(sign: int, branch: str) -> None:
    result = run_fixture(sign)
    assert result["branch"] == branch


def test_for_binds_each_item_and_index_and_empty_loop_returns_empty_array() -> None:
    result = run_fixture(0, [])
    assert result == {"branch": None, "indexes": [], "items": [], "total": None}


def test_exit_ends_nested_do_scope_and_parent_scope_continues() -> None:
    definition = minimal(
        {
            "nested": {
                "do": [
                    {"finish": {"set": {"nested": "done"}, "then": "exit"}},
                    {"skipped": {"set": {"wrong": True}}},
                ]
            }
        },
        {"parentAfter": {"set": {"parent": True, "nested": "${ .nested }"}}},
    )
    assert Interpreter(definition).run({}) == {"parent": True, "nested": "done"}


def test_exit_in_for_iteration_ends_only_that_iteration_do_scope() -> None:
    definition = minimal(
        {
            "loop": {
                "for": {"in": "${ .items }", "each": "item", "at": "index"},
                "do": [
                    {
                        "record": {
                            "set": {"item": "${ $item }", "index": "${ $index }"},
                            "then": "exit",
                        }
                    },
                    {"skipped": {"set": {"wrong": True}}},
                ],
            }
        },
        {"summarize": {"set": {"items": "${ map(.item) }", "indexes": "${ map(.index) }"}}},
    )
    assert Interpreter(definition).run({"items": [4, 8]}) == {
        "items": [4, 8],
        "indexes": [0, 1],
    }


def test_switch_default_is_identified_by_missing_when_not_case_name() -> None:
    definition = minimal(
        {
            "choose": {
                "switch": [
                    {"positive": {"when": "${ .value > 0 }", "then": "yes"}},
                    {"fallback": {"then": "no"}},
                ]
            }
        },
        {"yes": {"set": {"branch": "yes"}}},
        {"no": {"set": {"branch": "no"}}},
    )
    assert Interpreter(definition).run({"value": 0}) == {"branch": "no"}


def test_jq_compile_args_bind_loop_variables_as_json_values() -> None:
    assert evaluate_jq("${ $item + $index }", 0, {"item": 7, "index": 3}) == 10


@pytest.mark.parametrize(
    ("expression", "data", "expected"),
    [
        ("${ empty }", {}, None),
        ("${ .value }", {"value": 9}, 9),
        ("${ .values[] }", {"values": [1, 2]}, [1, 2]),
    ],
)
def test_jq_zero_one_and_multiple_outputs(expression: str, data: Any, expected: Any) -> None:
    assert evaluate_jq(expression, data) == expected


def test_plain_string_is_not_evaluated_as_jq() -> None:
    definition = minimal({"keepText": {"set": {"text": ".value"}}})
    assert Interpreter(definition).run({"value": "ignored"}) == {"text": ".value"}


@pytest.mark.parametrize("dsl_version", ["1.0.2", "1.0.4", 1.03])
def test_rejects_non_103_dsl_version(dsl_version: Any) -> None:
    definition = minimal({"task": {"set": {"ok": True}}})
    definition["document"]["dsl"] = dsl_version
    with pytest.raises(OwsError, match="DSL version must be the string '1.0.3'"):
        Interpreter(definition)


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (
            {
                "invoke": {
                    "call": "http",
                    "with": {"method": "get", "endpoint": "https://example.invalid"},
                }
            },
            "unsupported call target",
        ),
        (
            {
                "recover": {
                    "try": [{"attempt": {"set": {"ok": True}}}],
                    "catch": {"do": [{"fallback": {"set": {"ok": False}}}]},
                }
            },
            "unsupported task",
        ),
    ],
)
def test_schema_valid_call_and_try_are_rejected_by_subset(
    task: dict[str, Any], message: str
) -> None:
    definition = minimal(task)
    validate_official_schema(definition)
    with pytest.raises(OwsError, match=message):
        Interpreter(definition)


def test_workflow_input_from_and_output_as_transform_workflow_data() -> None:
    definition = minimal(
        {
            "capture": {
                "set": {
                    "value": "${ .value }",
                    "bound_input": "${ $input }",
                    "initial_context": "${ $context }",
                }
            }
        }
    )
    definition["input"] = {"from": "${ .request }"}
    definition["output"] = {"as": "${ {result: .value, context: $context} }"}

    result = Interpreter(definition).run(
        {"request": {"value": 7, "discarded": True}, "discarded": "raw"}
    )

    assert result == {
        "result": 7,
        "context": {"value": 7, "discarded": True},
    }


def test_task_input_from_uses_raw_input_and_action_uses_transformed_input() -> None:
    definition = minimal(
        {
            "select": {
                "input": {
                    "from": "${ {selected: .nested.value, context_value: $context.nested.value} }"
                },
                "set": {
                    "dot": "${ .selected }",
                    "bound_input": "${ $input.selected }",
                    "context_value": "${ $context.nested.value }",
                    "raw_only": "${ .nested // null }",
                },
            }
        }
    )

    assert Interpreter(definition).run({"nested": {"value": 11}}) == {
        "dot": 11,
        "bound_input": 11,
        "context_value": 11,
        "raw_only": None,
    }


def test_task_output_as_uses_raw_output_dot_and_passes_transformed_output() -> None:
    definition = minimal(
        {
            "produce": {
                "set": {
                    "raw_value": "${ .value + 1 }",
                    "source_input": "${ $input.value }",
                },
                "output": {"as": "${ {value: .raw_value, source: $input.value} }"},
            }
        },
        {"consume": {"set": {"received": "${ .value }", "source": "${ .source }"}}},
    )

    assert Interpreter(definition).run({"value": 4}) == {"received": 5, "source": 4}


def test_export_updates_context_from_transformed_output_and_omission_preserves_it() -> None:
    definition = minimal(
        {
            "publish": {
                "set": {"raw": "${ .value + 1 }"},
                "output": {"as": "${ {published: .raw} }"},
                "export": {
                    "as": "${ $context + {published: $output.published, seen: .published} }"
                },
            }
        },
        {"preserve": {"set": {"snapshot": "${ $context }", "value": "${ .published }"}}},
        {"finish": {"set": {"snapshot": "${ $context }", "value": "${ .value }"}}},
    )

    assert Interpreter(definition).run({"value": 8}) == {
        "snapshot": {"value": 8, "published": 9, "seen": 9},
        "value": 9,
    }


def test_nested_do_and_for_share_context_with_later_tasks() -> None:
    definition = minimal(
        {
            "nested": {
                "do": [
                    {
                        "publish": {
                            "set": {"value": "${ $context.marker }"},
                            "export": {"as": "${ $context + {nested: .value} }"},
                        }
                    },
                    {"read": {"set": {"nested": "${ $context.nested }"}}},
                ]
            }
        },
        {
            "loop": {
                "for": {"in": "${ $context.items }", "each": "item", "at": "index"},
                "do": [
                    {
                        "publish": {
                            "set": {"value": "${ $item }", "index": "${ $index }"},
                            "export": {
                                "as": (
                                    "${ $context + {loopCount: (($context.loopCount // 0) + 1), "
                                    "last: $item} }"
                                )
                            },
                        }
                    },
                    {"read": {"set": {"seenCount": "${ $context.loopCount }"}}},
                ],
            }
        },
        {
            "finish": {
                "set": {
                    "context": "${ $context }",
                    "nested": "${ $context.nested }",
                    "loopSeen": "${ map(.seenCount) }",
                }
            }
        },
    )
    definition["input"] = {"from": '${ {marker: "start", items: .items} }'}

    assert Interpreter(definition).run({"items": [3, 5]}) == {
        "context": {
            "marker": "start",
            "items": [3, 5],
            "nested": "start",
            "loopCount": 2,
            "last": 5,
        },
        "nested": "start",
        "loopSeen": [1, 2],
    }


@pytest.mark.parametrize(
    "definition",
    [
        {
            **minimal({"task": {"set": {"ok": True}}}),
            "input": {"schema": {"format": "json", "document": {"type": "object"}}},
        },
        {
            **minimal({"task": {"set": {"ok": True}}}),
            "output": {"schema": {"format": "json", "document": {"type": "object"}}},
        },
        minimal(
            {
                "task": {
                    "set": {"ok": True},
                    "input": {
                        "schema": {"format": "json", "document": {"type": "object"}},
                        "from": "${ . }",
                    },
                }
            }
        ),
        minimal({"task": {"set": {"ok": True}, "output": {"as": {"value": "${ . }"}}}}),
        minimal({"task": {"set": {"ok": True}, "input": {"from": {"value": "${ . }"}}}}),
        minimal({"task": {"set": {"ok": True}, "export": {"as": {"value": "${ . }"}}}}),
        minimal(
            {
                "task": {
                    "set": {"ok": True},
                    "export": {
                        "as": "${ . }",
                        "schema": {"format": "json", "document": {"type": "object"}},
                    },
                }
            }
        ),
        {**minimal({"task": {"set": {"ok": True}}}), "input": {"from": {"value": "${ . }"}}},
    ],
)
def test_rejects_schema_and_object_dataflow_forms(
    definition: dict[str, Any],
) -> None:
    validate_official_schema(definition)
    with pytest.raises(OwsError, match="unsupported"):
        Interpreter(definition)


def test_rejects_schema_valid_unsupported_if_field() -> None:
    definition = minimal({"conditional": {"set": {"ok": True}, "if": "${ true }"}})
    validate_official_schema(definition)
    with pytest.raises(OwsError, match="unsupported"):
        Interpreter(definition)


def test_rejects_for_while_in_subset_before_official_schema_validation() -> None:
    definition = minimal(
        {
            "loop": {
                "for": {"in": "${ .items }", "while": "${ true }"},
                "do": [{"copy": {"set": "${ . }"}}],
            }
        }
    )
    with pytest.raises(OwsError, match="unsupported for.while"):
        Interpreter(definition)


@pytest.mark.parametrize("variable", ["workflow", "task", "runtime", "secrets", "authorization"])
def test_rejects_unprovided_runtime_variables_at_validation_time(variable: str) -> None:
    definition = minimal({"task": {"set": {"value": f"${{ ${variable} }}"}}})
    with pytest.raises(OwsError, match="jq compile failed"):
        Interpreter(definition)


@pytest.mark.parametrize(
    "definition",
    [
        minimal({"task": {"input": {"from": "${ $input }"}, "set": {"ok": True}}}),
        minimal({"task": {"output": {"as": "${ $output }"}, "set": {"ok": True}}}),
        {
            **minimal({"task": {"set": {"ok": True}}}),
            "input": {"from": "${ $context }"},
        },
        {
            **minimal({"task": {"set": {"ok": True}}}),
            "output": {"as": "${ $input }"},
        },
    ],
)
def test_rejects_runtime_variables_unavailable_at_expression_stage(
    definition: dict[str, Any],
) -> None:
    with pytest.raises(OwsError, match="jq compile failed"):
        Interpreter(definition)


@pytest.mark.parametrize(
    ("field", "value"),
    [("input", {"from": "not-an-expression"}), ("output", {"as": "${ }"})],
)
def test_dataflow_expressions_must_be_nonempty_strict_jq(field: str, value: dict[str, Any]) -> None:
    definition = minimal({"task": {"set": {"ok": True}}})
    definition[field] = value
    with pytest.raises(OwsError, match="strict jq expression|empty jq expression"):
        Interpreter(definition)


def test_malformed_dataflow_jq_is_rejected_at_validation_time() -> None:
    definition = minimal({"task": {"set": {"ok": True}}})
    definition["input"] = {"from": "${ .value | ) }"}
    with pytest.raises(OwsError, match="jq compile failed at input.from"):
        Interpreter(definition)


def test_dataflow_jq_runtime_error_is_reported() -> None:
    definition = minimal({"task": {"set": {"ok": True}}})
    definition["input"] = {"from": '${ error("bad workflow input") }'}
    with pytest.raises(OwsError, match="jq evaluation failed"):
        Interpreter(definition).run({})


def test_rejects_unknown_workflow_and_task_keys() -> None:
    definition = minimal({"task": {"set": {"ok": True}}})
    definition["schedule"] = {"cron": "* * * * *"}
    with pytest.raises(OwsError, match="unsupported top-level key 'schedule'"):
        Interpreter(definition)

    definition = minimal({"task": {"mystery": {}}})
    with pytest.raises(OwsError, match="unsupported task"):
        Interpreter(definition)


def test_rejects_malformed_jq_at_validation_time() -> None:
    definition = minimal({"task": {"set": {"value": "${ .foo | ) }"}}})
    with pytest.raises(OwsError, match="jq compile failed"):
        Interpreter(definition)


def test_switch_condition_must_be_a_boolean() -> None:
    definition = minimal(
        {
            "select": {
                "switch": [
                    {"case": {"when": "${ .value }", "then": "done"}},
                    {"default": {"then": "done"}},
                ]
            }
        },
        {"done": {"set": {"ok": True}}},
    )
    with pytest.raises(OwsError, match="condition must produce a boolean"):
        Interpreter(definition).run({"value": 1})


def test_for_in_must_produce_an_array() -> None:
    definition = minimal(
        {
            "loop": {
                "for": {"in": "${ .items }"},
                "do": [{"copy": {"set": "${ . }"}}],
            }
        }
    )
    with pytest.raises(OwsError, match="for.in must produce an array"):
        Interpreter(definition).run({"items": {"bad": True}})


def test_switch_target_must_be_in_same_do_scope() -> None:
    definition = minimal(
        {"outer": {"do": [{"select": {"switch": [{"default": {"then": "outside"}}]}}]}},
        {"outside": {"set": {"ok": True}}},
    )
    with pytest.raises(OwsError, match="same do scope"):
        Interpreter(definition)


def test_static_validator_rejects_jump_cycle() -> None:
    definition = minimal(
        {"choose": {"switch": [{"default": {"then": "again"}}]}},
        {"again": {"set": {"ok": True}, "then": "choose"}},
    )
    with pytest.raises(OwsError, match="jump cycle"):
        Interpreter(definition)


def test_rejects_end_flow_directive() -> None:
    definition = minimal({"stop": {"set": {"ok": True}, "then": "end"}})
    with pytest.raises(OwsError, match="unsupported flow directive 'end'"):
        Interpreter(definition)


def test_maximum_task_execution_count_is_enforced() -> None:
    definition = minimal(
        {"first": {"set": {"value": 1}}},
        {"second": {"set": {"value": 2}}},
    )
    with pytest.raises(OwsError, match="maximum task execution count"):
        Interpreter(definition, max_tasks=1).run({})


def test_official_schema_violation_is_reported() -> None:
    definition = copy.deepcopy(minimal({"task": {"set": {"ok": True}}}))
    del definition["document"]["version"]
    with pytest.raises(OwsError, match="official OWS v1.0.3 schema"):
        Interpreter(definition)
