from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows import interpreter
from codex_bridge.workflows.codex_job_adapter import (
    CodexJobAdapter,
    CodexJobConflictError,
    CodexJobReconciliationRequiredError,
    CodexJobRequestError,
)
from codex_bridge.workflows.http_client import (
    CONTROL_TOKEN_ENV,
    CodexHTTPClient,
    CodexHTTPError,
)
from codex_bridge.workflows.interpreter import (
    SAFE_CODEX_JOB_PROMPT,
    OwsError,
)
from codex_bridge.workflows.worker import _persist_workflow_id

_JOB_COLUMNS = (
    "job_id",
    "request_key",
    "workflow_id",
    "task_identity",
    "request_digest",
    "request_json",
    "state",
    "client_request_id",
    "title_marker",
    "agent_cwd",
    "created_at",
    "updated_at",
    "thread_id",
    "turn_id",
    "remote_state",
    "receipt_json",
    "error",
)
_PRIVATE_PROMPT = "PRIVATE-PROMPT-TOKEN-FIXTURE"


def _legacy_job_row(
    number: int,
    root: Path,
    *,
    workflow_id: str = "workflow-12345678",
    task_identity: str | None = None,
    state: str = "PREPARED",
    created_at: str | None = None,
) -> dict[str, Any]:
    job_id = f"job-{number:08d}"
    task = task_identity or f"do[{number - 1}].calculate"
    client_request_id = f"ows4d-{number:032x}"
    title_marker = f"OWS-Phase4D-{job_id}"
    agent_cwd = str(root / "codex-job-cwd" / job_id)
    request = {
        "client_request_id": client_request_id,
        "cwd": agent_cwd,
        "prompt": _PRIVATE_PROMPT,
        "title": title_marker,
        "sandbox_mode": "inherit",
        "confirm_full_access": False,
    }
    request_json = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest_material = json.dumps(
        {"host": "127.0.0.1", "port": 18001, "body": request},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    thread_id = f"thread-{number:08d}"
    turn_id = f"turn-{number:08d}"
    remote_state: str | None = None
    receipt_json: str | None = None
    error: str | None = None
    if state == "RUNNING":
        remote_state = "in_progress"
    elif state == "ACCEPTED":
        remote_state = "in_progress"
    elif state == "SUCCEEDED":
        remote_state = "completed"
        receipt_json = '{"result":{"final_text":"fixture result"}}'
    elif state == "FAILED":
        remote_state = "failed"
        receipt_json = '{"remote_state":"failed"}'
        error = "CodexBridge turn failed"
    if state in {"PREPARED", "MAYBE_SENT"}:
        thread_id = ""
        turn_id = ""
    return {
        "job_id": job_id,
        "request_key": f"{workflow_id}::{task}",
        "workflow_id": workflow_id,
        "task_identity": task,
        "request_digest": hashlib.sha256(digest_material.encode("utf-8")).hexdigest(),
        "request_json": request_json,
        "state": state,
        "client_request_id": client_request_id,
        "title_marker": title_marker,
        "agent_cwd": agent_cwd,
        "created_at": created_at or f"2026-11-10T00:00:{number:02d}+00:00",
        "updated_at": f"2026-11-10T00:01:{number:02d}+00:00",
        "thread_id": None if not thread_id else thread_id,
        "turn_id": None if not turn_id else turn_id,
        "remote_state": remote_state,
        "receipt_json": receipt_json,
        "error": error,
    }


def _create_v1_database(
    database_path: Path,
    *,
    rows: tuple[dict[str, Any], ...] = (),
    events: tuple[tuple[str, str, str, str], ...] = (),
    reconciliations: tuple[tuple[str, str, str, str, str], ...] = (),
    unique_workflow: bool = True,
) -> None:
    workflow_unique = "UNIQUE" if unique_workflow else ""
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                request_key TEXT NOT NULL UNIQUE,
                workflow_id TEXT NOT NULL {workflow_unique},
                task_identity TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('PREPARED','MAYBE_SENT','ACCEPTED','RUNNING','SUCCEEDED','FAILED')
                ),
                client_request_id TEXT NOT NULL UNIQUE,
                title_marker TEXT NOT NULL UNIQUE,
                agent_cwd TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                thread_id TEXT,
                turn_id TEXT,
                remote_state TEXT,
                receipt_json TEXT,
                error TEXT
            );
            CREATE TABLE job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                state TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX job_events_job_event_idx ON job_events(job_id, event_id);
            CREATE TABLE job_reconciliation (
                job_id TEXT PRIMARY KEY,
                marker TEXT NOT NULL,
                reason TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        for row in rows:
            connection.execute(
                f"INSERT INTO jobs ({', '.join(_JOB_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _JOB_COLUMNS)})",
                tuple(row[column] for column in _JOB_COLUMNS),
            )
        connection.executemany(
            "INSERT INTO job_events (job_id, state, timestamp, payload_json) VALUES (?, ?, ?, ?)",
            events,
        )
        connection.executemany(
            "INSERT INTO job_reconciliation "
            "(job_id, marker, reason, detected_at, details_json) VALUES (?, ?, ?, ?, ?)",
            reconciliations,
        )


def _insert_job(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    connection.execute(
        f"INSERT INTO jobs ({', '.join(_JOB_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _JOB_COLUMNS)})",
        tuple(row[column] for column in _JOB_COLUMNS),
    )


def _table_snapshot(database_path: Path) -> dict[str, tuple[tuple[Any, ...], ...]]:
    with sqlite3.connect(database_path) as connection:
        return {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            )
            for table in ("jobs", "job_events", "job_reconciliation")
        }


def _unique_index_columns(connection: sqlite3.Connection) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for index in connection.execute("PRAGMA index_list(jobs)"):
        if bool(index[2]):
            result.add(
                tuple(
                    str(column[2])
                    for column in connection.execute(f"PRAGMA index_info('{index[1]}')")
                )
            )
    return result


class _NoPostClient:
    port = 18001

    def __init__(self) -> None:
        self.preflight_calls = 0
        self.create_thread_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def create_thread(self, request: dict[str, Any]) -> dict[str, Any]:
        self.create_thread_calls += 1
        return {
            "thread_id": "thread-99999999",
            "turn_id": "turn-99999999",
            "state": "in_progress",
        }


class _AmbiguousStartClient:
    port = 18001

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.preflight_calls = 0
        self.create_thread_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def create_thread(self, request: dict[str, Any]) -> dict[str, Any]:
        self.create_thread_calls += 1
        if self.outcome == "response-lost":
            raise CodexHTTPError("synthetic response loss")
        assert self.outcome == "unknown-identity"
        return {"thread_id": "thread-12345678", "turn_id": None, "state": "in_progress"}


def test_prepared_job_posts_once_after_recreation_and_replay_does_not_post(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "prepared-run"
    run_dir.mkdir()
    database_path = run_dir / "codex_jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        port=18001,
    )
    assert prepared["state"] == "PREPARED"

    client = _NoPostClient()
    recovered = CodexJobAdapter(database_path)
    accepted = recovered.start_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        client,  # type: ignore[arg-type]
    )
    assert accepted["state"] == "ACCEPTED"
    assert client.preflight_calls == 1
    assert client.create_thread_calls == 1

    replayed = CodexJobAdapter(database_path).start_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
        client,  # type: ignore[arg-type]
    )

    assert replayed["state"] == "ACCEPTED"
    assert client.preflight_calls == 1
    assert client.create_thread_calls == 1


