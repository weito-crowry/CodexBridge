from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidgetItem,
    QWidget,
)

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console import usage_history_window as usage_history_window_module
from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_updates import CodexUpdateInfo
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.runtime_launcher import DetachedLaunchResult
from codex_bridge.console.tunnel_supervisor import TunnelActionState
from codex_bridge.console.usage_history import (
    get_recent_usage_events,
    get_usage_samples,
)
from codex_bridge.console.widgets import TimelineEntry


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
        self.approval_requests: list[tuple[str, object, str, str]] = []
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

    def post_control_approval(
        self, token: str, *, request_id: object, decision: str, key: str
    ) -> bool:
        self.approval_requests.append((token, request_id, decision, key))
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


class FakeDiagnosticsReader:
    def __init__(self, batches: list[list[str]] | None = None) -> None:
        self.batches = list(batches or [])
        self.polls = 0
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def poll(self) -> list[str]:
        self.polls += 1
        return self.batches.pop(0) if self.batches else []


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


def _thread_item(window: MainWindow, thread_id: str) -> QTreeWidgetItem:
    tree = window.thread_pane.list_widget
    return next(
        tree.topLevelItem(group_index).child(child_index)
        for group_index in range(tree.topLevelItemCount())
        for child_index in range(tree.topLevelItem(group_index).childCount())
        if tree.topLevelItem(group_index).child(child_index).data(0, Qt.ItemDataRole.UserRole)
        == thread_id
    )


def _usage_window(client: FakeClient) -> MainWindow:
    return MainWindow(
        _config(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )


def _set_usage_ready(window: MainWindow) -> None:
    window._bridge_ready = True
    window._app_server_ready = True


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


def _wait_for_qt_timer(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def _history_activity(activity_type: str, activity_id: str, thread_id: str) -> dict[str, object]:
    return {
        **_activity(activity_id, thread_id),
        "type": activity_type,
        "status": "in_progress",
    }


def _complete_history_snapshot(client: FakeClient, generation: int) -> None:
    client.result(f"selection:{generation}:turns", {"turns": []})
    client.result(f"selection:{generation}:items", {"items": []})


def _history_items(count: int) -> dict[str, object]:
    return {
        "items": [
            {
                "turn_id": "turn-a",
                "item": _item(
                    f"item-{index}",
                    "agentMessage",
                    text="\n".join(f"message {index} line {line}" for line in range(8)),
                ),
            }
            for index in reversed(range(count))
        ]
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
    assert window.splitter.orientation() == Qt.Orientation.Horizontal
    assert window.splitter.count() == 3
    assert window.diagnostics_pane.isHidden()
    assert not window.diagnostics_timer.isActive()
    window.close()


def test_diagnostics_toggle_refreshes_immediately_and_stops_timer_when_collapsed() -> None:
    _application()
    reader = FakeDiagnosticsReader([["[Bridge] server.start"]])
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        diagnostics_reader=reader,
        tray_available=False,
    )

    window.diagnostics_toggle_button.click()

    assert not window.diagnostics_pane.isHidden()
    assert window.diagnostics_toggle_button.text() == "Hide Diagnostics"
    assert reader.resets == 1
    assert reader.polls == 1
    assert "[Bridge] server.start" in window.diagnostics_text.toPlainText()
    assert window.diagnostics_timer.isActive()
    assert window.diagnostics_timer.interval() == 1000

    window.diagnostics_toggle_button.click()

    assert window.diagnostics_pane.isHidden()
    assert window.diagnostics_toggle_button.text() == "Diagnostics"
    assert not window.diagnostics_timer.isActive()
    window.close()


def test_diagnostics_clear_only_clears_widget_and_future_logs_return(tmp_path) -> None:
    from codex_bridge.console.diagnostics import DiagnosticSource, DiagnosticsReader

    _application()
    path = tmp_path / "runtime.log"
    path.write_text("first line\n", encoding="utf-8")
    reader = DiagnosticsReader(
        sources=(DiagnosticSource("Bridge stdout", path, False),),
    )
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        diagnostics_reader=reader,
        tray_available=False,
    )
    window.diagnostics_toggle_button.click()
    original_contents = path.read_bytes()
    assert "first line" in window.diagnostics_text.toPlainText()

    window.diagnostics_clear_button.click()

    assert window.diagnostics_text.toPlainText() == ""
    assert path.read_bytes() == original_contents
    with path.open("a", encoding="utf-8") as stream:
        stream.write("after clear\n")
    window._on_diagnostics_timeout()
    assert "after clear" in window.diagnostics_text.toPlainText()
    window.close()


def test_diagnostics_reopen_rebuilds_recent_tail_without_duplicates(tmp_path) -> None:
    from codex_bridge.console.diagnostics import DiagnosticSource, DiagnosticsReader

    _application()
    path = tmp_path / "runtime.log"
    path.write_text("first\n", encoding="utf-8")
    reader = DiagnosticsReader(
        sources=(DiagnosticSource("Bridge stdout", path, False),),
    )
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        diagnostics_reader=reader,
        tray_available=False,
    )

    window.diagnostics_toggle_button.click()
    assert not window.diagnostics_pane.isHidden()
    assert window.diagnostics_timer.isActive()
    assert window.diagnostics_text.toPlainText().count("[Bridge stdout] first") == 1

    window.diagnostics_toggle_button.click()
    assert window.diagnostics_pane.isHidden()
    assert not window.diagnostics_timer.isActive()
    with path.open("a", encoding="utf-8") as stream:
        stream.write("second\n")

    window.diagnostics_toggle_button.click()
    text = window.diagnostics_text.toPlainText()
    assert not window.diagnostics_pane.isHidden()
    assert window.diagnostics_timer.isActive()
    assert text.count("[Bridge stdout] first") == 1
    assert text.count("[Bridge stdout] second") == 1
    window.close()


