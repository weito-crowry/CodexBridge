from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from codex_bridge.workflows import strict_definitions as strict_module
from codex_bridge.workflows.contracts import canonical_json
from codex_bridge.workflows.definitions import (
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    WorkflowDefinitionRepository,
)
from codex_bridge.workflows.strict_definitions import StrictWorkflowDefinitionStore

VALID_YAML = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: i3a-contract-storage
  version: "1.0.0"
evaluate:
  language: jq
  mode: strict
do:
  - first:
      call: codex-job
      with:
        prompt: "Return the word ready and do nothing else."
"""
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _legacy(state_root: Path) -> WorkflowDefinitionRepository:
    return WorkflowDefinitionRepository(state_root)


def _strict(state_root: Path) -> StrictWorkflowDefinitionStore:
    return StrictWorkflowDefinitionStore(state_root)


def _schema(
    properties: dict[str, object] | None = None,
    *,
    additional: object = False,
) -> dict[str, object]:
    schema: dict[str, object] = {
        "$schema": DRAFT_2020_12,
        "type": "object",
        "additionalProperties": additional,
    }
    if properties is not None:
        schema["properties"] = properties
    return schema


def _contract(*, default: str | None = None) -> dict[str, object]:
    properties: dict[str, object] = {}
    if default is not None:
        properties["mode"] = {"type": "string", "default": default}
    return {
        "contract_version": 1,
        "input_schema": _schema(properties),
        "output_schema": _schema(),
        "variables": [],
    }


def _make_v1_with_history(state_root: Path) -> tuple[object, object]:
    legacy = _legacy(state_root)
    first = legacy.create(
        mutation_request_id="legacy-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    second = legacy.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="legacy-update",
        source_yaml=VALID_YAML + "# revision two\n",
        max_live_codex_jobs=1,
    )
    noop = legacy.update(
        definition_id=first.definition_id,
        expected_revision=2,
        mutation_request_id="legacy-noop",
        source_yaml=second.source_yaml,
        max_live_codex_jobs=1,
    )
    assert noop == second
    return first, second


def _migrate(state_root: Path) -> tuple[StrictWorkflowDefinitionStore, object, object]:
    first, second = _make_v1_with_history(state_root)
    store = _strict(state_root)
    store.migrate_v1_to_v2(backup_path=state_root / "definitions-v1.backup")
    return store, first, second


def _database(state_root: Path) -> Path:
    return state_root / "definitions.sqlite"


def test_new_v2_initialization_is_explicit_and_legacy_repository_stays_v1_only(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    store = _strict(state_root)
    store.initialize_new_database()

    with sqlite3.connect(_database(state_root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert names == {
        "workflow_definitions",
        "workflow_definition_revisions",
        "workflow_definition_mutations",
        "workflow_definition_contracts",
    }
    with pytest.raises(DefinitionRepositoryUnavailable):
        _legacy(state_root).get("00000000-0000-4000-8000-000000000001")


def test_migration_preserves_legacy_revisions_etags_hashes_and_replays(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store, first, second = _migrate(state_root)

    actual_first = store.get(first.definition_id, revision=1)
    actual_second = store.get(first.definition_id, revision=2)
    assert actual_first.source_yaml == first.source_yaml
    assert actual_first.source_sha256 == first.source_sha256
    assert actual_first.canonical_sha256 == first.canonical_sha256
    assert actual_first.etag == first.etag
    assert actual_first.contract is None
    assert actual_second.source_yaml == second.source_yaml
    assert actual_second.source_sha256 == second.source_sha256
    assert actual_second.canonical_sha256 == second.canonical_sha256
    assert actual_second.etag == second.etag
    assert actual_second.contract is None
    assert (
        store.create(
            mutation_request_id="legacy-create",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
        )
        == actual_first
    )
    assert (
        store.update(
            definition_id=first.definition_id,
            expected_revision=1,
            mutation_request_id="legacy-update",
            source_yaml=VALID_YAML + "# revision two\n",
            max_live_codex_jobs=1,
        )
        == actual_second
    )
    with sqlite3.connect(_database(state_root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_contracts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_mutations"
        ).fetchone() == (3,)
    with pytest.raises(DefinitionRepositoryUnavailable):
        _legacy(state_root).get(first.definition_id)


def test_online_backup_can_be_restored_into_a_new_isolated_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    first, _second = _make_v1_with_history(source_root)
    store = _strict(source_root)
    backup_path = source_root / "v1.snapshot"
    identity = store.create_verified_backup(backup_path)
    assert identity.definition_count == 1
    assert identity.revision_count == 2
    assert identity.mutation_count == 3

    restored_root = tmp_path / "restored"
    store.restore_verified_backup(
        backup_path,
        destination_state_root=restored_root,
    )
    restored = _legacy(restored_root).get(first.definition_id)
    assert restored.revision == 2
    assert restored.etag == _legacy(source_root).get(first.definition_id).etag


@pytest.mark.parametrize("checkpoint", ["after_create", "after_user_version", "before_commit"])
def test_migration_failure_at_each_private_checkpoint_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
) -> None:
    state_root = tmp_path / checkpoint
    _make_v1_with_history(state_root)
    store = _strict(state_root)

    def fail_at(stage: str) -> None:
        if stage == checkpoint:
            raise RuntimeError("private injected failure")

    monkeypatch.setattr(strict_module, "_migration_checkpoint", fail_at)
    with pytest.raises(DefinitionRepositoryUnavailable) as error:
        store.migrate_v1_to_v2(backup_path=state_root / "backup.sqlite")
    assert "private injected failure" not in str(error.value)

    with sqlite3.connect(_database(state_root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='workflow_definition_contracts'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize(
    "tamper",
    ["unknown_table", "unknown_index", "unknown_version", "partial_schema", "foreign_key"],
)
def test_migration_rejects_unknown_or_inconsistent_v1_databases(
    tmp_path: Path,
    tamper: str,
) -> None:
    state_root = tmp_path / tamper
    _make_v1_with_history(state_root)
    with sqlite3.connect(_database(state_root)) as connection:
        if tamper == "unknown_table":
            connection.execute("CREATE TABLE extension_table (value TEXT)")
        elif tamper == "unknown_index":
            connection.execute("CREATE INDEX extension_index ON workflow_definitions (updated_at)")
        elif tamper == "unknown_version":
            connection.execute("PRAGMA user_version=7")
        elif tamper == "partial_schema":
            connection.execute("DROP TABLE workflow_definition_mutations")
        elif tamper == "foreign_key":
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """
                INSERT INTO workflow_definition_revisions (
                    definition_id, revision, source_yaml, source_sha256,
                    canonical_sha256, max_live_codex_jobs, created_at
                ) VALUES (?, 9, ?, ?, ?, 1, '2026-11-19T00:00:00+00:00')
                """,
                (
                    "00000000-0000-4000-8000-000000000099",
                    VALID_YAML,
                    hashlib.sha256(VALID_YAML.encode()).hexdigest(),
                    "0" * 64,
                ),
            )

    with pytest.raises(DefinitionRepositoryUnavailable):
        _strict(state_root).migrate_v1_to_v2(backup_path=state_root / "backup.sqlite")
    with sqlite3.connect(_database(state_root)) as connection:
        expected_version = 7 if tamper == "unknown_version" else 1
        assert connection.execute("PRAGMA user_version").fetchone() == (expected_version,)


def test_strict_create_read_binding_and_v2_etag_match_the_decision_payload(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    store = _strict(state_root)
    store.initialize_new_database()
    raw_contract = _contract(default="private-default-sentinel")
    created = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=raw_contract,
    )
    read_back = store.get(created.definition_id)

    expected_binding = hashlib.sha256(
        canonical_json(
            {
                "definition_id": created.definition_id,
                "revision": 1,
                "source_sha256": created.source_sha256,
                "canonical_sha256": created.canonical_sha256,
                "max_live_codex_jobs": 1,
                "contract_version": 1,
                "contract_sha256": created.contract_sha256,
            }
        ).encode("utf-8")
    ).hexdigest()
    etag_digest = hashlib.sha256(
        canonical_json(
            {
                "domain": "codexbridge-workflow-definition-etag-v2",
                "definition_id": created.definition_id,
                "revision": 1,
                "source_sha256": created.source_sha256,
                "canonical_sha256": created.canonical_sha256,
                "max_live_codex_jobs": 1,
                "contract_version": 1,
                "contract_sha256": created.contract_sha256,
                "binding_digest": expected_binding,
            }
        ).encode("utf-8")
    ).hexdigest()

    assert created.is_strict
    assert created.contract_sha256 is not None
    assert created.binding_digest == expected_binding
    assert created.etag == f'"workflow-definition-v2-{etag_digest}"'
    assert read_back == created
    assert read_back.contract is not None
    assert read_back.contract.contract_sha256 == created.contract_sha256
    assert "private-default-sentinel" not in repr(created)
    assert "private-default-sentinel" not in repr(created.contract)
    with sqlite3.connect(_database(state_root)) as connection:
        row = connection.execute(
            """
            SELECT contract_json_canonical, contract_sha256, binding_digest
            FROM workflow_definition_contracts WHERE definition_id=? AND revision=1
            """,
            (created.definition_id,),
        ).fetchone()
    assert row == (
        canonical_json(raw_contract),
        created.contract_sha256,
        expected_binding,
    )


def test_contract_only_change_creates_revision_and_exact_replay_is_stable(
    tmp_path: Path,
) -> None:
    store = _strict(tmp_path / "state")
    store.initialize_new_database()
    first_contract = _contract(default="first")
    second_contract = _contract(default="second")
    first = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=first_contract,
    )
    noop = store.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="strict-noop",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=first_contract,
    )
    changed = store.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="strict-contract-change",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=second_contract,
    )
    replay = store.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="strict-contract-change",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=second_contract,
    )

    assert noop.revision == 1
    assert changed.revision == 2
    assert changed.contract_sha256 != first.contract_sha256
    assert changed.etag != first.etag
    assert replay == changed
    assert store.get(first.definition_id).revision == 2
    with sqlite3.connect(_database(tmp_path / "state")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_revisions"
        ).fetchone() == (2,)


def test_legacy_to_strict_upgrade_and_legacy_identity_separation(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    first, _second = _make_v1_with_history(state_root)
    store = _strict(state_root)
    store.migrate_v1_to_v2(backup_path=state_root / "backup.sqlite")

    upgraded = store.update(
        definition_id=first.definition_id,
        expected_revision=2,
        mutation_request_id="legacy-to-strict",
        source_yaml=first.source_yaml,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    assert upgraded.revision == 3
    assert upgraded.is_strict
    assert upgraded.etag.startswith('"workflow-definition-v2-')
    assert first.etag.startswith('"workflow-definition-v1-')
    assert upgraded.etag != first.etag


def test_legacy_and_strict_mutations_cannot_share_a_request_id(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    _legacy(state_root).create(
        mutation_request_id="shared-request-id",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    store = _strict(state_root)
    store.migrate_v1_to_v2(backup_path=state_root / "backup.sqlite")

    with pytest.raises(DefinitionMutationConflict):
        store.create(
            mutation_request_id="shared-request-id",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
            contract=_contract(),
        )


@pytest.mark.parametrize("contract_value", [None, ...])
def test_strict_update_requires_explicit_non_null_contract(
    tmp_path: Path,
    contract_value: object,
) -> None:
    store = _strict(tmp_path / "state")
    store.initialize_new_database()
    first = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    kwargs: dict[str, Any] = {}
    if contract_value is not ...:
        kwargs["contract"] = contract_value
    with pytest.raises(DefinitionInvalid, match="contract is required"):
        store.update(
            definition_id=first.definition_id,
            expected_revision=1,
            mutation_request_id=f"missing-contract-{contract_value is None}",
            source_yaml=VALID_YAML + "# change\n",
            max_live_codex_jobs=1,
            **kwargs,
        )
    assert store.get(first.definition_id).revision == 1


def test_stale_strict_cas_and_reused_request_id_with_different_contract_conflict(
    tmp_path: Path,
) -> None:
    store = _strict(tmp_path / "state")
    store.initialize_new_database()
    first = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    second = store.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="strict-update",
        source_yaml=VALID_YAML + "# changed\n",
        max_live_codex_jobs=1,
        contract=_contract(default="updated"),
    )
    with pytest.raises(DefinitionPreconditionFailed):
        store.update(
            definition_id=first.definition_id,
            expected_revision=1,
            mutation_request_id="strict-stale",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
            contract=_contract(),
        )
    with pytest.raises(DefinitionMutationConflict):
        store.update(
            definition_id=first.definition_id,
            expected_revision=1,
            mutation_request_id="strict-update",
            source_yaml=VALID_YAML + "# changed\n",
            max_live_codex_jobs=1,
            contract=_contract(default="different"),
        )
    assert store.get(first.definition_id).revision == 2
    assert second.is_strict


def test_invalid_and_secret_contracts_are_rejected_without_leaking_values(
    tmp_path: Path,
) -> None:
    store = _strict(tmp_path / "state")
    store.initialize_new_database()
    invalid = {
        "contract_version": 1,
        "input_schema": {},
        "output_schema": _schema(),
        "variables": [],
    }
    secret = {
        "contract_version": 1,
        "input_schema": _schema(),
        "output_schema": _schema(),
        "variables": [
            {
                "declaration_id": "secret",
                "name": "secret",
                "owner_scope": "workflow",
                "value_schema": {"type": "string"},
                "sensitivity": "secret",
                "initial": "must-not-appear",
            }
        ],
    }
    for value in (invalid, secret):
        with pytest.raises(DefinitionInvalid) as error:
            store.create(
                mutation_request_id=f"invalid-{len(repr(value))}",
                source_yaml=VALID_YAML,
                max_live_codex_jobs=1,
                contract=value,
            )
        assert "must-not-appear" not in str(error.value)
        assert "must-not-appear" not in repr(error.value)
    with sqlite3.connect(_database(tmp_path / "state")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_definitions").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_mutations"
        ).fetchone() == (0,)


@pytest.mark.parametrize("column", ["contract_sha256", "binding_digest"])
def test_contract_hash_or_binding_tamper_fails_closed(
    tmp_path: Path,
    column: str,
) -> None:
    state_root = tmp_path / column
    store = _strict(state_root)
    store.initialize_new_database()
    created = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    with sqlite3.connect(_database(state_root)) as connection:
        connection.execute(
            f"UPDATE workflow_definition_contracts SET {column}=? WHERE definition_id=?",
            ("0" * 64, created.definition_id),
        )
    with pytest.raises(DefinitionRepositoryUnavailable):
        store.get(created.definition_id)


def test_deleting_a_strict_contract_row_does_not_downgrade_its_revision(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    store = _strict(state_root)
    store.initialize_new_database()
    created = store.create(
        mutation_request_id="strict-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
        contract=_contract(),
    )
    with sqlite3.connect(_database(state_root)) as connection:
        connection.execute(
            """
            DELETE FROM workflow_definition_contracts
            WHERE definition_id=? AND revision=?
            """,
            (created.definition_id, created.revision),
        )
    with pytest.raises(DefinitionRepositoryUnavailable):
        store.get(created.definition_id)


def test_v2_schema_rejects_unknown_objects_and_legacy_v1_never_auto_migrates(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    store = _strict(state_root)
    store.initialize_new_database()
    with sqlite3.connect(_database(state_root)) as connection:
        connection.execute("CREATE TABLE unexpected (value TEXT)")
    with pytest.raises(DefinitionRepositoryUnavailable):
        store.get("00000000-0000-4000-8000-000000000001")

    v1_root = tmp_path / "legacy"
    _make_v1_with_history(v1_root)
    legacy = _legacy(v1_root)
    with sqlite3.connect(_database(v1_root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
    assert legacy.get(_legacy(v1_root).list().items[0].definition_id).revision == 2
    with sqlite3.connect(_database(v1_root)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_two_threads_reusing_same_strict_request_id_commit_once(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = _strict(state_root)
    store.initialize_new_database()
    barrier = Barrier(2)

    def create() -> object:
        barrier.wait()
        return store.create(
            mutation_request_id="concurrent-strict-create",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
            contract=_contract(default="concurrent"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create)
        second = executor.submit(create)
        left = first.result()
        right = second.result()

    assert left == right
    with sqlite3.connect(_database(state_root)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_definitions").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_revisions"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM workflow_definition_mutations"
        ).fetchone() == (1,)
