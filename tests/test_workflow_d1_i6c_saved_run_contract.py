from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.server import create_app
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.api import _list_item_json, _snapshot_json
from codex_bridge.workflows.definitions import DefinitionRepositoryUnavailable
from codex_bridge.workflows.service import (
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowServiceUnavailable,
    _find_request,
)
from codex_bridge.workflows.strict_definition_repository import StrictWorkflowDefinitionRepository
from tests.test_workflow_d1_i5b2_reader_gate import (
    _SECRET_SENTINEL,
    _ReadOnlyWorkerManager,
    _strict_fixture,
)
from tests.test_workflow_saved_api import (
    _BASE as _HTTP_DEFINITIONS,
)
from tests.test_workflow_saved_api import (
    FakeBridge as HttpFakeBridge,
)
from tests.test_workflow_saved_api import (
    _headers as _http_headers,
)
from tests.test_workflow_saved_api import (
    _settings as _http_settings,
)
from tests.test_workflow_saved_api import (
    _transport as _http_transport,
)
from tests.test_workflow_saved_mcp import (
    FakeRuntime,
    _mcp_client,
    _runtime,
    _settings,
    _source,
)


def _service(fixture) -> WorkflowService:
    return WorkflowService(
        _ReadOnlyWorkerManager(fixture.state_root, fixture.workspace),
        codex_bridge_port=18001,
    )


def _saved_run_arguments(
    fixture, *, client_request_id: str, input_data: object
) -> dict[str, object]:
    return {
        "repository": StrictWorkflowDefinitionRepository(fixture.state_root),
        "definition_id": fixture.manifest["definition_id"],
        "revision": fixture.manifest["definition_revision"],
        "client_request_id": client_request_id,
        "input_data": input_data,
        "workspace_root": str(fixture.workspace.resolve()),
    }


