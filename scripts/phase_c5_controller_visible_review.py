"""Offline synthetic review harness for the Phase C5 workflow controller."""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QWidget

from codex_bridge.console.i18n import initialize_language, language
from codex_bridge.console.workflow_controller import WorkflowManagementController
from codex_bridge.console.workflow_graph_widget import _validate_snapshot

TITLE = "Phase C5 合成データ / 実行不可 / 外部通信なし"
_SIZES = {"regular": (900, 760), "compact": (720, 600)}
_FORBIDDEN_IMPORTS = frozenset(
    {"dbos", "http", "httpx", "mcp", "requests", "socket", "subprocess", "urllib"}
)
_EVENT_CHANGE_LIST_REQUEST = 5


class _ProcessOnlySettings:
    """Tiny settings adapter that never reads or writes QSettings storage."""

    def __init__(self, selected_language: str) -> None:
        self._selected_language = selected_language

    def value(self, key: str, default: object = None) -> object:
        return self._selected_language if key == "language" else default


class _MemorySignal:
    """Minimal synchronous signal contract used only by the synthetic client."""

    def __init__(self) -> None:
        self._slots: list[Callable[..., None]] = []

    def connect(self, slot: Callable[..., None]) -> None:
        if slot not in self._slots:
            self._slots.append(slot)

    def disconnect(self, slot: Callable[..., None]) -> None:
        if slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args: object) -> None:
        for slot in tuple(self._slots):
            slot(*args)


@dataclass(frozen=True)
class SyntheticRead:
    kind: str
    key: str
    workflow_id: str | None = None
    after_id: int | None = None


def _workflow_rows() -> list[dict[str, object]]:
    return [
        {
            "workflow_id": f"synthetic-workflow-{index:03d}",
            "status": "succeeded",
            "created_at": "2026-11-18T09:00:00Z",
            "updated_at": "2026-11-18T09:01:00Z",
        }
        for index in range(1, 51)
    ]


def _run_id(workflow_id: str) -> str:
    return f"synthetic-run-{workflow_id[-3:]}"


def _workflow_detail(workflow_id: str) -> dict[str, object]:
    return {
        "workflow_id": workflow_id,
        "run_id": _run_id(workflow_id),
        "status": "succeeded",
        "created_at": "2026-11-18T09:00:00Z",
        "updated_at": "2026-11-18T09:01:00Z",
        "worker_state": "succeeded",
        "terminal": True,
        "job_id": None,
        "thread_id": None,
    }


def _workflow_result(workflow_id: str) -> dict[str, object]:
    return {
        "result": {
            "final_text": f"合成結果: {workflow_id} の表示確認用データです。",
            "summary": "FakeWorkflowClient が返したインメモリ結果です。",
        }
    }


def _graph_snapshot(workflow_id: str, run_id: str, *, changed: bool) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    for index, operation in enumerate(("set", "do", "if", "call", "set"), start=1):
        node_id = f"/synthetic/task-{index:02d}"
        nodes.append(
            {
                "node_id": node_id,
                "operation": operation,
                "parent_scope": "synthetic",
                "order": index - 1,
                "state": "succeeded",
                "completion_observed": True,
                "executions": [
                    {
                        "execution_id": f"synthetic-execution-{index:02d}",
                        "task_identity": f"synthetic-task-{index:02d}",
                        "state": "succeeded",
                        "started_at": "2026-11-18T09:00:00Z",
                        "completed_at": "2026-11-18T09:01:00Z",
                        "completion_observed": True,
                        "observation_complete": True,
                        "evidence_sources": [],
                        "evidence_conflict": False,
                    }
                ],
            }
        )
    edges = [
        {
            "source": f"/synthetic/task-{index:02d}",
            "target": f"/synthetic/task-{index + 1:02d}",
            "kind": "sequence",
        }
        for index in range(1, 5)
    ]
    latest_event_id = 2 if changed else 1
    version_character = "b" if changed else "a"
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": "succeeded",
        "coverage": "complete",
        "coverage_reason": None,
        "latest_event_id": latest_event_id,
        "snapshot_version": "sha256:" + version_character * 64,
        "truncated": False,
        "counts": {"nodes": len(nodes), "edges": len(edges), "executions": len(nodes)},
        "nodes": nodes,
        "edges": edges,
    }


