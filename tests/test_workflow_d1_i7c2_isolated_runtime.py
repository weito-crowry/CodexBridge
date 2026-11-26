from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

import codex_bridge.workflows.service as service_module
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.manager import WorkflowManagerError, WorkflowWorkerManager
from codex_bridge.workflows.service import (
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowService,
    WorkflowServiceUnavailable,
    _find_workflow,
    _inspect_run,
)
from codex_bridge.workflows.strict_definition_repository import StrictWorkflowDefinitionRepository
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_requests import StrictWorkflowRequestStore
from tests.test_workflow_d1_i7b_variable_runtime import _contract, _reserved_adapter, _source


def _prepared_definition(
    state_root: Path,
    *,
    output: str = "${ {} }",
) -> tuple[StrictWorkflowDefinitionStore, Any, dict[str, Any]]:
    StrictWorkflowRequestStore(state_root).initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    contract = _contract()
    input_schema = contract["input_schema"]
    assert isinstance(input_schema, dict)
    input_schema["properties"] = {"value": {"type": "integer"}}
    revision = definitions.create(
        mutation_request_id="i7c2-definition-create-0001",
        source_yaml=_source(output=output),
        max_live_codex_jobs=1,
        contract=contract,
    )
    return definitions, revision, contract


def _manager(state_root: Path, workspace: Path, *, enabled: bool) -> WorkflowWorkerManager:
    return WorkflowWorkerManager(
        state_root=state_root,
        allowed_roots=(str(workspace.resolve()),),
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.5,
        enable_isolated_strict_runtime=enabled,
    )


@pytest.mark.asyncio
async def test_default_strict_admission_stays_disabled_without_allocating_a_run(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _definitions, revision, _contract_value = _prepared_definition(state_root)
    manager = _manager(state_root, workspace, enabled=False)
    service = WorkflowService(manager, codex_bridge_port=18001)

    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(state_root),
            definition_id=revision.definition_id,
            revision=revision.revision,
            client_request_id="i7c2-disabled-request-01",
            input_data={"value": 1},
            workspace_root=str(workspace.resolve()),
        )

    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        count = connection.execute("SELECT COUNT(*) FROM workflow_requests").fetchone()[0]
    assert count == 0
    assert not tuple(state_root.glob("run-*"))


