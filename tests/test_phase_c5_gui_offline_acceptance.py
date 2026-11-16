from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from codex_bridge.console.i18n import tr
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _execution(
    index: int,
    state: str,
    *,
    job_id: str | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> dict[str, object]:
    completed = state in {"succeeded", "failed"}
    return {
        "execution_id": f"offline-execution-{index}",
        "task_identity": f"offline-task-{index}",
        "state": state,
        "started_at": started_at,
        "completed_at": completed_at,
        "completion_observed": completed,
        "observation_complete": completed,
        "evidence_sources": ["codex_job_ledger" if job_id else "node_events"],
        "evidence_conflict": False,
        **({"job_id": job_id} if job_id else {}),
    }


def _node(
    node_id: str, operation: str, order: int, state: str, executions: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "operation": operation,
        "parent_scope": "/do/0",
        "order": order,
        "state": state,
        "completion_observed": state in {"succeeded", "failed"},
        "executions": executions,
    }


def _scenario_snapshot(
    workflow_id: str = "workflow-scenarios",
    run_id: str = "run-scenarios",
    version: str = "1",
) -> dict[str, object]:
    nodes = [
        _node("/do/0/for-zero", "for", 0, "unknown", []),
        _node(
            "/do/0/for-many",
            "for",
            1,
            "succeeded",
            [_execution(index, "succeeded") for index in range(1, 4)],
        ),
        _node(
            "/do/0/switch-one",
            "switch",
            2,
            "succeeded",
            [
                _execution(
                    4,
                    "succeeded",
                    job_id="job-offline-1",
                    started_at="2026-11-17T01:00:00Z",
                    completed_at="2026-11-17T01:00:01Z",
                )
            ],
        ),
        _node(
            "/do/0/do-two",
            "do",
            3,
            "running",
            [
                _execution(5, "running", job_id="job-offline-2"),
                _execution(6, "unknown", job_id="job-offline-3"),
            ],
        ),
        _node(
            "/do/0/call-three",
            "call",
            4,
            "failed",
            [
                _execution(7, "failed", job_id="job-offline-4"),
                _execution(8, "failed", job_id="job-offline-5"),
                _execution(
                    9,
                    "failed",
                    job_id="job-offline-6",
                    started_at="2026-11-17T01:05:00Z",
                    completed_at="2026-11-17T01:05:01Z",
                ),
            ],
        ),
    ]
    edges = [
        {"source": "@scope:/do/0:entry", "target": "/do/0/for-zero", "kind": "scope_entry"},
        {"source": "/do/0/for-zero", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
        {
            "source": "@scope:/do/0:exit",
            "target": "@scope:/do/0:entry",
            "kind": "loop_repeat",
        },
    ]
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": "needs_reconciliation",
        "coverage": "unavailable",
        "coverage_reason": "journal_missing",
        "latest_event_id": 0,
        "snapshot_version": "sha256:" + version * 64,
        "truncated": False,
        "counts": {"nodes": 5, "edges": 3, "executions": 9},
        "nodes": nodes,
        "edges": edges,
    }


def _simple_snapshot(workflow_id: str, run_id: str, version: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": "running",
        "coverage": "partial",
        "coverage_reason": "workflow_not_confirmed",
        "latest_event_id": 2,
        "snapshot_version": "sha256:" + version * 64,
        "truncated": False,
        "counts": {"nodes": 1, "edges": 0, "executions": 0},
        "nodes": [
            _node("/do/0/history", "set", 0, "running", []),
        ],
        "edges": [],
    }


def test_dialog_layout_supports_regular_and_compact_sizes() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    dialog.set_workflows([{"workflow_id": "workflow-layout", "status": "running"}])
    final_text = "\n".join(f"offline result line {index}" for index in range(80))
    dialog.set_result({"result": {"final_text": final_text}})
    assert dialog.result_tabs.currentIndex() == 0
    assert dialog.graph_widget.set_snapshot(
        "workflow-layout", _scenario_snapshot("workflow-layout")
    )
    dialog.show()
    app.processEvents()
    requested_actions: list[str] = []
    dialog.refresh_requested.connect(lambda: requested_actions.append("refresh"))
    dialog.start_requested.connect(lambda: requested_actions.append("start"))

    for size in (QSize(900, 760), QSize(720, 600)):
        dialog.resize(size)
        dialog.result_tabs.setCurrentIndex(1)
        app.processEvents()
        assert dialog.size() == size
        for widget in (
            dialog.workflow_list,
            dialog.detail_labels["status"],
            dialog.result_tabs,
            dialog.graph_widget,
            dialog.graph_widget.graph_view,
            dialog.graph_widget.graph_view.viewport(),
            dialog.graph_widget.node_list,
            dialog.graph_widget.detail_view,
            dialog.graph_widget.event_list,
        ):
            assert widget.geometry().width() > 0
            assert widget.geometry().height() > 0
        for button in (
            dialog.refresh_button,
            dialog.start_button,
            dialog.result_button,
            dialog.copy_button,
            dialog.close_button,
        ):
            assert button.isVisible()
            assert button.width() > 0 and button.height() > 0
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().center()))
        QTest.mouseClick(dialog.refresh_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
        expected_action_cycles = 1 if size == QSize(900, 760) else 2
        assert requested_actions == ["refresh", "start"] * expected_action_cycles
        dialog.result_tabs.setCurrentIndex(0)
        app.processEvents()
        assert dialog.selected_workflow_id == "workflow-layout"
        assert dialog.final_text_view.toPlainText() == final_text

    scroll_area = dialog.content_scroll_area
    assert isinstance(scroll_area, QScrollArea)
    assert scroll_area.widgetResizable()
    content = scroll_area.widget()
    assert content is not None
    for widget in (dialog.workflow_list, dialog.jobs_group, dialog.result_tabs):
        assert content.isAncestorOf(widget)
    for button in (
        dialog.refresh_button,
        dialog.start_button,
        dialog.result_button,
        dialog.copy_button,
        dialog.close_button,
    ):
        assert not content.isAncestorOf(button)

    def visible_in_scroll_area(widget: Any) -> bool:
        rectangle = QRect(widget.mapTo(scroll_area.viewport(), QPoint(0, 0)), widget.size())
        return scroll_area.viewport().rect().intersects(rectangle)

    dialog.jobs_group.hide()
    dialog.resize(QSize(900, 760))
    dialog.result_tabs.setCurrentIndex(1)
    app.processEvents()
    assert dialog.size() == QSize(900, 760)
    assert scroll_area.verticalScrollBar().maximum() == 0

    dialog.set_detail(
        {
            "workflow_id": "workflow-layout",
            "status": "running",
            "max_live_codex_jobs": 2,
            "jobs": [
                {
                    "job_id": "job-offline-layout",
                    "task_identity": "do[0].offline",
                    "state": "RUNNING",
                }
            ],
        }
    )
    dialog.resize(QSize(720, 600))
    dialog.result_tabs.setCurrentIndex(1)
    app.processEvents()
    assert dialog.size() == QSize(720, 600)
    assert scroll_area.verticalScrollBar().maximum() > 0
    assert scroll_area.horizontalScrollBar().maximum() > 0

    scroll_area.verticalScrollBar().setValue(0)
    assert visible_in_scroll_area(dialog.workflow_list)
    scroll_area.ensureWidgetVisible(dialog.job_list)
    app.processEvents()
    assert visible_in_scroll_area(dialog.job_list)
    dialog.graph_widget.select_node("/do/0/for-zero")
    scroll_area.verticalScrollBar().setValue(scroll_area.verticalScrollBar().maximum())
    scroll_area.horizontalScrollBar().setValue(scroll_area.horizontalScrollBar().maximum())
    app.processEvents()
    assert visible_in_scroll_area(dialog.result_tabs)
    assert visible_in_scroll_area(dialog.graph_widget.graph_view.viewport())
    assert visible_in_scroll_area(dialog.graph_widget.detail_view)
    assert dialog.graph_widget.detail_view.toPlainText()
    assert dialog.selected_workflow_id == "workflow-layout"
    for button in (
        dialog.refresh_button,
        dialog.start_button,
        dialog.result_button,
        dialog.copy_button,
        dialog.close_button,
    ):
        assert button.isVisible()
        assert dialog.rect().contains(button.mapTo(dialog, button.rect().center()))

    dialog.result_tabs.setCurrentIndex(0)
    scroll_area.ensureWidgetVisible(dialog.final_text_view)
    app.processEvents()
    assert visible_in_scroll_area(dialog.final_text_view)
    text_scroll = dialog.final_text_view.verticalScrollBar()
    assert text_scroll.maximum() > 0
    text_scroll.setValue(text_scroll.maximum())
    assert dialog.final_text_view.toPlainText() == final_text
    assert "offline result line 79" in dialog.final_text_view.toPlainText()
    QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not dialog.isVisible()


def test_graph_projects_offline_states_job_counts_and_virtual_edges() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    graph = dialog.graph_widget
    snapshot = _scenario_snapshot()

    assert graph.set_snapshot("workflow-scenarios", snapshot)
    assert "needs_reconciliation" in graph.metadata_label.text()
    assert "Coverage: unavailable" in graph.metadata_label.text()
    assert "journal_missing" in graph.metadata_label.text()
    assert len(graph.virtual_node_items) == 2
    assert len(graph.edge_items) == 3

    expected = (
        ("/do/0/for-zero", "for", "unknown", 0),
        ("/do/0/for-many", "for", "succeeded", 3),
        ("/do/0/switch-one", "switch", "succeeded", 1),
        ("/do/0/do-two", "do", "running", 2),
        ("/do/0/call-three", "call", "failed", 3),
    )
    details: list[str] = []
    for node_id, operation, state, execution_count in expected:
        assert graph.node_items[node_id].toolTip() == f"{operation} · {node_id} · {state}"
        graph.select_node(node_id)
        detail = graph.detail_view.toPlainText()
        details.append(detail)
        assert f"{tr('Execution count')}: {execution_count}" in detail
    assert "skipped" not in details[0].lower()
    assert "succeeded" not in details[0].lower()

    rendered = "\n".join(details)
    assert set(re.findall(r"\bjob-offline-\d+\b", rendered)) == {
        f"job-offline-{index}" for index in range(1, 7)
    }
    assert set(re.findall(r"\b2026-11-17T\d{2}:\d{2}:\d{2}Z\b", rendered)) == {
        "2026-11-17T01:00:00Z",
        "2026-11-17T01:00:01Z",
        "2026-11-17T01:05:00Z",
        "2026-11-17T01:05:01Z",
    }
    dialog.close()
    app.processEvents()


def test_graph_keeps_event_history_for_same_run_and_clears_new_identity() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    graph = dialog.graph_widget
    dialog.set_result({"result": {"final_text": "result survives graph updates"}})
    snapshot = _simple_snapshot("workflow-history", "run-one", "a")
    event_page = {
        "schema_version": 1,
        "workflow_id": "workflow-history",
        "run_id": "run-one",
        "events": [
            {
                "event_id": 1,
                "event_type": "task_started",
                "node_id": "/do/0/history",
                "occurred_at": "2026-11-17T02:00:00Z",
                "source": "interpreter",
                "metadata": {},
            }
        ],
        "next_after_id": 1,
        "has_more": True,
        "truncated": False,
        "coverage": "partial",
        "coverage_reason": "workflow_not_confirmed",
        "latest_event_id": 2,
    }

    assert graph.set_snapshot("workflow-history", snapshot)
    graph.select_node("/do/0/history")
    assert graph.set_events(event_page)
    item = graph.node_items["/do/0/history"]
    event_text = graph.event_list.item(0).text()
    graph.graph_view.scale(1.25, 1.25)
    transform = graph.graph_view.transform()

    assert graph.set_snapshot("workflow-history", dict(snapshot))
    updated = dict(snapshot, snapshot_version="sha256:" + "b" * 64)
    updated["nodes"] = [dict(snapshot["nodes"][0], state="succeeded")]
    assert graph.set_snapshot("workflow-history", updated)
    assert graph.node_items["/do/0/history"] is item
    assert graph.selected_node_id == "/do/0/history"
    assert graph.graph_view.transform() == transform
    assert graph.event_cursor == 1 and graph.has_more_events
    assert graph.event_list.item(0).text() == event_text
    assert "result survives graph updates" in dialog.final_text_view.toPlainText()

    new_run = _simple_snapshot("workflow-history", "run-two", "c")
    assert graph.set_snapshot("workflow-history", new_run)
    assert graph.selected_node_id is None
    assert graph.event_cursor == 0 and not graph.has_more_events
    assert graph.event_list.count() == 0
    new_workflow = _simple_snapshot("workflow-history-two", "run-three", "d")
    assert graph.set_snapshot("workflow-history-two", new_workflow)
    assert graph.event_cursor == 0 and graph.event_list.count() == 0
    assert "result survives graph updates" in dialog.final_text_view.toPlainText()
    dialog.close()
    app.processEvents()


def test_three_second_timer_requests_one_incremental_event_page(
    monkeypatch: Any,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    helpers = importlib.import_module("test_console_workflow_controller")
    app = _app()
    window, client, controller = helpers._controller()
    try:
        assert controller.timer.interval() == 3_000
        client.succeed(client.calls[-1], 200, helpers._list_payload("workflow-one"))
        client.succeed(client.calls[-1], 200, helpers._detail("workflow-one"))
        controller.dialog.result_tabs.setCurrentIndex(1)
        tasks_call = client.calls[-1]
        assert tasks_call[0] == "tasks"
        client.succeed(tasks_call, 200, helpers._graph_snapshot())
        event_call = client.calls[-1]
        assert event_call[0] == "task_events"
        assert event_call[3] == ("workflow-one", 0, 100)
        client.succeed(event_call, 200, helpers._graph_events())

        controller.timer.stop()
        controller.timer.start()
        before_tick = len(client.calls)
        QTest.qWait(controller.timer.interval() + 250)
        assert len(client.calls) == before_tick + 1
        list_call = client.calls[-1]
        assert list_call[0] == "list"
        client.succeed(list_call, 200, helpers._list_payload("workflow-one"))
        tasks_call = client.calls[-1]
        assert tasks_call[0] == "tasks"
        client.succeed(tasks_call, 200, helpers._graph_snapshot(version="2"))
        event_call = client.calls[-1]
        assert event_call[0] == "task_events"
        assert event_call[3] == ("workflow-one", 1, 100)
        assert sum(call[0] == "task_events" for call in client.calls) == 2
        client.succeed(event_call, 200, helpers._graph_events(after_id=1))
        assert controller.dialog.graph_widget.event_cursor == 2
    finally:
        controller.shutdown()
        controller.timer.stop()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
