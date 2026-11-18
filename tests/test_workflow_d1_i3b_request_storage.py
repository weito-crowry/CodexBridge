from __future__ import annotations

import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from codex_bridge.workflows import service as legacy_service
from codex_bridge.workflows import strict_requests as strict_module
from codex_bridge.workflows.strict_requests import (
    StrictWorkflowRequestConflict,
    StrictWorkflowRequestError,
    StrictWorkflowRequestInput,
    StrictWorkflowRequestStore,
)

_CREATED = "2026-11-19T00:00:00+00:00"
_UPDATED = "2026-11-19T01:00:00+00:00"
_DEFINITION_ID = "11111111-1111-4111-8111-111111111111"
_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"


def _legacy_request_rows() -> tuple[tuple[object, ...], ...]:
    states = ("queued", "running", "succeeded", "failed_to_start")
    return tuple(
        (
            f"legacy-request-{index:02d}",
            str(uuid.UUID(int=index + 1)),
            f"run-{index + 1:032x}",
            f"{index + 1:064x}",
            state,
            _CREATED,
            _UPDATED if index % 2 == 0 else _CREATED,
        )
        for index, state in enumerate(states)
    )


def _create_v0(state_root: Path) -> sqlite3.Connection:
    state_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_root / "requests.sqlite", timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    # Exercise the real legacy initializer without starting WorkflowService.
    legacy_service._initialize_write_connection(connection)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("BEGIN IMMEDIATE")
    rows = _legacy_request_rows()
    connection.executemany(
        "INSERT INTO workflow_requests ("
        "client_request_id, workflow_id, run_id, request_digest, state, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.executemany(
        "INSERT INTO workflow_request_sources ("
        "run_id, definition_id, revision, source_sha256) VALUES (?, ?, ?, ?)",
        (
            (str(row[2]), _DEFINITION_ID, index + 1, f"{index + 1:064x}")
            for index, row in enumerate(rows)
            if index in {0, 2}
        ),
    )
    connection.execute("COMMIT")
    return connection


def _legacy_snapshot(database: Path) -> tuple[tuple[object, ...], tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        requests = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT rowid, client_request_id, workflow_id, run_id, request_digest, "
                "state, created_at, updated_at FROM workflow_requests ORDER BY rowid"
            )
        )
        sources = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT rowid, run_id, definition_id, revision, source_sha256 "
                "FROM workflow_request_sources ORDER BY rowid"
            )
        )
    return requests, sources


def _user_objects(database: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(database) as connection:
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }


def _request(**overrides: object) -> StrictWorkflowRequestInput:
    values: dict[str, object] = {
        "client_request_id": "strict-request-0001",
        "workflow_id": _WORKFLOW_ID,
        "run_id": "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "request_digest": "a" * 64,
        "definition_id": _DEFINITION_ID,
        "definition_revision": 3,
        "source_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "binding_digest": "d" * 64,
        "normalized_input_sha256": "e" * 64,
        "created_at": _CREATED,
    }
    values.update(overrides)
    return StrictWorkflowRequestInput(**values)  # type: ignore[arg-type]


def _counts(state_root: Path) -> tuple[int, int, int]:
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "workflow_requests",
                "workflow_request_sources",
                "workflow_request_contract_bindings",
            )
        )  # type: ignore[return-value]


def _reserve(state_root: Path, request: StrictWorkflowRequestInput | None = None):
    return StrictWorkflowRequestStore(state_root).reserve_strict(request or _request())


