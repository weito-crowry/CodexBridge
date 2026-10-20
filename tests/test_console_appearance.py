from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import time

import pytest
from PySide6.QtCore import QDateTime, QSettings, Qt
from PySide6.QtGui import QPalette

from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.usage import CodexUsage, UsageWindow
from codex_bridge.console.usage_history import record_usage_sample
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
    _config,
)


def _settings(path: Path) -> QSettings:
    settings = QSettings(str(path), QSettings.Format.IniFormat)
    settings.setFallbacksEnabled(False)
    return settings


def _window(
    settings: QSettings,
    scheme: Qt.ColorScheme | Callable[[], object],
) -> tuple[MainWindow, FakeClient, FakeCodexProbe]:
    client = FakeClient()
    probe = FakeCodexProbe()
    scheme_getter = scheme if callable(scheme) else lambda: scheme
    window = MainWindow(
        _config(),
        api_client=client,
        codex_probe=probe,
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        appearance_settings=settings,
        appearance_system_scheme_getter=scheme_getter,
    )
    return window, client, probe


@pytest.mark.parametrize(
    ("scheme", "expected_scheme"),
    [(Qt.ColorScheme.Light, "light"), (Qt.ColorScheme.Dark, "dark")],
)
def test_system_mode_uses_os_scheme_and_invalid_preference_is_not_rewritten(
    tmp_path: Path,
    scheme: Qt.ColorScheme,
    expected_scheme: str,
) -> None:
    _application()
    settings = _settings(tmp_path / "appearance.ini")
    settings.setValue("appearance/mode", "unexpected")

    current_scheme = [scheme]
    window, _client, _probe = _window(settings, lambda: current_scheme[0])

    assert window._appearance_controller.mode == "System"
    assert window._appearance_controller.theme.scheme == expected_scheme
    assert window._appearance_actions["System"].isChecked()
    assert all(action.isCheckable() for action in window._appearance_actions.values())
    assert sum(action.isChecked() for action in window._appearance_actions.values()) == 1
    assert settings.value("appearance/mode") == "unexpected"
    current_scheme[0] = (
        Qt.ColorScheme.Dark if scheme == Qt.ColorScheme.Light else Qt.ColorScheme.Light
    )
    window._appearance_controller.refresh_system_scheme()
    assert window._appearance_controller.theme.scheme == (
        "dark" if current_scheme[0] == Qt.ColorScheme.Dark else "light"
    )
    assert window._appearance_actions["System"].isChecked()
    assert settings.value("appearance/mode") == "unexpected"
    assert window.appearance_button.menu() is not None
    appearance_action = next(
        action
        for action in window.appearance_button.menu().actions()
        if action.text() == "Appearance"
    )
    assert appearance_action.menu() is not None
    assert [action.text() for action in appearance_action.menu().actions()] == [
        "System",
        "Light",
        "Dark",
    ]
    window.close()


def test_menu_selection_persists_and_explicit_mode_ignores_os_changes(
    tmp_path: Path,
) -> None:
    _application()
    settings = _settings(tmp_path / "appearance.ini")
    scheme = [Qt.ColorScheme.Dark]
    window, _client, _probe = _window(settings, lambda: scheme[0])

    window._appearance_actions["Light"].trigger()
    assert settings.value("appearance/mode") == "Light"
    assert window._appearance_actions["Light"].isChecked()
    assert window._appearance_controller.theme.scheme == "light"

    scheme[0] = Qt.ColorScheme.Dark
    window._appearance_controller.refresh_system_scheme()
    assert window._appearance_controller.theme.scheme == "light"

    restored_scheme = [Qt.ColorScheme.Light]
    restored, _restored_client, _restored_probe = _window(settings, lambda: restored_scheme[0])
    assert restored._appearance_actions["Light"].isChecked()
    assert restored._appearance_controller.theme.scheme == "light"
    restored._appearance_actions["System"].trigger()
    assert restored._appearance_actions["System"].isChecked()
    assert settings.value("appearance/mode") == "System"
    restored_scheme[0] = Qt.ColorScheme.Dark
    restored._appearance_controller.refresh_system_scheme()
    assert restored._appearance_controller.theme.scheme == "dark"
    assert restored._appearance_actions["System"].isChecked()
    window.close()
    restored.close()


def test_live_switch_reapplies_quota_severity_without_backend_or_probe_activity(
    tmp_path: Path,
) -> None:
    _application()
    settings = _settings(tmp_path / "appearance.ini")
    settings.setValue("appearance/mode", "Light")
    window, client, probe = _window(settings, Qt.ColorScheme.Light)
    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 82}}})
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == (
        window._appearance_controller.theme.tokens.warning
    )
    before_requests = list(client.requests)
    before_probe_starts = probe.starts
    old_dialog_color = window.status_dialog.palette().color(QPalette.ColorRole.Window)

    window._appearance_actions["Dark"].trigger()

    assert window._appearance_controller.theme.scheme == "dark"
    assert window.palette().color(QPalette.ColorRole.Window) != old_dialog_color
    assert window.status_dialog.palette().color(QPalette.ColorRole.Window) == (
        window.palette().color(QPalette.ColorRole.Window)
    )
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == (
        window._appearance_controller.theme.tokens.warning
    )
    assert client.requests == before_requests
    assert probe.starts == before_probe_starts

    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 93}}})
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == (
        window._appearance_controller.theme.tokens.error
    )
    assert client.requests == before_requests
    assert probe.starts == before_probe_starts
    window.close()


