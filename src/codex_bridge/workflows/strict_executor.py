"""Isolated DBOS executor for already-reserved strict-v2 workflow runs.

The service and worker entry points deliberately do not call this executor yet.
I7-C2 owns that lifecycle connection.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from dbos import DBOS

from .agent_contracts import (
    AgentContractError,
    AgentError,
    AgentRequest,
    AgentResult,
    PreparedProviderRequest,
    evaluate_agent_request,
    validate_agent_wire,
)
from .agent_executor import AgentInvocationExecutor
from .agent_ledger import AgentLedgerConflictError, AgentLedgerIntegrityError
from .agent_runtime import (
    build_codex_agent_runtime,
    open_agent_ledger_for_manifest,
    validate_pinned_agent_definition,
)
from .contracts import (
    ContractOutputInvalid,
    ContractResourceLimit,
    canonical_json,
    parse_strict_json,
    sha256_json,
    validate_output,
)
from .interpreter import (
    Interpreter,
    OwsError,
    _escape_node_pointer_segment,
    _task_action,
    compute_codex_job_request,
    compute_task_action,
    evaluate_jq_single_result,
    validate_workflow,
)
from .strict_variable_runtime import (
    PreparedStrictVariableMutation,
    StrictVariableRuntimeError,
    StrictWorkflowVariableRuntimeAdapter,
)
from .task_tree import (
    StrictTaskTreeKind,
    TaskTreeClassificationError,
    classify_task_tree,
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
    expected_version: int = 1,
) -> dict[str, Any]:
    try:
        if type(value) is not dict or set(value) != expected_fields:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        if (
            type(value.get("checkpoint_version")) is not int
            or value["checkpoint_version"] != expected_version
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
            "checkpoint_version": 2,
            "kind": "pure_task_action",
            "binding_identity": expected_identity,
            "input_sha256": sha256_json(input_data),
            "variables_sha256": sha256_json(variables),
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


def _prepare_agent_request_checkpoint_body(
    state_root: str,
    run_id: str,
    allowed_roots: list[str],
    expected_identity: dict[str, Any],
    port: int,
    node_id: str,
    iteration_vector: list[int],
    task_name: str,
    task_spec: dict[str, Any],
    input_data: Any,
    variables: dict[str, Any],
) -> dict[str, Any]:
    try:
        adapter = _verified_adapter(state_root, run_id, allowed_roots, expected_identity)
        manifest = adapter._verified_manifest
        definition = adapter.pinned_definition
        if (
            manifest is None
            or definition is None
            or type(node_id) is not str
            or type(iteration_vector) is not list
            or any(type(index) is not int or index < 0 for index in iteration_vector)
            or type(task_name) is not str
            or type(task_spec) is not dict
            or type(variables) is not dict
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        pinned = _strict_task_map(definition, execution_mode="authorized_agent").get(node_id)
        if (
            pinned is None
            or pinned[0] != task_name
            or pinned[1] != "call"
            or canonical_json(pinned[2]) != canonical_json(task_spec)
        ):
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        provider, registry, context = build_codex_agent_runtime(
            port=port,
            allowed_roots=tuple(allowed_roots),
            workspace_root=manifest.workspace_root,
        )
        validate_pinned_agent_definition(definition, registry)
        run_directory = Path(state_root) / run_id
        ledger = open_agent_ledger_for_manifest(
            run_directory,
            manifest,
            expected_port=port,
        )
        invocation_id = ledger.invocation_id_for(node_id, iteration_vector)
        wire = task_spec.get("with")
        try:
            call_definition = validate_agent_wire(wire, registry)
            request = evaluate_agent_request(
                call_definition,
                lambda expression: evaluate_jq_single_result(
                    expression,
                    input_data,
                    variables,
                    strict_contract=adapter.compiled_contract,
                ),
            )
            provider.prepare(request, context)
            return {
                "checkpoint_version": 1,
                "kind": "agent_request_preparation",
                "binding_identity": expected_identity,
                "node_id": node_id,
                "iteration_vector": iteration_vector,
                "task_name": task_name,
                "input_sha256": sha256_json(input_data),
                "variables_sha256": sha256_json(variables),
                "invocation_id": invocation_id,
                "request_json": request.to_json(),
                "error_code": None,
            }
        except (AgentContractError, OwsError) as error:
            code = getattr(error, "code", None)
            if type(code) is not str:
                code = "preflight_failed"
            return {
                "checkpoint_version": 1,
                "kind": "agent_request_preparation",
                "binding_identity": expected_identity,
                "node_id": node_id,
                "iteration_vector": iteration_vector,
                "task_name": task_name,
                "input_sha256": sha256_json(input_data),
                "variables_sha256": sha256_json(variables),
                "invocation_id": invocation_id,
                "request_json": None,
                "error_code": code,
            }
    except StrictWorkflowExecutionError:
        raise
    except (AgentLedgerConflictError, AgentLedgerIntegrityError):
        raise StrictWorkflowExecutionError("strict_execution_uncertain") from None
    except Exception as error:
        raise _safe_execution_error(error) from None


@cache
def _prepare_agent_request_step(step_identity: str) -> Any:
    return DBOS.step(name=_step_name("agent-request-prepare", step_identity))(
        _prepare_agent_request_checkpoint_body
    )


def _strict_task_map(
    definition: dict[str, Any], *, execution_mode: str = "isolated"
) -> dict[str, tuple[str, str, dict[str, Any]]]:
    try:
        if execution_mode not in {"isolated", "authorized_codex", "authorized_agent"}:
            raise StrictWorkflowExecutionError("strict_execution_mode_invalid")
        classification = classify_task_tree(definition, strict_contract=True)
        if execution_mode == "authorized_agent" and (
            classification.kind is not StrictTaskTreeKind.STRICT_AGENT
        ):
            raise StrictWorkflowExecutionError("strict_execution_mode_invalid")
        if execution_mode == "isolated" and classification.kind is StrictTaskTreeKind.STRICT_AGENT:
            raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
        task_map = _pinned_task_map(definition)
        allowed_call_targets = {"codexbridge-variable"}
        if execution_mode == "authorized_codex":
            allowed_call_targets.add("codex-job")
        elif execution_mode == "authorized_agent":
            allowed_call_targets.add("codexbridge-agent")
        if any(
            operation == "call" and task_spec.get("call") not in allowed_call_targets
            for _name, operation, task_spec in task_map.values()
        ):
            raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
        return task_map
    except StrictWorkflowExecutionError:
        raise
    except TaskTreeClassificationError:
        raise StrictWorkflowExecutionError("strict_execution_invalid") from None
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
    execution_mode: str = "isolated",
) -> dict[str, Any]:
    try:
        definition = adapter.pinned_definition
        if definition is None:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        if execution_mode not in {"isolated", "authorized_codex", "authorized_agent"}:
            raise StrictWorkflowExecutionError("strict_execution_mode_invalid")
        _strict_task_map(definition, execution_mode=execution_mode)
        validate_workflow(
            definition,
            codex_job_live=execution_mode == "authorized_codex",
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
                historical = adapter.verify_historical_prepared_readonly(prepared)
                if canonical_json(historical) != canonical_json(checkpoint["result"]):
                    raise StrictWorkflowExecutionError("strict_execution_uncertain")
                return historical
            except Exception as error:
                raise _safe_execution_error(error) from None

        def task_provider(
            task_identity: str,
            _task_name: str,
            _operation: str,
            _task_spec: dict[str, Any],
            input_data: Any,
            variables: dict[str, Any],
            node_id: str,
            iteration_vector: tuple[int, ...],
        ) -> Any:
            try:
                if _operation == "call" and _task_spec.get("call") == "codexbridge-agent":
                    if execution_mode != "authorized_agent":
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    if os.environ.get("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED") != "1":
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    port_text = os.environ.get("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", "")
                    if not port_text.isascii() or not port_text.isdigit():
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    port = int(port_text)
                    if not 1 <= port <= 65535:
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    manifest = adapter._verified_manifest
                    pinned_definition = adapter.pinned_definition
                    if manifest is None or pinned_definition is None:
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    provider, registry, context = build_codex_agent_runtime(
                        port=port,
                        allowed_roots=tuple(roots),
                        workspace_root=manifest.workspace_root,
                    )
                    validate_pinned_agent_definition(pinned_definition, registry)
                    ledger = open_agent_ledger_for_manifest(
                        Path(state_root) / run_id,
                        manifest,
                        expected_port=port,
                    )
                    step_identity = f"{node_id}:{canonical_json(list(iteration_vector))}"
                    checkpoint = _prepare_agent_request_step(step_identity)(
                        state_root,
                        run_id,
                        roots,
                        identity,
                        port,
                        node_id,
                        list(iteration_vector),
                        _task_name,
                        _task_spec,
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
                            "input_sha256",
                            "variables_sha256",
                            "invocation_id",
                            "request_json",
                            "error_code",
                        },
                        expected_kind="agent_request_preparation",
                        expected_identity=identity,
                    )
                    if (
                        checkpoint["node_id"] != node_id
                        or checkpoint["iteration_vector"] != list(iteration_vector)
                        or checkpoint["task_name"] != _task_name
                        or checkpoint["input_sha256"] != sha256_json(input_data)
                        or checkpoint["variables_sha256"] != sha256_json(variables)
                        or checkpoint["invocation_id"]
                        != ledger.invocation_id_for(node_id, list(iteration_vector))
                    ):
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    if checkpoint["error_code"] is not None:
                        if (
                            type(checkpoint["error_code"]) is not str
                            or checkpoint["request_json"] is not None
                            or ledger.get_invocation_for_node(node_id, list(iteration_vector))
                            is not None
                        ):
                            raise StrictWorkflowExecutionError("strict_execution_uncertain")
                        wire = _task_spec.get("with")
                        operation = (
                            wire.get("operation", "invoke") if type(wire) is dict else "invoke"
                        )
                        if operation not in {"invoke", "observe", "wait", "interrupt"}:
                            raise StrictWorkflowExecutionError("strict_execution_uncertain")
                        return AgentResult(
                            invocation_id=checkpoint["invocation_id"],
                            operation=operation,
                            provider="codex",
                            target="default",
                            status="FAILED",
                            error=AgentError(checkpoint["error_code"]),
                        ).to_dict()
                    if type(checkpoint["request_json"]) is not str:
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    request = AgentRequest.from_json(checkpoint["request_json"])
                    if request.target_context != context:
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    existing = ledger.get_invocation_for_node(node_id, list(iteration_vector))
                    if (
                        existing is not None
                        and PreparedProviderRequest(request, context).to_json()
                        != existing.request_json
                    ):
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    result = AgentInvocationExecutor(ledger, provider).execute(
                        lambda: AgentRequest.from_json(checkpoint["request_json"]),
                        node_pointer=node_id,
                        iteration_vector=list(iteration_vector),
                        definition_sha256=manifest.source_sha256,
                        task_input=input_data,
                        variables=variables,
                    )
                    return result.to_dict()
                if _operation == "call" and _task_spec.get("call") == "codex-job":
                    if execution_mode != "authorized_codex":
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    port_text = os.environ.get("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", "")
                    if not port_text.isascii() or not port_text.isdigit():
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    port = int(port_text)
                    if not 1 <= port <= 65535:
                        raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
                    manifest = adapter._verified_manifest
                    if manifest is None:
                        raise StrictWorkflowExecutionError("strict_execution_uncertain")
                    codex_job_request = compute_codex_job_request(
                        _task_spec,
                        input_data,
                        variables,
                        max_live_codex_jobs=adapter.max_live_codex_jobs,
                        strict_contract=adapter.compiled_contract,
                    )
                    from .worker import _run_durable_codex_job

                    return _run_durable_codex_job(
                        identity=task_identity,
                        task_name=_task_name,
                        operation=_operation,
                        task_input=input_data,
                        variables=variables,
                        request=codex_job_request,
                        definition_sha256=manifest.source_sha256,
                        trace_path=str(Path(state_root) / run_id / "task-events.jsonl"),
                        control={
                            "max_live_codex_jobs": adapter.max_live_codex_jobs,
                            "codex_bridge_port": port,
                            "codex_job_max_polls": 600,
                            "codex_job_poll_interval_seconds": 0.2,
                            "workspace_root": manifest.workspace_root,
                            "allowed_roots": roots,
                        },
                    )
                if _operation == "call" and _task_spec.get("call") != "codexbridge-variable":
                    raise StrictWorkflowExecutionError("strict_execution_target_unavailable")
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
                        "input_sha256",
                        "variables_sha256",
                        "node_id",
                        "iteration_vector",
                        "task_name",
                        "operation",
                        "result",
                    },
                    expected_kind="pure_task_action",
                    expected_identity=identity,
                    expected_version=2,
                )
                pinned_task = _strict_task_map(definition, execution_mode=execution_mode).get(
                    node_id
                )
                if (
                    checkpoint["checkpoint_version"] != 2
                    or checkpoint["input_sha256"] != sha256_json(input_data)
                    or checkpoint["variables_sha256"] != sha256_json(variables)
                    or checkpoint["node_id"] != node_id
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
            codex_job_live=execution_mode == "authorized_codex",
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
    """Run only an existing reserved strict snapshot in the selected private mode."""
    try:
        if execution_mode not in {"isolated", "authorized_codex", "authorized_agent"}:
            raise StrictWorkflowExecutionError("strict_execution_mode_invalid")
        adapter = _verified_adapter(state_root, run_id, allowed_roots)
        definition = adapter.pinned_definition
        if definition is None:
            raise StrictWorkflowExecutionError("strict_execution_uncertain")
        _strict_task_map(definition, execution_mode=execution_mode)
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
            execution_mode=execution_mode,
        )
    except Exception as error:
        raise _safe_execution_error(error) from None


__all__ = ["StrictWorkflowExecutionError", "execute_strict_ows_workflow"]
