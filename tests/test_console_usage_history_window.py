from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QDateTime, QPointF, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QToolButton

from codex_bridge.console.usage import CodexUsage, UsageWindow
from codex_bridge.console.usage_history import (
    UsageHistoryEvent,
    UsageHistorySample,
    get_usage_samples_for_display,
    record_usage_sample,
)
from codex_bridge.console.usage_history_window import (
    UsageHistoryWindow,
    export_usage_history_csv,
    format_reset_tooltip,
    format_sample_tooltip,
    write_usage_history_csv,
)


def _application() -> QApplication:
    application = QApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _usage(five_hour: int | None, weekly: int | None) -> CodexUsage:
    return CodexUsage(
        five_hour=UsageWindow(300, five_hour, "reset") if five_hour is not None else None,
        weekly=UsageWindow(10080, weekly, "reset") if weekly is not None else None,
    )


def _epoch(year: int, month: int, day: int, hour: int = 12, minute: int = 30) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def test_usage_history_window_starts_at_one_month_with_auto_axis(tmp_path: Path) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", now=lambda: float(now))

    assert window.selected_preset == "1 month"
    assert window.end_edit.dateTime().toSecsSinceEpoch() == now
    assert window.start_edit.dateTime().toSecsSinceEpoch() == now - 30 * 24 * 60 * 60
    assert window.axis_mode_combo.currentText() == "Auto"
    assert window.start_edit.calendarPopup()
    assert window.end_edit.calendarPopup()
    assert window.start_edit.dateTime().timeSpec() == Qt.TimeSpec.LocalTime
    window.close()


def test_usage_history_auto_refresh_timer_uses_one_minute_interval(tmp_path: Path) -> None:
    _application()
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3")

    assert window._auto_refresh_timer.parent() is window
    assert window._auto_refresh_timer.interval() == 60_000
    assert not window._auto_refresh_timer.isActive()
    window.close()


def test_show_refreshes_rolling_range_and_starts_auto_refresh_timer(
    tmp_path: Path, monkeypatch
) -> None:
    application = _application()
    current_time = [_epoch(2026, 9, 30)]
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: float(current_time[0]),
    )
    window.preset_buttons["24 hours"].click()
    current_time[0] += 60 * 60
    calls: list[bool] = []

    def record_refresh(*, rolling: bool = False) -> bool:
        calls.append(rolling)
        return True

    monkeypatch.setattr(window, "refresh", record_refresh)

    window.show()
    application.processEvents()

    assert calls == [True]
    assert window._auto_refresh_timer.isActive()
    window.close()


def test_auto_refresh_timeout_requests_rolling_refresh(tmp_path: Path, monkeypatch) -> None:
    _application()
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3")
    calls: list[bool] = []

    def record_refresh(*, rolling: bool = False) -> bool:
        calls.append(rolling)
        return True

    monkeypatch.setattr(window, "refresh", record_refresh)

    window._auto_refresh_timer.timeout.emit()

    assert calls == [True]
    window.close()


def test_hide_stops_auto_refresh_timer(tmp_path: Path) -> None:
    application = _application()
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3")
    window.show()
    application.processEvents()
    assert window._auto_refresh_timer.isActive()

    window.hide()
    application.processEvents()

    assert not window._auto_refresh_timer.isActive()
    window.close()


def test_reshow_refreshes_current_range_and_restarts_auto_refresh_timer(tmp_path: Path) -> None:
    application = _application()
    current_time = [_epoch(2026, 9, 30)]
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: float(current_time[0]),
    )
    window.preset_buttons["24 hours"].click()
    current_time[0] += 60 * 60

    window.show()
    application.processEvents()
    assert window.end_edit.dateTime().toSecsSinceEpoch() == current_time[0]
    assert window._auto_refresh_timer.isActive()

    window.hide()
    application.processEvents()
    assert not window._auto_refresh_timer.isActive()
    current_time[0] += 60

    window.show()
    application.processEvents()

    assert window.end_edit.dateTime().toSecsSinceEpoch() == current_time[0]
    assert window.start_edit.dateTime().toSecsSinceEpoch() == current_time[0] - 24 * 60 * 60
    assert window._auto_refresh_timer.isActive()
    window.close()