def test_diagnostics_widget_is_bounded_and_follows_only_when_at_bottom() -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        tray_available=False,
    )
    widget = window.diagnostics_text
    assert widget.isReadOnly()
    assert widget.maximumBlockCount() == 2000

    window._append_diagnostics_lines([f"line-{index}" for index in range(80)])
    scrollbar = widget.verticalScrollBar()
    scrollbar.setValue(0)
    window._append_diagnostics_lines(["while-scrolled-up"])
    assert scrollbar.value() == 0

    scrollbar.setValue(scrollbar.maximum())
    window._append_diagnostics_lines(["after-bottom"])
    assert scrollbar.value() == scrollbar.maximum()
    window.close()


def test_status_button_opens_detail_dialog_and_refresh_reloads_usage(tmp_path: Path) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )

    window.status_button.click()
    assert window.status_dialog.isVisible()

    client.requests.clear()
    window.status_refresh_button.click()

    assert any(
        key == "usage" and path == "/ui-api/account/rate-limits" for key, path, _ in client.requests
    )
    assert window._usage_request_mode == "manual"
    client.failure("usage", "manual refresh unavailable")
    assert "refresh failed" in window.usage_status_label.text()
    assert "manual refresh unavailable" in window.usage_detail_label.text()
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


def test_manual_usage_mode_survives_bridge_readiness_transition() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window._request_usage(force=True)
    assert window._usage_request_mode == "manual"

    client.result("bridge-status", {"bridge": "ready", "app_server": "ready"})

    assert window._usage_request_mode == "manual"
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
    assert window.usage_poll_timer.interval() == 60 * 1_000
    window._on_usage_initial_timeout()
    client.failure("usage", "Bridge unavailable")

    assert "refresh failed" in window.usage_status_label.text()
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
    known_font_weight = window.usage_status_label.font().weight()
    known_color = window.usage_status_label.palette().color(QPalette.ColorRole.WindowText)
    window._bridge_ready = True
    window._app_server_ready = True
    window._on_usage_poll_timeout()
    client.failure("usage", "Bridge unavailable")

    assert window.usage_status_label.text() == f"{known} · refresh failed"
    assert window.usage_status_label.text().startswith("Codex Usage  5h 72% · Week —")
    assert "Bridge unavailable" in window.usage_status_label.toolTip()
    assert "Bridge unavailable" in window.usage_detail_label.text()
    assert window.usage_status_label.font().weight() == known_font_weight
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == known_color
    assert window._usage.five_hour is not None
    assert window._usage.five_hour.remaining_percent == 72
    window.close()


def test_periodic_usage_success_updates_values_and_clears_failure_state() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}})
    window._bridge_ready = True
    window._app_server_ready = True
    window._on_usage_poll_timeout()
    client.failure("usage", "temporary failure")

    window._on_usage_poll_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 11}}},
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 89% · Week —"
    assert "refresh failed" not in window.usage_status_label.text()
    assert "temporary failure" not in window.usage_detail_label.text()
    window.close()


@pytest.mark.parametrize("mode", ["initial", "periodic", "manual"])
def test_canonical_usage_modes_update_status_and_save_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    _set_usage_ready(window)
    monkeypatch.setattr(main_window_module, "time", lambda: 120.0)
    window._usage_request_in_flight = True
    window._usage_request_mode = mode

    client.result(
        "usage",
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 28},
                    "secondary": {"windowDurationMins": 10080, "usedPercent": 82},
                }
            }
        },
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    assert [
        sample.weekly_remaining
        for sample in get_usage_samples(0.0, 180.0, database_path=database_path)
    ] == [18]
    assert get_recent_usage_events(database_path=database_path) == []
    window.close()


def test_usage_failure_does_not_save_history(tmp_path: Path) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"

    client.failure("usage", "temporary failure")

    assert not database_path.exists()
    window.close()


def test_success_with_unavailable_usage_does_not_save_history(tmp_path: Path) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )

    client.result("usage", {"rateLimits": {}})

    assert not database_path.exists()
    window.close()


def test_status_opens_one_independent_usage_history_window(tmp_path: Path) -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )
    window._show_status()

    assert window.status_dialog.isVisible()
    assert window.status_dialog.findChild(QWidget, "usageHistoryChart") is None
    assert window.usage_history_button.text() == "Show usage history"
    assert not window.status_dialog.isModal()

    window.usage_history_button.click()
    first_window = window._usage_history_window
    assert first_window is not None
    assert first_window.isVisible()
    window.status_dialog.close()
    assert first_window.isVisible()

    window.usage_history_button.click()
    assert window._usage_history_window is first_window
    window.close()


def test_main_window_exit_closes_its_usage_history_window(tmp_path: Path) -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        runtime_launcher=FakeLauncher(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        quit_application=lambda: None,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )
    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None and history_window.isVisible()

    window._begin_exit()

    assert not history_window.isVisible()
    window.close()


def test_usage_sample_refreshes_only_a_visible_history_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    current_epoch = [1_790_000_000.0]
    monkeypatch.setattr(main_window_module, "time", lambda: current_epoch[0])
    monkeypatch.setattr(usage_history_window_module, "time", lambda: current_epoch[0])
    _set_usage_ready(window)
    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None
    refresh_calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        history_window,
        "refresh",
        lambda *, rolling=False: refresh_calls.append({"rolling": rolling}),
    )

    def deliver_sample(weekly_used: int) -> None:
        window._usage_request_in_flight = True
        window._usage_request_mode = "periodic"
        client.result(
            "usage",
            {
                "rateLimits": {
                    "primary": {"windowDurationMins": 300, "usedPercent": 28},
                    "secondary": {
                        "windowDurationMins": 10080,
                        "usedPercent": weekly_used,
                    },
                }
            },
        )

    deliver_sample(70)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    history_window.hide()
    current_epoch[0] += 60
    deliver_sample(65)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    window._show_status()
    current_epoch[0] += 60
    deliver_sample(60)
    assert refresh_calls == [{"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )

    history_window.show()
    assert refresh_calls == [{"rolling": True}, {"rolling": True}]
    current_epoch[0] += 60
    deliver_sample(55)
    assert refresh_calls == [{"rolling": True}, {"rolling": True}, {"rolling": True}]
    assert (
        len(get_usage_samples(current_epoch[0] - 1, current_epoch[0], database_path=database_path))
        == 1
    )
    window.close()


@pytest.mark.parametrize("history_error", [sqlite3.OperationalError, OSError])
def test_usage_history_write_failure_keeps_usage_and_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_error: type[Exception],
) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )

    def fail_record(*args: object, **kwargs: object) -> None:
        raise history_error("history unavailable")

    monkeypatch.setattr(main_window_module, "record_usage_sample", fail_record)
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    _set_usage_ready(window)

    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )

    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week —"
    assert window.usage_poll_timer.isActive()
    window.close()


