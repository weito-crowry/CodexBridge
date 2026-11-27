from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, cast

from dbos import DBOS, DBOSConfig, SetWorkflowID
from sqlalchemy.engine import URL

from ..paths import AllowedPathPolicy, PathPolicyError
from .agent_runtime import (
    build_codex_agent_runtime,
    open_agent_ledger_for_manifest,
    validate_pinned_agent_definition,
)
from .codex_job_adapter import (
    CodexJobAdapter,
    CodexJobFailedError,
    CodexJobReconciliationRequiredError,
    CodexJobTimeoutError,
)
from .contracts import ContractOutputInvalid, validate_output
from .diagnostic_log import append_lifecycle_event
from .http_client import CONTROL_TOKEN_ENV, CodexHTTPClient
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
from .node_events import NodeEventWriter
from .process_identity import get_process_identity
from .strict_executor import execute_strict_ows_workflow
from .strict_manifest import _is_symlink_or_junction
from .strict_reader_gate import (
    RunFormatVersion,
    StrictReaderGateError,
    _decode_manifest,
    classify_readonly,
    verify_strict_reserved_readonly,
)
from .strict_variable_runtime import StrictWorkflowVariableRuntimeAdapter
from .task_tree import (
    StrictTaskTreeKind,
    TaskTreeClassificationError,
    classify_task_tree,
    definition_has_call_target,
)

APPLICATION_NAME = "codexbridge-ows-phase3-poc"
APPLICATION_VERSION = "1.0.3-subset"
EXECUTOR_ID = "codexbridge-ows-phase3-executor"
_SHUTDOWN_LAUNCH_ID = re.compile(r"worker-shutdown-([0-9a-f]{32})\.request\Z", re.ASCII)
_LOGGER = logging.getLogger(__name__)


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


def load_run_request(
    run_dir: Path,
    *,
    allowed_roots: tuple[str, ...] = (),
) -> tuple[dict[str, Any], str, Any]:
    manifest, _manifest_bytes, verification = _read_run_manifest(
        run_dir,
        allowed_roots=allowed_roots,
    )
    if verification is not None:
        definition = verification._definition
        normalized_input = verification._normalized_input
        if (
            definition is None
            or type(definition.source_yaml) is not str
            or type(normalized_input) is not dict
        ):
            raise OwsError("workflow request binding is invalid")
        return manifest, definition.source_yaml, normalized_input

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
            three_live_codex_job_input_kind,
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
        input_kind = three_live_codex_job_input_kind(definition)
        validate_three_live_codex_job_input(input_data, input_kind=input_kind)
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


def _read_run_manifest(
    run_dir: Path,
    *,
    allowed_roots: tuple[str, ...] = (),
) -> tuple[dict[str, Any], bytes, Any | None]:
    manifest_path = run_dir / "manifest.json"
    if _is_symlink_or_junction(manifest_path):
        raise OwsError("workflow run manifest is unavailable")
    try:
        canonical_run_dir = run_dir.resolve(strict=True)
        resolved = manifest_path.resolve(strict=True)
        if resolved.parent != canonical_run_dir or not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("manifest is not a regular file")
        manifest_bytes = resolved.read_bytes()
        manifest, _ = _decode_manifest(manifest_bytes)
    except Exception:
        raise OwsError("workflow run manifest is invalid") from None
    if "run_format_version" in manifest:
        if (
            type(manifest.get("run_format_version")) is not int
            or manifest["run_format_version"] != 2
        ):
            raise OwsError("workflow run format is unsupported")
        workflow_id = manifest.get("workflow_id")
        request_digest = manifest.get("request_digest")
        if type(workflow_id) is not str or type(request_digest) is not str:
            raise OwsError("workflow request binding is invalid")
        try:
            verification = verify_strict_reserved_readonly(
                canonical_run_dir.parent,
                canonical_run_dir.name,
                manifest_bytes,
                expected_workflow_id=workflow_id,
                expected_request_digest=request_digest,
                allowed_roots=allowed_roots,
            )
        except StrictReaderGateError:
            raise OwsError("workflow request binding is invalid") from None
        if verification is None:
            raise OwsError("workflow request binding is invalid")
        return verification.manifest.to_dict(), manifest_bytes, verification
    _validate_managed_legacy_manifest(canonical_run_dir, manifest, manifest_bytes)
    return manifest, manifest_bytes, None


