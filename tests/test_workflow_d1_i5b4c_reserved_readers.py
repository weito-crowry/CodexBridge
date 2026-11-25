from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import codex_bridge.workflows.manager as manager_module
import codex_bridge.workflows.strict_reader_gate as strict_reader_gate
import codex_bridge.workflows.worker as worker_module
import codex_bridge.workflows.worker_entry as worker_entry
from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.manager import WorkflowManagerError, WorkflowWorkerManager
from codex_bridge.workflows.strict_manifest import StrictWorkflowSnapshotVerifier
from codex_bridge.workflows.strict_reader_gate import StrictReaderGateError
from codex_bridge.workflows.strict_requests import (
    StrictWorkflowRequestConflict,
    StrictWorkflowRequestStore,
)
from codex_bridge.workflows.strict_variable_mutations import (
    MutationContext,
    StrictWorkflowVariableMutationStore,
)
from tests.test_workflow_d1_i5a_manifest import _RUN_ID, _base_fixture, _prepare
from tests.test_workflow_d1_i5b3_worker_reader_gate import (
    _SECRET,
    _guard_worker_side_effects,
    _main_files,
    _make_strict_reserved_run,
)

_REQUEST_ID = "i5b4c-request-reserve-0001"


def _coordinator(
    state_root: Path,
    *,
    run_id: str = _RUN_ID,
    client_request_id: str = _REQUEST_ID,
    allowed_roots: tuple[str, ...],
) -> Any:
    coordinator = getattr(strict_reader_gate, "_reserve_prepared_strict_run", None)
    assert callable(coordinator), "prepared strict reservation coordinator is missing"
    return coordinator(
        state_root,
        run_id,
        client_request_id=client_request_id,
        allowed_roots=allowed_roots,
    )


def _write_worker_config(run_dir: Path, workspace: Path, mode: str) -> Path:
    config_path = run_dir / "worker-config-b4c.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": mode,
                "workspace_root": str(workspace.resolve()),
                "allowed_roots": [str(workspace.resolve())],
                "shutdown_file": f"worker-shutdown-{'c' * 32}.request",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_reserved_v2_readers_return_verified_pinned_data_and_require_allowed_roots(
    tmp_path: Path,
) -> None:
    run_dir, manifest = _make_strict_reserved_run(tmp_path)
    workspace = tmp_path / "workspace"
    allowed_roots = (str(workspace.resolve()),)
    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=allowed_roots,
        frozen=False,
    )

    manager_manifest = manager._read_manifest(run_dir)
    entry_manifest = worker_entry._read_manifest(run_dir, allowed_roots=allowed_roots)
    loaded_manifest, definition_text, normalized_input = worker_module.load_run_request(
        run_dir, allowed_roots=allowed_roots
    )

    assert manager_manifest["run_format_version"] == 2
    assert entry_manifest["run_id"] == run_dir.name
    assert loaded_manifest["request_digest"] == manifest["request_digest"]
    assert loaded_manifest["definition_sha256"] == manifest["definition_sha256"]
    assert definition_text == (run_dir / "workflow.yaml").read_text(encoding="utf-8")
    assert normalized_input == {"secret": _SECRET}
    assert "control" not in manager_manifest
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(WorkflowManagerError, match="^workflow request binding is invalid$"):
        WorkflowWorkerManager(
            state_root=run_dir.parent, allowed_roots=(str(outside),)
        )._read_manifest(run_dir)
    with pytest.raises(worker_entry.WorkflowWorkerEntryError):
        worker_entry._read_manifest(run_dir, allowed_roots=(str(outside),))
    with pytest.raises(worker_module.OwsError, match="workflow request binding is invalid"):
        worker_module.load_run_request(run_dir, allowed_roots=(str(outside),))


