"""Shared, value-safe contract parsing and projection for definition transports."""

from __future__ import annotations

import json
from typing import Any

from .contracts import CompiledContract, ContractError, compile_contract
from .definitions import (
    DefinitionRevision,
    DefinitionValidation,
    WorkflowContractInvalid,
    WorkflowContractUnavailable,
    WorkflowDefinitionRepository,
)

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


def validate_definition_source(
    repository: WorkflowDefinitionRepository,
    source_yaml: str,
    max_live_codex_jobs: int,
    *,
    contract: object = CONTRACT_MISSING,
) -> DefinitionValidation:
    """Validate source with the verified contract context when one is supplied."""
    if contract is CONTRACT_MISSING:
        return repository.validate(source_yaml, max_live_codex_jobs)
    compiled = compile_definition_contract(contract)
    return repository.validate(
        source_yaml,
        max_live_codex_jobs,
        strict_contract=compiled,
    )


def compiled_contract_for_revision(revision: DefinitionRevision) -> CompiledContract | None:
    """Recompile a repository-verified strict contract for static projection."""
    if revision.contract_version is None:
        return None
    if revision.contract_version != 1 or revision.contract is None:
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    try:
        compiled = compile_contract(revision.contract)
    except Exception:
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    if compiled.contract_version != revision.contract_version or (
        compiled.contract_sha256 != revision.contract_sha256
    ):
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    return compiled


def definition_contract_object(canonical_json: str) -> dict[str, Any]:
    """Decode a previously verified canonical contract into its public JSON object."""
    try:
        value = json.loads(canonical_json)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    if not isinstance(value, dict):
        raise WorkflowContractUnavailable("workflow contract storage is unavailable") from None
    return value
