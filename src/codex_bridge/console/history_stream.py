from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel

from .widgets import ActivityPane

_RECONNECT_MS = 1_500
_HISTORY_REFRESH_ACTIVITY_TYPES = frozenset(
    {
        "turn_started",
        "turn_completed",
        "turn_failed",
        "turn_interrupted",
        "command_started",
        "command_completed",
        "file_change_started",
        "file_change_completed",
        "agent_commentary",
        "agent_message",
    }
)
TERMINAL_TURN_STATES = frozenset({"completed", "interrupted", "failed", "error"})


@dataclass(frozen=True)
class HistoryStreamContext:
    generation: int
    thread_id: str | None
    closing: bool


class HistoryStreamCoordinator:
    def __init__(
        self,
        activity_pane: ActivityPane,
        stream_status_label: QLabel,
        bottom_status_label: QLabel,
        *,
        context_provider: Callable[[], HistoryStreamContext],
        request_selected_status: Callable[[], None],
        schedule_selected_history_refresh: Callable[[], None],
        set_thread_active: Callable[[str, bool], None],
        update_thread_activity: Callable[[str, str], None],
        schedule_turn_usage_snapshot: Callable[[str, str], None],
        sync_approval_controls: Callable[[], None],
        start_stream: Callable[[str, int], None],
    ) -> None:
        self._activity_pane = activity_pane
        self._stream_status_label = stream_status_label
        self._bottom_status_label = bottom_status_label
        self._context_provider = context_provider
        self._request_selected_status = request_selected_status
        self._schedule_selected_history_refresh = schedule_selected_history_refresh
        self._set_thread_active = set_thread_active
        self._update_thread_activity = update_thread_activity
        self._schedule_turn_usage_snapshot = schedule_turn_usage_snapshot
        self._sync_approval_controls = sync_approval_controls
        self._start_stream = start_stream
        self._stream_sync_pending = False
        self._reconnect_scheduled = False
        self._stream_connected = False
        self._stream_ever_connected = False

    def reset_selection(self, thread_id: str | None) -> None:
        self._stream_connected = False
        self._stream_ever_connected = False
        self._stream_sync_pending = thread_id is not None
        self._reconnect_scheduled = False

    def apply_status(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        context = self._context_provider()
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            thread_id = context.thread_id
        state = payload.get("state")
        if isinstance(thread_id, str) and isinstance(state, str):
            self._update_thread_activity(thread_id, state)
        self._activity_pane.set_snapshot(payload)
        self._sync_approval_controls()
        self._bottom_status_label.setText("Thread snapshot updated")
        if self._stream_sync_pending and context.thread_id is not None:
            self._stream_sync_pending = False
            self._start_stream(context.thread_id, context.generation)

    def apply_activity(self, generation: int, payload: object) -> None:
        context = self._context_provider()
        if (
            context.closing
            or generation != context.generation
            or context.thread_id is None
            or not isinstance(payload, Mapping)
            or payload.get("thread_id") != context.thread_id
        ):
            return
        thread_id = payload.get("thread_id")
        activity_type = payload.get("type")
        activity_status = payload.get("status")
        turn_id = payload.get("turn_id")
        if isinstance(activity_type, str) and activity_type in {"item_started", "item_completed"}:
            self._schedule_selected_history_refresh()
            return
        if isinstance(thread_id, str) and isinstance(activity_type, str):
            if (
                activity_type == "turn_started"
                or activity_status == "in_progress"
                or activity_type in {"approval_requested", "user_input_requested"}
                and activity_status == "requested"
            ):
                self._set_thread_active(thread_id, True)
            elif (
                activity_type in {"turn_completed", "turn_failed", "turn_interrupted", "error"}
                and activity_status in TERMINAL_TURN_STATES
            ):
                self._set_thread_active(thread_id, False)
            if (
                activity_type == "turn_completed"
                and activity_status == "completed"
                and isinstance(turn_id, str)
            ):
                self._schedule_turn_usage_snapshot(thread_id, turn_id)
            if activity_type == "approval_requested" and activity_status == "requested":
                self._request_selected_status()
            elif activity_type == "approval_resolved" and activity_status == "resolved":
                self._activity_pane.mark_approval_resolved()
                self._request_selected_status()
        self._activity_pane.append_activity(payload)
        if isinstance(activity_type, str) and activity_type in _HISTORY_REFRESH_ACTIVITY_TYPES:
            self._schedule_selected_history_refresh()

    def status_request_failed(self, message: str) -> None:
        self._activity_pane.set_error(message)
        if self._stream_sync_pending:
            self.schedule_reconnect(self._context_provider().generation)

    def apply_stream_state(self, generation: int, state: str) -> None:
        context = self._context_provider()
        if context.closing or generation != context.generation:
            return
        if state == "disconnected" and context.thread_id is None:
            self._stream_status_label.setText("Stream: idle")
        else:
            self._stream_status_label.setText(f"Stream: {state}")
        if state == "connected":
            is_reconnected = self._stream_ever_connected and not self._stream_connected
            self._stream_connected = True
            self._stream_ever_connected = True
            if is_reconnected:
                self._schedule_selected_history_refresh()
        elif state in {"disconnected", "reconnecting"}:
            self._stream_connected = False
        if state == "disconnected" and context.thread_id is not None:
            self.schedule_reconnect(generation)

    def schedule_reconnect(self, generation: int) -> None:
        if self._reconnect_scheduled or self._context_provider().closing:
            return
        self._reconnect_scheduled = True
        self._stream_status_label.setText("Stream: reconnecting")
        QTimer.singleShot(_RECONNECT_MS, lambda: self.reconnect(generation))

    def reconnect(self, generation: int) -> None:
        self._reconnect_scheduled = False
        context = self._context_provider()
        if context.closing or generation != context.generation or context.thread_id is None:
            return
        self._stream_sync_pending = True
        self._request_selected_status()
