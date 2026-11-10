from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

CONTROL_TOKEN = "phase5a-test-only-control-token"
EXPECTED_THREAD = {
    "thread_id": "thread_123456",
    "turn_id": "turn_123456",
    "state": "in_progress",
}


class _HTTPState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.capabilities_status = 200


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def state(self) -> _HTTPState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def _record(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length) if content_length else b""
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self) -> None:
        self._record()
        if self.path == "/healthz":
            self._reply(200, b'{"status":"ok"}')
        elif self.path == "/ui-api/status":
            self._reply(200, b'{"app_server":"ready"}')
        elif self.path == "/ui-api/control/capabilities":
            self._reply(self.state.capabilities_status, b'{"models":[]}')
        elif self.path == "/ui-api/control/threads":
            self._reply(201, json.dumps(EXPECTED_THREAD).encode("utf-8"))
        elif self.path == "/http-error":
            self._reply(403, b'{"detail":"forbidden"}')
        elif self.path == "/invalid-json":
            self._reply(200, b"{invalid json")
        elif self.path == "/oversized":
            self._reply(200, b"x" * 1024)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/should-not-follow")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._reply(404, b'{"detail":"not found"}')

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()


class _FakeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_BRIDGE_CONTROL_TOKEN", CONTROL_TOKEN)
    server = _FakeHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = _HTTPState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _http_client_module():
    try:
        from codex_bridge.workflows import http_client
    except ModuleNotFoundError as exc:
        pytest.fail(f"workflows HTTP client is not implemented: {exc.name}")
    return http_client


def _client(server: _FakeHTTPServer, **options: Any):
    module = _http_client_module()
    return module.CodexHTTPClient(port=server.server_address[1], **options)


def _payload(cwd: Path) -> dict[str, Any]:
    return {
        "client_request_id": "request-123456",
        "cwd": str(cwd.resolve()),
        "prompt": "Reply with 4.",
        "title": "Phase 5A fake request",
        "sandbox_mode": "inherit",
        "confirm_full_access": False,
    }


def test_workflows_http_client_is_importable() -> None:
    module = _http_client_module()

    assert module.CodexHTTPClient.__name__ == "CodexHTTPClient"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"host": "192.0.2.1"}, "loopback"),
        ({"host": "::1"}, "loopback"),
        ({"port": 0}, "port"),
        ({"port": 65536}, "port"),
        ({"port": True}, "port"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": float("inf")}, "timeout"),
        ({"timeout_seconds": True}, "timeout"),
        ({"max_response_bytes": 0}, "response limit"),
        ({"max_response_bytes": 1_048_577}, "response limit"),
        ({"max_response_bytes": True}, "response limit"),
    ],
)
def test_constructor_rejects_invalid_host_port_and_bounds(
    fake_api: _FakeHTTPServer, options: dict[str, Any], message: str
) -> None:
    module = _http_client_module()

    with pytest.raises(ValueError, match=message):
        module.CodexHTTPClient(**{"port": fake_api.server_address[1], **options})


def test_preflight_uses_get_only_and_authenticates_capabilities(fake_api: _FakeHTTPServer) -> None:
    _client(fake_api).preflight()

    requests = fake_api.state.requests
    assert [(entry["method"], entry["path"]) for entry in requests] == [
        ("GET", "/healthz"),
        ("GET", "/ui-api/status"),
        ("GET", "/ui-api/control/capabilities"),
    ]
    assert requests[-1]["headers"]["Authorization"] == f"Bearer {CONTROL_TOKEN}"
    assert all("Origin" not in entry["headers"] for entry in requests)
    assert all(entry["method"] != "POST" for entry in requests)


