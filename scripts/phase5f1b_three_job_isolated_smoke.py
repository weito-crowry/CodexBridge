from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import psutil  # type: ignore[import-untyped]
import yaml

from codex_bridge.codex_resolver import resolve_codex_executable
from codex_bridge.workflows.interpreter import (
    OwsError,
    validate_three_live_codex_job_input,
    validate_workflow,
)

r4: Any = import_module("scripts.phase5e2c2_isolated_smoke")

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "three_codex_job_loop_workflow.yaml"
EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f1b-three-job-e2e-evidence.json"
R1_EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f1b-three-job-e2e-r1-evidence.json"
FIXTURE_SHA256 = "a813cbdf5534039ead421c33571b44719e0576b2fe791b3fff8f5f5430a7bbb0"
R4_FIX_COMMIT = "1e8255f1086e7fb5111450f2c5454c8f01f5d008"
MAX_WORKFLOW_STATE_OBSERVATIONS = 32
MAX_OBSERVED_WORKFLOWS = 8
MAX_OBSERVED_JOBS = 8
FIXED_INPUT: dict[str, Any] = {
    "issues": [
        {"number": 17, "enabled": True},
        {"number": 5, "enabled": False},
        {"number": 9, "enabled": True},
        {"number": 21, "enabled": True},
    ]
}
EXPECTED_ISSUES = (
    {"number": 5, "enabled": False},
    {"number": 9, "enabled": True},
    {"number": 17, "enabled": True},
    {"number": 21, "enabled": True},
)
EXPECTED_TASK_IDENTITIES = (
    "do[0].processIssues.iteration[1].do[1].processIssue",
    "do[0].processIssues.iteration[2].do[1].processIssue",
    "do[0].processIssues.iteration[3].do[1].processIssue",
)
SmokeAbort = r4.SmokeAbort


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _select_evidence_path(*, retest_once: bool) -> Path:
    initial_exists = EVIDENCE_PATH.exists() or EVIDENCE_PATH.is_symlink()
    r1_exists = R1_EVIDENCE_PATH.exists() or R1_EVIDENCE_PATH.is_symlink()
    if retest_once:
        if EVIDENCE_PATH.is_symlink() or not EVIDENCE_PATH.is_file():
            raise SmokeAbort("BLOCKED", "initial_evidence_required_for_r1")
        if r1_exists:
            raise SmokeAbort("BLOCKED", "r1_evidence_path_already_exists")
        return R1_EVIDENCE_PATH
    if initial_exists:
        raise SmokeAbort("BLOCKED", "initial_evidence_path_already_exists")
    if r1_exists:
        raise SmokeAbort("BLOCKED", "r1_evidence_path_already_exists")
    return EVIDENCE_PATH


def load_fixed_workflow(
    path: Path = FIXTURE_PATH,
) -> tuple[str, dict[str, Any], tuple[dict[str, Any], ...]]:
    try:
        fixture_text = path.read_text(encoding="utf-8")
        definition = yaml.safe_load(fixture_text)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise SmokeAbort("BLOCKED", "fixed_three_job_fixture_unavailable") from None
    normalized = fixture_text.replace("\r\n", "\n").encode("utf-8")
    if _sha256(normalized) != FIXTURE_SHA256 or not isinstance(definition, dict):
        raise SmokeAbort("BLOCKED", "fixed_three_job_fixture_hash_or_shape_mismatch")
    try:
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)
        issues = validate_three_live_codex_job_input(FIXED_INPUT)
    except (OwsError, TypeError, ValueError, RecursionError):
        raise SmokeAbort("BLOCKED", "fixed_three_job_contract_is_invalid") from None
    if issues != EXPECTED_ISSUES:
        raise SmokeAbort("BLOCKED", "fixed_three_job_input_order_or_enablement_mismatch")
    return fixture_text, definition, issues


def build_three_job_request_body(
    *, workflow_yaml: str, client_request_id: str, workspace_root: str
) -> bytes:
    payload = {
        "client_request_id": client_request_id,
        "workflow_yaml": workflow_yaml,
        "input": FIXED_INPUT,
        "workspace_root": workspace_root,
        "max_live_codex_jobs": 3,
    }
    return _canonical_json(payload).encode("utf-8")


def validate_three_job_result(
    result_value: Any,
    *,
    workflow_id: str,
    jobs: Sequence[Mapping[str, Any]],
) -> list[str]:
    if (
        not isinstance(result_value, dict)
        or result_value.get("workflow_id") != workflow_id
        or not isinstance(result_value.get("result"), list)
        or len(result_value["result"]) != 4
        or len(jobs) != 3
    ):
        raise SmokeAbort("BLOCKED", "three_job_result_shape_or_identity_mismatch")
    expected: list[dict[str, Any]] = [
        {"number": 5, "status": "skipped", "job_id": None, "request_key": None, "final_text": None}
    ]
    final_texts: list[str] = []
    for number, task_identity, job in zip((9, 17, 21), EXPECTED_TASK_IDENTITIES, jobs, strict=True):
        job_id = job.get("job_id")
        request_key = job.get("request_key")
        final_text = str(number)
        if (
            not isinstance(job_id, str)
            or not job_id
            or job.get("task_identity") != task_identity
            or request_key != f"{workflow_id}::{task_identity}"
        ):
            raise SmokeAbort("BLOCKED", "three_job_result_job_identity_mismatch")
        expected.append(
            {
                "number": number,
                "status": "SUCCEEDED",
                "job_id": job_id,
                "request_key": request_key,
                "final_text": final_text,
            }
        )
        final_texts.append(final_text)
    if result_value.get("result") != expected:
        raise SmokeAbort("FAIL", "three_job_result_values_or_order_mismatch")
    return final_texts


