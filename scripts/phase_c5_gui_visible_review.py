"""Offline-only visible review harness for the Phase C5 workflow dialog."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication

    from codex_bridge.console.workflows_dialog import WorkflowManagementDialog


_FORBIDDEN_MODULES = (
    "codex_bridge.console.workflow_controller",
    "codex_bridge.console.api_client",
    "codex_bridge.console.main_console",
    "codex_bridge.bridge",
)
_SAMPLE_ID = re.compile(r"^sample-c5-[a-z0-9-]{1,64}$", re.ASCII)
_SIZES = {"regular": (900, 760), "compact": (720, 600)}


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    workflow_id: str
    run_id: str
    status: str
    coverage: str
    coverage_reason: str | None
    jobs: int
    execution_state: str


_SCENARIOS = (
    Scenario(
        "a",
        "A · 1 job · succeeded",
        "sample-c5-workflow-a-001",
        "sample-c5-run-a-001",
        "succeeded",
        "complete",
        None,
        1,
        "succeeded",
    ),
    Scenario(
        "b",
        "B · 2 jobs · succeeded",
        "sample-c5-workflow-b-001",
        "sample-c5-run-b-001",
        "succeeded",
        "complete",
        None,
        2,
        "succeeded",
    ),
    Scenario(
        "c",
        "C · for 3回 · 3 jobs · succeeded",
        "sample-c5-workflow-c-001",
        "sample-c5-run-c-001",
        "succeeded",
        "complete",
        None,
        3,
        "succeeded",
    ),
    Scenario(
        "unknown",
        "Unknown · running evidence",
        "sample-c5-workflow-unknown-001",
        "sample-c5-run-unknown-001",
        "unknown",
        "partial",
        "workflow_not_confirmed",
        1,
        "unknown",
    ),
    Scenario(
        "reconciliation",
        "Needs reconciliation",
        "sample-c5-workflow-reconcile-001",
        "sample-c5-run-reconcile-001",
        "needs_reconciliation",
        "partial",
        "workflow_not_confirmed",
        1,
        "needs_reconciliation",
    ),
    Scenario(
        "failed",
        "Failed · operation error",
        "sample-c5-workflow-failed-001",
        "sample-c5-run-failed-001",
        "failed",
        "complete",
        None,
        1,
        "failed",
    ),
    Scenario(
        "legacy",
        "Legacy · coverage unavailable",
        "sample-c5-workflow-legacy-001",
        "sample-c5-run-legacy-001",
        "unknown",
        "unavailable",
        "journal_missing",
        0,
        "unknown",
    ),
)


def build_scenarios() -> tuple[Scenario, ...]:
    """Return the fixed synthetic scenarios used by the GUI and tests."""
    return _SCENARIOS


def _node_state(scenario: Scenario, *, operation: str) -> str:
    if scenario.status == "succeeded":
        return "succeeded"
    if scenario.status == "failed":
        return "failed" if operation in {"do", "call"} else "succeeded"
    if scenario.status == "unknown":
        return "running" if operation == "do" else "unknown"
    if scenario.status == "needs_reconciliation":
        return "running" if operation == "do" else "needs_reconciliation"
    return "unknown"


def _execution(
    scenario: Scenario,
    index: int,
    state: str,
    *,
    job_index: int | None = None,
) -> dict[str, object]:
    completed = state in {"succeeded", "failed"}
    result: dict[str, object] = {
        "execution_id": f"sample-c5-execution-{scenario.key}-{index:02d}",
        "task_identity": f"sample-c5-task-{scenario.key}-{index:02d}",
        "state": state,
        "started_at": f"2026-11-17T00:00:{index:02d}Z",
        "completed_at": f"2026-11-17T00:01:{index:02d}Z" if completed else None,
        "completion_observed": completed,
        "observation_complete": completed,
        "evidence_sources": ["codex_job_ledger" if job_index is not None else "node_events"],
        "evidence_conflict": False,
    }
    if job_index is not None:
        result["job_id"] = f"sample-c5-job-{scenario.key}-{job_index:02d}"
        result["thread_id"] = f"sample-c5-thread-{scenario.key}-{job_index:02d}"
    return result


def _node(
    node_id: str,
    operation: str,
    order: int,
    state: str,
    executions: list[dict[str, object]],
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


def _snapshot(scenario: Scenario) -> dict[str, object]:
    many_state = _node_state(scenario, operation="for")
    switch_state = _node_state(scenario, operation="switch")
    do_state = _node_state(scenario, operation="do")
    call_state = _node_state(scenario, operation="call")
    execution_state = scenario.execution_state
    iteration_count = scenario.jobs
    do_count = min(2, scenario.jobs)

    nodes = [
        _node(
            "/do/0/for-zero",
            "for",
            0,
            "succeeded" if scenario.status == "succeeded" else many_state,
            [],
        ),
        _node(
            "/do/0/for-many",
            "for",
            1,
            many_state,
            [
                _execution(scenario, index, execution_state)
                for index in range(1, iteration_count + 1)
            ],
        ),
        _node(
            "/do/0/switch-one",
            "switch",
            2,
            switch_state,
            [_execution(scenario, 10, switch_state)] if scenario.jobs else [],
        ),
        _node(
            "/do/0/do-two",
            "do",
            3,
            do_state,
            [_execution(scenario, 20 + index, do_state) for index in range(1, do_count + 1)],
        ),
        _node(
            "/do/0/call-three",
            "call",
            4,
            call_state,
            [
                _execution(scenario, 30 + index, execution_state, job_index=index)
                for index in range(1, scenario.jobs + 1)
            ],
        ),
    ]
    edges = [
        {"source": "@scope:/do/0:entry", "target": "/do/0/for-zero", "kind": "scope_entry"},
        {"source": "/do/0/for-zero", "target": "/do/0/for-many", "kind": "loop_body"},
        {"source": "/do/0/for-many", "target": "/do/0/for-many", "kind": "loop_repeat"},
        {"source": "/do/0/for-many", "target": "/do/0/switch-one", "kind": "loop_exit"},
        {"source": "/do/0/switch-one", "target": "/do/0/do-two", "kind": "sequence"},
        {"source": "/do/0/do-two", "target": "/do/0/call-three", "kind": "sequence"},
        {"source": "/do/0/call-three", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
    ]
    event_payload = _events(scenario)
    latest_event_id = event_payload["latest_event_id"]
    return {
        "schema_version": 1,
        "workflow_id": scenario.workflow_id,
        "run_id": scenario.run_id,
        "workflow_status": scenario.status,
        "coverage": scenario.coverage,
        "coverage_reason": scenario.coverage_reason,
        "latest_event_id": latest_event_id,
        "snapshot_version": "sha256:" + scenario.key.encode().hex().ljust(64, "0")[:64],
        "truncated": False,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "executions": iteration_count + (1 if scenario.jobs else 0) + do_count + scenario.jobs,
        },
        "nodes": nodes,
        "edges": edges,
    }


def _events(scenario: Scenario) -> dict[str, object]:
    if scenario.coverage == "unavailable":
        return {
            "schema_version": 1,
            "workflow_id": scenario.workflow_id,
            "run_id": scenario.run_id,
            "events": [],
            "next_after_id": 0,
            "latest_event_id": 0,
            "has_more": False,
            "truncated": False,
            "coverage": "unavailable",
            "coverage_reason": "journal_missing",
        }

    records: list[dict[str, object]] = []

    def add(event_type: str, node_id: str | None, **metadata: object) -> None:
        event_id = len(records) + 1
        records.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "node_id": node_id,
                "occurred_at": f"2026-11-17T00:02:{event_id:02d}Z",
                "source": "interpreter",
                "metadata": metadata,
            }
        )

    add("scope_entered", "/do/0/for-zero")
    for index in range(1, scenario.jobs + 1):
        add("iteration_started", "/do/0/for-many", iteration_ordinal=index)
        add("iteration_completed", "/do/0/for-many", iteration_ordinal=index)
    add("branch_selected", "/do/0/switch-one", branch_class="continue", case_index=0)
    add("scope_entered", "/do/0/do-two")
    for index in range(1, scenario.jobs + 1):
        add("task_started", "/do/0/call-three", iteration_ordinal=index)
        if scenario.execution_state == "succeeded":
            add("task_completed", "/do/0/call-three", iteration_ordinal=index)
        elif scenario.execution_state == "failed":
            add(
                "task_failed",
                "/do/0/call-three",
                iteration_ordinal=index,
                failure_phase="operation",
            )
    if scenario.status == "succeeded":
        add("workflow_completed", None)

    latest_event_id = len(records)
    return {
        "schema_version": 1,
        "workflow_id": scenario.workflow_id,
        "run_id": scenario.run_id,
        "events": records,
        "next_after_id": latest_event_id,
        "latest_event_id": latest_event_id,
        "has_more": False,
        "truncated": False,
        "coverage": scenario.coverage,
        "coverage_reason": scenario.coverage_reason,
    }


def build_fixture(scenario: Scenario) -> dict[str, object]:
    """Build a fresh, fixed UI payload for one synthetic scenario."""
    jobs = [
        {
            "job_id": f"sample-c5-job-{scenario.key}-{index:02d}",
            "thread_id": f"sample-c5-thread-{scenario.key}-{index:02d}",
            "state": "SUCCEEDED"
            if scenario.execution_state == "succeeded"
            else scenario.execution_state.upper(),
        }
        for index in range(1, scenario.jobs + 1)
    ]
    snapshot = _snapshot(scenario)
    event_payload = _events(scenario)
    return {
        "workflow": {
            "workflow_id": scenario.workflow_id,
            "status": scenario.status,
            "created_at": "2026-11-17T00:00:00Z",
            "updated_at": "2026-11-17T00:02:00Z",
        },
        "detail": {
            "workflow_id": scenario.workflow_id,
            "status": scenario.status,
            "created_at": "2026-11-17T00:00:00Z",
            "updated_at": "2026-11-17T00:02:00Z",
            "worker_state": "completed"
            if scenario.status in {"succeeded", "failed"}
            else "unknown",
            "terminal": scenario.status.upper(),
            "max_live_codex_jobs": scenario.jobs if scenario.jobs else 1,
            "jobs": jobs,
        },
        "result": {
            "result": {
                "final_text": (
                    f"合成データです。実Workflowは実行していません。シナリオ: {scenario.label}"
                ),
                "scenario": scenario.key,
                "synthetic": True,
            }
        },
        "snapshot": snapshot,
        "events": event_payload,
    }


def _scenario_rows() -> list[dict[str, object]]:
    return [
        {
            "workflow_id": scenario.workflow_id,
            "status": scenario.status,
            "created_at": "2026-11-17T00:00:00Z",
            "updated_at": "2026-11-17T00:02:00Z",
        }
        for scenario in _SCENARIOS
    ]


def _validate_fixture_set() -> bool:
    if len(_SCENARIOS) != 7 or len({scenario.workflow_id for scenario in _SCENARIOS}) != 7:
        return False
    for scenario in _SCENARIOS:
        if not _SAMPLE_ID.fullmatch(scenario.workflow_id) or not _SAMPLE_ID.fullmatch(
            scenario.run_id
        ):
            return False
        fixture = build_fixture(scenario)
        snapshot = fixture["snapshot"]
        events = fixture["events"]
        if not isinstance(snapshot, Mapping) or not isinstance(events, Mapping):
            return False
        if (
            snapshot.get("schema_version") != 1
            or snapshot.get("workflow_id") != scenario.workflow_id
            or snapshot.get("run_id") != scenario.run_id
            or events.get("schema_version") != 1
            or events.get("workflow_id") != scenario.workflow_id
            or events.get("run_id") != scenario.run_id
        ):
            return False
        for job in fixture["detail"]["jobs"]:  # type: ignore[index]
            if not isinstance(job, Mapping) or not _SAMPLE_ID.fullmatch(str(job.get("job_id", ""))):
                return False
    return True


def _load_ui_types() -> tuple[type[QApplication], type[WorkflowManagementDialog]]:
    from PySide6.QtWidgets import QApplication

    from codex_bridge.console.workflows_dialog import WorkflowManagementDialog

    return QApplication, WorkflowManagementDialog


def prepare() -> tuple[bool, tuple[str, ...]]:
    """Check local GUI prerequisites without creating a QApplication or window."""
    checks: list[tuple[str, bool]] = []
    checks.append(("Windows native", sys.platform == "win32"))
    if sys.platform != "win32":
        checks.append(("PySide6 dialog classes", False))
        checks.append(("seven fixed offline fixtures", _validate_fixture_set()))
        checks.append(("no QApplication or window created", True))
        checks.append(("no controller, API client, or Bridge imported", True))
        return False, tuple(
            f"{'PASS' if passed else 'BLOCKED'}: {label}" for label, passed in checks
        )

    before_modules = set(sys.modules)
    try:
        application_type, dialog_type = _load_ui_types()
    except (ImportError, OSError):
        application_type = None
        dialog_type = None
        existing_application = None
    else:
        assert application_type is not None
        existing_application = application_type.instance()
    checks.append(
        ("PySide6 dialog classes", application_type is not None and dialog_type is not None)
    )
    checks.append(("seven fixed offline fixtures", _validate_fixture_set()))
    created_forbidden = any(
        module in sys.modules and module not in before_modules for module in _FORBIDDEN_MODULES
    )
    checks.append(("no controller, API client, or Bridge imported", not created_forbidden))
    no_application_created = (
        application_type is None or application_type.instance() is existing_application
    )
    checks.append(("no QApplication or window created", no_application_created))
    return all(passed for _label, passed in checks), tuple(
        f"{'PASS' if passed else 'BLOCKED'}: {label}" for label, passed in checks
    )


class VisibleReviewHarness:
    """Connect the real dialog only to local immutable synthetic payloads."""

    def __init__(self, dialog: WorkflowManagementDialog, size: str = "regular") -> None:
        if size not in _SIZES:
            raise ValueError("unknown sample size")
        self.dialog = dialog
        self._by_id = {scenario.workflow_id: scenario for scenario in _SCENARIOS}
        self._fixtures = {scenario.workflow_id: build_fixture(scenario) for scenario in _SCENARIOS}
        width, height = _SIZES[size]
        dialog.resize(width, height)
        dialog.setWindowTitle("合成データ・実Workflow未実行 — Workflow management")
        dialog.set_message("合成データ・実Workflow未実行。外部接続とWorkflow開始はありません。")
        dialog.status_label.setStyleSheet("font-weight: bold; color: #8a3d00;")
        dialog.set_bridge_ready(False)
        dialog.set_start_enabled(False)
        dialog.workflow_selected.connect(self.select_workflow)
        dialog.refresh_requested.connect(self.refresh)
        dialog.result_requested.connect(self.show_result)
        dialog.set_workflows(_scenario_rows(), selected_workflow_id=_SCENARIOS[0].workflow_id)
        selected = dialog.selected_workflow_id
        if selected is not None and dialog.graph_widget.snapshot_identity is None:
            self.select_workflow(selected)

    def select_workflow(self, workflow_id: str) -> None:
        scenario = self._by_id.get(workflow_id)
        fixture = self._fixtures.get(workflow_id)
        if scenario is None or fixture is None:
            return
        self.dialog.set_detail(cast(Mapping[str, object], fixture["detail"]))
        self.dialog.set_result(fixture["result"])
        self.dialog.graph_widget.set_snapshot(workflow_id, fixture["snapshot"])
        self.dialog.graph_widget.set_events(fixture["events"])
        self.dialog.set_message(f"合成データ・実Workflow未実行。表示中: {scenario.label}")
        self.dialog.set_bridge_ready(False)
        self.dialog.set_start_enabled(False)

    def refresh(self) -> None:
        selected = self.dialog.selected_workflow_id
        self.dialog.set_workflows(_scenario_rows(), selected_workflow_id=selected)
        if selected is not None:
            self.select_workflow(selected)

    def show_result(self) -> None:
        selected = self.dialog.selected_workflow_id
        if selected is not None:
            fixture = self._fixtures.get(selected)
            if fixture is not None:
                self.dialog.set_result(fixture["result"])
                self.dialog.result_tabs.setCurrentIndex(0)


def _show(size: str) -> int:
    if sys.platform != "win32":
        print("BLOCKED: --show requires a Windows native session.")
        return 2
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        print("BLOCKED: --show is unavailable when QT_QPA_PLATFORM=offscreen.")
        return 2
    application_type, dialog_type = _load_ui_types()
    candidate = application_type.instance()
    if isinstance(candidate, application_type):
        application = candidate
    else:
        application = application_type(["phase-c5-gui-visible-review"])
    application.setApplicationName("Phase C5 GUI visible review")
    application.setQuitOnLastWindowClosed(True)
    dialog = dialog_type()
    harness = VisibleReviewHarness(dialog, size)
    dialog.show()
    try:
        return application.exec()
    finally:
        del harness


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.phase_c5_gui_visible_review",
        description="Offline synthetic-data review harness for the C5 workflow dialog.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="check offline GUI prerequisites only")
    mode.add_argument("--show", action="store_true", help="show the synthetic review dialog")
    parser.add_argument("--size", choices=tuple(_SIZES), default="regular")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.prepare and not args.show:
        parser.print_help()
        return 0
    if args.prepare and args.size != "regular":
        parser.error("--size is only valid with --show")
    if args.prepare:
        passed, messages = prepare()
        for message in messages:
            print(message)
        return 0 if passed else 2
    return _show(args.size)


if __name__ == "__main__":
    raise SystemExit(main())
