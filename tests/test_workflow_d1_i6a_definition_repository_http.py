from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest

from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.service import WorkflowService, WorkflowServiceUnavailable
from codex_bridge.workflows.strict_definition_repository import (
    StrictWorkflowDefinitionRepository,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from tests.test_workflow_d1_i3a_contract_storage import _contract, _migrate
from tests.test_workflow_definition_api import (
    _BASE,
    _app,
    _auth,
    _payload,
    _source,
    _transport,
)


def test_strict_definition_store_lists_verified_summary_and_resolves_both_etag_versions(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    legacy = store.create(
        mutation_request_id="i6a-list-legacy",
        source_yaml=_source("i6a-legacy"),
        max_live_codex_jobs=1,
    )
    strict = store.create(
        mutation_request_id="i6a-list-strict",
        source_yaml=_source("i6a-strict"),
        max_live_codex_jobs=1,
        contract=_contract(),
    )

    list_definitions = getattr(store, "list", None)
    revision_for_etag = getattr(store, "revision_for_etag", None)
    assert callable(list_definitions), "v2 store is missing verified definition listing"
    assert callable(revision_for_etag), "v2 store is missing verified ETag revision lookup"

    first_page = list_definitions(limit=1)
    second_page = list_definitions(limit=1, cursor=first_page.next_cursor)
    listed = (*first_page.items, *second_page.items)
    by_id = {item.definition_id: item for item in listed}

    assert set(by_id) == {legacy.definition_id, strict.definition_id}
    strict_summary = by_id[strict.definition_id]
    assert strict_summary.contract_version == 1
    assert strict_summary.contract_sha256 == strict.contract_sha256
    assert strict_summary.binding_digest == strict.binding_digest
    assert not hasattr(strict_summary, "contract")
    assert revision_for_etag(legacy.definition_id, legacy.etag) == 1
    assert revision_for_etag(strict.definition_id, strict.etag) == 1
    assert revision_for_etag(strict.definition_id, legacy.etag) is None


def test_strict_repository_reads_migrated_legacy_history_without_strict_fields(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "migrated"
    store, first, second = _migrate(state_root)
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    migrated_latest = repository.get(second.definition_id)

    assert migrated_latest.revision == second.revision
    assert migrated_latest.etag == second.etag
    assert migrated_latest.contract_version is None
    assert migrated_latest.contract is None
    assert repository.revision_for_etag(first.definition_id, first.etag) == first.revision
    assert repository.revision_for_etag(second.definition_id, second.etag) == second.revision
    summary = repository.list().items[0]
    assert summary.contract_version is None
    assert summary.contract_sha256 is None
    assert summary.binding_digest is None


@pytest.mark.asyncio
async def test_v1_http_contract_validation_is_pure_and_strict_write_is_unavailable(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    repository = WorkflowDefinitionRepository(state_root)
    app = _app(tmp_path, repository=repository)
    valid_contract = _contract()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        validated = await client.post(
            f"{_BASE}/validate",
            headers=_auth(),
            json={
                "source_yaml": _source("i6a-validation"),
                "max_live_codex_jobs": 1,
                "contract": valid_contract,
            },
        )
        unavailable = await client.post(
            _BASE,
            headers=_auth(),
            json={
                **_payload("i6a-v1-strict-create", _source("i6a-v1")),
                "contract": valid_contract,
            },
        )
        null_contract = await client.post(
            _BASE,
            headers=_auth(),
            json={**_payload("i6a-null-create", _source("i6a-null")), "contract": None},
        )

    assert validated.status_code == 200
    assert validated.json()["valid"] is True
    assert validated.json()["contract_version"] == 1
    assert validated.json()["contract_sha256"]
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "workflow_contract_unavailable"}
    assert null_contract.status_code == 400
    assert null_contract.json() == {"error": "workflow_contract_invalid"}
    assert not state_root.exists()


@pytest.mark.asyncio
async def test_invalid_strict_contract_is_value_safe_and_does_not_write(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    app = _app(tmp_path, repository=repository)
    secret_marker = "I6A-CONTRACT-SECRET-MUST-NOT-LEAK"
    invalid_contract = {**_contract(), "unexpected": secret_marker}

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        invalid = await client.post(
            _BASE,
            headers=_auth(),
            json={
                **_payload("i6a-invalid-contract", _source("i6a-invalid-contract")),
                "contract": invalid_contract,
            },
        )
        null_contract = await client.post(
            _BASE,
            headers=_auth(),
            json={
                **_payload("i6a-null-contract-v2", _source("i6a-null-contract-v2")),
                "contract": None,
            },
        )

    assert invalid.status_code == 400
    assert invalid.json() == {"error": "workflow_contract_invalid"}
    assert secret_marker not in invalid.text
    assert null_contract.status_code == 400
    assert null_contract.json() == {"error": "workflow_contract_invalid"}
    assert store.list().items == ()


@pytest.mark.asyncio
async def test_strict_adapter_does_not_migrate_v1_database_for_contract_write(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "legacy-v1"
    legacy = WorkflowDefinitionRepository(state_root)
    saved = legacy.create(
        mutation_request_id="i6a-v1-existing",
        source_yaml=_source("i6a-v1-existing"),
        max_live_codex_jobs=1,
    )
    repository = StrictWorkflowDefinitionRepository(state_root)
    app = _app(tmp_path, repository=repository)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.post(
            _BASE,
            headers=_auth(),
            json={
                **_payload("i6a-v1-no-migration", _source("i6a-v1-no-migration")),
                "contract": _contract(),
            },
        )

    assert response.status_code == 503
    assert response.json() == {"error": "workflow_contract_unavailable"}
    assert legacy.get(saved.definition_id).revision == 1


@pytest.mark.asyncio
async def test_strict_http_create_detail_list_and_graph_project_allowlisted_contract_fields(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    app = _app(tmp_path, repository=repository)
    contract = _contract()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        validation = await client.post(
            f"{_BASE}/validate",
            headers=_auth(),
            json={
                "source_yaml": _source("i6a-v2-validate"),
                "max_live_codex_jobs": 1,
                "contract": contract,
            },
        )
        created = await client.post(
            _BASE,
            headers=_auth(),
            json={**_payload("i6a-v2-create", _source("i6a-v2-create")), "contract": contract},
        )
        definition_id = created.json()["definition_id"]
        detail = await client.get(f"{_BASE}/{definition_id}", headers=_auth())
        listing = await client.get(_BASE, headers=_auth())
        graph = await client.get(f"{_BASE}/{definition_id}/graph", headers=_auth())

    assert validation.status_code == 200, validation.text
    assert validation.json()["contract_version"] == 1
    assert validation.json()["contract_sha256"]
    assert "binding_digest" not in validation.json()
    assert created.status_code == 201
    assert created.headers["etag"] == created.json()["etag"]
    assert created.json()["contract_version"] == 1
    assert created.json()["contract_sha256"]
    assert created.json()["binding_digest"]
    assert detail.status_code == 200
    assert detail.headers["etag"] == detail.json()["etag"]
    assert detail.json()["contract"] == contract
    assert detail.json()["binding_digest"] == created.json()["binding_digest"]
    summary = next(
        item for item in listing.json()["definitions"] if item["definition_id"] == definition_id
    )
    assert summary["contract_version"] == 1
    assert summary["contract_sha256"] == created.json()["contract_sha256"]
    assert "contract" not in summary
    assert graph.status_code == 200
    assert graph.json()["contract_version"] == 1
    assert graph.json()["contract_sha256"] == created.json()["contract_sha256"]
    assert graph.json()["binding_digest"] == created.json()["binding_digest"]


@pytest.mark.asyncio
async def test_strict_http_update_requires_contract_supports_replay_and_rejects_wrong_etag(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    app = _app(tmp_path, repository=repository)
    contract = _contract()
    created = repository.create(
        mutation_request_id="i6a-update-create",
        source_yaml=_source("i6a-update-original"),
        max_live_codex_jobs=1,
        contract=contract,
    )

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        missing = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json=_payload("i6a-update-missing", _source("i6a-update-missing")),
        )
        null_contract = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json={
                **_payload("i6a-update-null", _source("i6a-update-null")),
                "contract": None,
            },
        )
        assert store.get(created.definition_id).revision == 1
        updated_body = {
            **_payload("i6a-update-replay", _source("i6a-update-next")),
            "contract": contract,
        }
        updated = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json=updated_body,
        )
        advanced = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": updated.json()["etag"]}),
            json={
                **_payload("i6a-update-advance", _source("i6a-update-advanced")),
                "contract": contract,
            },
        )
        replayed = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json=updated_body,
        )
        conflict = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json={
                **_payload("i6a-update-replay", _source("i6a-update-next")),
                "contract": _contract(default="changed"),
            },
        )
        wrong_version_etag = created.etag.replace(
            "workflow-definition-v2-", "workflow-definition-v1-"
        )
        wrong_version = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": wrong_version_etag}),
            json=updated_body,
        )
        stale = await client.put(
            f"{_BASE}/{created.definition_id}",
            headers=_auth(**{"If-Match": created.etag}),
            json={
                **_payload("i6a-update-stale", _source("i6a-update-stale")),
                "contract": contract,
            },
        )

    assert missing.status_code == 400
    assert missing.json() == {"error": "workflow_contract_required"}
    assert null_contract.status_code == 400
    assert null_contract.json() == {"error": "workflow_contract_required"}
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["etag"].startswith('"workflow-definition-v2-')
    assert advanced.status_code == 200
    assert advanced.json()["revision"] == 3
    assert replayed.status_code == 200
    assert replayed.json()["revision"] == 2
    assert replayed.json()["etag"] == updated.json()["etag"]
    assert conflict.status_code == 409
    assert conflict.json() == {"error": "mutation_conflict"}
    assert wrong_version.status_code == 412
    assert stale.status_code == 412
    assert store.get(created.definition_id).revision == 3


