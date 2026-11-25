from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
import yaml

from codex_bridge.server import create_app
from codex_bridge.workflows.contracts import DRAFT_2020_12, compile_contract
from codex_bridge.workflows.definitions import (
    WorkflowContractRequired,
    WorkflowDefinitionRepository,
)
from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import OwsError, validate_workflow
from codex_bridge.workflows.strict_definition_repository import (
    StrictWorkflowDefinitionRepository,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.task_projection import (
    project_workflow_task_events,
    project_workflow_tasks,
)
from tests.test_workflow_d1_i5b2_reader_gate import (
    _inspection,
    _request_row,
    _strict_fixture,
)
from tests.test_workflow_definition_api import _BASE, _auth, _transport
from tests.test_workflow_definition_api import _app as _http_app
from tests.test_workflow_definition_mcp import FakeRuntime, _config, _mcp_client


def _contract(*, owner_scope: str = "workflow") -> dict[str, Any]:
    object_schema = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "additionalProperties": False,
    }
    return {
        "contract_version": 1,
        "input_schema": object_schema,
        "output_schema": object_schema,
        "variables": [
            {
                "declaration_id": "decision",
                "name": "decision",
                "owner_scope": owner_scope,
                "value_schema": {"type": "string"},
                "sensitivity": "public",
                "initial": "PENDING",
            }
        ],
    }


def _definition(
    arguments: dict[str, Any] | None = None,
    *,
    call: str = "codexbridge-variable",
    input_config: dict[str, Any] | None = None,
    output_config: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "i7a-variable-call",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": tasks
        or [
            {
                "update_decision": {
                    "call": call,
                    "with": arguments
                    if arguments is not None
                    else {
                        "operation": "assign",
                        "declaration_id": "decision",
                        "scope": "workflow",
                        "value": "ACCEPTED",
                    },
                }
            }
        ],
    }
    if input_config is not None:
        definition["input"] = input_config
    if output_config is not None:
        definition["output"] = output_config
    return definition


def _source(name: str, arguments: dict[str, Any] | None = None) -> str:
    value = _definition(arguments)
    value["document"]["name"] = name
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True)


def _validate_strict(
    definition: dict[str, Any], contract: dict[str, Any] | None = None, *, profile: int = 1
) -> None:
    validate_workflow(
        definition,
        codex_job_live=True,
        max_live_codex_jobs=profile,
        strict_contract=compile_contract(contract or _contract()),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "operation": "assign",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": "${ .input.value }",
        },
        {"operation": "increment", "declaration_id": "decision", "scope": "workflow"},
        {"operation": "decrement", "declaration_id": "decision", "scope": "workflow", "by": 0.5},
        {
            "operation": "append",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": {"from": "${ .input.value }"},
        },
        {
            "operation": "object_set",
            "declaration_id": "decision",
            "scope": "workflow",
            "path": "/a~1b",
            "value": "REVIEWED",
        },
        {
            "operation": "object_delete",
            "declaration_id": "decision",
            "scope": "workflow",
            "path": "",
        },
        {"operation": "variable_delete", "declaration_id": "decision", "scope": "workflow"},
    ],
)
def test_strict_validator_accepts_all_static_variable_operations_and_zero_job_profile_one(
    arguments: dict[str, Any],
) -> None:
    _validate_strict(_definition(arguments))


def test_strict_literal_null_requires_a_nullable_declaration_schema() -> None:
    arguments = {
        "operation": "assign",
        "declaration_id": "decision",
        "scope": "workflow",
        "value": None,
    }
    nullable_contract = _contract()
    nullable_contract["variables"][0]["value_schema"] = {"type": ["string", "null"]}
    _validate_strict(_definition(arguments), nullable_contract)
    with pytest.raises(OwsError):
        _validate_strict(_definition(arguments))


