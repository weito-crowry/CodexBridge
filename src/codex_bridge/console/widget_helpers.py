from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QWidget


def _safe_text(value: object, limit: int | None = 16_384) -> str:
    return value[:limit] if isinstance(value, str) else ""


def copy_to_clipboard(text: str) -> bool:
    try:
        application = QApplication.instance()
        if not isinstance(application, QApplication):
            return False
        clipboard = application.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return clipboard.text() == text
    except Exception:
        return False


class CopyMessageButton(QPushButton):
    def __init__(self, text: str, copy_text: Callable[[str], object], parent: QWidget) -> None:
        super().__init__(parent)
        self._source_text = text
        self._copy_text = copy_text
        self._feedback_size: QSize | None = None
        # The button owns both the timer and its receiver, so deleting a card
        # cancels pending feedback without retaining a dead Qt wrapper.
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.setInterval(1800)
        self._reset_timer.timeout.connect(self._reset_feedback)
        self._reset_feedback()
        self.clicked.connect(self._copy)

    def sizeHint(self) -> QSize:
        if self._feedback_size is not None:
            return self._feedback_size
        return super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        if self._feedback_size is not None:
            return self._feedback_size
        return super().minimumSizeHint()

    def _reset_feedback(self) -> None:
        self._reset_timer.stop()
        self._feedback_size = None
        self.setText(self.tr("Copy"))
        self.setAccessibleName(self.tr("Copy"))
        self.setToolTip(self.tr("Copy message"))

    def _copy(self) -> None:
        try:
            copied = self._copy_text(self._source_text) is True
        except Exception:
            copied = False
        if not copied:
            self._reset_feedback()
            return
        if self._feedback_size is None:
            self._feedback_size = super().sizeHint()
        self.setText("✓")
        self.setAccessibleName(self.tr("Copied"))
        self.setToolTip(self.tr("Copied"))
        self._reset_timer.start()
