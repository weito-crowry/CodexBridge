from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from secrets import token_urlsafe
from time import monotonic
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
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
from .codex_updates import CodexUpdateInfo, CodexUpdateProbe, parse_codex_update_info
from .config import ConsoleConfig
from .runtime_launcher import BridgeRuntimeLauncher
from .tunnel_resolver import TunnelResolutionError
from .tunnel_resolver import enumerate_candidates as enumerate_tunnel_candidates
from .tunnel_supervisor import TunnelSupervisor, tunnel_state_label
from .usage import (
    CodexUsage,
    format_codex_usage,
    format_codex_usage_detail,
    format_codex_usage_tooltip,
    parse_codex_usage,
    usage_level,
)
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
_BRIDGE_START_RETRY_DELAYS_MS = (2_000, 5_000, 10_000)
_BRIDGE_LOSS_THRESHOLD = 2
_USAGE_INITIAL_DELAY_MS = 1_500
_USAGE_RETRY_DELAY_MS = 3_000
_USAGE_POLL_INTERVAL_MS = 5 * 60 * 1_000
_USAGE_MAX_ATTEMPTS = 3
_STOP_CONFIRMATION_INTERVAL_MS = 350
_STOP_CONFIRMATION_TIMEOUT_SECONDS = 10.0
_EXIT_TIMEOUT_SECONDS = 12.0
_ACTIVE_TURN_STATES = {
    "in_progress",
    "needs_approval",
    "needs_input",
    "needs_user_input",
}
_TERMINAL_TURN_STATES = {"completed", "interrupted", "failed", "error"}
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


