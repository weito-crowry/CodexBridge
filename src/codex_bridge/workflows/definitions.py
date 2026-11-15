from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .interpreter import OwsError, parse_workflow_text, validate_workflow

_DATABASE_NAME = "definitions.sqlite"
_DATABASE_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 10_000
_SOURCE_YAML_MAX_BYTES = 64 * 1024
_MUTATION_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_STRONG_ETAG = re.compile(r'"workflow-definition-v1-[0-9a-f]{64}"\Z', re.ASCII)
_CURSOR_COMPONENT = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_CURSOR_MAX_LENGTH = 512
_SCHEMA_UNAVAILABLE_PREFIX = "could not load official OWS v1.0.3 schema"


class DefinitionError(Exception):
    """Base class for safe, transport-neutral workflow definition errors."""

    code = "internal"


class DefinitionInvalid(DefinitionError):
    """A request field or workflow definition is invalid."""

    code = "invalid"


class DefinitionNotFound(DefinitionError):
    """The requested definition or revision does not exist."""

    code = "not_found"


class DefinitionPreconditionFailed(DefinitionError):
    """The expected latest revision does not match the stored revision."""

    code = "precondition_failed"


class DefinitionMutationConflict(DefinitionError):
    """A mutation request ID was reused for a different request."""

    code = "mutation_conflict"


class DefinitionRepositoryUnavailable(DefinitionError):
    """The definition schema or persistent store is unavailable."""

    code = "unavailable"


@dataclass(frozen=True)
class DefinitionValidation:
    source_yaml: str
    definition: dict[str, Any]
    source_sha256: str
    canonical_sha256: str
    max_live_codex_jobs: int


@dataclass(frozen=True)
class DefinitionRevision:
    definition_id: str
    revision: int
    source_yaml: str
    source_sha256: str
    canonical_sha256: str
    max_live_codex_jobs: int
    created_at: str
    etag: str
    definition: dict[str, Any]


@dataclass(frozen=True)
class DefinitionSummary:
    definition_id: str
    latest_revision: int
    document_name: str
    source_sha256: str
    canonical_sha256: str
    max_live_codex_jobs: int
    created_at: str
    updated_at: str
    etag: str


@dataclass(frozen=True)
class DefinitionPage:
    items: tuple[DefinitionSummary, ...]
    next_cursor: str | None


