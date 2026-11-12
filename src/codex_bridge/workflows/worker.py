from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig, SetWorkflowID
from sqlalchemy.engine import URL

from ..paths import AllowedPathPolicy, PathPolicyError
from .codex_job_adapter import (
    CodexJobAdapter,
    CodexJobFailedError,
    CodexJobReconciliationRequiredError,
    CodexJobTimeoutError,
)
from .diagnostic_log import append_lifecycle_event
from .http_client import CodexHTTPClient
from .interpreter import (
    Interpreter,
    OwsError,
    compute_codex_job_request,
    compute_mock_job_request,
    compute_task_action,
    parse_workflow_text,
    validate_workflow,
)
from .job_adapter import (
    DurableJobAdapter,
    JobFailedError,
    JobReconciliationRequiredError,
    JobTimeoutError,
    submit_and_wait,
)
from .process_identity import get_process_identity

APPLICATION_NAME = "codexbridge-ows-phase3-poc"
APPLICATION_VERSION = "1.0.3-subset"
EXECUTOR_ID = "codexbridge-ows-phase3-executor"
_SHUTDOWN_LAUNCH_ID = re.compile(r"worker-shutdown-([0-9a-f]{32})\.request\Z", re.ASCII)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise OwsError(f"value is not canonical JSON: {error}") from error


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        json.dump(value, output_file, ensure_ascii=False, indent=2, sort_keys=True)
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.replace(temporary_path, path)


def _persist_workflow_id(run_dir: Path, manifest: dict[str, Any], workflow_id: str) -> None:
    if manifest.get("workflow_id") == workflow_id:
        return
    manifest["workflow_id"] = workflow_id
    _write_json(run_dir / "manifest.json", manifest)


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as event_file:
        event_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        event_file.flush()
        os.fsync(event_file.fileno())


def _heartbeat(stop_event: threading.Event, path: Path) -> None:
    sequence = 0
    while not stop_event.wait(0.1):
        _append_event(
            path,
            {
                "event": "worker_heartbeat",
                "pid": os.getpid(),
                "sequence": sequence,
                "timestamp_utc": datetime.now(UTC).isoformat(),
            },
        )
        sequence += 1


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot read run manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise OwsError("run manifest must be a JSON object")
    return value


def load_run_request(run_dir: Path) -> tuple[dict[str, Any], str, Any]:
    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    definition_path = run_dir / "workflow.yaml"
    input_path = run_dir / "input.json"
    try:
        definition_text = definition_path.read_text(encoding="utf-8")
        input_data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OwsError(f"cannot read immutable workflow snapshot: {error}") from error

    actual_definition_hash = sha256_text(definition_text)
    actual_input_hash = sha256_json(input_data)
    if actual_definition_hash != manifest.get("definition_sha256"):
        raise OwsError("workflow snapshot hash does not match manifest")
    if actual_input_hash != manifest.get("input_sha256"):
        raise OwsError("workflow input hash does not match manifest")
    control = manifest.get("control")
    if isinstance(control, dict) and control.get("max_live_codex_jobs") == 3:
        bridge_port = control.get("codex_bridge_port")
        from .interpreter import (
            parse_workflow_text,
            validate_three_live_codex_job_input,
            validate_workflow,
        )

        if (
            manifest.get("mode") != "codex-job-live"
            or manifest.get("workflow_source") != "ui-api"
            or set(control)
            != {
                "codex_job_live",
                "codex_bridge_port",
                "codex_job_max_polls",
                "codex_job_poll_interval_seconds",
                "max_live_codex_jobs",
            }
            or control.get("codex_job_live") is not True
            or type(control.get("max_live_codex_jobs")) is not int
            or control.get("max_live_codex_jobs") != 3
            or type(bridge_port) is not int
            or not 1 <= bridge_port <= 65535
            or manifest.get("codex_bridge_port") != bridge_port
            or control.get("codex_job_max_polls") != 600
            or control.get("codex_job_poll_interval_seconds") != 0.2
            or manifest.get("max_tasks") != 10_000
        ):
            raise OwsError("three-job workflow manifest control is invalid")
        definition = parse_workflow_text(definition_text)
        validate_workflow(definition, codex_job_live=True, max_live_codex_jobs=3)
        validate_three_live_codex_job_input(input_data)
        workspace_root = manifest.get("workspace_root")
        if not isinstance(workspace_root, str) or not Path(workspace_root).is_absolute():
            raise OwsError("three-job workflow manifest workspace is invalid")
        request_digest = sha256_json(
            {
                "workflow": definition,
                "input": input_data,
                "workspace_root": workspace_root,
                "max_live_codex_jobs": 3,
            }
        )
        if request_digest != manifest.get("request_digest"):
            raise OwsError("three-job request digest does not match the manifest")
    return manifest, definition_text, input_data


