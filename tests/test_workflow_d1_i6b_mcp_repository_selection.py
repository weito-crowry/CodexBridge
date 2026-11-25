from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx2 as httpx
import pytest

from codex_bridge import server as server_module
from codex_bridge.activity import ActivityStore
from codex_bridge.server import create_app
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.definition_repository_selection import (
    select_workflow_definition_repository,
)
from codex_bridge.workflows.definitions import (
    DefinitionRepositoryUnavailable,
    WorkflowDefinitionRepository,
)
from codex_bridge.workflows.service import WorkflowService
from codex_bridge.workflows.strict_definition_repository import (
    StrictWorkflowDefinitionRepository,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from tests.test_workflow_d1_i3a_contract_storage import _contract, _migrate
from tests.test_workflow_definition_api import _BASE as _HTTP_DEFINITIONS
from tests.test_workflow_definition_api import FakeBridge as HttpFakeBridge
from tests.test_workflow_definition_api import _app as _http_app
from tests.test_workflow_definition_api import _auth
from tests.test_workflow_definition_api import _settings as _http_settings
from tests.test_workflow_definition_mcp import FakeRuntime, _config, _mcp_client, _source
from tests.test_workflows_service import FakeWorkerManager


def _http_transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


@pytest.mark.asyncio
async def test_definition_mcp_contract_schema_invocation_and_strict_projection(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    legacy = repository.create(
        mutation_request_id="i6b-legacy-create",
        source_yaml=_source("i6b-mcp-legacy", "legacy source"),
        max_live_codex_jobs=1,
    )
    strict = repository.create(
        mutation_request_id="i6b-strict-create",
        source_yaml=_source("i6b-mcp-strict", "strict source"),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    app = create_app(
        _config(tmp_path, ("read", "write")),
        runtime_factory=lambda _: FakeRuntime(repository),
    )
    contract = _contract()
    definition_database = state_root / "definitions.sqlite"
    database_before_reads = definition_database.read_bytes()

    async with _mcp_client(app) as client:
        catalog = await client.list_tools()
        tool_schemas = {
            tool.name: tool.model_dump(by_alias=True)["inputSchema"]
            for tool in catalog.tools
            if tool.name
            in {
                "workflow_definition_validate",
                "workflow_definition_create",
                "workflow_definition_update",
            }
        }
        listing = await client.call_tool("workflow_definitions_list", {})
        legacy_detail = await client.call_tool(
            "workflow_definition_get", {"definition_id": legacy.definition_id}
        )
        legacy_graph = await client.call_tool(
            "workflow_graph_get", {"definition_id": legacy.definition_id}
        )
        strict_detail = await client.call_tool(
            "workflow_definition_get", {"definition_id": strict.definition_id}
        )
        strict_graph = await client.call_tool(
            "workflow_graph_get", {"definition_id": strict.definition_id}
        )
        validated_legacy = await client.call_tool(
            "workflow_definition_validate",
            {"source_yaml": _source("i6b-validate-legacy", "validation")},
        )
        validated_strict = await client.call_tool(
            "workflow_definition_validate",
            {
                "source_yaml": _source("i6b-validate-strict", "validation"),
                "contract": contract,
            },
        )
        invalid_contract = await client.call_tool(
            "workflow_definition_validate",
            {
                "source_yaml": _source("i6b-invalid-contract", "invalid contract"),
                "contract": {**contract, "unexpected": "private contract value"},
            },
        )
        assert definition_database.read_bytes() == database_before_reads
        null_validation = await client.call_tool(
            "workflow_definition_validate",
            {"source_yaml": _source("i6b-validate-null", "validation"), "contract": None},
        )
        created_legacy = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "i6b-mcp-legacy-create-2",
                "source_yaml": _source("i6b-mcp-legacy-2", "legacy create"),
            },
        )
        created_strict = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "i6b-mcp-strict-create-2",
                "source_yaml": _source("i6b-mcp-strict-2", "strict create"),
                "contract": contract,
            },
        )
        created_null = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "i6b-mcp-null-create",
                "source_yaml": _source("i6b-mcp-null", "null create"),
                "contract": None,
            },
        )
        created_unknown = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "i6b-mcp-unknown-argument",
                "source_yaml": _source("i6b-mcp-unknown", "unknown argument"),
                "unexpected_argument": "must be rejected",
            },
        )
        strict_update_missing = await client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": strict.definition_id,
                "expected_revision": 1,
                "mutation_request_id": "i6b-mcp-update-missing",
                "source_yaml": _source("i6b-mcp-update-missing", "missing contract"),
            },
        )
        strict_update_null = await client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": strict.definition_id,
                "expected_revision": 1,
                "mutation_request_id": "i6b-mcp-update-null",
                "source_yaml": _source("i6b-mcp-update-null", "null contract"),
                "contract": None,
            },
        )
        legacy_update_null = await client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": legacy.definition_id,
                "expected_revision": 1,
                "mutation_request_id": "i6b-mcp-legacy-update-null",
                "source_yaml": _source("i6b-mcp-legacy-updated", "null legacy contract"),
                "contract": None,
            },
        )

    contract_schema = tool_schemas["workflow_definition_create"]["properties"]["contract"]
    contract_types = {
        option.get("type")
        for option in contract_schema.get("anyOf", [])
        if isinstance(option, dict)
    }
    for _name, schema in tool_schemas.items():
        assert "contract" in schema["properties"]
        assert "contract" not in schema.get("required", [])
        assert "default" not in schema["properties"]["contract"]
    assert contract_types == {"object", "null"}
    assert {"source_yaml"} <= set(tool_schemas["workflow_definition_validate"].get("required", []))
    assert {"mutation_request_id", "source_yaml"} <= set(
        tool_schemas["workflow_definition_create"].get("required", [])
    )
    assert {
        "definition_id",
        "expected_revision",
        "mutation_request_id",
        "source_yaml",
    } <= set(tool_schemas["workflow_definition_update"].get("required", []))

    definitions = listing.structured_content["definitions"]
    summary_by_id = {item["definition_id"]: item for item in definitions}
    assert "contract_version" not in summary_by_id[legacy.definition_id]
    assert set(summary_by_id[legacy.definition_id]) == {
        "definition_id",
        "latest_revision",
        "document_name",
        "source_sha256",
        "canonical_sha256",
        "max_live_codex_jobs",
        "created_at",
        "updated_at",
        "etag",
    }
    assert summary_by_id[strict.definition_id]["contract_version"] == 1
    assert summary_by_id[strict.definition_id]["binding_digest"] == strict.binding_digest
    assert "contract" not in summary_by_id[strict.definition_id]
    assert "contract_version" not in legacy_detail.structured_content
    assert set(legacy_detail.structured_content) == {
        "definition_id",
        "revision",
        "source_sha256",
        "canonical_sha256",
        "max_live_codex_jobs",
        "created_at",
        "etag",
        "document_name",
        "source_yaml",
    }
    assert strict_detail.structured_content["contract"] == contract
    assert strict_detail.structured_content["contract_sha256"] == strict.contract_sha256
    assert strict_graph.structured_content["binding_digest"] == strict.binding_digest
    assert "contract" not in strict_graph.structured_content
    assert set(strict_graph.structured_content) == {
        "definition_id",
        "revision",
        "etag",
        "graph",
        "contract_version",
        "contract_sha256",
        "binding_digest",
    }
    assert set(legacy_graph.structured_content) == {
        "definition_id",
        "revision",
        "etag",
        "graph",
    }
    assert set(validated_legacy.structured_content) == {
        "valid",
        "source_sha256",
        "canonical_sha256",
        "max_live_codex_jobs",
    }
    assert validated_strict.structured_content["contract_version"] == 1
    assert "binding_digest" not in validated_strict.structured_content
    assert invalid_contract.structured_content == {"error": "workflow_contract_invalid"}
    assert "private contract value" not in str(invalid_contract.structured_content)
    assert null_validation.structured_content == {"error": "workflow_contract_invalid"}
    assert "contract_version" not in created_legacy.structured_content
    assert set(created_legacy.structured_content) == {
        "definition_id",
        "revision",
        "source_sha256",
        "canonical_sha256",
        "max_live_codex_jobs",
        "created_at",
        "etag",
    }
    assert created_strict.structured_content["contract_version"] == 1
    assert created_strict.structured_content["binding_digest"]
    assert created_null.structured_content == {"error": "workflow_contract_invalid"}
    assert created_unknown.structured_content == {"error": "workflow_definition_invalid"}
    assert strict_update_missing.structured_content == {"error": "workflow_contract_required"}
    assert strict_update_null.structured_content == {"error": "workflow_contract_required"}
    assert legacy_update_null.structured_content == {"error": "workflow_contract_invalid"}
    assert store.get(strict.definition_id).revision == 1
    assert len(store.list().items) == 4