@pytest.mark.asyncio
async def test_verified_strict_saved_run_replay_returns_reconciliation_metadata(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    service = _service(fixture)
    request_id = "i5b2-request-create-0001"
    input_data = {"secret": _SECRET_SENTINEL}

    with pytest.raises(WorkflowReconciliationRequired) as caught:
        await service.start_saved_workflow(
            **_saved_run_arguments(
                fixture,
                client_request_id=request_id,
                input_data=input_data,
            )
        )

    snapshot = caught.value.snapshot
    assert snapshot.workflow_id == fixture.manifest["workflow_id"]
    assert snapshot.run_id == fixture.manifest["run_id"]
    assert snapshot.status == "needs_reconciliation"
    assert snapshot.worker_state == "unknown"
    assert snapshot.terminal is False
    public = _snapshot_json(snapshot)
    assert public["run_format_version"] == 2
    assert public["contract_version"] == 1
    assert public["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert public["binding_digest"] == fixture.manifest["binding_digest"]
    assert public["reason_code"] == "strict_execution_uncertain"


@pytest.mark.asyncio
async def test_strict_status_result_and_task_projections_use_verified_metadata(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    service = _service(fixture)
    workflow_id = fixture.manifest["workflow_id"]

    status = await service.get_workflow(workflow_id)
    result = await service.get_result(workflow_id)
    tasks = await service.get_workflow_tasks(workflow_id)
    events = await service.get_workflow_task_events(workflow_id, after_id=11, limit=10)

    assert result.result is None
    assert _snapshot_json(status)["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert _list_item_json(status)["binding_digest"] == fixture.manifest["binding_digest"]
    assert tasks["contract_version"] == 1
    assert tasks["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert tasks["reason_code"] == "strict_execution_uncertain"
    assert events["binding_digest"] == fixture.manifest["binding_digest"]
    assert events["reason_code"] == "strict_execution_uncertain"
    assert events["events"] == []
    rendered = json.dumps((_snapshot_json(status), tasks, events))
    assert _SECRET_SENTINEL not in rendered
    assert "prompt" not in rendered


@pytest.mark.asyncio
async def test_strict_saved_run_rejects_null_input_and_keeps_new_request_unreserved(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    service = _service(fixture)
    before_requests = (fixture.state_root / "requests.sqlite").read_bytes()
    before_manifest = (fixture.run_dir / "manifest.json").read_bytes()
    with pytest.raises(WorkflowRequestError) as caught:
        await service.start_saved_workflow(
            **_saved_run_arguments(
                fixture,
                client_request_id="i6c-new-strict-request-0001",
                input_data=None,
            )
        )
    assert getattr(caught.value, "error_code", None) == "workflow_input_invalid"
    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            **_saved_run_arguments(
                fixture,
                client_request_id="i6c-new-strict-request-0002",
                input_data={"secret": _SECRET_SENTINEL},
            )
        )

    assert (fixture.state_root / "requests.sqlite").read_bytes() == before_requests
    assert (fixture.run_dir / "manifest.json").read_bytes() == before_manifest


@pytest.mark.asyncio
async def test_invalid_reserved_strict_replay_keeps_ids_without_unverified_hashes(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    with sqlite3.connect(fixture.state_root / "requests.sqlite") as connection:
        connection.execute(
            "DELETE FROM workflow_request_contract_bindings WHERE run_id = ?",
            (fixture.manifest["run_id"],),
        )
    service = _service(fixture)

    with pytest.raises(WorkflowReconciliationRequired) as caught:
        await service.start_saved_workflow(
            **_saved_run_arguments(
                fixture,
                client_request_id="i5b2-request-create-0001",
                input_data={"secret": _SECRET_SENTINEL},
            )
        )

    public = _snapshot_json(caught.value.snapshot)
    assert public["workflow_id"] == fixture.manifest["workflow_id"]
    assert public["run_id"] == fixture.manifest["run_id"]
    assert public["status"] == "needs_reconciliation"
    assert public["reason_code"] == "run_integrity_invalid"
    assert "contract_sha256" not in public
    assert "binding_digest" not in public
    with sqlite3.connect(fixture.state_root / "requests.sqlite") as connection:
        state = connection.execute(
            "SELECT state FROM workflow_requests WHERE run_id = ?",
            (fixture.manifest["run_id"],),
        ).fetchone()[0]
    assert state == "queued"
    row = _find_request(fixture.state_root, "i5b2-request-create-0001")
    assert row is not None
    forced = await service._snapshot_for_row(row, forced_state="failed_to_start")
    assert forced.status == "needs_reconciliation"
    assert forced._strict_metadata is not None
    assert forced._strict_metadata.contract_sha256 is None


@pytest.mark.asyncio
async def test_strict_request_replay_uses_normalized_default_input_identity(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path, input_default="fast")
    service = _service(fixture)
    repository = StrictWorkflowDefinitionRepository(fixture.state_root)
    base = {
        "repository": repository,
        "definition_id": fixture.manifest["definition_id"],
        "revision": fixture.manifest["definition_revision"],
        "client_request_id": "i5b2-request-create-0001",
        "workspace_root": str(fixture.workspace.resolve()),
    }

    with pytest.raises(WorkflowReconciliationRequired) as omitted:
        await service.start_saved_workflow(**base, input_data={"secret": _SECRET_SENTINEL})
    with pytest.raises(WorkflowReconciliationRequired) as explicit_default:
        await service.start_saved_workflow(
            **base, input_data={"secret": _SECRET_SENTINEL, "mode": "fast"}
        )
    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            **base, input_data={"secret": _SECRET_SENTINEL, "mode": "slow"}
        )

    assert omitted.value.snapshot.run_id == explicit_default.value.snapshot.run_id
    assert (
        omitted.value.snapshot._strict_metadata == explicit_default.value.snapshot._strict_metadata
    )


@pytest.mark.asyncio
async def test_strict_request_id_conflicts_across_legacy_mode_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    fixture = _strict_fixture(tmp_path)
    service = _service(fixture)
    repository = StrictWorkflowDefinitionRepository(fixture.state_root)
    with pytest.raises(WorkflowReconciliationRequired):
        await service.start_saved_workflow(
            **_saved_run_arguments(
                fixture,
                client_request_id="i5b2-request-create-0001",
                input_data={"secret": _SECRET_SENTINEL},
            )
        )

    from tests.test_workflows_service import _workflow_yaml

    with pytest.raises(WorkflowRequestConflict):
        await service.start_workflow(
            client_request_id="i5b2-request-create-0001",
            workflow_yaml=_workflow_yaml("legacy mode conflict"),
            input_data={"secret": _SECRET_SENTINEL},
            workspace_root=str(fixture.workspace.resolve()),
        )
    assert not (fixture.run_dir / "bridge-lifecycle.jsonl").exists()
    saved = repository.get(fixture.manifest["definition_id"], 1)
    repository.update(
        definition_id=saved.definition_id,
        expected_revision=1,
        mutation_request_id="i6c-revision-update-0001",
        source_yaml=_source("i6c-revision-two", "different pinned revision"),
        max_live_codex_jobs=saved.max_live_codex_jobs,
        contract=saved.contract,
    )
    assert repository.get(saved.definition_id).revision == 2
    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=2,
            client_request_id="i5b2-request-create-0001",
            input_data={"secret": _SECRET_SENTINEL},
            workspace_root=str(fixture.workspace.resolve()),
        )


@pytest.mark.asyncio
async def test_strict_input_errors_have_safe_codes_and_resource_status(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    service = _service(fixture)
    base = _saved_run_arguments(
        fixture,
        client_request_id="i6c-invalid-input-0001",
        input_data={"secret": 12},
    )
    with pytest.raises(WorkflowRequestError) as invalid:
        await service.start_saved_workflow(**base)
    assert getattr(invalid.value, "error_code", None) == "workflow_input_invalid"

    too_large = _saved_run_arguments(
        fixture,
        client_request_id="i6c-large-input-00001",
        input_data={"secret": "x" * 100_000},
    )
    with pytest.raises(WorkflowRequestError) as limited:
        await service.start_saved_workflow(**too_large)
    assert getattr(limited.value, "error_code", None) == "workflow_resource_limit"


@pytest.mark.asyncio
async def test_mcp_saved_start_distinguishes_missing_null_and_object_input(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    repository = StrictWorkflowDefinitionRepository(fixture.state_root)
    service = _service(fixture)
    runtime = FakeRuntime(repository, service)
    app = create_app(
        _settings(tmp_path, ("read", "execute")),
        runtime_factory=lambda _config: runtime,
    )
    common = {
        "definition_id": fixture.manifest["definition_id"],
        "revision": fixture.manifest["definition_revision"],
        "workspace_root": str(fixture.workspace.resolve()),
    }
    requests_before = (fixture.state_root / "requests.sqlite").read_bytes()

    async with _mcp_client(app) as client:
        catalog = await client.list_tools()
        omitted = await client.call_tool(
            "workflow_run_start",
            {**common, "client_request_id": "i6c-mcp-omitted-0001"},
        )
        explicit_null = await client.call_tool(
            "workflow_run_start",
            {**common, "client_request_id": "i6c-mcp-null-000001", "input": None},
        )
        explicit_object = await client.call_tool(
            "workflow_run_start",
            {
                **common,
                "client_request_id": "i6c-mcp-object-0001",
                "input": {"secret": _SECRET_SENTINEL},
            },
        )
        unknown = await client.call_tool(
            "workflow_run_start",
            {
                **common,
                "client_request_id": "i6c-mcp-unknown-001",
                "run_format_version": 2,
            },
        )
        reserved_replay = await client.call_tool(
            "workflow_run_start",
            {
                **common,
                "client_request_id": "i5b2-request-create-0001",
                "input": {"secret": _SECRET_SENTINEL},
            },
        )
        run_status = await client.call_tool(
            "workflow_run_get", {"workflow_id": fixture.manifest["workflow_id"]}
        )
        run_result = await client.call_tool(
            "workflow_run_result", {"workflow_id": fixture.manifest["workflow_id"]}
        )
        run_list = await client.call_tool("workflow_runs_list", {})

    start_tool = next(tool for tool in catalog.tools if tool.name == "workflow_run_start")
    assert "input" in start_tool.input_schema["properties"]
    assert "input" not in start_tool.input_schema.get("required", [])
    assert omitted.structured_content == {"error": "workflow_service_unavailable"}
    assert explicit_null.structured_content == {"error": "workflow_input_invalid"}
    assert explicit_object.structured_content == {"error": "workflow_service_unavailable"}
    assert unknown.structured_content == {"error": "invalid_workflow_request"}
    assert reserved_replay.structured_content["error"] == "workflow_needs_reconciliation"
    assert reserved_replay.structured_content["run_id"] == fixture.manifest["run_id"]
    assert (
        reserved_replay.structured_content["binding_digest"] == fixture.manifest["binding_digest"]
    )
    assert run_status.structured_content["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert run_result.structured_content["error"] == "workflow_needs_reconciliation"
    assert run_result.structured_content["run_id"] == fixture.manifest["run_id"]
    assert run_result.structured_content["reason_code"] == "strict_execution_uncertain"
    assert (
        run_list.structured_content["workflows"][0]["binding_digest"]
        == fixture.manifest["binding_digest"]
    )
    assert (fixture.state_root / "requests.sqlite").read_bytes() == requests_before
    with sqlite3.connect(fixture.state_root / "requests.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_requests").fetchone()[0] == 1
    assert list(fixture.state_root.glob("run-*")) == [fixture.run_dir]


@pytest.mark.asyncio
async def test_mcp_legacy_explicit_null_input_keeps_empty_object_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import service as service_module

    monkeypatch.setattr(service_module, "missing_workflow_dependencies", lambda: [])
    app, _runtime_value, repository, manager, workspace, _settings_value = _runtime(tmp_path)
    saved = repository.create(
        mutation_request_id="i6c-legacy-definition-0001",
        source_yaml=_source("i6c-legacy-null", "legacy null must remain empty"),
        max_live_codex_jobs=1,
    )
    common = {
        "definition_id": saved.definition_id,
        "revision": 1,
        "workspace_root": str(workspace),
    }
    async with _mcp_client(app) as client:
        explicit_null = await client.call_tool(
            "workflow_run_start",
            {**common, "client_request_id": "i6c-legacy-null-000001", "input": None},
        )

    assert "workflow_id" in explicit_null.structured_content
    assert json.loads((manager.start_calls[0][0] / "input.json").read_text(encoding="utf-8")) == {}


@pytest.mark.asyncio
async def test_http_strict_saved_start_and_result_use_safe_reconciliation_contract(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    repository = StrictWorkflowDefinitionRepository(fixture.state_root)
    service = _service(fixture)
    app = create_ui_app(
        HttpFakeBridge(),
        ActivityStore(),
        _http_settings(tmp_path),
        workflow_service=service,
        workflow_definitions=repository,
    )
    endpoint = f"{_HTTP_DEFINITIONS}/{fixture.manifest['definition_id']}/runs"
    common = {
        "revision": fixture.manifest["definition_revision"],
        "workspace_root": str(fixture.workspace.resolve()),
    }
    requests_before = (fixture.state_root / "requests.sqlite").read_bytes()

    async with httpx.AsyncClient(
        transport=_http_transport(app), base_url="http://localhost"
    ) as client:
        omitted = await client.post(
            endpoint,
            headers=_http_headers(),
            json={**common, "client_request_id": "i6c-http-omitted-0001"},
        )
        explicit_null = await client.post(
            endpoint,
            headers=_http_headers(),
            json={**common, "client_request_id": "i6c-http-null-00001", "input": None},
        )
        explicit_object = await client.post(
            endpoint,
            headers=_http_headers(),
            json={
                **common,
                "client_request_id": "i6c-http-object-0001",
                "input": {"secret": _SECRET_SENTINEL},
            },
        )
        nested_input: object = "too deep"
        for _ in range(35):
            nested_input = {"secret": nested_input}
        limited = await client.post(
            endpoint,
            headers=_http_headers(),
            json={
                **common,
                "client_request_id": "i6c-http-resource-limit-1",
                "input": nested_input,
            },
        )
        unknown = await client.post(
            endpoint,
            headers=_http_headers(),
            json={
                **common,
                "client_request_id": "i6c-http-unknown-001",
                "run_format_version": 2,
            },
        )
        replay = await client.post(
            endpoint,
            headers=_http_headers(),
            json={
                **common,
                "client_request_id": "i5b2-request-create-0001",
                "input": {"secret": _SECRET_SENTINEL},
            },
        )
        workflow_id = fixture.manifest["workflow_id"]
        status = await client.get(
            f"/ui-api/control/workflows/{workflow_id}", headers=_http_headers()
        )
        result = await client.get(
            f"/ui-api/control/workflows/{workflow_id}/result", headers=_http_headers()
        )

    assert omitted.status_code == 503
    assert omitted.json() == {"error": "workflow_service_unavailable"}
    assert explicit_null.status_code == 400
    assert explicit_null.json() == {"error": "workflow_input_invalid"}
    assert explicit_object.status_code == 503
    assert limited.status_code == 413
    assert limited.json() == {"error": "workflow_resource_limit"}
    assert unknown.status_code == 400
    assert unknown.json() == {"error": "invalid"}
    assert replay.status_code == 409
    assert replay.json()["error"] == "workflow_needs_reconciliation"
    assert replay.json()["workflow_id"] == workflow_id
    assert replay.json()["run_id"] == fixture.manifest["run_id"]
    assert replay.json()["reason_code"] == "strict_execution_uncertain"
    assert replay.json()["binding_digest"] == fixture.manifest["binding_digest"]
    assert status.status_code == 200
    assert status.json()["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert result.status_code == 409
    assert result.json()["workflow_id"] == workflow_id
    assert result.json()["run_id"] == fixture.manifest["run_id"]
    assert result.json()["reason_code"] == "strict_execution_uncertain"
    assert result.json()["error"] == "workflow_needs_reconciliation"
    assert (fixture.state_root / "requests.sqlite").read_bytes() == requests_before


@pytest.mark.asyncio
async def test_contract_store_read_failure_maps_to_safe_unavailable_error(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)

    class UnavailableStrictRepository:
        supports_contracts = True

        def get(self, definition_id: str, revision: int | None = None):
            raise DefinitionRepositoryUnavailable("sensitive store detail")

    repository = UnavailableStrictRepository()
    service = _service(fixture)
    ui_app = create_ui_app(
        HttpFakeBridge(),
        ActivityStore(),
        _http_settings(tmp_path),
        workflow_service=service,
        workflow_definitions=repository,
    )
    mcp_app = create_app(
        _settings(tmp_path, ("read", "execute")),
        runtime_factory=lambda _config: FakeRuntime(repository, service),
    )
    body = {
        "revision": fixture.manifest["definition_revision"],
        "client_request_id": "i6c-store-unavailable-1",
        "workspace_root": str(fixture.workspace.resolve()),
        "input": {"secret": _SECRET_SENTINEL},
    }
    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            f"{_HTTP_DEFINITIONS}/{fixture.manifest['definition_id']}/runs",
            headers=_http_headers(),
            json=body,
        )
    async with _mcp_client(mcp_app) as client:
        mcp_response = await client.call_tool(
            "workflow_run_start",
            {
                "definition_id": fixture.manifest["definition_id"],
                "revision": fixture.manifest["definition_revision"],
                "client_request_id": "i6c-store-unavailable-2",
                "workspace_root": str(fixture.workspace.resolve()),
                "input": {"secret": _SECRET_SENTINEL},
            },
        )

    assert response.status_code == 503
    assert response.json() == {"error": "workflow_contract_unavailable"}
    assert mcp_response.structured_content == {"error": "workflow_contract_unavailable"}
    assert not (fixture.run_dir / "bridge-lifecycle.jsonl").exists()