def _dbos_config(database_path: Path) -> DBOSConfig:
    database_url = URL.create(
        "sqlite", database=database_path.resolve().as_posix()
    ).render_as_string(hide_password=False)
    return {
        "name": APPLICATION_NAME,
        "application_version": APPLICATION_VERSION,
        "executor_id": EXECUTOR_ID,
        "system_database_url": database_url,
        "log_level": "ERROR",
        "console_log_level": "ERROR",
    }


def _output_digest(value: Any) -> str:
    return sha256_json(value)


def _validate_task_checkpoint(
    checkpoint: Any,
    *,
    identity: str,
    task_name: str,
    operation: str,
    definition_sha256: str,
    task_input: Any,
    variables: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise OwsError(f"persisted task checkpoint is invalid for {identity}")
    expected = {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": sha256_json(task_input),
        "context_sha256": sha256_json(variables.get("context")),
        "variables_sha256": sha256_json(variables),
    }
    for field, expected_value in expected.items():
        if checkpoint.get(field) != expected_value:
            label = "input digest" if field == "input_sha256" else field
            if field == "context_sha256":
                label = "context digest"
            elif field == "variables_sha256":
                label = "runtime variables digest"
            raise OwsError(f"persisted task {label} mismatch for {identity}")
    if "result" not in checkpoint:
        raise OwsError(f"persisted task checkpoint has no result for {identity}")
    return checkpoint


def _run_task_step(
    identity: str,
    task_name: str,
    operation: str,
    task_spec: dict[str, Any],
    input_data: Any,
    variables: dict[str, Any],
    definition_sha256: str,
    trace_path: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    event_path = Path(trace_path)
    started_at = datetime.now(UTC).isoformat()
    _append_event(
        event_path,
        {
            "event": "step_started",
            "identity": identity,
            "task_name": task_name,
            "operation": operation,
            "definition_sha256": definition_sha256,
            "pid": os.getpid(),
            "started_at": started_at,
        },
    )

    if control.get("gate_task_name") == task_name:
        marker_value = control.get("continue_marker")
        if not isinstance(marker_value, str):
            raise OwsError("test gate requires a continue marker path")
        marker = Path(marker_value)
        deadline = time.monotonic() + float(control.get("gate_timeout_seconds", 90))
        while not marker.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"test gate timed out for task {task_name!r}")
            time.sleep(0.05)

    input_sha256 = sha256_json(input_data)
    context_sha256 = sha256_json(variables.get("context"))
    variables_sha256 = sha256_json(variables)
    if operation == "call":
        request = compute_mock_job_request(task_spec, input_data, variables)
        workflow_id = DBOS.workflow_id
        if not isinstance(workflow_id, str) or not workflow_id:
            raise OwsError("mock-job adapter requires an active DBOS workflow")
        adapter = DurableJobAdapter(event_path.parent / "jobs.sqlite")
        timeout_seconds = control.get("mock_job_timeout_seconds", 10.0)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise OwsError("mock_job_timeout_seconds must be numeric")
        result = submit_and_wait(
            adapter,
            workflow_id,
            identity,
            request,
            timeout_seconds=float(timeout_seconds),
        )
        if control.get("gate_after_job_identity") == identity:
            marker_value = control.get("post_job_continue_marker")
            if not isinstance(marker_value, str):
                raise OwsError("post-job test gate requires a continue marker path")
            _append_event(
                event_path,
                {
                    "event": "job_succeeded_before_checkpoint",
                    "identity": identity,
                    "job_id": result["job_id"],
                    "request_key": result["request_key"],
                    "pid": os.getpid(),
                    "timestamp_utc": datetime.now(UTC).isoformat(),
                },
            )
            marker = Path(marker_value)
            deadline = time.monotonic() + float(control.get("gate_timeout_seconds", 90))
            while not marker.is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError("post-job test gate timed out before DBOS checkpoint")
                time.sleep(0.05)
    else:
        result = compute_task_action(operation, task_spec, input_data, variables)
    _append_event(
        event_path,
        {
            "event": "step_completed",
            "identity": identity,
            "task_name": task_name,
            "operation": operation,
            "definition_sha256": definition_sha256,
            "output_sha256": _output_digest(result),
            "pid": os.getpid(),
            "completed_at": datetime.now(UTC).isoformat(),
        },
    )
    return {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": input_sha256,
        "context_sha256": context_sha256,
        "variables_sha256": variables_sha256,
        "result": result,
    }


def _durable_start_job_step(
    run_dir: str,
    workflow_id: str,
    identity: str,
    request: dict[str, Any],
    checkpoint_metadata: dict[str, Any],
) -> dict[str, Any]:
    started = DurableJobAdapter(Path(run_dir) / "jobs.sqlite").start_job(
        workflow_id, identity, request
    )
    return {
        **checkpoint_metadata,
        "request_sha256": sha256_json(request),
        "result": started,
    }


def _durable_poll_job_step(
    run_dir: str,
    job_id: str,
    request_key: str,
    request_sha256: str,
    event_cursor: int,
    poll_index: int,
    stale_ttl_seconds: float,
    checkpoint_metadata: dict[str, Any],
) -> dict[str, Any]:
    adapter = DurableJobAdapter(Path(run_dir) / "jobs.sqlite")
    before = adapter.get_job_status(job_id=job_id, stale_ttl_seconds=stale_ttl_seconds)
    events = adapter.read_events(after_id=event_cursor, limit=100)
    next_cursor = max((event["event_id"] for event in events), default=event_cursor)
    after = adapter.get_job_status(job_id=job_id, stale_ttl_seconds=stale_ttl_seconds)
    for _ in range(9):
        if next_cursor >= after["latest_event_id"]:
            break
        more_events = adapter.read_events(after_id=next_cursor, limit=100)
        if not more_events:
            break
        events.extend(more_events)
        next_cursor = max(event["event_id"] for event in more_events)
    if next_cursor < after["latest_event_id"]:
        raise JobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: event catch-up exceeded 1000 rows for {request_key}"
        )
    if after["request_key"] != request_key or after["request_digest"] != request_sha256:
        raise JobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: status identity changed for {request_key}"
        )
    if before["job_id"] != after["job_id"]:
        raise JobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: job identity changed for {request_key}"
        )
    return {
        **checkpoint_metadata,
        "request_sha256": request_sha256,
        "job_id": job_id,
        "request_key": request_key,
        "event_cursor": event_cursor,
        "poll_index": poll_index,
        "result": {"snapshot": after, "events": events, "next_cursor": next_cursor},
    }