def test_strict_append_and_object_set_use_their_operation_value_schema() -> None:
    array_contract = _contract()
    array_contract["variables"][0]["value_schema"] = {
        "type": "array",
        "items": {"type": ["string", "null"]},
    }
    array_contract["variables"][0]["initial"] = ["PENDING"]
    append_null = {
        "operation": "append",
        "declaration_id": "decision",
        "scope": "workflow",
        "value": None,
    }
    _validate_strict(_definition(append_null), array_contract)

    object_contract = _contract()
    object_contract["variables"][0]["value_schema"] = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    object_contract["variables"][0]["initial"] = {"count": 1}
    object_set = {
        "operation": "object_set",
        "declaration_id": "decision",
        "scope": "workflow",
        "path": "/count",
        "value": 2,
    }
    _validate_strict(_definition(object_set), object_contract)


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "unknown", "declaration_id": "decision", "scope": "workflow"},
        {"operation": "assign", "scope": "workflow", "value": "x"},
        {"operation": "assign", "declaration_id": "missing", "scope": "workflow", "value": "x"},
        {"operation": "assign", "declaration_id": "decision", "scope": "issue", "value": "x"},
        {"operation": "assign", "declaration_id": "decision", "scope": "workflow"},
        {
            "operation": "assign",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": None,
        },
        {
            "operation": "assign",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": 7,
        },
        {
            "operation": "assign",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": "x",
            "frame_id": "forged",
        },
        {"operation": "increment", "declaration_id": "decision", "scope": "workflow", "by": True},
        {"operation": "increment", "declaration_id": "decision", "scope": "workflow", "by": -1},
        {
            "operation": "increment",
            "declaration_id": "decision",
            "scope": "workflow",
            "by": float("nan"),
        },
        {
            "operation": "object_set",
            "declaration_id": "decision",
            "scope": "workflow",
            "path": "/bad~2path",
            "value": 3,
        },
        {
            "operation": "object_delete",
            "declaration_id": "decision",
            "scope": "workflow",
            "path": "relative",
        },
    ],
)
def test_strict_validator_rejects_invalid_variable_operation_arguments(
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(OwsError):
        _validate_strict(_definition(arguments))


def test_strict_variable_call_requires_workflow_declaration_and_rejects_legacy_mode() -> None:
    args = {
        "operation": "assign",
        "declaration_id": "decision",
        "scope": "workflow",
        "value": "ACCEPTED",
    }
    with pytest.raises(OwsError):
        _validate_strict(_definition(args), _contract(owner_scope="issue"))
    with pytest.raises(OwsError):
        validate_workflow(_definition(args), codex_job_live=True, max_live_codex_jobs=1)
    with pytest.raises(OwsError):
        _validate_strict(_definition(args), profile=2)
    with pytest.raises(OwsError):
        _validate_strict(_definition(call="custom-function"))


def test_strict_context_preserves_codex_job_and_nonlive_mock_job_boundaries() -> None:
    contract = compile_contract(_contract())
    _validate_strict(_definition(call="codex-job", arguments={"prompt": "Do nothing."}))
    mock = _definition(
        call="mock-job",
        arguments={"operation": "process", "payload": {"value": 1}, "duration_ms": 0},
    )
    validate_workflow(mock, codex_job_live=False, strict_contract=contract)
    with pytest.raises(OwsError):
        validate_workflow(
            mock, codex_job_live=True, max_live_codex_jobs=1, strict_contract=contract
        )


def test_cbvars_is_strict_only_and_cannot_be_shadowed_or_read_from_root_input() -> None:
    contract = _contract()
    compiled = compile_contract(contract)
    output_definition = _definition(
        output_config={"as": "${ $cbvars.workflow.decision }"},
        tasks=[
            {
                "copy": {
                    "input": {"from": "${ $cbvars.workflow.decision }"},
                    "set": {"value": "${ $input }"},
                }
            }
        ],
    )
    validate_workflow(
        output_definition,
        codex_job_live=True,
        max_live_codex_jobs=1,
        strict_contract=compiled,
    )
    with pytest.raises(OwsError):
        validate_workflow(
            _definition(input_config={"from": "${ $cbvars.workflow.decision }"}),
            codex_job_live=True,
            max_live_codex_jobs=1,
            strict_contract=compiled,
        )
    for binder in ("each", "at"):
        with pytest.raises(OwsError):
            validate_workflow(
                _definition(
                    tasks=[
                        {
                            "iterate": {
                                "for": {"in": "${ .items }", binder: "cbvars"},
                                "do": [{"copy": {"set": {"value": "${ $cbvars }"}}}],
                            }
                        }
                    ]
                ),
                codex_job_live=True,
                max_live_codex_jobs=1,
                strict_contract=compiled,
            )
    with pytest.raises(OwsError):
        validate_workflow(
            _definition(tasks=[{"copy": {"set": {"value": "${ $cbvars }"}}}]),
            codex_job_live=True,
            max_live_codex_jobs=1,
        )


def test_strict_variable_only_definition_roundtrips_through_v2_repository(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    source = _source("i7a-variable-only-create")
    contract = _contract()

    first = repository.create(
        mutation_request_id="i7a-create-variable-only",
        source_yaml=source,
        max_live_codex_jobs=1,
        contract=contract,
    )
    replay = repository.create(
        mutation_request_id="i7a-create-variable-only",
        source_yaml=source,
        max_live_codex_jobs=1,
        contract=contract,
    )
    loaded = repository.get(first.definition_id)
    listed = next(
        item for item in repository.list().items if item.definition_id == first.definition_id
    )
    updated_source = _source(
        "i7a-variable-only-update",
        {
            "operation": "assign",
            "declaration_id": "decision",
            "scope": "workflow",
            "value": "REVIEWED",
        },
    )
    revised = repository.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="i7a-update-variable-only",
        source_yaml=updated_source,
        max_live_codex_jobs=1,
        contract=contract,
    )
    update_replay = repository.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="i7a-update-variable-only",
        source_yaml=updated_source,
        max_live_codex_jobs=1,
        contract=contract,
    )
    with pytest.raises(WorkflowContractRequired):
        repository.update(
            definition_id=first.definition_id,
            expected_revision=2,
            mutation_request_id="i7a-update-contract-missing",
            source_yaml=_source("i7a-variable-only-missing-contract"),
            max_live_codex_jobs=1,
        )

    assert replay == first
    assert update_replay == revised
    assert loaded.contract_version == 1
    assert loaded.contract_sha256 == first.contract_sha256
    assert listed.contract_sha256 == first.contract_sha256
    assert revised.revision == 2
    assert repository.get(first.definition_id, 1).etag == first.etag
    assert repository.revision_for_etag(first.definition_id, first.etag) == 1
    assert repository.revision_for_etag(first.definition_id, revised.etag) == 2
    assert store.get(first.definition_id, 1).definition["do"][0]["update_decision"]["call"] == (
        "codexbridge-variable"
    )