def _workflow_events(
    workflow_id: str, run_id: str, *, after_id: int, changed: bool
) -> dict[str, object]:
    latest_event_id = 2 if changed else 1
    event_rows: list[dict[str, object]] = []
    for event_id in range(after_id + 1, latest_event_id + 1):
        if event_id == 1:
            event_rows.append(
                {
                    "event_id": 1,
                    "event_type": "task_completed",
                    "node_id": "/synthetic/task-01",
                    "task_identity": "synthetic-task-01",
                    "execution_id": "synthetic-execution-01",
                    "occurred_at": "2026-11-18T09:01:00Z",
                    "source": "interpreter",
                    "metadata": {},
                }
            )
        else:
            event_rows.append(
                {
                    "event_id": 2,
                    "event_type": "workflow_completed",
                    "node_id": None,
                    "task_identity": None,
                    "execution_id": None,
                    "occurred_at": "2026-11-18T09:01:01Z",
                    "source": "interpreter",
                    "metadata": {},
                }
            )
    next_after_id = cast(int, event_rows[-1]["event_id"]) if event_rows else after_id
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "events": event_rows,
        "next_after_id": next_after_id,
        "has_more": next_after_id < latest_event_id,
        "truncated": False,
        "coverage": "complete",
        "latest_event_id": latest_event_id,
    }


class FakeWorkflowClient:
    """In-memory, read-only stand-in for the workflow HTTP client."""

    def __init__(self) -> None:
        self.workflow_succeeded = _MemorySignal()
        self.workflow_failed = _MemorySignal()
        self.reads: list[SyntheticRead] = []
        self.aborted_keys: list[str] = []
        self.deferred_kinds: set[str] = set()
        self.pending: dict[str, tuple[int, object, str]] = {}
        self.run_ids: dict[str, str] = {}
        self.list_request_count = 0
        self.post_attempts = 0

    def _respond(self, kind: str, key: str, payload: object) -> bool:
        if kind in self.deferred_kinds:
            self.pending[key] = (200, payload, kind)
        else:
            self.workflow_succeeded.emit(key, 200, payload)
        return True

    def _current_run_id(self, workflow_id: str) -> str:
        return self.run_ids.get(workflow_id, _run_id(workflow_id))

    def get_control_workflows(self, token: str, *, key: str) -> bool:
        del token
        self.list_request_count += 1
        self.reads.append(SyntheticRead("list", key))
        return self._respond(
            "list",
            key,
            {"workflows": _workflow_rows(), "limit": 50, "truncated": False},
        )

    def get_control_workflow_detail(self, workflow_id: str, token: str, *, key: str) -> bool:
        del token
        self.reads.append(SyntheticRead("detail", key, workflow_id))
        payload = _workflow_detail(workflow_id)
        payload["run_id"] = self._current_run_id(workflow_id)
        return self._respond("detail", key, payload)

    def get_control_workflow_result(self, workflow_id: str, token: str, *, key: str) -> bool:
        del token
        self.reads.append(SyntheticRead("result", key, workflow_id))
        return self._respond("result", key, _workflow_result(workflow_id))

    def get_control_workflow_tasks(self, workflow_id: str, token: str, *, key: str) -> bool:
        del token
        self.reads.append(SyntheticRead("tasks", key, workflow_id))
        changed = self.list_request_count >= _EVENT_CHANGE_LIST_REQUEST
        return self._respond(
            "tasks",
            key,
            _graph_snapshot(workflow_id, self._current_run_id(workflow_id), changed=changed),
        )

    def get_control_workflow_task_events(
        self,
        workflow_id: str,
        token: str,
        after_id: int = 0,
        limit: int = 100,
        *,
        key: str,
    ) -> bool:
        del token
        if limit != 100:
            raise AssertionError(
                "The controller must request the synthetic event page size of 100."
            )
        self.reads.append(SyntheticRead("task_events", key, workflow_id, after_id))
        changed = self.list_request_count >= _EVENT_CHANGE_LIST_REQUEST
        payload = _workflow_events(
            workflow_id,
            self._current_run_id(workflow_id),
            after_id=after_id,
            changed=changed,
        )
        return self._respond("task_events", key, payload)

    def post_control_workflow(self, token: str, payload: object, *, key: str) -> bool:
        del token, payload, key
        self.post_attempts += 1
        raise AssertionError("Synthetic review must never call post_control_workflow.")

    def abort_workflow_request(self, key: str) -> None:
        self.aborted_keys.append(key)

    def pending_keys(self, kind: str) -> list[str]:
        return [
            key
            for key, (_status, _payload, pending_kind) in self.pending.items()
            if pending_kind == kind
        ]

    def succeed_pending(self, key: str) -> None:
        status, payload, _kind = self.pending.pop(key)
        self.workflow_succeeded.emit(key, status, payload)

    def emit_success(self, key: str, payload: object) -> None:
        self.workflow_succeeded.emit(key, 200, payload)

    def read_count(self, kind: str) -> int:
        return sum(read.kind == kind for read in self.reads)


