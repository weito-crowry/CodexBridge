from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows import worker as worker_module
from codex_bridge.workflows import worker_entry
from codex_bridge.workflows.contracts import DRAFT_2020_12
from codex_bridge.workflows.interpreter import OwsError
from codex_bridge.workflows.service import _initialize_write_connection
from codex_bridge.workflows.strict_binding import StrictWorkflowBindingVerifier
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowSnapshotVerifier,
    StrictWorkflowSnapshotWriter,
)
from codex_bridge.workflows.strict_reader_gate import (
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
from codex_bridge.workflows.worker import canonical_json, sha256_json, sha256_text

_ROOT = Path(__file__).resolve().parents[1]
_POC_FIXTURE = _ROOT / "poc" / "ows" / "fixtures" / "workflow.yaml"
_RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
_REQUEST_DIGEST = "a" * 64
_SECRET = "worker-private-prompt-and-input-sentinel"
_CREATED_AT = datetime(2026, 11, 20, 3, 4, 5, 123456, tzinfo=UTC)
_STRICT_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5b3-worker-reader-gate\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - first:\n"
    "      call: codex-job\n"
    "      with:\n"
    f'        prompt: "{_SECRET}"\n'
)


def _make_legacy_run(
    tmp_path: Path,
    *,
    requests_version: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    state_root = tmp_path / "state"
    state_root.mkdir()
    if requests_version == 0:
        connection = sqlite3.connect(state_root / "requests.sqlite")
        try:
            _initialize_write_connection(connection)
            connection.commit()
        finally:
            connection.close()
    elif requests_version == 1:
        StrictWorkflowRequestStore(state_root).initialize_new_database()
    run_dir = state_root / _RUN_ID
    run_dir.mkdir()
    definition_text = _POC_FIXTURE.read_text(encoding="utf-8")
    input_data = {"sign": 1, "offset": 2, "values": [3, 5]}
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(canonical_json(input_data) + "\n", encoding="utf-8")
    manifest: dict[str, Any] = {
        "workflow_source": str(_POC_FIXTURE),
        "workflow_id": None,
        "definition_sha256": sha256_text(definition_text),
        "input_sha256": sha256_json(input_data),
        "max_tasks": 100,
        "control": {},
    }
    _write_manifest(run_dir, manifest)
    return run_dir, manifest


def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True),
        encoding="utf-8",
    )


def _make_worker_config(run_dir: Path, mode: str = "start") -> Path:
    config_path = run_dir / "worker-config-test.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "mode": mode,
                "workspace_root": None,
                "allowed_roots": [],
                "shutdown_file": f"worker-shutdown-{'b' * 32}.request",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _make_managed_request_database(
    state_root: Path,
    *,
    run_id: str,
    workflow_id: str,
    request_digest: str,
    version: int,
) -> Path:
    database = state_root / "requests.sqlite"
    if not database.is_file():
        raise AssertionError("request schema must be initialized before the run directory")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO workflow_requests (client_request_id, workflow_id, run_id, "
            "request_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "i5b3-legacy-request-0001",
                workflow_id,
                run_id,
                request_digest,
                "running",
                _CREATED_AT.isoformat(),
                _CREATED_AT.isoformat(),
            ),
        )
    return database


def _add_legacy_identity(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    workflow_id: str = _WORKFLOW_ID,
    request_digest: str = _REQUEST_DIGEST,
) -> dict[str, Any]:
    updated = {
        **manifest,
        "workflow_id": workflow_id,
        "run_id": run_dir.name,
        "request_digest": request_digest,
    }
    _write_manifest(run_dir, updated)
    return updated


