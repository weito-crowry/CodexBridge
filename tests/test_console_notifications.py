from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from codex_bridge.console.i18n import initialize_language
from codex_bridge.console.notification_controller import (
    NotificationController,
    NotificationSettingsDialog,
)
from tests.bridge_test_fakes import make_activity_bridge
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
    assert controller.handle_activity(_activity("turn_failed", status="failed", turn_id="turn-2"))
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


@pytest.mark.parametrize("first", ["completed", "failed", "interrupted"])
def test_only_first_confirmed_terminal_notifies_for_each_thread_and_turn(first: str) -> None:
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    assert controller.handle_activity(_activity(f"turn_{first}", status=first))
    for status in ("completed", "failed", "interrupted"):
        assert not controller.handle_activity(_activity(f"turn_{status}", status=status))
    assert len(messages) == 1

    assert controller.handle_activity(_activity("turn_completed", turn_id="turn-2"))
    assert controller.handle_activity(_activity("turn_completed", thread_id="thread-2"))
    assert len(messages) == 3


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_transport_failure_does_not_notify_or_consume_later_confirmed_terminal(
    tmp_path: Path,
    terminal: str,
) -> None:
    bridge, _, store, activities = make_activity_bridge(tmp_path)
    store.ensure_turn("thread-1", "turn-1")
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )

    bridge.handle_app_server_failure("transport closed")
    assert store.snapshot("thread-1", "turn-1")["state"] == "failed"
    for activity in activities.get_recent("thread-1", "turn-1"):
        assert not controller.handle_activity(activity.to_dict())
    assert messages == []

    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": terminal},
            },
        }
    )
    confirmed = activities.latest("thread-1", "turn-1")
    assert confirmed is not None
    assert controller.handle_activity(confirmed.to_dict())
    assert not controller.handle_activity(confirmed.to_dict())
    assert len(messages) == 1
    assert messages[0][0] == f"Turn {terminal}"


def test_retryable_error_does_not_consume_later_confirmed_completion(tmp_path: Path) -> None:
    bridge, _, store, activities = make_activity_bridge(tmp_path)
    store.ensure_turn("thread-1", "turn-1")
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": True,
                "error": {"message": "retrying"},
            },
        }
    )
    retry = activities.latest("thread-1", "turn-1")
    assert retry is not None
    assert not controller.handle_activity(retry.to_dict())
    assert store.snapshot("thread-1", "turn-1")["state"] == "in_progress"
    assert controller.handle_activity(_activity("turn_completed"))
    assert messages == [("Turn completed", "summary")]


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


@pytest.mark.parametrize(
    "kind,status,title_en,title_ja",
    [
        ("turn_completed", "completed", "Turn completed", "ターンが完了しました"),
        ("turn_failed", "failed", "Turn failed", "ターンが失敗しました"),
        ("turn_interrupted", "interrupted", "Turn interrupted", "ターンが中断されました"),
        ("approval_requested", "requested", "Approval required", "承認が必要"),
        ("user_input_requested", "requested", "Input required", "入力が必要"),
    ],
)
def test_language_change_preserves_notification_identity_and_raw_body(
    tmp_path: Path,
    kind: str,
    status: str,
    title_en: str,
    title_ja: str,
) -> None:
    _application()
    settings = QSettings(str(tmp_path / "language.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", "en")
    initialize_language(settings)
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        settings,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    raw = "Turn completed Settings 日本語 {thread_id}"
    event = _activity(kind, status=status, summary=raw, details={"request_id": "request-1"})
    assert controller.handle_activity(event)

    settings.setValue("language", "ja-JP")
    initialize_language(settings)
    assert not controller.handle_activity({**event, "activity_id": "refetched-event"})
    assert controller.handle_activity({**event, "turn_id": "turn-2"})
    assert messages == [(title_en, raw), (title_ja, raw)]
    assert event["type"] == kind
    assert event["details"] == {"request_id": "request-1"}


@pytest.mark.parametrize("selected_language", ["en", "ja-JP"])
def test_settings_reopen_with_machine_keys_and_disable_notifications_after_restart(
    tmp_path: Path,
    selected_language: str,
) -> None:
    _application()
    path = str(tmp_path / "persisted.ini")
    settings = QSettings(path, QSettings.Format.IniFormat)
    settings.setValue("language", selected_language)
    initialize_language(settings)
    dialog = NotificationSettingsDialog(settings)
    assert [dialog.completion_mode.itemData(i) for i in range(3)] == ["always", "inactive", "off"]
    dialog.completion_mode.setCurrentIndex(dialog.completion_mode.findData("off"))
    dialog.approval_enabled.setChecked(False)
    dialog.user_input_enabled.setChecked(False)
    dialog.accept()

    reopened = QSettings(path, QSettings.Format.IniFormat)
    reopened.setFallbacksEnabled(False)
    reopened_dialog = NotificationSettingsDialog(reopened)
    assert reopened_dialog.completion_mode.currentData() == "off"
    assert not reopened_dialog.approval_enabled.isChecked()
    assert not reopened_dialog.user_input_enabled.isChecked()
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        reopened,
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    assert not controller.handle_activity(_activity("turn_completed"))
    for kind in ("approval_requested", "user_input_requested"):
        assert not controller.handle_activity(
            _activity(kind, status="requested", details={"request_id": "request-1"})
        )
    assert messages == []
    reopened_dialog.reject()


@pytest.mark.parametrize(
    "kind,status",
    [
        ("turn_completed", "in_progress"),
        ("turn_failed", "completed"),
        ("turn_interrupted", "unknown"),
        ("approval_requested", "resolved"),
        ("user_input_requested", "resolved"),
        ("approval_resolved", "resolved"),
        ("user_input_resolved", "resolved"),
    ],
)
def test_unconfirmed_or_resolved_activity_does_not_notify(kind: str, status: str) -> None:
    messages: list[tuple[str, str]] = []
    controller = NotificationController(
        FakeSettings(),
        show_message=lambda title, body: messages.append((title, body)),
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    assert not controller.handle_activity(
        _activity(kind, status=status, details={"request_id": "request-1"})
    )
    assert messages == []


def test_native_notification_failure_does_not_break_following_notifications() -> None:
    messages: list[tuple[str, str]] = []

    def show_message(title: str, body: str) -> None:
        if not messages:
            messages.append((title, body))
            raise RuntimeError("OS notifications unavailable")
        messages.append((title, body))

    controller = NotificationController(
        FakeSettings(),
        show_message=show_message,
        is_window_active=lambda: False,
        activate_console=lambda: None,
        play_sound=lambda: None,
    )
    assert not controller.handle_activity(_activity("turn_completed"))
    assert controller.handle_activity(_activity("turn_completed", thread_id="thread-2"))
    assert len(messages) == 2
