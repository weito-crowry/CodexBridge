from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QMessageBox, QPushButton, QSplitter

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_updates import CodexUpdateInfo
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.runtime_launcher import DetachedLaunchResult
from codex_bridge.console.tunnel_supervisor import TunnelActionState


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeClient:
    def __init__(self) -> None:
        self.json_succeeded = Signal()
        self.json_failed = Signal()
        self.activity_received = Signal()
        self.stream_state_changed = Signal()
        self.control_succeeded = Signal()
        self.control_failed = Signal()
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.streams: list[tuple[str, int]] = []
        self.aborted_groups: list[str] = []
        self.stopped_streams = 0
        self.aborted_all = False
        self.control_requests: list[tuple[str, str]] = []
        self.json_posts: list[tuple[str, str, dict[str, object]]] = []

    def get_json(self, path: str, *, key: str, query: dict[str, object] | None = None) -> bool:
        self.requests.append((key, path, query))
        return True

    def post_json(self, path: str, payload: dict[str, object], *, key: str) -> bool:
        self.json_posts.append((key, path, payload))
        return True

    def abort_json_group(self, prefix: str) -> None:
        self.aborted_groups.append(prefix)

    def start_stream(self, thread_id: str, generation: int) -> None:
        self.streams.append((thread_id, generation))

    def stop_stream(self) -> None:
        self.stopped_streams += 1

    def abort_all(self) -> None:
        self.aborted_all = True

    def post_control_shutdown(self, token: str, *, key: str) -> bool:
        self.control_requests.append((token, key))
        return True

    def result(self, key: str, payload: object) -> None:
        self.json_succeeded.emit(key, payload)

    def failure(self, key: str, message: str) -> None:
        self.json_failed.emit(key, message)

    def control_success(self, key: str) -> None:
        self.control_succeeded.emit(key)

    def control_failure(self, key: str, message: str = "Bridge control request failed") -> None:
        self.control_failed.emit(key, message)

    def activity(self, generation: int, payload: dict[str, object]) -> None:
        self.activity_received.emit(generation, payload)


class FakeCodexProbe:
    def __init__(self) -> None:
        self.resolved = Signal()
        self.failed = Signal()
        self.starts = 0
        self.aborts = 0

    def start(self) -> bool:
        self.starts += 1
        return True

    def abort(self) -> None:
        self.aborts += 1

    def result(self, resolution: CodexResolution) -> None:
        self.resolved.emit(resolution)

    def failure(self, message: str = "Codex version could not be verified") -> None:
        self.failed.emit(message)


class FakeCodexUpdateProbe:
    def __init__(self) -> None:
        self.check_succeeded = Signal()
        self.check_failed = Signal()
        self.update_succeeded = Signal()
        self.update_failed = Signal()
        self.busy_changed = Signal()
        self.check_calls: list[CodexResolution] = []
        self.update_calls: list[CodexResolution] = []
        self.busy = False

    def check_for_updates(self, resolution: CodexResolution) -> bool:
        self.check_calls.append(resolution)
        self.busy = True
        self.busy_changed.emit(True)
        return True

    def update(self, resolution: CodexResolution) -> bool:
        self.update_calls.append(resolution)
        self.busy = True
        self.busy_changed.emit(True)
        return True

    def abort(self) -> None:
        if self.busy:
            self.busy = False
            self.busy_changed.emit(False)

    def result(self, info: CodexUpdateInfo) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.check_succeeded.emit(info)

    def failure(self, message: str = "Codex update check failed") -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.check_failed.emit(message)

    def update_success(self) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.update_succeeded.emit()

    def update_failure(self, message: str = "Codex update failed") -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.update_failed.emit(message)


class FakeLauncher:
    def __init__(self, *, started: bool = True) -> None:
        self.started = started
        self.calls: list[tuple[str, int]] = []
        self.control_tokens: list[str] = []
        self.closes = 0

    def launch(
        self,
        *,
        codex_executable: str,
        ui_port: int,
        control_token: str,
        allowed_roots: tuple[str, ...] = (),
    ) -> DetachedLaunchResult:
        del allowed_roots
        self.calls.append((codex_executable, ui_port))
        self.control_tokens.append(control_token)
        return DetachedLaunchResult(self.started, 1234 if self.started else None)

    def close(self) -> None:
        self.closes += 1


class StableTunnel:
    def __init__(self) -> None:
        self.state_changed = Signal()
        self.message_changed = Signal()
        self.controls_changed = Signal()
        self.state = "unavailable"
        self.action_state = TunnelActionState(False, False, False)
        self.started = 0

    def set_bridge_ready(self, _ready: bool) -> None:
        pass

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, *, on_finished=None) -> bool:
        if on_finished is not None:
            on_finished()
        return False

    def close(self, *, on_finished=None) -> None:
        if on_finished is not None:
            on_finished()


class ManagedTunnel(StableTunnel):
    def __init__(self) -> None:
        super().__init__()
        self.state = "ready"
        self.action_state = TunnelActionState(False, True, True)
        self.stop_calls = 0

    def stop(self, *, on_finished=None) -> bool:
        self.stop_calls += 1
        self.state = "stopped"
        self.action_state = TunnelActionState(True, False, False)
        if on_finished is not None:
            on_finished()
        return True


class LifecycleTunnel(StableTunnel):
    def __init__(self, state: str = "unavailable") -> None:
        super().__init__()
        self.state = state
        self.retry_calls = 0

    def retry_now(self) -> bool:
        self.retry_calls += 1
        return True

    def emit_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)