def _make_strict_reserved_run(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    state_root = tmp_path / "state"
    state_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    requests = StrictWorkflowRequestStore(state_root)
    requests.initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    run_dir = state_root / _RUN_ID
    run_dir.mkdir()
    schema = {"$schema": DRAFT_2020_12, "type": "object", "additionalProperties": False}
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
        mutation_request_id="i5b3-definition-create-0001",
        source_yaml=_STRICT_YAML,
        max_live_codex_jobs=1,
        contract=contract,
    )
    normalized = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        definition.definition_id,
        definition.revision,
        {"secret": _SECRET},
    )
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
    manifest = writer.prepare_snapshot(
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
            client_request_id="i5b3-request-create-0001",
            workflow_id=manifest.workflow_id,
            run_id=manifest.run_id,
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
    assert (
        StrictWorkflowSnapshotVerifier(state_root, (str(workspace),))
        .verify_reserved(_RUN_ID)
        .reservation_verified
    )
    return run_dir, manifest.to_dict()


def _main_files(run_dir: Path) -> dict[str, bytes]:
    files = ("manifest.json", "workflow.yaml", "input.json")
    result = {name: (run_dir / name).read_bytes() for name in files if (run_dir / name).is_file()}
    request_database = run_dir.parent / "requests.sqlite"
    if request_database.is_file():
        result["requests.sqlite"] = request_database.read_bytes()
    return result


def _run_entry_rejection(
    run_dir: Path,
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, list[str], str]:
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
        lambda: pytest.fail("rejected manifest reached dependency checks"),
    )
    monkeypatch.setattr(
        worker_module,
        "main",
        lambda _args: pytest.fail("rejected manifest dispatched the worker"),
    )
    result = worker_entry.main(
        ["--mode", "start", "--run-dir", str(run_dir), "--config", str(config_path)]
    )
    return result, states, capsys.readouterr().err


def _guard_worker_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    class NeverDBOS:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("rejected manifest instantiated DBOS")

    monkeypatch.setattr(worker_module, "DBOS", NeverDBOS)
    monkeypatch.setattr(
        worker_module,
        "get_process_identity",
        lambda _pid: pytest.fail("rejected manifest wrote worker identity"),
    )
    monkeypatch.setattr(
        worker_module,
        "_write_json",
        lambda *_args, **_kwargs: pytest.fail("rejected manifest wrote a worker file"),
    )
    monkeypatch.setattr(
        worker_module,
        "_persist_workflow_id",
        lambda *_args: pytest.fail("rejected manifest rewrote workflow identity"),
    )
    monkeypatch.setattr(
        worker_module,
        "append_lifecycle_event",
        lambda *_args, **_kwargs: pytest.fail("rejected manifest wrote lifecycle output"),
    )
    monkeypatch.setattr(
        worker_module,
        "CodexHTTPClient",
        lambda *_args, **_kwargs: pytest.fail("rejected manifest created a job client"),
    )


def _assert_direct_main_rejected(
    run_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
) -> None:
    _guard_worker_side_effects(monkeypatch)
    with pytest.raises(OwsError) as error:
        worker_module.main(["--mode", "start", "--run-dir", str(run_dir)])
    assert str(error.value) == error_message


@pytest.mark.parametrize("surface", ["entry", "load", "main"])
@pytest.mark.parametrize("version", [2, 1, 3, True, None, _SECRET])
def test_every_worker_ingress_rejects_explicit_run_format_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    version: object,
) -> None:
    if type(version) is int and version == 2:
        run_dir, manifest = _make_strict_reserved_run(tmp_path)
    else:
        run_dir, manifest = _make_legacy_run(tmp_path)
    manifest["run_format_version"] = version
    _write_manifest(run_dir, manifest)
    config_path = _make_worker_config(run_dir)
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(run_dir, config_path, monkeypatch, capsys)
        assert result == 2
        assert states == ["failed"]
        assert "run_format_version" not in stderr
        assert "input" not in stderr.lower()
        assert "prompt" not in stderr.lower()
        assert _SECRET not in stderr
        status = json.loads((run_dir / "worker-entry-status.json").read_text("utf-8"))
        assert status["state"] == "failed"
        assert status["error_code"] == "invalid_config"
    else:
        _guard_worker_side_effects(monkeypatch)
        if surface == "load":
            with pytest.raises(OwsError) as error:
                worker_module.load_run_request(run_dir)
        else:
            with pytest.raises(OwsError) as error:
                worker_module.main(["--mode", "start", "--run-dir", str(run_dir)])
        assert str(error.value) == "workflow run format is unsupported"
        assert _SECRET not in str(error.value)
    assert _main_files(run_dir) == before
    assert not (run_dir / "start-worker.json").exists()
    assert not (run_dir / "start-worker-identity.json").exists()
    assert not (run_dir / "worker-lifecycle.jsonl").exists()
    assert not (run_dir / "result.json").exists()
    assert not (run_dir / "dbos.sqlite").exists()
    assert not (run_dir / "jobs.sqlite").exists()
    assert not (run_dir / "codex_jobs.sqlite").exists()
    assert not (run_dir / "task-events.jsonl").exists()


