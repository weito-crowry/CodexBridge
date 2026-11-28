from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dbos import DBOS

from codex_bridge.workflows.agent_contracts import (
    AgentError,
    AgentRequest,
    DispatchReceipt,
    Observation,
    PreparedProviderRequest,
    ProviderCapabilities,
    TargetContext,
    codex_agent_capabilities,
)
from codex_bridge.workflows.agent_ledger import (
    AgentInvocationLedger,
    AgentLedgerIntegrityError,
)
from codex_bridge.workflows.agent_provider import FakeAgentProvider
from codex_bridge.workflows.contracts import canonical_json, sha256_json

_HANDLE = {"thread_id": "thread_12345678", "turn_id": "turn_12345678"}
_CONTEXT = TargetContext(
    "codex",
    "default",
    1,
    endpoint="http://127.0.0.1:8765",
    workspace="D:\\workspace",
)


def _binding() -> dict[str, object]:
    return {
        "run_id": "run-12345678",
        "workflow_id": "workflow-12345678",
        "definition_id": "definition-12345678",
        "definition_revision": 2,
        "request_digest": "1" * 64,
        "source_sha256": "2" * 64,
        "contract_sha256": "3" * 64,
        "binding_digest": "4" * 64,
        "normalized_input_sha256": "5" * 64,
    }


class _Provider:
    def __init__(
        self,
        *,
        observations: list[Observation] | None = None,
        dispatch_receipt: DispatchReceipt | None = None,
        dispatch_receipts: list[DispatchReceipt] | None = None,
        interrupt_receipt: DispatchReceipt | None = None,
        preflight_error: str | None = None,
    ) -> None:
        self._observations = list(observations or [])
        self._receipt = dispatch_receipt or DispatchReceipt.accepted(
            handle=_HANDLE, remote_state="in_progress"
        )
        self._receipt_sequence = list(dispatch_receipts or [])
        self._interrupt_receipt = interrupt_receipt or DispatchReceipt.accepted(
            handle=_HANDLE, remote_state="in_progress"
        )
        self.preflight_error = preflight_error
        self.dispatch_count = 0
        self.interrupt_count = 0
        self.observe_count = 0
        self.observed_handles: list[dict[str, Any]] = []
        self.interrupted_handles: list[dict[str, Any]] = []
        self.prepare_count = 0
        self.preflight_count = 0

    def capabilities(self, target: str) -> ProviderCapabilities:
        assert target == "default"
        return ProviderCapabilities(
            provider="codex",
            target="default",
            operations=("invoke", "observe", "wait", "interrupt"),
            session_modes=("new", "continue"),
            usage_supported=False,
            endpoint=_CONTEXT.endpoint,
            workspace=_CONTEXT.workspace,
        )

    def prepare(
        self, request: AgentRequest, target_context: TargetContext
    ) -> PreparedProviderRequest:
        assert target_context == _CONTEXT
        self.prepare_count += 1
        return PreparedProviderRequest(request, target_context)

    def preflight(self, prepared: PreparedProviderRequest) -> None:
        assert prepared.provider == "codex"
        self.preflight_count += 1
        if self.preflight_error is not None:
            raise ValueError(self.preflight_error)

    def dispatch(
        self, prepared: PreparedProviderRequest, client_request_id: str
    ) -> DispatchReceipt:
        assert client_request_id.startswith("cba-")
        assert prepared.operation == "invoke"
        self.dispatch_count += 1
        if self.dispatch_count <= len(self._receipt_sequence):
            return self._receipt_sequence[self.dispatch_count - 1]
        return self._receipt

    def observe(self, handle: object, target_context: TargetContext) -> Observation:
        assert type(handle) is dict
        assert target_context == _CONTEXT
        self.observe_count += 1
        self.observed_handles.append(dict(handle))
        if self._observations:
            return self._observations.pop(0)
        return Observation(
            "UNKNOWN",
            handle=handle,
            error=AgentError("observation_unavailable").to_dict(),
        )

    def interrupt(
        self, handle: object, target_context: TargetContext, client_request_id: str
    ) -> DispatchReceipt:
        assert target_context == _CONTEXT
        assert type(handle) is dict
        assert client_request_id.startswith("cba-")
        self.interrupt_count += 1
        self.interrupted_handles.append(dict(handle))
        return self._interrupt_receipt


class _StepHarness:
    def __init__(self) -> None:
        self.checkpoints: dict[str, Any] = {}

    def __call__(self, name: str, function: Callable[..., Any], *args: Any) -> Any:
        if name not in self.checkpoints:
            self.checkpoints[name] = function(*args)
        return self.checkpoints[name]


_DBOS_EXECUTION_REGISTRY: dict[str, tuple[Any, AgentRequest]] = {}


@DBOS.workflow()
def _run_actual_agent_executor(execution_key: str) -> str:
    executor, request = _DBOS_EXECUTION_REGISTRY[execution_key]
    result = executor.execute(
        request,
        node_pointer="/do/0/agent",
        iteration_vector=[2],
        definition_sha256="a" * 64,
        task_input={"question": "hello"},
        variables={"region": "west"},
    )
    return result.to_json()


def _executor(
    tmp_path: Path,
    provider: Any,
    *,
    clock: Callable[[], int] | None = None,
    sleeper: Callable[[float], None] | None = None,
    fault: Callable[[str], None] | None = None,
    harness: _StepHarness | None = None,
) -> tuple[Any, AgentInvocationLedger, _StepHarness]:
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir(exist_ok=True)
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    steps = harness or _StepHarness()
    tick = 0

    def advancing_clock() -> int:
        nonlocal tick
        tick += 1
        return 1_800_000_000_000 + tick

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        step_runner=steps,
        clock_ms=clock or advancing_clock,
        durable_sleep=sleeper or (lambda _seconds: None),
        fault_injector=fault,
    )
    return executor, ledger, steps


def _request(
    prompt: str = "hello",
    *,
    timeout: int | None = None,
    wait: bool = True,
) -> AgentRequest:
    return AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=_CONTEXT,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt=prompt,
        response={"format": "text"},
        wait=wait,
        timeout_seconds=timeout,
    )


