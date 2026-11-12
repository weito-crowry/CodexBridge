from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from scripts.phase5f1d_restart_support import (
    AclSnapshotError,
    replay_history_is_unknown,
    restart_eligible,
    shutdown_once,
    snapshot_acl,
    write_evidence_once,
)
from scripts.phase5f1d_restart_support import (
    acl_scope_isolated as _acl_scope_isolated,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ROOT_LABEL = "codexbridge-phase5f1b-_xwln6v5"
WORKFLOW_ID = "bca46292-4ea0-4ab1-b80b-e45b924f5ecf"
RUN_ID = "run-14e085cffd6443a484533a985dcbe135"
CLIENT_REQUEST_ID = "phase5f1b-cfa1c2cd73334624bbf14f03fce1dece"
REQUEST_DIGEST = "6f7f53b2267012da9d3c0b2d20f22cc35d82386969b36437bd88f2b3b0e12c61"
BRIDGE_HOST = "127.0.0.1"
MCP_PORT = 62806
UI_PORT = 62807
OLD_BRIDGE_PID = 14208
OLD_BRIDGE_CREATED = 1794500465.809399
OLD_LISTENER_PID = 10308
OLD_LISTENER_CREATED = 1794500465.841188
OLD_LISTENER_EXE = Path(
    r"C:\Users\weito\AppData\Roaming\uv\python\cpython-3.14.10-windows-x86_64-none\python.exe"
)
CODEX_EXE = Path(r"C:\Users\weito\AppData\Local\OpenAI\Codex\bin\9e1da79431da526b\codex.exe")
R1C_EVIDENCE_SHA256 = "0ea040a25c4a1a87159b713a97bace050d1cdcdb672eaee9441ed32e64b5233a"
EVIDENCE_PATH = REPOSITORY_ROOT / "docs" / "phase5f1b-r1d-restart-readback-evidence.json"
PROTECTED_EVIDENCE_SHA256 = {
    "docs/phase5f1b-postmortem-addendum.json": (
        "8298326e05c6779bd41de01c74c93b1eafe06adb7e295510ab3f01f458f81c3d"
    ),
    "docs/phase5f1b-r1-postmortem-addendum.json": (
        "8ac77f67e8723c6c548f0509b2bbfaf7825fabb6372081e57dc9bc10d95aefd8"
    ),
    "docs/phase5f1b-three-job-e2e-evidence.json": (
        "1af75fcb89ad314be79f35f5e06b2eca777a1c877e150d6e683c0f88a435bdc5"
    ),
    "docs/phase5f1b-three-job-e2e-r1-evidence.json": (
        "a9572b879933808c813b1f96eaad0852ca55722dfd3f01c899944e74b4b17176"
    ),
    "docs/phase5e-2c2-diagnostic-retest-evidence.json": (
        "ede76c2334f362765ff136619f9a393fd592bb514ab9b707374598a5bd4152b5"
    ),
    "docs/phase5e-2c2-r4-evidence.json": (
        "9fc47e74ea21854af1a8e1157da72496f5a5cf30480880a713ca4497fd311742"
    ),
    "docs/phase5f1b-r1c-readback-replay-evidence.json": R1C_EVIDENCE_SHA256,
}
EXPECTED_STATIC_SHA256 = {
    "workflow.yaml": "e34b3bd2c2e1cf573cf751c73ccb5b107ab3e5579275905268d0be857eee5cb1",
    "input.json": "49c56c5cd2dab0ab4b3685befb2e8486b84b1c55136f7be49dc6cea565b961c3",
    "manifest.json": "2659023533936831c8eb363d3b78af45aae609be81609970220a62ea5d4590c2",
    "result.json": "250478c20131a857d671dab93408b757a919eb7c23b7dd7ac45de8df93d1011d",
    "requests.sqlite": "40c0f4c0973655ac459ca91655fb102194c1d8dc18a4f8c7f548feee2cb61788",
    "codex_jobs.sqlite": "b28dbafb55108646c8b20883175a3b6417c6805ccc0d107659df640667446741",
    "dbos.sqlite": "db7624ef2c86991f563f0edc0267419f942301c15f6f48a5d8c129380b86afc3",
}
EXPECTED_PRE_RESTART_API_SHA256 = {
    "list": "e6c1e3895bbd7f11420684a0fa9c60fac27236727341f44cd69307e01f9eef08",
    "detail": "c026fdd43c98ebcf0fa532a92b9a2dd4a2680ae114fa010244021f69d2c3fef3",
    "result": "172420d039ac3f48dc1d25d031be9c9084991a536e75e48b44f00b86ff1ea9d3",
}
_ENVIRONMENT_KEYS = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "CODEX_BRIDGE_CONFIG",
    "CODEX_BRIDGE_CONTROL_TOKEN",
    "LOCALAPPDATA",
    "APPDATA",
    "TEMP",
    "TMP",
    "CODEX_HOME",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
}
_TERMINAL_TURN_STATES = {"completed", "interrupted", "failed"}


