from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QTreeWidgetItem,
)

from codex_bridge.console.codex_resolver import CodexResolution
from codex_bridge.console.codex_updates import CodexUpdateInfo
from codex_bridge.console.config import ConsoleConfig
from codex_bridge.console.main_window import MainWindow
from codex_bridge.console.runtime_launcher import DetachedLaunchResult
from codex_bridge.console.tunnel_supervisor import TunnelActionState


class Signal:
    def __init__(self) -> None:
        self._slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self._slots.append(slot)

    def emit(self, *args: Any) -> None:
        for slot in tuple(self._slots):
            slot(*args)


class FakeClient:
    def __init__(self) -> None:
        self.json_succeeded = Signal()
        self.json_failed = Signal()
        self.activity_received = Signal()
        self.stream_state_changed = Signal()
        self.global_activity_received = Signal()
        self.global_stream_state_changed = Signal()
        self.control_succeeded = Signal()
        self.control_failed = Signal()
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.streams: list[tuple[str, int]] = []
        self.global_streams: list[int] = []
        self.aborted_groups: list[str] = []
        self.stopped_streams = 0
        self.aborted_all = False
        self.control_requests: list[tuple[str, str]] = []
        self.approval_requests: list[tuple[str, object, str, str]] = []
        self.json_posts: list[tuple[str, str, dict[str, object]]] = []

    def get_json(self, path: str, *, key: str, query: dict[str, object] | None = None) -> bool:
        self.requests.append((key, path, query))
        return True

    def post_json(self, path: str, payload: dict[str, object], *, key: str) -> bool:
        self.json_posts.append((key, path, payload))
        return True

    def abort_json_group(self, prefix: str) -> None:
        self.aborted_groups.append(prefix)

    def start_stream(self, thread_id: str, generation: int) -> None:
        self.streams.append((thread_id, generation))

    def stop_stream(self) -> None:
        self.stopped_streams += 1

    def start_global_stream(self, generation: int) -> None:
        self.global_streams.append(generation)

    def stop_global_stream(self) -> None:
        pass

    def abort_all(self) -> None:
        self.aborted_all = True

    def post_control_shutdown(self, token: str, *, key: str) -> bool:
        self.control_requests.append((token, key))
        return True

    def post_control_approval(
        self, token: str, *, request_id: object, decision: str, key: str
    ) -> bool:
        self.approval_requests.append((token, request_id, decision, key))
        return True

    def result(self, key: str, payload: object) -> None:
        self.json_succeeded.emit(key, payload)

    def failure(self, key: str, message: str) -> None:
        self.json_failed.emit(key, message)

    def control_success(self, key: str) -> None:
        self.control_succeeded.emit(key)

    def control_failure(self, key: str, message: str = "Bridge control request failed") -> None:
        self.control_failed.emit(key, message)

    def activity(self, generation: int, payload: dict[str, object]) -> None:
        self.activity_received.emit(generation, payload)

    def global_activity(self, generation: int, payload: dict[str, object]) -> None:
        self.global_activity_received.emit(generation, payload)


class FakeDiagnosticsReader:
    def __init__(self, batches: list[list[str]] | None = None) -> None:
        self.batches = list(batches or [])
        self.polls = 0
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def poll(self) -> list[str]:
        self.polls += 1
        return self.batches.pop(0) if self.batches else []


class FakeCodexProbe:
    def __init__(self) -> None:
        self.resolved = Signal()
        self.failed = Signal()
        self.starts = 0
        self.aborts = 0

    def start(self) -> bool:
        self.starts += 1
        return True

    def abort(self) -> None:
        self.aborts += 1

    def result(self, resolution: CodexResolution) -> None:
        self.resolved.emit(resolution)

    def failure(self, message: str = "Codex version could not be verified") -> None:
        self.failed.emit(message)


class FakeCodexUpdateProbe:
    def __init__(self) -> None:
        self.check_succeeded = Signal()
        self.check_failed = Signal()
        self.update_succeeded = Signal()
        self.update_failed = Signal()
        self.busy_changed = Signal()
        self.check_calls: list[CodexResolution] = []
        self.update_calls: list[CodexResolution] = []
        self.busy = False

    def check_for_updates(self, resolution: CodexResolution) -> bool:
        self.check_calls.append(resolution)
        self.busy = True
        self.busy_changed.emit(True)
        return True

    def update(self, resolution: CodexResolution) -> bool:
        self.update_calls.append(resolution)
        self.busy = True
        self.busy_changed.emit(True)
        return True

    def abort(self) -> None:
        if self.busy:
            self.busy = False
            self.busy_changed.emit(False)

    def result(self, info: CodexUpdateInfo) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.check_succeeded.emit(info)

    def failure(self, message: str = "Codex update check failed") -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.check_failed.emit(message)

    def update_success(self) -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.update_succeeded.emit()

    def update_failure(self, message: str = "Codex update failed") -> None:
        self.busy = False
        self.busy_changed.emit(False)
        self.update_failed.emit(message)


