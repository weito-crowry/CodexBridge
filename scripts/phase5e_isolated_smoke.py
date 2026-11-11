from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psutil
import yaml

try:
    from .phase5e_smoke_support import (
        PRODUCTION_PORTS,
        build_isolated_environment,
        ports_are_safe,
        roots_are_separate,
    )
except ImportError:
    from phase5e_smoke_support import (
        PRODUCTION_PORTS,
        build_isolated_environment,
        ports_are_safe,
        roots_are_separate,
    )

from codex_bridge.codex_resolver import resolve_codex_executable
from codex_bridge.config import BridgeConfig
from codex_bridge.server import prepare_config
from codex_bridge.workflows.interpreter import SAFE_CODEX_JOB_PROMPT, validate_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPOSITORY_ROOT / "poc" / "ows" / "fixtures" / "codex_job_workflow.yaml"
EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5e-0-evidence.json"
BRIDGE_PORT_HOST = "127.0.0.1"
BRIDGE_START_TIMEOUT_SECONDS = 35.0
WORKFLOW_TIMEOUT_SECONDS = 150.0
HTTP_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 30.0
MAX_HTTP_BODY_BYTES = 1_048_576


class SmokeAbort(RuntimeError):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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
            f"host = {_toml_string(BRIDGE_PORT_HOST)}",
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
            'name = "Local PC"',
            'kind = "local"',
            "",
        )
    )
    path.write_text(contents, encoding="utf-8", newline="\n")


def _reserve_loopback_ports() -> tuple[tuple[int, int], list[socket.socket]]:
    reservations: list[socket.socket] = []
    ports: list[int] = []
    try:
        while len(reservations) < 2:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                reservation.bind((BRIDGE_PORT_HOST, 0))
            except OSError:
                reservation.close()
                raise SmokeAbort("BLOCKED", "loopback_port_reservation_failed") from None
            port = int(reservation.getsockname()[1])
            if port in PRODUCTION_PORTS or port in ports:
                reservation.close()
                continue
            reservations.append(reservation)
            ports.append(port)
        if not ports_are_safe(ports[0], ports[1]):
            raise SmokeAbort("BLOCKED", "loopback_port_pair_invalid")
        return (ports[0], ports[1]), reservations
    except BaseException:
        for reservation in reservations:
            reservation.close()
        raise


def _load_existing_evidence(path: Path) -> None:
    if not path.exists():
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "evidence_path_is_not_readable") from None
    if not isinstance(value, dict) or value.get("phase") != "5E-0":
        raise SmokeAbort("BLOCKED", "evidence_path_already_exists")
    if value.get("live_post_attempted") is True:
        raise SmokeAbort("BLOCKED", "a_live_post_was_already_attempted")
    raise SmokeAbort("BLOCKED", "evidence_path_already_exists")


def _save_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _copy_authentication(source_home: Path, isolated_home: Path) -> Path:
    source = source_home / "auth.json"
    if source.is_symlink() or not source.is_file():
        raise SmokeAbort("BLOCKED", "codex_authentication_file_unavailable")
    if isolated_home.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_codex_home_is_a_symlink")
    isolated_home.mkdir(parents=True, exist_ok=True)
    destination = isolated_home / "auth.json"
    if destination.exists() or destination.is_symlink():
        raise SmokeAbort("BLOCKED", "isolated_codex_authentication_already_exists")
    try:
        shutil.copyfile(source, destination)
    except OSError:
        destination.unlink(missing_ok=True)
        isolated_home.rmdir()
        raise SmokeAbort("BLOCKED", "isolated_codex_authentication_copy_failed") from None
    return destination


def _source_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _process_snapshot(pid: int) -> list[tuple[int, float]]:
    try:
        root = psutil.Process(pid)
        children = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []
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


