from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_bridge.bridge import Bridge
from codex_bridge.final_answers import (
    MAX_FINAL_ANSWER_UTF16_CODE_UNITS,
    MAX_IDENTIFIER_UTF16_CODE_UNITS,
    FinalAnswerStore,
    default_final_answer_database_path,
)
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore
from tests.bridge_test_fakes import FakeAppServer

THREAD_ID = "thread-final-1"
TURN_ID = "turn-final-1"
FINAL_TEXT = "Confirmed final answer with citation metadata preserved."


def _bridge(tmp_path: Path, database_path: Path | None = None) -> tuple[Bridge, FinalAnswerStore]:
    store = FinalAnswerStore(database_path or tmp_path / "final-answers.sqlite3")
    bridge = Bridge(
        FakeAppServer(),
        StateStore(),
        AllowedPathPolicy((str(tmp_path),)),
        final_answer_store=store,
    )
    return bridge, store


def _item(
    bridge: Bridge,
    *,
    thread_id: str = THREAD_ID,
    turn_id: str = TURN_ID,
    item_id: str = "item-final-1",
    phase: str = "final_answer",
    text: object = FINAL_TEXT,
) -> None:
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": item_id,
                    "type": "agentMessage",
                    "phase": phase,
                    "text": text,
                },
            },
        }
    )


def _terminal(
    bridge: Bridge,
    status: str = "completed",
    *,
    thread_id: str = THREAD_ID,
    turn_id: str = TURN_ID,
    event_turn_id: str | None = None,
) -> None:
    bridge.handle_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id if event_turn_id is None else event_turn_id,
                "turn": {"id": turn_id, "status": status},
            },
        }
    )


def _text_digest(result: dict[str, object]) -> str:
    text = result.get("text")
    if not isinstance(text, str):
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_final_answer_is_pending_until_completed_terminal_is_persisted(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    pending = bridge.final_answer(THREAD_ID, TURN_ID)

    assert pending["status"] == "pending"
    assert pending["reason"] == "turn_terminal_not_observed"
    assert "text" not in pending

    _terminal(bridge)
    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert result["thread_id"] == THREAD_ID
    assert result["turn_id"] == TURN_ID
    assert result["source"] == "app_server.item/completed.agentMessage.phase=final_answer"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("phase", ["commentary", "analysis", "unknown", None])
def test_non_final_agent_messages_do_not_become_confirmed_answers(
    tmp_path: Path, phase: str | None
) -> None:
    bridge, _store = _bridge(tmp_path)

    if phase is None:
        _item(bridge, phase="")
    else:
        _item(bridge, phase=phase)
    bridge.handle_notification(
        {
            "method": "item/agentMessage/delta",
            "params": {"threadId": THREAD_ID, "turnId": TURN_ID, "delta": FINAL_TEXT},
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": THREAD_ID,
                "turnId": TURN_ID,
                "item": {"id": "reasoning-1", "type": "reasoning", "text": FINAL_TEXT},
            },
        }
    )
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_confirmed_final_answer"
    assert "text" not in result
    snapshot = bridge._state.snapshot_if_known(THREAD_ID, TURN_ID)
    assert snapshot is not None
    latest_message = snapshot["latest_agent_message"]
    assert (
        hashlib.sha256(latest_message.encode("utf-8")).hexdigest()
        == hashlib.sha256((FINAL_TEXT + FINAL_TEXT).encode("utf-8")).hexdigest()
    )


