from __future__ import annotations

import copy
import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from interpreter import (
    Interpreter,
    OwsError,
    compute_codex_job_request,
    parse_workflow_text,
    validate_official_schema,
    validate_workflow,
)

OWS_DIR = Path(__file__).resolve().parents[1]
FIXTURE = OWS_DIR / "fixtures" / "codex_job_workflow.yaml"
THREAD_ID = "thread_123456"
TURN_ID = "turn_123456"
CONTROL_TOKEN = "test-only-control-token"
SAFE_PROMPT = (
    "2+2を計算し、外部ツール・コマンド・ファイル操作・Git操作・ネットワーク操作を行わず、"
    "回答本文に4の1文字だけを返すこと"
)


def _definition() -> dict[str, Any]:
    return parse_workflow_text(FIXTURE.read_text(encoding="utf-8"))


def test_codex_job_fixture_is_schema_valid_and_requires_live_mode() -> None:
    definition = _definition()
    validate_official_schema(definition)

    with pytest.raises(OwsError, match="requires --codex-job-live"):
        validate_workflow(definition)

    validate_workflow(definition, codex_job_live=True)
    with pytest.raises(OwsError, match="codex-job live adapter"):
        Interpreter(definition, codex_job_live=True).run({})


def test_codex_job_rejects_arbitrary_prompt_in_live_mode() -> None:
    definition = _definition()
    definition["do"][0]["calculate"]["with"]["prompt"] = "run arbitrary work"

    with pytest.raises(OwsError, match="fixed safe prompt"):
        validate_workflow(definition, codex_job_live=True)


def test_codex_job_rejects_extra_with_fields() -> None:
    definition = _definition()
    definition["do"][0]["calculate"]["with"]["model"] = "some-model"

    with pytest.raises(OwsError, match="unsupported codex-job argument"):
        validate_workflow(definition, codex_job_live=True)


def test_codex_job_request_is_the_fixed_literal_only() -> None:
    definition = _definition()
    task_spec = definition["do"][0]["calculate"]

    assert compute_codex_job_request(task_spec, {"ignored": True}, {}) == {"prompt": SAFE_PROMPT}


def test_live_mode_requires_one_codex_job_and_rejects_mock_mixing() -> None:
    definition = _definition()
    duplicate = copy.deepcopy(definition["do"][0])
    duplicate["calculate2"] = copy.deepcopy(duplicate.pop("calculate"))
    definition["do"].insert(1, duplicate)
    definition["do"].append(
        {
            "mock": {
                "call": "mock-job",
                "with": {"operation": "process", "payload": 1, "duration_ms": 0},
            }
        }
    )

    with pytest.raises(OwsError, match="exactly one codex-job call"):
        validate_workflow(definition, codex_job_live=True)


def test_live_mode_rejects_mock_call_even_with_one_codex_job() -> None:
    definition = _definition()
    definition["do"].append(
        {
            "mock": {
                "call": "mock-job",
                "with": {"operation": "process", "payload": 1, "duration_ms": 0},
            }
        }
    )

    with pytest.raises(OwsError, match="cannot be mixed with mock-job"):
        validate_workflow(definition, codex_job_live=True)


def test_codex_job_is_rejected_inside_a_loop() -> None:
    definition = _definition()
    call = definition["do"].pop(0)
    definition["do"].insert(
        0,
        {
            "repeat": {
                "for": {"in": "${ [1] }"},
                "do": [call],
            }
        },
    )

    with pytest.raises(OwsError, match="may not be nested under for"):
        validate_workflow(definition, codex_job_live=True)


class _FakeBridgeState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.paths: list[str] = []
        self.post_count = 0
        self.request_body: dict[str, Any] | None = None
        self.auth_ok = False
        self.origin_seen = False
        self.ledger_path: Path | None = None
        self.ledger_state_at_post: str | None = None
        self.post_status = 200
        self.post_body: Any = {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "state": "in_progress",
        }
        self.drop_post_response = False
        self.status = "in_progress"
        self.turns: list[dict[str, Any]] | None = None
        self.items: list[dict[str, Any]] | None = None
        self.health_body: Any = {"status": "ok"}
        self.bridge_status_body: Any = {"bridge": "ready", "app_server": "ready"}
        self.capabilities_status = 200
        self.delay_post_seconds = 0.0


