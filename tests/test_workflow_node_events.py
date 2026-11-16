from __future__ import annotations

import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import (
    Interpreter,
    OwsError,
    _InterpreterEvent,
    compute_task_action,
)


def _workflow(items: list[int]) -> dict[str, Any]:
    return {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "node-events",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "outer~/do": {
                    "do": [
                        {
                            "items": {
                                "for": {"in": "${ .items }", "each": "item"},
                                "do": [
                                    {
                                        "copy~/value": {
                                            "set": {"value": "${ $item }"},
                                            "output": {"as": "${ {wrapped: .value} }"},
                                            "export": {"as": "${ {last: .wrapped} }"},
                                        }
                                    },
                                    {
                                        "branch": {
                                            "switch": [
                                                {
                                                    "positive": {
                                                        "when": "${ .wrapped > 0 }",
                                                        "then": "finish",
                                                    }
                                                },
                                                {"fallback": {"then": "exit"}},
                                            ]
                                        }
                                    },
                                    {
                                        "finish": {
                                            "set": {"finished": "${ .wrapped }"},
                                            "then": "exit",
                                        }
                                    },
                                ],
                            }
                        }
                    ]
                }
            }
        ],
    }


def test_interpreter_observer_uses_static_graph_ids_and_dynamic_loop_identity() -> None:
    definition = _workflow([2, -1])
    events: list[Any] = []

    result = Interpreter(definition, observer=events.append).run({"items": [2, -1]})

    assert result == [{"finished": 2}, {"wrapped": -1}]
    graph_node_ids = {node["node_id"] for node in project_workflow_graph(definition)["nodes"]}
    observed_node_ids = {event.node_id for event in events if event.node_id is not None}
    assert observed_node_ids == graph_node_ids

    copy_events = [
        event
        for event in events
        if event.event_type == "task_started" and event.node_id.endswith("/copy~0~1value")
    ]
    assert [event.node_id for event in copy_events] == [
        "/do/0/outer~0~1do/do/0/items/do/0/copy~0~1value",
        "/do/0/outer~0~1do/do/0/items/do/0/copy~0~1value",
    ]
    assert [event.task_identity for event in copy_events] == [
        "do[0].outer~/do.do[0].items.iteration[0].do[0].copy~/value",
        "do[0].outer~/do.do[0].items.iteration[1].do[0].copy~/value",
    ]

    assert [event.branch_class for event in events if event.event_type == "branch_selected"] == [
        "jump",
        "exit",
    ]
    assert [event.case_index for event in events if event.event_type == "branch_selected"] == [0, 1]
    assert [
        event.iteration_ordinal for event in events if event.event_type == "iteration_started"
    ] == [
        0,
        1,
    ]
    assert [
        event.iteration_ordinal for event in events if event.event_type == "iteration_completed"
    ] == [
        0,
        1,
    ]

    last_iteration_completed = max(
        index for index, event in enumerate(events) if event.event_type == "iteration_completed"
    )
    items_completed = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "task_completed"
        and event.task_identity == "do[0].outer~/do.do[0].items"
    )
    child_scope_exited = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "scope_exited" and event.scope_identity == "do[0].outer~/do.do"
    )
    outer_completed = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "task_completed" and event.task_identity == "do[0].outer~/do"
    )
    assert last_iteration_completed < items_completed < child_scope_exited < outer_completed

    first_copy_completed = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "task_completed"
        and event.task_identity.endswith("iteration[0].do[0].copy~/value")
    )
    first_branch_selected = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "branch_selected"
        and event.task_identity.endswith("iteration[0].do[1].branch")
    )
    assert first_copy_completed < first_branch_selected


def test_zero_item_for_has_no_iteration_events_but_completes_parent_task() -> None:
    events: list[Any] = []

    result = Interpreter(_workflow([]), observer=events.append).run({"items": []})

    assert result == []
    assert not any(event.event_type.startswith("iteration_") for event in events)
    assert any(
        event.event_type == "task_completed"
        and event.task_identity == "do[0].outer~/do.do[0].items"
        for event in events
    )


