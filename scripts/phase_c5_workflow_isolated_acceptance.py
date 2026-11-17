from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import yaml

from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import (
    OwsError,
    validate_three_live_codex_job_input,
    validate_workflow,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PARENT_HEAD = "93089459f8387ef0fa6ef25f8458144d9d1a25bf"
MAX_INITIAL_POSTS = 3
MAX_CODEX_JOBS = 6
MAX_WORKFLOW_WAIT_SECONDS = 600.0
STATUS_POLL_SECONDS = 0.5
BRIDGE_HOST = "127.0.0.1"
PRODUCTION_PORTS = frozenset({18000, 18001})
APPROVAL_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
HEAD_PATTERN = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?:\b[A-Z]:[\\/]|\\\\[^\\\s]+\\[^\\\s]+)")
POSIX_PATH_PATTERN = re.compile(r"(?<![/A-Za-z])/(?:home|Users|tmp|var|etc|private|mnt|workspace)/")
OFFLINE_TESTS = (
    "tests/test_phase_c5_workflow_isolated_acceptance.py",
    "tests/test_phase_b5_workflow_isolated_smoke.py",
    "tests/test_phase_c5_gui_offline_acceptance.py",
    "tests/test_workflow_task_projection.py",
    "tests/test_workflow_task_api.py",
    "tests/test_workflow_task_mcp.py",
    "tests/test_workflow_node_events.py",
    "tests/test_workflows_multijob.py",
)
REQUIRED_SOURCE_FILES = (
    "scripts/phase_c5_workflow_isolated_acceptance.py",
    "scripts/phase_b5_workflow_isolated_smoke.py",
    "src/codex_bridge/workflows/interpreter.py",
    "src/codex_bridge/workflows/graph.py",
    "src/codex_bridge/workflows/task_projection.py",
    "src/codex_bridge/workflows/run_task_mcp.py",
    "tests/test_phase_c5_workflow_isolated_acceptance.py",
    "docs/phase_c5_p2_runner_review.md",
)
_WORKFLOW_STATUSES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "needs_reconciliation",
        "succeeded",
        "failed",
        "failed_to_start",
    }
)


class RunnerAbort(RuntimeError):
    def __init__(self, status: str, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class RunCase:
    name: str
    expected_jobs: int
    definition: dict[str, Any]
    workflow_yaml: str
    input_data: dict[str, Any]
    expected_texts: tuple[str, ...]
    static_node_ids: tuple[str, ...]
    call_node_ids: tuple[str, ...]


class RunAdapter(Protocol):
    def start(self, case: RunCase, client_request_id: str) -> tuple[int, Any]: ...

    def get_status(self, workflow_id: str) -> tuple[int, Any]: ...

    def get_result(self, workflow_id: str) -> tuple[int, Any]: ...

    def verify(
        self,
        case: RunCase,
        accepted: dict[str, Any],
        detail: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]: ...

    def readonly_restart(self, completed: list[dict[str, Any]]) -> dict[str, Any]: ...


def _case_definitions() -> tuple[
    tuple[
        str, int, dict[str, Any], dict[str, Any], tuple[str, ...], tuple[str, ...], tuple[str, ...]
    ],
    ...,
]:
    document = {
        "dsl": "1.0.3",
        "namespace": "codexbridge-c5",
        "version": "1.0.0",
    }
    evaluate = {"language": "jq", "mode": "strict"}
    one = {
        "document": {**document, "name": "phase-c5-one-job"},
        "evaluate": evaluate,
        "do": [
            {
                "single": {
                    "call": "codex-job",
                    "with": {"prompt": "Reply with exactly C5-A and no other text."},
                }
            }
        ],
    }
    two = {
        "document": {**document, "name": "phase-c5-two-sequential-jobs"},
        "evaluate": evaluate,
        "do": [
            {"first": {"call": "codex-job", "with": {"prompt": "Reply with exactly C5-B1."}}},
            {
                "second": {
                    "call": "codex-job",
                    "with": {
                        "prompt": "Reply with exactly C5-B2 after the first result: ",
                        "prompt_from": "${ .result.final_text }",
                    },
                }
            },
        ],
    }
    three = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge-poc",
            "name": "three-codex-job-loop",
            "version": "1.0.0",
        },
        "evaluate": evaluate,
        "input": {"from": "${ {issues: (.issues | sort_by(.number))} }"},
        "do": [
            {
                "processIssues": {
                    "for": {"in": "${ .issues }", "each": "issue", "at": "index"},
                    "do": [
                        {
                            "choose": {
                                "switch": [
                                    {
                                        "enabled": {
                                            "when": "${ $issue.enabled == true }",
                                            "then": "processIssue",
                                        }
                                    },
                                    {"disabled": {"then": "skipIssue"}},
                                ]
                            }
                        },
                        {
                            "processIssue": {
                                "input": {"from": "${ {number: $issue.number} }"},
                                "call": "codex-job",
                                "with": {
                                    "prompt": "Reply with only the synthetic issue number: ",
                                    "prompt_from": "${ .number | tostring }",
                                },
                                "output": {
                                    "as": (
                                        "${ {number: $input.number, status: .status, "
                                        "job_id: .job_id, request_key: .request_key, "
                                        "final_text: .result.final_text} }"
                                    )
                                },
                                "then": "exit",
                            }
                        },
                        {
                            "skipIssue": {
                                "set": {
                                    "number": "${ $issue.number }",
                                    "status": "skipped",
                                    "job_id": None,
                                    "request_key": None,
                                    "final_text": None,
                                }
                            }
                        },
                    ],
                }
            }
        ],
    }
    three_input = {
        "issues": [
            {"number": 1, "enabled": True},
            {"number": 2, "enabled": True},
            {"number": 3, "enabled": True},
        ]
    }
    return (
        ("A", 1, one, {}, ("C5-A",), ("/do/0/single",), ("/do/0/single",)),
        (
            "B",
            2,
            two,
            {},
            ("C5-B1", "C5-B2"),
            ("/do/0/first", "/do/1/second"),
            ("/do/0/first", "/do/1/second"),
        ),
        (
            "C",
            3,
            three,
            three_input,
            ("1", "2", "3"),
            (
                "/do/0/processIssues",
                "/do/0/processIssues/do/0/choose",
                "/do/0/processIssues/do/1/processIssue",
                "/do/0/processIssues/do/2/skipIssue",
            ),
            ("/do/0/processIssues/do/1/processIssue",),
        ),
    )


def build_run_cases() -> tuple[RunCase, ...]:
    cases: list[RunCase] = []
    for (
        name,
        expected_jobs,
        definition,
        input_data,
        expected_texts,
        static_node_ids,
        call_node_ids,
    ) in _case_definitions():
        workflow_yaml = yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
        case = RunCase(
            name=name,
            expected_jobs=expected_jobs,
            definition=definition,
            workflow_yaml=workflow_yaml,
            input_data=input_data,
            expected_texts=expected_texts,
            static_node_ids=static_node_ids,
            call_node_ids=call_node_ids,
        )
        validate_fixed_case(case)
        cases.append(case)
    return tuple(cases)


def validate_fixed_case(case: RunCase) -> None:
    expected = next((spec for spec in _case_definitions() if spec[0] == case.name), None)
    if expected is None:
        raise RunnerAbort("BLOCKED", "unknown_fixed_workflow_case")
    name, jobs, definition, input_data, texts, static_node_ids, call_node_ids = expected
    if (
        case.name != name
        or case.expected_jobs != jobs
        or case.definition != definition
        or case.workflow_yaml != yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)
        or case.input_data != input_data
        or case.expected_texts != texts
        or case.call_node_ids != call_node_ids
    ):
        raise RunnerAbort("BLOCKED", "fixed_workflow_case_mutated")
    try:
        validate_workflow(
            definition,
            codex_job_live=True,
            max_live_codex_jobs=jobs,
        )
        graph = project_workflow_graph(
            definition,
            codex_job_live=True,
            max_live_codex_jobs=jobs,
        )
        if yaml.safe_load(case.workflow_yaml) != definition:
            raise RunnerAbort("BLOCKED", "fixed_workflow_yaml_mismatch")
        actual_node_ids = tuple(node["node_id"] for node in graph["nodes"])
        actual_call_ids = tuple(
            node["node_id"]
            for node in graph["nodes"]
            if node.get("operation") == "call"
            and isinstance(node.get("attributes"), dict)
            and node["attributes"].get("call") == "codex-job"
        )
        if actual_node_ids != case.static_node_ids or actual_call_ids != case.call_node_ids:
            raise RunnerAbort("BLOCKED", "fixed_workflow_graph_mismatch")
        if jobs == 3:
            normalized = validate_three_live_codex_job_input(case.input_data, input_kind="issues")
            if (
                len(normalized) != 3
                or any(item.get("enabled") is not True for item in normalized)
                or tuple(item.get("number") for item in normalized) != (1, 2, 3)
            ):
                raise RunnerAbort("BLOCKED", "fixed_three_job_input_mismatch")
        elif case.input_data != {} or len(actual_call_ids) != jobs:
            raise RunnerAbort("BLOCKED", "fixed_workflow_job_count_mismatch")
    except RunnerAbort:
        raise
    except (OwsError, TypeError, ValueError, RecursionError):
        raise RunnerAbort("BLOCKED", "fixed_workflow_contract_invalid") from None