def test_strict_static_graph_accepts_variable_call_and_preserves_legacy_rejection() -> None:
    definition = _definition()
    graph = project_workflow_graph(
        definition,
        codex_job_live=True,
        max_live_codex_jobs=1,
        strict_contract=compile_contract(_contract()),
    )
    assert graph["static_task_count"] == 1
    assert graph["nodes"][0]["operation"] == "call"
    with pytest.raises(OwsError):
        project_workflow_graph(definition, codex_job_live=True, max_live_codex_jobs=1)


def test_http_and_mcp_contract_validation_and_save_accept_strict_variable_only_source(
    tmp_path: Path,
) -> None:
    source = _source("i7a-transport-variable-only")
    contract = _contract()
    legacy_state = tmp_path / "validation-only"
    legacy_repo = WorkflowDefinitionRepository(legacy_state)
    legacy_app = _http_app(tmp_path, repository=legacy_repo)
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    http_app = _http_app(tmp_path, repository=repository)
    mcp_app = create_app(
        _config(tmp_path, ("read", "write")),
        runtime_factory=lambda _config_value: FakeRuntime(repository),
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=_transport(legacy_app), base_url="http://localhost"
        ) as legacy_client:
            validation_only = await legacy_client.post(
                f"{_BASE}/validate",
                headers=_auth(),
                json={"source_yaml": source, "max_live_codex_jobs": 1, "contract": contract},
            )
        async with httpx.AsyncClient(
            transport=_transport(http_app), base_url="http://localhost"
        ) as http_client:
            validated = await http_client.post(
                f"{_BASE}/validate",
                headers=_auth(),
                json={"source_yaml": source, "max_live_codex_jobs": 1, "contract": contract},
            )
            created = await http_client.post(
                _BASE,
                headers=_auth(),
                json={
                    "mutation_request_id": "i7a-http-create-variable-only",
                    "source_yaml": source,
                    "max_live_codex_jobs": 1,
                    "contract": contract,
                },
            )
            graph = await http_client.get(
                f"{_BASE}/{created.json()['definition_id']}/graph", headers=_auth()
            )
        async with _mcp_client(mcp_app) as mcp_client:
            mcp_validated = await mcp_client.call_tool(
                "workflow_definition_validate",
                {"source_yaml": source, "contract": contract},
            )
            mcp_created = await mcp_client.call_tool(
                "workflow_definition_create",
                {
                    "mutation_request_id": "i7a-mcp-create-variable-only",
                    "source_yaml": source,
                    "contract": contract,
                },
            )
            mcp_graph = await mcp_client.call_tool(
                "workflow_graph_get",
                {"definition_id": mcp_created.structured_content["definition_id"]},
            )

        assert validation_only.status_code == 200
        assert validation_only.json()["contract_version"] == 1
        assert validated.status_code == 200
        assert validated.json()["valid"] is True
        assert created.status_code == 201
        assert graph.status_code == 200
        assert graph.json()["graph"]["static_task_count"] == 1
        assert mcp_validated.structured_content["valid"] is True
        assert mcp_created.structured_content["contract_version"] == 1
        assert mcp_graph.structured_content["graph"]["static_task_count"] == 1
        assert not legacy_state.exists()

    import anyio

    anyio.run(exercise)


def test_verified_strict_task_event_graph_accepts_pinned_variable_call(
    tmp_path: Path,
) -> None:
    source = _source(
        "i7a-reader-variable-only",
        {
            "operation": "assign",
            "declaration_id": "counter",
            "scope": "workflow",
            "value": 1,
        },
    )
    fixture = _strict_fixture(tmp_path, source_yaml=source)
    row = _request_row(fixture.manifest)
    tasks = project_workflow_tasks(
        fixture.state_root,
        row,
        _inspection(),
        allowed_roots=(str(fixture.workspace),),
    )
    events = project_workflow_task_events(
        fixture.state_root,
        row,
        _inspection(),
        after_id=0,
        limit=10,
        allowed_roots=(str(fixture.workspace),),
    )
    assert tasks["coverage"] == "partial"
    assert tasks["coverage_reason"] == "workflow_not_confirmed"
    assert tasks["nodes"][0]["operation"] == "call"
    assert events["events"] == []
    assert events["coverage_reason"] == "workflow_not_confirmed"
    assert "private-input-sentinel-b2" not in str((tasks, events))
