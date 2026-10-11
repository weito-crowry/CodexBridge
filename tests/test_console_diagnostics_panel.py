from __future__ import annotations

import pytest

from codex_bridge.console.diagnostics_panel import DiagnosticsPanelController


class FakeScrollBar:
    def __init__(self, maximum: int, value: int) -> None:
        self._maximum = maximum
        self._value = value
        self.set_values: list[int] = []

    def maximum(self) -> int:
        return self._maximum

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        self._value = value
        self.set_values.append(value)


class FakeText:
    def __init__(self, events: list[object], maximum: int = 30, value: int = 30) -> None:
        self.events = events
        self.scrollbar = FakeScrollBar(maximum, value)
        self.next_maximum: int | None = None
        self.lines: list[str] = []

    def clear(self) -> None:
        self.events.append("clear")
        self.lines.clear()

    def verticalScrollBar(self) -> FakeScrollBar:
        return self.scrollbar

    def appendPlainText(self, text: str) -> None:
        self.events.append(("append", text))
        self.lines.append(text)
        if self.next_maximum is not None:
            self.scrollbar._maximum = self.next_maximum


class FakeButton:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.text = "Diagnostics"

    def setText(self, text: str) -> None:
        self.events.append(("button", text))
        self.text = text


class FakePane:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.hidden = True

    def isHidden(self) -> bool:
        return self.hidden

    def setVisible(self, visible: bool) -> None:
        self.events.append(("visible", visible))
        self.hidden = not visible


class FakeReader:
    def __init__(self, events: list[object], result: list[str] | Exception | None = None) -> None:
        self.events = events
        self.result = result if result is not None else []

    def reset(self) -> None:
        self.events.append("reset")

    def poll(self) -> list[str]:
        self.events.append("poll")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTimer:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def start(self) -> None:
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


class FakeWindow:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.diagnostics_pane = FakePane(self.events)
        self.diagnostics_toggle_button = FakeButton(self.events)
        self.diagnostics_text = FakeText(self.events)
        self._diagnostics_reader = FakeReader(self.events)
        self.diagnostics_timer = FakeTimer(self.events)
        self.controller = DiagnosticsPanelController(self)  # type: ignore[arg-type]

    def _on_diagnostics_timeout(self) -> None:
        self.controller._on_diagnostics_timeout()

    def _append_diagnostics_lines(self, lines: list[str]) -> None:
        self.controller._append_diagnostics_lines(lines)


def test_visible_toggle_resets_reader_polls_immediately_and_starts_timer() -> None:
    window = FakeWindow()
    window._diagnostics_reader.result = ["first line", "second line"]

    window.controller.toggle_diagnostics()

    assert window.events == [
        ("visible", True),
        ("button", "Hide Diagnostics"),
        "clear",
        "reset",
        "poll",
        ("append", "first line\nsecond line"),
        "start",
    ]
    assert window.diagnostics_toggle_button.text == "Hide Diagnostics"
    assert window.events[-1] == "start"


def test_hide_toggle_updates_button_and_stops_timer_without_clearing() -> None:
    window = FakeWindow()
    window.diagnostics_pane.hidden = False

    window.controller.toggle_diagnostics()

    assert window.events == [("visible", False), ("button", "Diagnostics"), "stop"]


@pytest.mark.parametrize("result", [RuntimeError("reader failed"), []])
def test_poll_error_or_empty_result_does_not_append(result: list[str] | Exception) -> None:
    window = FakeWindow()
    window._diagnostics_reader.result = result

    window.controller._on_diagnostics_timeout()

    assert window.events == ["poll"]


@pytest.mark.parametrize(
    ("maximum", "value", "new_maximum", "expected"),
    [(30, 29, 40, 40), (30, 10, 40, 10), (50, 50, 30, 30)],
)
def test_append_follows_bottom_preserves_up_scroll_and_clamps(
    maximum: int, value: int, new_maximum: int, expected: int
) -> None:
    window = FakeWindow()
    window.diagnostics_text.scrollbar._maximum = maximum
    window.diagnostics_text.scrollbar._value = value
    window.diagnostics_text.next_maximum = new_maximum

    window.controller._append_diagnostics_lines(["line"])

    assert window.diagnostics_text.lines == ["line"]
    assert window.diagnostics_text.scrollbar.set_values == [expected]


def test_append_empty_lines_has_no_text_or_scrollbar_side_effects() -> None:
    window = FakeWindow()

    window.controller._append_diagnostics_lines([])

    assert window.events == []
    assert window.diagnostics_text.scrollbar.set_values == []