def test_read_inspector_does_not_create_missing_database_and_new_v1_is_explicit(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing"
    store = StrictWorkflowRequestStore(missing_root)
    with pytest.raises(StrictWorkflowRequestError):
        store.inspect()
    assert not missing_root.exists()

    state_root = tmp_path / "new-state"
    store = StrictWorkflowRequestStore(state_root)
    store.initialize_new_database()
    info = store.inspect()
    assert (
        info.user_version,
        info.request_count,
        info.source_count,
        info.strict_binding_count,
    ) == (
        1,
        0,
        0,
        0,
    )
    assert _user_objects(state_root / "requests.sqlite") == {
        ("table", "workflow_requests", "workflow_requests"),
        ("table", "workflow_request_sources", "workflow_request_sources"),
        ("index", "workflow_requests_created_idx", "workflow_requests"),
        ("index", "workflow_requests_state_idx", "workflow_requests"),
        (
            "table",
            "workflow_request_contract_bindings",
            "workflow_request_contract_bindings",
        ),
    }


def test_new_v1_initialization_rejects_nonempty_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "not-empty"
    state_root.mkdir()
    marker = state_root / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(state_root).initialize_new_database()
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (state_root / "requests.sqlite").exists()


def test_wal_online_backup_restore_and_v0_migration_preserve_legacy_identity(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "legacy"
    legacy_connection = _create_v0(state_root)
    database = state_root / "requests.sqlite"
    with sqlite3.connect(database) as check:
        assert check.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    wal_path = Path(f"{database}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    before_rows = _legacy_snapshot(database)
    before_objects = _user_objects(database)
    store = StrictWorkflowRequestStore(state_root)

    backup = tmp_path / "legacy-v0.backup"
    store.create_verified_backup(backup)
    assert _legacy_snapshot(backup) == before_rows
    restored_root = tmp_path / "restored"
    store.restore_verified_backup(backup, destination_state_root=restored_root)
    assert _legacy_snapshot(restored_root / "requests.sqlite") == before_rows

    store.migrate_v0_to_v1(backup_path=tmp_path / "migration-v0.backup")
    assert _legacy_snapshot(database) == before_rows
    assert _user_objects(database) == before_objects | {
        (
            "table",
            "workflow_request_contract_bindings",
            "workflow_request_contract_bindings",
        )
    }
    info = store.inspect()
    assert (
        info.user_version,
        info.request_count,
        info.source_count,
        info.strict_binding_count,
    ) == (
        1,
        4,
        2,
        0,
    )
    assert tuple(legacy_connection.execute("PRAGMA user_version").fetchone()) == (1,)
    assert tuple(
        legacy_connection.execute(
            "SELECT COUNT(*) FROM workflow_request_contract_bindings"
        ).fetchone()
    ) == (0,)
    legacy_connection.close()


@pytest.mark.parametrize(
    "tamper",
    [
        "unknown_table",
        "unknown_index",
        "unknown_version",
        "partial_schema",
        "unknown_state",
        "invalid_request_id",
        "invalid_hash",
        "invalid_datetime",
        "orphan_source",
    ],
)
def test_v0_migration_rejects_unknown_schema_or_invalid_rows(tmp_path: Path, tamper: str) -> None:
    state_root = tmp_path / tamper
    connection = _create_v0(state_root)
    if tamper == "unknown_table":
        connection.execute("CREATE TABLE extension_table (value TEXT)")
    elif tamper == "unknown_index":
        connection.execute("CREATE INDEX extension_index ON workflow_requests (created_at)")
    elif tamper == "unknown_version":
        connection.execute("PRAGMA user_version=7")
    elif tamper == "partial_schema":
        connection.execute("DROP INDEX workflow_requests_state_idx")
    elif tamper == "unknown_state":
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE workflow_requests SET state='mystery' WHERE rowid=1")
    elif tamper == "invalid_request_id":
        connection.execute("UPDATE workflow_requests SET client_request_id='bad' WHERE rowid=1")
    elif tamper == "invalid_hash":
        connection.execute(
            "UPDATE workflow_requests SET request_digest=? WHERE rowid=1", ("A" * 64,)
        )
    elif tamper == "invalid_datetime":
        connection.execute("UPDATE workflow_requests SET updated_at='not-a-date' WHERE rowid=1")
    elif tamper == "orphan_source":
        connection.execute(
            "UPDATE workflow_request_sources SET run_id=? WHERE rowid=1",
            ("run-ffffffffffffffffffffffffffffffff",),
        )

    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(state_root).migrate_v0_to_v1(
            backup_path=tmp_path / f"{tamper}.backup"
        )
    assert tuple(connection.execute("PRAGMA user_version").fetchone()) == (
        7 if tamper == "unknown_version" else 0,
    )
    connection.close()


@pytest.mark.parametrize("checkpoint", ["after_create", "after_user_version", "before_commit"])
def test_migration_checkpoint_failure_rolls_back_to_verified_v0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint: str
) -> None:
    state_root = tmp_path / checkpoint
    connection = _create_v0(state_root)
    before_rows = _legacy_snapshot(state_root / "requests.sqlite")

    def fail_at(stage: str) -> None:
        if stage == checkpoint:
            raise RuntimeError("private checkpoint token")

    monkeypatch.setattr(strict_module, "_migration_checkpoint", fail_at)
    with pytest.raises(StrictWorkflowRequestError) as error:
        StrictWorkflowRequestStore(state_root).migrate_v0_to_v1(
            backup_path=tmp_path / f"{checkpoint}.backup"
        )
    assert "private checkpoint token" not in str(error.value)
    assert tuple(connection.execute("PRAGMA user_version").fetchone()) == (0,)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workflow_request_contract_bindings'"
        ).fetchone()
        is None
    )
    assert tuple(connection.execute("PRAGMA integrity_check").fetchone()) == ("ok",)
    assert _legacy_snapshot(state_root / "requests.sqlite") == before_rows
    connection.close()


