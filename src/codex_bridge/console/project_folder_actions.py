from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtGui import QDesktopServices

if TYPE_CHECKING:
    from .main_window import MainWindow


class ProjectFolderActionsController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def validated_project_folder(self, cwd: str) -> str | None:
        window = self._window
        folder = window.thread_pane._openable_cwd(cwd)
        if folder is None:
            window.bottom_status_label.setText("Project folder is no longer available")
        return folder

    def new_thread_for_folder(self, cwd: str) -> None:
        folder = self.validated_project_folder(cwd)
        if folder is not None:
            self._window._composer_controller.open_new_thread_draft(folder)

    def open_project_folder(self, cwd: str) -> None:
        window = self._window
        folder = self.validated_project_folder(cwd)
        if folder is None:
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception:
            opened = False
        if not opened:
            window.bottom_status_label.setText("Could not open project folder in Explorer")

    def open_project_terminal(self, cwd: str) -> None:
        window = self._window
        folder = self.validated_project_folder(cwd)
        if folder is None:
            return
        if sys.platform == "win32":
            commands: tuple[tuple[str, list[str]], ...] = (
                ("wt.exe", ["-d", folder]),
                ("cmd.exe", []),
            )
        elif sys.platform == "darwin":
            commands = (("open", ["-a", "Terminal"]),)
        else:
            commands = (("x-terminal-emulator", []),)
        for program, arguments in commands:
            try:
                result = QProcess.startDetached(program, arguments, folder)
            except Exception:
                continue
            started = bool(result[0]) if isinstance(result, tuple) and result else bool(result)
            if started:
                return
        window.bottom_status_label.setText("Could not open a terminal in the project folder")
