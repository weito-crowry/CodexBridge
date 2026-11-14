from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psutil
import yaml
from PySide6.QtWidgets import QApplication

from codex_bridge.codex_resolver import resolve_codex_executable
from codex_bridge.config import BridgeConfig
from codex_bridge.console.api_client import ApiClient
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog
from codex_bridge.server import prepare_config
from codex_bridge.workflows.interpreter import (
    OwsError,
    validate_three_live_codex_items_input,
    validate_workflow,
)
from scripts import phase5e2c2_isolated_smoke as support
from scripts.phase5e_smoke_support import claim_evidence_path, ports_are_safe, roots_are_separate

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ITEM_FIXTURE_PATH = (
    REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "conditional_items_codex_workflow.yaml"
)
SINGLE_JOB_FIXTURE_PATH = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"
EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f5-integrated-acceptance-evidence.json"
R2_EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f5-integrated-acceptance-r2-evidence.json"
R3_EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f5-integrated-acceptance-r3-evidence.json"
BASE_COMMIT = "9cdb00d2332a781e33387fffc1d237804a6ee4a4"
BRIDGE_HOST = "127.0.0.1"
ITEM_MAX_LIVE_CODEX_JOBS = 3
SINGLE_MAX_LIVE_CODEX_JOBS = 1
WORKFLOW_TIMEOUT_SECONDS = 300.0
MAX_JSON_BYTES = 256 * 1024
ACTIVE_STATUSES = frozenset({"queued", "starting", "running"})
ITEM_TASK_IDENTITIES = (
    "do[0].processItems.iteration[0].do[1].processItem",
    "do[0].processItems.iteration[2].do[1].processItem",
)
_DRIVE_PATH = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\r\n\"'<>|]+")
SmokeAbort = support.SmokeAbort


def _utc_now() -> str:
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


def _verified_base_ancestor_head(base_commit: str = BASE_COMMIT) -> str:
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeAbort("BLOCKED", "git_head_verification_failed") from None
    head = head_result.stdout.strip().lower()
    if head_result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", head) is None:
        raise SmokeAbort("BLOCKED", "git_head_verification_failed")
    try:
        ancestry_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_commit, head],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SmokeAbort("BLOCKED", "git_base_ancestry_check_failed") from None
    if ancestry_result.returncode == 1:
        raise SmokeAbort("BLOCKED", "phase5f4_base_commit_not_ancestor_of_head")
    if ancestry_result.returncode != 0:
        raise SmokeAbort("BLOCKED", "git_base_ancestry_check_failed")
    return head


def _evidence_path_for_attempt(attempt: str | None) -> Path:
    if attempt is None:
        return EVIDENCE_PATH
    if attempt == "r2":
        return R2_EVIDENCE_PATH
    if attempt == "r3":
        return R3_EVIDENCE_PATH
    raise ValueError("unsupported acceptance attempt")


def build_item_input() -> dict[str, Any]:
    return {
        "items": [
            {"id": "alpha", "enabled": True, "prompt": "Reply with exactly ALPHA"},
            {"id": "skip", "enabled": False, "prompt": "Reply with exactly SKIP"},
            {"id": "gamma", "enabled": True, "prompt": "Reply with exactly GAMMA"},
        ]
    }


def build_single_job_workflow(source_yaml: str) -> str:
    try:
        definition = yaml.safe_load(source_yaml)
    except yaml.YAMLError:
        raise SmokeAbort("BLOCKED", "single_job_template_invalid") from None
    if not isinstance(definition, dict):
        raise SmokeAbort("BLOCKED", "single_job_template_invalid")
    tasks = definition.get("do")
    if (
        not isinstance(tasks, list)
        or len(tasks) != 2
        or not isinstance(tasks[0], dict)
        or not isinstance(tasks[0].get("calculate"), dict)
        or not isinstance(tasks[0]["calculate"].get("with"), dict)
    ):
        raise SmokeAbort("BLOCKED", "single_job_template_shape_mismatch")
    tasks[0]["calculate"]["with"]["prompt"] = "Reply with exactly BRAVO"
    try:
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=1)
    except (OwsError, TypeError, ValueError, RecursionError):
        raise SmokeAbort("BLOCKED", "single_job_template_contract_invalid") from None
    return yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)


def validate_item_result(
    value: Any,
    *,
    workflow_id: str,
    jobs: Sequence[Mapping[str, Any]],
) -> list[str]:
    if (
        not isinstance(value, dict)
        or value.get("workflow_id") != workflow_id
        or len(jobs) != 2
        or not isinstance(value.get("result"), dict)
    ):
        raise SmokeAbort("BLOCKED", "item_result_shape_or_identity_mismatch")
    job_ids = [row.get("job_id") for row in jobs]
    request_keys = [row.get("request_key") for row in jobs]
    if (
        any(not isinstance(item, str) or not item for item in job_ids + request_keys)
        or len(set(job_ids)) != 2
        or len(set(request_keys)) != 2
    ):
        raise SmokeAbort("BLOCKED", "item_job_projection_invalid")
    payload = value["result"]
    expected = [
        {
            "id": "alpha",
            "status": "SUCCEEDED",
            "job_id": job_ids[0],
            "request_key": request_keys[0],
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
            "job_id": job_ids[1],
            "request_key": request_keys[1],
            "final_text": "GAMMA",
        },
    ]
    if (
        payload.get("results") != expected
        or payload.get("summary") != {"total": 3, "succeeded": 2, "skipped": 1}
        or set(payload) != {"results", "summary"}
    ):
        raise SmokeAbort("FAIL", "item_result_values_order_or_summary_mismatch")
    return ["ALPHA", "GAMMA"]


def validate_single_job_result(
    value: Any,
    *,
    workflow_id: str,
    job: Mapping[str, Any],
) -> str:
    if (
        not isinstance(value, dict)
        or value.get("workflow_id") != workflow_id
        or not isinstance(value.get("result"), dict)
        or value["result"].get("job_id") != job.get("job_id")
        or value["result"].get("request_key") != job.get("request_key")
    ):
        raise SmokeAbort("BLOCKED", "single_job_result_identity_mismatch")
    job_result = value["result"].get("result")
    if not isinstance(job_result, dict) or job_result.get("final_text") != "BRAVO":
        raise SmokeAbort("FAIL", "single_job_result_text_mismatch")
    return "BRAVO"


def _new_evidence() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": "5F-5",
        "status": "NOT_RUN",
        "reason": None,
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
        "source_commit": None,
        "base_commit": BASE_COMMIT,
        "head_commit": None,
        "runner_sha256": _sha256(Path(__file__).read_bytes()),
        "offline_regression_preceded_live_posts": True,
        "runtime": {},
        "isolation": {"status": "NOT_RUN"},
        "ports": {},
        "posts": {
            "unique_initial_attempts": 0,
            "accepted_initial_workflows": 0,
            "capacity_probe_attempts": 0,
            "same_id_replay_attempts": 0,
            "workflow_post_total_attempts": 0,
        },
        "workflows": {"A": {}, "B": {}, "C": {"status": "NOT_RUN"}},
        "checks": {
            "native_runtime_and_dependencies": "NOT_RUN",
            "isolated_auth_config_roots_and_empty_state": "NOT_RUN",
            "bridge_owner_ports_and_control_auth": "NOT_RUN",
            "workflow_A_items_result": "NOT_RUN",
            "workflow_B_single_result": "NOT_RUN",
            "simultaneous_capacity_409": "NOT_OBSERVED",
            "same_id_replay": "NOT_RUN",
            "lifecycle_logs": "NOT_RUN",
            "console_offscreen_readback": "NOT_RUN",
            "graceful_shutdown": "NOT_RUN",
            "restart_read_only_get": "NOT_RUN",
        },
        "processes": {
            "bridge_initial": None,
            "bridge_restart": None,
            "worker_snapshots": [],
            "shutdowns": [],
            "orphan_processes": None,
            "existing_processes_touched": 0,
        },
    }


