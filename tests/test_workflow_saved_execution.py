from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from codex_bridge.workflows import service as workflow_service_module
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.service import (
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowServiceUnavailable,
)
from tests.test_workflows_service import FakeWorkerManager, _workflow_yaml


def _saved_fixture(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "workflow-state"
    repository = WorkflowDefinitionRepository(state_root)
    manager = FakeWorkerManager(state_root, workspace, allowed_roots=(tmp_path,))
    monkeypatch.setattr(workflow_service_module, "missing_workflow_dependencies", lambda: [])
    return repository, manager, WorkflowService(manager, codex_bridge_port=18001), workspace


def _create(repository: WorkflowDefinitionRepository, request_id: str, source: str):
    return repository.create(
        mutation_request_id=request_id,
        source_yaml=source,
        max_live_codex_jobs=1,
    )


@pytest.mark.asyncio
async def test_saved_execution_commits_source_mapping_and_replays_fixed_revision(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-execution-one", _workflow_yaml("saved prompt"))
    arguments = {
        "repository": repository,
        "definition_id": saved.definition_id,
        "revision": saved.revision,
        "client_request_id": "saved-execution-request-1",
        "input_data": {"answer": 4},
        "workspace_root": str(workspace),
    }

    accepted = await service.start_saved_workflow(**arguments)
    replayed = await service.start_saved_workflow(**arguments)

    assert (replayed.workflow_id, replayed.run_id) == (accepted.workflow_id, accepted.run_id)
    assert len(manager.start_calls) == 1
    with sqlite3.connect(state_root := manager.state_root / "requests.sqlite") as connection:
        request = connection.execute(
            "SELECT run_id FROM workflow_requests WHERE client_request_id = ?",
            (arguments["client_request_id"],),
        ).fetchone()
        mapping = connection.execute(
            "SELECT definition_id, revision, source_sha256 FROM workflow_request_sources "
            "WHERE run_id = ?",
            (accepted.run_id,),
        ).fetchone()
    assert request == (accepted.run_id,)
    assert mapping == (saved.definition_id, saved.revision, saved.source_sha256)
    run_dir = state_root.parent / accepted.run_id
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["definition_sha256"] == saved.canonical_sha256
    assert manifest["definition_sha256"] != saved.source_sha256
    restarted_manager = FakeWorkerManager(manager.state_root, workspace, allowed_roots=(tmp_path,))
    restarted = WorkflowService(restarted_manager, codex_bridge_port=18001)
    replayed_after_restart = await restarted.start_saved_workflow(**arguments)
    assert replayed_after_restart.run_id == accepted.run_id
    assert restarted_manager.start_calls == []


@pytest.mark.asyncio
async def test_saved_and_inline_requests_conflict_on_source_identity_even_for_same_ast(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    source = _workflow_yaml("same semantic workflow")
    first = _create(repository, "save-identity-one", source)

    await service.start_saved_workflow(
        repository=repository,
        definition_id=first.definition_id,
        revision=first.revision,
        client_request_id="saved-then-inline-req",
        input_data={},
        workspace_root=str(workspace),
    )
    with pytest.raises(WorkflowRequestConflict):
        await service.start_workflow(
            client_request_id="saved-then-inline-req",
            workflow_yaml=source,
            input_data={},
            workspace_root=str(workspace),
        )

    workspace_inline = tmp_path / "workspace-inline"
    workspace_inline.mkdir()
    await service.start_workflow(
        client_request_id="inline-then-saved-req",
        workflow_yaml=source,
        input_data={},
        workspace_root=str(workspace_inline),
    )
    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=first.definition_id,
            revision=first.revision,
            client_request_id="inline-then-saved-req",
            input_data={},
            workspace_root=str(workspace_inline),
        )

    assert len(manager.start_calls) == 2


@pytest.mark.asyncio
async def test_same_ast_from_another_definition_conflicts_on_replay(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    source = _workflow_yaml("same semantic workflow")
    first = _create(repository, "save-other-ref-one", source)
    second = _create(repository, "save-other-ref-two", source)
    await service.start_saved_workflow(
        repository=repository,
        definition_id=first.definition_id,
        revision=first.revision,
        client_request_id="saved-other-ref-request",
        input_data={},
        workspace_root=str(workspace),
    )

    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=second.definition_id,
            revision=second.revision,
            client_request_id="saved-other-ref-request",
            input_data={},
            workspace_root=str(workspace),
        )
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_same_ast_from_another_saved_revision_conflicts_on_replay(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    source = _workflow_yaml("same semantic workflow")
    first = _create(repository, "save-revision-identity", source)
    second = repository.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="save-revision-identity-two",
        source_yaml="# semantically identical revision\n" + source,
        max_live_codex_jobs=1,
    )
    assert second.revision == 2

    await service.start_saved_workflow(
        repository=repository,
        definition_id=first.definition_id,
        revision=1,
        client_request_id="saved-revision-identity-request",
        input_data={},
        workspace_root=str(workspace),
    )
    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=first.definition_id,
            revision=2,
            client_request_id="saved-revision-identity-request",
            input_data={},
            workspace_root=str(workspace),
        )
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_saved_replay_conflicts_when_input_or_workspace_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-payload-identity", _workflow_yaml())
    arguments = {
        "repository": repository,
        "definition_id": saved.definition_id,
        "revision": saved.revision,
        "client_request_id": "saved-payload-identity-request",
        "input_data": {"value": 1},
        "workspace_root": str(workspace),
    }
    await service.start_saved_workflow(**arguments)

    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(**{**arguments, "input_data": {"value": 2}})
    another_workspace = tmp_path / "another-workspace"
    another_workspace.mkdir()
    with pytest.raises(WorkflowRequestConflict):
        await service.start_saved_workflow(
            **{**arguments, "workspace_root": str(another_workspace)}
        )
    assert len(manager.start_calls) == 1


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("source_sha256", "0" * 64), ("canonical_sha256", "0" * 64)],
)
@pytest.mark.asyncio
async def test_saved_definition_hash_mismatch_is_rejected_before_worker_start(
    tmp_path: Path, monkeypatch, field: str, replacement: str
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-hash-mismatch", _workflow_yaml())
    monkeypatch.setattr(
        repository,
        "get",
        lambda definition_id, revision: replace(saved, **{field: replacement}),
    )

    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="saved-hash-mismatch-request",
            input_data={},
            workspace_root=str(workspace),
        )
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_saved_execution_rechecks_allowed_roots_and_state_root_separation(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-path-recheck", _workflow_yaml())
    outside = tmp_path.parent / f"workflow-saved-outside-{tmp_path.name}"
    outside.mkdir()

    with pytest.raises(WorkflowRequestError):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="saved-outside-root-request",
            input_data={},
            workspace_root=str(outside),
        )
    with pytest.raises(WorkflowRequestError):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="saved-state-overlap-request",
            input_data={},
            workspace_root=str(manager.state_root),
        )
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_saved_reservation_rolls_back_both_rows_when_mapping_insert_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-rollback-one", _workflow_yaml())

    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_requests ("
            "client_request_id TEXT NOT NULL UNIQUE, workflow_id TEXT NOT NULL UNIQUE, "
            "run_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE workflow_request_sources ("
            "run_id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, revision INTEGER NOT NULL, "
            "source_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TRIGGER fail_source_insert BEFORE INSERT ON workflow_request_sources "
            "BEGIN SELECT RAISE(ABORT, 'source insert failure'); END"
        )

    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="saved-rollback-request",
            input_data={},
            workspace_root=str(workspace),
        )

    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_requests").fetchone()[0] == 0
        source_count = connection.execute(
            "SELECT COUNT(*) FROM workflow_request_sources"
        ).fetchone()[0]
        assert source_count == 0
    assert manager.start_calls == []


