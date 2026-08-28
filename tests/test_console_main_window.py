from __future__ import annotations

from typing import Any

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QSplitter

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.runtime_launcher import DetachedLaunchResult


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
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.streams: list[tuple[str, int]] = []
        self.aborted_groups: list[str] = []
        self.stopped_streams = 0
        self.aborted_all = False

    def get_json(self, path: str, *, key: str, query: dict[str, object] | None = None) -> bool:
        self.requests.append((key, path, query))
        return True

    def abort_json_group(self, prefix: str) -> None:
        self.aborted_groups.append(prefix)

    def start_stream(self, thread_id: str, generation: int) -> None:
        self.streams.append((thread_id, generation))

    def stop_stream(self) -> None:
        self.stopped_streams += 1

    def abort_all(self) -> None:
        self.aborted_all = True

    def result(self, key: str, payload: object) -> None:
        self.json_succeeded.emit(key, payload)

    def failure(self, key: str, message: str) -> None:
        self.json_failed.emit(key, message)

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


class FakeLauncher:
    def __init__(self, *, started: bool = True) -> None:
        self.started = started
        self.calls: list[tuple[str, int]] = []
        self.closes = 0

    def launch(self, *, codex_executable: str, ui_port: int) -> DetachedLaunchResult:
        self.calls.append((codex_executable, ui_port))
        return DetachedLaunchResult(self.started, 1234 if self.started else None)

    def close(self) -> None:
        self.closes += 1


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _config() -> ConsoleConfig:
    return ConsoleConfig()


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
        },
    )

    assert window.history_pane.load_older_button.isVisible() is False
    assert len(window._timeline_entries) == 2
    assert [entry.item_id for entry in window._timeline_entries] == ["old", "new"]
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
    assert window.runtime_state == "unavailable"
    assert window.start_bridge_button.isEnabled()
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


def test_launch_readiness_timeout_does_not_retry_or_enable_start() -> None:
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
    window._readiness_deadline = 0.0
    window._on_readiness_tick()

    assert window.runtime_state == "launch_timed_out"
    assert window.runtime_status_label.text() == "Runtime: launch timed out"
    assert not window.start_bridge_button.isEnabled()
    assert len(launcher.calls) == 1
    client.failure("health", "Bridge unavailable")
    assert window.runtime_status_label.text() == "Runtime: launch timed out"
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
    assert window.start_bridge_button.isEnabled()
    window.close()