def validate_job_event_order(
    events: Sequence[Mapping[str, Any]], jobs: Sequence[Mapping[str, Any]]
) -> tuple[int, dict[str, int]]:
    job_ids = [row.get("job_id") for row in jobs]
    if (
        len(job_ids) != 3
        or any(not isinstance(job_id, str) or not job_id for job_id in job_ids)
        or len(set(job_ids)) != 3
    ):
        raise SmokeAbort("BLOCKED", "three_job_event_job_ids_invalid")
    expected_ids = [str(job_id) for job_id in job_ids]
    dispatch_counts = {job_id: 0 for job_id in expected_ids}
    stages = {job_id: 0 for job_id in expected_ids}
    active: set[str] = set()
    maximum_active = 0
    dispatch_order: list[str] = []
    success_order: list[str] = []
    previous_event_id = 0
    for event in events:
        event_id = event.get("event_id")
        job_id = event.get("job_id")
        state = event.get("state")
        if (
            type(event_id) is not int
            or event_id <= previous_event_id
            or not isinstance(job_id, str)
            or job_id not in dispatch_counts
            or not isinstance(state, str)
        ):
            raise SmokeAbort("BLOCKED", "three_job_event_identity_or_order_invalid")
        previous_event_id = event_id
        if state == "PREPARED":
            if stages[job_id] != 0:
                raise SmokeAbort("BLOCKED", "three_job_event_transition_invalid")
            stages[job_id] = 1
        elif state == "MAYBE_SENT":
            if stages[job_id] != 1 or job_id in active or dispatch_counts[job_id] != 0:
                raise SmokeAbort("BLOCKED", "three_job_dispatch_was_not_exactly_once")
            dispatch_counts[job_id] += 1
            stages[job_id] = 2
            dispatch_order.append(job_id)
            active.add(job_id)
            maximum_active = max(maximum_active, len(active))
        elif state == "ACCEPTED":
            if stages[job_id] != 2 or job_id not in active:
                raise SmokeAbort("BLOCKED", "three_job_event_transition_invalid")
            stages[job_id] = 3
        elif state == "RUNNING":
            if stages[job_id] != 3 or job_id not in active:
                raise SmokeAbort("BLOCKED", "three_job_event_transition_invalid")
        elif state == "SUCCEEDED":
            if stages[job_id] != 3 or job_id not in active:
                raise SmokeAbort("BLOCKED", "three_job_event_did_not_finish_successfully")
            stages[job_id] = 4
            active.remove(job_id)
            success_order.append(job_id)
        else:
            raise SmokeAbort("BLOCKED", "three_job_event_state_unsupported")
    if (
        active
        or dispatch_counts != {job_id: 1 for job_id in expected_ids}
        or stages != {job_id: 4 for job_id in expected_ids}
        or dispatch_order != expected_ids
        or success_order != expected_ids
        or maximum_active != 1
    ):
        raise SmokeAbort("BLOCKED", "three_job_dispatch_sequence_or_concurrency_mismatch")
    return maximum_active, dispatch_counts


def _preflight_negative_cases(definition: Mapping[str, Any]) -> tuple[str, ...]:
    cases: tuple[tuple[str, Any], ...] = (
        (
            "five_issues",
            {"issues": [{"number": number, "enabled": False} for number in range(1, 6)]},
        ),
        (
            "four_enabled",
            {"issues": [{"number": number, "enabled": True} for number in range(1, 5)]},
        ),
        (
            "duplicate_number",
            {"issues": [{"number": 1, "enabled": True}, {"number": 1, "enabled": False}]},
        ),
        ("wrong_type", {"issues": [{"number": True, "enabled": True}]}),
    )
    rejected: list[str] = []
    for name, input_data in cases:
        try:
            validate_three_live_codex_job_input(input_data)
        except OwsError:
            rejected.append(name)
        else:
            raise SmokeAbort("FAIL", "three_job_negative_input_was_accepted")
    extra_task = copy.deepcopy(dict(definition))
    tasks = extra_task.get("do")
    if isinstance(tasks, list):
        tasks.append({"unexpected": {"set": {"status": "unsafe"}}})
    try:
        validate_workflow(extra_task, codex_job_live=True, max_live_codex_jobs=3)
    except OwsError:
        rejected.append("extra_task")
    else:
        raise SmokeAbort("FAIL", "three_job_extra_task_was_accepted")
    return tuple(rejected)


def _existing_evidence_hashes(*, require_phase5f1b_evidence: bool = False) -> dict[str, str]:
    required_names = (
        "phase5e-0-evidence.json",
        "phase5e-2c2-evidence.json",
        "phase5e-2c2-retest-evidence.json",
        "phase5e-2c2-diagnostic-retest-evidence.json",
        "phase5e-2c2-r4-evidence.json",
    )
    phase5f1b_names = (
        "phase5f1b-three-job-e2e-evidence.json",
        "phase5f1b-postmortem-addendum.json",
    )
    hashes: dict[str, str] = {}
    try:
        for name in required_names:
            path = REPOSITORY_ROOT / "docs" / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                raise OSError
            hashes[f"docs/{name}"] = _sha256(path.read_bytes())
        for name in phase5f1b_names:
            path = REPOSITORY_ROOT / "docs" / name
            if not path.exists() and not path.is_symlink():
                if require_phase5f1b_evidence:
                    raise OSError
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
                raise OSError
            hashes[f"docs/{name}"] = _sha256(path.read_bytes())
    except OSError:
        raise SmokeAbort("BLOCKED", "prior_evidence_unavailable_or_changed") from None
    return hashes


def _assert_existing_evidence_unchanged(expected: Mapping[str, str]) -> None:
    for relative, expected_hash in expected.items():
        path = REPOSITORY_ROOT / relative
        try:
            actual = _sha256(path.read_bytes())
        except OSError:
            raise SmokeAbort("FAIL", "prior_evidence_changed_during_run") from None
        if actual != expected_hash:
            raise SmokeAbort("FAIL", "prior_evidence_changed_during_run")


def _current_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise SmokeAbort("BLOCKED", "workflow_checkout_commit_unavailable") from None
    commit = result.stdout.strip().lower()
    if (
        result.returncode != 0
        or len(commit) != 40
        or any(ch not in "0123456789abcdef" for ch in commit)
    ):
        raise SmokeAbort("BLOCKED", "workflow_checkout_commit_unavailable")
    return commit


def _require_r4_fix() -> None:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", R4_FIX_COMMIT, "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise SmokeAbort("BLOCKED", "r4_transient_history_recheck_fix_unverified") from None
    if result.returncode != 0:
        raise SmokeAbort("BLOCKED", "r4_transient_history_recheck_fix_missing")


def _record_bridge_owner(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    try:
        current_user = psutil.Process().username()
        bridge = psutil.Process(process.pid)
        bridge_user = bridge.username()
        created_at = bridge.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "isolated_bridge_owner_or_creation_time_unavailable") from None
    if not current_user or bridge_user != current_user:
        raise SmokeAbort("BLOCKED", "isolated_bridge_owner_does_not_match_current_user")
    return {
        "pid": process.pid,
        "created_at_epoch": created_at,
        "created_at_utc": datetime.fromtimestamp(created_at, tz=UTC).isoformat(timespec="seconds"),
        "owner_matches_current_user": True,
        "identity_verified": True,
    }


def _bounded_observation_state(
    value: Any, allowed: set[str], *, uppercase: bool = False
) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.upper() if uppercase else value.lower()
    if candidate in allowed:
        return candidate
    return "OTHER" if uppercase else "other"


