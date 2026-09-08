from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount

from .activity import ActivityStore
from .app_server import AppServerClient
from .bridge import Bridge
from .codex_resolver import CodexResolutionError, resolve_codex_executable
from .config import BridgeConfig, ConfigurationError, validate_allowed_roots
from .logging_utils import log_event
from .models import ApprovalDecision
from .paths import AllowedPathPolicy, PathPolicyError
from .state import StateStore
from .ui_api import ShutdownCallback, create_ui_app
from .ui_server import LocalUiServer, UvicornShutdownController


class RuntimeLike(Protocol):
    bridge: Bridge

    async def start(self) -> None: ...

    async def shutdown(self) -> None: ...


@dataclass(slots=True)
class BridgeRuntime:
    bridge: Bridge
    app_server: AppServerClient
    activity_store: ActivityStore
    config: BridgeConfig
    ui_server: LocalUiServer

    async def start(self) -> None:
        await self.app_server.start()
        try:
            await self.ui_server.start()
        except BaseException:
            await self.app_server.shutdown(self.config.shutdown_grace_seconds)
            raise
        log_event("bridge.start")

    async def shutdown(self) -> None:
        await self.bridge.interrupt_active_turns(
            wait_seconds=min(0.5, self.config.shutdown_grace_seconds)
        )
        await self.ui_server.shutdown(min(0.5, self.config.shutdown_grace_seconds))
        await self.app_server.shutdown(self.config.shutdown_grace_seconds)
        log_event("bridge.shutdown")


def prepare_config(config: BridgeConfig) -> BridgeConfig:
    allowed_roots = validate_allowed_roots(config.allowed_roots)
    configured_executable = (
        config.codex_executable if config.codex_executable_source != "default" else None
    )
    try:
        resolution = resolve_codex_executable(config_executable=configured_executable)
    except CodexResolutionError as exc:
        raise ConfigurationError(str(exc)) from exc
    return replace(
        config,
        allowed_roots=allowed_roots,
        codex_executable=resolution.path,
        codex_executable_source=resolution.source,
    )


def build_runtime(
    config: BridgeConfig,
    *,
    shutdown_callback: ShutdownCallback | None = None,
) -> BridgeRuntime:
    config = prepare_config(config)
    state = StateStore()
    activity_store = ActivityStore()
    app_server = AppServerClient(config.codex_executable)
    bridge = Bridge(
        app_server,
        state,
        AllowedPathPolicy(config.allowed_roots),
        activity_store=activity_store,
        wait_default_seconds=config.wait_default_seconds,
        wait_max_seconds=config.wait_max_seconds,
    )
    app_server.set_handlers(
        on_notification=bridge.handle_notification,
        on_server_request=bridge.handle_server_request,
        on_failure=bridge.handle_app_server_failure,
    )
    ui_server = LocalUiServer(
        create_ui_app(bridge, activity_store, config, shutdown_callback=shutdown_callback),
        config.ui_port,
    )
    return BridgeRuntime(
        bridge=bridge,
        app_server=app_server,
        activity_store=activity_store,
        config=config,
        ui_server=ui_server,
    )


def _transport_security(config: BridgeConfig) -> TransportSecuritySettings | None:
    if not config.allowed_hosts and not config.allowed_origins:
        if config.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError("non-loopback bind requires at least one allowed host")
        return None
    allowed_hosts = list(config.allowed_hosts)
    if not allowed_hosts and config.host in {"127.0.0.1", "localhost", "::1"}:
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    return TransportSecuritySettings(
        allowed_hosts=allowed_hosts,
        allowed_origins=list(config.allowed_origins),
    )


async def _run_tool(operation: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await operation()
    except PathPolicyError as exc:
        message = "cwd is outside CODEX_BRIDGE_ALLOWED_ROOTS" if "outside" in str(exc) else str(exc)
        raise ToolError(message) from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def create_app(
    config: BridgeConfig,
    *,
    runtime_factory: Callable[[BridgeConfig], RuntimeLike] | None = None,
    shutdown_callback: ShutdownCallback | None = None,
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
        return await _run_tool(lambda: bridge().start(cwd, prompt))

    @mcp.tool()
    async def codex_continue(thread_id: str, prompt: str) -> dict[str, Any]:
        """Continue a native Codex thread, resuming it when needed."""
        return await _run_tool(lambda: bridge().continue_thread(thread_id, prompt))

    @mcp.tool()
    async def codex_wait(
        thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Wait for a bounded Codex turn state change or terminal state."""
        return await _run_tool(lambda: bridge().wait(thread_id, turn_id, timeout_seconds))

    @mcp.tool()
    async def codex_steer(thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        """Send additional input to the expected active Codex turn."""
        return await _run_tool(lambda: bridge().steer(thread_id, turn_id, prompt))

    @mcp.tool()
    async def codex_approval(request_id: int | str, decision: ApprovalDecision) -> dict[str, Any]:
        """Resolve one pending Codex command, file, or permission approval request."""
        return await _run_tool(lambda: bridge().approve(request_id, decision))

    @mcp.tool()
    async def codex_user_input(
        request_id: int | str, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Resolve one pending Codex user-input request by exact question IDs."""
        return await _run_tool(lambda: bridge().answer_user_input(request_id, answers))

    @mcp.tool()
    async def codex_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
        """Request interruption of a running Codex turn."""
        return await _run_tool(lambda: bridge().interrupt(thread_id, turn_id))

    @mcp.tool()
    async def codex_threads(
        thread_id: str | None = None,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List native Codex threads or read one native thread's bounded history."""
        return await _run_tool(
            lambda: bridge().threads(
                thread_id,
                include_history=include_history,
                limit=limit,
                cursor=cursor,
            )
        )

    @mcp.tool()
    async def codex_status(
        thread_id: str, turn_id: str | None = None, activity_limit: int = 20
    ) -> dict[str, Any]:
        """Return the current safe state and recent activities for a native Codex turn."""
        return await _run_tool(lambda: bridge().status(thread_id, turn_id, activity_limit))

    security = _transport_security(config)
    transport_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
        host=config.host,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            runtime: RuntimeLike
            if runtime_factory is None:
                runtime = build_runtime(config, shutdown_callback=shutdown_callback)
            else:
                runtime = runtime_factory(config)
            runtime_holder["runtime"] = runtime
            app.state.runtime = runtime
            app.state.bridge = runtime.bridge
            try:
                await runtime.start()
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

    config = prepare_config(config)
    controller = UvicornShutdownController()
    app = create_app(config, shutdown_callback=controller.request_shutdown)
    server = uvicorn.Server(uvicorn.Config(app, host=config.host, port=config.port))
    controller.bind(server)
    await server.serve()
