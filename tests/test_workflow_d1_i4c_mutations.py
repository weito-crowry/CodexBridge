from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

import codex_bridge.workflows.strict_variable_frames as frame_module
import codex_bridge.workflows.strict_variable_mutations as mutation_module
from codex_bridge.workflows.contracts import DRAFT_2020_12, CompiledContract, compile_contract
from codex_bridge.workflows.strict_variable_frames import StrictWorkflowFrameStore
from codex_bridge.workflows.strict_variable_logic import (
    MISSING,
    StrictVariableLogicInvalid,
)
from codex_bridge.workflows.strict_variable_mutations import (
    MutationContext,
    StrictWorkflowVariableMutationStore,
    StrictWorkflowVariableNeedsReconciliation,
    StrictWorkflowVariableResourceLimit,
)
from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableConflict,
    StrictWorkflowVariableInvalidMetadata,
    StrictWorkflowVariableStorageUnavailable,
    StrictWorkflowVariableStore,
)

_RUN_ID = "run-0123456789abcdef0123456789abcdef"
_WORKFLOW_ID = "12345678-1234-4234-8234-1234567890ab"
_DEFINITION_ID = "abcdef01-2345-4678-89ab-cdef01234567"
_CREATED_AT = datetime(2026, 11, 19, 3, 4, 5, 123456, tzinfo=UTC)
_LATER = datetime(2026, 11, 19, 3, 5, 6, 654321, tzinfo=UTC)
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _schema(value_type: str = "object", **extra: object) -> dict[str, object]:
    return {
        "$schema": DRAFT_2020_12,
        "type": value_type,
        "additionalProperties": False,
        **extra,
    }


def _variable(
    declaration_id: str,
    name: str,
    value_schema: dict[str, object],
    *,
    owner_scope: str = "workflow",
    initial: object = MISSING,
) -> dict[str, object]:
    result: dict[str, object] = {
        "declaration_id": declaration_id,
        "name": name,
        "owner_scope": owner_scope,
        "value_schema": value_schema,
        "sensitivity": "public",
    }
    if initial is not MISSING:
        result["initial"] = initial
    return result


def _contract(
    variables: list[dict[str, object]] | None = None,
) -> CompiledContract:
    if variables is None:
        variables = [
            _variable("counter", "counter", _schema("integer"), initial=10),
            _variable(
                "items",
                "items",
                _schema("array", items={"type": "string"}),
                initial=[],
            ),
            _variable(
                "doc",
                "doc",
                _schema(
                    "object",
                    properties={
                        "a/b": {
                            "type": "object",
                            "properties": {"~key": {"type": "integer"}},
                            "required": ["~key"],
                            "additionalProperties": False,
                        },
                        "delete": {"type": "string"},
                        "optional": {"type": "string"},
                    },
                    required=["a/b", "delete"],
                ),
                initial={"a/b": {"~key": 1}, "delete": "required", "optional": "remove"},
            ),
            _variable("gone", "gone", _schema("string"), initial="present"),
            _variable("nullable", "nullable", _schema("null"), initial=None),
            _variable("implicit", "implicit", _schema("string")),
            _variable(
                "child-value",
                "child-value",
                _schema("integer"),
                owner_scope="/do/1",
                initial=2,
            ),
        ]
    return compile_contract(
        {
            "contract_version": 1,
            "input_schema": _schema(),
            "output_schema": _schema(),
            "variables": variables,
        }
    )


def _binding(contract: CompiledContract, **overrides: object) -> StrictWorkflowRunBindingInput:
    values: dict[str, object] = {
        "run_id": _RUN_ID,
        "workflow_id": _WORKFLOW_ID,
        "definition_id": _DEFINITION_ID,
        "definition_revision": 3,
        "contract_sha256": contract.contract_sha256,
        "binding_digest": _HASH_B,
        "normalized_input_sha256": _HASH_C,
        "created_at": _CREATED_AT,
    }
    values.update(overrides)
    return StrictWorkflowRunBindingInput(**values)  # type: ignore[arg-type]


def _store(
    run_directory: Path,
    contract: CompiledContract | None = None,
    *,
    binding: StrictWorkflowRunBindingInput | None = None,
    _fault_injector=None,
) -> StrictWorkflowVariableMutationStore:
    selected_contract = contract or _contract()
    selected_binding = binding or _binding(selected_contract)
    return StrictWorkflowVariableMutationStore(
        run_directory,
        selected_binding,
        selected_contract,
        _fault_injector=_fault_injector,
    )