class _FakeBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _reply(self, status: int, value: Any) -> None:
        body = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def state(self) -> _FakeBridgeState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        with self.state.lock:
            self.state.paths.append(self.path)
            self.state.origin_seen = (
                self.state.origin_seen or self.headers.get("Origin") is not None
            )
        if parsed.path == "/healthz":
            self._reply(200, self.state.health_body)
        elif parsed.path == "/ui-api/status":
            self._reply(200, self.state.bridge_status_body)
        elif parsed.path == "/ui-api/control/capabilities":
            with self.state.lock:
                self.state.auth_ok = self.headers.get("Authorization") == f"Bearer {CONTROL_TOKEN}"
            self._reply(self.state.capabilities_status, {"models": []})
        elif parsed.path.endswith("/status"):
            self._reply(
                200,
                {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": self.state.status},
            )
        elif parsed.path.endswith("/turns"):
            turns = self.state.turns
            if turns is None:
                turns = [{"id": TURN_ID, "status": self.state.status}]
            self._reply(200, {"thread_id": THREAD_ID, "turns": turns})
        elif parsed.path.endswith("/items"):
            items = self.state.items
            if items is None:
                items = []
                if self.state.status == "completed":
                    items = [
                        {
                            "turn_id": TURN_ID,
                            "item": {
                                "type": "agentMessage",
                                "text": "4",
                                "phase": "final_answer",
                            },
                        }
                    ]
            self._reply(200, {"thread_id": THREAD_ID, "turn_id": TURN_ID, "items": items})
        elif parsed.path == "/oversized":
            self._reply(200, b"x" * 4096)
        elif parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/should-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        with self.state.lock:
            self.state.paths.append(self.path)
            self.state.post_count += 1
            self.state.origin_seen = (
                self.state.origin_seen or self.headers.get("Origin") is not None
            )
        content_length = int(self.headers.get("Content-Length", "0"))
        self.state.request_body = json.loads(self.rfile.read(content_length))
        if self.state.ledger_path is not None:
            try:
                with sqlite3.connect(self.state.ledger_path) as connection:
                    row = connection.execute("SELECT state FROM jobs").fetchone()
                self.state.ledger_state_at_post = row[0] if row else None
            except sqlite3.Error:
                self.state.ledger_state_at_post = None
        if self.state.delay_post_seconds:
            time.sleep(self.state.delay_post_seconds)
        if self.state.drop_post_response:
            self.close_connection = True
            return
        self._reply(self.state.post_status, self.state.post_body)


class _FakeBridgeServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        return


@pytest.fixture
def fake_bridge(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", CONTROL_TOKEN)
    server = _FakeBridgeServer(("127.0.0.1", 0), _FakeBridgeHandler)
    server.daemon_threads = True
    server.state = _FakeBridgeState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_client_preflights_with_bearer_without_origin_or_proxy(
    fake_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_http_client import CodexHTTPClient

    monkeypatch.setenv("HTTP_PROXY", "http://192.0.2.10:8123")
    monkeypatch.setenv("http_proxy", "http://192.0.2.10:8123")
    client = CodexHTTPClient(port=fake_bridge.server_address[1])
    client.preflight()

    state = fake_bridge.state
    assert state.paths == [
        "/healthz",
        "/ui-api/status",
        "/ui-api/control/capabilities",
    ]
    assert state.auth_ok is True
    assert state.origin_seen is False


def test_http_client_does_not_follow_redirects(fake_bridge) -> None:
    from codex_http_client import CodexHTTPClient, CodexHTTPError

    with pytest.raises(CodexHTTPError, match="HTTP 302"):
        CodexHTTPClient(port=fake_bridge.server_address[1])._request_json("GET", "/redirect")
    assert fake_bridge.state.paths == ["/redirect"]


def test_http_client_rejects_non_loopback_and_invalid_limits() -> None:
    from codex_http_client import CodexHTTPClient

    with pytest.raises(ValueError, match="loopback"):
        CodexHTTPClient(host="192.0.2.10", port=8001)
    with pytest.raises(ValueError, match="port"):
        CodexHTTPClient(port=70000)
    with pytest.raises(ValueError, match="timeout"):
        CodexHTTPClient(timeout_seconds=0)


def test_http_client_requires_environment_token_before_any_request(
    fake_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_http_client import CodexHTTPClient, CodexPreflightError

    monkeypatch.delenv("CODEX_BRIDGE_CONTROL_TOKEN")
    with pytest.raises(CodexPreflightError, match="not configured"):
        CodexHTTPClient(port=fake_bridge.server_address[1]).preflight()
    assert fake_bridge.state.paths == []


def test_http_client_enforces_response_limit(fake_bridge) -> None:
    from codex_http_client import CodexHTTPClient, CodexProtocolError

    client = CodexHTTPClient(
        port=fake_bridge.server_address[1],
        max_response_bytes=1024,
    )
    with pytest.raises(CodexProtocolError, match="response limit"):
        client._request_json("GET", "/oversized")


def _codex_adapter(tmp_path: Path):
    from codex_job_adapter import CodexJobAdapter

    return CodexJobAdapter(tmp_path / "codex_jobs.sqlite")


def _start_codex_job(adapter, fake_bridge, run_dir: Path, **client_options: Any):
    from codex_http_client import CodexHTTPClient

    client = CodexHTTPClient(
        port=fake_bridge.server_address[1],
        **client_options,
    )
    return adapter.start_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_PROMPT,
        run_dir,
        client,
    )


def test_codex_job_commits_write_ahead_before_exact_control_post(
    tmp_path: Path, fake_bridge
) -> None:
    adapter = _codex_adapter(tmp_path)
    fake_bridge.state.ledger_path = tmp_path / "codex_jobs.sqlite"

    job = _start_codex_job(adapter, fake_bridge, tmp_path)

    request = fake_bridge.state.request_body
    assert fake_bridge.state.post_count == 1
    assert fake_bridge.state.ledger_state_at_post == "MAYBE_SENT"
    assert job["state"] == "ACCEPTED"
    assert job["thread_id"] == THREAD_ID
    assert job["turn_id"] == TURN_ID
    assert set(request) == {
        "client_request_id",
        "cwd",
        "prompt",
        "title",
        "sandbox_mode",
        "confirm_full_access",
    }
    assert request["client_request_id"].startswith("ows4d-")
    assert request["prompt"] == SAFE_PROMPT
    assert request["sandbox_mode"] == "inherit"
    assert request["confirm_full_access"] is False
    assert job["job_id"] in request["title"]
    assert Path(request["cwd"]).is_dir()
    assert list(Path(request["cwd"]).iterdir()) == []
    assert fake_bridge.state.auth_ok is True


def test_codex_job_same_digest_reuses_id_and_changed_target_conflicts(
    tmp_path: Path, fake_bridge
) -> None:
    adapter = _codex_adapter(tmp_path)
    job = _start_codex_job(adapter, fake_bridge, tmp_path)
    resumed = _start_codex_job(adapter, fake_bridge, tmp_path)

    assert resumed["job_id"] == job["job_id"]
    assert resumed["request_digest"] == job["request_digest"]
    assert fake_bridge.state.post_count == 1

    from codex_job_adapter import CodexJobConflictError

    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_PROMPT,
            tmp_path,
            port=fake_bridge.server_address[1] + 1,
        )
    assert adapter.get_job_status(job_id=job["job_id"])["job_id"] == job["job_id"]


def test_codex_job_prepared_crash_can_send_once_after_reopen(tmp_path: Path, fake_bridge) -> None:
    adapter = _codex_adapter(tmp_path)
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_PROMPT,
        tmp_path,
        port=fake_bridge.server_address[1],
    )
    reopened = _codex_adapter(tmp_path)
    result = _start_codex_job(reopened, fake_bridge, tmp_path)

    assert prepared["state"] == "PREPARED"
    assert result["job_id"] == prepared["job_id"]
    assert fake_bridge.state.post_count == 1


def test_codex_adapter_rejects_arbitrary_prompt_before_network_or_job_insert(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobRequestError

    adapter = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobRequestError, match="fixed safe prompt"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            "run arbitrary work",
            tmp_path,
            port=fake_bridge.server_address[1],
        )

    with sqlite3.connect(tmp_path / "codex_jobs.sqlite") as connection:
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert job_count == 0
    assert fake_bridge.state.paths == []


def test_codex_job_maybe_sent_crash_never_posts_again(tmp_path: Path, fake_bridge) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    adapter = _codex_adapter(tmp_path)
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_PROMPT,
        tmp_path,
        port=fake_bridge.server_address[1],
    )
    assert adapter.claim_post(prepared["job_id"]) is True
    assert adapter.claim_post(prepared["job_id"]) is False
    reopened = _codex_adapter(tmp_path)

    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(reopened, fake_bridge, tmp_path)

    job = reopened.get_job_status(job_id=prepared["job_id"])
    assert fake_bridge.state.post_count == 0
    assert job["state"] == "MAYBE_SENT"
    assert job["status"] == "NEEDS_RECONCILIATION"


