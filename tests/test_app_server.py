from __future__ import annotations

import asyncio

import pytest

from codex_bridge.app_server import AppServerClient
from tests.fakes import FakeProcessFactory


@pytest.mark.asyncio
async def test_app_server_sends_initialize_then_initialized() -> None:
    factory = FakeProcessFactory()
    client = AppServerClient("codex", process_factory=factory)

    await client.start()

    assert factory.args == ("codex", "app-server", "--stdio")
    assert factory.process.methods == ["initialize"]
    assert factory.process.stdin.writes[-1] == {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {},
    }
    await client.shutdown()
    assert factory.process.terminated is True


@pytest.mark.asyncio
async def test_app_server_routes_server_request_to_callback() -> None:
    factory = FakeProcessFactory()
    requests: list[dict[str, object]] = []
    client = AppServerClient("codex", process_factory=factory, on_server_request=requests.append)
    await client.start()
    factory.process.feed(
        {
            "jsonrpc": "2.0",
            "id": "approval-1",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )
    await asyncio.sleep(0.05)

    assert requests[0]["id"] == "approval-1"
    await client.shutdown()


@pytest.mark.asyncio
async def test_app_server_marks_abnormal_stdout_exit() -> None:
    factory = FakeProcessFactory()
    client = AppServerClient("codex", process_factory=factory)
    await client.start()
    factory.process.returncode = 17
    factory.process.feed_eof()
    await asyncio.sleep(0.05)

    assert client.failed is True
    await client.shutdown()


@pytest.mark.asyncio
async def test_app_server_notifies_failure_handler() -> None:
    factory = FakeProcessFactory()
    failures: list[str] = []
    client = AppServerClient("codex", process_factory=factory, on_failure=failures.append)
    await client.start()
    factory.process.feed_eof()
    await asyncio.sleep(0.05)

    assert failures == ["JSON-RPC transport closed"]
    await client.shutdown()


@pytest.mark.asyncio
async def test_unsupported_server_request_does_not_stop_reader() -> None:
    factory = FakeProcessFactory()
    factory.process.responses["thread/list"] = {"data": []}
    client = AppServerClient("codex", process_factory=factory)

    async def reject_unknown(message: dict[str, object]) -> None:
        request_id = message["id"]
        assert isinstance(request_id, (int, str))
        await client.reject(request_id, -32601, "unsupported App Server request")

    client.set_handlers(on_server_request=reject_unknown)
    await client.start()
    factory.process.feed(
        {
            "jsonrpc": "2.0",
            "id": "unknown-1",
            "method": "future/request",
            "params": {"threadId": "thread", "turnId": "turn"},
        }
    )
    await asyncio.sleep(0.05)

    assert await client.request("thread/list", {}) == {"data": []}
    assert {
        "jsonrpc": "2.0",
        "id": "unknown-1",
        "error": {"code": -32601, "message": "unsupported App Server request"},
    } in factory.process.stdin.writes
    await client.shutdown()


@pytest.mark.asyncio
async def test_shutdown_kills_process_that_ignores_terminate() -> None:
    factory = FakeProcessFactory()
    factory.process.ignore_terminate = True
    client = AppServerClient("codex", process_factory=factory)
    await client.start()

    await client.shutdown(grace_seconds=0.1)

    assert factory.process.terminated is True
    assert factory.process.killed is True
    assert factory.process.returncode == -9


@pytest.mark.asyncio
async def test_shutdown_bounds_transport_close() -> None:
    factory = FakeProcessFactory()
    factory.process.stdin.ignore_wait_closed = True
    client = AppServerClient("codex", process_factory=factory)
    await client.start()

    await asyncio.wait_for(client.shutdown(grace_seconds=0.05), timeout=0.5)

    assert factory.process.terminated is True


@pytest.mark.asyncio
async def test_start_does_not_orphan_process_after_final_shutdown_timeout() -> None:
    factory = FakeProcessFactory()
    factory.process.ignore_terminate = True
    factory.process.ignore_kill = True
    client = AppServerClient("codex", process_factory=factory)
    await client.start()

    await client.shutdown(grace_seconds=0.01)

    with pytest.raises(RuntimeError, match="still running"):
        await client.start()