def test_open_usage_history_rethemes_without_rebuilding_or_changing_chart_state(
    tmp_path: Path,
) -> None:
    application = _application()
    settings = _settings(tmp_path / "appearance.ini")
    settings.setValue("appearance/mode", "Light")
    settings.setValue("console/usageHistory/geometry", b"saved-geometry")
    database_path = tmp_path / "usage-history.sqlite3"
    now = time()
    usage = CodexUsage(
        five_hour=UsageWindow(300, 72, "reset"),
        weekly=UsageWindow(10080, 61, "reset"),
    )
    record_usage_sample(usage, captured_at_epoch=now - 3600, database_path=database_path)
    record_usage_sample(usage, captured_at_epoch=now - 1800, database_path=database_path)
    window, _client, _probe = _window(settings, Qt.ColorScheme.Light)
    window._usage_history_path = database_path

    window._create_or_show_usage_history_window()
    history = window._usage_history_window
    assert history is not None
    history.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(now - 7200)))
    history.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(now)))
    assert history.refresh()
    history.time_axis.setRange(
        QDateTime.fromSecsSinceEpoch(int(now - 5000)),
        QDateTime.fromSecsSinceEpoch(int(now - 1000)),
    )
    history.percent_axis.setRange(20, 80)
    weekly_marker = history.chart.legend().markers(history.weekly_series)[0]
    history.toggle_series_visibility(history.weekly_series, weekly_marker)

    chart = history.chart
    five_hour_series = history.five_hour_series
    weekly_series = history.weekly_series
    reset_series = history.reset_series
    samples = list(history._display_samples)
    custom_range = (
        history.start_edit.dateTime().toSecsSinceEpoch(),
        history.end_edit.dateTime().toSecsSinceEpoch(),
    )
    axis_range = (
        history.time_axis.min().toSecsSinceEpoch(),
        history.time_axis.max().toSecsSinceEpoch(),
        history.percent_axis.min(),
        history.percent_axis.max(),
    )
    summary_values = {key: label.text() for key, label in history.summary_labels.items()}
    saved_geometry = history.saveGeometry()
    assert not weekly_series.isVisible()
    assert settings.value("console/usageHistory/geometry") == b"saved-geometry"

    window._appearance_actions["Dark"].trigger()

    assert history.chart is chart
    assert history.five_hour_series is five_hour_series
    assert history.weekly_series is weekly_series
    assert history.reset_series is reset_series
    assert history._display_samples == samples
    assert history.selected_preset == "Custom"
    assert (
        history.start_edit.dateTime().toSecsSinceEpoch(),
        history.end_edit.dateTime().toSecsSinceEpoch(),
    ) == custom_range
    assert (
        history.time_axis.min().toSecsSinceEpoch(),
        history.time_axis.max().toSecsSinceEpoch(),
        history.percent_axis.min(),
        history.percent_axis.max(),
    ) == axis_range
    assert {key: label.text() for key, label in history.summary_labels.items()} == summary_values
    assert not history.weekly_series.isVisible()
    assert history.chart.backgroundBrush().color() == history._appearance.tokens.chart_background
    assert history.time_axis.labelsColor() == history._appearance.tokens.text
    assert history.five_hour_series.pen().color() == history._appearance.tokens.series_primary
    assert history.reset_series.brush().color() == history._appearance.tokens.reset_marker
    assert weekly_marker.pen().color().alphaF() < 1.0
    assert history.saveGeometry() == saved_geometry
    assert settings.value("console/usageHistory/geometry") == b"saved-geometry"

    weekly_marker.clicked.emit()
    assert history.weekly_series.isVisible()
    assert weekly_marker.pen().color().alphaF() == pytest.approx(1.0)
    assert weekly_marker.pen().color() == history._appearance.tokens.series_secondary

    restored, _restored_client, _restored_probe = _window(settings, Qt.ColorScheme.Light)
    restored._usage_history_path = database_path
    restored._create_or_show_usage_history_window()
    restored_history = restored._usage_history_window
    assert restored_history is not None
    assert restored._appearance_controller.theme.scheme == "dark"
    assert restored_history.chart.backgroundBrush().color() == (
        restored._appearance_controller.theme.tokens.chart_background
    )

    history.hide()
    window.close()
    restored.close()
    application.processEvents()
