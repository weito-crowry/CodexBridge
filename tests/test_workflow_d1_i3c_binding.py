from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows.contracts import normalize_input, sha256_json
from codex_bridge.workflows.definitions import DefinitionRepositoryUnavailable
from codex_bridge.workflows.strict_binding import (
    StrictWorkflowBindingError,
    StrictWorkflowBindingVerifier,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore
from codex_bridge.workflows.strict_requests import (
    StrictWorkflowRequestError,
    StrictWorkflowRequestInput,
    StrictWorkflowRequestStore,
)

_CREATED = "2026-11-19T00:00:00+00:00"
_WORKFLOW_ID = "22222222-2222-4222-8222-222222222222"
_RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_REQUEST_DIGEST = "a" * 64
_ERROR = "strict workflow binding verification failed"
_VALID_YAML = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: i3c-binding
  version: "1.0.0"
evaluate:
  language: jq
  mode: strict
do:
  - first:
      call: codex-job
      with:
        prompt: "private-prompt-sentinel"
"""


def _contract(default: str = "safe") -> dict[str, object]:
    return {
        "contract_version": 1,
        "input_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"mode": {"type": "string", "default": default}},
            "additionalProperties": False,
        },
        "output_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        "variables": [],
    }


def _stores(state_root: Path) -> tuple[StrictWorkflowRequestStore, StrictWorkflowDefinitionStore]:
    requests = StrictWorkflowRequestStore(state_root)
    definitions = StrictWorkflowDefinitionStore(state_root)
    # requests.sqlite initialization requires an empty root; definitions.sqlite
    # can then be created alongside it by its explicit v2 initializer.
    requests.initialize_new_database()
    definitions.initialize_new_database()
    return requests, definitions


def _reserve(
    requests: StrictWorkflowRequestStore,
    definition: Any,
    *,
    run_id: str = _RUN_ID,
    workflow_id: str = _WORKFLOW_ID,
    request_digest: str = _REQUEST_DIGEST,
    normalized_input: object | None = None,
) -> StrictWorkflowRequestInput:
    snapshot = {"mode": "safe"} if normalized_input is None else normalized_input
    request = StrictWorkflowRequestInput(
        client_request_id="i3c-request-0001",
        workflow_id=workflow_id,
        run_id=run_id,
        request_digest=request_digest,
        definition_id=definition.definition_id,
        definition_revision=definition.revision,
        source_sha256=definition.source_sha256,
        contract_sha256=definition.contract_sha256 or "c" * 64,
        binding_digest=definition.binding_digest or "d" * 64,
        normalized_input_sha256=sha256_json(snapshot),
        created_at=_CREATED,
    )
    requests.reserve_strict(request)
    return request


def _prepared(
    tmp_path: Path,
) -> tuple[
    Path,
    StrictWorkflowRequestStore,
    StrictWorkflowDefinitionStore,
    Any,
    StrictWorkflowRequestInput,
    StrictWorkflowBindingVerifier,
]:
    state_root = tmp_path / "state"
    requests, definitions = _stores(state_root)
    definition = definitions.create(
        mutation_request_id="i3c-definition-0001",
        source_yaml=_VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    request = _reserve(requests, definition)
    return (
        state_root,
        requests,
        definitions,
        definition,
        request,
        StrictWorkflowBindingVerifier(state_root),
    )


def _tamper(state_root: Path, sql: str, values: tuple[object, ...]) -> None:
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(sql, values)


def _database_state(state_root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        name: ((state_root / name).read_bytes(), (state_root / name).stat().st_mtime_ns)
        for name in ("definitions.sqlite", "requests.sqlite")
    }


def test_cross_database_happy_path_and_normalized_input_snapshot(tmp_path: Path) -> None:
    _state, _requests, _definitions, definition, request, verifier = _prepared(tmp_path)

    raw_input: dict[str, object] = {}
    normalized = verifier.normalize_for_definition(
        definition.definition_id, definition.revision, raw_input
    )
    assert normalized.normalized_input == {"mode": "safe"}
    assert normalized.normalized_input_sha256 == sha256_json({"mode": "safe"})
    assert raw_input == {}
    raw_input["mode"] = "caller-mutated"
    assert normalized.normalized_input == {"mode": "safe"}
    assert normalized.normalized_input_sha256 == sha256_json({"mode": "safe"})
    assert normalize_input(definition.contract, normalized.normalized_input) == {"mode": "safe"}
    repeated = verifier.normalize_for_definition(
        definition.definition_id, definition.revision, normalized.normalized_input
    )
    assert repeated.normalized_input == normalized.normalized_input
    assert repeated.normalized_input_sha256 == normalized.normalized_input_sha256

    metadata = verifier.verify(
        request.run_id,
        expected_workflow_id=request.workflow_id,
        expected_request_digest=request.request_digest,
    )
    assert metadata.definition_id == definition.definition_id
    assert metadata.definition_revision == 1
    assert metadata.source_sha256 == definition.source_sha256
    assert metadata.contract_sha256 == definition.contract_sha256
    assert metadata.binding_digest == definition.binding_digest
    assert metadata.normalized_input_sha256 == request.normalized_input_sha256
    assert metadata.normalized_input_snapshot_verified is False
    assert "private-prompt-sentinel" not in repr(metadata)

    checked = verifier.verify_normalized_input(request.run_id, {"mode": "safe"})
    assert checked.normalized_input_snapshot_verified is True


def test_pinned_revision_survives_latest_revision_update(tmp_path: Path) -> None:
    _state, _requests, definitions, definition, request, verifier = _prepared(tmp_path)
    latest = definitions.update(
        definition_id=definition.definition_id,
        expected_revision=1,
        mutation_request_id="i3c-definition-update-0001",
        source_yaml=_VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(default="new-default"),
    )

    assert latest.revision == 2
    assert definitions.get(definition.definition_id).revision == 2
    checked = verifier.verify(request.run_id)
    assert checked.definition_revision == 1
    assert checked.contract_sha256 == definition.contract_sha256


def test_request_replay_remains_readable_without_starting_a_run(tmp_path: Path) -> None:
    _state, requests, _definitions, _definition, request, verifier = _prepared(tmp_path)
    replay = requests.reserve_strict(request)
    assert replay.replayed is True
    assert verifier.verify(request.run_id).run_id == request.run_id


@pytest.mark.parametrize("database", ["requests", "definitions", "both"])
def test_missing_database_fails_with_stable_error(tmp_path: Path, database: str) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    if database == "requests":
        StrictWorkflowDefinitionStore(state_root).initialize_new_database()
    elif database == "definitions":
        requests = StrictWorkflowRequestStore(state_root)
        requests.initialize_new_database()
        requests.reserve_strict(
            StrictWorkflowRequestInput(
                client_request_id="missing-definition-0001",
                workflow_id=_WORKFLOW_ID,
                run_id=_RUN_ID,
                request_digest=_REQUEST_DIGEST,
                definition_id="11111111-1111-4111-8111-111111111111",
                definition_revision=1,
                source_sha256="b" * 64,
                contract_sha256="c" * 64,
                binding_digest="d" * 64,
                normalized_input_sha256=sha256_json({"mode": "safe"}),
                created_at=_CREATED,
            )
        )
    with pytest.raises(StrictWorkflowBindingError) as error:
        StrictWorkflowBindingVerifier(state_root).verify(_RUN_ID)
    assert str(error.value) == _ERROR


def test_missing_request_fails_closed(tmp_path: Path) -> None:
    state_root, _requests, _definitions, _definition, _request, verifier = _prepared(tmp_path)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify("run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert (state_root / "requests.sqlite").is_file()


def test_legacy_request_without_strict_binding_fails_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    requests, definitions = _stores(state_root)
    definition = definitions.create(
        mutation_request_id="i3c-definition-0001",
        source_yaml=_VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(
            "INSERT INTO workflow_requests (client_request_id, workflow_id, run_id, "
            "request_digest, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-request-0001",
                _WORKFLOW_ID,
                _RUN_ID,
                _REQUEST_DIGEST,
                "queued",
                _CREATED,
                _CREATED,
            ),
        )
    assert requests.inspect().request_count == 1
    assert definition.is_strict
    with pytest.raises(StrictWorkflowBindingError):
        StrictWorkflowBindingVerifier(state_root).verify(_RUN_ID)


def test_legacy_definition_fails_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    requests, definitions = _stores(state_root)
    legacy = definitions.create(
        mutation_request_id="i3c-definition-legacy-0001",
        source_yaml=_VALID_YAML,
        max_live_codex_jobs=1,
    )
    assert not legacy.is_strict
    _reserve(requests, legacy, normalized_input={"mode": "safe"})
    with pytest.raises(StrictWorkflowBindingError):
        StrictWorkflowBindingVerifier(state_root).verify(_RUN_ID)


def test_missing_pinned_revision_fails_closed(tmp_path: Path) -> None:
    state_root, requests, definitions, definition, _request, _verifier = _prepared(tmp_path)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_request_sources SET revision=99 WHERE run_id=?", (_RUN_ID,)
        )
        connection.execute(
            "UPDATE workflow_request_contract_bindings SET definition_revision=99 WHERE run_id=?",
            (_RUN_ID,),
        )
    assert definitions.get(definition.definition_id, 1).is_strict
    assert requests.read_binding(_RUN_ID) is not None
    with pytest.raises(StrictWorkflowBindingError):
        StrictWorkflowBindingVerifier(state_root).verify(_RUN_ID)


@pytest.mark.parametrize(
    ("source_sql", "binding_sql", "values"),
    [
        (
            "UPDATE workflow_request_sources SET definition_id=? WHERE run_id=?",
            "UPDATE workflow_request_contract_bindings SET definition_id=? WHERE run_id=?",
            ("33333333-3333-4333-8333-333333333333", _RUN_ID),
        ),
        (
            "UPDATE workflow_request_sources SET source_sha256=? WHERE run_id=?",
            "",
            ("f" * 64, _RUN_ID),
        ),
        (
            "UPDATE workflow_request_sources SET revision=99 WHERE run_id=?",
            "UPDATE workflow_request_contract_bindings SET definition_revision=99 WHERE run_id=?",
            (_RUN_ID,),
        ),
    ],
)
def test_cross_database_definition_identity_and_source_mismatches_fail_closed(
    tmp_path: Path, source_sql: str, binding_sql: str, values: tuple[object, ...]
) -> None:
    state_root, _requests, _definitions, _definition, _request, verifier = _prepared(tmp_path)
    _tamper(state_root, source_sql, values)
    if binding_sql:
        _tamper(state_root, binding_sql, values)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(_RUN_ID)


@pytest.mark.parametrize(
    ("column", "replacement"),
    [("contract_sha256", "f" * 64), ("binding_digest", "e" * 64)],
)
def test_cross_database_contract_hash_mismatches_fail_closed(
    tmp_path: Path, column: str, replacement: str
) -> None:
    state_root, _requests, _definitions, _definition, _request, verifier = _prepared(tmp_path)
    _tamper(
        state_root,
        f"UPDATE workflow_request_contract_bindings SET {column}=? WHERE run_id=?",
        (replacement, _RUN_ID),
    )
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(_RUN_ID)


def test_request_row_tamper_fails_inside_existing_request_store(tmp_path: Path) -> None:
    state_root, requests, _definitions, _definition, _request, verifier = _prepared(tmp_path)
    _tamper(
        state_root,
        "UPDATE workflow_requests SET request_digest=? WHERE run_id=?",
        ("f" * 64, _RUN_ID),
    )
    with pytest.raises(StrictWorkflowRequestError):
        requests.read_binding(_RUN_ID)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(_RUN_ID)


def test_request_binding_tamper_fails_cross_database_comparison(tmp_path: Path) -> None:
    state_root, requests, _definitions, _definition, _request, verifier = _prepared(tmp_path)
    _tamper(
        state_root,
        "UPDATE workflow_request_contract_bindings SET binding_digest=? WHERE run_id=?",
        ("e" * 64, _RUN_ID),
    )
    assert requests.read_binding(_RUN_ID) is not None
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(_RUN_ID)


def test_definition_contract_row_tamper_fails_inside_existing_definition_store(
    tmp_path: Path,
) -> None:
    state_root, _requests, definitions, definition, _request, verifier = _prepared(tmp_path)
    with sqlite3.connect(state_root / "definitions.sqlite") as connection:
        connection.execute(
            "UPDATE workflow_definition_contracts SET contract_json_canonical=? "
            "WHERE definition_id=? AND revision=?",
            ("{}", definition.definition_id, definition.revision),
        )
    with pytest.raises(DefinitionRepositoryUnavailable):
        definitions.get(definition.definition_id, definition.revision)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(_RUN_ID)


@pytest.mark.parametrize(
    "snapshot",
    [{}, {"mode": "tampered"}],
)
def test_missing_defaults_or_tampered_normalized_snapshot_are_rejected(
    tmp_path: Path, snapshot: dict[str, object]
) -> None:
    _state, _requests, _definitions, _definition, request, verifier = _prepared(tmp_path)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify_normalized_input(request.run_id, snapshot)


def test_expected_identity_values_are_validated_then_compared(tmp_path: Path) -> None:
    _state, _requests, _definitions, _definition, request, verifier = _prepared(tmp_path)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(request.run_id, expected_workflow_id="bad")
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(request.run_id, expected_request_digest="A" * 64)
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(
            request.run_id,
            expected_workflow_id="33333333-3333-4333-8333-333333333333",
        )
    with pytest.raises(StrictWorkflowBindingError):
        verifier.verify(request.run_id, expected_request_digest="f" * 64)


@pytest.mark.parametrize("unsafe_kind", ["cycle", "nan", "non_json_secret"])
def test_unsafe_input_fails_without_echoing_values(tmp_path: Path, unsafe_kind: str) -> None:
    _state, _requests, _definitions, definition, _request, verifier = _prepared(tmp_path)
    if unsafe_kind == "cycle":
        raw: dict[str, object] = {"mode": "safe"}
        raw["cycle"] = raw
    elif unsafe_kind == "nan":
        raw = {"mode": float("nan")}
    else:

        class SecretPayload:
            def __repr__(self) -> str:
                return "private-input-sentinel"

        raw = {"mode": "safe", "extra": SecretPayload()}
    with pytest.raises(StrictWorkflowBindingError) as error:
        verifier.normalize_for_definition(definition.definition_id, 1, raw)
    assert str(error.value) == _ERROR
    assert "private-prompt-sentinel" not in str(error.value)
    assert "private-input-sentinel" not in str(error.value)


def test_read_only_verification_preserves_database_files_and_rows(tmp_path: Path) -> None:
    state_root, _requests, _definitions, _definition, request, verifier = _prepared(tmp_path)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        request_rows_before = tuple(
            tuple(row)
            for row in connection.execute("SELECT * FROM workflow_requests ORDER BY run_id")
        )
        source_rows_before = tuple(
            tuple(row)
            for row in connection.execute("SELECT * FROM workflow_request_sources ORDER BY run_id")
        )
        binding_rows_before = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM workflow_request_contract_bindings ORDER BY run_id"
            )
        )
    with sqlite3.connect(state_root / "definitions.sqlite") as connection:
        definition_rows_before = tuple(
            (
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall())
                if table == "workflow_definition_revisions"
                else tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
            )
            for table in (
                "workflow_definitions",
                "workflow_definition_revisions",
                "workflow_definition_contracts",
                "workflow_definition_mutations",
            )
        )
    before = _database_state(state_root)
    verifier.verify(request.run_id)
    verifier.verify_normalized_input(request.run_id, {"mode": "safe"})
    verifier.normalize_for_definition(
        _definition.definition_id, _definition.revision, {"mode": "safe"}
    )
    after = _database_state(state_root)
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        request_rows_after = tuple(
            tuple(row)
            for row in connection.execute("SELECT * FROM workflow_requests ORDER BY run_id")
        )
        source_rows_after = tuple(
            tuple(row)
            for row in connection.execute("SELECT * FROM workflow_request_sources ORDER BY run_id")
        )
        binding_rows_after = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM workflow_request_contract_bindings ORDER BY run_id"
            )
        )
    with sqlite3.connect(state_root / "definitions.sqlite") as connection:
        definition_rows_after = tuple(
            (
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall())
                if table == "workflow_definition_revisions"
                else tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
            )
            for table in (
                "workflow_definitions",
                "workflow_definition_revisions",
                "workflow_definition_contracts",
                "workflow_definition_mutations",
            )
        )
    assert after == before
    assert request_rows_after == request_rows_before
    assert source_rows_after == source_rows_before
    assert binding_rows_after == binding_rows_before
    assert definition_rows_after == definition_rows_before


def test_repr_redacts_normalized_input_and_prompt(tmp_path: Path) -> None:
    _state, _requests, _definitions, definition, _request, verifier = _prepared(tmp_path)
    normalized = verifier.normalize_for_definition(
        definition.definition_id,
        definition.revision,
        {"mode": "private-input-sentinel"},
    )
    assert "private-input-sentinel" not in repr(normalized)
    assert "private-prompt-sentinel" not in repr(normalized)
