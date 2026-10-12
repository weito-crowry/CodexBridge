from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import BridgeConfig
from .models import SandboxMode
from .paths import AllowedPathPolicy, PathPolicyError
from .thread_titles import normalize_explicit_title

_MAX_BODY_BYTES = 64 * 1024
_MAX_PROMPT_CHARS = 32_000
_MAX_REQUESTS = 128
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_ACTIVE_STATES = {"in_progress", "needs_approval", "needs_input"}
_TERMINAL_STATES = {"completed", "interrupted", "failed"}


class ControlConflictError(Exception):
    """Raised when a control operation conflicts with current or recorded state."""


class ControlBridge(Protocol):
    async def model_capabilities(self) -> dict[str, Any]: ...

    async def start(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: SandboxMode | None = None,
        title: str | None = None,
    ) -> dict[str, Any]: ...

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    async def status(
        self,
        thread_id: str,
        turn_id: str | None = None,
        activity_limit: int = 20,
    ) -> dict[str, Any]: ...

    async def read_thread_turns(
        self,
        thread_id: str,
        limit: int = 20,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class _RequestRecord:
    operation: str
    digest: bytes
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None
    dispatched: bool = False
    uncertain: bool = False


class _IdempotencyRegistry:
    def __init__(self) -> None:
        self._records: dict[str, _RequestRecord] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self,
        request_id: str,
        operation: str,
        digest: bytes,
        action: Callable[[Callable[[], None]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        async with self._lock:
            record = self._records.get(request_id)
            if record is not None:
                if record.operation != operation or record.digest != digest:
                    raise ControlConflictError("request id is already in use")
                owner = False
            else:
                self._make_room()
                record = _RequestRecord(operation, digest)
                self._records[request_id] = record
                owner = True

        if not owner:
            await record.completed.wait()
            if record.uncertain:
                raise ControlConflictError("request outcome is unknown")
            if record.result is None:
                raise ControlConflictError("request outcome is unavailable")
            return dict(record.result)

        try:
            result = await action(lambda: setattr(record, "dispatched", True))
        except BaseException:
            record.uncertain = record.dispatched
            record.completed.set()
            raise
        record.result = result
        record.completed.set()
        return dict(result)

    def _make_room(self) -> None:
        while len(self._records) >= _MAX_REQUESTS:
            removable = next(
                (
                    request_id
                    for request_id, record in self._records.items()
                    if record.completed.is_set() and not record.uncertain
                ),
                None,
            )
            if removable is None:
                raise ControlConflictError("control request capacity is full")
            del self._records[removable]


def _valid_origin(request: Request, ui_port: int) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == ui_port
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
        )
    except ValueError:
        return False


async def _json_payload(request: Request, allowed_fields: set[str]) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("invalid request")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) < 0 or int(content_length) > _MAX_BODY_BYTES:
                raise ValueError("invalid request")
        except ValueError as exc:
            raise ValueError("invalid request") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ValueError("invalid request")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid request") from exc
    if not isinstance(payload, dict) or set(payload) - allowed_fields:
        raise ValueError("invalid request")
    return payload


def _request_id(payload: Mapping[str, Any]) -> str:
    value = payload.get("client_request_id")
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("invalid request")
    return value


def _prompt(payload: Mapping[str, Any]) -> str:
    value = payload.get("prompt")
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_PROMPT_CHARS:
        raise ValueError("invalid request")
    return value


def _digest(operation: str, payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


def _safe_turn_result(result: Mapping[str, Any], thread_id: str | None = None) -> dict[str, Any]:
    returned_thread_id = result.get("thread_id")
    returned_turn_id = result.get("turn_id")
    state = result.get("state")
    if (
        not isinstance(returned_thread_id, str)
        or not returned_thread_id
        or (thread_id is not None and returned_thread_id != thread_id)
        or not isinstance(returned_turn_id, str)
        or not returned_turn_id
        or not isinstance(state, str)
        or state not in _ACTIVE_STATES | _TERMINAL_STATES
    ):
        raise RuntimeError("Bridge returned malformed control result")
    return {
        "thread_id": returned_thread_id,
        "turn_id": returned_turn_id,
        "state": state,
    }


class UiControl:
    def __init__(self, bridge: ControlBridge, config: BridgeConfig) -> None:
        self._bridge = bridge
        self._path_policy = AllowedPathPolicy(config.allowed_roots)
        self._requests = _IdempotencyRegistry()

    async def capabilities(self) -> dict[str, Any]:
        return await self._bridge.model_capabilities()

    async def create_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "client_request_id",
            "cwd",
            "prompt",
            "model",
            "reasoning_effort",
            "sandbox_mode",
            "confirm_full_access",
            "title",
        }
        if set(payload) - allowed or not {"client_request_id", "cwd", "prompt"} <= set(payload):
            raise ValueError("invalid request")
        request_id = _request_id(payload)
        cwd = payload["cwd"]
        prompt = _prompt(payload)
        if not isinstance(cwd, str):
            raise ValueError("invalid request")
        model = payload.get("model")
        effort = payload.get("reasoning_effort")
        if "model" in payload and (not isinstance(model, str) or not model.strip()):
            raise ValueError("invalid request")
        if "reasoning_effort" in payload and (not isinstance(effort, str) or not effort.strip()):
            raise ValueError("invalid request")
        if effort is not None and model is None:
            raise ValueError("invalid request")
        raw_sandbox = payload.get("sandbox_mode", "inherit")
        if not isinstance(raw_sandbox, str) or raw_sandbox not in {
            "inherit",
            "danger-full-access",
        }:
            raise ValueError("invalid request")
        confirm_full_access = payload.get("confirm_full_access", False)
        if not isinstance(confirm_full_access, bool):
            raise ValueError("invalid request")
        if raw_sandbox == "danger-full-access" and not confirm_full_access:
            raise ValueError("invalid request")
        raw_title = payload.get("title")
        if raw_title is not None and not isinstance(raw_title, str):
            raise ValueError("invalid request")

        async def action(mark_dispatched: Callable[[], None]) -> dict[str, Any]:
            canonical_cwd = self._path_policy.validate_cwd(cwd)
            title = normalize_explicit_title(raw_title) if raw_title is not None else None
            if model is not None:
                catalog = await self._bridge.model_capabilities()
                models = catalog.get("models")
                if not isinstance(models, list):
                    raise RuntimeError("Bridge model capabilities are malformed")
                model_entry = next(
                    (
                        entry
                        for entry in models
                        if isinstance(entry, Mapping) and entry.get("model") == model
                    ),
                    None,
                )
                if model_entry is None:
                    raise ValueError("invalid request")
                if effort is not None:
                    efforts = model_entry.get("reasoning_efforts")
                    if not isinstance(efforts, list) or not any(
                        isinstance(entry, Mapping) and entry.get("id") == effort
                        for entry in efforts
                    ):
                        raise ValueError("invalid request")
            mark_dispatched()
            result = await self._bridge.start(
                canonical_cwd,
                prompt,
                model=model,
                reasoning_effort=effort,
                sandbox_mode=(
                    "danger-full-access" if raw_sandbox == "danger-full-access" else None
                ),
                title=title,
            )
            safe = _safe_turn_result(result)
            title_assignment = result.get("title_assignment")
            if isinstance(title_assignment, Mapping):
                assignment_status = title_assignment.get("status")
                if assignment_status == "assigned" and isinstance(
                    title_assignment.get("name"), str
                ):
                    safe["title_assignment"] = {
                        "status": "assigned",
                        "name": title_assignment["name"],
                    }
                elif assignment_status == "failed":
                    safe["title_assignment"] = {"status": "failed"}
            return safe

        return await self._requests.execute(
            request_id, "create_thread", _digest("create_thread", payload), action
        )

    async def continue_thread(self, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"client_request_id", "prompt"}
        if set(payload) - allowed or not {"client_request_id", "prompt"} <= set(payload):
            raise ValueError("invalid request")
        request_id = _request_id(payload)
        prompt = _prompt(payload)
        if not thread_id:
            raise ValueError("invalid request")

        async def action(mark_dispatched: Callable[[], None]) -> dict[str, Any]:
            await self._validated_thread(thread_id)
            await self._ensure_inactive(thread_id)
            mark_dispatched()
            result = await self._bridge.continue_thread(thread_id, prompt)
            return _safe_turn_result(result, thread_id)

        return await self._requests.execute(
            request_id,
            "continue_thread",
            _digest("continue_thread:" + thread_id, payload),
            action,
        )

    async def interrupt(
        self, thread_id: str, turn_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {"client_request_id"}
        if set(payload) != allowed:
            raise ValueError("invalid request")
        request_id = _request_id(payload)
        if not thread_id or not turn_id:
            raise ValueError("invalid request")

        async def action(mark_dispatched: Callable[[], None]) -> dict[str, Any]:
            await self._validated_thread(thread_id)
            await self._ensure_interruptible(thread_id, turn_id)
            mark_dispatched()
            result = await self._bridge.interrupt(thread_id, turn_id)
            safe = _safe_turn_result(result, thread_id)
            if safe["turn_id"] != turn_id:
                raise RuntimeError("Bridge returned a mismatched control result")
            return safe

        return await self._requests.execute(
            request_id,
            "interrupt",
            _digest("interrupt:" + thread_id + ":" + turn_id, payload),
            action,
        )

    async def _validated_thread(self, thread_id: str) -> None:
        result = await self._bridge.threads(thread_id)
        thread = result.get("thread")
        if (
            not isinstance(thread, Mapping)
            or thread.get("id") != thread_id
            or not isinstance(thread.get("cwd"), str)
        ):
            raise PathPolicyError("thread is not available")
        self._path_policy.validate_cwd(thread["cwd"])

    async def _ensure_inactive(self, thread_id: str) -> None:
        status = await self._bridge.status(thread_id)
        if status.get("thread_id") != thread_id:
            raise ControlConflictError("thread state is unavailable")
        if status.get("pending_request") is not None:
            raise ControlConflictError("thread is waiting for a response")
        state = status.get("state")
        if isinstance(state, str) and state in _ACTIVE_STATES:
            raise ControlConflictError("thread already has an active turn")
        if isinstance(state, str) and state in _TERMINAL_STATES:
            return
        if not (state is None or (isinstance(state, str) and state in {"not_loaded", "unknown"})):
            raise ControlConflictError("thread state is unavailable")
        history = await self._bridge.read_thread_turns(thread_id, limit=100)
        turns = history.get("turns")
        if not isinstance(turns, list):
            raise RuntimeError("Bridge turn history is malformed")
        if not turns:
            if history.get("next_cursor") is not None:
                raise ControlConflictError("thread state is unavailable")
            return
        latest = turns[0]
        if not isinstance(latest, Mapping) or not isinstance(latest.get("id"), str):
            raise RuntimeError("Bridge turn history is malformed")
        turn_state = latest.get("status")
        if not isinstance(turn_state, str) or turn_state in _ACTIVE_STATES:
            raise ControlConflictError("thread state is active or unknown")
        if turn_state not in _TERMINAL_STATES:
            raise ControlConflictError("thread state is active or unknown")

    async def _ensure_interruptible(self, thread_id: str, turn_id: str) -> None:
        status = await self._bridge.status(thread_id, turn_id)
        if status.get("thread_id") != thread_id:
            raise ControlConflictError("turn state is unavailable")
        if status.get("turn_id") != turn_id:
            raise ControlConflictError("turn does not belong to the thread")
        if status.get("pending_request") is not None:
            raise ControlConflictError("turn is waiting for a response")
        state = status.get("state")
        if state == "in_progress":
            return
        if isinstance(state, str) and state in {"needs_approval", "needs_input"}:
            raise ControlConflictError("turn is waiting for a response")
        if isinstance(state, str) and state in _TERMINAL_STATES:
            raise ControlConflictError("turn is not running")
        if not (state is None or (isinstance(state, str) and state in {"not_loaded", "unknown"})):
            raise ControlConflictError("turn state is unavailable")
        turn = await self._find_turn(thread_id, turn_id)
        if turn is None:
            raise ControlConflictError("turn does not belong to the thread")
        turn_state = turn.get("status")
        if isinstance(turn_state, str) and turn_state in _TERMINAL_STATES:
            raise ControlConflictError("turn is not running")
        raise ControlConflictError("turn is not confirmed as actively running")

    async def _find_turn(self, thread_id: str, turn_id: str) -> Mapping[str, Any] | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(100):
            history = await self._bridge.read_thread_turns(
                thread_id, limit=100, cursor=cursor, sort_direction="desc"
            )
            turns = history.get("turns")
            if not isinstance(turns, list):
                raise RuntimeError("Bridge turn history is malformed")
            for turn in turns:
                if isinstance(turn, Mapping) and turn.get("id") == turn_id:
                    return turn
            next_cursor = history.get("next_cursor")
            if next_cursor is None:
                return None
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("Bridge turn history cursor is malformed")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("Bridge turn history exceeded its page bound")


def create_control_routes(
    bridge: ControlBridge,
    config: BridgeConfig,
    authorize: Callable[[Request, str | None], bool],
    error_response: Callable[[Exception], Response],
) -> list[Route]:
    control = UiControl(bridge, config)

    def authorized(request: Request) -> bool:
        return authorize(request, config.control_token) and _valid_origin(request, config.ui_port)

    async def capabilities(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            return JSONResponse(await control.capabilities())
        except Exception as exc:
            return error_response(exc)

    async def create_thread(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await _json_payload(
                request,
                {
                    "client_request_id",
                    "cwd",
                    "prompt",
                    "model",
                    "reasoning_effort",
                    "sandbox_mode",
                    "confirm_full_access",
                    "title",
                },
            )
            return JSONResponse(await control.create_thread(payload))
        except Exception as exc:
            return error_response(exc)

    async def continue_thread(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await _json_payload(request, {"client_request_id", "prompt"})
            result = await control.continue_thread(request.path_params["thread_id"], payload)
            return JSONResponse(result)
        except Exception as exc:
            return error_response(exc)

    async def interrupt(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await _json_payload(request, {"client_request_id"})
            result = await control.interrupt(
                request.path_params["thread_id"],
                request.path_params["turn_id"],
                payload,
            )
            return JSONResponse(result)
        except Exception as exc:
            return error_response(exc)

    return [
        Route("/ui-api/control/capabilities", capabilities, methods=["GET"]),
        Route("/ui-api/control/threads", create_thread, methods=["POST"]),
        Route("/ui-api/control/threads/{thread_id}/turns", continue_thread, methods=["POST"]),
        Route(
            "/ui-api/control/threads/{thread_id}/turns/{turn_id}/interrupt",
            interrupt,
            methods=["POST"],
        ),
    ]
