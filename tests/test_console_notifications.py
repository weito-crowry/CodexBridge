from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings

from codex_bridge.console.notification_controller import (
    NotificationController,
    NotificationSettingsDialog,
)
from tests.console_main_window_test_support import _application


class FakeSettings:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = {} if values is None else dict(values)

    def value(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


def _activity(
    activity_type: str,
    *,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    status: str = "completed",
    summary: str | None = "summary",
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "type": activity_type,
        "status": status,
        "summary": summary,
        "details": {} if details is None else details,
    }


def test_completion_notifications_follow_mode_and_click_shows_console() -> None:
    settings = FakeSettings({"notifications/completion_mode": "inactive"})
    active = True
    messages: list[tuple[str, str]] = []
    opened: list[str] = []
    sounds: list[bool] = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: active,
        activate_console=lambda: opened.append("console"),
        play_sound=lambda: sounds.append(True),
    )

    assert not controller.handle_activity(_activity("turn_completed"))
    active = False
    assert controller.handle_activity(_activity("turn_completed"))
    assert messages == [("Turn completed", "summary")]
    assert controller.handle_activity(_activity("turn_failed", status="failed"))
    assert messages[-1][0] == "Turn failed"

    assert controller.handle_message_clicked()
    assert opened == ["console"]
    assert sounds == []


def test_request_notifications_are_independently_configurable_and_deduplicated() -> None:
    settings = FakeSettings(
        {
            "notifications/approval_enabled": True,
            "notifications/user_input_enabled": False,
            "notifications/sound_enabled": True,
        }
    )
    messages: list[tuple[str, str]] = []
    sounds: list[bool] = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: True,
        activate_console=lambda: None,
        play_sound=lambda: sounds.append(True),
    )
    approval = _activity(
        "approval_requested",
        status="requested",
        summary="Run tests",
        details={"request_id": "request-1"},
    )

    assert controller.handle_activity(approval)
    assert not controller.handle_activity(approval)
    assert not controller.handle_activity(
        _activity(
            "user_input_requested",
            status="requested",
            details={"request_id": "question-1"},
        )
    )
    assert not controller.handle_activity(
        _activity("approval_requested", status="requested", details={})
    )
    assert messages == [("Approval required", "Run tests")]
    assert sounds == [True]


def test_completion_off_mode_and_unknown_events_are_ignored() -> None:
    settings = FakeSettings({"notifications/completion_mode": "off"})
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    assert not controller.handle_activity(_activity("turn_completed"))
    assert not controller.handle_activity(_activity("command_output", status="in_progress"))
    assert messages == []


def test_interrupted_turn_has_its_own_completion_notification() -> None:
    settings = FakeSettings({"notifications/completion_mode": "always"})
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: True,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    assert controller.handle_activity(
        _activity("turn_interrupted", status="interrupted", summary="Stopped")
    )
    assert messages == [("Turn interrupted", "Stopped")]


def test_request_deduplication_uses_request_id_within_thread_and_turn() -> None:
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: True,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    first = _activity(
        "approval_requested",
        status="requested",
        details={"request_id": "approval-1"},
    )
    second = _activity(
        "approval_requested",
        status="requested",
        details={"request_id": "approval-2"},
    )

    assert controller.handle_activity(first)
    assert not controller.handle_activity(first)
    assert controller.handle_activity(second)
    assert len(messages) == 2


def test_consecutive_notifications_from_different_threads_only_activate_console() -> None:
    messages: list[tuple[str, str]] = []
    activated: list[str] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: True,
        activate_console=lambda: activated.append("console"),
        play_sound=lambda: None,
    )

    assert controller.handle_activity(
        _activity("turn_completed", thread_id="thread-a", turn_id="turn-a")
    )
    assert controller.handle_activity(
        _activity("turn_failed", thread_id="thread-b", turn_id="turn-b", status="failed")
    )
    assert messages == [("Turn completed", "summary"), ("Turn failed", "summary")]

    assert controller.handle_message_clicked()
    assert activated == ["console"]


def test_missing_completion_mode_defaults_to_always_without_changing_settings() -> None:
    settings = FakeSettings({"notifications/approval_enabled": False})
    controller = NotificationController(
        settings,
        show_message=lambda _title, _body: None,
        is_window_active=lambda: True,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    assert controller.handle_activity(_activity("turn_completed"))
    assert "notifications/completion_mode" not in settings.values
    assert settings.values["notifications/approval_enabled"] is False


def test_notification_settings_dialog_loads_and_persists_preferences(tmp_path: Path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "notifications.ini"), QSettings.Format.IniFormat)
    settings.setFallbacksEnabled(False)
    settings.setValue("notifications/completion_mode", "always")
    settings.setValue("notifications/approval_enabled", False)
    dialog = NotificationSettingsDialog(settings)

    assert dialog.completion_mode.currentData() == "always"
    assert not dialog.approval_enabled.isChecked()
    assert dialog.user_input_enabled.isChecked()
    assert not dialog.sound_enabled.isChecked()
    assert dialog.sound_enabled.isEnabled() is (sys.platform != "win32")

    dialog.completion_mode.setCurrentIndex(dialog.completion_mode.findData("off"))
    dialog.approval_enabled.setChecked(True)
    dialog.user_input_enabled.setChecked(False)
    dialog.sound_enabled.setChecked(sys.platform != "win32")
    dialog.accept()

    assert settings.value("notifications/completion_mode") == "off"
    assert settings.value("notifications/approval_enabled", type=bool)
    assert not settings.value("notifications/user_input_enabled", type=bool)
    assert settings.value("notifications/sound_enabled", type=bool) is (sys.platform != "win32")


def test_notification_settings_default_to_always_and_preserve_saved_mode(tmp_path: Path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "notification-default.ini"), QSettings.Format.IniFormat)
    settings.setFallbacksEnabled(False)
    dialog = NotificationSettingsDialog(settings)

    assert dialog.completion_mode.currentData() == "always"
    assert settings.value("notifications/completion_mode") is None
    dialog.reject()
    assert settings.value("notifications/completion_mode") is None

    settings.setValue("notifications/completion_mode", "off")
    saved_dialog = NotificationSettingsDialog(settings)
    assert saved_dialog.completion_mode.currentData() == "off"
    saved_dialog.reject()
    assert settings.value("notifications/completion_mode") == "off"
