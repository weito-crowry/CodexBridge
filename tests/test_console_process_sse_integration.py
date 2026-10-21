from __future__ import annotations

import json
import time
from typing import Any

from PySide6.QtWidgets import QApplication

from codex_bridge.activity import Activity
from codex_bridge.console.activity_pane import ActivityPane
from codex_bridge.console.process_inspection import (
    CommandProcessState,
    InspectionResult,
    ProcessEvidence,
)
from codex_bridge.console.process_monitor import ProcessMonitorWidget
from codex_bridge.console.sse import SseEvent, parse_activity_event


def _application() -> QApplication:
    application = QApplication.instance() or QApplication([])
    assert application is not None
    return application


def _activity(
    activity_id: str,
    item_id: str | None,
    activity_type: str,
    status: str,
    summary: str,
    details: dict[str, object],
) -> Activity:
    return Activity(
        activity_id=activity_id,
        timestamp="2026-10-22T12:00:00Z",
        thread_id="thread-73",
        turn_id="turn-73",
        item_id=item_id,
        type=activity_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=summary,
        details=details,
    )


def _parse(activity: Activity) -> dict[str, object]:
    parsed = parse_activity_event(
        SseEvent(
            event="activity",
            event_id=None,
            data=json.dumps(activity.to_dict(), ensure_ascii=False),
        )
    )
    assert parsed is not None
    return parsed


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


def test_sse_command_item_id_reaches_live_process_monitor_and_output() -> None:
    application = _application()
    inspected: list[tuple[Any, ...]] = []

    def inspector(commands: tuple[Any, ...], evidence: ProcessEvidence, cancel: object):
        inspected.append(commands)
        return InspectionResult(
            tuple(
                CommandProcessState(command, "Unverified: fake inspector", None, ())
                for command in commands
            ),
            evidence,
        )

    monitor = ProcessMonitorWidget(inspector=inspector, platform_name="win32")
    pane = ActivityPane(process_monitor=monitor)
    pane.show()

    first_item_id = "exec-d2a05554-e0bd-4360-8f8e-e43ec2cfb61a"
    second_item_id = "exec-8f9a6194-83db-4e07-bf44-f618c7171b4c"
    prior_snapshot = [
        _activity(
            "old-start",
            first_item_id,
            "command_started",
            "in_progress",
            "First command",
            {"process_id": "40064", "started_at_ms": 1_792_670_400_000},
        ).to_dict(),
        _activity(
            "old-complete",
            first_item_id,
            "command_completed",
            "completed",
            "First command",
            {"process_id": "40064", "exit_code": 0, "completed_at_ms": 1_792_670_460_000},
        ).to_dict(),
    ]
    pane.set_snapshot({"state": "completed", "recent_activities": prior_snapshot})

    second_start = _activity(
        "live-second-start",
        second_item_id,
        "command_started",
        "in_progress",
        "Second live PowerShell command",
        {"process_id": "8648", "started_at_ms": 1_792_670_460_000},
    )
    parsed_start = _parse(second_start)
    pane.append_activity(parsed_start)
    assert _wait(
        application,
        lambda: (
            f"Thread / turn / item: thread-73 / turn-73 / {second_item_id}"
            in monitor.process_text.toPlainText()
        ),
    )

    output = _parse(
        _activity(
            "live-second-output",
            second_item_id,
            "command_output",
            "in_progress",
            "LIVE_24 output line\n",
            {"process_id": "8648"},
        )
    )
    pane.append_activity(output)
    complete = _parse(
        _activity(
            "live-second-complete",
            second_item_id,
            "command_completed",
            "completed",
            "Second live PowerShell command",
            {"process_id": "8648", "exit_code": 0, "completed_at_ms": 1_792_670_580_000},
        )
    )
    pane.append_activity(complete)
    pane.append_activity(complete)

    assert _wait(
        application,
        lambda: (
            f"Thread / turn / item: thread-73 / turn-73 / {second_item_id}"
            in monitor.process_text.toPlainText()
            and "Command status: completed · exit 0" in monitor.process_text.toPlainText()
        ),
    )
    process_text = monitor.process_text.toPlainText()
    assert process_text.count(second_item_id) == 1
    assert f"Thread / turn / item: thread-73 / turn-73 / {first_item_id}" in process_text
    assert "App Server ID: 8648" in process_text
    assert monitor.record_count == 2
    assert any(
        command.key.item_id == second_item_id
        and command.key.thread_id == "thread-73"
        and command.key.turn_id == "turn-73"
        and command.app_server_process_id == "8648"
        and command.exit_code == 0
        for batch in inspected
        for command in batch
    )
    output_text = pane.command_output_text.toPlainText()
    assert f"[Command {second_item_id} · App Server process id: 8648]" in output_text
    assert "LIVE_24 output line" in output_text
    assert "[Output for command unknown]" not in output_text

    unattributed = _parse(
        _activity(
            "live-output-without-item",
            None,
            "command_output",
            "in_progress",
            "orphan output stays unattributed",
            {"process_id": "8648"},
        )
    )
    pane.append_activity(unattributed)
    assert (
        "[Unattributed command output (missing item id)]" in pane.command_output_text.toPlainText()
    )
    assert "orphan output stays unattributed" in pane.command_output_text.toPlainText()
    assert monitor.record_count == 2

    monitor.shutdown()