def _durable_codex_job_start_step(
    run_dir: str,
    workflow_id: str,
    identity: str,
    request: dict[str, str],
    port: int,
    checkpoint_metadata: dict[str, Any],
    workspace_root: str | None = None,
    allowed_roots: tuple[str, ...] = (),
    max_jobs_per_workflow: int = 1,
) -> dict[str, Any]:
    adapter = CodexJobAdapter(Path(run_dir) / "codex_jobs.sqlite")
    started = adapter.start_job(
        workflow_id,
        identity,
        request["prompt"],
        Path(run_dir),
        CodexHTTPClient(port=port),
        workspace_root=None if workspace_root is None else Path(workspace_root),
        allowed_roots=allowed_roots,
        max_jobs_per_workflow=max_jobs_per_workflow,
    )
    result = {
        key: started[key]
        for key in (
            "job_id",
            "request_key",
            "request_digest",
            "state",
            "status",
            "thread_id",
            "turn_id",
        )
    }
    return {
        **checkpoint_metadata,
        "request_sha256": sha256_json(request),
        "result": result,
    }


def _durable_codex_job_poll_step(
    run_dir: str,
    job_id: str,
    request_key: str,
    request_digest: str,
    poll_index: int,
    port: int,
    checkpoint_metadata: dict[str, Any],
) -> dict[str, Any]:
    adapter = CodexJobAdapter(Path(run_dir) / "codex_jobs.sqlite")
    snapshot = adapter.observe_job(job_id, CodexHTTPClient(port=port))
    if snapshot["request_key"] != request_key or snapshot["request_digest"] != request_digest:
        raise CodexJobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: codex-job identity changed for {request_key}"
        )
    return {
        **checkpoint_metadata,
        "request_sha256": sha256_json({"prompt": snapshot["request"]["prompt"]}),
        "job_id": job_id,
        "request_key": request_key,
        "request_digest": request_digest,
        "poll_index": poll_index,
        "result": snapshot,
    }


@cache
def _durable_codex_job_step(identity: str, stage: str, poll_index: int = 0) -> Any:
    material = f"{identity}\0{stage}\0{poll_index}"
    step_name = f"ows4d_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
    function = _durable_codex_job_start_step if stage == "start" else _durable_codex_job_poll_step
    return DBOS.step(name=step_name)(function)


