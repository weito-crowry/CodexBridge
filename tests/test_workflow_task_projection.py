from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_bridge.workflows.codex_job_adapter import (
    _JOB_COLUMNS,
    CodexJobAdapter,
    _job_events_table_sql,
    _job_reconciliation_table_sql,
    _jobs_table_sql,
)
from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import (
    SAFE_CODEX_JOB_PROMPT,
    Interpreter,
    _InterpreterEvent,
    compute_task_action,
    parse_workflow_text,
)
from codex_bridge.workflows.node_events import NodeEventWriter
from codex_bridge.workflows.service import WorkflowService
from codex_bridge.workflows.task_projection import _is_task_identity


def _definition(task_name: str = "private_task") -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "task-projection-fixture",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {task_name: {"set": {"value": "${ .private_secret }"}}},
            {"execute": {"call": "codex-job", "with": {"prompt": SAFE_CODEX_JOB_PROMPT}}},
        ],
    }


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
        self,
        run_dir: Path,
        *,
        mode: str,
        workspace_root: Path | None = None,
        max_live_codex_jobs: int = 1,
    ) -> object:
        assert mode == "start"
        assert workspace_root is not None
        assert max_live_codex_jobs in {1, 2, 3}
        (run_dir / "worker-entry-status.json").write_text(
            json.dumps({"state": "running", "exit_code": None, "error_code": None}),
            encoding="utf-8",
        )
        return object()


async def _running_service(
    tmp_path: Path,
    definition: dict[str, Any] | None = None,
    *,
    input_data: dict[str, Any] | None = None,
    max_live_codex_jobs: int = 1,
):
    root = tmp_path / "state" / "workflows"
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    service = WorkflowService(FakeWorkerManager(root, tmp_path), codex_bridge_port=18001)
    snapshot = await service.start_workflow(
        client_request_id="projection-request-001",
        workflow_yaml=json.dumps(definition or _definition()),
        input_data={"value": 1} if input_data is None else input_data,
        workspace_root=str(workspace),
        max_live_codex_jobs=max_live_codex_jobs,
    )
    return service, snapshot, root / snapshot.run_id


@pytest.mark.asyncio
async def test_task_projection_exposes_safe_graph_and_observed_execution_only(
    tmp_path: Path,
) -> None:
    definition = _definition()
    service, snapshot, run_dir = await _running_service(tmp_path, definition)
    graph_node_id = project_workflow_graph(definition, codex_job_live=True)["nodes"][0]["node_id"]
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for event_type in ("task_started", "task_completed"):
        writer.observe(
            _InterpreterEvent(
                event_type,
                node_id=graph_node_id,
                task_identity="do[0].private_task",
                operation="set",
            )
        )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    payload = json.dumps(projection, ensure_ascii=False)

    assert projection["schema_version"] == 1
    assert projection["workflow_status"] == "running"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert projection["nodes"][0]["node_id"] == "/do/0/private_task"
    assert projection["nodes"][0]["operation"] == "set"
    assert projection["nodes"][0]["state"] == "unknown"
    execution = projection["nodes"][0]["executions"][0]
    assert execution["completion_observed"] is True
    assert execution["state"] == "unknown"
    assert "private_secret" not in payload
    assert "attributes" not in payload


@pytest.mark.asyncio
async def test_started_only_task_is_running_without_terminal_marker_conflict(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/private_task",
            task_identity="do[0].private_task",
            operation="set",
        )
    )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    node = next(item for item in projection["nodes"] if item["node_id"] == "/do/0/private_task")

    assert projection["workflow_status"] == "running"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert node["state"] == "running"
    assert node["executions"][0]["state"] == "running"
    assert node["executions"][0]["evidence_conflict"] is False
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "workflow_not_confirmed"


@pytest.mark.asyncio
async def test_event_operation_mismatch_is_an_evidence_conflict(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/private_task",
            task_identity="do[0].private_task",
            operation="set",
        )
    )
    writer.observe(
        _InterpreterEvent(
            "task_completed",
            node_id="/do/0/private_task",
            task_identity="do[0].private_task",
            operation="call",
        )
    )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert projection["nodes"][0]["state"] == "needs_reconciliation"