def _initialize(
    run_directory: Path, contract: CompiledContract | None = None
) -> tuple[CompiledContract, StrictWorkflowRunBindingInput, StrictWorkflowVariableMutationStore]:
    selected_contract = contract or _contract()
    selected_binding = _binding(selected_contract)
    run_directory.mkdir(parents=True, exist_ok=True)
    StrictWorkflowVariableStore(run_directory).initialize_new_database(selected_binding)
    return (
        selected_contract,
        selected_binding,
        _store(run_directory, selected_contract, binding=selected_binding),
    )


def _context(
    *,
    node: str = "/node/1",
    frame: str = "workflow-root",
    vector: tuple[int, ...] | list[int] = (),
    ordinal: int = 0,
    subflow: tuple[str, ...] | list[str] | None = None,
) -> MutationContext:
    return MutationContext(
        node_path=node,
        iteration_vector=vector,
        frame_id=frame,
        mutation_ordinal=ordinal,
        subflow_call_context=subflow,
    )


def _apply(
    store: StrictWorkflowVariableMutationStore,
    declaration_id: str,
    operation: str,
    *,
    node: str,
    ordinal: int = 0,
    frame: str = "workflow-root",
    vector: tuple[int, ...] | list[int] = (),
    before_version: int = 0,
    before_digest: str | None = None,
    subflow: tuple[str, ...] | list[str] | None = None,
    **arguments: object,
):
    return store.apply(
        _context(
            node=node,
            frame=frame,
            vector=vector,
            ordinal=ordinal,
            subflow=subflow,
        ),
        declaration_id,
        operation,
        expected_before_version=before_version,
        expected_before_digest=before_digest,
        **arguments,
    )


def _connect(run_directory: Path) -> sqlite3.Connection:
    return sqlite3.connect(run_directory / "variables.sqlite")


def _counts(run_directory: Path) -> tuple[int, int]:
    with _connect(run_directory) as connection:
        state_count = connection.execute("SELECT COUNT(*) FROM variable_states").fetchone()[0]
        mutation_count = connection.execute("SELECT COUNT(*) FROM variable_mutations").fetchone()[0]
    return state_count, mutation_count


def test_all_operations_commit_state_and_ledger_and_keep_missing_null_tombstone_distinct(
    tmp_path: Path,
) -> None:
    _, _, store = _initialize(tmp_path / "run-operations")

    initialized = _apply(store, "counter", "initialize", node="/init/counter")
    assert initialized.after_version == 1
    assert initialized.after_value == 10
    incremented = _apply(
        store,
        "counter",
        "increment",
        node="/counter/increment",
        before_version=1,
        before_digest=initialized.after_digest,
        by=3,
    )
    decremented = _apply(
        store,
        "counter",
        "decrement",
        node="/counter/decrement",
        before_version=2,
        before_digest=incremented.after_digest,
        by=2,
    )
    assigned = _apply(
        store,
        "counter",
        "assign",
        node="/counter/assign",
        before_version=3,
        before_digest=decremented.after_digest,
        value=12,
    )
    assert assigned.after_value == 12

    list_init = _apply(store, "items", "initialize", node="/init/items")
    first_append = _apply(
        store,
        "items",
        "append",
        node="/items/append",
        ordinal=0,
        before_version=1,
        before_digest=list_init.after_digest,
        value="same-item",
    )
    second_append = _apply(
        store,
        "items",
        "append",
        node="/items/append",
        ordinal=1,
        before_version=2,
        before_digest=first_append.after_digest,
        value="same-item",
    )
    assert second_append.after_value == ["same-item", "same-item"]

    doc_init = _apply(store, "doc", "initialize", node="/init/doc")
    doc_set = _apply(
        store,
        "doc",
        "object_set",
        node="/doc/set",
        before_version=1,
        before_digest=doc_init.after_digest,
        pointer="/a~1b/~0key",
        value=2,
    )
    doc_delete = _apply(
        store,
        "doc",
        "object_delete",
        node="/doc/delete",
        before_version=2,
        before_digest=doc_set.after_digest,
        pointer="/optional",
    )
    assert doc_delete.after_value == {"a/b": {"~key": 2}, "delete": "required"}
    before_required_delete = store.inspect_ledger().mutation_count
    with pytest.raises(StrictVariableLogicInvalid):
        _apply(
            store,
            "doc",
            "object_delete",
            node="/doc/required-delete",
            before_version=3,
            before_digest=doc_delete.after_digest,
            pointer="/delete",
        )
    assert store.inspect_ledger().mutation_count == before_required_delete

    gone_init = _apply(store, "gone", "initialize", node="/init/gone")
    tombstone = _apply(
        store,
        "gone",
        "variable_delete",
        node="/gone/delete",
        before_version=1,
        before_digest=gone_init.after_digest,
    )
    null_value = _apply(store, "nullable", "initialize", node="/init/null")
    missing_state = store.read_current("workflow-root", "implicit")
    null_state = store.read_current("workflow-root", "nullable")
    deleted_state = store.read_current("workflow-root", "gone")

    assert missing_state.exists is False
    assert missing_state.version == 0
    assert missing_state.value is MISSING
    assert null_value.after_present is True and null_value.after_value is None
    assert null_state.exists is True and null_state.present is True and null_state.value is None
    assert deleted_state.exists is True and deleted_state.present is False
    assert deleted_state.version == 2 and deleted_state.value is MISSING
    assert tombstone.after_present is False and tombstone.after_value is MISSING

    audit = store.inspect_ledger()
    assert audit.mutation_count == 13
    assert audit.state_count == 5
    assert audit.frame_count == 1
    assert audit.total_current_value_bytes > 0
    assert {item.operation for item in audit.mutations} == {
        "initialize",
        "increment",
        "decrement",
        "assign",
        "append",
        "object_set",
        "object_delete",
        "variable_delete",
    }
    with _connect(tmp_path / "run-operations") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_initialize_without_initial_is_a_noop_and_is_never_persisted(tmp_path: Path) -> None:
    _, _, store = _initialize(tmp_path / "run-no-initial")
    with pytest.raises(StrictWorkflowVariableConflict):
        _apply(store, "implicit", "initialize", node="/init/implicit")
    assert store.read_current("workflow-root", "implicit").exists is False
    assert _counts(tmp_path / "run-no-initial") == (0, 0)


