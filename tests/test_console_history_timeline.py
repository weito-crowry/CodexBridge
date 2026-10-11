from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any, cast

from codex_bridge.console.history_timeline import HistoryTimelineState
from codex_bridge.console.usage import CodexUsage, CodexUsageSnapshot
from codex_bridge.console.widgets import HistoryPane, TimelineEntry


class _HistoryPaneRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def set_timeline(
        self,
        entries: list[TimelineEntry],
        *,
        has_older: bool = False,
        turn_statuses: dict[str, str] | None = None,
        turn_model_metadata: dict[str, Any] | None = None,
        turn_usage_snapshots: dict[str, CodexUsageSnapshot] | None = None,
        prepend: bool = False,
    ) -> None:
        self.calls.append(
            {
                "entries": list(entries),
                "has_older": has_older,
                "turn_statuses": turn_statuses,
                "turn_model_metadata": turn_model_metadata,
                "turn_usage_snapshots": turn_usage_snapshots,
                "prepend": prepend,
            }
        )


def _item(turn_id: str, item_id: str, text: str) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "item": {"id": item_id, "type": "agentMessage", "text": text},
    }


def _metadata(model: str) -> dict[str, object]:
    return {
        "model_candidates": [{"model": model, "reasoning_effort": "high"}],
        "model_resolution_status": "resolved",
    }


def test_snapshot_live_refresh_and_explicit_snapshot_update_statuses_and_entries() -> None:
    state = HistoryTimelineState()
    assert (
        state.apply_turns(
            {
                "turns": [
                    {"id": "turn-old", "status": "completed"},
                    {"id": "turn-latest", "status": "in_progress"},
                ]
            },
            live_refresh=False,
        )
        is True
    )
    assert state.apply_items(
        {
            "items": [
                _item("turn-old", "old", "old answer"),
                _item("turn-latest", "latest", "first answer"),
            ]
        },
        prepend=False,
    )

    assert (
        state.apply_turns(
            {"turns": [{"id": "turn-latest", "status": "completed"}]},
            live_refresh=True,
        )
        is False
    )
    assert state.turn_statuses == {
        "turn-old": "completed",
        "turn-latest": "completed",
    }
    assert state.apply_items(
        {
            "items": [
                _item("turn-latest", "latest", "updated answer"),
                _item("turn-latest", "new", "new answer"),
            ]
        },
        prepend=False,
        live_refresh=True,
    )
    assert [(entry.item_id, entry.body) for entry in state.entries] == [
        ("latest", "updated answer"),
        ("old", "old answer"),
        ("new", "new answer"),
    ]

    assert (
        state.apply_turns(
            {"turns": [{"id": "turn-latest", "status": "failed"}]},
            live_refresh=False,
        )
        is False
    )
    assert state.turn_statuses == {"turn-latest": "failed"}
    assert state.apply_items(
        {"items": [_item("turn-latest", "snapshot", "snapshot answer")]},
        prepend=False,
    )
    assert [(entry.item_id, entry.body) for entry in state.entries] == [
        ("snapshot", "snapshot answer")
    ]


def test_older_items_prepend_without_duplicate_ids_and_merge_metadata() -> None:
    state = HistoryTimelineState()
    assert state.apply_items(
        {
            "items": [
                _item("turn-current", "current", "current"),
                _item("turn-current", "latest", "latest"),
            ],
            "next_cursor": "older-1",
            "turn_model_metadata": {"turn-current": _metadata("model-current")},
        },
        prepend=False,
    )
    assert state.apply_items(
        {
            "items": [
                _item("turn-old", "old", "old"),
                _item("turn-current", "latest", "duplicate"),
            ],
            "next_cursor": "older-2",
            "turn_model_metadata": {"turn-old": _metadata("model-old")},
        },
        prepend=True,
    )

    assert [(entry.turn_id, entry.item_id) for entry in state.entries] == [
        ("turn-old", "old"),
        ("turn-current", "latest"),
        ("turn-current", "current"),
    ]
    assert state.next_cursor == "older-2"
    assert state.has_loaded_older
    assert set(state.turn_model_metadata) == {"turn-current", "turn-old"}


