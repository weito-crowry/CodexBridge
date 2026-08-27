from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol

from .models import (
    ApprovalDecision,
    NormalizedState,
    PendingRequest,
    RequestId,
    UserInputAnswer,
    UserInputQuestion,
)
from .paths import AllowedPathPolicy
from .state import StateStore


class AppServerPort(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def respond(self, request_id: RequestId, result: dict[str, Any]) -> None: ...


class BridgeError(RuntimeError):
    """Raised when a native App Server response cannot be used safely."""


_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
_USER_INPUT_METHOD = "item/tool/requestUserInput"
_APPROVAL_DECISIONS = {"accept", "acceptForSession", "decline", "cancel"}
_TERMINAL_STATES = {"completed", "interrupted", "failed"}


def _native_state(value: object) -> NormalizedState:
    if value == "completed":
        return "completed"
    if value == "interrupted":
        return "interrupted"
    if value == "failed":
        return "failed"
    return "in_progress"


def _bounded_text(value: object, limit: int = 2_000) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value[-limit:]
    if isinstance(value, Mapping):
        message = value.get("message")
        if isinstance(message, str):
            return message[-limit:]
    return str(value)[-limit:]


def _sanitize(value: object, key: str = "", depth: int = 0) -> object:
    lowered = key.casefold()
    if any(term in lowered for term in ("raw", "encrypted", "chain_of_thought")):
        return None
    if depth > 5:
        return "<omitted>"
    if isinstance(value, str):
        return value[-16_000:]
    if isinstance(value, list):
        return [_sanitize(item, key, depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            child_key: sanitized
            for child_key, child_value in value.items()
            if (sanitized := _sanitize(child_value, str(child_key), depth + 1)) is not None
        }
    return value


class Bridge:
    def __init__(
        self,
        app_server: AppServerPort,
        state: StateStore,
        path_policy: AllowedPathPolicy,
        *,
        wait_default_seconds: float = 18.0,
        wait_max_seconds: float = 30.0,
    ) -> None:
        self._app_server = app_server
        self._state = state
        self._path_policy = path_policy
        self._wait_default_seconds = wait_default_seconds
        self._wait_max_seconds = min(wait_max_seconds, 30.0)

    async def _start_turn(self, thread_id: str, prompt: str) -> dict[str, Any]:
        response = await self._app_server.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
        )
        turn = response.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise BridgeError("turn/start did not return a turn id")
        turn_id = turn["id"]
        self._state.ensure_turn(thread_id, turn_id)
        status = _native_state(turn.get("status"))
        if status in _TERMINAL_STATES:
            self._state.set_terminal(thread_id, turn_id, status, _bounded_text(turn.get("error")))
        return self._public_snapshot(thread_id, turn_id)

    async def start(self, cwd: str, prompt: str) -> dict[str, Any]:
        canonical_cwd = self._path_policy.validate_cwd(cwd)
        response = await self._app_server.request("thread/start", {"cwd": canonical_cwd})
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise BridgeError("thread/start did not return a thread id")
        thread_id = thread["id"]
        self._state.mark_loaded(thread_id)
        return await self._start_turn(thread_id, prompt)

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]:
        if not self._state.is_loaded(thread_id):
            response = await self._app_server.request("thread/resume", {"threadId": thread_id})
            thread = response.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise BridgeError("thread/resume returned a mismatched thread id")
            self._state.mark_loaded(thread_id)
        return await self._start_turn(thread_id, prompt)

    async def wait(
        self, thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        timeout = self._wait_default_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("timeout_seconds must not be negative")
        timeout = min(timeout, self._wait_max_seconds)
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self._public_snapshot(thread_id, turn_id)
            if snapshot["state"] != "in_progress" or snapshot["pending_request"] is not None:
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not await self._state.wait_for_change(thread_id, turn_id, remaining):
                return self._public_snapshot(thread_id, turn_id)

    async def steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        await self._app_server.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        self._state.ensure_turn(thread_id, turn_id)
        return self._public_snapshot(thread_id, turn_id)

    async def approve(self, request_id: RequestId, decision: ApprovalDecision) -> dict[str, Any]:
        if decision not in _APPROVAL_DECISIONS:
            raise ValueError("unsupported approval decision")
        pending = self._state.get_pending_request(request_id)
        if pending is None or pending.method not in _APPROVAL_METHODS:
            raise ValueError("unknown approval request")
        await self._app_server.respond(request_id, {"decision": decision})
        self._state.pop_pending_request(request_id)
        return self._public_snapshot(pending.thread_id, pending.turn_id)

    async def answer_user_input(
        self, request_id: RequestId, answers: Mapping[str, list[str] | tuple[str, ...]]
    ) -> dict[str, Any]:
        pending = self._state.get_pending_request(request_id)
        if pending is None or pending.method != _USER_INPUT_METHOD:
            raise ValueError("unknown user-input request")
        expected = {question.id for question in pending.questions}
        actual = set(answers)
        if actual != expected:
            raise ValueError("answers must match pending question ids")
        normalized: dict[str, UserInputAnswer] = {}
        for question_id, values in answers.items():
            if not isinstance(values, (list, tuple)) or not all(isinstance(value, str) for value in values):
                raise ValueError("each answer must be a list of strings")
            normalized[question_id] = UserInputAnswer(tuple(values))
        payload = {"answers": {key: {"answers": list(value.answers)} for key, value in normalized.items()}}
        await self._app_server.respond(request_id, payload)
        self._state.pop_pending_request(request_id)
        return self._public_snapshot(pending.thread_id, pending.turn_id)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        await self._app_server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        self._state.ensure_turn(thread_id, turn_id)
        return self._public_snapshot(thread_id, turn_id)

    async def interrupt_active_turns(self) -> None:
        for thread_id, turn_id in self._state.active_turns():
            try:
                await self.interrupt(thread_id, turn_id)
            except Exception:
                continue

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if thread_id is not None:
            response = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": include_history}
            )
            return {"thread": _sanitize(response.get("thread", {}))}
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._app_server.request("thread/list", params)
        return {
            "threads": _sanitize(response.get("data", [])),
            "next_cursor": response.get("nextCursor"),
            "backwards_cursor": response.get("backwardsCursor"),
        }

    async def handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            raise BridgeError("server request id is invalid")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise BridgeError("server request is malformed")
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            raise BridgeError("server request is missing thread or turn id")
        if method in _APPROVAL_METHODS:
            self._state.put_pending_request(
                PendingRequest(
                    request_id=request_id,
                    method=method,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=params.get("itemId") if isinstance(params.get("itemId"), str) else None,
                    summary=_bounded_text(params.get("reason") or params.get("command")),
                )
            )
            return
        if method == _USER_INPUT_METHOD:
            raw_questions = params.get("questions")
            if not isinstance(raw_questions, list):
                raise BridgeError("user-input request questions are malformed")
            questions: list[UserInputQuestion] = []
            for raw in raw_questions:
                if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in ("header", "id", "question")):
                    raise BridgeError("user-input question is malformed")
                questions.append(
                    UserInputQuestion(header=raw["header"], id=raw["id"], question=raw["question"])
                )
            self._state.put_pending_request(
                PendingRequest(
                    request_id=request_id,
                    method=method,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=params.get("itemId") if isinstance(params.get("itemId"), str) else None,
                    questions=tuple(questions),
                )
            )
            return
        raise BridgeError(f"unsupported App Server request method: {method}")

    def handle_notification(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if method == "item/agentMessage/delta":
            if isinstance(thread_id, str) and isinstance(turn_id, str) and isinstance(params.get("delta"), str):
                self._state.append_agent_message(thread_id, turn_id, params["delta"])
            return
        if method == "turn/diff/updated":
            if isinstance(thread_id, str) and isinstance(turn_id, str) and isinstance(params.get("diff"), str):
                self._state.update_diff(thread_id, turn_id, params["diff"])
            return
        if method == "turn/started":
            turn = params.get("turn")
            if isinstance(thread_id, str) and isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self._state.ensure_turn(thread_id, turn["id"])
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(thread_id, str) and isinstance(turn, dict) and isinstance(turn.get("id"), str):
                status = _native_state(turn.get("status"))
                self._state.set_terminal(thread_id, turn["id"], status, _bounded_text(turn.get("error")))
            return
        if method == "error":
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                self._state.set_terminal(thread_id, turn_id, "failed", _bounded_text(params.get("message")))

    def _public_snapshot(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        snapshot = self._state.snapshot(thread_id, turn_id)
        pending = snapshot["pending_request"]
        if isinstance(pending, PendingRequest):
            snapshot["pending_request"] = {
                "request_id": pending.request_id,
                "method": pending.method,
                "thread_id": pending.thread_id,
                "turn_id": pending.turn_id,
                "item_id": pending.item_id,
                "summary": pending.summary,
                "questions": [
                    {"header": q.header, "id": q.id, "question": q.question} for q in pending.questions
                ],
            }
        return snapshot
