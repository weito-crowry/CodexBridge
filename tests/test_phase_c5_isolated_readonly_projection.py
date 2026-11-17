from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts import phase_c5_isolated_readonly_projection as projection

FAKE_APPROVAL_ID = "d" * 32
PRIVATE_SENTINEL = "RAW_PRIVATE_PROMPT_RESULT_TOKEN"
EVENT_TIME = "2026-01-01T00:00:00.000+00:00"


@dataclass(frozen=True)
class FakeRow:
    workflow_id: str
    run_id: str
    state: str


@dataclass(frozen=True)
class FakeInspection:
    result: dict[str, Any] | None


@dataclass(frozen=True)
class FakeSnapshot:
    status: str
    terminal: bool
    worker_state: str
    jobs: tuple[object, ...]


def marker_data(approval_id: str = FAKE_APPROVAL_ID) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    for name in projection.RUN_NAMES:
        expected = projection.EXPECTED_JOBS[name]
        runs[name] = {
            "expected_jobs": expected,
            "client_request_id": f"sample-request-{name.lower()}",
            "workflow_id": f"sample-workflow-{name.lower()}",
            "run_id": f"run-{name.lower() * 32}",
            "state": "succeeded",
            "observed_jobs": expected,
        }
    return {
        "schema_version": 1,
        "approval_id": approval_id,
        "approved_head": "a" * 40,
        "runner_source_sha256": "b" * 64,
        "status": "live_runs_pass_gui_not_run",
        "created_at_utc": EVENT_TIME,
        "updated_at_utc": EVENT_TIME,
        "post_count": 3,
        "job_count": 6,
        "runs": runs,
        "events": [{"at_utc": EVENT_TIME, "event": "attempt_started"}],
    }


def write_marker(repository_root: Path, data: dict[str, Any]) -> Path:
    marker_directory = repository_root / "docs" / ".phase_c5_attempt_budgets"
    marker_directory.mkdir(parents=True)
    path = marker_directory / f"{data['approval_id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def create_state_root(
    temp_root: Path,
    marker: projection.ValidatedMarker,
    *,
    candidate_name: str = "codexbridge-phase-b5-synthetic",
    request_states: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    candidate = temp_root / candidate_name
    state_root = candidate / "localappdata" / "CodexBridge" / "workflows"
    state_root.mkdir(parents=True)
    for run in marker.runs:
        (state_root / run.run_id).mkdir()
    database = state_root / "requests.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE workflow_requests (workflow_id TEXT, run_id TEXT, state TEXT)"
        )
        states = request_states or {name: "succeeded" for name in projection.RUN_NAMES}
        for run in marker.runs:
            connection.execute(
                "INSERT INTO workflow_requests(workflow_id, run_id, state) VALUES (?, ?, ?)",
                (run.workflow_id, run.run_id, states[run.name]),
            )
    return candidate, state_root


