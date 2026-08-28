from __future__ import annotations

import asyncio
import sys

import pytest

from codex_bridge.app_server import AppServerClient, _create_process
from codex_bridge.jsonrpc import JsonRpcClosedError, JsonRpcTransport
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
async def test_app_server_process_accepts_large_jsonrpc_lines_on_stdout_and_stderr() -> None:
    payload_size = 100 * 1024
    diagnostic_size = 100 * 1024
    script = (
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"payload = 'x' * {payload_size}\n"
        f"sys.stderr.write('diagnostic ' + ('y' * {diagnostic_size}) + '\\n')\n"
        "sys.stderr.flush()\n"
        "response = {'jsonrpc': '2.0', 'id': request['id'], "
        "'result': {'payload': payload}}\n"
        "sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    process = await _create_process(sys.executable, "-c", script)
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    transport = JsonRpcTransport(process.stdout, process.stdin)
    reader_task = asyncio.create_task(transport.run())
    stderr_task = asyncio.create_task(process.stderr.readline())

    try:
        result = await transport.send_request("thread/start", {})
        stderr_line = await stderr_task

        assert result == {"payload": "x" * payload_size}
        assert len(stderr_line) > 64 * 1024
    finally:
        await transport.close()
        await asyncio.gather(reader_task, return_exceptions=True)
        await asyncio.gather(stderr_task, return_exceptions=True)
        await process.wait()


@pytest.mark.asyncio
async def test_app_server_reader_failure_closes_transport_for_followup_requests() -> None:
    factory = FakeProcessFactory()
    failures: list[str] = []
    client = AppServerClient("codex", process_factory=factory, on_failure=failures.append)
    await client.start()
    factory.process.stdout.feed_data(b"not-json\n")

    assert client._reader_task is not None
    await client._reader_task

    assert client.failed is True
    assert failures == ["invalid JSON-RPC message"]
    assert client._transport is not None
    assert client._transport.closed is True
    with pytest.raises(JsonRpcClosedError):
        await client.request("after-reader-failure", {})
    await client.shutdown()
    assert factory.process.stdin.closed is True


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
