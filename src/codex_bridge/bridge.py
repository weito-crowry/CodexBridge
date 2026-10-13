from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from .account_auth import AccountAuthService
from .activity import ActivityStatus, ActivityStore, ActivityType
from .config import WAIT_HARD_MAX_SECONDS
from .final_answers import (
    FINAL_ANSWER_SOURCE,
    MAX_FINAL_ANSWER_UTF16_CODE_UNITS,
    FinalAnswerStore,
    FinalAnswerStoreError,
    is_valid_final_answer_identifier,
)
from .history import (
    HistoryValidationError,
    project_items_response,
    project_legacy_items_response,
    project_legacy_thread,
    project_turns_response,
    validate_cursor,
    validate_history_query,
    validate_legacy_cursor,
)
from .history_metadata_projection import attach_turn_execution_metadata
from .item_lifecycle import ItemLifecycleStore
from .jsonrpc import JsonRpcClosedError, JsonRpcRemoteError
from .logging_utils import log_event
from .models import (
    ApprovalDecision,
    ApprovalsReviewer,
    NormalizedState,
    PendingRequest,
    PermissionGrantScope,
    PermissionRequestDetails,
    RequestId,
    SandboxMode,
    UserInputAnswer,
    UserInputQuestion,
    validate_approvals_reviewer,
    validate_sandbox_mode,
)
from .paths import AllowedPathPolicy
from .setup_capabilities import normalize_capabilities
from .state import StateStore
from .thread_observations import ACTIVE, ThreadObservations
from .thread_titles import derive_thread_title, normalize_explicit_title
from .turn_input import build_turn_input
from .turn_outcome import PersistedTurnReader, TurnOutcomeHints


