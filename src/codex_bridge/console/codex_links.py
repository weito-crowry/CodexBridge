"""Codex App URI helpers."""

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def codex_thread_uri(thread_id: str) -> str:
    return f"codex://threads/{thread_id}"


def open_codex_uri(uri: str) -> bool:
    try:
        return bool(QDesktopServices.openUrl(QUrl(uri)))
    except Exception:
        return False
