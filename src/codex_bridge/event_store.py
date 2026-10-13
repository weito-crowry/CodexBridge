from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA_VERSION = 1
_SECRET_ROTATION_SECONDS = 300


class EventStoreError(RuntimeError):
    """Raised when the local MCP Events database cannot be used."""


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    event_id: str
    subscription_id: str
    callback_url: str
    signing_secret: str
    previous_signing_secret: str | None
    previous_secret_expires_at: str | None
    payload_json: str
    attempt_count: int


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def subscription_identifier(
    principal: str, callback_url: str, event_name: str, canonical_arguments: str
) -> str:
    key = canonical_json(
        {
            "principal": principal,
            "callback_url": callback_url,
            "event_name": event_name,
            "arguments": json.loads(canonical_arguments),
        }
    )
    return "sub_" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EventStore:
    def __init__(self, database_path: Path | str) -> None:
        self.path = Path(database_path).expanduser()
        self._initialized = False

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current_version > _SCHEMA_VERSION:
                    raise EventStoreError("MCP Events database schema is newer than this server")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS subscriptions (
                        subscription_id TEXT PRIMARY KEY,
                        principal TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        canonical_arguments TEXT NOT NULL,
                        callback_url TEXT NOT NULL,
                        signing_secret TEXT NOT NULL,
                        previous_signing_secret TEXT,
                        previous_secret_expires_at TEXT,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (principal, event_name, canonical_arguments, callback_url)
                    );
                    CREATE INDEX IF NOT EXISTS idx_event_subscriptions_expiry
                        ON subscriptions(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_event_subscriptions_name_expiry
                        ON subscriptions(event_name, expires_at);
                    CREATE TABLE IF NOT EXISTS outbox (
                        event_id TEXT PRIMARY KEY,
                        subscription_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        event_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        delivery_state TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL,
                        next_attempt_at TEXT,
                        created_at TEXT NOT NULL,
                        delivered_at TEXT,
                        UNIQUE (subscription_id, target_id, thread_id, turn_id, event_name)
                    );
                    CREATE INDEX IF NOT EXISTS idx_event_outbox_due
                        ON outbox(delivery_state, next_attempt_at, created_at);
                    CREATE INDEX IF NOT EXISTS idx_event_outbox_retention
                        ON outbox(delivery_state, created_at);
                    """
                )
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._initialized = True
        except sqlite3.Error as exc:
            raise EventStoreError("MCP Events database initialization failed") from exc

    def close(self) -> None:
        self._initialized = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise EventStoreError("MCP Events database is not initialized")

    def upsert_subscription(
        self,
        *,
        principal: str,
        event_name: str,
        canonical_arguments: str,
        callback_url: str,
        signing_secret: str,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> str:
        self._require_initialized()
        identifier = subscription_identifier(
            principal, callback_url, event_name, canonical_arguments
        )
        current = utc_now() if now is None else now.astimezone(UTC)
        created = utc_iso(current)
        expiration = utc_iso(expires_at)
        rotation_expiry = utc_iso(current + timedelta(seconds=_SECRET_ROTATION_SECONDS))
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT signing_secret, expires_at FROM subscriptions WHERE subscription_id = ?",
                (identifier,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO subscriptions (
                        subscription_id, principal, event_name, canonical_arguments,
                        callback_url, signing_secret, previous_signing_secret,
                        previous_secret_expires_at, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                    (
                        identifier,
                        principal,
                        event_name,
                        canonical_arguments,
                        callback_url,
                        signing_secret,
                        expiration,
                        created,
                        created,
                    ),
                )
            elif existing["expires_at"] <= created:
                connection.execute(
                    """UPDATE subscriptions SET signing_secret = ?,
                        previous_signing_secret = NULL, previous_secret_expires_at = NULL,
                        expires_at = ?, created_at = ?, updated_at = ?
                        WHERE subscription_id = ?""",
                    (signing_secret, expiration, created, created, identifier),
                )
            elif existing["signing_secret"] != signing_secret:
                connection.execute(
                    """UPDATE subscriptions SET signing_secret = ?,
                        previous_signing_secret = ?, previous_secret_expires_at = ?,
                        expires_at = ?, updated_at = ? WHERE subscription_id = ?""",
                    (
                        signing_secret,
                        existing["signing_secret"],
                        rotation_expiry,
                        expiration,
                        created,
                        identifier,
                    ),
                )
            else:
                connection.execute(
                    """UPDATE subscriptions SET expires_at = ?, updated_at = ?,
                        previous_signing_secret = CASE
                            WHEN previous_secret_expires_at <= ? THEN NULL
                            ELSE previous_signing_secret END,
                        previous_secret_expires_at = CASE
                            WHEN previous_secret_expires_at <= ? THEN NULL
                            ELSE previous_secret_expires_at END
                        WHERE subscription_id = ?""",
                    (expiration, created, created, created, identifier),
                )
        return identifier

    def unsubscribe(
        self,
        *,
        principal: str,
        event_name: str,
        canonical_arguments: str,
        callback_url: str,
        now: datetime | None = None,
    ) -> None:
        self._require_initialized()
        identifier = subscription_identifier(
            principal, callback_url, event_name, canonical_arguments
        )
        current = utc_iso(utc_now() if now is None else now)
        with self._transaction() as connection:
            connection.execute(
                """UPDATE subscriptions SET expires_at = ?, updated_at = ?
                    WHERE subscription_id = ? AND principal = ?""",
                (current, current, identifier, principal),
            )
            connection.execute(
                """UPDATE outbox SET delivery_state = 'cancelled'
                    WHERE subscription_id = ? AND delivery_state IN ('pending', 'sending')""",
                (identifier,),
            )

    def record_terminal(
        self,
        *,
        target_id: str,
        thread_id: str,
        turn_id: str,
        status: str,
        occurred_at: datetime | None = None,
    ) -> int:
        self._require_initialized()
        if status not in {"completed", "failed", "interrupted"}:
            return 0
        created = utc_now() if occurred_at is None else occurred_at.astimezone(UTC)
        timestamp = utc_iso(created)
        inserted = 0
        with self._transaction() as connection:
            subscriptions = connection.execute(
                "SELECT subscription_id, canonical_arguments FROM subscriptions "
                "WHERE event_name = ? AND created_at <= ? AND expires_at > ?",
                ("codex.turn.finished", timestamp, timestamp),
            ).fetchall()
            for subscription in subscriptions:
                try:
                    arguments = json.loads(subscription["canonical_arguments"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(arguments, dict) or arguments.get("thread_id") != thread_id:
                    continue
                selected_turn = arguments.get("turn_id")
                if selected_turn is not None and selected_turn != turn_id:
                    continue
                event_id = "evt_" + uuid.uuid4().hex
                data = {
                    "target_id": target_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "status": status,
                }
                payload = canonical_json(
                    {
                        "eventId": event_id,
                        "name": "codex.turn.finished",
                        "timestamp": timestamp,
                        "data": data,
                        "cursor": None,
                    }
                )
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO outbox (
                        event_id, subscription_id, target_id, thread_id, turn_id,
                        event_name, status, payload_json, delivery_state, attempt_count,
                        next_attempt_at, created_at, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?, NULL)""",
                    (
                        event_id,
                        subscription["subscription_id"],
                        target_id,
                        thread_id,
                        turn_id,
                        "codex.turn.finished",
                        status,
                        payload,
                        timestamp,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def claim_next_due(self, now: datetime | None = None) -> OutboxDelivery | None:
        self._require_initialized()
        current = utc_iso(utc_now() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT o.*, s.callback_url, s.signing_secret,
                        s.previous_signing_secret, s.previous_secret_expires_at
                    FROM outbox AS o JOIN subscriptions AS s
                        ON s.subscription_id = o.subscription_id
                    WHERE o.delivery_state = 'pending'
                        AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= ?)
                        AND s.expires_at > ?
                    ORDER BY COALESCE(o.next_attempt_at, o.created_at), o.created_at
                    LIMIT 1""",
                (current, current),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE outbox SET delivery_state = 'sending', attempt_count = attempt_count + 1 "
                "WHERE event_id = ? AND delivery_state = 'pending'",
                (row["event_id"],),
            )
            return OutboxDelivery(
                event_id=row["event_id"],
                subscription_id=row["subscription_id"],
                callback_url=row["callback_url"],
                signing_secret=row["signing_secret"],
                previous_signing_secret=row["previous_signing_secret"],
                previous_secret_expires_at=row["previous_secret_expires_at"],
                payload_json=row["payload_json"],
                attempt_count=row["attempt_count"] + 1,
            )

    def mark_delivered(self, event_id: str, delivered_at: datetime | None = None) -> None:
        self._set_delivery_state(
            event_id,
            "delivered",
            delivered_at=utc_iso(utc_now() if delivered_at is None else delivered_at),
        )

    def schedule_retry(self, event_id: str, next_attempt_at: datetime) -> None:
        self._set_delivery_state(
            event_id,
            "pending",
            next_attempt_at=utc_iso(next_attempt_at),
        )

    def mark_failed(self, event_id: str) -> None:
        self._set_delivery_state(event_id, "failed")

    def mark_cancelled(self, event_id: str) -> None:
        self._set_delivery_state(event_id, "cancelled")

    def _set_delivery_state(
        self,
        event_id: str,
        state: str,
        *,
        delivered_at: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        self._require_initialized()
        with self._transaction() as connection:
            connection.execute(
                """UPDATE outbox SET delivery_state = ?, delivered_at = ?, next_attempt_at = ?
                    WHERE event_id = ? AND delivery_state = 'sending'""",
                (state, delivered_at, next_attempt_at, event_id),
            )

    def reset_inflight(self) -> None:
        self._require_initialized()
        with self._transaction() as connection:
            connection.execute(
                """UPDATE outbox SET delivery_state = 'pending', next_attempt_at = NULL
                    WHERE delivery_state = 'sending'"""
            )

    def next_attempt_delay(
        self, now: datetime | None = None, *, maximum_seconds: float = 30.0
    ) -> float:
        self._require_initialized()
        current = utc_iso(utc_now() if now is None else now)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT MIN(next_attempt_at) FROM outbox WHERE delivery_state = 'pending' "
                "AND next_attempt_at IS NOT NULL"
            ).fetchone()
        if row is None or row[0] is None:
            return maximum_seconds
        next_at = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        current_at = datetime.fromisoformat(current.replace("Z", "+00:00"))
        return max(0.0, min(maximum_seconds, (next_at - current_at).total_seconds()))

    def cleanup(self, now: datetime | None = None) -> None:
        self._require_initialized()
        current = utc_now() if now is None else now.astimezone(UTC)
        cutoff_subscriptions = utc_iso(current - timedelta(hours=24))
        cutoff_outbox = utc_iso(current - timedelta(days=7))
        with self._transaction() as connection:
            connection.execute(
                """UPDATE subscriptions SET previous_signing_secret = NULL,
                    previous_secret_expires_at = NULL
                    WHERE previous_secret_expires_at IS NOT NULL
                        AND previous_secret_expires_at <= ?""",
                (utc_iso(current),),
            )
            connection.execute(
                "UPDATE outbox SET delivery_state = 'cancelled' WHERE delivery_state = 'pending' "
                "AND subscription_id IN (SELECT subscription_id FROM subscriptions "
                "WHERE expires_at <= ?)",
                (utc_iso(current),),
            )
            connection.execute(
                "DELETE FROM subscriptions WHERE expires_at <= ?", (cutoff_subscriptions,)
            )
            connection.execute(
                """DELETE FROM outbox WHERE delivery_state IN
                    ('delivered', 'failed', 'cancelled') AND created_at <= ?""",
                (cutoff_outbox,),
            )

    def subscription_secret(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> tuple[str, str | None, str | None] | None:
        self._require_initialized()
        with self._connection() as connection:
            row = connection.execute(
                """SELECT signing_secret, previous_signing_secret, previous_secret_expires_at
                    FROM subscriptions WHERE subscription_id = ? AND expires_at > ?""",
                (subscription_id, utc_iso(utc_now() if now is None else now)),
            ).fetchone()
        if row is None:
            return None
        return (
            row["signing_secret"],
            row["previous_signing_secret"],
            row["previous_secret_expires_at"],
        )

    def subscription_is_active(self, subscription_id: str, *, now: datetime | None = None) -> bool:
        self._require_initialized()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM subscriptions WHERE subscription_id = ? AND expires_at > ?",
                (subscription_id, utc_iso(utc_now() if now is None else now)),
            ).fetchone()
        return row is not None

    def count_outbox(self, *, delivery_state: str | None = None) -> int:
        self._require_initialized()
        with self._connection() as connection:
            if delivery_state is None:
                row = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE delivery_state = ?", (delivery_state,)
                ).fetchone()
        return int(row[0])
