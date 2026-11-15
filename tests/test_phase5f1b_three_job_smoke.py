from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from codex_bridge.workflows.codex_job_adapter import CodexJobAdapter

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "phase5f1b_three_job_isolated_smoke.py"


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    if not SCRIPT_PATH.is_file():
        pytest.skip("the Phase 5F-1B isolated runner is not implemented")
    spec = importlib.util.spec_from_file_location(
        "scripts.phase5f1b_three_job_isolated_smoke", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase5f1b_runner_exists() -> None:
    assert SCRIPT_PATH.is_file(), "the Phase 5F-1B isolated runner is not implemented"


def test_fixed_fixture_and_input_are_pinned(smoke: ModuleType) -> None:
    fixture_text, definition, issues = smoke.load_fixed_workflow()

    normalized = fixture_text.replace("\r\n", "\n").encode("utf-8")
    assert hashlib.sha256(normalized).hexdigest() == smoke.FIXTURE_SHA256
    assert definition["document"]["name"] == "three-codex-job-loop"
    assert issues == (
        {"number": 5, "enabled": False},
        {"number": 9, "enabled": True},
        {"number": 17, "enabled": True},
        {"number": 21, "enabled": True},
    )


def test_offline_preflight_rejects_all_five_invalid_cases(smoke: ModuleType) -> None:
    _fixture_text, definition, _issues = smoke.load_fixed_workflow()

    assert smoke._preflight_negative_cases(definition) == (
        "five_issues",
        "four_enabled",
        "duplicate_number",
        "wrong_type",
        "extra_task",
    )


def test_request_body_uses_fixed_three_job_mode(smoke: ModuleType) -> None:
    fixture_text, _definition, _issues = smoke.load_fixed_workflow()
    encoded = smoke.build_three_job_request_body(
        workflow_yaml=fixture_text,
        client_request_id="phase5f1b-test-request",
        workspace_root="C:/isolated/workspace",
    )

    request = json.loads(encoded)
    assert request == {
        "client_request_id": "phase5f1b-test-request",
        "workflow_yaml": fixture_text,
        "input": smoke.FIXED_INPUT,
        "workspace_root": "C:/isolated/workspace",
        "max_live_codex_jobs": 3,
    }


def test_result_contract_requires_exact_skip_and_three_job_projection(
    smoke: ModuleType,
) -> None:
    jobs = [
        {
            "job_id": f"job-{number}",
            "task_identity": (f"do[0].processIssues.iteration[{index}].do[1].processIssue"),
            "request_key": (
                f"workflow-1::do[0].processIssues.iteration[{index}].do[1].processIssue"
            ),
        }
        for index, number in ((1, 9), (2, 17), (3, 21))
    ]
    result = {
        "workflow_id": "workflow-1",
        "definition_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "result": [
            {
                "number": 5,
                "status": "skipped",
                "job_id": None,
                "request_key": None,
                "final_text": None,
            },
            {
                "number": 9,
                "status": "SUCCEEDED",
                "job_id": "job-9",
                "request_key": jobs[0]["request_key"],
                "final_text": "9",
            },
            {
                "number": 17,
                "status": "SUCCEEDED",
                "job_id": "job-17",
                "request_key": jobs[1]["request_key"],
                "final_text": "17",
            },
            {
                "number": 21,
                "status": "SUCCEEDED",
                "job_id": "job-21",
                "request_key": jobs[2]["request_key"],
                "final_text": "21",
            },
        ],
    }

    assert smoke.validate_three_job_result(result, workflow_id="workflow-1", jobs=jobs) == [
        "9",
        "17",
        "21",
    ]
    result["result"].pop()
    with pytest.raises(smoke.SmokeAbort):
        smoke.validate_three_job_result(result, workflow_id="workflow-1", jobs=jobs)


def test_event_order_allows_only_one_sequential_dispatch(smoke: ModuleType) -> None:
    jobs = [{"job_id": f"job-{number}"} for number in (9, 17, 21)]
    events = []
    for number in (9, 17, 21):
        job_id = f"job-{number}"
        events.extend(
            {"event_id": len(events) + 1, "job_id": job_id, "state": state}
            for state in ("PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED")
        )

    maximum_active, dispatch_counts = smoke.validate_job_event_order(events, jobs)

    assert maximum_active == 1
    assert dispatch_counts == {"job-9": 1, "job-17": 1, "job-21": 1}

    overlapping = [
        *events[:4],
        {"event_id": 5, "job_id": "job-17", "state": "MAYBE_SENT"},
        *events[4:],
    ]
    with pytest.raises(smoke.SmokeAbort):
        smoke.validate_job_event_order(overlapping, jobs)


