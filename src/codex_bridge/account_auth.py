from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

_ACCOUNT_TYPES = frozenset({"apiKey", "chatgpt", "amazonBedrock", "gcp"})
_PLAN_TYPES = frozenset(
    {
        "free",
        "go",
        "plus",
        "pro",
        "prolite",
        "promax",
        "team",
        "self_serve_business_prolite",
        "self_serve_business_usage_based",
        "business",
        "ent26",
        "enterprise_cbp_automation",
        "enterprise_cbp_usage_based",
        "enterprise",
        "edu",
        "edu_plus",
        "edu_pro",
        "sci",
        "sci_plus",
        "unknown",
    }
)
_AUTH_MODES = frozenset(
    {
        "apikey",
        "chatgpt",
        "chatgptAuthTokens",
        "headers",
        "agentIdentity",
        "personalAccessToken",
        "bedrockApiKey",
        "bedrockAccessKeys",
    }
)
_UNKNOWN_STATUS: dict[str, object] = {
    "state": "unknown",
    "account_type": None,
    "auth_mode": None,
    "plan_type": None,
    "login_state": "idle",
    "issue": None,
}
_PUBLIC_STATES = frozenset({"unknown", "checking", "signed_in", "login_required", "not_required"})
_LOGIN_STATES = frozenset(
    {"idle", "starting", "waiting", "cancelling", "cancelled", "checking", "failed"}
)
_LOGIN_ISSUES = frozenset(
    {
        "login_start_failed",
        "unsupported_login_response",
        "login_failed",
        "login_not_found",
        "unsupported_cancel_response",
        "cancel_failed",
        "not_confirmed",
        "execution_auth_error",
    }
)


