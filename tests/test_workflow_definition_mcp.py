from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import anyio
import httpx2 as httpx
import pytest
from mcp import ClientSession

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from tests.test_workflows_api import _workflow_yaml

_DEFINITION_READ_TOOLS = {
    "workflow_definitions_list",
    "workflow_definition_get",
    "workflow_graph_get",
}
_DEFINITION_WRITE_TOOLS = {
    "workflow_definition_validate",
    "workflow_definition_create",
    "workflow_definition_update",
}
_WORKFLOW_RUN_TOOLS = {
    "workflow_runs_list",
    "workflow_run_get",
    "workflow_run_result",
}
_HTTP_BASE = "/ui-api/control/workflow-definitions"
_TOKEN = "definition-mcp-test-token"


def _config(tmp_path, permissions: tuple[str, ...] = ()) -> BridgeConfig:
    return replace(
        BridgeConfig.from_sources(
            explicit_allowed_roots=(str(tmp_path),),
            environ={},
            config_data={},
        ),
        workflow_mcp_permissions=permissions,
        ui_port=18001,
        control_token=_TOKEN,
    )


class FakeRuntime:
    def __init__(self, repository: WorkflowDefinitionRepository | None = None) -> None:
        self.bridge = SimpleNamespace()
        if repository is not None:
            self.workflow_definitions = repository
        self.start_count = 0
        self.shutdown_count = 0

    async def start(self) -> None:
        self.start_count += 1

    async def shutdown(self) -> None:
        self.shutdown_count += 1


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


def _source(name: str, prompt: str) -> str:
    return _workflow_yaml(prompt).replace("fixed-codex-job", name)


def _http_transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


@pytest.mark.parametrize(
    ("permissions", "expected_definitions", "expected_runs"),
    [
        ((), set(), set()),
        (("execute",), set(), set()),
        (("read",), _DEFINITION_READ_TOOLS, _WORKFLOW_RUN_TOOLS),
        (("write",), _DEFINITION_WRITE_TOOLS, set()),
        (
            ("read", "write", "execute"),
            _DEFINITION_READ_TOOLS | _DEFINITION_WRITE_TOOLS,
            _WORKFLOW_RUN_TOOLS,
        ),
    ],
)
def test_definition_tools_catalog_respects_read_and_write_permissions(
    tmp_path, permissions, expected_definitions, expected_runs
) -> None:
    app = create_app(
        _config(tmp_path, permissions),
        runtime_factory=lambda _: FakeRuntime(),
    )
    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}

    assert names & (_DEFINITION_READ_TOOLS | _DEFINITION_WRITE_TOOLS) == expected_definitions
    assert names & _WORKFLOW_RUN_TOOLS == expected_runs


