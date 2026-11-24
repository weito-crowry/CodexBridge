from __future__ import annotations

from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget

from codex_bridge.console.account_auth_controller import AccountAuthController


class Signal:
    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def connect(self, handler: Any) -> None:
        self._handlers.append(handler)

    def emit(self, *args: object) -> None:
        for handler in tuple(self._handlers):
            handler(*args)


class FakeClient:
    def __init__(self) -> None:
        self.json_succeeded = Signal()
        self.json_failed = Signal()
        self.composer_json_succeeded = Signal()
        self.composer_json_failed = Signal()
        self.gets: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict[str, object], str, str]] = []

    def get_json(self, path: str, *, key: str) -> bool:
        self.gets.append((path, key))
        return True

    def post_control_json(
        self,
        path: str,
        payload: dict[str, object],
        token: str,
        *,
        key: str,
    ) -> bool:
        self.posts.append((path, payload, token, key))
        return True


class FakeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._client = FakeClient()
        self._control_token = "control-secret"
        self.connected = True
        self.status_dialog = QDialog(self)
        self.account_auth_status_label = QLabel(self)
        self.account_auth_refresh_button = QPushButton(self)
        self.account_auth_login_button = QPushButton(self)
        self.account_auth_device_button = QPushButton(self)
        self.account_auth_cancel_button = QPushButton(self)
        self.usage_refreshes = 0

    def _can_display_control_token(self) -> bool:
        return self.connected and self._control_token is not None

    def _request_usage(self, *, force: bool = False) -> None:
        if force:
            self.usage_refreshes += 1


def _application() -> QApplication:
    current = QApplication.instance()
    return current if isinstance(current, QApplication) else QApplication([])


def test_account_auth_controller_keeps_status_private_and_uses_control_route() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = AccountAuthController(
        window, browser_opener=lambda url: opened.append(url) is None
    )
    controller.on_connection_changed(True)
    refresh_path, refresh_payload, refresh_token, refresh_key = window._client.posts[-1]
    assert refresh_path == "/ui-api/control/account/auth/refresh"
    assert refresh_payload == {}
    assert refresh_token == "control-secret"
    window._client.composer_json_succeeded.emit(
        refresh_key,
        {
            "state": "signed_in",
            "account_type": "chatgpt",
            "plan_type": "plus",
            "email": "PRIVATE-EMAIL",
            "user_id": "PRIVATE-USER",
        },
    )
    assert "Account recognized" in window.account_auth_status_label.text()
    assert "does not guarantee online access" in window.account_auth_status_label.text()
    assert "PRIVATE" not in window.account_auth_status_label.text()

    controller.start_chatgpt_login()
    path, payload, token, key = window._client.posts[-1]
    assert path == "/ui-api/control/account/auth/login/start"
    assert payload == {"flow_type": "chatgpt"}
    assert token == "control-secret"
    window._client.composer_json_succeeded.emit(
        key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=PRIVATE-URL",
        },
    )
    assert [url.toString() for url in opened] == [
        "https://auth.openai.com/oauth/authorize?state=PRIVATE-URL"
    ]
    assert "PRIVATE" not in window.account_auth_status_label.text()
    assert "PRIVATE-LOGIN-ID" not in repr(window._client.gets)
    controller.close()
    window.deleteLater()


def test_account_auth_controller_shows_device_code_only_in_local_dialog() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = AccountAuthController(
        window, browser_opener=lambda url: opened.append(url) is None
    )
    controller.start_device_code_login()
    _, _, _, key = window._client.posts[-1]
    controller._on_control_success(
        key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgptDeviceCode",
            "verification_url": "https://auth.openai.com/codex/device",
            "user_code": "PRIVATE-DEVICE-CODE",
        },
    )

    assert [url.toString() for url in opened] == ["https://auth.openai.com/codex/device"]
    assert "PRIVATE-DEVICE-CODE" in controller._dialogs[-1].text()
    assert "PRIVATE-DEVICE-CODE" not in window.account_auth_status_label.text()
    assert all("PRIVATE-DEVICE-CODE" not in repr(row) for row in window._client.posts)
    controller.close()
    window.deleteLater()


