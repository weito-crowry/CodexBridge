from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import quote

from PySide6.QtWidgets import QInputDialog, QLineEdit

from .codex_links import codex_thread_uri
from .widgets import TimelineEntry, format_thread_content, timeline_entries

if TYPE_CHECKING:
    from .main_window import MainWindow


class ThreadInteractionController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._pending_rename_names: dict[str, str] = {}
        self._thread_copy_sequence = 0
        self._pending_thread_copies: dict[str, tuple[str, list[TimelineEntry]]] = {}

    def _rename_thread(
        self,
        thread_id: str,
        *,
        get_text: Callable[..., tuple[str, bool]] | None = None,
    ) -> None:
        window = self._window
        name, accepted = (get_text or QInputDialog.getText)(
            window,
            "名前を変更",
            "名前:",
            QLineEdit.EchoMode.Normal,
            window.thread_pane.thread_name(thread_id),
        )
        if not accepted or not name.strip():
            return
        key = f"rename:{thread_id}"
        if key in self._pending_rename_names:
            return
        if window._client.post_json(
            f"/ui-api/threads/{quote(thread_id, safe='')}/name",
            {"name": name},
            key=key,
        ):
            self._pending_rename_names[key] = name

    def apply_rename_success(self, key: str) -> None:
        name = self._pending_rename_names.pop(key, None)
        if name is not None:
            self._window.thread_pane.update_thread_name(key.removeprefix("rename:"), name)

    def discard_rename(self, key: str) -> None:
        self._pending_rename_names.pop(key, None)

    def _open_thread_in_codex(self, thread_id: str) -> None:
        window = self._window
        try:
            opened = window._codex_uri_opener(codex_thread_uri(thread_id))
        except Exception:
            opened = False
        if not opened:
            window.bottom_status_label.setText(
                "Could not open thread in Codex App. "
                "Check that the codex:// URI handler is available."
            )

    def _copy_thread_content(self, thread_id: str) -> None:
        self._thread_copy_sequence += 1
        key = f"copy-thread:{self._thread_copy_sequence}"
        self._pending_thread_copies[key] = (thread_id, [])
        if not self._request_thread_copy_page(key, thread_id):
            self._pending_thread_copies.pop(key, None)

    def _request_thread_copy_page(
        self, key: str, thread_id: str, cursor: str | None = None
    ) -> bool:
        query: dict[str, object] = {"limit": 100, "sort_direction": "desc"}
        if cursor is not None:
            query["cursor"] = cursor
        return self._window._client.get_json(
            f"/ui-api/threads/{quote(thread_id, safe='')}/items",
            key=key,
            query=query,
        )

    def _apply_thread_copy_page(
        self,
        key: str,
        payload: object,
        *,
        copy_to_clipboard: Callable[[str], None],
    ) -> None:
        pending = self._pending_thread_copies.get(key)
        if pending is None or not isinstance(payload, Mapping):
            self._pending_thread_copies.pop(key, None)
            return
        thread_id, entries = pending
        existing = {(entry.turn_id, entry.item_id) for entry in entries}
        page_entries = tuple(
            entry
            for entry in timeline_entries(payload)
            if (entry.turn_id, entry.item_id) not in existing
        )
        entries[0:0] = page_entries
        next_cursor = payload.get("next_cursor")
        if isinstance(next_cursor, str) and next_cursor:
            if self._request_thread_copy_page(key, thread_id, next_cursor):
                return
            self._pending_thread_copies.pop(key, None)
            return
        copy_to_clipboard(format_thread_content(entries))
        self._pending_thread_copies.pop(key, None)

    def discard_copy(self, key: str) -> None:
        self._pending_thread_copies.pop(key, None)

    def _thread_path(self, suffix: str = "") -> str:
        window = self._window
        assert window._selected_thread_id is not None
        return f"/ui-api/threads/{quote(window._selected_thread_id, safe='')}{suffix}"

    def _request_snapshot(self, generation: int) -> None:
        window = self._window
        if generation != window._selection_generation or window._selected_thread_id is None:
            return
        prefix = f"selection:{generation}:"
        window._client.get_json(self._thread_path(), key=prefix + "detail")
        window._request_history_snapshot(generation)
        window._client.get_json(
            self._thread_path("/status"),
            key=prefix + "status",
            query={"activity_limit": 50},
        )

    def _request_selected_status(self) -> None:
        window = self._window
        if window._selected_thread_id is None:
            return
        key = f"selection:{window._selection_generation}:status"
        window._client.get_json(self._thread_path("/status"), key=key, query={"activity_limit": 50})

    def select_thread(self, thread_id: str | None) -> None:
        window = self._window
        window._selection_generation += 1
        generation = window._selection_generation
        window._selected_thread_id = thread_id
        window._history_requests.reset()
        window._history_stream.reset_selection(thread_id)
        window._history_timeline.reset()
        window._client.abort_json_group("selection:")
        window._client.stop_stream()
        window.selected_status_timer.stop()
        if thread_id is None:
            window.stream_status_label.setText("Stream: idle")
            window._sync_empty_state()
            return
        window.history_pane.set_empty_state("Loading history…")
        window.activity_pane.set_loading_state("Loading activity…")
        window.selected_status_timer.start()
        self._request_snapshot(generation)

    @staticmethod
    def _selection_key(key: str) -> tuple[int, str] | None:
        parts = key.split(":", 2)
        if len(parts) != 3 or parts[0] != "selection":
            return None
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return None

    def _is_current_selection(self, key: str) -> tuple[int, str] | None:
        parsed = self._selection_key(key)
        if parsed is None or parsed[0] != self._window._selection_generation:
            return None
        return parsed