@pytest.mark.parametrize(
    "outcome",
    ["claimed", "response-lost", "unknown-identity"],
    ids=["claimed-before-send", "response-lost", "thread-or-turn-id-unknown"],
)
def test_maybe_sent_post_is_never_retried_after_recreation(
    tmp_path: Path,
    outcome: str,
) -> None:
    run_dir = tmp_path / f"maybe-sent-{outcome}"
    run_dir.mkdir()
    database_path = run_dir / "codex_jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    arguments = (
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        run_dir,
    )

    if outcome == "claimed":
        prepared = adapter.prepare_job(
            *arguments[:3],
            arguments[3],
            port=18001,
        )
        assert adapter.claim_post(prepared["job_id"])
    else:
        first_client = _AmbiguousStartClient(outcome)
        with pytest.raises(CodexJobReconciliationRequiredError):
            adapter.start_job(*arguments, first_client)  # type: ignore[arg-type]
        assert first_client.preflight_calls == 1
        assert first_client.create_thread_calls == 1

    recovered = CodexJobAdapter(database_path)
    retry_client = _NoPostClient()
    with pytest.raises(CodexJobReconciliationRequiredError):
        recovered.start_job(*arguments, retry_client)  # type: ignore[arg-type]

    status = recovered.get_job_status(request_key="workflow-12345678::do[0].calculate")
    assert status["state"] == "MAYBE_SENT"
    assert status["status"] == "NEEDS_RECONCILIATION"
    assert retry_client.preflight_calls == 0
    assert retry_client.create_thread_calls == 0