@pytest.mark.asyncio
async def test_missing_journal_is_read_only_and_does_not_create_database(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    before = {path.name for path in run_dir.iterdir()}
    status_before = await service.get_workflow(snapshot.workflow_id)

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    event_page = await service.get_workflow_task_events(snapshot.workflow_id, after_id=0, limit=10)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "journal_missing"
    assert event_page["coverage"] == "unavailable"
    assert event_page["events"] == []
    assert {path.name for path in run_dir.iterdir()} == before
    assert not (run_dir / "node_events.sqlite").exists()
    assert (await service.get_workflow(snapshot.workflow_id)).status == status_before.status
    assert not any(
        (run_dir / name).exists() for name in ("node_events.sqlite-wal", "node_events.sqlite-shm")
    )


@pytest.mark.asyncio
async def test_disabled_writer_cannot_make_empty_journal_look_complete(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer._disable("test_disabled")
    writer.workflow_completed()
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "workflow_not_confirmed"


@pytest.mark.asyncio
async def test_task_events_page_uses_strict_exclusive_cursor_and_safe_metadata(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    node_id = "/do/0/private_task"
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for event_type in ("task_started", "task_completed"):
        writer.observe(
            _InterpreterEvent(
                event_type,
                node_id=node_id,
                task_identity="do[0].private_task",
                operation="set",
            )
        )
    writer.close()

    first = await service.get_workflow_task_events(snapshot.workflow_id, after_id=0, limit=1)
    second = await service.get_workflow_task_events(
        snapshot.workflow_id, after_id=first["next_after_id"], limit=10
    )

    assert [event["event_id"] for event in first["events"]] == [1]
    assert first["has_more"] is True
    assert [event["event_id"] for event in second["events"]] == [2]
    assert second["next_after_id"] == 2
    assert second["events"][0]["metadata"] == {"operation": "set"}
    assert first["coverage"] == second["coverage"] == "partial"
    assert first["coverage_reason"] == second["coverage_reason"] == "workflow_not_confirmed"
    assert "PRIVATE_JQ_EXPRESSION" not in json.dumps(first | second)


@pytest.mark.asyncio
async def test_unbounded_task_name_is_not_projected_as_an_identifier(tmp_path: Path) -> None:
    definition = _definition("x" * 300)
    service, snapshot, _run_dir = await _running_service(tmp_path, definition)

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "graph_invalid"
    assert projection["nodes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_name", "node_id"),
    [
        ("日本語処理", "/do/0/日本語処理"),
        ("分類", "/do/0/分類"),
        ("親/子~名", "/do/0/親~1子~0名"),
    ],
)
async def test_unicode_task_name_and_journal_identity_are_projected_exactly(
    tmp_path: Path,
    task_name: str,
    node_id: str,
) -> None:
    definition = _definition(task_name)
    service, snapshot, run_dir = await _running_service(tmp_path, definition)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for event_type in ("task_started", "task_completed"):
        writer.observe(
            _InterpreterEvent(
                event_type,
                node_id=node_id,
                task_identity=f"do[0].{task_name}",
                operation="set",
            )
        )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    node = next(item for item in projection["nodes"] if item["node_id"] == node_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert node["executions"][0]["task_identity"] == f"do[0].{task_name}"
    assert node["executions"][0]["completion_observed"] is True
    assert "private_secret" not in json.dumps(projection, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name",
    [
        "../escape",
        "sk-proj-" + "A" * 32,
        "control\nname",
        "nul\x00name",
        "x" * 129,
    ],
)
async def test_path_secret_control_nul_and_overlong_task_names_are_rejected(
    tmp_path: Path,
    task_name: str,
) -> None:
    service, snapshot, _run_dir = await _running_service(tmp_path, _definition(task_name))

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "graph_invalid"
    assert projection["nodes"] == []


def test_task_identity_accepts_unicode_and_rejects_unsafe_characters() -> None:
    assert _is_task_identity("do[0].日本語処理")
    assert _is_task_identity("do[0].親/子~名.iteration[2].do")
    assert not _is_task_identity("do[0].../escape")
    assert not _is_task_identity("do[0].sk-proj-" + "A" * 32)
    assert not _is_task_identity("do[0].control\nname")
    assert not _is_task_identity("do[0].nul\x00name")
    assert not _is_task_identity("do[0]." + "x" * 2_050)


def _loop_workflow() -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "task-loop-fixture",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "iterate": {
                    "for": {"in": "${ .items }", "each": "item", "at": "index"},
                    "do": [
                        {
                            "choose": {
                                "switch": [
                                    {
                                        "positive": {
                                            "when": "${ $item > 0 }",
                                            "then": "process",
                                        }
                                    },
                                    {"fallback": {"then": "exit"}},
                                ]
                            }
                        },
                        {"process": {"set": "${ $item }"}},
                    ],
                }
            },
            {"execute": {"call": "codex-job", "with": {"prompt": SAFE_CODEX_JOB_PROMPT}}},
        ],
    }


def _nested_do_workflow() -> dict[str, Any]:
    definition = _definition()
    definition["do"][0] = {"container": {"do": [{"child": {"set": {"value": "${ .value }"}}}]}}
    return definition


def _fake_step_runner(
    _identity: str,
    _task_name: str,
    operation: str,
    task_spec: dict[str, Any],
    task_input: Any,
    variables: dict[str, Any],
) -> Any:
    if operation == "call":
        return {"final_text": "fake result"}
    return compute_task_action(operation, task_spec, task_input, variables)


@pytest.mark.asyncio
async def test_loop_switch_jump_exit_and_repeated_static_node_are_observed(tmp_path: Path) -> None:
    definition = _loop_workflow()
    service, snapshot, run_dir = await _running_service(tmp_path, definition)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    Interpreter(
        definition,
        step_runner=_fake_step_runner,
        codex_job_live=True,
        observer=writer.observe,
    ).run({"items": [1, -1, 2]})
    writer.workflow_completed()
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    by_id = {node["node_id"]: node for node in projection["nodes"]}

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert by_id["/do/0/iterate"]["executions"][0]["observation_complete"] is True
    assert len(by_id["/do/0/iterate/do/1/process"]["executions"]) == 2
    assert [
        event["metadata"]["branch_class"]
        for event in (await service.get_workflow_task_events(snapshot.workflow_id))["events"]
        if event["event_type"] == "branch_selected"
    ] == ["jump", "exit", "jump"]
    assert all(node["state"] != "skipped" for node in projection["nodes"])