def _record_workflow_get_observation(
    observations: dict[str, Any],
    *,
    route: str,
    http_status: Any,
    payload: Any,
) -> None:
    items = observations.get("items")
    if not isinstance(items, list):
        return
    if len(items) >= MAX_WORKFLOW_STATE_OBSERVATIONS:
        observations["dropped"] = min(
            MAX_WORKFLOW_STATE_OBSERVATIONS,
            int(observations.get("dropped", 0)) + 1,
        )
        return

    base_route = "/ui-api/control/workflows"
    route_path = route.split("?", 1)[0]
    if route_path == base_route:
        endpoint = "list"
    elif route_path.endswith("/result"):
        endpoint = "result"
    elif route_path.startswith(f"{base_route}/"):
        endpoint = "detail"
    else:
        endpoint = "other"
    status_code = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
    observation: dict[str, Any] = {
        "observed_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "endpoint": endpoint,
        "http_status": status_code,
    }
    workflow_states = {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
        "unknown",
    }
    worker_states = {"running", "completed", "failed", "not_started", "unknown"}
    job_states = {"PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"}
    if endpoint == "list":
        workflows = payload.get("workflows") if isinstance(payload, dict) else None
        if isinstance(workflows, list):
            observation["workflow_count"] = len(workflows)
            observation["workflow_statuses"] = [
                _bounded_observation_state(
                    item.get("status") if isinstance(item, dict) else None,
                    workflow_states,
                )
                for item in workflows[:MAX_OBSERVED_WORKFLOWS]
            ]
            observation["truncated_workflow_count"] = max(
                0, len(workflows) - MAX_OBSERVED_WORKFLOWS
            )
    elif endpoint == "detail" and isinstance(payload, dict):
        observation["workflow_status"] = _bounded_observation_state(
            payload.get("status"), workflow_states
        )
        observation["worker_state"] = _bounded_observation_state(
            payload.get("worker_state"), worker_states
        )
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            observation["job_count"] = len(jobs)
            observation["job_statuses"] = [
                _bounded_observation_state(
                    item.get("state") if isinstance(item, dict) else None,
                    job_states,
                    uppercase=True,
                )
                for item in jobs[:MAX_OBSERVED_JOBS]
            ]
            observation["truncated_job_count"] = max(0, len(jobs) - MAX_OBSERVED_JOBS)
    items.append(observation)


def _counted_http_json(
    original: Any,
    counts: dict[str, int],
    observations: dict[str, Any] | None = None,
) -> Any:
    def counted(
        ui_port: int,
        method: str,
        route: str,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[int, Any]:
        normalized_method = method.upper()
        if normalized_method == "GET":
            counts["http_get"] += 1
            if route.startswith("/ui-api/control/workflows"):
                counts["workflow_http_get"] += 1
            else:
                counts["other_http_get"] += 1
        elif normalized_method == "POST":
            counts["http_post"] += 1
            if route == "/ui-api/control/workflows":
                counts["workflow_post"] += 1
            elif route == "/ui-api/control/shutdown":
                counts["shutdown_post"] += 1
            else:
                counts["other_http_post"] += 1
        try:
            response = cast(tuple[int, Any], original(ui_port, method, route, *args, **kwargs))
        except SmokeAbort:
            if (
                observations is not None
                and normalized_method == "GET"
                and route.startswith("/ui-api/control/workflows")
            ):
                _record_workflow_get_observation(
                    observations, route=route, http_status=None, payload=None
                )
            raise
        if (
            observations is not None
            and normalized_method == "GET"
            and route.startswith("/ui-api/control/workflows")
        ):
            _record_workflow_get_observation(
                observations,
                route=route,
                http_status=response[0],
                payload=response[1],
            )
        return response

    return counted


def _verify_v2_job_ledger(database: Path) -> None:
    connection = r4._read_only_connection(database)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"jobs", "job_events", "job_reconciliation"} <= tables:
            raise SmokeAbort("BLOCKED", "three_job_v2_ledger_schema_incomplete")
        unique_indexes: set[tuple[str, ...]] = set()
        for index in connection.execute("PRAGMA index_list(jobs)"):
            if not bool(index[2]):
                continue
            name = str(index[1]).replace('"', '""')
            columns = tuple(
                str(column[2])
                for column in connection.execute(f'PRAGMA index_info("{name}")')
                if column[2] is not None
            )
            unique_indexes.add(columns)
        if ("workflow_id", "task_identity") not in unique_indexes:
            raise SmokeAbort("BLOCKED", "three_job_v2_workflow_task_uniqueness_missing")
    except sqlite3.Error:
        raise SmokeAbort("BLOCKED", "three_job_v2_ledger_schema_unreadable") from None
    finally:
        connection.close()


