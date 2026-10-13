from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from codex_bridge.event_delivery import EventDeliveryWorker
from codex_bridge.event_security import (
    CallbackDestination,
    CallbackRequestError,
    WebhookResponse,
    standard_webhook_signature,
    validate_callback_url,
)
from codex_bridge.event_store import EventStore, canonical_json


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class _FakeClient:
    def __init__(self, *outcomes: WebhookResponse | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.posts: list[tuple[bytes, dict[str, str]]] = []

    async def validate(self, url: str, _timeout_seconds: float):
        return CallbackDestination(validate_callback_url(url), "1.1.1.1")

    async def post(self, _destination, body, headers, _timeout_seconds):
        self.posts.append((body, dict(headers)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _pending_event(tmp_path) -> tuple[EventStore, datetime, str, bytes]:
    current = datetime(2026, 10, 13, tzinfo=UTC)
    secret = bytes(range(32))
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    store.upsert_subscription(
        principal="owner",
        event_name="codex.turn.finished",
        canonical_arguments=canonical_json({"thread_id": "thread-a"}),
        callback_url="https://receiver.example/callback",
        signing_secret="whsec_" + base64.b64encode(secret).decode("ascii"),
        expires_at=current + timedelta(hours=1),
        now=current,
    )
    store.record_terminal(
        target_id="local",
        thread_id="thread-a",
        turn_id="turn-a",
        status="completed",
        occurred_at=current + timedelta(seconds=1),
    )
    return store, current + timedelta(seconds=2), "sub_unused", secret


@pytest.mark.asyncio
async def test_successful_delivery_signs_stored_utf8_payload(tmp_path) -> None:
    store, current, _, secret = _pending_event(tmp_path)
    client = _FakeClient(WebhookResponse(202, {}, b""))
    worker = EventDeliveryWorker(store, client=client, clock=_Clock(current), unix_time=lambda: 7)

    assert await worker.process_next() is True

    body, headers = client.posts[0]
    assert headers["webhook-id"]
    assert headers["webhook-signature"] == standard_webhook_signature(
        secret, headers["webhook-id"], "7", body
    )
    assert headers["X-MCP-Subscription-Id"].startswith("sub_")
    assert store.count_outbox(delivery_state="delivered") == 1


@pytest.mark.asyncio
async def test_retry_keeps_event_id_and_body_but_resigns_with_new_timestamp(tmp_path) -> None:
    store, current, _, _ = _pending_event(tmp_path)
    client = _FakeClient(
        WebhookResponse(500, {}, b""),
        WebhookResponse(204, {}, b""),
    )
    clock = _Clock(current)
    timestamps = iter((100, 101))
    worker = EventDeliveryWorker(
        store,
        client=client,
        clock=clock,
        unix_time=lambda: next(timestamps),
    )

    await worker.process_next()
    assert store.count_outbox(delivery_state="pending") == 1
    assert await worker.process_next() is False
    clock.current += timedelta(seconds=5)
    await worker.process_next()

    first_body, first_headers = client.posts[0]
    second_body, second_headers = client.posts[1]
    assert first_body == second_body
    assert first_headers["webhook-id"] == second_headers["webhook-id"]
    assert first_headers["webhook-signature"] != second_headers["webhook-signature"]
    assert second_headers["webhook-timestamp"] == "101"
    assert store.count_outbox(delivery_state="delivered") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [410, 413, 400, 302])
async def test_permanent_http_statuses_are_not_retried(tmp_path, status: int) -> None:
    store, current, _, _ = _pending_event(tmp_path)
    client = _FakeClient(WebhookResponse(status, {}, b""))
    worker = EventDeliveryWorker(store, client=client, clock=_Clock(current))

    await worker.process_next()

    assert store.count_outbox(delivery_state="failed") == 1
    assert store.count_outbox(delivery_state="pending") == 0


@pytest.mark.asyncio
async def test_retry_after_is_bounded_and_used_for_rate_limits(tmp_path) -> None:
    store, current, _, _ = _pending_event(tmp_path)
    client = _FakeClient(WebhookResponse(429, {"Retry-After": "20"}, b""))
    worker = EventDeliveryWorker(store, client=client, clock=_Clock(current))

    await worker.process_next()

    assert store.next_attempt_delay(current, maximum_seconds=100) == pytest.approx(20)


@pytest.mark.asyncio
async def test_unreachable_callback_is_retried_but_retry_limit_is_respected(tmp_path) -> None:
    store, current, _, _ = _pending_event(tmp_path)
    client = _FakeClient(CallbackRequestError("callback_unreachable"))
    worker = EventDeliveryWorker(store, client=client, clock=_Clock(current), max_retries=0)

    await worker.process_next()

    assert store.count_outbox(delivery_state="failed") == 1
    assert store.count_outbox(delivery_state="pending") == 0


@pytest.mark.asyncio
async def test_worker_starts_and_stops_cleanly_when_idle(tmp_path) -> None:
    store = EventStore(tmp_path / "events.sqlite3")
    store.initialize()
    worker = EventDeliveryWorker(store)

    await worker.start()
    await worker.stop()

    assert worker._task is None
