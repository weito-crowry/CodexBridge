from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QSettings, Qt, QTimer
from PySide6.QtCore import Signal as QtSignal
from PySide6.QtGui import QPalette

from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.notification_controller import NotificationSettingsDialog
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
    _config,
)


class FakeTray(QObject):
    activated = QtSignal(object)
    messageClicked = QtSignal()

    def __init__(self) -> None:
        super().__init__()
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
    language: str = "en",
) -> MainWindow:
    if settings is not None:
        settings.setValue("language", language)
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


@pytest.mark.parametrize("language", ["en", "ja-JP"])
@pytest.mark.parametrize("notification_count", [1, 2])
def test_notification_click_preserves_selected_thread_and_does_not_submit_actions(
    tmp_path: Path,
    language: str,
    notification_count: int,
) -> None:
    _application()
    client = FakeClient()
    tray = FakeTray()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    window = _window(client, settings=settings, tray=tray, language=language)
    raw_title = "Settings Turn completed 日本語 {thread_id}"
    client.result("threads", {"threads": [{"id": "selected-thread", "name": raw_title}]})
    window.select_thread("selected-thread")
    for thread_id, request_id in (("thread-a", "request-a"), ("thread-b", "request-b"))[
        :notification_count
    ]:
        client.global_activity(
            window._global_stream_generation,
            {
                "activity_id": f"activity-{thread_id}",
                "timestamp": "2026-10-21T00:00:00Z",
                "thread_id": thread_id,
                "turn_id": f"turn-{thread_id}",
                "type": "user_input_requested",
                "status": "requested",
                "summary": "Question pending",
                "details": {"request_id": request_id},
            },
        )
    tray.messageClicked.emit()
    assert window._selected_thread_id == "selected-thread"
    assert window.isVisible()
    title_items = window.thread_pane.list_widget.findItems(
        raw_title, Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchRecursive
    )
    assert len(title_items) == 1
    assert title_items[0].data(0, Qt.ItemDataRole.UserRole) == "selected-thread"
    assert len(tray.messages) == notification_count
    assert not client.approval_requests
    assert not client.json_posts
    assert not client.control_requests

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
    for kind, status, details in (
        ("turn_completed", "completed", {}),
        ("approval_requested", "requested", {"request_id": "disabled-approval"}),
    ):
        client.global_activity(
            window._global_stream_generation,
            {
                "thread_id": "thread-c",
                "turn_id": "turn-c",
                "type": kind,
                "status": status,
                "summary": "Settings take effect immediately",
                "details": details,
            },
        )
    assert len(tray.messages) == notification_count
    assert not client.approval_requests
    assert not client.json_posts
    assert not client.control_requests
    window._begin_exit()
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
    running_section = window.thread_pane.list_widget.topLevelItem(0)
    assert running_section.text(0) == "Currently running"
    assert running_section.child(0).child(0).data(0, Qt.ItemDataRole.UserRole) == "thread-b"
    thread_b = next(
        child
        for group_index in range(window.thread_pane.list_widget.topLevelItemCount())
        for group in [window.thread_pane.list_widget.topLevelItem(group_index)]
        for child_index in range(group.childCount())
        for child in [group.child(child_index)]
        if child.data(0, Qt.ItemDataRole.UserRole) == "thread-b"
    )
    assert thread_b is not None
    assert thread_b.text(0) == "B"
    assert not thread_b.icon(0).isNull()
    assert thread_b.foreground(0).color() == window.thread_pane.list_widget.palette().color(
        QPalette.ColorRole.Link
    )

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
    assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Other"
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


def test_status_history_does_not_notify_or_consume_live_terminal_after_reconnect(
    tmp_path: Path,
) -> None:
    _application()
    client = FakeClient()
    tray = FakeTray()
    settings = QSettings(str(tmp_path / "console.ini"), QSettings.Format.IniFormat)
    window = _window(client, settings=settings, tray=tray)
    window.select_thread("thread-a")
    historical = {
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "type": "turn_failed",
        "status": "failed",
        "summary": "transport closed",
        "details": {"reason": "connection_lost"},
    }
    snapshot = {
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "state": "failed",
        "last_turn": {"status": "unknown", "reason": "connection_lost"},
        "recent_activities": [historical],
    }
    window._apply_status(snapshot)
    assert tray.messages == []

    client.global_activity(window._global_stream_generation, historical)
    assert tray.messages == []
    completed = {
        **historical,
        "type": "turn_completed",
        "status": "completed",
        "summary": "Turn completed",
        "details": {},
    }
    client.global_activity(window._global_stream_generation, completed)
    assert tray.messages == [("Turn completed", "Turn completed")]
    window._apply_global_stream_state(window._global_stream_generation, "disconnected")
    window._apply_global_stream_state(window._global_stream_generation, "connected")
    window._apply_status({**snapshot, "recent_activities": [completed]})
    client.activity(window._selection_generation, completed)
    client.global_activity(window._global_stream_generation, completed)
    assert tray.messages == [("Turn completed", "Turn completed")]
    assert not client.json_posts
    assert not client.approval_requests
    assert not client.control_requests
    window._begin_exit()
    window.close()


def test_failed_and_interrupted_turns_leave_the_running_aggregate() -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    client.result("threads", {"threads": [{"id": "thread-a", "name": "A"}]})
    generation = window._global_stream_generation

    for terminal_type, terminal_status in (
        ("turn_failed", "failed"),
        ("turn_interrupted", "interrupted"),
    ):
        client.global_activity(
            generation,
            {
                "thread_id": "thread-a",
                "turn_id": f"turn-{terminal_status}",
                "type": "turn_started",
                "status": "in_progress",
                "details": {},
            },
        )
        assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Currently running"
        client.global_activity(
            generation,
            {
                "thread_id": "thread-a",
                "turn_id": f"turn-{terminal_status}",
                "type": terminal_type,
                "status": terminal_status,
                "details": {},
            },
        )
        assert terminal_type not in window._thread_turn_statuses.values()
        assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Other"

    window.close()
