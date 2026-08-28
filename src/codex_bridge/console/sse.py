from __future__ import annotations

import codecs
import json
from dataclasses import dataclass
from typing import Any


class SseProtocolError(ValueError):
    """Raised when an SSE response cannot be safely bounded or decoded."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str | None
    event_id: str | None
    data: str


class SseParser:
    """Incrementally parse UTF-8 SSE frames without retaining unbounded input."""

    def __init__(self, max_buffer_bytes: int = 256 * 1024) -> None:
        if max_buffer_bytes < 1:
            raise ValueError("max_buffer_bytes must be positive")
        self._max_buffer_bytes = max_buffer_bytes
        self.reset()

    def reset(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._text_buffer = ""
        self._undecoded_bytes = 0
        self._event: str | None = None
        self._event_id: str | None = None
        self._data_lines: list[str] = []

    def _pending_size(self) -> int:
        fields = [self._event or "", self._event_id or "", *self._data_lines]
        return (
            self._undecoded_bytes
            + len(self._text_buffer.encode("utf-8"))
            + sum(len(field.encode("utf-8")) for field in fields)
        )

    def _ensure_bounded(self) -> None:
        if self._pending_size() > self._max_buffer_bytes:
            self.reset()
            raise SseProtocolError("SSE buffer exceeded its limit")

    def _reset_event(self) -> None:
        self._event = None
        self._event_id = None
        self._data_lines = []

    def _dispatch(self) -> SseEvent | None:
        if not self._data_lines:
            self._reset_event()
            return None
        event = SseEvent(
            event=self._event,
            event_id=self._event_id,
            data="\n".join(self._data_lines),
        )
        self._reset_event()
        return event

    def _handle_line(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            self._event = value
        elif field == "id":
            self._event_id = value
        elif field == "data":
            self._data_lines.append(value)
        self._ensure_bounded()
        return None

    def _consume_text(self, text: str) -> tuple[SseEvent, ...]:
        self._text_buffer += text
        events: list[SseEvent] = []
        while True:
            newline_positions = [
                position
                for position in (
                    self._text_buffer.find("\n"),
                    self._text_buffer.find("\r"),
                )
                if position >= 0
            ]
            if not newline_positions:
                break
            position = min(newline_positions)
            newline_length = 1
            if self._text_buffer[position] == "\r":
                if position + 1 == len(self._text_buffer):
                    break
                newline_length = 2 if self._text_buffer[position + 1] == "\n" else 1
            line = self._text_buffer[:position]
            self._text_buffer = self._text_buffer[position + newline_length :]
            event = self._handle_line(line)
            if event is not None:
                events.append(event)
            self._ensure_bounded()
        self._ensure_bounded()
        return tuple(events)

    def feed(self, chunk: bytes) -> tuple[SseEvent, ...]:
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            self.reset()
            raise SseProtocolError("SSE response is not valid UTF-8") from exc
        self._undecoded_bytes += len(chunk) - len(text.encode("utf-8"))
        return self._consume_text(text)

    def finish(self) -> tuple[SseEvent, ...]:
        try:
            text = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            self.reset()
            raise SseProtocolError("SSE response is not valid UTF-8") from exc
        events = self._consume_text(text)
        self.reset()
        return events


def _bounded_string(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def parse_activity_event(event: SseEvent) -> dict[str, object] | None:
    """Return only a bounded safe Activity shape from one SSE event."""
    if event.event != "activity":
        return None
    try:
        payload: Any = json.loads(event.data)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not {
        "activity_id",
        "timestamp",
        "thread_id",
        "turn_id",
        "type",
        "status",
        "summary",
        "details",
    }.issubset(payload):
        return None

    activity_id = _bounded_string(payload.get("activity_id"), 512)
    timestamp = _bounded_string(payload.get("timestamp"), 128)
    thread_id = _bounded_string(payload.get("thread_id"), 512)
    activity_type = _bounded_string(payload.get("type"), 128)
    status = _bounded_string(payload.get("status"), 128)
    turn_id = payload.get("turn_id")
    summary = payload.get("summary")
    details = payload.get("details")
    if (
        activity_id is None
        or timestamp is None
        or thread_id is None
        or activity_type is None
        or status is None
        or (turn_id is not None and _bounded_string(turn_id, 512) is None)
        or (summary is not None and _bounded_string(summary, 2_000) is None)
        or not isinstance(details, dict)
    ):
        return None

    safe_details: dict[str, object] = {}
    exit_code = details.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        safe_details["exit_code"] = exit_code
    decision = _bounded_string(details.get("decision"), 256)
    if decision is not None:
        safe_details["decision"] = decision
    paths = details.get("paths")
    if (
        isinstance(paths, list)
        and len(paths) <= 100
        and all(isinstance(path, str) and len(path) <= 4_096 for path in paths)
    ):
        safe_details["paths"] = list(paths)

    return {
        "activity_id": activity_id,
        "timestamp": timestamp,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "type": activity_type,
        "status": status,
        "summary": summary,
        "details": safe_details,
    }
