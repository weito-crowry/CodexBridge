from __future__ import annotations

import json
from typing import Any

from PySide6.QtWidgets import QApplication

from codex_bridge.console.api_client import ApiClient, ApiClientError, parse_json_reply


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
        self.metaDataChanged = Signal()
        self._body = body
        self._status = status
        self._error = error
        self.aborted = False
        self.deleted = False

    def readAll(self) -> bytes:
        body, self._body = self._body, b""
        return body

    def attribute(self, attribute: object) -> int:
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


def _application() -> QApplication:
    application = QApplication.instance()
    return application if application is not None else QApplication([])


def _activity() -> bytes:
    return (
        b"event: activity\n"
        b"data: "
        + json.dumps(
            {
                "activity_id": "activity-1",
                "timestamp": "2026-08-28T00:00:00Z",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "type": "error",
                "status": "failed",
                "summary": "safe",
                "details": {},
            }
        ).encode()
        + b"\n\n"
    )


def test_parse_json_reply_allows_2xx_and_rejects_raw_http_failures() -> None:
    assert parse_json_reply(200, b'{"ok":true}') == {"ok": True}

    for status, message in ((404, "Thread not found"), (500, "Request failed")):
        try:
            parse_json_reply(status, b"secret raw body")
        except ApiClientError as exc:
            assert str(exc) == message
        else:
            raise AssertionError("expected ApiClientError")


def test_api_client_uses_fixed_base_url_and_finished_json_callback() -> None:
    _application()
    reply = FakeReply(b'{"threads":[]}', status=200)
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    received: list[tuple[str, object]] = []
    client.json_succeeded.connect(lambda key, payload: received.append((key, payload)))

    assert client.get_json("/ui-api/threads", key="threads", query={"limit": 100})
    assert manager.requests[0].url().toString() == "http://127.0.0.1:8001/ui-api/threads?limit=100"
    reply.finished.emit()

    assert received == [("threads", {"threads": []})]
    assert reply.deleted


def test_api_client_suppresses_duplicate_key_and_maps_network_error() -> None:
    _application()
    first = FakeReply(b"{}")
    second = FakeReply(error=1)
    manager = FakeManager([first, second])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, str]] = []
    client.json_failed.connect(lambda key, message: failures.append((key, message)))

    assert client.get_json("/healthz", key="health")
    assert not client.get_json("/healthz", key="health")
    first.finished.emit()
    assert client.get_json("/healthz", key="health")
    second.finished.emit()

    assert failures == [("health", "Bridge unavailable")]


def test_api_client_streams_safe_activity_and_ignores_stale_reply() -> None:
    _application()
    old_reply = FakeReply()
    new_reply = FakeReply()
    manager = FakeManager([old_reply, new_reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    received: list[tuple[int, dict[str, object]]] = []
    client.activity_received.connect(
        lambda generation, activity: received.append((generation, activity))
    )

    client.start_stream("thread-a", 1)
    client.start_stream("thread-b", 2)
    old_reply._body = _activity()
    old_reply.readyRead.emit()
    new_reply._body = _activity().replace(b"thread-1", b"thread-2")
    new_reply.readyRead.emit()

    expected = json.loads(_activity().split(b"data: ", 1)[1].split(b"\n", 1)[0])
    expected["thread_id"] = "thread-2"
    assert received == [(2, expected)]
    assert old_reply.aborted
    assert old_reply.deleted
    assert manager.requests[1].url().toString().endswith("thread_id=thread-b")


def test_api_client_marks_stream_connected_after_response_headers() -> None:
    _application()
    reply = FakeReply()
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    states: list[str] = []
    client.stream_state_changed.connect(lambda generation, state: states.append(state))

    client.start_stream("thread", 1)
    reply.metaDataChanged.emit()

    assert states == ["reconnecting", "connected"]


def test_api_client_abort_all_aborts_json_and_stream_replies() -> None:
    _application()
    json_reply = FakeReply()
    stream_reply = FakeReply()
    manager = FakeManager([json_reply, stream_reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)

    client.get_json("/healthz", key="health")
    client.start_stream("thread", 1)
    client.abort_all()

    assert json_reply.aborted and json_reply.deleted
    assert stream_reply.aborted and stream_reply.deleted


def test_api_client_posts_authenticated_control_without_token_in_url_or_body() -> None:
    _application()
    token = "A" * 32
    reply = FakeReply(b'{"status":"shutdown_requested"}', status=202)
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    client.control_succeeded.connect(lambda key: successes.append(key))
    client.control_failed.connect(lambda key, message: failures.append((key, message)))

    assert client.post_control_shutdown(token, key="control:shutdown")
    request = manager.requests[0]
    assert request.url().toString() == "http://127.0.0.1:8001/ui-api/control/shutdown"
    assert bytes(request.rawHeader("Authorization")) == f"Bearer {token}".encode()
    assert bytes(request.rawHeader("Accept")) == b"application/json"
    assert manager.post_bodies == [b""]
    assert token not in request.url().toString()
    reply.finished.emit()

    assert successes == ["control:shutdown"]
    assert failures == []
    assert reply.deleted


def test_api_client_maps_control_network_and_http_failures_to_fixed_message() -> None:
    _application()
    network_reply = FakeReply(error=1)
    http_reply = FakeReply(b"server secret", status=403)
    manager = FakeManager([network_reply, http_reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)
    failures: list[tuple[str, str]] = []
    client.control_failed.connect(lambda key, message: failures.append((key, message)))

    assert client.post_control_shutdown("A" * 32, key="first")
    network_reply.finished.emit()
    assert client.post_control_shutdown("B" * 32, key="second")
    http_reply.finished.emit()

    assert failures == [
        ("first", "Bridge control request failed"),
        ("second", "Bridge control request failed"),
    ]
    assert b"server secret" not in str(failures).encode()


def test_api_client_abort_all_cleans_up_control_reply() -> None:
    _application()
    reply = FakeReply()
    manager = FakeManager([reply])
    client = ApiClient("http://127.0.0.1:8001", manager=manager)

    client.post_control_shutdown("A" * 32, key="control")
    client.abort_all()

    assert reply.aborted and reply.deleted
