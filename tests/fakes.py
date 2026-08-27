from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.closed = False
        self.ignore_wait_closed = False
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
        if self.ignore_wait_closed:
            await asyncio.sleep(10)
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.ignore_terminate = False
        self.ignore_kill = False
        self.waited = asyncio.Event()
        self.methods: list[str] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.server_request: dict[str, Any] | None = None
        self.thread_cwds: dict[str, str] = {}
        self.thread_list: list[dict[str, Any]] = []

        async def on_message(message: dict[str, Any]) -> None:
            method = message.get("method")
            if method is None:
                return
            self.methods.append(method)
            if method == "initialize":
                self.feed({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})
            elif method == "thread/read":
                thread_id = message["params"]["threadId"]
                thread: dict[str, Any] = {"id": thread_id, "turns": []}
                if thread_id in self.thread_cwds:
                    thread["cwd"] = self.thread_cwds[thread_id]
                self.feed({"jsonrpc": "2.0", "id": message["id"], "result": {"thread": thread}})
            elif method == "thread/list":
                self.feed(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"data": self.thread_list},
                    }
                )
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
        if self.ignore_terminate:
            return
        self.returncode = 0
        self.waited.set()

    def kill(self) -> None:
        self.killed = True
        if self.ignore_kill:
            return
        self.returncode = -9
        self.waited.set()


class FakeProcessFactory:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.args: tuple[str, ...] | None = None

    async def __call__(self, *args: str) -> FakeProcess:
        self.args = args
        return self.process
