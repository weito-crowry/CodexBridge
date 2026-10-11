from __future__ import annotations

from collections import OrderedDict

import pytest

from codex_bridge.console.usage import CodexUsage, UsageWindow
from codex_bridge.console.usage_snapshots import UsageSnapshotStore
from codex_bridge.console.widgets import TimelineEntry


class FakeTimer:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.operations: list[tuple[str, int | None]] = []

    def start(self, interval: int | None = None) -> None:
        self.active = True
        self.operations.append(("start", interval))

    def isActive(self) -> bool:
        return self.active


class FakeWindow:
    def __init__(self) -> None:
        self._closing = False
        self._usage_ready = True
        self._usage_request_in_flight = False
        self.usage_initial_timer = FakeTimer()
        self.usage_retry_timer = FakeTimer()
        self.turn_usage_snapshot_timer = FakeTimer()
        self._selected_thread_id = "thread-a"
        self._timeline_entries: list[TimelineEntry] = []
        self._usage = CodexUsage()
        self.history_writes = 0
        self.render_count = 0
        self.accept_usage_request = True
        self.usage_requests: list[str] = []

    def _start_usage_request(self, mode: str) -> bool:
        self.usage_requests.append(mode)
        return self.accept_usage_request

    def _render_timeline(self) -> None:
        self.render_count += 1


def make_store() -> tuple[FakeWindow, UsageSnapshotStore]:
    window = FakeWindow()
    return window, UsageSnapshotStore(window)  # type: ignore[arg-type]


def test_schedule_suppresses_duplicates_and_evicts_oldest_pending_request() -> None:
    window, store = make_store()
    oldest = ("thread-a", "turn-0")
    store.pending_turns.add(oldest)
    store.request_turns.add(oldest)

    store.schedule_turn(*oldest)
    store.schedule_turn(*oldest)
    for index in range(1, 501):
        store.schedule_turn("thread-a", f"turn-{index}")

    assert len(store.usage_snapshots) == 500
    assert oldest not in store.usage_snapshots
    assert oldest not in store.pending_turns
    assert oldest not in store.request_turns
    assert window.turn_usage_snapshot_timer.operations == [("start", 1_000)]


@pytest.mark.parametrize("blocked_by", ["closing", "empty"])
def test_timeout_is_noop_when_closing_or_no_turn_is_pending(blocked_by: str) -> None:
    window, store = make_store()
    if blocked_by == "closing":
        window._closing = True
        store.pending_turns.add(("thread-a", "turn-a"))

    store.on_timeout()

    assert not window.usage_requests
    assert not window.turn_usage_snapshot_timer.operations


@pytest.mark.parametrize("blocked_by", ["request", "initial", "retry"])
def test_timeout_defers_for_another_usage_request_or_timer(blocked_by: str) -> None:
    window, store = make_store()
    store.pending_turns.add(("thread-a", "turn-a"))
    if blocked_by == "request":
        window._usage_request_in_flight = True
    elif blocked_by == "initial":
        window.usage_initial_timer.active = True
    else:
        window.usage_retry_timer.active = True

    store.on_timeout()

    assert window.turn_usage_snapshot_timer.operations == [("start", 500)]
    assert not window.usage_requests


def test_timeout_keeps_pending_turn_when_usage_is_not_ready() -> None:
    window, store = make_store()
    window._usage_ready = False
    candidate = ("thread-a", "turn-a")
    store.pending_turns.add(candidate)

    store.on_timeout()

    assert store.pending_turns == {candidate}
    assert not store.request_turns
    assert not window.usage_requests
    assert not window.turn_usage_snapshot_timer.operations


def test_timeout_passes_pending_copy_and_sets_request_turns_only_on_acceptance() -> None:
    window, store = make_store()
    candidate = ("thread-a", "turn-a")
    store.pending_turns.add(candidate)

    store.on_timeout()

    assert window.usage_requests == ["turn_snapshot"]
    assert store.request_turns == {candidate}
    assert store.request_turns is not store.pending_turns

    rejected_window, rejected_store = make_store()
    rejected_window.accept_usage_request = False
    rejected_store.pending_turns.add(candidate)

    rejected_store.on_timeout()

    assert rejected_store.pending_turns == {candidate}
    assert not rejected_store.request_turns


def test_finish_stores_valid_usage_only_for_candidates_and_renders_selected_thread_once() -> None:
    window, store = make_store()
    selected = ("thread-a", "turn-selected")
    another_thread = ("thread-b", "turn-other")
    no_entry = ("thread-a", "turn-no-entry")
    absent = ("thread-a", "turn-absent")
    store.usage_snapshots = OrderedDict.fromkeys((selected, another_thread, no_entry), None)
    candidates = {selected, another_thread, no_entry, absent}
    store.pending_turns.update(candidates)
    window._timeline_entries = [
        TimelineEntry("turn-selected", "item-a", "Agent", "Agent", "a", None, ()),
        TimelineEntry("turn-selected", "item-b", "Agent", "Agent", "b", None, ()),
    ]
    usage = CodexUsage(
        five_hour=UsageWindow(300, 72, "reset"),
        weekly=UsageWindow(10_080, 18, "reset"),
    )

    store.finish(candidates, usage)

    selected_snapshot = store.usage_snapshots[selected]
    assert selected_snapshot is not None
    assert selected_snapshot.usage is usage
    assert selected_snapshot.captured_at.utcoffset() is not None
    assert store.usage_snapshots[another_thread] is not None
    assert store.usage_snapshots[no_entry] is not None
    assert absent not in store.usage_snapshots
    assert not store.pending_turns
    assert window.render_count == 1
    assert window._usage == CodexUsage()
    assert window.history_writes == 0


def test_finish_without_usage_clears_pending_without_snapshot_or_render() -> None:
    window, store = make_store()
    candidate = ("thread-a", "turn-a")
    store.usage_snapshots[candidate] = None
    store.pending_turns.add(candidate)

    store.finish({candidate}, CodexUsage())

    assert store.usage_snapshots[candidate] is None
    assert not store.pending_turns
    assert window.render_count == 0


def test_finish_does_not_render_after_selected_thread_changes() -> None:
    window, store = make_store()
    candidate = ("thread-a", "turn-a")
    store.usage_snapshots[candidate] = None
    store.pending_turns.add(candidate)
    window._selected_thread_id = "thread-b"
    window._timeline_entries = [TimelineEntry("turn-a", "item-a", "Agent", "Agent", "a", None, ())]

    store.finish({candidate}, CodexUsage(five_hour=UsageWindow(300, 72, "reset")))

    assert window.render_count == 0
