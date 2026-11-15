from __future__ import annotations

import hashlib
import importlib
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.interpreter import validate_workflow


def _load_runner() -> ModuleType | None:
    try:
        return importlib.import_module("scripts.phase5e2c2_isolated_smoke")
    except ImportError:
        return None


def test_two_job_builder_uses_pinned_fixture_and_runtime_prompt() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    first_job_fixture = yaml.safe_load(module.FIRST_JOB_FIXTURE_PATH.read_text(encoding="utf-8"))
    first_prompt = first_job_fixture["do"][0]["calculate"]["with"]["prompt"]
    runtime_prompt = "runtime supplied prompt"
    workflow_yaml = module.build_two_job_workflow_yaml(second_prompt=runtime_prompt)
    workflow = yaml.safe_load(workflow_yaml)

    validate_workflow(workflow, codex_job_live=True, max_live_codex_jobs=2)
    assert workflow["do"][0]["first"]["with"]["prompt"] == first_prompt
    assert workflow["do"][1]["second"]["with"] == {
        "prompt": runtime_prompt,
        "prompt_from": "${ .result.final_text }",
    }

    payload = module.build_two_job_payload(
        workflow_yaml=workflow_yaml,
        client_request_id="phase5e2c2-fixed-id",
        workspace_root="C:\\isolated\\workspace",
    )
    assert payload == {
        "client_request_id": "phase5e2c2-fixed-id",
        "workflow_yaml": workflow_yaml,
        "input": {},
        "workspace_root": "C:\\isolated\\workspace",
        "max_live_codex_jobs": 2,
    }


def test_runtime_prompt_environment_rejects_an_unapproved_value() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"

    with pytest.raises(module.SmokeAbort) as error:
        module.approved_second_prompt(
            {"CODEXBRIDGE_PHASE5E2C2_SECOND_PROMPT": "unapproved test prompt"}
        )

    assert error.value.code == "second_prompt_hash_mismatch"


def test_job_event_window_counts_sequential_jobs_as_one_active_job() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    events: list[dict[str, Any]] = [
        {"event_id": 1, "job_id": "first", "state": "MAYBE_SENT"},
        {"event_id": 2, "job_id": "first", "state": "ACCEPTED"},
        {"event_id": 3, "job_id": "first", "state": "SUCCEEDED"},
        {"event_id": 4, "job_id": "second", "state": "MAYBE_SENT"},
        {"event_id": 5, "job_id": "second", "state": "RUNNING"},
        {"event_id": 6, "job_id": "second", "state": "SUCCEEDED"},
    ]

    assert module.maximum_active_codex_jobs(events) == 1
    overlapping = [
        events[0],
        events[1],
        {"event_id": 3, "job_id": "second", "state": "MAYBE_SENT"},
        {"event_id": 4, "job_id": "first", "state": "SUCCEEDED"},
        events[5],
    ]
    assert module.maximum_active_codex_jobs(overlapping) == 2


def test_definition_hash_uses_the_exact_stored_snapshot_bytes() -> None:
    module = _load_runner()
    assert module is not None, "the independent Phase 5E-2C2 runner must exist"
    stored_definition = '{"document":{"name":"fixture"}}\n'

    assert (
        module.definition_sha256(stored_definition)
        == hashlib.sha256(stored_definition.encode("utf-8")).hexdigest()
    )
    assert (
        module.definition_sha256(stored_definition)
        != hashlib.sha256((stored_definition + "\n").encode("utf-8")).hexdigest()
    )


def _legacy_evidence() -> dict[str, Any]:
    return {
        "phase": "5E-2C2",
        "status": "BLOCKED",
        "reason": "runner_definition_hash_verifier_added_a_second_newline",
        "live_workflow_post_count": 1,
        "new_codex_job_count": 2,
        "new_codex_thread_count": 2,
        "maximum_active_codex_jobs": 1,
        "workflow": {
            "final_texts": ["4", "5"],
            "workflow_id": "legacy-workflow-id",
            "run_id": "legacy-run-id",
        },
        "jobs": [
            {
                "job_id": "legacy-job-1",
                "thread_id": "legacy-thread-1",
                "turn_id": "legacy-turn-1",
                "state": "SUCCEEDED",
            },
            {
                "job_id": "legacy-job-2",
                "thread_id": "legacy-thread-2",
                "turn_id": "legacy-turn-2",
                "state": "SUCCEEDED",
            },
        ],
        "checks": {
            "same_id_replay": {"status": "NOT_RUN"},
            "bridge_restart_readback": {"status": "NOT_RUN"},
            "console_qt_get_readback": {"status": "NOT_RUN"},
            "expected_final_texts": {"status": "PASS"},
            "immutable_hashes_and_ledgers": {"status": "PASS"},
            "workflow_api_identity_and_jobs": {"status": "PASS"},
            "active_job_concurrency": {"status": "PASS"},
        },
    }


def _install_evidence_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    module = _load_runner()
    assert module is not None
    previous_path = tmp_path / "phase5e-2c2-evidence.json"
    retest_path = tmp_path / "phase5e-2c2-retest-evidence.json"
    monkeypatch.setattr(module, "EVIDENCE_PATH", previous_path)
    monkeypatch.setattr(module, "RETEST_EVIDENCE_PATH", retest_path)
    monkeypatch.setattr(
        module,
        "DIAGNOSTIC_RETEST_EVIDENCE_PATH",
        tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json",
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "R3_EVIDENCE_PATH",
        tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json",
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "R4_EVIDENCE_PATH",
        tmp_path / "phase5e-2c2-r4-evidence.json",
        raising=False,
    )
    return previous_path, retest_path