@pytest.mark.asyncio
async def test_zero_item_loop_is_observed_without_inventing_skipped_state(tmp_path: Path) -> None:
    definition = _loop_workflow()
    service, snapshot, run_dir = await _running_service(tmp_path, definition)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    Interpreter(
        definition,
        step_runner=_fake_step_runner,
        codex_job_live=True,
        observer=writer.observe,
    ).run({"items": []})
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    loop_node = next(node for node in projection["nodes"] if node["node_id"] == "/do/0/iterate")

    assert loop_node["executions"][0]["completion_observed"] is True
    assert loop_node["executions"][0]["observation_complete"] is True
    assert loop_node["state"] == "unknown"
    assert all(node["state"] != "skipped" for node in projection["nodes"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_event_type",
    ["scope_entered", "scope_exited"],
)
async def test_repeated_do_execution_requires_its_own_scope_pair(
    tmp_path: Path,
    missing_event_type: str,
) -> None:
    definition = _loop_workflow()
    definition["do"][0]["iterate"]["do"] = [
        {"container": {"do": [{"child": {"set": "${ $item }"}}]}}
    ]
    service, snapshot, _run_dir, _job = await _successful_fake_run(
        tmp_path,
        definition=definition,
        input_data={"items": [1, 2]},
        omit_event_type=missing_event_type,
        omit_node_id="/do/0/iterate/do/0/container",
        omit_identity_fragment=".iteration[1].do[0].container.do",
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    container = next(
        node for node in projection["nodes"] if node["node_id"] == "/do/0/iterate/do/0/container"
    )
    missing_execution = next(
        execution
        for execution in container["executions"]
        if ".iteration[1].do[0].container" in execution["task_identity"]
    )

    assert projection["workflow_status"] == "succeeded"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert missing_execution["completion_observed"] is True
    assert missing_execution["observation_complete"] is False
    assert missing_execution["state"] != "succeeded"
    assert container["state"] != "succeeded"


@pytest.mark.asyncio
async def test_repeated_do_execution_cannot_borrow_another_scope_pair(tmp_path: Path) -> None:
    definition = _loop_workflow()
    definition["do"][0]["iterate"]["do"] = [
        {"container": {"do": [{"child": {"set": "${ $item }"}}]}}
    ]
    service, snapshot, _run_dir, _job = await _successful_fake_run(
        tmp_path,
        definition=definition,
        input_data={"items": [1, 2]},
        omit_event_type=("scope_entered", "scope_exited"),
        omit_node_id="/do/0/iterate/do/0/container",
        omit_identity_fragment=".iteration[1].do[0].container.do",
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    container = next(
        node for node in projection["nodes"] if node["node_id"] == "/do/0/iterate/do/0/container"
    )
    missing_execution = next(
        execution
        for execution in container["executions"]
        if ".iteration[1].do[0].container" in execution["task_identity"]
    )

    assert projection["workflow_status"] == "succeeded"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert missing_execution["completion_observed"] is True
    assert missing_execution["observation_complete"] is False
    assert missing_execution["state"] != "succeeded"
    assert container["state"] != "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_event_type",
    [
        "iteration_started",
        "iteration_completed",
        "scope_entered",
        "scope_exited",
        ("iteration_started", "iteration_completed"),
        ("scope_entered", "scope_exited"),
        ("iteration_started", "iteration_completed", "scope_entered", "scope_exited"),
    ],
)
async def test_loop_execution_requires_each_iteration_pair(
    tmp_path: Path,
    missing_event_type: str | tuple[str, ...],
) -> None:
    service, snapshot, _run_dir, _job = await _successful_fake_run(
        tmp_path,
        definition=_loop_workflow(),
        input_data={"items": [1, 2]},
        omit_event_type=missing_event_type,
        omit_node_id="/do/0/iterate",
        omit_iteration_ordinal=1,
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    loop_node = next(node for node in projection["nodes"] if node["node_id"] == "/do/0/iterate")
    execution = loop_node["executions"][0]

    assert projection["workflow_status"] == "succeeded"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert execution["completion_observed"] is True
    assert execution["observation_complete"] is False
    assert execution["state"] != "succeeded"
    assert loop_node["state"] != "succeeded"


@pytest.mark.asyncio
async def test_snapshot_version_changes_when_worker_status_changes_without_new_events(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/private_task",
            task_identity="do[0].private_task",
            operation="set",
        )
    )
    writer.close()
    first = await service.get_workflow_tasks(snapshot.workflow_id)
    repeated = await service.get_workflow_tasks(snapshot.workflow_id)
    (run_dir / "worker-entry-status.json").write_text(
        '{"state":"completed","exit_code":0,"error_code":null}', encoding="utf-8"
    )
    after_status_change = await service.get_workflow_tasks(snapshot.workflow_id)

    assert first["latest_event_id"] == after_status_change["latest_event_id"] == 1
    assert first["snapshot_version"] == repeated["snapshot_version"]
    assert first["snapshot_version"] != after_status_change["snapshot_version"]
    assert after_status_change["nodes"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_snapshot_version_changes_when_coverage_changes_without_status_or_events(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id).close()
    first = await service.get_workflow_tasks(snapshot.workflow_id)
    repeated = await service.get_workflow_tasks(snapshot.workflow_id)
    (run_dir / "node_events.sqlite").unlink()
    unavailable = await service.get_workflow_tasks(snapshot.workflow_id)
    unavailable_repeated = await service.get_workflow_tasks(snapshot.workflow_id)

    assert first["workflow_status"] == unavailable["workflow_status"] == "running"
    assert first["latest_event_id"] == unavailable["latest_event_id"] == 0
    assert first["coverage"] == "partial"
    assert first["coverage_reason"] == "workflow_not_confirmed"
    assert unavailable["coverage"] == "unavailable"
    assert unavailable["coverage_reason"] == "journal_missing"
    assert first["snapshot_version"] == repeated["snapshot_version"]
    assert unavailable["snapshot_version"] == unavailable_repeated["snapshot_version"]
    assert first["snapshot_version"] != unavailable["snapshot_version"]


@pytest.mark.asyncio
async def test_snapshot_version_keeps_job_fingerprint_when_public_nodes_are_unchanged(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/1/execute",
            task_identity="do[1].execute",
            operation="call",
        )
    )
    writer.close()
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    job = adapter.prepare_job(
        snapshot.workflow_id,
        "do[1].execute",
        "PRIVATE_JOB_PROMPT",
        run_dir,
        port=18001,
        workspace_root=tmp_path / "workspace",
        allowed_roots=(str(tmp_path),),
    )
    assert adapter.claim_post(job["job_id"])
    adapter.mark_accepted(job["job_id"], "thread-fixture-0001", "turn-fixture-0001", "in_progress")
    accepted = await service.get_workflow_tasks(snapshot.workflow_id)
    with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
        connection.execute("UPDATE jobs SET state = 'RUNNING' WHERE job_id = ?", (job["job_id"],))
    running = await service.get_workflow_tasks(snapshot.workflow_id)

    def without_version(response: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in response.items() if key != "snapshot_version"}

    assert accepted["workflow_status"] == running["workflow_status"] == "running"
    assert accepted["latest_event_id"] == running["latest_event_id"] == 1
    assert without_version(accepted) == without_version(running)
    assert accepted["snapshot_version"] != running["snapshot_version"]


def _write_v1_job_ledger(
    database: Path,
    *,
    workflow_id: str,
    task_identity: str,
    workspace: Path,
) -> str:
    job_id = "job-legacy-0001"
    values = {
        "job_id": job_id,
        "request_key": f"{workflow_id}::{task_identity}",
        "workflow_id": workflow_id,
        "task_identity": task_identity,
        "request_digest": "a" * 64,
        "request_json": json.dumps({"prompt": "PRIVATE_V1_PROMPT"}),
        "state": "PREPARED",
        "client_request_id": "legacy-client-request",
        "title_marker": "legacy-title-marker",
        "agent_cwd": str(workspace),
        "created_at": "2026-11-17T00:00:00+00:00",
        "updated_at": "2026-11-17T00:00:00+00:00",
        "thread_id": None,
        "turn_id": None,
        "remote_state": None,
        "receipt_json": None,
        "error": None,
    }
    with sqlite3.connect(database) as connection:
        connection.execute(_jobs_table_sql("jobs", version=1))
        connection.execute(_job_events_table_sql())
        connection.execute("CREATE INDEX job_events_job_event_idx ON job_events(job_id, event_id)")
        connection.execute(_job_reconciliation_table_sql())
        columns = ", ".join(_JOB_COLUMNS)
        placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
        connection.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
            tuple(values[column] for column in _JOB_COLUMNS),
        )
    return job_id


@pytest.mark.asyncio
async def test_single_v1_job_ledger_is_read_only_and_joined_by_exact_identity(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    workspace = tmp_path / "workspace"
    node_id = "/do/1/execute"
    task_identity = "do[1].execute"
    job_id = _write_v1_job_ledger(
        run_dir / "codex_jobs.sqlite",
        workflow_id=snapshot.workflow_id,
        task_identity=task_identity,
        workspace=workspace,
    )
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id=node_id,
            task_identity=task_identity,
            operation="call",
        )
    )
    writer.close()
    before = (run_dir / "codex_jobs.sqlite").read_bytes()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    execution = next(node for node in projection["nodes"] if node["node_id"] == node_id)[
        "executions"
    ][0]

    assert execution["job_id"] == job_id
    assert execution["state"] == "running"
    assert "thread_id" not in execution
    assert "PRIVATE_V1_PROMPT" not in json.dumps(projection)
    assert (run_dir / "codex_jobs.sqlite").read_bytes() == before


