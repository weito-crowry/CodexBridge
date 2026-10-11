from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from codex_bridge.console import history_stream as history_stream_module
from codex_bridge.console.history_stream import (
    HistoryStreamContext,
    HistoryStreamCoordinator,
)
from codex_bridge.console.widgets import ActivityPane


class _LabelRecorder:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text
        self.events.append(("label", text))


class _ActivityPaneRecorder:
    def __init__(self, events: list[tuple[object, ...]]) -> None:
        self.events = events
        self.snapshots: list[object] = []
        self.activities: list[object] = []
        self.errors: list[str] = []
        self.resolved = 0

    def set_snapshot(self, payload: object) -> None:
        self.snapshots.append(payload)
        self.events.append(("snapshot", payload))

    def append_activity(self, payload: object) -> None:
        self.activities.append(payload)
        self.events.append(("activity", payload))

    def set_error(self, message: str) -> None:
        self.errors.append(message)
        self.events.append(("error", message))

    def mark_approval_resolved(self) -> None:
        self.resolved += 1
        self.events.append(("approval_resolved",))


@dataclass
class _Harness:
    coordinator: HistoryStreamCoordinator
    context: list[HistoryStreamContext]
    activity_pane: _ActivityPaneRecorder
    stream_label: _LabelRecorder
    bottom_label: _LabelRecorder
    events: list[tuple[object, ...]]
    timers: list[tuple[int, Any]]
    refreshes: list[None]
    status_requests: list[None]
    thread_active: list[tuple[str, bool]]
    thread_activity: list[tuple[str, str]]
    usage_snapshots: list[tuple[str, str]]
    approval_syncs: list[None]
    streams: list[tuple[str, int]]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    events: list[tuple[object, ...]] = []
    timers: list[tuple[int, Any]] = []
    context = [HistoryStreamContext(generation=3, thread_id="thread-a", closing=False)]
    activity_pane = _ActivityPaneRecorder(events)
    stream_label = _LabelRecorder(events)
    bottom_label = _LabelRecorder(events)
    refreshes: list[None] = []
    status_requests: list[None] = []
    thread_active: list[tuple[str, bool]] = []
    thread_activity: list[tuple[str, str]] = []
    usage_snapshots: list[tuple[str, str]] = []
    approval_syncs: list[None] = []
    streams: list[tuple[str, int]] = []

    def update_thread_activity(thread_id: str, state: str) -> None:
        thread_activity.append((thread_id, state))
        events.append(("thread_activity", thread_id, state))

    def sync_approval_controls() -> None:
        approval_syncs.append(None)
        events.append(("approval_sync",))

    def start_stream(thread_id: str, generation: int) -> None:
        streams.append((thread_id, generation))
        events.append(("start_stream", thread_id, generation))

    monkeypatch.setattr(
        history_stream_module.QTimer,
        "singleShot",
        lambda delay, callback: timers.append((delay, callback)),
    )

    coordinator = HistoryStreamCoordinator(
        cast(ActivityPane, activity_pane),
        cast(Any, stream_label),
        cast(Any, bottom_label),
        context_provider=lambda: context[0],
        request_selected_status=lambda: status_requests.append(None),
        schedule_selected_history_refresh=lambda: refreshes.append(None),
        set_thread_active=lambda thread_id, active: thread_active.append((thread_id, active)),
        update_thread_activity=update_thread_activity,
        schedule_turn_usage_snapshot=lambda thread_id, turn_id: usage_snapshots.append(
            (thread_id, turn_id)
        ),
        sync_approval_controls=sync_approval_controls,
        start_stream=start_stream,
    )
    return _Harness(
        coordinator,
        context,
        activity_pane,
        stream_label,
        bottom_label,
        events,
        timers,
        refreshes,
        status_requests,
        thread_active,
        thread_activity,
        usage_snapshots,
        approval_syncs,
        streams,
    )


def test_status_snapshot_starts_stream_once_and_preserves_thread_fallback(
    harness: _Harness,
) -> None:
    harness.coordinator.reset_selection("thread-a")
    payload = {"state": "in_progress", "activities": []}

    harness.coordinator.apply_status(payload)
    harness.coordinator.apply_status(payload)

    assert harness.activity_pane.snapshots == [payload, payload]
    assert harness.thread_activity == [("thread-a", "in_progress"), ("thread-a", "in_progress")]
    assert harness.approval_syncs == [None, None]
    assert harness.bottom_label.text == "Thread snapshot updated"
    assert harness.streams == [("thread-a", 3)]
    assert harness.events == [
        ("thread_activity", "thread-a", "in_progress"),
        ("snapshot", payload),
        ("approval_sync",),
        ("label", "Thread snapshot updated"),
        ("start_stream", "thread-a", 3),
        ("thread_activity", "thread-a", "in_progress"),
        ("snapshot", payload),
        ("approval_sync",),
        ("label", "Thread snapshot updated"),
    ]


