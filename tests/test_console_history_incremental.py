from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextBrowser, QWidget

from codex_bridge.console import widgets as widgets_module
from codex_bridge.console.widgets import HistoryPane, TimelineEntry
from tests.test_console_widgets import _details_history_pane, _process_layout, _usage_snapshot


def _ordered_widgets(pane: HistoryPane, object_name: str) -> list[QWidget]:
    widgets: list[QWidget] = []
    for index in range(pane._content_layout.count()):
        item = pane._content_layout.itemAt(index)
        widget = item.widget() if item is not None else None
        if widget is not None and widget.objectName() == object_name:
            widgets.append(widget)
    return widgets


def test_appending_history_rows_preserves_existing_card_and_header_widgets() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(760, 560)
    pane.show()
    initial_entries = (
        TimelineEntry("turn-1", "user-1", "User", "User", "長い日本語の質問" * 20, None, ()),
        TimelineEntry("turn-1", "agent-1", "Agent", "Agent", "最初の回答", None, ()),
        TimelineEntry("turn-2", "command-1", "Command", "Command", "pwsh -NoProfile", None, ()),
    )
    pane.set_timeline(initial_entries)
    _process_layout(application)
    old_cards = _ordered_widgets(pane, "historyCard")
    old_headers = pane._content.findChildren(QLabel, "turnSeparator")
    assert len(old_cards) == 3
    assert len(old_headers) == 2

    pane.set_timeline(
        (
            *initial_entries,
            TimelineEntry("turn-2", "agent-2", "Agent", "Agent", "二つ目の回答", None, ()),
            TimelineEntry("turn-3", "user-3", "User", "User", "次の質問", None, ()),
        )
    )
    _process_layout(application)

    current_cards = _ordered_widgets(pane, "historyCard")
    current_headers = pane._content.findChildren(QLabel, "turnSeparator")
    assert current_cards[:3] == old_cards
    assert current_headers[:2] == old_headers
    assert len(current_cards) == 5
    assert len(current_headers) == 3
    pane.close()


def test_changed_card_is_replaced_alone_and_copy_uses_latest_body(monkeypatch) -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(760, 560)
    pane.show()
    entries = (
        TimelineEntry("turn", "command", "Command", "Command", "pwsh", "in_progress", ()),
        TimelineEntry("turn", "agent", "Agent", "Agent", "initial answer", "in_progress", ()),
        TimelineEntry("turn", "user", "User", "User", "question", None, ()),
    )
    pane.set_timeline(entries)
    _process_layout(application)
    old_cards = _ordered_widgets(pane, "historyCard")
    changed_card = old_cards[1]
    preserved_cards = (old_cards[0], old_cards[2])
    old_spinner = changed_card.findChild(QWidget, "historyRunningSpinner")
    assert old_spinner is not None and old_spinner._timer.isActive()
    clipboard: list[str] = []
    monkeypatch.setattr(widgets_module, "copy_to_clipboard", clipboard.append)

    updated_entries = (
        entries[0],
        replace(
            entries[1],
            body="latest answer",
            status="completed",
            details=("final",),
            started_at_ms=1_791_518_400_000,
            completed_at_ms=1_791_518_401_000,
        ),
        entries[2],
    )
    pane.set_timeline(updated_entries)
    _process_layout(application)

    current_cards = _ordered_widgets(pane, "historyCard")
    updated_card = current_cards[1]
    assert current_cards[0] is preserved_cards[0]
    assert current_cards[2] is preserved_cards[1]
    assert updated_card is not changed_card
    assert not changed_card.isVisible()
    assert changed_card.parentWidget() is None
    assert not old_spinner._timer.isActive()
    copy_button = updated_card.findChild(QPushButton, "copyMessageButton")
    assert copy_button is not None
    copy_button.click()
    assert clipboard == ["latest answer"]
    assert any(body.toPlainText() == "final" for body in updated_card.findChildren(QTextBrowser))

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    assert not shiboken6.isValid(changed_card)
    assert not shiboken6.isValid(old_spinner)
    pane.close()


