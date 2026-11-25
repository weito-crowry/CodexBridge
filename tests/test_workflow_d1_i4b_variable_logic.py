from __future__ import annotations

import copy
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codex_bridge.workflows.contracts import (
    DRAFT_2020_12,
    MAX_OUTPUT_BYTES,
    CompiledContract,
    ContractInvalid,
    compile_contract,
)
from codex_bridge.workflows.strict_variable_logic import (
    MISSING,
    StrictVariableLogicConflict,
    StrictVariableLogicInvalid,
    StrictVariableLogicResourceLimit,
    calculate_variable_candidate,
    resolve_variable_declaration,
    resolve_variable_declarations,
)
from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableStore,
)

_CREATED_AT = datetime(2026, 11, 19, 3, 4, 5, 123456, tzinfo=UTC)


def _schema(value_type: object = "object", **extra: object) -> dict[str, object]:
    return {
        "$schema": DRAFT_2020_12,
        "type": value_type,
        "additionalProperties": False,
        **extra,
    }


def _variable(
    declaration_id: str,
    name: str,
    *,
    owner_scope: str = "workflow",
    value_schema: dict[str, object] | None = None,
    initial: object = MISSING,
    sensitivity: str = "public",
) -> dict[str, object]:
    value: dict[str, object] = {
        "declaration_id": declaration_id,
        "name": name,
        "owner_scope": owner_scope,
        "value_schema": value_schema or _schema(),
        "sensitivity": sensitivity,
    }
    if initial is not MISSING:
        value["initial"] = initial
    return value


def _contract(variables: list[dict[str, object]]) -> CompiledContract:
    return compile_contract(
        {
            "contract_version": 1,
            "input_schema": _schema(),
            "output_schema": _schema(),
            "variables": variables,
        }
    )


def _calculate(
    contract: CompiledContract,
    declaration_id: str,
    operation: str,
    *,
    scope: str = "workflow",
    exists: bool = False,
    present: bool = False,
    previous: object = MISSING,
    **arguments: object,
):
    return calculate_variable_candidate(
        contract,
        declaration_id,
        scope,
        operation,
        previous_exists=exists,
        previous_present=present,
        previous_value=previous,
        **arguments,
    )


def test_compiled_declarations_are_scoped_immutable_and_value_safe() -> None:
    raw_variables = [
        _variable(
            "d1",
            "same",
            value_schema=_schema("object", properties={"secret-ish": {"type": "string"}}),
            initial={"secret-ish": "caller-value"},
        ),
        _variable("d2", "same", owner_scope="/do/1", value_schema=_schema("integer")),
    ]
    raw = {
        "contract_version": 1,
        "input_schema": _schema(),
        "output_schema": _schema(),
        "variables": raw_variables,
    }
    compiled = compile_contract(raw)
    raw_variables[0]["initial"] = {"secret-ish": "mutated"}

    declarations = resolve_variable_declarations(compiled)
    assert len(declarations) == 2
    assert declarations[0].initial == {"secret-ish": "caller-value"}
    copy_of_initial = declarations[0].initial
    copy_of_initial["secret-ish"] = "changed"
    assert declarations[0].initial == {"secret-ish": "caller-value"}
    copy_of_schema = declarations[0].value_schema
    copy_of_schema["properties"]["secret-ish"]["type"] = "integer"
    assert declarations[0].value_schema["properties"]["secret-ish"]["type"] == "string"
    assert "caller-value" not in repr(declarations[0])
    assert "caller-value" not in repr(compiled)

    resolved = resolve_variable_declaration(compiled, "d1", "workflow")
    assert resolved.declaration_id == "d1"
    with pytest.raises(StrictVariableLogicInvalid):
        resolve_variable_declaration(compiled, "d1", "/do/1")
    with pytest.raises(StrictVariableLogicInvalid):
        resolve_variable_declaration(compiled, "unknown", "workflow")


