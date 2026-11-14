from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "phase5f5_integrated_isolated_smoke.py"
SINGLE_JOB_FIXTURE = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"


def _load_runner() -> ModuleType:
    assert SCRIPT_PATH.is_file(), "Phase 5F-5 integrated runner is missing"
    spec = importlib.util.spec_from_file_location("phase5f5_integrated_isolated_smoke", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_item_input_keeps_the_approved_dispatch_order_and_capacity() -> None:
    runner = _load_runner()

    assert runner.build_item_input() == {
        "items": [
            {"id": "alpha", "enabled": True, "prompt": "Reply with exactly ALPHA"},
            {"id": "skip", "enabled": False, "prompt": "Reply with exactly SKIP"},
            {"id": "gamma", "enabled": True, "prompt": "Reply with exactly GAMMA"},
        ]
    }
    assert runner.ITEM_MAX_LIVE_CODEX_JOBS == 3


def test_single_job_template_uses_the_approved_bravo_prompt() -> None:
    runner = _load_runner()

    workflow_yaml = runner.build_single_job_workflow(SINGLE_JOB_FIXTURE.read_text(encoding="utf-8"))
    definition = yaml.safe_load(workflow_yaml)

    assert definition["do"][0]["calculate"]["with"]["prompt"] == "Reply with exactly BRAVO"
    assert len(definition["do"]) == 2


def test_repository_workflow_fixtures_pass_the_runner_preflight() -> None:
    runner = _load_runner()

    item_yaml, item_definition, single_yaml, single_definition = runner._validate_templates()

    assert yaml.safe_load(item_yaml) == item_definition
    assert yaml.safe_load(single_yaml) == single_definition
    assert item_definition["do"][0]["processItems"]
    assert single_definition["do"][0]["calculate"]["with"]["prompt"] == ("Reply with exactly BRAVO")


def test_runner_item_task_identities_match_the_workflow_service_contract() -> None:
    from codex_bridge.workflows.service import _three_job_task_identities

    runner = _load_runner()
    _item_yaml, item_definition, _single_yaml, _single_definition = runner._validate_templates()
    expected = _three_job_task_identities(item_definition, runner.build_item_input())

    assert (
        runner.ITEM_TASK_IDENTITIES
        == expected
        == (
            "do[0].processItems.iteration[0].do[1].processItem",
            "do[0].processItems.iteration[2].do[1].processItem",
        )
    )


def test_git_preflight_accepts_base_ancestor_of_head(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner()
    head = "8e90588d1cf09d0a8a832fdd9cf70f7ef5e7ccf7"
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{head}\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._verified_base_ancestor_head() == head
    assert calls == [
        ["git", "rev-parse", "HEAD"],
        ["git", "merge-base", "--is-ancestor", runner.BASE_COMMIT, head],
    ]


def test_git_preflight_rejects_unrelated_base_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner()

    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        if command == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="f" * 40)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    with pytest.raises(runner.SmokeAbort) as error:
        runner._verified_base_ancestor_head()
    assert error.value.code == "phase5f4_base_commit_not_ancestor_of_head"


def test_running_request_row_can_project_to_confirmed_success() -> None:
    runner = _load_runner()
    workflow_id = "9cf8fa22-19f7-42e2-a5d1-49f839973820"
    definition_sha256 = "a" * 64
    input_sha256 = "b" * 64
    jobs = [{"state": "SUCCEEDED"}, {"state": "SUCCEEDED"}]
    stored_result = {
        "workflow_id": workflow_id,
        "definition_sha256": definition_sha256,
        "input_sha256": input_sha256,
        "result": {"results": ["ALPHA", "GAMMA"]},
    }

    runner._validate_confirmed_durable_success(
        request_state="running",
        dbos_status="SUCCESS",
        worker_status={"state": "completed", "exit_code": 0},
        jobs=jobs,
        expected_job_count=2,
        receipts_verified=True,
        stored_result=stored_result,
        workflow_id=workflow_id,
        definition_sha256=definition_sha256,
        input_sha256=input_sha256,
    )

    with pytest.raises(runner.SmokeAbort):
        runner._validate_confirmed_durable_success(
            request_state="running",
            dbos_status="SUCCESS",
            worker_status={"state": "completed", "exit_code": 0},
            jobs=jobs,
            expected_job_count=2,
            receipts_verified=False,
            stored_result=stored_result,
            workflow_id=workflow_id,
            definition_sha256=definition_sha256,
            input_sha256=input_sha256,
        )


def test_attempt_selector_uses_fixed_paths_and_preserves_prior_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    r3_path = tmp_path / "phase5f5-integrated-acceptance-r3-evidence.json"
    r4_path = tmp_path / "phase5f5-integrated-acceptance-r4-evidence.json"
    r5_path = tmp_path / "phase5f5-integrated-acceptance-r5-evidence.json"
    monkeypatch.setattr(runner, "R3_EVIDENCE_PATH", r3_path, raising=False)
    monkeypatch.setattr(runner, "R4_EVIDENCE_PATH", r4_path, raising=False)
    monkeypatch.setattr(runner, "R5_EVIDENCE_PATH", r5_path, raising=False)

    assert runner._evidence_path_for_attempt(None) == runner.EVIDENCE_PATH
    assert runner._evidence_path_for_attempt("r2") == runner.R2_EVIDENCE_PATH
    assert runner._evidence_path_for_attempt("r3") == r3_path
    assert runner._evidence_path_for_attempt("r4") == r4_path
    assert runner._evidence_path_for_attempt("r5") == r5_path
    with pytest.raises(ValueError):
        runner._evidence_path_for_attempt("r6")

    occupied = tmp_path / "occupied-evidence.json"
    occupied.write_text("preserve", encoding="utf-8")
    with pytest.raises(runner.SmokeAbort):
        runner._EvidenceWriter(occupied, {})
    assert occupied.read_text(encoding="utf-8") == "preserve"

    r3_path.write_text("preserve-r3", encoding="utf-8")
    with pytest.raises(runner.SmokeAbort):
        runner._EvidenceWriter(runner._evidence_path_for_attempt("r3"), {})
    assert r3_path.read_text(encoding="utf-8") == "preserve-r3"

    r4_path.write_text("preserve-r4", encoding="utf-8")
    with pytest.raises(runner.SmokeAbort):
        runner._EvidenceWriter(runner._evidence_path_for_attempt("r4"), {})
    assert r4_path.read_text(encoding="utf-8") == "preserve-r4"

    r5_path.write_text("preserve-r5", encoding="utf-8")
    with pytest.raises(runner.SmokeAbort):
        runner._EvidenceWriter(runner._evidence_path_for_attempt("r5"), {})
    assert r5_path.read_text(encoding="utf-8") == "preserve-r5"


def test_cli_accepts_r4_and_selects_the_fixed_evidence_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    evidence_path = tmp_path / "docs" / "phase5f5-integrated-acceptance-r4-evidence.json"
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "R4_EVIDENCE_PATH", evidence_path, raising=False)
    captured: dict[str, Any] = {}

    class FakeEvidenceWriter:
        def __init__(self, path: Path, _evidence: Any) -> None:
            self.path = path

    def fake_run_acceptance(*, evidence: dict[str, Any], writer: Any) -> int:
        captured["path"] = writer.path
        evidence["status"] = "NOT_RUN"
        return 1

    monkeypatch.setattr(runner, "_EvidenceWriter", FakeEvidenceWriter)
    monkeypatch.setattr(runner, "_run_acceptance", fake_run_acceptance)

    assert runner.main(["--attempt", "r4"]) == 1
    assert captured["path"] == evidence_path