def test_observer_failure_preserves_result_and_six_argument_step_runner_contract() -> None:
    definition = _workflow([3])
    runner_calls: list[tuple[Any, ...]] = []

    def step_runner(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        runner_calls.append((identity, task_name, operation, task_spec, task_input, variables))
        return compute_task_action(operation, task_spec, task_input, variables)

    def broken_observer(_event: Any) -> None:
        raise RuntimeError("PRIVATE-OBSERVER-FAILURE")

    expected = Interpreter(definition).run({"items": [3]})
    actual = Interpreter(
        definition,
        step_runner=step_runner,
        observer=broken_observer,
    ).run({"items": [3]})

    assert actual == expected
    assert runner_calls
    assert all(len(arguments) == 6 for arguments in runner_calls)


@pytest.mark.parametrize("exit_type", [SystemExit, KeyboardInterrupt])
def test_observer_does_not_suppress_process_interrupts(exit_type: type[BaseException]) -> None:
    def interrupted_observer(_event: Any) -> None:
        raise exit_type("observer interrupt")

    with pytest.raises(exit_type, match="observer interrupt"):
        Interpreter(_workflow([1]), observer=interrupted_observer).run({"items": [1]})


def test_live_codex_job_observer_sees_task_without_result_or_uncertain_failure() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "live-hook",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [{"codex": {"call": "codex-job", "with": {"prompt": "PRIVATE-PROMPT-TOKEN"}}}],
    }
    result = {
        "job_id": "job-fake",
        "request_key": "workflow::do[0].codex",
        "status": "SUCCEEDED",
        "result": {"final_text": "PRIVATE-JOB-RESPONSE"},
    }
    events: list[Any] = []

    def fake_codex_step(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        del identity, task_name, operation, task_spec, task_input, variables
        return result

    actual = Interpreter(
        definition,
        step_runner=fake_codex_step,
        codex_job_live=True,
        observer=events.append,
    ).run({})

    assert actual == result
    assert [event.event_type for event in events] == ["task_started", "task_completed"]
    serialized_events = repr(events)
    assert "PRIVATE-PROMPT-TOKEN" not in serialized_events
    assert "PRIVATE-JOB-RESPONSE" not in serialized_events
    assert "job-fake" not in serialized_events

    def maybe_sent_step(
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        task_input: Any,
        variables: dict[str, Any],
    ) -> Any:
        del identity, task_name, operation, task_spec, task_input, variables
        raise RuntimeError("MAYBE_SENT PRIVATE-REMOTE-ERROR")

    uncertain_events: list[Any] = []
    with pytest.raises(RuntimeError, match="MAYBE_SENT PRIVATE-REMOTE-ERROR"):
        Interpreter(
            definition,
            step_runner=maybe_sent_step,
            codex_job_live=True,
            observer=uncertain_events.append,
        ).run({})
    assert [event.event_type for event in uncertain_events] == ["task_started"]


def test_switch_case_ordinal_is_omitted_when_multiple_cases_share_the_directive() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "ambiguous-switch-index",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "select": {
                    "switch": [
                        {"first": {"when": "${ .value > 0 }", "then": "finish"}},
                        {"second": {"when": "${ .value < 10 }", "then": "finish"}},
                        {"fallback": {"then": "exit"}},
                    ]
                }
            },
            {"finish": {"set": {"finished": True}}},
        ],
    }
    events: list[Any] = []

    assert Interpreter(definition, observer=events.append).run({"value": 3}) == {"finished": True}

    selected = next(event for event in events if event.event_type == "branch_selected")
    assert selected.branch_class == "jump"
    assert selected.case_index is None


def test_zero_iteration_does_not_emit_child_scope_or_iteration_events() -> None:
    events: list[Any] = []

    Interpreter(_workflow([]), observer=events.append).run({"items": []})

    assert not any(
        event.event_type in {"scope_entered", "scope_exited"}
        and ".iteration[" in (event.scope_identity or "")
        for event in events
    )


