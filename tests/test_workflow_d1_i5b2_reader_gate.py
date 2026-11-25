from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.contracts import DRAFT_2020_12
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT
from codex_bridge.workflows.manager import (
    WorkflowManagerError,
    WorkflowWorkerManager,
    _persisted_live_run_directories,
)
from codex_bridge.workflows.service import (
    WorkflowService,
    _RequestRow,
    _RunInspection,
)
from codex_bridge.workflows.strict_binding import StrictWorkflowBindingVerifier
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowSnapshotVerifier,
    StrictWorkflowSnapshotWriter,
)
from codex_bridge.workflows.strict_requests import (
    StrictWorkflowRequestInput,
    StrictWorkflowRequestStore,
)
from codex_bridge.workflows.strict_variable_mutations import (
    MutationContext,
    StrictWorkflowVariableMutationStore,
)
from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableStore,
)
from codex_bridge.workflows.task_projection import (
    project_workflow_task_events,
    project_workflow_tasks,
)

_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
_RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CREATED_AT = datetime(2026, 11, 20, 3, 4, 5, 123456, tzinfo=UTC)
_SECRET_SENTINEL = "private-input-sentinel-b2"
_PROMPT_SENTINEL = "private-prompt-sentinel-b2"
_STRICT_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5b2-reader-gate\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - first:\n"
    "      call: codex-job\n"
    "      with:\n"
    f'        prompt: "{_PROMPT_SENTINEL}"\n'
)


class _FakeProcess:
    pid = 43210
    returncode = 0

    async def wait(self) -> int:
        return 0


class _ReadOnlyWorkerManager:
    def __init__(self, state_root: Path, allowed_root: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(allowed_root),)


@dataclass(frozen=True)
class _StrictFixture:
    state_root: Path
    run_dir: Path
    manifest: dict[str, Any]
    workspace: Path


def _strict_fixture(
    tmp_path: Path,
    *,
    max_live_codex_jobs: int = 1,
    source_yaml: str = _STRICT_YAML,
) -> _StrictFixture:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    requests = StrictWorkflowRequestStore(state_root)
    requests.initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    schema = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "additionalProperties": False,
    }
    contract = {
        "contract_version": 1,
        "input_schema": {
            **schema,
            "properties": {"secret": {"type": "string"}},
        },
        "output_schema": schema,
        "variables": [
            {
                "declaration_id": "counter",
                "name": "counter",
                "owner_scope": "workflow",
                "value_schema": {**schema, "type": "integer"},
                "sensitivity": "public",
                "initial": 0,
            }
        ],
    }
    definition = definitions.create(
        mutation_request_id="i5b2-definition-create-0001",
        source_yaml=source_yaml,
        max_live_codex_jobs=max_live_codex_jobs,
        contract=contract,
    )
    normalized = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        definition.definition_id,
        definition.revision,
        {"secret": _SECRET_SENTINEL},
    )
    run_dir = state_root / _RUN_ID
    run_dir.mkdir()
    binding = StrictWorkflowRunBindingInput(
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        definition_id=definition.definition_id,
        definition_revision=definition.revision,
        contract_sha256=definition.contract_sha256,
        binding_digest=definition.binding_digest,
        normalized_input_sha256=normalized.normalized_input_sha256,
        created_at=_CREATED_AT,
    )
    StrictWorkflowVariableStore(run_dir).initialize_new_database(binding)
    writer = StrictWorkflowSnapshotWriter(state_root, (str(workspace),))
    manifest_value = writer.prepare_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        definition_id=definition.definition_id,
        definition_revision=definition.revision,
        workspace_root=str(workspace.resolve()),
        normalized_input=normalized,
        created_at=_CREATED_AT,
    )
    requests.reserve_strict(
        StrictWorkflowRequestInput(
            client_request_id="i5b2-request-create-0001",
            workflow_id=manifest_value.workflow_id,
            run_id=manifest_value.run_id,
            request_digest=manifest_value.request_digest,
            definition_id=manifest_value.definition_id,
            definition_revision=manifest_value.definition_revision,
            source_sha256=manifest_value.source_sha256,
            contract_sha256=manifest_value.contract_sha256,
            binding_digest=manifest_value.binding_digest,
            normalized_input_sha256=manifest_value.normalized_input_sha256,
            created_at=_CREATED_AT.isoformat(),
        )
    )
    mutation_store = StrictWorkflowVariableMutationStore(run_dir, binding, definition.contract)
    mutation_store.apply(
        MutationContext(
            node_path="/do/0/first",
            iteration_vector=(),
            frame_id="workflow-root",
            mutation_ordinal=0,
        ),
        "counter",
        "initialize",
        expected_before_version=0,
        expected_before_digest=None,
    )
    assert (
        StrictWorkflowSnapshotVerifier(state_root, (str(workspace),))
        .verify_reserved(_RUN_ID)
        .reservation_verified
    )
    return _StrictFixture(state_root, run_dir, manifest_value.to_dict(), workspace)