def _legacy_r2_evidence(previous_sha256: str) -> dict[str, Any]:
    checks = {
        "native_windows": "PASS",
        "dedicated_loopback_ports": "PASS",
        "isolated_environment_and_roots": "PASS",
        "bridge_preflight_and_authentication": "PASS",
        "workspace_allowed_root_and_cwd": "NOT_RUN",
        "single_live_workflow_post": "FAIL",
        "workflow_api_identity_and_jobs": "NOT_RUN",
        "immutable_hashes_and_ledgers": "NOT_RUN",
        "expected_final_texts": "NOT_RUN",
        "active_job_concurrency": "NOT_RUN",
        "console_qt_get_readback": "NOT_RUN",
        "same_id_replay": "NOT_RUN",
        "bridge_restart_readback": "NOT_RUN",
        "production_side_effects": "PASS",
    }
    return {
        "phase": "5E-2C2-R",
        "status": "FAIL",
        "reason": "initial_workflow_post_was_not_accepted",
        "retry_of": "5E-2C2",
        "retest_reason": "previous_runner_hash_validation_false_positive",
        "previous_evidence_path": "docs/phase5e-2c2-evidence.json",
        "previous_evidence_sha256": previous_sha256,
        "live_workflow_post_attempted": True,
        "live_workflow_post_count": 1,
        "same_id_replay_post_attempted": False,
        "same_id_replay_post_count": 0,
        "new_codex_job_count": 0,
        "new_codex_thread_count": None,
        "maximum_active_codex_jobs": None,
        "production_side_effect_count": 0,
        "workflow": {"initial_post_http_status": 409},
        "checks": {name: {"status": status} for name, status in checks.items()},
        "cleanup": {"status": "PRESERVED_ACTIVE_OR_UNKNOWN", "forced_termination": False},
    }


def _r3_reconciliation_evidence(
    original_sha256: str,
    r2_sha256: str,
) -> dict[str, Any]:
    return {
        "phase": "5E-2C2-R3",
        "status": "BLOCKED",
        "reason": "workflow_api_requires_reconciliation",
        "live_workflow_post_attempted": True,
        "live_workflow_post_count": 1,
        "same_id_replay_post_attempted": False,
        "same_id_replay_post_count": 0,
        "new_codex_job_count": 1,
        "workflow": {
            "workflow_id": "123e4567-e89b-12d3-a456-426614174000",
            "run_id": "run-0123456789abcdef0123456789abcdef",
            "accepted_status": 202,
        },
        "previous_evidence": {
            "original": {
                "path": "docs/phase5e-2c2-evidence.json",
                "sha256": original_sha256,
            },
            "r2": {
                "path": "docs/phase5e-2c2-retest-evidence.json",
                "sha256": r2_sha256,
            },
        },
        "cleanup": {
            "status": "PRESERVED_ACTIVE_OR_UNKNOWN",
            "forced_termination": False,
        },
    }


def _install_valid_r4_evidence_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[ModuleType, dict[str, Path], dict[str, str], dict[str, bytes]]:
    module = _load_runner()
    assert module is not None
    original_path, r2_path = _install_evidence_paths(monkeypatch, tmp_path)
    r3_path = tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json"
    r4_path = tmp_path / "phase5e-2c2-r4-evidence.json"
    original_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    original_sha256, normalized_original_sha256 = module._validated_previous_evidence_hashes(
        original_path
    )
    r2_path.write_text(
        json.dumps(_legacy_r2_evidence(normalized_original_sha256)),
        encoding="utf-8",
    )
    r2_sha256 = hashlib.sha256(r2_path.read_bytes()).hexdigest()
    r3_path.write_text(
        json.dumps(_r3_reconciliation_evidence(original_sha256, r2_sha256)),
        encoding="utf-8",
    )
    paths = {
        "original": original_path,
        "r2": r2_path,
        "r3": r3_path,
        "r4": r4_path,
    }
    hashes = {
        "original": original_sha256,
        "r2": r2_sha256,
        "r3": hashlib.sha256(r3_path.read_bytes()).hexdigest(),
    }
    original_bytes = {name: path.read_bytes() for name, path in paths.items() if name != "r4"}
    return module, paths, hashes, original_bytes


def _startup_diagnostic_document(
    *,
    workflow_id: str = "123e4567-e89b-12d3-a456-426614174000",
    run_id: str = "run-0123456789abcdef0123456789abcdef",
) -> dict[str, Any]:
    return {
        "version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stages": [
            {
                "stage": "worker_config_write",
                "phase": "started",
                "at_utc": "2026-11-12T01:02:03.456+00:00",
            }
        ],
        "failure": {
            "stage": "worker_config_write",
            "exception_class": "RuntimeError",
            "reason": "exception",
            "at_utc": "2026-11-12T01:02:03.457+00:00",
            "worker_config_write_completed": False,
            "worker_spawned": False,
        },
    }


class _FakeBridgeProcess:
    def __init__(self, stderr: Any) -> None:
        self.stderr = stderr


def _start_fake_bridge(
    module: ModuleType,
    stderr: Any,
) -> tuple[Any, Any]:
    process = _FakeBridgeProcess(stderr)
    collector = module._StderrDiagnosticCollector()
    collector.start_reader()
    collector.attach(stderr)
    return process, collector


def test_checked_in_previous_evidence_matches_the_retest_baseline() -> None:
    module = _load_runner()
    assert module is not None

    assert (
        module._validated_previous_evidence_hashes(module.EVIDENCE_PATH)[1]
        == "5eb3e3bb66471bd1e7d18e83ce09e1dab8774752fc3ba4936787eb9bdea82844"
    )