def test_i1_duplicate_and_secret_declarations_are_rejected() -> None:
    with pytest.raises(ContractInvalid):
        _contract([_variable("same", "a"), _variable("same", "b")])
    with pytest.raises(ContractInvalid):
        _contract([_variable("a", "same"), _variable("b", "same")])
    with pytest.raises(ContractInvalid):
        _contract([_variable("s", "secret", sensitivity="secret")])


def test_malformed_compiled_snapshot_is_revalidated_without_leaking_values() -> None:
    normal = _contract(
        [_variable("d", "state", value_schema=_schema("string"), initial="PRIVATE-SNAPSHOT")]
    )
    malformed = CompiledContract(
        contract_sha256=normal.contract_sha256,
        contract_json_canonical=normal.contract_json_canonical.replace(
            "PRIVATE-SNAPSHOT", "PRIVATE-SNAPSHOT"
        ),
        _input_schema_json=normal._input_schema_json,
        _output_schema_json=normal._output_schema_json,
        _variables_json=normal._variables_json,
    )
    assert resolve_variable_declarations(malformed)[0].initial == "PRIVATE-SNAPSHOT"

    corrupt = CompiledContract(
        contract_sha256="0" * 64,
        contract_json_canonical=normal.contract_json_canonical,
        _input_schema_json=normal._input_schema_json,
        _output_schema_json=normal._output_schema_json,
        _variables_json=normal._variables_json,
    )
    with pytest.raises(StrictVariableLogicInvalid) as error:
        resolve_variable_declarations(corrupt)
    assert "PRIVATE-SNAPSHOT" not in str(error.value)
    assert "PRIVATE-SNAPSHOT" not in repr(error.value)


def test_initialize_distinguishes_no_initial_from_explicit_json_null() -> None:
    contract = _contract(
        [
            _variable("missing", "missing"),
            _variable("null", "null", value_schema=_schema("null"), initial=None),
        ]
    )
    missing = _calculate(contract, "missing", "initialize")
    null_value = _calculate(contract, "null", "initialize")
    assert (missing.state_exists, missing.present, missing.value, missing.canonical) == (
        False,
        False,
        None,
        None,
    )
    assert (
        null_value.state_exists,
        null_value.present,
        null_value.value,
        null_value.canonical,
    ) == (
        True,
        True,
        None,
        "null",
    )
    with pytest.raises(StrictVariableLogicConflict):
        _calculate(contract, "null", "initialize", exists=True, present=False)


def test_initialize_explicit_value_overrides_initial_but_no_argument_is_unchanged() -> None:
    contract = _contract(
        [
            _variable("with-initial", "with_initial", value_schema=_schema("integer"), initial=3),
            _variable("without-initial", "without_initial", value_schema=_schema("integer")),
            _variable("nullable", "nullable", value_schema=_schema("null"), initial=None),
        ]
    )

    explicit = _calculate(contract, "with-initial", "initialize", value=8)
    legacy = _calculate(contract, "without-initial", "initialize")
    explicit_null = _calculate(contract, "nullable", "initialize", value=None)

    assert explicit.value == 8
    assert explicit.present is True
    assert legacy.state_exists is False and legacy.present is False
    assert explicit_null.state_exists is True and explicit_null.present is True
    assert explicit_null.value is None
    with pytest.raises(StrictVariableLogicConflict):
        _calculate(contract, "with-initial", "initialize", exists=True, present=False, value=9)