@pytest.mark.parametrize("history_error", [sqlite3.OperationalError, OSError])
def test_status_opens_and_usage_history_reports_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_error: type[Exception],
) -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )

    def fail_read(*args: object, **kwargs: object) -> None:
        raise history_error("history unavailable")

    monkeypatch.setattr(usage_history_window_module, "get_usage_samples_for_display", fail_read)

    window._show_status()
    window.usage_history_button.click()

    assert window.status_dialog.isVisible()
    assert window._usage_history_window is not None
    assert (
        window._usage_history_window.chart_stack.currentWidget()
        is window._usage_history_window.error_state_label
    )
    window.close()


def test_usage_failure_message_is_bounded() -> None:
    _application()
    window = _usage_window(FakeClient())

    window._apply_usage_failure("x" * 500)

    assert window._usage_refresh_error == "x" * 256
    assert len(window.usage_detail_label.text()) < 300
    window.close()


def test_completed_activity_schedules_one_delayed_snapshot_for_unique_turn() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.requests.clear()
    event = {
        "activity_id": "completed",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "type": "turn_completed",
        "status": "completed",
        "summary": "Turn completed",
    }

    client.activity(window._selection_generation, event)
    client.activity(window._selection_generation, event)

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert window.turn_usage_snapshot_timer.isActive()
    assert window.turn_usage_snapshot_timer.isSingleShot()
    assert window.turn_usage_snapshot_timer.interval() == 1_000
    assert not any(key == "usage" for key, _, _ in client.requests)
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window._usage_request_mode == "turn_snapshot"
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    client.activity(window._selection_generation, event)
    assert not window.turn_usage_snapshot_timer.isActive()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    window.close()


@pytest.mark.parametrize(
    ("activity_type", "status"),
    [
        ("turn_failed", "failed"),
        ("turn_interrupted", "interrupted"),
        ("error", "error"),
        ("turn_completed", "failed"),
        ("turn_completed", "interrupted"),
    ],
)
def test_non_completed_activity_does_not_schedule_usage_snapshot(
    activity_type: str, status: str
) -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "terminal",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": activity_type,
            "status": status,
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    window.close()


def test_completed_status_snapshot_does_not_capture_historical_turn_usage() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "completed",
            "recent_activities": [
                {
                    "activity_id": "old-completion",
                    "thread_id": "thread-a",
                    "turn_id": "turn-a",
                    "type": "turn_completed",
                    "status": "completed",
                }
            ],
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    assert not window._usage_snapshots
    window.close()


def test_completion_with_non_string_turn_id_does_not_schedule_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": 17,
            "type": "turn_completed",
            "status": "completed",
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    assert not window.turn_usage_snapshot_timer.isActive()
    window.close()


def test_simultaneous_completed_turns_share_one_usage_snapshot_request() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    for turn_id in ("turn-a", "turn-b"):
        client.activity(
            window._selection_generation,
            {
                "activity_id": f"completed-{turn_id}",
                "thread_id": "thread-a",
                "turn_id": turn_id,
                "type": "turn_completed",
                "status": "completed",
            },
        )

    assert len(window._pending_usage_snapshot_turns) == 2
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window._usage_snapshot_request_turns == {
        ("thread-a", "turn-a"),
        ("thread-a", "turn-b"),
    }
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    assert window._usage_snapshots[("thread-a", "turn-a")] is not None
    assert window._usage_snapshots[("thread-a", "turn-b")] is not None
    window.close()


@pytest.mark.parametrize("waiting_timer_name", ["usage_initial_timer", "usage_retry_timer"])
def test_turn_snapshot_wait_does_not_cancel_initial_usage_sequence(
    waiting_timer_name: str,
) -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._begin_usage_sequence()
    waiting_timer = getattr(window, waiting_timer_name)
    if waiting_timer_name == "usage_retry_timer":
        window.usage_initial_timer.stop()
        window._usage_attempts = 1
        waiting_timer.start()
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )

    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()

    assert waiting_timer.isActive()
    assert window._usage_sequence_active
    assert window._usage_request_mode is None
    assert not any(key == "usage" for key, _, _ in client.requests)
    assert window.turn_usage_snapshot_timer.interval() == 500
    window.close()


def test_turn_snapshot_waits_for_existing_usage_request_without_overwriting_mode() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._on_usage_poll_timeout()
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )

    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()

    assert window._usage_request_mode == "periodic"
    assert sum(key == "usage" for key, _, _ in client.requests) == 1
    assert window.turn_usage_snapshot_timer.isActive()
    assert window.turn_usage_snapshot_timer.interval() == 500
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 20}}},
    )
    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert window._usage_request_mode == "turn_snapshot"
    assert sum(key == "usage" for key, _, _ in client.requests) == 2
    window.close()


