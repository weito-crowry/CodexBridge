from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.closed = False
        self.on_message: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None

    def write(self, data: bytes) -> None:
        message = json.loads(data.decode("utf-8"))
        self.writes.append(message)
        if self.on_message is not None:
            result = self.on_message(message)
            if result is not None:
                asyncio.create_task(result)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter()
        self.returncode: int | None = None
        self.terminated = False
        self.waited = asyncio.Event()
        self.methods: list[str] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.server_request: dict[str, Any] | None = None

        async def on_message(message: dict[str, Any]) -> None:
            method = message.get("method")
            if method is None:
                return
            self.methods.append(method)
            if method == "initialize":
                self.feed({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})
            elif method in self.responses:
                self.feed({"jsonrpc": "2.0", "id": message["id"], "result": self.responses[method]})

        self.stdin.on_message = on_message

    def feed(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        self.stdout.feed_data(line)

    def feed_eof(self) -> None:
        self.stdout.feed_eof()

    async def wait(self) -> int:
        await self.waited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.waited.set()


class FakeProcessFactory:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.args: tuple[str, ...] | None = None

    async def __call__(self, *args: str) -> FakeProcess:
        self.args = args
        return self.process
