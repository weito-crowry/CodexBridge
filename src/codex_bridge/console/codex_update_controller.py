from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QMessageBox

from .codex_updates import CodexUpdateInfo, parse_codex_update_info

if TYPE_CHECKING:
    from .main_window import MainWindow


class CodexUpdateController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._codex_update_info: CodexUpdateInfo | None = None
        self._codex_update_busy = False
        self._codex_update_auto_check_started = False

    @property
    def auto_check_started(self) -> bool:
        return self._codex_update_auto_check_started

    def sync_codex_update_controls(self) -> None:
        window = self._window
        has_resolution = window._codex_resolution is not None
        window.codex_update_check_button.setEnabled(has_resolution and not self._codex_update_busy)
        can_update = (
            self._codex_update_info is not None
            and self._codex_update_info.can_update
            and not self._codex_update_busy
        )
        window.codex_update_button.setEnabled(can_update)

    def schedule_codex_update_auto_check(self) -> None:
        window = self._window
        if (
            window._closing
            or self._codex_update_auto_check_started
            or not window._usage_ready
            or window._codex_resolution is None
        ):
            return
        if not window.codex_update_auto_timer.isActive():
            window.codex_update_auto_timer.start()

    def on_codex_update_auto_timeout(self) -> None:
        window = self._window
        if self._codex_update_auto_check_started:
            return
        if not window._usage_ready or window._codex_resolution is None:
            return
        self._codex_update_auto_check_started = True
        self.check_codex_updates()

    def apply_codex_update_busy(self, busy: bool) -> None:
        if self._window._closing:
            return
        self._codex_update_busy = busy
        self.sync_codex_update_controls()

    def check_codex_updates(self) -> None:
        window = self._window
        resolution = window._codex_resolution
        if window._closing or resolution is None or self._codex_update_busy:
            return
        self._codex_update_busy = True
        window.codex_update_status_label.setText("Checking…")
        window.codex_update_message_label.setText("")
        self.sync_codex_update_controls()
        if not window._codex_update_probe.check_for_updates(resolution):
            self.apply_codex_update_check_error("Codex update check failed")

    def apply_codex_update_check(self, payload: object) -> None:
        window = self._window
        if window._closing or window._codex_resolution is None:
            return
        info = (
            payload
            if isinstance(payload, CodexUpdateInfo)
            else parse_codex_update_info(
                payload, current_version=window._codex_resolution.version or ""
            )
        )
        self._codex_update_info = info
        window.codex_latest_label.setText(info.latest_version or "Unavailable")
        window.codex_update_status_label.setText(info.display_status)
        window.codex_update_message_label.setText(
            f"Last checked: {info.last_checked_at}" if info.last_checked_at else ""
        )
        window.codex_update_banner_label.setText(
            "↑ Codex update available" if info.update_available else ""
        )
        window.codex_update_banner_label.setVisible(info.update_available)
        self.sync_codex_update_controls()

    def apply_codex_update_check_error(self, message: str) -> None:
        window = self._window
        if window._closing:
            return
        self._codex_update_busy = False
        self._codex_update_info = None
        window.codex_latest_label.setText("Unavailable")
        window.codex_update_status_label.setText("Unavailable")
        window.codex_update_message_label.setText(message)
        window.codex_update_banner_label.clear()
        window.codex_update_banner_label.setVisible(False)
        self.sync_codex_update_controls()

    def update_codex(self) -> None:
        window = self._window
        resolution = window._codex_resolution
        info = self._codex_update_info
        if window._closing or resolution is None or info is None or not info.can_update:
            return
        answer = QMessageBox.question(
            window,
            "Update Codex",
            "Install the available Codex update now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._codex_update_busy = True
        window.codex_update_status_label.setText("Updating…")
        window.codex_update_message_label.setText("")
        self.sync_codex_update_controls()
        if not window._codex_update_probe.update(resolution):
            self.apply_codex_update_failure("Codex update failed")

    def apply_codex_update_success(self) -> None:
        window = self._window
        if window._closing:
            return
        self._codex_update_busy = False
        self._codex_update_info = None
        window.codex_update_status_label.setText("Update completed; restart required")
        window.codex_update_message_label.setText("")
        window.codex_update_banner_label.clear()
        window.codex_update_banner_label.setVisible(False)
        self.sync_codex_update_controls()

    def apply_codex_update_failure(self, message: str) -> None:
        window = self._window
        if window._closing:
            return
        self._codex_update_busy = False
        window.codex_update_status_label.setText("Update failed")
        window.codex_update_message_label.setText(message)
        self.sync_codex_update_controls()
