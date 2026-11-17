from __future__ import annotations

import gc
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from codex_bridge.console.workflows_dialog import WorkflowManagementDialog
from scripts import phase_c5_isolated_readonly_projection as readonly
from scripts import phase_c5_isolated_visible_review as review


@dataclass(frozen=True, slots=True)
class FakeJob:
    job_id: str
    task_identity: str
    state: str
    thread_id: str | None
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class FakeSnapshot:
    workflow_id: str
    run_id: str
    status: str
    created_at: str
    updated_at: str
    worker_state: str
    terminal: bool
    job_id: str | None
    thread_id: str | None
    max_live_codex_jobs: int
    jobs: tuple[FakeJob, ...]


@dataclass(slots=True)
class FakeContext:
    approval_id: str
    marker_data: dict[str, Any]
    repository_root: Path
    temp_root: Path
    candidate: Path
    state_root: Path
    database_path: Path
    marker_path: Path
    rows: dict[str, Any]
    snapshots: dict[str, FakeSnapshot]
    inspections: dict[str, Any]
    tasks: dict[str, dict[str, Any]]
    events: dict[str, dict[str, Any]]
    products: readonly.ReadOnlyProducts
    calls: dict[str, int]


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    application = QApplication.instance()
    if application is None:
        application = QApplication(["phase-c5-p3c-offscreen-tests"])
    assert application.platformName().lower() == "offscreen"
    yield application
    application.closeAllWindows()


def _make_context(tmp_path: Path) -> FakeContext:
    marker_data = readonly._synthetic_marker()
    approval_id = marker_data["approval_id"]
    repository_root = tmp_path / "repository"
    marker_path = repository_root / "docs" / ".phase_c5_attempt_budgets" / f"{approval_id}.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(json.dumps(marker_data), encoding="utf-8")

    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    candidate = temp_root / "codexbridge-phase-b5-synthetic"
    state_root = candidate / "localappdata" / "CodexBridge" / "workflows"
    for raw_run in marker_data["runs"].values():
        (state_root / raw_run["run_id"]).mkdir(parents=True)
    database_path = state_root / "requests.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE workflow_requests "
        "(workflow_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL)"
    )
    for case in review._EXPECTED_CASES:
        raw_run = marker_data["runs"][case]
        connection.execute(
            "INSERT INTO workflow_requests(workflow_id, run_id, state) VALUES (?, ?, ?)",
            (
                raw_run["workflow_id"],
                raw_run["run_id"],
                review._EXPECTED_REQUEST_STATES[case],
            ),
        )
    connection.commit()
    connection.close()

    rows: dict[str, Any] = {}
    snapshots: dict[str, FakeSnapshot] = {}
    inspections: dict[str, Any] = {}
    tasks: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    calls = {"find": 0, "inspect": 0, "snapshot": 0, "tasks": 0, "events": 0}

    for case in review._EXPECTED_CASES:
        raw_run = marker_data["runs"][case]
        workflow_id = raw_run["workflow_id"]
        run_id = raw_run["run_id"]
        jobs = review._EXPECTED_JOBS[case]
        state = review._EXPECTED_REQUEST_STATES[case]
        row = SimpleNamespace(
            workflow_id=workflow_id,
            run_id=run_id,
            state=state,
            created_at="2026-11-17T00:00:00+00:00",
            updated_at="2026-11-17T00:02:00+00:00",
        )
        rows[workflow_id] = row
        job_snapshots = tuple(
            FakeJob(
                job_id=f"sample-p3c-job-{case.lower()}-{index:02d}",
                task_identity=f"sample-p3c-task-{case.lower()}-{index:02d}",
                state="SUCCEEDED",
                thread_id=f"sample-p3c-thread-{case.lower()}-{index:02d}",
                turn_id=None,
            )
            for index in range(1, jobs + 1)
        )
        snapshots[workflow_id] = FakeSnapshot(
            workflow_id=workflow_id,
            run_id=run_id,
            status="succeeded",
            created_at=row.created_at,
            updated_at=row.updated_at,
            worker_state="completed",
            terminal=True,
            job_id=job_snapshots[0].job_id,
            thread_id=job_snapshots[0].thread_id,
            max_live_codex_jobs=jobs,
            jobs=() if case == "A" else job_snapshots,
        )
        inspections[workflow_id] = SimpleNamespace(
            result={"status": "succeeded", "final_text": f"offline result {case}"}
        )
        task_payload, event_payload = review._synthetic_graph_payload(case)
        task_payload["workflow_id"] = workflow_id
        task_payload["run_id"] = run_id
        event_payload["workflow_id"] = workflow_id
        event_payload["run_id"] = run_id
        tasks[workflow_id] = task_payload
        events[workflow_id] = event_payload

    def find_workflow(state_root_arg: Path, workflow_id: str) -> Any:
        calls["find"] += 1
        assert state_root_arg == state_root
        return rows.get(workflow_id)

    def inspect_run(state_root_arg: Path, row: Any) -> Any:
        calls["inspect"] += 1
        assert state_root_arg == state_root
        return inspections[row.workflow_id]

    def snapshot_from_inspection(row: Any, _inspection: Any) -> Any:
        calls["snapshot"] += 1
        return snapshots[row.workflow_id]

    def project_tasks(state_root_arg: Path, row: Any, _inspection: Any) -> Any:
        calls["tasks"] += 1
        assert state_root_arg == state_root
        return tasks[row.workflow_id]

    def project_events(
        state_root_arg: Path,
        row: Any,
        _inspection: Any,
        *,
        after_id: int,
        limit: int,
    ) -> Any:
        calls["events"] += 1
        assert state_root_arg == state_root
        assert after_id == 0
        assert limit == 100
        return events[row.workflow_id]

    products = readonly.ReadOnlyProducts(
        find_workflow=find_workflow,
        inspect_run=inspect_run,
        snapshot_from_inspection=snapshot_from_inspection,
        project_tasks=project_tasks,
        project_events=project_events,
    )
    return FakeContext(
        approval_id=approval_id,
        marker_data=marker_data,
        repository_root=repository_root,
        temp_root=temp_root,
        candidate=candidate,
        state_root=state_root,
        database_path=database_path,
        marker_path=marker_path,
        rows=rows,
        snapshots=snapshots,
        inspections=inspections,
        tasks=tasks,
        events=events,
        products=products,
        calls=calls,
    )


