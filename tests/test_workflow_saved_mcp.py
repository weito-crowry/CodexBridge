from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession

from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.service import WorkflowService
from tests.test_workflow_definition_mcp import (
    _DEFINITION_READ_TOOLS,
    _DEFINITION_WRITE_TOOLS,
    _WORKFLOW_RUN_TOOLS,
    _source,
)
from tests.test_workflow_definition_mcp import (
    _config as _base_config,
)
from tests.test_workflows_service import FakeWorkerManager

_RUN_START_TOOL = "workflow_run_start"


class FakeRuntime:
    def __init__(self, repository=None, service=None, *, ready: bool = True) -> None:
        self.bridge = SimpleNamespace(app_server_ready=ready)
        if repository is not None:
            self.workflow_definitions = repository
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


def _settings(tmp_path, permissions: tuple[str, ...]) -> BridgeConfig:
    return replace(_base_config(tmp_path, permissions), allowed_roots=(str(tmp_path),))


def _runtime(tmp_path, *, permissions: tuple[str, ...] = ("execute",), ready: bool = True):
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repository = WorkflowDefinitionRepository(state_root)
    manager = FakeWorkerManager(state_root, workspace, allowed_roots=(tmp_path,))
    service = WorkflowService(manager, codex_bridge_port=18001)
    runtime = FakeRuntime(repository, service, ready=ready)
    settings = _settings(tmp_path, permissions)
    app = create_app(settings, runtime_factory=lambda _: runtime)
    return app, runtime, repository, manager, workspace, settings


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ((), set()),
        (("read",), _WORKFLOW_RUN_TOOLS | _DEFINITION_READ_TOOLS),
        (("write",), _DEFINITION_WRITE_TOOLS),
        (("execute",), {_RUN_START_TOOL}),
        (
            ("read", "write", "execute"),
            _WORKFLOW_RUN_TOOLS
            | _DEFINITION_READ_TOOLS
            | _DEFINITION_WRITE_TOOLS
            | {_RUN_START_TOOL},
        ),
    ],
)
def test_saved_run_tool_discovery_keeps_permissions_independent(
    tmp_path, permissions, expected
) -> None:
    app = create_app(_settings(tmp_path, permissions), runtime_factory=lambda _: FakeRuntime())
    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}
    all_workflow_tools = _WORKFLOW_RUN_TOOLS | _DEFINITION_READ_TOOLS | _DEFINITION_WRITE_TOOLS
    assert names & (all_workflow_tools | {_RUN_START_TOOL}) == expected


@pytest.mark.asyncio
async def test_saved_run_tool_uses_shared_service_fixed_revision_and_safe_response(
    tmp_path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, runtime, repository, manager, workspace, _ = _runtime(tmp_path)
    first = repository.create(
        mutation_request_id="mcp-saved-first",
        source_yaml=_source("saved-mcp-one", "private prompt must not return"),
        max_live_codex_jobs=1,
    )
    repository.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="mcp-saved-second",
        source_yaml=_source("saved-mcp-two", "newer private prompt"),
        max_live_codex_jobs=1,
    )
    arguments = {
        "definition_id": first.definition_id,
        "revision": 1,
        "client_request_id": "mcp-saved-run-request-01",
        "workspace_root": str(workspace),
        "input": {"secret": "private input must not return"},
    }

    async with _mcp_client(app) as client:
        catalog = await client.list_tools()
        accepted = await client.call_tool(_RUN_START_TOOL, arguments)
        replayed = await client.call_tool(_RUN_START_TOOL, arguments)

    assert _RUN_START_TOOL in {tool.name for tool in catalog.tools}
    assert accepted.structured_content["definition_id"] == first.definition_id
    assert accepted.structured_content["revision"] == 1
    assert {"workflow_id", "run_id", "status"} <= set(accepted.structured_content)
    assert accepted.structured_content == replayed.structured_content
    assert "private prompt must not return" not in str(accepted.structured_content)
    assert "newer private prompt" not in str(accepted.structured_content)
    assert "private input must not return" not in str(accepted.structured_content)
    assert len(manager.start_calls) == 1
    assert "private prompt must not return" in (
        manager.start_calls[0][0] / "workflow.yaml"
    ).read_text(encoding="utf-8")
    assert runtime.workflow_service is not None


