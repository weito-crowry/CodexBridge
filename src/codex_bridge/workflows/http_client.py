from __future__ import annotations

import http.client
import json
import math
import os
import re
from typing import Any
from urllib.parse import quote, urlencode

CONTROL_TOKEN_ENV = "CODEX_BRIDGE_CONTROL_TOKEN"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_DEFAULT_RESPONSE_LIMIT = 65_536
_AGENT_RESPONSE_LIMIT = 1_048_576
_AGENT_REQUEST_LIMIT = 65_536
_AGENT_PROMPT_LIMIT = 32_000
_AGENT_REQUEST_ID_RE = re.compile(r"cba-[0-9a-f]{64}\Z", re.ASCII)


class CodexHTTPError(RuntimeError):
    """An HTTP response did not satisfy the CodexBridge API contract."""


class CodexTransportError(CodexHTTPError):
    """A local HTTP request did not produce a complete response."""


class CodexProtocolError(CodexHTTPError):
    """A response exceeded the limit or was not valid API JSON."""


class CodexHTTPStatusError(CodexHTTPError):
    """An HTTP status preserved for callers that classify POST certainty."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"CodexBridge returned HTTP {status_code}")


class CodexPreflightError(CodexHTTPError):
    """The loopback CodexBridge API is not ready for a live job."""


class CodexHTTPClient:
    """Small, bounded HTTP client restricted to the local CodexBridge UI API."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = _DEFAULT_RESPONSE_LIMIT,
    ) -> None:
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("CodexBridge host must be an explicit loopback address")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("CodexBridge port must be an integer from 1 to 65535")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0.05 <= timeout_seconds <= 30
        ):
            raise ValueError("HTTP timeout must be finite and from 0.05 to 30 seconds")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 1_048_576:
            raise ValueError("response limit must be an integer from 1 to 1048576 bytes")
        self.host = host
        self.port = port
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes

    def preflight(self) -> None:
        token = self._control_token()
        try:
            health = self._request_json("GET", "/healthz")
            if health.get("status") != "ok":
                raise CodexPreflightError("CodexBridge health check is not ready")
            status = self._request_json("GET", "/ui-api/status")
            if status.get("app_server") != "ready":
                raise CodexPreflightError("CodexBridge app_server is not ready")
            capabilities = self._request_json("GET", "/ui-api/control/capabilities", token=token)
            if not isinstance(capabilities, dict):
                raise CodexPreflightError("CodexBridge control capabilities are unavailable")
        except CodexPreflightError:
            raise
        except CodexHTTPError:
            raise CodexPreflightError("CodexBridge API preflight failed") from None

    def create_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {
            "client_request_id",
            "cwd",
            "prompt",
            "title",
            "sandbox_mode",
            "confirm_full_access",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise CodexProtocolError("codex-job request body has an invalid shape")
        if (
            not isinstance(payload["client_request_id"], str)
            or not _ID_RE.fullmatch(payload["client_request_id"])
            or not isinstance(payload["cwd"], str)
            or not os.path.isabs(payload["cwd"])
            or not isinstance(payload["prompt"], str)
            or not payload["prompt"]
            or not isinstance(payload["title"], str)
            or not payload["title"]
            or payload["sandbox_mode"] != "inherit"
            or payload["confirm_full_access"] is not False
        ):
            raise CodexProtocolError("codex-job request body violates the fixed API contract")
        return self._request_json(
            "POST",
            "/ui-api/control/threads",
            payload,
            authenticated=True,
        )

    def observe_thread(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        if not _ID_RE.fullmatch(thread_id) or not _ID_RE.fullmatch(turn_id):
            raise CodexProtocolError("CodexBridge returned an invalid thread or turn ID")
        quoted_thread = quote(thread_id, safe="")
        status = self._request_json(
            "GET",
            f"/ui-api/threads/{quoted_thread}/status?{urlencode({'turn_id': turn_id})}",
        )
        turns_query = urlencode({"sort_direction": "desc", "limit": 20})
        turns_path = f"/ui-api/threads/{quoted_thread}/turns?{turns_query}"
        turns = self._request_json("GET", turns_path)
        items_query = urlencode({"turn_id": turn_id, "sort_direction": "desc", "limit": 100})
        items_path = f"/ui-api/threads/{quoted_thread}/items?{items_query}"
        items = self._request_json("GET", items_path)
        if (
            status.get("thread_id") != thread_id
            or status.get("turn_id") != turn_id
            or not isinstance(status.get("state"), str)
            or turns.get("thread_id") != thread_id
            or not isinstance(turns.get("turns"), list)
            or items.get("thread_id") != thread_id
            or not isinstance(items.get("items"), list)
        ):
            raise CodexProtocolError("CodexBridge returned malformed thread history")
        return {"status": status, "turns": turns["turns"], "items": items["items"]}

    def _control_token(self) -> str:
        token = os.environ.get(CONTROL_TOKEN_ENV)
        if not isinstance(token, str) or not token.strip():
            raise CodexPreflightError("CodexBridge control token is not configured")
        return token

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        token: str | None = None,
        authenticated: bool = False,
        preserve_http_status: bool = False,
    ) -> dict[str, Any]:
        if authenticated:
            token = self._control_token()
        body: bytes | None = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            try:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise CodexProtocolError("CodexBridge request is not valid JSON") from error
            if len(body) > self.max_response_bytes:
                raise CodexProtocolError("CodexBridge request exceeds the configured size limit")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        connection = http.client.HTTPConnection(
            self.host,
            self.port,
            timeout=self.timeout_seconds,
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            if len(raw) > self.max_response_bytes:
                raise CodexProtocolError("CodexBridge response limit exceeded")
            if response.status < 200 or response.status >= 300:
                if preserve_http_status:
                    raise CodexHTTPStatusError(response.status)
                raise CodexHTTPError(f"CodexBridge returned HTTP {response.status}")
        except CodexHTTPError:
            raise
        except (OSError, http.client.HTTPException, TimeoutError):
            raise CodexTransportError("CodexBridge loopback request failed") from None
        finally:
            connection.close()

        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CodexProtocolError("CodexBridge response is not valid JSON") from None
        if not isinstance(value, dict):
            raise CodexProtocolError("CodexBridge response must be a JSON object")
        return value


class CodexAgentHTTPClient(CodexHTTPClient):
    """New Agent transport limits and paged read-only observation methods."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8001,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = _AGENT_RESPONSE_LIMIT,
    ) -> None:
        super().__init__(
            host=host,
            port=port,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def create_agent_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        validate_agent_thread_payload(payload)
        return self._request_json(
            "POST",
            "/ui-api/control/threads",
            payload,
            authenticated=True,
            preserve_http_status=True,
        )

    def get_agent_thread_status(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        _validate_agent_handle_ids(thread_id, turn_id)
        query = urlencode({"turn_id": turn_id, "activity_limit": 1})
        result = self._request_json(
            "GET",
            f"/ui-api/threads/{quote(thread_id, safe='')}/status?{query}",
        )
        if (
            result.get("thread_id") != thread_id
            or result.get("turn_id") != turn_id
            or type(result.get("state")) is not str
        ):
            raise CodexProtocolError("CodexBridge returned a mismatched Agent turn status")
        return result

    def read_agent_turns_page(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _validate_agent_thread_id(thread_id)
        query_values: dict[str, str | int] = {"sort_direction": "desc", "limit": 100}
        if cursor is not None:
            _validate_history_cursor(cursor)
            query_values["cursor"] = cursor
        result = self._request_json(
            "GET",
            f"/ui-api/threads/{quote(thread_id, safe='')}/turns?{urlencode(query_values)}",
        )
        if result.get("thread_id") != thread_id or type(result.get("turns")) is not list:
            raise CodexProtocolError("CodexBridge returned malformed Agent turn history")
        _validate_next_cursor(result)
        return result

    def read_agent_items_page(
        self,
        thread_id: str,
        turn_id: str,
        *,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _validate_agent_handle_ids(thread_id, turn_id)
        query_values: dict[str, str | int] = {
            "turn_id": turn_id,
            "sort_direction": "desc",
            "limit": 100,
        }
        if cursor is not None:
            _validate_history_cursor(cursor)
            query_values["cursor"] = cursor
        result = self._request_json(
            "GET",
            f"/ui-api/threads/{quote(thread_id, safe='')}/items?{urlencode(query_values)}",
        )
        if (
            result.get("thread_id") != thread_id
            or result.get("turn_id") != turn_id
            or type(result.get("items")) is not list
        ):
            raise CodexProtocolError("CodexBridge returned malformed Agent item history")
        _validate_next_cursor(result)
        return result


def _validate_agent_thread_payload(payload: object) -> None:
    if type(payload) is not dict:
        raise CodexProtocolError("Agent request body has an invalid shape")
    required = {"client_request_id", "cwd", "prompt"}
    allowed = required | {"title", "sandbox_mode", "confirm_full_access"}
    if not required.issubset(payload) or set(payload) - allowed:
        raise CodexProtocolError("Agent request body has an invalid shape")
    request_id = payload.get("client_request_id")
    cwd = payload.get("cwd")
    prompt = payload.get("prompt")
    if (
        type(request_id) is not str
        or _AGENT_REQUEST_ID_RE.fullmatch(request_id) is None
        or type(cwd) is not str
        or not os.path.isabs(cwd)
        or type(prompt) is not str
        or not prompt.strip()
        or len(prompt) > _AGENT_PROMPT_LIMIT
        or payload.get("sandbox_mode", "inherit") != "inherit"
        or payload.get("confirm_full_access", False) is not False
    ):
        raise CodexProtocolError("Agent request body violates the supported API contract")
    try:
        prompt.encode("utf-8", errors="strict")
        cwd.encode("utf-8", errors="strict")
        if "title" in payload:
            title = payload["title"]
            if type(title) is not str or not title.strip():
                raise ValueError
            title.encode("utf-8", errors="strict")
    except (UnicodeEncodeError, ValueError):
        raise CodexProtocolError("Agent request contains invalid text") from None


def validate_agent_thread_payload(payload: object) -> int:
    """Validate and size a new Agent request before the ledger dispatch claim."""
    _validate_agent_thread_payload(payload)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise CodexProtocolError("Agent request body is invalid") from None
    if len(encoded) > _AGENT_REQUEST_LIMIT:
        raise CodexProtocolError("Agent request exceeds the UI API size limit")
    return len(encoded)


def _validate_agent_handle_ids(thread_id: object, turn_id: object) -> None:
    if type(turn_id) is not str or _ID_RE.fullmatch(turn_id) is None:
        raise CodexProtocolError("CodexBridge returned an invalid thread or turn ID")
    _validate_agent_thread_id(thread_id)


def _validate_agent_thread_id(thread_id: object) -> None:
    if type(thread_id) is not str or _ID_RE.fullmatch(thread_id) is None:
        raise CodexProtocolError("CodexBridge returned an invalid thread ID")


def _validate_history_cursor(cursor: object) -> None:
    if type(cursor) is not str or not cursor or len(cursor) > 1024:
        raise CodexProtocolError("CodexBridge history cursor is invalid")
    try:
        cursor.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise CodexProtocolError("CodexBridge history cursor is invalid") from None


def _validate_next_cursor(result: dict[str, Any]) -> None:
    if "next_cursor" in result and result["next_cursor"] is not None:
        _validate_history_cursor(result["next_cursor"])
    if "truncated" in result and type(result["truncated"]) is not bool:
        raise CodexProtocolError("CodexBridge history truncation marker is invalid")
