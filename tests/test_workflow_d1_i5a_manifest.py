from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import codex_bridge.workflows.strict_manifest as manifest_module
from codex_bridge.workflows.contracts import DRAFT_2020_12, canonical_json, sha256_json
from codex_bridge.workflows.strict_binding import (
    StrictWorkflowBindingVerifier,
    StrictWorkflowInputNormalization,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowManifestV2,
    StrictWorkflowSnapshotError,
    StrictWorkflowSnapshotVerifier,
    StrictWorkflowSnapshotWriter,
    parse_strict_manifest_v2,
    serialize_strict_manifest_v2,
    strict_request_digest_v2,
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

_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
_RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CREATED_AT = datetime(2026, 11, 19, 3, 4, 5, 123456, tzinfo=UTC)
_VALID_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5a-manifest\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - first:\n"
    "      call: codex-job\n"
    "      with:\n"
    '        prompt: "private-prompt-sentinel"\n'
)
_SECRET_SENTINEL = "private-input-sentinel"


def _schema(value_type: str = "object", **extra: object) -> dict[str, object]:
    return {
        "$schema": DRAFT_2020_12,
        "type": value_type,
        "additionalProperties": False,
        **extra,
    }


def _contract(
    *,
    variables: bool = True,
    required_input: tuple[str, ...] = (),
    nullable_mode: bool = True,
) -> dict[str, object]:
    variable_declarations: list[dict[str, object]] = []
    if variables:
        variable_declarations.append(
            {
                "declaration_id": "counter",
                "name": "counter",
                "owner_scope": "workflow",
                "value_schema": _schema("integer"),
                "sensitivity": "public",
                "initial": 0,
            }
        )
    input_schema = _schema(
        "object",
        properties={
            "mode": {
                "type": ["string", "null"] if nullable_mode else "string",
                "default": "safe",
            },
            "secret": {"type": "string"},
        },
    )
    if required_input:
        input_schema["required"] = list(required_input)
    return {
        "contract_version": 1,
        "input_schema": input_schema,
        "output_schema": _schema(),
        "variables": variable_declarations,
    }


@dataclass(frozen=True)
class _Fixture:
    state_root: Path
    workspace: Path
    run_directory: Path
    definition: Any
    normalized_input: StrictWorkflowInputNormalization
    binding: StrictWorkflowRunBindingInput
    writer: StrictWorkflowSnapshotWriter
    verifier: StrictWorkflowSnapshotVerifier


def _base_fixture(
    tmp_path: Path,
    *,
    raw_input: object | None = None,
    source_yaml: str = _VALID_YAML,
    variables: bool = True,
    required_input: tuple[str, ...] = (),
    nullable_mode: bool = True,
) -> _Fixture:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    state_root.mkdir(parents=True)
    workspace.mkdir()

    # I3-B initializes requests first so I3-A can add definitions.sqlite beside it.
    requests = StrictWorkflowRequestStore(state_root)
    requests.initialize_new_database()
    definitions = StrictWorkflowDefinitionStore(state_root)
    definitions.initialize_new_database()
    definition = definitions.create(
        mutation_request_id="i5a-definition-create-0001",
        source_yaml=source_yaml,
        max_live_codex_jobs=1,
        contract=_contract(
            variables=variables,
            required_input=required_input,
            nullable_mode=nullable_mode,
        ),
    )
    normalization = StrictWorkflowBindingVerifier(state_root).normalize_for_definition(
        definition.definition_id,
        definition.revision,
        {} if raw_input is None else raw_input,
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
        normalized_input_sha256=normalization.normalized_input_sha256,
        created_at=_CREATED_AT,
    )
    StrictWorkflowVariableStore(run_directory).initialize_new_database(binding)
    allowed_roots = (str(workspace),)
    writer = StrictWorkflowSnapshotWriter(state_root, allowed_roots)
    verifier = StrictWorkflowSnapshotVerifier(state_root, allowed_roots)
    return _Fixture(
        state_root,
        workspace,
        run_directory,
        definition,
        normalization,
        binding,
        writer,
        verifier,
    )


def _prepare(fixture: _Fixture) -> StrictWorkflowManifestV2:
    return fixture.writer.prepare_snapshot(
        workflow_id=_WORKFLOW_ID,
        run_id=_RUN_ID,
        definition_id=fixture.definition.definition_id,
        definition_revision=fixture.definition.revision,
        workspace_root=str(fixture.workspace.resolve()),
        normalized_input=fixture.normalized_input,
        created_at=_CREATED_AT,
    )


