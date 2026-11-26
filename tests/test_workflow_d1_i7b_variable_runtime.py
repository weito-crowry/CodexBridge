from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.contracts import DRAFT_2020_12, compile_contract
from codex_bridge.workflows.interpreter import Interpreter, OwsError, validate_workflow
from codex_bridge.workflows.strict_binding import StrictWorkflowBindingVerifier
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_manifest import StrictWorkflowSnapshotWriter
from codex_bridge.workflows.strict_reader_gate import _reserve_prepared_strict_run
from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
from codex_bridge.workflows.strict_variable_mutations import MutationContext
from codex_bridge.workflows.strict_variable_runtime import (
    StrictVariableRuntimeError,
    StrictWorkflowVariableRuntimeAdapter,
)
from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableStore,
)

_RUN_ID = "run-7b1234567890abcdef1234567890abcd"
_WORKFLOW_ID = "7b000000-0000-4000-8000-000000000001"
_CREATED_AT = datetime(2026, 11, 26, 3, 4, 5, 123456, tzinfo=UTC)


def _schema(value_type: str, **extra: Any) -> dict[str, Any]:
    return {
        "$schema": DRAFT_2020_12,
        "type": value_type,
        "additionalProperties": False,
        **extra,
    }


def _contract() -> dict[str, Any]:
    object_schema = _schema("object")
    return {
        "contract_version": 1,
        "input_schema": object_schema,
        "output_schema": object_schema,
        "variables": [
            {
                "declaration_id": "count",
                "name": "count",
                "owner_scope": "workflow",
                "value_schema": _schema("integer"),
                "sensitivity": "public",
            },
            {
                "declaration_id": "items",
                "name": "items",
                "owner_scope": "workflow",
                "value_schema": _schema("array", items={"type": "string"}),
                "sensitivity": "public",
            },
            {
                "declaration_id": "document-state",
                "name": "document_state",
                "owner_scope": "workflow",
                "value_schema": _schema(
                    "object",
                    properties={
                        "a/b": {
                            "type": "object",
                            "properties": {"~key": {"type": "integer"}},
                            "required": ["~key"],
                            "additionalProperties": False,
                        },
                        "remove": {"type": "string"},
                    },
                    required=["a/b"],
                ),
                "sensitivity": "public",
            },
            {
                "declaration_id": "nullable",
                "name": "nullable",
                "owner_scope": "workflow",
                "value_schema": {"type": ["string", "null"]},
                "sensitivity": "public",
            },
        ],
    }


def _source(tasks: list[dict[str, Any]] | None = None, *, output: str | None = None) -> str:
    definition: dict[str, Any] = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "i7b-variable-runtime",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": tasks or [{"pass": {"set": "${ . }"}}],
    }
    if output is not None:
        definition["output"] = {"as": output}
    return yaml.safe_dump(definition, sort_keys=False, allow_unicode=True)


def _reserved_adapter(
    tmp_path: Path,
    *,
    tasks: list[dict[str, Any]] | None = None,
    output: str | None = None,
    projection_provider=None,
    reserve: bool = True,
) -> tuple[StrictWorkflowVariableRuntimeAdapter, Path]:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_root.mkdir(parents=True)
    workspace.mkdir(parents=True)
    allowed_roots = (str(workspace.resolve()),)
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    revision = definitions.create(
        mutation_request_id="i7b-definition-create-0001",
        source_yaml=_source(tasks, output=output),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    normalized = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        revision.definition_id,
        revision.revision,
        {},
    )
    run_directory = state_root / _RUN_ID
    run_directory.mkdir()
    binding = StrictWorkflowRunBindingInput(
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        definition_id=revision.definition_id,
        definition_revision=revision.revision,
        contract_sha256=revision.contract_sha256,
        binding_digest=revision.binding_digest,
        normalized_input_sha256=normalized.normalized_input_sha256,
        created_at=_CREATED_AT,
    )
    StrictWorkflowVariableStore(run_directory).initialize_new_database(binding)
    prepared = StrictWorkflowSnapshotWriter(state_root, allowed_roots).prepare_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        definition_id=revision.definition_id,
        definition_revision=revision.revision,
        workspace_root=str(workspace.resolve()),
        normalized_input=normalized,
        created_at=_CREATED_AT,
    )
    if reserve:
        _reserve_prepared_strict_run(
            state_root,
            _RUN_ID,
            client_request_id="i7b-variable-request-0001",
            allowed_roots=allowed_roots,
        )
    assert prepared.run_id == _RUN_ID
    return (
        StrictWorkflowVariableRuntimeAdapter.from_reserved_run(
            state_root,
            _RUN_ID,
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=prepared.request_digest,
            allowed_roots=allowed_roots,
            projection_provider=projection_provider,
        ),
        run_directory,
    )


