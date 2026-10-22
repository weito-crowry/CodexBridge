from __future__ import annotations

import os
import sys
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from secrets import token_urlsafe
from time import monotonic, time
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from PySide6.QtCore import QTimer

from PySide6.QtCore import QProcess, QSettings, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QIcon,
)
from PySide6.QtGui import (
    QDesktopServices as QDesktopServices,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QToolButton,
    QWidget,
)
from PySide6.QtWidgets import QMessageBox as QMessageBox

from ..models import ApprovalDecision
from ..tunnel_profile import TunnelProfileError, generate_tunnel_profile
from .api_client import ApiClient
from .appearance import AppearanceController, install_appearance
from .bridge_managed_retry import BridgeManagedRetryCoordinator
from .bridge_readiness import BridgeReadinessCoordinator
from .bridge_runtime_observer import BridgeRuntimeObserver
from .bridge_stop_confirmation import BridgeStopConfirmationCoordinator
from .bridge_transition import BridgeTransitionContext, BridgeTransitionCoordinator
from .bulk_thread_naming import BulkThreadNamingController
from .codex_links import open_codex_uri
from .codex_resolver import (
    CodexResolution,
    CodexResolutionError,
    CodexVersionProbe,
    enumerate_candidates,
)
from .codex_update_controller import CodexUpdateController
from .codex_updates import CodexUpdateProbe
from .composer_controller import create_composer_controller
from .config import ConsoleConfig
from .diagnostics import DiagnosticsReader
from .diagnostics_panel import DiagnosticsPanelController
from .history_requests import (
    HistoryRequestContext,
    HistoryRequestCoordinator,
)
from .history_stream import (
    TERMINAL_TURN_STATES,
    HistoryStreamContext,
    HistoryStreamCoordinator,
)
from .history_timeline import ACTIVE_TURN_STATES, HistoryTimelineState
from .main_window_layout import build_main_window_ui
from .notification_controller import NotificationController, NotificationSettingsDialog
from .project_folder_actions import ProjectFolderActionsController
from .project_names import read_local_project_names
from .runtime_launcher import BridgeRuntimeLauncher
from .runtime_ui import RuntimeUiPresenter
from .settings_actions import SettingsActionController
from .thread_interactions import ThreadInteractionController
from .timer_bootstrap import build_console_timers
from .tray_controller import TrayController
from .tunnel_resolver import TunnelResolutionError
from .tunnel_resolver import enumerate_candidates as enumerate_tunnel_candidates
from .tunnel_supervisor import TunnelSupervisor, tunnel_state_label
from .usage import (
    CodexUsage,
    CodexUsageSnapshot,
)
from .usage_history import (
    default_usage_history_path,
    record_usage_sample,
)
from .usage_history_window import UsageHistoryWindow
from .usage_presentation import UsagePresentationController
from .usage_requests import UsageRequestCoordinator
from .usage_shortcut_label import UsageShortcutLabel
from .usage_snapshots import UsageSnapshotStore
from .widgets import (
    ActivityPane,
    HistoryPane,
    ThreadListPane,
    TimelineEntry,
    copy_to_clipboard,
)

_EXIT_TIMEOUT_SECONDS = 12.0