@pytest.mark.parametrize("version", [0, 1])
def test_managed_legacy_v0_and_v1_remain_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
) -> None:
    run_dir, manifest = _make_legacy_run(tmp_path, requests_version=version)
    managed = _add_legacy_identity(run_dir, manifest)
    database = _make_managed_request_database(
        run_dir.parent,
        run_id=run_dir.name,
        workflow_id=managed["workflow_id"],
        request_digest=managed["request_digest"],
        version=version,
    )

    loaded, definition_text, input_data = worker_module.load_run_request(run_dir)
    assert loaded["workflow_id"] == _WORKFLOW_ID
    assert definition_text
    assert input_data == {"sign": 1, "offset": 2, "values": [3, 5]}

    config_path = _make_worker_config(run_dir)
    calls: list[list[str]] = []
    monkeypatch.setattr(worker_entry, "missing_workflow_dependencies", lambda: ())
    monkeypatch.setattr(
        worker_module,
        "main",
        lambda args: calls.append(list(args)) or 0,
    )
    result = worker_entry.main(
        ["--mode", "start", "--run-dir", str(run_dir), "--config", str(config_path)]
    )
    status = json.loads((run_dir / "worker-entry-status.json").read_text("utf-8"))
    assert result == 0
    assert len(calls) == 1
    assert status["state"] == "completed"
    assert database.read_bytes()


def test_standalone_legacy_without_requests_database_keeps_optional_identity(
    tmp_path: Path,
) -> None:
    run_dir, manifest = _make_legacy_run(tmp_path)

    loaded, _definition_text, _input_data = worker_module.load_run_request(run_dir)

    assert loaded == manifest
    assert loaded["workflow_id"] is None
    assert not (run_dir.parent / "requests.sqlite").exists()


@pytest.mark.parametrize("surface", ["entry", "load", "main"])
@pytest.mark.parametrize(
    ("metadata_field", "invalid_value"),
    [
        ("workflow_id", "missing"),
        ("workflow_id", 123),
        ("run_id", "missing"),
        ("run_id", False),
        ("request_digest", "missing"),
        ("request_digest", None),
    ],
)
def test_managed_legacy_requires_typed_manifest_binding_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    metadata_field: str,
    invalid_value: object,
) -> None:
    run_dir, manifest = _make_legacy_run(tmp_path, requests_version=1)
    managed = _add_legacy_identity(run_dir, manifest)
    _make_managed_request_database(
        run_dir.parent,
        run_id=run_dir.name,
        workflow_id=managed["workflow_id"],
        request_digest=managed["request_digest"],
        version=1,
    )
    invalid = dict(managed)
    if invalid_value == "missing":
        invalid.pop(metadata_field)
    else:
        invalid[metadata_field] = invalid_value
    _write_manifest(run_dir, invalid)
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert "invalid" in stderr
        assert _SECRET not in stderr
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow request binding is invalid"):
            worker_module.load_run_request(run_dir)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow request binding is invalid")
    assert _main_files(run_dir) == before


@pytest.mark.parametrize("database_state", ["corrupt", "unknown-version"])
@pytest.mark.parametrize("surface", ["entry", "load", "main"])
def test_present_corrupt_or_unknown_request_database_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_state: str,
    surface: str,
) -> None:
    run_dir, manifest = _make_legacy_run(tmp_path)
    _add_legacy_identity(run_dir, manifest)
    database = run_dir.parent / "requests.sqlite"
    if database_state == "corrupt":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=99")
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert "binding" in stderr
        assert _SECRET not in stderr
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow request binding is invalid"):
            worker_module.load_run_request(run_dir)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow request binding is invalid")
    assert _main_files(run_dir) == before


