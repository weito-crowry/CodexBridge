from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic

from starlette.types import ASGIApp, Message, Receive, Scope, Send

BRIDGE_LOG_FILE_NAME = "bridge-observability.jsonl"
CONSOLE_LOG_FILE_NAME = "console-observability.jsonl"
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3
_PROTOCOL_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_SAFE_STRING_PATTERN = re.compile(r"[^\r\n\x00-\x1f\x7f]{1,256}\Z")
_ALLOWED_FIELDS = {
    "method",
    "path",
    "status",
    "duration_ms",
    "protocol_version",
    "exception_type",
    "cancelled",
    "exit_code",
}
_observer: ObservabilityLogger | None = None


def default_log_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
    file_name: str = BRIDGE_LOG_FILE_NAME,
) -> Path:
    values = os.environ if environ is None else environ
    platform_name = sys.platform if platform is None else platform
    if platform_name.startswith("win"):
        root_value = values.get("LOCALAPPDATA")
        root = Path(root_value) if root_value else Path.home() / "AppData" / "Local"
    else:
        state_home = values.get("XDG_STATE_HOME")
        root = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return root / "CodexBridge" / "logs" / file_name


def bridge_log_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    return default_log_path(environ, platform=platform, file_name=BRIDGE_LOG_FILE_NAME)


def console_log_path(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> Path:
    return default_log_path(environ, platform=platform, file_name=CONSOLE_LOG_FILE_NAME)


@dataclass(frozen=True, slots=True)
class RequestObservation:
    method: str
    path: str
    protocol_version: str | None
    started_at: float


class ObservabilityLogger:
    def __init__(
        self,
        log_path: Path | None = None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
        clock: Callable[[], float] = monotonic,
        handler_factory: Callable[..., logging.Handler] | None = None,
    ) -> None:
        self.log_path = default_log_path() if log_path is None else Path(log_path)
        self._clock = clock
        self._handler: logging.Handler | None = None
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            factory = handler_factory or RotatingFileHandler
            handler = factory(
                os.fspath(self.log_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._handler = handler
        except Exception:
            self._handler = None

    def server_start(self) -> None:
        self._emit("server.start")

    def server_shutdown(self) -> None:
        self._emit("server.shutdown")

    def mcp_request_start(
        self,
        method: str,
        path: str,
        protocol_version: str | None,
    ) -> RequestObservation:
        started_at = self._now()
        fields = self._request_fields(method, path, protocol_version)
        self._emit("mcp.request.start", **fields)
        return RequestObservation(
            method=method,
            path=path,
            protocol_version=protocol_version,
            started_at=started_at,
        )

    def mcp_request_end(
        self,
        request: RequestObservation,
        *,
        status: int | None,
        exception_type: str | None = None,
        cancelled: bool = False,
    ) -> None:
        try:
            duration_ms = max(0.0, (self._clock() - request.started_at) * 1000.0)
        except Exception:
            duration_ms = None
        fields = self._request_fields(request.method, request.path, request.protocol_version)
        fields["status"] = status
        fields["duration_ms"] = duration_ms
        if cancelled:
            fields["cancelled"] = True
        elif exception_type is not None:
            fields["exception_type"] = exception_type
        self._emit("mcp.request.end", **fields)

    def tunnel_start(self) -> None:
        self._emit("tunnel.start")

    def tunnel_exit(self, exit_code: int | None = None) -> None:
        self._emit("tunnel.exit", exit_code=exit_code)

    def tunnel_recovery(self) -> None:
        self._emit("tunnel.recovery")

    def _now(self) -> float:
        try:
            return self._clock()
        except Exception:
            return 0.0

    @staticmethod
    def _safe_string(value: object, *, max_length: int = 256) -> str | None:
        if not isinstance(value, str) or len(value) > max_length:
            return None
        return value if _SAFE_STRING_PATTERN.fullmatch(value) else None

    @classmethod
    def _safe_protocol_version(cls, value: object) -> str | None:
        if not isinstance(value, str) or not _PROTOCOL_VERSION_PATTERN.fullmatch(value):
            return None
        return value

    @classmethod
    def _request_fields(
        cls,
        method: object,
        path: object,
        protocol_version: object,
    ) -> dict[str, object]:
        return {
            "method": cls._safe_string(method, max_length=32),
            "path": cls._safe_string(path),
            "protocol_version": cls._safe_protocol_version(protocol_version),
        }

    def _emit(self, event: str, **fields: object) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            payload: dict[str, object] = {
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "event": event,
                "pid": os.getpid(),
            }
            for key, value in fields.items():
                if key not in _ALLOWED_FIELDS or value is None:
                    continue
                if key in {"method", "path", "exception_type"}:
                    value = self._safe_string(value)
                elif key == "protocol_version":
                    value = self._safe_protocol_version(value)
                elif key == "status" or key == "exit_code":
                    if isinstance(value, bool) or not isinstance(value, int):
                        value = None
                elif key == "duration_ms":
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        value = None
                    else:
                        value = round(float(value), 3)
                elif key == "cancelled" and not isinstance(value, bool):
                    value = None
                if value is not None:
                    payload[key] = value
            message = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            record = logging.LogRecord(
                name="codex_bridge.observability",
                level=logging.INFO,
                pathname=__file__,
                lineno=0,
                msg=message,
                args=(),
                exc_info=None,
            )
            handler.emit(record)
        except Exception:
            return


class MCPObservabilityMiddleware:
    def __init__(self, app: ASGIApp, *, observer: ObservabilityLogger) -> None:
        self.app = app
        self.observer = observer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return

        request = self.observer.mcp_request_start(
            scope.get("method", ""),
            scope.get("path", ""),
            _protocol_version(scope),
        )
        status: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                value = message.get("status")
                if isinstance(value, int) and not isinstance(value, bool):
                    status = value
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            self.observer.mcp_request_end(request, status=status, cancelled=True)
            raise
        except Exception as exc:
            self.observer.mcp_request_end(
                request,
                status=status,
                exception_type=type(exc).__name__,
            )
            raise
        else:
            self.observer.mcp_request_end(request, status=status)


def _protocol_version(scope: Scope) -> str | None:
    headers = scope.get("headers", ())
    for header in headers:
        if not isinstance(header, (tuple, list)) or len(header) != 2:
            continue
        name, value = header
        if not isinstance(name, bytes) or name.lower() != b"mcp-protocol-version":
            continue
        if not isinstance(value, bytes):
            return None
        try:
            decoded = value.decode("ascii")
        except UnicodeDecodeError:
            return None
        return decoded if _PROTOCOL_VERSION_PATTERN.fullmatch(decoded) else None
    return None


def get_observer() -> ObservabilityLogger:
    global _observer
    if _observer is None:
        _observer = ObservabilityLogger(log_path=console_log_path())
    return _observer
