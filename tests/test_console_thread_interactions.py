from __future__ import annotations

import pytest

from codex_bridge.console import thread_interactions
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.thread_interactions import ThreadInteractionController


class FakeClient:
    def __init__(self) -> None:
        self.get_result = True
        self.post_result = True
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.posts: list[tuple[str, str, dict[str, object]]] = []
        self.aborted_groups: list[str] = []
        self.stopped_streams = 0

    def get_json(self, path: str, *, key: str, query: dict[str, object] | None = None) -> bool:
        self.requests.append((key, path, query))
        return self.get_result

    def post_json(self, path: str, payload: dict[str, object], *, key: str) -> bool:
        self.posts.append((key, path, payload))
        return self.post_result

    def abort_json_group(self, prefix: str) -> None:
        self.aborted_groups.append(prefix)

    def stop_stream(self) -> None:
        self.stopped_streams += 1


class FakeThreadPane:
    def __init__(self) -> None:
        self.names = {"thread/a": "Current name"}
        self.updated_names: list[tuple[str, str]] = []

    def thread_name(self, thread_id: str) -> str:
        return self.names[thread_id]

    def update_thread_name(self, thread_id: str, name: str) -> None:
        self.updated_names.append((thread_id, name))


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text

    def set_empty_state(self, text: str) -> None:
        self.text = text

    def set_loading_state(self, text: str) -> None:
        self.text = text


class FakeTimer:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


class FakeHistoryRequests:
    def __init__(self) -> None:
        self.resets = 0
        self.snapshots: list[int] = []

    def reset(self) -> None:
        self.resets += 1


class FakeHistoryStream:
    def __init__(self) -> None:
        self.selections: list[str | None] = []

    def reset_selection(self, thread_id: str | None) -> None:
        self.selections.append(thread_id)


class FakeHistoryTimeline:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


class FakeWindow:
    def __init__(self) -> None:
        self._client = FakeClient()
        self.thread_pane = FakeThreadPane()
        self.bottom_status_label = FakeLabel()
        self._codex_uri_opener = lambda _uri: True
        self._pending_expected_only_for_type_checking = None
        self._selection_generation = 0
        self._selected_thread_id: str | None = None
        self._history_requests = FakeHistoryRequests()
        self._history_stream = FakeHistoryStream()
        self._history_timeline = FakeHistoryTimeline()
        self.selected_status_timer = FakeTimer()
        self.stream_status_label = FakeLabel()
        self.history_pane = FakeLabel()
        self.activity_pane = FakeLabel()
        self.history_snapshots: list[int] = []
        self.empty_state_syncs = 0

    def _history_request_context(self) -> object:
        return object()

    def _request_history_snapshot(self, generation: int) -> None:
        self.history_snapshots.append(generation)

    def _sync_empty_state(self) -> None:
        self.empty_state_syncs += 1


def make_controller() -> tuple[FakeWindow, ThreadInteractionController]:
    window = FakeWindow()
    return window, ThreadInteractionController(window)  # type: ignore[arg-type]


def test_main_window_state_properties_expose_controller_state_read_only() -> None:
    window, controller = make_controller()
    window._thread_interactions = controller

    assert MainWindow._pending_rename_names.__get__(window) is controller._pending_rename_names
    assert MainWindow._pending_thread_copies.__get__(window) is controller._pending_thread_copies
    assert MainWindow._thread_copy_sequence.__get__(window) == 0
    assert MainWindow._pending_rename_names.fset is None
    assert MainWindow._pending_thread_copies.fset is None
    assert MainWindow._thread_copy_sequence.fset is None

    controller._thread_copy_sequence += 1

    assert MainWindow._thread_copy_sequence.__get__(window) == 1


def test_rename_posts_encoded_thread_name_and_tracks_only_accepted_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, controller = make_controller()
    dialog_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        thread_interactions.QInputDialog,
        "getText",
        lambda *args: dialog_calls.append(args) or ("  New name  ", True),
    )

    controller._rename_thread("thread/a")

    assert dialog_calls == [
        (
            window,
            "名前を変更",
            "名前:",
            thread_interactions.QLineEdit.EchoMode.Normal,
            "Current name",
        )
    ]
    assert window._client.posts == [
        ("rename:thread/a", "/ui-api/threads/thread%2Fa/name", {"name": "  New name  "})
    ]
    assert controller._pending_rename_names == {"rename:thread/a": "  New name  "}


