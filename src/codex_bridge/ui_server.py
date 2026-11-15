from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Protocol, cast

import uvicorn
from starlette.applications import Starlette


class UvicornServerLike(Protocol):
    should_exit: bool
    started: bool

    async def serve(self) -> Any: ...


class OuterUvicornServerLike(Protocol):
    should_exit: bool


class UvicornShutdownController:
    """Request graceful exit of the outer MCP Uvicorn server."""

    def __init__(self) -> None:
        self._server: OuterUvicornServerLike | None = None

    def bind(self, server: OuterUvicornServerLike) -> None:
        self._server = server

    def request_shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


ServerFactory = Callable[[Starlette, str, int], UvicornServerLike]


class _SignalFreeUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _create_server(
    app: Starlette, host: str, port: int, graceful_timeout: float = 3.0
) -> UvicornServerLike:
    return _SignalFreeUvicornServer(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            timeout_graceful_shutdown=cast(Any, graceful_timeout),
            log_config=None,
        )
    )


class LocalUiServer:
    """Own one bounded Uvicorn task for the fixed-loopback UI application."""

    def __init__(
        self,
        app: Starlette,
        port: int,
        *,
        server_factory: ServerFactory | None = None,
        graceful_shutdown_timeout: float = 3.0,
    ) -> None:
        self._app = app
        self._port = port
        self._server_factory = server_factory
        self._graceful_shutdown_timeout = graceful_shutdown_timeout
        self._server: UvicornServerLike | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _wait_until_started(self) -> None:
        assert self._server is not None
        assert self._task is not None
        while True:
            if self._task.done():
                error = self._task.exception()
                if error is not None:
                    raise error
                raise RuntimeError("UI server exited during startup")
            if self._server.started:
                return
            await asyncio.sleep(0.01)

    async def start(self) -> None:
        if self.running:
            return
        if self._server_factory is None:
            self._server = _create_server(
                self._app, "127.0.0.1", self._port, self._graceful_shutdown_timeout
            )
        else:
            self._server = self._server_factory(self._app, "127.0.0.1", self._port)
        self._task = asyncio.create_task(self._server.serve())
        try:
            await asyncio.wait_for(self._wait_until_started(), timeout=5.0)
        except BaseException:
            await self.shutdown(0.5)
            raise

    async def shutdown(self, timeout: float = 1.0) -> None:
        server, task = self._server, self._task
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, timeout))
        except TimeoutError as exc:
            if task.done():
                task.result()
            else:
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=0.1)
                if not done:
                    raise TimeoutError("UI server remained running after cancellation") from exc
                if not task.cancelled():
                    task.result()
            raise TimeoutError(
                f"UI server did not shut down within {max(0.0, timeout):g} seconds"
            ) from exc
        finally:
            if task.done():
                self._server = None
                self._task = None