@pytest.mark.asyncio
async def test_legacy_request_database_reads_without_creating_source_table(
    tmp_path: Path, monkeypatch
) -> None:
    repository, manager, service, workspace = _saved_fixture(tmp_path, monkeypatch)
    accepted = await service.start_workflow(
        client_request_id="legacy-inline-request",
        workflow_yaml=_workflow_yaml("legacy prompt"),
        input_data={"answer": 4},
        workspace_root=str(workspace),
    )
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        connection.execute("DROP TABLE workflow_request_sources")

    assert (await service.get_workflow(accepted.workflow_id)).workflow_id == accepted.workflow_id
    replayed = await service.start_workflow(
        client_request_id="legacy-inline-request",
        workflow_yaml=_workflow_yaml("legacy prompt"),
        input_data={"answer": 4},
        workspace_root=str(workspace),
    )
    assert replayed.run_id == accepted.run_id
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        source_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'workflow_request_sources'"
        ).fetchone()
    assert source_table is None
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_saved_source_replay_second_check_is_atomic_across_services(
    tmp_path: Path, monkeypatch
) -> None:
    repository, first_manager, first_service, workspace = _saved_fixture(tmp_path, monkeypatch)
    saved = _create(repository, "save-race-one", _workflow_yaml("race prompt"))
    second_manager = FakeWorkerManager(first_manager.state_root, workspace)
    second_service = WorkflowService(second_manager, codex_bridge_port=18001)
    original_find = workflow_service_module._find_request
    barrier = threading.Barrier(2)

    def synchronized_find(state_root: Path, request_id: str):
        barrier.wait(timeout=5)
        return original_find(state_root, request_id)

    monkeypatch.setattr(workflow_service_module, "_find_request", synchronized_find)

    async def start(service: WorkflowService):
        return await service.start_saved_workflow(
            repository=repository,
            definition_id=saved.definition_id,
            revision=saved.revision,
            client_request_id="saved-race-request",
            input_data={},
            workspace_root=str(workspace),
        )

    first, second = await asyncio.gather(start(first_service), start(second_service))
    assert first.run_id == second.run_id
    assert len(first_manager.start_calls) + len(second_manager.start_calls) == 1
    with sqlite3.connect(first_manager.state_root / "requests.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workflow_requests WHERE client_request_id = ?",
                ("saved-race-request",),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workflow_request_sources WHERE run_id = ?",
                (first.run_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_saved_source_identity_is_rechecked_inside_reservation_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    repository, first_manager, first_service, workspace = _saved_fixture(tmp_path, monkeypatch)
    source = _workflow_yaml("same AST for racing definitions")
    first_saved = _create(repository, "save-race-identity-one", source)
    second_saved = _create(repository, "save-race-identity-two", source)
    second_manager = FakeWorkerManager(
        first_manager.state_root, workspace, allowed_roots=(tmp_path,)
    )
    second_service = WorkflowService(second_manager, codex_bridge_port=18001)
    original_find = workflow_service_module._find_request
    barrier = threading.Barrier(2)

    def synchronized_find(state_root: Path, request_id: str):
        barrier.wait(timeout=5)
        return original_find(state_root, request_id)

    monkeypatch.setattr(workflow_service_module, "_find_request", synchronized_find)

    async def start(service: WorkflowService, definition_id: str):
        return await service.start_saved_workflow(
            repository=repository,
            definition_id=definition_id,
            revision=1,
            client_request_id="saved-racing-source-identity",
            input_data={},
            workspace_root=str(workspace),
        )

    results = await asyncio.gather(
        start(first_service, first_saved.definition_id),
        start(second_service, second_saved.definition_id),
        return_exceptions=True,
    )
    assert sum(isinstance(result, WorkflowRequestConflict) for result in results) == 1
    successful = [result for result in results if not isinstance(result, BaseException)]
    assert len(successful) == 1
    assert len(first_manager.start_calls) + len(second_manager.start_calls) == 1