def _validate_managed_legacy_manifest(
    run_dir: Path,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
) -> None:
    request_database = run_dir.parent / "requests.sqlite"
    if _is_symlink_or_junction(request_database):
        raise OwsError("workflow request binding is invalid")
    try:
        metadata = request_database.stat()
    except FileNotFoundError:
        return
    except OSError:
        raise OwsError("workflow request binding is invalid") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise OwsError("workflow request binding is invalid")

    workflow_id = manifest.get("workflow_id")
    manifest_run_id = manifest.get("run_id")
    request_digest = manifest.get("request_digest")
    if (
        type(workflow_id) is not str
        or not workflow_id
        or type(manifest_run_id) is not str
        or manifest_run_id != run_dir.name
        or type(request_digest) is not str
    ):
        raise OwsError("workflow request binding is invalid")
    try:
        run_format = classify_readonly(
            run_dir.parent,
            run_dir.name,
            manifest_bytes,
            expected_workflow_id=workflow_id,
            expected_request_digest=request_digest,
        )
    except StrictReaderGateError:
        raise OwsError("workflow request binding is invalid") from None
    if run_format is not RunFormatVersion.LEGACY_V1:
        raise OwsError("workflow request binding is invalid")


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
    node_events_enabled: bool = False,
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
    if not isinstance(node_events_enabled, bool):
        raise OwsError("node event journal flag must be a boolean")
    if codex_job_live and durable_wait:
        raise OwsError("codex-job live mode cannot be mixed with mock-job durable-wait mode")
    validate_workflow(
        definition,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )
    if max_live_codex_jobs == 3:
        from .interpreter import (
            three_live_codex_job_input_kind,
            validate_three_live_codex_job_input,
        )

        input_kind = three_live_codex_job_input_kind(definition)
        validate_three_live_codex_job_input(input_data, input_kind=input_kind)

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

    node_event_writer: NodeEventWriter | None = None
    # Persisted pre-C2 DBOS invocations omit the flag and remain journal-free.
    if node_events_enabled:
        run_dir = Path(trace_path).parent
        try:
            active_workflow_id = DBOS.workflow_id
            if not isinstance(active_workflow_id, str) or not active_workflow_id:
                _LOGGER.warning("node event journal disabled reason=workflow_id_unavailable")
            else:
                node_event_writer = NodeEventWriter(
                    run_dir,
                    workflow_id=active_workflow_id,
                    run_id=run_dir.name,
                )
        except Exception:
            _LOGGER.warning("node event journal disabled reason=initialization_failed")

    try:
        interpreter = Interpreter(
            definition,
            max_tasks=max_tasks,
            step_runner=run_named_step,
            codex_job_live=codex_job_live,
            max_live_codex_jobs=max_live_codex_jobs,
            observer=None if node_event_writer is None else node_event_writer.observe,
        )
        result = interpreter.run(input_data)
        if node_event_writer is not None:
            try:
                node_event_writer.workflow_completed()
            except Exception:
                _LOGGER.warning("node event journal disabled reason=completion_write_failed")
        return result
    finally:
        if node_event_writer is not None:
            try:
                node_event_writer.close()
            except Exception:
                _LOGGER.warning("node event journal disabled reason=close_failed")


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
    parser.add_argument(
        "--strict-runtime-mode",
        choices=("isolated", "authorized_codex", "authorized_agent"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--codex-bridge-port", type=int, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


_STRICT_REASON_CODES = frozenset(
    {
        "strict_execution_uncertain",
        "strict_execution_invalid",
        "strict_execution_mode_invalid",
        "strict_execution_target_unavailable",
        "strict_operation_invalid",
        "resource_limit_exceeded",
        "output_contract_violation",
    }
)


def _strict_worker_adapter(
    args: argparse.Namespace, run_dir: Path
) -> StrictWorkflowVariableRuntimeAdapter:
    mode = args.strict_runtime_mode
    if mode not in {"isolated", "authorized_codex", "authorized_agent"}:
        raise OwsError("strict workflow runtime is not supported")
    try:
        adapter = StrictWorkflowVariableRuntimeAdapter.from_manifest_claim(
            run_dir.parent,
            run_dir.name,
            allowed_roots=tuple(args.allowed_root),
        )
        manifest = adapter._verified_manifest
        definition = adapter.pinned_definition
        if manifest is None or definition is None:
            raise ValueError
        try:
            classification = classify_task_tree(definition, strict_contract=True)
        except TaskTreeClassificationError:
            raise ValueError from None
        expected_mode = {
            StrictTaskTreeKind.STRICT_AGENT: "authorized_agent",
            StrictTaskTreeKind.STRICT_CODEX_JOB: "authorized_codex",
            StrictTaskTreeKind.STRICT_PURE: "isolated",
        }.get(classification.kind)
        if mode != expected_mode:
            raise ValueError
        if args.workspace_root is None:
            raise ValueError
        workspace_text = AllowedPathPolicy(tuple(args.allowed_root)).validate_cwd(
            str(args.workspace_root)
        )
        if os.path.normcase(workspace_text) != os.path.normcase(manifest.workspace_root):
            raise ValueError
        workspace = Path(workspace_text)
        state_root = run_dir.parent.resolve(strict=True)
        if not os.access(workspace, os.W_OK) or (
            workspace == state_root
            or workspace.is_relative_to(state_root)
            or state_root.is_relative_to(workspace)
        ):
            raise ValueError
        if mode in {"authorized_codex", "authorized_agent"} and (
            type(args.codex_bridge_port) is not int
            or not 1 <= args.codex_bridge_port <= 65535
            or not os.environ.get(CONTROL_TOKEN_ENV, "").strip()
        ):
            raise ValueError
        if mode == "authorized_agent":
            if os.environ.get("CODEXBRIDGE_WORKFLOW_STRICT_AGENT_ENABLED") != "1":
                raise ValueError
            _provider, registry, _context = build_codex_agent_runtime(
                port=args.codex_bridge_port,
                allowed_roots=tuple(args.allowed_root),
                workspace_root=manifest.workspace_root,
            )
            validate_pinned_agent_definition(definition, registry)
            open_agent_ledger_for_manifest(
                run_dir,
                manifest,
                expected_port=args.codex_bridge_port,
            )
        if mode == "isolated" and args.codex_bridge_port is not None:
            raise ValueError
        return adapter
    except OwsError:
        raise
    except Exception:
        raise OwsError("strict workflow request binding is invalid") from None


def _definition_contains_codex_job(value: object) -> bool:
    return definition_has_call_target(value, "codex-job")


def _strict_failure_reason(error: BaseException) -> str:
    reason_code = getattr(error, "reason_code", None)
    if type(reason_code) is str and reason_code in _STRICT_REASON_CODES:
        return reason_code
    return "strict_execution_uncertain"


def _strict_terminal_envelope(
    adapter: StrictWorkflowVariableRuntimeAdapter,
    *,
    run_id: str,
    status: str,
    reason_code: str | None,
    result: Any = None,
) -> dict[str, Any]:
    if status not in {"succeeded", "failed", "needs_reconciliation"}:
        raise OwsError("strict terminal status is invalid")
    identity = adapter.checkpoint_identity
    envelope: dict[str, Any] = {
        "format_version": 1,
        "status": status,
        "reason_code": reason_code,
        "run_id": run_id,
        "workflow_id": identity["workflow_id"],
        "binding_identity": identity,
    }
    if status == "succeeded":
        if reason_code is not None:
            raise OwsError("strict terminal result is invalid")
        contract = adapter.compiled_contract
        try:
            envelope["result"] = validate_output(contract, result)
        except ContractOutputInvalid:
            raise
        except Exception:
            raise ContractOutputInvalid() from None
    elif result is not None:
        raise OwsError("strict failure result is invalid")
    return envelope


def _write_strict_terminal(run_dir: Path, envelope: dict[str, Any]) -> None:
    path = run_dir / "strict-terminal.json"
    if path.is_symlink():
        raise OwsError("strict terminal evidence is unavailable")
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise OwsError("strict terminal evidence is unavailable") from None
        if canonical_json(existing) != canonical_json(envelope):
            raise OwsError("strict terminal evidence conflicts with the DBOS result")
        return
    _write_json(path, envelope)


def _wait_for_strict_result(
    database_path: Path,
    workflow_id: str,
    timeout: float | None,
    shutdown_file: Path | None,
) -> tuple[Any | None, str | None]:
    deadline = None if timeout is None else time.monotonic() + timeout
    while deadline is None or time.monotonic() < deadline:
        if shutdown_file is not None and shutdown_file.is_file():
            raise WorkerShutdownRequested("workflow worker shutdown was requested")
        status = _read_workflow_status(database_path, workflow_id)
        if status in {"SUCCESS", "SUCCEEDED"}:
            try:
                handle: Any = DBOS.retrieve_workflow(workflow_id)
                return handle.get_result(), None
            except Exception as error:
                return None, _strict_failure_reason(error)
        if status in {"ERROR", "CANCELLED"}:
            try:
                handle = DBOS.retrieve_workflow(workflow_id)
                handle.get_result()
            except Exception as error:
                return None, _strict_failure_reason(error)
            return None, "strict_execution_uncertain"
        time.sleep(0.05)
    raise TimeoutError("strict workflow completion was not confirmed")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    run_dir = args.run_dir.resolve()
    strict_adapter: StrictWorkflowVariableRuntimeAdapter | None = None
    strict_execution_mode = args.strict_runtime_mode
    manifest: dict[str, Any]
    definition_text: str
    input_data: Any
    if strict_execution_mode is not None:
        strict_adapter = _strict_worker_adapter(args, run_dir)
        if strict_execution_mode in {"authorized_codex", "authorized_agent"}:
            os.environ["CODEXBRIDGE_WORKFLOW_BRIDGE_PORT"] = str(args.codex_bridge_port)
        verified_manifest = strict_adapter._verified_manifest
        if verified_manifest is None:
            raise OwsError("strict workflow request binding is invalid")
        manifest = verified_manifest.to_dict()
        definition_text = ""
        input_data = strict_adapter.normalized_input()
    else:
        manifest, definition_text, input_data = load_run_request(
            run_dir,
            allowed_roots=tuple(args.allowed_root),
        )
        if manifest.get("run_format_version") == 2:
            raise OwsError("strict workflow runtime is not supported")
    startup_override = args.startup_file.resolve() if args.startup_file is not None else None
    if startup_override is not None:
        try:
            startup_override.relative_to(run_dir)
        except ValueError as error:
            raise OwsError("--startup-file must be inside the run directory") from error
    database_path = run_dir / "dbos.sqlite"
    control = manifest.get("control", {}) if strict_adapter is None else {}
    if not isinstance(control, dict):
        raise OwsError("manifest control must be an object")
    if strict_adapter is not None:
        codex_job_live = strict_execution_mode in {"authorized_codex", "authorized_agent"}
        manifest_workflow_id = strict_adapter.checkpoint_identity.get("workflow_id")
        workflow_id: str | None = (
            manifest_workflow_id if isinstance(manifest_workflow_id, str) else None
        )
    else:
        codex_job_live = control.get("codex_job_live", False)
        if not isinstance(codex_job_live, bool):
            raise OwsError("manifest codex_job_live must be a boolean")
        if codex_job_live and manifest.get("mode") != "codex-job-live":
            raise OwsError("codex-job live mode does not match the run manifest")
        if manifest.get("mode") == "codex-job-live" and not codex_job_live:
            raise OwsError("codex-job live mode is missing from manifest control")
        manifest_workflow_id = manifest.get("workflow_id")
        workflow_id = manifest_workflow_id if isinstance(manifest_workflow_id, str) else None
    if codex_job_live and strict_adapter is None:
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
    elif strict_adapter is not None and not isinstance(workflow_id, str):
        raise OwsError("strict workflow requires a verified workflow ID")

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
        stage = "worker_setup"
        if args.mode == "start":
            handle: Any
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat,
                args=(heartbeat_stop, run_dir / "initial-worker-heartbeat.jsonl"),
                name="ows-poc-worker-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            stage = "workflow_start"
            if strict_adapter is not None:
                requested_workflow_id = str(workflow_id)
                with SetWorkflowID(requested_workflow_id):
                    handle = DBOS.start_workflow(
                        execute_strict_ows_workflow,
                        run_dir.name,
                        str(run_dir.parent),
                        list(args.allowed_root),
                        str(strict_execution_mode),
                    )
                if handle.workflow_id != requested_workflow_id:
                    raise OwsError("DBOS workflow_id differs from the strict manifest ID")
            else:
                workflow_arguments = (
                    definition_text,
                    cast(Any, manifest["definition_sha256"]),
                    input_data,
                    cast(Any, manifest["input_sha256"]),
                    str(run_dir / "task-events.jsonl"),
                    control,
                    int(cast(Any, manifest.get("max_tasks", 10_000))),
                    True,
                )
                if codex_job_live:
                    requested_workflow_id = str(manifest["workflow_id"])
                    with SetWorkflowID(requested_workflow_id):
                        handle = DBOS.start_workflow(
                            execute_ows_workflow,
                            *workflow_arguments,
                        )
                    if handle.workflow_id != requested_workflow_id:
                        raise OwsError(
                            "DBOS workflow_id differs from the pre-generated manifest ID"
                        )
                else:
                    handle = DBOS.start_workflow(execute_ows_workflow, *workflow_arguments)
            workflow_id = handle.workflow_id
            stage = "workflow_metadata_persist"
            if strict_adapter is None:
                _persist_workflow_id(run_dir, manifest, workflow_id)
            startup_path = run_dir / "start-worker.json"
        else:
            stage = "workflow_recovery"
            recovered_workflow_id = manifest.get("workflow_id")
            if not isinstance(recovered_workflow_id, str) or not recovered_workflow_id:
                raise OwsError("recover mode requires the persisted workflow_id")
            workflow_id = recovered_workflow_id
            stage = "workflow_metadata_persist"
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
            if strict_adapter is not None:
                strict_result, failure_reason = _wait_for_strict_result(
                    database_path,
                    workflow_id,
                    None if strict_execution_mode == "authorized_agent" else args.timeout_seconds,
                    shutdown_file,
                )
                if failure_reason is None:
                    expected_binding = strict_adapter.checkpoint_identity
                    if (
                        type(strict_result) is not dict
                        or set(strict_result)
                        != {
                            "format_version",
                            "status",
                            "reason_code",
                            "run_id",
                            "workflow_id",
                            "binding",
                            "result",
                        }
                        or strict_result.get("format_version") != 1
                        or strict_result.get("status") != "succeeded"
                        or strict_result.get("reason_code") is not None
                        or strict_result.get("run_id") != run_dir.name
                        or strict_result.get("workflow_id") != workflow_id
                        or strict_result.get("binding") != expected_binding
                        or _read_workflow_status(database_path, workflow_id)
                        not in {"SUCCESS", "SUCCEEDED"}
                    ):
                        failure_reason = "strict_execution_uncertain"
                if failure_reason is None:
                    try:
                        assert isinstance(strict_result, dict)
                        terminal = _strict_terminal_envelope(
                            strict_adapter,
                            run_id=run_dir.name,
                            status="succeeded",
                            reason_code=None,
                            result=strict_result["result"],
                        )
                    except ContractOutputInvalid:
                        failure_reason = "output_contract_violation"
                if failure_reason is not None:
                    terminal_status = (
                        "needs_reconciliation"
                        if failure_reason == "strict_execution_uncertain"
                        else "failed"
                    )
                    terminal = _strict_terminal_envelope(
                        strict_adapter,
                        run_id=run_dir.name,
                        status=terminal_status,
                        reason_code=failure_reason,
                    )
                _write_strict_terminal(run_dir, terminal)
                append_lifecycle_event(
                    run_dir,
                    source="worker",
                    event="strict_terminal_persisted",
                    run_id=run_dir.name,
                    workflow_id=workflow_id,
                    launch_id=launch_id,
                    mode=args.mode,
                    pid=worker_identity["pid"],
                    state=terminal["status"],
                    reason_code=terminal.get("reason_code"),
                )
                return 0 if terminal["status"] == "succeeded" else 1
            result = _wait_for_result(
                database_path, workflow_id, args.timeout_seconds, shutdown_file
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
        "worker_setup": "worker_setup_failed",
        "workflow_start": "workflow_start_failed",
        "workflow_recovery": "workflow_recovery_failed",
        "workflow_metadata_persist": "workflow_metadata_persist_failed",
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
