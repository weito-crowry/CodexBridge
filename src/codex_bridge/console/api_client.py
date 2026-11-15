from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol, cast
from urllib.parse import quote

from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from ..models import ApprovalDecision, RequestId
from .sse import SseParser, SseProtocolError, parse_activity_event

_MAX_CONTROL_RESPONSE_BYTES = 4 * 1024
_MAX_WORKFLOW_LIST_RESPONSE_BYTES = 64 * 1024
_MAX_WORKFLOW_DETAIL_RESPONSE_BYTES = 4 * 1024
_MAX_WORKFLOW_RESULT_RESPONSE_BYTES = 64 * 1024
_MAX_WORKFLOW_REQUEST_BYTES = 128 * 1024
_WORKFLOW_GET_TRANSFER_TIMEOUT_MS = 10_000
_WORKFLOW_POST_TRANSFER_TIMEOUT_MS = 25_000
_WORKFLOW_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})
_COMPOSER_VALIDATION_CATEGORIES = frozenset(
    {
        "malformed_attachment_list",
        "too_many_attachments",
        "attachment_not_absolute",
        "attachment_missing_or_unavailable",
        "attachment_not_regular_file",
        "attachment_outside_allowed_root",
        "duplicate_attachment",
        "attachment_too_large",
        "invalid_image_signature",
        "total_attachment_size_exceeded",
        "cwd_not_absolute",
        "cwd_parent_traversal",
        "cwd_missing_or_unavailable",
        "cwd_not_directory",
        "cwd_outside_allowed_root",
    }
)


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
    global_activity_received = Signal(int, object)
    global_stream_state_changed = Signal(int, str)
    control_succeeded = Signal(str)
    control_failed = Signal(str, str)
    composer_json_succeeded = Signal(str, object)
    composer_json_failed = Signal(str, str)
    workflow_succeeded = Signal(str, int, object)
    workflow_failed = Signal(str, int, str)

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
        self._control_response_bodies: dict[str, bytearray] = {}
        self._control_kinds: dict[str, str] = {}
        self._composer_replies: dict[str, Any] = {}
        self._workflow_replies: dict[str, Any] = {}
        self._workflow_response_sizes: dict[str, int] = {}
        self._workflow_response_bodies: dict[str, bytearray] = {}
        self._workflow_response_limits: dict[str, int] = {}
        self._workflow_success_statuses: dict[str, frozenset[int]] = {}
        self._stream_reply: Any | None = None
        self._stream_generation: int | None = None
        self._stream_parser: SseParser | None = None
        self._stream_connected = False
        self._global_stream_reply: Any | None = None
        self._global_stream_generation: int | None = None
        self._global_stream_parser: SseParser | None = None
        self._global_stream_connected = False

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

    def post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        key: str,
    ) -> bool:
        if key in self._json_replies:
            return False
        try:
            request = self._request(self._url(path))
            request.setRawHeader(b"Content-Type", b"application/json")
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            reply = self._manager.post(request, body)
        except (UnicodeEncodeError, TypeError, ValueError):
            self.json_failed.emit(key, "Request failed")
            return False
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

    def get_control_json(self, path: str, token: str, *, key: str) -> bool:
        if key in self._composer_replies:
            return False
        try:
            request = self._request(self._control_url(path))
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
            reply = self._manager.get(request)
        except (UnicodeEncodeError, TypeError, ValueError):
            self.composer_json_failed.emit(key, "request_error")
            return False
        self._composer_replies[key] = reply
        reply.finished.connect(lambda key=key, reply=reply: self._finish_composer_json(key, reply))
        return True

    def post_control_json(
        self,
        path: str,
        payload: Mapping[str, object],
        token: str,
        *,
        key: str,
    ) -> bool:
        if key in self._composer_replies:
            return False
        try:
            request = self._request(self._control_url(path))
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
            request.setRawHeader(b"Content-Type", b"application/json")
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            reply = self._manager.post(request, body)
        except (UnicodeEncodeError, TypeError, ValueError):
            self.composer_json_failed.emit(key, "request_error")
            return False
        self._composer_replies[key] = reply
        reply.finished.connect(lambda key=key, reply=reply: self._finish_composer_json(key, reply))
        return True

    def _control_url(self, path: str) -> QUrl:
        if not path.startswith("/ui-api/control/"):
            raise ValueError("control API path is required")
        return self._url(path)

    def get_control_workflows(self, token: str, *, key: str) -> bool:
        return self._start_workflow_request(
            "GET",
            self._url("/ui-api/control/workflows", {"limit": 50}),
            token,
            key=key,
            response_limit=_MAX_WORKFLOW_LIST_RESPONSE_BYTES,
            success_statuses=frozenset({200}),
        )

    def get_control_workflow_detail(self, workflow_id: str, token: str, *, key: str) -> bool:
        safe_id = self._workflow_path_id(workflow_id)
        if safe_id is None:
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        return self._start_workflow_request(
            "GET",
            self._url(f"/ui-api/control/workflows/{safe_id}"),
            token,
            key=key,
            response_limit=_MAX_WORKFLOW_DETAIL_RESPONSE_BYTES,
            success_statuses=frozenset({200}),
        )

    def get_control_workflow_result(self, workflow_id: str, token: str, *, key: str) -> bool:
        safe_id = self._workflow_path_id(workflow_id)
        if safe_id is None:
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        return self._start_workflow_request(
            "GET",
            self._url(f"/ui-api/control/workflows/{safe_id}/result"),
            token,
            key=key,
            response_limit=_MAX_WORKFLOW_RESULT_RESPONSE_BYTES,
            success_statuses=frozenset({200, 202}),
        )

    @staticmethod
    def _workflow_path_id(workflow_id: object) -> str | None:
        if (
            not isinstance(workflow_id, str)
            or not workflow_id
            or len(workflow_id) > 256
            or workflow_id in {".", ".."}
            or any(character in workflow_id for character in "/\\?#")
        ):
            return None
        return quote(workflow_id, safe="")

    def post_control_workflow(
        self,
        token: str,
        payload: Mapping[str, object],
        *,
        key: str,
    ) -> bool:
        if key in self._workflow_replies:
            return False
        if not isinstance(payload, Mapping):
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (UnicodeEncodeError, TypeError, ValueError):
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        if len(body) > _MAX_WORKFLOW_REQUEST_BYTES:
            self.workflow_failed.emit(key, 0, "request_too_large")
            return False
        request_url = self._url("/ui-api/control/workflows")
        request = self._request(request_url)
        try:
            self._add_workflow_headers(
                request,
                token,
                json_body=True,
                transfer_timeout_ms=_WORKFLOW_POST_TRANSFER_TIMEOUT_MS,
            )
            reply = self._manager.post(request, body)
        except (UnicodeEncodeError, TypeError, ValueError):
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        return self._track_workflow_reply(
            reply,
            key=key,
            response_limit=_MAX_WORKFLOW_DETAIL_RESPONSE_BYTES,
            success_statuses=frozenset({202}),
        )

    @staticmethod
    def _add_workflow_headers(
        request: QNetworkRequest,
        token: str,
        *,
        json_body: bool,
        transfer_timeout_ms: int,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("control token is unavailable")
        request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
        if json_body:
            request.setRawHeader(b"Content-Type", b"application/json")
        set_transfer_timeout = getattr(request, "setTransferTimeout", None)
        if callable(set_transfer_timeout):
            set_transfer_timeout(transfer_timeout_ms)

    def _start_workflow_request(
        self,
        method: str,
        url: QUrl,
        token: str,
        *,
        key: str,
        response_limit: int,
        success_statuses: frozenset[int],
    ) -> bool:
        if key in self._workflow_replies:
            return False
        request = self._request(url)
        try:
            self._add_workflow_headers(
                request,
                token,
                json_body=False,
                transfer_timeout_ms=_WORKFLOW_GET_TRANSFER_TIMEOUT_MS,
            )
            reply = (
                self._manager.get(request) if method == "GET" else self._manager.post(request, b"")
            )
        except (UnicodeEncodeError, TypeError, ValueError):
            self.workflow_failed.emit(key, 0, "invalid_request")
            return False
        return self._track_workflow_reply(
            reply,
            key=key,
            response_limit=response_limit,
            success_statuses=success_statuses,
        )

    def _track_workflow_reply(
        self,
        reply: Any,
        *,
        key: str,
        response_limit: int,
        success_statuses: frozenset[int],
    ) -> bool:
        if key in self._workflow_replies:
            reply.abort()
            reply.deleteLater()
            return False
        self._workflow_replies[key] = reply
        self._workflow_response_sizes[key] = 0
        self._workflow_response_bodies[key] = bytearray()
        self._workflow_response_limits[key] = response_limit
        self._workflow_success_statuses[key] = success_statuses
        reply.readyRead.connect(
            lambda key=key, reply=reply: self._consume_workflow_data(key, reply)
        )
        reply.finished.connect(lambda key=key, reply=reply: self._finish_workflow_json(key, reply))
        return True

    def _fail_workflow_reply(self, key: str, reply: Any, status: int, code: str) -> None:
        active_reply = self._workflow_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return
        self._drop_workflow_reply(key)
        active_reply.abort()
        active_reply.deleteLater()
        self.workflow_failed.emit(key, status, code)

    def _drop_workflow_reply(self, key: str) -> tuple[Any | None, bytes]:
        reply = self._workflow_replies.pop(key, None)
        self._workflow_response_sizes.pop(key, None)
        body = bytes(self._workflow_response_bodies.pop(key, bytearray()))
        self._workflow_response_limits.pop(key, None)
        self._workflow_success_statuses.pop(key, None)
        return reply, body

    def _consume_workflow_data(self, key: str, reply: Any) -> bool:
        active_reply = self._workflow_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return False
        status = self._status(active_reply)
        limit = self._workflow_response_limits[key]
        remaining = max(1, limit - self._workflow_response_sizes[key] + 1)
        try:
            chunk = bytes(active_reply.read(remaining))
        except (AttributeError, TypeError, ValueError):
            self._fail_workflow_reply(key, active_reply, status, "invalid_response")
            return False
        size = self._workflow_response_sizes[key] + len(chunk)
        if size > limit:
            self._fail_workflow_reply(key, active_reply, status, "response_too_large")
            return False
        self._workflow_response_sizes[key] = size
        self._workflow_response_bodies[key].extend(chunk)
        return True

    @staticmethod
    def _workflow_error_code(body: bytes) -> str:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return "http_error"
        code = payload.get("error") if isinstance(payload, dict) else None
        return (
            code if isinstance(code, str) and _WORKFLOW_ERROR_CODE.fullmatch(code) else "http_error"
        )

    @staticmethod
    def _workflow_network_error(reply: Any) -> str | None:
        try:
            error = reply.error()
            error_value = getattr(error, "value", error)
            if error_value == QNetworkReply.NetworkError.NoError.value:
                return None
            if error_value == QNetworkReply.NetworkError.TimeoutError.value:
                return "timeout"
            return "network_error"
        except (AttributeError, TypeError):
            return None

    def _finish_workflow_json(self, key: str, reply: Any) -> None:
        active_reply = self._workflow_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return
        if not self._consume_workflow_data(key, active_reply):
            return
        success_statuses = self._workflow_success_statuses.get(key, frozenset())
        _, body = self._drop_workflow_reply(key)
        status = self._status(active_reply)
        try:
            network_error = self._workflow_network_error(active_reply)
            if network_error is not None or status == 0:
                raise ApiClientError(network_error or "network_error")
            if status not in success_statuses:
                raise ApiClientError(self._workflow_error_code(body))
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                raise ApiClientError("invalid_json") from exc
            if not isinstance(payload, dict):
                raise ApiClientError("invalid_json")
        except ApiClientError as exc:
            self.workflow_failed.emit(key, status, str(exc))
        else:
            self.workflow_succeeded.emit(key, status, payload)
        finally:
            active_reply.deleteLater()

    def abort_workflow_request(self, key: str) -> None:
        reply, _body = self._drop_workflow_reply(key)
        if reply is not None:
            reply.abort()
            reply.deleteLater()

    def abort_workflow_requests(self) -> None:
        for key in tuple(self._workflow_replies):
            self.abort_workflow_request(key)

    def _finish_composer_json(self, key: str, reply: Any) -> None:
        if self._composer_replies.get(key) is not reply:
            return
        self._composer_replies.pop(key, None)
        network_reply = cast(QNetworkReply, reply)
        try:
            status = self._status(network_reply)
            if status and not 200 <= status <= 299:
                body = bytes(cast(Any, network_reply.readAll()))
                if len(body) <= _MAX_CONTROL_RESPONSE_BYTES:
                    try:
                        payload = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError, TypeError):
                        payload = None
                    validation_category = (
                        payload.get("category") if isinstance(payload, dict) else None
                    )
                    if (
                        isinstance(validation_category, str)
                        and validation_category in _COMPOSER_VALIDATION_CATEGORIES
                    ):
                        raise ApiClientError(validation_category)
                category = f"http_{status}" if status in {400, 403, 409, 502, 503} else "http_error"
                raise ApiClientError(category)
            if self._has_network_error(network_reply) or status == 0:
                raise ApiClientError("network_error")
            try:
                body = bytes(cast(Any, network_reply.readAll()))
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, TypeError) as exc:
                raise ApiClientError("invalid_response") from exc
        except ApiClientError as exc:
            self.composer_json_failed.emit(key, str(exc))
        else:
            self.composer_json_succeeded.emit(key, payload)
        finally:
            network_reply.deleteLater()

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
        self._control_response_bodies[key] = bytearray()
        self._control_kinds[key] = "shutdown"
        reply.readyRead.connect(lambda key=key, reply=reply: self._consume_control_data(key, reply))
        reply.finished.connect(lambda key=key, reply=reply: self._finish_control(key, reply))
        return True

    def post_control_approval(
        self,
        token: str,
        *,
        request_id: RequestId,
        decision: ApprovalDecision,
        key: str,
    ) -> bool:
        if key in self._control_replies:
            return False
        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
            or not isinstance(decision, str)
            or decision not in _APPROVAL_DECISIONS
        ):
            self.control_failed.emit(key, "Bridge control request failed")
            return False
        try:
            request = self._request(self._url("/ui-api/control/approval"))
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
            request.setRawHeader(b"Content-Type", b"application/json")
            body = json.dumps(
                {"request_id": request_id, "decision": decision}, separators=(",", ":")
            ).encode("utf-8")
            reply = self._manager.post(request, body)
        except (UnicodeEncodeError, TypeError, ValueError):
            self.control_failed.emit(key, "Bridge control request failed")
            return False
        self._control_replies[key] = reply
        self._control_response_sizes[key] = 0
        self._control_response_bodies[key] = bytearray()
        self._control_kinds[key] = "approval"
        reply.readyRead.connect(lambda key=key, reply=reply: self._consume_control_data(key, reply))
        reply.finished.connect(lambda key=key, reply=reply: self._finish_control(key, reply))
        return True

    def _fail_control_reply(self, key: str, reply: Any) -> None:
        active_reply = self._control_replies.get(key)
        if active_reply is not reply:
            return
        self._control_replies.pop(key, None)
        self._control_response_sizes.pop(key, None)
        self._control_response_bodies.pop(key, None)
        self._control_kinds.pop(key, None)
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
        self._control_response_bodies.setdefault(key, bytearray()).extend(chunk)
        return True

    def _finish_control(self, key: str, reply: Any) -> None:
        active_reply = self._control_replies.get(key)
        if active_reply is None or active_reply is not reply:
            return
        if not self._consume_control_data(key, reply):
            return
        self._control_replies.pop(key, None)
        self._control_response_sizes.pop(key, None)
        body = bytes(self._control_response_bodies.pop(key, bytearray()))
        kind = self._control_kinds.pop(key, "shutdown")
        try:
            status = self._status(reply)
            if self._has_network_error(reply):
                raise ApiClientError("Bridge control request failed")
            if kind == "approval":
                if status == 409:
                    raise ApiClientError("Approval already resolved")
                if status != 200:
                    raise ApiClientError("Bridge control request failed")
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, TypeError) as exc:
                    raise ApiClientError("Invalid approval response") from exc
                if not isinstance(payload, dict) or payload.get("status") != "approval_resolved":
                    raise ApiClientError("Invalid approval response")
            elif status != 202:
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

    def start_global_stream(self, generation: int) -> None:
        """Subscribe to live activity for all threads without requesting history replay."""
        self.stop_global_stream()
        self._global_stream_generation = generation
        self._global_stream_parser = SseParser()
        self._global_stream_connected = False
        reply = self._manager.get(self._request(self._url("/ui-api/events")))
        self._global_stream_reply = reply
        meta_data_changed = getattr(reply, "metaDataChanged", None)
        if meta_data_changed is not None:
            meta_data_changed.connect(
                lambda generation=generation, reply=reply: self._mark_global_stream_connected(
                    generation, reply
                )
            )
        reply.readyRead.connect(
            lambda generation=generation, reply=reply: self._consume_global_stream(
                generation, reply
            )
        )
        reply.finished.connect(
            lambda generation=generation, reply=reply: self._finish_global_stream(generation, reply)
        )
        self.global_stream_state_changed.emit(generation, "reconnecting")

    def _mark_global_stream_connected(self, generation: int, reply: Any) -> None:
        if self._active_global_stream(generation, reply) and not self._global_stream_connected:
            self._global_stream_connected = True
            self.global_stream_state_changed.emit(generation, "connected")

    def _active_global_stream(self, generation: int, reply: Any) -> bool:
        return self._global_stream_generation == generation and self._global_stream_reply is reply

    def _consume_global_stream(self, generation: int, reply: Any) -> None:
        if not self._active_global_stream(generation, reply) or self._global_stream_parser is None:
            return
        try:
            events = self._global_stream_parser.feed(bytes(reply.readAll()))
        except SseProtocolError:
            self._close_global_stream(generation, reply)
            return
        self._mark_global_stream_connected(generation, reply)
        for event in events:
            activity = parse_activity_event(event)
            if activity is not None and self._active_global_stream(generation, reply):
                self.global_activity_received.emit(generation, activity)

    def _finish_global_stream(self, generation: int, reply: Any) -> None:
        if not self._active_global_stream(generation, reply):
            return
        parser = self._global_stream_parser
        if not self._has_network_error(reply) and self._status(reply) in range(200, 300):
            try:
                events = parser.finish() if parser is not None else ()
            except SseProtocolError:
                events = ()
            for event in events:
                activity = parse_activity_event(event)
                if activity is not None:
                    self.global_activity_received.emit(generation, activity)
        self._global_stream_reply = None
        self._global_stream_generation = None
        self._global_stream_parser = None
        self._global_stream_connected = False
        reply.deleteLater()
        self.global_stream_state_changed.emit(generation, "disconnected")

    def _close_global_stream(self, generation: int, reply: Any) -> None:
        if not self._active_global_stream(generation, reply):
            return
        self._global_stream_reply = None
        self._global_stream_generation = None
        self._global_stream_parser = None
        self._global_stream_connected = False
        reply.abort()
        reply.deleteLater()
        self.global_stream_state_changed.emit(generation, "disconnected")

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
            self.stream_state_changed.emit(generation, "idle")

    def stop_global_stream(self) -> None:
        if self._global_stream_reply is None:
            return
        reply, generation = self._global_stream_reply, self._global_stream_generation
        self._global_stream_reply = None
        self._global_stream_generation = None
        self._global_stream_parser = None
        self._global_stream_connected = False
        reply.abort()
        reply.deleteLater()
        if generation is not None:
            self.global_stream_state_changed.emit(generation, "idle")

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
        self._control_response_bodies.clear()
        self._control_kinds.clear()
        self.abort_workflow_requests()
        composer_replies = tuple(self._composer_replies.values())
        self._composer_replies.clear()
        for reply in composer_replies:
            reply.abort()
            reply.deleteLater()
        self.stop_stream()
        self.stop_global_stream()
