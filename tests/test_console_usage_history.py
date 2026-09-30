from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_bridge.console.usage import CodexUsage, UsageWindow
from codex_bridge.console.usage_history import (
    UsageHistoryEvent,
    UsageHistorySample,
    default_usage_history_path,
    get_recent_usage_events,
    get_usage_samples,
    record_usage_sample,
)


def _usage(five_hour: int | None, weekly: int | None) -> CodexUsage:
    return CodexUsage(
        five_hour=UsageWindow(300, five_hour, "reset") if five_hour is not None else None,
        weekly=UsageWindow(10080, weekly, "reset") if weekly is not None else None,
    )


def test_usage_history_path_uses_local_app_data_on_windows(tmp_path: Path) -> None:
    path = default_usage_history_path(
        {"LOCALAPPDATA": r"C:\Users\sample\AppData\Local"},
        platform="win32",
        home=tmp_path,
    )

    assert path == Path(r"C:\Users\sample\AppData\Local\CodexBridge\usage-history.sqlite3")


def test_usage_history_path_falls_back_to_roaming_home_on_windows(tmp_path: Path) -> None:
    path = default_usage_history_path({}, platform="win32", home=tmp_path)

    assert path == tmp_path / "AppData" / "Local" / "CodexBridge" / "usage-history.sqlite3"


def test_usage_history_schema_has_sample_and_event_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    record_usage_sample(_usage(70, 60), captured_at_epoch=120.0, database_path=database_path)

    connection = sqlite3.connect(database_path)
    try:
        sample_info = connection.execute("PRAGMA table_info(usage_samples)").fetchall()
        sample_columns = {row[1] for row in sample_info}
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
        event_indexes = {row[1] for row in connection.execute("PRAGMA index_list(usage_events)")}
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'usage_events'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert sample_columns == {
        "minute_epoch",
        "captured_at_epoch",
        "five_hour_remaining",
        "weekly_remaining",
    }
    sample_definitions = {row[1]: row for row in sample_info}
    assert sample_definitions["minute_epoch"][2] == "INTEGER"
    assert sample_definitions["minute_epoch"][5] == 1
    assert sample_definitions["captured_at_epoch"][2] == "REAL"
    assert sample_definitions["five_hour_remaining"][3] == 0
    assert sample_definitions["weekly_remaining"][3] == 0
    assert event_columns == {
        "id",
        "occurred_at_epoch",
        "event_type",
        "previous_weekly_remaining",
        "current_weekly_remaining",
    }
    assert "idx_usage_events_occurred_at_epoch" in event_indexes
    assert "AUTOINCREMENT" in event_sql


def test_first_usage_sample_is_saved_with_nullable_windows(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"

    saved = record_usage_sample(
        _usage(71, None), captured_at_epoch=125.5, database_path=database_path
    )

    assert saved == UsageHistorySample(120, 125.5, 71, None)
    assert get_usage_samples(0, 180, database_path=database_path) == [saved]
    assert get_recent_usage_events(database_path=database_path) == []


def test_same_minute_sample_upserts_the_existing_minute(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    record_usage_sample(_usage(70, 30), captured_at_epoch=125.0, database_path=database_path)

    saved = record_usage_sample(
        _usage(68, 30), captured_at_epoch=179.0, database_path=database_path
    )

    assert saved == UsageHistorySample(120, 179.0, 68, 30)
    assert get_usage_samples(0, 180, database_path=database_path) == [saved]
    assert get_recent_usage_events(database_path=database_path) == []


@pytest.mark.parametrize(
    ("previous_weekly", "current_weekly"),
    [(30, 30), (30, 29), (None, 80), (30, None)],
)
def test_weekly_event_is_not_saved_without_a_non_null_increase(
    tmp_path: Path, previous_weekly: int | None, current_weekly: int | None
) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    record_usage_sample(
        _usage(50, previous_weekly), captured_at_epoch=60.0, database_path=database_path
    )

    record_usage_sample(
        _usage(60, current_weekly), captured_at_epoch=120.0, database_path=database_path
    )

    assert get_recent_usage_events(database_path=database_path) == []


def test_weekly_increase_is_saved_once_and_detected_after_database_reopen(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    record_usage_sample(_usage(50, 30), captured_at_epoch=60.0, database_path=database_path)

    event = record_usage_sample(
        _usage(55, 80), captured_at_epoch=120.0, database_path=database_path
    )
    repeated = record_usage_sample(
        _usage(55, 80), captured_at_epoch=180.0, database_path=database_path
    )

    events = get_recent_usage_events(database_path=database_path)
    assert event == UsageHistorySample(120, 120.0, 55, 80)
    assert repeated == UsageHistorySample(180, 180.0, 55, 80)
    assert events == [UsageHistoryEvent(120.0, "weekly_remaining_increase", 30, 80)]


def test_weekly_increase_compares_against_last_non_null_sample(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    record_usage_sample(_usage(50, 30), captured_at_epoch=60.0, database_path=database_path)
    record_usage_sample(_usage(55, None), captured_at_epoch=120.0, database_path=database_path)

    record_usage_sample(_usage(60, 80), captured_at_epoch=180.0, database_path=database_path)

    assert get_recent_usage_events(database_path=database_path) == [
        UsageHistoryEvent(180.0, "weekly_remaining_increase", 30, 80)
    ]


def test_usage_history_sample_range_is_ascending_and_inclusive(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    for captured_at in (180.0, 60.0, 120.0):
        record_usage_sample(
            _usage(70, 60), captured_at_epoch=captured_at, database_path=database_path
        )

    samples = get_usage_samples(60, 120, database_path=database_path)

    assert [sample.minute_epoch for sample in samples] == [60, 120]


def test_recent_weekly_events_are_limited_and_newest_first(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"
    for index, weekly in enumerate((30, 40, 50, 60), start=1):
        record_usage_sample(
            _usage(70, weekly), captured_at_epoch=index * 60.0, database_path=database_path
        )

    events = get_recent_usage_events(limit=2, database_path=database_path)

    assert events == [
        UsageHistoryEvent(240.0, "weekly_remaining_increase", 50, 60),
        UsageHistoryEvent(180.0, "weekly_remaining_increase", 40, 50),
    ]
    assert get_recent_usage_events(limit=0, database_path=database_path) == []


def test_sample_without_any_usage_window_is_not_persisted(tmp_path: Path) -> None:
    database_path = tmp_path / "usage-history.sqlite3"

    saved = record_usage_sample(CodexUsage(), captured_at_epoch=120.0, database_path=database_path)

    assert saved is None
    assert not database_path.exists()
