from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import importlib.metadata
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2 as httpx
import psutil
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PySide6.QtWidgets import QApplication

from codex_bridge.codex_resolver import resolve_codex_executable
from codex_bridge.config import BridgeConfig
from codex_bridge.server import prepare_config
from codex_bridge.workflows.interpreter import OwsError, validate_workflow
from codex_bridge.workflows.manager import missing_workflow_dependencies
from scripts import phase5e2c2_isolated_smoke as isolated
from scripts import phase5f5_integrated_isolated_smoke as phase5f5
from scripts.phase5e_smoke_support import ports_are_safe, roots_are_separate

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PARENT_HEAD = "ce05ba54d9d47f9160a9b1a3f571f0239cad4983"
BRIDGE_HOST = "127.0.0.1"
EXPECTED_PROMPT = "Reply with exactly B5_VERIFY_OK"
EXPECTED_RESULT = "B5_VERIFY_OK"
MCP_TOOL_NAMES = frozenset(
    {
        "workflow_runs_list",
        "workflow_run_get",
        "workflow_run_result",
        "workflow_definitions_list",
        "workflow_definition_get",
        "workflow_graph_get",
        "workflow_definition_validate",
        "workflow_definition_create",
        "workflow_definition_update",
        "workflow_run_start",
    }
)
LEGACY_CODEX_TOOL_NAMES = frozenset(
    {"codex_threads", "codex_status", "codex_start", "codex_continue"}
)
MAX_WORKFLOW_WAIT_SECONDS = 600.0
MAX_STATUS_OBSERVATION_TRANSITIONS = 64
MAX_OFFLINE_COMMAND_SECONDS = 900.0
MAX_RESPONSE_BYTES = 1024 * 1024
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\"']+")
_WORKFLOW_STATUS_VALUES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "needs_reconciliation",
        "succeeded",
        "failed",
        "failed_to_start",
    }
)
SmokeAbort = phase5f5.SmokeAbort


@dataclass(slots=True)
class PostBudget:
    """Enforce one initial HTTP start and one exact same-reference MCP replay."""

    initial_calls: int = 0
    same_id_replay_calls: int = 0
    initial_accepted: bool = False
    initial_request: dict[str, Any] | None = None

    def claim_initial(self, request: Mapping[str, Any]) -> None:
        if self.initial_calls != 0:
            raise SmokeAbort("BLOCKED", "initial_workflow_post_budget_exhausted")
        self.initial_calls = 1
        self.initial_request = json.loads(_canonical_json(dict(request)))

    def mark_initial_accepted(self) -> None:
        if self.initial_calls != 1:
            raise SmokeAbort("BLOCKED", "initial_workflow_post_was_not_claimed")
        self.initial_accepted = True

    def claim_same_id_replay(self, request: Mapping[str, Any]) -> None:
        if self.same_id_replay_calls != 0:
            raise SmokeAbort("BLOCKED", "same_id_mcp_replay_budget_exhausted")
        if not self.initial_accepted or self.initial_request is None:
            raise SmokeAbort("BLOCKED", "same_id_replay_requires_accepted_initial_run")
        if _canonical_json(dict(request)) != _canonical_json(self.initial_request):
            raise SmokeAbort("BLOCKED", "same_id_replay_reference_changed")
        self.same_id_replay_calls = 1


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds")


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


def source_sha256(source: str) -> str:
    return _sha256(source.encode("utf-8", errors="strict"))


def canonical_sha256(source: str) -> str:
    definition = yaml.safe_load(source)
    return _sha256((_canonical_json(definition) + "\n").encode("utf-8"))


def build_fixed_workflow(source_yaml: str) -> str:
    try:
        definition = yaml.safe_load(source_yaml)
    except yaml.YAMLError:
        raise SmokeAbort("BLOCKED", "approved_b5_workflow_fixture_invalid") from None
    if not isinstance(definition, dict):
        raise SmokeAbort("BLOCKED", "approved_b5_workflow_fixture_invalid")
    tasks = definition.get("do")
    if (
        not isinstance(tasks, list)
        or len(tasks) != 2
        or not isinstance(tasks[0], dict)
        or not isinstance(tasks[0].get("calculate"), dict)
        or not isinstance(tasks[0]["calculate"].get("with"), dict)
        or tasks[0]["calculate"].get("call") != "codex-job"
    ):
        raise SmokeAbort("BLOCKED", "approved_b5_workflow_fixture_shape_invalid")
    tasks[0]["calculate"]["with"]["prompt"] = EXPECTED_PROMPT
    try:
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=1)
    except (OwsError, TypeError, ValueError, RecursionError):
        raise SmokeAbort("BLOCKED", "approved_b5_workflow_contract_invalid") from None
    return yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)


def add_comment_revision(source_yaml: str) -> str:
    suffix = "# B5 comment-only revision 2\n"
    if suffix.strip() in source_yaml:
        raise ValueError("revision comment already exists")
    return source_yaml.rstrip("\r\n") + "\n" + suffix


def add_mutation_conflict_comment(source_yaml: str) -> str:
    suffix = "# B5 mutation conflict payload\n"
    if suffix.strip() in source_yaml:
        raise ValueError("conflict comment already exists")
    return source_yaml.rstrip("\r\n") + "\n" + suffix


