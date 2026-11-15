from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint

from codex_bridge.console.appearance import stylesheet_for_theme, theme_for_scheme
from codex_bridge.console.composer_widget import ComposerWidget
from tests.test_console_composer_controller import _application, _controller


def test_attachment_basename_has_visible_space_beside_remove_button() -> None:
    app = _application()
    widget = ComposerWidget()
    widget.resize(620, 440)
    widget.set_attachments(["D:/files/日本語 report.txt"])
    widget.show()
    app.processEvents()
    try:
        chip = widget.attachment_bar._chips[0]
        label = chip.name_label
        assert label.text() == "日本語 report.txt"
        assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
        assert label.geometry().right() < chip.remove_button.geometry().left()
        assert "日本語 report.txt" in label.toolTip()
    finally:
        widget.close()


@pytest.mark.parametrize("width", [240, 360, 720])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_long_attachment_labels_fit_and_expand_with_composer(width: int, scheme: str) -> None:
    app = _application()
    widget = ComposerWidget()
    widget.setStyleSheet(stylesheet_for_theme(theme_for_scheme(scheme)))
    names = [f"日本語 資料 {index} " + "長い名前" * 12 + ".longextension" for index in range(8)]
    widget.resize(width, 480)
    widget.set_attachments([f"D:/files/{name}" for name in names])
    widget.show()
    for _ in range(3):
        app.processEvents()
    try:
        bar = widget.attachment_bar
        for chip, name in zip(bar._chips, names, strict=True):
            assert chip.geometry().left() >= 0
            assert chip.geometry().right() < bar.chip_container.width()
            assert chip.name_label.width() > chip.name_label.fontMetrics().horizontalAdvance("…")
            assert "…" in chip.name_label.text()
            assert (
                chip.name_label.fontMetrics().horizontalAdvance(chip.name_label.text())
                <= chip.name_label.width()
            )
            assert name in chip.name_label.toolTip()
            assert chip.name_label.geometry().right() < chip.remove_button.geometry().left()
            assert chip.rect().contains(chip.remove_button.geometry())
        assert bar.scroll_area.horizontalScrollBar().maximum() == 0
        send_position = widget.send_button.mapTo(widget, QPoint(0, 0))
        assert send_position.x() + widget.send_button.width() <= widget.width()
        narrow_text = bar._chips[0].name_label.text()
        widget.resize(900, 480)
        for _ in range(3):
            app.processEvents()
        assert len(bar._chips[0].name_label.text()) >= len(narrow_text)
    finally:
        widget.close()


def test_staged_same_names_keep_source_labels_through_drafts_and_individual_removal(
    tmp_path: Path,
) -> None:
    _application()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    staging = allowed / ".codexbridge-staging"
    staging.mkdir()
    (staging / "日本語 report.txt").write_text("existing", encoding="utf-8")
    sources = []
    for directory in ("first", "second"):
        source = tmp_path / directory / "日本語 report.txt"
        source.parent.mkdir()
        source.write_text(directory, encoding="utf-8")
        sources.append(str(source))
    _controller_instance, window, widget, _client, _root = _controller(
        tmp_path, allowed_roots=(str(allowed),)
    )
    window.thread_pane.thread_selected.emit("thread-a")
    widget.add_attachments(sources)
    staged_paths = widget.attachments
    assert [Path(path).name for path in staged_paths] == [
        "日本語 report (1).txt",
        "日本語 report (2).txt",
    ]
    window.thread_pane.thread_selected.emit("thread-b")
    assert widget.attachments == ()
    window.thread_pane.thread_selected.emit("thread-a")
    chips = widget.attachment_bar._chips
    assert [chip.name_label.text() for chip in chips] == ["日本語 report.txt"] * 2
    assert sources[0] in chips[0].name_label.toolTip()
    assert sources[1] in chips[1].name_label.toolTip()
    chips[0].remove_button.click()
    assert widget.attachments == (staged_paths[1],)
    remaining = widget.attachment_bar._chips[0]
    assert remaining.name_label.text() == "日本語 report.txt"
    assert sources[1] in remaining.toolTip()
    assert all(Path(path).exists() for path in (*sources, *staged_paths))
