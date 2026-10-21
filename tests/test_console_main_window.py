from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
)

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_updates import CodexUpdateInfo
from codex_bridge.console.config import ConsoleConfig, ConsoleConfigurationError
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    FakeDiagnosticsReader,
    StableTunnel,
    _application,
    _complete_history_snapshot,
    _config,
    _item,
    _thread_item,
    _usage_window,
)
from tests.console_main_window_test_support import (
    _set_usage_ready as _set_usage_ready,
)


def _settings_window(config: ConsoleConfig, client: FakeClient | None = None) -> MainWindow:
    return MainWindow(
        config,
        api_client=client or FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )


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


def test_settings_menu_contains_config_actions_and_existing_appearance_modes() -> None:
    _application()
    window = _settings_window(_config())

    actions = window.settings_button.menu().actions()
    labels = [action.text() for action in actions]
    appearance_action = next(action for action in actions if action.text() == "Appearance")

    assert labels[:3] == ["Open settings file", "Reload settings", "Open settings folder"]
    assert appearance_action.menu() is not None
    assert [action.text() for action in appearance_action.menu().actions()] == [
        "System",
        "Light",
        "Dark",
    ]
    assert window.settings_button.toolTip() == "Settings"
    window.close()


def test_settings_open_file_and_folder_use_active_config_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    config_path = tmp_path / "settings" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text("[bridge]\n", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(
        main_window_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    window = _settings_window(ConsoleConfig(config_path=str(config_path), config_mode="managed"))

    window._open_settings_file()
    window._open_settings_folder()

    assert [Path(path) for path in opened] == [config_path, config_path.parent]
    window.close()


def test_settings_reload_stages_valid_changes_without_disturbing_live_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    active = ConsoleConfig(config_path=str(tmp_path / "config.toml"), config_mode="managed")
    pending = ConsoleConfig(
        port=8123,
        config_path=str(tmp_path / "config.toml"),
        config_mode="managed",
    )
    client = FakeClient()
    window = _settings_window(active, client)
    selected_thread = "thread-1"
    window._selected_thread_id = selected_thread
    before_requests = list(client.requests)
    monkeypatch.setattr(ConsoleConfig, "from_sources", classmethod(lambda cls, **_kwargs: pending))
    messages: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "information",
        lambda _parent, _title, text: messages.append(text),
    )

    window._reload_configuration()

    assert window._config is active
    assert window._pending_config is pending
    assert window._selected_thread_id == selected_thread
    assert client.requests == before_requests
    assert not client.aborted_all
    assert "restart" in window.config_status_label.text().casefold()
    assert messages and "Bridge" in messages[0] and "Tunnel" in messages[0]
    window.close()


def test_invalid_settings_reload_keeps_last_good_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _application()
    active = ConsoleConfig(config_path=str(tmp_path / "config.toml"), config_mode="managed")
    window = _settings_window(active)
    previous_pending = ConsoleConfig(
        port=8123,
        config_path=active.config_path,
        config_mode="managed",
    )
    window._pending_config = previous_pending
    window._apply_pending_settings_action.setEnabled(True)
    monkeypatch.setattr(
        ConsoleConfig,
        "from_sources",
        classmethod(
            lambda cls, **_kwargs: (_ for _ in ()).throw(
                ConsoleConfigurationError("configuration contains invalid TOML")
            )
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda _parent, _title, text: messages.append(text),
    )

    window._reload_configuration()

    assert window._config is active
    assert window._pending_config is previous_pending
    assert messages == ["configuration contains invalid TOML"]
    window.close()


def test_apply_pending_settings_requires_confirmation_before_console_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application()
    window = _settings_window(_config())
    window._pending_config = ConsoleConfig(port=8123, allowed_roots=_config().allowed_roots)
    exits: list[bool] = []
    monkeypatch.setattr(window, "_begin_exit", lambda: exits.append(True))
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No,
    )

    window._apply_pending_configuration()

    assert exits == []
    assert not window._restart_after_settings_apply
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window._apply_pending_configuration()

    assert exits == [True]
    assert window._restart_after_settings_apply
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


def test_bulk_thread_naming_routes_batch_results_and_refreshes_once() -> None:
    application = _application()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result(
        "threads",
        {
            "threads": [
                {"id": "untitled", "preview": "# A useful task"},
                {"id": "named", "name": "New Thread", "preview": "# Keep this title"},
            ]
        },
    )
    client.requests.clear()

    window.thread_pane.name_untitled_button.click()

    dialog = window._bulk_thread_naming.dialog
    assert dialog is not None
    assert dialog.table.rowCount() == 1
    assert not client.json_posts
    dialog.confirm_button.click()
    application.processEvents()
    assert client.json_posts == [
        (
            "bulk-rename:1",
            "/ui-api/threads/untitled/name",
            {"name": "A useful task", "only_if_untitled": True},
        )
    ]

    client.json_failed.emit("bulk-rename:1", "private detail")
    application.processEvents()

    assert window.thread_pane.thread_name("named") == "New Thread"
    assert dialog.table.item(0, 4).text() == "Failed"
    assert "private detail" not in dialog.progress_label.text()
    assert [key for key, _, _ in client.requests] == ["threads"]
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


def test_project_parent_actions_open_folder_terminal_and_only_create_an_unsent_draft(
    tmp_path, monkeypatch
) -> None:
    _application()
    cwd = tmp_path / "日本語 project with spaces"
    cwd.mkdir()
    client = FakeClient()
    window = MainWindow(_config(), api_client=client, tray_available=False)
    client.result(
        "threads",
        {"threads": [{"id": "thread-a", "name": "A", "cwd": str(cwd)}]},
    )
    window.select_thread("thread-a")
    window.composer_widget.set_prompt_text("unsent existing draft")
    selected_draft_key = window._composer_controller._current_draft_key
    initial_requests = list(client.requests)
    parent = window.thread_pane.list_widget.topLevelItem(0)
    assert parent is not None
    menu = window.thread_pane._context_menu_for_item(parent)
    assert menu is not None

    menu.actions()[0].trigger()

    assert window._composer_controller._is_new_draft
    assert window._composer_controller._current_thread_id is None
    assert window._composer_controller._current_cwd == str(cwd)
    assert window.composer_widget.prompt_text == ""
    assert window._composer_controller._drafts[selected_draft_key] == "unsent existing draft"
    assert client.requests == initial_requests
    assert client.json_posts == []

    opened: list[str] = []
    monkeypatch.setattr(
        main_window_module.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    menu.actions()[2].trigger()
    assert len(opened) == 1
    assert os.path.normcase(os.path.normpath(opened[0])) == os.path.normcase(
        os.path.normpath(str(cwd))
    )

    detached: list[tuple[str, list[str], str]] = []

    def _start_detached(program: str, arguments: list[str], working_directory: str):
        detached.append((program, arguments, working_directory))
        return True, 1234

    monkeypatch.setattr(main_window_module.QProcess, "startDetached", _start_detached)
    menu.actions()[4].trigger()
    assert detached[0][2] == str(cwd)
    if main_window_module.sys.platform == "win32":
        assert detached[0] == ("wt.exe", ["-d", str(cwd)], str(cwd))
    else:
        assert detached[0][2] == str(cwd)
    assert client.requests == initial_requests
    window.close()


def test_project_folder_launch_failures_are_reported(tmp_path, monkeypatch) -> None:
    _application()
    cwd = tmp_path / "project"
    cwd.mkdir()
    window = MainWindow(_config(), api_client=FakeClient(), tray_available=False)
    window.thread_pane.set_threads([{"id": "thread-a", "cwd": str(cwd)}])
    parent = window.thread_pane.list_widget.topLevelItem(0)
    assert parent is not None
    menu = window.thread_pane._context_menu_for_item(parent)
    assert menu is not None

    monkeypatch.setattr(main_window_module.QDesktopServices, "openUrl", lambda _url: False)
    menu.actions()[2].trigger()
    assert "Could not open project folder" in window.bottom_status_label.text()

    monkeypatch.setattr(
        main_window_module.QProcess,
        "startDetached",
        lambda *_args: (False, 0),
    )
    menu.actions()[4].trigger()
    assert "Could not open a terminal" in window.bottom_status_label.text()
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