def test_node_event_writer_replay_is_idempotent_and_keeps_iteration_identity(
    tmp_path: Any,
) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    first_events: list[Any] = []
    Interpreter(_workflow([1, 2]), observer=first_events.append).run({"items": [1, 2]})

    writer = NodeEventWriter(run_dir, workflow_id="workflow-a", run_id="run-a")
    for event in first_events:
        writer.observe(event)
    writer.workflow_completed()
    writer.close()

    replay_events: list[Any] = []
    Interpreter(_workflow([1, 2]), observer=replay_events.append).run({"items": [1, 2]})
    replay_writer = NodeEventWriter(run_dir, workflow_id="workflow-a", run_id="run-a")
    for event in replay_events:
        replay_writer.observe(event)
    replay_writer.workflow_completed()
    replay_writer.close()

    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(node_events)")]
        rows = connection.execute(
            "SELECT event_id, event_key, node_id, task_identity, execution_id, event_type, "
            "occurred_at, source, schema_version FROM node_events ORDER BY event_id"
        ).fetchall()

    assert columns == [
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
    ]
    assert len(rows) == len(first_events) + 1
    assert len({row[1] for row in rows}) == len(rows)
    assert [row[0] for row in rows] == sorted(row[0] for row in rows)
    assert {row[5] for row in rows if row[8] == 1}
    assert all(row[7] == "interpreter" for row in rows)
    assert all(row[8] == 1 for row in rows)
    assert rows[-1][5] == "workflow_completed"

    copy_rows = [
        row
        for row in rows
        if row[5] == "task_started" and row[2] == "/do/0/outer~0~1do/do/0/items/do/0/copy~0~1value"
    ]
    assert len(copy_rows) == 2
    assert copy_rows[0][2] == copy_rows[1][2]
    assert copy_rows[0][3] != copy_rows[1][3]
    assert copy_rows[0][4] != copy_rows[1][4]
    assert copy_rows[0][1] != copy_rows[1][1]


def test_node_event_writer_accepts_only_bounded_metadata_and_disables_on_capacity(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import node_events

    run_dir = tmp_path / "run-capacity"
    run_dir.mkdir()
    writer = node_events.NodeEventWriter(run_dir, workflow_id="wf", run_id="run-capacity")
    monkeypatch.setattr(node_events, "MAX_NODE_EVENTS", 1)
    event = _InterpreterEvent(
        "task_started",
        node_id="/do/0/set",
        task_identity="do[0].set",
        operation="set",
    )
    writer.observe(event)
    writer.observe(
        _InterpreterEvent(
            "task_completed",
            node_id="/do/0/set",
            task_identity="do[0].set",
            operation="set",
        )
    )
    writer.workflow_completed()

    assert writer.disabled_reason == "capacity_events"
    writer.close()
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        event_types = [row[0] for row in connection.execute("SELECT event_type FROM node_events")]
    assert event_types == ["task_started"]


def test_node_event_writer_disables_when_event_metadata_exceeds_byte_limit(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import node_events

    run_dir = tmp_path / "run-metadata-limit"
    run_dir.mkdir()
    writer = node_events.NodeEventWriter(run_dir, workflow_id="wf", run_id="run-metadata-limit")
    monkeypatch.setattr(node_events, "MAX_EVENT_JSON_BYTES", 1)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/set",
            task_identity="do[0].set",
            operation="set",
        )
    )
    writer.workflow_completed()

    assert writer.disabled_reason == "event_too_large"
    writer.close()
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0] == 0


def test_node_event_writer_disables_before_exceeding_database_byte_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import node_events

    run_dir = tmp_path / "run-byte-limit"
    run_dir.mkdir()
    writer = node_events.NodeEventWriter(run_dir, workflow_id="wf", run_id="run-byte-limit")
    current_size = (run_dir / "node_events.sqlite").stat().st_size
    monkeypatch.setattr(node_events, "MAX_JOURNAL_BYTES", current_size + 128)
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/set",
            task_identity="do[0].set",
            operation="set",
        )
    )

    assert writer.disabled_reason == "capacity_bytes"
    writer.workflow_completed()
    writer.close()
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0] == 0


