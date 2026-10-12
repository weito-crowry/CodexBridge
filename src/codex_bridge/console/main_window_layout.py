from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .usage import (
    format_codex_usage,
    format_codex_usage_detail,
    format_codex_usage_tooltip,
)
from .usage_shortcut_label import UsageShortcutLabel
from .widgets import ActivityPane, HistoryPane, ThreadListPane

if TYPE_CHECKING:
    from .main_window import MainWindow


def build_main_window_ui(window: MainWindow) -> None:
    window.bridge_status_label = QLabel("Bridge: disconnected")
    window.app_server_status_label = QLabel("App Server: failed")
    window.stream_status_label = QLabel("Stream: idle")
    window.codex_status_label = QLabel("Codex: checking")
    window.runtime_status_label = QLabel("Runtime: unavailable")
    window.tunnel_status_label = QLabel("Tunnel: unavailable")
    window.tunnel_client_status_label = QLabel("Tunnel Client: unavailable")
    config_state = "ready" if window._config.roots_ready else "error"
    window.config_status_label = QLabel(
        f"Config: {config_state} · roots {window._config.roots_count}"
    )
    window.config_status_label.setToolTip(window._config.roots_error or "Allowed roots are ready")
    window.overall_status_label = QLabel("● Starting")
    window.overall_detail_label = QLabel("● Starting")
    window.usage_status_label = UsageShortcutLabel(format_codex_usage(window._usage))
    window.codex_update_banner_label = QLabel("")
    window.codex_update_banner_label.setVisible(False)
    window.status_button = QPushButton("Status")
    window.appearance_button = QToolButton()
    window.appearance_button.setText("Appearance")
    window.appearance_button.setObjectName("appearanceButton")
    window.appearance_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    window.diagnostics_toggle_button = QPushButton("Diagnostics")
    window.diagnostics_clear_button = QPushButton("Clear")
    window.retry_now_button = QPushButton("Retry now")
    window.restart_codexbridge_button = QPushButton("Restart CodexBridge")
    window.advanced_toggle_button = QPushButton("Advanced")
    window.advanced_toggle_button.setCheckable(True)
    window.start_bridge_button = QPushButton("Start Bridge")
    window.stop_bridge_button = QPushButton("Stop Bridge")
    window.restart_bridge_button = QPushButton("Restart Bridge")
    window.start_tunnel_button = QPushButton("Start Tunnel")
    window.stop_tunnel_button = QPushButton("Stop Tunnel")
    window.restart_tunnel_button = QPushButton("Restart Tunnel")
    window.start_bridge_button.setEnabled(False)
    window.stop_bridge_button.setEnabled(False)
    window.restart_bridge_button.setEnabled(False)
    window.stop_tunnel_button.setEnabled(False)
    window.restart_tunnel_button.setEnabled(False)
    window.retry_now_button.setEnabled(False)
    window.restart_codexbridge_button.setEnabled(False)
    for label in (
        window.bridge_status_label,
        window.app_server_status_label,
        window.stream_status_label,
        window.codex_status_label,
        window.runtime_status_label,
        window.tunnel_status_label,
        window.tunnel_client_status_label,
        window.config_status_label,
    ):
        label.setObjectName("detailStatus")
    window.overall_status_label.setObjectName("topStatus")
    window.overall_detail_label.setObjectName("detailStatus")
    window.usage_status_label.setObjectName("topStatus")
    window.usage_status_label.setProperty("usageShortcut", True)
    window.usage_status_label.setToolTip(format_codex_usage_tooltip(window._usage))

    window.usage_detail_label = QLabel(format_codex_usage_detail(window._usage))
    window.usage_detail_label.setWordWrap(True)
    window.usage_history_button = QPushButton("Show usage history")
    window.usage_history_button.setObjectName("showUsageHistory")
    window.usage_history_button.setFlat(True)
    window.usage_history_button.setCursor(Qt.CursorShape.PointingHandCursor)
    window.codex_latest_label = QLabel("Not checked")
    window.codex_update_status_label = QLabel("Not checked")
    window.codex_update_message_label = QLabel("")
    window.codex_update_message_label.setWordWrap(True)
    window.codex_update_check_button = QPushButton("Check for updates")
    window.codex_update_button = QPushButton("Update")
    window.codex_update_check_button.setEnabled(False)
    window.codex_update_button.setEnabled(False)
    codex_update_controls = QWidget(window)
    codex_update_controls_layout = QHBoxLayout(codex_update_controls)
    codex_update_controls_layout.setContentsMargins(0, 0, 0, 0)
    codex_update_controls_layout.addWidget(window.codex_update_check_button)
    codex_update_controls_layout.addWidget(window.codex_update_button)
    window.status_refresh_button = QPushButton("Refresh")
    window.status_close_button = QPushButton("Close")

    window.status_dialog = QDialog(window)
    window.status_dialog.setWindowTitle("CodexBridge Status")
    window.status_dialog.setModal(False)
    dialog_layout = QVBoxLayout(window.status_dialog)

    def add_section(title: str, rows: list[tuple[str, QWidget]]) -> None:
        group = QGroupBox(title, window.status_dialog)
        form = QFormLayout(group)
        for name, widget in rows:
            form.addRow(name, widget)
        dialog_layout.addWidget(group)

    add_section("Overall", [("State", window.overall_detail_label)])
    add_section(
        "Connection",
        [
            ("Bridge", window.bridge_status_label),
            ("App Server", window.app_server_status_label),
            ("Stream", window.stream_status_label),
        ],
    )
    add_section(
        "Codex",
        [
            ("Version", window.codex_status_label),
            ("Latest", window.codex_latest_label),
            ("Update", window.codex_update_status_label),
            ("", codex_update_controls),
            ("Status", window.codex_update_message_label),
            ("Runtime", window.runtime_status_label),
        ],
    )
    add_section(
        "Usage",
        [("Codex Usage", window.usage_detail_label), ("", window.usage_history_button)],
    )
    add_section(
        "Tunnel",
        [
            ("Tunnel", window.tunnel_status_label),
            ("Client", window.tunnel_client_status_label),
        ],
    )
    add_section("Configuration", [("Config", window.config_status_label)])

    lifecycle_controls = QHBoxLayout()
    lifecycle_controls.addWidget(window.retry_now_button)
    lifecycle_controls.addWidget(window.restart_codexbridge_button)
    dialog_layout.addLayout(lifecycle_controls)
    dialog_layout.addWidget(window.advanced_toggle_button)

    window.advanced_controls = QWidget(window.status_dialog)
    bridge_controls = QHBoxLayout(window.advanced_controls)
    bridge_controls.setContentsMargins(0, 0, 0, 0)
    for button in (
        window.start_bridge_button,
        window.stop_bridge_button,
        window.restart_bridge_button,
        window.start_tunnel_button,
        window.stop_tunnel_button,
        window.restart_tunnel_button,
    ):
        bridge_controls.addWidget(button)
    dialog_layout.addWidget(window.advanced_controls)
    window.advanced_controls.setVisible(False)
    dialog_buttons = QHBoxLayout()
    dialog_buttons.addStretch(1)
    dialog_buttons.addWidget(window.status_refresh_button)
    dialog_buttons.addWidget(window.status_close_button)
    dialog_layout.addLayout(dialog_buttons)
    window.status_dialog.adjustSize()

    status_bar = QHBoxLayout()
    status_bar.addWidget(window.overall_status_label)
    status_bar.addStretch(1)
    status_bar.addWidget(window.codex_update_banner_label)
    status_bar.addWidget(window.usage_status_label)
    status_bar.addWidget(window.status_button)
    status_bar.addWidget(window.appearance_button)

    window.thread_pane = ThreadListPane()
    window.history_pane = HistoryPane()
    window.activity_pane = ActivityPane()
    window.splitter = QSplitter(Qt.Orientation.Horizontal)
    window.splitter.addWidget(window.thread_pane)
    window.splitter.addWidget(window.history_pane)
    window.splitter.addWidget(window.activity_pane)
    window.splitter.setStretchFactor(0, 0)
    window.splitter.setStretchFactor(1, 1)
    window.splitter.setStretchFactor(2, 0)
    window.splitter.setSizes([300, 760, 340])

    window.diagnostics_pane = QWidget()
    diagnostics_layout = QVBoxLayout(window.diagnostics_pane)
    diagnostics_layout.setContentsMargins(0, 4, 0, 4)
    diagnostics_header = QHBoxLayout()
    window.diagnostics_title_label = QLabel("Diagnostics")
    diagnostics_header.addWidget(window.diagnostics_title_label)
    diagnostics_header.addStretch(1)
    diagnostics_header.addWidget(window.diagnostics_clear_button)
    diagnostics_layout.addLayout(diagnostics_header)
    window.diagnostics_text = QPlainTextEdit()
    window.diagnostics_text.setReadOnly(True)
    window.diagnostics_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    window.diagnostics_text.setMaximumBlockCount(2000)
    window.diagnostics_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    diagnostics_layout.addWidget(window.diagnostics_text)
    window.diagnostics_pane.setVisible(False)
    window.diagnostics_toggle_button.clicked.connect(lambda: window.toggle_diagnostics())
    window.diagnostics_clear_button.clicked.connect(window.diagnostics_text.clear)

    window.bottom_status_label = QLabel("Starting…")
    window.bottom_status_label.setObjectName("bottomStatus")
    window.bottom_status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    root = QWidget()
    layout = QVBoxLayout(root)
    layout.addLayout(status_bar)
    layout.addWidget(window.splitter, 1)
    layout.addWidget(window.diagnostics_pane)
    bottom_bar = QHBoxLayout()
    bottom_bar.addWidget(window.bottom_status_label, 1)
    bottom_bar.addWidget(window.diagnostics_toggle_button)
    layout.addLayout(bottom_bar)
    window.setCentralWidget(root)
    application = QApplication.instance()
    if isinstance(application, QApplication):
        application.setStyle("Fusion")
