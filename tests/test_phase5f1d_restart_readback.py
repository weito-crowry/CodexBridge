from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "phase5f1d_r1_restart_readback.py"


def load_restart_script() -> ModuleType | None:
    if not SCRIPT_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("phase5f1d_restart_readback", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_restart_eligibility_requires_confirmed_shutdown_and_unchanged_state() -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    eligible = restart_script.restart_eligible
    assert eligible(
        shutdown_status=202,
        parent_stopped=True,
        descendants_stopped=True,
        ports_released=True,
        state_unchanged=True,
    )

    for failed_check in (
        {"shutdown_status": None},
        {"shutdown_status": 500},
        {"parent_stopped": False},
        {"descendants_stopped": False},
        {"ports_released": False},
        {"state_unchanged": False},
    ):
        arguments = {
            "shutdown_status": 202,
            "parent_stopped": True,
            "descendants_stopped": True,
            "ports_released": True,
            "state_unchanged": True,
        }
        arguments.update(failed_check)
        assert not eligible(**arguments)


def test_shutdown_transport_unknown_is_attempted_once() -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    calls = 0

    def post_once() -> tuple[int, dict[str, str]]:
        nonlocal calls
        calls += 1
        raise TimeoutError("response unavailable")

    status, payload = restart_script.shutdown_once(post_once)

    assert calls == 1
    assert status is None
    assert payload is None


def test_evidence_creation_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError):
        restart_script.write_evidence_once(evidence_path, {"status": "PASS"})

    assert evidence_path.read_text(encoding="utf-8") == "preserve"


def test_evidence_writer_rejects_credential_or_prompt_fields(tmp_path: Path) -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    evidence_path = tmp_path / "evidence.json"

    with pytest.raises(ValueError, match="sensitive evidence field"):
        restart_script.write_evidence_once(
            evidence_path,
            {"status": "PASS", "control_token": "do-not-write"},
        )

    assert not evidence_path.exists()


def test_saved_state_requires_exact_request_job_and_result_projection() -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    valid_state = {
        "requests": [
            {
                "client_request_id": restart_script.CLIENT_REQUEST_ID,
                "workflow_id": restart_script.WORKFLOW_ID,
                "run_id": restart_script.RUN_ID,
                "request_digest": restart_script.REQUEST_DIGEST,
                "state": "running",
            }
        ],
        "jobs": [
            {
                "job_id": f"job-{index}",
                "workflow_id": restart_script.WORKFLOW_ID,
                "task_identity": f"do[{index}].calc",
                "request_digest": f"{index + 1:064x}",
                "client_request_id": f"ows4d-job{index}",
                "state": "SUCCEEDED",
                "thread_id": f"thread-{index}",
                "turn_id": f"turn-{index}",
                "remote_state": "completed",
            }
            for index in range(3)
        ],
        "events": [
            {"state": state, "count": 3}
            for state in ("ACCEPTED", "MAYBE_SENT", "PREPARED", "RUNNING", "SUCCEEDED")
        ],
        "event_count": 15,
        "reconciliation_count": 0,
        "dbos": [{"workflow_uuid": restart_script.WORKFLOW_ID, "status": "SUCCESS"}],
        "worker_status": {"state": "completed", "exit_code": 0},
        "result_rows": [
            {"status": "skipped", "number": 5, "final_text": None},
            {"status": "SUCCEEDED", "number": 9, "final_text": "9"},
            {"status": "SUCCEEDED", "number": 17, "final_text": "17"},
            {"status": "SUCCEEDED", "number": 21, "final_text": "21"},
        ],
        "result_final_texts": ["9", "17", "21"],
    }

    assert restart_script._expected_sqlite_state(valid_state)

    wrong_digest = {**valid_state, "jobs": [dict(job) for job in valid_state["jobs"]]}
    wrong_digest["jobs"][0]["request_digest"] = "different"
    assert not restart_script._expected_sqlite_state(wrong_digest)

    wrong_result = {**valid_state, "result_rows": [*valid_state["result_rows"][:-1]]}
    assert not restart_script._expected_sqlite_state(wrong_result)


def test_acl_scope_requires_all_expected_paths_without_broad_write_access() -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    names = (
        "r1_root",
        "workspace",
        "bridge_config",
        "codex_home",
        "codex_auth",
        "localappdata",
        "workflow_state_root",
        "run_root",
    )
    rows = [
        {
            "name": name,
            "owner_matches_current": True,
            "untrusted_writable_ace_count": 0,
            "access_rule_count": 3,
            "acl_sha256": "a" * 64,
        }
        for name in names
    ]

    assert restart_script._acl_scope_isolated(rows)

    rows[0] = {**rows[0], "untrusted_writable_ace_count": 1}
    assert not restart_script._acl_scope_isolated(rows)


def test_access_denied_process_check_is_unknown_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"

    class AccessDeniedProcess:
        def __init__(self, _pid: int) -> None:
            raise restart_script.psutil.AccessDenied()

    monkeypatch.setattr(restart_script.psutil, "Process", AccessDeniedProcess)

    assert restart_script._process_matches(14208, 1794500465.809399) is None


def test_r1c_replay_history_remains_unknown_and_unretried() -> None:
    restart_script = load_restart_script()
    assert restart_script is not None, "the Phase 5F-1D restart runner is not implemented"
    checks = {
        "replay": {
            "outcome": "UNKNOWN_NO_RETRY",
            "post_attempted": "UNKNOWN",
            "post_count": None,
            "possible_post_count_range": [0, 1],
            "accepted_http_status": None,
            "retry_count": 0,
            "new_workflow_post_count": 0,
            "new_workflow_count": 0,
            "new_codex_job_count": 0,
            "new_codex_thread_count": 0,
            "api_projection_identical_before_and_after": "UNKNOWN",
            "state_after_reconciliation_unchanged": True,
        }
    }

    assert restart_script.replay_history_is_unknown(checks)

    changed = {"replay": {**checks["replay"], "possible_post_count_range": [0, 0]}}
    assert not restart_script.replay_history_is_unknown(changed)
