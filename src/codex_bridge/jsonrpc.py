from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

JsonObject = dict[str, Any]
MessageCallback = Callable[[JsonObject], Awaitable[None] | None]


class WritableStream(Protocol):
    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> None: ...

    def close(self) -> Any: ...

    async def wait_closed(self) -> None: ...


class JsonRpcError(RuntimeError):
    """Base class for bridge-side JSON-RPC failures."""


class JsonRpcClosedError(JsonRpcError):
    """Raised when the App Server closes its protocol stream."""


class JsonRpcProtocolError(JsonRpcError):
    """Raised when the App Server emits malformed JSON-RPC."""


class JsonRpcRemoteError(JsonRpcError):
    """Raised for an error response from the App Server."""


class JsonRpcTransport:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: WritableStream,
        *,
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._next_id = 1
        self._closed = False

    def set_callbacks(
        self,
        *,
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
    ) -> None:
        self._on_notification = on_notification
        self._on_server_request = on_server_request

    @property
    def closed(self) -> bool:
        return self._closed

    async def _write(self, message: JsonObject) -> None:
        encoded = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        async with self._write_lock:
            self._writer.write(encoded + b"\n")
            await self._writer.drain()

    async def send_request(self, method: str, params: JsonObject) -> JsonObject:
        if self._closed:
            raise JsonRpcClosedError("JSON-RPC transport is closed")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def send_notification(self, method: str, params: JsonObject) -> None:
        if self._closed:
            raise JsonRpcClosedError("JSON-RPC transport is closed")
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def send_response(self, request_id: int | str, result: JsonObject) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def send_error(self, request_id: int | str, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _dispatch(self, callback: MessageCallback | None, message: JsonObject) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result

    async def run(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    raise JsonRpcClosedError("JSON-RPC transport closed")
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JsonRpcProtocolError("invalid JSON-RPC message") from exc
                if not isinstance(message, dict):
                    raise JsonRpcProtocolError("JSON-RPC message must be an object")
                if "id" in message and ("result" in message or "error" in message):
                    request_id = message["id"]
                    if not isinstance(request_id, int) or isinstance(request_id, bool):
                        raise JsonRpcProtocolError("response id must be an integer")
                    future = self._pending.get(request_id)
                    if future is None:
                        continue
                    if "error" in message:
                        error = message["error"]
                        text = (
                            error.get("message", "remote JSON-RPC error")
                            if isinstance(error, dict)
                            else str(error)
                        )
                        future.set_exception(JsonRpcRemoteError(text))
                    else:
                        result = message.get("result")
                        if not isinstance(result, dict):
                            future.set_exception(
                                JsonRpcProtocolError("JSON-RPC result must be an object")
                            )
                        else:
                            future.set_result(result)
                elif "method" in message:
                    if "id" in message:
                        await self._dispatch(self._on_server_request, message)
                    else:
                        await self._dispatch(self._on_notification, message)
                else:
                    raise JsonRpcProtocolError("unrecognized JSON-RPC message")
        except (JsonRpcClosedError, JsonRpcProtocolError) as exc:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(JsonRpcClosedError("JSON-RPC transport closed"))
        feed_eof = getattr(self._reader, "feed_eof", None)
        if callable(feed_eof):
            feed_eof()
        self._writer.close()
        await self._writer.wait_closed()
