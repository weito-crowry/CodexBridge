from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codex_http_client import CONTROL_TOKEN_ENV, CodexHTTPClient, CodexHTTPError
from interpreter import SAFE_CODEX_JOB_PROMPT

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


class CodexJobError(RuntimeError):
    """Base class for the isolated Phase 4D job adapter."""


class CodexJobRequestError(CodexJobError, ValueError):
    """A request falls outside the fixed Phase 4D fixture contract."""


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


class CodexJobAdapter:
    """Write-ahead SQLite ledger for one fixed CodexBridge job per workflow."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        with _connect(self.database_path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL UNIQUE,
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
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS job_events_job_event_idx "
                "ON job_events(job_id, event_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_reconciliation (
                    job_id TEXT PRIMARY KEY,
                    marker TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                )
                """
            )

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
    ) -> dict[str, Any]:
        if prompt != SAFE_CODEX_JOB_PROMPT:
            raise CodexJobRequestError("codex-job prompt must be the fixed safe prompt")
        if type(port) is not int or not 1 <= port <= 65535:
            raise CodexJobRequestError("CodexBridge port must be from 1 to 65535")
        request_key = self.make_request_key(workflow_id, task_identity)
        run_root = Path(run_dir).resolve(strict=True)
        if not run_root.is_dir():
            raise CodexJobRequestError("codex-job run directory must exist")
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
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

            other = connection.execute(
                "SELECT request_key FROM jobs WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if other is not None:
                raise CodexJobConflictError(
                    f"one live job is allowed per workflow; existing key {other['request_key']}"
                )

            job_id = str(uuid.uuid4())
            client_request_id = f"ows4d-{job_id.replace('-', '')}"
            title_marker = f"OWS-Phase4D-{job_id}"
            cwd_root = run_root / "codex-job-cwd"
            if cwd_root.is_symlink():
                raise CodexJobRequestError("codex-job cwd root may not be a symlink")
            cwd_root.mkdir(exist_ok=True)
            agent_cwd = cwd_root / job_id
            agent_cwd.mkdir(exist_ok=False)
            resolved_cwd = agent_cwd.resolve(strict=True)
            if not resolved_cwd.is_relative_to(run_root) or not resolved_cwd.is_dir():
                raise CodexJobRequestError("codex-job cwd must stay inside the run directory")
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
    ) -> dict[str, Any]:
        job = self.prepare_job(
            workflow_id,
            task_identity,
            prompt,
            run_dir,
            port=client.port,
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
            if (
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
                and isinstance(item.get("text"), str)
                and not (token and token in item["text"])
            ):
                return item["text"]
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
