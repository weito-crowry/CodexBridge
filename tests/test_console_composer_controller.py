from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from codex_bridge.console.composer_controller import ComposerController
from codex_bridge.console.composer_widget import ComposerWidget
from codex_bridge.console.widgets import ThreadListPane


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
        self.composer_json_succeeded = Signal()
        self.composer_json_failed = Signal()
        self.json_succeeded = Signal()
        self.json_failed = Signal()
        self.activity_received = Signal()
        self.get_calls: list[tuple[str, str, str]] = []
        self.get_json_calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.post_calls: list[tuple[str, dict[str, object], str, str]] = []

    def get_control_json(self, path: str, token: str, *, key: str) -> bool:
        self.get_calls.append((path, token, key))
        return True

    def post_control_json(
        self, path: str, payload: dict[str, object], token: str, *, key: str
    ) -> bool:
        self.post_calls.append((path, payload, token, key))
        return True

    def get_json(self, path: str, *, key: str, query: dict[str, object] | None = None) -> bool:
        self.get_json_calls.append((path, key, query))
        return True

    def succeed(self, key: str, payload: object) -> None:
        self.composer_json_succeeded.emit(key, payload)

    def fail(self, key: str, category: str) -> None:
        self.composer_json_failed.emit(key, category)


class FakeWindow:
    def __init__(self, roots: tuple[str, ...], client: FakeClient) -> None:
        self._config = SimpleNamespace(allowed_roots=roots)
        self._client = client
        self._selected_thread_id: str | None = None
        self._selection_generation = 0
        self._control_token = "fake-control-token"
        self._closing = False
        self.thread_pane = ThreadListPane()
        self.thread_pane.set_threads(
            [
                {"id": "thread-a", "name": "A", "cwd": roots[0]},
                {"id": "thread-b", "name": "B", "cwd": roots[0]},
            ]
        )
        self.thread_pane.thread_selected.connect(self.select_thread)
        self.refreshes = 0

    def select_thread(self, thread_id: str | None) -> None:
        self._selected_thread_id = thread_id
        self._selection_generation += 1

    def _request_threads(self) -> None:
        self.refreshes += 1
        self._client.get_json("/ui-api/threads", key="threads", query={"limit": 100})

    def _request_selected_status(self) -> None:
        if self._selected_thread_id:
            self._client.get_json(
                f"/ui-api/threads/{self._selected_thread_id}/status",
                key="composer-test-status",
            )


def _application() -> QApplication:
    application = QApplication.instance()
    return application if application is not None else QApplication([])


def _controller(tmp_path: Path, *, confirm: bool = True):
    root = str(tmp_path)
    client = FakeClient()
    window = FakeWindow((root,), client)
    widget = ComposerWidget((root,))
    controller = ComposerController(
        window,
        widget,
        client,
        allowed_roots=(root,),
        full_access_confirmation=lambda: confirm,
    )
    controller.set_control_available(True)
    return controller, window, widget, client, root


def test_new_thread_draft_is_local_and_first_send_uses_confirmed_options(tmp_path: Path) -> None:
    _application()
    controller, window, widget, client, root = _controller(tmp_path)
    client.succeed(
        client.get_calls[-1][2],
        {
            "models": [
                {
                    "model": "model-a",
                    "display_name": "Model A",
                    "reasoning_efforts": [{"id": "low"}, {"id": "high"}],
                    "default_reasoning_effort": "high",
                }
            ],
            "defaults": {"model": "model-a", "reasoning_effort": "high"},
        },
    )

    controller.open_new_thread_draft(root)
    widget.input.setPlainText("最初のメッセージ")
    assert client.post_calls == []
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.access_combo.setCurrentIndex(widget.access_combo.findData("danger-full-access"))

    controller.send()

    assert len(client.post_calls) == 1
    path, payload, token, _key = client.post_calls[0]
    assert path == "/ui-api/control/threads"
    assert token == "fake-control-token"
    assert payload["cwd"] == root
    assert payload["prompt"] == "最初のメッセージ"
    assert payload["model"] == "model-a"
    assert payload["reasoning_effort"] == "high"
    assert payload["sandbox_mode"] == "danger-full-access"
    assert payload["confirm_full_access"] is True
    assert window.refreshes == 0


