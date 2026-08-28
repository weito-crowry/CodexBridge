from __future__ import annotations

import pytest

from codex_bridge.history import (
    HistoryValidationError,
    project_items_response,
    project_legacy_items_response,
    project_legacy_thread,
    project_turns_response,
)
from codex_bridge.paths import AllowedPathPolicy


def _policy(tmp_path) -> AllowedPathPolicy:
    return AllowedPathPolicy((str(tmp_path),))


def test_project_turns_returns_bounded_safe_metadata_and_cursors() -> None:
    result = project_turns_response(
        "thread-1",
        {
            "data": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 123,
                    "completedAt": 456,
                    "durationMs": 333,
                    "itemsView": "notLoaded",
                    "error": {"message": "bounded failure", "additionalDetails": "secret"},
                    "raw": "must not expose",
                }
            ],
            "nextCursor": "next-1",
            "backwardsCursor": "back-1",
        },
    )

    assert result == {
        "thread_id": "thread-1",
        "history_mode": "paginated",
        "turns": [
            {
                "id": "turn-1",
                "status": "completed",
                "started_at": 123,
                "completed_at": 456,
                "duration_ms": 333,
                "error": "bounded failure",
                "items_view": "notLoaded",
            }
        ],
        "next_cursor": "next-1",
        "backwards_cursor": "back-1",
        "truncated": False,
    }
    assert "additionalDetails" not in str(result)
    assert "must not expose" not in str(result)


def test_project_items_allowlists_known_types_and_excludes_private_content(tmp_path) -> None:
    allowed_path = tmp_path / "src" / "changed.py"
    outside_path = tmp_path.parent / "outside-secret.py"
    result = project_items_response(
        "thread-1",
        "turn-1",
        {
            "data": [
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "user-1",
                        "type": "userMessage",
                        "content": [
                            {"type": "text", "text": "hello user"},
                            {"type": "image", "url": "opaque-image-url"},
                        ],
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "agent-1",
                        "type": "agentMessage",
                        "text": "hello agent",
                        "phase": "final_answer",
                        "reasoning": "must not expose",
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {"id": "plan-1", "type": "plan", "text": "safe plan"},
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "command-1",
                        "type": "commandExecution",
                        "command": "pytest tests",
                        "status": "completed",
                        "exitCode": 0,
                        "durationMs": 12,
                        "aggregatedOutput": "secret output",
                        "commandActions": [{"type": "unknown"}],
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "file-1",
                        "type": "fileChange",
                        "status": "completed",
                        "changes": [
                            {"path": str(allowed_path), "diff": "full diff"},
                            {"path": str(outside_path), "diff": "outside diff"},
                        ],
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "mcp-1",
                        "type": "mcpToolCall",
                        "server": "server",
                        "tool": "tool",
                        "status": "completed",
                        "durationMs": 20,
                        "readOnlyHint": True,
                        "pluginId": "plugin",
                        "arguments": {"password": "secret"},
                        "result": {"content": "secret result"},
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "dynamic-1",
                        "type": "dynamicToolCall",
                        "namespace": "namespace",
                        "tool": "tool",
                        "status": "completed",
                        "success": True,
                        "durationMs": 30,
                        "arguments": {"token": "secret"},
                        "contentItems": [{"text": "secret"}],
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "unknown-1",
                        "type": "futureItem",
                        "secretFutureField": "must not expose",
                    },
                },
                {
                    "turnId": "turn-1",
                    "item": {
                        "id": "reasoning-1",
                        "type": "reasoning",
                        "summary": ["must not expose"],
                        "content": ["must not expose"],
                    },
                },
            ],
            "nextCursor": "next-items",
            "backwardsCursor": "back-items",
        },
        policy=_policy(tmp_path),
    )

    items = result["items"]
    assert [entry["item"]["type"] for entry in items] == [
        "userMessage",
        "agentMessage",
        "plan",
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
    ]
    assert items[0]["item"] == {
        "id": "user-1",
        "type": "userMessage",
        "text": "hello user\n[image]",
    }
    assert items[1]["item"] == {
        "id": "agent-1",
        "type": "agentMessage",
        "text": "hello agent",
        "phase": "final_answer",
    }
    assert items[3]["item"] == {
        "id": "command-1",
        "type": "commandExecution",
        "command": "pytest tests",
        "status": "completed",
        "exit_code": 0,
        "duration_ms": 12,
    }
    assert items[4]["item"] == {
        "id": "file-1",
        "type": "fileChange",
        "status": "completed",
        "paths": ["src/changed.py"],
    }
    assert items[5]["item"] == {
        "id": "mcp-1",
        "type": "mcpToolCall",
        "server": "server",
        "tool": "tool",
        "status": "completed",
        "duration_ms": 20,
        "read_only_hint": True,
        "plugin_id": "plugin",
    }
    assert items[6]["item"] == {
        "id": "dynamic-1",
        "type": "dynamicToolCall",
        "namespace": "namespace",
        "tool": "tool",
        "status": "completed",
        "success": True,
        "duration_ms": 30,
    }
    assert result["next_cursor"] == "next-items"
    assert result["backwards_cursor"] == "back-items"
    assert result["truncated"] is False
    public_text = str(result)
    for secret in (
        "must not expose",
        "secret output",
        "full diff",
        "outside diff",
        "opaque-image-url",
    ):
        assert secret not in public_text


