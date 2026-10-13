from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol, cast

from mcp import MCPError, types
from mcp.server.context import (
    CallNext,
    HandlerResult,
    ServerMiddleware,
    ServerRequestContext,
)
from mcp.server.lowlevel import Server as LowLevelServer
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .config import McpEventsConfig
from .event_delivery import EventDeliveryWorker
from .event_security import (
    CallbackRequestError,
    CallbackVerificationCache,
    WebhookClient,
    decode_signing_secret,
    principal_from_request_context,
    validate_callback_url,
    verify_callback,
)
from .event_store import (
    EventStore,
    canonical_json,
    subscription_identifier,
    utc_iso,
    utc_now,
)
from .logging_utils import log_event

EVENT_NAME = "codex.turn.finished"
EVENT_DESCRIPTION = (
    "Emitted when a Codex turn completes, fails, or is interrupted on the local execution target."
)
PROTOCOL_VERSION = "2026-07-28"
FORBIDDEN = -32024
CALLBACK_ENDPOINT_ERROR = -32015

EVENT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thread_id": {
            "type": "string",
            "minLength": 1,
            "description": "Native local Codex thread ID to monitor.",
        },
        "turn_id": {
            "type": "string",
            "minLength": 1,
            "description": "Optional native Codex turn ID.",
        },
    },
    "required": ["thread_id"],
    "additionalProperties": False,
}
EVENT_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_id": {"type": "string"},
        "thread_id": {"type": "string"},
        "turn_id": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "failed", "interrupted"]},
    },
    "required": ["target_id", "thread_id", "turn_id", "status"],
    "additionalProperties": False,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventsListParams(types.RequestParams):
    cursor: StrictStr | None = None


class SubscribeDelivery(_StrictModel):
    mode: StrictStr
    url: StrictStr
    secret: StrictStr


class UnsubscribeDelivery(_StrictModel):
    mode: Literal["webhook"] | None = None
    url: StrictStr


class EventsSubscribeParams(types.RequestParams):
    name: StrictStr
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: SubscribeDelivery
    cursor: StrictStr | None = None
    ttl_ms: StrictInt | None = Field(default=None, alias="ttlMs")


class EventsUnsubscribeParams(types.RequestParams):
    name: StrictStr
    arguments: dict[str, Any] = Field(default_factory=dict)
    delivery: UnsubscribeDelivery


class EventsServerCapabilities(types.ServerCapabilities):
    events: dict[str, Any] = Field(default_factory=dict)


class EventsDiscoverResult(types.DiscoverResult):
    capabilities: EventsServerCapabilities


class EventBridgePort(Protocol):
    async def threads(
        self, thread_id: str | None = None, *, include_history: bool = False
    ) -> dict[str, Any]: ...

    async def _event_thread_turn_exists(self, thread_id: str, turn_id: str) -> bool: ...


PrincipalResolver = Callable[[object], str | None]
BridgeGetter = Callable[[], EventBridgePort | None]


