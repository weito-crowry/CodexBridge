from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

ActivityType = Literal[
    "turn_started",
    "turn_completed",
    "turn_failed",
    "turn_interrupted",
    "command_started",
    "command_completed",
    "file_change_started",
    "file_change_completed",
    "agent_message",
    "approval_requested",
    "approval_resolved",
    "user_input_requested",
    "user_input_resolved",
    "error",
]
ActivityStatus = Literal[
    "in_progress",
    "completed",
    "failed",
    "interrupted",
    "declined",
    "requested",
    "resolved",
]
ActivityDetailValue = str | int | bool | None | list[str]

_MAX_ACTIVITIES_PER_THREAD = 500
_MAX_TEXT_CHARS = 2_000
_MAX_DETAILS = 16
_MAX_DETAIL_LIST_ITEMS = 100
_SENSITIVE_DETAIL_TERMS = (
    "raw",
    "encrypted",
    "reasoning",
    "chainofthought",
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "apikey",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|token|password|secret|credential)\b\s*(?:[:=]|\s)\s*)[^\s,;]+"
    ),
)


class ActivitySubscription:
    """A bounded, process-local stream of safe Activity records."""

    def __init__(self, store: ActivityStore, thread_id: str | None) -> None:
        self._store = store
        self._thread_id = thread_id
        self._queue: asyncio.Queue[Activity] = asyncio.Queue(maxsize=100)
        self._closed = False

    async def get(self) -> Activity:
        if self._closed:
            raise RuntimeError("activity subscription is closed")
        return await self._queue.get()

    def _publish(self, activity: Activity) -> None:
        if self._closed or (self._thread_id is not None and self._thread_id != activity.thread_id):
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
        try:
            self._queue.put_nowait(activity)
        except asyncio.QueueFull:
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._store._unsubscribe(self)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    bounded = value[-limit:]
    for pattern in _SECRET_PATTERNS:
        bounded = pattern.sub(r"\1<redacted>", bounded)
    return bounded


def _safe_details(
    details: Mapping[str, ActivityDetailValue] | None,
) -> dict[str, ActivityDetailValue]:
    if details is None:
        return {}
    safe: dict[str, ActivityDetailValue] = {}
    for key, value in list(details.items())[:_MAX_DETAILS]:
        normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
        if any(term in normalized_key for term in _SENSITIVE_DETAIL_TERMS):
            continue
        if isinstance(value, str):
            safe[key] = _bounded_text(value) or ""
        elif isinstance(value, (int, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            safe[key] = [_bounded_text(item) or "" for item in value[:_MAX_DETAIL_LIST_ITEMS]]
    return safe


@dataclass(frozen=True, slots=True)
class Activity:
    activity_id: str
    timestamp: str
    thread_id: str
    turn_id: str | None
    item_id: str | None
    type: ActivityType
    status: ActivityStatus
    summary: str | None = None
    details: dict[str, ActivityDetailValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "activity_id": self.activity_id,
            "timestamp": self.timestamp,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "type": self.type,
            "status": self.status,
            "summary": self.summary,
            "details": {
                key: list(value) if isinstance(value, list) else value
                for key, value in self.details.items()
            },
        }


class ActivityStore:
    """Bounded, process-local history of safe observations for each native thread."""

    def __init__(self) -> None:
        self._activities: dict[str, deque[Activity]] = {}
        self._subscribers: set[ActivitySubscription] = set()

    def subscribe(self, thread_id: str | None = None) -> ActivitySubscription:
        subscription = ActivitySubscription(self, thread_id)
        self._subscribers.add(subscription)
        return subscription

    def _unsubscribe(self, subscription: ActivitySubscription) -> None:
        self._subscribers.discard(subscription)

    def add(
        self,
        *,
        thread_id: str,
        turn_id: str | None,
        type: ActivityType,
        status: ActivityStatus,
        item_id: str | None = None,
        summary: str | None = None,
        details: Mapping[str, ActivityDetailValue] | None = None,
    ) -> Activity:
        activity = Activity(
            activity_id=uuid4().hex,
            timestamp=_timestamp(),
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            type=type,
            status=status,
            summary=_bounded_text(summary),
            details=_safe_details(details),
        )
        self._activities.setdefault(thread_id, deque(maxlen=_MAX_ACTIVITIES_PER_THREAD)).append(
            activity
        )
        for subscriber in tuple(self._subscribers):
            subscriber._publish(activity)
        return activity

    def get_recent(
        self, thread_id: str, turn_id: str | None = None, *, limit: int = 20
    ) -> tuple[Activity, ...]:
        if limit < 1:
            return ()
        activities = self._activities.get(thread_id)
        if activities is None:
            return ()
        matching = [
            activity for activity in activities if turn_id is None or activity.turn_id == turn_id
        ]
        return tuple(matching[-limit:])

    def latest(self, thread_id: str, turn_id: str | None = None) -> Activity | None:
        recent = self.get_recent(thread_id, turn_id, limit=1)
        return recent[0] if recent else None

    def latest_known_turn(self, thread_id: str) -> str | None:
        activities = self._activities.get(thread_id)
        if activities is None:
            return None
        for activity in reversed(activities):
            if activity.turn_id is not None:
                return activity.turn_id
        return None
