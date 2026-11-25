from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import pytest

from codex_bridge.bridge import Bridge
from codex_bridge.final_answers import FinalAnswerStore
from codex_bridge.jsonrpc import JsonRpcClosedError, JsonRpcProtocolError, JsonRpcTransport
from codex_bridge.paths import AllowedPathPolicy
from codex_bridge.state import StateStore
from tests.bridge_test_fakes import FakeAppServer
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
async def test_async_notification_callback_is_awaited_before_next_notification() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()
    calls: list[str] = []

    async def on_notification(message: dict[str, object]) -> None:
        method = message.get("method")
        assert isinstance(method, str)
        calls.append(f"started:{method}")
        if method == "first":
            first_entered.set()
            await release_first.wait()
        calls.append(f"finished:{method}")
        if method == "second":
            second_finished.set()

    transport = JsonRpcTransport(reader, writer, on_notification=on_notification)
    task = asyncio.create_task(transport.run())
    reader.feed_data(
        b'{"jsonrpc":"2.0","method":"first","params":{}}\n'
        b'{"jsonrpc":"2.0","method":"second","params":{}}\n'
    )
    await asyncio.wait_for(first_entered.wait(), timeout=0.5)
    await asyncio.sleep(0.02)
    assert calls == ["started:first"]

    release_first.set()
    await asyncio.wait_for(second_finished.wait(), timeout=0.5)

    assert calls == [
        "started:first",
        "finished:first",
        "started:second",
        "finished:second",
    ]
    await transport.close()
    with pytest.raises(JsonRpcClosedError):
        await task


@pytest.mark.asyncio
async def test_final_answer_write_does_not_block_reader_and_read_waits_for_commit(
    tmp_path,
) -> None:
    thread_id = "thread-jsonrpc-final"
    turn_id = "turn-jsonrpc-final"
    answer = "Synthetic confirmed final answer for isolated ordering test."
    expected_digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    store = FinalAnswerStore(tmp_path / "final-answer-reader.sqlite3")
    bridge = Bridge(
        FakeAppServer(),
        StateStore(),
        AllowedPathPolicy((str(tmp_path),)),
        final_answer_store=store,
    )
    write_started = threading.Event()
    release_write = threading.Event()
    original_record = store.record_final_answer

    def blocked_record(thread: str, turn: str, item: str, text: object) -> None:
        write_started.set()
        release_write.wait(timeout=3)
        original_record(thread, turn, item, text)

    store.record_final_answer = blocked_record
    delta_processed = asyncio.Event()
    terminal_enqueued = asyncio.Event()
    notification_processed_during_read = asyncio.Event()

    async def on_notification(message: dict[str, object]) -> None:
        await bridge.handle_notification_async(message)
        if message.get("method") == "item/agentMessage/delta":
            params = message.get("params")
            if isinstance(params, dict) and params.get("delta") == "tail":
                delta_processed.set()
            elif isinstance(params, dict) and params.get("delta") == "after-read":
                notification_processed_during_read.set()
        elif message.get("method") == "turn/completed":
            terminal_enqueued.set()

    reader = asyncio.StreamReader()
    writer = FakeWriter()
    transport = JsonRpcTransport(reader, writer, on_notification=on_notification)
    reader_task = asyncio.create_task(transport.run())
    response_task = asyncio.create_task(transport.send_request("probe", {}))
    await asyncio.sleep(0)
    request_id = writer.writes[-1]["id"]

    final_item = {
        "jsonrpc": "2.0",
        "method": "item/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {
                "id": "item-jsonrpc-final",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": answer,
            },
        },
    }
    following_delta = {
        "jsonrpc": "2.0",
        "method": "item/agentMessage/delta",
        "params": {"threadId": thread_id, "turnId": turn_id, "delta": "tail"},
    }
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"response_processed": True},
    }
    terminal = {
        "jsonrpc": "2.0",
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "turn": {"id": turn_id, "status": "completed"},
        },
    }
    read_task: asyncio.Task[dict[str, object]] | None = None
    try:
        reader.feed_data(
            b"".join(
                json.dumps(message).encode("utf-8") + b"\n"
                for message in (final_item, following_delta, response)
            )
        )
        assert await asyncio.wait_for(asyncio.to_thread(write_started.wait, 1), timeout=1.2)
        await asyncio.sleep(0.05)
        reader_responsive_before_release = response_task.done() and delta_processed.is_set()

        reader.feed_data(json.dumps(terminal).encode("utf-8") + b"\n")
        await asyncio.sleep(0.05)
        terminal_processed_before_release = terminal_enqueued.is_set()
        read_task = asyncio.create_task(bridge.final_answer_async(thread_id, turn_id))
        reader.feed_data(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "delta": "after-read",
                    },
                }
            ).encode("utf-8")
            + b"\n"
        )
        await asyncio.sleep(0.05)
        read_waited_for_write = not read_task.done()
        notification_processed_during_read_before_release = (
            notification_processed_during_read.is_set()
        )

        release_write.set()
        response_result = await asyncio.wait_for(response_task, timeout=2)
        answer_result = await asyncio.wait_for(read_task, timeout=2)
    finally:
        release_write.set()
        await transport.close()
        pending: list[asyncio.Task[object]] = [reader_task, response_task]
        if read_task is not None:
            pending.append(read_task)  # type: ignore[arg-type]
        await asyncio.gather(*pending, return_exceptions=True)
        await bridge.drain_final_answer_writes(close=True)

    actual_text = answer_result.get("text")
    assert reader_responsive_before_release
    assert terminal_processed_before_release
    assert read_waited_for_write
    assert notification_processed_during_read_before_release
    assert response_result == {"response_processed": True}
    assert answer_result.get("status") == "retrieved"
    assert isinstance(actual_text, str)
    assert hashlib.sha256(actual_text.encode("utf-8")).hexdigest() == expected_digest


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