def build_environment(
    tmp_path: Path,
    *,
    request_states: dict[str, str] | None = None,
) -> tuple[Path, Path, Path, projection.ValidatedMarker]:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    data = marker_data()
    write_marker(repository_root, data)
    marker = projection.validate_marker_data(data, FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    _, state_root = create_state_root(temp_root, marker, request_states=request_states)
    return repository_root, temp_root, state_root, marker


def dependencies_for(
    marker: projection.ValidatedMarker,
    *,
    statuses: dict[str, str] | None = None,
    coverage: dict[str, str] | None = None,
    event_count: int = 4,
    failures: dict[str, Callable[..., Any]] | None = None,
) -> projection.ReadOnlyProducts:
    statuses = statuses or {name: "succeeded" for name in projection.RUN_NAMES}
    coverage = coverage or {name: "complete" for name in projection.RUN_NAMES}
    failures = failures or {}
    by_workflow = {run.workflow_id: run for run in marker.runs}
    calls: dict[str, int] = {}

    def maybe_fail(name: str) -> None:
        calls[name] = calls.get(name, 0) + 1
        failure = failures.get(name)
        if failure is not None:
            failure()

    def find_workflow(_state_root: Path, workflow_id: str) -> FakeRow:
        maybe_fail("find_workflow")
        run = by_workflow[workflow_id]
        row_state = "running" if run.name == "C" else "succeeded"
        return FakeRow(workflow_id, run.run_id, row_state)

    def inspect_run(_state_root: Path, row: FakeRow) -> FakeInspection:
        maybe_fail("inspect_run")
        return FakeInspection({"private": PRIVATE_SENTINEL})

    def snapshot_from_inspection(row: FakeRow, _inspection: FakeInspection) -> FakeSnapshot:
        maybe_fail("snapshot")
        run = by_workflow[row.workflow_id]
        status = statuses[run.name]
        count = run.expected_jobs if run.name != "A" else 0
        return FakeSnapshot(
            status=status,
            terminal=status in {"succeeded", "failed", "failed_to_start"},
            worker_state="completed" if status == "succeeded" else status,
            jobs=tuple(object() for _ in range(count)),
        )

    def project_tasks(
        _state_root: Path, row: FakeRow, _inspection: FakeInspection
    ) -> dict[str, Any]:
        maybe_fail("tasks_projection")
        run = by_workflow[row.workflow_id]
        state = statuses[run.name]
        return {
            "workflow_status": state,
            "coverage": coverage[run.name],
            "coverage_reason": (
                "workflow_not_confirmed" if coverage[run.name] == "partial" else None
            ),
            "counts": {
                "nodes": 3,
                "edges": 2,
                "executions": run.expected_jobs,
            },
            "nodes": [{"node_id": PRIVATE_SENTINEL}],
        }

    def project_events(
        _state_root: Path,
        _row: FakeRow,
        _inspection: FakeInspection,
        *,
        after_id: int,
        limit: int,
    ) -> dict[str, Any]:
        maybe_fail("events_projection")
        assert after_id == 0
        assert limit == 100
        return {
            "coverage": "partial",
            "events": [
                {"event_id": index + 1, "node_id": PRIVATE_SENTINEL}
                for index in range(min(event_count, limit))
            ],
            "latest_event_id": max(event_count, 0),
            "has_more": event_count > limit,
        }

    return projection.ReadOnlyProducts(
        find_workflow=find_workflow,
        inspect_run=inspect_run,
        snapshot_from_inspection=snapshot_from_inspection,
        project_tasks=project_tasks,
        project_events=project_events,
    )


def inspect_fake(
    repository_root: Path,
    temp_root: Path,
    marker: projection.ValidatedMarker,
    products: projection.ReadOnlyProducts,
    **kwargs: Any,
) -> dict[str, Any]:
    return projection.inspect_attempt(
        FAKE_APPROVAL_ID,
        repository_root=repository_root,
        temp_root=temp_root,
        products=products,
        **kwargs,
    )


def test_marker_success_contract_accepts_runs_dict_and_expected_counts() -> None:
    parsed = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)

    assert [run.name for run in parsed.runs] == ["A", "B", "C"]
    assert [run.expected_jobs for run in parsed.runs] == [1, 2, 3]
    assert [run.observed_jobs for run in parsed.runs] == [1, 2, 3]


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update({"runs": [value["runs"]["A"]]}),
        lambda value: value.update({"status": "UNKNOWN"}),
        lambda value: value.update({"post_count": True}),
        lambda value: value.update({"job_count": 5}),
        lambda value: value["runs"]["B"].update({"expected_jobs": 1}),
        lambda value: value["runs"]["C"].update({"observed_jobs": 2}),
        lambda value: value["runs"]["A"].update({"run_id": "../outside"}),
        lambda value: value["runs"]["B"].update({"workflow_id": value["runs"]["A"]["workflow_id"]}),
        lambda value: value.update({"runner_source_sha256": "B" * 64}),
        lambda value: value.update({"events": "not-an-array"}),
    ],
)
def test_marker_contract_rejects_stale_wrong_shape_or_duplicate_identity(
    change: Callable[[dict[str, Any]], None],
) -> None:
    data = marker_data()
    change(data)

    with pytest.raises(projection.InspectionFailure) as error:
        projection.validate_marker_data(data, FAKE_APPROVAL_ID)

    assert error.value.stage == "marker_validation"
    assert error.value.code


def test_marker_rejects_duplicate_json_keys() -> None:
    with pytest.raises(projection.InspectionFailure) as error:
        projection._parse_marker_bytes(b'{"schema_version":1,"schema_version":1}')

    assert error.value.stage == "marker_parse"


