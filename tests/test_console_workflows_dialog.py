from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from codex_bridge.console.i18n import initialize_language
from codex_bridge.console.workflows_dialog import (
    StartWorkflowDialog,
    WorkflowManagementDialog,
    workflow_error_message,
    workflow_status_label,
)


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def test_workflow_management_dialog_lists_short_ids_and_emits_selected_full_id() -> None:
    _app()
    dialog = WorkflowManagementDialog()
    selected: list[str] = []
    dialog.workflow_selected.connect(selected.append)
    first_id = "workflow-1234567890-abcdefghijkl"
    second_id = "workflow-second-1234567890"

    dialog.set_workflows(
        [
            {
                "workflow_id": first_id,
                "status": "needs_reconciliation",
                "created_at": "2026-11-11T01:00:00Z",
                "updated_at": "2026-11-11T01:02:00Z",
            },
            {
                "workflow_id": second_id,
                "status": "running",
                "created_at": "2026-11-10T01:00:00Z",
                "updated_at": "2026-11-10T01:02:00Z",
            },
        ]
    )

    assert dialog.workflow_list.count() == 2
    assert first_id not in dialog.workflow_list.item(0).text()
    assert any(
        text in dialog.workflow_list.item(0).text().lower()
        for text in ("needs reconciliation", "照合が必要")
    )
    assert dialog.selected_workflow_id == first_id
    assert selected == [first_id]
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == first_id
    dialog.workflow_list.setCurrentRow(1)
    assert dialog.selected_workflow_id == second_id
    assert selected[-1] == second_id


def test_workflow_management_dialog_displays_details_and_redacts_result_secrets() -> None:
    _app()
    dialog = WorkflowManagementDialog()
    workflow_id = "workflow-1234567890-abcdefghijkl"
    dialog.set_workflows(
        [
            {
                "workflow_id": workflow_id,
                "status": "succeeded",
                "created_at": "now",
                "updated_at": "now",
            }
        ]
    )
    detail = {
        "workflow_id": workflow_id,
        "status": "succeeded",
        "created_at": "created",
        "updated_at": "updated",
        "worker_state": "succeeded",
        "terminal": True,
        "job_id": "job-1",
        "thread_id": "thread-1",
    }

    dialog.set_detail(detail)
    dialog.set_result(
        {
            "result": {
                "result": {
                    "result": {
                        "final_text": (
                            "done C:\\private\\notes.txt with Bearer abcdef "
                            "Authorization: Bearer xyz /home/private/file"
                        )
                    }
                }
            },
            "workspace_root": "C:\\private\\workspace",
            "authorization": "Bearer abcdef",
            "value": "normal result",
        }
    )

    assert dialog.detail_labels["workflow_id"].text() == workflow_id
    assert dialog.detail_labels["job_id"].text() == "job-1"
    assert dialog.detail_labels["thread_id"].text() == "thread-1"
    assert dialog.detail_labels["terminal"].text() in {"Yes", "はい"}
    assert dialog.result_button.isEnabled()
    assert "done" in dialog.final_text_view.toPlainText()
    assert "C:\\private" not in dialog.final_text_view.toPlainText()
    assert "abcdef" not in dialog.final_text_view.toPlainText()
    assert "C:\\private" not in dialog.result_json.toPlainText()
    assert "/home/private" not in dialog.result_json.toPlainText()
    assert "Authorization: Bearer" not in dialog.result_json.toPlainText()
    assert "abcdef" not in dialog.result_json.toPlainText()
    assert "normal result" in dialog.result_json.toPlainText()