def test_turn_header_and_usage_snapshot_changes_preserve_cards_and_update_in_place() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(700, 520)
    pane.show()
    entries = (
        TimelineEntry("turn", "agent-1", "Agent", "Agent", "answer one", None, ()),
        TimelineEntry("turn", "agent-2", "Agent", "Agent", "answer two", None, ()),
    )
    statuses = {"turn": "in_progress"}
    metadata: dict[str, dict[str, object]] = {
        "turn": {
            "model_resolution_status": "resolved",
            "model_candidates": [{"model": "gpt-5", "reasoning_effort": "high"}],
        }
    }
    pane.set_timeline(entries, turn_statuses=statuses, turn_model_metadata=metadata)
    _process_layout(application)
    old_cards = _ordered_widgets(pane, "historyCard")
    header = pane._content.findChild(QLabel, "turnSeparator")
    assert header is not None

    statuses["turn"] = "completed"
    candidates = metadata["turn"]["model_candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate["model"] = "gpt-6"
    pane.set_timeline(entries, turn_statuses=statuses, turn_model_metadata=metadata)
    _process_layout(application)
    assert pane._content.findChild(QLabel, "turnSeparator") is header
    assert header.text() == "Turn · completed · Model: gpt-6 (high)"
    assert _ordered_widgets(pane, "historyCard") == old_cards

    snapshot = _usage_snapshot()
    pane.set_timeline(
        entries,
        turn_statuses=statuses,
        turn_model_metadata=metadata,
        turn_usage_snapshots={"turn": snapshot},
    )
    _process_layout(application)
    snapshot_label = pane._content.findChild(QLabel, "turnUsageSnapshot")
    assert snapshot_label is not None
    assert pane._content.findChild(QLabel, "turnSeparator") is header
    assert _ordered_widgets(pane, "historyCard") == old_cards

    changed_snapshot = replace(snapshot, captured_at=snapshot.captured_at + timedelta(minutes=1))
    pane.set_timeline(
        entries,
        turn_statuses=statuses,
        turn_model_metadata=metadata,
        turn_usage_snapshots={"turn": changed_snapshot},
    )
    _process_layout(application)
    assert pane._content.findChild(QLabel, "turnUsageSnapshot") is snapshot_label
    assert "captured " in snapshot_label.text()
    assert _ordered_widgets(pane, "historyCard") == old_cards
    pane.close()


def test_load_older_prepends_rows_without_replacing_cards_or_losing_view_anchor() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(640, 340)
    pane.show()
    entries = tuple(
        TimelineEntry(
            "current-turn", f"item-{index}", "Agent", "Agent", f"answer {index}", None, ()
        )
        for index in range(36)
    )
    pane.set_timeline(entries)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    old_value = scrollbar.value()
    old_maximum = scrollbar.maximum()
    old_cards = _ordered_widgets(pane, "historyCard")
    older_entries = (
        TimelineEntry("older-turn", "older-1", "User", "User", "older question", None, ()),
        TimelineEntry("older-turn", "older-2", "Agent", "Agent", "older answer", None, ()),
    )

    pane.set_timeline((*older_entries, *entries), has_older=True, prepend=True)
    _process_layout(application)

    current_cards = _ordered_widgets(pane, "historyCard")
    assert current_cards[2:] == old_cards
    assert len(current_cards) == len(old_cards) + len(older_entries)
    assert scrollbar.value() - old_value == scrollbar.maximum() - old_maximum
    scrollbar.setValue(0)
    _process_layout(application)
    assert pane.load_older_button.isVisible()
    pane.close()


def test_reorder_duplicate_ids_and_item_deletion_use_full_rebuild_fallback() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    initial = (
        TimelineEntry("turn", "one", "Agent", "Agent", "body one", None, ()),
        TimelineEntry("turn", "two", "Agent", "Agent", "body two", None, ()),
        TimelineEntry("turn", "three", "Agent", "Agent", "body three", None, ()),
    )
    changed_timelines = (
        (initial[2], initial[1], initial[0]),
        (*initial, replace(initial[0], body="duplicate id")),
        (initial[0], initial[2]),
    )
    for changed in changed_timelines:
        pane = _details_history_pane()
        pane.resize(640, 460)
        pane.show()
        pane.set_timeline(initial)
        _process_layout(application)
        old_cards = _ordered_widgets(pane, "historyCard")

        pane.set_timeline(changed)
        _process_layout(application)

        current_cards = _ordered_widgets(pane, "historyCard")
        assert len(current_cards) == len(changed)
        assert not any(current in old_cards for current in current_cards)
        rendered_bodies = [
            card.findChildren(QTextBrowser)[0].toPlainText() for card in current_cards
        ]
        assert rendered_bodies == [entry.body for entry in changed]
        pane.close()


def test_manual_scroll_does_not_follow_new_rows_but_bottom_scroll_does() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(640, 340)
    pane.show()
    entries = tuple(
        TimelineEntry("turn", f"item-{index}", "Agent", "Agent", f"answer {index}", None, ())
        for index in range(32)
    )
    pane.set_timeline(entries)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    pane._on_slider_pressed()
    old_value = scrollbar.value()
    pane.set_timeline(
        (*entries, TimelineEntry("turn", "new-item", "Agent", "Agent", "new answer", None, ()))
    )
    _process_layout(application)
    assert scrollbar.value() == old_value
    assert scrollbar.value() < scrollbar.maximum()
    pane._on_slider_released()

    scrollbar.setValue(scrollbar.maximum())
    _process_layout(application)
    pane.set_timeline(
        (
            *entries,
            TimelineEntry("turn", "new-item", "Agent", "Agent", "new answer", None, ()),
            TimelineEntry("turn", "latest-item", "Agent", "Agent", "latest answer", None, ()),
        )
    )
    _process_layout(application)
    assert scrollbar.value() == scrollbar.maximum()
    pane.close()


def test_identical_prepend_refresh_is_a_true_noop_and_thread_reset_rebuilds() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = _details_history_pane()
    pane.resize(640, 320)
    pane.show()
    entries = tuple(
        TimelineEntry(f"turn-{index}", f"item-{index}", "Agent", "Agent", "answer", None, ())
        for index in range(24)
    )
    pane.set_timeline(entries, has_older=True)
    _process_layout(application)
    scrollbar = pane._scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    _process_layout(application)
    old_value = scrollbar.value()
    old_cards = _ordered_widgets(pane, "historyCard")
    old_generation = pane._render_generation

    pane.set_timeline(tuple(entries), has_older=True, prepend=True)
    _process_layout(application)
    assert pane._render_generation == old_generation
    assert _ordered_widgets(pane, "historyCard") == old_cards
    assert scrollbar.value() == old_value
    assert not pane._scroll_restore_timer.isActive()
    assert pane._scroll_restore_state is None

    pane.set_empty_state("Select another thread")
    _process_layout(application)
    assert _ordered_widgets(pane, "historyCard") == []
    pane.set_timeline(entries)
    _process_layout(application)
    new_cards = _ordered_widgets(pane, "historyCard")
    assert len(new_cards) == len(old_cards)
    assert not any(card in old_cards for card in new_cards)
    pane.close()
