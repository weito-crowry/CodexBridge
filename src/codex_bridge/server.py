from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from importlib.resources import files as package_files
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

from .activity import ActivityStore
from .app_server import AppServerClient
from .bridge import Bridge
from .codex_resolver import CodexResolutionError, resolve_codex_executable
from .config import BridgeConfig, ConfigurationError, validate_allowed_roots
from .execution_targets import ExecutionTargetRouter
from .logging_utils import log_event
from .mcp_remote import RemoteMcpProvider
from .mcp_router import ToolRouter
from .models import ApprovalDecision
from .observability import MCPObservabilityMiddleware, ObservabilityLogger
from .paths import AllowedPathPolicy, PathPolicyError
from .server_instructions import MCP_SERVER_INSTRUCTIONS
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
    explicit_executable = (
        config.codex_executable if config.codex_executable_source == "explicit" else None
    )
    configured_executable = (
        config.codex_executable if config.codex_executable_source == "config" else None
    )
    try:
        resolution = resolve_codex_executable(
            explicit_executable=explicit_executable,
            config_executable=configured_executable,
        )
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
    observability: ObservabilityLogger | None = None,
) -> Starlette:
    apps = Apps()
    app_resource_uri = "ui://codexbridge/setup/app.html"
    setup_html = (
        package_files("codex_bridge")
        .joinpath("assets", "codexbridge_setup_app.html")
        .read_text(encoding="utf-8")
    )
    apps.add_html_resource(
        app_resource_uri,
        setup_html,
        name="codexbridge-setup-app",
        title="CodexBridge Setup",
        description="Choose an execution target, model, and reasoning effort.",
        csp=ResourceCsp(
            connect_domains=[], resource_domains=[], frame_domains=[], base_uri_domains=[]
        ),
    )
    observer = observability or ObservabilityLogger()
    runtime_holder: dict[str, RuntimeLike | None] = {"runtime": None}

    def bridge() -> Bridge:
        runtime = runtime_holder["runtime"]
        if runtime is None:
            raise RuntimeError("CodexBridge runtime is not started")
        return runtime.bridge

    execution_router = ExecutionTargetRouter(config.targets, bridge)

    @apps.tool(
        resource_uri=app_resource_uri,
        title="CodexBridge Setup",
        description="Open the CodexBridge target, model, and reasoning setup form.",
    )
    async def codex_setup(ctx: Context) -> dict[str, Any]:
        """Return fresh targets and the setup UI's initial selection data."""
        target_result = await execution_router.target_list()
        targets = target_result["targets"]
        result: dict[str, Any] = {
            "targets": targets,
            "selection_required": target_result["selection_required"],
        }
        if len(targets) == 1:
            result["capabilities"] = await execution_router.setup_capabilities(targets[0]["id"])
        target_lines = [
            f"- {target['name']} (id: {target['id']}; "
            f"{'Connected' if target['available'] else 'Unavailable'})"
            for target in targets
        ]
        message = "Available execution targets:\n" + "\n".join(target_lines)
        if result["selection_required"]:
            message += "\nChoose an execution target before starting Codex."
        if client_supports_apps(ctx):
            message += (
                "\nUse the CodexBridge Setup UI to select a target, model, and reasoning effort."
            )
        else:
            message += "\nA client with MCP Apps support can show the setup selection UI."
        result["message"] = message
        return result

    @apps.tool(
        resource_uri=app_resource_uri,
        visibility=["app"],
        title="Load target model capabilities",
        description="Load current model and reasoning choices for the selected execution target.",
    )
    async def codex_setup_capabilities(target_id: str) -> dict[str, Any]:
        """Load safe model and reasoning choices for one selected target."""
        return await execution_router.setup_capabilities(target_id)

    @apps.tool(
        resource_uri=app_resource_uri,
        visibility=["app"],
        title="Confirm CodexBridge setup",
        description="Revalidate and confirm the selected target, model, and reasoning effort.",
    )
    async def codex_setup_confirm(
        target_id: str, model: str, reasoning_effort: str
    ) -> dict[str, Any]:
        """Revalidate a setup selection against current target capabilities."""
        return await _run_tool(
            lambda: execution_router.confirm_setup(target_id, model, reasoning_effort)
        )

    mcp = MCPServer(
        "CodexBridge",
        version="0.1.0",
        instructions=MCP_SERVER_INSTRUCTIONS,
        extensions=[apps],
    )

    @mcp.tool()
    async def codex_targets() -> dict[str, Any]:
        """List configured execution machines and their current availability."""
        return await execution_router.target_list()

    @mcp.tool()
    async def mcp_tasks_probe(ctx: Context) -> dict[str, Any]:
        """Report the MCP client protocol and advertised Tasks capabilities."""
        capabilities = ctx.session.client_capabilities
        capabilities_json = (
            capabilities.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            if capabilities is not None
            else None
        )

        extensions = (
            capabilities_json.get("extensions", {}) if capabilities_json is not None else {}
        )

        return {
            "protocol_version": ctx.session.protocol_version,
            "tasks_extension_id": "io.modelcontextprotocol/tasks",
            "tasks_extension_advertised": ("io.modelcontextprotocol/tasks" in extensions),
            "legacy_tasks_capability_advertised": (
                capabilities_json is not None and capabilities_json.get("tasks") is not None
            ),
            "client_capabilities": capabilities_json,
        }

    @mcp.tool()
    async def mcp_long_wait_probe(seconds: int = 30) -> dict[str, Any]:
        """Wait inside one MCP tool call without invoking Codex, then return completion."""
        if seconds < 1 or seconds > 600:
            raise ValueError("seconds must be between 1 and 600")

        loop = asyncio.get_running_loop()
        started_at = loop.time()

        await asyncio.sleep(seconds)

        elapsed_seconds = loop.time() - started_at

        return {
            "status": "completed",
            "requested_seconds": seconds,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "codex_invoked": False,
            "probe": "mcp_long_wait_probe",
        }

    @mcp.tool()
    async def mcp_long_wait_progress_probe(
        seconds: int = 120,
        interval_seconds: int = 20,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Wait while periodically reporting MCP progress, without invoking Codex."""
        if seconds < 1 or seconds > 600:
            raise ValueError("seconds must be between 1 and 600")
        if interval_seconds < 1 or interval_seconds > 60:
            raise ValueError("interval_seconds must be between 1 and 60")
        if ctx is None:
            raise RuntimeError("MCP Context was not injected")

        meta = ctx.request_context.meta
        progress_token = meta.get("progress_token") if isinstance(meta, dict) else None
        progress_token_present = progress_token is not None

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        notifications_attempted = 0

        # 最初の通知も試す。progress token がない場合、SDK側では no-op。
        await ctx.report_progress(
            0.0,
            float(seconds),
            "Long-wait progress probe started",
        )
        notifications_attempted += 1

        elapsed_target = 0

        while elapsed_target < seconds:
            sleep_seconds = min(interval_seconds, seconds - elapsed_target)
            await asyncio.sleep(sleep_seconds)
            elapsed_target += sleep_seconds

            await ctx.report_progress(
                float(elapsed_target),
                float(seconds),
                f"Long-wait progress probe: {elapsed_target}/{seconds}s",
            )
            notifications_attempted += 1

        elapsed_seconds = loop.time() - started_at

        return {
            "status": "completed",
            "probe": "mcp_long_wait_progress_probe",
            "requested_seconds": seconds,
            "interval_seconds": interval_seconds,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "progress_token_present": progress_token_present,
            "progress_token_type": (
                type(progress_token).__name__ if progress_token_present else None
            ),
            "notifications_attempted": notifications_attempted,
            "codex_invoked": False,
        }

    @mcp.tool()
    async def mcp_progress_token_probe(ctx: Context) -> dict[str, Any]:
        """Report safe request and client capability details without metadata values."""
        meta = ctx.request_context.meta
        progress_token = meta.get("progress_token") if isinstance(meta, dict) else None
        client_info = ctx.session.client_info
        capabilities = ctx.session.client_capabilities
        capabilities_json = (
            capabilities.model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            if capabilities is not None
            else None
        )
        locale = meta.get("locale") if isinstance(meta, dict) else None
        timezone = meta.get("timezone") if isinstance(meta, dict) else None
        timezone_offset_minutes = (
            meta.get("timezone_offset_minutes") if isinstance(meta, dict) else None
        )

        return {
            "probe": "mcp_progress_token_probe",
            "progress_token_present": progress_token is not None,
            "progress_token_type": (
                type(progress_token).__name__ if progress_token is not None else None
            ),
            "request_meta_keys": sorted(str(key) for key in meta.keys())
            if isinstance(meta, dict)
            else [],
            "protocol_version": ctx.protocol_version,
            "client_info": (
                {"name": client_info.name, "version": client_info.version}
                if client_info is not None
                else None
            ),
            "client_capabilities": capabilities_json,
            "locale": locale if isinstance(locale, str) else None,
            "timezone": timezone if isinstance(timezone, str) else None,
            "timezone_offset_minutes": (
                timezone_offset_minutes if type(timezone_offset_minutes) is int else None
            ),
            "codex_invoked": False,
        }

    @mcp.tool()
    async def codex_start(
        cwd: str,
        prompt: str,
        target_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """Start a native Codex thread and its first turn without waiting for completion."""
        return await _run_tool(
            lambda: execution_router.codex_start(cwd, prompt, target_id, model, reasoning_effort)
        )

    @mcp.tool()
    async def codex_continue(thread_id: str, prompt: str) -> dict[str, Any]:
        """Start a new turn on an existing Codex thread, resuming the thread when needed."""
        return await _run_tool(lambda: execution_router.codex_continue(thread_id, prompt))

    @mcp.tool()
    async def codex_wait(
        thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        """Long-poll a Codex turn for a bounded duration; return immediately on terminal
        or intervention state, otherwise return the current in_progress snapshot on
        timeout."""
        return await _run_tool(
            lambda: execution_router.codex_wait(thread_id, turn_id, timeout_seconds)
        )

    @mcp.tool()
    async def codex_steer(thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        """Send additional input to the expected active Codex turn."""
        return await _run_tool(lambda: execution_router.codex_steer(thread_id, turn_id, prompt))

    @mcp.tool()
    async def codex_approval(request_id: int | str, decision: ApprovalDecision) -> dict[str, Any]:
        """Resolve one pending Codex command, file, or permission approval request."""
        return await _run_tool(lambda: execution_router.codex_approval(request_id, decision))

    @mcp.tool()
    async def codex_user_input(
        request_id: int | str, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Resolve one pending Codex user-input request by exact question IDs."""
        return await _run_tool(lambda: execution_router.codex_user_input(request_id, answers))

    @mcp.tool()
    async def codex_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
        """Request interruption of a running Codex turn."""
        return await _run_tool(lambda: execution_router.codex_interrupt(thread_id, turn_id))

    @mcp.tool()
    async def codex_threads(
        thread_id: str | None = None,
        target_id: str | None = None,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List native Codex threads or read one native thread's bounded history."""
        return await _run_tool(
            lambda: execution_router.codex_threads(
                thread_id,
                target_id=target_id,
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
        return await _run_tool(
            lambda: execution_router.codex_status(thread_id, turn_id, activity_limit)
        )

    remote_provider = RemoteMcpProvider(config.github_mcp)
    router = ToolRouter(mcp, remote_provider)
    wire_server = LowLevelServer(
        "CodexBridge",
        version="0.1.0",
        instructions=MCP_SERVER_INSTRUCTIONS,
        on_list_tools=router.list_tools,
        on_call_tool=router.call_tool,
        on_list_resources=mcp._handle_list_resources,
        on_read_resource=mcp._handle_read_resource,
    )
    wire_server.extensions.update(mcp._lowlevel_server.extensions)

    security = _transport_security(config)
    transport_app = wire_server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=security,
        host=config.host,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        observer.server_start()
        try:
            async with wire_server.session_manager.run():
                runtime: RuntimeLike
                if runtime_factory is None:
                    runtime = build_runtime(config, shutdown_callback=shutdown_callback)
                else:
                    runtime = runtime_factory(config)
                runtime_holder["runtime"] = runtime
                app.state.runtime = runtime
                app.state.bridge = runtime.bridge
                runtime_started = False
                try:
                    await router.start()
                    await runtime.start()
                    runtime_started = True
                    await execution_router.start()
                    yield
                finally:
                    await execution_router.close()
                    if runtime_started:
                        await runtime.shutdown()
                    await router.shutdown()
                    runtime_holder["runtime"] = None
                    app.state.runtime = None
                    app.state.bridge = None
        finally:
            observer.server_shutdown()

    app = Starlette(
        routes=[Mount("/", app=transport_app)],
        lifespan=lifespan,
        middleware=[Middleware(MCPObservabilityMiddleware, observer=observer)],
    )
    app.state.mcp_server = mcp
    app.state.mcp_lowlevel_server = wire_server
    app.state.mcp_router = router
    app.state.execution_target_router = execution_router
    app.state.transport_security = security
    app.state.observability = observer
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
