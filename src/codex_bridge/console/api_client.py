from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .sse import SseParser, SseProtocolError, parse_activity_event

_MAX_CONTROL_RESPONSE_BYTES = 4 * 1024


class ApiClientError(RuntimeError):
    """A bounded user-safe API client error."""


class NetworkManagerLike(Protocol):
    def get(self, request: QNetworkRequest) -> Any: ...

    def post(self, request: QNetworkRequest, body: bytes) -> Any: ...


def parse_json_reply(status_code: int, body: bytes) -> object:
    if status_code == 404:
        raise ApiClientError("Thread not found")
    if not 200 <= status_code <= 299:
        raise ApiClientError("Request failed")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ApiClientError("Invalid response") from exc


class ApiClient(QObject):
    """Non-blocking GET client for the fixed-loopback UI API."""

    json_succeeded = Signal(str, object)
    json_failed = Signal(str, str)
    activity_received = Signal(int, object)
    stream_state_changed = Signal(int, str)
    control_succeeded = Signal(str)
    control_failed = Signal(str, str)

    def __init__(
        self,
        base_url: str,
        parent: QObject | None = None,
        *,
        manager: NetworkManagerLike | None = None,
    ) -> None:
        super().__init__(parent)
        if not base_url.startswith("http://127.0.0.1:"):
            raise ValueError("console API base URL must use fixed loopback")
        self._base_url = base_url.rstrip("/")
        self._manager: NetworkManagerLike = manager or QNetworkAccessManager(self)
        self._json_replies: dict[str, Any] = {}
        self._control_replies: dict[str, Any] = {}
        self._control_response_sizes: dict[str, int] = {}
        self._stream_reply: Any | None = None
        self._stream_generation: int | None = None
        self._stream_parser: SseParser | None = None
        self._stream_connected = False

    def _url(self, path: str, query: Mapping[str, object] | None = None) -> QUrl:
        if not path.startswith("/") or "://" in path:
            raise ValueError("API path must be relative")
        url = QUrl(f"{self._base_url}{path}")
        if query:
            query_items = QUrlQuery()
            for key, value in query.items():
                if value is not None:
                    query_items.addQueryItem(str(key), str(value))
            url.setQuery(query_items)
        return url

    @staticmethod
    def _request(url: QUrl) -> QNetworkRequest:
        request = QNetworkRequest(url)
        request.setRawHeader(b"Accept", b"application/json")
        return request

    @staticmethod
    def _status(reply: Any) -> int:
        value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        return int(value) if isinstance(value, int) else 0

    @staticmethod
    def _has_network_error(reply: Any) -> bool:
        try:
            error = reply.error()
            error_value = getattr(error, "value", error)
            return bool(error_value != QNetworkReply.NetworkError.NoError.value)
        except (AttributeError, TypeError):
            return False

    def get_json(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, object] | None = None,
    ) -> bool:
        if key in self._json_replies:
            return False
        reply = self._manager.get(self._request(self._url(path, query)))
        self._json_replies[key] = reply
        reply.finished.connect(lambda key=key, reply=reply: self._finish_json(key, reply))
        return True

    def _finish_json(self, key: str, reply: Any) -> None:
        active_reply = self._json_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return
        self._json_replies.pop(key, None)
        try:
            if self._has_network_error(reply):
                raise ApiClientError("Bridge unavailable")
            payload = parse_json_reply(self._status(reply), bytes(reply.readAll()))
        except ApiClientError as exc:
            self.json_failed.emit(key, str(exc))
        else:
            self.json_succeeded.emit(key, payload)
        finally:
            reply.deleteLater()

    def post_control_shutdown(self, token: str, *, key: str) -> bool:
        if key in self._control_replies:
            return False
        try:
            request = self._request(self._url("/ui-api/control/shutdown"))
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
            reply = self._manager.post(request, b"")
        except (UnicodeEncodeError, TypeError, ValueError):
            self.control_failed.emit(key, "Bridge control request failed")
            return False
        self._control_replies[key] = reply
        self._control_response_sizes[key] = 0
        reply.readyRead.connect(lambda key=key, reply=reply: self._consume_control_data(key, reply))
        reply.finished.connect(lambda key=key, reply=reply: self._finish_control(key, reply))
        return True

    def _fail_control_reply(self, key: str, reply: Any) -> None:
        active_reply = self._control_replies.get(key)
        if active_reply is not reply:
            return
        self._control_replies.pop(key, None)
        self._control_response_sizes.pop(key, None)
        active_reply.abort()
        active_reply.deleteLater()
        self.control_failed.emit(key, "Bridge control request failed")

    def _consume_control_data(self, key: str, reply: Any) -> bool:
        active_reply = self._control_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return False
        try:
            chunk = bytes(active_reply.read(_MAX_CONTROL_RESPONSE_BYTES + 1))
        except (AttributeError, TypeError, ValueError):
            self._fail_control_reply(key, reply)
            return False
        size = self._control_response_sizes.get(key, 0) + len(chunk)
        self._control_response_sizes[key] = size
        if size > _MAX_CONTROL_RESPONSE_BYTES:
            self._fail_control_reply(key, reply)
            return False
        return True

    def _finish_control(self, key: str, reply: Any) -> None:
        active_reply = self._control_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return
        if not self._consume_control_data(key, reply):
            return
        self._control_replies.pop(key, None)
        self._control_response_sizes.pop(key, None)
        try:
            status = self._status(reply)
            if self._has_network_error(reply) or status != 202:
                raise ApiClientError("Bridge control request failed")
            self.control_succeeded.emit(key)
        except ApiClientError as exc:
            self.control_failed.emit(key, str(exc))
        finally:
            reply.deleteLater()

    def start_stream(self, thread_id: str, generation: int) -> None:
        self.stop_stream()
        self._stream_generation = generation
        self._stream_parser = SseParser()
        self._stream_connected = False
        reply = self._manager.get(
            self._request(self._url("/ui-api/events", {"thread_id": thread_id}))
        )
        self._stream_reply = reply
        meta_data_changed = getattr(reply, "metaDataChanged", None)
        if meta_data_changed is not None:
            meta_data_changed.connect(
                lambda generation=generation, reply=reply: self._mark_stream_connected(
                    generation, reply
                )
            )
        reply.readyRead.connect(
            lambda generation=generation, reply=reply: self._consume_stream(generation, reply)
        )
        reply.finished.connect(
            lambda generation=generation, reply=reply: self._finish_stream(generation, reply)
        )
        self.stream_state_changed.emit(generation, "reconnecting")

    def _mark_stream_connected(self, generation: int, reply: Any) -> None:
        if self._active_stream(generation, reply) and not self._stream_connected:
            self._stream_connected = True
            self.stream_state_changed.emit(generation, "connected")

    def _active_stream(self, generation: int, reply: Any) -> bool:
        return self._stream_generation == generation and self._stream_reply is reply

    def _consume_stream(self, generation: int, reply: Any) -> None:
        if not self._active_stream(generation, reply) or self._stream_parser is None:
            return
        try:
            events = self._stream_parser.feed(bytes(reply.readAll()))
        except SseProtocolError:
            self._close_stream(generation, reply)
            return
        self._mark_stream_connected(generation, reply)
        for event in events:
            activity = parse_activity_event(event)
            if activity is not None and self._active_stream(generation, reply):
                self.activity_received.emit(generation, activity)

    def _finish_stream(self, generation: int, reply: Any) -> None:
        if not self._active_stream(generation, reply):
            return
        parser = self._stream_parser
        if not self._has_network_error(reply) and self._status(reply) in range(200, 300):
            try:
                events = parser.finish() if parser is not None else ()
            except SseProtocolError:
                events = ()
            for event in events:
                activity = parse_activity_event(event)
                if activity is not None:
                    self.activity_received.emit(generation, activity)
        self._stream_reply = None
        self._stream_generation = None
        self._stream_parser = None
        self._stream_connected = False
        reply.deleteLater()
        self.stream_state_changed.emit(generation, "disconnected")

    def _close_stream(self, generation: int, reply: Any) -> None:
        if not self._active_stream(generation, reply):
            return
        self._stream_reply = None
        self._stream_generation = None
        self._stream_parser = None
        self._stream_connected = False
        reply.abort()
        reply.deleteLater()
        self.stream_state_changed.emit(generation, "disconnected")

    def abort_json_group(self, prefix: str) -> None:
        for key, reply in tuple(self._json_replies.items()):
            if key.startswith(prefix):
                self._json_replies.pop(key, None)
                reply.abort()
                reply.deleteLater()

    def stop_stream(self) -> None:
        if self._stream_reply is None:
            return
        reply, generation = self._stream_reply, self._stream_generation
        self._stream_reply = None
        self._stream_generation = None
        self._stream_parser = None
        self._stream_connected = False
        reply.abort()
        reply.deleteLater()
        if generation is not None:
            self.stream_state_changed.emit(generation, "disconnected")

    def abort_all(self) -> None:
        for reply in tuple(self._json_replies.values()):
            reply.abort()
            reply.deleteLater()
        self._json_replies.clear()
        for reply in tuple(self._control_replies.values()):
            reply.abort()
            reply.deleteLater()
        self._control_replies.clear()
        self._control_response_sizes.clear()
        self.stop_stream()
