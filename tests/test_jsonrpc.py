from __future__ import annotations

import asyncio

import pytest

from codex_bridge.jsonrpc import JsonRpcClosedError, JsonRpcTransport
from tests.fakes import FakeWriter


@pytest.mark.asyncio
async def test_jsonrpc_correlates_out_of_order_responses() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    transport = JsonRpcTransport(reader, writer)
    task = asyncio.create_task(transport.run())

    first = asyncio.create_task(transport.send_request("one", {}))
    second = asyncio.create_task(transport.send_request("two", {}))
    await asyncio.sleep(0)
    request_ids = [message["id"] for message in writer.writes]
    reader.feed_data(b'{"jsonrpc":"2.0","id":%d,"result":{"value":2}}\n' % request_ids[1])
    reader.feed_data(b'{"jsonrpc":"2.0","id":%d,"result":{"value":1}}\n' % request_ids[0])

    assert await first == {"value": 1}
    assert await second == {"value": 2}
    await transport.close()
    with pytest.raises(JsonRpcClosedError):
        await task


@pytest.mark.asyncio
async def test_notification_and_server_request_are_routed() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    notifications: list[dict[str, object]] = []
    requests: list[dict[str, object]] = []
    transport = JsonRpcTransport(
        reader,
        writer,
        on_notification=notifications.append,
        on_server_request=requests.append,
    )
    task = asyncio.create_task(transport.run())
    reader.feed_data(b'{"jsonrpc":"2.0","method":"turn/completed","params":{}}\n')
    reader.feed_data(
        b'{"jsonrpc":"2.0","id":9,"method":"item/tool/requestUserInput","params":{}}\n'
    )
    await asyncio.sleep(0.05)

    assert notifications == [{"jsonrpc": "2.0", "method": "turn/completed", "params": {}}]
    assert requests == [
        {"jsonrpc": "2.0", "id": 9, "method": "item/tool/requestUserInput", "params": {}}
    ]
    await transport.close()
    with pytest.raises(JsonRpcClosedError):
        await task


@pytest.mark.asyncio
async def test_eof_fails_pending_requests() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    transport = JsonRpcTransport(reader, writer)
    task = asyncio.create_task(transport.run())
    request = asyncio.create_task(transport.send_request("never", {}))
    await asyncio.sleep(0)
    reader.feed_eof()

    with pytest.raises(Exception, match="closed"):
        await request
    with pytest.raises(JsonRpcClosedError):
        await task