@pytest.mark.parametrize("surface", ["entry", "load", "main"])
def test_strict_binding_with_unversioned_manifest_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    run_dir, manifest = _make_strict_reserved_run(tmp_path)
    legacy_shaped = dict(manifest)
    legacy_shaped.pop("run_format_version")
    _write_manifest(run_dir, legacy_shaped)
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            run_dir.parent,
            run_dir.name,
            (run_dir / "manifest.json").read_bytes(),
            expected_workflow_id=manifest["workflow_id"],
            expected_request_digest=manifest["request_digest"],
        )
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert "binding" in stderr
        assert _SECRET not in stderr
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow request binding is invalid"):
            worker_module.load_run_request(run_dir)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow request binding is invalid")
    assert _main_files(run_dir) == before


@pytest.mark.parametrize("surface", ["entry", "load", "main"])
def test_v2_without_request_binding_is_still_rejected_before_worker_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    run_dir, manifest = _make_strict_reserved_run(tmp_path)
    with sqlite3.connect(run_dir.parent / "requests.sqlite") as connection:
        connection.execute(
            "DELETE FROM workflow_request_contract_bindings WHERE run_id = ?",
            (_RUN_ID,),
        )
    with pytest.raises(StrictReaderGateError):
        classify_readonly(
            run_dir.parent,
            run_dir.name,
            (run_dir / "manifest.json").read_bytes(),
            expected_workflow_id=manifest["workflow_id"],
            expected_request_digest=manifest["request_digest"],
        )
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert "unsupported" in stderr
        assert _SECRET not in stderr
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow run format is unsupported"):
            worker_module.load_run_request(run_dir)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow run format is unsupported")
    assert _main_files(run_dir) == before


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        b'{"run_format_version":1,"run_format_version":2}',
        b'{"control":{"secret":NaN}}',
        b'{"control":{"secret":Infinity}}',
        b'{"control":{"secret":-Infinity}}',
        b"{not-json}",
    ],
)
@pytest.mark.parametrize("surface", ["entry", "load", "main"])
def test_duplicate_nonfinite_or_malformed_manifest_is_rejected_without_echo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest_bytes: bytes,
    surface: str,
) -> None:
    run_dir, _manifest = _make_legacy_run(tmp_path)
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    before = _main_files(run_dir)
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert _SECRET not in stderr
        assert "input" not in stderr.lower()
        assert "prompt" not in stderr.lower()
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow run manifest is invalid") as error:
            worker_module.load_run_request(run_dir)
        assert _SECRET not in str(error.value)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow run manifest is invalid")
    assert _main_files(run_dir) == before


@pytest.mark.parametrize("surface", ["entry", "load", "main"])
def test_worker_entry_rejects_manifest_directory_before_running_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    run_dir, _manifest = _make_legacy_run(tmp_path)
    (run_dir / "manifest.json").unlink()
    (run_dir / "manifest.json").mkdir()
    if surface == "entry":
        result, states, stderr = _run_entry_rejection(
            run_dir, _make_worker_config(run_dir), monkeypatch, capsys
        )
        assert result == 2
        assert states == ["failed"]
        assert "running" not in states
        assert "completed" not in states
        assert "manifest" in stderr
    elif surface == "load":
        with pytest.raises(OwsError, match="workflow run manifest is invalid"):
            worker_module.load_run_request(run_dir)
    else:
        _assert_direct_main_rejected(run_dir, monkeypatch, "workflow run manifest is invalid")


def test_worker_entry_rejects_manifest_symlink_when_windows_allows_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _manifest = _make_legacy_run(tmp_path)
    target = run_dir / "manifest-target.json"
    (run_dir / "manifest.json").replace(target)
    try:
        (run_dir / "manifest.json").symlink_to(target.name)
    except OSError as error:
        pytest.skip(f"Windows symlink creation unavailable: {type(error).__name__}")
    result, states, stderr = _run_entry_rejection(
        run_dir, _make_worker_config(run_dir), monkeypatch, capsys
    )
    assert result == 2
    assert states == ["failed"]
    assert "running" not in states
    assert "completed" not in states
    assert "manifest" in stderr
