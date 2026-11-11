from __future__ import annotations

import hashlib
import importlib
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
    return previous_path, retest_path


def test_checked_in_previous_evidence_matches_the_retest_baseline() -> None:
    module = _load_runner()
    assert module is not None

    assert (
        module._validated_previous_evidence(module.EVIDENCE_PATH)
        == "5eb3e3bb66471bd1e7d18e83ce09e1dab8774752fc3ba4936787eb9bdea82844"
    )


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