def test_checked_in_diagnostic_retest_baselines_are_valid_and_read_only() -> None:
    module = _load_runner()
    assert module is not None
    original_bytes = module.EVIDENCE_PATH.read_bytes()
    r2_bytes = module.RETEST_EVIDENCE_PATH.read_bytes()

    original_sha256, normalized_original_sha256 = module._validated_previous_evidence_hashes(
        module.EVIDENCE_PATH
    )
    r2_sha256 = module._validated_retest_evidence(
        module.RETEST_EVIDENCE_PATH,
        original_evidence_sha256=normalized_original_sha256,
    )

    assert len(original_sha256) == 64
    assert len(r2_sha256) == 64
    assert module.EVIDENCE_PATH.read_bytes() == original_bytes
    assert module.RETEST_EVIDENCE_PATH.read_bytes() == r2_bytes


def test_diagnostic_retest_selection_records_both_baseline_hashes_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, r2_path = _install_evidence_paths(monkeypatch, tmp_path)
    previous_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    original_sha256, normalized_original_sha256 = module._validated_previous_evidence_hashes(
        previous_path
    )
    r2_path.write_text(
        json.dumps(_legacy_r2_evidence(normalized_original_sha256)),
        encoding="utf-8",
    )
    previous_bytes = previous_path.read_bytes()
    r2_bytes = r2_path.read_bytes()

    evidence_path, baseline_hashes = module._select_diagnostic_retest_evidence()

    assert evidence_path == tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json"
    assert baseline_hashes == {
        "original": original_sha256,
        "r2": hashlib.sha256(r2_bytes).hexdigest(),
    }
    assert previous_path.read_bytes() == previous_bytes
    assert r2_path.read_bytes() == r2_bytes


@pytest.mark.parametrize("mutation", ["previous_hash", "post_count"])
def test_diagnostic_retest_rejects_an_unexpected_r2_baseline_read_only(
    tmp_path: Path,
    mutation: str,
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path = tmp_path / "phase5e-2c2-evidence.json"
    r2_path = tmp_path / "phase5e-2c2-retest-evidence.json"
    previous_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    _raw_original_sha256, normalized_original_sha256 = module._validated_previous_evidence_hashes(
        previous_path
    )
    r2_evidence = _legacy_r2_evidence(normalized_original_sha256)
    if mutation == "previous_hash":
        r2_evidence["previous_evidence_sha256"] = "0" * 64
    else:
        r2_evidence["live_workflow_post_count"] = 2
    r2_path.write_text(json.dumps(r2_evidence), encoding="utf-8")
    previous_bytes = previous_path.read_bytes()
    r2_bytes = r2_path.read_bytes()

    with pytest.raises(module.SmokeAbort) as error:
        module._validated_retest_evidence(
            r2_path,
            original_evidence_sha256=normalized_original_sha256,
        )

    assert error.value.code == "retest_evidence_unexpected"
    assert previous_path.read_bytes() == previous_bytes
    assert r2_path.read_bytes() == r2_bytes


def test_r4_selects_and_hashes_the_valid_three_generation_chain_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, paths, expected_hashes, previous_bytes = _install_valid_r4_evidence_chain(
        monkeypatch, tmp_path
    )

    evidence_path, hashes = module._select_r4_evidence()

    assert evidence_path == paths["r4"]
    assert hashes == expected_hashes
    assert json.loads(paths["r3"].read_text(encoding="utf-8"))["new_codex_job_count"] == 1
    for name, original in previous_bytes.items():
        assert paths[name].read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        "phase",
        "status",
        "reason",
        "post_attempted",
        "post_count",
        "boolean_post_count",
        "accepted_status",
        "workflow_id",
        "run_id",
        "replay",
        "replay_count",
        "cleanup_status",
        "forced_termination",
        "original_path",
        "original_sha256",
        "r2_path",
        "r2_sha256",
    ],
)
def test_r4_rejects_r3_evidence_with_invalid_identity_or_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    module, paths, _hashes, previous_bytes = _install_valid_r4_evidence_chain(monkeypatch, tmp_path)
    r3 = json.loads(paths["r3"].read_text(encoding="utf-8"))
    if mutation == "phase":
        r3["phase"] = "5E-2C2-R2"
    elif mutation == "status":
        r3["status"] = "PASS"
    elif mutation == "reason":
        r3["reason"] = "unexpected_reason"
    elif mutation == "post_attempted":
        r3["live_workflow_post_attempted"] = False
    elif mutation == "post_count":
        r3["live_workflow_post_count"] = 2
    elif mutation == "boolean_post_count":
        r3["live_workflow_post_count"] = True
    elif mutation == "accepted_status":
        r3["workflow"]["accepted_status"] = 409
    elif mutation == "workflow_id":
        r3["workflow"]["workflow_id"] = "invalid-workflow-id"
    elif mutation == "run_id":
        r3["workflow"]["run_id"] = "invalid-run-id"
    elif mutation == "replay":
        r3["same_id_replay_post_attempted"] = True
    elif mutation == "replay_count":
        r3["same_id_replay_post_count"] = 1
    elif mutation == "cleanup_status":
        r3["cleanup"]["status"] = "CLEANED"
    elif mutation == "forced_termination":
        r3["cleanup"]["forced_termination"] = True
    elif mutation == "original_path":
        r3["previous_evidence"]["original"]["path"] = "other.json"
    elif mutation == "original_sha256":
        r3["previous_evidence"]["original"]["sha256"] = "0" * 64
    elif mutation == "r2_path":
        r3["previous_evidence"]["r2"]["path"] = "other.json"
    else:
        r3["previous_evidence"]["r2"]["sha256"] = "0" * 64
    paths["r3"].write_text(json.dumps(r3), encoding="utf-8")
    mutated_r3_bytes = paths["r3"].read_bytes()

    with pytest.raises(module.SmokeAbort) as error:
        module._select_r4_evidence()

    assert error.value.code == "r3_evidence_unexpected"
    assert paths["r3"].read_bytes() == mutated_r3_bytes
    for name in ("original", "r2"):
        original = previous_bytes[name]
        assert paths[name].read_bytes() == original
    assert not paths["r4"].exists()