def _call(operation: str, declaration_id: str, **arguments: Any) -> dict[str, Any]:
    return {
        "operation": operation,
        "declaration_id": declaration_id,
        "scope": "workflow",
        **arguments,
    }


def test_reserved_adapter_executes_all_operations_and_projects_present_null(tmp_path: Path) -> None:
    adapter, _run_directory = _reserved_adapter(tmp_path)

    assert adapter.execute_mutation(
        _call("initialize", "count", value=2), node_id="/do/0/count_init"
    ) == {"present": True, "value": 2}
    assert adapter.execute_mutation(
        _call("assign", "count", value=4), node_id="/do/1/count_assign"
    ) == {"present": True, "value": 4}
    assert adapter.execute_mutation(
        _call("increment", "count", by=2), node_id="/do/2/count_inc"
    ) == {"present": True, "value": 6}
    assert adapter.execute_mutation(
        _call("decrement", "count", by=1), node_id="/do/3/count_dec"
    ) == {"present": True, "value": 5}

    adapter.execute_mutation(_call("initialize", "items", value=["a"]), node_id="/do/4/items_init")
    assert adapter.execute_mutation(
        _call("append", "items", value="b"), node_id="/do/5/items_append"
    ) == {"present": True, "value": ["a", "b"]}
    adapter.execute_mutation(
        _call("initialize", "document-state", value={"a/b": {"~key": 1}, "remove": "x"}),
        node_id="/do/6/doc_init",
    )
    assert adapter.execute_mutation(
        _call("object_set", "document-state", path="/a~1b/~0key", value=2),
        node_id="/do/7/doc_set",
    )["value"] == {"a/b": {"~key": 2}, "remove": "x"}
    assert adapter.execute_mutation(
        _call("object_delete", "document-state", path="/remove"),
        node_id="/do/8/doc_delete",
    )["value"] == {"a/b": {"~key": 2}}
    assert adapter.execute_mutation(
        _call("initialize", "nullable", value=None), node_id="/do/9/null_init"
    ) == {"present": True, "value": None}
    assert adapter.projection_for_phase("test", "/projection")["workflow"]["nullable"] is None

    assert adapter.execute_mutation(
        _call("variable_delete", "count"), node_id="/do/10/count_delete"
    ) == {"present": False}
    database_path = adapter._run_directory / "variables.sqlite"
    database_before_projection = database_path.read_bytes()
    projection = adapter.projection_for_phase("after-delete", "/projection")
    assert database_path.read_bytes() == database_before_projection
    assert "count" not in projection["workflow"]
    assert projection["workflow"]["nullable"] is None
    assert "document-state" in projection["workflow"]
    assert "document_state" not in projection["workflow"]
    ledger = adapter._store.inspect_ledger()
    assert ledger.mutation_count == 11


def test_prepared_replay_returns_historical_result_without_rewinding_latest_state(
    tmp_path: Path,
) -> None:
    adapter, _run_directory = _reserved_adapter(tmp_path)
    prepared = adapter.prepare_mutation(
        _call("initialize", "count", value=1), node_id="/do/0/count_init"
    )
    first = adapter.apply_prepared(prepared)
    adapter.execute_mutation(_call("assign", "count", value=2), node_id="/do/1/count_assign")

    replay = adapter.apply_prepared(prepared)
    assert first == replay == {"present": True, "value": 1}
    assert adapter.projection_for_phase("latest", "/projection")["workflow"]["count"] == 2

    divergent = adapter.prepare_mutation(
        _call("initialize", "count", value=9), node_id="/do/0/count_init"
    )
    with pytest.raises(StrictVariableRuntimeError) as error:
        adapter.apply_prepared(divergent)
    assert error.value.reason_code == "strict_variable_needs_reconciliation"
    assert "9" not in str(error.value)
    assert adapter._store.inspect_ledger().mutation_count == 2


