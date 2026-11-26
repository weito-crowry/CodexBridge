from __future__ import annotations

import copy
import hashlib
import json
import math

import pytest

from codex_bridge.workflows.contracts import (
    DRAFT_2020_12,
    MAX_ARRAY_LENGTH,
    MAX_CONTRACT_BYTES,
    MAX_INPUT_BYTES,
    MAX_OBJECT_PROPERTIES,
    MAX_OUTPUT_BYTES,
    MAX_REF_DEPTH,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_NODES,
    MAX_VALUE_DEPTH,
    MAX_VARIABLE_DECLARATIONS,
    CompiledContract,
    ContractInputInvalid,
    ContractInvalid,
    ContractOutputInvalid,
    ContractResourceLimit,
    canonical_json,
    compile_contract,
    normalize_input,
    parse_strict_json,
    sha256_json,
    validate_output,
)


def _schema(
    properties: dict[str, object] | None = None,
    *,
    required: list[str] | None = None,
    additional: object = False,
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "additionalProperties": additional,
    }
    if properties is not None:
        result["properties"] = properties
    if required is not None:
        result["required"] = required
    result.update(extra)
    return result


def _contract(
    input_schema: dict[str, object] | None = None,
    output_schema: dict[str, object] | None = None,
    variables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "contract_version": 1,
        "input_schema": input_schema if input_schema is not None else _schema(),
        "output_schema": output_schema if output_schema is not None else _schema(),
        "variables": variables if variables is not None else [],
    }


def _variable(
    declaration_id: str,
    name: str,
    owner_scope: str = "workflow",
    value_schema: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "declaration_id": declaration_id,
        "name": name,
        "owner_scope": owner_scope,
        "value_schema": value_schema if value_schema is not None else {"type": "string"},
        "sensitivity": "public",
        **extra,
    }


def test_canonical_json_and_contract_digest_are_order_independent() -> None:
    first = {"z": [1, True, None], "a": {"é": "値"}}
    second = {"a": {"é": "値"}, "z": [1, True, None]}
    expected = json.dumps(
        first,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert canonical_json(first) == expected
    assert sha256_json(first) == sha256_json(second)
    assert sha256_json(first) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "source",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e9999}',
    ],
)
def test_strict_json_rejects_duplicate_keys_and_non_finite_values(source: str) -> None:
    with pytest.raises(ContractInvalid):
        parse_strict_json(source)


def test_strict_json_rejects_invalid_utf8_and_lone_surrogates() -> None:
    with pytest.raises(ContractInvalid):
        parse_strict_json(b"\xff")
    with pytest.raises(ContractInvalid):
        parse_strict_json('{"x":"\\ud800"}')


def test_compile_contract_is_immutable_and_uses_canonical_sha256() -> None:
    raw = _contract()
    original = copy.deepcopy(raw)
    compiled = compile_contract(raw)
    assert isinstance(compiled, CompiledContract)
    assert raw == original
    assert (
        compiled.contract_sha256
        == hashlib.sha256(compiled.contract_json_canonical.encode("utf-8")).hexdigest()
    )
    assert compiled.contract_json_canonical == canonical_json(raw)
    compiled.input_schema["properties"] = {"outside": {"type": "integer"}}
    assert compiled.input_schema == raw["input_schema"]
    returned_variables = compiled.variables
    returned_variables.append(_variable("x", "y"))
    assert compiled.variables == []


def test_compiled_contract_repr_hides_contract_values_but_keeps_digest() -> None:
    sentinel = "private-contract-value-sentinel"
    raw = _contract(
        input_schema=_schema(properties={"defaulted": {"type": "string", "default": sentinel}}),
        variables=[_variable("private", "private", initial=sentinel)],
    )

    compiled = compile_contract(raw)
    rendered = repr(compiled)

    assert sentinel not in rendered
    assert compiled.contract_sha256 in rendered
    assert compiled.contract_json_canonical == canonical_json(raw)
    assert sentinel in compiled.contract_json_canonical


