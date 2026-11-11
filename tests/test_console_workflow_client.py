from __future__ import annotations

import json
from typing import Any

import pytest
from PySide6.QtNetwork import QNetworkReply
from PySide6.QtWidgets import QApplication

from codex_bridge.console.api_client import ApiClient


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeReply:
    def __init__(self, body: bytes = b"", status: int = 200, error: int = 0) -> None:
        self.finished = Signal()
        self.readyRead = Signal()
        self._body = body
        self._status = status
        self._error = error
        self.aborted = False
        self.deleted = False

    def read(self, size: int) -> bytes:
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def readAll(self) -> bytes:
        return self.read(len(self._body))

    def attribute(self, _attribute: object) -> int:
        return self._status

    def error(self) -> int:
        return self._error

    def abort(self) -> None:
        self.aborted = True

    def deleteLater(self) -> None:
        self.deleted = True


class FakeManager:
    def __init__(self, replies: list[FakeReply]) -> None:
        self.replies = replies
        self.requests: list[Any] = []
        self.post_bodies: list[bytes] = []

    def get(self, request: Any) -> FakeReply:
        self.requests.append(request)
        return self.replies.pop(0)

    def post(self, request: Any, body: bytes) -> FakeReply:
        self.requests.append(request)
        self.post_bodies.append(body)
        return self.replies.pop(0)


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, b'{"error":"forbidden"}', "forbidden"),
        (404, b'{"error":"workflow_not_found"}', "workflow_not_found"),
        (409, b'{"error":"workflow_needs_reconciliation"}', "workflow_needs_reconciliation"),
        (503, b'{"error":"workflow_service_unavailable"}', "workflow_service_unavailable"),
        (500, b"raw secret response", "http_error"),
    ],
)
def test_workflow_client_reports_http_status_and_safe_error_code(
    status: int, body: bytes, expected: str
) -> None:
    _app()
    reply = FakeReply(body, status)
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))

    assert client.get_control_workflows("safe-token", key="list")
    reply.finished.emit()

    assert failures == [("list", status, expected)]
    assert reply.deleted
    assert "safe-token" not in repr(failures)
    assert b"raw secret" not in repr(failures).encode()


def test_workflow_client_authenticates_and_accepts_only_expected_success_status() -> None:
    _app()
    reply = FakeReply(b'{"workflows":[],"limit":50,"truncated":false}', 200)
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    successes: list[tuple[str, int, object]] = []
    client.workflow_succeeded.connect(lambda *args: successes.append(args))

    assert client.get_control_workflows("safe-token", key="list")
    request = manager.requests[0]
    assert request.url().toString() == ("http://127.0.0.1:8001/ui-api/control/workflows?limit=50")
    assert bytes(request.rawHeader("Authorization")) == b"Bearer safe-token"
    assert bytes(request.rawHeader("Accept")) == b"application/json"
    assert reply.error() == QNetworkReply.NetworkError.NoError.value
    reply.finished.emit()

    assert successes == [("list", 200, {"workflows": [], "limit": 50, "truncated": False})]


def test_workflow_client_posts_json_once_and_returns_202_payload() -> None:
    _app()
    payload = {
        "client_request_id": "request-1",
        "workflow_yaml": "name: test\n",
        "input": {},
        "workspace_root": "C:/workspace",
    }
    reply = FakeReply(b'{"workflow_id":"workflow-1","status":"queued"}', 202)
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    successes: list[tuple[str, int, object]] = []
    client.workflow_succeeded.connect(lambda *args: successes.append(args))

    assert client.post_control_workflow("safe-token", payload, key="start-1")
    request = manager.requests[0]
    assert request.url().toString() == "http://127.0.0.1:8001/ui-api/control/workflows"
    assert bytes(request.rawHeader("Authorization")) == b"Bearer safe-token"
    assert bytes(request.rawHeader("Content-Type")) == b"application/json"
    assert json.loads(manager.post_bodies[0]) == payload
    assert b"safe-token" not in manager.post_bodies[0]
    reply.finished.emit()

    assert successes == [("start-1", 202, {"workflow_id": "workflow-1", "status": "queued"})]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (QNetworkReply.NetworkError.TimeoutError.value, "timeout"),
        (QNetworkReply.NetworkError.ConnectionRefusedError.value, "network_error"),
    ],
)
def test_workflow_client_classifies_timeout_and_connection_errors(
    error: int, expected: str
) -> None:
    _app()
    reply = FakeReply(error=error)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))

    assert client.get_control_workflows("safe-token", key="list")
    reply.finished.emit()

    assert failures == [("list", 200, expected)]