def test_account_auth_controller_rejects_unsafe_url_and_stale_start_response() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = AccountAuthController(
        window, browser_opener=lambda url: opened.append(url) is None
    )
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    _, _, _, unsafe_key = window._client.posts[-1]
    controller._on_control_success(
        unsafe_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "auth_url": "https://auth.openai.com@evil.example/oauth/authorize?PRIVATE=1",
        },
    )
    assert not opened

    controller.start_chatgpt_login()
    _, _, _, stale_key = window._client.posts[-1]
    controller.on_connection_changed(False)
    controller._on_control_success(
        stale_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=STALE",
        },
    )
    assert not opened
    assert "PRIVATE" not in window.account_auth_status_label.text()
    controller.close()
    window.deleteLater()


def test_account_auth_status_is_visible_without_token_but_controls_are_disabled() -> None:
    _application()
    window = FakeWindow()
    window._control_token = None
    controller = AccountAuthController(window)
    controller.on_connection_changed(True)
    path, key = window._client.gets[-1]
    assert path == "/ui-api/account/auth"
    window._client.json_succeeded.emit(
        key,
        {
            "state": "login_required",
            "account_type": None,
            "plan_type": None,
            "login_id": "PRIVATE-LOGIN-ID",
        },
    )
    assert "Sign-in required" in window.account_auth_status_label.text()
    assert "PRIVATE" not in window.account_auth_status_label.text()
    assert not window.account_auth_login_button.isEnabled()
    assert "control token" in window.account_auth_login_button.toolTip()
    controller.close()
    window.deleteLater()


def test_account_auth_completion_refreshes_status_and_usage_without_opening_browser() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = AccountAuthController(
        window, browser_opener=lambda url: opened.append(url) is None
    )
    controller.on_connection_changed(True)
    _, _, _, refresh_key = window._client.posts[-1]
    window._client.composer_json_succeeded.emit(
        refresh_key,
        {"state": "login_required", "login_state": "idle"},
    )
    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    window._client.composer_json_succeeded.emit(
        start_key,
        {"login_id": "PRIVATE-LOGIN-ID", "flow_type": "chatgpt", "login_state": "completed"},
    )
    assert not opened
    status_path, status_key = window._client.gets[-1]
    assert status_path == "/ui-api/account/auth"
    window._client.json_succeeded.emit(
        status_key,
        {
            "state": "signed_in",
            "account_type": "chatgpt",
            "plan_type": "plus",
            "login_state": "idle",
        },
    )
    assert window.usage_refreshes == 1
    assert "sign-in was confirmed" in controller._dialogs[-1].text().lower()
    controller.close()
    window.deleteLater()


def test_cancel_completion_race_keeps_polling_until_status_is_confirmed() -> None:
    _application()
    window = FakeWindow()
    controller = AccountAuthController(window, browser_opener=lambda _url: True)
    controller.on_connection_changed(True)
    _, _, _, refresh_key = window._client.posts[-1]
    window._client.composer_json_succeeded.emit(
        refresh_key,
        {"state": "login_required", "login_state": "idle"},
    )

    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller.cancel_login()
    cancel_path, cancel_payload, _token, cancel_key = window._client.posts[-1]
    assert cancel_path == "/ui-api/control/account/auth/login/cancel"
    assert cancel_payload == {"login_id": "PRIVATE-LOGIN-ID"}

    controller._on_control_success(
        cancel_key,
        {"state": "login_required", "login_state": "checking"},
    )
    assert controller._login_pending
    assert controller._login_id is None
    assert controller._poll_timer.isActive()
    assert not window.account_auth_login_button.isEnabled()

    controller._poll_status()
    status_path, status_key = window._client.gets[-1]
    assert status_path == "/ui-api/account/auth"
    window._client.json_succeeded.emit(
        status_key,
        {
            "state": "signed_in",
            "account_type": "chatgpt",
            "plan_type": "plus",
            "login_state": "idle",
        },
    )
    assert not controller._login_pending
    assert window.usage_refreshes == 1
    assert "sign-in was confirmed" in controller._dialogs[-1].text().lower()
    controller.close()
    window.deleteLater()


def test_account_auth_timeout_sends_only_correlated_cancel() -> None:
    _application()
    window = FakeWindow()
    controller = AccountAuthController(window, browser_opener=lambda _url: True)
    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller._on_login_timeout()
    path, payload, token, _key = window._client.posts[-1]
    assert path == "/ui-api/control/account/auth/login/cancel"
    assert payload == {"login_id": "PRIVATE-LOGIN-ID"}
    assert token == "control-secret"
    assert "PRIVATE-LOGIN-ID" not in controller._dialogs[-1].text()
    controller.close()
    window.deleteLater()
