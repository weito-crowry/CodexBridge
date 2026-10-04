from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

_MAX_LIFECYCLE_ITEMS = 4_096
_MAX_EPOCH_MILLISECONDS = 253_402_300_799_999


def _timestamp(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_EPOCH_MILLISECONDS
    ):
        return None
    return value


@dataclass(frozen=True, slots=True)
class ItemLifecycle:
    started_at_ms: int | None = None
    completed_at_ms: int | None = None


class ItemLifecycleStore:
    """Bounded process-local item lifecycle timestamps indexed by native identity."""

    def __init__(self) -> None:
        self._items: OrderedDict[tuple[str, str, str], ItemLifecycle] = OrderedDict()

    def observe_started(
        self, thread_id: str, turn_id: str, item_id: str, started_at_ms: object
    ) -> None:
        timestamp = _timestamp(started_at_ms)
        if timestamp is None:
            return
        key = (thread_id, turn_id, item_id)
        previous = self._items.get(key)
        self._items[key] = ItemLifecycle(
            started_at_ms=timestamp,
            completed_at_ms=previous.completed_at_ms if previous is not None else None,
        )
        self._trim_and_mark_recent(key)

    def observe_completed(
        self, thread_id: str, turn_id: str, item_id: str, completed_at_ms: object
    ) -> None:
        timestamp = _timestamp(completed_at_ms)
        if timestamp is None:
            return
        key = (thread_id, turn_id, item_id)
        previous = self._items.get(key)
        self._items[key] = ItemLifecycle(
            started_at_ms=previous.started_at_ms if previous is not None else None,
            completed_at_ms=timestamp,
        )
        self._trim_and_mark_recent(key)

    def get(self, thread_id: str, turn_id: str, item_id: str) -> ItemLifecycle | None:
        key = (thread_id, turn_id, item_id)
        lifecycle = self._items.get(key)
        if lifecycle is not None:
            self._items.move_to_end(key)
        return lifecycle

    def _trim_and_mark_recent(self, key: tuple[str, str, str]) -> None:
        self._items.move_to_end(key)
        while len(self._items) > _MAX_LIFECYCLE_ITEMS:
            self._items.popitem(last=False)