def _owned_listener_pids(pid: int, ports: tuple[int, int]) -> dict[int, set[int]]:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        raise SmokeAbort("BLOCKED", "listener_ownership_could_not_be_verified") from None
    test_process_tree = {pid}
    test_process_tree.update(child_pid for child_pid, _ in _process_snapshot(pid))
    owners = {port: set() for port in ports}
    for connection in connections:
        address = connection.laddr
        if connection.status != psutil.CONN_LISTEN or not address or address.port not in owners:
            continue
        if address.ip != BRIDGE_PORT_HOST:
            raise SmokeAbort("BLOCKED", "bridge_listener_is_not_loopback_only")
        if connection.pid is not None:
            owners[address.port].add(connection.pid)
    mismatches = [
        f"{port}={'-'.join(str(owner) for owner in sorted(owner_set)) or 'none'}"
        for port, owner_set in owners.items()
        if not owner_set or not owner_set.issubset(test_process_tree)
    ]
    if mismatches:
        raise SmokeAbort(
            "BLOCKED",
            f"bridge_listener_owner_outside_test_process_tree_{pid}_" + "_".join(mismatches),
        )
    return owners


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
        raise SmokeAbort("BLOCKED", "child_process_tree_could_not_be_verified") from None
    count = 0
    for child in children:
        try:
            command = child.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "codex_bridge.workflows.worker_entry" in command:
            count += 1
    return count


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
        "Origin": f"http://{BRIDGE_PORT_HOST}:{ui_port}",
        "Host": f"{BRIDGE_PORT_HOST}:{ui_port}",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(BRIDGE_PORT_HOST, ui_port, timeout=timeout)
    try:
        connection.request(method, route, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_HTTP_BODY_BYTES + 1)
        if len(raw) > MAX_HTTP_BODY_BYTES:
            raise SmokeAbort("BLOCKED", "ui_api_response_exceeded_size_limit")
        try:
            value = json.loads(raw) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        return response.status, value
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
    status_code, health = _http_json(ui_port, "GET", "/healthz", timeout=2.0)
    if status_code != 200 or not isinstance(health, dict) or health.get("status") != "ok":
        raise SmokeAbort("BLOCKED", "isolated_bridge_health_check_failed")
    status_code, bridge_status = _http_json(ui_port, "GET", "/ui-api/status", timeout=2.0)
    if (
        status_code != 200
        or not isinstance(bridge_status, dict)
        or bridge_status.get("bridge") != "ready"
        or bridge_status.get("app_server") != "ready"
        or bridge_status.get("ui_host") != BRIDGE_PORT_HOST
        or bridge_status.get("ui_port") != ui_port
    ):
        raise SmokeAbort("BLOCKED", "isolated_appserver_status_check_failed")
    status_code, capabilities = _http_json(
        ui_port,
        "GET",
        "/ui-api/control/capabilities",
        token=token,
    )
    if status_code != 200 or not isinstance(capabilities, dict):
        raise SmokeAbort("BLOCKED", "control_capabilities_authentication_failed")
    status_code, _ = _http_json(ui_port, "GET", "/ui-api/control/workflows")
    if status_code != 403:
        raise SmokeAbort("BLOCKED", "missing_bearer_token_was_not_rejected")
    wrong_token = f"wrong-{uuid.uuid4().hex}"
    status_code, _ = _http_json(
        ui_port,
        "GET",
        "/ui-api/control/workflows",
        token=wrong_token,
    )
    if status_code != 403:
        raise SmokeAbort("BLOCKED", "incorrect_bearer_token_was_not_rejected")
    appserver_pid = _codex_appserver_pid(process.pid, expected_codex)
    return {
        "bridge_pid": process.pid,
        "appserver_pid": appserver_pid,
        "ports_owned_by_bridge": True,
        "capabilities_authenticated": True,
        "missing_token_rejected": True,
        "incorrect_token_rejected": True,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise SmokeAbort("BLOCKED", "expected_sqlite_artifact_is_unavailable")
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error:
        raise SmokeAbort("BLOCKED", "sqlite_read_only_open_failed") from None


def _read_run_state(
    state_root: Path,
    *,
    client_request_id: str,
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    request_path = state_root / "requests.sqlite"
    if not request_path.is_file():
        return {"request_count": 0, "request": None}
    connection = _read_only_connection(request_path)
    try:
        rows = connection.execute(
            "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests "
            "ORDER BY created_at"
        ).fetchall()
    except sqlite3.Error:
        raise SmokeAbort("BLOCKED", "workflow_request_ledger_read_failed") from None
    finally:
        connection.close()
    request_rows = [dict(row) for row in rows if row["client_request_id"] == client_request_id]
    if len(rows) > 1 or len(request_rows) > 1:
        raise SmokeAbort("BLOCKED", "isolated_request_ledger_has_unexpected_rows")
    request = request_rows[0] if request_rows else None
    if request is not None:
        workflow_id = str(request["workflow_id"])
        run_id = str(request["run_id"])
    result: dict[str, Any] = {
        "request_count": len(rows),
        "request": request,
    }
    if workflow_id is None or run_id is None:
        return result

    run_dir = state_root / run_id
    jobs_path = run_dir / "codex_jobs.sqlite"
    jobs: list[sqlite3.Row] = []
    if jobs_path.exists() or jobs_path.is_symlink():
        jobs_connection = _read_only_connection(jobs_path)
        try:
            jobs = jobs_connection.execute(
                "SELECT job_id, workflow_id, state, thread_id, turn_id FROM jobs"
            ).fetchall()
        except sqlite3.Error:
            raise SmokeAbort("BLOCKED", "codex_job_ledger_read_failed") from None
        finally:
            jobs_connection.close()
    if len(jobs) > 1:
        raise SmokeAbort("BLOCKED", "isolated_codex_job_ledger_has_multiple_rows")
    job = None if not jobs else dict(jobs[0])
    job_events: list[sqlite3.Row] = []
    if isinstance(job, dict):
        events_connection = _read_only_connection(jobs_path)
        try:
            job_events = events_connection.execute(
                "SELECT state FROM job_events WHERE job_id = ? ORDER BY event_id",
                (job["job_id"],),
            ).fetchall()
        except sqlite3.Error:
            raise SmokeAbort("BLOCKED", "codex_job_event_ledger_read_failed") from None
        finally:
            events_connection.close()
    post_claim_count = sum(event["state"] == "MAYBE_SENT" for event in job_events)

    dbos_path = run_dir / "dbos.sqlite"
    dbos_rows: list[sqlite3.Row] = []
    if dbos_path.exists() or dbos_path.is_symlink():
        dbos_connection = _read_only_connection(dbos_path)
        try:
            dbos_rows = dbos_connection.execute(
                "SELECT workflow_uuid, status FROM workflow_status"
            ).fetchall()
        except sqlite3.Error:
            raise SmokeAbort("BLOCKED", "dbos_status_read_failed") from None
        finally:
            dbos_connection.close()
    if len(dbos_rows) > 1:
        raise SmokeAbort("BLOCKED", "isolated_dbos_database_has_multiple_workflows")
    dbos_status = None if not dbos_rows else str(dbos_rows[0]["status"]).upper()
    if dbos_rows and dbos_rows[0]["workflow_uuid"] != workflow_id:
        raise SmokeAbort("BLOCKED", "dbos_workflow_identity_does_not_match")

    result_path = run_dir / "result.json"
    saved_result: Any = None
    if result_path.is_file() and not result_path.is_symlink():
        try:
            saved_result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise SmokeAbort("BLOCKED", "saved_workflow_result_read_failed") from None
    result.update(
        {
            "job_count": len(jobs),
            "job": job,
            "job_event_count": len(job_events),
            "post_claim_count": post_claim_count,
            "dbos_status": dbos_status,
            "saved_result": saved_result,
        }
    )
    return result


def _values_for_key(value: Any, key: str) -> list[Any]:
    matches: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            matches.append(value[key])
        for child in value.values():
            matches.extend(_values_for_key(child, key))
    elif isinstance(value, list):
        for child in value:
            matches.extend(_values_for_key(child, key))
    return matches


def _thread_ids(ui_port: int, token: str) -> set[str]:
    status_code, payload = _http_json(
        ui_port,
        "GET",
        "/ui-api/threads?limit=100",
        token=token,
    )
    if status_code != 200 or not isinstance(payload, dict):
        raise SmokeAbort("BLOCKED", "codex_thread_list_read_failed")
    raw_threads = payload.get("threads")
    if not isinstance(raw_threads, list):
        raise SmokeAbort("BLOCKED", "codex_thread_list_shape_invalid")
    identifiers: set[str] = set()
    for thread in raw_threads:
        if not isinstance(thread, dict):
            raise SmokeAbort("BLOCKED", "codex_thread_list_shape_invalid")
        identifier = thread.get("thread_id", thread.get("id"))
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    return identifiers


def _assert_worker_completed(run_dir: Path) -> None:
    status_path = run_dir / "worker-entry-status.json"
    if status_path.is_symlink() or not status_path.is_file():
        raise SmokeAbort("BLOCKED", "workflow_worker_terminal_status_unavailable")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "workflow_worker_terminal_status_unreadable") from None
    if (
        not isinstance(status, dict)
        or status.get("state") != "completed"
        or status.get("exit_code") != 0
    ):
        raise SmokeAbort("BLOCKED", "workflow_worker_did_not_exit_successfully")


def _worker_artifact_snapshot(run_dir: Path) -> tuple[tuple[str, int, int, str], ...]:
    names = {
        "worker-entry-status.json",
        "initial-worker-heartbeat.jsonl",
        "task-events.jsonl",
        "start-worker.json",
        "start-worker-identity.json",
    }
    paths = [run_dir / name for name in names]
    paths.extend(run_dir.glob("worker-config-*.json"))
    paths.extend(run_dir.glob("worker-shutdown-*.request"))
    snapshot: list[tuple[str, int, int, str]] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise SmokeAbort("BLOCKED", "workflow_worker_artifact_is_not_regular")
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise SmokeAbort("BLOCKED", "workflow_worker_artifact_read_failed") from None
        snapshot.append((path.name, stat.st_size, stat.st_mtime_ns, digest))
    return tuple(sorted(snapshot))


def _read_workflow_views(
    ui_port: int,
    token: str,
    workflow_id: str,
) -> tuple[tuple[int, Any], tuple[int, Any], tuple[int, Any]]:
    quoted = quote(workflow_id, safe="")
    listing = _http_json(ui_port, "GET", "/ui-api/control/workflows?limit=100", token=token)
    detail = _http_json(
        ui_port,
        "GET",
        f"/ui-api/control/workflows/{quoted}",
        token=token,
    )
    result = _http_json(
        ui_port,
        "GET",
        f"/ui-api/control/workflows/{quoted}/result",
        token=token,
    )
    return listing, detail, result


def _wait_for_success(
    ui_port: int,
    token: str,
    workflow_id: str,
    *,
    timeout_seconds: float = WORKFLOW_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "not_observed"
    while time.monotonic() < deadline:
        listing, detail, result = _read_workflow_views(ui_port, token, workflow_id)
        if listing[0] != 200 or not isinstance(listing[1], dict):
            raise SmokeAbort("BLOCKED", "workflow_list_read_failed")
        if detail[0] != 200 or not isinstance(detail[1], dict):
            raise SmokeAbort("BLOCKED", "workflow_detail_read_failed")
        if not isinstance(result[1], dict):
            raise SmokeAbort("BLOCKED", "workflow_result_shape_invalid")
        detail_payload = detail[1]
        workflow_status = detail_payload.get("status")
        last_status = workflow_status if isinstance(workflow_status, str) else "invalid"
        if workflow_status in {"failed", "failed_to_start"}:
            raise SmokeAbort("FAIL", "workflow_ended_unsuccessfully")
        if workflow_status == "succeeded" and result[0] == 200:
            list_payload = listing[1]
            workflows = list_payload.get("workflows")
            if not isinstance(workflows, list):
                raise SmokeAbort("BLOCKED", "workflow_list_shape_invalid")
            matching = [
                item
                for item in workflows
                if isinstance(item, dict) and item.get("workflow_id") == workflow_id
            ]
            if len(matching) != 1:
                raise SmokeAbort("BLOCKED", "workflow_list_identity_mismatch")
            return matching[0], detail_payload, result[1]
        if result[0] not in {200, 202, 404}:
            raise SmokeAbort("BLOCKED", "workflow_result_requires_reconciliation")
        time.sleep(0.5)
    if last_status in {"needs_reconciliation", "unknown"}:
        raise SmokeAbort("BLOCKED", f"workflow_status_{last_status}_timeout")
    raise SmokeAbort("BLOCKED", "workflow_completion_timeout")


def _assert_success_identity(
    *,
    workflow_id: str,
    run_id: str,
    list_item: dict[str, Any],
    detail: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, str]:
    job = detail.get("job_id")
    thread = detail.get("thread_id")
    if (
        not isinstance(job, str)
        or not job
        or not isinstance(thread, str)
        or not thread
        or detail.get("workflow_id") != workflow_id
        or detail.get("run_id") != run_id
        or detail.get("status") != "succeeded"
        or list_item.get("workflow_id") != workflow_id
        or list_item.get("status") != "succeeded"
        or list_item.get("job_id") != job
        or list_item.get("thread_id") != thread
        or result.get("workflow_id") != workflow_id
        or _values_for_key(result, "final_text") != ["4"]
    ):
        raise SmokeAbort("BLOCKED", "workflow_api_identity_or_result_mismatch")
    nested_jobs = _values_for_key(result, "job_id")
    nested_threads = _values_for_key(result, "thread_id")
    if set(nested_jobs) != {job}:
        raise SmokeAbort("BLOCKED", "workflow_result_job_identity_mismatch")
    if set(nested_threads) != {thread}:
        raise SmokeAbort("BLOCKED", "workflow_result_thread_identity_mismatch")
    return job, thread


def _assert_direct_success(
    state: dict[str, Any],
    *,
    client_request_id: str,
    workflow_id: str,
    run_id: str,
    job_id: str,
    thread_id: str,
) -> None:
    request = state.get("request")
    job = state.get("job")
    saved_result = state.get("saved_result")
    if (
        state.get("request_count") != 1
        or not isinstance(request, dict)
        or request.get("client_request_id") != client_request_id
        or request.get("workflow_id") != workflow_id
        or request.get("run_id") != run_id
        or state.get("job_count") != 1
        or state.get("post_claim_count") != 1
        or not isinstance(job, dict)
        or job.get("job_id") != job_id
        or job.get("workflow_id") != workflow_id
        or job.get("state") != "SUCCEEDED"
        or job.get("thread_id") != thread_id
        or state.get("dbos_status") not in {"SUCCESS", "SUCCEEDED"}
        or not isinstance(saved_result, dict)
        or saved_result.get("workflow_id") != workflow_id
        or _values_for_key(saved_result, "final_text") != ["4"]
    ):
        raise SmokeAbort("BLOCKED", "durable_state_does_not_match_workflow_result")


def _reconcile_after_unknown(
    state_root: Path,
    client_request_id: str,
    evidence: dict[str, Any],
    *,
    ui_port: int,
    token: str,
) -> None:
    try:
        state = _read_run_state(state_root, client_request_id=client_request_id)
    except SmokeAbort:
        evidence["reconciliation"] = {"read_only_state": "unavailable"}
        return
    request = state.get("request")
    if isinstance(request, dict):
        workflow_id = request.get("workflow_id")
        run_id = request.get("run_id")
        evidence["workflow"].update(
            {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": request.get("state"),
            }
        )
        if isinstance(workflow_id, str) and isinstance(run_id, str):
            try:
                state = _read_run_state(
                    state_root,
                    client_request_id=client_request_id,
                    workflow_id=workflow_id,
                    run_id=run_id,
                )
            except SmokeAbort:
                pass
    job = state.get("job")
    thread_status: dict[str, Any] = {"status": "unavailable"}
    thread_id = job.get("thread_id") if isinstance(job, dict) else None
    if isinstance(thread_id, str) and thread_id:
        try:
            status_code, payload = _http_json(
                ui_port,
                "GET",
                f"/ui-api/threads/{quote(thread_id, safe='')}/status",
                token=token,
            )
        except SmokeAbort:
            status_code, payload = 0, None
        if status_code == 200 and isinstance(payload, dict):
            thread_status = {
                "status": "inspected",
                "http_status": status_code,
                "turn_state": payload.get("state"),
            }
        else:
            thread_status = {"status": "inspected", "http_status": status_code}
    evidence["reconciliation"] = {
        "read_only_state": "inspected",
        "request_count": state.get("request_count"),
        "request_state": None if not isinstance(request, dict) else request.get("state"),
        "job_count": state.get("job_count"),
        "job_state": job.get("state") if isinstance(job, dict) else None,
        "dbos_status": state.get("dbos_status"),
        "thread_read_only_status": thread_status,
    }


def _post_fixed_workflow(
    *,
    ui_port: int,
    token: str,
    state_root: Path,
    workspace_root: Path,
    evidence: dict[str, Any],
    evidence_path: Path,
) -> None:
    try:
        workflow_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise SmokeAbort("BLOCKED", "fixed_workflow_fixture_is_unavailable") from None
    try:
        workflow = yaml.safe_load(workflow_yaml)
        validate_workflow(workflow, codex_job_live=True)
    except (yaml.YAMLError, ValueError, TypeError):
        raise SmokeAbort("BLOCKED", "fixed_workflow_fixture_validation_failed") from None
    try:
        steps = workflow["do"]
        call = steps[0]["calculate"]
        prompt = call["with"]["prompt"]
        if (
            len(steps) != 2
            or call.get("call") != "codex-job"
            or prompt != SAFE_CODEX_JOB_PROMPT
            or "finish" not in steps[1]
        ):
            raise SmokeAbort("BLOCKED", "workflow_fixture_is_not_the_fixed_codex_job")
    except (KeyError, IndexError, TypeError):
        raise SmokeAbort("BLOCKED", "workflow_fixture_shape_is_invalid") from None

    client_request_id = f"phase5e0-{uuid.uuid4().hex}"
    payload = {
        "client_request_id": client_request_id,
        "workflow_yaml": workflow_yaml,
        "input": {},
        "workspace_root": str(workspace_root),
    }
    body = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence["workflow"] = {
        "client_request_id": client_request_id,
        "fixture_sha256": hashlib.sha256(workflow_yaml.encode("utf-8")).hexdigest(),
    }
    evidence["live_post_attempted"] = True
    evidence["checks"]["single_live_post"] = {"status": "BLOCKED", "detail": "outcome_pending"}
    _save_evidence(evidence_path, evidence)

    try:
        status_code, response = _http_json(
            ui_port,
            "POST",
            "/ui-api/control/workflows",
            token=token,
            body=body,
        )
    except SmokeAbort:
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        evidence["workflow"]["status"] = "needs_reconciliation"
        evidence["checks"]["single_live_post"] = {
            "status": "BLOCKED",
            "detail": "response_unknown_no_retry",
        }
        _save_evidence(evidence_path, evidence)
        raise SmokeAbort("BLOCKED", "initial_post_response_unknown_no_retry") from None

    if status_code != 202 or not isinstance(response, dict):
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        if status_code >= 500:
            evidence["checks"]["single_live_post"] = {
                "status": "BLOCKED",
                "http_status": status_code,
                "detail": "server_error_no_retry",
            }
            _save_evidence(evidence_path, evidence)
            raise SmokeAbort("BLOCKED", "initial_post_server_error_no_retry")
        evidence["checks"]["single_live_post"] = {
            "status": "FAIL",
            "http_status": status_code,
        }
        _save_evidence(evidence_path, evidence)
        raise SmokeAbort("FAIL", "initial_post_was_rejected")

    workflow_id = response.get("workflow_id")
    run_id = response.get("run_id")
    if not isinstance(workflow_id, str) or not isinstance(run_id, str):
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        evidence["checks"]["single_live_post"] = {
            "status": "BLOCKED",
            "detail": "invalid_response_no_retry",
        }
        _save_evidence(evidence_path, evidence)
        raise SmokeAbort("BLOCKED", "initial_post_response_identity_unknown")
    evidence["workflow"].update(
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "initial_status": response.get("status"),
        }
    )
    _save_evidence(evidence_path, evidence)

    try:
        list_item, detail, result = _wait_for_success(ui_port, token, workflow_id)
    except SmokeAbort:
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        _save_evidence(evidence_path, evidence)
        raise
    job_id, thread_id = _assert_success_identity(
        workflow_id=workflow_id,
        run_id=run_id,
        list_item=list_item,
        detail=detail,
        result=result,
    )
    direct_state = _read_run_state(
        state_root,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    _assert_direct_success(
        direct_state,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
        job_id=job_id,
        thread_id=thread_id,
    )
    thread_ids_before_replay = _thread_ids(ui_port, token)
    if thread_id not in thread_ids_before_replay:
        raise SmokeAbort("BLOCKED", "completed_codex_thread_not_visible_before_replay")
    if direct_state.get("post_claim_count") != 1:
        raise SmokeAbort("BLOCKED", "codex_job_post_claim_count_is_not_one")

    evidence["workflow"].update(
        {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "job_id": job_id,
            "thread_id": thread_id,
            "status": "succeeded",
            "final_text": "4",
            "job_state": direct_state["job"]["state"],
            "dbos_status": direct_state["dbos_status"],
        }
    )
    evidence["checks"]["single_live_post"] = {"status": "PASS", "http_status": 202}
    evidence["checks"]["workflow_api_identity"] = {"status": "PASS"}
    evidence["checks"]["durable_state"] = {"status": "PASS"}
    evidence["checks"]["fixed_result"] = {"status": "PASS", "final_text": "4"}
    evidence["replay"] = {
        "status": "BLOCKED",
        "job_rows_before": direct_state["job_count"],
        "visible_thread_count_before": len(thread_ids_before_replay),
    }
    _save_evidence(evidence_path, evidence)

    if (
        direct_state.get("request_count") != 1
        or direct_state.get("job_count") != 1
        or direct_state.get("dbos_status") not in {"SUCCESS", "SUCCEEDED"}
        or direct_state.get("job", {}).get("state") != "SUCCEEDED"
    ):
        raise SmokeAbort("BLOCKED", "idempotency_replay_precondition_failed")

    try:
        replay_status, replay_response = _http_json(
            ui_port,
            "POST",
            "/ui-api/control/workflows",
            token=token,
            body=body,
        )
    except SmokeAbort:
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        evidence["replay"] = {"status": "BLOCKED", "detail": "response_unknown_no_retry"}
        _save_evidence(evidence_path, evidence)
        raise SmokeAbort("BLOCKED", "same_id_replay_response_unknown_no_retry") from None
    if (
        replay_status != 202
        or not isinstance(replay_response, dict)
        or replay_response.get("workflow_id") != workflow_id
        or replay_response.get("run_id") != run_id
    ):
        _reconcile_after_unknown(
            state_root,
            client_request_id,
            evidence,
            ui_port=ui_port,
            token=token,
        )
        evidence["replay"] = {
            "status": "BLOCKED",
            "http_status": replay_status,
            "detail": "replay_response_mismatch_no_retry",
        }
        _save_evidence(evidence_path, evidence)
        raise SmokeAbort("BLOCKED", "same_id_replay_did_not_return_the_original_run")
    replay_state = _read_run_state(
        state_root,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    _assert_direct_success(
        replay_state,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
        job_id=job_id,
        thread_id=thread_id,
    )
    thread_ids_after_replay = _thread_ids(ui_port, token)
    if replay_state.get("job_count") != direct_state.get("job_count"):
        raise SmokeAbort("BLOCKED", "same_id_replay_created_an_additional_job")
    if (
        replay_state.get("job_event_count") != direct_state.get("job_event_count")
        or replay_state.get("post_claim_count") != direct_state.get("post_claim_count")
        or replay_state.get("post_claim_count") != 1
        or replay_state.get("job") != direct_state.get("job")
    ):
        raise SmokeAbort("BLOCKED", "same_id_replay_changed_codex_job_post_ledger")
    if thread_ids_after_replay != thread_ids_before_replay:
        raise SmokeAbort("BLOCKED", "same_id_replay_changed_visible_thread_ids")
    replay_list, replay_detail, replay_result = _read_workflow_views(ui_port, token, workflow_id)
    if (
        replay_list[0] != 200
        or replay_detail[0] != 200
        or replay_result[0] != 200
        or not isinstance(replay_list[1], dict)
        or not isinstance(replay_list[1].get("workflows"), list)
        or not isinstance(replay_detail[1], dict)
        or not isinstance(replay_result[1], dict)
    ):
        raise SmokeAbort("BLOCKED", "same_id_replay_readback_failed")
    _assert_success_identity(
        workflow_id=workflow_id,
        run_id=run_id,
        list_item=next(
            (
                item
                for item in replay_list[1]["workflows"]
                if isinstance(item, dict) and item.get("workflow_id") == workflow_id
            ),
            {},
        ),
        detail=replay_detail[1],
        result=replay_result[1],
    )
    evidence["replay"] = {
        "status": "PASS",
        "same_workflow_and_run": True,
        "job_rows_before": direct_state["job_count"],
        "job_rows_after": replay_state["job_count"],
        "job_events_before": direct_state["job_event_count"],
        "job_events_after": replay_state["job_event_count"],
        "codex_post_claims_before": direct_state["post_claim_count"],
        "codex_post_claims_after": replay_state["post_claim_count"],
        "visible_thread_count_before": len(thread_ids_before_replay),
        "visible_thread_count_after": len(thread_ids_after_replay),
        "visible_thread_ids_unchanged": True,
        "additional_codex_job_posts": 0,
    }
    evidence["checks"]["same_id_replay"] = {"status": "PASS"}
    evidence["checks"]["thread_count_after_replay"] = {"status": "PASS"}


def _resume_posted_run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    evidence_path = Path(args.evidence).expanduser().resolve(strict=True)
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "existing_evidence_is_unreadable") from None
    if not isinstance(evidence, dict):
        raise SmokeAbort("BLOCKED", "existing_evidence_shape_is_invalid")
    workflow = evidence.get("workflow")
    replay = evidence.get("replay")
    if (
        evidence.get("phase") != "5E-0"
        or evidence.get("status") != "BLOCKED"
        or evidence.get("live_post_attempted") is not True
        or not isinstance(workflow, dict)
        or not isinstance(replay, dict)
        or replay.get("status") != "NOT_RUN"
        or evidence.get("replay_attempted") is True
    ):
        raise SmokeAbort("BLOCKED", "evidence_is_not_an_unreplayed_posted_run")

    client_request_id = workflow.get("client_request_id")
    workflow_id = workflow.get("workflow_id")
    run_id = workflow.get("run_id")
    artifact_id = evidence.get("artifact_id")
    if (
        not isinstance(client_request_id, str)
        or not isinstance(workflow_id, str)
        or not isinstance(run_id, str)
        or not isinstance(artifact_id, str)
        or len(artifact_id) != 32
        or any(character not in "0123456789abcdef" for character in artifact_id)
    ):
        raise SmokeAbort("BLOCKED", "evidence_workflow_identity_is_invalid")

    temp_base = Path(tempfile.gettempdir()).resolve(strict=True)
    temp_root = temp_base / f"codexbridge-phase5e0-{artifact_id}"
    if temp_root.is_symlink() or not temp_root.is_dir():
        raise SmokeAbort("BLOCKED", "isolated_posted_run_root_is_unavailable")
    temp_root = temp_root.resolve(strict=True)
    if temp_root.parent != temp_base:
        raise SmokeAbort("BLOCKED", "isolated_posted_run_root_is_not_direct_temp_child")
    workspace_root = temp_root / "workspace"
    state_root = temp_root / "localappdata" / "CodexBridge" / "workflows"
    run_dir = state_root / run_id
    if (
        workspace_root.is_symlink()
        or not workspace_root.is_dir()
        or state_root.is_symlink()
        or not state_root.is_dir()
        or not roots_are_separate(state_root, workspace_root)
    ):
        raise SmokeAbort("BLOCKED", "isolated_posted_run_roots_are_invalid")
    resolved_temp_root = temp_root.resolve(strict=True)
    if workspace_root.resolve(strict=True).parent != resolved_temp_root or not state_root.resolve(
        strict=True
    ).is_relative_to(resolved_temp_root):
        raise SmokeAbort("BLOCKED", "isolated_posted_roots_resolve_outside_temp_root")
    protected_roots = (
        REPOSITORY_ROOT / ".state",
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "CodexBridge"
        / "workflows",
        Path("D:/CodexBridge"),
    )
    if any(not roots_are_separate(state_root, protected) for protected in protected_roots):
        raise SmokeAbort("BLOCKED", "isolated_posted_state_overlaps_protected_state")

    try:
        workflow_yaml = FIXTURE_PATH.read_text(encoding="utf-8")
    except OSError:
        raise SmokeAbort("BLOCKED", "fixed_workflow_fixture_is_unavailable") from None
    if hashlib.sha256(workflow_yaml.encode("utf-8")).hexdigest() != workflow.get("fixture_sha256"):
        raise SmokeAbort("BLOCKED", "fixed_workflow_fixture_changed_since_initial_post")

    initial_state = _read_run_state(
        state_root,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    job = initial_state.get("job")
    if not isinstance(job, dict):
        raise SmokeAbort("BLOCKED", "posted_run_job_ledger_is_unavailable")
    job_id = job.get("job_id")
    thread_id = job.get("thread_id")
    if not isinstance(job_id, str) or not isinstance(thread_id, str):
        raise SmokeAbort("BLOCKED", "posted_run_job_identity_is_invalid")
    _assert_direct_success(
        initial_state,
        client_request_id=client_request_id,
        workflow_id=workflow_id,
        run_id=run_id,
        job_id=job_id,
        thread_id=thread_id,
    )
    _assert_worker_completed(run_dir)
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SmokeAbort("BLOCKED", "posted_run_manifest_is_unavailable") from None
    if not isinstance(manifest, dict):
        raise SmokeAbort("BLOCKED", "posted_run_manifest_shape_is_invalid")
    manifest_workspace = manifest.get("workspace_root")
    if not isinstance(manifest_workspace, str) or manifest_workspace != str(
        workspace_root.resolve(strict=True)
    ):
        raise SmokeAbort("BLOCKED", "posted_run_workspace_does_not_match_initial_payload")
    worker_artifacts_before_restart = _worker_artifact_snapshot(run_dir)

    codex_resolution = resolve_codex_executable(config_executable=None)
    codex_executable = Path(codex_resolution.path).resolve(strict=True)
    if not codex_executable.is_file() or codex_executable.suffix.casefold() != ".exe":
        raise SmokeAbort("BLOCKED", "resolved_codex_is_not_a_windows_executable")
    token = secrets.token_urlsafe(32)
    copied_auth_path: Path | None = None
    reservations: list[socket.socket] = []
    process: subprocess.Popen[bytes] | None = None
    process_results: list[dict[str, Any]] = []
    ports: tuple[int, int] | None = None
    readback: dict[str, Any] = {"status": "RUNNING", "post_attempted": False}
    previous_readback = evidence.get("readback_restart")
    if isinstance(previous_readback, dict) and previous_readback.get("status") == "BLOCKED":
        history = evidence.setdefault("readback_restart_history", [])
        if isinstance(history, list):
            previous_ports = evidence.get("ports", {})
            history.append(
                {
                    "status": previous_readback.get("status"),
                    "block_reason": previous_readback.get("block_reason"),
                    "ports": (
                        previous_ports.get("readback_restart")
                        if isinstance(previous_ports, dict)
                        else None
                    ),
                    "workflow_api_readback": "PASS_before_additional_thread_status_check",
                    "same_id_replay_post_attempted": previous_readback.get("post_attempted")
                    is True,
                }
            )
    evidence["readback_restart"] = readback
    evidence["resumed_at_utc"] = _utc_now()
    exit_code = 2
    try:
        source_home = _source_codex_home()
        copied_auth_path = _copy_authentication(source_home, temp_root / "codex_home")
        ports, reservations = _reserve_loopback_ports()
        mcp_port, ui_port = ports
        child_env = build_isolated_environment(
            dict(os.environ),
            temp_root=temp_root,
            config_path=temp_root / "bridge.toml",
            control_token=token,
        )
        config_path = temp_root / "bridge.toml"
        if config_path.is_symlink():
            raise SmokeAbort("BLOCKED", "isolated_bridge_config_is_a_symlink")
        _write_bridge_config(
            config_path,
            mcp_port=mcp_port,
            ui_port=ui_port,
            workspace_root=workspace_root,
            codex_executable=codex_executable,
        )
        _validate_fixture_config(
            child_env,
            workspace_root=workspace_root,
            mcp_port=mcp_port,
            ui_port=ui_port,
            codex_executable=codex_executable,
        )
        evidence["ports"]["readback_restart"] = {"mcp": mcp_port, "ui": ui_port}
        _save_evidence(evidence_path, evidence)

        for reservation in reservations:
            reservation.close()
        reservations.clear()
        process = _start_bridge(child_env)
        _wait_for_bridge(process, ui_port)
        preflight = _preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        workers_before_replay = _worker_process_count(process.pid)
        if workers_before_replay != 0:
            raise SmokeAbort("BLOCKED", "readback_restart_started_a_workflow_worker")
        worker_artifacts_after_restart = _worker_artifact_snapshot(run_dir)
        if worker_artifacts_after_restart != worker_artifacts_before_restart:
            raise SmokeAbort("BLOCKED", "readback_restart_changed_worker_execution_artifacts")

        list_item, detail, result = _wait_for_success(ui_port, token, workflow_id)
        api_job_id, api_thread_id = _assert_success_identity(
            workflow_id=workflow_id,
            run_id=run_id,
            list_item=list_item,
            detail=detail,
            result=result,
        )
        if api_job_id != job_id or api_thread_id != thread_id:
            raise SmokeAbort("BLOCKED", "readback_restart_job_identity_changed")
        thread_ids_before_replay = _thread_ids(ui_port, token)
        if thread_id not in thread_ids_before_replay:
            raise SmokeAbort("BLOCKED", "readback_restart_thread_is_not_visible")
        state_before_replay = _read_run_state(
            state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        _assert_direct_success(
            state_before_replay,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            job_id=job_id,
            thread_id=thread_id,
        )
        if state_before_replay.get("post_claim_count") != 1:
            raise SmokeAbort("BLOCKED", "posted_run_codex_post_claim_count_is_not_one")

        payload = {
            "client_request_id": client_request_id,
            "workflow_yaml": workflow_yaml,
            "input": {},
            "workspace_root": str(workspace_root),
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        readback["status"] = "PASS"
        readback["bridge_pid"] = preflight["bridge_pid"]
        readback["appserver_pid"] = preflight["appserver_pid"]
        readback["workflow_workers_before_replay"] = workers_before_replay
        evidence["restart"] = {
            "status": "PASS",
            "bridge_pid": preflight["bridge_pid"],
            "appserver_pid": preflight["appserver_pid"],
            "workflow_workers_started": workers_before_replay,
            "read_only_list_detail_result": "PASS",
            "job_rows_after_restart": state_before_replay["job_count"],
            "dbos_status_after_restart": state_before_replay["dbos_status"],
            "worker_artifacts_unchanged": True,
        }
        evidence["reconciliation"]["post_restart_api_status"] = "succeeded"
        evidence["reconciliation"]["worker_exit_status"] = "completed_exit_0"
        evidence["workflow"].update(
            {
                "job_id": job_id,
                "thread_id": thread_id,
                "status": "succeeded",
                "final_text": "4",
                "job_state": state_before_replay["job"]["state"],
                "dbos_status": state_before_replay["dbos_status"],
            }
        )
        evidence["checks"]["single_live_post"] = {"status": "PASS", "http_status": 202}
        evidence["checks"]["workflow_api_identity"] = {"status": "PASS"}
        evidence["checks"]["durable_state"] = {"status": "PASS"}
        evidence["checks"]["fixed_result"] = {"status": "PASS", "final_text": "4"}
        evidence["checks"]["restart_readback"] = {"status": "PASS"}
        evidence["initial_poll_outcome"] = "runner_stopped_on_unknown_or_needs_reconciliation"
        evidence["replay"] = {
            "status": "BLOCKED",
            "post_attempted": True,
            "job_rows_before": state_before_replay["job_count"],
            "job_events_before": state_before_replay["job_event_count"],
            "codex_post_claims_before": state_before_replay["post_claim_count"],
            "visible_thread_count_before": len(thread_ids_before_replay),
        }
        evidence["replay_attempted"] = True
        readback["replay_post_attempted"] = True
        _save_evidence(evidence_path, evidence)

        try:
            replay_status, replay_response = _http_json(
                ui_port,
                "POST",
                "/ui-api/control/workflows",
                token=token,
                body=body,
            )
        except SmokeAbort:
            evidence["replay"]["detail"] = "response_unknown_no_retry"
            evidence["replay"]["status"] = "BLOCKED"
            raise SmokeAbort("BLOCKED", "same_id_replay_response_unknown_no_retry") from None
        if (
            replay_status != 202
            or not isinstance(replay_response, dict)
            or replay_response.get("workflow_id") != workflow_id
            or replay_response.get("run_id") != run_id
        ):
            evidence["replay"].update(
                {
                    "http_status": replay_status,
                    "detail": "replay_response_mismatch_no_retry",
                }
            )
            raise SmokeAbort("BLOCKED", "same_id_replay_did_not_return_the_original_run")

        replay_list_item, replay_detail, replay_result = _wait_for_success(
            ui_port, token, workflow_id
        )
        _assert_success_identity(
            workflow_id=workflow_id,
            run_id=run_id,
            list_item=replay_list_item,
            detail=replay_detail,
            result=replay_result,
        )
        state_after_replay = _read_run_state(
            state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        _assert_direct_success(
            state_after_replay,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            job_id=job_id,
            thread_id=thread_id,
        )
        thread_ids_after_replay = _thread_ids(ui_port, token)
        workers_after_replay = _worker_process_count(process.pid)
        if (
            state_after_replay.get("job_count") != state_before_replay.get("job_count")
            or state_after_replay.get("job_event_count")
            != state_before_replay.get("job_event_count")
            or state_after_replay.get("post_claim_count")
            != state_before_replay.get("post_claim_count")
            or state_after_replay.get("job") != state_before_replay.get("job")
            or thread_ids_after_replay != thread_ids_before_replay
            or workers_after_replay != 0
            or _worker_artifact_snapshot(run_dir) != worker_artifacts_after_restart
        ):
            raise SmokeAbort("BLOCKED", "same_id_replay_changed_codex_job_or_thread_state")
        evidence["replay"] = {
            "status": "PASS",
            "post_attempted": True,
            "http_status": replay_status,
            "same_workflow_and_run": True,
            "job_rows_before": state_before_replay["job_count"],
            "job_rows_after": state_after_replay["job_count"],
            "job_events_before": state_before_replay["job_event_count"],
            "job_events_after": state_after_replay["job_event_count"],
            "codex_post_claims_before": state_before_replay["post_claim_count"],
            "codex_post_claims_after": state_after_replay["post_claim_count"],
            "visible_thread_count_before": len(thread_ids_before_replay),
            "visible_thread_count_after": len(thread_ids_after_replay),
            "visible_thread_ids_unchanged": True,
            "workflow_workers_after_replay": workers_after_replay,
            "additional_codex_job_posts": 0,
        }
        readback["workflow_workers_after_replay"] = workers_after_replay
        readback["worker_artifacts_unchanged"] = True
        evidence["checks"]["same_id_replay"] = {"status": "PASS"}
        evidence["checks"]["thread_count_after_replay"] = {"status": "PASS"}
        evidence["status"] = "PASS"
        evidence["production_side_effects"] = "no_existing_checkout_service_port_or_state_used"
        evidence.pop("block_reason", None)
        exit_code = 0
    except SmokeAbort as error:
        readback["status"] = error.status
        readback["block_reason"] = error.code
        evidence["status"] = error.status
        evidence["block_reason"] = error.code
    except Exception as error:
        readback["status"] = "BLOCKED"
        readback["block_reason"] = f"unexpected_{type(error).__name__}"
        evidence["status"] = "BLOCKED"
        evidence["block_reason"] = f"unexpected_{type(error).__name__}"
    finally:
        if process is not None:
            process_results.append(_stop_bridge(process, ui_port=ports[1], token=token))
            process = None
            if not process_results[-1]["reaped"] or process_results[-1]["forced_termination"]:
                evidence["status"] = "BLOCKED"
                evidence["block_reason"] = "readback_bridge_not_cleanly_reaped"
                readback["status"] = "BLOCKED"
                readback["block_reason"] = "readback_bridge_not_cleanly_reaped"
            else:
                _assert_ports_free(ports)
        for reservation in reservations:
            reservation.close()
        if copied_auth_path is not None:
            try:
                copied_auth_path.unlink(missing_ok=True)
                evidence["readback_auth_copy_removed"] = not copied_auth_path.exists()
            except OSError:
                evidence["readback_auth_copy_removed"] = False
        if process_results:
            old_cleanup = evidence.get("process_cleanup", {})
            old_cleanup = old_cleanup if isinstance(old_cleanup, dict) else {}
            evidence["process_cleanup"] = {
                "reaped": old_cleanup.get("reaped") is True
                and all(result["reaped"] for result in process_results),
                "forced_termination": old_cleanup.get("forced_termination") is True
                or any(result["forced_termination"] for result in process_results),
                "bridge_processes": int(old_cleanup.get("bridge_processes", 0))
                + len(process_results),
                "descendant_processes_observed": int(
                    old_cleanup.get("descendant_processes_observed", 0)
                )
                + sum(result["descendant_count"] for result in process_results),
            }
        evidence["ended_at_utc"] = _utc_now()
        _save_evidence(evidence_path, evidence)
    return exit_code, evidence


def _stop_bridge(
    process: subprocess.Popen[bytes] | None,
    *,
    ui_port: int,
    token: str,
) -> dict[str, Any]:
    if process is None:
        return {"reaped": True, "forced_termination": False, "descendant_count": 0}
    descendants = _process_snapshot(process.pid)
    if process.poll() is None:
        try:
            _http_json(
                ui_port,
                "POST",
                "/ui-api/control/shutdown",
                token=token,
                payload={},
                timeout=2.0,
            )
        except SmokeAbort:
            pass
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    forced = False
    if process.poll() is None:
        forced = True
        try:
            live_descendants = psutil.Process(process.pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            live_descendants = []
        for child in live_descendants:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if live_descendants:
            psutil.wait_procs(live_descendants, timeout=5.0)
        try:
            process.terminate()
            process.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    all_descendants_reaped = all(
        not _process_still_matches(pid, created_at) for pid, created_at in descendants
    )
    return {
        "reaped": process.poll() is not None and all_descendants_reaped,
        "forced_termination": forced,
        "descendant_count": len(descendants),
    }


def _assert_ports_free(ports: tuple[int, int]) -> None:
    sockets: list[socket.socket] = []
    try:
        for port in ports:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind((BRIDGE_PORT_HOST, port))
            sockets.append(reservation)
    except OSError:
        raise SmokeAbort("BLOCKED", "test_bridge_port_remained_in_use") from None
    finally:
        for reservation in sockets:
            reservation.close()


def _start_bridge(env: dict[str, str]) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "codex_bridge"],
            cwd=REPOSITORY_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_process_could_not_start") from None


def _validate_fixture_config(
    env: dict[str, str],
    *,
    workspace_root: Path,
    mcp_port: int,
    ui_port: int,
    codex_executable: Path,
) -> None:
    try:
        config = BridgeConfig.from_sources(environ=env)
        effective = prepare_config(config)
    except (OSError, ValueError):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_validation_failed") from None
    allowed_root = os.path.normcase(str(workspace_root.resolve(strict=True)))
    actual_roots = tuple(os.path.normcase(root) for root in effective.allowed_roots)
    if (
        effective.host != BRIDGE_PORT_HOST
        or effective.port != mcp_port
        or effective.ui_port != ui_port
        or actual_roots != (allowed_root,)
        or effective.control_token is None
        or effective.lan_enabled
        or effective.github_mcp.enabled
        or len(effective.targets) != 1
        or effective.targets[0].kind != "local"
        or os.path.normcase(effective.codex_executable)
        != os.path.normcase(str(codex_executable.resolve(strict=True)))
    ):
        raise SmokeAbort("BLOCKED", "isolated_bridge_config_does_not_match_scope")


def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    evidence_path = Path(args.evidence).expanduser().resolve(strict=False)
    _load_existing_evidence(evidence_path)
    started_at = _utc_now()
    artifact_id = uuid.uuid4().hex
    temp_root = Path(tempfile.gettempdir()) / f"codexbridge-phase5e0-{artifact_id}"
    temp_root.mkdir(parents=True, exist_ok=False)
    for directory in ("localappdata", "appdata", "tmp", "workspace"):
        (temp_root / directory).mkdir(parents=True, exist_ok=False)
    workspace_root = temp_root / "workspace"
    state_root = temp_root / "localappdata" / "CodexBridge" / "workflows"
    state_root.parent.mkdir(parents=True, exist_ok=True)
    if not roots_are_separate(state_root, workspace_root):
        raise SmokeAbort("BLOCKED", "state_and_workspace_roots_overlap")
    protected_roots = (
        REPOSITORY_ROOT / ".state",
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "CodexBridge"
        / "workflows",
        Path("D:/CodexBridge"),
    )
    for protected in protected_roots:
        if not roots_are_separate(state_root, protected):
            raise SmokeAbort("BLOCKED", "isolated_state_overlaps_protected_state")

    codex_resolution = resolve_codex_executable(config_executable=None)
    codex_executable = Path(codex_resolution.path).resolve(strict=True)
    if not codex_executable.is_file() or codex_executable.suffix.casefold() != ".exe":
        raise SmokeAbort("BLOCKED", "resolved_codex_is_not_a_windows_executable")

    source_home = _source_codex_home()
    copied_auth_path = _copy_authentication(source_home, temp_root / "codex_home")
    try:
        ports, reservations = _reserve_loopback_ports()
    except BaseException:
        copied_auth_path.unlink(missing_ok=True)
        copied_auth_path.parent.rmdir()
        raise
    mcp_port, ui_port = ports
    evidence: dict[str, Any] = {
        "phase": "5E-0",
        "status": "RUNNING",
        "started_at_utc": started_at,
        "ended_at_utc": None,
        "artifact_id": artifact_id,
        "ports": {"mcp": mcp_port, "ui": ui_port, "host": BRIDGE_PORT_HOST},
        "checks": {
            "environment_isolation": {"status": "PASS"},
            "port_preflight": {"status": "PASS"},
            "state_workspace_separation": {"status": "PASS"},
            "fixed_fixture": {"status": "UNKNOWN"},
            "bridge_api_preflight": {"status": "UNKNOWN"},
            "single_live_post": {"status": "UNKNOWN"},
            "workflow_api_identity": {"status": "UNKNOWN"},
            "durable_state": {"status": "UNKNOWN"},
            "fixed_result": {"status": "UNKNOWN"},
            "same_id_replay": {"status": "UNKNOWN"},
            "restart_readback": {"status": "UNKNOWN"},
            "console_rendering": {"status": "UNKNOWN"},
        },
        "live_post_attempted": False,
        "workflow": {},
        "replay": {"status": "NOT_RUN"},
        "restart": {"status": "NOT_RUN"},
        "process_cleanup": {"reaped": False, "forced_termination": False},
        "diagnostics": {"raw_logs_saved": False, "diagnostic_bytes_saved": 0},
        "production_side_effects": "no_existing_checkout_service_port_or_state_used",
    }
    token = secrets.token_urlsafe(32)
    try:
        child_env = build_isolated_environment(
            dict(os.environ),
            temp_root=temp_root,
            config_path=temp_root / "bridge.toml",
            control_token=token,
        )
        _write_bridge_config(
            temp_root / "bridge.toml",
            mcp_port=mcp_port,
            ui_port=ui_port,
            workspace_root=workspace_root,
            codex_executable=codex_executable,
        )
        _validate_fixture_config(
            child_env,
            workspace_root=workspace_root,
            mcp_port=mcp_port,
            ui_port=ui_port,
            codex_executable=codex_executable,
        )
        _save_evidence(evidence_path, evidence)
    except BaseException:
        for reservation in reservations:
            reservation.close()
        copied_auth_path.unlink(missing_ok=True)
        copied_auth_path.parent.rmdir()
        raise
    evidence["checks"]["fixed_fixture"] = {"status": "PASS"}
    _save_evidence(evidence_path, evidence)

    process_results: list[dict[str, Any]] = []
    try:
        for reservation in reservations:
            reservation.close()
        reservations.clear()
        process = _start_bridge(child_env)
        _wait_for_bridge(process, ui_port)
        preflight = _preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        evidence["checks"]["bridge_api_preflight"] = {"status": "PASS"}
        evidence["preflight"] = {
            "bridge_pid": preflight["bridge_pid"],
            "appserver_pid": preflight["appserver_pid"],
            "ports_owned_by_bridge": preflight["ports_owned_by_bridge"],
            "capabilities_authenticated": preflight["capabilities_authenticated"],
            "missing_token_rejected": preflight["missing_token_rejected"],
            "incorrect_token_rejected": preflight["incorrect_token_rejected"],
            "github_mount_enabled": False,
            "codex_home_mcp_config_present": False,
        }
        _save_evidence(evidence_path, evidence)
        if args.preflight_only:
            evidence["checks"]["single_live_post"] = {
                "status": "NOT_RUN",
                "detail": "preflight_only",
            }
            process_results.append(_stop_bridge(process, ui_port=ui_port, token=token))
            process = None
            if not process_results[-1]["reaped"] or process_results[-1]["forced_termination"]:
                raise SmokeAbort("BLOCKED", "preflight_bridge_not_cleanly_reaped")
            _assert_ports_free(ports)
            evidence["process_cleanup"] = {
                "reaped": True,
                "forced_termination": False,
                "bridge_processes": len(process_results),
                "descendant_processes_observed": sum(
                    result["descendant_count"] for result in process_results
                ),
            }
            evidence["status"] = "PREFLIGHT_ONLY"
            return 0, evidence
        _post_fixed_workflow(
            ui_port=ui_port,
            token=token,
            state_root=state_root,
            workspace_root=workspace_root,
            evidence=evidence,
            evidence_path=evidence_path,
        )
        process_results.append(_stop_bridge(process, ui_port=ui_port, token=token))
        process = None
        if not process_results[-1]["reaped"] or process_results[-1]["forced_termination"]:
            evidence["restart"] = {"status": "BLOCKED", "detail": "normal_shutdown_not_confirmed"}
            raise SmokeAbort("BLOCKED", "first_bridge_process_not_cleanly_reaped")
        _assert_ports_free(ports)

        process = _start_bridge(child_env)
        _wait_for_bridge(process, ui_port)
        restarted_preflight = _preflight_bridge(
            process,
            mcp_port=mcp_port,
            ui_port=ui_port,
            token=token,
            expected_codex=codex_executable,
        )
        worker_count = _worker_process_count(process.pid)
        if worker_count != 0:
            raise SmokeAbort("BLOCKED", "restart_started_a_workflow_worker")
        workflow = evidence.get("workflow", {})
        workflow_id = workflow.get("workflow_id") if isinstance(workflow, dict) else None
        run_id = workflow.get("run_id") if isinstance(workflow, dict) else None
        client_request_id = (
            workflow.get("client_request_id") if isinstance(workflow, dict) else None
        )
        job_id = workflow.get("job_id") if isinstance(workflow, dict) else None
        thread_id = workflow.get("thread_id") if isinstance(workflow, dict) else None
        if not all(
            isinstance(value, str)
            for value in (workflow_id, run_id, client_request_id, job_id, thread_id)
        ):
            raise SmokeAbort("BLOCKED", "restart_readback_identity_unavailable")
        listing, detail, result = _read_workflow_views(ui_port, token, workflow_id)
        if (
            listing[0] != 200
            or detail[0] != 200
            or result[0] != 200
            or not isinstance(listing[1], dict)
            or not isinstance(detail[1], dict)
            or not isinstance(result[1], dict)
        ):
            raise SmokeAbort("BLOCKED", "restart_read_only_api_readback_failed")
        workflows = listing[1].get("workflows")
        if not isinstance(workflows, list):
            raise SmokeAbort("BLOCKED", "restart_workflow_list_shape_invalid")
        item = next(
            (
                candidate
                for candidate in workflows
                if isinstance(candidate, dict) and candidate.get("workflow_id") == workflow_id
            ),
            {},
        )
        _assert_success_identity(
            workflow_id=workflow_id,
            run_id=run_id,
            list_item=item,
            detail=detail[1],
            result=result[1],
        )
        restarted_state = _read_run_state(
            state_root,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
        )
        _assert_direct_success(
            restarted_state,
            client_request_id=client_request_id,
            workflow_id=workflow_id,
            run_id=run_id,
            job_id=job_id,
            thread_id=thread_id,
        )
        restarted_threads = _thread_ids(ui_port, token)
        if thread_id not in restarted_threads:
            raise SmokeAbort("BLOCKED", "restart_codex_thread_not_visible")
        evidence["restart"] = {
            "status": "PASS",
            "bridge_pid": restarted_preflight["bridge_pid"],
            "appserver_pid": restarted_preflight["appserver_pid"],
            "workflow_workers_started": worker_count,
            "read_only_list_detail_result": "PASS",
            "job_rows_after_restart": restarted_state["job_count"],
            "dbos_status_after_restart": restarted_state["dbos_status"],
        }
        evidence["checks"]["restart_readback"] = {"status": "PASS"}
        evidence["checks"]["console_rendering"] = {
            "status": "UNKNOWN",
            "detail": "real_API_GETs_verified; offscreen_render_not_exercised",
        }
        _save_evidence(evidence_path, evidence)
        process_results.append(_stop_bridge(process, ui_port=ui_port, token=token))
        process = None
        if not process_results[-1]["reaped"] or process_results[-1]["forced_termination"]:
            raise SmokeAbort("BLOCKED", "restarted_bridge_process_not_cleanly_reaped")
        evidence["process_cleanup"] = {
            "reaped": all(result["reaped"] for result in process_results),
            "forced_termination": any(result["forced_termination"] for result in process_results),
            "bridge_processes": len(process_results),
            "descendant_processes_observed": sum(
                result["descendant_count"] for result in process_results
            ),
        }
        evidence["status"] = "PASS"
        return 0, evidence
    finally:
        if process is not None:
            process_results.append(_stop_bridge(process, ui_port=ui_port, token=token))
            evidence["process_cleanup"] = {
                "reaped": all(result["reaped"] for result in process_results),
                "forced_termination": any(
                    result["forced_termination"] for result in process_results
                ),
                "bridge_processes": len(process_results),
                "descendant_processes_observed": sum(
                    result["descendant_count"] for result in process_results
                ),
            }
        for reservation in reservations:
            reservation.close()
        try:
            copied_auth_path.unlink(missing_ok=True)
        except OSError:
            auth_copy_removed = False
        else:
            auth_copy_removed = not copied_auth_path.exists()
        evidence["isolated_auth_copy_removed"] = auth_copy_removed
        try:
            copied_auth_path.parent.rmdir()
        except OSError:
            evidence["isolated_codex_home_directory_removed"] = False
        else:
            evidence["isolated_codex_home_directory_removed"] = True
        evidence["ended_at_utc"] = _utc_now()
        _save_evidence(evidence_path, evidence)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the fixed Phase 5E-0 isolated dogfood once.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="authorize the single fixed live job")
    mode.add_argument(
        "--preflight-only",
        action="store_true",
        help="start the isolated Bridge and verify read-only readiness without workflow POST",
    )
    mode.add_argument(
        "--resume-posted",
        action="store_true",
        help=(
            "restart an isolated run from blocked evidence; GET first and only replay its "
            "same request ID after success is verified"
        ),
    )
    parser.add_argument(
        "--evidence",
        default=str(EVIDENCE_PATH),
        help="sanitized evidence JSON path (default: docs/phase5e-0-evidence.json)",
    )
    args = parser.parse_args(argv)

    if args.resume_posted:
        evidence_path = Path(args.evidence).expanduser().resolve(strict=False)
        try:
            exit_code, evidence = _resume_posted_run(args)
        except SmokeAbort as error:
            if evidence_path.is_file():
                try:
                    existing = json.loads(evidence_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = None
            else:
                existing = None
            if isinstance(existing, dict) and existing.get("phase") == "5E-0":
                evidence = existing
                readback = evidence.setdefault("readback_restart", {})
                if isinstance(readback, dict):
                    readback.update({"status": error.status, "block_reason": error.code})
                evidence["status"] = error.status
                evidence["block_reason"] = error.code
                evidence["ended_at_utc"] = _utc_now()
                _save_evidence(evidence_path, evidence)
            else:
                evidence = {
                    "phase": "5E-0",
                    "status": error.status,
                    "block_reason": error.code,
                    "live_post_attempted": False,
                    "evidence_preserved": False,
                }
            exit_code = 2 if error.status == "BLOCKED" else 1
        except KeyboardInterrupt:
            try:
                existing = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and existing.get("phase") == "5E-0":
                evidence = existing
                evidence["status"] = "BLOCKED"
                evidence["block_reason"] = "readback_interrupted_no_retry"
                evidence["ended_at_utc"] = _utc_now()
                replay = evidence.get("replay")
                if isinstance(replay, dict) and evidence.get("replay_attempted") is True:
                    replay["status"] = "BLOCKED"
                    replay["detail"] = "interrupted_no_retry"
                _save_evidence(evidence_path, evidence)
            else:
                evidence = {
                    "phase": "5E-0",
                    "status": "BLOCKED",
                    "block_reason": "readback_interrupted_no_retry",
                    "live_post_attempted": False,
                    "evidence_preserved": False,
                }
            exit_code = 2
        except Exception as error:
            try:
                existing = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and existing.get("phase") == "5E-0":
                evidence = existing
                evidence["status"] = "BLOCKED"
                evidence["block_reason"] = f"unexpected_{type(error).__name__}"
                evidence["ended_at_utc"] = _utc_now()
                _save_evidence(evidence_path, evidence)
            else:
                evidence = {
                    "phase": "5E-0",
                    "status": "BLOCKED",
                    "block_reason": f"unexpected_{type(error).__name__}",
                    "live_post_attempted": False,
                    "evidence_preserved": False,
                }
            exit_code = 2
        print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
        return exit_code

    exit_code = 2
    evidence: dict[str, Any] = {
        "phase": "5E-0",
        "status": "BLOCKED",
        "started_at_utc": _utc_now(),
        "ended_at_utc": None,
        "checks": {},
        "live_post_attempted": False,
        "workflow": {},
        "diagnostics": {"raw_logs_saved": False, "diagnostic_bytes_saved": 0},
        "production_side_effects": "no_existing_checkout_service_port_or_state_used",
    }
    evidence_path = Path(args.evidence).expanduser().resolve(strict=False)
    existed_before = evidence_path.exists()

    def save_failure(status: str, code: str) -> None:
        nonlocal evidence
        if existed_before:
            evidence = {
                "phase": "5E-0",
                "status": status,
                "block_reason": code,
                "live_post_attempted": False,
                "evidence_preserved": True,
            }
            return
        if evidence_path.is_file():
            try:
                current = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = None
            if isinstance(current, dict) and current.get("phase") == "5E-0":
                evidence = current
        evidence["status"] = status
        evidence["block_reason"] = code
        evidence["ended_at_utc"] = _utc_now()
        workflow = evidence.get("workflow")
        if (
            status == "BLOCKED"
            and evidence.get("live_post_attempted") is True
            and isinstance(workflow, dict)
            and workflow.get("status") != "succeeded"
        ):
            workflow["status"] = "needs_reconciliation"
        try:
            _save_evidence(evidence_path, evidence)
        except OSError:
            pass

    try:
        exit_code, evidence = _run(args)
    except SmokeAbort as error:
        save_failure(error.status, error.code)
        exit_code = 2 if error.status == "BLOCKED" else 1
    except KeyboardInterrupt:
        save_failure("BLOCKED", "interrupted_no_retry")
        exit_code = 2
    except Exception as error:
        save_failure("BLOCKED", f"unexpected_{type(error).__name__}")
        exit_code = 2
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