@pytest.mark.asyncio
async def test_orphan_job_ledger_identity_degrades_nodes_to_unknown(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    _write_v1_job_ledger(
        run_dir / "codex_jobs.sqlite",
        workflow_id=snapshot.workflow_id,
        task_identity="do[1].execute",
        workspace=tmp_path / "workspace",
    )
    NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id).close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert all(node["state"] == "unknown" for node in projection["nodes"])


def _two_job_definition() -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "two-sequential-jobs",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {"first": {"call": "codex-job", "with": {"prompt": "first prompt"}}},
            {
                "second": {
                    "call": "codex-job",
                    "with": {"prompt": "translate: ", "prompt_from": "${ .result.final_text }"},
                }
            },
        ],
    }


@pytest.mark.asyncio
async def test_two_job_v2_ledger_joins_distinct_static_nodes(tmp_path: Path) -> None:
    definition = _two_job_definition()
    service, snapshot, run_dir = await _running_service(tmp_path, definition, max_live_codex_jobs=2)
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    jobs = [
        adapter.prepare_job(
            snapshot.workflow_id,
            identity,
            f"fixture prompt {index}",
            run_dir,
            port=18001,
            workspace_root=tmp_path / "workspace",
            allowed_roots=(str(tmp_path),),
            max_jobs_per_workflow=2,
        )
        for index, identity in enumerate(("do[0].first", "do[1].second"), start=1)
    ]
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for node_id, identity in (("/do/0/first", "do[0].first"), ("/do/1/second", "do[1].second")):
        writer.observe(
            _InterpreterEvent(
                "task_started",
                node_id=node_id,
                task_identity=identity,
                operation="call",
            )
        )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    by_id = {node["node_id"]: node for node in projection["nodes"]}

    assert by_id["/do/0/first"]["executions"][0]["job_id"] == jobs[0]["job_id"]
    assert by_id["/do/1/second"]["executions"][0]["job_id"] == jobs[1]["job_id"]
    assert by_id["/do/0/first"]["state"] == "running"
    assert by_id["/do/1/second"]["state"] == "running"