@pytest.mark.asyncio
async def test_legacy_v2_revision_transitions_to_strict_without_contract_downgrade(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    app = _app(tmp_path, repository=repository)
    legacy = repository.create(
        mutation_request_id="i6a-legacy-v2-create",
        source_yaml=_source("i6a-legacy-v2"),
        max_live_codex_jobs=1,
    )
    contract = _contract()

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        legacy_detail = await client.get(f"{_BASE}/{legacy.definition_id}", headers=_auth())
        legacy_list = await client.get(_BASE, headers=_auth())
        strict_update = await client.put(
            f"{_BASE}/{legacy.definition_id}",
            headers=_auth(**{"If-Match": legacy.etag}),
            json={
                **_payload("i6a-legacy-to-strict", _source("i6a-now-strict")),
                "contract": contract,
            },
        )
        downgrade = await client.put(
            f"{_BASE}/{legacy.definition_id}",
            headers=_auth(**{"If-Match": strict_update.json()["etag"]}),
            json=_payload("i6a-strict-to-legacy", _source("i6a-downgrade")),
        )

    assert "contract" not in legacy_detail.json()
    assert "contract_version" not in legacy_detail.json()
    legacy_summary = next(
        item
        for item in legacy_list.json()["definitions"]
        if item["definition_id"] == legacy.definition_id
    )
    assert "contract_version" not in legacy_summary
    assert "contract_sha256" not in legacy_summary
    assert "binding_digest" not in legacy_summary
    assert strict_update.status_code == 200
    assert strict_update.json()["contract_version"] == 1
    assert strict_update.json()["etag"].startswith('"workflow-definition-v2-')
    assert downgrade.status_code == 400
    assert downgrade.json() == {"error": "workflow_contract_required"}
    assert store.get(legacy.definition_id).revision == 2


@pytest.mark.asyncio
async def test_saved_strict_definition_is_rejected_before_legacy_run_start_side_effects(
    tmp_path: Path,
) -> None:
    from tests.test_workflows_service import FakeWorkerManager, _workflow_yaml

    state_root = tmp_path / "prepared-v2"
    store = StrictWorkflowDefinitionStore(state_root)
    store.initialize_new_database()
    repository = StrictWorkflowDefinitionRepository(state_root, store=store)
    saved = repository.create(
        mutation_request_id="i6a-strict-start",
        source_yaml=_workflow_yaml(),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FakeWorkerManager(tmp_path / "run-state", workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)

    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="i6a-strict-start-client",
            input_data={},
            workspace_root=str(workspace),
        )

    assert manager.start_calls == []
    assert not manager.state_root.exists()
