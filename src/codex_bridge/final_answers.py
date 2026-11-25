from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from time import time

MAX_FINAL_ANSWER_UTF16_CODE_UNITS = 256 * 1024
MAX_IDENTIFIER_UTF16_CODE_UNITS = 256
FINAL_ANSWER_SOURCE = "app_server.item/completed.agentMessage.phase=final_answer"

_SCHEMA_VERSION = 1
_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_RETAINED_TURNS = 256
_MAX_RETAINED_TEXT_BYTES = 24 * 1024 * 1024
_MAX_DATABASE_BYTES = 64 * 1024 * 1024
_MAX_DATABASE_PAGES = _MAX_DATABASE_BYTES // 4096
_ISSUE_REASONS = frozenset(
    {
        "final_answer_too_large",
        "invalid_final_answer_text",
        "item_id_content_collision",
        "multiple_final_answers",
        "terminal_status_conflict",
    }
)


class FinalAnswerStoreError(RuntimeError):
    """A safe storage failure without database or answer content."""


def default_final_answer_database_path(
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
    return base / "CodexBridge" / "final-answers.sqlite3"


def is_valid_final_answer_identifier(value: object) -> bool:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    try:
        units = len(value.encode("utf-16-le", errors="strict")) // 2
    except UnicodeEncodeError:
        return False
    return (
        units <= MAX_IDENTIFIER_UTF16_CODE_UNITS
        and value == value.strip()
        and value.isprintable()
        and not any(character in value for character in ("/", "\\", "\x00"))
    )


class FinalAnswerStore:
    """Bounded SQLite evidence store for completed final-answer items."""

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        clock: Callable[[], float] = time,
        retention_seconds: int = _RETENTION_SECONDS,
        max_retained_turns: int = _MAX_RETAINED_TURNS,
        max_retained_text_bytes: int = _MAX_RETAINED_TEXT_BYTES,
    ) -> None:
        if retention_seconds <= 0 or max_retained_turns <= 0 or max_retained_text_bytes <= 0:
            raise ValueError("final answer retention limits must be positive")
        self.database_path = database_path or default_final_answer_database_path()
        self._clock = clock
        self._retention_seconds = retention_seconds
        self._max_retained_turns = max_retained_turns
        self._max_retained_text_bytes = max_retained_text_bytes

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        path = self.database_path
        connection: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists() and path.stat().st_size > _MAX_DATABASE_BYTES:
                raise FinalAnswerStoreError("storage_unavailable")
            connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            page_count = connection.execute(
                f"PRAGMA max_page_count={_MAX_DATABASE_PAGES}"
            ).fetchone()
            if not page_count or int(page_count[0]) > _MAX_DATABASE_PAGES:
                raise FinalAnswerStoreError("storage_unavailable")
            self._initialize(connection)
            if os.name != "nt":
                os.chmod(path, 0o600)
            yield connection
        except FinalAnswerStoreError:
            raise
        except (OSError, sqlite3.Error, UnicodeError, TypeError, ValueError):
            raise FinalAnswerStoreError("storage_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        path = self.database_path
        connection: sqlite3.Connection | None = None
        try:
            if not path.is_file() or path.stat().st_size > _MAX_DATABASE_BYTES:
                raise FinalAnswerStoreError("storage_unavailable")
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.1,
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != _SCHEMA_VERSION:
                raise FinalAnswerStoreError("storage_schema_unsupported")
            # A deferred read transaction keeps the multi-query result on one snapshot.
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except FinalAnswerStoreError:
            raise
        except (OSError, sqlite3.Error, UnicodeError, TypeError, ValueError):
            raise FinalAnswerStoreError("storage_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION or version not in {0, _SCHEMA_VERSION}:
                raise FinalAnswerStoreError("storage_schema_unsupported")
            if version == 0:
                connection.execute(
                    """
                    CREATE TABLE final_items (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        answer_text TEXT NOT NULL,
                        answer_sha256 TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (thread_id, turn_id, item_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE terminal_evidence (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (thread_id, turn_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE issues (
                        thread_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (thread_id, turn_id, reason)
                    )
                    """
                )
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _require_identity(thread_id: str, turn_id: str) -> None:
        if not is_valid_final_answer_identifier(thread_id) or not is_valid_final_answer_identifier(
            turn_id
        ):
            raise ValueError("final answer identity is malformed")

    @staticmethod
    def _add_issue(
        connection: sqlite3.Connection,
        thread_id: str,
        turn_id: str,
        reason: str,
        now: float,
    ) -> None:
        if reason not in _ISSUE_REASONS:
            raise ValueError("final answer issue is not recognized")
        connection.execute(
            """
            INSERT INTO issues (thread_id, turn_id, reason, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (thread_id, turn_id, reason)
            DO UPDATE SET updated_at=excluded.updated_at
            """,
            (thread_id, turn_id, reason, now),
        )

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        cutoff = now - self._retention_seconds
        for table in ("final_items", "terminal_evidence", "issues"):
            connection.execute(f"DELETE FROM {table} WHERE updated_at < ?", (cutoff,))

        turns = connection.execute(
            """
            SELECT thread_id, turn_id, MAX(updated_at) AS last_seen FROM (
                SELECT thread_id, turn_id, updated_at FROM final_items
                UNION ALL
                SELECT thread_id, turn_id, updated_at FROM terminal_evidence
                UNION ALL
                SELECT thread_id, turn_id, updated_at FROM issues
            )
            GROUP BY thread_id, turn_id
            ORDER BY last_seen DESC
            """
        ).fetchall()
        for thread_id, turn_id, _last_seen in turns[self._max_retained_turns :]:
            self._delete_turn(connection, thread_id, turn_id)

        while self._retained_text_bytes(connection) > self._max_retained_text_bytes:
            oldest = connection.execute(
                """
                SELECT thread_id, turn_id, MAX(updated_at) AS last_seen FROM final_items
                GROUP BY thread_id, turn_id
                ORDER BY last_seen ASC LIMIT 1
                """
            ).fetchone()
            if oldest is None:
                break
            self._delete_turn(connection, oldest[0], oldest[1])

    @staticmethod
    def _retained_text_bytes(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(length(CAST(answer_text AS BLOB))), 0) FROM final_items"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _delete_turn(connection: sqlite3.Connection, thread_id: str, turn_id: str) -> None:
        for table in ("final_items", "terminal_evidence", "issues"):
            connection.execute(
                f"DELETE FROM {table} WHERE thread_id=? AND turn_id=?", (thread_id, turn_id)
            )

    def record_final_answer(self, thread_id: str, turn_id: str, item_id: str, text: object) -> None:
        self._require_identity(thread_id, turn_id)
        if not is_valid_final_answer_identifier(item_id):
            return
        reason: str | None = None
        encoded: bytes | None = None
        if not isinstance(text, str) or not text.strip():
            reason = "invalid_final_answer_text"
        else:
            try:
                units = len(text.encode("utf-16-le", errors="strict")) // 2
                encoded = text.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                reason = "invalid_final_answer_text"
            else:
                if units > MAX_FINAL_ANSWER_UTF16_CODE_UNITS:
                    reason = "final_answer_too_large"

        now = self._clock()
        with self._connection() as connection:
            with self._transaction(connection):
                if reason is not None:
                    self._add_issue(connection, thread_id, turn_id, reason, now)
                else:
                    assert isinstance(text, str) and encoded is not None
                    digest = hashlib.sha256(encoded).hexdigest()
                    existing = connection.execute(
                        """
                        SELECT answer_text, answer_sha256 FROM final_items
                        WHERE thread_id=? AND turn_id=? AND item_id=?
                        """,
                        (thread_id, turn_id, item_id),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != text or existing[1] != digest:
                            self._add_issue(
                                connection,
                                thread_id,
                                turn_id,
                                "item_id_content_collision",
                                now,
                            )
                        else:
                            connection.execute(
                                """
                                UPDATE final_items SET updated_at=?
                                WHERE thread_id=? AND turn_id=? AND item_id=?
                                """,
                                (now, thread_id, turn_id, item_id),
                            )
                    else:
                        count = int(
                            connection.execute(
                                """
                                SELECT COUNT(*) FROM final_items
                                WHERE thread_id=? AND turn_id=?
                                """,
                                (thread_id, turn_id),
                            ).fetchone()[0]
                        )
                        if count >= 2:
                            self._add_issue(
                                connection, thread_id, turn_id, "multiple_final_answers", now
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO final_items (
                                    thread_id, turn_id, item_id, answer_text, answer_sha256,
                                    updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (thread_id, turn_id, item_id, text, digest, now),
                            )
                            if count == 1:
                                self._add_issue(
                                    connection, thread_id, turn_id, "multiple_final_answers", now
                                )
                self._prune(connection, now)

    def record_terminal(self, thread_id: str, turn_id: str, status: str) -> None:
        self._require_identity(thread_id, turn_id)
        if status not in {"completed", "failed", "interrupted", "unknown"}:
            status = "unknown"
        now = self._clock()
        with self._connection() as connection:
            with self._transaction(connection):
                existing = connection.execute(
                    """
                    SELECT status FROM terminal_evidence WHERE thread_id=? AND turn_id=?
                    """,
                    (thread_id, turn_id),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO terminal_evidence (thread_id, turn_id, status, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (thread_id, turn_id, status, now),
                    )
                elif existing[0] == status:
                    connection.execute(
                        """
                        UPDATE terminal_evidence SET updated_at=?
                        WHERE thread_id=? AND turn_id=?
                        """,
                        (now, thread_id, turn_id),
                    )
                else:
                    self._add_issue(connection, thread_id, turn_id, "terminal_status_conflict", now)
                self._prune(connection, now)

    def read(self, thread_id: str, turn_id: str) -> dict[str, object]:
        self._require_identity(thread_id, turn_id)
        result: dict[str, object] = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": "unavailable",
            "reason": "no_confirmed_final_answer",
        }
        cutoff = self._clock() - self._retention_seconds
        if not self.database_path.exists():
            if self.database_path.parent.exists() and not self.database_path.parent.is_dir():
                result["reason"] = "storage_unavailable"
            return result
        try:
            with self._read_connection() as connection:
                issues = connection.execute(
                    """
                    SELECT reason FROM issues
                    WHERE thread_id=? AND turn_id=? AND updated_at>=?
                    ORDER BY reason
                    """,
                    (thread_id, turn_id, cutoff),
                ).fetchall()
                items = connection.execute(
                    """
                    SELECT item_id, answer_text, answer_sha256 FROM final_items
                    WHERE thread_id=? AND turn_id=? AND updated_at>=?
                    ORDER BY item_id
                    """,
                    (thread_id, turn_id, cutoff),
                ).fetchall()
                terminal = connection.execute(
                    """
                    SELECT status FROM terminal_evidence
                    WHERE thread_id=? AND turn_id=? AND updated_at>=?
                    """,
                    (thread_id, turn_id, cutoff),
                ).fetchone()
        except FinalAnswerStoreError:
            result["reason"] = "storage_unavailable"
            return result

        issue_reasons = {str(row[0]) for row in issues}
        for reason in (
            "item_id_content_collision",
            "multiple_final_answers",
            "terminal_status_conflict",
            "final_answer_too_large",
            "invalid_final_answer_text",
        ):
            if reason in issue_reasons:
                result["status"] = "incomplete"
                result["reason"] = reason
                return result
        if len(items) > 1:
            result["status"] = "incomplete"
            result["reason"] = "multiple_final_answers"
            return result
        if terminal is None:
            if items:
                result["status"] = "pending"
                result["reason"] = "turn_terminal_not_observed"
            return result
        terminal_status = str(terminal[0])
        if terminal_status != "completed":
            result["status"] = "incomplete"
            result["reason"] = {
                "failed": "turn_failed",
                "interrupted": "turn_interrupted",
            }.get(terminal_status, "turn_terminal_status_unknown")
            return result
        if not items:
            return result

        item_id, answer_text, digest = items[0]
        if (
            not is_valid_final_answer_identifier(item_id)
            or not isinstance(answer_text, str)
            or not isinstance(digest, str)
        ):
            result["status"] = "incomplete"
            result["reason"] = "stored_record_invalid"
            return result
        try:
            encoded = answer_text.encode("utf-8", errors="strict")
            units = len(answer_text.encode("utf-16-le", errors="strict")) // 2
        except UnicodeEncodeError:
            result["status"] = "incomplete"
            result["reason"] = "stored_record_invalid"
            return result
        if (
            not answer_text.strip()
            or units > MAX_FINAL_ANSWER_UTF16_CODE_UNITS
            or hashlib.sha256(encoded).hexdigest() != digest
        ):
            result["status"] = "incomplete"
            result["reason"] = "stored_record_invalid"
            return result
        result.update(
            {
                "status": "retrieved",
                "source": FINAL_ANSWER_SOURCE,
                "item_id": item_id,
                "text": answer_text,
            }
        )
        result.pop("reason", None)
        return result
