from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from codex_bridge.console.workflow_controller import WorkflowManagementController


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeWorkflowClient:
    def __init__(self) -> None:
        self.workflow_succeeded = Signal()
        self.workflow_failed = Signal()
        self.calls: list[tuple[str, str, str, object | None]] = []
        self.aborted: list[str] = []

    def get_control_workflows(self, token: str, *, key: str) -> bool:
        self.calls.append(("list", key, token, None))
        return True

    def get_control_workflow_detail(self, workflow_id: str, token: str, *, key: str) -> bool:
        self.calls.append(("detail", key, token, workflow_id))
        return True

    def get_control_workflow_result(self, workflow_id: str, token: str, *, key: str) -> bool:
        self.calls.append(("result", key, token, workflow_id))
        return True

    def get_control_workflow_tasks(self, workflow_id: str, token: str, *, key: str) -> bool:
        self.calls.append(("tasks", key, token, workflow_id))
        return True

    def get_control_workflow_task_events(
        self,
        workflow_id: str,
        token: str,
        after_id: int = 0,
        limit: int = 100,
        *,
        key: str,
    ) -> bool:
        self.calls.append(("task_events", key, token, (workflow_id, after_id, limit)))
        return True

    def post_control_workflow(self, token: str, payload: object, *, key: str) -> bool:
        self.calls.append(("start", key, token, payload))
        return True

    def abort_workflow_request(self, key: str) -> None:
        self.aborted.append(key)

    def succeed(
        self, call: tuple[str, str, str, object | None], status: int, payload: object
    ) -> None:
        self.workflow_succeeded.emit(call[1], status, payload)

    def fail(self, call: tuple[str, str, str, object | None], status: int, code: str) -> None:
        self.workflow_failed.emit(call[1], status, code)


class FakeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._control_token: str | None = "token-one"
        self._ready = True

    def _can_display_control_token(self) -> bool:
        return self._ready and isinstance(self._control_token, str) and bool(self._control_token)


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _controller() -> tuple[FakeWindow, FakeWorkflowClient, WorkflowManagementController]:
    _app()
    window = FakeWindow()
    client = FakeWorkflowClient()
    controller = WorkflowManagementController(window, client, ())
    controller.show()
    return window, client, controller


ControllerFactory = Callable[
    [], tuple[FakeWindow, FakeWorkflowClient, WorkflowManagementController]
]


@pytest.fixture
def managed_controller() -> Iterator[ControllerFactory]:
    app = _app()
    created: list[tuple[FakeWindow, WorkflowManagementController]] = []

    def create() -> tuple[FakeWindow, FakeWorkflowClient, WorkflowManagementController]:
        result = _controller()
        created.append((result[0], result[2]))
        return result

    yield create

    try:
        for _window, controller in reversed(created):
            controller.shutdown()
    finally:
        for window, _controller_instance in reversed(created):
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        created.clear()


def _list_payload(*workflow_ids: str) -> dict[str, object]:
    return {
        "workflows": [
            {
                "workflow_id": workflow_id,
                "status": "running",
                "created_at": "created",
                "updated_at": "updated",
            }
            for workflow_id in workflow_ids
        ],
        "limit": 50,
        "truncated": False,
    }


def _detail(workflow_id: str, status: str = "running") -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "run_id": "run-1",
        "status": status,
        "created_at": "created",
        "updated_at": "updated",
        "worker_state": status,
        "terminal": status in {"succeeded", "failed", "failed_to_start"},
        "job_id": "job-1",
        "thread_id": "thread-1",
    }


def _graph_snapshot(
    workflow_id: str = "workflow-one", run_id: str = "run-one", version: str = "1"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": "running",
        "coverage": "unavailable",
        "coverage_reason": "journal_missing",
        "latest_event_id": 2,
        "snapshot_version": "sha256:" + version * 64,
        "truncated": False,
        "counts": {"nodes": 0, "edges": 0, "executions": 0},
        "nodes": [],
        "edges": [],
    }


