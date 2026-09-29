from __future__ import annotations

import json
import math
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import (
    bridge_log_path,
    bridge_runtime_stderr_log_path,
    bridge_runtime_stdout_log_path,
    console_log_path,
)

_ALLOWED_FIELDS = (
    "method",
    "path",
    "status",
    "duration_ms",
    "protocol_version",
    "exception_type",
    "cancelled",
    "exit_code",
)
_SAFE_EVENT = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_SAFE_TIMESTAMP = re.compile(r"[A-Za-z0-9:.+_-]{1,40}\Z")
_SAFE_BARE_VALUE = re.compile(r"[A-Za-z0-9_.:/+-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class DiagnosticSource:
    label: str
    path: Path
    structured: bool


def default_diagnostic_sources() -> tuple[DiagnosticSource, ...]:
    return (
        DiagnosticSource("Bridge", bridge_log_path(), True),
        DiagnosticSource("Console", console_log_path(), True),
        DiagnosticSource("Bridge stdout", bridge_runtime_stdout_log_path(), False),
        DiagnosticSource("Bridge stderr", bridge_runtime_stderr_log_path(), False),
    )


@dataclass(slots=True)
class _TailState:
    initialized: bool = False
    offset: int = 0
    last_size: int = 0
    identity: tuple[int, int] | None = None
    pending: bytearray = field(default_factory=bytearray)
    discard_until_newline: bool = False
    truncated_line: bool = False


class DiagnosticsReader:
    """Bounded, read-only incremental tail for Console diagnostics sources."""

    def __init__(
        self,
        *,
        sources: tuple[DiagnosticSource, ...] | None = None,
        initial_tail_bytes: int = 192 * 1024,
        max_poll_bytes: int = 64 * 1024,
        max_chunk_bytes: int = 16 * 1024,
        max_line_chars: int = 4096,
        max_output_lines: int = 256,
        max_queued_lines: int = 512,
    ) -> None:
        if min(initial_tail_bytes, max_poll_bytes, max_chunk_bytes, max_line_chars) <= 0:
            raise ValueError("diagnostics read limits must be positive")
        if min(max_output_lines, max_queued_lines) <= 0:
            raise ValueError("diagnostics line limits must be positive")
        self.sources = default_diagnostic_sources() if sources is None else sources
        self.initial_tail_bytes = initial_tail_bytes
        self.max_poll_bytes = max_poll_bytes
        self.max_chunk_bytes = max_chunk_bytes
        self.max_line_chars = max_line_chars
        self.max_output_lines = max_output_lines
        self._max_queued_lines = max_queued_lines
        self._states: list[_TailState] = []
        self._ready: deque[str] = deque(maxlen=max_queued_lines)
        self.bytes_read_last_poll = 0

    @property
    def is_caught_up(self) -> bool:
        return not self._ready and all(state.offset >= state.last_size for state in self._states)

    def reset(self) -> None:
        """Forget offsets so reopening the pane loads a fresh recent tail."""

        self._states.clear()
        self._ready.clear()
        self.bytes_read_last_poll = 0

    def poll(self) -> list[str]:
        if len(self._states) != len(self.sources):
            self._states = [_TailState() for _ in self.sources]
            self._ready.clear()

        self.bytes_read_last_poll = 0
        available_by_source: list[int] = []
        for source, state in zip(self.sources, self._states, strict=True):
            try:
                stat = source.path.stat()
            except FileNotFoundError:
                state.initialized = False
                state.offset = 0
                state.last_size = 0
                state.identity = None
                state.pending.clear()
                state.discard_until_newline = False
                state.truncated_line = False
                available_by_source.append(0)
                continue
            except OSError:
                available_by_source.append(0)
                continue

            size = stat.st_size
            identity = self._file_identity(stat)
            replaced = (
                state.initialized
                and identity is not None
                and state.identity is not None
                and identity != state.identity
            )
            truncated = state.initialized and size < state.offset
            if not state.initialized or replaced or truncated:
                state.offset = max(0, size - self.initial_tail_bytes)
                state.pending.clear()
                state.discard_until_newline = state.offset > 0
                state.truncated_line = False
                state.initialized = True
            state.identity = identity
            state.last_size = size
            available_by_source.append(max(0, size - state.offset))

        remaining = self.max_poll_bytes
        while remaining > 0 and any(available_by_source):
            progressed = False
            for index, (source, state) in enumerate(zip(self.sources, self._states, strict=True)):
                available = available_by_source[index]
                if available <= 0 or remaining <= 0:
                    continue
                amount = min(available, remaining, self.max_chunk_bytes)
                try:
                    with source.path.open("rb") as stream:
                        stream.seek(state.offset)
                        chunk = stream.read(amount)
                except OSError:
                    available_by_source[index] = 0
                    continue
                if not chunk:
                    available_by_source[index] = 0
                    continue
                state.offset += len(chunk)
                available_by_source[index] = max(0, available - len(chunk))
                remaining -= len(chunk)
                self.bytes_read_last_poll += len(chunk)
                progressed = True
                self._consume(source, state, chunk)
            if not progressed:
                break

        output: list[str] = []
        while self._ready and len(output) < self.max_output_lines:
            output.append(self._ready.popleft())
        return output

    @staticmethod
    def _file_identity(stat_result: os.stat_result) -> tuple[int, int] | None:
        device = getattr(stat_result, "st_dev", 0)
        file_index = getattr(stat_result, "st_ino", 0)
        if isinstance(device, int) and isinstance(file_index, int) and (device or file_index):
            return device, file_index
        return None

    def _consume(self, source: DiagnosticSource, state: _TailState, chunk: bytes) -> None:
        if state.discard_until_newline:
            newline = chunk.find(b"\n")
            if newline < 0:
                return
            chunk = chunk[newline + 1 :]
            state.discard_until_newline = False

        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            raw = bytes(state.pending) + part
            was_truncated = state.truncated_line
            self._set_pending(state, b"")
            self._emit(source, raw, was_truncated)
        if parts[-1]:
            if not state.truncated_line:
                self._set_pending(state, bytes(state.pending) + parts[-1])

    def _set_pending(self, state: _TailState, value: bytes) -> None:
        limit = self.max_line_chars * 4
        if len(value) > limit:
            state.pending[:] = value[:limit]
            state.truncated_line = True
        else:
            state.pending[:] = value
            if not value:
                state.truncated_line = False

    def _emit(self, source: DiagnosticSource, raw: bytes, truncated: bool) -> None:
        raw = raw.removesuffix(b"\r")
        decoded = raw.decode("utf-8", errors="replace")
        if source.structured:
            line = self._format_structured(source.label, decoded)
        else:
            line = self._format_plain(source.label, decoded, truncated)
        if line is not None:
            self._ready.append(line[: self.max_line_chars])

    def _format_plain(self, label: str, text: str, truncated: bool) -> str:
        safe_text = "".join(
            "�" if ord(character) < 32 or ord(character) == 127 else character for character in text
        )
        prefix = f"[{label}] "
        content_limit = max(0, self.max_line_chars - len(prefix))
        if len(safe_text) > content_limit:
            safe_text = safe_text[:content_limit]
            truncated = True
        if truncated and content_limit:
            safe_text = safe_text[: max(0, content_limit - 1)] + "…"
        return prefix + safe_text

    def _format_structured(self, label: str, text: str) -> str | None:
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(payload, dict):
            return None
        event = payload.get("event")
        if not isinstance(event, str) or _SAFE_EVENT.fullmatch(event) is None:
            return None
        timestamp = payload.get("timestamp")
        prefix = ""
        if isinstance(timestamp, str) and _SAFE_TIMESTAMP.fullmatch(timestamp):
            prefix = f"{timestamp} "
        fields: list[str] = []
        for key in _ALLOWED_FIELDS:
            if key not in payload:
                continue
            value = self._safe_field_value(key, payload[key])
            if value is not None:
                fields.append(f"{key}={value}")
        suffix = f" {' '.join(fields)}" if fields else ""
        return f"{prefix}[{label}] {event}{suffix}"

    @staticmethod
    def _safe_field_value(key: str, value: object) -> str | None:
        if key in {"method", "path", "protocol_version", "exception_type"}:
            if not isinstance(value, str) or len(value) > 128:
                return None
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                return None
            if _SAFE_BARE_VALUE.fullmatch(value):
                return value
            return json.dumps(value, ensure_ascii=True)
        if key in {"status", "exit_code"}:
            return str(value) if isinstance(value, int) and not isinstance(value, bool) else None
        if key == "duration_ms":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            numeric = float(value)
            return f"{numeric:.3f}".rstrip("0").rstrip(".") if math.isfinite(numeric) else None
        if key == "cancelled":
            return str(value).lower() if isinstance(value, bool) else None
        return None
