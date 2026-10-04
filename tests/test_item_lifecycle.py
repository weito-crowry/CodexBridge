from __future__ import annotations

from codex_bridge.item_lifecycle import ItemLifecycleStore


def test_item_lifecycle_store_keeps_start_and_completion_independently() -> None:
    store = ItemLifecycleStore()

    store.observe_started("thread", "turn", "item-start-only", 1_791_101_948_000)
    store.observe_completed("thread", "turn", "item-complete-only", 1_791_101_963_000)

    start_only = store.get("thread", "turn", "item-start-only")
    complete_only = store.get("thread", "turn", "item-complete-only")
    assert start_only is not None
    assert (start_only.started_at_ms, start_only.completed_at_ms) == (1_791_101_948_000, None)
    assert complete_only is not None
    assert (complete_only.started_at_ms, complete_only.completed_at_ms) == (None, 1_791_101_963_000)


def test_item_lifecycle_store_rejects_bool_and_malformed_timestamps() -> None:
    store = ItemLifecycleStore()

    store.observe_started("thread", "turn", "bool", True)
    store.observe_started("thread", "turn", "string", "123")
    store.observe_started("thread", "turn", "negative", -1)
    store.observe_completed("thread", "turn", "huge", 10**100)

    assert store.get("thread", "turn", "bool") is None
    assert store.get("thread", "turn", "string") is None
    assert store.get("thread", "turn", "negative") is None
    assert store.get("thread", "turn", "huge") is None


def test_item_lifecycle_store_evicts_oldest_item_at_capacity() -> None:
    store = ItemLifecycleStore()

    for index in range(4_097):
        store.observe_started("thread", "turn", f"item-{index}", index)

    assert store.get("thread", "turn", "item-0") is None
    assert store.get("thread", "turn", "item-1") is not None
    assert store.get("thread", "turn", "item-4096") is not None