@pytest.mark.asyncio
async def test_three_job_v2_ledger_projects_repeated_loop_node_executions(tmp_path: Path) -> None:
    workflow_path = (
        Path(__file__).parents[1]
        / "poc"
        / "ows"
        / "fixtures"
        / "three_codex_job_loop_workflow.yaml"
    )
    definition = parse_workflow_text(workflow_path.read_text(encoding="utf-8"))
    input_data = {
        "issues": [
            {"number": 17, "enabled": True},
            {"number": 5, "enabled": False},
            {"number": 9, "enabled": True},
            {"number": 21, "enabled": True},
        ]
    }
    service, snapshot, run_dir = await _running_service(
        tmp_path,
        definition,
        input_data=input_data,
        max_live_codex_jobs=3,
    )
    identities = (
        "do[0].processIssues.iteration[1].do[1].processIssue",
        "do[0].processIssues.iteration[2].do[1].processIssue",
        "do[0].processIssues.iteration[3].do[1].processIssue",
    )
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    jobs = [
        adapter.prepare_job(
            snapshot.workflow_id,
            identity,
            f"synthetic prompt {index}",
            run_dir,
            port=18001,
            workspace_root=tmp_path / "workspace",
            allowed_roots=(str(tmp_path),),
            max_jobs_per_workflow=3,
        )
        for index, identity in enumerate(identities, start=1)
    ]
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for identity in identities:
        writer.observe(
            _InterpreterEvent(
                "task_started",
                node_id="/do/0/processIssues/do/1/processIssue",
                task_identity=identity,
                operation="call",
            )
        )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    node = next(
        item
        for item in projection["nodes"]
        if item["node_id"] == "/do/0/processIssues/do/1/processIssue"
    )

    assert node["state"] == "running"
    assert len(node["executions"]) == 3
    assert [item["job_id"] for item in node["executions"]] == [job["job_id"] for job in jobs]


async def _successful_fake_run(
    tmp_path: Path,
    *,
    definition: dict[str, Any] | None = None,
    input_data: dict[str, Any] | None = None,
    omit_event_type: str | tuple[str, ...] | None = None,
    omit_node_id: str | None = None,
    fail_node_id: str | None = None,
    omit_iteration_ordinal: int | None = None,
    omit_identity_fragment: str | None = None,
):
    definition = definition or _definition()
    input_data = {"value": 1} if input_data is None else input_data
    service, snapshot, run_dir = await _running_service(
        tmp_path,
        definition,
        input_data=input_data,
    )
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    if omit_event_type is not None or fail_node_id is not None:
        omitted_types = (
            set()
            if omit_event_type is None
            else {omit_event_type}
            if isinstance(omit_event_type, str)
            else set(omit_event_type)
        )

        def record_unless_omitted(event: _InterpreterEvent) -> None:
            if (
                event.event_type in omitted_types
                and (omit_node_id is None or event.node_id == omit_node_id)
                and (
                    omit_iteration_ordinal is None
                    or event.iteration_ordinal == omit_iteration_ordinal
                )
                and (
                    omit_identity_fragment is None
                    or omit_identity_fragment in (event.task_identity or "")
                    or omit_identity_fragment in (event.scope_identity or "")
                )
            ):
                return
            if event.event_type == "task_completed" and event.node_id == fail_node_id:
                writer.observe(
                    _InterpreterEvent(
                        "task_failed",
                        node_id=event.node_id,
                        task_identity=event.task_identity,
                        operation=event.operation,
                        failure_phase="operation",
                    )
                )
                return
            writer.observe(event)

        Interpreter(
            definition,
            step_runner=_fake_step_runner,
            codex_job_live=True,
            observer=record_unless_omitted,
        ).run(input_data)
    else:
        for node_id, identity, operation in (
            ("/do/0/private_task", "do[0].private_task", "set"),
            ("/do/1/execute", "do[1].execute", "call"),
        ):
            for event_type in ("task_started", "task_completed"):
                writer.observe(
                    _InterpreterEvent(
                        event_type,
                        node_id=node_id,
                        task_identity=identity,
                        operation=operation,
                    )
                )
    writer.workflow_completed()
    writer.close()

    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    job = adapter.prepare_job(
        snapshot.workflow_id,
        "do[1].execute",
        "PRIVATE_CODEX_PROMPT",
        run_dir,
        port=18001,
        workspace_root=tmp_path / "workspace",
        allowed_roots=(str(tmp_path),),
    )
    assert adapter.claim_post(job["job_id"])
    adapter.mark_accepted(job["job_id"], "thread-fixture-0001", "turn-fixture-0001", "in_progress")
    adapter._mark_succeeded(job["job_id"], "PRIVATE_FINAL_TEXT")

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workflow_status VALUES (?, 'SUCCESS')", (snapshot.workflow_id,)
        )
    (run_dir / "worker-entry-status.json").write_text(
        '{"state":"completed","exit_code":0,"error_code":null}', encoding="utf-8"
    )
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "workflow_id": snapshot.workflow_id,
                "definition_sha256": manifest["definition_sha256"],
                "input_sha256": manifest["input_sha256"],
                "result": {"final_text": "PRIVATE_FINAL_TEXT"},
            }
        ),
        encoding="utf-8",
    )
    return service, snapshot, run_dir, job


