from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    return json.loads(canonical_json(request))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


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


def claim_next_job(database_path: Path) -> dict[str, Any] | None:
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
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        connection.execute("COMMIT")
        return DurableJobAdapter._row_to_job(row)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def update_job_progress(database_path: Path, job_id: str, completed_ms: int, total_ms: int) -> None:
    progress = {
        "completed_ms": min(completed_ms, total_ms),
        "percent": 100 if total_ms == 0 else min(100, int(completed_ms * 100 / total_ms)),
        "total_ms": total_ms,
    }
    with _connect(database_path) as connection:
        connection.execute(
            "UPDATE jobs SET progress_json = ?, updated_at = ? "
            "WHERE job_id = ? AND state = 'RUNNING'",
            (canonical_json(progress), _utc_now(), job_id),
        )


def finish_job(
    database_path: Path, job_id: str, result: Any = None, error: str | None = None
) -> None:
    state = "FAILED" if error is not None else "SUCCEEDED"
    result_json = None if error is not None else canonical_json(result)
    with _connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE jobs SET state = ?, result_json = ?, error = ?, updated_at = ?, "
            "progress_json = ? WHERE job_id = ? AND state = 'RUNNING'",
            (
                state,
                result_json,
                error,
                _utc_now(),
                canonical_json({"completed_ms": 0, "percent": 100}),
                job_id,
            ),
        )
        if cursor.rowcount != 1:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: running claim disappeared for job {job_id}"
            )


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


def process_one_job(database_path: Path, log_path: Path) -> str | None:
    job = claim_next_job(database_path)
    if job is None:
        return None
    request = job["request"]
    duration_ms = request["duration_ms"]
    started = time.monotonic()
    elapsed_ms = 0
    while elapsed_ms < duration_ms:
        time.sleep(min(0.05, (duration_ms - elapsed_ms) / 1000))
        elapsed_ms = min(duration_ms, int((time.monotonic() - started) * 1000))
        update_job_progress(database_path, job["job_id"], elapsed_ms, duration_ms)

    if request["payload"] == {"mock_failure": True}:
        error = "mock service deterministic failure"
        finish_job(database_path, job["job_id"], error=error)
        _append_service_event(log_path, "job_failed", job["job_id"])
        return job["job_id"]

    result = {"operation": "process", "payload": request["payload"], "processed": True}
    finish_job(database_path, job["job_id"], result=result)
    _append_service_event(log_path, "job_succeeded", job["job_id"])
    return job["job_id"]


def _append_service_event(path: Path, event: str, job_id: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import os

    record = {"event": event, "timestamp_utc": _utc_now(), "pid": os.getpid()}
    if job_id is not None:
        record["job_id"] = job_id
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
        output.flush()
