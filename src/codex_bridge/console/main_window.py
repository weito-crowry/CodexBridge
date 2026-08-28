from __future__ import annotations

from collections.abc import Mapping
from time import monotonic
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .api_client import ApiClient
from .codex_resolver import (
    CodexResolution,
    CodexResolutionError,
    CodexVersionProbe,
    enumerate_candidates,
)
from .config import ConsoleConfig
from .runtime_launcher import BridgeRuntimeLauncher
from .widgets import (
    ActivityPane,
    HistoryPane,
    ThreadListPane,
    TimelineEntry,
    timeline_entries,
)

_RECONNECT_MS = 1_500
_READINESS_INTERVAL_MS = 350
_READINESS_TIMEOUT_SECONDS = 10.0
_RUNTIME_LABELS = {
    "unavailable": "Runtime: unavailable",
    "external": "Runtime: external",
    "launching": "Runtime: launching",
    "launch_failed": "Runtime: launch failed",
    "launch_timed_out": "Runtime: launch timed out",
}


class MainWindow(QMainWindow):
    """Read-only desktop view over the existing localhost UI API."""

    def __init__(
        self,
        config: ConsoleConfig,
        parent: QWidget | None = None,
        *,
        api_client: Any | None = None,
        codex_probe: Any | None = None,
        runtime_launcher: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._client = api_client or ApiClient(config.base_url, self)
        self._codex_probe = codex_probe if codex_probe is not None else self._new_codex_probe()
        self._launcher = (
            runtime_launcher if runtime_launcher is not None else BridgeRuntimeLauncher()
        )
        self._closing = False
        self._selection_generation = 0
        self._selected_thread_id: str | None = None
        self._timeline_entries: list[TimelineEntry] = []
        self._turn_statuses: dict[str, str] = {}
        self._next_cursor: str | None = None
        self._stream_sync_pending = False
        self._reconnect_scheduled = False
        self._runtime_state = "unavailable"
        self._codex_resolution: CodexResolution | None = None
        self._bridge_seen_ready = False
        self._health_observed = False
        self._status_observed = False
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._launch_in_progress = False
        self._detached_launch_started = False
        self._detached_pid: int | None = None
        self._readiness_deadline = 0.0
        self._readiness_health_ok = False
        self._readiness_bridge_ready = False
        self._readiness_app_server_ready = False

        self.setWindowTitle("CodexBridge Console")
        self.resize(1_400, 850)
        self._build_ui()
        self._connect_client()
        self._build_timers()
        self._connect_runtime()
        self._set_unavailable_state()
        self._codex_probe.start()
        self.refresh()

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    def _new_codex_probe(self) -> CodexVersionProbe:
        try:
            candidates = enumerate_candidates()
        except CodexResolutionError:
            candidates = ()
        return CodexVersionProbe(candidates, parent=self)

    def _build_ui(self) -> None:
        self.bridge_status_label = QLabel("Bridge: disconnected")
        self.app_server_status_label = QLabel("App Server: failed")
        self.stream_status_label = QLabel("Stream: disconnected")
        self.codex_status_label = QLabel("Codex: checking")
        self.runtime_status_label = QLabel("Runtime: unavailable")
        self.start_bridge_button = QPushButton("Start Bridge")
        self.start_bridge_button.setEnabled(False)
        for label in (
            self.bridge_status_label,
            self.app_server_status_label,
            self.stream_status_label,
            self.codex_status_label,
            self.runtime_status_label,
        ):
            label.setObjectName("topStatus")

        status_bar = QHBoxLayout()
        status_bar.addWidget(QLabel("CodexBridge Console"))
        status_bar.addStretch(1)
        status_bar.addWidget(self.bridge_status_label)
        status_bar.addWidget(self.app_server_status_label)
        status_bar.addWidget(self.stream_status_label)
        status_bar.addWidget(self.codex_status_label)
        status_bar.addWidget(self.runtime_status_label)
        status_bar.addWidget(self.start_bridge_button)

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

    def _connect_runtime(self) -> None:
        self._codex_probe.resolved.connect(self._apply_codex_resolution)
        self._codex_probe.failed.connect(self._apply_codex_probe_error)
        self.start_bridge_button.clicked.connect(self._start_bridge)

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
        self.readiness_timer = QTimer(self)
        self.readiness_timer.setInterval(_READINESS_INTERVAL_MS)
        self.readiness_timer.timeout.connect(self._on_readiness_tick)
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

    def _apply_codex_resolution(self, resolution: CodexResolution) -> None:
        if self._closing:
            return
        self._codex_resolution = resolution
        self.codex_status_label.setText(f"Codex: {resolution.version} · {resolution.source}")
        self._update_start_button()

    def _apply_codex_probe_error(self, _message: str) -> None:
        if self._closing:
            return
        self._codex_resolution = None
        self.codex_status_label.setText("Codex: not found")
        self._update_start_button()

    def _set_runtime_state(self, state: str, *, label: str | None = None) -> None:
        self._runtime_state = state
        self.runtime_status_label.setText(label or _RUNTIME_LABELS.get(state, f"Runtime: {state}"))
        self._update_start_button()

    def _update_start_button(self) -> None:
        enabled = (
            not self._closing
            and self._codex_resolution is not None
            and self._health_observed
            and self._status_observed
            and not self._health_ok
            and not self._bridge_ready
            and not self._app_server_ready
            and not self._bridge_seen_ready
            and not self._launch_in_progress
            and not self._detached_launch_started
        )
        self.start_bridge_button.setEnabled(enabled)

    def _start_bridge(self) -> None:
        resolution = self._codex_resolution
        if (
            resolution is None
            or self._bridge_ready
            or self._bridge_seen_ready
            or self._launch_in_progress
            or self._detached_launch_started
            or self._closing
        ):
            return
        self._launch_in_progress = True
        self._set_runtime_state("launching")
        try:
            result = self._launcher.launch(
                codex_executable=resolution.path,
                ui_port=self._config.port,
            )
        except Exception:
            self._launch_in_progress = False
            self._set_runtime_state("launch_failed")
            self.bottom_status_label.setText("Bridge launch failed")
            return
        if not result.started:
            self._launch_in_progress = False
            self._set_runtime_state("launch_failed")
            self.bottom_status_label.setText("Bridge launch failed")
            return
        self._detached_launch_started = True
        self._detached_pid = result.pid
        self._readiness_deadline = monotonic() + _READINESS_TIMEOUT_SECONDS
        self._readiness_health_ok = False
        self._readiness_bridge_ready = False
        self._readiness_app_server_ready = False
        self.readiness_timer.start()
        self._request_launch_readiness()

    def _request_launch_readiness(self) -> None:
        if self._closing or not self._launch_in_progress:
            return
        self._client.get_json("/healthz", key="launch:health")
        self._client.get_json("/ui-api/status", key="launch:status")

    def _on_readiness_tick(self) -> None:
        if self._closing or not self._launch_in_progress:
            self.readiness_timer.stop()
            return
        if monotonic() >= self._readiness_deadline:
            self.readiness_timer.stop()
            self._launch_in_progress = False
            self._set_runtime_state("launch_timed_out")
            self.bottom_status_label.setText("Bridge launch timed out; it may still be starting")
            return
        self._request_launch_readiness()

    def _apply_launch_health(self, payload: object) -> None:
        self._readiness_health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
        self._finish_launch_if_ready()

    def _apply_launch_status(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        bridge = payload.get("bridge")
        app_server = payload.get("app_server")
        self._readiness_bridge_ready = bridge in {"ready", "connected"}
        self._readiness_app_server_ready = app_server in {"ready", "connected"}
        self.bridge_status_label.setText(
            "Bridge: connected" if self._readiness_bridge_ready else "Bridge: disconnected"
        )
        self.app_server_status_label.setText(
            "App Server: ready" if self._readiness_app_server_ready else "App Server: failed"
        )
        self._finish_launch_if_ready()

    def _finish_launch_if_ready(self) -> None:
        if not (
            self._launch_in_progress
            and self._readiness_health_ok
            and self._readiness_bridge_ready
            and self._readiness_app_server_ready
        ):
            return
        self.readiness_timer.stop()
        self._launch_in_progress = False
        self._health_ok = True
        self._bridge_ready = True
        self._app_server_ready = True
        self._bridge_seen_ready = True
        self._set_runtime_state("console_started", label="Runtime: started by Console")
        self.bottom_status_label.setText("Bridge started by Console")

    def _apply_runtime_observation(self) -> None:
        ready = self._health_ok and self._bridge_ready and self._app_server_ready
        if ready:
            self._bridge_seen_ready = True
            if self._detached_launch_started:
                self._launch_in_progress = False
                self.readiness_timer.stop()
                self._set_runtime_state("console_started", label="Runtime: started by Console")
            else:
                self._set_runtime_state("external")
            return
        if self._detached_launch_started:
            if not self._launch_in_progress and self._runtime_state != "launch_timed_out":
                self._set_runtime_state(
                    "console_started_unreachable",
                    label="Runtime: started by Console (not reachable)",
                )
        elif self._bridge_seen_ready:
            self._set_runtime_state(
                "external_unreachable",
                label="Runtime: external (not reachable)",
            )
        else:
            self._set_runtime_state("unavailable")

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
            self._health_observed = True
            self._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
            if self._health_ok:
                self.bottom_status_label.setText("Bridge reachable")
            self._apply_runtime_observation()
            return
        if key == "bridge-status":
            self._status_observed = True
            if isinstance(payload, Mapping):
                bridge = payload.get("bridge")
                app_server = payload.get("app_server")
                self._bridge_ready = bridge in {"ready", "connected"}
                self._app_server_ready = app_server in {"ready", "connected"}
                self.bridge_status_label.setText(
                    "Bridge: connected" if self._bridge_ready else "Bridge: disconnected"
                )
                self.app_server_status_label.setText(
                    "App Server: ready" if self._app_server_ready else "App Server: failed"
                )
                self._apply_runtime_observation()
            else:
                self._bridge_ready = False
                self._app_server_ready = False
                self.bridge_status_label.setText("Bridge: disconnected")
                self.app_server_status_label.setText("App Server: failed")
                self._apply_runtime_observation()
            return
        if key == "launch:health":
            self._apply_launch_health(payload)
            return
        if key == "launch:status":
            self._apply_launch_status(payload)
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
        if key == "launch:health":
            self._readiness_health_ok = False
            return
        if key == "launch:status":
            self._readiness_bridge_ready = False
            self._readiness_app_server_ready = False
            return
        if key in {"health", "bridge-status"}:
            if key == "health":
                self._health_observed = True
                self._health_ok = False
            else:
                self._status_observed = True
                self._bridge_ready = False
                self._app_server_ready = False
            self.bridge_status_label.setText("Bridge: disconnected")
            self.app_server_status_label.setText("App Server: failed")
            self.bottom_status_label.setText(message)
            self._apply_runtime_observation()
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
        self.readiness_timer.stop()
        self._codex_probe.abort()
        self._launcher.close()
        self._client.abort_all()
        event.accept()
