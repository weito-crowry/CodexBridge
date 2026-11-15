from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.manager import WorkflowManagerError
from codex_bridge.workflows.service import WorkflowService
from tests.test_workflows_api import _two_job_workflow_yaml, _workflow_yaml
from tests.test_workflows_multi_api import _THREE_JOB_INPUT, _three_job_yaml
from tests.test_workflows_service import FakeWorkerManager

_TOKEN = "workflow-saved-api-token"
_BASE = "/ui-api/control/workflow-definitions"
_INLINE_RUNS = "/ui-api/control/workflows"


class FakeBridge:
    def __init__(self, ready: bool = True) -> None:
        self.app_server_ready = ready


def _settings(tmp_path: Path) -> BridgeConfig:
    return replace(
        BridgeConfig.from_sources(
            explicit_allowed_roots=(str(tmp_path),),
            environ={},
            config_data={},
        ),
        ui_port=18001,
        control_token=_TOKEN,
    )


def _app(tmp_path: Path, *, ready: bool = True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state" / "workflows"
    repository = WorkflowDefinitionRepository(state_root)
    manager = FakeWorkerManager(state_root, workspace, allowed_roots=(tmp_path,))
    service = WorkflowService(manager, codex_bridge_port=18001)
    app = create_ui_app(
        FakeBridge(ready),
        ActivityStore(),
        _settings(tmp_path),
        workflow_service=service,
        workflow_definitions=repository,
    )
    return app, repository, manager, workspace


def _transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


def _headers(**values: str) -> dict[str, str]:
    return {"authorization": f"Bearer {_TOKEN}", **values}


def _save(repository, *, source: str, profile: int = 1, mutation: str = "saved-api-create"):
    return repository.create(
        mutation_request_id=mutation,
        source_yaml=source,
        max_live_codex_jobs=profile,
    )


def _run_payload(workspace: Path, **values: object) -> dict[str, object]:
    return {
        "client_request_id": "saved-api-request-0001",
        "revision": 1,
        "workspace_root": str(workspace),
        **values,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "profile", "input_data"),
    [
        (_workflow_yaml("saved single prompt"), 1, {"answer": 4}),
        (_two_job_workflow_yaml(), 2, {"answer": 4}),
        (_three_job_yaml(), 3, _THREE_JOB_INPUT),
    ],
)
async def test_http_saved_execution_uses_the_selected_revision_profile_and_input(
    tmp_path: Path, monkeypatch, source: str, profile: int, input_data: object
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    saved = _save(repository, source=source, profile=profile)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace, input=input_data),
        )

    assert response.status_code == 202
    body = response.json()
    assert body["definition_id"] == saved.definition_id
    assert body["revision"] == saved.revision
    assert {"workflow_id", "run_id", "status"} <= set(body)
    assert manager.start_calls[0][2] == profile
    run_dir = manager.start_calls[0][0]
    saved_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved_manifest["definition_sha256"] == saved.canonical_sha256
    assert saved_manifest["definition_sha256"] != saved.source_sha256
    assert "saved single prompt" not in response.text


@pytest.mark.asyncio
async def test_http_saved_execution_can_start_an_older_fixed_revision(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    first_source = _workflow_yaml("revision one prompt")
    saved = _save(repository, source=first_source)
    repository.update(
        definition_id=saved.definition_id,
        expected_revision=1,
        mutation_request_id="saved-api-update-revision-two",
        source_yaml=_workflow_yaml("revision two prompt"),
        max_live_codex_jobs=1,
    )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace),
        )

    assert response.status_code == 202
    assert response.json()["revision"] == 1
    assert "revision one prompt" in (manager.start_calls[0][0] / "workflow.yaml").read_text(
        encoding="utf-8"
    )
    assert "revision two prompt" not in response.text


