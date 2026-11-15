from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from codex_bridge.workflows import definitions as definitions_module
from codex_bridge.workflows.definitions import (
    DefinitionInvalid,
    DefinitionMutationConflict,
    DefinitionNotFound,
    DefinitionPreconditionFailed,
    DefinitionRepositoryUnavailable,
    WorkflowDefinitionRepository,
)
from codex_bridge.workflows.interpreter import OwsError, parse_workflow_text
from codex_bridge.workflows.service import _canonical_json

VALID_YAML = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: definition-test
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


def _repo(tmp_path: Path) -> WorkflowDefinitionRepository:
    return WorkflowDefinitionRepository(tmp_path / "workflow-state")


def test_create_preserves_source_and_returns_both_hashes_and_validated_definition(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    source_yaml = VALID_YAML.replace("\n", "\r\n") + "# original comment\r\n"

    saved = repo.create(
        mutation_request_id="create-001",
        source_yaml=source_yaml,
        max_live_codex_jobs=1,
    )

    parsed = parse_workflow_text(source_yaml)
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert _canonical_json(parsed) == canonical
    assert saved.revision == 1
    assert saved.source_yaml == source_yaml
    assert saved.source_sha256 == hashlib.sha256(source_yaml.encode("utf-8")).hexdigest()
    assert saved.canonical_sha256 == hashlib.sha256((canonical + "\n").encode("utf-8")).hexdigest()
    assert saved.definition == parsed
    assert saved.etag.startswith('"') and saved.etag.endswith('"')
    assert not saved.etag.startswith("W/")

    read_back = repo.get(saved.definition_id)
    assert read_back == saved
    assert (tmp_path / "workflow-state" / "definitions.sqlite").is_file()
    assert not (tmp_path / "workflow-state" / "requests.sqlite").exists()


def test_create_replay_returns_original_revision_and_rejects_payload_reuse(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = repo.create(
        mutation_request_id="create-replay",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )

    replay = repo.create(
        mutation_request_id="create-replay",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )

    assert replay == first
    assert repo.list().items[0].latest_revision == 1
    with pytest.raises(DefinitionMutationConflict):
        repo.create(
            mutation_request_id="create-replay",
            source_yaml=VALID_YAML + "# changed\n",
            max_live_codex_jobs=1,
        )


def test_update_is_compare_and_swap_and_replays_before_stale_precondition(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    first = repo.create(
        mutation_request_id="create-cas",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    source_two = VALID_YAML + "# revision two\n"
    second = repo.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="update-two",
        source_yaml=source_two,
        max_live_codex_jobs=1,
    )
    third = repo.update(
        definition_id=first.definition_id,
        expected_revision=2,
        mutation_request_id="update-three",
        source_yaml=VALID_YAML + "# revision three\n",
        max_live_codex_jobs=1,
    )

    replay = repo.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="update-two",
        source_yaml=source_two,
        max_live_codex_jobs=1,
    )

    assert second.revision == 2
    assert third.revision == 3
    assert replay == second
    assert repo.get(first.definition_id).revision == 3
    assert repo.get(first.definition_id, revision=1) == first
    with pytest.raises(DefinitionMutationConflict):
        repo.update(
            definition_id=first.definition_id,
            expected_revision=2,
            mutation_request_id="update-two",
            source_yaml=source_two,
            max_live_codex_jobs=1,
        )
    with pytest.raises(DefinitionMutationConflict):
        repo.update(
            definition_id=first.definition_id,
            expected_revision=0,
            mutation_request_id="update-two",
            source_yaml=source_two,
            max_live_codex_jobs=1,
        )


def test_update_noop_keeps_revision_but_records_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = repo.create(
        mutation_request_id="create-noop",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )

    unchanged = repo.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="noop-one",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    changed = repo.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="noop-two",
        source_yaml=VALID_YAML + "# one byte of source change\n",
        max_live_codex_jobs=1,
    )

    assert unchanged == first
    assert changed.revision == 2
    assert changed.etag != first.etag


def test_update_reports_missing_and_stale_revisions(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(DefinitionNotFound):
        repo.update(
            definition_id="00000000-0000-4000-8000-000000000001",
            expected_revision=1,
            mutation_request_id="update-missing",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
        )

    first = repo.create(
        mutation_request_id="create-stale",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    repo.update(
        definition_id=first.definition_id,
        expected_revision=1,
        mutation_request_id="update-current",
        source_yaml=VALID_YAML + "# current\n",
        max_live_codex_jobs=1,
    )
    with pytest.raises(DefinitionPreconditionFailed):
        repo.update(
            definition_id=first.definition_id,
            expected_revision=1,
            mutation_request_id="update-stale",
            source_yaml=VALID_YAML + "# stale\n",
            max_live_codex_jobs=1,
        )


@pytest.mark.parametrize(
    ("source_yaml", "profile"),
    [
        ("", 1),
        (" \r\n\t", 1),
        (VALID_YAML + ("#" + "x" * 65_536), 1),
        (VALID_YAML + "\ud800", 1),
        (VALID_YAML, True),
        (VALID_YAML, 0),
        (VALID_YAML, 4),
    ],
    ids=[
        "empty",
        "whitespace",
        "too-large",
        "invalid-unicode",
        "bool-profile",
        "low-profile",
        "high-profile",
    ],
)
def test_validate_rejects_invalid_source_encoding_size_or_profile(
    tmp_path: Path, source_yaml: str, profile: int
) -> None:
    repo = _repo(tmp_path)

    with pytest.raises(DefinitionInvalid):
        repo.validate(source_yaml, profile)


def test_validate_rejects_invalid_yaml_without_echoing_source(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    secret_marker = "private-prompt-fragment"

    with pytest.raises(DefinitionInvalid) as raised:
        repo.validate(f"not: [valid YAML {secret_marker}", 1)

    assert secret_marker not in str(raised.value)


def test_schema_unavailability_is_classified_without_exposing_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OwsError("could not load official OWS v1.0.3 schema: local path")

    monkeypatch.setattr(definitions_module, "validate_workflow", unavailable)
    with pytest.raises(DefinitionRepositoryUnavailable) as raised:
        repo.validate(VALID_YAML, 1)
    assert "local path" not in str(raised.value)


def test_mutation_id_and_definition_identifiers_use_fixed_safe_forms(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    with pytest.raises(DefinitionInvalid):
        repo.create(
            mutation_request_id="../unsafe",
            source_yaml=VALID_YAML,
            max_live_codex_jobs=1,
        )
    with pytest.raises(DefinitionInvalid):
        repo.get("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")


def test_missing_database_reads_are_empty_or_not_found_without_creating_state(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "missing-state"
    repo = WorkflowDefinitionRepository(state_root)

    page = repo.list()

    assert page.items == ()
    assert page.next_cursor is None
    with pytest.raises(DefinitionNotFound):
        repo.get("00000000-0000-4000-8000-000000000001")
    assert not state_root.exists()


def test_read_only_missing_database_does_not_create_sqlite_file(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    repo = WorkflowDefinitionRepository(state_root)

    assert repo.list().items == ()
    with pytest.raises(DefinitionNotFound):
        repo.get("00000000-0000-4000-8000-000000000001")
    assert not (state_root / "definitions.sqlite").exists()


def test_corrupt_database_is_unavailable_and_read_preserves_its_bytes(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    database = state_root / "definitions.sqlite"
    database.write_bytes(b"not a sqlite database")
    repo = WorkflowDefinitionRepository(state_root)

    with pytest.raises(DefinitionRepositoryUnavailable):
        repo.list()

    assert database.read_bytes() == b"not a sqlite database"


def test_unexpected_database_error_is_classified_without_exposing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    database = state_root / "definitions.sqlite"
    database.write_bytes(b"database placeholder")

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(str(database))

    monkeypatch.setattr(definitions_module.sqlite3, "connect", fail_connect)
    with pytest.raises(DefinitionRepositoryUnavailable) as raised:
        WorkflowDefinitionRepository(state_root).list()

    assert str(database) not in str(raised.value)


def test_list_uses_stable_keyset_cursor_and_enforces_limit_bounds(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = repo.create(
        mutation_request_id="list-create-a",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    second = repo.create(
        mutation_request_id="list-create-b",
        source_yaml=VALID_YAML.replace("definition-test", "definition-test-b"),
        max_live_codex_jobs=1,
    )

    page_one = repo.list(limit=1)
    page_two = repo.list(limit=1, cursor=page_one.next_cursor)

    expected_ids = sorted([first.definition_id, second.definition_id])
    assert [page_one.items[0].definition_id, page_two.items[0].definition_id] == expected_ids
    assert page_one.next_cursor == f"v1:{expected_ids[0]}"
    assert page_two.next_cursor is None
    with pytest.raises(DefinitionInvalid):
        repo.list(limit=0)
    with pytest.raises(DefinitionInvalid):
        repo.list(limit=101)
    with pytest.raises(DefinitionInvalid):
        repo.list(limit=True)
    with pytest.raises(DefinitionInvalid):
        repo.list(cursor="not-a-cursor")


def test_independent_repository_instances_allow_only_one_stale_cas_update(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "shared-state"
    first_repo = WorkflowDefinitionRepository(state_root)
    second_repo = WorkflowDefinitionRepository(state_root)
    first = first_repo.create(
        mutation_request_id="concurrent-create",
        source_yaml=VALID_YAML,
        max_live_codex_jobs=1,
    )
    barrier = Barrier(2)

    def update(repository: WorkflowDefinitionRepository, mutation_id: str) -> str:
        barrier.wait()
        try:
            repository.update(
                definition_id=first.definition_id,
                expected_revision=1,
                mutation_request_id=mutation_id,
                source_yaml=VALID_YAML + f"# {mutation_id}\n",
                max_live_codex_jobs=1,
            )
        except DefinitionPreconditionFailed:
            return "stale"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda args: update(*args),
                [
                    (first_repo, "concurrent-one"),
                    (second_repo, "concurrent-two"),
                ],
            )
        )

    assert sorted(outcomes) == ["stale", "updated"]
    assert first_repo.get(first.definition_id).revision == 2


def test_state_root_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "root-link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlink creation is unavailable: {error}")

    with pytest.raises(DefinitionRepositoryUnavailable):
        WorkflowDefinitionRepository(link).list()


def test_database_symlink_is_rejected(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"not a database")
    link = state_root / "definitions.sqlite"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"file symlink creation is unavailable: {error}")

    with pytest.raises(DefinitionRepositoryUnavailable):
        WorkflowDefinitionRepository(state_root).list()
