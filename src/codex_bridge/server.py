from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from .app_server import AppServerClient
from .bridge import Bridge
from .config import BridgeConfig
from .models import ApprovalDecision
from .paths import AllowedPathPolicy
from .state import StateStore


class RuntimeLike(Protocol):
    bridge: Bridge

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...


@dataclass(slots=True)
class BridgeRuntime:
    bridge: Bridge
    app_server: AppServerClient
    config: BridgeConfig

    async def start(self) -> None:
        await self.app_server.start()

    async def shutdown(self) -> None:
        await self.bridge.interrupt_active_turns()
        await self.app_server.shutdown(self.config.shutdown_grace_seconds)


def build_runtime(config: BridgeConfig) -> BridgeRuntime:
    state = StateStore()
    app_server = AppServerClient(config.codex_executable)
    bridge = Bridge(
        app_server,
        state,
        AllowedPathPolicy(config.allowed_roots),
        wait_default_seconds=config.wait_default_seconds,
        wait_max_seconds=config.wait_max_seconds,
    )
    app_server.set_handlers(
        on_notification=bridge.handle_notification,
        on_server_request=bridge.handle_server_request,
    )
    return BridgeRuntime(bridge=bridge, app_server=app_server, config=config)


def _transport_security(config: BridgeConfig) -> TransportSecuritySettings | None:
    if not config.allowed_hosts and not config.allowed_origins:
        return None
    return TransportSecuritySettings(
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=list(config.allowed_origins),
    )


def create_app(
    config: BridgeConfig,
    *,
    runtime_factory=build_runtime,
) -> Starlette:
    mcp = MCPServer("CodexBridge", version="0.1.0")
    runtime_holder: dict[str, RuntimeLike | None] = {"runtime": None}

    def bridge() -> Bridge:
        runtime = runtime_holder["runtime"]
        if runtime is None:
            raise RuntimeError("CodexBridge runtime is not started")
        return runtime.bridge

    @mcp.tool()
    async def codex_start(cwd: str, prompt: str) -> dict[str, Any]:
        """Start a native Codex thread and its first turn without waiting for completion."""
        return await bridge().start(cwd, prompt)

    @mcp.tool()
    async def codex_continue(thread_id: str, prompt: str) -> dict[str, Any]:
        """Continue a native Codex thread, resuming it when needed."""
        return await bridge().continue_thread(thread_id, prompt)

    @mcp.tool()
    async def codex_wait(
        thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Wait for a bounded Codex turn state change or terminal state."""
        return await bridge().wait(thread_id, turn_id, timeout_seconds)

    @mcp.tool()
    async def codex_steer(thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        """Send additional input to the expected active Codex turn."""
        return await bridge().steer(thread_id, turn_id, prompt)

    @mcp.tool()
    async def codex_approval(request_id: int | str, decision: ApprovalDecision) -> dict[str, Any]:
        """Resolve one pending Codex command, file, or permission approval request."""
        return await bridge().approve(request_id, decision)

    @mcp.tool()
    async def codex_user_input(
        request_id: int | str, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Resolve one pending Codex user-input request by exact question IDs."""
        return await bridge().answer_user_input(request_id, answers)

    @mcp.tool()
    async def codex_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
        """Request interruption of a running Codex turn."""
        return await bridge().interrupt(thread_id, turn_id)

    @mcp.tool()
    async def codex_threads(
        thread_id: str | None = None,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List native Codex threads or read one native thread's bounded history."""
        return await bridge().threads(
            thread_id,
            include_history=include_history,
            limit=limit,
            cursor=cursor,
        )

    security = _transport_security(config)
    transport_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
        host=config.host,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            runtime = runtime_factory(config)
            runtime_holder["runtime"] = runtime
            app.state.runtime = runtime
            app.state.bridge = runtime.bridge
            await runtime.start()
            try:
                yield
            finally:
                await runtime.shutdown()
                runtime_holder["runtime"] = None
                app.state.runtime = None
                app.state.bridge = None

    app = Starlette(routes=[Mount("/", app=transport_app)], lifespan=lifespan)
    app.state.mcp_server = mcp
    app.state.transport_security = security
    app.state.runtime = None
    app.state.bridge = None
    return app


async def run_server(config: BridgeConfig) -> None:
    import uvicorn

    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port))
    await server.serve()

