from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from codex_bridge.console import usage_history_window as usage_history_window_module
from codex_bridge.console.main_window import MainWindow
from tests.test_console_main_window import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    FakeLauncher,
    StableTunnel,
    _application,
    _config,
    _usage_window,
)


def test_status_opens_one_independent_usage_history_window(tmp_path: Path) -> None:
    _application()
    window = _usage_window(FakeClient(), tmp_path / "usage-history.sqlite3")
    window._show_status()

    assert window.status_dialog.isVisible()
    assert window.status_dialog.findChild(QWidget, "usageHistoryChart") is None
    assert window.usage_history_button.text() == "Show usage history"
    assert not window.status_dialog.isModal()

    window.usage_history_button.click()
    first_window = window._usage_history_window
    assert first_window is not None
    assert first_window.isVisible()
    window.status_dialog.close()
    assert first_window.isVisible()

    window.usage_history_button.click()
    assert window._usage_history_window is first_window
    window.close()


def test_usage_status_label_opens_history_directly_and_reuses_it(tmp_path: Path) -> None:
    application = _application()
    window = _usage_window(FakeClient(), tmp_path / "usage-history.sqlite3")
    label = window.usage_status_label
    window.show()
    application.processEvents()

    QTest.mouseClick(label, Qt.MouseButton.RightButton)
    assert window._usage_history_window is None
    QTest.mousePress(label, Qt.MouseButton.LeftButton, pos=label.rect().center())
    QTest.mouseRelease(label, Qt.MouseButton.LeftButton, pos=QPoint(-1, -1))
    assert window._usage_history_window is None

    QTest.mouseClick(label, Qt.MouseButton.LeftButton)
    first_window = window._usage_history_window
    assert first_window is not None and first_window.isVisible()
    assert not window.status_dialog.isVisible()
    assert label.objectName() == "topStatus"
    assert label.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert label.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert label.accessibleName() == "Codex Usage"
    assert label.accessibleDescription() == "Open Usage History"

    QTest.mouseClick(label, Qt.MouseButton.LeftButton)
    assert window._usage_history_window is first_window

    window._show_status()
    window.usage_history_button.click()
    assert window._usage_history_window is first_window
    QTest.mouseClick(label, Qt.MouseButton.LeftButton)
    assert window._usage_history_window is first_window
    window.close()


@pytest.mark.parametrize(
    "key",
    [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space],
    ids=["enter", "keypad-enter", "space"],
)
def test_usage_status_label_keyboard_opens_history(tmp_path: Path, key: Qt.Key) -> None:
    application = _application()
    window = _usage_window(FakeClient(), tmp_path / "usage-history.sqlite3")
    window.show()
    application.processEvents()
    window.usage_status_label.setFocus()

    QTest.keyClick(window.usage_status_label, key)

    history_window = window._usage_history_window
    assert history_window is not None and history_window.isVisible()
    assert not window.status_dialog.isVisible()
    QTest.keyClick(window.usage_status_label, key)
    assert window._usage_history_window is history_window
    window.close()


def test_usage_status_shortcut_survives_usage_updates_and_failures(tmp_path: Path) -> None:
    application = _application()
    window = _usage_window(FakeClient(), tmp_path / "usage-history.sqlite3")
    window.show()
    application.processEvents()

    assert window.usage_status_label.text() == "Codex Usage  unavailable"
    QTest.mouseClick(window.usage_status_label, Qt.MouseButton.LeftButton)
    history_window = window._usage_history_window
    assert history_window is not None and history_window._display_samples == []

    window._apply_usage({"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 85}}})
    known_text = "Codex Usage  5h 15% · Week —"
    known_tooltip = window.usage_status_label.toolTip()
    known_weight = window.usage_status_label.font().weight()
    known_color = window.usage_status_label.palette().color(QPalette.ColorRole.WindowText)
    assert window.usage_status_label.text() == known_text
    assert "5h: 15% left" in known_tooltip
    assert known_weight == QFont.Weight.DemiBold
    assert known_color == window.usage_status_label.palette().color(QPalette.ColorRole.Link)

    window._apply_usage_failure("Bridge unavailable")
    assert window.usage_status_label.text() == f"{known_text} · refresh failed"
    assert "Bridge unavailable" in window.usage_status_label.toolTip()
    assert window.usage_status_label.font().weight() == known_weight
    assert window.usage_status_label.palette().color(QPalette.ColorRole.WindowText) == known_color

    QTest.mouseClick(window.usage_status_label, Qt.MouseButton.LeftButton)
    assert window._usage_history_window is history_window
    assert not window.status_dialog.isVisible()
    window.close()


def test_main_window_exit_closes_its_usage_history_window(tmp_path: Path) -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        runtime_launcher=FakeLauncher(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        quit_application=lambda: None,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )
    window.usage_history_button.click()
    history_window = window._usage_history_window
    assert history_window is not None and history_window.isVisible()

    window._begin_exit()

    assert not history_window.isVisible()
    window.close()


@pytest.mark.parametrize("history_error", [sqlite3.OperationalError, OSError])
def test_status_opens_and_usage_history_reports_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    history_error: type[Exception],
) -> None:
    _application()
    window = MainWindow(
        _config(),
        api_client=FakeClient(),
        tray_available=False,
        usage_history_path=tmp_path / "usage-history.sqlite3",
    )

    def fail_read(*args: object, **kwargs: object) -> None:
        raise history_error("history unavailable")

    monkeypatch.setattr(usage_history_window_module, "get_usage_samples_for_display", fail_read)

    window._show_status()
    window.usage_history_button.click()

    assert window.status_dialog.isVisible()
    assert window._usage_history_window is not None
    assert (
        window._usage_history_window.chart_stack.currentWidget()
        is window._usage_history_window.error_state_label
    )
    window.close()
