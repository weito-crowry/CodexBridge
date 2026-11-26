"""Isolated DBOS executor for already-reserved strict-v2 workflow runs.

The service and worker entry points deliberately do not call this executor yet.
I7-C2 owns that lifecycle connection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from dbos import DBOS

from .contracts import (
    ContractOutputInvalid,
    ContractResourceLimit,
    canonical_json,
    parse_strict_json,
    validate_output,
)
from .interpreter import (
    Interpreter,
    OwsError,
    _escape_node_pointer_segment,
    _task_action,
    compute_task_action,
    validate_workflow,
)
from .strict_variable_runtime import (
    PreparedStrictVariableMutation,
    StrictVariableRuntimeError,
    StrictWorkflowVariableRuntimeAdapter,
)

_STEP_DOMAIN = "codexbridge.workflow.strict.dbos-checkpoint.v1"


class StrictWorkflowExecutionError(RuntimeError):
    """Small internal error contract for I7-C2 status and reason projection."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("Strict workflow execution did not complete.")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<{self.reason_code}>)"


def _safe_execution_error(error: BaseException) -> StrictWorkflowExecutionError:
    if isinstance(error, StrictWorkflowExecutionError):
        return error
    if isinstance(error, StrictVariableRuntimeError):
        if error.reason_code == "strict_variable_resource_limit":
            return StrictWorkflowExecutionError("resource_limit_exceeded")
        if error.reason_code == "strict_variable_invalid":
            return StrictWorkflowExecutionError("strict_operation_invalid")
        return StrictWorkflowExecutionError("strict_execution_uncertain")
    if isinstance(error, ContractResourceLimit):
        return StrictWorkflowExecutionError("resource_limit_exceeded")
    if isinstance(error, ContractOutputInvalid):
        return StrictWorkflowExecutionError("output_contract_violation")
    if isinstance(error, OwsError):
        return StrictWorkflowExecutionError("strict_execution_invalid")
    return StrictWorkflowExecutionError("strict_execution_uncertain")