@pytest.mark.parametrize("status", [400, 503])
def test_codex_job_http_errors_become_reconciliation_without_retry(
    tmp_path: Path, fake_bridge, status: int
) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    fake_bridge.state.post_status = status
    fake_bridge.state.post_body = {"error": CONTROL_TOKEN}
    adapter = _codex_adapter(tmp_path)

    with pytest.raises(CodexJobReconciliationRequiredError) as error:
        _start_codex_job(adapter, fake_bridge, tmp_path)
    assert CONTROL_TOKEN not in str(error.value)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(_codex_adapter(tmp_path), fake_bridge, tmp_path)

    job = adapter.get_job_status(request_key="workflow-12345678::do[0].calculate")
    assert fake_bridge.state.post_count == 1
    assert job["state"] == "MAYBE_SENT"
    assert job["status"] == "NEEDS_RECONCILIATION"
    assert CONTROL_TOKEN not in (tmp_path / "codex_jobs.sqlite").read_bytes().decode("latin-1")


def test_codex_job_lost_response_is_never_reposted_after_process_recovery(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    fake_bridge.state.drop_post_response = True
    adapter = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(adapter, fake_bridge, tmp_path, timeout_seconds=0.2)

    reopened = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(reopened, fake_bridge, tmp_path, timeout_seconds=0.2)

    jobs = reopened.read_events(after_id=0, limit=100)
    assert fake_bridge.state.post_count == 1
    assert sum(event["state"] == "MAYBE_SENT" for event in jobs) == 1
    assert CONTROL_TOKEN not in (tmp_path / "codex_jobs.sqlite").read_bytes().decode("latin-1")


def test_codex_job_post_timeout_is_never_reposted_after_process_recovery(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    fake_bridge.state.delay_post_seconds = 0.2
    adapter = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(adapter, fake_bridge, tmp_path, timeout_seconds=0.05)

    reopened = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(reopened, fake_bridge, tmp_path, timeout_seconds=0.2)

    assert fake_bridge.state.post_count == 1
    assert (
        reopened.get_job_status(request_key="workflow-12345678::do[0].calculate")["status"]
        == "NEEDS_RECONCILIATION"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"thread_id": "short", "turn_id": TURN_ID, "state": "in_progress"},
        {"thread_id": "t" * 101, "turn_id": TURN_ID, "state": "in_progress"},
        {"thread_id": THREAD_ID, "turn_id": "", "state": "in_progress"},
        {"thread_id": THREAD_ID, "state": "in_progress"},
        [],
        b"not-json",
    ],
)
def test_codex_job_invalid_post_body_is_reconciliation_only(
    tmp_path: Path, fake_bridge, body: Any
) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    fake_bridge.state.post_body = body
    adapter = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobReconciliationRequiredError):
        _start_codex_job(adapter, fake_bridge, tmp_path)

    assert fake_bridge.state.post_count == 1
    job = adapter.get_job_status(request_key="workflow-12345678::do[0].calculate")
    assert job["state"] == "MAYBE_SENT"
    assert job["status"] == "NEEDS_RECONCILIATION"