@pytest.mark.parametrize(
    ("path", "fragments"),
    [
        (r"C:\Users\Jane Doe\private.yaml", ("Jane", "Doe", "private.yaml")),
        ("/home/田中 太郎/仕事/秘密.yaml", ("田中", "太郎", "仕事", "秘密.yaml")),
        (
            r"\\server.example\Shared Files\secret.yaml",
            ("server.example", "Shared Files", "secret.yaml"),
        ),
        ("file:///C:/Users/Jane%20Doe/secret.yaml", ("Jane%20Doe", "secret.yaml")),
    ],
)
def test_result_redaction_hides_path_fragments_with_spaces_and_unicode(
    path: str, fragments: tuple[str, ...]
) -> None:
    _app()
    dialog = WorkflowManagementDialog()
    dialog.set_result({"final_text": f"Generated from {path} successfully"})

    displayed = dialog.result_json.toPlainText() + dialog.final_text_view.toPlainText()

    assert "Generated from" in displayed
    assert all(fragment not in displayed for fragment in fragments)
    dialog.close()


def test_workflow_management_dialog_caps_list_at_fifty_rows() -> None:
    _app()
    dialog = WorkflowManagementDialog()
    rows = [
        {
            "workflow_id": f"workflow-{index}",
            "status": "queued",
            "created_at": "now",
            "updated_at": "now",
        }
        for index in range(55)
    ]

    dialog.set_workflows(rows)

    assert dialog.workflow_list.count() == 50


def test_start_workflow_dialog_validates_yaml_and_workspace_and_reuses_request_on_retry(
    tmp_path: Path,
) -> None:
    _app()
    yaml_file = tmp_path / "approved.yaml"
    yaml_file.write_bytes(b"name: fixed\n")
    dialog = StartWorkflowDialog((str(tmp_path),))
    sent: list[tuple[str, dict[str, object]]] = []
    dialog.submitted.connect(lambda request_id, payload: sent.append((request_id, payload)))

    dialog.yaml_path_edit.setText(str(yaml_file))
    dialog.workspace_path_edit.setText(str(tmp_path))

    assert dialog.submit_button.isEnabled()
    assert "approved.yaml" in dialog.preview_label.text()
    assert str(tmp_path.resolve()) in dialog.preview_label.text()
    assert "name: fixed" not in dialog.preview_label.text()
    dialog.submit_button.click()
    assert len(sent) == 1
    request_id, payload = sent[0]
    assert len(request_id) == 36
    assert payload == {
        "client_request_id": request_id,
        "workflow_yaml": "name: fixed\n",
        "input": {},
        "workspace_root": str(tmp_path.resolve()),
    }
    assert not dialog.yaml_path_edit.isEnabled()

    dialog.show_request_failure("timeout", outcome_unknown=True, retry_allowed=True)
    assert any(text in dialog.error_label.text().lower() for text in ("unknown", "確認できません"))
    assert not dialog.retry_button.isHidden()
    assert dialog.cancel_button.isEnabled()
    dialog.retry_button.click()

    assert sent == [(request_id, payload), (request_id, payload)]