@pytest.mark.parametrize("dialog_result", [("", False), ("   ", True)])
def test_rename_cancel_or_blank_does_not_post(
    monkeypatch: pytest.MonkeyPatch, dialog_result: tuple[str, bool]
) -> None:
    window, controller = make_controller()
    monkeypatch.setattr(thread_interactions.QInputDialog, "getText", lambda *_args: dialog_result)

    controller._rename_thread("thread/a")

    assert window._client.posts == []
    assert controller._pending_rename_names == {}


def test_rename_duplicate_pending_request_is_not_posted_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, controller = make_controller()
    dialog_results = iter([("First", True), ("Second", True)])
    monkeypatch.setattr(
        thread_interactions.QInputDialog, "getText", lambda *_args: next(dialog_results)
    )

    controller._rename_thread("thread/a")
    controller._rename_thread("thread/a")

    assert len(window._client.posts) == 1
    assert controller._pending_rename_names == {"rename:thread/a": "First"}


def test_rename_rejected_post_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    window, controller = make_controller()
    window._client.post_result = False
    monkeypatch.setattr(thread_interactions.QInputDialog, "getText", lambda *_args: ("Name", True))

    controller._rename_thread("thread/a")

    assert window._client.posts == [
        ("rename:thread/a", "/ui-api/threads/thread%2Fa/name", {"name": "Name"})
    ]
    assert controller._pending_rename_names == {}


def test_rename_result_and_error_clear_pending_name() -> None:
    window, controller = make_controller()
    controller._pending_rename_names["rename:thread/a"] = "Renamed"

    controller.apply_rename_success("rename:thread/a")

    assert window.thread_pane.updated_names == [("thread/a", "Renamed")]
    assert controller._pending_rename_names == {}

    controller._pending_rename_names["rename:thread/a"] = "Rejected"
    controller.discard_rename("rename:thread/a")

    assert window.thread_pane.updated_names == [("thread/a", "Renamed")]
    assert controller._pending_rename_names == {}


@pytest.mark.parametrize("opener_result", [True, False, RuntimeError("unavailable")])
def test_open_thread_uses_uri_and_keeps_selection(
    opener_result: bool | Exception,
) -> None:
    window, controller = make_controller()
    window._selected_thread_id = "selected-thread"
    opened: list[str] = []

    def opener(uri: str) -> bool:
        opened.append(uri)
        if isinstance(opener_result, Exception):
            raise opener_result
        return opener_result

    window._codex_uri_opener = opener
    controller._open_thread_in_codex("thread/a")

    assert opened == ["codex://threads/thread/a"]
    assert window._selected_thread_id == "selected-thread"
    assert window.bottom_status_label.text == (
        ""
        if opener_result is True
        else "Could not open thread in Codex App. Check that the codex:// URI handler is available."
    )


def _item(item_id: str, item_type: str, text: str) -> dict[str, object]:
    return {"id": item_id, "type": item_type, "text": text}


def _entry(turn_id: str, item: dict[str, object]) -> dict[str, object]:
    return {"turn_id": turn_id, "item": item}


def test_copy_pages_prepend_older_items_deduplicate_and_use_callback() -> None:
    window, controller = make_controller()
    copied: list[str] = []
    controller._copy_thread_content("thread/a")
    key = "copy-thread:1"

    assert window._client.requests == [
        (key, "/ui-api/threads/thread%2Fa/items", {"limit": 100, "sort_direction": "desc"})
    ]
    controller._apply_thread_copy_page(
        key,
        {
            "items": [_entry("turn-1", _item("agent", "agentMessage", "New"))],
            "next_cursor": "older",
        },
        copy_to_clipboard=copied.append,
    )
    assert window._client.requests[-1] == (
        key,
        "/ui-api/threads/thread%2Fa/items",
        {"limit": 100, "sort_direction": "desc", "cursor": "older"},
    )
    controller._apply_thread_copy_page(
        key,
        {
            "items": [
                _entry("turn-1", _item("user", "userMessage", "Old")),
                _entry("turn-1", _item("agent", "agentMessage", "New duplicate")),
            ]
        },
        copy_to_clipboard=copied.append,
    )

    assert copied == ["User:\nOld\n\nAgent:\nNew"]
    assert key not in controller._pending_thread_copies


