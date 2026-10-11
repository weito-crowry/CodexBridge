from __future__ import annotations

from math import ceil

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QTextBrowser, QWidget

from codex_bridge.console.widgets import HistoryPane, TimelineEntry
from tests.test_console_widgets import _process_layout


def test_history_pane_hides_and_detaches_old_cards_before_deferred_delete() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(760, 520)
    pane.show()
    pane.set_timeline(
        (
            TimelineEntry(
                "old-turn",
                "old-command",
                "Command",
                "Command",
                "powershell.exe -NoProfile -Command " + "x" * 500,
                "in_progress",
                (),
                1_791_518_400_000,
            ),
            TimelineEntry("old-turn", "old-user", "User", "User", "old question", None, ()),
            TimelineEntry("old-turn", "old-agent", "Agent", "Agent", "old answer", None, ()),
        )
    )
    _process_layout(application)

    old_cards = pane._content.findChildren(QFrame, "historyCard")
    old_card_geometries = {card: card.geometry().getRect() for card in old_cards}
    old_spinner = old_cards[0].findChild(QWidget, "historyRunningSpinner")
    old_copy_button = old_cards[1].findChild(QPushButton, "copyMessageButton")
    assert len(old_cards) == 3
    assert old_spinner is not None and old_spinner._timer.isActive()
    assert old_copy_button is not None

    pane.set_timeline(
        (TimelineEntry("new-turn", "new-agent", "Agent", "Agent", "new answer", None, ()),)
    )

    new_cards = [
        card for card in pane._content.findChildren(QFrame, "historyCard") if card not in old_cards
    ]
    visible_overlaps = [
        (old.geometry().getRect(), new.geometry().getRect())
        for old in old_cards
        for new in new_cards
        if old.isVisible() and old.geometry().intersects(new.geometry())
    ]
    diagnostics = (
        f"old_geometries={old_card_geometries} "
        f"old_visible={[card.isVisible() for card in old_cards]} "
        f"old_parents={[card.parentWidget() for card in old_cards]} "
        f"visible_overlaps={visible_overlaps}"
    )
    assert not visible_overlaps, diagnostics
    assert all(not card.isVisible() for card in old_cards), diagnostics
    assert all(card.parentWidget() is None for card in old_cards), diagnostics
    assert old_spinner.isVisible() is False
    assert old_spinner._timer.isActive() is False
    assert old_copy_button.isVisible() is False
    assert len(pane._content.findChildren(QFrame, "historyCard")) == 1

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    assert all(not shiboken6.isValid(card) for card in old_cards)
    assert not shiboken6.isValid(old_spinner)
    assert not shiboken6.isValid(old_copy_button)
    pane.close()


def test_history_body_height_for_width_does_not_change_render_document_width() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(720, 520)
    pane.show()
    pane.set_timeline(
        (
            TimelineEntry(
                "turn",
                "command",
                "Command",
                "Command",
                "Get-Content -LiteralPath 'D:\\" + ("nested\\subdirectory\\" * 20) + "result.txt'",
                "completed",
                ("exit 0",),
            ),
        )
    )
    _process_layout(application)
    body = pane.findChild(QTextBrowser)
    assert body is not None
    current_document_width = body.document().textWidth()

    measured_height = body.heightForWidth(body.width() - 48)

    assert measured_height > 0
    assert body.document().textWidth() == current_document_width
    pane.close()