def test_turn_snapshot_is_saved_per_turn_without_changing_global_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    client = FakeClient()
    database_path = tmp_path / "usage-history.sqlite3"
    current_epoch = [1_790_000_000.0]
    monkeypatch.setattr(main_window_module, "time", lambda: current_epoch[0])
    monkeypatch.setattr(usage_history_window_module, "time", lambda: current_epoch[0])
    window = MainWindow(
        _config(),
        api_client=client,
        tray_available=False,
        usage_history_path=database_path,
    )
    _set_usage_ready(window)
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    periodic_payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 82},
            }
        }
    }
    client.result("usage", periodic_payload)
    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    assert [
        sample.weekly_remaining
        for sample in get_usage_samples(
            current_epoch[0] - 1, current_epoch[0] + 1, database_path=database_path
        )
    ] == [18]

    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None and history_window.isVisible()
    refresh_calls: list[dict[str, bool]] = []
    monkeypatch.setattr(
        history_window,
        "refresh",
        lambda *, rolling=False: refresh_calls.append({"rolling": rolling}),
    )
    current_epoch[0] += 60
    window._usage_request_in_flight = True
    window._usage_request_mode = "periodic"
    client.result("usage", periodic_payload)
    assert refresh_calls == [{"rolling": True}]
    global_usage = window._usage

    window.select_thread("thread-a")
    window._timeline_entries = [
        TimelineEntry("turn-a", "item-a", "Agent", "Agent", "answer", None, ())
    ]
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    assert window._usage_request_mode == "turn_snapshot"
    current_epoch[0] += 60
    client.result(
        "usage",
        {
            "rateLimits": {
                "primary": {"windowDurationMins": 300, "usedPercent": 28},
                "secondary": {"windowDurationMins": 10080, "usedPercent": 39},
            }
        },
    )

    assert hasattr(window, "_usage_snapshots")
    snapshot = window._usage_snapshots[("thread-a", "turn-a")]
    assert snapshot is not None
    assert snapshot.usage.five_hour is not None
    assert snapshot.usage.five_hour.remaining_percent == 72
    assert snapshot.usage.weekly is not None
    assert snapshot.usage.weekly.remaining_percent == 61
    assert snapshot.captured_at.tzinfo is not None
    assert window._usage is global_usage
    assert window.usage_status_label.text() == "Codex Usage  5h 72% · Week 18%"
    labels = {label.text() for label in window.history_pane.findChildren(QLabel)}
    assert any(
        label.startswith("Usage snapshot: 5h 72% · Week 61% · captured ") for label in labels
    )
    history_samples = get_usage_samples(
        current_epoch[0] - 180, current_epoch[0] + 1, database_path=database_path
    )
    assert [sample.weekly_remaining for sample in history_samples] == [18, 18]
    assert get_recent_usage_events(database_path=database_path) == []
    assert refresh_calls == [{"rolling": True}]
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_turn_snapshot_failure_preserves_current_usage_and_saves_no_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}})
    known = window._usage
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.failure("usage", "snapshot unavailable")

    assert window._usage is known
    assert "5h 72%" in window.usage_status_label.text()
    assert "refresh failed" in window.usage_status_label.text()
    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_unavailable_usage_does_not_create_turn_snapshot() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.result("usage", {"rateLimits": {}})

    assert window._usage_snapshots.get(("thread-a", "turn-a")) is None
    assert not window._pending_usage_snapshot_turns
    window.close()


def test_usage_snapshot_survives_thread_switch_and_store_is_bounded() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)
    window.select_thread("thread-a")
    client.activity(
        window._selection_generation,
        {
            "activity_id": "completed",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "turn_completed",
            "status": "completed",
        },
    )
    assert hasattr(window, "turn_usage_snapshot_timer")
    window.turn_usage_snapshot_timer.stop()
    window._on_turn_usage_snapshot_timeout()
    client.result(
        "usage",
        {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 28}}},
    )
    window.select_thread("thread-b")
    window.select_thread("thread-a")
    window._apply_items(
        {
            "items": [
                {
                    "turn_id": "turn-a",
                    "item": {"id": "item-a", "type": "agentMessage", "text": "answer"},
                }
            ]
        },
        prepend=False,
    )

    assert any(
        label.text().startswith("Usage snapshot: 5h 72% · Week — · captured ")
        for label in window.history_pane.findChildren(QLabel)
    )

    assert hasattr(window, "_schedule_turn_usage_snapshot")
    for index in range(501):
        window._schedule_turn_usage_snapshot("thread-a", f"turn-{index}")
    assert len(window._usage_snapshots) == 500
    assert ("thread-a", "turn-a") not in window._usage_snapshots
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
    window = _usage_window(client)

    client.result(
        "threads",
        {"threads": [{"id": "thread-a", "name": "A", "preview": "ignored"}]},
    )
    window.thread_pane.list_widget.setCurrentItem(_thread_item(window, "thread-a"))
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

    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    window.refresh()
    assert sum(path.endswith("/turns") for _, path, _ in client.requests) == 1
    window.close()


def test_main_window_keeps_active_style_and_updates_name_on_thread_refresh() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    client.result("threads", {"threads": [{"id": "thread-a"}]})
    window.thread_pane.list_widget.setCurrentItem(_thread_item(window, "thread-a"))
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {"thread_id": "thread-a", "state": "in_progress", "recent_activities": []},
    )

    active_item = _thread_item(window, "thread-a")
    assert active_item.text(0) == "\u25cf New \u30b9\u30ec\u30c3\u30c9"
    active_weight = active_item.font(0).weight()

    client.result("threads", {"threads": [{"id": "thread-a", "name": "Renamed"}]})

    current = window.thread_pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "thread-a"
    assert current.text(0) == "\u25cf Renamed"
    assert current.font(0).weight() == active_weight
    window.close()


