from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from .models import NormalizedState, PendingRequest, ThreadState, TurnState


_MAX_MESSAGE_CHARS = 16_000
_MAX_DIFF_CHARS = 32_000
_MAX_EVENTS = 32


class StateStore:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}
        self._pending: dict[str, PendingRequest] = {}
        self._conditions: dict[tuple[str, str], asyncio.Condition] = {}

    def _turn(self, thread_id: str, turn_id: str) -> TurnState:
        thread = self._threads.setdefault(thread_id, ThreadState(thread_id=thread_id))
        return thread.turns.setdefault(turn_id, TurnState(thread_id=thread_id, turn_id=turn_id))

    def _condition(self, thread_id: str, turn_id: str) -> asyncio.Condition:
        return self._conditions.setdefault((thread_id, turn_id), asyncio.Condition())

    def _touch(self, turn: TurnState, event: str) -> None:
        turn.generation += 1
        turn.recent_events.append(event)
        if len(turn.recent_events) > _MAX_EVENTS:
            del turn.recent_events[:-_MAX_EVENTS]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notify(turn.thread_id, turn.turn_id))

    async def _notify(self, thread_id: str, turn_id: str) -> None:
        condition = self._condition(thread_id, turn_id)
        async with condition:
            condition.notify_all()

    def ensure_turn(self, thread_id: str, turn_id: str) -> TurnState:
        return self._turn(thread_id, turn_id)

    def mark_loaded(self, thread_id: str) -> None:
        self._threads.setdefault(thread_id, ThreadState(thread_id=thread_id)).loaded = True

    def is_loaded(self, thread_id: str) -> bool:
        return self._threads.get(thread_id, ThreadState(thread_id=thread_id)).loaded

    def update_latest_message(self, thread_id: str, turn_id: str, message: str) -> None:
        turn = self._turn(thread_id, turn_id)
        turn.latest_agent_message = message[-_MAX_MESSAGE_CHARS:]
        self._touch(turn, "agent_message")

    def append_agent_message(self, thread_id: str, turn_id: str, delta: str) -> None:
        turn = self._turn(thread_id, turn_id)
        self.update_latest_message(thread_id, turn_id, turn.latest_agent_message + delta)

    def update_diff(self, thread_id: str, turn_id: str, diff: str) -> None:
        turn = self._turn(thread_id, turn_id)
        turn.current_diff = diff[-_MAX_DIFF_CHARS:]
        self._touch(turn, "diff")

    def set_terminal(
        self, thread_id: str, turn_id: str, state: NormalizedState, error: str | None = None
    ) -> None:
        turn = self._turn(thread_id, turn_id)
        turn.state = state
        turn.error = error[-2_000:] if error else None
        self._touch(turn, f"terminal:{state}")

    def put_pending_request(self, pending: PendingRequest) -> None:
        self._pending[pending.request_id] = pending
        turn = self._turn(pending.thread_id, pending.turn_id)
        turn.pending_request_id = pending.request_id
        turn.state = "needs_input" if "UserInput" in pending.method else "needs_approval"
        self._touch(turn, f"pending:{turn.state}")

    def get_pending_request(self, request_id: str) -> PendingRequest | None:
        return self._pending.get(request_id)

    def pop_pending_request(self, request_id: str) -> PendingRequest | None:
        pending = self._pending.pop(request_id, None)
        if pending is not None:
            turn = self._turn(pending.thread_id, pending.turn_id)
            if turn.pending_request_id == request_id:
                turn.pending_request_id = None
                turn.state = "in_progress"
                self._touch(turn, "pending:resolved")
        return pending

    async def wait_for_change(self, thread_id: str, turn_id: str, timeout: float) -> bool:
        turn = self._turn(thread_id, turn_id)
        condition = self._condition(thread_id, turn_id)
        generation = turn.generation
        try:
            async with condition:
                await asyncio.wait_for(
                    condition.wait_for(lambda: self._turn(thread_id, turn_id).generation != generation),
                    timeout=timeout,
                )
        except TimeoutError:
            return False
        return True

    def snapshot(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        turn = self._turn(thread_id, turn_id)
        pending = self._pending.get(turn.pending_request_id or "")
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "state": turn.state,
            "latest_agent_message": turn.latest_agent_message,
            "current_diff": turn.current_diff,
            "pending_request": pending,
            "error": turn.error,
        }