class McpEventsAdapter:
    def __init__(
        self,
        config: McpEventsConfig,
        bridge_getter: BridgeGetter,
        target_id: str,
        *,
        principal_resolver: PrincipalResolver = principal_from_request_context,
        webhook_client: WebhookClient | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self._bridge_getter = bridge_getter
        self.target_id = target_id
        self._principal_resolver = principal_resolver
        self._clock = clock
        self._webhook_client = webhook_client or WebhookClient()
        database_path = config.db_path or _fallback_events_path()
        self.store = EventStore(database_path)
        self.worker = EventDeliveryWorker(
            self.store,
            timeout_seconds=config.webhook_timeout_seconds,
            max_retries=config.max_retries,
            client=self._webhook_client,
            clock=clock,
        )
        self._verification_cache = CallbackVerificationCache()
        self._ready = False

    def register(self, server: LowLevelServer[Any]) -> None:
        base_discovery = server.get_request_handler("server/discover")
        if base_discovery is None or base_discovery.handler is None:
            raise RuntimeError("MCP SDK server/discover handler is unavailable")
        original_discover = base_discovery.handler

        async def discover(
            context: Any, params: types.RequestParams | None
        ) -> EventsDiscoverResult:
            result = await original_discover(context, params)
            if isinstance(result, BaseModel):
                values = result.model_dump(mode="python", by_alias=False, exclude_none=True)
            elif isinstance(result, Mapping):
                values = types.DiscoverResult.model_validate(result).model_dump(
                    mode="python", by_alias=False, exclude_none=True
                )
            else:
                raise MCPError(code=types.INTERNAL_ERROR, message="Discovery is unavailable")
            capabilities = values.get("capabilities")
            if not isinstance(capabilities, dict):
                capabilities = {}
            capabilities["events"] = {}
            values["capabilities"] = capabilities
            return EventsDiscoverResult.model_validate(values)

        server.add_request_handler("server/discover", types.RequestParams, discover)
        server.add_request_handler("events/list", EventsListParams, self.list_events)
        server.add_request_handler("events/subscribe", EventsSubscribeParams, self.subscribe)
        server.add_request_handler("events/unsubscribe", EventsUnsubscribeParams, self.unsubscribe)

        async def preserve_events_discovery(
            context: ServerRequestContext[Any, Any], call_next: CallNext
        ) -> HandlerResult:
            result = await call_next(context)
            if context.method != "server/discover" or not isinstance(result, Mapping):
                return result
            values = dict(result)
            raw_capabilities = values.get("capabilities")
            capabilities = dict(raw_capabilities) if isinstance(raw_capabilities, Mapping) else {}
            capabilities["events"] = {}
            values["capabilities"] = capabilities
            return values

        server.middleware.append(cast(ServerMiddleware[Any], preserve_events_discovery))

    async def start(self) -> None:
        if not self.config.enabled:
            return
        self.store.initialize()
        await self.worker.start()
        self._ready = True

    async def stop(self) -> None:
        try:
            await self.worker.stop()
        finally:
            self._ready = False
            self.store.close()

    def publish_terminal(self, target_id: str, thread_id: str, turn_id: str, status: str) -> None:
        if not self._ready:
            return
        try:
            inserted = self.store.record_terminal(
                target_id=target_id,
                thread_id=thread_id,
                turn_id=turn_id,
                status=status,
                occurred_at=self._clock(),
            )
            if inserted:
                self.worker.notify()
        except Exception as exc:
            log_event("mcp_events.publish_failed", error_type=exc.__class__.__name__)

    async def list_events(self, context: Any, params: EventsListParams) -> dict[str, object]:
        self._require_protocol(context)
        if params.cursor is not None:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="Events catalog pagination is not supported",
            )
        return {
            "events": [
                {
                    "name": EVENT_NAME,
                    "description": EVENT_DESCRIPTION,
                    "delivery": ["webhook"],
                    "inputSchema": EVENT_INPUT_SCHEMA,
                    "payloadSchema": EVENT_PAYLOAD_SCHEMA,
                }
            ]
        }

    async def subscribe(self, context: Any, params: EventsSubscribeParams) -> dict[str, object]:
        self._require_protocol(context)
        principal = self._principal_resolver(context)
        if not principal:
            raise MCPError(code=FORBIDDEN, message="Forbidden")
        if not self._ready:
            raise MCPError(code=types.INTERNAL_ERROR, message="MCP Events is unavailable")
        if params.name != EVENT_NAME:
            raise MCPError(code=types.INVALID_PARAMS, message="Unsupported event name")
        if params.cursor is not None:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message="Cursor replay is not supported for codex.turn.finished",
                data={"reason": "cursor_replay_unsupported"},
            )
        arguments = _validate_arguments(params.arguments)
        await self._validate_local_target(arguments["thread_id"], arguments.get("turn_id"))
        if params.delivery.mode != "webhook":
            raise MCPError(code=types.INVALID_PARAMS, message="Unsupported delivery mode")
        try:
            validate_callback_url(params.delivery.url)
        except ValueError as exc:
            raise MCPError(
                code=CALLBACK_ENDPOINT_ERROR,
                message="Callback verification failed",
                data={"reason": "invalid_callback"},
            ) from exc
        try:
            secret_bytes = decode_signing_secret(params.delivery.secret)
        except ValueError as exc:
            raise MCPError(
                code=types.INVALID_PARAMS, message="Invalid webhook signing secret"
            ) from exc
        canonical_arguments = canonical_json(arguments)
        subscription_id = self.store_subscription_id(
            principal, params.delivery.url, params.name, canonical_arguments
        )
        try:
            await verify_callback(
                client=self._webhook_client,
                cache=self._verification_cache,
                principal=principal,
                url=params.delivery.url,
                signing_secret=secret_bytes,
                subscription_id=subscription_id,
                timeout_seconds=self.config.webhook_timeout_seconds,
            )
        except CallbackRequestError as exc:
            raise MCPError(
                code=CALLBACK_ENDPOINT_ERROR,
                message="Callback verification failed",
                data={"reason": exc.reason},
            ) from None
        granted_ttl_ms = _granted_ttl_ms(params.ttl_ms, self.config)
        now = self._clock()
        expires_at = now + timedelta(milliseconds=granted_ttl_ms)
        identifier = self.store.upsert_subscription(
            principal=principal,
            event_name=params.name,
            canonical_arguments=canonical_arguments,
            callback_url=params.delivery.url,
            signing_secret=params.delivery.secret,
            expires_at=expires_at,
            now=now,
        )
        self.worker.notify()
        return {
            "id": identifier,
            "refreshBefore": utc_iso(expires_at),
            "cursor": None,
            "truncated": False,
        }

    async def unsubscribe(self, context: Any, params: EventsUnsubscribeParams) -> dict[str, object]:
        self._require_protocol(context)
        principal = self._principal_resolver(context)
        if not principal:
            raise MCPError(code=FORBIDDEN, message="Forbidden")
        if not self._ready:
            raise MCPError(code=types.INTERNAL_ERROR, message="MCP Events is unavailable")
        if params.name != EVENT_NAME:
            raise MCPError(code=types.INVALID_PARAMS, message="Unsupported event name")
        arguments = _validate_arguments(params.arguments)
        if params.delivery.mode not in {None, "webhook"}:
            raise MCPError(code=types.INVALID_PARAMS, message="Unsupported delivery mode")
        try:
            validate_callback_url(params.delivery.url)
        except ValueError as exc:
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid callback URL") from exc
        self.store.unsubscribe(
            principal=principal,
            event_name=params.name,
            canonical_arguments=canonical_json(arguments),
            callback_url=params.delivery.url,
            now=self._clock(),
        )
        self.worker.notify()
        return {}

    async def _validate_local_target(self, thread_id: str, turn_id: str | None) -> None:
        if "::" in thread_id:
            self._forbidden("remote_target")
        bridge = self._bridge_getter()
        if bridge is None:
            raise MCPError(code=types.INTERNAL_ERROR, message="CodexBridge is not running")
        if turn_id is not None:
            try:
                turn_exists = await bridge._event_thread_turn_exists(thread_id, turn_id)
            except Exception as exc:
                log_event(
                    "mcp_events.target_rejected",
                    reason="thread_unavailable",
                    error_type=exc.__class__.__name__,
                )
                self._forbidden("thread_unavailable")
            if not turn_exists:
                self._forbidden("turn_unavailable")
            return
        try:
            result = await bridge.threads(thread_id, include_history=False)
        except Exception as exc:
            log_event(
                "mcp_events.target_rejected",
                reason="thread_unavailable",
                error_type=exc.__class__.__name__,
            )
            self._forbidden("thread_unavailable")
        thread = result.get("thread") if isinstance(result, Mapping) else None
        if not isinstance(thread, Mapping) or thread.get("id") != thread_id:
            self._forbidden("thread_unavailable")

    @staticmethod
    def store_subscription_id(
        principal: str, callback_url: str, event_name: str, canonical_arguments: str
    ) -> str:
        return subscription_identifier(principal, callback_url, event_name, canonical_arguments)

    @staticmethod
    def _require_protocol(context: object) -> None:
        if getattr(context, "protocol_version", None) != PROTOCOL_VERSION:
            raise MCPError(
                code=types.INVALID_REQUEST,
                message=f"MCP Events require protocol version {PROTOCOL_VERSION}",
            )

    @staticmethod
    def _forbidden(reason: str) -> NoReturn:
        log_event("mcp_events.subscription_forbidden", reason=reason)
        raise MCPError(code=FORBIDDEN, message="Forbidden")


def _validate_arguments(arguments: Mapping[str, Any]) -> dict[str, str]:
    allowed = {"thread_id", "turn_id"}
    if set(arguments).difference(allowed):
        raise MCPError(
            code=types.INVALID_PARAMS,
            message="Event arguments contain unsupported fields",
        )
    thread_id = arguments.get("thread_id")
    turn_id = arguments.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise MCPError(code=types.INVALID_PARAMS, message="thread_id is required")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        raise MCPError(code=types.INVALID_PARAMS, message="turn_id must be a non-empty string")
    result = {"thread_id": thread_id}
    if turn_id is not None:
        result["turn_id"] = turn_id
    return result


def _granted_ttl_ms(requested: int | None, config: McpEventsConfig) -> int:
    if requested is None:
        requested = config.default_ttl_seconds * 1000
    elif requested <= 0:
        raise MCPError(
            code=types.INVALID_PARAMS,
            message="ttlMs must be a positive integer or null",
        )
    return max(60_000, min(requested, config.max_ttl_seconds * 1000))


def _fallback_events_path() -> Path:
    import os
    import sys

    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return base / "CodexBridge" / "mcp_events.sqlite3"
