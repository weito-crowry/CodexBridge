from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows.contracts import DRAFT_2020_12
from codex_bridge.workflows.service import (
    WorkflowConcurrencyConflict,
    WorkflowService,
    _inspect_run,
    _RequestRow,
    _verified_active_workspace,
)
from codex_bridge.workflows.strict_binding import StrictWorkflowBindingVerifier
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowSnapshotWriter,
    serialize_strict_manifest_v2,
)
from codex_bridge.workflows.strict_reader_gate import (
    RunFormatVersion,
    StrictReaderGateError,
    classify_readonly,
)
from codex_bridge.workflows.strict_requests import (
    StrictWorkflowRequestInput,
    StrictWorkflowRequestStore,
)
from codex_bridge.workflows.strict_variables import (
    StrictWorkflowRunBindingInput,
    StrictWorkflowVariableStore,
)

_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
_RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CREATED_AT = datetime(2026, 11, 19, 3, 4, 5, 123456, tzinfo=UTC)
_VALID_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5b1-reader-gate\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - first:\n"
    "      call: codex-job\n"
    "      with:\n"
    '        prompt: "strict reader gate fixture"\n'
)
_LEGACY_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5b1-legacy\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - calculate:\n"
    "      call: codex-job\n"
    "      with:\n"
    '        prompt: "legacy reader gate fixture"\n'
)


class _WorkerManager:
    def __init__(self, state_root: Path, workspace: Path) -> None:
        self.state_root = state_root
        self.allowed_roots = (str(workspace),)
        self.start_calls: list[Path] = []

    def create_run_directory(self) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.state_root / f"run-{len(self.start_calls) + 1:032x}"
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
        self.start_calls.append(run_dir)
        (run_dir / "worker-entry-status.json").write_text(
            '{"state":"running","exit_code":null,"error_code":null}',
            encoding="utf-8",
        )
        return object()


@dataclass(frozen=True)
class _StrictFixture:
    state_root: Path
    workspace: Path
    run_directory: Path
    manifest: Any
    request_digest: str


def _strict_fixture(tmp_path: Path) -> _StrictFixture:
    tmp_path.mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "strict-state"
    workspace = tmp_path / "workspace"
    state_root.mkdir()
    workspace.mkdir()

    requests = StrictWorkflowRequestStore(state_root)
    requests.initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    schema = {"$schema": DRAFT_2020_12, "type": "object", "additionalProperties": False}
    definition = definitions.create(
        mutation_request_id="i5b1-definition-create-0001",
        source_yaml=_VALID_YAML,
        max_live_codex_jobs=1,
        contract={
            "contract_version": 1,
            "input_schema": schema,
            "output_schema": schema,
            "variables": [],
        },
    )
    normalized = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        definition.definition_id, definition.revision, {}
    )

    run_directory = state_root / _RUN_ID
    run_directory.mkdir()
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
    StrictWorkflowVariableStore(run_directory).initialize_new_database(binding)
    manifest = StrictWorkflowSnapshotWriter(state_root, (str(workspace),)).prepare_snapshot(
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
            client_request_id="i5b1-request-create-0001",
            workflow_id=_WORKFLOW_ID,
            run_id=_RUN_ID,
            request_digest=manifest.request_digest,
            definition_id=manifest.definition_id,
            definition_revision=manifest.definition_revision,
            source_sha256=manifest.source_sha256,
            contract_sha256=manifest.contract_sha256,
            binding_digest=manifest.binding_digest,
            normalized_input_sha256=manifest.normalized_input_sha256,
            created_at=_CREATED_AT.isoformat(),
        )
    )
    return _StrictFixture(state_root, workspace, run_directory, manifest, manifest.request_digest)


def _strict_row(fixture: _StrictFixture) -> _RequestRow:
    created = _CREATED_AT.isoformat()
    return _RequestRow(
        "i5b1-request-create-0001",
        _WORKFLOW_ID,
        _RUN_ID,
        fixture.request_digest,
        "queued",
        created,
        created,
    )


def _encode_manifest(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _readonly_counts(database: Path) -> tuple[int, ...]:
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "workflow_requests",
                "workflow_request_sources",
                "workflow_request_contract_bindings",
            )
        )


async def _start_legacy_run(
    state_root: Path, workspace: Path, request_suffix: str
) -> tuple[WorkflowService, _WorkerManager, Any]:
    manager = _WorkerManager(state_root, workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)
    accepted = await service.start_workflow(
        client_request_id=f"i5b1-legacy-{request_suffix}-001",
        workflow_yaml=_LEGACY_YAML,
        input_data={"answer": 4},
        workspace_root=str(workspace),
    )
    return service, manager, accepted