class RecoveringLifecycleTunnel(LifecycleTunnel):
    def __init__(self, state: str = "failed") -> None:
        super().__init__(state)
        self.recovery_timer = QTimer()
        self.recovery_timer.setSingleShot(True)


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _config() -> ConsoleConfig:
    return ConsoleConfig(allowed_roots=(str(Path.cwd()),))


def _item(item_id: str, item_type: str, **fields: object) -> dict[str, object]:
    return {"id": item_id, "type": item_type, **fields}


def _activity(activity_id: str, thread_id: str = "thread-b") -> dict[str, object]:
    return {
        "activity_id": activity_id,
        "timestamp": "2026-08-28T00:00:00Z",
        "thread_id": thread_id,
        "turn_id": "turn-1",
        "type": "error",
        "status": "failed",
        "summary": activity_id,
        "details": {},
    }


def test_main_window_constructs_three_panes_and_disconnected_empty_state() -> None:
    _application()
    client = FakeClient()

    window = MainWindow(_config(), api_client=client, tray_available=False)

    assert len(window.findChildren(QSplitter)) == 1
    assert window.thread_pane is not None
    assert window.history_pane is not None
    assert window.activity_pane is not None
    assert "CodexBridge is not available" in window.history_pane._empty_label.text()
    assert window.stream_status_label.text() == "Stream: idle"
    assert window.overall_status_label.text() == "● Starting"
    assert window.usage_status_label.text() == "Codex Usage  unavailable"
    assert window.bridge_status_label.window() is window.status_dialog
    window.close()


def test_status_button_opens_detail_dialog_and_refresh_reloads_usage() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.status_button.click()
    assert window.status_dialog.isVisible()

    client.requests.clear()
    window.status_refresh_button.click()

    assert any(
        key == "usage" and path == "/ui-api/account/rate-limits" for key, path, _ in client.requests
    )
    window.status_dialog.close()
    window.close()


def test_generic_refresh_does_not_reload_usage() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.requests.clear()
    window.refresh()

    assert not any(key == "usage" for key, _, _ in client.requests)
    window.close()


def test_force_usage_does_not_change_mode_of_in_flight_initial_request() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window._bridge_ready = True
    window._app_server_ready = True
    window._begin_usage_sequence()
    window._on_usage_initial_timeout()

    window._request_usage(force=True)

    assert window._usage_request_mode == "initial"
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_ready_usage_waits_before_first_request() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert not any(key == "usage" for key, _, _ in client.requests)
    assert window.usage_initial_timer.isActive()
    assert window.usage_initial_timer.interval() == 1_500

    window._on_usage_initial_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_initial_usage_failure_retries_only_after_delay_and_stops_at_three_attempts() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_usage_initial_timeout()
    client.failure("usage", "Bridge unavailable")

    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window.usage_retry_timer.isActive()
    assert window.usage_retry_timer.interval() == 3_000

    window._on_usage_retry_timeout()
    client.failure("usage", "Bridge unavailable")
    window._on_usage_retry_timeout()
    client.failure("usage", "Bridge unavailable")

    assert sum(key == "usage" for key, _, _ in client.requests) == 3
    assert not window.usage_retry_timer.isActive()
    window.close()


def test_ready_loss_invalidates_pending_usage_retry() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_usage_initial_timeout()
    client.failure("usage", "Bridge unavailable")
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})

    request_count = sum(key == "usage" for key, _, _ in client.requests)
    window._on_usage_retry_timeout()

    assert sum(key == "usage" for key, _, _ in client.requests) == request_count
    assert not window.usage_retry_timer.isActive()
    window.close()


def test_periodic_usage_is_ready_only_and_generic_refresh_does_not_request_it() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_usage_initial_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 10}}},
    )
    client.requests.clear()

    window.refresh()
    assert not any(key == "usage" for key, _, _ in client.requests)

    window._on_usage_poll_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})
    client.requests.clear()
    window._on_usage_poll_timeout()
    assert not any(key == "usage" for key, _, _ in client.requests)
    window.close()


def test_periodic_usage_failure_keeps_last_known_display() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    known = window.usage_status_label.text()
    window._bridge_ready = True
    window._app_server_ready = True
    window._on_usage_poll_timeout()
    client.failure("usage", "Bridge unavailable")

    assert window.usage_status_label.text() == known
    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week —"
    window.close()


def test_codex_status_shows_update_check_controls_before_first_check() -> None:
    _application()
    window = MainWindow(_config(), api_client=FakeClient(), tray_available=False)

    labels = {label.text() for label in window.status_dialog.findChildren(QLabel)}
    buttons = {button.text() for button in window.status_dialog.findChildren(QPushButton)}

    assert "Not checked" in labels
    assert "Check for updates" in buttons
    assert "Update" in buttons
    window.close()


def test_auto_update_check_runs_once_after_ready_and_resolved_codex() -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    resolution = CodexResolution("C:/Codex/codex.exe", "1.2.3", "path")
    codex_probe.result(resolution)
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.codex_update_auto_timer.isActive()
    assert window.codex_update_auto_timer.interval() == 5_000
    assert updates.check_calls == []

    window._on_codex_update_auto_timeout()
    assert updates.check_calls == [resolution]
    updates.result(_available_update())
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    window._on_codex_update_auto_timeout()
    assert updates.check_calls == [resolution]
    window.close()


def test_auto_update_timeout_during_not_ready_waits_for_next_ready() -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    resolution = CodexResolution("C:/Codex/codex.exe", "1.2.3", "path")
    codex_probe.result(resolution)
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})

    window._on_codex_update_auto_timeout()

    assert not window._codex_update_auto_check_started
    assert updates.check_calls == []
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    assert window.codex_update_auto_timer.isActive()
    window._on_codex_update_auto_timeout()
    assert updates.check_calls == [resolution]
    window.close()


