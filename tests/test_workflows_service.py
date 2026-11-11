from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.service import (
    WorkflowConcurrencyConflict,
    WorkflowReconciliationRequired,
    WorkflowRequestConflict,
    WorkflowRequestError,
    WorkflowService,
    WorkflowStartFailed,
)


class FakeWorkerManager:
    def __init__(self, state_root: Path, workspace: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(workspace),)
        self.start_calls: list[tuple[Path, Path | None]] = []
        self.start_error: Exception | None = None

    def create_run_directory(self) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.state_root / f"run-{uuid.uuid4().hex}"
        run_dir.mkdir()
        return run_dir

    async def start_worker(
        self,
        run_dir: Path,
        *,
        mode: str,
        workspace_root: Path | None = None,
    ) -> object:
        assert mode == "start"
        self.start_calls.append((run_dir, workspace_root))
        if self.start_error is not None:
            raise self.start_error
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


def _workflow_yaml(prompt: str = SAFE_CODEX_JOB_PROMPT) -> str:
    prompt_value = json.dumps(prompt, ensure_ascii=False)
    return "\n".join(
        (
            "document:",
            '  dsl: "1.0.3"',
            "  namespace: codexbridge",
            "  name: fixed-codex-job",
            '  version: "1.0.0"',
            "evaluate:",
            "  language: jq",
            "  mode: strict",
            "do:",
            "  - calculate:",
            "      call: codex-job",
            "      with:",
            f"        prompt: {prompt_value}",
            "",
        )
    )


