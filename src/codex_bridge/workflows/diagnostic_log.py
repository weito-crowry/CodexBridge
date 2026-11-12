from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_MAX_RECORD_BYTES = 2 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_BRIDGE_FILE = "bridge-lifecycle.jsonl"
_WORKER_FILE = "worker-lifecycle.jsonl"
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_WORKFLOW_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)
_LAUNCH_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_EXCEPTION_CLASS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MODES = frozenset({"start", "recover"})
_STATES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
        "unknown",
        "completed",
        "stopped",
        "shutdown_failed",
    }
)
_REASON_CODES = frozenset(
    {
        "same_request_id",
        "request_digest_mismatch",
        "queued_replay_failed_to_start",
        "manager_start_failed",
        "worker_failed_before_ready",
        "readiness_unconfirmed",
        "request_start_cancelled",
        "unexpected_start_failure",
        "readiness_wait_failed",
        "spawn_os_error",
        "spawn_value_error",
        "spawn_cancelled_outcome_unknown",
        "spawn_exception_outcome_unknown",
        "shutdown_signal_write_failed",
        "process_already_exited",
        "terminate_failed",
        "kill_failed",
        "worker_did_not_exit",
        "dbos_configure_failed",
        "dbos_launch_failed",
        "worker_setup_failed",
        "workflow_start_failed",
        "workflow_recovery_failed",
        "workflow_metadata_persist_failed",
        "workflow_wait_failed",
        "result_persist_failed",
        "result_output_failed",
        "dbos_destroy_failed",
        "shutdown_requested",
    }
)

_BRIDGE_FIELDS: dict[str, frozenset[str]] = {
    "request_reserved": frozenset({"request_digest"}),
    "request_replayed": frozenset({"state", "reason_code", "request_digest"}),
    "request_conflict": frozenset({"state", "reason_code", "request_digest"}),
    "workflow_state_changed": frozenset({"previous_state", "state", "reason_code"}),
    "request_start_failed": frozenset({"state", "reason_code", "exception_class"}),
    "worker_launch_requested": frozenset({"launch_id", "mode"}),
    "worker_spawn_succeeded": frozenset({"launch_id", "mode", "pid"}),
    "worker_spawn_failed": frozenset({"launch_id", "mode", "reason_code", "exception_class"}),
    "worker_spawn_outcome_unknown": frozenset(
        {"launch_id", "mode", "reason_code", "exception_class"}
    ),
    "shutdown_signal_requested": frozenset({"launch_id", "pid"}),
    "shutdown_signal_failed": frozenset({"launch_id", "pid", "reason_code", "exception_class"}),
    "worker_terminate_requested": frozenset({"launch_id", "pid"}),
    "worker_terminate_failed": frozenset({"launch_id", "pid", "reason_code", "exception_class"}),
    "worker_kill_requested": frozenset({"launch_id", "pid"}),
    "worker_kill_failed": frozenset({"launch_id", "pid", "reason_code", "exception_class"}),
    "worker_shutdown_failed": frozenset({"launch_id", "pid", "reason_code"}),
    "worker_exited": frozenset(
        {"launch_id", "mode", "pid", "returncode", "shutdown_requested", "state"}
    ),
}
_WORKER_FIELDS: dict[str, frozenset[str]] = {
    "worker_bootstrap": frozenset({"launch_id", "mode", "pid"}),
    "dbos_ready": frozenset({"launch_id", "mode", "pid"}),
    "workflow_started": frozenset({"launch_id", "mode", "pid"}),
    "workflow_recovery_attached": frozenset({"launch_id", "mode", "pid"}),
    "result_persisted": frozenset({"launch_id", "mode", "pid"}),
    "shutdown_observed": frozenset({"launch_id", "mode", "pid", "reason_code"}),
    "worker_failed": frozenset({"launch_id", "mode", "pid", "reason_code", "exception_class"}),
}
_REQUIRED_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "bridge": {
        "request_reserved": frozenset({"workflow_id", "request_digest"}),
        "request_replayed": frozenset({"workflow_id", "state", "reason_code"}),
        "request_conflict": frozenset({"workflow_id", "state", "reason_code"}),
        "workflow_state_changed": frozenset({"workflow_id", "state"}),
        "request_start_failed": frozenset({"workflow_id", "state", "reason_code"}),
        "worker_launch_requested": frozenset({"launch_id", "mode"}),
        "worker_spawn_succeeded": frozenset({"launch_id", "mode", "pid"}),
        "worker_spawn_failed": frozenset({"launch_id", "mode", "reason_code", "exception_class"}),
        "worker_spawn_outcome_unknown": frozenset(
            {"launch_id", "mode", "reason_code", "exception_class"}
        ),
        "shutdown_signal_requested": frozenset({"launch_id", "pid"}),
        "shutdown_signal_failed": frozenset({"launch_id", "pid", "reason_code", "exception_class"}),
        "worker_terminate_requested": frozenset({"launch_id", "pid"}),
        "worker_terminate_failed": frozenset(
            {"launch_id", "pid", "reason_code", "exception_class"}
        ),
        "worker_kill_requested": frozenset({"launch_id", "pid"}),
        "worker_kill_failed": frozenset({"launch_id", "pid", "reason_code", "exception_class"}),
        "worker_shutdown_failed": frozenset({"launch_id", "pid", "reason_code"}),
        "worker_exited": frozenset(
            {"launch_id", "mode", "pid", "returncode", "shutdown_requested", "state"}
        ),
    },
    "worker": {
        "worker_bootstrap": frozenset({"mode", "pid"}),
        "dbos_ready": frozenset({"mode", "pid"}),
        "workflow_started": frozenset({"workflow_id", "mode", "pid"}),
        "workflow_recovery_attached": frozenset({"workflow_id", "mode", "pid"}),
        "result_persisted": frozenset({"workflow_id", "mode", "pid"}),
        "shutdown_observed": frozenset({"workflow_id", "mode", "pid", "reason_code"}),
        "worker_failed": frozenset({"mode", "pid", "reason_code", "exception_class"}),
    },
}
_WRITE_LOCKS = tuple(threading.Lock() for _ in range(32))
_LOGGER = logging.getLogger(__name__)


