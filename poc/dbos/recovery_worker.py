from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from dbos import DBOS, DBOSConfig
from recovery_config import APPLICATION_NAME, APPLICATION_VERSION, EXECUTOR_ID
from sqlalchemy.engine import URL

_event_log: Path | None = None
_heartbeat_log: Path | None = None
_continue_marker: Path | None = None
_step2_timeout_seconds = 120.0


def _record_step_start(step_name: str) -> None:
    if _event_log is None:
        raise RuntimeError("event log path was not initialized")
    event = {
        "event": "step_started",
        "step": step_name,
        "pid": os.getpid(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    with _event_log.open("a", encoding="utf-8", newline="\n") as event_file:
        event_file.write(json.dumps(event, sort_keys=True) + "\n")
        event_file.flush()
        os.fsync(event_file.fileno())


def _heartbeat(stop_event: threading.Event) -> None:
    if _heartbeat_log is None:
        raise RuntimeError("heartbeat log path was not initialized")
    sequence = 0
    while not stop_event.wait(0.1):
        heartbeat = {
            "event": "worker_heartbeat",
            "pid": os.getpid(),
            "sequence": sequence,
            "timestamp_utc": datetime.now(UTC).isoformat(),
        }
        with _heartbeat_log.open("a", encoding="utf-8", newline="\n") as heartbeat_file:
            heartbeat_file.write(json.dumps(heartbeat, sort_keys=True) + "\n")
            heartbeat_file.flush()
            os.fsync(heartbeat_file.fileno())
        sequence += 1


@DBOS.step()
def step1() -> str:
    _record_step_start("step1")
    return "step1 complete"


@DBOS.step()
def step2() -> str:
    _record_step_start("step2")
    if _continue_marker is None:
        raise RuntimeError("continue marker path was not initialized")
    deadline = time.monotonic() + _step2_timeout_seconds
    while not _continue_marker.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("step2 timed out waiting for the continue marker")
        time.sleep(0.05)
    return "step2 complete"


@DBOS.step()
def step3() -> str:
    _record_step_start("step3")
    return "step3 complete"


@DBOS.workflow()
def recovery_workflow() -> list[str]:
    return [step1(), step2(), step3()]


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
    parser = argparse.ArgumentParser(description="DBOS recovery experiment worker.")
    parser.add_argument("--mode", choices=("start", "recover"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--heartbeat-log", type=Path, required=True)
    parser.add_argument("--continue-marker", type=Path, required=True)
    parser.add_argument("--startup-metadata", type=Path, required=True)
    parser.add_argument("--workflow-id")
    parser.add_argument("--step2-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--completion-timeout-seconds", type=float, default=30.0)
    return parser.parse_args()


def main() -> int:
    global _continue_marker, _event_log, _heartbeat_log, _step2_timeout_seconds

    args = _parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    args.event_log.parent.mkdir(parents=True, exist_ok=True)
    args.heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
    args.startup_metadata.parent.mkdir(parents=True, exist_ok=True)
    _event_log = args.event_log
    _heartbeat_log = args.heartbeat_log
    _continue_marker = args.continue_marker
    _step2_timeout_seconds = args.step2_timeout_seconds

    if args.mode == "recover" and not args.workflow_id:
        raise ValueError("recovery mode requires the existing workflow ID")

    DBOS(config=_build_config(args.database))
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    try:
        DBOS.launch()
        metadata: dict[str, object] = {
            "mode": args.mode,
            "pid": os.getpid(),
            "workflow_id": args.workflow_id,
        }
        if args.mode == "start":
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                args=(heartbeat_stop,),
                name="dbos-poc-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            handle = DBOS.start_workflow(recovery_workflow)
            metadata["workflow_id"] = handle.workflow_id
            _write_json(args.startup_metadata, metadata)
            while True:
                time.sleep(1.0)

        _write_json(args.startup_metadata, metadata)
        deadline = time.monotonic() + args.completion_timeout_seconds
        while time.monotonic() < deadline:
            status = _read_workflow_status(args.database, args.workflow_id)
            if status == "SUCCESS":
                return 0
            time.sleep(0.05)
        final_status = _read_workflow_status(args.database, args.workflow_id)
        raise TimeoutError(
            f"workflow {args.workflow_id} did not reach SUCCESS; status={final_status!r}"
        )
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        DBOS.destroy()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"recovery worker failed: {error}", file=sys.stderr, flush=True)
        raise