def evidence_path_for_attempt(root: Path, attempt_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", attempt_id, re.ASCII) is None:
        raise ValueError("attempt id must be a UUID hex value")
    return root / "docs" / f"phase_b5_workflow_acceptance-{attempt_id}.json"


def assert_safe_evidence(
    value: Mapping[str, Any], *, secrets_to_reject: Sequence[str] = ()
) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for secret in secrets_to_reject:
        if secret and secret in encoded:
            raise SmokeAbort("BLOCKED", "evidence_contains_secret")
    for forbidden in (EXPECTED_PROMPT, "Authorization:", '"final_text"'):
        if forbidden.casefold() in encoded.casefold():
            raise SmokeAbort("BLOCKED", "evidence_contains_prompt_or_raw_result")
    if _WINDOWS_ABSOLUTE_PATH.search(encoded):
        raise SmokeAbort("BLOCKED", "evidence_contains_absolute_windows_path")


def parse_pytest_summary(output: str) -> dict[str, int] | None:
    summaries = [
        line for line in output.splitlines() if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line)
    ]
    for line in reversed(summaries):
        counts = {key: 0 for key in ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")}
        for count, kind in re.findall(
            r"\b(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed)\b", line
        ):
            normalized = "errors" if kind in {"error", "errors"} else kind
            counts[normalized] += int(count)
        if sum(counts.values()):
            return counts
    return None


def send_initial_once(
    budget: PostBudget,
    request: Mapping[str, Any],
    transport: Callable[[Mapping[str, Any]], tuple[int, Any]],
    *,
    before_send: Callable[[], None] | None = None,
) -> tuple[int, Any]:
    budget.claim_initial(request)
    if before_send is not None:
        before_send()
    try:
        status, payload = transport(request)
    except Exception:
        raise SmokeAbort("BLOCKED", "initial_workflow_post_outcome_unknown") from None
    if status != 202 or not isinstance(payload, dict):
        raise SmokeAbort("FAIL", "initial_workflow_post_not_accepted")
    budget.mark_initial_accepted()
    return status, payload


def stop_owned_bridge(
    process: subprocess.Popen[bytes],
    identity: Mapping[str, Any],
    *,
    ui_port: int,
    control_token: str,
) -> dict[str, Any]:
    phase5f5._assert_bridge_identity(process, identity)
    descendants = isolated._process_snapshot(process.pid) if process.poll() is None else []
    result = isolated._stop_bridge(process, ui_port=ui_port, token=control_token)
    remaining = sum(
        isolated._process_still_matches(pid, created_at) for pid, created_at in descendants
    )
    if (
        not result.get("reaped")
        or result.get("forced_termination") is not False
        or result.get("shutdown_http_status") != 202
        or remaining != 0
    ):
        raise SmokeAbort("FAIL", "owned_bridge_graceful_shutdown_incomplete")
    return {
        "reaped": True,
        "forced_termination": False,
        "descendant_count_before_shutdown": len(descendants),
        "orphan_processes": remaining,
    }


def _new_evidence(attempt_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": "B5",
        "attempt_id": attempt_id,
        "status": "NOT_RUN",
        "reason": None,
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
        "parent_head": PARENT_HEAD,
        "source_head": None,
        "runner_sha256": _sha256(Path(__file__).read_bytes()),
        "runtime": {},
        "offline_gate": {"status": "NOT_RUN", "preflight": {}, "tests": {}, "static": {}},
        "mcp_control_gate": {"status": "NOT_RUN", "checks": {}},
        "saved_execution_gate": {
            "status": "NOT_RUN",
            "posts": {
                "initial_http_workflow_posts": 0,
                "unique_initial_client_request_ids": 0,
                "initial_post_outcome": "NOT_RUN",
                "same_id_mcp_replay_calls": 0,
                "same_id_mcp_replay_outcome": "NOT_RUN",
                "same_id_conflict_http_posts": 0,
                "new_client_request_ids_after_initial": 0,
                "actual_codex_jobs": 0,
                "actual_codex_job_observation": "NOT_OBSERVED",
            },
        },
        "restart_gate": {"status": "NOT_RUN", "checks": {}},
        "processes": {"initial_bridge": "NOT_RUN", "restart_bridge": "NOT_RUN"},
    }


def _safe_write(
    writer: phase5f5._EvidenceWriter,
    evidence: dict[str, Any],
    *,
    secrets_to_reject: Sequence[str] = (),
) -> None:
    assert_safe_evidence(evidence, secrets_to_reject=secrets_to_reject)
    writer.write(evidence)


def _run_command(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SmokeAbort("BLOCKED", "offline_command_timed_out") from None
    except OSError:
        raise SmokeAbort("BLOCKED", "offline_command_unavailable") from None


def _git_head() -> str:
    result = _run_command(["git", "rev-parse", "HEAD"], timeout=10.0)
    head = result.stdout.strip().lower()
    if result.returncode or re.fullmatch(r"[0-9a-f]{40}", head, re.ASCII) is None:
        raise SmokeAbort("BLOCKED", "git_head_read_failed")
    ancestor = _run_command(["git", "merge-base", "--is-ancestor", PARENT_HEAD, head], timeout=10.0)
    if ancestor.returncode != 0:
        raise SmokeAbort("BLOCKED", "expected_parent_head_not_in_current_history")
    if head != PARENT_HEAD:
        raise SmokeAbort("BLOCKED", "runner_source_head_is_not_expected_parent")
    return head


def _runtime_preflight() -> tuple[dict[str, Any], Path | None, Path | None]:
    runtime: dict[str, Any] = {
        "platform": "windows_native" if os.name == "nt" else "non_windows",
        "python_version": sys.version.split()[0],
    }
    if os.name != "nt" or not sys.platform.startswith("win"):
        return runtime, None, None
    repository_venv = (REPOSITORY_ROOT / ".venv").resolve(strict=True)
    python_path = Path(sys.executable).resolve(strict=True)
    if (
        not python_path.is_relative_to(repository_venv)
        or Path(sys.prefix).resolve() != repository_venv
    ):
        raise SmokeAbort("BLOCKED", "repository_python_environment_required")
    if sys.version_info[:2] != (3, 14):
        raise SmokeAbort("BLOCKED", "python_314_required")
    runtime["repository_venv"] = True
    packages: dict[str, str] = {}
    expected_versions = {"dbos": "3.4.0", "jq": "1.13.0", "jsonschema": "4.25.1", "PyYAML": "6.0.3"}
    for package, expected in expected_versions.items():
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise SmokeAbort("BLOCKED", "workflow_dependency_unavailable") from None
        if version != expected:
            raise SmokeAbort("BLOCKED", "workflow_dependency_version_mismatch")
        packages[package] = version
    for package in ("PySide6", "psutil", "mcp", "httpx2"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise SmokeAbort("BLOCKED", "console_or_mcp_dependency_unavailable") from None
    try:
        codex = Path(
            resolve_codex_executable(config_executable=None, environ=os.environ).path
        ).resolve(strict=True)
    except (OSError, ValueError):
        runtime["active_codex_cli"] = "BLOCKED"
        runtime["authentication_copy_source"] = "NOT_CHECKED"
        runtime["packages"] = packages
        return runtime, None, None
    if not codex.is_file() or codex.suffix.casefold() != ".exe":
        runtime["active_codex_cli"] = "BLOCKED"
        runtime["authentication_copy_source"] = "NOT_CHECKED"
        runtime["packages"] = packages
        return runtime, None, None
    version_probe = _run_command([str(codex), "--version"], timeout=15.0)
    if version_probe.returncode != 0:
        runtime["active_codex_cli"] = "BLOCKED"
        runtime["authentication_copy_source"] = "NOT_CHECKED"
        runtime["packages"] = packages
        return runtime, None, None
    source_home = isolated._source_codex_home(os.environ)
    source_auth = source_home / "auth.json"
    try:
        auth_available = not source_auth.is_symlink() and source_auth.is_file()
        auth_available = auth_available and source_auth.stat().st_size <= isolated.MAX_AUTH_BYTES
    except OSError:
        auth_available = False
    runtime.update(
        {
            "repository_venv": True,
            "workflow_and_console_dependencies": packages,
            "ows_dsl_version": "1.0.3",
            "active_codex_cli": "PASS",
            "codex_authentication_source": "AVAILABLE" if auth_available else "BLOCKED",
        }
    )
    if not auth_available:
        return runtime, None, None
    return runtime, codex, source_home


TEST_GROUPS: dict[str, tuple[str, ...]] = {
    "workflow_api_mcp_saved_definitions": (
        "tests/test_workflow_mcp.py",
        "tests/test_workflow_definition_api.py",
        "tests/test_workflow_definition_mcp.py",
        "tests/test_workflow_saved_execution.py",
        "tests/test_workflow_saved_api.py",
        "tests/test_workflow_saved_mcp.py",
        "tests/test_workflow_definitions.py",
        "tests/test_workflow_graph.py",
    ),
    "service_api_multijob_manager": (
        "tests/test_workflows_service.py",
        "tests/test_workflows_api.py",
        "tests/test_workflows_multi_api.py",
        "tests/test_workflows_multijob.py",
        "tests/test_workflows_manager.py",
        "tests/test_workflows_http_client.py",
        "tests/test_workflows_entry.py",
        "tests/test_workflows_codex_job.py",
        "tests/test_workflows_package.py",
        "tests/test_workflows_startup_diagnostics.py",
        "tests/test_workflows_diagnostic_log.py",
    ),
    "console_smoke_and_b5_runner": (
        "tests/test_console_workflow_controller.py",
        "tests/test_console_workflow_client.py",
        "tests/test_console_workflows_dialog.py",
        "tests/test_phase5f5_integrated_acceptance.py",
        "tests/test_phase5e_isolated_smoke.py",
        "tests/test_phase5e2c2_isolated_smoke.py",
        "tests/test_phase5e2c2_stderr_relay.py",
        "tests/test_phase5f1b_three_job_smoke.py",
        "tests/test_phase5f1d_restart_readback.py",
        "tests/test_lan_mcp.py",
        "tests/test_phase_b5_workflow_isolated_smoke.py",
    ),
    "ows_regression": (
        "poc/ows/tests/test_interpreter.py",
        "poc/ows/tests/test_dbos.py",
        "poc/ows/tests/test_job_adapter.py",
        "poc/ows/tests/test_codex_job.py",
    ),
}


def _run_offline_gate(
    evidence: dict[str, Any], writer: phase5f5._EvidenceWriter
) -> tuple[Path | None, Path | None]:
    gate = evidence["offline_gate"]
    preflight: dict[str, Any] = {}
    codex_path: Path | None = None
    source_home: Path | None = None
    try:
        head = _git_head()
        evidence["source_head"] = head
        runtime, codex_path, source_home = _runtime_preflight()
        evidence["runtime"] = runtime
        preflight["parent_head_matches"] = "PASS"
        preflight["windows_python_and_dependencies"] = (
            "PASS"
            if runtime.get("repository_venv") and runtime.get("workflow_and_console_dependencies")
            else "BLOCKED"
        )
        preflight["active_codex_cli"] = runtime.get("active_codex_cli", "NOT_RUN")
        preflight["authentication_source"] = runtime.get("codex_authentication_source", "NOT_RUN")
        preflight["production_default_ports"] = "DEFERRED_TO_ISOLATION_PORT_RESERVATION"
    except SmokeAbort as error:
        preflight["preflight_error"] = error.code
    gate["preflight"] = preflight
    _safe_write(writer, evidence)

    tests: dict[str, Any] = {}
    totals = {key: 0 for key in ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")}
    tests_failed = False
    for group, targets in TEST_GROUPS.items():
        command = [
            "uv",
            "run",
            "--extra",
            "dev",
            "--extra",
            "workflows",
            "--extra",
            "console",
            "pytest",
            "-q",
            *targets,
        ]
        result = _run_command(command, timeout=MAX_OFFLINE_COMMAND_SECONDS)
        counts = parse_pytest_summary(result.stdout + "\n" + result.stderr)
        tests[group] = {
            "exit_code": result.returncode,
            "counts": counts,
            "status": "PASS"
            if result.returncode == 0
            and counts is not None
            and not counts["failed"]
            and not counts["errors"]
            else "FAIL",
        }
        if counts is not None:
            for key, value in counts.items():
                totals[key] += value
        gate["tests"] = {"groups": tests, "totals": totals}
        _safe_write(writer, evidence)
        if tests[group]["status"] != "PASS":
            tests_failed = True

    static_commands: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("ruff_check", ("uv", "run", "--extra", "dev", "ruff", "check", "src", "scripts", "tests")),
        (
            "ruff_format",
            ("uv", "run", "--extra", "dev", "ruff", "format", "--check", "src", "scripts", "tests"),
        ),
        ("mypy", ("uv", "run", "--extra", "dev", "mypy", "src")),
        (
            "compileall",
            (
                "uv",
                "run",
                "--extra",
                "dev",
                "python",
                "-m",
                "compileall",
                "-q",
                "src/codex_bridge",
                "scripts",
            ),
        ),
        ("uv_lock_check", ("uv", "lock", "--check")),
        ("git_diff_check", ("git", "diff", "--check")),
        ("git_diff_cached_check", ("git", "diff", "--cached", "--check")),
    )
    static_failed = False
    for name, command in static_commands:
        result = _run_command(command, timeout=MAX_OFFLINE_COMMAND_SECONDS)
        gate["static"][name] = {
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "exit_code": result.returncode,
        }
        _safe_write(writer, evidence)
        if result.returncode != 0:
            static_failed = True

    preflight_blockers = [name for name, status in preflight.items() if status == "BLOCKED"]
    if "preflight_error" in preflight:
        preflight_blockers.append(str(preflight["preflight_error"]))
    if tests_failed:
        gate["status"] = "FAIL"
        gate["reason"] = "targeted_pytest_failed"
    elif static_failed:
        gate["status"] = "FAIL"
        gate["reason"] = "static_check_failed"
    elif preflight_blockers:
        gate["status"] = "BLOCKED"
        gate["reason"] = "runtime_preflight_unavailable"
    else:
        gate["status"] = "PASS"
    return codex_path, source_home


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_bridge_config(
    path: Path,
    *,
    mcp_port: int,
    ui_port: int,
    workspace: Path,
    codex_executable: Path,
) -> None:
    allowed_hosts = [f"127.0.0.1:{mcp_port}", f"127.0.0.1:{ui_port}"]
    allowed_origins = [f"http://127.0.0.1:{mcp_port}", f"http://127.0.0.1:{ui_port}"]
    contents = "\n".join(
        (
            "[bridge]",
            f"host = {_toml_string(BRIDGE_HOST)}",
            f"port = {mcp_port}",
            f"allowed_roots = [{_toml_string(str(workspace.resolve(strict=True)))}]",
            f"allowed_hosts = [{', '.join(_toml_string(value) for value in allowed_hosts)}]",
            f"allowed_origins = [{', '.join(_toml_string(value) for value in allowed_origins)}]",
            "lan_enabled = false",
            f"codex_executable = {_toml_string(str(codex_executable.resolve(strict=True)))}",
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
            "[workflow]",
            'mcp_permissions = ["read", "write", "execute"]',
            "",
            "[targets.local]",
            'name = "B5 isolated local execution"',
            'kind = "local"',
            "",
        )
    )
    if path.exists() or path.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_config_path_already_exists")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
    except OSError:
        raise SmokeAbort("BLOCKED", "isolated_config_write_failed") from None


@dataclass(slots=True)
class IsolatedContext:
    temp_root: Path
    state_root: Path
    workspace: Path
    codex_executable: Path
    environment: dict[str, str]
    control_token: str
    mcp_token: str
    ports: tuple[int, int]
    process: subprocess.Popen[bytes] | None = None
    identity: dict[str, Any] | None = None
    app_server_pid: int | None = None
    app_server_identity: dict[str, Any] | None = None
    active_workflow_request: dict[str, Any] | None = None
    active_worker_observer: _OwnedWorkerObserver | None = None


def _build_runtime_environment(
    *,
    temp_root: Path,
    workspace: Path,
    codex_executable: Path,
    source_home: Path,
    control_token: str,
    mcp_token: str,
    ports: tuple[int, int],
    config_name: str,
) -> tuple[dict[str, str], Path, Path]:
    config_path = temp_root / config_name
    _write_bridge_config(
        config_path,
        mcp_port=ports[0],
        ui_port=ports[1],
        workspace=workspace,
        codex_executable=codex_executable,
    )
    path_entries = phase5f5.support._isolated_path_entries(os.environ, codex_executable)
    environment = isolated.build_phase5e2c2_environment(
        os.environ,
        temp_root=temp_root,
        config_path=config_path,
        control_token=control_token,
        python_path=REPOSITORY_ROOT / "src",
        path_entries=path_entries,
    )
    environment["CODEX_BRIDGE_MCP_TOKEN"] = mcp_token
    isolated_home = Path(environment["CODEX_HOME"])
    if not (isolated_home / "auth.json").is_file():
        isolated._copy_authentication(source_home, isolated_home)
    try:
        effective = prepare_config(BridgeConfig.from_sources(environ=environment))
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_validation_failed") from None
    expected_roots = (os.path.normcase(str(workspace.resolve(strict=True))),)
    actual_roots = tuple(os.path.normcase(root) for root in effective.allowed_roots)
    expected_mcp = os.path.normcase(str(codex_executable.resolve(strict=True)))
    if (
        effective.host != BRIDGE_HOST
        or effective.port != ports[0]
        or effective.ui_port != ports[1]
        or actual_roots != expected_roots
        or effective.control_token != control_token
        or effective.mcp_token != mcp_token
        or effective.workflow_mcp_permissions != ("read", "write", "execute")
        or effective.lan_enabled
        or effective.github_mcp.enabled
        or len(effective.targets) != 1
        or effective.targets[0].kind != "local"
        or os.path.normcase(effective.codex_executable) != expected_mcp
    ):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_does_not_match_scope")
    _environment, config_path, state_root = phase5f5._runtime_paths_from_config_result(
        (environment, config_path), temp_root=temp_root
    )
    return environment, config_path, state_root


def _make_isolated_context(codex_executable: Path, source_home: Path) -> IsolatedContext:
    temp_root = Path(tempfile.mkdtemp(prefix="codexbridge-phase-b5-")).resolve(strict=True)
    for name in ("localappdata", "appdata", "tmp"):
        (temp_root / name).mkdir()
    workspace = temp_root / "workspace"
    workspace.mkdir()
    isolated_home = temp_root / "codex_home"
    isolated._copy_authentication(source_home, isolated_home)
    source_auth = source_home / "auth.json"
    isolated_auth = isolated_home / "auth.json"
    if _sha256(source_auth.read_bytes()) != _sha256(isolated_auth.read_bytes()):
        raise SmokeAbort("BLOCKED", "isolated_authentication_copy_mismatch")
    token = secrets.token_urlsafe(32)
    mcp_token = secrets.token_urlsafe(32)
    if token == mcp_token:
        raise SmokeAbort("BLOCKED", "isolated_control_and_mcp_tokens_not_distinct")
    ports, reservations = isolated._reserve_loopback_ports()
    try:
        if not ports_are_safe(*ports):
            raise SmokeAbort("BLOCKED", "isolated_ports_overlap_production_defaults")
    finally:
        for reservation in reservations:
            reservation.close()
    isolated._assert_ports_free(ports)
    if not ports_are_safe(*ports):
        raise SmokeAbort("BLOCKED", "isolated_ports_overlap_production")
    environment, _config, state_root = _build_runtime_environment(
        temp_root=temp_root,
        workspace=workspace,
        codex_executable=codex_executable,
        source_home=source_home,
        control_token=token,
        mcp_token=mcp_token,
        ports=ports,
        config_name="bridge-initial.toml",
    )
    protected = (REPOSITORY_ROOT, Path("D:/CodexBridge-PoC"), Path("D:/CodexBridge-Workflow409"))
    if (
        temp_root.is_symlink()
        or workspace.is_symlink()
        or not workspace.is_dir()
        or any(workspace.iterdir())
        or not roots_are_separate(REPOSITORY_ROOT, temp_root)
        or not roots_are_separate(state_root, workspace)
        or any(not roots_are_separate(workspace, root) for root in protected)
        or (state_root / "definitions.sqlite").exists()
        or (state_root / "requests.sqlite").exists()
    ):
        raise SmokeAbort("BLOCKED", "isolated_root_or_initial_state_preflight_failed")
    return IsolatedContext(
        temp_root=temp_root,
        state_root=state_root,
        workspace=workspace,
        codex_executable=codex_executable,
        environment=environment,
        control_token=token,
        mcp_token=mcp_token,
        ports=ports,
    )


def _start_bridge(context: IsolatedContext, *, restart: bool = False) -> dict[str, Any]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    if context.process is not None and context.process.poll() is None:
        raise SmokeAbort("BLOCKED", "owned_bridge_already_running")
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "codex_bridge"],
            cwd=REPOSITORY_ROOT,
            env=context.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_process_could_not_start") from None
    context.process = process
    identity = phase5f5._capture_bridge_identity(process)
    context.identity = identity
    isolated._wait_for_bridge(process, context.ports[1])
    isolated._owned_listener_pids(process.pid, context.ports)
    preflight = isolated._preflight_bridge(
        process,
        mcp_port=context.ports[0],
        ui_port=context.ports[1],
        token=context.control_token,
        expected_codex=context.codex_executable,
    )
    app_pid = preflight.get("appserver_pid")
    if type(app_pid) is not int:
        raise SmokeAbort("BLOCKED", "isolated_app_server_pid_unavailable")
    context.app_server_identity = phase5f5._capture_owned_process(
        psutil.Process(app_pid),
        role="codex_appserver",
        expected_executable=context.codex_executable,
    )
    context.app_server_pid = app_pid
    if isolated._worker_process_count(process.pid) != 0:
        raise SmokeAbort("BLOCKED", "worker_started_before_saved_run")
    return {
        "pid_identity_verified": True,
        "parent_is_runner": True,
        "creation_time_tracked": True,
        "app_server_owner_verified": True,
        "restart": restart,
    }


def _http_json(
    context: IsolatedContext,
    method: str,
    route: str,
    *,
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[int, Any, dict[str, str]]:
    header_values = {
        "Accept": "application/json",
        "Host": f"{BRIDGE_HOST}:{context.ports[1]}",
        "Origin": f"http://{BRIDGE_HOST}:{context.ports[1]}",
        "Connection": "close",
    }
    if token is not None:
        header_values["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = _canonical_json(dict(payload)).encode("utf-8")
        header_values["Content-Type"] = "application/json"
    else:
        body = None
    if headers:
        header_values.update(headers)
    connection = http.client.HTTPConnection(BRIDGE_HOST, context.ports[1], timeout=timeout)
    try:
        connection.request(method, route, body=body, headers=header_values)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SmokeAbort("BLOCKED", "control_api_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        return (
            response.status,
            value,
            {key.casefold(): value for key, value in response.getheaders()},
        )
    except SmokeAbort:
        raise
    except (OSError, http.client.HTTPException, TimeoutError):
        raise SmokeAbort("BLOCKED", "control_api_transport_outcome_unknown") from None
    finally:
        connection.close()


async def _mcp_exchange_async(
    *,
    mcp_port: int,
    mcp_token: str,
    calls: Sequence[tuple[str, Mapping[str, Any]]],
) -> tuple[set[str], list[dict[str, Any]]]:
    url = f"http://{BRIDGE_HOST}:{mcp_port}/mcp"
    headers = {
        "Authorization": f"Bearer {mcp_token}",
        "Origin": url.rsplit("/mcp", 1)[0],
    }
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                names = {tool.name for tool in listing.tools}
                results: list[dict[str, Any]] = []
                for name, arguments in calls:
                    result = await session.call_tool(name, dict(arguments))
                    structured = result.structured_content
                    if result.is_error or not isinstance(structured, dict):
                        raise SmokeAbort("FAIL", "mcp_tool_call_failed")
                    results.append(dict(structured))
                return names, results


def _mcp_exchange(
    *, mcp_port: int, mcp_token: str, calls: Sequence[tuple[str, Mapping[str, Any]]] = ()
) -> tuple[set[str], list[dict[str, Any]]]:
    try:
        return asyncio.run(_mcp_exchange_async(mcp_port=mcp_port, mcp_token=mcp_token, calls=calls))
    except SmokeAbort:
        raise
    except Exception:
        raise SmokeAbort("BLOCKED", "isolated_mcp_transport_unavailable") from None


def _assert_mcp_error(value: Mapping[str, Any], expected: str) -> None:
    if value.get("error") != expected:
        raise SmokeAbort("FAIL", "mcp_response_contract_mismatch")


def _control_security_probes(context: IsolatedContext) -> dict[str, int]:
    route = "/ui-api/control/workflow-definitions"
    wrong_bearer = _http_json(context, "GET", route, token="wrong-b5-token")[0]
    bad_origin = _http_json(
        context,
        "GET",
        route,
        token=context.control_token,
        headers={"Origin": "https://evil.invalid"},
    )[0]
    bad_host = _http_json(
        context,
        "GET",
        route,
        token=context.control_token,
        headers={"Host": "evil.invalid"},
    )[0]
    if wrong_bearer != 403 or bad_origin != 403 or bad_host not in {400, 403}:
        raise SmokeAbort("FAIL", "control_api_auth_origin_or_host_guard_failed")
    return {"wrong_bearer": wrong_bearer, "bad_origin": bad_origin, "bad_host": bad_host}


def _run_mcp_control_gate(
    context: IsolatedContext,
    evidence: dict[str, Any],
    writer: phase5f5._EvidenceWriter,
    *,
    base_yaml: str,
    revision_two_yaml: str,
) -> dict[str, Any]:
    gate = evidence["mcp_control_gate"]
    checks: dict[str, Any] = gate["checks"]
    mcp_port = context.ports[0]
    security = _control_security_probes(context)
    checks["control_bearer_origin_host"] = {"status": "PASS", **security}

    initial_validation = {
        "source_yaml": base_yaml,
        "max_live_codex_jobs": 1,
    }
    revision_validation = {
        "source_yaml": revision_two_yaml,
        "max_live_codex_jobs": 1,
    }
    names, validation_results = _mcp_exchange(
        mcp_port=mcp_port,
        mcp_token=context.mcp_token,
        calls=(
            ("workflow_definition_validate", initial_validation),
            (
                "workflow_definition_create",
                {
                    "mutation_request_id": f"b5-create-{uuid.uuid4().hex}",
                    **initial_validation,
                },
            ),
            ("workflow_definition_validate", revision_validation),
        ),
    )
    if not MCP_TOOL_NAMES <= names or not LEGACY_CODEX_TOOL_NAMES <= names:
        raise SmokeAbort("FAIL", "workflow_or_legacy_mcp_tool_catalog_incomplete")
    validation_one, created, validation_two = validation_results
    if validation_one.get("valid") is not True or validation_two.get("valid") is not True:
        raise SmokeAbort("FAIL", "mcp_definition_validation_failed")
    definition_id = created.get("definition_id")
    etag_one = created.get("etag")
    canonical_one = validation_one.get("canonical_sha256")
    source_one = validation_one.get("source_sha256")
    source_two = validation_two.get("source_sha256")
    canonical_two = validation_two.get("canonical_sha256")
    if (
        created.get("revision") != 1
        or not isinstance(definition_id, str)
        or not isinstance(etag_one, str)
        or canonical_one != canonical_two
        or source_one != source_sha256(base_yaml)
        or source_two != source_sha256(revision_two_yaml)
        or source_one == source_two
    ):
        raise SmokeAbort("FAIL", "mcp_definition_create_or_comment_hash_mismatch")

    prefix = "/ui-api/control/workflow-definitions"
    list_status, listing, _ = _http_json(context, "GET", prefix, token=context.control_token)
    detail_status, detail, detail_headers = _http_json(
        context, "GET", f"{prefix}/{definition_id}", token=context.control_token
    )
    history_status, history_one, _ = _http_json(
        context, "GET", f"{prefix}/{definition_id}/revisions/1", token=context.control_token
    )
    graph_status, graph_one, _ = _http_json(
        context, "GET", f"{prefix}/{definition_id}/graph?revision=1", token=context.control_token
    )
    if (
        list_status != 200
        or detail_status != 200
        or history_status != 200
        or graph_status != 200
        or not isinstance(listing, dict)
        or not isinstance(detail, dict)
        or not isinstance(history_one, dict)
        or not isinstance(graph_one, dict)
        or detail.get("revision") != 1
        or detail.get("source_sha256") != source_one
        or detail_headers.get("etag") != etag_one
    ):
        raise SmokeAbort("FAIL", "control_definition_get_list_history_graph_failed")

    update_id = f"b5-update-{uuid.uuid4().hex}"
    update_payload = {
        "mutation_request_id": update_id,
        "source_yaml": revision_two_yaml,
        "max_live_codex_jobs": 1,
    }
    update_status, update, update_headers = _http_json(
        context,
        "PUT",
        f"{prefix}/{definition_id}",
        token=context.control_token,
        payload=update_payload,
        headers={"If-Match": etag_one},
    )
    replay_status, replay, replay_headers = _http_json(
        context,
        "PUT",
        f"{prefix}/{definition_id}",
        token=context.control_token,
        payload=update_payload,
        headers={"If-Match": etag_one},
    )
    conflict_status, conflict, _ = _http_json(
        context,
        "PUT",
        f"{prefix}/{definition_id}",
        token=context.control_token,
        payload={
            **update_payload,
            "source_yaml": add_mutation_conflict_comment(revision_two_yaml),
        },
        headers={"If-Match": etag_one},
    )
    stale_status, stale, _ = _http_json(
        context,
        "PUT",
        f"{prefix}/{definition_id}",
        token=context.control_token,
        payload={
            "mutation_request_id": f"b5-stale-{uuid.uuid4().hex}",
            "source_yaml": base_yaml,
            "max_live_codex_jobs": 1,
        },
        headers={"If-Match": etag_one},
    )
    if (
        update_status != 200
        or not isinstance(update, dict)
        or update.get("revision") != 2
        or update_headers.get("etag") != update.get("etag")
        or replay_status != 200
        or not isinstance(replay, dict)
        or replay.get("revision") != 2
        or replay_headers.get("etag") != update_headers.get("etag")
        or conflict_status != 409
        or not isinstance(conflict, dict)
        or conflict.get("error") != "mutation_conflict"
        or stale_status != 412
        or not isinstance(stale, dict)
        or stale.get("error") != "precondition_failed"
    ):
        raise SmokeAbort("FAIL", "definition_mutation_etag_or_replay_contract_failed")

    revision_two_status, history_two, history_two_headers = _http_json(
        context, "GET", f"{prefix}/{definition_id}/revisions/2", token=context.control_token
    )
    graph_two_status, graph_two, _ = _http_json(
        context, "GET", f"{prefix}/{definition_id}/graph?revision=2", token=context.control_token
    )
    _, mcp_reads = _mcp_exchange(
        mcp_port=mcp_port,
        mcp_token=context.mcp_token,
        calls=(
            ("workflow_definition_get", {"definition_id": definition_id, "revision": 1}),
            ("workflow_definition_get", {"definition_id": definition_id, "revision": 2}),
            ("workflow_graph_get", {"definition_id": definition_id, "revision": 1}),
            ("workflow_graph_get", {"definition_id": definition_id, "revision": 2}),
        ),
    )
    mcp_one, mcp_two, mcp_graph_one, mcp_graph_two = mcp_reads
    if (
        revision_two_status != 200
        or graph_two_status != 200
        or not isinstance(history_two, dict)
        or not isinstance(graph_two, dict)
        or history_two.get("revision") != 2
        or history_two.get("source_sha256") != source_two
        or history_two.get("canonical_sha256") != canonical_one
        or history_two_headers.get("etag") != update_headers.get("etag")
        or mcp_one.get("source_sha256") != source_one
        or mcp_one.get("canonical_sha256") != canonical_one
        or mcp_two.get("source_sha256") != source_two
        or mcp_two.get("canonical_sha256") != canonical_one
        or mcp_graph_one.get("revision") != 1
        or mcp_graph_two.get("revision") != 2
        or mcp_graph_one.get("etag") != etag_one
        or mcp_graph_two.get("etag") != update_headers.get("etag")
    ):
        raise SmokeAbort("FAIL", "mcp_http_revision_or_graph_readback_mismatch")

    checks.update(
        {
            "mcp_catalog": {
                "status": "PASS",
                "new_workflow_tool_count": len(MCP_TOOL_NAMES),
                "legacy_codex_tools_present": True,
                "dedicated_bearer_and_allowed_loopback_origin": True,
            },
            "mcp_validate_create": {"status": "PASS", "revision": 1},
            "http_definition_list_history_graph": {"status": "PASS"},
            "comment_only_revision_hashes": {
                "status": "PASS",
                "source_hash_changed": True,
                "canonical_hash_unchanged": True,
                "revision_one_source_sha256_prefix": str(source_one)[:12],
                "revision_two_source_sha256_prefix": str(source_two)[:12],
                "canonical_sha256_prefix": str(canonical_one)[:12],
            },
            "etag_stale_and_mutation_replay": {
                "status": "PASS",
                "stale_status": stale_status,
                "same_mutation_replay_status": replay_status,
                "same_mutation_replay_revision": replay.get("revision"),
                "different_payload_same_mutation_status": conflict_status,
                "latest_revision": history_two.get("revision"),
            },
            "permission_negative_catalog": {
                "status": "PASS",
                "method": "targeted_offline_catalog_regression_tests",
                "real_permission_process": "NOT_RUN",
            },
            "control_and_mcp_authority_separated": {"status": "PASS"},
        }
    )
    gate["definition"] = {
        "definition_id_prefix": definition_id[:8],
        "revision_one_etag_sha256_prefix": _sha256(etag_one.encode("utf-8"))[:12],
        "revision_two_etag_sha256_prefix": _sha256(str(update_headers.get("etag")).encode("utf-8"))[
            :12
        ],
    }
    gate["status"] = "PASS"
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
    return {
        "definition_id": definition_id,
        "revision_one": 1,
        "revision_two": 2,
        "etag_one": etag_one,
        "etag_two": update_headers.get("etag"),
        "source_sha256_one": source_one,
        "source_sha256_two": source_two,
        "canonical_sha256": canonical_one,
        "source_yaml_one": base_yaml,
        "source_yaml_two": revision_two_yaml,
    }


def _post_run_request(
    context: IsolatedContext,
    *,
    definition_id: str,
    revision: int,
    client_request_id: str,
) -> dict[str, Any]:
    return {
        "definition_id": definition_id,
        "revision": revision,
        "client_request_id": client_request_id,
        "workspace_root": str(context.workspace.resolve(strict=True)),
        "input": {},
    }


def _assert_ready_for_saved_run(context: IsolatedContext, definition: Mapping[str, Any]) -> None:
    if context.process is None or context.identity is None:
        raise SmokeAbort("BLOCKED", "owned_bridge_identity_missing_before_workflow_post")
    phase5f5._assert_bridge_identity(context.process, context.identity)
    isolated._owned_listener_pids(context.process.pid, context.ports)
    status = isolated._preflight_bridge(
        context.process,
        mcp_port=context.ports[0],
        ui_port=context.ports[1],
        token=context.control_token,
        expected_codex=context.codex_executable,
    )
    if status.get("appserver_pid") != context.app_server_pid:
        raise SmokeAbort("BLOCKED", "owned_app_server_identity_changed_before_workflow_post")
    if context.app_server_identity is None:
        raise SmokeAbort("BLOCKED", "owned_app_server_identity_missing_before_workflow_post")
    appserver_created_at = context.app_server_identity.get("created_at_epoch")
    if (
        type(context.app_server_identity.get("pid")) is not int
        or context.app_server_identity.get("pid") != context.app_server_pid
        or type(appserver_created_at) not in {int, float}
        or not isolated._process_still_matches(context.app_server_pid, appserver_created_at)
    ):
        raise SmokeAbort("BLOCKED", "owned_app_server_identity_changed_before_workflow_post")
    missing = missing_workflow_dependencies()
    if missing:
        raise SmokeAbort("BLOCKED", "workflow_runtime_dependency_preflight_failed")
    if (
        context.workspace.is_symlink()
        or not context.workspace.is_dir()
        or any(context.workspace.iterdir())
        or context.state_root.is_symlink()
        or not context.state_root.is_relative_to(context.temp_root)
        or (context.state_root / "requests.sqlite").exists()
        or isolated._worker_process_count(context.process.pid) != 0
    ):
        raise SmokeAbort("BLOCKED", "saved_run_post_workspace_or_empty_ledger_preflight_failed")
    revisions = _read_sqlite(
        context.state_root / "definitions.sqlite",
        "SELECT definition_id, revision, source_sha256, canonical_sha256 "
        "FROM workflow_definition_revisions WHERE definition_id = ? ORDER BY revision",
        (definition["definition_id"],),
    )
    if (
        len(revisions) != 2
        or tuple(row.get("revision") for row in revisions) != (1, 2)
        or revisions[1].get("source_sha256") != definition.get("source_sha256_two")
        or revisions[1].get("canonical_sha256") != definition.get("canonical_sha256")
    ):
        raise SmokeAbort("BLOCKED", "saved_definition_revision_preflight_mismatch")


def _post_initial_http(
    context: IsolatedContext,
    request: Mapping[str, Any],
) -> tuple[int, Any]:
    payload = {key: value for key, value in request.items() if key != "definition_id"}
    status, body, _ = _http_json(
        context,
        "POST",
        f"/ui-api/control/workflow-definitions/{request['definition_id']}/runs",
        token=context.control_token,
        payload=payload,
        timeout=25.0,
    )
    return status, body


def _wait_workflow_success(
    context: IsolatedContext,
    *,
    workflow_id: str,
    evidence: dict[str, Any],
    writer: phase5f5._EvidenceWriter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.monotonic()
    deadline = started_at + MAX_WORKFLOW_WAIT_SECONDS
    detail_path = f"/ui-api/control/workflows/{quote(workflow_id, safe='')}"
    result_path = detail_path + "/result"
    last_status = "NOT_OBSERVED"
    gate = evidence["saved_execution_gate"]
    observations: dict[str, Any] = {"transitions": [], "omitted_count": 0}
    gate["status_observations"] = observations
    while time.monotonic() < deadline:
        poll_started_at = time.monotonic()
        try:
            status, detail, _ = _http_json(context, "GET", detail_path, token=context.control_token)
        except SmokeAbort:
            _record_status_observation(
                observations,
                elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                status="unknown",
                http_status=None,
            )
            _safe_write(
                writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token)
            )
            raise
        safe_http_status = status if type(status) is int and 100 <= status <= 599 else None
        if safe_http_status != 200 or not isinstance(detail, dict):
            _record_status_observation(
                observations,
                elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                status="unknown",
                http_status=safe_http_status,
            )
            _safe_write(
                writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token)
            )
            raise SmokeAbort("BLOCKED", "workflow_status_readback_unavailable")
        raw_status = detail.get("status")
        last_status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in _WORKFLOW_STATUS_VALUES
            else "unknown"
        )
        _record_status_observation(
            observations,
            elapsed_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            status=last_status,
            http_status=safe_http_status,
        )
        if last_status in {"failed", "failed_to_start"}:
            gate["workflow_status"] = last_status
            gate["result_gate"] = "NOT_OBSERVED"
            _safe_write(
                writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token)
            )
            raise SmokeAbort("BLOCKED", "workflow_did_not_reach_confirmed_success")
        if last_status == "succeeded":
            _safe_write(
                writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token)
            )
            result_status, result, _ = _http_json(
                context, "GET", result_path, token=context.control_token
            )
            if result_status == 200 and isinstance(result, dict):
                return detail, result
            raise SmokeAbort("FAIL", "workflow_result_endpoint_mismatch")
        _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
        now = time.monotonic()
        remaining = deadline - now
        until_next_poll = max(0.0, 0.5 - (now - poll_started_at))
        if remaining > 0 and until_next_poll > 0:
            time.sleep(min(until_next_poll, remaining))
    gate["workflow_status"] = last_status
    gate["result_gate"] = "NOT_OBSERVED"
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
    timeout_reason = (
        "workflow_reconciliation_unresolved_at_deadline"
        if last_status in {"needs_reconciliation", "unknown"}
        else "workflow_result_wait_timed_out"
    )
    raise SmokeAbort("BLOCKED", timeout_reason)


