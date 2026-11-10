from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig
from side_effect_service import (
    EFFECT_VALUE,
    LOGICAL_OPERATION,
    TransientServiceError,
    lookup_effect,
    read_adapter_state,
    record_dispatch_intent,
    record_receipt,
    record_unknown,
    send_idempotent,
    send_transient,
    send_unqueryable,
)
from sqlalchemy.engine import URL

APPLICATION_NAME = "codexbridge-dbos-side-effects-poc"
APPLICATION_VERSION = "2.0.0"
EXECUTOR_ID = "codexbridge-dbos-side-effects-poc-executor"
STEP_NAME = "side_effect_step"
UNKNOWN_REASON = "dispatch intent exists without a local receipt; no retry"


@dataclass(frozen=True)
class WorkerSettings:
    case_name: str
    external_database: Path
    adapter_database: Path
    event_log: Path
    heartbeat_log: Path
    continue_marker: Path
    barrier_timeout_seconds: float


class OutcomeUnknownError(Exception):
    """The prior non-idempotent send has no locally recorded receipt."""


_settings: WorkerSettings | None = None


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output_file:
        output_file.write(json.dumps(record, sort_keys=True) + "\n")
        output_file.flush()
        os.fsync(output_file.fileno())


def _record_event(event: str, **fields: Any) -> None:
    if _settings is None:
        raise RuntimeError("worker settings are not initialized")
    _append_json_line(
        _settings.event_log,
        {
            "event": event,
            "pid": os.getpid(),
            "timestamp_utc": datetime.now(UTC).isoformat(),
            **fields,
        },
    )


def _heartbeat(stop_event: threading.Event) -> None:
    if _settings is None:
        raise RuntimeError("worker settings are not initialized")
    sequence = 0
    while not stop_event.wait(0.1):
        _append_json_line(
            _settings.heartbeat_log,
            {
                "event": "worker_heartbeat",
                "pid": os.getpid(),
                "sequence": sequence,
                "timestamp_utc": datetime.now(UTC).isoformat(),
            },
        )
        sequence += 1


def _wait_for_continue_marker() -> None:
    if _settings is None:
        raise RuntimeError("worker settings are not initialized")
    deadline = time.monotonic() + _settings.barrier_timeout_seconds
    while not _settings.continue_marker.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("side effect step timed out at the crash barrier")
        time.sleep(0.025)


def _should_retry(error: BaseException) -> bool:
    return isinstance(error, TransientServiceError)


@DBOS.step(
    retries_allowed=True,
    interval_seconds=0.05,
    max_attempts=3,
    backoff_rate=1.0,
    should_retry=_should_retry,
)
def side_effect_step(case_name: str) -> dict[str, object]:
    if _settings is None:
        raise RuntimeError("worker settings are not initialized")
    if case_name != _settings.case_name:
        raise RuntimeError("workflow case differs from worker configuration")
    workflow_id = DBOS.workflow_id
    if not workflow_id:
        raise RuntimeError("DBOS did not provide the active workflow ID")
    request_key = f"{workflow_id}:{LOGICAL_OPERATION}"
    _record_event(
        "step_entered",
        case=case_name,
        logical_operation=LOGICAL_OPERATION,
        workflow_id=workflow_id,
        step=STEP_NAME,
    )

    if case_name == "idempotent_recovery":
        previous = lookup_effect(_settings.external_database, request_key)
        if previous is not None:
            _record_event(
                "external_lookup_hit",
                workflow_id=workflow_id,
                request_key=request_key,
            )
            return previous
        result = send_idempotent(
            _settings.external_database,
            request_key=request_key,
            workflow_id=workflow_id,
            logical_operation=LOGICAL_OPERATION,
        )
        _record_event(
            "external_effect_committed",
            workflow_id=workflow_id,
            request_key=request_key,
            effect_value=EFFECT_VALUE,
        )
        _wait_for_continue_marker()
        return result

    if case_name == "transient_retry":
        try:
            result = send_transient(
                _settings.external_database,
                request_key=request_key,
                workflow_id=workflow_id,
                logical_operation=LOGICAL_OPERATION,
            )
        except TransientServiceError as error:
            _record_event(
                "retry_error",
                workflow_id=workflow_id,
                request_key=request_key,
                error_kind=type(error).__name__,
            )
            raise
        _record_event(
            "external_effect_committed",
            workflow_id=workflow_id,
            request_key=request_key,
            effect_value=EFFECT_VALUE,
        )
        return result

    if case_name == "unqueryable_unknown":
        prior_state = read_adapter_state(_settings.adapter_database, request_key)
        if prior_state is not None:
            if prior_state["receipt"] is not None:
                return {"receipt": prior_state["receipt"], "request_key": request_key}
            if prior_state["status"] == "DISPATCH_INTENT":
                record_unknown(
                    _settings.adapter_database,
                    request_key=request_key,
                    reason=UNKNOWN_REASON,
                )
            _record_event(
                "unknown_rejected",
                workflow_id=workflow_id,
                request_key=request_key,
                reason=UNKNOWN_REASON,
                send_attempted=False,
                service_query_attempted=False,
            )
            raise OutcomeUnknownError(UNKNOWN_REASON)

        record_dispatch_intent(
            _settings.adapter_database,
            request_key=request_key,
            workflow_id=workflow_id,
        )
        _record_event(
            "dispatch_intent_persisted",
            workflow_id=workflow_id,
            request_key=request_key,
        )
        result = send_unqueryable(
            _settings.external_database,
            request_key=request_key,
            workflow_id=workflow_id,
            logical_operation=LOGICAL_OPERATION,
        )
        _record_event(
            "external_effect_committed",
            workflow_id=workflow_id,
            request_key=request_key,
            effect_value=EFFECT_VALUE,
        )
        _wait_for_continue_marker()
        receipt = json.dumps(result, sort_keys=True)
        record_receipt(_settings.adapter_database, request_key=request_key, receipt=receipt)
        return result

    raise ValueError(f"unsupported case: {case_name}")