@pytest.mark.asyncio
async def test_http_and_mcp_share_one_repository_for_create_get_update_history_and_graph(
    tmp_path,
) -> None:
    state_root = tmp_path / "state" / "workflows"
    repository = WorkflowDefinitionRepository(state_root)
    settings = _config(tmp_path, ("read", "write"))
    runtime = FakeRuntime(repository)
    mcp_app = create_app(settings, runtime_factory=lambda _: runtime)
    ui_app = create_ui_app(
        SimpleNamespace(app_server_ready=True),
        ActivityStore(),
        settings,
        workflow_definitions=repository,
    )
    source_one = _source("shared-definition", "private-shared-prompt-one")
    source_two = _source("shared-definition-updated", "private-shared-prompt-two")

    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as client:
        created = await client.post(
            _HTTP_BASE,
            headers={"authorization": f"Bearer {_TOKEN}"},
            json={
                "mutation_request_id": "shared-http-create",
                "source_yaml": source_one,
                "max_live_codex_jobs": 1,
            },
        )
    assert created.status_code == 201
    definition_id = created.json()["definition_id"]

    async with _mcp_client(mcp_app) as client:
        catalog = await client.list_tools()
        assert _WORKFLOW_RUN_TOOLS | _DEFINITION_READ_TOOLS | _DEFINITION_WRITE_TOOLS <= {
            tool.name for tool in catalog.tools
        }
        fetched = await client.call_tool(
            "workflow_definition_get", {"definition_id": definition_id}
        )
        listing = await client.call_tool("workflow_definitions_list", {})
        validated = await client.call_tool(
            "workflow_definition_validate",
            {"source_yaml": source_two, "max_live_codex_jobs": 1},
        )
        projected = await client.call_tool(
            "workflow_graph_get", {"definition_id": definition_id, "revision": 1}
        )
        mcp_created = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "shared-mcp-create",
                "source_yaml": _source("created-through-mcp", "private-mcp-create-prompt"),
                "max_live_codex_jobs": 1,
            },
        )
        updated = await client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": definition_id,
                "expected_revision": 1,
                "mutation_request_id": "shared-mcp-update",
                "source_yaml": source_two,
                "max_live_codex_jobs": 1,
            },
        )
        assert fetched.structured_content["source_yaml"] == source_one
        assert "private-shared-prompt-one" not in str(listing.structured_content)
        assert "source_yaml" not in listing.structured_content["definitions"][0]
        assert validated.structured_content["valid"] is True
        assert "source_yaml" not in validated.structured_content
        assert "private-shared-prompt-two" not in str(validated.structured_content)
        assert "private-shared-prompt-one" in str(projected.structured_content)
        assert mcp_created.structured_content["revision"] == 1
        assert "source_yaml" not in mcp_created.structured_content
        assert "private-mcp-create-prompt" not in str(mcp_created.structured_content)
        assert updated.structured_content["revision"] == 2
        assert "source_yaml" not in updated.structured_content

    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as client:
        history = await client.get(
            f"{_HTTP_BASE}/{definition_id}/revisions/2",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        mcp_created_detail = await client.get(
            f"{_HTTP_BASE}/{mcp_created.structured_content['definition_id']}",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        graph = await client.get(
            f"{_HTTP_BASE}/{definition_id}/graph?revision=2",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )

    assert history.status_code == graph.status_code == mcp_created_detail.status_code == 200
    assert history.json()["source_yaml"] == source_two
    assert mcp_created_detail.json()["revision"] == 1
    assert graph.json()["revision"] == 2
    assert "private-shared-prompt-two" in graph.text
    assert not (state_root / "requests.sqlite").exists()
    assert runtime.start_count == runtime.shutdown_count == 1


@pytest.mark.asyncio
async def test_definition_mcp_errors_are_classified_and_handlers_recheck_permissions(
    tmp_path,
) -> None:
    state_root = tmp_path / "state"
    repository = WorkflowDefinitionRepository(state_root)
    settings = _config(tmp_path, ("read", "write"))
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime(repository))
    object.__setattr__(settings, "workflow_mcp_permissions", ("read",))

    async with _mcp_client(app) as client:
        permission_denied = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "permission-denied",
                "source_yaml": _source("permission", "secret"),
                "max_live_codex_jobs": 1,
            },
        )
        invalid = await client.call_tool("workflow_definition_get", {"definition_id": "not-a-uuid"})
        missing = await client.call_tool(
            "workflow_definition_get",
            {"definition_id": "00000000-0000-4000-8000-000000000001"},
        )

    assert permission_denied.structured_content == {"error": "workflow_permission_denied"}
    assert invalid.structured_content == {"error": "workflow_definition_invalid"}
    assert missing.structured_content == {"error": "workflow_definition_not_found"}
    assert not (state_root / "definitions.sqlite").exists()


@pytest.mark.asyncio
async def test_definition_mcp_reports_unavailable_and_enforces_response_limit(
    tmp_path, monkeypatch
) -> None:
    import codex_bridge.workflows.definition_mcp as mcp_module

    unavailable_app = create_app(
        _config(tmp_path, ("read", "write")), runtime_factory=lambda _: FakeRuntime()
    )
    async with _mcp_client(unavailable_app) as client:
        unavailable = await client.call_tool("workflow_definitions_list", {})
    assert unavailable.structured_content == {"error": "workflow_definition_service_unavailable"}

    repository = WorkflowDefinitionRepository(tmp_path / "saved")
    created = repository.create(
        mutation_request_id="mcp-size-create",
        source_yaml=_source("size-check", "bounded-private-prompt"),
        max_live_codex_jobs=1,
    )
    app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(repository),
    )
    monkeypatch.setattr(mcp_module, "_MAX_DEFINITION_RESPONSE_BYTES", 1)
    async with _mcp_client(app) as client:
        oversized = await client.call_tool(
            "workflow_definition_get", {"definition_id": created.definition_id}
        )
    assert oversized.structured_content == {"error": "response_too_large"}


@pytest.mark.asyncio
async def test_definition_mcp_read_only_database_corruption_is_safe(tmp_path) -> None:
    root = tmp_path / "corrupt"
    root.mkdir()
    database = root / "definitions.sqlite"
    database.write_bytes(b"not sqlite")
    app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(WorkflowDefinitionRepository(root)),
    )
    async with _mcp_client(app) as client:
        result = await client.call_tool("workflow_definitions_list", {})

    assert result.structured_content == {"error": "workflow_definition_service_unavailable"}
    assert database.read_bytes() == b"not sqlite"
