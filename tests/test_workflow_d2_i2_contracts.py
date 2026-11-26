from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from codex_bridge.workflows.agent_contracts import (
    AgentContractError,
    AgentRequest,
    AgentResult,
    AgentUsage,
    DispatchReceipt,
    Observation,
    OptionCapability,
    OptionConstraint,
    ProviderCapabilities,
    ProviderRegistry,
    TargetContext,
    codex_agent_capabilities,
    evaluate_agent_request,
    result_from_dispatch_receipt,
    result_from_observation,
    validate_agent_wire,
)
from codex_bridge.workflows.agent_provider import FakeAgentProvider, ProviderProtocol
from codex_bridge.workflows.contracts import (
    MAX_SCHEMA_DEPTH,
    ContractInvalid,
    ContractOutputInvalid,
    ContractResourceLimit,
    validate_json_value_schema,
)


def _caps(provider: str = "fake", target: str = "default") -> ProviderCapabilities:
    return ProviderCapabilities(
        provider=provider,
        target=target,
        operations=("invoke", "observe", "wait", "interrupt"),
        session_modes=("new", "continue"),
        option_rules=(
            OptionCapability("model", ("new", "continue"), ("string",)),
            OptionCapability("reasoning_effort", ("new",), ("string",)),
            OptionCapability("title", ("new",), ("string",)),
            OptionCapability(
                "sandbox_mode",
                ("new",),
                ("string",),
                allowed_values=('"inherit"', '"danger-full-access"'),
            ),
            OptionCapability("confirm_full_access", ("new",), ("boolean",)),
        ),
        option_constraints=(
            OptionConstraint("reasoning_effort", None, "model", None),
            OptionConstraint("sandbox_mode", '"danger-full-access"', "confirm_full_access", "true"),
        ),
        usage_supported=True,
        transport_constraints={"max_request_bytes": 49152},
    )


def _registry(
    *,
    provider: str = "fake",
    target: str = "default",
    capabilities: ProviderCapabilities | None = None,
    observations: tuple[Observation, ...] = (),
    receipts: tuple[DispatchReceipt, ...] = (),
    interrupt_receipts: tuple[DispatchReceipt, ...] = (),
) -> tuple[ProviderRegistry, FakeAgentProvider]:
    provider_capabilities = capabilities or _caps(provider, target)
    fake = FakeAgentProvider(
        provider_capabilities,
        dispatch_receipts=receipts,
        observation_sequence=observations,
        interrupt_receipts=interrupt_receipts,
    )
    return ProviderRegistry({provider: fake}), fake


def _invoke(**values: object) -> dict[str, object]:
    return {
        "provider": "fake",
        "session": {"mode": "new"},
        "prompt": "Summarize {{source}}",
        "bindings": {"source": {"from": "${ .source }", "format": "json"}},
        **values,
    }


def _definition(wire: object, registry: ProviderRegistry | None = None):
    actual_registry = registry or _registry()[0]
    return validate_agent_wire(wire, actual_registry)


def _request(
    *,
    response: dict[str, object] | None = None,
    registry: ProviderRegistry | None = None,
    evaluator=None,
) -> AgentRequest:
    wire = _invoke(response=response or {"format": "text"})
    definition = _definition(wire, registry)
    return evaluate_agent_request(definition, evaluator or (lambda _expression: "source"))


def _result_payload() -> dict[str, object]:
    return {
        "version": 1,
        "invocation_id": "call-1",
        "operation": "observe",
        "provider": "fake",
        "target": "default",
        "status": "RUNNING",
        "handle": {"opaque": [1, None]},
        "remote_state": "inProgress",
        "final_text": None,
        "data": None,
        "usage": {"input_tokens": None, "output_tokens": 7, "total_tokens": None, "source": "fake"},
        "error": None,
    }


def test_invoke_wire_defaults_and_request_round_trip() -> None:
    registry, _fake = _registry()
    definition = validate_agent_wire(_invoke(), registry)
    calls: list[str] = []
    request = evaluate_agent_request(
        definition, lambda expression: calls.append(expression) or "Ada"
    )

    assert request.operation == "invoke"
    assert request.provider == "fake"
    assert request.target == "default"
    assert request.session == {"mode": "new"}
    assert request.handle is None
    assert request.options == {}
    assert request.prompt == "Summarize " + '"Ada"'
    assert request.response == {"format": "text"}
    assert request.wait is True
    assert request.timeout_seconds is None
    assert calls == ["${ .source }"]
    assert AgentRequest.from_json(request.to_json()).to_json() == request.to_json()