@pytest.mark.asyncio
async def test_enabled_strict_admission_does_not_prepare_missing_request_storage(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    revision = definitions.create(
        mutation_request_id="i7c2-unprepared-definition-001",
        source_yaml=_source(),
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    manager = _manager(state_root, workspace, enabled=True)
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
    )

    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(state_root),
            definition_id=revision.definition_id,
            revision=revision.revision,
            client_request_id="i7c2-unprepared-request-001",
            input_data={},
            workspace_root=str(workspace.resolve()),
        )

    assert not (state_root / "requests.sqlite").exists()
    assert not tuple(state_root.glob("run-*"))
    assert manager.get_status("run-0123456789abcdef0123456789abcdef") is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_codex_job_admission_stays_disabled_without_internal_job_opt_in(
    tmp_path: Path,
) -> None:
    adapter, existing_run = _reserved_adapter(
        tmp_path,
        tasks=[
            {
                "calculate": {
                    "call": "codex-job",
                    "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                }
            }
        ],
        output="${ {} }",
    )
    manifest = adapter._verified_manifest
    assert manifest is not None
    state_root = existing_run.parent
    workspace = existing_run.parent.parent / "workspace"
    manager = _manager(state_root, workspace, enabled=True)
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
    )
    with pytest.raises(WorkflowServiceUnavailable):
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(state_root),
            definition_id=manifest.definition_id,
            revision=manifest.definition_revision,
            client_request_id="i7c2-codex-job-gate-001",
            input_data={},
            workspace_root=manifest.workspace_root,
        )

    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        stored_request_ids = {
            str(row[0])
            for row in connection.execute("SELECT client_request_id FROM workflow_requests")
        }
    assert "i7c2-codex-job-gate-001" not in stored_request_ids
    assert tuple(state_root.glob("run-*")) == (existing_run,)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_rejects_disabled_invalid_snapshot_and_workspace_before_spawn(
    tmp_path: Path,
) -> None:
    adapter, run_dir = _reserved_adapter(tmp_path)
    workspace = run_dir.parent.parent / "workspace"
    alternate_workspace = run_dir.parent.parent / "alternate-workspace"
    alternate_workspace.mkdir()
    state_root = run_dir.parent

    disabled_manager = WorkflowWorkerManager(
        state_root=state_root,
        allowed_roots=(str(workspace.resolve()),),
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.5,
    )
    with pytest.raises(WorkflowManagerError):
        await disabled_manager.start_worker(
            run_dir,
            mode="start",
            workspace_root=workspace,
            strict_runtime=True,
        )
    assert not tuple(run_dir.glob("worker-config-*.json"))
    assert disabled_manager.get_status(run_dir.name) is None
    await disabled_manager.shutdown()

    manager = WorkflowWorkerManager(
        state_root=state_root,
        allowed_roots=(str(workspace.resolve()), str(alternate_workspace.resolve())),
        python_executable=sys.executable,
        frozen=False,
        graceful_shutdown_seconds=0.5,
        enable_isolated_strict_runtime=True,
    )
    try:
        with pytest.raises(WorkflowManagerError):
            await manager.start_worker(
                run_dir,
                mode="start",
                workspace_root=alternate_workspace,
                max_live_codex_jobs=adapter.max_live_codex_jobs,
                strict_runtime=True,
            )
        assert not tuple(run_dir.glob("worker-config-*.json"))
        assert manager.get_status(run_dir.name) is None

        (run_dir / "input.json").write_text('{"changed":true}', encoding="utf-8")
        with pytest.raises(WorkflowManagerError):
            await manager.start_worker(
                run_dir,
                mode="start",
                workspace_root=workspace,
                max_live_codex_jobs=adapter.max_live_codex_jobs,
                strict_runtime=True,
            )
        assert not tuple(run_dir.glob("worker-config-*.json"))
        assert manager.get_status(run_dir.name) is None
    finally:
        await manager.shutdown()


def test_worker_entry_rejects_strict_run_without_private_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker_entry

    adapter, run_dir = _reserved_adapter(tmp_path)
    manifest = adapter._verified_manifest
    assert manifest is not None
    config_path = run_dir / "worker-config-legacy.json"
    _write = {
        "version": 1,
        "mode": "start",
        "workspace_root": manifest.workspace_root,
        "allowed_roots": [manifest.workspace_root],
        "shutdown_file": f"worker-shutdown-{'a' * 32}.request",
    }
    config_path.write_text(json.dumps(_write), encoding="utf-8")
    monkeypatch.setattr(
        worker_entry,
        "missing_workflow_dependencies",
        lambda: pytest.fail("dependency import gate ran before strict config rejection"),
    )

    assert (
        worker_entry.main(
            [
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--config",
                str(config_path),
            ]
        )
        == 2
    )
    status = json.loads((run_dir / "worker-entry-status.json").read_text(encoding="utf-8"))
    assert status == {"state": "failed", "exit_code": 2, "error_code": "invalid_config"}
    assert not (run_dir / "start-worker.json").exists()
    assert not (run_dir / "start-worker-identity.json").exists()