def _reserve(
    fixture: _Fixture,
    manifest: StrictWorkflowManifestV2,
    *,
    request_digest: str | None = None,
) -> None:
    request = StrictWorkflowRequestInput(
        client_request_id="i5a-request-create-0001",
        workflow_id=manifest.workflow_id,
        run_id=manifest.run_id,
        request_digest=manifest.request_digest if request_digest is None else request_digest,
        definition_id=manifest.definition_id,
        definition_revision=manifest.definition_revision,
        source_sha256=manifest.source_sha256,
        contract_sha256=manifest.contract_sha256,
        binding_digest=manifest.binding_digest,
        normalized_input_sha256=manifest.normalized_input_sha256,
        created_at=_CREATED_AT.isoformat(),
    )
    StrictWorkflowRequestStore(fixture.state_root).reserve_strict(request)


def _digest_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "workflow": {"document": {"name": "test"}},
        "workspace_root": "C:\\workspace",
        "max_live_codex_jobs": 2,
        "definition_id": "11111111-1111-4111-8111-111111111111",
        "definition_revision": 3,
        "source_sha256": "a" * 64,
        "contract_version": 1,
        "contract_sha256": "b" * 64,
        "binding_digest": "c" * 64,
        "normalized_input_sha256": "d" * 64,
    }
    fields.update(overrides)
    return fields


def _make_symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        if target_is_directory:
            link.symlink_to(target, target_is_directory=True)
        else:
            link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Windows symlink privilege unavailable: {type(exc).__name__}")


def test_request_digest_v2_is_deterministic_domain_separated_and_uses_normalized_hash_only() -> (
    None
):
    first = strict_request_digest_v2(**_digest_fields())
    second = strict_request_digest_v2(**_digest_fields())
    assert first == second
    assert first != sha256_json({"workflow": {"document": {"name": "test"}}})

    changed = strict_request_digest_v2(**_digest_fields(max_live_codex_jobs=3))
    assert changed != first


def test_i3_normalization_gives_same_digest_for_missing_or_explicit_default_and_keeps_null_distinct(
    tmp_path: Path,
) -> None:
    fixture = _base_fixture(tmp_path)
    verifier = StrictWorkflowBindingVerifier(fixture.state_root)
    missing = verifier.normalize_for_definition(
        fixture.definition.definition_id,
        fixture.definition.revision,
        {},
    )
    explicit_default = verifier.normalize_for_definition(
        fixture.definition.definition_id,
        fixture.definition.revision,
        {"mode": "safe"},
    )
    explicit_null = verifier.normalize_for_definition(
        fixture.definition.definition_id,
        fixture.definition.revision,
        {"mode": None},
    )
    assert missing.normalized_input == explicit_default.normalized_input
    assert missing.normalized_input_sha256 == explicit_default.normalized_input_sha256
    assert missing.normalized_input_sha256 != explicit_null.normalized_input_sha256


def test_writer_accepts_i3_normalized_nullable_null_input(tmp_path: Path) -> None:
    fixture = _base_fixture(
        tmp_path,
        raw_input={"mode": None, "secret": _SECRET_SENTINEL},
    )

    manifest = _prepare(fixture)

    assert fixture.verifier.verify_pre_reservation(_RUN_ID).manifest == manifest