def _record_status_observation(
    observations: dict[str, Any],
    *,
    elapsed_ms: int,
    status: str,
    http_status: int | None,
) -> None:
    transitions = observations["transitions"]
    omitted_count = observations["omitted_count"]
    if (
        transitions
        and omitted_count == 0
        and transitions[-1]["status"] == status
        and transitions[-1]["http_status"] == http_status
    ):
        transitions[-1]["last_elapsed_ms"] = elapsed_ms
        transitions[-1]["count"] += 1
        return
    if len(transitions) < MAX_STATUS_OBSERVATION_TRANSITIONS:
        transitions.append(
            {
                "first_elapsed_ms": elapsed_ms,
                "last_elapsed_ms": elapsed_ms,
                "status": status,
                "http_status": http_status,
                "count": 1,
            }
        )
        return
    observations["omitted_count"] = omitted_count + 1


def _read_sqlite(path: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise SmokeAbort("BLOCKED", "sqlite_artifact_unavailable")
    try:
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        raise SmokeAbort("BLOCKED", "sqlite_read_only_query_failed") from None


def _read_json(path: Path, *, maximum: int = 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise SmokeAbort("BLOCKED", "isolated_run_artifact_unavailable")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "isolated_run_artifact_unreadable") from None


def _read_codex_job_ledger(database: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    jobs = _read_sqlite(
        database,
        "SELECT job_id, request_key, workflow_id, task_identity, state, thread_id, turn_id, "
        "remote_state, receipt_json FROM jobs ORDER BY rowid",
    )
    events = _read_sqlite(
        database,
        "SELECT event_id, job_id, state FROM job_events ORDER BY event_id",
    )
    return jobs, events


class _OwnedWorkerObserver:
    def __init__(self, context: IsolatedContext) -> None:
        if context.process is None or context.identity is None:
            raise SmokeAbort("BLOCKED", "owned_bridge_identity_missing_for_worker_observer")
        self.context = context
        self.snapshots: list[list[dict[str, Any]]] = []
        self.error_code: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._observe, name="b5-owned-worker-observer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _observe(self) -> None:
        assert self.context.process is not None
        assert self.context.identity is not None
        while not self._stop.is_set():
            try:
                snapshot = phase5f5._worker_process_snapshot(
                    self.context.process.pid, self.context.identity
                )
            except SmokeAbort as error:
                self.error_code = error.code
                return
            if snapshot:
                self.snapshots.append(snapshot)
            self._stop.wait(0.05)

    def finish(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            raise SmokeAbort("BLOCKED", "worker_process_observer_did_not_stop")


def _verify_worker_ownership(
    context: IsolatedContext,
    *,
    run_id: str,
    workflow_id: str,
    observer: _OwnedWorkerObserver,
) -> dict[str, Any]:
    if context.process is None or context.identity is None:
        raise SmokeAbort("BLOCKED", "owned_bridge_identity_missing_for_worker_verification")
    if observer.error_code is not None:
        raise SmokeAbort("BLOCKED", "worker_process_identity_observation_failed")
    run_dir = isolated._run_directory(context.state_root, run_id)
    identity = _read_json(run_dir / "start-worker-identity.json", maximum=8192)
    bridge_events = phase5f5._read_lifecycle(
        run_dir / "bridge-lifecycle.jsonl", run_id=run_id, workflow_id=workflow_id
    )
    launches = [row for row in bridge_events if row.get("event") == "worker_spawn_succeeded"]
    if (
        not isinstance(identity, dict)
        or len(launches) != 1
        or type(launches[0].get("pid")) is not int
        or launches[0].get("workflow_id") != workflow_id
        or launches[0].get("run_id") != run_id
    ):
        raise SmokeAbort("BLOCKED", "worker_identity_record_or_launch_event_missing")
    allowed_executables = phase5f5._allowed_worker_executable_paths(
        executable=sys.executable,
        base_executable=sys._base_executable,
        prefix=sys.prefix,
        base_prefix=sys.base_prefix,
    )
    for snapshots in observer.snapshots:
        match = phase5f5._match_worker_process_pair(
            launcher_pid=launches[0]["pid"],
            worker_identity_record=identity,
            process_snapshots=snapshots,
            run_dir=run_dir,
            owned_bridge_pid=context.process.pid,
            allowed_worker_executables=allowed_executables,
        )
        if match is not None:
            return {
                "status": "PASS",
                "pid_parent_creation_time_verified": True,
                "owned_bridge_ancestry_verified": True,
                "interpreter_allowlist_verified": True,
                "process_model": match.get("model"),
                "observed_snapshot_count": len(observer.snapshots),
            }
    raise SmokeAbort("BLOCKED", "worker_process_pair_not_observed")


def _record_read_only_post_diagnostic(
    context: IsolatedContext,
    request: Mapping[str, Any],
    evidence: dict[str, Any],
) -> None:
    diagnostic: dict[str, Any] = {
        "status": "NOT_OBSERVED",
        "method": "sqlite_read_only_and_run_artifacts",
        "client_request_id_prefix": str(request.get("client_request_id", ""))[:12],
        "matching_request_rows": 0,
        "request_state": None,
        "workflow_id_prefix": None,
        "run_id_prefix": None,
        "observed_codex_jobs": 0,
        "codex_job_observation": "NOT_OBSERVED",
    }
    database = context.state_root / "requests.sqlite"
    if not database.exists():
        evidence["saved_execution_gate"]["unknown_outcome_diagnostic"] = diagnostic
        return
    rows = _read_sqlite(
        database,
        "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests "
        "WHERE client_request_id = ?",
        (request["client_request_id"],),
    )
    diagnostic["matching_request_rows"] = len(rows)
    if len(rows) > 1:
        diagnostic["status"] = "FAIL"
        diagnostic["codex_job_observation"] = "NOT_OBSERVED"
    elif rows:
        diagnostic["status"] = "PASS"
        row = rows[0]
        diagnostic["request_state"] = row.get("state")
        workflow_id = row.get("workflow_id")
        run_id = row.get("run_id")
        diagnostic["workflow_id_prefix"] = str(workflow_id)[:8]
        diagnostic["run_id_prefix"] = str(run_id)[:12]
        if isinstance(run_id, str):
            try:
                run_dir = isolated._run_directory(context.state_root, run_id)
                job_path = run_dir / "codex_jobs.sqlite"
                if job_path.exists():
                    jobs = _read_sqlite(job_path, "SELECT job_id, state FROM jobs")
                    diagnostic["observed_codex_jobs"] = len(jobs)
                    diagnostic["codex_job_observation"] = "PASS"
                    evidence["saved_execution_gate"]["posts"]["actual_codex_jobs"] = len(jobs)
                    evidence["saved_execution_gate"]["posts"]["actual_codex_job_observation"] = (
                        "PASS"
                    )
            except SmokeAbort:
                diagnostic["codex_job_observation"] = "NOT_OBSERVED"
    evidence["saved_execution_gate"]["unknown_outcome_diagnostic"] = diagnostic


def _verify_durable_run(
    context: IsolatedContext,
    *,
    request: Mapping[str, Any],
    accepted: Mapping[str, Any],
    details: Mapping[str, Any],
    api_result: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    workflow_id = accepted.get("workflow_id")
    run_id = accepted.get("run_id")
    if not isinstance(workflow_id, str) or not isinstance(run_id, str):
        raise SmokeAbort("FAIL", "accepted_workflow_identity_missing")
    request_rows = _read_sqlite(
        context.state_root / "requests.sqlite",
        "SELECT client_request_id, workflow_id, run_id, request_digest, state "
        "FROM workflow_requests",
    )
    source_rows = _read_sqlite(
        context.state_root / "requests.sqlite",
        "SELECT run_id, definition_id, revision, source_sha256 FROM workflow_request_sources",
    )
    if (
        len(request_rows) != 1
        or len(source_rows) != 1
        or request_rows[0].get("client_request_id") != request.get("client_request_id")
        or request_rows[0].get("workflow_id") != workflow_id
        or request_rows[0].get("run_id") != run_id
        or request_rows[0].get("state") not in {"running", "succeeded"}
        or source_rows[0].get("run_id") != run_id
        or source_rows[0].get("definition_id") != definition.get("definition_id")
        or source_rows[0].get("revision") != 2
        or source_rows[0].get("source_sha256") != definition.get("source_sha256_two")
    ):
        raise SmokeAbort("FAIL", "workflow_request_and_source_ledgers_mismatch")
    run_dir = isolated._run_directory(context.state_root, run_id)
    manifest = _read_json(run_dir / "manifest.json", maximum=32 * 1024)
    stored_result = _read_json(run_dir / "result.json")
    worker_status = _read_json(run_dir / "worker-entry-status.json", maximum=8192)
    stored_definition = _read_json(run_dir / "workflow.yaml", maximum=64 * 1024)
    stored_input = _read_json(run_dir / "input.json", maximum=64 * 1024)
    jobs, job_events = _read_codex_job_ledger(run_dir / "codex_jobs.sqlite")
    bridge_events = phase5f5._read_lifecycle(
        run_dir / "bridge-lifecycle.jsonl", run_id=run_id, workflow_id=workflow_id
    )
    dbos_status = isolated._read_dbos_status(run_dir, workflow_id)
    expected_input_sha = _sha256(b"{}")
    expected_definition = yaml.safe_load(definition["source_yaml_two"])
    request_digest_payload = {
        "workflow": yaml.safe_load(definition["source_yaml_two"]),
        "input": {},
        "workspace_root": str(context.workspace.resolve(strict=True)),
    }
    expected_request_digest = _sha256(_canonical_json(request_digest_payload).encode("utf-8"))
    definition_json = _canonical_json(yaml.safe_load(definition["source_yaml_two"]))
    expected_definition_sha = _sha256((definition_json + "\n").encode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("workflow_source") != "ui-api"
        or manifest.get("mode") != "codex-job-live"
        or manifest.get("request_digest") != request_rows[0].get("request_digest")
        or manifest.get("request_digest") != expected_request_digest
        or manifest.get("workspace_root") != str(context.workspace.resolve(strict=True))
        or manifest.get("definition_sha256") != expected_definition_sha
        or manifest.get("definition_sha256") != definition.get("canonical_sha256")
        or manifest.get("input_sha256") != expected_input_sha
        or not isinstance(manifest.get("control"), dict)
        or manifest["control"].get("codex_job_live") is not True
        or manifest["control"].get("codex_job_max_polls") != 600
        or manifest["control"].get("codex_job_poll_interval_seconds") != 0.2
        or stored_definition != expected_definition
        or stored_input != {}
        or not isinstance(worker_status, dict)
        or worker_status.get("state") != "completed"
        or worker_status.get("exit_code") != 0
        or dbos_status != "SUCCESS"
        or len(jobs) != 1
        or not job_events
        or jobs[0].get("state") != "SUCCEEDED"
        or jobs[0].get("workflow_id") != workflow_id
        or jobs[0].get("task_identity") != "do[0].calculate"
        or jobs[0].get("request_key") != f"{workflow_id}::do[0].calculate"
        or not isinstance(jobs[0].get("thread_id"), str)
        or not isinstance(jobs[0].get("turn_id"), str)
        or jobs[0].get("remote_state") != "completed"
        or not isinstance(jobs[0].get("receipt_json"), str)
    ):
        raise SmokeAbort("FAIL", "manifest_dbos_or_job_ledger_mismatch")
    try:
        receipt = json.loads(jobs[0]["receipt_json"])
    except json.JSONDecodeError:
        receipt = None
    spawn_events = [row for row in bridge_events if row.get("event") == "worker_spawn_succeeded"]
    maybe_sent_events = [row for row in job_events if row.get("state") == "MAYBE_SENT"]
    succeeded_events = [row for row in job_events if row.get("state") == "SUCCEEDED"]
    if (
        receipt != {"result": {"final_text": EXPECTED_RESULT}}
        or len(spawn_events) != 1
        or len(maybe_sent_events) != 1
        or len(succeeded_events) != 1
        or maybe_sent_events[0].get("job_id") != jobs[0].get("job_id")
        or succeeded_events[0].get("job_id") != jobs[0].get("job_id")
        or any(event.get("job_id") != jobs[0].get("job_id") for event in job_events)
    ):
        raise SmokeAbort("FAIL", "codex_job_receipt_or_single_dispatch_mismatch")
    job_result = stored_result.get("result") if isinstance(stored_result, dict) else None
    if (
        not isinstance(job_result, dict)
        or stored_result.get("workflow_id") != workflow_id
        or stored_result.get("definition_sha256") != expected_definition_sha
        or stored_result.get("input_sha256") != expected_input_sha
        or job_result.get("job_id") != jobs[0].get("job_id")
        or job_result.get("request_key") != jobs[0].get("request_key")
        or not isinstance(job_result.get("result"), dict)
        or job_result["result"].get("final_text") != EXPECTED_RESULT
        or dict(stored_result) != dict(api_result)
        or details.get("workflow_id") != workflow_id
        or details.get("run_id") != run_id
        or details.get("status") != "succeeded"
    ):
        raise SmokeAbort("FAIL", "saved_run_result_or_api_projection_mismatch")
    return {
        "request_state": request_rows[0]["state"],
        "request_digest_prefix": str(request_rows[0]["request_digest"])[:12],
        "definition_source_sha256_prefix": str(source_rows[0]["source_sha256"])[:12],
        "definition_sha256_prefix": str(manifest["definition_sha256"])[:12],
        "dbos_status": dbos_status,
        "worker_state": worker_status["state"],
        "worker_exit_code": worker_status["exit_code"],
        "job_count": len(jobs),
        "job_id_prefix": str(jobs[0]["job_id"])[:8],
        "job_state": jobs[0]["state"],
        "job_event_count": len(job_events),
        "codex_start_marker_count": len(maybe_sent_events),
        "codex_success_event_count": len(succeeded_events),
        "worker_spawn_event_count": len(spawn_events),
        "result_exact_match": True,
        "result_sha256_prefix": _sha256(_canonical_json(stored_result).encode("utf-8"))[:12],
        "workflow_id_prefix": workflow_id[:8],
        "run_id_prefix": run_id[:12],
    }


def _mcp_same_id_replay(context: IsolatedContext, request: Mapping[str, Any]) -> dict[str, Any]:
    args = {
        "definition_id": request["definition_id"],
        "revision": request["revision"],
        "client_request_id": request["client_request_id"],
        "workspace_root": request["workspace_root"],
        "input": dict(request["input"]),
    }
    _names, results = _mcp_exchange(
        mcp_port=context.ports[0],
        mcp_token=context.mcp_token,
        calls=(("workflow_run_start", args),),
    )
    return results[0]


def _read_public_run_views(
    context: IsolatedContext, workflow_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    status, listings, _ = _http_json(
        context, "GET", "/ui-api/control/workflows?limit=50", token=context.control_token
    )
    detail_status, detail, _ = _http_json(
        context,
        "GET",
        f"/ui-api/control/workflows/{quote(workflow_id, safe='')}",
        token=context.control_token,
    )
    result_status, result, _ = _http_json(
        context,
        "GET",
        f"/ui-api/control/workflows/{quote(workflow_id, safe='')}/result",
        token=context.control_token,
    )
    if (
        status != 200
        or detail_status != 200
        or result_status != 200
        or not isinstance(listings, dict)
        or not isinstance(detail, dict)
        or not isinstance(result, dict)
    ):
        raise SmokeAbort("FAIL", "control_workflow_readback_failed")
    workflows = listings.get("workflows")
    items = (
        [row for row in workflows if isinstance(row, dict)] if isinstance(workflows, list) else []
    )
    matches = [row for row in items if row.get("workflow_id") == workflow_id]
    if len(matches) != 1:
        raise SmokeAbort("FAIL", "control_workflow_list_identity_mismatch")
    return matches[0], detail, result, listings


def _read_mcp_run_views(
    context: IsolatedContext, workflow_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _names, results = _mcp_exchange(
        mcp_port=context.ports[0],
        mcp_token=context.mcp_token,
        calls=(
            ("workflow_runs_list", {"limit": 50}),
            ("workflow_run_get", {"workflow_id": workflow_id}),
            ("workflow_run_result", {"workflow_id": workflow_id}),
        ),
    )
    listing, detail, result = results
    rows = listing.get("workflows")
    matches = (
        [row for row in rows if isinstance(row, dict) and row.get("workflow_id") == workflow_id]
        if isinstance(rows, list)
        else []
    )
    if len(matches) != 1 or detail.get("workflow_id") != workflow_id:
        raise SmokeAbort("FAIL", "mcp_workflow_readback_identity_mismatch")
    return matches[0], detail, result


def _qt_readback(
    context: IsolatedContext, detail: Mapping[str, Any], result: Mapping[str, Any]
) -> None:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    phase5f5._qt_readback(
        ui_port=context.ports[1],
        token=context.control_token,
        label="B5",
        workflow_id=str(detail.get("workflow_id")),
        expected_detail=detail,
        expected_result=result,
    )
    application = QApplication.instance()
    if application is not None:
        application.processEvents()


def _run_execution_gate(
    context: IsolatedContext,
    evidence: dict[str, Any],
    writer: phase5f5._EvidenceWriter,
    *,
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    gate = evidence["saved_execution_gate"]
    budget = PostBudget()
    client_request_id = f"b5-run-{uuid.uuid4().hex}"
    request = _post_run_request(
        context,
        definition_id=str(definition["definition_id"]),
        revision=2,
        client_request_id=client_request_id,
    )
    context.active_workflow_request = request
    _assert_ready_for_saved_run(context, definition)
    gate["readiness_preflight"] = {
        "status": "PASS",
        "owned_bridge_and_listener_identity": True,
        "owned_app_server_identity": True,
        "production_defaults_avoided_by_dynamic_ports": True,
        "workflow_dependencies_ready": True,
        "workspace_empty_and_isolated": True,
        "request_ledger_empty": True,
        "worker_count_before_post": 0,
        "saved_revision_hashes_verified": True,
    }
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
    observer = _OwnedWorkerObserver(context)
    observer.start()
    context.active_worker_observer = observer

    def record_initial_send_boundary() -> None:
        gate["posts"]["initial_http_workflow_posts"] = 1
        gate["posts"]["unique_initial_client_request_ids"] = 1
        gate["posts"]["initial_post_outcome"] = "MAYBE_SENT"
        _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))

    try:
        status, accepted = send_initial_once(
            budget,
            request,
            lambda value: _post_initial_http(context, value),
            before_send=record_initial_send_boundary,
        )
    except SmokeAbort as error:
        gate["posts"]["initial_http_workflow_posts"] = budget.initial_calls
        gate["posts"]["initial_post_outcome"] = "UNKNOWN"
        gate["result_gate"] = "NOT_OBSERVED"
        try:
            observer.finish()
        finally:
            context.active_worker_observer = None
            _record_read_only_post_diagnostic(context, request, evidence)
            _safe_write(
                writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token)
            )
        if error.code == "initial_workflow_post_not_accepted":
            raise SmokeAbort("BLOCKED", "initial_workflow_post_acceptance_not_confirmed") from None
        raise
    gate["posts"]["initial_http_workflow_posts"] = budget.initial_calls
    gate["posts"]["initial_post_outcome"] = "ACCEPTED"
    if (
        status != 202
        or not isinstance(accepted, dict)
        or accepted.get("definition_id") != request["definition_id"]
        or accepted.get("revision") != 2
        or not isinstance(accepted.get("workflow_id"), str)
        or not isinstance(accepted.get("run_id"), str)
    ):
        raise SmokeAbort("FAIL", "initial_saved_workflow_acceptance_mismatch")
    gate["initial_acceptance"] = {
        "status": 202,
        "workflow_id_prefix": accepted["workflow_id"][:8],
        "run_id_prefix": accepted["run_id"][:12],
        "revision": accepted["revision"],
    }
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))

    # Exercise conflict guards only while the accepted run is still active. The active
    # workspace also makes a rejected probe unable to launch a second worker.
    detail_status, active_detail, _ = _http_json(
        context,
        "GET",
        f"/ui-api/control/workflows/{quote(accepted['workflow_id'], safe='')}",
        token=context.control_token,
    )
    if (
        detail_status == 200
        and isinstance(active_detail, dict)
        and active_detail.get("status") in {"queued", "starting", "running"}
    ):
        gate["posts"]["same_id_conflict_http_posts"] = 1
        _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
        conflict_revision_status, conflict_revision, _ = _http_json(
            context,
            "POST",
            f"/ui-api/control/workflow-definitions/{request['definition_id']}/runs",
            token=context.control_token,
            payload={
                "client_request_id": client_request_id,
                "revision": 1,
                "workspace_root": request["workspace_root"],
                "input": {},
            },
            timeout=10.0,
        )
        if (
            conflict_revision_status != 409
            or not isinstance(conflict_revision, dict)
            or conflict_revision.get("error") != "request_id_conflict"
        ):
            gate["same_id_conflicts"] = {
                "status": "FAIL",
                "revision_status": conflict_revision_status,
                "inline_status": "NOT_RUN",
            }
            raise SmokeAbort("FAIL", "same_id_revision_conflict_not_rejected")
        gate["posts"]["same_id_conflict_http_posts"] = 2
        _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
        conflict_inline_status, conflict_inline, _ = _http_json(
            context,
            "POST",
            "/ui-api/control/workflows",
            token=context.control_token,
            payload={
                "client_request_id": client_request_id,
                "workflow_yaml": definition["source_yaml_two"],
                "input": {},
                "workspace_root": request["workspace_root"],
                "max_live_codex_jobs": 1,
            },
            timeout=10.0,
        )
        if (
            conflict_inline_status != 409
            or not isinstance(conflict_inline, dict)
            or conflict_inline.get("error") != "request_id_conflict"
        ):
            raise SmokeAbort("FAIL", "same_id_revision_or_inline_conflict_not_rejected")
        gate["same_id_conflicts"] = {"status": "PASS", "revision_status": 409, "inline_status": 409}
    else:
        gate["same_id_conflicts"] = {
            "status": "NOT_RUN",
            "reason": "run_completed_before_conflict_probe_window",
            "offline_regression": "PASS",
        }
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))

    try:
        detail, result = _wait_workflow_success(
            context,
            workflow_id=accepted["workflow_id"],
            evidence=evidence,
            writer=writer,
        )
    finally:
        try:
            observer.finish()
        finally:
            context.active_worker_observer = None
    worker_ownership = _verify_worker_ownership(
        context,
        run_id=accepted["run_id"],
        workflow_id=accepted["workflow_id"],
        observer=observer,
    )
    list_item, detail, api_result, _listing = _read_public_run_views(
        context, accepted["workflow_id"]
    )
    mcp_list_item, mcp_detail, mcp_result = _read_mcp_run_views(context, accepted["workflow_id"])
    durable = _verify_durable_run(
        context,
        request=request,
        accepted=accepted,
        details=detail,
        api_result=api_result,
        definition=definition,
    )
    if (
        result != api_result
        or mcp_result != api_result
        or list_item.get("status") != "succeeded"
        or mcp_list_item.get("status") != "succeeded"
        or mcp_detail.get("run_id") != accepted.get("run_id")
        or api_result.get("result", {}).get("result", {}).get("final_text") != EXPECTED_RESULT
    ):
        raise SmokeAbort("FAIL", "mcp_http_and_durable_result_views_disagree")

    budget.claim_same_id_replay(request)
    gate["posts"]["same_id_mcp_replay_calls"] = budget.same_id_replay_calls
    gate["posts"]["same_id_mcp_replay_outcome"] = "MAYBE_SENT"
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
    try:
        replay = _mcp_same_id_replay(context, request)
    except SmokeAbort:
        gate["posts"]["same_id_mcp_replay_outcome"] = "UNKNOWN"
        gate["result_gate"] = "NOT_OBSERVED"
        _record_read_only_post_diagnostic(context, request, evidence)
        _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
        raise SmokeAbort("BLOCKED", "same_id_mcp_replay_outcome_unknown") from None
    gate["posts"]["same_id_mcp_replay_outcome"] = "REUSED"
    if (
        replay.get("workflow_id") != accepted.get("workflow_id")
        or replay.get("run_id") != accepted.get("run_id")
        or replay.get("definition_id") != request.get("definition_id")
        or replay.get("revision") != 2
    ):
        raise SmokeAbort("FAIL", "same_id_mcp_replay_did_not_reuse_accepted_run")
    post_replay_rows = isolated._read_request_ledger(context.state_root)
    if len(post_replay_rows) != 1 or post_replay_rows[0].get("workflow_id") != accepted.get(
        "workflow_id"
    ):
        raise SmokeAbort("FAIL", "same_id_mcp_replay_created_another_workflow")
    durable_after_replay = _verify_durable_run(
        context,
        request=request,
        accepted=accepted,
        details=detail,
        api_result=api_result,
        definition=definition,
    )
    if any(
        durable_after_replay.get(key) != durable.get(key)
        for key in (
            "job_count",
            "job_event_count",
            "codex_start_marker_count",
            "codex_success_event_count",
            "worker_spawn_event_count",
        )
    ):
        raise SmokeAbort("FAIL", "same_id_mcp_replay_created_another_codex_job")
    _qt_readback(context, detail, api_result)
    gate["workflow_status"] = "succeeded"
    gate["result_gate"] = "PASS"
    gate["durable_readback"] = durable
    gate["worker_ownership"] = worker_ownership
    gate["mcp_replay"] = {
        "status": "PASS",
        "existing_workflow_reused": True,
        "additional_workflows": 0,
        "additional_codex_jobs": 0,
    }
    gate["cross_api_readback"] = {
        "status": "PASS",
        "http_list_detail_result": True,
        "mcp_list_get_result": True,
        "qt_offscreen": "PASS",
    }
    gate["posts"]["actual_codex_jobs"] = 1
    gate["posts"]["new_client_request_ids_after_initial"] = 0
    gate["status"] = "PASS" if gate["same_id_conflicts"]["status"] == "PASS" else "BLOCKED"
    if gate["status"] != "PASS":
        gate["reason"] = "same_id_conflict_live_window_not_observed"
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))
    return {
        "request": request,
        "accepted": accepted,
        "detail": detail,
        "result": api_result,
        "durable": durable,
    }


