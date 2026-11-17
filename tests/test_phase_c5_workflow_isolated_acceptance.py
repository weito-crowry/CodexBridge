from __future__ import annotations

import json
import sqlite3
from collections import deque
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from codex_bridge.workflows.interpreter import (
    SAFE_CODEX_JOB_PROMPT,
    Interpreter,
    compute_task_action,
)
from codex_bridge.workflows.service import WorkflowService, _ensure_state_root
from scripts import phase_c5_workflow_isolated_acceptance as runner

APPROVAL_ID = "a" * 32
HEAD = "93089459f8387ef0fa6ef25f8458144d9d1a25bf"
SOURCE_HASH = "b" * 64


class FakeAdapter:
    def __init__(
        self, *, statuses: list[str] | None = None, report_overrides: dict[str, Any] | None = None
    ):
        self.start_calls: list[tuple[str, int, str]] = []
        self.statuses = deque(statuses or ["succeeded"])
        self.report_overrides = report_overrides or {}
        self.workflow_ids: dict[str, tuple[str, str]] = {}
        self.get_status_calls: list[str] = []

    def start(self, case: runner.RunCase, client_request_id: str) -> tuple[int, Any]:
        self.start_calls.append((case.name, case.expected_jobs, client_request_id))
        workflow_id = f"workflow-{case.name}"
        run_id = f"run-{case.name}"
        self.workflow_ids[workflow_id] = (run_id, client_request_id)
        return 202, {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": "queued",
        }

    def get_status(self, workflow_id: str) -> tuple[int, Any]:
        self.get_status_calls.append(workflow_id)
        status = self.statuses.popleft() if self.statuses else "succeeded"
        run_id, _client_request_id = self.workflow_ids[workflow_id]
        return 200, {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": status,
        }

    def get_result(self, workflow_id: str) -> tuple[int, Any]:
        return 200, {"workflow_id": workflow_id, "status": "succeeded"}

    def verify(
        self,
        case: runner.RunCase,
        accepted: dict[str, Any],
        _detail: dict[str, Any],
        _result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "job_count": case.expected_jobs,
            "result_matches": True,
            "graph_matches": True,
            "workflow_id": accepted["workflow_id"],
            "run_id": accepted["run_id"],
            **self.report_overrides.get(case.name, {}),
        }

    def readonly_restart(self, completed: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "status": "PASS",
            "new_post_count": 0,
            "new_job_count": 0,
            "snapshots_unchanged": True,
            "completed_runs": len(completed),
        }


@lru_cache(maxsize=1)
def _cases() -> tuple[runner.RunCase, ...]:
    return runner.build_run_cases()


