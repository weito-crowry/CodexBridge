from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_bridge.workflows.agent_contracts import (
    AgentError,
    AgentRequest,
    AgentResult,
    DispatchReceipt,
    PreparedProviderRequest,
    TargetContext,
)
from codex_bridge.workflows.contracts import canonical_json


def _binding(**changes: object) -> dict[str, object]:
    identity: dict[str, object] = {
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
    identity.update(changes)
    return identity


def _target_context() -> TargetContext:
    return TargetContext(
        "codex",
        "default",
        1,
        endpoint="http://127.0.0.1:8765",
        workspace="D:\\workspace",
    )


def _request(
    context: TargetContext | None = None,
    *,
    prompt: str = "hello",
    timeout: int | None = None,
    wait: bool = True,
):
    target = context or _target_context()
    return AgentRequest(
        operation="invoke",
        provider="codex",
        target="default",
        target_context=target,
        session={"mode": "new"},
        handle=None,
        options={},
        prompt=prompt,
        response={"format": "text"},
        wait=wait,
        timeout_seconds=timeout,
    )


def _read_only_request(
    operation: str,
    handle: dict[str, str],
    context: TargetContext | None = None,
    *,
    timeout: int | None = None,
) -> AgentRequest:
    target = context or _target_context()
    return AgentRequest(
        operation=operation,
        provider="codex",
        target="default",
        target_context=target,
        session=None,
        handle=handle,
        options=None,
        prompt=None,
        response={"format": "text"},
        wait=None,
        timeout_seconds=timeout,
    )


def _interrupt_request(
    handle: dict[str, str], context: TargetContext | None = None
) -> AgentRequest:
    target = context or _target_context()
    return AgentRequest(
        operation="interrupt",
        provider="codex",
        target="default",
        target_context=target,
        session=None,
        handle=handle,
        options=None,
        prompt=None,
        response=None,
        wait=None,
        timeout_seconds=None,
    )


def _ledger_module():
    try:
        from codex_bridge.workflows import agent_ledger
    except ModuleNotFoundError as exc:
        pytest.fail(f"Agent ledger is not implemented: {exc.name}")
    return agent_ledger


def test_new_ledger_initializes_only_an_absent_run_local_file(tmp_path: Path) -> None:
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    module = _ledger_module()

    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory,
        _binding(),
        _target_context(),
    )

    database = run_directory / "agent_invocations.sqlite"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM run_binding").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM invocations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM owned_handles").fetchone() == (0,)
    assert ledger.read_binding() == {
        "binding_identity": _binding(),
        "target_context": _target_context().to_dict(),
    }


@pytest.mark.parametrize("file_kind", ["missing", "directory", "invalid_sqlite"])
def test_existing_ledger_open_never_initializes_or_repairs(tmp_path: Path, file_kind: str) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    database = run_directory / "agent_invocations.sqlite"
    if file_kind == "directory":
        database.mkdir()
    elif file_kind == "invalid_sqlite":
        database.write_bytes(b"not a sqlite database")
    before = database.read_bytes() if database.is_file() else None

    with pytest.raises(module.AgentLedgerIntegrityError):
        module.AgentInvocationLedger.open_existing(run_directory, _binding(), _target_context())

    assert (database.read_bytes() if database.is_file() else None) == before


def test_read_only_inspection_is_explicit_and_rejects_binding_or_schema_drift(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )

    info = module.AgentInvocationLedger.inspect_read_only(
        run_directory, _binding(), _target_context()
    )
    assert info.binding_identity == _binding()
    assert info.target_context == _target_context().to_dict()
    with pytest.raises(module.AgentLedgerIntegrityError):
        module.AgentInvocationLedger.inspect_read_only(
            run_directory,
            _binding(run_id="run-other-12345678"),
            _target_context(),
        )
    with pytest.raises(module.AgentLedgerIntegrityError):
        module.AgentInvocationLedger.inspect_read_only(
            run_directory,
            _binding(),
            TargetContext(
                "codex", "other", 1, endpoint="http://127.0.0.1:8765", workspace="D:\\workspace"
            ),
        )


