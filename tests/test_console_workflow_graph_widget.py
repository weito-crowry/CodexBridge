from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.workflow_graph_widget import WorkflowExecutionGraphWidget


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _snapshot(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": "workflow-one",
        "run_id": "run-one",
        "workflow_status": "running",
        "coverage": "partial",
        "coverage_reason": "workflow_not_confirmed",
        "latest_event_id": 8,
        "snapshot_version": "sha256:" + "1" * 64,
        "truncated": False,
        "counts": {"nodes": 1, "edges": 3, "executions": 3},
        "nodes": [
            {
                "node_id": "/do/0/loop",
                "operation": "for",
                "parent_scope": "/do/0",
                "order": 0,
                "state": "unknown",
                "completion_observed": False,
                "executions": [
                    {
                        "execution_id": "execution-1",
                        "task_identity": "do[0].loop[0]",
                        "state": "running",
                        "started_at": "2026-11-17T00:00:00Z",
                        "completed_at": None,
                        "completion_observed": False,
                        "observation_complete": False,
                        "evidence_sources": ["node_events"],
                        "evidence_conflict": False,
                        "job_id": "job-one",
                        "thread_id": "C:\\Users\\secret\\thread-1",
                        "turn_id": "turn-one",
                    },
                    {
                        "execution_id": "execution-2",
                        "task_identity": "do[0].loop[1]",
                        "state": "unknown",
                        "started_at": None,
                        "completed_at": None,
                        "completion_observed": False,
                        "observation_complete": False,
                        "evidence_sources": [],
                        "evidence_conflict": False,
                        "job_id": "job-two",
                    },
                    {
                        "execution_id": "execution-3",
                        "task_identity": "do[0].loop[2]",
                        "state": "succeeded",
                        "started_at": "2026-11-17T00:00:02Z",
                        "completed_at": "2026-11-17T00:00:03Z",
                        "completion_observed": True,
                        "observation_complete": True,
                        "evidence_sources": ["codex_job_ledger"],
                        "evidence_conflict": False,
                        "job_id": "job-three",
                        "turn_id": "sk-proj-1234567890abcdefghijklmnop",
                    },
                ],
            }
        ],
        "edges": [
            {
                "source": "@scope:/do/0:entry",
                "target": "/do/0/loop",
                "kind": "scope_entry",
            },
            {
                "source": "/do/0/loop",
                "target": "@scope:/do/0:exit",
                "kind": "scope_exit",
            },
            {
                "source": "@scope:/do/0:exit",
                "target": "@scope:/do/0:entry",
                "kind": "loop_repeat",
            },
        ],
    }
    value.update(overrides)
    return value


def test_graph_draws_virtual_scope_edges_and_keeps_unknown_distinct() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()

    assert widget.set_snapshot("workflow-one", _snapshot())

    assert widget.graph_scene.items()
    assert widget.node_items["/do/0/loop"].state == "unknown"
    assert widget.node_items["/do/0/loop"].toolTip() == "for · /do/0/loop · unknown"
    assert widget.metadata_label.text().find("partial") >= 0
    assert widget.metadata_label.text().find("workflow_not_confirmed") >= 0
    assert len(widget.edge_items) == 3
    assert len(widget.virtual_node_items) == 2
    assert len(widget.graph_scene.items()) <= 2048


def test_graph_details_show_each_execution_and_redact_untrusted_text() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    secret_identity = "task <script>alert(1)</script> Bearer abcdef /home/user/private"
    snapshot = _snapshot()
    snapshot["nodes"][0]["executions"][0]["task_identity"] = secret_identity
    snapshot["nodes"][0]["executions"][0]["thread_id"] = "C:\\Users\\secret\\thread-1"
    snapshot["nodes"][0]["attributes"] = {"prompt": "raw prompt must not be retained"}
    snapshot["nodes"][0]["task_name"] = "raw YAML task name"
    snapshot["nodes"][0]["executions"][0]["raw_yaml"] = "raw YAML must not be retained"

    assert widget.set_snapshot("workflow-one", snapshot)
    widget.select_node("/do/0/loop")

    detail = widget.detail_view.toPlainText()
    assert all(f"execution-{index}" in detail for index in range(1, 4))
    assert all(f"job-{name}" in detail for name in ("one", "two", "three"))
    assert "C:\\Users" not in detail and "thread-1" not in detail
    assert "sk-proj-1234567890abcdefghijklmnop" not in detail
    assert "abcdef" not in detail
    assert "<script>" not in detail
    assert "private" not in detail
    assert "prompt must not be retained" not in detail
    assert "attributes" not in widget._node_records["/do/0/loop"]
    assert "raw_yaml" not in widget._node_records["/do/0/loop"]["executions"][0]
    assert "completion" in detail.lower() or "完了" in detail


