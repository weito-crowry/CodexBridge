from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from ..paths import AllowedPathPolicy, PathPolicyError
from .http_client import CONTROL_TOKEN_ENV, CodexHTTPClient, CodexHTTPError
from .interpreter import OwsError, validate_codex_job_prompt

CODEX_JOB_STATES = frozenset(
    {"PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"}
)
_BRIDGE_STATES = frozenset(
    {
        "in_progress",
        "needs_approval",
        "needs_input",
        "completed",
        "interrupted",
        "failed",
        "not_loaded",
        "unknown",
    }
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$")
MAX_JOBS_PER_WORKFLOW = 16
_OBSERVATION_RECHECK_LIMIT = 2
_OBSERVATION_RECHECK_DELAY_SECONDS = 0.2
_JOB_COLUMNS = (
    "job_id",
    "request_key",
    "workflow_id",
    "task_identity",
    "request_digest",
    "request_json",
    "state",
    "client_request_id",
    "title_marker",
    "agent_cwd",
    "created_at",
    "updated_at",
    "thread_id",
    "turn_id",
    "remote_state",
    "receipt_json",
    "error",
)
_JOB_COLUMN_SIGNATURE = (
    ("job_id", "TEXT", 0, 1),
    ("request_key", "TEXT", 1, 0),
    ("workflow_id", "TEXT", 1, 0),
    ("task_identity", "TEXT", 1, 0),
    ("request_digest", "TEXT", 1, 0),
    ("request_json", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("client_request_id", "TEXT", 1, 0),
    ("title_marker", "TEXT", 1, 0),
    ("agent_cwd", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("thread_id", "TEXT", 0, 0),
    ("turn_id", "TEXT", 0, 0),
    ("remote_state", "TEXT", 0, 0),
    ("receipt_json", "TEXT", 0, 0),
    ("error", "TEXT", 0, 0),
)
_EVENT_COLUMN_SIGNATURE = (
    ("event_id", "INTEGER", 0, 1),
    ("job_id", "TEXT", 1, 0),
    ("state", "TEXT", 1, 0),
    ("timestamp", "TEXT", 1, 0),
    ("payload_json", "TEXT", 1, 0),
)
_RECONCILIATION_COLUMN_SIGNATURE = (
    ("job_id", "TEXT", 0, 1),
    ("marker", "TEXT", 1, 0),
    ("reason", "TEXT", 1, 0),
    ("detected_at", "TEXT", 1, 0),
    ("details_json", "TEXT", 1, 0),
)
_EVENT_STATES = CODEX_JOB_STATES | frozenset({"NEEDS_RECONCILIATION"})


class CodexJobError(RuntimeError):
    """Base class for the isolated Phase 4D job adapter."""


class CodexJobRequestError(CodexJobError, ValueError):
    """A request falls outside the supported single-job contract."""


class CodexJobConflictError(CodexJobError):
    """A stable request key was reused for different request content."""


class CodexJobReconciliationRequiredError(CodexJobError):
    """The ledger cannot safely determine or advance the remote job."""


class CodexJobFailedError(CodexJobError):
    """CodexBridge reported a failed or interrupted turn."""


class CodexJobTimeoutError(CodexJobError, TimeoutError):
    """The bounded wait expired without cancelling the CodexBridge turn."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise CodexJobRequestError("codex-job request must be JSON") from error


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def _append_event(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    state: str,
    timestamp: str,
    payload: Any,
) -> None:
    connection.execute(
        "INSERT INTO job_events (job_id, state, timestamp, payload_json) VALUES (?, ?, ?, ?)",
        (job_id, state, timestamp, _canonical_json(payload)),
    )


def _compact_sql(sql: str | None) -> str:
    if sql is None:
        return ""
    compact = re.sub(r"\s+", "", sql).lower().rstrip(";")
    return compact.replace('"', "").replace("`", "").replace("[", "").replace("]", "")


def _jobs_table_sql(table_name: str, *, version: int) -> str:
    workflow_unique = (
        "" if version == 1 else ",\n                    UNIQUE(workflow_id, task_identity)"
    )
    workflow_constraint = " UNIQUE" if version == 1 else ""
    return f"""
        CREATE TABLE {table_name} (
            job_id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            workflow_id TEXT NOT NULL{workflow_constraint},
            task_identity TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            request_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('PREPARED','MAYBE_SENT','ACCEPTED','RUNNING','SUCCEEDED','FAILED')
            ),
            client_request_id TEXT NOT NULL UNIQUE,
            title_marker TEXT NOT NULL UNIQUE,
            agent_cwd TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            thread_id TEXT,
            turn_id TEXT,
            remote_state TEXT,
            receipt_json TEXT,
            error TEXT{workflow_unique}
        )
    """


def _job_events_table_sql() -> str:
    return """
        CREATE TABLE job_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            state TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
    """


def _job_reconciliation_table_sql() -> str:
    return """
        CREATE TABLE job_reconciliation (
            job_id TEXT PRIMARY KEY,
            marker TEXT NOT NULL,
            reason TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        )
    """


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class CodexJobAdapter:
    """Write-ahead SQLite ledger for one CodexBridge job per workflow."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        connection = _connect(self.database_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_ledger(connection)
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise CodexJobReconciliationRequiredError(
                "NEEDS_RECONCILIATION: could not initialize the Codex job ledger"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _schema_failure(reason: str) -> NoReturn:
        raise CodexJobReconciliationRequiredError(f"NEEDS_RECONCILIATION: {reason}")

    @staticmethod
    def _schema_objects(connection: sqlite3.Connection) -> set[tuple[str, str]]:
        return {
            (str(row["type"]), str(row["name"]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT GLOB 'sqlite_*' AND type IN ('table', 'index', 'view', 'trigger')"
            )
        }

    @staticmethod
    def _column_signature(
        connection: sqlite3.Connection,
        table: str,
    ) -> tuple[tuple[str, str, int, int], ...]:
        quoted_table = _quote_identifier(table)
        return tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({quoted_table})")
        )

    @staticmethod
    def _table_sql(connection: sqlite3.Connection, table: str) -> str:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return "" if row is None or row[0] is None else str(row[0])

    @staticmethod
    def _index_entries(
        connection: sqlite3.Connection,
        table: str,
    ) -> list[tuple[bool, str, tuple[str, ...]]]:
        quoted_table = _quote_identifier(table)
        entries: list[tuple[bool, str, tuple[str, ...]]] = []
        for row in connection.execute(f"PRAGMA index_list({quoted_table})"):
            index_name = str(row[1])
            quoted_index = _quote_identifier(index_name)
            columns = tuple(
                str(column[2])
                for column in connection.execute(f"PRAGMA index_info({quoted_index})")
            )
            entries.append((bool(row[2]), index_name, columns))
        return entries

    def _validate_database_schema(self, connection: sqlite3.Connection, version: int) -> None:
        expected_objects = {
            ("table", "jobs"),
            ("table", "job_events"),
            ("table", "job_reconciliation"),
            ("index", "job_events_job_event_idx"),
        }
        if version == 2:
            expected_objects.add(("index", "jobs_workflow_idx"))
        if self._schema_objects(connection) != expected_objects:
            self._schema_failure("unexpected objects were found in the Codex job ledger")

        if self._column_signature(connection, "jobs") != _JOB_COLUMN_SIGNATURE:
            self._schema_failure("the Codex job ledger columns do not match a supported schema")
        if _compact_sql(self._table_sql(connection, "jobs")) != _compact_sql(
            _jobs_table_sql("jobs", version=version)
        ):
            self._schema_failure("the Codex job ledger definition is not a supported version")

        expected_unique: list[tuple[str, ...]] = [
            ("job_id",),
            ("request_key",),
            ("client_request_id",),
            ("title_marker",),
        ]
        if version == 1:
            expected_unique.append(("workflow_id",))
            expected_non_unique: list[tuple[str, tuple[str, ...]]] = []
        else:
            expected_unique.append(("workflow_id", "task_identity"))
            expected_non_unique = [("jobs_workflow_idx", ("workflow_id", "created_at", "job_id"))]
        job_indexes = self._index_entries(connection, "jobs")
        actual_unique = [columns for unique, _, columns in job_indexes if unique]
        actual_non_unique = [(name, columns) for unique, name, columns in job_indexes if not unique]
        if sorted(actual_unique) != sorted(expected_unique) or sorted(actual_non_unique) != sorted(
            expected_non_unique
        ):
            self._schema_failure("the Codex job ledger indexes do not match its supported version")

        if self._column_signature(connection, "job_events") != _EVENT_COLUMN_SIGNATURE:
            self._schema_failure("the Codex job event table columns are unexpected")
        if _compact_sql(self._table_sql(connection, "job_events")) != _compact_sql(
            _job_events_table_sql()
        ):
            self._schema_failure("the Codex job event table definition is unexpected")
        event_indexes = self._index_entries(connection, "job_events")
        if event_indexes != [(False, "job_events_job_event_idx", ("job_id", "event_id"))]:
            self._schema_failure("the Codex job event index is unexpected")

        if (
            self._column_signature(connection, "job_reconciliation")
            != _RECONCILIATION_COLUMN_SIGNATURE
        ):
            self._schema_failure("the Codex job reconciliation table columns are unexpected")
        if _compact_sql(self._table_sql(connection, "job_reconciliation")) != _compact_sql(
            _job_reconciliation_table_sql()
        ):
            self._schema_failure("the Codex job reconciliation table definition is unexpected")
        reconciliation_indexes = self._index_entries(connection, "job_reconciliation")
        if len(reconciliation_indexes) != 1 or reconciliation_indexes[0][0] is not True:
            self._schema_failure("the Codex job reconciliation primary key is unexpected")
        if reconciliation_indexes[0][2] != ("job_id",):
            self._schema_failure("the Codex job reconciliation primary key is unexpected")

    def _validate_ledger_data(self, connection: sqlite3.Connection) -> None:
        job_ids: set[str] = set()
        for row in connection.execute("SELECT * FROM jobs ORDER BY job_id"):
            job_id = row["job_id"]
            workflow_id = row["workflow_id"]
            task_identity = row["task_identity"]
            request_key = row["request_key"]
            if any(
                not isinstance(value, str) or not value
                for value in (job_id, workflow_id, task_identity, request_key)
            ):
                self._schema_failure("a Codex job row has a missing identity key")
            if request_key != f"{workflow_id}::{task_identity}":
                self._schema_failure("a Codex job row has inconsistent identity keys")
            assert isinstance(job_id, str)
            job_ids.add(job_id)

            for column in (
                "request_digest",
                "request_json",
                "client_request_id",
                "title_marker",
                "agent_cwd",
                "created_at",
                "updated_at",
            ):
                if not isinstance(row[column], str) or not row[column]:
                    self._schema_failure("a Codex job row has a missing required value")
            if re.fullmatch(r"[0-9a-f]{64}", row["request_digest"]) is None:
                self._schema_failure("a Codex job row has an invalid request digest")

            try:
                request = json.loads(row["request_json"])
            except (TypeError, json.JSONDecodeError):
                self._schema_failure("a Codex job row has invalid request JSON")
            if not isinstance(request, dict) or set(request) != {
                "client_request_id",
                "cwd",
                "prompt",
                "title",
                "sandbox_mode",
                "confirm_full_access",
            }:
                self._schema_failure("a Codex job row has an unexpected request shape")
            if (
                request["client_request_id"] != row["client_request_id"]
                or request["cwd"] != row["agent_cwd"]
                or request["title"] != row["title_marker"]
                or request["sandbox_mode"] != "inherit"
                or request["confirm_full_access"] is not False
            ):
                self._schema_failure("a Codex job row disagrees with its stored request")
            try:
                validate_codex_job_prompt(request["prompt"])
            except OwsError:
                self._schema_failure("a Codex job row has an invalid stored prompt")

            state = row["state"]
            if not isinstance(state, str) or state not in CODEX_JOB_STATES:
                self._schema_failure("a Codex job row has an unsupported state")
            thread_id = row["thread_id"]
            turn_id = row["turn_id"]
            remote_state = row["remote_state"]
            receipt_json = row["receipt_json"]
            if state in {"PREPARED", "MAYBE_SENT"}:
                if any(
                    value is not None for value in (thread_id, turn_id, remote_state, receipt_json)
                ):
                    self._schema_failure("a pre-acceptance Codex job row has remote data")
            elif not self._valid_persisted_id(thread_id) or not self._valid_persisted_id(turn_id):
                self._schema_failure("an accepted Codex job row has incomplete remote identity")
            elif state == "ACCEPTED":
                if remote_state not in _BRIDGE_STATES or receipt_json is not None:
                    self._schema_failure("an accepted Codex job row has inconsistent remote data")
            elif state == "RUNNING":
                if remote_state != "in_progress" or receipt_json is not None:
                    self._schema_failure("a running Codex job row has inconsistent remote data")
            elif state == "SUCCEEDED":
                receipt = self._parse_receipt(receipt_json)
                result = receipt.get("result") if isinstance(receipt, dict) else None
                if (
                    remote_state != "completed"
                    or not isinstance(result, dict)
                    or set(receipt) != {"result"}
                    or set(result) != {"final_text"}
                    or not isinstance(result["final_text"], str)
                ):
                    self._schema_failure("a succeeded Codex job row has an invalid receipt")
                token = os.environ.get(CONTROL_TOKEN_ENV)
                if token and token in result["final_text"]:
                    self._schema_failure("a succeeded Codex job receipt contains a secret")
            elif state == "FAILED":
                receipt = self._parse_receipt(receipt_json)
                if (
                    remote_state not in {"failed", "interrupted"}
                    or receipt != {"remote_state": remote_state}
                    or not isinstance(row["error"], str)
                    or not row["error"]
                ):
                    self._schema_failure("a failed Codex job row has an invalid receipt")

        for event in connection.execute("SELECT * FROM job_events ORDER BY event_id"):
            if event["job_id"] not in job_ids:
                self._schema_failure("a Codex job event has no matching job")
            if event["state"] not in _EVENT_STATES or not event["timestamp"]:
                self._schema_failure("a Codex job event has invalid state or timestamp")
            self._parse_json_object(event["payload_json"], "a Codex job event has invalid payload")

        for reconciliation in connection.execute("SELECT * FROM job_reconciliation"):
            if reconciliation["job_id"] not in job_ids:
                self._schema_failure("a Codex job reconciliation row has no matching job")
            if (
                reconciliation["marker"] != "NEEDS_RECONCILIATION"
                or not reconciliation["reason"]
                or not reconciliation["detected_at"]
            ):
                self._schema_failure("a Codex job reconciliation row is incomplete")
            self._parse_json_object(
                reconciliation["details_json"],
                "a Codex job reconciliation row has invalid details",
            )

    @staticmethod
    def _valid_persisted_id(value: Any) -> bool:
        return CodexJobAdapter._valid_id(value)

    @classmethod
    def _parse_json_object(cls, value: Any, error_message: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            cls._schema_failure(error_message)
        if not isinstance(parsed, dict):
            cls._schema_failure(error_message)
        return parsed

    @classmethod
    def _parse_receipt(cls, value: Any) -> dict[str, Any]:
        return cls._parse_json_object(value, "a terminal Codex job row has invalid receipt JSON")

    @staticmethod
    def _ledger_snapshot(
        connection: sqlite3.Connection,
    ) -> tuple[tuple[tuple[Any, ...], ...], ...]:
        return tuple(
            tuple(tuple(row) for row in connection.execute(query))
            for query in (
                "SELECT * FROM jobs ORDER BY job_id",
                "SELECT * FROM job_events ORDER BY event_id",
                "SELECT * FROM job_reconciliation ORDER BY job_id",
            )
        )

    @staticmethod
    def _assert_migrated_content(
        connection: sqlite3.Connection,
        expected: tuple[tuple[tuple[Any, ...], ...], ...],
    ) -> None:
        if CodexJobAdapter._ledger_snapshot(connection) != expected:
            CodexJobAdapter._schema_failure("the V1 to V2 migration changed ledger content")

    def _initialize_ledger(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            objects = self._schema_objects(connection)
            if not objects:
                connection.execute(_jobs_table_sql("jobs", version=2))
                connection.execute(_job_events_table_sql())
                connection.execute(
                    "CREATE INDEX job_events_job_event_idx ON job_events(job_id, event_id)"
                )
                connection.execute(_job_reconciliation_table_sql())
                connection.execute(
                    "CREATE INDEX jobs_workflow_idx ON jobs(workflow_id, created_at, job_id)"
                )
                self._validate_database_schema(connection, version=2)
                connection.execute("COMMIT")
                return

            version = self._detect_schema_version(connection)
            self._validate_database_schema(connection, version=version)
            self._validate_ledger_data(connection)
            if version == 2:
                connection.execute("COMMIT")
                return

            active = connection.execute(
                "SELECT 1 FROM jobs WHERE state IN "
                "('PREPARED','MAYBE_SENT','ACCEPTED','RUNNING') LIMIT 1"
            ).fetchone()
            if active is not None:
                connection.execute("COMMIT")
                return

            before = self._ledger_snapshot(connection)
            connection.execute(_jobs_table_sql("jobs_v2", version=2))
            columns = ", ".join(_JOB_COLUMNS)
            connection.execute(f"INSERT INTO jobs_v2 ({columns}) SELECT {columns} FROM jobs")
            connection.execute("DROP TABLE jobs")
            connection.execute("ALTER TABLE jobs_v2 RENAME TO jobs")
            connection.execute(
                "CREATE INDEX jobs_workflow_idx ON jobs(workflow_id, created_at, job_id)"
            )
            self._validate_database_schema(connection, version=2)
            self._validate_ledger_data(connection)
            self._assert_migrated_content(connection, before)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                self._schema_failure("the migrated Codex job ledger failed SQLite integrity_check")
            connection.execute("COMMIT")
        except CodexJobReconciliationRequiredError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise CodexJobReconciliationRequiredError(
                "NEEDS_RECONCILIATION: the Codex job ledger schema is unsupported or inconsistent"
            ) from error
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _detect_schema_version(self, connection: sqlite3.Connection) -> int:
        actual_sql = _compact_sql(self._table_sql(connection, "jobs"))
        for version in (1, 2):
            if actual_sql == _compact_sql(_jobs_table_sql("jobs", version=version)):
                return version
        self._schema_failure("the Codex job ledger schema is not a recognized V1 or V2 definition")
        raise AssertionError("unreachable")

    @staticmethod
    def make_request_key(workflow_id: str, task_identity: str) -> str:
        if (
            not isinstance(workflow_id, str)
            or not workflow_id
            or not isinstance(task_identity, str)
            or not task_identity
        ):
            raise CodexJobRequestError("workflow_id and task_identity must be non-empty strings")
        return f"{workflow_id}::{task_identity}"

    def prepare_job(
        self,
        workflow_id: str,
        task_identity: str,
        prompt: str,
        run_dir: Path,
        *,
        port: int,
        workspace_root: Path | None = None,
        allowed_roots: tuple[str, ...] = (),
        max_jobs_per_workflow: int = 1,
    ) -> dict[str, Any]:
        if type(max_jobs_per_workflow) is not int or max_jobs_per_workflow not in {1, 2, 3}:
            raise CodexJobRequestError("max_jobs_per_workflow must be the integer 1, 2, or 3")
        try:
            prompt = validate_codex_job_prompt(prompt)
        except OwsError as error:
            raise CodexJobRequestError(str(error)) from error
        if type(port) is not int or not 1 <= port <= 65535:
            raise CodexJobRequestError("CodexBridge port must be from 1 to 65535")
        request_key = self.make_request_key(workflow_id, task_identity)
        run_root = Path(run_dir).resolve(strict=True)
        if not run_root.is_dir():
            raise CodexJobRequestError("codex-job run directory must exist")
        resolved_workspace: Path | None = None
        if workspace_root is not None:
            try:
                policy = AllowedPathPolicy(allowed_roots)
                resolved_workspace = Path(policy.validate_cwd(str(workspace_root)))
            except (PathPolicyError, OSError):
                raise CodexJobRequestError(
                    "codex-job workspace must be inside an allowed existing root"
                ) from None
            if not os.access(resolved_workspace, os.W_OK):
                raise CodexJobRequestError("codex-job workspace is not writable")
            if (
                resolved_workspace == run_root
                or resolved_workspace.is_relative_to(run_root)
                or run_root.is_relative_to(resolved_workspace)
            ):
                raise CodexJobRequestError("workflow state and job workspace must be separate")
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if max_jobs_per_workflow == 3 and self._detect_schema_version(connection) != 2:
                raise CodexJobConflictError("max_jobs_per_workflow=3 requires a V2 job ledger")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
            if existing is not None:
                job = self._row_to_job(existing)
                expected_body = {
                    "client_request_id": str(existing["client_request_id"]),
                    "cwd": str(existing["agent_cwd"]),
                    "prompt": prompt,
                    "title": str(existing["title_marker"]),
                    "sandbox_mode": "inherit",
                    "confirm_full_access": False,
                }
                expected_json = _canonical_json(expected_body)
                expected_digest = self._digest_for_port(expected_json, port)
                if (
                    str(existing["request_json"]) != expected_json
                    or str(existing["request_digest"]) != expected_digest
                ):
                    raise CodexJobConflictError(
                        f"request digest mismatch for durable request key {request_key}"
                    )
                connection.execute("COMMIT")
                return job

            if max_jobs_per_workflow in {2, 3} and self._detect_schema_version(connection) != 2:
                raise CodexJobConflictError(
                    f"max_jobs_per_workflow={max_jobs_per_workflow} requires a V2 job ledger"
                )

            existing_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()[0]
            )
            if existing_count >= max_jobs_per_workflow:
                if max_jobs_per_workflow == 1:
                    other = connection.execute(
                        "SELECT request_key FROM jobs WHERE workflow_id = ? LIMIT 1",
                        (workflow_id,),
                    ).fetchone()
                    existing_key = "unknown" if other is None else str(other["request_key"])
                    raise CodexJobConflictError(
                        f"one live job is allowed per workflow; existing key {existing_key}"
                    )
                raise CodexJobConflictError(
                    f"workflow job limit ({max_jobs_per_workflow}) was reached"
                )

            job_id = str(uuid.uuid4())
            client_request_id = f"ows4d-{job_id.replace('-', '')}"
            title_marker = f"OWS-Phase4D-{job_id}"
            if resolved_workspace is None:
                cwd_root = run_root / "codex-job-cwd"
                if cwd_root.is_symlink():
                    raise CodexJobRequestError("codex-job cwd root may not be a symlink")
                cwd_root.mkdir(exist_ok=True)
                agent_cwd = cwd_root / job_id
                agent_cwd.mkdir(exist_ok=False)
                resolved_cwd = agent_cwd.resolve(strict=True)
                if not resolved_cwd.is_relative_to(run_root) or not resolved_cwd.is_dir():
                    raise CodexJobRequestError("codex-job cwd must stay inside the run directory")
            else:
                resolved_cwd = resolved_workspace
            request_body = {
                "client_request_id": client_request_id,
                "cwd": str(resolved_cwd),
                "prompt": prompt,
                "title": title_marker,
                "sandbox_mode": "inherit",
                "confirm_full_access": False,
            }
            request_json = _canonical_json(request_body)
            request_digest = self._digest_for_port(request_json, port)
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_key, workflow_id, task_identity, request_digest,
                    request_json, state, client_request_id, title_marker, agent_cwd,
                    created_at, updated_at, thread_id, turn_id, remote_state,
                    receipt_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    job_id,
                    request_key,
                    workflow_id,
                    task_identity,
                    request_digest,
                    request_json,
                    client_request_id,
                    title_marker,
                    str(resolved_cwd),
                    now,
                    now,
                ),
            )
            _append_event(
                connection,
                job_id=job_id,
                state="PREPARED",
                timestamp=now,
                payload={
                    "request_key": request_key,
                    "request_digest": request_digest,
                    "client_request_id": client_request_id,
                    "title_marker": title_marker,
                },
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.execute("COMMIT")
            return self._row_to_job(row)
        except sqlite3.IntegrityError as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise CodexJobConflictError("one live job is allowed per workflow") from error
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _digest_for_port(request_json: str, port: int) -> str:
        material = _canonical_json(
            {"host": "127.0.0.1", "port": port, "body": json.loads(request_json)}
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def claim_post(self, job_id: str) -> bool:
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE jobs SET state = 'MAYBE_SENT', updated_at = ? "
                "WHERE job_id = ? AND state = 'PREPARED'",
                (now, job_id),
            )
            if cursor.rowcount == 1:
                _append_event(
                    connection,
                    job_id=job_id,
                    state="MAYBE_SENT",
                    timestamp=now,
                    payload={"write_ahead_committed": True},
                )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def start_job(
        self,
        workflow_id: str,
        task_identity: str,
        prompt: str,
        run_dir: Path,
        client: CodexHTTPClient,
        *,
        workspace_root: Path | None = None,
        allowed_roots: tuple[str, ...] = (),
        max_jobs_per_workflow: int = 1,
    ) -> dict[str, Any]:
        job = self.prepare_job(
            workflow_id,
            task_identity,
            prompt,
            run_dir,
            port=client.port,
            workspace_root=workspace_root,
            allowed_roots=allowed_roots,
            max_jobs_per_workflow=max_jobs_per_workflow,
        )
        if job["state"] == "MAYBE_SENT":
            self.mark_reconciliation(
                job["job_id"],
                "POST may have been sent; automatic retry is prohibited",
            )
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: POST outcome is unknown for {job['request_key']}"
            )
        if job["state"] != "PREPARED":
            return self.get_job_status(job_id=job["job_id"])

        try:
            client.preflight()
        except CodexHTTPError:
            raise CodexJobError("CodexBridge preflight failed before POST") from None

        if not self.claim_post(job["job_id"]):
            current = self.get_job_status(job_id=job["job_id"])
            if current["state"] == "MAYBE_SENT":
                self.mark_reconciliation(
                    job["job_id"],
                    "another sender may have issued the POST; automatic retry is prohibited",
                )
                raise CodexJobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: concurrent POST outcome for {job['request_key']}"
                )
            return current

        try:
            response = client.create_thread(job["request"])
        except CodexHTTPError:
            self.mark_reconciliation(
                job["job_id"],
                "control POST failed or its response was incomplete; automatic retry is prohibited",
            )
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: control POST outcome is unknown for {job['request_key']}"
            ) from None

        thread_id = response.get("thread_id")
        turn_id = response.get("turn_id")
        remote_state = response.get("state")
        if (
            not self._valid_id(thread_id)
            or not self._valid_id(turn_id)
            or not isinstance(remote_state, str)
            or remote_state not in _BRIDGE_STATES
        ):
            details = {
                "candidate_thread_id": thread_id if self._valid_id(thread_id) else None,
                "candidate_turn_id": turn_id if self._valid_id(turn_id) else None,
            }
            self.mark_reconciliation(
                job["job_id"],
                "control POST returned an invalid response; automatic retry is prohibited",
                details=details,
            )
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: invalid control response for {job['request_key']}"
            )
        assert isinstance(thread_id, str)
        assert isinstance(turn_id, str)
        assert isinstance(remote_state, str)
        self.mark_accepted(job["job_id"], thread_id, turn_id, remote_state)
        return self.get_job_status(job_id=job["job_id"])

    @staticmethod
    def _valid_id(value: Any) -> bool:
        token = os.environ.get(CONTROL_TOKEN_ENV)
        return (
            isinstance(value, str)
            and _ID_RE.fullmatch(value) is not None
            and not (token and token in value)
        )

    def mark_accepted(
        self,
        job_id: str,
        thread_id: str,
        turn_id: str,
        remote_state: str,
    ) -> None:
        if not self._valid_id(thread_id) or not self._valid_id(turn_id):
            raise CodexJobRequestError("thread_id and turn_id must match the API ID contract")
        if remote_state not in _BRIDGE_STATES:
            raise CodexJobRequestError("CodexBridge returned an unsupported turn state")
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, request_key FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "MAYBE_SENT":
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: job was not in MAYBE_SENT state when accepted"
                )
            connection.execute(
                "UPDATE jobs SET state = 'ACCEPTED', updated_at = ?, thread_id = ?, "
                "turn_id = ?, remote_state = ?, receipt_json = NULL, error = NULL WHERE job_id = ?",
                (
                    now,
                    thread_id,
                    turn_id,
                    remote_state,
                    job_id,
                ),
            )
            connection.execute("DELETE FROM job_reconciliation WHERE job_id = ?", (job_id,))
            _append_event(
                connection,
                job_id=job_id,
                state="ACCEPTED",
                timestamp=now,
                payload={"thread_id": thread_id, "turn_id": turn_id, "remote_state": remote_state},
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_reconciliation(
        self,
        job_id: str,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        safe_details = details or {}
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: cannot mark a missing job"
                )
            connection.execute(
                "INSERT INTO job_reconciliation "
                "(job_id, marker, reason, detected_at, details_json) "
                "VALUES (?, 'NEEDS_RECONCILIATION', ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET marker = excluded.marker, "
                "reason = excluded.reason, detected_at = excluded.detected_at, "
                "details_json = excluded.details_json",
                (job_id, reason, now, _canonical_json(safe_details)),
            )
            _append_event(
                connection,
                job_id=job_id,
                state="NEEDS_RECONCILIATION",
                timestamp=now,
                payload={"reason": reason, **safe_details},
            )
            connection.execute("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (now, job_id))
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def observe_job(self, job_id: str, client: CodexHTTPClient) -> dict[str, Any]:
        job = self.get_job_status(job_id=job_id)
        if job["status"] == "NEEDS_RECONCILIATION" or job["state"] in {"SUCCEEDED", "FAILED"}:
            return job
        if not self._valid_id(job["thread_id"]) or not self._valid_id(job["turn_id"]):
            self.mark_reconciliation(
                job_id, "accepted job has no complete thread and turn identity"
            )
            return self.get_job_status(job_id=job_id)
        rechecks = 0
        while True:
            try:
                observation = client.observe_thread(job["thread_id"], job["turn_id"])
            except CodexHTTPError:
                self.mark_reconciliation(job_id, "thread status or history read failed")
                return self.get_job_status(job_id=job_id)

            status_state = observation["status"].get("state")
            matching_turns = [
                turn
                for turn in observation["turns"]
                if isinstance(turn, dict) and turn.get("id") == job["turn_id"]
            ]
            turn_state = matching_turns[0].get("status") if matching_turns else None
            transient_mismatch = (status_state, turn_state) in {
                ("in_progress", "completed"),
                ("completed", "in_progress"),
            }
            if not transient_mismatch or rechecks >= _OBSERVATION_RECHECK_LIMIT:
                break
            rechecks += 1
            time.sleep(_OBSERVATION_RECHECK_DELAY_SECONDS)

        if status_state in {"not_loaded", "unknown"}:
            remote_state = turn_state if turn_state in _BRIDGE_STATES else status_state
        elif turn_state in _BRIDGE_STATES and turn_state != status_state:
            self.mark_reconciliation(job_id, "status and exact turn history disagree")
            return self.get_job_status(job_id=job_id)
        else:
            remote_state = status_state
        if remote_state not in _BRIDGE_STATES:
            self.mark_reconciliation(job_id, "status response has an unsupported state")
            return self.get_job_status(job_id=job_id)

        if remote_state == "in_progress":
            self._mark_running(job_id, remote_state)
        elif remote_state == "completed":
            if not matching_turns or turn_state != "completed":
                self.mark_reconciliation(
                    job_id, "completed status lacks exact completed turn history"
                )
                return self.get_job_status(job_id=job_id)
            final_text = self._final_text(observation["items"], job["turn_id"])
            if final_text is None:
                self.mark_reconciliation(job_id, "completed turn has no matching final answer item")
                return self.get_job_status(job_id=job_id)
            self._mark_succeeded(job_id, final_text)
        elif remote_state in {"failed", "interrupted"}:
            self._mark_failed(job_id, remote_state)
        elif remote_state in {"needs_approval", "needs_input"}:
            self.mark_reconciliation(
                job_id,
                f"CodexBridge turn requires {remote_state}; adapter "
                "will not provide approval or input",
            )
        elif remote_state == "not_loaded" and job["state"] == "RUNNING":
            return self.get_job_status(job_id=job_id)
        return self.get_job_status(job_id=job_id)

    @staticmethod
    def _final_text(items: list[dict[str, Any]], turn_id: str) -> str | None:
        token = os.environ.get(CONTROL_TOKEN_ENV)
        for entry in items:
            if not isinstance(entry, dict) or entry.get("turn_id") != turn_id:
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if (
                item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
                and isinstance(text, str)
                and not (token and token in text)
            ):
                return text
        return None

    def _mark_running(self, job_id: str, remote_state: str) -> None:
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise CodexJobReconciliationRequiredError("NEEDS_RECONCILIATION: job is missing")
            if row["state"] == "ACCEPTED":
                connection.execute(
                    "UPDATE jobs SET state = 'RUNNING', updated_at = ?, remote_state = ? "
                    "WHERE job_id = ?",
                    (now, remote_state, job_id),
                )
                _append_event(
                    connection,
                    job_id=job_id,
                    state="RUNNING",
                    timestamp=now,
                    payload={"remote_state": remote_state},
                )
            elif row["state"] != "RUNNING":
                raise CodexJobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: cannot mark {row['state']} job as RUNNING"
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _mark_succeeded(self, job_id: str, final_text: str) -> None:
        self._finish_job(
            job_id,
            state="SUCCEEDED",
            remote_state="completed",
            receipt={"result": {"final_text": final_text}},
            error=None,
        )

    def _mark_failed(self, job_id: str, remote_state: str) -> None:
        self._finish_job(
            job_id,
            state="FAILED",
            remote_state=remote_state,
            receipt={"remote_state": remote_state},
            error=(
                "CodexBridge turn interrupted"
                if remote_state == "interrupted"
                else "CodexBridge turn failed"
            ),
        )

    def _finish_job(
        self,
        job_id: str,
        *,
        state: str,
        remote_state: str,
        receipt: dict[str, Any],
        error: str | None,
    ) -> None:
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] not in {"ACCEPTED", "RUNNING"}:
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: terminal receipt does not match an active job"
                )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, remote_state = ?, "
                "receipt_json = ?, error = ? WHERE job_id = ?",
                (state, now, remote_state, _canonical_json(receipt), error, job_id),
            )
            _append_event(
                connection,
                job_id=job_id,
                state=state,
                timestamp=now,
                payload={"remote_state": remote_state, "receipt": receipt, "error": error},
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_job_status(
        self,
        *,
        job_id: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        if (job_id is None) == (request_key is None):
            raise CodexJobRequestError("provide exactly one of job_id or request_key")
        with _connect(self.database_path) as connection:
            if job_id is not None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
            if row is None:
                raise CodexJobReconciliationRequiredError("NEEDS_RECONCILIATION: job not found")
            job = self._row_to_job(row)
            reconciliation = connection.execute(
                "SELECT marker, reason, detected_at, details_json "
                "FROM job_reconciliation WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            event_row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM job_events WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
        job["status"] = job["state"]
        job["latest_event_id"] = int(event_row[0])
        job["adapter_marker"] = None
        job["reconciliation"] = None
        if reconciliation is not None:
            job["status"] = str(reconciliation["marker"])
            job["adapter_marker"] = str(reconciliation["marker"])
            job["reconciliation"] = {
                "reason": str(reconciliation["reason"]),
                "detected_at": str(reconciliation["detected_at"]),
                "details": json.loads(reconciliation["details_json"]),
            }
        return job

    def get_jobs_for_workflow(
        self,
        workflow_id: str,
        *,
        limit: int = MAX_JOBS_PER_WORKFLOW,
    ) -> list[dict[str, Any]]:
        if not isinstance(workflow_id, str) or not workflow_id:
            raise CodexJobRequestError("workflow_id must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= MAX_JOBS_PER_WORKFLOW:
            raise CodexJobRequestError(
                f"workflow job limit must be an integer from 1 to {MAX_JOBS_PER_WORKFLOW}"
            )
        database_path = self.database_path
        if database_path.is_symlink() or not database_path.is_file():
            raise CodexJobReconciliationRequiredError(
                "NEEDS_RECONCILIATION: the Codex job ledger is missing or unavailable"
            )
        try:
            database_uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro"
            connection = sqlite3.connect(database_uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "jobs" not in tables:
                    raise CodexJobReconciliationRequiredError(
                        "NEEDS_RECONCILIATION: the Codex job ledger has no jobs table"
                    )
                rows = connection.execute(
                    "SELECT workflow_id, job_id, task_identity, state, thread_id, turn_id, "
                    "remote_state, receipt_json FROM jobs WHERE workflow_id = ? "
                    "ORDER BY created_at ASC, job_id ASC LIMIT ?",
                    (workflow_id, limit + 1),
                ).fetchall()
            finally:
                connection.close()
        except CodexJobReconciliationRequiredError:
            raise
        except (OSError, sqlite3.Error, ValueError) as error:
            raise CodexJobReconciliationRequiredError(
                "NEEDS_RECONCILIATION: could not read the Codex job ledger"
            ) from error

        if len(rows) > limit:
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: workflow has more than the requested job limit ({limit})"
            )
        jobs: list[dict[str, Any]] = []
        for row in rows:
            if row["workflow_id"] != workflow_id:
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: workflow job query returned an unrelated row"
                )
            state = row["state"]
            job_id = row["job_id"]
            task_identity = row["task_identity"]
            if (
                not isinstance(job_id, str)
                or not job_id
                or not isinstance(task_identity, str)
                or not task_identity
                or not isinstance(state, str)
                or state not in CODEX_JOB_STATES
            ):
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: workflow job row has invalid identity or state"
                )
            thread_id = row["thread_id"]
            turn_id = row["turn_id"]
            if state in {"PREPARED", "MAYBE_SENT"}:
                if thread_id is not None or turn_id is not None:
                    raise CodexJobReconciliationRequiredError(
                        "NEEDS_RECONCILIATION: unaccepted workflow job has remote identity"
                    )
            elif not self._valid_persisted_id(thread_id) or not self._valid_persisted_id(turn_id):
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: workflow job has incomplete remote identity"
                )

            receipt: dict[str, Any] | None = None
            receipt_json = row["receipt_json"]
            if state == "SUCCEEDED":
                parsed = self._parse_receipt(receipt_json)
                result = parsed.get("result")
                if (
                    set(parsed) != {"result"}
                    or not isinstance(result, dict)
                    or set(result) != {"final_text"}
                    or not isinstance(result["final_text"], str)
                    or row["remote_state"] != "completed"
                ):
                    raise CodexJobReconciliationRequiredError(
                        "NEEDS_RECONCILIATION: workflow job receipt is inconsistent"
                    )
                token = os.environ.get(CONTROL_TOKEN_ENV)
                if token and token in result["final_text"]:
                    raise CodexJobReconciliationRequiredError(
                        "NEEDS_RECONCILIATION: workflow job receipt contains a secret"
                    )
                receipt = {"result": {"final_text": result["final_text"]}}
            elif state == "FAILED":
                parsed = self._parse_receipt(receipt_json)
                if (
                    set(parsed) != {"remote_state"}
                    or parsed.get("remote_state") != row["remote_state"]
                    or row["remote_state"] not in {"failed", "interrupted"}
                ):
                    raise CodexJobReconciliationRequiredError(
                        "NEEDS_RECONCILIATION: workflow job receipt is inconsistent"
                    )
                receipt = {"remote_state": row["remote_state"]}
            elif receipt_json is not None:
                raise CodexJobReconciliationRequiredError(
                    "NEEDS_RECONCILIATION: non-terminal workflow job has a receipt"
                )

            jobs.append(
                {
                    "job_id": job_id,
                    "task_identity": task_identity,
                    "state": state,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "receipt": receipt,
                }
            )
        return jobs

    @classmethod
    def read_shutdown_ownership(
        cls,
        database_path: Path,
    ) -> tuple[set[tuple[str, str]], bool]:
        database_path = Path(database_path)
        if database_path.is_symlink() or not database_path.is_file():
            return set(), False
        database_uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro"
        with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "jobs" not in tables:
                return set(), False
            rows = connection.execute("SELECT state, thread_id, turn_id FROM jobs").fetchall()
        owned: set[tuple[str, str]] = set()
        complete = True
        for state, thread_id, turn_id in rows:
            if state == "MAYBE_SENT":
                complete = False
            elif state in {"ACCEPTED", "RUNNING"}:
                if cls._valid_id(thread_id) and cls._valid_id(turn_id):
                    owned.add((str(thread_id), str(turn_id)))
                else:
                    complete = False
            elif state not in {"PREPARED", "SUCCEEDED", "FAILED"}:
                complete = False
        return owned, complete

    def read_events(self, *, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if type(after_id) is not int or after_id < 0:
            raise CodexJobRequestError("after_id must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CodexJobRequestError("event limit must be an integer from 1 to 1000")
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT event_id, job_id, state, timestamp, payload_json FROM job_events "
                "WHERE event_id > ? ORDER BY event_id LIMIT ?",
                (after_id, limit),
            ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "job_id": str(row["job_id"]),
                "state": str(row["state"]),
                "timestamp": str(row["timestamp"]),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        state = str(row["state"])
        if state not in CODEX_JOB_STATES:
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: unknown ledger state {state!r}"
            )
        return {
            "job_id": str(row["job_id"]),
            "request_key": str(row["request_key"]),
            "workflow_id": str(row["workflow_id"]),
            "task_identity": str(row["task_identity"]),
            "request_digest": str(row["request_digest"]),
            "request": json.loads(row["request_json"]),
            "state": state,
            "client_request_id": str(row["client_request_id"]),
            "title_marker": str(row["title_marker"]),
            "agent_cwd": str(row["agent_cwd"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "remote_state": row["remote_state"],
            "receipt": None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
            "error": row["error"],
        }
