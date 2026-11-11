from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

_DIAGNOSTIC_FILENAME = "startup-diagnostic.json"
_MAX_DIAGNOSTIC_BYTES = 4096
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
_SAFE_EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)


class WorkflowStartupStage(StrEnum):
    REQUEST_RESERVED = "request_reserved"
    STARTING_STATE_PERSISTED = "starting_state_persisted"
    MANAGER_VALIDATION = "manager_validation"
    WORKER_CONFIG_WRITE = "worker_config_write"
    WORKER_SPAWN = "worker_spawn"
    WORKER_READINESS_WAIT = "worker_readiness_wait"


class WorkflowStartupFailureReason(StrEnum):
    EXCEPTION = "exception"
    READINESS_INSPECTION_FAILED = "readiness_inspection_failed"
    READINESS_TIMEOUT = "readiness_timeout"
    READINESS_UNCONFIRMED = "readiness_unconfirmed"
    WORKER_FAILED = "worker_failed"


class WorkflowStartupDiagnostic:
    def __init__(self, workflow_id: str, run_id: str, run_dir: Path) -> None:
        self._workflow_id = _safe_workflow_id(workflow_id)
        self._run_id = run_id if _RUN_ID.fullmatch(run_id) else None
        self._run_dir = run_dir
        self._stages: list[dict[str, str]] = []
        self._current_stage: WorkflowStartupStage | None = None
        self._failure: dict[str, object] | None = None
        self._worker_config_write_completed = False
        self._worker_spawned: bool | None = False
        self._persist_attempted = False

    def begin(self, stage: WorkflowStartupStage) -> None:
        if self._current_stage is stage and self._stages:
            last = self._stages[-1]
            if last["stage"] == stage.value and last["phase"] == "started":
                return
        self._current_stage = stage
        self._stages.append({"stage": stage.value, "phase": "started", "at_utc": _utc_now()})

    def complete_if_active(self, stage: WorkflowStartupStage) -> None:
        if self._current_stage is not stage:
            return
        if self._stages and self._stages[-1]["phase"] == "completed":
            return
        self._stages.append({"stage": stage.value, "phase": "completed", "at_utc": _utc_now()})
        if stage is WorkflowStartupStage.WORKER_CONFIG_WRITE:
            self._worker_config_write_completed = True
        elif stage is WorkflowStartupStage.WORKER_SPAWN:
            self._worker_spawned = True

    def mark_worker_spawned(self, spawned: bool | None) -> None:
        self._worker_spawned = spawned

    def fail(
        self,
        error: BaseException | None,
        *,
        reason: WorkflowStartupFailureReason = WorkflowStartupFailureReason.EXCEPTION,
    ) -> None:
        if self._failure is not None:
            return
        stage = self._current_stage or WorkflowStartupStage.REQUEST_RESERVED
        exception_class = None if error is None else _safe_exception_class(error)
        self._failure = {
            "stage": stage.value,
            "exception_class": exception_class,
            "reason": reason.value,
            "at_utc": _utc_now(),
            "worker_config_write_completed": self._worker_config_write_completed,
            "worker_spawned": self._worker_spawned,
        }
        self._persist()

    def _persist(self) -> None:
        if self._persist_attempted or self._failure is None:
            return
        self._persist_attempted = True
        if self._workflow_id is None or self._run_id is None:
            return
        document: dict[str, object] = {
            "version": 1,
            "workflow_id": self._workflow_id,
            "run_id": self._run_id,
            "stages": self._stages,
            "failure": self._failure,
        }
        try:
            payload = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(payload) > _MAX_DIAGNOSTIC_BYTES:
                return
            _write_diagnostic_exclusive(self._run_dir, payload)
        except BaseException:
            return


_ACTIVE_DIAGNOSTIC: ContextVar[WorkflowStartupDiagnostic | None] = ContextVar(
    "workflow_startup_diagnostic", default=None
)


@contextmanager
def startup_diagnostic_scope(diagnostic: WorkflowStartupDiagnostic) -> Iterator[None]:
    token = _ACTIVE_DIAGNOSTIC.set(diagnostic)
    try:
        yield
    finally:
        _ACTIVE_DIAGNOSTIC.reset(token)


def current_startup_diagnostic() -> WorkflowStartupDiagnostic | None:
    return _ACTIVE_DIAGNOSTIC.get()


def _write_diagnostic_exclusive(run_dir: Path, payload: bytes) -> None:
    if run_dir.is_symlink():
        return
    resolved_run_dir = run_dir.resolve(strict=True)
    if resolved_run_dir != run_dir.absolute() or not resolved_run_dir.is_dir():
        return
    diagnostic_path = resolved_run_dir / _DIAGNOSTIC_FILENAME
    if diagnostic_path.is_symlink() or diagnostic_path.exists():
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(diagnostic_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_workflow_id(value: str) -> str | None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    normalized = str(parsed)
    return normalized if normalized == value.lower() else None


def _safe_exception_class(error: BaseException) -> str:
    name = type(error).__name__
    return name if _SAFE_EXCEPTION_NAME.fullmatch(name) else "UnknownException"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
