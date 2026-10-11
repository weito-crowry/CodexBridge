from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

if TYPE_CHECKING:
    from .main_window import MainWindow


class TrayController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def build(self) -> None:
        window = self._window
        if not window._tray_available or window._window_icon.isNull():
            return
        try:
            window.tray_icon = window._tray_factory(window)
            set_icon = getattr(window.tray_icon, "setIcon", None)
            if not callable(set_icon):
                window.tray_icon = None
                return
            set_icon(window._window_icon)
            window.tray_icon.setToolTip("CodexBridge Console")
        except Exception:
            window.tray_icon = None
            return
        menu = QMenu(window)
        window.tray_menu = menu
        for text, callback in (
            ("Show Console", window._show_console),
            ("Hide Console", window._hide_console),
        ):
            action = QAction(text, window)
            action.triggered.connect(callback)
            menu.addAction(action)
            window._tray_actions[text] = action
        menu.addSeparator()
        for text, callback in (
            ("Start Bridge", window._start_bridge),
            ("Stop Bridge", window._stop_bridge),
            ("Restart Bridge", window._restart_bridge),
        ):
            action = QAction(text, window)
            action.triggered.connect(callback)
            menu.addAction(action)
            window._tray_actions[text] = action
        menu.addSeparator()
        for text, callback in (
            ("Start Tunnel", window._start_tunnel),
            ("Stop Tunnel", window._stop_tunnel),
            ("Restart Tunnel", window._restart_tunnel),
        ):
            action = QAction(text, window)
            action.triggered.connect(callback)
            menu.addAction(action)
            window._tray_actions[text] = action
        menu.addSeparator()
        exit_action = QAction("Exit", window)
        exit_action.triggered.connect(window._begin_exit)
        menu.addAction(exit_action)
        window._tray_actions["Exit"] = exit_action
        try:
            window.tray_icon.setContextMenu(menu)
            window.tray_icon.activated.connect(window._on_tray_activated)
            window.tray_icon.show()
        except Exception:
            window.tray_icon = None
            window.tray_menu = None
            window._tray_actions.clear()
            return
        window._tray_usable = True

    def show_console(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def hide_console(self) -> None:
        self._window.hide()

    def on_activated(self, reason: object) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._window._show_console()

    def handle_close_event(self, event: QCloseEvent) -> None:
        window = self._window
        if window._closing:
            event.accept()
            return
        if window._tray_usable:
            window.hide()
            if not window._tray_notified and window.tray_icon is not None:
                show_message = getattr(window.tray_icon, "showMessage", None)
                if callable(show_message):
                    try:
                        show_message(
                            "CodexBridge Console",
                            "Console is still running in the system tray.",
                        )
                    except Exception:
                        pass
                window._tray_notified = True
            event.ignore()
            return
        window._begin_exit()
        window.hide()
        event.ignore()
