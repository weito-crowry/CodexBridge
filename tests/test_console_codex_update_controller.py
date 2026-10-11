from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_update_controller import CodexUpdateController
from codex_bridge.console.codex_updates import CodexUpdateInfo


class FakeControl:
    def __init__(self) -> None:
        self.enabled = True
        self.value = ""
        self.history: list[str] = []
        self.visible = False

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setText(self, value: str) -> None:
        self.value = value
        self.history.append(value)

    def text(self) -> str:
        return self.value

    def clear(self) -> None:
        self.setText("")

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeTimer:
    def __init__(self) -> None:
        self.active = False
        self.starts = 0

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True
        self.starts += 1


class FakeProbe:
    def __init__(self) -> None:
        self.check_calls: list[CodexResolution] = []
        self.update_calls: list[CodexResolution] = []
        self.check_result = True
        self.update_result = True

    def check_for_updates(self, resolution: CodexResolution) -> bool:
        self.check_calls.append(resolution)
        return self.check_result

    def update(self, resolution: CodexResolution) -> bool:
        self.update_calls.append(resolution)
        return self.update_result


class FakeWindow:
    def __init__(self, probe: FakeProbe | None = None) -> None:
        self._closing = False
        self._usage_ready = True
        self._codex_resolution: CodexResolution | None = CodexResolution(
            "C:/Codex/codex.exe", "1.2.3", "path"
        )
        self._codex_update_probe = probe or FakeProbe()
        self.codex_update_auto_timer = FakeTimer()
        self.codex_update_check_button = FakeControl()
        self.codex_update_button = FakeControl()
        self.codex_latest_label = FakeControl()
        self.codex_update_status_label = FakeControl()
        self.codex_update_message_label = FakeControl()
        self.codex_update_banner_label = FakeControl()


def _new_controller(window: FakeWindow | None = None) -> tuple[CodexUpdateController, FakeWindow]:
    target = window or FakeWindow()
    return CodexUpdateController(target), target


@pytest.mark.parametrize(
    ("condition",),
    [("closing",), ("not_ready",), ("unresolved",), ("already_started",)],
)
def test_auto_check_scheduling_waits_for_window_readiness(
    condition: str,
) -> None:
    controller, window = _new_controller()
    if condition == "closing":
        window._closing = True
    elif condition == "not_ready":
        window._usage_ready = False
    elif condition == "unresolved":
        window._codex_resolution = None
    else:
        controller.on_codex_update_auto_timeout()

    controller.schedule_codex_update_auto_check()

    assert window.codex_update_auto_timer.starts == 0


def test_auto_check_timer_and_busy_guard_prevent_duplicate_checks() -> None:
    controller, window = _new_controller()
    controller.schedule_codex_update_auto_check()
    controller.schedule_codex_update_auto_check()
    assert window.codex_update_auto_timer.starts == 1

    controller.check_codex_updates()
    controller.on_codex_update_auto_timeout()
    controller.on_codex_update_auto_timeout()

    assert len(window._codex_update_probe.check_calls) == 1


def test_manual_check_parses_result_and_updates_status_banner_and_controls() -> None:
    controller, window = _new_controller()

    controller.check_codex_updates()

    assert window._codex_update_probe.check_calls == [window._codex_resolution]
    assert "Checking…" in window.codex_update_status_label.history
    assert window.codex_update_status_label.text() == "Checking…"
    assert window.codex_update_message_label.text() == ""
    assert not window.codex_update_check_button.enabled
    assert not window.codex_update_button.enabled

    controller.apply_codex_update_busy(False)
    controller.apply_codex_update_check(
        {
            "codexVersion": "1.2.3",
            "checks": {
                "updates.status": {
                    "status": "ok",
                    "details": {
                        "latest version": "1.3.0",
                        "update action": "codex update",
                        "last checked at": "2030-01-02T03:04:05Z",
                    },
                }
            },
        }
    )

    assert window.codex_latest_label.text() == "1.3.0"
    assert window.codex_update_status_label.text() == "Update available"
    assert window.codex_update_message_label.text() == "Last checked: 2030-01-02T03:04:05Z"
    assert window.codex_update_banner_label.text() == "↑ Codex update available"
    assert window.codex_update_banner_label.visible
    assert window.codex_update_check_button.enabled
    assert window.codex_update_button.enabled


