from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_bridge.history import HistoryValidationError
from codex_bridge.paths import PathPolicyError
from tests.bridge_test_fakes import make_activity_bridge, make_bridge
from tests.bridge_test_support import write_rollout

pytest_plugins = ("tests.bridge_test_support",)


@pytest.mark.asyncio
async def test_threads_list_and_read_are_bounded_and_sanitized(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["native-thread"] = str(allowed_dir)

    listed = await bridge.threads(limit=5, cursor="next")
    detail = await bridge.threads("native-thread", include_history=True)

    assert listed["threads"] == []
    assert app.calls == [
        ("thread/list", {"limit": 5, "cursor": "next"}),
        ("thread/read", {"threadId": "native-thread", "includeTurns": False}),
        ("thread/read", {"threadId": "native-thread", "includeTurns": True}),
    ]
    assert detail["thread"] == {
        "id": "native-thread",
        "cwd": str(allowed_dir.resolve()),
        "turns": [],
    }


def _write_local_thread_catalog(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE local_thread_catalog ("
            "host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT NOT NULL, "
            "PRIMARY KEY (host_id, thread_id))"
        )
        connection.executemany(
            "INSERT INTO local_thread_catalog (host_id, thread_id, display_title) VALUES (?, ?, ?)",
            rows,
        )


@pytest.mark.asyncio
async def test_threads_prefer_app_server_name_over_local_catalog_and_ignore_prompt_prefix(
    allowed_dir, tmp_path, monkeypatch
) -> None:
    import codex_bridge.bridge as bridge_module

    catalog = tmp_path / "codex-dev.db"
    _write_local_thread_catalog(
        catalog,
        [
            ("local", "existing", "Stale local title"),
            ("local", "generated", "Generated title"),
            ("local", "prompt", "Please diagnose this"),
            ("remote", "remote-only", "Remote title"),
        ],
    )
    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", catalog, raising=False)
    bridge, app, _ = make_bridge(allowed_dir)
    cwd = str(allowed_dir)
    app.thread_list = [
        {"id": "existing", "name": "Existing title", "cwd": cwd, "preview": "User request"},
        {
            "id": "generated",
            "cwd": cwd,
            "preview": "Please review the changes and summarize the findings.",
        },
        {"id": "prompt", "cwd": cwd, "preview": "Please diagnose this failure carefully."},
        {"id": "remote-only", "cwd": cwd, "preview": "A local thread title is unavailable."},
        {"id": "untitled", "cwd": cwd, "preview": "A request without a local title."},
    ]

    result = await bridge.threads(limit=5)

    threads = {thread["id"]: thread for thread in result["threads"]}
    assert threads["existing"]["name"] == "Existing title"
    assert threads["generated"]["name"] == "Generated title"
    assert "name" not in threads["prompt"]
    assert "name" not in threads["remote-only"]
    assert "name" not in threads["untitled"]
    assert app.calls == [("thread/list", {"limit": 5})]


@pytest.mark.asyncio
async def test_threads_ignore_unavailable_local_catalog(allowed_dir, tmp_path, monkeypatch) -> None:
    import codex_bridge.bridge as bridge_module

    missing_catalog = tmp_path / "missing" / "codex-dev.db"
    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", missing_catalog, raising=False)
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_list = [
        {
            "id": "no-catalog",
            "cwd": str(allowed_dir),
            "preview": "Do not use this request as a title.",
        }
    ]

    result = await bridge.threads(limit=1)

    assert "name" not in result["threads"][0]
    assert app.calls == [("thread/list", {"limit": 1})]


@pytest.mark.asyncio
async def test_threads_pick_up_local_title_added_after_an_earlier_empty_refresh(
    allowed_dir, tmp_path, monkeypatch
) -> None:
    import codex_bridge.bridge as bridge_module

    catalog = tmp_path / "codex-dev.db"
    _write_local_thread_catalog(catalog, [])
    monkeypatch.setattr(bridge_module, "_LOCAL_THREAD_CATALOG_PATH", catalog, raising=False)
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_list = [
        {
            "id": "late-title",
            "cwd": str(allowed_dir),
            "preview": "This request is not a generated title.",
        }
    ]

    first = await bridge.threads(limit=1)
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            "INSERT INTO local_thread_catalog (host_id, thread_id, display_title) VALUES (?, ?, ?)",
            ("local", "late-title", "Generated title"),
        )
    second = await bridge.threads(limit=1)

    assert "name" not in first["threads"][0]
    assert second["threads"][0]["name"] == "Generated title"
    assert app.calls == [
        ("thread/list", {"limit": 1}),
        ("thread/list", {"limit": 1}),
    ]


@pytest.mark.asyncio
async def test_rename_thread_calls_native_name_set_for_supplied_thread(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.rename_thread("right-clicked", "Renamed")

    assert result == {"thread": {"id": "right-clicked", "name": "Renamed"}}
    assert app.calls[-1] == (
        "thread/name/set",
        {"threadId": "right-clicked", "name": "Renamed"},
    )


@pytest.mark.asyncio
async def test_threads_history_omits_reasoning_thread_items(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["native-thread"] = str(allowed_dir)
    app.thread_histories["native-thread"] = [
        {
            "id": "turn",
            "items": [
                {
                    "id": "user-item",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "normal userMessage"}],
                },
                {
                    "id": "reasoning-item",
                    "type": "reasoning",
                    "summary": "reasoning summary",
                    "content": ["reasoning content"],
                },
                {"id": "agent-item", "type": "agentMessage", "text": "normal agentMessage"},
            ],
        }
    ]

    detail = await bridge.threads("native-thread", include_history=True)

    items = detail["thread"]["turns"][0]["items"]
    assert [item["type"] for item in items] == ["userMessage", "agentMessage"]
    assert "reasoning item" not in str(detail)
    assert "reasoning summary" not in str(detail)
    assert "reasoning content" not in str(detail)


@pytest.mark.asyncio
async def test_read_thread_turns_preflights_metadata_and_maps_paginated_page(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    app.turns_response = {
        "data": [
            {
                "id": "turn-1",
                "status": "completed",
                "startedAt": 1,
                "completedAt": 2,
                "durationMs": 1,
                "itemsView": "notLoaded",
            }
        ],
        "nextCursor": "next",
        "backwardsCursor": "back",
    }

    result = await bridge.read_thread_turns(
        "paginated-thread", limit=2, cursor="cursor", sort_direction="asc"
    )

    assert app.calls == [
        ("thread/read", {"threadId": "paginated-thread", "includeTurns": False}),
        (
            "thread/turns/list",
            {
                "threadId": "paginated-thread",
                "limit": 2,
                "cursor": "cursor",
                "sortDirection": "asc",
                "itemsView": "notLoaded",
            },
        ),
    ]
    assert result["history_mode"] == "paginated"
    assert result["next_cursor"] == "next"
    assert result["backwards_cursor"] == "back"
    assert result["turns"][0]["items_view"] == "notLoaded"


@pytest.mark.asyncio
async def test_read_thread_items_preflights_metadata_and_preserves_entry_turn_id(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    app.items_response = {
        "data": [
            {
                "turnId": "turn-1",
                "item": {"id": "agent-1", "type": "agentMessage", "text": "hello"},
            }
        ],
        "nextCursor": "next",
        "backwardsCursor": "back",
    }

    result = await bridge.read_thread_items(
        "paginated-thread", turn_id="turn-1", limit=3, cursor="cursor", sort_direction="asc"
    )

    assert app.calls == [
        ("thread/read", {"threadId": "paginated-thread", "includeTurns": False}),
        (
            "thread/items/list",
            {
                "threadId": "paginated-thread",
                "turnId": "turn-1",
                "limit": 3,
                "cursor": "cursor",
                "sortDirection": "asc",
            },
        ),
    ]
    assert result["items"][0]["turn_id"] == "turn-1"
    assert "started_at_ms" not in result["items"][0]["item"]
    assert "completed_at_ms" not in result["items"][0]["item"]
    assert result["next_cursor"] == "next"
    assert result["backwards_cursor"] == "back"


@pytest.mark.asyncio
async def test_item_lifecycle_notifications_publish_transient_events_and_enrich_history(
    allowed_dir,
) -> None:
    bridge, app, _, activities = make_activity_bridge(allowed_dir)
    subscription = activities.subscribe("thread")
    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "startedAtMs": 1_791_101_948_000,
                "item": {"id": "tool-item", "type": "mcpToolCall", "status": "inProgress"},
            },
            "emittedAtMs": 1_700_000_000_000,
        }
    )
    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "completedAtMs": 1_791_101_963_000,
                "item": {
                    "id": "tool-item",
                    "type": "mcpToolCall",
                    "status": "completed",
                    "server": "github",
                    "tool": "fetch_file",
                    "result": "private output",
                },
            },
        }
    )

    started = await subscription.get()
    completed = await subscription.get()
    assert (started.type, completed.type) == ("item_started", "item_completed")
    assert (started.status, completed.status) == ("in_progress", "completed")
    assert activities.get_recent("thread") == ()

    app.thread_cwds["thread"] = str(allowed_dir)
    app.thread_history_modes["thread"] = "paginated"
    app.items_response = {
        "data": [
            {
                "turnId": "turn",
                "item": {
                    "id": "tool-item",
                    "type": "mcpToolCall",
                    "status": "completed",
                    "server": "github",
                    "tool": "fetch_file",
                    "result": "private output",
                },
            }
        ]
    }

    result = await bridge.read_thread_items("thread")
    item = result["items"][0]["item"]
    assert item["started_at_ms"] == 1_791_101_948_000
    assert item["completed_at_ms"] == 1_791_101_963_000
    assert "private output" not in str(result)
    subscription.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_item_completed_transient_event_preserves_item_status_and_stays_ephemeral(
    allowed_dir, status: str
) -> None:
    bridge, _, _, activities = make_activity_bridge(allowed_dir)
    subscription = activities.subscribe("thread")

    bridge.handle_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "item", "type": "mcpToolCall", "status": status},
            },
        }
    )

    event = await subscription.get()
    assert event.type == "item_completed"
    assert event.status == status
    assert (await bridge.status("thread", "turn"))["recent_activities"] == []
    subscription.close()