@pytest.mark.asyncio
async def test_succeeded_requires_worker_dbos_and_related_verified_job(tmp_path: Path) -> None:
    service, snapshot, _run_dir, job = await _successful_fake_run(tmp_path)

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    by_id = {node["node_id"]: node for node in projection["nodes"]}
    call_execution = by_id["/do/1/execute"]["executions"][0]
    json_payload = json.dumps(projection, ensure_ascii=False)

    assert projection["workflow_status"] == "succeeded"
    assert projection["coverage"] == "complete"
    assert "coverage_reason" not in projection
    assert events["coverage"] == "complete", events.get("coverage_reason")
    assert by_id["/do/0/private_task"]["state"] == "succeeded"
    assert by_id["/do/1/execute"]["state"] == "succeeded"
    assert call_execution["job_id"] == job["job_id"]
    assert call_execution["thread_id"] == "thread-fixture-0001"
    assert call_execution["turn_id"] == "turn-fixture-0001"
    assert call_execution["started_at"] is not None
    assert call_execution["completed_at"] is not None
    assert "PRIVATE_CODEX_PROMPT" not in json_payload
    assert "PRIVATE_FINAL_TEXT" not in json_payload
    assert "PRIVATE_RUN_RESULT" not in json_payload


@pytest.mark.asyncio
async def test_terminal_run_with_started_only_noncall_task_is_incomplete_evidence(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir, _job = await _successful_fake_run(
        tmp_path,
        omit_event_type="task_completed",
        omit_node_id="/do/0/private_task",
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    by_id = {node["node_id"]: node for node in projection["nodes"]}
    event_ids = [event["event_id"] for event in events["events"]]

    assert event_ids == list(range(1, events["latest_event_id"] + 1))
    assert events["events"][-1]["event_type"] == "workflow_completed"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "evidence_conflict"
    assert by_id["/do/0/private_task"]["state"] != "succeeded"
    assert by_id["/do/1/execute"]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_terminal_run_with_task_failure_is_incomplete_evidence(tmp_path: Path) -> None:
    service, snapshot, _run_dir, _job = await _successful_fake_run(
        tmp_path,
        fail_node_id="/do/0/private_task",
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    failed_node = next(
        node for node in projection["nodes"] if node["node_id"] == "/do/0/private_task"
    )

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "evidence_conflict"
    assert failed_node["state"] != "succeeded"
    assert failed_node["executions"][0]["completion_observed"] is True
    assert failed_node["executions"][0]["state"] != "succeeded"


@pytest.mark.asyncio
async def test_failed_workflow_keeps_local_task_failure_state(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for node_id, task_identity, operation in (
        ("/do/0/private_task", "do[0].private_task", "set"),
        ("/do/1/execute", "do[1].execute", "call"),
    ):
        writer.observe(
            _InterpreterEvent(
                "task_started",
                node_id=node_id,
                task_identity=task_identity,
                operation=operation,
            )
        )
        writer.observe(
            _InterpreterEvent(
                "task_failed",
                node_id=node_id,
                task_identity=task_identity,
                operation=operation,
                failure_phase="operation",
            )
        )
    writer.close()

    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    job = adapter.prepare_job(
        snapshot.workflow_id,
        "do[1].execute",
        "PRIVATE_CODEX_PROMPT",
        run_dir,
        port=18001,
        workspace_root=tmp_path / "workspace",
        allowed_roots=(str(tmp_path),),
    )
    assert adapter.claim_post(job["job_id"])
    adapter.mark_accepted(job["job_id"], "thread-fixture-0001", "turn-fixture-0001", "in_progress")
    adapter._mark_failed(job["job_id"], "failed")
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS workflow_status "
            "(workflow_uuid TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO workflow_status VALUES (?, 'ERROR')", (snapshot.workflow_id,)
        )
    (run_dir / "worker-entry-status.json").write_text(
        '{"state":"failed","exit_code":1,"error_code":"worker_failed"}',
        encoding="utf-8",
    )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    failed_node = next(
        node for node in projection["nodes"] if node["node_id"] == "/do/0/private_task"
    )

    assert projection["workflow_status"] == "failed"
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "workflow_not_confirmed"
    assert failed_node["state"] == "failed"
    assert failed_node["executions"][0]["state"] == "failed"
    assert failed_node["executions"][0]["evidence_conflict"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("incomplete_evidence", "expected_reason"),
    [
        ("marker_missing", "workflow_not_confirmed"),
        ("marker_not_last", "evidence_conflict"),
        ("duplicate_marker", "evidence_conflict"),
        ("marker_time_reversed", "evidence_conflict"),
        ("worker_failed", "workflow_not_confirmed"),
        ("dbos_pending", "workflow_not_confirmed"),
        ("job_running", "workflow_not_confirmed"),
        ("job_maybe_sent", "workflow_not_confirmed"),
        ("job_reconciliation", "workflow_not_confirmed"),
        ("job_receipt_invalid", "ledger_unavailable"),
    ],
)
async def test_coverage_requires_terminal_marker_and_verified_success_evidence(
    tmp_path: Path,
    incomplete_evidence: str,
    expected_reason: str,
) -> None:
    service, snapshot, run_dir, job = await _successful_fake_run(tmp_path)
    if incomplete_evidence == "marker_missing":
        with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
            connection.execute("DELETE FROM node_events WHERE event_type = 'workflow_completed'")
    elif incomplete_evidence == "marker_not_last":
        writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
        writer.observe(
            _InterpreterEvent(
                "task_started",
                node_id="/do/0/private_task",
                task_identity="do[0].private_task.after_marker",
                operation="set",
            )
        )
        writer.close()
    elif incomplete_evidence == "duplicate_marker":
        with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
            marker = connection.execute(
                "SELECT workflow_id, run_id, schema_version, occurred_at, source, metadata_json "
                "FROM node_events WHERE event_type = 'workflow_completed'"
            ).fetchone()
            assert marker is not None
            connection.execute(
                "INSERT INTO node_events (event_key, workflow_id, run_id, schema_version, "
                "node_id, task_identity, execution_id, event_type, occurred_at, source, "
                "job_id, metadata_json) VALUES (?, ?, ?, ?, NULL, NULL, NULL, "
                "'workflow_completed', ?, ?, NULL, ?)",
                ("e" * 64, *marker),
            )
    elif incomplete_evidence == "marker_time_reversed":
        with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
            connection.execute(
                "UPDATE node_events SET occurred_at = ? WHERE event_type = 'workflow_completed'",
                ("2000-01-01T00:00:00+00:00",),
            )
    elif incomplete_evidence == "worker_failed":
        (run_dir / "worker-entry-status.json").write_text(
            '{"state":"failed","exit_code":1,"error_code":"worker_failed"}',
            encoding="utf-8",
        )
    elif incomplete_evidence == "dbos_pending":
        with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
            connection.execute(
                "UPDATE workflow_status SET status = 'PENDING' WHERE workflow_uuid = ?",
                (snapshot.workflow_id,),
            )
    elif incomplete_evidence == "job_running":
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute(
                "UPDATE jobs SET state = 'RUNNING', remote_state = 'in_progress', "
                "receipt_json = NULL WHERE job_id = ?",
                (job["job_id"],),
            )
    elif incomplete_evidence == "job_maybe_sent":
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute(
                "UPDATE jobs SET state = 'MAYBE_SENT', thread_id = NULL, turn_id = NULL, "
                "remote_state = NULL, receipt_json = NULL WHERE job_id = ?",
                (job["job_id"],),
            )
    elif incomplete_evidence == "job_reconciliation":
        CodexJobAdapter(run_dir / "codex_jobs.sqlite").mark_reconciliation(
            job["job_id"], "fixture reconciliation marker", details={"private": "hidden"}
        )
    else:
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute(
                "UPDATE jobs SET remote_state = 'failed' WHERE job_id = ?",
                (job["job_id"],),
            )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)
    call_node = next(node for node in projection["nodes"] if node["node_id"] == "/do/1/execute")

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == expected_reason
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == expected_reason
    assert call_node["state"] != "succeeded"
    assert call_node["executions"][0]["state"] != "succeeded"


@pytest.mark.asyncio
async def test_contradictory_task_failure_prevents_projected_success(tmp_path: Path) -> None:
    service, snapshot, run_dir, _job = await _successful_fake_run(tmp_path)
    journal = run_dir / "node_events.sqlite"
    with sqlite3.connect(journal) as connection:
        original = connection.execute(
            "SELECT workflow_id, run_id, schema_version, node_id, task_identity, execution_id, "
            "occurred_at FROM node_events WHERE event_type = 'task_completed' AND node_id = ?",
            ("/do/0/private_task",),
        ).fetchone()
        assert original is not None
        connection.execute(
            "INSERT INTO node_events (event_key, workflow_id, run_id, schema_version, node_id, "
            "task_identity, execution_id, event_type, occurred_at, source, job_id, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'task_failed', ?, 'interpreter', NULL, ?)",
            (
                "f" * 64,
                original[0],
                original[1],
                original[2],
                original[3],
                original[4],
                original[5],
                original[6],
                '{"failure_phase":"operation","operation":"set"}',
            ),
        )

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    node = next(item for item in projection["nodes"] if item["node_id"] == "/do/0/private_task")

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "evidence_conflict"
    assert node["state"] == "needs_reconciliation"
    assert node["executions"][0]["evidence_conflict"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("job_condition", ["maybe_sent", "reconciliation_marker"])
async def test_maybe_sent_or_job_marker_projects_needs_reconciliation(
    tmp_path: Path,
    job_condition: str,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    task_identity = "do[1].execute"
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    job = adapter.prepare_job(
        snapshot.workflow_id,
        task_identity,
        "PRIVATE_JOB_PROMPT",
        run_dir,
        port=18001,
        workspace_root=tmp_path / "workspace",
        allowed_roots=(str(tmp_path),),
    )
    if job_condition == "maybe_sent":
        assert adapter.claim_post(job["job_id"])
    else:
        adapter.mark_reconciliation(
            job["job_id"],
            "PRIVATE_RECONCILIATION_REASON",
            details={"private": "PRIVATE_RECONCILIATION_DETAILS"},
        )
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/1/execute",
            task_identity=task_identity,
            operation="call",
        )
    )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    node = next(item for item in projection["nodes"] if item["node_id"] == "/do/1/execute")
    payload = json.dumps(projection)

    assert node["state"] == "needs_reconciliation"
    assert node["executions"][0]["state"] == "needs_reconciliation"
    assert "PRIVATE_JOB_PROMPT" not in payload
    assert "PRIVATE_RECONCILIATION_REASON" not in payload
    assert "PRIVATE_RECONCILIATION_DETAILS" not in payload


def _seed_started_event(run_dir: Path, snapshot) -> None:
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/private_task",
            task_identity="do[0].private_task",
            operation="set",
        )
    )
    writer.close()