def test_close_stops_auto_refresh_timer_and_saves_geometry(tmp_path: Path) -> None:
    application = _application()
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", settings=settings)
    window.show()
    application.processEvents()
    assert window._auto_refresh_timer.isActive()
    expected_geometry = window.saveGeometry()

    window.close()
    application.processEvents()

    assert not window._auto_refresh_timer.isActive()
    assert settings.value("console/usageHistory/geometry") == expected_geometry


def test_usage_history_summary_cards_use_console_dark_theme_styles(tmp_path: Path) -> None:
    _application()
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3")

    cards = window.findChildren(QFrame, "usageHistoryMetricCard")
    captions = window.findChildren(QLabel, "usageHistoryMetricCaption")
    values = window.findChildren(QLabel, "usageHistoryMetricValue")

    assert len(cards) == 3
    assert len(captions) == 3
    assert len(values) == 3

    for card in cards:
        style = card.styleSheet()
        assert "palette(alternate-base)" not in style
        assert "palette(text)" not in style
        assert "palette(mid)" not in style
        assert re.search(
            r"QFrame#usageHistoryMetricCard\s*\{[^}]*background\s*:\s*#292a2d",
            style,
            re.IGNORECASE,
        )
        assert re.search(
            r"QFrame#usageHistoryMetricCard\s+QLabel\s*\{[^}]*background\s*:\s*transparent",
            style,
            re.IGNORECASE,
        )
        assert re.search(
            r"QLabel#usageHistoryMetricCaption\s*\{[^}]*color\s*:\s*#aeb4bd",
            style,
            re.IGNORECASE,
        )
        assert re.search(
            r"QLabel#usageHistoryMetricValue\s*\{[^}]*color\s*:\s*#f1f3f4",
            style,
            re.IGNORECASE,
        )

    window.close()


def test_usage_history_presets_replace_range_and_manual_edit_selects_custom(
    tmp_path: Path,
) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", now=lambda: float(now))

    for label, days in (("7 days", 7), ("1 month", 30), ("1 year", 365)):
        button = window.findChild(QToolButton, window.preset_object_names[label])
        assert button is not None
        button.click()
        assert window.selected_preset == label
        assert window.end_edit.dateTime().toSecsSinceEpoch() == now
        assert window.start_edit.dateTime().toSecsSinceEpoch() == now - days * 24 * 60 * 60
        expected_axis = "MM/dd HH:mm" if days == 7 else "MM/dd" if days == 30 else "yyyy/MM"
        assert window.time_axis.format() == expected_axis

    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(now - 10 * 24 * 60 * 60 + 60))

    assert window.selected_preset == "Custom"
    assert window.findChild(QToolButton, window.preset_object_names["Custom"]).isChecked()
    window.close()


def test_24_hour_preset_uses_rolling_window_and_time_axis(tmp_path: Path) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", now=lambda: float(now))

    assert "24 hours" in window.preset_buttons
    window.preset_buttons["24 hours"].click()

    assert window.selected_preset == "24 hours"
    assert window.end_edit.dateTime().toSecsSinceEpoch() == now
    assert window.start_edit.dateTime().toSecsSinceEpoch() == now - 24 * 60 * 60
    assert window.time_axis.format() == "HH:mm"
    window.close()


def test_invalid_range_does_not_query_or_crash(tmp_path: Path, monkeypatch) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", now=lambda: float(now))
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(now))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(now - 60))
    queried = False

    def fail_if_queried(*args, **kwargs):
        nonlocal queried
        queried = True
        raise AssertionError("invalid ranges must not query storage")

    monkeypatch.setattr(
        "codex_bridge.console.usage_history_window.get_usage_samples_for_display",
        fail_if_queried,
    )

    assert not window.refresh()
    assert not queried
    assert window.range_error_label.text()
    window.close()


def test_empty_and_database_error_states_are_visible(tmp_path: Path) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    empty = UsageHistoryWindow(tmp_path / "empty.sqlite3", now=lambda: float(now))
    assert empty.refresh()
    assert empty.chart_stack.currentWidget() is empty.chart_content
    assert not empty.empty_state_label.isHidden()
    assert "No usage data" in empty.empty_state_label.text()
    empty.close()

    database_directory = tmp_path / "directory.sqlite3"
    database_directory.mkdir()
    failed = UsageHistoryWindow(database_directory, now=lambda: float(now))
    assert not failed.refresh()
    assert failed.chart_stack.currentWidget() is failed.error_state_label
    assert failed.error_state_label.text() == "Usage history unavailable."
    failed.close()


