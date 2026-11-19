from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_bridge.workflows.contracts import canonical_json
from codex_bridge.workflows.strict_variable_frames import StrictWorkflowFrameStore
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
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _binding(**overrides: object) -> StrictWorkflowRunBindingInput:
    values: dict[str, object] = {
        "run_id": _RUN_ID,
        "workflow_id": _WORKFLOW_ID,
        "definition_id": _DEFINITION_ID,
        "definition_revision": 3,
        "contract_sha256": _HASH_A,
        "binding_digest": _HASH_B,
        "normalized_input_sha256": _HASH_C,
        "created_at": _CREATED_AT,
    }
    values.update(overrides)
    return StrictWorkflowRunBindingInput(**values)  # type: ignore[arg-type]


def _initialize(run_directory: Path) -> StrictWorkflowRunBindingInput:
    run_directory.mkdir(parents=True, exist_ok=True)
    binding = _binding()
    StrictWorkflowVariableStore(run_directory).initialize_new_database(binding)
    return binding


def _frame_store(
    run_directory: Path,
    binding: StrictWorkflowRunBindingInput,
    **kwargs: object,
) -> StrictWorkflowFrameStore:
    return StrictWorkflowFrameStore(run_directory, binding, **kwargs)


def _inspect_i4a(run_directory: Path, binding: StrictWorkflowRunBindingInput) -> object:
    return StrictWorkflowVariableStore(run_directory).inspect(
        binding.run_id,
        binding.workflow_id,
        expected_definition_id=binding.definition_id,
        expected_definition_revision=binding.definition_revision,
        expected_contract_sha256=binding.contract_sha256,
        expected_binding_digest=binding.binding_digest,
        expected_normalized_input_sha256=binding.normalized_input_sha256,
        expected_created_at=binding.created_at,
    )