@DBOS.workflow()
def side_effect_workflow(case_name: str) -> dict[str, object]:
    return side_effect_step(case_name)


def _write_json(path: Path, value: dict[str, object]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        json.dump(value, output_file, sort_keys=True)
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, path)


def _read_workflow_status(database_path: Path, workflow_id: str) -> str | None:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
        row = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
            (workflow_id,),
        ).fetchone()
    return None if row is None else str(row[0])


def _build_config(database_path: Path) -> DBOSConfig:
    database_url = URL.create(
        "sqlite", database=database_path.resolve().as_posix()
    ).render_as_string(hide_password=False)
    return {
        "name": APPLICATION_NAME,
        "application_version": APPLICATION_VERSION,
        "executor_id": EXECUTOR_ID,
        "system_database_url": database_url,
        "log_level": "ERROR",
        "console_log_level": "ERROR",
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Phase 2 DBOS worker child.")
    parser.add_argument(
        "--case",
        choices=("idempotent_recovery", "transient_retry", "unqueryable_unknown"),
        required=True,
    )
    parser.add_argument("--mode", choices=("start", "recover"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--external-database", type=Path, required=True)
    parser.add_argument("--adapter-database", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--heartbeat-log", type=Path, required=True)
    parser.add_argument("--continue-marker", type=Path, required=True)
    parser.add_argument("--startup-metadata", type=Path, required=True)
    parser.add_argument("--workflow-id")
    parser.add_argument("--expected-status", default="SUCCESS")
    parser.add_argument("--completion-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--barrier-timeout-seconds", type=float, default=90.0)
    return parser.parse_args()


def _wait_for_status(
    database_path: Path, workflow_id: str, expected_status: str, timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = _read_workflow_status(database_path, workflow_id)
        except sqlite3.Error:
            status = None
        if status == expected_status:
            return status
        if status in {"SUCCESS", "ERROR", "CANCELLED"}:
            raise RuntimeError(
                f"workflow {workflow_id} reached {status}, expected {expected_status}"
            )
        time.sleep(0.05)
    status = _read_workflow_status(database_path, workflow_id)
    raise TimeoutError(f"workflow {workflow_id} did not reach {expected_status}; status={status!r}")


def main() -> int:
    global _settings

    args = _parse_args()
    if args.completion_timeout_seconds <= 0 or args.barrier_timeout_seconds <= 0:
        raise ValueError("worker timeouts must be positive")
    if args.mode == "recover" and not args.workflow_id:
        raise ValueError("recovery mode requires the existing workflow ID")
    if args.mode == "start" and args.workflow_id:
        raise ValueError("start mode must not receive an existing workflow ID")

    for path in (
        args.database,
        args.external_database,
        args.event_log,
        args.heartbeat_log,
        args.startup_metadata,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
    _settings = WorkerSettings(
        case_name=args.case,
        external_database=args.external_database,
        adapter_database=args.adapter_database,
        event_log=args.event_log,
        heartbeat_log=args.heartbeat_log,
        continue_marker=args.continue_marker,
        barrier_timeout_seconds=args.barrier_timeout_seconds,
    )

    DBOS(config=_build_config(args.database))
    heartbeat_stop = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        DBOS.launch()
        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            args=(heartbeat_stop,),
            name="phase2-worker-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        workflow_id = args.workflow_id
        workflow_started = False
        if args.mode == "start":
            handle = DBOS.start_workflow(side_effect_workflow, args.case)
            workflow_id = handle.workflow_id
            workflow_started = True
        if not workflow_id:
            raise RuntimeError("worker did not resolve a workflow ID")
        _write_json(
            args.startup_metadata,
            {
                "mode": args.mode,
                "pid": os.getpid(),
                "workflow_id": workflow_id,
                "workflow_started_here": workflow_started,
            },
        )

        if args.mode == "start" and args.case in {
            "idempotent_recovery",
            "unqueryable_unknown",
        }:
            while True:
                time.sleep(1.0)

        final_status = _wait_for_status(
            args.database,
            workflow_id,
            args.expected_status,
            args.completion_timeout_seconds,
        )
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "workflow_id": workflow_id,
                    "workflow_started_here": workflow_started,
                    "workflow_status": final_status,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        DBOS.destroy()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Phase 2 worker failed: {error}", file=sys.stderr, flush=True)
        raise
