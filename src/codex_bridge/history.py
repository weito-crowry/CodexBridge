from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .paths import AllowedPathPolicy


class HistoryValidationError(ValueError):
    """Raised when persisted App Server history cannot be exposed safely."""


_MAX_TEXT_CHARS = 16 * 1024
_MAX_ERROR_CHARS = 2_000
_MAX_ITEMS = 100
_MAX_CURSOR_CHARS = 4_096
_SENSITIVE_TERMS = (
    "raw",
    "encrypted",
    "reasoning",
    "chainofthought",
    "secret",
    "token",
    "password",
    "credential",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(\bauthorization\b\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|token|password|secret|credential)\b\s*(?:[:=]|\s)\s*)[^\s,;]+"
    ),
)
_KNOWN_ITEMS = {
    "userMessage",
    "agentMessage",
    "plan",
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "functionCallOutput",
    "collabAgentToolCall",
    "subAgentActivity",
    "imageView",
    "contextCompaction",
    "context_compaction",
    "enteredReviewMode",
    "exitedReviewMode",
}
_IMAGE_INPUT_TYPES = {"image", "localImage", "audio", "localAudio"}
_SAFE_PHASES = {"commentary", "final_answer"}


def validate_history_query(limit: object, sort_direction: object) -> tuple[int, str]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_ITEMS:
        raise HistoryValidationError("limit must be between 1 and 100")
    if sort_direction not in {"asc", "desc"}:
        raise HistoryValidationError('sort_direction must be "asc" or "desc"')
    return limit, sort_direction


def validate_legacy_cursor(cursor: object) -> None:
    validate_cursor(cursor)
    if cursor is not None:
        raise HistoryValidationError("legacy history does not support cursor pagination")


def validate_cursor(cursor: object) -> None:
    if cursor is not None and (
        not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_CHARS
    ):
        raise HistoryValidationError("cursor must be a non-empty bounded string")


