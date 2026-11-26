from __future__ import annotations

import json
import sqlite3
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

import codex_bridge.workflows.strict_reader_gate as strict_reader_gate
from codex_bridge.workflows.strict_manifest import (
    StrictWorkflowSnapshotVerification,
    StrictWorkflowSnapshotVerifier,
    serialize_strict_manifest_v2,
)
from codex_bridge.workflows.strict_reader_gate import (
    StrictReaderGateError,
)
from tests.test_workflow_d1_i5a_manifest import (
    _RUN_ID,
    _WORKFLOW_ID,
    _base_fixture,
    _Fixture,
    _prepare,
    _reserve,
)
from tests.test_workflow_d1_i5b1_service_gate import _start_legacy_run


def _call_gate(*args: object, **kwargs: object):
    verifier = getattr(strict_reader_gate, "verify_strict_reserved_readonly", None)
    assert callable(verifier), "verify_strict_reserved_readonly is not implemented"
    return verifier(*args, **kwargs)


def _verify(fixture: _Fixture, *, allowed_roots: tuple[str, ...] | None = None):
    state_root = fixture.state_root
    workspace = fixture.workspace
    manifest_bytes = (state_root / _RUN_ID / "manifest.json").read_bytes()
    manifest_value = json.loads(manifest_bytes)
    return _call_gate(
        state_root,
        _RUN_ID,
        manifest_bytes,
        expected_workflow_id=_WORKFLOW_ID,
        expected_request_digest=manifest_value["request_digest"],
        allowed_roots=(str(workspace),) if allowed_roots is None else allowed_roots,
    )


def test_reserved_strict_snapshot_is_verified_read_only(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    paths = [
        fixture.run_directory / name
        for name in ("manifest.json", "workflow.yaml", "input.json", "variables.sqlite")
    ] + [fixture.state_root / name for name in ("requests.sqlite", "definitions.sqlite")]
    before = {path: path.read_bytes() for path in paths}

    verification = _verify(fixture)

    assert verification is not None
    assert verification.manifest == manifest
    assert verification.reservation_verified is True
    assert {path: path.read_bytes() for path in paths} == before


def test_unreserved_strict_snapshot_is_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    _prepare(fixture)

    with pytest.raises(StrictReaderGateError):
        _verify(fixture)


def test_original_manifest_must_match_reserved_verifier_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    changed = replace(manifest, workspace_root=str(tmp_path / "changed-workspace"))

    def return_changed_manifest(
        _verifier: StrictWorkflowSnapshotVerifier, _run_id: str
    ) -> StrictWorkflowSnapshotVerification:
        (fixture.run_directory / "manifest.json").write_bytes(serialize_strict_manifest_v2(changed))
        return StrictWorkflowSnapshotVerification(
            manifest=changed,
            reservation_verified=True,
            mutation_count=0,
        )

    monkeypatch.setattr(StrictWorkflowSnapshotVerifier, "verify_reserved", return_changed_manifest)

    with pytest.raises(StrictReaderGateError):
        _verify(fixture)


@pytest.mark.parametrize("identity", ["workflow", "request_digest"])
def test_strict_reader_gate_rejects_row_identity_mismatch(
    tmp_path: Path,
    identity: str,
) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    payload = (fixture.run_directory / "manifest.json").read_bytes()
    expected_workflow_id = (
        "33333333-3333-4333-8333-333333333333" if identity == "workflow" else _WORKFLOW_ID
    )
    expected_request_digest = "f" * 64 if identity == "request_digest" else manifest.request_digest

    with pytest.raises(StrictReaderGateError):
        _call_gate(
            fixture.state_root,
            _RUN_ID,
            payload,
            expected_workflow_id=expected_workflow_id,
            expected_request_digest=expected_request_digest,
            allowed_roots=(str(fixture.workspace),),
        )


@pytest.mark.parametrize("name", ["workflow.yaml", "input.json"])
def test_snapshot_or_input_mutation_is_rejected(tmp_path: Path, name: str) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    path = fixture.run_directory / name
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(StrictReaderGateError):
        _verify(fixture)


@pytest.mark.parametrize("state", ["missing", "corrupt", "binding_mismatch"])
def test_invalid_variable_store_is_rejected(tmp_path: Path, state: str) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    database = fixture.run_directory / "variables.sqlite"
    if state == "missing":
        database.unlink()
    elif state == "corrupt":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE run_binding SET normalized_input_sha256 = ? WHERE run_id = ?",
                ("f" * 64, _RUN_ID),
            )

    with pytest.raises(StrictReaderGateError):
        _verify(fixture)


def test_workspace_outside_allowed_roots_is_rejected(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(StrictReaderGateError):
        _verify(fixture, allowed_roots=(str(outside),))


def test_unknown_version_and_extra_values_are_redacted(tmp_path: Path) -> None:
    fixture = _base_fixture(tmp_path)
    manifest = _prepare(fixture)
    _reserve(fixture, manifest)
    payload = manifest.to_dict()
    payload["run_format_version"] = 3
    payload["private"] = "private-input-sentinel"
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    with pytest.raises(StrictReaderGateError) as caught:
        _call_gate(
            fixture.state_root,
            _RUN_ID,
            raw,
            expected_workflow_id=_WORKFLOW_ID,
            expected_request_digest=manifest.request_digest,
            allowed_roots=(str(fixture.workspace),),
        )

    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert str(caught.value) == "NEEDS_RECONCILIATION"
    assert "private-input-sentinel" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_legacy_manifest_is_classified_without_strict_verification(tmp_path: Path) -> None:
    state_root = tmp_path / "legacy-state"
    workspace = tmp_path / "legacy-workspace"
    workspace.mkdir()
    _, _, accepted = await _start_legacy_run(state_root, workspace, "b4a")
    manifest_bytes = (state_root / accepted.run_id / "manifest.json").read_bytes()
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        request_digest = connection.execute(
            "SELECT request_digest FROM workflow_requests WHERE run_id = ?",
            (accepted.run_id,),
        ).fetchone()[0]

    assert (
        _call_gate(
            state_root,
            accepted.run_id,
            manifest_bytes,
            expected_workflow_id=accepted.workflow_id,
            expected_request_digest=request_digest,
            allowed_roots=(str(workspace),),
        )
        is None
    )