def client_request_id_for(approval_id: str, case: RunCase) -> str:
    if APPROVAL_ID_PATTERN.fullmatch(approval_id) is None or case.name not in {"A", "B", "C"}:
        raise RunnerAbort("BLOCKED", "approval_or_run_identity_invalid")
    return f"c5-{approval_id}-{case.name.lower()}"


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError, UnicodeError):
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RunnerAbort("BLOCKED", "persistent_budget_write_failed") from None


class AttemptLedger:
    def __init__(self, marker_path: Path, data: dict[str, Any]) -> None:
        self.marker_path = marker_path
        self.data = data

    @classmethod
    def create(
        cls,
        marker_root: Path,
        approval_id: str,
        approved_head: str,
        runner_source_sha256: str,
    ) -> AttemptLedger:
        if (
            APPROVAL_ID_PATTERN.fullmatch(approval_id) is None
            or HEAD_PATTERN.fullmatch(approved_head) is None
            or re.fullmatch(r"[0-9a-f]{64}", runner_source_sha256, re.ASCII) is None
        ):
            raise RunnerAbort("BLOCKED", "approval_marker_identity_invalid")
        try:
            marker_root.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(marker_root) or not marker_root.is_dir():
                raise RunnerAbort("BLOCKED", "approval_marker_root_unsafe")
        except OSError:
            raise RunnerAbort("BLOCKED", "approval_marker_root_unavailable") from None
        marker_path = marker_root / f"{approval_id}.json"
        data: dict[str, Any] = {
            "schema_version": 1,
            "approval_id": approval_id,
            "approved_head": approved_head,
            "runner_source_sha256": runner_source_sha256,
            "status": "started",
            "created_at_utc": _utc_now(),
            "updated_at_utc": _utc_now(),
            "post_count": 0,
            "job_count": 0,
            "runs": {
                name: {
                    "expected_jobs": jobs,
                    "client_request_id": None,
                    "workflow_id": None,
                    "run_id": None,
                    "state": "not_started",
                    "observed_jobs": 0,
                }
                for name, jobs, *_rest in _case_definitions()
            },
            "events": [{"at_utc": _utc_now(), "event": "attempt_started"}],
        }
        try:
            descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise RunnerAbort("BLOCKED", "approval_already_consumed") from None
        except (OSError, TypeError, ValueError, UnicodeError):
            raise RunnerAbort("BLOCKED", "approval_marker_create_failed") from None
        return cls(marker_path, data)

    @property
    def post_count(self) -> int:
        return int(self.data.get("post_count", 0))

    @property
    def job_count(self) -> int:
        return int(self.data.get("job_count", 0))

    @property
    def run_states(self) -> dict[str, str]:
        runs = self.data.get("runs")
        if not isinstance(runs, dict):
            return {}
        return {
            key: str(value.get("state", "unknown"))
            for key, value in runs.items()
            if isinstance(key, str) and isinstance(value, dict)
        }

    def _save(self, event: str) -> None:
        self.data["updated_at_utc"] = _utc_now()
        events = self.data.get("events")
        if isinstance(events, list):
            events.append({"at_utc": _utc_now(), "event": event})
        _atomic_json(self.marker_path, self.data)

    def before_post(self, case: RunCase, client_request_id: str) -> None:
        validate_fixed_case(case)
        expected_id = client_request_id_for(str(self.data.get("approval_id", "")), case)
        runs = self.data.get("runs")
        if (
            not isinstance(runs, dict)
            or case.name not in runs
            or client_request_id != expected_id
            or self.post_count >= MAX_INITIAL_POSTS
            or self.data.get("status") != "started"
            or runs[case.name].get("state") != "not_started"
        ):
            raise RunnerAbort("BLOCKED", "initial_post_budget_exhausted")
        runs[case.name]["client_request_id"] = client_request_id
        runs[case.name]["state"] = "maybe_sent"
        self.data["post_count"] = self.post_count + 1
        self._save(f"run_{case.name.lower()}_post_maybe_sent")

    def mark_accepted(
        self,
        case: RunCase,
        client_request_id: str,
        workflow_id: str,
        run_id: str,
    ) -> None:
        runs = self.data.get("runs")
        if (
            not isinstance(runs, dict)
            or case.name not in runs
            or runs[case.name].get("state") != "maybe_sent"
            or runs[case.name].get("client_request_id") != client_request_id
            or not _valid_identifier(workflow_id)
            or not _valid_identifier(run_id)
        ):
            raise RunnerAbort("BLOCKED", "accepted_workflow_identity_invalid")
        runs[case.name].update({"workflow_id": workflow_id, "run_id": run_id, "state": "accepted"})
        self._save(f"run_{case.name.lower()}_accepted")

    def record_observed_jobs(self, case: RunCase, observed_jobs: int) -> None:
        runs = self.data.get("runs")
        if (
            not isinstance(runs, dict)
            or case.name not in runs
            or type(observed_jobs) is not int
            or observed_jobs < 0
        ):
            raise RunnerAbort("BLOCKED", "job_ledger_observation_invalid")
        if runs[case.name].get("state") != "accepted":
            raise RunnerAbort("BLOCKED", "job_observation_before_workflow_acceptance")
        runs[case.name]["observed_jobs"] = observed_jobs
        self.data["job_count"] = self.job_count + observed_jobs
        self._save(f"run_{case.name.lower()}_jobs_observed")
        if observed_jobs > case.expected_jobs or self.job_count > MAX_CODEX_JOBS:
            self.mark_terminal(case, "FAIL", "job_budget_exceeded")
            raise RunnerAbort("FAIL", "job_budget_exceeded")

    def mark_success(self, case: RunCase) -> None:
        runs = self.data.get("runs")
        if not isinstance(runs, dict) or runs[case.name].get("state") != "accepted":
            raise RunnerAbort("BLOCKED", "workflow_success_state_invalid")
        if runs[case.name].get("observed_jobs") != case.expected_jobs:
            self.mark_terminal(case, "FAIL", "job_count_mismatch")
            raise RunnerAbort("FAIL", "job_count_mismatch")
        runs[case.name]["state"] = "succeeded"
        self._save(f"run_{case.name.lower()}_succeeded")

    def mark_unknown(self, case: RunCase, code: str) -> None:
        self.data["status"] = "UNKNOWN"
        self.data["terminal_code"] = code
        self._save(f"run_{case.name.lower()}_{code}")

    def mark_terminal(self, case: RunCase | None, status: str, code: str) -> None:
        if case is not None:
            runs = self.data.get("runs")
            if isinstance(runs, dict) and isinstance(runs.get(case.name), dict):
                runs[case.name]["state"] = status.casefold()
        self.data["status"] = status
        self.data["terminal_code"] = code
        self._save(code)


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and "\x00" not in value


def wait_for_success(
    adapter: RunAdapter,
    workflow_id: str,
    *,
    timeout_seconds: float = MAX_WORKFLOW_WAIT_SECONDS,
) -> dict[str, Any]:
    started_at = time.monotonic()
    deadline = started_at + min(max(timeout_seconds, 0.0), MAX_WORKFLOW_WAIT_SECONDS)
    while time.monotonic() < deadline:
        try:
            status_code, detail = adapter.get_status(workflow_id)
        except Exception:
            raise RunnerAbort("UNKNOWN", "workflow_status_readback_unknown") from None
        if (
            status_code != 200
            or not isinstance(detail, dict)
            or detail.get("workflow_id") != workflow_id
        ):
            raise RunnerAbort("UNKNOWN", "workflow_status_readback_unknown")
        status = detail.get("status")
        if not isinstance(status, str) or status not in _WORKFLOW_STATUSES:
            raise RunnerAbort("UNKNOWN", "workflow_status_unknown")
        if status == "succeeded":
            return detail
        if status in {"failed", "failed_to_start"}:
            raise RunnerAbort("FAIL", "workflow_terminal_failure")
        time.sleep(min(STATUS_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    raise RunnerAbort("UNKNOWN", "workflow_status_wait_timed_out")


def execute_runs(
    cases: Sequence[RunCase],
    ledger: AttemptLedger,
    adapter: RunAdapter,
) -> dict[str, Any]:
    expected_plan = tuple((spec[0], spec[1]) for spec in _case_definitions())
    if (
        len(cases) != len(expected_plan)
        or tuple((case.name, case.expected_jobs) for case in cases) != expected_plan
    ):
        raise RunnerAbort("BLOCKED", "live_run_plan_mismatch")
    completed: list[dict[str, Any]] = []
    for case in cases:
        validate_fixed_case(case)
        request_id = client_request_id_for(str(ledger.data.get("approval_id", "")), case)
        begin_run = getattr(adapter, "begin_run", None)
        if callable(begin_run):
            begin_run(case)
        ledger.before_post(case, request_id)
        try:
            status_code, accepted = adapter.start(case, request_id)
        except Exception:
            ledger.mark_unknown(case, "initial_workflow_post_outcome_unknown")
            diagnose = getattr(adapter, "diagnose_unknown", None)
            if callable(diagnose):
                try:
                    diagnose(case, request_id)
                except Exception:
                    pass
            raise RunnerAbort("UNKNOWN", "start_outcome_unknown") from None
        if (
            status_code != 202
            or not isinstance(accepted, dict)
            or not _valid_identifier(accepted.get("workflow_id"))
            or not _valid_identifier(accepted.get("run_id"))
        ):
            ledger.mark_unknown(case, "initial_workflow_post_response_unknown")
            diagnose = getattr(adapter, "diagnose_unknown", None)
            if callable(diagnose):
                try:
                    diagnose(case, request_id)
                except Exception:
                    pass
            raise RunnerAbort("UNKNOWN", "start_response_unknown")
        workflow_id = str(accepted["workflow_id"])
        run_id = str(accepted["run_id"])
        accepted_identity = {**accepted, "client_request_id": request_id}
        ledger.mark_accepted(case, request_id, workflow_id, run_id)
        try:
            detail = wait_for_success(adapter, workflow_id)
            if detail.get("run_id") != run_id:
                raise RunnerAbort("FAIL", "workflow_status_identity_mismatch")
            result_status, result_value = adapter.get_result(workflow_id)
            if result_status != 200 or not isinstance(result_value, dict):
                raise RunnerAbort("UNKNOWN", "workflow_result_readback_unknown")
            report = adapter.verify(case, accepted_identity, detail, result_value)
            observed_jobs = report.get("job_count")
            if type(observed_jobs) is not int:
                raise RunnerAbort("UNKNOWN", "job_ledger_observation_unknown")
            ledger.record_observed_jobs(case, observed_jobs)
            if report.get("result_matches") is not True or report.get("graph_matches") is not True:
                ledger.mark_terminal(case, "FAIL", "workflow_result_or_graph_mismatch")
                raise RunnerAbort("FAIL", "workflow_result_or_graph_mismatch")
            ledger.mark_success(case)
            completed.append(
                {
                    "name": case.name,
                    "expected_jobs": case.expected_jobs,
                    "observed_jobs": observed_jobs,
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "result_matches": True,
                    "graph_matches": True,
                }
            )
        except RunnerAbort as error:
            if error.status == "UNKNOWN":
                ledger.mark_unknown(case, error.code)
                diagnose = getattr(adapter, "diagnose_accepted", None)
                if callable(diagnose):
                    try:
                        diagnose(case, accepted_identity)
                    except Exception:
                        pass
            elif ledger.run_states.get(case.name) not in {"fail", "blocked"}:
                ledger.mark_terminal(case, error.status, error.code)
            raise
        except Exception:
            ledger.mark_unknown(case, "run_verification_unknown")
            raise RunnerAbort("UNKNOWN", "run_verification_unknown") from None
    restart_result = adapter.readonly_restart(completed)
    if (
        not isinstance(restart_result, dict)
        or restart_result.get("status") != "PASS"
        or restart_result.get("new_post_count") != 0
        or restart_result.get("new_job_count") != 0
        or restart_result.get("snapshots_unchanged") is not True
    ):
        ledger.mark_terminal(None, "BLOCKED", "readonly_restart_gate_failed")
        raise RunnerAbort("BLOCKED", "readonly_restart_gate_failed")
    ledger.data["status"] = "runs_succeeded_gui_not_run"
    ledger._save("all_runs_succeeded_readonly_restart_passed")
    return {
        "gate2": "PASS",
        "gate3": "PASS",
        "gate1": "NOT_RUN",
        "completed_runs": completed,
        "restart": {
            "status": "PASS",
            "new_post_count": 0,
            "new_job_count": 0,
            "snapshots_unchanged": True,
        },
    }


def validate_git_guard(
    current_head: str,
    approved_head: str,
    *,
    ancestor: bool,
    dirty_paths: Sequence[str],
    source_matches_head: bool,
) -> None:
    if (
        HEAD_PATTERN.fullmatch(current_head) is None
        or HEAD_PATTERN.fullmatch(approved_head) is None
        or current_head != approved_head
        or not ancestor
        or dirty_paths
        or not source_matches_head
    ):
        raise RunnerAbort("BLOCKED", "approved_head_or_source_guard_failed")


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink():
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & reparse_attribute)


def validate_isolated_layout(
    temp_root: Path,
    state_root: Path,
    workspace: Path,
    ports: tuple[int, int],
) -> None:
    try:
        root = temp_root.resolve(strict=True)
        state = state_root.resolve(strict=True)
        work = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RunnerAbort("BLOCKED", "isolated_root_unavailable") from None
    if (
        any(_is_reparse_point(path) for path in (temp_root, state_root, workspace))
        or not root.is_dir()
        or not state.is_dir()
        or not work.is_dir()
        or not state.is_relative_to(root)
        or not work.is_relative_to(root)
        or state == work
        or state.is_relative_to(work)
        or work.is_relative_to(state)
        or root == REPOSITORY_ROOT.resolve()
        or root.is_relative_to(REPOSITORY_ROOT.resolve())
        or REPOSITORY_ROOT.resolve().is_relative_to(root)
    ):
        raise RunnerAbort("BLOCKED", "isolated_roots_overlap_or_are_unsafe")
    first, second = ports
    if (
        type(first) is not int
        or type(second) is not int
        or first == second
        or first in PRODUCTION_PORTS
        or second in PRODUCTION_PORTS
        or not 1024 <= first <= 65535
        or not 1024 <= second <= 65535
    ):
        raise RunnerAbort("BLOCKED", "isolated_ports_unsafe")


def stop_owned_process(
    process: object,
    identity: dict[str, Any],
    *,
    verify_owner: Callable[[object, dict[str, Any]], None],
    stop_process: Callable[[object], None],
) -> None:
    verify_owner(process, identity)
    stop_process(process)


def validate_projection(
    case: RunCase,
    workflow_id: str,
    run_id: str,
    http_tasks: Mapping[str, Any],
    http_events: Mapping[str, Any],
    mcp_tasks: Mapping[str, Any],
    mcp_events: Mapping[str, Any],
) -> bool:
    if dict(http_tasks) != dict(mcp_tasks) or dict(http_events) != dict(mcp_events):
        raise RunnerAbort("FAIL", "http_mcp_task_projection_mismatch")
    for projection in (http_tasks, http_events):
        if projection.get("workflow_id") != workflow_id or projection.get("run_id") != run_id:
            raise RunnerAbort("FAIL", "task_projection_identity_mismatch")
        if projection.get("coverage") != "complete" or projection.get("truncated") is True:
            raise RunnerAbort("UNKNOWN", "task_projection_coverage_incomplete")
    nodes = http_tasks.get("nodes")
    events = http_events.get("events")
    if not isinstance(nodes, list) or not isinstance(events, list):
        raise RunnerAbort("UNKNOWN", "task_projection_shape_invalid")
    if (
        tuple(node.get("node_id") for node in nodes if isinstance(node, dict))
        != case.static_node_ids
    ):
        raise RunnerAbort("FAIL", "static_graph_node_ids_mismatch")
    call_nodes = {
        node.get("node_id"): node
        for node in nodes
        if isinstance(node, dict) and node.get("node_id") in case.call_node_ids
    }
    observed_job_ids: list[str] = []
    for node_id in case.call_node_ids:
        node = call_nodes.get(node_id)
        executions = node.get("executions") if isinstance(node, dict) else None
        expected_count = case.expected_jobs if case.name == "C" else 1
        if not isinstance(executions, list) or len(executions) != expected_count:
            raise RunnerAbort("FAIL", "task_execution_count_mismatch")
        for execution in executions:
            if (
                not isinstance(execution, dict)
                or execution.get("state") != "succeeded"
                or execution.get("observation_complete") is not True
                or not _valid_identifier(execution.get("execution_id"))
                or not _valid_identifier(execution.get("job_id"))
            ):
                raise RunnerAbort("FAIL", "task_execution_evidence_incomplete")
            observed_job_ids.append(str(execution["job_id"]))
    if len(set(observed_job_ids)) != case.expected_jobs:
        raise RunnerAbort("FAIL", "task_execution_job_identity_mismatch")
    valid_node_ids = set(case.static_node_ids)
    task_completions: dict[str, int] = {node_id: 0 for node_id in case.call_node_ids}
    for event in events:
        if not isinstance(event, dict):
            raise RunnerAbort("FAIL", "task_event_node_id_mismatch")
        event_node_id = event.get("node_id")
        if event_node_id is None:
            if event.get("event_type") != "workflow_completed":
                raise RunnerAbort("FAIL", "task_event_node_id_mismatch")
        elif event_node_id not in valid_node_ids:
            raise RunnerAbort("FAIL", "task_event_node_id_mismatch")
        if event.get("node_id") in task_completions and event.get("event_type") == "task_completed":
            task_completions[str(event["node_id"])] += 1
    if any(
        task_completions[node_id] != (case.expected_jobs if case.name == "C" else 1)
        for node_id in case.call_node_ids
    ):
        raise RunnerAbort("FAIL", "task_event_coverage_mismatch")
    if http_events.get("has_more") is True:
        raise RunnerAbort("UNKNOWN", "task_event_cursor_incomplete")
    return True


def validate_expected_texts(case: RunCase, observed_texts: Sequence[str]) -> bool:
    if tuple(observed_texts) != case.expected_texts:
        raise RunnerAbort("FAIL", "fixed_result_expectation_mismatch")
    return True


def assert_safe_evidence(
    value: Mapping[str, Any], *, secrets_to_reject: Sequence[str] = ()
) -> None:
    forbidden_key_parts = (
        "token",
        "authorization",
        "prompt",
        "final_text",
        "workspace",
        "directory",
        "path",
        "cwd",
    )
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RunnerAbort("BLOCKED", "evidence_not_json_safe") from None
    for secret in secrets_to_reject:
        if secret and secret in encoded:
            raise RunnerAbort("BLOCKED", "evidence_contains_secret")
    for match in (WINDOWS_PATH_PATTERN.search(encoded), POSIX_PATH_PATTERN.search(encoded)):
        if match is not None:
            raise RunnerAbort("BLOCKED", "evidence_contains_absolute_path")
    for forbidden in (
        "Authorization:",
        "Reply with exactly C5-",
        "Reply with only the synthetic issue number:",
    ):
        if forbidden.casefold() in encoded.casefold():
            raise RunnerAbort("BLOCKED", "evidence_contains_raw_prompt_or_auth")

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str) and any(
                    part in key.casefold() for part in forbidden_key_parts
                ):
                    raise RunnerAbort("BLOCKED", "evidence_contains_sensitive_field")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if WINDOWS_PATH_PATTERN.search(item) or POSIX_PATH_PATTERN.search(item):
                raise RunnerAbort("BLOCKED", "evidence_contains_absolute_path")

    visit(value)


