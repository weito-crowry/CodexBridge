from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from starlette.applications import Starlette


class UvicornServerLike(Protocol):
    should_exit: bool
    started: bool

    async def serve(self) -> Any: ...


ServerFactory = Callable[[Starlette, str, int], UvicornServerLike]


def _create_server(app: Starlette, host: str, port: int) -> UvicornServerLike:
    import uvicorn

    return uvicorn.Server(uvicorn.Config(app, host=host, port=port))


class LocalUiServer:
    """Own one bounded Uvicorn task for the fixed-loopback UI application."""

    def __init__(
        self,
        app: Starlette,
        port: int,
        *,
        server_factory: ServerFactory = _create_server,
    ) -> None:
        self._app = app
        self._port = port
        self._server_factory = server_factory
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
        except TimeoutError:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except BaseException:
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._server = None
            self._task = None