def test_workflow_client_rejects_invalid_json_and_oversized_body() -> None:
    _app()
    invalid = FakeReply(b"not-json", 200)
    oversized = FakeReply(b" " * (64 * 1024 + 1), 200)
    manager = FakeManager([invalid, oversized])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))

    assert client.get_control_workflows("safe-token", key="invalid")
    invalid.finished.emit()
    assert client.get_control_workflows("safe-token", key="large")
    oversized.readyRead.emit()

    assert failures == [("invalid", 200, "invalid_json"), ("large", 200, "response_too_large")]
    assert oversized.aborted and oversized.deleted


def test_workflow_client_aborts_old_selection_request_without_emitting_stale_reply() -> None:
    _app()
    pending = FakeReply(b'{"status":"running"}', 202)
    later = FakeReply(b'{"workflow_id":"next","status":"running"}', 200)
    manager = FakeManager([pending, later])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    successes: list[tuple[str, int, object]] = []
    client.workflow_succeeded.connect(lambda *args: successes.append(args))

    assert client.get_control_workflow_result("one", "safe-token", key="result:old")
    client.abort_workflow_request("result:old")
    assert pending.aborted and pending.deleted
    assert client.get_control_workflow_detail("next", "safe-token", key="detail:new")
    pending.finished.emit()
    later.finished.emit()

    assert successes == [("detail:new", 200, {"workflow_id": "next", "status": "running"})]


def test_workflow_client_accepts_202_for_result_polling() -> None:
    _app()
    reply = FakeReply(b'{"workflow_id":"workflow-one","status":"running"}', 202)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    successes: list[tuple[str, int, object]] = []
    client.workflow_succeeded.connect(lambda *args: successes.append(args))

    assert client.get_control_workflow_result("workflow-one", "safe-token", key="result")
    reply.finished.emit()

    assert successes == [("result", 202, {"workflow_id": "workflow-one", "status": "running"})]


@pytest.mark.parametrize(
    ("kind", "limit"),
    [("detail", 4 * 1024), ("result", 64 * 1024)],
)
def test_workflow_client_applies_detail_and_result_response_limits(kind: str, limit: int) -> None:
    _app()
    reply = FakeReply(b"x" * (limit + 1), 200)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))

    if kind == "detail":
        assert client.get_control_workflow_detail("workflow-one", "safe-token", key=kind)
    else:
        assert client.get_control_workflow_result("workflow-one", "safe-token", key=kind)
    reply.readyRead.emit()

    assert failures == [(kind, 200, "response_too_large")]
    assert reply.aborted and reply.deleted


def test_workflow_client_suppresses_duplicate_key_and_rejects_path_ids() -> None:
    _app()
    reply = FakeReply()
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))

    assert client.get_control_workflows("safe-token", key="list")
    assert not client.get_control_workflows("safe-token", key="list")
    assert not client.get_control_workflow_detail("../secret", "safe-token", key="unsafe")
    assert len(manager.requests) == 1
    assert failures == [("unsafe", 0, "invalid_request")]


def test_workflow_client_rejects_oversized_request_body_without_network_call() -> None:
    _app()
    manager = FakeManager([])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, int, str]] = []
    client.workflow_failed.connect(lambda *args: failures.append(args))
    payload = {
        "client_request_id": "request-1",
        "workflow_yaml": "x" * (128 * 1024),
        "workspace_root": "C:/workspace",
    }

    assert not client.post_control_workflow("safe-token", payload, key="start")

    assert failures == [("start", 0, "request_too_large")]
    assert manager.requests == []
