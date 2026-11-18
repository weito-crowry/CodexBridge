"""Pure strict-v1 Workflow contract validation and input normalization.

This module deliberately has no persistence, runtime, HTTP, MCP, or OWS
integration.  Callers opt into it explicitly for a pinned strict contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, NoReturn, cast

from jsonschema import Draft202012Validator

CONTRACT_VERSION = 1
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

MAX_CONTRACT_BYTES = 64 * 1024
MAX_INPUT_BYTES = 96 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_VARIABLE_INITIAL_BYTES = 64 * 1024
MAX_VARIABLE_DECLARATIONS = 256
MAX_SCHEMA_DEPTH = 24
MAX_SCHEMA_NODES = 512
MAX_REF_DEPTH = 16
MAX_SCHEMA_PROPERTIES = 512
MAX_VALUE_DEPTH = 32
MAX_OBJECT_PROPERTIES = 1024
MAX_ARRAY_LENGTH = 10_000

_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "default",
        "$defs",
        "$ref",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "title",
        "description",
    }
)
_JSON_TYPES = frozenset({"string", "integer", "number", "boolean", "null", "array", "object"})
_CONTRACT_KEYS = frozenset({"contract_version", "input_schema", "output_schema", "variables"})
_VARIABLE_REQUIRED_KEYS = frozenset(
    {"declaration_id", "name", "owner_scope", "value_schema", "sensitivity"}
)
_VARIABLE_OPTIONAL_KEYS = frozenset({"initial"})


class ContractError(ValueError):
    """Base class with a stable, value-free machine-readable error code."""

    code = "workflow_contract_invalid"
    safe_message = "Workflow contract operation failed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ContractInvalid(ContractError):
    code = "workflow_contract_invalid"
    safe_message = "Workflow contract is invalid."


class ContractInputInvalid(ContractError):
    code = "workflow_input_invalid"
    safe_message = "Workflow input is invalid."


class ContractOutputInvalid(ContractError):
    code = "workflow_output_invalid"
    safe_message = "Workflow output is invalid."


class ContractResourceLimit(ContractError):
    code = "workflow_resource_limit"
    safe_message = "Workflow contract resource limit exceeded."


_ErrorClass = type[ContractError]


def _raise(error_type: _ErrorClass) -> NoReturn:
    raise error_type() from None


def _check_json_tree(
    value: Any,
    *,
    invalid_error: _ErrorClass = ContractInvalid,
    max_depth: int | None = None,
    max_object_properties: int | None = None,
    max_array_length: int | None = None,
) -> None:
    """Reject non-JSON Python objects, cycles, non-finite numbers and bad UTF-8."""
    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 1, False)]
    while stack:
        current, depth, closing = stack.pop()
        if closing:
            active.remove(id(current))
            continue

        current_type = type(current)
        if current is None or current_type is bool or current_type is int:
            continue
        if current_type is float:
            if not math.isfinite(current):
                _raise(invalid_error)
            continue
        if current_type is str:
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                _raise(invalid_error)
            continue

        if current_type is dict:
            if max_depth is not None and depth > max_depth:
                _raise(ContractResourceLimit)
            if max_object_properties is not None and len(current) > max_object_properties:
                _raise(ContractResourceLimit)
            identity = id(current)
            if identity in active:
                _raise(invalid_error)
            active.add(identity)
            stack.append((current, depth, True))
            entries = list(current.items())
            for key, child in reversed(entries):
                if type(key) is not str:
                    _raise(invalid_error)
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError:
                    _raise(invalid_error)
                stack.append((child, depth + 1, False))
            continue

        if current_type is list:
            if max_depth is not None and depth > max_depth:
                _raise(ContractResourceLimit)
            if max_array_length is not None and len(current) > max_array_length:
                _raise(ContractResourceLimit)
            identity = id(current)
            if identity in active:
                _raise(invalid_error)
            active.add(identity)
            stack.append((current, depth, True))
            for child in reversed(current):
                stack.append((child, depth + 1, False))
            continue

        _raise(invalid_error)


def _serialize_json(
    value: Any,
    *,
    invalid_error: _ErrorClass = ContractInvalid,
    max_bytes: int | None = None,
) -> str:
    _check_json_tree(value, invalid_error=invalid_error)
    encoder = json.JSONEncoder(
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    chunks: list[str] = []
    byte_count = 0
    try:
        for chunk in encoder.iterencode(value):
            byte_count += len(chunk.encode("utf-8", errors="strict"))
            if max_bytes is not None and byte_count > max_bytes:
                _raise(ContractResourceLimit)
            chunks.append(chunk)
    except ContractError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError, OverflowError):
        _raise(invalid_error)
    return "".join(chunks)


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON using the legacy canonical encoding."""
    return _serialize_json(value)