def test_main_window_reads_project_names_on_thread_refresh_only(monkeypatch, tmp_path) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    cwd = str(tmp_path / "codexbridge")
    key = os.path.normcase(os.path.normpath(cwd))
    calls: list[None] = []

    def read_names() -> dict[str, str]:
        calls.append(None)
        return {key: "Friendly Project"}

    monkeypatch.setattr(main_window_module, "read_local_project_names", read_names)
    client.result("threads", {"threads": [{"id": "thread-a", "cwd": cwd}]})

    assert calls == [None]
    assert window.thread_pane.list_widget.topLevelItem(0).text(0) == (
        "Friendly Project — codexbridge"
    )
    window.thread_pane.filter_edit.setText("thread-a")
    assert calls == [None]

    client.result("threads", {"threads": [{"id": "thread-a", "cwd": cwd}]})
    assert calls == [None, None]
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

    assert _thread_item(window, "thread-b").text(0) == "Renamed"
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
    window.thread_pane.list_widget.setCurrentItem(_thread_item(window, "thread-a"))

    menu = window.thread_pane._context_menu_for_item(_thread_item(window, "thread-b"))
    action = next(action for action in menu.actions() if action.text() == "Open in Codex App")
    action.trigger()

    assert opened == ["codex://threads/thread-b"]
    assert window._selected_thread_id == "thread-a"
    current = window.thread_pane.list_widget.currentItem()
    assert current is not None
    assert current.data(0, Qt.ItemDataRole.UserRole) == "thread-a"
    assert client.json_posts == []
    window.close()


def test_copy_thread_content_fetches_all_pages_in_order_without_changing_selection(
    monkeypatch,
) -> None:
    application = _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    copied: list[str] = []
    monkeypatch.setattr("codex_bridge.console.main_window.copy_to_clipboard", copied.append)
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
    window._active_thread_ids.add("thread-a")
    initial_generation = window._selection_generation
    initial_selected = window._selected_thread_id
    initial_active = set(window._active_thread_ids)

    menu = window.thread_pane._context_menu_for_item(_thread_item(window, "thread-b"))
    action = next(action for action in menu.actions() if action.text() == "Copy thread content")
    action.trigger()

    copy_key = next(key for key, _, _ in client.requests if key.startswith("copy-thread:"))
    assert client.requests[-1] == (
        copy_key,
        "/ui-api/threads/thread-b/items",
        {"limit": 100, "sort_direction": "desc"},
    )
    client.result(
        copy_key,
        {
            "items": [
                {"turn_id": "turn-b", "item": _item("agent-b", "agentMessage", text="Agent B")},
                {
                    "turn_id": "turn-b",
                    "item": _item(
                        "commentary-b",
                        "agentMessage",
                        text="Progress B",
                        phase="commentary",
                    ),
                },
                {"turn_id": "turn-b", "item": _item("user-b", "userMessage", text="User B")},
            ],
            "next_cursor": "older",
        },
    )
    assert client.requests[-1] == (
        copy_key,
        "/ui-api/threads/thread-b/items",
        {"limit": 100, "sort_direction": "desc", "cursor": "older"},
    )
    client.result(
        copy_key,
        {
            "items": [
                {"turn_id": "turn-a", "item": _item("agent-a", "agentMessage", text="Agent A")},
                {"turn_id": "turn-a", "item": _item("user-a", "userMessage", text="User A")},
            ],
            "next_cursor": None,
        },
    )

    application.processEvents()
    assert copied == [
        "User:\nUser A\n\nAgent:\nAgent A\n\nUser:\nUser B"
        "\n\nCommentary:\nProgress B\n\nAgent:\nAgent B"
    ]
    assert window._selected_thread_id == initial_selected
    assert window._selection_generation == initial_generation
    assert window._active_thread_ids == initial_active
    window.close()