def test_invocation_identity_is_deterministic_and_request_conflicts_do_not_create_rows(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    prepared = PreparedProviderRequest(_request(), _target_context())

    first = ledger.prepare_invoke("/do/0/agent", [2, 4], prepared, created_ms=1_800_000_000_000)
    replay = ledger.prepare_invoke("/do/0/agent", [2, 4], prepared, created_ms=1_800_000_001_000)
    other_node = ledger.prepare_invoke(
        "/do/1/agent", [2, 4], prepared, created_ms=1_800_000_000_000
    )

    expected_identity = {
        "namespace": "codexbridge-agent-v1",
        "binding": _binding(),
        "node_pointer": "/do/0/agent",
        "iteration_vector": [2, 4],
    }
    expected_id = hashlib.sha256(canonical_json(expected_identity).encode("utf-8")).hexdigest()
    assert first.invocation_id == expected_id
    assert first.client_request_id == f"cba-{expected_id}"
    assert replay.invocation_id == first.invocation_id
    assert other_node.invocation_id != first.invocation_id
    assert len(ledger.list_invocations()) == 2

    with pytest.raises(module.AgentLedgerConflictError):
        ledger.prepare_invoke(
            "/do/0/agent",
            [2, 4],
            PreparedProviderRequest(_request(prompt="changed"), _target_context()),
            created_ms=1_800_000_000_000,
        )
    assert len(ledger.list_invocations()) == 2


def test_more_than_four_distinct_nodes_are_kept_and_paged_without_an_execution_cap(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    prepared = PreparedProviderRequest(_request(), _target_context())

    records = [
        ledger.prepare_invoke(f"/do/{index}/agent", [], prepared, created_ms=index + 1)
        for index in range(7)
    ]

    first_page = ledger.list_invocations(limit=3)
    second_page = ledger.list_invocations(
        limit=3,
        after=(first_page[-1].created_ms, first_page[-1].invocation_id),
    )
    third_page = ledger.list_invocations(
        limit=3,
        after=(second_page[-1].created_ms, second_page[-1].invocation_id),
    )
    assert len(records) == 7
    assert len(first_page + second_page + third_page) == 7


def test_deadline_is_pinned_to_first_prepare_and_only_sqlite_integer_overflow_is_rejected(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    request = PreparedProviderRequest(_request(timeout=37), _target_context())

    first = ledger.prepare_invoke("/do/0/agent", [], request, created_ms=1_800_000_000_000)
    replay = ledger.prepare_invoke("/do/0/agent", [], request, created_ms=1_800_000_001_000)
    assert first.deadline_ms == 1_800_000_037_000
    assert replay.deadline_ms == first.deadline_ms

    with pytest.raises(module.AgentLedgerConflictError):
        ledger.prepare_invoke(
            "/do/1/agent",
            [],
            PreparedProviderRequest(_request(timeout=10**30), _target_context()),
            created_ms=1_800_000_000_000,
        )


def test_dispatch_claim_has_a_single_winner_and_maybe_sent_cannot_be_reclaimed(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )

    assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is True
    assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is False
    assert ledger.get_invocation(record.invocation_id).dispatch_state == "MAYBE_SENT"


def test_concurrent_claimers_have_exactly_one_dispatch_winner(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda _index: ledger.claim_dispatch(record.invocation_id, record.request_sha256),
                (0, 1),
            )
        )

    assert sorted(claims) == [False, True]
    assert ledger.get_invocation(record.invocation_id).dispatch_state == "MAYBE_SENT"


def test_accepted_receipt_and_exact_owned_handle_are_atomic_and_immutable(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )
    ledger.claim_dispatch(record.invocation_id, record.request_sha256)
    receipt = DispatchReceipt.accepted(
        handle={"thread_id": "thread_123456", "turn_id": "turn_123456"},
        remote_state="in_progress",
    )

    accepted = ledger.save_dispatch_receipt(record.invocation_id, receipt)
    assert accepted.dispatch_state == "ACCEPTED"
    assert accepted.dispatch_receipt_json == receipt.to_json()
    owned = ledger.get_owned_handle(record.invocation_id)
    assert owned.handle == receipt.handle
    assert len(ledger.list_owned_handles()) == 1
    assert (
        ledger.save_dispatch_receipt(record.invocation_id, receipt).dispatch_receipt_json
        == receipt.to_json()
    )
    with pytest.raises(module.AgentLedgerConflictError):
        ledger.save_dispatch_receipt(
            record.invocation_id,
            DispatchReceipt.accepted(
                handle={"thread_id": "thread_876543", "turn_id": "turn_876543"},
                remote_state="in_progress",
            ),
        )


def test_receipt_and_result_semantics_are_checked_before_persistence(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )
    ledger.claim_dispatch(record.invocation_id, record.request_sha256)

    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(record.invocation_id, DispatchReceipt.accepted())
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(
            record.invocation_id,
            DispatchReceipt.rejected("dispatch_rejected", handle={"thread_id": "t"}),
        )
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(
            record.invocation_id,
            DispatchReceipt.accepted(
                handle={"thread_id": "thread_123456", "turn_id": "turn_123456"},
                remote_state="successful",
            ),
        )

    wrong = AgentResult(
        invocation_id="different",
        operation="invoke",
        provider="codex",
        target="default",
        status="UNKNOWN",
    )
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_result(record.invocation_id, wrong)
    assert ledger.get_invocation(record.invocation_id).result_json is None


def test_uncertain_receipt_and_same_handle_from_another_invocation_cannot_be_accepted(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    prepared = PreparedProviderRequest(_request(), _target_context())
    first = ledger.prepare_invoke("/do/0/agent", [], prepared, created_ms=1)
    second = ledger.prepare_invoke("/do/1/agent", [], prepared, created_ms=2)
    ledger.claim_dispatch(first.invocation_id, first.request_sha256)
    ledger.claim_dispatch(second.invocation_id, second.request_sha256)
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(first.invocation_id, DispatchReceipt.uncertain())

    receipt = DispatchReceipt.accepted(
        handle={"thread_id": "thread_123456", "turn_id": "turn_123456"},
        remote_state="in_progress",
    )
    ledger.save_dispatch_receipt(first.invocation_id, receipt)
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(second.invocation_id, receipt)
    assert ledger.get_invocation(second.invocation_id).dispatch_state == "MAYBE_SENT"


def test_logical_result_is_immutable_and_remote_observation_is_exactly_owned(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )
    ledger.claim_dispatch(record.invocation_id, record.request_sha256)
    handle = {"thread_id": "thread_123456", "turn_id": "turn_123456"}
    ledger.save_dispatch_receipt(
        record.invocation_id,
        DispatchReceipt.accepted(handle=handle, remote_state="in_progress"),
    )
    result = AgentResult(
        invocation_id=record.invocation_id,
        operation="invoke",
        provider="codex",
        target="default",
        status="UNKNOWN",
        handle=handle,
        remote_state="in_progress",
        error={"code": "observation_unavailable", "message": "Agent state could not be observed."},
    )

    assert ledger.save_result(record.invocation_id, result).result_json == result.to_json()
    assert ledger.save_result(record.invocation_id, result).result_json == result.to_json()
    with pytest.raises(module.AgentLedgerConflictError):
        ledger.save_result(
            record.invocation_id,
            AgentResult(
                invocation_id=record.invocation_id,
                operation="invoke",
                provider="codex",
                target="default",
                status="FAILED",
                handle=handle,
                remote_state="failed",
                error={"code": "remote_failed", "message": "Agent execution failed."},
            ),
        )
    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.update_remote_state(
            record.invocation_id,
            {"thread_id": "thread_other", "turn_id": "turn_other"},
            _target_context(),
            remote_state="completed",
            observed_ms=2,
            remote_terminal=True,
        )
    ledger.update_remote_state(
        record.invocation_id,
        handle,
        _target_context(),
        remote_state="completed",
        observed_ms=3,
        remote_terminal=True,
    )
    assert ledger.get_invocation(record.invocation_id).result_json == result.to_json()
    assert ledger.get_owned_handle(record.invocation_id).remote_state == "completed"


def test_read_only_invocations_use_none_dispatch_and_are_never_claimable(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread_123456", "turn_id": "turn_123456"}

    for index, request in enumerate(
        (
            _request(wait=False),
            _read_only_request("observe", handle),
            _read_only_request("wait", handle, timeout=4),
        )
    ):
        record = ledger.prepare_invoke(
            f"/do/{index}/agent",
            [],
            PreparedProviderRequest(request, _target_context()),
            created_ms=10 + index,
        )
        if request.operation == "invoke":
            assert record.dispatch_state == "PREPARED"
            assert record.client_request_id == f"cba-{record.invocation_id}"
            assert record.deadline_ms is None
        else:
            assert record.dispatch_state == "NONE"
            assert record.client_request_id is None
            assert record.deadline_ms == (None if request.operation == "observe" else 4_012)
            assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is False

    records = ledger.list_invocations(limit=10)
    assert [record.dispatch_state for record in records] == ["PREPARED", "NONE", "NONE"]
    assert [record.operation for record in records] == ["invoke", "observe", "wait"]
    assert ledger.list_owned_handles() == ()


def test_read_only_owned_handle_lookup_updates_only_exact_prior_owner(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread_123456", "turn_id": "turn_123456"}
    invoke = ledger.prepare_invoke(
        "/do/0/invoke",
        [],
        PreparedProviderRequest(_request(), _target_context()),
        created_ms=1,
    )
    ledger.claim_dispatch(invoke.invocation_id, invoke.request_sha256)
    receipt = DispatchReceipt.accepted(handle=handle, remote_state="in_progress")
    ledger.save_dispatch_receipt(invoke.invocation_id, receipt)
    observe = ledger.prepare_invoke(
        "/do/1/observe",
        [],
        PreparedProviderRequest(_read_only_request("observe", handle), _target_context()),
        created_ms=2,
    )

    owner = ledger.find_owned_handle(handle, _target_context())
    assert owner is not None and owner.invocation_id == invoke.invocation_id
    assert observe.dispatch_state == "NONE"
    assert (
        ledger.find_owned_handle(
            {"thread_id": "thread_other", "turn_id": "turn_other"}, _target_context()
        )
        is None
    )
    ledger.update_remote_state_for_handle(
        handle,
        _target_context(),
        remote_state="completed",
        observed_ms=3,
        remote_terminal=True,
    )
    with pytest.raises(module.AgentLedgerConflictError):
        ledger.update_remote_state_for_handle(
            handle,
            _target_context(),
            remote_state="unknown",
            observed_ms=3,
            remote_terminal=False,
        )
    ledger.update_remote_state_for_handle(
        handle,
        _target_context(),
        remote_state="unknown",
        observed_ms=4,
        remote_terminal=False,
    )
    assert ledger.get_owned_handle(invoke.invocation_id).remote_state == "completed"
    assert ledger.get_owned_handle(invoke.invocation_id).observed_ms == 3
    assert ledger.get_invocation(invoke.invocation_id).result_json is None
    assert ledger.get_invocation(observe.invocation_id).dispatch_state == "NONE"
    assert len(ledger.list_owned_handles()) == 1


def test_interrupt_receipt_and_result_preserve_explicit_handle_without_ownership(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread:external", "turn_id": "turn:active"}
    request = _interrupt_request(handle)
    record = ledger.prepare_invoke(
        "/do/interrupt",
        [1],
        PreparedProviderRequest(request, _target_context()),
        created_ms=1_800_000_000_000,
    )

    assert record.dispatch_state == "PREPARED"
    assert record.client_request_id == f"cba-{record.invocation_id}"
    assert record.deadline_ms is None
    assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is True
    receipt = DispatchReceipt.accepted(handle=handle, remote_state="in_progress")
    accepted = ledger.save_dispatch_receipt(record.invocation_id, receipt)
    result = ledger.save_result(
        record.invocation_id,
        AgentResult(
            invocation_id=record.invocation_id,
            operation="interrupt",
            provider="codex",
            target="default",
            status="ACCEPTED",
            handle=handle,
            remote_state="in_progress",
        ),
    )

    assert accepted.dispatch_state == "ACCEPTED"
    assert accepted.dispatch_receipt_json == receipt.to_json()
    assert result.dispatch_state == "ACCEPTED"
    assert AgentResult.from_json(result.result_json).handle == handle
    assert ledger.list_owned_handles() == ()

    reopened = module.AgentInvocationLedger.open_existing(
        run_directory,
        _binding(),
        _target_context(),
    )
    persisted = reopened.get_invocation(record.invocation_id)
    assert persisted.result_json == result.result_json
    assert persisted.dispatch_state == "ACCEPTED"
    assert reopened.list_owned_handles() == ()


def test_interrupt_receipt_mismatch_stays_maybe_sent_without_ownership(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    requested = {"thread_id": "thread:external", "turn_id": "turn:active"}
    record = ledger.prepare_invoke(
        "/do/interrupt/mismatch",
        [],
        PreparedProviderRequest(_interrupt_request(requested), _target_context()),
        created_ms=1_800_000_000_000,
    )
    assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is True

    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_dispatch_receipt(
            record.invocation_id,
            DispatchReceipt.accepted(
                handle={"thread_id": "thread:other", "turn_id": "turn:active"},
                remote_state="interrupted",
            ),
        )

    assert ledger.get_invocation(record.invocation_id).dispatch_state == "MAYBE_SENT"
    assert ledger.list_owned_handles() == ()


@pytest.mark.parametrize(
    ("dispatch_state", "status", "error_code", "receipt"),
    [
        ("PREPARED", "FAILED", "preflight_failed", None),
        ("MAYBE_SENT", "UNKNOWN", "dispatch_uncertain", None),
        (
            "REJECTED",
            "FAILED",
            "unauthorized",
            DispatchReceipt.rejected(
                "unauthorized",
                handle={
                    "thread_id": "thread:external",
                    "turn_id": "turn:active",
                },
            ),
        ),
    ],
)
def test_interrupt_nonaccepted_results_keep_known_target_without_ownership(
    tmp_path: Path,
    dispatch_state: str,
    status: str,
    error_code: str,
    receipt: DispatchReceipt | None,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread:external", "turn_id": "turn:active"}
    record = ledger.prepare_invoke(
        f"/do/interrupt/{dispatch_state.lower()}",
        [],
        PreparedProviderRequest(_interrupt_request(handle), _target_context()),
        created_ms=1_800_000_000_000,
    )
    if dispatch_state in {"MAYBE_SENT", "REJECTED"}:
        assert ledger.claim_dispatch(record.invocation_id, record.request_sha256) is True
    if receipt is not None:
        ledger.save_dispatch_receipt(record.invocation_id, receipt)
    result = AgentResult(
        invocation_id=record.invocation_id,
        operation="interrupt",
        provider="codex",
        target="default",
        status=status,
        handle=handle,
        error=AgentError(error_code),
    )

    stored = ledger.save_result(record.invocation_id, result)

    assert stored.dispatch_state == dispatch_state
    assert AgentResult.from_json(stored.result_json).handle == handle
    assert ledger.list_owned_handles() == ()


def test_none_rows_detect_dispatch_receipt_owner_result_and_digest_corruption(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread_123456", "turn_id": "turn_123456"}
    records = [
        ledger.prepare_invoke(
            f"/do/{index}/observe",
            [],
            PreparedProviderRequest(_read_only_request("observe", handle), _target_context()),
            created_ms=10 + index,
        )
        for index in range(6)
    ]
    target_json = canonical_json(_target_context().to_dict())
    handle_json = canonical_json(handle)
    with sqlite3.connect(run_directory / "agent_invocations.sqlite") as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE invocations SET client_request_id=? WHERE invocation_id=?",
            ("cba-" + "a" * 64, records[0].invocation_id),
        )
        connection.execute(
            "UPDATE invocations SET dispatch_state='MAYBE_SENT' WHERE invocation_id=?",
            (records[1].invocation_id,),
        )
        receipt_json = DispatchReceipt.uncertain().to_json()
        connection.execute(
            "UPDATE invocations SET dispatch_receipt_json=?,dispatch_receipt_sha256=? "
            "WHERE invocation_id=?",
            (
                receipt_json,
                hashlib.sha256(receipt_json.encode()).hexdigest(),
                records[2].invocation_id,
            ),
        )
        connection.execute(
            """INSERT INTO owned_handles VALUES (?,?,?,?,?,?,?,?)""",
            (
                records[3].invocation_id,
                "codex",
                hashlib.sha256(target_json.encode()).hexdigest(),
                handle_json,
                hashlib.sha256(handle_json.encode()).hexdigest(),
                None,
                None,
                0,
            ),
        )
        wrong_result = AgentResult(
            invocation_id=records[4].invocation_id,
            operation="invoke",
            provider="codex",
            target="default",
            status="UNKNOWN",
            handle=handle,
        ).to_json()
        connection.execute(
            "UPDATE invocations SET result_json=?,result_sha256=? WHERE invocation_id=?",
            (
                wrong_result,
                hashlib.sha256(wrong_result.encode()).hexdigest(),
                records[4].invocation_id,
            ),
        )
        connection.execute(
            "UPDATE invocations SET request_sha256=? WHERE invocation_id=?",
            ("f" * 64, records[5].invocation_id),
        )

    for record in records:
        with pytest.raises(module.AgentLedgerIntegrityError):
            ledger.get_invocation(record.invocation_id)


def test_wait_result_cannot_commit_a_running_observation(tmp_path: Path) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    handle = {"thread_id": "thread_123456", "turn_id": "turn_123456"}
    record = ledger.prepare_invoke(
        "/do/0/wait",
        [],
        PreparedProviderRequest(
            _read_only_request("wait", handle),
            _target_context(),
        ),
        created_ms=10,
    )
    running = AgentResult(
        invocation_id=record.invocation_id,
        operation="wait",
        provider="codex",
        target="default",
        status="RUNNING",
        handle=handle,
        remote_state="in_progress",
    )

    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.save_result(record.invocation_id, running)


def test_row_digest_corruption_is_detected_without_loading_other_invocations(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent",
        [],
        module.PreparedProviderRequest(_request(), _target_context()),
        created_ms=1,
    )
    with sqlite3.connect(run_directory / "agent_invocations.sqlite") as connection:
        connection.execute(
            "UPDATE invocations SET request_json=? WHERE invocation_id=?",
            ('{"tampered":true}', record.invocation_id),
        )

    with pytest.raises(module.AgentLedgerIntegrityError):
        ledger.get_invocation(record.invocation_id)


def test_schema_drift_is_an_integrity_error_and_existing_rows_are_left_in_place(
    tmp_path: Path,
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()
    ledger = module.AgentInvocationLedger.initialize_new_database(
        run_directory, _binding(), _target_context()
    )
    record = ledger.prepare_invoke(
        "/do/0/agent", [], PreparedProviderRequest(_request(), _target_context()), created_ms=1
    )
    with sqlite3.connect(run_directory / "agent_invocations.sqlite") as connection:
        connection.execute(
            "CREATE TRIGGER extra_trigger AFTER INSERT ON invocations BEGIN SELECT 1; END"
        )

    with pytest.raises(module.AgentLedgerIntegrityError):
        module.AgentInvocationLedger.open_existing(run_directory, _binding(), _target_context())

    with sqlite3.connect(run_directory / "agent_invocations.sqlite") as connection:
        assert connection.execute(
            "SELECT dispatch_state FROM invocations WHERE invocation_id=?",
            (record.invocation_id,),
        ).fetchone() == ("PREPARED",)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name='extra_trigger'"
        ).fetchone() == (1,)


@pytest.mark.parametrize("binding", [{}, {"run_id": "x"}, {"run_id": "x", "workflow_id": "y"}])
def test_binding_requires_the_complete_checkpoint_identity(
    tmp_path: Path, binding: dict[str, object]
) -> None:
    module = _ledger_module()
    run_directory = tmp_path / "isolated-run"
    run_directory.mkdir()

    with pytest.raises(module.AgentLedgerIntegrityError):
        module.AgentInvocationLedger.initialize_new_database(
            run_directory, binding, _target_context()
        )

    assert not (run_directory / "agent_invocations.sqlite").exists()
