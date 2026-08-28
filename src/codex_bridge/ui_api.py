from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .activity import ActivityStore
from .bridge import Bridge, BridgeError
from .config import BridgeConfig
from .history import HistoryValidationError, project_thread_metadata, validate_cursor
from .jsonrpc import JsonRpcClosedError
from .paths import PathPolicyError


class UiBridge(Protocol):
    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    async def read_thread_turns(
        self,
        thread_id: str,
        limit: int = 20,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]: ...

    async def read_thread_items(
        self,
        thread_id: str,
        turn_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]: ...

    async def status(
        self,
        thread_id: str,
        turn_id: str | None = None,
        activity_limit: int = 20,
    ) -> dict[str, Any]: ...


def _parse_limit(request: Request, default: int) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("invalid limit") from exc
    if not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    return limit


def _parse_activity_limit(request: Request) -> int:
    raw = request.query_params.get("activity_limit")
    if raw is None:
        return 20
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("invalid activity_limit") from exc
    if not 1 <= limit <= 100:
        raise ValueError("invalid activity_limit")
    return limit


def _parse_sort_direction(request: Request) -> str:
    value = request.query_params.get("sort_direction", "desc")
    if value not in {"asc", "desc"}:
        raise ValueError("invalid sort_direction")
    return value


def _cursor(request: Request) -> str | None:
    value = request.query_params.get("cursor")
    value = value or None
    if value is not None:
        try:
            validate_cursor(value)
        except HistoryValidationError as exc:
            raise ValueError("invalid cursor") from exc
    return value


def _app_server_ready(bridge: UiBridge) -> bool:
    explicit = getattr(bridge, "app_server_ready", None)
    if isinstance(explicit, bool):
        return explicit
    app_server = getattr(bridge, "_app_server", None)
    return not bool(getattr(app_server, "failed", False))


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, PathPolicyError):
        return JSONResponse({"error": "thread not found"}, status_code=404)
    if isinstance(exc, (HistoryValidationError, ValueError)):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if isinstance(exc, JsonRpcClosedError):
        return JSONResponse({"error": "App Server unavailable"}, status_code=503)
    if isinstance(exc, BridgeError):
        return JSONResponse({"error": "upstream App Server failure"}, status_code=502)
    return JSONResponse({"error": "upstream App Server failure"}, status_code=502)


def _safe_thread_list(result: dict[str, Any]) -> dict[str, object]:
    raw_threads = result.get("threads")
    threads: list[dict[str, object]] = []
    if isinstance(raw_threads, list):
        for thread in raw_threads:
            projected = project_thread_metadata(thread)
            if projected is not None:
                threads.append(projected)
    return {
        "threads": threads,
        "next_cursor": result.get("next_cursor")
        if isinstance(result.get("next_cursor"), str)
        else None,
        "backwards_cursor": result.get("backwards_cursor")
        if isinstance(result.get("backwards_cursor"), str)
        else None,
    }


def _safe_thread_detail(result: dict[str, Any]) -> dict[str, object] | None:
    return project_thread_metadata(result.get("thread"))


def _sse_event(activity: object) -> bytes:
    to_dict = getattr(activity, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("activity is not serializable")
    public = to_dict()
    activity_id = public.get("activity_id")
    if not isinstance(activity_id, str):
        raise TypeError("activity id is malformed")
    payload = json.dumps(public, separators=(",", ":"), ensure_ascii=False)
    return f"event: activity\nid: {activity_id}\ndata: {payload}\n\n".encode()


def create_ui_app(
    bridge: Bridge,
    activity_store: ActivityStore,
    config: BridgeConfig,
) -> Starlette:
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def status(request: Request) -> Response:
        return JSONResponse(
            {
                "bridge": "ready",
                "app_server": "ready" if _app_server_ready(bridge) else "failed",
                "ui_host": "127.0.0.1",
                "ui_port": config.ui_port,
            }
        )

    async def threads(request: Request) -> Response:
        try:
            limit = _parse_limit(request, 20)
            result = await bridge.threads(limit=limit, cursor=_cursor(request))
            return JSONResponse(_safe_thread_list(result))
        except Exception as exc:
            return _error_response(exc)

    async def thread_detail(request: Request) -> Response:
        try:
            result = await bridge.threads(request.path_params["thread_id"])
            thread = _safe_thread_detail(result)
            if thread is None:
                return JSONResponse({"error": "thread not found"}, status_code=404)
            return JSONResponse({"thread": thread})
        except Exception as exc:
            return _error_response(exc)

    async def turns(request: Request) -> Response:
        try:
            result = await bridge.read_thread_turns(
                request.path_params["thread_id"],
                limit=_parse_limit(request, 20),
                cursor=_cursor(request),
                sort_direction=_parse_sort_direction(request),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def items(request: Request) -> Response:
        try:
            result = await bridge.read_thread_items(
                request.path_params["thread_id"],
                turn_id=request.query_params.get("turn_id") or None,
                limit=_parse_limit(request, 100),
                cursor=_cursor(request),
                sort_direction=_parse_sort_direction(request),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def thread_status(request: Request) -> Response:
        try:
            result = await bridge.status(
                request.path_params["thread_id"],
                turn_id=request.query_params.get("turn_id") or None,
                activity_limit=_parse_activity_limit(request),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def events(request: Request) -> Response:
        thread_id = request.query_params.get("thread_id") or None
        if thread_id is not None:
            try:
                if _safe_thread_detail(await bridge.threads(thread_id)) is None:
                    return JSONResponse({"error": "thread not found"}, status_code=404)
            except Exception as exc:
                return _error_response(exc)

        subscription = activity_store.subscribe(thread_id)

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    try:
                        activity = await asyncio.wait_for(subscription.get(), timeout=15.0)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                    else:
                        yield _sse_event(activity)
            finally:
                subscription.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/healthz", healthz),
        Route("/ui-api/status", status),
        Route("/ui-api/threads", threads),
        Route("/ui-api/threads/{thread_id}", thread_detail),
        Route("/ui-api/threads/{thread_id}/turns", turns),
        Route("/ui-api/threads/{thread_id}/items", items),
        Route("/ui-api/threads/{thread_id}/status", thread_status),
        Route("/ui-api/events", events),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.bridge = bridge
    app.state.activity_store = activity_store
    app.state.config = config
    return app