def test_tombstone_can_be_assigned_again_without_losing_history(tmp_path: Path) -> None:
    _, _, store = _initialize(tmp_path / "run-tombstone-reassign")
    initialized = _apply(store, "gone", "initialize", node="/gone/init")
    deleted = _apply(
        store,
        "gone",
        "variable_delete",
        node="/gone/delete",
        before_version=1,
        before_digest=initialized.after_digest,
    )
    restored = _apply(
        store,
        "gone",
        "assign",
        node="/gone/restore",
        before_version=2,
        before_digest=deleted.after_digest,
        value="restored",
    )
    assert deleted.after_present is False and deleted.after_version == 2
    assert restored.after_present is True and restored.after_value == "restored"
    assert restored.after_version == 3
    assert store.read_current("workflow-root", "gone").value == "restored"


def test_schema_failure_rolls_back_and_does_not_include_input_value_in_errors(
    tmp_path: Path,
) -> None:
    _, _, store = _initialize(tmp_path / "run-schema-failure")
    initialized = _apply(store, "counter", "initialize", node="/init/counter")
    before_audit = store.inspect_ledger()
    secret = "PRIVATE-SCHEMA-CANARY"
    with pytest.raises(StrictVariableLogicInvalid) as error:
        _apply(
            store,
            "counter",
            "assign",
            node="/counter/bad",
            before_version=1,
            before_digest=initialized.after_digest,
            value=secret,
        )
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert store.read_current("workflow-root", "counter").value == 10
    assert store.inspect_ledger().mutation_count == before_audit.mutation_count