_OBSERVED_THREAD_ID = "thread-12345678"
_OBSERVED_TURN_ID = "turn-12345678"


class _ObservationClient:
    port = 18001

    def __init__(self, observations: list[dict[str, Any] | Exception]) -> None:
        self.observations = list(observations)
        self.observe_calls: list[tuple[str, str]] = []
        self.post_calls = 0

    def observe_thread(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.observe_calls.append((thread_id, turn_id))
        observation = self.observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    def create_thread(self, request: dict[str, Any]) -> dict[str, Any]:
        self.post_calls += 1
        raise AssertionError("observe_job must not issue a Codex job POST")


def _accepted_job_for_observation(tmp_path: Path) -> tuple[CodexJobAdapter, str]:
    state_root = tmp_path / "state"
    state_root.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    job = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
    )
    assert adapter.claim_post(job["job_id"])
    adapter.mark_accepted(job["job_id"], _OBSERVED_THREAD_ID, _OBSERVED_TURN_ID, "in_progress")
    return adapter, job["job_id"]


def _thread_observation(
    status_state: str,
    turn_state: str,
    *,
    include_turn: bool = True,
    final_text: str | None = "settled result",
) -> dict[str, Any]:
    return {
        "status": {
            "thread_id": _OBSERVED_THREAD_ID,
            "turn_id": _OBSERVED_TURN_ID,
            "state": status_state,
        },
        "turns": ([{"id": _OBSERVED_TURN_ID, "status": turn_state}] if include_turn else []),
        "items": (
            [
                {
                    "turn_id": _OBSERVED_TURN_ID,
                    "item": {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": final_text,
                    },
                }
            ]
            if final_text is not None
            else []
        ),
    }


@pytest.mark.parametrize(
    ("first_status", "first_turn"),
    [("in_progress", "completed"), ("completed", "in_progress")],
)
def test_observe_job_rechecks_only_transient_status_turn_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_status: str,
    first_turn: str,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient(
        [
            _thread_observation(first_status, first_turn),
            _thread_observation("completed", "completed"),
        ]
    )
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "SUCCEEDED"
    assert observed["state"] == "SUCCEEDED"
    assert observed["receipt"] == {"result": {"final_text": "settled result"}}
    assert client.observe_calls == [
        (_OBSERVED_THREAD_ID, _OBSERVED_TURN_ID),
        (_OBSERVED_THREAD_ID, _OBSERVED_TURN_ID),
    ]
    assert waits == [0.2]
    assert client.post_calls == 0


def test_observe_job_keeps_reconciliation_when_transient_mismatch_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    mismatch = _thread_observation("in_progress", "completed")
    client = _ObservationClient([mismatch, mismatch, mismatch])
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["adapter_marker"] == "NEEDS_RECONCILIATION"
    assert observed["reconciliation"]["reason"] == "status and exact turn history disagree"
    assert len(client.observe_calls) == 3
    assert waits == [0.2, 0.2]
    assert client.post_calls == 0


def test_observe_job_does_not_retry_terminal_state_contradictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient([_thread_observation("completed", "failed")])
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["state"] == "ACCEPTED"
    assert len(client.observe_calls) == 1
    assert waits == []
    assert client.post_calls == 0


@pytest.mark.parametrize("state", ["needs_approval", "needs_input"])
def test_observe_job_keeps_interactive_states_in_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient([_thread_observation(state, state, final_text=None)])
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["state"] == "ACCEPTED"
    assert len(client.observe_calls) == 1
    assert waits == []
    assert client.post_calls == 0


