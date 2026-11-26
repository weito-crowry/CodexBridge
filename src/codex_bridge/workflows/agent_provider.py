"""Provider contracts and deterministic injection-only Agent Fake."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .agent_contracts import (
    AgentContractError,
    AgentRequest,
    DispatchReceipt,
    Observation,
    PreparedProviderRequest,
    ProviderCapabilities,
    ProviderProtocol,
    TargetContext,
)
from .contracts import canonical_json


@dataclass(frozen=True, slots=True)
class ProviderCall:
    operation: str
    client_request_id: str | None = None
    handle_json: str | None = field(default=None, repr=False)


class FakeAgentProvider:
    """Configured in-memory provider for tests; never installed globally."""

    __slots__ = (
        "_capabilities",
        "_dispatch_receipts",
        "_observation_sequence",
        "_interrupt_receipts",
        "_dispatch_index",
        "_observation_index",
        "_interrupt_index",
        "_calls",
    )

    def __init__(
        self,
        capabilities: ProviderCapabilities,
        *,
        dispatch_receipts: Sequence[DispatchReceipt] = (),
        observation_sequence: Sequence[Observation] = (),
        interrupt_receipts: Sequence[DispatchReceipt] = (),
    ) -> None:
        if type(capabilities) is not ProviderCapabilities:
            raise AgentContractError("invalid_request")
        self._capabilities = capabilities
        self._dispatch_receipts = tuple(
            DispatchReceipt.from_json(receipt.to_json())
            for receipt in dispatch_receipts
            if type(receipt) is DispatchReceipt
        )
        self._observation_sequence = tuple(
            Observation.from_json(observation.to_json())
            for observation in observation_sequence
            if type(observation) is Observation
        )
        self._interrupt_receipts = tuple(
            DispatchReceipt.from_json(receipt.to_json())
            for receipt in interrupt_receipts
            if type(receipt) is DispatchReceipt
        )
        if (
            len(self._dispatch_receipts) != len(dispatch_receipts)
            or len(self._observation_sequence) != len(observation_sequence)
            or len(self._interrupt_receipts) != len(interrupt_receipts)
        ):
            raise AgentContractError("invalid_request")
        self._dispatch_index = 0
        self._observation_index = 0
        self._interrupt_index = 0
        self._calls: list[ProviderCall] = []

    @property
    def calls(self) -> tuple[ProviderCall, ...]:
        return tuple(self._calls)

    def capabilities(self, target: str) -> ProviderCapabilities:
        if type(target) is not str or target != self._capabilities.target:
            raise AgentContractError("unsupported_target")
        return self._capabilities

    def prepare(
        self, request: AgentRequest, target_context: TargetContext
    ) -> PreparedProviderRequest:
        self._check_context(target_context)
        if type(request) is not AgentRequest or request.target_context != target_context:
            raise AgentContractError("unsupported_target")
        if request.operation not in self._capabilities.operations:
            raise AgentContractError("unsupported_operation")
        if request.operation == "invoke":
            session = request.session
            if session is None or session.get("mode") not in self._capabilities.session_modes:
                raise AgentContractError("unsupported_operation")
        self._calls.append(ProviderCall("prepare"))
        return PreparedProviderRequest(request, target_context)

    def dispatch(
        self, prepared: PreparedProviderRequest, client_request_id: str
    ) -> DispatchReceipt:
        if type(prepared) is not PreparedProviderRequest or not self._matches_target(
            prepared.provider, prepared.target
        ):
            raise AgentContractError("unsupported_target")
        if prepared.operation != "invoke" or "invoke" not in self._capabilities.operations:
            raise AgentContractError("unsupported_operation")
        if TargetContext.from_dict(prepared.target_identity) != self._capabilities.target_context:
            raise AgentContractError("unsupported_target")
        if type(client_request_id) is not str or not client_request_id:
            raise AgentContractError("invalid_request")
        self._calls.append(ProviderCall("dispatch", client_request_id))
        if self._dispatch_index >= len(self._dispatch_receipts):
            return DispatchReceipt.uncertain()
        receipt = self._dispatch_receipts[self._dispatch_index]
        self._dispatch_index += 1
        return DispatchReceipt.from_json(receipt.to_json())

    def observe(self, handle: object, target_context: TargetContext) -> Observation:
        self._check_context(target_context)
        if "observe" not in self._capabilities.operations:
            raise AgentContractError("unsupported_operation")
        if type(handle) is not dict:
            raise AgentContractError("invalid_request")
        try:
            handle_json = canonical_json(handle)
        except Exception:
            raise AgentContractError("invalid_request") from None
        self._calls.append(ProviderCall("observe", handle_json=handle_json))
        if self._observation_index >= len(self._observation_sequence):
            return Observation(
                "UNKNOWN",
                handle=handle,
                error={
                    "code": "observation_unavailable",
                    "message": "Agent state could not be observed.",
                },
            )
        observation = self._observation_sequence[self._observation_index]
        self._observation_index += 1
        return Observation.from_json(observation.to_json())

    def interrupt(
        self,
        handle: object,
        target_context: TargetContext,
        client_request_id: str,
    ) -> DispatchReceipt:
        self._check_context(target_context)
        if "interrupt" not in self._capabilities.operations:
            raise AgentContractError("unsupported_operation")
        if type(handle) is not dict or type(client_request_id) is not str or not client_request_id:
            raise AgentContractError("invalid_request")
        try:
            handle_json = canonical_json(handle)
        except Exception:
            raise AgentContractError("invalid_request") from None
        self._calls.append(ProviderCall("interrupt", client_request_id, handle_json))
        if self._interrupt_index >= len(self._interrupt_receipts):
            return DispatchReceipt.uncertain()
        receipt = self._interrupt_receipts[self._interrupt_index]
        self._interrupt_index += 1
        return DispatchReceipt.from_json(receipt.to_json())

    def _matches_target(self, provider: str, target: str) -> bool:
        return provider == self._capabilities.provider and target == self._capabilities.target

    def _check_context(self, context: TargetContext) -> None:
        if (
            type(context) is not TargetContext
            or not self._matches_target(context.provider, context.target)
            or context != self._capabilities.target_context
        ):
            raise AgentContractError("unsupported_target")


__all__ = ["FakeAgentProvider", "ProviderCall", "ProviderProtocol"]
