from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from mcp import ClientSession

from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app

_TASK_TOOLS = {"workflow_run_tasks", "workflow_run_events"}


def _config(tmp_path, permissions: tuple[str, ...] = ()) -> BridgeConfig:
    return replace(
        BridgeConfig.from_sources(
            explicit_allowed_roots=(str(tmp_path),),
            environ={},
            config_data={},
        ),
        workflow_mcp_permissions=permissions,
    )


class FakeTaskService:
    def __init__(self) -> None:
        self.task_ids: list[str] = []
        self.event_calls: list[tuple[str, int, int]] = []
        self.task_response: dict[str, Any] | None = None

    async def get_workflow_tasks(self, workflow_id: str) -> dict[str, Any]:
        self.task_ids.append(workflow_id)
        if self.task_response is not None:
            return self.task_response
        return {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "run_id": "run-fixture",
            "workflow_status": "running",
            "coverage": "complete",
            "latest_event_id": 0,
            "snapshot_version": "sha256:fixture",
            "truncated": False,
            "nodes": [],
            "edges": [],
        }

    async def get_workflow_task_events(
        self, workflow_id: str, *, after_id: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        self.event_calls.append((workflow_id, after_id, limit))
        return {"workflow_id": workflow_id, "events": [], "next_after_id": after_id}


class FakeRuntime:
    def __init__(self, service: FakeTaskService | None = None) -> None:
        self.bridge = SimpleNamespace()
        if service is not None:
            self.workflow_service = service

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


@asynccontextmanager
async def _mcp_client(app) -> AsyncIterator[ClientSession]:
    async with app.router.lifespan_context(app):
        lowlevel = app.state.mcp_lowlevel_server
        client_send, client_receive = anyio.create_memory_object_stream(0)
        server_send, server_receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                lowlevel.run,
                client_receive,
                server_send,
                lowlevel.create_initialization_options(),
            )
            try:
                async with ClientSession(server_receive, client_send) as client:
                    await client.initialize()
                    yield client
            finally:
                task_group.cancel_scope.cancel()


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ((), set()),
        (("write",), set()),
        (("execute",), set()),
        (("read",), _TASK_TOOLS),
        (("read", "write", "execute"), _TASK_TOOLS),
    ],
)
def test_task_mcp_tools_are_registered_only_with_read_permission(tmp_path, permissions, expected):
    app = create_app(_config(tmp_path, permissions), runtime_factory=lambda _: FakeRuntime())
    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}

    assert names & _TASK_TOOLS == expected


@pytest.mark.asyncio
async def test_task_mcp_calls_shared_service_without_starting_workflow(tmp_path) -> None:
    service = FakeTaskService()
    runtime = FakeRuntime(service)
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: runtime)

    async with _mcp_client(app) as client:
        tasks = await client.call_tool("workflow_run_tasks", {"workflow_id": "wf-fixture"})
        events = await client.call_tool(
            "workflow_run_events",
            {"workflow_id": "wf-fixture", "after_id": 7, "limit": 3},
        )

    assert tasks.structured_content["workflow_id"] == "wf-fixture"
    assert events.structured_content["next_after_id"] == 7
    assert service.task_ids == ["wf-fixture"]
    assert service.event_calls == [("wf-fixture", 7, 3)]


@pytest.mark.asyncio
async def test_task_mcp_returns_safe_error_when_runtime_service_is_missing(tmp_path) -> None:
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: FakeRuntime())

    async with _mcp_client(app) as client:
        result = await client.call_tool("workflow_run_tasks", {"workflow_id": "wf-fixture"})

    assert result.structured_content == {"error": "workflow_service_unavailable"}


@pytest.mark.asyncio
async def test_task_mcp_rejects_oversized_service_response(tmp_path) -> None:
    service = FakeTaskService()
    service.task_response = {"payload": "x" * (256 * 1024 + 1)}
    app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(service),
    )

    async with _mcp_client(app) as client:
        result = await client.call_tool("workflow_run_tasks", {"workflow_id": "wf-fixture"})

    assert result.structured_content == {"error": "response_too_large"}
