from __future__ import annotations

import os
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu, QMessageBox, QToolButton

from .config import ConsoleConfig, ConsoleConfigurationError

if TYPE_CHECKING:
    from .main_window import MainWindow


class SettingsActionController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def install_settings_menu(self) -> None:
        window = self._window
        appearance_menu = window.appearance_button.menu()
        window._appearance_menu = appearance_menu or QMenu(window.settings_button)
        menu = QMenu(window.settings_button)
        window._settings_actions = {
            "Open settings file": menu.addAction("Open settings file", window._open_settings_file),
            "Reload settings": menu.addAction("Reload settings", window._reload_configuration),
            "Open settings folder": menu.addAction(
                "Open settings folder", window._open_settings_folder
            ),
        }
        menu.addSeparator()
        appearance_menu_item = menu.addMenu("Appearance")
        appearance_menu_item.addActions(window._appearance_menu.actions())
        menu.addSeparator()
        window._apply_pending_settings_action = menu.addAction(
            "Restart Console to apply pending settings…",
            window._apply_pending_configuration,
        )
        window._apply_pending_settings_action.setEnabled(False)
        window.settings_button.setMenu(menu)
        window.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        window.settings_menu = menu

    def active_config_path(self) -> Path | None:
        path = self._window._config.config_path
        if not path:
            return None
        return Path(path).expanduser()

    def open_settings_file(self) -> None:
        path = self.active_config_path()
        if path is None:
            QMessageBox.warning(
                self._window, "Settings", "The active configuration file is unavailable."
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path)))
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(
                self._window, "Settings", "Could not open the active configuration file."
            )

    def open_settings_folder(self) -> None:
        path = self.active_config_path()
        if path is None:
            QMessageBox.warning(
                self._window, "Settings", "The active configuration folder is unavailable."
            )
            return
        try:
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path.parent)))
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(
                self._window, "Settings", "Could not open the active configuration folder."
            )

    def settings_change_names(self, candidate: ConsoleConfig) -> tuple[str, ...]:
        window = self._window
        display_names = {
            "allowed_roots": "Allowed roots",
            "attachment_staging_directory": "Attachment staging directory",
            "codex_executable": "Codex executable",
            "config_mode": "Configuration source mode",
            "config_path": "Configuration source path",
            "port": "Console UI port",
            "tunnel_executable": "Tunnel executable",
            "tunnel_profile": "Tunnel profile",
            "tunnel_profile_dir": "Tunnel profile directory",
            "tunnel_profile_file": "Generated Tunnel profile path",
            "tunnel_profile_settings": "Tunnel connection settings",
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
            QMessageBox.warning(window, "Settings", str(exc))
            window.bottom_status_label.setText(
                "Settings reload failed; current settings remain active"
            )
            return

        changed = self.settings_change_names(candidate)
        window._pending_config = candidate if changed else None
        window._apply_pending_settings_action.setEnabled(bool(changed))
        if changed:
            changed_text = ", ".join(changed)
            window.config_status_label.setText(
                f"Config: {candidate.config_mode} · {len(changed)} pending restart"
            )
            window.config_status_label.setToolTip(
                f"Validated pending changes: {changed_text}. Restart Console to apply."
            )
            message = (
                f"Settings reloaded and validated. Pending changes: {changed_text}. "
                "Restart Console to apply them. Bridge and Tunnel were not restarted."
            )
            window.bottom_status_label.setText(
                "Settings reloaded; restart required to apply changes"
            )
        else:
            window.config_status_label.setText(
                f"Config: {candidate.config_mode} · roots {candidate.roots_count}"
            )
            window.config_status_label.setToolTip(
                candidate.roots_error or "Allowed roots are ready; settings are current"
            )
            message = "Settings reloaded and validated. No restart is required."
            window.bottom_status_label.setText("Settings reloaded")
        QMessageBox.information(window, "Settings reloaded", message)

    def apply_pending_configuration(self) -> None:
        window = self._window
        if window._pending_config is None:
            return
        answer = QMessageBox.question(
            window,
            "Restart Console to apply settings",
            "This closes Console and stops its managed Bridge and Tunnel so the validated "
            "settings can load. Active Codex work may be interrupted. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        window._restart_after_settings_apply = True
        window.bottom_status_label.setText("Applying settings and restarting Console…")
        window._begin_exit()
