from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .codex_job_adapter import CodexJobAdapter
from .graph import project_workflow_graph
from .service import (
    _MAX_RUN_FILE_BYTES,
    _MAX_WORKFLOW_YAML_BYTES,
    _canonical_json,
    _read_dbos_status,
    _read_regular_file,
    _RequestRow,
    _RunInspection,
    _snapshot_from_inspection,
    _strict_json,
    _validated_run_directory,
    strict_run_metadata_json,
    strict_verification_metadata_json,
)
from .strict_reader_gate import verify_strict_reserved_readonly

_JOURNAL_NAME = "node_events.sqlite"
_JOURNAL_SCHEMA_VERSION = 1
_MAX_JOURNAL_BYTES = 24 * 1024 * 1024
_MAX_JOURNAL_EVENTS = 60_000
_MAX_METADATA_BYTES = 1_024
_MAX_STATIC_NODES = 256
_MAX_EXECUTIONS = 512
_MAX_EDGES = 1_024
_MAX_TASK_RESPONSE_BYTES = 256 * 1024
_MAX_EVENT_RESPONSE_BYTES = 128 * 1024
_SQLITE_TIMEOUT_SECONDS = 0.1
_IDENTITY_PUNCTUATION = frozenset("_.:/~@-[]")
_GRAPH_ID_PUNCTUATION = frozenset("_/@:.~/-[]")
_TASK_NAME_PUNCTUATION = frozenset("_.~/-")
_SECRET_LIKE_RE = re.compile(
    r"(?:sk-(?:proj|live|test)-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})",
    re.IGNORECASE | re.ASCII,
)
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_EVENT_TYPES = frozenset(
    {
        "task_started",
        "task_completed",
        "task_failed",
        "scope_entered",
        "scope_exited",
        "iteration_started",
        "iteration_completed",
        "branch_selected",
        "workflow_completed",
    }
)
_OPERATIONS = frozenset({"call", "do", "for", "set", "switch"})
_BRANCH_CLASSES = frozenset({"continue", "exit", "jump"})
_FAILURE_PHASES = frozenset({"input", "operation", "output", "export"})
_JOB_STATES = frozenset({"PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING", "SUCCEEDED", "FAILED"})
_SUCCESS_DBOS_STATES = frozenset({"SUCCESS", "SUCCEEDED"})
_ACTIVE_WORKFLOW_STATES = frozenset({"queued", "starting", "running"})
_WORKFLOW_SUCCESS_STATES = frozenset({"succeeded"})
_TABLE_COLUMNS = (
    "event_id",
    "event_key",
    "workflow_id",
    "run_id",
    "schema_version",
    "node_id",
    "task_identity",
    "execution_id",
    "event_type",
    "occurred_at",
    "source",
    "job_id",
    "metadata_json",
)
_COLUMN_SIGNATURE = (
    ("event_id", "INTEGER", 0, 1),
    ("event_key", "TEXT", 1, 0),
    ("workflow_id", "TEXT", 1, 0),
    ("run_id", "TEXT", 1, 0),
    ("schema_version", "INTEGER", 1, 0),
    ("node_id", "TEXT", 0, 0),
    ("task_identity", "TEXT", 0, 0),
    ("execution_id", "TEXT", 0, 0),
    ("event_type", "TEXT", 1, 0),
    ("occurred_at", "TEXT", 1, 0),
    ("source", "TEXT", 1, 0),
    ("job_id", "TEXT", 0, 0),
    ("metadata_json", "TEXT", 1, 0),
)
_ALLOWED_METADATA = {
    "operation",
    "iteration_ordinal",
    "branch_class",
    "case_index",
    "failure_phase",
}


@dataclass(frozen=True, slots=True)
class _ProjectionIssue(Exception):
    reason: str


@dataclass(frozen=True, slots=True)
class _Event:
    event_id: int
    event_key: str
    node_id: str | None
    task_identity: str | None
    execution_id: str | None
    event_type: str
    occurred_at: str
    source: str
    metadata: dict[str, str | int]


@dataclass(frozen=True, slots=True)
class _Job:
    job_id: str
    task_identity: str
    state: str
    thread_id: str | None
    turn_id: str | None
    reconciliation: bool
    receipt_verified: bool