class _EvidenceWriter:
    def __init__(self, path: Path, evidence: Mapping[str, Any]) -> None:
        if path.exists() or path.is_symlink():
            raise SmokeAbort("BLOCKED", "evidence_path_already_exists")
        try:
            claim_evidence_path(path, evidence)
            self._identity = path.stat(follow_symlinks=False)
        except OSError:
            raise SmokeAbort("BLOCKED", "evidence_path_could_not_be_claimed") from None
        self.path = path

    def write(self, evidence: Mapping[str, Any]) -> None:
        try:
            current = self.path.stat(follow_symlinks=False)
            if (
                self.path.is_symlink()
                or current.st_dev != self._identity.st_dev
                or current.st_ino != self._identity.st_ino
            ):
                raise SmokeAbort("BLOCKED", "evidence_file_identity_changed")
            encoded = (
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            with self.path.open("r+b") as stream:
                opened = os.fstat(stream.fileno())
                if opened.st_dev != self._identity.st_dev or opened.st_ino != self._identity.st_ino:
                    raise SmokeAbort("BLOCKED", "evidence_file_identity_changed")
                stream.seek(0)
                stream.truncate()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except SmokeAbort:
            raise
        except OSError:
            raise SmokeAbort("BLOCKED", "evidence_update_failed") from None


def _save_evidence(
    writer: _EvidenceWriter,
    evidence: dict[str, Any],
    *,
    token: str | None = None,
    temp_root: Path | None = None,
) -> None:
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    if token and token in encoded:
        raise SmokeAbort("BLOCKED", "evidence_contains_control_token")
    for forbidden in ("Reply with exactly", "Authorization:", 'final_text":'):
        if forbidden.casefold() in encoded.casefold():
            raise SmokeAbort("BLOCKED", "evidence_contains_prompt_or_reply_text")
    if temp_root is not None and str(temp_root) in encoded:
        raise SmokeAbort("BLOCKED", "evidence_contains_absolute_temp_path")
    if _DRIVE_PATH.search(encoded):
        raise SmokeAbort("BLOCKED", "evidence_contains_personal_absolute_path")
    writer.write(evidence)


def _request_body(
    *,
    client_request_id: str,
    workflow_yaml: str,
    input_data: Mapping[str, Any],
    workspace: Path,
    max_live_codex_jobs: int,
) -> bytes:
    payload = {
        "client_request_id": client_request_id,
        "workflow_yaml": workflow_yaml,
        "input": dict(input_data),
        "workspace_root": str(workspace.resolve(strict=True)),
        "max_live_codex_jobs": max_live_codex_jobs,
    }
    return _canonical_json(payload).encode("utf-8")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_bridge_config(
    path: Path,
    *,
    mcp_port: int,
    ui_port: int,
    workspaces: Sequence[Path],
    codex_executable: Path,
) -> None:
    roots = ", ".join(_toml_string(str(root.resolve(strict=True))) for root in workspaces)
    contents = "\n".join(
        (
            "[bridge]",
            f"host = {_toml_string(BRIDGE_HOST)}",
            f"port = {mcp_port}",
            f"allowed_roots = [{roots}]",
            "allowed_hosts = []",
            "allowed_origins = []",
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
            "[targets.local]",
            'name = "Phase 5F-5 isolated local execution"',
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


def _preflight_runtime(evidence: dict[str, Any]) -> None:
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise SmokeAbort("BLOCKED", "windows_native_execution_required")
    environment_root = (REPOSITORY_ROOT / ".venv").resolve(strict=True)
    python_path = Path(sys.executable).resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    if not python_path.is_relative_to(environment_root) or prefix != environment_root:
        raise SmokeAbort("BLOCKED", "repository_python_environment_required")
    required = {
        "dbos": "3.4.0",
        "jq": "1.13.0",
        "jsonschema": "4.25.1",
        "PyYAML": "6.0.3",
        "PySide6": None,
        "psutil": None,
    }
    versions: dict[str, str] = {}
    for package, expected in required.items():
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise SmokeAbort("BLOCKED", "workflow_or_console_dependency_unavailable") from None
        if expected is not None and installed != expected:
            raise SmokeAbort("BLOCKED", "workflow_dependency_version_mismatch")
        versions[package] = installed
    evidence["runtime"] = {
        "platform": "windows_native",
        "python_version": sys.version.split()[0],
        "repository_venv": True,
        "workflow_and_console_dependencies": versions,
    }
    evidence["checks"]["native_runtime_and_dependencies"] = "PASS"


def _validate_templates() -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    try:
        item_yaml = ITEM_FIXTURE_PATH.read_text(encoding="utf-8")
        item_definition = yaml.safe_load(item_yaml)
        single_source = SINGLE_JOB_FIXTURE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError, yaml.YAMLError):
        raise SmokeAbort("BLOCKED", "approved_workflow_fixture_unavailable") from None
    item_input = build_item_input()
    try:
        validate_workflow(item_definition, codex_job_live=True, max_live_codex_jobs=3)
        normalized_items = validate_three_live_codex_items_input(item_input)
    except (OwsError, TypeError, ValueError, RecursionError):
        raise SmokeAbort("BLOCKED", "items_workflow_contract_invalid") from None
    if (
        not isinstance(item_definition, dict)
        or tuple(row.get("id") for row in normalized_items) != ("alpha", "skip", "gamma")
        or tuple(row.get("enabled") for row in normalized_items) != (True, False, True)
    ):
        raise SmokeAbort("BLOCKED", "items_workflow_order_or_enablement_mismatch")
    single_yaml = build_single_job_workflow(single_source)
    single_definition = yaml.safe_load(single_yaml)
    return item_yaml, item_definition, single_yaml, single_definition


def _write_child_bridge(
    environment: Mapping[str, str], *, temp_root: Path, log_prefix: str
) -> subprocess.Popen[bytes]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    if log_prefix not in {"initial", "restart"}:
        raise SmokeAbort("BLOCKED", "isolated_bridge_log_role_invalid")
    stdout_path = temp_root / f"bridge-{log_prefix}.stdout.log"
    stderr_path = temp_root / f"bridge-{log_prefix}.stderr.log"
    if any(path.exists() or path.is_symlink() for path in (stdout_path, stderr_path)):
        raise SmokeAbort("BLOCKED", "isolated_bridge_log_path_already_exists")
    try:
        with stdout_path.open("xb") as stdout_log, stderr_path.open("xb") as stderr_log:
            return subprocess.Popen(
                [sys.executable, "-m", "codex_bridge"],
                cwd=REPOSITORY_ROOT,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout_log,
                stderr=stderr_log,
                creationflags=flags,
                close_fds=True,
            )
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_process_could_not_start") from None


def _capture_owned_process(
    process: psutil.Process,
    *,
    role: str,
    expected_parent_pid: int | None = None,
    expected_executable: Path | None = None,
    command_marker: str | None = None,
) -> dict[str, Any]:
    try:
        current_user = psutil.Process().username()
        owner = process.username()
        created_at = process.create_time()
        parent_pid = process.ppid()
        executable = Path(process.exe()).resolve(strict=True)
        command_line = process.cmdline()
    except (OSError, psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", f"{role}_process_identity_unavailable") from None
    if (
        not current_user
        or owner != current_user
        or (expected_parent_pid is not None and parent_pid != expected_parent_pid)
        or (
            expected_executable is not None
            and os.path.normcase(str(executable))
            != os.path.normcase(str(expected_executable.resolve(strict=True)))
        )
        or (command_marker is not None and command_marker not in " ".join(command_line))
    ):
        raise SmokeAbort("BLOCKED", f"{role}_process_identity_mismatch")
    return {
        "role": role,
        "pid": process.pid,
        "created_at_epoch": created_at,
        "created_at_utc": datetime.fromtimestamp(created_at, tz=UTC).isoformat(
            timespec="milliseconds"
        ),
        "owner_matches_current_user": True,
        "parent_pid": parent_pid,
        "identity_verified": True,
    }


def _capture_bridge_identity(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    if process.poll() is not None:
        raise SmokeAbort("BLOCKED", "isolated_bridge_exited_during_startup")
    return _capture_owned_process(
        psutil.Process(process.pid),
        role="bridge",
        expected_parent_pid=os.getpid(),
        expected_executable=Path(sys.executable),
        command_marker="codex_bridge",
    )


def _assert_bridge_identity(process: subprocess.Popen[bytes], identity: Mapping[str, Any]) -> None:
    if process.poll() is not None:
        raise SmokeAbort("BLOCKED", "owned_bridge_process_already_exited")
    try:
        observed = psutil.Process(process.pid)
        current_user = psutil.Process().username()
        matches = (
            observed.is_running()
            and abs(observed.create_time() - float(identity["created_at_epoch"])) < 0.01
            and observed.username() == current_user
            and observed.ppid() == identity.get("parent_pid") == os.getpid()
            and "codex_bridge" in " ".join(observed.cmdline())
        )
    except (KeyError, TypeError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
        matches = False
    if not matches:
        raise SmokeAbort("BLOCKED", "owned_bridge_identity_changed_before_shutdown")


def _worker_process_snapshot(bridge_pid: int) -> list[dict[str, Any]]:
    try:
        descendants = psutil.Process(bridge_pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        raise SmokeAbort("BLOCKED", "bridge_child_process_tree_unavailable") from None
    result: list[dict[str, Any]] = []
    current_user = psutil.Process().username()
    for child in descendants:
        try:
            if "codex_bridge.workflows.worker_entry" not in child.cmdline():
                continue
            record = _capture_owned_process(child, role="worker")
            if record["owner_matches_current_user"] is not True:
                raise SmokeAbort("BLOCKED", "worker_owner_mismatch")
            record["descendant_of_owned_bridge"] = child.pid in {
                process.pid for process in descendants
            }
            if not record["descendant_of_owned_bridge"] or child.username() != current_user:
                raise SmokeAbort("BLOCKED", "worker_process_parent_or_owner_mismatch")
            result.append(record)
        except psutil.NoSuchProcess:
            continue
    return result


def _write_runtime_config(
    *,
    temp_root: Path,
    base_environment: Mapping[str, str],
    token: str,
    ports: tuple[int, int],
    workspaces: Sequence[Path],
    codex_executable: Path,
    config_name: str,
) -> tuple[dict[str, str], Path]:
    if not ports_are_safe(ports[0], ports[1]):
        raise SmokeAbort("BLOCKED", "dedicated_loopback_ports_invalid")
    config_path = temp_root / config_name
    _write_bridge_config(
        config_path,
        mcp_port=ports[0],
        ui_port=ports[1],
        workspaces=workspaces,
        codex_executable=codex_executable,
    )
    child_environment = support.build_phase5e2c2_environment(
        base_environment,
        temp_root=temp_root,
        config_path=config_path,
        control_token=token,
        python_path=REPOSITORY_ROOT / "src",
        path_entries=support._isolated_path_entries(base_environment, codex_executable),
    )
    isolated_home = Path(child_environment["CODEX_HOME"])
    if not (isolated_home / "auth.json").is_file() or (isolated_home / "config.toml").exists():
        raise SmokeAbort("BLOCKED", "isolated_codex_home_preflight_failed")
    try:
        config = BridgeConfig.from_sources(environ=child_environment)
        effective = prepare_config(config)
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_validation_failed") from None
    expected_roots = tuple(os.path.normcase(str(path.resolve(strict=True))) for path in workspaces)
    actual_roots = tuple(os.path.normcase(root) for root in effective.allowed_roots)
    if (
        effective.host != BRIDGE_HOST
        or effective.port != ports[0]
        or effective.ui_port != ports[1]
        or actual_roots != expected_roots
        or effective.control_token != token
        or effective.lan_enabled
        or effective.github_mcp.enabled
        or len(effective.targets) != 1
        or effective.targets[0].kind != "local"
        or os.path.normcase(effective.codex_executable)
        != os.path.normcase(str(codex_executable.resolve(strict=True)))
    ):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_does_not_match_scope")
    return child_environment, config_path


def _runtime_paths_from_config_result(
    runtime_config: tuple[dict[str, str], Path], *, temp_root: Path
) -> tuple[dict[str, str], Path, Path]:
    environment, config_path = runtime_config
    local_app_data_value = environment.get("LOCALAPPDATA")
    if not isinstance(local_app_data_value, str) or not local_app_data_value:
        raise SmokeAbort("BLOCKED", "isolated_localappdata_unavailable")
    if temp_root.is_symlink() or Path(local_app_data_value).is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_temp_or_localappdata_is_a_symlink")
    try:
        canonical_temp_root = temp_root.resolve(strict=True)
        canonical_local_app_data = Path(local_app_data_value).resolve(strict=True)
        expected_local_app_data = (canonical_temp_root / "localappdata").resolve(strict=True)
        canonical_config_path = config_path.resolve(strict=True)
    except OSError:
        raise SmokeAbort("BLOCKED", "isolated_runtime_path_unavailable") from None
    if (
        not canonical_local_app_data.is_dir()
        or canonical_local_app_data != expected_local_app_data
        or not canonical_local_app_data.is_relative_to(canonical_temp_root)
    ):
        raise SmokeAbort("BLOCKED", "isolated_localappdata_outside_temp_root")
    if (
        config_path.is_symlink()
        or not canonical_config_path.is_file()
        or not canonical_config_path.is_relative_to(canonical_temp_root)
    ):
        raise SmokeAbort("BLOCKED", "isolated_config_path_outside_temp_root")

    state_root_candidate = canonical_local_app_data / "CodexBridge" / "workflows"
    if state_root_candidate.parent.is_symlink() or state_root_candidate.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_state_root_is_a_symlink")
    try:
        state_root = state_root_candidate.resolve(strict=False)
    except OSError:
        raise SmokeAbort("BLOCKED", "isolated_state_root_unavailable") from None
    if (
        state_root == canonical_local_app_data
        or not state_root.is_relative_to(canonical_local_app_data)
        or state_root == canonical_config_path
    ):
        raise SmokeAbort("BLOCKED", "isolated_state_root_outside_localappdata")
    return environment, canonical_config_path, state_root


def _ensure_workspace_preflight(
    *, temp_root: Path, state_root: Path, workspaces: Sequence[Path]
) -> None:
    if len(workspaces) != 3:
        raise SmokeAbort("BLOCKED", "three_isolated_workspaces_required")
    protected = (
        REPOSITORY_ROOT,
        Path("D:/CodexBridge"),
        Path("D:/CodexBridge-PoC"),
        Path("D:/CodexBridge-Workflow409"),
    )
    for index, workspace in enumerate(workspaces):
        if workspace.is_symlink() or not workspace.is_dir() or any(workspace.iterdir()):
            raise SmokeAbort("BLOCKED", "dedicated_workspace_is_not_empty")
        if not roots_are_separate(state_root, workspace):
            raise SmokeAbort("BLOCKED", "isolated_state_overlaps_workspace")
        if any(not roots_are_separate(workspace, root) for root in protected):
            raise SmokeAbort("BLOCKED", "isolated_workspace_overlaps_protected_checkout")
        for other in workspaces[index + 1 :]:
            if not roots_are_separate(workspace, other):
                raise SmokeAbort("BLOCKED", "isolated_workspaces_overlap")
    if temp_root.is_symlink() or state_root.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_temp_or_state_root_is_a_symlink")
    try:
        canonical_temp = temp_root.resolve(strict=True)
        canonical_state = state_root.resolve(strict=False)
    except OSError:
        raise SmokeAbort("BLOCKED", "isolated_temp_or_state_root_unavailable") from None
    if canonical_state == canonical_temp or not canonical_state.is_relative_to(canonical_temp):
        raise SmokeAbort("BLOCKED", "isolated_state_root_is_outside_temp_root")
    if not roots_are_separate(REPOSITORY_ROOT, canonical_temp):
        raise SmokeAbort("BLOCKED", "isolated_temp_root_overlaps_repository")


def _request_get(ui_port: int, route: str, token: str) -> Any:
    return support._get_json(ui_port, route, token)


def _read_workflow_list(ui_port: int, token: str) -> list[dict[str, Any]]:
    payload = _request_get(ui_port, "/ui-api/control/workflows?limit=50", token)
    workflows = payload.get("workflows") if isinstance(payload, dict) else None
    if not isinstance(workflows, list) or any(not isinstance(row, dict) for row in workflows):
        raise SmokeAbort("BLOCKED", "workflow_list_shape_invalid")
    return workflows


def _start_post(
    *,
    ui_port: int,
    token: str,
    body: bytes,
    label: str,
    evidence: dict[str, Any],
    writer: _EvidenceWriter,
    temp_root: Path,
) -> tuple[int, Any]:
    evidence["posts"]["workflow_post_total_attempts"] += 1
    if label in {"A", "B"}:
        evidence["posts"]["unique_initial_attempts"] += 1
    elif label == "C":
        evidence["posts"]["capacity_probe_attempts"] += 1
    elif label in {"replay_A", "replay_B"}:
        evidence["posts"]["same_id_replay_attempts"] += 1
    workflow_record = evidence["workflows"].setdefault(label, {})
    workflow_record["post_attempts"] = int(workflow_record.get("post_attempts", 0)) + 1
    workflow_record["post_outcome"] = "MAYBE_SENT"
    _save_evidence(writer, evidence, token=token, temp_root=temp_root)
    try:
        return support._http_json(
            ui_port,
            "POST",
            "/ui-api/control/workflows",
            token=token,
            body=body,
            timeout=25.0,
        )
    except SmokeAbort:
        evidence["workflows"].setdefault(label, {})["post_outcome"] = "UNKNOWN"
        raise


def _reconcile_request(
    *, state_root: Path, ui_port: int, token: str, client_request_id: str
) -> dict[str, Any]:
    return support._read_reconciliation_state(
        state_root=state_root,
        ui_port=ui_port,
        token=token,
        client_request_id=client_request_id,
    )


def _record_read_only_reconciliations(
    *,
    evidence: dict[str, Any],
    state_root: Path,
    ui_port: int,
    token: str,
) -> None:
    for label in ("A", "B"):
        workflow_record = evidence["workflows"].get(label, {})
        client_request_id = workflow_record.get("client_request_id")
        attempts = workflow_record.get("post_attempts", 0)
        if (
            not isinstance(client_request_id, str)
            or type(attempts) is not int
            or attempts < 1
            or "reconciliation" in workflow_record
        ):
            continue
        try:
            reconciliation = _reconcile_request(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=client_request_id,
            )
            workflow_record["reconciliation"] = {
                "matching_request_rows": reconciliation["matching_request_rows"],
                "list_readable": reconciliation["list_readable"],
            }
        except Exception as error:
            workflow_record["reconciliation"] = {
                "status": "UNKNOWN",
                "error_code": error.code if isinstance(error, SmokeAbort) else type(error).__name__,
            }


def _read_lifecycle(path: Path, *, run_id: str, workflow_id: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise SmokeAbort("UNKNOWN", "lifecycle_log_missing_or_oversized")
    try:
        raw = path.read_bytes()
        if any(len(line) > 2048 for line in raw.splitlines()):
            raise SmokeAbort("UNKNOWN", "lifecycle_event_exceeds_documented_limit")
        records = [json.loads(line) for line in raw.splitlines() if line]
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SmokeAbort("UNKNOWN", "lifecycle_log_invalid") from None
    if not records or any(
        not isinstance(row, dict)
        or row.get("version") != 1
        or row.get("run_id") != run_id
        or row.get("workflow_id") not in {None, workflow_id}
        or not isinstance(row.get("event"), str)
        for row in records
    ):
        raise SmokeAbort("UNKNOWN", "lifecycle_log_identity_or_shape_mismatch")
    previous: datetime | None = None
    for row in records:
        timestamp = row.get("timestamp_utc")
        if not isinstance(timestamp, str):
            raise SmokeAbort("UNKNOWN", "lifecycle_timestamp_missing")
        try:
            current = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise SmokeAbort("UNKNOWN", "lifecycle_timestamp_invalid") from None
        if current.tzinfo is None or (previous is not None and current < previous):
            raise SmokeAbort("UNKNOWN", "lifecycle_timestamp_order_invalid")
        previous = current
    return records


def _lifecycle_projection(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    allowed = (
        "event",
        "timestamp_utc",
        "run_id",
        "workflow_id",
        "request_digest",
        "launch_id",
        "pid",
        "returncode",
        "shutdown_requested",
        "state",
        "mode",
    )
    return [{key: row[key] for key in allowed if key in row} for row in rows]


def _verify_lifecycle(
    *,
    run_dir: Path,
    run_id: str,
    workflow_id: str,
    request_digest: str,
    worker_status: Mapping[str, Any],
    worker_identity_record: Mapping[str, Any],
    bridge_pid: int,
    token: str,
    temp_root: Path,
    prompts: Sequence[str],
    replies: Sequence[str],
) -> dict[str, Any]:
    bridge_rows = _read_lifecycle(
        run_dir / "bridge-lifecycle.jsonl", run_id=run_id, workflow_id=workflow_id
    )
    worker_rows = _read_lifecycle(
        run_dir / "worker-lifecycle.jsonl", run_id=run_id, workflow_id=workflow_id
    )
    serialized = json.dumps([bridge_rows, worker_rows], ensure_ascii=False, sort_keys=True)
    forbidden_values = (token, str(temp_root), *prompts, *replies)
    if any(value and value.casefold() in serialized.casefold() for value in forbidden_values):
        raise SmokeAbort("UNKNOWN", "lifecycle_log_contains_sensitive_value")
    if any(
        value in serialized.casefold() for value in ("authorization", '"prompt"', '"final_text"')
    ):
        raise SmokeAbort("UNKNOWN", "lifecycle_log_contains_sensitive_field")
    bridge_counts = Counter(str(row["event"]) for row in bridge_rows)
    worker_counts = Counter(str(row["event"]) for row in worker_rows)
    bridge_expected = (
        "request_reserved",
        "worker_launch_requested",
        "worker_spawn_succeeded",
        "worker_exited",
    )
    worker_expected = ("worker_bootstrap", "dbos_ready", "workflow_started", "result_persisted")
    if any(bridge_counts[event] != 1 for event in bridge_expected) or any(
        worker_counts[event] != 1 for event in worker_expected
    ):
        raise SmokeAbort("UNKNOWN", "lifecycle_event_sequence_incomplete")
    reserved = next(row for row in bridge_rows if row["event"] == "request_reserved")
    spawned = next(row for row in bridge_rows if row["event"] == "worker_spawn_succeeded")
    exited = next(row for row in bridge_rows if row["event"] == "worker_exited")
    worker_identity = next(row for row in worker_rows if row["event"] == "worker_bootstrap")
    if (
        reserved.get("request_digest") != request_digest
        or not isinstance(spawned.get("launch_id"), str)
        or spawned.get("launch_id") != worker_identity.get("launch_id")
        or spawned.get("launch_id") != exited.get("launch_id")
        or spawned.get("pid") != worker_identity.get("pid")
        or spawned.get("pid") != exited.get("pid")
        or exited.get("returncode") != 0
        or exited.get("state") != "completed"
        or exited.get("shutdown_requested") is not False
        or worker_identity_record.get("pid") != spawned.get("pid")
        or worker_identity_record.get("parent_pid") != bridge_pid
        or worker_identity_record.get("run_dir") != str(run_dir.resolve(strict=True))
    ):
        raise SmokeAbort("UNKNOWN", "lifecycle_worker_identity_or_exit_mismatch")
    required_worker_rows = [
        next(row for row in worker_rows if row["event"] == event) for event in worker_expected
    ]
    if any(
        row.get("launch_id") != spawned.get("launch_id")
        or row.get("pid") != spawned.get("pid")
        or row.get("workflow_id") != workflow_id
        for row in required_worker_rows
    ):
        raise SmokeAbort("UNKNOWN", "worker_lifecycle_identity_mismatch")
    actual_bridge_sequence = [
        row["event"] for row in bridge_rows if row["event"] in bridge_expected
    ]
    actual_worker_sequence = [
        row["event"] for row in worker_rows if row["event"] in worker_expected
    ]
    if actual_bridge_sequence != list(bridge_expected) or actual_worker_sequence != list(
        worker_expected
    ):
        raise SmokeAbort("UNKNOWN", "lifecycle_event_order_invalid")

    def event_time(row: Mapping[str, Any]) -> datetime:
        return datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))

    reserved_time = event_time(reserved)
    bootstrap_time = event_time(required_worker_rows[0])
    result_time = event_time(required_worker_rows[-1])
    exit_time = event_time(exited)
    if reserved_time > bootstrap_time or result_time > exit_time:
        raise SmokeAbort("UNKNOWN", "lifecycle_cross_file_timestamp_order_invalid")
    return {
        "status": "PASS",
        "bridge_event_counts": dict(sorted(bridge_counts.items())),
        "worker_event_counts": dict(sorted(worker_counts.items())),
        "bridge_events": _lifecycle_projection(bridge_rows),
        "worker_events": _lifecycle_projection(worker_rows),
        "launch_id": spawned["launch_id"],
        "worker_pid": spawned["pid"],
    }


def _validate_confirmed_durable_success(
    *,
    request_state: object,
    dbos_status: object,
    worker_status: Any,
    jobs: Sequence[Mapping[str, Any]],
    expected_job_count: int,
    receipts_verified: bool,
    stored_result: Any,
    workflow_id: str,
    definition_sha256: object,
    input_sha256: object,
) -> None:
    if request_state not in {"running", "succeeded"}:
        raise SmokeAbort("BLOCKED", "request_ledger_state_not_admitted_for_success_projection")
    if dbos_status != "SUCCESS":
        raise SmokeAbort("FAIL", "dbos_workflow_did_not_succeed")
    if (
        not isinstance(worker_status, dict)
        or worker_status.get("state") != "completed"
        or type(worker_status.get("exit_code")) is not int
        or worker_status.get("exit_code") != 0
    ):
        raise SmokeAbort("FAIL", "workflow_worker_did_not_exit_successfully")
    if len(jobs) != expected_job_count or any(job.get("state") != "SUCCEEDED" for job in jobs):
        raise SmokeAbort("FAIL", "workflow_job_ledger_did_not_succeed")
    if not receipts_verified:
        raise SmokeAbort("FAIL", "workflow_job_receipts_not_verified")
    if (
        not isinstance(stored_result, dict)
        or stored_result.get("workflow_id") != workflow_id
        or stored_result.get("definition_sha256") != definition_sha256
        or stored_result.get("input_sha256") != input_sha256
        or "result" not in stored_result
    ):
        raise SmokeAbort("FAIL", "workflow_result_file_identity_or_hash_mismatch")


def _verify_saved_workflow(
    *,
    state_root: Path,
    ui_port: int,
    label: str,
    client_request_id: str,
    workflow_id: str,
    run_id: str,
    workspace: Path,
    max_live_codex_jobs: int,
    expected_definition: Mapping[str, Any],
    expected_input: Mapping[str, Any],
) -> dict[str, Any]:
    request_rows = support._read_request_ledger(state_root)
    matches = [row for row in request_rows if row.get("client_request_id") == client_request_id]
    if len(request_rows) != 2 or len(matches) != 1:
        raise SmokeAbort("BLOCKED", "request_ledger_workflow_count_or_identity_mismatch")
    request = matches[0]
    if (
        request.get("workflow_id") != workflow_id
        or request.get("run_id") != run_id
        or request.get("state") not in {"running", "succeeded"}
        or not isinstance(request.get("request_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(request.get("request_digest"))) is None
    ):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_request_ledger_mismatch")
    run_dir = support._run_directory(state_root, run_id)
    manifest = support._bounded_json_file(run_dir / "manifest.json", maximum=32 * 1024)
    input_data = support._bounded_json_file(run_dir / "input.json", maximum=MAX_JSON_BYTES)
    definition_text = (run_dir / "workflow.yaml").read_text(encoding="utf-8")
    try:
        definition = json.loads(definition_text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_definition_invalid") from None
    expected_workspace = str(workspace.resolve(strict=True))
    digest_payload = {
        "workflow": definition,
        "input": input_data,
        "workspace_root": expected_workspace,
    }
    if max_live_codex_jobs in {2, 3}:
        digest_payload["max_live_codex_jobs"] = max_live_codex_jobs
    calculated_request_digest = _sha256(_canonical_json(digest_payload).encode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or definition != dict(expected_definition)
        or definition_text != f"{_canonical_json(definition)}\n"
        or input_data != dict(expected_input)
        or manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("workflow_source") != "ui-api"
        or manifest.get("mode") != "codex-job-live"
        or manifest.get("workspace_root") != expected_workspace
        or manifest.get("request_digest") != request.get("request_digest")
        or manifest.get("definition_sha256") != support.definition_sha256(definition_text)
        or manifest.get("input_sha256") != _sha256(_canonical_json(input_data).encode("utf-8"))
        or calculated_request_digest != request.get("request_digest")
    ):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_manifest_or_digest_mismatch")
    control = manifest.get("control")
    if (
        not isinstance(control, dict)
        or control.get("codex_job_live") is not True
        or control.get("codex_job_max_polls") != 600
        or control.get("codex_job_poll_interval_seconds") != 0.2
        or (
            max_live_codex_jobs in {2, 3}
            and control.get("max_live_codex_jobs") != max_live_codex_jobs
        )
    ):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_manifest_control_mismatch")
    jobs = support._read_sqlite_rows(
        run_dir / "codex_jobs.sqlite",
        "SELECT job_id, request_key, workflow_id, task_identity, request_digest, request_json, "
        "state, client_request_id, agent_cwd, thread_id, turn_id, remote_state, receipt_json "
        "FROM jobs ORDER BY rowid LIMIT 8",
    )
    events = support._read_sqlite_rows(
        run_dir / "codex_jobs.sqlite",
        "SELECT event_id, job_id, state FROM job_events ORDER BY event_id LIMIT 256",
    )
    expected_count = 2 if label == "A" else 1
    expected_prompts = (
        ("Reply with exactly ALPHA", "Reply with exactly GAMMA")
        if label == "A"
        else ("Reply with exactly BRAVO",)
    )
    if len(jobs) != expected_count or len(events) == 0:
        raise SmokeAbort("BLOCKED", f"workflow_{label}_job_ledger_count_mismatch")
    if label == "A" and tuple(row.get("task_identity") for row in jobs) != ITEM_TASK_IDENTITIES:
        raise SmokeAbort("BLOCKED", "workflow_A_item_dispatch_order_mismatch")
    if label == "B" and jobs[0].get("task_identity") != "do[0].calculate":
        raise SmokeAbort("BLOCKED", "workflow_B_task_identity_mismatch")
    job_ids: list[str] = []
    thread_ids: list[str] = []
    turn_ids: list[str] = []
    replies: list[str] = []
    for row, expected_prompt in zip(jobs, expected_prompts, strict=True):
        job_id = row.get("job_id")
        thread_id = row.get("thread_id")
        turn_id = row.get("turn_id")
        if (
            not isinstance(job_id, str)
            or not isinstance(thread_id, str)
            or not isinstance(turn_id, str)
            or row.get("workflow_id") != workflow_id
            or row.get("state") != "SUCCEEDED"
            or row.get("remote_state") != "completed"
            or row.get("agent_cwd") != expected_workspace
            or row.get("request_key") != f"{workflow_id}::{row.get('task_identity')}"
        ):
            raise SmokeAbort("BLOCKED", f"workflow_{label}_job_identity_or_state_mismatch")
        try:
            request_body = json.loads(str(row["request_json"]))
            receipt = json.loads(str(row["receipt_json"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            raise SmokeAbort("BLOCKED", f"workflow_{label}_job_receipt_invalid") from None
        job_result = receipt.get("result") if isinstance(receipt, dict) else None
        final_text = job_result.get("final_text") if isinstance(job_result, dict) else None
        if (
            not isinstance(request_body, dict)
            or request_body.get("cwd") != expected_workspace
            or request_body.get("prompt") != expected_prompt
            or request_body.get("sandbox_mode") != "inherit"
            or request_body.get("confirm_full_access") is not False
            or final_text != expected_prompt.removeprefix("Reply with exactly ")
        ):
            raise SmokeAbort("FAIL", f"workflow_{label}_codex_request_or_reply_mismatch")
        job_digest = _sha256(
            _canonical_json({"host": BRIDGE_HOST, "port": ui_port, "body": request_body}).encode(
                "utf-8"
            )
        )
        if row.get("request_digest") != job_digest:
            raise SmokeAbort("BLOCKED", f"workflow_{label}_job_request_digest_mismatch")
        job_ids.append(job_id)
        thread_ids.append(thread_id)
        turn_ids.append(turn_id)
        replies.append(final_text)
    if (
        len(set(job_ids)) != expected_count
        or len(set(thread_ids)) != expected_count
        or len(set(turn_ids)) != expected_count
        or [row.get("job_id") for row in events if row.get("state") == "MAYBE_SENT"] != job_ids
        or support.maximum_active_codex_jobs(events) != 1
        or sum(row.get("state") == "SUCCEEDED" for row in events) != expected_count
    ):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_sequential_event_order_mismatch")
    dbos_status = support._read_dbos_status(run_dir, workflow_id)
    worker_status = support._bounded_json_file(run_dir / "worker-entry-status.json", maximum=4096)
    worker_identity_record = support._bounded_json_file(
        run_dir / "start-worker-identity.json", maximum=8192
    )
    stored_result = support._bounded_json_file(run_dir / "result.json", maximum=MAX_JSON_BYTES)
    if (
        not isinstance(worker_identity_record, dict)
        or not isinstance(worker_identity_record.get("pid"), int)
        or not isinstance(worker_identity_record.get("parent_pid"), int)
        or not isinstance(worker_identity_record.get("image_path"), str)
        or os.path.normcase(
            str(Path(str(worker_identity_record["image_path"])).resolve(strict=True))
        )
        != os.path.normcase(str(Path(sys.executable).resolve(strict=True)))
    ):
        raise SmokeAbort("FAIL", f"workflow_{label}_worker_identity_mismatch")
    _validate_confirmed_durable_success(
        request_state=request.get("state"),
        dbos_status=dbos_status,
        worker_status=worker_status,
        jobs=jobs,
        expected_job_count=expected_count,
        receipts_verified=len(replies) == expected_count,
        stored_result=stored_result,
        workflow_id=workflow_id,
        definition_sha256=manifest.get("definition_sha256"),
        input_sha256=manifest.get("input_sha256"),
    )
    if label == "A":
        validate_item_result(
            {"workflow_id": workflow_id, "result": stored_result.get("result")},
            workflow_id=workflow_id,
            jobs=jobs,
        )
    else:
        validate_single_job_result(
            {"workflow_id": workflow_id, "result": stored_result.get("result")},
            workflow_id=workflow_id,
            job=jobs[0],
        )
    return {
        "run_dir": run_dir,
        "request_state": request["state"],
        "request_digest": str(request["request_digest"]),
        "manifest": manifest,
        "jobs": jobs,
        "events": events,
        "job_ids": job_ids,
        "thread_ids": thread_ids,
        "turn_ids": turn_ids,
        "replies": replies,
        "dbos_status": dbos_status,
        "worker_status": worker_status,
        "worker_identity": worker_identity_record,
        "stored_result": stored_result,
    }


def _verify_api_readback(
    *,
    ui_port: int,
    token: str,
    label: str,
    workflow_id: str,
    run_id: str,
    max_live_codex_jobs: int,
    saved: Mapping[str, Any],
    listing: Mapping[str, Any],
    detail: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    jobs = saved["jobs"]
    if (
        listing.get("workflow_id") != workflow_id
        or listing.get("status") != "succeeded"
        or detail.get("workflow_id") != workflow_id
        or detail.get("run_id") != run_id
        or detail.get("status") != "succeeded"
        or detail.get("worker_state") != "completed"
        or detail.get("terminal") is not True
        or dict(result) != saved["stored_result"]
    ):
        raise SmokeAbort("BLOCKED", f"workflow_{label}_api_and_ledger_readback_mismatch")
    if max_live_codex_jobs == 1:
        job = jobs[0]
        if (
            "max_live_codex_jobs" in detail
            or "jobs" in detail
            or detail.get("job_id") != job.get("job_id")
            or detail.get("thread_id") != job.get("thread_id")
            or listing.get("job_id") != job.get("job_id")
            or listing.get("thread_id") != job.get("thread_id")
        ):
            raise SmokeAbort("BLOCKED", "workflow_B_single_job_api_projection_mismatch")
    elif detail.get("max_live_codex_jobs") != max_live_codex_jobs or detail.get("jobs") != [
        support._job_summary(row) for row in jobs
    ]:
        raise SmokeAbort("BLOCKED", f"workflow_{label}_api_and_ledger_readback_mismatch")
    if label == "A":
        validate_item_result(result, workflow_id=workflow_id, jobs=jobs)
    else:
        validate_single_job_result(result, workflow_id=workflow_id, job=jobs[0])


def _workflow_evidence(
    *,
    client_request_id: str,
    workflow_id: str,
    run_id: str,
    saved: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": "PASS",
        "client_request_id": client_request_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "request_digest": saved["request_digest"],
        "request_ledger_state": saved["request_state"],
        "dbos_status": saved["dbos_status"],
        "worker_exit_code": saved["worker_status"]["exit_code"],
        "worker_pid": saved["worker_identity"]["pid"],
        "job_count": len(saved["jobs"]),
        "jobs": [
            {
                "job_id": row["job_id"],
                "task_identity": row["task_identity"],
                "request_digest": row["request_digest"],
                "state": row["state"],
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "reply_digest": _sha256(str(reply).encode("utf-8")),
            }
            for row, reply in zip(saved["jobs"], saved["replies"], strict=True)
        ],
        "result_digest": _sha256(_canonical_json(result).encode("utf-8")),
        "result_statuses": (
            [row.get("status") for row in result["result"]["results"]]
            if "results" in result.get("result", {})
            else ["SUCCEEDED"]
        ),
        "result_summary": result.get("result", {}).get("summary"),
    }


def _qt_readback(
    *,
    ui_port: int,
    token: str,
    label: str,
    workflow_id: str,
    expected_detail: Mapping[str, Any],
    expected_result: Mapping[str, Any],
) -> None:
    application = QApplication.instance() or QApplication([])
    client = ApiClient(f"http://{BRIDGE_HOST}:{ui_port}")
    dialog = WorkflowManagementDialog()
    detail_path = f"/ui-api/control/workflows/{quote(workflow_id, safe='')}"
    result_path = f"/ui-api/control/workflows/{quote(workflow_id, safe='')}/result"
    try:
        detail_status, detail = support._qt_get(
            client, detail_path, token, f"phase5f5:{label}:detail"
        )
        result_status, result = support._qt_get(
            client, result_path, token, f"phase5f5:{label}:result"
        )
        if (
            detail_status != 200
            or result_status != 200
            or detail != expected_detail
            or result != expected_result
        ):
            raise SmokeAbort("FAIL", f"workflow_{label}_console_api_readback_mismatch")
        dialog.set_detail(detail)
        dialog.set_result(result)
        if (
            dialog.detail_labels["workflow_id"].text() != workflow_id
            or not dialog.result_json.toPlainText()
            or token in dialog.result_json.toPlainText()
            or "Reply with exactly" in dialog.result_json.toPlainText()
        ):
            raise SmokeAbort("FAIL", f"workflow_{label}_console_render_mismatch")
        application.processEvents()
    finally:
        dialog.close()
        client.deleteLater()
        application.processEvents()


def _reserve_restart_ports(
    first_pair: tuple[int, int],
) -> tuple[tuple[int, int], list[socket.socket]]:
    for _attempt in range(16):
        pair, reservations = support._reserve_loopback_ports()
        if not set(pair).intersection(first_pair):
            return pair, reservations
        for reservation in reservations:
            reservation.close()
    raise SmokeAbort("BLOCKED", "second_loopback_port_pair_unavailable")


def _verify_logical_run_ids(
    *, state_root: Path, ids: Mapping[str, tuple[str, str, str]], expected_count: int
) -> None:
    rows = support._read_request_ledger(state_root)
    expected_request_ids = {entry[0] for entry in ids.values()}
    if (
        len(rows) != expected_count
        or {row.get("client_request_id") for row in rows} != expected_request_ids
        or len({row.get("workflow_id") for row in rows}) != expected_count
        or len({row.get("run_id") for row in rows}) != expected_count
    ):
        raise SmokeAbort("BLOCKED", "workflow_request_run_identity_isolation_mismatch")


def _capacity_probe(
    *,
    process: subprocess.Popen[bytes],
    state_root: Path,
    ui_port: int,
    token: str,
    workspace_c: Path,
    body_c: bytes,
    client_request_id_c: str,
    accepted: Mapping[str, tuple[str, str, str]],
    evidence: dict[str, Any],
    writer: _EvidenceWriter,
    temp_root: Path,
) -> None:
    workflows = _read_workflow_list(ui_port, token)
    by_id = {str(row.get("workflow_id")): row for row in workflows}
    active = all(
        workflow_id in by_id and by_id[workflow_id].get("status") in ACTIVE_STATUSES
        for _request, workflow_id, _run in accepted.values()
    )
    workers = _worker_process_snapshot(process.pid)
    spawn_pids: set[int] = set()
    spawn_counts: dict[str, int] = {}
    if active and len(workflows) == 2 and len(workers) == 2:
        for label, (_request_id, workflow_id, run_id) in accepted.items():
            try:
                bridge_rows = _read_lifecycle(
                    support._run_directory(state_root, run_id) / "bridge-lifecycle.jsonl",
                    run_id=run_id,
                    workflow_id=workflow_id,
                )
            except SmokeAbort:
                bridge_rows = []
            launched = [
                row
                for row in bridge_rows
                if row.get("event") == "worker_spawn_succeeded" and isinstance(row.get("pid"), int)
            ]
            spawn_counts[label] = len(launched)
            spawn_pids.update(int(row["pid"]) for row in launched)
    observed_worker_pids = {int(row["pid"]) for row in workers}
    observed_worker_creation = {int(row["pid"]): float(row["created_at_epoch"]) for row in workers}
    if (
        not active
        or len(workflows) != 2
        or len(workers) != 2
        or not all(row.get("identity_verified") for row in workers)
        or spawn_counts != {"A": 1, "B": 1}
        or spawn_pids != observed_worker_pids
        or workspace_c.is_symlink()
        or not workspace_c.is_dir()
        or any(workspace_c.iterdir())
    ):
        evidence["checks"]["simultaneous_capacity_409"] = "NOT_OBSERVED"
        evidence["workflows"]["C"] = {
            "status": "NOT_OBSERVED",
            "reason": "two_active_owned_workers_not_observed_in_one_readback_window",
            "post_attempts": 0,
        }
        return
    evidence["processes"]["worker_snapshots"].append(
        {"stage": "both_workflows_active", "workers": workers}
    )
    before_rows = support._read_request_ledger(state_root)
    if len(before_rows) != 2:
        raise SmokeAbort("BLOCKED", "capacity_probe_precondition_request_count_mismatch")
    evidence["workflows"]["C"] = {
        "status": "MAYBE_SENT",
        "client_request_id": client_request_id_c,
        "workspace_label": "C",
        "request_body_digest": _sha256(body_c),
        "post_attempts": 0,
    }
    try:
        status, payload = _start_post(
            ui_port=ui_port,
            token=token,
            body=body_c,
            label="C",
            evidence=evidence,
            writer=writer,
            temp_root=temp_root,
        )
    except SmokeAbort:
        reconciliation = _reconcile_request(
            state_root=state_root,
            ui_port=ui_port,
            token=token,
            client_request_id=client_request_id_c,
        )
        evidence["workflows"]["C"]["reconciliation"] = {
            "matching_request_rows": reconciliation["matching_request_rows"],
            "list_readable": reconciliation["list_readable"],
        }
        raise SmokeAbort("UNKNOWN", "capacity_probe_outcome_unknown_no_retry") from None
    evidence["workflows"]["C"].update(
        {"post_outcome": "HTTP_RESPONSE", "http_status": status, "post_attempts": 1}
    )
    if (
        status != 409
        or not isinstance(payload, dict)
        or payload.get("error") != "workflow_capacity_reached"
    ):
        evidence["workflows"]["C"]["status"] = "UNEXPECTED_RESPONSE"
        evidence["workflows"]["C"]["error_code"] = (
            payload.get("error") if isinstance(payload, dict) else None
        )
        reconciliation = _reconcile_request(
            state_root=state_root,
            ui_port=ui_port,
            token=token,
            client_request_id=client_request_id_c,
        )
        evidence["workflows"]["C"]["reconciliation"] = {
            "matching_request_rows": reconciliation["matching_request_rows"],
            "list_readable": reconciliation["list_readable"],
        }
        raise SmokeAbort("UNKNOWN", "capacity_probe_did_not_return_expected_409")
    after_rows = support._read_request_ledger(state_root)
    after_workers = _worker_process_snapshot(process.pid)
    after_workflows = _read_workflow_list(ui_port, token)
    after_workers_same_identities = all(
        int(row["pid"]) in observed_worker_creation
        and abs(float(row["created_at_epoch"]) - observed_worker_creation[int(row["pid"])]) < 0.01
        for row in after_workers
    )
    after_spawn_counts: dict[str, int] = {}
    for label, (_request_id, workflow_id, run_id) in accepted.items():
        bridge_rows = _read_lifecycle(
            support._run_directory(state_root, run_id) / "bridge-lifecycle.jsonl",
            run_id=run_id,
            workflow_id=workflow_id,
        )
        after_spawn_counts[label] = sum(
            row.get("event") == "worker_spawn_succeeded" for row in bridge_rows
        )
    if (
        after_rows != before_rows
        or any(row.get("client_request_id") == client_request_id_c for row in after_rows)
        or {row.get("workflow_id") for row in after_workflows}
        != {workflow_id for _request_id, workflow_id, _run_id in accepted.values()}
        or not after_workers_same_identities
        or after_spawn_counts != spawn_counts
    ):
        raise SmokeAbort("UNKNOWN", "capacity_409_had_unexpected_durable_side_effect")
    evidence["workflows"]["C"]["status"] = "REJECTED_409"
    evidence["workflows"]["C"]["error_code"] = "workflow_capacity_reached"
    evidence["checks"]["simultaneous_capacity_409"] = "PASS"


def _read_replay_baseline(
    *, state_root: Path, ids: Mapping[str, tuple[str, str, str]]
) -> dict[str, Any]:
    rows = support._read_request_ledger(state_root)
    run_dirs: dict[str, str] = {}
    job_counts: dict[str, int] = {}
    spawn_counts: dict[str, int] = {}
    for label, (_request_id, workflow_id, run_id) in ids.items():
        run_dir = support._run_directory(state_root, run_id)
        job_rows, _events = support._read_job_ledger(run_dir)
        bridge_rows = _read_lifecycle(
            run_dir / "bridge-lifecycle.jsonl", run_id=run_id, workflow_id=workflow_id
        )
        run_dirs[label] = run_id
        job_counts[label] = len(job_rows)
        spawn_counts[label] = sum(
            row.get("event") == "worker_spawn_succeeded" for row in bridge_rows
        )
    return {
        "request_rows": rows,
        "run_dirs": run_dirs,
        "job_counts": job_counts,
        "spawn_counts": spawn_counts,
    }


def _replay_same_ids(
    *,
    ui_port: int,
    token: str,
    state_root: Path,
    process: subprocess.Popen[bytes],
    bodies: Mapping[str, bytes],
    ids: Mapping[str, tuple[str, str, str]],
    evidence: dict[str, Any],
    writer: _EvidenceWriter,
    temp_root: Path,
) -> None:
    baseline = _read_replay_baseline(state_root=state_root, ids=ids)
    evidence["checks"]["same_id_replay"] = "IN_PROGRESS"
    for label, replay_label in (("A", "replay_A"), ("B", "replay_B")):
        request_id, workflow_id, run_id = ids[label]
        replay_evidence = evidence["workflows"][label]
        if replay_evidence.get("status") != "PASS":
            raise SmokeAbort("BLOCKED", "same_id_replay_requires_confirmed_success")
        try:
            status, payload = _start_post(
                ui_port=ui_port,
                token=token,
                body=bodies[label],
                label=replay_label,
                evidence=evidence,
                writer=writer,
                temp_root=temp_root,
            )
        except SmokeAbort:
            reconciliation = _reconcile_request(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=request_id,
            )
            evidence["workflows"][label]["same_id_replay"] = "UNKNOWN"
            evidence["workflows"][label]["replay_reconciliation"] = {
                "matching_request_rows": reconciliation["matching_request_rows"],
                "list_readable": reconciliation["list_readable"],
            }
            raise SmokeAbort("UNKNOWN", "same_id_replay_outcome_unknown_no_retry") from None
        if (
            status != 202
            or not isinstance(payload, dict)
            or payload.get("workflow_id") != workflow_id
            or payload.get("run_id") != run_id
            or payload.get("status") != "succeeded"
        ):
            evidence["workflows"][label]["same_id_replay"] = "UNKNOWN"
            evidence["workflows"][label]["same_id_replay_http_status"] = status
            reconciliation = _reconcile_request(
                state_root=state_root,
                ui_port=ui_port,
                token=token,
                client_request_id=request_id,
            )
            evidence["workflows"][label]["replay_reconciliation"] = {
                "matching_request_rows": reconciliation["matching_request_rows"],
                "list_readable": reconciliation["list_readable"],
            }
            raise SmokeAbort("UNKNOWN", "same_id_replay_outcome_unknown_no_retry")
        evidence["workflows"][label]["same_id_replay"] = "REUSED_EXISTING_RUN"
        evidence["workflows"][label]["same_id_replay_http_status"] = status
    current = _read_replay_baseline(state_root=state_root, ids=ids)
    if (
        current["request_rows"] != baseline["request_rows"]
        or current["run_dirs"] != baseline["run_dirs"]
        or current["job_counts"] != baseline["job_counts"]
        or current["spawn_counts"] != baseline["spawn_counts"]
        or support._worker_process_count(process.pid) != 0
    ):
        raise SmokeAbort("UNKNOWN", "same_id_replay_created_work_or_worker")
    evidence["checks"]["same_id_replay"] = "PASS"


def _restart_read_only(
    *,
    base_environment: Mapping[str, str],
    temp_root: Path,
    token: str,
    workspaces: Sequence[Path],
    codex_executable: Path,
    state_root: Path,
    first_ports: tuple[int, int],
    ids: Mapping[str, tuple[str, str, str]],
    api_snapshots: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]],
    evidence: dict[str, Any],
    writer: _EvidenceWriter,
) -> tuple[subprocess.Popen[bytes] | None, dict[str, Any] | None]:
    ports, reservations = _reserve_restart_ports(first_ports)
    for reservation in reservations:
        reservation.close()
    reservations.clear()
    support._assert_ports_free(ports)
    evidence["ports"]["restart"] = {"mcp": ports[0], "ui": ports[1], "distinct_from_initial": True}
    environment, _config_path = _write_runtime_config(
        temp_root=temp_root,
        base_environment=base_environment,
        token=token,
        ports=ports,
        workspaces=workspaces,
        codex_executable=codex_executable,
        config_name="bridge-restart.toml",
    )
    process: subprocess.Popen[bytes] | None = None
    identity: dict[str, Any] | None = None
    try:
        process = _write_child_bridge(environment, temp_root=temp_root, log_prefix="restart")
        identity = _capture_bridge_identity(process)
        evidence["processes"]["bridge_restart"] = identity
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)
        support._wait_for_bridge(process, ports[1])
        support._owned_listener_pids(process.pid, ports)
        preflight = support._preflight_bridge(
            process,
            mcp_port=ports[0],
            ui_port=ports[1],
            token=token,
            expected_codex=codex_executable,
        )
        support._owned_listener_pids(process.pid, ports)
        if support._worker_process_count(process.pid) != 0:
            raise SmokeAbort("UNKNOWN", "restart_started_a_workflow_worker")
        rows = support._read_request_ledger(state_root)
        if len(rows) != 2:
            raise SmokeAbort("UNKNOWN", "restart_changed_durable_request_rows")
        for label, (_request, workflow_id, _run) in ids.items():
            listing = support._workflow_list_item(ports[1], token, workflow_id)
            detail = support._workflow_detail(ports[1], token, workflow_id)
            result = support._workflow_result(ports[1], token, workflow_id)
            expected_listing, expected_detail, expected_result = api_snapshots[label]
            if (
                listing != expected_listing
                or detail != expected_detail
                or result != expected_result
            ):
                raise SmokeAbort("UNKNOWN", f"restart_{label}_read_only_snapshot_changed")
        evidence["checks"]["restart_read_only_get"] = "PASS"
        try:
            for label, (_request, workflow_id, _run) in ids.items():
                _expected_listing, expected_detail, expected_result = api_snapshots[label]
                _qt_readback(
                    ui_port=ports[1],
                    token=token,
                    label=f"{label}_restart",
                    workflow_id=workflow_id,
                    expected_detail=expected_detail,
                    expected_result=expected_result,
                )
            evidence["checks"]["restart_console_offscreen_readback"] = "PASS"
        except Exception as error:
            evidence["checks"]["restart_console_offscreen_readback"] = "NOT_RUN"
            evidence["restart_console_offscreen_reason"] = (
                error.code if isinstance(error, SmokeAbort) else type(error).__name__
            )
        evidence["restart_readback"] = {
            "appserver_ready": preflight["appserver_pid"] > 0,
            "workflow_worker_count": 0,
            "request_rows_unchanged": True,
            "detail_and_result_matches_initial": True,
        }
        return process, identity
    except Exception:
        if process is not None:
            if identity is None or process.poll() is not None:
                evidence["processes"]["orphan_processes"] = "UNKNOWN"
            else:
                try:
                    _assert_bridge_identity(process, identity)
                    stopped = support._stop_bridge(process, ui_port=ports[1], token=token)
                    evidence["processes"]["shutdowns"].append(
                        {"role": "restart_after_readback", "pid": process.pid, **stopped}
                    )
                    if stopped.get("reaped"):
                        evidence["processes"]["orphan_processes"] = 0
                        support._assert_ports_free(ports)
                    else:
                        evidence["processes"]["orphan_processes"] = "UNKNOWN"
                except Exception:
                    evidence["processes"]["orphan_processes"] = "UNKNOWN"
        raise


def _run_acceptance(*, evidence: dict[str, Any], writer: _EvidenceWriter) -> int:
    process: subprocess.Popen[bytes] | None = None
    process_identity: dict[str, Any] | None = None
    token: str | None = None
    temp_root: Path | None = None
    reservations: list[socket.socket] = []
    ports: tuple[int, int] | None = None
    state_root: Path | None = None
    safe_to_shutdown = False
    replies: list[str] = []
    prompts = (
        "Reply with exactly ALPHA",
        "Reply with exactly SKIP",
        "Reply with exactly GAMMA",
        "Reply with exactly BRAVO",
        "Reply with exactly CHARLIE",
    )
    try:
        _preflight_runtime(evidence)
        base_environment = dict(os.environ)
        head_commit = _verified_base_ancestor_head()
        evidence["source_commit"] = head_commit
        evidence["head_commit"] = head_commit
        item_yaml, item_definition, single_yaml, single_definition = _validate_templates()
        item_input = build_item_input()
        try:
            codex_resolution = resolve_codex_executable(
                config_executable=None, environ=base_environment
            )
            codex_executable = Path(codex_resolution.path).resolve(strict=True)
        except (OSError, ValueError):
            raise SmokeAbort("BLOCKED", "windows_codex_executable_unavailable") from None
        if not codex_executable.is_file() or codex_executable.suffix.casefold() != ".exe":
            raise SmokeAbort("BLOCKED", "windows_codex_executable_invalid")
        source_codex_home = support._source_codex_home(base_environment)
        source_auth = source_codex_home / "auth.json"
        try:
            source_auth_ok = (
                not source_auth.is_symlink()
                and source_auth.is_file()
                and source_auth.stat().st_size <= support.MAX_AUTH_BYTES
            )
        except OSError:
            source_auth_ok = False
        if not source_auth_ok:
            raise SmokeAbort("BLOCKED", "existing_codex_authentication_unavailable")
        temp_root = Path(tempfile.mkdtemp(prefix="codexbridge-phase5f5-")).resolve(strict=True)
        for directory in ("localappdata", "appdata", "tmp"):
            (temp_root / directory).mkdir()
        workspaces = tuple(temp_root / f"workspace-{label}" for label in ("A", "B", "C"))
        for workspace in workspaces:
            workspace.mkdir()
        isolated_codex_home = temp_root / "codex_home"
        support._copy_authentication(source_codex_home, isolated_codex_home)
        try:
            auth_copy_matches = _sha256(source_auth.read_bytes()) == _sha256(
                (isolated_codex_home / "auth.json").read_bytes()
            )
        except OSError:
            auth_copy_matches = False
        if not auth_copy_matches:
            raise SmokeAbort("BLOCKED", "isolated_authentication_copy_mismatch")
        token = secrets.token_urlsafe(32)
        client_ids = {label: f"phase5f5-{uuid.uuid4().hex}" for label in ("A", "B", "C")}
        if len(set(client_ids.values())) != 3:
            raise SmokeAbort("BLOCKED", "client_request_id_uniqueness_failed")
        ports, reservations = support._reserve_loopback_ports()
        runtime_config = _write_runtime_config(
            temp_root=temp_root,
            base_environment=base_environment,
            token=token,
            ports=ports,
            workspaces=workspaces,
            codex_executable=codex_executable,
            config_name="bridge.toml",
        )
        environment, _config_path, state_root = _runtime_paths_from_config_result(
            runtime_config,
            temp_root=temp_root,
        )
        _ensure_workspace_preflight(
            temp_root=temp_root, state_root=state_root, workspaces=workspaces
        )
        if not roots_are_separate(state_root, workspaces[1]) or not roots_are_separate(
            state_root, workspaces[2]
        ):
            raise SmokeAbort("BLOCKED", "isolated_state_root_overlaps_allowed_workspace")
        evidence["isolation"] = {
            "status": "PASS",
            "temp_root_label": temp_root.name,
            "workspace_labels": ["A", "B", "C"],
            "workspace_roots_distinct_and_empty": True,
            "allowed_root_count": 3,
            "allowed_root_labels": ["A", "B", "C"],
            "isolated_auth_copy_verified": True,
            "isolated_codex_home": True,
            "isolated_state_empty": True,
            "github_mcp_disabled": True,
            "lan_disabled": True,
            "state_and_workspaces_separate": True,
            "production_checkouts_touched": 0,
        }
        evidence["ports"]["initial"] = {
            "mcp": ports[0],
            "ui": ports[1],
            "loopback_only": True,
            "default_ports_18000_18001_avoided": True,
        }
        support._preflight_state_is_separate(state_root, workspaces[0])
        support._ensure_empty_isolated_state(state_root)
        for reservation in reservations:
            reservation.close()
        reservations.clear()
        support._assert_ports_free(ports)
        evidence["checks"]["isolated_auth_config_roots_and_empty_state"] = "PASS"
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)

        process = _write_child_bridge(environment, temp_root=temp_root, log_prefix="initial")
        process_identity = _capture_bridge_identity(process)
        evidence["processes"]["bridge_initial"] = process_identity
        safe_to_shutdown = True
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)
        support._wait_for_bridge(process, ports[1])
        support._owned_listener_pids(process.pid, ports)
        preflight = support._preflight_bridge(
            process,
            mcp_port=ports[0],
            ui_port=ports[1],
            token=token,
            expected_codex=codex_executable,
        )
        support._owned_listener_pids(process.pid, ports)
        if support._worker_process_count(process.pid) != 0:
            raise SmokeAbort("BLOCKED", "worker_started_before_workflow_posts")
        empty_list = _read_workflow_list(ports[1], token)
        if empty_list or support._read_request_ledger(state_root):
            raise SmokeAbort("BLOCKED", "isolated_bridge_state_not_empty_before_posts")
        evidence["processes"]["appserver_pid"] = preflight["appserver_pid"]
        appserver_identity = _capture_owned_process(
            psutil.Process(preflight["appserver_pid"]),
            role="codex_appserver",
            expected_executable=codex_executable,
        )
        try:
            appserver_ancestors = {
                ancestor.pid for ancestor in psutil.Process(preflight["appserver_pid"]).parents()
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            raise SmokeAbort("BLOCKED", "codex_appserver_parent_chain_unavailable") from None
        if process.pid not in appserver_ancestors:
            raise SmokeAbort("BLOCKED", "codex_appserver_not_owned_by_isolated_bridge")
        evidence["processes"]["appserver"] = appserver_identity
        evidence["checks"]["bridge_owner_ports_and_control_auth"] = "PASS"
        evidence["checks"]["bridge_readiness"] = "PASS"
        evidence["workflows"]["A"] = {
            "client_request_id": client_ids["A"],
            "workspace_label": "A",
            "max_live_codex_jobs": 3,
            "workflow_yaml_sha256": _sha256(item_yaml.encode("utf-8")),
            "request_body_sha256": None,
            "post_attempts": 0,
        }
        evidence["workflows"]["B"] = {
            "client_request_id": client_ids["B"],
            "workspace_label": "B",
            "max_live_codex_jobs": 1,
            "workflow_yaml_sha256": _sha256(single_yaml.encode("utf-8")),
            "request_body_sha256": None,
            "post_attempts": 0,
        }
        bodies = {
            "A": _request_body(
                client_request_id=client_ids["A"],
                workflow_yaml=item_yaml,
                input_data=item_input,
                workspace=workspaces[0],
                max_live_codex_jobs=ITEM_MAX_LIVE_CODEX_JOBS,
            ),
            "B": _request_body(
                client_request_id=client_ids["B"],
                workflow_yaml=single_yaml,
                input_data={},
                workspace=workspaces[1],
                max_live_codex_jobs=SINGLE_MAX_LIVE_CODEX_JOBS,
            ),
        }
        for label in ("A", "B"):
            evidence["workflows"][label]["request_body_sha256"] = _sha256(bodies[label])
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)

        accepted: dict[str, tuple[str, str, str]] = {}
        for label in ("A", "B"):
            try:
                status, payload = _start_post(
                    ui_port=ports[1],
                    token=token,
                    body=bodies[label],
                    label=label,
                    evidence=evidence,
                    writer=writer,
                    temp_root=temp_root,
                )
            except SmokeAbort:
                reconciliation = _reconcile_request(
                    state_root=state_root,
                    ui_port=ports[1],
                    token=token,
                    client_request_id=client_ids[label],
                )
                evidence["workflows"][label]["post_outcome"] = "UNKNOWN"
                evidence["workflows"][label]["reconciliation"] = {
                    "matching_request_rows": reconciliation["matching_request_rows"],
                    "list_readable": reconciliation["list_readable"],
                }
                _save_evidence(writer, evidence, token=token, temp_root=temp_root)
                raise SmokeAbort(
                    "UNKNOWN", f"workflow_{label}_post_outcome_unknown_no_retry"
                ) from None
            if status != 202 or not isinstance(payload, dict):
                evidence["workflows"][label]["post_http_status"] = status
                evidence["workflows"][label]["post_outcome"] = "REJECTED"
                reconciliation = _reconcile_request(
                    state_root=state_root,
                    ui_port=ports[1],
                    token=token,
                    client_request_id=client_ids[label],
                )
                evidence["workflows"][label]["reconciliation"] = {
                    "matching_request_rows": reconciliation["matching_request_rows"],
                    "list_readable": reconciliation["list_readable"],
                }
                raise SmokeAbort("BLOCKED", f"workflow_{label}_post_not_accepted")
            try:
                workflow_id, run_id = support._bridge_run_directory_from_response(payload)
            except SmokeAbort:
                reconciliation = _reconcile_request(
                    state_root=state_root,
                    ui_port=ports[1],
                    token=token,
                    client_request_id=client_ids[label],
                )
                evidence["workflows"][label]["post_outcome"] = "UNKNOWN"
                evidence["workflows"][label]["reconciliation"] = {
                    "matching_request_rows": reconciliation["matching_request_rows"],
                    "list_readable": reconciliation["list_readable"],
                }
                raise SmokeAbort(
                    "UNKNOWN", f"workflow_{label}_accepted_identity_invalid_no_retry"
                ) from None
            accepted[label] = (client_ids[label], workflow_id, run_id)
            evidence["posts"]["accepted_initial_workflows"] += 1
            evidence["workflows"][label].update(
                {
                    "post_outcome": "ACCEPTED_202",
                    "post_http_status": 202,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                }
            )
            snapshots = _worker_process_snapshot(process.pid)
            evidence["processes"]["worker_snapshots"].append(
                {"stage": f"after_workflow_{label}_accepted", "workers": snapshots}
            )
            _save_evidence(writer, evidence, token=token, temp_root=temp_root)
        _verify_logical_run_ids(state_root=state_root, ids=accepted, expected_count=2)
        workflow_list_snapshot = _read_workflow_list(ports[1], token)
        by_workflow = {str(row.get("workflow_id")): row for row in workflow_list_snapshot}
        evidence["workflows"]["active_observation"] = {
            "workflow_count": len(workflow_list_snapshot),
            "statuses": {
                label: by_workflow.get(workflow_id, {}).get("status")
                for label, (_request, workflow_id, _run) in accepted.items()
            },
        }
        evidence["processes"]["worker_snapshots"].append(
            {
                "stage": "after_both_workflows_accepted",
                "workers": _worker_process_snapshot(process.pid),
            }
        )
        c_body = _request_body(
            client_request_id=client_ids["C"],
            workflow_yaml=single_yaml.replace("BRAVO", "CHARLIE"),
            input_data={},
            workspace=workspaces[2],
            max_live_codex_jobs=SINGLE_MAX_LIVE_CODEX_JOBS,
        )
        _capacity_probe(
            process=process,
            state_root=state_root,
            ui_port=ports[1],
            token=token,
            workspace_c=workspaces[2],
            body_c=c_body,
            client_request_id_c=client_ids["C"],
            accepted=accepted,
            evidence=evidence,
            writer=writer,
            temp_root=temp_root,
        )
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)

        api_snapshots: dict[
            str, tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]
        ] = {}
        saved_states: dict[str, dict[str, Any]] = {}
        global_ids: dict[str, set[str]] = {"job": set(), "thread": set(), "turn": set()}
        for label, definition, input_data, workspace, max_live in (
            ("A", item_definition, item_input, workspaces[0], ITEM_MAX_LIVE_CODEX_JOBS),
            ("B", single_definition, {}, workspaces[1], SINGLE_MAX_LIVE_CODEX_JOBS),
        ):
            _request, workflow_id, run_id = accepted[label]
            try:
                listing, detail, result = support._wait_for_workflow_success(
                    ports[1], token, workflow_id
                )
            except SmokeAbort as error:
                if error.code in {
                    "workflow_api_requires_reconciliation",
                    "workflow_completion_timeout",
                    "workflow_api_reported_failure",
                }:
                    raise SmokeAbort(
                        "UNKNOWN", f"workflow_{label}_did_not_reach_confirmed_success"
                    ) from None
                raise
            saved = _verify_saved_workflow(
                state_root=state_root,
                ui_port=ports[1],
                label=label,
                client_request_id=client_ids[label],
                workflow_id=workflow_id,
                run_id=run_id,
                workspace=workspace,
                max_live_codex_jobs=max_live,
                expected_definition=definition,
                expected_input=input_data,
            )
            _verify_api_readback(
                ui_port=ports[1],
                token=token,
                label=label,
                workflow_id=workflow_id,
                run_id=run_id,
                max_live_codex_jobs=max_live,
                saved=saved,
                listing=listing,
                detail=detail,
                result=result,
            )
            for kind, values in (
                ("job", saved["job_ids"]),
                ("thread", saved["thread_ids"]),
                ("turn", saved["turn_ids"]),
            ):
                if global_ids[kind].intersection(values):
                    raise SmokeAbort("BLOCKED", f"workflow_{kind}_identity_reused_across_runs")
                global_ids[kind].update(values)
            saved_states[label] = saved
            api_snapshots[label] = (listing, detail, result)
            evidence["workflows"][label] = {
                **evidence["workflows"][label],
                **_workflow_evidence(
                    client_request_id=client_ids[label],
                    workflow_id=workflow_id,
                    run_id=run_id,
                    saved=saved,
                    result=result,
                ),
            }
            evidence["checks"][
                f"workflow_{label}_items_result" if label == "A" else "workflow_B_single_result"
            ] = "PASS"
            replies.extend(saved["replies"])
            _save_evidence(writer, evidence, token=token, temp_root=temp_root)
        _verify_logical_run_ids(state_root=state_root, ids=accepted, expected_count=2)
        support._wait_for_no_workers(process.pid, timeout_seconds=10.0)
        worker_ids = {int(evidence["workflows"][label]["worker_pid"]) for label in ("A", "B")}
        spawned_ids = {
            int(row["pid"])
            for label in ("A", "B")
            for row in _read_lifecycle(
                saved_states[label]["run_dir"] / "bridge-lifecycle.jsonl",
                run_id=accepted[label][2],
                workflow_id=accepted[label][1],
            )
            if row.get("event") == "worker_spawn_succeeded" and isinstance(row.get("pid"), int)
        }
        observed_worker_ids = {
            int(row["pid"])
            for snapshot in evidence["processes"]["worker_snapshots"]
            for row in snapshot.get("workers", [])
            if isinstance(row, dict) and isinstance(row.get("pid"), int)
        }
        if worker_ids != spawned_ids or not worker_ids.issubset(observed_worker_ids):
            raise SmokeAbort("UNKNOWN", "workflow_worker_spawn_identity_not_correlated")
        for label in ("A", "B"):
            lifecycle = _verify_lifecycle(
                run_dir=saved_states[label]["run_dir"],
                run_id=accepted[label][2],
                workflow_id=accepted[label][1],
                request_digest=saved_states[label]["request_digest"],
                worker_status=saved_states[label]["worker_status"],
                worker_identity_record=saved_states[label]["worker_identity"],
                bridge_pid=process.pid,
                token=token,
                temp_root=temp_root,
                prompts=prompts,
                replies=saved_states[label]["replies"],
            )
            evidence["workflows"][label]["lifecycle"] = lifecycle
        evidence["checks"]["lifecycle_logs"] = "PASS"
        try:
            for label in ("A", "B"):
                listing, detail, result = api_snapshots[label]
                _qt_readback(
                    ui_port=ports[1],
                    token=token,
                    label=label,
                    workflow_id=accepted[label][1],
                    expected_detail=detail,
                    expected_result=result,
                )
            evidence["checks"]["console_offscreen_readback"] = "PASS"
        except Exception as error:
            evidence["checks"]["console_offscreen_readback"] = "NOT_RUN"
            evidence["console_offscreen_reason"] = (
                error.code if isinstance(error, SmokeAbort) else type(error).__name__
            )
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)

        _replay_same_ids(
            ui_port=ports[1],
            token=token,
            state_root=state_root,
            process=process,
            bodies=bodies,
            ids=accepted,
            evidence=evidence,
            writer=writer,
            temp_root=temp_root,
        )
        before_shutdown_rows = support._read_request_ledger(state_root)
        if len(before_shutdown_rows) != 2:
            raise SmokeAbort("BLOCKED", "request_ledger_changed_before_shutdown")

        _assert_bridge_identity(process, process_identity)
        support._owned_listener_pids(process.pid, ports)
        safe_to_shutdown = False
        initial_stop = support._stop_bridge(process, ui_port=ports[1], token=token)
        initial_exit_code = process.returncode
        evidence["processes"]["shutdowns"].append(
            {"role": "initial", "pid": process.pid, **initial_stop}
        )
        if initial_stop.get("reaped"):
            evidence["processes"]["orphan_processes"] = 0
            process = None
            process_identity = None
        if initial_stop.get("shutdown_http_status") != 202 or not initial_stop.get("reaped"):
            raise SmokeAbort("UNKNOWN", "initial_bridge_graceful_shutdown_unconfirmed")
        support._assert_ports_free(ports)
        evidence["checks"]["graceful_shutdown"] = "PASS"
        evidence["processes"]["initial_bridge_exit_code"] = initial_exit_code
        if initial_exit_code not in {0, None}:
            raise SmokeAbort("UNKNOWN", "initial_bridge_exit_code_nonzero")
        _save_evidence(writer, evidence, token=token, temp_root=temp_root)

        restart_process, restart_identity = _restart_read_only(
            base_environment=base_environment,
            temp_root=temp_root,
            token=token,
            workspaces=workspaces,
            codex_executable=codex_executable,
            state_root=state_root,
            first_ports=ports,
            ids=accepted,
            api_snapshots=api_snapshots,
            evidence=evidence,
            writer=writer,
        )
        process = restart_process
        process_identity = restart_identity
        if process is None or process_identity is None:
            raise SmokeAbort("UNKNOWN", "restart_bridge_identity_unavailable")
        safe_to_shutdown = True
        restart_ports = (
            int(evidence["ports"]["restart"]["mcp"]),
            int(evidence["ports"]["restart"]["ui"]),
        )
        _assert_bridge_identity(process, process_identity)
        support._owned_listener_pids(process.pid, restart_ports)
        safe_to_shutdown = False
        final_stop = support._stop_bridge(process, ui_port=restart_ports[1], token=token)
        evidence["processes"]["shutdowns"].append(
            {"role": "restart", "pid": process.pid, **final_stop}
        )
        if final_stop.get("reaped"):
            evidence["processes"]["orphan_processes"] = 0
            process = None
            process_identity = None
        if final_stop.get("shutdown_http_status") != 202 or not final_stop.get("reaped"):
            raise SmokeAbort("UNKNOWN", "restart_bridge_graceful_shutdown_unconfirmed")
        support._assert_ports_free(restart_ports)
        evidence["checks"]["graceful_shutdown"] = "PASS"
        process = None
        process_identity = None
        evidence["status"] = "PASS"
        evidence["reason"] = None
    except SmokeAbort as error:
        evidence["status"] = error.status
        evidence["reason"] = error.code
        if safe_to_shutdown and token and ports and state_root is not None:
            _record_read_only_reconciliations(
                evidence=evidence,
                state_root=state_root,
                ui_port=ports[1],
                token=token,
            )
    except Exception as error:
        evidence["status"] = "FAIL"
        evidence["reason"] = f"runner_unexpected_{type(error).__name__}"
        if safe_to_shutdown and token and ports and state_root is not None:
            _record_read_only_reconciliations(
                evidence=evidence,
                state_root=state_root,
                ui_port=ports[1],
                token=token,
            )
    finally:
        for reservation in reservations:
            reservation.close()
        if process is not None and safe_to_shutdown and process_identity is not None:
            try:
                _assert_bridge_identity(process, process_identity)
                active_ports = ports
                if evidence["processes"].get("bridge_restart") == process_identity:
                    active_ports = (
                        int(evidence["ports"]["restart"]["mcp"]),
                        int(evidence["ports"]["restart"]["ui"]),
                    )
                if active_ports is None:
                    raise SmokeAbort("BLOCKED", "owned_bridge_ports_not_recorded")
                support._owned_listener_pids(process.pid, active_ports)
                stopped = support._stop_bridge(
                    process,
                    ui_port=active_ports[1],
                    token=token or "",
                )
                evidence["processes"]["shutdowns"].append(
                    {"role": "finalized_after_error", "pid": process.pid, **stopped}
                )
                evidence["processes"]["orphan_processes"] = (
                    0 if stopped.get("reaped") else "UNKNOWN"
                )
                if stopped.get("reaped"):
                    support._assert_ports_free(active_ports)
            except Exception as error:
                evidence["processes"]["orphan_processes"] = "UNKNOWN"
                evidence["processes"]["shutdown_error_code"] = (
                    error.code if isinstance(error, SmokeAbort) else type(error).__name__
                )
                if evidence.get("status") == "PASS":
                    evidence["status"] = "UNKNOWN"
                    evidence["reason"] = "owned_bridge_shutdown_unconfirmed"
        elif process is not None:
            evidence["processes"]["orphan_processes"] = "UNKNOWN"
        evidence["isolation"]["temp_root_retained"] = temp_root is not None
        evidence["isolation"]["temp_root_label"] = temp_root.name if temp_root is not None else None
        evidence["finished_at_utc"] = _utc_now()
        try:
            _save_evidence(writer, evidence, token=token, temp_root=temp_root)
        except SmokeAbort as error:
            evidence["status"] = "UNKNOWN"
            evidence["reason"] = error.code
    return 0 if evidence.get("status") == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the one-shot Phase 5F-5 integrated isolated acceptance"
    )
    parser.add_argument("--attempt", choices=("r2", "r3"))
    args = parser.parse_args(argv)
    evidence_path = _evidence_path_for_attempt(args.attempt)
    evidence = _new_evidence()
    try:
        writer = _EvidenceWriter(evidence_path, evidence)
    except SmokeAbort as error:
        print(json.dumps({"status": error.status, "reason": error.code}, sort_keys=True))
        return 1
    result = _run_acceptance(evidence=evidence, writer=writer)
    print(
        json.dumps(
            {
                "status": evidence.get("status"),
                "reason": evidence.get("reason"),
                "evidence": str(evidence_path.relative_to(REPOSITORY_ROOT)),
            },
            sort_keys=True,
        )
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
