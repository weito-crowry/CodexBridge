from __future__ import annotations

import sqlite3
from pathlib import Path

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
