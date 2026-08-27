from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Protocol

from .logging_utils import log_event
from .models import (
    ApprovalDecision,
    NormalizedState,
    PendingRequest,
    PermissionGrantScope,
    PermissionRequestDetails,
    RequestId,
    UserInputAnswer,
    UserInputQuestion,
)
from .paths import AllowedPathPolicy
from .state import StateStore


class AppServerPort(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def respond(self, request_id: RequestId, result: dict[str, Any]) -> None: ...

    async def reject(self, request_id: RequestId, code: int, message: str) -> None: ...


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
_PERMISSION_METHOD = "item/permissions/requestApproval"
_PERMISSION_TEXT_LIMIT = 16_000


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


def _permission_path(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise BridgeError("permission path is malformed")
    path_type = value["type"]
    if path_type == "path" and isinstance(value.get("path"), str):
        path = value["path"]
        if len(path) > _PERMISSION_TEXT_LIMIT:
            raise BridgeError("permission path is too long")
        return {"type": "path", "path": path}
    if path_type == "glob_pattern" and isinstance(value.get("pattern"), str):
        pattern = value["pattern"]
        if len(pattern) > _PERMISSION_TEXT_LIMIT:
            raise BridgeError("permission glob pattern is too long")
        return {"type": "glob_pattern", "pattern": pattern}
    if path_type == "special" and isinstance(value.get("value"), dict):
        special = value["value"]
        kind = special.get("kind")
        if not isinstance(kind, str) or kind not in {
            "root",
            "minimal",
            "project_roots",
            "tmpdir",
            "slash_tmp",
            "unknown",
        }:
            raise BridgeError("permission special path is malformed")
        result: dict[str, Any] = {"type": "special", "value": {"kind": kind}}
        if kind == "unknown":
            path = special.get("path")
            if not isinstance(path, str) or len(path) > _PERMISSION_TEXT_LIMIT:
                raise BridgeError("permission special path is malformed")
            result["value"]["path"] = path
        if "subpath" in special:
            subpath = special["subpath"]
            if subpath is not None and (
                not isinstance(subpath, str) or len(subpath) > _PERMISSION_TEXT_LIMIT
            ):
                raise BridgeError("permission special path is malformed")
            result["value"]["subpath"] = subpath
        return result
    raise BridgeError("permission path is malformed")


def _permission_file_system(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BridgeError("file-system permission profile is malformed")
    result: dict[str, Any] = {}
    if "entries" in value:
        entries = value["entries"]
        if entries is None:
            result["entries"] = None
        elif not isinstance(entries, list) or len(entries) > 100:
            raise BridgeError("file-system permission entries are malformed")
        else:
            normalized_entries: list[dict[str, Any]] = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("access") not in {
                    "read",
                    "write",
                    "deny",
                }:
                    raise BridgeError("file-system permission entry is malformed")
                normalized_entries.append(
                    {"access": entry["access"], "path": _permission_path(entry.get("path"))}
                )
            result["entries"] = normalized_entries
    for key in ("read", "write"):
        if key in value:
            paths = value[key]
            if paths is None:
                result[key] = None
            elif (
                not isinstance(paths, list)
                or len(paths) > 100
                or not all(isinstance(path, str) for path in paths)
            ):
                raise BridgeError("file-system permission paths are malformed")
            else:
                if any(len(path) > _PERMISSION_TEXT_LIMIT for path in paths):
                    raise BridgeError("file-system permission path is too long")
                result[key] = list(paths)
    if "globScanMaxDepth" in value:
        depth = value["globScanMaxDepth"]
        if depth is None:
            result["globScanMaxDepth"] = None
        elif not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
            raise BridgeError("file-system permission depth is malformed")
        else:
            result["globScanMaxDepth"] = depth
    return result


def _permission_network(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BridgeError("network permission profile is malformed")
    result: dict[str, Any] = {}
    if "enabled" in value:
        enabled = value["enabled"]
        if enabled is not None and not isinstance(enabled, bool):
            raise BridgeError("network permission profile is malformed")
        result["enabled"] = enabled
    return result


def _permission_profile(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("permission profile is malformed")
    result: dict[str, Any] = {}
    if "fileSystem" in value:
        result["fileSystem"] = _permission_file_system(value["fileSystem"])
    if "network" in value:
        result["network"] = _permission_network(value["network"])
    return result


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

    @staticmethod
    def _thread_from_response(response: dict[str, Any]) -> dict[str, Any]:
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise BridgeError("thread response did not return thread metadata")
        return thread

    def _validate_thread_metadata(self, thread: dict[str, Any], thread_id: str) -> str:
        if thread.get("id") != thread_id:
            raise BridgeError("thread response returned a mismatched thread id")
        cwd = thread.get("cwd")
        if not isinstance(cwd, str):
            raise BridgeError("thread response did not return a cwd")
        return self._path_policy.validate_cwd(cwd)

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
        log_event("turn.start", thread_id=thread_id, turn_id=turn_id)
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
        self._state.mark_loaded(thread_id, canonical_cwd)
        log_event("thread.start", thread_id=thread_id)
        return await self._start_turn(thread_id, prompt)

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]:
        if not self._state.is_loaded(thread_id):
            metadata = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = self._thread_from_response(metadata)
            validated_cwd = self._validate_thread_metadata(thread, thread_id)
            response = await self._app_server.request("thread/resume", {"threadId": thread_id})
            resumed_thread = self._thread_from_response(response)
            if resumed_thread.get("id") != thread_id:
                raise BridgeError("thread/resume returned a mismatched thread id")
            resumed_cwd = response.get("cwd")
            if not isinstance(resumed_cwd, str):
                raise BridgeError("thread/resume returned a malformed cwd")
            if self._path_policy.validate_cwd(resumed_cwd) != validated_cwd:
                raise BridgeError("thread/resume returned a mismatched cwd")
            self._state.mark_loaded(thread_id, validated_cwd)
            log_event("thread.resume", thread_id=thread_id)
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
            if remaining <= 0 or not await self._state.wait_for_change(
                thread_id, turn_id, remaining
            ):
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
        payload: dict[str, Any]
        if pending.permission is None:
            payload = {"decision": decision}
        elif decision in {"accept", "acceptForSession"}:
            scope: PermissionGrantScope = "session" if decision == "acceptForSession" else "turn"
            payload = {
                "permissions": deepcopy(pending.permission.requested_permissions),
                "scope": scope,
            }
        else:
            payload = {
                "permissions": {"fileSystem": None, "network": None},
                "scope": "turn",
            }
        await self._app_server.respond(request_id, payload)
        self._state.pop_pending_request(request_id)
        log_event("approval.resolved", request_id=request_id, decision=decision)
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
            if not isinstance(values, (list, tuple)) or not all(
                isinstance(value, str) for value in values
            ):
                raise ValueError("each answer must be a list of strings")
            normalized[question_id] = UserInputAnswer(tuple(values))
        payload = {
            "answers": {key: {"answers": list(value.answers)} for key, value in normalized.items()}
        }
        await self._app_server.respond(request_id, payload)
        self._state.pop_pending_request(request_id)
        log_event("user_input.resolved", request_id=request_id)
        return self._public_snapshot(pending.thread_id, pending.turn_id)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        await self._app_server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        self._state.ensure_turn(thread_id, turn_id)
        log_event("turn.interrupt", thread_id=thread_id, turn_id=turn_id)
        return self._public_snapshot(thread_id, turn_id)

    async def interrupt_active_turns(self, wait_seconds: float = 0.5) -> None:
        active = self._state.active_turns()
        deadline = time.monotonic() + max(0.0, wait_seconds)
        remaining = deadline - time.monotonic()
        if remaining > 0 and active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(self.interrupt(thread_id, turn_id) for thread_id, turn_id in active),
                        return_exceptions=True,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                return
        while active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            changes = await asyncio.gather(
                *(
                    self._state.wait_for_change(thread_id, turn_id, remaining)
                    for thread_id, turn_id in active
                )
            )
            if not any(changes):
                return
            active = self._state.active_turns()

    def handle_app_server_failure(self, error: str) -> None:
        for thread_id, turn_id in self._state.active_turns():
            self._state.set_terminal(thread_id, turn_id, "failed", error)
            log_event("turn.terminal", thread_id=thread_id, turn_id=turn_id, state="failed")

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
            metadata = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = self._thread_from_response(metadata)
            validated_cwd = self._validate_thread_metadata(thread, thread_id)
            response = metadata
            if include_history:
                response = await self._app_server.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": True}
                )
                history_thread = self._thread_from_response(response)
                if self._validate_thread_metadata(history_thread, thread_id) != validated_cwd:
                    raise BridgeError("thread history returned a mismatched cwd")
            return {"thread": _sanitize(response.get("thread", {}))}
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._app_server.request("thread/list", params)
        rows = response.get("data")
        visible: list[object] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                try:
                    self._validate_thread_metadata(row, row["id"])
                except (BridgeError, ValueError):
                    continue
                visible.append(_sanitize(row))
        return {
            "threads": visible,
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
            await self._app_server.reject(request_id, -32602, "malformed App Server request")
            return
        if method == "mcpServer/elicitation/request":
            if not isinstance(params.get("threadId"), str):
                await self._app_server.reject(request_id, -32602, "malformed elicitation request")
                return
            await self._app_server.respond(request_id, {"action": "cancel"})
            log_event("elicitation.cancelled", request_id=request_id)
            return
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            await self._app_server.reject(
                request_id, -32602, "server request is missing thread or turn id"
            )
            return
        if method in _APPROVAL_METHODS:
            permission: PermissionRequestDetails | None = None
            summary = _bounded_text(params.get("reason") or params.get("command"))
            if method == _PERMISSION_METHOD:
                try:
                    cwd = params.get("cwd")
                    environment_id = params.get("environmentId")
                    reason = params.get("reason")
                    if not isinstance(cwd, str):
                        raise BridgeError("permission request is missing cwd")
                    if environment_id is not None and not isinstance(environment_id, str):
                        raise BridgeError("permission request environment id is malformed")
                    if reason is not None and not isinstance(reason, str):
                        raise BridgeError("permission request reason is malformed")
                    permission = PermissionRequestDetails(
                        requested_permissions=_permission_profile(params.get("permissions")),
                        cwd=self._path_policy.validate_cwd(cwd),
                        environment_id=environment_id,
                        reason=_bounded_text(reason),
                    )
                    summary = permission.reason
                except (BridgeError, ValueError):
                    await self._app_server.reject(
                        request_id, -32602, "malformed or disallowed permission request"
                    )
                    return
            self._state.put_pending_request(
                PendingRequest(
                    request_id=request_id,
                    method=method,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=params.get("itemId") if isinstance(params.get("itemId"), str) else None,
                    summary=summary,
                    permission=permission,
                )
            )
            log_event(
                "approval.request",
                request_id=request_id,
                method=method,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            return
        if method == _USER_INPUT_METHOD:
            raw_questions = params.get("questions")
            if not isinstance(raw_questions, list):
                raise BridgeError("user-input request questions are malformed")
            questions: list[UserInputQuestion] = []
            for raw in raw_questions:
                if not isinstance(raw, dict) or not all(
                    isinstance(raw.get(key), str) for key in ("header", "id", "question")
                ):
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
            log_event(
                "user_input.request",
                request_id=request_id,
                method=method,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            return
        await self._app_server.reject(request_id, -32601, "unsupported App Server request")

    def handle_notification(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return
        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            thread_id = params.get("threadId")
            if (
                isinstance(request_id, (int, str))
                and not isinstance(request_id, bool)
                and isinstance(thread_id, str)
            ):
                pending = self._state.get_pending_request(request_id)
                if pending is not None and pending.thread_id == thread_id:
                    self._state.pop_pending_request(request_id)
                    log_event("server_request.resolved", request_id=request_id)
            return
        thread_id, turn_id = params.get("threadId"), params.get("turnId")
        if method == "item/agentMessage/delta":
            if (
                isinstance(thread_id, str)
                and isinstance(turn_id, str)
                and isinstance(params.get("delta"), str)
            ):
                self._state.append_agent_message(thread_id, turn_id, params["delta"])
            return
        if method == "turn/diff/updated":
            if (
                isinstance(thread_id, str)
                and isinstance(turn_id, str)
                and isinstance(params.get("diff"), str)
            ):
                self._state.update_diff(thread_id, turn_id, params["diff"])
            return
        if method == "turn/started":
            turn = params.get("turn")
            if (
                isinstance(thread_id, str)
                and isinstance(turn, dict)
                and isinstance(turn.get("id"), str)
            ):
                self._state.ensure_turn(thread_id, turn["id"])
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if (
                isinstance(thread_id, str)
                and isinstance(turn, dict)
                and isinstance(turn.get("id"), str)
            ):
                status = _native_state(turn.get("status"))
                self._state.set_terminal(
                    thread_id, turn["id"], status, _bounded_text(turn.get("error"))
                )
                log_event("turn.terminal", thread_id=thread_id, turn_id=turn["id"], state=status)
            return
        if method == "error":
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                self._state.set_terminal(
                    thread_id, turn_id, "failed", _bounded_text(params.get("message"))
                )

    def _public_snapshot(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        snapshot = self._state.snapshot(thread_id, turn_id)
        pending = snapshot["pending_request"]
        if isinstance(pending, PendingRequest):
            public_pending: dict[str, Any] = {
                "request_id": pending.request_id,
                "method": pending.method,
                "thread_id": pending.thread_id,
                "turn_id": pending.turn_id,
                "item_id": pending.item_id,
                "summary": pending.summary,
                "questions": [
                    {"header": q.header, "id": q.id, "question": q.question}
                    for q in pending.questions
                ],
            }
            if pending.permission is not None:
                public_pending["permission"] = {
                    "requested_permissions": deepcopy(pending.permission.requested_permissions),
                    "cwd": pending.permission.cwd,
                    "environment_id": pending.permission.environment_id,
                    "reason": pending.permission.reason,
                    "allowed_scopes": list(pending.permission.allowed_scopes),
                }
            snapshot["pending_request"] = public_pending
        return snapshot
