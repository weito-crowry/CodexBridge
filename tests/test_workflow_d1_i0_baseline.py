from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import httpx2 as httpx
import pytest

from codex_bridge.server import create_app
from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import OwsError, validate_official_schema, validate_workflow
from tests.test_workflow_definition_api import (
    _BASE,
    _auth,
    _payload,
    _source,
    _transport,
)
from tests.test_workflow_definition_api import (
    _app as _definition_app,
)
from tests.test_workflow_saved_execution import _create as _create_saved
from tests.test_workflow_saved_execution import _saved_fixture
from tests.test_workflow_saved_mcp import (
    _DEFINITION_READ_TOOLS,
    _DEFINITION_WRITE_TOOLS,
    _RUN_START_TOOL,
    _WORKFLOW_RUN_TOOLS,
    FakeRuntime,
)
from tests.test_workflow_saved_mcp import (
    _settings as _mcp_settings,
)
from tests.test_workflows_api import _workflow_yaml

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "workflow_d1_i0_legacy_golden.json"
_STRICT_ONLY_FIELDS = {
    "contract",
    "contract_sha256",
    "binding_digest",
    "run_format_version",
    "reason_code",
}


def _golden() -> dict[str, list[str]]:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_legacy_http_definition_shapes_etag_and_detail_projection(tmp_path) -> None:
    repository_state = tmp_path / "state" / "workflows"
    from codex_bridge.workflows.definitions import WorkflowDefinitionRepository

    repository = WorkflowDefinitionRepository(repository_state)
    app = _definition_app(tmp_path, repository=repository)
    source = _source("i0-legacy-definition", "golden prompt")
    golden = _golden()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        validation = await client.post(
            f"{_BASE}/validate",
            headers=_auth(),
            json={"source_yaml": source, "max_live_codex_jobs": 1},
        )
        created = await client.post(
            _BASE,
            headers=_auth(),
            json=_payload("i0-legacy-create", source),
        )

        assert validation.status_code == 200
        assert set(validation.json()) == set(golden["validate"])
        assert created.status_code == 201
        assert set(created.json()) == set(golden["create"])
        created_body = created.json()
        assert created_body["etag"] == created.headers["etag"]
        assert re.fullmatch(r'"workflow-definition-v1-[0-9a-f]{64}"', created_body["etag"])

        definition_id = created_body["definition_id"]
        listed = await client.get(_BASE, headers=_auth())
        detail = await client.get(f"{_BASE}/{definition_id}", headers=_auth())
        revision = await client.get(f"{_BASE}/{definition_id}/revisions/1", headers=_auth())
        assert listed.status_code == detail.status_code == revision.status_code == 200
        assert set(listed.json()) == set(golden["list_envelope"])
        assert set(listed.json()["definitions"][0]) == set(golden["list_item"])
        assert set(detail.json()) == set(golden["detail"])
        assert set(revision.json()) == set(golden["detail"])
        assert detail.json()["source_yaml"] == revision.json()["source_yaml"] == source
        assert "golden prompt" not in listed.text
        assert "source_yaml" not in listed.text
        assert not (_STRICT_ONLY_FIELDS & set(created_body))
        assert not (_STRICT_ONLY_FIELDS & set(listed.json()["definitions"][0]))


@pytest.mark.asyncio
async def test_legacy_http_etag_preconditions_update_replay_and_revision_history(tmp_path) -> None:
    from codex_bridge.workflows.definitions import WorkflowDefinitionRepository

    repository = WorkflowDefinitionRepository(tmp_path / "state")
    app = _definition_app(tmp_path, repository=repository)
    golden = _golden()
    source_one = _source("i0-revision-one")
    source_two = _source("i0-revision-two")
    source_three = _source("i0-revision-three")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        created = await client.post(
            _BASE,
            headers=_auth(),
            json=_payload("i0-etag-create", source_one),
        )
        definition_id = created.json()["definition_id"]
        etag_one = created.headers["etag"]
        updated = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("i0-etag-update", source_two),
        )
        replay = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("i0-etag-update", source_two),
        )
        stale = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("i0-etag-stale", source_three),
        )
        missing = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(),
            json=_payload("i0-etag-missing", source_three),
        )
        old_revision = await client.get(f"{_BASE}/{definition_id}/revisions/1", headers=_auth())
        new_revision = await client.get(f"{_BASE}/{definition_id}/revisions/2", headers=_auth())

    assert updated.status_code == replay.status_code == 200
    assert set(updated.json()) == set(golden["update"])
    assert updated.json()["etag"] == updated.headers["etag"]
    assert re.fullmatch(r'"workflow-definition-v1-[0-9a-f]{64}"', updated.json()["etag"])
    assert updated.json()["revision"] == replay.json()["revision"] == 2
    assert replay.headers["etag"] == updated.headers["etag"]
    assert stale.status_code == 412
    assert stale.json() == {"error": "precondition_failed"}
    assert missing.status_code == 428
    assert old_revision.json()["source_yaml"] == source_one
    assert new_revision.json()["source_yaml"] == source_two
    assert not (_STRICT_ONLY_FIELDS & set(replay.json()))


