from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from ..account_auth import project_account_auth_status, validate_auth_url
from .i18n import tr

if TYPE_CHECKING:
    from .main_window import MainWindow


_LOGIN_TIMEOUT_MS = 15 * 60 * 1000
_STATUS_POLL_MS = 15_000
_LOGIN_POLL_MS = 3_000


@dataclass(frozen=True)
class _RequestOwner:
    kind: str
    connection_generation: int
    login_generation: int
    server_revision: int
    login_revision: int | None


class AccountAuthController(QObject):
    def __init__(
        self,
        window: MainWindow,
        *,
        browser_opener: Callable[[QUrl], bool] | None = None,
    ) -> None:
        super().__init__(window)
        self._window = window
        self._browser_opener = browser_opener or QDesktopServices.openUrl
        self._generation = 0
        self._login_generation = 0
        self._request_sequence = 0
        self._requests: dict[str, _RequestOwner] = {}
        self._status_in_flight_key: str | None = None
        self._connected = False
        self._last_server_revision = -1
        self._last_status: Mapping[str, object] | None = None
        self._baseline_server_revision = 0
        self._baseline_login_revision = 0
        self._attempt_server_revision = 0
        self._login_revision: int | None = None
        self._requested_flow_type: str | None = None
        self._login_id: str | None = None
        self._login_pending = False
        self._start_in_flight = False
        self._cancel_in_flight = False
        self._local_issue: str | None = None
        self._dialogs: list[QMessageBox] = []
        self._secret_dialogs: list[QMessageBox] = []
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_STATUS_POLL_MS)
        self._poll_timer.timeout.connect(self._poll_status)
        self._login_timeout = QTimer(self)
        self._login_timeout.setSingleShot(True)
        self._login_timeout.setInterval(_LOGIN_TIMEOUT_MS)
        self._login_timeout.timeout.connect(self._on_login_timeout)
        client = window._client
        client.json_succeeded.connect(self._on_json_success)
        client.json_failed.connect(self._on_json_failure)
        control_success = getattr(client, "composer_json_succeeded", None)
        control_failure = getattr(client, "composer_json_failed", None)
        if control_success is not None:
            control_success.connect(self._on_control_success)
        if control_failure is not None:
            control_failure.connect(self._on_control_failure)
        window.status_dialog.finished.connect(lambda _result: self.on_status_closed())
        window.account_auth_refresh_button.clicked.connect(self.refresh)
        window.account_auth_login_button.clicked.connect(self.start_chatgpt_login)
        window.account_auth_device_button.clicked.connect(self.start_device_code_login)
        window.account_auth_cancel_button.clicked.connect(self.cancel_login)
        self._render_status(None)

    def on_connection_changed(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._generation += 1
        self._last_server_revision = -1
        self._status_in_flight_key = None
        if not connected:
            self._requests.clear()
            self._forget_login()
            self._poll_timer.stop()
            self._last_status = None
            self._render_status(None)
            return
        if self._can_control():
            self.refresh()
        else:
            self._request_status()
        if self._window.status_dialog.isVisible():
            self._poll_timer.start()

    def on_status_opened(self) -> None:
        if self._connected:
            self._request_status()
            self._poll_timer.start()

    def on_status_closed(self) -> None:
        if not self._login_pending:
            self._poll_timer.stop()

    def refresh_controls(self) -> None:
        self._render_status(self._last_status)

    def close(self) -> None:
        if self._login_pending and self._can_control():
            self.cancel_login()
        self._generation += 1
        self._connected = False
        self._poll_timer.stop()
        self._requests.clear()
        self._forget_login()
        self._clear_all_dialogs()

    def refresh(self, _checked: bool = False) -> None:
        if not self._can_control():
            self._show_local_message(tr("Codex account refresh is unavailable."))
            return
        if not self._send_control("refresh", "/ui-api/control/account/auth/refresh", {}):
            self._show_local_message(tr("The account request could not be started."))

    def start_chatgpt_login(self, _checked: bool = False) -> None:
        self._start_login("chatgpt")

    def start_device_code_login(self, _checked: bool = False) -> None:
        self._start_login("chatgptDeviceCode")

    def _start_login(self, flow_type: str) -> None:
        if not self._can_control():
            self._show_local_message(
                tr("Codex sign-in requires a connected Bridge launched by this Console.")
            )
            return
        if self._login_pending:
            return
        self._login_generation += 1
        self._baseline_server_revision = max(self._last_server_revision, 0)
        previous_login_revision = (
            self._last_status.get("login_revision", 0) if self._last_status is not None else 0
        )
        self._baseline_login_revision = (
            previous_login_revision if type(previous_login_revision) is int else 0
        )
        self._attempt_server_revision = self._baseline_server_revision + 1
        self._login_revision = None
        self._requested_flow_type = flow_type
        self._login_id = None
        self._login_pending = True
        self._start_in_flight = True
        self._cancel_in_flight = False
        self._local_issue = None
        self._login_timeout.stop()
        self._status_in_flight_key = None
        self.refresh_controls()
        accepted = self._send_control(
            "start",
            "/ui-api/control/account/auth/login/start",
            {"flow_type": flow_type},
        )
        if not accepted and self._login_pending:
            self._forget_login()
            self._show_local_message(tr("The account request could not be started."))

    def cancel_login(self, _checked: bool = False) -> None:
        if (
            not self._can_control()
            or not self._login_pending
            or self._login_revision is None
            or self._cancel_in_flight
        ):
            return
        payload: dict[str, object] = {"login_revision": self._login_revision}
        if self._login_id is not None:
            payload["login_id"] = self._login_id
        self._cancel_in_flight = True
        self._local_issue = None
        self.refresh_controls()
        accepted = self._send_control(
            "cancel", "/ui-api/control/account/auth/login/cancel", payload
        )
        if not accepted:
            self._cancel_in_flight = False
            self._local_issue = "cancel_unresolved"
            self.refresh_controls()
            self._request_status()

    def _can_control(self) -> bool:
        predicate = getattr(self._window, "_can_display_control_token", None)
        return bool(callable(predicate) and predicate())

    def _send_control(self, kind: str, path: str, payload: Mapping[str, object]) -> bool:
        token = self._window._control_token
        if not isinstance(token, str) or not token:
            return False
        self._request_sequence += 1
        key = f"account-auth:{kind}:{self._generation}:{self._request_sequence}"
        self._requests[key] = _RequestOwner(
            kind=kind,
            connection_generation=self._generation,
            login_generation=self._login_generation,
            server_revision=max(self._last_server_revision, 0),
            login_revision=self._login_revision if kind == "cancel" else None,
        )
        accepted = self._window._client.post_control_json(path, dict(payload), token, key=key)
        if not accepted:
            self._requests.pop(key, None)
        return accepted

    def _request_status(self) -> None:
        if not self._connected or self._status_in_flight_key is not None:
            return
        self._request_sequence += 1
        key = f"account-auth:status:{self._generation}:{self._request_sequence}"
        self._requests[key] = _RequestOwner(
            kind="status",
            connection_generation=self._generation,
            login_generation=self._login_generation,
            server_revision=max(self._last_server_revision, 0),
            login_revision=self._login_revision,
        )
        if self._window._client.get_json("/ui-api/account/auth", key=key):
            self._status_in_flight_key = key
        else:
            self._requests.pop(key, None)

    def _poll_status(self) -> None:
        if not self._connected:
            self._poll_timer.stop()
            return
        if self._login_pending or self._window.status_dialog.isVisible():
            self._request_status()
            return
        self._poll_timer.stop()

    def _on_json_success(self, key: str, payload: object) -> None:
        if self._status_in_flight_key == key:
            self._status_in_flight_key = None
        request = self._take_request(key, "status")
        if request is not None:
            self._apply_status(payload, request=request)

    def _on_json_failure(self, key: str, _message: str) -> None:
        if self._status_in_flight_key == key:
            self._status_in_flight_key = None
        request = self._take_request(key, "status")
        if request is not None:
            if self._login_pending:
                self._local_issue = self._local_issue or "status_unavailable"
                self._render_status(self._last_status)
            elif request.connection_generation == self._generation:
                self._render_status(None)

    def _on_control_success(self, key: str, payload: object) -> None:
        request = self._take_request(key)
        if request is None:
            return
        if request.kind == "start":
            if self._login_pending:
                self._apply_login_start(payload, request=request)
            return
        if request.kind == "cancel":
            if not self._login_pending or request.login_revision != self._login_revision:
                return
            self._cancel_in_flight = False
            self._local_issue = None
        status_applied = self._apply_status(payload, request=request)
        if request.kind == "refresh" and status_applied:
            self._release_unstarted_attempt_if_confirmed(request)
        if (
            request.kind == "cancel"
            and self._login_pending
            and isinstance(self._last_status, Mapping)
            and self._last_status.get("login_revision") == self._login_revision
            and self._last_status.get("login_state") == "checking"
            and self._last_status.get("login_result") == "none"
        ):
            self._login_id = None
            self._login_timeout.stop()
            self._start_in_flight = False
            self._poll_timer.setInterval(_LOGIN_POLL_MS)
            self._poll_timer.start()
        self.refresh_controls()

    def _on_control_failure(self, key: str, message: str) -> None:
        request = self._take_request(key)
        if request is None:
            return
        if request.kind == "start":
            if message in {"request_error", "http_400", "http_403"}:
                self._forget_login()
                self._show_local_message(tr("Codex sign-in could not be started."))
            else:
                self._await_start_reconciliation()
        elif request.kind == "cancel":
            if request.login_revision != self._login_revision or not self._login_pending:
                return
            self._cancel_in_flight = False
            self._local_issue = "cancel_unresolved"
            self._render_status(self._last_status)
            self._request_status()
        elif request.kind == "refresh":
            self._show_local_message(tr("Codex account status could not be refreshed."))

    def _take_request(self, key: str, expected: str | None = None) -> _RequestOwner | None:
        request = self._requests.pop(key, None)
        if request is None or (expected is not None and request.kind != expected):
            return None
        if (
            request.connection_generation != self._generation
            or request.login_generation != self._login_generation
        ):
            return None
        return request

    def _apply_status(self, payload: object, *, request: _RequestOwner | None = None) -> bool:
        safe_payload = project_account_auth_status(payload)
        revision_value = safe_payload.get("revision", 0)
        revision = revision_value if type(revision_value) is int else 0
        if revision < self._last_server_revision or (
            request is not None and revision < request.server_revision
        ):
            if self._login_pending:
                self._reconcile_login_status(self._last_status)
            return False
        if request is not None and request.login_generation != self._login_generation:
            return False
        self._last_server_revision = revision
        self._last_status = safe_payload
        self._render_status(safe_payload)
        self._reconcile_login_status(safe_payload)
        return True

    def _release_unstarted_attempt_if_confirmed(self, request: _RequestOwner) -> None:
        status = self._last_status
        status_revision = status.get("revision") if status is not None else None
        if (
            not self._login_pending
            or self._login_revision is not None
            or self._start_in_flight
            or self._local_issue != "start_unknown"
            or status is None
            or status.get("login_revision") != self._baseline_login_revision
            or type(status_revision) is not int
            or status_revision < request.server_revision
        ):
            return
        login_state = status.get("login_state")
        login_result = status.get("login_result")
        no_active_attempt = (
            (login_state == "idle" and login_result == "none")
            or (login_state == "succeeded" and login_result == "succeeded")
            or (login_state == "failed" and login_result == "failed")
            or (login_state == "cancelled" and login_result == "cancelled")
        )
        if no_active_attempt:
            self._forget_login()
            self._show_local_message(
                tr("No active sign-in attempt was found after refresh. You can try again.")
            )

    def _await_start_reconciliation(self) -> None:
        self._start_in_flight = False
        self._local_issue = "start_unknown"
        self._poll_timer.setInterval(_LOGIN_POLL_MS)
        self._poll_timer.start()
        self._render_status(self._last_status)
        self._request_status()
        if self._can_control():
            self._send_control("refresh", "/ui-api/control/account/auth/refresh", {})

    def _reconcile_login_status(self, status: Mapping[str, object] | None) -> None:
        if not self._login_pending or status is None:
            return
        status_login_revision = status.get("login_revision")
        status_revision = status.get("revision")
        if (
            self._login_revision is None
            and type(status_login_revision) is int
            and status_login_revision > self._baseline_login_revision
            and type(status_revision) is int
            and status_revision > self._baseline_server_revision
        ):
            self._login_revision = status_login_revision
            self._attempt_server_revision = self._baseline_server_revision + 1
        if (
            self._login_revision is None
            or status_login_revision != self._login_revision
            or type(status_revision) is not int
            or status_revision < self._attempt_server_revision
        ):
            return
        self._start_in_flight = False
        login_state = status.get("login_state")
        login_result = status.get("login_result")
        if login_state == "cancelled" and login_result == "cancelled":
            self._finish_login("cancelled")
            return
        if (
            login_state == "succeeded"
            and login_result == "succeeded"
            and status.get("state") == "signed_in"
        ):
            self._finish_login("succeeded")
            return
        if login_state == "failed" and login_result == "failed":
            self._finish_login("failed")
            return
        if login_state == "checking" and login_result == "none":
            self._login_id = None
            self._cancel_in_flight = False
            self._local_issue = None
            self._login_timeout.stop()
            self._poll_timer.setInterval(_LOGIN_POLL_MS)
            self._poll_timer.start()
            self.refresh_controls()
        elif login_state == "waiting":
            self._cancel_in_flight = False
            if status.get("issue") in {"cancel_failed", "cancel_unknown"}:
                self._local_issue = "cancel_unresolved"
            self.refresh_controls()

    def _finish_login(self, result: str) -> None:
        self._forget_login()
        if result == "succeeded":
            self._window._request_usage(force=True)
            self._show_local_message(tr("Codex account sign-in was confirmed."))
        elif result == "cancelled":
            self._show_local_message(tr("Codex sign-in was canceled."))
        else:
            self._show_local_message(
                tr("Codex sign-in could not be confirmed. Check the account status and try again.")
            )
        if self._connected:
            self._request_status()

    def _apply_login_start(self, payload: object, *, request: _RequestOwner | None = None) -> None:
        if not isinstance(payload, Mapping):
            self._start_response_unresolved()
            return
        login_revision = payload.get("login_revision")
        if type(login_revision) is not int or not 1 <= login_revision <= 2**53 - 1:
            self._start_response_unresolved()
            return
        self._login_revision = login_revision
        response_revision = payload.get("revision")
        if type(response_revision) is int and response_revision >= 0:
            self._attempt_server_revision = max(
                self._baseline_server_revision + 1, response_revision
            )
        else:
            self._attempt_server_revision = self._baseline_server_revision + 1
        self._start_in_flight = False
        login_id = payload.get("login_id")
        if isinstance(login_id, str) and login_id.strip() and len(login_id) <= 256:
            self._login_id = login_id
        self._apply_status(payload, request=request)
        if not self._login_pending:
            return
        if self._last_status is not None:
            self._reconcile_login_status(self._last_status)
        if not self._login_pending:
            return
        safe = project_account_auth_status(payload)
        if (
            safe.get("login_revision") == self._login_revision
            and safe.get("login_state") == "checking"
            and safe.get("login_result") == "none"
        ):
            return
        if (
            self._last_status is not None
            and self._last_status.get("login_revision") == self._login_revision
            and self._last_status.get("login_state") == "checking"
            and self._last_status.get("login_result") == "none"
        ):
            return
        if self._login_id is None:
            self._reject_start_response()
            return
        flow_type = payload.get("flow_type")
        if (
            not isinstance(flow_type, str)
            or flow_type != self._requested_flow_type
            or flow_type not in {"chatgpt", "chatgptDeviceCode"}
            or payload.get("start_issue") is not None
        ):
            self._reject_start_response()
            return
        url_field = "auth_url" if flow_type == "chatgpt" else "verification_url"
        url = payload.get(url_field)
        if not validate_auth_url(url, flow_type):
            self._reject_start_response()
            return
        code: str | None = None
        if flow_type == "chatgptDeviceCode":
            candidate = payload.get("user_code")
            if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 128:
                self._reject_start_response()
                return
            code = candidate
        self._login_timeout.start()
        self._poll_timer.setInterval(_LOGIN_POLL_MS)
        self._poll_timer.start()
        opened = False
        try:
            opened = self._browser_opener(QUrl(cast(str, url)))
        except Exception:
            opened = False
        if flow_type == "chatgptDeviceCode" and code is not None:
            text = tr(
                "Open the verification page and enter this one-time code: {p0}\n\n{p1}",
                p0=code,
                p1=cast(str, url),
            )
            if not opened:
                text += "\n\n" + tr(
                    "The browser could not be opened. Copy the verification address into a browser."
                )
            self._show_local_message(text, title=tr("ChatGPT device sign-in"), sensitive=True)
        elif opened:
            self._show_local_message(
                tr("Continue signing in in your browser. Return to Console when finished."),
                title=tr("ChatGPT sign-in"),
            )
        else:
            self._show_local_message(
                tr(
                    "The browser could not be opened. Open this sign-in address manually:\n{p0}",
                    p0=cast(str, url),
                ),
                title=tr("ChatGPT sign-in"),
                sensitive=True,
            )
        self._request_status()

    def _start_response_unresolved(self) -> None:
        self._await_start_reconciliation()

    def _reject_start_response(self) -> None:
        self._local_issue = "start_response_rejected"
        self.refresh_controls()
        if self._login_revision is not None:
            self.cancel_login()

    def _render_status(self, payload: Mapping[str, object] | None) -> None:
        window = self._window
        self._last_status = payload
        if not self._connected:
            text = tr("Codex account: Bridge or App Server unavailable")
        elif payload is None:
            text = tr("Codex account: status unavailable")
        else:
            state = payload.get("state")
            state_labels = {
                "unknown": "Unknown",
                "checking": "Checking",
                "signed_in": "Account recognized",
                "login_required": "Sign-in required",
                "not_required": "Sign-in not required",
            }
            state_label = state_labels.get(state) if isinstance(state, str) else None
            text = tr("Codex account: {p0}", p0=tr(state_label or "Unknown"))
            if state == "signed_in":
                text += "\n" + tr("Saved account recognition does not guarantee online access.")
            account_type = payload.get("account_type")
            if isinstance(account_type, str):
                text += " · " + tr(account_type)
            plan_type = payload.get("plan_type")
            if isinstance(plan_type, str):
                text += " · " + tr(plan_type)
            if payload.get("auth_warning") == "execution_auth_error":
                text += "\n" + tr(
                    "A recent execution reported an authentication error. The saved account "
                    "is recognized, but reauthentication may be needed."
                )
        if self._login_pending:
            if self._cancel_in_flight:
                text += "\n" + tr("Canceling sign-in")
            elif self._start_in_flight:
                text += "\n" + tr("Starting sign-in")
            elif self._login_id is not None:
                text += "\n" + tr("Sign-in is waiting for the browser")
            elif self._login_revision is not None:
                text += "\n" + tr("Sign-in confirmation is being checked")
        elif payload is not None:
            login_state = payload.get("login_state")
            if login_state == "waiting":
                text += " · " + tr("Sign-in is waiting for the browser")
            elif login_state == "cancelling":
                text += " · " + tr("Canceling sign-in")
            elif login_state == "failed":
                text += " · " + tr("Sign-in did not complete")
        issue = self._local_issue
        if issue is None and payload is not None:
            if payload.get("issue") in {"cancel_failed", "cancel_unknown"}:
                issue = "cancel_unresolved"
        issue_texts = {
            "cancel_unresolved": (
                "The sign-in attempt is unresolved. Check status or try canceling again "
                "before starting another sign-in."
            ),
            "start_unknown": (
                "The sign-in start result is unknown. Check status before trying again."
            ),
            "start_response_rejected": (
                "The sign-in response could not be validated. Cancellation was requested; "
                "check status if the attempt remains open."
            ),
            "status_unavailable": (
                "The sign-in status could not be checked. The existing attempt remains unresolved."
            ),
        }
        issue_text = issue_texts.get(issue) if isinstance(issue, str) else None
        if issue_text is not None:
            text += "\n" + tr(issue_text)
        window.account_auth_status_label.setText(text)
        can_control = self._can_control()
        action_available = self._connected and can_control
        window.account_auth_refresh_button.setEnabled(action_available)
        can_start_login = action_available and not self._login_pending
        window.account_auth_login_button.setEnabled(can_start_login)
        window.account_auth_device_button.setEnabled(can_start_login)
        window.account_auth_cancel_button.setEnabled(
            action_available
            and self._login_pending
            and self._login_revision is not None
            and not self._cancel_in_flight
            and (self._login_id is not None or self._login_revision is not None)
        )
        if not can_control and self._connected:
            reason = tr(
                "Sign-in controls require this Console to launch the Bridge with its "
                "local control token."
            )
            for button in (
                window.account_auth_refresh_button,
                window.account_auth_login_button,
                window.account_auth_device_button,
                window.account_auth_cancel_button,
            ):
                button.setToolTip(reason)
        settings_actions = getattr(window, "_settings_actions", {})
        for action_name in ("Sign in / Re-sign in", "Sign in with device code"):
            action = settings_actions.get(action_name)
            if action is not None:
                action.setEnabled(can_start_login)

    def _on_login_timeout(self) -> None:
        if not self._login_pending:
            return
        self._local_issue = "cancel_unresolved"
        self.cancel_login()
        self._show_local_message(
            tr(
                "The sign-in attempt timed out. A cancellation was requested; check its "
                "status or cancel it again before starting another sign-in."
            )
        )
        self._render_status(self._last_status)

    def _forget_login(self) -> None:
        self._login_generation += 1
        self._login_timeout.stop()
        self._login_id = None
        self._login_revision = None
        self._requested_flow_type = None
        self._login_pending = False
        self._start_in_flight = False
        self._cancel_in_flight = False
        self._local_issue = None
        self._status_in_flight_key = None
        self._poll_timer.setInterval(_STATUS_POLL_MS)
        self._clear_secret_dialogs()
        self.refresh_controls()

    def _clear_secret_dialogs(self) -> None:
        for dialog in self._secret_dialogs:
            dialog.setText("")
            dialog.close()
        self._secret_dialogs.clear()

    def _clear_all_dialogs(self) -> None:
        for dialog in self._dialogs:
            dialog.setText("")
            dialog.close()
        self._dialogs.clear()
        self._secret_dialogs.clear()

    def _show_local_message(
        self, text: str, *, title: str | None = None, sensitive: bool = False
    ) -> None:
        dialog = QMessageBox(self._window)
        dialog.setWindowTitle(title or tr("Codex account"))
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(text)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.finished.connect(lambda _result, item=dialog: self._forget_dialog(item))
        self._dialogs.append(dialog)
        if sensitive:
            self._secret_dialogs.append(dialog)
        dialog.open()

    def _forget_dialog(self, dialog: QMessageBox) -> None:
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)
        if dialog in self._secret_dialogs:
            self._secret_dialogs.remove(dialog)