def test_status_error_retries_status_then_allows_stream_start(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")

    harness.coordinator.status_request_failed("status unavailable")

    assert harness.activity_pane.errors == ["status unavailable"]
    assert len(harness.timers) == 1
    assert harness.timers[0][0] == 1_500
    harness.timers[0][1]()
    assert harness.status_requests == [None]

    harness.coordinator.apply_status({"thread_id": "thread-a", "state": "completed"})
    assert harness.streams == [("thread-a", 3)]


def test_activity_ignores_stale_generation_wrong_thread_and_closing(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")
    activity = {"thread_id": "thread-a", "type": "turn_started", "status": "in_progress"}

    harness.coordinator.apply_activity(2, activity)
    harness.coordinator.apply_stream_state(2, "connected")
    harness.coordinator.apply_activity(3, {**activity, "thread_id": "thread-b"})
    harness.context[0] = HistoryStreamContext(3, "thread-a", closing=True)
    harness.coordinator.apply_activity(3, activity)
    harness.coordinator.apply_stream_state(3, "connected")

    assert harness.activity_pane.activities == []
    assert harness.thread_active == []
    assert harness.refreshes == []
    assert harness.stream_label.text == ""


def test_selection_reset_clears_stream_state_and_discards_stale_reconnect(
    harness: _Harness,
) -> None:
    harness.coordinator.reset_selection("thread-a")
    harness.coordinator.apply_stream_state(3, "connected")
    harness.coordinator.schedule_reconnect(3)
    stale_callback = harness.timers[0][1]

    harness.context[0] = HistoryStreamContext(4, "thread-b", closing=False)
    harness.coordinator.reset_selection("thread-b")
    stale_callback()

    assert harness.coordinator._stream_connected is False
    assert harness.coordinator._stream_ever_connected is False
    assert harness.coordinator._stream_sync_pending is True
    assert harness.coordinator._reconnect_scheduled is False
    assert harness.status_requests == []
    assert harness.stream_label.text == "Stream: reconnecting"


def test_item_events_schedule_history_refresh_without_appending_activity(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")

    for activity_type in ("item_started", "item_completed"):
        harness.coordinator.apply_activity(
            3,
            {"thread_id": "thread-a", "type": activity_type, "item_id": activity_type},
        )

    assert len(harness.refreshes) == 2
    assert harness.activity_pane.activities == []


def test_turn_approval_and_usage_events_keep_existing_side_effects(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")
    events = (
        {"thread_id": "thread-a", "type": "turn_started", "status": "in_progress"},
        {
            "thread_id": "thread-a",
            "type": "turn_completed",
            "status": "completed",
            "turn_id": "turn-a",
        },
        {"thread_id": "thread-a", "type": "turn_failed", "status": "failed"},
        {"thread_id": "thread-a", "type": "approval_requested", "status": "requested"},
        {"thread_id": "thread-a", "type": "approval_resolved", "status": "resolved"},
    )

    for event in events:
        harness.coordinator.apply_activity(3, event)

    assert harness.thread_active == [
        ("thread-a", True),
        ("thread-a", False),
        ("thread-a", False),
        ("thread-a", True),
    ]
    assert harness.usage_snapshots == [("thread-a", "turn-a")]
    assert harness.status_requests == [None, None]
    assert harness.activity_pane.resolved == 1
    assert harness.activity_pane.activities == list(events)
    assert len(harness.refreshes) == 3


def test_stream_reconnect_reconciles_history_once_per_reconnection(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")

    harness.coordinator.apply_stream_state(3, "connected")
    harness.coordinator.apply_stream_state(3, "connected")
    assert harness.refreshes == []

    harness.coordinator.apply_stream_state(3, "reconnecting")
    harness.coordinator.apply_stream_state(3, "connected")
    harness.coordinator.apply_stream_state(3, "connected")
    assert len(harness.refreshes) == 1

    harness.coordinator.apply_stream_state(3, "disconnected")
    harness.coordinator.apply_stream_state(3, "disconnected")
    assert len(harness.timers) == 1
    harness.coordinator.apply_stream_state(3, "connected")
    assert len(harness.refreshes) == 2
    assert harness.stream_label.text == "Stream: connected"


def test_reconnect_is_deduplicated_delayed_and_rejected_when_stale(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")

    harness.coordinator.schedule_reconnect(3)
    harness.coordinator.schedule_reconnect(3)

    assert len(harness.timers) == 1
    assert harness.timers[0][0] == 1_500
    stale_callback = harness.timers[0][1]
    harness.context[0] = HistoryStreamContext(4, "thread-b", closing=False)
    harness.coordinator.reset_selection("thread-b")
    stale_callback()
    assert harness.status_requests == []

    harness.coordinator.schedule_reconnect(4)
    harness.timers[1][1]()
    assert harness.status_requests == [None]
    assert harness.coordinator._stream_sync_pending is True


def test_status_failure_without_pending_stream_does_not_schedule_retry(harness: _Harness) -> None:
    harness.coordinator.status_request_failed("late failure")
    assert harness.activity_pane.errors == ["late failure"]
    assert harness.timers == []


def test_invalid_status_payload_does_not_start_stream(harness: _Harness) -> None:
    harness.coordinator.reset_selection("thread-a")

    harness.coordinator.apply_status(["not", "a", "mapping"])

    assert harness.activity_pane.snapshots == []
    assert harness.streams == []
