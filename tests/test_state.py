from __future__ import annotations

import asyncio

import pytest

from codex_bridge.models import PendingRequest, UserInputQuestion
from codex_bridge.state import StateStore


@pytest.mark.asyncio
async def test_wait_for_change_does_not_cross_talk() -> None:
    store = StateStore()
    store.ensure_turn("thread-a", "turn-a")
    store.ensure_turn("thread-b", "turn-b")

    waiter = asyncio.create_task(store.wait_for_change("thread-a", "turn-a", 0.05))
    store.update_latest_message("thread-b", "turn-b", "other")

    assert await waiter is False


@pytest.mark.asyncio
async def test_wait_for_change_wakes_for_matching_turn() -> None:
    store = StateStore()
    store.ensure_turn("thread-a", "turn-a")

    waiter = asyncio.create_task(store.wait_for_change("thread-a", "turn-a", 1.0))
    await asyncio.sleep(0)
    store.update_latest_message("thread-a", "turn-a", "latest")

    assert await waiter is True
    assert store.snapshot("thread-a", "turn-a")["latest_agent_message"] == "latest"


def test_pending_request_is_scoped_to_one_request_id() -> None:
    store = StateStore()
    question = UserInputQuestion(header="Choice", id="choice", question="Pick one")
    pending = PendingRequest(
        request_id="request-a",
        method="item/tool/requestUserInput",
        thread_id="thread-a",
        turn_id="turn-a",
        questions=(question,),
    )
    store.put_pending_request(pending)

    assert store.get_pending_request("request-a") == pending
    assert store.get_pending_request("request-b") is None
    assert store.pop_pending_request("request-a") == pending
    assert store.get_pending_request("request-a") is None


def test_zero_pending_request_id_is_exposed_in_snapshot() -> None:
    store = StateStore()
    pending = PendingRequest(
        request_id=0,
        method="item/fileChange/requestApproval",
        thread_id="thread",
        turn_id="turn",
    )

    store.put_pending_request(pending)

    assert store.snapshot("thread", "turn")["pending_request"] == pending