def _request_row(manifest: dict[str, Any], *, state: str = "running") -> _RequestRow:
    return _RequestRow(
        client_request_id="i5b2-request-create-0001",
        workflow_id=manifest["workflow_id"],
        run_id=manifest["run_id"],
        request_digest=manifest["request_digest"],
        state=state,
        created_at=_CREATED_AT.isoformat(),
        updated_at=_CREATED_AT.isoformat(),
    )


def _inspection() -> _RunInspection:
    return _RunInspection(
        status="needs_reconciliation",
        worker_state="unknown",
        job_id=None,
        thread_id=None,
        result=None,
        updated_at=_CREATED_AT.isoformat(),
    )


def _make_request_database(
    state_root: Path,
    *,
    run_id: str,
    workflow_id: str,
    request_digest: str,
    version: int,
    state: str = "running",
) -> Path:
    store = StrictWorkflowRequestStore(state_root)
    store.initialize_new_database()
    database = state_root / "requests.sqlite"
    with sqlite3.connect(database) as connection:
        if version == 0:
            connection.execute("DROP TABLE workflow_request_contract_bindings")
            connection.execute("PRAGMA user_version=0")
        connection.execute(
            "INSERT INTO workflow_requests (client_request_id, workflow_id, run_id, "
            "request_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "i5b2-legacy-request-0001",
                workflow_id,
                run_id,
                request_digest,
                state,
                _CREATED_AT.isoformat(),
                _CREATED_AT.isoformat(),
            ),
        )
    return database


def _write_legacy_manifest(
    run_dir: Path,
    *,
    workflow_id: str,
    run_id: str,
    request_digest: str,
    live: bool,
) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "request_digest": request_digest,
                "mode": "codex-job-live",
                "control": {"codex_job_live": live},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["start", "recover"])
@pytest.mark.parametrize("version", [2, 1, 3, True, None])
async def test_manager_rejects_every_explicit_run_format_before_config_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    version: object,
) -> None:
    state_root = tmp_path / "state"
    run_id = _RUN_ID
    run_dir = state_root / run_id
    state_root.mkdir()
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "workflow_id": _WORKFLOW_ID,
                "run_id": run_id,
                "request_digest": "a" * 64,
                "run_format_version": version,
                "control": {"codex_job_live": False},
            }
        ),
        encoding="utf-8",
    )
    spawned: list[object] = []

    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProcess:
        spawned.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr("codex_bridge.workflows.manager.asyncio.create_subprocess_exec", fake_spawn)
    manager = WorkflowWorkerManager(state_root=state_root, frozen=False)

    expected_error = (
        "workflow request binding is invalid"
        if type(version) is int and version == 2
        else "workflow run format is unsupported"
    )
    with pytest.raises(WorkflowManagerError, match=f"^{expected_error}$"):
        await manager.start_worker(run_dir, mode=mode)  # type: ignore[arg-type]

    assert spawned == []
    assert list(run_dir.glob("worker-config-*.json")) == []
    assert list(run_dir.glob("worker-shutdown-*.request")) == []
    assert not (run_dir / "codex_jobs.sqlite").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 1])
async def test_manager_accepts_bound_legacy_v0_and_v1_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / _RUN_ID
    state_root.mkdir()
    request_digest = "b" * 64
    _make_request_database(
        state_root,
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        request_digest=request_digest,
        version=version,
    )
    run_dir.mkdir()
    _write_legacy_manifest(
        run_dir,
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        request_digest=request_digest,
        live=False,
    )
    spawned: list[object] = []

    async def fake_spawn(*args: object, **kwargs: object) -> _FakeProcess:
        spawned.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr("codex_bridge.workflows.manager.asyncio.create_subprocess_exec", fake_spawn)
    manager = WorkflowWorkerManager(state_root=state_root, frozen=False)
    handle = await manager.start_worker(run_dir, mode="start")
    await handle.wait()

    assert len(spawned) == 1
    assert len(list(run_dir.glob("worker-config-*.json"))) == 1


def test_manager_keeps_standalone_legacy_mode_without_request_database(tmp_path: Path) -> None:
    run_dir = tmp_path / "state" / _RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"control": {"codex_job_live": False}}), encoding="utf-8"
    )

    manager = WorkflowWorkerManager(state_root=run_dir.parent)

    assert manager._read_manifest(run_dir)["control"] == {"codex_job_live": False}
    assert not (run_dir.parent / "requests.sqlite").exists()


