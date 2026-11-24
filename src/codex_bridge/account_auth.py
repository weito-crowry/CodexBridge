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
    "login_result": "none",
    "login_revision": 0,
    "auth_warning": "none",
    "revision": 0,
    "issue": None,
}
_PUBLIC_STATES = frozenset({"unknown", "checking", "signed_in", "login_required", "not_required"})
_LOGIN_STATES = frozenset(
    {
        "idle",
        "starting",
        "waiting",
        "cancelling",
        "cancelled",
        "checking",
        "succeeded",
        "failed",
    }
)
_LOGIN_RESULTS = frozenset({"none", "succeeded", "failed", "cancelled"})
_AUTH_WARNINGS = frozenset({"none", "execution_auth_error"})
_LOGIN_ISSUES = frozenset(
    {
        "login_start_failed",
        "unsupported_login_response",
        "login_failed",
        "login_not_found",
        "unsupported_cancel_response",
        "cancel_failed",
        "cancel_unknown",
        "not_confirmed",
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
    login_result = payload.get("login_result")
    if isinstance(login_result, str) and login_result in _LOGIN_RESULTS:
        defaults["login_result"] = login_result
    auth_warning = payload.get("auth_warning")
    if isinstance(auth_warning, str) and auth_warning in _AUTH_WARNINGS:
        defaults["auth_warning"] = auth_warning
    for revision_name in ("revision", "login_revision"):
        revision = payload.get(revision_name)
        if type(revision) is int and 0 <= revision <= 2**53 - 1:
            defaults[revision_name] = revision
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
        self._read_epoch = 0
        self._read_tasks: dict[tuple[int, int], asyncio.Task[dict[str, object]]] = {}
        self._refresh_dispatch_id = 0
        self._scheduled_refresh_owner: tuple[int, int] | None = None
        self._state_revision = 0
        self._login_revision = 0
        self._start_owner: tuple[int, int] | None = None
        self._active_login: dict[str, object] | None = None
        self._early_completions: dict[tuple[int, int], dict[str, bool]] = {}
        self._finished_login_ids: list[str] = []
        self._auth_recovery_login_revision: int | None = None

    def status(self) -> dict[str, object]:
        return dict(self._status)

    def _touch(self) -> None:
        self._state_revision += 1
        self._status["revision"] = self._state_revision

    async def refresh(self) -> dict[str, object]:
        generation = self._generation
        epoch = self._read_epoch
        key = (generation, epoch)
        task = self._read_tasks.get(key)
        if task is None or task.done():
            self._status["state"] = "checking"
            self._touch()
            task = asyncio.create_task(self._read_account(generation, epoch))
            self._read_tasks[key] = task
        try:
            return dict(await asyncio.shield(task))
        finally:
            if self._read_tasks.get(key) is task and task.done():
                self._read_tasks.pop(key, None)

    async def _read_account(self, generation: int, epoch: int) -> dict[str, object]:
        try:
            payload = await self._request("account/read", {})
            projected = project_account_read(payload)
            read_succeeded = True
        except Exception:
            projected = {"state": "unknown"}
            read_succeeded = False
        if generation != self._generation or epoch != self._read_epoch:
            return self.status()
        for field in ("state", "account_type", "auth_mode", "plan_type"):
            self._status[field] = projected.get(field)
        if self._auth_recovery_login_revision == self._login_revision:
            if read_succeeded and projected.get("state") == "signed_in":
                self._status["login_state"] = "succeeded"
                self._status["login_result"] = "succeeded"
                self._status["issue"] = None
                self._status["auth_warning"] = "none"
            else:
                self._status["login_state"] = "failed"
                self._status["login_result"] = "failed"
                self._status["issue"] = "not_confirmed"
            self._auth_recovery_login_revision = None
        self._touch()
        return self.status()

    async def start_login(self, flow_type: str) -> dict[str, object]:
        if flow_type not in {"chatgpt", "chatgptDeviceCode"}:
            raise AccountAuthError("unsupported_login_flow")
        if self._active_login is not None or self._start_owner is not None:
            raise AccountAuthError("login_in_progress")
        generation = self._generation
        self._login_revision += 1
        login_revision = self._login_revision
        owner = (generation, login_revision)
        self._start_owner = owner
        self._early_completions.pop(owner, None)
        self._auth_recovery_login_revision = None
        self._status["login_state"] = "starting"
        self._status["login_result"] = "none"
        self._status["login_revision"] = login_revision
        self._status["issue"] = None
        self._touch()
        try:
            response = await self._request("account/login/start", {"type": flow_type})
        except Exception:
            if self._start_owner != owner:
                raise AccountAuthError("connection_lost") from None
            self._start_owner = None
            self._early_completions.pop(owner, None)
            self._login_failed("login_start_failed")
            raise AccountAuthError("login_start_failed") from None
        if self._start_owner != owner:
            raise AccountAuthError("connection_lost") from None
        self._start_owner = None
        if not isinstance(response, Mapping):
            self._early_completions.pop(owner, None)
            self._login_failed("unsupported_login_response")
            raise AccountAuthError("unsupported_login_response")
        login_id = response.get("loginId")
        response_type = response.get("type")
        if not isinstance(login_id, str) or not login_id.strip() or len(login_id) > 256:
            self._early_completions.pop(owner, None)
            self._login_failed("unsupported_login_response")
            raise AccountAuthError("unsupported_login_response")
        if login_id in self._finished_login_ids:
            self._finished_login_ids.remove(login_id)
        self._active_login = {
            "login_id": login_id,
            "flow_type": flow_type,
            "login_revision": login_revision,
            "generation": generation,
            "cancel_requested": False,
        }
        early = self._early_completions.pop(owner, {})
        completed = early.get(login_id) if response_type == flow_type else None
        if response_type != flow_type:
            result = self._start_result(login_id, flow_type, login_revision)
            result["start_issue"] = "unsupported_login_response"
            self._set_login_waiting("unsupported_login_response")
            await self._cancel_invalid_login(login_id, login_revision)
            result.update(self.status())
            return result
        if completed is not None:
            await self._complete_login(login_id, login_revision, completed)
            result = self._start_result(login_id, flow_type, login_revision)
            result.update(self.status())
            return result
        url_field = "authUrl" if flow_type == "chatgpt" else "verificationUrl"
        url = response.get(url_field)
        invalid = not isinstance(url, str) or not validate_auth_url(url, flow_type)
        result = self._start_result(login_id, flow_type, login_revision)
        if invalid:
            result["start_issue"] = "unsupported_login_response"
            self._set_login_waiting("unsupported_login_response")
            await self._cancel_invalid_login(login_id, login_revision)
            result.update(self.status())
            return result
        result[url_field] = url
        if flow_type == "chatgptDeviceCode":
            code = response.get("userCode")
            if not isinstance(code, str) or not code.strip() or len(code) > 128:
                result.pop(url_field, None)
                result["start_issue"] = "unsupported_login_response"
                self._set_login_waiting("unsupported_login_response")
                await self._cancel_invalid_login(login_id, login_revision)
                result.update(self.status())
                return result
            result["userCode"] = code
        self._set_login_waiting(None)
        result.update(self.status())
        return result

    def _start_result(
        self, login_id: str, flow_type: str, login_revision: int
    ) -> dict[str, object]:
        return {
            "loginId": login_id,
            "type": flow_type,
            "login_revision": login_revision,
        }

    def _set_login_waiting(self, issue: str | None) -> None:
        self._status["login_state"] = "waiting"
        self._status["login_result"] = "none"
        self._status["issue"] = issue
        self._touch()

    async def _cancel_invalid_login(self, login_id: str, login_revision: int) -> None:
        try:
            await self.cancel_login(login_id, login_revision)
        except AccountAuthError:
            return

    async def cancel_login(self, login_id: str | None, login_revision: int) -> dict[str, object]:
        active = self._active_login
        if (
            active is None
            or type(login_revision) is not int
            or login_revision != active.get("login_revision")
            or (login_id is not None and login_id != active.get("login_id"))
        ):
            raise AccountAuthError("login_not_active")
        active["cancel_requested"] = True
        self._status["login_state"] = "cancelling"
        self._status["issue"] = None
        self._touch()
        try:
            response = await self._request("account/login/cancel", {"loginId": active["login_id"]})
        except Exception:
            if self._active_login is active:
                active["cancel_requested"] = False
                self._status["login_state"] = "waiting"
                self._status["issue"] = "cancel_failed"
                self._touch()
                raise AccountAuthError("cancel_failed") from None
            return self.status()
        if self._active_login is not active:
            return self.status()
        result = response.get("status") if isinstance(response, Mapping) else None
        if result == "canceled":
            self._active_login = None
            self._remember_finished_login(str(active["login_id"]))
            self._status["login_state"] = "cancelled"
            self._status["login_result"] = "cancelled"
            self._status["issue"] = None
        elif result == "notFound":
            self._active_login = None
            self._remember_finished_login(str(active["login_id"]))
            self._status["login_state"] = "failed"
            self._status["login_result"] = "failed"
            self._status["issue"] = "login_not_found"
        else:
            active["cancel_requested"] = False
            self._status["login_state"] = "waiting"
            self._status["issue"] = "cancel_unknown"
            self._touch()
            raise AccountAuthError("unsupported_cancel_response")
        self._touch()
        self._invalidate_and_schedule_read()
        return self.status()

    def on_notification(self, message: Mapping[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, Mapping):
            return
        if method == "account/updated":
            self._invalidate_and_schedule_read()
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
            owner = self._start_owner
            if owner is not None:
                self._early_completions.setdefault(owner, {})[login_id] = success
                early = self._early_completions[owner]
                while len(early) > 8:
                    early.pop(next(iter(early)))
            return
        if login_id == active.get("login_id"):
            self._finish_login_completion(active, success)

    def _finish_login_completion(self, active: dict[str, object], success: bool) -> None:
        login_id = active.get("login_id")
        login_revision = active.get("login_revision")
        if (
            not isinstance(login_id, str)
            or type(login_revision) is not int
            or self._active_login is not active
        ):
            return
        self._active_login = None
        self._remember_finished_login(login_id)
        if success:
            self._status["login_state"] = "checking"
            self._status["login_result"] = "none"
            self._status["issue"] = None
            self._auth_recovery_login_revision = login_revision
        else:
            self._status["login_state"] = "failed"
            self._status["login_result"] = "failed"
            self._status["issue"] = "login_failed"
            self._auth_recovery_login_revision = None
        self._touch()
        self._invalidate_and_schedule_read()

    async def _complete_login(self, login_id: str, login_revision: int, success: bool) -> None:
        active = self._active_login
        if (
            active is None
            or active.get("login_id") != login_id
            or active.get("login_revision") != login_revision
        ):
            return
        self._finish_login_completion(active, success)
        owner = self._scheduled_refresh_owner
        if owner is not None and owner[0] == self._generation:
            self._scheduled_refresh_owner = None
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
            self._status["auth_warning"] = "execution_auth_error"
            self._auth_recovery_login_revision = None
            self._touch()
            self._invalidate_and_schedule_read()
        return category

    def disconnect(self) -> None:
        self._generation += 1
        self._read_epoch += 1
        self._read_tasks.clear()
        self._refresh_dispatch_id += 1
        self._scheduled_refresh_owner = None
        auth_warning = self._status.get("auth_warning", "none")
        self._active_login = None
        self._start_owner = None
        self._early_completions.clear()
        self._finished_login_ids.clear()
        self._auth_recovery_login_revision = None
        self._status = dict(_UNKNOWN_STATUS)
        self._status["auth_warning"] = auth_warning
        self._status["login_revision"] = self._login_revision
        self._touch()

    def _invalidate_and_schedule_read(self) -> None:
        self._read_epoch += 1
        self._status["state"] = "checking"
        self._touch()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._status["state"] = "unknown"
            self._touch()
            return
        if self._scheduled_refresh_owner is not None:
            return
        self._refresh_dispatch_id += 1
        owner = (self._generation, self._refresh_dispatch_id)
        self._scheduled_refresh_owner = owner
        loop.call_soon(self._launch_scheduled_read, owner)

    def _launch_scheduled_read(self, owner: tuple[int, int]) -> None:
        if self._scheduled_refresh_owner != owner:
            return
        self._scheduled_refresh_owner = None
        if owner[0] == self._generation:
            asyncio.create_task(self.refresh())

    def _login_failed(self, issue: str) -> None:
        self._active_login = None
        self._start_owner = None
        self._status["login_state"] = "failed"
        self._status["login_result"] = "failed"
        self._status["issue"] = issue
        self._auth_recovery_login_revision = None
        self._touch()