def test_compare_and_swap_same_key_replay_and_payload_conflict(tmp_path: Path) -> None:
    _, _, store = _initialize(tmp_path / "run-cas")
    first = _apply(
        store,
        "counter",
        "assign",
        node="/counter/write",
        before_version=0,
        before_digest=None,
        value=4,
    )
    replay = _apply(
        store,
        "counter",
        "assign",
        node="/counter/write",
        before_version=0,
        before_digest=None,
        value=4,
    )
    assert replay.execution_key == first.execution_key
    assert replay.payload_digest == first.payload_digest
    assert replay.replayed is True
    assert replay.after_version == 1 and replay.after_value == 4
    with pytest.raises(StrictWorkflowVariableNeedsReconciliation) as error:
        _apply(
            store,
            "counter",
            "assign",
            node="/counter/write",
            before_version=0,
            before_digest=None,
            value=5,
        )
    assert error.value.code == "CONFLICT/NEEDS_RECONCILIATION"
    assert "5" not in str(error.value)
    with pytest.raises(StrictWorkflowVariableNeedsReconciliation):
        _apply(
            store,
            "counter",
            "increment",
            node="/counter/write",
            before_version=0,
            before_digest=None,
        )
    with pytest.raises(StrictWorkflowVariableNeedsReconciliation):
        _apply(
            store,
            "implicit",
            "assign",
            node="/counter/write",
            before_version=0,
            before_digest=None,
            value="different-declaration",
        )
    with pytest.raises(StrictWorkflowVariableConflict):
        _apply(
            store,
            "counter",
            "assign",
            node="/counter/stale",
            before_version=0,
            before_digest=None,
            value=6,
        )
    with pytest.raises(StrictWorkflowVariableConflict):
        _apply(
            store,
            "counter",
            "assign",
            node="/counter/stale-digest",
            before_version=1,
            before_digest="0" * 64,
            value=6,
        )
    assert store.read_current("workflow-root", "counter").value == 4
    assert store.inspect_ledger().mutation_count == 1