def _available_update() -> CodexUpdateInfo:
    return CodexUpdateInfo(
        current_version="1.2.3",
        latest_version="1.3.0",
        latest_status="ok",
        update_action="codex update",
        last_checked_at="2030-01-02T03:04:05Z",
        doctor_version="1.2.3",
    )


def test_update_available_shows_main_notification_and_enables_update() -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    resolution = CodexResolution("C:/Codex/codex.exe", "1.2.3", "codex_app")
    codex_probe.result(resolution)

    window.codex_update_check_button.click()
    assert updates.check_calls == [resolution]
    assert not window.codex_update_button.isEnabled()

    updates.result(_available_update())

    assert window.codex_latest_label.text() == "1.3.0"
    assert window.codex_update_status_label.text() == "Update available"
    assert window.codex_update_button.isEnabled()
    assert window.codex_update_banner_label.text() == "↑ Codex update available"
    assert not window.codex_update_banner_label.isHidden()
    window.close()


def test_update_button_stays_disabled_without_update_action_or_new_version() -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    codex_probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    window.codex_update_check_button.click()
    updates.result(CodexUpdateInfo("1.2.3", "1.3.0", "ok", "manual or unknown", None, "1.2.3"))
    assert not window.codex_update_button.isEnabled()
    assert not window.codex_update_banner_label.isHidden()

    window.codex_update_check_button.click()
    updates.result(CodexUpdateInfo("1.2.3", "1.2.3", "ok", "codex update", None, "1.2.3"))
    assert window.codex_update_status_label.text() == "Up to date"
    assert not window.codex_update_button.isEnabled()
    window.close()


def test_update_check_and_update_cannot_be_started_twice(monkeypatch) -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    codex_probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))

    window.codex_update_check_button.click()
    window.codex_update_check_button.click()
    assert len(updates.check_calls) == 1
    updates.result(_available_update())

    monkeypatch.setattr(
        "codex_bridge.console.main_window.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    window.codex_update_button.click()
    window.codex_update_button.click()
    assert len(updates.update_calls) == 1
    window.close()


def test_update_failure_and_success_are_shown_without_restart(monkeypatch) -> None:
    _application()
    client = FakeClient()
    codex_probe = FakeCodexProbe()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=codex_probe,
        codex_update_probe=updates,
        tray_available=False,
    )
    codex_probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    window.codex_update_check_button.click()
    updates.result(_available_update())
    monkeypatch.setattr(
        "codex_bridge.console.main_window.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    window.codex_update_button.click()
    updates.update_failure("Codex update failed")
    assert window.codex_update_status_label.text() == "Update failed"
    assert window.codex_update_message_label.text() == "Codex update failed"
    assert window.codex_update_button.isEnabled()

    window.codex_update_button.click()
    updates.update_success()
    assert window.codex_update_status_label.text() == "Update completed; restart required"
    assert not window.codex_update_button.isEnabled()
    assert window.codex_update_banner_label.isHidden()
    window.close()


def test_usage_failure_is_not_retried_while_connection_stays_ready() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    client.requests.clear()
    client.failure("usage", "Bridge unavailable")

    assert window.usage_status_label.text() == "Codex Usage  unavailable"
    assert not any(key == "usage" for key, _, _ in client.requests)
    window.close()


def test_partial_connection_state_uses_error_marker() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "failed"})

    assert window.overall_status_label.text() == "● Error"
    window.close()


def test_main_window_usage_renders_remaining_values_and_reset_tooltip() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result(
        "usage",
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "windowDurationMins": 10080,
                        "usedPercent": 39,
                        "resetsAt": "2030-01-09T03:04:05Z",
                    },
                    "secondary": {
                        "windowDurationMins": 300,
                        "usedPercent": 28,
                        "resetsAt": "2030-01-02T03:04:05Z",
                    },
                }
            }
        },
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 61%"
    assert "5h: 72% left" in window.usage_status_label.toolTip()
    assert "2030-01-02 " in window.usage_detail_label.text()
    window.close()


def test_main_window_uses_the_application_icon() -> None:
    application = _application()
    pixmap = QPixmap(16, 16)
    pixmap.fill()
    icon = QIcon(pixmap)
    application.setWindowIcon(icon)

    window = MainWindow(_config(), api_client=FakeClient(), tray_available=False)

    assert window.windowIcon().cacheKey() == icon.cacheKey()
    window.close()


def test_ready_bridge_with_no_threads_has_non_error_empty_state() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result("threads", {"threads": []})
    window.select_thread(None)

    assert window.history_pane._empty_label.text() == "No threads found."
    assert window.activity_pane.state_label.text() == "No thread selected."
    assert window.stream_status_label.text() == "Stream: idle"
    window.close()


def test_ready_bridge_with_threads_and_no_selection_prompts_selection() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result("threads", {"threads": [{"id": "thread-a", "name": "A"}]})
    window.select_thread(None)

    assert window.history_pane._empty_label.text() == "Select a thread to view history."
    assert window.activity_pane.state_label.text() == "Select a thread to view activity."
    window.close()