def _continue_request(*, wait: bool = True) -> AgentRequest:
    return AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=_CONTEXT,
        session={"mode": "continue", "thread_id": _HANDLE["thread_id"]},
        handle=None,
        options={},
        prompt="Continue the existing conversation.",
        response={"format": "text"},
        wait=wait,
        timeout_seconds=None,
    )


def _read_only_request(
    operation: str,
    *,
    timeout: int | None = None,
    handle: dict[str, str] | None = None,
    response: dict[str, Any] | None = None,
) -> AgentRequest:
    return AgentRequest(
        operation=operation,
        provider="codex",
        target="default",
        target_context=_CONTEXT,
        session=None,
        handle=handle or dict(_HANDLE),
        options=None,
        prompt=None,
        response=response or {"format": "text"},
        wait=None,
        timeout_seconds=timeout,
    )


def _interrupt_request(handle: dict[str, str] | None = None) -> AgentRequest:
    return AgentRequest(
        operation="interrupt",
        provider="codex",
        target="default",
        target_context=_CONTEXT,
        session=None,
        handle=dict(_HANDLE) if handle is None else handle,
        options=None,
        prompt=None,
        response=None,
        wait=None,
        timeout_seconds=None,
    )


def _execute(
    executor: Any,
    request: AgentRequest | Callable[[], AgentRequest] | None = None,
    *,
    node_pointer: str = "/do/0/agent",
    iteration_vector: list[int] | None = None,
) -> Any:
    return executor.execute(
        request or _request(),
        node_pointer=node_pointer,
        iteration_vector=[2] if iteration_vector is None else iteration_vector,
        definition_sha256="a" * 64,
        task_input={"question": "hello"},
        variables={"region": "west"},
    )


def _crash_once(wanted: str) -> Callable[[str], None]:
    seen = False

    def inject(point: str) -> None:
        nonlocal seen
        if point == wanted and not seen:
            seen = True
            raise RuntimeError(f"simulated crash: {wanted}")

    return inject


def test_executor_polls_without_a_fixed_count_and_persists_only_small_checkpoints(
    tmp_path: Path,
) -> None:
    running = [Observation("RUNNING", handle=_HANDLE, remote_state="in_progress") for _ in range(6)]
    provider = _Provider(
        observations=[
            *running,
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
        ]
    )
    executor, ledger, harness = _executor(tmp_path, provider)

    result = _execute(executor)

    assert result.status == "SUCCEEDED"
    assert result.final_text == "done"
    assert result.usage is None
    assert provider.dispatch_count == 1
    assert provider.observe_count == 7
    assert provider.preflight_count == 1
    assert ledger.get_invocation(result.invocation_id).dispatch_state == "ACCEPTED"
    assert ledger.get_invocation(result.invocation_id).result_json == result.to_json()
    assert len(harness.checkpoints) >= 9
    assert all("final_text" not in canonical_json(value) for value in harness.checkpoints.values())


def test_wait_false_returns_accepted_receipt_without_remote_observation(tmp_path: Path) -> None:
    provider = _Provider(
        dispatch_receipt=DispatchReceipt.accepted(handle=_HANDLE, remote_state="completed")
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor, _request(wait=False))

    assert result.status == "ACCEPTED"
    assert result.handle == _HANDLE
    assert result.remote_state == "completed"
    assert provider.dispatch_count == 1
    assert provider.observe_count == 0
    assert ledger.get_invocation(result.invocation_id).dispatch_state == "ACCEPTED"
    assert ledger.get_owned_handle(result.invocation_id).remote_terminal is True


