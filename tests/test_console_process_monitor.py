from __future__ import annotations

import threading
import time
from typing import Any

from PySide6.QtWidgets import QApplication

from codex_bridge.console.activity_pane import ActivityPane
from codex_bridge.console.process_inspection import (
    CommandDescriptor,
    CommandProcessState,
    InspectionResult,
    ProcessEvidence,
    ProcessIdentity,
    ProcessView,
)
from codex_bridge.console.process_monitor import ProcessMonitorWidget


def _application() -> QApplication:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    return application


def _activity(
    kind: str,
    *,
    thread_id: str = "thread-a",
    activity_id: str | None = None,
    item_id: str = "item-a",
    process_id: str = "pty-42",
    status: str = "in_progress",
    exit_code: int | None = None,
) -> dict[str, Any]:
    details: dict[str, object] = {"process_id": process_id, "started_at_ms": 1_000_000}
    if exit_code is not None:
        details["exit_code"] = exit_code
    return {
        "activity_id": activity_id or f"{kind}-{thread_id}-{item_id}",
        "thread_id": thread_id,
        "turn_id": "turn-a",
        "item_id": item_id,
        "type": kind,
        "timestamp": "1970-01-01T00:16:40+00:00",
        "status": status,
        "summary": f"command for {thread_id}",
        "details": details,
    }


def _wait(application: QApplication, predicate: object, timeout: float = 2.0) -> bool:
    check = predicate
    assert callable(check)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if check():
            return True
        time.sleep(0.005)
    application.processEvents()
    return bool(check())


def _result(command: CommandDescriptor, *, root_status: str = "Running") -> InspectionResult:
    root = ProcessView(123, 990.0, "pwsh.exe", "pwsh.exe -NoProfile", 1, "sleeping", root_status)
    return InspectionResult(
        commands=(CommandProcessState(command, root_status, root, ()),),
        evidence=ProcessEvidence(roots=((command.key, ProcessIdentity(123, 990.0)),)),
    )


def test_snapshot_and_live_events_merge_once_and_keep_command_and_os_status_separate() -> None:
    application = _application()
    calls: list[tuple[CommandDescriptor, ...]] = []
    worker_threads: list[int] = []

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        calls.append(commands)
        worker_threads.append(threading.get_ident())
        return _result(commands[0], root_status="Running")

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    start = _activity("command_started", process_id="123", status="in_progress")
    complete = _activity("command_completed", process_id="123", status="interrupted", exit_code=1)
    widget.set_snapshot({"recent_activities": [start, complete]})
    widget.append_activity(start)
    widget.append_activity(complete)
    widget.append_activity(_activity("command_started", activity_id="retry", process_id="123"))
    widget.refresh_processes()

    assert _wait(
        application,
        lambda: "Command status: interrupted · exit 1" in widget.process_text.toPlainText(),
        timeout=5.0,
    )
    assert widget.record_count == 1
    assert "command for thread-a" in widget.process_text.toPlainText()
    assert "App Server ID: 123" in widget.process_text.toPlainText()
    assert "Command status: interrupted · exit 1" in widget.process_text.toPlainText()
    assert "OS process: Running" in widget.process_text.toPlainText()
    assert widget.process_text.isReadOnly()
    assert worker_threads[0] != threading.get_ident()
    widget.toggle_button.click()
    widget.resize(300, 220)
    widget.show()
    application.processEvents()
    assert widget.process_text.verticalScrollBar().maximum() > 0
    widget.process_text.selectAll()
    assert widget.process_text.textCursor().hasSelection()
    widget.process_text.copy()
    assert "Windows PID: 123" in application.clipboard().text()
    widget.shutdown()