@pytest.mark.asyncio
async def test_reader_protocol_failure_closes_transport_and_rejects_followups() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()
    transport = JsonRpcTransport(reader, writer)
    task = asyncio.create_task(transport.run())
    request = asyncio.create_task(transport.send_request("never", {}))
    await asyncio.sleep(0)

    reader.feed_data(b"not-json\n")

    with pytest.raises(JsonRpcProtocolError, match="invalid JSON-RPC message"):
        await request
    with pytest.raises(JsonRpcProtocolError, match="invalid JSON-RPC message"):
        await task

    assert transport.closed is True
    with pytest.raises(JsonRpcClosedError):
        await transport.send_request("after-reader-failure", {})
    with pytest.raises(JsonRpcClosedError):
        await transport.send_notification("after-reader-failure", {})


@pytest.mark.asyncio
async def test_server_request_callback_failure_does_not_hang_pending_request() -> None:
    reader = asyncio.StreamReader()
    writer = FakeWriter()

    async def failing_callback(message: dict[str, object]) -> None:
        raise RuntimeError("callback failed")

    transport = JsonRpcTransport(reader, writer, on_server_request=failing_callback)
    task = asyncio.create_task(transport.run())
    request = asyncio.create_task(transport.send_request("after-callback", {}))
    await asyncio.sleep(0)
    request_id = writer.writes[-1]["id"]
    reader.feed_data(b'{"jsonrpc":"2.0","id":"server-1","method":"unknown","params":{}}\n')
    reader.feed_data(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"ok": True}}).encode() + b"\n"
    )

    assert await asyncio.wait_for(request, timeout=0.5) == {"ok": True}
    assert writer.writes[-1] == {
        "jsonrpc": "2.0",
        "id": "server-1",
        "error": {"code": -32603, "message": "server request callback failed"},
    }
    await transport.close()
    with pytest.raises(JsonRpcClosedError):
        await task
