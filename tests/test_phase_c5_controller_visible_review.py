from __future__ import annotations

import ast
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication

from codex_bridge.console.i18n import initialize_language, language, tr
from codex_bridge.console.workflow_graph_widget import (
    WorkflowExecutionGraphWidget,
    _validate_snapshot,
)
from codex_bridge.console.workflows_dialog import WorkflowManagementDialog
from scripts import phase_c5_controller_visible_review as review


@contextmanager
def _japanese_process_language() -> Iterator[None]:
    previous_language = language()
    previous_locale = QLocale()
    initialize_language(review._ProcessOnlySettings("ja-JP"), system_locale="ja_JP")
    try:
        yield
    finally:
        initialize_language(
            review._ProcessOnlySettings(previous_language),
            system_locale="en_US",
        )
        QLocale.setDefault(previous_locale)


def _application(monkeypatch: pytest.MonkeyPatch) -> QApplication:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    application = QApplication.instance()
    return application if application is not None else QApplication([])


def _wait_for_timer_ticks(
    application: QApplication,
    spies: list[list[None]],
    target: int,
    *,
    timeout_seconds: float = 4.5,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while any(len(spy) < target for spy in spies) and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    application.processEvents()
    assert all(len(spy) >= target for spy in spies)


def _open_session(size: str) -> review.ControllerVisibleReviewSession:
    session = review.ControllerVisibleReviewSession(size)
    session.show()
    QApplication.processEvents()
    return session


def _prepare_display_state(
    application: QApplication,
    session: review.ControllerVisibleReviewSession,
) -> dict[str, object]:
    dialog = session.dialog
    dialog.workflow_list.setCurrentRow(44)
    dialog.workflow_list.scrollToItem(dialog.workflow_list.item(49))
    dialog.result_tabs.setCurrentIndex(1)
    application.processEvents()

    graph = dialog.graph_widget
    assert len(graph.node_items) == 5
    graph.node_list.setCurrentRow(2)
    graph.graph_view.scale(1.4, 1.4)
    application.processEvents()
    graph_horizontal = graph.graph_view.horizontalScrollBar()
    graph_vertical = graph.graph_view.verticalScrollBar()
    graph_horizontal.setValue(graph_horizontal.maximum() // 2)
    graph_vertical.setValue(graph_vertical.maximum() // 2)

    selected_item = dialog.workflow_list.currentItem()
    assert selected_item is not None
    graph_node_id = graph.selected_node_id
    assert graph_node_id is not None
    assert dialog.workflow_list.count() == 50
    assert dialog.result_button.isEnabled()
    assert dialog.final_text_view.toPlainText().startswith("合成結果:")
    assert dialog.start_button.isEnabled() is False

    return {
        "workflow_id": dialog.selected_workflow_id,
        "selected_workflow_item": id(selected_item),
        "workflow_items": tuple(id(dialog.workflow_list.item(i)) for i in range(50)),
        "workflow_scroll": dialog.workflow_list.verticalScrollBar().value(),
        "detail": tuple(label.text() for label in dialog.detail_labels.values()),
        "result_json": dialog.result_json.toPlainText(),
        "result_text": dialog.final_text_view.toPlainText(),
        "graph_items": {node_id: id(item) for node_id, item in graph.node_items.items()},
        "graph_node_id": graph_node_id,
        "graph_transform": (
            graph.graph_view.transform().m11(),
            graph.graph_view.transform().m22(),
        ),
        "graph_horizontal": graph_horizontal.value(),
        "graph_vertical": graph_vertical.value(),
        "graph_event_count": graph.event_list.count(),
        "graph_identity": graph.snapshot_identity,
    }


def _assert_display_state(
    application: QApplication,
    session: review.ControllerVisibleReviewSession,
    expected: dict[str, object],
) -> None:
    application.processEvents()
    dialog = session.dialog
    graph = dialog.graph_widget
    current_item = dialog.workflow_list.currentItem()
    assert current_item is not None
    assert dialog.selected_workflow_id == expected["workflow_id"]
    assert id(current_item) == expected["selected_workflow_item"]
    assert tuple(id(dialog.workflow_list.item(i)) for i in range(50)) == expected["workflow_items"]
    assert dialog.workflow_list.verticalScrollBar().value() == expected["workflow_scroll"]
    assert tuple(label.text() for label in dialog.detail_labels.values()) == expected["detail"]
    assert dialog.result_json.toPlainText() == expected["result_json"]
    assert dialog.final_text_view.toPlainText() == expected["result_text"]
    assert {node_id: id(item) for node_id, item in graph.node_items.items()} == expected[
        "graph_items"
    ]
    assert graph.selected_node_id == expected["graph_node_id"]
    assert (
        graph.graph_view.transform().m11(),
        graph.graph_view.transform().m22(),
    ) == expected["graph_transform"]
    assert graph.graph_view.horizontalScrollBar().value() == expected["graph_horizontal"]
    assert graph.graph_view.verticalScrollBar().value() == expected["graph_vertical"]
    assert graph.snapshot_identity is not None


def test_real_controller_timer_preserves_synthetic_display_at_both_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(monkeypatch)
    sessions: list[review.ControllerVisibleReviewSession] = []
    previous_language = language()
    previous_locale = QLocale()
    try:
        with _japanese_process_language():
            for size, expected_size in (("regular", (900, 760)), ("compact", (720, 600))):
                session = _open_session(size)
                sessions.append(session)
                assert isinstance(session.dialog, WorkflowManagementDialog)
                assert isinstance(session.dialog.graph_widget, WorkflowExecutionGraphWidget)
                assert (session.dialog.width(), session.dialog.height()) == expected_size
                assert session.dialog.windowTitle() == review.TITLE
                assert session.controller.timer.interval() == 3_000
                assert session.controller.timer.isActive()
                assert session.client.list_request_count == 2
                assert session.dialog.workflow_list.count() == 50
                assert session.dialog.refresh_button.text() == "更新"
                assert session.dialog.start_button.text() == "新規実行"
                assert session.dialog.result_tabs.tabText(1) == "実行グラフ"
                assert tr("Workflow list") == "ワークフロー一覧"
                assert session.dialog.bridge_state_label.text() == "合成クライアント: インメモリ"
                assert (
                    _validate_snapshot(
                        "synthetic-workflow-045",
                        review._graph_snapshot(
                            "synthetic-workflow-045", "synthetic-run-045", changed=False
                        ),
                    )
                    is not None
                )

            initial = [_prepare_display_state(application, session) for session in sessions]
            assert all(session.dialog.graph_widget.event_cursor == 1 for session in sessions)
            assert all(session.dialog.graph_widget.event_list.count() == 1 for session in sessions)
            assert all(
                session.dialog.graph_widget.graph_view.verticalScrollBar().maximum() > 0
                for session in sessions
            )
            initial_versions = [
                cast_identity[2] for cast_identity in (item["graph_identity"] for item in initial)
            ]

            tick_spies: list[list[None]] = [[] for _ in sessions]
            for session, spy in zip(sessions, tick_spies, strict=True):
                session.controller.timer.timeout.connect(lambda _spy=spy: _spy.append(None))

            for tick_number in range(1, 5):
                _wait_for_timer_ticks(application, tick_spies, tick_number)
                for index, session in enumerate(sessions):
                    graph = session.dialog.graph_widget
                    _assert_display_state(application, session, initial[index])
                    if tick_number < 3:
                        assert graph.event_cursor == 1
                        assert graph.event_list.count() == 1
                        assert graph.snapshot_identity == initial[index]["graph_identity"]
                    elif tick_number == 3:
                        assert graph.event_cursor == 2
                        assert graph.event_list.count() == 2
                        assert graph.snapshot_identity is not None
                        assert graph.snapshot_identity[2] != initial_versions[index]
                    else:
                        assert graph.event_cursor == 2
                        assert graph.event_list.count() == 2
                        assert graph.snapshot_identity is not None
                        assert graph.snapshot_identity[2] != initial_versions[index]
                    assert session.client.read_count("list") == tick_number + 2
                    assert session.client.post_attempts == 0

            for session in sessions:
                session.dialog.start_button.click()
                session.dialog.start_requested.emit()
                session.controller._update_buttons()
                assert not session.dialog.start_button.isEnabled()
                assert session.controller.denied_start_signals == 1
                assert session.controller._active_start_dialog is None
                assert session.client.post_attempts == 0
                assert all(read.key.startswith("workflows:") for read in session.client.reads)

                previous_list_count = session.client.read_count("list")
                session.dialog.refresh_button.click()
                assert session.client.read_count("list") == previous_list_count + 1
                assert session.client.post_attempts == 0
                assert session.dialog.result_button.isEnabled()
        assert language() == previous_language
        assert QLocale() == previous_locale
    finally:
        for session in reversed(sessions):
            session.close()
            assert not session.controller.timer.isActive()
            assert session.controller._signal_connections == []
            assert session.controller._shutdown
        application.processEvents()


def test_stale_run_and_selection_responses_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(monkeypatch)
    session: review.ControllerVisibleReviewSession | None = None
    try:
        with _japanese_process_language():
            session = _open_session("regular")
            dialog = session.dialog
            client = session.client
            controller = session.controller
            workflow_one = "synthetic-workflow-001"
            workflow_two = "synthetic-workflow-002"
            dialog.result_tabs.setCurrentIndex(1)
            assert dialog.graph_widget.event_cursor == 1

            old_run_id = review._run_id(workflow_one)
            new_run_id = "synthetic-run-new-001"
            client.run_ids[workflow_one] = new_run_id
            client.deferred_kinds.add("task_events")
            controller._request_graph_tasks()
            assert dialog.graph_widget.snapshot_identity == (
                workflow_one,
                new_run_id,
                "sha256:" + "a" * 64,
            )
            assert dialog.graph_widget.event_cursor == 0
            run_event_key = client.pending_keys("task_events")[-1]
            stale_run_events = review._workflow_events(
                workflow_one, old_run_id, after_id=0, changed=False
            )
            client.emit_success(run_event_key, stale_run_events)
            assert dialog.graph_widget.event_cursor == 0
            assert dialog.graph_widget.event_list.count() == 0

            client.deferred_kinds.remove("task_events")
            controller._request_graph_tasks()
            assert dialog.graph_widget.event_cursor == 1

            client.deferred_kinds.add("tasks")
            controller._request_graph_tasks()
            stale_selection_key = client.pending_keys("tasks")[-1]
            dialog.workflow_list.setCurrentRow(1)
            assert dialog.selected_workflow_id == workflow_two
            assert dialog.graph_widget.snapshot_identity is None
            current_selection_key = client.pending_keys("tasks")[-1]
            assert current_selection_key != stale_selection_key

            client.succeed_pending(stale_selection_key)
            assert dialog.graph_widget.snapshot_identity is None
            client.succeed_pending(current_selection_key)
            assert dialog.graph_widget.snapshot_identity is not None
            assert dialog.graph_widget.snapshot_identity[0] == workflow_two
            assert dialog.graph_widget.event_cursor == 1
            assert stale_selection_key in client.aborted_keys
            assert client.post_attempts == 0
    finally:
        if session is not None:
            session.close()
            assert not session.controller.timer.isActive()
        application.processEvents()


def test_prepare_help_and_show_refusal_are_nonvisual_and_local(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    application = _application(monkeypatch)
    before_windows = len(application.topLevelWidgets())

    assert review.main([]) == 0
    assert "usage:" in capsys.readouterr().out

    assert review.main(["--prepare"]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "PASS"
    assert prepared["synthetic_workflows"] == 50
    assert prepared["synthetic_schema_valid"] is True
    assert prepared["windows_created"] == 0
    assert prepared["real_data_accesses"] == 0
    assert prepared["external_communications"] == 0
    assert prepared["qapplication_created"] is False
    assert len(application.topLevelWidgets()) == before_windows

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    assert review.main(["--show", "--size=regular"]) == 2
    assert "BLOCKED" in capsys.readouterr().out
    assert review.main(["--show", "--size=compact"]) == 2
    assert "BLOCKED" in capsys.readouterr().out
    assert len(application.topLevelWidgets()) == before_windows
    assert (
        review._show_environment_reason(os_name="posix", platform_name="linux", qpa_platform="")
        is not None
    )
    assert (
        review._show_environment_reason(
            os_name="nt", platform_name="win32", qpa_platform="offscreen"
        )
        is not None
    )
    assert (
        review._show_environment_reason(os_name="nt", platform_name="win32", qpa_platform="windows")
        is None
    )


def test_harness_imports_have_no_external_transport_or_process_module() -> None:
    script = Path(review.__file__).read_text(encoding="utf-8")
    tree = ast.parse(script)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert not imported.intersection(review._FORBIDDEN_IMPORTS)