class FakeWindow(QWidget):
    """Minimal fake readiness provider; it owns no Bridge or service object."""

    def __init__(self) -> None:
        super().__init__()
        self._control_token = "synthetic-only-token"

    def _can_display_control_token(self) -> bool:
        return self._control_token == "synthetic-only-token"


class SyntheticWorkflowManagementController(WorkflowManagementController):
    """Keep the real polling controller while permanently denying start requests."""

    def __init__(self, window: FakeWindow, client: FakeWorkflowClient) -> None:
        self.denied_start_signals = 0
        super().__init__(cast(Any, window), client, ())
        self.dialog.set_start_enabled(False)

    def _update_buttons(self) -> None:
        super()._update_buttons()
        self.dialog.set_start_enabled(False)

    def _start_new_workflow(self) -> None:
        self.denied_start_signals += 1
        self.dialog.set_message("合成レビュー: 新しい実行は無効です。")


class ControllerVisibleReviewSession:
    """One local synthetic Controller/Dialog pair for tests or manual review."""

    def __init__(self, size: str = "regular") -> None:
        if size not in _SIZES:
            raise ValueError(f"Unsupported review size: {size}")
        self.size_name = size
        self.window = FakeWindow()
        self.client = FakeWorkflowClient()
        self.controller = SyntheticWorkflowManagementController(self.window, self.client)
        self.dialog = self.controller.dialog
        self.dialog.setWindowTitle(TITLE)
        self.dialog.resize(*_SIZES[size])
        self.dialog.set_start_enabled(False)
        self._closed = False

    def show(self) -> None:
        self.controller.show()
        self.dialog.bridge_state_label.setText("合成クライアント: インメモリ")
        self.dialog.set_message("FakeWorkflowClient の合成データを表示しています。")
        self.dialog.start_button.setEnabled(False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.controller.shutdown()
        self.window.close()
        self.dialog.deleteLater()
        self.window.deleteLater()


def _validate_event_fixture(payload: Mapping[str, object]) -> bool:
    events = payload.get("events")
    if (
        payload.get("schema_version") != 1
        or not isinstance(payload.get("workflow_id"), str)
        or not isinstance(payload.get("run_id"), str)
        or not isinstance(events, list)
        or type(payload.get("next_after_id")) is not int
        or type(payload.get("has_more")) is not bool
        or type(payload.get("truncated")) is not bool
        or payload.get("coverage") != "complete"
        or type(payload.get("latest_event_id")) is not int
    ):
        return False
    return all(
        isinstance(event, Mapping)
        and type(event.get("event_id")) is int
        and isinstance(event.get("event_type"), str)
        and event.get("source") == "interpreter"
        and isinstance(event.get("occurred_at"), str)
        and isinstance(event.get("metadata"), Mapping)
        for event in events
    )


def _import_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def _static_safety_check() -> tuple[bool, list[str]]:
    repository_root = Path(__file__).resolve().parents[1]
    checked_paths = (
        Path(__file__).resolve(),
        repository_root / "src/codex_bridge/console/workflow_controller.py",
        repository_root / "src/codex_bridge/console/workflows_dialog.py",
        repository_root / "src/codex_bridge/console/workflow_graph_widget.py",
    )
    violations: list[str] = []
    for source_path in checked_paths:
        names = _import_names(source_path.read_text(encoding="utf-8"))
        forbidden = sorted(names & _FORBIDDEN_IMPORTS)
        if forbidden:
            violations.append(f"{source_path.name}: {', '.join(forbidden)}")
    return not violations, violations


def prepare() -> dict[str, object]:
    """Check only synthetic DTOs and static import boundaries; never creates a window."""
    app_before = QApplication.instance()
    rows = _workflow_rows()
    workflow_id = cast(str, rows[0]["workflow_id"])
    run_id = _run_id(workflow_id)
    snapshot = _graph_snapshot(workflow_id, run_id, changed=False)
    events = _workflow_events(workflow_id, run_id, after_id=0, changed=False)
    graph_valid = _validate_snapshot(workflow_id, snapshot) is not None
    events_valid = _validate_event_fixture(events)
    imports_valid, violations = _static_safety_check()
    app_after = QApplication.instance()
    status = (
        "PASS" if len(rows) == 50 and graph_valid and events_valid and imports_valid else "FAIL"
    )
    return {
        "status": status,
        "mode": "prepare",
        "synthetic_workflows": len(rows),
        "synthetic_graph_nodes": len(cast(list[object], snapshot["nodes"])),
        "synthetic_event_rows": len(cast(list[object], events["events"])),
        "synthetic_schema_valid": graph_valid and events_valid,
        "forbidden_imports": violations,
        "qapplication_created": app_before is None and app_after is not None,
        "windows_created": 0,
        "real_data_accesses": 0,
        "external_communications": 0,
    }


def _show_environment_reason(
    *, os_name: str | None = None, platform_name: str | None = None, qpa_platform: str | None = None
) -> str | None:
    active_os = os.name if os_name is None else os_name
    active_platform = sys.platform if platform_name is None else platform_name
    active_qpa = os.environ.get("QT_QPA_PLATFORM", "") if qpa_platform is None else qpa_platform
    if active_os != "nt" or active_platform != "win32":
        return "--show requires a native Windows session"
    if active_qpa.lower() == "offscreen":
        return "--show is unavailable when QT_QPA_PLATFORM=offscreen"
    return None


def _show(size: str) -> int:
    reason = _show_environment_reason()
    if reason is not None:
        print(f"BLOCKED: {reason}")
        return 2
    app = QApplication.instance() or QApplication([sys.argv[0]])
    previous_language = language()
    previous_locale = QLocale()
    initialize_language(cast(Any, _ProcessOnlySettings("ja-JP")), system_locale="ja_JP")
    session = ControllerVisibleReviewSession(size)
    app.aboutToQuit.connect(session.close)
    session.dialog.finished.connect(app.quit)
    try:
        session.show()
        return app.exec()
    finally:
        session.close()
        initialize_language(
            cast(Any, _ProcessOnlySettings(previous_language)),
            system_locale="en_US",
        )
        QLocale.setDefault(previous_locale)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated synthetic review using the real workflow Controller and Dialog."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare", action="store_true", help="check synthetic DTOs and safety boundaries"
    )
    mode.add_argument("--show", action="store_true", help="show the synthetic local review dialog")
    parser.add_argument("--size", choices=tuple(_SIZES), help="visible dialog size; show mode only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0
    options = parser.parse_args(arguments)
    if options.prepare:
        if options.size is not None:
            parser.error("--size is only valid with --show")
        result = prepare()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "PASS" else 1
    if not options.show:
        parser.error("choose --prepare or --show")
    reason = _show_environment_reason()
    if reason is not None:
        print(f"BLOCKED: {reason}")
        return 2
    return _show(options.size or "regular")


if __name__ == "__main__":
    raise SystemExit(main())