@pytest.mark.parametrize(
    "replacement",
    [
        None,
        {
            "contract_version": True,
            "input_schema": _schema(),
            "output_schema": _schema(),
            "variables": [],
        },
        {
            "contract_version": 1,
            "input_schema": _schema(),
            "output_schema": _schema(),
            "variables": [],
            "extra": 1,
        },
    ],
)
def test_contract_root_and_version_are_strict(replacement: object) -> None:
    with pytest.raises(ContractInvalid):
        compile_contract(replacement)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda schema: schema.update(format="email"),
        lambda schema: schema.update(pattern="x"),
        lambda schema: schema.update(oneOf=[{"type": "string"}]),
        lambda schema: schema.update(unevaluatedProperties=False),
        lambda schema: schema.update(minimum="not-a-number"),
    ],
)
def test_schema_allowlist_and_draft_validation_reject_unsupported_forms(mutation) -> None:
    input_schema = _schema()
    mutation(input_schema)
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema=input_schema))


def test_object_schemas_require_explicit_additional_properties_everywhere() -> None:
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema={"$schema": DRAFT_2020_12, "type": "object"}))
    nested = _schema(properties={"child": {"type": "object", "properties": {}}})
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema=nested))


def test_input_defaults_are_recursive_for_objects_refs_and_existing_array_items() -> None:
    input_schema = _schema(
        properties={
            "settings": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "default": "safe"},
                    "nested": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"enabled": {"type": "boolean", "default": True}},
                        "default": {},
                    },
                    "nullable": {"type": ["string", "null"], "default": "fallback"},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"label": {"type": "string", "default": "item"}},
                },
            },
            "label": {"$ref": "#/$defs/label"},
        },
        required=["settings", "items", "label"],
        **{
            "$defs": {
                "label": {"type": "string", "default": "referenced"},
            },
        },
    )
    compiled = compile_contract(_contract(input_schema=input_schema))
    raw = {"settings": {"nullable": None}, "items": [{}]}
    original = copy.deepcopy(raw)
    normalized = normalize_input(compiled, raw)
    assert raw == original
    assert normalized == {
        "settings": {"mode": "safe", "nested": {"enabled": True}, "nullable": None},
        "items": [{"label": "item"}],
        "label": "referenced",
    }
    assert normalize_input(compiled, normalized) == normalized


def test_explicit_null_is_not_defaulted_and_missing_required_is_rejected() -> None:
    compiled = compile_contract(
        _contract(
            input_schema=_schema(
                properties={"value": {"type": ["string", "null"], "default": "x"}},
                required=["value"],
            )
        )
    )
    assert normalize_input(compiled, {"value": None}) == {"value": None}
    assert normalize_input(compiled, {}) == {"value": "x"}
    assert ContractInputInvalid.code == "workflow_input_invalid"

    required_without_default = compile_contract(
        _contract(
            input_schema=_schema(properties={"value": {"type": "string"}}, required=["value"])
        )
    )
    with pytest.raises(ContractInputInvalid):
        normalize_input(required_without_default, {})


def test_default_must_validate_but_output_and_root_defaults_are_not_applied() -> None:
    invalid = _schema(properties={"count": {"type": "integer", "default": "bad"}})
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema=invalid))

    input_schema = _schema(additional=True, default={"generated": True})
    output_schema = _schema(properties={"result": {"type": "string", "default": "x"}})
    compiled = compile_contract(_contract(input_schema=input_schema, output_schema=output_schema))
    assert normalize_input(compiled, {}) == {}
    assert validate_output(compiled, {}) == {}


@pytest.mark.parametrize(
    "reference",
    [
        "https://example.invalid/schema.json",
        "file:///tmp/schema.json",
        "#/$defs/missing",
        "#/$defs/a/child",
    ],
)
def test_external_or_invalid_refs_are_rejected_without_network_access(reference: str) -> None:
    input_schema = _schema(
        properties={"x": {"$ref": reference}}, **{"$defs": {"a": {"type": "string"}}}
    )
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema=input_schema))