def test_preflight_403_is_reported_without_posting(fake_api: _FakeHTTPServer) -> None:
    module = _http_client_module()
    fake_api.state.capabilities_status = 403

    with pytest.raises(module.CodexPreflightError, match="preflight failed"):
        _client(fake_api).preflight()

    assert [entry["method"] for entry in fake_api.state.requests] == ["GET", "GET", "GET"]
    assert fake_api.state.requests[-1]["headers"]["Authorization"] == (f"Bearer {CONTROL_TOKEN}")


def test_preflight_requires_environment_token_before_request(
    fake_api: _FakeHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _http_client_module()
    monkeypatch.delenv("CODEX_BRIDGE_CONTROL_TOKEN")

    with pytest.raises(module.CodexPreflightError, match="not configured"):
        _client(fake_api).preflight()

    assert fake_api.state.requests == []


def test_create_thread_posts_the_fixed_payload_with_bearer(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    client = _client(fake_api)
    payload = _payload(tmp_path)

    result = client.create_thread(payload)

    assert result == EXPECTED_THREAD
    assert len(fake_api.state.requests) == 1
    request = fake_api.state.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/ui-api/control/threads"
    assert request["headers"]["Authorization"] == f"Bearer {CONTROL_TOKEN}"
    assert request["headers"]["Content-Type"] == "application/json"
    assert "Origin" not in request["headers"]
    decoded = json.loads(request["body"])
    assert set(decoded) == {
        "client_request_id",
        "cwd",
        "prompt",
        "title",
        "sandbox_mode",
        "confirm_full_access",
    }
    assert decoded == payload


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "missing",
        "bad_request_id",
        "relative_cwd",
        "empty_prompt",
        "empty_title",
        "dangerous_sandbox",
        "confirm_full_access",
    ],
)
def test_create_thread_rejects_payload_outside_fixed_shape(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    change: str,
) -> None:
    module = _http_client_module()
    payload = _payload(tmp_path)
    if change == "extra":
        payload["model"] = "unexpected"
    elif change == "missing":
        del payload["title"]
    elif change == "bad_request_id":
        payload["client_request_id"] = "short"
    elif change == "relative_cwd":
        payload["cwd"] = "relative/path"
    elif change == "empty_prompt":
        payload["prompt"] = ""
    elif change == "empty_title":
        payload["title"] = ""
    elif change == "dangerous_sandbox":
        payload["sandbox_mode"] = "danger-full-access"
    elif change == "confirm_full_access":
        payload["confirm_full_access"] = True

    with pytest.raises(module.CodexProtocolError):
        _client(fake_api).create_thread(payload)

    assert fake_api.state.requests == []


def test_create_thread_rejects_request_over_size_limit(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    module = _http_client_module()
    payload = _payload(tmp_path)
    payload["prompt"] = "x" * 1024

    with pytest.raises(module.CodexProtocolError, match="size limit"):
        _client(fake_api, max_response_bytes=128).create_thread(payload)

    assert fake_api.state.requests == []


@pytest.mark.parametrize(
    ("path", "error_type", "message"),
    [
        ("/http-error", "CodexHTTPError", "HTTP 403"),
        ("/invalid-json", "CodexProtocolError", "valid JSON"),
        ("/oversized", "CodexProtocolError", "response limit"),
    ],
)
def test_http_errors_invalid_json_and_oversized_responses_are_bounded(
    fake_api: _FakeHTTPServer,
    path: str,
    error_type: str,
    message: str,
) -> None:
    module = _http_client_module()
    error = getattr(module, error_type)

    with pytest.raises(error, match=message):
        _client(fake_api, max_response_bytes=128)._request_json("GET", path)

    assert [entry["path"] for entry in fake_api.state.requests] == [path]


def test_redirect_is_an_http_error_and_is_not_followed(fake_api: _FakeHTTPServer) -> None:
    module = _http_client_module()

    with pytest.raises(module.CodexHTTPError, match="HTTP 302"):
        _client(fake_api)._request_json("GET", "/redirect")

    assert [entry["path"] for entry in fake_api.state.requests] == ["/redirect"]
