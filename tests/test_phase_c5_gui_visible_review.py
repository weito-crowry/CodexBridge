from __future__ import annotations

import gc
import socket
import subprocess
import weakref
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.i18n import tr
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog, workflow_status_label
from scripts import phase_c5_gui_visible_review as review


def _app() -> QApplication:
    app = QApplication.instance()
    return app if app is not None else QApplication([])


def _deny_external(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("external process or network operation was attempted")


def _assert_scenario_rendered(dialog: WorkflowManagementDialog, index: int) -> None:
    scenario = review.build_scenarios()[index]
    fixture = review.build_fixture(scenario)
    assert dialog.selected_workflow_id == scenario.workflow_id
    assert dialog.detail_labels["workflow_id"].text() == scenario.workflow_id
    assert dialog.detail_labels["status"].text() != "Not available"
    assert scenario.label in dialog.final_text_view.toPlainText()
    assert dialog.graph_widget.snapshot_identity == (
        scenario.workflow_id,
        scenario.run_id,
        fixture["snapshot"]["snapshot_version"],
    )
    assert dialog.graph_widget.event_list.count() == len(fixture["events"]["events"])
    assert scenario.label in dialog.status_label.text()
    assert not dialog.start_button.isEnabled()


def test_show_keeps_harness_alive_during_selection_events_after_gc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    assert app.platformName().lower() == "offscreen"
    monkeypatch.setattr(socket, "socket", _deny_external)
    monkeypatch.setattr(socket, "create_connection", _deny_external)
    monkeypatch.setattr(subprocess, "Popen", _deny_external)
    monkeypatch.setattr(review.sys, "platform", "win32")
    monkeypatch.setattr(review, "os", SimpleNamespace(environ={"QT_QPA_PLATFORM": ""}))

    dialogs: list[WorkflowManagementDialog] = []
    harness_refs: list[weakref.ReferenceType[review.VisibleReviewHarness]] = []
    harness_factory = review.VisibleReviewHarness

    class RecordingDialog(WorkflowManagementDialog):
        def __init__(self) -> None:
            super().__init__()
            dialogs.append(self)

    def create_harness(dialog: WorkflowManagementDialog, size: str) -> review.VisibleReviewHarness:
        instance = harness_factory(dialog, size)
        harness_refs.append(weakref.ref(instance))
        return instance

    class FakeApp:
        @staticmethod
        def instance() -> None:
            return None

        def __init__(self, _argv: list[str]) -> None:
            pass

        def setApplicationName(self, _name: str) -> None:
            pass

        def setQuitOnLastWindowClosed(self, _enabled: bool) -> None:
            pass

        def exec(self) -> int:
            gc.collect()
            assert harness_refs and harness_refs[0]() is not None
            dialog = dialogs[-1]
            _assert_scenario_rendered(dialog, 0)

            # Selecting the already-current item must not clear the initial view.
            dialog.workflow_list.setCurrentRow(0)
            QApplication.instance().processEvents()
            _assert_scenario_rendered(dialog, 0)

            # Exercise the actual Qt selection signal after collecting garbage.
            for index in (1, 2, 3, 0):
                dialog.workflow_list.setCurrentRow(index)
                QApplication.instance().processEvents()
                _assert_scenario_rendered(dialog, index)

            dialog.result_tabs.setCurrentIndex(1)
            QApplication.instance().processEvents()
            dialog.graph_widget.node_list.setCurrentRow(1)
            QApplication.instance().processEvents()
            assert dialog.graph_widget._selected_node_id == "/do/0/for-many"
            dialog.result_tabs.setCurrentIndex(0)
            QApplication.instance().processEvents()
            return 0

    monkeypatch.setattr(review, "VisibleReviewHarness", create_harness)
    monkeypatch.setattr(review, "_load_ui_types", lambda: (FakeApp, RecordingDialog))
    assert review._show("regular") == 0
    assert len(dialogs) == 1
    _assert_scenario_rendered(dialogs[0], 0)
    dialogs[0].close()
    app.processEvents()


def test_same_selection_stays_rendered_and_returning_updates_graph_identity() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    harness = review.VisibleReviewHarness(dialog)
    _assert_scenario_rendered(dialog, 0)

    dialog.workflow_list.setCurrentRow(0)
    app.processEvents()
    _assert_scenario_rendered(dialog, 0)

    dialog.workflow_list.setCurrentRow(2)
    app.processEvents()
    _assert_scenario_rendered(dialog, 2)
    dialog.workflow_list.setCurrentRow(0)
    app.processEvents()
    _assert_scenario_rendered(dialog, 0)
    dialog.close()
    app.processEvents()
    assert harness.dialog is dialog


def test_fixed_fixtures_render_in_real_graph_widget() -> None:
    app = _app()
    scenarios = review.build_scenarios()
    assert len(scenarios) == 7
    assert [scenario.key for scenario in scenarios] == [
        "a",
        "b",
        "c",
        "unknown",
        "reconciliation",
        "failed",
        "legacy",
    ]

    dialog = WorkflowManagementDialog()
    harness = review.VisibleReviewHarness(dialog)
    assert dialog.workflow_list.count() == 7
    assert not dialog.start_button.isEnabled()
    assert dialog.graph_widget.set_snapshot(
        scenarios[0].workflow_id, review.build_fixture(scenarios[0])["snapshot"]
    )

    seen_operations: set[str] = set()
    seen_states: set[str] = set()
    execution_counts: set[int] = set()
    coverage: set[str] = set()
    for index, scenario in enumerate(scenarios):
        dialog.workflow_list.setCurrentRow(index)
        app.processEvents()
        fixture = review.build_fixture(scenario)
        assert dialog.selected_workflow_id == scenario.workflow_id
        assert dialog.graph_widget.snapshot_identity is not None
        assert dialog.graph_widget.snapshot_identity[:2] == (
            scenario.workflow_id,
            scenario.run_id,
        )
        assert dialog.graph_widget.set_snapshot(scenario.workflow_id, fixture["snapshot"])
        assert dialog.graph_widget.set_events(fixture["events"])
        assert dialog.graph_widget._snapshot is not None
        coverage.add(str(dialog.graph_widget._snapshot["coverage"]))
        for node in fixture["snapshot"]["nodes"]:
            seen_operations.add(str(node["operation"]))
            seen_states.add(str(node["state"]))
            executions = node["executions"]
            execution_counts.add(len(executions))
            seen_states.update(str(item["state"]) for item in executions)
        assert dialog.graph_widget.node_list.count() == 5
        assert dialog.graph_widget.event_list.count() == len(fixture["events"]["events"])
        assert "合成データ" in dialog.status_label.text()
        detail = fixture["detail"]
        assert isinstance(detail, dict)
        terminal = detail["terminal"]
        expected_terminal = scenario.status in {"succeeded", "failed", "failed_to_start"}
        assert type(terminal) is bool
        assert terminal is expected_terminal
        QTest.mouseClick(dialog.refresh_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert dialog.detail_labels["status"].text() == workflow_status_label(scenario.status)
        assert dialog.detail_labels["terminal"].text() == tr("Yes" if expected_terminal else "No")
        assert not dialog.start_button.isEnabled()

    assert {"for", "switch", "do", "call"} <= seen_operations
    assert {"unknown", "running", "failed", "needs_reconciliation", "succeeded"} <= seen_states
    assert {0, 1, 2, 3} <= execution_counts
    assert {"complete", "partial", "unavailable"} <= coverage
    assert harness.dialog is dialog
    dialog.close()
    app.processEvents()


def test_regular_compact_tabs_node_details_and_start_are_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_external)
    monkeypatch.setattr(subprocess, "Popen", _deny_external)
    app = _app()
    dialog = WorkflowManagementDialog()
    review.VisibleReviewHarness(dialog, "regular")
    starts: list[bool] = []
    dialog.start_requested.connect(lambda: starts.append(True))
    dialog.show()
    app.processEvents()

    for width, height in ((900, 760), (720, 600)):
        dialog.resize(width, height)
        app.processEvents()
        assert dialog.size().width() == width
        assert dialog.size().height() == height
        for widget in (
            dialog.workflow_list,
            dialog.detail_labels["status"],
            dialog.result_tabs,
            dialog.graph_widget,
            dialog.graph_widget.graph_view,
            dialog.graph_widget.node_list,
            dialog.graph_widget.detail_view,
            dialog.graph_widget.event_list,
        ):
            assert widget.geometry().width() > 0
            assert widget.geometry().height() > 0
        assert not dialog.start_button.isEnabled()
        QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
        assert starts == []

    dialog.result_tabs.setCurrentIndex(1)
    app.processEvents()
    assert dialog.graph_tab_active
    dialog.graph_widget.node_list.setCurrentRow(1)
    app.processEvents()
    assert dialog.graph_widget._selected_node_id == "/do/0/for-many"
    assert "for-many" in dialog.graph_widget.detail_view.toPlainText()
    dialog.result_tabs.setCurrentIndex(0)
    assert not dialog.graph_tab_active
    QTest.mouseClick(dialog.result_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert "実Workflowは実行していません" in dialog.final_text_view.toPlainText()
    assert starts == []
    dialog.close()
    app.processEvents()


def test_refresh_preserves_item_node_identity_and_graph_transform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_external)
    monkeypatch.setattr(subprocess, "Popen", _deny_external)
    app = _app()
    dialog = WorkflowManagementDialog()
    harness = review.VisibleReviewHarness(dialog)
    dialog.show()
    app.processEvents()
    graph = dialog.graph_widget
    node_id = "/do/0/for-many"
    workflow_item = dialog.workflow_list.item(0)
    node_item = graph._node_items[node_id]
    graph.node_list.setCurrentRow(1)
    graph.graph_view.scale(1.35, 1.35)
    app.processEvents()
    transform = graph.graph_view.transform()
    horizontal = graph.graph_view.horizontalScrollBar().value()
    vertical = graph.graph_view.verticalScrollBar().value()

    QTest.mouseClick(dialog.refresh_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert dialog.workflow_list.item(0) is workflow_item
    assert graph._node_items[node_id] is node_item
    assert graph.graph_view.transform() == transform
    assert graph.graph_view.horizontalScrollBar().value() == horizontal
    assert graph.graph_view.verticalScrollBar().value() == vertical
    assert graph._selected_node_id == node_id
    assert not dialog.start_button.isEnabled()
    dialog.close()
    app.processEvents()
    assert harness.dialog is dialog


def test_cli_help_and_prepare_do_not_create_window_or_external_activity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(socket, "socket", _deny_external)
    monkeypatch.setattr(subprocess, "Popen", _deny_external)
    app_before = QApplication.instance()
    assert review.main([]) == 0
    assert "--prepare" in capsys.readouterr().out
    assert QApplication.instance() is app_before

    monkeypatch.setattr(review.sys, "platform", "win32")
    assert review.main(["--prepare"]) == 0
    output = capsys.readouterr().out
    assert "PASS: Windows native" in output
    assert "PASS: seven fixed offline fixtures" in output
    assert "PASS: no QApplication or window created" in output
    assert "PASS: no controller, API client, or Bridge imported" in output
    assert QApplication.instance() is app_before


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_show_rejects_non_windows_without_creating_application(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    app_before = QApplication.instance()
    monkeypatch.setattr(review.sys, "platform", platform)
    assert review.main(["--show", "--size=regular"]) == 2
    assert "requires a Windows native session" in capsys.readouterr().out
    assert QApplication.instance() is app_before


def test_show_rejects_offscreen_without_overriding_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app_before = QApplication.instance()
    monkeypatch.setattr(review.sys, "platform", "win32")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert review.main(["--show", "--size=compact"]) == 2
    assert "QT_QPA_PLATFORM=offscreen" in capsys.readouterr().out
    assert QApplication.instance() is app_before


def test_prepare_rejects_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.sys, "platform", "linux")
    passed, messages = review.prepare()
    assert not passed
    assert any("BLOCKED: Windows native" in message for message in messages)


def test_refresh_and_selection_use_only_the_seven_fixed_sample_ids() -> None:
    app = _app()
    dialog = WorkflowManagementDialog()
    harness = review.VisibleReviewHarness(dialog)
    ids = [
        dialog.workflow_list.item(index).data(Qt.ItemDataRole.UserRole)
        for index in range(dialog.workflow_list.count())
    ]
    assert all(isinstance(workflow_id, str) for workflow_id in ids)
    assert all(workflow_id.startswith("sample-c5-") for workflow_id in ids)
    assert all("/" not in workflow_id and "\\" not in workflow_id for workflow_id in ids)
    harness.refresh()
    app.processEvents()
    assert dialog.workflow_list.count() == 7
    assert not dialog.start_button.isEnabled()
    dialog.close()
    app.processEvents()