def _run_durable_codex_job(
    *,
    identity: str,
    task_name: str,
    operation: str,
    task_input: Any,
    variables: dict[str, Any],
    request: dict[str, str],
    definition_sha256: str,
    trace_path: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    workflow_id = DBOS.workflow_id
    if not isinstance(workflow_id, str) or not workflow_id:
        raise OwsError("codex-job adapter requires an active DBOS workflow")
    max_live_codex_jobs = control.get("max_live_codex_jobs", 1)
    if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
        raise OwsError("max_live_codex_jobs must be the integer 1, 2, or 3")
    port = control.get("codex_bridge_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise OwsError("codex_bridge_port must be an integer from 1 to 65535")
    max_polls = control.get("codex_job_max_polls", 600)
    poll_interval = control.get("codex_job_poll_interval_seconds", 0.2)
    if type(max_polls) is not int or not 1 <= max_polls <= 600:
        raise OwsError("codex_job_max_polls must be an integer from 1 to 600")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not 0.05 <= poll_interval <= 0.2
        or max_polls * poll_interval > 120
    ):
        raise OwsError("codex_job_poll_interval_seconds produces an invalid bounded wait")

    run_dir = Path(trace_path).parent
    metadata = {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": sha256_json(task_input),
        "context_sha256": sha256_json(variables.get("context")),
        "variables_sha256": sha256_json(variables),
    }
    workspace_root = control.get("workspace_root")
    raw_allowed_roots = control.get("allowed_roots", [])
    if workspace_root is not None and not isinstance(workspace_root, str):
        raise OwsError("workspace_root must be a string")
    if not isinstance(raw_allowed_roots, list) or not all(
        isinstance(root, str) for root in raw_allowed_roots
    ):
        raise OwsError("allowed_roots must be a list of strings")
    start_step = _durable_codex_job_step(identity, "start")
    start_arguments = (str(run_dir), workflow_id, identity, request, port, metadata)
    if workspace_root is None and not raw_allowed_roots:
        if max_live_codex_jobs == 1:
            start_checkpoint = start_step(*start_arguments)
        else:
            start_checkpoint = start_step(*start_arguments, None, (), max_live_codex_jobs)
    else:
        if max_live_codex_jobs == 1:
            start_checkpoint = start_step(
                *start_arguments,
                workspace_root,
                tuple(raw_allowed_roots),
            )
        else:
            start_checkpoint = start_step(
                *start_arguments,
                workspace_root,
                tuple(raw_allowed_roots),
                max_live_codex_jobs,
            )
    start_checkpoint = _validate_task_checkpoint(
        start_checkpoint,
        identity=identity,
        task_name=task_name,
        operation=operation,
        definition_sha256=definition_sha256,
        task_input=task_input,
        variables=variables,
    )
    started = start_checkpoint["result"]
    job_id = started.get("job_id")
    request_key = started.get("request_key")
    request_digest = started.get("request_digest")
    request_sha256 = sha256_json(request)
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(request_key, str)
        or not request_key
        or not isinstance(request_digest, str)
        or started.get("status") == "NEEDS_RECONCILIATION"
        or start_checkpoint.get("request_sha256") != request_sha256
    ):
        raise CodexJobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: invalid codex-job start checkpoint for {identity}"
        )

    for poll_index in range(max_polls):
        checkpoint = _durable_codex_job_step(identity, "status", poll_index)(
            str(run_dir),
            job_id,
            request_key,
            request_digest,
            poll_index,
            port,
            metadata,
        )
        checkpoint = _validate_task_checkpoint(
            checkpoint,
            identity=identity,
            task_name=task_name,
            operation=operation,
            definition_sha256=definition_sha256,
            task_input=task_input,
            variables=variables,
        )
        if (
            checkpoint.get("request_sha256") != request_sha256
            or checkpoint.get("job_id") != job_id
            or checkpoint.get("request_key") != request_key
            or checkpoint.get("request_digest") != request_digest
            or checkpoint.get("poll_index") != poll_index
        ):
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: invalid codex-job status checkpoint for {identity}"
            )
        snapshot = checkpoint["result"]
        if snapshot.get("status") == "NEEDS_RECONCILIATION":
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: job {job_id} requires adapter reconciliation"
            )
        if snapshot.get("state") == "SUCCEEDED":
            receipt = snapshot.get("receipt")
            result = receipt.get("result") if isinstance(receipt, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("final_text"), str):
                raise CodexJobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: SUCCEEDED job has no final answer for {request_key}"
                )
            return {
                "job_id": job_id,
                "request_key": request_key,
                "thread_id": snapshot["thread_id"],
                "turn_id": snapshot["turn_id"],
                "status": "SUCCEEDED",
                "result": {"final_text": result["final_text"]},
            }
        if snapshot.get("state") == "FAILED":
            raise CodexJobFailedError(
                f"codex-job FAILED: job_id={job_id} request_key={request_key}"
            )
        if snapshot.get("state") not in {"ACCEPTED", "RUNNING"}:
            raise CodexJobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: unsupported codex-job state for {request_key}"
            )
        if poll_index + 1 < max_polls:
            DBOS.sleep(float(poll_interval))

    raise CodexJobTimeoutError(
        f"codex-job wait exceeded {max_polls} status checks for job {job_id}; job was not cancelled"
    )


