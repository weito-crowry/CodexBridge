from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import phase_b5_workflow_isolated_smoke as runner
from scripts.phase5e_smoke_support import claim_evidence_path


def test_phase_b5_runner_exists_as_a_dedicated_artifact() -> None:
    runner = Path(__file__).resolve().parents[1] / "scripts" / "phase_b5_workflow_isolated_smoke.py"

    assert runner.is_file()


def test_fixed_workflow_and_comment_revision_keep_canonical_hash() -> None:
    fixture = (
        Path(__file__).resolve().parents[1] / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"
    )
    source = runner.build_fixed_workflow(fixture.read_text(encoding="utf-8"))
    revision_two = runner.add_comment_revision(source)

    definition = yaml.safe_load(source)
    assert definition["do"][0]["calculate"]["with"]["prompt"] == runner.EXPECTED_PROMPT
    assert runner.source_sha256(source) != runner.source_sha256(revision_two)
    assert runner.canonical_sha256(source) == runner.canonical_sha256(revision_two)

    conflict_payload = runner.add_mutation_conflict_comment(revision_two)
    assert runner.source_sha256(revision_two) != runner.source_sha256(conflict_payload)
    assert runner.canonical_sha256(revision_two) == runner.canonical_sha256(conflict_payload)


def test_post_budget_allows_only_one_initial_and_exact_same_id_replay() -> None:
    request = {
        "definition_id": "00000000-0000-4000-8000-000000000001",
        "revision": 2,
        "client_request_id": "b5-run-0123456789abcdef",
        "workspace_root": "D:/b5-workspace",
        "input": {},
    }
    budget = runner.PostBudget()
    budget.claim_initial(request)
    budget.mark_initial_accepted()
    budget.claim_same_id_replay(request)

    assert budget.initial_calls == 1
    assert budget.same_id_replay_calls == 1
    with pytest.raises(runner.SmokeAbort, match="same_id_mcp_replay_budget_exhausted"):
        budget.claim_same_id_replay(request)
    with pytest.raises(runner.SmokeAbort, match="initial_workflow_post_budget_exhausted"):
        budget.claim_initial(request)


def test_post_budget_rejects_changed_reference_and_replay_before_acceptance() -> None:
    request = {
        "definition_id": "00000000-0000-4000-8000-000000000001",
        "revision": 2,
        "client_request_id": "b5-run-0123456789abcdef",
        "workspace_root": "D:/b5-workspace",
        "input": {},
    }
    budget = runner.PostBudget()
    budget.claim_initial(request)
    with pytest.raises(runner.SmokeAbort, match="same_id_replay_requires_accepted_initial_run"):
        budget.claim_same_id_replay(request)
    budget.mark_initial_accepted()
    with pytest.raises(runner.SmokeAbort, match="same_id_replay_reference_changed"):
        budget.claim_same_id_replay({**request, "revision": 1})


def test_dynamic_ports_exclude_the_production_default_pair() -> None:
    assert not runner.ports_are_safe(18000, 28181)
    assert not runner.ports_are_safe(28181, 18001)
    assert runner.ports_are_safe(28181, 28182)


def test_port_reservations_are_released_before_free_port_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []

    class Reservation:
        def close(self) -> None:
            events.append("released")

    ports = (28181, 28182)
    reservations = [Reservation(), Reservation()]
    monkeypatch.setattr(
        runner.isolated,
        "_assert_ports_free",
        lambda observed: events.append(("checked", observed)),
    )

    runner._release_reserved_ports(ports, reservations)

    assert events == ["released", "released", ("checked", ports)]
    assert reservations == []


def test_initial_transport_unknown_is_never_retried() -> None:
    budget = runner.PostBudget()
    request = {"client_request_id": "b5-run-0123456789abcdef", "revision": 2}
    calls = 0
    before_send_calls = 0

    def transport(_request):
        nonlocal calls
        calls += 1
        raise TimeoutError

    def before_send() -> None:
        nonlocal before_send_calls
        before_send_calls += 1

    with pytest.raises(runner.SmokeAbort, match="initial_workflow_post_outcome_unknown"):
        runner.send_initial_once(budget, request, transport, before_send=before_send)

    assert calls == 1
    assert before_send_calls == 1
    assert budget.initial_calls == 1
    with pytest.raises(runner.SmokeAbort, match="initial_workflow_post_budget_exhausted"):
        runner.send_initial_once(budget, request, transport)
    assert calls == 1