def test_refresh_button_and_timer_use_worker_and_manual_refresh_is_available() -> None:
    application = _application()
    calls = 0
    called = threading.Event()

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        nonlocal calls
        calls += 1
        called.set()
        return _result(commands[0])

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    widget.append_activity(_activity("command_started", process_id="123"))
    widget.refresh_button.click()
    assert called.wait(1.0)
    assert _wait(application, lambda: widget.process_text.toPlainText().count("OS process:") == 1)
    assert _wait(application, lambda: not widget._busy and not widget._refresh_pending)
    calls_before_timer = calls
    widget.process_timer.timeout.emit()
    assert _wait(application, lambda: calls > calls_before_timer)
    assert widget.process_timer.interval() == 4_000
    widget.hide()
    assert not widget.process_timer.isActive()
    calls_before_show = calls
    widget.show()
    assert _wait(application, lambda: calls > calls_before_show)
    widget.shutdown()


def test_status_anchor_is_captured_for_worker_and_cleared_on_selection_reset() -> None:
    application = _application()
    observed_anchors: list[object] = []

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        observed_anchors.append(getattr(evidence, "app_server_os_pid", None))
        result = _result(commands[0])
        return InspectionResult(result.commands, evidence)

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    widget.set_snapshot(
        {
            "app_server_os_pid": 220132,
            "recent_activities": [_activity("command_started", process_id="11385")],
        }
    )

    assert _wait(application, lambda: observed_anchors == [220132])
    widget.reset()
    widget.append_activity(_activity("command_started", activity_id="next-command"))

    assert _wait(application, lambda: len(observed_anchors) == 2)
    assert observed_anchors == [220132, None]
    widget.shutdown()


def test_activity_pane_forwards_and_clears_local_process_anchor() -> None:
    application = _application()
    observed_anchors: list[int | None] = []

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ) -> InspectionResult:
        observed_anchors.append(evidence.app_server_os_pid)
        result = _result(commands[0])
        return InspectionResult(result.commands, evidence)

    monitor = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    pane = ActivityPane(process_monitor=monitor)
    pane.show()
    pane.set_snapshot(
        {
            "state": "in_progress",
            "thread_metadata": {},
            "app_server_os_pid": 220132,
            "recent_activities": [_activity("command_started")],
        },
        reset=True,
    )

    assert _wait(application, lambda: observed_anchors == [220132])
    pane.set_error("connection unavailable")

    assert _wait(application, lambda: observed_anchors == [220132, None])
    assert "App Server anchor unavailable" not in monitor.process_text.toPlainText()
    pane.set_empty_state("Select a thread")
    assert monitor.record_count == 0
    pane.close()
    monitor.shutdown()


def test_app_server_restart_discards_stale_worker_result() -> None:
    application = _application()
    first_started = threading.Event()
    release_first = threading.Event()
    observed_anchors: list[int | None] = []

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ) -> InspectionResult:
        anchor = evidence.app_server_os_pid
        observed_anchors.append(anchor)
        if anchor == 220132:
            first_started.set()
            release_first.wait(1.0)
        result = _result(commands[0])
        return InspectionResult(result.commands, evidence)

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    widget.set_app_server_os_pid(220132)
    widget.append_activity(_activity("command_started"))
    assert first_started.wait(1.0)

    widget.set_app_server_os_pid(330)
    release_first.set()

    assert _wait(
        application,
        lambda: (
            observed_anchors == [220132, 330]
            and not widget._busy
            and "command for thread-a" in widget.process_text.toPlainText()
        ),
    )
    assert widget._evidence.app_server_os_pid == 330
    widget.shutdown()


def test_selection_change_discards_stale_completion_and_clears_previous_records() -> None:
    application = _application()
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        thread_id = commands[0].key.thread_id
        calls.append(thread_id)
        if thread_id == "thread-a":
            started.set()
            release.wait(1.0)
        return _result(commands[0])

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    widget.append_activity(_activity("command_started", thread_id="thread-a", process_id="123"))
    widget.refresh_processes()
    assert started.wait(1.0)
    widget.reset()
    widget.append_activity(_activity("command_started", thread_id="thread-b", process_id="456"))
    widget.refresh_processes()
    release.set()

    assert _wait(application, lambda: "thread-b" in calls)
    assert _wait(application, lambda: "command for thread-b" in widget.process_text.toPlainText())
    assert "command for thread-a" not in widget.process_text.toPlainText()
    assert widget.record_count == 1
    widget.shutdown()