def test_manager_rejects_unversioned_manifest_for_a_strict_binding(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    manifest = dict(fixture.manifest)
    manifest.pop("run_format_version")
    (fixture.run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    manager = WorkflowWorkerManager(state_root=fixture.state_root)

    with pytest.raises(WorkflowManagerError, match="^workflow request binding is invalid$"):
        manager._read_manifest(fixture.run_dir)


@pytest.mark.parametrize("database_kind", ["corrupt", "unsupported", "missing-row"])
def test_manager_fails_closed_when_request_database_is_present_but_unusable(
    tmp_path: Path,
    database_kind: str,
) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / _RUN_ID
    state_root.mkdir()
    database = state_root / "requests.sqlite"
    if database_kind == "corrupt":
        database.write_bytes(b"not a sqlite database")
    elif database_kind == "unsupported":
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=9")
    else:
        StrictWorkflowRequestStore(state_root).initialize_new_database()
    run_dir.mkdir()
    _write_legacy_manifest(
        run_dir,
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        request_digest="c" * 64,
        live=False,
    )

    manager = WorkflowWorkerManager(state_root=state_root)

    with pytest.raises(WorkflowManagerError, match="^workflow request binding is invalid$"):
        manager._read_manifest(run_dir)


@pytest.mark.parametrize("version", [0, 1])
@pytest.mark.asyncio
async def test_persisted_legacy_v0_and_v1_runs_keep_shutdown_turn_ownership(
    tmp_path: Path,
    version: int,
) -> None:
    state_root = tmp_path / "state"
    run_dir = state_root / _RUN_ID
    state_root.mkdir()
    request_digest = "d" * 64
    _make_request_database(
        state_root,
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        request_digest=request_digest,
        version=version,
    )
    run_dir.mkdir()
    _write_legacy_manifest(
        run_dir,
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        request_digest=request_digest,
        live=True,
    )
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        _WORKFLOW_ID,
        "do[0].first",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])
    adapter.mark_accepted(prepared["job_id"], "legacy-thread-123", "legacy-turn-123", "in_progress")
    db_bytes_before = (state_root / "requests.sqlite").read_bytes()

    persisted, complete = _persisted_live_run_directories(state_root)
    report = await WorkflowWorkerManager(state_root=state_root).shutdown()

    assert complete is True
    assert persisted == (run_dir.resolve(),)
    assert report.ownership_complete is True
    assert report.owned_turns == {("legacy-thread-123", "legacy-turn-123")}
    assert (state_root / "requests.sqlite").read_bytes() == db_bytes_before


@pytest.mark.parametrize("case", ["strict-v2", "mismatched-legacy", "malformed-manifest"])
@pytest.mark.asyncio
async def test_unverified_persisted_runs_make_shutdown_ownership_incomplete(
    tmp_path: Path,
    case: str,
) -> None:
    if case == "strict-v2":
        fixture = _strict_fixture(tmp_path)
        state_root = fixture.state_root
        run_dir = fixture.run_dir
        with sqlite3.connect(state_root / "requests.sqlite") as connection:
            connection.execute(
                "UPDATE workflow_requests SET state='running' WHERE run_id=?", (_RUN_ID,)
            )
    else:
        state_root = tmp_path / "state"
        state_root.mkdir()
        _make_request_database(
            state_root,
            run_id=_RUN_ID,
            workflow_id=_WORKFLOW_ID,
            request_digest="e" * 64,
            version=1,
        )
        run_dir = state_root / _RUN_ID
        run_dir.mkdir()
        _write_legacy_manifest(
            run_dir,
            workflow_id=_WORKFLOW_ID,
            run_id=_RUN_ID,
            request_digest="f" * 64 if case == "mismatched-legacy" else "e" * 64,
            live=True,
        )
        if case == "malformed-manifest":
            (run_dir / "manifest.json").write_bytes(b"{malformed")

    persisted, complete = _persisted_live_run_directories(state_root)
    report = await WorkflowWorkerManager(state_root=state_root).shutdown()

    assert complete is False
    assert run_dir.resolve() not in persisted
    assert report.ownership_complete is False


def test_persisted_reader_keeps_other_verified_legacy_runs_when_one_is_invalid(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    first_digest = "1" * 64
    _make_request_database(
        state_root,
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        request_digest=first_digest,
        version=1,
    )
    second_run_id = "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    second_workflow_id = "33333333-3333-4333-8333-333333333333"
    second_digest = "2" * 64
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(
            "INSERT INTO workflow_requests (client_request_id, workflow_id, run_id, "
            "request_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "i5b2-legacy-request-0002",
                second_workflow_id,
                second_run_id,
                second_digest,
                "running",
                _CREATED_AT.isoformat(),
                _CREATED_AT.isoformat(),
            ),
        )
    verified_run = state_root / _RUN_ID
    invalid_run = state_root / second_run_id
    verified_run.mkdir()
    invalid_run.mkdir()
    _write_legacy_manifest(
        verified_run,
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        request_digest=first_digest,
        live=True,
    )
    (invalid_run / "manifest.json").write_bytes(b"{malformed")

    persisted, complete = _persisted_live_run_directories(state_root)

    assert persisted == (verified_run.resolve(),)
    assert complete is False