@pytest.mark.parametrize(
    "invalid_document",
    [
        pytest.param(b"{", id="malformed-json"),
        pytest.param(b"x" * (64 * 1024 + 1), id="oversized"),
    ],
)
def test_r4_rejects_malformed_or_oversized_r3_evidence_read_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_document: bytes,
) -> None:
    module, paths, _hashes, previous_bytes = _install_valid_r4_evidence_chain(monkeypatch, tmp_path)
    paths["r3"].write_bytes(invalid_document)
    invalid_bytes = paths["r3"].read_bytes()

    with pytest.raises(module.SmokeAbort) as error:
        module._select_r4_evidence()

    assert error.value.code == "r3_evidence_unexpected"
    assert paths["r3"].read_bytes() == invalid_bytes
    for name in ("original", "r2"):
        original = previous_bytes[name]
        assert paths[name].read_bytes() == original


def test_r4_rejects_r3_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, paths, _hashes, _previous_bytes = _install_valid_r4_evidence_chain(
        monkeypatch, tmp_path
    )
    target = tmp_path / "r3-target.json"
    target.write_bytes(paths["r3"].read_bytes())
    paths["r3"].unlink()
    try:
        paths["r3"].symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"file symlink unavailable: {error}")

    with pytest.raises(module.SmokeAbort) as error:
        module._select_r4_evidence()

    assert error.value.code == "r3_evidence_unexpected"
    assert target.is_file()


@pytest.mark.parametrize("existing_mode", ["--retest-once", "--diagnostic-retest-once"])
def test_r4_cli_rejects_combining_existing_execution_modes(
    existing_mode: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    assert module is not None

    with pytest.raises(SystemExit) as error:
        module.main(["--r4-once", existing_mode])

    assert error.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_r4_existing_evidence_blocks_without_reading_or_overwriting_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    assert module is not None
    _install_evidence_paths(monkeypatch, tmp_path)
    evidence_path = tmp_path / "phase5e-2c2-r4-evidence.json"
    original = b"preserve existing R4 evidence"
    evidence_path.write_bytes(original)

    def fail_if_live(_evidence: dict[str, Any]) -> int:
        pytest.fail("existing R4 evidence must block before E2E")

    monkeypatch.setattr(module, "_run_e2e", fail_if_live)

    assert module.main(["--r4-once"]) == 2

    assert evidence_path.read_bytes() == original
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "reason": "r4_evidence_already_exists",
    }


