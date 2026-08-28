from __future__ import annotations

from collections.abc import Callable, Mapping
from secrets import token_urlsafe
from time import monotonic
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSystemTrayIcon,
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
from .tunnel_resolver import TunnelResolutionError
from .tunnel_resolver import enumerate_candidates as enumerate_tunnel_candidates
from .tunnel_supervisor import TunnelSupervisor, tunnel_state_label
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
_STOP_CONFIRMATION_INTERVAL_MS = 350
_STOP_CONFIRMATION_TIMEOUT_SECONDS = 10.0
_RUNTIME_LABELS = {
    "unavailable": "Runtime: unavailable",
    "external": "Runtime: external",
    "launching": "Runtime: launching",
    "launch_failed": "Runtime: launch failed",
    "launch_timed_out": "Runtime: launch timed out",
    "stopping": "Runtime: stopping",
    "restarting": "Runtime: restarting",
    "stopped": "Runtime: stopped",
    "control_failed": "Runtime: control failed",
    "stop_timed_out": "Runtime: stop timed out",
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
        tunnel_supervisor: Any | None = None,
        tray_factory: Callable[[QWidget], Any] | None = None,
        tray_available: bool | None = None,
        quit_application: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._client = api_client or ApiClient(config.base_url, self)
        self._codex_probe = codex_probe if codex_probe is not None else self._new_codex_probe()
        self._launcher = (
            runtime_launcher if runtime_launcher is not None else BridgeRuntimeLauncher()
        )
        self._tunnel = (
            tunnel_supervisor if tunnel_supervisor is not None else self._new_tunnel_supervisor()
        )
        self._tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        )
        self._tray_factory = tray_factory or (lambda owner: QSystemTrayIcon(QIcon(), owner))
        self._quit_application = quit_application or self._quit_qapplication
        self.tray_icon: Any | None = None
        self.tray_menu: QMenu | None = None
        self._tray_actions: dict[str, QAction] = {}
        self._closing = False
        self._exit_finished = False
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
        self._control_token: str | None = None
        self._launch_generation = 0
        self._bridge_transition = False
        self._pending_bridge_action: str | None = None
        self._restart_tunnel_was_running = False
        self._control_request_pending = False
        self._stop_confirmation_active = False
        self._stop_confirmation_health_unavailable = False
        self._stop_confirmation_status_unavailable = False
        self._stop_confirmation_deadline = 0.0
        self._stop_outcome_uncertain = False
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
        self._build_tray()
        self._sync_tunnel_controls()
        self._sync_bridge_controls()
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

    def _new_tunnel_supervisor(self) -> TunnelSupervisor:
        environ = {}
        if self._config.tunnel_executable is not None:
            environ["CODEX_BRIDGE_TUNNEL_EXECUTABLE"] = self._config.tunnel_executable
        try:
            candidates = enumerate_tunnel_candidates(environ=environ)
        except TunnelResolutionError:
            candidates = ()
        executable = candidates[0].path if candidates else None
        return TunnelSupervisor(
            executable=executable,
            profile=self._config.tunnel_profile,
            parent=self,
        )

    def _build_ui(self) -> None:
        self.bridge_status_label = QLabel("Bridge: disconnected")
        self.app_server_status_label = QLabel("App Server: failed")
        self.stream_status_label = QLabel("Stream: disconnected")
        self.codex_status_label = QLabel("Codex: checking")
        self.runtime_status_label = QLabel("Runtime: unavailable")
        self.tunnel_status_label = QLabel("Tunnel: unavailable")
        self.start_bridge_button = QPushButton("Start Bridge")
        self.stop_bridge_button = QPushButton("Stop Bridge")
        self.restart_bridge_button = QPushButton("Restart Bridge")
        self.start_tunnel_button = QPushButton("Start Tunnel")
        self.stop_tunnel_button = QPushButton("Stop Tunnel")
        self.restart_tunnel_button = QPushButton("Restart Tunnel")
        self.start_bridge_button.setEnabled(False)
        self.stop_bridge_button.setEnabled(False)
        self.restart_bridge_button.setEnabled(False)
        self.stop_tunnel_button.setEnabled(False)
        self.restart_tunnel_button.setEnabled(False)
        for label in (
            self.bridge_status_label,
            self.app_server_status_label,
            self.stream_status_label,
            self.codex_status_label,
            self.runtime_status_label,
            self.tunnel_status_label,
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
        status_bar.addWidget(self.tunnel_status_label)
        status_bar.addWidget(self.start_bridge_button)
        status_bar.addWidget(self.stop_bridge_button)
        status_bar.addWidget(self.restart_bridge_button)
        status_bar.addWidget(self.start_tunnel_button)
        status_bar.addWidget(self.stop_tunnel_button)
        status_bar.addWidget(self.restart_tunnel_button)

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

    @staticmethod
    def _quit_qapplication() -> None:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.quit()

    def _connect_client(self) -> None:
        self._client.json_succeeded.connect(self.apply_json_result)
        self._client.json_failed.connect(self._apply_json_error)
        self._client.activity_received.connect(self._apply_activity)
        self._client.stream_state_changed.connect(self._apply_stream_state)
        self._client.control_succeeded.connect(self._apply_control_success)
        self._client.control_failed.connect(self._apply_control_failure)
        self.thread_pane.refresh_requested.connect(self.refresh)
        self.thread_pane.thread_selected.connect(self.select_thread)
        self.history_pane.older_requested.connect(self.load_older)

    def _connect_runtime(self) -> None:
        self._codex_probe.resolved.connect(self._apply_codex_resolution)
        self._codex_probe.failed.connect(self._apply_codex_probe_error)
        self.start_bridge_button.clicked.connect(self._start_bridge)
        self.stop_bridge_button.clicked.connect(self._stop_bridge)
        self.restart_bridge_button.clicked.connect(self._restart_bridge)
        self._tunnel.state_changed.connect(self._apply_tunnel_state)
        self._tunnel.message_changed.connect(self._apply_tunnel_message)
        self._tunnel.controls_changed.connect(self._apply_tunnel_controls)
        self.start_tunnel_button.clicked.connect(self._start_tunnel)
        self.stop_tunnel_button.clicked.connect(self._stop_tunnel)
        self.restart_tunnel_button.clicked.connect(self._restart_tunnel)

    def _build_tray(self) -> None:
        if not self._tray_available:
            return
        self.tray_icon = self._tray_factory(self)
        self.tray_icon.setToolTip("CodexBridge Console")
        menu = QMenu(self)
        self.tray_menu = menu
        for text, callback in (
            ("Show Console", self._show_console),
            ("Hide Console", self._hide_console),
        ):
            action = QAction(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)
            self._tray_actions[text] = action
        menu.addSeparator()
        for text, callback in (
            ("Start Bridge", self._start_bridge),
            ("Stop Bridge", self._stop_bridge),
            ("Restart Bridge", self._restart_bridge),
        ):
            action = QAction(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)
            self._tray_actions[text] = action
        menu.addSeparator()
        for text, callback in (
            ("Start Tunnel", self._start_tunnel),
            ("Stop Tunnel", self._stop_tunnel),
            ("Restart Tunnel", self._restart_tunnel),
        ):
            action = QAction(text, self)
            action.triggered.connect(callback)
            menu.addAction(action)
            self._tray_actions[text] = action
        menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._begin_exit)
        menu.addAction(exit_action)
        self._tray_actions["Exit"] = exit_action
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _show_console(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_console(self) -> None:
        self.hide()

    def _on_tray_activated(self, reason: object) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_console()

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
        self.stop_confirmation_timer = QTimer(self)
        self.stop_confirmation_timer.setInterval(_STOP_CONFIRMATION_INTERVAL_MS)
        self.stop_confirmation_timer.timeout.connect(self._on_stop_confirmation_tick)
        self.health_timer.start()
        self.thread_timer.start()

    def _apply_tunnel_state(self, state: str) -> None:
        if not self._closing:
            self.tunnel_status_label.setText(tunnel_state_label(state))
            self._update_bridge_controls()

    def _apply_tunnel_message(self, message: str) -> None:
        if not self._closing:
            self.bottom_status_label.setText(message)

    def _apply_tunnel_controls(self, start: bool, stop: bool, restart: bool) -> None:
        if self._bridge_transition:
            start = stop = restart = False
        self.start_tunnel_button.setEnabled(start)
        self.stop_tunnel_button.setEnabled(stop)
        self.restart_tunnel_button.setEnabled(restart)
        for name, enabled in (
            ("Start Tunnel", start),
            ("Stop Tunnel", stop),
            ("Restart Tunnel", restart),
        ):
            action = self._tray_actions.get(name)
            if action is not None:
                action.setEnabled(enabled)

    def _tunnel_is_transitioning(self) -> bool:
        return getattr(self._tunnel, "state", "unavailable") in {
            "checking",
            "starting",
            "stopping",
        }

    def _update_bridge_controls(self) -> None:
        enabled = (
            not self._closing
            and self._runtime_state == "console_started"
            and self._bridge_ready
            and self._app_server_ready
            and self._control_token is not None
            and not self._bridge_transition
            and not self._tunnel_is_transitioning()
        )
        self.stop_bridge_button.setEnabled(enabled)
        self.restart_bridge_button.setEnabled(enabled)
        for name in ("Stop Bridge", "Restart Bridge"):
            action = self._tray_actions.get(name)
            if action is not None:
                action.setEnabled(enabled)
        start_action = self._tray_actions.get("Start Bridge")
        if start_action is not None:
            start_action.setEnabled(self.start_bridge_button.isEnabled())

    def _sync_bridge_controls(self) -> None:
        self._update_start_button()
        self._update_bridge_controls()

    def _sync_tunnel_controls(self) -> None:
        actions = self._tunnel.action_state
        self._apply_tunnel_controls(
            actions.start_enabled,
            actions.stop_enabled,
            actions.restart_enabled,
        )

    def _start_tunnel(self) -> None:
        self._tunnel.start()

    def _stop_tunnel(self) -> None:
        self._tunnel.stop()

    def _restart_tunnel(self) -> None:
        self._tunnel.restart()

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
        self._update_bridge_controls()

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
            and not self._bridge_transition
            and not self._tunnel_is_transitioning()
            and self._runtime_state in {"unavailable", "stopped", "launch_failed"}
        )
        self.start_bridge_button.setEnabled(enabled)
        start_action = self._tray_actions.get("Start Bridge")
        if start_action is not None:
            start_action.setEnabled(enabled)

    def _start_bridge(self) -> None:
        resolution = self._codex_resolution
        if (
            resolution is None
            or self._bridge_ready
            or self._bridge_seen_ready
            or self._launch_in_progress
            or self._detached_launch_started
            or self._bridge_transition
            or self._closing
        ):
            return
        self._pending_bridge_action = None
        self._restart_tunnel_was_running = False
        self._bridge_transition = True
        self._sync_tunnel_controls()
        self._launch_bridge(resolution.path)

    def _launch_bridge(self, codex_executable: str) -> None:
        self._launch_generation += 1
        control_token = token_urlsafe(32)
        self._control_token = control_token
        self._launch_in_progress = True
        self._set_runtime_state("launching")
        try:
            result = self._launcher.launch(
                codex_executable=codex_executable,
                ui_port=self._config.port,
                control_token=control_token,
            )
        except Exception:
            self._launch_in_progress = False
            self._control_token = None
            self._bridge_transition = False
            self._set_runtime_state("launch_failed")
            self.bottom_status_label.setText("Bridge launch failed")
            return
        if not result.started:
            self._launch_in_progress = False
            self._control_token = None
            self._bridge_transition = False
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
            self._bridge_transition = False
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
        self._bridge_transition = False
        self._set_runtime_state("console_started", label="Runtime: started by Console")
        self.bottom_status_label.setText("Bridge started by Console")
        self._tunnel.set_bridge_ready(True)
        self._restore_restarted_tunnel()

    def _restore_restarted_tunnel(self) -> None:
        if self._restart_tunnel_was_running:
            self._restart_tunnel_was_running = False
            self._tunnel.start()

    def _bridge_control_allowed(self) -> bool:
        return (
            self._runtime_state == "console_started"
            and self._bridge_ready
            and self._app_server_ready
            and self._control_token is not None
            and not self._bridge_transition
            and not self._tunnel_is_transitioning()
            and not self._closing
        )

    def _tunnel_owned_running(self) -> bool:
        actions = getattr(self._tunnel, "action_state", None)
        return bool(getattr(actions, "stop_enabled", False)) and getattr(
            self._tunnel, "state", "running"
        ) in {"running", "ready", "not_ready"}

    def _stop_bridge(self) -> None:
        if not self._bridge_control_allowed():
            return
        self._begin_bridge_transition("stop", tunnel_running=self._tunnel_owned_running())

    def _restart_bridge(self) -> None:
        if not self._bridge_control_allowed():
            return
        self._begin_bridge_transition("restart", tunnel_running=self._tunnel_owned_running())

    def _begin_bridge_transition(self, action: str, *, tunnel_running: bool) -> None:
        self._bridge_transition = True
        self._pending_bridge_action = action
        self._restart_tunnel_was_running = tunnel_running
        self._sync_tunnel_controls()
        self._set_runtime_state("restarting" if action == "restart" else "stopping")
        if tunnel_running:
            started = self._tunnel.stop(on_finished=self._on_tunnel_stopped_for_bridge)
            if not started and not self._control_request_pending:
                self._request_bridge_shutdown()
        else:
            self._request_bridge_shutdown()

    def _on_tunnel_stopped_for_bridge(self) -> None:
        self._request_bridge_shutdown()

    def _request_bridge_shutdown(self) -> None:
        if self._closing or self._control_request_pending or self._stop_confirmation_active:
            return
        token = self._control_token
        if token is None:
            self._apply_control_failure("control:shutdown", "Bridge control request failed")
            return
        self._control_request_pending = True
        if not self._client.post_control_shutdown(token, key="control:shutdown"):
            self._control_request_pending = False
            self._apply_control_failure("control:shutdown", "Bridge control request failed")
            return

    def _apply_control_success(self, key: str) -> None:
        if key != "control:shutdown" or not self._control_request_pending:
            return
        self._control_request_pending = False
        self._stop_confirmation_active = True
        self._stop_confirmation_health_unavailable = False
        self._stop_confirmation_status_unavailable = False
        self._stop_confirmation_deadline = monotonic() + _STOP_CONFIRMATION_TIMEOUT_SECONDS
        self.stop_confirmation_timer.start()
        self.bottom_status_label.setText("Bridge shutdown requested")
        self._request_health()

    def _apply_control_failure(self, key: str, _message: str) -> None:
        if key != "control:shutdown":
            return
        self._control_request_pending = False
        self._stop_confirmation_active = False
        self.stop_confirmation_timer.stop()
        self._bridge_transition = False
        self._set_runtime_state("control_failed")
        self.bottom_status_label.setText("Bridge control request failed")

    def _maybe_finish_stop_confirmation(self) -> None:
        if (
            self._stop_confirmation_active
            and self._stop_confirmation_health_unavailable
            and self._stop_confirmation_status_unavailable
        ):
            self._confirm_bridge_stopped()

    def _on_stop_confirmation_tick(self) -> None:
        if not self._stop_confirmation_active:
            self.stop_confirmation_timer.stop()
            return
        if (
            self._stop_confirmation_health_unavailable
            and self._stop_confirmation_status_unavailable
        ):
            self._confirm_bridge_stopped()
        elif monotonic() >= self._stop_confirmation_deadline:
            self._stop_confirmation_active = False
            self.stop_confirmation_timer.stop()
            self._bridge_transition = False
            self._stop_outcome_uncertain = True
            self._health_observed = False
            self._status_observed = False
            self._set_runtime_state("stop_timed_out")
            self.bottom_status_label.setText("Bridge stop timed out")
        else:
            self._request_health()

    def _confirm_bridge_stopped(self) -> None:
        action = self._pending_bridge_action
        stop_outcome_was_uncertain = self._stop_outcome_uncertain
        self._stop_confirmation_active = False
        self.stop_confirmation_timer.stop()
        self._stop_outcome_uncertain = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._health_ok = False
        self._control_token = None
        self._detached_pid = None
        self._detached_launch_started = False
        self._bridge_seen_ready = False
        self._launch_in_progress = False
        self._tunnel.set_bridge_ready(False)
        self._pending_bridge_action = None
        if action == "restart" and not stop_outcome_was_uncertain:
            resolution = self._codex_resolution
            if resolution is None:
                self._bridge_transition = False
                self._restart_tunnel_was_running = False
                self._set_runtime_state("launch_failed")
                self.bottom_status_label.setText("Bridge relaunch failed")
                return
            self._launch_bridge(resolution.path)
            return
        self._restart_tunnel_was_running = False
        self._bridge_transition = False
        self._sync_tunnel_controls()
        self.bridge_status_label.setText("Bridge: disconnected")
        self.app_server_status_label.setText("App Server: failed")
        self._set_runtime_state("stopped")
        self.bottom_status_label.setText("Bridge stopped")

    def _apply_runtime_observation(self) -> None:
        ready = self._health_ok and self._bridge_ready and self._app_server_ready
        if self._bridge_transition or self._stop_confirmation_active:
            self._tunnel.set_bridge_ready(False)
            return
        if self._stop_outcome_uncertain:
            fresh_observations = self._health_observed and self._status_observed
            if fresh_observations and ready:
                self._stop_outcome_uncertain = False
                self._pending_bridge_action = None
            elif fresh_observations and (
                not self._health_ok and not self._bridge_ready and not self._app_server_ready
            ):
                self._confirm_bridge_stopped()
                return
            else:
                self._tunnel.set_bridge_ready(False)
                return
        self._tunnel.set_bridge_ready(ready)
        if ready:
            self._bridge_seen_ready = True
            if self._detached_launch_started:
                self._launch_in_progress = False
                self.readiness_timer.stop()
                self._set_runtime_state("console_started", label="Runtime: started by Console")
                self._restore_restarted_tunnel()
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
        if self._stop_confirmation_active and key in {"health", "bridge-status"}:
            if key == "health":
                self._health_observed = True
                self._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
                self._stop_confirmation_health_unavailable = False
            elif isinstance(payload, Mapping):
                self._status_observed = True
                self._bridge_ready = payload.get("bridge") in {"ready", "connected"}
                self._app_server_ready = payload.get("app_server") in {"ready", "connected"}
                self._stop_confirmation_status_unavailable = False
            self._maybe_finish_stop_confirmation()
            return
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
        if self._stop_confirmation_active and key in {"health", "bridge-status"}:
            if key == "health":
                self._health_observed = True
                self._health_ok = False
                self._stop_confirmation_health_unavailable = True
                self.bridge_status_label.setText("Bridge: disconnected")
            else:
                self._status_observed = True
                self._bridge_ready = False
                self._app_server_ready = False
                self._stop_confirmation_status_unavailable = True
                self.app_server_status_label.setText("App Server: failed")
            self._maybe_finish_stop_confirmation()
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
        if self._closing:
            event.accept()
            return
        if self._tray_available:
            self.hide()
            event.ignore()
            return
        self._begin_exit()
        self.hide()
        event.ignore()

    def _begin_exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.health_timer.stop()
        self.thread_timer.stop()
        self.selected_status_timer.stop()
        self.readiness_timer.stop()
        self.stop_confirmation_timer.stop()
        self._codex_probe.abort()
        self._launcher.close()
        self._client.abort_all()
        self._tunnel.close(on_finished=self._finish_exit)

    def _finish_exit(self) -> None:
        if self._exit_finished:
            return
        self._exit_finished = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self._quit_application()
