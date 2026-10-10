from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QLabel, QWidget


class UsageShortcutLabel(QLabel):
    activated = Signal()

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._left_press_active = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Codex Usage")
        self.setAccessibleDescription("Open Usage History")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            self._left_press_active = False
            super().mousePressEvent(event)
            return
        self._left_press_active = self.rect().contains(event.position().toPoint())
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            self._left_press_active = False
            super().mouseReleaseEvent(event)
            return
        should_activate = self._left_press_active and self.rect().contains(
            event.position().toPoint()
        )
        self._left_press_active = False
        event.accept()
        if should_activate:
            self.activated.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if not event.isAutoRepeat():
                self.activated.emit()
            event.accept()
            return
        super().keyPressEvent(event)