def test_pytest_summary_counts_pass_fail_and_skip_exactly() -> None:
    summary = runner.parse_pytest_summary("= 43 passed, 2 failed, 5 skipped in 12.34s =")

    assert summary == {
        "passed": 43,
        "failed": 2,
        "skipped": 5,
        "errors": 0,
        "xfailed": 0,
        "xpassed": 0,
    }


def test_exception_diagnostic_records_safe_frame_without_message_or_path() -> None:
    try:
        raise ValueError("private-token and D:\\Users\\person\\private")
    except ValueError as error:
        diagnostic = runner._safe_exception_diagnostic(error)

    assert diagnostic["exception_type"] == "ValueError"
    assert (
        diagnostic["function"]
        == "test_exception_diagnostic_records_safe_frame_without_message_or_path"
    )
    assert type(diagnostic["line"]) is int
    assert "private-token" not in str(diagnostic)
    assert "D:\\Users" not in str(diagnostic)


def test_read_only_codex_job_ledger_uses_persisted_schema(tmp_path: Path) -> None:
    database = tmp_path / "codex_jobs.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (job_id TEXT, request_key TEXT, workflow_id TEXT, "
            "task_identity TEXT, state TEXT, thread_id TEXT, turn_id TEXT, "
            "remote_state TEXT, receipt_json TEXT)"
        )
        connection.execute("CREATE TABLE job_events (event_id INTEGER, job_id TEXT, state TEXT)")
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1",
                "workflow-1::do[0].calculate",
                "workflow-1",
                "do[0].calculate",
                "SUCCEEDED",
                "thread-1",
                "turn-1",
                "completed",
                '{"result":{"final_text":"B5_VERIFY_OK"}}',
            ),
        )
        connection.execute("INSERT INTO job_events VALUES (1, 'job-1', 'MAYBE_SENT')")
        connection.execute("INSERT INTO job_events VALUES (2, 'job-1', 'SUCCEEDED')")

    jobs, events = runner._read_codex_job_ledger(database)

    assert len(jobs) == 1
    assert jobs[0]["state"] == "SUCCEEDED"
    assert [event["state"] for event in events] == ["MAYBE_SENT", "SUCCEEDED"]


def test_evidence_path_is_attempt_unique_and_exclusive(tmp_path: Path) -> None:
    attempt_one = "0" * 31 + "1"
    attempt_two = "0" * 31 + "2"
    first = runner.evidence_path_for_attempt(tmp_path, attempt_one)
    second = runner.evidence_path_for_attempt(tmp_path, attempt_two)
    assert first != second

    claim_evidence_path(first, {"status": "NOT_RUN"})
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        claim_evidence_path(first, {"status": "FAIL"})
    assert first.read_bytes() == before


@pytest.mark.parametrize(
    "value",
    [
        {"prompt": runner.EXPECTED_PROMPT},
        {"authorization": "secret-token-value"},
        {"path": r"D:\\Users\\person\\private"},
    ],
)
def test_evidence_sanitizer_rejects_prompt_secrets_and_user_paths(value) -> None:
    secrets_to_reject = ("secret-token-value",) if "authorization" in value else ()
    with pytest.raises(runner.SmokeAbort):
        runner.assert_safe_evidence(value, secrets_to_reject=secrets_to_reject)


def test_graceful_shutdown_checks_mock_owned_process_and_never_forces_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 43210

        def poll(self):
            return None

    fake = FakeProcess()
    ownership_checks = []
    monkeypatch.setattr(
        runner.phase5f5,
        "_assert_bridge_identity",
        lambda process, identity: ownership_checks.append((process, identity)),
    )
    monkeypatch.setattr(runner.isolated, "_process_snapshot", lambda _pid: [(43211, 42.0)])
    monkeypatch.setattr(
        runner.isolated,
        "_stop_bridge",
        lambda process, **_kwargs: {
            "reaped": True,
            "forced_termination": False,
            "shutdown_http_status": 202,
        },
    )
    monkeypatch.setattr(runner.isolated, "_process_still_matches", lambda _pid, _created: False)

    result = runner.stop_owned_bridge(
        fake, {"pid": fake.pid}, ui_port=54321, control_token="x" * 32
    )

    assert ownership_checks == [(fake, {"pid": fake.pid})]
    assert result == {
        "reaped": True,
        "forced_termination": False,
        "descendant_count_before_shutdown": 1,
        "orphan_processes": 0,
    }