def test_all_eight_operations_calculate_candidates_only() -> None:
    contract = _contract(
        [
            _variable("n", "count", value_schema=_schema("integer"), initial=2),
            _variable(
                "items",
                "items",
                value_schema={"type": "array", "items": {"type": "string"}},
                initial=["same"],
            ),
            _variable(
                "object",
                "object",
                value_schema=_schema(
                    "object",
                    properties={
                        "a/b": {
                            "type": "object",
                            "properties": {"~key": {"type": "integer"}},
                            "additionalProperties": False,
                        }
                    },
                    required=["a/b"],
                ),
                initial={"a/b": {"~key": 1}},
            ),
        ]
    )

    initialized = _calculate(contract, "n", "initialize")
    assigned = _calculate(contract, "n", "assign", value=7)
    incremented = _calculate(
        contract, "n", "increment", exists=True, present=True, previous=7, by=2
    )
    decremented = _calculate(contract, "n", "decrement", exists=True, present=True, previous=9)
    appended = _calculate(
        contract,
        "items",
        "append",
        exists=True,
        present=True,
        previous=["same"],
        value="same",
    )
    object_set = _calculate(
        contract,
        "object",
        "object_set",
        exists=True,
        present=True,
        previous={"a/b": {"~key": 1}},
        pointer="/a~1b/~0key",
        value=2,
    )
    object_delete = _calculate(
        contract,
        "object",
        "object_delete",
        exists=True,
        present=True,
        previous={"a/b": {"~key": 1}},
        pointer="/a~1b/~0key",
    )
    deleted = _calculate(contract, "n", "variable_delete", exists=True, present=True, previous=7)

    assert initialized.value == 2
    assert assigned.value == 7
    assert incremented.value == 9
    assert decremented.value == 8
    assert appended.value == ["same", "same"]
    assert object_set.value == {"a/b": {"~key": 2}}
    assert object_delete.value == {"a/b": {}}
    assert deleted.state_exists is True and deleted.present is False
    assert all(candidate.value_digest for candidate in (initialized, assigned, incremented))
    assert assigned.canonical == "7"
    assert "PRIVATE" not in repr(assigned)


def test_missing_tombstone_null_and_candidate_getters_are_distinct_and_copied() -> None:
    contract = _contract(
        [
            _variable(
                "n",
                "nullable",
                value_schema=_schema(
                    ["null", "object"],
                    properties={"x": {"type": "array", "items": {"type": "integer"}}},
                ),
            ),
        ]
    )
    assigned_null = _calculate(contract, "n", "assign", value=None)
    missing = _calculate(contract, "n", "initialize")
    tombstone = _calculate(
        contract,
        "n",
        "variable_delete",
        exists=True,
        present=True,
        previous={},
    )
    assert assigned_null.present and assigned_null.canonical == "null"
    assert missing.state_exists is False and tombstone.state_exists is True
    assert not missing.present and not tombstone.present
    assert missing.value_digest == tombstone.value_digest

    assigned = _calculate(contract, "n", "assign", value={"x": [1]})
    returned = assigned.value
    returned["x"].append(2)
    assert assigned.value == {"x": [1]}


def test_numeric_boolean_nonfinite_and_integer_ref_rules_are_strict() -> None:
    integer_schema: dict[str, object] = {
        "$defs": {"integerValue": {"type": "integer"}},
        "$ref": "#/$defs/integerValue",
    }
    contract = _contract(
        [
            _variable("integer", "integer", value_schema=integer_schema),
            _variable("number", "number", value_schema=_schema("number")),
        ]
    )
    assert (
        _calculate(
            contract, "integer", "increment", exists=True, present=True, previous=2, by=1.0
        ).value
        == 3
    )
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "integer", "increment", exists=True, present=True, previous=2, by=0.5)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "integer", "increment", exists=True, present=True, previous=True)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "number", "assign", value=True)
    for invalid in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(StrictVariableLogicInvalid):
            _calculate(contract, "number", "assign", value=invalid)
        with pytest.raises(StrictVariableLogicInvalid):
            _calculate(
                contract, "number", "increment", exists=True, present=True, previous=1, by=invalid
            )
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(
            contract,
            "number",
            "increment",
            exists=True,
            present=True,
            previous=1e308,
            by=1e308,
        )