def test_unique_candidate_match_is_readonly_and_exposes_only_request_states(
    tmp_path: Path,
) -> None:
    data = marker_data()
    marker = projection.validate_marker_data(data, FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    _, state_root = create_state_root(
        temp_root,
        marker,
        request_states={"A": "succeeded", "B": "succeeded", "C": "running"},
    )
    database = state_root / "requests.sqlite"
    before = database.read_bytes()
    connections: list[tuple[str, bool]] = []
    original_connect = sqlite3.connect

    class ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection, uri: str) -> None:
            self.connection = connection
            self.uri = uri
            self.query_only_enabled = False

        def execute(self, sql: str, parameters: tuple[str, ...] = ()) -> Any:
            if sql.strip().upper() == "PRAGMA QUERY_ONLY=ON":
                self.query_only_enabled = True
            return self.connection.execute(sql, parameters)

        def close(self) -> None:
            connections.append((self.uri, self.query_only_enabled))
            self.connection.close()

    def connect(database_uri: str, **kwargs: Any) -> ConnectionProxy:
        return ConnectionProxy(original_connect(database_uri, **kwargs), database_uri)

    match = projection.find_unique_state_root(temp_root, marker, connect=connect)

    assert match.state_root == state_root
    assert match.request_states == (
        ("A", "succeeded"),
        ("B", "succeeded"),
        ("C", "running"),
    )
    assert len(connections) == 1
    assert connections[0][0].endswith("?mode=ro")
    assert connections[0][1] is True
    assert database.read_bytes() == before


def test_zero_or_multiple_exact_candidates_block(tmp_path: Path) -> None:
    marker = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)
    empty_root = tmp_path / "empty-temp"
    empty_root.mkdir()
    with pytest.raises(projection.InspectionFailure) as no_match:
        projection.find_unique_state_root(empty_root, marker)
    assert no_match.value.stage == "candidate_match"

    temp_root = tmp_path / "multiple-temp"
    temp_root.mkdir()
    create_state_root(temp_root, marker, candidate_name="codexbridge-phase-b5-one")
    create_state_root(temp_root, marker, candidate_name="codexbridge-phase-b5-two")
    with pytest.raises(projection.InspectionFailure) as multiple:
        projection.find_unique_state_root(temp_root, marker)
    assert multiple.value.stage == "candidate_match"


def test_duplicate_request_rows_do_not_match(tmp_path: Path) -> None:
    marker = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    _, state_root = create_state_root(temp_root, marker)
    first = marker.runs[0]
    with sqlite3.connect(state_root / "requests.sqlite") as connection:
        connection.execute(
            "INSERT INTO workflow_requests(workflow_id, run_id, state) VALUES (?, ?, ?)",
            (first.workflow_id, first.run_id, "succeeded"),
        )

    with pytest.raises(projection.InspectionFailure) as error:
        projection.find_unique_state_root(temp_root, marker)

    assert error.value.stage == "candidate_match"


def test_nonmatching_and_nested_candidates_are_not_selected(tmp_path: Path) -> None:
    marker = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    nested_parent = temp_root / "unrelated"
    nested_parent.mkdir()
    create_state_root(
        nested_parent,
        marker,
        candidate_name="codexbridge-phase-b5-nested",
    )
    wrong_candidate = temp_root / "codexbridge-phase-b5-wrong"
    wrong_state_root = wrong_candidate / "localappdata" / "CodexBridge" / "workflows"
    wrong_state_root.mkdir(parents=True)
    with sqlite3.connect(wrong_state_root / "requests.sqlite") as connection:
        connection.execute(
            "CREATE TABLE workflow_requests (workflow_id TEXT, run_id TEXT, state TEXT)"
        )
        connection.execute(
            "INSERT INTO workflow_requests VALUES (?, ?, ?)",
            ("different-workflow", "run-" + "e" * 32, "succeeded"),
        )

    with pytest.raises(projection.InspectionFailure) as error:
        projection.find_unique_state_root(temp_root, marker)
    assert error.value.stage == "candidate_match"


def test_missing_request_database_is_not_created(tmp_path: Path) -> None:
    marker = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    candidate = temp_root / "codexbridge-phase-b5-no-db"
    state_root = candidate / "localappdata" / "CodexBridge" / "workflows"
    state_root.mkdir(parents=True)
    database = state_root / "requests.sqlite"

    with pytest.raises(projection.InspectionFailure):
        projection.find_unique_state_root(temp_root, marker)

    assert not database.exists()