def test_cli_accepts_r5_and_selects_the_fixed_evidence_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    evidence_path = tmp_path / "docs" / "phase5f5-integrated-acceptance-r5-evidence.json"
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "R5_EVIDENCE_PATH", evidence_path, raising=False)
    captured: dict[str, Any] = {}

    class FakeEvidenceWriter:
        def __init__(self, path: Path, _evidence: Any) -> None:
            self.path = path

    def fake_run_acceptance(*, evidence: dict[str, Any], writer: Any) -> int:
        captured["path"] = writer.path
        evidence["status"] = "NOT_RUN"
        return 1

    monkeypatch.setattr(runner, "_EvidenceWriter", FakeEvidenceWriter)
    monkeypatch.setattr(runner, "_run_acceptance", fake_run_acceptance)

    assert runner.main(["--attempt", "r5"]) == 1
    assert captured["path"] == evidence_path


def test_runtime_state_root_uses_localappdata_and_ignores_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    temp_root = tmp_path / "isolated"
    local_app_data = temp_root / "localappdata"
    local_app_data.mkdir(parents=True)
    config_path = temp_root / "bridge.toml"
    config_path.write_text("isolated config", encoding="utf-8")
    environment = {"LOCALAPPDATA": str(local_app_data)}

    def fake_write_runtime_config(**_kwargs: Any) -> tuple[dict[str, str], Path]:
        return environment, config_path

    monkeypatch.setattr(runner, "_write_runtime_config", fake_write_runtime_config)
    runtime_result = runner._write_runtime_config(
        temp_root=temp_root,
        base_environment={},
        token="test-only-token",
        ports=(12345, 12346),
        workspaces=(),
        codex_executable=tmp_path / "codex.exe",
        config_name="bridge.toml",
    )

    prepared_environment, prepared_config, state_root = runner._runtime_paths_from_config_result(
        runtime_result,
        temp_root=temp_root,
    )

    assert prepared_environment == environment
    assert prepared_config == config_path
    assert state_root == (local_app_data / "CodexBridge" / "workflows").resolve()
    assert state_root != config_path
    runner.support._ensure_empty_isolated_state(state_root)