def _restart_ports(first_pair: tuple[int, int]) -> tuple[tuple[int, int], list[socket.socket]]:
    for _ in range(16):
        pair, reservations = isolated._reserve_loopback_ports()
        if not set(pair).intersection(first_pair):
            return pair, reservations
        for reservation in reservations:
            reservation.close()
    raise SmokeAbort("BLOCKED", "restart_loopback_port_pair_unavailable")


def _release_reserved_ports(ports: tuple[int, int], reservations: list[socket.socket]) -> None:
    for reservation in reservations:
        reservation.close()
    reservations.clear()
    isolated._assert_ports_free(ports)


def _run_restart_gate(
    context: IsolatedContext,
    evidence: dict[str, Any],
    writer: phase5f5._EvidenceWriter,
    *,
    run: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> None:
    gate = evidence["restart_gate"]
    first_rows = isolated._read_request_ledger(context.state_root)
    first_job = _verify_durable_run(
        context,
        request=run["request"],
        accepted=run["accepted"],
        details=run["detail"],
        api_result=run["result"],
        definition=definition,
    )
    if context.process is None or context.identity is None:
        raise SmokeAbort("BLOCKED", "initial_bridge_identity_missing")
    initial_shutdown = stop_owned_bridge(
        context.process,
        context.identity,
        ui_port=context.ports[1],
        control_token=context.control_token,
    )
    evidence["processes"]["initial_bridge"] = {
        "identity_verified": True,
        **initial_shutdown,
    }
    new_ports, reservations = _restart_ports(context.ports)
    _release_reserved_ports(new_ports, reservations)
    context.ports = new_ports
    context.environment, _config, verified_state_root = _build_runtime_environment(
        temp_root=context.temp_root,
        workspace=context.workspace,
        codex_executable=context.codex_executable,
        source_home=Path(context.environment["CODEX_HOME"]).parent,
        control_token=context.control_token,
        mcp_token=context.mcp_token,
        ports=context.ports,
        config_name="bridge-restart.toml",
    )
    if verified_state_root != context.state_root:
        raise SmokeAbort("BLOCKED", "restart_state_root_changed")
    context.process = None
    context.identity = None
    restart_identity = _start_bridge(context, restart=True)
    evidence["processes"]["restart_bridge"] = restart_identity

    prefix = "/ui-api/control/workflow-definitions"
    detail_status, latest, latest_headers = _http_json(
        context,
        "GET",
        f"{prefix}/{definition['definition_id']}",
        token=context.control_token,
    )
    history_one_status, history_one, _ = _http_json(
        context,
        "GET",
        f"{prefix}/{definition['definition_id']}/revisions/1",
        token=context.control_token,
    )
    history_two_status, history_two, history_two_headers = _http_json(
        context,
        "GET",
        f"{prefix}/{definition['definition_id']}/revisions/2",
        token=context.control_token,
    )
    if (
        detail_status != 200
        or history_one_status != 200
        or history_two_status != 200
        or not isinstance(latest, dict)
        or not isinstance(history_one, dict)
        or not isinstance(history_two, dict)
        or latest.get("revision") != 2
        or history_one.get("source_sha256") != definition.get("source_sha256_one")
        or history_two.get("source_sha256") != definition.get("source_sha256_two")
        or latest_headers.get("etag") != history_two_headers.get("etag")
    ):
        raise SmokeAbort("FAIL", "restart_saved_definition_history_mismatch")
    _names, reads = _mcp_exchange(
        mcp_port=context.ports[0],
        mcp_token=context.mcp_token,
        calls=(
            ("workflow_definitions_list", {"limit": 20}),
            (
                "workflow_definition_get",
                {"definition_id": definition["definition_id"], "revision": 1},
            ),
            (
                "workflow_definition_get",
                {"definition_id": definition["definition_id"], "revision": 2},
            ),
            ("workflow_graph_get", {"definition_id": definition["definition_id"], "revision": 2}),
            ("workflow_runs_list", {"limit": 50}),
            ("workflow_run_get", {"workflow_id": run["accepted"]["workflow_id"]}),
            ("workflow_run_result", {"workflow_id": run["accepted"]["workflow_id"]}),
        ),
    )
    mcp_def_list, mcp_one, mcp_two, mcp_graph, mcp_run_list, mcp_run_detail, mcp_result = reads
    if (
        mcp_one.get("source_sha256") != definition.get("source_sha256_one")
        or mcp_two.get("source_sha256") != definition.get("source_sha256_two")
        or mcp_graph.get("revision") != 2
        or mcp_run_detail.get("workflow_id") != run["accepted"]["workflow_id"]
        or mcp_result != run["result"]
        or not isinstance(mcp_def_list.get("definitions"), list)
        or not isinstance(mcp_run_list.get("workflows"), list)
    ):
        raise SmokeAbort("FAIL", "restart_mcp_saved_state_or_result_mismatch")
    http_list_item, http_detail, http_result, _ = _read_public_run_views(
        context, run["accepted"]["workflow_id"]
    )
    if http_result != run["result"] or http_detail.get("status") != "succeeded":
        raise SmokeAbort("FAIL", "restart_control_result_mismatch")
    _qt_readback(context, http_detail, http_result)
    after_rows = isolated._read_request_ledger(context.state_root)
    after_job = _verify_durable_run(
        context,
        request=run["request"],
        accepted=run["accepted"],
        details=http_detail,
        api_result=http_result,
        definition=definition,
    )
    if (
        len(after_rows) != len(first_rows)
        or after_rows[0].get("workflow_id") != first_rows[0].get("workflow_id")
        or after_job.get("job_count") != first_job.get("job_count")
        or isolated._worker_process_count(context.process.pid) != 0
    ):
        raise SmokeAbort("FAIL", "restart_readback_mutated_run_or_started_worker")
    gate["checks"] = {
        "saved_revision_history": "PASS",
        "mcp_http_run_readback": "PASS",
        "qt_offscreen_readback": "PASS",
        "request_and_job_ledgers_unchanged": "PASS",
        "restart_worker_count": 0,
        "workflow_start_posts_on_restart": 0,
    }
    if context.identity is None:
        raise SmokeAbort("BLOCKED", "restart_bridge_identity_missing")
    shutdown = stop_owned_bridge(
        context.process,
        context.identity,
        ui_port=context.ports[1],
        control_token=context.control_token,
    )
    evidence["processes"]["restart_bridge"] = {"identity_verified": True, **shutdown}
    gate["status"] = "PASS"
    _safe_write(writer, evidence, secrets_to_reject=(context.control_token, context.mcp_token))


def _record_failure(evidence: dict[str, Any], gate_name: str, error: SmokeAbort) -> None:
    evidence["reason"] = error.code
    gate = evidence.get(gate_name)
    if isinstance(gate, dict) and gate.get("status") == "NOT_RUN":
        gate["status"] = error.status
        gate["reason"] = error.code
    elif isinstance(gate, dict) and gate.get("status") == "PASS":
        gate["status"] = error.status
        gate["reason"] = error.code


def _safe_exception_diagnostic(error: Exception) -> dict[str, Any]:
    frames = traceback.extract_tb(error.__traceback__)
    diagnostic: dict[str, Any] = {"exception_type": type(error).__name__}
    if frames:
        diagnostic["function"] = frames[-1].name
        diagnostic["line"] = frames[-1].lineno
    return diagnostic


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot isolated Phase B5 Workflow acceptance")
    parser.parse_args(argv)
    attempt_id = uuid.uuid4().hex
    evidence_path = evidence_path_for_attempt(REPOSITORY_ROOT, attempt_id)
    evidence = _new_evidence(attempt_id)
    try:
        writer = phase5f5._EvidenceWriter(evidence_path, evidence)
    except Exception:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": "evidence_path_could_not_be_claimed"},
                sort_keys=True,
            )
        )
        return 1

    context: IsolatedContext | None = None
    active_gate = "offline_gate"
    tokens: tuple[str, ...] = ()
    try:
        codex_executable, source_home = _run_offline_gate(evidence, writer)
        if evidence["offline_gate"]["status"] != "PASS":
            if evidence["offline_gate"]["status"] == "NOT_RUN":
                evidence["offline_gate"]["status"] = "BLOCKED"
            raise SmokeAbort(
                str(evidence["offline_gate"]["status"]),
                str(evidence["offline_gate"].get("reason", "offline_gate_not_passed")),
            )
        if codex_executable is None or source_home is None:
            evidence["offline_gate"]["status"] = "BLOCKED"
            evidence["offline_gate"]["reason"] = "codex_cli_or_auth_unavailable"
            raise SmokeAbort("BLOCKED", "codex_cli_or_auth_unavailable")

        context = _make_isolated_context(codex_executable, source_home)
        tokens = (context.control_token, context.mcp_token)
        evidence["isolation"] = {
            "status": "PASS",
            "temp_root_label": context.temp_root.name,
            "workspace_label": "workspace",
            "isolated_state_empty": True,
            "isolated_auth_copy_verified": True,
            "distinct_control_and_mcp_tokens": True,
            "lan_disabled": True,
            "github_mcp_disabled": True,
            "production_defaults_18000_18001_avoided": True,
            "state_and_workspace_separate": True,
        }
        evidence["ports"] = {
            "initial_random_loopback_pair": [context.ports[0], context.ports[1]],
            "production_defaults_avoided": True,
        }
        source_path = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"
        base_yaml = build_fixed_workflow(source_path.read_text(encoding="utf-8"))
        revision_two_yaml = add_comment_revision(base_yaml)
        if source_sha256(base_yaml) == source_sha256(revision_two_yaml) or canonical_sha256(
            base_yaml
        ) != canonical_sha256(revision_two_yaml):
            raise SmokeAbort("BLOCKED", "comment_only_revision_preflight_failed")
        active_gate = "mcp_control_gate"
        evidence["processes"]["initial_bridge"] = _start_bridge(context)
        definition = _run_mcp_control_gate(
            context,
            evidence,
            writer,
            base_yaml=base_yaml,
            revision_two_yaml=revision_two_yaml,
        )
        active_gate = "saved_execution_gate"
        run = _run_execution_gate(context, evidence, writer, definition=definition)
        if context.process is None or context.identity is None:
            raise SmokeAbort("BLOCKED", "initial_bridge_identity_missing")
        active_gate = "restart_gate"
        _run_restart_gate(context, evidence, writer, run=run, definition=definition)
        gate_statuses = (
            evidence["offline_gate"].get("status"),
            evidence["mcp_control_gate"].get("status"),
            evidence["saved_execution_gate"].get("status"),
            evidence["restart_gate"].get("status"),
        )
        if all(status == "PASS" for status in gate_statuses):
            evidence["status"] = "PASS"
            evidence["reason"] = None
        else:
            evidence["status"] = "BLOCKED"
            evidence["reason"] = "one_or_more_acceptance_gates_not_passed"
    except SmokeAbort as error:
        if (
            active_gate == "saved_execution_gate"
            and context is not None
            and context.active_workflow_request is not None
            and evidence["saved_execution_gate"]["posts"]["initial_http_workflow_posts"] == 1
        ):
            saved_gate = evidence["saved_execution_gate"]
            if saved_gate.get("result_gate") != "PASS":
                saved_gate["result_gate"] = "NOT_OBSERVED"
            if "unknown_outcome_diagnostic" not in saved_gate:
                try:
                    _record_read_only_post_diagnostic(
                        context, context.active_workflow_request, evidence
                    )
                    _safe_write(writer, evidence, secrets_to_reject=tokens)
                except SmokeAbort:
                    saved_gate["unknown_outcome_diagnostic"] = {
                        "status": "NOT_OBSERVED",
                        "method": "sqlite_read_only_and_run_artifacts",
                        "reason": "read_only_diagnostic_unavailable",
                    }
        _record_failure(evidence, active_gate, error)
        evidence["status"] = error.status
        evidence["reason"] = error.code
    except Exception as error:
        if (
            active_gate == "saved_execution_gate"
            and context is not None
            and context.active_workflow_request is not None
            and evidence["saved_execution_gate"]["posts"]["initial_http_workflow_posts"] == 1
        ):
            saved_gate = evidence["saved_execution_gate"]
            if saved_gate.get("result_gate") != "PASS":
                saved_gate["result_gate"] = "NOT_OBSERVED"
            if "unknown_outcome_diagnostic" not in saved_gate:
                try:
                    _record_read_only_post_diagnostic(
                        context, context.active_workflow_request, evidence
                    )
                    _safe_write(writer, evidence, secrets_to_reject=tokens)
                except Exception:
                    saved_gate["unknown_outcome_diagnostic"] = {
                        "status": "NOT_OBSERVED",
                        "method": "sqlite_read_only_and_run_artifacts",
                        "reason": "read_only_diagnostic_unavailable",
                    }
        safe_code = type(error).__name__
        evidence["failure_diagnostic"] = _safe_exception_diagnostic(error)
        _record_failure(evidence, active_gate, SmokeAbort("BLOCKED", safe_code))
        evidence["status"] = "BLOCKED"
        evidence["reason"] = safe_code
    finally:
        if context is not None and context.active_worker_observer is not None:
            try:
                context.active_worker_observer.finish()
            except Exception:
                evidence["processes"]["worker_observer"] = "NOT_OBSERVED"
                evidence["status"] = "BLOCKED"
                evidence["reason"] = "worker_observer_cleanup_not_verified"
            finally:
                context.active_worker_observer = None
        if context is not None and context.process is not None:
            if context.process.poll() is None and context.identity is None:
                try:
                    context.identity = phase5f5._capture_bridge_identity(context.process)
                except Exception:
                    evidence["processes"]["cleanup"] = "NOT_OBSERVED"
                    evidence["status"] = "BLOCKED"
                    evidence["reason"] = "owned_process_identity_unavailable_for_cleanup"
            if context.process.poll() is None and context.identity is not None:
                try:
                    shutdown = stop_owned_bridge(
                        context.process,
                        context.identity,
                        ui_port=context.ports[1],
                        control_token=context.control_token,
                    )
                    key = (
                        "restart_bridge"
                        if evidence["processes"].get("restart_bridge") != "NOT_RUN"
                        else "initial_bridge"
                    )
                    evidence["processes"][key] = {"identity_verified": True, **shutdown}
                except Exception:
                    evidence["processes"]["cleanup"] = "NOT_OBSERVED"
                    evidence["status"] = "BLOCKED"
                    evidence["reason"] = "owned_process_cleanup_not_verified"
            elif context.process.poll() is not None:
                try:
                    context.process.wait(timeout=0)
                    evidence["processes"]["cleanup"] = {
                        "reaped": True,
                        "forced_termination": False,
                    }
                except (OSError, subprocess.TimeoutExpired):
                    evidence["processes"]["cleanup"] = "NOT_OBSERVED"
                    evidence["status"] = "BLOCKED"
                    evidence["reason"] = "owned_process_reap_not_verified"
        evidence["finished_at_utc"] = _utc_now()
        try:
            _safe_write(writer, evidence, secrets_to_reject=tokens)
        except Exception:
            evidence["status"] = "BLOCKED"
            evidence["reason"] = "evidence_final_write_failed"
    summary = {
        "status": evidence.get("status"),
        "reason": evidence.get("reason"),
        "attempt_id": attempt_id,
        "evidence": str(evidence_path.relative_to(REPOSITORY_ROOT)),
        "initial_http_posts": evidence["saved_execution_gate"]["posts"][
            "initial_http_workflow_posts"
        ],
        "unique_initial_request_ids": evidence["saved_execution_gate"]["posts"][
            "unique_initial_client_request_ids"
        ],
        "same_id_mcp_replays": evidence["saved_execution_gate"]["posts"][
            "same_id_mcp_replay_calls"
        ],
        "same_id_conflict_http_posts": evidence["saved_execution_gate"]["posts"][
            "same_id_conflict_http_posts"
        ],
        "actual_codex_jobs": evidence["saved_execution_gate"]["posts"]["actual_codex_jobs"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if evidence.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