def test_main_window_tracks_selected_turn_activity_without_refresh_fanout() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result(
        "threads",
        {"threads": [{"id": "thread-a", "name": "A", "preview": "ignored"}]},
    )
    window.thread_pane.list_widget.setCurrentRow(0)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))

    for state in ("in_progress", "needs_approval", "needs_user_input", "needs_input"):
        client.result(
            status_key,
            {"thread_id": "thread-a", "state": state, "recent_activities": []},
        )
        assert "thread-a" in window._active_thread_ids

    client.result(
        status_key,
        {"thread_id": "thread-a", "state": "completed", "recent_activities": []},
    )
    assert "thread-a" not in window._active_thread_ids

    client.requests.clear()
    window.refresh()
    assert sum(path.endswith("/turns") for _, path, _ in client.requests) == 1
    window.close()


def test_main_window_keeps_active_style_and_updates_name_on_thread_refresh() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("threads", {"threads": [{"id": "thread-a"}]})
    window.thread_pane.list_widget.setCurrentRow(0)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {"thread_id": "thread-a", "state": "in_progress", "recent_activities": []},
    )

    active_item = window.thread_pane.list_widget.item(0)
    assert active_item.text() == "New スレッド"
    active_weight = active_item.font().weight()

    client.result("threads", {"threads": [{"id": "thread-a", "name": "Renamed"}]})

    current = window.thread_pane.list_widget.currentItem()
    assert current is not None
    assert current.data(Qt.ItemDataRole.UserRole) == "thread-a"
    assert current.text() == "Renamed"
    assert current.font().weight() == active_weight
    window.close()


def test_rename_uses_right_clicked_thread_and_updates_after_success(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result(
        "threads",
        {
            "threads": [
                {"id": "thread-a", "name": "A"},
                {"id": "thread-b", "name": "B"},
            ]
        },
    )
    window.select_thread("thread-a")
    monkeypatch.setattr(
        "codex_bridge.console.main_window.QInputDialog.getText",
        lambda *args: ("Renamed", True),
    )

    window.thread_pane.thread_rename_requested.emit("thread-b")

    assert window._selected_thread_id == "thread-a"
    assert client.json_posts == [
        ("rename:thread-b", "/ui-api/threads/thread-b/name", {"name": "Renamed"})
    ]
    client.result("rename:thread-b", {})

    assert window.thread_pane.list_widget.item(1).text() == "Renamed"
    window.close()


@pytest.mark.parametrize("dialog_result", [("", False), ("   ", True)])
def test_rename_cancel_or_blank_does_not_call_api(monkeypatch, dialog_result) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result("threads", {"threads": [{"id": "thread-b", "name": "B"}]})
    monkeypatch.setattr(
        "codex_bridge.console.main_window.QInputDialog.getText",
        lambda *args: dialog_result,
    )

    window.thread_pane.thread_rename_requested.emit("thread-b")

    assert client.json_posts == []
    window.close()


def test_open_in_codex_app_uses_right_clicked_thread_id_and_preserves_selection() -> None:
    _application()
    client = FakeClient()
    opened: list[str] = []
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        codex_uri_opener=lambda uri: opened.append(uri) or True,
    )
    client.result(
        "threads",
        {
            "threads": [
                {"id": "thread-a", "name": "A"},
                {"id": "thread-b", "name": "B"},
            ]
        },
    )
    window.select_thread("thread-a")
    window.thread_pane.list_widget.setCurrentRow(0)

    menu = window.thread_pane._context_menu_for_item(window.thread_pane.list_widget.item(1))
    action = next(action for action in menu.actions() if action.text() == "Open in Codex App")
    action.trigger()

    assert opened == ["codex://threads/thread-b"]
    assert window._selected_thread_id == "thread-a"
    current = window.thread_pane.list_widget.currentItem()
    assert current is not None
    assert current.data(Qt.ItemDataRole.UserRole) == "thread-a"
    assert client.json_posts == []
    window.close()


def test_open_in_codex_app_default_helper_passes_exact_uri_to_qt_shell(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result("threads", {"threads": [{"id": "thread-a", "name": "A"}]})
    window.select_thread("thread-a")

    actions = window.thread_pane._context_menu_for_item(
        window.thread_pane.list_widget.item(0)
    ).actions()
    assert "Open in Codex App" in [action.text() for action in actions]
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )

    action = next(action for action in actions if action.text() == "Open in Codex App")
    action.trigger()

    assert opened == ["codex://threads/thread-a"]
    window.close()


def _raise_uri_error(_uri: str) -> bool:
    raise OSError("shell unavailable")


@pytest.mark.parametrize("opener", [lambda _uri: False, _raise_uri_error])
def test_open_in_codex_app_failure_is_visible_without_changing_thread_state(opener) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        codex_uri_opener=opener,
    )
    client.result("threads", {"threads": [{"id": "thread-a", "name": "A"}]})
    window.select_thread("thread-a")
    window._active_thread_ids.add("thread-a")
    initial_title = window.thread_pane.thread_name("thread-a")
    initial_generation = window._selection_generation

    menu = window.thread_pane._context_menu_for_item(window.thread_pane.list_widget.item(0))
    action = next(action for action in menu.actions() if action.text() == "Open in Codex App")
    action.trigger()

    assert "Could not open thread in Codex App" in window.bottom_status_label.text()
    assert window._selected_thread_id == "thread-a"
    assert window._selection_generation == initial_generation
    assert window.thread_pane.thread_name("thread-a") == initial_title
    assert "thread-a" in window._active_thread_ids
    assert client.json_posts == []
    window.close()


