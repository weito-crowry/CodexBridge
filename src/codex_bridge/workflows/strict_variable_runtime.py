"""Narrow direct-runtime adapter for verified strict variable snapshots.

This adapter is for isolated I7-B evaluation only. It does not start a worker,
create a reservation, or provide durable replay across Interpreter restarts.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    CompiledContract,
    ContractResourceLimit,
    canonical_json,
    parse_strict_json,
    sha256_json,
)
from .strict_manifest import (
    StrictWorkflowManifestV2,
    _run_binding_input,
    parse_strict_manifest_v2,
)
from .strict_reader_gate import _read_manifest_bytes, verify_strict_reserved_readonly
from .strict_variable_logic import (
    StrictVariableLogicError,
    StrictVariableLogicResourceLimit,
    _pointer_segments,
    resolve_variable_declaration,
)
from .strict_variable_mutations import (
    MutationContext,
    StrictWorkflowVariableMutationStore,
    StrictWorkflowVariableNeedsReconciliation,
    StrictWorkflowVariableResourceLimit,
)
from .strict_variables import (
    StrictWorkflowVariableError,
    StrictWorkflowVariableInvalidMetadata,
    StrictWorkflowVariableStorageUnavailable,
)

_OPERATIONS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "initialize": (frozenset({"value"}), frozenset()),
    "assign": (frozenset({"value"}), frozenset()),
    "increment": (frozenset(), frozenset({"by"})),
    "decrement": (frozenset(), frozenset({"by"})),
    "append": (frozenset({"value"}), frozenset()),
    "object_set": (frozenset({"path", "value"}), frozenset()),
    "object_delete": (frozenset({"path"}), frozenset()),
    "variable_delete": (frozenset(), frozenset()),
}


class StrictVariableRuntimeError(RuntimeError):
    """Stable value-free error returned by the I7-B adapter."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("Strict workflow variable operation is unavailable.")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<{self.reason_code}>)"


def checkpoint_identity_from_manifest(
    manifest: StrictWorkflowManifestV2,
) -> dict[str, object]:
    """Build the shared DBOS and Agent ledger binding from a verified manifest."""
    if type(manifest) is not StrictWorkflowManifestV2:
        raise StrictVariableRuntimeError("strict_variable_run_invalid")
    return {
        "run_id": manifest.run_id,
        "workflow_id": manifest.workflow_id,
        "definition_id": manifest.definition_id,
        "definition_revision": manifest.definition_revision,
        "request_digest": manifest.request_digest,
        "source_sha256": manifest.source_sha256,
        "contract_sha256": manifest.contract_sha256,
        "binding_digest": manifest.binding_digest,
        "normalized_input_sha256": manifest.normalized_input_sha256,
    }


@dataclass(frozen=True, slots=True, repr=False)
class PreparedStrictVariableMutation:
    """Detached apply arguments captured before the I4 ledger transaction."""

    context: MutationContext = field(repr=False)
    declaration_id: str = field(repr=False)
    operation: str = field(repr=False)
    expected_before_version: int = field(repr=False)
    expected_before_digest: str | None = field(repr=False)
    evaluated_arguments_sha256: str = field(repr=False)
    _operation_args_json: str = field(repr=False)

    def __repr__(self) -> str:
        return "PreparedStrictVariableMutation(<redacted>)"