@pytest.mark.parametrize(
    ("operation", "wire_tail", "expected_session", "expected_prompt", "expected_timeout"),
    [
        ("observe", {"handle": {"thread_id": "t", "turn_id": "u"}}, None, None, None),
        (
            "wait",
            {
                "handle": {"thread_id": "t", "turn_id": "u"},
                "timeout_seconds": 15,
                "response": {"format": "text"},
            },
            None,
            None,
            15,
        ),
        ("interrupt", {"handle": {"opaque": "h"}}, None, None, None),
    ],
)
def test_non_invoke_operations_keep_inapplicable_fields_null(
    operation: str,
    wire_tail: dict[str, object],
    expected_session: object,
    expected_prompt: object,
    expected_timeout: int | None,
) -> None:
    wire = {"operation": operation, "provider": "fake", **wire_tail}
    request = evaluate_agent_request(_definition(wire), lambda _expression: None)

    assert request.operation == operation
    assert request.session is expected_session
    assert request.prompt is expected_prompt
    assert request.options is None
    assert request.timeout_seconds == expected_timeout
    if operation == "interrupt":
        assert request.response is None
        assert request.wait is None
    else:
        assert request.response == {"format": "text"}
        assert request.wait is None


@pytest.mark.parametrize(
    "wire",
    [
        {**_invoke(), "extra": 1},
        {**_invoke(), "handle": {"thread_id": "t"}},
        {"operation": "observe", "provider": "fake", "handle": {}, "prompt": "extra"},
        {"operation": "wait", "provider": "fake", "handle": {}, "wait": True},
        {
            "operation": "interrupt",
            "provider": "fake",
            "handle": {},
            "response": {"format": "text"},
        },
        {"operation": "interrupt", "provider": "fake", "handle": {}, "timeout_seconds": 1},
        {**_invoke(), "response": None},
    ],
)
def test_each_operation_rejects_extra_or_inapplicable_wire_fields(wire: dict[str, object]) -> None:
    with pytest.raises(AgentContractError) as error:
        _definition(wire)

    assert error.value.code == "invalid_request"


def test_literal_provider_target_and_operation_are_resolved_from_registry() -> None:
    registry, _fake = _registry()

    with pytest.raises(AgentContractError) as unknown_provider:
        validate_agent_wire({**_invoke(), "provider": "unknown"}, registry)
    assert unknown_provider.value.code == "unsupported_provider"

    with pytest.raises(AgentContractError) as unknown_target:
        validate_agent_wire({**_invoke(), "target": "arbitrary"}, registry)
    assert unknown_target.value.code == "unsupported_target"

    with pytest.raises(AgentContractError) as dynamic_provider:
        validate_agent_wire({**_invoke(), "provider": "${ .provider }"}, registry)
    assert dynamic_provider.value.code == "invalid_request"

    with pytest.raises(AgentContractError) as operation:
        validate_agent_wire({**_invoke(), "operation": "delete"}, registry)
    assert operation.value.code == "unsupported_operation"


def test_omitted_target_resolves_the_injected_provider_default() -> None:
    capabilities = _caps(target="primary")
    fake = FakeAgentProvider(capabilities)
    registry = ProviderRegistry({"fake": fake}, {"fake": "primary"})

    definition = validate_agent_wire(_invoke(), registry)

    assert definition.target == "primary"
    assert definition.target_context.target == "primary"


def test_registered_provider_can_reject_an_operation_through_capabilities() -> None:
    capabilities = ProviderCapabilities(
        provider="limited",
        target="default",
        operations=("invoke",),
        session_modes=("new",),
    )
    registry, _fake = _registry(provider="limited", capabilities=capabilities)
    with pytest.raises(AgentContractError) as error:
        validate_agent_wire({"operation": "observe", "provider": "limited", "handle": {}}, registry)
    assert error.value.code == "unsupported_operation"


def test_codex_static_capabilities_report_usage_unavailable() -> None:
    assert codex_agent_capabilities().usage_supported is False


