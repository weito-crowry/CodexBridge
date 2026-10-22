from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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


def _controller(
    tmp_path: Path, *, confirm: bool = True, allowed_roots: tuple[str, ...] | None = None
):
    roots = allowed_roots or (str(tmp_path),)
    client = FakeClient()
    window = FakeWindow(roots, client)
    widget = ComposerWidget(roots)
    controller = ComposerController(
        window,
        widget,
        client,
        allowed_roots=roots,
        full_access_confirmation=lambda: confirm,
    )
    controller.set_control_available(True)
    return controller, window, widget, client, roots[0]


def test_unloaded_history_metadata_is_readonly_and_selection_generation_bound(
    tmp_path: Path,
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    baseline_requests = list(client.get_json_calls)
    client.json_succeeded.emit(
        "selection:1:status",
        {"thread_id": "thread-a", "state": "not_loaded", "thread_metadata": None},
    )

    controller.apply_history_model_metadata(
        "thread-a",
        1,
        ["turn-a"],
        {
            "turn-a": {
                "model_candidates": [{"model": "history-model", "reasoning_effort": "high"}],
                "model_resolution_status": "resolved",
            }
        },
        turn_access_metadata={
            "turn-a": {
                "access_candidates": ["danger-full-access"],
                "access_resolution_status": "resolved",
            }
        },
    )

    assert widget.model_combo.itemText(0) == "Model: history-model"
    assert widget.effort_combo.itemText(0) == "Effort: high"
    assert widget.access_combo.currentText() == "Access: Full Access"
    assert client.get_json_calls == baseline_requests
    assert client.post_calls == []

    window.thread_pane.thread_selected.emit("thread-b")
    controller.apply_history_model_metadata(
        "thread-a",
        1,
        ["turn-a"],
        {},
        turn_access_metadata={
            "turn-a": {
                "access_candidates": ["read-only"],
                "access_resolution_status": "resolved",
            }
        },
    )
    client.json_succeeded.emit(
        "selection:1:status",
        {
            "thread_id": "thread-a",
            "state": "completed",
            "thread_metadata": {"model": "late-model-a"},
        },
    )
    assert widget.model_combo.itemText(0) == "Model: Unknown"
    assert widget.access_combo.itemText(0) == "Access: Unknown"


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
                    "default_reasoning_effort": "low",
                }
            ],
            "defaults": {"model": "model-a", "reasoning_effort": "low"},
        },
    )

    controller.open_new_thread_draft(root)
    widget.input.setPlainText("最初のメッセージ")
    assert client.post_calls == []
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData("high"))
    assert widget.selected_effort == "high"
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


@pytest.mark.parametrize(
    ("selected_effort", "expected_effort"),
    [("low", "low"), (None, None)],
)
def test_selected_effort_is_used_in_new_thread_post_payload(
    tmp_path: Path, selected_effort: str | None, expected_effort: str | None
) -> None:
    _application()
    controller, _window, widget, client, root = _controller(tmp_path)
    client.succeed(
        client.get_calls[-1][2],
        {
            "models": [
                {
                    "model": "model-a",
                    "reasoning_efforts": [{"id": "low"}, {"id": "high"}],
                    "default_reasoning_effort": "high",
                }
            ],
            "defaults": {},
        },
    )
    controller.open_new_thread_draft(root)
    widget.input.setPlainText("new conversation")
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData(selected_effort))

    assert widget.selected_effort == expected_effort
    assert controller.send()

    payload = client.post_calls[-1][1]
    if expected_effort is None:
        assert "reasoning_effort" not in payload
    else:
        assert payload["reasoning_effort"] == expected_effort


def test_effort_selection_is_kept_per_new_thread_draft(tmp_path: Path) -> None:
    _application()
    root_a = str(tmp_path / "a")
    root_b = str(tmp_path / "b")
    Path(root_a).mkdir()
    Path(root_b).mkdir()
    controller, _window, widget, client, _root = _controller(
        tmp_path, allowed_roots=(root_a, root_b)
    )
    client.succeed(
        client.get_calls[-1][2],
        {
            "models": [
                {
                    "model": "model-a",
                    "reasoning_efforts": [{"id": "low"}, {"id": "high"}],
                    "default_reasoning_effort": "low",
                }
            ],
            "defaults": {},
        },
    )
    controller.open_new_thread_draft(root_a)
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData("high"))
    widget.input.setPlainText("draft A")

    widget.cwd_combo.setCurrentIndex(widget.cwd_combo.findData(root_b))
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData("low"))
    widget.input.setPlainText("draft B")

    widget.cwd_combo.setCurrentIndex(widget.cwd_combo.findData(root_a))
    assert widget.selected_effort == "high"
    assert widget.prompt_text == "draft A"
    assert controller.send()
    assert client.post_calls[-1][1]["reasoning_effort"] == "high"


def test_model_change_adjusts_effort_to_supported_option_before_post(tmp_path: Path) -> None:
    _application()
    controller, _window, widget, client, root = _controller(tmp_path)
    client.succeed(
        client.get_calls[-1][2],
        {
            "models": [
                {
                    "model": "model-a",
                    "reasoning_efforts": [{"id": "low"}, {"id": "high"}],
                    "default_reasoning_effort": "low",
                },
                {
                    "model": "model-b",
                    "reasoning_efforts": [{"id": "low"}],
                    "default_reasoning_effort": "low",
                },
            ],
            "defaults": {},
        },
    )
    controller.open_new_thread_draft(root)
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-a"))
    widget.effort_combo.setCurrentIndex(widget.effort_combo.findData("high"))
    widget.model_combo.setCurrentIndex(widget.model_combo.findData("model-b"))
    assert widget.selected_effort == "low"
    widget.input.setPlainText("after model change")

    assert controller.send()
    payload = client.post_calls[-1][1]
    assert payload["model"] == "model-b"
    assert payload["reasoning_effort"] == "low"


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
    assert not widget.model_combo.isEnabled()
    assert not widget.effort_combo.isEnabled()
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


@pytest.mark.parametrize(
    ("activity_type", "status"),
    [
        ("turn_completed", "completed"),
        ("turn_failed", "failed"),
        ("turn_interrupted", "interrupted"),
    ],
)
def test_late_turn_a_completion_does_not_replace_running_turn_b(
    tmp_path: Path, activity_type: str, status: str
) -> None:
    _application()
    controller, window, widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    generation = window._selection_generation

    client.activity_received.emit(
        generation,
        {"thread_id": "thread-a", "type": "turn_started", "turn_id": "turn-a"},
    )
    client.activity_received.emit(
        generation,
        {
            "thread_id": "thread-a",
            "type": "turn_completed",
            "turn_id": "turn-a",
            "status": "completed",
        },
    )
    client.activity_received.emit(
        generation,
        {"thread_id": "thread-a", "type": "turn_started", "turn_id": "turn-b"},
    )

    client.activity_received.emit(
        generation,
        {
            "thread_id": "thread-a",
            "type": activity_type,
            "turn_id": "turn-a",
            "status": status,
        },
    )

    assert controller._current_state == ("in_progress", "turn-b", False)
    assert controller._stop_target == ("thread-a", "turn-b")
    assert not widget.stop_button.isHidden()
    assert widget.stop_button.isEnabled()

    client.activity_received.emit(
        generation,
        {
            "thread_id": "thread-a",
            "type": "turn_completed",
            "turn_id": "turn-b",
            "status": "completed",
        },
    )

    assert controller._current_state == ("completed", "turn-b", False)
    assert controller._stop_target is None
    assert widget.stop_button.isHidden()
    assert "終了を確認" in widget.status_label.text()


@pytest.mark.parametrize(
    ("activity_type", "status"),
    [
        ("turn_completed", "completed"),
        ("turn_failed", "failed"),
        ("turn_interrupted", "interrupted"),
    ],
)
def test_turn_a_completion_preserves_unresolved_turn_b_record(
    tmp_path: Path, activity_type: str, status: str
) -> None:
    _application()
    controller, window, _widget, client, _root = _controller(tmp_path)
    window.thread_pane.thread_selected.emit("thread-a")
    generation = window._selection_generation
    client.activity_received.emit(
        generation,
        {"thread_id": "thread-a", "type": "turn_started", "turn_id": "turn-a"},
    )
    controller._awaiting_turns["thread-a"] = "turn-b"

    client.activity_received.emit(
        generation,
        {
            "thread_id": "thread-a",
            "type": activity_type,
            "turn_id": "turn-a",
            "status": status,
        },
    )

    assert controller._current_state == (status, "turn-a", False)
    assert controller._awaiting_turns["thread-a"] == "turn-b"


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
