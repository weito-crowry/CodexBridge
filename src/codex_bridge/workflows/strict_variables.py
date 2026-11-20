"""Opt-in run-local variables.sqlite v1 schema and initial root binding.

This store is intentionally not connected to the workflow runtime. The caller
supplies identity metadata; this module does not prove it against definitions,
requests, snapshots, or manifests.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

_DATABASE_NAME = "variables.sqlite"
_BUSY_TIMEOUT_MS = 10_000
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ROOT_FRAME_ID = "workflow-root"
_ROOT_SCOPE_PATH = "workflow"
_ROOT_ITERATION_VECTOR = "[]"
_UNAVAILABLE = "strict workflow variable storage is unavailable"
_INVALID_METADATA = "strict workflow variable metadata is invalid"
_CONFLICT = "strict workflow variable storage conflicts with existing state"

_CREATE_RUN_BINDING = """
CREATE TABLE run_binding (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    definition_revision INTEGER NOT NULL CHECK (definition_revision > 0),
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    normalized_input_sha256 TEXT NOT NULL
        CHECK (length(normalized_input_sha256) = 64),
    run_format_version INTEGER NOT NULL CHECK (run_format_version = 2),
    store_schema_version INTEGER NOT NULL CHECK (store_schema_version = 1),
    status TEXT NOT NULL CHECK (status = 'INITIALIZED'),
    created_at TEXT NOT NULL
)
"""
_CREATE_SCOPE_FRAMES = """
CREATE TABLE scope_frames (
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    parent_frame_id TEXT,
    scope_path TEXT NOT NULL,
    iteration_vector_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (run_id, frame_id),
    FOREIGN KEY (run_id)
        REFERENCES run_binding (run_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (run_id, parent_frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT
)
"""
_CREATE_SCOPE_ROOT_INDEX = """
CREATE UNIQUE INDEX uq_scope_frames_one_root
    ON scope_frames (run_id)
    WHERE parent_frame_id IS NULL
"""
_CREATE_SCOPE_PARENT_INDEX = """
CREATE INDEX idx_scope_frames_parent
    ON scope_frames (run_id, parent_frame_id)
"""
_CREATE_VARIABLE_STATES = """
CREATE TABLE variable_states (
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    declaration_id TEXT NOT NULL,
    name TEXT NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    value_json TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    value_digest TEXT NOT NULL CHECK (length(value_digest) = 64),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, frame_id, declaration_id),
    FOREIGN KEY (run_id, frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT,
    CHECK (
        (present = 0 AND value_json IS NULL)
        OR (present = 1 AND value_json IS NOT NULL)
    )
)
"""
_CREATE_VARIABLE_STATES_NAME_INDEX = """
CREATE UNIQUE INDEX uq_variable_states_scope_name
    ON variable_states (run_id, frame_id, name)
"""
_CREATE_VARIABLE_STATES_FRAME_INDEX = """
CREATE INDEX idx_variable_states_frame
    ON variable_states (run_id, frame_id, declaration_id)
"""
_CREATE_VARIABLE_MUTATIONS = """
CREATE TABLE variable_mutations (
    execution_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    frame_id TEXT NOT NULL,
    declaration_id TEXT NOT NULL,
    mutation_ordinal INTEGER NOT NULL CHECK (mutation_ordinal >= 0),
    operation TEXT NOT NULL CHECK (
        operation IN (
            'initialize',
            'assign',
            'increment',
            'decrement',
            'append',
            'object_set',
            'object_delete',
            'variable_delete'
        )
    ),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    before_version INTEGER NOT NULL CHECK (before_version >= 0),
    after_version INTEGER NOT NULL CHECK (after_version > 0),
    before_present INTEGER NOT NULL CHECK (before_present IN (0, 1)),
    after_present INTEGER NOT NULL CHECK (after_present IN (0, 1)),
    before_digest TEXT,
    after_digest TEXT NOT NULL CHECK (length(after_digest) = 64),
    after_value_json TEXT,
    committed_at TEXT NOT NULL,
    FOREIGN KEY (run_id, frame_id)
        REFERENCES scope_frames (run_id, frame_id)
        ON DELETE RESTRICT,
    UNIQUE (run_id, frame_id, declaration_id, after_version),
    UNIQUE (run_id, frame_id, declaration_id, before_version),
    CHECK (
        (before_version = 0 AND before_present = 0 AND before_digest IS NULL)
        OR
        (
            before_version > 0
            AND before_digest IS NOT NULL
            AND length(before_digest) = 64
        )
    ),
    CHECK (
        (after_present = 0 AND after_value_json IS NULL)
        OR (after_present = 1 AND after_value_json IS NOT NULL)
    )
)
"""
_CREATE_VARIABLE_MUTATIONS_HISTORY_INDEX = """
CREATE INDEX idx_variable_mutations_history
    ON variable_mutations (run_id, frame_id, declaration_id, after_version)
"""

_CREATE_STATEMENTS = (
    _CREATE_RUN_BINDING,
    _CREATE_SCOPE_FRAMES,
    _CREATE_SCOPE_ROOT_INDEX,
    _CREATE_SCOPE_PARENT_INDEX,
    _CREATE_VARIABLE_STATES,
    _CREATE_VARIABLE_STATES_NAME_INDEX,
    _CREATE_VARIABLE_STATES_FRAME_INDEX,
    _CREATE_VARIABLE_MUTATIONS,
    _CREATE_VARIABLE_MUTATIONS_HISTORY_INDEX,
)


class StrictWorkflowVariableError(RuntimeError):
    """Base class for value-free variable-store errors."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class StrictWorkflowVariableStorageUnavailable(StrictWorkflowVariableError):
    """The run-local database is missing, malformed, or unsupported."""

    def __init__(self) -> None:
        super().__init__(_UNAVAILABLE)


class StrictWorkflowVariableInvalidMetadata(StrictWorkflowVariableError):
    """Caller-supplied identity metadata is invalid."""

    def __init__(self) -> None:
        super().__init__(_INVALID_METADATA)


class StrictWorkflowVariableConflict(StrictWorkflowVariableError):
    """An existing database or binding conflicts with initialization."""

    def __init__(self) -> None:
        super().__init__(_CONFLICT)


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowRunBindingInput:
    run_id: str = field(repr=False)
    workflow_id: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    created_at: datetime = field(repr=False)
    run_format_version: int = field(default=2, repr=False)
    store_schema_version: int = field(default=1, repr=False)
    status: str = field(default="INITIALIZED", repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowRunBindingInput(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowVariableDatabaseInfo:
    user_version: int = field(repr=False)
    run_id: str = field(repr=False)
    workflow_id: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    run_format_version: int = field(repr=False)
    store_schema_version: int = field(repr=False)
    status: str = field(repr=False)
    created_at: str = field(repr=False)
    root_frame_id: str = field(repr=False)
    frame_count: int = field(repr=False)
    variable_state_count: int = field(repr=False)
    variable_mutation_count: int = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowVariableDatabaseInfo(<verified>)"


_FAULT_INJECTOR = Callable[[str], None]


class StrictWorkflowVariableStore:
    """Internal explicit initializer and read-only inspector for one run DB."""

    def __init__(self, run_directory: Path) -> None:
        try:
            self._run_directory = Path(run_directory)
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None

    def initialize_new_database(
        self,
        binding: StrictWorkflowRunBindingInput,
        *,
        _fault_injector: _FAULT_INJECTOR | None = None,
    ) -> None:
        """Create schema and the initial binding/root rows in one transaction."""
        created_at = _validate_binding(binding)
        root = self._resolve_run_directory()
        database = root / _DATABASE_NAME
        related_paths = _database_related_paths(database)
        if any(_is_symlink_or_junction(path) or path.exists() for path in related_paths):
            raise StrictWorkflowVariableConflict()
        if database.parent.resolve(strict=True) != root:
            raise StrictWorkflowVariableStorageUnavailable()

        connection: sqlite3.Connection | None = None
        created_by_call = False
        initialized = False
        try:
            with database.open("xb"):
                pass
            created_by_call = True
            connection = sqlite3.connect(
                database.resolve(strict=True).as_uri() + "?mode=rw",
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            _configure_write_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            _expect_empty_database(connection)
            _create_schema(connection)
            _fault_checkpoint(_fault_injector, "after_schema")
            connection.execute(
                """
                INSERT INTO run_binding (
                    run_id, workflow_id, definition_id, definition_revision,
                    contract_sha256, binding_digest, normalized_input_sha256,
                    run_format_version, store_schema_version, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding.run_id,
                    binding.workflow_id,
                    binding.definition_id,
                    binding.definition_revision,
                    binding.contract_sha256,
                    binding.binding_digest,
                    binding.normalized_input_sha256,
                    binding.run_format_version,
                    binding.store_schema_version,
                    binding.status,
                    created_at,
                ),
            )
            _fault_checkpoint(_fault_injector, "after_run_binding")
            connection.execute(
                """
                INSERT INTO scope_frames (
                    run_id, frame_id, parent_frame_id, scope_path,
                    iteration_vector_json, status, created_at, closed_at
                ) VALUES (?, ?, NULL, ?, ?, 'ACTIVE', ?, NULL)
                """,
                (
                    binding.run_id,
                    _ROOT_FRAME_ID,
                    _ROOT_SCOPE_PATH,
                    _ROOT_ITERATION_VECTOR,
                    created_at,
                ),
            )
            _fault_checkpoint(_fault_injector, "after_root")
            connection.execute("PRAGMA user_version=1")
            _check_schema(connection)
            _check_integrity(connection)
            _validate_initial_rows(connection, binding, created_at)
            _fault_checkpoint(_fault_injector, "before_commit")
            connection.execute("COMMIT")
            initialized = True
        except StrictWorkflowVariableError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    if initialized:
                        raise StrictWorkflowVariableStorageUnavailable() from None
            if created_by_call and not initialized:
                _remove_created_database_files(related_paths)

    def inspect(
        self,
        expected_run_id: str,
        expected_workflow_id: str,
        *,
        expected_definition_id: str,
        expected_definition_revision: int,
        expected_contract_sha256: str,
        expected_binding_digest: str,
        expected_normalized_input_sha256: str,
        expected_created_at: datetime,
    ) -> StrictWorkflowVariableDatabaseInfo:
        """Inspect only the initial, empty variable-store state in read-only mode."""
        expected = StrictWorkflowRunBindingInput(
            run_id=expected_run_id,
            workflow_id=expected_workflow_id,
            definition_id=expected_definition_id,
            definition_revision=expected_definition_revision,
            contract_sha256=expected_contract_sha256,
            binding_digest=expected_binding_digest,
            normalized_input_sha256=expected_normalized_input_sha256,
            created_at=expected_created_at,
        )
        expected_created = _validate_binding(expected)
        database = self._database_path(required=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = _read_connection(database)
            _check_schema(connection)
            _check_integrity(connection)
            info = _validate_initial_rows(connection, expected, expected_created)
            return info
        except StrictWorkflowVariableError:
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def _resolve_run_directory(self) -> Path:
        candidate = self._run_directory.expanduser()
        if _is_symlink_or_junction(candidate):
            raise StrictWorkflowVariableStorageUnavailable()
        try:
            if not candidate.exists() or not candidate.is_dir():
                raise StrictWorkflowVariableStorageUnavailable()
            root = candidate.resolve(strict=True)
            if not root.is_dir():
                raise StrictWorkflowVariableStorageUnavailable()
            return root
        except StrictWorkflowVariableError:
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None

    def _database_path(self, *, required: bool) -> Path:
        root = self._resolve_run_directory()
        database = root / _DATABASE_NAME
        if _is_symlink_or_junction(database):
            raise StrictWorkflowVariableStorageUnavailable()
        if required and (not database.exists() or not database.is_file()):
            raise StrictWorkflowVariableStorageUnavailable()
        if database.exists() and not database.is_file():
            raise StrictWorkflowVariableStorageUnavailable()
        try:
            if database.parent.resolve(strict=True) != root:
                raise StrictWorkflowVariableStorageUnavailable()
            if database.exists() and database.resolve(strict=True).parent != root:
                raise StrictWorkflowVariableStorageUnavailable()
        except StrictWorkflowVariableError:
            raise
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        return database


_EXPECTED_TABLE_INFO: dict[str, tuple[tuple[object, ...], ...]] = {
    "run_binding": (
        (0, "run_id", "TEXT", 0, None, 1),
        (1, "workflow_id", "TEXT", 1, None, 0),
        (2, "definition_id", "TEXT", 1, None, 0),
        (3, "definition_revision", "INTEGER", 1, None, 0),
        (4, "contract_sha256", "TEXT", 1, None, 0),
        (5, "binding_digest", "TEXT", 1, None, 0),
        (6, "normalized_input_sha256", "TEXT", 1, None, 0),
        (7, "run_format_version", "INTEGER", 1, None, 0),
        (8, "store_schema_version", "INTEGER", 1, None, 0),
        (9, "status", "TEXT", 1, None, 0),
        (10, "created_at", "TEXT", 1, None, 0),
    ),
    "scope_frames": (
        (0, "run_id", "TEXT", 1, None, 1),
        (1, "frame_id", "TEXT", 1, None, 2),
        (2, "parent_frame_id", "TEXT", 0, None, 0),
        (3, "scope_path", "TEXT", 1, None, 0),
        (4, "iteration_vector_json", "TEXT", 1, None, 0),
        (5, "status", "TEXT", 1, None, 0),
        (6, "created_at", "TEXT", 1, None, 0),
        (7, "closed_at", "TEXT", 0, None, 0),
    ),
    "variable_states": (
        (0, "run_id", "TEXT", 1, None, 1),
        (1, "frame_id", "TEXT", 1, None, 2),
        (2, "declaration_id", "TEXT", 1, None, 3),
        (3, "name", "TEXT", 1, None, 0),
        (4, "present", "INTEGER", 1, None, 0),
        (5, "value_json", "TEXT", 0, None, 0),
        (6, "version", "INTEGER", 1, None, 0),
        (7, "value_digest", "TEXT", 1, None, 0),
        (8, "updated_at", "TEXT", 1, None, 0),
    ),
    "variable_mutations": (
        (0, "execution_key", "TEXT", 0, None, 1),
        (1, "run_id", "TEXT", 1, None, 0),
        (2, "frame_id", "TEXT", 1, None, 0),
        (3, "declaration_id", "TEXT", 1, None, 0),
        (4, "mutation_ordinal", "INTEGER", 1, None, 0),
        (5, "operation", "TEXT", 1, None, 0),
        (6, "payload_digest", "TEXT", 1, None, 0),
        (7, "before_version", "INTEGER", 1, None, 0),
        (8, "after_version", "INTEGER", 1, None, 0),
        (9, "before_present", "INTEGER", 1, None, 0),
        (10, "after_present", "INTEGER", 1, None, 0),
        (11, "before_digest", "TEXT", 0, None, 0),
        (12, "after_digest", "TEXT", 1, None, 0),
        (13, "after_value_json", "TEXT", 0, None, 0),
        (14, "committed_at", "TEXT", 1, None, 0),
    ),
}

_EXPECTED_OBJECTS = {
    (kind, name, table_name, re.sub(r"\s+", "", statement).casefold())
    for kind, name, table_name, statement in (
        ("table", "run_binding", "run_binding", _CREATE_RUN_BINDING),
        ("table", "scope_frames", "scope_frames", _CREATE_SCOPE_FRAMES),
        ("index", "uq_scope_frames_one_root", "scope_frames", _CREATE_SCOPE_ROOT_INDEX),
        ("index", "idx_scope_frames_parent", "scope_frames", _CREATE_SCOPE_PARENT_INDEX),
        ("table", "variable_states", "variable_states", _CREATE_VARIABLE_STATES),
        (
            "index",
            "uq_variable_states_scope_name",
            "variable_states",
            _CREATE_VARIABLE_STATES_NAME_INDEX,
        ),
        (
            "index",
            "idx_variable_states_frame",
            "variable_states",
            _CREATE_VARIABLE_STATES_FRAME_INDEX,
        ),
        ("table", "variable_mutations", "variable_mutations", _CREATE_VARIABLE_MUTATIONS),
        (
            "index",
            "idx_variable_mutations_history",
            "variable_mutations",
            _CREATE_VARIABLE_MUTATIONS_HISTORY_INDEX,
        ),
    )
}

_EXPECTED_INDEXES = {
    "run_binding": frozenset({(1, "pk", 0, ("run_id",))}),
    "scope_frames": frozenset(
        {
            (1, "pk", 0, ("run_id", "frame_id")),
            (1, "c", 1, ("run_id",)),
            (0, "c", 0, ("run_id", "parent_frame_id")),
        }
    ),
    "variable_states": frozenset(
        {
            (1, "pk", 0, ("run_id", "frame_id", "declaration_id")),
            (1, "c", 0, ("run_id", "frame_id", "name")),
            (0, "c", 0, ("run_id", "frame_id", "declaration_id")),
        }
    ),
    "variable_mutations": frozenset(
        {
            (1, "pk", 0, ("execution_key",)),
            (1, "u", 0, ("run_id", "frame_id", "declaration_id", "after_version")),
            (1, "u", 0, ("run_id", "frame_id", "declaration_id", "before_version")),
            (0, "c", 0, ("run_id", "frame_id", "declaration_id", "after_version")),
        }
    ),
}


def _validate_binding(binding: object) -> str:
    if not isinstance(binding, StrictWorkflowRunBindingInput):
        raise StrictWorkflowVariableInvalidMetadata()
    if (
        not isinstance(binding.run_id, str)
        or _RUN_ID.fullmatch(binding.run_id) is None
        or not _is_canonical_uuid(binding.workflow_id)
        or not _is_canonical_uuid(binding.definition_id)
        or type(binding.definition_revision) is not int
        or binding.definition_revision < 1
        or not _is_sha256(binding.contract_sha256)
        or not _is_sha256(binding.binding_digest)
        or not _is_sha256(binding.normalized_input_sha256)
        or type(binding.run_format_version) is not int
        or binding.run_format_version != 2
        or type(binding.store_schema_version) is not int
        or binding.store_schema_version != 1
        or binding.status != "INITIALIZED"
    ):
        raise StrictWorkflowVariableInvalidMetadata()
    if type(binding.status) is not str or type(binding.created_at) is not datetime:
        raise StrictWorkflowVariableInvalidMetadata()
    try:
        offset = binding.created_at.utcoffset()
        if offset is None or offset != timedelta(0):
            raise StrictWorkflowVariableInvalidMetadata()
        return _format_utc_timestamp(binding.created_at)
    except StrictWorkflowVariableError:
        raise
    except Exception:
        raise StrictWorkflowVariableInvalidMetadata() from None


def _is_canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise StrictWorkflowVariableStorageUnavailable()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise StrictWorkflowVariableStorageUnavailable() from None
    if parsed.utcoffset() != timedelta(0) or _format_utc_timestamp(parsed) != value:
        raise StrictWorkflowVariableStorageUnavailable()
    return parsed


def _configure_write_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).casefold() != "wal":
        raise StrictWorkflowVariableStorageUnavailable()
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")


def _read_connection(database: Path) -> sqlite3.Connection:
    if _is_symlink_or_junction(database) or not database.is_file():
        raise StrictWorkflowVariableStorageUnavailable()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            database.resolve(strict=True).as_uri() + "?mode=ro",
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise StrictWorkflowVariableStorageUnavailable() from None


def _check_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()
    if version is None or type(version[0]) is not int or version[0] != 1:
        raise StrictWorkflowVariableStorageUnavailable()
    actual_objects = {
        (
            row["type"],
            row["name"],
            row["tbl_name"],
            _normalize_sql(row["sql"]),
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    }
    if actual_objects != _EXPECTED_OBJECTS:
        raise StrictWorkflowVariableStorageUnavailable()

    for table_name, expected_rows in _EXPECTED_TABLE_INFO.items():
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        actual_rows = tuple(
            (row["cid"], row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"])
            for row in rows
        )
        if actual_rows != expected_rows:
            raise StrictWorkflowVariableStorageUnavailable()

    _check_foreign_keys(
        connection,
        "run_binding",
        (),
    )
    _check_foreign_keys(
        connection,
        "scope_frames",
        (
            (("run_binding", "run_id", "run_id", "NO ACTION", "RESTRICT", "NONE"),),
            (
                ("scope_frames", "run_id", "run_id", "NO ACTION", "RESTRICT", "NONE"),
                ("scope_frames", "parent_frame_id", "frame_id", "NO ACTION", "RESTRICT", "NONE"),
            ),
        ),
    )
    for table_name in ("variable_states", "variable_mutations"):
        _check_foreign_keys(
            connection,
            table_name,
            (
                (
                    ("scope_frames", "run_id", "run_id", "NO ACTION", "RESTRICT", "NONE"),
                    ("scope_frames", "frame_id", "frame_id", "NO ACTION", "RESTRICT", "NONE"),
                ),
            ),
        )
    for table_name, expected in _EXPECTED_INDEXES.items():
        _check_indexes(connection, table_name, expected)

    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
        raise StrictWorkflowVariableStorageUnavailable()


def _normalize_sql(sql: object) -> str | None:
    if not isinstance(sql, str):
        return None
    return re.sub(r"\s+", "", sql).casefold()


def _check_foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
    expected_groups: tuple[tuple[tuple[str, ...], ...], ...],
) -> None:
    rows = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["id"], []).append(row)
    actual = tuple(
        sorted(
            tuple(
                (
                    row["table"],
                    row["from"],
                    row["to"],
                    row["on_update"],
                    row["on_delete"],
                    row["match"],
                )
                for row in sorted(group, key=lambda item: item["seq"])
            )
            for group in grouped.values()
        )
    )
    if actual != tuple(sorted(expected_groups)):
        raise StrictWorkflowVariableStorageUnavailable()


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
                row["unique"],
                row["origin"],
                row["partial"],
                tuple(column["name"] for column in columns),
            )
        )
    if actual != expected:
        raise StrictWorkflowVariableStorageUnavailable()


def _check_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise StrictWorkflowVariableStorageUnavailable()
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise StrictWorkflowVariableStorageUnavailable()


def _expect_empty_database(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    version = connection.execute("PRAGMA user_version").fetchone()
    if row is not None or version is None or version[0] != 0:
        raise StrictWorkflowVariableConflict()


def _create_schema(connection: sqlite3.Connection) -> None:
    for statement in _CREATE_STATEMENTS:
        connection.execute(statement)


def _validate_initial_rows(
    connection: sqlite3.Connection,
    expected: StrictWorkflowRunBindingInput,
    expected_created_at: str,
) -> StrictWorkflowVariableDatabaseInfo:
    bindings = connection.execute("SELECT * FROM run_binding").fetchall()
    if len(bindings) != 1:
        raise StrictWorkflowVariableStorageUnavailable()
    row = bindings[0]
    if (
        type(row["run_id"]) is not str
        or _RUN_ID.fullmatch(row["run_id"]) is None
        or not _is_canonical_uuid(row["workflow_id"])
        or not _is_canonical_uuid(row["definition_id"])
        or type(row["definition_revision"]) is not int
        or row["definition_revision"] < 1
        or not _is_sha256(row["contract_sha256"])
        or not _is_sha256(row["binding_digest"])
        or not _is_sha256(row["normalized_input_sha256"])
        or type(row["run_format_version"]) is not int
        or row["run_format_version"] != 2
        or type(row["store_schema_version"]) is not int
        or row["store_schema_version"] != 1
        or row["status"] != "INITIALIZED"
        or type(row["status"]) is not str
    ):
        raise StrictWorkflowVariableStorageUnavailable()
    stored_created_at = _format_utc_timestamp(_parse_utc_timestamp(row["created_at"]))
    expected_identity = (
        expected.run_id,
        expected.workflow_id,
        expected.definition_id,
        expected.definition_revision,
        expected.contract_sha256,
        expected.binding_digest,
        expected.normalized_input_sha256,
        expected.run_format_version,
        expected.store_schema_version,
        expected.status,
        expected_created_at,
    )
    actual_identity = (
        row["run_id"],
        row["workflow_id"],
        row["definition_id"],
        row["definition_revision"],
        row["contract_sha256"],
        row["binding_digest"],
        row["normalized_input_sha256"],
        row["run_format_version"],
        row["store_schema_version"],
        row["status"],
        stored_created_at,
    )
    if actual_identity != expected_identity:
        raise StrictWorkflowVariableConflict()

    frames = connection.execute("SELECT * FROM scope_frames").fetchall()
    if len(frames) != 1:
        raise StrictWorkflowVariableStorageUnavailable()
    frame = frames[0]
    if (
        frame["run_id"] != row["run_id"]
        or frame["frame_id"] != _ROOT_FRAME_ID
        or frame["parent_frame_id"] is not None
        or frame["scope_path"] != _ROOT_SCOPE_PATH
        or frame["iteration_vector_json"] != _ROOT_ITERATION_VECTOR
        or frame["status"] != "ACTIVE"
        or frame["closed_at"] is not None
        or frame["created_at"] != stored_created_at
    ):
        raise StrictWorkflowVariableStorageUnavailable()
    if connection.execute("SELECT 1 FROM variable_states LIMIT 1").fetchone() is not None:
        raise StrictWorkflowVariableStorageUnavailable()
    if connection.execute("SELECT 1 FROM variable_mutations LIMIT 1").fetchone() is not None:
        raise StrictWorkflowVariableStorageUnavailable()
    return StrictWorkflowVariableDatabaseInfo(
        user_version=1,
        run_id=row["run_id"],
        workflow_id=row["workflow_id"],
        definition_id=row["definition_id"],
        definition_revision=row["definition_revision"],
        contract_sha256=row["contract_sha256"],
        binding_digest=row["binding_digest"],
        normalized_input_sha256=row["normalized_input_sha256"],
        run_format_version=row["run_format_version"],
        store_schema_version=row["store_schema_version"],
        status=row["status"],
        created_at=stored_created_at,
        root_frame_id=frame["frame_id"],
        frame_count=len(frames),
        variable_state_count=0,
        variable_mutation_count=0,
    )


def _database_related_paths(database: Path) -> tuple[Path, Path, Path]:
    return (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
    )


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _fault_checkpoint(injector: _FAULT_INJECTOR | None, name: str) -> None:
    if injector is not None:
        injector(name)


def _rollback(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _remove_created_database_files(paths: tuple[Path, Path, Path]) -> None:
    for path in paths:
        try:
            if path.is_file() or _is_symlink_or_junction(path):
                path.unlink(missing_ok=True)
        except OSError:
            pass
