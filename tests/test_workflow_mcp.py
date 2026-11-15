from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import anyio
import pytest
from mcp import ClientSession

from codex_bridge.config import BridgeConfig
from codex_bridge.server import create_app
from codex_bridge.workflows.service import (
    WorkflowJobSnapshot,
    WorkflowNotFoundError,
    WorkflowResultSnapshot,
    WorkflowService,
    WorkflowServiceUnavailable,
    WorkflowSnapshot,
)
from tests.test_workflows_api import FakeWorkerManager, _workflow_yaml

_WORKFLOW_TOOLS = {
    "workflow_runs_list",
    "workflow_run_get",
    "workflow_run_result",
}
_WORKFLOW_ID = "03cdbf21-70e2-49f6-8894-161f326297f1"
_OTHER_WORKFLOW_ID = "c260f751-2e4a-4d01-92a5-3d7ff7dd41ba"


def _config(tmp_path, permissions: tuple[str, ...] = ()) -> BridgeConfig:
    return replace(
        BridgeConfig.from_sources(
            explicit_allowed_roots=(str(tmp_path),),
            environ={},
            config_data={},
        ),
        workflow_mcp_permissions=permissions,
    )


def _snapshot(
    workflow_id: str,
    status: str = "running",
    *,
    max_live_codex_jobs: int = 1,
) -> WorkflowSnapshot:
    jobs = (
        (
            WorkflowJobSnapshot("job-one", "nodes.first", "SUCCEEDED", "thread-one", "turn-one"),
            WorkflowJobSnapshot("job-two", "nodes.second", "RUNNING", "thread-two", None),
        )
        if max_live_codex_jobs > 1
        else ()
    )
    return WorkflowSnapshot(
        workflow_id=workflow_id,
        run_id="run-0123456789abcdef0123456789abcdef",
        status=status,
        created_at="2026-11-15T00:00:00Z",
        updated_at="2026-11-15T00:01:00Z",
        worker_state="running",
        terminal=status in {"succeeded", "failed", "failed_to_start"},
        job_id="job-one",
        thread_id="thread-one",
        max_live_codex_jobs=max_live_codex_jobs,
        jobs=jobs,
    )


class FakeWorkflowService:
    def __init__(
        self,
        snapshots: tuple[WorkflowSnapshot, ...] = (),
        results: dict[str, dict[str, Any] | None] | None = None,
    ) -> None:
        self.snapshots = {snapshot.workflow_id: snapshot for snapshot in snapshots}
        self.results = results or {}
        self.list_limits: list[int] = []
        self.get_ids: list[str] = []
        self.result_ids: list[str] = []
        self.failure: Exception | None = None

    async def list_workflows(self, *, limit: int = 50) -> list[WorkflowSnapshot]:
        self.list_limits.append(limit)
        if self.failure is not None:
            raise self.failure
        return list(self.snapshots.values())[:limit]

    async def get_workflow(self, workflow_id: str) -> WorkflowSnapshot:
        self.get_ids.append(workflow_id)
        if self.failure is not None:
            raise self.failure
        if not _valid_workflow_id(workflow_id):
            raise ValueError("invalid workflow id")
        try:
            return self.snapshots[workflow_id]
        except KeyError as exc:
            raise WorkflowNotFoundError("missing") from exc

    async def get_result(self, workflow_id: str) -> WorkflowResultSnapshot:
        self.result_ids.append(workflow_id)
        if self.failure is not None:
            raise self.failure
        if not _valid_workflow_id(workflow_id):
            raise ValueError("invalid workflow id")
        try:
            snapshot = self.snapshots[workflow_id]
        except KeyError as exc:
            raise WorkflowNotFoundError("missing") from exc
        result = self.results.get(workflow_id)
        if result is not None and snapshot.status != "succeeded":
            snapshot = replace(snapshot, status="succeeded", terminal=True)
        return WorkflowResultSnapshot(snapshot, result)


def _valid_workflow_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value.lower()


class FakeRuntime:
    def __init__(self, service: FakeWorkflowService | None = None) -> None:
        self.bridge = SimpleNamespace()
        if service is not None:
            self.workflow_service = service
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


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        ((), set()),
        (("write",), set()),
        (("execute",), set()),
        (("read",), _WORKFLOW_TOOLS),
        (("read", "write", "execute"), _WORKFLOW_TOOLS),
    ],
)
def test_workflow_tools_are_cataloged_only_with_read_permission(tmp_path, permissions, expected):
    settings = _config(tmp_path, permissions)
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime())

    names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}

    assert names & _WORKFLOW_TOOLS == expected
    assert settings.mcp_token is None