def test_completed_command_cards_keep_compact_order_and_text_height_when_resized() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    command_one = "\n".join(
        (
            "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command `",
            "  Get-ChildItem -LiteralPath 'D:\\FX-LLM\\成果\\"
            "非常に長い日本語のディレクトリ名\\測定結果' | `",
            "  Where-Object { $_.Name -like '*report*' } | `",
            "  Set-Content -LiteralPath 'D:\\FX-LLM\\出力\\history-layout-result.txt'",
        )
    )
    long_path = "D:\\" + ("階層の深いディレクトリ名\\subdirectory\\" * 14) + "最終結果.txt"
    command_two = f"Get-Content -LiteralPath '{long_path}' | Measure-Object -Line"
    entries = (
        TimelineEntry(
            "turn-one",
            "command-one",
            "Command",
            "Command",
            command_one,
            "completed",
            ("exit 0",),
            1_791_518_400_000,
            1_791_518_401_000,
        ),
        TimelineEntry(
            "turn-one",
            "command-two",
            "Command",
            "Command",
            command_two,
            "completed",
            ("exit 0",),
            1_791_518_402_000,
            1_791_518_403_000,
        ),
        TimelineEntry(
            "turn-one",
            "command-short",
            "Command",
            "Command",
            "echo done",
            "completed",
            ("exit 0",),
            1_791_518_404_000,
            1_791_518_405_000,
        ),
    )
    pane.resize(860, 900)
    pane.show()
    pane.set_timeline(entries)
    _process_layout(application)

    for width in (860, 510, 390, 860):
        pane.resize(width, 900)
        _process_layout(application)
        viewport = pane._scroll.viewport()
        cards = pane._content.findChildren(QFrame, "historyCard")
        assert len(cards) == len(entries)
        previous_bottom = -1
        for card in cards:
            rect = card.geometry()
            assert rect.left() >= 0
            assert rect.right() < viewport.width(), f"width={width} card={rect.getRect()}"
            assert rect.top() > previous_bottom, f"width={width} card={rect.getRect()}"
            previous_bottom = rect.bottom()

            card_layout = card.layout()
            assert card_layout is not None
            header_layout = card_layout.itemAt(0).layout()
            assert header_layout is not None
            header = header_layout.itemAt(0).widget()
            assert isinstance(header, QLabel)
            header_diagnostics = (
                f"width={width} card={rect.getRect()} header={header.geometry().getRect()} "
                f"header_minimum={header.minimumSizeHint().toTuple()}"
            )
            assert header.width() >= header.minimumSizeHint().width(), header_diagnostics
            assert header.height() <= 2 * header.fontMetrics().height(), header_diagnostics
            child_rects = [card_layout.itemAt(i).geometry() for i in range(card_layout.count())]
            gaps = [
                current.top() - previous.bottom() - 1
                for previous, current in zip(child_rects, child_rects[1:], strict=False)
            ]
            bodies = [
                card_layout.itemAt(i).widget()
                for i in range(card_layout.count())
                if isinstance(card_layout.itemAt(i).widget(), QTextBrowser)
            ]
            last_rect = child_rects[-1]
            for body in bodies:
                document_height = body.document().documentLayout().documentSize().height()
                expected_height = ceil(document_height) + body.frameWidth() * 2
                assert abs(body.height() - expected_height) <= 1, (
                    f"width={width} card={rect.getRect()} body={body.geometry().getRect()} "
                    f"document_height={document_height} expected_height={expected_height} "
                    f"text={body.toPlainText()[:100]!r}"
                )
            bottom_slack = (
                card.height() - card_layout.contentsMargins().bottom() - 1 - last_rect.bottom()
            )
            geometry_report = (
                f"width={width} card={rect.getRect()} size_hint={card.sizeHint().height()} "
                f"minimum_hint={card.minimumSizeHint().height()} child_rects="
                f"{[child.getRect() for child in child_rects]} gaps={gaps} slack={bottom_slack}"
            )
            assert all(gap <= card_layout.spacing() + 2 for gap in gaps), geometry_report
            assert bottom_slack <= 2, geometry_report
            for body in bodies:
                assert body.width() > 0
                height_for_width = body.heightForWidth(body.width())
                assert abs(body.height() - height_for_width) <= 1, (
                    f"{geometry_report} body={body.geometry().getRect()} "
                    f"height_for_width={height_for_width} "
                    f"document_height={body.document().documentLayout().documentSize().height()} "
                    f"text={body.toPlainText()[:100]!r}"
                )

    completed_cards = cards[:2]
    for card, expected_body in zip(completed_cards, (command_one, command_two), strict=True):
        card_layout = card.layout()
        assert card_layout is not None
        header_layout = card_layout.itemAt(0).layout()
        assert header_layout is not None
        header = header_layout.itemAt(0).widget()
        timing = card_layout.itemAt(1).widget()
        body = card_layout.itemAt(2).widget()
        details = card_layout.itemAt(3).widget()
        assert isinstance(header, QLabel) and header.text().startswith("Command · completed")
        assert isinstance(timing, QLabel) and timing.objectName() == "historyTiming"
        assert timing.text().startswith("Start ") and " · End " in timing.text()
        assert isinstance(body, QTextBrowser) and body.toPlainText() == expected_body
        assert isinstance(details, QTextBrowser) and details.toPlainText() == "exit 0"
        assert header.geometry().bottom() < timing.geometry().top()
        assert timing.geometry().bottom() < body.geometry().top()
        assert body.geometry().bottom() < details.geometry().top()
    short_body = cards[2].layout().itemAt(2).widget()
    assert isinstance(short_body, QTextBrowser)
    short_document_height = short_body.document().documentLayout().documentSize().height()
    short_minimum_height = short_body.minimumSizeHint().height()
    assert short_minimum_height <= ceil(short_document_height) + short_body.frameWidth() * 2 + 1, (
        f"short_body={short_body.geometry().getRect()} minimum_hint={short_minimum_height} "
        f"document_height={short_document_height}"
    )
    assert pane._scroll.horizontalScrollBar().maximum() == 0
    pane.close()