class _FakeLiveAdapterHarness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workspace = tmp_path / "fake-live-workspace"
        self.temp_root = tmp_path
        localappdata = self.temp_root / "localappdata"
        localappdata.mkdir()
        self.state_root = localappdata / "CodexBridge" / "workflows"
        self.state_root.mkdir(parents=True)
        self.workspace.mkdir()
        self.expected_ports = (19081, 19082)
        self.expected_identity = {"pid": 7311, "create_time": 123.0}
        process = SimpleNamespace(pid=self.expected_identity["pid"])
        self.context = SimpleNamespace(
            temp_root=self.temp_root,
            process=process,
            identity=dict(self.expected_identity),
            ports=self.expected_ports,
            app_server_pid=8322,
            control_token="fake-control-token",
            codex_executable=tmp_path / "fake-codex.exe",
            workspace=self.workspace,
            state_root=self.state_root,
        )
        self.guard_calls: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.prepost_snapshots: list[dict[str, Any]] = []
        self.workflow_runs: dict[str, str] = {}
        self.worker_count = 0
        self.preflight_appserver_pid = self.context.app_server_pid
        self.missing_dependencies: list[str] = []
        self.raise_on_post = False
        self.ledger = runner.AttemptLedger.create(
            tmp_path / "budget",
            APPROVAL_ID,
            HEAD,
            SOURCE_HASH,
        )
        self.adapter = runner.LiveAdapter(self.context, self.ledger)
        self.adapter.begin_run = lambda _case: None
        self.adapter.get_status = self._get_status
        self.adapter.get_result = lambda workflow_id: (
            200,
            {"workflow_id": workflow_id, "status": "succeeded"},
        )
        self.adapter.verify = self._verify
        self.adapter.readonly_restart = lambda completed: {
            "status": "PASS",
            "new_post_count": 0,
            "new_job_count": 0,
            "snapshots_unchanged": True,
        }
        isolated = SimpleNamespace(
            _owned_listener_pids=self._owned_listener_pids,
            _preflight_bridge=self._preflight_bridge,
            _worker_process_count=self._worker_process_count,
        )
        phase5f5 = SimpleNamespace(_assert_bridge_identity=self._assert_bridge_identity)
        fake_b5 = SimpleNamespace(
            phase5f5=phase5f5,
            isolated=isolated,
            missing_workflow_dependencies=self._missing_workflow_dependencies,
            _read_sqlite=self._read_sqlite,
        )
        monkeypatch.setattr(runner, "_b5", lambda: fake_b5)
        monkeypatch.setattr(runner, "initial_workflow_post", self._initial_post)

    def _assert_bridge_identity(self, process: object, identity: dict[str, Any]) -> None:
        self.guard_calls.append("identity")
        if process is not self.context.process or identity != self.expected_identity:
            raise runner.RunnerAbort("BLOCKED", "fake_owned_bridge_identity_mismatch")

    def _owned_listener_pids(self, pid: int, ports: tuple[int, int]) -> None:
        self.guard_calls.append("listeners")
        if pid != self.context.process.pid or ports != self.expected_ports:
            raise runner.RunnerAbort("BLOCKED", "fake_owned_listener_identity_mismatch")

    def _preflight_bridge(
        self,
        process: object,
        *,
        mcp_port: int,
        ui_port: int,
        token: str,
        expected_codex: Path,
    ) -> dict[str, int]:
        self.guard_calls.append("appserver")
        if (
            process is not self.context.process
            or (mcp_port, ui_port) != self.expected_ports
            or token != self.context.control_token
            or expected_codex != self.context.codex_executable
        ):
            raise runner.RunnerAbort("BLOCKED", "fake_bridge_preflight_identity_mismatch")
        return {"appserver_pid": self.preflight_appserver_pid}

    def _worker_process_count(self, pid: int) -> int:
        self.guard_calls.append("worker")
        if pid != self.context.process.pid:
            raise runner.RunnerAbort("BLOCKED", "fake_worker_parent_identity_mismatch")
        return self.worker_count

    def _missing_workflow_dependencies(self) -> list[str]:
        self.guard_calls.append("dependencies")
        return self.missing_dependencies

    def _read_sqlite(
        self, path: Path, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        if not query.lstrip().casefold().startswith("select "):
            raise AssertionError("pre-POST ledger reads must remain read-only")
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def _request_rows(self) -> list[dict[str, Any]]:
        path = self.state_root / "requests.sqlite"
        if not path.exists():
            return []
        return self._read_sqlite(
            path,
            "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests",
        )

    def add_request(
        self,
        client_request_id: str,
        workflow_id: str,
        run_id: str,
        state: str = "running",
    ) -> None:
        path = self.state_root / "requests.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workflow_requests "
                "(client_request_id TEXT, workflow_id TEXT, run_id TEXT, state TEXT)"
            )
            connection.execute(
                "INSERT INTO workflow_requests VALUES (?, ?, ?, ?)",
                (client_request_id, workflow_id, run_id, state),
            )

    def _initial_post(self, context: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self.posts.append(dict(payload))
        if context is not self.context:
            raise AssertionError("live adapter must send through the isolated context")
        post_number = self.adapter.initial_posts
        expected_case = _cases()[post_number - 1]
        expected_request_id = runner.client_request_id_for(APPROVAL_ID, expected_case)
        if payload.get("client_request_id") != expected_request_id:
            raise AssertionError("fake POST did not receive the fixed current run ID")
        rows = self._request_rows()
        if post_number != len(rows) + 1:
            raise AssertionError("POST counter must increment only after preflight")
        self.prepost_snapshots.append(
            {
                "initial_posts_before_current": post_number - 1,
                "ledger_post_count": self.ledger.post_count,
                "ledger_job_count": self.ledger.job_count,
                "states": self.ledger.run_states.copy(),
                "database_ids": tuple(row.get("client_request_id") for row in rows),
                "current_id": payload["client_request_id"],
            }
        )
        if self.raise_on_post:
            raise TimeoutError("fake transport outcome unknown")
        workflow_id = f"workflow-live-{post_number}"
        run_id = f"run-live-{post_number}"
        self.workflow_runs[workflow_id] = run_id
        self.add_request(payload["client_request_id"], workflow_id, run_id)
        return 202, {"workflow_id": workflow_id, "run_id": run_id, "status": "queued"}

    def _get_status(self, workflow_id: str) -> tuple[int, dict[str, Any]]:
        return 200, {
            "workflow_id": workflow_id,
            "run_id": self.workflow_runs[workflow_id],
            "status": "succeeded",
        }

    @staticmethod
    def _verify(
        case: runner.RunCase,
        accepted: dict[str, Any],
        _detail: dict[str, Any],
        _result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "job_count": case.expected_jobs,
            "result_matches": True,
            "graph_matches": True,
            "workflow_id": accepted["workflow_id"],
            "run_id": accepted["run_id"],
        }


def _seed_live_prepost_run(
    harness: _FakeLiveAdapterHarness,
    case_index: int,
    *,
    include_prior_rows: bool = True,
    prior_row_state: str = "running",
) -> tuple[runner.RunCase, str]:
    cases = _cases()
    for prior_index, prior_case in enumerate(cases[:case_index], start=1):
        prior_id = runner.client_request_id_for(APPROVAL_ID, prior_case)
        harness.ledger.before_post(prior_case, prior_id)
        workflow_id = f"workflow-prior-{prior_index}"
        run_id = f"run-prior-{prior_index}"
        harness.ledger.mark_accepted(prior_case, prior_id, workflow_id, run_id)
        harness.ledger.record_observed_jobs(prior_case, prior_case.expected_jobs)
        harness.ledger.mark_success(prior_case)
        if include_prior_rows:
            harness.add_request(prior_id, workflow_id, run_id, prior_row_state)
    case = cases[case_index]
    client_request_id = runner.client_request_id_for(APPROVAL_ID, case)
    harness.ledger.before_post(case, client_request_id)
    harness.adapter.initial_posts = case_index
    return case, client_request_id


@pytest.mark.parametrize("case_index", [0, 1, 2])
def test_live_adapter_start_allows_only_prior_succeeded_request_rows(
    case_index: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    case, client_request_id = _seed_live_prepost_run(harness, case_index)

    status_code, accepted = harness.adapter.start(case, client_request_id)

    prior_cases = _cases()[:case_index]
    snapshot = harness.prepost_snapshots[0]
    assert status_code == 202
    assert accepted["workflow_id"] == f"workflow-live-{case_index + 1}"
    assert len(harness.posts) == 1
    assert harness.adapter.initial_posts == case_index + 1
    assert snapshot["initial_posts_before_current"] == case_index
    assert snapshot["ledger_post_count"] == case_index + 1
    assert snapshot["ledger_job_count"] == sum(item.expected_jobs for item in prior_cases)
    assert snapshot["database_ids"] == tuple(
        runner.client_request_id_for(APPROVAL_ID, item) for item in prior_cases
    )
    assert snapshot["current_id"] == client_request_id
    assert snapshot["states"] == {
        "A": "succeeded" if case_index > 0 else "maybe_sent",
        "B": "succeeded"
        if case_index > 1
        else ("maybe_sent" if case_index == 1 else "not_started"),
        "C": "maybe_sent" if case_index == 2 else "not_started",
    }


def test_execute_runs_uses_live_adapter_for_ordered_a_b_c_prepost_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)

    outcome = runner.execute_runs(_cases(), harness.ledger, harness.adapter)

    assert [case.name for case in _cases()] == ["A", "B", "C"]
    assert [post["client_request_id"] for post in harness.posts] == [
        runner.client_request_id_for(APPROVAL_ID, case) for case in _cases()
    ]
    assert [len(row["database_ids"]) for row in harness.prepost_snapshots] == [0, 1, 2]
    assert [row["initial_posts_before_current"] for row in harness.prepost_snapshots] == [0, 1, 2]
    assert [row["ledger_post_count"] for row in harness.prepost_snapshots] == [1, 2, 3]
    assert [row["ledger_job_count"] for row in harness.prepost_snapshots] == [0, 1, 3]
    assert [row["states"] for row in harness.prepost_snapshots] == [
        {"A": "maybe_sent", "B": "not_started", "C": "not_started"},
        {"A": "succeeded", "B": "maybe_sent", "C": "not_started"},
        {"A": "succeeded", "B": "succeeded", "C": "maybe_sent"},
    ]
    assert harness.ledger.post_count == 3
    assert harness.ledger.job_count == 6
    assert harness.ledger.run_states == {"A": "succeeded", "B": "succeeded", "C": "succeeded"}
    assert harness.adapter.initial_posts == 3
    assert len(harness.posts) == 3
    assert harness.guard_calls.count("identity") == 3
    assert harness.guard_calls.count("listeners") == 3
    assert harness.guard_calls.count("appserver") == 3
    assert harness.guard_calls.count("dependencies") == 3
    assert harness.guard_calls.count("worker") == 3
    assert outcome["gate2"] == "PASS"


@pytest.mark.parametrize("request_state", ["running", "succeeded"])
def test_live_adapter_accepts_prior_request_row_state_and_identity(
    request_state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    case, client_request_id = _seed_live_prepost_run(harness, 1, prior_row_state=request_state)

    status_code, accepted = harness.adapter.start(case, client_request_id)

    assert status_code == 202
    assert accepted["workflow_id"] == "workflow-live-2"
    assert len(harness.posts) == 1
    assert harness.adapter.initial_posts == 2


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("workflow_id", "workflow-other"),
        ("run_id", "run-other"),
        ("state", "failed"),
        ("state", "unknown"),
        ("state", "needs_reconciliation"),
    ],
)
def test_live_adapter_rejects_prior_request_row_identity_or_state_mismatch(
    mutation: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    case, client_request_id = _seed_live_prepost_run(harness, 1)
    prior_id = runner.client_request_id_for(APPROVAL_ID, _cases()[0])
    column = mutation
    with sqlite3.connect(harness.state_root / "requests.sqlite") as connection:
        connection.execute(
            f"UPDATE workflow_requests SET {column} = ? WHERE client_request_id = ?",
            (value, prior_id),
        )

    with pytest.raises(runner.RunnerAbort):
        harness.adapter.start(case, client_request_id)

    assert harness.posts == []
    assert harness.adapter.initial_posts == 1


@pytest.mark.parametrize(
    ("scenario", "case_index"),
    [
        ("foreign_row", 0),
        ("current_id_present", 0),
        ("missing_prior_row", 1),
        ("duplicate_prior_row", 2),
        ("invalid_db_id", 0),
        ("invalid_current_id", 0),
        ("invalid_prior_id", 1),
        ("multiple_maybe_sent", 0),
        ("prior_accepted", 1),
        ("prior_unknown", 1),
        ("prior_failed", 1),
        ("future_id_occupied", 0),
        ("initial_posts_mismatch", 1),
        ("ledger_post_count_mismatch", 0),
        ("duplicate_ledger_id", 1),
        ("current_accepted", 1),
    ],
)
def test_live_adapter_start_rejects_prepost_ledger_inconsistency_without_post(
    scenario: str,
    case_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    case, client_request_id = _seed_live_prepost_run(
        harness,
        case_index,
        include_prior_rows=scenario != "missing_prior_row",
    )
    prior_case = _cases()[0]
    prior_id = runner.client_request_id_for(APPROVAL_ID, prior_case)
    future_case = _cases()[2]

    if scenario == "foreign_row":
        harness.add_request("foreign-request-id", "workflow-foreign", "run-foreign")
    elif scenario == "current_id_present":
        harness.add_request(client_request_id, "workflow-current", "run-current")
    elif scenario == "duplicate_prior_row":
        harness.add_request(prior_id, "workflow-prior-duplicate", "run-prior-duplicate")
    elif scenario == "invalid_db_id":
        harness.add_request("invalid request ID", "workflow-invalid", "run-invalid")
    elif scenario == "invalid_current_id":
        harness.ledger.data["runs"][case.name]["client_request_id"] = "invalid request ID"
    elif scenario == "invalid_prior_id":
        harness.ledger.data["runs"][prior_case.name]["client_request_id"] = "invalid request ID"
    elif scenario == "multiple_maybe_sent":
        second_case = _cases()[1]
        harness.ledger.data["runs"][second_case.name]["state"] = "maybe_sent"
        harness.ledger.data["runs"][second_case.name]["client_request_id"] = (
            runner.client_request_id_for(APPROVAL_ID, second_case)
        )
    elif scenario in {"prior_accepted", "prior_unknown", "prior_failed"}:
        states = {"prior_accepted": "accepted", "prior_unknown": "unknown", "prior_failed": "fail"}
        harness.ledger.data["runs"][prior_case.name]["state"] = states[scenario]
    elif scenario == "future_id_occupied":
        harness.ledger.data["runs"][future_case.name]["client_request_id"] = (
            runner.client_request_id_for(APPROVAL_ID, future_case)
        )
    elif scenario == "initial_posts_mismatch":
        harness.adapter.initial_posts = 0
    elif scenario == "ledger_post_count_mismatch":
        harness.ledger.data["post_count"] = 0
    elif scenario == "duplicate_ledger_id":
        harness.ledger.data["runs"][case.name]["client_request_id"] = prior_id
    elif scenario == "current_accepted":
        harness.ledger.mark_accepted(
            case,
            client_request_id,
            "workflow-current",
            "run-current",
        )
    harness.ledger._save("fixture_prepost_ledger_mutation")
    before_initial_posts = harness.adapter.initial_posts

    with pytest.raises(runner.RunnerAbort):
        harness.adapter.start(case, client_request_id)

    assert harness.posts == []
    assert harness.adapter.initial_posts == before_initial_posts


@pytest.mark.parametrize(
    "guard", ["identity", "listener", "appserver", "worker", "dependencies", "workspace"]
)
def test_live_adapter_start_preserves_bridge_worker_and_workspace_guards(
    guard: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    case, client_request_id = _seed_live_prepost_run(harness, 0)
    if guard == "identity":
        harness.context.identity = {"pid": 9999, "create_time": 1.0}
    elif guard == "listener":
        harness.context.ports = (19091, 19092)
    elif guard == "appserver":
        harness.preflight_appserver_pid += 1
    elif guard == "worker":
        harness.worker_count = 1
    elif guard == "dependencies":
        harness.missing_dependencies = ["fake-workflow-dependency"]
    else:
        (harness.workspace / "unexpected.txt").write_text("occupied", encoding="utf-8")

    with pytest.raises(runner.RunnerAbort):
        harness.adapter.start(case, client_request_id)

    assert harness.posts == []
    assert harness.adapter.initial_posts == 0


def test_live_adapter_transport_unknown_is_not_reposted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _FakeLiveAdapterHarness(tmp_path, monkeypatch)
    harness.raise_on_post = True

    with pytest.raises(runner.RunnerAbort, match="start_outcome_unknown"):
        runner.execute_runs(_cases(), harness.ledger, harness.adapter)

    assert len(harness.posts) == 1
    assert harness.adapter.initial_posts == 1
    assert harness.ledger.post_count == 1
    assert harness.ledger.job_count == 0
    assert harness.ledger.run_states["A"] == "maybe_sent"
    assert not (harness.state_root / "requests.sqlite").exists()


def test_fixed_workflows_validate_as_exact_one_two_three_job_cases() -> None:
    cases = _cases()

    assert [case.expected_jobs for case in cases] == [1, 2, 3]
    assert [case.name for case in cases] == ["A", "B", "C"]
    assert [len(case.call_node_ids) for case in cases] == [1, 2, 1]
    assert len(cases[2].workflow_yaml.encode("utf-8")) < 64 * 1024
    for case in cases:
        runner.validate_fixed_case(case)
        assert yaml.safe_load(case.workflow_yaml) == case.definition

    loop = cases[2].definition["do"][0]["processIssues"]
    assert loop["for"]["in"] == "${ .issues }"
    assert loop["do"][0]["choose"]["switch"]
    assert cases[2].input_data == {
        "issues": [
            {"number": 1, "enabled": True},
            {"number": 2, "enabled": True},
            {"number": 3, "enabled": True},
        ]
    }


def test_runner_digest_matches_service_prepare_request_for_one_two_three_jobs(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "fixed-c5-workspace"
    state_root = tmp_path / "fixed-c5-state"
    workspace.mkdir()
    state_root.mkdir()
    service = WorkflowService(
        SimpleNamespace(state_root=state_root, allowed_roots=(str(tmp_path),)),
        codex_bridge_port=18001,
    )
    canonical_workspace = str(workspace.resolve(strict=True))

    for case in _cases():
        definition = yaml.safe_load(case.workflow_yaml)
        prepared = service._prepare_request(
            client_request_id=f"c5-request-{case.name.lower()}",
            workflow_yaml=case.workflow_yaml,
            input_data=case.input_data,
            workspace_root=canonical_workspace,
            max_live_codex_jobs=case.expected_jobs,
        )
        runner_digest = runner._workflow_request_digest(case, definition, canonical_workspace)
        assert prepared.request_digest == runner_digest

        wrong_max_live_codex_jobs = 1 if case.expected_jobs == 2 else 2
        wrong_prepared = service._prepare_request(
            client_request_id=f"c5-request-wrong-{case.name.lower()}",
            workflow_yaml=case.workflow_yaml,
            input_data=case.input_data,
            workspace_root=canonical_workspace,
            max_live_codex_jobs=wrong_max_live_codex_jobs,
            validate_definition=False,
        )
        assert wrong_prepared.request_digest != runner_digest


def _product_adapter_job_rows_and_events(
    run_dir: Path,
    workflow_id: str,
    expected_texts: tuple[str, ...],
    *,
    include_running: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    adapter = CodexJobAdapter(run_dir / "codex_jobs.sqlite")
    for index, final_text in enumerate(expected_texts, start=1):
        task_identity = f"do[0].codexJob[{index}]"
        job = adapter.prepare_job(
            workflow_id,
            task_identity,
            SAFE_CODEX_JOB_PROMPT,
            run_dir,
            port=24017,
            max_jobs_per_workflow=len(expected_texts),
        )
        job_id = str(job["job_id"])
        assert adapter.claim_post(job_id)
        adapter.mark_accepted(
            job_id,
            f"thread-c5-{index:08d}",
            f"turn-c5-{index:08d}",
            "in_progress",
        )
        if include_running:
            adapter._mark_running(job_id, "in_progress")
        adapter._mark_succeeded(job_id, final_text)

    adapter_jobs = adapter.get_jobs_for_workflow(workflow_id, limit=max(1, len(expected_texts)))
    adapter_jobs.sort(key=lambda job: str(job["task_identity"]))
    jobs = [
        {
            "job_id": job["job_id"],
            "request_key": f"{workflow_id}::{job['task_identity']}",
            "workflow_id": workflow_id,
            "task_identity": job["task_identity"],
            "state": job["state"],
            "remote_state": "completed",
            "receipt_json": json.dumps(job["receipt"], ensure_ascii=False),
        }
        for job in adapter_jobs
    ]
    events = [
        {key: event[key] for key in ("event_id", "job_id", "state")}
        for event in adapter.read_events(after_id=0, limit=runner.MAX_CODEX_JOB_EVENT_ROWS)
    ]
    return jobs, events


@pytest.mark.parametrize("case_index", [0, 1, 2])
@pytest.mark.parametrize("include_running", [False, True], ids=["without-running", "with-running"])
def test_verify_live_run_requires_matching_service_digest_in_ledger_and_manifest(
    case_index: int,
    include_running: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _cases()[case_index]
    workspace = tmp_path / "c5-verify-workspace"
    state_root = tmp_path / "c5-verify-state"
    workspace.mkdir()
    state_root.mkdir()
    work_root = str(workspace.resolve(strict=True))
    service = WorkflowService(
        SimpleNamespace(state_root=state_root, allowed_roots=(str(tmp_path),)),
        codex_bridge_port=18001,
    )
    client_request_id = f"c5-verify-{case.name.lower()}"
    workflow_id = f"workflow-{case.name}"
    run_id = f"run-{case.name}"
    definition = yaml.safe_load(case.workflow_yaml)
    prepared = service._prepare_request(
        client_request_id=client_request_id,
        workflow_yaml=case.workflow_yaml,
        input_data=case.input_data,
        workspace_root=work_root,
        max_live_codex_jobs=case.expected_jobs,
    )
    run_dir = state_root / run_id
    run_dir.mkdir()
    result = {"workflow_id": workflow_id, "status": "succeeded"}
    definition_hash = runner._sha256((runner._canonical_json(definition) + "\n").encode())
    input_hash = runner._sha256(runner._canonical_json(case.input_data).encode())
    manifest = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_source": "ui-api",
        "mode": "codex-job-live",
        "workspace_root": work_root,
        "definition_sha256": definition_hash,
        "input_sha256": input_hash,
        "request_digest": prepared.request_digest,
    }
    for filename, value in (
        ("manifest.json", manifest),
        ("result.json", result),
        ("worker-entry-status.json", {"state": "completed", "exit_code": 0}),
        ("workflow.yaml", definition),
        ("input.json", case.input_data),
    ):
        (run_dir / filename).write_text(json.dumps(value), encoding="utf-8")

    requests_db = state_root / "requests.sqlite"
    with sqlite3.connect(requests_db) as connection:
        connection.execute(
            "CREATE TABLE workflow_requests "
            "(client_request_id TEXT, workflow_id TEXT, run_id TEXT, "
            "request_digest TEXT, state TEXT)"
        )
        connection.execute(
            "INSERT INTO workflow_requests VALUES (?, ?, ?, ?, ?)",
            (client_request_id, workflow_id, run_id, prepared.request_digest, "succeeded"),
        )

    jobs, job_events = _product_adapter_job_rows_and_events(
        run_dir,
        workflow_id,
        case.expected_texts,
        include_running=include_running,
    )

    nodes: list[dict[str, Any]] = []
    task_events: list[dict[str, Any]] = []
    job_index = 0
    for node_id in case.static_node_ids:
        executions: list[dict[str, Any]] = []
        if node_id in case.call_node_ids:
            execution_count = case.expected_jobs if case.name == "C" else 1
            for _ in range(execution_count):
                job = jobs[job_index]
                job_index += 1
                executions.append(
                    {
                        "execution_id": f"execution-{job['job_id']}",
                        "state": "succeeded",
                        "observation_complete": True,
                        "job_id": job["job_id"],
                    }
                )
                task_events.append({"node_id": node_id, "event_type": "task_completed"})
        nodes.append({"node_id": node_id, "executions": executions})
    tasks = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "coverage": "complete",
        "truncated": False,
        "nodes": nodes,
    }
    events = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "coverage": "complete",
        "truncated": False,
        "has_more": False,
        "events": task_events,
    }

    def read_sqlite(path: Path, query: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def read_json(path: Path, *, maximum: int = 1024 * 1024) -> Any:
        if path.stat().st_size > maximum:
            raise ValueError("fixture exceeds requested read limit")
        return json.loads(path.read_text(encoding="utf-8"))

    fake_b5 = SimpleNamespace(
        _read_json=read_json,
        _read_sqlite=read_sqlite,
        _read_codex_job_ledger=lambda _path: (jobs, job_events),
        isolated=SimpleNamespace(
            _run_directory=lambda root, requested_run_id: root / requested_run_id,
            _read_dbos_status=lambda _run_dir, _workflow_id: "SUCCESS",
        ),
    )
    monkeypatch.setattr(runner, "_b5", lambda: fake_b5)
    monkeypatch.setattr(runner, "_read_events_http", lambda *_args: (tasks, events))
    monkeypatch.setattr(runner, "_read_events_mcp", lambda *_args: (tasks, events))
    context = SimpleNamespace(workspace=workspace, state_root=state_root)
    accepted = {
        "client_request_id": client_request_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
    }
    detail = {"workflow_id": workflow_id, "run_id": run_id, "status": "succeeded"}

    verified = runner._verify_live_run(context, case, accepted, detail, result)
    assert verified["job_count"] == case.expected_jobs

    with sqlite3.connect(requests_db) as connection:
        connection.execute("UPDATE workflow_requests SET request_digest = ?", ("0" * 64,))
    with pytest.raises(runner.RunnerAbort, match="workflow_request_digest_mismatch"):
        runner._verify_live_run(context, case, accepted, detail, result)

    with sqlite3.connect(requests_db) as connection:
        connection.execute(
            "UPDATE workflow_requests SET request_digest = ?", (prepared.request_digest,)
        )
    wrong_max_live_codex_jobs = 1 if case.expected_jobs == 2 else 2
    wrong_prepared = service._prepare_request(
        client_request_id=f"c5-verify-wrong-{case.name.lower()}",
        workflow_yaml=case.workflow_yaml,
        input_data=case.input_data,
        workspace_root=work_root,
        max_live_codex_jobs=wrong_max_live_codex_jobs,
        validate_definition=False,
    )
    manifest["request_digest"] = wrong_prepared.request_digest
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(runner.RunnerAbort, match="workflow_manifest_worker_or_dbos_mismatch"):
        runner._verify_live_run(context, case, accepted, detail, result)


@pytest.mark.parametrize(
    "mutation",
    [
        "two_event_legacy_trace",
        "missing_prepared",
        "missing_maybe_sent",
        "missing_accepted",
        "missing_succeeded",
        "duplicate_transition",
        "success_before_accepted",
        "foreign_job",
        "reverse_event_ids",
        "duplicate_event_id",
        "zero_event_id",
        "boolean_event_id",
        "unknown_state",
        "failed_state",
        "reconciliation_state",
        "too_many_events",
        "duplicate_job_id",
    ],
)
def test_codex_job_event_trace_rejects_invalid_adapter_histories(
    tmp_path: Path, mutation: str
) -> None:
    run_dir = tmp_path / "invalid-job-event-run"
    run_dir.mkdir()
    jobs, original_events = _product_adapter_job_rows_and_events(
        run_dir, "workflow-event-trace", ("4",), include_running=True
    )
    events = json.loads(json.dumps(original_events))
    if mutation == "two_event_legacy_trace":
        events = [event for event in events if event["state"] in {"MAYBE_SENT", "SUCCEEDED"}]
    elif mutation.startswith("missing_"):
        state = mutation.removeprefix("missing_").upper()
        events = [event for event in events if event["state"] != state]
    elif mutation == "duplicate_transition":
        events[-1]["state"] = "RUNNING"
    elif mutation == "success_before_accepted":
        for event, state in zip(
            events,
            ("PREPARED", "MAYBE_SENT", "SUCCEEDED", "ACCEPTED", "RUNNING"),
            strict=True,
        ):
            event["state"] = state
    elif mutation == "foreign_job":
        events[-1]["job_id"] = "foreign-job-1234"
    elif mutation == "reverse_event_ids":
        events.reverse()
    elif mutation == "duplicate_event_id":
        events[1]["event_id"] = events[0]["event_id"]
    elif mutation == "zero_event_id":
        events[0]["event_id"] = 0
    elif mutation == "boolean_event_id":
        events[0]["event_id"] = True
    elif mutation == "unknown_state":
        events[2]["state"] = "UNKNOWN"
    elif mutation == "failed_state":
        events[-1]["state"] = "FAILED"
    elif mutation == "reconciliation_state":
        events[-1]["state"] = "NEEDS_RECONCILIATION"
    elif mutation == "too_many_events":
        extra = dict(events[-1])
        extra["event_id"] = events[-1]["event_id"] + 1
        events.append(extra)
    elif mutation == "duplicate_job_id":
        jobs.append(dict(jobs[0]))

    with pytest.raises(runner.RunnerAbort, match="codex_job_event_trace_invalid"):
        runner.validate_codex_job_event_trace(jobs, events)


def test_codex_job_event_trace_checks_each_interleaved_job_independently(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "interleaved-job-event-run"
    run_dir.mkdir()
    jobs, events = _product_adapter_job_rows_and_events(
        run_dir,
        "workflow-event-traces",
        ("C5-B1", "C5-B2"),
        include_running=True,
    )
    grouped = {
        str(job["job_id"]): [event for event in events if event["job_id"] == job["job_id"]]
        for job in jobs
    }
    interleaved = [
        event
        for event_index in range(5)
        for job in jobs
        for event in grouped[str(job["job_id"])][event_index : event_index + 1]
    ]
    for event_id, event in enumerate(interleaved, start=1):
        event["event_id"] = event_id

    assert runner.validate_codex_job_event_trace(jobs, interleaved)


@pytest.mark.parametrize("mutation", ["extra_call", "input_count", "switch_path"])
def test_fixed_case_rejects_unreviewed_shape_or_job_count(mutation: str) -> None:
    case = _cases()[2]
    definition = json.loads(json.dumps(case.definition))
    input_data = json.loads(json.dumps(case.input_data))
    if mutation == "extra_call":
        definition["do"].append({"unexpected": {"call": "codex-job", "with": {"prompt": "x"}}})
    elif mutation == "input_count":
        input_data["issues"].append({"number": 4, "enabled": True})
    else:
        definition["do"][0]["processIssues"]["do"][0]["choose"]["switch"].append(
            {"extra": {"then": "processIssue"}}
        )
    mutated = runner.RunCase(
        name=case.name,
        expected_jobs=case.expected_jobs,
        definition=definition,
        workflow_yaml=yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
        input_data=input_data,
        expected_texts=case.expected_texts,
        static_node_ids=case.static_node_ids,
        call_node_ids=case.call_node_ids,
    )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_fixed_case(mutated)


def test_three_job_zero_iteration_and_switch_skip_are_fake_only() -> None:
    case = _cases()[2]
    observed_calls: list[str] = []

    def run_fake(input_data: dict[str, Any]) -> None:
        def step_runner(
            identity: str,
            task_name: str,
            operation: str,
            task_spec: dict[str, Any],
            task_input: Any,
            variables: dict[str, Any],
        ) -> Any:
            if operation == "call":
                observed_calls.append(identity)
                return {"status": "SUCCEEDED", "result": {"final_text": "fake"}}
            return compute_task_action(operation, task_spec, task_input, variables)

        Interpreter(
            case.definition,
            step_runner=step_runner,
            codex_job_live=True,
            max_live_codex_jobs=3,
        ).run(input_data)

    run_fake({"issues": []})
    run_fake({"issues": [{"number": 7, "enabled": False}]})
    assert observed_calls == []


def test_persistent_approval_marker_rejects_replay_after_interruption(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    ledger.before_post(_cases()[0], f"c5-{APPROVAL_ID}-a")

    marker = json.loads((tmp_path / f"{APPROVAL_ID}.json").read_text(encoding="utf-8"))
    assert marker["post_count"] == 1
    assert marker["runs"]["A"]["state"] == "maybe_sent"
    with pytest.raises(runner.RunnerAbort, match="approval_already_consumed"):
        runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)


def test_post_budget_is_consumed_before_transport_exception_and_never_retried(
    tmp_path: Path,
) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)

    class RaisesOnStart(FakeAdapter):
        def start(self, case: runner.RunCase, client_request_id: str) -> tuple[int, Any]:
            super().start(case, client_request_id)
            raise TimeoutError("fake timeout")

    adapter = RaisesOnStart()
    with pytest.raises(runner.RunnerAbort, match="start_outcome_unknown"):
        runner.execute_runs(_cases(), ledger, adapter)
    assert len(adapter.start_calls) == 1
    assert ledger.post_count == 1
    assert ledger.run_states["A"] == "maybe_sent"

    with pytest.raises(runner.RunnerAbort, match="approval_already_consumed"):
        runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)


def test_one_two_three_budget_runs_once_each_and_caps_total_jobs(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    adapter = FakeAdapter()

    result = runner.execute_runs(_cases(), ledger, adapter)

    assert [row[:2] for row in adapter.start_calls] == [("A", 1), ("B", 2), ("C", 3)]
    assert len({row[2] for row in adapter.start_calls}) == 3
    assert ledger.post_count == 3
    assert ledger.job_count == 6
    assert result["gate2"] == "PASS"
    assert result["gate3"] == "PASS"
    assert adapter.readonly_restart(result["completed_runs"])["new_post_count"] == 0


def test_failure_evidence_distinguishes_partial_runs_from_restart_failure(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    evidence = runner._create_evidence(
        APPROVAL_ID, {"approved_head": HEAD, "runner_source_sha256": SOURCE_HASH}
    )

    class UnknownStart(FakeAdapter):
        def start(self, case: runner.RunCase, client_request_id: str) -> tuple[int, Any]:
            super().start(case, client_request_id)
            return 200, {}

    with pytest.raises(runner.RunnerAbort) as caught:
        runner.execute_runs(_cases(), ledger, UnknownStart())
    runner._apply_failure_state(evidence, ledger, caught.value.status, caught.value.code)
    runner._apply_ledger_to_evidence(evidence, ledger)

    assert evidence["status"] == "UNKNOWN"
    assert evidence["gates"]["gate2"] == "BLOCKED"
    assert evidence["gates"]["gate3"] == "NOT_RUN"
    assert [row["status"] for row in evidence["runs"]] == ["MAYBE_SENT", "NOT_RUN", "NOT_RUN"]

    second_ledger = runner.AttemptLedger.create(tmp_path, "c" * 32, HEAD, SOURCE_HASH)
    second_evidence = runner._create_evidence(
        "c" * 32, {"approved_head": HEAD, "runner_source_sha256": SOURCE_HASH}
    )

    class RestartBlocked(FakeAdapter):
        def readonly_restart(self, _completed: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "status": "BLOCKED",
                "new_post_count": 0,
                "new_job_count": 0,
                "snapshots_unchanged": True,
            }

    with pytest.raises(runner.RunnerAbort) as restart_error:
        runner.execute_runs(_cases(), second_ledger, RestartBlocked())
    runner._apply_failure_state(
        second_evidence, second_ledger, restart_error.value.status, restart_error.value.code
    )
    runner._apply_ledger_to_evidence(second_evidence, second_ledger)

    assert second_evidence["gates"]["gate2"] == "PASS"
    assert second_evidence["gates"]["gate3"] == "BLOCKED"
    assert [row["status"] for row in second_evidence["runs"]] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_unknown_202_response_stops_later_runs(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)

    class UnknownStart(FakeAdapter):
        def start(self, case: runner.RunCase, client_request_id: str) -> tuple[int, Any]:
            super().start(case, client_request_id)
            return 202, {"status": "queued"}

    adapter = UnknownStart()
    with pytest.raises(runner.RunnerAbort, match="start_response_unknown"):
        runner.execute_runs(_cases(), ledger, adapter)
    assert [row[0] for row in adapter.start_calls] == ["A"]
    assert ledger.post_count == 1


def test_transient_reconciliation_is_polled_without_a_second_start(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    adapter = FakeAdapter(statuses=["needs_reconciliation", "running", "succeeded"])

    result = runner.execute_runs(_cases(), ledger, adapter)

    assert len(adapter.start_calls) == 3
    assert adapter.get_status_calls[:3] == ["workflow-A"] * 3
    assert result["gate2"] == "PASS"


def test_persistent_reconciliation_times_out_and_stops_after_first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    monkeypatch.setattr(runner, "MAX_WORKFLOW_WAIT_SECONDS", 0.01)

    class NeverResolved(FakeAdapter):
        def get_status(self, workflow_id: str) -> tuple[int, Any]:
            self.get_status_calls.append(workflow_id)
            run_id, _request_id = self.workflow_ids[workflow_id]
            return 200, {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": "needs_reconciliation",
            }

    adapter = NeverResolved()
    with pytest.raises(runner.RunnerAbort, match="workflow_status_wait_timed_out"):
        runner.execute_runs(_cases(), ledger, adapter)
    assert [row[0] for row in adapter.start_calls] == ["A"]
    assert ledger.post_count == 1


@pytest.mark.parametrize("terminal", ["failed", "failed_to_start", "unknown"])
def test_terminal_or_unknown_status_stops_later_runs(tmp_path: Path, terminal: str) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    adapter = FakeAdapter(statuses=[terminal])

    with pytest.raises(runner.RunnerAbort):
        runner.execute_runs(_cases(), ledger, adapter)
    assert [row[0] for row in adapter.start_calls] == ["A"]
    assert ledger.post_count == 1


def test_job_count_or_result_mismatch_stops_later_runs(tmp_path: Path) -> None:
    ledger = runner.AttemptLedger.create(tmp_path, APPROVAL_ID, HEAD, SOURCE_HASH)
    adapter = FakeAdapter(report_overrides={"A": {"job_count": 2}})

    with pytest.raises(runner.RunnerAbort):
        runner.execute_runs(_cases(), ledger, adapter)
    assert [row[0] for row in adapter.start_calls] == ["A"]


def test_head_and_dirty_source_guard_rejects_mismatch() -> None:
    runner.validate_git_guard(
        HEAD,
        HEAD,
        ancestor=True,
        dirty_paths=(),
        source_matches_head=True,
    )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_git_guard(
            "c" * 40, HEAD, ancestor=True, dirty_paths=(), source_matches_head=True
        )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_git_guard(
            HEAD, HEAD, ancestor=True, dirty_paths=("src/file.py",), source_matches_head=True
        )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_git_guard(
            HEAD, HEAD, ancestor=False, dirty_paths=(), source_matches_head=True
        )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_git_guard(
            HEAD, HEAD, ancestor=True, dirty_paths=(), source_matches_head=False
        )


def test_isolation_guard_rejects_production_ports_and_overlapping_roots(tmp_path: Path) -> None:
    root, _localappdata, workspace, _codexbridge_root, state = _fresh_isolated_roots(tmp_path)
    runner.validate_isolated_layout(
        root, state, workspace, (24001, 24002), require_state_root=False
    )

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            root, state, workspace, (18000, 24002), require_state_root=False
        )
    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            root, root, workspace, (24001, 24002), require_state_root=False
        )


def _fresh_isolated_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    temp_root = tmp_path / "isolated"
    temp_root.mkdir()
    localappdata = temp_root / "localappdata"
    localappdata.mkdir()
    workspace = temp_root / "workspace"
    workspace.mkdir()
    codexbridge_root = localappdata / "CodexBridge"
    state_root = codexbridge_root / "workflows"
    return temp_root, localappdata, workspace, codexbridge_root, state_root


def test_layout_accepts_fresh_uncreated_state_root_before_bridge_start(tmp_path: Path) -> None:
    temp_root, _localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )

    assert not codexbridge_root.exists()
    assert not state_root.exists()
    runner.validate_isolated_layout(
        temp_root, state_root, workspace, (24001, 24002), require_state_root=False
    )
    assert not codexbridge_root.exists()
    assert not state_root.exists()


def test_layout_requires_state_root_only_after_bridge_start(tmp_path: Path) -> None:
    temp_root, _localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=True
        )
    assert not codexbridge_root.exists()

    state_root.mkdir(parents=True)
    runner.validate_isolated_layout(
        temp_root, state_root, workspace, (24001, 24002), require_state_root=True
    )


def test_missing_state_after_mock_start_reproduces_previous_gate0_failure(
    tmp_path: Path,
) -> None:
    temp_root, _localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )

    runner.validate_isolated_layout(
        temp_root, state_root, workspace, (24001, 24002), require_state_root=False
    )
    assert not codexbridge_root.exists()
    with pytest.raises(runner.RunnerAbort, match="isolated_root_unavailable"):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=True
        )