def test_runtime_state_root_rejects_localappdata_outside_temp_root(tmp_path: Path) -> None:
    runner = _load_runner()
    temp_root = tmp_path / "isolated"
    temp_root.mkdir()
    (temp_root / "localappdata").mkdir()
    outside_local_app_data = tmp_path / "outside" / "localappdata"
    outside_local_app_data.mkdir(parents=True)
    config_path = temp_root / "bridge.toml"
    config_path.write_text("isolated config", encoding="utf-8")

    with pytest.raises(runner.SmokeAbort) as error:
        runner._runtime_paths_from_config_result(
            ({"LOCALAPPDATA": str(outside_local_app_data)}, config_path),
            temp_root=temp_root,
        )

    assert error.value.code == "isolated_localappdata_outside_temp_root"


def test_runtime_state_root_may_not_overlap_a_workspace(tmp_path: Path) -> None:
    runner = _load_runner()
    temp_root = tmp_path / "isolated"
    local_app_data = temp_root / "localappdata"
    local_app_data.mkdir(parents=True)
    config_path = temp_root / "bridge.toml"
    config_path.write_text("isolated config", encoding="utf-8")
    _environment, _config_path, state_root = runner._runtime_paths_from_config_result(
        ({"LOCALAPPDATA": str(local_app_data)}, config_path),
        temp_root=temp_root,
    )
    state_root.mkdir(parents=True)
    workspaces = (state_root, temp_root / "workspace-B", temp_root / "workspace-C")
    for workspace in workspaces[1:]:
        workspace.mkdir()

    with pytest.raises(runner.SmokeAbort) as error:
        runner._ensure_workspace_preflight(
            temp_root=temp_root,
            state_root=state_root,
            workspaces=workspaces,
        )

    assert error.value.code == "isolated_state_overlaps_workspace"


def test_isolation_preflight_allows_state_under_its_dedicated_temp_root(tmp_path: Path) -> None:
    runner = _load_runner()
    temp_root = tmp_path / "isolated"
    temp_root.mkdir()
    state_root = temp_root / "localappdata" / "CodexBridge" / "workflows"
    state_root.mkdir(parents=True)
    workspaces = tuple(temp_root / f"workspace-{label}" for label in ("A", "B", "C"))
    for workspace in workspaces:
        workspace.mkdir()

    runner._ensure_workspace_preflight(
        temp_root=temp_root,
        state_root=state_root,
        workspaces=workspaces,
    )