@pytest.mark.asyncio
async def test_legacy_v0_and_migrated_v1_keep_service_status_and_response(tmp_path: Path) -> None:
    state_v0 = tmp_path / "legacy-v0-state"
    workspace_v0 = tmp_path / "workspace-v0"
    workspace_v0.mkdir()
    service_v0, _, accepted_v0 = await _start_legacy_run(state_v0, workspace_v0, "v0")
    assert accepted_v0.status == "running"
    database_v0 = state_v0 / "requests.sqlite"
    with closing(sqlite3.connect(database_v0)) as connection:
        digest_v0 = connection.execute(
            "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
            (accepted_v0.run_id,),
        ).fetchone()[0]
    row_v0 = _RequestRow(
        f"i5b1-legacy-v0-{accepted_v0.run_id[-4:]}",
        accepted_v0.workflow_id,
        accepted_v0.run_id,
        digest_v0,
        "running",
        accepted_v0.created_at,
        accepted_v0.updated_at,
    )
    assert (
        _verified_active_workspace(state_v0, row_v0, (str(workspace_v0),)) == workspace_v0.resolve()
    )
    snapshot_v0 = await service_v0.get_workflow(accepted_v0.workflow_id)
    result_v0 = await service_v0.get_result(accepted_v0.workflow_id)
    assert snapshot_v0.workflow_id == accepted_v0.workflow_id
    assert snapshot_v0.run_id == accepted_v0.run_id
    assert result_v0.workflow == snapshot_v0
    assert snapshot_v0.status == "running"
    assert result_v0.workflow.status == "running"
    assert result_v0.result is None
    assert RunFormatVersion.LEGACY_V1 is classify_readonly(
        state_v0,
        accepted_v0.run_id,
        (state_v0 / accepted_v0.run_id / "manifest.json").read_bytes(),
        expected_workflow_id=accepted_v0.workflow_id,
        expected_request_digest=digest_v0,
    )
    with closing(sqlite3.connect(database_v0)) as connection:
        assert (
            connection.execute(
                "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
                (accepted_v0.run_id,),
            ).fetchone()[0]
            == digest_v0
        )

    state_v1 = tmp_path / "legacy-v1-state"
    workspace_v1 = tmp_path / "workspace-v1"
    workspace_v1.mkdir()
    service_v1, _, accepted_v1 = await _start_legacy_run(state_v1, workspace_v1, "v1")
    database_v1 = state_v1 / "requests.sqlite"
    with closing(sqlite3.connect(database_v1)) as connection:
        digest_v1 = connection.execute(
            "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
            (accepted_v1.run_id,),
        ).fetchone()[0]
    StrictWorkflowRequestStore(state_v1).migrate_v0_to_v1(
        backup_path=state_v1 / "requests-before-v1.sqlite"
    )
    row_v1 = _RequestRow(
        f"i5b1-legacy-v1-{accepted_v1.run_id[-4:]}",
        accepted_v1.workflow_id,
        accepted_v1.run_id,
        digest_v1,
        "running",
        accepted_v1.created_at,
        accepted_v1.updated_at,
    )
    assert (
        _verified_active_workspace(state_v1, row_v1, (str(workspace_v1),)) == workspace_v1.resolve()
    )
    snapshot_v1 = await service_v1.get_workflow(accepted_v1.workflow_id)
    result_v1 = await service_v1.get_result(accepted_v1.workflow_id)
    assert snapshot_v1.workflow_id == accepted_v1.workflow_id
    assert snapshot_v1.run_id == accepted_v1.run_id
    assert result_v1.workflow == snapshot_v1
    assert snapshot_v1.status == "running"
    assert result_v1.workflow.status == "running"
    assert result_v1.result is None
    with closing(sqlite3.connect(database_v1)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
                (accepted_v1.run_id,),
            ).fetchone()[0]
            == digest_v1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workflow_request_contract_bindings"
            ).fetchone()[0]
            == 0
        )
    assert RunFormatVersion.LEGACY_V1 is classify_readonly(
        state_v1,
        accepted_v1.run_id,
        (state_v1 / accepted_v1.run_id / "manifest.json").read_bytes(),
        expected_workflow_id=accepted_v1.workflow_id,
        expected_request_digest=digest_v1,
    )