def test_node_event_writer_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-symlink"
    run_dir.mkdir()
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"preserve-target")
    link = run_dir / "node_events.sqlite"
    try:
        os.symlink(target, link)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {type(error).__name__}")
    writer = NodeEventWriter(run_dir, workflow_id="wf", run_id="run-symlink")
    assert writer.disabled_reason == "journal_path_unsafe"
    writer.close()
    assert target.read_bytes() == b"preserve-target"


def test_node_event_writer_rejects_reparse_point_before_opening_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-reparse"
    run_dir.mkdir()
    database_path = run_dir / "node_events.sqlite"
    original_lstat = Path.lstat

    def fake_lstat(path: Path) -> Any:
        if path == database_path:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    writer = NodeEventWriter(run_dir, workflow_id="wf", run_id="run-reparse")

    assert writer.disabled_reason == "journal_path_unsafe"
    assert not database_path.exists()
    writer.close()


def test_node_event_writer_rejects_invalid_schema_without_migration(tmp_path: Path) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    invalid_run_dir = tmp_path / "run-invalid-schema"
    invalid_run_dir.mkdir()
    invalid_path = invalid_run_dir / "node_events.sqlite"
    with sqlite3.connect(invalid_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    invalid_writer = NodeEventWriter(
        invalid_run_dir,
        workflow_id="wf",
        run_id="run-invalid-schema",
    )
    assert invalid_writer.disabled_reason == "schema_invalid"
    invalid_writer.close()
    with sqlite3.connect(invalid_path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "keep"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='node_events'"
            ).fetchone()[0]
            == 0
        )


def test_node_event_writer_permission_failure_is_fixed_and_workflow_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from codex_bridge.workflows import node_events

    run_dir = tmp_path / "run-permission"
    run_dir.mkdir()

    def deny_database_access(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("PRIVATE-PATH-AND-ERROR")

    with monkeypatch.context() as patcher:
        patcher.setattr(node_events.sqlite3, "connect", deny_database_access)
        writer = node_events.NodeEventWriter(run_dir, workflow_id="wf", run_id="run-permission")
    caplog.set_level("WARNING", logger="codex_bridge.workflows.node_events")

    result = Interpreter(_workflow([5]), observer=writer.observe).run({"items": [5]})
    writer.workflow_completed()
    writer.close()

    assert result == [{"finished": 5}]
    assert writer.disabled_reason == "write_failed"
    assert "write_failed" in caplog.text
    assert "PRIVATE-PATH-AND-ERROR" not in caplog.text
    assert str(run_dir) not in caplog.text


def test_node_event_writer_busy_lock_disables_without_completion_marker(tmp_path: Path) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-busy"
    run_dir.mkdir()
    writer = NodeEventWriter(run_dir, workflow_id="wf", run_id="run-busy")
    blocker = sqlite3.connect(run_dir / "node_events.sqlite", timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    writer.observe(
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/set",
            task_identity="do[0].set",
            operation="set",
        )
    )
    writer.workflow_completed()
    blocker.execute("ROLLBACK")
    blocker.close()

    assert writer.disabled_reason == "sqlite_busy"
    writer.close()
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0] == 0


def test_local_operation_failure_is_recorded_without_exception_text() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "local-failure",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "loop": {
                    "for": {"in": "${ .items }"},
                    "do": [{"noop": {"set": {"ok": True}}}],
                }
            }
        ],
    }
    events: list[Any] = []

    with pytest.raises(OwsError, match="for.in must produce an array") as failure:
        Interpreter(definition, observer=events.append).run({"items": {"private": True}})

    failed = next(event for event in events if event.event_type == "task_failed")
    assert failed.task_identity == "do[0].loop"
    assert failed.operation == "for"
    assert failed.failure_phase == "operation"
    assert "private" not in repr(events)
    assert str(failure.value) == "for.in must produce an array"


