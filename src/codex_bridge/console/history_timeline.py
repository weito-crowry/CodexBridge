from __future__ import annotations

from collections.abc import Mapping

from .history_metadata import _turn_model_metadata_from_payload
from .usage import CodexUsageSnapshot
from .widgets import HistoryPane, TimelineEntry, timeline_entries

ACTIVE_TURN_STATES = frozenset(
    {
        "in_progress",
        "needs_approval",
        "needs_input",
        "needs_user_input",
    }
)


class HistoryTimelineState:
    def __init__(self) -> None:
        self._entries: list[TimelineEntry] = []
        self._turn_model_metadata: dict[str, Mapping[str, object]] = {}
        self._turn_statuses: dict[str, str] = {}
        self._next_cursor: str | None = None
        self._has_loaded_older = False

    @property
    def entries(self) -> list[TimelineEntry]:
        return self._entries

    @entries.setter
    def entries(self, value: list[TimelineEntry]) -> None:
        self._entries = value

    @property
    def turn_model_metadata(self) -> dict[str, Mapping[str, object]]:
        return self._turn_model_metadata

    @property
    def turn_statuses(self) -> dict[str, str]:
        return self._turn_statuses

    @property
    def next_cursor(self) -> str | None:
        return self._next_cursor

    @property
    def has_loaded_older(self) -> bool:
        return self._has_loaded_older

    def reset(self) -> None:
        self._entries = []
        self._turn_model_metadata = {}
        self._turn_statuses = {}
        self._next_cursor = None
        self._has_loaded_older = False

    def apply_turns(self, payload: object, *, live_refresh: bool) -> bool | None:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("turns"), list):
            return None
        turns = payload["turns"]
        refreshed_statuses = {
            turn["id"]: turn["status"]
            for turn in turns
            if isinstance(turn, Mapping)
            and isinstance(turn.get("id"), str)
            and isinstance(turn.get("status"), str)
        }
        if live_refresh:
            self._turn_statuses.update(refreshed_statuses)
        else:
            self._turn_statuses = refreshed_statuses
        return any(status in ACTIVE_TURN_STATES for status in refreshed_statuses.values())

    def apply_items(
        self,
        payload: object,
        *,
        prepend: bool,
        live_refresh: bool = False,
    ) -> bool:
        if not isinstance(payload, Mapping):
            return False
        new_entries = timeline_entries(payload)
        page_metadata = _turn_model_metadata_from_payload(payload)
        if prepend:
            self._turn_model_metadata.update(page_metadata)
            self._has_loaded_older = True
        elif live_refresh:
            self._turn_model_metadata.update(page_metadata)
        else:
            self._has_loaded_older = False
            self._turn_model_metadata = page_metadata
        if prepend:
            existing = {(entry.turn_id, entry.item_id) for entry in self._entries}
            new_entries = tuple(
                entry for entry in new_entries if (entry.turn_id, entry.item_id) not in existing
            )
            self._entries = list(new_entries) + self._entries
        elif live_refresh:
            refreshed_by_id = {(entry.turn_id, entry.item_id): entry for entry in new_entries}
            existing_ids = {(entry.turn_id, entry.item_id) for entry in self._entries}
            self._entries = [
                refreshed_by_id.pop((entry.turn_id, entry.item_id), entry)
                for entry in self._entries
            ]
            self._entries.extend(
                entry for entry in new_entries if (entry.turn_id, entry.item_id) not in existing_ids
            )
        else:
            self._entries = list(new_entries)
        if not live_refresh or not self._has_loaded_older:
            next_cursor = payload.get("next_cursor")
            self._next_cursor = next_cursor if isinstance(next_cursor, str) else None
        return True

    def render(
        self,
        history_pane: HistoryPane,
        *,
        selected_thread_id: str | None,
        usage_snapshots: Mapping[tuple[str, str], CodexUsageSnapshot | None],
        prepend: bool = False,
    ) -> None:
        turn_usage_snapshots = {
            turn_id: snapshot
            for (thread_id, turn_id), snapshot in usage_snapshots.items()
            if thread_id == selected_thread_id and snapshot is not None
        }
        history_pane.set_timeline(
            self._entries,
            has_older=self._next_cursor is not None,
            turn_statuses=self._turn_statuses,
            turn_model_metadata=self._turn_model_metadata,
            turn_usage_snapshots=turn_usage_snapshots,
            prepend=prepend,
        )