@pytest.mark.asyncio
async def test_native_workflow_calls_use_shared_service_and_existing_public_shapes(
    tmp_path,
) -> None:
    single = _snapshot(_WORKFLOW_ID)
    multi = _snapshot(_OTHER_WORKFLOW_ID, max_live_codex_jobs=2)
    verified_result = {
        "workflow_id": _WORKFLOW_ID,
        "run_id": single.run_id,
        "status": "succeeded",
        "final_text": "explicit private result",
    }
    service = FakeWorkflowService((single, multi), {_WORKFLOW_ID: verified_result})
    runtime = FakeRuntime(service)
    settings = _config(tmp_path, ("read",))
    app = create_app(settings, runtime_factory=lambda _: runtime)

    async with _mcp_client(app) as client:
        listed = await client.list_tools()
        assert _WORKFLOW_TOOLS <= {tool.name for tool in listed.tools}
        listing = await client.call_tool("workflow_runs_list", {"limit": 50})
        one = await client.call_tool("workflow_run_get", {"workflow_id": _WORKFLOW_ID})
        two = await client.call_tool("workflow_run_get", {"workflow_id": _OTHER_WORKFLOW_ID})
        result = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})

    assert listing.structured_content == {
        "workflows": [
            {
                "workflow_id": _WORKFLOW_ID,
                "status": "running",
                "created_at": single.created_at,
                "updated_at": single.updated_at,
                "job_id": "job-one",
                "thread_id": "thread-one",
            },
            {
                "workflow_id": _OTHER_WORKFLOW_ID,
                "status": "running",
                "created_at": multi.created_at,
                "updated_at": multi.updated_at,
                "job_id": "job-one",
                "thread_id": "thread-one",
            },
        ],
        "limit": 50,
        "truncated": False,
    }
    assert set(one.structured_content) == {
        "workflow_id",
        "run_id",
        "status",
        "created_at",
        "updated_at",
        "worker_state",
        "terminal",
        "job_id",
        "thread_id",
    }
    assert two.structured_content["max_live_codex_jobs"] == 2
    assert two.structured_content["jobs"] == [
        {
            "job_id": "job-one",
            "task_identity": "nodes.first",
            "state": "SUCCEEDED",
            "thread_id": "thread-one",
            "turn_id": "turn-one",
        },
        {
            "job_id": "job-two",
            "task_identity": "nodes.second",
            "state": "RUNNING",
            "thread_id": "thread-two",
            "turn_id": None,
        },
    ]
    assert result.structured_content == verified_result
    assert "explicit private result" not in str(listing.structured_content)
    assert "prompt" not in str(listing.structured_content).casefold()
    assert "control_token" not in str(one.structured_content)
    assert "mcp_token" not in str(two.structured_content)
    assert service.list_limits == [50]
    assert service.get_ids == [_WORKFLOW_ID, _OTHER_WORKFLOW_ID]
    assert service.result_ids == [_WORKFLOW_ID]
    assert runtime.start_count == 1
    assert runtime.shutdown_count == 1


@pytest.mark.asyncio
async def test_native_workflow_tools_read_temp_service_without_http_or_extra_worker_start(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state" / "workflows"
    manager = FakeWorkerManager(state_root, tmp_path)
    service = WorkflowService(manager, codex_bridge_port=8001)
    started = await service.start_workflow(
        client_request_id="phaseb1-mcp-read-state",
        workflow_yaml=_workflow_yaml(),
        input_data={"value": "test-only"},
        workspace_root=str(workspace),
    )
    assert len(manager.start_calls) == 1

    app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(service),
    )
    async with _mcp_client(app) as client:
        listing = await client.call_tool("workflow_runs_list", {})
        detail = await client.call_tool("workflow_run_get", {"workflow_id": started.workflow_id})
        result = await client.call_tool("workflow_run_result", {"workflow_id": started.workflow_id})

    assert [item["workflow_id"] for item in listing.structured_content["workflows"]] == [
        started.workflow_id
    ]
    assert detail.structured_content["status"] == "running"
    assert result.structured_content == {
        "error": "workflow_not_completed",
        "workflow_id": started.workflow_id,
        "status": "running",
    }
    assert (state_root / "requests.sqlite").is_file()
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_workflow_read_errors_are_structured_and_do_not_echo_exceptions(tmp_path) -> None:
    service = FakeWorkflowService((_snapshot(_WORKFLOW_ID),))
    runtime = FakeRuntime(service)
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: runtime)

    async with _mcp_client(app) as client:
        empty = await client.call_tool("workflow_runs_list", {"limit": 1})
        invalid_limit = await client.call_tool("workflow_runs_list", {"limit": 0})
        invalid_id = await client.call_tool(
            "workflow_run_get", {"workflow_id": "z3cdbf21-70e2-49f6-8894-161f326297f1"}
        )
        missing = await client.call_tool("workflow_run_get", {"workflow_id": _OTHER_WORKFLOW_ID})
        service.failure = WorkflowServiceUnavailable("control_token=never-echo-this")
        unavailable = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})
        service.failure = RuntimeError("mcp_token=never-echo-this-either")
        internal = await client.call_tool("workflow_runs_list", {})

    assert empty.structured_content == {
        "workflows": [
            {
                "workflow_id": _WORKFLOW_ID,
                "status": "running",
                "created_at": "2026-11-15T00:00:00Z",
                "updated_at": "2026-11-15T00:01:00Z",
                "job_id": "job-one",
                "thread_id": "thread-one",
            }
        ],
        "limit": 1,
        "truncated": True,
    }
    assert invalid_limit.structured_content == {"error": "invalid_limit"}
    assert invalid_id.structured_content == {"error": "invalid_workflow_id"}
    assert missing.structured_content == {"error": "workflow_not_found"}
    assert unavailable.structured_content == {"error": "workflow_service_unavailable"}
    assert internal.structured_content == {"error": "internal_error"}
    assert "never-echo-this" not in str(unavailable.structured_content)
    assert "never-echo-this-either" not in str(internal.structured_content)