def _state_root_context(tmp_path: Path) -> tuple[Any, Path, Path, Path]:
    temp_root, localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    appdata = temp_root / "appdata"
    appdata.mkdir()
    temp_dir = temp_root / "tmp"
    temp_dir.mkdir()
    context = SimpleNamespace(
        temp_root=temp_root,
        state_root=state_root,
        workspace=workspace,
        ports=(24001, 24002),
        environment={
            "LOCALAPPDATA": str(localappdata),
            "APPDATA": str(appdata),
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "CODEX_HOME": str(temp_root / "codex_home"),
        },
    )
    return context, localappdata, codexbridge_root, state_root


def _install_fake_execute_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge_start: Any | None = None,
    run_execution: Any | None = None,
    bridge_stop: Any | None = None,
) -> tuple[Any, list[str], list[dict[str, Any]], runner.AttemptLedger, Any]:
    context, _localappdata, _codexbridge_root, _state_root = _state_root_context(tmp_path)
    context.control_token = "fake-control-token"
    context.mcp_token = "fake-mcp-token"
    context.codex_executable = context.temp_root / "fake-codex.exe"
    context.process = None
    context.identity = None
    trace: list[str] = []
    captured_evidence: list[dict[str, Any]] = []
    process = SimpleNamespace(exit_code=None)
    process.poll = lambda: process.exit_code
    identity = {"pid": 7311, "create_time": 123.0}
    ledger = runner.AttemptLedger.create(tmp_path / "budget", APPROVAL_ID, HEAD, SOURCE_HASH)
    original_validate = runner.validate_isolated_layout
    original_initialize = runner._initialize_isolated_state_root

    class TrackingEnvironment(dict[str, str]):
        localappdata_checked = False

        def get(self, key: str, default: Any = None) -> Any:
            if key == "LOCALAPPDATA" and not self.localappdata_checked:
                self.localappdata_checked = True
                trace.append("environment_check")
            return super().get(key, default)

    context.environment = TrackingEnvironment(context.environment)

    def fake_ledger_create(
        cls: type[runner.AttemptLedger], *_args: Any, **_kwargs: Any
    ) -> runner.AttemptLedger:
        return ledger

    def fake_preflight(*_args: Any) -> dict[str, Any]:
        trace.append("preflight")
        return {
            "approved_head": HEAD,
            "runner_source_sha256": SOURCE_HASH,
            "codex_executable": context.codex_executable,
            "source_home": context.temp_root / "source-home",
        }

    def create_context(*_args: Any) -> Any:
        trace.append("context_created")
        return context

    def assert_owner(owned_process: object, owned_identity: dict[str, Any]) -> None:
        trace.append("verify_owner")
        if owned_process is not process or owned_identity != identity:
            raise runner.RunnerAbort("BLOCKED", "fake_owner_mismatch")

    def stop_process(
        owned_process: Any,
        _owned_identity: dict[str, Any],
        *,
        ui_port: int,
        control_token: str,
    ) -> None:
        trace.append("stop_owned")
        if bridge_stop is not None:
            bridge_stop(owned_process)
            return
        if ui_port != context.ports[1] or control_token != context.control_token:
            raise runner.RunnerAbort("BLOCKED", "fake_stop_arguments_mismatch")
        owned_process.exit_code = 0

    def read_sqlite(
        path: Path, query: str, parameters: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        with sqlite3.connect(
            f"{path.resolve(strict=True).as_uri()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    fake_b5 = SimpleNamespace(
        _make_isolated_context=create_context,
        phase5f5=SimpleNamespace(_assert_bridge_identity=assert_owner),
        stop_owned_bridge=stop_process,
        _read_sqlite=read_sqlite,
    )

    def tracked_validate(
        temp_root: Path,
        state_root: Path,
        workspace: Path,
        ports: tuple[int, int],
        *,
        require_state_root: bool,
    ) -> None:
        trace.append("strict_layout" if require_state_root else "prestart_layout")
        original_validate(
            temp_root,
            state_root,
            workspace,
            ports,
            require_state_root=require_state_root,
        )

    def tracked_initialize(_context: Any) -> None:
        trace.append("initialize_state")
        original_initialize(_context)

    def fake_start(_context: Any, *, restart: bool = False) -> dict[str, Any]:
        trace.append("bridge_start")
        if restart:
            raise AssertionError("initial acceptance must not use restart mode")
        if bridge_start is not None:
            return bridge_start(_context, process, identity)
        _context.process = process
        _context.identity = identity
        return {}

    def fake_runs(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        trace.append("execute_runs")
        if run_execution is not None:
            return run_execution(*_args, **_kwargs)
        raise runner.RunnerAbort("BLOCKED", "offline_test_stop_before_post")

    def capture_evidence(
        evidence: dict[str, Any],
        _approval_id: str,
        secrets_to_reject: tuple[str, ...],
    ) -> None:
        runner.assert_safe_evidence(evidence, secrets_to_reject=secrets_to_reject)
        captured_evidence.append(json.loads(json.dumps(evidence)))

    monkeypatch.setattr(runner.AttemptLedger, "create", classmethod(fake_ledger_create))
    monkeypatch.setattr(runner, "live_preflight", fake_preflight)
    monkeypatch.setattr(runner, "_b5", lambda: fake_b5)
    monkeypatch.setattr(runner, "_same_owner_as_current_user", lambda _path: True)
    monkeypatch.setattr(runner, "validate_isolated_layout", tracked_validate)
    monkeypatch.setattr(runner, "_initialize_isolated_state_root", tracked_initialize)
    monkeypatch.setattr(runner, "start_isolated_bridge", fake_start)
    monkeypatch.setattr(runner, "execute_runs", fake_runs)
    monkeypatch.setattr(runner, "_write_live_evidence", capture_evidence)
    return context, trace, captured_evidence, ledger, process


def _fake_successful_live_outcome(ledger: runner.AttemptLedger) -> dict[str, Any]:
    completed_runs: list[dict[str, Any]] = []
    for case in runner.build_run_cases():
        request_id = runner.client_request_id_for(APPROVAL_ID, case)
        workflow_id = f"workflow-evidence-{case.name}"
        run_id = f"run-evidence-{case.name}"
        ledger.before_post(case, request_id)
        ledger.mark_accepted(case, request_id, workflow_id, run_id)
        ledger.record_observed_jobs(case, case.expected_jobs)
        ledger.mark_success(case)
        completed_runs.append(
            {
                "name": case.name,
                "expected_jobs": case.expected_jobs,
                "observed_jobs": case.expected_jobs,
                "workflow_id": workflow_id,
                "run_id": run_id,
                "result_matches": True,
                "graph_matches": True,
            }
        )
    ledger.data["status"] = "runs_succeeded_gui_not_run"
    ledger._save("all_runs_succeeded_readonly_restart_passed")
    return {
        "gate1": "NOT_RUN",
        "gate2": "PASS",
        "gate3": "PASS",
        "completed_runs": completed_runs,
        "restart": {
            "status": "PASS",
            "new_post_count": 0,
            "new_job_count": 0,
            "snapshots_unchanged": True,
        },
    }


def test_execute_live_success_normalizes_and_writes_safe_public_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    docs = repo_root / "docs"
    docs.mkdir(parents=True)
    writer = runner._write_live_evidence
    run_executor = runner.execute_runs
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", repo_root)

    fake_adapters: list[Any] = []

    class FakeLiveAdapter:
        def __init__(self, context: Any, _ledger: runner.AttemptLedger) -> None:
            self.context = context
            self._worker_observer = None
            self.rows: dict[str, tuple[runner.RunCase, str]] = {}
            self.start_count = 0
            self.restart_count = 0
            fake_adapters.append(self)

        def start(self, case: runner.RunCase, _request_id: str) -> tuple[int, dict[str, str]]:
            workflow_id = f"workflow-evidence-{case.name}"
            run_id = f"run-evidence-{case.name}"
            self.rows[workflow_id] = (case, run_id)
            self.start_count += 1
            return 202, {"workflow_id": workflow_id, "run_id": run_id}

        def get_status(self, workflow_id: str) -> tuple[int, dict[str, str]]:
            _case, run_id = self.rows[workflow_id]
            return 200, {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": "succeeded",
            }

        def get_result(self, workflow_id: str) -> tuple[int, dict[str, str]]:
            return 200, {"workflow_id": workflow_id, "status": "succeeded"}

        def verify(
            self,
            case: runner.RunCase,
            accepted: dict[str, Any],
            detail: dict[str, Any],
            _api_result: dict[str, Any],
        ) -> dict[str, Any]:
            assert accepted["workflow_id"] == f"workflow-evidence-{case.name}"
            assert detail["run_id"] == f"run-evidence-{case.name}"
            run_dir = self.context.state_root / f"run-ledger-{case.name.lower()}"
            run_dir.mkdir()
            with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
                connection.execute("CREATE TABLE jobs (job_id TEXT NOT NULL)")
                connection.executemany(
                    "INSERT INTO jobs VALUES (?)",
                    [
                        (f"fake-job-{case.name.lower()}-{index}",)
                        for index in range(case.expected_jobs)
                    ],
                )
            return {
                "job_count": case.expected_jobs,
                "result_matches": True,
                "graph_matches": True,
            }

        def readonly_restart(self, completed: list[dict[str, Any]]) -> dict[str, Any]:
            assert [row["name"] for row in completed] == ["A", "B", "C"]
            self.restart_count += 1
            return {
                "status": "PASS",
                "new_post_count": 0,
                "new_job_count": 0,
                "snapshots_unchanged": True,
            }

    _context, _trace, _captured, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(runner, "LiveAdapter", FakeLiveAdapter)
    monkeypatch.setattr(runner, "execute_runs", run_executor)
    monkeypatch.setattr(runner, "_write_live_evidence", writer)

    result = runner.execute_live(HEAD, APPROVAL_ID)

    evidence_path = docs / f"phase_c5_workflow_acceptance-{APPROVAL_ID}.json"
    summary_path = docs / f"phase_c5_p2_live_acceptance-{APPROVAL_ID}.md"
    assert result == 0
    assert evidence_path.is_file()
    assert summary_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    runner.assert_safe_evidence(
        evidence, secrets_to_reject=("fake-control-token", "fake-mcp-token")
    )
    assert evidence["status"] == "LIVE_RUNS_PASS_GUI_NOT_RUN"
    assert evidence["gate0_stage"] == "runs_completed"
    assert evidence["owned_bridge_started"] is True
    assert evidence["owned_bridge_shutdown_confirmed"] is True
    assert evidence["read_only_ledger_job_rows"] == 6
    assert evidence["gates"] == {
        "gate0": "PASS",
        "gate1": "NOT_RUN",
        "gate2": "PASS",
        "gate3": "PASS",
    }
    assert evidence["budget"]["observed_initial_workflow_posts"] == 3
    assert evidence["budget"]["verified_completed_job_count"] == 6
    public_run_keys = {
        "name",
        "status",
        "initial_post_count",
        "expected_job_count",
        "verified_completed_job_count",
        "workflow_id",
        "run_id",
        "result_matches",
        "graph_matches",
    }
    assert [row["name"] for row in evidence["runs"]] == ["A", "B", "C"]
    for row, expected_jobs in zip(evidence["runs"], (1, 2, 3), strict=True):
        assert set(row) == public_run_keys
        assert row["status"] == "SUCCEEDED"
        assert row["initial_post_count"] == 1
        assert row["expected_job_count"] == expected_jobs
        assert row["verified_completed_job_count"] == expected_jobs
        assert row["result_matches"] is True
        assert row["graph_matches"] is True
        assert "expected_jobs" not in row
        assert "observed_jobs" not in row
    summary = summary_path.read_text(encoding="utf-8")
    assert "Verified completed jobs: 6/6" in summary
    assert "Read-only isolated ledger job rows: 6" in summary
    assert not runner.WINDOWS_PATH_PATTERN.search(summary)
    assert not runner.POSIX_PATH_PATTERN.search(summary)
    assert ledger.post_count == 3
    assert ledger.job_count == 6
    assert len(fake_adapters) == 1
    assert fake_adapters[0].start_count == 3
    assert fake_adapters[0].restart_count == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_run",
        "duplicate_run",
        "out_of_order",
        "expected_jobs_mismatch",
        "observed_jobs_mismatch",
        "invalid_workflow_id",
        "duplicate_identity",
        "result_mismatch",
        "graph_mismatch",
    ],
)
def test_success_outcome_normalizer_rejects_inconsistent_runs(
    tmp_path: Path, mutation: str
) -> None:
    ledger = runner.AttemptLedger.create(tmp_path / "budget", APPROVAL_ID, HEAD, SOURCE_HASH)
    outcome = _fake_successful_live_outcome(ledger)
    completed = outcome["completed_runs"]
    if mutation == "missing_run":
        completed.pop()
    elif mutation == "duplicate_run":
        completed[1] = dict(completed[0])
    elif mutation == "out_of_order":
        completed[0], completed[1] = completed[1], completed[0]
    elif mutation == "expected_jobs_mismatch":
        completed[1]["expected_jobs"] = 3
    elif mutation == "observed_jobs_mismatch":
        completed[1]["observed_jobs"] = 1
    elif mutation == "invalid_workflow_id":
        completed[0]["workflow_id"] = "C:\\private\\workflow"
    elif mutation == "duplicate_identity":
        completed[2]["run_id"] = completed[0]["run_id"]
    elif mutation == "result_mismatch":
        completed[1]["result_matches"] = False
    elif mutation == "graph_mismatch":
        completed[2]["graph_matches"] = False

    with pytest.raises(runner.RunnerAbort, match=runner._LIVE_SUCCESS_OUTCOME_ERROR):
        runner._normalize_success_outcome_runs(outcome, ledger)