def test_local_ref_pointer_escaping_is_supported() -> None:
    input_schema = _schema(
        properties={"value": {"$ref": "#/$defs/a~1b~0c"}},
        required=["value"],
        **{"$defs": {"a/b~c": {"type": "string", "default": "local"}}},
    )
    compiled = compile_contract(_contract(input_schema=input_schema))
    assert normalize_input(compiled, {}) == {"value": "local"}


def test_cyclic_ref_and_ref_expansion_over_limit_are_rejected() -> None:
    cyclic = _schema(
        properties={"x": {"$ref": "#/$defs/a"}}, **{"$defs": {"a": {"$ref": "#/$defs/a"}}}
    )
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(input_schema=cyclic))

    definitions: dict[str, object] = {}
    for index in range(MAX_REF_DEPTH):
        definitions[f"r{index}"] = (
            {"$ref": f"#/$defs/r{index + 1}"} if index + 1 < MAX_REF_DEPTH else {"type": "string"}
        )
    chain = _schema(properties={"x": {"$ref": "#/$defs/r0"}}, **{"$defs": definitions})
    assert compile_contract(_contract(input_schema=chain))

    definitions[f"r{MAX_REF_DEPTH - 1}"] = {"$ref": f"#/$defs/r{MAX_REF_DEPTH}"}
    definitions[f"r{MAX_REF_DEPTH}"] = {"type": "string"}
    too_deep = _schema(properties={"x": {"$ref": "#/$defs/r0"}}, **{"$defs": definitions})
    with pytest.raises(ContractResourceLimit):
        compile_contract(_contract(input_schema=too_deep))


def test_variable_declarations_validate_identity_scope_sensitivity_and_initial() -> None:
    valid = _contract(
        variables=[
            _variable("v1", "state", value_schema={"type": "integer"}, initial=3),
            _variable("v2", "state", owner_scope="/do/1", value_schema={"type": "integer"}),
        ]
    )
    compiled = compile_contract(valid)
    assert [item["declaration_id"] for item in compiled.variables] == ["v1", "v2"]

    duplicate_id = _contract(variables=[_variable("same", "a"), _variable("same", "b")])
    duplicate_name = _contract(variables=[_variable("a", "same"), _variable("b", "same")])
    secret = _contract(variables=[{**_variable("s", "secret"), "sensitivity": "secret"}])
    invalid_initial = _contract(
        variables=[_variable("i", "count", value_schema={"type": "integer"}, initial=True)]
    )
    for invalid in (duplicate_id, duplicate_name, secret, invalid_initial):
        with pytest.raises(ContractInvalid):
            compile_contract(invalid)


def test_integer_schema_rejects_boolean_and_input_errors_do_not_echo_values() -> None:
    compiled = compile_contract(
        _contract(
            input_schema=_schema(properties={"count": {"type": "integer"}}, required=["count"])
        )
    )
    with pytest.raises(ContractInputInvalid) as error:
        normalize_input(compiled, {"count": True, "private": "do-not-echo"})
    assert error.value.code == "workflow_input_invalid"
    assert "do-not-echo" not in str(error.value)


def test_output_is_strictly_validated_without_defaults_and_returned_as_copy() -> None:
    compiled = compile_contract(
        _contract(
            output_schema=_schema(
                properties={"result": {"type": "string", "default": "not-injected"}},
                required=["result"],
            )
        )
    )
    with pytest.raises(ContractOutputInvalid):
        validate_output(compiled, {})
    accepted = validate_output(compiled, {"result": "ok"})
    accepted["result"] = "changed"
    assert validate_output(compiled, {"result": "ok"}) == {"result": "ok"}


def test_d1_22_mock_child_output_is_validated_at_contract_boundary() -> None:
    """Cover contract validation for a mock child; D6 owns parent return routing."""
    compiled = compile_contract(
        _contract(
            output_schema=_schema(
                properties={
                    "return_payload": _schema(
                        properties={"answer": {"type": "string"}},
                        required=["answer"],
                    )
                },
                required=["return_payload"],
            )
        )
    )

    mock_child_output = {"return_payload": {"answer": "verified child output"}}
    assert validate_output(compiled, mock_child_output) == mock_child_output

    invalid_mock_child_output = {"return_payload": {"answer": 42}}
    with pytest.raises(ContractOutputInvalid):
        validate_output(compiled, invalid_mock_child_output)


