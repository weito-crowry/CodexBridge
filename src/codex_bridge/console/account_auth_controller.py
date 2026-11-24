from __future__ import annotations

from collections.abc import Callable, Mapping
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
        self._request_sequence = 0
        self._requests: dict[str, tuple[str, int]] = {}
        self._status_in_flight = False
        self._connected = False
        self._last_status: Mapping[str, object] | None = None
        self._login_id: str | None = None
        self._login_pending = False
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
        self._status_in_flight = False
        if not connected:
            self._forget_login()
            self._poll_timer.stop()
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
        if self._login_id is None and not self._login_pending:
            self._poll_timer.stop()

    def refresh_controls(self) -> None:
        self._render_status(self._last_status)

    def close(self) -> None:
        if self._login_id is not None and self._can_control():
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
        self._send_control(
            "refresh",
            "/ui-api/control/account/auth/refresh",
            {},
        )

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
        if (
            self._login_id is not None
            or self._login_pending
            or any(kind == "start" for kind, _ in self._requests.values())
        ):
            return
        self._send_control(
            "start",
            "/ui-api/control/account/auth/login/start",
            {"flow_type": flow_type},
        )

    def cancel_login(self, _checked: bool = False) -> None:
        if not self._can_control() or self._login_id is None:
            return
        self._send_control(
            "cancel",
            "/ui-api/control/account/auth/login/cancel",
            {"login_id": self._login_id},
        )

    def _can_control(self) -> bool:
        predicate = getattr(self._window, "_can_display_control_token", None)
        return bool(callable(predicate) and predicate())

    def _send_control(self, kind: str, path: str, payload: Mapping[str, object]) -> None:
        token = self._window._control_token
        if not isinstance(token, str) or not token:
            return
        self._request_sequence += 1
        key = f"account-auth:{kind}:{self._generation}:{self._request_sequence}"
        self._requests[key] = (kind, self._generation)
        accepted = self._window._client.post_control_json(path, payload, token, key=key)
        if not accepted:
            self._requests.pop(key, None)
            self._show_local_message(tr("The account request could not be started."))

    def _request_status(self) -> None:
        if not self._connected or self._status_in_flight:
            return
        self._request_sequence += 1
        key = f"account-auth:status:{self._generation}:{self._request_sequence}"
        self._requests[key] = ("status", self._generation)
        if self._window._client.get_json("/ui-api/account/auth", key=key):
            self._status_in_flight = True
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
        request = self._take_request(key, "status")
        if request is None:
            return
        self._status_in_flight = False
        self._apply_status(payload)

    def _on_json_failure(self, key: str, _message: str) -> None:
        request = self._take_request(key, "status")
        if request is None:
            return
        self._status_in_flight = False
        if request[1] == self._generation:
            self._render_status(None)

    def _on_control_success(self, key: str, payload: object) -> None:
        request = self._take_request(key)
        if request is None or request[1] != self._generation:
            return
        kind = request[0]
        if kind == "start":
            self._apply_login_start(payload)
        else:
            self._apply_status(payload)
            if kind == "cancel":
                if (
                    self._login_pending
                    and isinstance(self._last_status, Mapping)
                    and self._last_status.get("login_state") == "checking"
                ):
                    self._login_id = None
                    self._login_timeout.stop()
                    self._poll_timer.setInterval(_LOGIN_POLL_MS)
                    self._poll_timer.start()
                    self.refresh_controls()
                else:
                    self._forget_login()

    def _on_control_failure(self, key: str, message: str) -> None:
        request = self._take_request(key)
        if request is None or request[1] != self._generation:
            return
        kind = request[0]
        if kind == "start":
            self._show_local_message(tr("Codex sign-in could not be started."))
            self._request_status()
        elif kind == "cancel":
            self._show_local_message(tr("The sign-in attempt could not be canceled."))
        elif kind == "refresh":
            self._show_local_message(tr("Codex account status could not be refreshed."))
        if kind == "cancel":
            self._request_status()
        del message

    def _take_request(self, key: str, expected: str | None = None) -> tuple[str, int] | None:
        request = self._requests.pop(key, None)
        if request is None or (expected is not None and request[0] != expected):
            return None
        if request[1] != self._generation:
            return None
        return request

    def _apply_status(self, payload: object) -> None:
        safe_payload = project_account_auth_status(payload)
        self._render_status(safe_payload)
        login_state = safe_payload.get("login_state")
        if self._login_pending and login_state in {"idle", "failed", "cancelled"}:
            was_cancelled = login_state == "cancelled"
            self._forget_login()
            if safe_payload.get("state") == "signed_in" and login_state != "failed":
                self._window._request_usage(force=True)
                self._show_local_message(tr("Codex account sign-in was confirmed."))
            elif was_cancelled:
                self._show_local_message(tr("Codex sign-in was canceled."))
            else:
                self._show_local_message(
                    tr(
                        "Codex sign-in could not be confirmed. Check the account status "
                        "and try again."
                    )
                )

    def _apply_login_start(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            self._show_local_message(tr("The account sign-in response was not recognized."))
            return
        flow_type = payload.get("flow_type")
        login_id = payload.get("login_id")
        if (
            not isinstance(flow_type, str)
            or flow_type not in {"chatgpt", "chatgptDeviceCode"}
            or not isinstance(login_id, str)
            or not login_id.strip()
            or len(login_id) > 256
        ):
            self._show_local_message(tr("The account sign-in response was not recognized."))
            return
        completion_state = payload.get("login_state")
        if isinstance(completion_state, str) and completion_state in {"completed", "failed"}:
            self._login_pending = True
            self._poll_timer.setInterval(_LOGIN_POLL_MS)
            self._poll_timer.start()
            self._request_status()
            return
        url_field = "auth_url" if flow_type == "chatgpt" else "verification_url"
        url = payload.get(url_field)
        if not validate_auth_url(url, flow_type):
            self._show_local_message(tr("The account sign-in address was rejected for safety."))
            return
        code: str | None = None
        if flow_type == "chatgptDeviceCode":
            code = payload.get("user_code")
            if not isinstance(code, str) or not code.strip() or len(code) > 128:
                self._show_local_message(tr("The device sign-in response was not recognized."))
                return
        self._login_id = login_id
        self._login_pending = True
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
            login_state = payload.get("login_state")
            if login_state == "waiting":
                text += " · " + tr("Sign-in is waiting for the browser")
            elif login_state == "cancelling":
                text += " · " + tr("Canceling sign-in")
            elif login_state == "failed":
                text += " · " + tr("Sign-in did not complete")
        window.account_auth_status_label.setText(text)
        can_control = self._can_control()
        action_available = self._connected and can_control
        window.account_auth_refresh_button.setEnabled(action_available)
        can_start_login = action_available and self._login_id is None and not self._login_pending
        window.account_auth_login_button.setEnabled(can_start_login)
        window.account_auth_device_button.setEnabled(can_start_login)
        window.account_auth_cancel_button.setEnabled(
            action_available and self._login_id is not None
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
        if self._login_id is None:
            return
        self.cancel_login()
        self._show_local_message(
            tr("The sign-in attempt timed out. You can start again from Settings.")
        )

    def _forget_login(self) -> None:
        self._login_timeout.stop()
        self._login_id = None
        self._login_pending = False
        self._poll_timer.setInterval(_STATUS_POLL_MS)
        self._clear_secret_dialogs()

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