def test_created_thread_is_selected_only_after_list_contains_it_and_draft_stays_selected(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, root = _controller(tmp_path)
    controller.open_new_thread_draft(root)
    widget.input.setPlainText("create once")
    assert controller.send()
    _path, _payload, _token, key = client.post_calls[-1]
    client.succeed(
        key,
        {"thread_id": "thread-new", "turn_id": "turn-new", "state": "in_progress"},
    )

    assert window._selected_thread_id is None
    assert widget.is_new_draft
    assert widget.prompt_text == ""
    assert window.refreshes == 1
    window.thread_pane.set_threads(
        [
            {"id": "thread-a", "name": "A", "cwd": root},
            {"id": "thread-b", "name": "B", "cwd": root},
        ]
    )
    client.json_succeeded.emit("threads", {"threads": []})
    QCoreApplication.processEvents()

    assert window._selected_thread_id is None
    assert widget.is_new_draft
    assert "一覧で確認できません" in widget.status_label.text()
    assert window.refreshes == 1

    window.thread_pane.set_threads(
        [
            {"id": "thread-a", "name": "A", "cwd": root},
            {"id": "thread-b", "name": "B", "cwd": root},
            {"id": "thread-new", "name": "Created", "cwd": root},
        ]
    )
    client.json_succeeded.emit(
        "threads",
        {"threads": [{"id": "thread-new", "name": "Created", "cwd": root}]},
    )
    QCoreApplication.processEvents()

    assert window._selected_thread_id == "thread-new"
    assert not widget.is_new_draft
    assert widget.prompt_text == ""


def test_existing_send_is_pinned_and_delayed_success_does_not_clear_other_draft(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("send to A")

    controller.send()

    assert len(client.post_calls) == 1
    path, payload, _token, key = client.post_calls[0]
    assert path == "/ui-api/control/threads/thread-a/turns"
    assert payload == {
        "client_request_id": payload["client_request_id"],
        "prompt": "send to A",
    }
    assert "model" not in payload
    assert "reasoning_effort" not in payload
    assert "sandbox_mode" not in payload

    window.thread_pane.thread_selected.emit("thread-b")
    widget.input.setPlainText("draft for B")
    client.succeed(
        key,
        {"thread_id": "thread-a", "turn_id": "turn-a", "state": "in_progress"},
    )

    assert widget.prompt_text == "draft for B"
    window.thread_pane.thread_selected.emit("thread-a")
    assert widget.prompt_text == ""


def test_send_button_tracks_prompt_whitespace_and_length_validation(tmp_path: Path) -> None:
    _application()
    _controller_instance, window, widget, _client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")

    assert not widget.send_button.isEnabled()
    widget.input.setPlainText("   ")
    assert not widget.send_button.isEnabled()
    widget.input.setPlainText("  send this  ")
    assert widget.send_button.isEnabled()
    widget.input.setPlainText("x" * 32_001)
    assert not widget.send_button.isEnabled()


def test_unavailable_bridge_disables_send_and_unknown_result_keeps_prompt(tmp_path: Path) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText("preserve this prompt")
    controller.set_control_available(False)
    assert not widget.send_button.isEnabled()
    assert not widget.stop_button.isEnabled()

    controller.set_control_available(True)
    controller.send()
    _path, _payload, _token, key = client.post_calls[-1]
    client.fail(key, "http_409")

    assert widget.prompt_text == "preserve this prompt"
    assert "結果" in widget.status_label.text()
    assert not widget.send_button.isEnabled()
    assert len(client.post_calls) == 1
    assert (
        controller._unknown_sends["thread:thread-a"].request_id
        == client.post_calls[0][1]["client_request_id"]
    )


def test_full_access_requires_confirmation_and_model_catalog_failure_has_no_fake_models(
    tmp_path: Path,
) -> None:
    _application()
    controller, _window, widget, client, root = _controller(tmp_path, confirm=False)
    client.succeed(client.get_calls[-1][2], {"models": [], "defaults": {}})
    controller.open_new_thread_draft(root)
    widget.input.setPlainText("keep this draft")
    widget.access_combo.setCurrentIndex(widget.access_combo.findData("danger-full-access"))

    assert widget.model_combo.count() == 1
    assert widget.model_combo.itemData(0) is None
    assert not controller.send()
    assert client.post_calls == []
    assert widget.prompt_text == "keep this draft"


def test_stop_uses_status_confirmed_thread_turn_and_waits_for_status_after_success(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    status_key = f"selection:{window._selection_generation}:status"
    client.json_succeeded.emit(
        status_key,
        {
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "state": "in_progress",
            "pending_request": None,
            "thread_metadata": {
                "model": "actual-model",
                "reasoning_effort": "high",
                "sandbox_mode": "danger-full-access",
            },
        },
    )

    assert not widget.stop_button.isHidden()
    assert widget.stop_button.isEnabled()
    model_labels = [widget.model_combo.itemText(i) for i in range(widget.model_combo.count())]
    assert "actual-model" not in model_labels
    assert not widget.send_button.isEnabled()
    assert controller.stop()
    path, payload, _token, key = client.post_calls[-1]
    assert path.endswith("/threads/thread-a/turns/turn-a/interrupt")
    assert payload["client_request_id"]
    client.succeed(
        key,
        {"thread_id": "thread-a", "turn_id": "turn-a", "state": "interrupted"},
    )

    assert not widget.stop_button.isHidden()
    assert not widget.stop_button.isEnabled()
    assert "状態を確認" in widget.status_label.text()
    assert any("thread-a/status" in call[0] for call in client.get_json_calls)

    client.json_succeeded.emit(
        f"selection:{window._selection_generation}:status",
        {
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "state": "in_progress",
            "pending_request": None,
            "thread_metadata": {},
        },
    )
    assert not widget.stop_button.isEnabled()

    status_key = next(
        key
        for _path, key, _query in client.get_json_calls
        if key.startswith("composer:refresh-status:")
    )
    client.json_succeeded.emit(
        status_key,
        {
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "state": "in_progress",
            "pending_request": None,
            "thread_metadata": {},
        },
    )
    assert widget.stop_button.isEnabled()


def test_stop_target_is_cleared_when_selection_changes_and_pending_requests_go_to_activity(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    status_key = f"selection:{window._selection_generation}:status"
    client.json_succeeded.emit(
        status_key,
        {
            "thread_id": "thread-a",
            "turn_id": "turn-a",
            "state": "in_progress",
            "pending_request": None,
            "thread_metadata": {},
        },
    )
    window.thread_pane.thread_selected.emit("thread-b")
    assert widget.stop_button.isHidden()
    assert not controller.stop()

    status_key = f"selection:{window._selection_generation}:status"
    client.json_succeeded.emit(
        status_key,
        {
            "thread_id": "thread-b",
            "turn_id": "turn-b",
            "state": "needs_input",
            "pending_request": {"turn_id": "turn-b"},
            "thread_metadata": {},
        },
    )
    assert "Activity" in widget.status_label.text()
    assert not widget.stop_button.isEnabled()


def test_prompt_validation_rejects_whitespace_and_more_than_32000_characters(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    widget.input.setPlainText(" \t\n")
    assert not widget.send_button.isEnabled()
    assert not controller.send()
    widget.input.setPlainText("x" * 32_001)
    assert not widget.send_button.isEnabled()
    assert not controller.send()
    assert client.post_calls == []