@pytest.mark.asyncio
async def test_committed_reservation_is_preserved_when_failure_prevents_worker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _definitions, revision, _contract_value = _prepared_definition(state_root)
    manager = _manager(state_root, workspace, enabled=True)
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
    )
    reserve = service_module._reserve_prepared_strict_run
    committed: list[str] = []

    def reserve_then_lose_ack(*args: Any, **kwargs: Any) -> Any:
        result = reserve(*args, **kwargs)
        committed.append(result.reservation.run_id)
        raise OSError("injected post-commit failure")

    monkeypatch.setattr(service_module, "_reserve_prepared_strict_run", reserve_then_lose_ack)
    with pytest.raises(service_module.WorkflowReconciliationRequired) as caught:
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(state_root),
            definition_id=revision.definition_id,
            revision=revision.revision,
            client_request_id="i7c2-post-reserve-fail-001",
            input_data={"value": 1},
            workspace_root=str(workspace.resolve()),
        )
    assert len(committed) == 1
    assert caught.value.snapshot.run_id == committed[0]
    assert caught.value.snapshot.workflow_id
    assert caught.value.snapshot.status == "needs_reconciliation"
    assert not (state_root / committed[0] / "worker-entry-status.json").exists()
    assert manager.get_status(committed[0]) is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_worker_launch_failure_returns_verified_reservation_ids_without_reader_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _definitions, revision, _contract_value = _prepared_definition(state_root)
    manager = _manager(state_root, workspace, enabled=True)
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
    )

    async def fail_worker_start(*_args: Any, **_kwargs: Any) -> Any:
        raise WorkflowManagerError("injected worker launch failure")

    async def fail_snapshot_read(*_args: Any, **_kwargs: Any) -> Any:
        raise WorkflowServiceUnavailable("injected snapshot reader failure")

    monkeypatch.setattr(manager, "start_worker", fail_worker_start)
    monkeypatch.setattr(service, "_snapshot_for_row", fail_snapshot_read)

    with pytest.raises(WorkflowReconciliationRequired) as caught:
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(state_root),
            definition_id=revision.definition_id,
            revision=revision.revision,
            client_request_id="i7c2-worker-launch-fail-01",
            input_data={"value": 1},
            workspace_root=str(workspace.resolve()),
        )

    snapshot = caught.value.snapshot
    assert snapshot.run_id
    assert snapshot.workflow_id
    assert snapshot.status == "needs_reconciliation"
    assert snapshot._strict_metadata is not None
    assert snapshot._strict_metadata.binding_digest == revision.binding_digest
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        state = connection.execute(
            "SELECT state FROM workflow_requests WHERE client_request_id = ?",
            ("i7c2-worker-launch-fail-01",),
        ).fetchone()[0]
    assert state == "needs_reconciliation"
    assert manager.get_status(snapshot.run_id) is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_opted_in_service_runs_reserved_v2_replays_and_worker_recovers(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definitions, revision, contract = _prepared_definition(state_root)
    manager = _manager(state_root, workspace, enabled=True)
    service = WorkflowService(
        manager,
        codex_bridge_port=18001,
        enable_isolated_strict_runtime=True,
    )
    arguments = {
        "repository": StrictWorkflowDefinitionRepository(state_root),
        "definition_id": revision.definition_id,
        "revision": revision.revision,
        "client_request_id": "i7c2-isolated-request-0001",
        "input_data": {"value": 1},
        "workspace_root": str(workspace.resolve()),
    }
    try:
        started = await service.start_saved_workflow(**arguments)
        assert started.status in {"running", "succeeded"}
        for _ in range(100):
            current = await service.get_workflow(started.workflow_id)
            if current.terminal:
                break
            await asyncio.sleep(0.05)
        assert current.status == "succeeded"
        assert current.worker_state == "completed"
        tasks = await service.get_workflow_tasks(started.workflow_id)
        events = await service.get_workflow_task_events(started.workflow_id, after_id=7, limit=10)
        assert tasks["coverage"] == "partial"
        assert tasks["coverage_reason"] == "workflow_not_confirmed"
        assert tasks["counts"]["executions"] == 0
        assert events["events"] == []
        assert events["next_after_id"] == 7
        assert events["coverage_reason"] == "workflow_not_confirmed"
        result_snapshot = await service.get_result(started.workflow_id)
        assert result_snapshot.result == {
            "workflow_id": started.workflow_id,
            "run_id": started.run_id,
            "status": "succeeded",
            "result": {},
            "run_format_version": 2,
            "contract_version": 1,
            "contract_sha256": revision.contract_sha256,
            "binding_digest": revision.binding_digest,
        }
        assert result_snapshot.workflow._strict_metadata is not None
        assert not (state_root / started.run_id / "result.json").exists()

        replayed = await service.start_saved_workflow(**arguments)
        assert replayed.run_id == started.run_id
        assert replayed.workflow_id == started.workflow_id
        assert replayed.status == "succeeded"
        with pytest.raises(WorkflowRequestConflict):
            await service.start_saved_workflow(**{**arguments, "input_data": {"value": 2}})
        definitions.update(
            definition_id=revision.definition_id,
            expected_revision=revision.revision,
            mutation_request_id="i7c2-definition-update-0001",
            source_yaml=_source(output="${ {} }") + "# revision two\n",
            max_live_codex_jobs=1,
            contract=contract,
        )
        with pytest.raises(WorkflowRequestConflict):
            await service.start_saved_workflow(**{**arguments, "revision": 2})
    finally:
        await manager.shutdown()

    # A new worker process attaches to the already completed DBOS workflow;
    # it does not allocate a request, rewrite the manifest, or run legacy OWS.
    run_dir = state_root / started.run_id
    manifest_bytes = (run_dir / "manifest.json").read_bytes()
    recovery_manager = _manager(state_root, workspace, enabled=True)
    try:
        handle = await recovery_manager.start_worker(
            run_dir,
            mode="recover",
            workspace_root=workspace,
            max_live_codex_jobs=1,
            strict_runtime=True,
        )
        recovered = await handle.wait(60)
        assert recovered.state == "completed"
        assert (run_dir / "manifest.json").read_bytes() == manifest_bytes
        assert json.loads((run_dir / "strict-terminal.json").read_text())["status"] == "succeeded"
    finally:
        await recovery_manager.shutdown()


def test_strict_result_reader_revalidates_bound_terminal_and_ignores_legacy_result(
    tmp_path: Path,
) -> None:
    adapter, run_dir = _reserved_adapter(tmp_path, output="${ {} }")
    workflow_id = adapter.checkpoint_identity["workflow_id"]
    assert isinstance(workflow_id, str)
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    (run_dir / "start-worker.json").write_text(
        json.dumps(
            {
                "mode": "start",
                "pid": 1234,
                "run_dir": str(run_dir),
                "workflow_id": workflow_id,
                "creation_time_100ns": 1,
                "image_path": "worker.exe",
                "timestamp_utc": "2026-11-26T00:00:00+00:00",
                "argv": [],
            }
        ),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (workflow_id,))
    (run_dir / "result.json").write_text(
        json.dumps({"workflow_id": workflow_id, "result": {"fake": "legacy"}}),
        encoding="utf-8",
    )
    (run_dir / "strict-terminal.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "status": "succeeded",
                "reason_code": None,
                "run_id": run_dir.name,
                "workflow_id": workflow_id,
                "binding_identity": adapter.checkpoint_identity,
                "result": 7,
            }
        ),
        encoding="utf-8",
    )

    row = _find_workflow(run_dir.parent, workflow_id)
    assert row is not None
    inspection = _inspect_run(
        run_dir.parent,
        row,
        allowed_roots=(str(run_dir.parent.parent / "workspace"),),
    )

    assert inspection.status == "failed"
    assert inspection.result is None
    assert inspection._strict_metadata is not None
    assert inspection._strict_metadata.reason_code == "output_contract_violation"