def test_open_in_codex_app_default_helper_passes_exact_uri_to_qt_shell(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result("threads", {"threads": [{"id": "thread-a", "name": "A"}]})
    window.select_thread("thread-a")

    actions = window.thread_pane._context_menu_for_item(_thread_item(window, "thread-a")).actions()
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

    menu = window.thread_pane._context_menu_for_item(_thread_item(window, "thread-a"))
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


def test_history_activity_refresh_is_debounced_and_does_not_request_status() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()

    non_target_activity = _history_activity("approval_requested", "approval", "thread-a")
    non_target_activity["status"] = "requested"
    client.activity(window._selection_generation, non_target_activity)
    assert not window._history_refresh_timer.isActive()
    assert any(path.endswith("/status") for _, path, _ in client.requests)
    client.requests.clear()

    for activity_type in ("command_started", "agent_commentary", "file_change_completed"):
        client.activity(
            window._selection_generation,
            _history_activity(activity_type, activity_type, "thread-a"),
        )

    assert window._history_refresh_timer.isActive()
    assert window._history_refresh_timer.isSingleShot()
    assert window._history_refresh_timer.interval() == 250
    assert client.requests == []

    _wait_for_qt_timer(300)

    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    assert len(history_requests) == 2
    assert all(path.endswith(("/turns", "/items")) for _, path, _ in history_requests)
    assert not any(path.endswith("/status") for _, path, _ in client.requests)
    window.close()


def test_transient_item_events_refresh_history_without_activity_pane_append() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    activity_count = window.activity_pane.activity_list.count()

    client.activity(
        window._selection_generation,
        _history_activity("item_started", "item-start", "thread-a"),
    )
    assert window.activity_pane.activity_list.count() == activity_count
    assert window._history_refresh_timer.isActive()
    assert window._history_refresh_timer.interval() == 250

    client.activity(
        window._selection_generation,
        _history_activity("item_completed", "item-complete", "thread-a"),
    )
    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(history_requests) == 2
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    assert window.activity_pane.activity_list.count() == activity_count
    window.close()


def test_history_refresh_keeps_one_pending_refresh_while_requests_are_in_flight() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    generation = window._selection_generation

    client.activity(generation, _history_activity("command_started", "first", "thread-a"))
    _wait_for_qt_timer(300)
    first_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(first_requests) == 2

    client.activity(generation, _history_activity("item_completed", "second", "thread-a"))
    _wait_for_qt_timer(300)
    assert len([request for request in client.requests if ":history:" in request[0]]) == 2

    for key, _, _ in first_requests:
        if key.endswith(":turns"):
            client.failure(key, "Bridge unavailable")
        else:
            client.result(key, {"items": []})
    assert window._history_refresh_timer.isActive()

    _wait_for_qt_timer(300)
    all_history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(all_history_requests) == 4
    second_requests = all_history_requests[2:]
    for key, _, _ in second_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": []})
        else:
            client.result(key, {"items": []})
    _wait_for_qt_timer(300)
    assert len([request for request in client.requests if ":history:" in request[0]]) == 4
    window.close()


def test_history_refresh_state_and_responses_are_isolated_by_thread_selection() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    _complete_history_snapshot(client, window._selection_generation)
    client.requests.clear()
    first_generation = window._selection_generation

    client.activity(
        first_generation,
        _history_activity("agent_message", "thread-a-event", "thread-a"),
    )
    _wait_for_qt_timer(300)
    first_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(first_requests) == 2
    client.activity(
        first_generation,
        _history_activity("command_completed", "pending-a", "thread-a"),
    )
    _wait_for_qt_timer(300)
    window.select_thread("thread-b")
    assert not window._history_refresh_timer.isActive()
    assert window._history_refresh_pending is False

    _complete_history_snapshot(client, window._selection_generation)

    client.activity(
        first_generation,
        _history_activity("item_started", "stale-item", "thread-a"),
    )
    assert not window._history_refresh_timer.isActive()

    old_items_key = next(key for key, _, _ in first_requests if key.endswith(":items"))
    client.result(
        old_items_key,
        {"items": [{"turn_id": "old", "item": _item("stale", "agentMessage", text="STALE")}]},
    )
    assert window._timeline_entries == []

    client.requests.clear()
    client.activity(
        window._selection_generation,
        _history_activity("command_started", "thread-b-event", "thread-b"),
    )
    _wait_for_qt_timer(300)
    current_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(current_requests) == 2
    assert all(
        key.startswith(f"selection:{window._selection_generation}:history:")
        for key, _, _ in current_requests
    )
    window.close()


@pytest.mark.parametrize("failed_suffix", ["items", "turns"])
def test_live_history_failure_preserves_timeline_and_scroll_position(failed_suffix: str) -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    window.resize(1_000, 650)
    window.show()
    window.select_thread("thread-a")
    generation = window._selection_generation
    client.result(
        f"selection:{generation}:turns",
        {"turns": [{"id": "turn-a", "status": "completed"}]},
    )
    client.result(f"selection:{generation}:items", _history_items(32))
    _wait_for_qt_timer(50)

    scrollbar = window.history_pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    for _ in range(4):
        application.processEvents()
    old_value = scrollbar.value()
    old_entries = list(window._timeline_entries)
    window.history_pane._follow_newest = False
    assert not window.history_pane._follow_newest

    client.activity(generation, _history_activity("agent_commentary", "live", "thread-a"))
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(live_requests) == 2
    failed_key = next(key for key, _, _ in live_requests if key.endswith(failed_suffix))
    client.failure(failed_key, "Temporary live history failure")

    assert window._timeline_entries == old_entries
    assert any(
        widget.objectName() == "historyCard"
        for widget in window.history_pane._content.findChildren(QFrame)
    )
    assert not window.history_pane._empty_label.isVisible()
    assert scrollbar.value() == old_value
    assert not window.history_pane._follow_newest
    assert "Temporary live history failure" in window.bottom_status_label.text()

    remaining_key = next(key for key, _, _ in live_requests if key != failed_key)
    if remaining_key.endswith(":items"):
        client.result(remaining_key, {"items": []})
    else:
        client.result(remaining_key, {"turns": []})
    window.close()


def test_initial_history_request_is_serialized_before_live_reconciliation() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    initial_items_key = f"selection:{generation}:items"
    initial_turns_key = f"selection:{generation}:turns"

    client.activity(generation, _history_activity("agent_message", "new-message", "thread-a"))
    _wait_for_qt_timer(300)

    assert not any(":history:" in key for key, _, _ in client.requests)
    assert window._history_refresh_pending

    client.result(initial_turns_key, {"turns": [{"id": "turn-a", "status": "in_progress"}]})
    client.result(
        initial_items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("old", "agentMessage", text="old")}]},
    )
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(live_requests) == 2

    for key, _, _ in live_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": [{"id": "turn-a", "status": "completed"}]})
        else:
            client.result(
                key,
                {
                    "items": [
                        {"turn_id": "turn-a", "item": _item("new", "agentMessage", text="new")}
                    ]
                },
            )

    assert [entry.item_id for entry in window._timeline_entries] == ["old", "new"]
    assert window._turn_statuses["turn-a"] == "completed"
    window.close()


def test_manual_snapshot_waits_for_active_history_request_and_replaces_it() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    items_key = f"selection:{generation}:items"
    turns_key = f"selection:{generation}:turns"

    window.refresh()
    assert sum(key == items_key for key, _, _ in client.requests) == 1
    assert sum(key == turns_key for key, _, _ in client.requests) == 1

    client.result(turns_key, {"turns": [{"id": "turn-a", "status": "in_progress"}]})
    client.result(
        items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("old", "agentMessage", text="old")}]},
    )
    assert sum(key == items_key for key, _, _ in client.requests) == 2
    assert sum(key == turns_key for key, _, _ in client.requests) == 2

    client.result(turns_key, {"turns": [{"id": "turn-a", "status": "completed"}]})
    client.result(
        items_key,
        {"items": [{"turn_id": "turn-a", "item": _item("new", "agentMessage", text="new")}]},
    )
    assert [entry.item_id for entry in window._timeline_entries] == ["new"]
    window.close()


