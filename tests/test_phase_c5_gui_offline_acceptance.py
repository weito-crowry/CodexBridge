from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, cast

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QGraphicsView, QScrollArea

from codex_bridge.console.i18n import tr
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog
from scripts import phase_c5_gui_visible_review as visible_review


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


def _graph_node_view_rect(graph_view: Any, node_item: Any) -> QRect:
    scene_bounds = node_item.sceneBoundingRect()
    return QRect(
        graph_view.mapFromScene(scene_bounds.topLeft()),
        graph_view.mapFromScene(scene_bounds.bottomRight()),
    ).normalized()


def _send_graph_wheel(app: QApplication, graph_view: QGraphicsView, delta: int) -> None:
    viewport = graph_view.viewport()
    position = QPoint(viewport.width() // 2, viewport.height() // 2)
    event = QWheelEvent(
        QPointF(position),
        QPointF(viewport.mapToGlobal(position)),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(viewport, event)
    app.processEvents()


def _partial_snapshot(workflow_id: str, run_id: str, version: str) -> dict[str, object]:
    snapshot = _scenario_snapshot(workflow_id, run_id, version)
    snapshot["coverage"] = "partial"
    snapshot["coverage_reason"] = "workflow_not_confirmed"
    snapshot["latest_event_id"] = 1
    return snapshot


def test_hidden_initial_graph_is_readable_centered_and_scrollable_at_both_sizes() -> None:
    app = _app()
    scenarios = visible_review.build_scenarios()

    for size_name, size in (("regular", QSize(900, 760)), ("compact", QSize(720, 600))):
        dialog = WorkflowManagementDialog()
        harness = visible_review.VisibleReviewHarness(dialog, size_name)
        graph = dialog.graph_widget
        view = graph.graph_view
        assert dialog.result_tabs.currentIndex() == 0
        assert not graph.isVisible()
        assert view.transform().m11() == pytest.approx(1.0)

        dialog.resize(size)
        dialog.show()
        app.processEvents()
        assert not graph.isVisible()
        dialog.result_tabs.setCurrentIndex(1)
        app.processEvents()

        assert graph.isVisible()
        assert view.transform().m11() == pytest.approx(1.0)
        first_node = graph.node_items["/do/0/for-zero"]
        first_node_view_rect = _graph_node_view_rect(view, first_node)
        assert first_node_view_rect.width() >= 160
        assert view.viewport().rect().intersects(first_node_view_rect)

        for row in (1, 2):
            scenario = scenarios[row]
            fixture = visible_review.build_fixture(scenario)
            event_payload = fixture["events"]
            assert isinstance(event_payload, dict)
            dialog.workflow_list.setCurrentRow(row)
            app.processEvents()
            assert graph.snapshot_identity is not None
            assert graph.snapshot_identity[:2] == (scenario.workflow_id, scenario.run_id)
            assert view.transform().m11() == pytest.approx(1.0)
            assert graph.event_cursor == event_payload["next_after_id"]

        assert len(graph.node_items) == 5
        assert view.verticalScrollBar().maximum() > view.verticalScrollBar().minimum()
        assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag
        graph.node_list.setCurrentRow(1)
        app.processEvents()
        assert graph.selected_node_id == "/do/0/for-many"
        assert "for-many" in graph.detail_view.toPlainText()

        scrollbar = view.verticalScrollBar()
        scrollbar.setValue((scrollbar.minimum() + scrollbar.maximum()) // 2)
        before_pan = scrollbar.value()
        start = QPoint(view.viewport().width() - 15, view.viewport().height() - 15)
        end = QPoint(start.x(), start.y() - 80)
        QTest.mousePress(
            view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start
        )
        QTest.mouseMove(view.viewport(), end, delay=20)
        QTest.mouseRelease(
            view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end
        )
        app.processEvents()
        assert scrollbar.value() != before_pan

        _send_graph_wheel(app, view, 120)
        assert view.transform().m11() == pytest.approx(1.15)
        dialog.result_tabs.setCurrentIndex(0)
        app.processEvents()
        dialog.result_tabs.setCurrentIndex(1)
        app.processEvents()
        assert view.transform().m11() == pytest.approx(1.15)
        _send_graph_wheel(app, view, -120)
        assert view.transform().m11() == pytest.approx(1.0)

        view.scale(1.35, 1.35)
        before_refresh_transform = view.transform()
        before_refresh_scroll = scrollbar.value()
        QTest.mouseClick(dialog.refresh_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert view.transform() == before_refresh_transform
        assert scrollbar.value() == before_refresh_scroll

        other_size = QSize(720, 600) if size == QSize(900, 760) else QSize(900, 760)
        dialog.resize(other_size)
        app.processEvents()
        assert view.transform().m11() == pytest.approx(1.35)

        dialog.workflow_list.setCurrentRow(0)
        app.processEvents()
        assert view.transform().m11() == pytest.approx(1.0)
        assert graph.snapshot_identity is not None
        assert graph.snapshot_identity[:2] == (
            scenarios[0].workflow_id,
            scenarios[0].run_id,
        )
        dialog.close()
        app.processEvents()
        assert harness.dialog is dialog


def test_same_run_refresh_version_change_preserves_view_and_new_run_resets_scale() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    dialog.resize(QSize(900, 760))
    dialog.show()
    app.processEvents()
    dialog.result_tabs.setCurrentIndex(1)
    app.processEvents()
    graph = dialog.graph_widget
    view = graph.graph_view
    workflow_id = "workflow-view-preserve"
    run_id = "run-view-preserve"
    snapshot = _partial_snapshot(workflow_id, run_id, "a")
    events = _one_graph_event_page(workflow_id, run_id)
    assert graph.set_snapshot(workflow_id, snapshot)
    assert graph.set_events(events)
    graph.node_list.setCurrentRow(1)
    app.processEvents()
    node_item = graph.node_items["/do/0/for-many"]
    view.scale(1.35, 1.35)
    scrollbar = view.verticalScrollBar()
    assert scrollbar.maximum() > scrollbar.minimum()
    scrollbar.setValue(min(scrollbar.maximum(), scrollbar.minimum() + 80))
    expected_transform = view.transform()
    expected_scroll = scrollbar.value()

    assert graph.set_snapshot(workflow_id, dict(snapshot))
    assert graph.set_events(events)
    assert graph.node_items["/do/0/for-many"] is node_item
    assert graph.selected_node_id == "/do/0/for-many"
    assert view.transform() == expected_transform
    assert scrollbar.value() == expected_scroll
    assert graph.event_cursor == 1

    version_update = dict(snapshot)
    snapshot_nodes = cast(list[dict[str, object]], snapshot["nodes"])
    updated_nodes = [dict(node) for node in snapshot_nodes]
    updated_nodes[1] = dict(updated_nodes[1], state="running", completion_observed=False)
    version_update["nodes"] = updated_nodes
    version_update["snapshot_version"] = "sha256:" + "b" * 64
    assert graph.set_snapshot(workflow_id, version_update)
    assert graph.node_items["/do/0/for-many"] is node_item
    assert graph.selected_node_id == "/do/0/for-many"
    assert view.transform() == expected_transform
    assert scrollbar.value() == expected_scroll
    assert graph.event_cursor == 1

    dialog.result_tabs.setCurrentIndex(0)
    app.processEvents()
    dialog.resize(QSize(720, 600))
    app.processEvents()
    dialog.result_tabs.setCurrentIndex(1)
    app.processEvents()
    assert view.transform().m11() == pytest.approx(1.35)

    next_run = _partial_snapshot(workflow_id, "run-view-next", "c")
    assert graph.set_snapshot(workflow_id, next_run)
    assert graph.snapshot_identity is not None
    assert graph.snapshot_identity[:2] == (workflow_id, "run-view-next")
    assert graph.selected_node_id is None
    assert view.transform().m11() == pytest.approx(1.0)
    assert graph.event_cursor == 0
    assert graph.event_list.count() == 0
    assert graph.set_events(_one_graph_event_page(workflow_id, "run-view-next"))
    graph.node_list.setCurrentRow(1)
    app.processEvents()
    assert graph.selected_node_id == "/do/0/for-many"
    assert graph.event_cursor == 1
    dialog.close()
    app.processEvents()


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


def _one_graph_event_page(workflow_id: str, run_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "events": [
            {
                "event_id": 1,
                "event_type": "task_started",
                "node_id": "/do/0/for-many",
                "occurred_at": "2026-11-17T03:00:00Z",
                "source": "interpreter",
                "metadata": {},
            }
        ],
        "next_after_id": 1,
        "has_more": False,
        "truncated": False,
        "coverage": "partial",
        "coverage_reason": "workflow_not_confirmed",
        "latest_event_id": 1,
    }


def test_clear_data_blocks_selection_signal_before_destroying_scene_items() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    graph = dialog.graph_widget
    workflow_id = "workflow-clear-selected"
    run_id = "run-clear-selected"
    snapshot = _scenario_snapshot(workflow_id, run_id, "e")
    snapshot["coverage"] = "partial"
    snapshot["coverage_reason"] = "workflow_not_confirmed"
    snapshot["latest_event_id"] = 1
    assert graph.set_snapshot(workflow_id, snapshot)
    assert graph.set_events(_one_graph_event_page(workflow_id, run_id))

    selection_spy = QSignalSpy(graph.node_list.currentItemChanged)
    graph.node_list.setCurrentRow(1)
    app.processEvents()
    assert graph.selected_node_id == "/do/0/for-many"
    assert selection_spy.count() == 1
    before_clear_count = selection_spy.count()

    graph.clear_data()
    app.processEvents()

    assert selection_spy.count() == before_clear_count
    assert graph.selected_node_id is None
    assert graph.snapshot_identity is None
    assert graph.node_list.count() == 0
    assert graph.graph_scene.items() == []
    assert graph.node_items == {}
    assert graph.edge_items == []
    assert graph.virtual_node_items == {}
    assert graph._scope_labels == []
    assert graph.event_list.count() == 0
    assert graph.event_cursor == 0
    assert graph.detail_view.toPlainText() == ""
    dialog.close()
    app.processEvents()


def test_clear_data_restores_previously_blocked_signals_and_allows_new_run() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    graph = dialog.graph_widget
    first_id = "workflow-preblocked-first"
    first_run = "run-preblocked-first"
    first = _scenario_snapshot(first_id, first_run, "f")
    first["coverage"] = "partial"
    first["coverage_reason"] = "workflow_not_confirmed"
    first["latest_event_id"] = 1
    assert graph.set_snapshot(first_id, first)
    assert graph.set_events(_one_graph_event_page(first_id, first_run))

    graph.node_list.blockSignals(True)
    graph.clear_data()
    assert graph.node_list.signalsBlocked()
    assert graph.selected_node_id is None
    assert graph.node_items == {}
    assert graph.graph_scene.items() == []

    second_id = "workflow-preblocked-second"
    second_run = "run-preblocked-second"
    second = _scenario_snapshot(second_id, second_run, "a")
    second["coverage"] = "partial"
    second["coverage_reason"] = "workflow_not_confirmed"
    second["latest_event_id"] = 1
    assert graph.set_snapshot(second_id, second)
    assert graph.set_events(_one_graph_event_page(second_id, second_run))
    graph.select_node("/do/0/switch-one")
    assert graph.selected_node_id == "/do/0/switch-one"
    assert graph.node_list.signalsBlocked()
    graph.clear_data()
    assert graph.node_list.signalsBlocked()
    graph.node_list.blockSignals(False)

    third_id = "workflow-after-clear"
    third_run = "run-after-clear"
    third = _scenario_snapshot(third_id, third_run, "b")
    third["coverage"] = "partial"
    third["coverage_reason"] = "workflow_not_confirmed"
    third["latest_event_id"] = 1
    assert graph.set_snapshot(third_id, third)
    assert graph.set_events(_one_graph_event_page(third_id, third_run))
    graph.node_list.setCurrentRow(1)
    app.processEvents()
    assert graph.selected_node_id == "/do/0/for-many"
    assert graph.event_cursor == 1
    assert graph.event_list.count() == 1
    dialog.close()
    app.processEvents()


def test_dialog_workflow_switch_stress_keeps_graph_and_detail_in_sync() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    graph = dialog.graph_widget
    scenarios = (
        ("workflow-a", "run-a", "A", "succeeded"),
        ("workflow-b", "run-b", "B", "succeeded"),
        ("workflow-c", "run-c", "C", "succeeded"),
        ("workflow-unknown", "run-unknown", "Unknown", "unknown"),
    )
    rows: list[dict[str, object]] = []
    payloads: dict[str, dict[str, object]] = {}
    for index, (workflow_id, run_id, label, status) in enumerate(scenarios):
        snapshot = _scenario_snapshot(workflow_id, run_id, chr(ord("a") + index))
        snapshot["workflow_status"] = status
        snapshot["coverage"] = "partial"
        snapshot["coverage_reason"] = "workflow_not_confirmed"
        snapshot["latest_event_id"] = 1
        payloads[workflow_id] = {
            "run_id": run_id,
            "label": label,
            "status": status,
            "snapshot": snapshot,
            "events": _one_graph_event_page(workflow_id, run_id),
        }
        rows.append({"workflow_id": workflow_id, "status": status})

    applied: list[str] = []

    def apply_workflow(workflow_id: str) -> None:
        payload = payloads[workflow_id]
        graph_payload = payload["snapshot"]
        event_payload = payload["events"]
        assert isinstance(graph_payload, dict)
        assert isinstance(event_payload, dict)
        detail = {
            "workflow_id": workflow_id,
            "status": payload["status"],
            "created_at": "2026-11-17T03:00:00Z",
            "updated_at": "2026-11-17T03:01:00Z",
            "worker_state": "completed" if payload["status"] == "succeeded" else "unknown",
            "max_live_codex_jobs": 1,
        }
        dialog.set_detail(detail)
        dialog.set_result({"result": {"final_text": f"synthetic result {payload['label']}"}})
        assert graph.set_snapshot(workflow_id, graph_payload)
        assert graph.set_events(event_payload)
        dialog.set_message(f"Synthetic scenario {payload['label']}")
        applied.append(workflow_id)

    dialog.workflow_selected.connect(apply_workflow)
    dialog.set_bridge_ready(False)
    dialog.set_start_enabled(False)
    dialog.set_workflows(rows, selected_workflow_id=scenarios[0][0])
    dialog.resize(900, 760)
    dialog.show()
    app.processEvents()
    assert dialog.selected_workflow_id == scenarios[0][0]

    graph_index = {
        workflow_id: index for index, (workflow_id, _run, _label, _status) in enumerate(scenarios)
    }
    for cycle in range(50):
        dialog.result_tabs.setCurrentIndex(1 if cycle < 25 else 0)
        app.processEvents()
        for target_index in (1, 2, 3, 0):
            workflow_id, run_id, label, status = scenarios[target_index]
            dialog.workflow_list.setCurrentRow(target_index)
            app.processEvents()
            assert dialog.selected_workflow_id == workflow_id
            assert dialog.detail_labels["workflow_id"].text() == workflow_id
            assert dialog.detail_labels["status"].text() != "Not available"
            assert f"synthetic result {label}" in dialog.final_text_view.toPlainText()
            assert label in dialog.status_label.text()
            assert graph.snapshot_identity is not None
            assert graph.snapshot_identity[:2] == (workflow_id, run_id)
            assert graph.event_cursor == 1
            assert graph.event_list.count() == 1
            assert graph_index[workflow_id] == target_index
            assert applied[-1] == workflow_id

            graph.node_list.setCurrentRow(1)
            app.processEvents()
            assert graph.selected_node_id == "/do/0/for-many"
            assert graph.event_cursor == 1
            assert not dialog.start_button.isEnabled()

    assert len(applied) == 201
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