@cache
def _durable_job_step(identity: str, stage: str, poll_index: int = 0) -> Any:
    material = f"{identity}\0{stage}\0{poll_index}"
    step_name = f"ows4c_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"
    function = _durable_start_job_step if stage == "start" else _durable_poll_job_step
    return DBOS.step(name=step_name)(function)


def _run_durable_mock_job(
    *,
    identity: str,
    task_name: str,
    operation: str,
    task_spec: dict[str, Any],
    task_input: Any,
    variables: dict[str, Any],
    request: dict[str, Any],
    definition_sha256: str,
    trace_path: str,
    control: dict[str, Any],
) -> dict[str, Any]:
    workflow_id = DBOS.workflow_id
    if not isinstance(workflow_id, str) or not workflow_id:
        raise OwsError("durable mock-job adapter requires an active DBOS workflow")
    run_dir = Path(trace_path).parent
    metadata = {
        "identity": identity,
        "task_name": task_name,
        "operation": operation,
        "definition_sha256": definition_sha256,
        "input_sha256": sha256_json(task_input),
        "context_sha256": sha256_json(variables.get("context")),
        "variables_sha256": sha256_json(variables),
    }

    start_checkpoint = _durable_job_step(identity, "start")(
        str(run_dir), workflow_id, identity, request, metadata
    )
    start_checkpoint = _validate_task_checkpoint(
        start_checkpoint,
        identity=identity,
        task_name=task_name,
        operation=operation,
        definition_sha256=definition_sha256,
        task_input=task_input,
        variables=variables,
    )
    started = start_checkpoint["result"]
    job_id = started.get("job_id")
    request_key = started.get("request_key")
    request_sha256 = start_checkpoint.get("request_sha256")
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(request_key, str)
        or not request_key
        or request_sha256 != sha256_json(request)
    ):
        raise JobReconciliationRequiredError(
            f"NEEDS_RECONCILIATION: invalid durable start checkpoint for {identity}"
        )
    _append_event(
        Path(trace_path),
        {
            "event": "durable_job_started_before_wait",
            "identity": identity,
            "job_id": job_id,
            "request_key": request_key,
            "pid": os.getpid(),
            "timestamp_utc": datetime.now(UTC).isoformat(),
        },
    )

    max_polls = control.get("mock_job_max_polls", 100)
    poll_interval = control.get("mock_job_poll_interval_seconds", 0.1)
    stale_ttl = control.get("mock_job_reconciliation_ttl_seconds", 2.0)
    if type(max_polls) is not int or not 1 <= max_polls <= 600:
        raise OwsError("mock_job_max_polls must be an integer from 1 to 600")
    if (
        isinstance(poll_interval, bool)
        or not isinstance(poll_interval, (int, float))
        or not 0.05 <= poll_interval <= 0.25
    ):
        raise OwsError("mock_job_poll_interval_seconds must be from 0.05 to 0.25")
    if (
        isinstance(stale_ttl, bool)
        or not isinstance(stale_ttl, (int, float))
        or not 0.1 <= stale_ttl <= 30
    ):
        raise OwsError("mock_job_reconciliation_ttl_seconds must be from 0.1 to 30")
    if max_polls * poll_interval > 30:
        raise OwsError("durable mock-job wait may not exceed 30 seconds")

    event_cursor = 0
    for poll_index in range(max_polls):
        checkpoint = _durable_job_step(identity, "status", poll_index)(
            str(run_dir),
            job_id,
            request_key,
            request_sha256,
            event_cursor,
            poll_index,
            float(stale_ttl),
            metadata,
        )
        checkpoint = _validate_task_checkpoint(
            checkpoint,
            identity=identity,
            task_name=task_name,
            operation=operation,
            definition_sha256=definition_sha256,
            task_input=task_input,
            variables=variables,
        )
        if (
            checkpoint.get("request_sha256") != request_sha256
            or checkpoint.get("job_id") != job_id
            or checkpoint.get("request_key") != request_key
            or checkpoint.get("event_cursor") != event_cursor
            or checkpoint.get("poll_index") != poll_index
        ):
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: invalid durable status checkpoint for {identity}"
            )
        snapshot = checkpoint["result"]["snapshot"]
        if snapshot.get("adapter_marker") == "NEEDS_RECONCILIATION":
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: job {job_id} requires adapter reconciliation"
            )
        if snapshot.get("state") == "SUCCEEDED":
            if snapshot.get("result") is None:
                raise JobReconciliationRequiredError(
                    f"NEEDS_RECONCILIATION: SUCCEEDED job has no receipt for {request_key}"
                )
            return {
                "job_id": job_id,
                "request_key": request_key,
                "status": "SUCCEEDED",
                "result": snapshot["result"],
            }
        if snapshot.get("state") == "FAILED":
            raise JobFailedError(
                f"mock-job FAILED: job_id={job_id} request_key={request_key} "
                f"error={snapshot.get('error')}"
            )
        if snapshot.get("state") not in {"QUEUED", "RUNNING"}:
            raise JobReconciliationRequiredError(
                f"NEEDS_RECONCILIATION: unsupported job state {snapshot.get('state')!r} "
                f"for {request_key}"
            )
        event_cursor = checkpoint["result"]["next_cursor"]
        if poll_index + 1 < max_polls:
            DBOS.sleep(float(poll_interval))

    raise JobTimeoutError(
        f"mock-job durable wait exceeded {max_polls} status checks for job {job_id}; "
        "job was not cancelled"
    )