def test_continue_wait_true_owns_only_the_new_turn_and_observes_it(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation(
                "COMPLETED",
                handle=_HANDLE,
                remote_state="completed",
                final_text="continued result",
            )
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(
        executor,
        _continue_request(wait=True),
        node_pointer="/do/0/continue",
    )

    assert result.status == "SUCCEEDED"
    assert result.handle == _HANDLE
    assert result.final_text == "continued result"
    assert provider.dispatch_count == 1
    assert provider.observe_count == 1
    assert provider.observed_handles == [_HANDLE]
    assert ledger.get_owned_handle(result.invocation_id).handle == _HANDLE
    assert ledger.get_owned_handle(result.invocation_id).remote_terminal is True


def test_interrupt_ack_is_dispatch_only_and_later_observe_confirms_terminal_state(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        interrupt_receipt=DispatchReceipt.accepted(handle=_HANDLE, remote_state="in_progress"),
        observations=[
            Observation(
                "INTERRUPTED",
                handle=_HANDLE,
                remote_state="interrupted",
                error=AgentError("remote_interrupted"),
            )
        ],
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    invoke_result = _execute(executor, _request(wait=False), node_pointer="/do/0/invoke")
    invoke_record = ledger.get_invocation(invoke_result.invocation_id)
    invoke_result_json = invoke_record.result_json
    assert invoke_result.status == "ACCEPTED"
    assert invoke_result_json is not None
    assert ledger.get_owned_handle(invoke_result.invocation_id).remote_state == "in_progress"

    acknowledged = _execute(
        executor,
        _interrupt_request(),
        node_pointer="/do/1/interrupt",
    )

    assert acknowledged.status == "ACCEPTED"
    assert acknowledged.handle == _HANDLE
    assert acknowledged.remote_state == "in_progress"
    assert provider.interrupt_count == 1
    assert provider.interrupted_handles == [_HANDLE]
    assert provider.dispatch_count == 1
    assert provider.observe_count == 0
    owned_rows = ledger.list_owned_handles()
    assert len(owned_rows) == 1
    assert {item.invocation_id for item in owned_rows} == {invoke_result.invocation_id}
    assert ledger.get_invocation(invoke_result.invocation_id).result_json == invoke_result_json
    assert ledger.get_owned_handle(invoke_result.invocation_id).remote_state == "in_progress"
    assert ledger.get_owned_handle(invoke_result.invocation_id).remote_terminal is False

    observed = _execute(
        executor,
        _read_only_request("observe"),
        node_pointer="/do/2/observe",
    )

    assert observed.status == "INTERRUPTED"
    assert observed.handle == _HANDLE
    assert observed.remote_state == "interrupted"
    assert provider.observe_count == 1
    assert ledger.get_invocation(invoke_result.invocation_id).result_json == invoke_result_json
    assert ledger.get_owned_handle(invoke_result.invocation_id).remote_state == "interrupted"
    assert ledger.get_owned_handle(invoke_result.invocation_id).remote_terminal is True
    assert len(ledger.list_owned_handles()) == 1


def test_executor_uses_provider_interrupt_contract_without_dispatch(tmp_path: Path) -> None:
    provider = FakeAgentProvider(
        codex_agent_capabilities(_CONTEXT),
        interrupt_receipts=(DispatchReceipt.accepted(handle=_HANDLE, remote_state="in_progress"),),
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor, _interrupt_request())

    assert result.status == "ACCEPTED"
    assert result.handle == _HANDLE
    assert [call.operation for call in provider.calls] == ["prepare", "interrupt"]
    assert provider.calls[1].client_request_id == f"cba-{result.invocation_id}"
    assert provider.calls[1].handle_json == canonical_json(_HANDLE)
    assert ledger.list_owned_handles() == ()


def test_executor_maps_mismatched_fake_interrupt_receipt_to_unknown_without_repost(
    tmp_path: Path,
) -> None:
    provider = FakeAgentProvider(
        codex_agent_capabilities(_CONTEXT),
        interrupt_receipts=(
            DispatchReceipt.accepted(
                handle={"thread_id": "thread_87654321", "turn_id": _HANDLE["turn_id"]},
                remote_state="in_progress",
            ),
        ),
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    first = _execute(executor, _interrupt_request(), node_pointer="/do/0/interrupt")
    replay = _execute(executor, _interrupt_request(), node_pointer="/do/0/interrupt")

    assert first.status == "UNKNOWN"
    assert first.handle == _HANDLE
    assert replay.to_json() == first.to_json()
    external_calls = [call for call in provider.calls if call.operation != "prepare"]
    assert [call.operation for call in external_calls] == ["interrupt"]
    assert ledger.get_invocation(first.invocation_id).dispatch_state == "MAYBE_SENT"
    assert ledger.list_owned_handles() == ()


def test_executor_maps_mismatched_continue_thread_to_unknown_without_repost(
    tmp_path: Path,
) -> None:
    provider = FakeAgentProvider(
        codex_agent_capabilities(_CONTEXT),
        dispatch_receipts=(
            DispatchReceipt.accepted(
                handle={"thread_id": "thread_87654321", "turn_id": "turn_12345678"},
                remote_state="in_progress",
            ),
        ),
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    first = _execute(
        executor,
        _continue_request(wait=False),
        node_pointer="/do/0/continue",
    )
    replay = _execute(
        executor,
        _continue_request(wait=False),
        node_pointer="/do/0/continue",
    )

    assert first.status == "UNKNOWN"
    assert first.handle is None
    assert replay.to_json() == first.to_json()
    external_calls = [call for call in provider.calls if call.operation != "prepare"]
    assert [call.operation for call in external_calls] == ["dispatch"]
    assert ledger.get_invocation(first.invocation_id).dispatch_state == "MAYBE_SENT"
    assert ledger.list_owned_handles() == ()


def test_uncertain_interrupt_keeps_target_unknown_and_never_reposts(
    tmp_path: Path,
) -> None:
    provider = _Provider(interrupt_receipt=DispatchReceipt.uncertain(handle=_HANDLE))
    executor, ledger, _harness = _executor(tmp_path, provider)

    first = _execute(executor, _interrupt_request(), node_pointer="/do/0/interrupt")
    replay = _execute(executor, _interrupt_request(), node_pointer="/do/0/interrupt")

    assert first.status == "UNKNOWN"
    assert first.handle == _HANDLE
    assert first.error == {
        "code": "dispatch_uncertain",
        "message": "Agent request acceptance could not be confirmed.",
    }
    assert replay.to_json() == first.to_json()
    assert provider.interrupt_count == 1
    assert provider.observe_count == 0
    assert ledger.get_invocation(first.invocation_id).dispatch_state == "MAYBE_SENT"
    assert ledger.list_owned_handles() == ()


def test_wait_false_replays_saved_receipt_after_checkpoint_fault_without_posting_again(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        fault=_crash_once("after_receipt_before_dispatch_checkpoint"),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, _request(wait=False))
    saved = ledger.get_invocation_for_node("/do/0/agent", [2])
    assert saved is not None
    assert saved.dispatch_state == "ACCEPTED" and saved.dispatch_receipt_json is not None
    assert saved.result_json is None
    result = _execute(executor, _request(wait=False))

    assert result.status == "ACCEPTED"
    assert provider.dispatch_count == 1
    assert provider.observe_count == 0


def test_observe_running_is_immutable_and_a_later_wait_uses_a_new_invocation(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("RUNNING", handle=_HANDLE, remote_state="in_progress"),
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    observed = _execute(executor, _read_only_request("observe"), node_pointer="/do/0/observe")
    replayed = _execute(executor, _read_only_request("observe"), node_pointer="/do/0/observe")
    waited = _execute(executor, _read_only_request("wait"), node_pointer="/do/1/wait")

    assert observed.status == replayed.status == "RUNNING"
    assert observed.to_json() == replayed.to_json()
    assert waited.status == "SUCCEEDED" and waited.final_text == "done"
    assert provider.dispatch_count == 0
    assert provider.observe_count == 2
    assert ledger.get_invocation(observed.invocation_id).dispatch_state == "NONE"
    assert ledger.get_invocation(waited.invocation_id).dispatch_state == "NONE"
    assert ledger.list_owned_handles() == ()


def test_wait_polls_without_a_fixed_120_second_limit(tmp_path: Path) -> None:
    now = [1_800_000_000_000]
    observations = [
        Observation("RUNNING", handle=_HANDLE, remote_state="in_progress") for _ in range(22)
    ]
    observations.append(
        Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
    )
    provider = _Provider(observations=observations)
    executor, _ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000) + 1_000),
    )

    result = _execute(executor, _read_only_request("wait"))

    assert result.status == "SUCCEEDED"
    assert provider.observe_count == 23
    assert now[0] - 1_800_000_000_000 > 120_000
    assert provider.dispatch_count == 0


def test_wait_false_maybe_sent_is_unknown_and_never_reposted(tmp_path: Path) -> None:
    provider = _Provider()
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        fault=_crash_once("after_claim_before_post"),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, _request(wait=False))
    result = _execute(executor, _request(wait=False))

    assert result.status == "UNKNOWN"
    assert result.error is not None and result.error["code"] == "dispatch_uncertain"
    assert provider.dispatch_count == 0
    assert provider.observe_count == 0
    assert ledger.get_invocation(result.invocation_id).dispatch_state == "MAYBE_SENT"


@pytest.mark.parametrize(
    ("observation", "expected_status"),
    [
        (
            Observation(
                "BLOCKED",
                handle=_HANDLE,
                remote_state="needs_approval",
                error=AgentError("needs_approval"),
            ),
            "BLOCKED",
        ),
        (
            Observation(
                "FAILED",
                handle=_HANDLE,
                remote_state="failed",
                error=AgentError("remote_failed"),
            ),
            "FAILED",
        ),
        (
            Observation("UNKNOWN", handle=_HANDLE, error=AgentError("observation_unavailable")),
            "UNKNOWN",
        ),
        (
            Observation(
                "INTERRUPTED",
                handle=_HANDLE,
                remote_state="interrupted",
                error=AgentError("remote_interrupted"),
            ),
            "INTERRUPTED",
        ),
    ],
)
def test_wait_returns_nonrunning_observation_states_to_workflow(
    tmp_path: Path,
    observation: Observation,
    expected_status: str,
) -> None:
    provider = _Provider(observations=[observation])
    executor, _ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor, _read_only_request("wait"))

    assert result.status == expected_status
    assert provider.observe_count == 1
    assert provider.dispatch_count == 0
    assert provider.interrupt_count == 0