def _bounded_text(value: object, limit: int = _MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    text = value[:limit]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1<redacted>", text)
    return text


def _safe_error(value: object) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("message")
    return _bounded_text(value, _MAX_ERROR_CHARS)


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _safe_status(value: object) -> str:
    if value == "inProgress":
        return "in_progress"
    if value in {"completed", "interrupted", "failed"}:
        return str(value)
    return "unknown"


def _safe_item_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("id")
    return value if isinstance(value, str) else None


def _project_user_message(item: Mapping[str, Any]) -> dict[str, object] | None:
    item_id = _safe_item_id(item)
    content = item.get("content")
    if item_id is None or not isinstance(content, list):
        return None
    parts: list[str] = []
    for entry in content[:100]:
        if not isinstance(entry, Mapping):
            continue
        entry_type = entry.get("type")
        if entry_type == "text":
            text = _bounded_text(entry.get("text"))
            if text is not None:
                parts.append(text)
        elif entry_type in _IMAGE_INPUT_TYPES:
            parts.append("[image]")
    return {"id": item_id, "type": "userMessage", "text": _bounded_text("\n".join(parts)) or ""}


def _project_item(item: object, policy: AllowedPathPolicy) -> dict[str, object] | None:
    if not isinstance(item, Mapping):
        return None
    item_type = item.get("type")
    if not isinstance(item_type, str) or item_type not in _KNOWN_ITEMS:
        return None
    item_id = _safe_item_id(item)
    if item_type == "userMessage":
        return _project_user_message(item)
    if item_type == "agentMessage":
        text = _bounded_text(item.get("text"))
        if item_id is None or text is None:
            return None
        result: dict[str, object] = {"id": item_id, "type": item_type, "text": text}
        phase = item.get("phase")
        if phase in _SAFE_PHASES:
            result["phase"] = phase
        return result
    if item_type == "plan":
        text = _bounded_text(item.get("text"))
        if item_id is None or text is None:
            return None
        return {"id": item_id, "type": item_type, "text": text}
    if item_type == "commandExecution":
        command = _bounded_text(item.get("command"))
        status = item.get("status")
        if item_id is None or command is None or not isinstance(status, str):
            return None
        result = {
            "id": item_id,
            "type": item_type,
            "command": command,
            "status": _safe_status(status),
        }
        exit_code = _safe_int(item.get("exitCode"))
        duration_ms = _safe_int(item.get("durationMs"))
        if exit_code is not None:
            result["exit_code"] = exit_code
        if duration_ms is not None:
            result["duration_ms"] = duration_ms
        return result
    if item_type == "fileChange":
        status = item.get("status")
        if item_id is None or not isinstance(status, str):
            return None
        paths: list[str] = []
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes[:_MAX_ITEMS]:
                if not isinstance(change, Mapping) or not isinstance(change.get("path"), str):
                    continue
                path = policy.safe_relative_path(change["path"])
                if path != "<path omitted>" and path not in paths:
                    paths.append(path)
        return {"id": item_id, "type": item_type, "status": _safe_status(status), "paths": paths}
    if item_type == "mcpToolCall":
        if item_id is None or not all(
            isinstance(item.get(key), str) for key in ("server", "tool", "status")
        ):
            return None
        result = {
            "id": item_id,
            "type": item_type,
            "server": item["server"],
            "tool": item["tool"],
            "status": _safe_status(item["status"]),
        }
        duration_ms = _safe_int(item.get("durationMs"))
        if duration_ms is not None:
            result["duration_ms"] = duration_ms
        if isinstance(item.get("readOnlyHint"), bool):
            result["read_only_hint"] = item["readOnlyHint"]
        if isinstance(item.get("pluginId"), str):
            result["plugin_id"] = item["pluginId"]
        return result
    if item_type == "dynamicToolCall":
        if item_id is None or not all(isinstance(item.get(key), str) for key in ("tool", "status")):
            return None
        result = {
            "id": item_id,
            "type": item_type,
            "tool": item["tool"],
            "status": _safe_status(item["status"]),
        }
        if isinstance(item.get("namespace"), str):
            result["namespace"] = item["namespace"]
        if isinstance(item.get("success"), bool):
            result["success"] = item["success"]
        duration_ms = _safe_int(item.get("durationMs"))
        if duration_ms is not None:
            result["duration_ms"] = duration_ms
        return result
    if item_type == "functionCallOutput":
        if item_id is None:
            return None
        result = {"id": item_id, "type": item_type}
        for source, target in (("name", "name"), ("namespace", "namespace")):
            if isinstance(item.get(source), str):
                result[target] = item[source]
        return result
    if item_type == "collabAgentToolCall":
        if item_id is None or not all(isinstance(item.get(key), str) for key in ("tool", "status")):
            return None
        return {
            "id": item_id,
            "type": item_type,
            "tool": item["tool"],
            "status": _safe_status(item["status"]),
        }
    if item_type == "subAgentActivity":
        if (
            item_id is None
            or not isinstance(item.get("kind"), str)
            or not isinstance(item.get("agentThreadId"), str)
        ):
            return None
        return {
            "id": item_id,
            "type": item_type,
            "kind": item["kind"],
            "agent_thread_id": item["agentThreadId"],
        }
    if item_type == "imageView":
        if item_id is None or not isinstance(item.get("path"), str):
            return None
        result = {"id": item_id, "type": item_type}
        path = policy.safe_relative_path(item["path"])
        if path != "<path omitted>":
            result["path"] = path
        return result
    if item_type in {
        "contextCompaction",
        "context_compaction",
        "enteredReviewMode",
        "exitedReviewMode",
    }:
        return {"id": item_id, "type": item_type} if item_id is not None else None
    return None


def project_item(item: object, policy: AllowedPathPolicy) -> dict[str, object] | None:
    """Project one persisted ThreadItem through the strict public allowlist."""
    return _project_item(item, policy)


def project_thread_metadata(thread: object) -> dict[str, object] | None:
    """Project thread metadata without exposing persisted paths or opaque fields."""
    if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
        return None
    result: dict[str, object] = {"id": thread["id"]}
    for source, target in (
        ("cwd", "cwd"),
        ("name", "name"),
        ("preview", "preview"),
    ):
        value = thread.get(source)
        if isinstance(value, str):
            bounded = _bounded_text(value)
            if bounded is not None:
                result[target] = bounded
    for source, target in (("createdAt", "created_at"), ("updatedAt", "updated_at")):
        value = _safe_int(thread.get(source))
        if value is not None:
            result[target] = value
    if thread.get("historyMode") in {"legacy", "paginated"}:
        result["history_mode"] = thread["historyMode"]
    return result


def _project_turn(
    turn: object, policy: AllowedPathPolicy | None = None
) -> dict[str, object] | None:
    if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
        return None
    result: dict[str, object] = {
        "id": turn["id"],
        "status": _safe_status(turn.get("status")),
        "started_at": _safe_int(turn.get("startedAt")),
        "completed_at": _safe_int(turn.get("completedAt")),
        "duration_ms": _safe_int(turn.get("durationMs")),
        "error": _safe_error(turn.get("error")),
        "items_view": turn.get("itemsView")
        if turn.get("itemsView") in {"notLoaded", "summary", "full"}
        else "notLoaded",
    }
    if policy is not None and isinstance(turn.get("items"), list):
        result["items"] = [
            projected
            for item in turn["items"][:_MAX_ITEMS]
            if (projected := _project_item(item, policy)) is not None
        ]
    return result


def _cursor(response: Mapping[str, Any], key: str) -> str | None:
    value = response.get(key)
    return value if isinstance(value, str) and len(value) <= _MAX_CURSOR_CHARS else None


def project_turns_response(
    thread_id: str, response: Mapping[str, Any], limit: int = 20
) -> dict[str, object]:
    validate_history_query(limit, "desc")
    data = response.get("data")
    if not isinstance(data, list):
        raise HistoryValidationError("turn history response data is malformed")
    turns = [
        projected for turn in data[:_MAX_ITEMS] if (projected := _project_turn(turn)) is not None
    ]
    return {
        "thread_id": thread_id,
        "history_mode": "paginated",
        "turns": turns[:limit],
        "next_cursor": _cursor(response, "nextCursor"),
        "backwards_cursor": _cursor(response, "backwardsCursor"),
        "truncated": len(data) > limit,
    }


def project_items_response(
    thread_id: str,
    turn_id: str | None,
    response: Mapping[str, Any],
    *,
    policy: AllowedPathPolicy,
    limit: int = 100,
) -> dict[str, object]:
    validate_history_query(limit, "desc")
    data = response.get("data")
    if not isinstance(data, list):
        raise HistoryValidationError("item history response data is malformed")
    items: list[dict[str, object]] = []
    for entry in data[:_MAX_ITEMS]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("turnId"), str):
            continue
        projected = _project_item(entry.get("item"), policy)
        if projected is not None:
            items.append({"turn_id": entry["turnId"], "item": projected})
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "history_mode": "paginated",
        "items": items[:limit],
        "next_cursor": _cursor(response, "nextCursor"),
        "backwards_cursor": _cursor(response, "backwardsCursor"),
        "truncated": len(data) > limit,
    }