@pytest.mark.asyncio
async def test_corrupt_request_source_row_replay_retains_authoritative_run_ids(
    tmp_path: Path,
) -> None:
    adapter, run_dir = _reserved_adapter(tmp_path)
    manifest = adapter._verified_manifest
    assert manifest is not None
    with sqlite3.connect(run_dir.parent / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_request_sources SET source_sha256 = ? WHERE run_id = ?",
            ("0" * 64, run_dir.name),
        )
    manager = _manager(
        run_dir.parent,
        run_dir.parent.parent / "workspace",
        enabled=False,
    )
    service = WorkflowService(manager, codex_bridge_port=18001)

    with pytest.raises(WorkflowReconciliationRequired) as caught:
        await service.start_saved_workflow(
            repository=StrictWorkflowDefinitionRepository(run_dir.parent),
            definition_id=manifest.definition_id,
            revision=manifest.definition_revision,
            client_request_id="i7b-variable-request-0001",
            input_data={},
            workspace_root=manifest.workspace_root,
        )

    assert caught.value.snapshot.run_id == run_dir.name
    assert caught.value.snapshot.workflow_id == manifest.workflow_id
    assert caught.value.snapshot.status == "needs_reconciliation"


def test_persisted_pure_strict_run_has_no_external_shutdown_ownership(
    tmp_path: Path,
) -> None:
    from codex_bridge.workflows.manager import _persisted_live_run_directories

    adapter, run_dir = _reserved_adapter(tmp_path)
    with sqlite3.connect(run_dir.parent / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_requests SET state = 'running' WHERE run_id = ?",
            (run_dir.name,),
        )

    persisted, complete = _persisted_live_run_directories(
        run_dir.parent,
        allowed_roots=(str(run_dir.parent.parent / "workspace"),),
    )

    assert persisted == ()
    assert complete
    assert adapter.checkpoint_identity["workflow_id"]