def test_observe_json_result_validation_preserves_invalid_raw_text(tmp_path: Path) -> None:
    good_provider = _Provider(
        observations=[
            Observation(
                "COMPLETED", handle=_HANDLE, remote_state="completed", final_text='{"ok":true}'
            )
        ]
    )
    (tmp_path / "good").mkdir()
    good_executor, _good_ledger, _harness = _executor(tmp_path / "good", good_provider)
    good_result = _execute(
        good_executor,
        _read_only_request("observe", response={"format": "json"}),
        node_pointer="/do/0/good",
    )

    bad_provider = _Provider(
        observations=[
            Observation(
                "COMPLETED", handle=_HANDLE, remote_state="completed", final_text="not json"
            )
        ]
    )
    (tmp_path / "bad").mkdir()
    bad_executor, _bad_ledger, _harness = _executor(tmp_path / "bad", bad_provider)
    bad_result = _execute(
        bad_executor,
        _read_only_request("observe", response={"format": "json"}),
        node_pointer="/do/0/bad",
    )

    assert good_result.status == "SUCCEEDED" and good_result.data == {"ok": True}
    assert bad_result.status == "FAILED"
    assert bad_result.final_text == "not json"
    assert bad_result.error is not None and bad_result.error["code"] == "output_invalid"
    assert good_provider.dispatch_count == bad_provider.dispatch_count == 0


def test_read_only_observation_handle_mismatch_is_unknown_and_foreign_handle_stays_unowned(
    tmp_path: Path,
) -> None:
    foreign = {"thread_id": "thread_external", "turn_id": "turn_external"}
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="wrong"),
            Observation(
                "COMPLETED",
                handle={"thread_id": "thread_other", "turn_id": "turn_other"},
                remote_state="completed",
                final_text="mismatch",
            ),
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    accepted = _execute(
        executor,
        _read_only_request("observe", handle=foreign),
        node_pointer="/do/0/external",
    )

    assert accepted.status == "UNKNOWN"
    assert accepted.handle == foreign
    assert accepted.error is not None and accepted.error["code"] == "observation_unavailable"
    assert provider.observed_handles == [foreign]
    assert ledger.list_owned_handles() == ()


def test_wait_timeout_keeps_handle_remote_state_and_never_interrupts(tmp_path: Path) -> None:
    now = [1_000]
    provider = _Provider(
        observations=[Observation("RUNNING", handle=_HANDLE, remote_state="in_progress")]
    )
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: now[0],
        sleeper=lambda _seconds: now.__setitem__(0, 2_500),
    )

    result = _execute(executor, _read_only_request("wait", timeout=1))

    assert result.status == "TIMED_OUT"
    assert result.handle == _HANDLE
    assert result.remote_state == "in_progress"
    assert provider.observe_count == 1
    assert provider.dispatch_count == 0
    assert provider.interrupt_count == 0
    assert ledger.get_invocation(result.invocation_id).deadline_ms == 2_000
    assert ledger.list_owned_handles() == ()


def test_timeout_uses_latest_owned_state_instead_of_an_older_poll_checkpoint(
    tmp_path: Path,
) -> None:
    now = [1_000]
    ledger_box: list[AgentInvocationLedger] = []
    provider = _Provider(
        observations=[Observation("RUNNING", handle=_HANDLE, remote_state="in_progress")]
    )

    def sleep_then_complete(_seconds: float) -> None:
        ledger = ledger_box[0]
        invoke = ledger.get_invocation_for_node("/do/0/invoke", [2])
        assert invoke is not None
        now[0] = 2_500
        ledger.update_remote_state(
            invoke.invocation_id,
            _HANDLE,
            _CONTEXT,
            remote_state="completed",
            observed_ms=1_500,
            remote_terminal=True,
        )

    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: now[0],
        sleeper=sleep_then_complete,
    )
    ledger_box.append(ledger)
    accepted = _execute(
        executor,
        _request(wait=False),
        node_pointer="/do/0/invoke",
    )

    result = _execute(
        executor,
        _read_only_request("wait", timeout=1),
        node_pointer="/do/1/wait",
    )

    assert accepted.status == "ACCEPTED"
    assert result.status == "TIMED_OUT"
    assert result.remote_state == "completed"
    assert provider.observe_count == 1
    assert ledger.get_owned_handle(accepted.invocation_id).remote_terminal is True


