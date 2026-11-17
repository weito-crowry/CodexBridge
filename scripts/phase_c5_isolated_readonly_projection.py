"""Read-only provenance and Workflow projection audit for a completed C5 attempt."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Never

APPROVAL_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
HEAD_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
RUN_ID_PATTERN = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
EVENT_NAME_PATTERN = re.compile(r"[a-z0-9_]{1,80}\Z", re.ASCII)
TEMP_PREFIX = "codexbridge-phase-b5-"
MAX_MARKER_BYTES = 1024 * 1024
MAX_MARKER_EVENTS = 4096
MAX_TEMP_CANDIDATES = 4096
SQLITE_TIMEOUT_SECONDS = 0.25

RUN_NAMES = ("A", "B", "C")
EXPECTED_JOBS = {"A": 1, "B": 2, "C": 3}
REQUEST_STATES = {
    "queued",
    "starting",
    "running",
    "succeeded",
    "failed",
    "failed_to_start",
    "needs_reconciliation",
    "unknown",
}
WORKER_STATES = {"not_started", "running", "completed", "failed", "unknown"}
WORKFLOW_STATES = REQUEST_STATES
COVERAGE_VALUES = {"complete", "partial", "unavailable"}
COVERAGE_REASONS = {
    "evidence_conflict",
    "graph_invalid",
    "journal_busy",
    "journal_corrupt",
    "journal_event_limit",
    "journal_invalid",
    "journal_missing",
    "journal_schema_invalid",
    "journal_too_large",
    "journal_unavailable",
    "journal_unsafe_path",
    "ledger_unavailable",
    "run_integrity_invalid",
    "truncated",
    "workflow_not_confirmed",
}
COUNT_LIMITS = {"nodes": 256, "edges": 1024, "executions": 512}
RUN_FILE_NAMES = (
    "workflow.yaml",
    "input.json",
    "manifest.json",
    "result.json",
    "worker-entry-status.json",
    "dbos.sqlite",
    "codex_jobs.sqlite",
    "node_events.sqlite",
)
MARKER_KEYS = {
    "schema_version",
    "approval_id",
    "approved_head",
    "runner_source_sha256",
    "status",
    "created_at_utc",
    "updated_at_utc",
    "post_count",
    "job_count",
    "runs",
    "events",
}
RUN_KEYS = {
    "expected_jobs",
    "client_request_id",
    "workflow_id",
    "run_id",
    "state",
    "observed_jobs",
}
EVENT_KEYS = {"at_utc", "event"}

REPOSITORY_ROOT = Path(__file__).absolute().parents[1]


@dataclass(frozen=True, slots=True)
class MarkerRun:
    name: str
    expected_jobs: int
    client_request_id: str
    workflow_id: str
    run_id: str
    observed_jobs: int


@dataclass(frozen=True, slots=True)
class ValidatedMarker:
    approval_id: str
    approved_head: str
    runner_source_sha256: str
    runs: tuple[MarkerRun, ...]


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    state_root: Path
    request_states: tuple[tuple[str, str], ...]
    direct_candidate_count: int
    readable_database_count: int


@dataclass(frozen=True, slots=True)
class ReadOnlyProducts:
    find_workflow: Callable[..., Any]
    inspect_run: Callable[..., Any]
    snapshot_from_inspection: Callable[..., Any]
    project_tasks: Callable[..., Any]
    project_events: Callable[..., Any]


class InspectionFailure(Exception):
    """An error with a safe stage and fixed code; the original message is discarded."""

    def __init__(
        self,
        stage: str,
        code: str,
        exception_class: str | None = None,
    ) -> None:
        super().__init__()
        self.stage = stage
        self.code = code
        self.exception_class = exception_class


class _DuplicateJsonKey(ValueError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise _ArgumentError


class _ArgumentError(Exception):
    pass


def _safe_exception_class(error: BaseException) -> str:
    name = type(error).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name, re.ASCII):
        return name
    return "Exception"


def _blocked(
    stage: str,
    code: str,
    exception_class: str | None = None,
) -> InspectionFailure:
    return InspectionFailure(stage, code, exception_class)


def _is_reparse_point(path: Path, result: os.stat_result | None = None) -> bool:
    try:
        metadata = result if result is not None else os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _lstat(path: Path, stage: str) -> os.stat_result:
    try:
        result = os.lstat(path)
    except OSError as error:
        raise _blocked(stage, "path_unavailable", _safe_exception_class(error)) from None
    if _is_reparse_point(path, result):
        raise _blocked(stage, "reparse_path")
    return result


def _path_parts(path: Path) -> list[Path]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    return list(reversed([absolute, *absolute.parents]))


def _require_safe_directory(path: Path, stage: str) -> None:
    for part in _path_parts(path):
        result = _lstat(part, stage)
        if not stat.S_ISDIR(result.st_mode):
            raise _blocked(stage, "directory_required")


def _require_safe_parent_chain(path: Path, stage: str) -> None:
    _require_safe_directory(path.parent, stage)


def _is_contained_child(parent: Path, child: Path) -> bool:
    parent_text = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(parent))))
    child_text = os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(child))))
    try:
        common = os.path.normcase(os.path.normpath(os.path.commonpath((parent_text, child_text))))
    except (OSError, ValueError):
        return False
    return common == parent_text and Path(child_text).parent == Path(parent_text)


def _read_regular_file(path: Path, *, limit: int, stage: str) -> bytes:
    _require_safe_parent_chain(path, stage)
    before = _lstat(path, stage)
    if not stat.S_ISREG(before.st_mode):
        raise _blocked(stage, "regular_file_required")
    if before.st_size > limit:
        raise _blocked(stage, "file_too_large")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _blocked(stage, "file_open_failed", _safe_exception_class(error)) from None

    try:
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _is_reparse_point(path, opened) or not stat.S_ISREG(opened.st_mode):
                raise _blocked(stage, "opened_file_invalid")
            if _file_identity(before) != _file_identity(opened):
                raise _blocked(stage, "file_changed_during_read")
            content = stream.read(limit + 1)
            if len(content) > limit:
                raise _blocked(stage, "file_too_large")
    except InspectionFailure:
        raise
    except OSError as error:
        raise _blocked(stage, "file_read_failed", _safe_exception_class(error)) from None

    after = _lstat(path, stage)
    if _file_identity(before) != _file_identity(after):
        raise _blocked(stage, "file_changed_during_read")
    _require_safe_parent_chain(path, stage)
    return content


def _file_identity(result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(result.st_dev),
        int(result.st_ino),
        int(result.st_size),
        int(result.st_mtime_ns),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError


def _parse_marker_bytes(content: bytes) -> Any:
    try:
        text = content.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError):
        raise _blocked("marker_parse", "invalid_json") from None


def _valid_identifier(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and "\x00" not in value
        and all(ord(character) >= 0x20 for character in value)
    )


def _valid_timestamp(value: object) -> bool:
    if type(value) is not str or not 20 <= len(value) <= 40:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_marker_data(data: object, approval_id: str) -> ValidatedMarker:
    if APPROVAL_ID_PATTERN.fullmatch(approval_id) is None:
        raise _blocked("marker_validation", "approval_id_invalid")
    if type(data) is not dict or set(data) != MARKER_KEYS:
        raise _blocked("marker_validation", "marker_schema_invalid")
    if (
        type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("approval_id") != approval_id
        or type(data.get("approved_head")) is not str
        or HEAD_PATTERN.fullmatch(data["approved_head"]) is None
        or type(data.get("runner_source_sha256")) is not str
        or SHA256_PATTERN.fullmatch(data["runner_source_sha256"]) is None
        or data.get("status") != "live_runs_pass_gui_not_run"
        or type(data.get("post_count")) is not int
        or data["post_count"] != 3
        or type(data.get("job_count")) is not int
        or data["job_count"] != 6
        or not _valid_timestamp(data.get("created_at_utc"))
        or not _valid_timestamp(data.get("updated_at_utc"))
    ):
        raise _blocked("marker_validation", "marker_success_contract_mismatch")

    raw_runs = data.get("runs")
    if type(raw_runs) is not dict or set(raw_runs) != set(RUN_NAMES):
        raise _blocked("marker_validation", "runs_schema_invalid")

    runs: list[MarkerRun] = []
    workflow_ids: set[str] = set()
    run_ids: set[str] = set()
    request_ids: set[str] = set()
    for name in RUN_NAMES:
        raw_run = raw_runs[name]
        if type(raw_run) is not dict or set(raw_run) != RUN_KEYS:
            raise _blocked("marker_validation", "run_schema_invalid")
        expected = EXPECTED_JOBS[name]
        client_request_id = raw_run.get("client_request_id")
        workflow_id = raw_run.get("workflow_id")
        run_id = raw_run.get("run_id")
        observed_jobs = raw_run.get("observed_jobs")
        if (
            type(raw_run.get("expected_jobs")) is not int
            or raw_run["expected_jobs"] != expected
            or type(raw_run.get("state")) is not str
            or raw_run["state"] != "succeeded"
            or type(observed_jobs) is not int
            or observed_jobs != expected
            or type(client_request_id) is not str
            or not _valid_identifier(client_request_id)
            or type(workflow_id) is not str
            or not _valid_identifier(workflow_id)
            or type(run_id) is not str
            or RUN_ID_PATTERN.fullmatch(run_id) is None
            or client_request_id in request_ids
            or workflow_id in workflow_ids
            or run_id in run_ids
        ):
            raise _blocked("marker_validation", "run_success_contract_mismatch")
        request_ids.add(client_request_id)
        workflow_ids.add(workflow_id)
        run_ids.add(run_id)
        runs.append(
            MarkerRun(
                name=name,
                expected_jobs=expected,
                client_request_id=client_request_id,
                workflow_id=workflow_id,
                run_id=run_id,
                observed_jobs=observed_jobs,
            )
        )

    events = data.get("events")
    if type(events) is not list or not 1 <= len(events) <= MAX_MARKER_EVENTS:
        raise _blocked("marker_validation", "events_schema_invalid")
    for event in events:
        if (
            type(event) is not dict
            or set(event) != EVENT_KEYS
            or not _valid_timestamp(event.get("at_utc"))
            or type(event.get("event")) is not str
            or EVENT_NAME_PATTERN.fullmatch(event["event"]) is None
        ):
            raise _blocked("marker_validation", "events_schema_invalid")

    return ValidatedMarker(
        approval_id=approval_id,
        approved_head=data["approved_head"],
        runner_source_sha256=data["runner_source_sha256"],
        runs=tuple(runs),
    )


def _marker_path(repository_root: Path, approval_id: str) -> Path:
    if APPROVAL_ID_PATTERN.fullmatch(approval_id) is None:
        raise _blocked("marker_validation", "approval_id_invalid")
    return repository_root / "docs" / ".phase_c5_attempt_budgets" / f"{approval_id}.json"


def _read_marker(repository_root: Path, approval_id: str) -> ValidatedMarker:
    try:
        if APPROVAL_ID_PATTERN.fullmatch(approval_id) is None:
            raise _blocked("marker_validation", "approval_id_invalid")
        _require_safe_directory(repository_root, "marker_parse")
        path = _marker_path(repository_root, approval_id)
        content = _read_regular_file(path, limit=MAX_MARKER_BYTES, stage="marker_parse")
    except InspectionFailure:
        raise
    except Exception as error:
        raise _blocked("marker_parse", "marker_unavailable", _safe_exception_class(error)) from None
    data = _parse_marker_bytes(content)
    return validate_marker_data(data, approval_id)


def _windows_temp_root() -> Path:
    if os.name != "nt" or sys.platform != "win32":
        raise _blocked("candidate_match", "windows_native_required")
    previous = tempfile.tempdir
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        get_temp_path = ctypes.windll.kernel32.GetTempPathW
        get_temp_path.argtypes = (ctypes.c_uint32, ctypes.c_wchar_p)
        get_temp_path.restype = ctypes.c_uint32
        length = get_temp_path(len(buffer), buffer)
        if length == 0 or length >= len(buffer):
            raise _blocked("candidate_match", "temp_root_unavailable")
        tempfile.tempdir = buffer.value
        return Path(os.path.abspath(tempfile.gettempdir()))
    except InspectionFailure:
        raise
    except Exception as error:
        raise _blocked(
            "candidate_match", "temp_root_unavailable", _safe_exception_class(error)
        ) from None
    finally:
        tempfile.tempdir = previous


def _open_readonly_database(
    database_path: Path,
    connect: Callable[..., Any] = sqlite3.connect,
) -> Any:
    uri = database_path.as_uri() + "?mode=ro"
    connection = connect(
        uri,
        uri=True,
        timeout=SQLITE_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("PRAGMA query_only").fetchone()
        if row is None or row[0] != 1:
            raise _blocked("candidate_match", "sqlite_readonly_unavailable")
    except Exception:
        connection.close()
        raise
    return connection


def _query_marker_rows(
    database_path: Path,
    marker: ValidatedMarker,
    *,
    connect: Callable[..., Any] = sqlite3.connect,
) -> tuple[tuple[str, str], ...] | None:
    connection: Any | None = None
    try:
        _require_safe_parent_chain(database_path, "candidate_match")
        database_stat = _lstat(database_path, "candidate_match")
        if not stat.S_ISREG(database_stat.st_mode):
            return None
        connection = _open_readonly_database(database_path, connect)
        matched: list[tuple[str, str]] = []
        for run in marker.runs:
            rows = connection.execute(
                "SELECT workflow_id, run_id, state FROM workflow_requests "
                "WHERE workflow_id = ? AND run_id = ?",
                (run.workflow_id, run.run_id),
            ).fetchall()
            if len(rows) != 1:
                return ()
            workflow_id, run_id, state = rows[0]
            if (
                workflow_id != run.workflow_id
                or run_id != run.run_id
                or type(state) is not str
                or state not in REQUEST_STATES
            ):
                return ()
            matched.append((run.name, state))
        return tuple(matched)
    except InspectionFailure:
        return None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def find_unique_state_root(
    temp_root: Path,
    marker: ValidatedMarker,
    *,
    connect: Callable[..., Any] = sqlite3.connect,
) -> CandidateMatch:
    _require_safe_directory(temp_root, "candidate_match")
    direct_candidate_count = 0
    readable_database_count = 0
    matches: list[tuple[Path, tuple[tuple[str, str], ...]]] = []
    try:
        with os.scandir(temp_root) as entries:
            for entry in entries:
                if not entry.name.casefold().startswith(TEMP_PREFIX):
                    continue
                candidate = Path(entry.path)
                try:
                    candidate_stat = os.lstat(candidate)
                except OSError:
                    continue
                if _is_reparse_point(candidate, candidate_stat) or not stat.S_ISDIR(
                    candidate_stat.st_mode
                ):
                    continue
                if not _is_contained_child(temp_root, candidate):
                    continue
                _require_safe_directory(candidate, "candidate_match")
                direct_candidate_count += 1
                if direct_candidate_count > MAX_TEMP_CANDIDATES:
                    raise _blocked("candidate_match", "candidate_limit_exceeded")
                local_app_data = candidate / "localappdata"
                bridge_root = local_app_data / "CodexBridge"
                state_root = bridge_root / "workflows"
                try:
                    _require_safe_directory(local_app_data, "candidate_match")
                    _require_safe_directory(bridge_root, "candidate_match")
                    _require_safe_directory(state_root, "candidate_match")
                    database_path = state_root / "requests.sqlite"
                    database_stat = _lstat(database_path, "candidate_match")
                    if not stat.S_ISREG(database_stat.st_mode):
                        continue
                except InspectionFailure:
                    continue
                request_states = _query_marker_rows(database_path, marker, connect=connect)
                if request_states is None:
                    continue
                readable_database_count += 1
                if len(request_states) == 3:
                    matches.append((state_root, request_states))
    except InspectionFailure:
        raise
    except OSError as error:
        raise _blocked(
            "candidate_match", "temp_enumeration_failed", _safe_exception_class(error)
        ) from None

    if len(matches) != 1:
        raise _blocked("candidate_match", "unique_state_root_required")

    state_root, request_states = matches[0]
    return CandidateMatch(
        state_root=state_root,
        request_states=request_states,
        direct_candidate_count=direct_candidate_count,
        readable_database_count=readable_database_count,
    )


def _validate_run_paths(state_root: Path, marker: ValidatedMarker) -> None:
    _require_safe_directory(state_root, "candidate_match")
    for run in marker.runs:
        run_directory = state_root / run.run_id
        if not _is_contained_child(state_root, run_directory):
            raise _blocked("candidate_match", "run_path_outside_root")
        _require_safe_directory(run_directory, "candidate_match")
        for filename in RUN_FILE_NAMES:
            path = run_directory / filename
            _require_safe_parent_chain(path, "candidate_match")
            try:
                result = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise _blocked(
                    "candidate_match", "run_file_unavailable", _safe_exception_class(error)
                ) from None
            if _is_reparse_point(path, result) or not stat.S_ISREG(result.st_mode):
                raise _blocked("candidate_match", "run_file_unsafe")


def _load_products() -> ReadOnlyProducts:
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from codex_bridge.workflows import service, task_projection
    except Exception as error:
        raise _blocked(
            "product_import", "readonly_projection_unavailable", _safe_exception_class(error)
        ) from None
    finally:
        sys.dont_write_bytecode = previous
    try:
        return ReadOnlyProducts(
            find_workflow=service._find_workflow,
            inspect_run=service._inspect_run,
            snapshot_from_inspection=service._snapshot_from_inspection,
            project_tasks=task_projection.project_workflow_tasks,
            project_events=task_projection.project_workflow_task_events,
        )
    except Exception as error:
        raise _blocked(
            "product_import", "readonly_projection_unavailable", _safe_exception_class(error)
        ) from None


def _call_product(stage: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return function(*args, **kwargs)
    except Exception as error:
        raise _blocked(stage, "projection_call_failed", _safe_exception_class(error)) from None


def _safe_enum(value: object, allowed: set[str], stage: str) -> str:
    if type(value) is not str or value not in allowed:
        raise _blocked(stage, "enum_invalid")
    return value


def _safe_count(value: object, name: str, stage: str) -> int:
    limit = COUNT_LIMITS[name]
    if type(value) is not int or not 0 <= value <= limit:
        raise _blocked(stage, "count_invalid")
    return value


def _format_run(
    run: MarkerRun,
    request_row: Any,
    inspection: Any,
    snapshot: Any,
    task_projection: Any,
    event_projection: Any,
) -> dict[str, Any]:
    stage = "safe_formatter"
    try:
        request_state = _safe_enum(request_row.state, REQUEST_STATES, stage)
        public_status = _safe_enum(snapshot.status, WORKFLOW_STATES, stage)
        worker_state = _safe_enum(snapshot.worker_state, WORKER_STATES, stage)
        if type(snapshot.terminal) is not bool:
            raise _blocked(stage, "terminal_type_invalid")
        if inspection.result is not None and type(inspection.result) is not dict:
            raise _blocked(stage, "result_type_invalid")

        if run.name == "A":
            public_jobs_count: int | None = None
        elif type(snapshot.jobs) in {tuple, list}:
            if len(snapshot.jobs) > run.expected_jobs:
                raise _blocked(stage, "public_job_count_invalid")
            public_jobs_count = len(snapshot.jobs)
        else:
            public_jobs_count = None

        if type(task_projection) is not dict:
            raise _blocked(stage, "task_projection_type_invalid")
        task_status = _safe_enum(task_projection.get("workflow_status"), WORKFLOW_STATES, stage)
        task_coverage = _safe_enum(task_projection.get("coverage"), COVERAGE_VALUES, stage)
        reason = task_projection.get("coverage_reason")
        if reason is None:
            task_coverage_reason = "none"
        elif type(reason) is str and reason in COVERAGE_REASONS:
            task_coverage_reason = reason
        else:
            task_coverage_reason = "unknown"

        counts = task_projection.get("counts")
        if type(counts) is not dict or set(counts) != set(COUNT_LIMITS):
            raise _blocked(stage, "counts_schema_invalid")
        safe_counts = {
            name: _safe_count(counts[name], name, stage)
            for name in ("nodes", "edges", "executions")
        }

        if type(event_projection) is not dict:
            raise _blocked(stage, "event_projection_type_invalid")
        event_coverage = _safe_enum(event_projection.get("coverage"), COVERAGE_VALUES, stage)
        events = event_projection.get("events")
        if type(events) is not list or len(events) > 100:
            raise _blocked(stage, "event_page_invalid")
        latest_event_id = event_projection.get("latest_event_id")
        if type(latest_event_id) is not int or not 0 <= latest_event_id <= 2**63 - 1:
            raise _blocked(stage, "event_cursor_invalid")
        has_more = event_projection.get("has_more")
        if type(has_more) is not bool:
            raise _blocked(stage, "event_has_more_invalid")

        return {
            "case": run.name,
            "request_state": request_state,
            "public_status": public_status,
            "terminal": snapshot.terminal,
            "worker_state": worker_state,
            "has_result": inspection.result is not None,
            "public_jobs_count": public_jobs_count,
            "task_workflow_status": task_status,
            "task_coverage": task_coverage,
            "task_coverage_reason": task_coverage_reason,
            "counts": safe_counts,
            "event_coverage": event_coverage,
            "events_returned": len(events),
            "latest_event_id": latest_event_id,
            "has_more": has_more,
        }
    except InspectionFailure:
        raise
    except Exception as error:
        raise _blocked(stage, "format_failed", _safe_exception_class(error)) from None


def inspect_attempt(
    approval_id: str,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    temp_root: Path | None = None,
    products: ReadOnlyProducts | None = None,
    connect: Callable[..., Any] = sqlite3.connect,
) -> dict[str, Any]:
    try:
        marker = _read_marker(repository_root, approval_id)
    except InspectionFailure as error:
        return _failure_result(error)
    except Exception as error:
        return _failure_result(
            _blocked("marker_parse", "marker_validation_failed", _safe_exception_class(error))
        )

    try:
        effective_temp_root = temp_root if temp_root is not None else _windows_temp_root()
    except InspectionFailure as error:
        return _failure_result(error)
    except Exception as error:
        return _failure_result(
            _blocked("candidate_match", "temp_root_unavailable", _safe_exception_class(error))
        )

    try:
        match = find_unique_state_root(effective_temp_root, marker, connect=connect)
    except InspectionFailure as error:
        return _failure_result(error)
    except Exception as error:
        return _failure_result(
            _blocked("candidate_match", "candidate_scan_failed", _safe_exception_class(error))
        )

    try:
        _validate_run_paths(match.state_root, marker)
    except InspectionFailure as error:
        return _failure_result(error)
    except Exception as error:
        return _failure_result(
            _blocked("candidate_match", "run_path_validation_failed", _safe_exception_class(error))
        )

    try:
        dependencies = products if products is not None else _load_products()
    except InspectionFailure as error:
        return _failure_result(error)
    except Exception as error:
        return _failure_result(
            _blocked(
                "product_import", "readonly_projection_unavailable", _safe_exception_class(error)
            )
        )

    loaded: list[tuple[MarkerRun, Any, Any, Any]] = []
    for run in marker.runs:
        try:
            request_row = _call_product(
                "find_workflow",
                dependencies.find_workflow,
                match.state_root,
                run.workflow_id,
            )
        except InspectionFailure as error:
            return _failure_result(error)
        if request_row is None:
            return _failure_result(_blocked("find_workflow", "request_identity_mismatch"))
        try:
            row_workflow_id = request_row.workflow_id
            row_run_id = request_row.run_id
        except Exception as error:
            return _failure_result(
                _blocked("find_workflow", "request_identity_invalid", _safe_exception_class(error))
            )
        if row_workflow_id != run.workflow_id or row_run_id != run.run_id:
            return _failure_result(_blocked("find_workflow", "request_identity_mismatch"))
        try:
            inspection = _call_product(
                "inspect_run", dependencies.inspect_run, match.state_root, request_row
            )
        except InspectionFailure as error:
            return _failure_result(error)
        try:
            snapshot = _call_product(
                "snapshot",
                dependencies.snapshot_from_inspection,
                request_row,
                inspection,
            )
        except InspectionFailure as error:
            return _failure_result(error)
        loaded.append((run, request_row, inspection, snapshot))

    projected: list[dict[str, Any]] = []
    for run, request_row, inspection, snapshot in loaded:
        try:
            task_projection = _call_product(
                "tasks_projection",
                dependencies.project_tasks,
                match.state_root,
                request_row,
                inspection,
            )
        except InspectionFailure as error:
            return _failure_result(error)
        try:
            event_projection = _call_product(
                "events_projection",
                dependencies.project_events,
                match.state_root,
                request_row,
                inspection,
                after_id=0,
                limit=100,
            )
        except InspectionFailure as error:
            return _failure_result(error)
        try:
            projected_run = _format_run(
                run,
                request_row,
                inspection,
                snapshot,
                task_projection,
                event_projection,
            )
        except InspectionFailure as error:
            return _failure_result(error)
        projected.append(projected_run)

    return {
        "status": "PASS",
        "stage": "complete",
        "provenance": {
            "direct_candidates": match.direct_candidate_count,
            "readable_request_databases": match.readable_database_count,
            "exact_matches": 1,
        },
        "runs": projected,
    }


def _failure_result(error: InspectionFailure) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "BLOCKED",
        "stage": error.stage,
        "code": error.code,
    }
    if error.exception_class is not None:
        result["exception_class"] = error.exception_class
    return result


def _synthetic_marker() -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    for name in RUN_NAMES:
        jobs = EXPECTED_JOBS[name]
        runs[name] = {
            "expected_jobs": jobs,
            "client_request_id": f"sample-request-{name.lower()}",
            "workflow_id": f"sample-workflow-{name.lower()}",
            "run_id": f"run-{name.lower() * 32}",
            "state": "succeeded",
            "observed_jobs": jobs,
        }
    timestamp = "2026-01-01T00:00:00.000+00:00"
    return {
        "schema_version": 1,
        "approval_id": "a" * 32,
        "approved_head": "b" * 40,
        "runner_source_sha256": "c" * 64,
        "status": "live_runs_pass_gui_not_run",
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "post_count": 3,
        "job_count": 6,
        "runs": runs,
        "events": [{"at_utc": timestamp, "event": "attempt_started"}],
    }


def prepare() -> dict[str, Any]:
    try:
        validate_marker_data(_synthetic_marker(), "a" * 32)
        dependencies = _load_products()
        if not all(
            callable(function)
            for function in (
                dependencies.find_workflow,
                dependencies.inspect_run,
                dependencies.snapshot_from_inspection,
                dependencies.project_tasks,
                dependencies.project_events,
            )
        ):
            raise _blocked("dependency_check", "readonly_functions_unavailable")
        return {
            "status": "PASS",
            "mode": "prepare",
            "fixture": "synthetic",
            "live_data_access": False,
        }
    except InspectionFailure as error:
        result: dict[str, Any] = {
            "status": "BLOCKED",
            "mode": "prepare",
            "stage": error.stage,
            "code": error.code,
        }
        if error.exception_class is not None:
            result["exception_class"] = error.exception_class
        return result
    except Exception as error:
        return {
            "status": "BLOCKED",
            "mode": "prepare",
            "stage": "dependency_check",
            "code": "dependency_check_failed",
            "exception_class": _safe_exception_class(error),
        }


def _is_windows_native() -> bool:
    return os.name == "nt" and sys.platform == "win32"


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        prog="python -m scripts.phase_c5_isolated_readonly_projection",
        description="Read-only projection audit for an already completed isolated C5 attempt.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare", action="store_true", help="check synthetic fixture and dependencies"
    )
    mode.add_argument(
        "--inspect", action="store_true", help="inspect an existing approved attempt read-only"
    )
    parser.add_argument("--approval-id", help="32 lowercase hexadecimal approval identifier")
    return parser


def _emit(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        options = parser.parse_args(arguments)
    except _ArgumentError:
        _emit({"status": "BLOCKED", "stage": "cli_parse", "code": "invalid_arguments"})
        return 2

    if not arguments:
        parser.print_help()
        return 0
    if options.prepare and options.approval_id is not None:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "unexpected_approval_id"})
        return 2
    if options.inspect and options.approval_id is None:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "approval_id_required"})
        return 2
    if not options.prepare and not options.inspect:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "mode_required"})
        return 2
    if not _is_windows_native():
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "windows_native_required"})
        return 2

    result = prepare() if options.prepare else inspect_attempt(options.approval_id)
    _emit(result)
    return 0 if result.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
