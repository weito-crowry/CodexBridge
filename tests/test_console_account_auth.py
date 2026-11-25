from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
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
        self.accept_posts = True

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
        if not self.accept_posts:
            return False
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
    if isinstance(current, QApplication):
        return current
    return QApplication([])


@pytest.fixture(autouse=True)
def _block_system_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempted: list[bool] = []

    def blocked(_url: QUrl) -> bool:
        attempted.append(True)
        return False

    monkeypatch.setattr(QDesktopServices, "openUrl", blocked)
    yield
    assert not attempted, "A test attempted to use the system browser."


def _fake_controller(
    window: FakeWindow, *, opened: list[QUrl] | None = None
) -> AccountAuthController:
    def open_fake(url: QUrl) -> bool:
        if opened is not None:
            opened.append(url)
        return True

    return AccountAuthController(window, browser_opener=open_fake)


def test_account_auth_controller_keeps_status_private_and_uses_control_route() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = _fake_controller(window, opened=opened)
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
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
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
    controller = _fake_controller(window, opened=opened)
    controller.on_connection_changed(True)
    controller.start_device_code_login()
    _, _, _, key = window._client.posts[-1]
    controller._on_control_success(
        key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgptDeviceCode",
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
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
    controller = _fake_controller(window, opened=opened)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    _, _, _, unsafe_key = window._client.posts[-1]
    controller._on_control_success(
        unsafe_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com@evil.example/oauth/authorize?PRIVATE=1",
        },
    )
    assert not opened
    cancel_key = window._client.posts[-1][3]
    assert window._client.posts[-1][0].endswith("/login/cancel")
    controller._on_control_success(
        cancel_key,
        {
            "state": "login_required",
            "login_state": "cancelled",
            "login_result": "cancelled",
            "login_revision": 1,
            "revision": 3,
        },
    )

    controller.start_chatgpt_login()
    _, _, _, stale_key = window._client.posts[-1]
    controller.on_connection_changed(False)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
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
    controller = _fake_controller(window)
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


def test_unsent_start_releases_controls_and_allows_explicit_retry() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    previous_post_count = len(window._client.posts)
    window._client.accept_posts = False

    controller.start_chatgpt_login()

    assert len(window._client.posts) == previous_post_count
    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    assert window.account_auth_device_button.isEnabled()
    controller.close()
    window.deleteLater()


def test_lost_start_response_rechecks_even_when_status_dialog_is_closed() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    _, _, _, initial_refresh_key = window._client.posts[-1]
    window._client.composer_json_succeeded.emit(
        initial_refresh_key,
        {
            "state": "login_required",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "revision": 1,
        },
    )

    controller.start_chatgpt_login()
    start_path, _, _, start_key = window._client.posts[-1]
    assert start_path.endswith("/login/start")
    controller._on_control_failure(start_key, "network_error")

    assert not window.status_dialog.isVisible()
    assert controller._login_pending
    assert controller._poll_timer.isActive()
    assert not window.account_auth_cancel_button.isEnabled()
    assert not any(path.endswith("/login/start") for path, *_ in window._client.posts[2:])
    status_key = window._client.gets[-1][1]
    window._client.json_succeeded.emit(
        status_key,
        {
            "state": "login_required",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "revision": 2,
        },
    )
    assert controller._login_pending

    refresh_path, _, _, refresh_key = window._client.posts[-1]
    assert refresh_path.endswith("/account/auth/refresh")
    window._client.composer_json_succeeded.emit(
        refresh_key,
        {
            "state": "login_required",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "revision": 3,
        },
    )
    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    assert window.account_auth_device_button.isEnabled()
    assert not any(path.endswith("/login/start") for path, *_ in window._client.posts[2:])
    controller.close()
    window.deleteLater()


def test_malformed_start_response_rechecks_before_releasing_retry() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    _, _, _, initial_refresh_key = window._client.posts[-1]
    window._client.composer_json_succeeded.emit(
        initial_refresh_key,
        {
            "state": "login_required",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "revision": 1,
        },
    )
    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(start_key, {"unexpected": "shape"})

    assert controller._login_pending
    assert controller._poll_timer.isActive()
    refresh_path, _, _, refresh_key = window._client.posts[-1]
    assert refresh_path.endswith("/account/auth/refresh")
    window._client.composer_json_succeeded.emit(
        refresh_key,
        {
            "state": "login_required",
            "login_state": "idle",
            "login_result": "none",
            "login_revision": 0,
            "revision": 2,
        },
    )
    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    controller.close()
    window.deleteLater()


def test_account_auth_completion_refreshes_status_and_usage_without_opening_browser() -> None:
    _application()
    window = FakeWindow()
    opened: list[QUrl] = []
    controller = _fake_controller(window, opened=opened)
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
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 1,
            "revision": 2,
            "state": "checking",
            "login_state": "checking",
            "login_result": "none",
            "auth_warning": "none",
        },
    )
    assert not opened
    controller._poll_status()
    status_path, status_key = window._client.gets[-1]
    assert status_path == "/ui-api/account/auth"
    window._client.json_succeeded.emit(
        status_key,
        {
            "state": "signed_in",
            "account_type": "chatgpt",
            "plan_type": "plus",
            "login_state": "succeeded",
            "login_result": "succeeded",
            "login_revision": 1,
            "revision": 3,
        },
    )
    assert window.usage_refreshes == 1
    assert "sign-in was confirmed" in controller._dialogs[-1].text().lower()
    controller.close()
    window.deleteLater()


