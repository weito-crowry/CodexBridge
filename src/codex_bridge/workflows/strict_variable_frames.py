"""Persistent lifecycle operations for run-local strict workflow scope frames.

The store is opt-in and only attaches to an already initialized I4-A
variables.sqlite. It owns ScopeFrame lifecycle only; it never writes
variable_states or variable_mutations.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contracts import ContractError, canonical_json, parse_strict_json
from .strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableConflict,
    StrictWorkflowVariableError,
    StrictWorkflowVariableInvalidMetadata,
    StrictWorkflowVariableStorageUnavailable,
    StrictWorkflowVariableStore,
    _check_integrity,
    _check_schema,
    _configure_write_connection,
    _format_utc_timestamp,
    _parse_utc_timestamp,
    _read_connection,
    _rollback,
    _validate_binding,
)

_ROOT_FRAME_ID = "workflow-root"
_ROOT_SCOPE_PATH = "workflow"
_ROOT_ITERATION_VECTOR = "[]"
_MAX_FRAMES = 1024
_MAX_DEPTH = 24
_FAULT_INJECTOR = Callable[[str], None]


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowScopeFrameInfo:
    run_id: str = field(repr=False)
    frame_id: str = field(repr=False)
    parent_frame_id: str | None = field(repr=False)
    scope_path: str = field(repr=False)
    iteration_vector_json: str = field(repr=False)
    status: str = field(repr=False)
    created_at: str = field(repr=False)
    closed_at: str | None = field(repr=False)

    @property
    def iteration_vector(self) -> tuple[int, ...]:
        value = parse_strict_json(self.iteration_vector_json)
        return tuple(value)

    def __repr__(self) -> str:
        return "StrictWorkflowScopeFrameInfo(<verified>)"


@dataclass(frozen=True, slots=True, repr=False)
class StrictWorkflowFrameDatabaseInfo:
    run_id: str = field(repr=False)
    workflow_id: str = field(repr=False)
    definition_id: str = field(repr=False)
    definition_revision: int = field(repr=False)
    contract_sha256: str = field(repr=False)
    binding_digest: str = field(repr=False)
    normalized_input_sha256: str = field(repr=False)
    created_at: str = field(repr=False)
    root_frame_id: str = field(repr=False)
    frame_count: int = field(repr=False)
    maximum_depth: int = field(repr=False)
    frames: tuple[StrictWorkflowScopeFrameInfo, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "StrictWorkflowFrameDatabaseInfo(<verified>)"


class StrictWorkflowFrameStore:
    """Opt-in ScopeFrame lifecycle for one expected run binding."""

    def __init__(
        self,
        run_directory: Path,
        expected_binding: StrictWorkflowRunBindingInput,
        *,
        _fault_injector: _FAULT_INJECTOR | None = None,
    ) -> None:
        self._variable_store = StrictWorkflowVariableStore(run_directory)
        self._expected_binding = expected_binding
        self._expected_created_at = _validate_binding(expected_binding)
        self._fault_injector = _fault_injector

    def enter(
        self,
        parent_frame_id: str,
        scope_path: str,
        iteration_vector: list[int],
        created_at: datetime,
    ) -> StrictWorkflowScopeFrameInfo:
        """Create a deterministic child frame, replaying only an ACTIVE match."""
        if type(parent_frame_id) is not str or not parent_frame_id:
            raise StrictWorkflowVariableInvalidMetadata()
        created = _canonical_timestamp(created_at)
        vector = _canonical_iteration_vector(iteration_vector)
        _validate_scope_path_value(scope_path)
        frame_id = _child_frame_id(
            self._expected_binding.run_id,
            parent_frame_id,
            scope_path,
            vector,
        )

        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._open_write_connection()
            connection.execute("BEGIN IMMEDIATE")
            binding_created, rows, depths = self._validate_existing(connection)
            by_id = {row["frame_id"]: row for row in rows}
            parent = by_id.get(parent_frame_id)
            if parent is None or parent["status"] != "ACTIVE":
                raise StrictWorkflowVariableConflict()
            parent_vector = _decode_iteration_vector(parent["iteration_vector_json"])
            child_vector = _decode_iteration_vector(vector)
            if (
                len(parent_vector) > len(child_vector)
                or child_vector[: len(parent_vector)] != parent_vector
            ):
                raise StrictWorkflowVariableInvalidMetadata()
            if not _is_static_child_scope(parent["scope_path"], scope_path):
                raise StrictWorkflowVariableInvalidMetadata()

            existing = by_id.get(frame_id)
            if existing is not None:
                if (
                    existing["run_id"] != self._expected_binding.run_id
                    or existing["parent_frame_id"] != parent_frame_id
                    or existing["scope_path"] != scope_path
                    or existing["iteration_vector_json"] != vector
                ):
                    raise StrictWorkflowVariableConflict()
                if existing["status"] != "ACTIVE":
                    raise StrictWorkflowVariableConflict()
                info = _frame_info(existing)
                connection.execute("COMMIT")
                committed = True
                return info

            if len(rows) >= _MAX_FRAMES or depths[parent_frame_id] + 1 > _MAX_DEPTH:
                raise StrictWorkflowVariableConflict()
            connection.execute(
                """
                INSERT INTO scope_frames (
                    run_id, frame_id, parent_frame_id, scope_path,
                    iteration_vector_json, status, created_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, NULL)
                """,
                (
                    self._expected_binding.run_id,
                    frame_id,
                    parent_frame_id,
                    scope_path,
                    vector,
                    created,
                ),
            )
            _fault_checkpoint(self._fault_injector, "after_frame_insert")
            row = connection.execute(
                "SELECT * FROM scope_frames WHERE run_id=? AND frame_id=?",
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if row is None:
                raise StrictWorkflowVariableStorageUnavailable()
            info = _frame_info(row)
            _fault_checkpoint(self._fault_injector, "before_commit")
            connection.execute("COMMIT")
            committed = True
            return info
        except StrictWorkflowVariableError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            _close_connection(connection, committed)

    def close(self, frame_id: str, closed_at: datetime) -> StrictWorkflowScopeFrameInfo:
        """Close a non-root ACTIVE frame after all its children have closed."""
        if type(frame_id) is not str or not frame_id:
            raise StrictWorkflowVariableInvalidMetadata()
        closed = _canonical_timestamp(closed_at)

        connection: sqlite3.Connection | None = None
        committed = False
        try:
            connection = self._open_write_connection()
            connection.execute("BEGIN IMMEDIATE")
            _, rows, _ = self._validate_existing(connection)
            by_id = {row["frame_id"]: row for row in rows}
            frame = by_id.get(frame_id)
            if frame is None:
                raise StrictWorkflowVariableConflict()
            if frame_id == _ROOT_FRAME_ID or frame["parent_frame_id"] is None:
                raise StrictWorkflowVariableConflict()
            if frame["status"] == "CLOSED":
                if frame["closed_at"] != closed:
                    raise StrictWorkflowVariableConflict()
                info = _frame_info(frame)
                connection.execute("COMMIT")
                committed = True
                return info
            active_child = connection.execute(
                """
                SELECT 1 FROM scope_frames
                WHERE run_id=? AND parent_frame_id=? AND status='ACTIVE'
                LIMIT 1
                """,
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if active_child is not None:
                raise StrictWorkflowVariableConflict()
            connection.execute(
                """
                UPDATE scope_frames
                SET status='CLOSED', closed_at=?
                WHERE run_id=? AND frame_id=? AND status='ACTIVE'
                """,
                (closed, self._expected_binding.run_id, frame_id),
            )
            _fault_checkpoint(self._fault_injector, "after_frame_close")
            row = connection.execute(
                "SELECT * FROM scope_frames WHERE run_id=? AND frame_id=?",
                (self._expected_binding.run_id, frame_id),
            ).fetchone()
            if row is None:
                raise StrictWorkflowVariableStorageUnavailable()
            info = _frame_info(row)
            _fault_checkpoint(self._fault_injector, "before_commit")
            connection.execute("COMMIT")
            committed = True
            return info
        except StrictWorkflowVariableError:
            _rollback(connection)
            raise
        except Exception:
            _rollback(connection)
            raise StrictWorkflowVariableStorageUnavailable() from None
        finally:
            _close_connection(connection, committed)

    def inspect_all(self) -> StrictWorkflowFrameDatabaseInfo:
        """Read-only strict validation of the complete I4-B frame history."""
        database = self._variable_store._database_path(required=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = _read_connection(database)
            binding_created, rows, depths = self._validate_existing(connection)
            if connection.execute("SELECT 1 FROM variable_states LIMIT 1").fetchone() is not None:
                raise StrictWorkflowVariableStorageUnavailable()
            if (
                connection.execute("SELECT 1 FROM variable_mutations LIMIT 1").fetchone()
                is not None
            ):
                raise StrictWorkflowVariableStorageUnavailable()
            ordered = tuple(
                _frame_info(row) for row in sorted(rows, key=lambda item: item["frame_id"])
            )
            return StrictWorkflowFrameDatabaseInfo(
                run_id=self._expected_binding.run_id,
                workflow_id=self._expected_binding.workflow_id,
                definition_id=self._expected_binding.definition_id,
                definition_revision=self._expected_binding.definition_revision,
                contract_sha256=self._expected_binding.contract_sha256,
                binding_digest=self._expected_binding.binding_digest,
                normalized_input_sha256=self._expected_binding.normalized_input_sha256,
                created_at=binding_created,
                root_frame_id=_ROOT_FRAME_ID,
                frame_count=len(rows),
                maximum_depth=max(depths.values(), default=0),
                frames=ordered,
            )
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

    def _open_write_connection(self) -> sqlite3.Connection:
        database = self._variable_store._database_path(required=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                database.resolve(strict=True).as_uri() + "?mode=rw",
                uri=True,
                timeout=10.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            _configure_write_connection(connection)
            return connection
        except StrictWorkflowVariableError:
            if connection is not None:
                connection.close()
            raise
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            raise StrictWorkflowVariableStorageUnavailable() from None

    def _validate_existing(
        self, connection: sqlite3.Connection
    ) -> tuple[str, list[sqlite3.Row], dict[str, int]]:
        _check_schema(connection)
        _check_integrity(connection)
        if connection.execute("SELECT 1 FROM variable_states LIMIT 1").fetchone() is not None:
            raise StrictWorkflowVariableStorageUnavailable()
        if connection.execute("SELECT 1 FROM variable_mutations LIMIT 1").fetchone() is not None:
            raise StrictWorkflowVariableStorageUnavailable()
        binding_created = _verify_binding(
            connection, self._expected_binding, self._expected_created_at
        )
        rows = connection.execute("SELECT * FROM scope_frames").fetchall()
        depths = _validate_frames(rows, self._expected_binding.run_id, binding_created)
        return binding_created, rows, depths


def _verify_binding(
    connection: sqlite3.Connection,
    expected: StrictWorkflowRunBindingInput,
    expected_created_at: str,
) -> str:
    rows = connection.execute("SELECT * FROM run_binding").fetchall()
    if len(rows) != 1:
        raise StrictWorkflowVariableStorageUnavailable()
    row = rows[0]
    try:
        created_at = _format_utc_timestamp(_parse_utc_timestamp(row["created_at"]))
    except Exception:
        raise StrictWorkflowVariableStorageUnavailable() from None
    identity = (
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
        created_at,
    )
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
    if identity != expected_identity:
        raise StrictWorkflowVariableConflict()
    return created_at


def _validate_frames(
    rows: list[sqlite3.Row], run_id: str, binding_created_at: str
) -> dict[str, int]:
    if not rows or len(rows) > _MAX_FRAMES:
        raise StrictWorkflowVariableStorageUnavailable()
    by_id: dict[str, sqlite3.Row] = {}
    vectors: dict[str, tuple[int, ...]] = {}
    children: dict[str, list[str]] = {}
    roots: list[sqlite3.Row] = []

    for row in rows:
        if (
            type(row["run_id"]) is not str
            or row["run_id"] != run_id
            or type(row["frame_id"]) is not str
            or not row["frame_id"]
            or (row["parent_frame_id"] is not None and type(row["parent_frame_id"]) is not str)
            or type(row["scope_path"]) is not str
            or type(row["iteration_vector_json"]) is not str
            or type(row["status"]) is not str
            or row["status"] not in {"ACTIVE", "CLOSED"}
            or type(row["created_at"]) is not str
            or (row["closed_at"] is not None and type(row["closed_at"]) is not str)
            or row["frame_id"] in by_id
        ):
            raise StrictWorkflowVariableStorageUnavailable()
        if row["parent_frame_id"] is None:
            if row["scope_path"] != _ROOT_SCOPE_PATH:
                raise StrictWorkflowVariableStorageUnavailable()
        else:
            _validate_scope_path_value(row["scope_path"], storage=True)
        try:
            vector = _decode_iteration_vector(row["iteration_vector_json"])
            created_at = _format_utc_timestamp(_parse_utc_timestamp(row["created_at"]))
            closed_at = (
                None
                if row["closed_at"] is None
                else _format_utc_timestamp(_parse_utc_timestamp(row["closed_at"]))
            )
        except Exception:
            raise StrictWorkflowVariableStorageUnavailable() from None
        if created_at != row["created_at"] or closed_at != row["closed_at"]:
            raise StrictWorkflowVariableStorageUnavailable()
        if row["status"] == "ACTIVE" and closed_at is not None:
            raise StrictWorkflowVariableStorageUnavailable()
        if row["status"] == "CLOSED" and closed_at is None:
            raise StrictWorkflowVariableStorageUnavailable()
        by_id[row["frame_id"]] = row
        vectors[row["frame_id"]] = vector
        if row["parent_frame_id"] is None:
            roots.append(row)
        else:
            children.setdefault(row["parent_frame_id"], []).append(row["frame_id"])

    if len(roots) != 1:
        raise StrictWorkflowVariableStorageUnavailable()
    root = roots[0]
    if (
        root["frame_id"] != _ROOT_FRAME_ID
        or root["scope_path"] != _ROOT_SCOPE_PATH
        or root["iteration_vector_json"] != _ROOT_ITERATION_VECTOR
        or root["status"] != "ACTIVE"
        or root["closed_at"] is not None
        or root["created_at"] != binding_created_at
    ):
        raise StrictWorkflowVariableStorageUnavailable()

    for row in rows:
        parent_id = row["parent_frame_id"]
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            raise StrictWorkflowVariableStorageUnavailable()
        if not _is_static_child_scope(parent["scope_path"], row["scope_path"]):
            raise StrictWorkflowVariableStorageUnavailable()
        parent_vector = vectors[parent_id]
        vector = vectors[row["frame_id"]]
        if len(vector) < len(parent_vector) or vector[: len(parent_vector)] != parent_vector:
            raise StrictWorkflowVariableStorageUnavailable()
        if parent["status"] == "CLOSED" and row["status"] == "ACTIVE":
            raise StrictWorkflowVariableStorageUnavailable()
        expected_id = _child_frame_id(
            run_id,
            parent_id,
            row["scope_path"],
            row["iteration_vector_json"],
        )
        if row["frame_id"] != expected_id:
            raise StrictWorkflowVariableStorageUnavailable()

    depths: dict[str, int] = {_ROOT_FRAME_ID: 0}
    pending = [_ROOT_FRAME_ID]
    while pending:
        parent_id = pending.pop()
        for child_id in children.get(parent_id, []):
            if child_id in depths:
                raise StrictWorkflowVariableStorageUnavailable()
            depth = depths[parent_id] + 1
            if depth > _MAX_DEPTH:
                raise StrictWorkflowVariableStorageUnavailable()
            depths[child_id] = depth
            pending.append(child_id)
    if len(depths) != len(rows):
        raise StrictWorkflowVariableStorageUnavailable()
    return depths


def _canonical_timestamp(value: datetime) -> str:
    if type(value) is not datetime:
        raise StrictWorkflowVariableInvalidMetadata()
    try:
        offset = value.utcoffset()
    except Exception:
        raise StrictWorkflowVariableInvalidMetadata() from None
    if offset is None or offset != timedelta(0):
        raise StrictWorkflowVariableInvalidMetadata()
    try:
        return _format_utc_timestamp(value.astimezone(UTC))
    except Exception:
        raise StrictWorkflowVariableInvalidMetadata() from None


def _canonical_iteration_vector(value: list[int]) -> str:
    if type(value) is not list or any(type(item) is not int or item < 0 for item in value):
        raise StrictWorkflowVariableInvalidMetadata()
    try:
        return canonical_json(value)
    except ContractError:
        raise StrictWorkflowVariableInvalidMetadata() from None


def _decode_iteration_vector(value: str) -> tuple[int, ...]:
    parsed = parse_strict_json(value)
    if (
        type(parsed) is not list
        or any(type(item) is not int or item < 0 for item in parsed)
        or canonical_json(parsed) != value
    ):
        raise StrictWorkflowVariableStorageUnavailable()
    return tuple(parsed)


def _validate_scope_path_value(value: str, *, storage: bool = False) -> None:
    invalid = (
        StrictWorkflowVariableStorageUnavailable
        if storage
        else StrictWorkflowVariableInvalidMetadata
    )
    if type(value) is not str or not value or value == _ROOT_SCOPE_PATH:
        raise invalid()
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise invalid() from None
    if not value.startswith("/") or value.startswith("//") or value.endswith("/"):
        raise invalid()
    if "//" in value:
        raise invalid()


def _is_static_child_scope(parent_scope: str, child_scope: str) -> bool:
    if parent_scope == _ROOT_SCOPE_PATH:
        return child_scope.startswith("/") and child_scope != "/"
    return child_scope.startswith(parent_scope + "/") and len(child_scope) > len(parent_scope) + 1


def _child_frame_id(
    run_id: str, parent_frame_id: str, scope_path: str, iteration_vector_json: str
) -> str:
    vector = _decode_iteration_vector(iteration_vector_json)
    identity = {
        "run_id": run_id,
        "parent_frame_id": parent_frame_id,
        "scope_path": scope_path,
        "iteration_vector": list(vector),
    }
    return "frame-" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _frame_info(row: sqlite3.Row) -> StrictWorkflowScopeFrameInfo:
    return StrictWorkflowScopeFrameInfo(
        run_id=row["run_id"],
        frame_id=row["frame_id"],
        parent_frame_id=row["parent_frame_id"],
        scope_path=row["scope_path"],
        iteration_vector_json=row["iteration_vector_json"],
        status=row["status"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def _fault_checkpoint(injector: _FAULT_INJECTOR | None, name: str) -> None:
    if injector is not None:
        injector(name)


def _close_connection(connection: sqlite3.Connection | None, committed: bool) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except sqlite3.Error:
        if committed:
            raise StrictWorkflowVariableStorageUnavailable() from None


__all__ = [
    "StrictWorkflowFrameDatabaseInfo",
    "StrictWorkflowFrameStore",
    "StrictWorkflowScopeFrameInfo",
]