def test_node_event_writer_contention_does_not_interrupt_interpreter(
    tmp_path: Any,
) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-contention"
    run_dir.mkdir()
    writer = NodeEventWriter(run_dir, workflow_id="wf", run_id="run-contention")
    events = [
        _InterpreterEvent(
            "task_started",
            node_id="/do/0/item",
            task_identity=f"do[0].loop.iteration[{index}].do[0].item",
            operation="set",
        )
        for index in range(64)
    ]

    def observe(event: Any) -> None:
        writer.observe(event)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(observe, events))

    result = Interpreter(_workflow([7]), observer=writer.observe).run({"items": [7]})
    assert result == [{"finished": 7}]
    assert writer.disabled_reason is None
    writer.close()
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM node_events").fetchone()[0] == 64 + 17


def test_node_event_writer_rejects_corrupt_database_without_changing_workflow_result(
    tmp_path: Any,
) -> None:
    from codex_bridge.workflows.node_events import NodeEventWriter

    run_dir = tmp_path / "run-corrupt"
    run_dir.mkdir()
    (run_dir / "node_events.sqlite").write_bytes(b"not a sqlite database")
    writer = NodeEventWriter(run_dir, workflow_id="wf", run_id="run-corrupt")
    result = Interpreter(_workflow([4]), observer=writer.observe).run({"items": [4]})

    assert result == [{"finished": 4}]
    assert writer.disabled_reason == "database_invalid"
    writer.workflow_completed()
    writer.close()


def test_fake_worker_records_node_completion_after_step_checkpoint_and_workflow_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.workflows import worker

    class FakeDBOS:
        workflow_id = "workflow-node-events-12345678"

    run_dir = tmp_path / "run-worker"
    run_dir.mkdir()
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "test",
            "name": "worker-node-events",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "record": {
                    "set": {"value": "PRIVATE-NODE-EVENT-VALUE"},
                    "output": {"as": "${ {wrapped: .value} }"},
                    "export": {"as": "${ {latest: .wrapped} }"},
                }
            }
        ],
        "output": {"as": "${ {result: ., context: $context} }"},
    }
    definition_text = yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
    monkeypatch.setattr(worker, "DBOS", FakeDBOS)

    def named_step(_identity: str) -> Any:
        return worker._run_task_step

    monkeypatch.setattr(worker, "_named_step", named_step)
    result = worker.execute_ows_workflow.__wrapped__(
        definition_text,
        worker.sha256_text(definition_text),
        {},
        worker.sha256_json({}),
        str(run_dir / "task-events.jsonl"),
        {},
        10,
        True,
    )

    assert result == {
        "result": {"wrapped": "PRIVATE-NODE-EVENT-VALUE"},
        "context": {"latest": "PRIVATE-NODE-EVENT-VALUE"},
    }
    legacy_events = [
        yaml.safe_load(line)
        for line in (run_dir / "task-events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    step_completed = next(event for event in legacy_events if event["event"] == "step_completed")
    with sqlite3.connect(run_dir / "node_events.sqlite") as connection:
        node_events = connection.execute(
            "SELECT event_type, task_identity, occurred_at FROM node_events ORDER BY event_id"
        ).fetchall()
    node_completed = next(
        row for row in node_events if row[0] == "task_completed" and row[1] == "do[0].record"
    )
    workflow_completed = next(row for row in node_events if row[0] == "workflow_completed")

    assert datetime.fromisoformat(node_completed[2]) > datetime.fromisoformat(
        step_completed["completed_at"]
    )
    assert datetime.fromisoformat(workflow_completed[2]) > datetime.fromisoformat(node_completed[2])
    assert b"PRIVATE-NODE-EVENT-VALUE" not in (run_dir / "node_events.sqlite").read_bytes()

    legacy_run_dir = tmp_path / "run-old-no-journal"
    legacy_run_dir.mkdir()
    worker.execute_ows_workflow.__wrapped__(
        definition_text,
        worker.sha256_text(definition_text),
        {},
        worker.sha256_json({}),
        str(legacy_run_dir / "task-events.jsonl"),
        {},
        10,
    )
    assert not (legacy_run_dir / "node_events.sqlite").exists()