def test_normalized_default_and_explicit_value_have_same_canonical_hash() -> None:
    compiled = compile_contract(
        _contract(input_schema=_schema(properties={"mode": {"type": "string", "default": "safe"}}))
    )
    omitted = normalize_input(compiled, {})
    explicit = normalize_input(compiled, {"mode": "safe"})
    assert omitted == explicit == {"mode": "safe"}
    assert sha256_json(omitted) == sha256_json(explicit)


def test_additional_properties_true_false_and_schema_are_honored() -> None:
    allows_unknown = compile_contract(_contract(input_schema=_schema(additional=True)))
    assert normalize_input(allows_unknown, {"extra": {"nested": True}}) == {
        "extra": {"nested": True}
    }

    denies_unknown = compile_contract(_contract(input_schema=_schema(additional=False)))
    with pytest.raises(ContractInputInvalid):
        normalize_input(denies_unknown, {"extra": 1})

    validates_unknown = compile_contract(
        _contract(input_schema=_schema(additional={"type": "integer"}))
    )
    assert normalize_input(validates_unknown, {"extra": 3}) == {"extra": 3}
    with pytest.raises(ContractInputInvalid):
        normalize_input(validates_unknown, {"extra": True})


def test_enum_const_and_numeric_string_array_constraints_are_enforced() -> None:
    compiled = compile_contract(
        _contract(
            input_schema=_schema(
                properties={
                    "mode": {"type": "string", "enum": ["safe", "fast"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 4},
                    "kind": {"const": "issue", "type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                    "label": {"type": "string", "minLength": 1, "maxLength": 4},
                },
                required=["mode", "count", "kind", "items", "label"],
            )
        )
    )
    valid = {"mode": "safe", "count": 2, "kind": "issue", "items": ["a"], "label": "x"}
    assert normalize_input(compiled, valid) == valid
    for field, value in (
        ("mode", "other"),
        ("count", 0),
        ("count", 5),
        ("kind", "task"),
        ("items", ["a", "b", "c"]),
        ("label", ""),
    ):
        invalid = {**valid, field: value}
        with pytest.raises(ContractInputInvalid):
            normalize_input(compiled, invalid)


def test_variable_shape_extra_keys_and_schema_defaults_are_checked() -> None:
    with pytest.raises(ContractInvalid):
        compile_contract(_contract(variables=[{**_variable("x", "x"), "unexpected": 1}]))

    with pytest.raises(ContractInvalid):
        compile_contract(
            _contract(
                variables=[
                    _variable(
                        "x",
                        "x",
                        value_schema={"type": "integer", "default": "not-an-integer"},
                    )
                ]
            )
        )


def test_contract_and_value_size_caps_are_enforced_at_the_exact_boundary() -> None:
    base = _contract(input_schema=_schema(title=""))
    base_size = len(canonical_json(base).encode("utf-8"))
    exact_contract = copy.deepcopy(base)
    exact_contract["input_schema"]["title"] = "x" * (MAX_CONTRACT_BYTES - base_size)
    compile_contract(exact_contract)
    over_contract = copy.deepcopy(exact_contract)
    over_contract["input_schema"]["title"] += "x"
    with pytest.raises(ContractResourceLimit):
        compile_contract(over_contract)

    compiled = compile_contract(
        _contract(input_schema=_schema(properties={"value": {"type": "string"}}))
    )
    input_overhead = len(canonical_json({"value": ""}).encode("utf-8"))
    at_input_limit = {"value": "x" * (MAX_INPUT_BYTES - input_overhead)}
    assert len(canonical_json(at_input_limit).encode("utf-8")) == MAX_INPUT_BYTES
    assert normalize_input(compiled, at_input_limit) == at_input_limit
    with pytest.raises(ContractResourceLimit):
        normalize_input(compiled, {"value": at_input_limit["value"] + "x"})

    output_compiled = compile_contract(
        _contract(output_schema=_schema(properties={"value": {"type": "string"}}))
    )
    output_overhead = len(canonical_json({"value": ""}).encode("utf-8"))
    at_output_limit = {"value": "x" * (MAX_OUTPUT_BYTES - output_overhead)}
    assert validate_output(output_compiled, at_output_limit) == at_output_limit
    with pytest.raises(ContractResourceLimit):
        validate_output(output_compiled, {"value": at_output_limit["value"] + "x"})


def test_declaration_count_and_schema_depth_node_caps_are_enforced() -> None:
    variables = [_variable(f"d{index}", f"n{index}") for index in range(MAX_VARIABLE_DECLARATIONS)]
    assert compile_contract(_contract(variables=variables))
    with pytest.raises(ContractResourceLimit):
        compile_contract(_contract(variables=[*variables, _variable("extra", "extra")]))

    def nested_schema(depth: int) -> dict[str, object]:
        root = _schema()
        current = root
        for _ in range(depth - 1):
            child = _schema()
            current["properties"] = {"child": child}
            current = child
        return root

    assert compile_contract(_contract(input_schema=nested_schema(MAX_SCHEMA_DEPTH)))
    with pytest.raises(ContractResourceLimit):
        compile_contract(_contract(input_schema=nested_schema(MAX_SCHEMA_DEPTH + 1)))

    def with_property_nodes(count: int) -> dict[str, object]:
        return _schema(properties={f"p{index}": {"type": "string"} for index in range(count)})

    # Input root + 510 property schemas + output root is exactly 512 nodes.
    assert compile_contract(_contract(input_schema=with_property_nodes(MAX_SCHEMA_NODES - 2)))
    with pytest.raises(ContractResourceLimit):
        compile_contract(_contract(input_schema=with_property_nodes(MAX_SCHEMA_NODES - 1)))


def test_value_depth_object_property_and_array_length_caps_are_enforced() -> None:
    compiled = compile_contract(_contract(input_schema=_schema(additional=True)))

    def nested_value(depth: int) -> dict[str, object]:
        value: dict[str, object] = {}
        current = value
        for _ in range(depth - 1):
            child: dict[str, object] = {}
            current["child"] = child
            current = child
        return value

    assert normalize_input(compiled, nested_value(MAX_VALUE_DEPTH)) == nested_value(MAX_VALUE_DEPTH)
    with pytest.raises(ContractResourceLimit):
        normalize_input(compiled, nested_value(MAX_VALUE_DEPTH + 1))

    assert (
        len(normalize_input(compiled, {f"k{i}": i for i in range(MAX_OBJECT_PROPERTIES)}))
        == MAX_OBJECT_PROPERTIES
    )
    with pytest.raises(ContractResourceLimit):
        normalize_input(compiled, {f"k{i}": i for i in range(MAX_OBJECT_PROPERTIES + 1)})

    assert (
        len(normalize_input(compiled, {"items": [0] * MAX_ARRAY_LENGTH})["items"])
        == MAX_ARRAY_LENGTH
    )
    with pytest.raises(ContractResourceLimit):
        normalize_input(compiled, {"items": [0] * (MAX_ARRAY_LENGTH + 1)})


def test_circular_python_values_and_invalid_canonical_values_are_rejected() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ContractInvalid):
        canonical_json(cyclic)
    with pytest.raises(ContractInvalid):
        canonical_json({1: "bad-key"})
    with pytest.raises(ContractInvalid):
        canonical_json(math.inf)
    with pytest.raises(ContractInvalid):
        canonical_json("\ud800")


def test_resource_errors_have_safe_machine_codes() -> None:
    compiled = compile_contract(_contract(input_schema=_schema(additional=True)))
    with pytest.raises(ContractResourceLimit) as error:
        normalize_input(compiled, {"oversized": "x" * MAX_INPUT_BYTES})
    assert error.value.code == "workflow_resource_limit"
    assert "oversized" not in str(error.value)