@pytest.mark.parametrize(
    ("required_input", "nullable_mode", "valid_input", "forged_input"),
    [
        (
            (),
            True,
            {"secret": _SECRET_SENTINEL},
            {"mode": 13, "secret": _SECRET_SENTINEL},
        ),
        ((), True, {"secret": _SECRET_SENTINEL}, {"secret": _SECRET_SENTINEL}),
        (
            ("secret",),
            True,
            {"secret": _SECRET_SENTINEL},
            {"mode": _SECRET_SENTINEL},
        ),
        (
            (),
            False,
            {"secret": _SECRET_SENTINEL},
            {"mode": None, "secret": _SECRET_SENTINEL},
        ),
        (
            (),
            True,
            {"secret": _SECRET_SENTINEL},
            {"mode": "safe", "secret": _SECRET_SENTINEL, "extra": "x"},
        ),
    ],
    ids=(
        "schema-invalid",
        "default-missing",
        "required-missing",
        "null-disallowed",
        "additional-property",
    ),
)
def test_writer_rejects_forged_invalid_normalization_before_creating_run_directory(
    tmp_path: Path,
    required_input: tuple[str, ...],
    nullable_mode: bool,
    valid_input: dict[str, object],
    forged_input: dict[str, object],
) -> None:
    fixture = _base_fixture(
        tmp_path,
        raw_input=valid_input,
        required_input=required_input,
        nullable_mode=nullable_mode,
    )
    run_id = "run-ffffffffffffffffffffffffffffffff"
    forged = StrictWorkflowInputNormalization(
        definition_id=fixture.definition.definition_id,
        definition_revision=fixture.definition.revision,
        normalized_input=forged_input,
        normalized_input_sha256=sha256_json(forged_input),
    )

    with pytest.raises(StrictWorkflowSnapshotError) as caught:
        fixture.writer.prepare_snapshot(
            workflow_id=_WORKFLOW_ID,
            run_id=run_id,
            definition_id=fixture.definition.definition_id,
            definition_revision=fixture.definition.revision,
            workspace_root=str(fixture.workspace.resolve()),
            normalized_input=forged,
            created_at=_CREATED_AT,
        )

    assert _SECRET_SENTINEL not in str(caught.value)
    assert _SECRET_SENTINEL not in repr(caught.value)
    run_directory = fixture.state_root / run_id
    assert not run_directory.exists()
    assert not (run_directory / "workflow.yaml").exists()
    assert not (run_directory / "input.json").exists()
    assert not (run_directory / "manifest.json").exists()


