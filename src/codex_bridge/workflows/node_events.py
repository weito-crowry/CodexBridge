from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

from .interpreter import _InterpreterEvent

MAX_NODE_EVENTS = 60_000
MAX_EVENT_JSON_BYTES = 1_024
MAX_JOURNAL_BYTES = 24 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 250
SCHEMA_VERSION = 1

_LOGGER = logging.getLogger(__name__)
_TABLE_COLUMNS = (
    "event_id",
    "event_key",
    "workflow_id",
    "run_id",
    "schema_version",
    "node_id",
    "task_identity",
    "execution_id",
    "event_type",
    "occurred_at",
    "source",
    "job_id",
    "metadata_json",
)
_TASK_EVENT_TYPES = frozenset({"task_started", "task_completed", "task_failed"})
_SCOPE_EVENT_TYPES = frozenset({"scope_entered", "scope_exited"})
_ITERATION_EVENT_TYPES = frozenset({"iteration_started", "iteration_completed"})
_EVENT_TYPES = (
    _TASK_EVENT_TYPES
    | _SCOPE_EVENT_TYPES
    | _ITERATION_EVENT_TYPES
    | {"branch_selected", "workflow_completed"}
)
_OPERATIONS = frozenset({"call", "do", "for", "set", "switch"})
_BRANCH_CLASSES = frozenset({"continue", "exit", "jump"})
_FAILURE_PHASES = frozenset({"input", "operation", "output", "export"})
_IDENTIFIER_LIMIT = 2_048
_DATABASE_NAME = "node_events.sqlite"
_SCHEMA_SQL = """
CREATE TABLE node_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    node_id TEXT NULL,
    task_identity TEXT NULL,
    execution_id TEXT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    job_id TEXT NULL,
    metadata_json TEXT NOT NULL
)
"""