def _load(context: FakeContext, **kwargs: Any) -> review.ReviewBundle:
    return review.load_review_data(
        context.approval_id,
        repository_root=context.repository_root,
        temp_root=context.temp_root,
        expected_provenance=(1, 1),
        products=context.products,
        **kwargs,
    )


def test_synthetic_provenance_uses_a_single_readonly_matching_root(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    db_before = context.database_path.read_bytes()
    marker_before = context.marker_path.read_bytes()

    bundle = _load(context)

    assert [(run.case, run.request_state) for run in bundle.runs] == [
        ("A", "succeeded"),
        ("B", "succeeded"),
        ("C", "running"),
    ]
    assert (
        bundle.direct_candidate_count,
        bundle.readable_database_count,
        bundle.exact_matches,
    ) == (
        1,
        1,
        1,
    )
    assert context.calls == {"find": 3, "inspect": 3, "snapshot": 3, "tasks": 3, "events": 3}
    assert context.database_path.read_bytes() == db_before
    assert context.marker_path.read_bytes() == marker_before


def test_default_provenance_requires_the_reviewed_8_4_1_match(
    tmp_path: Path,
) -> None:
    context = _make_context(tmp_path)
    with pytest.raises(readonly.InspectionFailure) as caught:
        review.load_review_data(
            context.approval_id,
            repository_root=context.repository_root,
            temp_root=context.temp_root,
            products=context.products,
        )
    assert (caught.value.stage, caught.value.code) == (
        "candidate_match",
        "provenance_mismatch",
    )
    assert context.calls["find"] == 0


def test_zero_and_duplicate_matching_roots_fail_closed(tmp_path: Path) -> None:
    zero = _make_context(tmp_path / "zero")
    connection = sqlite3.connect(zero.database_path)
    connection.execute("UPDATE workflow_requests SET workflow_id = 'other-workflow'")
    connection.commit()
    connection.close()
    with pytest.raises(readonly.InspectionFailure) as zero_error:
        _load(zero)
    assert zero_error.value.code == "unique_state_root_required"

    duplicate = _make_context(tmp_path / "duplicate")
    second = duplicate.temp_root / "codexbridge-phase-b5-synthetic-duplicate"
    shutil.copytree(duplicate.candidate, second)
    with pytest.raises(readonly.InspectionFailure) as duplicate_error:
        _load(duplicate)
    assert duplicate_error.value.code == "unique_state_root_required"


def test_reparse_candidate_is_not_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = _make_context(tmp_path)
    original = readonly._is_reparse_point

    def reject_candidate(path: Path, result: Any = None) -> bool:
        if path == context.candidate:
            return True
        return original(path, result)

    monkeypatch.setattr(readonly, "_is_reparse_point", reject_candidate)
    with pytest.raises(readonly.InspectionFailure) as caught:
        _load(context)
    assert caught.value.code == "unique_state_root_required"
    assert context.calls["find"] == 0


@pytest.mark.parametrize(
    ("mutation", "expected_stage", "expected_code"),
    [
        ("partial", "tasks_projection", "task_projection_identity_invalid"),
        ("has_more", "events_projection", "event_projection_identity_invalid"),
        ("no_result", "inspect_run", "result_missing"),
        ("wrong_identity", "tasks_projection", "task_projection_identity_invalid"),
        ("wrong_status", "snapshot", "snapshot_success_contract_mismatch"),
        ("short_jobs", "snapshot", "job_snapshot_count_invalid"),
    ],
)
def test_inconsistent_projection_blocks_before_dialog_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_stage: str,
    expected_code: str,
) -> None:
    context = _make_context(tmp_path)
    run_b = context.marker_data["runs"]["B"]
    workflow_id = run_b["workflow_id"]
    if mutation == "partial":
        context.tasks[workflow_id]["coverage"] = "partial"
    elif mutation == "has_more":
        context.events[workflow_id]["has_more"] = True
    elif mutation == "no_result":
        context.inspections[workflow_id].result = None
    elif mutation == "wrong_identity":
        context.tasks[workflow_id]["workflow_id"] = "other-workflow"
    elif mutation == "wrong_status":
        context.snapshots[workflow_id] = replace(context.snapshots[workflow_id], status="unknown")
    elif mutation == "short_jobs":
        context.snapshots[workflow_id] = replace(
            context.snapshots[workflow_id], jobs=context.snapshots[workflow_id].jobs[:1]
        )

    monkeypatch.setattr(
        review,
        "_load_ui_types",
        lambda: pytest.fail("Qt dialog must not be loaded before projections validate"),
    )
    with pytest.raises(readonly.InspectionFailure) as caught:
        _load(context)
    assert (caught.value.stage, caught.value.code) == (expected_stage, expected_code)