def test_invalid_runtime_calls_are_value_safe_and_leave_the_ledger_unchanged(
    tmp_path: Path,
) -> None:
    adapter, _run_directory = _reserved_adapter(tmp_path)
    before = adapter._store.inspect_ledger().mutation_count
    invalid_calls = [
        _call("unknown", "count", value="PRIVATE-VALUE"),
        _call("initialize", "missing-declaration", value=1),
        _call("initialize", "count"),
        _call("initialize", "count", value="PRIVATE-VALUE"),
        _call("object_set", "document-state", path="", value=1),
        _call("increment", "count", by=True),
        {**_call("increment", "count", by=1), "run_id": _RUN_ID},
    ]
    for index, arguments in enumerate(invalid_calls):
        with pytest.raises(StrictVariableRuntimeError) as error:
            adapter.execute_mutation(arguments, node_id=f"/invalid/{index}")
        assert "PRIVATE-VALUE" not in str(error.value)
        assert "PRIVATE-VALUE" not in repr(error.value)
    assert adapter._store.inspect_ledger().mutation_count == before


def test_runtime_adapter_requires_reserved_snapshot_and_allowed_workspace(
    tmp_path: Path,
) -> None:
    with pytest.raises(StrictVariableRuntimeError) as unreserved_error:
        _reserved_adapter(tmp_path / "unreserved", reserve=False)
    assert unreserved_error.value.reason_code == "strict_variable_run_invalid"

    adapter, run_directory = _reserved_adapter(tmp_path / "wrong-root")
    manifest = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(StrictVariableRuntimeError) as roots_error:
        StrictWorkflowVariableRuntimeAdapter.from_reserved_run(
            run_directory.parent,
            run_directory.name,
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=manifest["request_digest"],
            allowed_roots=(str(outside.resolve()),),
        )
    assert roots_error.value.reason_code == "strict_variable_run_invalid"


def test_interpreter_uses_verified_definition_cbvars_and_nested_iteration_identity(
    tmp_path: Path,
) -> None:
    tasks = [
        {
            "initialize_count": {
                "call": "codexbridge-variable",
                "with": _call("initialize", "count", value=1),
            }
        },
        {
            "loop": {
                "for": {"in": "${ [0, 1] }", "each": "item", "at": "index"},
                "do": [
                    {
                        "inner": {
                            "for": {
                                "in": "${ [0, 1] }",
                                "each": "inner_item",
                                "at": "inner_index",
                            },
                            "do": [
                                {
                                    "increment_count": {
                                        "call": "codexbridge-variable",
                                        "with": _call("increment", "count", by=1),
                                    }
                                }
                            ],
                        }
                    }
                ],
            }
        },
    ]
    adapter, _run_directory = _reserved_adapter(
        tmp_path,
        tasks=tasks,
        output="${ $cbvars.workflow.count }",
    )
    result = Interpreter(adapter.pinned_definition, strict_variable_runtime=adapter).run({})

    assert result == 5
    mutations = adapter._store.inspect_ledger().mutations
    assert len(mutations) == 5
    loop_contexts = [
        MutationContext(
            node_path="/do/1/loop/do/0/inner/do/0/increment_count",
            iteration_vector=(outer_index, inner_index),
            frame_id="workflow-root",
            mutation_ordinal=0,
        )
        for outer_index in (0, 1)
        for inner_index in (0, 1)
    ]
    loop_keys = {adapter._store.execution_key_for(context) for context in loop_contexts}
    assert len(loop_keys) == 4
    assert all(adapter._store.get_mutation(key) is not None for key in loop_keys)


def test_cbvars_missing_is_an_error_but_present_null_and_quoted_text_are_preserved(
    tmp_path: Path,
) -> None:
    missing_tasks = [{"read_count": {"set": "${ $cbvars.workflow.count }"}}]
    adapter, _run_directory = _reserved_adapter(tmp_path, tasks=missing_tasks)
    with pytest.raises(OwsError, match="strict workflow jq evaluation failed") as error:
        Interpreter(adapter.pinned_definition, strict_variable_runtime=adapter).run({})
    assert "$cbvars.workflow.count" not in str(error.value)

    null_tasks = [
        {
            "initialize_null": {
                "call": "codexbridge-variable",
                "with": _call("initialize", "nullable", value=None),
            }
        },
        {"read_null": {"set": "${ $cbvars.workflow.nullable }"}},
    ]
    null_adapter, _run_directory = _reserved_adapter(tmp_path / "null", tasks=null_tasks)
    result = Interpreter(
        null_adapter.pinned_definition,
        strict_variable_runtime=null_adapter,
    ).run({})
    assert result is None

    quoted_tasks = [{"literal": {"set": '${ "$cbvars.workflow.count" }'}}]
    quoted_adapter, _run_directory = _reserved_adapter(tmp_path / "quoted", tasks=quoted_tasks)
    result = Interpreter(
        quoted_adapter.pinned_definition,
        strict_variable_runtime=quoted_adapter,
    ).run({})
    assert result == "$cbvars.workflow.count"

    interpolation_tasks = [
        {
            "initialize_count": {
                "call": "codexbridge-variable",
                "with": _call("initialize", "count", value=6),
            }
        },
        {"interpolated": {"set": '${ "count=\\($cbvars.workflow.count)" }'}},
    ]
    interpolation_adapter, _run_directory = _reserved_adapter(
        tmp_path / "interpolation", tasks=interpolation_tasks
    )
    result = Interpreter(
        interpolation_adapter.pinned_definition,
        strict_variable_runtime=interpolation_adapter,
    ).run({})
    assert result == "count=6"