def test_marker_file_swap_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repo"
    original_marker = write_marker(repository_root, marker_data())
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}", encoding="utf-8")
    original_open = projection.os.open

    def swapped_open(path: str | bytes | Path, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == original_marker:
            return original_open(replacement, flags, *args, **kwargs)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(projection.os, "open", swapped_open)

    with pytest.raises(projection.InspectionFailure) as error:
        projection._read_marker(repository_root, FAKE_APPROVAL_ID)

    assert error.value.stage == "marker_parse"
    assert error.value.code == "file_changed_during_read"


def test_oversized_marker_is_blocked_without_temp_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root = tmp_path / "repo"
    marker_directory = repository_root / "docs" / ".phase_c5_attempt_budgets"
    marker_directory.mkdir(parents=True)
    marker_path = marker_directory / f"{FAKE_APPROVAL_ID}.json"
    marker_path.write_bytes(b" " * (projection.MAX_MARKER_BYTES + 1))

    def unexpected_temp_root() -> Path:
        raise AssertionError("an oversized marker must stop before Temp")

    monkeypatch.setattr(projection, "_windows_temp_root", unexpected_temp_root)

    result = projection.inspect_attempt(
        FAKE_APPROVAL_ID,
        repository_root=repository_root,
    )

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "marker_parse"
    assert result["code"] == "file_too_large"


def test_reparse_candidate_is_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = projection.validate_marker_data(marker_data(), FAKE_APPROVAL_ID)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    candidate, _ = create_state_root(temp_root, marker)
    original = projection._is_reparse_point

    def fake_reparse(path: Path, result: Any = None) -> bool:
        return path == candidate or original(path, result)

    monkeypatch.setattr(projection, "_is_reparse_point", fake_reparse)

    with pytest.raises(projection.InspectionFailure) as error:
        projection.find_unique_state_root(temp_root, marker)
    assert error.value.stage == "candidate_match"


def test_reparse_run_file_blocks_before_product_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, temp_root, state_root, marker = build_environment(tmp_path)
    manifest = state_root / marker.runs[0].run_id / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    original = projection._is_reparse_point

    def fake_reparse(path: Path, result: Any = None) -> bool:
        return path == manifest or original(path, result)

    monkeypatch.setattr(projection, "_is_reparse_point", fake_reparse)
    dependencies = dependencies_for(marker)

    result = inspect_fake(repository_root, temp_root, marker, dependencies)

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "candidate_match"
    assert result["code"] == "run_file_unsafe"


def test_inspect_projects_c_raw_running_as_public_succeeded_without_leakage(
    tmp_path: Path,
) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(
        tmp_path,
        request_states={"A": "succeeded", "B": "succeeded", "C": "running"},
    )
    dependencies = dependencies_for(
        marker, coverage={"A": "complete", "B": "partial", "C": "unavailable"}
    )

    result = inspect_fake(repository_root, temp_root, marker, dependencies)
    serialized = json.dumps(result)

    assert result["status"] == "PASS"
    assert result["provenance"]["exact_matches"] == 1
    assert [run["case"] for run in result["runs"]] == ["A", "B", "C"]
    assert result["runs"][2]["request_state"] == "running"
    assert result["runs"][2]["public_status"] == "succeeded"
    assert result["runs"][2]["terminal"] is True
    assert result["runs"][2]["worker_state"] == "completed"
    assert result["runs"][0]["public_jobs_count"] is None
    assert result["runs"][0]["task_coverage_reason"] == "none"
    assert result["runs"][1]["public_jobs_count"] == 2
    assert result["runs"][2]["public_jobs_count"] == 3
    assert result["runs"][0]["task_coverage"] == "complete"
    assert result["runs"][1]["task_coverage_reason"] == "workflow_not_confirmed"
    assert result["runs"][2]["task_coverage"] == "unavailable"
    assert result["runs"][2]["events_returned"] == 4
    assert result["runs"][2]["latest_event_id"] == 4
    assert result["runs"][2]["has_more"] is False
    assert PRIVATE_SENTINEL not in serialized
    assert FAKE_APPROVAL_ID not in serialized
    assert str(repository_root) not in serialized
    for run in result["runs"]:
        assert set(run) == {
            "case",
            "request_state",
            "public_status",
            "terminal",
            "worker_state",
            "has_result",
            "public_jobs_count",
            "task_workflow_status",
            "task_coverage",
            "task_coverage_reason",
            "counts",
            "event_coverage",
            "events_returned",
            "latest_event_id",
            "has_more",
        }


def test_event_page_is_capped_at_one_hundred(tmp_path: Path) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)
    dependencies = dependencies_for(marker, event_count=150)

    result = inspect_fake(repository_root, temp_root, marker, dependencies)

    assert result["status"] == "PASS"
    assert all(run["events_returned"] == 100 for run in result["runs"])
    assert all(run["latest_event_id"] == 150 for run in result["runs"])
    assert all(run["has_more"] is True for run in result["runs"])