@pytest.mark.asyncio
async def test_mcp_and_http_share_strict_mutation_replay_and_precondition_rules(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    settings = _config(tmp_path, ("read", "write"))
    runtime = FakeRuntime(repository)
    mcp_app = create_app(settings, runtime_factory=lambda _: runtime)
    ui_app = _http_app(tmp_path, repository=repository)
    contract = _contract()
    create_body = {
        "mutation_request_id": "i6b-cross-create",
        "source_yaml": _source("i6b-cross-create", "cross transport"),
        "max_live_codex_jobs": 1,
        "contract": contract,
    }

    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as http_client:
        http_created = await http_client.post(
            _HTTP_DEFINITIONS,
            headers=_auth(),
            json=create_body,
        )
    async with _mcp_client(mcp_app) as mcp_client:
        mcp_created = await mcp_client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": create_body["mutation_request_id"],
                "source_yaml": create_body["source_yaml"],
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )

    assert http_created.status_code == 201
    assert mcp_created.structured_content["definition_id"] == http_created.json()["definition_id"]
    assert mcp_created.structured_content["etag"] == http_created.json()["etag"]

    update_source = _source("i6b-cross-update", "same update")
    update_request_id = "i6b-cross-update"
    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as http_client:
        http_updated = await http_client.put(
            f"{_HTTP_DEFINITIONS}/{http_created.json()['definition_id']}",
            headers=_auth(**{"If-Match": http_created.json()["etag"]}),
            json={
                "mutation_request_id": update_request_id,
                "source_yaml": update_source,
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )
    mcp_app = create_app(settings, runtime_factory=lambda _: runtime)
    async with _mcp_client(mcp_app) as mcp_client:
        mcp_replay = await mcp_client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": http_created.json()["definition_id"],
                "expected_revision": 1,
                "mutation_request_id": update_request_id,
                "source_yaml": update_source,
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )

    assert http_updated.status_code == 200
    assert mcp_replay.structured_content["revision"] == http_updated.json()["revision"] == 2
    assert mcp_replay.structured_content["etag"] == http_updated.json()["etag"]

    mcp_first_update_id = "i6b-cross-update-reverse"
    mcp_app = create_app(settings, runtime_factory=lambda _: runtime)
    async with _mcp_client(mcp_app) as mcp_client:
        mcp_updated = await mcp_client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": http_created.json()["definition_id"],
                "expected_revision": 2,
                "mutation_request_id": mcp_first_update_id,
                "source_yaml": _source("i6b-cross-update-reverse", "reverse replay"),
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )
        stale = await mcp_client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": http_created.json()["definition_id"],
                "expected_revision": 2,
                "mutation_request_id": "i6b-stale-revision",
                "source_yaml": _source("i6b-stale-revision", "stale"),
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )
        conflict = await mcp_client.call_tool(
            "workflow_definition_update",
            {
                "definition_id": http_created.json()["definition_id"],
                "expected_revision": 2,
                "mutation_request_id": mcp_first_update_id,
                "source_yaml": _source("i6b-cross-update-reverse", "reverse replay"),
                "max_live_codex_jobs": 1,
                "contract": _contract(default="different"),
            },
        )
    async with httpx.AsyncClient(
        transport=_http_transport(ui_app), base_url="http://localhost"
    ) as http_client:
        http_replay = await http_client.put(
            f"{_HTTP_DEFINITIONS}/{http_created.json()['definition_id']}",
            headers=_auth(**{"If-Match": http_updated.json()["etag"]}),
            json={
                "mutation_request_id": mcp_first_update_id,
                "source_yaml": _source("i6b-cross-update-reverse", "reverse replay"),
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )

    assert mcp_updated.structured_content["revision"] == 3
    assert stale.structured_content == {"error": "workflow_definition_precondition_failed"}
    assert conflict.structured_content == {"error": "workflow_definition_mutation_conflict"}
    assert http_replay.status_code == 200
    assert http_replay.json()["revision"] == mcp_updated.structured_content["revision"]
    assert http_replay.json()["etag"] == mcp_updated.structured_content["etag"]


def test_runtime_repository_selection_is_read_only_and_fail_closed(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    selected_missing = select_workflow_definition_repository(missing_root)
    assert type(selected_missing) is WorkflowDefinitionRepository
    assert not missing_root.exists()

    legacy_root = tmp_path / "legacy-v1"
    legacy = WorkflowDefinitionRepository(legacy_root)
    legacy.create(
        mutation_request_id="i6b-selection-legacy",
        source_yaml=_source("i6b-selection-legacy", "legacy"),
        max_live_codex_jobs=1,
    )
    legacy_database = legacy_root / "definitions.sqlite"
    legacy_bytes = legacy_database.read_bytes()
    selected_legacy = select_workflow_definition_repository(legacy_root)
    assert type(selected_legacy) is WorkflowDefinitionRepository
    assert legacy_database.read_bytes() == legacy_bytes

    strict_root = tmp_path / "strict-v2"
    strict_store = StrictWorkflowDefinitionStore(strict_root)
    strict_store.initialize_new_database()
    strict_store.create(
        mutation_request_id="i6b-selection-strict",
        source_yaml=_source("i6b-selection-strict", "strict"),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    strict_database = strict_root / "definitions.sqlite"
    strict_bytes = strict_database.read_bytes()
    selected_strict = select_workflow_definition_repository(strict_root)
    assert isinstance(selected_strict, StrictWorkflowDefinitionRepository)
    assert selected_strict.get(strict_store.list().items[0].definition_id).contract == _contract()
    assert strict_database.read_bytes() == strict_bytes

    migrated_root = tmp_path / "migrated-v2"
    migrated_store, _first, migrated_latest = _migrate(migrated_root)
    migrated_database = migrated_root / "definitions.sqlite"
    migrated_bytes = migrated_database.read_bytes()
    selected_migrated = select_workflow_definition_repository(migrated_root)
    assert isinstance(selected_migrated, StrictWorkflowDefinitionRepository)
    migrated_readback = selected_migrated.get(migrated_latest.definition_id)
    assert migrated_readback.contract_version is None
    assert migrated_readback.etag == migrated_latest.etag
    assert migrated_store.list().items
    assert migrated_database.read_bytes() == migrated_bytes

    unknown_root = tmp_path / "unknown-version"
    unknown_root.mkdir()
    unknown_database = unknown_root / "definitions.sqlite"
    with sqlite3.connect(unknown_database) as connection:
        connection.execute("PRAGMA user_version=19")
    unknown_bytes = unknown_database.read_bytes()
    selected_unknown = select_workflow_definition_repository(unknown_root)
    with pytest.raises(DefinitionRepositoryUnavailable):
        selected_unknown.list()
    assert unknown_database.read_bytes() == unknown_bytes

    corrupt_root = tmp_path / "corrupt-v2"
    corrupt_store = StrictWorkflowDefinitionStore(corrupt_root)
    corrupt_store.initialize_new_database()
    corrupt_database = corrupt_root / "definitions.sqlite"
    with sqlite3.connect(corrupt_database) as connection:
        connection.execute("DROP TABLE workflow_definition_contracts")
    corrupt_bytes = corrupt_database.read_bytes()
    selected_corrupt = select_workflow_definition_repository(corrupt_root)
    with pytest.raises(DefinitionRepositoryUnavailable):
        selected_corrupt.list()
    assert corrupt_database.read_bytes() == corrupt_bytes


def test_build_runtime_shares_the_selected_repository_with_http_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "prepared-v2"
    StrictWorkflowDefinitionStore(state_root).initialize_new_database()
    captured: dict[str, object] = {}

    class DummyBridge:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.observations = SimpleNamespace()

        def set_handlers(self, **_kwargs: object) -> None:
            return None

        async def handle_notification_async(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def handle_server_request(self, *_args: object, **_kwargs: object) -> object:
            return None

        def handle_app_server_failure(self, *_args: object, **_kwargs: object) -> None:
            return None

    class DummyUiServer:
        def __init__(self, app: object, *_args: object, **_kwargs: object) -> None:
            captured["ui_app"] = app

    monkeypatch.setattr(server_module, "prepare_config", lambda config: config)
    monkeypatch.setattr(server_module, "StateStore", lambda: object())
    monkeypatch.setattr(server_module, "ActivityStore", lambda: object())
    monkeypatch.setattr(
        server_module,
        "AppServerClient",
        lambda *_args, **_kwargs: SimpleNamespace(set_handlers=lambda **_kw: None),
    )
    monkeypatch.setattr(server_module, "FinalAnswerStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "Bridge", DummyBridge)
    monkeypatch.setattr(server_module, "CodexSessionScanner", lambda **_kwargs: object())
    monkeypatch.setattr(server_module, "ExternalExecutionMonitor", lambda *_args: object())
    monkeypatch.setattr(
        server_module,
        "WorkflowWorkerManager",
        lambda **_kwargs: SimpleNamespace(state_root=state_root),
    )
    monkeypatch.setattr(server_module, "WorkflowService", lambda *_args, **_kwargs: object())

    def capture_ui_app(*_args: object, workflow_definitions: object, **_kwargs: object) -> object:
        captured["repository"] = workflow_definitions
        return object()

    monkeypatch.setattr(server_module, "create_ui_app", capture_ui_app)
    monkeypatch.setattr(server_module, "LocalUiServer", DummyUiServer)

    runtime = server_module.build_runtime(_config(tmp_path, ("read", "write")))
    repository = runtime.workflow_definitions
    assert isinstance(repository, StrictWorkflowDefinitionRepository)
    assert captured["repository"] is repository

    async def no_start(_self: object) -> None:
        return None

    async def no_shutdown(_self: object) -> None:
        return None

    monkeypatch.setattr(server_module.BridgeRuntime, "start", no_start)
    monkeypatch.setattr(server_module.BridgeRuntime, "shutdown", no_shutdown)
    mcp_app = create_app(_config(tmp_path, ("read",)), runtime_factory=lambda _config: runtime)

    async def read_through_mcp() -> dict[str, object]:
        async with _mcp_client(mcp_app) as client:
            result = await client.call_tool("workflow_definitions_list", {})
            return result.structured_content

    mcp_result = anyio.run(read_through_mcp)
    assert mcp_result["definitions"] == []


def test_runtime_repository_selection_rejects_definition_database_symlink(
    tmp_path: Path,
) -> None:
    target_root = tmp_path / "target-v2"
    StrictWorkflowDefinitionStore(target_root).initialize_new_database()
    target_database = target_root / "definitions.sqlite"
    target_bytes = target_database.read_bytes()
    link_root = tmp_path / "symlink-db"
    link_root.mkdir()
    link_database = link_root / "definitions.sqlite"
    try:
        link_database.symlink_to(target_database)
    except OSError:
        pytest.skip("file symlink creation is unavailable on this Windows host")

    selected = select_workflow_definition_repository(link_root)
    with pytest.raises(DefinitionRepositoryUnavailable):
        selected.list()
    assert target_database.read_bytes() == target_bytes


@pytest.mark.asyncio
async def test_mcp_contract_validate_is_pure_on_v1_and_strict_saved_runs_stay_blocked(
    tmp_path: Path,
) -> None:
    unprepared_root = tmp_path / "unprepared-v1"
    legacy_repository = WorkflowDefinitionRepository(unprepared_root)
    write_app = create_app(
        _config(tmp_path, ("write",)),
        runtime_factory=lambda _config: FakeRuntime(legacy_repository),
    )
    async with _mcp_client(write_app) as client:
        validated = await client.call_tool(
            "workflow_definition_validate",
            {
                "source_yaml": _source("i6b-v1-contract-validation", "validation"),
                "contract": _contract(),
            },
        )
        unavailable_write = await client.call_tool(
            "workflow_definition_create",
            {
                "mutation_request_id": "i6b-v1-strict-write",
                "source_yaml": _source("i6b-v1-strict-write", "strict write"),
                "contract": _contract(),
            },
        )
    assert validated.structured_content["contract_version"] == 1
    assert unavailable_write.structured_content == {"error": "workflow_contract_unavailable"}
    assert not unprepared_root.exists()

    strict_root = tmp_path / "prepared-strict"
    store = StrictWorkflowDefinitionStore(strict_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(strict_root, store=store)
    saved = repository.create(
        mutation_request_id="i6b-strict-run-definition",
        source_yaml=_source("i6b-strict-run-definition", "strict runtime stays off"),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FakeWorkerManager(tmp_path / "run-state", workspace, allowed_roots=(tmp_path,))
    service = WorkflowService(manager, codex_bridge_port=18001)
    runtime = FakeRuntime(repository)
    runtime.bridge.app_server_ready = True
    runtime.workflow_service = service
    run_app = create_app(_config(tmp_path, ("execute",)), runtime_factory=lambda _config: runtime)
    async with _mcp_client(run_app) as client:
        mcp_rejected = await client.call_tool(
            "workflow_run_start",
            {
                "definition_id": saved.definition_id,
                "revision": 1,
                "client_request_id": "i6b-strict-mcp-run",
                "workspace_root": str(workspace),
            },
        )

    http_app = create_ui_app(
        HttpFakeBridge(),
        ActivityStore(),
        _http_settings(tmp_path),
        workflow_service=service,
        workflow_definitions=repository,
    )
    async with httpx.AsyncClient(
        transport=_http_transport(http_app), base_url="http://localhost"
    ) as client:
        http_rejected = await client.post(
            f"{_HTTP_DEFINITIONS}/{saved.definition_id}/runs",
            headers=_auth(),
            json={
                "client_request_id": "i6b-strict-http-run",
                "revision": 1,
                "workspace_root": str(workspace),
            },
        )

    assert mcp_rejected.structured_content == {"error": "workflow_service_unavailable"}
    assert http_rejected.status_code == 503
    assert http_rejected.json() == {"error": "workflow_service_unavailable"}
    assert manager.start_calls == []
    assert not manager.state_root.exists()