def test_selected_thread_records_roll_at_fifty_commands() -> None:
    application = _application()

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        states = tuple(CommandProcessState(command, "Unverified", None, ()) for command in commands)
        return InspectionResult(states, evidence)

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    for index in range(51):
        widget.append_activity(
            _activity(
                "command_started",
                activity_id=f"start-{index}",
                item_id=f"item-{index}",
            )
        )
    widget.refresh_processes()

    assert widget.record_count == 50
    assert _wait(application, lambda: "item-50" in widget.process_text.toPlainText())
    assert "item-0" not in widget.process_text.toPlainText()
    widget.shutdown()


def test_completion_without_an_observed_start_does_not_create_a_process_record() -> None:
    _application()
    widget = ProcessMonitorWidget(platform_name="win32")
    widget.append_activity(
        _activity("command_completed", process_id="123", status="failed", exit_code=1)
    )

    assert widget.record_count == 0
    assert widget.process_text.toPlainText() == ""
    widget.shutdown()


def test_close_cancels_polling_and_discards_late_worker_results() -> None:
    application = _application()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        started.set()
        release.wait(1.0)
        finished.set()
        return _result(commands[0])

    widget = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    widget.show()
    widget.append_activity(_activity("command_started", process_id="123"))
    widget.refresh_processes()
    assert started.wait(1.0)
    assert widget.process_timer.isActive()
    widget.shutdown()
    release.set()

    assert finished.wait(1.0)
    assert _wait(application, lambda: not widget._busy)
    assert not widget.process_timer.isActive()
    assert widget.record_count == 0
    assert widget.process_text.toPlainText() == ""


def test_unsupported_platform_and_error_status_are_visible_and_recoverable() -> None:
    application = _application()
    unsupported = ProcessMonitorWidget(platform_name="linux")
    assert "Windows only" in unsupported.process_text.toPlainText()
    unsupported.shutdown()

    calls = 0

    def flaky_inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private process details")
        return _result(commands[0])

    widget = ProcessMonitorWidget(inspector=flaky_inspector, platform_name="win32")
    widget.show()
    widget.append_activity(_activity("command_started", process_id="123"))
    widget.refresh_processes()
    assert _wait(
        application, lambda: "Process inspection failed" in widget.process_text.toPlainText()
    )
    assert "private process details" not in widget.process_text.toPlainText()
    widget.refresh_button.click()
    assert _wait(application, lambda: "OS process: Running" in widget.process_text.toPlainText())
    widget.shutdown()


def test_missing_output_item_id_is_labeled_unattributed() -> None:
    _application()
    pane = ActivityPane()
    pane.process_monitor.shutdown()
    pane.append_activity(
        {
            "activity_id": "known-command",
            "type": "command_started",
            "item_id": "item-known",
            "summary": "known command",
            "details": {},
        }
    )
    pane.append_activity(
        {
            "activity_id": "unattributed-output",
            "type": "command_output",
            "summary": "orphan output",
            "details": {},
        }
    )

    output = pane.command_output_text.toPlainText()
    assert "[Unattributed command output (missing item id)]" in output
    assert "[Output for command item-known]" not in output
    assert "orphan output" in output


def test_activity_pane_snapshot_rehydrates_and_error_keeps_process_records() -> None:
    application = _application()

    def inspector(
        commands: tuple[CommandDescriptor, ...], evidence: ProcessEvidence, cancel: object
    ):
        return _result(commands[0])

    monitor = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    pane = ActivityPane(process_monitor=monitor)
    pane.show()
    start = _activity("command_started", process_id="123", status="in_progress")
    complete = _activity("command_completed", process_id="123", status="failed", exit_code=1)
    pane.set_snapshot({"state": "failed", "recent_activities": [start, complete]})
    pane.append_activity(start)
    pane.append_activity(complete)

    assert monitor.record_count == 1
    assert _wait(
        application,
        lambda: "Command status: failed · exit 1" in monitor.process_text.toPlainText(),
    )
    pane.set_error("Bridge unavailable")
    assert monitor.record_count == 1
    pane.set_empty_state("Loading activity…")
    assert monitor.record_count == 0
    assert monitor.process_text.toPlainText() == ""
    monitor.shutdown()