@pytest.mark.asyncio
async def test_replayed_event_key_is_read_once_and_get_does_not_construct_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    event = _InterpreterEvent(
        "task_started",
        node_id="/do/0/private_task",
        task_identity="do[0].private_task",
        operation="set",
    )
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    writer.observe(event)
    writer.observe(event)
    writer.close()

    def unexpected_writer(*_args, **_kwargs):
        raise AssertionError("GET must not construct a NodeEventWriter")

    monkeypatch.setattr(
        "codex_bridge.workflows.node_events.NodeEventWriter",
        unexpected_writer,
    )
    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0]

    assert event_count == 1
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert len(projection["nodes"][0]["executions"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("journal_mode", "expected_reason"),
    [
        ("corrupt", {"journal_corrupt", "journal_schema_invalid"}),
        ("schema", {"journal_schema_invalid"}),
        ("metadata", {"journal_invalid"}),
    ],
)
async def test_corrupt_schema_or_metadata_degrades_to_safe_coverage(
    tmp_path: Path,
    journal_mode: str,
    expected_reason: set[str],
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    database = run_dir / "node_events.sqlite"
    if journal_mode == "corrupt":
        database.write_bytes(b"not-a-sqlite-database")
    else:
        _seed_started_event(run_dir, snapshot)
        with sqlite3.connect(database) as connection:
            if journal_mode == "schema":
                connection.execute("PRAGMA user_version=8")
            else:
                connection.execute(
                    "UPDATE node_events SET metadata_json = ?",
                    ('{"operation":"set","private":"PRIVATE_METADATA"}',),
                )
    before = database.read_bytes()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    events = await service.get_workflow_task_events(snapshot.workflow_id)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] in expected_reason
    assert events["coverage"] == "unavailable"
    assert events["events"] == []
    assert database.read_bytes() == before
    assert "PRIVATE_METADATA" not in json.dumps(projection | events)
    assert not any(
        (run_dir / name).exists()
        for name in (
            "node_events.sqlite-wal",
            "node_events.sqlite-shm",
            "node_events.sqlite-journal",
        )
    )


@pytest.mark.asyncio
async def test_journal_file_size_limit_returns_unavailable_without_opening_sqlite(
    tmp_path: Path,
) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    database = run_dir / "node_events.sqlite"
    with database.open("wb") as output:
        output.truncate(24 * 1024 * 1024 + 1)

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "journal_too_large"
    assert database.stat().st_size == 24 * 1024 * 1024 + 1


@pytest.mark.asyncio
async def test_journal_symlink_is_rejected_without_reading_target(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    _seed_started_event(run_dir, snapshot)
    database = run_dir / "node_events.sqlite"
    target = tmp_path / "external-node-journal.sqlite"
    target.write_bytes(database.read_bytes())
    database.unlink()
    try:
        database.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Windows did not permit creating a test symlink: {type(error).__name__}")

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "journal_unsafe_path"
    assert target.stat().st_size > 0


@pytest.mark.asyncio
async def test_windows_reparse_attribute_is_rejected(tmp_path: Path, monkeypatch) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id).close()
    database = run_dir / "node_events.sqlite"
    original_lstat = Path.lstat

    def lstat_with_reparse_attribute(path: Path):
        if path == database:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_attribute)

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "journal_unsafe_path"