@pytest.mark.parametrize(
    "session",
    [
        {"mode": "new", "thread_id": "t"},
        {"mode": "continue"},
        {"mode": "continue", "thread_id": ""},
        {"mode": "new", "extra": True},
        {"mode": "${ .mode }"},
    ],
)
def test_session_mode_and_thread_id_shape_are_closed(session: dict[str, object]) -> None:
    with pytest.raises(AgentContractError):
        _definition(_invoke(session=session))


def test_continue_requires_literal_mode_and_explicit_thread_id_expression() -> None:
    wire = _invoke(session={"mode": "continue", "thread_id": "${ .thread_id }"})
    request = evaluate_agent_request(_definition(wire), lambda expression: "thread-42")
    assert request.session == {"mode": "continue", "thread_id": "thread-42"}


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"custom": 1}, "unsupported_option"),
        ({"reasoning_effort": "high"}, "invalid_request"),
        ({"sandbox_mode": "danger-full-access"}, "invalid_request"),
        ({"sandbox_mode": "unsafe"}, "invalid_request"),
        ({"confirm_full_access": 1}, "invalid_request"),
    ],
)
def test_codex_capability_closes_options_and_enforces_constraints(
    options: dict[str, object], code: str
) -> None:
    registry, _fake = _registry(provider="codex", capabilities=codex_agent_capabilities())
    with pytest.raises(AgentContractError) as error:
        validate_agent_wire(_invoke(provider="codex", options=options), registry)
    assert error.value.code == code


def test_codex_continue_rejects_new_session_options() -> None:
    registry, _fake = _registry(provider="codex", capabilities=codex_agent_capabilities())
    wire = _invoke(
        provider="codex",
        session={"mode": "continue", "thread_id": "thread-1"},
        options={"model": "m"},
    )
    with pytest.raises(AgentContractError) as error:
        validate_agent_wire(wire, registry)
    assert error.value.code == "unsupported_option"


def test_codex_options_can_be_dynamic_with_capability_type_and_dependency_checks() -> None:
    registry, _fake = _registry(provider="codex", capabilities=codex_agent_capabilities())
    request = evaluate_agent_request(
        _definition(
            _invoke(
                provider="codex",
                options={"model": "${ .model }", "reasoning_effort": "high"},
            ),
            registry,
        ),
        lambda _expression: "model-x",
    )
    assert request.options == {"model": "model-x", "reasoning_effort": "high"}


def test_dynamic_fields_are_evaluated_once_and_not_recursively() -> None:
    wire = _invoke(
        session={"mode": "continue", "thread_id": "${ .thread }"},
        prompt="Literal ${ .not_jq } {{x}} {{x}}",
        bindings={"x": {"from": "${ .binding }", "format": "text"}},
        options={"model": "${ .model }"},
        wait="${ .wait }",
        timeout_seconds="${ .timeout }",
        response={"format": "json", "schema": {"const": "${ .schema_literal }"}},
    )
    registry, _fake = _registry()
    definition = validate_agent_wire(wire, registry)
    seen: list[str] = []
    values = {
        "${ .thread }": "thread-1",
        "${ .binding }": "${ .not_a_second_expression }",
        "${ .model }": "${ .also_literal }\u0000",
        "${ .wait }": True,
        "${ .timeout }": 9,
    }

    def evaluate(expression: str) -> object:
        seen.append(expression)
        return values[expression]

    request = evaluate_agent_request(definition, evaluate)
    assert len(seen) == 5 and set(seen) == {
        "${ .binding }",
        "${ .thread }",
        "${ .model }",
        "${ .wait }",
        "${ .timeout }",
    }
    assert (
        request.prompt
        == "Literal ${ .not_jq } ${ .not_a_second_expression } ${ .not_a_second_expression }"
    )
    assert request.options == {"model": "${ .also_literal }\u0000"}
    assert request.response == {"format": "json", "schema": {"const": "${ .schema_literal }"}}


