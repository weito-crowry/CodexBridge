"""Durable I3 Agent invoke executor for isolated DBOS workflows."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable
from typing import Any, cast

from dbos import DBOS

from .agent_contracts import (
    AgentContractError,
    AgentError,
    AgentRequest,
    AgentResult,
    DispatchReceipt,
    Observation,
    PreparedProviderRequest,
    ProviderProtocol,
    TargetContext,
    result_from_observation,
)
from .agent_ledger import (
    AgentInvocationLedger,
    AgentInvocationRecord,
    AgentLedgerConflictError,
    AgentLedgerIntegrityError,
    AgentOwnedHandle,
)
from .contracts import canonical_json, sha256_json

_STEP_DOMAIN = "codexbridge.workflow.agent.i3.dbos-checkpoint.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RESULT_TERMINAL = frozenset(
    {"SUCCEEDED", "FAILED", "TIMED_OUT", "UNKNOWN", "BLOCKED", "INTERRUPTED"}
)
_EXPECTED_REMOTE_STATE = {
    "RUNNING": frozenset({"in_progress"}),
    "COMPLETED": frozenset({"completed"}),
    "FAILED": frozenset({"failed"}),
    "BLOCKED": frozenset({"needs_approval", "needs_input"}),
    "TIMED_OUT": frozenset({"timed_out"}),
    "INTERRUPTED": frozenset({"interrupted"}),
}
_CODEX_REMOTE_STATES = frozenset(
    {
        "in_progress",
        "needs_approval",
        "needs_input",
        "completed",
        "failed",
        "interrupted",
        "timed_out",
    }
)
_CODEX_TERMINAL_REMOTE_STATES = frozenset({"completed", "failed", "interrupted", "timed_out"})


class AgentInvocationExecutor:
    """Run one invoke/wait=true call using ledger claims and named DBOS steps."""

    def __init__(
        self,
        ledger: AgentInvocationLedger,
        provider: ProviderProtocol,
        *,
        step_runner: Callable[..., Any] | None = None,
        clock_ms: Callable[[], int] | None = None,
        durable_sleep: Callable[[float], None] | None = None,
        poll_interval_seconds: float = 5.0,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if type(ledger) is not AgentInvocationLedger:
            raise AgentLedgerIntegrityError("Agent invocation ledger is invalid")
        if not isinstance(provider, ProviderProtocol):
            raise AgentContractError("unsupported_provider")
        if (
            type(poll_interval_seconds) not in {int, float}
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise AgentContractError("invalid_request")
        self._ledger = ledger
        self._provider = provider
        self._step_runner = step_runner
        self._clock_ms = clock_ms or _utc_epoch_ms
        self._durable_sleep = durable_sleep
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._fault_injector = fault_injector

    def execute(
        self,
        request: AgentRequest | Callable[[], AgentRequest],
        *,
        node_pointer: str,
        iteration_vector: object,
        definition_sha256: str,
        task_input: Any,
        variables: Any,
    ) -> AgentResult:
        """Execute a logical node invocation, recovering from its run-local ledger."""
        if type(definition_sha256) is not str or _SHA256.fullmatch(definition_sha256) is None:
            raise AgentContractError("invalid_request")
        binding = self._ledger.read_binding()
        binding_identity = binding["binding_identity"]
        target_context = TargetContext.from_dict(binding["target_context"])
        capabilities = self._provider.capabilities(target_context.target)
        if (
            capabilities.target_context != target_context
            or "invoke" not in capabilities.operations
            or "observe" not in capabilities.operations
        ):
            raise AgentContractError("unsupported_target")

        execution_digests = {
            "definition_sha256": definition_sha256,
            "task_input_sha256": sha256_json(task_input),
            "variables_sha256": sha256_json(variables),
        }
        binding_sha256 = sha256_json(binding_identity)
        invocation_id = self._ledger.invocation_id_for(node_pointer, iteration_vector)
        record = self._ledger.get_invocation_for_node(node_pointer, iteration_vector)

        if record is None:
            if isinstance(request, AgentRequest):
                normalized_request = request
            elif callable(request):
                normalized_request = request()
            else:
                raise AgentContractError("invalid_request")
            prepared = self._prepare_request(normalized_request, target_context)
            created_ms = self._clock_ms()
            timeout_seconds = prepared.timeout_seconds
            deadline_ms = None if timeout_seconds is None else created_ms + timeout_seconds * 1000
        else:
            if isinstance(request, AgentRequest):
                supplied = self._prepare_request(request, target_context)
                if supplied.to_json() != record.request_json:
                    raise AgentLedgerConflictError("logical Agent invocation changed its request")
            prepared = record.request
            created_ms = record.created_ms
            deadline_ms = record.deadline_ms

        request_sha256 = _sha256(prepared.to_json())
        expected_prepare = self._prepare_metadata(
            invocation_id,
            node_pointer,
            iteration_vector,
            request_sha256,
            binding_sha256,
            execution_digests,
            created_ms=created_ms,
            deadline_ms=deadline_ms,
        )

        def prepare_step() -> dict[str, Any]:
            current = self._ledger.get_invocation_for_node(node_pointer, iteration_vector)
            if current is None:
                current = self._ledger.prepare_invoke(
                    node_pointer,
                    iteration_vector,
                    prepared,
                    created_ms=created_ms,
                )
            elif (
                current.invocation_id != invocation_id
                or current.request_json != prepared.to_json()
                or current.request_sha256 != request_sha256
            ):
                raise AgentLedgerConflictError("logical Agent invocation changed its request")
            self._inject_fault("after_prepare_before_checkpoint")
            return self._prepare_metadata(
                current.invocation_id,
                current.node_pointer,
                current.iteration_vector,
                current.request_sha256,
                binding_sha256,
                execution_digests,
                created_ms=current.created_ms,
                deadline_ms=current.deadline_ms,
            )

        prepare_checkpoint = self._run_step(_step_name("prepare", invocation_id), prepare_step)
        _assert_checkpoint(prepare_checkpoint, expected_prepare)
        record = self._ledger.get_invocation_for_node(node_pointer, iteration_vector)
        if record is None:
            raise AgentLedgerIntegrityError("Agent prepare checkpoint has no ledger row")
        if (
            record.invocation_id != invocation_id
            or record.request_json != prepared.to_json()
            or record.request_sha256 != request_sha256
        ):
            raise AgentLedgerIntegrityError("Agent prepare checkpoint disagrees with ledger")
        _assert_checkpoint(
            prepare_checkpoint,
            self._prepare_metadata(
                record.invocation_id,
                record.node_pointer,
                record.iteration_vector,
                record.request_sha256,
                binding_sha256,
                execution_digests,
                created_ms=record.created_ms,
                deadline_ms=record.deadline_ms,
            ),
        )

        def dispatch_step() -> dict[str, Any]:
            current = self._ledger.get_invocation(invocation_id)
            if current.result_json is not None:
                return self._dispatch_metadata(current)
            if current.dispatch_state in {"ACCEPTED", "REJECTED"}:
                if current.dispatch_state == "REJECTED":
                    self._save_rejected_result(current)
                    current = self._ledger.get_invocation(invocation_id)
                return self._dispatch_metadata(current)
            if current.dispatch_state == "MAYBE_SENT":
                self._save_dispatch_uncertain(current)
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))
            if current.dispatch_state != "PREPARED":
                raise AgentLedgerIntegrityError("Agent dispatch state is invalid")

            if self._deadline_reached(current):
                self._save_timeout(current, None)
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))

            preflight = getattr(self._provider, "preflight", None)
            if callable(preflight):
                try:
                    preflight(current.request)
                except Exception as error:
                    code = (
                        error.code if isinstance(error, AgentContractError) else "preflight_failed"
                    )
                    if code not in {
                        "unauthorized",
                        "preflight_failed",
                        "invalid_request",
                        "unsupported_operation",
                        "unsupported_option",
                        "unsupported_target",
                        "request_too_large",
                    }:
                        code = "preflight_failed"
                    self._save_preflight_failure(current, code)
                    return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))

            if self._deadline_reached(current):
                self._save_timeout(current, None)
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))
            self._inject_fault("before_claim")
            if not self._ledger.claim_dispatch(invocation_id, request_sha256):
                current = self._ledger.get_invocation(invocation_id)
                if current.dispatch_state == "MAYBE_SENT":
                    self._save_dispatch_uncertain(current)
                    current = self._ledger.get_invocation(invocation_id)
                elif current.dispatch_state == "REJECTED":
                    self._save_rejected_result(current)
                    current = self._ledger.get_invocation(invocation_id)
                elif current.dispatch_state == "PREPARED" and current.result_json is None:
                    raise AgentLedgerIntegrityError("Agent dispatch claim was not acquired")
                return self._dispatch_metadata(current)

            self._inject_fault("after_claim_before_post")
            try:
                receipt = self._provider.dispatch(
                    current.request, cast(str, current.client_request_id)
                )
            except Exception:
                self._save_dispatch_uncertain(self._ledger.get_invocation(invocation_id))
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))

            if type(receipt) is not DispatchReceipt:
                self._save_dispatch_uncertain(self._ledger.get_invocation(invocation_id))
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))
            self._inject_fault("after_post_before_receipt")
            if receipt.disposition == "UNCERTAIN":
                self._save_dispatch_uncertain(self._ledger.get_invocation(invocation_id))
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))
            try:
                current = self._ledger.save_dispatch_receipt(invocation_id, receipt)
            except AgentLedgerIntegrityError:
                current = self._ledger.get_invocation(invocation_id)
                if current.dispatch_state != "MAYBE_SENT" or current.result_json is not None:
                    raise
                self._save_dispatch_uncertain(current)
                return self._dispatch_metadata(self._ledger.get_invocation(invocation_id))
            if receipt.disposition == "REJECTED":
                self._save_rejected_result(current)
                current = self._ledger.get_invocation(invocation_id)
            self._inject_fault("after_receipt_before_dispatch_checkpoint")
            return self._dispatch_metadata(current)

        dispatch_checkpoint = self._run_step(_step_name("dispatch", invocation_id), dispatch_step)
        current = self._ledger.get_invocation(invocation_id)
        _assert_checkpoint(dispatch_checkpoint, self._dispatch_metadata(current))
        if current.result_json is not None and current.dispatch_state != "ACCEPTED":
            return self._result_from_record(current)
        if current.dispatch_state != "ACCEPTED":
            raise AgentLedgerIntegrityError("Agent dispatch checkpoint has no confirmed receipt")

        request_model = _request_from_prepared(current.request)
        context = TargetContext.from_dict(current.request.target_identity)
        poll_index = 0
        while True:
            current = self._ledger.get_invocation(invocation_id)
            if current.dispatch_state != "ACCEPTED":
                raise AgentLedgerIntegrityError("accepted Agent dispatch state changed")

            def poll_step(poll_index: int = poll_index) -> dict[str, Any]:
                latest = self._ledger.get_invocation(invocation_id)
                latest_owned = self._ledger.get_owned_handle(invocation_id)
                if latest.result_json is not None:
                    return self._poll_metadata(latest, latest_owned, poll_index)
                if self._deadline_reached(latest):
                    self._save_timeout(latest, latest_owned)
                    latest = self._ledger.get_invocation(invocation_id)
                    latest_owned = self._ledger.get_owned_handle(invocation_id)
                    return self._poll_metadata(latest, latest_owned, poll_index)

                try:
                    observation = self._provider.observe(latest_owned.handle, context)
                except Exception:
                    observation = _unknown_observation(latest_owned.handle)
                observed_ms = self._clock_ms()
                if (
                    type(observation) is not Observation
                    or observation.handle != latest_owned.handle
                    or not _observation_semantics_match(observation)
                ):
                    observation = _unknown_observation(latest_owned.handle)

                if observation.remote_state is not None:
                    terminal = observation.remote_state in _CODEX_TERMINAL_REMOTE_STATES
                    self._ledger.update_remote_state(
                        invocation_id,
                        latest_owned.handle,
                        context,
                        remote_state=observation.remote_state,
                        observed_ms=observed_ms,
                        remote_terminal=terminal,
                    )
                    latest_owned = self._ledger.get_owned_handle(invocation_id)

                if latest.deadline_ms is not None and observed_ms >= latest.deadline_ms:
                    self._save_timeout(latest, latest_owned)
                elif observation.state != "RUNNING":
                    result = result_from_observation(invocation_id, request_model, observation)
                    if result.status not in _RESULT_TERMINAL:
                        result = _unknown_result(invocation_id, request_model, latest_owned.handle)
                    saved = self._ledger.save_result(invocation_id, result)
                    self._inject_fault("after_result_before_poll_checkpoint")
                    latest = saved
                else:
                    latest = self._ledger.get_invocation(invocation_id)

                latest = self._ledger.get_invocation(invocation_id)
                latest_owned = self._ledger.get_owned_handle(invocation_id)
                return self._poll_metadata(latest, latest_owned, poll_index, observed_ms)

            checkpoint = self._run_step(_step_name("poll", invocation_id, poll_index), poll_step)
            latest = self._ledger.get_invocation(invocation_id)
            latest_owned = self._ledger.get_owned_handle(invocation_id)
            _validate_poll_checkpoint(
                checkpoint,
                invocation_id=invocation_id,
                request_sha256=request_sha256,
                poll_index=poll_index,
                owned=latest_owned,
                record=latest,
            )
            if checkpoint["result_status"] != "RUNNING":
                if latest.result_json is None:
                    raise AgentLedgerIntegrityError(
                        "terminal Agent checkpoint has no ledger result"
                    )
                result = self._result_from_record(latest)
                if result.status != checkpoint["result_status"]:
                    raise AgentLedgerIntegrityError("Agent result disagrees with its checkpoint")
                return result
            self._sleep()
            poll_index += 1

    def _prepare_request(
        self,
        request: AgentRequest,
        target_context: TargetContext,
    ) -> PreparedProviderRequest:
        if type(request) is not AgentRequest:
            raise AgentContractError("invalid_request")
        if request.operation != "invoke" or request.wait is not True:
            raise AgentContractError("unsupported_operation")
        try:
            prepared = self._provider.prepare(request, target_context)
        except AgentContractError:
            raise
        except Exception:
            raise AgentContractError("invalid_request") from None
        if (
            type(prepared) is not PreparedProviderRequest
            or prepared.operation != "invoke"
            or prepared.wait is not True
            or prepared.provider != target_context.provider
            or prepared.target != target_context.target
            or prepared.target_identity != target_context.to_dict()
        ):
            raise AgentContractError("unsupported_operation")
        return PreparedProviderRequest.from_json(prepared.to_json())

    def _prepare_metadata(
        self,
        invocation_id: str,
        node_pointer: str,
        iteration_vector: object,
        request_sha256: str,
        binding_sha256: str,
        execution_digests: dict[str, str],
        *,
        created_ms: int | None = None,
        deadline_ms: int | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": "agent_prepare",
            "invocation_id": invocation_id,
            "node_pointer": node_pointer,
            "iteration_vector": list(cast(list[int], iteration_vector)),
            "request_sha256": request_sha256,
            "binding_sha256": binding_sha256,
            "execution_digests": dict(execution_digests),
            "created_ms": created_ms,
            "deadline_ms": deadline_ms,
        }

    def _dispatch_metadata(self, record: AgentInvocationRecord) -> dict[str, Any]:
        return {
            "kind": "agent_dispatch",
            "invocation_id": record.invocation_id,
            "request_sha256": record.request_sha256,
            "dispatch_state": record.dispatch_state,
            "receipt_sha256": record.dispatch_receipt_sha256,
        }

    def _poll_metadata(
        self,
        record: AgentInvocationRecord,
        owned: AgentOwnedHandle,
        poll_index: int,
        observed_ms: int | None = None,
    ) -> dict[str, Any]:
        result = None if record.result_json is None else AgentResult.from_json(record.result_json)
        return {
            "kind": "agent_poll",
            "invocation_id": record.invocation_id,
            "request_sha256": record.request_sha256,
            "poll_index": poll_index,
            "handle_sha256": owned.handle_sha256,
            "observed_ms": observed_ms if observed_ms is not None else owned.observed_ms,
            "remote_state": owned.remote_state,
            "result_status": "RUNNING" if result is None else result.status,
            "result_sha256": record.result_sha256,
        }

    def _save_rejected_result(self, record: AgentInvocationRecord) -> None:
        if record.dispatch_state != "REJECTED" or record.dispatch_receipt_json is None:
            raise AgentLedgerIntegrityError("rejected Agent dispatch has no receipt")
        receipt = DispatchReceipt.from_json(record.dispatch_receipt_json)
        if receipt.disposition != "REJECTED":
            raise AgentLedgerIntegrityError("rejected Agent dispatch receipt is invalid")
        result = AgentResult(
            invocation_id=record.invocation_id,
            operation=record.operation,
            provider=record.request.provider,
            target=record.request.target,
            status="FAILED",
            error=receipt.error,
        )
        self._ledger.save_result(record.invocation_id, result)

    def _save_dispatch_uncertain(self, record: AgentInvocationRecord) -> None:
        if record.dispatch_state != "MAYBE_SENT":
            raise AgentLedgerIntegrityError("uncertain Agent dispatch has no ambiguous claim")
        result = AgentResult(
            invocation_id=record.invocation_id,
            operation=record.operation,
            provider=record.request.provider,
            target=record.request.target,
            status="UNKNOWN",
            error=AgentError("dispatch_uncertain"),
        )
        self._ledger.save_result(record.invocation_id, result)

    def _save_preflight_failure(self, record: AgentInvocationRecord, code: str) -> None:
        if record.dispatch_state != "PREPARED":
            raise AgentLedgerIntegrityError("preflight failure followed an Agent dispatch claim")
        result = AgentResult(
            invocation_id=record.invocation_id,
            operation=record.operation,
            provider=record.request.provider,
            target=record.request.target,
            status="FAILED",
            error=AgentError(code),
        )
        self._ledger.save_result(record.invocation_id, result)

    def _save_timeout(
        self,
        record: AgentInvocationRecord,
        owned: AgentOwnedHandle | None,
    ) -> None:
        handle = None if owned is None else owned.handle
        remote_state = None if owned is None else owned.remote_state
        result = AgentResult(
            invocation_id=record.invocation_id,
            operation=record.operation,
            provider=record.request.provider,
            target=record.request.target,
            status="TIMED_OUT",
            handle=handle,
            remote_state=remote_state,
            error=AgentError("timeout"),
        )
        self._ledger.save_result(record.invocation_id, result)

    def _result_from_record(self, record: AgentInvocationRecord) -> AgentResult:
        if record.result_json is None or record.result_sha256 is None:
            raise AgentLedgerIntegrityError("Agent result is missing")
        result = AgentResult.from_json(record.result_json)
        if _sha256(result.to_json()) != record.result_sha256:
            raise AgentLedgerIntegrityError("Agent result digest does not match")
        return result

    def _deadline_reached(self, record: AgentInvocationRecord) -> bool:
        return record.deadline_ms is not None and self._clock_ms() >= record.deadline_ms

    def _run_step(self, name: str, function: Callable[[], Any]) -> Any:
        if self._step_runner is not None:
            return self._step_runner(name, function)
        return DBOS.step(name=name, retries_allowed=False)(function)()

    def _sleep(self) -> None:
        if self._durable_sleep is not None:
            self._durable_sleep(self._poll_interval_seconds)
        else:
            DBOS.sleep(self._poll_interval_seconds)

    def _inject_fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)


def _request_from_prepared(prepared: PreparedProviderRequest) -> AgentRequest:
    context = TargetContext.from_dict(prepared.target_identity)
    return AgentRequest(
        operation=prepared.operation,
        provider=prepared.provider,
        target=prepared.target,
        target_context=context,
        session=prepared.session,
        handle=prepared.handle,
        options=prepared.options,
        prompt=prepared.prompt,
        response=prepared.response,
        wait=prepared.wait,
        timeout_seconds=prepared.timeout_seconds,
    )


def _unknown_observation(handle: dict[str, Any]) -> Observation:
    return Observation(
        "UNKNOWN",
        handle=handle,
        error=AgentError("observation_unavailable"),
    )


def _unknown_result(
    invocation_id: str,
    request: AgentRequest,
    handle: dict[str, Any],
) -> AgentResult:
    return AgentResult(
        invocation_id=invocation_id,
        operation=request.operation,
        provider=request.provider,
        target=request.target,
        status="UNKNOWN",
        handle=handle,
        error=AgentError("observation_unavailable"),
    )


def _observation_semantics_match(observation: Observation) -> bool:
    expected = _EXPECTED_REMOTE_STATE.get(observation.state)
    if expected is None:
        return observation.remote_state is None or observation.remote_state in _CODEX_REMOTE_STATES
    return observation.remote_state in expected


def _assert_checkpoint(actual: Any, expected: dict[str, Any]) -> None:
    if type(actual) is not dict:
        raise AgentLedgerIntegrityError("Agent DBOS checkpoint is invalid")
    try:
        matches = canonical_json(actual) == canonical_json(expected)
    except Exception:
        matches = False
    if not matches:
        raise AgentLedgerIntegrityError("Agent DBOS checkpoint disagrees with its identity")


def _validate_poll_checkpoint(
    checkpoint: Any,
    *,
    invocation_id: str,
    request_sha256: str,
    poll_index: int,
    owned: AgentOwnedHandle,
    record: AgentInvocationRecord,
) -> None:
    expected_keys = {
        "kind",
        "invocation_id",
        "request_sha256",
        "poll_index",
        "handle_sha256",
        "observed_ms",
        "remote_state",
        "result_status",
        "result_sha256",
    }
    if type(checkpoint) is not dict or set(checkpoint) != expected_keys:
        raise AgentLedgerIntegrityError("Agent poll checkpoint is invalid")
    if (
        checkpoint["kind"] != "agent_poll"
        or checkpoint["invocation_id"] != invocation_id
        or checkpoint["request_sha256"] != request_sha256
        or checkpoint["poll_index"] != poll_index
        or checkpoint["handle_sha256"] != owned.handle_sha256
        or (
            checkpoint["observed_ms"] is not None
            and (
                type(checkpoint["observed_ms"]) is not int
                or not 0 <= checkpoint["observed_ms"] <= 9_223_372_036_854_775_807
            )
        )
        or (checkpoint["remote_state"] is not None and type(checkpoint["remote_state"]) is not str)
    ):
        raise AgentLedgerIntegrityError("Agent poll checkpoint disagrees with its identity")
    status = checkpoint["result_status"]
    result_sha256 = checkpoint["result_sha256"]
    if status == "RUNNING":
        if result_sha256 is not None:
            raise AgentLedgerIntegrityError("running Agent checkpoint contains a result digest")
        return
    if status not in _RESULT_TERMINAL or record.result_json is None:
        raise AgentLedgerIntegrityError("terminal Agent poll checkpoint has no ledger result")
    result = AgentResult.from_json(record.result_json)
    if status != result.status or result_sha256 != record.result_sha256:
        raise AgentLedgerIntegrityError("Agent poll checkpoint disagrees with its result")


def _step_name(stage: str, invocation_id: str, poll_index: int | None = None) -> str:
    identity = {"invocation_id": invocation_id, "stage": stage, "poll_index": poll_index}
    digest = hashlib.sha256(f"{_STEP_DOMAIN}\0{canonical_json(identity)}".encode()).hexdigest()
    return f"agent_i3_{stage}_{digest}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_epoch_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = ["AgentInvocationExecutor"]