def test_execute_live_saves_safe_failure_evidence_without_reusing_attempt_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    docs = repo_root / "docs"
    docs.mkdir(parents=True)
    writer = runner._write_live_evidence
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", repo_root)

    def fail_after_post(
        cases: list[runner.RunCase], ledger: runner.AttemptLedger, _adapter: Any
    ) -> dict[str, Any]:
        case = cases[0]
        request_id = runner.client_request_id_for(APPROVAL_ID, case)
        ledger.before_post(case, request_id)
        ledger.mark_accepted(case, request_id, "workflow-failure-a", "run-failure-a")
        ledger.mark_terminal(case, "FAIL", "codex_job_event_trace_invalid")
        raise runner.RunnerAbort("FAIL", "codex_job_event_trace_invalid")

    _context, _trace, _captured, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch, run_execution=fail_after_post
    )
    monkeypatch.setattr(runner, "_write_live_evidence", writer)

    result = runner.execute_live(HEAD, APPROVAL_ID)

    evidence_path = docs / f"phase_c5_workflow_acceptance-{APPROVAL_ID}.json"
    summary_path = docs / f"phase_c5_p2_live_acceptance-{APPROVAL_ID}.md"
    assert result == 2
    assert evidence_path.is_file()
    assert summary_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    runner.assert_safe_evidence(
        evidence, secrets_to_reject=("fake-control-token", "fake-mcp-token")
    )
    assert evidence["status"] == "FAIL"
    assert evidence["terminal_code"] == "codex_job_event_trace_invalid"
    assert evidence["budget"]["observed_initial_workflow_posts"] == 1
    assert evidence["runs"][0]["status"] == "FAIL"
    assert evidence["runs"][0]["verified_completed_job_count"] == 0
    assert evidence["runs"][1]["status"] == "NOT_RUN"
    assert evidence["runs"][2]["status"] == "NOT_RUN"
    assert ledger.marker_path.is_file()