def test_live_refresh_after_older_load_preserves_older_cursor() -> None:
    state = HistoryTimelineState()
    assert state.apply_items(
        {"items": [_item("turn-a", "new", "new")], "next_cursor": "older-page"},
        prepend=False,
    )
    assert state.apply_items(
        {"items": [_item("turn-old", "old", "old")], "next_cursor": "older-next"},
        prepend=True,
    )

    assert state.apply_items(
        {"items": [_item("turn-a", "newer", "newer")], "next_cursor": "live-cursor"},
        prepend=False,
        live_refresh=True,
    )
    assert state.next_cursor == "older-next"
    assert [(entry.item_id) for entry in state.entries] == ["old", "new", "newer"]


def test_invalid_payloads_preserve_state_and_do_not_render() -> None:
    state = HistoryTimelineState()
    assert (
        state.apply_turns(
            {"turns": [{"id": "turn-a", "status": "completed"}]},
            live_refresh=False,
        )
        is False
    )
    assert state.apply_items(
        {
            "items": [_item("turn-a", "item-a", "kept")],
            "next_cursor": "older",
            "turn_model_metadata": {"turn-a": _metadata("model-a")},
        },
        prepend=False,
    )
    before = (
        list(state.entries),
        dict(state.turn_statuses),
        dict(state.turn_model_metadata),
        state.next_cursor,
        state.has_loaded_older,
    )
    pane = _HistoryPaneRecorder()

    items_applied = state.apply_items(None, prepend=False)
    turns_applied = state.apply_turns(None, live_refresh=False)
    if items_applied:
        state.render(
            cast(HistoryPane, pane),
            selected_thread_id="thread-a",
            usage_snapshots={},
        )
    if turns_applied is not None:
        state.render(
            cast(HistoryPane, pane),
            selected_thread_id="thread-a",
            usage_snapshots={},
        )

    assert items_applied is False
    assert turns_applied is None
    assert (
        state.entries,
        state.turn_statuses,
        state.turn_model_metadata,
        state.next_cursor,
        state.has_loaded_older,
    ) == before
    assert pane.calls == []


def test_render_passes_only_selected_thread_usage_snapshots() -> None:
    state = HistoryTimelineState()
    assert state.apply_items(
        {
            "items": [
                _item("turn-a", "item-a", "answer a"),
                _item("turn-b", "item-b", "answer b"),
            ],
            "next_cursor": "older",
        },
        prepend=False,
    )
    pane = _HistoryPaneRecorder()
    selected_snapshot = CodexUsageSnapshot(CodexUsage(), datetime.now(UTC))
    other_snapshot = CodexUsageSnapshot(CodexUsage(), datetime.now(UTC))

    state.render(
        cast(HistoryPane, pane),
        selected_thread_id="thread-a",
        usage_snapshots=OrderedDict(
            [
                (("thread-a", "turn-a"), selected_snapshot),
                (("thread-b", "turn-b"), other_snapshot),
                (("thread-a", "turn-missing"), None),
            ]
        ),
        prepend=True,
    )

    assert len(pane.calls) == 1
    call = pane.calls[0]
    assert call["turn_usage_snapshots"] == {"turn-a": selected_snapshot}
    assert call["entries"] == state.entries
    assert call["has_older"] is True
    assert call["turn_statuses"] == state.turn_statuses
    assert call["turn_model_metadata"] == state.turn_model_metadata
    assert call["prepend"] is True


def test_reset_clears_timeline_status_metadata_and_cursor_state() -> None:
    state = HistoryTimelineState()
    state.apply_turns({"turns": [{"id": "turn-a", "status": "completed"}]}, live_refresh=False)
    state.apply_items(
        {
            "items": [_item("turn-a", "item-a", "answer")],
            "next_cursor": "older",
            "turn_model_metadata": {"turn-a": _metadata("model-a")},
        },
        prepend=False,
    )
    state.apply_items({"items": [_item("turn-old", "item-old", "old")]}, prepend=True)

    state.reset()

    assert state.entries == []
    assert state.turn_model_metadata == {}
    assert state.turn_statuses == {}
    assert state.next_cursor is None
    assert state.has_loaded_older is False
