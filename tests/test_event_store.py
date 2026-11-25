from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from codex_bridge.event_store import EventStore, canonical_json, utc_iso


def _subscribe(
    store: EventStore,
    *,
    principal: str = "owner-a",
    arguments: dict[str, str] | None = None,
    secret: str = "whsec_secret-a",
    now: datetime | None = None,
    expires_in: int = 3600,
) -> str:
    current = now or datetime(2026, 10, 13, tzinfo=UTC)
    return store.upsert_subscription(
        principal=principal,
        event_name="codex.turn.finished",
        canonical_arguments=canonical_json(arguments or {"thread_id": "thread-a"}),
        callback_url="https://receiver.example/callback",
        signing_secret=secret,
        expires_at=current + timedelta(seconds=expires_in),
        now=current,
    )


def test_record_terminal_is_durable_and_idempotent(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    _subscribe(store)

    assert (
        store.record_terminal(
            target_id="local-workstation",
            thread_id="thread-a",
            turn_id="turn-a",
            status="completed",
            occurred_at=datetime(2026, 10, 13, 0, 1, tzinfo=UTC),
        )
        == 1
    )
    assert (
        store.record_terminal(
            target_id="local-workstation",
            thread_id="thread-a",
            turn_id="turn-a",
            status="completed",
            occurred_at=datetime(2026, 10, 13, 0, 1, tzinfo=UTC),
        )
        == 0
    )
    assert store.count_outbox() == 1
    delivery = store.claim_next_due(datetime(2026, 10, 13, 0, 2, tzinfo=UTC))
    assert delivery is not None
    payload = json.loads(delivery.payload_json)
    assert payload["data"] == {
        "target_id": "local-workstation",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "status": "completed",
    }
    assert payload["eventId"] == delivery.event_id


def test_subscription_identity_is_canonical_and_principal_scoped(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    current = datetime(2026, 10, 13, tzinfo=UTC)
    first = _subscribe(
        store,
        arguments={"thread_id": "thread-a", "turn_id": "turn-a"},
        now=current,
    )
    same = _subscribe(
        store,
        arguments={"turn_id": "turn-a", "thread_id": "thread-a"},
        now=current + timedelta(minutes=1),
        expires_in=7200,
    )
    other_principal = _subscribe(
        store,
        principal="owner-b",
        arguments={"thread_id": "thread-a", "turn_id": "turn-a"},
        now=current,
    )

    assert first == same
    assert first != other_principal


def test_secret_rotation_retains_old_secret_for_five_minutes(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    current = datetime(2026, 10, 13, tzinfo=UTC)
    identifier = _subscribe(store, secret="whsec_old", now=current)

    same = _subscribe(
        store,
        secret="whsec_new",
        now=current + timedelta(minutes=1),
        expires_in=7200,
    )

    assert same == identifier
    assert store.subscription_secret(identifier, now=current + timedelta(minutes=2)) == (
        "whsec_new",
        "whsec_old",
        utc_iso(current + timedelta(minutes=6)),
    )


def test_cleanup_removes_expired_rotation_secret(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    current = datetime(2026, 10, 13, tzinfo=UTC)
    identifier = _subscribe(store, secret="whsec_old", now=current)
    _subscribe(store, secret="whsec_new", now=current + timedelta(minutes=1))

    store.cleanup(current + timedelta(minutes=7))

    assert store.subscription_secret(identifier, now=current + timedelta(minutes=7)) == (
        "whsec_new",
        None,
        None,
    )


def test_unsubscribe_is_idempotent_and_cancels_unsent_events(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    current = datetime(2026, 10, 13, tzinfo=UTC)
    identifier = _subscribe(store, now=current)
    store.record_terminal(
        target_id="local",
        thread_id="thread-a",
        turn_id="turn-a",
        status="interrupted",
        occurred_at=current + timedelta(minutes=1),
    )

    values = {
        "principal": "owner-a",
        "event_name": "codex.turn.finished",
        "canonical_arguments": canonical_json({"thread_id": "thread-a"}),
        "callback_url": "https://receiver.example/callback",
        "now": current + timedelta(minutes=2),
    }
    store.unsubscribe(**values)
    store.unsubscribe(**values)

    assert store.count_outbox(delivery_state="cancelled") == 1
    assert store.subscription_secret(identifier) is None
    assert store.claim_next_due(current + timedelta(minutes=2)) is None


def test_subscription_created_after_terminal_does_not_receive_backfill(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    terminal = datetime(2026, 10, 13, tzinfo=UTC)
    store.record_terminal(
        target_id="local",
        thread_id="thread-a",
        turn_id="turn-a",
        status="failed",
        occurred_at=terminal,
    )
    _subscribe(store, now=terminal + timedelta(seconds=1))

    assert store.count_outbox() == 0


def test_resubscribing_after_expiration_does_not_backfill_expired_interval(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    start = datetime(2026, 10, 13, tzinfo=UTC)
    _subscribe(store, now=start, expires_in=60)
    renewed = start + timedelta(minutes=2)
    _subscribe(store, now=renewed, expires_in=3600)

    assert (
        store.record_terminal(
            target_id="local",
            thread_id="thread-a",
            turn_id="expired-turn",
            status="completed",
            occurred_at=start + timedelta(seconds=90),
        )
        == 0
    )
    assert (
        store.record_terminal(
            target_id="local",
            thread_id="thread-a",
            turn_id="new-turn",
            status="completed",
            occurred_at=renewed + timedelta(seconds=1),
        )
        == 1
    )


def test_sqlite_restart_recovers_inflight_outbox(tmp_path) -> None:
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    store.initialize()
    current = datetime(2026, 10, 13, tzinfo=UTC)
    _subscribe(store, now=current)
    store.record_terminal(
        target_id="local",
        thread_id="thread-a",
        turn_id="turn-a",
        status="failed",
        occurred_at=current + timedelta(seconds=1),
    )
    assert store.claim_next_due(current + timedelta(seconds=2)) is not None
    store.close()

    restarted = EventStore(path)
    restarted.initialize()
    restarted.reset_inflight()
    recovered = restarted.claim_next_due(current + timedelta(seconds=3))

    assert recovered is not None
    assert recovered.attempt_count == 2
