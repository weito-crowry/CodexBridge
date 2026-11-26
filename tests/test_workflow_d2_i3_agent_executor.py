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
)
from codex_bridge.workflows.agent_ledger import (
    AgentInvocationLedger,
    AgentLedgerIntegrityError,
)
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
        preflight_error: str | None = None,
    ) -> None:
        self._observations = list(observations or [])
        self._receipt = dispatch_receipt or DispatchReceipt.accepted(
            handle=_HANDLE, remote_state="in_progress"
        )
        self.preflight_error = preflight_error
        self.dispatch_count = 0
        self.observe_count = 0
        self.prepare_count = 0
        self.preflight_count = 0

    def capabilities(self, target: str) -> ProviderCapabilities:
        assert target == "default"
        return ProviderCapabilities(
            provider="codex",
            target="default",
            operations=("invoke", "observe"),
            session_modes=("new",),
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
        assert prepared.operation == "invoke"
        assert client_request_id.startswith("cba-")
        self.dispatch_count += 1
        return self._receipt

    def observe(self, handle: object, target_context: TargetContext) -> Observation:
        assert handle == _HANDLE
        assert target_context == _CONTEXT
        self.observe_count += 1
        if self._observations:
            return self._observations.pop(0)
        return Observation(
            "UNKNOWN",
            handle=_HANDLE,
            error=AgentError("observation_unavailable").to_dict(),
        )

    def interrupt(
        self, handle: object, target_context: TargetContext, client_request_id: str
    ) -> DispatchReceipt:
        raise AssertionError("I3 executor must not send external interrupt")


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
    provider: _Provider,
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


def _request(prompt: str = "hello", *, timeout: int | None = None) -> AgentRequest:
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
        wait=True,
        timeout_seconds=timeout,
    )


def _execute(
    executor: Any, request: AgentRequest | Callable[[], AgentRequest] | None = None
) -> Any:
    return executor.execute(
        request or _request(),
        node_pointer="/do/0/agent",
        iteration_vector=[2],
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