def test_history_pane_keeps_card_geometry_stable_during_rapid_refresh_and_resize() -> None:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    pane = HistoryPane()
    pane.resize(940, 640)
    pane.show()
    _process_layout(application)
    repeated_japanese_command = "\n".join(["Write-Output '日本語'" * 5] * 8)

    for update in range(25):
        count = (6, 14, 24, 4, 18)[update % 5]
        turn_id = f"thread-{update % 2}-turn"
        entries = tuple(
            TimelineEntry(
                turn_id if index < count // 2 else f"{turn_id}-later",
                f"item-{index}",
                ("Command", "User", "Agent")[index % 3],
                ("Command", "User", "Agent")[index % 3],
                (
                    "powershell.exe -NoProfile -Command " + repeated_japanese_command
                    if index % 3 == 0
                    else f"thread {update} item {index}\n" + "line\n" * (index % 5)
                ),
                (
                    "in_progress"
                    if index % 3 == 0 and update % 2
                    else "completed"
                    if index % 3 == 0
                    else None
                ),
                ("exit 0",) if index % 3 == 0 and update % 2 == 0 else (),
                1_791_518_400_000,
                1_791_518_401_000 if index % 3 == 0 and update % 2 == 0 else None,
            )
            for index in range(count)
        )
        pane.resize((940, 580, 410, 820, 520)[update % 5], 640)
        scrollbar = pane._scroll.verticalScrollBar()
        if update % 4 == 0 and scrollbar.maximum() > 0:
            scrollbar.setValue(scrollbar.maximum() // 2)
        if update % 3 == 0:
            pane.set_timeline(entries[: max(1, count // 2)], has_older=True)
        pane.set_timeline(
            entries,
            has_older=update % 2 == 0,
            prepend=update % 5 == 0,
        )
        _process_layout(application)

        cards = pane._content.findChildren(QFrame, "historyCard")
        assert len(cards) == count
        previous_bottom = -1
        for card in cards:
            rect = card.geometry()
            assert rect.left() >= 0, f"update={update} card={rect.getRect()}"
            assert rect.right() < pane._scroll.viewport().width(), (
                f"update={update} viewport={pane._scroll.viewport().width()} card={rect.getRect()}"
            )
            assert rect.top() > previous_bottom, f"update={update} card={rect.getRect()}"
            previous_bottom = rect.bottom()
    pane.set_empty_state("Select another thread")
    _process_layout(application)
    assert pane._content.findChildren(QFrame, "historyCard") == []
    pane.close()
