from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .api_client import ApiClient
from .config import ConsoleConfig
from .widgets import (
    ActivityPane,
    HistoryPane,
    ThreadListPane,
    TimelineEntry,
    timeline_entries,
)

_RECONNECT_MS = 1_500


class MainWindow(QMainWindow):
    """Read-only desktop view over the existing localhost UI API."""

    def __init__(
        self,
        config: ConsoleConfig,
        parent: QWidget | None = None,
        *,
        api_client: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._client = api_client or ApiClient(config.base_url, self)
        self._closing = False
        self._selection_generation = 0
        self._selected_thread_id: str | None = None
        self._timeline_entries: list[TimelineEntry] = []
        self._turn_statuses: dict[str, str] = {}
        self._next_cursor: str | None = None
        self._stream_sync_pending = False
        self._reconnect_scheduled = False

        self.setWindowTitle("CodexBridge Console")
        self.resize(1_400, 850)
        self._build_ui()
        self._connect_client()
        self._build_timers()
        self._set_unavailable_state()
        self.refresh()

    def _build_ui(self) -> None:
        self.bridge_status_label = QLabel("Bridge: disconnected")
        self.app_server_status_label = QLabel("App Server: failed")
        self.stream_status_label = QLabel("Stream: disconnected")
        for label in (
            self.bridge_status_label,
            self.app_server_status_label,
            self.stream_status_label,
        ):
            label.setObjectName("topStatus")

        status_bar = QHBoxLayout()
        status_bar.addWidget(QLabel("CodexBridge Console"))
        status_bar.addStretch(1)
        status_bar.addWidget(self.bridge_status_label)
        status_bar.addWidget(self.app_server_status_label)
        status_bar.addWidget(self.stream_status_label)

        self.thread_pane = ThreadListPane()
        self.history_pane = HistoryPane()
        self.activity_pane = ActivityPane()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.thread_pane)
        self.splitter.addWidget(self.history_pane)
        self.splitter.addWidget(self.activity_pane)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([300, 760, 340])

        self.bottom_status_label = QLabel("Starting…")
        self.bottom_status_label.setObjectName("bottomStatus")
        self.bottom_status_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addLayout(status_bar)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.bottom_status_label)
        self.setCentralWidget(root)
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.setStyle("Fusion")
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #202124; color: #e8eaed; }
            QLabel { color: #d8dbe0; }
            QLabel#topStatus { padding: 3px 8px; border: 1px solid #3c4043; border-radius: 3px; }
            QLabel#bottomStatus { color: #aeb4bd; padding: 4px 6px; border-top: 1px solid #3c4043; }
            QLineEdit, QListWidget, QTextEdit {
                background: #292a2d; color: #f1f3f4; border: 1px solid #4a4d50;
            }
            QPushButton {
                background: #303134; color: #f1f3f4; border: 1px solid #5f6368;
                padding: 4px 10px;
            }
            QPushButton:hover { background: #3c4043; }
            QFrame#historyCard {
                background: #292a2d; border: 1px solid #42464a; border-radius: 4px;
            }
            QLabel#turnSeparator { color: #7f8791; padding: 4px; }
            QSplitter::handle { background: #3c4043; }
            """
        )

    def _connect_client(self) -> None:
        self._client.json_succeeded.connect(self.apply_json_result)
        self._client.json_failed.connect(self._apply_json_error)
        self._client.activity_received.connect(self._apply_activity)
        self._client.stream_state_changed.connect(self._apply_stream_state)
        self.thread_pane.refresh_requested.connect(self.refresh)
        self.thread_pane.thread_selected.connect(self.select_thread)
        self.history_pane.older_requested.connect(self.load_older)

    def _build_timers(self) -> None:
        self.health_timer = QTimer(self)
        self.health_timer.setInterval(5_000)
        self.health_timer.timeout.connect(self._request_health)
        self.thread_timer = QTimer(self)
        self.thread_timer.setInterval(10_000)
        self.thread_timer.timeout.connect(self._request_threads)
        self.selected_status_timer = QTimer(self)
        self.selected_status_timer.setInterval(5_000)
        self.selected_status_timer.timeout.connect(self._request_selected_status)
        self.health_timer.start()
        self.thread_timer.start()

    def _set_unavailable_state(self) -> None:
        location = f"{self._config.host}:{self._config.port}"
        self.history_pane.set_empty_state(
            f"CodexBridge is not available on {location}\nStart codex-bridge and retry."
        )
        self.activity_pane.set_empty_state("Bridge unavailable")

    def refresh(self) -> None:
        self._request_health()
        self._request_threads()
        if self._selected_thread_id is not None:
            self._request_snapshot(self._selection_generation)

    def _request_health(self) -> None:
        self._client.get_json("/healthz", key="health")
        self._request_bridge_status()

    def _request_bridge_status(self) -> None:
        self._client.get_json("/ui-api/status", key="bridge-status")

    def _request_threads(self) -> None:
        self._client.get_json("/ui-api/threads", key="threads", query={"limit": 100})

    def _thread_path(self, suffix: str = "") -> str:
        assert self._selected_thread_id is not None
        return f"/ui-api/threads/{quote(self._selected_thread_id, safe='')}{suffix}"

    def _request_snapshot(self, generation: int) -> None:
        if generation != self._selection_generation or self._selected_thread_id is None:
            return
        prefix = f"selection:{generation}:"
        self._client.get_json(self._thread_path(), key=prefix + "detail")
        self._client.get_json(
            self._thread_path("/turns"),
            key=prefix + "turns",
            query={"limit": 20, "sort_direction": "desc"},
        )
        self._client.get_json(
            self._thread_path("/items"),
            key=prefix + "items",
            query={"limit": 100, "sort_direction": "desc"},
        )
        self._client.get_json(
            self._thread_path("/status"),
            key=prefix + "status",
            query={"activity_limit": 50},
        )

    def _request_selected_status(self) -> None:
        if self._selected_thread_id is None:
            return
        key = f"selection:{self._selection_generation}:status"
        self._client.get_json(self._thread_path("/status"), key=key, query={"activity_limit": 50})

    def select_thread(self, thread_id: str | None) -> None:
        self._selection_generation += 1
        generation = self._selection_generation
        self._selected_thread_id = thread_id
        self._timeline_entries = []
        self._turn_statuses = {}
        self._next_cursor = None
        self._stream_sync_pending = thread_id is not None
        self._reconnect_scheduled = False
        self._client.abort_json_group("selection:")
        self._client.stop_stream()
        self.selected_status_timer.stop()
        if thread_id is None:
            self._set_unavailable_state()
            return
        self.history_pane.set_empty_state("Loading history…")
        self.activity_pane.set_empty_state("Loading activity…")
        self.selected_status_timer.start()
        self._request_snapshot(generation)

    @staticmethod
    def _selection_key(key: str) -> tuple[int, str] | None:
        parts = key.split(":", 2)
        if len(parts) != 3 or parts[0] != "selection":
            return None
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return None

    def _is_current_selection(self, key: str) -> tuple[int, str] | None:
        parsed = self._selection_key(key)
        if parsed is None or parsed[0] != self._selection_generation:
            return None
        return parsed

    def apply_json_result(self, key: str, payload: object) -> None:
        if key == "health":
            if isinstance(payload, Mapping) and payload.get("status") == "ok":
                self.bottom_status_label.setText("Bridge reachable")
            return
        if key == "bridge-status":
            if isinstance(payload, Mapping):
                bridge = payload.get("bridge")
                app_server = payload.get("app_server")
                self.bridge_status_label.setText(
                    "Bridge: connected"
                    if bridge in {"ready", "connected"}
                    else "Bridge: disconnected"
                )
                self.app_server_status_label.setText(
                    "App Server: ready"
                    if app_server in {"ready", "connected"}
                    else "App Server: failed"
                )
            return
        if key == "threads":
            if isinstance(payload, Mapping) and isinstance(payload.get("threads"), list):
                threads = [thread for thread in payload["threads"] if isinstance(thread, Mapping)]
                self.thread_pane.set_threads(threads)
                if not threads:
                    self.thread_pane.set_empty_state("No threads found.")
            return

        selection = self._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        if suffix == "detail":
            return
        if suffix == "turns":
            if isinstance(payload, Mapping) and isinstance(payload.get("turns"), list):
                self._turn_statuses = {
                    turn["id"]: turn["status"]
                    for turn in payload["turns"]
                    if isinstance(turn, Mapping)
                    and isinstance(turn.get("id"), str)
                    and isinstance(turn.get("status"), str)
                }
                self._render_timeline()
            return
        if suffix == "items":
            self._apply_items(payload, prepend=False)
            return
        if suffix == "status":
            self._apply_status(payload)
            return
        if suffix.startswith("older:"):
            self._apply_items(payload, prepend=True)

    def _apply_items(self, payload: object, *, prepend: bool) -> None:
        if not isinstance(payload, Mapping):
            return
        new_entries = timeline_entries(payload)
        if prepend:
            existing = {(entry.turn_id, entry.item_id) for entry in self._timeline_entries}
            new_entries = tuple(
                entry for entry in new_entries if (entry.turn_id, entry.item_id) not in existing
            )
            self._timeline_entries = list(new_entries) + self._timeline_entries
        else:
            self._timeline_entries = list(new_entries)
        next_cursor = payload.get("next_cursor")
        self._next_cursor = next_cursor if isinstance(next_cursor, str) else None
        self._render_timeline()

    def _render_timeline(self) -> None:
        self.history_pane.set_timeline(
            self._timeline_entries,
            has_older=self._next_cursor is not None,
            turn_statuses=self._turn_statuses,
        )

    def _apply_status(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        self.activity_pane.set_snapshot(payload)
        self.bottom_status_label.setText("Thread snapshot updated")
        if self._stream_sync_pending and self._selected_thread_id is not None:
            self._stream_sync_pending = False
            self._client.start_stream(self._selected_thread_id, self._selection_generation)

    def load_older(self) -> None:
        if self._selected_thread_id is None or self._next_cursor is None:
            return
        generation = self._selection_generation
        cursor = self._next_cursor
        key = f"selection:{generation}:older:{cursor}"
        self._client.get_json(
            self._thread_path("/items"),
            key=key,
            query={"limit": 100, "sort_direction": "desc", "cursor": cursor},
        )

    def _apply_json_error(self, key: str, message: str) -> None:
        if key in {"health", "bridge-status"}:
            self.bridge_status_label.setText("Bridge: disconnected")
            self.app_server_status_label.setText("App Server: failed")
            self.bottom_status_label.setText(message)
            if self._selected_thread_id is None:
                self._set_unavailable_state()
            return
        selection = self._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        self.bottom_status_label.setText(message)
        if suffix in {"items", "turns"} or suffix.startswith("older:"):
            self.history_pane.set_error(message)
        if suffix == "status":
            self.activity_pane.set_empty_state(message)
            if self._stream_sync_pending:
                self._schedule_reconnect(self._selection_generation)

    def _apply_activity(self, generation: int, payload: object) -> None:
        if (
            self._closing
            or generation != self._selection_generation
            or self._selected_thread_id is None
            or not isinstance(payload, Mapping)
            or payload.get("thread_id") != self._selected_thread_id
        ):
            return
        self.activity_pane.append_activity(payload)

    def _apply_stream_state(self, generation: int, state: str) -> None:
        if self._closing or generation != self._selection_generation:
            return
        self.stream_status_label.setText(f"Stream: {state}")
        if state == "disconnected" and self._selected_thread_id is not None:
            self._schedule_reconnect(generation)

    def _schedule_reconnect(self, generation: int) -> None:
        if self._reconnect_scheduled or self._closing:
            return
        self._reconnect_scheduled = True
        self.stream_status_label.setText("Stream: reconnecting")
        QTimer.singleShot(_RECONNECT_MS, lambda: self._reconnect(generation))

    def _reconnect(self, generation: int) -> None:
        self._reconnect_scheduled = False
        if (
            self._closing
            or generation != self._selection_generation
            or self._selected_thread_id is None
        ):
            return
        self._stream_sync_pending = True
        self._request_selected_status()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self.health_timer.stop()
        self.thread_timer.stop()
        self.selected_status_timer.stop()
        self._client.abort_all()
        event.accept()