def test_migration_rejects_live_rows_that_differ_from_online_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "backup-mismatch"
    connection = _create_v0(state_root)
    store = StrictWorkflowRequestStore(state_root)
    create_backup = store.create_verified_backup

    def create_then_change(backup_path: Path) -> None:
        create_backup(backup_path)
        connection.execute(
            "UPDATE workflow_requests SET request_digest=? WHERE rowid=1", ("f" * 64,)
        )

    monkeypatch.setattr(store, "create_verified_backup", create_then_change)
    with pytest.raises(StrictWorkflowRequestError):
        store.migrate_v0_to_v1(backup_path=tmp_path / "backup-mismatch.sqlite")
    assert tuple(connection.execute("PRAGMA user_version").fetchone()) == (0,)
    assert tuple(
        connection.execute("SELECT request_digest FROM workflow_requests WHERE rowid=1").fetchone()
    ) == ("f" * 64,)
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='workflow_request_contract_bindings'"
        ).fetchone()
        is None
    )
    connection.close()


def test_strict_reservation_is_atomic_readable_and_does_not_create_run_artifacts(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    store = StrictWorkflowRequestStore(state_root)
    request = _request()

    reservation = store.reserve_strict(request)
    assert reservation.replayed is False
    assert reservation.state == "queued"
    assert reservation.created_at == _CREATED
    assert store.read_binding(request.run_id) == reservation
    assert store.read_binding("run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb") is None
    assert _counts(state_root) == (1, 1, 1)
    assert not (state_root / request.run_id).exists()
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        parent = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests"
        ).fetchone()
        source = connection.execute(
            "SELECT run_id, definition_id, revision, source_sha256 FROM workflow_request_sources"
        ).fetchone()
        binding = connection.execute(
            "SELECT run_id, workflow_id, definition_id, definition_revision, "
            "run_format_version, contract_version, contract_sha256, binding_digest, "
            "normalized_input_sha256, request_digest, created_at "
            "FROM workflow_request_contract_bindings"
        ).fetchone()
    assert parent == (
        request.client_request_id,
        request.workflow_id,
        request.run_id,
        request.request_digest,
        "queued",
        request.created_at,
        request.created_at,
    )
    assert source == (
        request.run_id,
        request.definition_id,
        request.definition_revision,
        request.source_sha256,
    )
    assert binding == (
        request.run_id,
        request.workflow_id,
        request.definition_id,
        request.definition_revision,
        2,
        1,
        request.contract_sha256,
        request.binding_digest,
        request.normalized_input_sha256,
        request.request_digest,
        request.created_at,
    )


def test_same_request_retry_replays_and_changed_payload_conflicts_without_writes(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    first = _reserve(state_root, request)
    replay = _reserve(
        state_root,
        replace(request, created_at="2026-11-19T02:00:00+00:00"),
    )
    assert replay.replayed is True
    assert replay.run_id == first.run_id
    assert replay.created_at == first.created_at
    assert _counts(state_root) == (1, 1, 1)

    with pytest.raises(StrictWorkflowRequestConflict):
        _reserve(state_root, replace(request, request_digest="f" * 64))
    assert _counts(state_root) == (1, 1, 1)


@pytest.mark.parametrize("collision", ["run_id", "workflow_id"])
def test_run_or_workflow_identity_collision_is_rejected_without_writes(
    tmp_path: Path, collision: str
) -> None:
    state_root = tmp_path / collision
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    _reserve(state_root, request)
    changed: dict[str, object] = {
        "client_request_id": "strict-request-0002",
        "workflow_id": "33333333-3333-4333-8333-333333333333",
        "run_id": "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "request_digest": "f" * 64,
        "created_at": "2026-11-19T02:00:00+00:00",
    }
    if collision == "run_id":
        changed["run_id"] = request.run_id
    else:
        changed["workflow_id"] = request.workflow_id
    with pytest.raises(StrictWorkflowRequestConflict):
        _reserve(state_root, replace(request, **changed))
    assert _counts(state_root) == (1, 1, 1)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("client_request_id", "bad"),
        ("workflow_id", "not-a-uuid"),
        ("run_id", "not-a-run"),
        ("request_digest", "A" * 64),
        ("definition_id", "not-a-uuid"),
        ("definition_revision", True),
        ("source_sha256", "g" * 64),
        ("contract_sha256", "C" * 64),
        ("binding_digest", "D" * 64),
        ("normalized_input_sha256", "E" * 64),
        ("run_format_version", 1),
        ("contract_version", 2),
        ("created_at", "not-a-date"),
    ],
)
def test_strict_reservation_validates_input_types_versions_hashes_and_time(
    tmp_path: Path, field_name: str, value: object
) -> None:
    state_root = tmp_path / field_name
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    with pytest.raises(StrictWorkflowRequestError):
        _reserve(state_root, replace(_request(), **{field_name: value}))
    assert _counts(state_root) == (0, 0, 0)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("workflow_request_contract_bindings", "contract_sha256", "A" * 64),
        ("workflow_request_contract_bindings", "binding_digest", "B" * 64),
        (
            "workflow_request_contract_bindings",
            "normalized_input_sha256",
            "C" * 64,
        ),
        ("workflow_request_contract_bindings", "definition_revision", 4),
        (
            "workflow_request_contract_bindings",
            "workflow_id",
            "33333333-3333-4333-8333-333333333333",
        ),
        ("workflow_requests", "request_digest", "f" * 64),
        ("workflow_request_sources", "source_sha256", "D" * 64),
    ],
)
def test_v1_read_verifier_rejects_binding_hash_and_identity_tampering(
    tmp_path: Path, table: str, column: str, value: object
) -> None:
    state_root = tmp_path / f"{table}-{column}"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    _reserve(state_root, request)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(state_root).read_binding(request.run_id)


