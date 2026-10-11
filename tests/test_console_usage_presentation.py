from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QPalette

from codex_bridge.console.usage import CodexUsage
from codex_bridge.console.usage_history import UsageHistorySample
from codex_bridge.console.usage_presentation import UsagePresentationController


class FakeFont:
    def __init__(self) -> None:
        self.bold = False
        self.weight = QFont.Weight.Normal

    def setBold(self, bold: bool) -> None:
        self.bold = bold

    def setWeight(self, weight: QFont.Weight) -> None:
        self.weight = weight


class FakePalette:
    def __init__(self) -> None:
        roles = QPalette.ColorRole
        self.colors: dict[QPalette.ColorRole, str] = {
            roles.Text: "text",
            roles.Link: "link",
            roles.BrightText: "bright",
            roles.WindowText: "window",
        }
        self.set_color_calls: list[tuple[QPalette.ColorRole, str]] = []

    def color(self, role: QPalette.ColorRole) -> str:
        return self.colors[role]

    def setColor(self, role: QPalette.ColorRole, color: str) -> None:
        self.colors[role] = color
        self.set_color_calls.append((role, color))


class FakeLabel:
    def __init__(self) -> None:
        self.text_value = ""
        self.tooltip = ""
        self.current_font = FakeFont()
        self.current_palette = FakePalette()

    def setText(self, value: str) -> None:
        self.text_value = value

    def setToolTip(self, value: str) -> None:
        self.tooltip = value

    def font(self) -> FakeFont:
        return self.current_font

    def setFont(self, font: FakeFont) -> None:
        self.current_font = font

    def palette(self) -> FakePalette:
        return self.current_palette

    def setPalette(self, palette: FakePalette) -> None:
        self.current_palette = palette


class FakeHistoryWindow:
    def __init__(self, visible: bool) -> None:
        self.visible = visible
        self.refresh_calls: list[bool] = []

    def isVisible(self) -> bool:
        return self.visible

    def refresh(self, *, rolling: bool = False) -> None:
        self.refresh_calls.append(rolling)


class FakeWindow:
    def __init__(self, history_window: FakeHistoryWindow | None = None) -> None:
        self.usage_status_label = FakeLabel()
        self.usage_detail_label = FakeLabel()
        self._usage_history_path = Path("usage-history.sqlite3")
        self._usage_history_window = history_window


def _controller(
    history_window: FakeHistoryWindow | None = None,
) -> tuple[UsagePresentationController, FakeWindow]:
    window = FakeWindow(history_window)
    return UsagePresentationController(window), window  # type: ignore[arg-type]


def _usage_payload(*, five_hour_used: int = 28, weekly_used: int = 82) -> dict[str, object]:
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "windowDurationMins": 300,
                    "usedPercent": five_hour_used,
                    "resetsAt": "2026-10-12T12:00:00",
                },
                "secondary": {
                    "windowDurationMins": 10080,
                    "usedPercent": weekly_used,
                    "resetsAt": "2026-10-19T12:00:00",
                },
            }
        }
    }


def test_initial_display_and_usage_success_preserve_usage_text() -> None:
    controller, window = _controller()
    status = window.usage_status_label
    detail = window.usage_detail_label

    assert controller.usage == CodexUsage()
    assert controller.refresh_error is None
    controller.render_usage_status()
    assert status.text_value == "Codex Usage  unavailable"
    assert status.tooltip == "Codex Usage unavailable"
    assert detail.text_value == "Unavailable"
    assert status.current_font.weight == QFont.Weight.Normal
    assert status.current_palette.color(QPalette.ColorRole.WindowText) == "text"

    controller.apply_usage(_usage_payload())

    assert status.text_value == "Codex Usage  5h 72% · Week 18%"
    assert status.tooltip == (
        "5h: 72% left · reset 2026-10-12 12:00:00\nWeek: 18% left · reset 2026-10-19 12:00:00"
    )
    assert detail.text_value == status.tooltip
    assert status.current_font.weight == QFont.Weight.DemiBold
    assert status.current_palette.color(QPalette.ColorRole.WindowText) == "link"