def test_event_page_over_limit_is_blocked_by_safe_formatter(tmp_path: Path) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)
    dependencies = dependencies_for(marker, event_count=101)
    base_project_events = dependencies.project_events

    def oversized_page(
        state_root: Path,
        row: FakeRow,
        inspection: FakeInspection,
        *,
        after_id: int,
        limit: int,
    ) -> dict[str, Any]:
        page = base_project_events(
            state_root,
            row,
            inspection,
            after_id=after_id,
            limit=limit,
        )
        page["events"].append({"event_id": 101, "node_id": PRIVATE_SENTINEL})
        return page

    dependencies = projection.ReadOnlyProducts(
        find_workflow=dependencies.find_workflow,
        inspect_run=dependencies.inspect_run,
        snapshot_from_inspection=dependencies.snapshot_from_inspection,
        project_tasks=dependencies.project_tasks,
        project_events=oversized_page,
    )

    result = inspect_fake(repository_root, temp_root, marker, dependencies)

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "safe_formatter"


@pytest.mark.parametrize(
    ("dependency_name", "stage"),
    [
        ("find_workflow", "find_workflow"),
        ("inspect_run", "inspect_run"),
        ("snapshot", "snapshot"),
        ("tasks_projection", "tasks_projection"),
        ("events_projection", "events_projection"),
    ],
)
def test_product_typeerrors_are_reported_by_fixed_stage_only(
    tmp_path: Path,
    dependency_name: str,
    stage: str,
) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)

    def raise_type_error() -> None:
        raise TypeError(f"{PRIVATE_SENTINEL} {repository_root}")

    dependencies = dependencies_for(marker, failures={dependency_name: raise_type_error})
    result = inspect_fake(repository_root, temp_root, marker, dependencies)
    serialized = json.dumps(result)

    assert result["status"] == "BLOCKED"
    assert result["stage"] == stage
    assert result["exception_class"] == "TypeError"
    assert PRIVATE_SENTINEL not in serialized
    assert str(repository_root) not in serialized


def test_safe_formatter_attribute_error_is_separate_from_product_errors(
    tmp_path: Path,
) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)
    dependencies = dependencies_for(marker)

    class InvalidSnapshot:
        status = "succeeded"
        worker_state = "completed"
        jobs: tuple[object, ...] = ()

        @property
        def terminal(self) -> bool:
            raise AttributeError(PRIVATE_SENTINEL)

    def bad_snapshot(row: FakeRow, inspection: FakeInspection) -> InvalidSnapshot:
        del row, inspection
        return InvalidSnapshot()

    dependencies = projection.ReadOnlyProducts(
        find_workflow=dependencies.find_workflow,
        inspect_run=dependencies.inspect_run,
        snapshot_from_inspection=bad_snapshot,
        project_tasks=dependencies.project_tasks,
        project_events=dependencies.project_events,
    )
    result = inspect_fake(repository_root, temp_root, marker, dependencies)
    serialized = json.dumps(result)

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "safe_formatter"
    assert result["exception_class"] == "AttributeError"
    assert PRIVATE_SENTINEL not in serialized


def test_formatter_rejects_non_int_counts_and_bool_is_not_an_int(
    tmp_path: Path,
) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)
    dependencies = dependencies_for(marker)
    project_tasks = dependencies.project_tasks

    def bad_counts(state_root: Path, row: FakeRow, inspection: FakeInspection) -> dict[str, Any]:
        value = project_tasks(state_root, row, inspection)
        value["counts"]["nodes"] = True
        return value

    dependencies = projection.ReadOnlyProducts(
        find_workflow=dependencies.find_workflow,
        inspect_run=dependencies.inspect_run,
        snapshot_from_inspection=dependencies.snapshot_from_inspection,
        project_tasks=bad_counts,
        project_events=dependencies.project_events,
    )
    result = inspect_fake(repository_root, temp_root, marker, dependencies)

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "safe_formatter"
    assert result["code"] == "count_invalid"


