"""Incremental read-only discovery of running Codex session logs."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..paths import AllowedPathPolicy

_START_EVENTS = {"task_started", "turn_started"}
_TERMINAL_EVENTS = {
    "task_complete",
    "task_completed",
    "task_failed",
    "turn_complete",
    "turn_completed",
    "turn_failed",
    "turn_aborted",
    "turn_interrupted",
}
_THREAD_ID_SUFFIX = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE
)
_THREAD_ID_FALLBACK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_RELEVANT_RECORD_MARKERS = (
    b'"session_meta"',
    *(f'"{event}"'.encode("ascii") for event in _START_EVENTS | _TERMINAL_EVENTS),
)
_REVERSE_READ_BYTES = 64 * 1024


def _event_data(raw_line: bytes) -> tuple[str, str | None] | None:
    if not any(marker in raw_line for marker in _RELEVANT_RECORD_MARKERS[1:]):
        return None
    try:
        record = json.loads(raw_line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, Mapping):
        return None
    payload = record.get("payload")
    payload_mapping = payload if isinstance(payload, Mapping) else {}
    event_type = payload_mapping.get("type")
    if not isinstance(event_type, str):
        candidate = record.get("type")
        event_type = candidate if isinstance(candidate, str) else ""
    if event_type not in _START_EVENTS and event_type not in _TERMINAL_EVENTS:
        return None
    turn_id = payload_mapping.get("turn_id", record.get("turn_id"))
    if not isinstance(turn_id, str) or not turn_id:
        turn_id = payload_mapping.get("turnId", record.get("turnId"))
    return event_type, turn_id if isinstance(turn_id, str) and turn_id else None


class _Scanner(Protocol):
    def scan(self, stop_event: threading.Event) -> dict[str, dict[str, object]]: ...


@dataclass(slots=True)
class _SessionState:
    identity: tuple[int, int]
    position: int = 0
    modified_ns: int = 0
    pending: bytes = b""
    cwd: str | None = None
    originator: str | None = None
    turns: dict[str, bool] = field(default_factory=dict)
    latest_turn: str | None = None
    anonymous_turn: int = 0

    @property
    def latest_is_running(self) -> bool:
        return self.latest_turn is not None and self.turns.get(self.latest_turn, False)

    def consume_line(self, raw_line: bytes) -> None:
        if not any(marker in raw_line for marker in _RELEVANT_RECORD_MARKERS):
            return
        try:
            record = json.loads(raw_line)
        except (ValueError, RecursionError):
            return
        if not isinstance(record, Mapping):
            return
        record_type = record.get("type")
        payload = record.get("payload")
        payload_mapping = payload if isinstance(payload, Mapping) else {}
        if record_type == "session_meta":
            cwd = payload_mapping.get("cwd", record.get("cwd"))
            originator = payload_mapping.get("originator", record.get("originator"))
            if isinstance(cwd, str):
                self.cwd = cwd
            if isinstance(originator, str):
                self.originator = originator

        event = _event_data(raw_line)
        if event is None:
            return
        event_type, turn_id = event
        turn_key = f"id:{turn_id}" if isinstance(turn_id, str) and turn_id else None
        if event_type in _START_EVENTS:
            if turn_key is None:
                self.anonymous_turn += 1
                turn_key = f"anonymous:{self.anonymous_turn}"
            self.turns[turn_key] = True
            self.latest_turn = turn_key
            return
        if turn_key is None:
            turn_key = self.latest_turn
        if turn_key is not None and turn_key in self.turns:
            self.turns[turn_key] = False


class CodexSessionScanner:
    """Track the latest turn in allowed Codex session files without full rescans."""

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str],
        codex_home: Path | str | None = None,
    ) -> None:
        self._codex_home: Path | None = None
        self._policy: AllowedPathPolicy | None = None
        self._states: dict[Path, _SessionState] = {}
        try:
            self._policy = AllowedPathPolicy(tuple(allowed_roots))
        except (OSError, RuntimeError, ValueError):
            return
        try:
            if codex_home is None:
                if "CODEX_HOME" in os.environ:
                    configured_home = os.environ["CODEX_HOME"]
                    if not configured_home.strip():
                        return
                    home = Path(configured_home).expanduser()
                else:
                    home = Path.home() / ".codex"
            else:
                home = Path(codex_home).expanduser()
            self._codex_home = home.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return

    @staticmethod
    def _thread_id(path: Path) -> str | None:
        stem = path.stem
        match = _THREAD_ID_SUFFIX.search(stem)
        if match is not None:
            return match.group(1)
        if stem.startswith("rollout-"):
            return None
        return stem if _THREAD_ID_FALLBACK.fullmatch(stem) is not None else None

    @staticmethod
    def _parse_path(path: Path, stop_event: threading.Event) -> _SessionState | None:
        try:
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
            state = _SessionState(
                identity=identity,
                position=info.st_size,
                modified_ns=info.st_mtime_ns,
            )
            with path.open("rb") as session_file:
                first_line = session_file.readline()
                if first_line.endswith(b"\n"):
                    state.consume_line(first_line[:-1].removesuffix(b"\r"))
                state.pending = CodexSessionScanner._trailing_partial(
                    session_file, info.st_size, stop_event
                )
                terminal_ids: set[str] = set()
                terminal_without_id = False
                for line in CodexSessionScanner._complete_lines_reverse(
                    session_file, info.st_size, stop_event
                ):
                    event = _event_data(line)
                    if event is None:
                        continue
                    event_type, turn_id = event
                    if event_type in _TERMINAL_EVENTS:
                        if turn_id is None:
                            terminal_without_id = True
                        else:
                            terminal_ids.add(turn_id)
                        continue
                    turn_key = f"id:{turn_id}" if turn_id is not None else "anonymous:1"
                    state.turns[turn_key] = (
                        turn_id not in terminal_ids
                        if turn_id is not None
                        else not terminal_without_id
                    )
                    state.latest_turn = turn_key
                    break
            return state
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _trailing_partial(session_file: BinaryIO, size: int, stop_event: threading.Event) -> bytes:
        position = size
        partial = bytearray()
        while position > 0 and not stop_event.is_set():
            start = max(0, position - _REVERSE_READ_BYTES)
            session_file.seek(start)
            chunk = session_file.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                partial[:0] = chunk[newline + 1 :]
                return bytes(partial)
            partial[:0] = chunk
            position = start
        return bytes(partial)

    @staticmethod
    def _complete_lines_reverse(
        session_file: BinaryIO, size: int, stop_event: threading.Event
    ) -> Iterator[bytes]:
        position = size
        carry = b""
        first_chunk = True
        while position > 0 and not stop_event.is_set():
            start = max(0, position - _REVERSE_READ_BYTES)
            session_file.seek(start)
            chunk = session_file.read(position - start)
            position = start
            data = chunk + carry
            if first_chunk:
                if data.endswith(b"\n"):
                    data = data[:-1]
                else:
                    newline = data.rfind(b"\n")
                    data = data[: newline + 1] if newline >= 0 else b""
                first_chunk = False
            while not stop_event.is_set():
                newline = data.rfind(b"\n")
                if newline < 0:
                    break
                line = data[newline + 1 :]
                data = data[:newline]
                if line:
                    yield line.removesuffix(b"\r")
            carry = data
        if carry and not stop_event.is_set():
            yield carry.removesuffix(b"\r")

    @staticmethod
    def _apply_appended_data(path: Path, state: _SessionState, stop_event: threading.Event) -> None:
        try:
            with path.open("rb") as session_file:
                session_file.seek(state.position)
                while not stop_event.is_set():
                    raw_line = session_file.readline()
                    if not raw_line:
                        state.position = session_file.tell()
                        break
                    state.position = session_file.tell()
                    if not raw_line.endswith(b"\n"):
                        state.pending += raw_line
                        break
                    line = state.pending + raw_line[:-1]
                    state.pending = b""
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    state.consume_line(line)
        except (OSError, RuntimeError, ValueError):
            return

    @staticmethod
    def _candidate_files(sessions_dir: Path, stop_event: threading.Event) -> list[Path]:
        result: list[Path] = []
        try:
            for path in sessions_dir.rglob("*.jsonl"):
                if stop_event.is_set():
                    break
                try:
                    if path.is_file():
                        result.append(path)
                except (OSError, RuntimeError, ValueError):
                    continue
        except (OSError, RuntimeError, ValueError):
            pass
        return result

    def scan(self, stop_event: threading.Event | None = None) -> dict[str, dict[str, object]]:
        cancelled = stop_event or threading.Event()
        codex_home = self._codex_home
        policy = self._policy
        if cancelled.is_set() or codex_home is None or policy is None:
            self._states.clear()
            return {}
        try:
            sessions_dir = (codex_home / "sessions").resolve(strict=True)
            if not sessions_dir.is_dir():
                self._states.clear()
                return {}
        except (OSError, RuntimeError, ValueError):
            self._states.clear()
            return {}

        paths = self._candidate_files(sessions_dir, cancelled)
        current_paths = set(paths)
        self._states = {
            path: state for path, state in self._states.items() if path in current_paths
        }
        running: dict[str, tuple[int, dict[str, object]]] = {}
        for path in paths:
            if cancelled.is_set():
                break
            thread_id = self._thread_id(path)
            if thread_id is None:
                continue
            try:
                info = path.stat()
            except (OSError, RuntimeError, ValueError):
                continue
            state = self._states.get(path)
            identity = (info.st_dev, info.st_ino)
            if state is None or state.identity != identity or info.st_size < state.position:
                state = self._parse_path(path, cancelled)
                if state is None:
                    self._states.pop(path, None)
                    continue
                self._states[path] = state
            elif info.st_size > state.position or info.st_mtime_ns != state.modified_ns:
                self._apply_appended_data(path, state, cancelled)
                state.modified_ns = info.st_mtime_ns
            if not state.latest_is_running or state.cwd is None:
                continue
            try:
                cwd = policy.validate_cwd(state.cwd)
            except (OSError, RuntimeError, ValueError):
                continue
            row: dict[str, object] = {"id": thread_id, "cwd": cwd}
            if state.originator is not None:
                row["originator"] = state.originator
            candidate = (info.st_mtime_ns, row)
            if thread_id not in running or candidate[0] >= running[thread_id][0]:
                running[thread_id] = candidate
        return {thread_id: row for thread_id, (_modified, row) in running.items()}


class _ScanSignals(QObject):
    completed = Signal(object)


class _ScanJob(QRunnable):
    def __init__(
        self, scanner: _Scanner, stop_event: threading.Event, signals: _ScanSignals
    ) -> None:
        super().__init__()
        self._scanner = scanner
        self._stop_event = stop_event
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._scanner.scan(self._stop_event)
        except Exception:
            result = {}
        self._signals.completed.emit(result)


class ExternalThreadMonitor(QObject):
    """Run session scans away from the GUI and suppress results after shutdown."""

    updated = Signal(object)

    def __init__(
        self,
        *,
        allowed_roots: Sequence[str],
        codex_home: Path | str | None = None,
        scanner: _Scanner | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._scanner = (
            scanner
            if scanner is not None
            else CodexSessionScanner(allowed_roots=allowed_roots, codex_home=codex_home)
        )
        self._stop_event = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _ScanSignals(self)
        self._signals.completed.connect(self._scan_completed)
        self._busy = False
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return not self._stopped

    @Slot()
    def request_scan(self) -> None:
        if self._stopped or self._busy:
            return
        self._busy = True
        self._pool.start(_ScanJob(self._scanner, self._stop_event, self._signals))

    @Slot(object)
    def _scan_completed(self, result: object) -> None:
        self._busy = False
        if self._stopped:
            return
        if isinstance(result, Mapping):
            self.updated.emit(dict(result))
        else:
            self.updated.emit({})

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        self._pool.clear()
        self._pool.waitForDone()
