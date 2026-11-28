from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from codex_bridge.workflows.agent_contracts import (
    AgentContractError,
    AgentRequest,
    PreparedProviderRequest,
    ProviderRegistry,
    TargetContext,
)

CONTROL_TOKEN = "i3-local-test-token-do-not-persist"
THREAD_ID = "thread_123456"
TURN_ID = "turn_123456"


class _APIState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.create_status = 201
        self.continue_status = 201
        self.interrupt_status = 200
        self.capabilities_status = 200
        self.capabilities_body: Any = {"models": []}
        self.create_body: Any = {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "state": "in_progress",
        }
        self.continue_body: Any | None = None
        self.interrupt_body: Any | None = None
        self.status_body: Any = {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "state": "in_progress",
        }
        self.turn_pages: dict[str, Any] = {
            "": {"thread_id": THREAD_ID, "turns": [], "next_cursor": None, "truncated": False}
        }
        self.item_pages: dict[str, Any] = {
            "": {
                "thread_id": THREAD_ID,
                "turn_id": TURN_ID,
                "items": [],
                "next_cursor": None,
                "truncated": False,
            }
        }
        self.response_overrides: dict[str, tuple[int, bytes]] = {}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def state(self) -> _APIState:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.state.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        return body

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        self._reply(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _dispatch(self) -> None:
        self._record()
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        elif self.path == "/ui-api/status":
            self._json(200, {"app_server": "ready"})
        elif self.path == "/ui-api/control/capabilities":
            self._json(self.state.capabilities_status, self.state.capabilities_body)
        elif self.path == "/ui-api/control/threads":
            self._json(self.state.create_status, self.state.create_body)
        else:
            parsed = urlsplit(self.path)
            override = self.state.response_overrides.get(parsed.path)
            if override is not None:
                self._reply(*override)
                return
            if self.command == "POST" and parsed.path.endswith("/interrupt"):
                parts = parsed.path.split("/")
                body = self.state.interrupt_body or {
                    "thread_id": unquote(parts[4]),
                    "turn_id": unquote(parts[6]),
                    "state": "in_progress",
                }
                self._json(self.state.interrupt_status, body)
            elif self.command == "POST" and parsed.path.endswith("/turns"):
                parts = parsed.path.split("/")
                body = self.state.continue_body or {
                    "thread_id": unquote(parts[4]),
                    "turn_id": "turn_123456",
                    "state": "in_progress",
                }
                self._json(self.state.continue_status, body)
            elif parsed.path.endswith("/status"):
                self._json(200, self.state.status_body)
            elif parsed.path.endswith("/turns"):
                cursor = parse_qs(parsed.query).get("cursor", [""])[0]
                self._json(200, self.state.turn_pages.get(cursor, self.state.turn_pages[""]))
            elif parsed.path.endswith("/items"):
                cursor = parse_qs(parsed.query).get("cursor", [""])[0]
                self._json(200, self.state.item_pages.get(cursor, self.state.item_pages[""]))
            else:
                self._json(404, {"error": "not found"})

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
    server.state = _APIState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _modules():
    try:
        from codex_bridge.workflows import codex_agent_provider, http_client
    except ImportError as exc:
        pytest.fail(f"D2-I3 Codex Provider is not implemented: {exc.name}")
    return codex_agent_provider, http_client


def _provider(server: _FakeHTTPServer, workspace: Path, **kwargs: Any):
    provider_module, _http_module = _modules()
    return provider_module.CodexAgentProvider(
        port=server.server_address[1],
        allowed_roots=(str(workspace),),
        workspace_root=str(workspace),
        **kwargs,
    )


def _invoke(context: TargetContext, **changes: Any) -> AgentRequest:
    values: dict[str, Any] = {
        "operation": "invoke",
        "provider": "codex",
        "target": "default",
        "target_context": context,
        "session": {"mode": "new"},
        "handle": None,
        "options": {},
        "prompt": "Say hello.",
        "response": {"format": "text"},
        "wait": True,
        "timeout_seconds": None,
    }
    values.update(changes)
    return AgentRequest(**values)


def _read_only(
    context: TargetContext,
    operation: str,
    *,
    handle: dict[str, str] | None = None,
    timeout: int | None = None,
) -> AgentRequest:
    return AgentRequest(
        operation=operation,
        provider="codex",
        target="default",
        target_context=context,
        session=None,
        handle=handle or {"thread_id": THREAD_ID, "turn_id": TURN_ID},
        options=None,
        prompt=None,
        response={"format": "text"},
        wait=None,
        timeout_seconds=timeout,
    )


def _interrupt(context: TargetContext, handle: dict[str, str]) -> AgentRequest:
    return AgentRequest(
        operation="interrupt",
        provider="codex",
        target="default",
        target_context=context,
        session=None,
        handle=handle,
        options=None,
        prompt=None,
        response=None,
        wait=None,
        timeout_seconds=None,
    )


def test_codex_provider_advertises_i5_operations_and_usage_is_unknown(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider = _provider(fake_api, tmp_path)

    capabilities = provider.capabilities("default")

    assert capabilities.operations == ("invoke", "observe", "wait", "interrupt")
    assert capabilities.session_modes == ("new", "continue")
    assert capabilities.usage_supported is False
    registry = ProviderRegistry({"codex": provider})
    assert registry.resolve("codex", "default") == capabilities
    context = capabilities.target_context
    request = _invoke(context, options={"unknown-option": "value"})
    with pytest.raises(AgentContractError) as error:
        provider.prepare(request, context)
    assert error.value.code == "unsupported_option"
    assert fake_api.state.requests == []


def test_prepare_is_read_only_and_validates_unicode_prompt_limits_and_options(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    request = _invoke(context, prompt="x" * 20_000)

    prepared = provider.prepare(request, context)

    assert prepared.prompt == "x" * 20_000
    assert fake_api.state.requests == []


def test_prepare_supports_dispatch_only_and_explicit_read_only_operations(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context

    dispatch_only = provider.prepare(_invoke(context, wait=False), context)
    observe = provider.prepare(_read_only(context, "observe"), context)
    wait = provider.prepare(_read_only(context, "wait", timeout=3), context)

    assert dispatch_only.operation == "invoke" and dispatch_only.wait is False
    assert observe.operation == "observe" and observe.handle == {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
    }
    assert wait.operation == "wait" and wait.timeout_seconds == 3
    assert fake_api.state.requests == []


@pytest.mark.parametrize(
    "options",
    [
        {"sandbox_mode": "danger-full-access"},
        {"sandbox_mode": "danger-full-access", "confirm_full_access": False},
    ],
)
def test_danger_full_access_requires_explicit_confirmation_before_any_request(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    options: dict[str, Any],
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context

    with pytest.raises(AgentContractError) as error:
        provider.prepare(_invoke(context, options=options), context)

    assert error.value.code == "invalid_request"
    assert fake_api.state.requests == []


def test_dispatch_rejects_prepared_read_only_operation_without_post(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = PreparedProviderRequest(_read_only(context, "observe"), context)

    with pytest.raises(AgentContractError) as error:
        provider.dispatch(prepared, "cba-" + "a" * 64)

    assert error.value.code == "unsupported_operation"
    assert fake_api.state.requests == []


def test_provider_preflight_and_dispatch_use_only_fake_local_bridge(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider, http_client = _modules()
    workspace = tmp_path.resolve()
    client = http_client.CodexAgentHTTPClient(port=fake_api.server_address[1])
    instance = _provider(fake_api, workspace, http_client=client)
    context = instance.capabilities("default").target_context
    prepared = instance.prepare(_invoke(context), context)

    instance.preflight(prepared)
    receipt = instance.dispatch(prepared, "cba-" + "a" * 64)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == {"thread_id": THREAD_ID, "turn_id": TURN_ID}
    assert [item["method"] for item in fake_api.state.requests] == ["GET", "GET", "GET", "POST"]
    post = fake_api.state.requests[-1]
    assert post["headers"]["Authorization"] == f"Bearer {CONTROL_TOKEN}"
    body = json.loads(post["body"])
    assert body == {
        "client_request_id": "cba-" + "a" * 64,
        "cwd": str(workspace),
        "prompt": "Say hello.",
        "sandbox_mode": "inherit",
        "confirm_full_access": False,
    }
    assert CONTROL_TOKEN.encode() not in post["body"]


def test_new_session_model_effort_and_explicit_full_access_are_forwarded(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    fake_api.state.capabilities_body = {
        "models": [{"model": "model-a", "reasoning_efforts": [{"id": "high"}]}]
    }
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    request = _invoke(
        context,
        options={
            "model": "model-a",
            "reasoning_effort": "high",
            "title": "Workflow request",
            "sandbox_mode": "danger-full-access",
            "confirm_full_access": True,
        },
    )
    prepared = provider.prepare(request, context)

    provider.preflight(prepared)
    receipt = provider.dispatch(prepared, "cba-" + "6" * 64)

    assert receipt.disposition == "ACCEPTED"
    post = [item for item in fake_api.state.requests if item["method"] == "POST"]
    assert len(post) == 1
    assert json.loads(post[0]["body"]) == {
        "client_request_id": "cba-" + "6" * 64,
        "cwd": str(tmp_path.resolve()),
        "prompt": "Say hello.",
        "model": "model-a",
        "reasoning_effort": "high",
        "title": "Workflow request",
        "sandbox_mode": "danger-full-access",
        "confirm_full_access": True,
    }


@pytest.mark.parametrize(
    ("catalog", "options", "expected_code"),
    [
        ({"models": []}, {"model": "missing-model"}, "unsupported_option"),
        (
            {"models": [{"model": "model-a", "reasoning_efforts": [{"id": "low"}]}]},
            {"model": "model-a", "reasoning_effort": "high"},
            "unsupported_option",
        ),
        ({"models": "malformed"}, {"model": "model-a"}, "preflight_failed"),
        (
            {"models": [{"model": "model-a"}, {"model": "model-a"}]},
            {"model": "model-a"},
            "preflight_failed",
        ),
        (
            {"models": [{"model": "model-a", "reasoning_efforts": "malformed"}]},
            {"model": "model-a", "reasoning_effort": "high"},
            "preflight_failed",
        ),
        ({"models": [{"model": "model-a"}]}, {"model": "model-a"}, "preflight_failed"),
        (
            {
                "models": [
                    {
                        "model": "model-a",
                        "reasoning_efforts": [{"id": "low"}, {"id": "low"}],
                    }
                ]
            },
            {"model": "model-a"},
            "preflight_failed",
        ),
    ],
)
def test_model_catalog_is_validated_before_dispatch_claim_or_post(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    catalog: Any,
    options: dict[str, str],
    expected_code: str,
) -> None:
    fake_api.state.capabilities_body = catalog
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context, options=options), context)

    with pytest.raises(AgentContractError) as error:
        provider.preflight(prepared)

    assert error.value.code == expected_code
    assert not any(item["method"] == "POST" for item in fake_api.state.requests)


def test_unavailable_model_catalog_fails_preflight_without_post(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    fake_api.state.capabilities_status = 503
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context, options={"model": "model-a"}), context)

    with pytest.raises(AgentContractError) as error:
        provider.preflight(prepared)

    assert error.value.code == "preflight_failed"
    assert not any(item["method"] == "POST" for item in fake_api.state.requests)


def test_effort_without_model_is_rejected_before_control_request(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context

    with pytest.raises(AgentContractError) as error:
        provider.prepare(_invoke(context, options={"reasoning_effort": "high"}), context)

    assert error.value.code == "invalid_request"
    assert fake_api.state.requests == []


@pytest.mark.parametrize("wait", [True, False])
def test_continue_dispatches_exact_thread_without_workspace_or_options(
    fake_api: _FakeHTTPServer, tmp_path: Path, wait: bool
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    thread_id = "thread:external"
    prepared = provider.prepare(
        _invoke(
            context,
            session={"mode": "continue", "thread_id": thread_id},
            prompt="Continue the external thread.",
            options={},
            wait=wait,
        ),
        context,
    )
    provider.preflight(prepared)

    receipt = provider.dispatch(prepared, "cba-" + "9" * 64)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == {"thread_id": thread_id, "turn_id": "turn_123456"}
    posts = [item for item in fake_api.state.requests if item["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/ui-api/control/threads/thread%3Aexternal/turns"
    assert json.loads(posts[0]["body"]) == {
        "client_request_id": "cba-" + "9" * 64,
        "prompt": "Continue the external thread.",
    }


def test_continue_options_are_rejected_before_post(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context

    with pytest.raises(AgentContractError) as error:
        provider.prepare(
            _invoke(
                context,
                session={"mode": "continue", "thread_id": "thread_123456"},
                options={"title": "unsupported"},
            ),
            context,
        )

    assert error.value.code == "unsupported_option"
    assert fake_api.state.requests == []


def test_continue_mismatch_and_409_are_uncertain_without_retry(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(
        _invoke(
            context,
            session={"mode": "continue", "thread_id": "thread_123456"},
            options={},
        ),
        context,
    )
    fake_api.state.continue_body = {
        "thread_id": "thread_654321",
        "turn_id": "turn_123456",
        "state": "in_progress",
    }
    mismatch = provider.dispatch(prepared, "cba-" + "a" * 64)
    assert mismatch.disposition == "UNCERTAIN"
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1

    fake_api.state.requests.clear()
    fake_api.state.continue_body = None
    fake_api.state.continue_status = 409
    conflict = provider.dispatch(prepared, "cba-" + "b" * 64)
    assert conflict.disposition == "UNCERTAIN"
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1

    fake_api.state.requests.clear()
    fake_api.state.continue_status = 200
    fake_api.state.continue_body = {
        "thread_id": "thread_123456",
        "turn_id": "turn_123456",
    }
    malformed = provider.dispatch(prepared, "cba-" + "e" * 64)
    assert malformed.disposition == "UNCERTAIN"
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1


def test_interrupt_ack_is_accepted_for_exact_handle_without_observation(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    handle = {"thread_id": "thread:external", "turn_id": "turn:active"}
    prepared = provider.prepare(_interrupt(context, handle), context)
    provider.preflight(prepared)
    preflight_request_count = len(fake_api.state.requests)

    receipt = provider.dispatch(prepared, "cba-" + "c" * 64)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == handle
    assert receipt.remote_state == "in_progress"
    posts = [item for item in fake_api.state.requests if item["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == (
        "/ui-api/control/threads/thread%3Aexternal/turns/turn%3Aactive/interrupt"
    )
    assert json.loads(posts[0]["body"]) == {"client_request_id": "cba-" + "c" * 64}
    dispatch_requests = fake_api.state.requests[preflight_request_count:]
    assert not any(
        item["path"].split("?")[0].endswith(("/status", "/turns", "/items"))
        for item in dispatch_requests
    )


def test_provider_protocol_interrupt_dispatches_one_explicit_handle(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    handle = {"thread_id": "thread:external", "turn_id": "turn:active"}

    receipt = provider.interrupt(handle, context, "cba-" + "7" * 64)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == handle
    assert receipt.remote_state == "in_progress"
    assert len(fake_api.state.requests) == 1
    request = fake_api.state.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == (
        "/ui-api/control/threads/thread%3Aexternal/turns/turn%3Aactive/interrupt"
    )


@pytest.mark.parametrize("failure", ["mismatch", "conflict", "malformed"])
def test_interrupt_mismatch_or_409_is_uncertain_without_retry(
    fake_api: _FakeHTTPServer, tmp_path: Path, failure: str
) -> None:
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    handle = {"thread_id": THREAD_ID, "turn_id": TURN_ID}
    prepared = provider.prepare(_interrupt(context, handle), context)
    if failure == "mismatch":
        fake_api.state.interrupt_body = {
            "thread_id": THREAD_ID,
            "turn_id": "turn_654321",
            "state": "interrupted",
        }
    elif failure == "conflict":
        fake_api.state.interrupt_status = 409
    else:
        fake_api.state.interrupt_body = {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
        }

    receipt = provider.dispatch(prepared, "cba-" + "d" * 64)

    assert receipt.disposition == "UNCERTAIN"
    assert receipt.handle == handle
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1


@pytest.mark.parametrize(
    "title_assignment",
    [
        {"status": "assigned", "name": "Task title"},
        {"status": "failed"},
    ],
)
def test_title_assignment_metadata_does_not_affect_dispatch_receipt(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    title_assignment: dict[str, str],
) -> None:
    fake_api.state.create_body = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "state": "in_progress",
        "title_assignment": title_assignment,
        "future_metadata": {"ignored": True},
    }
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context), context)

    receipt = provider.dispatch(prepared, "cba-" + "f" * 64)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == {"thread_id": THREAD_ID, "turn_id": TURN_ID}
    assert receipt.remote_state == "in_progress"
    assert receipt.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment_status", ["assigned", "failed"])
async def test_existing_ui_control_route_response_is_accepted_by_agent_provider(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    assignment_status: str,
) -> None:
    from codex_bridge.activity import ActivityStore
    from codex_bridge.ui_api import create_ui_app
    from tests.test_ui_control import (
        FakeBridge as UiFakeBridge,
    )
    from tests.test_ui_control import (
        config as ui_config,
    )
    from tests.test_ui_control import (
        request as ui_request,
    )
    from tests.test_ui_control import (
        route as ui_route,
    )

    if assignment_status == "failed":

        class FailedTitleBridge(UiFakeBridge):
            async def start(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                result = await super().start(*args, **kwargs)
                result["title_assignment"] = {"status": "failed"}
                result["title_warning"] = "Thread name could not be set"
                return result

        bridge = FailedTitleBridge()
    else:
        bridge = UiFakeBridge()
    app = create_ui_app(bridge, ActivityStore(), ui_config(tmp_path))
    request_id = "cba-" + "7" * 64
    body = json.dumps(
        {
            "client_request_id": request_id,
            "cwd": str(tmp_path.resolve()),
            "prompt": "Start an isolated workflow task.",
            "model": "model-a",
            "reasoning_effort": "high",
            "sandbox_mode": "inherit",
            "title": "  Task title  ",
        }
    ).encode("utf-8")
    route_response = await ui_route(app, "/ui-api/control/threads").endpoint(
        ui_request(
            "/ui-api/control/threads",
            body=body,
            authorization="Bearer local-control-token",
        )
    )
    api_response = json.loads(route_response.body)
    assert route_response.status_code == 200
    if assignment_status == "assigned":
        assert api_response["title_assignment"] == {"status": "assigned", "name": "Task title"}
    else:
        assert api_response["title_assignment"] == {"status": "failed"}

    fake_api.state.create_body = api_response
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context), context)

    receipt = provider.dispatch(prepared, request_id)

    assert receipt.disposition == "ACCEPTED"
    assert receipt.handle == {
        "thread_id": "native-thread-1",
        "turn_id": "native-turn-1",
    }
    assert receipt.remote_state == "in_progress"


@pytest.mark.parametrize(
    "required_response",
    [
        {"thread_id": "invalid id", "turn_id": TURN_ID, "state": "in_progress"},
        {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "not_loaded"},
    ],
)
def test_invalid_required_post_receipt_fields_remain_uncertain(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    required_response: dict[str, str],
) -> None:
    fake_api.state.create_body = required_response
    provider = _provider(fake_api, tmp_path)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context), context)

    receipt = provider.dispatch(prepared, "cba-" + "8" * 64)

    assert receipt.disposition == "UNCERTAIN"


def test_agent_http_body_limit_is_independent_of_legacy_16k_prompt_limit(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    _provider_module, http_client = _modules()
    client = http_client.CodexAgentHTTPClient(port=fake_api.server_address[1])
    payload = {
        "client_request_id": "cba-" + "b" * 64,
        "cwd": str(tmp_path.resolve()),
        "prompt": "x" * 20_000,
        "sandbox_mode": "inherit",
        "confirm_full_access": False,
    }

    client.create_agent_thread(payload)

    assert len(fake_api.state.requests) == 1
    assert len(fake_api.state.requests[0]["body"]) > 16_384
    assert len(fake_api.state.requests[0]["body"]) <= 65_536


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 32_001, "\ud800"])
def test_agent_http_rejects_empty_oversized_and_invalid_unicode_before_post(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    prompt: str,
) -> None:
    _provider_module, http_client = _modules()
    client = http_client.CodexAgentHTTPClient(port=fake_api.server_address[1])
    payload = {
        "client_request_id": "cba-" + "c" * 64,
        "cwd": str(tmp_path.resolve()),
        "prompt": prompt,
        "sandbox_mode": "inherit",
        "confirm_full_access": False,
    }

    with pytest.raises(http_client.CodexProtocolError):
        client.create_agent_thread(payload)

    assert fake_api.state.requests == []


@pytest.mark.parametrize("status", [400, 403, 404, 409, 413, 422, 500, 502, 503])
def test_post_http_status_is_preserved_for_certainty_classification(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    status: int,
) -> None:
    provider_module, http_client = _modules()
    fake_api.state.create_status = status
    client = http_client.CodexAgentHTTPClient(port=fake_api.server_address[1])
    provider = _provider(fake_api, tmp_path, http_client=client)
    context = provider.capabilities("default").target_context
    prepared = provider.prepare(_invoke(context), context)

    receipt = provider.dispatch(prepared, "cba-" + "d" * 64)

    if status in {403, 404, 413}:
        assert receipt.disposition == "REJECTED"
    else:
        assert receipt.disposition == "UNCERTAIN"
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1


def test_malformed_post_receipt_is_unknown_and_never_retried(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.create_body = {"thread_id": THREAD_ID, "state": "in_progress"}
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context
    prepared = instance.prepare(_invoke(context), context)

    first = instance.dispatch(prepared, "cba-" + "e" * 64)

    assert first.disposition == "UNCERTAIN"
    assert sum(item["method"] == "POST" for item in fake_api.state.requests) == 1


def test_running_observation_reads_status_only(fake_api: _FakeHTTPServer, tmp_path: Path) -> None:
    provider, _http_module = _modules()
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe(
        {"thread_id": THREAD_ID, "turn_id": TURN_ID},
        context,
    )

    assert observation.state == "RUNNING"
    assert [item["path"].split("?")[0] for item in fake_api.state.requests] == [
        f"/ui-api/threads/{THREAD_ID}/status"
    ]


@pytest.mark.parametrize(
    ("state", "expected", "error_code"),
    [
        ("needs_approval", "BLOCKED", "needs_approval"),
        ("needs_input", "BLOCKED", "needs_input"),
    ],
)
def test_needs_approval_or_input_is_blocked_without_history(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    state: str,
    expected: str,
    error_code: str,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": state}
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == expected
    assert observation.error is not None and observation.error["code"] == error_code
    assert len(fake_api.state.requests) == 1


@pytest.mark.parametrize(
    ("state", "expected", "error_code"),
    [
        ("failed", "FAILED", "remote_failed"),
        ("interrupted", "INTERRUPTED", "remote_interrupted"),
    ],
)
def test_terminal_failure_states_require_an_exact_history_turn(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
    state: str,
    expected: str,
    error_code: str,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "state": state,
    }
    fake_api.state.turn_pages[""] = {
        "thread_id": THREAD_ID,
        "turns": [{"id": TURN_ID, "status": state}],
        "next_cursor": None,
        "truncated": False,
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == expected
    assert observation.handle == {"thread_id": THREAD_ID, "turn_id": TURN_ID}
    assert observation.error is not None and observation.error["code"] == error_code


def test_remote_timed_out_state_requires_exact_history_turn(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "state": "timed_out",
    }
    fake_api.state.turn_pages[""] = {
        "thread_id": THREAD_ID,
        "turns": [{"id": TURN_ID, "status": "timed_out"}],
        "next_cursor": None,
        "truncated": False,
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "TIMED_OUT"
    assert observation.remote_state == "timed_out"
    assert observation.error is not None and observation.error["code"] == "timeout"


def test_unknown_state_requires_exact_history_turn_and_preserves_remote_state(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "state": "unknown",
    }
    fake_api.state.turn_pages[""] = {
        "thread_id": THREAD_ID,
        "turns": [{"id": TURN_ID, "status": "unknown"}],
        "next_cursor": None,
        "truncated": False,
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "UNKNOWN"
    assert observation.handle == {"thread_id": THREAD_ID, "turn_id": TURN_ID}
    assert observation.remote_state == "unknown"
    assert observation.error is not None and observation.error["code"] == "observation_unavailable"


def test_completed_turn_and_final_answer_are_found_across_history_pages(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "completed"}
    fake_api.state.turn_pages = {
        "": {
            "thread_id": THREAD_ID,
            "turns": [{"id": "turn_older123", "status": "completed"}],
            "next_cursor": "turns-page-2",
            "truncated": True,
        },
        "turns-page-2": {
            "thread_id": THREAD_ID,
            "turns": [{"id": TURN_ID, "status": "completed"}],
            "next_cursor": None,
            "truncated": False,
        },
    }
    fake_api.state.item_pages = {
        "": {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "items": [],
            "next_cursor": "items-page-2",
            "truncated": True,
        },
        "items-page-2": {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "items": [
                {
                    "turn_id": TURN_ID,
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "done"},
                }
            ],
            "next_cursor": None,
            "truncated": False,
        },
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "COMPLETED"
    assert observation.final_text == "done"
    assert [item["path"].split("?")[0] for item in fake_api.state.requests] == [
        f"/ui-api/threads/{THREAD_ID}/status",
        f"/ui-api/threads/{THREAD_ID}/turns",
        f"/ui-api/threads/{THREAD_ID}/turns",
        f"/ui-api/threads/{THREAD_ID}/items",
        f"/ui-api/threads/{THREAD_ID}/items",
    ]
    assert parse_qs(urlsplit(fake_api.state.requests[2]["path"]).query)["cursor"] == [
        "turns-page-2"
    ]
    assert parse_qs(urlsplit(fake_api.state.requests[4]["path"]).query)["cursor"] == [
        "items-page-2"
    ]


def test_not_loaded_status_recovers_exact_completed_turn_from_history(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "not_loaded"}
    fake_api.state.turn_pages = {
        "": {
            "thread_id": THREAD_ID,
            "turns": [{"id": TURN_ID, "status": "completed"}],
            "next_cursor": None,
            "truncated": False,
        }
    }
    fake_api.state.item_pages = {
        "": {
            "thread_id": THREAD_ID,
            "turn_id": TURN_ID,
            "items": [
                {
                    "turn_id": TURN_ID,
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "recovered"},
                }
            ],
            "next_cursor": None,
            "truncated": False,
        }
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "COMPLETED"
    assert observation.final_text == "recovered"


def test_history_conflicts_and_invalid_cursor_progress_return_unknown(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "completed"}
    fake_api.state.turn_pages = {
        "": {
            "thread_id": THREAD_ID,
            "turns": [{"id": TURN_ID, "status": "failed"}],
            "next_cursor": "repeat",
            "truncated": True,
        },
        "repeat": {
            "thread_id": THREAD_ID,
            "turns": [{"id": "turn_other123", "status": "completed"}],
            "next_cursor": "repeat",
            "truncated": True,
        },
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "UNKNOWN"
    assert observation.error is not None
    assert observation.error["code"] == "observation_unavailable"
    assert len(fake_api.state.requests) == 3


def test_multiple_final_answer_items_for_one_turn_are_unknown(
    fake_api: _FakeHTTPServer,
    tmp_path: Path,
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "completed"}
    fake_api.state.turn_pages[""] = {
        "thread_id": THREAD_ID,
        "turns": [{"id": TURN_ID, "status": "completed"}],
        "next_cursor": None,
        "truncated": False,
    }
    fake_api.state.item_pages[""] = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "items": [
            {
                "turn_id": TURN_ID,
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "first"},
            },
            {
                "turn_id": TURN_ID,
                "item": {"type": "agentMessage", "phase": "final_answer", "text": "second"},
            },
        ],
        "next_cursor": None,
        "truncated": False,
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "UNKNOWN"
    assert observation.remote_state == "completed"
    assert observation.error is not None
    assert observation.error["code"] == "response_unavailable"


def test_final_answer_token_exclusion_is_preserved(
    fake_api: _FakeHTTPServer, tmp_path: Path
) -> None:
    provider, _http_module = _modules()
    fake_api.state.status_body = {"thread_id": THREAD_ID, "turn_id": TURN_ID, "state": "completed"}
    fake_api.state.turn_pages[""] = {
        "thread_id": THREAD_ID,
        "turns": [{"id": TURN_ID, "status": "completed"}],
        "next_cursor": None,
        "truncated": False,
    }
    fake_api.state.item_pages[""] = {
        "thread_id": THREAD_ID,
        "turn_id": TURN_ID,
        "items": [
            {
                "turn_id": TURN_ID,
                "item": {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": f"token={CONTROL_TOKEN}",
                },
            }
        ],
        "next_cursor": None,
        "truncated": False,
    }
    instance = _provider(fake_api, tmp_path)
    context = instance.capabilities("default").target_context

    observation = instance.observe({"thread_id": THREAD_ID, "turn_id": TURN_ID}, context)

    assert observation.state == "UNKNOWN"
    assert observation.final_text is None
    assert CONTROL_TOKEN not in str(observation.to_dict())