def test_a_failure_stops_before_later_runs(tmp_path: Path) -> None:
    repository_root, temp_root, _state_root, marker = build_environment(tmp_path)
    calls: list[str] = []

    def find_workflow(_state_root: Path, workflow_id: str) -> FakeRow:
        calls.append(workflow_id)
        if workflow_id == marker.runs[0].workflow_id:
            raise OSError(PRIVATE_SENTINEL)
        return FakeRow(workflow_id, "run-" + "e" * 32, "succeeded")

    dependencies = dependencies_for(marker)
    dependencies = projection.ReadOnlyProducts(
        find_workflow=find_workflow,
        inspect_run=dependencies.inspect_run,
        snapshot_from_inspection=dependencies.snapshot_from_inspection,
        project_tasks=dependencies.project_tasks,
        project_events=dependencies.project_events,
    )

    result = inspect_fake(repository_root, temp_root, marker, dependencies)

    assert result["stage"] == "find_workflow"
    assert result["exception_class"] == "OSError"
    assert calls == [marker.runs[0].workflow_id]


def test_invalid_marker_does_not_scan_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    data = marker_data()
    data["runs"] = [data["runs"]["A"]]
    write_marker(repository_root, data)

    def unexpected_scan(*_args: Any, **_kwargs: Any) -> projection.CandidateMatch:
        raise AssertionError("temp scan must not run for an invalid marker")

    monkeypatch.setattr(projection, "find_unique_state_root", unexpected_scan)

    result = projection.inspect_attempt(
        FAKE_APPROVAL_ID,
        repository_root=repository_root,
        temp_root=tmp_path,
        products=projection.ReadOnlyProducts(*(lambda *_args, **_kwargs: None for _ in range(5))),
    )

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "marker_validation"


def test_prepare_uses_synthetic_fixture_without_marker_or_temp_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependencies = projection.ReadOnlyProducts(*(lambda *_args, **_kwargs: None for _ in range(5)))
    monkeypatch.setattr(projection, "_load_products", lambda: dependencies)

    def unexpected_access(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prepare must not inspect marker or temp")

    monkeypatch.setattr(projection, "_read_marker", unexpected_access)
    monkeypatch.setattr(projection, "_windows_temp_root", unexpected_access)
    monkeypatch.setattr(projection, "find_unique_state_root", unexpected_access)
    monkeypatch.setattr(socket.socket, "connect", unexpected_access)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected_access)
    monkeypatch.setattr(socket, "create_connection", unexpected_access)
    monkeypatch.setattr(subprocess, "Popen", unexpected_access)

    result = projection.prepare()

    assert result == {
        "status": "PASS",
        "mode": "prepare",
        "fixture": "synthetic",
        "live_data_access": False,
    }


def test_cli_no_arguments_shows_help_only(capsys: pytest.CaptureFixture[str]) -> None:
    assert projection.main([]) == 0
    captured = capsys.readouterr()
    assert "--prepare" in captured.out
    assert "--inspect" in captured.out
    assert captured.err == ""


def test_cli_prepare_outputs_only_synthetic_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dependencies = projection.ReadOnlyProducts(*(lambda *_args, **_kwargs: None for _ in range(5)))
    monkeypatch.setattr(projection, "_load_products", lambda: dependencies)
    monkeypatch.setattr(projection, "_is_windows_native", lambda: True)

    assert projection.main(["--prepare"]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out) == {
        "status": "PASS",
        "mode": "prepare",
        "fixture": "synthetic",
        "live_data_access": False,
    }
    assert captured.err == ""


def test_cli_inspect_routes_to_mocked_audit_without_leaking_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(projection, "_is_windows_native", lambda: True)
    monkeypatch.setattr(
        projection,
        "inspect_attempt",
        lambda _approval_id: {"status": "PASS", "stage": "complete", "runs": []},
    )

    assert projection.main(["--inspect", "--approval-id", FAKE_APPROVAL_ID]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out) == {
        "status": "PASS",
        "stage": "complete",
        "runs": [],
    }
    assert FAKE_APPROVAL_ID not in captured.out
    assert captured.err == ""


def test_cli_rejects_invalid_arguments_without_echoing_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = projection.main(["--inspect", "--approval-id", "not-a-safe-id"])
    captured = capsys.readouterr()

    assert status == 2
    assert "not-a-safe-id" not in captured.out
    assert captured.err == ""