@cache
def _named_step(identity: str) -> Any:
    step_name = f"ows_{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    return DBOS.step(name=step_name)(_run_task_step)


@DBOS.workflow()
def execute_ows_workflow(
    definition_text: str,
    definition_sha256: str,
    input_data: Any,
    input_sha256: str,
    trace_path: str,
    control: dict[str, Any],
    max_tasks: int,
) -> Any:
    if sha256_text(definition_text) != definition_sha256:
        raise OwsError("persisted OWS definition hash mismatch")
    if sha256_json(input_data) != input_sha256:
        raise OwsError("persisted OWS input hash mismatch")
    definition = parse_workflow_text(definition_text)
    codex_job_live = control.get("codex_job_live", False)
    if not isinstance(codex_job_live, bool):
        raise OwsError("codex_job_live must be a boolean")
    max_live_codex_jobs = control.get("max_live_codex_jobs", 1)
    if type(max_live_codex_jobs) is not int or max_live_codex_jobs not in {1, 2, 3}:
        raise OwsError("max_live_codex_jobs must be the integer 1, 2, or 3")
    durable_wait = control.get("mock_job_durable_wait", False)
    if not isinstance(durable_wait, bool):
        raise OwsError("mock_job_durable_wait must be a boolean")
    if codex_job_live and durable_wait:
        raise OwsError("codex-job live mode cannot be mixed with mock-job durable-wait mode")
    validate_workflow(
        definition,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )
    if max_live_codex_jobs == 3:
        from .interpreter import validate_three_live_codex_job_input

        validate_three_live_codex_job_input(input_data)

    def run_named_step(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        if codex_job_live and operation == "call":
            request = compute_codex_job_request(
                task_spec,
                task_input,
                variables,
                max_live_codex_jobs=max_live_codex_jobs,
            )
            return _run_durable_codex_job(
                identity=identity,
                task_name=task_name,
                operation=operation,
                task_input=task_input,
                variables=variables,
                request=request,
                definition_sha256=definition_sha256,
                trace_path=trace_path,
                control=control,
            )
        if durable_wait and operation == "call":
            request = compute_mock_job_request(task_spec, task_input, variables)
            return _run_durable_mock_job(
                identity=identity,
                task_name=task_name,
                operation=operation,
                task_spec=task_spec,
                task_input=task_input,
                variables=variables,
                request=request,
                definition_sha256=definition_sha256,
                trace_path=trace_path,
                control=control,
            )
        checkpoint = _named_step(identity)(
            identity,
            task_name,
            operation,
            task_spec,
            task_input,
            variables,
            definition_sha256,
            trace_path,
            control,
        )
        checkpoint = _validate_task_checkpoint(
            checkpoint,
            identity=identity,
            task_name=task_name,
            operation=operation,
            definition_sha256=definition_sha256,
            task_input=task_input,
            variables=variables,
        )
        return checkpoint["result"]

    interpreter = Interpreter(
        definition,
        max_tasks=max_tasks,
        step_runner=run_named_step,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )
    return interpreter.run(input_data)


def _read_workflow_status(database_path: Path, workflow_id: str) -> str | None:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True, timeout=2.0) as connection:
        row = connection.execute(
            "SELECT status FROM workflow_status WHERE workflow_uuid = ?",
            (workflow_id,),
        ).fetchone()
    return None if row is None else str(row[0])


class WorkerShutdownRequested(RuntimeError):
    """The Bridge requested worker shutdown without cancelling a Codex job."""