@pytest.mark.parametrize(
    ("status_thread_id", "status_turn_id"),
    [
        ("thread-87654321", _OBSERVED_TURN_ID),
        (_OBSERVED_THREAD_ID, "turn-87654321"),
    ],
)
def test_observe_job_does_not_retry_http_identity_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_thread_id: str,
    status_turn_id: str,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = CodexHTTPClient(port=18001)
    read_paths: list[str] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        assert payload is None
        read_paths.append(path)
        return {
            "thread_id": status_thread_id,
            "turn_id": status_turn_id,
            "state": "completed",
        }

    monkeypatch.setattr(client, "_request_json", fake_request_json)

    observed = adapter.observe_job(job_id, client)

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert len(read_paths) == 3
    assert read_paths[0] == ("/ui-api/threads/thread-12345678/status?turn_id=turn-12345678")
    assert "/turns?" in read_paths[1]
    assert "/items?" in read_paths[2]


def test_observe_job_does_not_retry_malformed_http_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = CodexHTTPClient(port=18001)
    read_paths: list[str] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        assert payload is None
        read_paths.append(path)
        if "/status?" in path:
            return {
                "thread_id": _OBSERVED_THREAD_ID,
                "turn_id": _OBSERVED_TURN_ID,
                "state": "completed",
            }
        return {"thread_id": _OBSERVED_THREAD_ID, "turns": "invalid"}

    monkeypatch.setattr(client, "_request_json", fake_request_json)

    observed = adapter.observe_job(job_id, client)

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert len(read_paths) == 3
    assert "/turns?" in read_paths[1]
    assert "/items?" in read_paths[2]


@pytest.mark.parametrize(
    ("include_turn", "final_text"),
    [(False, "settled result"), (True, None)],
)
def test_observe_job_still_requires_exact_completed_turn_and_final_answer(
    tmp_path: Path,
    include_turn: bool,
    final_text: str | None,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient(
        [
            _thread_observation(
                "completed",
                "completed",
                include_turn=include_turn,
                final_text=final_text,
            )
        ]
    )

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["state"] == "ACCEPTED"
    assert client.post_calls == 0


def test_observe_job_fails_closed_when_read_recheck_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient(
        [
            _thread_observation("in_progress", "completed"),
            CodexHTTPError("read failed"),
        ]
    )
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["state"] == "ACCEPTED"
    assert len(client.observe_calls) == 2
    assert waits == [0.2]
    assert client.post_calls == 0


def test_observe_job_preserves_existing_reconciliation_without_reading_again(
    tmp_path: Path,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    adapter.mark_reconciliation(job_id, "existing marker")
    client = _ObservationClient([])

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "NEEDS_RECONCILIATION"
    assert observed["reconciliation"]["reason"] == "existing marker"
    assert client.observe_calls == []
    assert client.post_calls == 0


def test_observe_job_keeps_consistent_running_observation_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, job_id = _accepted_job_for_observation(tmp_path)
    client = _ObservationClient([_thread_observation("in_progress", "in_progress")])
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)

    observed = adapter.observe_job(job_id, client)  # type: ignore[arg-type]

    assert observed["status"] == "RUNNING"
    assert observed["state"] == "RUNNING"
    assert len(client.observe_calls) == 1
    assert waits == []
    assert client.post_calls == 0


def test_preassigned_workflow_id_does_not_rewrite_immutable_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    original = b'{"workflow_id":"12345678-1234-4234-8234-123456789abc"}\n'
    manifest_path.write_bytes(original)

    _persist_workflow_id(
        tmp_path,
        {"workflow_id": "12345678-1234-4234-8234-123456789abc"},
        "12345678-1234-4234-8234-123456789abc",
    )

    assert manifest_path.read_bytes() == original


def test_product_codex_job_uses_explicit_allowed_workspace_not_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    job = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
        workspace_root=workspace_root,
        allowed_roots=(str(workspace_root),),
    )

    assert job["request"]["cwd"] == str(workspace_root.resolve())
    assert job["agent_cwd"] == str(workspace_root.resolve())
    assert not (state_root / "codex-job-cwd").exists()


def test_product_codex_job_preserves_literal_prompt_and_existing_request_controls(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prompt = "  ${ . }\nＡＢＣ日本語\t末尾空白  "

    job = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        prompt,
        state_root,
        port=18001,
        workspace_root=workspace_root,
        allowed_roots=(str(workspace_root),),
    )

    assert job["request"]["prompt"] == prompt
    assert job["request"]["cwd"] == str(workspace_root.resolve())
    assert job["request"]["sandbox_mode"] == "inherit"
    assert job["request"]["confirm_full_access"] is False