def _step_name(kind: str, identity: str) -> str:
    material = f"{_STEP_DOMAIN}\0{kind}\0{identity}"
    return f"ows7c1_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _verified_adapter(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: Mapping[str, Any] | None = None,
) -> StrictWorkflowVariableRuntimeAdapter:
    try:
        if (
            type(state_root) is not str
            or type(run_id) is not str
            or type(allowed_roots) is not list
            or not allowed_roots
            or any(type(root) is not str for root in allowed_roots)
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        adapter = StrictWorkflowVariableRuntimeAdapter.from_manifest_claim(
            Path(state_root),
            run_id,
            allowed_roots=tuple(allowed_roots),
        )
        if expected_identity is not None and dict(expected_identity) != adapter.checkpoint_identity:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        if adapter.pinned_definition is None:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        return adapter
    except StrictWorkflowExecutionError:
        raise
    except Exception as error:
        raise _safe_execution_error(error) from None


def _phase_id(phase: str, node_id: str, iteration_vector: list[int]) -> str:
    return f"{node_id}#{phase}:{canonical_json(iteration_vector)}"


def _validate_envelope(
    value: Any,
    *,
    expected_fields: set[str],
    expected_kind: str,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if type(value) is not dict or set(value) != expected_fields:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        if (
            type(value.get("checkpoint_version")) is not int
            or value["checkpoint_version"] != 1
            or value.get("kind") != expected_kind
            or value.get("binding_identity") != dict(expected_identity)
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        detached = parse_strict_json(canonical_json(value))
        if type(detached) is not dict:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        return detached
    except StrictWorkflowExecutionError:
        raise
    except Exception:
        raise StrictWorkflowExecutionError("strict_execution_uncertain") from None


def _projection_checkpoint_body(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: dict[str, Any],
    phase: str,
    node_id: str,
    iteration_vector: list[int],
    phase_id: str,
) -> dict[str, Any]:
    try:
        adapter = _verified_adapter(state_root, run_id, allowed_roots, expected_identity)
        if (
            type(phase) is not str
            or type(node_id) is not str
            or type(iteration_vector) is not list
            or any(type(index) is not int or index < 0 for index in iteration_vector)
            or phase_id != _phase_id(phase, node_id, iteration_vector)
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        projected = adapter.projection_for_phase(phase, node_id, iteration_vector)
        if type(projected) is not dict or type(projected.get("workflow")) is not dict:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        return {
            "checkpoint_version": 1,
            "kind": "phase_projection",
            "binding_identity": expected_identity,
            "phase_id": phase_id,
            "phase": phase,
            "node_id": node_id,
            "iteration_vector": iteration_vector,
            "projection": projected["workflow"],
        }
    except Exception as error:
        raise _safe_execution_error(error) from None


@cache
def _projection_step(phase_id: str) -> Any:
    return DBOS.step(name=_step_name("phase-projection", phase_id))(_projection_checkpoint_body)


def _prepare_mutation_checkpoint_body(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: dict[str, Any],
    arguments: dict[str, Any],
    node_id: str,
    iteration_vector: list[int],
    evaluated_arguments_sha256: str,
) -> dict[str, Any]:
    try:
        adapter = _verified_adapter(state_root, run_id, allowed_roots, expected_identity)
        if (
            type(arguments) is not dict
            or hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()
            != evaluated_arguments_sha256
            or type(node_id) is not str
            or type(iteration_vector) is not list
            or any(type(index) is not int or index < 0 for index in iteration_vector)
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        prepared = adapter.prepare_mutation(
            arguments,
            node_id=node_id,
            iteration_vector=iteration_vector,
        )
        return {
            "checkpoint_version": 1,
            "kind": "prepared_variable_mutation",
            "binding_identity": expected_identity,
            "node_id": node_id,
            "iteration_vector": iteration_vector,
            "evaluated_arguments_sha256": evaluated_arguments_sha256,
            "prepared": adapter.serialize_prepared_mutation(prepared),
        }
    except Exception as error:
        raise _safe_execution_error(error) from None


@cache
def _prepare_mutation_step(step_identity: str) -> Any:
    return DBOS.step(name=_step_name("variable-prepare", step_identity))(
        _prepare_mutation_checkpoint_body
    )


def _apply_mutation_checkpoint_body(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: dict[str, Any],
    prepared_payload: dict[str, Any],
    node_id: str,
    iteration_vector: list[int],
    execution_key: str,
) -> dict[str, Any]:
    try:
        adapter = _verified_adapter(state_root, run_id, allowed_roots, expected_identity)
        prepared = adapter.restore_prepared_mutation(
            prepared_payload,
            node_id=node_id,
            iteration_vector=iteration_vector,
        )
        if prepared_payload.get("execution_key") != execution_key:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        result = adapter.apply_prepared(prepared)
        return {
            "checkpoint_version": 1,
            "kind": "applied_variable_mutation",
            "binding_identity": expected_identity,
            "node_id": node_id,
            "iteration_vector": iteration_vector,
            "execution_key": execution_key,
            "result": result,
        }
    except Exception as error:
        raise _safe_execution_error(error) from None


@cache
def _apply_mutation_step(execution_key: str) -> Any:
    return DBOS.step(name=_step_name("variable-apply", execution_key))(
        _apply_mutation_checkpoint_body
    )


def _pinned_task_map(definition: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    tasks_by_node: dict[str, tuple[str, str, dict[str, Any]]] = {}

    def walk(tasks: list[dict[str, Any]], scope_pointer: str) -> None:
        for index, task in enumerate(tasks):
            if type(task) is not dict or len(task) != 1:
                raise StrictWorkflowExecutionError("strict_execution_uncertain")
            task_name, task_spec = next(iter(task.items()))
            if type(task_name) is not str or type(task_spec) is not dict:
                raise StrictWorkflowExecutionError("strict_execution_uncertain")
            operation, _action = _task_action(task_name, task_spec)
            node_id = f"{scope_pointer}/{index}/{_escape_node_pointer_segment(task_name)}"
            tasks_by_node[node_id] = (task_name, operation, task_spec)
            if operation == "do":
                walk(task_spec["do"], f"{node_id}/do")
            elif operation == "for":
                walk(task_spec["do"], f"{node_id}/do")

    walk(definition["do"], "/do")
    return tasks_by_node


def _compute_task_checkpoint_body(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: dict[str, Any],
    node_id: str,
    iteration_vector: list[int],
    input_data: Any,
    variables: dict[str, Any],
) -> dict[str, Any]:
    try:
        adapter = _verified_adapter(state_root, run_id, allowed_roots, expected_identity)
        definition = adapter.pinned_definition
        if definition is None or type(variables) is not dict:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        task = _pinned_task_map(definition).get(node_id)
        if task is None:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        task_name, operation, task_spec = task
        if operation == "call":
            raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
        result = compute_task_action(
            operation,
            task_spec,
            input_data,
            variables,
            strict_contract=adapter.compiled_contract,
        )
        return {
            "checkpoint_version": 1,
            "kind": "pure_task_action",
            "binding_identity": expected_identity,
            "node_id": node_id,
            "iteration_vector": iteration_vector,
            "task_name": task_name,
            "operation": operation,
            "result": result,
        }
    except Exception as error:
        raise _safe_execution_error(error) from None


@cache
def _task_action_step(step_identity: str) -> Any:
    return DBOS.step(name=_step_name("pure-task", step_identity))(_compute_task_checkpoint_body)


def _strict_task_map(definition: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    try:
        task_map = _pinned_task_map(definition)
        if any(
            operation == "call" and task_spec.get("call") != "codexbridge-variable"
            for _name, operation, task_spec in task_map.values()
        ):
            raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
        return task_map
    except StrictWorkflowExecutionError:
        raise
    except Exception as error:
        raise _safe_execution_error(error) from None


def _validated_output(contract: Any, result: Any) -> dict[str, Any]:
    try:
        return validate_output(contract, result)
    except Exception as error:
        raise _safe_execution_error(error) from None


def _execute_verified_run(
    adapter: StrictWorkflowVariableRuntimeAdapter,
    *,
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
) -> dict[str, Any]:
    try:
        definition = adapter.pinned_definition
        if definition is None:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        _strict_task_map(definition)
        validate_workflow(
            definition,
            codex_job_live=False,
            max_live_codex_jobs=adapter.max_live_codex_jobs,
            strict_contract=adapter.compiled_contract,
        )
        identity = adapter.checkpoint_identity
        state_root = str(Path(state_root).resolve(strict=True))
        run_id = str(run_id)
        roots = list(allowed_roots)

        def projection_provider(
            phase: str, node_id: str, iteration_vector: tuple[int, ...], phase_id: str
        ) -> Mapping[str, Any]:
            checkpoint = _projection_step(phase_id)(
                state_root,
                run_id,
                roots,
                identity,
                phase,
                node_id,
                list(iteration_vector),
                phase_id,
            )
            checkpoint = _validate_envelope(
                checkpoint,
                expected_fields={
                    "checkpoint_version",
                    "kind",
                    "binding_identity",
                    "phase_id",
                    "phase",
                    "node_id",
                    "iteration_vector",
                    "projection",
                },
                expected_kind="phase_projection",
                expected_identity=identity,
            )
            if (
                checkpoint["phase_id"] != phase_id
                or checkpoint["phase"] != phase
                or checkpoint["node_id"] != node_id
                or checkpoint["iteration_vector"] != list(iteration_vector)
                or type(checkpoint["projection"]) is not dict
            ):
                raise StrictWorkflowExecutionError("strict_execution_uncertain")
            return checkpoint["projection"]

        def prepare_provider(
            arguments: Mapping[str, Any], node_id: str, iteration_vector: tuple[int, ...]
        ) -> PreparedStrictVariableMutation:
            try:
                detached_arguments = parse_strict_json(canonical_json(dict(arguments)))
                arguments_digest = hashlib.sha256(
                    canonical_json(detached_arguments).encode("utf-8")
                ).hexdigest()
                step_identity = f"{node_id}:{canonical_json(list(iteration_vector))}"
                checkpoint = _prepare_mutation_step(step_identity)(
                    state_root,
                    run_id,
                    roots,
                    identity,
                    detached_arguments,
                    node_id,
                    list(iteration_vector),
                    arguments_digest,
                )
                checkpoint = _validate_envelope(
                    checkpoint,
                    expected_fields={
                        "checkpoint_version",
                        "kind",
                        "binding_identity",
                        "node_id",
                        "iteration_vector",
                        "evaluated_arguments_sha256",
                        "prepared",
                    },
                    expected_kind="prepared_variable_mutation",
                    expected_identity=identity,
                )
                if (
                    checkpoint["node_id"] != node_id
                    or checkpoint["iteration_vector"] != list(iteration_vector)
                    or checkpoint["evaluated_arguments_sha256"] != arguments_digest
                    or type(checkpoint["prepared"]) is not dict
                ):
                    raise StrictWorkflowExecutionError("strict_execution_uncertain")
                return adapter.restore_prepared_mutation(
                    checkpoint["prepared"],
                    node_id=node_id,
                    iteration_vector=iteration_vector,
                    evaluated_arguments_sha256=arguments_digest,
                )
            except Exception as error:
                raise _safe_execution_error(error) from None

        def apply_provider(prepared: PreparedStrictVariableMutation) -> dict[str, Any]:
            try:
                payload = adapter.serialize_prepared_mutation(prepared)
                node_id = prepared.context.node_path
                iteration_vector = tuple(prepared.context.iteration_vector)
                execution_key = str(payload["execution_key"])
                checkpoint = _apply_mutation_step(execution_key)(
                    state_root,
                    run_id,
                    roots,
                    identity,
                    payload,
                    node_id,
                    list(iteration_vector),
                    execution_key,
                )
                checkpoint = _validate_envelope(
                    checkpoint,
                    expected_fields={
                        "checkpoint_version",
                        "kind",
                        "binding_identity",
                        "node_id",
                        "iteration_vector",
                        "execution_key",
                        "result",
                    },
                    expected_kind="applied_variable_mutation",
                    expected_identity=identity,
                )
                if (
                    checkpoint["node_id"] != node_id
                    or checkpoint["iteration_vector"] != list(iteration_vector)
                    or checkpoint["execution_key"] != execution_key
                    or type(checkpoint["result"]) is not dict
                ):
                    raise StrictWorkflowExecutionError("strict_execution_uncertain")
                # Re-read the I4 historical execution record even when DBOS returned
                # an existing checkpoint; this validates it without using latest state.
                historical = adapter.apply_prepared(prepared)
                if historical != checkpoint["result"]:
                    raise StrictWorkflowExecutionError("strict_execution_uncertain")
                return historical
            except Exception as error:
                raise _safe_execution_error(error) from None

        def task_provider(
            _identity: str,
            _task_name: str,
            _operation: str,
            _task_spec: dict[str, Any],
            input_data: Any,
            variables: dict[str, Any],
            node_id: str,
            iteration_vector: tuple[int, ...],
        ) -> Any:
            try:
                step_identity = f"{node_id}:{canonical_json(list(iteration_vector))}"
                checkpoint = _task_action_step(step_identity)(
                    state_root,
                    run_id,
                    roots,
                    identity,
                    node_id,
                    list(iteration_vector),
                    input_data,
                    variables,
                )
                checkpoint = _validate_envelope(
                    checkpoint,
                    expected_fields={
                        "checkpoint_version",
                        "kind",
                        "binding_identity",
                        "node_id",
                        "iteration_vector",
                        "task_name",
                        "operation",
                        "result",
                    },
                    expected_kind="pure_task_action",
                    expected_identity=identity,
                )
                pinned_task = _strict_task_map(definition).get(node_id)
                if (
                    checkpoint["node_id"] != node_id
                    or checkpoint["iteration_vector"] != list(iteration_vector)
                    or pinned_task is None
                    or checkpoint["task_name"] != pinned_task[0]
                    or checkpoint["operation"] != pinned_task[1]
                ):
                    raise StrictWorkflowExecutionError("strict_execution_uncertain")
                return checkpoint["result"]
            except Exception as error:
                raise _safe_execution_error(error) from None

        adapter.configure_durable_hooks(
            projection_provider=projection_provider,
            prepare_provider=prepare_provider,
            apply_provider=apply_provider,
            task_provider=task_provider,
        )
        result = Interpreter(
            definition,
            max_tasks=adapter.max_tasks,
            max_live_codex_jobs=adapter.max_live_codex_jobs,
            strict_variable_runtime=adapter,
        ).run(adapter.normalized_input())
        validated = _validated_output(adapter.compiled_contract, result)
        return {
            "format_version": 1,
            "status": "succeeded",
            "reason_code": None,
            "run_id": run_id,
            "workflow_id": identity["workflow_id"],
            "binding": identity,
            "result": validated,
        }
    except Exception as error:
        raise _safe_execution_error(error) from None


@DBOS.workflow()
def execute_strict_ows_workflow(
    run_id: str,
    state_root: str,
    allowed_roots: list[str],
    execution_mode: str,
) -> dict[str, Any]:
    """Run only an existing reserved strict snapshot in isolated DBOS mode."""
    try:
        if execution_mode != "isolated":
            raise StrictWorkflowExecutionError("strict_execution_mode_invalid")
        adapter = _verified_adapter(state_root, run_id, allowed_roots)
        workflow_id = DBOS.workflow_id
        if (
            type(workflow_id) is not str
            or workflow_id != adapter.checkpoint_identity["workflow_id"]
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        return _execute_verified_run(
            adapter,
            state_root=state_root,
            run_id=run_id,
            allowed_roots=allowed_roots,
        )
    except Exception as error:
        raise _safe_execution_error(error) from None


__all__ = ["StrictWorkflowExecutionError", "execute_strict_ows_workflow"]
