from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote

from PySide6.QtCore import QTimer

HISTORY_REFRESH_DEBOUNCE_MS = 250


class HistoryJsonClient(Protocol):
    def get_json(
        self,
        path: str,
        *,
        key: str,
        query: Mapping[str, object] | None = None,
    ) -> bool: ...


@dataclass(frozen=True)
class HistoryRequestContext:
    generation: int
    selected_thread_id: str | None
    next_cursor: str | None
    closing: bool


class HistoryRequestCoordinator:
    def __init__(self, client: HistoryJsonClient, refresh_timer: QTimer) -> None:
        self._client = client
        self._refresh_timer = refresh_timer
        self._pending_live = False
        self._generation: int | None = None
        self._in_flight_keys: set[str] = set()
        self._snapshot_pending = False
        self._older_pending = False

    @property
    def pending_live(self) -> bool:
        return self._pending_live

    @property
    def in_flight(self) -> bool:
        return bool(self._in_flight_keys)

    def request_snapshot(self, generation: int, context: HistoryRequestContext) -> None:
        if generation != context.generation or context.selected_thread_id is None:
            return
        self._refresh_timer.stop()
        self._pending_live = False
        if self._in_flight_keys:
            self._snapshot_pending = True
            return
        self._start_group(context, "snapshot")

    def schedule_selected_history_refresh(self, context: HistoryRequestContext) -> None:
        if context.closing or context.selected_thread_id is None:
            return
        self._refresh_timer.start(HISTORY_REFRESH_DEBOUNCE_MS)

    def request_selected_history(self, context: HistoryRequestContext) -> None:
        if context.closing or context.selected_thread_id is None:
            return
        if self._in_flight_keys:
            self._pending_live = True
            return
        self._start_group(context, "live")

    def request_older(self, context: HistoryRequestContext) -> None:
        if context.selected_thread_id is None or context.next_cursor is None:
            return
        if self._in_flight_keys:
            self._older_pending = True
            return
        self._start_group(context, "older")

    def finish_request(self, key: str, context: HistoryRequestContext) -> None:
        if key not in self._in_flight_keys:
            return
        self._in_flight_keys.discard(key)
        if self._in_flight_keys:
            return

        generation = self._generation
        self._generation = None
        if generation is not None:
            self._dispatch_pending(generation, context)

    def reset(self) -> None:
        self._refresh_timer.stop()
        self._pending_live = False
        self._generation = None
        self._in_flight_keys.clear()
        self._snapshot_pending = False
        self._older_pending = False

    def _start_group(
        self,
        context: HistoryRequestContext,
        kind: Literal["snapshot", "older", "live"],
    ) -> None:
        thread_id = context.selected_thread_id
        if context.closing or thread_id is None or self._in_flight_keys:
            return

        prefix = f"selection:{context.generation}:"
        if kind == "live":
            prefix += "history:"
        thread_path = f"/ui-api/threads/{quote(thread_id, safe='')}"
        requests: tuple[tuple[str, str, dict[str, object]], ...]
        if kind == "older":
            cursor = context.next_cursor
            if cursor is None:
                return
            requests = (
                (
                    prefix + f"older:{cursor}",
                    thread_path + "/items",
                    {"limit": 100, "sort_direction": "desc", "cursor": cursor},
                ),
            )
        else:
            requests = (
                (
                    prefix + "turns",
                    thread_path + "/turns",
                    {"limit": 20, "sort_direction": "desc"},
                ),
                (
                    prefix + "items",
                    thread_path + "/items",
                    {"limit": 100, "sort_direction": "desc"},
                ),
            )

        self._generation = context.generation
        self._in_flight_keys = {key for key, _, _ in requests}
        if kind == "live":
            self._pending_live = False
        rejected_keys: list[str] = []
        for key, path, query in requests:
            if not self._client.get_json(path, key=key, query=query):
                rejected_keys.append(key)
        for key in rejected_keys:
            self.finish_request(key, context)

    def _dispatch_pending(self, generation: int, context: HistoryRequestContext) -> None:
        if (
            generation != context.generation
            or context.closing
            or context.selected_thread_id is None
            or self._in_flight_keys
        ):
            return
        if self._snapshot_pending:
            self._snapshot_pending = False
            self._start_group(context, "snapshot")
            return
        if self._older_pending:
            self._older_pending = False
            self._start_group(context, "older")
            if self._in_flight_keys:
                return
        if self._pending_live and not self._refresh_timer.isActive():
            self._refresh_timer.start(HISTORY_REFRESH_DEBOUNCE_MS)