def _wait_for_result(
    database_path: Path,
    workflow_id: str,
    timeout: float,
    shutdown_file: Path | None = None,
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if shutdown_file is not None and shutdown_file.is_file():
            raise WorkerShutdownRequested("workflow worker shutdown was requested")
        status = _read_workflow_status(database_path, workflow_id)
        if status == "SUCCESS":
            handle: Any = DBOS.retrieve_workflow(workflow_id)
            return handle.get_result()
        if status in {"ERROR", "CANCELLED"}:
            raise RuntimeError(f"workflow {workflow_id} ended with status {status}")
        time.sleep(0.05)
    status = _read_workflow_status(database_path, workflow_id)
    raise TimeoutError(f"workflow {workflow_id} timed out with status {status!r}")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OWS subset DBOS worker")
    parser.add_argument("--mode", choices=("start", "recover"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--startup-file", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--allowed-root", action="append", default=[])
    parser.add_argument("--shutdown-file", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    run_dir = args.run_dir.resolve()
    manifest, definition_text, input_data = load_run_request(run_dir)
    startup_override = args.startup_file.resolve() if args.startup_file is not None else None
    if startup_override is not None:
        try:
            startup_override.relative_to(run_dir)
        except ValueError as error:
            raise OwsError("--startup-file must be inside the run directory") from error
    database_path = run_dir / "dbos.sqlite"
    control = manifest.get("control", {})
    if not isinstance(control, dict):
        raise OwsError("manifest control must be an object")
    codex_job_live = control.get("codex_job_live", False)
    if not isinstance(codex_job_live, bool):
        raise OwsError("manifest codex_job_live must be a boolean")
    if codex_job_live and manifest.get("mode") != "codex-job-live":
        raise OwsError("codex-job live mode does not match the run manifest")
    if manifest.get("mode") == "codex-job-live" and not codex_job_live:
        raise OwsError("codex-job live mode is missing from manifest control")
    manifest_workflow_id = manifest.get("workflow_id")
    workflow_id: str | None = (
        manifest_workflow_id if isinstance(manifest_workflow_id, str) else None
    )
    if codex_job_live:
        if not isinstance(workflow_id, str) or not workflow_id:
            raise OwsError("codex-job live mode requires a pre-generated workflow_id")
        if args.workspace_root is not None:
            try:
                policy = AllowedPathPolicy(tuple(args.allowed_root))
                workspace_root = Path(policy.validate_cwd(str(args.workspace_root)))
            except (PathPolicyError, OSError):
                raise OwsError("codex-job workspace is outside an allowed existing root") from None
            if not os.access(workspace_root, os.W_OK):
                raise OwsError("codex-job workspace is not writable")
            if (
                workspace_root == run_dir
                or workspace_root.is_relative_to(run_dir)
                or run_dir.is_relative_to(workspace_root)
            ):
                raise OwsError("workflow state and job workspace must be separate")
            control = {
                **control,
                "workspace_root": str(workspace_root),
                "allowed_roots": list(args.allowed_root),
            }

    shutdown_file: Path | None = None
    if args.shutdown_file is not None:
        shutdown_file = args.shutdown_file.resolve(strict=False)
        try:
            shutdown_file.relative_to(run_dir)
        except ValueError as error:
            raise OwsError("--shutdown-file must be inside the run directory") from error
        if shutdown_file.is_symlink():
            raise OwsError("--shutdown-file may not be a symlink")

    launch_id: str | None = None
    if shutdown_file is not None:
        launch_match = _SHUTDOWN_LAUNCH_ID.fullmatch(shutdown_file.name)
        if launch_match is not None:
            launch_id = launch_match.group(1)

    worker_identity = get_process_identity(os.getpid())
    if worker_identity is None or not worker_identity["active"]:
        raise OwsError("DBOS worker could not confirm its Windows process identity")
    identity_record = {
        "mode": args.mode,
        "pid": worker_identity["pid"],
        "parent_pid": worker_identity["parent_pid"],
        "run_dir": str(run_dir),
        "argv": sys.argv,
        "creation_time_100ns": worker_identity["creation_time_100ns"],
        "image_path": worker_identity["image_path"],
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }
    identity_path = (
        startup_override.with_suffix(".identity.json")
        if startup_override is not None
        else run_dir / f"{args.mode}-worker-identity.json"
    )
    _write_json(identity_path, identity_record)

    append_lifecycle_event(
        run_dir,
        source="worker",
        event="worker_bootstrap",
        run_id=run_dir.name,
        workflow_id=workflow_id,
        launch_id=launch_id,
        mode=args.mode,
        pid=worker_identity["pid"],
    )

    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    dbos_configured = False
    stage = "dbos_configure"
    try:
        DBOS(config=_dbos_config(database_path))
        dbos_configured = True
        stage = "dbos_launch"
        DBOS.launch()
        append_lifecycle_event(
            run_dir,
            source="worker",
            event="dbos_ready",
            run_id=run_dir.name,
            workflow_id=workflow_id,
            launch_id=launch_id,
            mode=args.mode,
            pid=worker_identity["pid"],
        )
        if args.mode == "start":
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                args=(heartbeat_stop, run_dir / "initial-worker-heartbeat.jsonl"),
                name="ows-poc-worker-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            workflow_arguments = (
                definition_text,
                manifest["definition_sha256"],
                input_data,
                manifest["input_sha256"],
                str(run_dir / "task-events.jsonl"),
                control,
                int(manifest.get("max_tasks", 10_000)),
            )
            stage = "workflow_start"
            if codex_job_live:
                requested_workflow_id = str(manifest["workflow_id"])
                with SetWorkflowID(requested_workflow_id):
                    handle = DBOS.start_workflow(
                        execute_ows_workflow,
                        *workflow_arguments,
                    )
                if handle.workflow_id != requested_workflow_id:
                    raise OwsError("DBOS workflow_id differs from the pre-generated manifest ID")
            else:
                handle = DBOS.start_workflow(execute_ows_workflow, *workflow_arguments)
            workflow_id = handle.workflow_id
            _persist_workflow_id(run_dir, manifest, workflow_id)
            startup_path = run_dir / "start-worker.json"
        else:
            stage = "workflow_recovery"
            recovered_workflow_id = manifest.get("workflow_id")
            if not isinstance(recovered_workflow_id, str) or not recovered_workflow_id:
                raise OwsError("recover mode requires the persisted workflow_id")
            workflow_id = recovered_workflow_id
            startup_path = run_dir / "recovery-worker.json"

        startup_path = startup_override if startup_override is not None else startup_path
        _write_json(
            startup_path,
            {**identity_record, "workflow_id": workflow_id},
        )

        append_lifecycle_event(
            run_dir,
            source="worker",
            event=("workflow_started" if args.mode == "start" else "workflow_recovery_attached"),
            run_id=run_dir.name,
            workflow_id=workflow_id,
            launch_id=launch_id,
            mode=args.mode,
            pid=worker_identity["pid"],
        )

        stage = "workflow_wait"
        try:
            result = _wait_for_result(
                database_path,
                workflow_id,
                args.timeout_seconds,
                shutdown_file,
            )
        except WorkerShutdownRequested:
            append_lifecycle_event(
                run_dir,
                source="worker",
                event="shutdown_observed",
                run_id=run_dir.name,
                workflow_id=workflow_id,
                launch_id=launch_id,
                mode=args.mode,
                pid=worker_identity["pid"],
                reason_code="shutdown_requested",
            )
            raise

        stage = "result_persist"
        _write_json(
            run_dir / "result.json",
            {
                "workflow_id": workflow_id,
                "result": result,
                "definition_sha256": manifest["definition_sha256"],
                "input_sha256": manifest["input_sha256"],
            },
        )
        append_lifecycle_event(
            run_dir,
            source="worker",
            event="result_persisted",
            run_id=run_dir.name,
            workflow_id=workflow_id,
            launch_id=launch_id,
            mode=args.mode,
            pid=worker_identity["pid"],
        )
        stage = "result_output"
        print(json.dumps({"workflow_id": workflow_id, "result": result}, ensure_ascii=False))
        return 0
    except WorkerShutdownRequested:
        raise
    except Exception as error:
        append_lifecycle_event(
            run_dir,
            source="worker",
            event="worker_failed",
            run_id=run_dir.name,
            workflow_id=workflow_id,
            launch_id=launch_id,
            mode=args.mode,
            pid=worker_identity["pid"],
            reason_code=_worker_failure_reason(stage),
            exception_class=type(error).__name__,
        )
        raise
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)
        if dbos_configured:
            try:
                DBOS.destroy()
            except Exception as error:
                append_lifecycle_event(
                    run_dir,
                    source="worker",
                    event="worker_failed",
                    run_id=run_dir.name,
                    workflow_id=workflow_id,
                    launch_id=launch_id,
                    mode=args.mode,
                    pid=worker_identity["pid"],
                    reason_code="dbos_destroy_failed",
                    exception_class=type(error).__name__,
                )
                raise


def _worker_failure_reason(stage: str) -> str:
    reasons = {
        "dbos_configure": "dbos_configure_failed",
        "dbos_launch": "dbos_launch_failed",
        "workflow_start": "workflow_start_failed",
        "workflow_recovery": "workflow_recovery_failed",
        "workflow_wait": "workflow_wait_failed",
        "result_persist": "result_persist_failed",
        "result_output": "result_output_failed",
    }
    return reasons.get(stage, "workflow_wait_failed")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"OWS DBOS worker failed: {error}", file=sys.stderr, flush=True)
        raise