def test_event_order_rejects_unknown_states_and_invalid_transitions(smoke: ModuleType) -> None:
    jobs = [{"job_id": f"job-{number}"} for number in (9, 17, 21)]
    events = []
    for number in (9, 17, 21):
        job_id = f"job-{number}"
        events.extend(
            {"event_id": len(events) + 1, "job_id": job_id, "state": state}
            for state in ("PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED")
        )

    unknown_state = [
        {"event_id": event_id, "job_id": event["job_id"], "state": event["state"]}
        for event_id, event in enumerate(
            [*events[:2], {"job_id": "job-9", "state": "SURPRISE"}, *events[2:]],
            start=1,
        )
    ]
    invalid_transition = [
        events[0],
        events[1],
        {"event_id": 3, "job_id": "job-9", "state": "SUCCEEDED"},
        *events[3:],
    ]
    for invalid_events in (unknown_state, invalid_transition):
        with pytest.raises(smoke.SmokeAbort):
            smoke.validate_job_event_order(invalid_events, jobs)


def test_r1_evidence_path_is_separate_and_requires_an_explicit_retest(
    smoke: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_path = tmp_path / "phase5f1b-three-job-e2e-evidence.json"
    r1_path = tmp_path / "phase5f1b-three-job-e2e-r1-evidence.json"
    monkeypatch.setattr(smoke, "EVIDENCE_PATH", initial_path)
    monkeypatch.setattr(smoke, "R1_EVIDENCE_PATH", r1_path)

    assert smoke._select_evidence_path(retest_once=False) == initial_path
    with pytest.raises(smoke.SmokeAbort):
        smoke._select_evidence_path(retest_once=True)

    initial_path.write_text("original evidence", encoding="utf-8")
    assert smoke._select_evidence_path(retest_once=True) == r1_path
    with pytest.raises(smoke.SmokeAbort):
        smoke._select_evidence_path(retest_once=False)

    r1_path.write_text("first R1 evidence", encoding="utf-8")
    with pytest.raises(smoke.SmokeAbort):
        smoke._select_evidence_path(retest_once=True)
    initial_path.unlink()
    with pytest.raises(smoke.SmokeAbort):
        smoke._select_evidence_path(retest_once=False)
    assert not initial_path.exists()
    assert r1_path.read_text(encoding="utf-8") == "first R1 evidence"


def test_r1_workflow_get_observations_are_bounded_and_redacted(smoke: ModuleType) -> None:
    token = "secret-token-value"
    prompt = "private prompt body"
    payload = {
        "status": "running",
        "worker_state": "running",
        "jobs": [
            {"state": "SUCCEEDED", "prompt": prompt},
            {"state": "MAYBE_SENT", "prompt": prompt},
        ],
        "result": {"final_text": prompt},
    }
    counters = {
        "http_get": 0,
        "workflow_http_get": 0,
        "other_http_get": 0,
        "qt_get": 0,
        "http_post": 0,
        "workflow_post": 0,
        "shutdown_post": 0,
        "other_http_post": 0,
    }
    observations = {"items": [], "dropped": 0}
    wrapped = smoke._counted_http_json(
        lambda *_args, **_kwargs: (200, payload), counters, observations
    )

    for _ in range(smoke.MAX_WORKFLOW_STATE_OBSERVATIONS + 2):
        status, response = wrapped(
            12345,
            "GET",
            "/ui-api/control/workflows/private-workflow-id",
            token=token,
        )
        assert status == 200
        assert response is payload

    assert counters["http_get"] == smoke.MAX_WORKFLOW_STATE_OBSERVATIONS + 2
    assert counters["workflow_http_get"] == smoke.MAX_WORKFLOW_STATE_OBSERVATIONS + 2
    assert len(observations["items"]) == smoke.MAX_WORKFLOW_STATE_OBSERVATIONS
    assert observations["dropped"] == 2
    first = observations["items"][0]
    assert first["endpoint"] == "detail"
    assert first["http_status"] == 200
    assert first["workflow_status"] == "running"
    assert first["worker_state"] == "running"
    assert first["job_statuses"] == ["SUCCEEDED", "MAYBE_SENT"]
    encoded = json.dumps(observations)
    assert token not in encoded
    assert prompt not in encoded
    assert "private-workflow-id" not in encoded


def test_r1_workflow_listing_observation_keeps_only_bounded_statuses(
    smoke: ModuleType,
) -> None:
    counters = {"http_get": 0, "workflow_http_get": 0, "other_http_get": 0}
    observations = {"items": [], "dropped": 0}
    payload = {
        "workflows": [
            {"workflow_id": "private-workflow-id", "status": "needs_reconciliation"},
            {"workflow_id": "another-id", "status": "running"},
        ]
    }
    wrapped = smoke._counted_http_json(
        lambda *_args, **_kwargs: (200, payload), counters, observations
    )

    wrapped(12345, "GET", "/ui-api/control/workflows?limit=50", token="secret-token")

    assert len(observations["items"]) == 1
    item = observations["items"][0]
    assert item["observed_at_utc"].endswith("+00:00")
    assert item["endpoint"] == "list"
    assert item["http_status"] == 200
    assert item["workflow_count"] == 2
    assert item["workflow_statuses"] == ["needs_reconciliation", "running"]
    assert item["truncated_workflow_count"] == 0
    serialized = json.dumps(observations)
    assert "private-workflow-id" not in serialized
    assert "secret-token" not in serialized


def test_main_blocks_existing_initial_or_r1_evidence_without_running_e2e(
    smoke: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial_path = tmp_path / "phase5f1b-three-job-e2e-evidence.json"
    r1_path = tmp_path / "phase5f1b-three-job-e2e-r1-evidence.json"
    initial_path.write_text("original evidence", encoding="utf-8")
    r1_path.write_text("first R1 evidence", encoding="utf-8")
    monkeypatch.setattr(smoke, "EVIDENCE_PATH", initial_path)
    monkeypatch.setattr(smoke, "R1_EVIDENCE_PATH", r1_path)
    monkeypatch.setattr(
        smoke,
        "_run_e2e",
        lambda *_args, **_kwargs: pytest.fail("the live E2E runner must not start"),
    )

    assert smoke.main([]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert smoke.main(["--retest-once"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert initial_path.read_text(encoding="utf-8") == "original evidence"
    assert r1_path.read_text(encoding="utf-8") == "first R1 evidence"


def test_r1_preflight_hashes_initial_and_postmortem_evidence(
    smoke: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    names = {
        "phase5e-0-evidence.json",
        "phase5e-2c2-evidence.json",
        "phase5e-2c2-retest-evidence.json",
        "phase5e-2c2-diagnostic-retest-evidence.json",
        "phase5e-2c2-r4-evidence.json",
        "phase5f1b-three-job-e2e-evidence.json",
        "phase5f1b-postmortem-addendum.json",
    }
    for name in names:
        (docs / name).write_text(name, encoding="utf-8")

    hashes = smoke._existing_evidence_hashes()

    assert set(hashes) == {f"docs/{name}" for name in names}


def test_r1_preflight_requires_both_initial_and_postmortem_evidence(
    smoke: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    required_names = (
        "phase5e-0-evidence.json",
        "phase5e-2c2-evidence.json",
        "phase5e-2c2-retest-evidence.json",
        "phase5e-2c2-diagnostic-retest-evidence.json",
        "phase5e-2c2-r4-evidence.json",
        "phase5f1b-three-job-e2e-evidence.json",
    )
    for name in required_names:
        (docs / name).write_text(name, encoding="utf-8")

    with pytest.raises(smoke.SmokeAbort):
        smoke._existing_evidence_hashes(require_phase5f1b_evidence=True)

    addendum = docs / "phase5f1b-postmortem-addendum.json"
    addendum.write_text("postmortem evidence", encoding="utf-8")
    hashes = smoke._existing_evidence_hashes(require_phase5f1b_evidence=True)

    assert "docs/phase5f1b-three-job-e2e-evidence.json" in hashes
    assert "docs/phase5f1b-postmortem-addendum.json" in hashes


def test_cleanup_reports_reaped_only_for_known_process_identities(
    smoke: ModuleType,
) -> None:
    initial = {"pid": 1234, "created_at_epoch": 1794482814.016682}
    restart = {"pid": 4400, "created_at_epoch": 1794483000.0}

    assert smoke._owned_children_reaped([], []) is False
    assert smoke._owned_children_reaped([initial], []) is False
    assert (
        smoke._owned_children_reaped(
            [initial],
            [
                {
                    "pid": initial["pid"],
                    "created_at_epoch": initial["created_at_epoch"],
                    "reaped": False,
                }
            ],
        )
        is False
    )
    assert smoke._owned_children_reaped(
        [initial],
        [{"pid": initial["pid"], "created_at_epoch": initial["created_at_epoch"], "reaped": True}],
    )
    assert (
        smoke._owned_children_reaped(
            [initial, restart],
            [
                {
                    "pid": initial["pid"],
                    "created_at_epoch": initial["created_at_epoch"],
                    "reaped": True,
                },
                {
                    "pid": restart["pid"],
                    "created_at_epoch": restart["created_at_epoch"],
                    "reaped": False,
                },
            ],
        )
        is False
    )


def _write_valid_saved_state(
    smoke: ModuleType,
    root: Path,
    *,
    request_state: str = "succeeded",
    identity_mismatch: str | None = None,
) -> dict[str, Any]:
    state_root = root / "localappdata" / "CodexBridge" / "workflows"
    state_root.mkdir(parents=True)
    workspace_root = root / "workspace"
    workspace_root.mkdir()
    workflow_id = "11111111-2222-4333-8444-555555555555"
    run_id = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    client_request_id = "phase5f1b-offline-request"
    ui_port = 62806
    run_dir = state_root / run_id
    run_dir.mkdir()

    _fixture_text, definition, _issues = smoke.load_fixed_workflow()
    definition_text = (
        json.dumps(
            definition, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    input_data = smoke.FIXED_INPUT
    workspace = str(workspace_root.resolve(strict=True))

    def canonical(value: object) -> str:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )

    request_digest = hashlib.sha256(
        canonical(
            {
                "workflow": definition,
                "input": input_data,
                "workspace_root": workspace,
                "max_live_codex_jobs": 3,
            }
        ).encode("utf-8")
    ).hexdigest()
    definition_sha256 = hashlib.sha256(definition_text.encode("utf-8")).hexdigest()
    input_sha256 = hashlib.sha256(canonical(input_data).encode("utf-8")).hexdigest()
    manifest = {
        "workflow_id": workflow_id,
        "run_id": run_id,
        "request_digest": request_digest,
        "workspace_root": workspace,
        "workflow_source": "ui-api",
        "mode": "codex-job-live",
        "definition_sha256": definition_sha256,
        "input_sha256": input_sha256,
        "control": {
            "codex_job_live": True,
            "max_live_codex_jobs": 3,
            "codex_job_max_polls": 600,
            "codex_job_poll_interval_seconds": 0.2,
        },
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "workflow.yaml").write_text(definition_text, encoding="utf-8")
    (run_dir / "input.json").write_text(json.dumps(input_data), encoding="utf-8")
    (run_dir / "worker-entry-status.json").write_text(
        json.dumps({"state": "completed", "exit_code": 0}), encoding="utf-8"
    )

    requests_db = state_root / "requests.sqlite"
    with sqlite3.connect(requests_db) as connection:
        connection.execute(
            "CREATE TABLE workflow_requests ("
            "client_request_id TEXT NOT NULL UNIQUE, workflow_id TEXT NOT NULL UNIQUE, "
            "run_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, state TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        request_row = {
            "client_request_id": client_request_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "request_digest": request_digest,
            "state": request_state,
        }
        if identity_mismatch is not None:
            request_row[identity_mismatch] = f"mismatched-{identity_mismatch}"
        connection.execute(
            "INSERT INTO workflow_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_row["client_request_id"],
                request_row["workflow_id"],
                request_row["run_id"],
                request_digest,
                request_state,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    jobs_db = run_dir / "codex_jobs.sqlite"
    adapter = CodexJobAdapter(jobs_db)
    job_rows: list[dict[str, str]] = []
    with sqlite3.connect(jobs_db) as connection:
        connection.row_factory = sqlite3.Row
        for number, task_identity in zip((9, 17, 21), smoke.EXPECTED_TASK_IDENTITIES, strict=True):
            job_id = f"job-{number}"
            request_key = f"{workflow_id}::{task_identity}"
            thread_id = f"thread-{number}"
            turn_id = f"turn-id-{number}"
            job_request = {
                "client_request_id": f"phase5f1b-offline-{number}",
                "cwd": workspace,
                "prompt": f"Reply with only the synthetic issue number: {number}",
                "title": f"phase5f1b-offline-{number}",
                "sandbox_mode": "inherit",
                "confirm_full_access": False,
            }
            job_digest = hashlib.sha256(
                canonical(
                    {
                        "host": smoke.r4.BRIDGE_HOST,
                        "port": ui_port,
                        "body": job_request,
                    }
                ).encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO jobs (job_id, request_key, workflow_id, task_identity, "
                "request_digest, request_json, state, client_request_id, title_marker, "
                "agent_cwd, created_at, updated_at, thread_id, turn_id, remote_state, "
                "receipt_json, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    request_key,
                    workflow_id,
                    task_identity,
                    job_digest,
                    canonical(job_request),
                    "SUCCEEDED",
                    job_request["client_request_id"],
                    job_request["title"],
                    workspace,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    thread_id,
                    turn_id,
                    "completed",
                    canonical({"result": {"final_text": str(number)}}),
                    None,
                ),
            )
            for state in ("PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED"):
                connection.execute(
                    "INSERT INTO job_events (job_id, state, timestamp, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (job_id, state, "2026-01-01T00:00:00+00:00", "{}"),
                )
            job_rows.append(
                {
                    "job_id": job_id,
                    "task_identity": task_identity,
                    "request_key": request_key,
                }
            )
        adapter._validate_ledger_data(connection)

    dbos_db = run_dir / "dbos.sqlite"
    with sqlite3.connect(dbos_db) as connection:
        connection.execute(
            "CREATE TABLE workflow_status (workflow_uuid TEXT NOT NULL, status TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO workflow_status VALUES (?, ?)", (workflow_id, "SUCCESS"))
    result = {
        "workflow_id": workflow_id,
        "definition_sha256": definition_sha256,
        "input_sha256": input_sha256,
        "result": [
            {
                "number": 5,
                "status": "skipped",
                "job_id": None,
                "request_key": None,
                "final_text": None,
            },
            *(
                {
                    "number": number,
                    "status": "SUCCEEDED",
                    "job_id": job["job_id"],
                    "request_key": job["request_key"],
                    "final_text": str(number),
                }
                for number, job in zip((9, 17, 21), job_rows, strict=True)
            ),
        ],
    }
    (run_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return {
        "state_root": state_root,
        "client_request_id": client_request_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workspace_root": workspace_root,
        "ui_port": ui_port,
    }


@pytest.mark.parametrize("request_state", ("running", "succeeded"))
def test_saved_state_accepts_running_or_succeeded_request_row_only_with_durable_success(
    smoke: ModuleType, tmp_path: Path, request_state: str
) -> None:
    saved = smoke._read_three_job_saved_state(
        **_write_valid_saved_state(smoke, tmp_path, request_state=request_state)
    )

    assert [row["state"] for row in saved["jobs"]] == ["SUCCEEDED"] * 3
    assert [row["remote_state"] for row in saved["jobs"]] == ["completed"] * 3
    assert saved["dbos_status"] == "SUCCESS"
    assert saved["worker_status"] == {"state": "completed", "exit_code": 0}
    assert saved["final_texts"] == ["9", "17", "21"]
    assert len(saved["events"]) == 15


@pytest.mark.parametrize("request_state", ("queued", "starting", "failed", "needs_reconciliation"))
def test_saved_state_rejects_non_running_and_non_succeeded_request_rows(
    smoke: ModuleType, tmp_path: Path, request_state: str
) -> None:
    with pytest.raises(smoke.SmokeAbort) as error:
        smoke._read_three_job_saved_state(
            **_write_valid_saved_state(smoke, tmp_path, request_state=request_state)
        )

    assert error.value.code == "three_job_request_ledger_identity_or_state_mismatch"


@pytest.mark.parametrize("identity", ("client_request_id", "workflow_id", "run_id"))
def test_saved_state_rejects_each_request_ledger_identity_mismatch(
    smoke: ModuleType, tmp_path: Path, identity: str
) -> None:
    with pytest.raises(smoke.SmokeAbort) as error:
        smoke._read_three_job_saved_state(
            **_write_valid_saved_state(smoke, tmp_path, identity_mismatch=identity)
        )

    assert error.value.code == "three_job_request_ledger_identity_or_state_mismatch"