@pytest.mark.asyncio
async def test_workflow_tools_report_empty_and_unavailable_runtime(tmp_path) -> None:
    empty_app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(FakeWorkflowService()),
    )
    unavailable_app = create_app(
        _config(tmp_path, ("read",)),
        runtime_factory=lambda _: FakeRuntime(),
    )

    async with _mcp_client(empty_app) as client:
        empty = await client.call_tool("workflow_runs_list", {})
    async with _mcp_client(unavailable_app) as client:
        listing = await client.call_tool("workflow_runs_list", {})
        detail = await client.call_tool("workflow_run_get", {"workflow_id": _WORKFLOW_ID})
        result = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})

    assert empty.structured_content == {"workflows": [], "limit": 50, "truncated": False}
    assert listing.structured_content == {"error": "workflow_service_unavailable"}
    assert detail.structured_content == {"error": "workflow_service_unavailable"}
    assert result.structured_content == {"error": "workflow_service_unavailable"}


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        ("queued", "workflow_not_completed"),
        ("running", "workflow_not_completed"),
        ("failed", "workflow_failed"),
        ("failed_to_start", "workflow_failed_to_start"),
        ("needs_reconciliation", "workflow_needs_reconciliation"),
        ("unknown", "workflow_state_unknown"),
    ],
)
@pytest.mark.asyncio
async def test_workflow_result_preserves_non_success_status(
    tmp_path, status: str, expected_error: str
) -> None:
    snapshot = _snapshot(_WORKFLOW_ID, status)
    service = FakeWorkflowService((snapshot,))
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: FakeRuntime(service))

    async with _mcp_client(app) as client:
        response = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})

    assert response.structured_content == {
        "error": expected_error,
        "workflow_id": _WORKFLOW_ID,
        "status": status,
    }


@pytest.mark.asyncio
async def test_workflow_result_enforces_exact_json_utf8_size_limit(tmp_path) -> None:
    snapshot = _snapshot(_WORKFLOW_ID, "succeeded")
    result = {"workflow_id": _WORKFLOW_ID, "status": "succeeded", "final_text": ""}
    fixed_size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    remaining = 64 * 1024 - fixed_size
    result["final_text"] = "あ" * (remaining // len("あ".encode())) + "x" * (
        remaining % len("あ".encode())
    )
    expected_result = dict(result)
    service = FakeWorkflowService(
        (snapshot,),
        {_WORKFLOW_ID: result},
    )
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: FakeRuntime(service))

    async with _mcp_client(app) as client:
        exact = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})
        result["final_text"] += "x"
        oversized = await client.call_tool("workflow_run_result", {"workflow_id": _WORKFLOW_ID})

    assert exact.structured_content == expected_result
    assert oversized.structured_content == {"error": "result_too_large"}


@pytest.mark.asyncio
async def test_general_workflow_responses_are_bounded(tmp_path) -> None:
    oversized = replace(_snapshot(_WORKFLOW_ID), workflow_id="w" * (64 * 1024))
    service = FakeWorkflowService((oversized,))
    app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _: FakeRuntime(service))

    async with _mcp_client(app) as client:
        response = await client.call_tool("workflow_runs_list", {})

    assert response.structured_content == {"error": "response_too_large"}


@pytest.mark.asyncio
async def test_workflow_permission_is_rechecked_by_each_handler(tmp_path) -> None:
    settings = _config(tmp_path, ("read",))
    service = FakeWorkflowService((_snapshot(_WORKFLOW_ID),))
    app = create_app(settings, runtime_factory=lambda _: FakeRuntime(service))

    async with _mcp_client(app) as client:
        object.__setattr__(settings, "workflow_mcp_permissions", ())
        response = await client.call_tool("workflow_runs_list", {})

    assert response.structured_content == {"error": "workflow_permission_denied"}
    assert service.list_limits == []
