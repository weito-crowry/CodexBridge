from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QMessageBox, QTreeWidget, QTreeWidgetItem

from .composer_widget import ComposerWidget

if TYPE_CHECKING:
    from .api_client import ApiClient
    from .main_window import MainWindow

_ACTIVE_STATES = frozenset({"in_progress", "needs_approval", "needs_input"})
_TERMINAL_STATES = frozenset({"completed", "interrupted", "failed"})
_MAX_PROMPT_CHARS = 32_000


@dataclass(frozen=True, slots=True)
class DraftOptions:
    model: str | None = None
    effort: str | None = None
    access: str = "inherit"


@dataclass(frozen=True, slots=True)
class SendContext:
    key: str
    request_id: str
    draft_key: str
    prompt: str
    thread_id: str | None
    cwd: str | None
    model: str | None
    effort: str | None
    access: str


@dataclass(frozen=True, slots=True)
class StopContext:
    key: str
    request_id: str
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class StatusRequestContext:
    thread_id: str
    selection_generation: int | None
    confirms_stop: bool


class ComposerController(QObject):
    """Owns per-thread drafts and the authenticated Composer operations."""

    def __init__(
        self,
        window: MainWindow,
        widget: ComposerWidget,
        api_client: ApiClient,
        *,
        allowed_roots: Sequence[str] = (),
        full_access_confirmation: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(widget)
        self._window = window
        self._widget = widget
        self._client = api_client
        self._allowed_roots = tuple(allowed_roots)
        self._confirm_full_access = full_access_confirmation or self._ask_full_access
        self._drafts: dict[str, str] = {"new:unselected": ""}
        self._draft_options: dict[str, DraftOptions] = {}
        self._draft_cwds: dict[str, str | None] = {}
        self._thread_metadata: dict[str, tuple[str | None, str | None, str | None]] = {}
        self._thread_states: dict[str, tuple[str | None, str | None, bool]] = {}
        self._unknown_sends: dict[str, SendContext] = {}
        self._awaiting_turns: dict[str, str] = {}
        self._current_draft_key = "new:unselected"
        self._current_thread_id: str | None = None
        self._current_cwd: str | None = None
        self._is_new_draft = False
        self._control_available = False
        self._capabilities_key: str | None = None
        self._capabilities_sequence = 0
        self._capabilities_available = False
        self._models: dict[str, tuple[str, tuple[str, ...], str | None]] = {}
        self._default_model: str | None = None
        self._default_effort: str | None = None
        self._send_contexts: dict[str, SendContext] = {}
        self._active_send_key: str | None = None
        self._stop_contexts: dict[str, StopContext] = {}
        self._active_stop_key: str | None = None
        self._status_requests: dict[str, StatusRequestContext] = {}
        self._status_request_sequence = 0
        self._stop_waiting_for_status: tuple[str, str] | None = None
        self._stop_target: tuple[str, str] | None = None
        self._current_state: tuple[str | None, str | None, bool] | None = None
        self._pending_create_selection: tuple[str, str] | None = None
        self._pending_create_notice_shown = False
        self._status_messages: dict[str, tuple[str, bool]] = {}

        widget.input.textChanged.connect(self._on_prompt_changed)
        widget.send_requested.connect(self.send)
        widget.stop_requested.connect(self.stop)
        widget.new_thread_requested.connect(self._start_new_thread_from_selection)
        widget.cwd_changed.connect(self._change_cwd)
        widget.model_changed.connect(self._change_model)
        widget.access_changed.connect(self._change_access)
        window.thread_pane.thread_selected.connect(self.select_thread)
        self._connect_api_signals()
        self._render()
        self._update_controls()

    @property
    def control_available(self) -> bool:
        return self._control_available

    def _connect_api_signals(self) -> None:
        for name, slot in (
            ("composer_json_succeeded", self._on_composer_success),
            ("composer_json_failed", self._on_composer_failure),
            ("json_succeeded", self._on_json_success),
            ("json_failed", self._on_json_failure),
            ("activity_received", self._on_activity),
        ):
            signal = getattr(self._client, name, None)
            if signal is not None:
                signal.connect(slot)

    def set_control_available(self, available: bool) -> None:
        can_call_control = callable(getattr(self._client, "get_control_json", None)) and callable(
            getattr(self._client, "post_control_json", None)
        )
        token = getattr(self._window, "_control_token", None)
        available = bool(available and can_call_control and isinstance(token, str) and token)
        if available == self._control_available:
            self._update_controls()
            return
        self._control_available = available
        self._stop_target = None
        self._stop_waiting_for_status = None
        if available:
            self._request_capabilities()
        else:
            self._capabilities_available = False
            self._capabilities_key = None
            self._models.clear()
        self._render()
        self._update_controls()

    def sync_bridge_ownership(self) -> None:
        self.set_control_available(self._window._bridge_control_allowed())

    def select_thread(self, thread_id: str | None) -> None:
        self._save_visible_draft()
        self._pending_create_selection = None
        self._pending_create_notice_shown = False
        self._current_thread_id = thread_id
        self._current_cwd = None
        self._is_new_draft = False
        self._current_draft_key = f"thread:{thread_id}" if thread_id else "new:unselected"
        self._drafts.setdefault(self._current_draft_key, "")
        self._current_state = None
        self._stop_target = None
        self._stop_waiting_for_status = None
        if thread_id is not None:
            self._thread_states.pop(thread_id, None)
        self._render()
        self._update_controls()

    def open_new_thread_draft(self, cwd: str | None) -> None:
        """Open a local draft; no API request is made until the first Send."""
        self._save_visible_draft()
        self._pending_create_selection = None
        self._pending_create_notice_shown = False
        self._current_thread_id = None
        self._is_new_draft = True
        candidate = cwd if isinstance(cwd, str) and cwd.strip() else None
        self._current_cwd = candidate
        self._current_draft_key = self._new_draft_key(candidate)
        self._drafts.setdefault(self._current_draft_key, "")
        self._draft_options.setdefault(self._current_draft_key, DraftOptions())
        self._draft_cwds[self._current_draft_key] = candidate
        self._current_state = None
        self._stop_target = None
        self._render()
        self._update_controls()

    def _start_new_thread_from_selection(self) -> None:
        if self._is_new_draft:
            self.open_new_thread_draft(self._current_cwd)
            return
        cwd: str | None = None
        thread_id = getattr(self._window, "_selected_thread_id", None)
        pane = self._window.thread_pane
        thread = pane._thread_for_id(thread_id) if isinstance(thread_id, str) else None
        raw_cwd = thread.get("cwd") if isinstance(thread, Mapping) else None
        if isinstance(raw_cwd, str) and os.path.isabs(raw_cwd) and os.path.isdir(raw_cwd):
            cwd = raw_cwd
        self.open_new_thread_draft(cwd)

    @staticmethod
    def _new_draft_key(cwd: str | None) -> str:
        if cwd is None or not cwd.strip():
            return "new:unselected"
        canonical = os.path.normcase(os.path.abspath(os.path.normpath(cwd)))
        return f"new:{canonical}"

    def _save_visible_draft(self) -> None:
        if hasattr(self, "_current_draft_key"):
            self._drafts[self._current_draft_key] = self._widget.prompt_text

    def _on_prompt_changed(self) -> None:
        self._save_visible_draft()
        self._update_controls()

    def _change_cwd(self, value: str) -> None:
        if not self._is_new_draft:
            return
        self._save_visible_draft()
        self._current_cwd = value if value.strip() else None
        self._current_draft_key = self._new_draft_key(self._current_cwd)
        self._drafts.setdefault(self._current_draft_key, "")
        self._draft_options.setdefault(self._current_draft_key, DraftOptions())
        self._draft_cwds[self._current_draft_key] = self._current_cwd
        self._render()
        self._update_controls()

    def _change_model(self) -> None:
        if not self._is_new_draft:
            return
        options = self._draft_options.get(self._current_draft_key, DraftOptions())
        model = self._widget.selected_model
        effort = options.effort
        if model is None:
            effort = None
        elif model in self._models:
            _label, supported, default_effort = self._models[model]
            if effort not in supported:
                effort = default_effort if default_effort in supported else None
                if effort is None and supported:
                    effort = supported[0]
        self._draft_options[self._current_draft_key] = DraftOptions(
            model=model, effort=effort, access=options.access
        )
        self._render()
        self._update_controls()

    def _change_access(self) -> None:
        if not self._is_new_draft:
            return
        options = self._draft_options.get(self._current_draft_key, DraftOptions())
        self._draft_options[self._current_draft_key] = DraftOptions(
            model=options.model,
            effort=options.effort,
            access=self._widget.selected_access,
        )

    def _request_capabilities(self) -> None:
        token = getattr(self._window, "_control_token", None)
        getter = getattr(self._client, "get_control_json", None)
        if not isinstance(token, str) or not token or not callable(getter):
            return
        self._capabilities_sequence += 1
        key = f"composer:capabilities:{self._capabilities_sequence}"
        self._capabilities_key = key
        if not getter("/ui-api/control/capabilities", token, key=key):
            self._capabilities_key = None
            self._capabilities_available = False
            self._render()

    def _apply_capabilities(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            self._capabilities_available = False
            self._models.clear()
            self._render()
            self._update_controls()
            return
        raw_models = payload.get("models")
        raw_defaults = payload.get("defaults")
        models: dict[str, tuple[str, tuple[str, ...], str | None]] = {}
        if isinstance(raw_models, list):
            for raw_model in raw_models:
                if not isinstance(raw_model, Mapping):
                    continue
                model = raw_model.get("model")
                if not isinstance(model, str) or not model.strip():
                    continue
                display = raw_model.get("display_name")
                label = display if isinstance(display, str) and display.strip() else model
                raw_efforts = raw_model.get("reasoning_efforts")
                efforts = (
                    tuple(
                        dict.fromkeys(
                            entry["id"]
                            for entry in raw_efforts
                            if isinstance(entry, Mapping)
                            and isinstance(entry.get("id"), str)
                            and entry["id"].strip()
                        )
                    )
                    if isinstance(raw_efforts, list)
                    else ()
                )
                raw_default_effort = raw_model.get("default_reasoning_effort")
                default_effort = (
                    raw_default_effort
                    if isinstance(raw_default_effort, str) and raw_default_effort in efforts
                    else None
                )
                models[model] = (label, efforts, default_effort)
        defaults = raw_defaults if isinstance(raw_defaults, Mapping) else {}
        default_model = defaults.get("model")
        self._default_model = default_model if isinstance(default_model, str) else None
        default_effort = defaults.get("reasoning_effort")
        self._default_effort = default_effort if isinstance(default_effort, str) else None
        self._models = models
        self._capabilities_available = bool(models)
        for key, options in tuple(self._draft_options.items()):
            if options.model not in models:
                self._draft_options[key] = DraftOptions(access=options.access)
                continue
            supported = models[options.model][1]
            effort = options.effort
            if effort is not None and effort not in supported:
                self._draft_options[key] = DraftOptions(model=options.model, access=options.access)
        self._render()
        self._update_controls()

    def _render(self) -> None:
        self._widget.set_draft_mode(self._is_new_draft)
        self._widget.set_prompt_text(self._drafts.get(self._current_draft_key, ""))
        if self._is_new_draft:
            options = self._draft_options.setdefault(self._current_draft_key, DraftOptions())
            self._widget.set_target(self._new_target_text(self._current_cwd))
            self._widget.set_cwd_options(self._allowed_roots, self._current_cwd)
            model_options = [
                (f"{label} ({model})" if label != model else model, model)
                for model, (label, _efforts, _default) in self._models.items()
            ]
            self._widget.set_model_options(
                model_options, options.model, available=self._capabilities_available
            )
            efforts: list[tuple[str, str]] = []
            if options.model in self._models:
                efforts = [(effort, effort) for effort in self._models[options.model][1]]
            self._widget.set_effort_options(efforts, options.effort)
            self._widget.set_access(options.access)
        else:
            thread_id = self._current_thread_id
            thread = self._thread_for_id(thread_id)
            self._widget.set_cwd_options(self._allowed_roots, None)
            if thread_id is None:
                self._widget.set_target("スレッドを選択するか、新しいスレッドを開始してください")
                self._widget.set_readonly_metadata(None, None, None)
            else:
                self._widget.set_target(self._existing_target_text(thread_id, thread))
                model, effort, sandbox = self._thread_metadata.get(thread_id, (None, None, None))
                self._widget.set_readonly_metadata(model, effort, sandbox)
        message, is_error = self._status_messages.get(self._current_draft_key, ("", False))
        if not message and not self._control_available:
            message = "Console管理Bridgeに接続するとSend／Stopを利用できます"
        self._widget.set_status(message, error=is_error)

    def _thread_for_id(self, thread_id: str | None) -> Mapping[str, object] | None:
        if thread_id is None:
            return None
        pane = getattr(self._window, "thread_pane", None)
        thread = pane._thread_for_id(thread_id) if pane is not None else None
        return thread if isinstance(thread, Mapping) else None

    @staticmethod
    def _new_target_text(cwd: str | None) -> str:
        return f"New thread draft · CWD: {cwd or '未選択'}"

    @staticmethod
    def _existing_target_text(thread_id: str, thread: Mapping[str, object] | None) -> str:
        name = thread.get("name") if thread is not None else None
        cwd = thread.get("cwd") if thread is not None else None
        display_name = name if isinstance(name, str) and name.strip() else "Untitled thread"
        project = cwd if isinstance(cwd, str) and cwd.strip() else "Project: Unknown"
        return f"Thread: {display_name} · {project} · ID {thread_id[:8]}…"

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status_messages[self._current_draft_key] = (text, error)
        self._widget.set_status(text, error=error)

    def _update_controls(self) -> None:
        prompt = self._widget.prompt_text
        has_content = bool(prompt.strip())
        under_limit = len(prompt) <= _MAX_PROMPT_CHARS
        has_target = self._current_thread_id is not None or (
            self._is_new_draft and self._current_cwd is not None
        )
        blocked_state = False
        if self._current_state is not None:
            state, _turn_id, pending = self._current_state
            blocked_state = pending or state in _ACTIVE_STATES
        can_send = (
            self._control_available
            and has_content
            and under_limit
            and has_target
            and self._active_send_key is None
            and self._active_stop_key is None
            and self._current_draft_key not in self._unknown_sends
            and not blocked_state
            and not (
                self._current_thread_id is not None
                and self._current_thread_id in self._awaiting_turns
            )
        )
        self._widget.set_send_enabled(can_send)
        target_matches = (
            self._stop_target is not None
            and not self._is_new_draft
            and self._current_thread_id == self._stop_target[0]
        )
        stop_visible = bool(target_matches and not (self._current_state or (None, None, False))[2])
        self._widget.set_stop_state(
            visible=stop_visible,
            enabled=bool(
                self._control_available
                and target_matches
                and self._active_stop_key is None
                and self._stop_waiting_for_status is None
            ),
        )

    def send(self) -> bool:
        self._save_visible_draft()
        self._update_controls()
        if not self._widget.send_button.isEnabled():
            prompt = self._widget.prompt_text
            if len(prompt) > _MAX_PROMPT_CHARS:
                self._set_status("入力は32,000文字以内にしてください", error=True)
            elif not prompt.strip():
                self._set_status("メッセージを入力してください")
            elif self._is_new_draft and self._current_cwd is None:
                self._set_status("新しいスレッドのCWDを選択してください", error=True)
            return False

        options = self._draft_options.get(self._current_draft_key, DraftOptions())
        if self._is_new_draft and options.access == "danger-full-access":
            if not self._confirm_full_access():
                self._set_status("Full Accessを確認してから送信してください")
                return False
        request_id = str(uuid4())
        key = f"composer:send:{request_id}"
        thread_id = self._current_thread_id
        context = SendContext(
            key=key,
            request_id=request_id,
            draft_key=self._current_draft_key,
            prompt=self._widget.prompt_text,
            thread_id=thread_id,
            cwd=self._current_cwd if self._is_new_draft else None,
            model=options.model if self._is_new_draft else None,
            effort=options.effort if self._is_new_draft and options.model else None,
            access=options.access if self._is_new_draft else "inherit",
        )
        payload: dict[str, object] = {"client_request_id": request_id, "prompt": context.prompt}
        if thread_id is None:
            payload["cwd"] = context.cwd or ""
            payload["sandbox_mode"] = context.access
            if context.access == "danger-full-access":
                payload["confirm_full_access"] = True
            if context.model is not None:
                payload["model"] = context.model
                if context.effort is not None:
                    payload["reasoning_effort"] = context.effort
            path = "/ui-api/control/threads"
        else:
            path = f"/ui-api/control/threads/{quote(thread_id, safe='')}/turns"
        token = getattr(self._window, "_control_token", None)
        post = getattr(self._client, "post_control_json", None)
        if not isinstance(token, str) or not token or not callable(post):
            self._set_status("Console管理Bridgeへの接続がありません", error=True)
            self._update_controls()
            return False
        self._send_contexts[key] = context
        self._active_send_key = key
        self._set_status("送信中…")
        self._update_controls()
        if not post(path, payload, token, key=key):
            if key in self._send_contexts:
                self._send_contexts.pop(key, None)
                self._active_send_key = None
                self._set_status("送信要求を開始できませんでした", error=True)
                self._update_controls()
                return False
            return True
        return True

    def stop(self) -> bool:
        if (
            not self._control_available
            or self._stop_target is None
            or self._active_stop_key is not None
            or self._stop_waiting_for_status is not None
        ):
            return False
        thread_id, turn_id = self._stop_target
        request_id = str(uuid4())
        key = f"composer:stop:{request_id}"
        context = StopContext(key, request_id, thread_id, turn_id)
        token = getattr(self._window, "_control_token", None)
        post = getattr(self._client, "post_control_json", None)
        if not isinstance(token, str) or not token or not callable(post):
            return False
        path = (
            f"/ui-api/control/threads/{quote(thread_id, safe='')}/turns/"
            f"{quote(turn_id, safe='')}/interrupt"
        )
        self._stop_contexts[key] = context
        self._active_stop_key = key
        self._set_status("Stop要求を送信中…")
        self._update_controls()
        if not post(path, {"client_request_id": request_id}, token, key=key):
            if key in self._stop_contexts:
                self._stop_contexts.pop(key, None)
                self._active_stop_key = None
                if self._current_thread_id == thread_id and not self._is_new_draft:
                    self._stop_target = None
                    self._stop_waiting_for_status = None
                    self._set_status(
                        "Stop結果を確認できません。Statusを更新してください", error=True
                    )
                    self._update_controls()
                self._request_status(thread_id, confirms_stop=True)
                return False
            return True
        return True

    def _ask_full_access(self) -> bool:
        answer = QMessageBox.question(
            self._widget,
            "Full Accessの確認",
            "この新規スレッドをFull Accessで開始します。続行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_composer_success(self, key: str, payload: object) -> None:
        if key == self._capabilities_key:
            self._capabilities_key = None
            self._apply_capabilities(payload)
            return
        send_context = self._send_contexts.pop(key, None)
        if send_context is not None:
            if self._active_send_key == key:
                self._active_send_key = None
            self._complete_send(send_context, payload)
            return
        stop_context = self._stop_contexts.pop(key, None)
        if stop_context is not None:
            if self._active_stop_key == key:
                self._active_stop_key = None
            self._complete_stop(stop_context, payload)

    def _on_composer_failure(self, key: str, category: str) -> None:
        if key == self._capabilities_key:
            self._capabilities_key = None
            self._capabilities_available = False
            self._models.clear()
            self._set_status("Model catalog unavailable; using Default")
            self._render()
            self._update_controls()
            return
        send_context = self._send_contexts.pop(key, None)
        if send_context is not None:
            if self._active_send_key == key:
                self._active_send_key = None
            self._handle_send_failure(send_context, category)
            return
        stop_context = self._stop_contexts.pop(key, None)
        if stop_context is not None:
            if self._active_stop_key == key:
                self._active_stop_key = None
            if self._current_thread_id == stop_context.thread_id and not self._is_new_draft:
                self._stop_target = None
                self._stop_waiting_for_status = None
                self._set_status_for_key(
                    f"thread:{stop_context.thread_id}",
                    "Stop結果を確認できません。Statusを更新しました",
                    error=True,
                )
            self._request_status(stop_context.thread_id, confirms_stop=True)
            self._update_controls()

    def _complete_send(self, context: SendContext, payload: object) -> None:
        if not isinstance(payload, Mapping):
            self._handle_send_failure(context, "invalid_response")
            return
        thread_id = payload.get("thread_id")
        turn_id = payload.get("turn_id")
        state = payload.get("state")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or (context.thread_id is not None and thread_id != context.thread_id)
            or not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(state, str)
            or state not in _ACTIVE_STATES | _TERMINAL_STATES
        ):
            self._handle_send_failure(context, "invalid_response")
            return
        self._awaiting_turns[thread_id] = turn_id
        if self._drafts.get(context.draft_key) == context.prompt:
            self._drafts[context.draft_key] = ""
            if self._current_draft_key == context.draft_key:
                self._widget.set_prompt_text("")
        if context.thread_id is None:
            if self._current_draft_key == context.draft_key and self._is_new_draft:
                self._pending_create_selection = (thread_id, context.draft_key)
                self._pending_create_notice_shown = False
            self._set_status_for_key(
                context.draft_key, "新しいスレッドを作成しました。Threadsを更新しています"
            )
        elif self._current_draft_key == context.draft_key:
            self._set_status_for_key(
                context.draft_key, "送信を受け付けました。Turnの状態を確認しています"
            )
        self._refresh_threads()
        if context.thread_id is not None:
            if getattr(self._window, "_selected_thread_id", None) == context.thread_id:
                self._request_selected_status()
            self._request_status(context.thread_id)
        self._update_controls()

    def _complete_stop(self, context: StopContext, payload: object) -> None:
        state = payload.get("state") if isinstance(payload, Mapping) else None
        if (
            not isinstance(payload, Mapping)
            or payload.get("thread_id") != context.thread_id
            or payload.get("turn_id") != context.turn_id
            or not isinstance(state, str)
            or state not in _ACTIVE_STATES | _TERMINAL_STATES
        ):
            if self._current_thread_id == context.thread_id and not self._is_new_draft:
                self._stop_target = None
                self._stop_waiting_for_status = None
                self._set_status("Stop結果を確認できません。Statusを更新しています", error=True)
            self._request_status(context.thread_id, confirms_stop=True)
            self._update_controls()
            return
        if getattr(self._window, "_selected_thread_id", None) == context.thread_id:
            if self._current_thread_id == context.thread_id and not self._is_new_draft:
                self._stop_waiting_for_status = (context.thread_id, context.turn_id)
                self._set_status("Stop要求を受け付けました。Turnの状態を確認しています")
            self._request_selected_status()
        self._request_status(context.thread_id, confirms_stop=True)
        self._update_controls()

    def _handle_send_failure(self, context: SendContext, category: str) -> None:
        uncertain = category not in {"http_400", "http_403", "request_error"}
        if uncertain:
            self._unknown_sends[context.draft_key] = context
            text = "送信結果が不明です。Threadsを更新し、履歴とActivityで実行結果を確認してください"
        else:
            text = {
                "http_400": "APIが送信内容を受け付けませんでした。入力とCWDを確認してください",
                "http_403": (
                    "Control APIの認証が拒否されました。Console管理Bridgeを確認してください"
                ),
                "request_error": "送信要求を作成できませんでした。入力内容を確認してください",
            }.get(category, "送信要求に失敗しました")
        self._set_status_for_key(context.draft_key, text, error=True)
        self._refresh_threads()
        if context.thread_id is not None:
            self._request_status(context.thread_id)
        self._update_controls()

    def _on_json_success(self, key: str, payload: object) -> None:
        if key == "threads" and self._pending_create_selection is not None:
            QTimer.singleShot(0, lambda payload=payload: self._select_created_thread(payload))
            return
        request = self._status_requests.pop(key, None)
        if request is not None:
            current_selection = (
                request.selection_generation is not None
                and request.selection_generation
                == getattr(self._window, "_selection_generation", None)
                and request.thread_id == getattr(self._window, "_selected_thread_id", None)
                and request.thread_id == self._current_thread_id
                and not self._is_new_draft
            )
            self._apply_status(
                request.thread_id,
                payload,
                current_selection=current_selection,
                fresh_stop_confirmation=request.confirms_stop and current_selection,
            )
            return
        status = self._status_key(key)
        if status is not None:
            generation, thread_id, current_selection = status
            if current_selection:
                if generation != getattr(self._window, "_selection_generation", generation):
                    return
                selected_thread = getattr(self._window, "_selected_thread_id", None)
                if not isinstance(selected_thread, str):
                    return
                thread_id = selected_thread
            self._apply_status(thread_id, payload, current_selection=current_selection)

    def _on_json_failure(self, key: str, _message: str) -> None:
        request = self._status_requests.pop(key, None)
        if request is not None:
            self._thread_states.pop(request.thread_id, None)
            current_selection = (
                request.selection_generation is not None
                and request.selection_generation
                == getattr(self._window, "_selection_generation", None)
                and request.thread_id == getattr(self._window, "_selected_thread_id", None)
                and request.thread_id == self._current_thread_id
                and not self._is_new_draft
            )
            if current_selection:
                self._current_state = None
                self._stop_target = None
                if request.confirms_stop:
                    self._stop_waiting_for_status = None
                    self._set_status(
                        "Stop結果を確認できません。ThreadsとActivityを確認してください",
                        error=True,
                    )
                self._update_controls()
            return
        status = self._status_key(key)
        if status is None:
            return
        generation, thread_id, current_selection = status
        if current_selection:
            if generation != getattr(self._window, "_selection_generation", generation):
                return
            selected_thread = getattr(self._window, "_selected_thread_id", None)
            if not isinstance(selected_thread, str):
                return
            thread_id = selected_thread
        self._thread_states.pop(thread_id, None)
        if current_selection and not self._is_new_draft and self._current_thread_id == thread_id:
            self._current_state = None
            self._stop_target = None
            self._update_controls()

    @staticmethod
    def _status_key(key: str) -> tuple[int, str, bool] | None:
        parts = key.split(":", 2)
        if len(parts) == 3 and parts[0] == "selection" and parts[2] == "status":
            try:
                return int(parts[1]), "", True
            except ValueError:
                return None
        return None

    def _apply_status(
        self,
        expected_thread: str,
        payload: object,
        *,
        current_selection: bool,
        fresh_stop_confirmation: bool = False,
    ) -> None:
        if not isinstance(payload, Mapping):
            return
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str) or (expected_thread and thread_id != expected_thread):
            return
        metadata = payload.get("thread_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        model = metadata.get("model") if isinstance(metadata.get("model"), str) else None
        effort = (
            metadata.get("reasoning_effort")
            if isinstance(metadata.get("reasoning_effort"), str)
            else None
        )
        sandbox = (
            metadata.get("sandbox_mode") if isinstance(metadata.get("sandbox_mode"), str) else None
        )
        self._thread_metadata[thread_id] = (model, effort, sandbox)
        state = payload.get("state") if isinstance(payload.get("state"), str) else None
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
        pending = payload.get("pending_request") is not None
        self._thread_states[thread_id] = (state, turn_id, pending)
        awaiting = self._awaiting_turns.get(thread_id)
        if awaiting is not None and (turn_id == awaiting or state in _TERMINAL_STATES):
            self._awaiting_turns.pop(thread_id, None)
        if current_selection and not self._is_new_draft and self._current_thread_id == thread_id:
            self._current_state = (state, turn_id, pending)
            wait_for_turn = self._stop_waiting_for_status
            if (
                wait_for_turn is not None
                and wait_for_turn[0] == thread_id
                and not fresh_stop_confirmation
            ):
                self._stop_target = None
                self._render()
                self._update_controls()
                return
            if wait_for_turn is not None and wait_for_turn[0] == thread_id:
                self._stop_waiting_for_status = None
            if state == "in_progress" and turn_id and not pending:
                self._stop_target = (thread_id, turn_id)
            else:
                self._stop_target = None
            if pending:
                self._set_status(
                    "承認またはユーザー入力が必要です。Activityペインを確認してください"
                )
            elif state == "in_progress":
                self._set_status("Turnを実行中です")
            elif state in _TERMINAL_STATES:
                self._set_status("Turnの終了を確認しました")
            else:
                self._set_status("")
            self._render()
            self._update_controls()

    def _on_activity(self, generation: int, payload: object) -> None:
        if (
            generation != getattr(self._window, "_selection_generation", generation)
            or not isinstance(payload, Mapping)
            or payload.get("thread_id") != self._current_thread_id
            or self._is_new_draft
            or not isinstance(self._current_thread_id, str)
        ):
            return
        thread_id = self._current_thread_id
        activity_type = payload.get("type")
        status = payload.get("status")
        turn_id = payload.get("turn_id")
        if not isinstance(activity_type, str):
            return
        if activity_type == "turn_started" and isinstance(turn_id, str) and turn_id:
            self._current_state = ("in_progress", turn_id, False)
            waiting = self._stop_waiting_for_status
            if waiting == (thread_id, turn_id):
                self._stop_target = None
            else:
                self._stop_target = (thread_id, turn_id)
                if waiting is not None and waiting[0] == thread_id:
                    self._stop_waiting_for_status = None
            self._awaiting_turns.pop(thread_id, None)
            if self._stop_target is not None:
                self._set_status("Turnを実行中です")
        elif (
            activity_type in {"turn_completed", "turn_failed", "turn_interrupted"}
            and isinstance(status, str)
            and status in _TERMINAL_STATES
            and isinstance(turn_id, str)
        ):
            waiting = self._stop_waiting_for_status
            if waiting is not None and waiting != (thread_id, turn_id):
                self._update_controls()
                return
            self._current_state = (str(status), turn_id, False)
            self._stop_target = None
            self._stop_waiting_for_status = None
            self._awaiting_turns.pop(thread_id, None)
            self._set_status("Turnの終了を確認しました")
        elif (
            activity_type in {"approval_requested", "user_input_requested"}
            and status == "requested"
        ):
            state = "needs_approval" if activity_type == "approval_requested" else "needs_input"
            self._current_state = (
                state,
                turn_id if isinstance(turn_id, str) else None,
                True,
            )
            self._stop_target = None
            self._set_status("承認またはユーザー入力が必要です。Activityペインを確認してください")
        self._update_controls()

    def _refresh_threads(self) -> None:
        refresh = getattr(self._window, "_request_threads", None)
        if callable(refresh):
            refresh()

    def _request_selected_status(self) -> None:
        request = getattr(self._window, "_request_selected_status", None)
        if callable(request):
            request()

    def _request_status(self, thread_id: str, *, confirms_stop: bool = False) -> None:
        getter = getattr(self._client, "get_json", None)
        if not callable(getter):
            return
        self._status_request_sequence += 1
        key = f"composer:refresh-status:{self._status_request_sequence}"
        selected = getattr(self._window, "_selected_thread_id", None) == thread_id
        generation = getattr(self._window, "_selection_generation", None) if selected else None
        self._status_requests[key] = StatusRequestContext(
            thread_id,
            generation if isinstance(generation, int) else None,
            confirms_stop,
        )
        if not getter(
            f"/ui-api/threads/{quote(thread_id, safe='')}/status",
            key=key,
            query={"activity_limit": 50},
        ):
            self._status_requests.pop(key, None)

    def _select_created_thread(self, payload: object) -> None:
        pending = self._pending_create_selection
        if pending is None:
            return
        thread_id, draft_key = pending
        if not self._is_new_draft or self._current_draft_key != draft_key:
            self._pending_create_selection = None
            self._pending_create_notice_shown = False
            return
        pane = self._window.thread_pane
        item = self._find_thread_item(pane, thread_id)
        if item is not None:
            self._pending_create_selection = None
            self._pending_create_notice_shown = False
            pane.list_widget.setCurrentItem(item)
            pane.thread_selected.emit(thread_id)
        elif not self._pending_create_notice_shown:
            self._pending_create_notice_shown = True
            self._set_status(
                "スレッドを一覧で確認できません。ThreadsのRefreshを実行してください",
                error=True,
            )
        self._update_controls()

    @staticmethod
    def _find_thread_item(pane: object, thread_id: str) -> QTreeWidgetItem | None:
        tree = cast(QTreeWidget | None, getattr(pane, "list_widget", None))
        if tree is None:
            return None
        for index in range(tree.topLevelItemCount()):
            parent = tree.topLevelItem(index)
            if parent is None:
                continue
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                if child is None:
                    continue
                if child.data(0, Qt.ItemDataRole.UserRole) == thread_id:
                    parent.setExpanded(True)
                    return child
        return None

    def _set_status_for_key(self, key: str, text: str, *, error: bool = False) -> None:
        self._status_messages[key] = (text, error)
        if key == self._current_draft_key:
            self._widget.set_status(text, error=error)


def create_composer_controller(window: MainWindow, api_client: ApiClient) -> ComposerController:
    widget = cast(ComposerWidget, window.composer_widget)
    return ComposerController(
        window, widget, api_client, allowed_roots=window._config.allowed_roots
    )