def test_restart_checkpoint_replay_returns_older_historical_value_without_rollback(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run-replay-history"
    contract, binding, store = _initialize(run_directory)
    first = _apply(
        store,
        "counter",
        "assign",
        node="/counter/checkpoint",
        ordinal=0,
        before_version=0,
        before_digest=None,
        value=20,
    )
    second = _apply(
        store,
        "counter",
        "assign",
        node="/counter/checkpoint",
        ordinal=1,
        before_version=1,
        before_digest=first.after_digest,
        value=30,
    )
    third = _apply(
        store,
        "counter",
        "assign",
        node="/counter/checkpoint",
        ordinal=2,
        before_version=2,
        before_digest=second.after_digest,
        value=40,
    )

    # The SQLite commit has happened while a simulated DBOS checkpoint has not.
    restarted = _store(run_directory, contract, binding=binding)
    replay = _apply(
        restarted,
        "counter",
        "assign",
        node="/counter/checkpoint",
        ordinal=0,
        before_version=0,
        before_digest=None,
        value=20,
    )
    assert replay.replayed is True
    assert replay.after_version == 1 and replay.after_value == 20
    assert restarted.read_current("workflow-root", "counter").version == 3
    assert restarted.read_current("workflow-root", "counter").value == 40
    historical = restarted.get_mutation(first.execution_key)
    assert historical is not None
    assert historical.after_value == replay.after_value
    assert historical.after_digest == replay.after_digest
    assert third.after_version == 3


def test_execution_key_binds_node_iteration_frame_ordinal_subflow_and_binding(
    tmp_path: Path,
) -> None:
    contract, binding, store = _initialize(tmp_path / "run-key-identity")
    base = _context(node="/node/1", vector=[0], ordinal=1, subflow=None)
    variations = [
        _context(node="/node/2", vector=[0], ordinal=1, subflow=None),
        _context(node="/node/1", vector=[1], ordinal=1, subflow=None),
        _context(node="/node/1", frame="frame-other", vector=[0], ordinal=1),
        _context(node="/node/1", vector=[0], ordinal=2, subflow=None),
        _context(node="/node/1", vector=[0], ordinal=1, subflow=["subflow-call-1"]),
    ]
    base_key = store._execution_key(store._validate_context(base)[0])
    keys = [store._execution_key(store._validate_context(item)[0]) for item in variations]
    assert len(set([base_key, *keys])) == len(keys) + 1

    other_binding = StrictWorkflowRunBindingInput(
        run_id="run-ffffffffffffffffffffffffffffffff",
        workflow_id=binding.workflow_id,
        definition_id=binding.definition_id,
        definition_revision=binding.definition_revision,
        contract_sha256=binding.contract_sha256,
        binding_digest=binding.binding_digest,
        normalized_input_sha256=binding.normalized_input_sha256,
        created_at=binding.created_at,
    )
    other_store = _store(tmp_path / "run-key-identity", contract, binding=other_binding)
    assert other_store._execution_key(other_store._validate_context(base)[0]) != base_key


def test_wrong_contract_binding_owner_and_context_are_rejected(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-identity-reject"
    contract, binding, store = _initialize(run_directory)
    other_contract = _contract([_variable("other", "other", _schema("string"))])
    with pytest.raises(StrictWorkflowVariableInvalidMetadata):
        _store(run_directory, other_contract, binding=binding)
    wrong_binding = StrictWorkflowRunBindingInput(
        run_id="run-ffffffffffffffffffffffffffffffff",
        workflow_id=binding.workflow_id,
        definition_id=binding.definition_id,
        definition_revision=binding.definition_revision,
        contract_sha256=binding.contract_sha256,
        binding_digest=binding.binding_digest,
        normalized_input_sha256=binding.normalized_input_sha256,
        created_at=binding.created_at,
    )
    with pytest.raises(StrictWorkflowVariableConflict):
        _store(run_directory, contract, binding=wrong_binding).read_current(
            "workflow-root", "counter"
        )
    with pytest.raises(StrictVariableLogicInvalid):
        _apply(
            store,
            "child-value",
            "assign",
            node="/child/write",
            value=1,
        )
    with pytest.raises(StrictWorkflowVariableInvalidMetadata):
        store.apply(
            _context(vector=[True]),
            "counter",
            "assign",
            expected_before_version=0,
            expected_before_digest=None,
            value=1,
        )


def test_ledger_and_state_corruption_fail_closed(tmp_path: Path) -> None:
    for tamper in ("after_digest", "state_value", "orphan_state"):
        run_directory = tmp_path / f"run-corrupt-{tamper}"
        _, _, store = _initialize(run_directory)
        result = _apply(
            store,
            "counter",
            "assign",
            node="/counter/corrupt",
            value=4,
        )
        with _connect(run_directory) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            if tamper == "after_digest":
                connection.execute(
                    "UPDATE variable_mutations SET after_digest=? WHERE execution_key=?",
                    ("0" * 64, result.execution_key),
                )
            elif tamper == "state_value":
                connection.execute(
                    "UPDATE variable_states SET value_json='5' WHERE declaration_id='counter'"
                )
            else:
                connection.execute("DELETE FROM variable_mutations")
            connection.commit()
        with pytest.raises(StrictWorkflowVariableStorageUnavailable):
            store.read_current("workflow-root", "counter")
        with pytest.raises(StrictWorkflowVariableStorageUnavailable):
            store.inspect_ledger()


def test_payload_digest_is_request_verified_but_not_self_proving(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-payload-limitation"
    _, _, store = _initialize(run_directory)
    result = _apply(
        store,
        "counter",
        "assign",
        node="/counter/digest",
        value=4,
    )
    with _connect(run_directory) as connection:
        connection.execute(
            "UPDATE variable_mutations SET payload_digest=? WHERE execution_key=?",
            ("0" * 64, result.execution_key),
        )
        connection.commit()
    # The row has no original request body, so a syntactically valid digest
    # cannot be independently recomputed during a full audit.
    assert store.inspect_ledger().mutation_count == 1
    with pytest.raises(StrictWorkflowVariableNeedsReconciliation):
        _apply(
            store,
            "counter",
            "assign",
            node="/counter/digest",
            before_version=0,
            before_digest=None,
            value=4,
        )


@pytest.mark.parametrize("corruption", ["schema", "user_version"])
def test_schema_and_user_version_corruption_are_rejected(tmp_path: Path, corruption: str) -> None:
    run_directory = tmp_path / f"run-schema-{corruption}"
    _, _, store = _initialize(run_directory)
    with _connect(run_directory) as connection:
        if corruption == "schema":
            connection.execute("CREATE TABLE injected (value TEXT)")
        else:
            connection.execute("PRAGMA user_version=77")
        connection.commit()
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.inspect_ledger()


@pytest.mark.parametrize("checkpoint", ["after_state_write", "after_ledger_write", "before_commit"])
def test_fault_injection_rolls_back_both_rows_without_leaking_hook_text(
    tmp_path: Path, checkpoint: str
) -> None:
    run_directory = tmp_path / f"run-fault-{checkpoint}"
    contract, binding, _ = _initialize(run_directory)

    def fail(name: str) -> None:
        if name == checkpoint:
            raise RuntimeError("PRIVATE-FAULT-CANARY")

    failing = _store(run_directory, contract, binding=binding, _fault_injector=fail)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable) as error:
        _apply(failing, "counter", "assign", node="/counter/fault", value=9)
    assert "PRIVATE-FAULT-CANARY" not in str(error.value)
    assert "PRIVATE-FAULT-CANARY" not in repr(error.value)
    assert _counts(run_directory) == (0, 0)
    assert failing.inspect_ledger().mutation_count == 0
    committed = _store(run_directory, contract, binding=binding)
    assert _apply(committed, "counter", "assign", node="/counter/fault", value=9).after_value == 9


def test_two_threads_same_key_commit_once_and_return_same_historical_result(
    tmp_path: Path,
) -> None:
    _, _, store = _initialize(tmp_path / "run-same-key-threads")
    barrier = Barrier(2)

    def apply_once(_index: int):
        barrier.wait()
        return _apply(
            store,
            "counter",
            "assign",
            node="/counter/concurrent",
            value=7,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply_once, range(2)))
    assert results[0].execution_key == results[1].execution_key
    assert results[0].after_version == results[1].after_version == 1
    assert results[0].after_digest == results[1].after_digest
    assert sorted(result.replayed for result in results) == [False, True]
    assert _counts(tmp_path / "run-same-key-threads") == (1, 1)


def test_two_different_keys_with_same_stale_before_allocate_one_version(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run-stale-threads"
    _, _, store = _initialize(run_directory)
    barrier = Barrier(2)

    def apply_once(index: int) -> str:
        barrier.wait()
        try:
            result = _apply(
                store,
                "counter",
                "assign",
                node=f"/counter/concurrent/{index}",
                value=index + 1,
            )
            return f"ok:{result.after_version}"
        except StrictWorkflowVariableConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply_once, range(2)))
    assert sum(item.startswith("ok:") for item in outcomes) == 1
    assert outcomes.count("conflict") == 1
    assert store.read_current("workflow-root", "counter").version == 1
    assert store.inspect_ledger().mutation_count == 1


