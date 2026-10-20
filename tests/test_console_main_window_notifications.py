from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer

from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.notification_controller import NotificationSettingsDialog
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    Signal,
    StableTunnel,
    _application,
    _config,
)


class FakeTray:
    def __init__(self) -> None:
        self.activated = Signal()
        self.messageClicked = Signal()
        self.messages: list[tuple[str, str]] = []

    def setIcon(self, _icon: object) -> None:
        pass

    def setToolTip(self, _text: str) -> None:
        pass

    def setContextMenu(self, _menu: object) -> None:
        pass

    def show(self) -> None:
        pass

    def showMessage(self, title: str, body: str, *_args: object) -> None:
        self.messages.append((title, body))

    def hide(self) -> None:
        pass


def _window(
    client: FakeClient,
    *,
    settings: QSettings | None = None,
    tray: FakeTray | None = None,
) -> MainWindow:
    return MainWindow(
        _config(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=tray is not None,
        tray_factory=(lambda _owner: tray) if tray is not None else None,
        appearance_settings=settings,
    )


def test_global_live_notifications_deduplicate_and_retries_back_off_until_connected(
    tmp_path: Path,
) -> None:
    _application()
    client = FakeClient()
    tray = FakeTray()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    window = _window(client, settings=settings, tray=tray)
    window.select_thread("thread-a")
    activity = {
        "activity_id": "activity-a",
        "timestamp": "2026-10-21T00:00:00Z",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "type": "approval_requested",
        "status": "requested",
        "summary": "Approve command",
        "details": {"request_id": "request-a"},
    }

    assert client.global_streams == [window._global_stream_generation]
    client.activity(1, activity)
    client.global_activity(window._global_stream_generation, activity)
    assert len(tray.messages) == 1

    for attempt in range(7):
        window._apply_global_stream_state(window._global_stream_generation, "disconnected")
        assert window._global_stream_retry_timer.interval() == min(1_000 * (2**attempt), 30_000)
        window._retry_global_stream()
    assert len(client.global_streams) == 8
    window._apply_global_stream_state(window._global_stream_generation, "connected")
    assert window._global_stream_retries == 0
    window._apply_global_stream_state(window._global_stream_generation, "disconnected")
    assert window._global_stream_retry_timer.interval() == 1_000
    window._begin_exit()
    window._retry_global_stream()
    assert len(client.global_streams) == 8
    window.close()
    settings.clear()


def test_notification_click_selects_target_thread_and_settings_button_persists(
    tmp_path: Path,
) -> None:
    _application()
    client = FakeClient()
    tray = FakeTray()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    window = _window(client, settings=settings, tray=tray)
    activity = {
        "activity_id": "activity-b",
        "timestamp": "2026-10-21T00:00:00Z",
        "thread_id": "thread-b",
        "turn_id": "turn-b",
        "type": "user_input_requested",
        "status": "requested",
        "summary": "Question pending",
        "details": {"request_id": "request-b"},
    }
    client.global_activity(window._global_stream_generation, activity)
    tray.messageClicked.emit()
    assert window._selected_thread_id == "thread-b"
    assert window.isVisible()
    assert not client.approval_requests

    def accept_settings_dialog() -> None:
        children = window.findChildren(NotificationSettingsDialog)
        assert children
        settings_dialog = children[-1]
        settings_dialog.completion_mode.setCurrentIndex(
            settings_dialog.completion_mode.findData("off")
        )
        settings_dialog.approval_enabled.setChecked(False)
        settings_dialog.user_input_enabled.setChecked(True)
        if sys.platform == "win32":
            assert not settings_dialog.sound_enabled.isEnabled()
        else:
            settings_dialog.sound_enabled.setChecked(True)
        settings_dialog.accept()

    QTimer.singleShot(0, accept_settings_dialog)
    window.notification_settings_button.click()
    assert settings.value("notifications/completion_mode") == "off"
    assert settings.value("notifications/approval_enabled") is False
    assert settings.value("notifications/user_input_enabled") is True
    assert settings.value("notifications/sound_enabled") is (sys.platform != "win32")
    window.close()


def test_global_activity_updates_status_for_unselected_thread() -> None:
    _application()
    client = FakeClient()
    tray = FakeTray()
    window = _window(client, tray=tray)
    client.result(
        "threads",
        {"threads": [{"id": "thread-a", "name": "A"}, {"id": "thread-b", "name": "B"}]},
    )
    generation = window._global_stream_generation

    client.global_activity(
        generation,
        {
            "activity_id": "started-b",
            "thread_id": "thread-b",
            "turn_id": "turn-b",
            "type": "turn_started",
            "status": "in_progress",
            "summary": "Turn started",
            "details": {},
        },
    )

    assert window._thread_turn_statuses["thread-b"] == "in_progress"
    thread_b = next(
        child
        for group_index in range(window.thread_pane.list_widget.topLevelItemCount())
        for group in [window.thread_pane.list_widget.topLevelItem(group_index)]
        for child_index in range(group.childCount())
        for child in [group.child(child_index)]
        if child.data(0, Qt.ItemDataRole.UserRole) == "thread-b"
    )
    assert thread_b is not None
    assert thread_b.text(0).startswith("\u25cf ")

    client.global_activity(
        generation,
        {
            "activity_id": "completed-b",
            "thread_id": "thread-b",
            "turn_id": "turn-b",
            "type": "turn_completed",
            "status": "completed",
            "summary": "Turn completed",
            "details": {},
        },
    )

    assert "thread-b" not in window._thread_turn_statuses
    completed_thread_b = next(
        child
        for group_index in range(window.thread_pane.list_widget.topLevelItemCount())
        for group in [window.thread_pane.list_widget.topLevelItem(group_index)]
        for child_index in range(group.childCount())
        for child in [group.child(child_index)]
        if child.data(0, Qt.ItemDataRole.UserRole) == "thread-b"
    )
    assert completed_thread_b.text(0) == "B"
    window.close()