def test_execute_live_blocks_invalid_success_outcome_instead_of_claiming_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_success_outcome(
        _cases: list[runner.RunCase], ledger: runner.AttemptLedger, _adapter: Any
    ) -> dict[str, Any]:
        outcome = _fake_successful_live_outcome(ledger)
        outcome["completed_runs"][1]["graph_matches"] = False
        return outcome

    _context, _trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch, run_execution=invalid_success_outcome
    )

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    evidence = evidence_rows[0]
    assert evidence["status"] == "BLOCKED"
    assert evidence["terminal_code"] == runner._LIVE_SUCCESS_OUTCOME_ERROR
    assert evidence["gates"]["gate2"] == "BLOCKED"
    assert evidence["gates"]["gate3"] == "BLOCKED"
    assert all(row["status"] == "SUCCEEDED" for row in evidence["runs"])
    assert all(row["result_matches"] is None for row in evidence["runs"])
    assert all(row["graph_matches"] is None for row in evidence["runs"])
    assert ledger.data["status"] == "BLOCKED"
    assert ledger.data["terminal_code"] == runner._LIVE_SUCCESS_OUTCOME_ERROR


@pytest.mark.parametrize(
    ("writer_code", "expected_output_code"),
    [
        ("evidence_run_job_count_invalid", "evidence_run_job_count_invalid"),
        ("unsafe-C:\\private\\detail", "live_evidence_write_failed"),
    ],
)
def test_execute_live_prints_only_known_fixed_evidence_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    writer_code: str,
    expected_output_code: str,
) -> None:
    _context, _trace, _captured, _ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch
    )

    def blocked_writer(*_args: Any, **_kwargs: Any) -> None:
        raise runner.RunnerAbort("BLOCKED", writer_code)

    monkeypatch.setattr(runner, "_write_live_evidence", blocked_writer)
    result = runner.execute_live(HEAD, APPROVAL_ID)
    output = capsys.readouterr().out

    assert result == 2
    assert f"live evidence could not be safely written ({expected_output_code})" in output
    if writer_code != expected_output_code:
        assert writer_code not in output
    assert "fake-control-token" not in output
    assert "fake-mcp-token" not in output