def test_c_raw_request_running_is_preserved_with_public_succeeded(qapp: QApplication) -> None:
    bundle = review._synthetic_bundle()
    dialog, harness = review._create_dialog(qapp, WorkflowManagementDialog, bundle, "regular")
    try:
        dialog.workflow_list.setCurrentRow(2)
        qapp.processEvents()
        assert "running" in dialog.status_label.text()
        assert "succeeded" in dialog.status_label.text()
        c_run = bundle.runs[2]
        assert c_run.request_state == "running"
        assert c_run.detail["status"] == "succeeded"
        assert c_run.task_snapshot["workflow_status"] == "succeeded"
    finally:
        dialog.close()
        del harness


@pytest.mark.parametrize(("size", "expected"), [("regular", (900, 760)), ("compact", (720, 600))])
def test_real_dialog_switches_a_b_c_with_readonly_dtos_and_disabled_start(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    size: str,
    expected: tuple[int, int],
) -> None:
    bundle = review._synthetic_bundle()
    monkeypatch.setattr(
        readonly,
        "_read_marker",
        lambda *_args, **_kwargs: pytest.fail("GUI interactions must not reread marker"),
    )
    monkeypatch.setattr(
        readonly,
        "find_unique_state_root",
        lambda *_args, **_kwargs: pytest.fail("GUI interactions must not rescan Temp"),
    )

    def forbidden_external_call(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("GUI interactions must not start network or child processes")

    monkeypatch.setattr(socket, "socket", forbidden_external_call)
    monkeypatch.setattr(socket, "create_connection", forbidden_external_call)
    monkeypatch.setattr(subprocess, "Popen", forbidden_external_call)
    modules_before = set(sys.modules)
    dialog, harness = review._create_dialog(qapp, WorkflowManagementDialog, bundle, size)
    assert not any(
        module in sys.modules and module not in modules_before
        for module in review._FORBIDDEN_MODULES
    )
    dialog.show()
    qapp.processEvents()
    assert (dialog.width(), dialog.height()) == expected
    assert "Workflow" in dialog.windowTitle()
    assert "起動不可" in dialog.windowTitle()
    assert dialog.workflow_list.count() == 3
    assert not dialog.start_button.isEnabled()
    start_signal_calls: list[bool] = []
    dialog.start_requested.connect(lambda: start_signal_calls.append(True))
    dialog.start_button.click()
    assert start_signal_calls == []

    runs_by_case = {run.case: run for run in bundle.runs}
    for row, case in ((1, "B"), (2, "C"), (0, "A")):
        run = runs_by_case[case]
        dialog.workflow_list.setCurrentRow(row)
        qapp.processEvents()
        assert dialog.detail_labels["workflow_id"].text() == run.workflow_id
        assert dialog.detail_labels["terminal"].text()
        assert run.result["final_text"] in dialog.final_text_view.toPlainText()
        assert dialog.graph_widget.snapshot_identity == (
            run.workflow_id,
            run.run_id,
            run.task_snapshot["snapshot_version"],
        )
        assert dialog.graph_widget.event_cursor == review._EXPECTED_EVENT_COUNTS[case]
        assert dialog.graph_widget.event_list.count() == review._EXPECTED_EVENT_COUNTS[case]
        assert len(dialog.graph_widget.node_items) == review._EXPECTED_GRAPH_COUNTS[case]["nodes"]
        assert (
            sum(len(node["executions"]) for node in run.task_snapshot["nodes"])
            == review._EXPECTED_GRAPH_COUNTS[case]["executions"]
        )
        assert not dialog.start_button.isEnabled()

    dialog.workflow_list.setCurrentRow(1)
    qapp.processEvents()
    assert dialog.job_list.count() == 2
    assert not dialog.start_button.isEnabled()

    c_run = runs_by_case["C"]
    dialog.workflow_list.setCurrentRow(2)
    dialog.result_tabs.setCurrentIndex(1)
    qapp.processEvents()
    graph = dialog.graph_widget
    assert graph.event_cursor == 30
    assert "running" in dialog.status_label.text()
    assert "succeeded" in dialog.status_label.text()
    graph.node_list.setCurrentRow(1)
    qapp.processEvents()
    assert graph.selected_node_id is not None
    selected_node = graph.node_items[graph.selected_node_id]
    graph.graph_view.scale(1.35, 1.35)
    vertical_scrollbar = graph.graph_view.verticalScrollBar()
    vertical_scrollbar.setValue(vertical_scrollbar.maximum())
    old_scroll = vertical_scrollbar.value()
    old_identity = graph.snapshot_identity
    dialog.refresh_button.click()
    qapp.processEvents()
    assert graph.snapshot_identity == old_identity
    assert graph.node_items[graph.selected_node_id] is selected_node
    assert graph.graph_view.transform().m11() == pytest.approx(1.35)
    assert graph.graph_view.verticalScrollBar().value() == old_scroll
    dialog.result_button.click()
    qapp.processEvents()
    assert dialog.result_tabs.currentIndex() == 0
    assert c_run.result["final_text"] in dialog.final_text_view.toPlainText()
    assert not dialog.start_button.isEnabled()

    dialog.workflow_list.setCurrentRow(0)
    qapp.processEvents()
    a_run = runs_by_case["A"]
    assert dialog.graph_widget.snapshot_identity is not None
    assert dialog.graph_widget.snapshot_identity[:2] == (a_run.workflow_id, a_run.run_id)
    assert dialog.graph_widget.graph_view.transform().m11() == pytest.approx(1.0)
    assert not dialog.start_button.isEnabled()
    dialog.close()
    qapp.processEvents()
    del harness


def test_event_loop_holds_harness_alive_across_gc_and_selection(qapp: QApplication) -> None:
    bundle = review._synthetic_bundle()
    dialog = WorkflowManagementDialog()
    observed: list[str] = []

    class FakeApplication:
        def exec(self) -> int:
            gc.collect()
            dialog.workflow_list.setCurrentRow(2)
            qapp.processEvents()
            observed.append(dialog.detail_labels["workflow_id"].text())
            return 0

    assert review._run_dialog_loop(FakeApplication(), dialog, bundle, "compact") == 0
    assert observed == [bundle.runs[2].workflow_id]
    assert dialog.size().width() == 720
    assert dialog.size().height() == 600
    assert not dialog.isVisible()


def test_prepare_and_help_never_access_real_marker_or_temp(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("prepare/help must not access marker or Temp")

    monkeypatch.setattr(readonly, "_read_marker", forbidden)
    monkeypatch.setattr(readonly, "_windows_temp_root", forbidden)
    monkeypatch.setattr(readonly, "find_unique_state_root", forbidden)
    before = QApplication.instance()
    assert review.main(["--prepare"]) == 0
    output = capsys.readouterr().out
    assert '"status":"PASS"' in output
    assert "synthetic_ABC_dtos" in output
    assert QApplication.instance() is before
    assert review.main([]) == 0
    assert "--prepare" in capsys.readouterr().out


def test_cli_prepare_rejects_unused_approval_id_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert review.main(["--prepare", "--approval-id", "c" * 32]) == 2
    output = capsys.readouterr().out
    assert '"stage":"cli_validate"' in output
    assert "c" * 32 not in output


def test_synthetic_payloads_match_three_expected_graph_and_event_counts() -> None:
    for case in review._EXPECTED_CASES:
        snapshot, events = review._synthetic_graph_payload(case)
        assert snapshot["counts"] == review._EXPECTED_GRAPH_COUNTS[case]
        assert len(snapshot["nodes"]) == review._EXPECTED_GRAPH_COUNTS[case]["nodes"]
        assert len(snapshot["edges"]) == review._EXPECTED_GRAPH_COUNTS[case]["edges"]
        assert len(events["events"]) == review._EXPECTED_EVENT_COUNTS[case]
        assert events["has_more"] is False