def test_live_turn_statuses_merge_and_explicit_snapshot_replaces() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    client.result(
        f"selection:{generation}:turns",
        {
            "turns": [
                {"id": "turn-old", "status": "completed"},
                {"id": "turn-latest", "status": "in_progress"},
            ]
        },
    )
    client.result(
        f"selection:{generation}:items",
        {
            "items": [
                {"turn_id": "turn-latest", "item": _item("latest", "agentMessage", text="latest")}
            ],
            "next_cursor": "older-turn",
        },
    )
    window.load_older()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "turn-latest", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "turn-old", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
        },
    )

    client.activity(generation, _history_activity("turn_completed", "completed", "thread-a"))
    _wait_for_qt_timer(300)
    live_requests = [request for request in client.requests if ":history:" in request[0]]
    for key, _, _ in live_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": [{"id": "turn-latest", "status": "completed"}]})
        else:
            client.result(
                key,
                {
                    "items": [
                        {
                            "turn_id": "turn-latest",
                            "item": _item("latest", "agentMessage", text="latest updated"),
                        }
                    ]
                },
            )

    assert window._turn_statuses == {
        "turn-old": "completed",
        "turn-latest": "completed",
    }
    headers = [label.text() for label in window.history_pane.findChildren(QLabel, "turnSeparator")]
    assert any("completed" in header for header in headers)

    window._request_snapshot(generation)
    client.result(
        f"selection:{generation}:turns",
        {"turns": [{"id": "turn-latest", "status": "failed"}]},
    )
    client.result(
        f"selection:{generation}:items",
        {
            "items": [
                {
                    "turn_id": "turn-latest",
                    "item": _item("latest", "agentMessage", text="latest"),
                }
            ]
        },
    )
    assert window._turn_statuses == {"turn-latest": "failed"}
    window.close()


def test_sse_reconnect_schedules_one_history_reconciliation_after_first_connect() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    generation = window._selection_generation
    _complete_history_snapshot(client, generation)
    status_key = f"selection:{generation}:status"
    client.result(status_key, {"thread_id": "thread-a", "state": "running"})

    client.stream_state_changed.emit(generation, "connected")
    assert not window._history_refresh_timer.isActive()
    assert not any(":history:" in key for key, _, _ in client.requests)

    client.stream_state_changed.emit(generation, "disconnected")
    client.stream_state_changed.emit(generation, "reconnecting")
    client.stream_state_changed.emit(generation, "connected")
    client.stream_state_changed.emit(generation, "connected")
    client.stream_state_changed.emit(generation, "connected")
    assert window._history_refresh_timer.isActive()

    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    assert len(history_requests) == 2
    assert {key.rsplit(":", 1)[-1] for key, _, _ in history_requests} == {"turns", "items"}
    window.close()


def test_main_window_resolves_pending_approval_and_refreshes_on_sse() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "needs_approval",
            "current_diff": "",
            "pending_request": {
                "request_id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "thread_id": "thread-a",
                "turn_id": "turn-a",
                "summary": "run checks",
            },
            "recent_activities": [],
        },
    )
    window._runtime_state = "console_started"
    window._control_token = "owned-bridge-token"
    window._sync_approval_controls()

    assert window.activity_pane.allow_once_button.isEnabled()
    window.activity_pane.allow_once_button.click()

    assert client.approval_requests == [
        ("owned-bridge-token", "approval-1", "accept", "control:approval")
    ]
    assert not window.activity_pane.allow_once_button.isEnabled()
    client.control_success("control:approval")
    assert window.activity_pane._approval_feedback.text() == "Approval sent. Refreshing status…"

    before_refresh = len(client.requests)
    client.activity(
        window._selection_generation,
        {
            "activity_id": "approval-resolved",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "type": "approval_resolved",
            "status": "resolved",
            "summary": "run checks",
            "details": {},
        },
    )
    assert len(client.requests) > before_refresh
    assert not window.activity_pane.allow_once_button.isEnabled()
    window.close()


def test_external_bridge_approval_is_read_only_and_conflict_is_nonfatal() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")
    status_key = next(key for key, _, _ in client.requests if key.endswith(":status"))
    pending_snapshot = {
        "thread_id": "thread-a",
        "state": "needs_approval",
        "pending_request": {
            "request_id": 9,
            "method": "item/fileChange/requestApproval",
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "summary": "change files",
        },
        "recent_activities": [],
    }
    client.result(status_key, pending_snapshot)
    assert not window.activity_pane.allow_once_button.isEnabled()
    assert window.activity_pane.approval_control_message.text().startswith(
        "Resolve from MCP client"
    )
    window.activity_pane.allow_once_button.click()
    assert client.approval_requests == []

    window._runtime_state = "console_started"
    window._control_token = "owned-token"
    window._sync_approval_controls()
    window.activity_pane.allow_once_button.click()
    assert client.approval_requests == [("owned-token", 9, "accept", "control:approval")]
    client.control_failure("control:approval", "Approval already resolved")

    assert window.runtime_state == "console_started"
    assert "already resolved" in window.activity_pane._approval_feedback.text().lower()
    assert any(key.endswith(":status") for key, _, _ in client.requests)
    window.close()


def test_historical_items_render_commentary_and_final_agent_separately() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)

    window.select_thread("thread-a")
    items_key = next(key for key, _, _ in client.requests if key.endswith(":items"))
    client.result(
        items_key,
        {
            "items": [
                {
                    "turn_id": "turn-1",
                    "item": _item(
                        "agent-final", "agentMessage", text="final answer", phase="final_answer"
                    ),
                },
                {
                    "turn_id": "turn-1",
                    "item": _item(
                        "agent-commentary",
                        "agentMessage",
                        text="progress update",
                        phase="commentary",
                    ),
                },
                {
                    "turn_id": "turn-1",
                    "item": _item("plan", "plan", text="planned work"),
                },
                {"turn_id": "turn-1", "item": _item("user", "userMessage", text="question")},
            ]
        },
    )

    assert [entry.title for entry in window._timeline_entries] == [
        "User",
        "Plan",
        "Commentary",
        "Agent",
    ]
    visible_text = [label.text() for label in window.history_pane._content.findChildren(QLabel)]
    assert "Commentary" in visible_text
    assert "Agent" in visible_text
    window.close()


def test_live_activity_rows_render_commentary_and_final_agent_separately() -> None:
    _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    window.select_thread("thread-a")

    client.activity(
        1,
        {
            "activity_id": "commentary",
            "thread_id": "thread-a",
            "type": "agent_commentary",
            "status": "completed",
            "summary": "progress update",
        },
    )
    client.activity(
        1,
        {
            "activity_id": "final",
            "thread_id": "thread-a",
            "type": "agent_message",
            "status": "completed",
            "summary": "final answer",
        },
    )

    rows = [window.activity_pane.activity_list.item(index).text() for index in range(2)]
    assert "Commentary" in rows[0]
    assert "Agent" in rows[1]
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