def test_late_terminal_wait_observation_returns_timeout_and_keeps_remote_terminal_state(
    tmp_path: Path,
) -> None:
    ticks = iter([1_000, 1_000, 3_000])
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="late")
        ]
    )
    executor, _ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: next(ticks),
    )

    result = _execute(executor, _read_only_request("wait", timeout=1))

    assert result.status == "TIMED_OUT"
    assert result.handle == _HANDLE
    assert result.remote_state == "completed"
    assert provider.observe_count == 1
    assert provider.dispatch_count == 0


def test_wait_deadline_is_not_extended_when_prepare_checkpoint_recovers(tmp_path: Path) -> None:
    now = [1_000]
    provider = _Provider()
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: now[0],
        fault=_crash_once("after_prepare_before_checkpoint"),
    )
    request = _read_only_request("wait", timeout=1)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, request)
    initial = ledger.get_invocation_for_node("/do/0/agent", [2])
    assert initial is not None and initial.deadline_ms == 2_000
    now[0] = 2_500
    result = _execute(executor, request)

    assert result.status == "TIMED_OUT"
    assert result.handle == _HANDLE
    assert provider.observe_count == 0
    assert provider.dispatch_count == 0
    assert ledger.get_invocation(result.invocation_id).deadline_ms == 2_000


def test_read_only_remote_state_update_can_replay_before_result_but_result_stays_immutable(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
        ]
    )
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        fault=_crash_once("after_remote_state_before_result"),
    )
    accepted = _execute(executor, _request(wait=False), node_pointer="/do/0/invoke")
    observe_request = _read_only_request("observe")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, observe_request, node_pointer="/do/1/observe")
    observed = _execute(executor, observe_request, node_pointer="/do/1/observe")

    assert observed.status == "SUCCEEDED"
    assert observed.final_text == "done"
    assert provider.observe_count == 2
    assert provider.dispatch_count == 1
    assert ledger.get_invocation(accepted.invocation_id).result_json == accepted.to_json()
    assert ledger.get_owned_handle(accepted.invocation_id).remote_state == "completed"
    assert ledger.get_invocation(observed.invocation_id).dispatch_state == "NONE"


def test_unknown_observation_does_not_clear_previously_confirmed_terminal_state(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("UNKNOWN", handle=_HANDLE, error=AgentError("observation_unavailable"))
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)
    accepted = _execute(
        executor,
        _request(wait=False),
        node_pointer="/do/0/invoke",
    )
    ledger.update_remote_state(
        accepted.invocation_id,
        _HANDLE,
        _CONTEXT,
        remote_state="completed",
        observed_ms=1_800_000_000_010,
        remote_terminal=True,
    )

    observed = _execute(
        executor,
        _read_only_request("observe"),
        node_pointer="/do/1/observe",
    )

    assert observed.status == "UNKNOWN"
    assert provider.observe_count == 1
    assert ledger.get_owned_handle(accepted.invocation_id).remote_state == "completed"


def test_explicit_unknown_remote_state_does_not_clear_terminal_owner_state(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation(
                "UNKNOWN",
                handle=_HANDLE,
                remote_state="unknown",
                error=AgentError("observation_unavailable"),
            )
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)
    accepted = _execute(
        executor,
        _request(wait=False),
        node_pointer="/do/0/invoke",
    )
    ledger.update_remote_state(
        accepted.invocation_id,
        _HANDLE,
        _CONTEXT,
        remote_state="completed",
        observed_ms=1_800_000_000_010,
        remote_terminal=True,
    )

    observed = _execute(
        executor,
        _read_only_request("observe"),
        node_pointer="/do/1/observe",
    )

    assert observed.status == "UNKNOWN"
    assert observed.remote_state == "unknown"
    assert provider.observe_count == 1
    assert ledger.get_owned_handle(accepted.invocation_id).remote_state == "completed"


def test_late_unknown_observation_timeout_checkpoint_keeps_owned_terminal_state(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation(
                "UNKNOWN",
                handle=_HANDLE,
                remote_state="unknown",
                error=AgentError("observation_unavailable"),
            )
        ]
    )
    clock_values = iter((500, 1_000, 1_001, 2_000))
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: next(clock_values),
    )
    accepted = _execute(
        executor,
        _request(wait=False),
        node_pointer="/do/0/invoke",
    )
    ledger.update_remote_state(
        accepted.invocation_id,
        _HANDLE,
        _CONTEXT,
        remote_state="completed",
        observed_ms=900,
        remote_terminal=True,
    )

    timed_out = _execute(
        executor,
        _read_only_request("wait", timeout=1),
        node_pointer="/do/1/wait",
    )

    assert timed_out.status == "TIMED_OUT"
    assert timed_out.handle == _HANDLE
    assert timed_out.remote_state == "completed"
    assert provider.observe_count == 1
    assert ledger.get_owned_handle(accepted.invocation_id).remote_state == "completed"


def test_read_only_result_checkpoint_replay_does_not_reobserve(tmp_path: Path) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
        ]
    )
    executor, _ledger, _harness = _executor(
        tmp_path,
        provider,
        fault=_crash_once("after_result_before_poll_checkpoint"),
    )
    request = _read_only_request("observe")

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, request)
    result = _execute(executor, request)

    assert result.status == "SUCCEEDED"
    assert provider.observe_count == 1
    assert provider.dispatch_count == 0


