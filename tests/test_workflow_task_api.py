from __future__ import annotations

import json
from pathlib import Path

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.service import WorkflowService

_TOKEN = "workflow-task-api-test-token"
_PATH = "/ui-api/control/workflows"
_WORKFLOW = {
    "document": {
        "dsl": "1.0.3",
        "namespace": "codexbridge",
        "name": "task-api-fixture",
        "version": "1.0.0",
    },
    "evaluate": {"language": "jq", "mode": "strict"},
    "do": [
        {"task": {"set": {"value": "${ .value }"}}},
        {"execute": {"call": "codex-job", "with": {"prompt": SAFE_CODEX_JOB_PROMPT}}},
    ],
}


class FakeBridge:
    app_server_ready = True
    app_server_os_pid = 4567


class FakeWorkerManager:
    def __init__(self, state_root: Path, allowed_root: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(allowed_root),)

    def create_run_directory(self) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.state_root / "run-0123456789abcdef0123456789abcdef"
        run_dir.mkdir()
        return run_dir

    async def start_worker(
        self, run_dir: Path, *, mode: str, workspace_root: Path | None = None
    ) -> object:
        assert mode == "start" and workspace_root is not None
        (run_dir / "worker-entry-status.json").write_text(
            '{"state":"running","exit_code":null,"error_code":null}', encoding="utf-8"
        )
        return object()


def _app(tmp_path: Path):
    root = tmp_path / "state" / "workflows"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    service = WorkflowService(FakeWorkerManager(root, tmp_path), codex_bridge_port=18001)
    config = BridgeConfig(
        host="127.0.0.1",
        port=18000,
        ui_port=18001,
        allowed_roots=(str(tmp_path),),
        allowed_hosts=(),
        allowed_origins=(),
        codex_executable="codex",
        wait_default_seconds=18.0,
        wait_max_seconds=30.0,
        shutdown_grace_seconds=3.0,
        control_token=_TOKEN,
    )
    return create_ui_app(FakeBridge(), ActivityStore(), config, workflow_service=service), service


def _transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


@pytest.mark.asyncio
async def test_task_http_routes_read_projection_and_events_with_control_auth(
    tmp_path: Path,
) -> None:
    app, service = _app(tmp_path)
    workspace = tmp_path / "workspace"
    snapshot = await service.start_workflow(
        client_request_id="task-api-request-001",
        workflow_yaml=json.dumps(_WORKFLOW),
        input_data={"value": 1},
        workspace_root=str(workspace),
    )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        tasks = await client.get(
            f"{_PATH}/{snapshot.workflow_id}/tasks",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        events = await client.get(
            f"{_PATH}/{snapshot.workflow_id}/task-events?after_id=0&limit=5",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        denied = await client.get(f"{_PATH}/{snapshot.workflow_id}/tasks")
        denied_events = await client.get(f"{_PATH}/{snapshot.workflow_id}/task-events")
        bad_origin = await client.get(
            f"{_PATH}/{snapshot.workflow_id}/tasks",
            headers={"authorization": f"Bearer {_TOKEN}", "origin": "http://localhost:18002"},
        )
        bad_host = await client.get(
            f"{_PATH}/{snapshot.workflow_id}/task-events",
            headers={"authorization": f"Bearer {_TOKEN}", "host": "evil.example"},
        )
        invalid_id = await client.get(
            f"{_PATH}/not-a-uuid/tasks",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        missing_id = await client.get(
            f"{_PATH}/00000000-0000-4000-8000-000000000001/tasks",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )

    assert tasks.status_code == 200
    assert tasks.json()["workflow_id"] == snapshot.workflow_id
    assert events.status_code == 200
    assert events.json()["events"] == []
    assert denied.status_code == denied_events.status_code == bad_origin.status_code == 403
    assert bad_host.status_code == 400
    assert invalid_id.status_code == 400
    assert missing_id.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_error"),
    [
        ("after_id=-1", "invalid_cursor"),
        ("after_id=%2B1", "invalid_cursor"),
        ("after_id=true", "invalid_cursor"),
        ("after_id=1&after_id=2", "invalid_cursor"),
        ("limit=0", "invalid_limit"),
        ("limit=101", "invalid_limit"),
        ("limit=1&unexpected=1", "invalid_query"),
        ("limit=1&limit=2", "invalid_limit"),
    ],
)
async def test_task_events_rejects_invalid_or_extra_query_parameters(
    tmp_path: Path, query: str, expected_error: str
) -> None:
    app, service = _app(tmp_path)
    workspace = tmp_path / "workspace"
    snapshot = await service.start_workflow(
        client_request_id="task-api-request-001",
        workflow_yaml=json.dumps(_WORKFLOW),
        input_data={"value": 1},
        workspace_root=str(workspace),
    )
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.get(
            f"{_PATH}/{snapshot.workflow_id}/task-events?{query}",
            headers={"authorization": f"Bearer {_TOKEN}"},
        )

    assert response.status_code == 400
    assert response.json() == {"error": expected_error}