def _read_three_job_saved_state(
    *,
    state_root: Path,
    client_request_id: str,
    workflow_id: str,
    run_id: str,
    workspace_root: Path,
    ui_port: int,
) -> dict[str, Any]:
    request_rows = r4._read_request_ledger(state_root)
    if len(request_rows) != 1:
        raise SmokeAbort("BLOCKED", "three_job_request_ledger_row_count_mismatch")
    request = request_rows[0]
    if (
        request.get("client_request_id") != client_request_id
        or request.get("workflow_id") != workflow_id
        or request.get("run_id") != run_id
        or request.get("state") != "succeeded"
    ):
        raise SmokeAbort("BLOCKED", "three_job_request_ledger_identity_or_state_mismatch")

    run_dir = r4._run_directory(state_root, run_id)
    manifest = r4._bounded_json_file(run_dir / "manifest.json", maximum=32 * 1024)
    definition_text = (run_dir / "workflow.yaml").read_text(encoding="utf-8")
    try:
        definition = json.loads(definition_text)
    except (UnicodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "three_job_immutable_definition_is_invalid") from None
    input_data = r4._bounded_json_file(run_dir / "input.json", maximum=r4.MAX_LOCAL_JSON_BYTES)
    _, expected_definition, _ = load_fixed_workflow()
    expected_stored_definition_text = f"{r4._canonical_json(expected_definition)}\n"
    if (
        not isinstance(manifest, dict)
        or not isinstance(definition, dict)
        or not isinstance(input_data, dict)
        or definition != expected_definition
        or definition_text != expected_stored_definition_text
    ):
        raise SmokeAbort("BLOCKED", "three_job_immutable_snapshot_shape_invalid")

    expected_workspace = str(workspace_root.resolve(strict=True))
    if (
        manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("request_digest") != request.get("request_digest")
        or manifest.get("workspace_root") != expected_workspace
        or manifest.get("workflow_source") != "ui-api"
        or manifest.get("mode") != "codex-job-live"
        or manifest.get("definition_sha256") != r4.definition_sha256(definition_text)
        or manifest.get("input_sha256") != _sha256(r4._canonical_json(input_data).encode("utf-8"))
        or input_data != FIXED_INPUT
    ):
        raise SmokeAbort("BLOCKED", "three_job_manifest_hash_or_root_mismatch")
    control = manifest.get("control")
    if (
        not isinstance(control, dict)
        or control.get("codex_job_live") is not True
        or control.get("max_live_codex_jobs") != 3
        or control.get("codex_job_max_polls") != 600
        or control.get("codex_job_poll_interval_seconds") != 0.2
    ):
        raise SmokeAbort("BLOCKED", "three_job_manifest_limit_mismatch")
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": expected_workspace,
        "max_live_codex_jobs": 3,
    }
    request_digest = _sha256(r4._canonical_json(digest_payload).encode("utf-8"))
    if request_digest != request.get("request_digest"):
        raise SmokeAbort("BLOCKED", "three_job_request_digest_mismatch")

    database = run_dir / "codex_jobs.sqlite"
    _verify_v2_job_ledger(database)
    jobs = r4._read_sqlite_rows(
        database,
        "SELECT job_id, request_key, workflow_id, task_identity, request_digest, request_json, "
        "state, client_request_id, title_marker, agent_cwd, thread_id, turn_id, remote_state, "
        "receipt_json FROM jobs ORDER BY rowid LIMIT 4",
    )
    events = r4._read_sqlite_rows(
        database,
        "SELECT event_id, job_id, state FROM job_events ORDER BY event_id LIMIT 256",
    )
    if (
        len(jobs) != 3
        or tuple(row.get("task_identity") for row in jobs) != EXPECTED_TASK_IDENTITIES
    ):
        raise SmokeAbort("BLOCKED", "three_job_ledger_count_or_order_mismatch")
    job_ids = [row.get("job_id") for row in jobs]
    thread_ids = [row.get("thread_id") for row in jobs]
    turn_ids = [row.get("turn_id") for row in jobs]
    request_keys = [row.get("request_key") for row in jobs]
    all_ids = job_ids + thread_ids + turn_ids + request_keys
    if (
        any(not isinstance(value, str) or not value for value in all_ids)
        or len(set(job_ids)) != 3
        or len(set(thread_ids)) != 3
        or len(set(turn_ids)) != 3
        or len(set(request_keys)) != 3
        or any(row.get("workflow_id") != workflow_id for row in jobs)
        or any(row.get("state") != "SUCCEEDED" for row in jobs)
        or any(row.get("remote_state") != "completed" for row in jobs)
    ):
        raise SmokeAbort("BLOCKED", "three_job_terminal_identity_mismatch")

    final_texts: list[str] = []
    for number, row in zip((9, 17, 21), jobs, strict=True):
        try:
            request_body = json.loads(str(row["request_json"]))
            receipt = json.loads(str(row["receipt_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            raise SmokeAbort("BLOCKED", "three_job_request_or_receipt_invalid") from None
        if not isinstance(request_body, dict) or not isinstance(receipt, dict):
            raise SmokeAbort("BLOCKED", "three_job_request_or_receipt_shape_invalid")
        expected_job_digest = _sha256(
            r4._canonical_json(
                {"host": r4.BRIDGE_HOST, "port": ui_port, "body": request_body}
            ).encode("utf-8")
        )
        if (
            row.get("request_digest") != expected_job_digest
            or row.get("request_key") != f"{workflow_id}::{row.get('task_identity')}"
            or row.get("agent_cwd") != expected_workspace
            or request_body.get("cwd") != expected_workspace
            or request_body.get("prompt") != f"Reply with only the synthetic issue number: {number}"
            or request_body.get("sandbox_mode") != "inherit"
            or request_body.get("confirm_full_access") is not False
        ):
            raise SmokeAbort("BLOCKED", "three_job_codex_request_hash_or_scope_mismatch")
        job_result = receipt.get("result")
        final_text = job_result.get("final_text") if isinstance(job_result, dict) else None
        if final_text != str(number) or row.get("remote_state") != "completed":
            raise SmokeAbort("FAIL", "three_job_codex_final_text_mismatch")
        final_texts.append(final_text)

    maximum_active, dispatch_counts = validate_job_event_order(events, jobs)
    if len({event.get("job_id") for event in events}) != 3:
        raise SmokeAbort("BLOCKED", "three_job_ledger_contains_additional_events")
    dbos_status = r4._read_dbos_status(run_dir, workflow_id)
    if dbos_status != "SUCCESS":
        raise SmokeAbort("FAIL", "dbos_workflow_did_not_succeed")
    worker = r4._bounded_json_file(run_dir / "worker-entry-status.json", maximum=4096)
    if (
        not isinstance(worker, dict)
        or worker.get("state") != "completed"
        or worker.get("exit_code") != 0
    ):
        raise SmokeAbort("FAIL", "three_job_workflow_worker_did_not_exit_successfully")
    stored_result = r4._bounded_json_file(run_dir / "result.json", maximum=r4.MAX_LOCAL_JSON_BYTES)
    if (
        not isinstance(stored_result, dict)
        or stored_result.get("workflow_id") != workflow_id
        or stored_result.get("definition_sha256") != manifest.get("definition_sha256")
        or stored_result.get("input_sha256") != manifest.get("input_sha256")
        or validate_three_job_result(stored_result, workflow_id=workflow_id, jobs=jobs)
        != final_texts
    ):
        raise SmokeAbort("BLOCKED", "three_job_saved_result_hash_or_projection_mismatch")
    return {
        "run_dir": run_dir,
        "manifest": manifest,
        "jobs": jobs,
        "events": events,
        "dbos_status": dbos_status,
        "worker_status": worker,
        "maximum_active": maximum_active,
        "dispatch_counts": dispatch_counts,
        "final_texts": final_texts,
        "stored_result": stored_result,
    }


def _validate_api_success(
    *,
    workflow_id: str,
    run_id: str,
    listing: Mapping[str, Any],
    detail: Mapping[str, Any],
    result: Mapping[str, Any],
    saved: Mapping[str, Any],
) -> dict[str, Any]:
    jobs = detail.get("jobs")
    saved_jobs = saved.get("jobs")
    if (
        listing.get("workflow_id") != workflow_id
        or listing.get("status") != "succeeded"
        or detail.get("workflow_id") != workflow_id
        or detail.get("run_id") != run_id
        or detail.get("status") != "succeeded"
        or detail.get("max_live_codex_jobs") != 3
        or detail.get("worker_state") != "completed"
        or detail.get("terminal") is not True
        or not isinstance(jobs, list)
        or len(jobs) != 3
        or not isinstance(saved_jobs, list)
        or jobs != [r4._job_summary(row) for row in saved_jobs]
        or dict(result) != saved.get("stored_result")
    ):
        raise SmokeAbort("BLOCKED", "three_job_api_terminal_snapshot_mismatch")
    final_texts = validate_three_job_result(result, workflow_id=workflow_id, jobs=saved_jobs)
    if final_texts != ["9", "17", "21"]:
        raise SmokeAbort("FAIL", "three_job_api_final_texts_mismatch")
    return {"jobs": jobs, "final_texts": final_texts}


def _verify_console_qt_get(
    *,
    ui_port: int,
    token: str,
    workflow_id: str,
    expected_detail: Mapping[str, Any],
    expected_result: Mapping[str, Any],
    evidence: dict[str, Any],
    label: str,
) -> None:
    application = r4.QApplication.instance() or r4.QApplication([])
    client = r4.ApiClient(f"http://{r4.BRIDGE_HOST}:{ui_port}")
    dialog = r4.WorkflowManagementDialog()
    detail_path = f"/ui-api/control/workflows/{workflow_id}"
    result_path = f"/ui-api/control/workflows/{workflow_id}/result"
    try:
        detail_status, detail = r4._qt_get(client, detail_path, token, f"phase5f1b:{label}:detail")
        result_status, result = r4._qt_get(client, result_path, token, f"phase5f1b:{label}:result")
        evidence["http_counts"]["qt_get"] += 2
        observations = evidence.get("workflow_state_observations")
        if isinstance(observations, dict):
            _record_workflow_get_observation(
                observations,
                route=detail_path,
                http_status=detail_status,
                payload=detail,
            )
            _record_workflow_get_observation(
                observations,
                route=result_path,
                http_status=result_status,
                payload=result,
            )
        jobs_in_detail = detail.get("jobs") if isinstance(detail, dict) else None
        if (
            detail_status != 200
            or result_status != 200
            or detail != expected_detail
            or result != expected_result
            or not isinstance(jobs_in_detail, list)
            or len(jobs_in_detail) != 3
        ):
            raise SmokeAbort("BLOCKED", "console_qt_three_job_get_payload_mismatch")
        dialog.set_detail(detail)
        dialog.set_result(result)
        try:
            rendered = json.loads(dialog.result_json.toPlainText())
        except json.JSONDecodeError:
            raise SmokeAbort("BLOCKED", "console_qt_three_job_result_render_invalid") from None
        if (
            rendered != expected_result
            or workflow_id not in dialog.detail_labels["workflow_id"].text()
            or token in dialog.result_json.toPlainText()
        ):
            raise SmokeAbort("BLOCKED", "console_qt_three_job_result_render_mismatch")
    finally:
        dialog.close()
        client.deleteLater()
        application.processEvents()


def _remove_owned_temp_root(temp_root: Path) -> bool:
    try:
        temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
        resolved = temp_root.resolve(strict=True)
        if (
            resolved != temp_root
            or temp_root.parent != temporary_parent
            or not temp_root.name.startswith("codexbridge-phase5f1b-")
            or temp_root.is_symlink()
        ):
            return False
        shutil.rmtree(temp_root)
        return not temp_root.exists()
    except OSError:
        return False


def _owned_children_reaped(
    isolated_processes: Sequence[Mapping[str, Any]],
    stop_records: Sequence[Mapping[str, Any]],
) -> bool:
    process_states: dict[tuple[int, float], bool] = {}
    for process in isolated_processes:
        pid = process.get("pid")
        created_at = process.get("created_at_epoch")
        if type(pid) is int and isinstance(created_at, (int, float)):
            process_states[(pid, float(created_at))] = False
    for record in stop_records:
        pid = record.get("pid")
        created_at = record.get("created_at_epoch")
        identity = (
            (pid, float(created_at))
            if type(pid) is int and isinstance(created_at, (int, float))
            else None
        )
        if identity in process_states and record.get("reaped") is True:
            process_states[identity] = True
    return bool(process_states) and all(process_states.values())


def _new_evidence(commit_sha: str | None) -> dict[str, Any]:
    checks = (
        "native_windows",
        "fixture_and_negative_preflight",
        "r4_transient_history_recheck",
        "isolated_environment_and_roots",
        "dedicated_loopback_ports",
        "bridge_owner_readiness_and_authentication",
        "single_live_workflow_post",
        "workflow_api_identity_and_jobs",
        "immutable_hashes_and_v2_ledger",
        "expected_result_and_final_texts",
        "sequential_dispatch_and_concurrency",
        "same_id_replay",
        "console_qt_get_readback",
        "bridge_restart_get_readback",
        "existing_evidence_unchanged",
        "production_side_effects",
    )
    return {
        "phase": "5F-1B",
        "commit_sha": commit_sha,
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
        "status": "RUNNING",
        "reason": None,
        "checks": {name: {"status": "NOT_RUN"} for name in checks},
        "fixture_sha256": FIXTURE_SHA256,
        "fixture_path": "poc/ows/fixtures/three_codex_job_loop_workflow.yaml",
        "input_sha256": None,
        "workflow": {},
        "jobs": [],
        "ports": {},
        "isolated_processes": [],
        "http_counts": {
            "http_get": 0,
            "workflow_http_get": 0,
            "other_http_get": 0,
            "qt_get": 0,
            "http_post": 0,
            "workflow_post": 0,
            "shutdown_post": 0,
            "other_http_post": 0,
        },
        "live_workflow_post_attempted": False,
        "live_workflow_post_count": 0,
        "same_id_replay_post_attempted": False,
        "same_id_replay_post_count": 0,
        "new_codex_job_count": 0,
        "new_codex_thread_count": 0,
        "maximum_active_codex_jobs": None,
        "production_side_effect_count": 0,
        "production_side_effect_basis": "dedicated_loopback_config_and_isolated_state",
        "previous_evidence_sha256": {},
        "workflow_state_observations": {"items": [], "dropped": 0},
        "cleanup": {
            "status": "NOT_RUN",
            "forced_termination": False,
            "owned_children_reaped": False,
            "isolated_temp_state_removed": False,
        },
    }


def _run_e2e(evidence: dict[str, Any], *, evidence_path: Path) -> int:
    base_environ = dict(os.environ)
    temp_root: Path | None = None
    workspace_root: Path | None = None
    state_root: Path | None = None
    process: subprocess.Popen[bytes] | None = None
    stderr_relay: Any | None = None
    stderr_relays: list[Any] = []
    process_records: list[dict[str, Any]] = []
    ports: tuple[int, int] | None = None
    reservations: list[socket.socket] = []
    token = secrets.token_urlsafe(32)
    client_request_id = f"phase5f1b-{uuid.uuid4().hex}"
    workflow_id: str | None = None
    run_id: str | None = None
    workflow_yaml = ""
    prompt_material: tuple[str, ...] = ()
    safe_to_stop = True
    final_success = False
    cleanup_reaped = False
    original_http_json = r4._http_json
    counters = evidence["http_counts"]
    r4._http_json = _counted_http_json(
        original_http_json,
        counters,
        evidence["workflow_state_observations"],
    )
    try:
        if os.name != "nt" or not sys.platform.startswith("win"):
            raise SmokeAbort("BLOCKED", "windows_native_execution_required")
        if evidence.get("commit_sha") is None:
            raise SmokeAbort("BLOCKED", "workflow_checkout_commit_unavailable")
        if evidence_path.exists() or evidence_path.is_symlink():
            raise SmokeAbort("BLOCKED", "phase5f1b_evidence_path_already_exists")
        old_evidence_hashes = _existing_evidence_hashes(
            require_phase5f1b_evidence=evidence_path == R1_EVIDENCE_PATH
        )
        evidence["previous_evidence_sha256"] = old_evidence_hashes
        fixture_bytes = FIXTURE_PATH.read_bytes()
        evidence["fixture_raw_sha256"] = _sha256(fixture_bytes)
        workflow_yaml, definition, _issues = load_fixed_workflow()
        prompt_material = tuple(
            f"Reply with only the synthetic issue number: {number}" for number in (9, 17, 21)
        )
        rejected = _preflight_negative_cases(definition)
        if len(rejected) != 5:
            raise SmokeAbort("BLOCKED", "three_job_negative_preflight_cases_incomplete")
        _require_r4_fix()
        evidence["checks"]["fixture_and_negative_preflight"] = {
            "status": "PASS",
            "negative_cases_rejected": list(rejected),
            "enabled_jobs": 3,
        }
        evidence["checks"]["r4_transient_history_recheck"] = {
            "status": "PASS",
            "commit": R4_FIX_COMMIT,
        }
        evidence["checks"]["native_windows"] = {"status": "PASS", "platform": "windows_native"}

        resolution = resolve_codex_executable(config_executable=None, environ=base_environ)
        codex_executable = Path(resolution.path).resolve(strict=True)
        if not codex_executable.is_file() or codex_executable.suffix.casefold() != ".exe":
            raise SmokeAbort("BLOCKED", "resolved_codex_is_not_a_windows_executable")
        source_home = r4._source_codex_home(base_environ)
        auth_file = source_home / "auth.json"
        if (
            auth_file.is_symlink()
            or not auth_file.is_file()
            or auth_file.stat().st_size > r4.MAX_AUTH_BYTES
        ):
            raise SmokeAbort("BLOCKED", "codex_authentication_file_unavailable_or_oversized")

        temp_root = Path(tempfile.mkdtemp(prefix="codexbridge-phase5f1b-")).resolve(strict=True)
        workspace_root = temp_root / "workspace"
        workspace_root.mkdir()
        for directory in ("localappdata", "appdata", "tmp"):
            (temp_root / directory).mkdir()
        isolated_home = temp_root / "codex_home"
        r4._copy_authentication(source_home, isolated_home)
        config_path = temp_root / "bridge.toml"
        if config_path.exists() or config_path.is_symlink():
            raise SmokeAbort("BLOCKED", "isolated_bridge_config_path_already_exists")
        ports, reservations = r4._reserve_loopback_ports()
        mcp_port, ui_port = ports
        r4._write_bridge_config(
            config_path,
            mcp_port=mcp_port,
            ui_port=ui_port,
            workspace_root=workspace_root,
            codex_executable=codex_executable,
        )
        child_environment = r4.build_phase5e2c2_environment(
            base_environ,
            temp_root=temp_root,
            config_path=config_path,
            control_token=token,
            python_path=REPOSITORY_ROOT / "src",
            path_entries=r4._isolated_path_entries(base_environ, codex_executable),
        )
        state_root = Path(child_environment["LOCALAPPDATA"]) / "CodexBridge" / "workflows"
        r4._preflight_state_is_separate(state_root, workspace_root)
        r4._ensure_empty_isolated_state(state_root)
        if (
            not r4.roots_are_separate(Path("D:/CodexBridge"), temp_root)
            or not r4.roots_are_separate(REPOSITORY_ROOT, temp_root)
            or not r4.roots_are_separate(state_root, workspace_root)
            or workspace_root.is_symlink()
            or not workspace_root.is_dir()
        ):
            raise SmokeAbort("BLOCKED", "isolated_workspace_or_state_root_preflight_failed")
        r4._validate_isolated_config(
            child_environment,
            workspace_root=workspace_root,
            mcp_port=mcp_port,
            ui_port=ui_port,
            codex_executable=codex_executable,
            codex_home=isolated_home,
        )
        evidence["ports"] = {"mcp": mcp_port, "ui": ui_port, "loopback_only": True}
        evidence["checks"]["isolated_environment_and_roots"] = {
            "status": "PASS",
            "allowlist_is_workspace_only": True,
            "github_mcp_disabled": True,
            "state_and_workspace_separate": True,
            "workspace_new_and_empty": True,
            "auth_copied_to_isolated_home": True,
        }
        evidence["checks"]["dedicated_loopback_ports"] = {
            "status": "PASS",
            "production_ports_avoided": True,
            "loopback_only": True,
        }
        evidence["checks"]["production_side_effects"] = {
            "status": "PASS",
            "count": 0,
            "basis": "dedicated_loopback_config_and_isolated_state",
        }
        for reservation in reservations:
            reservation.close()
        reservations.clear()
        r4._assert_ports_free(ports)

        safe_to_stop = False
        process, stderr_relay = r4._start_bridge(child_environment)
        stderr_relays.append(stderr_relay)
        bridge_owner = _record_bridge_owner(process)
        bridge_owner["role"] = "initial"
        evidence["isolated_processes"].append(bridge_owner)
        safe_to_stop = True
        r4._wait_for_bridge(process, ui_port)
        preflight = r4._preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        if r4._worker_process_count(process.pid) != 0:
            raise SmokeAbort("BLOCKED", "isolated_bridge_started_worker_before_post")
        empty_list = r4._get_json(ui_port, "/ui-api/control/workflows?limit=50", token)
        if not isinstance(empty_list, dict) or empty_list.get("workflows") != []:
            raise SmokeAbort("BLOCKED", "isolated_workflow_list_not_empty_before_post")
        if r4._read_request_ledger(state_root):
            raise SmokeAbort("BLOCKED", "isolated_request_ledger_not_empty_before_post")
        evidence["checks"]["bridge_owner_readiness_and_authentication"] = {
            "status": "PASS",
            "owner_matches_current_user": bridge_owner["owner_matches_current_user"],
            "process_identity_matches": preflight["process_identity_matches"],
            "ports_owned_by_bridge": preflight["ports_owned_by_bridge"],
            "appserver_ready": True,
            "capabilities_authenticated": preflight["capabilities_authenticated"],
            "missing_token_rejected": preflight["missing_token_rejected"],
            "incorrect_token_rejected": preflight["incorrect_token_rejected"],
        }
        evidence["workflow"]["workspace_preflight"] = {
            "allowed_root_isolated_workspace": True,
            "workspace_exists": True,
        }
        _assert_existing_evidence_unchanged(old_evidence_hashes)

        workspace = str(workspace_root.resolve(strict=True))
        body = build_three_job_request_body(
            workflow_yaml=workflow_yaml,
            client_request_id=client_request_id,
            workspace_root=workspace,
        )
        evidence["input_sha256"] = _sha256(r4._canonical_json(FIXED_INPUT).encode("utf-8"))
        evidence["workflow"].update(
            {
                "client_request_id": client_request_id,
                "max_live_codex_jobs": 3,
                "workflow_yaml_sha256": FIXTURE_SHA256,
                "request_body_sha256": _sha256(body),
            }
        )
        evidence["live_workflow_post_attempted"] = True
        evidence["live_workflow_post_count"] = 1
        evidence["new_codex_job_count"] = None
        evidence["new_codex_thread_count"] = None
        safe_to_stop = False
        try:
            post_status, post_response = r4._http_json(
                ui_port,
                "POST",
                "/ui-api/control/workflows",
                token=token,
                body=body,
                timeout=25.0,
            )
        except SmokeAbort:
            evidence["workflow"]["initial_post_outcome"] = "unknown"
            evidence["workflow"]["read_only_reconciliation"] = r4._read_reconciliation_state(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=client_request_id,
            )
            raise SmokeAbort("BLOCKED", "initial_workflow_post_outcome_unknown_no_retry") from None
        if post_status != 202 or not isinstance(post_response, dict):
            evidence["workflow"]["initial_post_http_status"] = post_status
            evidence["workflow"]["read_only_reconciliation"] = r4._read_reconciliation_state(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=client_request_id,
            )
            raise SmokeAbort("FAIL", "initial_workflow_post_was_not_accepted")
        workflow_id, run_id = r4._bridge_run_directory_from_response(post_response)
        if workflow_id is None or run_id is None:
            raise SmokeAbort("BLOCKED", "accepted_workflow_identity_invalid")
        evidence["workflow"].update(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "accepted_status": post_status,
            }
        )
        listing, detail, result = r4._wait_for_workflow_success(ui_port, token, workflow_id)
        saved = _read_three_job_saved_state(
            state_root=state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_root=workspace_root,
            ui_port=ui_port,
        )
        r4._wait_for_no_workers(process.pid)
        api_success = _validate_api_success(
            workflow_id=workflow_id,
            run_id=run_id,
            listing=listing,
            detail=detail,
            result=result,
            saved=saved,
        )
        evidence["jobs"] = [
            {
                **r4._job_summary(row),
                "request_key": row.get("request_key"),
                "request_sha256": row.get("request_digest"),
                "dispatch_count": saved["dispatch_counts"][str(row["job_id"])],
            }
            for row in saved["jobs"]
        ]
        evidence["new_codex_job_count"] = len(saved["jobs"])
        evidence["new_codex_thread_count"] = len({row.get("thread_id") for row in saved["jobs"]})
        evidence["maximum_active_codex_jobs"] = saved["maximum_active"]
        evidence["workflow"].update(
            {
                "status": detail.get("status"),
                "dbos_status": saved["dbos_status"],
                "worker_state": saved["worker_status"].get("state"),
                "definition_sha256": saved["manifest"].get("definition_sha256"),
                "input_sha256": saved["manifest"].get("input_sha256"),
                "request_sha256": saved["manifest"].get("request_digest"),
                "final_texts": api_success["final_texts"],
                "result_numbers": [5, 9, 17, 21],
                "dispatch_counts": list(saved["dispatch_counts"].values()),
                "dispatch_count_source": "one MAYBE_SENT event per V2 job ledger row",
            }
        )
        evidence["checks"]["single_live_workflow_post"] = {
            "status": "PASS",
            "accepted_http_status": 202,
            "post_count": 1,
        }
        evidence["checks"]["workflow_api_identity_and_jobs"] = {
            "status": "PASS",
            "list_detail_result_ids_match": True,
            "ordered_jobs": 3,
            "additional_jobs": 0,
        }
        evidence["checks"]["immutable_hashes_and_v2_ledger"] = {
            "status": "PASS",
            "request_rows": 1,
            "job_rows": 3,
            "schema_version": 2,
            "dbos_status": saved["dbos_status"],
            "worker_exit_code": saved["worker_status"].get("exit_code"),
        }
        evidence["checks"]["expected_result_and_final_texts"] = {
            "status": "PASS",
            "result_numbers": [5, 9, 17, 21],
            "final_texts": api_success["final_texts"],
        }
        evidence["checks"]["sequential_dispatch_and_concurrency"] = {
            "status": "PASS",
            "dispatch_counts": list(saved["dispatch_counts"].values()),
            "dispatch_count_source": "MAYBE_SENT event count per job",
            "maximum_active_running": saved["maximum_active"],
            "event_order": "job 1 success before job 2 dispatch before job 3 dispatch",
        }

        before_jobs = saved["jobs"]
        before_events = saved["events"]
        before_rows = r4._read_request_ledger(state_root)
        evidence["same_id_replay_post_attempted"] = True
        evidence["same_id_replay_post_count"] = 1
        try:
            replay_status, replay_response = r4._http_json(
                ui_port,
                "POST",
                "/ui-api/control/workflows",
                token=token,
                body=body,
                timeout=25.0,
            )
        except SmokeAbort:
            evidence["workflow"]["same_id_replay_read_only_reconciliation"] = (
                r4._read_reconciliation_state(
                    state_root=state_root,
                    ui_port=ui_port,
                    token=token,
                    client_request_id=client_request_id,
                )
            )
            raise SmokeAbort("BLOCKED", "same_id_replay_outcome_unknown_no_retry") from None
        if (
            replay_status != 202
            or not isinstance(replay_response, dict)
            or replay_response.get("workflow_id") != workflow_id
            or replay_response.get("run_id") != run_id
        ):
            raise SmokeAbort("BLOCKED", "same_id_replay_identity_or_response_mismatch")
        after_saved = _read_three_job_saved_state(
            state_root=state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_root=workspace_root,
            ui_port=ui_port,
        )
        after_jobs = after_saved["jobs"]
        after_events = after_saved["events"]
        after_rows = r4._read_request_ledger(state_root)
        if (
            before_jobs != after_jobs
            or before_events != after_events
            or before_rows != after_rows
            or len(after_jobs) != 3
            or r4._worker_process_count(process.pid) != 0
        ):
            raise SmokeAbort("BLOCKED", "same_id_replay_created_new_job_or_worker")
        evidence["workflow"]["same_id_replay_accepted_status"] = replay_status
        evidence["checks"]["same_id_replay"] = {
            "status": "PASS",
            "same_id": True,
            "exact_same_body": True,
            "job_rows_unchanged": True,
            "new_threads": 0,
            "additional_jobs": 0,
        }

        _verify_console_qt_get(
            ui_port=ui_port,
            token=token,
            workflow_id=workflow_id,
            expected_detail=detail,
            expected_result=result,
            evidence=evidence,
            label="before-restart",
        )
        evidence["checks"]["console_qt_get_readback"] = {
            "status": "PASS",
            "real_get_detail": True,
            "real_get_result": True,
            "new_post": False,
            "after_same_id_replay": True,
        }

        if process is None or ports is None:
            raise SmokeAbort("BLOCKED", "initial_isolated_bridge_identity_lost")
        if not r4._process_still_matches(process.pid, float(bridge_owner["created_at_epoch"])):
            raise SmokeAbort("BLOCKED", "initial_isolated_bridge_process_identity_changed")
        r4._owned_listener_pids(process.pid, ports)
        first_stop = r4._stop_bridge(process, ui_port=ui_port, token=token)
        process_records.append(
            {
                "role": "initial",
                "pid": process.pid,
                "created_at_epoch": float(bridge_owner["created_at_epoch"]),
                **first_stop,
            }
        )
        cleanup_reaped = bool(first_stop.get("reaped"))
        if stderr_relay is not None:
            stderr_relay.finish_after_bridge(bridge_reaped=cleanup_reaped)
        if not cleanup_reaped or first_stop.get("shutdown_http_status") != 202:
            raise SmokeAbort("BLOCKED", "initial_isolated_bridge_graceful_shutdown_unconfirmed")
        process = None
        stderr_relay = None
        r4._assert_ports_free(ports)

        process, stderr_relay = r4._start_bridge(child_environment)
        stderr_relays.append(stderr_relay)
        restart_owner = _record_bridge_owner(process)
        restart_owner["role"] = "restart"
        evidence["isolated_processes"].append(restart_owner)
        r4._wait_for_bridge(process, ui_port)
        restart_preflight = r4._preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        if r4._worker_process_count(process.pid) != 0:
            raise SmokeAbort("BLOCKED", "bridge_restart_started_automatic_workflow_worker")
        restarted_listing, restarted_detail, restarted_result = r4._wait_for_workflow_success(
            ui_port, token, workflow_id
        )
        restarted_saved = _read_three_job_saved_state(
            state_root=state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_root=workspace_root,
            ui_port=ui_port,
        )
        _validate_api_success(
            workflow_id=workflow_id,
            run_id=run_id,
            listing=restarted_listing,
            detail=restarted_detail,
            result=restarted_result,
            saved=restarted_saved,
        )
        if (
            restarted_saved["jobs"] != saved["jobs"]
            or restarted_saved["dbos_status"] != saved["dbos_status"]
            or restarted_detail != detail
            or restarted_result != result
            or restarted_saved["events"] != saved["events"]
            or r4._read_request_ledger(state_root) != before_rows
        ):
            raise SmokeAbort("BLOCKED", "bridge_restart_readback_changed")
        _verify_console_qt_get(
            ui_port=ui_port,
            token=token,
            workflow_id=workflow_id,
            expected_detail=restarted_detail,
            expected_result=restarted_result,
            evidence=evidence,
            label="after-restart",
        )
        evidence["checks"]["bridge_restart_get_readback"] = {
            "status": "PASS",
            "same_workflow_run_jobs_result": True,
            "restarted_workers": 0,
            "no_automatic_job_start": True,
            "appserver_ready": restart_preflight["appserver_pid"] > 0,
            "qt_get_detail_and_result": True,
        }
        _assert_existing_evidence_unchanged(old_evidence_hashes)
        evidence["checks"]["existing_evidence_unchanged"] = {
            "status": "PASS",
            "file_count": len(old_evidence_hashes),
        }
        safe_to_stop = True
        final_success = True
        evidence["status"] = "PASS"
        evidence["reason"] = None
        return 0
    except SmokeAbort as error:
        evidence["status"] = error.status
        evidence["reason"] = error.code
        return 1
    except Exception as error:
        evidence["status"] = "FAIL"
        evidence["reason"] = f"runner_unexpected_{type(error).__name__}"
        return 1
    finally:
        for reservation in reservations:
            reservation.close()
        if process is not None:
            owner_records = evidence["isolated_processes"]
            if safe_to_stop and (not owner_records or "created_at_epoch" not in owner_records[-1]):
                safe_to_stop = False
                cleanup_reaped = False
                if evidence.get("status") == "PASS":
                    evidence["status"] = "BLOCKED"
                    evidence["reason"] = "isolated_bridge_owner_not_verified_before_shutdown"
            if safe_to_stop:
                try:
                    owner = owner_records[-1]
                    if not r4._process_still_matches(process.pid, float(owner["created_at_epoch"])):
                        raise SmokeAbort(
                            "BLOCKED", "isolated_bridge_process_identity_changed_before_shutdown"
                        )
                    if ports is not None:
                        r4._owned_listener_pids(process.pid, ports)
                    stopped = r4._stop_bridge(
                        process, ui_port=ports[1] if ports else 0, token=token
                    )
                except SmokeAbort as error:
                    stopped = {"reaped": False, "forced_termination": False, "reason": error.code}
                    if evidence.get("status") == "PASS":
                        evidence["status"] = error.status
                        evidence["reason"] = error.code
                process_records.append(
                    {
                        "role": "final",
                        "pid": process.pid,
                        "created_at_epoch": float(owner_records[-1]["created_at_epoch"]),
                        **stopped,
                    }
                )
                cleanup_reaped = cleanup_reaped and bool(stopped.get("reaped"))
                if (
                    evidence["live_workflow_post_attempted"]
                    and stopped.get("shutdown_http_status") != 202
                ):
                    cleanup_reaped = False
                    if evidence.get("status") == "PASS":
                        evidence["status"] = "BLOCKED"
                        evidence["reason"] = "isolated_bridge_graceful_shutdown_unconfirmed"
                if stderr_relay is not None:
                    stderr_relay.finish_after_bridge(bridge_reaped=bool(stopped.get("reaped")))
                if stopped.get("reaped") and ports is not None:
                    try:
                        r4._assert_ports_free(ports)
                    except SmokeAbort:
                        cleanup_reaped = False
            else:
                cleanup_reaped = False
                if stderr_relay is not None:
                    stderr_relay.finish_after_bridge(bridge_reaped=False)
        cleanup_reaped = _owned_children_reaped(evidence["isolated_processes"], process_records)
        evidence["cleanup"]["bridge_processes"] = process_records
        evidence["cleanup"]["forced_termination"] = False
        evidence["cleanup"]["owned_children_reaped"] = cleanup_reaped
        for relay in stderr_relays:
            relay.collector.wait(timeout=0.25)
        if temp_root is not None:
            evidence["cleanup"]["isolated_root_label"] = temp_root.name
        if (
            temp_root is not None
            and cleanup_reaped
            and safe_to_stop
            and (not evidence["live_workflow_post_attempted"] or final_success)
        ):
            removed = _remove_owned_temp_root(temp_root)
            evidence["cleanup"]["isolated_temp_state_removed"] = removed
            evidence["cleanup"]["status"] = "CLEANED" if removed else "NOT_RUN"
            if not removed and evidence.get("status") == "PASS":
                evidence["status"] = "BLOCKED"
                evidence["reason"] = "isolated_temp_cleanup_not_confirmed"
        elif evidence["live_workflow_post_attempted"]:
            evidence["cleanup"]["status"] = "PRESERVED_ACTIVE_OR_UNKNOWN"
            evidence["cleanup"]["isolated_temp_state_removed"] = False
        r4._http_json = original_http_json
        try:
            expected_hashes = evidence.get("previous_evidence_sha256")
            if isinstance(expected_hashes, dict) and expected_hashes:
                _assert_existing_evidence_unchanged(expected_hashes)
            if temp_root is not None:
                r4._no_sensitive_evidence(
                    evidence,
                    token=token,
                    temp_root=temp_root,
                    sensitive_strings=prompt_material,
                )
        except SmokeAbort as error:
            if evidence.get("status") == "PASS":
                evidence["status"] = error.status
                evidence["reason"] = error.code
        evidence["http_counts"] = counters
        evidence["finished_at_utc"] = _utc_now()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Phase 5F-1B three-job E2E once")
    parser.add_argument(
        "--retest-once",
        action="store_true",
        help="write one explicit retest to the separate R1 evidence path",
    )
    args = parser.parse_args(argv)
    try:
        evidence_path = _select_evidence_path(retest_once=args.retest_once)
    except SmokeAbort as error:
        print(json.dumps({"phase": "5F-1B", "status": error.status, "reason": error.code}))
        return 2
    try:
        commit_sha = _current_commit()
    except SmokeAbort:
        commit_sha = None
    evidence = _new_evidence(commit_sha)
    evidence["run_attempt"] = "R1" if evidence_path == R1_EVIDENCE_PATH else "initial"
    status_code = _run_e2e(evidence, evidence_path=evidence_path)
    try:
        r4.claim_evidence_path(evidence_path, evidence)
    except OSError:
        print(
            json.dumps({"phase": "5F-1B", "status": "BLOCKED", "reason": "evidence_write_failed"})
        )
        return 2
    print(
        json.dumps(
            {
                "phase": evidence["phase"],
                "status": evidence["status"],
                "reason": evidence["reason"],
                "evidence": evidence_path.relative_to(REPOSITORY_ROOT).as_posix(),
                "live_workflow_post_count": evidence["live_workflow_post_count"],
                "same_id_replay_post_count": evidence["same_id_replay_post_count"],
                "new_codex_job_count": evidence["new_codex_job_count"],
            },
            sort_keys=True,
        )
    )
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