def test_duplicate_same_item_and_content_is_idempotent(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    _item(bridge)
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()


def test_item_id_content_collision_is_incomplete(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    _item(bridge, text="Different content for the same item ID.")
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == "item_id_content_collision"
    assert "text" not in result


def test_distinct_final_items_are_a_conflict(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge, item_id="item-final-1")
    _item(bridge, item_id="item-final-2", text="A second final item.")
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == "multiple_final_answers"
    assert "text" not in result


def test_request_thread_or_turn_mismatch_never_returns_other_text(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    _terminal(bridge)

    other_thread = bridge.final_answer("thread-final-2", TURN_ID)
    other_turn = bridge.final_answer(THREAD_ID, "turn-final-2")

    assert other_thread["status"] == "unavailable"
    assert other_thread["thread_id"] == "thread-final-2"
    assert other_turn["status"] == "unavailable"
    assert other_turn["turn_id"] == "turn-final-2"
    assert "text" not in other_thread
    assert "text" not in other_turn


@pytest.mark.parametrize(
    ("terminal_status", "reason"),
    [
        ("failed", "turn_failed"),
        ("interrupted", "turn_interrupted"),
        ("inProgress", "turn_terminal_status_unknown"),
    ],
)
def test_non_completed_terminal_status_never_returns_text(
    tmp_path: Path, terminal_status: str, reason: str
) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    _terminal(bridge, terminal_status)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == reason
    assert "text" not in result


def test_terminal_before_final_item_is_supported_without_guessing(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _terminal(bridge)
    before_item = bridge.final_answer(THREAD_ID, TURN_ID)
    _item(bridge)
    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert before_item["status"] == "unavailable"
    assert before_item["reason"] == "no_confirmed_final_answer"
    assert result["status"] == "retrieved"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()


def test_utf16_size_boundary_is_exact_and_oversize_is_not_truncated(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)
    exact_boundary = "🙂" * (MAX_FINAL_ANSWER_UTF16_CODE_UNITS // 2)

    _item(bridge, text=exact_boundary)
    _terminal(bridge)
    accepted = bridge.final_answer(THREAD_ID, TURN_ID)

    assert accepted["status"] == "retrieved"
    assert isinstance(accepted.get("text"), str)
    assert len(accepted["text"].encode("utf-16-le")) // 2 == MAX_FINAL_ANSWER_UTF16_CODE_UNITS

    oversized_bridge, _store = _bridge(tmp_path, tmp_path / "oversized.sqlite3")
    _item(oversized_bridge, text=exact_boundary + "🙂")
    _terminal(oversized_bridge)
    rejected = oversized_bridge.final_answer(THREAD_ID, TURN_ID)

    assert rejected["status"] == "incomplete"
    assert rejected["reason"] == "final_answer_too_large"
    assert "text" not in rejected


def test_restart_preserves_final_text_and_terminal_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "persistent.sqlite3"
    first_bridge, _first_store = _bridge(tmp_path, database_path)
    _item(first_bridge)
    _terminal(first_bridge)

    restarted_bridge, _restarted_store = _bridge(tmp_path, database_path)
    result = restarted_bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()


def test_invalid_event_turn_binding_is_ignored(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _terminal(bridge, event_turn_id="another-turn")
    _item(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "pending"
    assert result["reason"] == "turn_terminal_not_observed"
    assert "text" not in result


def test_invalid_final_text_is_incomplete_and_not_exposed(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge, text={"unexpected": "value"})
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == "invalid_final_answer_text"
    assert "text" not in result


@pytest.mark.parametrize("item_id", ["../outside", "x" * (MAX_IDENTIFIER_UTF16_CODE_UNITS + 1)])
def test_unrecognized_item_identifier_is_ignored(tmp_path: Path, item_id: str) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge, item_id=item_id)
    _terminal(bridge)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_confirmed_final_answer"
    assert "text" not in result


def test_conflicting_terminal_evidence_is_incomplete(tmp_path: Path) -> None:
    bridge, _store = _bridge(tmp_path)

    _item(bridge)
    _terminal(bridge, "completed")
    _terminal(bridge, "failed")

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == "terminal_status_conflict"
    assert "text" not in result


def test_unreadable_storage_returns_safe_unavailable_result(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("placeholder", encoding="utf-8")
    bridge, _store = _bridge(tmp_path, parent_file / "answers.sqlite3")

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "storage_unavailable"
    assert "text" not in result


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            {
                "thread_id": "another-thread",
                "turn_id": TURN_ID,
                "status": "retrieved",
                "text": "never return",
            },
            "stored_identity_mismatch",
        ),
        (
            {
                "thread_id": THREAD_ID,
                "turn_id": TURN_ID,
                "status": {"unexpected": "shape"},
                "text": "never return",
            },
            "stored_result_invalid",
        ),
    ],
)
def test_local_store_response_identity_and_status_are_validated(
    tmp_path: Path, monkeypatch, response: dict[str, object], reason: str
) -> None:
    bridge, store = _bridge(tmp_path)
    monkeypatch.setattr(store, "read", lambda _thread_id, _turn_id: response)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "incomplete"
    assert result["reason"] == reason
    assert result["thread_id"] == THREAD_ID
    assert result["turn_id"] == TURN_ID
    assert "text" not in result


def test_corrupt_storage_returns_safe_unavailable_result(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.sqlite3"
    database_path.write_bytes(b"not a sqlite database")
    bridge, _store = _bridge(tmp_path, database_path)

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "storage_unavailable"
    assert "text" not in result


def test_capture_failure_never_logs_completion_text(tmp_path: Path, monkeypatch) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("placeholder", encoding="utf-8")
    bridge, _store = _bridge(tmp_path, parent_file / "answers.sqlite3")
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "codex_bridge.bridge.log_event",
        lambda name, **values: logged.append((name, values)),
    )

    _item(bridge)

    def contains_text(value: object) -> bool:
        if isinstance(value, str):
            return value == FINAL_TEXT or FINAL_TEXT in value
        if isinstance(value, dict):
            return any(contains_text(key) or contains_text(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains_text(item) for item in value)
        return False

    assert not any(contains_text(entry) for entry in logged), "completion text appeared in logs"


@pytest.mark.asyncio
async def test_async_storage_failure_is_unavailable_and_never_logs_answer_text(
    tmp_path: Path, monkeypatch
) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("placeholder", encoding="utf-8")
    bridge, _store = _bridge(tmp_path, parent_file / "answers.sqlite3")
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "codex_bridge.bridge.log_event",
        lambda name, **values: logged.append((name, values)),
    )
    final_event = {
        "method": "item/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {
                "id": "item-final-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": FINAL_TEXT,
            },
        },
    }
    terminal_event = {
        "method": "turn/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "turn": {"id": TURN_ID, "status": "completed"},
        },
    }

    await bridge.handle_notification_async(final_event)
    await bridge.handle_notification_async(terminal_event)
    result = await bridge.final_answer_async(THREAD_ID, TURN_ID)
    await bridge.drain_final_answer_writes(close=True)

    def contains_answer(value: object) -> bool:
        if isinstance(value, str):
            return FINAL_TEXT in value
        if isinstance(value, dict):
            return any(contains_answer(key) or contains_answer(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains_answer(item) for item in value)
        return False

    assert result["status"] == "unavailable"
    assert result["reason"] == "storage_unavailable"
    assert "text" not in result
    assert not contains_answer(logged), "completion text appeared in logs"


def test_retention_turn_limit_prunes_oldest_turn(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    store = FinalAnswerStore(
        tmp_path / "turn-cap.sqlite3", clock=lambda: now[0], max_retained_turns=1
    )
    bridge = Bridge(
        FakeAppServer(),
        StateStore(),
        AllowedPathPolicy((str(tmp_path),)),
        final_answer_store=store,
    )
    _item(bridge)
    _terminal(bridge)
    now[0] += 1
    _item(bridge, turn_id="turn-final-2")

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_confirmed_final_answer"
    assert "text" not in result


def test_retention_payload_cap_prunes_oldest_answer(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    store = FinalAnswerStore(
        tmp_path / "payload-cap.sqlite3",
        clock=lambda: now[0],
        max_retained_text_bytes=8,
    )
    bridge = Bridge(
        FakeAppServer(),
        StateStore(),
        AllowedPathPolicy((str(tmp_path),)),
        final_answer_store=store,
    )
    _item(bridge, text="12345678")
    _terminal(bridge)
    now[0] += 1
    _item(bridge, turn_id="turn-final-2", text="abcdefgh")
    _terminal(bridge, turn_id="turn-final-2")

    first = bridge.final_answer(THREAD_ID, TURN_ID)
    second = bridge.final_answer(THREAD_ID, "turn-final-2")

    assert first["status"] == "unavailable"
    assert first["reason"] == "no_confirmed_final_answer"
    assert second["status"] == "retrieved"
    assert _text_digest(second) == hashlib.sha256(b"abcdefgh").hexdigest()


def test_concurrent_duplicate_capture_remains_idempotent(tmp_path: Path) -> None:
    store = FinalAnswerStore(tmp_path / "concurrent.sqlite3")

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                store.record_final_answer,
                THREAD_ID,
                TURN_ID,
                "item-final-1",
                FINAL_TEXT,
            )
            for _ in range(12)
        ]
        for future in futures:
            future.result(timeout=10)
    store.record_terminal(THREAD_ID, TURN_ID, "completed")

    result = store.read(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()


def test_default_storage_uses_user_specific_bridge_state_directory(tmp_path: Path) -> None:
    actual = default_final_answer_database_path(
        {"LOCALAPPDATA": str(tmp_path / "local")}, platform="win32", home=tmp_path
    )

    assert actual == tmp_path / "local" / "CodexBridge" / "final-answers.sqlite3"


def test_retention_expires_old_turns(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    store = FinalAnswerStore(tmp_path / "retention.sqlite3", clock=lambda: now[0])
    bridge = Bridge(
        FakeAppServer(),
        StateStore(),
        AllowedPathPolicy((str(tmp_path),)),
        final_answer_store=store,
    )
    _item(bridge)
    _terminal(bridge)

    now[0] += 31 * 24 * 60 * 60
    _item(bridge, turn_id="turn-final-2")

    result = bridge.final_answer(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_confirmed_final_answer"
    assert "text" not in result


def test_read_of_missing_store_does_not_create_database(tmp_path: Path) -> None:
    database_path = tmp_path / "not-yet-created.sqlite3"
    store = FinalAnswerStore(database_path)

    result = store.read(THREAD_ID, TURN_ID)

    assert result["status"] == "unavailable"
    assert result["reason"] == "no_confirmed_final_answer"
    assert not database_path.exists()


def test_read_uses_no_immediate_write_transaction(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "read-only.sqlite3"
    store = FinalAnswerStore(database_path)
    store.record_final_answer(THREAD_ID, TURN_ID, "item-final-1", FINAL_TEXT)
    store.record_terminal(THREAD_ID, TURN_ID, "completed")
    traced_statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(traced_statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    result = store.read(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert not any(
        statement.upper().startswith("BEGIN IMMEDIATE") for statement in traced_statements
    )


@pytest.mark.asyncio
async def test_async_notification_persistence_does_not_block_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    bridge, store = _bridge(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_record = store.record_final_answer

    def blocked_record(thread_id: str, turn_id: str, item_id: str, text: object) -> None:
        entered.set()
        release.wait(timeout=2)
        original_record(thread_id, turn_id, item_id, text)

    monkeypatch.setattr(store, "record_final_answer", blocked_record)
    event = {
        "method": "item/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {
                "id": "item-final-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": FINAL_TEXT,
            },
        },
    }
    handler = asyncio.create_task(bridge.handle_notification_async(event))
    release_timer = threading.Timer(0.5, release.set)
    release_timer.start()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.2)
        loop = asyncio.get_running_loop()
        pulse = loop.create_future()
        loop.call_later(0.02, pulse.set_result, True)
        assert await asyncio.wait_for(pulse, timeout=0.15)
        assert not release.is_set()
    finally:
        release.set()
        release_timer.cancel()
        await handler
        await bridge.drain_final_answer_writes(close=True)


@pytest.mark.asyncio
async def test_async_final_item_precedes_terminal_and_confirmed_read(
    tmp_path: Path, monkeypatch
) -> None:
    bridge, store = _bridge(tmp_path)
    persisted_order: list[str] = []
    original_item_record = store.record_final_answer
    original_terminal_record = store.record_terminal

    def record_item(thread_id: str, turn_id: str, item_id: str, text: object) -> None:
        persisted_order.append("item")
        original_item_record(thread_id, turn_id, item_id, text)

    def record_terminal(thread_id: str, turn_id: str, status: str) -> None:
        persisted_order.append("terminal")
        original_terminal_record(thread_id, turn_id, status)

    monkeypatch.setattr(store, "record_final_answer", record_item)
    monkeypatch.setattr(store, "record_terminal", record_terminal)
    final_event = {
        "method": "item/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {
                "id": "item-final-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": FINAL_TEXT,
            },
        },
    }
    terminal_event = {
        "method": "turn/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "turn": {"id": TURN_ID, "status": "completed"},
        },
    }

    await bridge.handle_notification_async(final_event)
    await bridge.handle_notification_async(final_event)
    await bridge.handle_notification_async(terminal_event)
    result = await bridge.final_answer_async(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert result["thread_id"] == THREAD_ID
    assert result["turn_id"] == TURN_ID
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()
    assert persisted_order == ["item", "item", "terminal"]
    await bridge.drain_final_answer_writes(close=True)


@pytest.mark.asyncio
async def test_shutdown_drain_completes_queued_writes_after_waiter_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    bridge, store = _bridge(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_record = store.record_final_answer

    def blocked_record(thread_id: str, turn_id: str, item_id: str, text: object) -> None:
        entered.set()
        release.wait(timeout=2)
        original_record(thread_id, turn_id, item_id, text)

    monkeypatch.setattr(store, "record_final_answer", blocked_record)
    final_event = {
        "method": "item/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "item": {
                "id": "item-final-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": FINAL_TEXT,
            },
        },
    }
    terminal_event = {
        "method": "turn/completed",
        "params": {
            "threadId": THREAD_ID,
            "turnId": TURN_ID,
            "turn": {"id": TURN_ID, "status": "completed"},
        },
    }
    await bridge.handle_notification_async(final_event)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.2)
        await bridge.handle_notification_async(terminal_event)
        drain = asyncio.create_task(bridge.drain_final_answer_writes(close=True))
        await asyncio.sleep(0.02)
        assert not drain.done()
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
    finally:
        release.set()
    await bridge.drain_final_answer_writes(close=True)

    result = await bridge.final_answer_async(THREAD_ID, TURN_ID)

    assert result["status"] == "retrieved"
    assert _text_digest(result) == hashlib.sha256(FINAL_TEXT.encode("utf-8")).hexdigest()
    assert bridge._final_answer_writer_closed


@pytest.mark.asyncio
async def test_final_answer_queue_is_bounded_and_applies_backpressure(
    tmp_path: Path, monkeypatch
) -> None:
    bridge, store = _bridge(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_record = store.record_final_answer

    def blocked_first_record(thread_id: str, turn_id: str, item_id: str, text: object) -> None:
        if turn_id == "turn-queue-1":
            entered.set()
            release.wait(timeout=3)
        original_record(thread_id, turn_id, item_id, text)

    monkeypatch.setattr(store, "record_final_answer", blocked_first_record)

    def event_for(index: int) -> dict[str, object]:
        turn_id = f"turn-queue-{index}"
        return {
            "method": "item/completed",
            "params": {
                "threadId": THREAD_ID,
                "turnId": turn_id,
                "item": {
                    "id": f"item-queue-{index}",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": FINAL_TEXT,
                },
            },
        }

    await bridge.handle_notification_async(event_for(1))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.2)
    for index in range(2, 2 + bridge._final_answer_write_queue.maxsize):
        await bridge.handle_notification_async(event_for(index))

    queue = bridge._final_answer_write_queue
    assert queue is not None
    assert queue.maxsize > 0
    assert queue.qsize() == queue.maxsize
    overflow_handler = asyncio.create_task(
        bridge.handle_notification_async(event_for(queue.maxsize + 2))
    )
    await asyncio.sleep(0.02)
    backpressured = not overflow_handler.done()
    assert queue.qsize() == queue.maxsize
    overflow_handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await overflow_handler

    release.set()
    await bridge.drain_final_answer_writes(close=True)
    result = await bridge.final_answer_async(THREAD_ID, f"turn-queue-{queue.maxsize + 2}")

    assert backpressured
    assert result["status"] == "pending"
    assert "text" not in result