def test_strict_shutdown_ownership_checks_verified_workflow_and_request_key(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "codex_jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    workflow_id = "7b000000-0000-4000-8000-000000000101"
    run_dir = tmp_path / "run-00000000000000000000000000000001"
    run_dir.mkdir()
    prepared = adapter.prepare_job(
        workflow_id,
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])
    adapter.mark_accepted(
        prepared["job_id"],
        "thread-12345678",
        "turn-12345678",
        "in_progress",
    )

    turns, complete = CodexJobAdapter.read_shutdown_ownership(
        database_path,
        expected_workflow_id=workflow_id,
        expected_task_identities=("do[0].calculate",),
    )
    wrong_turns, wrong_complete = CodexJobAdapter.read_shutdown_ownership(
        database_path,
        expected_workflow_id="7b000000-0000-4000-8000-000000000102",
        expected_task_identities=("do[0].calculate",),
    )
    wrong_identity_turns, wrong_identity_complete = CodexJobAdapter.read_shutdown_ownership(
        database_path,
        expected_workflow_id=workflow_id,
        expected_task_identities=("do[0].unrelated",),
    )

    assert turns == {("thread-12345678", "turn-12345678")}
    assert complete
    assert wrong_turns == set()
    assert not wrong_complete
    assert wrong_identity_turns == set()
    assert not wrong_identity_complete