def test_ledger_and_state_budgets_fail_atomically_at_patched_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_directory = tmp_path / "run-ledger-budget"
    _, _, ledger_store = _initialize(ledger_directory)
    monkeypatch.setattr(mutation_module, "_MAX_MUTATIONS", 1)
    first = _apply(
        ledger_store,
        "counter",
        "assign",
        node="/counter/limit",
        value=1,
    )
    assert (
        _apply(
            ledger_store,
            "counter",
            "assign",
            node="/counter/limit",
            value=1,
        ).replayed
        is True
    )
    with pytest.raises(StrictWorkflowVariableResourceLimit):
        _apply(
            ledger_store,
            "counter",
            "assign",
            node="/counter/limit-next",
            before_version=1,
            before_digest=first.after_digest,
            value=2,
        )
    assert ledger_store.read_current("workflow-root", "counter").value == 1
    assert ledger_store.inspect_ledger().mutation_count == 1

    state_directory = tmp_path / "run-state-budget"
    _, _, state_store = _initialize(state_directory)
    monkeypatch.setattr(mutation_module, "_MAX_MUTATIONS", 10_000)
    monkeypatch.setattr(mutation_module, "_MAX_STATE_BYTES", 5)
    null_value = _apply(state_store, "nullable", "assign", node="/state/null", value=None)
    exact = _apply(state_store, "counter", "assign", node="/state/counter", value=1)
    assert state_store.inspect_ledger().total_current_value_bytes == 5
    with pytest.raises(StrictWorkflowVariableResourceLimit):
        _apply(state_store, "items", "assign", node="/state/items", value=[])
    assert state_store.read_current("workflow-root", "nullable").value is None
    assert state_store.read_current("workflow-root", "counter").value == 1
    assert state_store.read_current("workflow-root", "items").exists is False
    assert state_store.inspect_ledger().mutation_count == 2
    assert null_value.after_version == exact.after_version == 1


