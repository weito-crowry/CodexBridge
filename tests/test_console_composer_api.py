from __future__ import annotations

import json
from typing import Any

import pytest
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
        self._body = body
        self._status = status
        self._error = error
        self.deleted = False
        self.aborted = False

    def readAll(self) -> bytes:
        body, self._body = self._body, b""
        return body

    def attribute(self, _attribute: object) -> int:
        return self._status

    def error(self) -> int:
        return self._error

    def deleteLater(self) -> None:
        self.deleted = True

    def abort(self) -> None:
        self.aborted = True


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


def _application() -> QApplication:
    application = QApplication.instance()
    return application if application is not None else QApplication([])


def test_control_json_uses_bearer_auth_and_isolated_success_signal() -> None:
    _application()
    reply = FakeReply(b'{"models":[]}')
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    received: list[tuple[str, object]] = []
    client.composer_json_succeeded.connect(lambda key, body: received.append((key, body)))

    assert client.get_control_json(
        "/ui-api/control/capabilities", "control-secret", key="composer:capabilities"
    )
    request = manager.requests[0]
    assert bytes(request.rawHeader("Authorization")) == b"Bearer control-secret"
    reply.finished.emit()

    assert received == [("composer:capabilities", {"models": []})]
    assert reply.deleted


def test_control_json_posts_payload_and_suppresses_duplicate_dispatch() -> None:
    _application()
    reply = FakeReply(b'{"thread_id":"thread-1","turn_id":"turn-1","state":"in_progress"}')
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    payload = {"client_request_id": "request-1234", "prompt": " 日本語 "}

    assert client.post_control_json(
        "/ui-api/control/threads/thread-1/turns",
        payload,
        "control-secret",
        key="composer:send:request-1234",
    )
    assert not client.post_control_json(
        "/ui-api/control/threads/thread-1/turns",
        payload,
        "control-secret",
        key="composer:send:request-1234",
    )
    assert json.loads(manager.post_bodies[0]) == payload
    assert bytes(manager.requests[0].rawHeader("Authorization")) == b"Bearer control-secret"


@pytest.mark.parametrize(
    ("status", "network_error", "category"),
    [
        (400, 0, "http_400"),
        (403, 0, "http_403"),
        (403, 1, "http_403"),
        (409, 0, "http_409"),
        (502, 0, "http_502"),
        (503, 0, "http_503"),
        (0, 1, "network_error"),
    ],
)
def test_control_json_reports_safe_failure_categories(
    status: int, network_error: int, category: str
) -> None:
    _application()
    reply = FakeReply(b"sensitive raw response", status=status, error=network_error)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, str]] = []
    client.composer_json_failed.connect(lambda key, error: failures.append((key, error)))

    assert client.get_control_json(
        "/ui-api/control/capabilities", "control-secret", key="composer:capabilities"
    )
    reply.finished.emit()

    assert failures == [("composer:capabilities", category)]
    assert "sensitive" not in failures[0][1]


def test_control_json_extracts_only_allowed_composer_validation_categories() -> None:
    _application()
    reply = FakeReply(
        b'{"error":"invalid_attachment","category":"attachment_outside_allowed_root",'
        b'"message":"/private/path"}',
        status=400,
    )
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, str]] = []
    client.composer_json_failed.connect(lambda key, error: failures.append((key, error)))

    assert client.post_control_json(
        "/ui-api/control/threads",
        {"client_request_id": "request-1234", "cwd": "/private", "prompt": "go"},
        "control-secret",
        key="composer:send:request-1234",
    )
    reply.finished.emit()

    assert failures == [("composer:send:request-1234", "attachment_outside_allowed_root")]
    assert "/private" not in failures[0][1]


def test_control_json_ignores_unknown_validation_category() -> None:
    _application()
    reply = FakeReply(b'{"category":"raw_exception_text","message":"do not show me"}', status=400)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, str]] = []
    client.composer_json_failed.connect(lambda key, error: failures.append((key, error)))

    assert client.post_control_json(
        "/ui-api/control/threads",
        {"client_request_id": "request-1234", "cwd": "/private", "prompt": "go"},
        "control-secret",
        key="composer:send:request-1234",
    )
    reply.finished.emit()

    assert failures == [("composer:send:request-1234", "http_400")]
    assert "raw_exception_text" not in failures[0][1]


def test_control_json_ignores_non_string_validation_category() -> None:
    _application()
    reply = FakeReply(b'{"category":[]} ', status=400)
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, str]] = []
    client.composer_json_failed.connect(lambda key, error: failures.append((key, error)))

    assert client.post_control_json(
        "/ui-api/control/threads",
        {"client_request_id": "request-1234", "cwd": "/private", "prompt": "go"},
        "control-secret",
        key="composer:send:request-1234",
    )
    reply.finished.emit()

    assert failures == [("composer:send:request-1234", "http_400")]


def test_abort_all_cancels_composer_requests_without_crossing_control_signals() -> None:
    _application()
    reply = FakeReply(b'{"models":[]}')
    client = ApiClient("http://127.0.0.1:8001", manager=FakeManager([reply]))
    failures: list[tuple[str, str]] = []
    client.composer_json_failed.connect(lambda key, error: failures.append((key, error)))

    assert client.get_control_json(
        "/ui-api/control/capabilities", "control-secret", key="composer:capabilities"
    )
    client.abort_all()
    reply.finished.emit()

    assert reply.aborted
    assert reply.deleted
    assert failures == []