def test_project_legacy_thread_is_bounded_and_non_paginated(tmp_path) -> None:
    result = project_legacy_thread(
        "thread-legacy",
        {
            "thread": {
                "id": "thread-legacy",
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "startedAt": 1,
                        "items": [
                            {"id": "agent-1", "type": "agentMessage", "text": "safe"},
                            {"id": "reasoning-1", "type": "reasoning", "summary": ["private"]},
                        ],
                    },
                    {
                        "id": "turn-2",
                        "status": "completed",
                        "items": [{"id": "agent-2", "type": "agentMessage", "text": "new"}],
                    },
                ],
            }
        },
        limit=1,
        policy=_policy(tmp_path),
    )

    assert result["history_mode"] == "legacy"
    assert len(result["turns"]) == 1
    assert result["turns"][0]["id"] == "turn-2"
    assert result["turns"][0]["items"] == [{"id": "agent-2", "type": "agentMessage", "text": "new"}]
    assert result["next_cursor"] is None
    assert result["backwards_cursor"] is None
    assert result["truncated"] is True
    assert "private" not in str(result)


def _legacy_sorted_history() -> dict[str, object]:
    return {
        "thread": {
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"id": "item-1", "type": "agentMessage", "text": "oldest"},
                        {"id": "private-1", "type": "reasoning", "summary": ["private"]},
                        {"id": "unknown-1", "type": "notPublic", "text": "unknown"},
                        {"id": "item-2", "type": "agentMessage", "text": "old"},
                    ],
                },
                {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [{"id": "item-3", "type": "agentMessage", "text": "middle"}],
                },
                {
                    "id": "turn-3",
                    "status": "completed",
                    "items": [{"id": "item-4", "type": "agentMessage", "text": "newest"}],
                },
            ]
        }
    }


def test_project_legacy_thread_applies_sort_direction_before_limit(tmp_path) -> None:
    policy = _policy(tmp_path)

    descending = project_legacy_thread(
        "thread",
        _legacy_sorted_history(),
        limit=2,
        sort_direction="desc",
        policy=policy,
    )
    ascending = project_legacy_thread(
        "thread",
        _legacy_sorted_history(),
        limit=2,
        sort_direction="asc",
        policy=policy,
    )

    assert [turn["id"] for turn in descending["turns"]] == ["turn-3", "turn-2"]
    assert [turn["id"] for turn in ascending["turns"]] == ["turn-1", "turn-2"]


def test_project_legacy_items_applies_sort_direction_after_safe_projection(tmp_path) -> None:
    policy = _policy(tmp_path)

    descending = project_legacy_items_response(
        "thread",
        None,
        _legacy_sorted_history(),
        limit=3,
        sort_direction="desc",
        policy=policy,
    )
    ascending = project_legacy_items_response(
        "thread",
        None,
        _legacy_sorted_history(),
        limit=3,
        sort_direction="asc",
        policy=policy,
    )

    assert [entry["item"]["id"] for entry in descending["items"]] == [
        "item-4",
        "item-3",
        "item-2",
    ]
    assert [entry["item"]["id"] for entry in ascending["items"]] == [
        "item-1",
        "item-2",
        "item-3",
    ]
    assert "private-1" not in str(descending)
    assert "unknown-1" not in str(descending)