class WorkflowDefinitionRepository:
    """Persist validated immutable workflow definition revisions."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = Path(state_root)

    def validate(self, source_yaml: str, max_live_codex_jobs: int) -> DefinitionValidation:
        source_bytes = _validate_source_fields(source_yaml, max_live_codex_jobs)
        try:
            definition = parse_workflow_text(source_yaml)
        except (OwsError, TypeError, ValueError, RecursionError):
            raise DefinitionInvalid("workflow definition is invalid") from None

        try:
            validate_workflow(
                definition,
                codex_job_live=True,
                max_live_codex_jobs=max_live_codex_jobs,
            )
        except OwsError as error:
            if str(error).startswith(_SCHEMA_UNAVAILABLE_PREFIX):
                raise DefinitionRepositoryUnavailable(
                    "workflow definition schema is unavailable"
                ) from None
            raise DefinitionInvalid("workflow definition is invalid") from None
        except Exception:
            raise DefinitionRepositoryUnavailable(
                "workflow definition validation is unavailable"
            ) from None

        try:
            canonical_json = _canonical_json(definition)
        except (TypeError, ValueError, RecursionError):
            raise DefinitionInvalid("workflow definition is invalid") from None

        return DefinitionValidation(
            source_yaml=source_yaml,
            definition=definition,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            canonical_sha256=hashlib.sha256((canonical_json + "\n").encode("utf-8")).hexdigest(),
            max_live_codex_jobs=max_live_codex_jobs,
        )

    def create(
        self,
        *,
        mutation_request_id: str,
        source_yaml: str,
        max_live_codex_jobs: int,
    ) -> DefinitionRevision:
        request_id = _validate_mutation_request_id(mutation_request_id)
        try:
            request_digest = _mutation_digest(
                operation="create",
                definition_id=None,
                expected_revision=None,
                source_yaml=source_yaml,
                max_live_codex_jobs=max_live_codex_jobs,
            )
        except (TypeError, ValueError):
            raise DefinitionInvalid("mutation request is invalid") from None

        replay = self._read_mutation_replay(request_id, "create", request_digest)
        if replay is not None:
            return replay

        _validate_source_fields(source_yaml, max_live_codex_jobs)
        validated = self.validate(source_yaml, max_live_codex_jobs)
        definition_id = str(uuid.uuid4())
        created_at = _utc_now()
        etag = _strong_etag(
            definition_id,
            1,
            validated.source_sha256,
            validated.max_live_codex_jobs,
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
            connection.execute(
                """
                INSERT INTO workflow_definition_revisions (
                    definition_id, revision, source_yaml, source_sha256,
                    canonical_sha256, max_live_codex_jobs, created_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    definition_id,
                    validated.source_yaml,
                    validated.source_sha256,
                    validated.canonical_sha256,
                    validated.max_live_codex_jobs,
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_definition_mutations (
                    mutation_request_id, operation, request_digest, definition_id,
                    result_revision, result_etag, created_at
                ) VALUES (?, 'create', ?, ?, 1, ?, ?)
                """,
                (request_id, request_digest, definition_id, etag, created_at),
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
    ) -> DefinitionRevision:
        request_id = _validate_mutation_request_id(mutation_request_id)
        try:
            request_digest = _mutation_digest(
                operation="update",
                definition_id=definition_id,
                expected_revision=expected_revision,
                source_yaml=source_yaml,
                max_live_codex_jobs=max_live_codex_jobs,
            )
        except (TypeError, ValueError):
            raise DefinitionInvalid("mutation request is invalid") from None

        replay = self._read_mutation_replay(request_id, "update", request_digest)
        if replay is not None:
            return replay

        canonical_id = _validate_definition_id(definition_id)
        _validate_revision(expected_revision)
        _validate_source_fields(source_yaml, max_live_codex_jobs)
        validated = self.validate(source_yaml, max_live_codex_jobs)
        updated_at = _utc_now()

        with self._write_transaction() as connection:
            replay = self._mutation_replay(connection, request_id, "update", request_digest)
            if replay is not None:
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
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            if latest_revision != expected_revision:
                raise DefinitionPreconditionFailed(
                    "workflow definition revision precondition failed"
                )

            current = self._get_revision(connection, canonical_id, latest_revision)
            if (
                current.source_yaml == validated.source_yaml
                and current.max_live_codex_jobs == validated.max_live_codex_jobs
            ):
                result = current
            else:
                next_revision = latest_revision + 1
                connection.execute(
                    """
                    INSERT INTO workflow_definition_revisions (
                        definition_id, revision, source_yaml, source_sha256,
                        canonical_sha256, max_live_codex_jobs, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_id,
                        next_revision,
                        validated.source_yaml,
                        validated.source_sha256,
                        validated.canonical_sha256,
                        validated.max_live_codex_jobs,
                        updated_at,
                    ),
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

            connection.execute(
                """
                INSERT INTO workflow_definition_mutations (
                    mutation_request_id, operation, request_digest, definition_id,
                    result_revision, result_etag, created_at
                ) VALUES (?, 'update', ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    request_digest,
                    canonical_id,
                    result.revision,
                    result.etag,
                    updated_at,
                ),
            )
            return result

    def get(self, definition_id: str, revision: int | None = None) -> DefinitionRevision:
        canonical_id = _validate_definition_id(definition_id)
        if revision is not None:
            _validate_revision(revision)

        with self._read_connection() as connection:
            if connection is None:
                raise DefinitionNotFound("workflow definition was not found")
            if revision is None:
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
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                return self._get_revision(connection, canonical_id, latest_revision)

            definition_row = connection.execute(
                """
                SELECT latest_revision
                FROM workflow_definitions
                WHERE definition_id = ?
                """,
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
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                raise DefinitionNotFound("workflow definition was not found")
            if revision_row is None:
                raise DefinitionNotFound("workflow definition was not found")
            latest_revision = definition_row["latest_revision"]
            if type(latest_revision) is not int or latest_revision < revision:
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            return self._revision_from_row(revision_row)

    def revision_for_etag(self, definition_id: str, etag: str) -> int | None:
        """Resolve a recorded strong ETag to its immutable revision without writing state."""
        canonical_id = _validate_definition_id(definition_id)
        if not isinstance(etag, str) or not _STRONG_ETAG.fullmatch(etag):
            raise DefinitionInvalid("workflow definition ETag is invalid")

        with self._read_connection() as connection:
            if connection is None:
                raise DefinitionNotFound("workflow definition was not found")
            definition_row = connection.execute(
                "SELECT 1 FROM workflow_definitions WHERE definition_id = ?",
                (canonical_id,),
            ).fetchone()
            if definition_row is None:
                raise DefinitionNotFound("workflow definition was not found")
            rows = connection.execute(
                """
                SELECT result_revision, result_etag
                FROM workflow_definition_mutations
                WHERE definition_id = ? AND result_etag = ?
                """,
                (canonical_id, etag),
            ).fetchall()
            if not rows:
                return None

            revisions: set[int] = set()
            for row in rows:
                revision = row["result_revision"]
                if type(revision) is not int or revision < 1 or row["result_etag"] != etag:
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                resolved = self._get_revision(connection, canonical_id, revision)
                if resolved.etag != etag:
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                revisions.add(revision)
            if len(revisions) != 1:
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            return next(iter(revisions))

    def list(self, limit: int = 50, cursor: str | None = None) -> DefinitionPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DefinitionInvalid("workflow definition list limit is invalid")
        after = _decode_cursor(cursor)

        with self._read_connection() as connection:
            if connection is None:
                return DefinitionPage(items=(), next_cursor=None)
            query = """
                SELECT d.definition_id, d.latest_revision,
                       d.created_at AS definition_created_at,
                       d.updated_at AS definition_updated_at,
                       r.definition_id AS revision_definition_id,
                       r.revision, r.source_yaml, r.source_sha256,
                       r.canonical_sha256, r.max_live_codex_jobs, r.created_at
                FROM workflow_definitions AS d
                LEFT JOIN workflow_definition_revisions AS r
                  ON r.definition_id = d.definition_id
                 AND r.revision = d.latest_revision
            """
            if after is None:
                rows = connection.execute(
                    query + " ORDER BY d.created_at DESC, d.definition_id DESC LIMIT ?",
                    (limit + 1,),
                ).fetchall()
            else:
                last_created_at, last_definition_id = after
                rows = connection.execute(
                    query
                    + """
                    WHERE d.created_at < ?
                       OR (d.created_at = ? AND d.definition_id < ?)
                    ORDER BY d.created_at DESC, d.definition_id DESC
                    LIMIT ?
                    """,
                    (
                        last_created_at,
                        last_created_at,
                        last_definition_id,
                        limit + 1,
                    ),
                ).fetchall()

            has_more = len(rows) > limit
            page_rows = rows[:limit]
            items: list[DefinitionSummary] = []
            for row in page_rows:
                if row["revision_definition_id"] is None:
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                latest_revision = row["latest_revision"]
                if type(latest_revision) is not int or row["revision"] != latest_revision:
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                revision = self._revision_from_row(row)
                document = revision.definition.get("document")
                document_name = document.get("name") if isinstance(document, dict) else None
                if not isinstance(document_name, str):
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                definition_created_at = row["definition_created_at"]
                definition_updated_at = row["definition_updated_at"]
                if (
                    not isinstance(definition_created_at, str)
                    or not _is_canonical_utc_timestamp(definition_created_at)
                    or not isinstance(definition_updated_at, str)
                ):
                    raise DefinitionRepositoryUnavailable(
                        "workflow definition storage is unavailable"
                    )
                items.append(
                    DefinitionSummary(
                        definition_id=revision.definition_id,
                        latest_revision=revision.revision,
                        document_name=document_name,
                        source_sha256=revision.source_sha256,
                        canonical_sha256=revision.canonical_sha256,
                        max_live_codex_jobs=revision.max_live_codex_jobs,
                        created_at=definition_created_at,
                        updated_at=definition_updated_at,
                        etag=revision.etag,
                    )
                )
            next_cursor = (
                _encode_cursor(items[-1].created_at, items[-1].definition_id)
                if has_more and items
                else None
            )
            return DefinitionPage(items=tuple(items), next_cursor=next_cursor)

    def _read_mutation_replay(
        self,
        mutation_request_id: str,
        operation: str,
        request_digest: str,
    ) -> DefinitionRevision | None:
        with self._read_connection() as connection:
            if connection is None:
                return None
            return self._mutation_replay(connection, mutation_request_id, operation, request_digest)

    def _mutation_replay(
        self,
        connection: sqlite3.Connection,
        mutation_request_id: str,
        operation: str,
        request_digest: str,
    ) -> DefinitionRevision | None:
        row = connection.execute(
            """
            SELECT operation, request_digest, definition_id,
                   result_revision, result_etag
            FROM workflow_definition_mutations
            WHERE mutation_request_id = ?
            """,
            (mutation_request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_digest"] != request_digest:
            raise DefinitionMutationConflict(
                "mutation request ID was already used for a different request"
            )
        definition_id = row["definition_id"]
        result_revision = row["result_revision"]
        if not _is_canonical_uuid(definition_id) or type(result_revision) is not int:
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        result = self._get_revision(connection, definition_id, result_revision)
        if row["result_etag"] != result.etag:
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        return result

    def _get_revision(
        self, connection: sqlite3.Connection, definition_id: str, revision: int
    ) -> DefinitionRevision:
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
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        return self._revision_from_row(row)

    def _revision_from_row(self, row: sqlite3.Row) -> DefinitionRevision:
        definition_id = row["definition_id"]
        revision = row["revision"]
        source_yaml = row["source_yaml"]
        source_sha256 = row["source_sha256"]
        canonical_sha256 = row["canonical_sha256"]
        max_live_codex_jobs = row["max_live_codex_jobs"]
        created_at = row["created_at"]
        if (
            not _is_canonical_uuid(definition_id)
            or type(revision) is not int
            or revision < 1
            or not isinstance(source_yaml, str)
            or not isinstance(source_sha256, str)
            or not _SHA256.fullmatch(source_sha256)
            or not isinstance(canonical_sha256, str)
            or not _SHA256.fullmatch(canonical_sha256)
            or type(max_live_codex_jobs) is not int
            or max_live_codex_jobs not in {1, 2, 3}
            or not isinstance(created_at, str)
        ):
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        try:
            validated = self.validate(source_yaml, max_live_codex_jobs)
        except DefinitionError:
            raise DefinitionRepositoryUnavailable(
                "workflow definition storage is unavailable"
            ) from None
        if (
            validated.source_sha256 != source_sha256
            or validated.canonical_sha256 != canonical_sha256
        ):
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        etag = _strong_etag(
            definition_id,
            revision,
            source_sha256,
            max_live_codex_jobs,
        )
        return DefinitionRevision(
            definition_id=definition_id,
            revision=revision,
            source_yaml=source_yaml,
            source_sha256=source_sha256,
            canonical_sha256=canonical_sha256,
            max_live_codex_jobs=max_live_codex_jobs,
            created_at=created_at,
            etag=etag,
            definition=validated.definition,
        )

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection | None]:
        connection: sqlite3.Connection | None = None
        try:
            root = self._resolve_state_root(create=False)
            if root is None:
                yield None
                return
            database = root / _DATABASE_NAME
            if _is_symlink_or_junction(database):
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            if not database.exists():
                yield None
                return
            if not database.is_file():
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            database_uri = database.resolve(strict=True).as_uri() + "?mode=ro"
            connection = sqlite3.connect(
                database_uri,
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA query_only=ON")
            _check_database_schema(connection)
            yield connection
        except DefinitionError:
            raise
        except Exception:
            raise DefinitionRepositoryUnavailable(
                "workflow definition storage is unavailable"
            ) from None
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            root = self._resolve_state_root(create=True)
            if root is None:
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            database = root / _DATABASE_NAME
            if _is_symlink_or_junction(database) or (database.exists() and not database.is_file()):
                raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
            database_preexisted = database.exists()
            connection = sqlite3.connect(
                database,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=WAL")
            if not database_preexisted and os.name != "nt":
                database.chmod(0o600)
            connection.execute("BEGIN IMMEDIATE")
            _initialize_or_check_database_schema(connection)
            connection.execute("COMMIT")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except DefinitionError:
            if connection is not None and connection.in_transaction:
                _rollback_transaction(connection)
            raise
        except Exception:
            if connection is not None and connection.in_transaction:
                _rollback_transaction(connection)
            raise DefinitionRepositoryUnavailable(
                "workflow definition storage is unavailable"
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def _resolve_state_root(self, *, create: bool) -> Path | None:
        candidate = self._state_root.expanduser()
        try:
            if _is_symlink_or_junction(candidate):
                raise DefinitionRepositoryUnavailable("workflow definition state root is invalid")
            if create:
                candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not candidate.exists():
                return None
            if _is_symlink_or_junction(candidate):
                raise DefinitionRepositoryUnavailable("workflow definition state root is invalid")
            canonical = candidate.resolve(strict=True)
            if not canonical.is_dir():
                raise DefinitionRepositoryUnavailable("workflow definition state root is invalid")
            return canonical
        except DefinitionError:
            raise
        except Exception:
            raise DefinitionRepositoryUnavailable(
                "workflow definition state root is unavailable"
            ) from None


def _validate_source_fields(source_yaml: str, max_live_codex_jobs: int) -> bytes:
    if not isinstance(source_yaml, str) or not source_yaml.strip():
        raise DefinitionInvalid("workflow source YAML is invalid")
    if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
        raise DefinitionInvalid("max_live_codex_jobs must be the integer 1, 2, or 3")
    try:
        source_bytes = source_yaml.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise DefinitionInvalid("workflow source YAML is invalid") from None
    if len(source_bytes) > _SOURCE_YAML_MAX_BYTES:
        raise DefinitionInvalid("workflow source YAML exceeds the 64 KiB limit")
    return source_bytes


def _validate_mutation_request_id(value: str) -> str:
    if not isinstance(value, str) or not _MUTATION_REQUEST_ID.fullmatch(value):
        raise DefinitionInvalid("mutation request ID is invalid")
    return value


def _validate_definition_id(value: str) -> str:
    if not _is_canonical_uuid(value):
        raise DefinitionInvalid("definition ID is invalid")
    return value


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36 or value.lower() != value:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _validate_revision(value: int) -> None:
    if type(value) is not int or value < 1:
        raise DefinitionInvalid("revision must be a positive integer")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _mutation_digest(
    *,
    operation: str,
    definition_id: str | None,
    expected_revision: int | None,
    source_yaml: str,
    max_live_codex_jobs: int,
) -> str:
    payload = {
        "operation": operation,
        "definition_id": definition_id,
        "expected_revision": expected_revision,
        "source_yaml": source_yaml,
        "max_live_codex_jobs": max_live_codex_jobs,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _strong_etag(
    definition_id: str,
    revision: int,
    source_sha256: str,
    max_live_codex_jobs: int,
) -> str:
    payload = {
        "version": 1,
        "definition_id": definition_id,
        "revision": revision,
        "source_sha256": source_sha256,
        "max_live_codex_jobs": max_live_codex_jobs,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f'"workflow-definition-v1-{digest}"'


def _encode_cursor(created_at: str, definition_id: str) -> str:
    payload = _canonical_json(
        {
            "created_at": created_at,
            "definition_id": definition_id,
        }
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"v1:{encoded}"


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or len(cursor) > _CURSOR_MAX_LENGTH
        or not cursor.startswith("v1:")
    ):
        raise DefinitionInvalid("workflow definition cursor is invalid")
    encoded = cursor[3:]
    if not _CURSOR_COMPONENT.fullmatch(encoded):
        raise DefinitionInvalid("workflow definition cursor is invalid")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        raise DefinitionInvalid("workflow definition cursor is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"created_at", "definition_id"}
        or not isinstance(payload["created_at"], str)
        or not _is_canonical_utc_timestamp(payload["created_at"])
        or not _is_canonical_uuid(payload["definition_id"])
        or _encode_cursor(payload["created_at"], payload["definition_id"]) != cursor
    ):
        raise DefinitionInvalid("workflow definition cursor is invalid")
    return payload["created_at"], payload["definition_id"]


def _is_canonical_utc_timestamp(value: str) -> bool:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return False
    return timestamp.utcoffset() == timedelta(0) and value in {
        timestamp.isoformat(),
        timestamp.isoformat(timespec="microseconds"),
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


_EXPECTED_COLUMNS = {
    "workflow_definitions": {
        "definition_id",
        "latest_revision",
        "created_at",
        "updated_at",
    },
    "workflow_definition_revisions": {
        "definition_id",
        "revision",
        "source_yaml",
        "source_sha256",
        "canonical_sha256",
        "max_live_codex_jobs",
        "created_at",
    },
    "workflow_definition_mutations": {
        "mutation_request_id",
        "operation",
        "request_digest",
        "definition_id",
        "result_revision",
        "result_etag",
        "created_at",
    },
}


def _initialize_or_check_database_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None or type(version_row[0]) is not int:
        raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
    version = version_row[0]
    if version == 0:
        existing_tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
        }
        if existing_tables:
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
        connection.execute(
            """
            CREATE TABLE workflow_definitions (
                definition_id TEXT PRIMARY KEY,
                latest_revision INTEGER NOT NULL CHECK (latest_revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
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
        )
        connection.execute(
            """
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
        )
        connection.execute("PRAGMA user_version=1")
    elif version != _DATABASE_SCHEMA_VERSION:
        raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
    _check_database_schema(connection)


def _check_database_schema(connection: sqlite3.Connection) -> None:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None or version_row[0] != _DATABASE_SCHEMA_VERSION:
        raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
    table_names = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    if table_names != set(_EXPECTED_COLUMNS):
        raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
    for table_name, expected_columns in _EXPECTED_COLUMNS.items():
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if columns != expected_columns:
            raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if journal_mode is None or str(journal_mode[0]).lower() != "wal":
        raise DefinitionRepositoryUnavailable("workflow definition storage is unavailable")


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _rollback_transaction(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
    except sqlite3.Error:
        return