def test_saved_read_only_result_replay_validates_terminal_dbos_checkpoint(tmp_path: Path) -> None:
    provider = _Provider(
        observations=[
            Observation("RUNNING", handle=_HANDLE, remote_state="in_progress"),
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
        ]
    )
    executor, _ledger, harness = _executor(tmp_path, provider)
    request = _read_only_request("wait")

    result = _execute(executor, request)
    terminal_step = next(
        name
        for name, checkpoint in harness.checkpoints.items()
        if type(checkpoint) is dict
        and checkpoint.get("kind") == "agent_observation"
        and checkpoint.get("result_status") == "SUCCEEDED"
    )
    harness.checkpoints[terminal_step] = {
        **harness.checkpoints[terminal_step],
        "request_sha256": "f" * 64,
    }

    with pytest.raises(AgentLedgerIntegrityError):
        _execute(executor, request)

    assert result.status == "SUCCEEDED"
    assert provider.observe_count == 2


def test_five_wait_false_nodes_are_not_limited_by_legacy_job_caps(tmp_path: Path) -> None:
    handles = [
        {"thread_id": f"thread_{index:08}", "turn_id": f"turn_{index:08}"} for index in range(5)
    ]
    provider = _Provider(
        dispatch_receipts=[
            DispatchReceipt.accepted(handle=handle, remote_state="in_progress")
            for handle in handles
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    results = [
        _execute(
            executor, _request(f"request {index}", wait=False), node_pointer=f"/do/{index}/agent"
        )
        for index in range(5)
    ]

    assert len(results) == 5
    assert all(result.status == "ACCEPTED" for result in results)
    assert provider.dispatch_count == 5
    assert provider.observe_count == 0
    assert len(ledger.list_invocations(limit=10)) == 5
    assert len(ledger.list_owned_handles(limit=10)) == 5


@pytest.mark.parametrize(
    ("crash_point", "expected_dispatches", "expected_status"),
    [
        ("before_claim", 1, "SUCCEEDED"),
        ("after_claim_before_post", 0, "UNKNOWN"),
        ("after_post_before_receipt", 1, "UNKNOWN"),
        ("after_receipt_before_dispatch_checkpoint", 1, "SUCCEEDED"),
        ("after_result_before_poll_checkpoint", 1, "SUCCEEDED"),
    ],
)
def test_step_replay_never_reposts_across_claim_receipt_and_result_crash_boundaries(
    tmp_path: Path,
    crash_point: str,
    expected_dispatches: int,
    expected_status: str,
) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
        ]
    )
    fault = _crash_once(crash_point)
    executor, ledger, _harness = _executor(tmp_path, provider, fault=fault)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor)
    result = _execute(executor)

    assert result.status == expected_status
    assert provider.dispatch_count == expected_dispatches
    assert ledger.get_invocation(result.invocation_id).result_json == result.to_json()


def test_prepare_ledger_recovery_does_not_reevaluate_latest_request_factory(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="pinned")
        ]
    )
    factory_calls = 0

    def factory() -> AgentRequest:
        nonlocal factory_calls
        factory_calls += 1
        return _request("pinned" if factory_calls == 1 else "changed")

    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        fault=_crash_once("after_prepare_before_checkpoint"),
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, factory)
    record = ledger.get_invocation_for_node("/do/0/agent", [2])
    assert record is not None

    result = _execute(executor, factory)

    assert factory_calls == 1
    assert result.status == "SUCCEEDED"
    assert provider.prepare_count == 1
    assert record.request.prompt == "pinned"


def test_deadline_after_late_completion_returns_timeout_and_keeps_remote_ownership(
    tmp_path: Path,
) -> None:
    ticks = iter([1000, 1000, 1000, 1000, 2500])
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="late")
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider, clock=lambda: next(ticks))

    result = _execute(executor, _request(timeout=1))

    assert result.status == "TIMED_OUT"
    assert result.handle == _HANDLE
    assert result.remote_state == "completed"
    assert provider.dispatch_count == 1
    owned = ledger.get_owned_handle(result.invocation_id)
    assert owned.handle == _HANDLE
    assert owned.remote_terminal is True


def test_deadline_before_claim_does_not_dispatch(tmp_path: Path) -> None:
    ticks = iter([1000, 2000])
    provider = _Provider()
    executor, ledger, _harness = _executor(tmp_path, provider, clock=lambda: next(ticks))

    result = _execute(executor, _request(timeout=1))

    assert result.status == "TIMED_OUT"
    assert result.handle is None
    assert provider.dispatch_count == 0
    assert ledger.get_invocation(result.invocation_id).dispatch_state == "PREPARED"


def test_recovery_keeps_original_deadline_and_does_not_interrupt_remote_work(
    tmp_path: Path,
) -> None:
    now = 1000
    provider = _Provider(
        observations=[Observation("RUNNING", handle=_HANDLE, remote_state="in_progress")]
    )
    executor, ledger, _harness = _executor(
        tmp_path,
        provider,
        clock=lambda: now,
        fault=_crash_once("after_receipt_before_dispatch_checkpoint"),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        _execute(executor, _request(timeout=10))
    record = ledger.get_invocation_for_node("/do/0/agent", [2])
    assert record is not None and record.deadline_ms == 11_000
    now = 11_000

    result = _execute(executor, _request(timeout=10))

    assert result.status == "TIMED_OUT"
    assert result.handle == _HANDLE
    assert provider.dispatch_count == 1
    assert provider.observe_count == 0
    assert ledger.get_invocation(result.invocation_id).deadline_ms == 11_000
    assert ledger.get_owned_handle(result.invocation_id).remote_terminal is False


@pytest.mark.parametrize(
    ("observation", "expected_status", "remote_terminal"),
    [
        (
            Observation(
                "BLOCKED",
                handle=_HANDLE,
                remote_state="needs_input",
                error=AgentError("needs_input"),
            ),
            "BLOCKED",
            False,
        ),
        (
            Observation(
                "FAILED", handle=_HANDLE, remote_state="failed", error=AgentError("remote_failed")
            ),
            "FAILED",
            True,
        ),
        (
            Observation(
                "INTERRUPTED",
                handle=_HANDLE,
                remote_state="interrupted",
                error=AgentError("remote_interrupted"),
            ),
            "INTERRUPTED",
            True,
        ),
    ],
)
def test_terminal_agent_statuses_preserve_exact_handle_and_ownership(
    tmp_path: Path,
    observation: Observation,
    expected_status: str,
    remote_terminal: bool,
) -> None:
    provider = _Provider(observations=[observation])
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor)

    assert result.status == expected_status
    assert result.handle == _HANDLE
    assert ledger.get_owned_handle(result.invocation_id).remote_terminal is remote_terminal