class NodeEventWriter:
    """Append bounded, replay-safe Interpreter events to a run-local journal."""

    def __init__(self, run_dir: Path, *, workflow_id: str, run_id: str) -> None:
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._database_path: Path | None = None
        self._workflow_id = workflow_id
        self._run_id = run_id
        self.disabled_reason: str | None = None
        self._closed = False

        try:
            validated_run_dir = self._validate_run_dir(run_dir, run_id)
            if not self._valid_identifier(workflow_id) or not self._valid_identifier(run_id):
                self._disable("identity_invalid")
                return
            self._database_path = validated_run_dir / _DATABASE_NAME
            if self._is_reparse_point(self._database_path):
                self._disable("journal_path_unsafe")
                return
            if os.path.lexists(self._database_path) and not self._database_path.is_file():
                self._disable("journal_path_unsafe")
                return
            self._initialize_database()
        except NodeEventJournalError as error:
            self._disable(error.reason_code)
        except sqlite3.Error as error:
            self._disable(self._initialization_reason(error))
        except OSError:
            self._disable("write_failed")

    @staticmethod
    def _valid_identifier(value: object) -> bool:
        return isinstance(value, str) and bool(value) and len(value) <= 256 and "\x00" not in value

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            file_stat = path.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(file_stat.st_mode):
            return True
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        attributes = getattr(file_stat, "st_file_attributes", 0)
        return bool(attributes & reparse_attribute)

    @classmethod
    def _validate_run_dir(cls, run_dir: Path, run_id: str) -> Path:
        path = Path(run_dir)
        if not path.is_absolute() or cls._is_reparse_point(path):
            raise NodeEventJournalError("run_dir_invalid")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise NodeEventJournalError("run_dir_invalid") from None
        if not resolved.is_dir() or resolved.name != run_id:
            raise NodeEventJournalError("run_dir_invalid")
        return resolved

    def _initialize_database(self) -> None:
        assert self._database_path is not None
        self._connection = sqlite3.connect(
            self._database_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        mode = self._connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise NodeEventJournalError("schema_invalid")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")

        check_row = self._connection.execute("PRAGMA quick_check").fetchone()
        if check_row is None or check_row[0] != "ok":
            raise NodeEventJournalError("database_invalid")

        existing_objects = {
            (str(row[0]), str(row[1]))
            for row in self._connection.execute(
                "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        user_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if not existing_objects and user_version == 0:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(_SCHEMA_SQL)
                self._connection.execute(
                    "CREATE INDEX idx_node_events_run_order ON node_events(run_id, event_id)"
                )
                self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self._connection.execute("COMMIT")
            except sqlite3.Error:
                self._rollback()
                raise
        else:
            self._validate_existing_schema(existing_objects, user_version)

        if self._database_size() > MAX_JOURNAL_BYTES:
            raise NodeEventJournalError("capacity_bytes")

    def _validate_existing_schema(
        self,
        existing_objects: set[tuple[str, str]],
        user_version: int,
    ) -> None:
        assert self._connection is not None
        if (
            existing_objects
            != {
                ("node_events", "table"),
                ("idx_node_events_run_order", "index"),
            }
            or user_version != SCHEMA_VERSION
        ):
            raise NodeEventJournalError("schema_invalid")
        column_info = tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in self._connection.execute("PRAGMA table_info(node_events)")
        )
        expected_column_info = (
            ("event_id", "INTEGER", 0, 1),
            ("event_key", "TEXT", 1, 0),
            ("workflow_id", "TEXT", 1, 0),
            ("run_id", "TEXT", 1, 0),
            ("schema_version", "INTEGER", 1, 0),
            ("node_id", "TEXT", 0, 0),
            ("task_identity", "TEXT", 0, 0),
            ("execution_id", "TEXT", 0, 0),
            ("event_type", "TEXT", 1, 0),
            ("occurred_at", "TEXT", 1, 0),
            ("source", "TEXT", 1, 0),
            ("job_id", "TEXT", 0, 0),
            ("metadata_json", "TEXT", 1, 0),
        )
        if tuple(row[0] for row in column_info) != _TABLE_COLUMNS:
            raise NodeEventJournalError("schema_invalid")
        if column_info != expected_column_info:
            raise NodeEventJournalError("schema_invalid")
        unique_indexes = self._connection.execute("PRAGMA index_list(node_events)").fetchall()
        has_unique_event_key = False
        has_run_order_index = False
        for row in unique_indexes:
            index_name = str(row[1])
            index_columns = tuple(
                str(index_row[2])
                for index_row in self._connection.execute(f'PRAGMA index_info("{index_name}")')
            )
            if bool(row[2]) and str(row[3]) == "u" and index_columns == ("event_key",):
                has_unique_event_key = True
            if index_name == "idx_node_events_run_order" and index_columns == (
                "run_id",
                "event_id",
            ):
                has_run_order_index = True
        if not has_unique_event_key:
            raise NodeEventJournalError("schema_invalid")
        if not has_run_order_index:
            raise NodeEventJournalError("schema_invalid")

    def _database_size(self) -> int:
        assert self._connection is not None
        page_count = int(self._connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        return page_count * page_size

    def _disable(self, reason_code: str) -> None:
        if self.disabled_reason is not None:
            return
        self.disabled_reason = reason_code
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        try:
            _LOGGER.warning("node event journal disabled reason=%s", reason_code)
        except Exception:
            pass

    def _rollback(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def observe(self, event: _InterpreterEvent) -> None:
        """Persist one fixed-shape Interpreter event; arbitrary payloads are rejected."""
        if self.disabled_reason is not None or self._closed:
            return
        try:
            row = self._row_for_event(event, allow_workflow_completed=False)
        except NodeEventJournalError as error:
            self._disable(error.reason_code)
            return
        except Exception:
            self._disable("event_invalid")
            return
        self._insert(row)

    def workflow_completed(self) -> None:
        """Write the terminal marker only while every prior observer write succeeded."""
        if self.disabled_reason is not None or self._closed:
            return
        try:
            row = self._row_for_event(
                _InterpreterEvent("workflow_completed"),
                allow_workflow_completed=True,
            )
        except NodeEventJournalError as error:
            self._disable(error.reason_code)
            return
        self._insert(row)

    def _row_for_event(
        self,
        event: _InterpreterEvent,
        *,
        allow_workflow_completed: bool,
    ) -> tuple[str, str, str | None, str | None, str | None, str, str]:
        if not isinstance(event, _InterpreterEvent) or event.event_type not in _EVENT_TYPES:
            raise NodeEventJournalError("event_invalid")
        if event.event_type == "workflow_completed" and not allow_workflow_completed:
            raise NodeEventJournalError("event_invalid")
        if event.event_type in _TASK_EVENT_TYPES | {"branch_selected"}:
            if not self._valid_event_identity(event.node_id) or not self._valid_event_identity(
                event.task_identity
            ):
                raise NodeEventJournalError("event_invalid")
            if event.event_type == "task_failed" and event.failure_phase not in _FAILURE_PHASES:
                raise NodeEventJournalError("event_invalid")
            if event.event_type == "branch_selected" and (
                event.operation != "switch" or event.branch_class not in _BRANCH_CLASSES
            ):
                raise NodeEventJournalError("event_invalid")
        elif event.event_type in _SCOPE_EVENT_TYPES:
            if (
                not self._valid_event_identity(event.node_id)
                or not self._valid_event_identity(event.scope_identity)
                or event.operation not in {"do", "for"}
            ):
                raise NodeEventJournalError("event_invalid")
        elif event.event_type in _ITERATION_EVENT_TYPES:
            if (
                not self._valid_event_identity(event.node_id)
                or not self._valid_event_identity(event.task_identity)
                or not self._valid_event_identity(event.scope_identity)
                or type(event.iteration_ordinal) is not int
                or not 0 <= event.iteration_ordinal <= 1_000_000
                or event.operation != "for"
            ):
                raise NodeEventJournalError("event_invalid")
        elif event.event_type == "workflow_completed":
            if any(
                value is not None
                for value in (
                    event.node_id,
                    event.task_identity,
                    event.scope_identity,
                    event.operation,
                    event.iteration_ordinal,
                    event.branch_class,
                    event.case_index,
                    event.failure_phase,
                )
            ):
                raise NodeEventJournalError("event_invalid")

        metadata: dict[str, str | int] = {}
        if event.operation is not None:
            if event.operation not in _OPERATIONS:
                raise NodeEventJournalError("event_invalid")
            metadata["operation"] = event.operation
        if event.iteration_ordinal is not None:
            if (
                type(event.iteration_ordinal) is not int
                or not 0 <= event.iteration_ordinal <= 1_000_000
            ):
                raise NodeEventJournalError("event_invalid")
            metadata["iteration_ordinal"] = event.iteration_ordinal
        if event.branch_class is not None:
            if event.branch_class not in _BRANCH_CLASSES:
                raise NodeEventJournalError("event_invalid")
            metadata["branch_class"] = event.branch_class
        if event.case_index is not None:
            if type(event.case_index) is not int or not 0 <= event.case_index <= 10_000:
                raise NodeEventJournalError("event_invalid")
            metadata["case_index"] = event.case_index
        if event.failure_phase is not None:
            if event.failure_phase not in _FAILURE_PHASES:
                raise NodeEventJournalError("event_invalid")
            metadata["failure_phase"] = event.failure_phase

        metadata_json = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
        if len(metadata_json.encode("utf-8")) > MAX_EVENT_JSON_BYTES:
            raise NodeEventJournalError("event_too_large")
        dynamic_identity = event.scope_identity or event.task_identity
        stored_task_identity = (
            event.scope_identity
            if event.event_type in _SCOPE_EVENT_TYPES | _ITERATION_EVENT_TYPES
            else event.task_identity
        )
        execution_id = None
        if dynamic_identity is not None:
            execution_id = hashlib.sha256(
                f"codexbridge.node-event.execution.v1\0{self._workflow_id}\0{dynamic_identity}".encode()
            ).hexdigest()
        material = json.dumps(
            [
                self._workflow_id,
                self._run_id,
                event.node_id,
                event.task_identity,
                event.scope_identity,
                event.event_type,
                event.iteration_ordinal,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        event_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return (
            event_key,
            event.event_type,
            event.node_id,
            stored_task_identity,
            execution_id,
            metadata_json,
            dynamic_identity or "",
        )

    @classmethod
    def _valid_event_identity(cls, value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= _IDENTIFIER_LIMIT
            and "\x00" not in value
        )

    def _insert(
        self,
        row: tuple[str, str, str | None, str | None, str | None, str, str],
    ) -> None:
        with self._lock:
            connection = self._connection
            if connection is None or self.disabled_reason is not None or self._closed:
                return
            (
                event_key,
                event_type,
                node_id,
                task_identity,
                execution_id,
                metadata_json,
                dynamic_identity,
            ) = row
            try:
                connection.execute("BEGIN IMMEDIATE")
                duplicate = connection.execute(
                    "SELECT 1 FROM node_events WHERE event_key = ?", (event_key,)
                ).fetchone()
                if duplicate is not None:
                    connection.execute("COMMIT")
                    return
                count = int(connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0])
                if count >= MAX_NODE_EVENTS:
                    self._rollback()
                    self._disable("capacity_events")
                    return
                if self._database_size() > MAX_JOURNAL_BYTES:
                    self._rollback()
                    self._disable("capacity_bytes")
                    return
                projected_size = (
                    self._database_size()
                    + len(event_key)
                    + len(event_type)
                    + len(node_id or "")
                    + len(task_identity or "")
                    + len(execution_id or "")
                    + len(metadata_json.encode("utf-8"))
                    + len(dynamic_identity)
                    + 512
                )
                if projected_size > MAX_JOURNAL_BYTES:
                    self._rollback()
                    self._disable("capacity_bytes")
                    return
                occurred_at = datetime.now(UTC).isoformat(timespec="microseconds")
                connection.execute(
                    "INSERT OR IGNORE INTO node_events "
                    "(event_key, workflow_id, run_id, schema_version, node_id, task_identity, "
                    "execution_id, event_type, occurred_at, source, job_id, metadata_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        event_key,
                        self._workflow_id,
                        self._run_id,
                        SCHEMA_VERSION,
                        node_id,
                        task_identity,
                        execution_id,
                        event_type,
                        occurred_at,
                        "interpreter",
                        metadata_json,
                    ),
                )
                if self._database_size() > MAX_JOURNAL_BYTES:
                    self._rollback()
                    self._disable("capacity_bytes")
                    return
                connection.execute("COMMIT")
            except sqlite3.Error as error:
                self._rollback()
                self._disable(self._sqlite_reason(error))
            except OSError:
                self._rollback()
                self._disable("write_failed")

    @staticmethod
    def _sqlite_reason(error: sqlite3.Error) -> str:
        error_code = getattr(error, "sqlite_errorcode", None)
        if isinstance(error_code, int) and (error_code & 0xFF) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return "sqlite_busy"
        return "write_failed"

    @classmethod
    def _initialization_reason(cls, error: sqlite3.Error) -> str:
        error_code = getattr(error, "sqlite_errorcode", None)
        if isinstance(error_code, int) and (error_code & 0xFF) in {
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB,
        }:
            return "database_invalid"
        return cls._sqlite_reason(error)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            self._connection = None
            if connection is None:
                return
            try:
                connection.close()
            except Exception:
                self._disable("write_failed")


class NodeEventJournalError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
