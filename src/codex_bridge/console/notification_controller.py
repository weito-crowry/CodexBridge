from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Protocol

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

_COMPLETION_MODES = frozenset({"always", "inactive", "off"})
_MAX_SEEN = 2_048
_MAX_NOTIFICATION_TEXT = 240


class SettingsLike(Protocol):
    def value(self, key: str, default: object = None) -> object: ...

    def setValue(self, key: str, value: object) -> None: ...


def _setting_bool(settings: SettingsLike, key: str, default: bool) -> bool:
    value = settings.value(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _bounded_text(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    normalized = " ".join(value.split())
    if len(normalized) <= _MAX_NOTIFICATION_TEXT:
        return normalized
    return normalized[: _MAX_NOTIFICATION_TEXT - 1].rstrip() + "…"


class NotificationController:
    """Decide which live Activity records produce a native notification."""

    def __init__(
        self,
        settings: SettingsLike,
        *,
        show_message: Callable[[str, str], None],
        is_window_active: Callable[[], bool],
        activate_thread: Callable[[str], None],
        play_sound: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._show_message = show_message
        self._is_window_active = is_window_active
        self._activate_thread = activate_thread
        self._play_sound = play_sound
        self._seen: OrderedDict[tuple[object, ...], None] = OrderedDict()
        self._last_clicked_thread_id: str | None = None

    def _remember(self, key: tuple[object, ...]) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > _MAX_SEEN:
            self._seen.popitem(last=False)
        return True

    def _notify(self, key: tuple[object, ...], thread_id: str, title: str, body: str) -> bool:
        if not self._remember(key):
            return False
        self._last_clicked_thread_id = thread_id
        try:
            self._show_message(title, body)
        except Exception:
            return False
        if _setting_bool(self._settings, "notifications/sound_enabled", False):
            try:
                self._play_sound()
            except Exception:
                pass
        return True

    def handle_activity(self, activity: object) -> bool:
        if not isinstance(activity, Mapping):
            return False
        thread_id = activity.get("thread_id")
        turn_id = activity.get("turn_id")
        activity_type = activity.get("type")
        status = activity.get("status")
        details = activity.get("details")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(activity_type, str)
            or not isinstance(status, str)
            or not isinstance(details, Mapping)
        ):
            return False
        if not isinstance(turn_id, str) or not turn_id:
            return False
        body = _bounded_text(activity.get("summary"), "Open CodexBridge Console to view details")

        if activity_type in {"turn_completed", "turn_failed", "turn_interrupted"}:
            expected_status = {
                "turn_completed": "completed",
                "turn_failed": "failed",
                "turn_interrupted": "interrupted",
            }[activity_type]
            if status != expected_status:
                return False
            mode = self._settings.value("notifications/completion_mode", "inactive")
            if mode not in _COMPLETION_MODES:
                mode = "inactive"
            if mode == "off" or (mode == "inactive" and self._is_window_active()):
                return False
            title = {
                "turn_completed": "Turn completed",
                "turn_failed": "Turn failed",
                "turn_interrupted": "Turn interrupted",
            }[activity_type]
            return self._notify(("turn", thread_id, turn_id, activity_type), thread_id, title, body)

        request_types = {
            "approval_requested": ("notifications/approval_enabled", "Approval required"),
            "user_input_requested": ("notifications/user_input_enabled", "Input required"),
        }
        request = request_types.get(activity_type)
        if request is None or status != "requested":
            return False
        setting_key, title = request
        if not _setting_bool(self._settings, setting_key, True):
            return False
        request_id = details.get("request_id")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            return False
        return self._notify(
            ("request", activity_type, thread_id, turn_id, request_id), thread_id, title, body
        )

    def handle_message_clicked(self) -> bool:
        thread_id = self._last_clicked_thread_id
        if thread_id is None:
            return False
        self._last_clicked_thread_id = None
        self._activate_thread(thread_id)
        return True


class NotificationSettingsDialog(QDialog):
    """Small Console-owned editor for notification preferences."""

    def __init__(self, settings: SettingsLike, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Notification settings")
        self.completion_mode = QComboBox(self)
        self.completion_mode.addItem("Always", "always")
        self.completion_mode.addItem("When Console is inactive", "inactive")
        self.completion_mode.addItem("Off", "off")
        self.approval_enabled = QCheckBox("Approval requests", self)
        self.user_input_enabled = QCheckBox("User input requests", self)
        self.sound_enabled = QCheckBox("Play an extra beep when showing a notification", self)
        self.os_sound_note = QLabel(self)
        if sys.platform == "win32":
            self.sound_enabled.setEnabled(False)
            self.os_sound_note.setText(
                "Windows controls native notification sound and quiet hours; Console does not "
                "override them."
            )
        else:
            self.os_sound_note.setText(
                "The extra beep is separate from any sound played by native notifications."
            )

        stored_mode = settings.value("notifications/completion_mode", "inactive")
        index = self.completion_mode.findData(stored_mode)
        self.completion_mode.setCurrentIndex(index if index >= 0 else 1)
        self.approval_enabled.setChecked(
            _setting_bool(settings, "notifications/approval_enabled", True)
        )
        self.user_input_enabled.setChecked(
            _setting_bool(settings, "notifications/user_input_enabled", True)
        )
        self.sound_enabled.setChecked(_setting_bool(settings, "notifications/sound_enabled", False))

        form = QFormLayout()
        form.addRow("Turn completion", self.completion_mode)
        form.addRow(self.approval_enabled)
        form.addRow(self.user_input_enabled)
        form.addRow(self.sound_enabled)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.os_sound_note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        mode = self.completion_mode.currentData()
        if mode not in _COMPLETION_MODES:
            mode = "inactive"
        self._settings.setValue("notifications/completion_mode", mode)
        self._settings.setValue("notifications/approval_enabled", self.approval_enabled.isChecked())
        self._settings.setValue(
            "notifications/user_input_enabled", self.user_input_enabled.isChecked()
        )
        self._settings.setValue("notifications/sound_enabled", self.sound_enabled.isChecked())
        sync = getattr(self._settings, "sync", None)
        if callable(sync):
            sync()
        super().accept()
