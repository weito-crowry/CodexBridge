"""Opt-in, isolated requests.sqlite v1 storage for strict workflow requests.

The legacy workflow runtime does not import or construct this store. Callers
must explicitly initialize a new v1 database or migrate a verified v0 database
before reading or reserving strict requests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

_DATABASE_NAME = "requests.sqlite"
_BUSY_TIMEOUT_MS = 10_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_WORKFLOW_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
    }
)
_UNAVAILABLE = "strict workflow request storage is unavailable"
_INVALID_REQUEST = "strict workflow request is invalid"
_CONFLICT = "strict workflow request conflicts with an existing reservation"

_CREATE_REQUESTS = """
CREATE TABLE workflow_requests (
    client_request_id TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'queued','starting','running','succeeded','failed',
            'failed_to_start','needs_reconciliation'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_CREATE_CREATED_INDEX = (
    "CREATE INDEX workflow_requests_created_idx "
    "ON workflow_requests(created_at DESC, workflow_id DESC)"
)
_CREATE_STATE_INDEX = "CREATE INDEX workflow_requests_state_idx ON workflow_requests(state)"
_CREATE_SOURCES = """
CREATE TABLE workflow_request_sources (
    run_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    source_sha256 TEXT NOT NULL
)
"""
_CREATE_BINDINGS = """
CREATE TABLE workflow_request_contract_bindings (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_revision INTEGER NOT NULL CHECK (definition_revision >= 1),
    run_format_version INTEGER NOT NULL CHECK (run_format_version = 2),
    contract_version INTEGER NOT NULL CHECK (contract_version = 1),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    normalized_input_sha256 TEXT NOT NULL
        CHECK (length(normalized_input_sha256) = 64),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id)
        REFERENCES workflow_requests (run_id)
        ON DELETE RESTRICT
)
"""


class StrictWorkflowRequestError(RuntimeError):
    """A safe, value-free strict request storage error."""


class StrictWorkflowRequestConflict(StrictWorkflowRequestError):
    """A safe conflict for a reused request or reserved identifier."""


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowRequestInput:
    client_request_id: str = field(repr=False)
    workflow_id: str = field(repr=False)
    run_id: str = field(repr=False)
    request_digest: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    source_sha256: str = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    created_at: str = field(repr=False)
    run_format_version: int = field(default=2, repr=False)
    contract_version: int = field(default=1, repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowRequestInput(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowRequestReservation:
    client_request_id: str = field(repr=False)
    workflow_id: str = field(repr=False)
    run_id: str = field(repr=False)
    request_digest: str = field(repr=False)
    state: str = field(repr=False)
    created_at: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    source_sha256: str = field(repr=False)
    run_format_version: int = field(repr=False)
    contract_version: int = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    replayed: bool = field(default=False, repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowRequestReservation(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowRequestDatabaseInfo:
    user_version: int = field(repr=False)
    request_count: int = field(repr=False)
    source_count: int = field(repr=False)
    strict_binding_count: int = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowRequestDatabaseInfo(<verified>)"


@dataclass(frozen=True, slots=True)
class _LegacyIdentity:
    digest: str
    request_count: int
    source_count: int


class StrictWorkflowRequestStore:
    """Internal opt-in storage for an explicitly prepared requests v1 DB."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = Path(state_root)

    def initialize_new_database(self) -> None:
        """Create a new v1 database only under an empty state root."""
        root = self._resolve_state_root(create=True)
        if any(root.iterdir()):
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        database = root / _DATABASE_NAME
        connection: sqlite3.Connection | None = None
        created_by_call = False
        initialized = False
        try:
            with database.open("xb"):
                pass
            created_by_call = True
            connection = sqlite3.connect(
                database,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            _configure_write_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            _expect_empty_database(connection)
            _create_v1_schema(connection)
            connection.execute("PRAGMA user_version=1")
            _check_v1_database(connection)
            connection.execute("COMMIT")
            initialized = True
        except StrictWorkflowRequestError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()
            if created_by_call and not initialized:
                _remove_database_files(database)

    def create_verified_backup(self, backup_path: Path) -> None:
        """Create and verify an online backup of an existing legacy v0 DB."""
        database = self._database_path(required=True)
        backup = _new_artifact_path(backup_path, database)
        created_by_call = False
        try:
            with _read_connection(database) as source:
                source.execute("BEGIN")
                _check_v0_database(source)
                source_identity = _legacy_identity(source)
                with backup.open("xb"):
                    pass
                created_by_call = True
                destination = sqlite3.connect(backup, isolation_level=None)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            with _read_connection(backup) as restored:
                restored.execute("BEGIN")
                _check_v0_database(restored)
                if _legacy_identity(restored) != source_identity:
                    raise StrictWorkflowRequestError(_UNAVAILABLE)
        except StrictWorkflowRequestError:
            if created_by_call:
                _remove_database_files(backup)
            raise
        except Exception:
            if created_by_call:
                _remove_database_files(backup)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None

    def restore_verified_backup(
        self,
        backup_path: Path,
        *,
        destination_state_root: Path,
    ) -> None:
        """Restore a verified v0 backup into a new, empty isolated state root."""
        backup = Path(backup_path)
        destination_root = Path(destination_state_root)
        if backup.is_symlink() or not backup.is_file():
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        destination = StrictWorkflowRequestStore(destination_root)
        root = destination._resolve_state_root(create=True)
        if any(root.iterdir()):
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        database = root / _DATABASE_NAME
        created_by_call = False
        try:
            with _read_connection(backup) as source:
                source.execute("BEGIN")
                _check_v0_database(source)
                identity = _legacy_identity(source)
                with database.open("xb"):
                    pass
                created_by_call = True
                target = sqlite3.connect(database, isolation_level=None)
                try:
                    source.backup(target)
                finally:
                    target.close()
            with _read_connection(database) as restored:
                restored.execute("BEGIN")
                _check_v0_database(restored)
                if _legacy_identity(restored) != identity:
                    raise StrictWorkflowRequestError(_UNAVAILABLE)
        except StrictWorkflowRequestError:
            if created_by_call:
                _remove_database_files(database)
            raise
        except Exception:
            if created_by_call:
                _remove_database_files(database)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None

    def migrate_v0_to_v1(self, *, backup_path: Path) -> None:
        """Verify, back up, and explicitly migrate a legacy v0 DB to v1."""
        database = self._database_path(required=True)
        self.create_verified_backup(backup_path)
        backup = Path(backup_path)
        connection: sqlite3.Connection | None = None
        try:
            with _read_connection(backup) as backup_connection:
                backup_connection.execute("BEGIN")
                _check_v0_database(backup_connection)
                backup_identity = _legacy_identity(backup_connection)

            connection = _connect_write(database)
            connection.execute("BEGIN IMMEDIATE")
            _check_v0_database(connection)
            before = _legacy_identity(connection)
            if before != backup_identity:
                raise StrictWorkflowRequestError(_UNAVAILABLE)
            connection.execute(_CREATE_BINDINGS)
            _migration_checkpoint("after_create")
            connection.execute("PRAGMA user_version=1")
            _migration_checkpoint("after_user_version")
            _check_v1_database(connection)
            after = _legacy_identity(connection)
            if after != before:
                raise StrictWorkflowRequestError(_UNAVAILABLE)
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_request_contract_bindings"
                ).fetchone()[0]
                != 0
            ):
                raise StrictWorkflowRequestError(_UNAVAILABLE)
            _migration_checkpoint("before_commit")
            connection.execute("COMMIT")
        except StrictWorkflowRequestError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()

        with _read_connection(database) as read_connection:
            read_connection.execute("BEGIN")
            _check_v1_database(read_connection)
            if _legacy_identity(read_connection) != backup_identity:
                raise StrictWorkflowRequestError(_UNAVAILABLE)

    def inspect(self) -> StrictWorkflowRequestDatabaseInfo:
        """Read and validate an existing v1 database without creating files."""
        database = self._database_path(required=True)
        try:
            with _read_connection(database) as connection:
                connection.execute("BEGIN")
                _check_v1_database(connection)
                return StrictWorkflowRequestDatabaseInfo(
                    user_version=1,
                    request_count=_table_count(connection, "workflow_requests"),
                    source_count=_table_count(connection, "workflow_request_sources"),
                    strict_binding_count=_table_count(
                        connection, "workflow_request_contract_bindings"
                    ),
                )
        except StrictWorkflowRequestError:
            raise
        except Exception:
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None

    def reserve_strict(
        self, request: StrictWorkflowRequestInput
    ) -> StrictWorkflowRequestReservation:
        """Atomically reserve request, saved source, and strict binding rows."""
        _validate_input(request)
        database = self._database_path(required=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_write(database)
            connection.execute("BEGIN IMMEDIATE")
            _check_v1_database(connection)
            existing = connection.execute(
                "SELECT * FROM workflow_requests WHERE client_request_id = ?",
                (request.client_request_id,),
            ).fetchone()
            if existing is not None:
                reservation = _reservation_for_run(connection, str(existing["run_id"]))
                if reservation is None or not _same_request(request, reservation):
                    raise StrictWorkflowRequestConflict(_CONFLICT)
                connection.execute("COMMIT")
                return replace(reservation, replayed=True)

            collision = connection.execute(
                "SELECT 1 FROM workflow_requests WHERE workflow_id = ? OR run_id = ? LIMIT 1",
                (request.workflow_id, request.run_id),
            ).fetchone()
            if collision is not None:
                raise StrictWorkflowRequestConflict(_CONFLICT)

            connection.execute(
                "INSERT INTO workflow_requests ("
                "client_request_id, workflow_id, run_id, request_digest, "
                "state, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (
                    request.client_request_id,
                    request.workflow_id,
                    request.run_id,
                    request.request_digest,
                    request.created_at,
                    request.created_at,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_request_sources ("
                "run_id, definition_id, revision, source_sha256) VALUES (?, ?, ?, ?)",
                (
                    request.run_id,
                    request.definition_id,
                    request.definition_revision,
                    request.source_sha256,
                ),
            )
            connection.execute(
                "INSERT INTO workflow_request_contract_bindings ("
                "run_id, workflow_id, definition_id, definition_revision, "
                "run_format_version, contract_version, contract_sha256, "
                "binding_digest, normalized_input_sha256, request_digest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request.run_id,
                    request.workflow_id,
                    request.definition_id,
                    request.definition_revision,
                    request.run_format_version,
                    request.contract_version,
                    request.contract_sha256,
                    request.binding_digest,
                    request.normalized_input_sha256,
                    request.request_digest,
                    request.created_at,
                ),
            )
            _check_v1_database(connection)
            result = _reservation_for_run(connection, request.run_id)
            if result is None:
                raise StrictWorkflowRequestError(_UNAVAILABLE)
            connection.execute("COMMIT")
            return result
        except StrictWorkflowRequestError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except sqlite3.Error:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()

    def read_binding(self, run_id: str) -> StrictWorkflowRequestReservation | None:
        """Verify a strict reservation; return None only when the run is absent.

        Existing legacy rows remain unbound. An explicit strict read of one
        fails closed because it has no strict binding row.
        """
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise StrictWorkflowRequestError(_INVALID_REQUEST)
        database = self._database_path(required=True)
        try:
            with _read_connection(database) as connection:
                connection.execute("BEGIN")
                _check_v1_database(connection)
                request = connection.execute(
                    "SELECT 1 FROM workflow_requests WHERE run_id = ?", (run_id,)
                ).fetchone()
                if request is None:
                    return None
                reservation = _reservation_for_run(connection, run_id)
                if reservation is None:
                    raise StrictWorkflowRequestError(_UNAVAILABLE)
                return reservation
        except StrictWorkflowRequestError:
            raise
        except Exception:
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None

    def _resolve_state_root(self, *, create: bool) -> Path:
        candidate = self._state_root.expanduser()
        if candidate.is_symlink():
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        try:
            if create:
                candidate.mkdir(parents=True, exist_ok=True)
            if not candidate.is_dir():
                raise StrictWorkflowRequestError(_UNAVAILABLE)
            root = candidate.resolve(strict=True)
        except StrictWorkflowRequestError:
            raise
        except OSError:
            raise StrictWorkflowRequestError(_UNAVAILABLE) from None
        return root

    def _database_path(self, *, required: bool) -> Path:
        root = self._resolve_state_root(create=False)
        database = root / _DATABASE_NAME
        if database.is_symlink():
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        if required and not database.is_file():
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        return database


def _configure_write_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    mode = connection.execute("PRAGMA journal_mode").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


def _connect_write(database: Path) -> sqlite3.Connection:
    if database.is_symlink() or not database.is_file():
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    connection: sqlite3.Connection | None = None
    try:
        uri = database.resolve(strict=True).as_uri() + "?mode=rw"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        _configure_write_connection(connection)
        return connection
    except StrictWorkflowRequestError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise StrictWorkflowRequestError(_UNAVAILABLE) from None


def _read_connection(database: Path) -> sqlite3.Connection:
    if database.is_symlink() or not database.is_file():
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    connection: sqlite3.Connection | None = None
    try:
        uri = database.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise StrictWorkflowRequestError(_UNAVAILABLE) from None


def _check_v0_database(connection: sqlite3.Connection) -> None:
    _check_schema(connection, version=0, include_bindings=False)
    _check_integrity(connection)
    _legacy_identity(connection)


def _check_v1_database(connection: sqlite3.Connection) -> None:
    _check_schema(connection, version=1, include_bindings=True)
    _check_integrity(connection)
    _validate_legacy_rows(connection)
    _validate_bindings(connection)


def _check_schema(connection: sqlite3.Connection, *, version: int, include_bindings: bool) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None or type(row[0]) is not int or row[0] != version:
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    expected = _expected_objects(include_bindings=include_bindings)
    actual = {
        (str(obj["type"]), str(obj["name"]), str(obj["tbl_name"]), _normalize_sql(obj["sql"]))
        for obj in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    }
    if actual != expected:
        raise StrictWorkflowRequestError(_UNAVAILABLE)

    expected_info = _expected_table_info(include_bindings=include_bindings)
    for table_name, expected_rows in expected_info.items():
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        actual_rows = tuple(
            (
                row["cid"],
                row["name"],
                row["type"],
                row["notnull"],
                row["dflt_value"],
                row["pk"],
            )
            for row in rows
        )
        if actual_rows != expected_rows:
            raise StrictWorkflowRequestError(_UNAVAILABLE)

    _check_foreign_keys(connection, "workflow_requests", ())
    _check_foreign_keys(connection, "workflow_request_sources", ())
    if include_bindings:
        _check_foreign_keys(
            connection,
            "workflow_request_contract_bindings",
            ((0, 0, "workflow_requests", "run_id", "run_id", "NO ACTION", "RESTRICT", "NONE"),),
        )
    _check_indexes(connection, "workflow_requests", _REQUEST_INDEXES)
    _check_indexes(connection, "workflow_request_sources", _SOURCE_INDEXES)
    if include_bindings:
        _check_indexes(connection, "workflow_request_contract_bindings", _BINDING_INDEXES)


def _expected_objects(*, include_bindings: bool) -> set[tuple[str, str, str, str | None]]:
    statements = [
        ("table", "workflow_requests", "workflow_requests", _CREATE_REQUESTS),
        (
            "index",
            "workflow_requests_created_idx",
            "workflow_requests",
            _CREATE_CREATED_INDEX,
        ),
        (
            "index",
            "workflow_requests_state_idx",
            "workflow_requests",
            _CREATE_STATE_INDEX,
        ),
        ("table", "workflow_request_sources", "workflow_request_sources", _CREATE_SOURCES),
    ]
    if include_bindings:
        statements.append(
            (
                "table",
                "workflow_request_contract_bindings",
                "workflow_request_contract_bindings",
                _CREATE_BINDINGS,
            )
        )
    return {
        (kind, name, table_name, _normalize_sql(sql)) for kind, name, table_name, sql in statements
    }


def _expected_table_info(*, include_bindings: bool) -> dict[str, tuple[tuple[object, ...], ...]]:
    expected: dict[str, tuple[tuple[object, ...], ...]] = {
        "workflow_requests": (
            (0, "client_request_id", "TEXT", 1, None, 0),
            (1, "workflow_id", "TEXT", 1, None, 0),
            (2, "run_id", "TEXT", 1, None, 0),
            (3, "request_digest", "TEXT", 1, None, 0),
            (4, "state", "TEXT", 1, None, 0),
            (5, "created_at", "TEXT", 1, None, 0),
            (6, "updated_at", "TEXT", 1, None, 0),
        ),
        "workflow_request_sources": (
            (0, "run_id", "TEXT", 0, None, 1),
            (1, "definition_id", "TEXT", 1, None, 0),
            (2, "revision", "INTEGER", 1, None, 0),
            (3, "source_sha256", "TEXT", 1, None, 0),
        ),
    }
    if include_bindings:
        expected["workflow_request_contract_bindings"] = (
            (0, "run_id", "TEXT", 0, None, 1),
            (1, "workflow_id", "TEXT", 1, None, 0),
            (2, "definition_id", "TEXT", 1, None, 0),
            (3, "definition_revision", "INTEGER", 1, None, 0),
            (4, "run_format_version", "INTEGER", 1, None, 0),
            (5, "contract_version", "INTEGER", 1, None, 0),
            (6, "contract_sha256", "TEXT", 1, None, 0),
            (7, "binding_digest", "TEXT", 1, None, 0),
            (8, "normalized_input_sha256", "TEXT", 1, None, 0),
            (9, "request_digest", "TEXT", 1, None, 0),
            (10, "created_at", "TEXT", 1, None, 0),
        )
    return expected


_REQUEST_INDEXES = frozenset(
    {
        (1, "u", 0, ("client_request_id",)),
        (1, "u", 0, ("workflow_id",)),
        (1, "u", 0, ("run_id",)),
        (0, "c", 0, ("created_at", "workflow_id")),
        (0, "c", 0, ("state",)),
    }
)
_SOURCE_INDEXES = frozenset({(1, "pk", 0, ("run_id",))})
_BINDING_INDEXES = frozenset({(1, "pk", 0, ("run_id",))})


def _check_indexes(
    connection: sqlite3.Connection,
    table_name: str,
    expected: frozenset[tuple[int, str, int, tuple[str, ...]]],
) -> None:
    actual: set[tuple[int, str, int, tuple[str, ...]]] = set()
    for row in connection.execute(f"PRAGMA index_list({table_name})").fetchall():
        name = str(row["name"]).replace('"', '""')
        columns = connection.execute(f'PRAGMA index_info("{name}")').fetchall()
        actual.add(
            (
                int(row["unique"]),
                str(row["origin"]),
                int(row["partial"]),
                tuple(str(column["name"]) for column in columns),
            )
        )
    if actual != expected:
        raise StrictWorkflowRequestError(_UNAVAILABLE)


def _check_foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    actual = tuple(
        (
            row["id"],
            row["seq"],
            row["table"],
            row["from"],
            row["to"],
            row["on_update"],
            row["on_delete"],
            row["match"],
        )
        for row in connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    )
    if actual != expected:
        raise StrictWorkflowRequestError(_UNAVAILABLE)


def _check_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchall()
    if len(result) != 1 or result[0][0] != "ok":
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise StrictWorkflowRequestError(_UNAVAILABLE)


def _legacy_identity(connection: sqlite3.Connection) -> _LegacyIdentity:
    requests, sources = _validate_legacy_rows(connection)
    digest = hashlib.sha256()
    for table_name, rows in (
        ("workflow_requests", requests),
        ("workflow_request_sources", sources),
    ):
        digest.update(table_name.encode("ascii"))
        digest.update(b"\0")
        for row in rows:
            payload = json.dumps(
                [row["rowid"], *[row[name] for name in row.keys() if name != "rowid"]],
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
    return _LegacyIdentity(digest.hexdigest(), len(requests), len(sources))


def _validate_legacy_rows(
    connection: sqlite3.Connection,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    request_rows = connection.execute(
        "SELECT rowid, client_request_id, workflow_id, run_id, request_digest, state, "
        "created_at, updated_at FROM workflow_requests ORDER BY rowid"
    ).fetchall()
    requests_by_run: dict[str, sqlite3.Row] = {}
    for row in request_rows:
        client_request_id = row["client_request_id"]
        workflow_id = row["workflow_id"]
        run_id = row["run_id"]
        request_digest = row["request_digest"]
        state = row["state"]
        if (
            not isinstance(client_request_id, str)
            or _REQUEST_ID.fullmatch(client_request_id) is None
            or not _is_canonical_uuid(workflow_id)
            or not isinstance(run_id, str)
            or _RUN_ID.fullmatch(run_id) is None
            or not _is_sha256(request_digest)
            or not isinstance(state, str)
            or state not in _WORKFLOW_STATES
        ):
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        created = _parse_utc_timestamp(row["created_at"])
        updated = _parse_utc_timestamp(row["updated_at"])
        if updated < created or run_id in requests_by_run:
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        requests_by_run[run_id] = row

    source_rows = connection.execute(
        "SELECT rowid, run_id, definition_id, revision, source_sha256 "
        "FROM workflow_request_sources ORDER BY rowid"
    ).fetchall()
    for row in source_rows:
        run_id = row["run_id"]
        revision = row["revision"]
        if (
            not isinstance(run_id, str)
            or _RUN_ID.fullmatch(run_id) is None
            or run_id not in requests_by_run
            or not _is_canonical_uuid(row["definition_id"])
            or type(revision) is not int
            or revision < 1
            or not _is_sha256(row["source_sha256"])
        ):
            raise StrictWorkflowRequestError(_UNAVAILABLE)
    return request_rows, source_rows


def _validate_bindings(connection: sqlite3.Connection) -> None:
    requests = {
        str(row["run_id"]): row
        for row in connection.execute(
            "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
            "created_at, updated_at FROM workflow_requests"
        ).fetchall()
    }
    sources = {
        str(row["run_id"]): row
        for row in connection.execute(
            "SELECT run_id, definition_id, revision, source_sha256 FROM workflow_request_sources"
        ).fetchall()
    }
    binding_rows = connection.execute(
        "SELECT run_id, workflow_id, definition_id, definition_revision, "
        "run_format_version, contract_version, contract_sha256, binding_digest, "
        "normalized_input_sha256, request_digest, created_at "
        "FROM workflow_request_contract_bindings"
    ).fetchall()
    for binding in binding_rows:
        run_id = binding["run_id"]
        request = requests.get(str(run_id)) if isinstance(run_id, str) else None
        source = sources.get(str(run_id)) if isinstance(run_id, str) else None
        revision = binding["definition_revision"]
        if (
            not isinstance(run_id, str)
            or _RUN_ID.fullmatch(run_id) is None
            or request is None
            or source is None
            or not _is_canonical_uuid(binding["workflow_id"])
            or binding["workflow_id"] != request["workflow_id"]
            or not _is_canonical_uuid(binding["definition_id"])
            or binding["definition_id"] != source["definition_id"]
            or type(revision) is not int
            or revision < 1
            or revision != source["revision"]
            or type(binding["run_format_version"]) is not int
            or binding["run_format_version"] != 2
            or type(binding["contract_version"]) is not int
            or binding["contract_version"] != 1
            or not _is_sha256(binding["contract_sha256"])
            or not _is_sha256(binding["binding_digest"])
            or not _is_sha256(binding["normalized_input_sha256"])
            or not _is_sha256(binding["request_digest"])
            or binding["request_digest"] != request["request_digest"]
            or not isinstance(binding["created_at"], str)
            or binding["created_at"] != request["created_at"]
        ):
            raise StrictWorkflowRequestError(_UNAVAILABLE)
        _parse_utc_timestamp(binding["created_at"])


def _reservation_for_run(
    connection: sqlite3.Connection, run_id: str
) -> StrictWorkflowRequestReservation | None:
    request = connection.execute(
        "SELECT client_request_id, workflow_id, run_id, request_digest, state, "
        "created_at FROM workflow_requests WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if request is None:
        return None
    binding = connection.execute(
        "SELECT * FROM workflow_request_contract_bindings WHERE run_id = ?", (run_id,)
    ).fetchone()
    if binding is None:
        return None
    source = connection.execute(
        "SELECT definition_id, revision, source_sha256 "
        "FROM workflow_request_sources WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if source is None:
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    return StrictWorkflowRequestReservation(
        client_request_id=str(request["client_request_id"]),
        workflow_id=str(request["workflow_id"]),
        run_id=str(request["run_id"]),
        request_digest=str(request["request_digest"]),
        state=str(request["state"]),
        created_at=str(request["created_at"]),
        definition_id=str(source["definition_id"]),
        definition_revision=int(source["revision"]),
        source_sha256=str(source["source_sha256"]),
        run_format_version=int(binding["run_format_version"]),
        contract_version=int(binding["contract_version"]),
        contract_sha256=str(binding["contract_sha256"]),
        binding_digest=str(binding["binding_digest"]),
        normalized_input_sha256=str(binding["normalized_input_sha256"]),
    )


def _validate_input(request: StrictWorkflowRequestInput) -> None:
    if not isinstance(request, StrictWorkflowRequestInput):
        raise StrictWorkflowRequestError(_INVALID_REQUEST)
    if (
        not isinstance(request.client_request_id, str)
        or _REQUEST_ID.fullmatch(request.client_request_id) is None
        or not _is_canonical_uuid(request.workflow_id)
        or not isinstance(request.run_id, str)
        or _RUN_ID.fullmatch(request.run_id) is None
        or not _is_sha256(request.request_digest)
        or not _is_canonical_uuid(request.definition_id)
        or type(request.definition_revision) is not int
        or request.definition_revision < 1
        or not _is_sha256(request.source_sha256)
        or not _is_sha256(request.contract_sha256)
        or not _is_sha256(request.binding_digest)
        or not _is_sha256(request.normalized_input_sha256)
        or type(request.run_format_version) is not int
        or request.run_format_version != 2
        or type(request.contract_version) is not int
        or request.contract_version != 1
    ):
        raise StrictWorkflowRequestError(_INVALID_REQUEST)
    _parse_utc_timestamp(request.created_at)


def _same_request(
    request: StrictWorkflowRequestInput,
    existing: StrictWorkflowRequestReservation,
) -> bool:
    return (
        request.client_request_id == existing.client_request_id
        and request.workflow_id == existing.workflow_id
        and request.run_id == existing.run_id
        and request.request_digest == existing.request_digest
        and request.definition_id == existing.definition_id
        and request.definition_revision == existing.definition_revision
        and request.source_sha256 == existing.source_sha256
        and request.run_format_version == existing.run_format_version
        and request.contract_version == existing.contract_version
        and request.contract_sha256 == existing.contract_sha256
        and request.binding_digest == existing.binding_digest
        and request.normalized_input_sha256 == existing.normalized_input_sha256
    )


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError):
        raise StrictWorkflowRequestError(_UNAVAILABLE) from None
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    return parsed.astimezone(UTC)


def _normalize_sql(sql: object) -> str | None:
    if not isinstance(sql, str):
        return None
    return re.sub(r"\s+", "", sql).casefold()


def _expect_empty_database(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()
    object_row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if version is None or version[0] != 0 or object_row is not None:
        raise StrictWorkflowRequestError(_UNAVAILABLE)


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    for statement in (
        _CREATE_REQUESTS,
        _CREATE_CREATED_INDEX,
        _CREATE_STATE_INDEX,
        _CREATE_SOURCES,
        _CREATE_BINDINGS,
    ):
        connection.execute(statement)


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _new_artifact_path(backup_path: Path, database: Path) -> Path:
    backup = Path(backup_path).expanduser()
    if backup == database or backup.is_symlink() or backup.exists():
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    try:
        parent = backup.parent.resolve(strict=True)
    except OSError:
        raise StrictWorkflowRequestError(_UNAVAILABLE) from None
    candidate = parent / backup.name
    if candidate == database or candidate.exists() or candidate.is_symlink():
        raise StrictWorkflowRequestError(_UNAVAILABLE)
    for suffix in ("-wal", "-shm"):
        if Path(f"{candidate}{suffix}").exists():
            raise StrictWorkflowRequestError(_UNAVAILABLE)
    return candidate


def _migration_checkpoint(stage: str) -> None:
    """Private test checkpoint; production callers cannot configure callbacks."""


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _remove_database_files(database: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass
