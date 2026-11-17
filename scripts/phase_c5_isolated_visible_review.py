"""Read-only Qt viewer for one completed, isolated Phase C5 attempt."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Never

from scripts import phase_c5_isolated_readonly_projection as readonly

_APPROVAL_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_SHA256_VERSION = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SIZES = {"regular": (900, 760), "compact": (720, 600)}
_EXPECTED_CASES = ("A", "B", "C")
_EXPECTED_JOBS = {"A": 1, "B": 2, "C": 3}
_EXPECTED_REQUEST_STATES = {"A": "succeeded", "B": "succeeded", "C": "running"}
_EXPECTED_EVENT_COUNTS = {"A": 3, "B": 5, "C": 30}
_EXPECTED_GRAPH_COUNTS = {
    "A": {"nodes": 1, "edges": 2, "executions": 1},
    "B": {"nodes": 2, "edges": 3, "executions": 2},
    "C": {"nodes": 4, "edges": 10, "executions": 7},
}
_EXPECTED_PROVENANCE = (8, 4)
_WORKFLOW_STATES = frozenset(readonly.WORKFLOW_STATES)
_WORKER_STATES = frozenset(readonly.WORKER_STATES)
_FORBIDDEN_MODULES = (
    "codex_bridge.console.workflow_controller",
    "codex_bridge.console.api_client",
    "codex_bridge.console.main_console",
    "codex_bridge.bridge",
    "codex_bridge.workflows.worker",
    "codex_bridge.workflows.worker_manager",
)


@dataclass(frozen=True, slots=True)
class VisibleRun:
    case: str
    workflow_id: str
    run_id: str
    request_state: str
    workflow_row: Mapping[str, object]
    detail: Mapping[str, object]
    result: dict[str, Any]
    task_snapshot: dict[str, Any]
    task_events: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    runs: tuple[VisibleRun, ...]
    direct_candidate_count: int
    readable_database_count: int
    exact_matches: int


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise _ArgumentError


class _ArgumentError(Exception):
    pass


def _blocked(
    stage: str,
    code: str,
    error: BaseException | None = None,
) -> readonly.InspectionFailure:
    exception_class = readonly._safe_exception_class(error) if error is not None else None
    return readonly._blocked(stage, code, exception_class)


def _call_product(stage: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return readonly._call_product(stage, function, *args, **kwargs)


def _safe_mapping(value: object, stage: str, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _blocked(stage, code)
    return value


def _get_attr(value: object, name: str, stage: str) -> Any:
    try:
        return getattr(value, name)
    except Exception as error:
        raise _blocked(stage, "projection_shape_invalid", error) from None


def _snapshot_detail(snapshot: object, stage: str) -> dict[str, object]:
    if not is_dataclass(snapshot) or isinstance(snapshot, type):
        raise _blocked(stage, "snapshot_dataclass_invalid")
    try:
        values = asdict(snapshot)
    except Exception as error:
        raise _blocked(stage, "snapshot_conversion_failed", error) from None
    required = (
        "workflow_id",
        "run_id",
        "status",
        "created_at",
        "updated_at",
        "worker_state",
        "terminal",
        "job_id",
        "thread_id",
        "max_live_codex_jobs",
        "jobs",
    )
    if set(values) != set(required):
        raise _blocked(stage, "snapshot_schema_invalid")
    result: dict[str, object] = {
        key: values[key]
        for key in (
            "workflow_id",
            "run_id",
            "status",
            "created_at",
            "updated_at",
            "worker_state",
            "terminal",
        )
    }
    for key in ("job_id", "thread_id"):
        if values[key] is not None:
            result[key] = values[key]
    max_jobs = values["max_live_codex_jobs"]
    if type(max_jobs) is not int or max_jobs not in {1, 2, 3}:
        raise _blocked(stage, "job_limit_invalid")
    if max_jobs in {2, 3}:
        jobs = values["jobs"]
        if type(jobs) not in {tuple, list} or len(jobs) != max_jobs:
            raise _blocked(stage, "job_snapshot_count_invalid")
        result["max_live_codex_jobs"] = max_jobs
        result["jobs"] = list(jobs)
    return result


def _run_item(snapshot_payload: Mapping[str, object]) -> dict[str, object]:
    result = {
        key: snapshot_payload[key] for key in ("workflow_id", "status", "created_at", "updated_at")
    }
    for key in ("job_id", "thread_id"):
        if key in snapshot_payload:
            result[key] = snapshot_payload[key]
    return result


def _load_graph_validators() -> tuple[Callable[..., Any], Callable[..., bool]]:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            graph_module = importlib.import_module("codex_bridge.console.workflow_graph_widget")
        return graph_module._validate_snapshot, graph_module._valid_node_id
    except Exception as error:
        raise _blocked("task_projection", "graph_validator_unavailable", error) from None


def _validate_event_payload(
    payload: object,
    *,
    workflow_id: str,
    run_id: str,
    expected_count: int,
    valid_node_id: Callable[[object], bool],
) -> dict[str, Any]:
    stage = "events_projection"
    event_payload = _safe_mapping(payload, stage, "event_projection_shape_invalid")
    base_keys = {
        "schema_version",
        "workflow_id",
        "run_id",
        "events",
        "next_after_id",
        "has_more",
        "truncated",
        "latest_event_id",
        "coverage",
    }
    if frozenset(event_payload) not in {
        frozenset(base_keys),
        frozenset(base_keys | {"coverage_reason"}),
    }:
        raise _blocked(stage, "event_projection_schema_invalid")
    if (
        type(event_payload.get("schema_version")) is not int
        or event_payload.get("schema_version") != 1
        or event_payload.get("workflow_id") != workflow_id
        or event_payload.get("run_id") != run_id
        or event_payload.get("coverage") != "complete"
        or type(event_payload.get("has_more")) is not bool
        or event_payload.get("has_more") is not False
        or type(event_payload.get("truncated")) is not bool
        or event_payload.get("truncated") is not False
        or type(event_payload.get("next_after_id")) is not int
        or type(event_payload.get("latest_event_id")) is not int
        or event_payload.get("next_after_id") != expected_count
        or event_payload.get("latest_event_id") != expected_count
    ):
        raise _blocked(stage, "event_projection_identity_invalid")
    if event_payload.get("coverage_reason") is not None:
        raise _blocked(stage, "event_projection_coverage_invalid")
    events = event_payload.get("events")
    if type(events) is not list or len(events) != expected_count or len(events) > 100:
        raise _blocked(stage, "event_page_incomplete")
    allowed_event_types = {
        "task_started",
        "task_completed",
        "task_failed",
        "branch_selected",
        "scope_entered",
        "scope_exited",
        "iteration_started",
        "iteration_completed",
        "workflow_completed",
    }
    seen: set[int] = set()
    for expected_id, event in enumerate(events, start=1):
        if not isinstance(event, Mapping):
            raise _blocked(stage, "event_record_invalid")
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        node_id = event.get("node_id")
        occurred_at = event.get("occurred_at")
        if (
            type(event_id) is not int
            or event_id != expected_id
            or event_id in seen
            or type(event_type) is not str
            or event_type not in allowed_event_types
            or (event_type == "workflow_completed" and node_id is not None)
            or (event_type != "workflow_completed" and node_id is None)
            or (node_id is not None and not valid_node_id(node_id))
            or type(occurred_at) is not str
            or len(occurred_at) > 128
            or _CONTROL.search(occurred_at) is not None
            or event.get("source") != "interpreter"
            or not isinstance(event.get("metadata", {}), Mapping)
        ):
            raise _blocked(stage, "event_record_invalid")
        seen.add(event_id)
    return dict(event_payload)


def _validate_task_payload(
    payload: object,
    *,
    workflow_id: str,
    run_id: str,
    expected_status: str,
    expected_counts: Mapping[str, int],
    expected_latest_event_id: int,
    validate_snapshot: Callable[[str, object], object],
) -> dict[str, Any]:
    stage = "tasks_projection"
    task_payload = _safe_mapping(payload, stage, "task_projection_shape_invalid")
    base_keys = {
        "schema_version",
        "workflow_id",
        "run_id",
        "workflow_status",
        "coverage",
        "latest_event_id",
        "truncated",
        "nodes",
        "edges",
        "counts",
        "snapshot_version",
    }
    if frozenset(task_payload) not in {
        frozenset(base_keys),
        frozenset(base_keys | {"coverage_reason"}),
    }:
        raise _blocked(stage, "task_projection_schema_invalid")
    if (
        type(task_payload.get("schema_version")) is not int
        or task_payload.get("schema_version") != 1
        or task_payload.get("workflow_id") != workflow_id
        or task_payload.get("run_id") != run_id
        or task_payload.get("workflow_status") != expected_status
        or task_payload.get("coverage") != "complete"
        or task_payload.get("coverage_reason") is not None
        or type(task_payload.get("truncated")) is not bool
        or task_payload.get("truncated") is not False
        or type(task_payload.get("latest_event_id")) is not int
        or task_payload.get("latest_event_id") != expected_latest_event_id
        or type(task_payload.get("snapshot_version")) is not str
        or _SHA256_VERSION.fullmatch(task_payload["snapshot_version"]) is None
    ):
        raise _blocked(stage, "task_projection_identity_invalid")
    counts = task_payload.get("counts")
    if not isinstance(counts, Mapping) or dict(counts) != dict(expected_counts):
        raise _blocked(stage, "task_projection_counts_invalid")
    nodes = task_payload.get("nodes")
    edges = task_payload.get("edges")
    if type(nodes) is not list or len(nodes) != expected_counts["nodes"]:
        raise _blocked(stage, "task_projection_counts_invalid")
    if type(edges) is not list or len(edges) != expected_counts["edges"]:
        raise _blocked(stage, "task_projection_counts_invalid")
    try:
        normalized = validate_snapshot(workflow_id, task_payload)
    except Exception as error:
        raise _blocked(stage, "graph_snapshot_validation_failed", error) from None
    if normalized is None:
        raise _blocked(stage, "graph_snapshot_invalid")
    return dict(task_payload)


def _validate_snapshot_fields(
    run: readonly.MarkerRun,
    request_row: object,
    snapshot: object,
    inspection: object,
) -> tuple[str, str, dict[str, object], dict[str, Any]]:
    stage = "snapshot"
    row_workflow_id = _get_attr(request_row, "workflow_id", "find_workflow")
    row_run_id = _get_attr(request_row, "run_id", "find_workflow")
    request_state = _get_attr(request_row, "state", "find_workflow")
    if (
        row_workflow_id != run.workflow_id
        or row_run_id != run.run_id
        or request_state != _EXPECTED_REQUEST_STATES[run.name]
    ):
        raise _blocked("find_workflow", "request_identity_mismatch")

    fields = {
        name: _get_attr(snapshot, name, stage)
        for name in (
            "workflow_id",
            "run_id",
            "status",
            "created_at",
            "updated_at",
            "worker_state",
            "terminal",
            "job_id",
            "thread_id",
            "max_live_codex_jobs",
            "jobs",
        )
    }
    if (
        fields["workflow_id"] != run.workflow_id
        or fields["run_id"] != run.run_id
        or fields["status"] != "succeeded"
        or fields["terminal"] is not True
        or fields["worker_state"] != "completed"
        or type(fields["created_at"]) is not str
        or type(fields["updated_at"]) is not str
        or type(fields["max_live_codex_jobs"]) is not int
        or fields["max_live_codex_jobs"] != run.expected_jobs
    ):
        raise _blocked(stage, "snapshot_success_contract_mismatch")

    if run.name == "A":
        if type(fields["job_id"]) is not str or not fields["job_id"]:
            raise _blocked(stage, "single_job_identity_missing")
    else:
        jobs = fields["jobs"]
        if type(jobs) not in {tuple, list} or len(jobs) != run.expected_jobs:
            raise _blocked(stage, "job_snapshot_count_invalid")
        for job in jobs:
            job_id = _get_attr(job, "job_id", stage)
            if type(job_id) is not str or not job_id:
                raise _blocked(stage, "job_snapshot_identity_invalid")

    result = _get_attr(inspection, "result", "inspect_run")
    if type(result) is not dict:
        raise _blocked("inspect_run", "result_missing")
    detail = _snapshot_detail(snapshot, stage)
    return request_state, run.workflow_id, detail, result


def _validate_state_root_layout(temp_root: Path, state_root: Path) -> None:
    try:
        root = Path(os.path.abspath(os.fspath(state_root)))
        temp = Path(os.path.abspath(os.fspath(temp_root)))
        candidate = root.parent.parent.parent
        if (
            root.name != "workflows"
            or root.parent.name != "CodexBridge"
            or root.parent.parent.name != "localappdata"
            or candidate.parent != temp
            or not candidate.name.casefold().startswith(readonly.TEMP_PREFIX)
        ):
            raise _blocked("candidate_match", "state_root_layout_mismatch")
    except readonly.InspectionFailure:
        raise
    except Exception as error:
        raise _blocked("candidate_match", "state_root_layout_invalid", error) from None


def load_review_data(
    approval_id: str,
    *,
    repository_root: Path | None = None,
    temp_root: Path | None = None,
    expected_provenance: tuple[int, int] = _EXPECTED_PROVENANCE,
    products: readonly.ReadOnlyProducts | None = None,
    audit_module: Any = readonly,
    graph_validator: tuple[Callable[..., Any], Callable[[object], bool]] | None = None,
) -> ReviewBundle:
    if _APPROVAL_ID.fullmatch(approval_id) is None:
        raise _blocked("marker_validation", "approval_id_invalid")
    repo = repository_root if repository_root is not None else audit_module.REPOSITORY_ROOT
    try:
        marker = audit_module._read_marker(repo, approval_id)
    except readonly.InspectionFailure:
        raise
    except Exception as error:
        raise _blocked("marker_validation", "marker_validation_failed", error) from None
    if (
        marker.approval_id != approval_id
        or tuple(run.name for run in marker.runs) != _EXPECTED_CASES
    ):
        raise _blocked("marker_validation", "marker_identity_invalid")

    try:
        effective_temp = temp_root if temp_root is not None else audit_module._windows_temp_root()
        match = audit_module.find_unique_state_root(effective_temp, marker)
        audit_module._validate_run_paths(match.state_root, marker)
    except readonly.InspectionFailure:
        raise
    except Exception as error:
        raise _blocked("candidate_match", "candidate_match_failed", error) from None

    _validate_state_root_layout(effective_temp, match.state_root)
    if (
        match.direct_candidate_count != expected_provenance[0]
        or match.readable_database_count != expected_provenance[1]
        or len(match.request_states) != 3
        or dict(match.request_states) != _EXPECTED_REQUEST_STATES
    ):
        raise _blocked("candidate_match", "provenance_mismatch")

    try:
        dependencies = products if products is not None else audit_module._load_products()
    except readonly.InspectionFailure:
        raise
    except Exception as error:
        raise _blocked("product_import", "readonly_projection_unavailable", error) from None
    validators = graph_validator if graph_validator is not None else _load_graph_validators()
    validate_snapshot, valid_node_id = validators

    validated: list[VisibleRun] = []
    for run in marker.runs:
        row = _call_product(
            "find_workflow", dependencies.find_workflow, match.state_root, run.workflow_id
        )
        if row is None:
            raise _blocked("find_workflow", "request_identity_mismatch")
        try:
            row_workflow_id = row.workflow_id
            row_run_id = row.run_id
            request_state = row.state
        except Exception as error:
            raise _blocked("find_workflow", "request_row_invalid", error) from None
        if (
            row_workflow_id != run.workflow_id
            or row_run_id != run.run_id
            or request_state != _EXPECTED_REQUEST_STATES[run.name]
        ):
            raise _blocked("find_workflow", "request_identity_mismatch")

        inspection = _call_product("inspect_run", dependencies.inspect_run, match.state_root, row)
        snapshot = _call_product(
            "snapshot",
            dependencies.snapshot_from_inspection,
            row,
            inspection,
        )
        task_payload = _call_product(
            "tasks_projection", dependencies.project_tasks, match.state_root, row, inspection
        )
        event_payload = _call_product(
            "events_projection",
            dependencies.project_events,
            match.state_root,
            row,
            inspection,
            after_id=0,
            limit=100,
        )
        state, workflow_id, detail, result = _validate_snapshot_fields(
            run, row, snapshot, inspection
        )
        expected_event_count = _EXPECTED_EVENT_COUNTS[run.name]
        expected_counts = _EXPECTED_GRAPH_COUNTS[run.name]
        checked_task = _validate_task_payload(
            task_payload,
            workflow_id=workflow_id,
            run_id=run.run_id,
            expected_status="succeeded",
            expected_counts=expected_counts,
            expected_latest_event_id=expected_event_count,
            validate_snapshot=validate_snapshot,
        )
        checked_events = _validate_event_payload(
            event_payload,
            workflow_id=workflow_id,
            run_id=run.run_id,
            expected_count=expected_event_count,
            valid_node_id=valid_node_id,
        )
        item = _run_item(detail)
        validated.append(
            VisibleRun(
                case=run.name,
                workflow_id=workflow_id,
                run_id=run.run_id,
                request_state=state,
                workflow_row=item,
                detail=detail,
                result=result,
                task_snapshot=checked_task,
                task_events=checked_events,
            )
        )

    if tuple(run.case for run in validated) != _EXPECTED_CASES:
        raise _blocked("safe_formatter", "run_set_incomplete")
    return ReviewBundle(
        runs=tuple(validated),
        direct_candidate_count=match.direct_candidate_count,
        readable_database_count=match.readable_database_count,
        exact_matches=1,
    )


def _synthetic_graph_payload(case: str) -> tuple[dict[str, Any], dict[str, Any]]:
    jobs = _EXPECTED_JOBS[case]
    workflow_id = f"sample-p3c-workflow-{case.lower()}-001"
    run_id = f"sample-p3c-run-{case.lower()}-001"

    def make_execution(index: int, *, job: bool = False) -> dict[str, Any]:
        execution: dict[str, Any] = {
            "execution_id": f"sample-p3c-execution-{case.lower()}-{index:02d}",
            "task_identity": f"sample-p3c-task-{case.lower()}-{index:02d}",
            "state": "succeeded",
            "started_at": "2026-11-17T00:00:00Z",
            "completed_at": "2026-11-17T00:00:01Z",
            "completion_observed": True,
            "observation_complete": True,
            "evidence_sources": ["node_events"],
            "evidence_conflict": False,
        }
        if job:
            execution.update(
                {
                    "job_id": f"sample-p3c-job-{case.lower()}-{index:02d}",
                    "thread_id": f"sample-p3c-thread-{case.lower()}-{index:02d}",
                    "evidence_sources": ["codex_job_ledger"],
                }
            )
        return execution

    def make_node(
        node_id: str, operation: str, order: int, executions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "node_id": node_id,
            "operation": operation,
            "parent_scope": "/do/0",
            "order": order,
            "state": "succeeded",
            "completion_observed": True,
            "executions": executions,
        }

    if case == "A":
        nodes = [make_node("/do/0/call", "call", 0, [make_execution(1, job=True)])]
        edges = [
            {"source": "@scope:/do/0:entry", "target": "/do/0/call", "kind": "scope_entry"},
            {"source": "/do/0/call", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
        ]
    elif case == "B":
        nodes = [
            make_node("/do/0/for", "for", 0, []),
            make_node(
                "/do/0/call",
                "call",
                1,
                [make_execution(index, job=True) for index in range(1, jobs + 1)],
            ),
        ]
        edges = [
            {"source": "@scope:/do/0:entry", "target": "/do/0/for", "kind": "scope_entry"},
            {"source": "/do/0/for", "target": "/do/0/call", "kind": "sequence"},
            {"source": "/do/0/call", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
        ]
    else:
        nodes = [
            make_node("/do/0/for-zero", "for", 0, []),
            make_node(
                "/do/0/for-many",
                "for",
                1,
                [make_execution(index) for index in range(1, 4)],
            ),
            make_node("/do/0/switch", "switch", 2, [make_execution(4)]),
            make_node(
                "/do/0/call",
                "call",
                3,
                [make_execution(index, job=True) for index in range(1, jobs + 1)],
            ),
        ]
        edges = [
            {"source": "@scope:/do/0:entry", "target": "/do/0/for-zero", "kind": "scope_entry"},
            {"source": "/do/0/for-zero", "target": "/do/0/for-many", "kind": "loop_body"},
            {"source": "/do/0/for-many", "target": "/do/0/for-many", "kind": "loop_repeat"},
            {"source": "/do/0/for-many", "target": "/do/0/switch", "kind": "loop_exit"},
            {"source": "/do/0/switch", "target": "/do/0/call", "kind": "sequence"},
            {"source": "/do/0/call", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
            {"source": "/do/0/switch", "target": "@scope:/do/0:exit", "kind": "fallthrough"},
            {"source": "/do/0/for-many", "target": "@scope:/do/0:exit", "kind": "default"},
            {"source": "/do/0/call", "target": "/do/0/call", "kind": "sequence"},
            {"source": "/do/0/for-zero", "target": "@scope:/do/0:exit", "kind": "scope_exit"},
        ]
    expected_events = _EXPECTED_EVENT_COUNTS[case]
    snapshot = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": "succeeded",
        "coverage": "complete",
        "latest_event_id": expected_events,
        "snapshot_version": "sha256:" + (case.lower() * 64)[:64],
        "truncated": False,
        "counts": {
            "nodes": _EXPECTED_GRAPH_COUNTS[case]["nodes"],
            "edges": _EXPECTED_GRAPH_COUNTS[case]["edges"],
            "executions": _EXPECTED_GRAPH_COUNTS[case]["executions"],
        },
        "nodes": nodes,
        "edges": edges,
    }
    events: list[dict[str, Any]] = []
    for event_id in range(1, expected_events + 1):
        event_type = "workflow_completed" if event_id == expected_events else "task_completed"
        events.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "node_id": None if event_type == "workflow_completed" else "/do/0/call-three",
                "occurred_at": f"2026-11-17T00:00:{event_id % 60:02d}Z",
                "source": "interpreter",
                "metadata": {},
            }
        )
    event_payload = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "events": events,
        "next_after_id": expected_events,
        "latest_event_id": expected_events,
        "has_more": False,
        "truncated": False,
        "coverage": "complete",
    }
    return snapshot, event_payload


def _synthetic_bundle() -> ReviewBundle:
    runs: list[VisibleRun] = []
    for case in _EXPECTED_CASES:
        jobs = _EXPECTED_JOBS[case]
        workflow_id = f"sample-p3c-workflow-{case.lower()}-001"
        run_id = f"sample-p3c-run-{case.lower()}-001"
        snapshot, events = _synthetic_graph_payload(case)
        public_jobs: list[dict[str, object]] = [
            {
                "job_id": f"sample-p3c-job-{case.lower()}-{index:02d}",
                "task_identity": f"sample-p3c-task-{case.lower()}-{index:02d}",
                "state": "SUCCEEDED",
                "thread_id": f"sample-p3c-thread-{case.lower()}-{index:02d}",
                "turn_id": None,
            }
            for index in range(1, jobs + 1)
        ]
        detail: dict[str, object] = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": "succeeded",
            "created_at": "2026-11-17T00:00:00Z",
            "updated_at": "2026-11-17T00:02:00Z",
            "worker_state": "completed",
            "terminal": True,
            "job_id": f"sample-p3c-job-{case.lower()}-01",
            "thread_id": f"sample-p3c-thread-{case.lower()}-01",
        }
        if jobs in {2, 3}:
            detail["max_live_codex_jobs"] = jobs
            detail["jobs"] = public_jobs
        runs.append(
            VisibleRun(
                case=case,
                workflow_id=workflow_id,
                run_id=run_id,
                request_state=_EXPECTED_REQUEST_STATES[case],
                workflow_row={
                    "workflow_id": workflow_id,
                    "status": "succeeded",
                    "created_at": detail["created_at"],
                    "updated_at": detail["updated_at"],
                },
                detail=detail,
                result={"status": "succeeded", "final_text": f"synthetic result {case}"},
                task_snapshot=snapshot,
                task_events=events,
            )
        )
    return ReviewBundle(tuple(runs), 8, 4, 1)


def _load_ui_types() -> tuple[type[Any], type[Any]]:
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from PySide6.QtWidgets import QApplication

            existing_application = QApplication.instance()
            from codex_bridge.console.workflows_dialog import WorkflowManagementDialog

            if QApplication.instance() is not existing_application:
                raise _blocked("prepare", "application_created")
    except Exception as error:
        if isinstance(error, readonly.InspectionFailure):
            raise
        raise _blocked("prepare", "qt_dependencies_unavailable", error) from None
    return QApplication, WorkflowManagementDialog


def prepare() -> dict[str, object]:
    """Check Qt types and synthetic DTO acceptance without touching live data."""
    before_modules = set(sys.modules)
    application_type: type[Any] | None = None
    application_before: Any = None
    try:
        application_type, _dialog_type = _load_ui_types()
        application_before = application_type.instance()
        validate_snapshot, valid_node_id = _load_graph_validators()
        bundle = _synthetic_bundle()
        for run in bundle.runs:
            _validate_task_payload(
                run.task_snapshot,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                expected_status="succeeded",
                expected_counts=_EXPECTED_GRAPH_COUNTS[run.case],
                expected_latest_event_id=_EXPECTED_EVENT_COUNTS[run.case],
                validate_snapshot=validate_snapshot,
            )
            _validate_event_payload(
                run.task_events,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                expected_count=_EXPECTED_EVENT_COUNTS[run.case],
                valid_node_id=valid_node_id,
            )
        forbidden_imported = any(
            name in sys.modules and name not in before_modules for name in _FORBIDDEN_MODULES
        )
        if forbidden_imported:
            raise _blocked("prepare", "forbidden_module_import")
        if application_type.instance() is not application_before:
            raise _blocked("prepare", "application_created")
    except readonly.InspectionFailure as error:
        return _failure_payload(error)
    except Exception as error:
        return _failure_payload(_blocked("prepare", "synthetic_check_failed", error))
    return {
        "mode": "prepare",
        "status": "PASS",
        "checks": [
            "synthetic_ABC_dtos",
            "qt_types_imported",
            "no_qapplication",
            "no_live_data_access",
        ],
    }


class ReadOnlyReviewHarness:
    """Connect the real management dialog only to one in-memory review bundle."""

    def __init__(self, dialog: Any, bundle: ReviewBundle, size: str = "regular") -> None:
        if size not in _SIZES:
            raise ValueError("unknown size")
        if tuple(run.case for run in bundle.runs) != _EXPECTED_CASES:
            raise _blocked("gui_setup", "review_bundle_invalid")
        self.dialog = dialog
        self._bundle = bundle
        self._by_id = {run.workflow_id: run for run in bundle.runs}
        self._rows = [dict(run.workflow_row) for run in bundle.runs]
        width, height = _SIZES[size]
        dialog.resize(width, height)
        dialog.setWindowTitle("隔離実データ・読み取り専用・Workflow起動不可")
        dialog.set_message(
            "隔離実データを読取専用で表示します。Workflowの開始・再実行はできません。"
        )
        dialog.set_bridge_ready(False)
        dialog.set_start_enabled(False)
        dialog.workflow_selected.connect(self.select_workflow)
        dialog.refresh_requested.connect(self.refresh)
        dialog.result_requested.connect(self.show_result)
        dialog.set_workflows(self._rows, selected_workflow_id=bundle.runs[0].workflow_id)
        selected = dialog.selected_workflow_id
        if selected is not None and dialog.graph_widget.snapshot_identity is None:
            self.select_workflow(selected)

    def _message(self, run: VisibleRun) -> str:
        return (
            f"隔離実データを読取専用表示中。{run.case}: 要求台帳 {run.request_state} / "
            f"製品公開状態 {run.detail['status']}。Workflow起動不可。"
        )

    def select_workflow(self, workflow_id: str) -> None:
        run = self._by_id.get(workflow_id)
        if run is None:
            return
        self.dialog.set_detail(run.detail)
        self.dialog.set_result(run.result)
        accepted_snapshot = self.dialog.graph_widget.set_snapshot(
            run.workflow_id, run.task_snapshot
        )
        accepted_events = self.dialog.graph_widget.set_events(run.task_events)
        self.dialog.set_message(self._message(run))
        self.dialog.set_bridge_ready(False)
        self.dialog.set_start_enabled(False)
        if not accepted_snapshot or not accepted_events:
            self.dialog.set_message("表示を中断しました。読み取り専用DTOを再確認してください。")

    def refresh(self) -> None:
        selected = self.dialog.selected_workflow_id
        self.dialog.set_workflows(self._rows, selected_workflow_id=selected)
        if selected is not None:
            self.select_workflow(selected)

    def show_result(self) -> None:
        selected = self.dialog.selected_workflow_id
        run = self._by_id.get(selected) if selected is not None else None
        if run is not None:
            self.dialog.set_result(run.result)
            self.dialog.result_tabs.setCurrentIndex(0)


def _create_dialog(
    application: Any,
    dialog_type: type[Any],
    bundle: ReviewBundle,
    size: str,
) -> tuple[Any, ReadOnlyReviewHarness]:
    if application is None:
        raise _blocked("gui_setup", "application_unavailable")
    dialog = dialog_type()
    harness = ReadOnlyReviewHarness(dialog, bundle, size)
    return dialog, harness


def _failure_payload(error: readonly.InspectionFailure) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "BLOCKED",
        "stage": error.stage,
        "code": error.code,
    }
    if error.exception_class is not None:
        payload["exception_class"] = error.exception_class
    return payload


def _emit(payload: Mapping[str, object]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")))


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(
        prog="python -m scripts.phase_c5_isolated_visible_review",
        description="Read-only visible review of completed isolated C5 Workflow data.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare", action="store_true", help="check synthetic DTO and Qt dependencies"
    )
    mode.add_argument("--show", action="store_true", help="show the read-only local review window")
    parser.add_argument("--approval-id")
    parser.add_argument("--size", choices=tuple(_SIZES), default="regular")
    return parser


def _show(approval_id: str, size: str) -> int:
    if size not in _SIZES:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "size_invalid"})
        return 2
    if sys.platform != "win32" or os.name != "nt":
        _emit({"status": "BLOCKED", "stage": "gui_gate", "code": "windows_native_required"})
        return 2
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        _emit({"status": "BLOCKED", "stage": "gui_gate", "code": "offscreen_not_visible"})
        return 2
    try:
        # Complete marker, provenance, product, graph and event validation before QApplication.
        bundle = load_review_data(approval_id)
        before_modules = set(sys.modules)
        application_type, dialog_type = _load_ui_types()
        forbidden_imported = any(
            name in sys.modules and name not in before_modules for name in _FORBIDDEN_MODULES
        )
        if forbidden_imported:
            raise _blocked("gui_setup", "forbidden_module_import")
        application = application_type.instance()
        if not isinstance(application, application_type):
            application = application_type(["phase-c5-isolated-visible-review"])
        application.setApplicationName("Phase C5 isolated read-only review")
        application.setQuitOnLastWindowClosed(True)
        dialog = dialog_type()
    except readonly.InspectionFailure as error:
        _emit(_failure_payload(error))
        return 2
    except Exception as error:
        _emit(_failure_payload(_blocked("gui_setup", "visible_review_start_failed", error)))
        return 2
    return _run_dialog_loop(application, dialog, bundle, size)


def _run_dialog_loop(
    application: Any,
    dialog: Any,
    bundle: ReviewBundle,
    size: str,
) -> int:
    """Keep the non-QObject signal receiver alive for the entire event loop."""
    harness = ReadOnlyReviewHarness(dialog, bundle, size)
    if harness.dialog is not dialog:
        raise _blocked("gui_setup", "harness_binding_invalid")
    dialog.show()
    try:
        return int(application.exec())
    finally:
        dialog.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        options = parser.parse_args(argv)
    except _ArgumentError:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "arguments_invalid"})
        return 2
    if not options.prepare and not options.show and options.approval_id is None:
        parser.print_help()
        return 0
    if options.prepare:
        if options.approval_id is not None:
            _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "unexpected_approval_id"})
            return 2
        payload = prepare()
        _emit(payload)
        return 0 if payload.get("status") == "PASS" else 2
    if not options.show or options.approval_id is None:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "show_arguments_required"})
        return 2
    if _APPROVAL_ID.fullmatch(options.approval_id) is None:
        _emit({"status": "BLOCKED", "stage": "cli_validate", "code": "approval_id_invalid"})
        return 2
    return _show(options.approval_id, options.size)


if __name__ == "__main__":
    raise SystemExit(main())