@pytest.mark.asyncio
async def test_saved_run_tool_rechecks_execute_permission_and_readiness(
    tmp_path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, runtime, repository, manager, workspace, settings = _runtime(tmp_path)
    saved = repository.create(
        mutation_request_id="mcp-saved-permission",
        source_yaml=_source("saved-mcp-permission", "private"),
        max_live_codex_jobs=1,
    )
    arguments = {
        "definition_id": saved.definition_id,
        "revision": 1,
        "client_request_id": "mcp-saved-permission-run",
        "workspace_root": str(workspace),
    }
    async with _mcp_client(app) as client:
        object.__setattr__(settings, "workflow_mcp_permissions", ())
        denied = await client.call_tool(_RUN_START_TOOL, arguments)
    assert denied.structured_content == {"error": "workflow_permission_denied"}
    assert manager.start_calls == []

    ready_app, _, ready_repo, ready_manager, ready_workspace, _ = _runtime(
        tmp_path / "not-ready", ready=False
    )
    ready_saved = ready_repo.create(
        mutation_request_id="mcp-saved-not-ready",
        source_yaml=_source("saved-mcp-not-ready", "private"),
        max_live_codex_jobs=1,
    )
    async with _mcp_client(ready_app) as client:
        not_ready = await client.call_tool(
            _RUN_START_TOOL,
            {
                "definition_id": ready_saved.definition_id,
                "revision": 1,
                "client_request_id": "mcp-saved-not-ready-run",
                "workspace_root": str(ready_workspace),
            },
        )
    assert not_ready.structured_content == {"error": "bridge_not_ready"}
    assert ready_manager.start_calls == []


@pytest.mark.asyncio
async def test_saved_run_tool_reports_missing_runtime_components_without_starting(tmp_path) -> None:
    app = create_app(
        _settings(tmp_path, ("execute",)),
        runtime_factory=lambda _: FakeRuntime(),
    )
    async with _mcp_client(app) as client:
        unavailable = await client.call_tool(
            _RUN_START_TOOL,
            {
                "definition_id": "00000000-0000-4000-8000-000000000001",
                "revision": 1,
                "client_request_id": "mcp-saved-missing-runtime",
                "workspace_root": str(tmp_path),
            },
        )
    assert unavailable.structured_content == {"error": "workflow_service_unavailable"}


@pytest.mark.asyncio
async def test_saved_run_tool_rejects_missing_service_or_repository(tmp_path) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    repository = WorkflowDefinitionRepository(state_root)
    manager = FakeWorkerManager(state_root, workspace, allowed_roots=(tmp_path,))
    service = WorkflowService(manager, codex_bridge_port=18001)
    saved = repository.create(
        mutation_request_id="mcp-saved-components",
        source_yaml=_source("saved-mcp-components", "private"),
        max_live_codex_jobs=1,
    )
    arguments = {
        "definition_id": saved.definition_id,
        "revision": 1,
        "client_request_id": "mcp-saved-components-run",
        "workspace_root": str(workspace),
    }
    apps = [
        create_app(
            _settings(tmp_path, ("execute",)),
            runtime_factory=lambda _: FakeRuntime(repository, ready=True),
        ),
        create_app(
            _settings(tmp_path, ("execute",)),
            runtime_factory=lambda _: FakeRuntime(service=service, ready=True),
        ),
    ]
    for app in apps:
        async with _mcp_client(app) as client:
            unavailable = await client.call_tool(_RUN_START_TOOL, arguments)
        assert unavailable.structured_content == {"error": "workflow_service_unavailable"}
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_saved_run_tool_response_is_bounded(tmp_path, monkeypatch) -> None:
    from codex_bridge.workflows import run_mcp as run_mcp_module
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    monkeypatch.setattr(run_mcp_module, "_MAX_RUN_START_RESPONSE_BYTES", 1)
    app, _, repository, manager, workspace, _ = _runtime(tmp_path)
    saved = repository.create(
        mutation_request_id="mcp-saved-response-limit",
        source_yaml=_source("saved-mcp-response-limit", "private prompt"),
        max_live_codex_jobs=1,
    )

    async with _mcp_client(app) as client:
        response = await client.call_tool(
            _RUN_START_TOOL,
            {
                "definition_id": saved.definition_id,
                "revision": 1,
                "client_request_id": "mcp-saved-response-limit-run",
                "workspace_root": str(workspace),
            },
        )
    assert response.structured_content == {"error": "response_too_large"}
    assert len(manager.start_calls) == 1