@pytest.mark.parametrize(
    ("used_percent", "weight", "color_role", "color"),
    (
        (50, QFont.Weight.Normal, QPalette.ColorRole.Text, "text"),
        (85, QFont.Weight.DemiBold, QPalette.ColorRole.Link, "link"),
        (95, QFont.Weight.Bold, QPalette.ColorRole.BrightText, "bright"),
    ),
)
def test_usage_level_sets_font_weight_and_window_text_color(
    used_percent: int,
    weight: QFont.Weight,
    color_role: QPalette.ColorRole,
    color: str,
) -> None:
    controller, window = _controller()

    controller.apply_usage(_usage_payload(five_hour_used=used_percent, weekly_used=0))

    status = window.usage_status_label
    assert status.current_font.weight == weight
    assert status.current_font.bold is (weight != QFont.Weight.Normal)
    assert status.current_palette.color(QPalette.ColorRole.WindowText) == color
    assert status.current_palette.set_color_calls[-1] == (
        QPalette.ColorRole.WindowText,
        color,
    )
    assert color_role in status.current_palette.colors


def test_failure_is_bounded_normalized_and_keeps_usage_until_success() -> None:
    controller, window = _controller()
    controller.apply_usage(_usage_payload())
    usage = controller.usage

    controller.apply_usage_failure(" \t upstream   temporarily\n unavailable  ")
    assert controller.usage == usage
    assert controller.refresh_error == "upstream temporarily unavailable"
    assert window.usage_status_label.text_value.endswith(" · refresh failed")
    assert window.usage_detail_label.text_value.endswith(
        "Refresh failed: upstream temporarily unavailable"
    )

    controller.apply_usage_failure("x" * 500)
    assert controller.usage == usage
    assert controller.refresh_error == "x" * 256

    controller.apply_usage_failure(" \t\n ")
    assert controller.usage == usage
    assert controller.refresh_error == "request failed"

    controller.apply_usage(_usage_payload(five_hour_used=40, weekly_used=60))
    assert controller.refresh_error is None
    assert "refresh failed" not in window.usage_status_label.text_value


@pytest.mark.parametrize(
    ("sample", "visible", "has_history_window", "expected_refreshes"),
    (
        (None, True, True, []),
        (UsageHistorySample(120, 125.0, 72, 18), False, True, []),
        (UsageHistorySample(120, 125.0, 72, 18), True, True, [True]),
        (UsageHistorySample(120, 125.0, 72, 18), True, False, []),
    ),
)
def test_record_usage_history_refreshes_only_for_visible_window_and_sample(
    sample: UsageHistorySample | None,
    visible: bool,
    has_history_window: bool,
    expected_refreshes: list[bool],
) -> None:
    history_window = FakeHistoryWindow(visible) if has_history_window else None
    controller, window = _controller(history_window)
    controller.apply_usage(_usage_payload())
    records: list[tuple[CodexUsage, float, Path]] = []

    def record_sample(
        usage: CodexUsage,
        *,
        captured_at_epoch: float,
        database_path: Path | None,
    ) -> UsageHistorySample | None:
        assert database_path is not None
        records.append((usage, captured_at_epoch, database_path))
        return sample

    controller.record_usage_history(captured_at_epoch=125.0, record_sample=record_sample)

    assert records == [(controller.usage, 125.0, window._usage_history_path)]
    if history_window is not None:
        assert history_window.refresh_calls == expected_refreshes


def test_record_usage_history_tolerates_storage_errors_without_changing_display() -> None:
    history_window = FakeHistoryWindow(True)
    controller, window = _controller(history_window)
    controller.apply_usage(_usage_payload())
    status_text = window.usage_status_label.text_value

    def fail_record(
        _usage: CodexUsage,
        *,
        captured_at_epoch: float,
        database_path: Path | None,
    ) -> UsageHistorySample | None:
        assert captured_at_epoch == 125.0
        assert database_path == Path("usage-history.sqlite3")
        raise sqlite3.OperationalError("history unavailable")

    controller.record_usage_history(captured_at_epoch=125.0, record_sample=fail_record)

    assert window.usage_status_label.text_value == status_text
    assert history_window.refresh_calls == []

    def fail_os(
        _usage: CodexUsage,
        *,
        captured_at_epoch: float,
        database_path: Path | None,
    ) -> UsageHistorySample | None:
        assert captured_at_epoch == 125.0
        assert database_path == Path("usage-history.sqlite3")
        raise OSError("history unavailable")

    controller.record_usage_history(captured_at_epoch=125.0, record_sample=fail_os)
    assert window.usage_status_label.text_value == status_text
    assert history_window.refresh_calls == []