def test_cancelled_attempt_with_existing_signed_in_account_is_not_success() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    assert not window.account_auth_login_button.isEnabled()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 1,
            "revision": 3,
            "state": "signed_in",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller.cancel_login()
    _path, _payload, _token, cancel_key = window._client.posts[-1]
    controller._on_control_success(
        cancel_key,
        {
            "state": "signed_in",
            "login_state": "cancelled",
            "login_result": "cancelled",
            "login_revision": 1,
            "revision": 5,
            "auth_warning": "none",
        },
    )

    assert window.usage_refreshes == 0
    assert "canceled" in controller._dialogs[-1].text().lower()
    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    assert window.account_auth_device_button.isEnabled()
    controller.close()
    window.deleteLater()


def test_status_started_before_new_login_cannot_complete_that_attempt() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller._request_status()
    _path, stale_status_key = window._client.gets[-1]

    controller.start_chatgpt_login()
    assert not window.account_auth_login_button.isEnabled()
    window._client.json_succeeded.emit(
        stale_status_key,
        {
            "state": "signed_in",
            "login_state": "succeeded",
            "login_result": "succeeded",
            "login_revision": 1,
            "revision": 99,
        },
    )
    assert controller._login_pending
    assert window.usage_refreshes == 0

    _path, _payload, _token, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 2,
            "revision": 100,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=current",
        },
    )
    assert controller._login_pending
    assert window.usage_refreshes == 0
    assert not window.account_auth_login_button.isEnabled()
    controller.close()
    window.deleteLater()


def test_cancel_failure_keeps_attempt_unresolved_and_allows_recancel() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    _path, _payload, _token, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 7,
            "revision": 10,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller._on_login_timeout()
    assert "start again" not in controller._dialogs[-1].text().lower()
    first_cancel_path, first_cancel_payload, _, first_cancel_key = window._client.posts[-1]
    assert first_cancel_path == "/ui-api/control/account/auth/login/cancel"
    assert first_cancel_payload == {"login_id": "PRIVATE-LOGIN-ID", "login_revision": 7}

    controller._on_control_failure(first_cancel_key, "request failed")
    assert controller._login_pending
    assert controller._login_id == "PRIVATE-LOGIN-ID"
    assert window.account_auth_cancel_button.isEnabled()
    assert not window.account_auth_login_button.isEnabled()

    controller.cancel_login()
    second_cancel_path, second_cancel_payload, _, second_cancel_key = window._client.posts[-1]
    assert second_cancel_path == first_cancel_path
    assert second_cancel_payload == first_cancel_payload
    controller._on_control_success(
        second_cancel_key,
        {
            "state": "signed_in",
            "login_state": "failed",
            "login_result": "failed",
            "login_revision": 7,
            "revision": 14,
            "issue": "login_not_found",
            "auth_warning": "none",
        },
    )
    assert window.usage_refreshes == 0
    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    controller.close()
    window.deleteLater()


def test_account_auth_warning_is_visible_without_claiming_expiry() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller._apply_status(
        {
            "state": "signed_in",
            "account_type": "chatgpt",
            "plan_type": "plus",
            "login_state": "idle",
            "login_result": "none",
            "auth_warning": "execution_auth_error",
            "revision": 8,
            "login_revision": 0,
        }
    )
    label = window.account_auth_status_label.text().lower()
    assert "account recognized" in label
    assert "authentication error" in label
    assert window.usage_refreshes == 0
    controller.close()
    window.deleteLater()


def test_auth_recovery_read_failure_is_terminal_and_allows_retry() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller._apply_status(
        {
            "state": "unknown",
            "login_state": "failed",
            "login_result": "failed",
            "login_revision": 1,
            "issue": "not_confirmed",
            "auth_warning": "execution_auth_error",
            "revision": 4,
        }
    )

    assert not controller._login_pending
    assert window.account_auth_login_button.isEnabled()
    assert window.account_auth_device_button.isEnabled()
    assert window.usage_refreshes == 0
    assert "authentication error" in window.account_auth_status_label.text().lower()
    controller.close()
    window.deleteLater()


def test_cancel_completion_race_keeps_polling_until_status_is_confirmed() -> None:
    _application()
    window = FakeWindow()
    controller = _fake_controller(window)
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
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller.cancel_login()
    cancel_path, cancel_payload, _token, cancel_key = window._client.posts[-1]
    assert cancel_path == "/ui-api/control/account/auth/login/cancel"
    assert cancel_payload == {"login_id": "PRIVATE-LOGIN-ID", "login_revision": 1}

    controller._on_control_success(
        cancel_key,
        {
            "state": "login_required",
            "login_state": "checking",
            "login_result": "none",
            "login_revision": 1,
            "revision": 3,
        },
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
            "login_state": "succeeded",
            "login_result": "succeeded",
            "login_revision": 1,
            "revision": 4,
            "auth_warning": "none",
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
    controller = _fake_controller(window)
    controller.on_connection_changed(True)
    controller.start_chatgpt_login()
    _, _, _, start_key = window._client.posts[-1]
    controller._on_control_success(
        start_key,
        {
            "login_id": "PRIVATE-LOGIN-ID",
            "flow_type": "chatgpt",
            "login_revision": 1,
            "revision": 2,
            "state": "login_required",
            "login_state": "waiting",
            "login_result": "none",
            "auth_warning": "none",
            "auth_url": "https://auth.openai.com/oauth/authorize?state=temporary",
        },
    )
    controller._on_login_timeout()
    path, payload, token, _key = window._client.posts[-1]
    assert path == "/ui-api/control/account/auth/login/cancel"
    assert payload == {"login_id": "PRIVATE-LOGIN-ID", "login_revision": 1}
    assert token == "control-secret"
    assert "PRIVATE-LOGIN-ID" not in controller._dialogs[-1].text()
    controller.close()
    window.deleteLater()
