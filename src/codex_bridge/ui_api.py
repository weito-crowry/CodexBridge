from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .account_auth import (
    AccountAuthError,
    project_account_auth_status,
    validate_auth_url,
)
from .activity import ActivityStore
from .attachment_policy import AttachmentPolicyError
from .bridge import ApprovalConflictError, Bridge, BridgeError
from .config import BridgeConfig
from .history import HistoryValidationError, project_thread_metadata, validate_cursor
from .jsonrpc import JsonRpcClosedError
from .logging_utils import log_event
from .models import ApprovalDecision, RequestId
from .paths import PathPolicyError
from .thread_titles import normalize_explicit_title
from .ui_control import ControlConflictError, create_control_routes
from .workflows.api import create_workflow_routes
from .workflows.definition_api import create_workflow_definition_routes
from .workflows.definitions import WorkflowDefinitionRepository
from .workflows.service import WorkflowService


class UiBridge(Protocol):
    @property
    def app_server_os_pid(self) -> int | None: ...

    async def approve(
        self, request_id: RequestId, decision: ApprovalDecision
    ) -> dict[str, Any]: ...

    async def rename_thread(
        self, thread_id: str, name: str, *, only_if_untitled: bool = False
    ) -> dict[str, Any]: ...

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    async def read_thread_turns(
        self,
        thread_id: str,
        limit: int = 20,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]: ...

    async def read_thread_items(
        self,
        thread_id: str,
        turn_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]: ...

    async def status(
        self,
        thread_id: str,
        turn_id: str | None = None,
        activity_limit: int = 20,
    ) -> dict[str, Any]: ...

    async def rate_limits(self) -> dict[str, Any]: ...

    def account_auth_status(self) -> dict[str, object]: ...

    async def refresh_account_auth(self) -> dict[str, object]: ...

    async def start_account_login(self, flow_type: str) -> dict[str, object]: ...

    async def cancel_account_login(
        self, login_id: str | None, revision: int
    ) -> dict[str, object]: ...


ShutdownCallback = Callable[[], Awaitable[None] | None]
_APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})


def _parse_limit(request: Request, default: int) -> int:
    raw = request.query_params.get("limit")
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("invalid limit") from exc
    if not 1 <= limit <= 100:
        raise ValueError("invalid limit")
    return limit


def _parse_activity_limit(request: Request) -> int:
    raw = request.query_params.get("activity_limit")
    if raw is None:
        return 20
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("invalid activity_limit") from exc
    if not 1 <= limit <= 100:
        raise ValueError("invalid activity_limit")
    return limit


def _parse_sort_direction(request: Request) -> str:
    value = request.query_params.get("sort_direction", "desc")
    if value not in {"asc", "desc"}:
        raise ValueError("invalid sort_direction")
    return value


def _cursor(request: Request) -> str | None:
    value = request.query_params.get("cursor")
    value = value or None
    if value is not None:
        try:
            validate_cursor(value)
        except HistoryValidationError as exc:
            raise ValueError("invalid cursor") from exc
    return value


def _app_server_ready(bridge: UiBridge) -> bool:
    explicit = getattr(bridge, "app_server_ready", None)
    if isinstance(explicit, bool):
        return explicit
    app_server = getattr(bridge, "_app_server", None)
    return not bool(getattr(app_server, "failed", False))


def _authorized_control_request(request: Request, token: str | None) -> bool:
    authorization = request.headers.get("authorization")
    if token is None or authorization is None:
        return False
    prefix = "Bearer "
    if not authorization.startswith(prefix) or not authorization[len(prefix) :]:
        return False
    try:
        return hmac.compare_digest(authorization[len(prefix) :], token)
    except TypeError:
        return False


def _authorized_local_auth_request(request: Request, token: str | None, ui_port: int) -> bool:
    if not _authorized_control_request(request, token) or request.client is None:
        return False
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            return False
        host = urlsplit("//" + request.headers.get("host", ""))
        if (
            host.hostname not in {"127.0.0.1", "localhost"}
            or host.username is not None
            or host.password is not None
            or host.path
            or host.query
            or host.fragment
            or (host.port is not None and host.port != ui_port)
        ):
            return False
        origin = request.headers.get("origin")
        if origin is None:
            return True
        parsed_origin = urlsplit(origin)
        return (
            parsed_origin.scheme == "http"
            and parsed_origin.hostname in {"127.0.0.1", "localhost"}
            and parsed_origin.port == ui_port
            and parsed_origin.username is None
            and parsed_origin.password is None
            and not parsed_origin.path
            and not parsed_origin.query
            and not parsed_origin.fragment
        )
    except ValueError:
        return False