@pytest.mark.parametrize(
    ("wire", "value"),
    [
        (_invoke(wait="${ .value }"), 1),
        (_invoke(timeout_seconds="${ .value }"), True),
        (_invoke(timeout_seconds="${ .value }"), 0),
        (_invoke(session={"mode": "continue", "thread_id": "${ .value }"}), None),
        (
            {"operation": "observe", "provider": "fake", "handle": "${ .value }"},
            ["not", "an", "object"],
        ),
    ],
)
def test_dynamic_values_are_type_checked_without_bool_integer_coercion(
    wire: dict[str, object], value: object
) -> None:
    with pytest.raises(AgentContractError):
        evaluate_agent_request(_definition(wire), lambda _expression: value)


def test_evaluator_failure_is_sanitized_and_never_exposes_exception_text() -> None:
    secret = "untrusted-jq-secret-81f2"

    def fail(_expression: str) -> object:
        raise RuntimeError(secret)

    with pytest.raises(AgentContractError) as error:
        evaluate_agent_request(_definition(_invoke()), fail)

    assert error.value.code == "preflight_failed"
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    ("wire", "expected_timeout"),
    [
        (_invoke(wait=False), None),
        (_invoke(wait="${ .wait }", timeout_seconds="${ .timeout }"), None),
    ],
)
def test_wait_false_and_null_timeout_are_allowed(
    wire: dict[str, object], expected_timeout: None
) -> None:
    values = {"${ .source }": "source", "${ .wait }": False, "${ .timeout }": None}
    request = evaluate_agent_request(_definition(wire), lambda expression: values[expression])
    assert request.wait is False
    assert request.timeout_seconds is expected_timeout


def test_literal_wait_false_rejects_a_literal_non_null_timeout() -> None:
    with pytest.raises(AgentContractError):
        validate_agent_wire(_invoke(wait=False, timeout_seconds=1), _registry()[0])


def test_wait_false_with_non_null_timeout_is_rejected_after_single_evaluation() -> None:
    wire = _invoke(wait="${ .wait }", timeout_seconds="${ .timeout }")
    seen: list[str] = []

    def evaluate(expression: str) -> object:
        seen.append(expression)
        return {"${ .source }": "source", "${ .wait }": False, "${ .timeout }": 12}[expression]

    with pytest.raises(AgentContractError):
        evaluate_agent_request(_definition(wire), evaluate)
    assert len(seen) == 3 and set(seen) == {"${ .source }", "${ .wait }", "${ .timeout }"}


def test_literals_are_not_jq_and_timeout_has_no_fixed_maximum() -> None:
    request = evaluate_agent_request(
        _definition(
            _invoke(
                prompt="Say ${ this stays literal }",
                bindings={},
                timeout_seconds=999_999_999,
            )
        ),
        lambda _expression: None,
    )
    assert request.prompt == "Say ${ this stays literal }"
    assert request.timeout_seconds == 999_999_999


def test_literal_handle_map_is_not_recursively_evaluated() -> None:
    handle = {"thread_id": "${ .keep_literal }", "turn_id": "turn-1"}
    seen: list[str] = []
    request = evaluate_agent_request(
        _definition({"operation": "observe", "provider": "fake", "handle": handle}),
        lambda expression: seen.append(expression) or None,
    )

    assert request.handle == handle
    assert seen == []


def test_provider_capabilities_do_not_import_the_legacy_three_job_limit() -> None:
    receipts = tuple(DispatchReceipt.accepted(handle={"id": str(index)}) for index in range(5))
    registry, fake = _registry(receipts=receipts)
    request = evaluate_agent_request(_definition(_invoke(), registry), lambda _value: None)
    prepared = fake.prepare(request, request.target_context)

    results = [fake.dispatch(prepared, f"request-{index}") for index in range(5)]

    assert len(results) == 5
    assert all(receipt.disposition == "ACCEPTED" for receipt in results)


def test_prepared_request_round_trip_is_fixed_and_defensively_copied() -> None:
    registry, fake = _registry()
    request = evaluate_agent_request(_definition(_invoke(), registry), lambda _value: {"x": [1]})
    original = request.session
    original["mode"] = "mutated"
    prepared = fake.prepare(request, request.target_context)
    snapshot = prepared.to_json()
    returned_options = prepared.options
    returned_options["mutate"] = True

    assert prepared.options == {}
    assert AgentRequest.from_json(request.to_json()).session == {"mode": "new"}
    assert type(prepared).from_json(snapshot).to_json() == snapshot
    assert set(json.loads(snapshot)) == {
        "version",
        "operation",
        "provider",
        "target",
        "target_identity",
        "session",
        "handle",
        "options",
        "prompt",
        "response",
        "wait",
        "timeout_seconds",
    }
    with pytest.raises(FrozenInstanceError):
        prepared.provider = "changed"


