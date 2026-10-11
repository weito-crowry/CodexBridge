from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import TYPE_CHECKING

from .usage import CodexUsage, CodexUsageSnapshot

if TYPE_CHECKING:
    from .main_window import MainWindow


_USAGE_SNAPSHOT_DELAY_MS = 1_000
_USAGE_SNAPSHOT_DEFER_MS = 500
_USAGE_SNAPSHOT_MAX_ENTRIES = 500


class UsageSnapshotStore:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self.usage_snapshots: OrderedDict[tuple[str, str], CodexUsageSnapshot | None] = (
            OrderedDict()
        )
        self.pending_turns: set[tuple[str, str]] = set()
        self.request_turns: set[tuple[str, str]] = set()

    def start_timer(self) -> None:
        self._window.turn_usage_snapshot_timer.start(_USAGE_SNAPSHOT_DELAY_MS)

    def schedule_turn(self, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        if key in self.usage_snapshots:
            return
        self.usage_snapshots[key] = None
        self.pending_turns.add(key)
        while len(self.usage_snapshots) > _USAGE_SNAPSHOT_MAX_ENTRIES:
            oldest, _snapshot = self.usage_snapshots.popitem(last=False)
            self.pending_turns.discard(oldest)
            self.request_turns.discard(oldest)
        if self.pending_turns and not self._window.turn_usage_snapshot_timer.isActive():
            self.start_timer()

    def on_timeout(self) -> None:
        window = self._window
        if window._closing or not self.pending_turns:
            return
        if (
            window._usage_request_in_flight
            or window.usage_initial_timer.isActive()
            or window.usage_retry_timer.isActive()
        ):
            window.turn_usage_snapshot_timer.start(_USAGE_SNAPSHOT_DEFER_MS)
            return
        if not window._usage_ready:
            return
        candidates = set(self.pending_turns)
        if not window._start_usage_request("turn_snapshot"):
            return
        self.request_turns = candidates

    def finish(self, candidates: set[tuple[str, str]], usage: CodexUsage) -> None:
        has_usage = usage.five_hour is not None or usage.weekly is not None
        snapshot = CodexUsageSnapshot(usage, datetime.now().astimezone()) if has_usage else None
        for key in candidates:
            if key in self.usage_snapshots and snapshot is not None:
                self.usage_snapshots[key] = snapshot
            self.pending_turns.discard(key)

        selected_thread_id = self._window._selected_thread_id
        snapshot_turn_ids = {
            turn_id
            for thread_id, turn_id in candidates
            if thread_id == selected_thread_id and snapshot is not None
        }
        if snapshot_turn_ids and any(
            entry.turn_id in snapshot_turn_ids for entry in self._window._timeline_entries
        ):
            self._window._render_timeline()
