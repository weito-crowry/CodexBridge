"""Opt-in definitions.sqlite v2 storage for isolated strict-contract work.

Nothing in the legacy application constructs this store. Callers must explicitly
initialize a new v2 database or migrate an isolated v1 database before using CRUD.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import (
    CompiledContract,
    ContractError,
    canonical_json,
    compile_contract,
    parse_strict_json,
)
from .definitions import (
    _DATABASE_NAME,
    _MUTATION_REQUEST_ID,
    _SHA256,
    DefinitionError,
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionNotFound,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    DefinitionRevision,
    WorkflowDefinitionRepository,
    _canonical_json,
    _is_canonical_uuid,
    _is_symlink_or_junction,
    _mutation_digest,
    _strong_etag,
    _utc_now,
    _validate_definition_id,
    _validate_mutation_request_id,
    _validate_revision,
    _validate_source_fields,
)

_STRICT_DATABASE_SCHEMA_VERSION = 2
_BUSY_TIMEOUT_MS = 10_000
_STRICT_ETAG_DOMAIN = "codexbridge-workflow-definition-etag-v2"
_STRICT_MUTATION_DOMAIN = "codexbridge-workflow-definition-mutation-v2"
_STRICT_ETAG = re.compile(r'"workflow-definition-v2-[0-9a-f]{64}"\Z', re.ASCII)
_MISSING = object()
_UNAVAILABLE = "workflow definition storage is unavailable"

_CREATE_DEFINITIONS = """
CREATE TABLE workflow_definitions (
    definition_id TEXT PRIMARY KEY,
    latest_revision INTEGER NOT NULL CHECK (latest_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_CREATE_REVISIONS = """
CREATE TABLE workflow_definition_revisions (
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    source_yaml TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL,
    max_live_codex_jobs INTEGER NOT NULL
        CHECK (max_live_codex_jobs IN (1, 2, 3)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (definition_id, revision),
    FOREIGN KEY (definition_id)
        REFERENCES workflow_definitions (definition_id)
)
"""
_CREATE_MUTATIONS = """
CREATE TABLE workflow_definition_mutations (
    mutation_request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update')),
    request_digest TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    result_revision INTEGER NOT NULL CHECK (result_revision >= 1),
    result_etag TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
_CREATE_CONTRACTS = """
CREATE TABLE workflow_definition_contracts (
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    contract_json_canonical TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL CHECK (length(contract_sha256) = 64),
    binding_digest TEXT NOT NULL CHECK (length(binding_digest) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (definition_id, revision),
    FOREIGN KEY (definition_id, revision)
        REFERENCES workflow_definition_revisions (definition_id, revision)
        ON DELETE RESTRICT
)
"""


def _normalize_sql(sql: object) -> str | None:
    if not isinstance(sql, str):
        return None
    return re.sub(r"\s+", "", sql).casefold()


def _expected_objects(*, include_contracts: bool) -> set[tuple[str, str, str, str]]:
    statements = [
        ("table", "workflow_definitions", "workflow_definitions", _CREATE_DEFINITIONS),
        (
            "table",
            "workflow_definition_revisions",
            "workflow_definition_revisions",
            _CREATE_REVISIONS,
        ),
        (
            "table",
            "workflow_definition_mutations",
            "workflow_definition_mutations",
            _CREATE_MUTATIONS,
        ),
    ]
    if include_contracts:
        statements.append(
            (
                "table",
                "workflow_definition_contracts",
                "workflow_definition_contracts",
                _CREATE_CONTRACTS,
            )
        )
    return {
        (kind, name, table_name, normalized)
        for kind, name, table_name, sql in statements
        if (normalized := _normalize_sql(sql)) is not None
    }


_EXPECTED_TABLE_INFO: dict[str, tuple[tuple[int, str, str, int, None, int], ...]] = {
    "workflow_definitions": (
        (0, "definition_id", "TEXT", 0, None, 1),
        (1, "latest_revision", "INTEGER", 1, None, 0),
        (2, "created_at", "TEXT", 1, None, 0),
        (3, "updated_at", "TEXT", 1, None, 0),
    ),
    "workflow_definition_revisions": (
        (0, "definition_id", "TEXT", 1, None, 1),
        (1, "revision", "INTEGER", 1, None, 2),
        (2, "source_yaml", "TEXT", 1, None, 0),
        (3, "source_sha256", "TEXT", 1, None, 0),
        (4, "canonical_sha256", "TEXT", 1, None, 0),
        (5, "max_live_codex_jobs", "INTEGER", 1, None, 0),
        (6, "created_at", "TEXT", 1, None, 0),
    ),
    "workflow_definition_mutations": (
        (0, "mutation_request_id", "TEXT", 0, None, 1),
        (1, "operation", "TEXT", 1, None, 0),
        (2, "request_digest", "TEXT", 1, None, 0),
        (3, "definition_id", "TEXT", 1, None, 0),
        (4, "result_revision", "INTEGER", 1, None, 0),
        (5, "result_etag", "TEXT", 1, None, 0),
        (6, "created_at", "TEXT", 1, None, 0),
    ),
    "workflow_definition_contracts": (
        (0, "definition_id", "TEXT", 1, None, 1),
        (1, "revision", "INTEGER", 1, None, 2),
        (2, "contract_json_canonical", "TEXT", 1, None, 0),
        (3, "contract_sha256", "TEXT", 1, None, 0),
        (4, "binding_digest", "TEXT", 1, None, 0),
        (5, "created_at", "TEXT", 1, None, 0),
    ),
}


@dataclass(frozen=True, slots=True)
class StrictDefinitionRevision:
    """A validated definition revision and its optional strict contract."""

    definition_id: str
    revision: int
    source_yaml: str = field(repr=False)
    source_sha256: str
    canonical_sha256: str
    max_live_codex_jobs: int
    created_at: str
    etag: str
    definition: dict[str, Any] = field(repr=False)
    contract: CompiledContract | None = field(repr=False)
    contract_json_canonical: str | None = field(repr=False)
    contract_sha256: str | None
    binding_digest: str | None

    @property
    def is_strict(self) -> bool:
        return self.contract_json_canonical is not None


@dataclass(frozen=True, slots=True)
class _LegacyIdentity:
    digest: str
    definition_count: int
    revision_count: int
    mutation_count: int


class StrictWorkflowDefinitionStore:
    """Internal, opt-in CRUD store for an explicitly prepared definitions v2 DB."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = Path(state_root)
        # Reuse the existing root and source validation boundary. No legacy
        # repository method is called for database initialization or migration.
        self._validator = WorkflowDefinitionRepository(self._state_root)

    def initialize_new_database(self) -> None:
        """Explicitly create a new, empty definitions.sqlite schema v2."""
        root = self._validator._resolve_state_root(create=True)
        if root is None:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        database = root / _DATABASE_NAME
        if _is_symlink_or_junction(database) or database.exists():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

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
            self._configure_writable_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            _expect_empty_database(connection)
            _create_v2_schema(connection)
            connection.execute("PRAGMA user_version=2")
            _check_v2_schema(connection)
            _check_integrity(connection)
            connection.execute("COMMIT")
            initialized = True
        except DefinitionError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()
            if created_by_call and not initialized:
                # An empty/rolled-back database created here can be retried.
                for suffix in ("", "-wal", "-shm"):
                    candidate = Path(f"{database}{suffix}")
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass

    def migrate_v1_to_v2(self, *, backup_path: Path) -> None:
        """Verify, online-backup, and explicitly migrate a v1 database to v2."""
        database = self._database_path(create=False, required=True)
        if database is None:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        backup_identity = self.create_verified_backup(backup_path)

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            self._configure_writable_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            _check_v1_schema(connection)
            before = _legacy_identity(connection, self._validator)
            if before != backup_identity:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

            connection.execute(_CREATE_CONTRACTS)
            _migration_checkpoint("after_create")
            connection.execute("PRAGMA user_version=2")
            _migration_checkpoint("after_user_version")
            _check_v2_schema(connection)
            _check_integrity(connection)
            after = _legacy_identity(connection, self._validator)
            if after != before:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            if (
                connection.execute("SELECT COUNT(*) FROM workflow_definition_contracts").fetchone()[
                    0
                ]
                != 0
            ):
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            _migration_checkpoint("before_commit")
            connection.execute("COMMIT")
        except DefinitionError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()

        # Read back after commit to verify the durable state, including the
        # legacy identity that must remain byte-for-byte stable.
        with self._read_connection(required=True) as read_connection:
            if read_connection is None:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            _check_v2_schema(read_connection)
            _check_integrity(read_connection)
            if _legacy_identity(read_connection, self._validator) != backup_identity:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            if (
                read_connection.execute(
                    "SELECT COUNT(*) FROM workflow_definition_contracts"
                ).fetchone()[0]
                != 0
            ):
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def create_verified_backup(self, destination: Path) -> _LegacyIdentity:
        """Create and validate a consistent online backup of an isolated v1 DB."""
        database = self._database_path(create=False, required=True)
        if database is None:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        root = database.parent
        backup = _contained_file_path(root, destination)
        if backup.exists():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

        with self._read_connection(required=True, version=1) as source:
            if source is None:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            identity = _legacy_identity(source, self._validator)
            target: sqlite3.Connection | None = None
            try:
                with backup.open("xb"):
                    pass
                target = sqlite3.connect(
                    backup,
                    timeout=_BUSY_TIMEOUT_MS / 1000,
                    isolation_level=None,
                )
                target.row_factory = sqlite3.Row
                source.backup(target)
            except Exception:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
            finally:
                if target is not None:
                    target.close()

        try:
            with _readonly_connection(backup) as restored:
                _check_v1_schema(restored)
                _check_integrity(restored)
                if _legacy_identity(restored, self._validator) != identity:
                    raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        except DefinitionError:
            raise
        except Exception:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        return identity

    def restore_verified_backup(
        self,
        backup_path: Path,
        *,
        destination_state_root: Path,
    ) -> None:
        """Restore a verified v1 online backup into a new, empty isolated root."""
        source_database = self._database_path(create=False, required=True)
        if source_database is None:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        source = _contained_file_path(source_database.parent, backup_path)
        if not source.is_file():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        destination_store = StrictWorkflowDefinitionStore(destination_state_root)
        destination_root = destination_store._validator._resolve_state_root(create=True)
        if destination_root is None or destination_root == source_database.parent:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        destination_database = destination_root / _DATABASE_NAME
        if _is_symlink_or_junction(destination_database) or destination_database.exists():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

        with _readonly_connection(source) as backup:
            _check_v1_schema(backup)
            _check_integrity(backup)
            identity = _legacy_identity(backup, self._validator)
            target: sqlite3.Connection | None = None
            try:
                with destination_database.open("xb"):
                    pass
                target = sqlite3.connect(
                    destination_database,
                    timeout=_BUSY_TIMEOUT_MS / 1000,
                    isolation_level=None,
                )
                target.row_factory = sqlite3.Row
                backup.backup(target)
            except Exception:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
            finally:
                if target is not None:
                    target.close()

        try:
            with _readonly_connection(destination_database) as restored:
                _check_v1_schema(restored)
                _check_integrity(restored)
                if _legacy_identity(restored, destination_store._validator) != identity:
                    raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        except DefinitionError:
            raise
        except Exception:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None

    def create(
        self,
        *,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
        contract: object | None = None,
    ) -> StrictDefinitionRevision:
        request_id = _validate_mutation_request_id(mutation_request_id)
        compiled = _compile_request_contract(contract) if contract is not None else None
        try:
            request_digest = (
                _strict_mutation_digest(
                    operation="create",
                    definition_id=None,
                    expected_revision=None,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    contract=compiled,
                )
                if compiled is not None
                else _mutation_digest(
                    operation="create",
                    definition_id=None,
                    expected_revision=None,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
            )
        except (TypeError, ValueError):
            raise DefinitionInvalid("mutation request is invalid") from None

        replay = self._read_mutation_replay(request_id, "create", request_digest)
        if replay is not None:
            return replay
        _validate_source_fields(source_yaml, max_live_codex_jobs)
        validated = self._validator.validate(source_yaml, max_live_codex_jobs)
        definition_id = str(uuid.uuid4())
        created_at = _utc_now()
        contract_values = (
            _contract_binding(
                definition_id,
                1,
                validated.source_sha256,
                validated.canonical_sha256,
                validated.max_live_codex_jobs,
                compiled,
            )
            if compiled is not None
            else None
        )
        etag = (
            _strict_etag(
                definition_id,
                1,
                validated.source_sha256,
                validated.canonical_sha256,
                validated.max_live_codex_jobs,
                contract_values[0],
                contract_values[1],
            )
            if contract_values is not None
            else _strong_etag(
                definition_id,
                1,
                validated.source_sha256,
                validated.max_live_codex_jobs,
            )
        )

        with self._write_transaction() as connection:
            replay = self._mutation_replay(connection, request_id, "create", request_digest)
            if replay is not None:
                return replay
            connection.execute(
                """
                INSERT INTO workflow_definitions (
                    definition_id, latest_revision, created_at, updated_at
                ) VALUES (?, 1, ?, ?)
                """,
                (definition_id, created_at, created_at),
            )
            self._insert_revision(connection, definition_id, 1, validated, created_at)
            if compiled is not None and contract_values is not None:
                self._insert_contract(
                    connection,
                    definition_id,
                    1,
                    compiled,
                    contract_values[0],
                    contract_values[1],
                    created_at,
                )
            self._insert_mutation(
                connection,
                request_id,
                "create",
                request_digest,
                definition_id,
                1,
                etag,
                created_at,
            )
            return self._get_revision(connection, definition_id, 1)

    def update(
        self,
        *,
        definition_id: str,
        expected_revision: int,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
        contract: object = _MISSING,
    ) -> StrictDefinitionRevision:
        request_id = _validate_mutation_request_id(mutation_request_id)
        has_contract = contract is not _MISSING and contract is not None
        compiled = _compile_request_contract(contract) if has_contract else None
        try:
            request_digest = (
                _strict_mutation_digest(
                    operation="update",
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                    contract=compiled,
                )
                if compiled is not None
                else _mutation_digest(
                    operation="update",
                    definition_id=definition_id,
                    expected_revision=expected_revision,
                    source_yaml=source_yaml,
                    max_live_codex_jobs=max_live_codex_jobs,
                )
            )
        except (TypeError, ValueError):
            raise DefinitionInvalid("mutation request is invalid") from None

        replay = self._read_mutation_replay(request_id, "update", request_digest)
        if replay is not None:
            if replay.is_strict and compiled is None:
                raise DefinitionMutationConflict(
                    "mutation request ID was already used for a different request"
                )
            return replay

        canonical_id = _validate_definition_id(definition_id)
        _validate_revision(expected_revision)
        _validate_source_fields(source_yaml, max_live_codex_jobs)
        validated = self._validator.validate(source_yaml, max_live_codex_jobs)
        updated_at = _utc_now()

        with self._write_transaction() as connection:
            replay = self._mutation_replay(connection, request_id, "update", request_digest)
            if replay is not None:
                if replay.is_strict and compiled is None:
                    raise DefinitionMutationConflict(
                        "mutation request ID was already used for a different request"
                    )
                return replay
            definition_row = connection.execute(
                """
                SELECT latest_revision
                FROM workflow_definitions
                WHERE definition_id = ?
                """,
                (canonical_id,),
            ).fetchone()
            if definition_row is None:
                raise DefinitionNotFound("workflow definition was not found")
            latest_revision = definition_row["latest_revision"]
            if type(latest_revision) is not int or latest_revision < 1:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            if latest_revision != expected_revision:
                raise DefinitionPreconditionFailed(
                    "workflow definition revision precondition failed"
                )

            current = self._get_revision(connection, canonical_id, latest_revision)
            if current.is_strict and compiled is None:
                raise DefinitionInvalid("strict workflow contract is required")
            if (
                current.is_strict == (compiled is not None)
                and current.source_yaml == validated.source_yaml
                and current.max_live_codex_jobs == validated.max_live_codex_jobs
                and (compiled is None or current.contract_sha256 == compiled.contract_sha256)
            ):
                result = current
            else:
                next_revision = latest_revision + 1
                contract_values = (
                    _contract_binding(
                        canonical_id,
                        next_revision,
                        validated.source_sha256,
                        validated.canonical_sha256,
                        validated.max_live_codex_jobs,
                        compiled,
                    )
                    if compiled is not None
                    else None
                )
                next_etag = (
                    _strict_etag(
                        canonical_id,
                        next_revision,
                        validated.source_sha256,
                        validated.canonical_sha256,
                        validated.max_live_codex_jobs,
                        contract_values[0],
                        contract_values[1],
                    )
                    if contract_values is not None
                    else _strong_etag(
                        canonical_id,
                        next_revision,
                        validated.source_sha256,
                        validated.max_live_codex_jobs,
                    )
                )
                self._insert_revision(
                    connection,
                    canonical_id,
                    next_revision,
                    validated,
                    updated_at,
                )
                if compiled is not None and contract_values is not None:
                    self._insert_contract(
                        connection,
                        canonical_id,
                        next_revision,
                        compiled,
                        contract_values[0],
                        contract_values[1],
                        updated_at,
                    )
                cursor = connection.execute(
                    """
                    UPDATE workflow_definitions
                    SET latest_revision = ?, updated_at = ?
                    WHERE definition_id = ? AND latest_revision = ?
                    """,
                    (next_revision, updated_at, canonical_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise DefinitionPreconditionFailed(
                        "workflow definition revision precondition failed"
                    )
                result = self._get_revision(connection, canonical_id, next_revision)
                if result.etag != next_etag:
                    raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

            self._insert_mutation(
                connection,
                request_id,
                "update",
                request_digest,
                canonical_id,
                result.revision,
                result.etag,
                updated_at,
            )
            return result

    def get(self, definition_id: str, revision: int | None = None) -> StrictDefinitionRevision:
        canonical_id = _validate_definition_id(definition_id)
        if revision is not None:
            _validate_revision(revision)
        with self._read_connection(required=False) as connection:
            if connection is None:
                raise DefinitionNotFound("workflow definition was not found")
            if revision is None:
                row = connection.execute(
                    "SELECT latest_revision FROM workflow_definitions WHERE definition_id = ?",
                    (canonical_id,),
                ).fetchone()
                if row is None:
                    raise DefinitionNotFound("workflow definition was not found")
                latest = row["latest_revision"]
                if type(latest) is not int or latest < 1:
                    raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
                revision = latest
            definition_row = connection.execute(
                "SELECT latest_revision FROM workflow_definitions WHERE definition_id = ?",
                (canonical_id,),
            ).fetchone()
            revision_row = connection.execute(
                """
                SELECT definition_id, revision, source_yaml, source_sha256,
                       canonical_sha256, max_live_codex_jobs, created_at
                FROM workflow_definition_revisions
                WHERE definition_id = ? AND revision = ?
                """,
                (canonical_id, revision),
            ).fetchone()
            if definition_row is None:
                if revision_row is not None:
                    raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
                raise DefinitionNotFound("workflow definition was not found")
            if revision_row is None:
                raise DefinitionNotFound("workflow definition was not found")
            latest = definition_row["latest_revision"]
            if type(latest) is not int or latest < revision:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            return self._revision_from_row(connection, revision_row)

    def _revision_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> StrictDefinitionRevision:
        try:
            base = self._validator._revision_from_row(row)
        except DefinitionError:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None

        contract_row = connection.execute(
            """
            SELECT contract_json_canonical, contract_sha256, binding_digest, created_at
            FROM workflow_definition_contracts
            WHERE definition_id = ? AND revision = ?
            """,
            (base.definition_id, base.revision),
        ).fetchone()
        if contract_row is None:
            strict_mutation = connection.execute(
                """
                SELECT 1
                FROM workflow_definition_mutations
                WHERE definition_id = ? AND result_revision = ?
                  AND result_etag GLOB '"workflow-definition-v2-*'
                LIMIT 1
                """,
                (base.definition_id, base.revision),
            ).fetchone()
            if strict_mutation is not None:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            return _strict_revision(base, None, None, None, None, base.etag)
        canonical = contract_row["contract_json_canonical"]
        stored_sha = contract_row["contract_sha256"]
        stored_binding = contract_row["binding_digest"]
        created_at = contract_row["created_at"]
        if (
            not isinstance(canonical, str)
            or not isinstance(stored_sha, str)
            or not _SHA256.fullmatch(stored_sha)
            or not isinstance(stored_binding, str)
            or not _SHA256.fullmatch(stored_binding)
            or created_at != base.created_at
        ):
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        try:
            parsed = parse_strict_json(canonical)
            compiled = compile_contract(parsed)
            if canonical_json(parsed) != canonical or compiled.contract_sha256 != stored_sha:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        except DefinitionError:
            raise
        except ContractError:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        except Exception:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        expected_binding = _binding_digest(
            base.definition_id,
            base.revision,
            base.source_sha256,
            base.canonical_sha256,
            base.max_live_codex_jobs,
            compiled.contract_version,
            compiled.contract_sha256,
        )
        if expected_binding != stored_binding:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        etag = _strict_etag(
            base.definition_id,
            base.revision,
            base.source_sha256,
            base.canonical_sha256,
            base.max_live_codex_jobs,
            compiled.contract_sha256,
            expected_binding,
        )
        return _strict_revision(
            base,
            compiled,
            canonical,
            compiled.contract_sha256,
            expected_binding,
            etag,
        )

    def _insert_revision(
        self,
        connection: sqlite3.Connection,
        definition_id: str,
        revision: int,
        validated: Any,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_definition_revisions (
                definition_id, revision, source_yaml, source_sha256,
                canonical_sha256, max_live_codex_jobs, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                definition_id,
                revision,
                validated.source_yaml,
                validated.source_sha256,
                validated.canonical_sha256,
                validated.max_live_codex_jobs,
                created_at,
            ),
        )

    def _insert_contract(
        self,
        connection: sqlite3.Connection,
        definition_id: str,
        revision: int,
        contract: CompiledContract,
        contract_sha256: str,
        binding_digest: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_definition_contracts (
                definition_id, revision, contract_json_canonical,
                contract_sha256, binding_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                definition_id,
                revision,
                contract.contract_json_canonical,
                contract_sha256,
                binding_digest,
                created_at,
            ),
        )

    def _insert_mutation(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        operation: str,
        request_digest: str,
        definition_id: str,
        result_revision: int,
        result_etag: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO workflow_definition_mutations (
                mutation_request_id, operation, request_digest, definition_id,
                result_revision, result_etag, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                operation,
                request_digest,
                definition_id,
                result_revision,
                result_etag,
                created_at,
            ),
        )

    @contextmanager
    def _read_connection(
        self, *, required: bool, version: int = _STRICT_DATABASE_SCHEMA_VERSION
    ) -> Iterator[sqlite3.Connection | None]:
        try:
            database = self._database_path(create=False, required=required)
            if database is None:
                yield None
                return
            with _readonly_connection(database) as connection:
                _check_schema(connection, version=version)
                yield connection
        except DefinitionError:
            raise
        except Exception:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            database = self._database_path(create=False, required=True)
            if database is None:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            connection = sqlite3.connect(
                database,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            self._configure_writable_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            _check_v2_schema(connection)
            yield connection
            connection.execute("COMMIT")
        except DefinitionError:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback(connection)
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()

    def _configure_writable_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    def _database_path(self, *, create: bool, required: bool) -> Path | None:
        root = self._validator._resolve_state_root(create=create)
        if root is None:
            if required:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            return None
        database = root / _DATABASE_NAME
        if _is_symlink_or_junction(database):
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        if not database.exists():
            if required:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            return None
        if not database.is_file():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        return database

    def _read_mutation_replay(
        self, request_id: str, operation: str, request_digest: str
    ) -> StrictDefinitionRevision | None:
        with self._read_connection(required=False) as connection:
            if connection is None:
                return None
            return self._mutation_replay(connection, request_id, operation, request_digest)

    def _mutation_replay(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        operation: str,
        request_digest: str,
    ) -> StrictDefinitionRevision | None:
        row = connection.execute(
            """
            SELECT operation, request_digest, definition_id,
                   result_revision, result_etag
            FROM workflow_definition_mutations
            WHERE mutation_request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_digest"] != request_digest:
            raise DefinitionMutationConflict(
                "mutation request ID was already used for a different request"
            )
        definition_id = row["definition_id"]
        revision = row["result_revision"]
        if not _is_canonical_uuid(definition_id) or type(revision) is not int or revision < 1:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        result = self._get_revision(connection, definition_id, revision)
        if row["result_etag"] != result.etag:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        return result

    def _get_revision(
        self, connection: sqlite3.Connection, definition_id: str, revision: int
    ) -> StrictDefinitionRevision:
        row = connection.execute(
            """
            SELECT definition_id, revision, source_yaml, source_sha256,
                   canonical_sha256, max_live_codex_jobs, created_at
            FROM workflow_definition_revisions
            WHERE definition_id = ? AND revision = ?
            """,
            (definition_id, revision),
        ).fetchone()
        if row is None:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        return self._revision_from_row(connection, row)


def _strict_revision(
    base: DefinitionRevision,
    contract: CompiledContract | None,
    canonical: str | None,
    contract_sha256: str | None,
    binding_digest: str | None,
    etag: str,
) -> StrictDefinitionRevision:
    return StrictDefinitionRevision(
        definition_id=base.definition_id,
        revision=base.revision,
        source_yaml=base.source_yaml,
        source_sha256=base.source_sha256,
        canonical_sha256=base.canonical_sha256,
        max_live_codex_jobs=base.max_live_codex_jobs,
        created_at=base.created_at,
        etag=etag,
        definition=base.definition,
        contract=contract,
        contract_json_canonical=canonical,
        contract_sha256=contract_sha256,
        binding_digest=binding_digest,
    )


def _compile_request_contract(raw: object) -> CompiledContract:
    try:
        return compile_contract(raw)
    except ContractError:
        raise DefinitionInvalid("workflow contract is invalid") from None
    except Exception:
        raise DefinitionInvalid("workflow contract is invalid") from None


def _contract_binding(
    definition_id: str,
    revision: int,
    source_sha256: str,
    canonical_sha256: str,
    max_live_codex_jobs: int,
    contract: CompiledContract,
) -> tuple[str, str]:
    contract_sha = contract.contract_sha256
    binding = _binding_digest(
        definition_id,
        revision,
        source_sha256,
        canonical_sha256,
        max_live_codex_jobs,
        contract.contract_version,
        contract_sha,
    )
    return contract_sha, binding


def _binding_digest(
    definition_id: str,
    revision: int,
    source_sha256: str,
    canonical_sha256: str,
    max_live_codex_jobs: int,
    contract_version: int,
    contract_sha256: str,
) -> str:
    payload = {
        "definition_id": definition_id,
        "revision": revision,
        "source_sha256": source_sha256,
        "canonical_sha256": canonical_sha256,
        "max_live_codex_jobs": max_live_codex_jobs,
        "contract_version": contract_version,
        "contract_sha256": contract_sha256,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _strict_etag(
    definition_id: str,
    revision: int,
    source_sha256: str,
    canonical_sha256: str,
    max_live_codex_jobs: int,
    contract_sha256: str,
    binding_digest: str,
) -> str:
    payload = {
        "domain": _STRICT_ETAG_DOMAIN,
        "definition_id": definition_id,
        "revision": revision,
        "source_sha256": source_sha256,
        "canonical_sha256": canonical_sha256,
        "max_live_codex_jobs": max_live_codex_jobs,
        "contract_version": 1,
        "contract_sha256": contract_sha256,
        "binding_digest": binding_digest,
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f'"workflow-definition-v2-{digest}"'


def _strict_mutation_digest(
    *,
    operation: str,
    definition_id: str | None,
    expected_revision: int | None,
    source_yaml: str,
    max_live_codex_jobs: int,
    contract: CompiledContract,
) -> str:
    payload = {
        "domain": _STRICT_MUTATION_DOMAIN,
        "mode": "strict",
        "operation": operation,
        "definition_id": definition_id,
        "expected_revision": expected_revision,
        "source_yaml": source_yaml,
        "max_live_codex_jobs": max_live_codex_jobs,
        "contract_json_canonical": contract.contract_json_canonical,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@contextmanager
def _readonly_connection(database: Path) -> Iterator[sqlite3.Connection]:
    connection: sqlite3.Connection | None = None
    try:
        if _is_symlink_or_junction(database) or not database.is_file():
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        uri = database.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA query_only=ON")
        yield connection
    except DefinitionError:
        raise
    except Exception:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()


def _contained_file_path(root: Path, path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
    if parent != root or _is_symlink_or_junction(candidate):
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
    return parent / candidate.name


def _check_schema(connection: sqlite3.Connection, *, version: int) -> None:
    if version == 1:
        _check_v1_schema(connection)
    elif version == 2:
        _check_v2_schema(connection)
    else:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _check_v1_schema(connection: sqlite3.Connection) -> None:
    _check_schema_objects(connection, version=1, include_contracts=False)


def _check_v2_schema(connection: sqlite3.Connection) -> None:
    _check_schema_objects(connection, version=2, include_contracts=True)


def _check_schema_objects(
    connection: sqlite3.Connection, *, version: int, include_contracts: bool
) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None or type(version_row[0]) is not int or version_row[0] != version:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
    actual_objects = {
        (row["type"], row["name"], row["tbl_name"], _normalize_sql(row["sql"]))
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    }
    if actual_objects != _expected_objects(include_contracts=include_contracts):
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    table_names = [
        "workflow_definitions",
        "workflow_definition_revisions",
        "workflow_definition_mutations",
    ]
    if include_contracts:
        table_names.append("workflow_definition_contracts")
    for table_name in table_names:
        info = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        actual_info = tuple(
            (
                row["cid"],
                row["name"],
                row["type"],
                row["notnull"],
                row["dflt_value"],
                row["pk"],
            )
            for row in info
        )
        if actual_info != _EXPECTED_TABLE_INFO[table_name]:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)

    _check_foreign_keys(connection, "workflow_definitions", ())
    _check_foreign_keys(
        connection,
        "workflow_definition_revisions",
        (
            (
                0,
                0,
                "workflow_definitions",
                "definition_id",
                "definition_id",
                "NO ACTION",
                "NO ACTION",
                "NONE",
            ),
        ),
    )
    _check_foreign_keys(connection, "workflow_definition_mutations", ())
    if include_contracts:
        _check_foreign_keys(
            connection,
            "workflow_definition_contracts",
            (
                (
                    0,
                    0,
                    "workflow_definition_revisions",
                    "definition_id",
                    "definition_id",
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
                (
                    0,
                    1,
                    "workflow_definition_revisions",
                    "revision",
                    "revision",
                    "NO ACTION",
                    "RESTRICT",
                    "NONE",
                ),
            ),
        )

    _check_primary_key_index(connection, "workflow_definitions", ("definition_id",))
    _check_primary_key_index(
        connection, "workflow_definition_revisions", ("definition_id", "revision")
    )
    _check_primary_key_index(connection, "workflow_definition_mutations", ("mutation_request_id",))
    if include_contracts:
        _check_primary_key_index(
            connection, "workflow_definition_contracts", ("definition_id", "revision")
        )

    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).lower() != "wal":
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _check_foreign_keys(
    connection: sqlite3.Connection,
    table_name: str,
    expected: tuple[tuple[object, ...], ...],
) -> None:
    rows = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
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
        for row in rows
    )
    if actual != expected:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _check_primary_key_index(
    connection: sqlite3.Connection, table_name: str, expected_columns: tuple[str, ...]
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    if (
        len(indexes) != 1
        or indexes[0]["unique"] != 1
        or indexes[0]["origin"] != "pk"
        or indexes[0]["partial"] != 0
    ):
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
    name = str(indexes[0]["name"]).replace('"', '""')
    columns = connection.execute(f'PRAGMA index_info("{name}")').fetchall()
    if tuple(row["name"] for row in columns) != expected_columns:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _check_integrity(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _expect_empty_database(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()
    objects = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    if version is None or version[0] != 0 or objects is not None:
        raise DefinitionRepositoryUnavailable(_UNAVAILABLE)


def _create_v2_schema(connection: sqlite3.Connection) -> None:
    for statement in (
        _CREATE_DEFINITIONS,
        _CREATE_REVISIONS,
        _CREATE_MUTATIONS,
        _CREATE_CONTRACTS,
    ):
        connection.execute(statement)


def _legacy_identity(
    connection: sqlite3.Connection, validator: WorkflowDefinitionRepository
) -> _LegacyIdentity:
    _check_integrity(connection)
    definitions = connection.execute(
        """
        SELECT definition_id, latest_revision, created_at, updated_at
        FROM workflow_definitions
        ORDER BY definition_id
        """
    ).fetchall()
    revisions_by_key: dict[tuple[str, int], DefinitionRevision] = {}
    digest = hashlib.sha256()
    revision_count = 0

    for definition_row in definitions:
        definition_id = definition_row["definition_id"]
        latest = definition_row["latest_revision"]
        if (
            not _is_canonical_uuid(definition_id)
            or type(latest) is not int
            or latest < 1
            or not isinstance(definition_row["created_at"], str)
            or not isinstance(definition_row["updated_at"], str)
        ):
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        rows = connection.execute(
            """
            SELECT definition_id, revision, source_yaml, source_sha256,
                   canonical_sha256, max_live_codex_jobs, created_at
            FROM workflow_definition_revisions
            WHERE definition_id = ?
            ORDER BY revision
            """,
            (definition_id,),
        ).fetchall()
        if len(rows) != latest:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        definition_revisions: list[DefinitionRevision] = []
        for expected_revision, row in enumerate(rows, start=1):
            if row["revision"] != expected_revision:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            try:
                checked = validator._revision_from_row(row)
            except DefinitionError:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE) from None
            revisions_by_key[(definition_id, expected_revision)] = checked
            definition_revisions.append(checked)
            revision_count += 1
        _update_identity_digest(
            digest,
            (
                definition_row["definition_id"],
                definition_row["latest_revision"],
                definition_row["created_at"],
                definition_row["updated_at"],
            ),
        )
        for checked in definition_revisions:
            _update_identity_digest(
                digest,
                (
                    checked.definition_id,
                    checked.revision,
                    checked.source_yaml,
                    checked.source_sha256,
                    checked.canonical_sha256,
                    checked.max_live_codex_jobs,
                    checked.created_at,
                    checked.etag,
                ),
            )

    mutations = connection.execute(
        """
        SELECT mutation_request_id, operation, request_digest, definition_id,
               result_revision, result_etag, created_at
        FROM workflow_definition_mutations
        ORDER BY mutation_request_id
        """
    ).fetchall()
    for row in mutations:
        request_id = row["mutation_request_id"]
        operation = row["operation"]
        request_digest = row["request_digest"]
        definition_id = row["definition_id"]
        revision = row["result_revision"]
        created_at = row["created_at"]
        if (
            not isinstance(request_id, str)
            or not _MUTATION_REQUEST_ID.fullmatch(request_id)
            or operation not in {"create", "update"}
            or not isinstance(request_digest, str)
            or not _SHA256.fullmatch(request_digest)
            or not _is_canonical_uuid(definition_id)
            or type(revision) is not int
            or not isinstance(created_at, str)
        ):
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        mutation_revision = revisions_by_key.get((definition_id, revision))
        if mutation_revision is None or row["result_etag"] != mutation_revision.etag:
            raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        if operation == "create":
            if revision != 1:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
            expected_digest = _mutation_digest(
                operation="create",
                definition_id=None,
                expected_revision=None,
                source_yaml=mutation_revision.source_yaml,
                max_live_codex_jobs=mutation_revision.max_live_codex_jobs,
            )
            if request_digest != expected_digest:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        else:
            candidate_digests = {
                _mutation_digest(
                    operation="update",
                    definition_id=definition_id,
                    expected_revision=candidate_revision,
                    source_yaml=mutation_revision.source_yaml,
                    max_live_codex_jobs=mutation_revision.max_live_codex_jobs,
                )
                for candidate_revision in {revision, max(1, revision - 1)}
            }
            if request_digest not in candidate_digests:
                raise DefinitionRepositoryUnavailable(_UNAVAILABLE)
        _update_identity_digest(
            digest,
            (
                request_id,
                operation,
                request_digest,
                definition_id,
                revision,
                row["result_etag"],
                created_at,
            ),
        )
    return _LegacyIdentity(
        digest=digest.hexdigest(),
        definition_count=len(definitions),
        revision_count=revision_count,
        mutation_count=len(mutations),
    )


def _update_identity_digest(digest: Any, value: object) -> None:
    digest.update(_canonical_json(value).encode("utf-8"))
    digest.update(b"\n")


def _migration_checkpoint(_stage: str) -> None:
    """Private test seam for transaction rollback boundary coverage."""
    return


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        return


__all__ = [
    "StrictDefinitionRevision",
    "StrictWorkflowDefinitionStore",
]
