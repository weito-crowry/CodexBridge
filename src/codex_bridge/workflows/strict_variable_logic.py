"""Pure typed candidates for the opt-in strict workflow variable model.

This module deliberately does not open SQLite or commit variable state. The
caller must supply the explicit frame scope and the complete previous-state
projection; an ACTIVE frame check belongs to the later I4-C transaction.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, cast

from . import contracts as _contracts
from .contracts import (
    MAX_ARRAY_LENGTH,
    MAX_OBJECT_PROPERTIES,
    MAX_OUTPUT_BYTES,
    MAX_VALUE_DEPTH,
    CompiledContract,
    ContractError,
    ContractResourceLimit,
    canonical_json,
    compile_contract,
    parse_strict_json,
    sha256_json,
)


class _MissingValue:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _MissingValue()
_UNSET = object()
_OPERATIONS = frozenset(
    {
        "initialize",
        "assign",
        "increment",
        "decrement",
        "append",
        "object_set",
        "object_delete",
        "variable_delete",
    }
)
_OPERATION_ARGUMENTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "initialize": (frozenset(), frozenset({"value"})),
    "assign": (frozenset({"value"}), frozenset()),
    "increment": (frozenset(), frozenset({"by"})),
    "decrement": (frozenset(), frozenset({"by"})),
    "append": (frozenset({"value"}), frozenset()),
    "object_set": (frozenset({"pointer", "value"}), frozenset()),
    "object_delete": (frozenset({"pointer"}), frozenset()),
    "variable_delete": (frozenset(), frozenset()),
}


class StrictVariableLogicError(ValueError):
    """Base class for stable, value-free pure variable-operation errors."""

    code = "strict_variable_invalid"
    safe_message = "Strict workflow variable operation is invalid."

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class StrictVariableLogicInvalid(StrictVariableLogicError):
    code = "strict_variable_invalid"
    safe_message = "Strict workflow variable operation is invalid."


class StrictVariableLogicConflict(StrictVariableLogicError):
    code = "strict_variable_conflict"
    safe_message = "Strict workflow variable operation conflicts with current state."


class StrictVariableLogicResourceLimit(StrictVariableLogicError):
    code = "strict_variable_resource_limit"
    safe_message = "Strict workflow variable operation exceeds a resource limit."


@dataclass(frozen=True, slots=True, repr=False)
class VariableDeclaration:
    declaration_id: str = field(repr=False)
    name: str = field(repr=False)
    owner_scope: str = field(repr=False)
    sensitivity: str = field(repr=False)
    _value_schema_json: str = field(repr=False)
    has_initial: bool = field(repr=False)
    _initial_json: str | None = field(repr=False)

    @property
    def value_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._value_schema_json))

    @property
    def initial(self) -> Any:
        if not self.has_initial or self._initial_json is None:
            return MISSING
        return json.loads(self._initial_json)

    def __repr__(self) -> str:
        return "VariableDeclaration(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class VariableCandidate:
    declaration_id: str = field(repr=False)
    name: str = field(repr=False)
    owner_scope: str = field(repr=False)
    state_exists: bool = field(repr=False)
    present: bool = field(repr=False)
    _value_json: str | None = field(repr=False)
    value_digest: str = field(repr=False)

    @property
    def value(self) -> Any:
        if not self.present or self._value_json is None:
            return None
        return json.loads(self._value_json)

    @property
    def canonical(self) -> str | None:
        return self._value_json if self.present else None

    def __repr__(self) -> str:
        return "VariableCandidate(<redacted>)"


def resolve_variable_declarations(contract: CompiledContract) -> tuple[VariableDeclaration, ...]:
    """Resolve immutable declarations from the compiled contract snapshot.

    The canonical snapshot is revalidated through I1's compiler so malformed
    hand-constructed CompiledContract instances cannot bypass declaration,
    schema, duplicate-ID, same-scope-name, or sensitivity checks.
    """
    try:
        if not isinstance(contract, CompiledContract):
            raise StrictVariableLogicInvalid()
        source = contract.contract_json_canonical
        if type(source) is not str:
            raise StrictVariableLogicInvalid()
        raw = parse_strict_json(source)
        compiled = compile_contract(raw)
        if (
            compiled.contract_json_canonical != source
            or compiled.contract_sha256 != contract.contract_sha256
        ):
            raise StrictVariableLogicInvalid()

        variables = compiled.variables
        by_id: set[str] = set()
        by_scope_name: set[tuple[str, str]] = set()
        result: list[VariableDeclaration] = []
        for item in variables:
            declaration_id = item.get("declaration_id")
            name = item.get("name")
            owner_scope = item.get("owner_scope")
            sensitivity = item.get("sensitivity")
            schema = item.get("value_schema")
            if (
                type(declaration_id) is not str
                or not declaration_id
                or type(name) is not str
                or not name
                or type(owner_scope) is not str
                or not owner_scope
                or sensitivity != "public"
                or type(sensitivity) is not str
                or type(schema) is not dict
                or declaration_id in by_id
                or (owner_scope, name) in by_scope_name
            ):
                raise StrictVariableLogicInvalid()
            schema_json = canonical_json(schema)
            has_initial = "initial" in item
            initial_json = _snapshot_value(item["initial"]) if has_initial else None
            by_id.add(declaration_id)
            by_scope_name.add((owner_scope, name))
            result.append(
                VariableDeclaration(
                    declaration_id=declaration_id,
                    name=name,
                    owner_scope=owner_scope,
                    sensitivity=sensitivity,
                    _value_schema_json=schema_json,
                    has_initial=has_initial,
                    _initial_json=initial_json,
                )
            )
        return tuple(result)
    except StrictVariableLogicError:
        raise
    except ContractResourceLimit:
        raise StrictVariableLogicResourceLimit() from None
    except ContractError:
        raise StrictVariableLogicInvalid() from None
    except Exception:
        raise StrictVariableLogicInvalid() from None


def resolve_variable_declaration(
    contract: CompiledContract,
    declaration_id: str,
    frame_scope_path: str,
) -> VariableDeclaration:
    """Resolve one declaration only when the explicitly supplied scope owns it."""
    try:
        if type(declaration_id) is not str or type(frame_scope_path) is not str:
            raise StrictVariableLogicInvalid()
        for declaration in resolve_variable_declarations(contract):
            if declaration.declaration_id == declaration_id:
                if declaration.owner_scope != frame_scope_path:
                    raise StrictVariableLogicInvalid()
                return declaration
        raise StrictVariableLogicInvalid()
    except StrictVariableLogicError:
        raise
    except Exception:
        raise StrictVariableLogicInvalid() from None


def calculate_variable_candidate(
    contract: CompiledContract,
    declaration_id: str,
    frame_scope_path: str,
    operation: str,
    *,
    previous_exists: object = _UNSET,
    previous_present: object = _UNSET,
    previous_value: object = MISSING,
    **arguments: object,
) -> VariableCandidate:
    """Calculate a typed after-state candidate without persistence side effects.

    ``previous_exists`` distinguishes a missing row from a tombstone;
    ``previous_present`` distinguishes a value (including JSON null) from a
    tombstone. The caller must also enforce ACTIVE frame authenticity inside
    the I4-C transaction; this pure function cannot verify it.
    """
    try:
        if type(operation) is not str or operation not in _OPERATIONS:
            raise StrictVariableLogicInvalid()
        declaration = resolve_variable_declaration(contract, declaration_id, frame_scope_path)
        _validate_previous_state(previous_exists, previous_present, previous_value)
        previous_copy: Any = MISSING
        if previous_exists is True and previous_present is True:
            previous_json = _snapshot_value(previous_value)
            previous_copy = json.loads(previous_json)
            _validate_value(declaration, previous_copy)
        required, optional = _OPERATION_ARGUMENTS[operation]
        names = set(arguments)
        if not required <= names or names - (required | optional):
            raise StrictVariableLogicInvalid()
        _validate_payload(operation, arguments)

        if operation == "initialize":
            if previous_exists is True:
                raise StrictVariableLogicConflict()
            if "value" in arguments:
                return _build_candidate(
                    declaration, state_exists=True, present=True, value=arguments["value"]
                )
            if not declaration.has_initial:
                return _build_candidate(declaration, state_exists=False, present=False)
            value = declaration.initial
            return _build_candidate(declaration, state_exists=True, present=True, value=value)

        if operation == "assign":
            return _build_candidate(
                declaration,
                state_exists=True,
                present=True,
                value=arguments["value"],
            )

        if previous_exists is not True or previous_present is not True:
            raise StrictVariableLogicInvalid()

        if operation in {"increment", "decrement"}:
            if type(previous_copy) not in (int, float):
                raise StrictVariableLogicInvalid()
            if type(previous_copy) is float and not math.isfinite(previous_copy):
                raise StrictVariableLogicInvalid()
            amount = arguments.get("by", 1)
            if type(amount) not in (int, float):
                raise StrictVariableLogicInvalid()
            numeric_amount = cast(int | float, amount)
            if type(numeric_amount) is float and not math.isfinite(numeric_amount):
                raise StrictVariableLogicInvalid()
            if numeric_amount < 0:
                raise StrictVariableLogicInvalid()
            after = (
                previous_copy + numeric_amount
                if operation == "increment"
                else previous_copy - numeric_amount
            )
            if type(after) is float and not math.isfinite(after):
                raise StrictVariableLogicInvalid()
            if _schema_requires_strict_integer(declaration.value_schema) and type(after) is float:
                if after.is_integer():
                    after = int(after)
                else:
                    raise StrictVariableLogicInvalid()
            return _build_candidate(declaration, state_exists=True, present=True, value=after)

        if operation == "append":
            if type(previous_copy) is not list:
                raise StrictVariableLogicInvalid()
            after_list = list(previous_copy)
            after_list.append(arguments["value"])
            return _build_candidate(declaration, state_exists=True, present=True, value=after_list)

        if operation in {"object_set", "object_delete"}:
            after_object = _object_mutation(
                previous_copy,
                operation,
                arguments["pointer"],
                arguments.get("value", MISSING),
            )
            return _build_candidate(
                declaration, state_exists=True, present=True, value=after_object
            )

        if operation == "variable_delete":
            return _build_candidate(declaration, state_exists=True, present=False)

        raise StrictVariableLogicInvalid()
    except StrictVariableLogicError:
        raise
    except ContractResourceLimit:
        raise StrictVariableLogicResourceLimit() from None
    except ContractError:
        raise StrictVariableLogicInvalid() from None
    except Exception:
        raise StrictVariableLogicInvalid() from None


def _validate_previous_state(
    previous_exists: object,
    previous_present: object,
    previous_value: object,
) -> None:
    if type(previous_exists) is not bool or type(previous_present) is not bool:
        raise StrictVariableLogicInvalid()
    if not previous_exists:
        if previous_present or previous_value is not MISSING:
            raise StrictVariableLogicInvalid()
        return
    if previous_present:
        if previous_value is MISSING:
            raise StrictVariableLogicInvalid()
        return
    if previous_value is not MISSING:
        raise StrictVariableLogicInvalid()


def _validate_payload(operation: str, arguments: dict[str, object]) -> None:
    payload: dict[str, object] = {"operation": operation}
    for key, value in arguments.items():
        if value is MISSING:
            raise StrictVariableLogicInvalid()
        payload[key] = value
    _snapshot_value(payload)


def _snapshot_value(value: object) -> str:
    try:
        _contracts._check_json_tree(
            value,
            max_depth=MAX_VALUE_DEPTH,
            max_object_properties=MAX_OBJECT_PROPERTIES,
            max_array_length=MAX_ARRAY_LENGTH,
        )
        return _contracts._serialize_json(value, max_bytes=MAX_OUTPUT_BYTES)
    except ContractResourceLimit:
        raise
    except ContractError:
        raise
    except Exception:
        raise StrictVariableLogicInvalid() from None


def _validate_value(declaration: VariableDeclaration, value: object) -> None:
    _snapshot_value(value)
    schema = declaration.value_schema
    if type(value) is float and _schema_requires_strict_integer(schema):
        raise StrictVariableLogicInvalid()
    if not _contracts._validator_accepts(schema, value):
        raise StrictVariableLogicInvalid()


def _schema_requires_strict_integer(schema: dict[str, Any]) -> bool:
    definitions = schema.get("$defs", {})
    if type(definitions) is not dict:
        return False
    pending: list[dict[str, Any]] = [schema]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in visited:
            continue
        visited.add(identity)
        schema_type = current.get("type")
        if schema_type == "integer":
            return True
        if type(schema_type) is list and "integer" in schema_type and "number" not in schema_type:
            return True
        reference = current.get("$ref")
        if reference is not None:
            name = _contracts._ref_name(reference, definitions)
            target = definitions.get(name)
            if type(target) is dict:
                pending.append(target)
    return False


def _pointer_segments(pointer: object) -> list[str]:
    if type(pointer) is not str or not pointer or not pointer.startswith("/"):
        raise StrictVariableLogicInvalid()
    segments = pointer[1:].split("/")
    decoded: list[str] = []
    for segment in segments:
        output: list[str] = []
        index = 0
        while index < len(segment):
            character = segment[index]
            if character != "~":
                output.append(character)
                index += 1
                continue
            if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
                raise StrictVariableLogicInvalid()
            output.append("~" if segment[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(output))
    return decoded


def _object_mutation(
    value: object,
    operation: str,
    pointer: object,
    replacement: object,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise StrictVariableLogicInvalid()
    segments = _pointer_segments(pointer)
    result = json.loads(_snapshot_value(value))
    if type(result) is not dict:
        raise StrictVariableLogicInvalid()
    parent: dict[str, Any] = result
    for segment in segments[:-1]:
        child = parent.get(segment, MISSING)
        if type(child) is not dict:
            raise StrictVariableLogicInvalid()
        parent = child
    final = segments[-1]
    if operation == "object_set":
        if replacement is MISSING:
            raise StrictVariableLogicInvalid()
        parent[final] = json.loads(_snapshot_value(replacement))
    else:
        if final not in parent:
            raise StrictVariableLogicInvalid()
        del parent[final]
    return result


def _build_candidate(
    declaration: VariableDeclaration,
    *,
    state_exists: bool,
    present: bool,
    value: object = MISSING,
) -> VariableCandidate:
    if not present:
        if value is not MISSING:
            raise StrictVariableLogicInvalid()
        digest = sha256_json({"present": False})
        return VariableCandidate(
            declaration_id=declaration.declaration_id,
            name=declaration.name,
            owner_scope=declaration.owner_scope,
            state_exists=state_exists,
            present=False,
            _value_json=None,
            value_digest=digest,
        )
    if value is MISSING:
        raise StrictVariableLogicInvalid()
    value_json = _snapshot_value(value)
    isolated = json.loads(value_json)
    _validate_value(declaration, isolated)
    digest = sha256_json({"present": True, "value": isolated})
    return VariableCandidate(
        declaration_id=declaration.declaration_id,
        name=declaration.name,
        owner_scope=declaration.owner_scope,
        state_exists=state_exists,
        present=True,
        _value_json=value_json,
        value_digest=digest,
    )


__all__ = [
    "MISSING",
    "StrictVariableLogicConflict",
    "StrictVariableLogicError",
    "StrictVariableLogicInvalid",
    "StrictVariableLogicResourceLimit",
    "VariableCandidate",
    "VariableDeclaration",
    "calculate_variable_candidate",
    "resolve_variable_declaration",
    "resolve_variable_declarations",
]