def test_rejected_check_shows_unavailable_and_clears_update_state() -> None:
    probe = FakeProbe()
    probe.check_result = False
    controller, window = _new_controller(FakeWindow(probe))

    controller.check_codex_updates()

    assert window.codex_update_status_label.text() == "Unavailable"
    assert window.codex_latest_label.text() == "Unavailable"
    assert window.codex_update_message_label.text() == "Codex update check failed"
    assert window.codex_update_banner_label.text() == ""
    assert not window.codex_update_banner_label.visible
    assert window.codex_update_check_button.enabled
    assert not window.codex_update_button.enabled


def test_unknown_update_action_keeps_banner_but_disables_update_button() -> None:
    controller, window = _new_controller()

    controller.apply_codex_update_check(
        CodexUpdateInfo("1.2.3", "1.3.0", "ok", "manual or unknown", None, "1.2.3")
    )

    assert window.codex_update_status_label.text() == "Update available"
    assert window.codex_update_banner_label.visible
    assert not window.codex_update_button.enabled


def test_update_confirmation_no_then_yes_preserves_failure_and_clears_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, window = _new_controller()
    controller.apply_codex_update_check(
        CodexUpdateInfo("1.2.3", "1.3.0", "ok", "codex update", None, "1.2.3")
    )
    questions: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def answer_no(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
        questions.append((args, kwargs))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", answer_no)
    controller.update_codex()

    assert window._codex_update_probe.update_calls == []
    assert questions[0][0][0] is window
    assert questions[0][0][1:3] == (
        "Update Codex",
        "Install the available Codex update now?",
    )
    assert questions[0][0][4] == QMessageBox.StandardButton.No

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    controller.update_codex()
    assert window._codex_update_probe.update_calls == [window._codex_resolution]
    assert window.codex_update_status_label.text() == "Updating…"
    assert window.codex_update_message_label.text() == ""
    assert not window.codex_update_button.enabled

    controller.apply_codex_update_failure("Codex update failed")
    assert window.codex_update_status_label.text() == "Update failed"
    assert window.codex_update_message_label.text() == "Codex update failed"
    assert window.codex_update_banner_label.visible
    assert window.codex_update_button.enabled

    controller.update_codex()
    controller.apply_codex_update_success()
    assert window.codex_update_status_label.text() == "Update completed; restart required"
    assert window.codex_update_message_label.text() == ""
    assert window.codex_update_banner_label.text() == ""
    assert not window.codex_update_banner_label.visible
    assert not window.codex_update_button.enabled


def test_rejected_update_shows_the_existing_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = FakeProbe()
    probe.update_result = False
    controller, window = _new_controller(FakeWindow(probe))
    controller.apply_codex_update_check(
        CodexUpdateInfo("1.2.3", "1.3.0", "ok", "codex update", None, "1.2.3")
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    controller.update_codex()

    assert window._codex_update_probe.update_calls == [window._codex_resolution]
    assert window.codex_update_status_label.text() == "Update failed"
    assert window.codex_update_message_label.text() == "Codex update failed"
    assert window.codex_update_button.enabled


def test_closing_ignores_probe_busy_and_result_signals() -> None:
    controller, window = _new_controller()
    initial = CodexUpdateInfo("1.2.3", "1.3.0", "ok", "codex update", None, "1.2.3")
    controller.apply_codex_update_check(initial)
    old_values = (
        window.codex_latest_label.text(),
        window.codex_update_status_label.text(),
        window.codex_update_message_label.text(),
        window.codex_update_banner_label.text(),
        window.codex_update_button.enabled,
    )
    window._closing = True

    controller.apply_codex_update_busy(True)
    controller.apply_codex_update_check_error("late check failure")
    controller.apply_codex_update_check(
        CodexUpdateInfo("1.2.3", "1.4.0", "ok", "codex update", None, "1.2.3")
    )
    controller.apply_codex_update_failure("late update failure")
    controller.apply_codex_update_success()

    assert (
        window.codex_latest_label.text(),
        window.codex_update_status_label.text(),
        window.codex_update_message_label.text(),
        window.codex_update_banner_label.text(),
        window.codex_update_button.enabled,
    ) == old_values