def test_initialize_state_root_isolated_and_lazy_create_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, localappdata, codexbridge_root, state_root = _state_root_context(tmp_path)
    monkeypatch.setattr(runner, "_same_owner_as_current_user", lambda _path: True)

    runner.validate_isolated_layout(
        context.temp_root,
        state_root,
        context.workspace,
        context.ports,
        require_state_root=False,
    )
    runner._initialize_isolated_state_root(context)
    runner.validate_isolated_layout(
        context.temp_root,
        state_root,
        context.workspace,
        context.ports,
        require_state_root=True,
    )

    assert codexbridge_root.is_dir()
    assert state_root.is_dir()
    assert tuple(localappdata.iterdir()) == (codexbridge_root,)
    assert _ensure_state_root(state_root, create=True) == state_root.resolve(strict=True)
    assert WorkflowDefinitionRepository(state_root)._resolve_state_root(create=True) == (
        state_root.resolve(strict=True)
    )


@pytest.mark.parametrize(
    "fault",
    [
        "localappdata_mismatch",
        "existing_codexbridge",
        "owner_mismatch",
        "permission_denied",
        "bridge_mkdir_failure",
        "workflows_mkdir_failure",
        "original_state_overlap",
        "reparse_parent",
    ],
)
def test_initialize_state_root_fails_closed_without_leaving_isolated_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    context, localappdata, codexbridge_root, state_root = _state_root_context(tmp_path)
    monkeypatch.setattr(runner, "_same_owner_as_current_user", lambda _path: True)
    if fault == "localappdata_mismatch":
        context.environment["LOCALAPPDATA"] = context.environment["APPDATA"]
    elif fault == "existing_codexbridge":
        codexbridge_root.mkdir()
    elif fault == "owner_mismatch":
        monkeypatch.setattr(
            runner,
            "_same_owner_as_current_user",
            lambda path: Path(path) != localappdata,
        )
    elif fault == "permission_denied":
        real_access = runner.os.access
        monkeypatch.setattr(
            runner.os,
            "access",
            lambda path, mode, *args, **kwargs: (
                False if Path(path) == localappdata else real_access(path, mode, *args, **kwargs)
            ),
        )
    elif fault in {"bridge_mkdir_failure", "workflows_mkdir_failure"}:
        target = codexbridge_root if fault == "bridge_mkdir_failure" else state_root
        real_mkdir = Path.mkdir

        def fail_target_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == target:
                raise PermissionError("fake directory creation failure")
            real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_target_mkdir)
    elif fault == "original_state_overlap":
        monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    elif fault == "reparse_parent":
        real_reparse = runner._is_reparse_point
        monkeypatch.setattr(
            runner,
            "_is_reparse_point",
            lambda path: Path(path) == localappdata or real_reparse(path),
        )

    with pytest.raises(runner.RunnerAbort):
        runner._initialize_isolated_state_root(context)

    if fault in {
        "localappdata_mismatch",
        "existing_codexbridge",
        "owner_mismatch",
        "permission_denied",
        "bridge_mkdir_failure",
        "original_state_overlap",
        "reparse_parent",
    }:
        assert not state_root.exists()