class RestartBlocked(RuntimeError):
    """A fixed, user-safe reason that a restart precondition was not met."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded.encode())


def _file_hash(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    return _sha256(path.read_bytes())


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RestartBlocked("required_json_artifact_unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RestartBlocked("required_json_artifact_invalid") from None
    if not isinstance(value, dict):
        raise RestartBlocked("required_json_artifact_invalid")
    return value


def _request_json(
    method: str,
    route: str,
    *,
    port: int = UI_PORT,
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json",
        "Origin": f"http://{BRIDGE_HOST}:{port}",
        "Host": f"{BRIDGE_HOST}:{port}",
    }
    body = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(BRIDGE_HOST, port, timeout=timeout)
    try:
        connection.request(method, route, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise RestartBlocked("ui_api_response_exceeded_limit")
        try:
            result = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = None
        return response.status, result
    except RestartBlocked:
        raise
    except (OSError, http.client.HTTPException, TimeoutError):
        raise RestartBlocked("ui_api_transport_outcome_unknown") from None
    finally:
        connection.close()


def _read_only_rows(
    path: Path,
    query: str,
    parameters: tuple[object, ...] = (),
) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise RestartBlocked("sqlite_artifact_unavailable")
    try:
        connection = sqlite3.connect(
            path.resolve(strict=True).as_uri() + "?mode=ro",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        raise RestartBlocked("sqlite_read_only_query_failed") from None


def _sqlite_snapshot(state_root: Path, run_dir: Path) -> dict[str, Any]:
    requests = _read_only_rows(
        state_root / "requests.sqlite",
        "SELECT client_request_id,workflow_id,run_id,request_digest,state "
        "FROM workflow_requests ORDER BY client_request_id",
    )
    jobs_path = run_dir / "codex_jobs.sqlite"
    jobs = _read_only_rows(
        jobs_path,
        "SELECT job_id,workflow_id,task_identity,request_digest,state,client_request_id,"
        "thread_id,turn_id,remote_state FROM jobs ORDER BY rowid",
    )
    events = _read_only_rows(
        jobs_path,
        "SELECT state,COUNT(*) AS count FROM job_events GROUP BY state ORDER BY state",
    )
    event_total = _read_only_rows(jobs_path, "SELECT COUNT(*) AS count FROM job_events")[0]["count"]
    reconciliation_count = _read_only_rows(
        jobs_path, "SELECT COUNT(*) AS count FROM job_reconciliation"
    )[0]["count"]
    dbos = _read_only_rows(
        run_dir / "dbos.sqlite",
        "SELECT workflow_uuid,status,name FROM workflow_status ORDER BY workflow_uuid",
    )
    worker = _read_json(run_dir / "worker-entry-status.json")
    result = _read_json(run_dir / "result.json")
    result_rows = result.get("result")
    result_projection = (
        [
            {
                "status": row.get("status"),
                "number": row.get("number"),
                "final_text": row.get("final_text"),
            }
            for row in result_rows
            if isinstance(row, dict)
        ]
        if isinstance(result_rows, list)
        else []
    )
    final_texts = (
        [
            row.get("final_text")
            for row in result_rows
            if isinstance(row, dict) and row.get("status") == "SUCCEEDED"
        ]
        if isinstance(result_rows, list)
        else []
    )
    return {
        "requests": requests,
        "jobs": jobs,
        "events": events,
        "event_count": event_total,
        "reconciliation_count": reconciliation_count,
        "dbos": dbos,
        "worker_status": {
            "state": worker.get("state"),
            "exit_code": worker.get("exit_code"),
        },
        "result_rows": result_projection,
        "result_final_texts": final_texts,
    }


def _expected_sqlite_state(state: Mapping[str, Any]) -> bool:
    expected_event_counts = [
        {"state": name, "count": 3}
        for name in ("ACCEPTED", "MAYBE_SENT", "PREPARED", "RUNNING", "SUCCEEDED")
    ]
    return (
        len(state["requests"]) == 1
        and state["requests"][0]
        == {
            "client_request_id": CLIENT_REQUEST_ID,
            "workflow_id": WORKFLOW_ID,
            "run_id": RUN_ID,
            "request_digest": REQUEST_DIGEST,
            "state": "running",
        }
        and len(state["jobs"]) == 3
        and all(
            row["workflow_id"] == WORKFLOW_ID
            and row["client_request_id"] == f"ows4d-{row['job_id'].replace('-', '')}"
            and re.fullmatch(r"[0-9a-f]{64}", row["request_digest"]) is not None
            and row["state"] == "SUCCEEDED"
            and row["remote_state"] == "completed"
            for row in state["jobs"]
        )
        and len({row["job_id"] for row in state["jobs"]}) == 3
        and len({row["request_digest"] for row in state["jobs"]}) == 3
        and len({row["thread_id"] for row in state["jobs"]}) == 3
        and state["event_count"] == 15
        and state["events"] == expected_event_counts
        and state["reconciliation_count"] == 0
        and len(state["dbos"]) == 1
        and state["dbos"][0]["workflow_uuid"] == WORKFLOW_ID
        and state["dbos"][0]["status"] == "SUCCESS"
        and state["worker_status"] == {"state": "completed", "exit_code": 0}
        and state["result_rows"]
        == [
            {"status": "skipped", "number": 5, "final_text": None},
            {"status": "SUCCEEDED", "number": 9, "final_text": "9"},
            {"status": "SUCCEEDED", "number": 17, "final_text": "17"},
            {"status": "SUCCEEDED", "number": 21, "final_text": "21"},
        ]
        and state["result_final_texts"] == ["9", "17", "21"]
    )


def _artifact_snapshot(root: Path, state_root: Path, run_dir: Path) -> dict[str, Any]:
    artifact_paths = {
        "bridge.toml": root / "bridge.toml",
        "workflow.yaml": run_dir / "workflow.yaml",
        "input.json": run_dir / "input.json",
        "manifest.json": run_dir / "manifest.json",
        "result.json": run_dir / "result.json",
        "task-events.jsonl": run_dir / "task-events.jsonl",
        "initial-worker-heartbeat.jsonl": run_dir / "initial-worker-heartbeat.jsonl",
        "start-worker.json": run_dir / "start-worker.json",
        "start-worker-identity.json": run_dir / "start-worker-identity.json",
        "worker-entry-status.json": run_dir / "worker-entry-status.json",
        "requests.sqlite": state_root / "requests.sqlite",
        "codex_jobs.sqlite": run_dir / "codex_jobs.sqlite",
        "dbos.sqlite": run_dir / "dbos.sqlite",
    }
    result: dict[str, Any] = {}
    for name, path in artifact_paths.items():
        if path.is_symlink() or not path.is_file():
            raise RestartBlocked("protected_run_artifact_unavailable")
        result[name] = {"sha256": _file_hash(path), "size": path.stat().st_size}
    for database_name, path in (
        ("requests.sqlite", state_root / "requests.sqlite"),
        ("codex_jobs.sqlite", run_dir / "codex_jobs.sqlite"),
        ("dbos.sqlite", run_dir / "dbos.sqlite"),
    ):
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            result[database_name + suffix] = {
                "exists": sidecar.is_file(),
                "sha256": _file_hash(sidecar),
                "size": sidecar.stat().st_size if sidecar.is_file() else None,
            }
    auth_file = root / "codex_home" / "auth.json"
    result["isolated_auth_file"] = {
        "exists": auth_file.is_file() and not auth_file.is_symlink(),
        "size": auth_file.stat().st_size if auth_file.is_file() else None,
    }
    return result


def _protected_evidence_hashes() -> dict[str, str | None]:
    return {
        relative: _file_hash(REPOSITORY_ROOT / relative) for relative in PROTECTED_EVIDENCE_SHA256
    }


def _acl_snapshot(
    root: Path,
    workspace: Path,
    state_root: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    try:
        return snapshot_acl(root, workspace, state_root, run_dir)
    except AclSnapshotError as error:
        raise RestartBlocked(error.code) from None


def _owned_listener_rows(
    parent_pid: int,
) -> tuple[bool, dict[int, list[dict[str, Any]]]]:
    try:
        parent = psutil.Process(parent_pid)
        children = parent.children(recursive=True)
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return False, {}
    child_ids = {parent_pid, *(child.pid for child in children)}
    listener_rows: dict[int, list[dict[str, Any]]] = {MCP_PORT: [], UI_PORT: []}
    for connection in connections:
        address = connection.laddr
        if connection.status == psutil.CONN_LISTEN and address and address.port in listener_rows:
            listener_rows[address.port].append({"ip": address.ip, "pid": connection.pid})
    ownership_ok = all(
        len(listener_rows[port]) == 1
        and all(row["ip"] == BRIDGE_HOST and row["pid"] in child_ids for row in listener_rows[port])
        for port in (MCP_PORT, UI_PORT)
    )
    return ownership_ok, listener_rows


def _worker_process_count(parent_pid: int) -> tuple[int, bool]:
    try:
        children = psutil.Process(parent_pid).children(recursive=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return 0, False
    count = 0
    complete = True
    for child in children:
        try:
            if any("codex_bridge.workflows.worker_entry" in part for part in child.cmdline()):
                count += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            complete = False
    return count, complete


def _thread_snapshot() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    complete = False
    for _ in range(20):
        route = "/ui-api/threads?limit=100"
        if cursor:
            route += "&cursor=" + urllib.parse.quote(cursor, safe="")
        status, payload = _request_json("GET", route)
        if (
            status != 200
            or not isinstance(payload, dict)
            or not isinstance(payload.get("threads"), list)
        ):
            break
        rows.extend(row for row in payload["threads"] if isinstance(row, dict))
        external = payload.get("external_threads", [])
        if isinstance(external, list):
            rows.extend(row for row in external if isinstance(row, dict))
        cursor = payload.get("next_cursor")
        if cursor is None:
            complete = True
            break
    threads: dict[str, dict[str, Any]] = {}
    for row in rows:
        thread_id = row.get("id") or row.get("thread_id")
        if isinstance(thread_id, str) and thread_id:
            threads[thread_id] = row
    statuses: list[dict[str, Any]] = []
    all_terminal = complete
    for thread_id in sorted(threads):
        route = "/ui-api/threads/" + urllib.parse.quote(thread_id, safe="") + "/status"
        code, payload = _request_json("GET", route)
        if code != 200 or not isinstance(payload, dict):
            statuses.append({"thread_id": thread_id, "http_status": code, "terminal": False})
            all_terminal = False
            continue
        state = payload.get("state")
        pending = payload.get("pending_request")
        terminal = state in _TERMINAL_TURN_STATES
        if not terminal and state in {None, "unknown", "not_loaded"}:
            turns_route = route.rsplit("/status", 1)[0] + "/turns?limit=100&sort_direction=desc"
            turns_code, turns_payload = _request_json("GET", turns_route)
            turns = turns_payload.get("turns") if isinstance(turns_payload, dict) else None
            if (
                turns_code == 200
                and isinstance(turns, list)
                and isinstance(turns_payload, dict)
                and turns_payload.get("next_cursor") is None
            ):
                terminal = all(
                    isinstance(turn, dict) and turn.get("status") in _TERMINAL_TURN_STATES
                    for turn in turns
                )
                if not turns:
                    terminal = state in {None, "not_loaded"}
        statuses.append(
            {
                "thread_id": thread_id,
                "state": state,
                "pending": pending is not None,
                "terminal": terminal,
            }
        )
        all_terminal = all_terminal and terminal and pending is None
    return {
        "complete": complete,
        "count": len(threads),
        "thread_ids": sorted(threads),
        "external_count": sum(1 for row in rows if row.get("external") is True),
        "statuses": statuses,
        "all_terminal_no_pending": all_terminal,
    }


def _api_readback(token: str, state: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    health_code, health = _request_json("GET", "/healthz")
    status_code, bridge_status = _request_json("GET", "/ui-api/status")
    capabilities_code, _ = _request_json("GET", "/ui-api/control/capabilities", token=token)
    missing_token_code, _ = _request_json("GET", "/ui-api/control/workflows")
    malformed_token_code, _ = _request_json(
        "GET", "/ui-api/control/workflows", token="malformed-token-for-negative-check"
    )
    list_code, listing = _request_json("GET", "/ui-api/control/workflows?limit=100", token=token)
    detail_code, detail = _request_json(
        "GET", f"/ui-api/control/workflows/{WORKFLOW_ID}", token=token
    )
    result_code, result = _request_json(
        "GET", f"/ui-api/control/workflows/{WORKFLOW_ID}/result", token=token
    )
    saved_result = _read_json(run_dir / "result.json")
    thread_status = _thread_snapshot()
    workflows = listing.get("workflows") if isinstance(listing, dict) else None
    workflow_projection = (
        [
            {"workflow_id": row.get("workflow_id"), "status": row.get("status")}
            for row in workflows
            if isinstance(row, dict)
        ]
        if isinstance(workflows, list)
        else []
    )
    detail_jobs = detail.get("jobs") if isinstance(detail, dict) else None
    job_projection = (
        [
            {
                key: row.get(key)
                for key in ("job_id", "task_identity", "state", "thread_id", "turn_id")
            }
            for row in detail_jobs
            if isinstance(row, dict)
        ]
        if isinstance(detail_jobs, list)
        else []
    )
    api_ok = (
        health_code == 200
        and isinstance(health, dict)
        and health.get("status") == "ok"
        and status_code == 200
        and isinstance(bridge_status, dict)
        and bridge_status.get("bridge") == "ready"
        and bridge_status.get("app_server") == "ready"
        and bridge_status.get("ui_host") == BRIDGE_HOST
        and bridge_status.get("ui_port") == UI_PORT
        and capabilities_code == 200
        and missing_token_code == 403
        and malformed_token_code == 403
        and list_code == 200
        and workflow_projection == [{"workflow_id": WORKFLOW_ID, "status": "succeeded"}]
        and detail_code == 200
        and isinstance(detail, dict)
        and detail.get("workflow_id") == WORKFLOW_ID
        and detail.get("run_id") == RUN_ID
        and detail.get("status") == "succeeded"
        and detail.get("worker_state") == "completed"
        and len(job_projection) == 3
        and all(row["state"] == "SUCCEEDED" for row in job_projection)
        and result_code == 200
        and result == saved_result
        and thread_status["complete"]
        and thread_status["all_terminal_no_pending"]
        and set(thread_status["thread_ids"]) == {row["thread_id"] for row in state["jobs"]}
    )
    return {
        "status": "PASS" if api_ok else "BLOCKED",
        "http": {
            "health": health_code,
            "status": status_code,
            "capabilities": capabilities_code,
            "missing_token": missing_token_code,
            "malformed_token": malformed_token_code,
            "list": list_code,
            "detail": detail_code,
            "result": result_code,
        },
        "hashes": {
            "list": _canonical_sha256(listing),
            "detail": _canonical_sha256(detail),
            "result": _canonical_sha256(result),
        },
        "workflow_count": len(workflow_projection),
        "workflow_id": workflow_projection[0]["workflow_id"] if workflow_projection else None,
        "workflow_status": workflow_projection[0]["status"] if workflow_projection else None,
        "detail_jobs": job_projection,
        "result_matches_saved": result == saved_result,
        "result_final_texts": state["result_final_texts"],
        "threads": thread_status,
        "_payloads": (detail, result),
    }


def _qt_readback(token: str, api_detail: Any, api_result: Any) -> dict[str, Any]:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    from codex_bridge.console.api_client import ApiClient  # type: ignore[import-untyped]

    _application = QCoreApplication.instance() or QCoreApplication([])
    client = ApiClient(f"http://{BRIDGE_HOST}:{UI_PORT}")
    replies: dict[str, dict[str, Any]] = {}

    def succeeded(key: str, status: int, payload: Any) -> None:
        replies[key] = {"status": status, "payload": payload}

    def failed(key: str, status: int, _code: str) -> None:
        replies[key] = {"status": status, "payload": None}

    client.workflow_succeeded.connect(succeeded)
    client.workflow_failed.connect(failed)
    requests: tuple[tuple[str, Callable[[], bool]], ...] = (
        (
            "detail",
            lambda: client.get_control_workflow_detail(WORKFLOW_ID, token, key="phase5f1d-detail"),
        ),
        (
            "result",
            lambda: client.get_control_workflow_result(WORKFLOW_ID, token, key="phase5f1d-result"),
        ),
    )
    for name, start in requests:
        key = f"phase5f1d-{name}"
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        started = start()
        if started and key not in replies:
            timer.start(12_000)
            loop.exec()
            timer.stop()
        if key not in replies:
            if started:
                client.abort_workflow_request(key)
            replies[key] = {"status": 0, "payload": None}
    detail = replies["phase5f1d-detail"]
    result = replies["phase5f1d-result"]
    detail_payload = detail["payload"]
    result_payload = result["payload"]
    passed = (
        detail["status"] == 200
        and result["status"] == 200
        and detail_payload == api_detail
        and result_payload == api_result
    )
    return {
        "status": "PASS" if passed else "BLOCKED",
        "detail_http": detail["status"],
        "result_http": result["status"],
        "detail_matches_canonical": detail_payload == api_detail,
        "result_matches_canonical": result_payload == api_result,
        "detail_sha256": _canonical_sha256(detail_payload),
        "result_sha256": _canonical_sha256(result_payload),
    }


def _process_matches(pid: int, created_at: float) -> bool | None:
    try:
        process = psutil.Process(pid)
        return bool(process.is_running() and abs(process.create_time() - created_at) < 0.01)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return None


def _ports_are_free() -> tuple[bool, list[dict[str, Any]]]:
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return False, []
    listeners = [
        {"port": connection.laddr.port, "ip": connection.laddr.ip, "pid": connection.pid}
        for connection in connections
        if connection.status == psutil.CONN_LISTEN
        and connection.laddr
        and connection.laddr.port in {MCP_PORT, UI_PORT}
    ]
    return not listeners, listeners


def _root_context() -> tuple[Path, Path, Path, Path]:
    temp = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "")
    if not temp.is_absolute() or not temp.is_dir():
        raise RestartBlocked("windows_temp_root_unavailable")
    root = (temp / ROOT_LABEL).resolve(strict=True)
    state_root = root / "localappdata" / "CodexBridge" / "workflows"
    run_dir = state_root / RUN_ID
    workspace = root / "workspace"
    if root.is_symlink() or not root.is_dir() or not state_root.is_dir() or not run_dir.is_dir():
        raise RestartBlocked("r1_isolated_root_unavailable")
    if workspace.is_symlink() or not workspace.is_dir():
        raise RestartBlocked("r1_workspace_unavailable")
    if not state_root.resolve(strict=True).is_relative_to(root):
        raise RestartBlocked("r1_state_scope_invalid")
    return root, state_root, run_dir, workspace


def _validate_environment(
    root: Path,
    workspace: Path,
    environment: Mapping[str, str],
    current_user: str,
) -> tuple[str, Path, Path, bool]:
    token = environment.get("CODEX_BRIDGE_CONTROL_TOKEN", "")
    python_exe = (REPOSITORY_ROOT / ".venv" / "Scripts" / "python.exe").resolve(strict=True)
    codex_home = root / "codex_home"
    codex_exe = CODEX_EXE.resolve(strict=True)
    expected_keys = set(_ENVIRONMENT_KEYS)
    if set(environment) - {"__PYVENV_LAUNCHER__"} != expected_keys:
        raise RestartBlocked("r1_environment_allowlist_mismatch")
    if os.path.normcase(environment.get("__PYVENV_LAUNCHER__", "")) != os.path.normcase(
        str(python_exe)
    ):
        raise RestartBlocked("r1_python_launcher_marker_mismatch")
    expected_path = {
        Path(environment["SYSTEMROOT"], "System32").resolve(strict=True),
        python_exe.parent,
        codex_exe.parent,
        Path(r"D:\Program Files\nodejs").resolve(strict=True),
    }
    path_entries = {
        Path(entry).resolve(strict=True)
        for entry in environment.get("PATH", "").split(os.pathsep)
        if entry
    }
    if path_entries != expected_path:
        raise RestartBlocked("r1_executable_path_allowlist_mismatch")
    expected_values = {
        "CODEX_BRIDGE_CONFIG": str(root / "bridge.toml"),
        "LOCALAPPDATA": str(root / "localappdata"),
        "APPDATA": str(root / "appdata"),
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "CODEX_HOME": str(codex_home),
        "USERPROFILE": str(root),
        "HOME": str(root),
        "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
    }
    if not token or any(environment.get(key) != value for key, value in expected_values.items()):
        raise RestartBlocked("r1_isolated_environment_path_mismatch")
    if not codex_exe.is_file() or Path(sys.executable).resolve(strict=True) != python_exe:
        raise RestartBlocked("r1_python_or_codex_executable_mismatch")
    if not (root / "bridge.toml").is_file() or not (codex_home / "auth.json").is_file():
        raise RestartBlocked("r1_config_or_authentication_unavailable")
    if (codex_home / "config.toml").exists():
        raise RestartBlocked("r1_codex_home_config_present")
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    from codex_bridge.config import BridgeConfig  # type: ignore[import-untyped]
    from codex_bridge.server import prepare_config  # type: ignore[import-untyped]

    effective = prepare_config(BridgeConfig.from_sources(environ=environment))
    config_ok = (
        effective.host == BRIDGE_HOST
        and effective.port == MCP_PORT
        and effective.ui_port == UI_PORT
        and tuple(os.path.normcase(path) for path in effective.allowed_roots)
        == (os.path.normcase(str(workspace.resolve(strict=True))),)
        and not effective.lan_enabled
        and not effective.github_mcp.enabled
        and len(effective.targets) == 1
        and effective.targets[0].kind == "local"
        and os.path.normcase(effective.codex_executable) == os.path.normcase(str(codex_exe))
        and effective.control_token == token
    )
    if not config_ok:
        raise RestartBlocked("r1_config_scope_mismatch")
    return token, python_exe, codex_exe, config_ok


def _preflight() -> dict[str, Any]:
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise RestartBlocked("windows_native_runtime_required")
    if EVIDENCE_PATH.exists() or EVIDENCE_PATH.is_symlink():
        raise RestartBlocked("new_evidence_path_already_exists")
    root, state_root, run_dir, workspace = _root_context()
    current_user = psutil.Process().username()
    try:
        parent = psutil.Process(OLD_BRIDGE_PID)
        environment = parent.environ()
        old_cwd = Path(parent.cwd()).resolve(strict=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        raise RestartBlocked("r1_bridge_process_unavailable") from None
    expected_parent_exe = (REPOSITORY_ROOT / ".venv" / "Scripts" / "python.exe").resolve(
        strict=True
    )
    if (
        not parent.is_running()
        or abs(parent.create_time() - OLD_BRIDGE_CREATED) >= 0.01
        or parent.username() != current_user
        or os.path.normcase(parent.exe()) != os.path.normcase(str(expected_parent_exe))
        or os.path.normcase(str(old_cwd))
        != os.path.normcase(str((root / "tmp").resolve(strict=True)))
        or not old_cwd.is_relative_to(root)
    ):
        raise RestartBlocked("r1_bridge_process_identity_mismatch")
    try:
        listener = psutil.Process(OLD_LISTENER_PID)
        listener_exe = Path(listener.exe()).resolve(strict=True)
        descendants = parent.children(recursive=True)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        raise RestartBlocked("r1_listener_process_unavailable") from None
    if (
        not listener.is_running()
        or abs(listener.create_time() - OLD_LISTENER_CREATED) >= 0.01
        or listener.ppid() != OLD_BRIDGE_PID
        or listener.username() != current_user
        or listener.pid not in {process.pid for process in descendants}
        or os.path.normcase(str(listener_exe)) != os.path.normcase(str(OLD_LISTENER_EXE))
    ):
        raise RestartBlocked("r1_listener_process_identity_mismatch")
    token, python_exe, codex_exe, config_ok = _validate_environment(
        root, workspace, environment, current_user
    )
    owner_ok = True
    process_rows: list[dict[str, Any]] = []
    worker_count = 0
    for child in descendants:
        try:
            command = child.cmdline()
            child_owner_ok = child.username() == current_user
            owner_ok = owner_ok and child_owner_ok
            process_rows.append(
                {
                    "pid": child.pid,
                    "parent_pid": child.ppid(),
                    "creation_epoch": child.create_time(),
                    "owner_matches_current": child_owner_ok,
                    "executable": Path(child.exe()).name,
                }
            )
            if any("codex_bridge.workflows.worker_entry" in part for part in command):
                worker_count += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            owner_ok = False
    if not owner_ok or worker_count != 0:
        raise RestartBlocked("r1_descendant_owner_or_worker_state_unknown")
    appservers = [
        child
        for child in descendants
        if os.path.normcase(child.exe()) == os.path.normcase(str(codex_exe))
    ]
    if len(appservers) != 1 or appservers[0].username() != current_user:
        raise RestartBlocked("r1_codex_appserver_identity_mismatch")
    appserver_environment = appservers[0].environ()
    isolated_keys = (
        "CODEX_BRIDGE_CONFIG",
        "CODEX_HOME",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "PYTHONPATH",
    )
    if any(appserver_environment.get(key) != environment.get(key) for key in isolated_keys):
        raise RestartBlocked("r1_codex_appserver_scope_mismatch")
    ports_ok, port_rows = _owned_listener_rows(OLD_BRIDGE_PID)
    if not ports_ok or any(
        len(port_rows[port]) != 1 or port_rows[port][0]["pid"] != OLD_LISTENER_PID
        for port in (MCP_PORT, UI_PORT)
    ):
        raise RestartBlocked("r1_listener_port_ownership_mismatch")
    acl_rows = _acl_snapshot(root, workspace, state_root, run_dir)
    if not _acl_scope_isolated(acl_rows):
        raise RestartBlocked("r1_isolated_acl_scope_mismatch")
    state = _sqlite_snapshot(state_root, run_dir)
    if not _expected_sqlite_state(state):
        raise RestartBlocked("r1_saved_ledger_not_terminal_or_unambiguous")
    artifacts = _artifact_snapshot(root, state_root, run_dir)
    if any(
        artifacts.get(name, {}).get("sha256") != expected
        for name, expected in EXPECTED_STATIC_SHA256.items()
    ):
        raise RestartBlocked("r1_protected_artifact_hash_mismatch")
    protected = _protected_evidence_hashes()
    if any(protected.get(name) != expected for name, expected in PROTECTED_EVIDENCE_SHA256.items()):
        raise RestartBlocked("protected_evidence_hash_mismatch")
    r1c = _read_json(REPOSITORY_ROOT / "docs" / "phase5f1b-r1c-readback-replay-evidence.json")
    if not replay_history_is_unknown(r1c.get("checks", {})):
        raise RestartBlocked("r1c_unknown_replay_record_changed")
    api = _api_readback(token, state, run_dir)
    threads = api["threads"]
    if api["status"] != "PASS":
        raise RestartBlocked("r1_current_get_readback_failed")
    if api["hashes"] != EXPECTED_PRE_RESTART_API_SHA256:
        raise RestartBlocked("r1_current_get_projection_differs_from_r1c")
    if threads["count"] != 3 or threads["external_count"] != 0:
        raise RestartBlocked("r1_thread_scope_not_exact")
    return {
        "root": root,
        "state_root": state_root,
        "run_dir": run_dir,
        "workspace": workspace,
        "parent": parent,
        "old_cwd": old_cwd,
        "old_descendants": [(child.pid, child.create_time()) for child in descendants],
        "environment": environment,
        "token": token,
        "python_exe": python_exe,
        "codex_exe": codex_exe,
        "state": state,
        "artifacts": artifacts,
        "protected_evidence": protected,
        "acl": acl_rows,
        "api": api,
        "threads": threads,
        "process_rows": process_rows,
        "port_rows": port_rows,
        "worker_count": worker_count,
        "source_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "branch": subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "config_validated": config_ok,
        "started_at_utc": _utc_now(),
    }


def _wait_for_shutdown(context: Mapping[str, Any]) -> dict[str, Any]:
    old_descendants = context["old_descendants"]
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        parent_state = _process_matches(OLD_BRIDGE_PID, OLD_BRIDGE_CREATED)
        descendant_states = [_process_matches(pid, created) for pid, created in old_descendants]
        parent_stopped = parent_state is False
        descendants_stopped = all(state is False for state in descendant_states)
        ports_released, remaining = _ports_are_free()
        if parent_stopped and descendants_stopped and ports_released:
            break
        time.sleep(0.25)
    parent_state = _process_matches(OLD_BRIDGE_PID, OLD_BRIDGE_CREATED)
    descendant_states = [_process_matches(pid, created) for pid, created in old_descendants]
    parent_stopped = parent_state is False
    descendants_stopped = all(state is False for state in descendant_states)
    ports_released, remaining = _ports_are_free()
    return {
        "parent_stopped": parent_stopped,
        "descendants_stopped": descendants_stopped,
        "ports_released": ports_released,
        "process_identity_checks_complete": parent_state is not None
        and all(state is not None for state in descendant_states),
        "unverified_process_count": sum(state is None for state in descendant_states)
        + int(parent_state is None),
        "remaining_listeners": remaining,
        "forced_termination": False,
    }


def _start_bridge(context: Mapping[str, Any]) -> subprocess.Popen[bytes]:
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    return subprocess.Popen(
        [str(context["python_exe"]), "-m", "codex_bridge"],
        cwd=str(context["old_cwd"]),
        env=dict(context["environment"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def _wait_for_health(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, payload = _request_json("GET", "/healthz", timeout=1.0)
        except RestartBlocked:
            time.sleep(0.25)
            continue
        if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
            return True
        time.sleep(0.25)
    return False


def _new_process_snapshot(
    process: subprocess.Popen[bytes], context: Mapping[str, Any]
) -> dict[str, Any]:
    owner = psutil.Process().username()
    parent = psutil.Process(process.pid)
    descendants = parent.children(recursive=True)
    ports_ok, port_rows = _owned_listener_rows(process.pid)
    workers, worker_tree_complete = _worker_process_count(process.pid)
    rows: list[dict[str, Any]] = []
    complete = True
    for child in descendants:
        try:
            rows.append(
                {
                    "pid": child.pid,
                    "parent_pid": child.ppid(),
                    "creation_epoch": child.create_time(),
                    "owner_matches_current": child.username() == owner,
                    "executable": Path(child.exe()).name,
                }
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            complete = False
    listener_pids = {
        row["pid"]
        for port_rows in port_rows.values()
        for row in port_rows
        if isinstance(row.get("pid"), int)
    }
    listener = psutil.Process(next(iter(listener_pids))) if len(listener_pids) == 1 else None
    listener_pid = listener.pid if listener is not None else None
    appservers = [
        child
        for child in descendants
        if os.path.normcase(child.exe()) == os.path.normcase(str(context["codex_exe"]))
    ]
    isolated_keys = (
        "CODEX_BRIDGE_CONFIG",
        "CODEX_HOME",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "PYTHONPATH",
    )
    process_environment = parent.environ()
    process_environment_isolated = all(
        process_environment.get(key) == context["environment"].get(key) for key in isolated_keys
    )
    appserver_environment_isolated = False
    if len(appservers) == 1:
        appserver_environment = appservers[0].environ()
        appserver_environment_isolated = all(
            appserver_environment.get(key) == context["environment"].get(key)
            for key in isolated_keys
        )
    ready = (
        parent.is_running()
        and parent.username() == owner
        and os.path.normcase(parent.exe()) == os.path.normcase(str(context["python_exe"]))
        and os.path.normcase(parent.cwd()) == os.path.normcase(str(context["old_cwd"]))
        and complete
        and all(row["owner_matches_current"] for row in rows)
        and ports_ok
        and all(
            len(port_rows[port]) == 1 and port_rows[port][0]["pid"] == listener_pid
            for port in (MCP_PORT, UI_PORT)
        )
        and worker_tree_complete
        and workers == 0
        and listener is not None
        and listener.ppid() == parent.pid
        and listener.username() == owner
        and listener.is_running()
        and len(appservers) == 1
        and appservers[0].username() == owner
        and process_environment_isolated
        and appserver_environment_isolated
    )
    return {
        "ready": ready,
        "parent_pid": parent.pid,
        "parent_creation_epoch": parent.create_time(),
        "parent_executable": Path(parent.exe()).name,
        "parent_owner_matches_current": parent.username() == owner,
        "parent_cwd_isolated": os.path.normcase(parent.cwd())
        == os.path.normcase(str(context["old_cwd"])),
        "listener_pid": listener.pid if listener is not None else None,
        "listener_creation_epoch": listener.create_time() if listener is not None else None,
        "listener_parent_pid": listener.ppid() if listener is not None else None,
        "listener_executable": Path(listener.exe()).name if listener is not None else None,
        "appserver_pid": appservers[0].pid if len(appservers) == 1 else None,
        "environment_isolated": process_environment_isolated and appserver_environment_isolated,
        "descendant_count": len(rows),
        "descendants": rows,
        "ports": port_rows,
        "worker_process_count": workers,
    }


def _main_db_hashes(artifacts: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        name: artifacts.get(name, {}).get("sha256")
        for name in ("requests.sqlite", "codex_jobs.sqlite", "dbos.sqlite")
    }


def run_restart_readback() -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": "codexbridge.phase5f1b.r1d-restart-readback.v1",
        "phase": "Phase5F-1D",
        "status": "BLOCKED",
        "recorded_at_utc": _utc_now(),
        "source_commit_sha": None,
        "scope": {
            "root_label": ROOT_LABEL,
            "workflow_id": WORKFLOW_ID,
            "run_id": RUN_ID,
            "client_request_id": CLIENT_REQUEST_ID,
            "new_workflow_posts": 0,
            "same_id_replay_posts": 0,
            "new_codex_jobs": 0,
            "new_codex_threads": 0,
            "old_initial_bridge_pid_3584_touched": False,
            "production_side_effects": 0,
            "cleanup_performed": False,
        },
        "preflight": {"status": "NOT_RUN"},
        "shutdown": {"status": "NOT_RUN", "post_count": 0},
        "restart": {"status": "NOT_RUN"},
        "readback": {"status": "NOT_RUN"},
        "replay": {
            "outcome": "UNKNOWN_NO_RETRY",
            "post_attempted": "UNKNOWN",
            "possible_post_count_range": [0, 1],
            "accepted_response": "UNKNOWN",
            "accepted_http_status": None,
            "retry_count": 0,
            "api_projection_identical_before_and_after": "UNKNOWN",
            "state_after_reconciliation_unchanged": True,
        },
        "cleanup_status": "NOT_RUN",
        "blocker": None,
    }
    token: str | None = None
    context: dict[str, Any] | None = None
    if EVIDENCE_PATH.exists() or EVIDENCE_PATH.is_symlink():
        evidence["blocker"] = "new_evidence_path_already_exists"
        return evidence
    try:
        context = _preflight()
        token = context["token"]
        evidence["source_commit_sha"] = context["source_sha"]
        evidence["preflight"] = {
            "status": "PASS",
            "branch": context["branch"],
            "parent_pid": context["parent"].pid,
            "parent_creation_epoch": context["parent"].create_time(),
            "parent_executable": Path(context["parent"].exe()).name,
            "parent_owner_matches_current": True,
            "parent_cwd_isolated": True,
            "listener_pid": OLD_LISTENER_PID,
            "listener_creation_epoch": OLD_LISTENER_CREATED,
            "listener_executable": OLD_LISTENER_EXE.name,
            "descendant_count": len(context["process_rows"]),
            "descendants": context["process_rows"],
            "ports": context["port_rows"],
            "environment_isolated": True,
            "config_validated": context["config_validated"],
            "acl_validated": True,
            "acl": context["acl"],
            "worker_process_count": context["worker_count"],
            "auth": {
                "capabilities_http": context["api"]["http"]["capabilities"],
                "missing_token_http": context["api"]["http"]["missing_token"],
                "malformed_token_http": context["api"]["http"]["malformed_token"],
            },
            "api_hashes": context["api"]["hashes"],
            "threads": {
                "count": context["threads"]["count"],
                "external_count": context["threads"]["external_count"],
                "all_terminal_no_pending": context["threads"]["all_terminal_no_pending"],
            },
            "ledger": {
                "request_count": len(context["state"]["requests"]),
                "client_request_id": context["state"]["requests"][0]["client_request_id"],
                "request_digest": context["state"]["requests"][0]["request_digest"],
                "request_state": context["state"]["requests"][0]["state"],
                "job_count": len(context["state"]["jobs"]),
                "jobs": [
                    {
                        key: row[key]
                        for key in (
                            "job_id",
                            "task_identity",
                            "request_digest",
                            "client_request_id",
                            "state",
                            "thread_id",
                            "turn_id",
                            "remote_state",
                        )
                    }
                    for row in context["state"]["jobs"]
                ],
                "job_states": [row["state"] for row in context["state"]["jobs"]],
                "remote_states": [row["remote_state"] for row in context["state"]["jobs"]],
                "event_count": context["state"]["event_count"],
                "event_state_counts": context["state"]["events"],
                "reconciliation_count": context["state"]["reconciliation_count"],
                "dbos_status": context["state"]["dbos"][0]["status"],
                "worker_status": context["state"]["worker_status"],
                "result_rows": context["state"]["result_rows"],
                "result_final_texts": context["state"]["result_final_texts"],
            },
            "artifacts": context["artifacts"],
            "protected_evidence_sha256": context["protected_evidence"],
        }
        evidence["started_at_utc"] = context["started_at_utc"]
        if context["branch"] != "codex/phase5f1b-three-job-e2e":
            raise RestartBlocked("checkout_branch_mismatch")
        post_count = 1
        status, payload = shutdown_once(
            lambda: _request_json(
                "POST",
                "/ui-api/control/shutdown",
                token=token,
                payload={},
                timeout=5.0,
            )
        )
        evidence["shutdown"] = {
            "post_count": post_count,
            "http_status": status,
            "accepted": isinstance(payload, dict) and payload.get("status") == "shutdown_requested",
            "requested_at_utc": _utc_now(),
            "forced_termination": False,
        }
        stopped = _wait_for_shutdown(context)
        state_after_shutdown = _sqlite_snapshot(context["state_root"], context["run_dir"])
        artifacts_after_shutdown = _artifact_snapshot(
            context["root"], context["state_root"], context["run_dir"]
        )
        state_unchanged = state_after_shutdown == context["state"]
        main_hashes_unchanged = _main_db_hashes(artifacts_after_shutdown) == _main_db_hashes(
            context["artifacts"]
        )
        eligible = (
            restart_eligible(
                shutdown_status=status,
                parent_stopped=stopped["parent_stopped"],
                descendants_stopped=stopped["descendants_stopped"],
                ports_released=stopped["ports_released"],
                state_unchanged=state_unchanged and main_hashes_unchanged,
            )
            and evidence["shutdown"]["accepted"]
        )
        evidence["shutdown"].update(stopped)
        evidence["shutdown"].update(
            {
                "state_unchanged": state_unchanged,
                "main_db_hashes_unchanged": main_hashes_unchanged,
                "sidecars_after_shutdown": {
                    key: value
                    for key, value in artifacts_after_shutdown.items()
                    if key.endswith("-wal") or key.endswith("-shm")
                },
                "sidecars_match_preflight": {
                    key: artifacts_after_shutdown.get(key) == context["artifacts"].get(key)
                    for key in artifacts_after_shutdown
                    if key.endswith("-wal") or key.endswith("-shm")
                },
            }
        )
        if not eligible:
            evidence["blocker"] = "graceful_shutdown_or_state_verification_incomplete"
            return evidence
        evidence["restart"]["status"] = "STARTING"
        evidence["restart"]["started_at_utc"] = _utc_now()
        process = _start_bridge(context)
        evidence["restart"]["parent_pid"] = process.pid
        if not _wait_for_health(process):
            evidence["restart"]["status"] = "BLOCKED"
            evidence["restart"]["blocker"] = "new_bridge_health_not_confirmed_no_retry"
            evidence["restart"]["process_alive"] = process.poll() is None
            evidence["blocker"] = evidence["restart"]["blocker"]
            return evidence
        new_process = _new_process_snapshot(process, context)
        evidence["restart"].update(new_process)
        evidence["restart"]["status"] = "PASS" if new_process["ready"] else "BLOCKED"
        if not new_process["ready"]:
            evidence["blocker"] = "new_bridge_process_scope_or_worker_count_unverified"
            return evidence
        state_after_restart = _sqlite_snapshot(context["state_root"], context["run_dir"])
        artifacts_after_restart = _artifact_snapshot(
            context["root"], context["state_root"], context["run_dir"]
        )
        acl_after_restart = _acl_snapshot(
            context["root"],
            context["workspace"],
            context["state_root"],
            context["run_dir"],
        )
        api_after = _api_readback(token, state_after_restart, context["run_dir"])
        if api_after["status"] != "PASS":
            evidence["readback"] = {"status": "BLOCKED", "canonical_api": api_after}
            evidence["blocker"] = "post_restart_canonical_get_failed"
            return evidence
        qt_result = _qt_readback(
            token,
            api_after["_payloads"][0],
            api_after["_payloads"][1],
        )
        protected_after = _protected_evidence_hashes()
        final_workers, worker_tree_complete = _worker_process_count(process.pid)
        ports_still_owned, ports_after = _owned_listener_rows(process.pid)
        state_unchanged_after_restart = state_after_restart == context["state"]
        acl_unchanged = acl_after_restart == context["acl"] and _acl_scope_isolated(
            acl_after_restart
        )
        protected_unchanged = protected_after == context["protected_evidence"]
        static_artifacts_unchanged = all(
            artifacts_after_restart.get(name, {}).get("sha256")
            == context["artifacts"].get(name, {}).get("sha256")
            for name in (
                "bridge.toml",
                "workflow.yaml",
                "input.json",
                "manifest.json",
                "result.json",
                "task-events.jsonl",
                "initial-worker-heartbeat.jsonl",
                "start-worker.json",
                "start-worker-identity.json",
                "worker-entry-status.json",
                "requests.sqlite",
                "codex_jobs.sqlite",
                "dbos.sqlite",
            )
        )
        api_hashes_same = api_after["hashes"] == context["api"]["hashes"]
        readback_ok = all(
            (
                api_after["status"] == "PASS",
                api_hashes_same,
                qt_result["status"] == "PASS",
                state_unchanged_after_restart,
                protected_unchanged,
                static_artifacts_unchanged,
                acl_unchanged,
                worker_tree_complete,
                final_workers == 0,
                ports_still_owned,
            )
        )
        evidence["readback"] = {
            "status": "PASS" if readback_ok else "BLOCKED",
            "canonical_api": {
                "status": api_after["status"],
                "http": api_after["http"],
                "hashes": api_after["hashes"],
                "matches_pre_restart": api_hashes_same,
                "workflow_count": api_after["workflow_count"],
                "workflow_id": api_after["workflow_id"],
                "workflow_status": api_after["workflow_status"],
                "jobs": api_after["detail_jobs"],
                "result_matches_saved": api_after["result_matches_saved"],
                "result_final_texts": api_after["result_final_texts"],
                "threads": api_after["threads"],
            },
            "qt": qt_result,
            "ledger_unchanged": state_unchanged_after_restart,
            "static_artifacts_unchanged": static_artifacts_unchanged,
            "protected_evidence_unchanged": protected_unchanged,
            "protected_evidence_sha256_after": protected_after,
            "acl_unchanged": acl_unchanged,
            "acl_after_restart": acl_after_restart,
            "worker_process_count_after_gets": final_workers,
            "listener_ownership_after_gets": ports_still_owned,
            "listener_ports_after_gets": ports_after,
            "sidecars_after_restart": {
                key: value
                for key, value in artifacts_after_restart.items()
                if key.endswith("-wal") or key.endswith("-shm")
            },
            "sidecars_match_preflight": {
                key: artifacts_after_restart.get(key) == context["artifacts"].get(key)
                for key in artifacts_after_restart
                if key.endswith("-wal") or key.endswith("-shm")
            },
            "artifacts_after_restart": artifacts_after_restart,
        }
        evidence["restart"]["status"] = "PASS"
        evidence["restart"]["completed_at_utc"] = _utc_now()
        evidence["status"] = "PASS" if readback_ok else "BLOCKED"
        if not readback_ok:
            evidence["blocker"] = "post_restart_readback_or_immutability_check_failed"
    except RestartBlocked as error:
        evidence["blocker"] = error.code
    except Exception as error:
        evidence["blocker"] = "unexpected_error"
        evidence["error_type"] = type(error).__name__
    finally:
        evidence["recorded_at_utc"] = _utc_now()
        if context is not None:
            evidence["source_commit_sha"] = context["source_sha"]
            token = context["token"]
        if not EVIDENCE_PATH.exists() and not EVIDENCE_PATH.is_symlink():
            evidence["evidence_write_status"] = "WRITTEN_ONCE"
            try:
                write_evidence_once(
                    EVIDENCE_PATH,
                    evidence,
                    sensitive_values=(token,) if token else (),
                )
            except Exception as error:
                evidence["evidence_write_status"] = "BLOCKED"
                evidence["evidence_write_error_type"] = type(error).__name__
                evidence["status"] = "BLOCKED"
                evidence["blocker"] = evidence.get("blocker") or "evidence_write_failed"
        else:
            evidence["evidence_write_status"] = "NOT_RUN_PATH_EXISTS"
    return evidence


def main() -> int:
    evidence = run_restart_readback()
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if evidence.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