def _connect(run_directory: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(run_directory / "variables.sqlite")
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def test_enter_replay_nested_loop_vectors_close_and_inspect(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-frames"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    _inspect_i4a(run_directory, binding)

    child = store.enter("workflow-root", "/do/1", [], _CREATED_AT)
    replay = store.enter("workflow-root", "/do/1", [], _LATER)
    assert replay == child
    assert child.status == "ACTIVE"
    assert child.iteration_vector == ()

    nested = store.enter(child.frame_id, "/do/1/nested/2", [], _CREATED_AT)
    loop_zero = store.enter(child.frame_id, "/do/1/for/3", [0], _CREATED_AT)
    loop_one = store.enter(child.frame_id, "/do/1/for/3", [1], _CREATED_AT)
    assert loop_zero.frame_id != loop_one.frame_id
    assert loop_zero.iteration_vector == (0,)
    assert loop_one.iteration_vector == (1,)

    with pytest.raises(StrictWorkflowVariableConflict):
        store.close(child.frame_id, _LATER)
    for frame in (nested, loop_zero, loop_one):
        closed = store.close(frame.frame_id, _LATER)
        assert closed.status == "CLOSED"
        assert store.close(frame.frame_id, _LATER) == closed
        with pytest.raises(StrictWorkflowVariableConflict):
            store.close(frame.frame_id, _CREATED_AT)
        with pytest.raises(StrictWorkflowVariableConflict):
            store.enter(frame.frame_id, frame.scope_path + "/again", [], _LATER)

    closed_child = store.close(child.frame_id, _LATER)
    assert closed_child.status == "CLOSED"
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter(child.frame_id, "/do/1/nested/again", [], _LATER)
    with pytest.raises(StrictWorkflowVariableConflict):
        store.close("workflow-root", _LATER)

    info = store.inspect_all()
    assert info.root_frame_id == "workflow-root"
    assert info.frame_count == 5
    assert info.maximum_depth == 2
    assert {frame.status for frame in info.frames} == {"ACTIVE", "CLOSED"}
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect_i4a(run_directory, binding)

    connection = _connect(run_directory)
    try:
        assert connection.execute("SELECT count(*) FROM variable_states").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM variable_mutations").fetchone()[0] == 0
    finally:
        connection.close()


def test_frame_identity_is_sha256_of_canonical_scope_metadata(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-identity"
    binding = _initialize(run_directory)
    frame = _frame_store(run_directory, binding).enter("workflow-root", "/do/1", [0], _CREATED_AT)
    run_id = _RUN_ID
    parent_id = "workflow-root"
    identity = {
        "run_id": run_id,
        "parent_frame_id": parent_id,
        "scope_path": "/do/1",
        "iteration_vector": [0],
    }
    expected = "frame-" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    assert frame.frame_id == expected
    assert frame.created_at == _CREATED_AT.isoformat(timespec="microseconds").replace("+00:00", "Z")

    assert _CREATED_AT.utcoffset() == timedelta(0)


def test_invalid_metadata_and_noncanonical_utc_are_rejected(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-invalid-metadata"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    for path in ("", "workflow", "relative", "/", "/bad/", "/bad//path"):
        with pytest.raises(StrictWorkflowVariableInvalidMetadata):
            store.enter("workflow-root", path, [], _CREATED_AT)
    for vector in ([True], [-1], (0,), [1.0]):
        with pytest.raises(StrictWorkflowVariableInvalidMetadata):
            store.enter("workflow-root", "/do/1", vector, _CREATED_AT)  # type: ignore[arg-type]
    with pytest.raises(StrictWorkflowVariableInvalidMetadata):
        store.enter("workflow-root", "/do/1", [], datetime(2026, 11, 19, 3, 4, 5))
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter("missing-parent", "/do/1", [], _CREATED_AT)


def test_simultaneous_enter_is_serialized_and_replayed(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-concurrent"
    binding = _initialize(run_directory)

    def enter_once() -> str:
        return (
            _frame_store(run_directory, binding)
            .enter("workflow-root", "/do/concurrent", [0], _CREATED_AT)
            .frame_id
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enter_once(), range(2)))
    assert results[0] == results[1]
    assert _frame_store(run_directory, binding).inspect_all().frame_count == 2


@pytest.mark.parametrize(
    "tamper", ["field", "schema", "foreign_key", "root", "path", "cycle", "version"]
)
def test_inspect_all_rejects_tampered_schema_and_frame_history(tmp_path: Path, tamper: str) -> None:
    run_directory = tmp_path / f"run-tamper-{tamper}"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    child = store.enter("workflow-root", "/do/1", [], _CREATED_AT)
    nested = store.enter(child.frame_id, "/do/1/nested", [], _CREATED_AT)

    connection = _connect(run_directory)
    try:
        if tamper == "field":
            connection.execute(
                "UPDATE scope_frames SET created_at='tampered' WHERE frame_id=?",
                (child.frame_id,),
            )
        elif tamper == "schema":
            connection.execute("CREATE TABLE injected (value TEXT)")
        elif tamper == "foreign_key":
            connection.execute(
                """
                INSERT INTO scope_frames (
                    run_id, frame_id, parent_frame_id, scope_path,
                    iteration_vector_json, status, created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, NULL)
                """,
                (
                    _RUN_ID,
                    "orphan-frame",
                    "missing-parent",
                    "/orphan",
                    "[]",
                    _CREATED_AT.isoformat().replace("+00:00", "Z"),
                ),
            )
        elif tamper == "root":
            connection.execute(
                "UPDATE scope_frames SET scope_path='/broken-root' WHERE frame_id='workflow-root'"
            )
        elif tamper == "path":
            connection.execute(
                "UPDATE scope_frames SET scope_path='/do/altered' WHERE frame_id=?",
                (child.frame_id,),
            )
        elif tamper == "cycle":
            connection.execute(
                "UPDATE scope_frames SET parent_frame_id=? WHERE frame_id=?",
                (nested.frame_id, child.frame_id),
            )
        else:
            connection.execute("PRAGMA user_version=77")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.inspect_all()


def test_wrong_run_binding_cannot_inspect_or_attach_to_another_run(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-binding"
    binding = _initialize(run_directory)
    wrong_binding = _binding(run_id="run-ffffffffffffffffffffffffffffffff")
    wrong_store = _frame_store(run_directory, wrong_binding)
    with pytest.raises(StrictWorkflowVariableConflict):
        wrong_store.inspect_all()
    with pytest.raises(StrictWorkflowVariableConflict):
        wrong_store.enter("workflow-root", "/do/other", [], _CREATED_AT)
    assert _frame_store(run_directory, binding).inspect_all().frame_count == 1


def test_readonly_inspector_missing_path_and_database_are_unchanged(tmp_path: Path) -> None:
    missing_directory = tmp_path / "does-not-exist"
    expected = _binding()
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _frame_store(missing_directory, expected).inspect_all()
    assert not missing_directory.exists()

    run_directory = tmp_path / "run-readonly"
    binding = _initialize(run_directory)
    database = run_directory / "variables.sqlite"
    before_bytes = database.read_bytes()
    before_stat = database.stat()
    before = _frame_store(run_directory, binding).inspect_all()
    assert database.read_bytes() == before_bytes
    after_stat = database.stat()
    assert after_stat.st_size == before_stat.st_size
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    after = _frame_store(run_directory, binding).inspect_all()
    assert after == before


def test_depth_limit_is_24_and_enter_close_roll_back_on_fault(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-depth"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    parent_id = "workflow-root"
    parent_path = "workflow"
    for index in range(1, 25):
        next_path = f"/node/{index}" if parent_path == "workflow" else f"{parent_path}/node/{index}"
        frame = store.enter(parent_id, next_path, [], _CREATED_AT)
        parent_id = frame.frame_id
        parent_path = frame.scope_path
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter(parent_id, f"{parent_path}/too-deep", [], _CREATED_AT)
    assert store.inspect_all().maximum_depth == 24

    def fail_after_insert(checkpoint: str) -> None:
        if checkpoint == "after_frame_insert":
            raise RuntimeError("untrusted-value-must-not-escape")

    failing_store = _frame_store(run_directory, binding, _fault_injector=fail_after_insert)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable) as error:
        failing_store.enter("workflow-root", "/rollback", [], _CREATED_AT)
    assert "untrusted-value-must-not-escape" not in str(error.value)
    assert store.inspect_all().frame_count == 25

    def fail_after_close(checkpoint: str) -> None:
        if checkpoint == "after_frame_close":
            raise RuntimeError("close-marker-must-not-escape")

    failing_close_store = _frame_store(run_directory, binding, _fault_injector=fail_after_close)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable) as close_error:
        failing_close_store.close(parent_id, _LATER)
    assert "close-marker-must-not-escape" not in str(close_error.value)
    leaf = next(frame for frame in store.inspect_all().frames if frame.frame_id == parent_id)
    assert leaf.status == "ACTIVE"
    assert store.close(parent_id, _LATER).status == "CLOSED"


def test_frame_limit_includes_root_and_rejects_the_next_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_bridge.workflows.strict_variable_frames as frame_module

    monkeypatch.setattr(frame_module, "_MAX_FRAMES", 5)
    run_directory = tmp_path / "run-frame-limit"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    for index in range(4):
        store.enter("workflow-root", f"/node/{index}", [], _CREATED_AT)
    assert store.inspect_all().frame_count == 5
    with pytest.raises(StrictWorkflowVariableConflict):
        store.enter("workflow-root", "/node/overflow", [], _CREATED_AT)


@pytest.mark.parametrize("table", ["variable_states", "variable_mutations"])
def test_nonempty_state_or_ledger_rejects_enter_close_and_inspect(
    tmp_path: Path, table: str
) -> None:
    run_directory = tmp_path / f"run-nonempty-{table}"
    binding = _initialize(run_directory)
    store = _frame_store(run_directory, binding)
    child = store.enter("workflow-root", "/do/1", [], _CREATED_AT)

    connection = _connect(run_directory)
    try:
        if table == "variable_states":
            connection.execute(
                """
                INSERT INTO variable_states (
                    run_id, frame_id, declaration_id, name, present, value_json,
                    version, value_digest, updated_at
                ) VALUES (?, ?, ?, ?, 0, NULL, 1, ?, ?)
                """,
                (
                    _RUN_ID,
                    child.frame_id,
                    "declaration-1",
                    "state",
                    _HASH_A,
                    _CREATED_AT.isoformat().replace("+00:00", "Z"),
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO variable_mutations (
                    execution_key, run_id, frame_id, declaration_id, mutation_ordinal,
                    operation, payload_digest, before_version, after_version,
                    before_present, after_present, before_digest, after_digest,
                    after_value_json, committed_at
                ) VALUES (?, ?, ?, ?, 0, 'assign', ?, 0, 1, 0, 1, NULL, ?, '1', ?)
                """,
                (
                    "execution-1",
                    _RUN_ID,
                    child.frame_id,
                    "declaration-1",
                    _HASH_A,
                    _HASH_B,
                    _CREATED_AT.isoformat().replace("+00:00", "Z"),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.enter("workflow-root", "/do/2", [], _CREATED_AT)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.close(child.frame_id, _LATER)
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        store.inspect_all()