@pytest.mark.parametrize("prompt_state", ["missing", "hash_mismatch"])
def test_r4_prompt_preflight_does_not_claim_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    prompt_state: str,
) -> None:
    module, paths, _hashes, previous_bytes = _install_valid_r4_evidence_chain(monkeypatch, tmp_path)
    run_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_run_e2e", run_calls.append)
    if prompt_state == "missing":
        monkeypatch.delenv(module.SECOND_PROMPT_ENVIRONMENT_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(module.SECOND_PROMPT_ENVIRONMENT_VARIABLE, "unapproved test prompt")

    assert module.main(["--r4-once"]) == 2

    assert not paths["r4"].exists()
    assert run_calls == []
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED"
    assert result["reason"] in {
        "approved_second_prompt_unavailable",
        "second_prompt_hash_mismatch",
    }
    for name, original in previous_bytes.items():
        assert paths[name].read_bytes() == original


def test_r4_first_fixture_preflight_does_not_claim_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, paths, _hashes, _previous_bytes = _install_valid_r4_evidence_chain(
        monkeypatch, tmp_path
    )
    run_calls: list[dict[str, Any]] = []

    def invalid_fixture() -> str:
        raise module.SmokeAbort("BLOCKED", "fixed_first_job_fixture_changed_or_invalid")

    monkeypatch.setattr(module, "_fixed_first_prompt", invalid_fixture)
    monkeypatch.setattr(module, "approved_second_prompt", lambda _environ: "approved fake prompt")
    monkeypatch.setattr(module, "_run_e2e", run_calls.append)

    assert module.main(["--r4-once"]) == 2

    assert not paths["r4"].exists()
    assert run_calls == []
    assert json.loads(capsys.readouterr().out)["reason"] == (
        "fixed_first_job_fixture_changed_or_invalid"
    )


def test_r4_main_records_lineage_and_claims_once_without_live_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, paths, hashes, previous_bytes = _install_valid_r4_evidence_chain(monkeypatch, tmp_path)
    test_prompt = "approved fake prompt"
    monkeypatch.setattr(
        module,
        "APPROVED_SECOND_PROMPT_SHA256",
        hashlib.sha256(test_prompt.encode("utf-8")).hexdigest(),
    )
    monkeypatch.setenv(module.SECOND_PROMPT_ENVIRONMENT_VARIABLE, test_prompt)
    run_calls: list[dict[str, Any]] = []

    def stop_before_live(evidence: dict[str, Any]) -> int:
        run_calls.append(evidence)
        evidence["workflow"].update(
            {"accepted_status": 202, "dbos_status": "SUCCESS", "final_texts": ["4", "5"]}
        )
        evidence["jobs"] = [{"state": "SUCCEEDED"}, {"state": "SUCCEEDED"}]
        evidence["new_codex_job_count"] = 2
        evidence["maximum_active_codex_jobs"] = 1
        raise module.SmokeAbort("BLOCKED", "test_stopped_before_live")

    monkeypatch.setattr(module, "_run_e2e", stop_before_live)

    assert module.main(["--r4-once"]) == 2

    evidence_bytes = paths["r4"].read_bytes()
    evidence = json.loads(evidence_bytes)
    assert evidence["phase"] == "5E-2C2-R4"
    assert evidence["retry_of"] == "5E-2C2-R3"
    assert evidence["retest_reason"] == "verify_transient_status_history_recheck_fix"
    assert evidence["tested_fix_commit"] == "1e8255f1086e7fb5111450f2c5454c8f01f5d008"
    assert evidence["previous_evidence"] == {
        name: {
            "path": paths[name].name.replace("phase5e-2c2-", "docs/phase5e-2c2-"),
            "sha256": hashes[name],
        }
        for name in ("original", "r2", "r3")
    }
    assert evidence["workflow"]["dbos_status"] == "SUCCESS"
    assert evidence["jobs"] == [{"state": "SUCCEEDED"}, {"state": "SUCCEEDED"}]
    assert evidence["new_codex_job_count"] == 2
    assert evidence["maximum_active_codex_jobs"] == 1
    assert evidence["reason"] == "test_stopped_before_live"
    assert test_prompt not in evidence_bytes.decode("utf-8")
    assert len(run_calls) == 1
    for name, original in previous_bytes.items():
        assert paths[name].read_bytes() == original

    monkeypatch.setattr(
        module,
        "_run_e2e",
        lambda _evidence: pytest.fail("existing R4 evidence must block a second run"),
    )
    assert module.main(["--r4-once"]) == 2
    assert paths["r4"].read_bytes() == evidence_bytes
    assert len(run_calls) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["reason"] == (
        "r4_evidence_already_exists"
    )


def test_diagnostic_retest_main_records_hashes_without_running_live_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, r2_path = _install_evidence_paths(monkeypatch, tmp_path)
    previous_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    original_sha256, normalized_original_sha256 = module._validated_previous_evidence_hashes(
        previous_path
    )
    r2_path.write_text(
        json.dumps(_legacy_r2_evidence(normalized_original_sha256)),
        encoding="utf-8",
    )
    previous_bytes = previous_path.read_bytes()
    r2_bytes = r2_path.read_bytes()
    run_calls: list[dict[str, Any]] = []

    def stop_before_live(evidence: dict[str, Any]) -> int:
        run_calls.append(evidence)
        raise module.SmokeAbort("BLOCKED", "test_stopped_before_live")

    monkeypatch.setattr(module, "_run_e2e", stop_before_live)

    assert module.main(["--diagnostic-retest-once"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "phase": "5E-2C2-R3",
        "status": "BLOCKED",
        "reason": "test_stopped_before_live",
    }
    assert len(run_calls) == 1
    evidence_path = tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["previous_evidence"]["original"]["sha256"] == original_sha256
    assert evidence["previous_evidence"]["r2"]["sha256"] == hashlib.sha256(r2_bytes).hexdigest()
    assert previous_path.read_bytes() == previous_bytes
    assert r2_path.read_bytes() == r2_bytes


def test_diagnostic_retest_main_blocks_existing_evidence_before_e2e(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    assert module is not None
    _previous_path, _r2_path = _install_evidence_paths(monkeypatch, tmp_path)
    evidence_path = tmp_path / "phase5e-2c2-diagnostic-retest-evidence.json"
    original = b"preserve existing R3 evidence"
    evidence_path.write_bytes(original)

    def fail_if_live(_evidence: dict[str, Any]) -> int:
        pytest.fail("an existing R3 evidence file must block before E2E")

    monkeypatch.setattr(module, "_run_e2e", fail_if_live)

    assert module.main(["--diagnostic-retest-once"]) == 2

    assert evidence_path.read_bytes() == original
    assert json.loads(capsys.readouterr().out) == {
        "status": "BLOCKED",
        "reason": "diagnostic_retest_evidence_already_exists",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "phase",
        "status",
        "reason",
        "post_count",
        "boolean_post_count",
        "job_count",
        "job_row_shape",
        "final_texts",
        "replay",
        "restart",
        "console",
    ],
)
def test_retest_rejects_an_unexpected_previous_evidence(
    mutation: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, _retest_path = _install_evidence_paths(monkeypatch, tmp_path)
    legacy = _legacy_evidence()
    if mutation == "phase":
        legacy["phase"] = "5E-2C1"
    elif mutation == "status":
        legacy["status"] = "PASS"
    elif mutation == "reason":
        legacy["reason"] = "different_failure"
    elif mutation == "post_count":
        legacy["live_workflow_post_count"] = 2
    elif mutation == "boolean_post_count":
        legacy["live_workflow_post_count"] = True
    elif mutation == "job_count":
        legacy["new_codex_job_count"] = 3
    elif mutation == "job_row_shape":
        legacy["jobs"][1] = "invalid"
    elif mutation == "final_texts":
        legacy["workflow"]["final_texts"] = ["4", "6"]
    elif mutation == "replay":
        legacy["checks"]["same_id_replay"]["status"] = "PASS"
    elif mutation == "restart":
        legacy["checks"]["bridge_restart_readback"]["status"] = "PASS"
    else:
        legacy["checks"]["console_qt_get_readback"]["status"] = "PASS"
    previous_path.write_text(json.dumps(legacy), encoding="utf-8")

    with pytest.raises(module.SmokeAbort) as error:
        module._select_evidence_paths(retest_once=True)

    assert error.value.code == "previous_evidence_unexpected"


def test_retest_rejects_an_already_claimed_retest_evidence_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, retest_path = _install_evidence_paths(monkeypatch, tmp_path)
    previous_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    retest_path.write_bytes(b"existing evidence")
    original = retest_path.read_bytes()

    with pytest.raises(module.SmokeAbort) as error:
        module._select_evidence_paths(retest_once=True)

    assert error.value.code == "retest_evidence_already_exists"
    assert retest_path.read_bytes() == original


def test_default_entry_claims_the_legacy_path_and_does_not_run_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, _retest_path = _install_evidence_paths(monkeypatch, tmp_path)
    original = b"preserve legacy evidence exactly"
    previous_path.write_bytes(original)

    def fail_if_live(_evidence: dict[str, Any]) -> int:
        pytest.fail("default entry must not run the live E2E")

    monkeypatch.setattr(module, "_run_e2e", fail_if_live)
    assert module.main([]) == 2

    assert previous_path.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["reason"] == "evidence_already_exists"


def test_retest_flag_selects_new_record_with_baseline_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_runner()
    assert module is not None
    previous_path, retest_path = _install_evidence_paths(monkeypatch, tmp_path)
    previous_path.write_text(json.dumps(_legacy_evidence()), encoding="utf-8")
    previous_bytes = previous_path.read_bytes()
    calls: list[dict[str, Any]] = []

    def stop_before_live(evidence: dict[str, Any]) -> int:
        calls.append(evidence)
        raise module.SmokeAbort("BLOCKED", "test_stopped_before_live")

    monkeypatch.setattr(module, "_run_e2e", stop_before_live)
    assert module.main(["--retest-once"]) == 2

    record = json.loads(retest_path.read_text(encoding="utf-8"))
    assert record["phase"] == "5E-2C2-R"
    assert record["retry_of"] == "5E-2C2"
    assert record["retest_reason"] == "previous_runner_hash_validation_false_positive"
    assert record["previous_evidence_sha256"] == hashlib.sha256(previous_bytes).hexdigest()
    assert previous_path.read_bytes() == previous_bytes
    assert calls == [record]
    assert "test_stopped_before_live" in capsys.readouterr().out


def test_retest_cli_does_not_accept_an_arbitrary_evidence_path() -> None:
    module = _load_runner()
    assert module is not None

    with pytest.raises(SystemExit) as error:
        module.main(["--evidence", "elsewhere.json"])

    assert error.value.code == 2


def test_bridge_stderr_reader_extracts_only_valid_diagnostic_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    assert module is not None
    valid = (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=worker_config_write original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    )
    invalid_stage = valid.replace("stage=worker_config_write", "stage=untrusted_stage")
    invalid_exception = valid.replace(
        "original_exception=RuntimeError", "original_exception=token=private"
    )
    invalid_persistence = valid.replace(
        "persistence_exception=OSError", "persistence_exception=none"
    )
    private_stderr = (
        "private prompt and api_key=synthetic-secret C:\\private\\file.txt\n"
        + invalid_stage
        + invalid_exception
        + invalid_persistence
        + valid
    ).encode("ascii")

    process, collector = _start_fake_bridge(module, io.BytesIO(private_stderr))

    assert process.stderr is not None
    assert collector.wait(timeout=2.0)
    captured = collector.snapshot()
    assert captured["read_status"] == "EOF"
    assert captured["events"] == [
        {
            "stage": "worker_config_write",
            "original_exception": "RuntimeError",
            "persistence_exception": "OSError",
        }
    ]
    encoded = json.dumps(captured)
    assert "private prompt" not in encoded
    assert "synthetic-secret" not in encoded
    assert "C:\\private" not in encoded
    assert "token=private" not in encoded


def test_bridge_stderr_reader_bounds_lines_and_event_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    assert module is not None
    valid = (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=worker_config_write original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    ).encode("ascii")
    noisy_stderr = b"x" * (module.MAX_STDERR_DIAGNOSTIC_LINE_BYTES + 10_000)
    noisy_stderr += b"\n" + valid * (module.MAX_STDERR_DIAGNOSTIC_EVENTS + 4)

    _process, collector = _start_fake_bridge(module, io.BytesIO(noisy_stderr))

    assert collector.wait(timeout=2.0)
    captured = collector.snapshot()
    assert len(captured["events"]) == module.MAX_STDERR_DIAGNOSTIC_EVENTS
    assert captured["events_truncated"] is True
    assert captured["long_line_discarded"] is True
    assert len(json.dumps(captured).encode("utf-8")) <= module.MAX_DIAGNOSTIC_EVIDENCE_BYTES


def test_bridge_stderr_reader_uses_available_pipe_chunks_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    assert module is not None
    event = (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=worker_config_write original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    ).encode("ascii")

    class _Read1Stderr:
        def __init__(self) -> None:
            self.calls = 0

        def read1(self, size: int) -> bytes:
            assert size == module.STDERR_READ_CHUNK_BYTES
            self.calls += 1
            return event if self.calls == 1 else b""

        def read(self, _size: int) -> bytes:
            pytest.fail("pipe reader should prefer read1 when available")

        def close(self) -> None:
            pass

    stderr = _Read1Stderr()
    _process, collector = _start_fake_bridge(module, stderr)

    assert collector.wait(timeout=2.0)
    assert stderr.calls == 2
    assert collector.snapshot()["events"] == [
        {
            "stage": "worker_config_write",
            "original_exception": "RuntimeError",
            "persistence_exception": "OSError",
        }
    ]


def test_bridge_stderr_reader_records_read_failure_without_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    assert module is not None

    class _FailingStderr:
        def read(self, _size: int) -> bytes:
            raise OSError("private path and token=synthetic-secret")

        def close(self) -> None:
            pass

    _process, collector = _start_fake_bridge(module, _FailingStderr())

    assert collector.wait(timeout=2.0)
    captured = collector.snapshot()
    assert captured["read_status"] == "READ_FAILED"
    assert captured["read_error_class"] == "OSError"
    assert captured["events"] == []
    assert "private path" not in json.dumps(captured)
    assert "synthetic-secret" not in json.dumps(captured)


def test_startup_diagnostic_json_is_validated_and_allowlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    workflow_id = "123e4567-e89b-12d3-a456-426614174000"
    run_id = "run-0123456789abcdef0123456789abcdef"
    request_id = "phase5e2c2-diagnostic-test"
    state_root = tmp_path / "state"
    run_dir = state_root / run_id
    run_dir.mkdir(parents=True)
    diagnostic = _startup_diagnostic_document(workflow_id=workflow_id, run_id=run_id)
    diagnostic["unsafe_message"] = "api_key=synthetic-secret private prompt"
    (run_dir / "startup-diagnostic.json").write_text(
        json.dumps({key: value for key, value in diagnostic.items() if key != "unsafe_message"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_read_request_ledger",
        lambda _root: [
            {"client_request_id": request_id, "workflow_id": workflow_id, "run_id": run_id}
        ],
    )
    monkeypatch.setattr(module, "_run_directory", lambda _root, _run_id: run_dir)

    result = module._read_startup_diagnostic(state_root, request_id)

    assert result["status"] == "VALID"
    assert result["workflow_id"] == workflow_id
    assert result["run_id"] == run_id
    assert result["failure"]["stage"] == "worker_config_write"
    assert result["failure"]["exception_class"] == "RuntimeError"
    assert "unsafe_message" not in json.dumps(result)
    assert "synthetic-secret" not in json.dumps(result)

    (run_dir / "startup-diagnostic.json").write_text(json.dumps(diagnostic), encoding="utf-8")
    rejected = module._read_startup_diagnostic(state_root, request_id)
    assert rejected["status"] == "INVALID"
    assert "unsafe_message" not in json.dumps(rejected)
    assert "synthetic-secret" not in json.dumps(rejected)


@pytest.mark.parametrize(
    "invalid_kind",
    ["truncated_json", "oversized", "stage", "exception_class", "identity", "run_identity"],
)
def test_startup_diagnostic_json_rejects_invalid_or_mismatched_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    module = _load_runner()
    assert module is not None
    workflow_id = "123e4567-e89b-12d3-a456-426614174000"
    run_id = "run-0123456789abcdef0123456789abcdef"
    request_id = "phase5e2c2-diagnostic-invalid"
    state_root = tmp_path / "state"
    run_dir = state_root / run_id
    run_dir.mkdir(parents=True)
    diagnostic_path = run_dir / "startup-diagnostic.json"
    if invalid_kind == "truncated_json":
        diagnostic_path.write_text('{"version":1,"failure":', encoding="utf-8")
    elif invalid_kind == "oversized":
        diagnostic_path.write_bytes(b"x" * (module.MAX_STARTUP_DIAGNOSTIC_BYTES + 1))
    else:
        diagnostic = _startup_diagnostic_document(
            workflow_id=workflow_id,
            run_id=run_id,
        )
        if invalid_kind == "stage":
            diagnostic["failure"]["stage"] = "untrusted_stage"
        elif invalid_kind == "exception_class":
            diagnostic["failure"]["exception_class"] = "token=private"
        elif invalid_kind == "identity":
            diagnostic["workflow_id"] = "123e4567-e89b-12d3-a456-426614174001"
        else:
            diagnostic["run_id"] = "run-abcdef0123456789abcdef0123456789"
        diagnostic_path.write_text(json.dumps(diagnostic), encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_read_request_ledger",
        lambda _root: [
            {"client_request_id": request_id, "workflow_id": workflow_id, "run_id": run_id}
        ],
    )
    monkeypatch.setattr(module, "_run_directory", lambda _root, _run_id: run_dir)

    result = module._read_startup_diagnostic(state_root, request_id)

    assert result["status"] == "INVALID"
    assert "reason" in result
    assert "token=private" not in json.dumps(result)


def test_missing_startup_diagnostic_and_stderr_are_reported_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    workflow_id = "123e4567-e89b-12d3-a456-426614174000"
    run_id = "run-0123456789abcdef0123456789abcdef"
    request_id = "phase5e2c2-diagnostic-missing"
    state_root = tmp_path / "state"
    run_dir = state_root / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        module,
        "_read_request_ledger",
        lambda _root: [
            {"client_request_id": request_id, "workflow_id": workflow_id, "run_id": run_id}
        ],
    )
    monkeypatch.setattr(module, "_run_directory", lambda _root, _run_id: run_dir)
    _process, collector = _start_fake_bridge(module, io.BytesIO(b"private stderr is discarded\n"))
    assert collector.wait(timeout=2.0)

    result = module._collect_startup_diagnostics(
        state_root=state_root,
        client_request_id=request_id,
        stderr_collector=collector,
    )

    assert result["file"]["status"] == "NOT_FOUND"
    assert result["stderr"]["read_status"] == "EOF"
    assert result["stderr"]["events"] == []
    assert result["diagnostic_obtained"] is False
    assert result["diagnostics_unavailable"] is True
    assert "private stderr" not in json.dumps(result)


def test_missing_startup_diagnostic_uses_valid_stderr_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    workflow_id = "123e4567-e89b-12d3-a456-426614174000"
    run_id = "run-0123456789abcdef0123456789abcdef"
    request_id = "phase5e2c2-stderr-diagnostic-fallback"
    state_root = tmp_path / "state"
    run_dir = state_root / run_id
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        module,
        "_read_request_ledger",
        lambda _root: [
            {"client_request_id": request_id, "workflow_id": workflow_id, "run_id": run_id}
        ],
    )
    monkeypatch.setattr(module, "_run_directory", lambda _root, _run_id: run_dir)
    stderr_event = (
        "workflow.startup_diagnostic.persistence_failed "
        "stage=worker_spawn original_exception=RuntimeError "
        "persistence_exception=OSError\n"
    )
    _process, collector = _start_fake_bridge(module, io.BytesIO(stderr_event.encode("ascii")))
    assert collector.wait(timeout=2.0)

    result = module._collect_startup_diagnostics(
        state_root=state_root,
        client_request_id=request_id,
        stderr_collector=collector,
    )

    assert result["file"]["status"] == "NOT_FOUND"
    assert result["stderr"]["events"] == [
        {
            "stage": "worker_spawn",
            "original_exception": "RuntimeError",
            "persistence_exception": "OSError",
        }
    ]
    assert result["diagnostic_obtained"] is True
    assert result["diagnostics_unavailable"] is False
    assert len(json.dumps(result).encode("utf-8")) <= module.MAX_DIAGNOSTIC_EVIDENCE_BYTES


def test_production_roots_and_ports_are_excluded() -> None:
    module = _load_runner()
    assert module is not None

    assert not module.roots_are_separate(Path("D:/CodexBridge/workflows"), Path("D:/CodexBridge"))
    assert not module.roots_are_separate(
        module.REPOSITORY_ROOT / "poc/ows/.state/runs", module.REPOSITORY_ROOT / "poc/ows/.state"
    )
    assert not module.ports_are_safe(18000, 58886)
    assert not module.ports_are_safe(58885, 18001)
    assert not module.ports_are_safe(58885, 58885)
    assert module.ports_are_safe(58885, 58886)


@pytest.mark.parametrize("sensitive_kind", ["token", "prompt", "temp_root"])
def test_sensitive_evidence_material_is_rejected(sensitive_kind: str, tmp_path: Path) -> None:
    module = _load_runner()
    assert module is not None
    token = "synthetic-control-token"
    prompt = "synthetic private prompt"
    evidence: dict[str, Any] = {"phase": "5E-2C2-R"}
    if sensitive_kind == "token":
        evidence["leak"] = token
    elif sensitive_kind == "prompt":
        evidence["leak"] = prompt
    else:
        evidence["leak"] = str(tmp_path)

    with pytest.raises(module.SmokeAbort) as error:
        module._no_sensitive_evidence(
            evidence,
            token=token,
            temp_root=tmp_path,
            sensitive_strings=(prompt,),
        )

    assert error.value.code == "evidence_contains_sensitive_or_private_material"


def test_saved_state_verifier_hashes_one_trailing_newline_in_stored_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_runner()
    assert module is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workflow_id = "workflow-id"
    run_id = "run-00000000-0000-0000-0000-000000000001"
    client_request_id = "test-request-id"
    ui_port = 58886
    stored_definition = '{"document":{"name":"fixture"}}\n'
    (run_dir / "workflow.yaml").write_text(stored_definition, encoding="utf-8")
    (run_dir / "input.json").write_text("{}\n", encoding="utf-8")
    input_data: dict[str, Any] = {}
    definition = json.loads(stored_definition)
    workspace_value = str(workspace.resolve(strict=True))
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": workspace_value,
        "max_live_codex_jobs": 2,
    }
    request_digest = hashlib.sha256(module._canonical_json(digest_payload).encode()).hexdigest()
    manifest = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "request_digest": request_digest,
        "workspace_root": workspace_value,
        "definition_sha256": hashlib.sha256(stored_definition.encode()).hexdigest(),
        "input_sha256": hashlib.sha256(module._canonical_json(input_data).encode()).hexdigest(),
        "control": {"max_live_codex_jobs": 2},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0}), encoding="utf-8"
    )
    jobs: list[dict[str, Any]] = []
    for index, (task, final_text) in enumerate(
        (("do[0].first", "4"), ("do[1].second", "5")), start=1
    ):
        request_body = {
            "cwd": workspace_value,
            "prompt": "first" if index == 1 else "prefix4",
            "sandbox_mode": "inherit",
            "confirm_full_access": False,
        }
        request_hash = hashlib.sha256(
            module._canonical_json(
                {"host": module.BRIDGE_HOST, "port": ui_port, "body": request_body}
            ).encode()
        ).hexdigest()
        jobs.append(
            {
                "task_identity": task,
                "job_id": f"job-{index}",
                "thread_id": f"thread-{index}",
                "turn_id": f"turn-{index}",
                "workflow_id": workflow_id,
                "state": "SUCCEEDED",
                "remote_state": "completed",
                "request_json": json.dumps(request_body),
                "request_digest": request_hash,
                "receipt_json": json.dumps({"result": {"final_text": final_text}}),
            }
        )
    events = [
        {"event_id": 1, "job_id": "job-1", "state": "MAYBE_SENT"},
        {"event_id": 2, "job_id": "job-1", "state": "SUCCEEDED"},
        {"event_id": 3, "job_id": "job-2", "state": "MAYBE_SENT"},
        {"event_id": 4, "job_id": "job-2", "state": "SUCCEEDED"},
    ]
    request = {
        "client_request_id": client_request_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "state": "succeeded",
        "request_digest": request_digest,
    }
    monkeypatch.setattr(module, "_read_request_ledger", lambda _root: [request])
    monkeypatch.setattr(module, "_run_directory", lambda _root, _run: run_dir)
    monkeypatch.setattr(module, "_read_job_ledger", lambda _run_dir: (jobs, events))
    monkeypatch.setattr(module, "_read_dbos_status", lambda *_args: "SUCCESS")

    saved = module._verify_saved_state(
        state_root=tmp_path / "state",
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
        workspace_root=workspace,
        ui_port=ui_port,
        expected_first_prompt="first",
        expected_second_prompt="prefix",
    )

    assert saved["final_texts"] == ["4", "5"]