@pytest.mark.parametrize("precreated", ["codexbridge", "workflows"])
def test_layout_rejects_precreated_state_hierarchy_before_bridge_start(
    tmp_path: Path, precreated: str
) -> None:
    temp_root, _localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    target = codexbridge_root if precreated == "codexbridge" else state_root
    target.mkdir(parents=True)

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=False
        )


@pytest.mark.parametrize(
    ("unsafe_case", "ports"),
    [
        ("state_path_shifted", (24001, 24002)),
        ("workspace_outside", (24001, 24002)),
        ("workspace_localappdata_overlap", (24001, 24002)),
        ("workspace_missing", (24001, 24002)),
        ("workspace_dirty", (24001, 24002)),
        ("state_workspace_overlap", (24001, 24002)),
        ("duplicate_port", (24001, 24001)),
        ("production_port", (18000, 24002)),
    ],
)
def test_layout_rejects_unsafe_roots_ports_and_workspace(
    tmp_path: Path, unsafe_case: str, ports: tuple[int, int]
) -> None:
    temp_root, localappdata, workspace, _codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    selected_workspace = workspace
    selected_state = state_root
    require_state_root = False
    if unsafe_case == "state_path_shifted":
        selected_state = tmp_path / "elsewhere" / "workflows"
    elif unsafe_case == "workspace_outside":
        selected_workspace = tmp_path / "outside-workspace"
        selected_workspace.mkdir()
    elif unsafe_case == "workspace_localappdata_overlap":
        selected_workspace = localappdata
    elif unsafe_case == "workspace_missing":
        workspace.rmdir()
    elif unsafe_case == "workspace_dirty":
        (workspace / "unexpected.txt").write_text("occupied", encoding="utf-8")
    elif unsafe_case == "state_workspace_overlap":
        state_root.mkdir(parents=True)
        selected_workspace = state_root
        require_state_root = True

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root,
            selected_state,
            selected_workspace,
            ports,
            require_state_root=require_state_root,
        )


@pytest.mark.parametrize(
    "component", ["temp_root", "localappdata", "workspace", "codexbridge", "state_root"]
)
def test_layout_rejects_reparse_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    temp_root, localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    state_root.mkdir(parents=True)
    targets = {
        "temp_root": temp_root,
        "localappdata": localappdata,
        "workspace": workspace,
        "codexbridge": codexbridge_root,
        "state_root": state_root,
    }
    target = targets[component]
    real_is_reparse_point = runner._is_reparse_point
    monkeypatch.setattr(
        runner,
        "_is_reparse_point",
        lambda path: Path(path) == target or real_is_reparse_point(path),
    )

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=True
        )


def test_layout_rejects_state_root_without_write_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_root, _localappdata, workspace, _codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    state_root.mkdir(parents=True)
    real_access = runner.os.access

    def deny_state_access(path: str | bytes | Path, mode: int, *args: Any, **kwargs: Any) -> bool:
        if Path(path) == state_root and mode == runner.os.R_OK | runner.os.W_OK | runner.os.X_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(runner.os, "access", deny_state_access)
    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=True
        )


def test_layout_rejects_an_actual_symlinked_state_parent(tmp_path: Path) -> None:
    temp_root, localappdata, workspace, codexbridge_root, state_root = _fresh_isolated_roots(
        tmp_path
    )
    target = tmp_path / "other-localappdata"
    target.mkdir()
    localappdata.rmdir()
    try:
        localappdata.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Windows could not create a symlink in the test temp directory: {error}")

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(
            temp_root, state_root, workspace, (24001, 24002), require_state_root=False
        )


def test_execute_live_initializes_state_before_mock_bridge_and_records_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch
    )

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    assert ledger.post_count == 0
    assert trace == [
        "preflight",
        "context_created",
        "prestart_layout",
        "environment_check",
        "initialize_state",
        "strict_layout",
        "bridge_start",
        "strict_layout",
        "execute_runs",
        "verify_owner",
        "stop_owned",
        "strict_layout",
    ]
    assert context.state_root.is_dir()
    assert len(evidence_rows) == 1
    evidence = evidence_rows[0]
    assert evidence["gate0_stage"] == "starting_runs"
    assert evidence["owned_bridge_started"] is True
    assert evidence["owned_bridge_shutdown_confirmed"] is True
    assert evidence["read_only_ledger_job_rows"] == 0
    assert evidence["budget"]["verified_completed_job_count"] == 0
    assert evidence["terminal_code"] == "offline_test_stop_before_post"


def test_execute_live_records_read_only_job_rows_when_post_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_after_job_was_recorded(
        cases: tuple[runner.RunCase, ...],
        ledger: runner.AttemptLedger,
        _adapter: Any,
    ) -> None:
        case = cases[0]
        request_id = runner.client_request_id_for(APPROVAL_ID, case)
        ledger.before_post(case, request_id)
        ledger.mark_accepted(case, request_id, "workflow-audit-1", "run-audit-1")
        run_dir = context.state_root / "run-audit-1"
        run_dir.mkdir()
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute("CREATE TABLE jobs (job_id TEXT NOT NULL)")
            connection.execute("INSERT INTO jobs VALUES (?)", ("job-audit-0001",))
        raise runner.RunnerAbort("FAIL", "codex_job_event_trace_invalid")

    context, _trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path,
        monkeypatch,
        run_execution=fail_after_job_was_recorded,
    )

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    assert ledger.post_count == 1
    assert ledger.job_count == 0
    evidence = evidence_rows[0]
    assert evidence["status"] == "FAIL"
    assert evidence["budget"]["verified_completed_job_count"] == 0
    assert evidence["runs"][0]["verified_completed_job_count"] == 0
    assert evidence["read_only_ledger_job_rows"] == 1


@pytest.mark.parametrize("fault", ["unsafe_layout", "ledger_unavailable"])
def test_read_only_job_ledger_audit_records_unknown_instead_of_guessing_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    context, _localappdata, _codexbridge_root, state_root = _state_root_context(tmp_path)
    monkeypatch.setattr(runner, "_same_owner_as_current_user", lambda _path: True)
    runner._initialize_isolated_state_root(context)
    context.process = SimpleNamespace(poll=lambda: 0)
    context.identity = {"pid": 17, "create_time": 1.0}
    original_validate = runner.validate_isolated_layout
    count_calls = 0

    def unavailable_layout(*_args: Any, **_kwargs: Any) -> None:
        raise runner.RunnerAbort("UNKNOWN", "isolated_root_unavailable")

    def unavailable_ledger(_state_root: Path) -> int:
        nonlocal count_calls
        count_calls += 1
        raise runner.RunnerAbort("UNKNOWN", "isolated_run_ledger_unavailable")

    monkeypatch.setattr(
        runner,
        "validate_isolated_layout",
        unavailable_layout if fault == "unsafe_layout" else original_validate,
    )
    monkeypatch.setattr(runner, "count_isolated_codex_jobs", unavailable_ledger)

    observed = runner._read_only_owned_job_ledger_rows(
        context,
        bridge_started=True,
        bridge_shutdown_confirmed=True,
        worker_observer_finished=True,
    )

    assert observed is None
    assert count_calls == (0 if fault == "unsafe_layout" else 1)
    assert state_root.is_dir()


@pytest.mark.parametrize(
    ("fault", "expected_stage"),
    [
        ("localappdata_mismatch", "prestart_layout_validated"),
        ("preexisting_codexbridge", "context_created"),
        ("owner_mismatch", "initializing_isolated_state"),
        ("permission_denied", "initializing_isolated_state"),
        ("mkdir_failure", "initializing_isolated_state"),
    ],
)
def test_execute_live_is_fail_closed_before_mock_bridge_for_state_setup_faults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected_stage: str,
) -> None:
    context, trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch
    )
    localappdata = Path(context.environment["LOCALAPPDATA"])
    state_root = context.state_root
    codexbridge_root = state_root.parent
    if fault == "localappdata_mismatch":
        context.environment["LOCALAPPDATA"] = context.environment["APPDATA"]
    elif fault == "preexisting_codexbridge":
        codexbridge_root.mkdir()
    elif fault == "owner_mismatch":
        monkeypatch.setattr(
            runner,
            "_same_owner_as_current_user",
            lambda path: Path(path) != localappdata,
        )
    elif fault == "permission_denied":
        real_access = runner.os.access
        monkeypatch.setattr(
            runner.os,
            "access",
            lambda path, mode, *args, **kwargs: (
                False if Path(path) == localappdata else real_access(path, mode, *args, **kwargs)
            ),
        )
    elif fault == "mkdir_failure":
        real_mkdir = Path.mkdir

        def fail_state_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == state_root:
                raise PermissionError("fake directory creation failure")
            real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_state_mkdir)

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    assert ledger.post_count == 0
    assert "bridge_start" not in trace
    assert "execute_runs" not in trace
    assert len(evidence_rows) == 1
    evidence = evidence_rows[0]
    assert evidence["gate0_stage"] == expected_stage
    assert evidence["owned_bridge_started"] is False
    assert evidence["owned_bridge_shutdown_confirmed"] is False
    assert evidence["read_only_ledger_job_rows"] is None


def test_execute_live_records_unknown_start_and_cleanup_without_stopping_unowned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ambiguous_start(_context: Any, _process: Any, _identity: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("fake start result unavailable")

    context, trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch, bridge_start=ambiguous_start
    )

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    assert context.process is None
    assert ledger.post_count == 0
    assert "stop_owned" not in trace
    assert evidence_rows[0]["gate0_stage"] == "starting_bridge"
    assert evidence_rows[0]["owned_bridge_started"] is None
    assert evidence_rows[0]["owned_bridge_shutdown_confirmed"] is None
    assert evidence_rows[0]["read_only_ledger_job_rows"] is None
    assert evidence_rows[0]["terminal_code"] == "owned_bridge_start_outcome_unknown"
    assert evidence_rows[0]["status"] == "UNKNOWN"


def test_execute_live_records_cleanup_failure_as_unknown_without_killing_unverified_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_stop(_process: Any) -> None:
        raise RuntimeError("fake stop result unavailable")

    _context, trace, evidence_rows, ledger, _process = _install_fake_execute_live(
        tmp_path, monkeypatch, bridge_stop=fail_stop
    )

    result = runner.execute_live(HEAD, APPROVAL_ID)

    assert result == 2
    assert ledger.post_count == 0
    assert trace[-2:] == ["verify_owner", "stop_owned"]
    assert evidence_rows[0]["owned_bridge_started"] is True
    assert evidence_rows[0]["owned_bridge_shutdown_confirmed"] is None
    assert evidence_rows[0]["read_only_ledger_job_rows"] is None
    assert evidence_rows[0]["terminal_code"] == "owned_shutdown_unconfirmed"
    assert evidence_rows[0]["status"] == "BLOCKED"