def _fake_wait_environment(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, object]],
    *,
    max_wait: float,
    transport_latency: float = 0.0,
    result_response: tuple[int, object] = (
        200,
        {"workflow_id": "workflow-test", "result": {"final_text": "B5_VERIFY_OK"}},
    ),
):
    clock = {"now": 0.0}
    response_index = {"value": 0}
    calls: list[tuple[str, str]] = []
    write_snapshots: list[dict[str, object]] = []

    def monotonic() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    def fake_http_json(_context, method, route, **_kwargs):
        assert method == "GET"
        calls.append((method, route))
        clock["now"] += transport_latency
        if route.endswith("/result"):
            status, body = result_response
            return status, body, {}
        index = response_index["value"]
        response_index["value"] += 1
        status, body = responses[index]
        return status, body, {}

    def fake_safe_write(_writer, evidence, **_kwargs) -> None:
        runner.assert_safe_evidence(evidence, secrets_to_reject=("control-test", "mcp-test"))
        write_snapshots.append(deepcopy(evidence.get("saved_execution_gate", {})))

    monkeypatch.setattr(runner.time, "monotonic", monotonic)
    monkeypatch.setattr(runner.time, "sleep", sleep)
    monkeypatch.setattr(runner, "MAX_WORKFLOW_WAIT_SECONDS", max_wait)
    monkeypatch.setattr(runner, "_http_json", fake_http_json)
    monkeypatch.setattr(runner, "_safe_write", fake_safe_write)
    context = SimpleNamespace(control_token="control-test", mcp_token="mcp-test")
    evidence: dict[str, object] = {"saved_execution_gate": {}}

    def wait():
        return runner._wait_workflow_success(
            context,
            workflow_id="workflow-test",
            evidence=evidence,
            writer=object(),
        )

    return wait, evidence, calls, write_snapshots, clock


def test_wait_workflow_success_recovers_after_transient_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, writes, _clock = _fake_wait_environment(
        monkeypatch,
        [
            (200, {"status": "needs_reconciliation"}),
            (200, {"status": "running"}),
            (200, {"status": "succeeded"}),
        ],
        max_wait=2.0,
        transport_latency=0.125,
    )

    detail, result = wait()

    assert detail == {"status": "succeeded"}
    assert result["result"]["final_text"] == "B5_VERIFY_OK"
    assert [method for method, _route in calls] == ["GET", "GET", "GET", "GET"]
    assert calls[-1][1].endswith("/result")
    observations = evidence["saved_execution_gate"]["status_observations"]
    assert [item["status"] for item in observations["transitions"]] == [
        "needs_reconciliation",
        "running",
        "succeeded",
    ]
    assert [item["first_elapsed_ms"] for item in observations["transitions"]] == [
        125,
        625,
        1125,
    ]
    assert observations["omitted_count"] == 0
    assert len(writes) == 3


def test_wait_workflow_success_blocks_when_reconciliation_is_unresolved_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(200, {"status": "needs_reconciliation"})] * 2,
        max_wait=1.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_reconciliation_unresolved_at_deadline"):
        wait()

    gate = evidence["saved_execution_gate"]
    assert gate["workflow_status"] == "needs_reconciliation"
    assert gate["result_gate"] == "NOT_OBSERVED"
    assert len(calls) == 2
    assert all(method == "GET" for method, _route in calls)
    observation = gate["status_observations"]["transitions"][0]
    assert observation["count"] == 2
    assert observation["first_elapsed_ms"] == 0
    assert observation["last_elapsed_ms"] == 500


@pytest.mark.parametrize("failure_status", ["failed", "failed_to_start"])
def test_wait_workflow_success_blocks_immediately_when_reconciliation_becomes_failed(
    monkeypatch: pytest.MonkeyPatch,
    failure_status: str,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [
            (200, {"status": "needs_reconciliation"}),
            (200, {"status": failure_status}),
        ],
        max_wait=10.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_did_not_reach_confirmed_success"):
        wait()

    assert evidence["saved_execution_gate"]["workflow_status"] == failure_status
    assert len(calls) == 2
    assert all(method == "GET" for method, _route in calls)
    assert not any(route.endswith("/result") for _method, route in calls)


def test_wait_workflow_success_classifies_unknown_at_deadline_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(200, {"status": "PRIVATE_UNKNOWN_STATUS"})] * 2,
        max_wait=1.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_reconciliation_unresolved_at_deadline"):
        wait()

    gate = evidence["saved_execution_gate"]
    assert gate["workflow_status"] == "unknown"
    assert gate["result_gate"] == "NOT_OBSERVED"
    assert "PRIVATE_UNKNOWN_STATUS" not in str(evidence)
    assert gate["status_observations"]["transitions"][0]["count"] == 2
    assert all(method == "GET" for method, _route in calls)