def _turn_model_metadata_from_payload(payload: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        return {}
    raw_metadata = payload.get("turn_model_metadata")
    if not isinstance(raw_metadata, Mapping):
        return {}
    result: dict[str, Mapping[str, object]] = {}
    for turn_id, raw_entry in raw_metadata.items():
        if not isinstance(turn_id, str) or not isinstance(raw_entry, Mapping):
            continue
        raw_candidates = raw_entry.get("model_candidates")
        candidates: list[dict[str, object]] = []
        if isinstance(raw_candidates, list):
            for raw_candidate in raw_candidates:
                if not isinstance(raw_candidate, Mapping):
                    continue
                model = raw_candidate.get("model")
                if not isinstance(model, str) or not model:
                    continue
                effort = raw_candidate.get("reasoning_effort")
                candidates.append(
                    {
                        "model": model[:512],
                        "reasoning_effort": effort[:512]
                        if isinstance(effort, str) and effort
                        else None,
                    }
                )
        status = raw_entry.get("model_resolution_status")
        if not isinstance(status, str) or status not in {"resolved", "multiple", "unavailable"}:
            status = (
                "unavailable"
                if not candidates
                else "resolved"
                if len(candidates) == 1
                else "multiple"
            )
        result[turn_id] = {
            "model_candidates": candidates,
            "model_resolution_status": status,
        }
    return result


class MainWindow(QMainWindow):
    """Read-only desktop view over the existing localhost UI API."""

    def __init__(
        self,
        config: ConsoleConfig,
        parent: QWidget | None = None,
        *,
        api_client: Any | None = None,
        codex_probe: Any | None = None,
        codex_update_probe: Any | None = None,
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
        self._codex_update_probe = (
            codex_update_probe if codex_update_probe is not None else CodexUpdateProbe(parent=self)
        )
        self._launcher = (
            runtime_launcher if runtime_launcher is not None else BridgeRuntimeLauncher()
        )
        self._tunnel = (
            tunnel_supervisor if tunnel_supervisor is not None else self._new_tunnel_supervisor()
        )
        self._tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        )
        self._window_icon = self._standard_icon()
        self.setWindowIcon(self._window_icon)
        self._tray_factory = tray_factory or (
            lambda owner: QSystemTrayIcon(self._window_icon, owner)
        )
        self._quit_application = quit_application or self._quit_qapplication
        self.tray_icon: Any | None = None
        self.tray_menu: QMenu | None = None
        self._tray_actions: dict[str, QAction] = {}
        self._tray_usable = False
        self._tray_notified = False
        self._closing = False
        self._exit_finished = False
        self._selection_generation = 0
        self._selected_thread_id: str | None = None
        self._timeline_entries: list[TimelineEntry] = []
        self._turn_model_metadata: dict[str, Mapping[str, object]] = {}
        self._turn_statuses: dict[str, str] = {}
        self._active_thread_ids: set[str] = set()
        self._pending_rename_names: dict[str, str] = {}
        self._next_cursor: str | None = None
        self._stream_sync_pending = False
        self._reconnect_scheduled = False
        self._runtime_state = "unavailable"
        self._usage = CodexUsage()
        self._usage_sequence_active = False
        self._usage_attempts = 0
        self._usage_request_in_flight = False
        self._usage_request_mode: str | None = None
        self._codex_update_info: CodexUpdateInfo | None = None
        self._codex_update_busy = False
        self._codex_update_auto_check_started = False
        self._codex_resolution: CodexResolution | None = None
        self._bridge_seen_ready = False
        self._health_observed = False
        self._status_observed = False
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._bridge_loss_count = 0
        self._launch_in_progress = False
        self._managed_start_active = False
        self._bridge_start_retry_index = 0
        self._bridge_start_retry_exhausted = False
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
        self._exit_shutdown_pending = False
        self._exit_deadline = 0.0
        self._sigint_requested = False
        self._tunnel_resolution_error: str | None = None
        self._tunnel_state = getattr(self._tunnel, "state", "unavailable")

        self.setWindowTitle("CodexBridge Console")
        self._build_ui()
        self._set_initial_size()
        self._connect_client()
        self._build_timers()
        self._connect_runtime()
        self._build_tray()
        self._update_tunnel_client_status()
        self._sync_tunnel_controls()
        self._sync_bridge_controls()
        self._set_unavailable_state()
        self._codex_probe.start()
        self.refresh()

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    def _new_codex_probe(self) -> CodexVersionProbe:
        environ = dict(os.environ)
        explicit_executable = None
        config_executable = None
        if self._config.codex_executable_source == "explicit":
            explicit_executable = self._config.codex_executable
        elif self._config.codex_executable_source == "environment":
            if self._config.codex_executable is not None:
                environ["CODEX_BRIDGE_CODEX_EXECUTABLE"] = self._config.codex_executable
        elif self._config.codex_executable_source == "config":
            config_executable = self._config.codex_executable
        try:
            candidates = enumerate_candidates(
                environ,
                explicit_executable=explicit_executable,
                config_executable=config_executable,
            )
        except CodexResolutionError:
            candidates = ()
        return CodexVersionProbe(candidates, parent=self)

    def _new_tunnel_supervisor(self) -> TunnelSupervisor:
        environ: dict[str, str] = {}
        config_executable = None
        if self._config.tunnel_executable_source in {"explicit", "environment"}:
            if self._config.tunnel_executable is not None:
                environ["CODEX_BRIDGE_TUNNEL_EXECUTABLE"] = self._config.tunnel_executable
        elif self._config.tunnel_executable_source == "config":
            config_executable = self._config.tunnel_executable
        try:
            candidates = enumerate_tunnel_candidates(
                environ=environ,
                config_executable=config_executable,
            )
        except TunnelResolutionError as exc:
            candidates = ()
            self._tunnel_resolution_error = str(exc)
        executable = candidates[0].path if candidates else None
        return TunnelSupervisor(
            executable=executable,
            profile=self._config.tunnel_profile,
            resolution_source=candidates[0].source if candidates else "unavailable",
            validate_version=True,
            parent=self,
        )

    def _standard_icon(self) -> QIcon:
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application_icon = application.windowIcon()
            if not application_icon.isNull():
                return application_icon
            icon = application.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            if not icon.isNull():
                return icon
        return QIcon()

    def _build_ui(self) -> None:
        self.bridge_status_label = QLabel("Bridge: disconnected")
        self.app_server_status_label = QLabel("App Server: failed")
        self.stream_status_label = QLabel("Stream: idle")
        self.codex_status_label = QLabel("Codex: checking")
        self.runtime_status_label = QLabel("Runtime: unavailable")
        self.tunnel_status_label = QLabel("Tunnel: unavailable")
        self.tunnel_client_status_label = QLabel("Tunnel Client: unavailable")
        config_state = "ready" if self._config.roots_ready else "error"
        self.config_status_label = QLabel(
            f"Config: {config_state} · roots {self._config.roots_count}"
        )
        self.config_status_label.setToolTip(self._config.roots_error or "Allowed roots are ready")
        self.overall_status_label = QLabel("● Starting")
        self.overall_detail_label = QLabel("● Starting")
        self.usage_status_label = QLabel(format_codex_usage(self._usage))
        self.codex_update_banner_label = QLabel("")
        self.codex_update_banner_label.setVisible(False)
        self.status_button = QPushButton("Status")
        self.retry_now_button = QPushButton("Retry now")
        self.restart_codexbridge_button = QPushButton("Restart CodexBridge")
        self.advanced_toggle_button = QPushButton("Advanced")
        self.advanced_toggle_button.setCheckable(True)
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
        self.retry_now_button.setEnabled(False)
        self.restart_codexbridge_button.setEnabled(False)
        for label in (
            self.bridge_status_label,
            self.app_server_status_label,
            self.stream_status_label,
            self.codex_status_label,
            self.runtime_status_label,
            self.tunnel_status_label,
            self.tunnel_client_status_label,
            self.config_status_label,
        ):
            label.setObjectName("detailStatus")
        self.overall_status_label.setObjectName("topStatus")
        self.overall_detail_label.setObjectName("detailStatus")
        self.usage_status_label.setObjectName("topStatus")
        self.usage_status_label.setToolTip(format_codex_usage_tooltip(self._usage))

        self.usage_detail_label = QLabel(format_codex_usage_detail(self._usage))
        self.usage_detail_label.setWordWrap(True)
        self.codex_latest_label = QLabel("Not checked")
        self.codex_update_status_label = QLabel("Not checked")
        self.codex_update_message_label = QLabel("")
        self.codex_update_message_label.setWordWrap(True)
        self.codex_update_check_button = QPushButton("Check for updates")
        self.codex_update_button = QPushButton("Update")
        self.codex_update_check_button.setEnabled(False)
        self.codex_update_button.setEnabled(False)
        codex_update_controls = QWidget(self)
        codex_update_controls_layout = QHBoxLayout(codex_update_controls)
        codex_update_controls_layout.setContentsMargins(0, 0, 0, 0)
        codex_update_controls_layout.addWidget(self.codex_update_check_button)
        codex_update_controls_layout.addWidget(self.codex_update_button)
        self.status_refresh_button = QPushButton("Refresh")
        self.status_close_button = QPushButton("Close")

        self.status_dialog = QDialog(self)
        self.status_dialog.setWindowTitle("CodexBridge Status")
        self.status_dialog.setModal(False)
        dialog_layout = QVBoxLayout(self.status_dialog)

        def add_section(title: str, rows: list[tuple[str, QWidget]]) -> None:
            group = QGroupBox(title, self.status_dialog)
            form = QFormLayout(group)
            for name, widget in rows:
                form.addRow(name, widget)
            dialog_layout.addWidget(group)

        add_section("Overall", [("State", self.overall_detail_label)])
        add_section(
            "Connection",
            [
                ("Bridge", self.bridge_status_label),
                ("App Server", self.app_server_status_label),
                ("Stream", self.stream_status_label),
            ],
        )
        add_section(
            "Codex",
            [
                ("Version", self.codex_status_label),
                ("Latest", self.codex_latest_label),
                ("Update", self.codex_update_status_label),
                ("", codex_update_controls),
                ("Status", self.codex_update_message_label),
                ("Runtime", self.runtime_status_label),
            ],
        )
        add_section("Usage", [("Codex Usage", self.usage_detail_label)])
        add_section(
            "Tunnel",
            [
                ("Tunnel", self.tunnel_status_label),
                ("Client", self.tunnel_client_status_label),
            ],
        )
        add_section("Configuration", [("Config", self.config_status_label)])

        lifecycle_controls = QHBoxLayout()
        lifecycle_controls.addWidget(self.retry_now_button)
        lifecycle_controls.addWidget(self.restart_codexbridge_button)
        dialog_layout.addLayout(lifecycle_controls)
        dialog_layout.addWidget(self.advanced_toggle_button)

        self.advanced_controls = QWidget(self.status_dialog)
        bridge_controls = QHBoxLayout(self.advanced_controls)
        bridge_controls.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.start_bridge_button,
            self.stop_bridge_button,
            self.restart_bridge_button,
            self.start_tunnel_button,
            self.stop_tunnel_button,
            self.restart_tunnel_button,
        ):
            bridge_controls.addWidget(button)
        dialog_layout.addWidget(self.advanced_controls)
        self.advanced_controls.setVisible(False)
        dialog_buttons = QHBoxLayout()
        dialog_buttons.addStretch(1)
        dialog_buttons.addWidget(self.status_refresh_button)
        dialog_buttons.addWidget(self.status_close_button)
        dialog_layout.addLayout(dialog_buttons)
        self.status_dialog.adjustSize()

        status_bar = QHBoxLayout()
        status_bar.addWidget(self.overall_status_label)
        status_bar.addStretch(1)
        status_bar.addWidget(self.codex_update_banner_label)
        status_bar.addWidget(self.usage_status_label)
        status_bar.addWidget(self.status_button)

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

    def _set_initial_size(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        available_width = available.width()
        available_height = available.height()
        width = min(1_400, available_width, max(480, available_width - 32))
        height = min(850, available_height, max(480, available_height - 64))
        self.setMinimumSize(0, 0)
        self.resize(width, height)

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
        self.thread_pane.thread_rename_requested.connect(self._rename_thread)
        self.history_pane.older_requested.connect(self.load_older)

    def _connect_runtime(self) -> None:
        self._codex_probe.resolved.connect(self._apply_codex_resolution)
        self._codex_probe.failed.connect(self._apply_codex_probe_error)
        self._codex_update_probe.check_succeeded.connect(self._apply_codex_update_check)
        self._codex_update_probe.check_failed.connect(self._apply_codex_update_check_error)
        self._codex_update_probe.update_succeeded.connect(self._apply_codex_update_success)
        self._codex_update_probe.update_failed.connect(self._apply_codex_update_failure)
        self._codex_update_probe.busy_changed.connect(self._apply_codex_update_busy)
        self.start_bridge_button.clicked.connect(self._start_bridge)
        self.stop_bridge_button.clicked.connect(self._stop_bridge)
        self.restart_bridge_button.clicked.connect(self._restart_bridge)
        self._tunnel.state_changed.connect(self._apply_tunnel_state)
        self._tunnel.message_changed.connect(self._apply_tunnel_message)
        self._tunnel.controls_changed.connect(self._apply_tunnel_controls)
        self.start_tunnel_button.clicked.connect(self._start_tunnel)
        self.stop_tunnel_button.clicked.connect(self._stop_tunnel)
        self.restart_tunnel_button.clicked.connect(self._restart_tunnel)
        self.status_button.clicked.connect(self._show_status)
        self.status_refresh_button.clicked.connect(self._refresh_status)
        self.status_close_button.clicked.connect(self.status_dialog.close)
        self.codex_update_check_button.clicked.connect(self._check_codex_updates)
        self.codex_update_button.clicked.connect(self._update_codex)
        self.retry_now_button.clicked.connect(self._retry_now)
        self.restart_codexbridge_button.clicked.connect(self._restart_codexbridge)
        self.advanced_toggle_button.toggled.connect(self.advanced_controls.setVisible)

    def _show_status(self) -> None:
        self.status_dialog.show()
        self.status_dialog.raise_()
        self.status_dialog.activateWindow()

    def _refresh_status(self) -> None:
        self._request_usage(force=True)
        self.refresh()

    def _build_tray(self) -> None:
        if not self._tray_available or self._window_icon.isNull():
            return
        try:
            self.tray_icon = self._tray_factory(self)
            set_icon = getattr(self.tray_icon, "setIcon", None)
            if not callable(set_icon):
                self.tray_icon = None
                return
            set_icon(self._window_icon)
            self.tray_icon.setToolTip("CodexBridge Console")
        except Exception:
            self.tray_icon = None
            return
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
        try:
            self.tray_icon.setContextMenu(menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()
        except Exception:
            self.tray_icon = None
            self.tray_menu = None
            self._tray_actions.clear()
            return
        self._tray_usable = True

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
        self.bridge_start_retry_timer = QTimer(self)
        self.bridge_start_retry_timer.setSingleShot(True)
        self.bridge_start_retry_timer.timeout.connect(self._on_bridge_start_retry_timeout)
        self.usage_initial_timer = QTimer(self)
        self.usage_initial_timer.setSingleShot(True)
        self.usage_initial_timer.setInterval(_USAGE_INITIAL_DELAY_MS)
        self.usage_initial_timer.timeout.connect(self._on_usage_initial_timeout)
        self.usage_retry_timer = QTimer(self)
        self.usage_retry_timer.setSingleShot(True)
        self.usage_retry_timer.setInterval(_USAGE_RETRY_DELAY_MS)
        self.usage_retry_timer.timeout.connect(self._on_usage_retry_timeout)
        self.usage_poll_timer = QTimer(self)
        self.usage_poll_timer.setInterval(_USAGE_POLL_INTERVAL_MS)
        self.usage_poll_timer.timeout.connect(self._on_usage_poll_timeout)
        self.codex_update_auto_timer = QTimer(self)
        self.codex_update_auto_timer.setSingleShot(True)
        self.codex_update_auto_timer.setInterval(5_000)
        self.codex_update_auto_timer.timeout.connect(self._on_codex_update_auto_timeout)
        self.stop_confirmation_timer = QTimer(self)
        self.stop_confirmation_timer.setInterval(_STOP_CONFIRMATION_INTERVAL_MS)
        self.stop_confirmation_timer.timeout.connect(self._on_stop_confirmation_tick)
        self.exit_timer = QTimer(self)
        self.exit_timer.setSingleShot(True)
        self.exit_timer.setInterval(int(_EXIT_TIMEOUT_SECONDS * 1_000))
        self.exit_timer.timeout.connect(self._on_exit_timeout)
        self.signal_timer = QTimer(self)
        self.signal_timer.setInterval(50)
        self.signal_timer.timeout.connect(self._on_signal_tick)
        self.health_timer.start()
        self.thread_timer.start()
        self.signal_timer.start()

    def request_sigint(self) -> None:
        self._sigint_requested = True

    def _on_signal_tick(self) -> None:
        if self._sigint_requested:
            self._sigint_requested = False
            self._begin_exit()

    def _apply_tunnel_state(self, state: str) -> None:
        self._tunnel_state = state
        if not self._closing:
            self.tunnel_status_label.setText(tunnel_state_label(state))
            self._update_tunnel_client_status()
            self._update_bridge_controls()
            self._sync_overall_status()

    def _update_tunnel_client_status(self) -> None:
        version = getattr(self._tunnel, "client_version", None)
        source = getattr(self._tunnel, "resolution_source", "unavailable")
        executable = getattr(self._tunnel, "executable", None)
        if version is None:
            text = "Tunnel Client: unavailable"
        else:
            text = f"Tunnel Client: {version} · {source}"
        self.tunnel_client_status_label.setText(text)
        if executable is not None:
            self.tunnel_client_status_label.setToolTip(executable)
        if self._tunnel_resolution_error is not None:
            self.tunnel_client_status_label.setToolTip(self._tunnel_resolution_error)

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
        self.restart_codexbridge_button.setEnabled(enabled)
        if self._runtime_state == "external" and self._bridge_ready and self._app_server_ready:
            self.restart_codexbridge_button.setToolTip(
                "Disabled while the Bridge is managed externally."
            )
        else:
            self.restart_codexbridge_button.setToolTip("Restart the Console-managed Bridge.")
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

    def _sync_empty_state(self) -> None:
        if not self._bridge_ready:
            location = f"{self._config.host}:{self._config.port}"
            self.history_pane.set_empty_state(
                f"CodexBridge is not available on {location}\nStart codex-bridge and retry."
            )
            self.activity_pane.set_empty_state("Bridge unavailable")
            return
        if getattr(self.thread_pane, "thread_count", 0) == 0:
            self.history_pane.set_empty_state("No threads found.")
            self.activity_pane.set_empty_state("No thread selected.")
            return
        if self._selected_thread_id is None:
            self.history_pane.set_empty_state("Select a thread to view history.")
            self.activity_pane.set_empty_state("Select a thread to view activity.")

    def _set_unavailable_state(self) -> None:
        self._bridge_ready = False
        self._sync_overall_status()
        self._sync_empty_state()

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
        self._sync_codex_update_controls()
        self._schedule_codex_update_auto_check()
        self._maybe_auto_start_bridge()

    def _apply_codex_probe_error(self, _message: str) -> None:
        if self._closing:
            return
        self._codex_resolution = None
        self.codex_status_label.setText("Codex: not found")
        self._update_start_button()
        self._sync_codex_update_controls()

    def _sync_codex_update_controls(self) -> None:
        has_resolution = self._codex_resolution is not None
        self.codex_update_check_button.setEnabled(has_resolution and not self._codex_update_busy)
        can_update = (
            self._codex_update_info is not None
            and self._codex_update_info.can_update
            and not self._codex_update_busy
        )
        self.codex_update_button.setEnabled(can_update)

    def _schedule_codex_update_auto_check(self) -> None:
        if (
            self._closing
            or self._codex_update_auto_check_started
            or not self._usage_ready
            or self._codex_resolution is None
        ):
            return
        if not self.codex_update_auto_timer.isActive():
            self.codex_update_auto_timer.start()

    def _on_codex_update_auto_timeout(self) -> None:
        if self._codex_update_auto_check_started:
            return
        if not self._usage_ready or self._codex_resolution is None:
            return
        self._codex_update_auto_check_started = True
        self._check_codex_updates()

    def _apply_codex_update_busy(self, busy: bool) -> None:
        if self._closing:
            return
        self._codex_update_busy = busy
        self._sync_codex_update_controls()

    def _check_codex_updates(self) -> None:
        resolution = self._codex_resolution
        if self._closing or resolution is None or self._codex_update_busy:
            return
        self._codex_update_busy = True
        self.codex_update_status_label.setText("Checking…")
        self.codex_update_message_label.setText("")
        self._sync_codex_update_controls()
        if not self._codex_update_probe.check_for_updates(resolution):
            self._apply_codex_update_check_error("Codex update check failed")

    def _apply_codex_update_check(self, payload: object) -> None:
        if self._closing or self._codex_resolution is None:
            return
        info = (
            payload
            if isinstance(payload, CodexUpdateInfo)
            else parse_codex_update_info(
                payload, current_version=self._codex_resolution.version or ""
            )
        )
        self._codex_update_info = info
        self.codex_latest_label.setText(info.latest_version or "Unavailable")
        self.codex_update_status_label.setText(info.display_status)
        self.codex_update_message_label.setText(
            f"Last checked: {info.last_checked_at}" if info.last_checked_at else ""
        )
        self.codex_update_banner_label.setText(
            "↑ Codex update available" if info.update_available else ""
        )
        self.codex_update_banner_label.setVisible(info.update_available)
        self._sync_codex_update_controls()

    def _apply_codex_update_check_error(self, message: str) -> None:
        if self._closing:
            return
        self._codex_update_busy = False
        self._codex_update_info = None
        self.codex_latest_label.setText("Unavailable")
        self.codex_update_status_label.setText("Unavailable")
        self.codex_update_message_label.setText(message)
        self.codex_update_banner_label.clear()
        self.codex_update_banner_label.setVisible(False)
        self._sync_codex_update_controls()

    def _update_codex(self) -> None:
        resolution = self._codex_resolution
        info = self._codex_update_info
        if self._closing or resolution is None or info is None or not info.can_update:
            return
        answer = QMessageBox.question(
            self,
            "Update Codex",
            "Install the available Codex update now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._codex_update_busy = True
        self.codex_update_status_label.setText("Updating…")
        self.codex_update_message_label.setText("")
        self._sync_codex_update_controls()
        if not self._codex_update_probe.update(resolution):
            self._apply_codex_update_failure("Codex update failed")

    def _apply_codex_update_success(self) -> None:
        if self._closing:
            return
        self._codex_update_busy = False
        self._codex_update_info = None
        self.codex_update_status_label.setText("Update completed; restart required")
        self.codex_update_message_label.setText("")
        self.codex_update_banner_label.clear()
        self.codex_update_banner_label.setVisible(False)
        self._sync_codex_update_controls()

    def _apply_codex_update_failure(self, message: str) -> None:
        if self._closing:
            return
        self._codex_update_busy = False
        self.codex_update_status_label.setText("Update failed")
        self.codex_update_message_label.setText(message)
        self._sync_codex_update_controls()

    def _set_runtime_state(self, state: str, *, label: str | None = None) -> None:
        self._runtime_state = state
        self.runtime_status_label.setText(label or _RUNTIME_LABELS.get(state, f"Runtime: {state}"))
        self._update_start_button()
        self._update_bridge_controls()
        self._sync_overall_status()

    def _request_usage(self, *, force: bool = False) -> None:
        if self._closing:
            return
        if force:
            if self._usage_request_in_flight:
                return
            self.usage_initial_timer.stop()
            self.usage_retry_timer.stop()
            self._usage_sequence_active = False
            self._usage_attempts = 0
            self._usage_request_mode = "manual"
            self._start_usage_request("manual")
            return
        self._start_usage_request("initial")

    @property
    def _usage_ready(self) -> bool:
        return self._bridge_ready and self._app_server_ready

    def _begin_usage_sequence(self) -> None:
        self._usage_sequence_active = True
        self._usage_attempts = 0
        self._usage_request_mode = None
        self.usage_initial_timer.start()
        self.usage_retry_timer.stop()
        self.usage_poll_timer.start()

    def _invalidate_usage_sequence(self) -> None:
        self._usage_sequence_active = False
        self._usage_attempts = 0
        self.usage_initial_timer.stop()
        self.usage_retry_timer.stop()
        self.usage_poll_timer.stop()
        if self._usage_request_in_flight:
            self._client.abort_json_group("usage")
        self._usage_request_in_flight = False
        self._usage_request_mode = None

    def _start_usage_request(self, mode: str) -> None:
        if self._closing or self._usage_request_in_flight:
            return
        if mode != "manual" and not self._usage_ready:
            return
        if mode == "initial":
            if not self._usage_sequence_active or self._usage_attempts >= _USAGE_MAX_ATTEMPTS:
                return
            self._usage_attempts += 1
        if self._client.get_json("/ui-api/account/rate-limits", key="usage"):
            self._usage_request_in_flight = True
            self._usage_request_mode = mode
        elif mode == "initial":
            self._usage_attempts -= 1

    def _on_usage_initial_timeout(self) -> None:
        self.usage_initial_timer.stop()
        self._start_usage_request("initial")

    def _on_usage_retry_timeout(self) -> None:
        self.usage_retry_timer.stop()
        if self._usage_sequence_active:
            self._start_usage_request("initial")

    def _on_usage_poll_timeout(self) -> None:
        if self._usage_ready:
            self._start_usage_request("periodic")

    def _apply_usage(self, payload: object) -> None:
        self._usage = parse_codex_usage(payload)
        self.usage_status_label.setText(format_codex_usage(self._usage))
        self.usage_status_label.setToolTip(format_codex_usage_tooltip(self._usage))
        self.usage_detail_label.setText(format_codex_usage_detail(self._usage))
        level = usage_level(self._usage)
        font = self.usage_status_label.font()
        font.setBold(level != "normal")
        if level == "error":
            font.setWeight(QFont.Weight.Bold)
        elif level == "warning":
            font.setWeight(QFont.Weight.DemiBold)
        else:
            font.setWeight(QFont.Weight.Normal)
        self.usage_status_label.setFont(font)
        palette = self.usage_status_label.palette()
        role = {
            "normal": QPalette.ColorRole.Text,
            "warning": QPalette.ColorRole.Link,
            "error": QPalette.ColorRole.BrightText,
        }[level]
        palette.setColor(QPalette.ColorRole.WindowText, palette.color(role))
        self.usage_status_label.setPalette(palette)

    def _sync_overall_status(self) -> None:
        bridge_ready = self._health_ok and self._bridge_ready and self._app_server_ready
        if bridge_ready:
            if self._tunnel_state == "ready":
                state = "Ready"
            elif self._tunnel_state in {"checking", "starting", "running", "stopping"}:
                state = "Starting"
            else:
                state = "Degraded"
        elif (
            self._launch_in_progress
            or self._bridge_transition
            or self._managed_start_active
            or self._runtime_state
            in {
                "launching",
                "restarting",
                "stopping",
                "external_unreachable",
                "console_started_unreachable",
            }
        ):
            state = "Starting"
        elif self._bridge_start_retry_exhausted or self._runtime_state in {
            "launch_failed",
            "control_failed",
            "stop_timed_out",
        }:
            state = "Error"
        elif not self._health_observed and not self._status_observed:
            state = "Starting"
        else:
            state = "Error"
        text = f"● {state}"
        self.overall_status_label.setText(text)
        self.overall_detail_label.setText(text)

    def _update_start_button(self) -> None:
        self.start_bridge_button.setEnabled(self._can_start_bridge())
        start_action = self._tray_actions.get("Start Bridge")
        if start_action is not None:
            start_action.setEnabled(self.start_bridge_button.isEnabled())
        self._update_lifecycle_controls()

    def _update_lifecycle_controls(self) -> None:
        bridge_ready = self._health_ok and self._bridge_ready and self._app_server_ready
        retry_enabled = (
            not self._closing
            and not self._launch_in_progress
            and not self._bridge_transition
            and (not bridge_ready or self._tunnel_state != "ready")
        )
        self.retry_now_button.setEnabled(retry_enabled)

    def _can_start_bridge(self) -> bool:
        return (
            not self._closing
            and self._config.roots_ready
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
            and not self.bridge_start_retry_timer.isActive()
            and not self._bridge_start_retry_exhausted
            and self._runtime_state in {"unavailable", "stopped", "launch_failed"}
        )

    def _maybe_auto_start_bridge(self) -> None:
        if self._bridge_start_retry_exhausted or self._managed_start_active:
            return
        if not self._can_start_bridge():
            return
        self._managed_start_active = True
        self._start_bridge(managed=True)

    def _retry_now(self) -> None:
        if self._closing or self._launch_in_progress or self._bridge_transition:
            return
        bridge_ready = self._health_ok and self._bridge_ready and self._app_server_ready
        if bridge_ready and self._tunnel_state != "ready":
            retry_now = getattr(self._tunnel, "retry_now", None)
            if callable(retry_now):
                retry_now()
            return
        self.bridge_start_retry_timer.stop()
        self._bridge_start_retry_index = 0
        self._bridge_start_retry_exhausted = False
        self._managed_start_active = True
        if self._can_start_bridge():
            self._start_bridge(managed=True)

    def _restart_codexbridge(self) -> None:
        self._restart_bridge()

    def _schedule_managed_bridge_retry(self) -> None:
        if self._closing or not self._managed_start_active:
            return
        if self._bridge_start_retry_index >= len(_BRIDGE_START_RETRY_DELAYS_MS):
            self._bridge_start_retry_exhausted = True
            self._managed_start_active = False
            self._update_start_button()
            return
        delay = _BRIDGE_START_RETRY_DELAYS_MS[self._bridge_start_retry_index]
        self._bridge_start_retry_index += 1
        self.bridge_start_retry_timer.setInterval(delay)
        self.bridge_start_retry_timer.start()

    def _on_bridge_start_retry_timeout(self) -> None:
        self.bridge_start_retry_timer.stop()
        if self._closing or self._bridge_start_retry_exhausted:
            return
        if self._can_start_bridge():
            self._start_bridge(managed=True)
        else:
            self._managed_start_active = False

    def _start_bridge(self, *, managed: bool = False) -> None:
        resolution = self._codex_resolution
        if resolution is None or not self._can_start_bridge():
            return
        if managed:
            self._managed_start_active = True
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
                allowed_roots=self._config.allowed_roots,
            )
        except Exception:
            self._launch_in_progress = False
            self._control_token = None
            self._bridge_transition = False
            self._set_runtime_state("launch_failed")
            self.bottom_status_label.setText("Bridge launch failed")
            self._schedule_managed_bridge_retry()
            return
        if not result.started:
            self._launch_in_progress = False
            self._control_token = None
            self._bridge_transition = False
            self._set_runtime_state("launch_failed")
            self.bottom_status_label.setText("Bridge launch failed")
            self._schedule_managed_bridge_retry()
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
            if self._managed_start_active:
                self._set_runtime_state("launch_failed")
            else:
                self._set_runtime_state("launch_timed_out")
            self.bottom_status_label.setText("Bridge launch timed out; it may still be starting")
            self._schedule_managed_bridge_retry()
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
        self._bridge_loss_count = 0
        self.bridge_start_retry_timer.stop()
        self._bridge_start_retry_index = 0
        self._bridge_start_retry_exhausted = False
        self._managed_start_active = False
        self._bridge_transition = False
        self._set_runtime_state("console_started", label="Runtime: started by Console")
        self.bottom_status_label.setText("Bridge started by Console")
        self._tunnel.set_bridge_ready(True)
        self._begin_usage_sequence()
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

    def _request_bridge_shutdown(self, *, allow_closing: bool = False) -> None:
        if (
            (self._closing and not allow_closing)
            or self._control_request_pending
            or self._stop_confirmation_active
        ):
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
        if self._exit_shutdown_pending:
            self._finish_exit()
            return
        self._bridge_transition = False
        self._set_runtime_state("control_failed")
        self.bottom_status_label.setText("Bridge control request failed")

    def _maybe_finish_stop_confirmation(self) -> None:
        if (
            self._stop_confirmation_active
            and self._stop_confirmation_health_unavailable
            and self._stop_confirmation_status_unavailable
        ):
            if self._exit_shutdown_pending:
                self._finish_exit()
                return
            self._confirm_bridge_stopped()

    def _on_stop_confirmation_tick(self) -> None:
        if not self._stop_confirmation_active:
            self.stop_confirmation_timer.stop()
            return
        if (
            self._stop_confirmation_health_unavailable
            and self._stop_confirmation_status_unavailable
        ):
            if self._exit_shutdown_pending:
                self._finish_exit()
                return
            self._confirm_bridge_stopped()
        elif monotonic() >= self._stop_confirmation_deadline:
            self._stop_confirmation_active = False
            self.stop_confirmation_timer.stop()
            if self._exit_shutdown_pending:
                self._finish_exit()
                return
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
        self._bridge_loss_count = 0
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
            self.bridge_start_retry_timer.stop()
            self._bridge_start_retry_index = 0
            self._bridge_start_retry_exhausted = False
            self._managed_start_active = False
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
        self._maybe_auto_start_bridge()

    def _record_bridge_status_observation(self, available: bool) -> None:
        if available:
            self._bridge_loss_count = 0
            return
        self._bridge_loss_count += 1
        if (
            self._bridge_loss_count < _BRIDGE_LOSS_THRESHOLD
            or self._closing
            or self._bridge_transition
            or self._launch_in_progress
        ):
            return
        self._recover_bridge_after_loss()

    def _recover_bridge_after_loss(self) -> None:
        self._bridge_loss_count = _BRIDGE_LOSS_THRESHOLD
        if self._detached_launch_started and self._control_token is not None:
            self._managed_start_active = True
            self._begin_bridge_transition("restart", tunnel_running=self._tunnel_owned_running())
            return
        self._bridge_seen_ready = False
        self._runtime_state = "unavailable"
        self._update_start_button()
        self._maybe_auto_start_bridge()

    def _request_health(self) -> None:
        self._client.get_json("/healthz", key="health")
        self._request_bridge_status()

    def _request_bridge_status(self) -> None:
        self._client.get_json("/ui-api/status", key="bridge-status")

    def _request_threads(self) -> None:
        self._client.get_json("/ui-api/threads", key="threads", query={"limit": 100})

    def _rename_thread(self, thread_id: str) -> None:
        name, accepted = QInputDialog.getText(
            self,
            "名前を変更",
            "名前:",
            QLineEdit.EchoMode.Normal,
            self.thread_pane.thread_name(thread_id),
        )
        if not accepted or not name.strip():
            return
        key = f"rename:{thread_id}"
        if key in self._pending_rename_names:
            return
        if self._client.post_json(
            f"/ui-api/threads/{quote(thread_id, safe='')}/name",
            {"name": name},
            key=key,
        ):
            self._pending_rename_names[key] = name

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
        self._turn_model_metadata = {}
        self._turn_statuses = {}
        self._next_cursor = None
        self._stream_sync_pending = thread_id is not None
        self._reconnect_scheduled = False
        self._client.abort_json_group("selection:")
        self._client.stop_stream()
        self.selected_status_timer.stop()
        if thread_id is None:
            self.stream_status_label.setText("Stream: idle")
            self._sync_empty_state()
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
        if key.startswith("rename:"):
            name = self._pending_rename_names.pop(key, None)
            if name is not None:
                self.thread_pane.update_thread_name(key.removeprefix("rename:"), name)
            return
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
            self._sync_overall_status()
            self._sync_empty_state()
            return
        if key == "bridge-status":
            self._status_observed = True
            was_ready = self._bridge_ready and self._app_server_ready
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
            self._record_bridge_status_observation(self._bridge_ready and self._app_server_ready)
            ready = self._usage_ready
            if ready and not was_ready:
                self._begin_usage_sequence()
                self._schedule_codex_update_auto_check()
            elif not ready:
                self._invalidate_usage_sequence()
            self._sync_overall_status()
            self._sync_empty_state()
            return
        if key == "usage":
            self._usage_request_in_flight = False
            self._usage_request_mode = None
            self._usage_sequence_active = False
            self.usage_initial_timer.stop()
            self.usage_retry_timer.stop()
            if self._usage_ready:
                self.usage_poll_timer.start()
            self._apply_usage(payload)
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
                self.thread_pane.set_threads(threads, active_thread_ids=self._active_thread_ids)
                if not threads:
                    self.thread_pane.set_empty_state("No threads found.")
                self._sync_empty_state()
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
                if any(status in _ACTIVE_TURN_STATES for status in self._turn_statuses.values()):
                    if self._selected_thread_id is not None:
                        self._set_thread_active(self._selected_thread_id, True)
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
        page_metadata = _turn_model_metadata_from_payload(payload)
        if prepend:
            self._turn_model_metadata.update(page_metadata)
        else:
            self._turn_model_metadata = page_metadata
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
            turn_model_metadata=self._turn_model_metadata,
        )

    def _apply_status(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            thread_id = self._selected_thread_id
        state = payload.get("state")
        if isinstance(thread_id, str) and isinstance(state, str):
            self._update_thread_activity(thread_id, state)
        self.activity_pane.set_snapshot(payload)
        self.bottom_status_label.setText("Thread snapshot updated")
        if self._stream_sync_pending and self._selected_thread_id is not None:
            self._stream_sync_pending = False
            self._client.start_stream(self._selected_thread_id, self._selection_generation)

    def _set_thread_active(self, thread_id: str, active: bool) -> None:
        if active:
            self._active_thread_ids.add(thread_id)
        else:
            self._active_thread_ids.discard(thread_id)
        self.thread_pane.set_active_thread_ids(self._active_thread_ids)

    def _update_thread_activity(self, thread_id: str, state: str) -> None:
        if state in _ACTIVE_TURN_STATES:
            self._set_thread_active(thread_id, True)
        elif state in _TERMINAL_TURN_STATES or state == "not_loaded":
            self._set_thread_active(thread_id, False)

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
        if key.startswith("rename:"):
            self._pending_rename_names.pop(key, None)
            return
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
                self._invalidate_usage_sequence()
            self.bridge_status_label.setText("Bridge: disconnected")
            self.app_server_status_label.setText("App Server: failed")
            self.bottom_status_label.setText(message)
            self._apply_runtime_observation()
            if key == "bridge-status":
                self._record_bridge_status_observation(False)
            self._sync_overall_status()
            self._sync_empty_state()
            return
        if key == "usage":
            if not self._usage_request_in_flight:
                return
            mode = self._usage_request_mode
            self._usage_request_in_flight = False
            self._usage_request_mode = None
            if (
                mode == "initial"
                and self._usage_sequence_active
                and self._usage_ready
                and self._usage_attempts < _USAGE_MAX_ATTEMPTS
            ):
                self.usage_retry_timer.start()
            else:
                self._usage_sequence_active = False
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
        thread_id = payload.get("thread_id")
        activity_type = payload.get("type")
        activity_status = payload.get("status")
        if isinstance(thread_id, str) and isinstance(activity_type, str):
            if (
                activity_type == "turn_started"
                or activity_status == "in_progress"
                or activity_type in {"approval_requested", "user_input_requested"}
                and activity_status == "requested"
            ):
                self._set_thread_active(thread_id, True)
            elif (
                activity_type
                in {
                    "turn_completed",
                    "turn_failed",
                    "turn_interrupted",
                    "error",
                }
                and activity_status in _TERMINAL_TURN_STATES
            ):
                self._set_thread_active(thread_id, False)
        self.activity_pane.append_activity(payload)

    def _apply_stream_state(self, generation: int, state: str) -> None:
        if self._closing or generation != self._selection_generation:
            return
        if state == "disconnected" and self._selected_thread_id is None:
            self.stream_status_label.setText("Stream: idle")
        else:
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
        if self._tray_usable:
            self.hide()
            if not self._tray_notified and self.tray_icon is not None:
                show_message = getattr(self.tray_icon, "showMessage", None)
                if callable(show_message):
                    try:
                        show_message(
                            "CodexBridge Console",
                            "Console is still running in the system tray.",
                        )
                    except Exception:
                        pass
                self._tray_notified = True
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
        self.bridge_start_retry_timer.stop()
        self.stop_confirmation_timer.stop()
        self._codex_probe.abort()
        self._exit_deadline = monotonic() + _EXIT_TIMEOUT_SECONDS
        self.exit_timer.start()
        self._tunnel.close(on_finished=self._on_exit_tunnel_stopped)

    def _on_exit_tunnel_stopped(self) -> None:
        if self._exit_finished or self._exit_shutdown_pending:
            return
        if self._detached_launch_started and self._control_token is not None:
            self._exit_shutdown_pending = True
            self._request_bridge_shutdown(allow_closing=True)
            return
        self._finish_exit()

    def _on_exit_timeout(self) -> None:
        self._finish_exit()

    def _finish_exit(self) -> None:
        if self._exit_finished:
            return
        self._exit_finished = True
        self.exit_timer.stop()
        self.signal_timer.stop()
        self.stop_confirmation_timer.stop()
        self._invalidate_usage_sequence()
        abort_update = getattr(self._codex_update_probe, "abort", None)
        if callable(abort_update):
            abort_update()
        self._client.abort_all()
        self._launcher.close()
        if self.tray_icon is not None:
            try:
                self.tray_icon.hide()
                delete_later = getattr(self.tray_icon, "deleteLater", None)
                if callable(delete_later):
                    delete_later()
            except Exception:
                pass
        self._quit_application()
