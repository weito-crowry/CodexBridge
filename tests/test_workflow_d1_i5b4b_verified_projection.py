from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import codex_bridge.workflows.service as workflow_service
import codex_bridge.workflows.task_projection as task_projection
from codex_bridge.workflows.service import WorkflowService
from tests.test_workflow_d1_i5b2_reader_gate import (
    _PROMPT_SENTINEL,
    _SECRET_SENTINEL,
    _inspection,
    _ReadOnlyWorkerManager,
    _request_row,
    _strict_fixture,
)

_TWO_JOB_YAML = (
    "document:\n"
    '  dsl: "1.0.3"\n'
    "  namespace: codexbridge\n"
    "  name: i5b4b-two-job-reader\n"
    '  version: "1.0.0"\n'
    "evaluate:\n"
    "  language: jq\n"
    "  mode: strict\n"
    "do:\n"
    "  - first:\n"
    "      call: codex-job\n"
    "      with:\n"
    '        prompt: "first private prompt"\n'
    "  - second:\n"
    "      call: codex-job\n"
    "      with:\n"
    '        prompt: "second private prompt: "\n'
    '        prompt_from: "${ .result.final_text }"\n'
)
_FAKE_RESULT_SENTINEL = "private-fake-runtime-result-b4b"


@pytest.mark.asyncio
async def test_service_verified_strict_status_and_result_ignore_runtime_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _strict_fixture(
        tmp_path,
        max_live_codex_jobs=2,
        source_yaml=_TWO_JOB_YAML,
    )
    without_allowed_roots = workflow_service._inspect_run(
        fixture.state_root, _request_row(fixture.manifest), allowed_roots=()
    )
    assert without_allowed_roots.status == "needs_reconciliation"
    assert without_allowed_roots.max_live_codex_jobs == 1
    service = WorkflowService(
        _ReadOnlyWorkerManager(fixture.state_root, fixture.workspace), codex_bridge_port=18001
    )
    (fixture.run_dir / "worker-entry-status.json").write_text(
        '{"state":"completed","exit_code":0,"error_code":null}', encoding="utf-8"
    )
    (fixture.run_dir / "codex_jobs.sqlite").write_bytes(b"completed fake job ledger")
    (fixture.run_dir / "node_events.sqlite").write_bytes(b"completed fake node journal")
    (fixture.run_dir / "dbos.sqlite").write_bytes(b"fake DBOS success")
    (fixture.run_dir / "result.json").write_text(
        json.dumps(
            {
                "workflow_id": fixture.manifest["workflow_id"],
                "definition_sha256": fixture.manifest["definition_sha256"],
                "input_sha256": fixture.manifest["input_sha256"],
                "result": {"final_text": _FAKE_RESULT_SENTINEL},
            }
        ),
        encoding="utf-8",
    )

    def fail_runtime_read(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("strict reader attempted to consume legacy runtime evidence")

    original_read_file = workflow_service._read_regular_file

    def guarded_read_file(path: Path, *, limit: int) -> bytes:
        if path.name in {"worker-entry-status.json", "result.json"}:
            pytest.fail("strict status/result reader opened a legacy runtime artifact")
        return original_read_file(path, limit=limit)

    monkeypatch.setattr(workflow_service, "_read_regular_file", guarded_read_file)
    monkeypatch.setattr(workflow_service, "_read_codex_jobs", fail_runtime_read)
    monkeypatch.setattr(workflow_service, "_read_codex_job", fail_runtime_read)
    monkeypatch.setattr(workflow_service, "_read_dbos_status", fail_runtime_read)
    monkeypatch.setattr(task_projection, "_read_journal", fail_runtime_read)
    monkeypatch.setattr(task_projection, "_read_jobs", fail_runtime_read)
    monkeypatch.setattr(task_projection, "_read_dbos_status", fail_runtime_read)

    status = await service.get_workflow(fixture.manifest["workflow_id"])
    result = await service.get_result(fixture.manifest["workflow_id"])
    tasks = await service.get_workflow_tasks(fixture.manifest["workflow_id"])
    events = await service.get_workflow_task_events(
        fixture.manifest["workflow_id"], after_id=7, limit=10
    )

    assert status.status == "needs_reconciliation"
    assert status.worker_state == "unknown"
    assert status.terminal is False
    assert status.job_id is None and status.thread_id is None
    assert status.max_live_codex_jobs == 2
    assert result.workflow.status == "needs_reconciliation"
    assert result.workflow.worker_state == "unknown"
    assert result.workflow.terminal is False
    assert result.workflow.max_live_codex_jobs == 2
    assert result.result is None
    assert tasks["coverage"] == "partial"
    assert tasks["coverage_reason"] == "workflow_not_confirmed"
    assert [node["node_id"] for node in tasks["nodes"]] == ["/do/0/first", "/do/1/second"]
    assert all(node["state"] == "unknown" for node in tasks["nodes"])
    assert all(node["completion_observed"] is False for node in tasks["nodes"])
    assert all(node["executions"] == [] for node in tasks["nodes"])
    assert all(
        set(node)
        == {
            "node_id",
            "operation",
            "parent_scope",
            "order",
            "state",
            "completion_observed",
            "executions",
        }
        for node in tasks["nodes"]
    )
    assert all(set(edge) == {"source", "target", "kind"} for edge in tasks["edges"])
    assert tasks["counts"]["executions"] == 0
    assert tasks["latest_event_id"] == 0
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "workflow_not_confirmed"
    assert events["events"] == []
    assert events["latest_event_id"] == 0
    assert events["next_after_id"] == 7
    output = json.dumps(
        {
            "status": status.status,
            "worker_state": status.worker_state,
            "result": result.result,
            "tasks": tasks,
            "events": events,
        },
        ensure_ascii=False,
    )
    assert _SECRET_SENTINEL not in output
    assert _PROMPT_SENTINEL not in output
    assert _FAKE_RESULT_SENTINEL not in output


@pytest.mark.parametrize(
    "invalid_case",
    ["allowed_root", "reservation_binding", "input", "variables_corrupt", "variables_binding"],
)
def test_strict_projection_rejects_invalid_reservation_or_snapshot(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    fixture = _strict_fixture(tmp_path)
    allowed_roots = (str(fixture.workspace),)
    if invalid_case == "allowed_root":
        outside = tmp_path / "outside"
        outside.mkdir()
        allowed_roots = (str(outside),)
    elif invalid_case == "reservation_binding":
        with sqlite3.connect(fixture.state_root / "requests.sqlite") as connection:
            connection.execute(
                "DELETE FROM workflow_request_contract_bindings WHERE run_id = ?",
                (fixture.manifest["run_id"],),
            )
    elif invalid_case == "input":
        input_path = fixture.run_dir / "input.json"
        input_path.write_bytes(input_path.read_bytes() + b" ")
    elif invalid_case == "variables_corrupt":
        (fixture.run_dir / "variables.sqlite").write_bytes(b"not a sqlite database")
    elif invalid_case == "variables_binding":
        with sqlite3.connect(fixture.run_dir / "variables.sqlite") as connection:
            connection.execute(
                "UPDATE run_binding SET normalized_input_sha256 = ? WHERE run_id = ?",
                ("f" * 64, fixture.manifest["run_id"]),
            )

    row = _request_row(fixture.manifest)
    tasks = task_projection.project_workflow_tasks(
        fixture.state_root, row, _inspection(), allowed_roots=allowed_roots
    )
    events = task_projection.project_workflow_task_events(
        fixture.state_root,
        row,
        _inspection(),
        after_id=0,
        limit=10,
        allowed_roots=allowed_roots,
    )

    assert tasks["coverage"] == "unavailable"
    assert tasks["coverage_reason"] == "run_integrity_invalid"
    assert tasks["nodes"] == [] and tasks["edges"] == []
    assert events["coverage"] == "unavailable"
    assert events["coverage_reason"] == "run_integrity_invalid"
    assert events["events"] == []
    rendered = json.dumps((tasks, events), ensure_ascii=False)
    assert _SECRET_SENTINEL not in rendered
    assert _PROMPT_SENTINEL not in rendered


def test_strict_projection_uses_verified_pinned_definition_without_rereading_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _strict_fixture(tmp_path)
    row = _request_row(fixture.manifest)
    original = task_projection.verify_strict_reserved_readonly
    source_path = fixture.run_dir / "workflow.yaml"

    def verify_then_replace_source(*args: Any, **kwargs: Any) -> Any:
        verification = original(*args, **kwargs)
        source_path.write_text("unverified replacement after verified read\n", encoding="utf-8")
        return verification

    monkeypatch.setattr(
        task_projection, "verify_strict_reserved_readonly", verify_then_replace_source
    )

    projection = task_projection.project_workflow_tasks(
        fixture.state_root,
        row,
        _inspection(),
        allowed_roots=(str(fixture.workspace),),
    )

    assert projection["coverage"] == "partial"
    assert projection["coverage_reason"] == "workflow_not_confirmed"
    assert projection["nodes"][0]["node_id"] == "/do/0/first"


def test_strict_projection_reports_graph_invalid_for_unsupported_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _strict_fixture(tmp_path)

    def unsupported_graph(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("strict-only node is not supported by the legacy graph validator")

    monkeypatch.setattr(task_projection, "project_workflow_graph", unsupported_graph)
    row = _request_row(fixture.manifest)
    tasks = task_projection.project_workflow_tasks(
        fixture.state_root,
        row,
        _inspection(),
        allowed_roots=(str(fixture.workspace),),
    )
    events = task_projection.project_workflow_task_events(
        fixture.state_root,
        row,
        _inspection(),
        after_id=3,
        limit=10,
        allowed_roots=(str(fixture.workspace),),
    )

    assert tasks["coverage"] == "partial"
    assert tasks["coverage_reason"] == "graph_invalid"
    assert tasks["nodes"] == [] and tasks["edges"] == []
    assert tasks["contract_version"] == 1
    assert tasks["contract_sha256"] == fixture.manifest["contract_sha256"]
    assert tasks["reason_code"] == "strict_execution_uncertain"
    assert events["coverage"] == "partial"
    assert events["coverage_reason"] == "graph_invalid"
    assert events["events"] == []
    assert events["next_after_id"] == 3
    assert events["binding_digest"] == fixture.manifest["binding_digest"]
    assert events["reason_code"] == "strict_execution_uncertain"