def test_control_token_echoed_as_thread_id_is_not_persisted(
    tmp_path: Path, fake_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_job_adapter import CodexJobReconciliationRequiredError

    token = "thread_SECRET123"
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", token)
    fake_bridge.state.post_body = {
        "thread_id": token,
        "turn_id": TURN_ID,
        "state": "in_progress",
    }
    adapter = _codex_adapter(tmp_path)

    with pytest.raises(CodexJobReconciliationRequiredError) as error:
        _start_codex_job(adapter, fake_bridge, tmp_path)

    assert token not in str(error.value)
    assert token not in (tmp_path / "codex_jobs.sqlite").read_bytes().decode("latin-1")
    job = adapter.get_job_status(request_key="workflow-12345678::do[0].calculate")
    assert job["thread_id"] is None
    assert job["status"] == "NEEDS_RECONCILIATION"
    assert fake_bridge.state.post_count == 1


def test_codex_job_rejects_second_call_in_same_workflow_before_post(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobConflictError

    adapter = _codex_adapter(tmp_path)
    first = _start_codex_job(adapter, fake_bridge, tmp_path)
    from codex_http_client import CodexHTTPClient

    with pytest.raises(CodexJobConflictError, match="one live job"):
        adapter.start_job(
            "workflow-12345678",
            "do[1].calculate-again",
            SAFE_PROMPT,
            tmp_path,
            CodexHTTPClient(port=fake_bridge.server_address[1]),
        )

    assert fake_bridge.state.post_count == 1
    assert adapter.get_job_status(job_id=first["job_id"])["job_id"] == first["job_id"]


@pytest.mark.parametrize(
    ("remote_state", "expected_state", "expected_status"),
    [
        ("in_progress", "RUNNING", "RUNNING"),
        ("completed", "SUCCEEDED", "SUCCEEDED"),
        ("failed", "FAILED", "FAILED"),
        ("interrupted", "FAILED", "FAILED"),
        ("needs_approval", "ACCEPTED", "NEEDS_RECONCILIATION"),
        ("needs_input", "ACCEPTED", "NEEDS_RECONCILIATION"),
        ("not_loaded", "ACCEPTED", "ACCEPTED"),
    ],
)
def test_codex_job_observes_exact_turn_terminal_states(
    tmp_path: Path,
    fake_bridge,
    remote_state: str,
    expected_state: str,
    expected_status: str,
) -> None:
    adapter = _codex_adapter(tmp_path)
    started = _start_codex_job(adapter, fake_bridge, tmp_path)
    fake_bridge.state.status = remote_state
    observed = adapter.observe_job(
        started["job_id"],
        __import__("codex_http_client").CodexHTTPClient(port=fake_bridge.server_address[1]),
    )

    assert observed["state"] == expected_state
    assert observed["status"] == expected_status
    assert fake_bridge.state.post_count == 1
    if remote_state == "completed":
        assert observed["receipt"] == {"result": {"final_text": "4"}}
    if remote_state == "interrupted":
        assert observed["receipt"]["remote_state"] == "interrupted"


def test_codex_job_completed_without_its_final_answer_requires_reconciliation(
    tmp_path: Path, fake_bridge
) -> None:
    adapter = _codex_adapter(tmp_path)
    started = _start_codex_job(adapter, fake_bridge, tmp_path)
    fake_bridge.state.status = "completed"
    fake_bridge.state.items = [
        {
            "turn_id": "turn_other_123",
            "item": {"type": "agentMessage", "text": "4", "phase": "final_answer"},
        }
    ]

    observed = adapter.observe_job(
        started["job_id"],
        __import__("codex_http_client").CodexHTTPClient(port=fake_bridge.server_address[1]),
    )

    assert observed["state"] == "ACCEPTED"
    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["receipt"] is None


def test_control_token_echoed_as_final_answer_is_not_persisted(
    tmp_path: Path, fake_bridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_http_client import CodexHTTPClient

    token = "thread_SECRET123"
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", token)
    adapter = _codex_adapter(tmp_path)
    started = _start_codex_job(adapter, fake_bridge, tmp_path)
    fake_bridge.state.status = "completed"
    fake_bridge.state.items = [
        {
            "turn_id": TURN_ID,
            "item": {"type": "agentMessage", "text": token, "phase": "final_answer"},
        }
    ]

    observed = adapter.observe_job(
        started["job_id"], CodexHTTPClient(port=fake_bridge.server_address[1])
    )

    assert token not in (tmp_path / "codex_jobs.sqlite").read_bytes().decode("latin-1")
    job = adapter.get_job_status(job_id=started["job_id"])
    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert job["receipt"] is None
    assert job["status"] == "NEEDS_RECONCILIATION"
    assert fake_bridge.state.post_count == 1


def test_codex_job_preflight_failure_does_not_cross_write_ahead_or_post(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobError

    fake_bridge.state.capabilities_status = 403
    adapter = _codex_adapter(tmp_path)
    with pytest.raises(CodexJobError, match="preflight failed"):
        _start_codex_job(adapter, fake_bridge, tmp_path)

    job = adapter.get_job_status(request_key="workflow-12345678::do[0].calculate")
    assert job["state"] == "PREPARED"
    assert fake_bridge.state.post_count == 0
    assert "/ui-api/control/capabilities" in fake_bridge.state.paths


def test_codex_job_audit_events_match_write_ahead_and_acceptance_transactions(
    tmp_path: Path, fake_bridge
) -> None:
    adapter = _codex_adapter(tmp_path)
    fake_bridge.state.ledger_path = tmp_path / "codex_jobs.sqlite"

    job = _start_codex_job(adapter, fake_bridge, tmp_path)
    events = adapter.read_events(after_id=0, limit=100)

    assert [event["state"] for event in events] == ["PREPARED", "MAYBE_SENT", "ACCEPTED"]
    assert events[1]["payload"] == {"write_ahead_committed": True}
    assert job["request_digest"] == events[0]["payload"]["request_digest"]


def test_repeated_running_observation_does_not_write_unpaired_job_updates(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_http_client import CodexHTTPClient

    adapter = _codex_adapter(tmp_path)
    started = _start_codex_job(adapter, fake_bridge, tmp_path)
    fake_bridge.state.status = "in_progress"
    client = CodexHTTPClient(port=fake_bridge.server_address[1])
    adapter.observe_job(started["job_id"], client)
    before = adapter.get_job_status(job_id=started["job_id"])
    event_count_before = len(adapter.read_events(after_id=0, limit=100))
    time.sleep(0.002)
    adapter.observe_job(started["job_id"], client)
    after = adapter.get_job_status(job_id=started["job_id"])

    assert after["updated_at"] == before["updated_at"]
    assert len(adapter.read_events(after_id=0, limit=100)) == event_count_before


def test_codex_job_rejects_digest_collision_with_changed_canonical_body(
    tmp_path: Path, fake_bridge
) -> None:
    from codex_job_adapter import CodexJobConflictError

    adapter = _codex_adapter(tmp_path)
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_PROMPT,
        tmp_path,
        port=fake_bridge.server_address[1],
    )
    with sqlite3.connect(tmp_path / "codex_jobs.sqlite") as connection:
        connection.execute(
            "UPDATE jobs SET request_json = ? WHERE job_id = ?",
            ('{"prompt":"different canonical body"}', prepared["job_id"]),
        )

    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_PROMPT,
            tmp_path,
            port=fake_bridge.server_address[1],
        )


def test_replayed_start_step_after_accepted_response_reuses_ledger_job(
    tmp_path: Path, fake_bridge
) -> None:
    from worker import _durable_codex_job_start_step

    metadata = {
        "identity": "do[0].calculate",
        "task_name": "calculate",
        "operation": "call",
        "definition_sha256": "definition-digest",
        "input_sha256": "input-digest",
        "context_sha256": "context-digest",
        "variables_sha256": "variables-digest",
    }
    fake_bridge.state.ledger_path = tmp_path / "codex_jobs.sqlite"

    first = _durable_codex_job_start_step(
        str(tmp_path),
        "workflow-12345678",
        "do[0].calculate",
        {"prompt": SAFE_PROMPT},
        fake_bridge.server_address[1],
        metadata,
    )
    replay = _durable_codex_job_start_step(
        str(tmp_path),
        "workflow-12345678",
        "do[0].calculate",
        {"prompt": SAFE_PROMPT},
        fake_bridge.server_address[1],
        metadata,
    )

    assert replay["result"]["job_id"] == first["result"]["job_id"]
    assert replay["result"]["state"] == "ACCEPTED"
    assert fake_bridge.state.post_count == 1


def _cli_args(*, live: bool, port: int) -> SimpleNamespace:
    return SimpleNamespace(
        workflow=FIXTURE,
        input_json="{}",
        max_tasks=1000,
        timeout_seconds=20,
        mock_job_durable_wait=False,
        codex_job_live=live,
        codex_bridge_port=port,
    )


def test_cli_requires_explicit_live_flag_before_creating_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli

    state_root = tmp_path / "phase4d-runs"
    monkeypatch.setattr(cli, "PHASE4D_STATE_ROOT", state_root)

    with pytest.raises(OwsError, match="requires --codex-job-live"):
        cli._run(_cli_args(live=False, port=8001))

    assert not state_root.exists()


def test_cli_live_run_uses_pregenerated_dbos_id_and_can_resume_offline(
    tmp_path: Path,
    fake_bridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli

    fake_bridge.state.status = "completed"
    state_root = tmp_path / "phase4d-runs"
    monkeypatch.setattr(cli, "PHASE4D_STATE_ROOT", state_root)
    monkeypatch.setattr(
        cli,
        "start_mock_job_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("mock service started")),
    )
    result = cli._run(_cli_args(live=True, port=fake_bridge.server_address[1]))

    run_dir = Path(result["run_dir"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert result["workflow_id"] == manifest["workflow_id"]
    assert result["mode"] == "codex-job-live"
    assert result["result"]["status"] == "SUCCEEDED"
    assert result["result"]["result"] == {"final_text": "4"}
    assert manifest["mode"] == "codex-job-live"
    assert manifest["control"]["codex_job_live"] is True
    assert manifest["control"]["codex_bridge_port"] == fake_bridge.server_address[1]
    assert manifest["definition_sha256"]
    assert manifest["input_sha256"]
    assert CONTROL_TOKEN not in (run_dir / "manifest.json").read_text(encoding="utf-8")
    assert (run_dir / "codex_jobs.sqlite").is_file()
    assert not (run_dir / "jobs.sqlite").exists()
    assert fake_bridge.state.post_count == 1
    assert fake_bridge.state.paths.count("/ui-api/control/threads") == 1
    assert all("/control/threads/" not in path for path in fake_bridge.state.paths)

    status = cli._job_status(
        SimpleNamespace(
            run_dir=run_dir, job_id=None, request_key=f"{result['workflow_id']}::do[0].calculate"
        )
    )
    events = cli._job_events(SimpleNamespace(run_dir=run_dir, after_id=0, limit=100))
    assert status["job"]["state"] == "SUCCEEDED"
    assert [event["state"] for event in events["events"]] == [
        "PREPARED",
        "MAYBE_SENT",
        "ACCEPTED",
        "SUCCEEDED",
    ]
    for artifact in run_dir.rglob("*"):
        if artifact.is_file():
            assert CONTROL_TOKEN.encode("ascii") not in artifact.read_bytes()

    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        workflow_count = connection.execute("SELECT COUNT(*) FROM workflow_status").fetchone()[0]
    resumed = cli._resume(SimpleNamespace(run_dir=run_dir, timeout_seconds=10))
    with sqlite3.connect(run_dir / "dbos.sqlite") as connection:
        recovered_workflow_count = connection.execute(
            "SELECT COUNT(*) FROM workflow_status"
        ).fetchone()[0]
    assert resumed["workflow_id"] == result["workflow_id"]
    assert recovered_workflow_count == workflow_count == 1
    assert fake_bridge.state.post_count == 1


@pytest.mark.parametrize(
    ("remote_state", "expected_ledger_state", "expected_error"),
    [
        ("in_progress", "RUNNING", "codex-job wait exceeded"),
        ("failed", "FAILED", "codex-job FAILED"),
    ],
)
def test_codex_job_timeout_and_failed_turn_are_distinct_and_not_cancelled(
    tmp_path: Path,
    fake_bridge,
    monkeypatch: pytest.MonkeyPatch,
    remote_state: str,
    expected_ledger_state: str,
    expected_error: str,
) -> None:
    import cli
    from codex_job_adapter import CodexJobAdapter

    fake_bridge.state.status = remote_state
    state_root = tmp_path / "phase4d-runs"
    monkeypatch.setattr(cli, "PHASE4D_STATE_ROOT", state_root)
    run_dir = cli._new_run(
        FIXTURE,
        "{}",
        1000,
        codex_job_live=True,
        codex_bridge_port=fake_bridge.server_address[1],
    )
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["control"].update({"codex_job_max_polls": 1, "codex_job_poll_interval_seconds": 0.05})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="DBOS worker exited"):
        cli._invoke_worker(run_dir, "start", 20)

    job = CodexJobAdapter(run_dir / "codex_jobs.sqlite").get_job_status(
        request_key=f"{manifest['workflow_id']}::do[0].calculate"
    )
    logs = json.loads((run_dir / "start-worker-logs.json").read_text(encoding="utf-8"))
    assert expected_error in logs["stderr"]
    assert job["state"] == expected_ledger_state
    assert job["status"] == expected_ledger_state
    assert job["thread_id"] == THREAD_ID
    assert job["turn_id"] == TURN_ID
    assert fake_bridge.state.post_count == 1
    assert all("interrupt" not in path for path in fake_bridge.state.paths)


def test_cli_parser_keeps_live_mode_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "run", "--workflow", str(FIXTURE), "--input-json", "{}"],
    )
    args = cli._parse_args()
    assert args.codex_job_live is False