def test_strict_runtime_keeps_nested_do_and_switch_flow(tmp_path: Path) -> None:
    tasks = [
        {
            "nested": {
                "do": [
                    {
                        "initialize_count": {
                            "call": "codexbridge-variable",
                            "with": _call("initialize", "count", value=7),
                        }
                    },
                    {
                        "choose": {
                            "switch": [
                                {"increment": {"when": "${ true }", "then": "update"}},
                                {"default": {"then": "exit"}},
                            ]
                        }
                    },
                    {
                        "update": {
                            "call": "codexbridge-variable",
                            "with": _call("increment", "count", by=1),
                        }
                    },
                ]
            }
        }
    ]
    adapter, _run_directory = _reserved_adapter(
        tmp_path,
        tasks=tasks,
        output="${ $cbvars.workflow.count }",
    )

    assert Interpreter(adapter.pinned_definition, strict_variable_runtime=adapter).run({}) == 8


def test_cbvars_identifier_lookup_uses_declaration_id_and_allows_nested_json_access(
    tmp_path: Path,
) -> None:
    tasks = [
        {
            "initialize_document": {
                "call": "codexbridge-variable",
                "with": _call(
                    "initialize",
                    "document-state",
                    value={"a/b": {"~key": 12}, "remove": "x"},
                ),
            }
        },
        {"read_document": {"set": '${ $cbvars.workflow["document-state"]["a/b"]["~key"] }'}},
    ]
    adapter, _run_directory = _reserved_adapter(tmp_path, tasks=tasks)

    assert Interpreter(adapter.pinned_definition, strict_variable_runtime=adapter).run({}) == 12


@pytest.mark.parametrize(
    "expression",
    [
        "${ $cbvars }",
        "${ $cbvars.workflow }",
        "${ $cbvars.workflow[$input.name] }",
        "${ $cbvars.other.count }",
        "${ $cbvars.workflow.unknown }",
    ],
)
def test_strict_static_validator_rejects_unsupported_cbvars_references(
    expression: str,
) -> None:
    contract = compile_contract(_contract())
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "unsupported-cbvars",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [{"read": {"set": expression}}],
    }

    with pytest.raises(OwsError):
        validate_workflow(definition, strict_contract=contract)


def test_cbvars_text_in_jq_comment_is_not_treated_as_a_variable_reference() -> None:
    contract = compile_contract(_contract())
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "cbvars-comment",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [{"read": {"set": "${ 7 # $cbvars.workflow.unknown\n }"}}],
    }

    validate_workflow(definition, strict_contract=contract)


def test_projection_provider_captures_one_detached_projection_per_evaluation_phase(
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def provider(phase_id: str) -> dict[str, Any]:
        captured.append(phase_id)
        return {"count": len(captured)}

    tasks = [
        {
            "first": {
                "set": '${ {"seen": $cbvars.workflow.count} }',
                "output": {"as": "${ .seen + $cbvars.workflow.count }"},
            }
        },
        {
            "second": {
                "input": {"from": "${ . + $cbvars.workflow.count }"},
                "set": "${ . }",
                "export": {"as": '${ {"value": ., "current": $cbvars.workflow.count} }'},
            }
        },
    ]
    adapter, _run_directory = _reserved_adapter(
        tmp_path,
        tasks=tasks,
        output="${ $cbvars.workflow.count }",
        projection_provider=provider,
    )
    result = Interpreter(adapter.pinned_definition, strict_variable_runtime=adapter).run({})

    assert result == 5
    assert len(captured) == 5
    assert len(set(captured)) == 5
