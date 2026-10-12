from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtGui import QFont, QPalette

from .usage import (
    CodexUsage,
    format_codex_usage,
    format_codex_usage_detail,
    format_codex_usage_tooltip,
    parse_codex_usage,
    usage_level,
)
from .usage_history import UsageHistorySample

if TYPE_CHECKING:
    from .main_window import MainWindow


class UsageSampleRecorder(Protocol):
    def __call__(
        self,
        usage: CodexUsage,
        *,
        captured_at_epoch: float,
        database_path: Path | None,
    ) -> UsageHistorySample | None: ...


class UsagePresentationController:
    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self.usage = CodexUsage()
        self.refresh_error: str | None = None

    def apply_usage(self, payload: object) -> None:
        self.usage = parse_codex_usage(payload)
        self.refresh_error = None
        self.render_usage_status()

    def apply_usage_failure(self, message: str) -> None:
        bounded_message = " ".join(message.split())[:256]
        self.refresh_error = bounded_message or "request failed"
        self.render_usage_status()

    def render_usage_status(self) -> None:
        window = self._window
        status_text = format_codex_usage(self.usage)
        tooltip = format_codex_usage_tooltip(self.usage)
        detail = format_codex_usage_detail(self.usage)
        if self.refresh_error is not None:
            failure_detail = f"Refresh failed: {self.refresh_error}"
            status_text += " · refresh failed"
            tooltip += f"\n{failure_detail}"
            detail += f"\n{failure_detail}"
        window.usage_status_label.setText(status_text)
        window.usage_status_label.setToolTip(tooltip)
        window.usage_detail_label.setText(detail)
        level = usage_level(self.usage)
        font = window.usage_status_label.font()
        font.setBold(level != "normal")
        if level == "error":
            font.setWeight(QFont.Weight.Bold)
        elif level == "warning":
            font.setWeight(QFont.Weight.DemiBold)
        else:
            font.setWeight(QFont.Weight.Normal)
        window.usage_status_label.setFont(font)
        palette = window.usage_status_label.palette()
        appearance_controller = getattr(window, "_appearance_controller", None)
        theme = getattr(appearance_controller, "theme", None)
        if theme is None:
            role = {
                "normal": QPalette.ColorRole.Text,
                "warning": QPalette.ColorRole.Link,
                "error": QPalette.ColorRole.BrightText,
            }[level]
            status_color = palette.color(role)
        else:
            status_color = {
                "normal": theme.tokens.text,
                "warning": theme.tokens.warning,
                "error": theme.tokens.error,
            }[level]
        palette.setColor(QPalette.ColorRole.WindowText, status_color)
        window.usage_status_label.setPalette(palette)

    def record_usage_history(
        self,
        *,
        captured_at_epoch: float,
        record_sample: UsageSampleRecorder,
    ) -> None:
        window = self._window
        try:
            sample = record_sample(
                self.usage,
                captured_at_epoch=captured_at_epoch,
                database_path=window._usage_history_path,
            )
        except (OSError, sqlite3.Error):
            return
        history_window = window._usage_history_window
        if sample is not None and history_window is not None and history_window.isVisible():
            history_window.refresh(rolling=True)