def test_preflight_failure_is_terminal_without_claim_or_post(tmp_path: Path) -> None:
    provider = _Provider(preflight_error="private token must not be persisted")
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor)

    assert result.status == "FAILED"
    assert result.error == {
        "code": "preflight_failed",
        "message": "Agent request could not be prepared.",
    }
    assert provider.dispatch_count == 0
    assert ledger.get_invocation(result.invocation_id).dispatch_state == "PREPARED"
    assert "private token" not in result.to_json()


def test_observation_with_state_or_handle_mismatch_is_unknown_not_success(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation(
                "COMPLETED",
                handle={"thread_id": "thread_87654321", "turn_id": "turn_87654321"},
                remote_state="in_progress",
                final_text="must not be accepted",
            )
        ]
    )
    executor, ledger, _harness = _executor(tmp_path, provider)

    result = _execute(executor)

    assert result.status == "UNKNOWN"
    assert result.handle == _HANDLE
    assert result.final_text is None
    assert ledger.get_owned_handle(result.invocation_id).handle == _HANDLE


def test_pinned_execution_metadata_digest_rejects_changed_checkpoint_identity(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
        ]
    )
    executor, _ledger, harness = _executor(tmp_path, provider)
    expected = {
        "definition_sha256": "a" * 64,
        "task_input_sha256": sha256_json({"question": "hello"}),
        "variables_sha256": sha256_json({"region": "west"}),
    }
    result = _execute(executor)
    assert result.status == "SUCCEEDED"
    cached_prepare = next(
        value for name, value in harness.checkpoints.items() if name.startswith("agent_i3_prepare_")
    )
    assert cached_prepare["execution_digests"] == expected
    with pytest.raises(AgentLedgerIntegrityError):
        executor.execute(
            _request(),
            node_pointer="/do/0/agent",
            iteration_vector=[2],
            definition_sha256="a" * 64,
            task_input={"question": "changed"},
            variables={"region": "west"},
        )


def test_cached_prepare_checkpoint_without_ledger_row_is_never_success(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
        ]
    )
    executor, ledger, harness = _executor(tmp_path, provider)
    result = _execute(executor)
    database = tmp_path / "isolated-run" / "agent_invocations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM owned_handles WHERE invocation_id=?", (result.invocation_id,)
        )
        connection.execute("DELETE FROM invocations WHERE invocation_id=?", (result.invocation_id,))

    with pytest.raises(AgentLedgerIntegrityError):
        _execute(executor)
    assert provider.dispatch_count == 1
    assert ledger.get_invocation_for_node("/do/0/agent", [2]) is None
    assert harness.checkpoints


def test_actual_dbos_rewind_after_receipt_crash_does_not_repeat_post(tmp_path: Path) -> None:
    from dbos import SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done")
        ]
    )
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    fault = _crash_once("after_receipt_before_dispatch_checkpoint")
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        clock_ms=lambda: 1_800_000_000_000,
        durable_sleep=lambda _seconds: None,
        fault_injector=fault,
    )
    execution_key = f"i3-{uuid4()}"
    workflow_id = f"workflow-{uuid4()}"
    _DBOS_EXECUTION_REGISTRY[execution_key] = (executor, _request())
    dbos_config = _dbos_config(run_directory / "dbos.sqlite")
    DBOS(config=dbos_config)
    DBOS.launch()
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_run_actual_agent_executor, execution_key)
        with pytest.raises(RuntimeError, match="simulated crash"):
            handle.get_result()

        resumed = DBOS.rewind_workflow(workflow_id)
        result_json = resumed.get_result()
    finally:
        DBOS.destroy()
        _DBOS_EXECUTION_REGISTRY.pop(execution_key, None)

    from codex_bridge.workflows.agent_contracts import AgentResult

    result = AgentResult.from_json(result_json)
    assert result.status == "SUCCEEDED"
    assert result.final_text == "done"
    assert provider.dispatch_count == 1
    assert provider.observe_count == 1


