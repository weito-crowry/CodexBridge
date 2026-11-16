from __future__ import annotations

import json
import sqlite3
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.interpreter import Interpreter, compute_task_action
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
    root = tmp_path / "isolated"
    root.mkdir()
    state = root / "state"
    workspace = root / "workspace"
    state.mkdir()
    workspace.mkdir()
    runner.validate_isolated_layout(root, state, workspace, (24001, 24002))

    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(root, state, workspace, (18000, 24002))
    with pytest.raises(runner.RunnerAbort):
        runner.validate_isolated_layout(root, root, workspace, (24001, 24002))


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