@pytest.mark.asyncio
async def test_legacy_saved_run_manifest_binding_replay_and_restart(tmp_path, monkeypatch) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create_saved(repository, "i0-saved-definition", _workflow_yaml("saved prompt"))
    arguments = {
        "repository": repository,
        "definition_id": saved.definition_id,
        "revision": saved.revision,
        "client_request_id": "i0-saved-run-request",
        "input_data": {"answer": 4},
        "workspace_root": str(workspace),
    }

    first = await service.start_saved_workflow(**arguments)
    replay = await service.start_saved_workflow(**arguments)
    assert replay.run_id == first.run_id
    assert len(manager.start_calls) == 1

    run_dir = manager.state_root / first.run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "run_format_version" not in manifest
    assert manifest["definition_sha256"] == saved.canonical_sha256
    input_snapshot = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    assert input_snapshot == arguments["input_data"]
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        request_row = connection.execute(
            "SELECT run_id, request_digest FROM workflow_requests WHERE client_request_id = ?",
            (arguments["client_request_id"],),
        ).fetchone()
        source_row = connection.execute(
            "SELECT definition_id, revision, source_sha256 FROM workflow_request_sources "
            "WHERE run_id = ?",
            (first.run_id,),
        ).fetchone()
    assert request_row is not None and request_row[0] == first.run_id
    assert isinstance(request_row[1], str) and len(request_row[1]) == 64
    assert source_row == (saved.definition_id, saved.revision, saved.source_sha256)

    from codex_bridge.workflows.service import WorkflowService
    from tests.test_workflows_service import FakeWorkerManager

    restarted_manager = FakeWorkerManager(
        manager.state_root,
        workspace,
        allowed_roots=(tmp_path,),
    )
    restarted_service = WorkflowService(restarted_manager, codex_bridge_port=18001)
    restarted_replay = await restarted_service.start_saved_workflow(**arguments)
    assert restarted_replay.run_id == first.run_id
    assert restarted_manager.start_calls == []


def test_legacy_graph_schema_count_and_json_pointer_node_ids_are_stable() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "i0-pointer-baseline",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {"a/b~c": {"do": [{"a/b~c": {"set": {"value": "nested"}}}]}},
            {"finish": {"set": {"value": "done"}}},
        ],
    }

    graph = project_workflow_graph(definition)

    assert graph["schema_version"] == 1
    assert graph["static_task_count"] == 3
    assert [node["node_id"] for node in graph["nodes"]] == [
        "/do/0/a~1b~0c",
        "/do/0/a~1b~0c/do/0/a~1b~0c",
        "/do/1/finish",
    ]
    assert json.loads(json.dumps(graph, ensure_ascii=False, allow_nan=False)) == graph


def test_legacy_mode_official_ows_allows_but_interpreter_rejects_variable_call() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "i0-legacy-variable-call",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "update_decision": {
                    "call": "codexbridge-variable",
                    "with": {
                        "operation": "assign",
                        "declaration_id": "decision",
                        "scope": "workflow",
                        "value": "reviewed",
                    },
                }
            }
        ],
    }

    validate_official_schema(definition)
    with pytest.raises(OwsError, match="unsupported call target 'codexbridge-variable'"):
        validate_workflow(definition)


@pytest.mark.parametrize(
    ("permissions", "expected_tools"),
    [
        (("read",), _DEFINITION_READ_TOOLS | _WORKFLOW_RUN_TOOLS),
        (("write",), _DEFINITION_WRITE_TOOLS),
        (("execute",), {_RUN_START_TOOL}),
    ],
)
def test_legacy_mcp_permission_catalog_keeps_read_write_execute_separate(
    tmp_path, permissions, expected_tools
) -> None:
    app = create_app(
        _mcp_settings(tmp_path, permissions),
        runtime_factory=lambda _: FakeRuntime(),
    )
    tool_names = {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}
    workflow_tools = (
        _DEFINITION_READ_TOOLS | _DEFINITION_WRITE_TOOLS | _WORKFLOW_RUN_TOOLS | {_RUN_START_TOOL}
    )

    assert tool_names & workflow_tools == expected_tools