class AppServerPort(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...

    async def respond(self, request_id: RequestId, result: dict[str, Any]) -> None: ...

    async def reject(self, request_id: RequestId, code: int, message: str) -> None: ...


class BridgeError(RuntimeError):
    """Raised when a native App Server response cannot be used safely."""


class ApprovalConflictError(ValueError):
    """Raised when an approval request is no longer pending."""


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
_ACTIVITY_LIMIT_MIN = 1
_ACTIVITY_LIMIT_MAX = 100
_FINAL_ANSWER_WRITE_QUEUE_CAPACITY = 4
_LOCAL_THREAD_CATALOG_PATH = Path.home() / ".codex" / "sqlite" / "codex-dev.db"
_LOCAL_THREAD_TITLE_MAX_CHARS = 200
_MAX_EPOCH_MILLISECONDS = 253_402_300_799_999


@dataclass(frozen=True, slots=True)
class _FinalAnswerWrite:
    kind: Literal["item", "terminal"]
    thread_id: str
    turn_id: str
    value: object
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class _FinalAnswerReadBarrier:
    completed: asyncio.Future[None]


def _safe_item_timestamp(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_EPOCH_MILLISECONDS
    ):
        return None
    return value


def _notification_item_timestamp(
    message: Mapping[str, Any], params: Mapping[str, Any], field: str
) -> int | None:
    for value in (params.get(field), params.get("emittedAtMs"), message.get("emittedAtMs")):
        timestamp = _safe_item_timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _read_local_thread_titles(thread_ids: list[str]) -> dict[str, str]:
    if not thread_ids:
        return {}
    placeholders = ", ".join("?" for _ in thread_ids)
    query = (
        "SELECT thread_id, display_title FROM local_thread_catalog "
        f"WHERE host_id = ? AND thread_id IN ({placeholders})"
    )
    try:
        connection = sqlite3.connect(
            f"{_LOCAL_THREAD_CATALOG_PATH.as_uri()}?mode=ro", uri=True, timeout=0.05
        )
        try:
            rows = connection.execute(query, ("local", *thread_ids)).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return {}
    return {
        thread_id: title
        for thread_id, title in rows
        if isinstance(thread_id, str) and isinstance(title, str) and title.strip()
    }


def _usable_local_display_title(value: object, preview: object) -> str | None:
    if not isinstance(value, str) or not isinstance(preview, str) or not preview.strip():
        return None
    title = value.strip()
    if not title or len(title) > _LOCAL_THREAD_TITLE_MAX_CHARS or "\n" in title or "\r" in title:
        return None
    normalized_title = " ".join(title.rstrip("…").split()).casefold()
    normalized_preview = " ".join(preview.split()).casefold()
    if not normalized_title or normalized_preview.startswith(normalized_title):
        return None
    return title


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


def _sanitize_thread_history(thread: object) -> object:
    sanitized = _sanitize(thread)
    if not isinstance(sanitized, dict):
        return sanitized
    turns = sanitized.get("turns")
    if not isinstance(turns, list):
        return sanitized
    sanitized["turns"] = [
        {
            **turn,
            "items": [
                item
                for item in items
                if not (isinstance(item, dict) and item.get("type") == "reasoning")
            ],
        }
        if isinstance(turn, dict) and isinstance(items := turn.get("items"), list)
        else turn
        for turn in turns
    ]
    return sanitized


def _is_running_observation(value: Mapping[str, Any]) -> bool:
    state = value.get("state")
    evidence = value.get("evidence")
    if evidence == "live":
        return state in ACTIVE
    return state == "estimated_running" and evidence == "inferred"


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
        activity_store: ActivityStore | None = None,
        final_answer_store: FinalAnswerStore | None = None,
        wait_default_seconds: float = 50.0,
        wait_max_seconds: float = WAIT_HARD_MAX_SECONDS,
    ) -> None:
        self._app_server = app_server
        self._account_auth = AccountAuthService(app_server.request)
        self._state = state
        self._path_policy = path_policy
        self._activities = activity_store if activity_store is not None else ActivityStore()
        self._final_answer_store = final_answer_store
        self._item_lifecycle = ItemLifecycleStore()
        self._wait_default_seconds = wait_default_seconds
        self._wait_max_seconds = min(wait_max_seconds, WAIT_HARD_MAX_SECONDS)
        self._approval_lock = asyncio.Lock()
        self._final_answer_lock = asyncio.Lock()
        self._final_answer_write_queue: (
            asyncio.Queue[_FinalAnswerWrite | _FinalAnswerReadBarrier | None] | None
        ) = None
        self._final_answer_writer_task: asyncio.Task[None] | None = None
        self._final_answer_enqueue_tasks: set[asyncio.Task[None]] = set()
        self._final_answer_close_task: asyncio.Task[None] | None = None
        self._final_answer_writer_closing = False
        self._final_answer_writer_closed = False
        self._turn_outcomes = TurnOutcomeHints()
        self._persisted_reader = PersistedTurnReader()
        self.observations = ThreadObservations(
            state, self._activities, self._turn_outcomes, self._persisted_reader
        )
        self._event_publisher: Callable[[str, str, str, str], None] | None = None
        self._event_target_id: str | None = None

    def set_event_publisher(
        self,
        publisher: Callable[[str, str, str, str], None] | None,
        *,
        target_id: str | None = None,
    ) -> None:
        self._event_publisher = publisher
        self._event_target_id = target_id if publisher is not None else None

    def _set_terminal(
        self,
        thread_id: str,
        turn_id: str,
        status: NormalizedState,
        error: str | None = None,
    ) -> bool:
        transitioned = self._state.set_terminal(thread_id, turn_id, status, error)
        publisher = self._event_publisher
        target_id = self._event_target_id
        if transitioned and publisher is not None and target_id is not None:
            try:
                publisher(target_id, thread_id, turn_id, status)
            except Exception as exc:
                log_event("mcp_events.publish_failed", error_type=exc.__class__.__name__)
        return transitioned

    @staticmethod
    def _activity_status(value: object) -> ActivityStatus:
        if not isinstance(value, str):
            return "in_progress"
        statuses: dict[str, ActivityStatus] = {
            "inProgress": "in_progress",
            "in_progress": "in_progress",
            "completed": "completed",
            "failed": "failed",
            "declined": "declined",
            "interrupted": "interrupted",
        }
        return statuses.get(value, "in_progress")

    def _record_activity(
        self,
        *,
        thread_id: str,
        turn_id: str | None,
        type: ActivityType,
        status: ActivityStatus,
        item_id: str | None = None,
        summary: str | None = None,
        details: dict[str, str | int | bool | None | list[str]] | None = None,
    ) -> None:
        try:
            self._activities.add(
                thread_id=thread_id,
                turn_id=turn_id,
                type=type,
                status=status,
                item_id=item_id,
                summary=summary,
                details=details,
            )
        except Exception as exc:
            log_event("activity.record_error", error_type=exc.__class__.__name__)

    def _has_activity_type(self, thread_id: str, turn_id: str, activity_type: ActivityType) -> bool:
        try:
            latest = self._activities.latest(thread_id, turn_id)
            return latest is not None and latest.type == activity_type
        except Exception as exc:
            log_event("activity.lookup_error", error_type=exc.__class__.__name__)
            return False

    def _safe_file_paths(self, item: dict[str, Any]) -> list[str]:
        changes = item.get("changes")
        if not isinstance(changes, list):
            return []
        paths: list[str] = []
        for change in changes[:100]:
            if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                continue
            path = self._path_policy.safe_relative_path(change["path"])
            if path != "<path omitted>" and path not in paths:
                paths.append(path)
        return paths

    def _record_item_activity(
        self,
        method: str,
        thread_id: str,
        turn_id: str,
        item: dict[str, Any],
        *,
        timestamp_ms: int | None = None,
        persist_final_answer: bool = True,
    ) -> None:
        item_id = item.get("id") if isinstance(item.get("id"), str) else None
        item_type = item.get("type")
        if item_type == "commandExecution":
            if method == "item/completed" and item_id is not None:
                aggregated_output = item.get("aggregatedOutput")
                has_output_delta = any(
                    activity.type == "command_output" and activity.item_id == item_id
                    for activity in self._activities.get_recent(thread_id, turn_id, limit=500)
                )
                if (
                    isinstance(aggregated_output, str)
                    and aggregated_output
                    and not has_output_delta
                ):
                    self._record_activity(
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_id=item_id,
                        type="command_output",
                        status="completed",
                        summary=aggregated_output,
                        details={
                            "output_source": "completed_item",
                            "output_truncated": len(aggregated_output) > 2_000,
                        },
                    )
            activity_type: ActivityType = (
                "command_started" if method == "item/started" else "command_completed"
            )
            details: dict[str, str | int | bool | None | list[str]] = {}
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                details["exit_code"] = exit_code
            cwd = item.get("cwd")
            if isinstance(cwd, str):
                try:
                    details["cwd"] = self._path_policy.validate_cwd(cwd)
                except ValueError:
                    details["cwd"] = "<path omitted>"
            process_id = item.get("processId")
            if isinstance(process_id, str):
                details["process_id"] = process_id
            duration_ms = item.get("durationMs")
            if isinstance(duration_ms, int) and not isinstance(duration_ms, bool):
                details["duration_ms"] = duration_ms
            if timestamp_ms is not None:
                timestamp_key = "started_at_ms" if method == "item/started" else "completed_at_ms"
                details[timestamp_key] = timestamp_ms
            command = item.get("command")
            summary = command if isinstance(command, str) else "Command execution"
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                type=activity_type,
                status=self._activity_status(item.get("status")),
                summary=_bounded_text(summary),
                details=details,
            )
            return
        if item_type == "fileChange":
            activity_type = (
                "file_change_started" if method == "item/started" else "file_change_completed"
            )
            paths = self._safe_file_paths(item)
            file_details: dict[str, str | int | bool | None | list[str]] | None = (
                {"paths": paths} if paths else None
            )
            summary = ", ".join(paths) if paths else "File change"
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=item_id,
                type=activity_type,
                status=self._activity_status(item.get("status")),
                summary=summary,
                details=file_details,
            )
            return
        if method == "item/completed" and item_type == "agentMessage":
            text = item.get("text")
            if (
                item.get("phase") == "final_answer"
                and persist_final_answer
                and self._final_answer_store is not None
                and is_valid_final_answer_identifier(thread_id)
                and is_valid_final_answer_identifier(turn_id)
                and isinstance(item_id, str)
                and is_valid_final_answer_identifier(item_id)
            ):
                try:
                    self._final_answer_store.record_final_answer(thread_id, turn_id, item_id, text)
                except Exception as exc:
                    log_event("final_answer.capture_error", error_type=exc.__class__.__name__)
            if isinstance(text, str):
                self._state.update_latest_message(thread_id, turn_id, text)
                agent_activity_type: ActivityType = "agent_message"
                if item.get("phase") == "commentary":
                    agent_activity_type = "agent_commentary"
                self._record_activity(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    type=agent_activity_type,
                    status="completed",
                    summary=_bounded_text(text),
                )

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

    def _remember_thread_metadata(self, thread: Mapping[str, Any]) -> None:
        thread_id = thread.get("id")
        if isinstance(thread_id, str):
            self._state.update_thread_metadata(thread_id, thread)

    async def _start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        image_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": build_turn_input(prompt, image_paths),
        }
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        response = await self._app_server.request("turn/start", params)
        turn = response.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise BridgeError("turn/start did not return a turn id")
        turn_id = turn["id"]
        self._state.ensure_turn(thread_id, turn_id)
        log_event("turn.start", thread_id=thread_id, turn_id=turn_id)
        status = _native_state(turn.get("status"))
        if status in _TERMINAL_STATES:
            self._set_terminal(thread_id, turn_id, status, _bounded_text(turn.get("error")))
            activity_type: ActivityType
            if status == "completed":
                activity_type = "turn_completed"
            elif status == "failed":
                activity_type = "turn_failed"
            else:
                activity_type = "turn_interrupted"
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                type=activity_type,
                status=self._activity_status(status),
                summary=_bounded_text(turn.get("error"))
                if status == "failed"
                else "Turn " + status,
            )
        else:
            if not self._has_activity_type(thread_id, turn_id, "turn_started"):
                self._record_activity(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    type="turn_started",
                    status="in_progress",
                    summary="Turn started",
                )
        return self._public_snapshot(thread_id, turn_id)

    async def start(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: SandboxMode | None = None,
        approvals_reviewer: ApprovalsReviewer = "auto_review",
        title: str | None = None,
        image_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        sandbox_mode = validate_sandbox_mode(sandbox_mode)
        approvals_reviewer = validate_approvals_reviewer(approvals_reviewer)
        if reasoning_effort is not None and model is None:
            raise BridgeError("reasoning_effort requires an explicit model")
        canonical_cwd = self._path_policy.validate_cwd(cwd)
        normalized_title = normalize_explicit_title(title) if title is not None else None
        thread_params: dict[str, Any] = {"cwd": canonical_cwd}
        if sandbox_mode == "danger-full-access":
            thread_params["config"] = {"default_permissions": ":danger-full-access"}
        if model is not None:
            thread_params["model"] = model
        thread_params["approvalsReviewer"] = approvals_reviewer
        thread_params["approvalPolicy"] = "on-request"
        try:
            response = await self._app_server.request("thread/start", thread_params)
        except JsonRpcRemoteError:
            raise BridgeError("Codex App Server rejected the thread/start settings") from None
        thread = response.get("thread")
        if (
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or not thread["id"].strip()
        ):
            raise BridgeError("thread/start did not return a thread id")
        thread_id = thread["id"]
        if response.get("approvalsReviewer") != approvals_reviewer:
            raise BridgeError(
                "thread/start created empty thread "
                f"{thread_id} with mismatched approvals_reviewer; turn/start was not issued"
            )
        if response.get("approvalPolicy") != "on-request":
            raise BridgeError(
                "thread/start created empty thread "
                f"{thread_id} with mismatched approval_policy; turn/start was not issued"
            )
        self._state.mark_loaded(thread_id, canonical_cwd, thread)
        self._state.update_thread_metadata(thread_id, response)
        log_event("thread.start", thread_id=thread_id)
        assigned_title = normalized_title or derive_thread_title(prompt, thread_id)
        title_failed = False
        try:
            await self._app_server.request(
                "thread/name/set", {"threadId": thread_id, "name": assigned_title}
            )
        except (JsonRpcRemoteError, JsonRpcClosedError, OSError, TimeoutError):
            title_failed = True
        snapshot = await self._start_turn(
            thread_id,
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            image_paths=image_paths,
        )
        snapshot["title_assignment"] = (
            {"status": "failed"} if title_failed else {"status": "assigned", "name": assigned_title}
        )
        if title_failed:
            snapshot["title_warning"] = "Thread name could not be set"
        return snapshot

    async def model_capabilities(self) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        try:
            for _ in range(100):
                params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
                page = await self._app_server.request("model/list", params)
                if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                    raise ValueError("malformed model catalog")
                pages.append(page)
                next_cursor = page.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor.strip():
                    raise ValueError("malformed model catalog cursor")
                if next_cursor in seen_cursors:
                    raise ValueError("model catalog pagination cycle")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise ValueError("model catalog exceeded its page bound")
            config_response = await self._app_server.request(
                "config/read", {"includeLayers": False, "cwd": None}
            )
            return normalize_capabilities(pages, config_response)
        except JsonRpcClosedError:
            raise
        except Exception:
            raise BridgeError("model capabilities are unavailable") from None

    async def continue_thread(
        self, thread_id: str, prompt: str, *, image_paths: Sequence[str] = ()
    ) -> dict[str, Any]:
        if not self._state.is_loaded(thread_id):
            metadata = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = self._thread_from_response(metadata)
            validated_cwd = self._validate_thread_metadata(thread, thread_id)
            response = await self._app_server.request(
                "thread/resume", {"threadId": thread_id, "excludeTurns": True}
            )
            resumed_thread = self._thread_from_response(response)
            if resumed_thread.get("id") != thread_id:
                raise BridgeError("thread/resume returned a mismatched thread id")
            resumed_cwd = response.get("cwd")
            if not isinstance(resumed_cwd, str):
                raise BridgeError("thread/resume returned a malformed cwd")
            if self._path_policy.validate_cwd(resumed_cwd) != validated_cwd:
                raise BridgeError("thread/resume returned a mismatched cwd")
            self._state.mark_loaded(thread_id, validated_cwd, resumed_thread)
            self._state.update_thread_metadata(thread_id, response)
            log_event("thread.resume", thread_id=thread_id)
        return await self._start_turn(thread_id, prompt, image_paths=image_paths)

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

    def has_pending_request(self, request_id: RequestId) -> bool:
        return self._state.get_pending_request(request_id) is not None

    async def approve(self, request_id: RequestId, decision: ApprovalDecision) -> dict[str, Any]:
        if not isinstance(decision, str) or decision not in _APPROVAL_DECISIONS:
            raise ValueError("unsupported approval decision")
        async with self._approval_lock:
            pending = self._state.get_pending_request(request_id)
            if pending is None or pending.method not in _APPROVAL_METHODS:
                raise ApprovalConflictError("unknown approval request")
            payload: dict[str, Any]
            if pending.permission is None:
                payload = {"decision": decision}
            elif decision in {"accept", "acceptForSession"}:
                scope: PermissionGrantScope = (
                    "session" if decision == "acceptForSession" else "turn"
                )
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
            self._record_activity(
                thread_id=pending.thread_id,
                turn_id=pending.turn_id,
                item_id=pending.item_id,
                type="approval_resolved",
                status="resolved",
                summary=pending.summary,
                details={"decision": decision},
            )
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
        self._record_activity(
            thread_id=pending.thread_id,
            turn_id=pending.turn_id,
            item_id=pending.item_id,
            type="user_input_resolved",
            status="resolved",
            summary=pending.summary,
        )
        log_event("user_input.resolved", request_id=request_id)
        return self._public_snapshot(pending.thread_id, pending.turn_id)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        await self._app_server.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        self._turn_outcomes.stops.add((thread_id, turn_id))
        self._state.ensure_turn(thread_id, turn_id)
        log_event("turn.interrupt", thread_id=thread_id, turn_id=turn_id)
        return self._public_snapshot(thread_id, turn_id)

    async def interrupt_active_turns(
        self,
        wait_seconds: float = 0.5,
        *,
        exclude_turns: frozenset[tuple[str, str]] | set[tuple[str, str]] | None = None,
    ) -> None:
        excluded = exclude_turns or frozenset()

        def interruptible_turns() -> tuple[tuple[str, str], ...]:
            return tuple(turn for turn in self._state.active_turns() if turn not in excluded)

        active = interruptible_turns()
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
            active = interruptible_turns()

    def handle_app_server_failure(self, error: str) -> None:
        self._account_auth.disconnect()
        lines = error.splitlines()
        error_summary = _bounded_text(lines[0] if lines else error)
        for thread_id, turn_id in self._state.active_turns():
            self._turn_outcomes.reasons[(thread_id, turn_id)] = "connection_lost"
            self._set_terminal(thread_id, turn_id, "failed", error)
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                type="turn_failed",
                status="failed",
                summary=error_summary or "App Server failure",
                details={"reason": "connection_lost"},
            )
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                type="error",
                status="failed",
                summary=error_summary or "App Server failure",
            )
            log_event("turn.terminal", thread_id=thread_id, turn_id=turn_id, state="failed")

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
        running_only: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if running_only:
            if thread_id is not None:
                raise ValueError("running_only cannot be combined with thread_id")
            if include_history:
                raise ValueError("running_only cannot be combined with include_history")
            if cursor is not None:
                raise ValueError("running_only cannot be combined with cursor")
            return await self._running_threads()
        if thread_id is not None:
            metadata = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = self._thread_from_response(metadata)
            validated_cwd = self._validate_thread_metadata(thread, thread_id)
            self._remember_thread_metadata(thread)
            response = metadata
            if include_history:
                response = await self._app_server.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": True}
                )
                history_thread = self._thread_from_response(response)
                if self._validate_thread_metadata(history_thread, thread_id) != validated_cwd:
                    raise BridgeError("thread history returned a mismatched cwd")
                self._remember_thread_metadata(history_thread)
            return {"thread": _sanitize_thread_history(response.get("thread", {}))}
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = await self._app_server.request("thread/list", params)
        rows = response.get("data")
        visible: list[object] = []
        missing_title_rows: list[dict[str, object]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                try:
                    self._validate_thread_metadata(row, row["id"])
                except (BridgeError, ValueError):
                    continue
                self._remember_thread_metadata(row)
                public_row = _sanitize(row)
                visible.append(public_row)
                if isinstance(public_row, dict) and not (
                    isinstance(row.get("name"), str) and row["name"].strip()
                ):
                    missing_title_rows.append(public_row)
        missing_title_ids = [
            row_id for row in missing_title_rows if isinstance((row_id := row.get("id")), str)
        ]
        local_titles = _read_local_thread_titles(missing_title_ids)
        for row in missing_title_rows:
            row_id = row.get("id")
            if not isinstance(row_id, str):
                continue
            title = _usable_local_display_title(local_titles.get(row_id), row.get("preview"))
            if title is not None:
                row["name"] = title
        return self.observations.attach_list(
            {
                "threads": visible,
                "next_cursor": response.get("nextCursor"),
                "backwards_cursor": response.get("backwardsCursor"),
            }
        )

    async def _running_threads(self) -> dict[str, Any]:
        observed = self.observations.external_snapshot()
        candidate_ids: list[str] = []
        seen: set[str] = set()

        def add_if_running(thread_id: object) -> None:
            if not isinstance(thread_id, str) or thread_id in seen:
                return
            current = self.observations.current(thread_id, observed)
            if _is_running_observation(current):
                seen.add(thread_id)
                candidate_ids.append(thread_id)

        for thread_id, _turn_id in self._state.active_turns():
            add_if_running(thread_id)
        external_threads = observed.get("threads")
        if isinstance(external_threads, Mapping):
            for thread_id in external_threads:
                add_if_running(thread_id)

        visible: list[dict[str, object]] = []
        missing_title_rows: list[dict[str, object]] = []
        for thread_id in candidate_ids:
            try:
                metadata = await self._app_server.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                )
                thread = self._thread_from_response(metadata)
                self._validate_thread_metadata(thread, thread_id)
            except (BridgeError, ValueError):
                continue
            self._remember_thread_metadata(thread)
            public_row = _sanitize(thread)
            if not isinstance(public_row, dict):
                continue
            public_row.pop("turns", None)
            visible.append(public_row)
            if not (isinstance(thread.get("name"), str) and thread["name"].strip()):
                missing_title_rows.append(public_row)

        missing_title_ids = [
            row_id for row in missing_title_rows if isinstance((row_id := row.get("id")), str)
        ]
        local_titles = _read_local_thread_titles(missing_title_ids)
        for row in missing_title_rows:
            row_id = row.get("id")
            if not isinstance(row_id, str):
                continue
            title = _usable_local_display_title(local_titles.get(row_id), row.get("preview"))
            if title is not None:
                row["name"] = title

        result = self.observations.attach_list(
            {"threads": visible, "next_cursor": None, "backwards_cursor": None}
        )
        result["threads"] = [
            row
            for row in result["threads"]
            if isinstance(row, dict)
            and isinstance(row.get("current_execution"), Mapping)
            and _is_running_observation(row["current_execution"])
        ]
        returned_ids = {row["id"] for row in result["threads"] if isinstance(row.get("id"), str)}
        result["external_threads"] = [
            row
            for row in result["external_threads"]
            if isinstance(row, dict) and row.get("id") in returned_ids
        ]
        return result

    async def rename_thread(
        self,
        thread_id: str,
        name: str,
        *,
        only_if_untitled: bool = False,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("name must not be blank")
        if only_if_untitled:
            metadata = await self._app_server.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            )
            thread = self._thread_from_response(metadata)
            self._validate_thread_metadata(thread, thread_id)
            self._remember_thread_metadata(thread)
            current_name = thread.get("name")
            if isinstance(current_name, str) and current_name.strip():
                return {"status": "skipped", "reason": "already_named"}
            local_titles = _read_local_thread_titles([thread_id])
            if (
                _usable_local_display_title(local_titles.get(thread_id), thread.get("preview"))
                is not None
            ):
                return {"status": "skipped", "reason": "already_named"}
            await self._app_server.request("thread/name/set", {"threadId": thread_id, "name": name})
            return {"status": "renamed"}
        return await self._app_server.request(
            "thread/name/set", {"threadId": thread_id, "name": name}
        )

    async def rate_limits(self) -> dict[str, Any]:
        return await self._app_server.request("account/rateLimits/read", {})

    def account_auth_status(self) -> dict[str, object]:
        return self._account_auth.status()

    async def refresh_account_auth(self) -> dict[str, object]:
        return await self._account_auth.refresh()

    def disconnect_account_auth(self) -> None:
        self._account_auth.disconnect()

    async def start_account_login(self, flow_type: str) -> dict[str, Any]:
        return await self._account_auth.start_login(flow_type)

    async def cancel_account_login(self, login_id: str | None, revision: int) -> Any:
        return await self._account_auth.cancel_login(login_id, revision)

    async def _history_metadata(self, thread_id: str) -> tuple[dict[str, Any], str, str]:
        response = await self._app_server.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = self._thread_from_response(response)
        validated_cwd = self._validate_thread_metadata(thread, thread_id)
        self._remember_thread_metadata(thread)
        mode = thread.get("historyMode")
        history_mode = mode if mode == "paginated" else "legacy"
        return response, history_mode, validated_cwd

    async def _legacy_history_response(
        self,
        thread_id: str,
        validated_cwd: str,
    ) -> dict[str, Any]:
        response = await self._app_server.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = self._thread_from_response(response)
        if self._validate_thread_metadata(thread, thread_id) != validated_cwd:
            raise BridgeError("thread history returned a mismatched cwd")
        return response

    async def read_thread_turns(
        self,
        thread_id: str,
        limit: int = 20,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        limit, sort_direction = validate_history_query(limit, sort_direction)
        validate_cursor(cursor)
        _, history_mode, validated_cwd = await self._history_metadata(thread_id)
        if history_mode == "paginated":
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": limit,
                "sortDirection": sort_direction,
                "itemsView": "notLoaded",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._app_server.request("thread/turns/list", params)
            try:
                return project_turns_response(thread_id, response, limit)
            except HistoryValidationError as exc:
                raise BridgeError("malformed thread turns response") from exc

        validate_legacy_cursor(cursor)
        response = await self._legacy_history_response(thread_id, validated_cwd)
        try:
            return project_legacy_thread(
                thread_id,
                response,
                limit,
                sort_direction=sort_direction,
                policy=self._path_policy,
                cursor=cursor,
            )
        except HistoryValidationError as exc:
            raise BridgeError("malformed legacy thread history response") from exc

    async def read_thread_items(
        self,
        thread_id: str,
        turn_id: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        limit, sort_direction = validate_history_query(limit, sort_direction)
        validate_cursor(cursor)
        metadata_response, history_mode, validated_cwd = await self._history_metadata(thread_id)
        if history_mode == "paginated":
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": limit,
                "sortDirection": sort_direction,
            }
            if turn_id is not None:
                params["turnId"] = turn_id
            if cursor is not None:
                params["cursor"] = cursor
            response = await self._app_server.request("thread/items/list", params)
            try:
                result = project_items_response(
                    thread_id,
                    turn_id,
                    response,
                    policy=self._path_policy,
                    limit=limit,
                )
                self._merge_item_lifecycle(thread_id, result)
                return attach_turn_execution_metadata(result, metadata_response)
            except HistoryValidationError as exc:
                raise BridgeError("malformed thread items response") from exc

        validate_legacy_cursor(cursor)
        response = await self._legacy_history_response(thread_id, validated_cwd)
        try:
            result = project_legacy_items_response(
                thread_id,
                turn_id,
                response,
                policy=self._path_policy,
                limit=limit,
                sort_direction=sort_direction,
                cursor=cursor,
            )
            self._merge_item_lifecycle(thread_id, result)
            return attach_turn_execution_metadata(result, metadata_response)
        except HistoryValidationError as exc:
            raise BridgeError("malformed legacy thread history response") from exc

    def _merge_item_lifecycle(self, thread_id: str, result: dict[str, object]) -> None:
        items = result.get("items")
        if not isinstance(items, list):
            return
        for entry in items:
            if not isinstance(entry, dict):
                continue
            turn_id = entry.get("turn_id")
            item = entry.get("item")
            if not isinstance(turn_id, str) or not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            lifecycle = self._item_lifecycle.get(thread_id, turn_id, item_id)
            if lifecycle is None:
                continue
            item["started_at_ms"] = lifecycle.started_at_ms
            item["completed_at_ms"] = lifecycle.completed_at_ms

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
            raw_summary = params.get("reason") or params.get("command")
            summary = _bounded_text(raw_summary)
            activity_summary = raw_summary if isinstance(raw_summary, str) else None
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
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=params.get("itemId") if isinstance(params.get("itemId"), str) else None,
                type="approval_requested",
                status="requested",
                summary=activity_summary,
                details={"request_id": request_id},
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
            question_summary = "; ".join(question.header for question in questions)
            self._record_activity(
                thread_id=thread_id,
                turn_id=turn_id,
                item_id=params.get("itemId") if isinstance(params.get("itemId"), str) else None,
                type="user_input_requested",
                status="requested",
                summary=_bounded_text(question_summary),
                details={"request_id": request_id},
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

    def _final_answer_write_from_notification(
        self, message: Mapping[str, Any]
    ) -> _FinalAnswerWrite | None:
        if self._final_answer_store is None:
            return None
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return None
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not is_valid_final_answer_identifier(thread_id):
            return None
        if method == "item/completed":
            turn_id = params.get("turnId")
            item = params.get("item")
            if (
                not isinstance(turn_id, str)
                or not is_valid_final_answer_identifier(turn_id)
                or not isinstance(item, dict)
                or item.get("type") != "agentMessage"
                or item.get("phase") != "final_answer"
            ):
                return None
            item_id = item.get("id")
            if not isinstance(item_id, str) or not is_valid_final_answer_identifier(item_id):
                return None
            return _FinalAnswerWrite("item", thread_id, turn_id, item.get("text"), item_id)
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                return None
            turn_id = turn["id"]
            event_turn_id = params.get("turnId")
            if not is_valid_final_answer_identifier(turn_id) or (
                event_turn_id is not None and event_turn_id != turn_id
            ):
                return None
            raw_status = turn.get("status")
            status = (
                raw_status
                if isinstance(raw_status, str)
                and raw_status in {"completed", "failed", "interrupted"}
                else "unknown"
            )
            return _FinalAnswerWrite("terminal", thread_id, turn_id, status)
        if method == "error" and params.get("willRetry") is not True:
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and is_valid_final_answer_identifier(turn_id):
                return _FinalAnswerWrite("terminal", thread_id, turn_id, "failed")
        return None

    def _ensure_final_answer_writer(
        self,
    ) -> asyncio.Queue[_FinalAnswerWrite | _FinalAnswerReadBarrier | None] | None:
        if self._final_answer_store is None or self._final_answer_writer_closing:
            return None
        if self._final_answer_write_queue is None:
            self._final_answer_write_queue = asyncio.Queue(
                maxsize=_FINAL_ANSWER_WRITE_QUEUE_CAPACITY
            )
        task = self._final_answer_writer_task
        if task is None or task.done():
            if task is not None and not task.cancelled():
                error = task.exception()
                if error is not None:
                    log_event("final_answer.writer_error", error_type=error.__class__.__name__)
            self._final_answer_writer_task = asyncio.create_task(self._run_final_answer_writer())
        return self._final_answer_write_queue

    def _record_final_answer_write(self, write: _FinalAnswerWrite) -> None:
        store = self._final_answer_store
        if store is None:
            return
        if write.kind == "item":
            assert write.item_id is not None
            store.record_final_answer(write.thread_id, write.turn_id, write.item_id, write.value)
        else:
            assert isinstance(write.value, str)
            store.record_terminal(write.thread_id, write.turn_id, write.value)

    async def _run_final_answer_writer(self) -> None:
        queue = self._final_answer_write_queue
        if queue is None:
            return
        while True:
            entry = await queue.get()
            try:
                if entry is None:
                    return
                if isinstance(entry, _FinalAnswerReadBarrier):
                    if not entry.completed.done():
                        entry.completed.set_result(None)
                    continue
                try:
                    await asyncio.to_thread(self._record_final_answer_write, entry)
                except Exception as exc:
                    event = (
                        "final_answer.capture_error"
                        if entry.kind == "item"
                        else "final_answer.terminal_error"
                    )
                    log_event(event, error_type=exc.__class__.__name__)
            finally:
                queue.task_done()

    def _final_answer_enqueue_done(self, task: asyncio.Task[None]) -> None:
        self._final_answer_enqueue_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log_event("final_answer.queue_error", error_type=error.__class__.__name__)

    async def _enqueue_final_answer_entry(
        self,
        queue: asyncio.Queue[_FinalAnswerWrite | _FinalAnswerReadBarrier | None],
        entry: _FinalAnswerWrite | _FinalAnswerReadBarrier | None,
    ) -> None:
        try:
            queue.put_nowait(entry)
            return
        except asyncio.QueueFull:
            pass
        task = asyncio.create_task(queue.put(entry))
        self._final_answer_enqueue_tasks.add(task)
        task.add_done_callback(self._final_answer_enqueue_done)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("final_answer.queue_error", error_type=exc.__class__.__name__)
        finally:
            if task.done():
                self._final_answer_enqueue_tasks.discard(task)

    async def _wait_for_final_answer_enqueues(self) -> None:
        while self._final_answer_enqueue_tasks:
            pending = tuple(self._final_answer_enqueue_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_final_answer_writer(self) -> None:
        await self._wait_for_final_answer_enqueues()
        queue = self._final_answer_write_queue
        writer_task = self._final_answer_writer_task
        if queue is not None and writer_task is not None and not writer_task.done():
            await self._enqueue_final_answer_entry(queue, None)
            await asyncio.shield(writer_task)
        self._final_answer_writer_closed = True

    async def handle_notification_async(self, message: Mapping[str, Any]) -> None:
        async with self._final_answer_lock:
            await self._wait_for_final_answer_enqueues()
            write = self._final_answer_write_from_notification(message)
            self.handle_notification(message, persist_final_answer=False)
            if write is not None:
                queue = self._ensure_final_answer_writer()
                if queue is None:
                    log_event("final_answer.queue_unavailable")
                else:
                    await self._enqueue_final_answer_entry(queue, write)

    async def drain_final_answer_writes(self, *, close: bool = False) -> None:
        close_task: asyncio.Task[None] | None = None
        barrier: _FinalAnswerReadBarrier | None = None
        if close:
            async with self._final_answer_lock:
                if self._final_answer_writer_closed:
                    return
                if self._final_answer_close_task is None:
                    self._ensure_final_answer_writer()
                    self._final_answer_writer_closing = True
                    self._final_answer_close_task = asyncio.create_task(
                        self._close_final_answer_writer()
                    )
                close_task = self._final_answer_close_task
                assert close_task is not None
            await asyncio.shield(close_task)
            return

        async with self._final_answer_lock:
            await self._wait_for_final_answer_enqueues()
            if self._final_answer_writer_closed:
                return
            close_task = self._final_answer_close_task
            if self._final_answer_writer_closing and close_task is not None:
                barrier = None
            else:
                close_task = None
                queue = self._ensure_final_answer_writer()
                if queue is None:
                    return
                barrier = _FinalAnswerReadBarrier(asyncio.get_running_loop().create_future())
                await self._enqueue_final_answer_entry(queue, barrier)
        if close_task is not None:
            await asyncio.shield(close_task)
        elif barrier is not None:
            await barrier.completed

    def handle_notification(
        self, message: Mapping[str, Any], *, persist_final_answer: bool = True
    ) -> None:
        self._account_auth.on_notification(message)
        self._turn_outcomes.observe(message)
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
                    resolved_activity_type: ActivityType = (
                        "user_input_resolved"
                        if pending.method == _USER_INPUT_METHOD
                        else "approval_resolved"
                    )
                    self._record_activity(
                        thread_id=pending.thread_id,
                        turn_id=pending.turn_id,
                        item_id=pending.item_id,
                        type=resolved_activity_type,
                        status="resolved",
                        summary=pending.summary,
                    )
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
        if method == "item/commandExecution/outputDelta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if (
                isinstance(thread_id, str)
                and isinstance(turn_id, str)
                and isinstance(item_id, str)
                and isinstance(delta, str)
                and delta
            ):
                self._state.ensure_turn(thread_id, turn_id)
                self._record_activity(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    type="command_output",
                    status="in_progress",
                    summary=delta,
                    details={"output_truncated": True} if len(delta) > 2_000 else None,
                )
            return
        if method == "turn/started":
            turn = params.get("turn")
            if (
                isinstance(thread_id, str)
                and isinstance(turn, dict)
                and isinstance(turn.get("id"), str)
            ):
                self._state.ensure_turn(thread_id, turn["id"])
                if not self._has_activity_type(thread_id, turn["id"], "turn_started"):
                    self._record_activity(
                        thread_id=thread_id,
                        turn_id=turn["id"],
                        type="turn_started",
                        status="in_progress",
                        summary="Turn started",
                    )
            return
        if method in {"item/started", "item/completed"}:
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                item = params.get("item")
                if isinstance(item, dict):
                    try:
                        self._state.ensure_turn(thread_id, turn_id)
                        item_id = item.get("id")
                        timestamp_field = (
                            "startedAtMs" if method == "item/started" else "completedAtMs"
                        )
                        timestamp = _notification_item_timestamp(message, params, timestamp_field)
                        if isinstance(item_id, str) and timestamp is not None:
                            if method == "item/started":
                                self._item_lifecycle.observe_started(
                                    thread_id, turn_id, item_id, timestamp
                                )
                            else:
                                self._item_lifecycle.observe_completed(
                                    thread_id, turn_id, item_id, timestamp
                                )
                        self._record_item_activity(
                            method,
                            thread_id,
                            turn_id,
                            item,
                            timestamp_ms=timestamp,
                            persist_final_answer=persist_final_answer,
                        )
                        self._activities.publish_transient(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            item_id=item_id if isinstance(item_id, str) else None,
                            type="item_started" if method == "item/started" else "item_completed",
                            status=self._activity_status(item.get("status")),
                        )
                    except Exception as exc:
                        log_event("activity.normalize_error", error_type=exc.__class__.__name__)
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if (
                isinstance(thread_id, str)
                and isinstance(turn, dict)
                and isinstance(turn.get("id"), str)
            ):
                status = _native_state(turn.get("status"))
                turn_error = turn.get("error")
                auth_category = self._account_auth.observe_execution_error(
                    turn_error, turn_state=status
                )
                event_turn_id = params.get("turnId")
                if (
                    persist_final_answer
                    and self._final_answer_store is not None
                    and is_valid_final_answer_identifier(thread_id)
                    and is_valid_final_answer_identifier(turn["id"])
                    and (event_turn_id is None or event_turn_id == turn["id"])
                ):
                    final_status = turn.get("status")
                    if not isinstance(final_status, str) or final_status not in {
                        "completed",
                        "failed",
                        "interrupted",
                    }:
                        final_status = "unknown"
                    self._record_final_answer_terminal(thread_id, turn["id"], final_status)
                error_text = (
                    "Authentication may be required. Refresh the Codex account status."
                    if auth_category == "auth_check"
                    else _bounded_text(turn_error)
                )
                self._set_terminal(thread_id, turn["id"], status, error_text)
                activity_types: dict[str, ActivityType] = {
                    "completed": "turn_completed",
                    "failed": "turn_failed",
                    "interrupted": "turn_interrupted",
                }
                activity_type = activity_types.get(status)
                if activity_type is not None:
                    error_summary = error_text
                    self._record_activity(
                        thread_id=thread_id,
                        turn_id=turn["id"],
                        type=activity_type,
                        status=self._activity_status(status),
                        summary=error_summary if status == "failed" else "Turn " + status,
                    )
                log_event("turn.terminal", thread_id=thread_id, turn_id=turn["id"], state=status)
            return
        if method == "error":
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                raw_error = params.get("error")
                auth_category = self._account_auth.observe_execution_error(raw_error)
                error_summary = (
                    "Authentication may be required. Refresh the Codex account status."
                    if auth_category == "auth_check"
                    else _bounded_text(raw_error)
                )
                if error_summary is None:
                    error_summary = _bounded_text(params.get("message"))
                will_retry = params.get("willRetry")
                retryable = will_retry is True
                if not retryable:
                    if persist_final_answer:
                        self._record_final_answer_terminal(thread_id, turn_id, "failed")
                    self._set_terminal(thread_id, turn_id, "failed", error_summary)
                self._record_activity(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    type="error",
                    status="in_progress" if retryable else "failed",
                    summary=error_summary or "App Server error",
                )

    def _record_final_answer_terminal(self, thread_id: str, turn_id: str, status: str) -> None:
        store = self._final_answer_store
        if store is None:
            return
        try:
            store.record_terminal(thread_id, turn_id, status)
        except Exception as exc:
            log_event("final_answer.terminal_error", error_type=exc.__class__.__name__)

    def final_answer(self, thread_id: str, turn_id: str) -> dict[str, object]:
        if not is_valid_final_answer_identifier(thread_id) or not is_valid_final_answer_identifier(
            turn_id
        ):
            raise BridgeError("thread_id or turn_id is malformed")
        store = self._final_answer_store
        if store is None:
            return {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "unavailable",
                "reason": "storage_unavailable",
            }
        try:
            result = store.read(thread_id, turn_id)
        except FinalAnswerStoreError:
            result = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "unavailable",
                "reason": "storage_unavailable",
            }
        if result.get("thread_id") != thread_id or result.get("turn_id") != turn_id:
            return {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "incomplete",
                "reason": "stored_identity_mismatch",
            }
        result_status = result.get("status")
        if not isinstance(result_status, str) or result_status not in {
            "retrieved",
            "pending",
            "incomplete",
            "unavailable",
        }:
            return {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "incomplete",
                "reason": "stored_result_invalid",
            }
        if result.get("status") == "retrieved":
            text = result.get("text")
            if (
                result.get("source") != FINAL_ANSWER_SOURCE
                or not isinstance(text, str)
                or not text.strip()
                or len(text.encode("utf-16-le", errors="surrogatepass")) // 2
                > MAX_FINAL_ANSWER_UTF16_CODE_UNITS
            ):
                return {
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "status": "incomplete",
                    "reason": "stored_record_invalid",
                }
            return result
        if not isinstance(result.get("reason"), str) or not result["reason"]:
            return {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "status": "incomplete",
                "reason": "stored_result_invalid",
            }
        result.pop("text", None)
        result.pop("source", None)
        return result

    async def final_answer_async(self, thread_id: str, turn_id: str) -> dict[str, object]:
        await self.drain_final_answer_writes()
        return await asyncio.to_thread(self.final_answer, thread_id, turn_id)

    def _public_snapshot(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        snapshot = self._state.snapshot_if_known(thread_id, turn_id)
        if snapshot is None:
            return self._not_loaded_status(thread_id, turn_id)
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

    @staticmethod
    def _not_loaded_status(thread_id: str, turn_id: str | None) -> dict[str, Any]:
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "thread_metadata": {
                "model_provider": None,
                "model": None,
                "reasoning_effort": None,
                "cli_version": None,
                "sandbox_mode": None,
                "approval_policy": None,
                "approvals_reviewer": None,
            },
            "state": "not_loaded",
            "latest_agent_message": "",
            "current_diff": "",
            "pending_request": None,
            "error": None,
            "latest_activity": None,
            "recent_activities": [],
        }

    @property
    def app_server_os_pid(self) -> int | None:
        """Return the OS PID of this host's live managed App Server, if available."""
        pid = getattr(self._app_server, "os_pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 4_294_967_296:
            return pid
        return None

    async def status(
        self, thread_id: str, turn_id: str | None = None, activity_limit: int = 20
    ) -> dict[str, Any]:
        if (
            isinstance(activity_limit, bool)
            or not isinstance(activity_limit, int)
            or not _ACTIVITY_LIMIT_MIN <= activity_limit <= _ACTIVITY_LIMIT_MAX
        ):
            raise ValueError("activity_limit must be between 1 and 100")

        selected_turn_id = turn_id
        if selected_turn_id is None:
            selected_turn_id = self._state.active_turn_for_thread(thread_id)
            if selected_turn_id is None:
                selected_turn_id = self._state.latest_known_turn(thread_id)
            if selected_turn_id is None:
                selected_turn_id = self._activities.latest_known_turn(thread_id)

        if selected_turn_id is None or not self._state.has_turn(thread_id, selected_turn_id):
            result = self._not_loaded_status(thread_id, selected_turn_id)
        else:
            result = self._public_snapshot(thread_id, selected_turn_id)
            recent = self._activities.get_recent(thread_id, selected_turn_id, limit=activity_limit)
            latest = self._activities.latest(thread_id, selected_turn_id)
            result["latest_activity"] = latest.to_dict() if latest is not None else None
            result["recent_activities"] = [activity.to_dict() for activity in recent]
        return await self.observations.status(
            result,
            thread_id,
            self._history_metadata,
            self._legacy_history_response,
            self._app_server.request,
        )