def test_reset_events_without_confirmation_samples_are_not_displayed(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "events-only.sqlite3"
    reset_at = _epoch(2026, 9, 27, 6, 14)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at_epoch REAL NOT NULL,
                event_type TEXT NOT NULL,
                previous_weekly_remaining INTEGER NOT NULL,
                current_weekly_remaining INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO usage_events VALUES (NULL, ?, ?, ?, ?)",
            (reset_at, "weekly_remaining_increase", 18, 100),
        )
    window = UsageHistoryWindow(
        database_path,
        now=lambda: float(_epoch(2026, 9, 30)),
    )

    assert window.chart_stack.currentWidget() is window.chart_content
    assert not window.empty_state_label.isHidden()
    assert window.summary_labels["Reset candidates"].text() == "0"
    assert window.reset_series.count() == 0
    assert window.reset_history_list.count() == 1
    assert window.reset_history_list.item(0).text() == "No reset candidates in this period."
    window.close()


def test_legacy_transient_reset_event_is_hidden_without_deleting_database_row(
    tmp_path: Path,
) -> None:
    _application()
    database_path = tmp_path / "legacy-event.sqlite3"
    reset_at = _epoch(2026, 9, 27, 6, 14)
    record_usage_sample(
        _usage(70, 18), captured_at_epoch=reset_at - 120, database_path=database_path
    )
    record_usage_sample(
        _usage(70, 61), captured_at_epoch=reset_at - 60, database_path=database_path
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO usage_events (
                occurred_at_epoch, event_type, previous_weekly_remaining,
                current_weekly_remaining
            ) VALUES (?, ?, ?, ?)""",
            (reset_at - 60, "weekly_remaining_increase", 18, 61),
        )
    record_usage_sample(_usage(70, 19), captured_at_epoch=reset_at, database_path=database_path)
    record_usage_sample(
        _usage(70, 18), captured_at_epoch=reset_at + 60, database_path=database_path
    )
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = UsageHistoryWindow(
        database_path,
        now=lambda: float(reset_at + 120),
        settings=settings,
    )
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at - 300))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at + 300))

    assert window.refresh()
    assert window.summary_labels["Reset candidates"].text() == "0"
    assert window.reset_series.count() == 0
    assert window.reset_history_list.count() == 1
    assert window.reset_history_list.item(0).text() == "No reset candidates in this period."
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    window.close()


def test_legacy_rising_candidate_is_hidden_without_deleting_database_row(
    tmp_path: Path,
) -> None:
    _application()
    database_path = tmp_path / "legacy-rising-event.sqlite3"
    reset_at = _epoch(2026, 9, 27, 6, 14)
    record_usage_sample(
        _usage(70, 30), captured_at_epoch=reset_at - 120, database_path=database_path
    )
    record_usage_sample(
        _usage(70, 40), captured_at_epoch=reset_at - 60, database_path=database_path
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """INSERT INTO usage_events (
                occurred_at_epoch, event_type, previous_weekly_remaining,
                current_weekly_remaining
            ) VALUES (?, ?, ?, ?)""",
            (reset_at - 60, "weekly_remaining_increase", 30, 40),
        )
    record_usage_sample(_usage(70, 50), captured_at_epoch=reset_at, database_path=database_path)
    record_usage_sample(
        _usage(70, 60), captured_at_epoch=reset_at + 60, database_path=database_path
    )
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    window = UsageHistoryWindow(
        database_path,
        now=lambda: float(reset_at + 300),
        settings=settings,
    )
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at - 300))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at + 300))

    assert window.refresh()
    assert window.summary_labels["Reset candidates"].text() == "0"
    assert window.reset_series.count() == 0
    assert window.reset_history_list.item(0).text() == "No reset candidates in this period."
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    window.close()


def test_reset_view_ignores_unrecognized_event_types(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "usage.sqlite3"
    reset_at = _epoch(2026, 9, 27, 6, 14)
    record_usage_sample(
        _usage(70, 18), captured_at_epoch=reset_at - 120, database_path=database_path
    )
    record_usage_sample(
        _usage(70, 100), captured_at_epoch=reset_at - 60, database_path=database_path
    )
    record_usage_sample(_usage(70, 99), captured_at_epoch=reset_at, database_path=database_path)
    record_usage_sample(
        _usage(70, 98), captured_at_epoch=reset_at + 60, database_path=database_path
    )
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """INSERT INTO usage_events (
                occurred_at_epoch, event_type, previous_weekly_remaining,
                current_weekly_remaining
            ) VALUES (?, ?, ?, ?)""",
            (
                (reset_at + 30, "unrecognized_event", 40, 90),
                (reset_at - 59, "weekly_remaining_increase", 18, 100),
                (reset_at - 58, "weekly_remaining_increase", 40, 90),
            ),
        )
    window = UsageHistoryWindow(database_path, now=lambda: float(reset_at + 60))
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at - 300))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at + 300))

    assert window.refresh()

    assert window.summary_labels["Reset candidates"].text() == "1"
    assert window.reset_series.count() == 1
    assert window.reset_history_list.count() == 1
    assert "18% → 100%" in window.reset_history_list.item(0).text()
    window.close()


def test_summary_and_reset_marker_use_selected_period_events(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "usage.sqlite3"
    reset_at = _epoch(2026, 9, 27, 6, 14)
    record_usage_sample(
        _usage(71, 18), captured_at_epoch=reset_at - 120, database_path=database_path
    )
    record_usage_sample(
        _usage(None, 100), captured_at_epoch=reset_at - 60, database_path=database_path
    )
    record_usage_sample(_usage(None, 99), captured_at_epoch=reset_at, database_path=database_path)
    record_usage_sample(
        _usage(None, 98), captured_at_epoch=reset_at + 60, database_path=database_path
    )
    window = UsageHistoryWindow(
        database_path,
        now=lambda: float(reset_at + 1),
    )
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at - 3_600))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(reset_at + 3_600))

    assert window.refresh()

    assert window.summary_labels["5h remaining"].text() == "71%"
    assert window.summary_labels["Weekly remaining"].text() == "98%"
    assert window.summary_labels["Reset candidates"].text() == "1"
    assert window.reset_series.count() == 1
    point = window.reset_series.at(0)
    assert point.x() == (reset_at - 60) * 1_000
    assert point.y() == 100
    assert window.reset_history_list.count() == 1
    assert "18% → 100%" in window.reset_history_list.item(0).text()
    window.close()


def test_axis_modes_update_datetime_format(tmp_path: Path) -> None:
    _application()
    window = UsageHistoryWindow(tmp_path / "usage.sqlite3", now=lambda: 1_790_000_000.0)

    window.axis_mode_combo.setCurrentText("Date")
    assert window.time_axis.format() == "MM/dd"
    window.axis_mode_combo.setCurrentText("Time")
    assert window.time_axis.format() == "HH:mm"
    window.axis_mode_combo.setCurrentText("Auto")
    assert window.time_axis.format() == "MM/dd"
    window.close()


@pytest.mark.parametrize(
    "series_name",
    ("five_hour_series", "weekly_series"),
)
def test_legend_marker_click_can_toggle_each_series_off_and_on(
    tmp_path: Path, series_name: str
) -> None:
    _application()
    settings = QSettings(str(tmp_path / "legend-settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("test/isolated", True)
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: 1_790_000_000.0,
        settings=settings,
    )
    window.resize(900, 700)
    window.show()
    _application().processEvents()

    series = getattr(window, series_name)
    marker = window.chart.legend().markers(series)[0]
    original_pen = marker.pen()
    original_brush = marker.brush()
    original_label_brush = marker.labelBrush()
    legend_rect = window.chart.legend().sceneBoundingRect()
    x_fraction = 0.35 if series_name == "five_hour_series" else 0.47
    click_position = window.chart_view.mapFromScene(
        QPointF(legend_rect.left() + legend_rect.width() * x_fraction, legend_rect.center().y())
    )
    QTest.mouseClick(window.chart_view.viewport(), Qt.MouseButton.LeftButton, pos=click_position)
    _application().processEvents()
    assert not series.isVisible()
    assert marker.isVisible()
    assert marker.pen().color().alpha() < original_pen.color().alpha()
    assert marker.labelBrush().color().alpha() < original_label_brush.color().alpha()

    QTest.mouseClick(window.chart_view.viewport(), Qt.MouseButton.LeftButton, pos=click_position)
    _application().processEvents()
    assert series.isVisible()
    assert marker.isVisible()
    assert marker.pen() == original_pen
    assert marker.brush() == original_brush
    assert marker.labelBrush() == original_label_brush
    window.close()


def test_reset_candidate_legend_marker_does_not_toggle_its_series(tmp_path: Path) -> None:
    _application()
    settings = QSettings(str(tmp_path / "reset-legend-settings.ini"), QSettings.Format.IniFormat)
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: 1_790_000_000.0,
        settings=settings,
    )
    window.resize(900, 700)
    window.show()
    _application().processEvents()

    marker = window.chart.legend().markers(window.reset_series)[0]
    legend_rect = window.chart.legend().sceneBoundingRect()
    click_position = window.chart_view.mapFromScene(
        QPointF(legend_rect.left() + legend_rect.width() * 0.62, legend_rect.center().y())
    )
    QTest.mouseClick(window.chart_view.viewport(), Qt.MouseButton.LeftButton, pos=click_position)
    _application().processEvents()

    assert window.reset_series.isVisible()
    assert marker.isVisible()
    window.close()


def test_manual_refresh_keeps_custom_range(tmp_path: Path) -> None:
    _application()
    now = _epoch(2026, 9, 30)
    database_path = tmp_path / "usage.sqlite3"
    record_usage_sample(
        _usage(70, 40), captured_at_epoch=float(now - 600), database_path=database_path
    )
    window = UsageHistoryWindow(database_path, now=lambda: float(now))
    start = now - 10 * 24 * 60 * 60
    end = now - 300
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(start))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(end))

    window.refresh_button.click()

    assert window.selected_preset == "Custom"
    assert window.start_edit.dateTime().toSecsSinceEpoch() == start
    assert window.end_edit.dateTime().toSecsSinceEpoch() == end
    assert window.five_hour_series.count() == 1
    assert window.updated_label.text().startswith("Updated ")
    window.close()


def test_refresh_button_rolls_24_hour_preset_to_current_time(tmp_path: Path) -> None:
    _application()
    current_time = [_epoch(2026, 9, 30)]
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: float(current_time[0]),
    )
    window.preset_buttons["24 hours"].click()
    current_time[0] += 60 * 60

    window.refresh_button.click()

    assert window.end_edit.dateTime().toSecsSinceEpoch() == current_time[0]
    assert window.start_edit.dateTime().toSecsSinceEpoch() == current_time[0] - 24 * 60 * 60
    window.close()


def test_rolling_refresh_updates_presets_but_preserves_custom_dates(tmp_path: Path) -> None:
    _application()
    current_time = [_epoch(2026, 9, 30)]
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: float(current_time[0]),
    )
    window.preset_buttons["7 days"].click()
    current_time[0] += 24 * 60 * 60

    assert window.refresh(rolling=True)
    assert window.end_edit.dateTime().toSecsSinceEpoch() == current_time[0]
    assert window.start_edit.dateTime().toSecsSinceEpoch() == current_time[0] - 7 * 24 * 60 * 60

    custom_start = current_time[0] - 3 * 24 * 60 * 60
    custom_end = current_time[0] - 60
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(custom_start))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(custom_end))
    current_time[0] += 24 * 60 * 60

    assert window.refresh(rolling=True)
    assert window.selected_preset == "Custom"
    assert window.start_edit.dateTime().toSecsSinceEpoch() == custom_start
    assert window.end_edit.dateTime().toSecsSinceEpoch() == custom_end
    window.close()


def test_timer_and_refresh_button_preserve_custom_range(tmp_path: Path) -> None:
    _application()
    current_time = [_epoch(2026, 9, 30)]
    window = UsageHistoryWindow(
        tmp_path / "usage.sqlite3",
        now=lambda: float(current_time[0]),
    )
    custom_start = current_time[0] - 10 * 24 * 60 * 60
    custom_end = current_time[0] - 300
    window.start_edit.setDateTime(QDateTime.fromSecsSinceEpoch(custom_start))
    window.end_edit.setDateTime(QDateTime.fromSecsSinceEpoch(custom_end))
    current_time[0] += 60 * 60

    window._auto_refresh_timer.timeout.emit()
    window.refresh_button.click()

    assert window.selected_preset == "Custom"
    assert window.start_edit.dateTime().toSecsSinceEpoch() == custom_start
    assert window.end_edit.dateTime().toSecsSinceEpoch() == custom_end
    window.close()


def test_sample_and_reset_tooltips_explain_observed_values() -> None:
    sample = UsageHistorySample(
        1,
        float(_epoch(2026, 9, 29, 18, 42)),
        71,
        None,
    )
    event = UsageHistoryEvent(
        float(_epoch(2026, 9, 27, 6, 14)),
        "weekly_remaining_increase",
        18,
        100,
    )

    sample_text = format_sample_tooltip(sample)
    complete_sample_text = format_sample_tooltip(
        UsageHistorySample(sample.minute_epoch, sample.captured_at_epoch, 71, 32)
    )
    reset_text = format_reset_tooltip(event)

    assert "5h remaining: 71%" in sample_text
    assert "Weekly remaining" not in sample_text
    assert "Weekly remaining: 32%" in complete_sample_text
    assert "reset candidate" in reset_text.lower()
    assert "quota increase" in reset_text.lower()
    assert "18% → 100%" in reset_text
    assert "+82 pt" in reset_text


def test_csv_export_writes_raw_utf8_rows_and_blank_nulls(tmp_path: Path) -> None:
    path = tmp_path / "usage.csv"
    samples = [
        UsageHistorySample(1, float(_epoch(2026, 9, 29, 18, 42)), 71, None),
        UsageHistorySample(2, float(_epoch(2026, 9, 29, 18, 43)), None, 44),
    ]

    write_usage_history_csv(path, samples)

    content = path.read_text(encoding="utf-8-sig")
    rows = content.splitlines()
    assert rows[0] == "timestamp,five_hour_remaining,weekly_remaining"
    assert len(rows) == 3
    assert rows[1].endswith(",71,")
    assert rows[2].endswith(",,44")
    expected_timestamp = (
        datetime.fromtimestamp(samples[0].captured_at_epoch)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    assert rows[1].startswith(f"{expected_timestamp},")


def test_csv_export_queries_raw_rows_instead_of_display_downsampling(tmp_path: Path) -> None:
    database_path = tmp_path / "usage.sqlite3"
    start = _epoch(2026, 9, 1)
    sample_count = 4_500
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE usage_samples (
                minute_epoch INTEGER PRIMARY KEY,
                captured_at_epoch REAL NOT NULL,
                five_hour_remaining INTEGER,
                weekly_remaining INTEGER
            )"""
        )
        connection.executemany(
            "INSERT INTO usage_samples VALUES (?, ?, ?, ?)",
            (
                (start + index * 60, start + index * 60, index % 101, None if index % 3 else 50)
                for index in range(sample_count)
            ),
        )
    end = start + (sample_count - 1) * 60
    chart_samples = get_usage_samples_for_display(
        start,
        end,
        max_points=1_000,
        database_path=database_path,
    )
    export_path = tmp_path / "raw.csv"

    written = export_usage_history_csv(
        export_path,
        start,
        end,
        database_path=database_path,
    )

    assert len(chart_samples) <= 1_000
    assert written == sample_count
    assert len(export_path.read_text(encoding="utf-8-sig").splitlines()) == sample_count + 1


def test_usage_history_window_restores_saved_geometry_and_recovers_from_corruption(
    tmp_path: Path,
) -> None:
    _application()
    settings_path = tmp_path / "settings.ini"
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    first = UsageHistoryWindow(tmp_path / "usage.sqlite3", settings=settings)
    screen = QApplication.primaryScreen()
    assert screen is not None
    available = screen.availableGeometry()
    first.resize(
        min(1_100, max(first.minimumWidth(), available.width() - 40)),
        min(740, max(first.minimumHeight(), available.height() - 40)),
    )
    saved_size = first.size()
    first.close()

    restored = UsageHistoryWindow(tmp_path / "usage.sqlite3", settings=settings)
    assert restored.size() == saved_size
    restored.close()

    settings.setValue("console/usageHistory/geometry", b"damaged")
    fallback = UsageHistoryWindow(tmp_path / "usage.sqlite3", settings=settings)
    assert fallback.size().width() == 1_040
    assert fallback.size().height() == 700
    fallback.close()
