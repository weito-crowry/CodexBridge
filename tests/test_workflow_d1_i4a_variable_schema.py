from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableConflict,
    StrictWorkflowVariableInvalidMetadata,
    StrictWorkflowVariableStorageUnavailable,
    StrictWorkflowVariableStore,
    _check_integrity,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_RUN_ID = "run-0123456789abcdef0123456789abcdef"
_WORKFLOW_ID = "12345678-1234-4234-8234-1234567890ab"
_DEFINITION_ID = "abcdef01-2345-4678-89ab-cdef01234567"
_CREATED_AT = datetime(2026, 11, 19, 3, 4, 5, 123456, tzinfo=UTC)


def _binding(**overrides: object) -> StrictWorkflowRunBindingInput:
    values: dict[str, object] = {
        "run_id": _RUN_ID,
        "workflow_id": _WORKFLOW_ID,
        "definition_id": _DEFINITION_ID,
        "definition_revision": 3,
        "contract_sha256": _HASH_A,
        "binding_digest": _HASH_B,
        "normalized_input_sha256": "c" * 64,
        "created_at": _CREATED_AT,
    }
    values.update(overrides)
    return StrictWorkflowRunBindingInput(**values)  # type: ignore[arg-type]


def _store(run_directory: Path) -> StrictWorkflowVariableStore:
    return StrictWorkflowVariableStore(run_directory)


def _initialize(run_directory: Path) -> StrictWorkflowRunBindingInput:
    binding = _binding()
    _store(run_directory).initialize_new_database(binding)
    return binding