@pytest.mark.asyncio
async def test_strict_v2_service_status_and_verified_active_workspace(tmp_path: Path) -> None:
    fixture = _strict_fixture(tmp_path)
    database = fixture.state_root / "requests.sqlite"
    main_bytes = database.read_bytes()
    counts = _readonly_counts(database)
    snapshot_bytes = {
        name: (fixture.run_directory / name).read_bytes()
        for name in ("manifest.json", "workflow.yaml", "input.json")
    }
    manifest_bytes = snapshot_bytes["manifest.json"]

    assert RunFormatVersion.STRICT_V2 is classify_readonly(
        fixture.state_root,
        _RUN_ID,
        manifest_bytes,
        expected_workflow_id=_WORKFLOW_ID,
        expected_request_digest=fixture.request_digest,
    )
    row = _strict_row(fixture)
    inspection = _inspect_run(fixture.state_root, row)
    assert inspection.status == "needs_reconciliation"
    assert inspection.worker_state == "unknown"
    assert inspection.job_id is None
    assert inspection.result is None
    assert (
        _verified_active_workspace(fixture.state_root, row, (str(fixture.workspace),))
        == fixture.workspace.resolve()
    )

    manager = _WorkerManager(fixture.state_root, fixture.workspace)
    service = WorkflowService(manager, codex_bridge_port=18001)
    snapshot = await service.get_workflow(_WORKFLOW_ID)
    result = await service.get_result(_WORKFLOW_ID)
    assert snapshot.run_id == _RUN_ID
    assert snapshot.status == "needs_reconciliation"
    assert snapshot.worker_state == "unknown"
    assert result.workflow.status == "needs_reconciliation"
    assert result.result is None
    assert database.read_bytes() == main_bytes
    assert _readonly_counts(database) == counts
    assert {
        name: (fixture.run_directory / name).read_bytes() for name in snapshot_bytes
    } == snapshot_bytes

    with pytest.raises(WorkflowConcurrencyConflict):
        await service.start_workflow(
            client_request_id="i5b1-active-probe-0001",
            workflow_yaml=_LEGACY_YAML,
            input_data={"answer": 9},
            workspace_root=str(fixture.workspace),
        )
    assert manager.start_calls == []


def test_inconsistent_strict_snapshot_keeps_workspace_conflict(tmp_path: Path) -> None:
    fixture = _strict_fixture(tmp_path)
    with sqlite3.connect(fixture.run_directory / "variables.sqlite") as connection:
        connection.execute(
            "UPDATE run_binding SET normalized_input_sha256 = ? WHERE run_id = ?",
            ("f" * 64, _RUN_ID),
        )

    with pytest.raises(WorkflowConcurrencyConflict):
        _verified_active_workspace(
            fixture.state_root,
            _strict_row(fixture),
            (str(fixture.workspace),),
        )


@pytest.mark.parametrize(
    "version_mode",
    ["missing", "one", "other", "boolean", "null", "string"],
)
def test_strict_binding_never_falls_back_for_bad_manifest_version(
    tmp_path: Path, version_mode: str
) -> None:
    fixture = _strict_fixture(tmp_path)
    manifest = fixture.manifest.to_dict()
    if version_mode == "missing":
        manifest.pop("run_format_version")
    elif version_mode == "one":
        manifest["run_format_version"] = 1
    elif version_mode == "other":
        manifest["run_format_version"] = 7
    elif version_mode == "boolean":
        manifest["run_format_version"] = True
    elif version_mode == "null":
        manifest["run_format_version"] = None
    else:
        manifest["run_format_version"] = "2"

    with pytest.raises(StrictReaderGateError) as error:
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            _encode_manifest(manifest),
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=fixture.request_digest,
        )
    assert str(error.value) == "NEEDS_RECONCILIATION"
    assert error.value.reason_code == "NEEDS_RECONCILIATION"
    (fixture.run_directory / "manifest.json").write_bytes(_encode_manifest(manifest))
    inspection = _inspect_run(fixture.state_root, _strict_row(fixture))
    assert inspection.status == "needs_reconciliation"
    assert inspection.result is None


@pytest.mark.parametrize("bad_payload", [b"{", b"[]", b'{"x":1,"x":2}'])
def test_malformed_non_object_and_duplicate_key_manifest_fail_closed(
    tmp_path: Path, bad_payload: bytes
) -> None:
    fixture = _strict_fixture(tmp_path)
    with pytest.raises(StrictReaderGateError) as error:
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            bad_payload,
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=fixture.request_digest,
        )
    assert str(error.value) == "NEEDS_RECONCILIATION"
    (fixture.run_directory / "manifest.json").write_bytes(bad_payload)
    inspection = _inspect_run(fixture.state_root, _strict_row(fixture))
    assert inspection.status == "needs_reconciliation"
    assert inspection.result is None


