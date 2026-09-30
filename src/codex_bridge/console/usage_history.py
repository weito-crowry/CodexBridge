from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import time

from .usage import CodexUsage

_DATABASE_NAME = "usage-history.sqlite3"
_EVENT_INDEX_NAME = "idx_usage_events_occurred_at_epoch"
WEEKLY_REMAINING_INCREASE_EVENT = "weekly_remaining_increase"


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
        recent_weekly = connection.execute(
            """
            SELECT minute_epoch, captured_at_epoch, weekly_remaining
            FROM usage_samples
            WHERE weekly_remaining IS NOT NULL
            ORDER BY minute_epoch DESC
            LIMIT 4
            """
        ).fetchall()
        if len(recent_weekly) == 4:
            confirmation_2, confirmation_1, candidate, baseline = recent_weekly
            previous_weekly = baseline["weekly_remaining"]
            candidate_weekly = candidate["weekly_remaining"]
            confirmed_weekly_1 = confirmation_1["weekly_remaining"]
            confirmed_weekly_2 = confirmation_2["weekly_remaining"]
            candidate_minute = candidate["minute_epoch"]
            if (
                candidate_weekly > previous_weekly
                and confirmed_weekly_1 > previous_weekly
                and confirmed_weekly_2 > previous_weekly
            ):
                existing_event = connection.execute(
                    """
                    SELECT 1
                    FROM usage_events
                    WHERE event_type = ?
                      AND occurred_at_epoch >= ?
                      AND occurred_at_epoch < ?
                    LIMIT 1
                    """,
                    (
                        WEEKLY_REMAINING_INCREASE_EVENT,
                        candidate_minute,
                        candidate_minute + 60,
                    ),
                ).fetchone()
                if existing_event is None:
                    connection.execute(
                        """
                        INSERT INTO usage_events (
                            occurred_at_epoch,
                            event_type,
                            previous_weekly_remaining,
                            current_weekly_remaining
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            candidate["captured_at_epoch"],
                            WEEKLY_REMAINING_INCREASE_EVENT,
                            previous_weekly,
                            candidate_weekly,
                        ),
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


def get_usage_samples_for_display(
    start_epoch: float,
    end_epoch: float,
    *,
    max_points: int = 4_000,
    database_path: Path | None = None,
) -> list[UsageHistorySample]:
    if end_epoch < start_epoch:
        return []
    if max_points <= 0:
        raise ValueError("max_points must be positive")

    duration_seconds = max(0.0, end_epoch - start_epoch)
    bucket_seconds = max(60, ceil(duration_seconds / max_points))
    with _open_database(database_path) as connection:
        rows = connection.execute(
            """
            WITH ranked_samples AS (
                SELECT minute_epoch, captured_at_epoch, five_hour_remaining,
                       weekly_remaining,
                       ROW_NUMBER() OVER (
                           PARTITION BY MIN(
                               CAST((captured_at_epoch - ?) / ? AS INTEGER), ?
                           )
                           ORDER BY captured_at_epoch DESC, minute_epoch DESC
                       ) AS bucket_rank
                FROM usage_samples
                WHERE captured_at_epoch >= ? AND captured_at_epoch <= ?
            )
            SELECT minute_epoch, captured_at_epoch, five_hour_remaining, weekly_remaining
            FROM ranked_samples
            WHERE bucket_rank = 1
            ORDER BY captured_at_epoch ASC, minute_epoch ASC
            """,
            (start_epoch, bucket_seconds, max_points - 1, start_epoch, end_epoch),
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


def get_latest_usage_remaining(
    start_epoch: float,
    end_epoch: float,
    *,
    database_path: Path | None = None,
) -> tuple[int | None, int | None]:
    if end_epoch < start_epoch:
        return None, None
    with _open_database(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT five_hour_remaining FROM usage_samples
                 WHERE captured_at_epoch >= ? AND captured_at_epoch <= ?
                   AND five_hour_remaining IS NOT NULL
                 ORDER BY captured_at_epoch DESC, minute_epoch DESC LIMIT 1)
                    AS five_hour_remaining,
                (SELECT weekly_remaining FROM usage_samples
                 WHERE captured_at_epoch >= ? AND captured_at_epoch <= ?
                   AND weekly_remaining IS NOT NULL
                 ORDER BY captured_at_epoch DESC, minute_epoch DESC LIMIT 1)
                    AS weekly_remaining
            """,
            (start_epoch, end_epoch, start_epoch, end_epoch),
        ).fetchone()
    return row["five_hour_remaining"], row["weekly_remaining"]


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


def get_usage_events(
    start_epoch: float,
    end_epoch: float,
    *,
    database_path: Path | None = None,
) -> list[UsageHistoryEvent]:
    if end_epoch < start_epoch:
        return []
    with _open_database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT occurred_at_epoch, event_type, previous_weekly_remaining,
                   current_weekly_remaining
            FROM usage_events
            WHERE occurred_at_epoch >= ? AND occurred_at_epoch <= ?
            ORDER BY occurred_at_epoch ASC, id ASC
            """,
            (start_epoch, end_epoch),
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


def get_confirmed_usage_events(
    start_epoch: float,
    end_epoch: float,
    *,
    database_path: Path | None = None,
) -> list[UsageHistoryEvent]:
    events = get_usage_events(start_epoch, end_epoch, database_path=database_path)
    confirmed: list[UsageHistoryEvent] = []
    seen_candidate_minutes: set[int] = set()
    with _open_database(database_path) as connection:
        for event in events:
            if event.event_type != WEEKLY_REMAINING_INCREASE_EVENT:
                continue
            candidate_minute = int(event.occurred_at_epoch // 60 * 60)
            if candidate_minute in seen_candidate_minutes:
                continue
            baseline = connection.execute(
                """
                SELECT weekly_remaining
                FROM usage_samples
                WHERE minute_epoch < ? AND weekly_remaining IS NOT NULL
                ORDER BY minute_epoch DESC
                LIMIT 1
                """,
                (candidate_minute,),
            ).fetchone()
            candidate = connection.execute(
                """
                SELECT weekly_remaining
                FROM usage_samples
                WHERE minute_epoch = ? AND weekly_remaining IS NOT NULL
                LIMIT 1
                """,
                (candidate_minute,),
            ).fetchone()
            confirmations = connection.execute(
                """
                SELECT weekly_remaining
                FROM usage_samples
                WHERE minute_epoch > ? AND weekly_remaining IS NOT NULL
                ORDER BY minute_epoch ASC
                LIMIT 2
                """,
                (candidate_minute,),
            ).fetchall()
            if baseline is None or candidate is None or len(confirmations) < 2:
                continue
            previous_weekly = baseline["weekly_remaining"]
            candidate_weekly = candidate["weekly_remaining"]
            if (
                event.previous_weekly_remaining == previous_weekly
                and event.current_weekly_remaining == candidate_weekly
                and candidate_weekly > previous_weekly
                and confirmations[0]["weekly_remaining"] > previous_weekly
                and confirmations[1]["weekly_remaining"] > previous_weekly
            ):
                confirmed.append(event)
                seen_candidate_minutes.add(candidate_minute)
    return confirmed
