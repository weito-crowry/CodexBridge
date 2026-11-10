from __future__ import annotations

import os
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .config import ConsoleConfig, ConsoleConfigurationError
from .configuration_error_presentation import format_configuration_error
from .i18n import install_language_menu, tr

if TYPE_CHECKING:
    from .main_window import MainWindow


class SettingsActionController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def install_settings_menu(self) -> None:
        window = self._window
        menu = window.settings_menu
        pending_action = getattr(window, "_apply_pending_settings_action", None)
        pending_enabled = pending_action.isEnabled() if pending_action is not None else False
        menu.clear()
        window._settings_actions = {
            "Open settings file": menu.addAction(
                tr("Open settings file"), window._open_settings_file
            ),
            "Reload settings": menu.addAction(tr("Reload settings"), window._reload_configuration),
            "Open settings folder": menu.addAction(
                tr("Open settings folder"), window._open_settings_folder
            ),
        }
        install_language_menu(menu, window._appearance_settings)
        menu.addSeparator()
        appearance_menu_item = menu.addMenu(tr("Appearance"))
        appearance_menu_item.addActions(window._appearance_menu.actions())
        menu.addSeparator()
        window._apply_pending_settings_action = menu.addAction(
            tr("Restart Console to apply pending settings…"),
            window._apply_pending_configuration,
        )
        window._apply_pending_settings_action.setEnabled(pending_enabled)
        menu.addAction(tr("Notification settings"), window._show_notification_settings)

    def active_config_path(self) -> Path | None:
        path = self._window._config.config_path
        if not path:
            return None
        return Path(path).expanduser()

    def open_settings_file(self) -> None:
        path = self.active_config_path()
        if path is None:
            QMessageBox.warning(
                self._window, tr("Settings"), tr("The active configuration file is unavailable.")
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(
                self._window, tr("Settings"), tr("Could not open the active configuration file.")
            )

    def open_settings_folder(self) -> None:
        path = self.active_config_path()
        if path is None:
            QMessageBox.warning(
                self._window, tr("Settings"), tr("The active configuration folder is unavailable.")
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path.parent)))
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(
                self._window, tr("Settings"), tr("Could not open the active configuration folder.")
            )

    def settings_change_names(self, candidate: ConsoleConfig) -> tuple[str, ...]:
        window = self._window
        display_names = {
            "host": tr("Host"),
            "codex_executable_source": tr("Codex Executable Source"),
            "tunnel_executable_source": tr("Tunnel Executable Source"),
            "allowed_roots": tr("Allowed roots"),
            "attachment_staging_directory": tr("Attachment staging directory"),
            "codex_executable": tr("Codex executable"),
            "config_mode": tr("Configuration source mode"),
            "config_path": tr("Configuration source path"),
            "port": tr("Console UI port"),
            "tunnel_executable": tr("Tunnel executable"),
            "tunnel_profile": tr("Tunnel profile"),
            "tunnel_profile_dir": tr("Tunnel profile directory"),
            "tunnel_profile_file": tr("Generated Tunnel profile path"),
            "tunnel_profile_settings": tr("Tunnel connection settings"),
        }
        changed: list[str] = []
        for item in dataclass_fields(window._config):
            if item.name == "explicit_overrides":
                continue
            if getattr(window._config, item.name) != getattr(candidate, item.name):
                changed.append(display_names.get(item.name, item.name.replace("_", " ").title()))
        return tuple(changed)

    def reload_configuration(self) -> None:
        window = self._window
        explicit_overrides = cast(dict[str, Any], dict(window._config.explicit_overrides))
        try:
            candidate = ConsoleConfig.from_sources(**explicit_overrides)
        except ConsoleConfigurationError as exc:
            QMessageBox.warning(window, tr("Settings"), format_configuration_error(str(exc)))
            window.bottom_status_label.setText(
                tr("Settings reload failed; current settings remain active")
            )
            return

        changed = self.settings_change_names(candidate)
        window._pending_config = candidate if changed else None
        window._apply_pending_settings_action.setEnabled(bool(changed))
        if changed:
            changed_text = ", ".join(changed)
            window.config_status_label.setText(
                tr("Config: {p0} · {p1} pending restart", p0=candidate.config_mode, p1=len(changed))
            )
            window.config_status_label.setToolTip(
                tr("Validated pending changes: {p0}. Restart Console to apply.", p0=changed_text)
            )
            message = tr(
                "Settings reloaded and validated. Pending changes: {p0}. Restart Console "
                "to apply them. Bridge and Tunnel were not restarted.",
                p0=changed_text,
            )
            window.bottom_status_label.setText(
                tr("Settings reloaded; restart required to apply changes")
            )
        else:
            window.config_status_label.setText(
                tr("Config: {p0} · roots {p1}", p0=candidate.config_mode, p1=candidate.roots_count)
            )
            window.config_status_label.setToolTip(
                format_configuration_error(candidate.roots_error or "")
                or tr("Allowed roots are ready; settings are current")
            )
            message = tr("Settings reloaded and validated. No restart is required.")
            window.bottom_status_label.setText(tr("Settings reloaded"))
        QMessageBox.information(window, tr("Settings reloaded"), message)

    def apply_pending_configuration(self) -> None:
        window = self._window
        if window._pending_config is None:
            return
        answer = QMessageBox.question(
            window,
            tr("Restart Console to apply settings"),
            tr(
                "This closes Console and stops its managed Bridge and Tunnel so the "
                "validated settings can load. Active Codex work may be interrupted. "
                "Continue?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        window._restart_after_settings_apply = True
        window.bottom_status_label.setText(tr("Applying settings and restarting Console…"))
        window._begin_exit()