def test_product_codex_job_prompt_change_conflicts_for_same_request_key(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        "日本の首都を一語で答えて",
        state_root,
        port=18001,
    )

    with pytest.raises(CodexJobConflictError, match="request digest mismatch"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            "別の有効なliteral prompt",
            state_root,
            port=18001,
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "",
        " \n\t",
        True,
        1,
        [],
        {},
        None,
        "bad\u0000prompt",
        "bad\u0001prompt",
        "bad\u007fprompt",
        "bad\u0085prompt",
        "bad\ud800prompt",
        "x" * (16 * 1024 + 1),
    ],
)
def test_codex_job_prompt_validator_rejects_invalid_literals(prompt: object) -> None:
    validator = getattr(interpreter, "validate_codex_job_prompt", None)
    assert callable(validator)
    with pytest.raises(OwsError) as error:
        validator(prompt)

    if isinstance(prompt, str) and prompt:
        assert prompt not in str(error.value)


def test_codex_job_prompt_validator_accepts_exact_utf8_limit_without_normalizing() -> None:
    prompt = "あ" * 5461 + "x"

    assert len(prompt.encode("utf-8")) == 16 * 1024
    assert interpreter.compute_codex_job_request({"with": {"prompt": prompt}}) == {"prompt": prompt}


def test_product_codex_job_rejects_workspace_outside_allowed_roots(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=outside,
            allowed_roots=(str(allowed),),
        )


def test_product_codex_job_rejects_parent_traversal(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=allowed / ".." / outside.name,
            allowed_roots=(str(allowed),),
        )


def test_product_codex_job_rejects_symlink_escape(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = allowed / "linked-outside"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"directory symlink unavailable: {error}")
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")

    with pytest.raises(CodexJobRequestError, match="allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[0].calculate",
            SAFE_CODEX_JOB_PROMPT,
            state_root,
            port=18001,
            workspace_root=symlink,
            allowed_roots=(str(allowed),),
        )


def test_shutdown_ownership_comes_from_exact_ledger_ids_and_unknown_post_is_incomplete(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])
    adapter.mark_accepted(prepared["job_id"], "thread-12345678", "turn-12345678", "in_progress")

    turns, complete = CodexJobAdapter.read_shutdown_ownership(state_root / "codex_jobs.sqlite")

    assert turns == {("thread-12345678", "turn-12345678")}
    assert complete


def test_shutdown_ownership_does_not_guess_identity_for_maybe_sent(tmp_path: Path) -> None:
    state_root = tmp_path / "state" / "run-0123456789abcdef0123456789abcdef"
    state_root.mkdir(parents=True)
    adapter = CodexJobAdapter(state_root / "codex_jobs.sqlite")
    prepared = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        SAFE_CODEX_JOB_PROMPT,
        state_root,
        port=18001,
    )
    assert adapter.claim_post(prepared["job_id"])

    turns, complete = CodexJobAdapter.read_shutdown_ownership(state_root / "codex_jobs.sqlite")

    assert turns == set()
    assert not complete


def test_shutdown_ownership_is_incomplete_when_ledger_is_missing(tmp_path: Path) -> None:
    turns, complete = CodexJobAdapter.read_shutdown_ownership(tmp_path / "missing.sqlite")

    assert turns == set()
    assert not complete