def test_item_result_checks_skip_summary_order_and_job_projection() -> None:
    runner = _load_runner()
    workflow_id = "9cf8fa22-19f7-42e2-a5d1-49f839973820"
    alpha_key = f"{workflow_id}::do[0].processItems.iteration[0].do[1].processItem"
    gamma_key = f"{workflow_id}::do[0].processItems.iteration[2].do[1].processItem"
    jobs: list[dict[str, Any]] = [
        {"job_id": "job-alpha", "request_key": alpha_key},
        {"job_id": "job-gamma", "request_key": gamma_key},
    ]
    result = {
        "workflow_id": workflow_id,
        "result": {
            "results": [
                {
                    "id": "alpha",
                    "status": "SUCCEEDED",
                    "job_id": "job-alpha",
                    "request_key": alpha_key,
                    "final_text": "ALPHA",
                },
                {
                    "id": "skip",
                    "status": "skipped",
                    "job_id": None,
                    "request_key": None,
                    "final_text": None,
                },
                {
                    "id": "gamma",
                    "status": "SUCCEEDED",
                    "job_id": "job-gamma",
                    "request_key": gamma_key,
                    "final_text": "GAMMA",
                },
            ],
            "summary": {"total": 3, "succeeded": 2, "skipped": 1},
        },
    }

    assert runner.validate_item_result(result, workflow_id=workflow_id, jobs=jobs) == [
        "ALPHA",
        "GAMMA",
    ]
    result["result"]["summary"]["skipped"] = 0
    with pytest.raises(runner.SmokeAbort):
        runner.validate_item_result(result, workflow_id=workflow_id, jobs=jobs)


def test_single_job_api_readback_uses_legacy_projection_fields() -> None:
    runner = _load_runner()
    workflow_id = "9cf8fa22-19f7-42e2-a5d1-49f839973820"
    run_id = "run-9cf8fa22-19f7-42e2-a5d1-49f839973820"
    job = {
        "job_id": "job-bravo",
        "task_identity": "do[0].calculate",
        "state": "SUCCEEDED",
        "thread_id": "thread-bravo",
        "turn_id": "turn-bravo",
        "request_key": f"{workflow_id}::do[0].calculate",
    }
    result = {
        "workflow_id": workflow_id,
        "definition_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "result": {
            "job_id": job["job_id"],
            "request_key": job["request_key"],
            "result": {"final_text": "BRAVO"},
        },
    }
    runner._verify_api_readback(
        ui_port=12345,
        token="test-only-token",
        label="B",
        workflow_id=workflow_id,
        run_id=run_id,
        max_live_codex_jobs=1,
        saved={"jobs": [job], "stored_result": result},
        listing={
            "workflow_id": workflow_id,
            "status": "succeeded",
            "job_id": job["job_id"],
            "thread_id": job["thread_id"],
        },
        detail={
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": "succeeded",
            "worker_state": "completed",
            "terminal": True,
            "job_id": job["job_id"],
            "thread_id": job["thread_id"],
        },
        result=result,
    )