@pytest.mark.parametrize("close_via", ["button", "window"])
@pytest.mark.parametrize(
    ("answer", "expected_visible"),
    [
        (QMessageBox.StandardButton.Yes, False),
        (QMessageBox.StandardButton.No, True),
    ],
)
@pytest.mark.parametrize(
    ("language", "expected_title", "expected_notice"),
    [
        (
            "en",
            "Unresolved workflow request",
            "Closing this dialog does not cancel the request sent to the server. "
            "Any job it started may continue. Check the workflow list before starting "
            "another run.",
        ),
        (
            "ja-JP",
            "未解決のワークフロー要求",
            "このダイアログを閉じても、サーバーへ送信済みの要求は取り消されません。"
            "開始済みのジョブが続行することがあります。新しい実行を始める前に"
            "ワークフロー一覧を確認してください。",
        ),
    ],
)
def test_start_workflow_dialog_confirms_close_when_result_is_unknown(
    close_via: str,
    answer: QMessageBox.StandardButton,
    expected_visible: bool,
    language: str,
    expected_title: str,
    expected_notice: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _app()
    settings = QSettings(str(tmp_path / f"{language}.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", language)
    initialize_language(settings)
    yaml_file = tmp_path / "pending.yaml"
    yaml_file.write_text("name: pending\n", encoding="utf-8")
    dialog = StartWorkflowDialog()
    sent: list[tuple[str, dict[str, object]]] = []
    dialog.submitted.connect(lambda request_id, payload: sent.append((request_id, payload)))
    dialog.yaml_path_edit.setText(str(yaml_file))
    dialog.workspace_path_edit.setText(str(tmp_path))
    dialog.show()
    dialog.submit_button.click()
    request_id, payload = sent[0]
    dialog.show_request_failure("timeout", outcome_unknown=True, retry_allowed=True)

    assert dialog.cancel_button.isEnabled()
    assert dialog.yaml_path_edit.text() == str(yaml_file)
    assert dialog.workspace_path_edit.text() == str(tmp_path)
    assert dialog._request_id == request_id
    assert dialog._payload == payload

    questions: list[tuple[object, ...]] = []

    def answer_question(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        questions.append(args)
        return answer

    monkeypatch.setattr(QMessageBox, "question", answer_question)
    if close_via == "button":
        dialog.cancel_button.click()
    else:
        dialog.close()

    assert len(questions) == 1
    assert questions[0][1] == expected_title
    assert questions[0][2] == expected_notice
    assert questions[0][3] == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    assert questions[0][4] == QMessageBox.StandardButton.No
    assert dialog.isVisible() is expected_visible
    assert len(sent) == 1
    if expected_visible:
        assert dialog.yaml_path_edit.text() == str(yaml_file)
        assert dialog.workspace_path_edit.text() == str(tmp_path)
        assert dialog._request_id == request_id
        assert dialog._payload == payload


def test_start_workflow_dialog_rejects_oversized_invalid_utf8_and_bad_paths(tmp_path: Path) -> None:
    _app()
    dialog = StartWorkflowDialog()
    good_file = tmp_path / "valid.yml"
    good_file.write_text("name: valid\n", encoding="utf-8")
    oversized_file = tmp_path / "large.yaml"
    oversized_file.write_bytes(b"x" * (64 * 1024 + 1))
    invalid_utf8_file = tmp_path / "invalid.yaml"
    invalid_utf8_file.write_bytes(b"\xff")

    dialog.yaml_path_edit.setText(str(oversized_file))
    dialog.workspace_path_edit.setText(str(tmp_path))
    assert not dialog.submit_button.isEnabled()
    assert "64 KiB" in dialog.error_label.text()

    dialog.yaml_path_edit.setText(str(invalid_utf8_file))
    assert not dialog.submit_button.isEnabled()
    assert "UTF-8" in dialog.error_label.text()

    dialog.yaml_path_edit.setText(str(good_file))
    dialog.workspace_path_edit.setText(str(tmp_path / "missing"))
    assert not dialog.submit_button.isEnabled()
    assert any(
        text in dialog.preview_label.text().lower()
        for text in ("existing workspace", "作業フォルダー")
    )


def test_workflow_status_and_error_messages_are_known_and_translatable() -> None:
    _app()

    assert any(
        text in workflow_status_label("needs_reconciliation").lower()
        for text in ("needs reconciliation", "照合が必要")
    )
    assert any(text in workflow_status_label("unexpected").lower() for text in ("unknown", "不明"))
    assert any(
        text in workflow_error_message("forbidden").lower()
        for text in ("not authorized", "認証されません")
    )
    assert any(
        text in workflow_error_message("untrusted raw body").lower()
        for text in ("request failed", "要求に失敗")
    )


@pytest.mark.parametrize(
    ("language", "expected_title", "expected_reconciliation"),
    [
        ("en", "Workflow management", "Workflow status: needs reconciliation"),
        ("ja-JP", "ワークフロー管理", "ワークフロー状態: 照合が必要"),
    ],
)
def test_workflow_dialog_translations(
    language: str, expected_title: str, expected_reconciliation: str, tmp_path: Path
) -> None:
    _app()
    settings = QSettings(str(tmp_path / f"{language}.ini"), QSettings.Format.IniFormat)
    settings.setValue("language", language)
    initialize_language(settings)
    dialog = WorkflowManagementDialog()

    assert dialog.windowTitle() == expected_title
    assert workflow_status_label("needs_reconciliation") == expected_reconciliation
    assert dialog.refresh_button.text() in {"Update", "更新"}