def test_operation_arguments_missing_states_and_parent_lookup_are_rejected() -> None:
    contract = _contract(
        [
            _variable("n", "count", value_schema=_schema("integer")),
            _variable("parent", "same", owner_scope="/do/1", value_schema=_schema("integer")),
        ]
    )
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "n", "assign", value=1, by=2)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "n", "assign")
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "n", "increment", exists=True, present=False)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "n", "assign", scope="/do/1", value=1)


def test_object_pointer_parent_rules_required_rollback_and_bad_escapes() -> None:
    contract = _contract(
        [
            _variable(
                "object",
                "object",
                value_schema=_schema(
                    "object",
                    properties={
                        "required": {"type": "integer"},
                        "nested": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                    },
                    required=["required", "nested"],
                ),
            )
        ]
    )
    original = {"required": 1, "nested": {}}
    original_copy = copy.deepcopy(original)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(
            contract,
            "object",
            "object_delete",
            exists=True,
            present=True,
            previous=original,
            pointer="/required",
        )
    assert original == original_copy
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(
            contract,
            "object",
            "object_set",
            exists=True,
            present=True,
            previous=original,
            pointer="/missing/child",
            value=3,
        )
    for pointer in ("", "required", "/bad~2escape", "/nested/missing"):
        with pytest.raises(StrictVariableLogicInvalid):
            _calculate(
                contract,
                "object",
                "object_delete",
                exists=True,
                present=True,
                previous=original,
                pointer=pointer,
            )
    assert _calculate(
        contract,
        "object",
        "object_set",
        exists=True,
        present=True,
        previous=original,
        pointer="/nested/new",
        value=3,
    ).value == {"required": 1, "nested": {"new": 3}}


def test_payload_after_value_size_limits_and_json_tree_failures_are_safe() -> None:
    contract = _contract(
        [
            _variable("text", "text", value_schema=_schema("string")),
            _variable(
                "items",
                "items",
                value_schema={"type": "array", "items": {"type": "string"}},
            ),
            _variable(
                "object", "object", value_schema=_schema("object", additionalProperties=True)
            ),
        ]
    )
    too_large_text = "private-value-" + "x" * MAX_OUTPUT_BYTES
    with pytest.raises(StrictVariableLogicResourceLimit) as payload_error:
        _calculate(contract, "text", "assign", value=too_large_text)
    assert too_large_text not in str(payload_error.value)
    assert too_large_text not in repr(payload_error.value)

    prior = ["x" * 8] * 5000
    with pytest.raises(StrictVariableLogicResourceLimit):
        _calculate(
            contract,
            "items",
            "append",
            exists=True,
            present=True,
            previous=prior,
            value="y" * 15_000,
        )
    assert len(prior) == 5000

    cycle: list[object] = []
    cycle.append(cycle)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "items", "assign", value=cycle)
    with pytest.raises(StrictVariableLogicInvalid):
        _calculate(contract, "object", "assign", value={"bad\ud800": 1})


def test_logic_never_writes_variable_state_or_mutation_rows(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-pure-logic"
    run_directory.mkdir()
    binding = StrictWorkflowRunBindingInput(
        run_id="run-0123456789abcdef0123456789abcdef",
        workflow_id="12345678-1234-4234-8234-1234567890ab",
        definition_id="abcdef01-2345-4678-89ab-cdef01234567",
        definition_revision=3,
        contract_sha256="a" * 64,
        binding_digest="b" * 64,
        normalized_input_sha256="c" * 64,
        created_at=_CREATED_AT,
    )
    StrictWorkflowVariableStore(run_directory).initialize_new_database(binding)

    candidate = _calculate(
        _contract([_variable("n", "count", value_schema=_schema("integer"))]),
        "n",
        "assign",
        value=5,
    )
    assert candidate.value == 5
    connection = sqlite3.connect(run_directory / "variables.sqlite")
    try:
        assert connection.execute("SELECT count(*) FROM variable_states").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM variable_mutations").fetchone()[0] == 0
    finally:
        connection.close()
