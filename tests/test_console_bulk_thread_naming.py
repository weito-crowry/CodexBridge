from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from codex_bridge.console.bulk_thread_naming import (
    BulkThreadNamingController,
    build_untitled_thread_proposals,
)
from codex_bridge.console.widgets import ThreadListPane


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object], str]] = []

    def post_json(self, path: str, payload: Mapping[str, object], *, key: str) -> bool:
        self.posts.append((path, dict(payload), key))
        return True


class FakeWindow:
    def __init__(self, threads: Sequence[Mapping[str, object]]) -> None:
        self.thread_pane = ThreadListPane()
        self.thread_pane.set_threads(threads)
        self._client = FakeClient()
        self.refreshes = 0

    def _request_threads(self) -> None:
        self.refreshes += 1


def _application() -> QApplication:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    return application


def _process_events(application: QApplication) -> None:
    for _ in range(5):
        application.processEvents()


def test_thread_pane_returns_loaded_untitled_rows_even_when_filter_hides_them() -> None:
    _application()
    pane = ThreadListPane()
    pane.set_threads(
        [
            {"id": "named", "name": "New Thread", "preview": "Named"},
            {"id": "missing", "preview": "Missing"},
            {"id": "empty", "name": "  ", "preview": "Empty"},
            {"id": "invalid", "name": 42, "preview": "Invalid"},
        ]
    )
    pane.filter_edit.setText("nothing matches")

    rows = pane.loaded_untitled_threads()

    assert [row["id"] for row in rows] == ["missing", "empty", "invalid"]
    assert "Named" not in str(rows)
    pane.update_thread_name("missing", "Assigned title")
    assert [row["id"] for row in pane.loaded_untitled_threads()] == ["empty", "invalid"]


def test_proposals_are_bounded_safe_and_duplicate_titles_get_stable_suffixes() -> None:
    rows = build_untitled_thread_proposals(
        [
            {"id": "ja-thread-1", "preview": "## 日本語の問い合わせを整理する"},
            {"id": "en-thread-2", "preview": "# Resolve the failing build"},
            {"id": "duplicate-1", "preview": "## Investigate the same issue"},
            {"id": "duplicate-2", "preview": "## Investigate the same issue"},
            {"id": "secret-thread", "preview": "# token=very-secret-value"},
            {"id": "long-1", "preview": "# " + "Long title " * 30},
            {"id": "long-2", "preview": "# " + "Long title " * 30},
        ]
    )
    titles = {row.thread_id: row.title for row in rows}

    assert titles["ja-thread-1"].startswith("日本語")
    assert titles["en-thread-2"] == "Resolve the failing build"
    assert titles["duplicate-1"] != titles["duplicate-2"]
    assert "duplicate-1"[-8:] in titles["duplicate-1"]
    assert "duplicate-2"[-8:] in titles["duplicate-2"]
    assert "very-secret-value" not in str(titles)
    assert all(len(title) <= 100 for title in titles.values())


def test_cancel_and_confirm_without_selection_send_no_posts() -> None:
    application = _application()
    window = FakeWindow(
        [
            {"id": "first", "preview": "# First task"},
            {"id": "second", "preview": "# Second task"},
        ]
    )
    controller = BulkThreadNamingController(window)

    controller.open_preview()
    assert controller.dialog is not None
    assert controller.dialog.table.rowCount() == 2
    controller.dialog.cancel_button.click()
    controller.open_preview()
    assert controller.dialog is not None
    for row in range(controller.dialog.table.rowCount()):
        checkbox = controller.dialog.table.item(row, 0)
        assert checkbox is not None
        checkbox.setCheckState(Qt.CheckState.Unchecked)
    controller.dialog.confirm_button.click()
    _process_events(application)

    assert not window._client.posts
    assert window.refreshes == 0