def test_auth_source_requires_owned_regular_bounded_file(tmp_path: Path) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    auth_file = source_home / "auth.json"
    auth_file.write_text("fake-auth-content", encoding="utf-8")

    runner.validate_auth_source(
        source_home,
        auth_file,
        owner_matches=True,
        maximum_bytes=128,
    )
    with pytest.raises(runner.RunnerAbort, match="isolated_codex_auth_preflight_failed"):
        runner.validate_auth_source(
            source_home,
            auth_file,
            owner_matches=False,
            maximum_bytes=128,
        )


def test_cumulative_job_count_uses_read_only_run_ledgers(tmp_path: Path) -> None:
    for name, count in (("run-a", 1), ("run-b", 2)):
        run_dir = tmp_path / name
        run_dir.mkdir()
        with sqlite3.connect(run_dir / "codex_jobs.sqlite") as connection:
            connection.execute("CREATE TABLE jobs (job_id TEXT NOT NULL)")
            connection.executemany(
                "INSERT INTO jobs (job_id) VALUES (?)", [(f"{name}-{i}",) for i in range(count)]
            )

    assert runner.count_isolated_codex_jobs(tmp_path) == 3


def test_owner_mismatch_prevents_bridge_shutdown() -> None:
    calls: list[str] = []

    def reject_owner(_process: object, _identity: dict[str, Any]) -> None:
        raise runner.RunnerAbort("BLOCKED", "owned_process_identity_mismatch")

    def stop(_process: object) -> None:
        calls.append("stop")

    with pytest.raises(runner.RunnerAbort, match="owned_process_identity_mismatch"):
        runner.stop_owned_process(
            object(), {"pid": 12}, verify_owner=reject_owner, stop_process=stop
        )
    assert calls == []


def test_graph_projection_and_event_node_ids_must_match_fixed_case() -> None:
    case = _cases()[0]
    tasks = {
        "workflow_id": "workflow-A",
        "run_id": "run-A",
        "coverage": "complete",
        "truncated": False,
        "nodes": [
            {
                "node_id": node_id,
                "operation": "call" if node_id in case.call_node_ids else "set",
                "executions": (
                    [
                        {
                            "execution_id": "execution-A-1",
                            "state": "succeeded",
                            "observation_complete": True,
                            "job_id": "job-A-1",
                        }
                    ]
                    if node_id in case.call_node_ids
                    else []
                ),
            }
            for node_id in case.static_node_ids
        ],
    }
    events = {
        "workflow_id": "workflow-A",
        "run_id": "run-A",
        "coverage": "complete",
        "truncated": False,
        "has_more": False,
        "events": [{"node_id": case.call_node_ids[0], "event_type": "task_completed"}],
    }
    assert runner.validate_projection(case, "workflow-A", "run-A", tasks, events, tasks, events)

    bad_events = json.loads(json.dumps(events))
    bad_events["events"][0]["node_id"] = "/do/0/unexpected"
    with pytest.raises(runner.RunnerAbort):
        runner.validate_projection(case, "workflow-A", "run-A", tasks, bad_events, tasks, events)


@pytest.mark.parametrize(
    ("case_index", "observed"),
    [(0, ("C5-A",)), (1, ("C5-B1", "C5-B2")), (2, ("1", "2", "3"))],
)
def test_fixed_result_expectations_are_compared_without_persisting_text(
    case_index: int, observed: tuple[str, ...]
) -> None:
    assert runner.validate_expected_texts(_cases()[case_index], observed)
    with pytest.raises(runner.RunnerAbort, match="fixed_result_expectation_mismatch"):
        runner.validate_expected_texts(_cases()[case_index], (*observed, "unexpected"))


@pytest.mark.parametrize(
    "unsafe",
    [
        {"path": "D:\\CodexBridge\\secret"},
        {"summary": "Bearer very-secret-token"},
        {"final_text": "C5-A"},
        {"prompt": "Reply with exactly C5-A"},
    ],
)
def test_evidence_sanitizer_rejects_paths_secrets_and_raw_text(unsafe: dict[str, Any]) -> None:
    with pytest.raises(runner.RunnerAbort):
        runner.assert_safe_evidence(unsafe, secrets_to_reject=("very-secret-token",))


@pytest.mark.parametrize(
    "unsafe",
    [
        {"gate0_stage": "unexpected-stage"},
        {"gate0_stage": ["starting_bridge"]},
        {"owned_bridge_started": "yes"},
        {"owned_bridge_shutdown_confirmed": 1},
    ],
)
def test_evidence_sanitizer_requires_fixed_stage_and_process_ownership_values(
    unsafe: dict[str, Any],
) -> None:
    with pytest.raises(runner.RunnerAbort):
        runner.assert_safe_evidence(unsafe)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"read_only_ledger_job_rows": True},
        {"read_only_ledger_job_rows": -1},
        {"read_only_ledger_job_rows": runner.MAX_CODEX_JOBS + 1},
        {"read_only_ledger_job_rows": 1.5},
        {"budget": {"verified_completed_job_count": True}},
        {"budget": {"verified_completed_job_count": runner.MAX_CODEX_JOBS + 1}},
        {"runs": [{"expected_job_count": 1, "verified_completed_job_count": 2}]},
    ],
)
def test_evidence_sanitizer_bounds_verified_and_read_only_job_counts(
    unsafe: dict[str, Any],
) -> None:
    with pytest.raises(runner.RunnerAbort):
        runner.assert_safe_evidence(unsafe)


def test_evidence_sanitizer_accepts_unknown_and_budget_bounded_job_counts() -> None:
    runner.assert_safe_evidence({"read_only_ledger_job_rows": None})
    runner.assert_safe_evidence({"read_only_ledger_job_rows": 0})
    runner.assert_safe_evidence({"read_only_ledger_job_rows": runner.MAX_CODEX_JOBS})
    runner.assert_safe_evidence(
        {
            "budget": {"verified_completed_job_count": runner.MAX_CODEX_JOBS},
            "runs": [
                {
                    "expected_job_count": 3,
                    "verified_completed_job_count": 3,
                }
            ],
        }
    )


def test_evidence_paths_are_unique_and_validate_external_approval_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)

    evidence_path, summary_path = runner._live_evidence_paths(APPROVAL_ID)

    assert evidence_path == tmp_path / "docs" / f"phase_c5_workflow_acceptance-{APPROVAL_ID}.json"
    assert summary_path == tmp_path / "docs" / f"phase_c5_p2_live_acceptance-{APPROVAL_ID}.md"
    with pytest.raises(runner.RunnerAbort):
        runner._live_evidence_paths("../unsafe")


def test_live_preflight_and_writer_ignore_older_fixed_summary_without_overwriting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    new_approval_id = "c" * 32
    docs = tmp_path / "docs"
    docs.mkdir()
    old_summary = docs / "phase_c5_p2_live_acceptance.md"
    old_json = docs / f"phase_c5_workflow_acceptance-{APPROVAL_ID}.json"
    old_summary.write_text("previous fixed summary", encoding="utf-8")
    old_json.write_text('{"previous":"evidence"}', encoding="utf-8")
    old_summary_bytes = old_summary.read_bytes()
    old_json_bytes = old_json.read_bytes()
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_git_preflight", lambda _head: (HEAD, SOURCE_HASH))
    monkeypatch.setattr(
        runner,
        "_environment_preflight",
        lambda: (tmp_path / "codex.exe", tmp_path / "source-home"),
    )

    preflight = runner.live_preflight(HEAD, new_approval_id)
    evidence = runner._create_evidence(new_approval_id, preflight)
    json_target, summary_target = runner._live_evidence_paths(new_approval_id)
    runner._write_live_evidence(evidence, new_approval_id, ())

    assert json_target.is_file()
    assert summary_target.is_file()
    runner.assert_safe_evidence(json.loads(json_target.read_text(encoding="utf-8")))
    summary_text = summary_target.read_text(encoding="utf-8")
    assert not runner.WINDOWS_PATH_PATTERN.search(summary_text)
    assert not runner.POSIX_PATH_PATTERN.search(summary_text)
    assert old_summary.read_bytes() == old_summary_bytes
    assert old_json.read_bytes() == old_json_bytes


@pytest.mark.parametrize("existing_target", ["json", "summary"])
def test_live_preflight_rejects_only_existing_target_for_same_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_target: str
) -> None:
    approval_id = "d" * 32
    docs = tmp_path / "docs"
    docs.mkdir()
    json_target, summary_target = (
        tmp_path / "docs" / f"phase_c5_workflow_acceptance-{approval_id}.json",
        tmp_path / "docs" / f"phase_c5_p2_live_acceptance-{approval_id}.md",
    )
    target = json_target if existing_target == "json" else summary_target
    target.write_text("existing target", encoding="utf-8")
    before = target.read_bytes()
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_git_preflight", lambda _head: (HEAD, SOURCE_HASH))
    monkeypatch.setattr(
        runner,
        "_environment_preflight",
        lambda: (tmp_path / "codex.exe", tmp_path / "source-home"),
    )

    with pytest.raises(runner.RunnerAbort, match="live_evidence_output_already_exists"):
        runner.live_preflight(HEAD, approval_id)
    evidence = runner._create_evidence(
        approval_id, {"approved_head": HEAD, "runner_source_sha256": SOURCE_HASH}
    )
    with pytest.raises(runner.RunnerAbort, match="live_evidence_output_already_exists"):
        runner._write_live_evidence(evidence, approval_id, ())

    assert target.read_bytes() == before


def test_cli_default_and_prepare_never_enter_live_execution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def forbidden_live(*_args: Any, **_kwargs: Any) -> int:
        calls.append("live")
        raise AssertionError("live execution path must not be called")

    monkeypatch.setattr(runner, "execute_live", forbidden_live)
    monkeypatch.setattr(
        runner, "start_isolated_bridge", lambda *_args, **_kwargs: calls.append("bridge")
    )
    monkeypatch.setattr(
        runner, "initial_workflow_post", lambda *_args, **_kwargs: calls.append("post")
    )
    assert runner.main([]) == 0
    assert runner.main(["--prepare"]) == 0
    assert calls == []
    output = capsys.readouterr().out
    assert "offline" in output.casefold() or "usage" in output.casefold()


def test_execute_without_both_guards_is_blocked_before_live_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runner, "execute_live", lambda *_args, **_kwargs: calls.append("live") or 0)

    assert runner.main(["--execute"]) != 0
    assert calls == []