def test_request_and_prepared_request_reject_wrong_versions_keys_and_types() -> None:
    registry, fake = _registry()
    request = evaluate_agent_request(_definition(_invoke(), registry), lambda _value: None)
    prepared = fake.prepare(request, request.target_context)

    for cls, payload in (
        (AgentRequest, json.loads(request.to_json())),
        (type(prepared), json.loads(prepared.to_json())),
    ):
        malformed = copy.deepcopy(payload)
        malformed["version"] = True
        with pytest.raises(AgentContractError):
            cls.from_json(json.dumps(malformed))
        malformed = copy.deepcopy(payload)
        malformed["extra"] = None
        with pytest.raises(AgentContractError):
            cls.from_json(json.dumps(malformed))
        malformed = copy.deepcopy(payload)
        malformed["provider"] = 8
        with pytest.raises(AgentContractError):
            cls.from_json(json.dumps(malformed))


def test_result_round_trip_copy_immutability_and_fixed_keys() -> None:
    result = AgentResult.from_dict(_result_payload())
    encoded = result.to_json()
    exposed = result.handle
    exposed["opaque"].append("changed")

    assert AgentResult.from_json(encoded).to_json() == encoded
    assert result.handle == {"opaque": [1, None]}
    assert set(json.loads(encoded)) == set(_result_payload())
    with pytest.raises(FrozenInstanceError):
        result.status = "SUCCEEDED"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(version=True),
        lambda value: value.update(extra=1),
        lambda value: value.pop("error"),
        lambda value: value.update(status="DONE"),
        lambda value: value.update(
            usage={"input_tokens": True, "output_tokens": None, "total_tokens": None, "source": "x"}
        ),
        lambda value: value.update(error={"code": "private", "message": "raw exception"}),
    ],
)
def test_result_deserializer_rejects_bad_version_keys_types_and_unsafe_error(mutate) -> None:
    payload = _result_payload()
    mutate(payload)
    with pytest.raises(AgentContractError):
        AgentResult.from_dict(payload)


def test_usage_supports_partial_unknown_values_and_rejects_boolean_counts() -> None:
    usage = AgentUsage(input_tokens=None, output_tokens=3, total_tokens=None, source="exact_turn")
    assert AgentUsage.from_json(usage.to_json()).to_dict() == {
        "input_tokens": None,
        "output_tokens": 3,
        "total_tokens": None,
        "source": "exact_turn",
    }
    with pytest.raises(AgentContractError):
        AgentUsage(input_tokens=True, output_tokens=None, total_tokens=None, source=None)
    with pytest.raises(AgentContractError):
        AgentUsage(input_tokens=-1, output_tokens=None, total_tokens=None, source=None)


def test_dispatch_receipt_and_observation_have_versioned_round_trips() -> None:
    receipt = DispatchReceipt.accepted(handle={"thread": "t"}, remote_state="running")
    observation = Observation(
        state="COMPLETED",
        handle={"thread": "t"},
        remote_state="completed",
        final_text="ok",
        usage={"input_tokens": 1, "output_tokens": None, "total_tokens": None, "source": "api"},
    )

    assert DispatchReceipt.from_json(receipt.to_json()).to_json() == receipt.to_json()
    assert Observation.from_json(observation.to_json()).to_json() == observation.to_json()
    receipt_handle = receipt.handle
    receipt_handle["thread"] = "mutated"
    observation_handle = observation.handle
    observation_handle["thread"] = "mutated"
    assert receipt.handle == {"thread": "t"}
    assert observation.handle == {"thread": "t"}
    with pytest.raises(AgentContractError):
        DispatchReceipt.from_dict({"version": True})
    malformed_receipt = json.loads(receipt.to_json())
    malformed_receipt["extra"] = None
    with pytest.raises(AgentContractError):
        DispatchReceipt.from_dict(malformed_receipt)
    malformed_receipt = json.loads(receipt.to_json())
    malformed_receipt["disposition"] = "accepted"
    with pytest.raises(AgentContractError):
        DispatchReceipt.from_dict(malformed_receipt)
    malformed_receipt = json.loads(DispatchReceipt.rejected().to_json())
    malformed_receipt["error"] = None
    with pytest.raises(AgentContractError):
        DispatchReceipt.from_dict(malformed_receipt)
    malformed = json.loads(observation.to_json())
    malformed["state"] = "DONE"
    with pytest.raises(AgentContractError):
        Observation.from_dict(malformed)
    malformed = json.loads(observation.to_json())
    malformed["usage"]["input_tokens"] = True
    with pytest.raises(AgentContractError):
        Observation.from_dict(malformed)
    malformed = json.loads(observation.to_json())
    malformed["version"] = True
    with pytest.raises(AgentContractError):
        Observation.from_dict(malformed)