def test_confirm_posts_only_selected_rows_sequentially_and_reports_skip() -> None:
    application = _application()
    window = FakeWindow(
        [
            {"id": "thread/one", "preview": "# First task"},
            {"id": "thread-two", "preview": "# Ignore this row"},
            {"id": "thread-three", "preview": "# Third task"},
        ]
    )
    controller = BulkThreadNamingController(window)
    controller.open_preview()
    dialog = controller.dialog
    assert dialog is not None
    dialog.table.item(0, 3).setText("  日本語  title   ")
    dialog.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    dialog.table.item(2, 3).setText("English title")

    dialog.confirm_button.click()
    _process_events(application)

    assert len(window._client.posts) == 1
    first_path, first_payload, first_key = window._client.posts[0]
    assert first_path == "/ui-api/threads/thread%2Fone/name"
    assert first_payload == {"name": "日本語 title", "only_if_untitled": True}
    controller.handle_result(first_key, {"thread_id": "thread/one", "status": "renamed"})
    _process_events(application)

    assert len(window._client.posts) == 2
    second_path, second_payload, second_key = window._client.posts[1]
    assert second_path == "/ui-api/threads/thread-three/name"
    assert second_payload == {"name": "English title", "only_if_untitled": True}
    controller.handle_result(second_key, {"thread_id": "thread-three", "status": "skipped"})
    _process_events(application)

    assert window.refreshes == 1
    assert dialog.progress_label.text().find("Renamed 1") >= 0
    assert "Skipped 1" in dialog.progress_label.text()
    assert "Failed 0" in dialog.progress_label.text()
    assert dialog.table.item(2, 4).text() == "Skipped"


def test_cancel_during_request_finishes_current_item_then_stops() -> None:
    application = _application()
    window = FakeWindow(
        [
            {"id": "one", "preview": "# One"},
            {"id": "two", "preview": "# Two"},
            {"id": "three", "preview": "# Three"},
        ]
    )
    controller = BulkThreadNamingController(window)
    controller.open_preview()
    dialog = controller.dialog
    assert dialog is not None
    dialog.confirm_button.click()
    _process_events(application)
    first_key = window._client.posts[0][2]
    controller.handle_error(first_key, "private user title must not appear here")
    _process_events(application)
    assert len(window._client.posts) == 2

    dialog.cancel_button.click()
    second_key = window._client.posts[1][2]
    controller.handle_result(second_key, {"thread_id": "two", "status": "skipped"})
    _process_events(application)

    assert len(window._client.posts) == 2
    assert window.refreshes == 1
    assert "Failed 1" in dialog.progress_label.text()
    assert "Skipped 1" in dialog.progress_label.text()
    assert "private user title" not in dialog.progress_label.text()
    assert dialog.table.item(2, 4).text() == "Not processed"


def test_invalid_edited_title_is_rejected_without_echoing_input() -> None:
    _application()
    window = FakeWindow([{"id": "thread", "preview": "# Task"}])
    controller = BulkThreadNamingController(window)
    controller.open_preview()
    dialog = controller.dialog
    assert dialog is not None
    dialog.table.item(0, 3).setText("secret\x01value")

    dialog.confirm_button.click()

    assert not window._client.posts
    assert "unsupported control characters" not in dialog.progress_label.text()
    assert "secret" not in dialog.progress_label.text()


def test_blank_edited_title_is_rejected_without_network_write() -> None:
    _application()
    window = FakeWindow([{"id": "thread", "preview": "# Task"}])
    controller = BulkThreadNamingController(window)
    controller.open_preview()
    dialog = controller.dialog
    assert dialog is not None
    dialog.table.item(0, 3).setText(" \t ")

    dialog.confirm_button.click()

    assert not window._client.posts
    assert "Correct blank or unsupported titles" in dialog.progress_label.text()


def test_preview_table_handles_dozen_of_loaded_rows() -> None:
    _application()
    window = FakeWindow(
        [{"id": f"thread-{index}", "preview": f"# Task {index}"} for index in range(72)]
    )
    controller = BulkThreadNamingController(window)

    controller.open_preview()

    assert controller.dialog is not None
    assert controller.dialog.table.rowCount() == 72
    first_id = controller.dialog.table.item(0, 1)
    assert first_id is not None
    assert first_id.toolTip() == "thread-0"
    assert not window._client.posts