def project_workflow_tasks(
    state_root: Path,
    row: _RequestRow,
    inspection: _RunInspection,
    *,
    allowed_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    snapshot = _snapshot_from_inspection(row, inspection)
    base = {
        "schema_version": 1,
        "workflow_id": row.workflow_id,
        "run_id": row.run_id,
        "workflow_status": snapshot.status,
        "coverage": "unavailable",
        "latest_event_id": 0,
        "truncated": False,
        "nodes": [],
        "edges": [],
        "counts": {"nodes": 0, "edges": 0, "executions": 0},
    }
    try:
        run_dir, definition, max_jobs, strict_verification = _verified_run_source(
            state_root, row, allowed_roots=allowed_roots
        )
    except _ProjectionIssue as issue:
        base["coverage_reason"] = issue.reason
        base.update(
            strict_run_metadata_json(
                snapshot, reason_only=True, reason_code="run_integrity_invalid"
            )
        )
        return _attach_snapshot_version(base, snapshot, None, ())

    strict_v2 = strict_verification is not None
    if strict_verification is not None:
        projection_reason = (
            inspection._strict_metadata.reason_code
            if inspection._strict_metadata is not None
            and inspection._strict_metadata.reason_code is not None
            else "workflow_not_confirmed"
        )
        base.update(
            strict_verification_metadata_json(
                strict_verification,
                reason_code=projection_reason,
            )
        )
        pinned_definition = strict_verification._definition
        if pinned_definition is None or pinned_definition.contract is None:
            base["coverage"] = "unavailable"
            base["coverage_reason"] = "run_integrity_invalid"
            return _attach_snapshot_version(base, snapshot, None, ())
        strict_contract = pinned_definition.contract
    else:
        strict_contract = None

    try:
        graph = project_workflow_graph(
            definition,
            codex_job_live=True,
            max_live_codex_jobs=max_jobs,
            strict_contract=strict_contract,
        )
        static_nodes = graph["nodes"]
        static_edges = graph["edges"]
        _validate_static_graph(static_nodes, static_edges)
    except Exception:
        base["coverage"] = "partial"
        base["coverage_reason"] = "graph_invalid"
        return _attach_snapshot_version(base, snapshot, None, ())

    if strict_v2:
        return _project_strict_static_tasks(base, snapshot, static_nodes, static_edges)

    node_ids = {node["node_id"] for node in static_nodes}
    journal_reason: str | None = None
    events: list[_Event] = []
    latest_event_id = 0
    try:
        events, latest_event_id = _read_journal(run_dir, row, node_ids)
    except _ProjectionIssue as issue:
        journal_reason = issue.reason
    jobs: list[_Job] = []
    ledger_reason: str | None = None
    try:
        jobs = _read_jobs(run_dir, row.workflow_id)
    except _ProjectionIssue as issue:
        ledger_reason = issue.reason
    dbos_status: str | None = None
    dbos_path = run_dir / "dbos.sqlite"
    if dbos_path.exists() or dbos_path.is_symlink():
        try:
            if _is_reparse_point(dbos_path):
                raise _ProjectionIssue("ledger_unavailable")
            dbos_status = _read_dbos_status(dbos_path, row.workflow_id)
        except Exception:
            ledger_reason = ledger_reason or "ledger_unavailable"

    by_identity: dict[str, list[_Job]] = defaultdict(list)
    for ledger_job in jobs:
        by_identity[ledger_job.task_identity].append(ledger_job)
    call_node_ids = {
        node["node_id"]
        for node in static_nodes
        if node.get("operation") == "call"
        and isinstance(node.get("attributes"), dict)
        and node["attributes"].get("call") == "codex-job"
    }
    journal_call_task_identities = {
        event.task_identity
        for event in events
        if event.event_type == "task_started"
        and event.node_id in call_node_ids
        and event.task_identity is not None
    }
    if any(job.task_identity not in journal_call_task_identities for job in jobs):
        ledger_reason = ledger_reason or "evidence_conflict"
    event_groups: dict[tuple[str, str, str], list[_Event]] = defaultdict(list)
    groups_by_node: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for event in events:
        if event.event_type in {"task_started", "task_completed", "task_failed", "branch_selected"}:
            assert event.node_id is not None and event.execution_id is not None
            assert event.task_identity is not None
            group_key = (event.node_id, event.execution_id, event.task_identity)
            if group_key not in event_groups:
                groups_by_node[event.node_id].append(group_key)
            event_groups[group_key].append(event)

    workflow_status_success = _workflow_status_success(snapshot, events, dbos_status)
    conflicts = _event_projection_conflicts(
        events, static_nodes, require_completed_tasks=workflow_status_success
    )
    task_exec_refs: list[tuple[dict[str, Any], str, str, list[_Event]]] = []
    for node in static_nodes:
        for key in groups_by_node.get(node["node_id"], ()):
            _node_id, execution_id, task_identity = key
            rows = event_groups[key]
            if any(event.event_type == "task_started" for event in rows):
                task_exec_refs.append((node, execution_id, task_identity, rows))
    task_exec_refs.sort(
        key=lambda item: (
            item[0]["order"],
            _first_event_id(item[3]),
            item[0]["node_id"],
            item[2],
        )
    )
    observation_by_key: dict[tuple[str, str, str], bool] = {}
    for node, execution_id, task_identity, rows in task_exec_refs:
        key = (node["node_id"], execution_id, task_identity)
        observation_complete = _execution_observation_complete(
            node["operation"], node["node_id"], task_identity, rows, events
        )
        observation_by_key[key] = observation_complete
        starts = [event for event in rows if event.event_type == "task_started"]
        terminals = [
            event for event in rows if event.event_type in {"task_completed", "task_failed"}
        ]
        if len(starts) == 1 and len(terminals) == 1 and starts[0].event_id >= terminals[0].event_id:
            conflicts.add(key)
        if terminals and not observation_complete:
            conflicts.add(key)
    total_executions = len(task_exec_refs)
    exposed_execution_keys = {
        (node["node_id"], execution_id, identity)
        for node, execution_id, identity, _ in task_exec_refs[:_MAX_EXECUTIONS]
    }
    global_success = workflow_status_success
    coverage_complete = _complete_workflow_evidence(
        snapshot, events, static_nodes, jobs, dbos_status, conflicts
    )
    executions_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_states_by_node: dict[str, list[str]] = defaultdict(list)
    node_conflict: dict[str, bool] = defaultdict(bool)
    for node_id, _execution_id, _identity in conflicts:
        node_conflict[node_id] = True

    for node, execution_id, task_identity, rows in task_exec_refs:
        key = (node["node_id"], execution_id, task_identity)
        starts = [event for event in rows if event.event_type == "task_started"]
        completes = [event for event in rows if event.event_type == "task_completed"]
        failures = [event for event in rows if event.event_type == "task_failed"]
        branch_rows = [event for event in rows if event.event_type == "branch_selected"]
        job_matches = by_identity.get(task_identity, [])
        conflict = (
            key in conflicts
            or len(job_matches) > 1
            or len(starts) != 1
            or len(completes) + len(failures) > 1
            or (bool(completes) and bool(failures))
        )
        operation = node["operation"]
        if operation == "switch" and len(branch_rows) != 1 and (completes or failures):
            conflict = True
        matched_job = job_matches[0] if len(job_matches) == 1 else None
        if matched_job is not None and matched_job.state == "SUCCEEDED" and failures:
            conflict = True
        if matched_job is not None and matched_job.state == "FAILED" and completes:
            conflict = True
        if global_success and failures:
            conflict = True
        observation_complete = observation_by_key.get(key, False)

        completion_observed = bool(completes or failures)
        evidence_sources = ["node_events"]
        if conflict:
            state = "needs_reconciliation"
        elif matched_job is not None and (
            matched_job.state == "MAYBE_SENT" or matched_job.reconciliation
        ):
            state = "needs_reconciliation"
            evidence_sources.append("codex_job_ledger")
        elif failures:
            if matched_job is not None and matched_job.state == "SUCCEEDED" or global_success:
                state = "needs_reconciliation"
                conflict = True
            else:
                state = "failed"
        elif completes:
            if global_success:
                if operation == "call" and node.get("attributes", {}).get("call") == "codex-job":
                    if (
                        matched_job is None
                        or matched_job.state != "SUCCEEDED"
                        or not matched_job.receipt_verified
                        or not observation_complete
                    ):
                        state = "unknown"
                    else:
                        state = "succeeded"
                        evidence_sources.append("codex_job_ledger")
                elif matched_job is not None and (
                    matched_job.state != "SUCCEEDED" or not matched_job.receipt_verified
                ):
                    state = "unknown"
                elif not observation_complete:
                    state = "unknown"
                else:
                    state = "succeeded"
                evidence_sources.extend(["workflow_status", "worker_status", "dbos_status"])
            else:
                state = "unknown"
        elif starts:
            if snapshot.worker_state in {"completed", "failed"}:
                state = "unknown"
            elif snapshot.status == "running" or snapshot.worker_state == "running":
                state = "running"
                evidence_sources.extend(["workflow_status", "worker_status"])
            else:
                state = "unknown"
        elif (
            snapshot.status in _ACTIVE_WORKFLOW_STATES
            and journal_reason is None
            and ledger_reason is None
            and not conflicts
        ):
            state = "not_started"
        else:
            state = "unknown"
        if matched_job is not None:
            evidence_sources.append("codex_job_ledger")
        all_states_by_node[node["node_id"]].append(state)
        node_conflict[node["node_id"]] = node_conflict[node["node_id"]] or conflict
        if key not in exposed_execution_keys:
            continue
        start_event = starts[0] if starts else None
        terminal_event = completes or failures
        completed_event = terminal_event[0] if terminal_event else None
        public_execution: dict[str, Any] = {
            "execution_id": execution_id,
            "task_identity": task_identity,
            "state": state,
            "started_at": None if start_event is None else start_event.occurred_at,
            "completed_at": None if completed_event is None else completed_event.occurred_at,
            "completion_observed": completion_observed,
            "observation_complete": bool(observation_complete and not conflict),
            "evidence_sources": sorted(set(evidence_sources)),
            "evidence_conflict": bool(conflict),
        }
        if matched_job is not None:
            public_execution["job_id"] = matched_job.job_id
            if matched_job.thread_id is not None:
                public_execution["thread_id"] = matched_job.thread_id
            if matched_job.turn_id is not None:
                public_execution["turn_id"] = matched_job.turn_id
        executions_by_node[node["node_id"]].append(public_execution)

    nodes: list[dict[str, Any]] = []
    for node in static_nodes[:_MAX_STATIC_NODES]:
        node_id = node["node_id"]
        states = all_states_by_node[node_id]
        if not states:
            summary_state = (
                "needs_reconciliation"
                if node_conflict[node_id]
                else "not_started"
                if (
                    snapshot.status in _ACTIVE_WORKFLOW_STATES
                    and journal_reason is None
                    and ledger_reason is None
                    and not conflicts
                )
                else "unknown"
            )
        elif node_conflict[node_id] or "needs_reconciliation" in states:
            summary_state = "needs_reconciliation"
        elif all(state == "succeeded" for state in states):
            summary_state = "succeeded"
        elif all(state == "failed" for state in states):
            summary_state = "failed"
        elif "running" in states:
            summary_state = "running"
        else:
            summary_state = "unknown"
        node_executions = executions_by_node[node_id]
        nodes.append(
            {
                "node_id": node_id,
                "operation": node["operation"],
                "parent_scope": node["parent_scope"],
                "order": node["order"],
                "state": summary_state,
                "completion_observed": any(
                    execution["completion_observed"] for execution in node_executions
                )
                or any(
                    event.node_id == node_id
                    and event.event_type in {"task_completed", "task_failed"}
                    for event in events
                ),
                "executions": node_executions,
            }
        )

    included_node_ids = {node["node_id"] for node in nodes}
    safe_edges = [
        {"source": edge["source"], "target": edge["target"], "kind": edge["kind"]}
        for edge in static_edges[:_MAX_EDGES]
        if _edge_is_visible(edge, included_node_ids)
    ]
    counts = {
        "nodes": len(static_nodes),
        "edges": len(static_edges),
        "executions": total_executions,
    }
    truncated = (
        len(static_nodes) > _MAX_STATIC_NODES
        or len(static_edges) > _MAX_EDGES
        or total_executions > _MAX_EXECUTIONS
    )
    coverage = "complete"
    coverage_reason: str | None = None
    if journal_reason is not None:
        coverage, coverage_reason = "unavailable", journal_reason
    elif ledger_reason is not None:
        coverage, coverage_reason = "partial", ledger_reason
    elif conflicts:
        coverage, coverage_reason = "partial", "evidence_conflict"
    elif _workflow_marker_reason(events) == "evidence_conflict":
        coverage, coverage_reason = "partial", "evidence_conflict"
    elif truncated:
        coverage, coverage_reason = "partial", "truncated"
    elif not coverage_complete:
        coverage, coverage_reason = "partial", "workflow_not_confirmed"

    result = {
        **base,
        "coverage": coverage,
        "latest_event_id": latest_event_id,
        "truncated": truncated,
        "nodes": nodes,
        "edges": safe_edges,
        "counts": counts,
    }
    if coverage_reason is not None:
        result["coverage_reason"] = coverage_reason
    return _attach_snapshot_version(_fit_task_response(result), snapshot, dbos_status, jobs)


def _project_strict_static_tasks(
    base: dict[str, Any],
    snapshot: Any,
    static_nodes: list[dict[str, Any]],
    static_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    nodes = [
        {
            "node_id": node["node_id"],
            "operation": node["operation"],
            "parent_scope": node["parent_scope"],
            "order": node["order"],
            "state": "unknown",
            "completion_observed": False,
            "executions": [],
        }
        for node in static_nodes[:_MAX_STATIC_NODES]
    ]
    included_node_ids = {node["node_id"] for node in nodes}
    edges = [
        {"source": edge["source"], "target": edge["target"], "kind": edge["kind"]}
        for edge in static_edges[:_MAX_EDGES]
        if _edge_is_visible(edge, included_node_ids)
    ]
    result = {
        **base,
        "coverage": "partial",
        "coverage_reason": "workflow_not_confirmed",
        "latest_event_id": 0,
        "truncated": len(static_nodes) > _MAX_STATIC_NODES or len(static_edges) > _MAX_EDGES,
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "nodes": len(static_nodes),
            "edges": len(static_edges),
            "executions": 0,
        },
    }
    fitted = _fit_task_response(result)
    fitted["coverage"] = "partial"
    fitted["coverage_reason"] = "workflow_not_confirmed"
    return _attach_snapshot_version(fitted, snapshot, None, ())


def project_workflow_task_events(
    state_root: Path,
    row: _RequestRow,
    inspection: _RunInspection,
    *,
    after_id: int,
    limit: int,
    allowed_roots: tuple[str, ...] = (),
) -> dict[str, Any]:
    if type(after_id) is not int or not 0 <= after_id <= 2**63 - 1:
        raise ValueError("after_id is invalid")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit is invalid")
    base: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": row.workflow_id,
        "run_id": row.run_id,
        "events": [],
        "next_after_id": after_id,
        "has_more": False,
        "truncated": False,
        "latest_event_id": 0,
        "coverage": "unavailable",
    }
    try:
        run_dir, definition, max_jobs, strict_verification = _verified_run_source(
            state_root, row, allowed_roots=allowed_roots
        )
        if strict_verification is not None:
            projection_reason = (
                inspection._strict_metadata.reason_code
                if inspection._strict_metadata is not None
                and inspection._strict_metadata.reason_code is not None
                else "workflow_not_confirmed"
            )
            base.update(
                strict_verification_metadata_json(
                    strict_verification,
                    reason_code=projection_reason,
                )
            )
            pinned_definition = strict_verification._definition
            if pinned_definition is None or pinned_definition.contract is None:
                raise _ProjectionIssue("run_integrity_invalid")
            strict_contract = pinned_definition.contract
        else:
            strict_contract = None
        graph = project_workflow_graph(
            definition,
            codex_job_live=True,
            max_live_codex_jobs=max_jobs,
            strict_contract=strict_contract,
        )
        nodes = graph["nodes"]
        edges = graph["edges"]
        _validate_static_graph(nodes, edges)
        if strict_verification is not None:
            base["coverage"] = "partial"
            base["coverage_reason"] = "workflow_not_confirmed"
            return base
        events, latest = _read_journal(run_dir, row, {node["node_id"] for node in nodes})
    except _ProjectionIssue as issue:
        base["coverage_reason"] = issue.reason
        base.update(
            strict_run_metadata_json(
                _snapshot_from_inspection(row, inspection),
                reason_only=True,
                reason_code="run_integrity_invalid",
            )
        )
        return base
    except Exception:
        base["coverage"] = "partial"
        base["coverage_reason"] = "graph_invalid"
        return base

    snapshot = _snapshot_from_inspection(row, inspection)
    jobs: list[_Job] = []
    ledger_reason: str | None = None
    try:
        jobs = _read_jobs(run_dir, row.workflow_id)
    except _ProjectionIssue as issue:
        ledger_reason = issue.reason
    dbos_status: str | None = None
    dbos_path = run_dir / "dbos.sqlite"
    if dbos_path.exists() or dbos_path.is_symlink():
        try:
            if _is_reparse_point(dbos_path):
                raise _ProjectionIssue("ledger_unavailable")
            dbos_status = _read_dbos_status(dbos_path, row.workflow_id)
        except Exception:
            ledger_reason = ledger_reason or "ledger_unavailable"

    workflow_status_success = _workflow_status_success(snapshot, events, dbos_status)
    conflicts = _event_projection_conflicts(
        events, nodes, require_completed_tasks=workflow_status_success
    )
    call_node_ids = {
        node["node_id"]
        for node in nodes
        if node.get("operation") == "call"
        and isinstance(node.get("attributes"), dict)
        and node["attributes"].get("call") == "codex-job"
    }
    identities = {
        event.task_identity
        for event in events
        if event.event_type == "task_started"
        and event.node_id in call_node_ids
        and event.task_identity is not None
    }
    if any(job.task_identity not in identities for job in jobs):
        ledger_reason = ledger_reason or "evidence_conflict"
    workflow_confirmed = _complete_workflow_evidence(
        snapshot, events, nodes, jobs, dbos_status, conflicts
    )
    if ledger_reason is not None:
        base["coverage"] = "partial"
        base["coverage_reason"] = ledger_reason
    elif conflicts:
        base["coverage"] = "partial"
        base["coverage_reason"] = "evidence_conflict"
    elif _workflow_marker_reason(events) == "evidence_conflict":
        base["coverage"] = "partial"
        base["coverage_reason"] = "evidence_conflict"
    elif not workflow_confirmed:
        base["coverage"] = "partial"
        base["coverage_reason"] = "workflow_not_confirmed"
    else:
        base["coverage"] = "complete"

    page = [event for event in events if event.event_id > after_id][:limit]
    public_events = [_public_event(event) for event in page]
    result = {
        **base,
        "events": public_events,
        "latest_event_id": latest,
        "next_after_id": page[-1].event_id if page else after_id,
        "has_more": (page[-1].event_id if page else after_id) < latest,
    }
    while len(_json_bytes(result)) > _MAX_EVENT_RESPONSE_BYTES and result["events"]:
        result["events"].pop()
        result["next_after_id"] = result["events"][-1]["event_id"] if result["events"] else after_id
        result["has_more"] = result["next_after_id"] < latest
        result["truncated"] = True
        result["coverage"] = "partial"
        result["coverage_reason"] = "truncated"
    return result


def _verified_run_source(
    state_root: Path,
    row: _RequestRow,
    *,
    allowed_roots: tuple[str, ...] = (),
) -> tuple[Path, dict[str, Any], int, Any | None]:
    try:
        root = Path(state_root)
        if _is_reparse_point(root):
            raise _ProjectionIssue("run_integrity_invalid")
        run_path = root / row.run_id
        if _is_reparse_point(run_path):
            raise _ProjectionIssue("run_integrity_invalid")
        run_dir = _validated_run_directory(root, row.run_id)
        manifest_path = run_dir / "manifest.json"
        workflow_path = run_dir / "workflow.yaml"
        input_path = run_dir / "input.json"
        if _is_reparse_point(manifest_path):
            raise _ProjectionIssue("run_integrity_invalid")
        manifest_bytes = _read_regular_file(manifest_path, limit=32 * 1024)
        strict_verification = verify_strict_reserved_readonly(
            root,
            row.run_id,
            manifest_bytes,
            expected_workflow_id=row.workflow_id,
            expected_request_digest=row.request_digest,
            allowed_roots=allowed_roots,
        )
        if strict_verification is not None:
            pinned_definition = strict_verification._definition
            if pinned_definition is None:
                raise _ProjectionIssue("run_integrity_invalid")
            return (
                run_dir,
                pinned_definition.definition,
                strict_verification.manifest.max_live_codex_jobs,
                strict_verification,
            )
        for path in (workflow_path, input_path):
            if _is_reparse_point(path):
                raise _ProjectionIssue("run_integrity_invalid")
        manifest = _strict_json(manifest_bytes)
        if not isinstance(manifest, dict):
            raise _ProjectionIssue("run_integrity_invalid")
        if (
            manifest.get("workflow_id") != row.workflow_id
            or manifest.get("run_id") != row.run_id
            or manifest.get("request_digest") != row.request_digest
            or manifest.get("mode") != "codex-job-live"
            or manifest.get("workflow_source") != "ui-api"
            or manifest.get("max_tasks") != 10_000
        ):
            raise _ProjectionIssue("run_integrity_invalid")
        control = manifest.get("control")
        base_control_keys = {
            "codex_job_live",
            "codex_bridge_port",
            "codex_job_max_polls",
            "codex_job_poll_interval_seconds",
        }
        if not isinstance(control, dict):
            raise _ProjectionIssue("run_integrity_invalid")
        max_jobs = 1
        if set(control) == base_control_keys | {"max_live_codex_jobs"}:
            if type(control.get("max_live_codex_jobs")) is not int:
                raise _ProjectionIssue("run_integrity_invalid")
            max_jobs = control["max_live_codex_jobs"]
        elif set(control) != base_control_keys:
            raise _ProjectionIssue("run_integrity_invalid")
        bridge_port = control.get("codex_bridge_port")
        if (
            max_jobs not in {1, 2, 3}
            or control.get("codex_job_live") is not True
            or type(bridge_port) is not int
            or not 1 <= bridge_port <= 65535
            or manifest.get("codex_bridge_port") != bridge_port
            or control.get("codex_job_max_polls") != 600
            or control.get("codex_job_poll_interval_seconds") != 0.2
            or (max_jobs == 1 and "max_live_codex_jobs" in control)
        ):
            raise _ProjectionIssue("run_integrity_invalid")
        definition_text = _read_regular_file(
            workflow_path,
            limit=_MAX_WORKFLOW_YAML_BYTES,
        ).decode("utf-8")
        input_data = _strict_json(_read_regular_file(input_path, limit=_MAX_RUN_FILE_BYTES))
        definition = _strict_json(definition_text.encode("utf-8"))
        workspace = manifest.get("workspace_root")
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise _ProjectionIssue("run_integrity_invalid")
        digest_payload: dict[str, Any] = {
            "workflow": definition,
            "input": input_data,
            "workspace_root": workspace,
        }
        if max_jobs in {2, 3}:
            digest_payload["max_live_codex_jobs"] = max_jobs
        if (
            hashlib.sha256(_canonical_json(digest_payload).encode("utf-8")).hexdigest()
            != row.request_digest
            or hashlib.sha256(definition_text.encode("utf-8")).hexdigest()
            != manifest.get("definition_sha256")
            or hashlib.sha256(_canonical_json(input_data).encode("utf-8")).hexdigest()
            != manifest.get("input_sha256")
        ):
            raise _ProjectionIssue("run_integrity_invalid")
        from .interpreter import validate_workflow

        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=max_jobs)
        if max_jobs == 3:
            from .interpreter import (
                three_live_codex_job_input_kind,
                validate_three_live_codex_job_input,
            )

            validate_three_live_codex_job_input(
                input_data,
                input_kind=three_live_codex_job_input_kind(definition),
            )
        return run_dir, definition, max_jobs, None
    except _ProjectionIssue:
        raise
    except Exception:
        raise _ProjectionIssue("run_integrity_invalid") from None


def _validate_static_graph(nodes: Any, edges: Any) -> None:
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("workflow graph is invalid")
    if len(nodes) > 10_000 or len(edges) > 40_000:
        raise ValueError("workflow graph exceeds the source bound")
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("workflow graph node is invalid")
        node_id = node.get("node_id")
        task_name = node.get("task_name")
        if (
            not isinstance(node_id, str)
            or not _valid_unicode_identifier(
                node_id, max_length=2_048, punctuation=_GRAPH_ID_PUNCTUATION
            )
            or _has_graph_traversal_segment(node_id)
            or not isinstance(task_name, str)
            or not _valid_task_name(task_name)
            or node_id in seen
            or node.get("operation") not in _OPERATIONS
            or not isinstance(node.get("parent_scope"), str)
            or not _valid_unicode_identifier(
                node["parent_scope"], max_length=2_048, punctuation=_GRAPH_ID_PUNCTUATION
            )
            or _has_graph_traversal_segment(node["parent_scope"])
            or type(node.get("order")) is not int
            or node["order"] < 0
        ):
            raise ValueError("workflow graph node identity is invalid")
        seen.add(node_id)
    for edge in edges:
        if (
            not isinstance(edge, dict)
            or not isinstance(edge.get("source"), str)
            or not isinstance(edge.get("target"), str)
            or not isinstance(edge.get("kind"), str)
            or not _valid_unicode_identifier(
                edge["source"], max_length=2_048, punctuation=_GRAPH_ID_PUNCTUATION
            )
            or not _valid_unicode_identifier(
                edge["target"], max_length=2_048, punctuation=_GRAPH_ID_PUNCTUATION
            )
            or _has_graph_traversal_segment(edge["source"])
            or _has_graph_traversal_segment(edge["target"])
            or not re.fullmatch(r"[a-z_]{1,32}", edge["kind"], re.ASCII)
        ):
            raise ValueError("workflow graph edge identity is invalid")


def _read_journal(
    run_dir: Path,
    row: _RequestRow,
    static_node_ids: set[str],
) -> tuple[list[_Event], int]:
    database = run_dir / _JOURNAL_NAME
    if _is_reparse_point(run_dir) or _is_reparse_point(database):
        raise _ProjectionIssue("journal_unsafe_path")
    if not database.exists():
        raise _ProjectionIssue("journal_missing")
    try:
        file_stat = database.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise _ProjectionIssue("journal_unsafe_path")
        if file_stat.st_size > _MAX_JOURNAL_BYTES:
            raise _ProjectionIssue("journal_too_large")
        connection = _open_readonly(database)
    except _ProjectionIssue:
        raise
    except sqlite3.Error as error:
        raise _ProjectionIssue(_sqlite_reason(error)) from None
    except OSError:
        raise _ProjectionIssue("journal_unavailable") from None
    try:
        connection.execute("PRAGMA query_only=ON")
        _validate_journal_schema(connection)
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise _ProjectionIssue("journal_corrupt")
        latest_row = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM node_events"
        ).fetchone()
        latest = int(latest_row[0]) if latest_row is not None else 0
        raw_rows = connection.execute(
            "SELECT event_id, event_key, workflow_id, run_id, schema_version, node_id, "
            "task_identity, execution_id, event_type, occurred_at, source, job_id, metadata_json "
            "FROM node_events ORDER BY event_id LIMIT ?",
            (_MAX_JOURNAL_EVENTS + 1,),
        ).fetchall()
        if len(raw_rows) > _MAX_JOURNAL_EVENTS:
            raise _ProjectionIssue("journal_event_limit")
        events = [_event_from_row(raw, row, static_node_ids) for raw in raw_rows]
        ids = [event.event_id for event in events]
        if ids != sorted(set(ids)) or (ids and ids[-1] != latest):
            raise _ProjectionIssue("evidence_conflict")
        if latest != len(ids) or (ids and ids[0] != 1):
            raise _ProjectionIssue("evidence_conflict")
        return events, latest
    except _ProjectionIssue:
        raise
    except sqlite3.Error as error:
        raise _ProjectionIssue(_sqlite_reason(error)) from None
    except Exception:
        raise _ProjectionIssue("journal_invalid") from None
    finally:
        connection.close()


def _open_readonly(database: Path) -> sqlite3.Connection:
    uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=_SQLITE_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=100")
    return connection


def _validate_journal_schema(connection: sqlite3.Connection) -> None:
    objects = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    if objects != {("node_events", "table"), ("idx_node_events_run_order", "index")}:
        raise _ProjectionIssue("journal_schema_invalid")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != _JOURNAL_SCHEMA_VERSION:
        raise _ProjectionIssue("journal_schema_invalid")
    mode = connection.execute("PRAGMA journal_mode").fetchone()
    if mode is None or str(mode[0]).casefold() != "delete":
        raise _ProjectionIssue("journal_schema_invalid")
    signature = tuple(
        (str(item[1]), str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in connection.execute("PRAGMA table_info(node_events)")
    )
    if tuple(item[0] for item in signature) != _TABLE_COLUMNS or signature != _COLUMN_SIGNATURE:
        raise _ProjectionIssue("journal_schema_invalid")
    has_unique_event_key = False
    has_run_order = False
    for index in connection.execute("PRAGMA index_list(node_events)"):
        name = str(index[1])
        quoted = '"' + name.replace('"', '""') + '"'
        columns = tuple(
            str(item[2])
            for item in connection.execute(f"PRAGMA index_info({quoted})")
            if item[2] is not None
        )
        if bool(index[2]) and columns == ("event_key",):
            has_unique_event_key = True
        if name == "idx_node_events_run_order" and columns == ("run_id", "event_id"):
            has_run_order = True
    if not has_unique_event_key or not has_run_order:
        raise _ProjectionIssue("journal_schema_invalid")


def _event_from_row(raw: sqlite3.Row, row: _RequestRow, static_node_ids: set[str]) -> _Event:
    event_id = raw["event_id"]
    event_key = raw["event_key"]
    workflow_id = raw["workflow_id"]
    run_id = raw["run_id"]
    schema_version = raw["schema_version"]
    node_id = raw["node_id"]
    task_identity = raw["task_identity"]
    execution_id = raw["execution_id"]
    event_type = raw["event_type"]
    occurred_at = raw["occurred_at"]
    source = raw["source"]
    if (
        type(event_id) is not int
        or event_id < 1
        or not isinstance(event_key, str)
        or _HEX_DIGEST_RE.fullmatch(event_key) is None
        or workflow_id != row.workflow_id
        or run_id != row.run_id
        or type(schema_version) is not int
        or schema_version != _JOURNAL_SCHEMA_VERSION
        or event_type not in _EVENT_TYPES
        or source != "interpreter"
        or raw["job_id"] is not None
    ):
        raise _ProjectionIssue("journal_invalid")
    if event_type == "workflow_completed":
        if any(value is not None for value in (node_id, task_identity, execution_id)):
            raise _ProjectionIssue("journal_invalid")
    else:
        if (
            not isinstance(node_id, str)
            or node_id not in static_node_ids
            or not isinstance(task_identity, str)
            or not _is_task_identity(task_identity)
            or not isinstance(execution_id, str)
            or _HEX_DIGEST_RE.fullmatch(execution_id) is None
        ):
            raise _ProjectionIssue("journal_invalid")
        expected_execution_id = hashlib.sha256(
            f"codexbridge.node-event.execution.v1\0{row.workflow_id}\0{task_identity}".encode()
        ).hexdigest()
        if execution_id != expected_execution_id:
            raise _ProjectionIssue("journal_invalid")
    if not isinstance(occurred_at, str) or not _is_utc_timestamp(occurred_at):
        raise _ProjectionIssue("journal_invalid")
    metadata = _parse_metadata(raw["metadata_json"])
    _validate_event_metadata(event_type, metadata)
    return _Event(
        event_id,
        event_key,
        node_id,
        task_identity,
        execution_id,
        event_type,
        occurred_at,
        source,
        metadata,
    )


def _parse_metadata(value: Any) -> dict[str, str | int]:
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8", errors="strict")) > _MAX_METADATA_BYTES
    ):
        raise _ProjectionIssue("journal_invalid")

    def pairs_to_dict(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, child in pairs:
            if key in parsed:
                raise ValueError("duplicate metadata key")
            parsed[key] = child
        return parsed

    try:
        result = json.loads(value, object_pairs_hook=pairs_to_dict)
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError):
        raise _ProjectionIssue("journal_invalid") from None
    if not isinstance(result, dict) or set(result) - _ALLOWED_METADATA:
        raise _ProjectionIssue("journal_invalid")
    if any(
        not isinstance(key, str) or type(child) not in {str, int} for key, child in result.items()
    ):
        raise _ProjectionIssue("journal_invalid")
    return result


def _validate_event_metadata(event_type: str, metadata: dict[str, str | int]) -> None:
    operation = metadata.get("operation")
    if operation is not None and operation not in _OPERATIONS:
        raise _ProjectionIssue("journal_invalid")
    ordinal = metadata.get("iteration_ordinal")
    if ordinal is not None and (type(ordinal) is not int or not 0 <= ordinal <= 1_000_000):
        raise _ProjectionIssue("journal_invalid")
    branch_class = metadata.get("branch_class")
    if branch_class is not None and branch_class not in _BRANCH_CLASSES:
        raise _ProjectionIssue("journal_invalid")
    case_index = metadata.get("case_index")
    if case_index is not None and (type(case_index) is not int or not 0 <= case_index <= 10_000):
        raise _ProjectionIssue("journal_invalid")
    failure_phase = metadata.get("failure_phase")
    if failure_phase is not None and failure_phase not in _FAILURE_PHASES:
        raise _ProjectionIssue("journal_invalid")
    if event_type in {"task_started", "task_completed", "task_failed", "branch_selected"}:
        if operation is None:
            raise _ProjectionIssue("journal_invalid")
    if event_type == "task_failed" and failure_phase is None:
        raise _ProjectionIssue("journal_invalid")
    if event_type == "branch_selected" and (operation != "switch" or branch_class is None):
        raise _ProjectionIssue("journal_invalid")
    if event_type in {"iteration_started", "iteration_completed"} and (
        operation != "for" or ordinal is None
    ):
        raise _ProjectionIssue("journal_invalid")
    if event_type in {"scope_entered", "scope_exited"} and operation is None and ordinal is None:
        raise _ProjectionIssue("journal_invalid")
    if event_type == "workflow_completed" and metadata:
        raise _ProjectionIssue("journal_invalid")


def _read_jobs(run_dir: Path, workflow_id: str) -> list[_Job]:
    database = run_dir / "codex_jobs.sqlite"
    if not database.exists() and not database.is_symlink():
        return []
    if _is_reparse_point(database):
        raise _ProjectionIssue("ledger_unavailable")
    try:
        if not database.is_file() or database.stat().st_size > 8 * _MAX_RUN_FILE_BYTES:
            raise _ProjectionIssue("ledger_unavailable")
        connection = _open_readonly(database)
    except _ProjectionIssue:
        raise
    except Exception:
        raise _ProjectionIssue("ledger_unavailable") from None
    try:
        connection.execute("PRAGMA query_only=ON")
        adapter = object.__new__(CodexJobAdapter)
        version = adapter._detect_schema_version(connection)
        adapter._validate_database_schema(connection, version)
        if version not in {1, 2}:
            raise _ProjectionIssue("ledger_unavailable")
        rows = connection.execute(
            "SELECT job_id, workflow_id, task_identity, state, thread_id, turn_id, "
            "remote_state, receipt_json "
            "FROM jobs WHERE workflow_id = ? ORDER BY created_at ASC, job_id ASC LIMIT 4",
            (workflow_id,),
        ).fetchall()
        markers = {
            str(item[0])
            for item in connection.execute(
                "SELECT job_id FROM job_reconciliation LIMIT 4"
            ).fetchall()
        }
        if len(rows) > 3:
            raise _ProjectionIssue("ledger_unavailable")
        jobs: list[_Job] = []
        job_ids: set[str] = set()
        identities: set[str] = set()
        for item in rows:
            job_id = item["job_id"]
            task_identity = item["task_identity"]
            state = item["state"]
            thread_id = item["thread_id"]
            turn_id = item["turn_id"]
            receipt_verified = _job_receipt_verified(
                state,
                item["remote_state"],
                item["receipt_json"],
            )
            if (
                item["workflow_id"] != workflow_id
                or not CodexJobAdapter._valid_persisted_id(job_id)
                or not isinstance(task_identity, str)
                or not _is_task_identity(task_identity)
                or state not in _JOB_STATES
                or job_id in job_ids
                or task_identity in identities
            ):
                raise _ProjectionIssue("ledger_unavailable")
            if state in {"PREPARED", "MAYBE_SENT"}:
                if thread_id is not None or turn_id is not None:
                    raise _ProjectionIssue("ledger_unavailable")
            elif not CodexJobAdapter._valid_persisted_id(
                thread_id
            ) or not CodexJobAdapter._valid_persisted_id(turn_id):
                raise _ProjectionIssue("ledger_unavailable")
            job_ids.add(job_id)
            identities.add(task_identity)
            jobs.append(
                _Job(
                    job_id=job_id,
                    task_identity=task_identity,
                    state=state,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    reconciliation=job_id in markers,
                    receipt_verified=receipt_verified,
                )
            )
        if not markers <= job_ids:
            raise _ProjectionIssue("ledger_unavailable")
        return jobs
    except _ProjectionIssue:
        raise
    except Exception:
        raise _ProjectionIssue("ledger_unavailable") from None
    finally:
        connection.close()