def _inspect(
    run_directory: Path,
    binding: StrictWorkflowRunBindingInput,
) -> object:
    return _store(run_directory).inspect(
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
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _valid_mutation_values(**overrides: object) -> tuple[object, ...]:
    values: dict[str, object] = {
        "execution_key": "execution-1",
        "run_id": _RUN_ID,
        "frame_id": "workflow-root",
        "declaration_id": "declaration-1",
        "mutation_ordinal": 0,
        "operation": "assign",
        "payload_digest": _HASH_A,
        "before_version": 0,
        "after_version": 1,
        "before_present": 0,
        "after_present": 1,
        "before_digest": None,
        "after_digest": _HASH_B,
        "after_value_json": "null",
        "committed_at": "2026-11-19T03:04:05.123456Z",
    }
    values.update(overrides)
    return tuple(values[key] for key in values)


def _insert_mutation(connection: sqlite3.Connection, **overrides: object) -> None:
    connection.execute(
        """
        INSERT INTO variable_mutations (
            execution_key, run_id, frame_id, declaration_id, mutation_ordinal,
            operation, payload_digest, before_version, after_version,
            before_present, after_present, before_digest, after_digest,
            after_value_json, committed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _valid_mutation_values(**overrides),
    )


def test_initialize_and_inspect_creates_complete_v1_in_nonempty_run_directory(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run-existing-artifacts"
    run_directory.mkdir()
    snapshot = run_directory / "input-snapshot.json"
    snapshot.write_text('{"safe":true}', encoding="utf-8")
    before_snapshot = snapshot.read_bytes()
    binding = _binding()

    _store(run_directory).initialize_new_database(binding)
    info = _inspect(run_directory, binding)

    assert info.user_version == 1
    assert info.run_id == _RUN_ID
    assert info.workflow_id == _WORKFLOW_ID
    assert info.definition_id == _DEFINITION_ID
    assert info.definition_revision == 3
    assert info.contract_sha256 == _HASH_A
    assert info.binding_digest == _HASH_B
    assert info.normalized_input_sha256 == "c" * 64
    assert info.run_format_version == 2
    assert info.store_schema_version == 1
    assert info.status == "INITIALIZED"
    assert info.created_at == "2026-11-19T03:04:05.123456Z"
    assert info.root_frame_id == "workflow-root"
    assert info.frame_count == 1
    assert info.variable_state_count == 0
    assert info.variable_mutation_count == 0
    assert repr(info) == "StrictWorkflowVariableDatabaseInfo(<verified>)"
    assert snapshot.read_bytes() == before_snapshot

    with _connect(run_directory) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert tables == {
            "run_binding",
            "scope_frames",
            "variable_states",
            "variable_mutations",
        }
        named_indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert named_indexes == {
            "uq_scope_frames_one_root",
            "idx_scope_frames_parent",
            "uq_variable_states_scope_name",
            "idx_variable_states_frame",
            "idx_variable_mutations_history",
        }
        assert connection.execute("SELECT COUNT(*) FROM run_binding").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM scope_frames").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM variable_states").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM variable_mutations").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reinitialization_and_any_preexisting_database_never_overwrite(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    binding = _initialize(run_directory)
    database = run_directory / "variables.sqlite"
    before = database.read_bytes()

    with pytest.raises(StrictWorkflowVariableConflict):
        _store(run_directory).initialize_new_database(binding)
    assert database.read_bytes() == before

    for contents in (b"not sqlite", b"user-owned database bytes"):
        other_directory = tmp_path / f"existing-{len(contents)}"
        other_directory.mkdir()
        existing = other_directory / "variables.sqlite"
        existing.write_bytes(contents)
        with pytest.raises(StrictWorkflowVariableConflict):
            _store(other_directory).initialize_new_database(binding)
        assert existing.read_bytes() == contents


def test_existing_database_with_v1_or_unknown_schema_is_not_reinitialized(
    tmp_path: Path,
) -> None:
    for index, version in enumerate((1, 77)):
        run_directory = tmp_path / f"existing-v{index}"
        run_directory.mkdir()
        database = run_directory / "variables.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(f"PRAGMA user_version={version}")
            connection.execute("CREATE TABLE user_owned(value TEXT)")
            connection.execute("INSERT INTO user_owned VALUES ('preserve')")
        before = database.read_bytes()
        with pytest.raises(StrictWorkflowVariableConflict):
            _store(run_directory).initialize_new_database(_binding())
        assert database.read_bytes() == before
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT value FROM user_owned").fetchone() == ("preserve",)


@pytest.mark.parametrize(
    "checkpoint",
    ["after_schema", "after_run_binding", "after_root", "before_commit"],
)
def test_fault_checkpoints_rollback_and_allow_safe_retry(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    run_directory = tmp_path / checkpoint
    run_directory.mkdir()
    snapshot = run_directory / "snapshot.yaml"
    snapshot.write_text("user artifact", encoding="utf-8")
    binding = _binding()
    observed: list[str] = []

    def fail(name: str) -> None:
        observed.append(name)
        if name == checkpoint:
            raise RuntimeError("private secret payload")

    with pytest.raises(StrictWorkflowVariableStorageUnavailable) as error:
        _store(run_directory).initialize_new_database(binding, _fault_injector=fail)
    assert observed[-1] == checkpoint
    assert "private secret payload" not in str(error.value)
    assert "private secret payload" not in repr(error.value)
    assert snapshot.read_text(encoding="utf-8") == "user artifact"
    assert not (run_directory / "variables.sqlite").exists()
    assert not (run_directory / "variables.sqlite-wal").exists()
    assert not (run_directory / "variables.sqlite-shm").exists()

    _store(run_directory).initialize_new_database(binding)
    assert _inspect(run_directory, binding).frame_count == 1


def test_inspect_missing_database_is_read_only_and_does_not_create_file(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect(run_directory, _binding())

    assert list(run_directory.iterdir()) == []


def test_inspect_checks_complete_expected_identity_and_does_not_change_rows(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    binding = _initialize(run_directory)
    with _connect(run_directory) as connection:
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "run_binding",
                "scope_frames",
                "variable_states",
                "variable_mutations",
            )
        }

    wrong_run = replace(binding, run_id="run-ffffffffffffffffffffffffffffffff")
    with pytest.raises(StrictWorkflowVariableConflict):
        _inspect(run_directory, wrong_run)
    wrong_workflow = replace(
        binding,
        workflow_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    with pytest.raises(StrictWorkflowVariableConflict):
        _inspect(run_directory, wrong_workflow)

    _inspect(run_directory, binding)
    with _connect(run_directory) as connection:
        after = {table: connection.execute(f"SELECT * FROM {table}").fetchall() for table in before}
    assert after == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "run-ABCDEF"),
        ("workflow_id", "12345678-1234-4234-8234-1234567890AB"),
        ("definition_id", "not-a-uuid"),
        ("definition_revision", True),
        ("definition_revision", 0),
        ("contract_sha256", "A" * 64),
        ("binding_digest", "short"),
        ("normalized_input_sha256", "d" * 63),
        ("run_format_version", True),
        ("run_format_version", 1),
        ("store_schema_version", False),
        ("store_schema_version", 2),
        ("status", "ACTIVE"),
        ("created_at", datetime(2026, 11, 19, 3, 4, 5)),
        (
            "created_at",
            datetime(2026, 11, 19, 3, 4, 5, tzinfo=timezone(timedelta(hours=1))),
        ),
    ],
)
def test_invalid_binding_metadata_is_rejected_without_value_leak(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    secret = "sensitive-input-payload"
    overrides = {field: secret if field == "contract_sha256" and value == "A" * 64 else value}
    binding = _binding(**overrides)

    assert secret not in repr(binding)
    with pytest.raises(StrictWorkflowVariableInvalidMetadata) as error:
        _store(run_directory).initialize_new_database(binding)
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert not (run_directory / "variables.sqlite").exists()


def test_inspect_rejects_invalid_expected_metadata_before_opening_database(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    with pytest.raises(StrictWorkflowVariableInvalidMetadata):
        _store(run_directory).inspect(
            "run-invalid",
            _WORKFLOW_ID,
            expected_definition_id=_DEFINITION_ID,
            expected_definition_revision=True,
            expected_contract_sha256=_HASH_A,
            expected_binding_digest=_HASH_B,
            expected_normalized_input_sha256="c" * 64,
            expected_created_at=_CREATED_AT,
        )
    assert list(run_directory.iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "table",
        "index",
        "column",
        "primary_key",
        "foreign_key",
        "check",
    ],
)
def test_inspect_rejects_unknown_or_altered_schema(
    tmp_path: Path,
    mutation: str,
) -> None:
    run_directory = tmp_path / mutation
    run_directory.mkdir()
    _initialize(run_directory)
    with sqlite3.connect(run_directory / "variables.sqlite") as connection:
        if mutation == "version":
            connection.execute("PRAGMA user_version=2")
        elif mutation == "table":
            connection.execute("CREATE TABLE unexpected(value TEXT)")
        elif mutation == "index":
            connection.execute("CREATE INDEX unexpected_idx ON run_binding(run_id)")
        elif mutation == "column":
            connection.execute("ALTER TABLE run_binding ADD COLUMN unexpected TEXT")
        else:
            replacements = {
                "primary_key": (
                    "run_binding",
                    "run_id TEXT PRIMARY KEY",
                    "run_id TEXT",
                ),
                "foreign_key": (
                    "scope_frames",
                    "REFERENCES run_binding (run_id)",
                    "REFERENCES missing_binding (run_id)",
                ),
                "check": (
                    "run_binding",
                    "status = 'INITIALIZED'",
                    "status IN ('INITIALIZED', 'OTHER')",
                ),
            }
            table_name, before, after = replacements[mutation]
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=replace(sql, ?, ?) WHERE type='table' AND name=?",
                (before, after, table_name),
            )
            connection.execute("PRAGMA schema_version=100")

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect(run_directory, _binding())


@pytest.mark.parametrize(
    "tamper",
    [
        "missing_root",
        "extra_frame",
        "closed_root",
        "bad_iteration_vector",
        "bad_frame_timestamp",
        "bad_binding_timestamp",
        "uppercase_hash",
        "invalid_definition_uuid",
        "invalid_run_id",
    ],
)
def test_inspect_rejects_tampered_initial_rows(tmp_path: Path, tamper: str) -> None:
    run_directory = tmp_path / tamper
    run_directory.mkdir()
    binding = _initialize(run_directory)
    with sqlite3.connect(run_directory / "variables.sqlite") as connection:
        if tamper == "missing_root":
            connection.execute("DELETE FROM scope_frames")
        elif tamper == "extra_frame":
            connection.execute(
                "INSERT INTO scope_frames VALUES (?, 'child', 'workflow-root', "
                "'workflow.child', '[]', 'ACTIVE', ?, NULL)",
                (_RUN_ID, "2026-11-19T03:04:05.123456Z"),
            )
        elif tamper == "closed_root":
            connection.execute(
                "UPDATE scope_frames SET status='CLOSED', closed_at=?",
                ("2026-11-19T03:04:05.123456Z",),
            )
        elif tamper == "bad_iteration_vector":
            connection.execute("UPDATE scope_frames SET iteration_vector_json='[ ]'")
        elif tamper == "bad_frame_timestamp":
            connection.execute("UPDATE scope_frames SET created_at='not-a-timestamp'")
        elif tamper == "bad_binding_timestamp":
            connection.execute("UPDATE run_binding SET created_at='not-a-timestamp'")
        elif tamper == "uppercase_hash":
            connection.execute("UPDATE run_binding SET contract_sha256=?", ("A" * 64,))
        elif tamper == "invalid_definition_uuid":
            connection.execute("UPDATE run_binding SET definition_id='NOT-A-UUID'")
        else:
            connection.execute("UPDATE run_binding SET run_id='not-a-run-id'")

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect(run_directory, binding)


def test_inspect_rejects_foreign_key_violations_and_nonempty_unimplemented_tables(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    binding = _initialize(run_directory)
    with sqlite3.connect(run_directory / "variables.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO scope_frames VALUES (?, 'orphan', 'missing-parent', "
            "'workflow.orphan', '[]', 'ACTIVE', ?, NULL)",
            (_RUN_ID, "2026-11-19T03:04:05.123456Z"),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall()

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect(run_directory, binding)

    second_directory = tmp_path / "nonempty-state"
    second_directory.mkdir()
    second_binding = _initialize(second_directory)
    with sqlite3.connect(second_directory / "variables.sqlite") as connection:
        connection.execute(
            "INSERT INTO variable_states VALUES (?, ?, 'd1', 'name', 1, 'null', 1, ?, ?)",
            (
                _RUN_ID,
                "workflow-root",
                _HASH_A,
                "2026-11-19T03:04:05.123456Z",
            ),
        )
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _inspect(second_directory, second_binding)


def test_integrity_check_mismatch_is_rejected() -> None:
    class _Rows:
        def fetchall(self) -> list[tuple[str]]:
            return [("row missing from index",)]

    class _Connection:
        def execute(self, statement: str) -> _Rows:
            assert statement == "PRAGMA integrity_check"
            return _Rows()

    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _check_integrity(_Connection())  # type: ignore[arg-type]


def test_scope_frame_foreign_key_parent_and_single_root_constraints(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    _initialize(run_directory)
    with _connect(run_directory) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO scope_frames VALUES (?, 'orphan', 'missing', "
                "'workflow.orphan', '[]', 'ACTIVE', '2026-11-19T00:00:00Z', NULL)",
                (_RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO scope_frames VALUES (?, 'other-root', NULL, "
                "'workflow', '[]', 'ACTIVE', '2026-11-19T00:00:00Z', NULL)",
                (_RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO scope_frames VALUES (?, 'missing-run', NULL, "
                "'workflow', '[]', 'ACTIVE', '2026-11-19T00:00:00Z', NULL)",
                ("run-ffffffffffffffffffffffffffffffff",),
            )


def test_variable_states_present_value_tombstone_and_name_constraints(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    _initialize(run_directory)
    with _connect(run_directory) as connection:
        valid = (
            _RUN_ID,
            "workflow-root",
            "declaration-1",
            "name-1",
            1,
            "null",
            1,
            _HASH_A,
            "2026-11-19T03:04:05.123456Z",
        )
        connection.execute("INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", valid)
        connection.execute(
            "INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _RUN_ID,
                "workflow-root",
                "declaration-2",
                "name-2",
                0,
                None,
                1,
                _HASH_B,
                "2026-11-19T03:04:05.123456Z",
            ),
        )
        for present, value in ((0, "null"), (1, None), (2, "null")):
            invalid = list(valid)
            invalid[2] = f"invalid-{present}-{value}"
            invalid[3] = f"invalid-name-{present}-{value}"
            invalid[4] = present
            invalid[5] = value
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    invalid,
                )
        duplicate_name = list(valid)
        duplicate_name[2] = "declaration-3"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                duplicate_name,
            )
        invalid_version = list(valid)
        invalid_version[2] = "declaration-4"
        invalid_version[3] = "name-4"
        invalid_version[6] = 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                invalid_version,
            )
        invalid_frame = list(valid)
        invalid_frame[1] = "not-a-frame"
        invalid_frame[2] = "declaration-5"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO variable_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                invalid_frame,
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"mutation_ordinal": -1},
        {"operation": "unknown"},
        {"payload_digest": "short"},
        {"before_version": -1},
        {"after_version": 0},
        {"before_version": 0, "before_present": 1, "before_digest": None},
        {"before_version": 0, "before_present": 0, "before_digest": _HASH_A},
        {"before_version": 1, "before_present": 1, "before_digest": None},
        {"before_present": 2},
        {"after_present": 2},
        {"after_digest": "short"},
        {"after_present": 0, "after_value_json": "null"},
        {"after_present": 1, "after_value_json": None},
        {"run_id": "run-ffffffffffffffffffffffffffffffff"},
    ],
)
def test_variable_mutation_checks_and_composite_foreign_key(
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    _initialize(run_directory)
    with _connect(run_directory) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mutation(connection, **overrides)


def test_variable_mutation_execution_and_version_uniqueness(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    _initialize(run_directory)
    with _connect(run_directory) as connection:
        _insert_mutation(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mutation(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mutation(
                connection,
                execution_key="execution-2",
                before_version=2,
                before_present=1,
                before_digest=_HASH_A,
                after_version=1,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_mutation(
                connection,
                execution_key="execution-3",
                before_version=0,
                before_present=0,
                before_digest=None,
                after_version=2,
            )
        _insert_mutation(
            connection,
            execution_key="execution-4",
            declaration_id="declaration-2",
            after_present=0,
            after_value_json=None,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_invalid_and_escaped_run_directories_fail_without_creating_database(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _store(missing).initialize_new_database(_binding())
    assert not missing.exists()

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("owned", encoding="utf-8")
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _store(regular_file).initialize_new_database(_binding())
    assert regular_file.read_text(encoding="utf-8") == "owned"


def test_run_directory_symlink_is_rejected_or_skipped_when_windows_denies_creation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "run-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires SeCreateSymbolicLinkPrivilege")
        raise
    with pytest.raises(StrictWorkflowVariableStorageUnavailable):
        _store(link).initialize_new_database(_binding())


def test_database_symlink_is_rejected_or_skipped_when_windows_denies_creation(
    tmp_path: Path,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    target = run_directory / "owned.sqlite"
    target.write_bytes(b"preserve")
    database = run_directory / "variables.sqlite"
    try:
        database.symlink_to(target)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink creation requires SeCreateSymbolicLinkPrivilege")
        raise
    before = target.read_bytes()
    with pytest.raises(StrictWorkflowVariableConflict):
        _store(run_directory).initialize_new_database(_binding())
    assert target.read_bytes() == before