def test_main_window_updates_active_threads_from_sse_lifecycle_events() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")

    client.activity(
        1,
        {
            "activity_id": "started",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_started",
            "status": "in_progress",
            "summary": "Turn started",
            "details": {},
        },
    )
    assert "thread-a" in window._active_thread_ids

    client.activity(
        1,
        {
            "activity_id": "finished",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
            "summary": "Turn completed",
            "details": {},
        },
    )
    assert "thread-a" not in window._active_thread_ids

    for index, activity_type in enumerate(("approval_requested", "user_input_requested")):
        client.activity(
            1,
            {
                "activity_id": f"requested-{index}",
                "thread_id": "thread-a",
                "turn_id": "turn-a",
                "type": activity_type,
                "status": "requested",
                "summary": "Input requested",
                "details": {},
            },
        )
        assert "thread-a" in window._active_thread_ids

    window.close()


def test_deselect_does_not_restore_bridge_unavailable_after_ready_thread() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result("threads", {"threads": [{"id": "thread-a"}]})
    window.select_thread("thread-a")
    window.select_thread(None)

    assert "not available" not in window.history_pane._empty_label.text()
    assert window.stream_status_label.text() == "Stream: idle"
    window.close()


def test_main_window_requests_snapshot_and_applies_connected_status() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    keys = {key for key, _, _ in client.requests}
    assert any(key.endswith(":detail") for key in keys)
    assert any(key.endswith(":turns") for key in keys)
    assert any(key.endswith(":items") for key in keys)
    status_key = next(key for key in keys if key.endswith(":status"))
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    client.result(status_key, {"state": "completed", "recent_activities": []})

    assert "connected" in window.bridge_status_label.text().casefold()
    assert "ready" in window.app_server_status_label.text().casefold()
    assert client.streams == [("thread-a", 1)]
    window.close()