def test_copy_initial_request_failure_discards_pending_copy() -> None:
    window, controller = make_controller()
    window._client.get_result = False

    controller._copy_thread_content("thread-a")

    assert controller._thread_copy_sequence == 1
    assert controller._pending_thread_copies == {}


def test_copy_next_page_request_failure_discards_pending_copy() -> None:
    window, controller = make_controller()
    window._client.get_result = True
    controller._copy_thread_content("thread-a")
    key = "copy-thread:1"
    window._client.get_result = False
    copied: list[str] = []

    controller._apply_thread_copy_page(
        key,
        {"items": [], "next_cursor": "older"},
        copy_to_clipboard=copied.append,
    )

    assert copied == []
    assert key not in controller._pending_thread_copies


@pytest.mark.parametrize("payload", [None, [], {"items": "invalid"}])
def test_copy_invalid_payload_discards_pending_copy(payload: object) -> None:
    window, controller = make_controller()
    controller._copy_thread_content("thread-a")
    key = "copy-thread:1"

    controller._apply_thread_copy_page(key, payload, copy_to_clipboard=lambda _text: None)

    assert key not in controller._pending_thread_copies


def test_copy_error_discards_pending_copy() -> None:
    window, controller = make_controller()
    controller._copy_thread_content("thread-a")
    key = "copy-thread:1"

    controller.discard_copy(key)

    assert controller._pending_thread_copies == {}


def test_selection_lifecycle_resets_old_state_and_requests_current_snapshot() -> None:
    window, controller = make_controller()

    controller.select_thread("thread/a")

    assert window._selection_generation == 1
    assert window._selected_thread_id == "thread/a"
    assert window._history_requests.resets == 1
    assert window._history_stream.selections == ["thread/a"]
    assert window._history_timeline.resets == 1
    assert window._client.aborted_groups == ["selection:"]
    assert window._client.stopped_streams == 1
    assert window.selected_status_timer.events == ["stop", "start"]
    assert window.history_pane.text == "Loading history…"
    assert window.activity_pane.text == "Loading activity…"
    assert window.history_snapshots == [1]
    assert window._client.requests == [
        ("selection:1:detail", "/ui-api/threads/thread%2Fa", None),
        (
            "selection:1:status",
            "/ui-api/threads/thread%2Fa/status",
            {"activity_limit": 50},
        ),
    ]

    controller._request_selected_status()

    assert window._client.requests[-1] == (
        "selection:1:status",
        "/ui-api/threads/thread%2Fa/status",
        {"activity_limit": 50},
    )


def test_stale_snapshot_is_ignored_and_selection_clear_restores_empty_state() -> None:
    window, controller = make_controller()
    controller.select_thread("thread-a")
    request_count = len(window._client.requests)

    controller.select_thread("thread-b")
    controller._request_snapshot(1)
    controller.select_thread(None)

    assert window._selection_generation == 3
    assert window._selected_thread_id is None
    assert len(window._client.requests) == request_count + 2
    assert window.selected_status_timer.events == ["stop", "start", "stop", "start", "stop"]
    assert window.stream_status_label.text == "Stream: idle"
    assert window.empty_state_syncs == 1
    assert controller._is_current_selection("selection:2:detail") is None
    assert controller._is_current_selection("selection:3:detail") == (3, "detail")
    assert controller._selection_key("selection:3:status") == (3, "status")
    assert controller._selection_key("selection:nope:status") is None
    assert controller._selection_key("other:3:status") is None


def test_selected_status_does_not_request_when_no_thread_is_selected() -> None:
    window, controller = make_controller()

    controller._request_selected_status()

    assert window._client.requests == []