def sha256_json(value: Any) -> str:
    """Return SHA-256 of :func:`canonical_json` encoded as UTF-8."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _DuplicateJSONKey(ValueError):
    pass


def _object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _reject_json_constant(_constant: str) -> NoReturn:
    raise ValueError


def parse_strict_json(source: str | bytes) -> Any:
    """Parse JSON while rejecting duplicate keys, non-finite values and bad UTF-8."""
    if type(source) is bytes:
        try:
            text = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _raise(ContractInvalid)
    elif type(source) is str:
        text = source
    else:
        _raise(ContractInvalid)

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_json_constant,
        )
        _check_json_tree(value)
        _serialize_json(value)
    except ContractError:
        raise
    except (json.JSONDecodeError, _DuplicateJSONKey, TypeError, ValueError, RecursionError):
        _raise(ContractInvalid)
    return value


@dataclass(slots=True)
class _SchemaBudget:
    nodes: int = 0
    properties: int = 0


def _decode_pointer_segment(segment: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char != "~":
            result.append(char)
            index += 1
            continue
        if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
            _raise(ContractInvalid)
        result.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _ref_name(reference: Any, definitions: dict[str, Any]) -> str:
    if type(reference) is not str:
        _raise(ContractInvalid)
    parts = reference.split("/")
    if len(parts) != 3 or parts[0] != "#" or parts[1] != "$defs":
        _raise(ContractInvalid)
    name = _decode_pointer_segment(parts[2])
    if name not in definitions:
        _raise(ContractInvalid)
    return name


def _schema_children(schema: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    properties = schema.get("properties")
    if type(properties) is dict:
        children.extend(properties.values())
    items = schema.get("items")
    if type(items) is dict:
        children.append(items)
    additional = schema.get("additionalProperties")
    if type(additional) is dict:
        children.append(additional)
    definitions = schema.get("$defs")
    if type(definitions) is dict:
        children.extend(definitions.values())
    return children


def _validate_schema_shape(
    schema: Any,
    *,
    budget: _SchemaBudget,
    depth: int,
) -> None:
    if type(schema) is not dict:
        _raise(ContractInvalid)
    if depth > MAX_SCHEMA_DEPTH:
        _raise(ContractResourceLimit)
    budget.nodes += 1
    if budget.nodes > MAX_SCHEMA_NODES:
        _raise(ContractResourceLimit)

    unknown = set(schema) - _SCHEMA_KEYWORDS
    if unknown:
        _raise(ContractInvalid)
    if "$schema" in schema and schema["$schema"] != DRAFT_2020_12:
        _raise(ContractInvalid)

    schema_type = schema.get("type")
    if schema_type is not None:
        if type(schema_type) is str:
            if schema_type not in _JSON_TYPES:
                _raise(ContractInvalid)
            allowed_types = {schema_type}
        elif type(schema_type) is list:
            if not schema_type or any(
                type(item) is not str or item not in _JSON_TYPES for item in schema_type
            ):
                _raise(ContractInvalid)
            if len(set(schema_type)) != len(schema_type):
                _raise(ContractInvalid)
            allowed_types = set(schema_type)
        else:
            _raise(ContractInvalid)
    else:
        allowed_types = set()

    if "properties" in schema:
        properties = schema["properties"]
        if type(properties) is not dict or "object" not in allowed_types:
            _raise(ContractInvalid)
        budget.properties += len(properties)
        if budget.properties > MAX_SCHEMA_PROPERTIES:
            _raise(ContractResourceLimit)
        if any(type(name) is not str for name in properties):
            _raise(ContractInvalid)

    if "object" in allowed_types and "additionalProperties" not in schema:
        _raise(ContractInvalid)

    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if type(additional) is not bool and type(additional) is not dict:
            _raise(ContractInvalid)
    if "items" in schema and type(schema["items"]) is not dict:
        _raise(ContractInvalid)
    if "$defs" in schema and type(schema["$defs"]) is not dict:
        _raise(ContractInvalid)
    if "required" in schema:
        required = schema["required"]
        if (
            type(required) is not list
            or any(type(item) is not str for item in required)
            or len(set(required)) != len(required)
        ):
            _raise(ContractInvalid)
    if "$ref" in schema and type(schema["$ref"]) is not str:
        _raise(ContractInvalid)

    for child in _schema_children(schema):
        _validate_schema_shape(child, budget=budget, depth=depth + 1)


def _collect_refs(schema: dict[str, Any], definitions: dict[str, Any]) -> list[str]:
    found: list[str] = []
    stack: list[dict[str, Any]] = [schema]
    while stack:
        current = stack.pop()
        if "$ref" in current:
            found.append(_ref_name(current["$ref"], definitions))
        stack.extend(_schema_children(current))
    return found


def _validate_references(schema: dict[str, Any]) -> None:
    raw_definitions = schema.get("$defs", {})
    if type(raw_definitions) is not dict:
        _raise(ContractInvalid)
    definitions: dict[str, Any] = raw_definitions
    if any(type(name) is not str for name in definitions):
        _raise(ContractInvalid)

    edges: dict[str, list[str]] = {}
    for name, definition in definitions.items():
        if type(definition) is not dict:
            _raise(ContractInvalid)
        edges[name] = _collect_refs(definition, definitions)

    active: set[str] = set()
    depths: dict[str, int] = {}

    def depth_from(name: str) -> int:
        if name in active:
            _raise(ContractInvalid)
        if name in depths:
            return depths[name]
        active.add(name)
        result = 0
        for child in edges.get(name, []):
            result = max(result, 1 + depth_from(child))
        active.remove(name)
        if result > MAX_REF_DEPTH:
            _raise(ContractResourceLimit)
        depths[name] = result
        return result

    for name in definitions:
        depth_from(name)

    for target in _collect_refs(schema, definitions):
        if 1 + depth_from(target) > MAX_REF_DEPTH:
            _raise(ContractResourceLimit)


def _iter_schema_objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack = [schema]
    while stack:
        current = stack.pop()
        result.append(current)
        stack.extend(_schema_children(current))
    return result


def _check_schema_and_defaults(schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for subschema in _iter_schema_objects(schema):
            if "default" not in subschema:
                continue
            default = subschema["default"]
            _check_json_tree(
                default,
                invalid_error=ContractInvalid,
                max_depth=MAX_VALUE_DEPTH,
                max_object_properties=MAX_OBJECT_PROPERTIES,
                max_array_length=MAX_ARRAY_LENGTH,
            )
            errors = validator.descend(default, subschema)
            if next(errors, None) is not None:
                _raise(ContractInvalid)
    except ContractError:
        raise
    except Exception:
        _raise(ContractInvalid)


def _validate_root_schema(schema: Any, *, require_object_root: bool) -> dict[str, Any]:
    if type(schema) is not dict:
        _raise(ContractInvalid)
    if require_object_root and (
        schema.get("$schema") != DRAFT_2020_12
        or schema.get("type") != "object"
        or "additionalProperties" not in schema
    ):
        _raise(ContractInvalid)
    if "$schema" in schema and schema["$schema"] != DRAFT_2020_12:
        _raise(ContractInvalid)
    return schema


def _validator_accepts(schema: dict[str, Any], value: Any) -> bool:
    try:
        return bool(Draft202012Validator(schema).is_valid(value))
    except Exception:
        return False


def _validate_contract(
    raw: object,
) -> tuple[str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if type(raw) is not dict:
        _raise(ContractInvalid)
    _check_json_tree(raw, max_depth=128)
    canonical = _serialize_json(raw, max_bytes=MAX_CONTRACT_BYTES)
    if set(raw) != _CONTRACT_KEYS:
        _raise(ContractInvalid)
    if type(raw["contract_version"]) is not int or raw["contract_version"] != CONTRACT_VERSION:
        _raise(ContractInvalid)

    input_schema = _validate_root_schema(raw["input_schema"], require_object_root=True)
    output_schema = _validate_root_schema(raw["output_schema"], require_object_root=True)
    variables_value = raw["variables"]
    if type(variables_value) is not list:
        _raise(ContractInvalid)
    if len(variables_value) > MAX_VARIABLE_DECLARATIONS:
        _raise(ContractResourceLimit)

    budget = _SchemaBudget()
    for schema in (input_schema, output_schema):
        _validate_schema_shape(schema, budget=budget, depth=1)
    for variable in variables_value:
        if type(variable) is not dict:
            _raise(ContractInvalid)
        _validate_schema_shape(variable.get("value_schema"), budget=budget, depth=1)

    for schema in (input_schema, output_schema):
        _validate_references(schema)
        _check_schema_and_defaults(schema)

    declaration_ids: set[str] = set()
    names_by_scope: set[tuple[str, str]] = set()
    variables: list[dict[str, Any]] = []
    for variable in variables_value:
        keys = set(variable)
        if not _VARIABLE_REQUIRED_KEYS <= keys or keys - (
            _VARIABLE_REQUIRED_KEYS | _VARIABLE_OPTIONAL_KEYS
        ):
            _raise(ContractInvalid)
        for field_name in ("declaration_id", "name", "owner_scope"):
            if type(variable[field_name]) is not str or not variable[field_name]:
                _raise(ContractInvalid)
        if variable["sensitivity"] != "public" or type(variable["sensitivity"]) is not str:
            _raise(ContractInvalid)
        declaration_id = variable["declaration_id"]
        name = variable["name"]
        owner_scope = variable["owner_scope"]
        if declaration_id in declaration_ids or (owner_scope, name) in names_by_scope:
            _raise(ContractInvalid)
        declaration_ids.add(declaration_id)
        names_by_scope.add((owner_scope, name))

        value_schema = variable["value_schema"]
        _validate_references(value_schema)
        _check_schema_and_defaults(value_schema)
        if "initial" in variable:
            initial = variable["initial"]
            _check_json_tree(
                initial,
                invalid_error=ContractInvalid,
                max_depth=MAX_VALUE_DEPTH,
                max_object_properties=MAX_OBJECT_PROPERTIES,
                max_array_length=MAX_ARRAY_LENGTH,
            )
            initial_json = _serialize_json(initial, max_bytes=MAX_VARIABLE_INITIAL_BYTES)
            initial_copy = json.loads(initial_json)
            if not _validator_accepts(value_schema, initial_copy):
                _raise(ContractInvalid)
        variables.append(variable)

    return canonical, input_schema, output_schema, variables


@dataclass(frozen=True, slots=True)
class CompiledContract:
    """Immutable canonical snapshot; properties return newly parsed copies."""

    contract_sha256: str
    contract_json_canonical: str = field(repr=False)
    _input_schema_json: str = field(repr=False)
    _output_schema_json: str = field(repr=False)
    _variables_json: str = field(repr=False)

    @property
    def contract_version(self) -> int:
        return CONTRACT_VERSION

    @property
    def input_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._input_schema_json))

    @property
    def output_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._output_schema_json))

    @property
    def variables(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], json.loads(self._variables_json))


def compile_contract(raw: object) -> CompiledContract:
    """Validate and compile a strict-v1 contract without retaining caller data."""
    canonical, input_schema, output_schema, variables = _validate_contract(raw)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return CompiledContract(
        contract_sha256=digest,
        contract_json_canonical=canonical,
        _input_schema_json=canonical_json(input_schema),
        _output_schema_json=canonical_json(output_schema),
        _variables_json=canonical_json(variables),
    )


def _schema_default(
    schema: dict[str, Any], definitions: dict[str, Any], *, depth: int = 0
) -> tuple[bool, Any]:
    if depth > MAX_REF_DEPTH:
        _raise(ContractResourceLimit)
    if "default" in schema:
        return True, schema["default"]
    if "$ref" in schema:
        target = definitions[_ref_name(schema["$ref"], definitions)]
        return _schema_default(target, definitions, depth=depth + 1)
    return False, None


def _apply_defaults(
    value: Any,
    schema: dict[str, Any],
    definitions: dict[str, Any],
    *,
    ref_depth: int = 0,
) -> None:
    if ref_depth > MAX_REF_DEPTH:
        _raise(ContractResourceLimit)
    if "$ref" in schema:
        target = definitions[_ref_name(schema["$ref"], definitions)]
        _apply_defaults(value, target, definitions, ref_depth=ref_depth + 1)

    if type(value) is dict:
        properties = schema.get("properties", {})
        if type(properties) is dict:
            for name, property_schema in properties.items():
                if name not in value:
                    has_default, default = _schema_default(property_schema, definitions)
                    if has_default:
                        value[name] = deepcopy(default)
                if name in value:
                    _apply_defaults(value[name], property_schema, definitions)

        additional = schema.get("additionalProperties")
        if type(additional) is dict:
            property_names = set(properties) if type(properties) is dict else set()
            for name in list(value):
                if name not in property_names:
                    _apply_defaults(value[name], additional, definitions)
        return

    if type(value) is list:
        items = schema.get("items")
        if type(items) is dict:
            for item in value:
                _apply_defaults(item, items, definitions)


def _load_compiled_schema(schema_json: str, error_type: _ErrorClass) -> dict[str, Any]:
    try:
        result = json.loads(schema_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        _raise(error_type)
    if type(result) is not dict:
        _raise(error_type)
    return result


def normalize_input(contract: CompiledContract, raw_input: object) -> dict[str, Any]:
    """Apply input property defaults once, validate, and return an isolated copy."""
    if not isinstance(contract, CompiledContract) or type(raw_input) is not dict:
        _raise(ContractInputInvalid)
    _check_json_tree(
        raw_input,
        invalid_error=ContractInputInvalid,
        max_depth=MAX_VALUE_DEPTH,
        max_object_properties=MAX_OBJECT_PROPERTIES,
        max_array_length=MAX_ARRAY_LENGTH,
    )
    raw_json = _serialize_json(
        raw_input,
        invalid_error=ContractInputInvalid,
        max_bytes=MAX_INPUT_BYTES,
    )
    try:
        normalized = json.loads(raw_json)
        schema = _load_compiled_schema(contract._input_schema_json, ContractInputInvalid)
        definitions = schema.get("$defs", {})
        _apply_defaults(normalized, schema, definitions)
        _check_json_tree(
            normalized,
            invalid_error=ContractInputInvalid,
            max_depth=MAX_VALUE_DEPTH,
            max_object_properties=MAX_OBJECT_PROPERTIES,
            max_array_length=MAX_ARRAY_LENGTH,
        )
        normalized_json = _serialize_json(
            normalized,
            invalid_error=ContractInputInvalid,
            max_bytes=MAX_INPUT_BYTES,
        )
        isolated = json.loads(normalized_json)
        if type(isolated) is not dict or not _validator_accepts(schema, isolated):
            _raise(ContractInputInvalid)
        return isolated
    except ContractError:
        raise
    except Exception:
        _raise(ContractInputInvalid)


def validate_output(contract: CompiledContract, output: object) -> dict[str, Any]:
    """Validate output without applying defaults and return an isolated copy."""
    if not isinstance(contract, CompiledContract) or type(output) is not dict:
        _raise(ContractOutputInvalid)
    _check_json_tree(
        output,
        invalid_error=ContractOutputInvalid,
        max_depth=MAX_VALUE_DEPTH,
        max_object_properties=MAX_OBJECT_PROPERTIES,
        max_array_length=MAX_ARRAY_LENGTH,
    )
    output_json = _serialize_json(
        output,
        invalid_error=ContractOutputInvalid,
        max_bytes=MAX_OUTPUT_BYTES,
    )
    try:
        isolated = json.loads(output_json)
        schema = _load_compiled_schema(contract._output_schema_json, ContractOutputInvalid)
        if type(isolated) is not dict or not _validator_accepts(schema, isolated):
            _raise(ContractOutputInvalid)
        return isolated
    except ContractError:
        raise
    except Exception:
        _raise(ContractOutputInvalid)


__all__ = [
    "CONTRACT_VERSION",
    "DRAFT_2020_12",
    "MAX_ARRAY_LENGTH",
    "MAX_CONTRACT_BYTES",
    "MAX_INPUT_BYTES",
    "MAX_OBJECT_PROPERTIES",
    "MAX_OUTPUT_BYTES",
    "MAX_REF_DEPTH",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_NODES",
    "MAX_SCHEMA_PROPERTIES",
    "MAX_VALUE_DEPTH",
    "MAX_VARIABLE_DECLARATIONS",
    "MAX_VARIABLE_INITIAL_BYTES",
    "CompiledContract",
    "ContractError",
    "ContractInputInvalid",
    "ContractInvalid",
    "ContractOutputInvalid",
    "ContractResourceLimit",
    "canonical_json",
    "compile_contract",
    "normalize_input",
    "parse_strict_json",
    "sha256_json",
    "validate_output",
]
