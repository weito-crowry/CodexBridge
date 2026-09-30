from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import time

from .usage import CodexUsage

_DATABASE_NAME = "usage-history.sqlite3"
_EVENT_INDEX_NAME = "idx_usage_events_occurred_at_epoch"


@dataclass(frozen=True, slots=True)
class UsageHistorySample:
    minute_epoch: int
    captured_at_epoch: float
    five_hour_remaining: int | None
    weekly_remaining: int | None


@dataclass(frozen=True, slots=True)
class UsageHistoryEvent:
    occurred_at_epoch: float
    event_type: str
    previous_weekly_remaining: int
    current_weekly_remaining: int


def default_usage_history_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if environ is None else environ
    platform_name = sys.platform if platform is None else platform
    home_path = Path.home() if home is None else home
    if platform_name.startswith("win"):
        root = values.get("LOCALAPPDATA")
        base = Path(root) if root else home_path / "AppData" / "Local"
    else:
        state_home = values.get("XDG_STATE_HOME")
        base = Path(state_home).expanduser() if state_home else home_path / ".local" / "state"
    return base / "CodexBridge" / _DATABASE_NAME


@contextmanager
def _open_database(database_path: Path | None) -> Iterator[sqlite3.Connection]:
    path = default_usage_history_path() if database_path is None else database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_samples (
                minute_epoch INTEGER PRIMARY KEY,
                captured_at_epoch REAL NOT NULL,
                five_hour_remaining INTEGER,
                weekly_remaining INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at_epoch REAL NOT NULL,
                event_type TEXT NOT NULL,
                previous_weekly_remaining INTEGER NOT NULL,
                current_weekly_remaining INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {_EVENT_INDEX_NAME} ON usage_events (occurred_at_epoch)"
        )
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_usage_sample(
    usage: CodexUsage,
    *,
    captured_at_epoch: float | None = None,
    database_path: Path | None = None,
) -> UsageHistorySample | None:
    five_hour = usage.five_hour.remaining_percent if usage.five_hour is not None else None
    weekly = usage.weekly.remaining_percent if usage.weekly is not None else None
    if five_hour is None and weekly is None:
        return None

    captured = time() if captured_at_epoch is None else captured_at_epoch
    minute_epoch = int(captured // 60 * 60)
    sample = UsageHistorySample(minute_epoch, captured, five_hour, weekly)
    with _open_database(database_path) as connection:
        previous = connection.execute(
            """
            SELECT weekly_remaining
            FROM usage_samples
            WHERE weekly_remaining IS NOT NULL
            ORDER BY minute_epoch DESC
            LIMIT 1
            """
        ).fetchone()
        previous_weekly = previous["weekly_remaining"] if previous is not None else None
        if previous_weekly is not None and weekly is not None and weekly > previous_weekly:
            connection.execute(
                """
                INSERT INTO usage_events (
                    occurred_at_epoch,
                    event_type,
                    previous_weekly_remaining,
                    current_weekly_remaining
                ) VALUES (?, 'weekly_remaining_increase', ?, ?)
                """,
                (captured, previous_weekly, weekly),
            )
        connection.execute(
            """
            INSERT INTO usage_samples (
                minute_epoch,
                captured_at_epoch,
                five_hour_remaining,
                weekly_remaining
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(minute_epoch) DO UPDATE SET
                captured_at_epoch = excluded.captured_at_epoch,
                five_hour_remaining = excluded.five_hour_remaining,
                weekly_remaining = excluded.weekly_remaining
            """,
            (minute_epoch, captured, five_hour, weekly),
        )
    return sample


def get_usage_samples(
    start_epoch: float,
    end_epoch: float,
    *,
    database_path: Path | None = None,
) -> list[UsageHistorySample]:
    if end_epoch < start_epoch:
        return []
    with _open_database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT minute_epoch, captured_at_epoch, five_hour_remaining, weekly_remaining
            FROM usage_samples
            WHERE captured_at_epoch >= ? AND captured_at_epoch <= ?
            ORDER BY minute_epoch ASC
            """,
            (start_epoch, end_epoch),
        ).fetchall()
    return [
        UsageHistorySample(
            row["minute_epoch"],
            row["captured_at_epoch"],
            row["five_hour_remaining"],
            row["weekly_remaining"],
        )
        for row in rows
    ]


def get_recent_usage_events(
    limit: int = 20,
    *,
    database_path: Path | None = None,
) -> list[UsageHistoryEvent]:
    if limit <= 0:
        return []
    with _open_database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT occurred_at_epoch, event_type, previous_weekly_remaining,
                   current_weekly_remaining
            FROM usage_events
            ORDER BY occurred_at_epoch DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        UsageHistoryEvent(
            row["occurred_at_epoch"],
            row["event_type"],
            row["previous_weekly_remaining"],
            row["current_weekly_remaining"],
        )
        for row in rows
    ]