def test_main_window_uses_current_thread_metadata_not_setup_access_values() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)

    window.select_thread("thread-a")
    status_a = f"selection:{window._selection_generation}:status"
    client.result(
        status_a,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert "Full Access" in sandbox.text()

    window.select_thread("thread-b")
    status_b = f"selection:{window._selection_generation}:status"
    assert sandbox.text() == "Sandbox: Unknown"
    assert policy.text() == "Approval policy: Unknown"
    assert reviewer.text() == "Approvals reviewer: Unknown"

    # A late response for the previous generation must not overwrite thread B.
    client.result(
        status_a,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    client.result(
        status_b,
        {
            "thread_id": "thread-b",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "auto_review",
            },
            "setup_selection": {"sandbox_mode": "danger-full-access"},
            "sandbox_mode": "danger-full-access",
        },
    )

    assert sandbox.text() == "Sandbox: workspace-write"
    assert policy.text() == "Approval policy: on-request"
    assert reviewer.text() == "Approvals reviewer: auto_review"
    window.close()


def test_main_window_shows_unknown_access_while_selected_thread_status_is_loading() -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    _set_usage_ready(window)

    window.select_thread("thread-a")
    window.show()
    application.processEvents()

    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: Unknown"
    assert policy.text() == "Approval policy: Unknown"
    assert reviewer.text() == "Approvals reviewer: Unknown"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()

    window.select_thread(None)
    application.processEvents()
    assert not sandbox.isVisible()
    assert not policy.isVisible()
    assert not reviewer.isVisible()
    window.close()


def test_main_window_clears_effective_access_on_status_error_and_disconnect() -> None:
    application = _application()
    client = FakeClient()
    window = _usage_window(client)
    window.show()
    application.processEvents()
    window.select_thread("thread-a")
    status_key = f"selection:{window._selection_generation}:status"
    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
            },
            "recent_activities": [
                {"activity_id": "activity-1", "type": "turn_started", "summary": "started"}
            ],
        },
    )

    client.failure(status_key, "Status request failed")

    sandbox = window.activity_pane.findChild(QLabel, "effectiveSandboxMode")
    policy = window.activity_pane.findChild(QLabel, "effectiveApprovalPolicy")
    reviewer = window.activity_pane.findChild(QLabel, "effectiveApprovalsReviewer")
    assert sandbox is not None
    assert policy is not None
    assert reviewer is not None
    assert sandbox.text() == "Sandbox: Unavailable"
    assert policy.text() == "Approval policy: Unavailable"
    assert reviewer.text() == "Approvals reviewer: Unavailable"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()
    assert window.activity_pane.activity_list.count() == 1

    client.result(
        status_key,
        {
            "thread_id": "thread-a",
            "state": "in_progress",
            "thread_metadata": {
                "sandbox_mode": "danger-full-access",
                "approval_policy": "never",
                "approvals_reviewer": "user",
            },
        },
    )
    assert "Full Access" in sandbox.text()

    client.failure("bridge-status", "Bridge unavailable")

    assert sandbox.text() == "Sandbox: Unavailable"
    assert policy.text() == "Approval policy: Unavailable"
    assert reviewer.text() == "Approvals reviewer: Unavailable"
    assert sandbox.isVisible()
    assert policy.isVisible()
    assert reviewer.isVisible()
    assert window.activity_pane.activity_list.count() == 1
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
    client.result("selection:1:turns", {"turns": []})
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


def test_live_history_refresh_preserves_loaded_older_entries_and_order() -> None:
    _application()
    client = FakeClient()
    window = _usage_window(client)
    window.select_thread("thread-a")
    client.result("selection:1:turns", {"turns": []})
    client.result(
        "selection:1:items",
        {
            "items": [
                {"turn_id": "t2", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
            ],
            "next_cursor": "older-1",
        },
    )
    window.load_older()
    older_key = next(key for key, _, _ in client.requests if ":older:" in key)
    client.result(
        older_key,
        {
            "items": [
                {"turn_id": "t2", "item": _item("latest", "agentMessage", text="latest")},
                {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
                {"turn_id": "t1", "item": _item("old", "agentMessage", text="old")},
            ],
            "next_cursor": None,
            "turn_model_metadata": {"t1": {"model_resolution_status": "resolved"}},
        },
    )

    client.requests.clear()
    client.activity(
        window._selection_generation,
        _history_activity("agent_message", "new-message", "thread-a"),
    )
    _wait_for_qt_timer(300)
    history_requests = [request for request in client.requests if ":history:" in request[0]]
    for key, _, _ in history_requests:
        if key.endswith(":turns"):
            client.result(key, {"turns": []})
        else:
            client.result(
                key,
                {
                    "items": [
                        {"turn_id": "t3", "item": _item("newest", "agentMessage", text="newest")},
                        {
                            "turn_id": "t2",
                            "item": _item("latest", "agentMessage", text="latest updated"),
                        },
                        {"turn_id": "t2", "item": _item("current", "agentMessage", text="current")},
                    ],
                    "next_cursor": "newer-page-cursor",
                    "turn_model_metadata": {"t3": {"model_resolution_status": "resolved"}},
                },
            )

    assert [(entry.turn_id, entry.item_id) for entry in window._timeline_entries] == [
        ("t1", "old"),
        ("t2", "current"),
        ("t2", "latest"),
        ("t3", "newest"),
    ]
    assert len({(entry.turn_id, entry.item_id) for entry in window._timeline_entries}) == 4
    assert set(window._turn_model_metadata) == {"t1", "t3"}
    assert window._next_cursor is None
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
    assert any(body.toPlainText() == "answer" for body in cards[0].findChildren(QTextBrowser))
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
    assert not window._history_refresh_timer.isActive()


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