def project_legacy_thread(
    thread_id: str,
    response: Mapping[str, Any],
    limit: int = 20,
    *,
    sort_direction: str = "desc",
    policy: AllowedPathPolicy | None = None,
    cursor: str | None = None,
) -> dict[str, object]:
    validate_history_query(limit, sort_direction)
    validate_legacy_cursor(cursor)
    thread = response.get("thread")
    if not isinstance(thread, Mapping) or not isinstance(thread.get("turns"), list):
        raise HistoryValidationError("legacy thread history response is malformed")
    turns: list[dict[str, object]] = []
    raw_turns = reversed(thread["turns"]) if sort_direction == "desc" else iter(thread["turns"])
    for turn in raw_turns:
        projected = _project_turn(turn, policy)
        if projected is not None:
            turns.append(projected)
            if len(turns) > limit:
                break
    return {
        "thread_id": thread_id,
        "history_mode": "legacy",
        "turns": turns[:limit],
        "next_cursor": None,
        "backwards_cursor": None,
        "truncated": len(thread["turns"]) > limit,
    }


def project_legacy_items_response(
    thread_id: str,
    turn_id: str | None,
    response: Mapping[str, Any],
    *,
    policy: AllowedPathPolicy,
    limit: int = 100,
    sort_direction: str = "desc",
    cursor: str | None = None,
) -> dict[str, object]:
    validate_history_query(limit, sort_direction)
    validate_legacy_cursor(cursor)
    thread = response.get("thread")
    if not isinstance(thread, Mapping) or not isinstance(thread.get("turns"), list):
        raise HistoryValidationError("legacy thread history response is malformed")
    items: list[dict[str, object]] = []
    raw_turns = reversed(thread["turns"]) if sort_direction == "desc" else iter(thread["turns"])
    for turn in raw_turns:
        if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
            continue
        if turn_id is not None and turn["id"] != turn_id:
            continue
        raw_items = turn.get("items")
        if not isinstance(raw_items, list):
            continue
        ordered_items = reversed(raw_items) if sort_direction == "desc" else iter(raw_items)
        for item in ordered_items:
            projected = _project_item(item, policy)
            if projected is not None:
                items.append({"turn_id": turn["id"], "item": projected})
                if len(items) > limit:
                    break
        if len(items) > limit:
            break
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "history_mode": "legacy",
        "items": items[:limit],
        "next_cursor": None,
        "backwards_cursor": None,
        "truncated": len(items) > limit,
    }
