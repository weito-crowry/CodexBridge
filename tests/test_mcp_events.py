from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mcp import MCPError, types
from pydantic import ValidationError

from codex_bridge.config import McpEventsConfig
from codex_bridge.event_security import WebhookClient, WebhookResponse, validate_callback_url
from codex_bridge.mcp_events import (
    EVENT_DESCRIPTION,
    EVENT_INPUT_SCHEMA,
    EVENT_NAME,
    EVENT_PAYLOAD_SCHEMA,
    EventsListParams,
    EventsSubscribeParams,
    EventsUnsubscribeParams,
    McpEventsAdapter,
)


class _Bridge:
    def __init__(self, *, turns: list[dict[str, str]] | None = None) -> None:
        self.turns = turns or []
        self.calls: list[tuple[str, bool]] = []

    async def threads(self, thread_id=None, *, include_history=False):
        self.calls.append((thread_id, include_history))
        return {"thread": {"id": thread_id, "turns": self.turns}}


class _CallbackClient(WebhookClient):
    def __init__(self) -> None:
        self.posts: list[tuple[bytes, dict[str, str]]] = []

    async def validate(self, url: str, _timeout_seconds: float = 10.0):
        from codex_bridge.event_security import CallbackDestination

        return CallbackDestination(validate_callback_url(url), "1.1.1.1")

    async def post(self, _destination, body, headers, _timeout_seconds):
        self.posts.append((body, dict(headers)))
        challenge = json.loads(body)["challenge"]
        return WebhookResponse(200, {}, json.dumps({"challenge": challenge}).encode())


def _adapter(tmp_path, *, principal="owner-a", bridge=None, clock=None):
    current = clock or datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    config = McpEventsConfig(enabled=True, db_path=tmp_path / "events.sqlite3")
    bridge_value = bridge or _Bridge(turns=[{"id": "turn-a"}])
    client = _CallbackClient()
    adapter = McpEventsAdapter(
        config,
        lambda: bridge_value,
        "local-machine",
        principal_resolver=lambda _context: principal,
        webhook_client=client,
        clock=lambda: current,
    )
    return adapter, bridge_value, client, current


def _context(version="2026-07-28"):
    return SimpleNamespace(protocol_version=version)


_UNSET = object()


def _subscription(*, ttl=_UNSET, cursor=None, mode="webhook", secret=None):
    secret_text = secret or "whsec_" + base64.b64encode(bytes(range(32))).decode("ascii")
    values = {
        "name": EVENT_NAME,
        "arguments": {"thread_id": "thread-a", "turn_id": "turn-a"},
        "delivery": {
            "mode": mode,
            "url": "https://receiver.example/callback",
            "secret": secret_text,
        },
    }
    if ttl is not _UNSET:
        values["ttlMs"] = ttl
    if cursor is not None:
        values["cursor"] = cursor
    return EventsSubscribeParams.model_validate(values)


@pytest.mark.asyncio
async def test_events_list_exposes_one_webhook_event_and_both_schemas(tmp_path) -> None:
    adapter, _, _, _ = _adapter(tmp_path)

    payload = await adapter.list_events(_context(), EventsListParams())
    (event,) = payload["events"]
    assert event["name"] == EVENT_NAME
    assert event["description"] == EVENT_DESCRIPTION
    assert event["delivery"] == ["webhook"]
    assert event["inputSchema"] == EVENT_INPUT_SCHEMA
    assert event["payloadSchema"] == EVENT_PAYLOAD_SCHEMA


@pytest.mark.asyncio
async def test_subscribe_verifies_callback_persists_and_clamps_ttl(tmp_path) -> None:
    adapter, bridge, client, now = _adapter(tmp_path)
    await adapter.start()

    result = await adapter.subscribe(_context(), _subscription(ttl=30_000))

    assert result["id"].startswith("sub_")
    assert result["refreshBefore"] == "2026-10-13T12:01:00.000000Z"
    assert result["cursor"] is None
    assert result["truncated"] is False
    assert bridge.calls == [("thread-a", True)]
    assert len(client.posts) == 1
    secret = adapter.store.subscription_secret(result["id"], now=now)
    assert secret is not None
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_ttl", [_UNSET, None])
async def test_subscribe_uses_default_ttl_when_omitted_or_null(tmp_path, requested_ttl) -> None:
    adapter, _, _, now = _adapter(tmp_path)
    await adapter.start()

    result = await adapter.subscribe(_context(), _subscription(ttl=requested_ttl))

    assert result["refreshBefore"] == "2026-10-13T13:00:00.000000Z"
    await adapter.stop()