def _runtime_thread_status_observation(thread: Mapping[str, object]) -> tuple[bool, str | None]:
    status = thread.get("runtime_status")
    if not isinstance(status, Mapping):
        return False, None
    status_type = status.get("type")
    if not isinstance(status_type, str):
        return False, None
    if status_type in {"notLoaded", "idle", "systemError"}:
        return True, None
    if status_type != "active":
        return False, None
    active_flags = status.get("active_flags")
    if not isinstance(active_flags, list) or any(
        not isinstance(flag, str) for flag in active_flags
    ):
        return False, None
    if not active_flags:
        return True, "in_progress"
    if active_flags == ["waitingOnApproval"]:
        return True, "needs_approval"
    if active_flags == ["waitingOnUserInput"]:
        return True, "needs_input"
    return True, "waiting"


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

    health_timer: QTimer
    thread_timer: QTimer
    selected_status_timer: QTimer
    _history_refresh_timer: QTimer
    readiness_timer: QTimer
    bridge_start_retry_timer: QTimer
    usage_initial_timer: QTimer
    usage_retry_timer: QTimer
    usage_poll_timer: QTimer
    turn_usage_snapshot_timer: QTimer
    codex_update_auto_timer: QTimer
    stop_confirmation_timer: QTimer
    diagnostics_timer: QTimer
    exit_timer: QTimer
    signal_timer: QTimer

    bridge_status_label: QLabel
    app_server_status_label: QLabel
    stream_status_label: QLabel
    codex_status_label: QLabel
    runtime_status_label: QLabel
    tunnel_status_label: QLabel
    tunnel_client_status_label: QLabel
    config_status_label: QLabel
    overall_status_label: QLabel
    overall_detail_label: QLabel
    usage_status_label: UsageShortcutLabel
    codex_update_banner_label: QLabel
    status_button: QPushButton
    diagnostics_toggle_button: QPushButton
    diagnostics_clear_button: QPushButton
    retry_now_button: QPushButton
    restart_codexbridge_button: QPushButton
    advanced_toggle_button: QPushButton
    start_bridge_button: QPushButton
    stop_bridge_button: QPushButton
    restart_bridge_button: QPushButton
    start_tunnel_button: QPushButton
    stop_tunnel_button: QPushButton
    restart_tunnel_button: QPushButton
    usage_detail_label: QLabel
    usage_history_button: QPushButton
    codex_latest_label: QLabel
    codex_update_status_label: QLabel
    codex_update_message_label: QLabel
    codex_update_check_button: QPushButton
    codex_update_button: QPushButton
    status_refresh_button: QPushButton
    status_close_button: QPushButton
    status_dialog: QDialog
    appearance_button: QToolButton
    settings_button: QToolButton
    settings_menu: QMenu
    _appearance_menu: QMenu
    _settings_actions: dict[str, QAction]
    _apply_pending_settings_action: QAction
    notification_settings_button: QToolButton
    _appearance_actions: dict[str, QAction]
    _appearance_controller: AppearanceController
    _appearance_settings: QSettings
    _settings_controller: SettingsActionController
    _project_folder_actions_controller: ProjectFolderActionsController
    advanced_controls: QWidget
    thread_pane: ThreadListPane
    history_pane: HistoryPane
    composer_widget: QWidget
    activity_pane: ActivityPane
    splitter: QSplitter
    diagnostics_pane: QWidget
    diagnostics_title_label: QLabel
    diagnostics_text: QPlainTextEdit
    bottom_status_label: QLabel

    def __init__(
        self,
        config: ConsoleConfig,
        parent: QWidget | None = None,
        *,
        api_client: Any | None = None,
        codex_probe: Any | None = None,
        codex_update_probe: Any | None = None,
        runtime_launcher: Any | None = None,
        diagnostics_reader: Any | None = None,
        tunnel_supervisor: Any | None = None,
        tray_factory: Callable[[QWidget], Any] | None = None,
        tray_available: bool | None = None,
        quit_application: Callable[[], None] | None = None,
        codex_uri_opener: Callable[[str], bool] | None = None,
        usage_history_path: Path | None = None,
        appearance_settings: QSettings | None = None,
        appearance_system_scheme_getter: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._pending_config: ConsoleConfig | None = None
        self._restart_after_settings_apply = False
        self._usage_history_path = (
            default_usage_history_path() if usage_history_path is None else usage_history_path
        )
        self._usage_history_window: UsageHistoryWindow | None = None
        self._client = api_client or ApiClient(config.base_url, self)
        self._codex_probe = codex_probe if codex_probe is not None else self._new_codex_probe()
        self._codex_update_probe = (
            codex_update_probe if codex_update_probe is not None else CodexUpdateProbe(parent=self)
        )
        self._launcher = (
            runtime_launcher if runtime_launcher is not None else BridgeRuntimeLauncher()
        )
        self._diagnostics_reader = (
            diagnostics_reader if diagnostics_reader is not None else DiagnosticsReader()
        )
        self._tunnel_resolution_error: str | None = None
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
        self._codex_uri_opener = codex_uri_opener or open_codex_uri
        self.tray_icon: Any | None = None
        self.tray_menu: QMenu | None = None
        self._tray_actions: dict[str, QAction] = {}
        self._tray_usable = False
        self._tray_notified = False
        self._closing = False
        self._exit_finished = False
        self._selection_generation = 0
        self._selected_thread_id: str | None = None
        self._history_timeline = HistoryTimelineState()
        self._active_thread_ids: set[str] = set()
        self._thread_turn_statuses: dict[str, str] = {}
        self._runtime_state = "unavailable"
        self._usage_presentation = UsagePresentationController(self)
        self._usage_snapshot_store = UsageSnapshotStore(self)
        self._usage_requests = UsageRequestCoordinator(self)
        self._codex_resolution: CodexResolution | None = None
        self._bridge_seen_ready = False
        self._health_observed = False
        self._status_observed = False
        self._health_ok = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._bridge_runtime_observer = BridgeRuntimeObserver(self)
        self._launch_in_progress = False
        self._detached_launch_started = False
        self._detached_pid: int | None = None
        self._control_token: str | None = None
        self._approval_control_pending = False
        self._launch_generation = 0
        self._bridge_transition = False
        self._stop_outcome_uncertain = False
        self._exit_shutdown_pending = False
        self._exit_deadline = 0.0
        self._sigint_requested = False
        self._tunnel_state = getattr(self._tunnel, "state", "unavailable")

        self.setWindowTitle("CodexBridge Console")
        self._build_ui()
        self.thread_pane.set_thread_statuses(self._thread_turn_statuses)
        install_appearance(self, appearance_settings, appearance_system_scheme_getter)
        self._settings_controller = SettingsActionController(self)
        self._install_settings_menu()
        self._notification_controller = NotificationController(
            self._appearance_settings,
            show_message=self._show_notification,
            is_window_active=self.isActiveWindow,
            activate_console=self._show_console,
            play_sound=self._play_notification_sound,
        )
        self._global_stream_generation = 1
        self._global_stream_retries = 0
        self._global_stream_retry_initial_ms = 1_000
        self._global_stream_retry_max_ms = 30_000
        self._global_stream_retry_timer = QTimer(self)
        self._global_stream_retry_timer.setSingleShot(True)
        self._global_stream_retry_timer.setInterval(1500)
        self._global_stream_retry_timer.timeout.connect(self._retry_global_stream)
        self._set_initial_size()
        self._build_timers()
        self._thread_interactions = ThreadInteractionController(self)
        self._project_folder_actions_controller = ProjectFolderActionsController(self)
        self._bulk_thread_naming = BulkThreadNamingController(self)
        self._diagnostics_panel = DiagnosticsPanelController(self)
        self._connect_client()
        self._codex_update_controller = CodexUpdateController(self)
        self._bridge_managed_retry_coordinator = BridgeManagedRetryCoordinator(
            self.bridge_start_retry_timer,
            get_is_closing=lambda: self._closing,
            can_start_bridge=self._can_start_bridge,
            start_bridge_managed=lambda: self._start_bridge(managed=True),
            update_start_button=self._update_start_button,
        )
        self._bridge_stop_confirmation = BridgeStopConfirmationCoordinator(
            self.stop_confirmation_timer,
            on_confirmed=self._on_stop_confirmation_confirmed,
            on_timed_out=self._on_stop_confirmation_timed_out,
            request_health=self._request_health,
            clock=monotonic,
        )
        self._bridge_transition_coordinator = BridgeTransitionCoordinator(
            get_context=lambda: BridgeTransitionContext(
                self._closing, self._control_token, self._stop_confirmation_active
            ),
            stop_tunnel=lambda on_finished: self._tunnel.stop(on_finished=on_finished),
            post_shutdown=lambda token: self._client.post_control_shutdown(
                token, key="control:shutdown"
            ),
            on_transition_started=self._on_bridge_transition_started,
            on_request_failure=lambda: self._apply_control_failure(
                "control:shutdown", "Bridge control request failed"
            ),
        )
        self._bridge_readiness = BridgeReadinessCoordinator(
            self.readiness_timer,
            self._client,
            self.bridge_status_label,
            self.app_server_status_label,
            is_closing=lambda: self._closing,
            is_launch_in_progress=lambda: self._launch_in_progress,
            on_ready=self._finish_launch_if_ready,
            on_timeout=self._on_launch_readiness_timeout,
        )
        self._history_requests = HistoryRequestCoordinator(
            self._client,
            self._history_refresh_timer,
        )
        self._history_stream = HistoryStreamCoordinator(
            self.activity_pane,
            self.stream_status_label,
            self.bottom_status_label,
            context_provider=self._history_stream_context,
            request_selected_status=self._request_selected_status,
            schedule_selected_history_refresh=self._schedule_selected_history_refresh,
            set_thread_active=self._set_thread_active,
            update_thread_activity=self._update_thread_activity,
            schedule_turn_usage_snapshot=self._schedule_turn_usage_snapshot,
            sync_approval_controls=self._sync_approval_controls,
            start_stream=self._client.start_stream,
        )
        self._start_global_stream()
        self._composer_controller = create_composer_controller(self, self._client)
        self._connect_runtime()
        self._tray_controller = TrayController(self)
        self._build_tray()
        self._runtime_ui = RuntimeUiPresenter(self)
        self._update_tunnel_client_status()
        self._sync_tunnel_controls()
        self._sync_bridge_controls()
        self._set_unavailable_state()
        self._codex_probe.start()
        self.refresh()

    @property
    def runtime_state(self) -> str:
        return self._runtime_state

    @property
    def _pending_rename_names(self) -> dict[str, str]:
        return self._thread_interactions._pending_rename_names

    @property
    def _thread_copy_sequence(self) -> int:
        return self._thread_interactions._thread_copy_sequence

    @property
    def _pending_thread_copies(self) -> dict[str, tuple[str, list[TimelineEntry]]]:
        return self._thread_interactions._pending_thread_copies

    @property
    def _managed_start_active(self) -> bool:
        return self._bridge_managed_retry_coordinator.managed_active

    @property
    def _bridge_start_retry_index(self) -> int:
        return self._bridge_managed_retry_coordinator.retry_index

    @property
    def _bridge_start_retry_exhausted(self) -> bool:
        return self._bridge_managed_retry_coordinator.retry_exhausted

    @property
    def _bridge_loss_count(self) -> int:
        return self._bridge_runtime_observer.loss_count

    @property
    def _stop_confirmation_active(self) -> bool:
        return self._bridge_stop_confirmation.active

    @property
    def _stop_confirmation_deadline(self) -> float:
        return self._bridge_stop_confirmation.deadline

    @_stop_confirmation_deadline.setter
    def _stop_confirmation_deadline(self, value: float) -> None:
        self._bridge_stop_confirmation.deadline = value

    @property
    def _readiness_deadline(self) -> float:
        return self._bridge_readiness.deadline

    @_readiness_deadline.setter
    def _readiness_deadline(self, value: float) -> None:
        self._bridge_readiness.deadline = value

    @property
    def _history_refresh_pending(self) -> bool:
        return self._history_requests.pending_live

    def _history_request_context(self) -> HistoryRequestContext:
        return HistoryRequestContext(
            generation=self._selection_generation,
            selected_thread_id=self._selected_thread_id,
            next_cursor=self._next_cursor,
            closing=self._closing,
        )

    def _history_stream_context(self) -> HistoryStreamContext:
        return HistoryStreamContext(
            generation=self._selection_generation,
            thread_id=self._selected_thread_id,
            closing=self._closing,
        )

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
        profile_file: str | None = None
        profile_settings = self._config.tunnel_profile_settings
        has_managed_tunnel_profile = any(key != "config_version" for key in profile_settings)
        if (
            executable is not None
            and self._config.tunnel_profile_file is not None
            and has_managed_tunnel_profile
        ):
            configured_profile_path = Path(self._config.tunnel_profile_file)
            try:
                generated = generate_tunnel_profile(
                    profile_settings,
                    profile_dir=configured_profile_path.parent,
                    profile_name=configured_profile_path.stem,
                    client_executable=executable,
                )
            except TunnelProfileError as exc:
                self._tunnel_resolution_error = str(exc)
                executable = None
            else:
                profile_file = os.fspath(generated.path)
        return TunnelSupervisor(
            executable=executable,
            profile=self._config.tunnel_profile,
            profile_file=profile_file,
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
        build_main_window_ui(self)

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
        global_activity_signal = getattr(self._client, "global_activity_received", None)
        if global_activity_signal is not None:
            global_activity_signal.connect(self._apply_global_activity)
        global_state_signal = getattr(self._client, "global_stream_state_changed", None)
        if global_state_signal is not None:
            global_state_signal.connect(self._apply_global_stream_state)
        self._client.control_succeeded.connect(self._apply_control_success)
        self._client.control_failed.connect(self._apply_control_failure)
        self.activity_pane.approval_decision_requested.connect(self._resolve_approval)
        self.thread_pane.refresh_requested.connect(self.refresh)
        self.thread_pane.thread_selected.connect(self.select_thread)
        self.thread_pane.thread_rename_requested.connect(self._rename_thread)
        self.thread_pane.thread_open_requested.connect(self._open_thread_in_codex)
        self.thread_pane.thread_copy_requested.connect(self._copy_thread_content)
        self.thread_pane.new_thread_for_folder_requested.connect(self._new_thread_for_folder)
        self.thread_pane.folder_explorer_requested.connect(self._open_project_folder)
        self.thread_pane.folder_terminal_requested.connect(self._open_project_terminal)
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
        self.notification_settings_button.clicked.connect(self._show_notification_settings)
        self.usage_status_label.activated.connect(self._create_or_show_usage_history_window)
        self.usage_history_button.clicked.connect(self._create_or_show_usage_history_window)
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

    def _show_notification_settings(self) -> None:
        dialog = NotificationSettingsDialog(self._appearance_settings, self)
        dialog.exec()

    def _install_settings_menu(self) -> None:
        self._settings_controller.install_settings_menu()

    def _active_config_path(self) -> Path | None:
        return self._settings_controller.active_config_path()

    def _open_settings_file(self) -> None:
        self._settings_controller.open_settings_file()

    def _open_settings_folder(self) -> None:
        self._settings_controller.open_settings_folder()

    def _settings_change_names(self, candidate: ConsoleConfig) -> tuple[str, ...]:
        return self._settings_controller.settings_change_names(candidate)

    def _reload_configuration(self) -> None:
        self._settings_controller.reload_configuration()

    def _apply_pending_configuration(self) -> None:
        self._settings_controller.apply_pending_configuration()

    def _show_notification(self, title: str, body: str) -> None:
        if self.tray_icon is None:
            return
        show_message = getattr(self.tray_icon, "showMessage", None)
        if callable(show_message):
            show_message(title, body, QSystemTrayIcon.MessageIcon.Information, 5000)

    @staticmethod
    def _play_notification_sound() -> None:
        if sys.platform != "win32":
            QApplication.beep()

    def _start_global_stream(self) -> None:
        start = getattr(self._client, "start_global_stream", None)
        if callable(start) and not self._closing:
            start(self._global_stream_generation)

    def _apply_global_activity(self, generation: int, payload: object) -> None:
        if self._closing or generation != self._global_stream_generation:
            return
        self._update_thread_status_from_activity(payload)
        self._notification_controller.handle_activity(payload)

    def _update_thread_status_from_activity(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        thread_id = payload.get("thread_id")
        activity_type = payload.get("type")
        status = payload.get("status")
        if not isinstance(thread_id, str) or not isinstance(activity_type, str):
            return
        if activity_type == "approval_requested" and status == "requested":
            self._update_thread_activity(thread_id, "needs_approval")
        elif activity_type == "user_input_requested" and status == "requested":
            self._update_thread_activity(thread_id, "needs_input")
        elif activity_type == "turn_started" and status in ACTIVE_TURN_STATES:
            self._update_thread_activity(thread_id, status)
        elif activity_type in {"turn_completed", "turn_failed", "turn_interrupted", "error"} and (
            status in TERMINAL_TURN_STATES
        ):
            self._update_thread_activity(thread_id, status)

    def _apply_global_stream_state(self, generation: int, state: str) -> None:
        if generation != self._global_stream_generation:
            return
        if state == "connected":
            self._global_stream_retries = 0
            self._global_stream_retry_timer.stop()
        elif state == "disconnected" and not self._closing:
            delay_ms = min(
                self._global_stream_retry_initial_ms * (2**self._global_stream_retries),
                self._global_stream_retry_max_ms,
            )
            self._global_stream_retry_timer.start(delay_ms)

    def _retry_global_stream(self) -> None:
        if self._closing:
            return
        self._global_stream_retries += 1
        self._start_global_stream()

    def _create_or_show_usage_history_window(self) -> None:
        if self._usage_history_window is None:
            self._usage_history_window = UsageHistoryWindow(
                self._usage_history_path,
                appearance=self._appearance_controller.theme,
                settings=self._appearance_settings,
                parent=self,
            )
        else:
            self._usage_history_window.refresh(rolling=True)
        self._usage_history_window.show()
        self._usage_history_window.raise_()
        self._usage_history_window.activateWindow()

    def _refresh_status(self) -> None:
        self._request_usage(force=True)
        self.refresh()

    def _build_tray(self) -> None:
        self._tray_controller.build()

    def _show_console(self) -> None:
        self._tray_controller.show_console()

    def _hide_console(self) -> None:
        self._tray_controller.hide_console()

    def _on_tray_activated(self, reason: object) -> None:
        self._tray_controller.on_activated(reason)

    def _build_timers(self) -> None:
        build_console_timers(self, exit_timeout_seconds=_EXIT_TIMEOUT_SECONDS)

    def toggle_diagnostics(self) -> None:
        self._diagnostics_panel.toggle_diagnostics()

    def _set_diagnostics_visible(self, visible: bool) -> None:
        self._diagnostics_panel._set_diagnostics_visible(visible)

    def _on_diagnostics_timeout(self) -> None:
        self._diagnostics_panel._on_diagnostics_timeout()

    def _append_diagnostics_lines(self, lines: list[str]) -> None:
        self._diagnostics_panel._append_diagnostics_lines(lines)

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
        self._runtime_ui._update_tunnel_client_status()

    def _apply_tunnel_message(self, message: str) -> None:
        if not self._closing:
            self.bottom_status_label.setText(message)

    def _apply_tunnel_controls(self, start: bool, stop: bool, restart: bool) -> None:
        self._runtime_ui._apply_tunnel_controls(start, stop, restart)

    def _tunnel_is_transitioning(self) -> bool:
        return getattr(self._tunnel, "state", "unavailable") in {
            "checking",
            "starting",
            "stopping",
        }

    def _update_bridge_controls(self) -> None:
        self._runtime_ui._update_bridge_controls()

    def _sync_bridge_controls(self) -> None:
        self._runtime_ui._sync_bridge_controls()

    def _sync_tunnel_controls(self) -> None:
        self._runtime_ui._sync_tunnel_controls()

    def _start_tunnel(self) -> None:
        self._tunnel.start()

    def _stop_tunnel(self) -> None:
        self._tunnel.stop()

    def _restart_tunnel(self) -> None:
        self._tunnel.restart()

    def _sync_empty_state(self) -> None:
        self._runtime_ui._sync_empty_state()

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
        self._codex_update_controller.sync_codex_update_controls()

    def _schedule_codex_update_auto_check(self) -> None:
        self._codex_update_controller.schedule_codex_update_auto_check()

    def _on_codex_update_auto_timeout(self) -> None:
        self._codex_update_controller.on_codex_update_auto_timeout()

    def _apply_codex_update_busy(self, busy: bool) -> None:
        self._codex_update_controller.apply_codex_update_busy(busy)

    def _check_codex_updates(self) -> None:
        self._codex_update_controller.check_codex_updates()

    def _apply_codex_update_check(self, payload: object) -> None:
        self._codex_update_controller.apply_codex_update_check(payload)

    def _apply_codex_update_check_error(self, message: str) -> None:
        self._codex_update_controller.apply_codex_update_check_error(message)

    def _update_codex(self) -> None:
        self._codex_update_controller.update_codex()

    def _apply_codex_update_success(self) -> None:
        self._codex_update_controller.apply_codex_update_success()

    def _apply_codex_update_failure(self, message: str) -> None:
        self._codex_update_controller.apply_codex_update_failure(message)

    @property
    def _codex_update_auto_check_started(self) -> bool:
        return self._codex_update_controller.auto_check_started

    def _set_runtime_state(self, state: str, *, label: str | None = None) -> None:
        self._runtime_state = state
        self.runtime_status_label.setText(label or _RUNTIME_LABELS.get(state, f"Runtime: {state}"))
        self._composer_controller.sync_bridge_ownership()
        self._sync_approval_controls()
        self._update_start_button()
        self._update_bridge_controls()
        self._sync_overall_status()

    def _request_usage(self, *, force: bool = False) -> None:
        self._usage_requests.request_usage(force=force)

    @property
    def _usage_ready(self) -> bool:
        return self._bridge_ready and self._app_server_ready

    @property
    def _usage(self) -> CodexUsage:
        return self._usage_presentation.usage

    @property
    def _usage_refresh_error(self) -> str | None:
        return self._usage_presentation.refresh_error

    @property
    def _usage_sequence_active(self) -> bool:
        return self._usage_requests.sequence_active

    @_usage_sequence_active.setter
    def _usage_sequence_active(self, value: bool) -> None:
        self._usage_requests.sequence_active = value

    @property
    def _usage_attempts(self) -> int:
        return self._usage_requests.attempts

    @_usage_attempts.setter
    def _usage_attempts(self, value: int) -> None:
        self._usage_requests.attempts = value

    @property
    def _usage_request_in_flight(self) -> bool:
        return self._usage_requests.request_in_flight

    @_usage_request_in_flight.setter
    def _usage_request_in_flight(self, value: bool) -> None:
        self._usage_requests.request_in_flight = value

    @property
    def _usage_request_mode(self) -> str | None:
        return self._usage_requests.request_mode

    @_usage_request_mode.setter
    def _usage_request_mode(self, value: str | None) -> None:
        self._usage_requests.request_mode = value

    @property
    def _usage_snapshots(self) -> OrderedDict[tuple[str, str], CodexUsageSnapshot | None]:
        return self._usage_snapshot_store.usage_snapshots

    @property
    def _pending_usage_snapshot_turns(self) -> set[tuple[str, str]]:
        return self._usage_snapshot_store.pending_turns

    @property
    def _usage_snapshot_request_turns(self) -> set[tuple[str, str]]:
        return self._usage_snapshot_store.request_turns

    def _begin_usage_sequence(self) -> None:
        self._usage_requests.begin_sequence()

    def _invalidate_usage_sequence(self) -> None:
        self._usage_requests.invalidate_sequence()

    def _start_usage_request(self, mode: str) -> bool:
        return self._usage_requests.start_request(mode)

    def _on_usage_initial_timeout(self) -> None:
        self._usage_requests.initial_timeout()

    def _on_usage_retry_timeout(self) -> None:
        self._usage_requests.retry_timeout()

    def _on_usage_poll_timeout(self) -> None:
        self._usage_requests.poll_timeout()

    def _start_usage_snapshot_timer(self) -> None:
        self._usage_snapshot_store.start_timer()

    def _schedule_turn_usage_snapshot(self, thread_id: str, turn_id: str) -> None:
        self._usage_snapshot_store.schedule_turn(thread_id, turn_id)

    def _on_turn_usage_snapshot_timeout(self) -> None:
        self._usage_snapshot_store.on_timeout()

    def _finish_turn_usage_snapshot(
        self,
        candidates: set[tuple[str, str]],
        usage: CodexUsage,
    ) -> None:
        self._usage_snapshot_store.finish(candidates, usage)

    def _apply_usage(self, payload: object) -> None:
        self._usage_presentation.apply_usage(payload)

    def _apply_usage_failure(self, message: str) -> None:
        self._usage_presentation.apply_usage_failure(message)

    def _render_usage_status(self) -> None:
        self._usage_presentation.render_usage_status()

    def _record_usage_history(self) -> None:
        self._usage_presentation.record_usage_history(
            captured_at_epoch=time(),
            record_sample=record_usage_sample,
        )

    def _sync_overall_status(self) -> None:
        self._runtime_ui._sync_overall_status()

    def _update_start_button(self) -> None:
        self._runtime_ui._update_start_button()

    def _update_lifecycle_controls(self) -> None:
        self._runtime_ui._update_lifecycle_controls()

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
        self._bridge_managed_retry_coordinator.maybe_auto_start_bridge()

    def _retry_now(self) -> None:
        if self._closing or self._launch_in_progress or self._bridge_transition:
            return
        bridge_ready = self._health_ok and self._bridge_ready and self._app_server_ready
        if bridge_ready and self._tunnel_state != "ready":
            retry_now = getattr(self._tunnel, "retry_now", None)
            if callable(retry_now):
                retry_now()
            return
        self._bridge_managed_retry_coordinator.reset_for_retry_now()
        if self._can_start_bridge():
            self._start_bridge(managed=True)

    def _restart_codexbridge(self) -> None:
        self._restart_bridge()

    def _schedule_managed_bridge_retry(self) -> None:
        self._bridge_managed_retry_coordinator.schedule_retry()

    def _on_bridge_start_retry_timeout(self) -> None:
        self._bridge_managed_retry_coordinator.on_retry_timeout()

    def _start_bridge(self, *, managed: bool = False) -> None:
        resolution = self._codex_resolution
        if resolution is None or not self._can_start_bridge():
            return
        if managed:
            self._bridge_managed_retry_coordinator.activate()
        self._bridge_transition_coordinator.reset_for_launch()
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
            if self._config.config_mode == "managed" and self._config.config_path is not None:
                result = self._launcher.launch(
                    codex_executable=codex_executable,
                    ui_port=self._config.port,
                    control_token=control_token,
                    allowed_roots=self._config.allowed_roots,
                    managed_config_path=self._config.config_path,
                )
            else:
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
        self._bridge_readiness.begin()

    def _request_launch_readiness(self) -> None:
        self._bridge_readiness.request()

    def _on_readiness_tick(self) -> None:
        self._bridge_readiness.tick()

    def _apply_launch_health(self, payload: object) -> None:
        self._bridge_readiness.apply_health(payload)

    def _apply_launch_status(self, payload: object) -> None:
        self._bridge_readiness.apply_status(payload)

    def _on_launch_readiness_timeout(self) -> None:
        self._launch_in_progress = False
        self._bridge_transition = False
        if self._managed_start_active:
            self._set_runtime_state("launch_failed")
        else:
            self._set_runtime_state("launch_timed_out")
        self.bottom_status_label.setText("Bridge launch timed out; it may still be starting")
        self._schedule_managed_bridge_retry()

    def _finish_launch_if_ready(self) -> None:
        if not self._bridge_readiness.ready():
            return
        self.readiness_timer.stop()
        self._launch_in_progress = False
        self._health_ok = True
        self._bridge_ready = True
        self._app_server_ready = True
        self._bridge_seen_ready = True
        self._bridge_runtime_observer.reset_loss_count()
        self._bridge_managed_retry_coordinator.reset_after_ready()
        self._bridge_transition = False
        self._set_runtime_state("console_started", label="Runtime: started by Console")
        self.bottom_status_label.setText("Bridge started by Console")
        self._tunnel.set_bridge_ready(True)
        self._begin_usage_sequence()
        self._restore_restarted_tunnel()

    def _restore_restarted_tunnel(self) -> None:
        if self._bridge_transition_coordinator.consume_restart_tunnel_flag():
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

    def _begin_bridge_transition(
        self, action: Literal["stop", "restart"], *, tunnel_running: bool
    ) -> None:
        self._bridge_transition_coordinator.begin_transition(action, tunnel_running=tunnel_running)

    def _on_bridge_transition_started(self, action: str) -> None:
        self._bridge_transition = True
        self._sync_tunnel_controls()
        self._set_runtime_state("restarting" if action == "restart" else "stopping")

    def _on_tunnel_stopped_for_bridge(self) -> None:
        self._bridge_transition_coordinator.on_tunnel_stopped()

    def _request_bridge_shutdown(self, *, allow_closing: bool = False) -> None:
        self._bridge_transition_coordinator.request_shutdown(allow_closing=allow_closing)

    def _apply_control_success(self, key: str) -> None:
        if key == "control:approval" and self._approval_control_pending:
            self._approval_control_pending = False
            self.activity_pane.set_approval_busy(False)
            self.activity_pane.set_approval_feedback("Approval sent. Refreshing status…")
            self._request_selected_status()
            return
        if key != "control:shutdown" or not (
            self._bridge_transition_coordinator.acknowledge_shutdown_success()
        ):
            return
        self._bridge_stop_confirmation.begin()
        self.bottom_status_label.setText("Bridge shutdown requested")
        self._request_health()

    def _apply_control_failure(self, key: str, message: str) -> None:
        if key == "control:approval" and self._approval_control_pending:
            self._approval_control_pending = False
            self.activity_pane.set_approval_busy(False)
            if "already resolved" in message.casefold():
                self.activity_pane.mark_approval_resolved()
                self.activity_pane.set_approval_feedback(
                    "Approval already resolved. Refreshing status…"
                )
                self._request_selected_status()
            else:
                self.activity_pane.set_approval_feedback("Approval request failed")
            return
        if key != "control:shutdown":
            return
        self._bridge_transition_coordinator.mark_shutdown_failed()
        self._bridge_stop_confirmation.cancel()
        if self._exit_shutdown_pending:
            self._finish_exit()
            return
        self._bridge_transition = False
        self._set_runtime_state("control_failed")
        self.bottom_status_label.setText("Bridge control request failed")

    def _maybe_finish_stop_confirmation(self) -> None:
        self._bridge_stop_confirmation.maybe_finish()

    def _on_stop_confirmation_tick(self) -> None:
        self._bridge_stop_confirmation.tick()

    def _on_stop_confirmation_confirmed(self) -> None:
        if self._exit_shutdown_pending:
            self._finish_exit()
            return
        self._confirm_bridge_stopped()

    def _on_stop_confirmation_timed_out(self) -> None:
        if self._exit_shutdown_pending:
            self._finish_exit()
            return
        self._bridge_transition = False
        self._stop_outcome_uncertain = True
        self._health_observed = False
        self._status_observed = False
        self._set_runtime_state("stop_timed_out")
        self.bottom_status_label.setText("Bridge stop timed out")

    def _confirm_bridge_stopped(self) -> None:
        action = self._bridge_transition_coordinator.pending_action
        stop_outcome_was_uncertain = self._stop_outcome_uncertain
        self._bridge_stop_confirmation.complete()
        self._stop_outcome_uncertain = False
        self._bridge_ready = False
        self._app_server_ready = False
        self._health_ok = False
        self._control_token = None
        self._detached_pid = None
        self._detached_launch_started = False
        self._bridge_seen_ready = False
        self._bridge_runtime_observer.reset_loss_count()
        self._launch_in_progress = False
        self._tunnel.set_bridge_ready(False)
        self._bridge_transition_coordinator.clear_pending_action()
        if action == "restart" and not stop_outcome_was_uncertain:
            resolution = self._codex_resolution
            if resolution is None:
                self._bridge_transition = False
                self._bridge_transition_coordinator.clear_restart_flag()
                self._set_runtime_state("launch_failed")
                self.bottom_status_label.setText("Bridge relaunch failed")
                return
            self._launch_bridge(resolution.path)
            return
        self._bridge_transition_coordinator.clear_restart_flag()
        self._bridge_transition = False
        self._sync_tunnel_controls()
        self.bridge_status_label.setText("Bridge: disconnected")
        self.app_server_status_label.setText("App Server: failed")
        self._set_runtime_state("stopped")
        self.bottom_status_label.setText("Bridge stopped")

    def _apply_runtime_observation(self) -> None:
        self._bridge_runtime_observer.apply_runtime_observation()

    def _record_bridge_status_observation(self, available: bool) -> None:
        self._bridge_runtime_observer.record_bridge_status_observation(available)

    def _recover_bridge_after_loss(self) -> None:
        self._bridge_runtime_observer.recover_bridge_after_loss()

    def _request_health(self) -> None:
        self._client.get_json("/healthz", key="health")
        self._request_bridge_status()

    def _request_bridge_status(self) -> None:
        self._client.get_json("/ui-api/status", key="bridge-status")

    def _request_threads(self) -> None:
        self._client.get_json("/ui-api/threads", key="threads", query={"limit": 100})

    def _rename_thread(self, thread_id: str) -> None:
        self._thread_interactions._rename_thread(thread_id, get_text=QInputDialog.getText)

    def _open_thread_in_codex(self, thread_id: str) -> None:
        self._thread_interactions._open_thread_in_codex(thread_id)

    def _copy_thread_content(self, thread_id: str) -> None:
        self._thread_interactions._copy_thread_content(thread_id)

    def _validated_project_folder(self, cwd: str) -> str | None:
        return self._project_folder_actions_controller.validated_project_folder(cwd)

    def _new_thread_for_folder(self, cwd: str) -> None:
        self._project_folder_actions_controller.new_thread_for_folder(cwd)

    def _open_project_folder(self, cwd: str) -> None:
        self._project_folder_actions_controller.open_project_folder(cwd)

    def _open_project_terminal(self, cwd: str) -> None:
        self._project_folder_actions_controller.open_project_terminal(cwd)

    def _request_thread_copy_page(
        self, key: str, thread_id: str, cursor: str | None = None
    ) -> bool:
        return self._thread_interactions._request_thread_copy_page(key, thread_id, cursor)

    def _apply_thread_copy_page(self, key: str, payload: object) -> None:
        self._thread_interactions._apply_thread_copy_page(
            key, payload, copy_to_clipboard=copy_to_clipboard
        )

    def _thread_path(self, suffix: str = "") -> str:
        return self._thread_interactions._thread_path(suffix)

    def _request_snapshot(self, generation: int) -> None:
        self._thread_interactions._request_snapshot(generation)

    def _request_history_snapshot(self, generation: int) -> None:
        self._history_requests.request_snapshot(generation, self._history_request_context())

    def _request_selected_status(self) -> None:
        self._thread_interactions._request_selected_status()

    def _schedule_selected_history_refresh(self) -> None:
        self._history_requests.schedule_selected_history_refresh(self._history_request_context())

    def _request_selected_history(self) -> None:
        self._history_requests.request_selected_history(self._history_request_context())

    def _finish_selected_history_request(self, key: str) -> None:
        self._history_requests.finish_request(key, self._history_request_context())

    @property
    def _timeline_entries(self) -> list[TimelineEntry]:
        return self._history_timeline.entries

    @_timeline_entries.setter
    def _timeline_entries(self, value: list[TimelineEntry]) -> None:
        self._history_timeline.entries = value

    @property
    def _turn_model_metadata(self) -> dict[str, Mapping[str, object]]:
        return self._history_timeline.turn_model_metadata

    @property
    def _turn_statuses(self) -> dict[str, str]:
        return self._history_timeline.turn_statuses

    @property
    def _next_cursor(self) -> str | None:
        return self._history_timeline.next_cursor

    def select_thread(self, thread_id: str | None) -> None:
        self._thread_interactions.select_thread(thread_id)

    @staticmethod
    def _selection_key(key: str) -> tuple[int, str] | None:
        return ThreadInteractionController._selection_key(key)

    def _is_current_selection(self, key: str) -> tuple[int, str] | None:
        return self._thread_interactions._is_current_selection(key)

    def apply_json_result(self, key: str, payload: object) -> None:
        if key in self._pending_thread_copies:
            self._apply_thread_copy_page(key, payload)
            return
        if self._bulk_thread_naming.handle_result(key, payload):
            return
        if key.startswith("rename:"):
            self._thread_interactions.apply_rename_success(key)
            return
        if self._bridge_stop_confirmation.active and key in {"health", "bridge-status"}:
            if key == "health":
                self._health_observed = True
                self._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
                self._bridge_stop_confirmation.observe_health(True)
            elif isinstance(payload, Mapping):
                self._status_observed = True
                self._bridge_ready = payload.get("bridge") in {"ready", "connected"}
                self._app_server_ready = payload.get("app_server") in {"ready", "connected"}
                self._bridge_stop_confirmation.observe_status(True)
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
            self._usage_requests.apply_result(payload)
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
                thread_ids = {
                    thread_id
                    for thread in threads
                    if isinstance((thread_id := thread.get("id")), str)
                }
                statuses = {
                    thread_id: state
                    for thread_id, state in self._thread_turn_statuses.items()
                    if thread_id in thread_ids
                }
                for thread in threads:
                    thread_id = thread.get("id")
                    if not isinstance(thread_id, str):
                        continue
                    observed, state = _runtime_thread_status_observation(thread)
                    if observed:
                        if state is None:
                            statuses.pop(thread_id, None)
                        else:
                            statuses[thread_id] = state
                self._thread_turn_statuses = statuses
                self._active_thread_ids = {
                    thread_id
                    for thread_id, state in statuses.items()
                    if state in ACTIVE_TURN_STATES
                }
                self.thread_pane.set_threads(
                    threads,
                    thread_statuses=self._thread_turn_statuses,
                    project_names=read_local_project_names(),
                )
                if not threads:
                    self.thread_pane.set_empty_state("No threads found.")
                self._sync_empty_state()
            return

        selection = self._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        live_refresh = suffix.startswith("history:")
        if live_refresh:
            suffix = suffix.removeprefix("history:")
        if suffix == "detail":
            return
        if suffix == "turns":
            has_active_turn = self._history_timeline.apply_turns(payload, live_refresh=live_refresh)
            if has_active_turn is not None:
                if self._selected_thread_id is not None:
                    active_states = [
                        state
                        for state in self._turn_statuses.values()
                        if state in ACTIVE_TURN_STATES
                    ]
                    if active_states:
                        self._update_thread_activity(self._selected_thread_id, active_states[-1])
                    else:
                        self._update_thread_activity(self._selected_thread_id, "completed")
                self._render_timeline()
            self._finish_selected_history_request(key)
            return
        if suffix == "items":
            self._apply_items(payload, prepend=False, live_refresh=live_refresh)
            self._finish_selected_history_request(key)
            return
        if suffix == "status":
            self._apply_status(payload)
            return
        if suffix.startswith("older:"):
            self._apply_items(payload, prepend=True)
            self._finish_selected_history_request(key)

    def _apply_items(self, payload: object, *, prepend: bool, live_refresh: bool = False) -> None:
        if self._history_timeline.apply_items(payload, prepend=prepend, live_refresh=live_refresh):
            self._render_timeline(prepend=prepend)
            thread_id = self._selected_thread_id
            generation = self._selection_generation
            if thread_id is not None:
                self._composer_controller.apply_history_model_metadata(
                    thread_id,
                    generation,
                    tuple(
                        entry.turn_id
                        for entry in self._history_timeline.entries
                        if isinstance(entry.turn_id, str)
                    ),
                    self._history_timeline.turn_model_metadata,
                    turn_access_metadata=self._history_timeline.turn_access_metadata,
                )

    def _render_timeline(self, *, prepend: bool = False) -> None:
        self._history_timeline.render(
            self.history_pane,
            selected_thread_id=self._selected_thread_id,
            usage_snapshots=self._usage_snapshots,
            prepend=prepend,
        )

    def _apply_status(self, payload: object) -> None:
        self._history_stream.apply_status(payload)

    def _set_thread_active(self, thread_id: str, active: bool) -> None:
        if active:
            self._active_thread_ids.add(thread_id)
        else:
            self._active_thread_ids.discard(thread_id)

    def _update_thread_activity(self, thread_id: str, state: str) -> None:
        if state in ACTIVE_TURN_STATES:
            self._thread_turn_statuses[thread_id] = state
            self._active_thread_ids.add(thread_id)
        else:
            self._thread_turn_statuses.pop(thread_id, None)
            self._active_thread_ids.discard(thread_id)
        self.thread_pane.set_thread_statuses(self._thread_turn_statuses)

    def load_older(self) -> None:
        if self._selected_thread_id is None or self._next_cursor is None:
            return
        self._history_requests.request_older(self._history_request_context())

    def _apply_json_error(self, key: str, message: str) -> None:
        if key in self._pending_thread_copies:
            self._thread_interactions.discard_copy(key)
            return
        if self._bulk_thread_naming.handle_error(key, message):
            return
        if key.startswith("rename:"):
            self._thread_interactions.discard_rename(key)
            return
        if key == "launch:health":
            self._bridge_readiness.fail_health()
            return
        if key == "launch:status":
            self._bridge_readiness.fail_status()
            return
        if self._bridge_stop_confirmation.active and key in {"health", "bridge-status"}:
            if key == "health":
                self._health_observed = True
                self._health_ok = False
                self.bridge_status_label.setText("Bridge: disconnected")
                self._bridge_stop_confirmation.observe_health(False)
            else:
                self._status_observed = True
                self._bridge_ready = False
                self._app_server_ready = False
                self.app_server_status_label.setText("App Server: failed")
                self._bridge_stop_confirmation.observe_status(False)
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
            self._usage_requests.apply_error(message)
            return
        selection = self._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        live_refresh = suffix.startswith("history:")
        if live_refresh:
            suffix = suffix.removeprefix("history:")
        self.bottom_status_label.setText(message)
        if suffix in {"items", "turns"} or suffix.startswith("older:"):
            if not live_refresh:
                self.history_pane.set_error(message)
        if suffix == "status":
            self._history_stream.status_request_failed(message)
        self._finish_selected_history_request(key)

    def _apply_activity(self, generation: int, payload: object) -> None:
        self._history_stream.apply_activity(generation, payload)
        if (
            self._closing
            or generation != self._selection_generation
            or not isinstance(payload, Mapping)
            or payload.get("thread_id") != self._selected_thread_id
        ):
            return
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            return
        self._notification_controller.handle_activity(payload)
        self._update_thread_status_from_activity(payload)

    def _sync_approval_controls(self) -> None:
        self.activity_pane.set_control_available(
            self._runtime_state == "console_started"
            and self._control_token is not None
            and not self._closing
        )

    def _resolve_approval(self, request_id: object, decision: str) -> None:
        if (
            self._closing
            or self._approval_control_pending
            or self._runtime_state != "console_started"
            or self._control_token is None
            or request_id != self.activity_pane.approval_request_id
            or isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
            or decision not in {"accept", "acceptForSession", "decline", "cancel"}
        ):
            return
        self._approval_control_pending = True
        self.activity_pane.set_approval_busy(True)
        accepted = self._client.post_control_approval(
            self._control_token,
            request_id=request_id,
            decision=cast(ApprovalDecision, decision),
            key="control:approval",
        )
        if not accepted and self._approval_control_pending:
            self._apply_control_failure("control:approval", "Bridge control request failed")

    def _apply_stream_state(self, generation: int, state: str) -> None:
        self._history_stream.apply_stream_state(generation, state)

    def _schedule_reconnect(self, generation: int) -> None:
        self._history_stream.schedule_reconnect(generation)

    def _reconnect(self, generation: int) -> None:
        self._history_stream.reconnect(generation)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._tray_controller.handle_close_event(event)

    def _begin_exit(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._global_stream_retry_timer.stop()
        self._composer_controller.set_control_available(False)
        self.health_timer.stop()
        self.thread_timer.stop()
        self.selected_status_timer.stop()
        self._history_requests.reset()
        self.diagnostics_timer.stop()
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
        self.diagnostics_timer.stop()
        self.stop_confirmation_timer.stop()
        self._invalidate_usage_sequence()
        abort_update = getattr(self._codex_update_probe, "abort", None)
        if callable(abort_update):
            abort_update()
        self._client.abort_all()
        self._launcher.close()
        if self._usage_history_window is not None:
            self._usage_history_window.close()
        if self.tray_icon is not None:
            try:
                self.tray_icon.hide()
                delete_later = getattr(self.tray_icon, "deleteLater", None)
                if callable(delete_later):
                    delete_later()
            except Exception:
                pass
        if self._restart_after_settings_apply:
            try:
                result = QProcess.startDetached(sys.argv[0], list(sys.argv[1:]))
                restarted = (
                    bool(result[0]) if isinstance(result, tuple) and result else bool(result)
                )
            except Exception:
                restarted = False
            self._restart_after_settings_apply = False
            if not restarted:
                QMessageBox.warning(
                    self,
                    "Settings",
                    "Console could not restart. Launch it again to apply the validated settings.",
                )
        self._quit_application()
