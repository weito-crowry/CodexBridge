from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import psutil
import yaml
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

try:
    from .phase5e_smoke_support import (
        PRODUCTION_PORTS,
        build_phase5e2c2_environment,
        claim_evidence_path,
        ports_are_safe,
        roots_are_separate,
    )
except ImportError:
    from phase5e_smoke_support import (
        PRODUCTION_PORTS,
        build_phase5e2c2_environment,
        claim_evidence_path,
        ports_are_safe,
        roots_are_separate,
    )

from codex_bridge.codex_resolver import resolve_codex_executable
from codex_bridge.config import BridgeConfig
from codex_bridge.console.api_client import ApiClient
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog
from codex_bridge.server import prepare_config
from codex_bridge.workflows.interpreter import validate_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5e-2c2-evidence.json"
RETEST_EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5e-2c2-retest-evidence.json"
DIAGNOSTIC_RETEST_EVIDENCE_PATH = (
    REPOSITORY_ROOT / "docs" / "phase5e-2c2-diagnostic-retest-evidence.json"
)
R3_EVIDENCE_PATH = DIAGNOSTIC_RETEST_EVIDENCE_PATH
R4_EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5e-2c2-r4-evidence.json"
FIRST_JOB_FIXTURE_PATH = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"
SECOND_PROMPT_ENVIRONMENT_VARIABLE = "CODEXBRIDGE_PHASE5E2C2_SECOND_PROMPT"
APPROVED_SECOND_PROMPT_SHA256 = "0e12daed6b090fc7c47bf72ecb37e50a69d39e7b1e3bacf02793c453e31fcf05"
R4_TESTED_FIX_COMMIT = "1e8255f1086e7fb5111450f2c5454c8f01f5d008"
R4_RETEST_REASON = "verify_transient_status_history_recheck_fix"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_START_TIMEOUT_SECONDS = 45.0
WORKFLOW_TIMEOUT_SECONDS = 300.0
SHUTDOWN_TIMEOUT_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 10.0
MAX_HTTP_BODY_BYTES = 128 * 1024
MAX_LOCAL_JSON_BYTES = 256 * 1024
MAX_AUTH_BYTES = 1024 * 1024
MAX_PREVIOUS_EVIDENCE_BYTES = 64 * 1024
MAX_STARTUP_DIAGNOSTIC_BYTES = 4 * 1024
MAX_STARTUP_DIAGNOSTIC_STAGE_EVENTS = 16
MAX_DIAGNOSTIC_EVIDENCE_BYTES = 8 * 1024
STDERR_READ_CHUNK_BYTES = 1024
MAX_STDERR_DIAGNOSTIC_LINE_BYTES = 512
MAX_STDERR_DIAGNOSTIC_EVENTS = 8
STDERR_RELAY_START_TIMEOUT_SECONDS = 10.0
STDERR_RELAY_EOF_TIMEOUT_SECONDS = 5.0
STDERR_RELAY_READY_LINE = "workflow.stderr_relay.ready"
STDERR_RELAY_EVENTS_TRUNCATED_LINE = "workflow.stderr_relay.events_truncated"
STDERR_RELAY_LONG_LINE_DISCARDED_LINE = "workflow.stderr_relay.long_line_discarded"
PREVIOUS_EVIDENCE_REASON = "runner_definition_hash_verifier_added_a_second_newline"
RETEST_REASON = "previous_runner_hash_validation_false_positive"
DIAGNOSTIC_RETEST_REASON = "capture_workflow_startup_diagnostic_after_reconciliation"
STARTUP_DIAGNOSTIC_FILENAME = "startup-diagnostic.json"
STARTUP_DIAGNOSTIC_EVENT_ID = "workflow.startup_diagnostic.persistence_failed"
KNOWN_STARTUP_STAGES = frozenset(
    {
        "request_reserved",
        "starting_state_persisted",
        "manager_validation",
        "worker_config_write",
        "worker_spawn",
        "worker_readiness_wait",
    }
)
KNOWN_STARTUP_FAILURE_REASONS = frozenset(
    {
        "exception",
        "readiness_inspection_failed",
        "readiness_timeout",
        "readiness_unconfirmed",
        "worker_failed",
    }
)
PERSISTENCE_FAILURE_REASON_CODES = frozenset(
    {"diagnostic_payload_too_large", "diagnostic_persistence_failed"}
)
_SAFE_EXCEPTION_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_WORKFLOW_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)
_RUN_ID = re.compile(r"run-[0-9a-f]{32}\Z", re.ASCII)
KNOWN_JOB_STATES = frozenset(
    {"PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"}
)


class SmokeAbort(RuntimeError):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fixed_first_prompt() -> str:
    try:
        fixture_text = FIRST_JOB_FIXTURE_PATH.read_text(encoding="utf-8")
        fixture = yaml.safe_load(fixture_text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise SmokeAbort("BLOCKED", "fixed_first_job_fixture_unavailable") from None
    if (
        hashlib.sha256(fixture_text.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        != "dfa53ef7b1d59f9c96ee1fd2eb2f55df1394115fc78de2e8826a8cd905f1e34a"
        or not isinstance(fixture, dict)
        or not isinstance(fixture.get("document"), dict)
        or fixture["document"].get("dsl") != "1.0.3"
        or not isinstance(fixture.get("do"), list)
        or not fixture["do"]
        or not isinstance(fixture["do"][0], dict)
    ):
        raise SmokeAbort("BLOCKED", "fixed_first_job_fixture_changed_or_invalid")
    calculate = fixture["do"][0].get("calculate")
    task = calculate.get("with") if isinstance(calculate, dict) else None
    prompt = task.get("prompt") if isinstance(task, dict) else None
    if (
        not isinstance(calculate, dict)
        or calculate.get("call") != "codex-job"
        or not isinstance(task, dict)
        or not isinstance(prompt, str)
    ):
        raise SmokeAbort("BLOCKED", "fixed_first_job_fixture_changed_or_invalid")
    return prompt


def approved_second_prompt(environ: Mapping[str, str]) -> str:
    prompt = environ.get(SECOND_PROMPT_ENVIRONMENT_VARIABLE)
    if not isinstance(prompt, str) or not prompt:
        raise SmokeAbort("BLOCKED", "approved_second_prompt_unavailable")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != APPROVED_SECOND_PROMPT_SHA256:
        raise SmokeAbort("BLOCKED", "second_prompt_hash_mismatch")
    return prompt


def build_two_job_workflow_yaml(*, second_prompt: str) -> str:
    workflow = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "phase5e2c2-two-sequential-codex-jobs",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "first": {
                    "call": "codex-job",
                    "with": {"prompt": _fixed_first_prompt()},
                }
            },
            {
                "second": {
                    "call": "codex-job",
                    "with": {
                        "prompt": second_prompt,
                        "prompt_from": "${ .result.final_text }",
                    },
                }
            },
        ],
    }
    return yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False)


def definition_sha256(stored_definition_text: str) -> str:
    return hashlib.sha256(stored_definition_text.encode("utf-8")).hexdigest()


def build_two_job_payload(
    *, workflow_yaml: str, client_request_id: str, workspace_root: str
) -> dict[str, object]:
    return {
        "client_request_id": client_request_id,
        "workflow_yaml": workflow_yaml,
        "input": {},
        "workspace_root": workspace_root,
        "max_live_codex_jobs": 2,
    }


def maximum_active_codex_jobs(events: Sequence[Mapping[str, object]]) -> int:
    active: set[str] = set()
    maximum = 0
    for event in sorted(events, key=lambda row: row.get("event_id", 0)):
        job_id = event.get("job_id")
        state = event.get("state")
        if not isinstance(job_id, str) or not isinstance(state, str):
            continue
        if state == "MAYBE_SENT":
            active.add(job_id)
        elif state in {"SUCCEEDED", "FAILED"}:
            active.discard(job_id)
        maximum = max(maximum, len(active))
    return maximum