def test_json_deserializers_reject_invalid_utf8_and_duplicate_keys() -> None:
    with pytest.raises(AgentContractError):
        AgentResult.from_json(b"\xff")
    with pytest.raises(AgentContractError):
        AgentResult.from_json('{"version":1,"version":1}')


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [("accepted", "ACCEPTED"), ("rejected", "FAILED"), ("uncertain", "UNKNOWN")],
)
def test_dispatch_receipts_map_to_distinct_result_states(dispatch: str, expected: str) -> None:
    factory = {
        "accepted": lambda: DispatchReceipt.accepted(handle={"id": "h"}),
        "rejected": lambda: DispatchReceipt.rejected(),
        "uncertain": lambda: DispatchReceipt.uncertain(),
    }
    result = result_from_dispatch_receipt("call-2", _request(), factory[dispatch]())

    assert result.status == expected


def test_interrupt_acknowledgment_is_accepted_and_never_implies_remote_end() -> None:
    ack = DispatchReceipt.accepted(
        handle={"thread_id": "t", "turn_id": "u"}, remote_state="running"
    )
    request = evaluate_agent_request(
        _definition({"operation": "interrupt", "provider": "fake", "handle": {"id": "h"}}),
        lambda _expression: None,
    )
    result = result_from_dispatch_receipt("interrupt-1", request, ack)

    assert result.operation == "interrupt"
    assert result.status == "ACCEPTED"
    assert result.remote_state == "running"
    assert result.status != "INTERRUPTED"


def test_fake_provider_records_explicit_operations_and_observation_sequence() -> None:
    receipt = DispatchReceipt.accepted(handle={"id": "h"}, remote_state="queued")
    sequence = (
        Observation("RUNNING", handle={"id": "h"}, remote_state="running"),
        Observation("COMPLETED", handle={"id": "h"}, remote_state="completed", final_text="done"),
    )
    registry, fake = _registry(receipts=(receipt,), observations=sequence)
    assert isinstance(fake, ProviderProtocol)
    request = evaluate_agent_request(_definition(_invoke(), registry), lambda _value: None)
    prepared = fake.prepare(request, request.target_context)
    accepted = fake.dispatch(prepared, "client-1")
    first = fake.observe(accepted.handle, request.target_context)
    second = fake.observe(accepted.handle, request.target_context)

    assert [first.state, second.state] == ["RUNNING", "COMPLETED"]
    assert [call.operation for call in fake.calls] == ["prepare", "dispatch", "observe", "observe"]
    assert fake.calls[1].client_request_id == "client-1"