def test_unreserved_strict_v2_is_not_a_worker_loader_fallback(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    with pytest.raises(worker_module.OwsError, match="workflow request binding is invalid"):
        worker_module.load_run_request(
            fixture.run_directory,
            allowed_roots=(str(fixture.workspace.resolve()),),
        )


@pytest.mark.parametrize("mode", ["start", "recover"])
@pytest.mark.asyncio
async def test_manager_verifies_reserved_v2_then_stops_before_runtime_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    run_dir, _manifest = _make_strict_reserved_run(tmp_path)
    workspace = tmp_path / "workspace"
    manager = WorkflowWorkerManager(
        state_root=run_dir.parent,
        allowed_roots=(str(workspace.resolve()),),
        frozen=False,
    )
    before = _main_files(run_dir)
    spawned: list[object] = []

    async def fake_spawn(*args: object, **kwargs: object) -> Any:
        spawned.append((args, kwargs))
        raise AssertionError("strict v2 reached worker spawn")

    monkeypatch.setattr(manager_module.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(WorkflowManagerError, match="^strict workflow runtime is not supported$"):
        await manager.start_worker(run_dir, mode=mode)  # type: ignore[arg-type]

    assert spawned == []
    assert _main_files(run_dir) == before
    assert list(run_dir.glob("worker-config-*.json")) == []
    assert list(run_dir.glob("worker-shutdown-*.request")) == []
    assert not (run_dir / "worker-entry-status.json").exists()
    assert not (run_dir / "worker-lifecycle.jsonl").exists()


@pytest.mark.parametrize("mode", ["start", "recover"])
def test_worker_entry_validates_reserved_v2_then_writes_only_failure_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    run_dir, _manifest = _make_strict_reserved_run(tmp_path)
    workspace = tmp_path / "workspace"
    config_path = _write_worker_config(run_dir, workspace, mode)
    states: list[str] = []
    write_status = worker_entry._write_status

    def record_status(
        path: Path,
        *,
        state: str,
        exit_code: int | None,
        error_code: str | None,
    ) -> None:
        states.append(state)
        write_status(path, state=state, exit_code=exit_code, error_code=error_code)

    monkeypatch.setattr(worker_entry, "_write_status", record_status)
    monkeypatch.setattr(
        worker_entry,
        "missing_workflow_dependencies",
        lambda: pytest.fail("strict v2 reached dependency import/checks"),
    )
    monkeypatch.setattr(
        worker_module,
        "main",
        lambda _args: pytest.fail("strict v2 reached worker dispatch"),
    )

    result = worker_entry.main(
        ["--mode", mode, "--run-dir", str(run_dir), "--config", str(config_path)]
    )
    stderr = capsys.readouterr().err
    status = json.loads((run_dir / "worker-entry-status.json").read_text(encoding="utf-8"))

    assert result == 2
    assert states == ["failed"]
    assert status["state"] == "failed"
    assert status["error_code"] == "invalid_config"
    assert "strict workflow runtime is not supported" in stderr
    assert _SECRET not in stderr
    assert not (run_dir / "start-worker-identity.json").exists()
    assert not (run_dir / "worker-lifecycle.jsonl").exists()


def test_direct_worker_main_reads_verified_v2_then_refuses_runtime_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _manifest = _make_strict_reserved_run(tmp_path)
    workspace = tmp_path / "workspace"
    before = _main_files(run_dir)
    _guard_worker_side_effects(monkeypatch)

    with pytest.raises(worker_module.OwsError, match="^strict workflow runtime is not supported$"):
        worker_module.main(
            [
                "--mode",
                "start",
                "--run-dir",
                str(run_dir),
                "--workspace-root",
                str(workspace.resolve()),
                "--allowed-root",
                str(workspace.resolve()),
            ]
        )

    assert _main_files(run_dir) == before
    assert not (run_dir / "start-worker.json").exists()
    assert not (run_dir / "start-worker-identity.json").exists()
    assert not (run_dir / "worker-lifecycle.jsonl").exists()
    assert not (run_dir / "result.json").exists()
    assert not (run_dir / "dbos.sqlite").exists()
    assert not (run_dir / "codex_jobs.sqlite").exists()


@pytest.mark.asyncio
async def test_persisted_shutdown_reader_verifies_v2_but_never_claims_its_job_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _manifest = _make_strict_reserved_run(tmp_path)
    workspace = tmp_path / "workspace"
    database = run_dir / "codex_jobs.sqlite"
    database.write_text("fake-owned-thread-private-b4c", encoding="utf-8")
    roots = (str(workspace.resolve()),)
    with sqlite3.connect(run_dir.parent / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_requests SET state = 'running' WHERE run_id = ?",
            (run_dir.name,),
        )

    persisted, complete = manager_module._persisted_live_run_directories(
        run_dir.parent, allowed_roots=roots
    )

    assert persisted == ()
    assert complete is False

    monkeypatch.setattr(
        CodexJobAdapter,
        "read_shutdown_ownership",
        lambda *_args, **_kwargs: pytest.fail("strict v2 job evidence was treated as ownership"),
    )
    report = await _shutdown_manager(run_dir.parent, roots)

    assert report.ownership_complete is False
    assert report.owned_turns == frozenset()
    assert "fake-owned-thread-private-b4c" not in repr(report)


async def _shutdown_manager(state_root: Path, allowed_roots: tuple[str, ...]) -> Any:
    manager = WorkflowWorkerManager(state_root=state_root, allowed_roots=allowed_roots)
    return await manager.shutdown()


def test_prepared_strict_reservation_coordinator_orders_verify_reserve_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    prepared = _prepare(fixture)
    events: list[str] = []
    original_pre = StrictWorkflowSnapshotVerifier.verify_pre_reservation
    original_reserved = StrictWorkflowSnapshotVerifier.verify_reserved
    original_reserve = StrictWorkflowRequestStore.reserve_strict

    def verify_pre(self: StrictWorkflowSnapshotVerifier, run_id: str) -> Any:
        events.append("pre-reservation")
        return original_pre(self, run_id)

    def reserve(self: StrictWorkflowRequestStore, request: Any) -> Any:
        events.append("reserve_strict")
        return original_reserve(self, request)

    def verify_reserved(self: StrictWorkflowSnapshotVerifier, run_id: str) -> Any:
        events.append("verify_reserved")
        return original_reserved(self, run_id)

    monkeypatch.setattr(StrictWorkflowSnapshotVerifier, "verify_pre_reservation", verify_pre)
    monkeypatch.setattr(StrictWorkflowRequestStore, "reserve_strict", reserve)
    monkeypatch.setattr(StrictWorkflowSnapshotVerifier, "verify_reserved", verify_reserved)

    result = _coordinator(
        fixture.state_root,
        client_request_id=_REQUEST_ID,
        allowed_roots=(str(fixture.workspace.resolve()),),
    )

    assert events == ["pre-reservation", "reserve_strict", "verify_reserved"]
    assert result.reservation.client_request_id == _REQUEST_ID
    assert result.reservation.run_id == prepared.run_id
    assert result.verification.reservation_verified is True
    assert result.verification.manifest == prepared
    assert result.verification._normalized_input == fixture.normalized_input.normalized_input
    assert "private-input-sentinel" not in repr(result)


def test_reservation_failure_keeps_prepared_snapshot_and_creates_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    manifest_before = (fixture.run_directory / "manifest.json").read_bytes()
    input_before = (fixture.run_directory / "input.json").read_bytes()

    def fail_reservation(
        _store: StrictWorkflowRequestStore,
        _request: Any,
    ) -> Any:
        raise StrictWorkflowRequestConflict("request identity is already reserved")

    monkeypatch.setattr(StrictWorkflowRequestStore, "reserve_strict", fail_reservation)

    with pytest.raises(StrictReaderGateError):
        _coordinator(
            fixture.state_root,
            client_request_id=_REQUEST_ID,
            allowed_roots=(str(fixture.workspace.resolve()),),
        )

    assert StrictWorkflowRequestStore(fixture.state_root).read_binding(_RUN_ID) is None
    assert (fixture.run_directory / "manifest.json").read_bytes() == manifest_before
    assert (fixture.run_directory / "input.json").read_bytes() == input_before


def test_post_reservation_verification_failure_leaves_reservation_for_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    original_verify_reserved = StrictWorkflowSnapshotVerifier.verify_reserved

    def fail_verification(
        _verifier: StrictWorkflowSnapshotVerifier,
        _run_id: str,
    ) -> Any:
        raise RuntimeError("post-reservation verification did not complete")

    monkeypatch.setattr(StrictWorkflowSnapshotVerifier, "verify_reserved", fail_verification)
    with pytest.raises(StrictReaderGateError):
        _coordinator(
            fixture.state_root,
            client_request_id=_REQUEST_ID,
            allowed_roots=(str(fixture.workspace.resolve()),),
        )

    reservation = StrictWorkflowRequestStore(fixture.state_root).read_binding(_RUN_ID)
    assert reservation is not None
    assert reservation.client_request_id == _REQUEST_ID
    assert reservation.state == "queued"

    monkeypatch.setattr(StrictWorkflowSnapshotVerifier, "verify_reserved", original_verify_reserved)
    monkeypatch.setattr(
        StrictWorkflowRequestStore,
        "reserve_strict",
        lambda *_args, **_kwargs: pytest.fail("recheck attempted a second reservation"),
    )
    rechecked = _coordinator(
        fixture.state_root,
        client_request_id=_REQUEST_ID,
        allowed_roots=(str(fixture.workspace.resolve()),),
    )
    assert rechecked.reservation.run_id == _RUN_ID
    assert rechecked.verification.reservation_verified is True


def test_coordinator_replay_uses_reserved_verifier_after_variable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    roots = (str(fixture.workspace.resolve()),)
    first = _coordinator(
        fixture.state_root,
        client_request_id=_REQUEST_ID,
        allowed_roots=roots,
    )
    StrictWorkflowVariableMutationStore(
        fixture.run_directory,
        fixture.binding,
        fixture.definition.contract,
    ).apply(
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
    monkeypatch.setattr(
        StrictWorkflowSnapshotVerifier,
        "verify_pre_reservation",
        lambda *_args, **_kwargs: pytest.fail("mutated replay used pre-reservation verifier"),
    )
    monkeypatch.setattr(
        StrictWorkflowRequestStore,
        "reserve_strict",
        lambda *_args, **_kwargs: pytest.fail("replay attempted to reserve again"),
    )

    replay = _coordinator(
        fixture.state_root,
        client_request_id=_REQUEST_ID,
        allowed_roots=roots,
    )

    assert replay.reservation.run_id == first.reservation.run_id == _RUN_ID
    assert replay.reservation.client_request_id == first.reservation.client_request_id
    assert replay.verification.reservation_verified is True
    assert replay.verification.mutation_count == 1


def test_coordinator_rejects_replay_identity_conflict_without_overwriting_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    roots = (str(fixture.workspace.resolve()),)
    first = _coordinator(fixture.state_root, client_request_id=_REQUEST_ID, allowed_roots=roots)
    monkeypatch.setattr(
        StrictWorkflowRequestStore,
        "reserve_strict",
        lambda *_args, **_kwargs: pytest.fail("identity conflict attempted another reserve"),
    )

    with pytest.raises(StrictReaderGateError):
        _coordinator(
            fixture.state_root,
            client_request_id="i5b4c-different-request-0001",
            allowed_roots=roots,
        )

    persisted = StrictWorkflowRequestStore(fixture.state_root).read_binding(_RUN_ID)
    assert persisted is not None
    assert persisted.client_request_id == first.reservation.client_request_id == _REQUEST_ID


def test_coordinator_rejects_prepared_snapshot_mutation_without_reserving(
    tmp_path: Path,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    input_path = fixture.run_directory / "input.json"
    input_path.write_bytes(input_path.read_bytes() + b" ")

    with pytest.raises(StrictReaderGateError):
        _coordinator(
            fixture.state_root,
            client_request_id=_REQUEST_ID,
            allowed_roots=(str(fixture.workspace.resolve()),),
        )

    assert StrictWorkflowRequestStore(fixture.state_root).read_binding(_RUN_ID) is None