@pytest.mark.asyncio
async def test_authorized_strict_codex_job_uses_existing_adapter_and_service_reads_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dbos import DBOS, SetWorkflowID

    from codex_bridge.workflows import strict_executor, worker
    from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
    from codex_bridge.workflows.interpreter import validate_workflow
    from codex_bridge.workflows.worker import _dbos_config

    tasks = [
        {
            "outer": {
                "do": [
                    {
                        "calculate": {
                            "call": "codex-job",
                            "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                        }
                    }
                ]
            }
        }
    ]
    adapter, run_dir = _reserved_adapter(
        tmp_path,
        tasks=tasks,
        output="${ {} }",
    )
    assert adapter.pinned_definition is not None
    validate_workflow(
        adapter.pinned_definition,
        codex_job_live=True,
        max_live_codex_jobs=1,
        strict_contract=adapter.compiled_contract,
    )
    calls: list[str] = []

    class FakeCodexHTTPClient:
        def __init__(self, *, port: int) -> None:
            assert port == 18001
            self.port = port
            calls.append("client")

        def preflight(self) -> None:
            calls.append("preflight")

        def create_thread(self, request: dict[str, Any]) -> dict[str, Any]:
            assert request["prompt"] == SAFE_CODEX_JOB_PROMPT
            calls.append("create")
            return {
                "thread_id": "thread-12345678",
                "turn_id": "turn-12345678",
                "state": "in_progress",
            }

        def observe_thread(self, thread_id: str, turn_id: str) -> dict[str, Any]:
            assert thread_id == "thread-12345678"
            assert turn_id == "turn-12345678"
            calls.append("observe")
            return {
                "status": {"state": "completed"},
                "turns": [{"id": turn_id, "status": "completed"}],
                "items": [
                    {
                        "turn_id": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "isolated fake response",
                        },
                    }
                ],
            }

    monkeypatch.setattr(worker, "CodexHTTPClient", FakeCodexHTTPClient)
    monkeypatch.setenv("CODEXBRIDGE_WORKFLOW_BRIDGE_PORT", "18001")
    dbos_config = _dbos_config(run_dir / "dbos.sqlite")
    DBOS(config=dbos_config)
    DBOS.launch()
    try:
        with SetWorkflowID(str(adapter.checkpoint_identity["workflow_id"])):
            handle = DBOS.start_workflow(
                strict_executor.execute_strict_ows_workflow,
                run_dir.name,
                str(run_dir.parent),
                [str(run_dir.parent.parent / "workspace")],
                "authorized_codex",
            )
        try:
            result = handle.get_result()
        except strict_executor.StrictWorkflowExecutionError as error:
            pytest.fail(
                f"strict Codex job failed with safe reason: {error.reason_code}; "
                f"fake calls: {calls}"
            )
    finally:
        DBOS.destroy()

    assert result["status"] == "succeeded"
    assert result["result"] == {}
    assert calls == ["client", "preflight", "create", "client", "observe"]
    turns, complete = CodexJobAdapter.read_shutdown_ownership(
        run_dir / "codex_jobs.sqlite",
        expected_workflow_id=str(adapter.checkpoint_identity["workflow_id"]),
    )
    assert turns == set()
    assert complete
    from codex_bridge.workflows.interpreter import _non_looped_codex_job_task_identities

    with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
        stored_task_identities = tuple(
            row[0] for row in connection.execute("SELECT task_identity FROM jobs ORDER BY rowid")
        )
    assert stored_task_identities == _non_looped_codex_job_task_identities(
        adapter.pinned_definition
    )
    (run_dir / "strict-terminal.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "status": "succeeded",
                "reason_code": None,
                "run_id": run_dir.name,
                "workflow_id": str(adapter.checkpoint_identity["workflow_id"]),
                "binding_identity": result["binding"],
                "result": result["result"],
            }
        ),
        encoding="utf-8",
    )

    workflow_id = str(adapter.checkpoint_identity["workflow_id"])
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    (run_dir / "start-worker.json").write_text(
        json.dumps(
            {
                "mode": "start",
                "pid": 1234,
                "run_dir": str(run_dir),
                "workflow_id": workflow_id,
                "creation_time_100ns": 1,
                "image_path": "worker.exe",
                "timestamp_utc": "2026-11-26T00:00:00+00:00",
                "argv": [],
            }
        ),
        encoding="utf-8",
    )
    workspace = run_dir.parent.parent / "workspace"
    service = WorkflowService(
        _manager(run_dir.parent, workspace, enabled=True), codex_bridge_port=18001
    )

    workflow = await service.get_workflow(workflow_id)
    result_snapshot = await service.get_result(workflow_id)

    assert workflow.status == "succeeded", (
        workflow.worker_state,
        workflow.jobs,
        workflow._strict_metadata.reason_code if workflow._strict_metadata else None,
    )
    assert result_snapshot.workflow.status == "succeeded"
    assert result_snapshot.result == {
        "workflow_id": workflow_id,
        "run_id": run_dir.name,
        "status": "succeeded",
        "result": {},
        "run_format_version": 2,
        "contract_version": 1,
        "contract_sha256": adapter._verified_manifest.contract_sha256,
        "binding_digest": adapter._verified_manifest.binding_digest,
    }