def test_fake_provider_sequences_failure_unknown_and_interrupt_ack_without_retry() -> None:
    dispatch = (DispatchReceipt.rejected(), DispatchReceipt.uncertain())
    observations = (Observation("FAILED", remote_state="failed"), Observation("UNKNOWN"))
    registry, fake = _registry(
        receipts=dispatch,
        observations=observations,
        interrupt_receipts=(DispatchReceipt.accepted(remote_state="running"),),
    )
    request = evaluate_agent_request(_definition(_invoke(), registry), lambda _value: None)
    prepared = fake.prepare(request, request.target_context)
    rejected = fake.dispatch(prepared, "reject-1")
    uncertain = fake.dispatch(prepared, "unknown-1")
    first = fake.observe({"id": "h"}, request.target_context)
    second = fake.observe({"id": "h"}, request.target_context)
    interrupt = fake.interrupt({"id": "h"}, request.target_context, "interrupt-1")

    assert [rejected.disposition, uncertain.disposition] == ["REJECTED", "UNCERTAIN"]
    assert [first.state, second.state] == ["FAILED", "UNKNOWN"]
    assert interrupt.disposition == "ACCEPTED" and interrupt.remote_state == "running"
    assert [call.operation for call in fake.calls] == [
        "prepare",
        "dispatch",
        "dispatch",
        "observe",
        "observe",
        "interrupt",
    ]


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("RUNNING", "RUNNING"),
        ("FAILED", "FAILED"),
        ("TIMED_OUT", "TIMED_OUT"),
        ("UNKNOWN", "UNKNOWN"),
        ("BLOCKED", "BLOCKED"),
        ("INTERRUPTED", "INTERRUPTED"),
    ],
)
def test_remote_observation_state_is_mapped_without_collapsing_result_status(
    state: str, status: str
) -> None:
    observation = Observation(state, handle={"opaque": "h"}, remote_state="provider-state")
    result = result_from_observation("call-3", _request(), observation)
    assert result.status == status
    assert result.remote_state == "provider-state"


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',
        "NaN",
        "Infinity",
        "-Infinity",
        '```json\n{"ok":true}\n```',
        '{"ok":true} trailing',
        '{"x":"\\ud800"}',
    ],
)
def test_json_final_response_rejects_ambiguous_or_non_strict_text(text: str) -> None:
    request = _request(response={"format": "json"})
    result = result_from_observation(
        "call-4",
        request,
        Observation("COMPLETED", remote_state="completed", final_text=text),
    )
    assert result.status == "FAILED"
    assert result.error["code"] == "output_invalid"
    assert result.final_text == text


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        (None, {"type": "null"}),
        (True, {"type": "boolean"}),
        (5, {"type": "integer"}),
        (2.5, {"type": "number"}),
        ("text", {"type": "string"}),
        ([1, None], {"type": "array", "items": {"type": ["integer", "null"]}}),
        (
            {"ok": True},
            {
                "type": "object",
                "properties": {"ok": {"const": True}},
                "additionalProperties": False,
            },
        ),
    ],
)
def test_json_response_supports_any_root_type_including_null(
    value: object, schema: dict[str, object]
) -> None:
    request = _request(response={"format": "json", "schema": schema})
    result = result_from_observation(
        "call-5",
        request,
        Observation("COMPLETED", remote_state="completed", final_text=json.dumps(value)),
    )
    assert result.status == "SUCCEEDED"
    assert result.data == value


def test_json_response_supports_a_bounded_local_reference() -> None:
    schema = {"$defs": {"answer": {"type": "string"}}, "$ref": "#/$defs/answer"}
    request = _request(response={"format": "json", "schema": schema})
    result = result_from_observation(
        "call-local-ref",
        request,
        Observation("COMPLETED", remote_state="completed", final_text='"answer"'),
    )
    assert result.status == "SUCCEEDED"
    assert result.data == "answer"


def test_schema_failure_keeps_raw_answer_and_remote_completion_state() -> None:
    request = _request(response={"format": "json", "schema": {"type": "integer"}})
    result = result_from_observation(
        "call-6",
        request,
        Observation(
            "COMPLETED",
            remote_state="completed",
            final_text='{"answer":42}',
            usage={
                "input_tokens": None,
                "output_tokens": 4,
                "total_tokens": None,
                "source": "turn",
            },
        ),
    )
    assert result.status == "FAILED"
    assert result.remote_state == "completed"
    assert result.final_text == '{"answer":42}'
    assert result.data is None
    assert result.usage.output_tokens == 4
    assert result.error["code"] == "output_invalid"


def test_json_null_is_successful_data_and_text_never_populates_data() -> None:
    json_result = result_from_observation(
        "call-7",
        _request(response={"format": "json"}),
        Observation("COMPLETED", remote_state="completed", final_text="null"),
    )
    text_result = result_from_observation(
        "call-8",
        _request(response={"format": "text"}),
        Observation("COMPLETED", remote_state="completed", final_text="null"),
    )
    assert json_result.status == "SUCCEEDED" and json_result.data is None
    assert text_result.status == "SUCCEEDED" and text_result.data is None