def _graph_events(
    workflow_id: str = "workflow-one", run_id: str = "run-one", after_id: int = 0
) -> dict[str, object]:
    event_rows: list[dict[str, object]] = []
    if after_id < 2:
        event_id = after_id + 1
        event_rows = [
            {
                "event_id": event_id,
                "event_type": "workflow_completed",
                "node_id": None,
                "task_identity": None,
                "execution_id": None,
                "occurred_at": "2026-11-17T00:00:00Z",
                "source": "interpreter",
                "metadata": {},
            }
        ]
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "events": event_rows,
        "next_after_id": event_rows[-1]["event_id"] if event_rows else after_id,
        "has_more": bool(event_rows and event_rows[-1]["event_id"] < 2),
        "truncated": False,
        "coverage": "unavailable",
        "coverage_reason": "journal_missing",
        "latest_event_id": 2,
    }


def test_workflow_controller_loads_list_detail_and_result_in_order(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    assert [call[0] for call in client.calls] == ["list"]
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    assert [call[0] for call in client.calls] == ["list", "detail"]
    client.succeed(client.calls[-1], 200, _detail("workflow-one", "succeeded"))
    assert [call[0] for call in client.calls] == ["list", "detail", "result"]
    client.succeed(
        client.calls[-1],
        200,
        {"result": {"result": {"final_text": "workflow complete"}}},
    )

    assert "workflow complete" in controller.dialog.final_text_view.toPlainText()
    assert controller.dialog.result_json.toPlainText().startswith("{")
    controller.shutdown()


def test_workflow_controller_polls_graph_only_when_visible_and_pages_one_event_batch_per_tick(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    assert not any(call[0] in {"tasks", "task_events"} for call in client.calls)

    controller.dialog.result_tabs.setCurrentIndex(1)
    tasks_call = client.calls[-1]
    assert tasks_call[0] == "tasks"
    client.succeed(tasks_call, 200, _graph_snapshot())
    first_events_call = client.calls[-1]
    assert first_events_call[0] == "task_events"
    assert first_events_call[3] == ("workflow-one", 0, 100)
    first_events = _graph_events()
    client.succeed(first_events_call, 200, first_events)
    assert controller.dialog.graph_widget.event_cursor == 1
    assert client.calls[-1] == first_events_call

    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, _list_payload("workflow-one"))
    tasks_call = client.calls[-1]
    assert tasks_call[0] == "tasks"
    client.succeed(tasks_call, 200, _graph_snapshot(version="2"))
    second_events_call = client.calls[-1]
    assert second_events_call[0] == "task_events"
    assert second_events_call[3] == ("workflow-one", 1, 100)
    client.succeed(second_events_call, 200, _graph_events(after_id=1))
    assert controller.dialog.graph_widget.event_cursor == 2

    controller._refresh_if_ready()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    pending_graph = client.calls[-1]
    assert pending_graph[0] == "tasks"
    controller.dialog.refresh_button.click()
    assert pending_graph[1] in client.aborted
    assert client.calls[-1][0] == "list"
    refreshed_list = client.calls[-1]
    client.succeed(refreshed_list, 200, _list_payload("workflow-one"))
    assert client.calls[-1][0] == "detail"
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    assert client.calls[-1][0] == "tasks"
    controller.shutdown()


def test_workflow_controller_graph_errors_do_not_change_workflow_state_and_start_has_priority(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    controller.dialog.result_tabs.setCurrentIndex(1)
    graph_call = client.calls[-1]
    client.fail(graph_call, 503, "workflow_service_unavailable")

    assert controller.dialog.detail_labels["status"].text() == "Workflow status: running"
    assert (
        "Workflow service is unavailable"
        in controller.dialog.graph_widget.graph_status_label.text()
    )
    assert controller.timer.isActive()

    controller._refresh_if_ready()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    graph_call = client.calls[-1]
    assert graph_call[0] == "tasks"
    controller.dialog.start_button.click()
    assert graph_call[1] in client.aborted
    assert controller._active_start_dialog is not None
    assert not any(call[0] == "start" for call in client.calls)
    controller.shutdown()


def test_workflow_controller_result_request_preempts_graph_poll(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one", "succeeded"))
    assert client.calls[-1][0] == "result"
    client.succeed(client.calls[-1], 200, {"result": {"final_text": "done"}})
    controller.dialog.result_tabs.setCurrentIndex(1)
    graph_call = client.calls[-1]
    assert graph_call[0] == "tasks"

    controller.dialog.result_button.click()

    assert graph_call[1] in client.aborted
    assert client.calls[-1][0] == "result"
    controller.shutdown()


def test_workflow_controller_periodic_list_poll_preempts_slow_graph_get(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    controller.dialog.result_tabs.setCurrentIndex(1)
    slow_graph = client.calls[-1]
    assert slow_graph[0] == "tasks"

    controller._refresh_if_ready()

    assert slow_graph[1] in client.aborted
    assert client.calls[-1][0] == "list"
    controller.shutdown()


def test_workflow_controller_aborts_stale_graph_requests_on_selection_token_and_close(
    managed_controller: ControllerFactory,
) -> None:
    window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one", "workflow-two"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    controller.dialog.result_tabs.setCurrentIndex(1)
    stale_tasks = client.calls[-1]
    assert stale_tasks[0] == "tasks"

    controller.dialog.workflow_list.setCurrentRow(1)
    assert stale_tasks[1] in client.aborted
    assert controller.dialog.selected_workflow_id == "workflow-two"
    client.succeed(stale_tasks, 200, _graph_snapshot())
    assert controller.dialog.graph_widget.snapshot_identity is None
    client.succeed(client.calls[-1], 200, _detail("workflow-two"))
    active_tasks = client.calls[-1]
    assert active_tasks[0] == "tasks"

    window._control_token = "token-two"
    controller.notify_readiness_changed()
    assert active_tasks[1] in client.aborted
    list_call = client.calls[-1]
    assert list_call[0] == "list" and list_call[2] == "token-two"
    client.succeed(active_tasks, 200, _graph_snapshot("workflow-two"))
    assert controller.dialog.graph_widget.snapshot_identity is None
    controller.dialog.close()
    assert list_call[1] in client.aborted
    controller.shutdown()


def test_workflow_controller_graph_403_blocks_auth_and_rejects_wrong_workflow_dto(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one"))
    controller.dialog.result_tabs.setCurrentIndex(1)
    graph_call = client.calls[-1]
    wrong_workflow = _graph_snapshot(workflow_id="workflow-two")
    client.succeed(graph_call, 200, wrong_workflow)

    assert "invalid" in controller.dialog.graph_widget.graph_status_label.text().lower()
    assert controller.dialog.detail_labels["status"].text() == "Workflow status: running"
    assert not any(call[0] == "task_events" for call in client.calls)

    controller._refresh_if_ready()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    graph_call = client.calls[-1]
    assert graph_call[0] == "tasks"
    client.fail(graph_call, 403, "forbidden")
    assert controller._auth_blocked_token == "token-one"
    assert not controller.timer.isActive()
    assert "not authorized" in controller.dialog.graph_widget.graph_status_label.text().lower()
    controller.shutdown()


def test_workflow_controller_repeated_identical_polls_preserve_display_and_avoid_extra_gets(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    dialog = controller.dialog
    dialog.workflow_list.setMaximumHeight(120)
    rows = [
        {
            "workflow_id": f"workflow-{index}",
            "status": "succeeded" if index == 0 else "running",
            "created_at": "created",
            "updated_at": "updated",
        }
        for index in range(50)
    ]
    payload = {"workflows": rows, "truncated": False}
    selected: list[str] = []
    dialog.workflow_selected.connect(selected.append)

    client.succeed(client.calls[-1], 200, payload)
    detail = _detail("workflow-0", "succeeded")
    detail["max_live_codex_jobs"] = 2
    detail["jobs"] = [
        {
            "job_id": "job-1",
            "task_identity": "do[0].first",
            "state": "SUCCEEDED",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }
    ]
    client.succeed(client.calls[-1], 200, detail)
    client.succeed(
        client.calls[-1],
        200,
        {"result": {"result": {"final_text": "workflow complete"}}, "value": "stable"},
    )
    QApplication.instance().processEvents()
    dialog.workflow_list.scrollToItem(dialog.workflow_list.item(49))
    scrollbar = dialog.workflow_list.verticalScrollBar()
    scroll_value = scrollbar.value()
    assert scrollbar.maximum() > 0
    assert scroll_value > 0

    original_items = [dialog.workflow_list.item(index) for index in range(50)]
    detail_labels = {key: label.text() for key, label in dialog.detail_labels.items()}
    job_item = dialog.job_list.item(0)
    job_text = job_item.text()
    final_text = dialog.final_text_view.toPlainText()
    result_json = dialog.result_json.toPlainText()
    request_counts = {
        kind: sum(call[0] == kind for call in client.calls) for kind in ("detail", "result")
    }

    for _ in range(2):
        controller._refresh_if_ready()
        list_call = client.calls[-1]
        assert list_call[0] == "list"
        client.succeed(list_call, 200, payload)

        assert dialog.selected_workflow_id == "workflow-0"
        assert all(dialog.workflow_list.item(index) is original_items[index] for index in range(50))
        assert scrollbar.value() == scroll_value
        assert {key: label.text() for key, label in dialog.detail_labels.items()} == detail_labels
        assert dialog.job_list.item(0) is job_item
        assert dialog.job_list.item(0).text() == job_text
        assert dialog.final_text_view.toPlainText() == final_text
        assert dialog.result_json.toPlainText() == result_json
        assert dialog.copy_button.isEnabled()
        assert dialog.result_button.isEnabled()
        assert selected == ["workflow-0"]
        assert sum(call[0] == "detail" for call in client.calls) == request_counts["detail"]
        assert sum(call[0] == "result" for call in client.calls) == request_counts["result"]
    controller.shutdown()


def test_workflow_controller_refreshes_only_changed_selected_row_and_keeps_visible_result(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    rows = [
        {
            "workflow_id": "workflow-one",
            "status": "running",
            "created_at": "created",
            "updated_at": "updated",
        },
        {
            "workflow_id": "workflow-two",
            "status": "queued",
            "created_at": "created",
            "updated_at": "updated",
        },
    ]
    payload = {"workflows": rows, "truncated": False}
    selected: list[str] = []
    controller.dialog.workflow_selected.connect(selected.append)
    client.succeed(client.calls[-1], 200, payload)
    initial_detail = _detail("workflow-one")
    initial_detail["max_live_codex_jobs"] = 2
    initial_detail["jobs"] = [
        {"job_id": "job-one", "task_identity": "do[0].first", "state": "RUNNING"}
    ]
    client.succeed(client.calls[-1], 200, initial_detail)
    controller.dialog.set_result({"result": {"final_text": "previous visible result"}})
    items = [controller.dialog.workflow_list.item(index) for index in range(2)]
    unchanged_row_text = items[1].text()

    changed_rows = [dict(row) for row in rows]
    changed_rows[0]["status"] = "succeeded"
    changed_rows[0]["updated_at"] = "updated later"
    changed_payload = {"workflows": changed_rows, "truncated": False}
    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, changed_payload)

    assert client.calls[-1][0] == "detail"
    changed_detail_call = client.calls[-1]
    assert changed_detail_call[3] == "workflow-one"
    assert controller.dialog.workflow_list.item(0) is items[0]
    assert controller.dialog.workflow_list.item(1) is items[1]
    assert "succeeded" in items[0].text()
    assert items[1].text() == unchanged_row_text
    assert controller.dialog.selected_workflow_id == "workflow-one"
    assert selected == ["workflow-one"]
    assert controller.dialog.detail_labels["status"].text() == "Workflow status: running"
    assert controller.dialog.job_list.count() == 1
    assert controller.dialog.final_text_view.toPlainText() == "previous visible result"

    client.succeed(changed_detail_call, 200, _detail("workflow-one", "succeeded"))
    assert client.calls[-1][0] == "result"
    result_call = client.calls[-1]
    assert result_call[3] == "workflow-one"
    assert controller.dialog.final_text_view.toPlainText() == "previous visible result"
    client.succeed(result_call, 200, {"result": {"final_text": "new result"}})
    assert controller.dialog.final_text_view.toPlainText() == "new result"

    nonselected_rows = [dict(row) for row in changed_rows]
    nonselected_rows[1]["updated_at"] = "non-selected update"
    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, {"workflows": nonselected_rows, "truncated": False})

    assert controller.dialog.workflow_list.item(0) is items[0]
    assert controller.dialog.workflow_list.item(1) is items[1]
    assert controller.dialog.selected_workflow_id == "workflow-one"
    assert selected == ["workflow-one"]
    assert sum(call[0] == "detail" for call in client.calls) == 2
    assert sum(call[0] == "result" for call in client.calls) == 1
    controller.shutdown()


def test_workflow_controller_reuses_items_and_clears_state_when_selection_disappears(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    dialog = controller.dialog
    dialog.workflow_list.setMaximumHeight(120)
    rows = [
        {
            "workflow_id": f"workflow-{index}",
            "status": "running",
            "created_at": "created",
            "updated_at": "updated",
        }
        for index in range(50)
    ]
    payload = {"workflows": rows, "truncated": False}
    selected: list[str] = []
    cleared: list[bool] = []
    dialog.workflow_selected.connect(selected.append)
    dialog.selection_cleared.connect(lambda: cleared.append(True))
    client.succeed(client.calls[-1], 200, payload)
    client.succeed(client.calls[-1], 200, _detail("workflow-0"))
    dialog.workflow_list.setCurrentRow(1)
    selected_detail = _detail("workflow-1", "succeeded")
    selected_detail["max_live_codex_jobs"] = 2
    selected_detail["jobs"] = [
        {
            "job_id": "job-selected",
            "task_identity": "do[0].selected",
            "state": "SUCCEEDED",
        }
    ]
    client.succeed(client.calls[-1], 200, selected_detail)
    client.succeed(client.calls[-1], 200, {"result": {"final_text": "selected result"}})
    job_item = dialog.job_list.item(0)
    QApplication.instance().processEvents()
    dialog.workflow_list.scrollToItem(dialog.workflow_list.item(35))
    scrollbar = dialog.workflow_list.verticalScrollBar()
    scroll_value = scrollbar.value()
    assert scroll_value > 0
    original_items = {
        dialog.workflow_list.item(index).data(Qt.ItemDataRole.UserRole): dialog.workflow_list.item(
            index
        )
        for index in range(dialog.workflow_list.count())
    }

    reordered_rows = [
        dict(rows[0]),
        {
            "workflow_id": "workflow-new",
            "status": "queued",
            "created_at": "created",
            "updated_at": "updated",
        },
        dict(rows[1]),
        *[dict(row) for row in rows[3:]],
    ]
    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, {"workflows": reordered_rows, "truncated": False})

    item_by_id = {
        dialog.workflow_list.item(index).data(Qt.ItemDataRole.UserRole): dialog.workflow_list.item(
            index
        )
        for index in range(dialog.workflow_list.count())
    }
    assert dialog.selected_workflow_id == "workflow-1"
    assert selected == ["workflow-0", "workflow-1"]
    assert item_by_id["workflow-0"] is original_items["workflow-0"]
    assert item_by_id["workflow-1"] is original_items["workflow-1"]
    assert "workflow-new" not in original_items
    assert scrollbar.value() == scroll_value
    assert dialog.detail_labels["workflow_id"].text() == "workflow-1"
    assert dialog.job_list.item(0) is job_item
    assert dialog.final_text_view.toPlainText() == "selected result"

    removed_rows = [dict(row) for row in reordered_rows if row["workflow_id"] != "workflow-1"]
    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, {"workflows": removed_rows, "truncated": False})

    assert dialog.selected_workflow_id == "workflow-0"
    assert controller._selected_workflow_id == "workflow-0"
    assert controller._result_loaded_for is None
    assert selected == ["workflow-0", "workflow-1", "workflow-0"]
    assert dialog.detail_labels["workflow_id"].text() == "Not available"
    assert dialog.job_list.count() == 0
    assert dialog.final_text_view.toPlainText() == ""
    fallback_detail = client.calls[-1]
    assert fallback_detail[0] == "detail" and fallback_detail[3] == "workflow-0"
    client.succeed(fallback_detail, 200, _detail("workflow-0"))

    controller._pending_selected_workflow_id = "workflow-stale"
    controller._refresh_if_ready()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, {"workflows": [], "truncated": False})

    assert dialog.workflow_list.count() == 0
    assert dialog.selected_workflow_id is None
    assert controller._selected_workflow_id is None
    assert controller._result_loaded_for is None
    assert controller._pending_selected_workflow_id is None
    assert dialog.detail_labels["workflow_id"].text() == "Not available"
    assert dialog.final_text_view.toPlainText() == ""
    assert selected == ["workflow-0", "workflow-1", "workflow-0"]
    assert cleared == [True]
    controller.shutdown()


def test_workflow_controller_manual_refresh_reloads_detail_without_clearing_result(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    payload = _list_payload("workflow-one")
    client.succeed(client.calls[-1], 200, payload)
    client.succeed(client.calls[-1], 200, _detail("workflow-one", "succeeded"))
    client.succeed(client.calls[-1], 200, {"result": {"final_text": "persisted result"}})
    old_result = controller.dialog.final_text_view.toPlainText()
    detail_count = sum(call[0] == "detail" for call in client.calls)
    result_count = sum(call[0] == "result" for call in client.calls)

    controller.refresh()
    list_call = client.calls[-1]
    assert list_call[0] == "list"
    client.succeed(list_call, 200, payload)

    assert client.calls[-1][0] == "detail"
    detail_call = client.calls[-1]
    assert controller.dialog.final_text_view.toPlainText() == old_result
    assert sum(call[0] == "detail" for call in client.calls) == detail_count + 1
    client.succeed(detail_call, 200, _detail("workflow-one", "succeeded"))

    assert controller.dialog.final_text_view.toPlainText() == old_result
    assert sum(call[0] == "result" for call in client.calls) == result_count
    controller.shutdown()


def test_workflow_controller_ignores_old_detail_after_selection_changes(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one", "workflow-two"))
    old_detail = client.calls[-1]
    assert old_detail[0] == "detail" and old_detail[3] == "workflow-one"

    controller.dialog.workflow_list.setCurrentRow(1)
    new_detail = client.calls[-1]
    assert new_detail[0] == "detail" and new_detail[3] == "workflow-two"
    assert old_detail[1] in client.aborted
    client.succeed(old_detail, 200, _detail("workflow-one", "failed"))
    assert controller.dialog.detail_labels["workflow_id"].text() == "Not available"

    client.succeed(new_detail, 200, _detail("workflow-two"))
    assert controller.dialog.detail_labels["workflow_id"].text() == "workflow-two"
    controller.shutdown()


def test_workflow_controller_ignores_old_result_after_selection_changes(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one", "workflow-two"))
    client.succeed(client.calls[-1], 200, _detail("workflow-one", "succeeded"))
    stale_result = client.calls[-1]
    assert stale_result[0] == "result"

    controller.dialog.workflow_list.setCurrentRow(1)
    new_detail = client.calls[-1]
    assert new_detail[0] == "detail" and new_detail[3] == "workflow-two"
    assert stale_result[1] in client.aborted
    client.succeed(stale_result, 200, {"result": {"final_text": "stale result"}})

    assert controller.dialog.final_text_view.toPlainText() == ""
    assert controller.dialog.result_json.toPlainText() == ""
    client.succeed(new_detail, 200, _detail("workflow-two"))
    assert controller.dialog.final_text_view.toPlainText() == ""
    controller.shutdown()


def test_workflow_controller_clears_jobs_when_selection_changes(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one", "workflow-two"))
    first_detail = client.calls[-1]
    first = _detail("workflow-one")
    first["max_live_codex_jobs"] = 2
    first["jobs"] = [
        {
            "job_id": "first-job-id",
            "task_identity": "do[0].first",
            "state": "SUCCEEDED",
            "thread_id": "first-thread-id",
            "turn_id": "first-turn-id",
        }
    ]
    client.succeed(first_detail, 200, first)
    assert controller.dialog.job_list.count() == 1

    controller.dialog.workflow_list.setCurrentRow(1)

    assert controller.dialog.job_list.count() == 0
    assert controller.dialog.jobs_group.isHidden()
    second_detail = client.calls[-1]
    client.succeed(second_detail, 200, _detail("workflow-two"))
    assert controller.dialog.job_list.count() == 0
    controller.shutdown()


def test_workflow_controller_keeps_communication_failures_separate_from_run_state(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload("workflow-one"))
    detail_call = client.calls[-1]
    client.fail(detail_call, 503, "workflow_service_unavailable")

    assert controller.dialog.detail_labels["status"].text() in {
        "Not available",
        "利用できません",
    }
    assert "unknown" not in controller.dialog.detail_labels["status"].text().lower()
    assert controller.dialog.status_label.text() in {
        "Workflow service is unavailable",
        "ワークフローサービスを利用できません",
    }
    controller.shutdown()


def test_workflow_controller_aborts_requests_on_readiness_change_and_close(
    managed_controller: ControllerFactory,
) -> None:
    window, client, controller = managed_controller()
    old_list = client.calls[-1]
    window._ready = False
    controller.notify_readiness_changed()

    assert old_list[1] in client.aborted
    assert not controller.has_inflight_request
    assert controller.dialog.bridge_state_label.text() == "Bridge unavailable"
    assert not controller.timer.isActive()
    count = len(client.calls)
    controller._refresh_if_ready()
    assert len(client.calls) == count

    window._ready = True
    window._control_token = "token-two"
    controller.notify_readiness_changed()
    assert client.calls[-1][0] == "list"
    assert client.calls[-1][2] == "token-two"
    assert controller.timer.isActive()
    new_list = client.calls[-1]
    controller.dialog.close()
    assert new_list[1] in client.aborted
    assert not controller.timer.isActive()
    controller.shutdown()


def test_workflow_controller_suppresses_overlapping_poll_and_retries_same_start_id(
    managed_controller: ControllerFactory,
    tmp_path: Path,
) -> None:
    _window, client, controller = managed_controller()
    controller._refresh_if_ready()
    assert len(client.calls) == 1
    client.succeed(client.calls[-1], 200, _list_payload())
    controller.dialog.start_button.click()
    start_dialog = controller._active_start_dialog
    assert start_dialog is not None
    yaml_file = tmp_path / "workflow.yaml"
    yaml_file.write_bytes(b"name: fixed\n")
    start_dialog.yaml_path_edit.setText(str(yaml_file))
    start_dialog.workspace_path_edit.setText(str(tmp_path))
    start_dialog.submit_button.click()

    first_post = client.calls[-1]
    assert first_post[0] == "start"
    start_dialog.submit_button.click()
    assert sum(call[0] == "start" for call in client.calls) == 1
    request_id = first_post[3]["client_request_id"]
    client.fail(first_post, 0, "timeout")
    assert start_dialog.retry_button.isHidden() is False
    before_poll = sum(call[0] == "start" for call in client.calls)
    controller._refresh_if_ready()
    assert sum(call[0] == "start" for call in client.calls) == before_poll
    poll_call = client.calls[-1]
    assert poll_call[0] == "list"
    client.succeed(poll_call, 200, _list_payload())
    start_dialog.retry_button.click()

    second_post = client.calls[-1]
    assert second_post[0] == "start"
    assert second_post[3] == first_post[3]
    assert second_post[3]["client_request_id"] == request_id
    client.succeed(second_post, 202, {"workflow_id": "workflow-new", "status": "queued"})
    assert client.calls[-1][0] == "list"
    client.succeed(client.calls[-1], 200, _list_payload("workflow-new"))
    assert controller.dialog.selected_workflow_id == "workflow-new"
    assert client.calls[-1][0] == "detail"
    controller.shutdown()


def test_workflow_controller_does_not_auto_retry_authentication_failure(
    managed_controller: ControllerFactory,
) -> None:
    _window, client, controller = managed_controller()
    first_list = client.calls[-1]
    client.fail(first_list, 403, "forbidden")
    assert controller.dialog.start_button.isEnabled() is False
    assert not controller.timer.isActive()
    count = len(client.calls)
    controller._refresh_if_ready()
    assert len(client.calls) == count

    controller.refresh()
    assert len(client.calls) == count + 1
    assert controller.timer.isActive()
    controller.shutdown()


def test_workflow_controller_preserves_unknown_start_across_bridge_transition(
    managed_controller: ControllerFactory,
    tmp_path: Path,
) -> None:
    window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload())
    controller.dialog.start_button.click()
    start_dialog = controller._active_start_dialog
    assert start_dialog is not None
    yaml_file = tmp_path / "workflow.yml"
    yaml_file.write_bytes(b"name: fixed\n")
    start_dialog.yaml_path_edit.setText(str(yaml_file))
    start_dialog.workspace_path_edit.setText(str(tmp_path))
    start_dialog.job_count_combo.setCurrentIndex(start_dialog.job_count_combo.findData(2))
    start_dialog.submit_button.click()
    post_call = client.calls[-1]
    assert post_call[0] == "start"
    first_payload = post_call[3]
    assert isinstance(first_payload, dict)
    assert first_payload["max_live_codex_jobs"] == 2

    window._ready = False
    controller.notify_readiness_changed()
    assert post_call[1] in client.aborted
    assert start_dialog.has_unknown_outcome
    assert start_dialog.retry_button.isHidden()
    assert sum(call[0] == "start" for call in client.calls) == 1

    window._ready = True
    window._control_token = "token-rotated"
    controller.notify_readiness_changed()
    assert not start_dialog.retry_button.isHidden()
    assert sum(call[0] == "start" for call in client.calls) == 1
    updated_list = client.calls[-1]
    assert updated_list[0] == "list"
    assert updated_list[2] == "token-rotated"
    client.succeed(updated_list, 200, _list_payload())
    start_dialog.retry_button.click()
    retry_call = client.calls[-1]
    assert retry_call[0] == "start"
    assert retry_call[2] == "token-rotated"
    assert retry_call[3] == first_payload
    assert retry_call[3]["client_request_id"] == post_call[3]["client_request_id"]
    assert sum(call[0] == "start" for call in client.calls) == 2
    controller.shutdown()


def test_workflow_controller_can_close_unknown_start_without_posting_again(
    managed_controller: ControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _window, client, controller = managed_controller()
    client.succeed(client.calls[-1], 200, _list_payload())
    controller.dialog.start_button.click()
    start_dialog = controller._active_start_dialog
    assert start_dialog is not None
    yaml_file = tmp_path / "workflow.yaml"
    yaml_file.write_text("name: fixed\n", encoding="utf-8")
    start_dialog.yaml_path_edit.setText(str(yaml_file))
    start_dialog.workspace_path_edit.setText(str(tmp_path))
    start_dialog.submit_button.click()
    post_call = client.calls[-1]
    client.fail(post_call, 0, "timeout")

    assert start_dialog.has_unknown_outcome
    assert start_dialog.cancel_button.isEnabled()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    start_dialog.cancel_button.click()

    assert controller._active_start_dialog is None
    assert sum(call[0] == "start" for call in client.calls) == 1
    controller.refresh()
    assert client.calls[-1][0] == "list"
    assert sum(call[0] == "start" for call in client.calls) == 1
    controller.shutdown()
