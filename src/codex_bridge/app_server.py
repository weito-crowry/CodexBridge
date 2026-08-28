from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from .jsonrpc import JsonObject, JsonRpcTransport
from .logging_utils import log_event

logger = logging.getLogger(__name__)

_APP_SERVER_STREAM_LIMIT = 16 * 1024 * 1024


class ProcessLike(Protocol):
    stdin: Any
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> Any: ...

    def kill(self) -> Any: ...


ProcessFactory = Callable[..., Awaitable[ProcessLike]]
MessageCallback = Callable[[JsonObject], Awaitable[None] | None]
FailureCallback = Callable[[str], Awaitable[None] | None]


async def _create_process(*args: str) -> ProcessLike:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_APP_SERVER_STREAM_LIMIT,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("Codex App Server pipes are unavailable")
    return cast(ProcessLike, process)


class AppServerClient:
    def __init__(
        self,
        executable: str,
        *,
        process_factory: ProcessFactory = _create_process,
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
        on_failure: FailureCallback | None = None,
    ) -> None:
        self._executable = executable
        self._process_factory = process_factory
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_failure = on_failure
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
        on_failure: FailureCallback | None = None,
    ) -> None:
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._on_failure = on_failure
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
        if self._process is not None:
            raise RuntimeError("previous App Server process is still running")
        self._process = await self._process_factory(self._executable, "app-server", "--stdio")
        if (
            self._process.stdin is None
            or self._process.stdout is None
            or self._process.stderr is None
        ):
            raise RuntimeError("Codex App Server pipes are unavailable")
        self._transport = JsonRpcTransport(
            self._process.stdout,
            self._process.stdin,
            on_notification=self._on_notification,
            on_server_request=self._on_server_request,
        )
        self._reader_task = asyncio.create_task(self._run_reader())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            initialize_response = await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "codexbridge", "version": "0.1.0"},
                    "capabilities": {},
                },
            )
            user_agent = initialize_response.get("userAgent")
            if isinstance(user_agent, str):
                version = re.search(r"(?:Codex Desktop|codex-cli)/(\d+\.\d+\.\d+)", user_agent)
                if version:
                    log_event("codex.version", codex_version=version.group(1))
            await self._transport.send_notification("initialized", {})
            log_event("app_server.start")
        except Exception:
            await self.shutdown()
            raise

    async def _run_reader(self) -> None:
        assert self._transport is not None
        try:
            await self._transport.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._transport.closing and not self._transport.reader_failed:
                return
            self._failed = True
            self._failure = str(exc)[:2_000]
            log_event("protocol.error", error_type=type(exc).__name__)
            if self._process is not None and self._process.returncode not in (None, 0):
                log_event("subprocess.abnormal_exit", exit_code=self._process.returncode)
            if self._on_failure is not None:
                result = self._on_failure(self._failure)
                if inspect.isawaitable(result):
                    await result

    async def _drain_stderr(self) -> None:
        assert self._process is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            logger.debug("App Server emitted diagnostic stderr")

    async def request(self, method: str, params: JsonObject) -> JsonObject:
        if self._transport is None:
            raise RuntimeError("App Server is not started")
        return await self._transport.send_request(method, params)

    async def respond(self, request_id: int | str, result: JsonObject) -> None:
        if self._transport is None:
            raise RuntimeError("App Server is not started")
        await self._transport.send_response(request_id, result)

    async def reject(self, request_id: int | str, code: int, message: str) -> None:
        if self._transport is None:
            raise RuntimeError("App Server is not started")
        await self._transport.send_error(request_id, code, message)

    async def shutdown(self, grace_seconds: float = 3.0) -> None:
        transport, process = self._transport, self._process
        if process is None:
            return
        deadline = asyncio.get_running_loop().time() + max(0.0, grace_seconds)
        if transport is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(transport.close(), timeout=remaining)
                except TimeoutError:
                    logger.error("App Server transport close exceeded shutdown deadline")
                except Exception:
                    logger.debug("App Server transport close failed during shutdown")
        if self._reader_task is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(self._reader_task, timeout=remaining)
                except TimeoutError:
                    if not self._reader_task.done():
                        self._reader_task.cancel()
                    await asyncio.gather(self._reader_task, return_exceptions=True)
                except Exception:
                    pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        if process.returncode is None:
            try:
                process.terminate()
            except Exception:
                logger.debug("App Server terminate failed during shutdown")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(process.wait(), timeout=remaining / 2)
                except TimeoutError:
                    pass
                except Exception:
                    pass
        if process.returncode is None:
            try:
                process.kill()
            except Exception:
                logger.error("App Server kill failed during shutdown")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(process.wait(), timeout=remaining)
                except TimeoutError:
                    logger.error("App Server did not exit after kill")
                except Exception:
                    pass
        if process.returncode is None:
            logger.error("App Server process remains after bounded shutdown")
            self._transport = None
            self._reader_task = None
            self._stderr_task = None
            return
        self._transport = None
        self._process = None
        self._reader_task = None
        self._stderr_task = None
        log_event("app_server.shutdown")