@pytest.mark.asyncio
async def test_http_saved_and_inline_routes_conflict_for_the_same_request_id(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    source = _workflow_yaml("cross-route private prompt")
    saved = _save(repository, source=source)
    workspace_inline = tmp_path / "workspace-inline"
    workspace_inline.mkdir()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        saved_first = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace),
        )
        inline_after_saved = await client.post(
            _INLINE_RUNS,
            headers=_headers(),
            json={
                "client_request_id": "saved-api-request-0001",
                "workflow_yaml": source,
                "input": {},
                "workspace_root": str(workspace),
            },
        )
        inline_first = await client.post(
            _INLINE_RUNS,
            headers=_headers(),
            json={
                "client_request_id": "saved-api-request-0003",
                "workflow_yaml": source,
                "input": {},
                "workspace_root": str(workspace_inline),
            },
        )
        saved_after_inline = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json={
                **_run_payload(workspace_inline),
                "client_request_id": "saved-api-request-0003",
            },
        )

    assert saved_first.status_code == inline_first.status_code == 202
    assert inline_after_saved.status_code == saved_after_inline.status_code == 409
    assert (
        inline_after_saved.json() == saved_after_inline.json() == {"error": "request_id_conflict"}
    )
    assert len(manager.start_calls) == 2
    assert "cross-route private prompt" not in inline_after_saved.text + saved_after_inline.text


@pytest.mark.asyncio
async def test_http_saved_execution_rejects_bad_identity_fields_and_missing_revision(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    saved = _save(repository, source=_workflow_yaml())
    valid = _run_payload(workspace)
    invalid_bodies = [
        {key: value for key, value in valid.items() if key != "revision"},
        {**valid, "revision": True},
        {**valid, "revision": "1"},
        {**valid, "revision": 0},
        {**valid, "workflow_yaml": _workflow_yaml()},
        {**valid, "max_live_codex_jobs": 3},
        {key: value for key, value in valid.items() if key != "client_request_id"},
        {key: value for key, value in valid.items() if key != "workspace_root"},
    ]

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        for body in invalid_bodies:
            response = await client.post(
                f"{_BASE}/{saved.definition_id}/runs", headers=_headers(), json=body
            )
            assert response.status_code == 400
        invalid_id = await client.post(f"{_BASE}/not-a-uuid/runs", headers=_headers(), json=valid)
        missing_definition = await client.post(
            f"{_BASE}/00000000-0000-4000-8000-000000000009/runs",
            headers=_headers(),
            json=valid,
        )
        missing_revision = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json={**valid, "revision": 2},
        )

    assert invalid_id.status_code == 400
    assert missing_definition.status_code == missing_revision.status_code == 404
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_http_saved_execution_preserves_json_limits_auth_and_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path, ready=False)
    saved = _save(repository, source=_workflow_yaml("secret source prompt"))

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        not_ready = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace),
        )
        oversized = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(**{"content-type": "application/json"}),
            content=json.dumps({**_run_payload(workspace), "padding": "x" * 131072}),
        )
        duplicate_key = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(**{"content-type": "application/json"}),
            content=(
                '{"client_request_id":"saved-api-request-0001",'
                '"client_request_id":"saved-api-request-0002","revision":1,'
                f'"workspace_root":{json.dumps(str(workspace))}}}'
            ),
        )

    assert not_ready.status_code == 503
    assert not_ready.json() == {"error": "bridge_not_ready"}
    assert oversized.status_code == 413
    assert duplicate_key.status_code == 400
    assert manager.start_calls == []
    assert "secret source prompt" not in not_ready.text + oversized.text + duplicate_key.text


@pytest.mark.asyncio
async def test_http_saved_execution_rejects_three_job_input_shape_before_worker_start(
    tmp_path: Path, monkeypatch
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    saved = _save(repository, source=_three_job_yaml(), profile=3)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace, input={"issues": [{"number": 2}]}),
        )

    assert response.status_code == 400
    assert manager.start_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_error", "status_code", "error"),
    [
        (WorkflowManagerError("worker unavailable"), 503, "workflow_worker_not_ready"),
        (RuntimeError("private worker detail"), 409, "workflow_needs_reconciliation"),
    ],
)
async def test_http_saved_execution_preserves_worker_failure_classification(
    tmp_path: Path, monkeypatch, start_error, status_code: int, error: str
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, repository, manager, workspace = _app(tmp_path)
    manager.start_error = start_error
    saved = _save(repository, source=_workflow_yaml("secret saved prompt"))

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            f"{_BASE}/{saved.definition_id}/runs",
            headers=_headers(),
            json=_run_payload(workspace),
        )

    assert response.status_code == status_code
    assert response.json()["error"] == error
    assert response.json()["definition_id"] == saved.definition_id
    assert response.json()["revision"] == 1
    assert len(manager.start_calls) == 1
    assert "secret saved prompt" not in response.text
    assert "private worker detail" not in response.text