def test_graph_event_history_is_safe_sorted_deduplicated_and_bounded() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    assert widget.set_snapshot("workflow-one", _snapshot())
    events = [
        {
            "event_id": event_id,
            "event_type": "task_started",
            "node_id": "/do/0/loop",
            "task_identity": "secret task identity is not shown",
            "execution_id": f"execution-{event_id}",
            "occurred_at": "2026-11-17T00:00:00Z",
            "source": "interpreter",
            "metadata": {"iteration_ordinal": event_id, "operation": "for"},
        }
        for event_id in range(1, 181)
    ]
    first_page = {
        "schema_version": 1,
        "workflow_id": "workflow-one",
        "run_id": "run-one",
        "events": events[:100],
        "next_after_id": 100,
        "has_more": True,
        "truncated": False,
        "coverage": "partial",
        "coverage_reason": "journal_missing",
        "latest_event_id": 200,
    }
    second_page = dict(first_page, events=events[100:], next_after_id=180)

    assert widget.set_events(first_page)
    assert widget.set_events(second_page)
    assert widget.set_events(second_page)
    assert not widget.set_events(dict(second_page, run_id="run-two"))
    assert not widget.set_events(dict(second_page, schema_version=2))
    assert widget.event_cursor == 180
    assert widget.event_list.count() == 150
    assert widget.older_event_count == 30
    assert widget.event_list.item(0).text().find("31") >= 0
    assert all("secret task identity" not in widget.event_list.item(i).text() for i in range(150))
    assert "partial" in widget.metadata_label.text()
    assert (
        "truncated" in widget.metadata_label.text().lower()
        or "省略" in widget.metadata_label.text()
    )
    assert widget.set_events(
        {
            "schema_version": 1,
            "workflow_id": "workflow-one",
            "run_id": "run-one",
            "events": [],
            "next_after_id": 180,
            "has_more": False,
            "truncated": False,
            "coverage": "unavailable",
            "coverage_reason": "journal_missing",
            "latest_event_id": 0,
        }
    )
    assert "180/0" in widget.metadata_label.text()


def test_graph_scene_is_bounded_and_same_snapshot_reuses_items_and_view_state() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    nodes = [
        {
            "node_id": f"/do/0/task-{index}",
            "operation": "set",
            "parent_scope": f"/do/{index // 8}",
            "order": index,
            "state": "not_started",
            "completion_observed": False,
            "executions": [],
        }
        for index in range(300)
    ]
    edges = [
        {
            "source": f"/do/0/task-{index % 300}",
            "target": f"/do/0/task-{(index + 1) % 300}",
            "kind": "sequence",
        }
        for index in range(1100)
    ]
    snapshot = _snapshot(
        nodes=nodes,
        edges=edges,
        counts={"nodes": 300, "edges": 1100, "executions": 0},
    )

    assert widget.set_snapshot("workflow-one", snapshot)
    assert len(widget.node_items) == 256
    assert len(widget.edge_items) <= 1024
    assert widget.omitted_graph_count > 0
    node_item = widget.node_items["/do/0/task-0"]
    widget.select_node("/do/0/task-0")
    widget.graph_view.scale(1.25, 1.25)
    transform = widget.graph_view.transform()
    scroll = widget.graph_view.horizontalScrollBar().value()

    updated = dict(snapshot)
    updated["snapshot_version"] = "sha256:" + "2" * 64
    updated_nodes = [dict(node) for node in nodes]
    updated_nodes[0] = dict(updated_nodes[0], state="running")
    updated["nodes"] = updated_nodes
    assert widget.set_snapshot("workflow-one", updated)

    assert widget.node_items["/do/0/task-0"] is node_item
    assert widget.selected_node_id == "/do/0/task-0"
    assert widget.graph_view.transform() == transform
    assert widget.graph_view.horizontalScrollBar().value() == scroll


def test_same_snapshot_version_does_not_rebuild_graph() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    payload = _snapshot()
    assert widget.set_snapshot("workflow-one", payload)
    scene_items = tuple(widget.graph_scene.items())
    node_item = widget.node_items["/do/0/loop"]

    assert widget.set_snapshot("workflow-one", dict(payload))

    assert tuple(widget.graph_scene.items()) == scene_items
    assert widget.node_items["/do/0/loop"] is node_item


def test_graph_never_turns_unknown_or_unavailable_coverage_into_success() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    snapshot = _snapshot(
        workflow_status="needs_reconciliation",
        coverage="unavailable",
        coverage_reason="journal_missing",
        truncated=True,
        counts={"nodes": 2, "edges": 5, "executions": 5},
    )

    assert widget.set_snapshot("workflow-one", snapshot)

    label = widget.metadata_label.text()
    assert "needs_reconciliation" in label
    assert "Coverage: unavailable" in label
    assert "journal_missing" in label
    assert "success" not in label.lower()
    assert "Truncated items: 5" in label or "省略項目数: 5" in label


def test_graph_displays_unicode_node_names_and_rejects_invalid_snapshot_identity() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    snapshot = _snapshot()
    node = dict(snapshot["nodes"][0], node_id="/do/0/日本語のタスク")
    snapshot["nodes"] = [node]
    snapshot["edges"] = []

    assert widget.set_snapshot("workflow-one", snapshot)
    assert "日本語のタスク" in widget.node_items["/do/0/日本語のタスク"].toolTip()
    assert not widget.set_snapshot("workflow-one", _snapshot(snapshot_version="sha256:bad"))
    assert not widget.set_snapshot("workflow-one", _snapshot(workflow_status=[]))
    bad_edge = _snapshot()
    bad_edge["edges"] = [{"source": "/do/0/missing", "target": "/do/0/loop", "kind": "sequence"}]
    assert not widget.set_snapshot("workflow-one", bad_edge)
    assert widget.snapshot_identity is not None


def test_clicking_a_graph_node_selects_its_execution_details() -> None:
    _app()
    widget = WorkflowExecutionGraphWidget()
    widget.resize(900, 650)
    assert widget.set_snapshot("workflow-one", _snapshot())
    widget.show()
    QApplication.processEvents()
    item = widget.node_items["/do/0/loop"]
    point = widget.graph_view.mapFromScene(item.mapToScene(QPointF(100.0, 25.0)))

    QTest.mouseClick(widget.graph_view.viewport(), Qt.MouseButton.LeftButton, pos=point)

    assert widget.selected_node_id == "/do/0/loop"
    assert "execution-1" in widget.detail_view.toPlainText()
    widget.close()
