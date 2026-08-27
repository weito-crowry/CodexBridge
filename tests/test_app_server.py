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
    assert factory.process.stdin.writes[-1] == {"jsonrpc": "2.0", "method": "initialized", "params": {}}
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
