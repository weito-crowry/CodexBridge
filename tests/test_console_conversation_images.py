from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QDesktopServices, QImage
from PySide6.QtWidgets import QApplication, QLabel, QToolButton, QWidget

from codex_bridge.console.widgets import HistoryPane, timeline_entries


def _application() -> QApplication:
    application = QApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _write_image(path: Path, color: QColor) -> None:
    image = QImage(1600, 900, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path))


def test_user_images_render_as_small_safe_links_in_all_history_modes(tmp_path, monkeypatch) -> None:
    application = _application()
    first_image = tmp_path / "first image.png"
    second_image = tmp_path / "日本語.png"
    third_image = tmp_path / "third.png"
    missing_image = tmp_path / "moved.png"
    _write_image(first_image, QColor("red"))
    _write_image(second_image, QColor("blue"))
    _write_image(third_image, QColor("green"))
    entries = timeline_entries(
        {
            "items": [
                {
                    "turn_id": "turn-2",
                    "item": {
                        "id": "user-2",
                        "type": "userMessage",
                        "text": "second prompt [image]",
                        "images": [str(third_image)],
                    },
                },
                {
                    "turn_id": "turn-1",
                    "item": {
                        "id": "user-1",
                        "type": "userMessage",
                        "text": "first prompt [image]",
                        "images": [str(first_image), str(second_image), str(missing_image)],
                    },
                },
            ]
        }
    )
    assert [(entry.turn_id, entry.item_id, entry.images) for entry in entries] == [
        ("turn-1", "user-1", (str(first_image), str(second_image), str(missing_image))),
        ("turn-2", "user-2", (str(third_image),)),
    ]

    pane = HistoryPane()
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    for mode in ("Conversation", "Messages", "Details"):
        pane.set_mode(mode)
        pane.set_timeline(entries)
        application.processEvents()

        previews = pane.findChildren(QLabel, "conversationImagePreview")
        buttons = pane.findChildren(QToolButton, "conversationImageOpenButton")
        image_rows = pane.findChildren(QWidget, "conversationImageAttachments")
        assert len(previews) == 3
        assert len(buttons) == 3
        assert all(
            preview.pixmap() is not None
            and preview.pixmap().size().boundedTo(QSize(320, 200)) == preview.pixmap().size()
            for preview in previews
        )
        assert {(row.property("turnId"), row.property("itemId")) for row in image_rows} == {
            ("turn-1", "user-1"),
            ("turn-2", "user-2"),
        }
        first_button = next(button for button in buttons if button.text() == first_image.name)
        first_button.click()
        assert Path(opened[-1]).resolve() == first_image.resolve()

    legacy_entries = timeline_entries(
        {
            "items": [
                {
                    "turn_id": "turn-old",
                    "item": {
                        "id": "old-user",
                        "type": "userMessage",
                        "text": "old message [image]",
                    },
                }
            ]
        }
    )
    pane.set_timeline(legacy_entries)
    application.processEvents()
    assert pane.findChildren(QLabel, "conversationImagePreview") == []
    assert pane.findChildren(QToolButton, "conversationImageOpenButton") == []
    pane.close()
