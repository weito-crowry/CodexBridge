from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
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


def test_workflow_controller_loads_list_detail_and_result_in_order() -> None:
    _window, client, controller = _controller()
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


def test_workflow_controller_ignores_old_detail_after_selection_changes() -> None:
    _window, client, controller = _controller()
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


def test_workflow_controller_clears_jobs_when_selection_changes() -> None:
    _window, client, controller = _controller()
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


def test_workflow_controller_keeps_communication_failures_separate_from_run_state() -> None:
    _window, client, controller = _controller()
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


def test_workflow_controller_aborts_requests_on_readiness_change_and_close() -> None:
    window, client, controller = _controller()
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
    tmp_path: Path,
) -> None:
    _window, client, controller = _controller()
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


def test_workflow_controller_does_not_auto_retry_authentication_failure() -> None:
    _window, client, controller = _controller()
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
    tmp_path: Path,
) -> None:
    window, client, controller = _controller()
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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _window, client, controller = _controller()
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