def prepare_offline() -> dict[str, Any]:
    missing_sources = [
        name for name in REQUIRED_SOURCE_FILES if not (REPOSITORY_ROOT / name).is_file()
    ]
    missing_tests = [name for name in OFFLINE_TESTS if not (REPOSITORY_ROOT / name).is_file()]
    if missing_sources or missing_tests:
        raise RunnerAbort("BLOCKED", "offline_source_or_test_list_incomplete")
    cases = build_run_cases()
    if tuple(case.expected_jobs for case in cases) != (1, 2, 3):
        raise RunnerAbort("BLOCKED", "offline_job_budget_preview_invalid")
    if (
        sum(case.expected_jobs for case in cases) != MAX_CODEX_JOBS
        or len(cases) != MAX_INITIAL_POSTS
    ):
        raise RunnerAbort("BLOCKED", "offline_run_budget_preview_invalid")
    if not _cli_contract_valid():
        raise RunnerAbort("BLOCKED", "offline_cli_guard_contract_invalid")
    return {
        "status": "PASS",
        "source_count": len(REQUIRED_SOURCE_FILES),
        "test_count": len(OFFLINE_TESTS),
        "run_count": len(cases),
        "initial_post_budget": MAX_INITIAL_POSTS,
        "codex_job_budget": MAX_CODEX_JOBS,
        "fixtures_validated": True,
        "live_calls": 0,
    }


def _cli_contract_valid() -> bool:
    return (
        MAX_INITIAL_POSTS == 3
        and MAX_CODEX_JOBS == 6
        and APPROVAL_ID_PATTERN.fullmatch("a" * 32) is not None
        and HEAD_PATTERN.fullmatch("a" * 40) is not None
        and APPROVAL_ID_PATTERN.fullmatch("A" * 32) is None
        and HEAD_PATTERN.fullmatch("A" * 40) is None
    )


def _git_output(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise RunnerAbort("BLOCKED", "git_preflight_unavailable") from None


def _approved_source_matches(head: str) -> bool:
    path = "scripts/phase_c5_workflow_isolated_acceptance.py"
    try:
        source = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            timeout=20.0,
            check=False,
        )
        if source.returncode != 0:
            return False
        local_bytes = (REPOSITORY_ROOT / path).read_bytes()
    except (OSError, subprocess.SubprocessError):
        return False
    return source.stdout == local_bytes


def _git_preflight(approved_head: str) -> tuple[str, str]:
    head_result = _git_output("rev-parse", "HEAD")
    if head_result.returncode != 0:
        raise RunnerAbort("BLOCKED", "git_head_unavailable")
    current_head = head_result.stdout.strip()
    ancestor_result = _git_output("merge-base", "--is-ancestor", PARENT_HEAD, current_head)
    dirty_result = _git_output(
        "status", "--porcelain", "--untracked-files=all", "--", "src", "scripts", "tests"
    )
    if ancestor_result.returncode not in {0, 1} or dirty_result.returncode != 0:
        raise RunnerAbort("BLOCKED", "git_source_preflight_unavailable")
    source_hash = _sha256(
        (REPOSITORY_ROOT / "scripts/phase_c5_workflow_isolated_acceptance.py").read_bytes()
    )
    validate_git_guard(
        current_head,
        approved_head,
        ancestor=ancestor_result.returncode == 0,
        dirty_paths=tuple(dirty_result.stdout.splitlines()),
        source_matches_head=_approved_source_matches(approved_head),
    )
    return current_head, source_hash


def _environment_preflight() -> tuple[Path, Path]:
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise RunnerAbort("BLOCKED", "windows_native_required")
    try:
        venv_root = (REPOSITORY_ROOT / ".venv").resolve(strict=True)
        python_path = Path(sys.executable).resolve(strict=True)
        prefix = Path(sys.prefix).resolve(strict=True)
    except (OSError, RuntimeError):
        raise RunnerAbort("BLOCKED", "repository_venv_required") from None
    if not python_path.is_relative_to(venv_root) or prefix != venv_root:
        raise RunnerAbort("BLOCKED", "repository_venv_required")
    required_packages = ("dbos", "jq", "jsonschema", "PyYAML", "PySide6", "psutil", "mcp", "httpx2")
    for package in required_packages:
        try:
            importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            raise RunnerAbort("BLOCKED", "live_dependency_unavailable") from None
    try:
        from codex_bridge.codex_resolver import resolve_codex_executable
        from scripts import phase_b5_workflow_isolated_smoke as b5

        codex = Path(
            resolve_codex_executable(config_executable=None, environ=os.environ).path
        ).resolve(strict=True)
        source_home = b5.isolated._source_codex_home(os.environ)
        auth_file = source_home / "auth.json"
        if not codex.is_file() or codex.suffix.casefold() != ".exe":
            raise RunnerAbort("BLOCKED", "isolated_codex_auth_preflight_failed")
        validate_auth_source(
            source_home,
            auth_file,
            owner_matches=_same_owner_as_current_user(auth_file),
            maximum_bytes=b5.isolated.MAX_AUTH_BYTES,
        )
        if b5.missing_workflow_dependencies():
            raise RunnerAbort("BLOCKED", "workflow_dependency_preflight_failed")
    except RunnerAbort:
        raise
    except Exception:
        raise RunnerAbort("BLOCKED", "live_environment_preflight_failed") from None
    return codex, source_home


def validate_auth_source(
    source_home: Path,
    auth_file: Path,
    *,
    owner_matches: bool,
    maximum_bytes: int,
) -> None:
    try:
        resolved_home = source_home.resolve(strict=True)
        resolved_auth = auth_file.resolve(strict=True)
        size = auth_file.stat().st_size
    except (OSError, RuntimeError):
        raise RunnerAbort("BLOCKED", "isolated_codex_auth_preflight_failed") from None
    if (
        _is_reparse_point(source_home)
        or _is_reparse_point(auth_file)
        or not resolved_home.is_dir()
        or not resolved_auth.is_file()
        or resolved_auth.parent != resolved_home
        or auth_file.name != "auth.json"
        or size < 0
        or size > maximum_bytes
        or not owner_matches
    ):
        raise RunnerAbort("BLOCKED", "isolated_codex_auth_preflight_failed")