def test_project_legacy_items_applies_sort_direction_for_selected_turn(tmp_path) -> None:
    policy = _policy(tmp_path)

    descending = project_legacy_items_response(
        "thread",
        "turn-1",
        _legacy_sorted_history(),
        limit=2,
        sort_direction="desc",
        policy=policy,
    )
    ascending = project_legacy_items_response(
        "thread",
        "turn-1",
        _legacy_sorted_history(),
        limit=2,
        sort_direction="asc",
        policy=policy,
    )

    assert [entry["item"]["id"] for entry in descending["items"]] == ["item-2", "item-1"]
    assert [entry["item"]["id"] for entry in ascending["items"]] == ["item-1", "item-2"]


def test_project_items_allowlists_remaining_work_summary_types(tmp_path) -> None:
    result = project_items_response(
        "thread",
        None,
        {
            "data": [
                {
                    "turnId": "turn",
                    "item": {
                        "id": "function-1",
                        "type": "functionCallOutput",
                        "name": "function",
                        "namespace": "namespace",
                        "output": "private output",
                    },
                },
                {
                    "turnId": "turn",
                    "item": {
                        "id": "collab-1",
                        "type": "collabAgentToolCall",
                        "tool": "spawn",
                        "status": "completed",
                        "prompt": "private prompt",
                        "reasoningEffort": "high",
                    },
                },
                {
                    "turnId": "turn",
                    "item": {
                        "id": "subagent-1",
                        "type": "subAgentActivity",
                        "kind": "started",
                        "agentThreadId": "agent-thread",
                        "agentPath": "C:/private/agent",
                    },
                },
                {
                    "turnId": "turn",
                    "item": {
                        "id": "image-1",
                        "type": "imageView",
                        "path": str(tmp_path / "image.png"),
                    },
                },
                {
                    "turnId": "turn",
                    "item": {
                        "id": "image-2",
                        "type": "imageView",
                        "path": str(tmp_path.parent / "outside.png"),
                    },
                },
                {"turnId": "turn", "item": {"id": "compact-1", "type": "contextCompaction"}},
                {
                    "turnId": "turn",
                    "item": {"id": "enter-1", "type": "enteredReviewMode", "review": "private"},
                },
                {
                    "turnId": "turn",
                    "item": {"id": "exit-1", "type": "exitedReviewMode", "review": "private"},
                },
                {"turnId": "turn", "item": {"id": "hook-1", "type": "hookPrompt", "fragments": []}},
            ]
        },
        policy=_policy(tmp_path),
    )

    assert [entry["item"] for entry in result["items"]] == [
        {
            "id": "function-1",
            "type": "functionCallOutput",
            "name": "function",
            "namespace": "namespace",
        },
        {"id": "collab-1", "type": "collabAgentToolCall", "tool": "spawn", "status": "completed"},
        {
            "id": "subagent-1",
            "type": "subAgentActivity",
            "kind": "started",
            "agent_thread_id": "agent-thread",
        },
        {"id": "image-1", "type": "imageView", "path": "image.png"},
        {"id": "image-2", "type": "imageView"},
        {"id": "compact-1", "type": "contextCompaction"},
        {"id": "enter-1", "type": "enteredReviewMode"},
        {"id": "exit-1", "type": "exitedReviewMode"},
    ]
    public_text = str(result)
    for secret in ("private output", "private prompt", "private/agent", "private"):
        assert secret not in public_text


def test_legacy_cursor_is_rejected_instead_of_fabricated() -> None:
    with pytest.raises(HistoryValidationError, match="cursor"):
        project_legacy_thread("thread", {"thread": {"turns": []}}, limit=1, cursor="fake")


def test_history_cursor_must_be_a_bounded_string() -> None:
    with pytest.raises(HistoryValidationError, match="cursor"):
        project_legacy_thread("thread", {"thread": {"turns": []}}, limit=1, cursor=123)  # type: ignore[arg-type]
    with pytest.raises(HistoryValidationError, match="cursor"):
        project_legacy_thread("thread", {"thread": {"turns": []}}, limit=1, cursor="x" * 4097)