def test_history_header_includes_turn_model_metadata() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    items_key = next(key for key, _, _ in client.requests if key.endswith(":items"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "thread_metadata": {"model": "gpt-5", "reasoning_effort": "high"},
            "state": "completed",
            "recent_activities": [],
        },
    )
    client.result(
        items_key,
        {
            "items": [
                {
                    "turn_id": "turn-1",
                    "item": {"id": "agent-1", "type": "agentMessage", "text": "answer"},
                }
            ],
            "turn_model_metadata": {
                "turn-1": {
                    "model_candidates": [{"model": "gpt-5", "reasoning_effort": "high"}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )

    headers = {label.text() for label in window.history_pane._content.findChildren(QLabel)}
    assert "Turn · Model: gpt-5 (high)" in headers
    assert "Agent · Model: gpt-5 · Reasoning: high" not in headers
    window.close()


def test_health_poll_also_refreshes_bridge_status_and_recovers_without_refresh() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.failure("bridge-status", "Bridge unavailable")
    assert window.bridge_status_label.text() == "Bridge: disconnected"
    assert window.app_server_status_label.text() == "App Server: failed"

    client.requests.clear()
    window._request_health()

    assert [key for key, _, _ in client.requests] == ["health", "bridge-status"]
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.bridge_status_label.text() == "Bridge: connected"
    assert window.app_server_status_label.text() == "App Server: ready"
    window.close()


def test_old_selection_json_and_sse_cannot_update_new_selection() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {"items": [{"turn_id": "a", "item": _item("a", "agentMessage", text="A only")}]},
    )
    window.select_thread("thread-b")
    client.result(
        "selection:1:items",
        {"items": [{"turn_id": "a", "item": _item("stale", "agentMessage", text="STALE A")}]},
    )
    client.activity(1, _activity("stale-activity", "thread-a"))
    client.activity(2, _activity("current-activity", "thread-b"))

    assert (
        "STALE A"
        not in window.history_pane._content.findChildren(type(window.history_pane._empty_label))[
            -1
        ].text()
    )
    assert window.activity_pane.activity_list.count() == 1
    assert "current-activity" in window.activity_pane.activity_list.item(0).text()
    window.close()


def test_history_pagination_prepends_older_page_and_deduplicates() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {
            "items": [{"turn_id": "t2", "item": _item("new", "agentMessage", text="new")}],
            "next_cursor": "older",
            "turn_model_metadata": {
                "t2": {
                    "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )
    window.history_pane.older_requested.emit()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "t2", "item": _item("new", "agentMessage", text="new")},
                {"turn_id": "t1", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
            "turn_model_metadata": {
                "t2": {
                    "model_candidates": [{"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"}],
                    "model_resolution_status": "resolved",
                },
                "t1": {
                    "model_candidates": [{"model": "gpt-5.6-sol", "reasoning_effort": "high"}],
                    "model_resolution_status": "resolved",
                },
            },
        },
    )

    assert window.history_pane.load_older_button.isVisible() is False
    assert len(window._timeline_entries) == 2
    assert [entry.item_id for entry in window._timeline_entries] == ["old", "new"]
    assert set(window._turn_model_metadata) == {"t1", "t2"}
    window.close()


def test_main_window_resets_turn_model_metadata_when_selection_changes() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {
            "items": [],
            "turn_model_metadata": {
                "turn-a": {
                    "model_candidates": [{"model": "gpt-5", "reasoning_effort": None}],
                    "model_resolution_status": "resolved",
                }
            },
        },
    )
    assert "turn-a" in window._turn_model_metadata

    window.select_thread("thread-b")

    assert window._turn_model_metadata == {}
    window.close()


def test_main_window_keeps_history_when_turn_model_metadata_is_unavailable() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    client.result(
        "selection:1:items",
        {
            "items": [
                {"turn_id": "turn-1", "item": _item("agent-1", "agentMessage", text="answer")}
            ],
            "turn_model_metadata": {
                "turn-1": {
                    "model_candidates": [],
                    "model_resolution_status": "unavailable",
                }
            },
        },
    )

    cards = [
        card
        for card in window.history_pane._content.findChildren(QFrame)
        if card.objectName() == "historyCard"
    ]
    assert len(cards) == 1
    assert any(body.text() == "answer" for body in cards[0].findChildren(QLabel))
    assert "Turn · Model: unavailable" in {
        label.text() for label in window.history_pane._content.findChildren(QLabel)
    }
    window.close()


def test_activity_is_deduplicated_and_bounded() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-b")
    for index in range(201):
        client.activity(1, _activity(f"activity-{index}"))
    client.activity(1, _activity("activity-200"))

    assert window.activity_pane.activity_list.count() == 200
    assert "activity-0" not in window.activity_pane.activity_list.item(0).text()
    window.close()


def test_periodic_status_snapshot_merges_with_sse_activities_without_loss_or_duplicates() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-b")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    activity_a = _activity("activity-a")
    activity_b = _activity("activity-b")

    client.result(
        status_key,
        {"thread_id": "thread-b", "state": "running", "recent_activities": [activity_a]},
    )
    client.activity(1, activity_b)
    client.result(
        status_key,
        {"thread_id": "thread-b", "state": "running", "recent_activities": [activity_a]},
    )

    assert window.activity_pane.activity_list.count() == 2
    assert "activity-a" in window.activity_pane.activity_list.item(0).text()
    assert "activity-b" in window.activity_pane.activity_list.item(1).text()

    client.result(
        status_key,
        {
            "thread_id": "thread-b",
            "state": "running",
            "recent_activities": [activity_a, activity_b],
        },
    )

    assert window.activity_pane.activity_list.count() == 2
    window.close()


def test_close_stops_timers_and_aborts_only_client_replies() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.close()

    assert client.aborted_all
    assert not window.health_timer.isActive()
    assert not window.thread_timer.isActive()
    assert not window.selected_status_timer.isActive()


def test_existing_ready_bridge_is_external_and_start_is_disabled() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "codex_app"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert probe.starts == 1
    assert window.codex_status_label.text() == "Codex: 1.2.3 · codex_app"
    assert window.runtime_state == "external"
    assert window.runtime_status_label.text() == "Runtime: external"
    assert not window.start_bridge_button.isEnabled()
    assert launcher.calls == []
    window.close()


def test_valid_detected_codex_enables_start_when_bridge_is_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.codex_status_label.text() == "Codex: 1.2.3 · path"
    assert window.runtime_state == "launching"
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_empty_allowed_roots_keep_start_disabled_even_with_codex_resolution() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    window = MainWindow(ConsoleConfig(), api_client=client, codex_probe=probe, tray_available=False)
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert not window.start_bridge_button.isEnabled()
    assert "roots" in window.config_status_label.text().casefold()
    window.close()


def test_start_bridge_is_detached_once_and_readiness_marks_console_started() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "explicit"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    window.start_bridge_button.click()

    assert window.runtime_state == "launching"
    assert window.runtime_status_label.text() == "Runtime: launching"
    assert not window.start_bridge_button.isEnabled()
    assert not window.retry_now_button.isEnabled()
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert [key for key, _, _ in client.requests if key.startswith("launch:")] == [
        "launch:health",
        "launch:status",
    ]

    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "connected", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.runtime_status_label.text() == "Runtime: started by Console"
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_managed_launch_readiness_starts_usage_sequence() -> None:
    window, client, _launcher = _owned_window()

    assert window._usage_ready
    assert window.usage_initial_timer.isActive()
    assert window.usage_poll_timer.isActive()

    window._on_usage_initial_timeout()

    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


def test_successful_detached_launch_never_reenables_after_temporary_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "launching"
    assert not window.start_bridge_button.isEnabled()
    window.start_bridge_button.click()
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    window.close()


def test_managed_launch_readiness_timeout_schedules_retry() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window._readiness_deadline = 0.0
    window._on_readiness_tick()

    assert window.runtime_state == "launch_failed"
    assert window.runtime_status_label.text() == "Runtime: launch failed"
    assert not window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()
    assert window.bridge_start_retry_timer.interval() == 2_000
    window.close()


def test_close_aborts_probe_and_readiness_only_without_stopping_bridge() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()

    window.close()

    assert client.control_requests
    assert not client.aborted_all
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert client.aborted_all
    assert probe.aborts == 1
    assert launcher.closes == 1
    assert not window.readiness_timer.isActive()


def test_start_stays_disabled_until_both_bridge_observations_confirm_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))

    assert not window.start_bridge_button.isEnabled()
    client.failure("health", "Bridge unavailable")
    assert not window.start_bridge_button.isEnabled()
    client.failure("bridge-status", "Bridge unavailable")
    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_unavailable_bridge_with_valid_config_auto_starts_once() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert launcher.calls == [("C:/Codex/codex.exe", 8001)]
    assert window.runtime_state == "launching"
    window.close()


def test_managed_bridge_launch_failures_use_bounded_retry_schedule() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()
    assert window.bridge_start_retry_timer.interval() == 2_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 2
    assert window.bridge_start_retry_timer.interval() == 5_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 3
    assert window.bridge_start_retry_timer.interval() == 10_000

    window._on_bridge_start_retry_timeout()
    assert len(launcher.calls) == 4
    assert not window.bridge_start_retry_timer.isActive()
    assert window.runtime_state == "launch_failed"
    window.close()


def test_retry_now_cancels_managed_bridge_wait_and_attempts_immediately() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tray_available=False,
    )

    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert window.bridge_start_retry_timer.isActive()

    launcher.started = True
    window._retry_now()

    assert len(launcher.calls) == 2
    assert not window.bridge_start_retry_timer.isActive()
    assert window.runtime_state == "launching"
    window.close()


