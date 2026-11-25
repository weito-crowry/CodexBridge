"""Shared, value-safe contract parsing and projection for definition transports."""

from __future__ import annotations

import json
from typing import Any

from .contracts import CompiledContract, ContractError, compile_contract
from .definitions import WorkflowContractInvalid, WorkflowContractUnavailable

CONTRACT_MISSING = object()


def compile_definition_contract(raw: object) -> CompiledContract:
    """Compile a public contract object without exposing parser details or values."""
    if not isinstance(raw, dict):
        raise WorkflowContractInvalid("workflow contract is invalid") from None
    try:
        return compile_contract(raw)
    except ContractError:
        raise WorkflowContractInvalid("workflow contract is invalid") from None
    except Exception:
        raise WorkflowContractInvalid("workflow contract is invalid") from None


def optional_definition_contract(payload: dict[str, Any]) -> CompiledContract | object:
    """Return the compiled contract or a sentinel that distinguishes omission."""
    if "contract" not in payload:
        return CONTRACT_MISSING
    return compile_definition_contract(payload["contract"])


def definition_contract_object(canonical_json: str) -> dict[str, Any]:
    """Decode a previously verified canonical contract into its public JSON object."""
    try:
        value = json.loads(canonical_json)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    if not isinstance(value, dict):
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    return value
