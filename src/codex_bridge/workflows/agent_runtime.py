"""I4-B Agent target and ledger binding helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..paths import AllowedPathPolicy
from .agent_contracts import (
    AgentContractError,
    ProviderRegistry,
    TargetContext,
    codex_agent_i4b_registry,
    validate_agent_wire,
)
from .agent_ledger import (
    AgentInvocationLedger,
    AgentLedgerInspection,
    AgentLedgerIntegrityError,
)
from .codex_agent_provider import CodexAgentProvider
from .contracts import canonical_json
from .strict_variable_runtime import checkpoint_identity_from_manifest
from .task_tree import StrictTaskTreeKind, TaskTreeClassificationError, classify_task_tree

_CANONICAL_LOOPBACK_ENDPOINT = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})\Z", re.ASCII)


class AgentRuntimeBindingError(RuntimeError):
    """The pinned Agent target or its run-local ledger is inconsistent."""


def canonical_agent_target_context(port: int, workspace_root: str) -> TargetContext:
    if type(port) is not int or not 1 <= port <= 65535 or type(workspace_root) is not str:
        raise AgentRuntimeBindingError("strict_execution_target_unavailable")
    return TargetContext(
        "codex",
        "default",
        1,
        endpoint=f"http://127.0.0.1:{port}",
        workspace=workspace_root,
    )


def build_codex_agent_runtime(
    *, port: int, allowed_roots: tuple[str, ...], workspace_root: str
) -> tuple[CodexAgentProvider, ProviderRegistry, TargetContext]:
    try:
        provider = CodexAgentProvider(
            port=port,
            allowed_roots=allowed_roots,
            workspace_root=workspace_root,
        )
        context = provider.capabilities("default").target_context
        if context != canonical_agent_target_context(port, workspace_root):
            raise AgentRuntimeBindingError("strict_execution_target_unavailable")
        registry = ProviderRegistry({"codex": provider}, {"codex": "default"})
        return provider, registry, context
    except AgentRuntimeBindingError:
        raise
    except Exception:
        raise AgentRuntimeBindingError("strict_execution_target_unavailable") from None


def validate_pinned_agent_definition(
    definition: object,
    registry: ProviderRegistry | None = None,
) -> None:
    try:
        classification = classify_task_tree(definition, strict_contract=True)
        if classification.kind is not StrictTaskTreeKind.STRICT_AGENT:
            raise AgentRuntimeBindingError("strict_execution_mode_invalid")
        runtime_registry = registry or codex_agent_i4b_registry()
        for call in classification.agent_calls:
            validate_agent_wire(call.task_spec.get("with"), runtime_registry)
    except (AgentContractError, TaskTreeClassificationError):
        raise AgentRuntimeBindingError("strict_operation_invalid") from None


def validate_agent_stored_target(
    stored_context: object,
    *,
    manifest: Any,
    expected_port: int | None = None,
) -> TargetContext:
    try:
        context = TargetContext.from_dict(stored_context)
        if (
            context.provider != "codex"
            or context.target != "default"
            or context.contract_version != 1
            or type(context.endpoint) is not str
            or type(context.workspace) is not str
            or context.workspace != manifest.workspace_root
        ):
            raise ValueError
        match = _CANONICAL_LOOPBACK_ENDPOINT.fullmatch(context.endpoint)
        if match is None:
            raise ValueError
        endpoint_port = int(match.group(1))
        if not 1 <= endpoint_port <= 65535 or str(endpoint_port) != match.group(1):
            raise ValueError
        if expected_port is not None:
            if type(expected_port) is not int or not 1 <= expected_port <= 65535:
                raise ValueError
            if endpoint_port != expected_port:
                raise ValueError
        workspace = Path(context.workspace)
        if not workspace.is_absolute() or ".." in workspace.parts:
            raise ValueError
        if str(workspace.resolve(strict=False)) != context.workspace:
            raise ValueError
        return context
    except Exception:
        raise AgentRuntimeBindingError("strict_execution_target_unavailable") from None


def inspect_agent_ledger_binding(
    run_directory: Path,
    manifest: Any,
    *,
    expected_port: int | None = None,
) -> tuple[AgentLedgerInspection, TargetContext]:
    try:
        inspection = AgentInvocationLedger.inspect_stored_binding(run_directory)
        expected_identity = checkpoint_identity_from_manifest(manifest)
        if canonical_json(inspection.binding_identity) != canonical_json(expected_identity):
            raise AgentLedgerIntegrityError("Agent binding does not match the strict snapshot")
        context = validate_agent_stored_target(
            inspection.target_context,
            manifest=manifest,
            expected_port=expected_port,
        )
        return inspection, context
    except AgentRuntimeBindingError:
        raise
    except Exception:
        raise AgentLedgerIntegrityError("Agent ledger binding is invalid") from None


def open_agent_ledger_for_manifest(
    run_directory: Path,
    manifest: Any,
    *,
    expected_port: int | None = None,
) -> AgentInvocationLedger:
    inspection, context = inspect_agent_ledger_binding(
        run_directory,
        manifest,
        expected_port=expected_port,
    )
    try:
        return AgentInvocationLedger.open_existing(
            run_directory,
            inspection.binding_identity,
            context,
        )
    except Exception:
        raise AgentLedgerIntegrityError("Agent ledger binding is invalid") from None


def canonical_workspace(allowed_roots: tuple[str, ...], workspace_root: str) -> str:
    try:
        return AllowedPathPolicy(allowed_roots).validate_cwd(workspace_root)
    except Exception:
        raise AgentRuntimeBindingError("strict_execution_target_unavailable") from None


__all__ = [
    "AgentRuntimeBindingError",
    "build_codex_agent_runtime",
    "canonical_agent_target_context",
    "canonical_workspace",
    "inspect_agent_ledger_binding",
    "open_agent_ledger_for_manifest",
    "validate_agent_stored_target",
    "validate_pinned_agent_definition",
]