def _set_ready_components(window: MainWindow, tunnel_state: str = "ready") -> None:
    window._health_ok = True
    window._health_observed = True
    window._status_observed = True
    window._bridge_ready = True
    window._app_server_ready = True
    window._runtime_state = "external"
    window._tunnel_state = tunnel_state


def test_overall_is_starting_during_managed_start_or_tunnel_recovery() -> None:
    _application()
    tunnel = LifecycleTunnel("starting")
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )

    _set_ready_components(window, tunnel_state="starting")
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Starting"

    window._runtime_state = "launching"
    window._health_ok = False
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Starting"
    window.close()


def test_overall_is_ready_only_when_bridge_app_server_and_tunnel_are_usable() -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )

    _set_ready_components(window)
    window._sync_overall_status()
    assert window.overall_status_label.text() == "● Ready"

    window._app_server_ready = False
    window._sync_overall_status()
    assert window.overall_status_label.text() != "● Ready"
    window.close()


def test_tunnel_failure_with_local_bridge_ready_is_degraded() -> None:
    _application()
    tunnel = RecoveringLifecycleTunnel("failed")
    tunnel.recovery_timer.start(60_000)
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )

    _set_ready_components(window, tunnel_state="failed")
    window._runtime_state = "console_started"
    window._sync_overall_status()

    assert window.overall_status_label.text() == "● Degraded"
    window.close()


def test_usage_failure_does_not_change_ready_to_degraded() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    _set_ready_components(window)
    window._sync_overall_status()

    client.failure("usage", "Usage unavailable")

    assert window.overall_status_label.text() == "● Ready"
    window.close()


def test_update_check_failure_does_not_change_ready_to_degraded() -> None:
    _application()
    updates = FakeCodexUpdateProbe()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_update_probe=updates,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    _set_ready_components(window)
    window._sync_overall_status()

    updates.failure()

    assert window.overall_status_label.text() == "● Ready"
    window.close()


def test_retry_now_routes_to_bridge_when_bridge_unavailable() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher(started=False)
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=LifecycleTunnel("unavailable"),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    assert len(launcher.calls) == 1

    window._retry_now()

    assert len(launcher.calls) == 2
    window.close()


def test_retry_now_routes_to_tunnel_when_bridge_ready_but_tunnel_unavailable() -> None:
    _application()
    tunnel = LifecycleTunnel("failed")
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tunnel_supervisor=tunnel,
        tray_available=False,
    )
    _set_ready_components(window, tunnel_state="failed")
    window._runtime_state = "external"
    window._sync_overall_status()

    window._retry_now()

    assert tunnel.retry_calls == 1
    window.close()


def test_restart_codexbridge_uses_existing_managed_restart_path() -> None:
    window, client, launcher = _owned_window()

    assert window.restart_codexbridge_button.isEnabled()
    window.restart_codexbridge_button.click()

    assert window.runtime_state == "restarting"
    assert not window.retry_now_button.isEnabled()
    assert len(client.control_requests) == 1
    assert len(launcher.calls) == 1
    window.close()


def test_restart_codexbridge_is_disabled_for_ready_external_bridge() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        tunnel_supervisor=LifecycleTunnel("ready"),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert not window.restart_codexbridge_button.isEnabled()
    assert "external" in window.restart_codexbridge_button.toolTip().casefold()
    window.close()


def test_advanced_controls_are_hidden_by_default_and_preserve_individual_actions() -> None:
    _application()
    window = MainWindow(_config(), api_client=FakeClient(), tray_available=False)

    assert window.advanced_controls.isHidden()
    assert window.start_bridge_button.parentWidget() is window.advanced_controls
    assert window.start_tunnel_button.parentWidget() is window.advanced_controls

    window.advanced_toggle_button.click()

    assert not window.advanced_controls.isHidden()
    assert window.start_bridge_button.text() == "Start Bridge"
    assert window.stop_tunnel_button.text() == "Stop Tunnel"
    window.close()


def _owned_window(
    tunnel: StableTunnel | None = None,
) -> tuple[MainWindow, FakeClient, FakeLauncher]:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    tunnel = tunnel or StableTunnel()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=tunnel,
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})
    return window, client, launcher


def test_owned_ready_bridge_enables_stop_and_restart_with_process_local_token() -> None:
    window, client, launcher = _owned_window()

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    assert len(launcher.control_tokens) == 1
    assert len(launcher.control_tokens[0]) >= 32
    assert client.control_requests == []

    window.stop_bridge_button.click()

    assert window.runtime_state == "stopping"
    assert len(client.control_requests) == 1
    assert not window.stop_bridge_button.isEnabled()
    window.close()


def test_external_bridge_keeps_bridge_controls_disabled() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "external"
    assert not window.start_bridge_button.isEnabled()
    assert not window.stop_bridge_button.isEnabled()
    assert not window.restart_bridge_button.isEnabled()
    assert launcher.calls == []
    window.close()


def _fail_bridge_observation(client: FakeClient) -> None:
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")


def test_one_failed_bridge_status_observation_does_not_restart_owned_bridge() -> None:
    window, client, launcher = _owned_window()

    client.failure("health", "Bridge unavailable")
    assert window._bridge_loss_count == 0
    client.failure("bridge-status", "Bridge unavailable")

    assert window._bridge_loss_count == 1
    assert client.control_requests == []
    assert len(launcher.calls) == 1
    window.close()