def _new_evidence(
    *,
    previous_evidence_sha256: str | None = None,
    diagnostic_retest_sha256: Mapping[str, str] | None = None,
    r4_baseline_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    check_names = (
        "native_windows",
        "dedicated_loopback_ports",
        "isolated_environment_and_roots",
        "bridge_preflight_and_authentication",
        "workspace_allowed_root_and_cwd",
        "single_live_workflow_post",
        "workflow_api_identity_and_jobs",
        "immutable_hashes_and_ledgers",
        "expected_final_texts",
        "active_job_concurrency",
        "console_qt_get_readback",
        "same_id_replay",
        "bridge_restart_readback",
        "production_side_effects",
    )
    evidence: dict[str, Any] = {
        "phase": (
            "5E-2C2-R4"
            if r4_baseline_sha256 is not None
            else "5E-2C2-R3"
            if diagnostic_retest_sha256 is not None
            else "5E-2C2-R"
            if previous_evidence_sha256 is not None
            else "5E-2C2"
        ),
        "started_at_utc": _utc_now(),
        "status": "RUNNING",
        "ports": {},
        "workflow": {},
        "jobs": [],
        "checks": {name: {"status": "NOT_RUN"} for name in check_names},
        "live_workflow_post_attempted": False,
        "live_workflow_post_count": 0,
        "same_id_replay_post_attempted": False,
        "same_id_replay_post_count": 0,
        "new_codex_job_count": 0,
        "maximum_active_codex_jobs": None,
        "production_side_effect_count": 0,
        "cleanup": {"status": "NOT_RUN", "forced_termination": False},
    }
    if previous_evidence_sha256 is not None:
        evidence.update(
            {
                "retry_of": "5E-2C2",
                "retest_reason": RETEST_REASON,
                "previous_evidence_path": "docs/phase5e-2c2-evidence.json",
                "previous_evidence_sha256": previous_evidence_sha256,
            }
        )
    if diagnostic_retest_sha256 is not None:
        evidence.update(
            {
                "retry_of": "5E-2C2-R",
                "retest_reason": DIAGNOSTIC_RETEST_REASON,
                "previous_evidence": {
                    "original": {
                        "path": "docs/phase5e-2c2-evidence.json",
                        "sha256": diagnostic_retest_sha256["original"],
                    },
                    "r2": {
                        "path": "docs/phase5e-2c2-retest-evidence.json",
                        "sha256": diagnostic_retest_sha256["r2"],
                    },
                },
            }
        )
    if r4_baseline_sha256 is not None:
        evidence.update(
            {
                "retry_of": "5E-2C2-R3",
                "retest_reason": R4_RETEST_REASON,
                "tested_fix_commit": R4_TESTED_FIX_COMMIT,
                "previous_evidence": {
                    "original": {
                        "path": "docs/phase5e-2c2-evidence.json",
                        "sha256": r4_baseline_sha256["original"],
                    },
                    "r2": {
                        "path": "docs/phase5e-2c2-retest-evidence.json",
                        "sha256": r4_baseline_sha256["r2"],
                    },
                    "r3": {
                        "path": "docs/phase5e-2c2-diagnostic-retest-evidence.json",
                        "sha256": r4_baseline_sha256["r3"],
                    },
                },
            }
        )
    return evidence


def _save_evidence(evidence: dict[str, Any], path: Path = EVIDENCE_PATH) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_previous_evidence(path: Path) -> str:
    return _validated_previous_evidence_hashes(path)[0]


def _validated_previous_evidence_hashes(path: Path) -> tuple[str, str]:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        if path.stat().st_size > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        with path.open("rb") as stream:
            encoded = stream.read(MAX_PREVIOUS_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        previous = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "previous_evidence_unexpected") from None

    if not isinstance(previous, dict):
        raise SmokeAbort("BLOCKED", "previous_evidence_unexpected")
    workflow = previous.get("workflow")
    checks = previous.get("checks")
    jobs = previous.get("jobs")
    if not isinstance(workflow, dict) or not isinstance(checks, dict) or not isinstance(jobs, list):
        raise SmokeAbort("BLOCKED", "previous_evidence_unexpected")
    if any(not isinstance(row, dict) for row in jobs):
        raise SmokeAbort("BLOCKED", "previous_evidence_unexpected")
    check_statuses = {
        name: check.get("status") if isinstance(check, dict) else None
        for name, check in checks.items()
    }
    job_ids = [row.get("job_id") for row in jobs]
    thread_ids = [row.get("thread_id") for row in jobs]
    turn_ids = [row.get("turn_id") for row in jobs]
    if (
        previous.get("phase") != "5E-2C2"
        or previous.get("status") != "BLOCKED"
        or previous.get("reason") != PREVIOUS_EVIDENCE_REASON
        or type(previous.get("live_workflow_post_count")) is not int
        or previous.get("live_workflow_post_count") != 1
        or type(previous.get("new_codex_job_count")) is not int
        or previous.get("new_codex_job_count") != 2
        or type(previous.get("new_codex_thread_count")) is not int
        or previous.get("new_codex_thread_count") != 2
        or type(previous.get("maximum_active_codex_jobs")) is not int
        or previous.get("maximum_active_codex_jobs") != 1
        or workflow.get("final_texts") != ["4", "5"]
        or len(jobs) != 2
        or any(not isinstance(value, str) or not value for value in job_ids + thread_ids + turn_ids)
        or len(set(job_ids)) != 2
        or len(set(thread_ids)) != 2
        or len(set(turn_ids)) != 2
        or any(row.get("state") != "SUCCEEDED" for row in jobs)
        or any(
            check_statuses.get(name) != "PASS"
            for name in (
                "expected_final_texts",
                "immutable_hashes_and_ledgers",
                "workflow_api_identity_and_jobs",
                "active_job_concurrency",
            )
        )
        or any(
            check_statuses.get(name) != "NOT_RUN"
            for name in (
                "same_id_replay",
                "bridge_restart_readback",
                "console_qt_get_readback",
            )
        )
    ):
        raise SmokeAbort("BLOCKED", "previous_evidence_unexpected")
    raw_sha256 = hashlib.sha256(encoded).hexdigest()
    normalized_sha256 = hashlib.sha256(encoded.replace(b"\r\n", b"\n")).hexdigest()
    return raw_sha256, normalized_sha256


def _validated_retest_evidence(path: Path, *, original_evidence_sha256: str) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        if path.stat().st_size > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        with path.open("rb") as stream:
            encoded = stream.read(MAX_PREVIOUS_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        retest = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "retest_evidence_unexpected") from None

    if not isinstance(retest, dict):
        raise SmokeAbort("BLOCKED", "retest_evidence_unexpected")
    workflow = retest.get("workflow")
    checks = retest.get("checks")
    cleanup = retest.get("cleanup")
    expected_check_statuses = {
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
    if (
        not isinstance(workflow, dict)
        or not isinstance(checks, dict)
        or not isinstance(cleanup, dict)
    ):
        raise SmokeAbort("BLOCKED", "retest_evidence_unexpected")
    actual_check_statuses = {
        name: check.get("status") if isinstance(check, dict) else None
        for name, check in checks.items()
    }
    if (
        retest.get("phase") != "5E-2C2-R"
        or retest.get("status") != "FAIL"
        or retest.get("reason") != "initial_workflow_post_was_not_accepted"
        or retest.get("retry_of") != "5E-2C2"
        or retest.get("retest_reason") != RETEST_REASON
        or retest.get("previous_evidence_path") != "docs/phase5e-2c2-evidence.json"
        or retest.get("previous_evidence_sha256") != original_evidence_sha256
        or retest.get("live_workflow_post_attempted") is not True
        or type(retest.get("live_workflow_post_count")) is not int
        or retest.get("live_workflow_post_count") != 1
        or retest.get("same_id_replay_post_attempted") is not False
        or type(retest.get("same_id_replay_post_count")) is not int
        or retest.get("same_id_replay_post_count") != 0
        or type(retest.get("new_codex_job_count")) is not int
        or retest.get("new_codex_job_count") != 0
        or retest.get("new_codex_thread_count") is not None
        or retest.get("maximum_active_codex_jobs") is not None
        or type(retest.get("production_side_effect_count")) is not int
        or retest.get("production_side_effect_count") != 0
        or type(workflow.get("initial_post_http_status")) is not int
        or workflow.get("initial_post_http_status") != 409
        or actual_check_statuses != expected_check_statuses
        or cleanup.get("status") != "PRESERVED_ACTIVE_OR_UNKNOWN"
        or cleanup.get("forced_termination") is not False
    ):
        raise SmokeAbort("BLOCKED", "retest_evidence_unexpected")
    return hashlib.sha256(encoded).hexdigest()


def _select_diagnostic_retest_evidence() -> tuple[Path, dict[str, str]]:
    if DIAGNOSTIC_RETEST_EVIDENCE_PATH.exists() or DIAGNOSTIC_RETEST_EVIDENCE_PATH.is_symlink():
        raise SmokeAbort("BLOCKED", "diagnostic_retest_evidence_already_exists")
    original_sha256, normalized_original_sha256 = _validated_previous_evidence_hashes(EVIDENCE_PATH)
    r2_sha256 = _validated_retest_evidence(
        RETEST_EVIDENCE_PATH,
        original_evidence_sha256=normalized_original_sha256,
    )
    return DIAGNOSTIC_RETEST_EVIDENCE_PATH, {"original": original_sha256, "r2": r2_sha256}


def _validated_r3_evidence(
    path: Path,
    *,
    original_evidence_sha256: str,
    r2_evidence_sha256: str,
) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        if path.stat().st_size > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        with path.open("rb") as stream:
            encoded = stream.read(MAX_PREVIOUS_EVIDENCE_BYTES + 1)
        if len(encoded) > MAX_PREVIOUS_EVIDENCE_BYTES:
            raise OSError
        r3 = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "r3_evidence_unexpected") from None

    if not isinstance(r3, dict):
        raise SmokeAbort("BLOCKED", "r3_evidence_unexpected")
    workflow = r3.get("workflow")
    cleanup = r3.get("cleanup")
    previous_evidence = r3.get("previous_evidence")
    if (
        not isinstance(workflow, dict)
        or not isinstance(cleanup, dict)
        or not isinstance(previous_evidence, dict)
    ):
        raise SmokeAbort("BLOCKED", "r3_evidence_unexpected")
    original = previous_evidence.get("original")
    r2 = previous_evidence.get("r2")
    if not isinstance(original, dict) or not isinstance(r2, dict):
        raise SmokeAbort("BLOCKED", "r3_evidence_unexpected")

    workflow_id = workflow.get("workflow_id")
    run_id = workflow.get("run_id")
    if (
        r3.get("phase") != "5E-2C2-R3"
        or r3.get("status") != "BLOCKED"
        or r3.get("reason") != "workflow_api_requires_reconciliation"
        or r3.get("live_workflow_post_attempted") is not True
        or type(r3.get("live_workflow_post_count")) is not int
        or r3.get("live_workflow_post_count") != 1
        or type(workflow.get("accepted_status")) is not int
        or workflow.get("accepted_status") != 202
        or not isinstance(workflow_id, str)
        or _WORKFLOW_ID.fullmatch(workflow_id) is None
        or not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or r3.get("same_id_replay_post_attempted") is not False
        or type(r3.get("same_id_replay_post_count")) is not int
        or r3.get("same_id_replay_post_count") != 0
        or cleanup.get("status") != "PRESERVED_ACTIVE_OR_UNKNOWN"
        or cleanup.get("forced_termination") is not False
        or original.get("path") != "docs/phase5e-2c2-evidence.json"
        or original.get("sha256") != original_evidence_sha256
        or r2.get("path") != "docs/phase5e-2c2-retest-evidence.json"
        or r2.get("sha256") != r2_evidence_sha256
    ):
        raise SmokeAbort("BLOCKED", "r3_evidence_unexpected")
    return hashlib.sha256(encoded).hexdigest()


def _select_r4_evidence() -> tuple[Path, dict[str, str]]:
    if R4_EVIDENCE_PATH.exists() or R4_EVIDENCE_PATH.is_symlink():
        raise SmokeAbort("BLOCKED", "r4_evidence_already_exists")
    original_sha256, normalized_original_sha256 = _validated_previous_evidence_hashes(EVIDENCE_PATH)
    r2_sha256 = _validated_retest_evidence(
        RETEST_EVIDENCE_PATH,
        original_evidence_sha256=normalized_original_sha256,
    )
    r3_sha256 = _validated_r3_evidence(
        R3_EVIDENCE_PATH,
        original_evidence_sha256=original_sha256,
        r2_evidence_sha256=r2_sha256,
    )
    return R4_EVIDENCE_PATH, {
        "original": original_sha256,
        "r2": r2_sha256,
        "r3": r3_sha256,
    }


def _select_evidence_paths(*, retest_once: bool) -> tuple[Path, str | None]:
    if not retest_once:
        return EVIDENCE_PATH, None
    if RETEST_EVIDENCE_PATH.exists() or RETEST_EVIDENCE_PATH.is_symlink():
        raise SmokeAbort("BLOCKED", "retest_evidence_already_exists")
    previous_sha256 = _validated_previous_evidence(EVIDENCE_PATH)
    return RETEST_EVIDENCE_PATH, previous_sha256


def _mark_pass(evidence: dict[str, Any], name: str, **details: Any) -> None:
    evidence["checks"][name] = {"status": "PASS", **details}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_bridge_config(
    path: Path,
    *,
    mcp_port: int,
    ui_port: int,
    workspace_root: Path,
    codex_executable: Path,
) -> None:
    contents = "\n".join(
        (
            "[bridge]",
            f"host = {_toml_string(BRIDGE_HOST)}",
            f"port = {mcp_port}",
            f"allowed_roots = [{_toml_string(str(workspace_root))}]",
            "allowed_hosts = []",
            "allowed_origins = []",
            "lan_enabled = false",
            f"codex_executable = {_toml_string(str(codex_executable))}",
            "wait_default_seconds = 50.0",
            "wait_max_seconds = 55.0",
            "shutdown_grace_seconds = 5.0",
            "",
            "[console]",
            f"ui_port = {ui_port}",
            "",
            "[github_mcp]",
            "enabled = false",
            "",
            "[targets.local]",
            'name = "Isolated local execution"',
            'kind = "local"',
            "",
        )
    )
    path.write_text(contents, encoding="utf-8", newline="\n")


def _isolated_path_entries(
    base_environ: Mapping[str, str], codex_executable: Path
) -> tuple[str, ...]:
    system_root = base_environ.get("SYSTEMROOT") or base_environ.get("WINDIR")
    if not isinstance(system_root, str) or not system_root:
        raise SmokeAbort("BLOCKED", "windows_system_root_unavailable")
    candidates = [
        Path(system_root) / "System32",
        Path(sys.executable).resolve(strict=True).parent,
        codex_executable.resolve(strict=True).parent,
    ]
    inherited_path = base_environ.get("PATH", "")
    for raw_entry in inherited_path.split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        directory = Path(entry)
        try:
            if (directory / "node.exe").is_file():
                candidates.append(directory.resolve(strict=True))
        except OSError:
            continue
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(str(resolved))
    if not unique:
        raise SmokeAbort("BLOCKED", "isolated_executable_search_path_unavailable")
    return tuple(unique)


def _source_codex_home(base_environ: Mapping[str, str]) -> Path:
    configured = base_environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _copy_authentication(source_home: Path, isolated_home: Path) -> None:
    source = source_home / "auth.json"
    if source.is_symlink() or not source.is_file():
        raise SmokeAbort("BLOCKED", "codex_authentication_file_unavailable")
    try:
        if source.stat().st_size > MAX_AUTH_BYTES:
            raise SmokeAbort("BLOCKED", "codex_authentication_file_exceeded_limit")
    except OSError:
        raise SmokeAbort("BLOCKED", "codex_authentication_file_unavailable") from None
    if isolated_home.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_codex_home_is_a_symlink")
    isolated_home.mkdir(parents=True, exist_ok=False)
    destination = isolated_home / "auth.json"
    try:
        shutil.copyfile(source, destination)
    except OSError:
        destination.unlink(missing_ok=True)
        raise SmokeAbort("BLOCKED", "isolated_codex_authentication_copy_failed") from None


def _reserve_loopback_ports() -> tuple[tuple[int, int], list[socket.socket]]:
    reservations: list[socket.socket] = []
    ports: list[int] = []
    try:
        while len(ports) < 2:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                reservation.bind((BRIDGE_HOST, 0))
            except OSError:
                reservation.close()
                raise SmokeAbort("BLOCKED", "loopback_port_reservation_failed") from None
            port = int(reservation.getsockname()[1])
            if port in PRODUCTION_PORTS or port in ports:
                reservation.close()
                continue
            ports.append(port)
            reservations.append(reservation)
        pair = (ports[0], ports[1])
        if not ports_are_safe(*pair):
            raise SmokeAbort("BLOCKED", "loopback_port_pair_invalid")
        return pair, reservations
    except BaseException:
        for reservation in reservations:
            reservation.close()
        raise


def _assert_ports_free(ports: tuple[int, int]) -> None:
    reservations: list[socket.socket] = []
    try:
        for port in ports:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind((BRIDGE_HOST, port))
            reservations.append(reservation)
    except OSError:
        raise SmokeAbort("BLOCKED", "isolated_bridge_port_is_not_free") from None
    finally:
        for reservation in reservations:
            reservation.close()


def _process_snapshot(pid: int) -> list[tuple[int, float]]:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "owned_child_process_tree_unavailable") from None
    snapshot: list[tuple[int, float]] = []
    for child in children:
        try:
            snapshot.append((child.pid, child.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return snapshot


def _process_still_matches(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.01
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _owned_listener_pids(pid: int, ports: tuple[int, int]) -> None:
    try:
        connections = psutil.net_connections(kind="tcp")
        process_tree = {pid, *(child_pid for child_pid, _ in _process_snapshot(pid))}
    except (psutil.AccessDenied, OSError):
        raise SmokeAbort("BLOCKED", "listener_ownership_could_not_be_verified") from None
    owners = {port: set() for port in ports}
    for connection in connections:
        address = connection.laddr
        if connection.status != psutil.CONN_LISTEN or not address or address.port not in owners:
            continue
        if address.ip != BRIDGE_HOST:
            raise SmokeAbort("BLOCKED", "isolated_bridge_listener_is_not_loopback_only")
        if connection.pid is not None:
            owners[address.port].add(connection.pid)
    if any(not owners[port] or not owners[port].issubset(process_tree) for port in ports):
        raise SmokeAbort("BLOCKED", "isolated_bridge_listener_owner_mismatch")


def _codex_appserver_pid(bridge_pid: int, expected_executable: Path) -> int:
    expected = os.path.normcase(str(expected_executable.resolve(strict=True)))
    try:
        children = psutil.Process(bridge_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "codex_appserver_process_could_not_be_verified") from None
    for child in children:
        try:
            executable = os.path.normcase(str(Path(child.exe()).resolve(strict=True)))
        except (OSError, psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if executable == expected:
            return child.pid
    raise SmokeAbort("BLOCKED", "expected_windows_codex_appserver_is_not_running")


def _worker_process_count(bridge_pid: int) -> int:
    try:
        children = psutil.Process(bridge_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "owned_child_process_tree_unavailable") from None
    count = 0
    for child in children:
        try:
            if "codex_bridge.workflows.worker_entry" in child.cmdline():
                count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return count


def _wait_for_no_workers(bridge_pid: int, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    observed_empty = False
    while time.monotonic() < deadline:
        if _worker_process_count(bridge_pid) == 0:
            if observed_empty:
                return
            observed_empty = True
            time.sleep(0.5)
            continue
        observed_empty = False
        time.sleep(0.1)
    raise SmokeAbort("BLOCKED", "completed_workflow_worker_child_did_not_exit")


_STDERR_DIAGNOSTIC_EVENT = re.compile(
    rf"{re.escape(STARTUP_DIAGNOSTIC_EVENT_ID)} "
    r"stage=(?P<stage>[A-Za-z_][A-Za-z0-9_]*) "
    r"original_exception=(?P<original>none|[A-Za-z_][A-Za-z0-9_]{0,63}) "
    r"persistence_exception=(?P<persistence>[A-Za-z_][A-Za-z0-9_]{0,63})\Z",
    re.ASCII,
)
_STDERR_RELAY_SCRIPT_PATH = Path(__file__).with_name("phase5e2c2_stderr_relay.py")


class _StderrDiagnosticCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._reader: threading.Thread | None = None
        self._read_status = "NOT_STARTED"
        self._read_error_class: str | None = None
        self._line_buffer = bytearray()
        self._discarding_long_line = False
        self._events: list[dict[str, str]] = []
        self._events_truncated = False
        self._long_line_discarded = False
        self._relay_ready = threading.Event()

    def start_reader(self) -> None:
        with self._lock:
            self._read_status = "STARTING"
        reader = threading.Thread(
            target=_drain_relay_output,
            args=(self._source, self),
            name="codexbridge-stderr-relay-output-reader",
            daemon=True,
        )
        reader.start()
        self._reader = reader

    def attach(self, stream: Any) -> None:
        if stream is None:
            self._set_read_failure_name("StderrUnavailable")
            self._source.put(None)
            return
        with self._lock:
            if self._read_status != "READ_FAILED":
                self._read_status = "READING"
        self._source.put(stream)

    def finish_without_stream(self) -> None:
        with self._lock:
            self._read_status = "NOT_STARTED"
        self._source.put(None)

    def feed(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes) or len(chunk) > STDERR_READ_CHUNK_BYTES:
            self._set_read_failure_name("InvalidStderrChunk")
            return
        with self._lock:
            for byte in chunk:
                if byte == 0x0A:
                    if self._discarding_long_line:
                        self._long_line_discarded = True
                    else:
                        self._accept_line_locked(bytes(self._line_buffer))
                    self._line_buffer.clear()
                    self._discarding_long_line = False
                elif not self._discarding_long_line:
                    if len(self._line_buffer) >= MAX_STDERR_DIAGNOSTIC_LINE_BYTES:
                        self._line_buffer.clear()
                        self._discarding_long_line = True
                        self._long_line_discarded = True
                    else:
                        self._line_buffer.append(byte)

    def finish_eof(self) -> None:
        with self._lock:
            if not self._discarding_long_line and self._line_buffer:
                self._accept_line_locked(bytes(self._line_buffer))
            self._line_buffer.clear()
            self._discarding_long_line = False
            if self._read_status != "READ_FAILED":
                self._read_status = "EOF"

    def mark_read_failure(self, error: BaseException) -> None:
        self._set_read_failure_name(_safe_exception_name(type(error).__name__))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot: dict[str, Any] = {
                "read_status": self._read_status,
                "events": [dict(event) for event in self._events],
                "events_truncated": self._events_truncated,
                "long_line_discarded": self._long_line_discarded,
            }
            if self._read_error_class is not None:
                snapshot["read_error_class"] = self._read_error_class
            return snapshot

    def wait(self, timeout: float) -> bool:
        reader = self._reader
        if reader is None:
            return True
        reader.join(timeout=max(0.0, timeout))
        return not reader.is_alive()

    def wait_for_relay_ready(self, timeout: float) -> bool:
        return self._relay_ready.wait(timeout=max(0.0, timeout))

    def relay_ready(self) -> bool:
        return self._relay_ready.is_set()

    def _accept_line_locked(self, raw_line: bytes) -> None:
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            line = raw_line.decode("ascii")
        except UnicodeDecodeError:
            return
        if line == STDERR_RELAY_READY_LINE:
            self._relay_ready.set()
            return
        if line == STDERR_RELAY_EVENTS_TRUNCATED_LINE:
            self._events_truncated = True
            return
        if line == STDERR_RELAY_LONG_LINE_DISCARDED_LINE:
            self._long_line_discarded = True
            return
        match = _STDERR_DIAGNOSTIC_EVENT.fullmatch(line)
        if match is None:
            return
        stage = match.group("stage")
        original = match.group("original")
        persistence = match.group("persistence")
        if (
            stage not in KNOWN_STARTUP_STAGES
            or (original != "none" and not _SAFE_EXCEPTION_CLASS.fullmatch(original))
            or persistence == "none"
            or (
                not _SAFE_EXCEPTION_CLASS.fullmatch(persistence)
                and persistence not in PERSISTENCE_FAILURE_REASON_CODES
            )
        ):
            return
        if len(self._events) >= MAX_STDERR_DIAGNOSTIC_EVENTS:
            self._events_truncated = True
            return
        self._events.append(
            {
                "stage": stage,
                "original_exception": original,
                "persistence_exception": persistence,
            }
        )

    def _set_read_failure_name(self, exception_name: str) -> None:
        with self._lock:
            self._read_status = "READ_FAILED"
            self._read_error_class = _safe_exception_name(exception_name)


def _safe_exception_name(value: str) -> str:
    return value if _SAFE_EXCEPTION_CLASS.fullmatch(value) else "UnknownException"


def _drain_relay_output(source: queue.Queue[Any], collector: _StderrDiagnosticCollector) -> None:
    stream = source.get()
    if stream is None:
        return
    try:
        read_chunk = getattr(stream, "read1", None)
        while True:
            chunk = (
                read_chunk(STDERR_READ_CHUNK_BYTES)
                if callable(read_chunk)
                else stream.read(STDERR_READ_CHUNK_BYTES)
            )
            if not chunk:
                collector.finish_eof()
                return
            collector.feed(chunk)
            snapshot = collector.snapshot()
            if snapshot["read_status"] == "READ_FAILED":
                return
    except BaseException as error:
        collector.mark_read_failure(error)
    finally:
        try:
            stream.close()
        except BaseException as error:
            collector.mark_read_failure(error)


class _BridgeStderrRelay:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        collector: _StderrDiagnosticCollector,
    ) -> None:
        self.process = process
        self.collector = collector
        self._parent_write_close_failed = False
        self._final_record: dict[str, Any] | None = None

    def mark_parent_write_close_failed(self) -> None:
        self._parent_write_close_failed = True

    def wait_until_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.collector.relay_ready():
                return self.is_running()
            try:
                if self.process.poll() is not None:
                    return False
            except BaseException:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.collector.wait_for_relay_ready(min(0.05, remaining)):
                return self.is_running()
            if self.collector.snapshot()["read_status"] in {"EOF", "READ_FAILED"}:
                return False

    def is_running(self) -> bool:
        if self._parent_write_close_failed or not self.collector.relay_ready():
            return False
        if self.collector.snapshot()["read_status"] in {"EOF", "READ_FAILED"}:
            return False
        try:
            return self.process.poll() is None
        except BaseException:
            return False

    def snapshot(self) -> dict[str, Any]:
        if self._final_record is not None:
            return dict(self._final_record)
        try:
            returncode = self.process.poll()
        except BaseException:
            return {
                "pid": self.process.pid,
                "status": "STATUS_UNAVAILABLE",
                "failure_reason": "stderr_relay_status_unavailable",
            }
        collector_status = self.collector.snapshot()["read_status"]
        failure_reason: str | None
        if self._parent_write_close_failed:
            status = "UNAVAILABLE"
            failure_reason = "stderr_relay_parent_write_handle_not_closed"
        elif returncode is None:
            status = "RUNNING"
            failure_reason = (
                "stderr_relay_output_unavailable"
                if collector_status in {"EOF", "READ_FAILED"}
                else None
            )
        else:
            status = "EXITED_UNEXPECTEDLY"
            failure_reason = "stderr_relay_exited_while_bridge_active"
        result: dict[str, Any] = {
            "pid": self.process.pid,
            "status": status,
            "failure_reason": failure_reason,
            "output_read_status": collector_status,
        }
        if returncode is not None:
            result["exit_code"] = returncode
        return result

    def finish_after_bridge(self, *, bridge_reaped: bool) -> dict[str, Any]:
        if self._final_record is not None:
            return dict(self._final_record)
        failure_reason: str | None = None
        try:
            returncode = self.process.poll()
        except BaseException:
            returncode = None
            failure_reason = "stderr_relay_status_unavailable"

        if not bridge_reaped:
            if returncode is None:
                status = "PRESERVED_WITH_BRIDGE_OR_UNKNOWN"
                if self._parent_write_close_failed:
                    failure_reason = "stderr_relay_parent_write_handle_not_closed"
                elif self.collector.snapshot()["read_status"] in {"EOF", "READ_FAILED"}:
                    failure_reason = "stderr_relay_output_unavailable"
            else:
                status = "EXITED_UNEXPECTEDLY"
                failure_reason = "stderr_relay_exited_while_bridge_active"
                try:
                    self.process.wait(timeout=0)
                except BaseException:
                    pass
        elif returncode is None:
            try:
                returncode = self.process.wait(timeout=STDERR_RELAY_EOF_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                failure_reason = "stderr_relay_eof_timeout"
                try:
                    self.process.terminate()
                    returncode = self.process.wait(timeout=2.0)
                    status = "TERMINATED_AFTER_BRIDGE_EXIT"
                except BaseException:
                    status = "EXIT_UNCONFIRMED"
                    failure_reason = "stderr_relay_exit_unconfirmed"
            except BaseException:
                status = "EXIT_UNCONFIRMED"
                failure_reason = "stderr_relay_exit_unconfirmed"
            else:
                status = "REAPED" if returncode == 0 else "EXITED_UNEXPECTEDLY"
                if returncode != 0:
                    failure_reason = "stderr_relay_exited_nonzero"
        else:
            try:
                self.process.wait(timeout=0)
            except BaseException:
                pass
            status = "REAPED" if returncode == 0 else "EXITED_UNEXPECTEDLY"
            if returncode != 0:
                failure_reason = "stderr_relay_exited_nonzero"

        reader_reaped = self.collector.wait(timeout=0.25)
        output_read_status = self.collector.snapshot()["read_status"]
        if not reader_reaped and status == "REAPED":
            status = "OUTPUT_READER_INCOMPLETE"
            failure_reason = "stderr_relay_output_reader_incomplete"
        self._final_record = {
            "pid": self.process.pid,
            "status": status,
            "failure_reason": failure_reason,
            "output_read_status": output_read_status,
            "output_reader_reaped": reader_reaped,
        }
        if returncode is not None:
            self._final_record["exit_code"] = returncode
        return dict(self._final_record)


def _stderr_relay_environment() -> dict[str, str]:
    required_names = {"path", "systemroot", "windir"}
    environment = {
        name: value for name, value in os.environ.items() if name.casefold() in required_names
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


def _close_pipe_stream(stream: Any) -> bool:
    try:
        stream.close()
        return True
    except BaseException:
        try:
            os.close(stream.fileno())
            return True
        except BaseException:
            return bool(getattr(stream, "closed", False))


def _start_bridge(
    environment: dict[str, str],
) -> tuple[subprocess.Popen[bytes], _BridgeStderrRelay]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    read_fd: int | None = None
    write_fd: int | None = None
    read_stream: Any | None = None
    write_stream: Any | None = None
    try:
        read_fd, write_fd = os.pipe()
        read_stream = os.fdopen(read_fd, "rb", buffering=0)
        read_fd = None
        write_stream = os.fdopen(write_fd, "wb", buffering=0)
        write_fd = None
    except (OSError, ValueError):
        for stream in (read_stream, write_stream):
            if stream is not None:
                _close_pipe_stream(stream)
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise SmokeAbort("BLOCKED", "isolated_bridge_stderr_relay_pipe_unavailable") from None
    except BaseException:
        for stream in (read_stream, write_stream):
            if stream is not None:
                _close_pipe_stream(stream)
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise

    assert read_stream is not None and write_stream is not None

    stderr_collector = _StderrDiagnosticCollector()
    try:
        stderr_collector.start_reader()
    except BaseException:
        _close_pipe_stream(read_stream)
        _close_pipe_stream(write_stream)
        raise SmokeAbort("BLOCKED", "isolated_bridge_stderr_reader_unavailable") from None

    try:
        relay_process = subprocess.Popen(
            [sys.executable, "-I", str(_STDERR_RELAY_SCRIPT_PATH)],
            cwd=REPOSITORY_ROOT,
            env=_stderr_relay_environment(),
            stdin=read_stream,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except (OSError, ValueError):
        _close_pipe_stream(read_stream)
        _close_pipe_stream(write_stream)
        stderr_collector.finish_without_stream()
        stderr_collector.wait(timeout=0.25)
        raise SmokeAbort("BLOCKED", "isolated_bridge_stderr_relay_could_not_start") from None
    except BaseException:
        _close_pipe_stream(read_stream)
        _close_pipe_stream(write_stream)
        stderr_collector.finish_without_stream()
        stderr_collector.wait(timeout=0.25)
        raise
    finally:
        _close_pipe_stream(read_stream)

    stderr_collector.attach(relay_process.stdout)
    stderr_relay = _BridgeStderrRelay(relay_process, stderr_collector)
    if relay_process.stdout is None or not stderr_relay.wait_until_ready(
        STDERR_RELAY_START_TIMEOUT_SECONDS
    ):
        _close_pipe_stream(write_stream)
        stderr_relay.finish_after_bridge(bridge_reaped=True)
        raise SmokeAbort("BLOCKED", "isolated_bridge_stderr_relay_unavailable")

    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "codex_bridge"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=write_stream,
            creationflags=flags,
            close_fds=True,
        )
    except (OSError, ValueError):
        _close_pipe_stream(write_stream)
        stderr_relay.finish_after_bridge(bridge_reaped=True)
        raise SmokeAbort("BLOCKED", "isolated_bridge_process_could_not_start") from None
    except BaseException:
        _close_pipe_stream(write_stream)
        stderr_relay.finish_after_bridge(bridge_reaped=True)
        raise
    if not _close_pipe_stream(write_stream):
        stderr_relay.mark_parent_write_close_failed()
    return process, stderr_relay


def _http_json(
    ui_port: int,
    method: str,
    route: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    body: bytes | None = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json",
        "Origin": f"http://{BRIDGE_HOST}:{ui_port}",
        "Host": f"{BRIDGE_HOST}:{ui_port}",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = _canonical_json(payload).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(BRIDGE_HOST, ui_port, timeout=timeout)
    try:
        connection.request(method, route, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_HTTP_BODY_BYTES + 1)
        if len(raw) > MAX_HTTP_BODY_BYTES:
            raise SmokeAbort("BLOCKED", "ui_api_response_exceeded_size_limit")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return response.status, parsed
    except SmokeAbort:
        raise
    except (OSError, http.client.HTTPException, TimeoutError):
        raise SmokeAbort("BLOCKED", "ui_api_transport_outcome_unknown") from None
    finally:
        connection.close()


def _wait_for_bridge(process: subprocess.Popen[bytes], ui_port: int) -> None:
    deadline = time.monotonic() + BRIDGE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeAbort("BLOCKED", "isolated_bridge_exited_during_startup")
        try:
            status, payload = _http_json(ui_port, "GET", "/healthz", timeout=1.0)
        except SmokeAbort:
            time.sleep(0.25)
            continue
        if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
            return
        time.sleep(0.25)
    raise SmokeAbort("BLOCKED", "isolated_bridge_health_timeout")


def _preflight_bridge(
    process: subprocess.Popen[bytes],
    *,
    mcp_port: int,
    ui_port: int,
    token: str,
    expected_codex: Path,
) -> dict[str, Any]:
    if process.poll() is not None:
        raise SmokeAbort("BLOCKED", "isolated_bridge_is_not_running")
    _owned_listener_pids(process.pid, (mcp_port, ui_port))
    try:
        current_user = psutil.Process().username()
        bridge_user = psutil.Process(process.pid).username()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "windows_process_identity_could_not_be_verified") from None
    if not current_user or bridge_user != current_user:
        raise SmokeAbort("BLOCKED", "isolated_bridge_process_identity_mismatch")
    status_code, health = _http_json(ui_port, "GET", "/healthz", timeout=2.0)
    if status_code != 200 or not isinstance(health, dict) or health.get("status") != "ok":
        raise SmokeAbort("BLOCKED", "isolated_bridge_health_check_failed")
    status_code, bridge_status = _http_json(ui_port, "GET", "/ui-api/status", timeout=2.0)
    if (
        status_code != 200
        or not isinstance(bridge_status, dict)
        or bridge_status.get("bridge") != "ready"
        or bridge_status.get("app_server") != "ready"
        or bridge_status.get("ui_host") != BRIDGE_HOST
        or bridge_status.get("ui_port") != ui_port
    ):
        raise SmokeAbort("BLOCKED", "isolated_appserver_status_check_failed")
    status_code, capabilities = _http_json(
        ui_port, "GET", "/ui-api/control/capabilities", token=token
    )
    if status_code != 200 or not isinstance(capabilities, dict):
        raise SmokeAbort("BLOCKED", "control_capabilities_authentication_failed")
    status_code, _ = _http_json(ui_port, "GET", "/ui-api/control/workflows")
    if status_code != 403:
        raise SmokeAbort("BLOCKED", "missing_bearer_token_was_not_rejected")
    status_code, _ = _http_json(
        ui_port,
        "GET",
        "/ui-api/control/workflows",
        token=f"wrong-{uuid.uuid4().hex}",
    )
    if status_code != 403:
        raise SmokeAbort("BLOCKED", "incorrect_bearer_token_was_not_rejected")
    appserver_pid = _codex_appserver_pid(process.pid, expected_codex)
    return {
        "bridge_pid": process.pid,
        "appserver_pid": appserver_pid,
        "ports_owned_by_bridge": True,
        "process_identity_matches": True,
        "capabilities_authenticated": True,
        "missing_token_rejected": True,
        "incorrect_token_rejected": True,
    }


def _validate_isolated_config(
    environment: dict[str, str],
    *,
    workspace_root: Path,
    mcp_port: int,
    ui_port: int,
    codex_executable: Path,
    codex_home: Path,
) -> None:
    try:
        config = BridgeConfig.from_sources(environ=environment)
        effective = prepare_config(config)
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_validation_failed") from None
    allowed_root = os.path.normcase(str(workspace_root.resolve(strict=True)))
    actual_roots = tuple(os.path.normcase(root) for root in effective.allowed_roots)
    if (
        effective.host != BRIDGE_HOST
        or effective.port != mcp_port
        or effective.ui_port != ui_port
        or actual_roots != (allowed_root,)
        or effective.control_token != environment.get("CODEX_BRIDGE_CONTROL_TOKEN")
        or effective.lan_enabled
        or effective.github_mcp.enabled
        or len(effective.targets) != 1
        or effective.targets[0].kind != "local"
        or os.path.normcase(effective.codex_executable)
        != os.path.normcase(str(codex_executable.resolve(strict=True)))
        or (codex_home / "config.toml").exists()
    ):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_does_not_match_scope")


def _stop_bridge(
    process: subprocess.Popen[bytes] | None,
    *,
    ui_port: int,
    token: str,
) -> dict[str, Any]:
    if process is None:
        return {"reaped": True, "forced_termination": False, "descendant_count": 0}
    descendants = _process_snapshot(process.pid) if process.poll() is None else []
    shutdown_status: int | None = None
    if process.poll() is None:
        try:
            shutdown_status, _ = _http_json(
                ui_port,
                "POST",
                "/ui-api/control/shutdown",
                token=token,
                payload={},
                timeout=2.0,
            )
        except SmokeAbort:
            shutdown_status = None
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    reaped = process.poll() is not None and all(
        not _process_still_matches(pid, created_at) for pid, created_at in descendants
    )
    return {
        "reaped": reaped,
        "forced_termination": False,
        "descendant_count": len(descendants),
        "shutdown_http_status": shutdown_status,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOCAL_JSON_BYTES * 8:
        raise SmokeAbort("BLOCKED", "sqlite_artifact_unavailable_or_oversized")
    try:
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (OSError, sqlite3.Error, ValueError):
        raise SmokeAbort("BLOCKED", "sqlite_read_only_open_failed") from None


def _read_sqlite_rows(
    path: Path, query: str, parameters: tuple[object, ...] = ()
) -> list[dict[str, Any]]:
    connection = _read_only_connection(path)
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    except sqlite3.Error:
        raise SmokeAbort("BLOCKED", "sqlite_read_only_query_failed") from None
    finally:
        connection.close()


def _read_request_ledger(state_root: Path) -> list[dict[str, Any]]:
    database = state_root / "requests.sqlite"
    if not database.exists():
        return []
    return _read_sqlite_rows(
        database,
        "SELECT client_request_id, workflow_id, run_id, request_digest, state "
        "FROM workflow_requests ORDER BY created_at, workflow_id",
    )


def _run_directory(state_root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not run_id.startswith("run-") or len(run_id) != 36:
        raise SmokeAbort("BLOCKED", "workflow_run_identity_invalid")
    path = state_root / run_id
    if path.is_symlink():
        raise SmokeAbort("BLOCKED", "workflow_run_directory_is_a_symlink")
    try:
        resolved = path.resolve(strict=True)
        root = state_root.resolve(strict=True)
    except OSError:
        raise SmokeAbort("BLOCKED", "workflow_run_directory_unavailable") from None
    if not resolved.is_dir() or resolved.parent != root:
        raise SmokeAbort("BLOCKED", "workflow_run_directory_outside_isolated_state")
    return resolved


def _bounded_json_file(path: Path, *, maximum: int) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise SmokeAbort("BLOCKED", "isolated_state_file_unavailable_or_oversized")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "isolated_state_json_unreadable") from None


def _read_job_ledger(run_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    database = run_dir / "codex_jobs.sqlite"
    rows = _read_sqlite_rows(
        database,
        "SELECT job_id, workflow_id, task_identity, request_digest, request_json, state, "
        "thread_id, turn_id, remote_state, receipt_json FROM jobs ORDER BY rowid",
    )
    events = _read_sqlite_rows(
        database,
        "SELECT event_id, job_id, state FROM job_events ORDER BY event_id LIMIT 256",
    )
    return rows, events


def _read_dbos_status(run_dir: Path, workflow_id: str) -> str:
    rows = _read_sqlite_rows(
        run_dir / "dbos.sqlite",
        "SELECT workflow_uuid, status FROM workflow_status LIMIT 2",
    )
    if len(rows) != 1 or rows[0].get("workflow_uuid") != workflow_id:
        raise SmokeAbort("BLOCKED", "dbos_workflow_identity_mismatch")
    status = rows[0].get("status")
    if not isinstance(status, str):
        raise SmokeAbort("BLOCKED", "dbos_workflow_status_invalid")
    return status.upper()


def _job_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key) for key in ("job_id", "task_identity", "state", "thread_id", "turn_id")
    }


def _verify_saved_state(
    *,
    state_root: Path,
    client_request_id: str,
    workflow_id: str,
    run_id: str,
    workspace_root: Path,
    ui_port: int,
    expected_first_prompt: str,
    expected_second_prompt: str,
) -> dict[str, Any]:
    request_rows = _read_request_ledger(state_root)
    if len(request_rows) != 1:
        raise SmokeAbort("BLOCKED", "isolated_request_ledger_row_count_mismatch")
    request = request_rows[0]
    if (
        request.get("client_request_id") != client_request_id
        or request.get("workflow_id") != workflow_id
        or request.get("run_id") != run_id
        or request.get("state") not in {"running", "succeeded"}
    ):
        raise SmokeAbort("BLOCKED", "isolated_request_ledger_identity_or_state_mismatch")

    run_dir = _run_directory(state_root, run_id)
    manifest = _bounded_json_file(run_dir / "manifest.json", maximum=32 * 1024)
    definition_text = (run_dir / "workflow.yaml").read_text(encoding="utf-8")
    definition = json.loads(definition_text)
    input_data = _bounded_json_file(run_dir / "input.json", maximum=MAX_LOCAL_JSON_BYTES)
    if not isinstance(manifest, dict) or not isinstance(input_data, dict):
        raise SmokeAbort("BLOCKED", "immutable_workflow_snapshot_shape_invalid")
    expected_workspace = str(workspace_root.resolve(strict=True))
    if (
        manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("request_digest") != request.get("request_digest")
        or manifest.get("workspace_root") != expected_workspace
        or manifest.get("definition_sha256") != definition_sha256(definition_text)
        or manifest.get("input_sha256")
        != hashlib.sha256(_canonical_json(input_data).encode("utf-8")).hexdigest()
        or input_data != {}
    ):
        raise SmokeAbort("BLOCKED", "immutable_workflow_snapshot_hash_or_root_mismatch")
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": expected_workspace,
        "max_live_codex_jobs": 2,
    }
    if hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest() != request.get(
        "request_digest"
    ):
        raise SmokeAbort("BLOCKED", "workflow_request_digest_mismatch")
    control = manifest.get("control")
    if not isinstance(control, dict) or control.get("max_live_codex_jobs") != 2:
        raise SmokeAbort("BLOCKED", "manifest_two_job_limit_mismatch")

    jobs, events = _read_job_ledger(run_dir)
    expected_tasks = ("do[0].first", "do[1].second")
    if len(jobs) != 2 or tuple(row.get("task_identity") for row in jobs) != expected_tasks:
        raise SmokeAbort("BLOCKED", "codex_job_ledger_count_or_order_mismatch")
    job_ids = [row.get("job_id") for row in jobs]
    thread_ids = [row.get("thread_id") for row in jobs]
    turn_ids = [row.get("turn_id") for row in jobs]
    if (
        any(not isinstance(value, str) or not value for value in job_ids + thread_ids + turn_ids)
        or len(set(job_ids)) != 2
        or len(set(thread_ids)) != 2
        or len(set(turn_ids)) != 2
        or any(row.get("workflow_id") != workflow_id for row in jobs)
        or any(row.get("state") != "SUCCEEDED" for row in jobs)
        or any(row.get("remote_state") != "completed" for row in jobs)
    ):
        raise SmokeAbort("BLOCKED", "codex_job_terminal_identity_mismatch")

    actual_final_texts: list[str] = []
    decoded_requests: list[dict[str, Any]] = []
    for row in jobs:
        try:
            request_body = json.loads(str(row["request_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            raise SmokeAbort("BLOCKED", "codex_job_request_record_invalid") from None
        if not isinstance(request_body, dict):
            raise SmokeAbort("BLOCKED", "codex_job_request_record_invalid")
        decoded_requests.append(request_body)
        expected_job_digest = hashlib.sha256(
            _canonical_json({"host": BRIDGE_HOST, "port": ui_port, "body": request_body}).encode(
                "utf-8"
            )
        ).hexdigest()
        if row.get("request_digest") != expected_job_digest:
            raise SmokeAbort("BLOCKED", "codex_job_request_hash_mismatch")
        try:
            receipt = json.loads(str(row["receipt_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            raise SmokeAbort("BLOCKED", "codex_job_receipt_invalid") from None
        result_payload = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or not isinstance(result_payload, dict)
            or not isinstance(result_payload.get("final_text"), str)
        ):
            raise SmokeAbort("BLOCKED", "codex_job_receipt_shape_invalid")
        actual_final_texts.append(result_payload["final_text"])

    for index, request_body in enumerate(decoded_requests):
        expected_prompt = (
            expected_first_prompt if index == 0 else expected_second_prompt + actual_final_texts[0]
        )
        if (
            request_body.get("cwd") != expected_workspace
            or request_body.get("prompt") != expected_prompt
            or request_body.get("sandbox_mode") != "inherit"
            or request_body.get("confirm_full_access") is not False
        ):
            raise SmokeAbort("BLOCKED", "codex_job_workspace_or_request_contract_mismatch")

    maximum_active = maximum_active_codex_jobs(events)
    maybe_sent = [event for event in events if event.get("state") == "MAYBE_SENT"]
    if len(maybe_sent) != 2 or maximum_active != 1:
        raise SmokeAbort("BLOCKED", "codex_job_active_window_count_mismatch")
    first_success_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("job_id") == job_ids[0] and event.get("state") == "SUCCEEDED"
        ),
        -1,
    )
    second_start_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("job_id") == job_ids[1] and event.get("state") == "MAYBE_SENT"
        ),
        -1,
    )
    if first_success_index < 0 or second_start_index <= first_success_index:
        raise SmokeAbort("BLOCKED", "codex_jobs_did_not_run_sequentially")

    dbos_status = _read_dbos_status(run_dir, workflow_id)
    if dbos_status not in {"SUCCESS", "SUCCEEDED"}:
        raise SmokeAbort("FAIL", "dbos_workflow_did_not_succeed")
    worker = _bounded_json_file(run_dir / "worker-entry-status.json", maximum=4096)
    if (
        not isinstance(worker, dict)
        or worker.get("state") != "completed"
        or worker.get("exit_code") != 0
    ):
        raise SmokeAbort("FAIL", "workflow_worker_did_not_exit_successfully")
    return {
        "run_dir": run_dir,
        "manifest": manifest,
        "jobs": jobs,
        "events": events,
        "dbos_status": dbos_status,
        "worker_status": worker,
        "maximum_active": maximum_active,
        "final_texts": actual_final_texts,
    }


def _get_json(ui_port: int, route: str, token: str) -> Any:
    status, payload = _http_json(ui_port, "GET", route, token=token)
    if status != 200 or not isinstance(payload, (dict, list)):
        raise SmokeAbort("BLOCKED", "workflow_readback_failed")
    return payload


def _workflow_list_item(ui_port: int, token: str, workflow_id: str) -> dict[str, Any]:
    payload = _get_json(ui_port, "/ui-api/control/workflows?limit=50", token)
    if not isinstance(payload, dict) or not isinstance(payload.get("workflows"), list):
        raise SmokeAbort("BLOCKED", "workflow_list_shape_invalid")
    matches = [
        row
        for row in payload["workflows"]
        if isinstance(row, dict) and row.get("workflow_id") == workflow_id
    ]
    if len(matches) != 1:
        raise SmokeAbort("BLOCKED", "workflow_list_identity_mismatch")
    return matches[0]


def _workflow_detail(ui_port: int, token: str, workflow_id: str) -> dict[str, Any]:
    payload = _get_json(
        ui_port,
        f"/ui-api/control/workflows/{quote(workflow_id, safe='')}",
        token,
    )
    if not isinstance(payload, dict) or payload.get("workflow_id") != workflow_id:
        raise SmokeAbort("BLOCKED", "workflow_detail_identity_mismatch")
    return payload


def _workflow_result(ui_port: int, token: str, workflow_id: str) -> dict[str, Any]:
    payload = _get_json(
        ui_port,
        f"/ui-api/control/workflows/{quote(workflow_id, safe='')}/result",
        token,
    )
    if not isinstance(payload, dict):
        raise SmokeAbort("BLOCKED", "workflow_result_shape_invalid")
    return payload


def _wait_for_workflow_success(
    ui_port: int,
    token: str,
    workflow_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + WORKFLOW_TIMEOUT_SECONDS
    detail: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        detail = _workflow_detail(ui_port, token, workflow_id)
        status = detail.get("status")
        if status == "succeeded":
            listing = _workflow_list_item(ui_port, token, workflow_id)
            result = _workflow_result(ui_port, token, workflow_id)
            return listing, detail, result
        if status in {"failed", "failed_to_start"}:
            raise SmokeAbort("FAIL", "workflow_api_reported_failure")
        if status in {"needs_reconciliation", "unknown"}:
            raise SmokeAbort("BLOCKED", "workflow_api_requires_reconciliation")
        if status not in {"queued", "starting", "running"}:
            raise SmokeAbort("BLOCKED", "workflow_api_status_unrecognized")
        time.sleep(0.5)
    raise SmokeAbort("BLOCKED", "workflow_completion_timeout")


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
        or detail.get("max_live_codex_jobs") != 2
        or detail.get("worker_state") != "completed"
        or detail.get("terminal") is not True
        or not isinstance(jobs, list)
        or len(jobs) != 2
        or not isinstance(saved_jobs, list)
    ):
        raise SmokeAbort("BLOCKED", "workflow_api_terminal_snapshot_mismatch")
    ledger_jobs = [_job_summary(row) for row in saved_jobs]
    if jobs != ledger_jobs:
        raise SmokeAbort("BLOCKED", "workflow_api_jobs_differ_from_ledger")
    result_value = result.get("result")
    if not isinstance(result_value, dict) or result_value.get("job_id") != jobs[1].get("job_id"):
        raise SmokeAbort("BLOCKED", "workflow_result_second_job_identity_mismatch")
    result_payload = result_value.get("result")
    if not isinstance(result_payload, dict) or not isinstance(
        result_payload.get("final_text"), str
    ):
        raise SmokeAbort("BLOCKED", "workflow_result_final_text_invalid")
    if result_payload["final_text"] != "5":
        raise SmokeAbort("FAIL", "workflow_result_second_final_text_mismatch")
    return {"jobs": jobs, "final_text": result_payload["final_text"]}


def _qt_get(client: ApiClient, path: str, token: str, key: str) -> tuple[int, Any]:
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    result: dict[str, Any] = {}

    def succeeded(event_key: str, status: int, payload: object) -> None:
        if event_key == key:
            result.update(status=status, payload=payload)
            loop.quit()

    def failed(event_key: str, status: int, code: str) -> None:
        if event_key == key:
            result.update(status=status, error=code)
            loop.quit()

    client.workflow_succeeded.connect(succeeded)
    client.workflow_failed.connect(failed)
    timer.timeout.connect(loop.quit)
    if path.endswith("/result"):
        workflow_id = path.removeprefix("/ui-api/control/workflows/").removesuffix("/result")
        started = client.get_control_workflow_result(workflow_id, token, key=key)
    else:
        workflow_id = path.removeprefix("/ui-api/control/workflows/")
        started = client.get_control_workflow_detail(workflow_id, token, key=key)
    if not started:
        client.workflow_succeeded.disconnect(succeeded)
        client.workflow_failed.disconnect(failed)
        raise SmokeAbort("BLOCKED", "console_qt_get_did_not_start")
    timer.start(int(HTTP_TIMEOUT_SECONDS * 1000))
    loop.exec()
    client.workflow_succeeded.disconnect(succeeded)
    client.workflow_failed.disconnect(failed)
    timer.stop()
    if "payload" not in result:
        raise SmokeAbort("BLOCKED", "console_qt_get_did_not_complete")
    return int(result["status"]), result["payload"]


def _verify_console_qt_readback(
    *,
    ui_port: int,
    token: str,
    workflow_id: str,
    expected_detail: Mapping[str, Any],
    expected_result: Mapping[str, Any],
) -> None:
    application = QApplication.instance() or QApplication([])
    client = ApiClient(f"http://{BRIDGE_HOST}:{ui_port}")
    dialog = WorkflowManagementDialog()
    detail_path = f"/ui-api/control/workflows/{workflow_id}"
    result_path = f"/ui-api/control/workflows/{workflow_id}/result"
    detail_status, detail = _qt_get(client, detail_path, token, "phase5e2c2:detail")
    result_status, result = _qt_get(client, result_path, token, "phase5e2c2:result")
    if (
        detail_status != 200
        or result_status != 200
        or detail != expected_detail
        or result != expected_result
    ):
        raise SmokeAbort("BLOCKED", "console_qt_get_payload_mismatch")
    dialog.set_detail(detail)
    dialog.set_result(result)
    visible_text = "\n".join(
        dialog.job_list.item(index).text() for index in range(dialog.job_list.count())
    )
    if (
        dialog.jobs_group.isHidden()
        or dialog.job_list.count() != 2
        or dialog.final_text_view.toPlainText() != "5"
        or token in visible_text
        or str(expected_detail.get("workflow_id")) not in dialog.detail_labels["workflow_id"].text()
    ):
        raise SmokeAbort("BLOCKED", "console_qt_readback_render_mismatch")
    dialog.close()
    client.deleteLater()
    application.processEvents()


def _read_reconciliation_state(
    *, state_root: Path, ui_port: int, token: str, client_request_id: str
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    try:
        rows = _read_request_ledger(state_root)
    except SmokeAbort:
        pass
    matches = [row for row in rows if row.get("client_request_id") == client_request_id]
    response: dict[str, Any] = {
        "ledger_rows": len(rows),
        "matching_request_rows": len(matches),
        "matching_workflow_ids": [row.get("workflow_id") for row in matches],
        "matching_run_ids": [row.get("run_id") for row in matches],
        "matching_states": [row.get("state") for row in matches],
        "list_readable": False,
        "matching_list_workflow_ids": [],
    }
    try:
        status, payload = _http_json(
            ui_port, "GET", "/ui-api/control/workflows?limit=50", token=token, timeout=2.0
        )
        response["list_readable"] = status == 200 and isinstance(payload, dict)
        if isinstance(payload, dict) and isinstance(payload.get("workflows"), list):
            response["matching_list_workflow_ids"] = [
                row.get("workflow_id")
                for row in payload["workflows"]
                if isinstance(row, dict)
                and row.get("workflow_id") in response["matching_workflow_ids"]
            ]
    except SmokeAbort:
        pass
    return response


def _diagnostic_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _valid_diagnostic_timestamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(None)


def _validate_startup_diagnostic(
    value: object,
    *,
    workflow_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "workflow_id",
        "run_id",
        "stages",
        "failure",
    }:
        return None
    if (
        type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("workflow_id") != workflow_id
        or value.get("run_id") != run_id
    ):
        return None

    raw_stages = value.get("stages")
    raw_failure = value.get("failure")
    if (
        not isinstance(raw_stages, list)
        or not 1 <= len(raw_stages) <= MAX_STARTUP_DIAGNOSTIC_STAGE_EVENTS
        or not isinstance(raw_failure, dict)
        or set(raw_failure)
        != {
            "stage",
            "exception_class",
            "reason",
            "at_utc",
            "worker_config_write_completed",
            "worker_spawned",
        }
    ):
        return None

    stages: list[dict[str, str]] = []
    started_stages: set[str] = set()
    for raw_event in raw_stages:
        event_stage = raw_event.get("stage") if isinstance(raw_event, dict) else None
        event_phase = raw_event.get("phase") if isinstance(raw_event, dict) else None
        if (
            not isinstance(raw_event, dict)
            or set(raw_event) != {"stage", "phase", "at_utc"}
            or not isinstance(event_stage, str)
            or event_stage not in KNOWN_STARTUP_STAGES
            or not isinstance(event_phase, str)
            or event_phase not in {"started", "completed"}
            or not _valid_diagnostic_timestamp(raw_event.get("at_utc"))
        ):
            return None
        stage = event_stage
        phase = event_phase
        if phase == "started":
            started_stages.add(stage)
        stages.append({"stage": stage, "phase": phase, "at_utc": str(raw_event["at_utc"])})

    failure_stage = raw_failure.get("stage")
    exception_class = raw_failure.get("exception_class")
    reason = raw_failure.get("reason")
    worker_config_completed = raw_failure.get("worker_config_write_completed")
    worker_spawned = raw_failure.get("worker_spawned")
    if (
        not isinstance(failure_stage, str)
        or failure_stage not in KNOWN_STARTUP_STAGES
        or failure_stage not in started_stages
        or (
            exception_class is not None
            and (
                not isinstance(exception_class, str)
                or not _SAFE_EXCEPTION_CLASS.fullmatch(exception_class)
            )
        )
        or not isinstance(reason, str)
        or reason not in KNOWN_STARTUP_FAILURE_REASONS
        or not _valid_diagnostic_timestamp(raw_failure.get("at_utc"))
        or type(worker_config_completed) is not bool
        or (worker_spawned is not None and type(worker_spawned) is not bool)
    ):
        return None

    return {
        "status": "VALID",
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stages": stages,
        "failure": {
            "stage": str(failure_stage),
            "exception_class": exception_class,
            "reason": str(reason),
            "at_utc": str(raw_failure["at_utc"]),
            "worker_config_write_completed": worker_config_completed,
            "worker_spawned": worker_spawned,
        },
    }


def _read_startup_diagnostic(state_root: Path, client_request_id: str) -> dict[str, Any]:
    try:
        rows = _read_request_ledger(state_root)
    except BaseException:
        return {"status": "UNAVAILABLE", "reason": "request_ledger_unreadable"}
    matches = [row for row in rows if row.get("client_request_id") == client_request_id]
    if len(matches) != 1:
        return {"status": "IDENTITY_UNAVAILABLE", "reason": "request_identity_not_unique"}

    workflow_id = matches[0].get("workflow_id")
    run_id = matches[0].get("run_id")
    if (
        not isinstance(workflow_id, str)
        or not _WORKFLOW_ID.fullmatch(workflow_id)
        or not isinstance(run_id, str)
        or not _RUN_ID.fullmatch(run_id)
    ):
        return {"status": "IDENTITY_INVALID", "reason": "request_identity_invalid"}
    try:
        run_dir = _run_directory(state_root, run_id)
    except BaseException:
        return {
            "status": "UNAVAILABLE",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "reason": "run_directory_unavailable",
        }

    diagnostic_path = run_dir / STARTUP_DIAGNOSTIC_FILENAME
    try:
        if diagnostic_path.is_symlink():
            return {
                "status": "INVALID",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "reason": "diagnostic_file_is_a_symlink",
            }
        if not diagnostic_path.exists():
            return {"status": "NOT_FOUND", "workflow_id": workflow_id, "run_id": run_id}
        if not diagnostic_path.is_file():
            return {
                "status": "INVALID",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "reason": "diagnostic_file_not_regular",
            }
        if diagnostic_path.stat().st_size > MAX_STARTUP_DIAGNOSTIC_BYTES:
            return {
                "status": "INVALID",
                "workflow_id": workflow_id,
                "run_id": run_id,
                "reason": "diagnostic_file_oversized",
            }
        with diagnostic_path.open("rb") as stream:
            encoded = stream.read(MAX_STARTUP_DIAGNOSTIC_BYTES + 1)
    except OSError:
        return {
            "status": "READ_FAILED",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "reason": "diagnostic_file_read_failed",
        }
    if len(encoded) > MAX_STARTUP_DIAGNOSTIC_BYTES:
        return {
            "status": "INVALID",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "reason": "diagnostic_file_oversized",
        }
    try:
        document = json.loads(encoded.decode("utf-8"), object_pairs_hook=_diagnostic_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return {
            "status": "INVALID",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "reason": "diagnostic_json_invalid",
        }
    validated = _validate_startup_diagnostic(
        document,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    if validated is None:
        return {
            "status": "INVALID",
            "workflow_id": workflow_id,
            "run_id": run_id,
            "reason": "diagnostic_schema_or_identity_invalid",
        }
    return validated


def _collect_startup_diagnostics(
    *,
    state_root: Path,
    client_request_id: str,
    stderr_collector: _StderrDiagnosticCollector,
) -> dict[str, Any]:
    file_result = _read_startup_diagnostic(state_root, client_request_id)
    stderr_result = stderr_collector.snapshot()
    file_obtained = file_result.get("status") == "VALID"
    stderr_events = stderr_result.get("events")
    stderr_obtained = isinstance(stderr_events, list) and bool(stderr_events)
    read_incomplete = stderr_result.get("read_status") in {"STARTING", "READING"}
    result: dict[str, Any] = {
        "file": file_result,
        "stderr": stderr_result,
        "diagnostic_obtained": file_obtained or stderr_obtained,
        "diagnostics_unavailable": not file_obtained
        and not stderr_obtained
        and not read_incomplete,
        "stderr_capture_incomplete": read_incomplete,
    }
    if len(_canonical_json(result).encode("utf-8")) > MAX_DIAGNOSTIC_EVIDENCE_BYTES:
        return {
            "file": {"status": "EVIDENCE_LIMIT"},
            "stderr": {
                "read_status": stderr_result.get("read_status", "UNKNOWN"),
                "events": [],
                "events_truncated": True,
                "long_line_discarded": bool(stderr_result.get("long_line_discarded")),
            },
            "diagnostic_obtained": False,
            "diagnostics_unavailable": False,
            "stderr_capture_incomplete": read_incomplete,
            "evidence_truncated": True,
        }
    return result


def _no_sensitive_evidence(
    evidence: Mapping[str, Any],
    *,
    token: str,
    temp_root: Path,
    sensitive_strings: Sequence[str],
) -> None:
    encoded = json.dumps(evidence, ensure_ascii=False)
    encoded_temp_root = json.dumps(str(temp_root), ensure_ascii=False)[1:-1]
    if (
        token in encoded
        or str(temp_root) in encoded
        or encoded_temp_root in encoded
        or any(value and value in encoded for value in sensitive_strings)
    ):
        raise SmokeAbort("BLOCKED", "evidence_contains_sensitive_or_private_material")


def _set_check(evidence: dict[str, Any], name: str, status: str, **details: Any) -> None:
    evidence["checks"][name] = {"status": status, **details}


def _preflight_state_is_separate(state_root: Path, workspace_root: Path) -> None:
    protected_roots = [
        Path("D:/CodexBridge"),
        REPOSITORY_ROOT / "poc" / "ows" / ".state",
        REPOSITORY_ROOT,
    ]
    host_local = os.environ.get("LOCALAPPDATA")
    if host_local:
        protected_roots.append(Path(host_local) / "CodexBridge" / "workflows")
    if not roots_are_separate(state_root, workspace_root):
        raise SmokeAbort("BLOCKED", "isolated_state_and_workspace_overlap")
    if any(
        not roots_are_separate(state_root, protected)
        or not roots_are_separate(workspace_root, protected)
        for protected in protected_roots
    ):
        raise SmokeAbort("BLOCKED", "isolated_paths_overlap_protected_state")


def _ensure_empty_isolated_state(state_root: Path) -> None:
    if state_root.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_state_root_is_a_symlink")
    if state_root.exists():
        if not state_root.is_dir() or any(state_root.iterdir()):
            raise SmokeAbort("BLOCKED", "isolated_state_root_is_not_empty")


def _can_safely_stop_after_post(
    process: subprocess.Popen[bytes], state_root: Path, client_request_id: str
) -> bool:
    try:
        if _worker_process_count(process.pid) != 0:
            return False
        rows = _read_request_ledger(state_root)
        if not rows:
            return True
        if len(rows) != 1 or rows[0].get("client_request_id") != client_request_id:
            return False
        state = rows[0].get("state")
        if state not in {"succeeded", "failed", "failed_to_start"}:
            return False
        run_dir = _run_directory(state_root, str(rows[0].get("run_id", "")))
        jobs_path = run_dir / "codex_jobs.sqlite"
        if not jobs_path.exists():
            return state == "failed_to_start"
        jobs, _events = _read_job_ledger(run_dir)
        return all(row.get("state") in {"SUCCEEDED", "FAILED"} for row in jobs)
    except SmokeAbort:
        return False


def _bridge_run_directory_from_response(
    response: Mapping[str, Any],
) -> tuple[str, str]:
    workflow_id = response.get("workflow_id")
    run_id = response.get("run_id")
    try:
        parsed = str(uuid.UUID(workflow_id)) if isinstance(workflow_id, str) else ""
    except ValueError:
        parsed = ""
    if (
        parsed != workflow_id
        or not isinstance(run_id, str)
        or len(run_id) != 36
        or not run_id.startswith("run-")
    ):
        raise SmokeAbort("BLOCKED", "accepted_workflow_identity_invalid")
    return workflow_id, run_id


def _run_e2e(evidence: dict[str, Any]) -> int:
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise SmokeAbort("BLOCKED", "windows_native_execution_required")
    base_environ = dict(os.environ)
    first_prompt = _fixed_first_prompt()
    second_prompt = approved_second_prompt(base_environ)
    prompt_material = (first_prompt, second_prompt)
    workflow_yaml = build_two_job_workflow_yaml(second_prompt=second_prompt)
    definition = yaml.safe_load(workflow_yaml)
    validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=2)
    resolution = resolve_codex_executable(config_executable=None, environ=base_environ)
    codex_executable = Path(resolution.path).resolve(strict=True)
    if not codex_executable.is_file() or codex_executable.suffix.casefold() != ".exe":
        raise SmokeAbort("BLOCKED", "resolved_codex_is_not_a_windows_executable")

    temp_root = Path(tempfile.mkdtemp(prefix="codexbridge-phase5e2c2-"))
    temp_root = temp_root.resolve(strict=True)
    workspace_root = temp_root / "workspace"
    token = secrets.token_urlsafe(32)
    client_request_id = f"phase5e2c2-{uuid.uuid4().hex}"
    process: subprocess.Popen[bytes] | None = None
    stderr_relay: _BridgeStderrRelay | None = None
    stderr_relays: list[_BridgeStderrRelay] = []
    process_records: list[dict[str, Any]] = []
    ports: tuple[int, int] | None = None
    reservations: list[socket.socket] = []
    state_root: Path | None = None
    safe_to_stop = True
    cleanup_reaped = True
    try:
        workspace_root.mkdir()
        for directory in ("localappdata", "appdata", "tmp"):
            (temp_root / directory).mkdir()
        source_home = _source_codex_home(base_environ)
        isolated_home = temp_root / "codex_home"
        _copy_authentication(source_home, isolated_home)
        config_path = temp_root / "bridge.toml"
        if config_path.exists() or config_path.is_symlink():
            raise SmokeAbort("BLOCKED", "isolated_bridge_config_path_already_exists")
        ports, reservations = _reserve_loopback_ports()
        mcp_port, ui_port = ports
        _write_bridge_config(
            config_path,
            mcp_port=mcp_port,
            ui_port=ui_port,
            workspace_root=workspace_root,
            codex_executable=codex_executable,
        )
        child_environment = build_phase5e2c2_environment(
            base_environ,
            temp_root=temp_root,
            config_path=config_path,
            control_token=token,
            python_path=REPOSITORY_ROOT / "src",
            path_entries=_isolated_path_entries(base_environ, codex_executable),
        )
        state_root = Path(child_environment["LOCALAPPDATA"]) / "CodexBridge" / "workflows"
        _preflight_state_is_separate(state_root, workspace_root)
        _ensure_empty_isolated_state(state_root)
        if not roots_are_separate(Path("D:/CodexBridge"), temp_root):
            raise SmokeAbort("BLOCKED", "temporary_root_overlaps_production_checkout")
        _validate_isolated_config(
            child_environment,
            workspace_root=workspace_root,
            mcp_port=mcp_port,
            ui_port=ui_port,
            codex_executable=codex_executable,
            codex_home=isolated_home,
        )
        if not workspace_root.is_dir() or workspace_root.is_symlink():
            raise SmokeAbort("BLOCKED", "isolated_workspace_preflight_failed")
        _mark_pass(evidence, "native_windows", platform="windows_native")
        _mark_pass(
            evidence,
            "isolated_environment_and_roots",
            allowlist=True,
            state_and_workspace_separate=True,
        )
        evidence["ports"] = {"mcp": mcp_port, "ui": ui_port, "loopback_only": True}
        _mark_pass(
            evidence,
            "dedicated_loopback_ports",
            mcp=mcp_port,
            ui=ui_port,
            production_ports_avoided=True,
        )
        for reservation in reservations:
            reservation.close()
        reservations.clear()
        _assert_ports_free(ports)
        process, stderr_relay = _start_bridge(child_environment)
        stderr_relays.append(stderr_relay)
        _wait_for_bridge(process, ui_port)
        preflight = _preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        if _worker_process_count(process.pid) != 0:
            raise SmokeAbort("BLOCKED", "bridge_started_a_workflow_worker_before_post")
        empty_list = _get_json(ui_port, "/ui-api/control/workflows?limit=50", token)
        if not isinstance(empty_list, dict) or empty_list.get("workflows") != []:
            raise SmokeAbort("BLOCKED", "isolated_workflow_list_not_empty_before_post")
        if _read_request_ledger(state_root):
            raise SmokeAbort("BLOCKED", "isolated_request_ledger_not_empty_before_post")
        _mark_pass(
            evidence,
            "bridge_preflight_and_authentication",
            process_identity_matches=preflight["process_identity_matches"],
            ports_owned_by_bridge=preflight["ports_owned_by_bridge"],
            capabilities_authenticated=preflight["capabilities_authenticated"],
            missing_token_rejected=preflight["missing_token_rejected"],
            incorrect_token_rejected=preflight["incorrect_token_rejected"],
            appserver_ready=True,
        )
        evidence["workflow"]["workspace_preflight"] = {
            "allowed_root_isolated_workspace": True,
            "workspace_exists": True,
        }

        payload = build_two_job_payload(
            workflow_yaml=workflow_yaml,
            client_request_id=client_request_id,
            workspace_root=str(workspace_root.resolve(strict=True)),
        )
        body = _canonical_json(payload).encode("utf-8")
        evidence["live_workflow_post_attempted"] = True
        evidence["live_workflow_post_count"] = 1
        safe_to_stop = False
        try:
            post_status, post_response = _http_json(
                ui_port,
                "POST",
                "/ui-api/control/workflows",
                token=token,
                body=body,
                timeout=25.0,
            )
        except SmokeAbort:
            evidence["workflow"]["initial_post_outcome"] = "unknown"
            evidence["workflow"]["read_only_reconciliation"] = _read_reconciliation_state(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=client_request_id,
            )
            evidence["workflow"]["status"] = "NEEDS_RECONCILIATION"
            raise SmokeAbort("BLOCKED", "initial_workflow_post_outcome_unknown_no_retry") from None
        if post_status != 202 or not isinstance(post_response, dict):
            evidence["workflow"]["initial_post_http_status"] = post_status
            evidence["workflow"]["read_only_response_identity"] = (
                {
                    key: post_response.get(key)
                    for key in ("workflow_id", "run_id", "status")
                    if isinstance(post_response, dict)
                    and isinstance(post_response.get(key), (str, int, bool))
                }
                if isinstance(post_response, dict)
                else {}
            )
            evidence["workflow"]["read_only_reconciliation"] = _read_reconciliation_state(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=client_request_id,
            )
            if post_status == 409 and stderr_relays:
                evidence["workflow"]["startup_diagnostics"] = _collect_startup_diagnostics(
                    state_root=state_root,
                    client_request_id=client_request_id,
                    stderr_collector=stderr_relays[0].collector,
                )
            raise SmokeAbort("FAIL", "initial_workflow_post_was_not_accepted")
        workflow_id, run_id = _bridge_run_directory_from_response(post_response)
        evidence["workflow"].update(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "accepted_status": post_status,
                "max_live_codex_jobs": 2,
            }
        )
        _set_check(
            evidence, "single_live_workflow_post", "PASS", accepted_http_status=202, post_count=1
        )
        listing, detail, result = _wait_for_workflow_success(ui_port, token, workflow_id)
        safe_to_stop = True
        saved = _verify_saved_state(
            state_root=state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_root=workspace_root,
            ui_port=ui_port,
            expected_first_prompt=first_prompt,
            expected_second_prompt=second_prompt,
        )
        _wait_for_no_workers(process.pid)
        evidence["workflow"]["final_texts"] = saved["final_texts"]
        if saved["final_texts"][0] != "4":
            raise SmokeAbort("FAIL", "job_1_final_text_mismatch")
        api_success = _validate_api_success(
            workflow_id=workflow_id,
            run_id=run_id,
            listing=listing,
            detail=detail,
            result=result,
            saved=saved,
        )
        if api_success["final_text"] != saved["final_texts"][1]:
            raise SmokeAbort("BLOCKED", "workflow_api_result_differs_from_job_receipt")
        evidence["jobs"] = [
            {
                **_job_summary(row),
                "request_sha256": row.get("request_digest"),
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
            }
        )
        _mark_pass(
            evidence,
            "workflow_api_identity_and_jobs",
            list_detail_result_ids_match=True,
            ordered_jobs=2,
        )
        _mark_pass(
            evidence,
            "immutable_hashes_and_ledgers",
            request_rows=1,
            job_rows=2,
            dbos_status=saved["dbos_status"],
            worker_exit_code=0,
        )
        _mark_pass(evidence, "expected_final_texts", first="4", second=api_success["final_text"])
        _mark_pass(
            evidence,
            "workspace_allowed_root_and_cwd",
            allowed_root_is_workspace=True,
            verified_job_cwds=2,
        )
        _mark_pass(
            evidence,
            "active_job_concurrency",
            new_codex_jobs=2,
            new_threads=evidence["new_codex_thread_count"],
            maximum_active=saved["maximum_active"],
        )
        _mark_pass(
            evidence, "production_side_effects", count=0, basis="isolated_loopback_config_and_state"
        )
        _verify_console_qt_readback(
            ui_port=ui_port,
            token=token,
            workflow_id=workflow_id,
            expected_detail=detail,
            expected_result=result,
        )
        _mark_pass(
            evidence,
            "console_qt_get_readback",
            real_get_detail=True,
            real_get_result=True,
            new_post=False,
        )

        before_jobs, before_events = _read_job_ledger(saved["run_dir"])
        before_ids = [row["job_id"] for row in before_jobs]
        before_threads = [row["thread_id"] for row in before_jobs]
        evidence["same_id_replay_post_attempted"] = True
        evidence["same_id_replay_post_count"] = 1
        safe_to_stop = False
        try:
            replay_status, replay_response = _http_json(
                ui_port,
                "POST",
                "/ui-api/control/workflows",
                token=token,
                body=body,
                timeout=25.0,
            )
        except SmokeAbort:
            evidence["workflow"]["same_id_replay_read_only_reconciliation"] = (
                _read_reconciliation_state(
                    state_root=state_root,
                    ui_port=ui_port,
                    token=token,
                    client_request_id=client_request_id,
                )
            )
            raise SmokeAbort("BLOCKED", "same_id_replay_outcome_unknown_no_retry") from None
        if replay_status != 202 or not isinstance(replay_response, dict):
            raise SmokeAbort("BLOCKED", "same_id_replay_response_invalid")
        if (
            replay_response.get("workflow_id") != workflow_id
            or replay_response.get("run_id") != run_id
        ):
            raise SmokeAbort("BLOCKED", "same_id_replay_identity_changed")
        after_jobs, after_events = _read_job_ledger(saved["run_dir"])
        after_ids = [row["job_id"] for row in after_jobs]
        after_threads = [row["thread_id"] for row in after_jobs]
        if (
            after_ids != before_ids
            or after_threads != before_threads
            or len(after_jobs) != 2
            or len(after_events) != len(before_events)
            or _worker_process_count(process.pid) != 0
        ):
            raise SmokeAbort("BLOCKED", "same_id_replay_created_new_job_or_worker")
        safe_to_stop = True
        _mark_pass(
            evidence,
            "same_id_replay",
            same_id=True,
            exact_same_body=True,
            job_rows_unchanged=True,
            new_threads=0,
        )

        first_stop = _stop_bridge(process, ui_port=ui_port, token=token)
        process_records.append(first_stop)
        cleanup_reaped = bool(first_stop.get("reaped"))
        if stderr_relay is not None:
            stderr_relay.finish_after_bridge(bridge_reaped=cleanup_reaped)
        process = None
        stderr_relay = None
        if not cleanup_reaped:
            raise SmokeAbort("BLOCKED", "first_isolated_bridge_normal_shutdown_not_reaped")
        _assert_ports_free(ports)
        process, stderr_relay = _start_bridge(child_environment)
        stderr_relays.append(stderr_relay)
        _wait_for_bridge(process, ui_port)
        restart_preflight = _preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        if _worker_process_count(process.pid) != 0:
            raise SmokeAbort("BLOCKED", "bridge_restart_started_an_automatic_workflow_worker")
        restarted_listing, restarted_detail, restarted_result = _wait_for_workflow_success(
            ui_port, token, workflow_id
        )
        restarted_saved = _verify_saved_state(
            state_root=state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_root=workspace_root,
            ui_port=ui_port,
            expected_first_prompt=first_prompt,
            expected_second_prompt=second_prompt,
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
            [_job_summary(row) for row in restarted_saved["jobs"]]
            != [_job_summary(row) for row in saved["jobs"]]
            or restarted_saved["dbos_status"] != saved["dbos_status"]
            or restarted_detail != detail
            or restarted_result != result
        ):
            raise SmokeAbort("BLOCKED", "bridge_restart_readback_changed")
        _verify_console_qt_readback(
            ui_port=ui_port,
            token=token,
            workflow_id=workflow_id,
            expected_detail=restarted_detail,
            expected_result=restarted_result,
        )
        _mark_pass(
            evidence,
            "bridge_restart_readback",
            same_workflow_run_jobs_result=True,
            restarted_workers=0,
            no_automatic_job_start=True,
            appserver_ready=restart_preflight["appserver_pid"] > 0,
        )
        evidence["cleanup"]["first_bridge"] = first_stop
        return 0
    finally:
        for reservation in reservations:
            reservation.close()
        if process is not None:
            if not safe_to_stop and state_root is not None:
                safe_to_stop = _can_safely_stop_after_post(process, state_root, client_request_id)
            if safe_to_stop:
                try:
                    stopped = _stop_bridge(
                        process,
                        ui_port=ports[1] if ports else 0,
                        token=token,
                    )
                except SmokeAbort as error:
                    stopped = {"reaped": False, "forced_termination": False, "reason": error.code}
                process_records.append(stopped)
                cleanup_reaped = cleanup_reaped and bool(stopped.get("reaped"))
                evidence["cleanup"]["last_bridge"] = stopped
                if stderr_relay is not None:
                    stderr_relay.finish_after_bridge(bridge_reaped=bool(stopped.get("reaped")))
            else:
                cleanup_reaped = False
                evidence["cleanup"]["status"] = "PRESERVED_ACTIVE_OR_UNKNOWN"
                evidence["cleanup"]["forced_termination"] = False
                if stderr_relay is not None:
                    stderr_relay.finish_after_bridge(bridge_reaped=False)
        if process_records:
            evidence["cleanup"]["bridge_processes"] = process_records
        evidence["cleanup"]["forced_termination"] = False
        evidence["cleanup"]["owned_children_reaped"] = cleanup_reaped
        for relay in stderr_relays:
            relay.collector.wait(timeout=0.25)
        if stderr_relays:
            evidence["cleanup"]["stderr_relay_processes"] = [
                relay.snapshot() for relay in stderr_relays
            ]
        startup_diagnostics = evidence.get("workflow", {}).get("startup_diagnostics")
        if isinstance(startup_diagnostics, dict) and stderr_relays:
            stderr_result = stderr_relays[0].collector.snapshot()
            startup_diagnostics["stderr"] = stderr_result
            startup_diagnostics["stderr_relay"] = stderr_relays[0].snapshot()
            file_result = startup_diagnostics.get("file")
            file_obtained = isinstance(file_result, dict) and file_result.get("status") == "VALID"
            stderr_events = stderr_result.get("events")
            stderr_obtained = isinstance(stderr_events, list) and bool(stderr_events)
            read_incomplete = stderr_result.get("read_status") in {"STARTING", "READING"}
            startup_diagnostics["diagnostic_obtained"] = file_obtained or stderr_obtained
            startup_diagnostics["diagnostics_unavailable"] = (
                not file_obtained and not stderr_obtained and not read_incomplete
            )
            startup_diagnostics["stderr_capture_incomplete"] = read_incomplete
        if cleanup_reaped:
            try:
                temporary_parent = Path(tempfile.gettempdir()).resolve(strict=True)
                owned_temp_root = (
                    temp_root.parent == temporary_parent
                    and temp_root.name.startswith("codexbridge-phase5e2c2-")
                    and not temp_root.is_symlink()
                )
                if not owned_temp_root:
                    evidence["cleanup"]["isolated_temp_state_removed"] = False
                else:
                    for attempt in range(5):
                        try:
                            shutil.rmtree(temp_root)
                            evidence["cleanup"]["isolated_temp_state_removed"] = True
                            break
                        except OSError:
                            if attempt == 4:
                                evidence["cleanup"]["isolated_temp_state_removed"] = False
                            else:
                                time.sleep(0.5)
            except OSError:
                evidence["cleanup"]["isolated_temp_state_removed"] = False
        _no_sensitive_evidence(
            evidence,
            token=token,
            temp_root=temp_root,
            sensitive_strings=prompt_material,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Phase 5E-2C2 E2E once")
    parser.add_argument(
        "--retest-once",
        action="store_true",
        help="claim the dedicated retest evidence path after validating the original evidence",
    )
    parser.add_argument(
        "--diagnostic-retest-once",
        action="store_true",
        help="claim the dedicated diagnostic retest evidence path after validating both baselines",
    )
    parser.add_argument(
        "--r4-once",
        action="store_true",
        help="claim the dedicated R4 evidence path after validating the original, R2, and R3 evidence",
    )
    arguments = parser.parse_args(argv)
    if sum((arguments.retest_once, arguments.diagnostic_retest_once, arguments.r4_once)) > 1:
        parser.error("--retest-once, --diagnostic-retest-once, and --r4-once cannot be combined")
    previous_evidence_sha256: str | None = None
    diagnostic_retest_sha256: dict[str, str] | None = None
    r4_baseline_sha256: dict[str, str] | None = None
    try:
        if arguments.r4_once:
            evidence_path, r4_baseline_sha256 = _select_r4_evidence()
            _fixed_first_prompt()
            approved_second_prompt(os.environ)
        elif arguments.diagnostic_retest_once:
            evidence_path, diagnostic_retest_sha256 = _select_diagnostic_retest_evidence()
        else:
            evidence_path, previous_evidence_sha256 = _select_evidence_paths(
                retest_once=arguments.retest_once
            )
    except SmokeAbort as error:
        print(json.dumps({"status": error.status, "reason": error.code}, ensure_ascii=False))
        return 2

    evidence = _new_evidence(
        previous_evidence_sha256=previous_evidence_sha256,
        diagnostic_retest_sha256=diagnostic_retest_sha256,
        r4_baseline_sha256=r4_baseline_sha256,
    )
    try:
        claim_evidence_path(evidence_path, evidence)
    except FileExistsError:
        if arguments.r4_once:
            reason = "r4_evidence_already_exists"
        elif arguments.diagnostic_retest_once:
            reason = "diagnostic_retest_evidence_already_exists"
        else:
            reason = (
                "retest_evidence_already_exists"
                if arguments.retest_once
                else "evidence_already_exists"
            )
        print(json.dumps({"status": "BLOCKED", "reason": reason}, ensure_ascii=False))
        return 2
    except OSError:
        print(json.dumps({"status": "BLOCKED", "reason": "evidence_path_unavailable"}))
        return 2

    status = "PASS"
    reason: str | None = None
    code = 0
    try:
        code = _run_e2e(evidence)
    except SmokeAbort as error:
        status = error.status
        reason = error.code
        code = 2 if status == "BLOCKED" else 1
    except Exception as error:
        status = "BLOCKED"
        reason = f"unexpected_{type(error).__name__}"
        code = 2
    finally:
        evidence["finished_at_utc"] = _utc_now()
        if reason is not None:
            evidence["reason"] = reason
        if reason in {
            "job_1_final_text_mismatch",
            "workflow_result_second_final_text_mismatch",
        }:
            _set_check(
                evidence,
                "expected_final_texts",
                "FAIL",
                observed=evidence.get("workflow", {}).get("final_texts"),
            )
        if evidence["checks"].get("production_side_effects", {}).get("status") == "NOT_RUN":
            _set_check(
                evidence,
                "production_side_effects",
                "PASS",
                count=0,
                basis="production_process_and_state_not_used",
            )
        evidence["status"] = status
        evidence["exit_code"] = code
        if (
            evidence["live_workflow_post_attempted"]
            and evidence["checks"]["single_live_workflow_post"]["status"] == "NOT_RUN"
        ):
            _set_check(
                evidence,
                "single_live_workflow_post",
                status,
                post_count=1,
                accepted_http_status=evidence.get("workflow", {}).get("initial_post_http_status"),
            )
        if (
            evidence["same_id_replay_post_attempted"]
            and evidence["checks"]["same_id_replay"]["status"] == "NOT_RUN"
        ):
            _set_check(evidence, "same_id_replay", status, replay_count=1)
        _save_evidence(evidence, evidence_path)
    print(
        json.dumps(
            {
                "phase": evidence["phase"],
                "status": evidence["status"],
                "reason": evidence.get("reason"),
            },
            ensure_ascii=False,
        )
    )
    return int(evidence["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