async def _account_auth_json(request: Request, allowed_fields: set[str]) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("invalid account authentication request")
    raw = await request.body()
    if not raw or len(raw) > 4_096:
        raise ValueError("invalid account authentication request")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid account authentication request") from exc
    if not isinstance(payload, dict) or set(payload) != allowed_fields:
        raise ValueError("invalid account authentication request")
    return payload


def _auth_response(payload: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ControlConflictError):
        return JSONResponse({"error": "request conflict"}, status_code=409)
    if isinstance(exc, AttachmentPolicyError):
        messages = {
            "malformed_attachment_list": "Review the attachment selection and try again.",
            "too_many_attachments": "Remove attachments so no more than 8 files are selected.",
            "attachment_not_absolute": "Choose the file again from an allowed folder.",
            "attachment_missing_or_unavailable": "Choose an existing regular file and try again.",
            "attachment_not_regular_file": "Choose a regular file instead of a folder.",
            "attachment_outside_allowed_root": "Choose a file inside an allowed folder.",
            "duplicate_attachment": "Remove duplicate files from the attachment list.",
            "attachment_too_large": "Choose a smaller file.",
            "invalid_image_signature": "Choose a valid PNG or JPEG image.",
            "total_attachment_size_exceeded": "Choose fewer or smaller files.",
        }
        category = exc.category if exc.category in messages else "invalid_attachments"
        return JSONResponse(
            {
                "error": "invalid_attachment",
                "category": category,
                "message": messages.get(category, "Review the attachments and try again."),
            },
            status_code=400,
        )
    if isinstance(exc, PathPolicyError):
        cwd_messages = {
            "cwd_not_absolute": "Choose an absolute working folder.",
            "cwd_parent_traversal": "Choose a working folder without parent-folder traversal.",
            "cwd_missing_or_unavailable": "Choose an existing working folder.",
            "cwd_not_directory": "Choose an existing working folder, not a file.",
            "cwd_outside_allowed_root": "Choose a working folder inside an allowed folder.",
        }
        if exc.category in cwd_messages:
            return JSONResponse(
                {
                    "error": "invalid_cwd",
                    "category": exc.category,
                    "message": cwd_messages[exc.category],
                },
                status_code=404,
            )
        return JSONResponse({"error": "thread not found"}, status_code=404)
    if isinstance(exc, (HistoryValidationError, ValueError)):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if isinstance(exc, JsonRpcClosedError):
        return JSONResponse({"error": "App Server unavailable"}, status_code=503)
    if isinstance(exc, BridgeError):
        return JSONResponse({"error": "upstream App Server failure"}, status_code=502)
    return JSONResponse({"error": "upstream App Server failure"}, status_code=502)


def _safe_thread_list(result: dict[str, Any]) -> dict[str, object]:
    raw_threads = result.get("threads")
    threads: list[dict[str, object]] = []
    if isinstance(raw_threads, list):
        for thread in raw_threads:
            projected = project_thread_metadata(thread)
            if projected is not None:
                threads.append(projected)
    public: dict[str, object] = {
        "threads": threads,
        "next_cursor": result.get("next_cursor")
        if isinstance(result.get("next_cursor"), str)
        else None,
        "backwards_cursor": result.get("backwards_cursor")
        if isinstance(result.get("backwards_cursor"), str)
        else None,
    }
    if isinstance(result.get("external_threads"), list):
        public["external_threads"] = [
            row
            for value in result["external_threads"]
            if (row := project_thread_metadata(value)) is not None
        ]
    return public


def _safe_thread_detail(result: dict[str, Any]) -> dict[str, object] | None:
    return project_thread_metadata(result.get("thread"))