@pytest.mark.asyncio
async def test_exclusive_journal_lock_returns_coverage_busy(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    _seed_started_event(run_dir, snapshot)
    blocker = sqlite3.connect(run_dir / "node_events.sqlite", isolation_level=None, timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        projection = await service.get_workflow_tasks(snapshot.workflow_id)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "journal_busy"


@pytest.mark.asyncio
async def test_task_and_event_response_caps_keep_counts_and_safe_cursor(tmp_path: Path) -> None:
    service, snapshot, run_dir = await _running_service(tmp_path)
    writer = NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id)
    for index in range(200):
        identity = f"do[0].private_task.iteration[{index}]." + ("x" * 1_300)
        writer.observe(
            _InterpreterEvent(
                "task_started",
                node_id="/do/0/private_task",
                task_identity=identity,
                operation="set",
            )
        )
    writer.close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)
    page = await service.get_workflow_task_events(snapshot.workflow_id, after_id=0, limit=100)
    task_bytes = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    event_bytes = json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    assert projection["truncated"] is True
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "truncated"
    assert projection["counts"]["executions"] == 200
    assert len(projection["nodes"][0]["executions"]) < 200
    assert len(task_bytes) <= 256 * 1024
    assert page["truncated"] is True
    assert page["coverage"] == "partial"
    assert page["has_more"] is True
    assert page["next_after_id"] == page["events"][-1]["event_id"]
    assert page["next_after_id"] < page["latest_event_id"]
    assert len(event_bytes) <= 128 * 1024


@pytest.mark.asyncio
async def test_static_node_and_edge_counts_are_preserved_when_capped(tmp_path: Path) -> None:
    definition = _definition()
    definition["do"] = [
        {f"task_{index}": {"set": {"value": index}, "then": "exit"}} for index in range(1_024)
    ] + [
        {
            "execute": {
                "call": "codex-job",
                "with": {"prompt": SAFE_CODEX_JOB_PROMPT},
                "then": "exit",
            }
        }
    ]
    service, snapshot, run_dir = await _running_service(tmp_path, definition)
    NodeEventWriter(run_dir, workflow_id=snapshot.workflow_id, run_id=snapshot.run_id).close()

    projection = await service.get_workflow_tasks(snapshot.workflow_id)

    assert projection["truncated"] is True
    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "truncated"
    assert projection["counts"] == {"nodes": 1_025, "edges": 1_026, "executions": 0}
    assert len(projection["nodes"]) == 256
    assert len(projection["edges"]) <= 1_024