@pytest.mark.asyncio
async def test_subscribe_caps_requested_ttl_at_configured_maximum(tmp_path) -> None:
    adapter, _, _, _ = _adapter(tmp_path)
    await adapter.start()

    result = await adapter.subscribe(_context(), _subscription(ttl=90_000_000))

    assert result["refreshBefore"] == "2026-10-14T12:00:00.000000Z"
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments", "mode", "url", "secret", "error_code"),
    [
        (
            "codex.turn.started",
            {"thread_id": "thread-a"},
            "webhook",
            "https://receiver.example/callback",
            "valid",
            types.INVALID_PARAMS,
        ),
        (
            EVENT_NAME,
            {"thread_id": ""},
            "webhook",
            "https://receiver.example/callback",
            "valid",
            types.INVALID_PARAMS,
        ),
        (
            EVENT_NAME,
            {"thread_id": "thread-a", "other": "value"},
            "webhook",
            "https://receiver.example/callback",
            "valid",
            types.INVALID_PARAMS,
        ),
        (
            EVENT_NAME,
            {"thread_id": "thread-a"},
            "stream",
            "https://receiver.example/callback",
            "valid",
            types.INVALID_PARAMS,
        ),
        (
            EVENT_NAME,
            {"thread_id": "thread-a"},
            "webhook",
            "http://receiver.example/callback",
            "valid",
            -32015,
        ),
        (
            EVENT_NAME,
            {"thread_id": "thread-a"},
            "webhook",
            "https://receiver.example/callback",
            "invalid",
            types.INVALID_PARAMS,
        ),
    ],
)
async def test_subscribe_rejects_unsupported_or_malformed_requests(
    tmp_path, name, arguments, mode, url, secret, error_code
) -> None:
    adapter, _, client, _ = _adapter(tmp_path)
    await adapter.start()
    secret_value = secret
    if secret == "valid":
        secret_value = "whsec_" + base64.b64encode(bytes(range(32))).decode("ascii")
    params = EventsSubscribeParams.model_validate(
        {
            "name": name,
            "arguments": arguments,
            "delivery": {"mode": mode, "url": url, "secret": secret_value},
        }
    )

    with pytest.raises(MCPError) as exc_info:
        await adapter.subscribe(_context(), params)

    assert exc_info.value.code == error_code
    assert client.posts == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_anonymous_subscribe_is_forbidden_before_callback_io(tmp_path) -> None:
    adapter, _, client, _ = _adapter(tmp_path, principal=None)
    await adapter.start()
    with pytest.raises(MCPError) as exc_info:
        await adapter.subscribe(_context(), _subscription())

    assert exc_info.value.code == -32024
    assert client.posts == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_unavailable_thread_turn_and_remote_target_are_forbidden(tmp_path) -> None:
    adapter, _, client, _ = _adapter(tmp_path, bridge=_Bridge(turns=[]))
    await adapter.start()
    with pytest.raises(MCPError) as turn_error:
        await adapter.subscribe(_context(), _subscription())
    assert turn_error.value.code == -32024

    remote = _subscription()
    remote.arguments = {"thread_id": "remote-target::native-thread"}
    with pytest.raises(MCPError) as remote_error:
        await adapter.subscribe(_context(), remote)
    assert remote_error.value.code == -32024
    assert client.posts == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_other_principal_cannot_unsubscribe_subscription(tmp_path) -> None:
    adapter, _, _, now = _adapter(tmp_path)
    await adapter.start()
    created = await adapter.subscribe(_context(), _subscription())
    assert adapter.store.subscription_is_active(created["id"], now=now)

    params = EventsUnsubscribeParams.model_validate(
        {
            "name": EVENT_NAME,
            "arguments": {"thread_id": "thread-a", "turn_id": "turn-a"},
            "delivery": {
                "url": "https://receiver.example/callback",
                "mode": "webhook",
            },
        }
    )
    other_adapter, _, _, _ = _adapter(tmp_path, principal="owner-b")
    await other_adapter.start()
    other_adapter.store = adapter.store
    await other_adapter.unsubscribe(_context(), params)

    assert adapter.store.subscription_is_active(created["id"], now=now)
    await adapter.unsubscribe(_context(), params)
    await adapter.unsubscribe(_context(), params)
    assert not adapter.store.subscription_is_active(created["id"], now=now)
    await other_adapter.stop()
    await adapter.stop()


@pytest.mark.asyncio
async def test_cursor_and_protocol_version_errors_are_explicit(tmp_path) -> None:
    adapter, _, _, _ = _adapter(tmp_path)
    await adapter.start()
    with pytest.raises(MCPError) as cursor_error:
        await adapter.subscribe(_context(), _subscription(cursor="cursor-1"))
    assert cursor_error.value.code == types.INVALID_PARAMS

    with pytest.raises(MCPError) as version_error:
        await adapter.subscribe(_context("2025-11-25"), _subscription())
    assert version_error.value.code == types.INVALID_REQUEST
    await adapter.stop()


def test_event_params_reject_unknown_delivery_fields() -> None:
    with pytest.raises(ValidationError):
        EventsSubscribeParams.model_validate(
            {
                "name": EVENT_NAME,
                "arguments": {"thread_id": "thread-a"},
                "delivery": {
                    "mode": "webhook",
                    "url": "https://receiver.example/callback",
                    "secret": "whsec_abc",
                    "extra": "must be rejected",
                },
            }
        )