def _sse_event(activity: object) -> bytes:
    to_dict = getattr(activity, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("activity is not serializable")
    public = to_dict()
    activity_id = public.get("activity_id")
    if not isinstance(activity_id, str):
        raise TypeError("activity id is malformed")
    payload = json.dumps(public, separators=(",", ":"), ensure_ascii=False)
    return f"event: activity\nid: {activity_id}\ndata: {payload}\n\n".encode()


def create_ui_app(
    bridge: Bridge,
    activity_store: ActivityStore,
    config: BridgeConfig,
    *,
    shutdown_callback: ShutdownCallback | None = None,
    workflow_service: WorkflowService | None = None,
    workflow_definitions: WorkflowDefinitionRepository | None = None,
) -> Starlette:
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def status(request: Request) -> Response:
        return JSONResponse(
            {
                "bridge": "ready",
                "app_server": "ready" if _app_server_ready(bridge) else "failed",
                "ui_host": "127.0.0.1",
                "ui_port": config.ui_port,
            }
        )

    async def threads(request: Request) -> Response:
        try:
            limit = _parse_limit(request, 20)
            result = await bridge.threads(limit=limit, cursor=_cursor(request))
            return JSONResponse(_safe_thread_list(result))
        except Exception as exc:
            return _error_response(exc)

    async def rename_thread(request: Request) -> Response:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid name")
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid name")
            only_if_untitled = payload.get("only_if_untitled", False)
            if not isinstance(only_if_untitled, bool):
                raise ValueError("invalid conditional rename flag")
            thread_id = request.path_params["thread_id"]
            if only_if_untitled:
                normalized_name = normalize_explicit_title(name)
                result = await bridge.rename_thread(
                    thread_id, normalized_name, only_if_untitled=True
                )
                status = result.get("status")
                if status not in {"renamed", "skipped"}:
                    raise BridgeError("conditional rename returned an invalid status")
                return JSONResponse({"thread_id": thread_id, "status": status})
            await bridge.rename_thread(thread_id, name)
            return JSONResponse({"thread_id": thread_id, "name": name})
        except Exception as exc:
            return _error_response(exc)

    async def thread_detail(request: Request) -> Response:
        try:
            result = await bridge.threads(request.path_params["thread_id"])
            thread = _safe_thread_detail(result)
            if thread is None:
                return JSONResponse({"error": "thread not found"}, status_code=404)
            return JSONResponse({"thread": thread})
        except Exception as exc:
            return _error_response(exc)

    async def turns(request: Request) -> Response:
        try:
            result = await bridge.read_thread_turns(
                request.path_params["thread_id"],
                limit=_parse_limit(request, 20),
                cursor=_cursor(request),
                sort_direction=_parse_sort_direction(request),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def items(request: Request) -> Response:
        try:
            result = await bridge.read_thread_items(
                request.path_params["thread_id"],
                turn_id=request.query_params.get("turn_id") or None,
                limit=_parse_limit(request, 100),
                cursor=_cursor(request),
                sort_direction=_parse_sort_direction(request),
            )
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def thread_status(request: Request) -> Response:
        try:
            result = await bridge.status(
                request.path_params["thread_id"],
                turn_id=request.query_params.get("turn_id") or None,
                activity_limit=_parse_activity_limit(request),
            )
            result["app_server_os_pid"] = bridge.app_server_os_pid
            return JSONResponse(result)
        except Exception as exc:
            return _error_response(exc)

    async def rate_limits(request: Request) -> Response:
        try:
            return JSONResponse(await bridge.rate_limits())
        except Exception as exc:
            return _error_response(exc)

    async def account_auth_status(request: Request) -> Response:
        return _auth_response(project_account_auth_status(bridge.account_auth_status()))

    def local_auth_control(request: Request) -> bool:
        return _authorized_local_auth_request(request, config.control_token, config.ui_port)

    async def refresh_account_auth(request: Request) -> Response:
        if not local_auth_control(request):
            return _auth_response({"error": "forbidden"}, status_code=403)
        try:
            status = await bridge.refresh_account_auth()
            return _auth_response(project_account_auth_status(status))
        except Exception:
            return _auth_response({"error": "account_status_unavailable"}, status_code=503)

    async def start_account_login(request: Request) -> Response:
        if not local_auth_control(request):
            return _auth_response({"error": "forbidden"}, status_code=403)
        try:
            payload = await _account_auth_json(request, {"flow_type"})
            flow_type = payload.get("flow_type")
            if not isinstance(flow_type, str) or flow_type not in {
                "chatgpt",
                "chatgptDeviceCode",
            }:
                return _auth_response({"error": "unsupported_login_flow"}, status_code=400)
            login = await bridge.start_account_login(flow_type)
            login_id = login.get("loginId")
            response_type = login.get("type")
            login_revision = login.get("login_revision")
            if (
                not isinstance(login_id, str)
                or not login_id.strip()
                or len(login_id) > 256
                or response_type != flow_type
                or type(login_revision) is not int
                or login_revision <= 0
                or login_revision > 2**53 - 1
            ):
                return _auth_response({"error": "unsupported_login_response"}, status_code=502)
            private_result: dict[str, object] = {
                "login_id": login_id,
                "flow_type": flow_type,
                **project_account_auth_status(login),
            }
            start_issue = login.get("start_issue")
            if start_issue == "unsupported_login_response":
                private_result["start_issue"] = start_issue
                return _auth_response(private_result)
            if private_result["login_result"] in {"succeeded", "failed", "cancelled"}:
                return _auth_response(private_result)
            url_field = "authUrl" if flow_type == "chatgpt" else "verificationUrl"
            url = login.get(url_field)
            if not isinstance(url, str) or not validate_auth_url(url, flow_type):
                try:
                    status = await bridge.cancel_account_login(login_id, login_revision)
                except Exception:
                    status = bridge.account_auth_status()
                private_result.update(project_account_auth_status(status))
                private_result["start_issue"] = "unsupported_login_response"
                return _auth_response(private_result)
            private_result["auth_url" if flow_type == "chatgpt" else "verification_url"] = url
            if flow_type == "chatgptDeviceCode":
                user_code = login.get("userCode")
                if not isinstance(user_code, str) or not user_code.strip() or len(user_code) > 128:
                    try:
                        status = await bridge.cancel_account_login(login_id, login_revision)
                    except Exception:
                        status = bridge.account_auth_status()
                    private_result.update(project_account_auth_status(status))
                    private_result.pop("verification_url", None)
                    private_result["start_issue"] = "unsupported_login_response"
                    return _auth_response(private_result)
                private_result["user_code"] = user_code
            return _auth_response(private_result)
        except ValueError:
            return _auth_response({"error": "invalid_account_auth_request"}, status_code=400)
        except AccountAuthError as exc:
            status_code = 409 if exc.category in {"login_in_progress", "login_not_active"} else 503
            return _auth_response({"error": exc.category}, status_code=status_code)
        except Exception:
            return _auth_response({"error": "account_login_failed"}, status_code=503)

    async def cancel_account_login(request: Request) -> Response:
        if not local_auth_control(request):
            return _auth_response({"error": "forbidden"}, status_code=403)
        try:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("invalid account authentication request")
            raw = await request.body()
            if not raw or len(raw) > 4_096:
                raise ValueError("invalid account authentication request")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) not in (
                {"login_revision"},
                {"login_id", "login_revision"},
            ):
                raise ValueError("invalid account authentication request")
            login_id = payload.get("login_id")
            login_revision = payload.get("login_revision")
            if login_id is not None and (
                not isinstance(login_id, str) or not login_id.strip() or len(login_id) > 256
            ):
                return _auth_response({"error": "invalid_account_auth_request"}, status_code=400)
            if type(login_revision) is not int or not 1 <= login_revision <= 2**53 - 1:
                return _auth_response({"error": "invalid_account_auth_request"}, status_code=400)
            status = await bridge.cancel_account_login(login_id, login_revision)
            return _auth_response(project_account_auth_status(status))
        except ValueError:
            return _auth_response({"error": "invalid_account_auth_request"}, status_code=400)
        except AccountAuthError as exc:
            return _auth_response({"error": exc.category}, status_code=409)
        except Exception:
            return _auth_response({"error": "account_login_cancel_failed"}, status_code=503)

    async def events(request: Request) -> Response:
        thread_id = request.query_params.get("thread_id") or None
        if thread_id is not None:
            try:
                if _safe_thread_detail(await bridge.threads(thread_id)) is None:
                    return JSONResponse({"error": "thread not found"}, status_code=404)
            except Exception as exc:
                return _error_response(exc)

        subscription = activity_store.subscribe(thread_id)

        async def stream() -> AsyncIterator[bytes]:
            observations = getattr(bridge, "observations", None) if thread_id is None else None
            previous = None
            try:
                while True:
                    if observations is not None:
                        snapshot = observations.external_snapshot()
                        identity = (snapshot["state"], snapshot["threads"])
                        if identity != previous:
                            previous = identity
                            payload = json.dumps(
                                snapshot, separators=(",", ":"), ensure_ascii=False
                            )
                            yield f"event: external_threads\ndata: {payload}\n\n".encode()
                    try:
                        activity = await asyncio.wait_for(
                            subscription.get_or_closed(), timeout=1.0 if observations else 15.0
                        )
                    except TimeoutError:
                        yield b": keepalive\n\n"
                    else:
                        if activity is None:
                            return
                        yield _sse_event(activity)
            finally:
                subscription.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def shutdown(request: Request) -> Response:
        if not _authorized_control_request(request, config.control_token):
            return JSONResponse({"error": "forbidden"}, status_code=403)

        assert shutdown_callback is not None
        log_event("bridge.control.shutdown.requested", reason="control_api")

        async def run_shutdown() -> None:
            result = shutdown_callback()
            if inspect.isawaitable(result):
                await result

        return JSONResponse(
            {"status": "shutdown_requested"},
            status_code=202,
            background=BackgroundTask(run_shutdown),
        )

    async def approval(request: Request) -> Response:
        if not _authorized_control_request(request, config.control_token):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid approval request")
            request_id = payload.get("request_id")
            decision = payload.get("decision")
            if (
                isinstance(request_id, bool)
                or not isinstance(request_id, (int, str))
                or not isinstance(decision, str)
                or decision not in _APPROVAL_DECISIONS
            ):
                raise ValueError("invalid approval request")
            await bridge.approve(request_id, decision)  # type: ignore[arg-type]
            return JSONResponse({"status": "approval_resolved"})
        except ApprovalConflictError:
            return JSONResponse({"error": "already_resolved"}, status_code=409)
        except Exception as exc:
            return _error_response(exc)

    routes = [
        Route("/healthz", healthz),
        Route("/ui-api/status", status),
        Route("/ui-api/threads", threads),
        Route("/ui-api/threads/{thread_id}/name", rename_thread, methods=["POST"]),
        Route("/ui-api/threads/{thread_id}", thread_detail),
        Route("/ui-api/threads/{thread_id}/turns", turns),
        Route("/ui-api/threads/{thread_id}/items", items),
        Route("/ui-api/threads/{thread_id}/status", thread_status),
        Route("/ui-api/account/rate-limits", rate_limits),
        Route("/ui-api/account/auth", account_auth_status),
        Route("/ui-api/events", events),
    ]
    if config.control_token is not None and shutdown_callback is not None:
        routes.append(Route("/ui-api/control/shutdown", shutdown, methods=["POST"]))
    if config.control_token is not None:
        routes.append(Route("/ui-api/control/approval", approval, methods=["POST"]))
        routes.extend(
            [
                Route(
                    "/ui-api/control/account/auth/refresh",
                    refresh_account_auth,
                    methods=["POST"],
                ),
                Route(
                    "/ui-api/control/account/auth/login/start",
                    start_account_login,
                    methods=["POST"],
                ),
                Route(
                    "/ui-api/control/account/auth/login/cancel",
                    cancel_account_login,
                    methods=["POST"],
                ),
            ]
        )
        routes.extend(
            create_control_routes(bridge, config, _authorized_control_request, _error_response)
        )
        if workflow_service is not None:
            routes.extend(
                create_workflow_routes(
                    workflow_service,
                    token=config.control_token,
                    ui_port=config.ui_port,
                    authorize=_authorized_control_request,
                    ready=lambda: _app_server_ready(bridge),
                )
            )
        routes.extend(
            create_workflow_definition_routes(
                workflow_definitions,
                service=workflow_service,
                token=config.control_token,
                ui_port=config.ui_port,
                authorize=_authorized_control_request,
                ready=lambda: _app_server_ready(bridge),
            )
        )
    app = Starlette(routes=routes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.state.bridge = bridge
    app.state.activity_store = activity_store
    app.state.config = config
    return app
