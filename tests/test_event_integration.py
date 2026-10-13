from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codex_bridge.event_store import EventStore, canonical_json
from tests.bridge_test_fakes import make_bridge

pytest_plugins = ("tests.bridge_test_support",)


def _subscribe(store: EventStore, thread_id: str, *, turn_id: str | None = None) -> None:
    now = datetime.now(UTC)
    arguments = {"thread_id": thread_id}
    if turn_id is not None:
        arguments["turn_id"] = turn_id
    store.upsert_subscription(
        principal="test-owner",
        event_name="codex.turn.finished",
        canonical_arguments=canonical_json(arguments),
        callback_url="https://receiver.example/callback",
        signing_secret="whsec_test",
        expires_at=now + timedelta(hours=1),
        now=now - timedelta(seconds=1),
    )


def _connect_publisher(bridge, store: EventStore) -> None:
    bridge.set_event_publisher(
        lambda target_id, thread_id, turn_id, status: store.record_terminal(
            target_id=target_id,
            thread_id=thread_id,
            turn_id=turn_id,
            status=status,
        ),
        target_id="local-workstation",
    )


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_bridge_terminal_notifications_create_one_matching_outbox_event(
    tmp_path, allowed_dir, status: str
) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    _subscribe(store, "native-thread")
    bridge, _, state = make_bridge(allowed_dir)
    _connect_publisher(bridge, store)
    notification = {
        "method": "turn/completed",
        "params": {
            "threadId": "native-thread",
            "turn": {"id": "native-turn", "status": status},
        },
    }

    bridge.handle_notification(notification)
    bridge.handle_notification(notification)

    assert state.snapshot("native-thread", "native-turn")["state"] == status
    assert store.count_outbox() == 1
    delivery = store.claim_next_due()
    assert delivery is not None
    assert delivery.payload_json.find(f'"status":"{status}"') >= 0
    assert '"target_id":"local-workstation"' in delivery.payload_json


def test_nonterminal_and_retryable_error_notifications_do_not_publish(
    tmp_path, allowed_dir
) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    _subscribe(store, "native-thread")
    bridge, _, state = make_bridge(allowed_dir)
    _connect_publisher(bridge, store)
    state.ensure_turn("native-thread", "native-turn")

    bridge.handle_notification(
        {
            "method": "turn/started",
            "params": {
                "threadId": "native-thread",
                "turn": {"id": "native-turn", "status": "inProgress"},
            },
        }
    )
    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "native-thread",
                "turnId": "native-turn",
                "willRetry": True,
                "message": "transient",
            },
        }
    )

    assert state.snapshot("native-thread", "native-turn")["state"] == "in_progress"
    assert store.count_outbox() == 0


def test_nonretryable_error_and_app_server_failure_publish_failed(tmp_path, allowed_dir) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    _subscribe(store, "native-thread")
    bridge, _, state = make_bridge(allowed_dir)
    _connect_publisher(bridge, store)
    state.ensure_turn("native-thread", "native-turn")

    bridge.handle_notification(
        {
            "method": "error",
            "params": {
                "threadId": "native-thread",
                "turnId": "native-turn",
                "willRetry": False,
                "message": "terminal error",
            },
        }
    )
    state.ensure_turn("native-thread", "active-turn")
    bridge.handle_app_server_failure("app server exited")

    assert store.count_outbox() == 2
    assert state.snapshot("native-thread", "native-turn")["state"] == "failed"
    assert state.snapshot("native-thread", "active-turn")["state"] == "failed"


@pytest.mark.asyncio
async def test_terminal_turn_start_response_is_published(tmp_path, allowed_dir) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    _subscribe(store, "native-thread", turn_id="immediate-turn")
    bridge, app, _ = make_bridge(allowed_dir)
    _connect_publisher(bridge, store)
    original_request = app.request

    async def request(method, params):
        if method == "turn/start":
            return {"turn": {"id": "immediate-turn", "status": "completed"}}
        return await original_request(method, params)

    app.request = request

    result = await bridge.start(str(allowed_dir), "safe test prompt")

    assert result["turn_id"] == "immediate-turn"
    assert store.count_outbox() == 1