class StrictWorkflowVariableRuntimeAdapter:
    """Apply OWS root-scope variable calls through the existing I4 ledger."""

    def __init__(
        self,
        run_directory: Path,
        compiled_contract: CompiledContract,
        store: StrictWorkflowVariableMutationStore,
        *,
        projection_provider: Callable[[str], Mapping[str, Any]] | None = None,
        normalized_input: dict[str, Any] | None = None,
        verified_manifest: StrictWorkflowManifestV2 | None = None,
        pinned_definition: dict[str, Any] | None = None,
    ) -> None:
        self._run_directory = Path(run_directory)
        self.compiled_contract = compiled_contract
        self._store = store
        self._projection_provider = projection_provider
        self._normalized_input = copy.deepcopy(normalized_input)
        self._verified_manifest = verified_manifest
        self.pinned_definition = copy.deepcopy(pinned_definition) if pinned_definition else None
        self._durable_projection_provider: Callable[..., Mapping[str, Any]] | None = None
        self._durable_prepare_provider: Callable[..., PreparedStrictVariableMutation] | None = None
        self._durable_apply_provider: (
            Callable[[PreparedStrictVariableMutation], dict[str, Any]] | None
        ) = None
        self._durable_task_provider: Callable[..., Any] | None = None
        self._workflow_names = frozenset(
            variable["declaration_id"]
            for variable in compiled_contract.variables
            if variable.get("owner_scope") == "workflow"
            and type(variable.get("declaration_id")) is str
        )

    @classmethod
    def from_reserved_run(
        cls,
        state_root: Path,
        run_id: str,
        *,
        expected_workflow_id: str,
        expected_request_digest: str,
        allowed_roots: tuple[str, ...],
        projection_provider: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> StrictWorkflowVariableRuntimeAdapter:
        """Verify a reserved run through I5 before attaching the I4 store."""
        try:
            manifest_bytes = _read_manifest_bytes(Path(state_root), run_id)
            return cls._from_reserved_manifest_bytes(
                Path(state_root),
                run_id,
                manifest_bytes,
                expected_workflow_id=expected_workflow_id,
                expected_request_digest=expected_request_digest,
                allowed_roots=allowed_roots,
                projection_provider=projection_provider,
            )
        except StrictVariableRuntimeError:
            raise
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_run_invalid") from None

    @classmethod
    def from_manifest_claim(
        cls,
        state_root: Path,
        run_id: str,
        *,
        allowed_roots: tuple[str, ...],
    ) -> StrictWorkflowVariableRuntimeAdapter:
        """Use only identity claims read from the reserved manifest itself."""
        try:
            manifest_bytes = _read_manifest_bytes(Path(state_root), run_id)
            claim = parse_strict_manifest_v2(manifest_bytes)
            return cls._from_reserved_manifest_bytes(
                Path(state_root),
                run_id,
                manifest_bytes,
                expected_workflow_id=claim.workflow_id,
                expected_request_digest=claim.request_digest,
                allowed_roots=allowed_roots,
            )
        except StrictVariableRuntimeError:
            raise
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_run_invalid") from None

    @classmethod
    def _from_reserved_manifest_bytes(
        cls,
        state_root: Path,
        run_id: str,
        manifest_bytes: bytes,
        *,
        expected_workflow_id: str,
        expected_request_digest: str,
        allowed_roots: tuple[str, ...],
        projection_provider: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> StrictWorkflowVariableRuntimeAdapter:
        try:
            run_directory = Path(state_root) / run_id
            verified = verify_strict_reserved_readonly(
                state_root,
                run_id,
                manifest_bytes,
                expected_workflow_id=expected_workflow_id,
                expected_request_digest=expected_request_digest,
                allowed_roots=allowed_roots,
            )
            if verified is None or verified.reservation_verified is not True:
                raise StrictVariableRuntimeError("strict_variable_run_invalid")
            definition = verified._definition
            if definition is None or definition.contract is None:
                raise StrictVariableRuntimeError("strict_variable_run_invalid")
            normalized_input = verified._normalized_input
            if type(normalized_input) is not dict:
                raise StrictVariableRuntimeError("strict_variable_run_invalid")
            store = StrictWorkflowVariableMutationStore(
                run_directory,
                _run_binding_input(verified.manifest),
                definition.contract,
            )
            return cls(
                run_directory,
                definition.contract,
                store,
                projection_provider=projection_provider,
                normalized_input=normalized_input,
                verified_manifest=verified.manifest,
                pinned_definition=definition.definition,
            )
        except StrictVariableRuntimeError:
            raise
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_run_invalid") from None

    @property
    def checkpoint_identity(self) -> dict[str, object]:
        manifest = self._verified_manifest
        if manifest is None:
            raise StrictVariableRuntimeError("strict_variable_run_invalid")
        return checkpoint_identity_from_manifest(manifest)

    @property
    def max_live_codex_jobs(self) -> int:
        manifest = self._verified_manifest
        if manifest is None:
            raise StrictVariableRuntimeError("strict_variable_run_invalid")
        return manifest.max_live_codex_jobs

    @property
    def max_tasks(self) -> int:
        manifest = self._verified_manifest
        if manifest is None:
            raise StrictVariableRuntimeError("strict_variable_run_invalid")
        return manifest.max_tasks

    def normalized_input(self) -> dict[str, Any]:
        if self._normalized_input is None:
            raise StrictVariableRuntimeError("strict_variable_run_invalid")
        return copy.deepcopy(self._normalized_input)

    def configure_durable_hooks(
        self,
        *,
        projection_provider: Callable[..., Mapping[str, Any]],
        prepare_provider: Callable[..., PreparedStrictVariableMutation],
        apply_provider: Callable[[PreparedStrictVariableMutation], dict[str, Any]],
        task_provider: Callable[..., Any],
    ) -> None:
        """Install internal DBOS providers after a reserved adapter is verified."""
        if any(
            hook is not None
            for hook in (
                self._durable_projection_provider,
                self._durable_prepare_provider,
                self._durable_apply_provider,
                self._durable_task_provider,
            )
        ):
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
        self._durable_projection_provider = projection_provider
        self._durable_prepare_provider = prepare_provider
        self._durable_apply_provider = apply_provider
        self._durable_task_provider = task_provider

    @property
    def has_durable_task_provider(self) -> bool:
        return self._durable_task_provider is not None

    def run_durable_task(self, *arguments: Any) -> Any:
        provider = self._durable_task_provider
        if provider is None:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
        return provider(*arguments)

    def projection_for_phase(
        self,
        phase: str,
        node_id: str,
        iteration_vector: tuple[int, ...] | list[int] = (),
    ) -> dict[str, dict[str, Any]]:
        """Return one detached root projection for a stable evaluation phase."""
        try:
            if type(phase) is not str or not phase or type(node_id) is not str or not node_id:
                raise StrictVariableRuntimeError("strict_variable_invalid")
            if type(iteration_vector) not in (tuple, list) or any(
                type(index) is not int or index < 0 for index in iteration_vector
            ):
                raise StrictVariableRuntimeError("strict_variable_invalid")
            phase_id = f"{node_id}#{phase}:{canonical_json(list(iteration_vector))}"
            if self._durable_projection_provider is not None:
                values = self._durable_projection_provider(
                    phase, node_id, tuple(iteration_vector), phase_id
                )
            elif self._projection_provider is not None:
                values = self._projection_provider(phase_id)
            else:
                values = self._store.read_root_projection()
            if not isinstance(values, Mapping) or set(values) - self._workflow_names:
                raise StrictVariableRuntimeError("strict_variable_storage_unavailable")
            detached = parse_strict_json(canonical_json(dict(values)))
            return {"workflow": detached}
        except StrictVariableRuntimeError:
            raise
        except StrictWorkflowVariableResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except ContractResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictWorkflowVariableStorageUnavailable:
            raise StrictVariableRuntimeError("strict_variable_storage_unavailable") from None
        except StrictWorkflowVariableError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictVariableLogicResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictVariableLogicError:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_storage_unavailable") from None

    def prepare_mutation(
        self,
        arguments: Mapping[str, Any],
        *,
        node_id: str,
        iteration_vector: tuple[int, ...] | list[int] = (),
    ) -> PreparedStrictVariableMutation:
        """Snapshot evaluated OWS arguments and the applicable before-CAS."""
        try:
            if not isinstance(arguments, Mapping):
                raise StrictVariableRuntimeError("strict_variable_invalid")
            operation = arguments.get("operation")
            if type(operation) is not str or operation not in _OPERATIONS:
                raise StrictVariableRuntimeError("strict_variable_invalid")
            declaration_id = arguments.get("declaration_id")
            if type(declaration_id) is not str or arguments.get("scope") != "workflow":
                raise StrictVariableRuntimeError("strict_variable_invalid")
            declaration = resolve_variable_declaration(
                self.compiled_contract, declaration_id, "workflow"
            )
            required, optional = _OPERATIONS[operation]
            expected = {"operation", "declaration_id", "scope"} | required | optional
            if set(arguments) - expected or not (
                {"operation", "declaration_id", "scope"} | required
            ) <= set(arguments):
                raise StrictVariableRuntimeError("strict_variable_invalid")

            operation_args = {
                name: value
                for name, value in arguments.items()
                if name not in {"operation", "declaration_id", "scope"}
            }
            if operation in {"object_set", "object_delete"}:
                pointer = operation_args.pop("path")
                _pointer_segments(pointer)
                operation_args["pointer"] = pointer
            operation_args_json = canonical_json(operation_args)
            evaluated_arguments_sha256 = sha256_json(dict(arguments))
            context = MutationContext(
                node_path=node_id,
                iteration_vector=tuple(iteration_vector),
                frame_id="workflow-root",
                mutation_ordinal=0,
                subflow_call_context=None,
            )
            execution_key = self._store.execution_key_for(context)
            current = self._store.read_current("workflow-root", declaration_id)
            historical = self._store.get_mutation(execution_key)
            if historical is None:
                before_version = current.version
                before_digest = current.value_digest
            else:
                before_version = historical.before_version
                before_digest = historical.before_digest
            if declaration.declaration_id not in self._workflow_names:
                raise StrictVariableRuntimeError("strict_variable_invalid")
            return PreparedStrictVariableMutation(
                context=context,
                declaration_id=declaration_id,
                operation=operation,
                expected_before_version=before_version,
                expected_before_digest=before_digest,
                evaluated_arguments_sha256=evaluated_arguments_sha256,
                _operation_args_json=operation_args_json,
            )
        except StrictVariableRuntimeError:
            raise
        except StrictWorkflowVariableNeedsReconciliation:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictWorkflowVariableResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except ContractResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictWorkflowVariableStorageUnavailable:
            raise StrictVariableRuntimeError("strict_variable_storage_unavailable") from None
        except StrictWorkflowVariableInvalidMetadata:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except StrictWorkflowVariableError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictVariableLogicResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictVariableLogicError:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None

    def apply_prepared(self, prepared: PreparedStrictVariableMutation) -> dict[str, Any]:
        """Commit/replay the captured payload and return the call's safe output."""
        try:
            if not isinstance(prepared, PreparedStrictVariableMutation):
                raise StrictVariableRuntimeError("strict_variable_invalid")
            arguments = parse_strict_json(prepared._operation_args_json)
            # The root frame must still be ACTIVE when a prepared historical key
            # is replayed; I4 keeps generic ledger replay behavior unchanged.
            self._store.read_current("workflow-root", prepared.declaration_id)
            result = self._store.apply(
                prepared.context,
                prepared.declaration_id,
                prepared.operation,
                expected_before_version=prepared.expected_before_version,
                expected_before_digest=prepared.expected_before_digest,
                **arguments,
            )
            if result.after_present:
                return {"present": True, "value": copy.deepcopy(result.after_value)}
            return {"present": False}
        except StrictVariableRuntimeError:
            raise
        except StrictWorkflowVariableNeedsReconciliation:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictWorkflowVariableResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except ContractResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictWorkflowVariableStorageUnavailable:
            raise StrictVariableRuntimeError("strict_variable_storage_unavailable") from None
        except StrictWorkflowVariableInvalidMetadata:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except StrictWorkflowVariableError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictVariableLogicResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictVariableLogicError:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None

    def verify_historical_prepared_readonly(
        self, prepared: PreparedStrictVariableMutation
    ) -> dict[str, Any]:
        """Require and verify the I4 historical record without applying it."""
        try:
            if not isinstance(prepared, PreparedStrictVariableMutation):
                raise StrictVariableRuntimeError("strict_variable_invalid")
            arguments = parse_strict_json(prepared._operation_args_json)
            result = self._store.verify_existing_prepared(
                prepared.context,
                prepared.declaration_id,
                prepared.operation,
                expected_before_version=prepared.expected_before_version,
                expected_before_digest=prepared.expected_before_digest,
                **arguments,
            )
            if result.after_present:
                return {"present": True, "value": copy.deepcopy(result.after_value)}
            return {"present": False}
        except StrictVariableRuntimeError:
            raise
        except StrictWorkflowVariableNeedsReconciliation:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictWorkflowVariableStorageUnavailable:
            raise StrictVariableRuntimeError("strict_variable_storage_unavailable") from None
        except StrictWorkflowVariableInvalidMetadata:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except StrictWorkflowVariableError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictVariableLogicResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictVariableLogicError:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None

    def serialize_prepared_mutation(
        self, prepared: PreparedStrictVariableMutation
    ) -> dict[str, Any]:
        """Serialize only the immutable apply arguments required by a durable step."""
        try:
            if not isinstance(prepared, PreparedStrictVariableMutation):
                raise StrictVariableRuntimeError("strict_variable_invalid")
            context = prepared.context
            execution_key = self._store.execution_key_for(context)
            return {
                "format_version": 1,
                "binding_identity": self.checkpoint_identity,
                "execution_key": execution_key,
                "node_id": context.node_path,
                "iteration_vector": list(context.iteration_vector),
                "frame_id": context.frame_id,
                "mutation_ordinal": context.mutation_ordinal,
                "subflow_call_context": None,
                "declaration_id": prepared.declaration_id,
                "operation": prepared.operation,
                "expected_before_version": prepared.expected_before_version,
                "expected_before_digest": prepared.expected_before_digest,
                "evaluated_arguments_sha256": prepared.evaluated_arguments_sha256,
                "operation_args": parse_strict_json(prepared._operation_args_json),
            }
        except StrictVariableRuntimeError:
            raise
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_invalid") from None

    def restore_prepared_mutation(
        self,
        payload: Mapping[str, Any],
        *,
        node_id: str,
        iteration_vector: tuple[int, ...] | list[int],
        evaluated_arguments_sha256: str | None = None,
    ) -> PreparedStrictVariableMutation:
        """Validate a DBOS checkpoint before any I4 mutation is attempted."""
        try:
            fields = {
                "format_version",
                "binding_identity",
                "execution_key",
                "node_id",
                "iteration_vector",
                "frame_id",
                "mutation_ordinal",
                "subflow_call_context",
                "declaration_id",
                "operation",
                "expected_before_version",
                "expected_before_digest",
                "evaluated_arguments_sha256",
                "operation_args",
            }
            if type(payload) is not dict or set(payload) != fields:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            if (
                type(payload.get("format_version")) is not int
                or payload.get("format_version") != 1
                or payload.get("binding_identity") != self.checkpoint_identity
                or payload.get("node_id") != node_id
                or payload.get("iteration_vector") != list(iteration_vector)
                or payload.get("frame_id") != "workflow-root"
                or payload.get("mutation_ordinal") != 0
                or payload.get("subflow_call_context") is not None
                or (
                    evaluated_arguments_sha256 is not None
                    and payload.get("evaluated_arguments_sha256") != evaluated_arguments_sha256
                )
            ):
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            operation = payload.get("operation")
            declaration_id = payload.get("declaration_id")
            if type(operation) is not str or operation not in _OPERATIONS:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            if type(declaration_id) is not str:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            resolve_variable_declaration(self.compiled_contract, declaration_id, "workflow")
            required, optional = _OPERATIONS[operation]
            operation_args = payload.get("operation_args")
            if type(operation_args) is not dict:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            mandatory_internal = set(required)
            allowed_internal = set(required | optional)
            if operation in {"object_set", "object_delete"}:
                mandatory_internal.discard("path")
                allowed_internal.discard("path")
                allowed_internal.add("pointer")
                mandatory_internal.add("pointer")
            if set(operation_args) - allowed_internal or not mandatory_internal <= set(
                operation_args
            ):
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            if operation in {"object_set", "object_delete"}:
                pointer = operation_args.get("pointer")
                _pointer_segments(pointer)
            version = payload.get("expected_before_version")
            digest = payload.get("expected_before_digest")
            if type(version) is not int or version < 0:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            if digest is not None and (
                type(digest) is not str
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            args_digest = payload.get("evaluated_arguments_sha256")
            if (
                type(args_digest) is not str
                or len(args_digest) != 64
                or any(c not in "0123456789abcdef" for c in args_digest)
            ):
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            reconstructed_arguments = {
                "operation": operation,
                "declaration_id": declaration_id,
                "scope": "workflow",
                **operation_args,
            }
            if operation in {"object_set", "object_delete"}:
                reconstructed_arguments["path"] = reconstructed_arguments.pop("pointer")
            if sha256_json(reconstructed_arguments) != args_digest:
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            context = MutationContext(
                node_path=node_id,
                iteration_vector=tuple(iteration_vector),
                frame_id="workflow-root",
                mutation_ordinal=0,
                subflow_call_context=None,
            )
            if payload.get("execution_key") != self._store.execution_key_for(context):
                raise StrictVariableRuntimeError("strict_variable_needs_reconciliation")
            return PreparedStrictVariableMutation(
                context=context,
                declaration_id=declaration_id,
                operation=operation,
                expected_before_version=version,
                expected_before_digest=digest,
                evaluated_arguments_sha256=args_digest,
                _operation_args_json=canonical_json(operation_args),
            )
        except StrictVariableRuntimeError:
            raise
        except StrictWorkflowVariableError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except StrictVariableLogicResourceLimit:
            raise StrictVariableRuntimeError("strict_variable_resource_limit") from None
        except StrictVariableLogicError:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None
        except Exception:
            raise StrictVariableRuntimeError("strict_variable_needs_reconciliation") from None

    def execute_mutation(
        self,
        arguments: Mapping[str, Any],
        *,
        node_id: str,
        iteration_vector: tuple[int, ...] | list[int] = (),
    ) -> dict[str, Any]:
        """Prepare once, then apply through the I4 transaction."""
        vector = tuple(iteration_vector)
        prepared = (
            self.prepare_mutation(arguments, node_id=node_id, iteration_vector=vector)
            if self._durable_prepare_provider is None
            else self._durable_prepare_provider(arguments, node_id, vector)
        )
        return (
            self.apply_prepared(prepared)
            if self._durable_apply_provider is None
            else self._durable_apply_provider(prepared)
        )


__all__ = [
    "PreparedStrictVariableMutation",
    "StrictVariableRuntimeError",
    "StrictWorkflowVariableRuntimeAdapter",
    "checkpoint_identity_from_manifest",
]