def test_v2_jobs_index_allows_multiple_rows_but_prepare_keeps_one_job_guard(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    database_path = state_root / "codex_jobs.sqlite"
    adapter = CodexJobAdapter(database_path)

    first = adapter.prepare_job(
        "workflow-12345678",
        "do[0].calculate",
        "first prompt",
        state_root,
        port=18001,
    )
    with pytest.raises(CodexJobConflictError, match="one live job is allowed"):
        adapter.prepare_job(
            "workflow-12345678",
            "do[1].calculate",
            "second prompt",
            state_root,
            port=18001,
        )

    second = _legacy_job_row(
        2,
        state_root,
        workflow_id="workflow-12345678",
        task_identity="do[1].calculate",
    )
    with sqlite3.connect(database_path) as connection:
        unique_columns = _unique_index_columns(connection)
        workflow_index = {
            str(index[1]): (
                bool(index[2]),
                tuple(
                    str(column[2])
                    for column in connection.execute(f"PRAGMA index_info('{index[1]}')")
                ),
            )
            for index in connection.execute("PRAGMA index_list(jobs)")
        }
        _insert_job(connection, second)
        stored_workflows = connection.execute(
            "SELECT workflow_id FROM jobs WHERE workflow_id = ? ORDER BY job_id",
            ("workflow-12345678",),
        ).fetchall()

    assert unique_columns == {
        ("job_id",),
        ("request_key",),
        ("client_request_id",),
        ("title_marker",),
        ("workflow_id", "task_identity"),
    }
    assert ("workflow_id",) not in unique_columns
    assert workflow_index["jobs_workflow_idx"] == (
        False,
        ("workflow_id", "created_at", "job_id"),
    )
    assert first["state"] == "PREPARED"
    assert len(stored_workflows) == 2


@pytest.mark.parametrize("state", ["EMPTY", "SUCCEEDED", "FAILED"])
def test_v1_empty_or_terminal_ledger_migrates_rows_and_children_idempotently(
    tmp_path: Path,
    state: str,
) -> None:
    database_path = tmp_path / "legacy.sqlite"
    row = None if state == "EMPTY" else _legacy_job_row(1, tmp_path, state=state)
    rows = () if row is None else (row,)
    events = (
        ()
        if row is None
        else ((row["job_id"], state, "2026-11-10T00:02:00+00:00", '{"keep":"event"}'),)
    )
    reconciliations = (
        ()
        if row is None
        else (
            (
                row["job_id"],
                "NEEDS_RECONCILIATION",
                "fixture reconciliation",
                "2026-11-10T00:03:00+00:00",
                '{"keep":"reconciliation"}',
            ),
        )
    )
    _create_v1_database(
        database_path,
        rows=rows,
        events=events,
        reconciliations=reconciliations,
    )
    before = _table_snapshot(database_path)

    CodexJobAdapter(database_path)

    with sqlite3.connect(database_path) as connection:
        unique_columns = _unique_index_columns(connection)
        workflow_indexes = {
            str(index[1]): (
                bool(index[2]),
                tuple(
                    str(column[2])
                    for column in connection.execute(f"PRAGMA index_info('{index[1]}')")
                ),
            )
            for index in connection.execute("PRAGMA index_list(jobs)")
        }
    assert unique_columns == {
        ("job_id",),
        ("request_key",),
        ("client_request_id",),
        ("title_marker",),
        ("workflow_id", "task_identity"),
    }
    assert ("workflow_id",) not in unique_columns
    assert workflow_indexes["jobs_workflow_idx"] == (
        False,
        ("workflow_id", "created_at", "job_id"),
    )
    assert _table_snapshot(database_path) == before

    CodexJobAdapter(database_path)

    assert _table_snapshot(database_path) == before


@pytest.mark.parametrize("state", ["PREPARED", "MAYBE_SENT", "ACCEPTED", "RUNNING"])
def test_active_v1_ledger_stays_v1_and_reconciles_without_post(
    tmp_path: Path,
    state: str,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    database_path = state_root / "codex_jobs.sqlite"
    row = _legacy_job_row(1, state_root, state=state)
    events = ((row["job_id"], "PREPARED", "2026-11-10T00:00:00+00:00", '{"keep":"prepared"}'),)
    if state in {"MAYBE_SENT", "RUNNING"}:
        events += ((row["job_id"], state, "2026-11-10T00:01:00+00:00", '{"keep":"active"}'),)
    _create_v1_database(database_path, rows=(row,), events=events)

    adapter = CodexJobAdapter(database_path)
    job = adapter.prepare_job(
        row["workflow_id"],
        row["task_identity"],
        _PRIVATE_PROMPT,
        state_root,
        port=18001,
    )
    with pytest.raises(CodexJobConflictError, match="requires a V2 job ledger"):
        adapter.prepare_job(
            row["workflow_id"],
            "do[1].second",
            "second prompt",
            state_root,
            port=18001,
            max_jobs_per_workflow=2,
        )
    with pytest.raises(
        CodexJobConflictError,
        match="max_jobs_per_workflow=3 requires a V2 job ledger",
    ):
        adapter.prepare_job(
            row["workflow_id"],
            "do[2].third",
            "third prompt",
            state_root,
            port=18001,
            max_jobs_per_workflow=3,
        )
    with pytest.raises(
        CodexJobConflictError,
        match="max_jobs_per_workflow=3 requires a V2 job ledger",
    ):
        adapter.prepare_job(
            row["workflow_id"],
            row["task_identity"],
            _PRIVATE_PROMPT,
            state_root,
            port=18001,
            max_jobs_per_workflow=3,
        )
    status = adapter.get_job_status(job_id=row["job_id"])
    client = _NoPostClient()

    assert job["job_id"] == row["job_id"]
    assert job["state"] == state
    assert status["state"] == state
    assert status["request"]["prompt"] == _PRIVATE_PROMPT
    with sqlite3.connect(database_path) as connection:
        unique_columns = _unique_index_columns(connection)
    assert ("workflow_id",) in unique_columns
    assert ("workflow_id", "task_identity") not in unique_columns

    if state == "MAYBE_SENT":
        with pytest.raises(CodexJobReconciliationRequiredError, match="POST outcome is unknown"):
            adapter.start_job(
                row["workflow_id"],
                row["task_identity"],
                _PRIVATE_PROMPT,
                state_root,
                client,  # type: ignore[arg-type]
            )
    elif state in {"ACCEPTED", "RUNNING"}:
        resumed = adapter.start_job(
            row["workflow_id"],
            row["task_identity"],
            _PRIVATE_PROMPT,
            state_root,
            client,  # type: ignore[arg-type]
        )
        assert resumed["state"] == state

    assert client.preflight_calls == 0
    assert client.create_thread_calls == 0
    turns, complete = CodexJobAdapter.read_shutdown_ownership(database_path)
    assert turns == (
        {("thread-00000001", "turn-00000001")} if state in {"ACCEPTED", "RUNNING"} else set()
    )
    assert complete is (state != "MAYBE_SENT")


@pytest.mark.parametrize("corruption", ["unexpected_column", "request_key", "duplicate_workflow"])
def test_v1_schema_or_data_anomaly_fails_closed_without_replacing_legacy_data(
    tmp_path: Path,
    corruption: str,
) -> None:
    database_path = tmp_path / "legacy.sqlite"
    first = _legacy_job_row(1, tmp_path)
    rows = (first,)
    if corruption == "duplicate_workflow":
        second = _legacy_job_row(2, tmp_path, task_identity="different.task")
        rows = (first, second)
    _create_v1_database(
        database_path,
        rows=rows,
        unique_workflow=corruption != "duplicate_workflow",
    )
    with sqlite3.connect(database_path) as connection:
        if corruption == "unexpected_column":
            connection.execute("ALTER TABLE jobs ADD COLUMN future_payload TEXT")
        elif corruption == "request_key":
            connection.execute(
                "UPDATE jobs SET request_key = 'workflow-12345678::wrong.task' WHERE job_id = ?",
                (first["job_id"],),
            )
    before = _table_snapshot(database_path)
    with sqlite3.connect(database_path) as connection:
        before_schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY type, name"
            )
        )

    with pytest.raises(CodexJobReconciliationRequiredError):
        CodexJobAdapter(database_path)

    assert _table_snapshot(database_path) == before
    with sqlite3.connect(database_path) as connection:
        after_schema = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') ORDER BY type, name"
            )
        )
    assert after_schema == before_schema


