from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
    ui_window = cast(Any, window)
    ui_window.bridge_status_label = QLabel("Bridge: disconnected")
    ui_window.app_server_status_label = QLabel("App Server: failed")
    ui_window.stream_status_label = QLabel("Stream: idle")
    ui_window.codex_status_label = QLabel("Codex: checking")
    ui_window.runtime_status_label = QLabel("Runtime: unavailable")
    ui_window.tunnel_status_label = QLabel("Tunnel: unavailable")
    ui_window.tunnel_client_status_label = QLabel("Tunnel Client: unavailable")
    config_state = "ready" if ui_window._config.roots_ready else "error"
    ui_window.config_status_label = QLabel(
        f"Config: {config_state} · roots {ui_window._config.roots_count}"
    )
    ui_window.config_status_label.setToolTip(
        ui_window._config.roots_error or "Allowed roots are ready"
    )
    ui_window.overall_status_label = QLabel("● Starting")
    ui_window.overall_detail_label = QLabel("● Starting")
    ui_window.usage_status_label = UsageShortcutLabel(format_codex_usage(ui_window._usage))
    ui_window.codex_update_banner_label = QLabel("")
    ui_window.codex_update_banner_label.setVisible(False)
    ui_window.status_button = QPushButton("Status")
    ui_window.diagnostics_toggle_button = QPushButton("Diagnostics")
    ui_window.diagnostics_clear_button = QPushButton("Clear")
    ui_window.retry_now_button = QPushButton("Retry now")
    ui_window.restart_codexbridge_button = QPushButton("Restart CodexBridge")
    ui_window.advanced_toggle_button = QPushButton("Advanced")
    ui_window.advanced_toggle_button.setCheckable(True)
    ui_window.start_bridge_button = QPushButton("Start Bridge")
    ui_window.stop_bridge_button = QPushButton("Stop Bridge")
    ui_window.restart_bridge_button = QPushButton("Restart Bridge")
    ui_window.start_tunnel_button = QPushButton("Start Tunnel")
    ui_window.stop_tunnel_button = QPushButton("Stop Tunnel")
    ui_window.restart_tunnel_button = QPushButton("Restart Tunnel")
    ui_window.start_bridge_button.setEnabled(False)
    ui_window.stop_bridge_button.setEnabled(False)
    ui_window.restart_bridge_button.setEnabled(False)
    ui_window.stop_tunnel_button.setEnabled(False)
    ui_window.restart_tunnel_button.setEnabled(False)
    ui_window.retry_now_button.setEnabled(False)
    ui_window.restart_codexbridge_button.setEnabled(False)
    for label in (
        ui_window.bridge_status_label,
        ui_window.app_server_status_label,
        ui_window.stream_status_label,
        ui_window.codex_status_label,
        ui_window.runtime_status_label,
        ui_window.tunnel_status_label,
        ui_window.tunnel_client_status_label,
        ui_window.config_status_label,
    ):
        label.setObjectName("detailStatus")
    ui_window.overall_status_label.setObjectName("topStatus")
    ui_window.overall_detail_label.setObjectName("detailStatus")
    ui_window.usage_status_label.setObjectName("topStatus")
    ui_window.usage_status_label.setProperty("usageShortcut", True)
    ui_window.usage_status_label.setToolTip(format_codex_usage_tooltip(ui_window._usage))

    ui_window.usage_detail_label = QLabel(format_codex_usage_detail(ui_window._usage))
    ui_window.usage_detail_label.setWordWrap(True)
    ui_window.usage_history_button = QPushButton("Show usage history")
    ui_window.usage_history_button.setObjectName("showUsageHistory")
    ui_window.usage_history_button.setFlat(True)
    ui_window.usage_history_button.setCursor(Qt.CursorShape.PointingHandCursor)
    ui_window.usage_history_button.setStyleSheet(
        "QPushButton { color: palette(link); text-decoration: underline; "
        "border: 0; padding: 2px 0; text-align: left; }"
        "QPushButton:hover { color: palette(highlight); }"
    )
    ui_window.codex_latest_label = QLabel("Not checked")
    ui_window.codex_update_status_label = QLabel("Not checked")
    ui_window.codex_update_message_label = QLabel("")
    ui_window.codex_update_message_label.setWordWrap(True)
    ui_window.codex_update_check_button = QPushButton("Check for updates")
    ui_window.codex_update_button = QPushButton("Update")
    ui_window.codex_update_check_button.setEnabled(False)
    ui_window.codex_update_button.setEnabled(False)
    codex_update_controls = QWidget(window)
    codex_update_controls_layout = QHBoxLayout(codex_update_controls)
    codex_update_controls_layout.setContentsMargins(0, 0, 0, 0)
    codex_update_controls_layout.addWidget(ui_window.codex_update_check_button)
    codex_update_controls_layout.addWidget(ui_window.codex_update_button)
    ui_window.status_refresh_button = QPushButton("Refresh")
    ui_window.status_close_button = QPushButton("Close")

    ui_window.status_dialog = QDialog(window)
    ui_window.status_dialog.setWindowTitle("CodexBridge Status")
    ui_window.status_dialog.setModal(False)
    dialog_layout = QVBoxLayout(ui_window.status_dialog)

    def add_section(title: str, rows: list[tuple[str, QWidget]]) -> None:
        group = QGroupBox(title, ui_window.status_dialog)
        form = QFormLayout(group)
        for name, widget in rows:
            form.addRow(name, widget)
        dialog_layout.addWidget(group)

    add_section("Overall", [("State", ui_window.overall_detail_label)])
    add_section(
        "Connection",
        [
            ("Bridge", ui_window.bridge_status_label),
            ("App Server", ui_window.app_server_status_label),
            ("Stream", ui_window.stream_status_label),
        ],
    )
    add_section(
        "Codex",
        [
            ("Version", ui_window.codex_status_label),
            ("Latest", ui_window.codex_latest_label),
            ("Update", ui_window.codex_update_status_label),
            ("", codex_update_controls),
            ("Status", ui_window.codex_update_message_label),
            ("Runtime", ui_window.runtime_status_label),
        ],
    )
    add_section(
        "Usage",
        [("Codex Usage", ui_window.usage_detail_label), ("", ui_window.usage_history_button)],
    )
    add_section(
        "Tunnel",
        [
            ("Tunnel", ui_window.tunnel_status_label),
            ("Client", ui_window.tunnel_client_status_label),
        ],
    )
    add_section("Configuration", [("Config", ui_window.config_status_label)])

    lifecycle_controls = QHBoxLayout()
    lifecycle_controls.addWidget(ui_window.retry_now_button)
    lifecycle_controls.addWidget(ui_window.restart_codexbridge_button)
    dialog_layout.addLayout(lifecycle_controls)
    dialog_layout.addWidget(ui_window.advanced_toggle_button)

    ui_window.advanced_controls = QWidget(ui_window.status_dialog)
    bridge_controls = QHBoxLayout(ui_window.advanced_controls)
    bridge_controls.setContentsMargins(0, 0, 0, 0)
    for button in (
        ui_window.start_bridge_button,
        ui_window.stop_bridge_button,
        ui_window.restart_bridge_button,
        ui_window.start_tunnel_button,
        ui_window.stop_tunnel_button,
        ui_window.restart_tunnel_button,
    ):
        bridge_controls.addWidget(button)
    dialog_layout.addWidget(ui_window.advanced_controls)
    ui_window.advanced_controls.setVisible(False)
    dialog_buttons = QHBoxLayout()
    dialog_buttons.addStretch(1)
    dialog_buttons.addWidget(ui_window.status_refresh_button)
    dialog_buttons.addWidget(ui_window.status_close_button)
    dialog_layout.addLayout(dialog_buttons)
    ui_window.status_dialog.adjustSize()

    status_bar = QHBoxLayout()
    status_bar.addWidget(ui_window.overall_status_label)
    status_bar.addStretch(1)
    status_bar.addWidget(ui_window.codex_update_banner_label)
    status_bar.addWidget(ui_window.usage_status_label)
    status_bar.addWidget(ui_window.status_button)

    ui_window.thread_pane = ThreadListPane()
    ui_window.history_pane = HistoryPane()
    ui_window.activity_pane = ActivityPane()
    ui_window.splitter = QSplitter(Qt.Orientation.Horizontal)
    ui_window.splitter.addWidget(ui_window.thread_pane)
    ui_window.splitter.addWidget(ui_window.history_pane)
    ui_window.splitter.addWidget(ui_window.activity_pane)
    ui_window.splitter.setStretchFactor(0, 0)
    ui_window.splitter.setStretchFactor(1, 1)
    ui_window.splitter.setStretchFactor(2, 0)
    ui_window.splitter.setSizes([300, 760, 340])

    ui_window.diagnostics_pane = QWidget()
    diagnostics_layout = QVBoxLayout(ui_window.diagnostics_pane)
    diagnostics_layout.setContentsMargins(0, 4, 0, 4)
    diagnostics_header = QHBoxLayout()
    ui_window.diagnostics_title_label = QLabel("Diagnostics")
    diagnostics_header.addWidget(ui_window.diagnostics_title_label)
    diagnostics_header.addStretch(1)
    diagnostics_header.addWidget(ui_window.diagnostics_clear_button)
    diagnostics_layout.addLayout(diagnostics_header)
    ui_window.diagnostics_text = QPlainTextEdit()
    ui_window.diagnostics_text.setReadOnly(True)
    ui_window.diagnostics_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    ui_window.diagnostics_text.setMaximumBlockCount(2000)
    ui_window.diagnostics_text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    diagnostics_layout.addWidget(ui_window.diagnostics_text)
    ui_window.diagnostics_pane.setVisible(False)
    ui_window.diagnostics_toggle_button.clicked.connect(lambda: ui_window.toggle_diagnostics())
    ui_window.diagnostics_clear_button.clicked.connect(ui_window.diagnostics_text.clear)

    ui_window.bottom_status_label = QLabel("Starting…")
    ui_window.bottom_status_label.setObjectName("bottomStatus")
    ui_window.bottom_status_label.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
    )

    root = QWidget()
    layout = QVBoxLayout(root)
    layout.addLayout(status_bar)
    layout.addWidget(ui_window.splitter, 1)
    layout.addWidget(ui_window.diagnostics_pane)
    bottom_bar = QHBoxLayout()
    bottom_bar.addWidget(ui_window.bottom_status_label, 1)
    bottom_bar.addWidget(ui_window.diagnostics_toggle_button)
    layout.addLayout(bottom_bar)
    ui_window.setCentralWidget(root)
    application = QApplication.instance()
    if isinstance(application, QApplication):
        application.setStyle("Fusion")
    ui_window.setStyleSheet(
        """
        QMainWindow, QWidget { background: #202124; color: #e8eaed; }
        QLabel { color: #d8dbe0; }
        QLabel#topStatus { padding: 3px 8px; border: 1px solid #3c4043; border-radius: 3px; }
        QLabel#topStatus[usageShortcut="true"]:hover { background: #2b2d30; }
        QLabel#topStatus[usageShortcut="true"]:focus { border-color: #8ab4f8; }
        QLabel#bottomStatus { color: #aeb4bd; padding: 4px 6px; border-top: 1px solid #3c4043; }
        QLineEdit, QTreeWidget, QTextEdit, QPlainTextEdit {
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
