from __future__ import annotations

import asyncio
from typing import Any

from .models import NormalizedState, PendingRequest, RequestId, ThreadState, TurnState

_MAX_MESSAGE_CHARS = 16_000
_MAX_DIFF_CHARS = 32_000
_MAX_EVENTS = 32


class StateStore:
    def __init__(self) -> None:
        self._threads: dict[str, ThreadState] = {}
        self._pending: dict[RequestId, PendingRequest] = {}
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

    def has_thread(self, thread_id: str) -> bool:
        return thread_id in self._threads

    def has_turn(self, thread_id: str, turn_id: str) -> bool:
        thread = self._threads.get(thread_id)
        return thread is not None and turn_id in thread.turns

    def latest_known_turn(self, thread_id: str) -> str | None:
        thread = self._threads.get(thread_id)
        if thread is None or not thread.turns:
            return None
        return next(reversed(thread.turns))

    def mark_loaded(self, thread_id: str, validated_cwd: str) -> None:
        thread = self._threads.setdefault(thread_id, ThreadState(thread_id=thread_id))
        thread.loaded = True
        thread.validated_cwd = validated_cwd

    def active_turns(self) -> tuple[tuple[str, str], ...]:
        active: list[tuple[str, str]] = []
        for thread_id, thread in self._threads.items():
            for turn_id, turn in thread.turns.items():
                if turn.state in {"in_progress", "needs_approval", "needs_input"}:
                    active.append((thread_id, turn_id))
        return tuple(active)

    def active_turn_for_thread(self, thread_id: str) -> str | None:
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        for turn_id, turn in reversed(tuple(thread.turns.items())):
            if turn.state in {"in_progress", "needs_approval", "needs_input"}:
                return turn_id
        return None

    def is_loaded(self, thread_id: str) -> bool:
        thread = self._threads.get(thread_id)
        return thread is not None and thread.loaded and thread.validated_cwd is not None

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
        for request_id, pending in tuple(self._pending.items()):
            if pending.thread_id == thread_id and pending.turn_id == turn_id:
                del self._pending[request_id]
        turn.pending_request_id = None
        turn.state = state
        turn.error = error[-2_000:] if error else None
        self._touch(turn, f"terminal:{state}")

    def put_pending_request(self, pending: PendingRequest) -> None:
        turn = self._turn(pending.thread_id, pending.turn_id)
        if turn.pending_request_id is not None and turn.pending_request_id != pending.request_id:
            self._pending.pop(turn.pending_request_id, None)
        self._pending[pending.request_id] = pending
        turn.pending_request_id = pending.request_id
        turn.state = "needs_input" if "UserInput" in pending.method else "needs_approval"
        self._touch(turn, f"pending:{turn.state}")

    def get_pending_request(self, request_id: RequestId) -> PendingRequest | None:
        return self._pending.get(request_id)

    def pop_pending_request(self, request_id: RequestId) -> PendingRequest | None:
        pending = self._pending.pop(request_id, None)
        if pending is not None:
            turn = self._turn(pending.thread_id, pending.turn_id)
            if turn.pending_request_id == request_id:
                turn.pending_request_id = None
                if turn.state not in {"completed", "interrupted", "failed"}:
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
                    condition.wait_for(
                        lambda: self._turn(thread_id, turn_id).generation != generation
                    ),
                    timeout=timeout,
                )
        except TimeoutError:
            return False
        return True

    def snapshot(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        turn = self._turn(thread_id, turn_id)
        return self._snapshot_turn(turn)

    def snapshot_if_known(self, thread_id: str, turn_id: str) -> dict[str, Any] | None:
        thread = self._threads.get(thread_id)
        if thread is None:
            return None
        turn = thread.turns.get(turn_id)
        if turn is None:
            return None
        return self._snapshot_turn(turn)

    def _snapshot_turn(self, turn: TurnState) -> dict[str, Any]:
        pending = (
            self._pending.get(turn.pending_request_id)
            if turn.pending_request_id is not None
            else None
        )
        return {
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "state": turn.state,
            "latest_agent_message": turn.latest_agent_message,
            "current_diff": turn.current_diff,
            "pending_request": pending,
            "error": turn.error,
        }