def test_ledger_aware_frame_lifecycle_preserves_state_and_legacy_frame_store_stays_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child_schema = _schema("integer")
    contract = _contract(
        [
            _variable("root-value", "root-value", _schema("integer"), initial=1),
            _variable(
                "child-value",
                "child-value",
                child_schema,
                owner_scope="/do/1",
                initial=2,
            ),
        ]
    )
    run_directory = tmp_path / "run-frame-ledger"
    _, binding, store = _initialize(run_directory, contract)
    root_result = _apply(store, "root-value", "initialize", node="/init/root")
    monkeypatch.setattr(frame_module, "_MAX_FRAMES", 4)
    child_zero = store.enter_frame("workflow-root", "/do/1", [0], _CREATED_AT)
    assert store.enter_frame("workflow-root", "/do/1", [0], _LATER) == child_zero
    child_value = _apply(
        store,
        "child-value",
        "initialize",
        node="/do/1/init",
        frame=child_zero.frame_id,
        vector=[0],
    )
    child_one = store.enter_frame("workflow-root", "/do/1", [1], _LATER)
    assert child_one.frame_id != child_zero.frame_id
    assert child_one.iteration_vector == (1,)
    nested = store.enter_frame(child_zero.frame_id, "/do/1/nested", [0], _CREATED_AT)
    with pytest.raises(StrictWorkflowVariableConflict):
        store.close_frame("workflow-root", _LATER)
    with pytest.raises(StrictWorkflowVariableConflict):
        store.close_frame(child_zero.frame_id, _LATER)
    assert store.close_frame(nested.frame_id, _LATER).status == "CLOSED"
    closed = store.close_frame(child_zero.frame_id, _LATER)
    assert closed.status == "CLOSED"
    assert store.close_frame(child_zero.frame_id, _LATER) == closed
    assert store.read_current(child_zero.frame_id, "child-value").value == 2
    with pytest.raises(StrictWorkflowVariableConflict):
        _apply(
            store,
            "child-value",
            "assign",
            node="/do/1/after-close",
            frame=child_zero.frame_id,
            vector=[0],
            before_version=1,
            before_digest=child_value.after_digest,
            value=3,
        )
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter_frame("workflow-root", "/do/1", [0], _LATER)

    legacy = StrictWorkflowFrameStore(run_directory, binding)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        legacy.enter("workflow-root", "/legacy/new", [], _LATER)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        legacy.close(child_one.frame_id, _LATER)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        legacy.inspect_all()
    assert store.read_current("workflow-root", "root-value").value == 1
    assert (
        store.read_current("workflow-root", "root-value").value_digest == root_result.after_digest
    )
    assert store.inspect_ledger().frame_count == 4


def test_frame_limit_uses_existing_frame_policy_after_state_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_directory = tmp_path / "run-frame-limit"
    _, _, store = _initialize(run_directory)
    _apply(store, "counter", "initialize", node="/init/counter")
    monkeypatch.setattr(frame_module, "_MAX_FRAMES", 2)
    first = store.enter_frame("workflow-root", "/frame/one", [], _CREATED_AT)
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter_frame("workflow-root", "/frame/two", [], _LATER)
    assert first.status == "ACTIVE"
    assert store.inspect_ledger().frame_count == 2


def test_state_and_result_getters_are_detached_and_repr_is_value_free(tmp_path: Path) -> None:
    _, _, store = _initialize(tmp_path / "run-redaction")
    result = _apply(
        store,
        "doc",
        "assign",
        node="/doc/private",
        value={
            "a/b": {"~key": 3},
            "delete": "required",
            "optional": "PRIVATE-VALUE-CANARY",
        },
    )
    assert "PRIVATE-VALUE-CANARY" not in repr(result)
    state = store.read_current("workflow-root", "doc")
    value = state.value
    value["a/b"]["~key"] = 999
    assert store.read_current("workflow-root", "doc").value["a/b"]["~key"] == 3
    assert "PRIVATE-VALUE-CANARY" not in repr(state)
    assert store.read_current("workflow-root", "doc").value["optional"] == "PRIVATE-VALUE-CANARY"


def test_read_only_inspection_does_not_create_database_or_side_effect_rows(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-readonly-missing"
    contract = _contract()
    binding = _binding(contract)
    store = _store(run_directory, contract, binding=binding)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.read_current("workflow-root", "counter")
    assert not run_directory.exists()

    existing = tmp_path / "run-readonly"
    _, _, initialized = _initialize(existing)
    database = existing / "variables.sqlite"
    before_bytes = database.read_bytes()
    before_mtime = database.stat().st_mtime_ns
    assert initialized.inspect_ledger().mutation_count == 0
    assert database.read_bytes() == before_bytes
    assert database.stat().st_mtime_ns == before_mtime


def test_mutation_context_validation_and_full_integrity_readback(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-integrity"
    _, _, store = _initialize(run_directory)
    result = _apply(store, "counter", "initialize", node="/init/counter")
    state = store.read_current("workflow-root", "counter")
    assert state.value_digest == result.after_digest
    with _connect(run_directory) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
