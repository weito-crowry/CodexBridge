from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from .history_timeline import ACTIVE_TURN_STATES

if TYPE_CHECKING:
    from .main_window import MainWindow


class ApiResultDispatcher:
    def __init__(
        self,
        window: MainWindow,
        *,
        read_project_names: Callable[[], dict[str, str]],
        observe_thread_status: Callable[[Mapping[str, object]], tuple[bool, str | None]],
    ) -> None:
        self._window = window
        self._read_project_names = read_project_names
        self._observe_thread_status = observe_thread_status

    def apply_result(self, key: str, payload: object) -> None:
        window = self._window
        if key in window._pending_thread_copies:
            window._apply_thread_copy_page(key, payload)
            return
        if window._bulk_thread_naming.handle_result(key, payload):
            return
        if key.startswith("rename:"):
            window._thread_interactions.apply_rename_success(key)
            return
        if window._bridge_stop_confirmation.active and key in {"health", "bridge-status"}:
            if key == "health":
                window._health_observed = True
                window._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
                window._bridge_stop_confirmation.observe_health(True)
            elif isinstance(payload, Mapping):
                window._status_observed = True
                window._bridge_ready = payload.get("bridge") in {"ready", "connected"}
                window._app_server_ready = payload.get("app_server") in {"ready", "connected"}
                window._bridge_stop_confirmation.observe_status(True)
            window._maybe_finish_stop_confirmation()
            return
        if key == "health":
            window._health_observed = True
            window._health_ok = isinstance(payload, Mapping) and payload.get("status") == "ok"
            if window._health_ok:
                window.bottom_status_label.setText("Bridge reachable")
            window._apply_runtime_observation()
            window._sync_overall_status()
            window._sync_empty_state()
            return
        if key == "bridge-status":
            window._status_observed = True
            was_ready = window._bridge_ready and window._app_server_ready
            if isinstance(payload, Mapping):
                bridge = payload.get("bridge")
                app_server = payload.get("app_server")
                window._bridge_ready = bridge in {"ready", "connected"}
                window._app_server_ready = app_server in {"ready", "connected"}
                window.bridge_status_label.setText(
                    "Bridge: connected" if window._bridge_ready else "Bridge: disconnected"
                )
                window.app_server_status_label.setText(
                    "App Server: ready" if window._app_server_ready else "App Server: failed"
                )
                window._apply_runtime_observation()
            else:
                window._bridge_ready = False
                window._app_server_ready = False
                window.bridge_status_label.setText("Bridge: disconnected")
                window.app_server_status_label.setText("App Server: failed")
                window._apply_runtime_observation()
            window._record_bridge_status_observation(
                window._bridge_ready and window._app_server_ready
            )
            ready = window._usage_ready
            if ready and not was_ready:
                window._begin_usage_sequence()
                window._schedule_codex_update_auto_check()
            elif not ready:
                window._invalidate_usage_sequence()
            window._sync_overall_status()
            window._sync_empty_state()
            return
        if key == "usage":
            window._usage_requests.apply_result(payload)
            return
        if key == "launch:health":
            window._apply_launch_health(payload)
            return
        if key == "launch:status":
            window._apply_launch_status(payload)
            return
        if key == "threads":
            if isinstance(payload, Mapping) and isinstance(payload.get("threads"), list):
                threads = [thread for thread in payload["threads"] if isinstance(thread, Mapping)]
                thread_ids = {
                    thread_id
                    for thread in threads
                    if isinstance((thread_id := thread.get("id")), str)
                }
                statuses = {
                    thread_id: state
                    for thread_id, state in window._thread_turn_statuses.items()
                    if thread_id in thread_ids
                }
                for thread in threads:
                    thread_id = thread.get("id")
                    if not isinstance(thread_id, str):
                        continue
                    observed, state = self._observe_thread_status(thread)
                    if observed:
                        if state is None:
                            statuses.pop(thread_id, None)
                        else:
                            statuses[thread_id] = state
                window._thread_turn_statuses = statuses
                window._active_thread_ids = {
                    thread_id
                    for thread_id, state in statuses.items()
                    if state in ACTIVE_TURN_STATES
                }
                window.thread_pane.set_threads(
                    threads,
                    thread_statuses=window._thread_turn_statuses,
                    project_names=self._read_project_names(),
                )
                if not threads and window.thread_pane.thread_count == 0:
                    window.thread_pane.set_empty_state("No threads found.")
                window._sync_empty_state()
            return

        selection = window._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        live_refresh = suffix.startswith("history:")
        if live_refresh:
            suffix = suffix.removeprefix("history:")
        if suffix == "detail":
            return
        if suffix == "turns":
            has_active_turn = window._history_timeline.apply_turns(
                payload, live_refresh=live_refresh
            )
            if has_active_turn is not None:
                if window._selected_thread_id is not None:
                    active_states = [
                        state
                        for state in window._turn_statuses.values()
                        if state in ACTIVE_TURN_STATES
                    ]
                    if active_states:
                        window._update_thread_activity(
                            window._selected_thread_id, active_states[-1]
                        )
                    else:
                        window._update_thread_activity(window._selected_thread_id, "completed")
                window._render_timeline()
            window._finish_selected_history_request(key)
            return
        if suffix == "items":
            window._apply_items(payload, prepend=False, live_refresh=live_refresh)
            window._finish_selected_history_request(key)
            return
        if suffix == "status":
            window._apply_status(payload)
            return
        if suffix.startswith("older:"):
            window._apply_items(payload, prepend=True)
            window._finish_selected_history_request(key)

    def apply_error(self, key: str, message: str) -> None:
        window = self._window
        if key in window._pending_thread_copies:
            window._thread_interactions.discard_copy(key)
            return
        if window._bulk_thread_naming.handle_error(key, message):
            return
        if key.startswith("rename:"):
            window._thread_interactions.discard_rename(key)
            return
        if key == "launch:health":
            window._bridge_readiness.fail_health()
            return
        if key == "launch:status":
            window._bridge_readiness.fail_status()
            return
        if window._bridge_stop_confirmation.active and key in {"health", "bridge-status"}:
            if key == "health":
                window._health_observed = True
                window._health_ok = False
                window.bridge_status_label.setText("Bridge: disconnected")
                window._bridge_stop_confirmation.observe_health(False)
            else:
                window._status_observed = True
                window._bridge_ready = False
                window._app_server_ready = False
                window.app_server_status_label.setText("App Server: failed")
                window._bridge_stop_confirmation.observe_status(False)
            window._maybe_finish_stop_confirmation()
            return
        if key in {"health", "bridge-status"}:
            if key == "health":
                window._health_observed = True
                window._health_ok = False
            else:
                window._status_observed = True
                window._bridge_ready = False
                window._app_server_ready = False
                window._invalidate_usage_sequence()
            window.bridge_status_label.setText("Bridge: disconnected")
            window.app_server_status_label.setText("App Server: failed")
            window.bottom_status_label.setText(message)
            window._apply_runtime_observation()
            if key == "bridge-status":
                window._record_bridge_status_observation(False)
            window._sync_overall_status()
            window._sync_empty_state()
            return
        if key == "usage":
            window._usage_requests.apply_error(message)
            return
        selection = window._is_current_selection(key)
        if selection is None:
            return
        _, suffix = selection
        live_refresh = suffix.startswith("history:")
        if live_refresh:
            suffix = suffix.removeprefix("history:")
        window.bottom_status_label.setText(message)
        if suffix in {"items", "turns"} or suffix.startswith("older:"):
            if not live_refresh:
                window.history_pane.set_error(message)
        if suffix == "status":
            window._history_stream.status_request_failed(message)
        window._finish_selected_history_request(key)