def test_item_lifecycle_uses_notification_emitted_timestamp_as_fallback(allowed_dir) -> None:
    bridge, _, _, _ = make_activity_bridge(allowed_dir)

    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread",
                "turnId": "turn",
                "item": {"id": "tool-item", "type": "dynamicToolCall"},
                "emittedAtMs": 1_791_101_948_000,
            },
            "emittedAtMs": 1_700_000_000_000,
        }
    )

    lifecycle = bridge._item_lifecycle.get("thread", "turn", "tool-item")
    assert lifecycle is not None
    assert lifecycle.started_at_ms == 1_791_101_948_000


@pytest.mark.asyncio
async def test_read_thread_items_adds_turn_model_metadata_from_thread_read_path(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    rollout_path = allowed_dir / "rollout.jsonl"
    write_rollout(
        rollout_path,
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "xhigh"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "xhigh"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol", "effort": "high"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "orphan", "model": "gpt-5.5", "effort": "medium"},
        },
    )
    app.thread_paths["paginated-thread"] = str(rollout_path)
    app.items_response = {
        "data": [
            {
                "turnId": "turn-1",
                "item": {"id": "agent-1", "type": "agentMessage", "text": "hello"},
            },
            {
                "turnId": "turn-2",
                "item": {"id": "user-1", "type": "userMessage", "content": []},
            },
        ],
        "nextCursor": None,
        "backwardsCursor": None,
    }

    result = await bridge.read_thread_items("paginated-thread")

    assert app.methods == ["thread/read", "thread/items/list"]
    assert result["turn_model_metadata"] == {
        "turn-1": {
            "model_candidates": [
                {"model": "gpt-5.6-luna", "reasoning_effort": "xhigh"},
                {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
            ],
            "model_resolution_status": "multiple",
        },
        "turn-2": {
            "model_candidates": [],
            "model_resolution_status": "unavailable",
        },
    }
    assert "orphan" not in result["turn_model_metadata"]
    assert str(rollout_path) not in str(result)


@pytest.mark.asyncio
async def test_read_thread_items_does_not_use_thread_list_path_for_metadata(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["paginated-thread"] = str(allowed_dir)
    app.thread_history_modes["paginated-thread"] = "paginated"
    listed_rollout = allowed_dir / "listed-rollout.jsonl"
    write_rollout(
        listed_rollout,
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-luna"}},
    )
    app.thread_list = [{"id": "paginated-thread", "path": str(listed_rollout)}]
    app.items_response = {
        "data": [
            {
                "turnId": "turn-1",
                "item": {"id": "item-1", "type": "agentMessage", "text": "safe"},
            }
        ]
    }

    result = await bridge.read_thread_items("paginated-thread")

    assert result["turn_model_metadata"]["turn-1"]["model_resolution_status"] == "unavailable"
    assert app.methods == ["thread/read", "thread/items/list"]


@pytest.mark.asyncio
async def test_read_legacy_items_adds_turn_model_metadata_and_fails_soft(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {
            "id": "turn-1",
            "items": [{"id": "item-1", "type": "agentMessage", "text": "safe"}],
        },
        {
            "id": "turn-2",
            "items": [{"id": "item-2", "type": "agentMessage", "text": "older"}],
        },
    ]
    app.thread_paths["legacy-thread"] = str(allowed_dir / "missing-rollout.jsonl")
    bridge.handle_notification(
        {
            "method": "item/started",
            "params": {
                "threadId": "legacy-thread",
                "turnId": "turn-1",
                "startedAtMs": 1_791_101_948_000,
                "item": {"id": "item-1", "type": "agentMessage"},
            },
        }
    )

    result = await bridge.read_thread_items("legacy-thread")

    assert [entry["turn_id"] for entry in result["items"]] == ["turn-2", "turn-1"]
    assert result["turn_model_metadata"] == {
        "turn-2": {"model_candidates": [], "model_resolution_status": "unavailable"},
        "turn-1": {"model_candidates": [], "model_resolution_status": "unavailable"},
    }
    assert "started_at_ms" not in result["items"][0]["item"]
    assert result["items"][1]["item"]["started_at_ms"] == 1_791_101_948_000
    assert app.methods == ["thread/read", "thread/read"]


@pytest.mark.asyncio
async def test_read_thread_history_uses_legacy_fallback_without_cursor(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"id": "agent-1", "type": "agentMessage", "text": "safe"},
                {"id": "reasoning-1", "type": "reasoning", "summary": ["private"]},
            ],
        }
    ]

    turns = await bridge.read_thread_turns("legacy-thread", limit=1)
    items = await bridge.read_thread_items("legacy-thread", limit=1)

    assert turns["history_mode"] == "legacy"
    assert turns["turns"][0]["items"][0]["text"] == "safe"
    assert items["items"][0]["item"]["text"] == "safe"
    assert app.methods == [
        "thread/read",
        "thread/read",
        "thread/read",
        "thread/read",
    ]
    assert app.calls[1][1] == {"threadId": "legacy-thread", "includeTurns": True}
    assert app.calls[3][1] == {"threadId": "legacy-thread", "includeTurns": True}
    assert turns["next_cursor"] is None
    assert items["next_cursor"] is None
    assert "private" not in str(turns)