def _validate_event_relationships(
    events: list[_Event],
) -> tuple[set[tuple[str, str, str]], dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    grouped: dict[str, list[_Event]] = defaultdict(list)
    pair_counts: Counter[tuple[str, str]] = Counter()
    conflicts: set[tuple[str, str, str]] = set()
    event_key_counts = Counter(event.event_key for event in events)
    if any(count != 1 for count in event_key_counts.values()):
        for event in events:
            if event.execution_id and event.node_id and event.task_identity:
                conflicts.add((event.node_id, event.execution_id, event.task_identity))
    for event in events:
        if event.execution_id is None:
            continue
        grouped[event.execution_id].append(event)
        if event.node_id is not None:
            pair_counts[(event.node_id, event.event_type)] += 1
    for execution_rows in grouped.values():
        execution_rows.sort(key=lambda event: event.event_id)
        identities = {
            (event.node_id, event.task_identity)
            for event in execution_rows
            if event.node_id is not None and event.task_identity is not None
        }
        types = Counter(event.event_type for event in execution_rows)
        if len(identities) > 1 or any(count > 1 for count in types.values()):
            conflicts.update(
                (event.node_id, event.execution_id, event.task_identity)
                for event in execution_rows
                if event.node_id and event.execution_id and event.task_identity
            )
        timestamps = [datetime.fromisoformat(event.occurred_at) for event in execution_rows]
        if timestamps != sorted(timestamps):
            conflicts.update(
                (event.node_id, event.execution_id, event.task_identity)
                for event in execution_rows
                if event.node_id and event.execution_id and event.task_identity
            )
        task_counts = Counter(event.event_type for event in execution_rows)
        if (
            any(
                event.event_type in {"task_completed", "task_failed", "branch_selected"}
                for event in execution_rows
            )
            and task_counts["task_started"] != 1
        ):
            conflicts.update(
                (event.node_id, event.execution_id, event.task_identity)
                for event in execution_rows
                if event.node_id and event.execution_id and event.task_identity
            )
        if task_counts["task_completed"] + task_counts["task_failed"] > 1:
            conflicts.update(
                (event.node_id, event.execution_id, event.task_identity)
                for event in execution_rows
                if event.node_id and event.execution_id and event.task_identity
            )
        if task_counts["branch_selected"] and not task_counts["task_completed"]:
            conflicts.update(
                (event.node_id, event.execution_id, event.task_identity)
                for event in execution_rows
                if event.node_id and event.execution_id and event.task_identity
            )
    scope_pair_map: dict[str, tuple[int, int]] = {}
    iteration_pair_map: dict[str, tuple[int, int]] = {}
    for node_id, event_type in pair_counts:
        if event_type not in {
            "scope_entered",
            "scope_exited",
            "iteration_started",
            "iteration_completed",
        }:
            continue
        scope_pair_map[node_id] = (
            pair_counts[(node_id, "scope_entered")],
            pair_counts[(node_id, "scope_exited")],
        )
        iteration_pair_map[node_id] = (
            pair_counts[(node_id, "iteration_started")],
            pair_counts[(node_id, "iteration_completed")],
        )
    for execution_rows in grouped.values():
        if not execution_rows:
            continue
        event = execution_rows[0]
        if event.node_id is None or event.execution_id is None or event.task_identity is None:
            continue
        types = Counter(item.event_type for item in execution_rows)
        if (
            types["scope_entered"] != types["scope_exited"]
            or types["iteration_started"] != types["iteration_completed"]
        ):
            conflicts.add((event.node_id, event.execution_id, event.task_identity))
    for node_id, (entered, exited) in scope_pair_map.items():
        if entered != exited:
            for event in events:
                if event.node_id == node_id and event.event_type in {
                    "task_started",
                    "task_completed",
                }:
                    assert event.execution_id is not None and event.task_identity is not None
                    conflicts.add((node_id, event.execution_id, event.task_identity))
    for node_id, (started, completed) in iteration_pair_map.items():
        if started != completed:
            for event in events:
                if event.node_id == node_id and event.event_type in {
                    "task_started",
                    "task_completed",
                }:
                    assert event.execution_id is not None and event.task_identity is not None
                    conflicts.add((node_id, event.execution_id, event.task_identity))
    return conflicts, scope_pair_map, iteration_pair_map


def _event_projection_conflicts(
    events: list[_Event],
    static_nodes: list[dict[str, Any]],
    *,
    require_completed_tasks: bool = False,
) -> set[tuple[str, str, str]]:
    conflicts, _scope_pairs, _iteration_pairs = _validate_event_relationships(events)
    operation_by_node = {node["node_id"]: node["operation"] for node in static_nodes}
    grouped: dict[tuple[str, str, str], list[_Event]] = defaultdict(list)
    task_operations: dict[tuple[str, str], str] = {}
    for event in events:
        if event.event_type not in {
            "task_started",
            "task_completed",
            "task_failed",
            "branch_selected",
        }:
            continue
        assert event.node_id is not None
        assert event.execution_id is not None
        assert event.task_identity is not None
        key = (event.node_id, event.execution_id, event.task_identity)
        grouped[key].append(event)
        operation = operation_by_node.get(event.node_id)
        event_operation = event.metadata.get("operation")
        if event_operation is not None and event_operation != operation:
            conflicts.add(key)
        if event.event_type == "task_started" and isinstance(event_operation, str):
            task_operations[(event.node_id, event.task_identity)] = event_operation

    for (node_id, execution_id, task_identity), rows in grouped.items():
        key = (node_id, execution_id, task_identity)
        operation = operation_by_node.get(node_id)
        starts = [event for event in rows if event.event_type == "task_started"]
        completes = [event for event in rows if event.event_type == "task_completed"]
        failures = [event for event in rows if event.event_type == "task_failed"]
        terminals = [
            event for event in rows if event.event_type in {"task_completed", "task_failed"}
        ]
        if (
            require_completed_tasks
            and starts
            and (len(starts) != 1 or len(completes) != 1 or failures)
        ):
            conflicts.add(key)
        if len(starts) == 1 and len(terminals) == 1 and starts[0].event_id >= terminals[0].event_id:
            conflicts.add(key)
        if (
            operation == "switch"
            and terminals
            and not any(event.event_type == "branch_selected" for event in rows)
        ):
            conflicts.add(key)
        if (
            terminals
            and operation in {"do", "for"}
            and not _execution_observation_complete(operation, node_id, task_identity, rows, events)
        ):
            conflicts.add(key)

    for event in events:
        if event.event_type not in {
            "scope_entered",
            "scope_exited",
            "iteration_started",
            "iteration_completed",
        }:
            continue
        assert event.node_id is not None
        assert event.execution_id is not None
        assert event.task_identity is not None
        key = (event.node_id, event.execution_id, event.task_identity)
        operation = event.metadata.get("operation")
        ordinal = event.metadata.get("iteration_ordinal")
        if event.event_type in {"iteration_started", "iteration_completed"}:
            parts = _iteration_identity_parts(event.task_identity, scope=False)
            if (
                operation != "for"
                or type(ordinal) is not int
                or parts is None
                or parts[1] != ordinal
                or task_operations.get((event.node_id, parts[0])) != "for"
            ):
                conflicts.add(key)
        elif operation == "do":
            if (
                ordinal is not None
                or not event.task_identity.endswith(".do")
                or task_operations.get((event.node_id, event.task_identity[:-3])) != "do"
            ):
                conflicts.add(key)
        else:
            parts = _iteration_identity_parts(event.task_identity, scope=True)
            if (
                operation not in {None, "for"}
                or type(ordinal) is not int
                or parts is None
                or parts[1] != ordinal
                or task_operations.get((event.node_id, parts[0])) != "for"
            ):
                conflicts.add(key)
    return conflicts


def _execution_observation_complete(
    operation: str,
    node_id: str,
    task_identity: str,
    task_events: list[_Event],
    all_events: list[_Event],
) -> bool:
    starts = [event for event in task_events if event.event_type == "task_started"]
    terminals = [
        event for event in task_events if event.event_type in {"task_completed", "task_failed"}
    ]
    if len(starts) != 1 or len(terminals) != 1:
        return False
    start = starts[0]
    terminal = terminals[0]
    if start.event_id >= terminal.event_id:
        return False
    if operation == "do":
        scope_identity = f"{task_identity}.do"
        scope_events = [
            event
            for event in all_events
            if event.node_id == node_id
            and event.task_identity == scope_identity
            and event.event_type in {"scope_entered", "scope_exited"}
        ]
        entered = [event for event in scope_events if event.event_type == "scope_entered"]
        exited = [event for event in scope_events if event.event_type == "scope_exited"]
        return (
            len(entered) == 1
            and len(exited) == 1
            and start.event_id < entered[0].event_id < exited[0].event_id < terminal.event_id
        )
    if operation != "for":
        return True

    loop_rows = [
        event
        for event in all_events
        if event.node_id == node_id
        and event.event_type
        in {"iteration_started", "iteration_completed", "scope_entered", "scope_exited"}
        and (
            parts := _iteration_identity_parts(
                event.task_identity or "",
                scope=event.event_type in {"scope_entered", "scope_exited"},
            )
        )
        is not None
        and parts[0] == task_identity
    ]
    ordinals = {
        event.metadata.get("iteration_ordinal")
        for event in loop_rows
        if type(event.metadata.get("iteration_ordinal")) is int
    }
    child_node_prefix = f"{node_id}/do/"
    for event in all_events:
        if (
            event.event_type != "task_started"
            or event.node_id is None
            or not event.node_id.startswith(child_node_prefix)
            or event.task_identity is None
        ):
            continue
        observed_ordinal = _child_iteration_ordinal(task_identity, event.task_identity)
        if observed_ordinal is not None:
            ordinals.add(observed_ordinal)
    if any(type(event.metadata.get("iteration_ordinal")) is not int for event in loop_rows):
        return False
    for ordinal in ordinals:
        iteration_identity = f"{task_identity}.iteration[{ordinal}]"
        iteration_rows = [
            event for event in loop_rows if event.metadata.get("iteration_ordinal") == ordinal
        ]
        expected_identity = {
            "iteration_started": iteration_identity,
            "iteration_completed": iteration_identity,
            "scope_entered": f"{iteration_identity}.do",
            "scope_exited": f"{iteration_identity}.do",
        }
        if any(
            event.task_identity != expected_identity[event.event_type] for event in iteration_rows
        ):
            return False
        by_type = {
            event_type: [event for event in iteration_rows if event.event_type == event_type]
            for event_type in expected_identity
        }
        if any(len(by_type[event_type]) != 1 for event_type in expected_identity):
            return False
        ordered = (
            start,
            by_type["iteration_started"][0],
            by_type["scope_entered"][0],
            by_type["scope_exited"][0],
            by_type["iteration_completed"][0],
            terminal,
        )
        if any(
            left.event_id >= right.event_id
            for left, right in zip(ordered, ordered[1:], strict=False)
        ):
            return False
    return True


def _iteration_identity_parts(value: str, *, scope: bool) -> tuple[str, int] | None:
    suffix = r"\.do" if scope else ""
    match = re.fullmatch(rf"(.+)\.iteration\[([0-9]+)\]{suffix}", value, re.ASCII)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _child_iteration_ordinal(loop_identity: str, child_identity: str) -> int | None:
    prefix = f"{loop_identity}.iteration["
    if not child_identity.startswith(prefix):
        return None
    match = re.match(r"([0-9]+)\]\.do\[[0-9]+\]\.", child_identity[len(prefix) :], re.ASCII)
    return None if match is None else int(match.group(1))


def _workflow_marker_reason(events: list[_Event]) -> str | None:
    markers = [event for event in events if event.event_type == "workflow_completed"]
    if not markers:
        return "workflow_not_confirmed"
    if len(markers) != 1 or not events or events[-1] is not markers[0]:
        return "evidence_conflict"
    timestamps = [datetime.fromisoformat(event.occurred_at) for event in events]
    if timestamps != sorted(timestamps):
        return "evidence_conflict"
    marker_time = datetime.fromisoformat(markers[0].occurred_at)
    if any(datetime.fromisoformat(event.occurred_at) > marker_time for event in events[:-1]):
        return "evidence_conflict"
    return None


def _complete_workflow_evidence(
    snapshot: Any,
    events: list[_Event],
    static_nodes: list[dict[str, Any]],
    jobs: list[_Job],
    dbos_status: str | None,
    conflicts: set[tuple[str, str, str]],
) -> bool:
    if not _workflow_status_success(snapshot, events, dbos_status) or conflicts:
        return False
    if any(
        job.state != "SUCCEEDED" or not job.receipt_verified or job.reconciliation for job in jobs
    ):
        return False

    by_identity: dict[str, list[_Job]] = defaultdict(list)
    for job in jobs:
        by_identity[job.task_identity].append(job)
    call_nodes = {
        node["node_id"]
        for node in static_nodes
        if node.get("operation") == "call"
        and isinstance(node.get("attributes"), dict)
        and node["attributes"].get("call") == "codex-job"
    }
    for event in events:
        if event.node_id not in call_nodes or event.event_type != "task_started":
            continue
        assert event.task_identity is not None
        matching_events = [
            candidate
            for candidate in events
            if candidate.node_id == event.node_id
            and candidate.execution_id == event.execution_id
            and candidate.task_identity == event.task_identity
            and candidate.event_type == "task_completed"
        ]
        matching_jobs = by_identity.get(event.task_identity, [])
        if (
            len(matching_events) != 1
            or len(matching_jobs) != 1
            or matching_jobs[0].state != "SUCCEEDED"
            or not matching_jobs[0].receipt_verified
        ):
            return False
    call_identities = {
        event.task_identity
        for event in events
        if event.node_id in call_nodes
        and event.event_type == "task_started"
        and event.task_identity is not None
    }
    return all(job.task_identity in call_identities for job in jobs)


def _workflow_status_success(snapshot: Any, events: list[_Event], dbos_status: str | None) -> bool:
    return (
        _workflow_marker_reason(events) is None
        and snapshot.status in _WORKFLOW_SUCCESS_STATES
        and snapshot.worker_state == "completed"
        and dbos_status in _SUCCESS_DBOS_STATES
    )


def _job_receipt_verified(state: Any, remote_state: Any, receipt_json: Any) -> bool:
    if state not in {"SUCCEEDED", "FAILED"}:
        return False
    if not isinstance(receipt_json, str):
        raise _ProjectionIssue("ledger_unavailable")
    try:
        receipt = _strict_json(receipt_json.encode("utf-8"))
    except Exception:
        raise _ProjectionIssue("ledger_unavailable") from None
    if state == "SUCCEEDED":
        result = receipt.get("result") if isinstance(receipt, dict) else None
        valid = (
            remote_state == "completed"
            and isinstance(result, dict)
            and set(receipt) == {"result"}
            and set(result) == {"final_text"}
            and isinstance(result["final_text"], str)
        )
    else:
        valid = (
            isinstance(receipt, dict)
            and set(receipt) == {"remote_state"}
            and receipt.get("remote_state") == remote_state
            and remote_state in {"failed", "interrupted"}
        )
    if not valid:
        raise _ProjectionIssue("ledger_unavailable")
    return isinstance(state, str) and state == "SUCCEEDED"


def scope_counts(events: list[_Event], node_id: str) -> tuple[int, int]:
    return (
        sum(event.node_id == node_id and event.event_type == "scope_entered" for event in events),
        sum(event.node_id == node_id and event.event_type == "scope_exited" for event in events),
    )


def iteration_counts(events: list[_Event], node_id: str) -> tuple[int, int]:
    return (
        sum(
            event.node_id == node_id and event.event_type == "iteration_started" for event in events
        ),
        sum(
            event.node_id == node_id and event.event_type == "iteration_completed"
            for event in events
        ),
    )


def _is_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _is_task_identity(value: Any) -> bool:
    return _valid_unicode_identifier(
        value,
        max_length=2_048,
        punctuation=_IDENTITY_PUNCTUATION,
        require_letter_or_number_start=True,
    ) and not _has_unsafe_path_segment(value)


def _valid_task_name(value: str) -> bool:
    if not _valid_unicode_identifier(
        value,
        max_length=128,
        punctuation=_TASK_NAME_PUNCTUATION,
        require_letter_or_number_start=True,
    ) or _has_unsafe_path_segment(value):
        return False
    return not value.startswith("/") and not value.endswith("/") and "//" not in value


def _valid_unicode_identifier(
    value: Any,
    *,
    max_length: int,
    punctuation: frozenset[str],
    require_letter_or_number_start: bool = False,
) -> bool:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if _SECRET_LIKE_RE.search(value):
        return False
    if require_letter_or_number_start and unicodedata.category(value[0])[0] not in {"L", "N"}:
        return False
    previous_base = False
    for character in value:
        if character in punctuation:
            previous_base = False
            continue
        category = unicodedata.category(character)
        if category[0] in {"L", "N"}:
            previous_base = True
            continue
        if category[0] == "M" and previous_base:
            continue
        return False
    return True


def _has_unsafe_path_segment(value: str) -> bool:
    segments = value.split("/")
    return (
        any(segment in {".", ".."} for segment in segments)
        or bool(re.search(r"\.{2,}/", value))
        or "\\" in value
        or any(":" in segment for segment in segments)
    )


def _has_graph_traversal_segment(value: str) -> bool:
    return any(segment in {".", ".."} for segment in value.split("/") if segment)


def _is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(value.st_mode):
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & reparse_attribute)