def test_v1_migration_rolls_back_table_rebuild_when_post_copy_verification_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.sqlite"
    row = _legacy_job_row(1, tmp_path, state="SUCCEEDED")
    _create_v1_database(database_path, rows=(row,))
    before = _table_snapshot(database_path)
    original_verifier = getattr(CodexJobAdapter, "_assert_migrated_content", None)
    assert callable(original_verifier)

    def reject_copy(*args: Any, **kwargs: Any) -> None:
        raise CodexJobReconciliationRequiredError("injected copy verification failure")

    monkeypatch.setattr(CodexJobAdapter, "_assert_migrated_content", staticmethod(reject_copy))
    with pytest.raises(CodexJobReconciliationRequiredError, match="injected"):
        CodexJobAdapter(database_path)

    assert _table_snapshot(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert ("workflow_id",) in _unique_index_columns(connection)
        assert ("workflow_id", "task_identity") not in _unique_index_columns(connection)


def test_get_jobs_for_workflow_is_read_only_bounded_ordered_and_allowlisted(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    first = _legacy_job_row(1, tmp_path, created_at="2026-11-10T00:00:00+00:00")
    second = _legacy_job_row(
        2,
        tmp_path,
        task_identity="do[1].calculate",
        state="RUNNING",
        created_at="2026-11-10T00:00:00+00:00",
    )
    third = _legacy_job_row(
        3,
        tmp_path,
        task_identity="do[2].calculate",
        state="SUCCEEDED",
        created_at="2026-11-10T00:01:00+00:00",
    )
    unrelated = _legacy_job_row(
        4,
        tmp_path,
        workflow_id="workflow-87654321",
        task_identity="outside.workflow",
        created_at="2026-11-09T00:00:00+00:00",
    )
    with sqlite3.connect(database_path) as connection:
        for row in (first, second, third, unrelated):
            _insert_job(connection, row)
    before = _table_snapshot(database_path)

    jobs = adapter.get_jobs_for_workflow("workflow-12345678")

    assert [job["job_id"] for job in jobs] == ["job-00000001", "job-00000002", "job-00000003"]
    assert [job["task_identity"] for job in jobs] == [
        "do[0].calculate",
        "do[1].calculate",
        "do[2].calculate",
    ]
    assert set(jobs[0]) == {
        "job_id",
        "task_identity",
        "state",
        "thread_id",
        "turn_id",
        "receipt",
    }
    assert jobs[0]["receipt"] is None
    assert jobs[1]["thread_id"] == "thread-00000002"
    assert jobs[2]["receipt"] == {"result": {"final_text": "fixture result"}}
    serialized = json.dumps(jobs, ensure_ascii=False)
    for private_value in (
        _PRIVATE_PROMPT,
        "ows4d-00000000000000000000000000000001",
        "OWS-Phase4D-job-00000001",
        str(tmp_path / "codex-job-cwd" / "job-00000001"),
    ):
        assert private_value not in serialized
    assert _table_snapshot(database_path) == before
    assert adapter.get_jobs_for_workflow("workflow-12345678", limit=3) == jobs
    assert adapter.get_jobs_for_workflow("workflow-87654321")[0]["job_id"] == "job-00000004"
    assert adapter.get_jobs_for_workflow("workflow-no-jobs") == []
    with pytest.raises(CodexJobReconciliationRequiredError, match="limit"):
        adapter.get_jobs_for_workflow("workflow-12345678", limit=2)


def test_get_jobs_for_workflow_rejects_invalid_scope_and_more_than_maximum(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    with pytest.raises(CodexJobRequestError, match="workflow_id"):
        adapter.get_jobs_for_workflow("")
    with pytest.raises(CodexJobRequestError, match="limit"):
        adapter.get_jobs_for_workflow("workflow-12345678", limit=17)

    with sqlite3.connect(database_path) as connection:
        for number in range(1, 18):
            _insert_job(
                connection,
                _legacy_job_row(
                    number,
                    tmp_path,
                    workflow_id="workflow-12345678",
                    task_identity=f"task.{number}",
                ),
            )

    with pytest.raises(CodexJobReconciliationRequiredError, match="limit"):
        adapter.get_jobs_for_workflow("workflow-12345678")


def test_get_jobs_for_workflow_does_not_return_a_control_token_as_remote_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "jobs.sqlite"
    adapter = CodexJobAdapter(database_path)
    token = "fixture-secret-123456"
    monkeypatch.setenv(CONTROL_TOKEN_ENV, token)
    row = _legacy_job_row(1, tmp_path, state="RUNNING")
    row["thread_id"] = f"thread-{token}"
    with sqlite3.connect(database_path) as connection:
        _insert_job(connection, row)

    with pytest.raises(CodexJobReconciliationRequiredError) as error:
        adapter.get_jobs_for_workflow("workflow-12345678")

    assert token not in str(error.value)


def test_get_jobs_for_workflow_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite"
    adapter = CodexJobAdapter.__new__(CodexJobAdapter)
    adapter.database_path = database_path

    with pytest.raises(CodexJobReconciliationRequiredError):
        adapter.get_jobs_for_workflow("workflow-12345678")

    assert not database_path.exists()