def append_lifecycle_event(
    run_dir: Path,
    *,
    source: Literal["bridge", "worker"],
    event: str,
    run_id: object,
    workflow_id: object = None,
    launch_id: object = None,
    mode: object = None,
    pid: object = None,
    returncode: object = None,
    shutdown_requested: object = None,
    previous_state: object = None,
    state: object = None,
    reason_code: object = None,
    exception_class: object = None,
    job_id: object = None,
    task_identity: object = None,
    thread_id: object = None,
    turn_id: object = None,
    request_digest: object = None,
) -> bool:
    """Append one bounded, allowlisted diagnostic event without affecting its caller."""
    try:
        payload = _event_payload(
            source=source,
            event=event,
            run_id=run_id,
            workflow_id=workflow_id,
            launch_id=launch_id,
            mode=mode,
            pid=pid,
            returncode=returncode,
            shutdown_requested=shutdown_requested,
            previous_state=previous_state,
            state=state,
            reason_code=reason_code,
            exception_class=exception_class,
            job_id=job_id,
            task_identity=task_identity,
            thread_id=thread_id,
            turn_id=turn_id,
            request_digest=request_digest,
        )
        if payload is None:
            _warn_write_failure()
            return False
        encoded = (
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            _warn_write_failure()
            return False
        file_name = _BRIDGE_FILE if source == "bridge" else _WORKER_FILE
        log_path = run_dir / file_name
        lock_key = os.path.normcase(os.path.abspath(os.fspath(log_path)))
        lock = _WRITE_LOCKS[hash(lock_key) % len(_WRITE_LOCKS)]
        with lock:
            descriptor = _open_append_file(run_dir, log_path)
            try:
                if os.fstat(descriptor).st_size + len(encoded) > _MAX_FILE_BYTES:
                    raise OSError("diagnostic file limit reached")
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise OSError("diagnostic append made no progress")
                    offset += written
            finally:
                os.close(descriptor)
        return True
    except Exception:
        _warn_write_failure()
        return False


def _event_payload(
    *,
    source: str,
    event: str,
    run_id: object,
    workflow_id: object,
    launch_id: object,
    mode: object,
    pid: object,
    returncode: object,
    shutdown_requested: object,
    previous_state: object,
    state: object,
    reason_code: object,
    exception_class: object,
    job_id: object,
    task_identity: object,
    thread_id: object,
    turn_id: object,
    request_digest: object,
) -> dict[str, object] | None:
    if source == "bridge":
        event_fields = _BRIDGE_FIELDS.get(event)
    elif source == "worker":
        event_fields = _WORKER_FIELDS.get(event)
    else:
        return None
    if event_fields is None or not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        return None
    payload: dict[str, object] = {
        "version": 1,
        "timestamp_utc": datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "source": source,
        "event": event,
        "run_id": run_id,
    }
    values = {
        "workflow_id": workflow_id,
        "launch_id": launch_id,
        "mode": mode,
        "pid": pid,
        "returncode": returncode,
        "shutdown_requested": shutdown_requested,
        "previous_state": previous_state,
        "state": state,
        "reason_code": reason_code,
        "exception_class": exception_class,
        "job_id": job_id,
        "task_identity": task_identity,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "request_digest": request_digest,
    }
    common_fields = frozenset({"workflow_id", "mode"})
    for name, value in values.items():
        if value is None:
            continue
        if name not in event_fields and name not in common_fields:
            return None
        validated = _safe_value(name, value)
        if validated is not None:
            payload[name] = validated
    required = _REQUIRED_FIELDS[source].get(event, frozenset())
    if not required.issubset(payload):
        return None
    return payload


def _safe_value(name: str, value: object) -> str | int | bool | None:
    if name == "workflow_id":
        return value if isinstance(value, str) and _WORKFLOW_ID.fullmatch(value) else None
    if name == "launch_id":
        return value if isinstance(value, str) and _LAUNCH_ID.fullmatch(value) else None
    if name == "mode":
        return value if isinstance(value, str) and value in _MODES else None
    if name in {"state", "previous_state"}:
        return value if isinstance(value, str) and value in _STATES else None
    if name == "reason_code":
        return value if isinstance(value, str) and value in _REASON_CODES else None
    if name == "exception_class":
        return value if isinstance(value, str) and _EXCEPTION_CLASS.fullmatch(value) else None
    if name == "request_digest":
        return value if isinstance(value, str) and _DIGEST.fullmatch(value) else None
    if name in {"job_id", "task_identity", "thread_id", "turn_id"}:
        return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None
    if name == "pid":
        return value if type(value) is int and 0 < value <= 2**31 - 1 else None
    if name == "returncode":
        return value if type(value) is int and -(2**31) <= value < 2**31 else None
    if name == "shutdown_requested":
        return value if type(value) is bool else None
    return None


def _open_append_file(run_dir: Path, log_path: Path) -> int:
    if log_path.parent != run_dir or _is_reparse_point(os.lstat(run_dir)):
        raise OSError("unsafe diagnostic directory")
    directory_info = os.lstat(run_dir)
    if not stat.S_ISDIR(directory_info.st_mode):
        raise OSError("diagnostic path is not a directory")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(2):
        try:
            file_info = os.lstat(log_path)
        except FileNotFoundError:
            try:
                return os.open(log_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
        if _is_reparse_point(file_info) or not stat.S_ISREG(file_info.st_mode):
            raise OSError("unsafe diagnostic file")
        descriptor = os.open(log_path, flags)
        opened_info = os.fstat(descriptor)
        if _is_reparse_point(opened_info) or not stat.S_ISREG(opened_info.st_mode):
            os.close(descriptor)
            raise OSError("unsafe diagnostic file")
        return descriptor
    raise OSError("diagnostic file changed during open")


def _is_reparse_point(file_info: os.stat_result) -> bool:
    if stat.S_ISLNK(file_info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(file_info, "st_file_attributes", 0) & reparse_flag)


def _warn_write_failure() -> None:
    try:
        _LOGGER.warning("workflow lifecycle diagnostic write failed")
    except Exception:
        pass
