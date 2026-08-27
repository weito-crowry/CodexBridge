from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .jsonrpc import JsonObject, JsonRpcTransport


logger = logging.getLogger(__name__)


class ProcessLike(Protocol):
    stdin: Any
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> Any: ...


ProcessFactory = Callable[..., Awaitable[ProcessLike]]
MessageCallback = Callable[[JsonObject], Awaitable[None] | None]


async def _create_process(*args: str) -> ProcessLike:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class AppServerClient:
    def __init__(
        self,
        executable: str,
        *,
        process_factory: ProcessFactory = _create_process,
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
    ) -> None:
        self._executable = executable
        self._process_factory = process_factory
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._process: ProcessLike | None = None
        self._transport: JsonRpcTransport | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._failed = False
        self._failure: str | None = None

    def set_handlers(
        self,
        *,
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
    ) -> None:
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        if self._transport is not None:
            self._transport.set_callbacks(
                on_notification=on_notification,
                on_server_request=on_server_request,
            )

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def failure(self) -> str | None:
        return self._failure

    async def start(self) -> None:
        if self._transport is not None:
            return
        self._process = await self._process_factory(self._executable, "app-server", "--stdio")
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            raise RuntimeError("Codex App Server pipes are unavailable")
        self._transport = JsonRpcTransport(
            self._process.stdout,
            self._process.stdin,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
        )
        self._reader_task = asyncio.create_task(self._run_reader())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {"clientInfo": {"name": "codexbridge", "version": "0.1.0"}, "capabilities": {}},
        )
        await self._transport.send_notification("initialized", {})

    async def _run_reader(self) -> None:
        assert self._transport is not None
        try:
            await self._transport.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed = True
            self._failure = str(exc)[:2_000]
            logger.error("App Server protocol ended: %s", type(exc).__name__)

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            logger.debug("App Server diagnostic: %s", line.decode("utf-8", errors="replace").strip()[:500])

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        if self._transport is None:
            raise RuntimeError("App Server is not started")
        return await self._transport.send_request(method, params)

    async def respond(self, request_id: int | str, result: JsonObject) -> None:
        if self._transport is None:
            raise RuntimeError("App Server is not started")
        await self._transport.send_response(request_id, result)

    async def shutdown(self, grace_seconds: float = 3.0) -> None:
        transport, process = self._transport, self._process
        if transport is None or process is None:
            return
        await transport.close()
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=grace_seconds)
            except (asyncio.TimeoutError, Exception):
                if not self._reader_task.done():
                    self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            except asyncio.TimeoutError:
                logger.error("App Server did not exit during graceful shutdown")
        self._transport = None
        self._process = None