def test_response_schema_is_literal_and_uses_d1_schema_subset_and_resource_limits() -> None:
    schema = {"type": "string", "default": "unused"}
    assert validate_json_value_schema(schema, "value") is None
    assert validate_json_value_schema({"type": "string", "default": "unused"}, "value") is None

    with pytest.raises(ContractInvalid):
        validate_json_value_schema({"$ref": "https://example.invalid/schema"}, "value")
    with pytest.raises(ContractInvalid):
        validate_json_value_schema({"type": "string", "pattern": "x"}, "value")
    oversized: object = 1
    for _ in range(MAX_SCHEMA_DEPTH + 10):
        oversized = [oversized]
    with pytest.raises(ContractResourceLimit):
        validate_json_value_schema({"type": "array"}, oversized)


def test_schema_defaults_are_not_applied_and_invalid_schema_is_rejected_at_definition() -> None:
    request = _request(
        response={
            "format": "json",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "string", "default": "filled"}},
                "additionalProperties": False,
            },
        }
    )
    result = result_from_observation(
        "call-9",
        request,
        Observation("COMPLETED", remote_state="completed", final_text="{}"),
    )
    assert result.status == "SUCCEEDED"
    assert result.data == {}

    with pytest.raises(AgentContractError):
        _definition(_invoke(response={"format": "text", "schema": {"type": "string"}}))
    with pytest.raises(AgentContractError):
        _definition(_invoke(response={"format": "json", "schema": {"pattern": "x"}}))


def test_json_response_value_resource_limit_is_a_safe_logical_failure() -> None:
    value: object = 1
    for _ in range(MAX_SCHEMA_DEPTH + 10):
        value = [value]
    request = _request(response={"format": "json", "schema": {"type": "array"}})
    result = result_from_observation(
        "call-large",
        request,
        Observation("COMPLETED", remote_state="completed", final_text=json.dumps(value)),
    )
    assert result.status == "FAILED"
    assert result.error["code"] == "output_too_large"


def test_d1_schema_subset_helper_accepts_arbitrary_root_without_changing_contract_root() -> None:
    assert validate_json_value_schema({"type": "integer"}, 7) is None
    with pytest.raises(ContractOutputInvalid):
        validate_json_value_schema({"type": "integer"}, True)
    with pytest.raises(ContractInvalid):
        validate_json_value_schema({"type": "object", "properties": {}}, {})


def test_json_schema_resource_depth_matches_existing_d1_limit() -> None:
    schema: dict[str, object] = {"type": "integer"}
    for _ in range(MAX_SCHEMA_DEPTH):
        schema = {"$defs": {"node": schema}, "$ref": "#/$defs/node"}
    with pytest.raises((ContractResourceLimit, ContractInvalid)):
        validate_json_value_schema(schema, 1)


def test_text_response_preserves_text_and_usage_unknown_is_not_fabricated_as_zero() -> None:
    result = result_from_observation(
        "call-10",
        _request(),
        Observation(
            "COMPLETED",
            remote_state=None,
            final_text="answer",
            usage={
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "source": None,
            },
        ),
    )
    assert result.status == "SUCCEEDED"
    assert result.final_text == "answer"
    assert result.data is None
    assert result.remote_state is None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


def test_completed_without_final_text_is_unknown_not_success() -> None:
    result = result_from_observation(
        "call-11",
        _request(),
        Observation("COMPLETED", remote_state="completed", final_text=None),
    )
    assert result.status == "UNKNOWN"
    assert result.error["code"] == "response_unavailable"


def test_provider_protocol_declares_only_pure_interface_and_fake_is_injected() -> None:
    context = TargetContext("fake", "default", 1, endpoint=None, workspace=None)
    assert context.identity == {
        "provider": "fake",
        "target": "default",
        "contract_version": 1,
        "endpoint": None,
        "workspace": None,
    }
    registry, fake = _registry()
    assert registry.provider_ids == ("fake",)
    assert isinstance(fake, ProviderProtocol)


def test_schema_function_does_not_accept_bool_as_integer() -> None:
    with pytest.raises(ContractOutputInvalid):
        validate_json_value_schema({"type": "integer"}, True)