def test_v1_rejects_missing_binding_table_and_missing_binding_for_strict_read(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "missing-binding"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    _reserve(state_root, request)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute("DROP TABLE workflow_request_contract_bindings")
    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(state_root).inspect()

    other_root = tmp_path / "missing-row"
    StrictWorkflowRequestStore(other_root).initialize_new_database()
    _reserve(other_root, request)
    with sqlite3.connect(other_root / "requests.sqlite") as connection:
        connection.execute(
            "DELETE FROM workflow_request_contract_bindings WHERE run_id=?",
            (request.run_id,),
        )
    other_store = StrictWorkflowRequestStore(other_root)
    # Legacy request rows remain valid without bindings; an explicit strict read
    # of such a row fails closed rather than treating it as a strict reservation.
    assert other_store.inspect().strict_binding_count == 0
    with pytest.raises(StrictWorkflowRequestError):
        other_store.read_binding(request.run_id)


def test_v1_rejects_missing_source_and_foreign_key_orphan(tmp_path: Path) -> None:
    state_root = tmp_path / "missing-source"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    _reserve(state_root, request)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute("DELETE FROM workflow_request_sources WHERE run_id=?", (request.run_id,))
    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(state_root).read_binding(request.run_id)

    orphan_root = tmp_path / "orphan-binding"
    StrictWorkflowRequestStore(orphan_root).initialize_new_database()
    with sqlite3.connect(orphan_root / "requests.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO workflow_request_contract_bindings ("
            "run_id, workflow_id, definition_id, definition_revision, "
            "run_format_version, contract_version, contract_sha256, "
            "binding_digest, normalized_input_sha256, request_digest, created_at) "
            "VALUES (?, ?, ?, ?, 2, 1, ?, ?, ?, ?, ?)",
            (
                _request().run_id,
                _request().workflow_id,
                _request().definition_id,
                1,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "a" * 64,
                _CREATED,
            ),
        )
    with pytest.raises(StrictWorkflowRequestError):
        StrictWorkflowRequestStore(orphan_root).inspect()


def test_binding_foreign_key_rejects_orphan_insert(tmp_path: Path) -> None:
    state_root = tmp_path / "foreign-key"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO workflow_request_contract_bindings ("
                "run_id, workflow_id, definition_id, definition_revision, "
                "run_format_version, contract_version, contract_sha256, "
                "binding_digest, normalized_input_sha256, request_digest, created_at) "
                "VALUES (?, ?, ?, ?, 2, 1, ?, ?, ?, ?, ?)",
                (
                    _request().run_id,
                    _request().workflow_id,
                    _request().definition_id,
                    1,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    "a" * 64,
                    _CREATED,
                ),
            )
    assert _counts(state_root) == (0, 0, 0)


def test_two_threads_reserving_same_request_commit_once(tmp_path: Path) -> None:
    state_root = tmp_path / "threaded"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request()
    barrier = Barrier(2)

    def reserve_after_barrier() -> bool:
        barrier.wait(timeout=10)
        return StrictWorkflowRequestStore(state_root).reserve_strict(request).replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve_after_barrier(), range(2)))
    assert sorted(results) == [False, True]
    assert _counts(state_root) == (1, 1, 1)


def test_safe_errors_and_repr_do_not_expose_request_or_binding_values(tmp_path: Path) -> None:
    state_root = tmp_path / "redacted"
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    request = _request(
        client_request_id="sensitive-request-marker",
        binding_digest="d" * 64,
    )
    assert "sensitive-request-marker" not in repr(request)
    _reserve(state_root, request)
    with pytest.raises(StrictWorkflowRequestConflict) as error:
        _reserve(state_root, replace(request, request_digest="f" * 64))
    for private_value in (
        "sensitive-request-marker",
        "d" * 64,
        "f" * 64,
        "private checkpoint token",
    ):
        assert private_value not in str(error.value)
        assert private_value not in repr(error.value)
