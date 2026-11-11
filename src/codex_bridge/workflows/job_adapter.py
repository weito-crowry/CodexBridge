from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

JOB_STATES = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED"})
_REQUEST_FIELDS = frozenset({"operation", "payload", "duration_ms"})


class JobAdapterError(RuntimeError):
    """Base class for durable mock-job adapter failures."""


class JobRequestError(JobAdapterError, ValueError):
    """The request is outside the mock-job allowlist."""


class JobConflictError(JobAdapterError):
    """A durable request key was reused for different request content."""


class JobReconciliationRequiredError(JobAdapterError):
    """The ledger cannot safely establish whether a job may be dispatched."""

    adapter_marker = "NEEDS_RECONCILIATION"


class JobFailedError(JobAdapterError):
    """The mock job finished with a durable FAILED receipt."""


class JobTimeoutError(JobAdapterError, TimeoutError):
    """Bounded polling expired without cancelling the durable job."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise JobRequestError(f"payload must be JSON: {error}") from error


def _validate_json_value(value: Any, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JobRequestError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise JobRequestError(f"{path} object keys must be strings")
            _validate_json_value(child, f"{path}.{key}")
        return
    raise JobRequestError(f"{path} contains a non-JSON value")


def validate_mock_job_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise JobRequestError("mock-job with must evaluate to an object")
    unsupported = set(request) - _REQUEST_FIELDS
    if unsupported:
        raise JobRequestError(f"unsupported mock-job argument '{sorted(unsupported)[0]}'")
    if set(request) != _REQUEST_FIELDS:
        missing = sorted(_REQUEST_FIELDS - set(request))[0]
        raise JobRequestError(f"mock-job requires argument '{missing}'")
    if request["operation"] != "process" or not isinstance(request["operation"], str):
        raise JobRequestError("mock-job operation must be 'process'")
    duration_ms = request["duration_ms"]
    if type(duration_ms) is not int or not 0 <= duration_ms <= 2000:
        raise JobRequestError("mock-job duration_ms must be an integer from 0 to 2000")
    _validate_json_value(request["payload"])
    return cast(dict[str, Any], json.loads(canonical_json(request)))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _append_job_event(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    state: str,
    timestamp: str,
    payload: Any,
) -> None:
    connection.execute(
        "INSERT INTO job_events (job_id, state, timestamp, payload_json) VALUES (?, ?, ?, ?)",
        (job_id, state, timestamp, canonical_json(payload)),
    )


def register_service(
    database_path: Path,
    *,
    service_id: str,
    pid: int,
    parent_pid: int | None,
    creation_time_100ns: int,
    image_path: str,
    run_dir: str,
) -> None:
    now = _utc_now()
    with _connect(database_path) as connection:
        connection.execute(
            "INSERT INTO service_runtime (service_id, pid, parent_pid, creation_time_100ns, "
            "image_path, run_dir, status, heartbeat_at, stopped_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, NULL)",
            (service_id, pid, parent_pid, creation_time_100ns, image_path, run_dir, now),
        )


def heartbeat_service(database_path: Path, service_id: str) -> None:
    with _connect(database_path) as connection:
        connection.execute(
            "UPDATE service_runtime SET heartbeat_at = ? "
            "WHERE service_id = ? AND status = 'RUNNING'",
            (_utc_now(), service_id),
        )


def stop_service(database_path: Path, service_id: str) -> None:
    now = _utc_now()
    with _connect(database_path) as connection:
        connection.execute(
            "UPDATE service_runtime SET status = 'STOPPED', heartbeat_at = ?, stopped_at = ? "
            "WHERE service_id = ? AND status = 'RUNNING'",
            (now, now, service_id),
        )


class DurableJobAdapter:
    """SQLite-backed idempotent request ledger for the mock-job allowlist."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        with _connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL,
                    task_identity TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    dispatch_count INTEGER NOT NULL,
                    service_execution_count INTEGER NOT NULL,
                    progress_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_state_created_idx "
                "ON jobs(state, created_at, job_id)"
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
                CREATE TABLE IF NOT EXISTS service_runtime (
                    service_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    parent_pid INTEGER,
                    creation_time_100ns INTEGER NOT NULL,
                    image_path TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('RUNNING','STOPPED')),
                    heartbeat_at TEXT NOT NULL,
                    stopped_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_service_claims (
                    job_id TEXT PRIMARY KEY,
                    service_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_reconciliation (
                    job_id TEXT PRIMARY KEY,
                    marker TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detected_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def make_request_key(workflow_id: str, task_identity: str) -> str:
        if not workflow_id or not task_identity:
            raise JobRequestError("workflow_id and task_identity must be non-empty")
        return f"{workflow_id}::{task_identity}"

    def submit_or_get_job(
        self,
        workflow_id: str,
        task_identity: str,
        request: Any,
    ) -> dict[str, Any]:
        normalized = validate_mock_job_request(request)
        request_json = canonical_json(normalized)
        request_digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        request_key = self.make_request_key(workflow_id, task_identity)
        now = _utc_now()
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
            if row is not None:
                job = self._row_to_job(row)
                if job["request_digest"] != request_digest:
                    raise JobConflictError(
                        f"request digest mismatch for durable request key {request_key}"
                    )
                connection.execute(
                    "UPDATE jobs SET dispatch_count = dispatch_count + 1, updated_at = ? "
                    "WHERE request_key = ?",
                    (now, request_key),
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._row_to_job(row)

            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, request_key, workflow_id, task_identity, request_digest,
                    request_json, state, created_at, updated_at, result_json, error,
                    dispatch_count, service_execution_count, progress_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, NULL, NULL, 1, 0, ?)
                """,
                (
                    job_id,
                    request_key,
                    workflow_id,
                    task_identity,
                    request_digest,
                    request_json,
                    now,
                    now,
                    canonical_json({"completed_ms": 0, "total_ms": normalized["duration_ms"]}),
                ),
            )
            _append_job_event(
                connection,
                job_id=job_id,
                state="QUEUED",
                timestamp=now,
                payload={"request_key": request_key, "request_digest": request_digest},
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.execute("COMMIT")
            return self._row_to_job(row)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._row_to_job(row)

    def get_job_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
        return None if row is None else self._row_to_job(row)

    def start_job(
        self,
        workflow_id: str,
        identity: str,
        request: Any,
    ) -> dict[str, Any]:
        """Create or identify a durable job and return without waiting for its result."""
        job = self.submit_or_get_job(workflow_id, identity, request)
        snapshot = self.get_job_status(job_id=job["job_id"])
        return {
            "job_id": job["job_id"],
            "request_key": job["request_key"],
            "status": snapshot["status"],
        }

    def get_job_status(
        self,
        *,
        job_id: str | None = None,
        request_key: str | None = None,
        stale_ttl_seconds: float = 2.0,
    ) -> dict[str, Any]:
        if (job_id is None) == (request_key is None):
            raise JobRequestError("provide exactly one of job_id or request_key")
        if (
            isinstance(stale_ttl_seconds, bool)
            or not isinstance(stale_ttl_seconds, (int, float))
            or not math.isfinite(stale_ttl_seconds)
            or stale_ttl_seconds <= 0
        ):
            raise JobRequestError("stale_ttl_seconds must be a positive finite number")

        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE request_key = ?", (request_key,)
                ).fetchone()
            if row is None:
                identifier = job_id if job_id is not None else request_key
                raise JobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: job lookup is missing for {identifier}"
                )
            job = self._row_to_job(row)
            reconciliation = connection.execute(
                "SELECT marker, reason, detected_at FROM job_reconciliation WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            if job["state"] == "RUNNING" and reconciliation is None:
                service = connection.execute(
                    "SELECT runtime.status, runtime.heartbeat_at, runtime.stopped_at "
                    "FROM job_service_claims AS claim "
                    "JOIN service_runtime AS runtime USING (service_id) "
                    "WHERE claim.job_id = ?",
                    (job["job_id"],),
                ).fetchone()
                reason: str | None = None
                if service is None:
                    reason = "RUNNING job has no recorded owning service identity"
                elif service["status"] not in {"RUNNING", "STOPPED"}:
                    raise JobReconciliationRequiredError(
                        f"NEEDS_RECONCILIATION: invalid service status for {job['request_key']}"
                    )
                elif service["status"] == "STOPPED":
                    reason = "claimed service stopped while job remained RUNNING"
                else:
                    try:
                        now = datetime.now(UTC)
                        job_age = (now - datetime.fromisoformat(job["updated_at"])).total_seconds()
                        heartbeat_age = (
                            now - datetime.fromisoformat(str(service["heartbeat_at"]))
                        ).total_seconds()
                    except (TypeError, ValueError) as error:
                        raise JobReconciliationRequiredError(
                            f"NEEDS_RECONCILIATION: invalid job/service heartbeat for "
                            f"{job['request_key']}"
                        ) from error
                    if job_age >= stale_ttl_seconds and heartbeat_age >= stale_ttl_seconds:
                        reason = "job and owning service heartbeats exceeded the stale TTL"
                if reason is not None:
                    detected_at = _utc_now()
                    connection.execute(
                        "INSERT OR IGNORE INTO job_reconciliation "
                        "(job_id, marker, reason, detected_at) "
                        "VALUES (?, 'NEEDS_RECONCILIATION', ?, ?)",
                        (job["job_id"], reason, detected_at),
                    )
                    _append_job_event(
                        connection,
                        job_id=job["job_id"],
                        state="NEEDS_RECONCILIATION",
                        timestamp=detected_at,
                        payload={"ledger_state": "RUNNING", "reason": reason},
                    )
                    reconciliation = connection.execute(
                        "SELECT marker, reason, detected_at FROM job_reconciliation "
                        "WHERE job_id = ?",
                        (job["job_id"],),
                    ).fetchone()
            latest_event_id_row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM job_events WHERE job_id = ?",
                (job["job_id"],),
            ).fetchone()
            job["latest_event_id"] = int(latest_event_id_row[0])
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

        if job["state"] == "SUCCEEDED" and job["result"] is None:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: SUCCEEDED job has no result for {job['request_key']}"
            )
        if job["state"] == "FAILED" and not job["error"]:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: FAILED job has no error receipt for {job['request_key']}"
            )
        job["status"] = job["state"]
        job["adapter_marker"] = None
        job["reconciliation"] = None
        if reconciliation is not None and job["state"] == "RUNNING":
            job["status"] = str(reconciliation["marker"])
            job["adapter_marker"] = str(reconciliation["marker"])
            job["reconciliation"] = {
                "reason": str(reconciliation["reason"]),
                "detected_at": str(reconciliation["detected_at"]),
            }
        return job

    def read_events(self, *, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if type(after_id) is not int or after_id < 0:
            raise JobRequestError("after_id must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise JobRequestError("event limit must be an integer from 1 to 1000")
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

    def count_jobs(self) -> int:
        with _connect(self.database_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        state = str(row["state"])
        if state not in JOB_STATES:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: unsupported ledger state {state!r} "
                f"for request key {row['request_key']}"
            )
        return {
            "job_id": str(row["job_id"]),
            "request_key": str(row["request_key"]),
            "workflow_id": str(row["workflow_id"]),
            "task_identity": str(row["task_identity"]),
            "request_digest": str(row["request_digest"]),
            "request": json.loads(row["request_json"]),
            "state": state,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "result": None if row["result_json"] is None else json.loads(row["result_json"]),
            "error": row["error"],
            "dispatch_count": int(row["dispatch_count"]),
            "service_execution_count": int(row["service_execution_count"]),
            "progress": json.loads(row["progress_json"]),
        }


def claim_next_job(database_path: Path, service_id: str | None = None) -> dict[str, Any] | None:
    connection = _connect(database_path)
    now = _utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT job_id FROM jobs WHERE state = 'QUEUED' ORDER BY created_at, job_id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        job_id = str(row["job_id"])
        cursor = connection.execute(
            "UPDATE jobs SET state = 'RUNNING', service_execution_count = "
            "service_execution_count + 1, updated_at = ?, progress_json = ? "
            "WHERE job_id = ? AND state = 'QUEUED'",
            (now, canonical_json({"completed_ms": 0, "percent": 0}), job_id),
        )
        if cursor.rowcount != 1:
            connection.execute("ROLLBACK")
            return None
        if service_id is not None:
            service = connection.execute(
                "SELECT service_id FROM service_runtime WHERE service_id = ? "
                "AND status = 'RUNNING'",
                (service_id,),
            ).fetchone()
            if service is None:
                raise JobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: claiming service {service_id} is not registered"
                )
            connection.execute(
                "INSERT INTO job_service_claims (job_id, service_id, claimed_at) VALUES (?, ?, ?)",
                (job_id, service_id, now),
            )
        _append_job_event(
            connection,
            job_id=job_id,
            state="RUNNING",
            timestamp=now,
            payload={"service_id": service_id},
        )
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        connection.execute("COMMIT")
        return DurableJobAdapter._row_to_job(row)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def update_job_progress(
    database_path: Path,
    job_id: str,
    completed_ms: int,
    total_ms: int,
    service_id: str | None = None,
) -> None:
    progress = {
        "completed_ms": min(completed_ms, total_ms),
        "percent": 100 if total_ms == 0 else min(100, int(completed_ms * 100 / total_ms)),
        "total_ms": total_ms,
    }
    now = _utc_now()
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ? "
            "WHERE job_id = ? AND state = 'RUNNING'",
            (canonical_json(progress), now, job_id),
        )
        if cursor.rowcount == 1:
            _append_job_event(
                connection,
                job_id=job_id,
                state="RUNNING",
                timestamp=now,
                payload={"progress": progress},
            )
            if service_id is not None:
                connection.execute(
                    "UPDATE service_runtime SET heartbeat_at = ? "
                    "WHERE service_id = ? AND status = 'RUNNING'",
                    (now, service_id),
                )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def finish_job(
    database_path: Path, job_id: str, result: Any = None, error: str | None = None
) -> None:
    state = "FAILED" if error is not None else "SUCCEEDED"
    result_json = None if error is not None else canonical_json(result)
    now = _utc_now()
    connection = _connect(database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE jobs SET state = ?, result_json = ?, error = ?, updated_at = ?, "
            "progress_json = ? WHERE job_id = ? AND state = 'RUNNING'",
            (
                state,
                result_json,
                error,
                now,
                canonical_json({"completed_ms": 0, "percent": 100}),
                job_id,
            ),
        )
        if cursor.rowcount != 1:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: running claim disappeared for job {job_id}"
            )
        _append_job_event(
            connection,
            job_id=job_id,
            state=state,
            timestamp=now,
            payload=(
                {"error": error}
                if error is not None
                else {"result": json.loads(result_json) if result_json is not None else None}
            ),
        )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def submit_and_wait(
    adapter: DurableJobAdapter,
    workflow_id: str,
    task_identity: str,
    request: dict[str, Any],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.02,
) -> dict[str, Any]:
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise JobRequestError("mock-job timeout must be greater than 0 and at most 30 seconds")
    if not math.isfinite(poll_interval_seconds) or not 0 < poll_interval_seconds <= 0.5:
        raise JobRequestError(
            "mock-job poll interval must be greater than 0 and at most 0.5 seconds"
        )
    submitted = adapter.submit_or_get_job(workflow_id, task_identity, request)
    request_key = submitted["request_key"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        current = adapter.get_job_by_request_key(request_key)
        if current is None:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: submitted job is missing for request key {request_key}"
            )
        if current["request_digest"] != submitted["request_digest"]:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: request digest changed for request key {request_key}"
            )
        if current["state"] == "SUCCEEDED":
            if current["result"] is None:
                raise JobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: SUCCEEDED job has no result for {request_key}"
                )
            return {
                "job_id": current["job_id"],
                "request_key": request_key,
                "status": "SUCCEEDED",
                "result": current["result"],
            }
        if current["state"] == "FAILED":
            raise JobFailedError(
                f"mock-job FAILED: job_id={current['job_id']} request_key={request_key} "
                f"error={current['error']}"
            )
        if current["state"] not in {"QUEUED", "RUNNING"}:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: unsupported job state {current['state']!r} "
                f"for request key {request_key}"
            )
        if time.monotonic() >= deadline:
            raise JobTimeoutError(
                f"mock-job timeout: job_id={current['job_id']} request_key={request_key} "
                f"ledger_state={current['state']}; job was not cancelled"
            )
        time.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))


def process_one_job(
    database_path: Path, log_path: Path, service_id: str | None = None
) -> str | None:
    job = claim_next_job(database_path, service_id)
    if job is None:
        return None
    request = job["request"]
    duration_ms = request["duration_ms"]
    started = time.monotonic()
    elapsed_ms = 0
    while elapsed_ms < duration_ms:
        time.sleep(min(0.05, (duration_ms - elapsed_ms) / 1000))
        elapsed_ms = min(duration_ms, int((time.monotonic() - started) * 1000))
        update_job_progress(database_path, job["job_id"], elapsed_ms, duration_ms, service_id)

    if request["payload"] == {"mock_failure": True}:
        error = "mock service deterministic failure"
        finish_job(database_path, job["job_id"], error=error)
        _append_service_event(log_path, "job_failed", job["job_id"])
        return cast(str, job["job_id"])

    result = {"operation": "process", "payload": request["payload"], "processed": True}
    finish_job(database_path, job["job_id"], result=result)
    _append_service_event(log_path, "job_succeeded", job["job_id"])
    return cast(str, job["job_id"])


def _append_service_event(path: Path, event: str, job_id: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import os

    record = {"event": event, "timestamp_utc": _utc_now(), "pid": os.getpid()}
    if job_id is not None:
        record["job_id"] = job_id
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
        output.flush()