def test_actual_dbos_rewind_recovers_wait_false_receipt_without_observing(tmp_path: Path) -> None:
    from dbos import SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    provider = _Provider(
        dispatch_receipt=DispatchReceipt.accepted(handle=_HANDLE, remote_state="completed")
    )
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        clock_ms=lambda: 1_800_000_000_000,
        fault_injector=_crash_once("after_receipt_before_dispatch_checkpoint"),
    )
    execution_key = f"i4a-{uuid4()}"
    workflow_id = f"workflow-{uuid4()}"
    _DBOS_EXECUTION_REGISTRY[execution_key] = (executor, _request(wait=False))
    DBOS(config=_dbos_config(run_directory / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_run_actual_agent_executor, execution_key)
        with pytest.raises(RuntimeError, match="simulated crash"):
            handle.get_result()

        resumed = DBOS.rewind_workflow(workflow_id)
        result_json = resumed.get_result()
    finally:
        DBOS.destroy()
        _DBOS_EXECUTION_REGISTRY.pop(execution_key, None)

    from codex_bridge.workflows.agent_contracts import AgentResult

    result = AgentResult.from_json(result_json)
    assert result.status == "ACCEPTED"
    assert result.remote_state == "completed"
    assert provider.dispatch_count == 1
    assert provider.observe_count == 0


def test_actual_dbos_rewind_recovers_interrupt_receipt_without_reposting_or_observing(
    tmp_path: Path,
) -> None:
    from dbos import SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    provider = FakeAgentProvider(
        codex_agent_capabilities(_CONTEXT),
        interrupt_receipts=(DispatchReceipt.accepted(handle=_HANDLE, remote_state="in_progress"),),
    )
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        clock_ms=lambda: 1_800_000_000_000,
        fault_injector=_crash_once("after_receipt_before_dispatch_checkpoint"),
    )
    execution_key = f"i5-{uuid4()}"
    workflow_id = f"workflow-{uuid4()}"
    request = _interrupt_request()
    _DBOS_EXECUTION_REGISTRY[execution_key] = (executor, request)
    DBOS(config=_dbos_config(run_directory / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_run_actual_agent_executor, execution_key)
        with pytest.raises(RuntimeError, match="simulated crash"):
            handle.get_result()

        resumed = DBOS.rewind_workflow(workflow_id)
        result_json = resumed.get_result()
    finally:
        DBOS.destroy()
        _DBOS_EXECUTION_REGISTRY.pop(execution_key, None)

    from codex_bridge.workflows.agent_contracts import AgentResult

    result = AgentResult.from_json(result_json)
    record = ledger.get_invocation(result.invocation_id)
    assert result.status == "ACCEPTED"
    assert result.handle == _HANDLE
    assert record.dispatch_state == "ACCEPTED"
    assert record.result_json == result.to_json()
    external_calls = [call for call in provider.calls if call.operation != "prepare"]
    assert [call.operation for call in external_calls] == ["interrupt"]
    assert external_calls[0].handle_json == canonical_json(_HANDLE)
    assert ledger.list_owned_handles() == ()


def test_actual_dbos_rewind_does_not_repeat_saved_read_only_result(tmp_path: Path) -> None:
    from dbos import SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    provider = _Provider(
        observations=[
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
            Observation(
                "FAILED", handle=_HANDLE, remote_state="failed", error=AgentError("remote_failed")
            ),
        ]
    )
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        clock_ms=lambda: 1_800_000_000_000,
        fault_injector=_crash_once("after_result_before_poll_checkpoint"),
    )
    execution_key = f"i4a-{uuid4()}"
    workflow_id = f"workflow-{uuid4()}"
    request = _read_only_request("observe")
    _DBOS_EXECUTION_REGISTRY[execution_key] = (executor, request)
    DBOS(config=_dbos_config(run_directory / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_run_actual_agent_executor, execution_key)
        with pytest.raises(RuntimeError, match="simulated crash"):
            handle.get_result()

        resumed = DBOS.rewind_workflow(workflow_id)
        result_json = resumed.get_result()
    finally:
        DBOS.destroy()
        _DBOS_EXECUTION_REGISTRY.pop(execution_key, None)

    from codex_bridge.workflows.agent_contracts import AgentResult

    result = AgentResult.from_json(result_json)
    assert result.status == "SUCCEEDED"
    assert result.final_text == "done"
    assert provider.dispatch_count == 0
    assert provider.observe_count == 1


def test_actual_dbos_rewind_replays_wait_sleeps_before_saved_terminal_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbos import SetWorkflowID

    from codex_bridge.workflows.worker import _dbos_config

    provider = _Provider(
        observations=[
            Observation("RUNNING", handle=_HANDLE, remote_state="in_progress"),
            Observation("RUNNING", handle=_HANDLE, remote_state="in_progress"),
            Observation("COMPLETED", handle=_HANDLE, remote_state="completed", final_text="done"),
            Observation(
                "FAILED", handle=_HANDLE, remote_state="failed", error=AgentError("remote_failed")
            ),
        ]
    )
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = AgentInvocationLedger.initialize_new_database(run_directory, _binding(), _CONTEXT)
    from codex_bridge.workflows.agent_executor import AgentInvocationExecutor

    replay_calls = 0
    original_replay = AgentInvocationExecutor._replay_read_only_result

    def counted_replay(
        self: Any,
        record: Any,
        request_sha256: str,
        handle_sha256: str,
    ) -> Any:
        nonlocal replay_calls
        replay_calls += 1
        return original_replay(self, record, request_sha256, handle_sha256)

    monkeypatch.setattr(AgentInvocationExecutor, "_replay_read_only_result", counted_replay)

    sleep_calls: list[float] = []

    def durable_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        DBOS.sleep(seconds)

    executor = AgentInvocationExecutor(
        ledger,
        provider,
        clock_ms=lambda: 1_800_000_000_000,
        durable_sleep=durable_sleep,
        poll_interval_seconds=0.01,
        fault_injector=_crash_once("after_result_before_poll_checkpoint"),
    )
    execution_key = f"i4a-{uuid4()}"
    workflow_id = f"workflow-{uuid4()}"
    request = _read_only_request("wait")
    _DBOS_EXECUTION_REGISTRY[execution_key] = (executor, request)
    DBOS(config=_dbos_config(run_directory / "dbos.sqlite"))
    DBOS.launch()
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_run_actual_agent_executor, execution_key)
        with pytest.raises(RuntimeError, match="simulated crash"):
            handle.get_result()

        record = ledger.get_invocation_for_node("/do/0/agent", [2])
        assert record is not None and record.result_json is not None
        saved_result_json = record.result_json
        # Keep prepare, both RUNNING polls, and their sleeps; the terminal poll is step six.
        resumed = DBOS.rewind_workflow(workflow_id, start_step=6)
        result_json = resumed.get_result()
    finally:
        DBOS.destroy()
        _DBOS_EXECUTION_REGISTRY.pop(execution_key, None)

    from codex_bridge.workflows.agent_contracts import AgentResult

    result = AgentResult.from_json(result_json)
    assert result.status == "SUCCEEDED"
    assert result.final_text == "done"
    assert result.to_json() == saved_result_json
    assert provider.dispatch_count == 0
    assert provider.observe_count == 3
    assert replay_calls == 1
    assert sleep_calls == [0.01, 0.01, 0.01, 0.01]