def test_manifest_codec_is_canonical_allowlisted_and_value_free(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path, raw_input={"secret": _SECRET_SENTINEL})
    manifest = _prepare(fixture)
    encoded = serialize_strict_manifest_v2(manifest)
    assert (fixture.run_directory / "manifest.json").read_bytes() == encoded
    assert parse_strict_manifest_v2(encoded).to_dict() == manifest.to_dict()
    assert "control" not in manifest.to_dict()
    assert _SECRET_SENTINEL not in encoded.decode("utf-8")
    assert "private-prompt-sentinel" not in encoded.decode("utf-8")
    assert _SECRET_SENTINEL not in repr(manifest)
    assert _SECRET_SENTINEL not in repr(fixture.verifier.verify_pre_reservation(_RUN_ID))


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "unknown"},
        {"run_format_version": 1},
        {"run_format_version": 3},
        {"run_format_version": True},
        {"run_format_version": None},
        {"contract_sha256": "A" * 64},
        {"max_live_codex_jobs": True},
        {"definition_revision": False},
    ],
)
def test_manifest_codec_rejects_unknown_or_invalid_fields(
    tmp_path: Path,
    change: dict[str, object],
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    value = manifest.to_dict()
    value.update(change)
    with pytest.raises(StrictWorkflowSnapshotError):
        parse_strict_manifest_v2(canonical_json(value).encode("utf-8"))


def test_manifest_codec_rejects_duplicate_keys_nonfinite_and_noncanonical_json(
    tmp_path: Path,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    encoded = serialize_strict_manifest_v2(manifest)
    with pytest.raises(StrictWorkflowSnapshotError):
        parse_strict_manifest_v2(b'{"run_format_version":2,"run_format_version":2}')
    with pytest.raises(StrictWorkflowSnapshotError):
        parse_strict_manifest_v2(b'{"run_format_version":NaN}')
    with pytest.raises(StrictWorkflowSnapshotError):
        parse_strict_manifest_v2(encoded + b"\n")
    missing_field = parse_strict_manifest_v2(encoded).to_dict()
    missing_field.pop("contract_version")
    with pytest.raises(StrictWorkflowSnapshotError):
        parse_strict_manifest_v2(canonical_json(missing_field).encode("utf-8"))


def test_pre_reservation_snapshot_verifies_pinned_revision_and_initial_variable_db(
    tmp_path: Path,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    result = fixture.verifier.verify_pre_reservation(_RUN_ID)
    assert result.manifest == manifest
    assert result.reservation_verified is False
    assert result.mutation_count == 0


def test_reserved_snapshot_verifies_cross_database_binding_and_nonempty_ledger(
    tmp_path: Path,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    mutation_store = StrictWorkflowVariableMutationStore(
        fixture.run_directory,
        fixture.binding,
        fixture.definition.contract,
    )
    initialized = mutation_store.apply(
        MutationContext(
            node_path="/do/1/initialize",
            iteration_vector=(),
            frame_id="workflow-root",
            mutation_ordinal=0,
        ),
        "counter",
        "initialize",
        expected_before_version=0,
        expected_before_digest=None,
    )
    mutation_store.apply(
        MutationContext(
            node_path="/do/1/increment",
            iteration_vector=(),
            frame_id="workflow-root",
            mutation_ordinal=0,
        ),
        "counter",
        "increment",
        expected_before_version=initialized.after_version,
        expected_before_digest=initialized.after_digest,
        by=1,
    )

    result = fixture.verifier.verify_reserved(_RUN_ID)
    assert result.reservation_verified is True
    assert result.mutation_count == 2  # initialize and increment are both ledgered.


@pytest.mark.parametrize("close_child", [False, True], ids=["active", "closed"])
def test_reserved_snapshot_accepts_child_frames_without_mutations(
    tmp_path: Path,
    close_child: bool,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    mutation_store = StrictWorkflowVariableMutationStore(
        fixture.run_directory,
        fixture.binding,
        fixture.definition.contract,
    )
    child = mutation_store.enter_frame("workflow-root", "/do/1", [], _CREATED_AT)
    if close_child:
        child = mutation_store.close_frame(child.frame_id, _CREATED_AT)

    result = fixture.verifier.verify_reserved(_RUN_ID)

    assert result.reservation_verified is True
    assert result.mutation_count == 0
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


@pytest.mark.parametrize("tamper", ["identity", "path", "foreign_key"])
def test_reserved_snapshot_rejects_tampered_child_frame_without_mutations(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    mutation_store = StrictWorkflowVariableMutationStore(
        fixture.run_directory,
        fixture.binding,
        fixture.definition.contract,
    )
    child = mutation_store.enter_frame("workflow-root", "/do/1", [], _CREATED_AT)
    with sqlite3.connect(fixture.run_directory / "variables.sqlite") as connection:
        if tamper == "identity":
            connection.execute(
                "UPDATE scope_frames SET frame_id=? WHERE frame_id=?",
                ("frame-" + "f" * 64, child.frame_id),
            )
        elif tamper == "path":
            connection.execute(
                "UPDATE scope_frames SET scope_path='/do/2' WHERE frame_id=?",
                (child.frame_id,),
            )
        else:
            connection.execute(
                "UPDATE scope_frames SET parent_frame_id=? WHERE frame_id=?",
                ("frame-" + "e" * 64, child.frame_id),
            )

    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_reserved(_RUN_ID)


def test_reserved_snapshot_rejects_state_without_any_mutations(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    with sqlite3.connect(fixture.run_directory / "variables.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO variable_states (
                run_id, frame_id, declaration_id, name, present,
                value_json, version, value_digest, updated_at
            ) VALUES (?, 'workflow-root', 'counter', 'counter', 1, ?, 1, ?, ?)
            """,
            (
                _RUN_ID,
                canonical_json(0),
                sha256_json(0),
                _CREATED_AT.isoformat().replace("+00:00", "Z"),
            ),
        )

    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_reserved(_RUN_ID)


def test_reserved_snapshot_without_request_binding_is_reconciliation_error(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    with pytest.raises(StrictWorkflowSnapshotError, match="needs reconciliation") as caught:
        fixture.verifier.verify_reserved(_RUN_ID)
    assert caught.value.reason_code == "NEEDS_RECONCILIATION"


def test_reserved_snapshot_rejects_legacy_request_row_without_binding(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    with sqlite3.connect(fixture.state_root / "requests.sqlite") as connection:
        connection.execute(
            """
            INSERT INTO workflow_requests (
                client_request_id, workflow_id, run_id, request_digest, state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                "legacy-request-without-binding",
                manifest.workflow_id,
                manifest.run_id,
                manifest.request_digest,
                _CREATED_AT.isoformat(),
                _CREATED_AT.isoformat(),
            ),
        )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_reserved(_RUN_ID)


def test_reserved_snapshot_rejects_request_digest_mismatch_and_v1_manifest(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest, request_digest="e" * 64)
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_reserved(_RUN_ID)

    fixture = _base_fixture(tmp_path / "other")
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    legacy_value = manifest.to_dict()
    legacy_value["run_format_version"] = 1
    (fixture.run_directory / "manifest.json").write_bytes(
        canonical_json(legacy_value).encode("utf-8")
    )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_reserved(_RUN_ID)


def test_verification_is_read_only_for_snapshot_and_database_files(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    paths = [
        fixture.run_directory / "workflow.yaml",
        fixture.run_directory / "input.json",
        fixture.run_directory / "manifest.json",
        fixture.run_directory / "variables.sqlite",
        fixture.state_root / "requests.sqlite",
        fixture.state_root / "definitions.sqlite",
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    fixture.verifier.verify_pre_reservation(_RUN_ID)
    fixture.verifier.verify_reserved(_RUN_ID)
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    assert after == before


def test_partial_snapshot_is_retained_and_retry_conflicts_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    original = manifest_module._write_exclusive

    def fail_on_input(path: Path, payload: bytes) -> None:
        if path.name == "input.json":
            raise StrictWorkflowSnapshotError()
        original(path, payload)

    monkeypatch.setattr(manifest_module, "_write_exclusive", fail_on_input)
    with pytest.raises(StrictWorkflowSnapshotError):
        _prepare(fixture)
    workflow_path = fixture.run_directory / "workflow.yaml"
    assert workflow_path.is_file()
    first_bytes = workflow_path.read_bytes()
    monkeypatch.setattr(manifest_module, "_write_exclusive", original)
    with pytest.raises(StrictWorkflowSnapshotError):
        _prepare(fixture)
    assert workflow_path.read_bytes() == first_bytes
    assert not (fixture.run_directory / "input.json").exists()
    assert not (fixture.run_directory / "manifest.json").exists()


def test_existing_snapshot_and_user_file_are_never_overwritten_or_deleted(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    note = fixture.run_directory / "user-note.txt"
    note.write_text("preserve", encoding="utf-8")
    manifest = _prepare(fixture)
    snapshot_before = {
        name: (fixture.run_directory / name).read_bytes()
        for name in ("workflow.yaml", "input.json", "manifest.json")
    }
    with pytest.raises(StrictWorkflowSnapshotError):
        _prepare(fixture)
    assert note.read_text(encoding="utf-8") == "preserve"
    assert snapshot_before == {
        name: (fixture.run_directory / name).read_bytes()
        for name in ("workflow.yaml", "input.json", "manifest.json")
    }
    assert manifest.request_digest


@pytest.mark.parametrize("name", ["workflow.yaml", "input.json", "manifest.json"])
def test_tampered_snapshot_bytes_fail_closed(tmp_path: Path, name: str) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    path = fixture.run_directory / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


def test_workflow_duplicate_yaml_keys_and_non_json_yaml_values_are_rejected(tmp_path: Path) -> None:
    duplicate_yaml = _VALID_YAML + "\ndocument:\n  dsl: 'duplicate'\n"
    with pytest.raises(StrictWorkflowSnapshotError):
        manifest_module._parse_workflow_source(duplicate_yaml.encode("utf-8"))
    with pytest.raises(StrictWorkflowSnapshotError):
        manifest_module._parse_workflow_source(
            (_VALID_YAML + "\nnon_finite: .nan\n").encode("utf-8")
        )


@pytest.mark.parametrize("db_state", ["missing", "corrupt", "unknown_schema"])
def test_invalid_variable_database_fails_closed(tmp_path: Path, db_state: str) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    database = fixture.run_directory / "variables.sqlite"
    if db_state == "missing":
        database.unlink()
    elif db_state == "corrupt":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA user_version=99")
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


def test_variable_run_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    with sqlite3.connect(fixture.run_directory / "variables.sqlite") as connection:
        connection.execute(
            "UPDATE run_binding SET normalized_input_sha256=? WHERE run_id=?",
            ("f" * 64, _RUN_ID),
        )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


@pytest.mark.parametrize("tamper", ["source_yaml", "contract_sha256"])
def test_pinned_definition_and_contract_tampering_fail_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    with sqlite3.connect(fixture.state_root / "definitions.sqlite") as connection:
        if tamper == "source_yaml":
            connection.execute(
                """
                UPDATE workflow_definition_revisions
                SET source_yaml=source_yaml || ?
                WHERE definition_id=? AND revision=?
                """,
                ("# changed", fixture.definition.definition_id, fixture.definition.revision),
            )
        else:
            connection.execute(
                """
                UPDATE workflow_definition_contracts
                SET contract_sha256=?
                WHERE definition_id=? AND revision=?
                """,
                ("e" * 64, fixture.definition.definition_id, fixture.definition.revision),
            )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


def test_run_id_traversal_workspace_escape_and_state_overlap_are_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.writer.prepare_snapshot(
            workflow_id=_WORKFLOW_ID,
            run_id="../escape",
            definition_id=fixture.definition.definition_id,
            definition_revision=fixture.definition.revision,
            workspace_root=str(fixture.workspace.resolve()),
            normalized_input=fixture.normalized_input,
            created_at=_CREATED_AT,
        )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.writer.prepare_snapshot(
            workflow_id=_WORKFLOW_ID,
            run_id="run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            definition_id=fixture.definition.definition_id,
            definition_revision=fixture.definition.revision,
            workspace_root=str(tmp_path.resolve()),
            normalized_input=fixture.normalized_input,
            created_at=_CREATED_AT,
        )
    overlapping_writer = StrictWorkflowSnapshotWriter(
        fixture.state_root,
        (str(fixture.state_root.parent),),
    )
    with pytest.raises(StrictWorkflowSnapshotError):
        overlapping_writer.prepare_snapshot(
            workflow_id=_WORKFLOW_ID,
            run_id="run-cccccccccccccccccccccccccccccccc",
            definition_id=fixture.definition.definition_id,
            definition_revision=fixture.definition.revision,
            workspace_root=str(fixture.state_root.resolve()),
            normalized_input=fixture.normalized_input,
            created_at=_CREATED_AT,
        )


def test_run_directory_symlink_is_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    run_target = tmp_path / "outside-run"
    fixture.run_directory.rename(run_target)
    _make_symlink_or_skip(
        fixture.run_directory,
        run_target,
        target_is_directory=True,
    )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


def test_wal_sidecar_symlink_is_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)
    sidecar_target = tmp_path / "wal-target"
    sidecar_target.write_bytes(b"sidecar")
    sidecar = fixture.run_directory / "variables.sqlite-wal"
    if sidecar.exists():
        sidecar.unlink()
    _make_symlink_or_skip(sidecar, sidecar_target)
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)


def test_size_limits_reject_oversized_source_input_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    large_input = {"secret": "x" * (manifest_module._MAX_INPUT_BYTES + 1)}
    invalid_normalization = StrictWorkflowInputNormalization(
        definition_id=fixture.definition.definition_id,
        definition_revision=fixture.definition.revision,
        normalized_input=large_input,
        normalized_input_sha256=sha256_json(large_input),
    )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.writer.prepare_snapshot(
            workflow_id=_WORKFLOW_ID,
            run_id="run-dddddddddddddddddddddddddddddddd",
            definition_id=fixture.definition.definition_id,
            definition_revision=fixture.definition.revision,
            workspace_root=str(fixture.workspace.resolve()),
            normalized_input=invalid_normalization,
            created_at=_CREATED_AT,
        )

    monkeypatch.setattr(manifest_module, "_MAX_MANIFEST_BYTES", 16)
    with pytest.raises(StrictWorkflowSnapshotError):
        _prepare(fixture)


def test_workflow_source_size_limit_is_enforced_before_snapshot_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    monkeypatch.setattr(manifest_module, "_MAX_WORKFLOW_BYTES", 16)
    with pytest.raises(StrictWorkflowSnapshotError):
        _prepare(fixture)
    assert not (fixture.run_directory / "workflow.yaml").exists()

    monkeypatch.setattr(manifest_module, "_MAX_WORKFLOW_BYTES", 64 * 1024)
    _prepare(fixture)
    workflow = fixture.run_directory / "workflow.yaml"
    workflow.write_bytes(
        _VALID_YAML.encode("utf-8") + b"\n# " + b"x" * (manifest_module._MAX_WORKFLOW_BYTES + 1)
    )
    with pytest.raises(StrictWorkflowSnapshotError):
        fixture.verifier.verify_pre_reservation(_RUN_ID)