def _sqlite_reason(error: sqlite3.Error) -> str:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "journal_busy"
    if isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "journal_corrupt"
    return "journal_unavailable"


def _first_event_id(events: list[_Event]) -> int:
    starts = [event.event_id for event in events if event.event_type == "task_started"]
    return min(starts) if starts else min((event.event_id for event in events), default=0)


def _snapshot_version(
    snapshot: Any,
    dbos_status: str | None,
    jobs: list[_Job] | tuple[_Job, ...],
    latest: int,
    projection: dict[str, Any],
) -> str:
    material = {
        "projection": {
            key: value for key, value in projection.items() if key != "snapshot_version"
        },
        "workflow_status": snapshot.status,
        "worker_status": snapshot.worker_state,
        "dbos_status": dbos_status,
        "jobs": [
            {
                "job_id": job.job_id,
                "task_identity": job.task_identity,
                "state": job.state,
                "thread_id": job.thread_id,
                "turn_id": job.turn_id,
                "reconciliation": job.reconciliation,
                "receipt_verified": job.receipt_verified,
            }
            for job in sorted(jobs, key=lambda item: (item.task_identity, item.job_id))
        ],
        "latest_event_id": latest,
    }
    encoded = json.dumps(
        material, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _attach_snapshot_version(
    projection: dict[str, Any],
    snapshot: Any,
    dbos_status: str | None,
    jobs: list[_Job] | tuple[_Job, ...],
) -> dict[str, Any]:
    projection["snapshot_version"] = "sha256:" + _snapshot_version(
        snapshot,
        dbos_status,
        jobs,
        projection.get("latest_event_id", 0),
        projection,
    )
    return projection


def _edge_is_visible(edge: dict[str, Any], node_ids: set[str]) -> bool:
    source = edge["source"]
    target = edge["target"]
    return (source in node_ids or source.startswith("@scope:")) and (
        target in node_ids or target.startswith("@scope:")
    )


def _public_event(event: _Event) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "node_id": event.node_id,
        "task_identity": event.task_identity,
        "execution_id": event.execution_id,
        "occurred_at": event.occurred_at,
        "source": event.source,
        "metadata": event.metadata,
    }


def _fit_task_response(result: dict[str, Any]) -> dict[str, Any]:
    response_budget = _MAX_TASK_RESPONSE_BYTES - 128
    if len(_json_bytes(result)) <= response_budget:
        return result
    result["truncated"] = True
    result["coverage"] = "partial"
    result["coverage_reason"] = "truncated"
    while len(_json_bytes(result)) > response_budget:
        last_node = result["nodes"][-1] if result["nodes"] else None
        if last_node is not None and last_node["executions"]:
            last_node["executions"].pop()
            continue
        if result["edges"]:
            result["edges"].pop()
            continue
        if result["nodes"]:
            result["nodes"].pop()
            node_ids = {node["node_id"] for node in result["nodes"]}
            result["edges"] = [edge for edge in result["edges"] if _edge_is_visible(edge, node_ids)]
            continue
        break
    return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