@pytest.mark.asyncio
async def test_read_legacy_history_passes_sort_direction_to_projection(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {"id": "turn-1", "status": "completed", "items": []},
        {"id": "turn-2", "status": "completed", "items": []},
        {"id": "turn-3", "status": "completed", "items": []},
    ]

    descending = await bridge.read_thread_turns("legacy-thread", limit=2, sort_direction="desc")
    ascending = await bridge.read_thread_turns("legacy-thread", limit=2, sort_direction="asc")

    assert [turn["id"] for turn in descending["turns"]] == ["turn-3", "turn-2"]
    assert [turn["id"] for turn in ascending["turns"]] == ["turn-1", "turn-2"]


@pytest.mark.asyncio
async def test_read_legacy_items_passes_sort_direction_to_projection(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)
    app.thread_histories["legacy-thread"] = [
        {
            "id": "turn-1",
            "status": "completed",
            "items": [
                {"id": "item-1", "type": "agentMessage", "text": "old"},
                {"id": "private", "type": "reasoning", "summary": ["private"]},
                {"id": "item-2", "type": "agentMessage", "text": "new"},
            ],
        },
        {
            "id": "turn-2",
            "status": "completed",
            "items": [{"id": "item-3", "type": "agentMessage", "text": "newest"}],
        },
    ]

    descending = await bridge.read_thread_items("legacy-thread", limit=2, sort_direction="desc")
    ascending = await bridge.read_thread_items(
        "legacy-thread", turn_id="turn-1", limit=2, sort_direction="asc"
    )

    assert [entry["item"]["id"] for entry in descending["items"]] == ["item-3", "item-2"]
    assert [entry["item"]["id"] for entry in ascending["items"]] == ["item-1", "item-2"]


@pytest.mark.asyncio
async def test_read_legacy_history_rejects_cursor_after_metadata_preflight(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["legacy-thread"] = str(allowed_dir)

    with pytest.raises(HistoryValidationError, match="cursor"):
        await bridge.read_thread_turns("legacy-thread", cursor="fake")

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_read_history_rejects_invalid_query_without_rpc(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(HistoryValidationError):
        await bridge.read_thread_turns("thread", limit=0)
    with pytest.raises(HistoryValidationError):
        await bridge.read_thread_items("thread", sort_direction="sideways")
    with pytest.raises(HistoryValidationError, match="cursor"):
        await bridge.read_thread_items("thread", cursor=123)  # type: ignore[arg-type]

    assert app.methods == []


@pytest.mark.asyncio
async def test_read_paginated_history_rejects_disallowed_thread_before_history_rpc(
    allowed_dir, tmp_path
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside-history"
    outside.mkdir()
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["outside-thread"] = str(outside)
    app.thread_history_modes["outside-thread"] = "paginated"

    with pytest.raises(PathPolicyError):
        await bridge.read_thread_items("outside-thread")

    assert app.methods == ["thread/read"]