def _same_owner_as_current_user(path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    token = wintypes.HANDLE()
    descriptor = ctypes.c_void_p()
    file_owner = ctypes.c_void_p()
    current_sid_text = wintypes.LPWSTR()
    file_sid_text = wintypes.LPWSTR()
    try:
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not required.value:
            return False
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, required.value, ctypes.byref(required)
        ):
            return False
        current_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        if not advapi32.ConvertSidToStringSidW(
            current_user.user.sid, ctypes.byref(current_sid_text)
        ):
            return False
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            1,
            0x00000001,
            ctypes.byref(file_owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if result != 0 or not advapi32.ConvertSidToStringSidW(
            file_owner, ctypes.byref(file_sid_text)
        ):
            return False
        return current_sid_text.value == file_sid_text.value
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        if current_sid_text:
            kernel32.LocalFree(ctypes.cast(current_sid_text, ctypes.c_void_p))
        if file_sid_text:
            kernel32.LocalFree(ctypes.cast(file_sid_text, ctypes.c_void_p))
        if descriptor:
            kernel32.LocalFree(descriptor)
        if token:
            kernel32.CloseHandle(token)


def live_preflight(approved_head: str, approval_id: str) -> dict[str, Any]:
    if (
        HEAD_PATTERN.fullmatch(approved_head) is None
        or APPROVAL_ID_PATTERN.fullmatch(approval_id) is None
    ):
        raise RunnerAbort("BLOCKED", "execution_guards_invalid")
    current_head, source_hash = _git_preflight(approved_head)
    codex, source_home = _environment_preflight()
    evidence_json = REPOSITORY_ROOT / "docs" / f"phase_c5_workflow_acceptance-{approval_id}.json"
    summary_md = REPOSITORY_ROOT / "docs" / "phase_c5_p2_live_acceptance.md"
    if evidence_json.exists() or summary_md.exists():
        raise RunnerAbort("BLOCKED", "live_evidence_output_already_exists")
    return {
        "approved_head": current_head,
        "runner_source_sha256": source_hash,
        "codex_executable": codex,
        "source_home": source_home,
    }


def _b5() -> Any:
    from scripts import phase_b5_workflow_isolated_smoke as b5

    return b5


def start_isolated_bridge(context: Any, *, restart: bool = False) -> dict[str, Any]:
    result = _b5()._start_bridge(context, restart=restart)
    if not isinstance(result, dict):
        raise RunnerAbort("BLOCKED", "isolated_bridge_start_result_invalid")
    return result


def initial_workflow_post(context: Any, payload: Mapping[str, Any]) -> tuple[int, Any]:
    status, body, _headers = _b5()._http_json(
        context,
        "POST",
        "/ui-api/control/workflows",
        token=context.control_token,
        payload=payload,
        timeout=25.0,
    )
    return status, body


def _read_events_http(context: Any, workflow_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    b5 = _b5()
    tasks_status, tasks, _ = b5._http_json(
        context,
        "GET",
        f"/ui-api/control/workflows/{quote(workflow_id, safe='')}/tasks",
        token=context.control_token,
    )
    if tasks_status != 200 or not isinstance(tasks, dict):
        raise RunnerAbort("UNKNOWN", "workflow_task_http_readback_unavailable")
    pages: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(8):
        events_route = (
            f"/ui-api/control/workflows/{quote(workflow_id, safe='')}"
            f"/task-events?after_id={cursor}&limit=100"
        )
        status, page, _ = b5._http_json(
            context,
            "GET",
            events_route,
            token=context.control_token,
        )
        if status != 200 or not isinstance(page, dict) or not isinstance(page.get("events"), list):
            raise RunnerAbort("UNKNOWN", "workflow_task_events_http_readback_unavailable")
        pages.extend(row for row in page["events"] if isinstance(row, dict))
        if page.get("has_more") is not True:
            combined = dict(page)
            combined["events"] = pages
            return tasks, combined
        next_cursor = page.get("next_after_id")
        if type(next_cursor) is not int or next_cursor <= cursor:
            raise RunnerAbort("UNKNOWN", "workflow_task_event_cursor_invalid")
        cursor = next_cursor
    raise RunnerAbort("UNKNOWN", "workflow_task_event_pages_exceeded")


def _read_events_mcp(context: Any, workflow_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    b5 = _b5()
    names, results = b5._mcp_exchange(
        mcp_port=context.ports[0],
        mcp_token=context.mcp_token,
        calls=(
            ("workflow_run_tasks", {"workflow_id": workflow_id}),
            ("workflow_run_events", {"workflow_id": workflow_id, "after_id": 0, "limit": 100}),
        ),
    )
    if not {"workflow_run_tasks", "workflow_run_events"} <= names:
        raise RunnerAbort("UNKNOWN", "workflow_task_mcp_tools_unavailable")
    tasks, events = results
    if not isinstance(tasks, dict) or not isinstance(events, dict):
        raise RunnerAbort("UNKNOWN", "workflow_task_mcp_readback_unavailable")
    if events.get("has_more") is True:
        pages = list(events.get("events", []))
        cursor = events.get("next_after_id")
        for _ in range(8):
            if type(cursor) is not int:
                raise RunnerAbort("UNKNOWN", "workflow_task_mcp_cursor_invalid")
            _names, next_results = b5._mcp_exchange(
                mcp_port=context.ports[0],
                mcp_token=context.mcp_token,
                calls=(
                    (
                        "workflow_run_events",
                        {"workflow_id": workflow_id, "after_id": cursor, "limit": 100},
                    ),
                ),
            )
            page = next_results[0]
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise RunnerAbort("UNKNOWN", "workflow_task_mcp_readback_unavailable")
            pages.extend(row for row in page["events"] if isinstance(row, dict))
            if page.get("has_more") is not True:
                events = dict(page)
                events["events"] = pages
                break
            next_cursor = page.get("next_after_id")
            if type(next_cursor) is not int or next_cursor <= cursor:
                raise RunnerAbort("UNKNOWN", "workflow_task_mcp_cursor_invalid")
            cursor = next_cursor
        else:
            raise RunnerAbort("UNKNOWN", "workflow_task_mcp_event_pages_exceeded")
    return tasks, events


class LiveAdapter:
    def __init__(self, context: Any, ledger: AttemptLedger) -> None:
        self.context = context
        self.ledger = ledger
        self.initial_posts = 0
        self._worker_observer: Any | None = None
        self._snapshots: dict[str, str] = {}

    def begin_run(self, _case: RunCase) -> None:
        b5 = _b5()
        self._worker_observer = b5._OwnedWorkerObserver(self.context)
        self._worker_observer.start()

    def start(self, case: RunCase, client_request_id: str) -> tuple[int, Any]:
        self._assert_bridge_ready()
        payload = {
            "client_request_id": client_request_id,
            "workflow_yaml": case.workflow_yaml,
            "input": case.input_data,
            "workspace_root": str(self.context.workspace.resolve(strict=True)),
            "max_live_codex_jobs": case.expected_jobs,
        }
        self.initial_posts += 1
        return initial_workflow_post(self.context, payload)

    def get_status(self, workflow_id: str) -> tuple[int, Any]:
        status, detail, _ = _b5()._http_json(
            self.context,
            "GET",
            f"/ui-api/control/workflows/{quote(workflow_id, safe='')}",
            token=self.context.control_token,
            timeout=15.0,
        )
        return status, detail

    def get_result(self, workflow_id: str) -> tuple[int, Any]:
        status, result, _ = _b5()._http_json(
            self.context,
            "GET",
            f"/ui-api/control/workflows/{quote(workflow_id, safe='')}/result",
            token=self.context.control_token,
            timeout=15.0,
        )
        return status, result

    def verify(
        self,
        case: RunCase,
        accepted: dict[str, Any],
        detail: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        report = _verify_live_run(self.context, case, accepted, detail, result)
        cumulative_jobs = count_isolated_codex_jobs(self.context.state_root)
        if (
            cumulative_jobs != self.ledger.job_count + case.expected_jobs
            or cumulative_jobs > MAX_CODEX_JOBS
        ):
            raise RunnerAbort("FAIL", "cumulative_job_ledger_mismatch")
        if self._worker_observer is None:
            raise RunnerAbort("BLOCKED", "owned_worker_observer_missing")
        self._worker_observer.finish()
        b5 = _b5()
        ownership = b5._verify_worker_ownership(
            self.context,
            run_id=str(accepted["run_id"]),
            workflow_id=str(accepted["workflow_id"]),
            observer=self._worker_observer,
        )
        self._worker_observer = None
        if ownership.get("status") != "PASS":
            raise RunnerAbort("FAIL", "owned_worker_process_verification_failed")
        report["worker_owner_verified"] = True
        return report

    def diagnose_unknown(self, case: RunCase, client_request_id: str) -> None:
        state_root = self.context.state_root
        database = state_root / "requests.sqlite"
        if not database.exists():
            return
        _b5()._read_sqlite(
            database,
            "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests "
            "WHERE client_request_id = ?",
            (client_request_id,),
        )
        # The read-only lookup is diagnostic only; its result never resumes or resubmits a run.
        _ = case.name

    def diagnose_accepted(self, case: RunCase, accepted: Mapping[str, Any]) -> None:
        client_request_id = accepted.get("client_request_id")
        if not isinstance(client_request_id, str):
            return
        self.diagnose_unknown(case, client_request_id)

    def readonly_restart(self, completed: list[dict[str, Any]]) -> dict[str, Any]:
        b5 = _b5()
        context = self.context
        if len(completed) != 3 or self.initial_posts != 3:
            raise RunnerAbort("BLOCKED", "restart_gate_requires_three_completed_runs")
        before = {str(item["workflow_id"]): self._stable_run_snapshot(item) for item in completed}
        process = context.process
        identity = context.identity
        if process is None or identity is None:
            raise RunnerAbort("BLOCKED", "owned_bridge_identity_missing_for_restart")
        stop_owned_process(
            process,
            identity,
            verify_owner=lambda owned, expected: b5.phase5f5._assert_bridge_identity(
                owned, expected
            ),
            stop_process=lambda owned: b5.stop_owned_bridge(
                owned,
                identity,
                ui_port=context.ports[1],
                control_token=context.control_token,
            ),
        )
        if process.poll() is None:
            raise RunnerAbort("BLOCKED", "owned_bridge_shutdown_not_confirmed")
        new_ports, reservations = b5._restart_ports(context.ports)
        try:
            b5._release_reserved_ports(new_ports, reservations)
            if not b5.ports_are_safe(*new_ports) or set(new_ports) & PRODUCTION_PORTS:
                raise RunnerAbort("BLOCKED", "restart_ports_unsafe")
            context.ports = new_ports
            environment, _config, state_root = b5._build_runtime_environment(
                temp_root=context.temp_root,
                workspace=context.workspace,
                codex_executable=context.codex_executable,
                source_home=b5.isolated._source_codex_home(os.environ),
                control_token=context.control_token,
                mcp_token=context.mcp_token,
                ports=new_ports,
                config_name="bridge-c5-restart.toml",
            )
            if state_root != context.state_root:
                raise RunnerAbort("BLOCKED", "restart_state_root_changed")
            context.environment = environment
            b5.isolated._assert_ports_free(new_ports)
            start_isolated_bridge(context, restart=True)
            after = {
                str(item["workflow_id"]): self._stable_run_snapshot(item) for item in completed
            }
            if before != after or self.initial_posts != 3:
                raise RunnerAbort("FAIL", "readonly_restart_snapshot_changed")
            if (
                b5.isolated._worker_process_count(context.process.pid if context.process else -1)
                != 0
            ):
                raise RunnerAbort("BLOCKED", "worker_process_remained_after_readonly_restart")
            return {
                "status": "PASS",
                "new_post_count": 0,
                "new_job_count": 0,
                "snapshots_unchanged": True,
            }
        finally:
            for reservation in reservations:
                try:
                    reservation.close()
                except OSError:
                    pass

    def _stable_run_snapshot(self, item: Mapping[str, Any]) -> str:
        workflow_id = str(item["workflow_id"])
        run_id = str(item["run_id"])
        status, detail = self.get_status(workflow_id)
        result_status, result = self.get_result(workflow_id)
        if (
            status != 200
            or result_status != 200
            or not isinstance(detail, dict)
            or not isinstance(result, dict)
        ):
            raise RunnerAbort("UNKNOWN", "restart_http_readback_unavailable")
        if detail.get("run_id") != run_id or detail.get("status") != "succeeded":
            raise RunnerAbort("FAIL", "restart_workflow_identity_or_status_mismatch")
        tasks_http, events_http = _read_events_http(self.context, workflow_id)
        tasks_mcp, events_mcp = _read_events_mcp(self.context, workflow_id)
        case = next(case for case in build_run_cases() if case.name == item["name"])
        validate_projection(
            case, workflow_id, run_id, tasks_http, events_http, tasks_mcp, events_mcp
        )
        b5 = _b5()
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        b5._qt_readback(self.context, detail, result)
        run_dir = b5.isolated._run_directory(self.context.state_root, run_id)
        database = run_dir / "codex_jobs.sqlite"
        jobs, job_events = b5._read_codex_job_ledger(database)
        dbos_status = b5.isolated._read_dbos_status(run_dir, workflow_id)
        summary = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": detail.get("status"),
            "snapshot_version": tasks_http.get("snapshot_version"),
            "event_cursor": events_http.get("latest_event_id"),
            "events": events_http.get("events"),
            "dbos_status": dbos_status,
            "jobs": jobs,
            "job_events": job_events,
            "result_sha256": _sha256(_canonical_json(result).encode("utf-8")),
            "manifest_sha256": _sha256((run_dir / "manifest.json").read_bytes()),
            "request_rows": b5._read_sqlite(
                self.context.state_root / "requests.sqlite",
                "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests "
                "WHERE workflow_id = ? AND run_id = ?",
                (workflow_id, run_id),
            ),
        }
        return _sha256(_canonical_json(summary).encode("utf-8"))

    def _assert_bridge_ready(self) -> None:
        b5 = _b5()
        context = self.context
        if context.process is None or context.identity is None:
            raise RunnerAbort("BLOCKED", "owned_bridge_identity_missing_before_post")
        b5.phase5f5._assert_bridge_identity(context.process, context.identity)
        b5.isolated._owned_listener_pids(context.process.pid, context.ports)
        status = b5.isolated._preflight_bridge(
            context.process,
            mcp_port=context.ports[0],
            ui_port=context.ports[1],
            token=context.control_token,
            expected_codex=context.codex_executable,
        )
        if (
            status.get("appserver_pid") != context.app_server_pid
            or b5.missing_workflow_dependencies()
        ):
            raise RunnerAbort("BLOCKED", "isolated_bridge_pre_post_guard_failed")
        if context.workspace.is_symlink() or any(context.workspace.iterdir()):
            raise RunnerAbort("BLOCKED", "isolated_workspace_not_empty")
        if b5.isolated._worker_process_count(context.process.pid) != 0:
            raise RunnerAbort("BLOCKED", "unexpected_worker_before_run")
        request_database = context.state_root / "requests.sqlite"
        request_rows: list[dict[str, Any]] = []
        if request_database.exists():
            request_rows = _readonly_sqlite(
                request_database,
                "SELECT client_request_id, workflow_id, run_id, state FROM workflow_requests",
            )
        expected_request_ids = {
            str(run["client_request_id"])
            for run in self.ledger.data.get("runs", {}).values()
            if isinstance(run, dict) and isinstance(run.get("client_request_id"), str)
        }
        observed_request_ids = {
            str(row.get("client_request_id")) for row in request_rows if isinstance(row, dict)
        }
        if len(request_rows) != self.initial_posts or observed_request_ids != expected_request_ids:
            raise RunnerAbort("BLOCKED", "unexpected_isolated_workflow_request_count")


def _readonly_sqlite(
    path: Path, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    b5 = _b5()
    rows = b5._read_sqlite(path, query, parameters)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RunnerAbort("UNKNOWN", "isolated_sqlite_readback_invalid")
    return rows


def count_isolated_codex_jobs(state_root: Path) -> int:
    try:
        root = state_root.resolve(strict=True)
        run_directories = tuple(root.iterdir())
    except (OSError, RuntimeError):
        raise RunnerAbort("UNKNOWN", "isolated_run_ledger_unavailable") from None
    total = 0
    for run_directory in run_directories:
        if _is_reparse_point(run_directory):
            raise RunnerAbort("UNKNOWN", "isolated_run_root_reparse_point")
        if not run_directory.is_dir():
            continue
        database = run_directory / "codex_jobs.sqlite"
        if database.exists():
            total += len(_readonly_sqlite(database, "SELECT job_id FROM jobs ORDER BY rowid"))
    return total


def _workflow_request_digest(
    case: RunCase, definition: Mapping[str, Any], workspace_root: str
) -> str:
    digest_payload: dict[str, Any] = {
        "workflow": definition,
        "input": case.input_data,
        "workspace_root": workspace_root,
    }
    if case.expected_jobs in {2, 3}:
        digest_payload["max_live_codex_jobs"] = case.expected_jobs
    return _sha256(_canonical_json(digest_payload).encode("utf-8"))


def _verify_live_run(
    context: Any,
    case: RunCase,
    accepted: Mapping[str, Any],
    detail: Mapping[str, Any],
    api_result: Mapping[str, Any],
) -> dict[str, Any]:
    b5 = _b5()
    workflow_id = accepted.get("workflow_id")
    run_id = accepted.get("run_id")
    client_request_id = accepted.get("client_request_id")
    if not all(_valid_identifier(value) for value in (workflow_id, run_id, client_request_id)):
        raise RunnerAbort("FAIL", "accepted_workflow_identity_invalid")
    work_root = str(context.workspace.resolve(strict=True))
    requests_db = context.state_root / "requests.sqlite"
    request_rows = _readonly_sqlite(
        requests_db,
        "SELECT client_request_id, workflow_id, run_id, request_digest, state "
        "FROM workflow_requests WHERE client_request_id = ?",
        (client_request_id,),
    )
    if (
        len(request_rows) != 1
        or request_rows[0].get("workflow_id") != workflow_id
        or request_rows[0].get("run_id") != run_id
        or request_rows[0].get("state") not in {"running", "succeeded"}
    ):
        raise RunnerAbort("FAIL", "workflow_request_ledger_mismatch")
    run_dir = b5.isolated._run_directory(context.state_root, str(run_id))
    manifest = b5._read_json(run_dir / "manifest.json", maximum=32 * 1024)
    stored_result = b5._read_json(run_dir / "result.json")
    worker_status = b5._read_json(run_dir / "worker-entry-status.json", maximum=8192)
    stored_definition = b5._read_json(run_dir / "workflow.yaml", maximum=64 * 1024)
    stored_input = b5._read_json(run_dir / "input.json", maximum=64 * 1024)
    jobs, job_events = b5._read_codex_job_ledger(run_dir / "codex_jobs.sqlite")
    dbos_status = b5.isolated._read_dbos_status(run_dir, str(workflow_id))
    definition = yaml.safe_load(case.workflow_yaml)
    definition_hash = _sha256((_canonical_json(definition) + "\n").encode("utf-8"))
    input_hash = _sha256(_canonical_json(case.input_data).encode("utf-8"))
    request_digest = _workflow_request_digest(case, definition, work_root)
    if request_rows[0].get("request_digest") != request_digest:
        raise RunnerAbort("FAIL", "workflow_request_digest_mismatch")
    if (
        not isinstance(manifest, dict)
        or manifest.get("workflow_id") != workflow_id
        or manifest.get("run_id") != run_id
        or manifest.get("workflow_source") != "ui-api"
        or manifest.get("mode") != "codex-job-live"
        or manifest.get("workspace_root") != work_root
        or manifest.get("definition_sha256") != definition_hash
        or manifest.get("input_sha256") != input_hash
        or manifest.get("request_digest") != request_digest
        or stored_definition != definition
        or stored_input != case.input_data
        or not isinstance(worker_status, dict)
        or worker_status.get("state") != "completed"
        or worker_status.get("exit_code") != 0
        or dbos_status != "SUCCESS"
        or len(jobs) != case.expected_jobs
        or not jobs
        or not isinstance(stored_result, dict)
        or dict(stored_result) != dict(api_result)
        or detail.get("workflow_id") != workflow_id
        or detail.get("run_id") != run_id
        or detail.get("status") != "succeeded"
    ):
        raise RunnerAbort("FAIL", "workflow_manifest_worker_or_dbos_mismatch")
    observed_texts: list[str] = []
    job_ids: set[str] = set()
    for job in jobs:
        if (
            job.get("state") != "SUCCEEDED"
            or job.get("workflow_id") != workflow_id
            or not _valid_identifier(job.get("job_id"))
            or job.get("job_id") in job_ids
            or not isinstance(job.get("request_key"), str)
            or not isinstance(job.get("task_identity"), str)
            or job.get("request_key") != f"{workflow_id}::{job['task_identity']}"
            or job.get("remote_state") != "completed"
            or not isinstance(job.get("receipt_json"), str)
        ):
            raise RunnerAbort("FAIL", "codex_job_ledger_mismatch")
        job_ids.add(str(job["job_id"]))
        try:
            receipt = json.loads(job["receipt_json"])
        except json.JSONDecodeError:
            raise RunnerAbort("FAIL", "codex_job_receipt_invalid") from None
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("result"), dict)
            or not isinstance(receipt["result"].get("final_text"), str)
        ):
            raise RunnerAbort("FAIL", "fixed_result_expectation_mismatch")
        observed_texts.append(receipt["result"]["final_text"])
    validate_expected_texts(case, observed_texts)
    if (
        len(job_events) != 2 * case.expected_jobs
        or sum(event.get("state") == "MAYBE_SENT" for event in job_events) != case.expected_jobs
        or sum(event.get("state") == "SUCCEEDED" for event in job_events) != case.expected_jobs
        or any(event.get("job_id") not in job_ids for event in job_events)
    ):
        raise RunnerAbort("FAIL", "codex_job_start_marker_or_event_mismatch")
    tasks_http, events_http = _read_events_http(context, str(workflow_id))
    tasks_mcp, events_mcp = _read_events_mcp(context, str(workflow_id))
    validate_projection(
        case,
        str(workflow_id),
        str(run_id),
        tasks_http,
        events_http,
        tasks_mcp,
        events_mcp,
    )
    projected_job_ids = {
        str(execution["job_id"])
        for node in tasks_http["nodes"]
        if isinstance(node, dict) and node.get("node_id") in case.call_node_ids
        for execution in node.get("executions", [])
        if isinstance(execution, dict) and isinstance(execution.get("job_id"), str)
    }
    if projected_job_ids != job_ids:
        raise RunnerAbort("FAIL", "task_graph_job_ledger_mismatch")
    return {
        "job_count": len(jobs),
        "result_matches": True,
        "graph_matches": True,
        "request_state": request_rows[0].get("state"),
        "dbos_status": dbos_status,
        "worker_state": worker_status.get("state"),
        "snapshot_version": tasks_http.get("snapshot_version"),
        "latest_event_id": events_http.get("latest_event_id"),
        "result_sha256": _sha256(_canonical_json(api_result).encode("utf-8")),
        "manifest_sha256": _sha256((run_dir / "manifest.json").read_bytes()),
    }


def _create_evidence(approval_id: str, preflight: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "phase": "C5-P2A",
        "approval_id": approval_id,
        "approved_source_head": preflight.get("approved_head"),
        "runner_source_sha256": preflight.get("runner_source_sha256"),
        "status": "RUNNING",
        "budget": {
            "planned_runs": 3,
            "max_initial_workflow_posts": MAX_INITIAL_POSTS,
            "max_codex_jobs": MAX_CODEX_JOBS,
            "observed_initial_workflow_posts": 0,
            "observed_codex_jobs": 0,
        },
        "gates": {
            "gate0": "NOT_RUN",
            "gate1": "NOT_RUN",
            "gate2": "NOT_RUN",
            "gate3": "NOT_RUN",
        },
        "runs": [
            {
                "name": case.name,
                "expected_job_count": case.expected_jobs,
                "status": "NOT_RUN",
                "initial_post_count": 0,
                "observed_job_count": 0,
            }
            for case in build_run_cases()
        ],
        "gui_human_checklist": [
            "Open the isolated GUI and inspect Graph and Result tabs.",
            "Check node selection, 3-second refresh stability, and both "
            "900x760 and 720x600 layouts.",
            "Check Japanese text, scrolling, and the distinction between unknown "
            "and needs_reconciliation.",
            "Record a human visual decision separately; offscreen Qt is not a human PASS.",
        ],
    }


def _apply_failure_state(
    evidence: dict[str, Any], ledger: AttemptLedger, status: str, code: str
) -> None:
    evidence["status"] = status
    evidence["terminal_code"] = code
    evidence["budget"]["observed_initial_workflow_posts"] = ledger.post_count
    evidence["budget"]["observed_codex_jobs"] = ledger.job_count
    ledger_runs = ledger.data.get("runs", {})
    evidence_runs = {row["name"]: row for row in evidence["runs"]}
    for name, jobs, *_rest in _case_definitions():
        record = ledger_runs.get(name, {}) if isinstance(ledger_runs, dict) else {}
        row = evidence_runs[name]
        run_state = str(record.get("state", "not_started"))
        row["status"] = "NOT_RUN" if run_state == "not_started" else run_state.upper()
        row["initial_post_count"] = int(record.get("client_request_id") is not None)
        row["observed_job_count"] = int(record.get("observed_jobs", 0))
        row["expected_job_count"] = jobs
    if ledger.post_count:
        gate_status = status if status in {"FAIL", "BLOCKED"} else "BLOCKED"
        all_runs_succeeded = all(
            ledger.run_states.get(name) == "succeeded" for name, *_rest in _case_definitions()
        )
        evidence["gates"]["gate2"] = "PASS" if all_runs_succeeded else gate_status
        if all_runs_succeeded:
            evidence["gates"]["gate3"] = gate_status


def _apply_ledger_to_evidence(evidence: dict[str, Any], ledger: AttemptLedger) -> None:
    evidence["budget"]["observed_initial_workflow_posts"] = ledger.post_count
    evidence["budget"]["observed_codex_jobs"] = ledger.job_count
    for row in evidence["runs"]:
        state = ledger.run_states.get(row["name"], "not_started")
        run_data = ledger.data.get("runs", {}).get(row["name"], {})
        row["status"] = "NOT_RUN" if state == "not_started" else state.upper()
        row["initial_post_count"] = int(state != "not_started")
        row["observed_job_count"] = int(run_data.get("observed_jobs", 0))
        if isinstance(run_data.get("workflow_id"), str):
            row["workflow_id"] = run_data["workflow_id"]
        if isinstance(run_data.get("run_id"), str):
            row["run_id"] = run_data["run_id"]


def _write_live_evidence(
    evidence: dict[str, Any], approval_id: str, secrets_to_reject: Sequence[str]
) -> None:
    assert_safe_evidence(evidence, secrets_to_reject=secrets_to_reject)
    docs = REPOSITORY_ROOT / "docs"
    evidence_path = docs / f"phase_c5_workflow_acceptance-{approval_id}.json"
    summary_path = docs / "phase_c5_p2_live_acceptance.md"
    if evidence_path.exists() or summary_path.exists():
        raise RunnerAbort("BLOCKED", "live_evidence_output_already_exists")
    _atomic_json(evidence_path, evidence)
    gates = evidence.get("gates", {})
    budget = evidence.get("budget", {})
    summary = (
        "# Phase C5-P2 live acceptance\n\n"
        f"- Status: {evidence.get('status')}\n"
        f"- Source HEAD: {evidence.get('approved_source_head')}\n"
        f"- Runner source SHA-256: {evidence.get('runner_source_sha256')}\n"
        f"- Gate 0: {gates.get('gate0')}\n"
        f"- Gate 1 human GUI: {gates.get('gate1')}\n"
        f"- Gate 2 1/2/3 jobs: {gates.get('gate2')}\n"
        f"- Gate 3 read-only restart: {gates.get('gate3')}\n"
        f"- Initial POSTs: {budget.get('observed_initial_workflow_posts')}/{MAX_INITIAL_POSTS}\n"
        f"- Codex jobs: {budget.get('observed_codex_jobs')}/{MAX_CODEX_JOBS}\n\n"
        "Gate 1 remains NOT_RUN until a person inspects the isolated visible GUI.\n"
    )
    if WINDOWS_PATH_PATTERN.search(summary) or POSIX_PATH_PATTERN.search(summary):
        raise RunnerAbort("BLOCKED", "live_summary_contains_absolute_path")
    try:
        with summary_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(summary)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RunnerAbort("BLOCKED", "live_summary_write_failed") from None


def execute_live(approved_head: str, approval_id: str) -> int:
    evidence: dict[str, Any] | None = None
    ledger: AttemptLedger | None = None
    context: Any | None = None
    adapter: LiveAdapter | None = None
    secrets_to_reject: tuple[str, ...] = ()
    exit_code = 2
    preflight: dict[str, Any] = {
        "approved_head": approved_head,
        "runner_source_sha256": _sha256(Path(__file__).read_bytes()),
    }
    evidence = _create_evidence(approval_id, preflight)
    try:
        ledger = AttemptLedger.create(
            REPOSITORY_ROOT / "docs" / ".phase_c5_attempt_budgets",
            approval_id,
            approved_head,
            str(preflight["runner_source_sha256"]),
        )
        preflight = live_preflight(approved_head, approval_id)
        evidence = _create_evidence(approval_id, preflight)
        evidence["gates"]["gate0"] = "PASS"
        b5 = _b5()
        context = b5._make_isolated_context(preflight["codex_executable"], preflight["source_home"])
        secrets_to_reject = (context.control_token, context.mcp_token)
        validate_isolated_layout(
            context.temp_root, context.state_root, context.workspace, context.ports
        )
        for key in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "CODEX_HOME"):
            value = context.environment.get(key)
            if not isinstance(value, str) or not Path(value).resolve().is_relative_to(
                context.temp_root.resolve()
            ):
                raise RunnerAbort("BLOCKED", "isolated_environment_root_mismatch")
        if context.control_token == context.mcp_token:
            raise RunnerAbort("BLOCKED", "isolated_tokens_not_distinct")
        start_isolated_bridge(context)
        adapter = LiveAdapter(context, ledger)
        outcome = execute_runs(build_run_cases(), ledger, adapter)
        evidence["gates"]["gate0"] = "PASS"
        evidence["gates"]["gate2"] = outcome["gate2"]
        evidence["gates"]["gate3"] = outcome["gate3"]
        evidence["runs"] = outcome["completed_runs"]
        evidence["budget"]["observed_initial_workflow_posts"] = ledger.post_count
        evidence["budget"]["observed_codex_jobs"] = ledger.job_count
        evidence["status"] = "LIVE_RUNS_PASS_GUI_NOT_RUN"
        ledger.data["status"] = "live_runs_pass_gui_not_run"
        ledger._save("live_runs_pass_gui_gate_not_run")
        exit_code = 0
    except RunnerAbort as error:
        if evidence is not None and ledger is not None:
            _apply_failure_state(evidence, ledger, error.status, error.code)
        elif evidence is not None:
            evidence["status"] = error.status
            evidence["terminal_code"] = error.code
            evidence["gates"]["gate0"] = "BLOCKED"
        if evidence is not None and ledger is not None and not ledger.post_count:
            evidence["gates"]["gate0"] = "BLOCKED"
        if ledger is not None and ledger.data.get("status") == "started":
            ledger.mark_terminal(None, error.status, error.code)
    except Exception:
        if evidence is not None and ledger is not None:
            _apply_failure_state(evidence, ledger, "BLOCKED", "live_runner_unexpected_failure")
        elif evidence is not None:
            evidence["status"] = "BLOCKED"
            evidence["terminal_code"] = "live_runner_unexpected_failure"
            evidence["gates"]["gate0"] = "BLOCKED"
        if evidence is not None and ledger is not None and not ledger.post_count:
            evidence["gates"]["gate0"] = "BLOCKED"
        if ledger is not None and ledger.data.get("status") == "started":
            ledger.mark_terminal(None, "BLOCKED", "live_runner_unexpected_failure")
    finally:
        if adapter is not None and adapter._worker_observer is not None:
            try:
                adapter._worker_observer.finish()
            except Exception:
                if evidence is not None:
                    evidence["gates"]["gate3"] = "BLOCKED"
        if context is not None and context.process is not None:
            try:
                if context.process.poll() is None:
                    b5 = _b5()
                    identity = context.identity
                    if identity is None:
                        raise RunnerAbort("BLOCKED", "owned_bridge_identity_missing_for_cleanup")
                    stop_owned_process(
                        context.process,
                        identity,
                        verify_owner=lambda owned, expected: b5.phase5f5._assert_bridge_identity(
                            owned, expected
                        ),
                        stop_process=lambda owned: b5.stop_owned_bridge(
                            owned,
                            identity,
                            ui_port=context.ports[1],
                            control_token=context.control_token,
                        ),
                    )
                if context.process.poll() is None:
                    raise RunnerAbort("BLOCKED", "owned_bridge_shutdown_not_confirmed")
            except Exception:
                if evidence is not None:
                    evidence["gates"]["gate3"] = "BLOCKED"
                    evidence["status"] = "BLOCKED"
                    evidence["terminal_code"] = "owned_shutdown_unconfirmed"
                    exit_code = 2
        if ledger is not None:
            _apply_ledger_to_evidence(evidence, ledger)
        if evidence is not None:
            try:
                _write_live_evidence(evidence, approval_id, secrets_to_reject)
            except RunnerAbort:
                print("BLOCKED: live evidence could not be safely written")
                exit_code = 2
    if exit_code == 0:
        print("PASS: Gate 2 and Gate 3 complete; Gate 1 GUI visual check remains NOT_RUN")
    else:
        print("BLOCKED: live acceptance stopped; inspect the fixed diagnostic code in evidence")
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.phase_c5_workflow_isolated_acceptance",
        description="Prepare offline checks or run an explicitly approved isolated C5 acceptance.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="run only offline preflight")
    mode.add_argument(
        "--execute", action="store_true", help="run the explicitly approved isolated acceptance"
    )
    parser.add_argument("--approved-head", help="exact reviewed 40-character commit SHA")
    parser.add_argument(
        "--approval-id", help="externally supplied 32-character lowercase attempt ID"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    if not arguments:
        parser.print_help()
        return 0
    options = parser.parse_args(arguments)
    if options.prepare:
        if options.approved_head is not None or options.approval_id is not None:
            print("BLOCKED: --prepare does not accept live approval values")
            return 2
        try:
            preview = prepare_offline()
        except RunnerAbort as error:
            print(f"BLOCKED: {error.code}")
            return 2
        print(
            "PASS: offline prepare only; "
            f"runs={preview['run_count']} posts={preview['initial_post_budget']} "
            f"jobs={preview['codex_job_budget']} live_calls=0"
        )
        return 0
    if not options.execute or options.approved_head is None or options.approval_id is None:
        print("BLOCKED: --execute requires --approved-head and --approval-id")
        return 2
    if (
        HEAD_PATTERN.fullmatch(options.approved_head) is None
        or APPROVAL_ID_PATTERN.fullmatch(options.approval_id) is None
    ):
        print("BLOCKED: approval values have an invalid fixed format")
        return 2
    return execute_live(options.approved_head, options.approval_id)


if __name__ == "__main__":
    raise SystemExit(main())