class AccountAuthError(RuntimeError):
    """A bounded, non-sensitive account authentication operation failure."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class AccountAuthRequester(Protocol):
    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


def project_account_read(payload: object) -> dict[str, object]:
    result: dict[str, object] = {
        "state": "unknown",
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "idle",
    }
    if not isinstance(payload, Mapping) or "account" not in payload:
        return result
    account = payload.get("account")
    requires_auth = payload.get("requiresOpenaiAuth")
    if account is None:
        if not isinstance(requires_auth, bool):
            return result
        result["state"] = "login_required" if requires_auth else "not_required"
        return result
    if not isinstance(account, Mapping):
        return result
    account_type = account.get("type")
    if not isinstance(account_type, str) or account_type not in _ACCOUNT_TYPES:
        return result
    plan_type = account.get("planType")
    if account_type == "chatgpt" and (
        not isinstance(plan_type, str) or plan_type not in _PLAN_TYPES
    ):
        return result
    if not isinstance(requires_auth, bool):
        return result
    result["state"] = "signed_in"
    result["account_type"] = account_type
    result["auth_mode"] = "chatgpt" if account_type == "chatgpt" else None
    result["plan_type"] = (
        plan_type if isinstance(plan_type, str) and plan_type in _PLAN_TYPES else None
    )
    return result


def project_account_auth_status(payload: object) -> dict[str, object]:
    """Return only bounded status enums suitable for the unauthenticated read route."""
    if not isinstance(payload, Mapping):
        return dict(_UNKNOWN_STATUS)
    defaults = dict(_UNKNOWN_STATUS)
    state = payload.get("state")
    if isinstance(state, str) and state in _PUBLIC_STATES:
        defaults["state"] = state
    account_type = payload.get("account_type")
    if isinstance(account_type, str) and account_type in _ACCOUNT_TYPES:
        defaults["account_type"] = account_type
    auth_mode = payload.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode in _AUTH_MODES:
        defaults["auth_mode"] = auth_mode
    plan_type = payload.get("plan_type")
    if isinstance(plan_type, str) and plan_type in _PLAN_TYPES:
        defaults["plan_type"] = plan_type
    login_state = payload.get("login_state")
    if isinstance(login_state, str) and login_state in _LOGIN_STATES:
        defaults["login_state"] = login_state
    issue = payload.get("issue")
    if isinstance(issue, str) and issue in _LOGIN_ISSUES:
        defaults["issue"] = issue
    return defaults


def classify_execution_error(error: object, *, turn_state: str | None = None) -> str:
    if turn_state in {"needs_approval", "approval_required"}:
        return "approval"
    if not isinstance(error, Mapping):
        return "unknown"
    info = error.get("codexErrorInfo")
    if info == "unauthorized":
        return "auth_check"
    if isinstance(info, str) and info in {"usageLimitExceeded", "rateLimitExceeded"}:
        return "quota"
    if isinstance(info, Mapping) and len(info) == 1:
        variant, details = next(iter(info.items()))
        if variant in {
            "httpConnectionFailed",
            "responseStreamConnectionFailed",
            "responseStreamDisconnected",
        }:
            if isinstance(details, Mapping) and details.get("httpStatusCode") == 429:
                return "quota"
            return "network"
    if error.get("httpStatusCode") == 429:
        return "quota"
    if isinstance(info, str) and info in {"serverOverloaded", "internalServerError"}:
        return "app_server"
    return "unknown"


def validate_auth_url(url: object, flow_type: str) -> bool:
    if not isinstance(url, str) or flow_type not in {"chatgpt", "chatgptDeviceCode"}:
        return False
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "auth.openai.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return False
    except ValueError:
        return False
    return parsed.path == ("/oauth/authorize" if flow_type == "chatgpt" else "/codex/device")


class AccountAuthService:
    """Small state owner for App Server account reads and explicit login attempts."""

    def __init__(self, request: AccountAuthRequester) -> None:
        self._request = request
        self._status = dict(_UNKNOWN_STATUS)
        self._generation = 0
        self._read_task: asyncio.Task[dict[str, object]] | None = None
        self._active_login: dict[str, str | bool] | None = None
        self._login_starting = False
        self._early_completions: dict[str, bool] = {}
        self._finished_login_ids: list[str] = []

    def status(self) -> dict[str, object]:
        return dict(self._status)

    async def refresh(self) -> dict[str, object]:
        task = self._read_task
        if task is not None and not task.done():
            return dict(await asyncio.shield(task))
        generation = self._generation
        self._status["state"] = "checking"
        task = asyncio.create_task(self._read_account(generation))
        self._read_task = task
        try:
            return dict(await asyncio.shield(task))
        finally:
            if self._read_task is task and task.done():
                self._read_task = None

    async def _read_account(self, generation: int) -> dict[str, object]:
        try:
            payload = await self._request("account/read", {})
            projected = project_account_read(payload)
        except Exception:
            projected = dict(_UNKNOWN_STATUS)
        if generation != self._generation:
            return self.status()
        login_state = self._status.get("login_state", "idle")
        issue = self._status.get("issue")
        if login_state == "checking":
            if projected.get("state") == "signed_in":
                login_state, issue = "idle", None
            else:
                login_state, issue = "failed", "not_confirmed"
        elif (
            projected.get("state") == "signed_in"
            and login_state == "failed"
            and issue in {"not_confirmed", "execution_auth_error"}
        ):
            login_state, issue = "idle", None
        projected["login_state"] = login_state
        projected["issue"] = issue
        self._status = projected
        return self.status()

    async def start_login(self, flow_type: str) -> dict[str, str]:
        if flow_type not in {"chatgpt", "chatgptDeviceCode"}:
            raise AccountAuthError("unsupported_login_flow")
        if self._active_login is not None or self._login_starting:
            raise AccountAuthError("login_in_progress")
        generation = self._generation
        self._login_starting = True
        self._status["login_state"] = "starting"
        self._status["issue"] = None
        try:
            response = await self._request("account/login/start", {"type": flow_type})
        except Exception:
            if generation != self._generation:
                self._login_starting = False
                raise AccountAuthError("connection_lost") from None
            self._login_failed("login_start_failed")
            raise AccountAuthError("login_start_failed") from None
        if generation != self._generation:
            self._login_starting = False
            raise AccountAuthError("connection_lost")
        if not isinstance(response, Mapping):
            self._login_failed("unsupported_login_response")
            raise AccountAuthError("unsupported_login_response")
        login_id = response.get("loginId")
        response_type = response.get("type")
        if (
            not isinstance(login_id, str)
            or not login_id.strip()
            or len(login_id) > 256
            or response_type != flow_type
        ):
            self._login_failed("unsupported_login_response")
            raise AccountAuthError("unsupported_login_response")
        if login_id in self._finished_login_ids:
            self._finished_login_ids.remove(login_id)
        result: dict[str, str] = {"loginId": login_id, "type": flow_type}
        self._active_login = {
            "login_id": login_id,
            "flow_type": flow_type,
            "cancel_requested": False,
        }
        completed = self._early_completions.pop(login_id, None)
        if completed is not None:
            self._login_starting = False
            await self._complete_login(login_id, completed)
            result["login_state"] = "completed" if completed else "failed"
            return result
        url_field = "authUrl" if flow_type == "chatgpt" else "verificationUrl"
        url = response.get(url_field)
        if not isinstance(url, str) or not validate_auth_url(url, flow_type):
            self._active_login = None
            await self._cancel_invalid_login(login_id)
            self._remember_finished_login(login_id)
            self._login_failed("unsupported_login_response")
            raise AccountAuthError("unsupported_login_response")
        result[url_field] = url
        if flow_type == "chatgptDeviceCode":
            code = response.get("userCode")
            if not isinstance(code, str) or not code.strip() or len(code) > 128:
                self._active_login = None
                await self._cancel_invalid_login(login_id)
                self._remember_finished_login(login_id)
                self._login_failed("unsupported_login_response")
                raise AccountAuthError("unsupported_login_response")
            result["userCode"] = code
        self._login_starting = False
        self._status["login_state"] = "waiting"
        self._status["issue"] = None
        return result

    async def _cancel_invalid_login(self, login_id: str) -> None:
        try:
            await self._request("account/login/cancel", {"loginId": login_id})
        except Exception:
            return

    async def cancel_login(self, login_id: str) -> dict[str, object]:
        active = self._active_login
        if active is None or login_id != active.get("login_id"):
            raise AccountAuthError("login_not_active")
        active["cancel_requested"] = True
        self._status["login_state"] = "cancelling"
        try:
            response = await self._request("account/login/cancel", {"loginId": login_id})
        except Exception:
            if self._active_login is active:
                active["cancel_requested"] = False
                self._status["login_state"] = "waiting"
                self._status["issue"] = "cancel_failed"
                raise AccountAuthError("cancel_failed") from None
            return self.status()
        if self._active_login is not active:
            return self.status()
        result = response.get("status") if isinstance(response, Mapping) else None
        self._active_login = None
        self._remember_finished_login(login_id)
        if result == "Canceled":
            self._status["login_state"] = "cancelled"
            self._status["issue"] = None
        elif result == "NotFound":
            self._status["login_state"] = "failed"
            self._status["issue"] = "login_not_found"
        else:
            self._status["login_state"] = "failed"
            self._status["issue"] = "unsupported_cancel_response"
        return self.status()

    def on_notification(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        if method == "account/updated":
            self._schedule_refresh()
            return
        if method != "account/login/completed":
            return
        login_id = params.get("loginId")
        success = params.get("success")
        if not isinstance(login_id, str) or not isinstance(success, bool):
            return
        if login_id in self._finished_login_ids:
            return
        active = self._active_login
        if active is None:
            self._early_completions[login_id] = success
            if len(self._early_completions) > 8:
                self._early_completions.pop(next(iter(self._early_completions)))
            return
        if login_id == active.get("login_id"):
            self._active_login = None
            self._remember_finished_login(login_id)
            if success:
                self._status["login_state"] = "checking"
                self._status["issue"] = None
            else:
                self._status["login_state"] = "failed"
                self._status["issue"] = "login_failed"
            self._schedule_refresh()

    async def _complete_login(self, login_id: str, success: bool) -> None:
        active = self._active_login
        if active is None or active.get("login_id") != login_id:
            return
        self._active_login = None
        self._remember_finished_login(login_id)
        if success:
            self._status["login_state"] = "checking"
            self._status["issue"] = None
        else:
            self._status["login_state"] = "failed"
            self._status["issue"] = "login_failed"
        await self.refresh()

    def _remember_finished_login(self, login_id: str) -> None:
        if login_id in self._finished_login_ids:
            return
        self._finished_login_ids.append(login_id)
        if len(self._finished_login_ids) > 16:
            self._finished_login_ids.pop(0)

    def observe_execution_error(self, error: object, *, turn_state: str | None = None) -> str:
        category = classify_execution_error(error, turn_state=turn_state)
        if category == "auth_check":
            self._status["login_state"] = "checking"
            self._status["issue"] = "execution_auth_error"
            self._schedule_refresh()
        return category

    def disconnect(self) -> None:
        self._generation += 1
        self._read_task = None
        self._active_login = None
        self._login_starting = False
        self._early_completions.clear()
        self._status = dict(_UNKNOWN_STATUS)

    def _schedule_refresh(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self.refresh())
        except RuntimeError:
            self._status["state"] = "unknown"

    def _login_failed(self, issue: str) -> None:
        self._active_login = None
        self._login_starting = False
        self._status["login_state"] = "failed"
        self._status["issue"] = issue