def test_wait_workflow_success_keeps_normal_timeout_when_only_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(200, {"status": "running"})] * 2,
        max_wait=1.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_result_wait_timed_out"):
        wait()

    gate = evidence["saved_execution_gate"]
    assert gate["workflow_status"] == "running"
    assert gate["result_gate"] == "NOT_OBSERVED"
    assert len(calls) == 2


def test_wait_workflow_success_times_out_as_running_after_reconciliation_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [
            (200, {"status": "needs_reconciliation"}),
            (200, {"status": "running"}),
        ],
        max_wait=1.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_result_wait_timed_out"):
        wait()

    assert evidence["saved_execution_gate"]["workflow_status"] == "running"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("http_status", "body"),
    [
        (503, {"detail": "PRIVATE_RESPONSE_BODY"}),
        (200, ["PRIVATE_RESPONSE_BODY"]),
    ],
)
def test_wait_workflow_success_blocks_on_unreadable_status_without_body_leak(
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
    body: object,
) -> None:
    wait, evidence, calls, writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(http_status, body)],
        max_wait=10.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_status_readback_unavailable"):
        wait()

    gate = evidence["saved_execution_gate"]
    assert gate["status_observations"]["transitions"] == [
        {
            "first_elapsed_ms": 0,
            "last_elapsed_ms": 0,
            "status": "unknown",
            "http_status": http_status,
            "count": 1,
        }
    ]
    assert "PRIVATE_RESPONSE_BODY" not in str(gate)
    assert len(calls) == 1
    assert writes


def test_wait_workflow_success_normalizes_unknown_status_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [
            (200, {"status": "PRIVATE_UNKNOWN_STATUS"}),
            (200, {"status": "succeeded"}),
        ],
        max_wait=2.0,
    )

    _detail, result = wait()

    observations = evidence["saved_execution_gate"]["status_observations"]
    assert [item["status"] for item in observations["transitions"]] == ["unknown", "succeeded"]
    assert "PRIVATE_UNKNOWN_STATUS" not in str(evidence)
    assert result["result"]["final_text"] == "B5_VERIFY_OK"
    assert all(method == "GET" for method, _route in calls)


def test_wait_workflow_success_records_transport_error_without_changing_safe_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait, evidence, _calls, writes, _clock = _fake_wait_environment(
        monkeypatch,
        [],
        max_wait=10.0,
    )
    calls: list[tuple[str, str]] = []

    def failed_transport(_context, method, route, **_kwargs):
        assert method == "GET"
        calls.append((method, route))
        raise runner.SmokeAbort("BLOCKED", "control_api_transport_outcome_unknown")

    monkeypatch.setattr(runner, "_http_json", failed_transport)

    with pytest.raises(runner.SmokeAbort, match="control_api_transport_outcome_unknown"):
        wait()

    observations = evidence["saved_execution_gate"]["status_observations"]
    assert observations["transitions"] == [
        {
            "first_elapsed_ms": 0,
            "last_elapsed_ms": 0,
            "status": "unknown",
            "http_status": None,
            "count": 1,
        }
    ]
    assert calls == [("GET", "/ui-api/control/workflows/workflow-test")]
    assert writes


@pytest.mark.parametrize(
    "result_response",
    [
        (503, {"detail": "PRIVATE_RESULT_BODY"}),
        (200, ["PRIVATE_RESULT_BODY"]),
    ],
)
def test_wait_workflow_success_requires_result_200_dict(
    monkeypatch: pytest.MonkeyPatch,
    result_response: tuple[int, object],
) -> None:
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(200, {"status": "succeeded"})],
        max_wait=2.0,
        result_response=result_response,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_result_endpoint_mismatch"):
        wait()

    assert [method for method, _route in calls] == ["GET", "GET"]
    assert calls[-1][1].endswith("/result")
    assert (
        evidence["saved_execution_gate"]["status_observations"]["transitions"][0]["status"]
        == "succeeded"
    )
    assert "PRIVATE_RESULT_BODY" not in str(evidence)


def test_wait_workflow_success_bounds_status_observation_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = ["queued" if index % 2 == 0 else "running" for index in range(70)]
    wait, evidence, calls, _writes, _clock = _fake_wait_environment(
        monkeypatch,
        [(200, {"status": status}) for status in statuses],
        max_wait=35.0,
    )

    with pytest.raises(runner.SmokeAbort, match="workflow_result_wait_timed_out"):
        wait()

    observations = evidence["saved_execution_gate"]["status_observations"]
    assert len(observations["transitions"]) == 64
    assert observations["omitted_count"] == 6
    assert len(calls) == 70
    assert all(method == "GET" for method, _route in calls)