class FakeLauncher:
    def __init__(self, *, started: bool = True) -> None:
        self.started = started
        self.calls: list[tuple[str, int]] = []
        self.control_tokens: list[str] = []
        self.closes = 0

    def launch(
        self,
        *,
        codex_executable: str,
        ui_port: int,
        control_token: str,
        allowed_roots: tuple[str, ...] = (),
    ) -> DetachedLaunchResult:
        del allowed_roots
        self.calls.append((codex_executable, ui_port))
        self.control_tokens.append(control_token)
        return DetachedLaunchResult(self.started, 1234 if self.started else None)

    def close(self) -> None:
        self.closes += 1


class StableTunnel:
    def __init__(self) -> None:
        self.state_changed = Signal()
        self.message_changed = Signal()
        self.controls_changed = Signal()
        self.state = "unavailable"
        self.action_state = TunnelActionState(False, False, False)
        self.started = 0

    def set_bridge_ready(self, _ready: bool) -> None:
        pass

    def start(self) -> bool:
        self.started += 1
        return True

    def stop(self, *, on_finished=None) -> bool:
        if on_finished is not None:
            on_finished()
        return False

    def close(self, *, on_finished=None) -> None:
        if on_finished is not None:
            on_finished()


class ManagedTunnel(StableTunnel):
    def __init__(self) -> None:
        super().__init__()
        self.state = "ready"
        self.action_state = TunnelActionState(False, True, True)
        self.stop_calls = 0

    def stop(self, *, on_finished=None) -> bool:
        self.stop_calls += 1
        self.state = "stopped"
        self.action_state = TunnelActionState(True, False, False)
        if on_finished is not None:
            on_finished()
        return True


class LifecycleTunnel(StableTunnel):
    def __init__(self, state: str = "unavailable") -> None:
        super().__init__()
        self.state = state
        self.retry_calls = 0

    def retry_now(self) -> bool:
        self.retry_calls += 1
        return True

    def emit_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)


class RecoveringLifecycleTunnel(LifecycleTunnel):
    def __init__(self, state: str = "failed") -> None:
        super().__init__(state)
        self.recovery_timer = QTimer()
        self.recovery_timer.setSingleShot(True)


def _application() -> QApplication:
    application = QCoreApplication.instance()
    return application if isinstance(application, QApplication) else QApplication([])


def _config() -> ConsoleConfig:
    return ConsoleConfig(allowed_roots=(str(Path.cwd()),))


def _thread_item(window: MainWindow, thread_id: str) -> QTreeWidgetItem:
    tree = window.thread_pane.list_widget
    return next(
        tree.topLevelItem(group_index).child(child_index)
        for group_index in range(tree.topLevelItemCount())
        for child_index in range(tree.topLevelItem(group_index).childCount())
        if tree.topLevelItem(group_index).child(child_index).data(0, Qt.ItemDataRole.UserRole)
        == thread_id
    )


def _usage_window(client: FakeClient, usage_history_path: Path | None = None) -> MainWindow:
    return MainWindow(
        _config(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
        usage_history_path=usage_history_path,
    )


def _set_usage_ready(window: MainWindow) -> None:
    window._bridge_ready = True
    window._app_server_ready = True


def _item(item_id: str, item_type: str, **fields: object) -> dict[str, object]:
    return {"id": item_id, "type": item_type, **fields}


def _activity(activity_id: str, thread_id: str = "thread-b") -> dict[str, object]:
    return {
        "activity_id": activity_id,
        "timestamp": "2026-08-28T00:00:00Z",
        "thread_id": thread_id,
        "turn_id": "turn-1",
        "type": "error",
        "status": "failed",
        "summary": activity_id,
        "details": {},
    }


def _wait_for_qt_timer(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def _history_activity(activity_type: str, activity_id: str, thread_id: str) -> dict[str, object]:
    return {
        **_activity(activity_id, thread_id),
        "type": activity_type,
        "status": "in_progress",
    }


def _complete_history_snapshot(client: FakeClient, generation: int) -> None:
    client.result(f"selection:{generation}:turns", {"turns": []})
    client.result(f"selection:{generation}:items", {"items": []})


def _history_items(count: int) -> dict[str, object]:
    return {
        "items": [
            {
                "turn_id": "turn-a",
                "item": _item(
                    f"item-{index}",
                    "agentMessage",
                    text="\n".join(f"message {index} line {line}" for line in range(8)),
                ),
            }
            for index in reversed(range(count))
        ]
    }
