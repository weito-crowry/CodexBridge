from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
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
    def __init__(
        self,
        state_root: Path,
        workspace: Path,
        *,
        allowed_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self.state_root = state_root
        self.allowed_roots = tuple(
            str(root) for root in (allowed_roots if allowed_roots is not None else (workspace,))
        )
        self.start_calls: list[tuple[Path, Path | None, int]] = []
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
        max_live_codex_jobs: int = 1,
    ) -> object:
        assert mode == "start"
        self.start_calls.append((run_dir, workspace_root, max_live_codex_jobs))
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


def _two_job_workflow_yaml() -> str:
    return "\n".join(
        (
            "document:",
            '  dsl: "1.0.3"',
            "  namespace: codexbridge",
            "  name: two-sequential-jobs",
            '  version: "1.0.0"',
            "evaluate:",
            "  language: jq",
            "  mode: strict",
            "do:",
            "  - first:",
            "      call: codex-job",
            "      with:",
            '        prompt: "first prompt"',
            "  - second:",
            "      call: codex-job",
            "      with:",
            '        prompt: "translate: "',
            "        prompt_from: '${ .result.final_text }'",
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
async def test_lifecycle_log_tracks_reservation_state_and_replay_without_poll_records(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace = service_fixture
    arguments = {
        "client_request_id": "phase5f2b-lifecycle-0001",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {"answer": 4},
        "workspace_root": str(workspace),
    }

    accepted = await service.start_workflow(**arguments)
    log_path = manager.state_root / accepted.run_id / "bridge-lifecycle.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [
        "request_reserved",
        "workflow_state_changed",
        "workflow_state_changed",
    ]
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        reserved_digest = connection.execute(
            "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
            (accepted.run_id,),
        ).fetchone()[0]
    assert events[0]["request_digest"] == reserved_digest
    assert [(event.get("previous_state"), event.get("state")) for event in events[1:]] == [
        ("queued", "starting"),
        ("starting", "running"),
    ]
    assert all(event["run_id"] == accepted.run_id for event in events)
    assert all(event["workflow_id"] == accepted.workflow_id for event in events)

    before_reads = log_path.read_bytes()
    assert (await service.get_workflow(accepted.workflow_id)).status == "running"
    await service.list_workflows()
    assert log_path.read_bytes() == before_reads

    run_dir = manager.state_root / accepted.run_id
    job_adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    prepared_job = job_adapter.prepare_job(
        accepted.workflow_id,
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    assert job_adapter.claim_post(prepared_job["job_id"])
    with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
        job_trace = connection.execute(
            "SELECT j.workflow_id, j.job_id, j.request_key, j.task_identity, "
            "e.event_id, e.state FROM jobs AS j JOIN job_events AS e USING (job_id) "
            "WHERE j.workflow_id = ? ORDER BY e.event_id",
            (accepted.workflow_id,),
        ).fetchall()
    assert [row[5] for row in job_trace] == ["PREPARED", "MAYBE_SENT"]
    assert [row[4] for row in job_trace] == sorted(row[4] for row in job_trace)
    assert all(row[0] == events[0]["workflow_id"] for row in job_trace)
    assert all(row[1] == prepared_job["job_id"] for row in job_trace)

    replay_manager = FakeWorkerManager(manager.state_root, workspace)
    replay_service = WorkflowService(replay_manager, codex_bridge_port=18001)
    replayed = await replay_service.start_workflow(**arguments)
    assert replayed.run_id == accepted.run_id
    assert replay_manager.start_calls == []
    replay_events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert replay_events[-1]["event"] == "request_replayed"
    assert replay_events[-1]["reason_code"] == "same_request_id"


@pytest.mark.asyncio
async def test_lifecycle_write_failure_does_not_change_workflow_acceptance(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import diagnostic_log

    service, manager, workspace = service_fixture

    def fail_open(_run_dir: Path, _log_path: Path) -> int:
        raise PermissionError("test-only diagnostic failure")

    monkeypatch.setattr(diagnostic_log, "_open_append_file", fail_open)
    accepted = await service.start_workflow(
        client_request_id="phase5f2b-write-failure-0001",
        workflow_yaml=_workflow_yaml(),
        input_data={"answer": 4},
        workspace_root=str(workspace),
    )

    assert accepted.status == "running"
    assert len(manager.start_calls) == 1
    assert not (manager.state_root / accepted.run_id / "bridge-lifecycle.jsonl").exists()
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        state = connection.execute(
            "SELECT state FROM workflow_requests WHERE run_id = ?", (accepted.run_id,)
        ).fetchone()[0]
    assert state == "running"


@pytest.mark.asyncio
async def test_queued_reservation_after_service_interruption_is_not_respawned(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, manager, workspace = service_fixture
    arguments = {
        "client_request_id": "phase5f2a-queued-interruption",
        "workflow_yaml": _workflow_yaml(),
        "input_data": {"answer": 4},
        "workspace_root": str(workspace),
    }

    async def interrupt_before_manager(
        _row: object, _prepared: object, _diagnostic: object
    ) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_start_reserved_workflow", interrupt_before_manager)
    with pytest.raises(asyncio.CancelledError):
        await service.start_workflow(**arguments)

    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        reserved_state = connection.execute(
            "SELECT state FROM workflow_requests WHERE client_request_id = ?",
            (arguments["client_request_id"],),
        ).fetchone()
    assert reserved_state == ("queued",)
    assert manager.start_calls == []

    restarted_manager = FakeWorkerManager(manager.state_root, workspace)
    replay = await WorkflowService(restarted_manager, codex_bridge_port=18001).start_workflow(
        **arguments
    )

    assert replay.status == "failed_to_start"
    assert restarted_manager.start_calls == []
    events = [
        json.loads(line)
        for line in (manager.state_root / replay.run_id / "bridge-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "request_reserved",
        "request_replayed",
        "workflow_state_changed",
    ]
    assert events[1]["state"] == "queued"
    assert events[1]["reason_code"] == "queued_replay_failed_to_start"
    assert events[2]["previous_state"] == "queued"
    assert events[2]["state"] == "failed_to_start"


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
    run_dir = manager.start_calls[0][0]
    events = [
        json.loads(line)
        for line in (run_dir / "bridge-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    conflict = events[-1]
    assert conflict["event"] == "request_conflict"
    assert conflict["state"] == "running"
    assert conflict["reason_code"] == "request_digest_mismatch"


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
async def test_different_live_request_ids_cannot_share_one_workspace(
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


@pytest.mark.asyncio
async def test_two_independent_workspaces_run_and_third_workflow_is_rejected(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    service, manager, workspace_a = service_fixture
    workspace_b = workspace_a.parent / "workspace-b"
    workspace_c = workspace_a.parent / "workspace-c"
    workspace_b.mkdir()
    workspace_c.mkdir()
    manager.allowed_roots = (str(workspace_a.parent),)
    service = WorkflowService(manager, codex_bridge_port=18001)

    first = await service.start_workflow(
        client_request_id="phase5f4-workspace-a-0001",
        workflow_yaml=_workflow_yaml(),
        input_data={"answer": "A"},
        workspace_root=str(workspace_a),
    )
    second = await service.start_workflow(
        client_request_id="phase5f4-workspace-b-0001",
        workflow_yaml=_workflow_yaml(),
        input_data={"answer": "B"},
        workspace_root=str(workspace_b),
    )

    assert first.status == second.status == "running"
    assert first.workflow_id != second.workflow_id
    assert first.run_id != second.run_id
    assert len(manager.start_calls) == 2
    assert {call[1] for call in manager.start_calls} == {workspace_a, workspace_b}

    replayed = await service.start_workflow(
        client_request_id="phase5f4-workspace-a-0001",
        workflow_yaml=_workflow_yaml(),
        input_data={"answer": "A"},
        workspace_root=str(workspace_a),
    )
    assert (replayed.workflow_id, replayed.run_id) == (first.workflow_id, first.run_id)
    with pytest.raises(WorkflowRequestConflict):
        await service.start_workflow(
            client_request_id="phase5f4-workspace-a-0001",
            workflow_yaml=_workflow_yaml(),
            input_data={"answer": "changed"},
            workspace_root=str(workspace_a),
        )
    assert {snapshot.workflow_id for snapshot in await service.list_workflows()} == {
        first.workflow_id,
        second.workflow_id,
    }
    assert (await service.get_workflow(first.workflow_id)).run_id == first.run_id
    assert (await service.get_workflow(second.workflow_id)).run_id == second.run_id

    with pytest.raises(WorkflowConcurrencyConflict):
        await service.start_workflow(
            client_request_id="phase5f4-workspace-c-0001",
            workflow_yaml=_workflow_yaml(),
            input_data={"answer": "C"},
            workspace_root=str(workspace_c),
        )
    assert len(manager.start_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_is_child", "second_kind"),
    [
        (False, "same"),
        (False, "child"),
        (True, "parent"),
        pytest.param(
            False, "case", marks=pytest.mark.skipif(os.name != "nt", reason="Windows path case")
        ),
    ],
)
async def test_active_workspace_identity_and_parent_child_roots_conflict(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    first_is_child: bool,
    second_kind: str,
) -> None:
    _service, manager, workspace = service_fixture
    child = workspace / "child"
    child.mkdir()
    manager.allowed_roots = (str(workspace.parent),)
    service = WorkflowService(manager, codex_bridge_port=18001)
    first_workspace = child if first_is_child else workspace
    second_workspace = {
        "same": first_workspace,
        "child": child,
        "parent": workspace,
        "case": Path(str(workspace).upper()),
    }[second_kind]

    await service.start_workflow(
        client_request_id="phase5f4-workspace-overlap-01",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(first_workspace),
    )
    with pytest.raises(WorkflowConcurrencyConflict):
        await service.start_workflow(
            client_request_id="phase5f4-workspace-overlap-02",
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(second_workspace),
        )
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_target", ["missing", "state_root"])
async def test_active_workspace_path_that_cannot_be_safely_isolated_fails_closed(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    workspace_target: str,
) -> None:
    _service, manager, workspace = service_fixture
    manager.allowed_roots = (str(workspace.parent),)
    service = WorkflowService(manager, codex_bridge_port=18001)
    workspace_c = workspace.parent / "workspace-c"
    workspace_c.mkdir()
    accepted = await service.start_workflow(
        client_request_id="phase5f4-workspace-unknown-01",
        workflow_yaml=_workflow_yaml(),
        input_data={"value": 1},
        workspace_root=str(workspace),
    )
    run_dir = manager.start_calls[0][0]
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["workspace_root"] = str(
        manager.state_root if workspace_target == "state_root" else workspace.parent / "missing"
    )
    definition = json.loads((run_dir / "workflow.yaml").read_text(encoding="utf-8"))
    input_data = json.loads((run_dir / "input.json").read_text(encoding="utf-8"))
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": manifest["workspace_root"],
    }
    manifest["request_digest"] = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_requests SET request_digest = ? WHERE run_id = ?",
            (manifest["request_digest"], accepted.run_id),
        )

    manifest_before = manifest_path.read_bytes()
    with pytest.raises(WorkflowConcurrencyConflict):
        await service.start_workflow(
            client_request_id="phase5f4-workspace-unknown-02",
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(workspace_c),
        )

    assert manifest_path.read_bytes() == manifest_before
    assert len(manager.start_calls) == 1


@pytest.mark.asyncio
async def test_two_service_instances_enforce_capacity_two_atomically(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, manager, workspace_a = service_fixture
    workspace_b = workspace_a.parent / "workspace-b"
    workspace_c = workspace_a.parent / "workspace-c"
    workspace_b.mkdir()
    workspace_c.mkdir()
    manager.allowed_roots = (str(workspace_a.parent),)
    manager_two = FakeWorkerManager(
        manager.state_root,
        workspace_a,
        allowed_roots=(workspace_a.parent,),
    )
    service_one = WorkflowService(manager, codex_bridge_port=18001)
    service_two = WorkflowService(manager_two, codex_bridge_port=18001)

    original_connect = sqlite3.connect
    connection_labels: dict[int, str] = {}
    sqlite_stages: dict[str, list[str]] = {}
    connect_failures: list[str] = []

    def stage_for_statement(statement: str) -> str:
        normalized = " ".join(statement.strip().split()).upper()
        if normalized.startswith("PRAGMA "):
            pragma_name = normalized.split(maxsplit=1)[1].split("=", maxsplit=1)[0].strip()
            return f"PRAGMA_{pragma_name}"
        if normalized.startswith("CREATE TABLE"):
            return "DDL_CREATE_TABLE"
        if normalized.startswith("CREATE INDEX"):
            return "DDL_CREATE_INDEX"
        if normalized.startswith("BEGIN IMMEDIATE"):
            return "BEGIN_IMMEDIATE"
        if normalized.startswith("COMMIT"):
            return "COMMIT"
        if normalized.startswith("ROLLBACK"):
            return "ROLLBACK"
        return f"SQL_{normalized.split(maxsplit=1)[0]}" if normalized else "SQL_EMPTY"

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        try:
            connection = original_connect(*args, **kwargs)
        except sqlite3.Error as error:
            connect_failures.append(type(error).__name__)
            raise
        label = f"connection-{len(connection_labels) + 1}"
        connection_labels[id(connection)] = label
        sqlite_stages[label] = []
        connection.set_trace_callback(
            lambda statement: sqlite_stages[label].append(stage_for_statement(statement))
        )
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    async def start(service: WorkflowService, request_id: str, path: Path):
        return await service.start_workflow(
            client_request_id=request_id,
            workflow_yaml=_workflow_yaml(),
            input_data={"workspace": path.name},
            workspace_root=str(path),
        )

    outcomes = await asyncio.gather(
        start(service_one, "phase5f4-atomic-slot-01", workspace_a),
        start(service_two, "phase5f4-atomic-slot-02", workspace_b),
        start(service_one, "phase5f4-atomic-slot-03", workspace_c),
        return_exceptions=True,
    )
    accepted = [value for value in outcomes if not isinstance(value, BaseException)]

    def safe_message(error: BaseException) -> str:
        message = " ".join(str(error).split())
        for path in (manager.state_root, workspace_a.parent, workspace_a):
            message = message.replace(str(path), "<isolated-path>")
        return message[:180]

    def outcome_diagnostics(value: object) -> dict[str, object]:
        if not isinstance(value, BaseException):
            return {
                "kind": "accepted",
                "type": type(value).__name__,
                "status": getattr(value, "status", None),
            }
        exception_chain: list[BaseException] = []
        current: BaseException | None = value
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            exception_chain.append(current)
            current = current.__cause__ or current.__context__
        frames: list[str] = []
        stages: list[str] = []
        for error in exception_chain:
            traceback = error.__traceback__
            while traceback is not None:
                frame = traceback.tb_frame
                frames.append(
                    f"{Path(frame.f_code.co_filename).name}:{traceback.tb_lineno}:"
                    f"{frame.f_code.co_name}"
                )
                connection = frame.f_locals.get("connection")
                label = connection_labels.get(id(connection)) if connection is not None else None
                if label is not None:
                    stages.append(f"{label}:{sqlite_stages[label][-1:]}")
                traceback = traceback.tb_next
        return {
            "kind": "exception",
            "type": type(value).__name__,
            "message": safe_message(value),
            "exception_chain": [
                {"type": type(error).__name__, "message": safe_message(error)}
                for error in exception_chain
            ],
            "sqlite_stage": stages[-1:] or ["unmatched"],
            "frames": frames[-8:],
        }

    diagnostics = {
        "outcomes": [outcome_diagnostics(value) for value in outcomes],
        "sqlite_trace_stages": sqlite_stages,
        "sqlite_connect_failures": connect_failures,
    }

    assert len(accepted) == 2, f"capacity outcomes: {diagnostics}"
    assert sum(isinstance(value, WorkflowConcurrencyConflict) for value in outcomes) == 1, (
        f"capacity outcomes: {diagnostics}"
    )
    assert len(manager.start_calls) + len(manager_two.start_calls) == 2
    assert len({snapshot.run_id for snapshot in accepted}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("effective_state", ["queued", "needs_reconciliation", "unknown"])
async def test_nonterminal_effective_states_keep_a_workflow_slot(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    monkeypatch: pytest.MonkeyPatch,
    effective_state: str,
) -> None:
    _service, manager, workspace_a = service_fixture
    workspace_b = workspace_a.parent / "workspace-b"
    workspace_c = workspace_a.parent / "workspace-c"
    workspace_b.mkdir()
    workspace_c.mkdir()
    manager.allowed_roots = (str(workspace_a.parent),)
    service_one = WorkflowService(manager, codex_bridge_port=18001)

    if effective_state == "queued":

        async def interrupt_before_start(
            _row: object, _prepared: object, _diagnostic: object
        ) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(service_one, "_start_reserved_workflow", interrupt_before_start)
        with pytest.raises(asyncio.CancelledError):
            await service_one.start_workflow(
                client_request_id="phase5f4-queued-active-01",
                workflow_yaml=_workflow_yaml(),
                input_data={},
                workspace_root=str(workspace_a),
            )
    elif effective_state == "needs_reconciliation":
        manager.start_error = RuntimeError("uncertain test start")
        with pytest.raises(WorkflowReconciliationRequired):
            await service_one.start_workflow(
                client_request_id="phase5f4-reconcile-active-01",
                workflow_yaml=_workflow_yaml(),
                input_data={},
                workspace_root=str(workspace_a),
            )
        manager.start_error = None
    else:
        accepted = await service_one.start_workflow(
            client_request_id="phase5f4-unknown-active-01",
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(workspace_a),
        )
        (manager.state_root / accepted.run_id / "worker-entry-status.json").write_text(
            json.dumps({"state": "completed", "exit_code": 0, "error_code": None}),
            encoding="utf-8",
        )
        assert (await service_one.get_workflow(accepted.workflow_id)).status == "unknown"

    service_two = WorkflowService(manager, codex_bridge_port=18001)
    second = await service_two.start_workflow(
        client_request_id="phase5f4-nonterminal-second-01",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace_b),
    )
    assert second.status == "running"
    with pytest.raises(WorkflowConcurrencyConflict):
        await service_two.start_workflow(
            client_request_id="phase5f4-nonterminal-third-01",
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(workspace_c),
        )


@pytest.mark.asyncio
async def test_failed_to_start_is_terminal_and_does_not_consume_capacity(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
) -> None:
    _service, manager, workspace_a = service_fixture
    workspace_b = workspace_a.parent / "workspace-b"
    workspace_b.mkdir()
    manager.allowed_roots = (str(workspace_a.parent),)
    service = WorkflowService(manager, codex_bridge_port=18001)
    from codex_bridge.workflows.manager import WorkflowManagerError

    manager.start_error = WorkflowManagerError("worker not started")
    with pytest.raises(WorkflowStartFailed):
        await service.start_workflow(
            client_request_id="phase5f4-failed-to-start-01",
            workflow_yaml=_workflow_yaml(),
            input_data={},
            workspace_root=str(workspace_a),
        )
    manager.start_error = None

    accepted = await service.start_workflow(
        client_request_id="phase5f4-after-failed-start-01",
        workflow_yaml=_workflow_yaml(),
        input_data={},
        workspace_root=str(workspace_b),
    )
    assert accepted.status == "running"
    with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
        failed_state = connection.execute(
            "SELECT state FROM workflow_requests WHERE client_request_id = ?",
            ("phase5f4-failed-to-start-01",),
        ).fetchone()[0]
    assert failed_state == "failed_to_start"


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["one", "two", "three", "items"])
async def test_external_two_run_capacity_is_independent_of_sequential_job_template(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    template: str,
) -> None:
    _service, manager, workspace_a = service_fixture
    workspace_b = workspace_a.parent / "workspace-b"
    workspace_c = workspace_a.parent / "workspace-c"
    workspace_b.mkdir()
    workspace_c.mkdir()
    manager.allowed_roots = (str(workspace_a.parent),)
    service = WorkflowService(manager, codex_bridge_port=18001)

    if template == "one":
        definition, input_data, job_limit = _workflow_yaml(), {}, 1
    elif template == "two":
        definition, input_data, job_limit = _two_job_workflow_yaml(), {}, 2
    elif template == "three":
        definition = (
            Path(__file__).parents[1]
            / "poc"
            / "ows"
            / "fixtures"
            / "three_codex_job_loop_workflow.yaml"
        ).read_text(encoding="utf-8")
        input_data = {
            "issues": [
                {"number": 17, "enabled": True},
                {"number": 5, "enabled": False},
                {"number": 9, "enabled": True},
                {"number": 21, "enabled": True},
            ]
        }
        job_limit = 3
    else:
        definition = (
            Path(__file__).parents[1]
            / "poc"
            / "ows"
            / "fixtures"
            / "conditional_items_codex_workflow.yaml"
        ).read_text(encoding="utf-8")
        input_data = {
            "items": [
                {"id": "gamma", "enabled": True, "prompt": "reply gamma"},
                {"id": "beta", "enabled": False, "prompt": "reply beta"},
                {"id": "alpha", "enabled": True, "prompt": "reply alpha"},
            ]
        }
        job_limit = 3

    first = await service.start_workflow(
        client_request_id=f"phase5f4-{template}-capacity-01",
        workflow_yaml=definition,
        input_data=input_data,
        workspace_root=str(workspace_a),
        max_live_codex_jobs=job_limit,
    )
    second = await service.start_workflow(
        client_request_id=f"phase5f4-{template}-capacity-02",
        workflow_yaml=definition,
        input_data=input_data,
        workspace_root=str(workspace_b),
        max_live_codex_jobs=job_limit,
    )

    assert first.status == second.status == "running"
    assert len(manager.start_calls) == 2
    assert [call[2] for call in manager.start_calls] == [job_limit, job_limit]
    for snapshot in (first, second):
        manifest = json.loads(
            (manager.state_root / snapshot.run_id / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["request_digest"]
        assert manifest["control"].get("max_live_codex_jobs", 1) == job_limit
        with sqlite3.connect(manager.state_root / "requests.sqlite") as connection:
            ledger_digest = connection.execute(
                "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
                (snapshot.run_id,),
            ).fetchone()[0]
        assert manifest["request_digest"] == ledger_digest

    with pytest.raises(WorkflowConcurrencyConflict):
        await service.start_workflow(
            client_request_id=f"phase5f4-{template}-capacity-03",
            workflow_yaml=definition,
            input_data=input_data,
            workspace_root=str(workspace_c),
            max_live_codex_jobs=job_limit,
        )
    assert len(manager.start_calls) == 2


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
    events = [
        json.loads(line)
        for line in (manager.state_root / replay.run_id / "bridge-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["event"] == "request_start_failed")
    assert failure["state"] == "needs_reconciliation"
    assert failure["reason_code"] == "unexpected_start_failure"
    assert failure["exception_class"] == "WorkflowStartTestError"
    transition = next(
        event
        for event in events
        if event["event"] == "workflow_state_changed" and event["state"] == "needs_reconciliation"
    )
    assert transition["previous_state"] == "starting"


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
    events = [
        json.loads(line)
        for line in (manager.state_root / replay.run_id / "bridge-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["event"] == "request_start_failed")
    assert failure["state"] == "failed_to_start"
    assert failure["reason_code"] == "manager_start_failed"
    assert failure["exception_class"] == "WorkflowManagerError"


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
    assert set(manifest["control"]) == {
        "codex_job_live",
        "codex_bridge_port",
        "codex_job_max_polls",
        "codex_job_poll_interval_seconds",
    }
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
@pytest.mark.parametrize(
    "result_payload",
    [None, "{"],
    ids=["missing", "corrupt"],
)
async def test_completed_worker_without_valid_result_is_not_reported_as_success(
    service_fixture: tuple[WorkflowService, FakeWorkerManager, Path],
    result_payload: str | None,
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
    if result_payload is not None:
        (run_dir / "result.json").write_text(result_payload, encoding="utf-8")

    status = await service.get_workflow(accepted.workflow_id)
    result = await service.get_result(accepted.workflow_id)

    assert status.status == "unknown"
    assert result.workflow.status == "unknown"
    assert result.result is None


def test_interrupted_atomic_result_write_preserves_existing_result(tmp_path: Path) -> None:
    from codex_bridge.workflows.worker import _write_json

    result_path = tmp_path / "result.json"
    saved_result = '{"result":{"final_text":"saved"}}\n'
    result_path.write_text(saved_result, encoding="utf-8")

    with pytest.raises(TypeError):
        _write_json(
            result_path,
            {"first": "partial write", "unserializable": object()},
        )

    assert result_path.read_text(encoding="utf-8") == saved_result
    assert result_path.with_suffix(".json.tmp").is_file()


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