@pytest.fixture
def service_fixture(tmp_path: Path) -> tuple[WorkflowService, FakeWorkerManager, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FakeWorkerManager(tmp_path / "workflow-state", workspace)
    return WorkflowService(manager, codex_bridge_port=18001), manager, workspace


@pytest.mark.asyncio
async def test_same_request_id_reuses_durable_run_after_service_recreation(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    prompt = "日本の首都を一語で答えて"
    arguments = {
        "client_request_id": "phase5c-request-0001",
        "workflow_yaml": _workflow_yaml(prompt),
        "input_data": {"answer": 4},
        "workspace_root": str(workspace),
    }

    accepted = await service.start_workflow(**arguments)
    recreated_manager = FakeWorkerManager(manager.state_root, workspace)
    recreated = WorkflowService(recreated_manager, codex_bridge_port=18001)
    replay_arguments = {
        **arguments,
        "workflow_yaml": "# semantically identical\n" + _workflow_yaml(prompt),
    }
    replayed = await recreated.start_workflow(**replay_arguments)

    assert replayed.workflow_id == accepted.workflow_id
    assert replayed.run_id == accepted.run_id
    assert replayed.status == "running"
    assert len(manager.start_calls) == 1
    assert recreated_manager.start_calls == []


@pytest.mark.asyncio
async def test_request_id_with_different_payload_conflicts_without_second_spawn(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    await service.start_workflow(
        client_request_id="phase5c-request-0002",
        workflow_yaml=_workflow_yaml(),
        input_data={"answer": 4},
        workspace_root=str(workspace),
    )

    with pytest.raises(WorkflowRequestConflict):
        await service.start_workflow(
            client_request_id="phase5c-request-0002",
            workflow_yaml=_workflow_yaml("同じrequest IDで別promptへ変更"),
            input_data={"answer": 4},
            workspace_root=str(workspace),
        )

    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_whitespace_only_codex_prompt_is_rejected_before_creating_run(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture

    with pytest.raises(WorkflowRequestError):
        await service.start_workflow(
            client_request_id="phase5c-request-0003",
            workflow_yaml=_workflow_yaml(" \n\t"),
            input_data={},
            workspace_root=str(workspace),
        )

    assert manager.start_calls == []
    assert not manager.state_root.exists()


@pytest.mark.asyncio
async def test_concurrent_identical_requests_start_exactly_one_worker(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    arguments = {
        "client_request_id": "phase5c-request-concurrent",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {},
        "workspace_root": str(workspace),
    }

    first, second = await asyncio.gather(
        service.start_workflow(**arguments),
        service.start_workflow(**arguments),
    )

    assert first.workflow_id == second.workflow_id
    assert first.run_id == second.run_id
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_different_live_request_ids_share_one_active_slot(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture

    async def start(request_id: str):
        return await service.start_workflow(
            client_request_id=request_id,
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(workspace),
        )

    outcomes = await asyncio.gather(
        start("phase5c-request-slot-01"),
        start("phase5c-request-slot-02"),
        return_exceptions=True,
    )

    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    assert sum(isinstance(value, WorkflowConcurrencyConflict) for value in outcomes) == 1
    assert len(manager.start_calls) == 1


class WorkflowStartTestError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_uncertain_worker_start_is_persisted_and_never_retried(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    manager.start_error = WorkflowStartTestError("fake uncertain start")
    arguments = {
        "client_request_id": "phase5c-request-uncertain",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {},
        "workspace_root": str(workspace),
    }

    with pytest.raises(WorkflowReconciliationRequired):
        await service.start_workflow(**arguments)
    manager.start_error = None
    restarted_manager = FakeWorkerManager(manager.state_root, workspace)
    replay = await WorkflowService(restarted_manager, codex_bridge_port=18001).start_workflow(
        **arguments
    )

    assert replay.status == "needs_reconciliation"
    assert len(manager.start_calls) == 1
    assert restarted_manager.start_calls == []


@pytest.mark.asyncio
async def test_confirmed_manager_failure_is_durable_without_retry(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    from codex_bridge.workflows.manager import WorkflowManagerError

    manager.start_error = WorkflowManagerError("worker process could not be started")
    arguments = {
        "client_request_id": "phase5c-request-not-started",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {},
        "workspace_root": str(workspace),
    }

    with pytest.raises(WorkflowStartFailed):
        await service.start_workflow(**arguments)
    manager.start_error = None
    replay = await WorkflowService(manager, codex_bridge_port=18001).start_workflow(**arguments)

    assert replay.status == "failed_to_start"
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_manifest_matches_existing_live_worker_contract(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    tmp_path: Path,
) -> None:
    service, manager, workspace = service_fixture
    prompt = "東京ではなく日本の首都を一語で答えて"
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-manifest",
        workflow_yaml=_workflow_yaml(prompt),
        input_data={"value": 4},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    from codex_bridge.workflows.worker import load_run_request

    loaded_manifest, definition_text, input_data = load_run_request(run_dir)

    assert manifest == loaded_manifest
    assert manifest["workflow_id"] == accepted.workflow_id
    assert manifest["mode"] == "codex-job-live"
    assert manifest["control"]["codex_job_live"] is True
    assert manifest["control"]["codex_bridge_port"] == 18001
    assert manifest["definition_sha256"]
    assert manifest["input_sha256"]
    assert manifest["max_tasks"] == 10_000
    assert manifest["control"]["codex_job_max_polls"] == 600
    assert manifest["control"]["codex_job_poll_interval_seconds"] == 0.2
    assert definition_text.endswith("\n")
    assert input_data == {"value": 4}
    definition = json.loads(definition_text)
    assert definition["do"][0]["calculate"]["with"]["prompt"] == prompt

    reference_manager = FakeWorkerManager(tmp_path / "reference-state", workspace)
    await WorkflowService(reference_manager, codex_bridge_port=18001).start_workflow(
        client_request_id="phase5c-request-manifest-reference",
        workflow_yaml=_workflow_yaml(),
        input_data={"value": 4},
        workspace_root=str(workspace),
    )
    reference_manifest = json.loads(
        (reference_manager.start_calls[0][0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["definition_sha256"] != reference_manifest["definition_sha256"]


@pytest.mark.asyncio
async def test_status_and_result_use_read_only_sqlite_and_verify_saved_result(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-result",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (accepted.workflow_id,)
        )
    result_body = {
        "workflow_id": accepted.workflow_id,
        "result": {"final_text": "4"},
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
    }
    (run_dir / "result.json").write_text(json.dumps(result_body), encoding="utf-8")

    def writes_are_forbidden(*_args, **_kwargs):
        raise AssertionError("GET status/result must not write SQLite")

    monkeypatch.setattr("codex_bridge.workflows.service._connect_write", writes_are_forbidden)
    status = await service.get_workflow(accepted.workflow_id)
    saved = await service.get_result(accepted.workflow_id)
    listed = await service.list_workflows()

    assert status.status == "succeeded"
    assert status.terminal is True
    assert saved.result == result_body
    assert listed[0].workflow_id == accepted.workflow_id


@pytest.mark.asyncio
async def test_success_projects_running_until_worker_entry_completes(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-success-transition",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (accepted.workflow_id,)
        )

    before_result = await service.get_workflow(accepted.workflow_id)
    assert before_result.status == "running"

    result_body = {
        "workflow_id": accepted.workflow_id,
        "result": {"final_text": "4"},
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
    }
    (run_dir / "result.json").write_text(json.dumps(result_body), encoding="utf-8")
    after_result = await service.get_workflow(accepted.workflow_id)
    assert after_result.status == "running"

    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    completed = await service.get_workflow(accepted.workflow_id)

    assert completed.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_state", "dbos_status", "job_state", "expected"),
    [
        ("running", "SUCCESS", "FAILED", "failed"),
        ("running", "ERROR", None, "failed"),
        ("failed", "SUCCESS", None, "failed_to_start"),
    ],
    ids=["failed_job", "dbos_error", "worker_failed"],
)
async def test_explicit_failure_states_are_not_overridden_by_saved_result(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    worker_state: str,
    dbos_status: str,
    job_state: str | None,
    expected: str,
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-failed-job",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, ?)", (accepted.workflow_id, dbos_status)
        )
    if job_state is not None:
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute(
                "CREATE TABLE jobs (job_id TEXT, workflow_id TEXT, state TEXT, "
                "thread_id TEXT, turn_id TEXT)"
            )
            connection.execute(
                "INSERT INTO jobs VALUES ('job-1', ?, ?, NULL, NULL)",
                (accepted.workflow_id, job_state),
            )
    if worker_state == "failed":
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "failed", "exit_code": 1, "error_code": "worker_failed"}),
            encoding="utf-8",
        )
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "workflow_id": accepted.workflow_id,
                "result": {"final_text": "4"},
                "definition_sha256": manifest["definition_sha256"],
                "input_sha256": manifest["input_sha256"],
            }
        ),
        encoding="utf-8",
    )

    snapshot = await service.get_workflow(accepted.workflow_id)

    assert snapshot.status == expected


@pytest.mark.asyncio
async def test_maybe_sent_and_tampered_snapshot_require_reconciliation(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-maybe-sent",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    jobs_database = run_dir / "codex_jobs.sqlite"
    connection = sqlite3.connect(jobs_database)
    try:
        connection.execute(
            "CREATE TABLE jobs (job_id TEXT, workflow_id TEXT, state TEXT, "
            "thread_id TEXT, turn_id TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES ('job-1', ?, 'MAYBE_SENT', NULL, NULL)",
            (accepted.workflow_id,),
        )
        connection.commit()
    finally:
        connection.close()

    maybe_sent = await service.get_workflow(accepted.workflow_id)
    assert maybe_sent.status == "needs_reconciliation"
    assert maybe_sent.terminal is False

    jobs_database.unlink()
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workflow_id"] = str(uuid.uuid4())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tampered = await service.get_workflow(accepted.workflow_id)

    assert tampered.status == "needs_reconciliation"


@pytest.mark.asyncio
async def test_completed_worker_without_result_is_not_reported_as_success(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-missing-result",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (accepted.workflow_id,)
        )

    status = await service.get_workflow(accepted.workflow_id)
    result = await service.get_result(accepted.workflow_id)

    assert status.status == "unknown"
    assert result.workflow.status == "unknown"
    assert result.result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["codex_job", "dbos"])
async def test_run_local_ledgers_from_another_workflow_require_reconciliation(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    ledger: str,
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-cross-mix",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    other_workflow_id = str(uuid.uuid4())
    database_name = "codex_jobs.sqlite" if ledger == "codex_job" else "dbos.sqlite"
    connection = sqlite3.connect(run_dir / database_name)
    try:
        if ledger == "codex_job":
            connection.execute(
                "CREATE TABLE jobs (job_id TEXT, workflow_id TEXT, state TEXT, "
                "thread_id TEXT, turn_id TEXT)"
            )
            connection.execute(
                "INSERT INTO jobs VALUES ('job-other', ?, 'SUCCEEDED', NULL, NULL)",
                (other_workflow_id,),
            )
        else:
            connection.execute(
                "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, "
                "status TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (other_workflow_id,)
            )
        connection.commit()
    finally:
        connection.close()

    status = await service.get_workflow(accepted.workflow_id)

    assert status.status == "needs_reconciliation"


@pytest.mark.asyncio
async def test_oversized_result_is_unknown_and_not_returned(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    accepted = await service.start_workflow(
        client_request_id="phase5c-request-large-result",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (accepted.workflow_id,)
        )
    result_body = {
        "workflow_id": accepted.workflow_id,
        "result": "x" * (64 * 1024),
        "definition_sha256": manifest["definition_sha256"],
        "input_sha256": manifest["input_sha256"],
    }
    (run_dir / "result.json").write_text(json.dumps(result_body), encoding="utf-8")

    result = await service.get_result(accepted.workflow_id)

    assert result.workflow.status == "unknown"
    assert result.result is None


@pytest.mark.asyncio
async def test_list_is_newest_first_and_bounded_and_terminal_runs_release_slot(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    first = await service.start_workflow(
        client_request_id="phase5c-request-list-first",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    first_run = manager.start_calls[0][0]
    manifest = json.loads((first_run / "manifest.json").read_text(encoding="utf-8"))
    (first_run / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
        encoding="utf-8",
    )
    connection = sqlite3.connect(first_run / "dbos.sqlite")
    try:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (first.workflow_id,)
        )
        connection.commit()
    finally:
        connection.close()
    (first_run / "result.json").write_text(
        json.dumps(
            {
                "workflow_id": first.workflow_id,
                "result": {"final_text": "4"},
                "definition_sha256": manifest["definition_sha256"],
                "input_sha256": manifest["input_sha256"],
            }
        ),
        encoding="utf-8",
    )

    second = await service.start_workflow(
        client_request_id="phase5c-request-list-second",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace),
    )
    newest = await service.list_workflows(limit=1)
    all_workflows = await service.list_workflows(limit=100)

    assert len(manager.start_calls) == 2
    assert len(newest) == 1
    assert newest[0].workflow_id == second.workflow_id
    assert [record.workflow_id for record in all_workflows] == [
        second.workflow_id,
        first.workflow_id,
    ]
    with pytest.raises(WorkflowRequestError):
        await service.list_workflows(limit=101)