def test_version_two_without_binding_and_v1_corruption_fail_closed(tmp_path: Path) -> None:
    fixture = _strict_fixture(tmp_path)
    database = fixture.state_root / "requests.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "DELETE FROM workflow_request_contract_bindings WHERE run_id = ?", (_RUN_ID,)
        )
        connection.commit()
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            serialize_strict_manifest_v2(fixture.manifest),
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=fixture.request_digest,
        )
    missing_binding_inspection = _inspect_run(fixture.state_root, _strict_row(fixture))
    assert missing_binding_inspection.status == "needs_reconciliation"
    assert missing_binding_inspection.result is None

    corrupt = _strict_fixture(tmp_path / "corrupt")
    with closing(sqlite3.connect(corrupt.state_root / "requests.sqlite")) as connection:
        connection.execute(
            "UPDATE workflow_requests SET request_digest = ? WHERE run_id = ?",
            ("x" * 64, _RUN_ID),
        )
        connection.commit()
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            corrupt.state_root,
            _RUN_ID,
            serialize_strict_manifest_v2(corrupt.manifest),
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=corrupt.request_digest,
        )
    corrupt_inspection = _inspect_run(corrupt.state_root, _strict_row(corrupt))
    assert corrupt_inspection.status == "needs_reconciliation"
    assert corrupt_inspection.result is None


@pytest.mark.parametrize("corruption", ["unknown_version", "missing_binding_table", "bad_revision"])
def test_unknown_or_inconsistent_v1_database_fails_closed(tmp_path: Path, corruption: str) -> None:
    fixture = _strict_fixture(tmp_path)
    database = fixture.state_root / "requests.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        if corruption == "unknown_version":
            connection.execute("PRAGMA user_version=9")
        elif corruption == "missing_binding_table":
            connection.execute("DROP TABLE workflow_request_contract_bindings")
        else:
            connection.execute(
                "UPDATE workflow_request_contract_bindings "
                "SET definition_revision = 99 WHERE run_id = ?",
                (_RUN_ID,),
            )
        connection.commit()
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            serialize_strict_manifest_v2(fixture.manifest),
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=fixture.request_digest,
        )
    inspection = _inspect_run(fixture.state_root, _strict_row(fixture))
    assert inspection.status == "needs_reconciliation"
    assert inspection.result is None


@pytest.mark.asyncio
async def test_v0_illicit_binding_and_missing_database_fail_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "legacy-state"
    workspace = tmp_path / "legacy-workspace"
    workspace.mkdir()
    _, _, accepted = await _start_legacy_run(state_root, workspace, "illicit")
    manifest = (state_root / accepted.run_id / "manifest.json").read_bytes()
    with closing(sqlite3.connect(state_root / "requests.sqlite")) as connection:
        request_digest = connection.execute(
            "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
            (accepted.run_id,),
        ).fetchone()[0]
    with closing(sqlite3.connect(state_root / "requests.sqlite")) as connection:
        connection.execute("CREATE TABLE workflow_request_contract_bindings (run_id TEXT)")
        connection.commit()
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            state_root,
            accepted.run_id,
            manifest,
            expected_workflow_id=accepted.workflow_id,
            expected_request_digest=request_digest,
        )
    row = _RequestRow(
        "i5b1-legacy-illicit-001",
        accepted.workflow_id,
        accepted.run_id,
        request_digest,
        "running",
        accepted.created_at,
        accepted.updated_at,
    )
    inspection = _inspect_run(state_root, row)
    assert inspection.status == "needs_reconciliation"
    assert inspection.result is None

    missing_root = tmp_path / "missing-state"
    missing_root.mkdir()
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            missing_root,
            _RUN_ID,
            b"{}",
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest="a" * 64,
        )
    assert not (missing_root / "requests.sqlite").exists()


def test_strict_identity_mismatch_and_error_redaction(tmp_path: Path) -> None:
    fixture = _strict_fixture(tmp_path)
    with pytest.raises(StrictReaderGateError) as error:
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            serialize_strict_manifest_v2(fixture.manifest),
            expected_workflow_id="33333333-3333-4333-8333-333333333333",
            expected_request_digest=fixture.request_digest,
        )
    assert str(error.value) == "NEEDS_RECONCILIATION"
    assert "strict reader gate fixture" not in str(error.value)

    bad_manifest = fixture.manifest.to_dict()
    bad_manifest["run_format_version"] = "private-input-sentinel"
    bad_manifest["prompt"] = "private-prompt-sentinel"
    with pytest.raises(StrictReaderGateError) as redacted:
        classify_readonly(
            fixture.state_root,
            _RUN_ID,
            _encode_manifest(bad_manifest),
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=fixture.request_digest,
        )
    assert str(redacted.value) == "NEEDS_RECONCILIATION"
    assert "private-input-sentinel" not in str(redacted.value)
    assert "private-prompt-sentinel" not in str(redacted.value)
