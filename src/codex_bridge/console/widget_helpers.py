from __future__ import annotations

from PySide6.QtWidgets import QApplication


def _safe_text(value: object, limit: int = 16_384) -> str:
    return value[:limit] if isinstance(value, str) else ""


def copy_to_clipboard(text: str) -> None:
    application = QApplication.instance()
    if isinstance(application, QApplication):
        application.clipboard().setText(text)