def test_two_failed_bridge_status_observations_restart_owned_bridge() -> None:
    window, client, launcher = _owned_window()

    _fail_bridge_observation(client)
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 2
    assert window.runtime_state == "restarting"
    assert len(client.control_requests) == 1
    assert len(launcher.calls) == 1
    window.close()


def test_successful_bridge_status_observation_resets_loss_counter() -> None:
    window, client, _launcher = _owned_window()

    _fail_bridge_observation(client)
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 1
    assert client.control_requests == []
    window.close()


def test_two_failed_external_bridge_status_observations_take_over_without_shutdown() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    _fail_bridge_observation(client)
    _fail_bridge_observation(client)

    assert window._bridge_loss_count == 2
    assert window.runtime_state == "launching"
    assert len(launcher.calls) == 1
    assert client.control_requests == []
    assert window._detached_launch_started
    window.close()


def test_stop_requires_bridge_disappearance_after_202_and_clears_ownership() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")

    assert window.runtime_state == "stopping"
    assert window._control_token == old_token
    client.failure("health", "Bridge unavailable")
    assert window.runtime_state == "stopping"
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert window._control_token is None
    assert window._detached_pid is None
    assert not window._detached_launch_started
    assert window.start_bridge_button.isEnabled()
    window.start_bridge_button.click()

    assert len(launcher.control_tokens) == 2
    assert launcher.control_tokens[1] != old_token
    window.close()


def test_stop_timeout_is_fail_closed_and_does_not_clear_token_or_relaunch() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    assert window.runtime_state == "stop_timed_out"
    assert window._control_token is not None
    assert window._detached_launch_started
    assert len(launcher.calls) == 1
    assert client.control_requests == [(window._control_token, "control:shutdown")]
    window.close()


def test_launch_timeout_recovers_to_owned_ready_on_late_normal_poll() -> None:
    _application()
    client = FakeClient()
    probe = FakeCodexProbe()
    launcher = FakeLauncher()
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        runtime_launcher=launcher,
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )
    probe.result(CodexResolution("C:/Codex/codex.exe", "1.2.3", "path"))
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    window.start_bridge_button.click()
    old_token = launcher.control_tokens[0]

    window._readiness_deadline = 0.0
    window._on_readiness_tick()

    assert window.runtime_state == "launch_failed"
    assert not window._bridge_transition
    assert not window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window.bridge_start_retry_timer.isActive()

    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    assert window._control_token == old_token
    window.close()


def test_stop_timeout_recovers_to_owned_ready_without_relaunch() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    assert window.runtime_state == "stop_timed_out"
    client.result("health", {"status": "ok"})
    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window._control_token == old_token
    assert len(launcher.calls) == 1
    assert window.stop_bridge_button.isEnabled()
    assert window.restart_bridge_button.isEnabled()
    window.close()


def test_stop_timeout_late_unavailable_confirms_stopped_and_allows_start() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert window._control_token is None
    assert window._detached_pid is None
    assert not window._detached_launch_started
    assert window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    window.close()


def test_restart_timeout_late_stop_does_not_automatically_relaunch() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    window._stop_confirmation_deadline = 0.0
    window._on_stop_confirmation_tick()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "stopped"
    assert len(launcher.calls) == 1
    assert tunnel.started == 0
    assert window.start_bridge_button.isEnabled()
    window.close()


def test_unexpected_unreachable_owned_bridge_does_not_reenable_start() -> None:
    window, client, _launcher = _owned_window()

    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "console_started_unreachable"
    assert not window.start_bridge_button.isEnabled()
    assert not window.stop_bridge_button.isEnabled()
    assert not window.restart_bridge_button.isEnabled()
    window.close()


def test_control_failure_does_not_trigger_duplicate_launch() -> None:
    window, client, launcher = _owned_window()

    window.stop_bridge_button.click()
    client.control_failure("control:shutdown")

    assert window.runtime_state == "control_failed"
    assert len(launcher.calls) == 1
    assert not window.start_bridge_button.isEnabled()
    window.close()


def test_restart_relaunches_after_confirmed_stop_with_fresh_token() -> None:
    window, client, launcher = _owned_window()
    old_token = launcher.control_tokens[0]

    window.restart_bridge_button.click()
    assert window.runtime_state == "restarting"
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert len(launcher.calls) == 2
    assert launcher.control_tokens[1] != old_token
    assert window.runtime_state == "launching"
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert window.runtime_state == "console_started"
    assert window.stop_bridge_button.isEnabled()
    window.close()


def test_restart_restores_managed_tunnel_once_after_new_bridge_ready() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)

    window.restart_bridge_button.click()
    assert tunnel.stop_calls == 1
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert tunnel.started == 1
    assert len(client.control_requests) == 1
    assert len(launcher.control_tokens) == 2
    window.close()


def test_restart_with_stopped_tunnel_does_not_start_it_after_readiness() -> None:
    window, client, launcher = _owned_window()

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")
    client.result("launch:health", {"status": "ok"})
    client.result("launch:status", {"bridge": "ready", "app_server": "ready"})

    assert window._tunnel.started == 0
    assert len(launcher.control_tokens) == 2
    window.close()


def test_restart_relaunch_failure_does_not_start_managed_tunnel() -> None:
    tunnel = ManagedTunnel()
    window, client, launcher = _owned_window(tunnel)
    launcher.started = False

    window.restart_bridge_button.click()
    client.control_success("control:shutdown")
    client.failure("health", "Bridge unavailable")
    client.failure("bridge-status", "Bridge unavailable")

    assert window.runtime_state == "launch_failed"
    assert tunnel.started == 0
    assert len(launcher.control_tokens) == 2
    window.close()