def _worker_pair_fixture(
    tmp_path: Path,
    *,
    label: str = "A",
    direct: bool = False,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    runner = _load_runner()
    runtime = runtime_root or (tmp_path / "runtime")
    venv_prefix = runtime / ".venv"
    base_prefix = runtime / "base-python"
    venv_executable = venv_prefix / "Scripts" / "python.exe"
    base_executable = base_prefix / "python.exe"
    for path in (venv_executable, base_executable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test executable placeholder")
    allowed = runner._allowed_worker_executable_paths(
        executable=venv_executable,
        base_executable=base_executable,
        prefix=venv_prefix,
        base_prefix=base_prefix,
    )
    run_dir = tmp_path / f"run-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    owned_bridge_pid = 500
    bridge_actual_pid = 510
    launcher_pid = 600 if label == "A" else 700
    worker_pid = launcher_pid if direct else launcher_pid + 1
    launcher_created_at = 1_800_000_100.0 + launcher_pid
    worker_created_at = launcher_created_at if direct else launcher_created_at + 0.25
    launcher = {
        "role": "worker",
        "pid": launcher_pid,
        "parent_pid": bridge_actual_pid,
        "created_at_epoch": launcher_created_at,
        "image_path": str(venv_executable.resolve()),
        "owner_matches_current_user": True,
        "identity_verified": True,
        "descendant_of_owned_bridge": True,
        "owned_bridge_ancestor_pids": [bridge_actual_pid, owned_bridge_pid],
        "owned_bridge_pid": owned_bridge_pid,
    }
    worker = (
        launcher
        if direct
        else {
            "role": "worker",
            "pid": worker_pid,
            "parent_pid": launcher_pid,
            "created_at_epoch": worker_created_at,
            "image_path": str(base_executable.resolve()),
            "owner_matches_current_user": True,
            "identity_verified": True,
            "descendant_of_owned_bridge": True,
            "owned_bridge_ancestor_pids": [
                launcher_pid,
                bridge_actual_pid,
                owned_bridge_pid,
            ],
            "owned_bridge_pid": owned_bridge_pid,
        }
    )
    image_path = venv_executable if direct else base_executable
    identity = {
        "mode": "start",
        "pid": worker_pid,
        "parent_pid": bridge_actual_pid if direct else launcher_pid,
        "run_dir": str(run_dir.resolve()),
        "creation_time_100ns": int((worker_created_at + 11_644_473_600) * 10_000_000),
        "image_path": str(image_path),
    }
    return {
        "runner": runner,
        "allowed": allowed,
        "run_dir": run_dir,
        "owned_bridge_pid": owned_bridge_pid,
        "launcher_pid": launcher_pid,
        "worker_pid": worker_pid,
        "identity": identity,
        "snapshots": [launcher] if direct else [launcher, worker],
        "launcher": launcher,
        "worker": worker,
        "venv_executable": venv_executable,
        "base_executable": base_executable,
    }


@pytest.mark.parametrize("direct", [True, False], ids=["direct", "venv-child"])
def test_worker_process_pair_accepts_direct_and_venv_child_models(
    tmp_path: Path, direct: bool
) -> None:
    fixture = _worker_pair_fixture(tmp_path, direct=direct)

    pair = fixture["runner"]._match_worker_process_pair(
        launcher_pid=fixture["launcher_pid"],
        worker_identity_record=fixture["identity"],
        process_snapshots=fixture["snapshots"],
        run_dir=fixture["run_dir"],
        owned_bridge_pid=fixture["owned_bridge_pid"],
        allowed_worker_executables=fixture["allowed"],
    )

    assert pair is not None
    assert pair["launcher"]["pid"] == fixture["launcher_pid"]
    assert pair["worker"]["pid"] == fixture["worker_pid"]
    assert pair["model"] == ("direct" if direct else "venv_child")


@pytest.mark.parametrize(
    "mutation",
    [
        "worker_pid",
        "worker_pid_swap",
        "parent_pid",
        "third_party_exe",
        "creation_time",
        "unowned_launcher",
        "run_dir",
    ],
)
def test_worker_process_pair_rejects_identity_mismatch(tmp_path: Path, mutation: str) -> None:
    fixture = _worker_pair_fixture(tmp_path)
    identity = dict(fixture["identity"])
    snapshots = [dict(row) for row in fixture["snapshots"]]
    if mutation == "worker_pid":
        identity["pid"] += 99
    elif mutation == "worker_pid_swap":
        identity["pid"] = fixture["launcher_pid"]
    elif mutation == "parent_pid":
        identity["parent_pid"] += 99
    elif mutation == "third_party_exe":
        third_party = tmp_path / "other-python" / "python.exe"
        third_party.parent.mkdir()
        third_party.write_bytes(b"third party")
        identity["image_path"] = str(third_party)
    elif mutation == "creation_time":
        identity["creation_time_100ns"] += 10_000_000
    elif mutation == "unowned_launcher":
        snapshots[0]["descendant_of_owned_bridge"] = False
    elif mutation == "run_dir":
        identity["run_dir"] = str((tmp_path / "another-run").resolve())

    assert (
        fixture["runner"]._match_worker_process_pair(
            launcher_pid=fixture["launcher_pid"],
            worker_identity_record=identity,
            process_snapshots=snapshots,
            run_dir=fixture["run_dir"],
            owned_bridge_pid=fixture["owned_bridge_pid"],
            allowed_worker_executables=fixture["allowed"],
        )
        is None
    )


def test_allowed_worker_executables_are_limited_to_venv_and_its_base(
    tmp_path: Path,
) -> None:
    venv_prefix = tmp_path / ".venv"
    base_prefix = tmp_path / "python-base"
    venv_executable = venv_prefix / "Scripts" / "python.exe"
    base_executable = base_prefix / "python.exe"
    unrelated_executable = tmp_path / "unrelated" / "python.exe"
    outside_base = tmp_path / "other-base" / "python.exe"
    for path in (venv_executable, base_executable, unrelated_executable, outside_base):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test executable placeholder")
    runner = _load_runner()

    allowed = runner._allowed_worker_executable_paths(
        executable=venv_executable,
        base_executable=base_executable,
        prefix=venv_prefix,
        base_prefix=base_prefix,
    )
    unrelated = runner._allowed_worker_executable_paths(
        executable=venv_executable,
        base_executable=outside_base,
        prefix=venv_prefix,
        base_prefix=base_prefix,
    )
    non_venv = runner._allowed_worker_executable_paths(
        executable=base_executable,
        base_executable=base_executable,
        prefix=base_prefix,
        base_prefix=base_prefix,
    )

    assert allowed == {
        str(venv_executable.resolve()).casefold(),
        str(base_executable.resolve()).casefold(),
    }
    assert str(unrelated_executable.resolve()).casefold() not in allowed
    assert str(outside_base.resolve()).casefold() not in unrelated
    assert non_venv == {str(base_executable.resolve()).casefold()}


def test_two_worker_pairs_match_four_process_snapshots_and_reject_extras(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    first = _worker_pair_fixture(tmp_path, label="A", runtime_root=runtime_root)
    second = _worker_pair_fixture(tmp_path, label="B", runtime_root=runtime_root)
    snapshots = first["snapshots"] + second["snapshots"]
    expected = [
        {
            "label": label,
            "run_dir": fixture["run_dir"],
            "launcher_pid": fixture["launcher_pid"],
            "worker_identity_record": fixture["identity"],
        }
        for label, fixture in (("A", first), ("B", second))
    ]
    runner = first["runner"]

    pairs = runner._match_worker_process_pairs(
        expected_pairs=expected,
        process_snapshots=snapshots,
        owned_bridge_pid=first["owned_bridge_pid"],
        allowed_worker_executables=first["allowed"],
    )
    extra_process = dict(snapshots[-1], pid=999, parent_pid=998)
    with_extra = [*snapshots, extra_process]

    assert pairs is not None
    assert len(pairs) == 2
    assert pairs["A"]["launcher"]["pid"] == first["launcher_pid"]
    assert pairs["A"]["worker"]["pid"] == first["worker_pid"]
    assert pairs["B"]["launcher"]["pid"] == second["launcher_pid"]
    assert pairs["B"]["worker"]["pid"] == second["worker_pid"]
    assert (
        runner._match_worker_process_pairs(
            expected_pairs=expected,
            process_snapshots=with_extra,
            owned_bridge_pid=first["owned_bridge_pid"],
            allowed_worker_executables=first["allowed"],
        )
        is None
    )


def test_worker_snapshot_evidence_projects_identity_without_absolute_paths(
    tmp_path: Path,
) -> None:
    fixture = _worker_pair_fixture(tmp_path)

    projected = fixture["runner"]._worker_snapshot_evidence(
        fixture["snapshots"], fixture["allowed"]
    )
    serialized = json.dumps(projected)

    assert len(projected) == 2
    assert all(row["image_matches_configured_interpreter"] for row in projected)
    assert "image_path" not in serialized
    assert str(fixture["venv_executable"]) not in serialized
    assert str(fixture["base_executable"]) not in serialized


@pytest.mark.parametrize("direct", [True, False], ids=["direct", "venv-child"])
def test_lifecycle_correlates_spawn_launcher_to_worker_bootstrap_pid(
    tmp_path: Path, direct: bool
) -> None:
    fixture = _worker_pair_fixture(tmp_path, direct=direct)
    run_id = f"run-{fixture['launcher_pid']}"
    workflow_id = f"workflow-{fixture['launcher_pid']}"
    launch_id = f"launch-{fixture['launcher_pid']}"
    request_digest = "d" * 64
    bridge_events = [
        ("request_reserved", "2026-11-14T00:00:00Z", {"request_digest": request_digest}),
        (
            "worker_launch_requested",
            "2026-11-14T00:00:01Z",
            {"launch_id": launch_id, "mode": "start"},
        ),
        (
            "worker_spawn_succeeded",
            "2026-11-14T00:00:02Z",
            {"launch_id": launch_id, "mode": "start", "pid": fixture["launcher_pid"]},
        ),
        (
            "worker_exited",
            "2026-11-14T00:00:07Z",
            {
                "launch_id": launch_id,
                "mode": "start",
                "pid": fixture["launcher_pid"],
                "returncode": 0,
                "state": "completed",
                "shutdown_requested": False,
            },
        ),
    ]
    worker_events = [
        ("worker_bootstrap", "2026-11-14T00:00:03Z"),
        ("dbos_ready", "2026-11-14T00:00:04Z"),
        ("workflow_started", "2026-11-14T00:00:05Z"),
        ("result_persisted", "2026-11-14T00:00:06Z"),
    ]
    (fixture["run_dir"] / "bridge-lifecycle.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "version": 1,
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "event": event,
                    "timestamp_utc": timestamp,
                    **fields,
                }
            )
            for event, timestamp, fields in bridge_events
        )
        + "\n",
        encoding="utf-8",
    )
    (fixture["run_dir"] / "worker-lifecycle.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "version": 1,
                    "run_id": run_id,
                    "workflow_id": workflow_id,
                    "launch_id": launch_id,
                    "mode": "start",
                    "pid": fixture["worker_pid"],
                    "event": event,
                    "timestamp_utc": timestamp,
                }
            )
            for event, timestamp in worker_events
        )
        + "\n",
        encoding="utf-8",
    )

    lifecycle = fixture["runner"]._verify_lifecycle(
        run_dir=fixture["run_dir"],
        run_id=run_id,
        workflow_id=workflow_id,
        request_digest=request_digest,
        worker_status={"state": "completed", "exit_code": 0},
        worker_identity_record=fixture["identity"],
        bridge_pid=fixture["owned_bridge_pid"],
        worker_process_snapshots=fixture["snapshots"],
        allowed_worker_executables=fixture["allowed"],
        token="test-only-token",
        temp_root=tmp_path / "isolated-temp",
        prompts=("Reply with exactly ALPHA",),
        replies=("ALPHA",),
    )

    assert lifecycle["status"] == "PASS"
    assert lifecycle["worker_launcher_pid"] == fixture["launcher_pid"]
    assert lifecycle["worker_pid"] == fixture["worker_pid"]
    assert lifecycle["worker_process_model"] == ("direct" if direct else "venv_child")
    serialized = json.dumps(lifecycle)
    assert "test-only-token" not in serialized
    assert "Reply with exactly ALPHA" not in serialized
    assert "ALPHA" not in serialized