@pytest.mark.parametrize(
    "invalid_case",
    [
        "unversioned-bound",
        "unknown-version",
        "missing-binding",
        "corrupt-db",
        "malformed-manifest",
    ],
)
def test_task_and_event_projection_reject_strict_or_invalid_run_sources_without_leaks(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    fixture = _strict_fixture(tmp_path)
    manifest = dict(fixture.manifest)
    manifest_path = fixture.run_dir / "manifest.json"
    requests_database = fixture.state_root / "requests.sqlite"
    if invalid_case == "unversioned-bound":
        manifest.pop("run_format_version")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif invalid_case == "unknown-version":
        manifest["run_format_version"] = 3
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif invalid_case == "missing-binding":
        with sqlite3.connect(requests_database) as connection:
            connection.execute(
                "DELETE FROM workflow_request_contract_bindings WHERE run_id=?", (_RUN_ID,)
            )
    elif invalid_case == "corrupt-db":
        requests_database.write_bytes(b"corrupt request database")
    elif invalid_case == "malformed-manifest":
        manifest_path.write_bytes(b"{malformed")

    row = _request_row(manifest)
    tracked_files = (
        requests_database,
        manifest_path,
        fixture.run_dir / "workflow.yaml",
        fixture.run_dir / "input.json",
        fixture.run_dir / "variables.sqlite",
    )
    before = {path: path.read_bytes() for path in tracked_files}

    allowed_roots = (str(fixture.workspace),)
    projection = project_workflow_tasks(
        fixture.state_root, row, _inspection(), allowed_roots=allowed_roots
    )
    event_page = project_workflow_task_events(
        fixture.state_root,
        row,
        _inspection(),
        after_id=0,
        limit=10,
        allowed_roots=allowed_roots,
    )
    combined = json.dumps((projection, event_page), ensure_ascii=False)

    assert projection["coverage"] == "unavailable"
    assert projection["coverage_reason"] == "run_integrity_invalid"
    assert projection["nodes"] == []
    assert projection["edges"] == []
    assert projection["counts"] == {"nodes": 0, "edges": 0, "executions": 0}
    assert event_page["coverage"] == "unavailable"
    assert event_page["coverage_reason"] == "run_integrity_invalid"
    assert event_page["events"] == []
    assert _SECRET_SENTINEL not in combined
    assert _PROMPT_SENTINEL not in combined
    assert {path: path.read_bytes() for path in tracked_files} == before


@pytest.mark.asyncio
async def test_service_keeps_strict_run_in_reconciliation_with_static_projection(
    tmp_path: Path,
) -> None:
    fixture = _strict_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    service = WorkflowService(
        _ReadOnlyWorkerManager(fixture.state_root, workspace), codex_bridge_port=18001
    )
    db_bytes_before = (fixture.state_root / "requests.sqlite").read_bytes()
    snapshot_files = (
        fixture.run_dir / "manifest.json",
        fixture.run_dir / "workflow.yaml",
        fixture.run_dir / "input.json",
        fixture.run_dir / "variables.sqlite",
    )
    snapshot_bytes_before = {path: path.read_bytes() for path in snapshot_files}

    status = await service.get_workflow(_WORKFLOW_ID)
    result = await service.get_result(_WORKFLOW_ID)
    tasks = await service.get_workflow_tasks(_WORKFLOW_ID)
    events = await service.get_workflow_task_events(_WORKFLOW_ID, after_id=0, limit=10)

    assert status.status == "needs_reconciliation"
    assert status.worker_state == "unknown"
    assert status.terminal is False
    assert result.workflow.status == "needs_reconciliation"
    assert result.workflow.worker_state == "unknown"
    assert result.workflow.terminal is False
    assert result.result is None
    assert tasks["coverage"] == "partial"
    assert tasks["coverage_reason"] == "workflow_not_confirmed"
    assert tasks["nodes"][0]["node_id"] == "/do/0/first"
    assert tasks["nodes"][0]["state"] == "unknown"
    assert tasks["nodes"][0]["completion_observed"] is False
    assert tasks["nodes"][0]["executions"] == []
    assert tasks["counts"]["executions"] == 0
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "workflow_not_confirmed"
    assert events["events"] == []
    assert events["latest_event_id"] == 0
    assert (fixture.state_root / "requests.sqlite").read_bytes() == db_bytes_before
    assert {path: path.read_bytes() for path in snapshot_files} == snapshot_bytes_before
    output = json.dumps((tasks, events), ensure_ascii=False)
    assert _SECRET_SENTINEL not in output
    assert _PROMPT_SENTINEL not in output
