from __future__ import annotations

import asyncio

import pytest

from codex_bridge.account_auth import (
    AccountAuthError,
    AccountAuthService,
    classify_execution_error,
    project_account_read,
    validate_auth_url,
)


def test_account_read_exposes_only_known_chatgpt_account_fields() -> None:
    payload = {
        "account": {
            "type": "chatgpt",
            "email": "private@example.test",
            "planType": "plus",
            "workspaceName": "private workspace",
        },
        "userId": "private-user-id",
        "requiresOpenaiAuth": True,
        "workspaceRouting": {"accountId": "private-account-id"},
    }

    assert project_account_read(payload) == {
        "state": "signed_in",
        "account_type": "chatgpt",
        "auth_mode": "chatgpt",
        "plan_type": "plus",
        "login_state": "idle",
    }


@pytest.mark.parametrize(
    ("requires_auth", "expected_state"),
    [(True, "login_required"), (False, "not_required")],
)
def test_null_account_uses_explicit_requires_openai_auth(
    requires_auth: bool, expected_state: str
) -> None:
    assert project_account_read(
        {"account": None, "userId": None, "requiresOpenaiAuth": requires_auth}
    ) == {
        "state": expected_state,
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "idle",
    }


def test_non_chatgpt_account_does_not_expose_unknown_plan_values() -> None:
    assert project_account_read(
        {
            "account": {"type": "apiKey", "planType": "PRIVATE-PLAN"},
            "requiresOpenaiAuth": False,
        }
    ) == {
        "state": "signed_in",
        "account_type": "apiKey",
        "auth_mode": None,
        "plan_type": None,
        "login_state": "idle",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"account": None, "requiresOpenaiAuth": "true"},
        {"account": {"type": "futureProvider"}, "requiresOpenaiAuth": False},
        {"account": {"type": "chatgpt", "planType": "futurePlan"}, "requiresOpenaiAuth": True},
    ],
)
def test_malformed_or_unsupported_account_response_is_unknown(payload: object) -> None:
    assert project_account_read(payload) == {
        "state": "unknown",
        "account_type": None,
        "auth_mode": None,
        "plan_type": None,
        "login_state": "idle",
    }


@pytest.mark.parametrize(
    ("error", "turn_state", "expected"),
    [
        ({"codexErrorInfo": "unauthorized"}, "failed", "auth_check"),
        ({"codexErrorInfo": "usageLimitExceeded"}, "failed", "quota"),
        ({"codexErrorInfo": "rateLimitExceeded"}, "failed", "quota"),
        ({"httpStatusCode": 429}, "failed", "quota"),
        (
            {"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": None}}},
            "failed",
            "network",
        ),
        ({"httpStatusCode": 401}, "failed", "unknown"),
        ({"httpStatusCode": 403}, "failed", "unknown"),
        ({"message": "unauthorized"}, "failed", "unknown"),
        (None, "needs_approval", "approval"),
        ({"codexErrorInfo": "serverOverloaded"}, "failed", "app_server"),
    ],
)
def test_execution_error_classification_requires_structured_auth_evidence(
    error: object, turn_state: str, expected: str
) -> None:
    assert classify_execution_error(error, turn_state=turn_state) == expected


@pytest.mark.parametrize(
    ("flow_type", "url"),
    [
        ("chatgpt", "https://auth.openai.com/oauth/authorize?client_id=public&state=opaque"),
        ("chatgptDeviceCode", "https://auth.openai.com/codex/device"),
    ],
)
def test_auth_url_accepts_only_expected_official_flow(flow_type: str, url: str) -> None:
    assert validate_auth_url(url, flow_type) is True


@pytest.mark.parametrize(
    ("flow_type", "url"),
    [
        ("chatgpt", "http://auth.openai.com/oauth/authorize"),
        ("chatgpt", "https://auth.openai.com@evil.example/oauth/authorize"),
        ("chatgpt", "https://evil.example@auth.openai.com/oauth/authorize"),
        ("chatgpt", "https://auth.openai.com.evil.example/oauth/authorize"),
        ("chatgpt", "https://auth.openai.com:444/oauth/authorize"),
        ("chatgpt", "https://auth.openai.com/codex/device"),
        ("chatgptDeviceCode", "https://auth.openai.com/oauth/authorize"),
        ("chatgptDeviceCode", "https://auth.openai.com/codex/device#fragment"),
        ("unknown", "https://auth.openai.com/oauth/authorize"),
    ],
)
def test_auth_url_rejects_unverified_targets(flow_type: str, url: str) -> None:
    assert validate_auth_url(url, flow_type) is False


@pytest.mark.asyncio
async def test_service_reads_account_without_refresh_and_coalesces_reads() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    gate = asyncio.Event()

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        await gate.wait()
        return {
            "account": {"type": "chatgpt", "planType": "pro"},
            "requiresOpenaiAuth": True,
        }

    service = AccountAuthService(request)
    first = asyncio.create_task(service.refresh())
    second = asyncio.create_task(service.refresh())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == [("account/read", {})]
    gate.set()
    assert (await first)["state"] == "signed_in"
    assert (await second)["state"] == "signed_in"


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_read_first", [False, True])
async def test_login_completion_invalidates_inflight_read_in_either_response_order(
    fresh_read_first: bool,
) -> None:
    read_started = [asyncio.Event(), asyncio.Event()]
    read_release = [asyncio.Event(), asyncio.Event()]
    read_responses = [
        {"account": None, "requiresOpenaiAuth": True},
        {
            "account": {"type": "chatgpt", "planType": "plus"},
            "requiresOpenaiAuth": True,
        },
    ]
    read_index = 0

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal read_index
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "fresh-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=fresh",
            }
        if method == "account/read":
            index = read_index
            read_index += 1
            read_started[index].set()
            await read_release[index].wait()
            return read_responses[index]
        raise AssertionError(method)

    service = AccountAuthService(request)
    await service.start_login("chatgpt")
    old_read = asyncio.create_task(service.refresh())
    await read_started[0].wait()
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "fresh-login", "success": True, "error": None},
        }
    )
    for _ in range(10):
        if read_started[1].is_set():
            break
        await asyncio.sleep(0)
    assert read_started[1].is_set(), "completion must start a fresh-epoch account/read"

    fresh_read = asyncio.create_task(service.refresh())
    if fresh_read_first:
        read_release[1].set()
        await fresh_read
        read_release[0].set()
        await old_read
    else:
        read_release[0].set()
        await old_read
        assert service.status()["login_result"] == "none"
        read_release[1].set()
        await fresh_read

    status = service.status()
    assert status["state"] == "signed_in"
    assert status["login_state"] == "succeeded"
    assert status["login_result"] == "succeeded"
    assert status["auth_warning"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["account/updated", "unauthorized"])
async def test_auth_event_read_epoch_rejects_old_signed_in_result(event: str) -> None:
    read_started = [asyncio.Event(), asyncio.Event()]
    read_release = [asyncio.Event(), asyncio.Event()]
    read_index = 0

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal read_index
        if method == "account/read":
            index = read_index
            read_index += 1
            read_started[index].set()
            await read_release[index].wait()
            if index == 0:
                return {
                    "account": {"type": "chatgpt", "planType": "pro"},
                    "requiresOpenaiAuth": True,
                }
            return {"account": None, "requiresOpenaiAuth": True}
        raise AssertionError(method)

    service = AccountAuthService(request)
    old_read = asyncio.create_task(service.refresh())
    await read_started[0].wait()
    if event == "account/updated":
        service.on_notification({"method": "account/updated", "params": {}})
    else:
        service.observe_execution_error({"codexErrorInfo": "unauthorized"})
    for _ in range(10):
        if read_started[1].is_set():
            break
        await asyncio.sleep(0)
    assert read_started[1].is_set(), "semantic auth events must bypass the old read"

    fresh_read = asyncio.create_task(service.refresh())
    read_release[1].set()
    await fresh_read
    read_release[0].set()
    await old_read

    status = service.status()
    assert status["state"] == "login_required"
    assert status["auth_warning"] == ("execution_auth_error" if event == "unauthorized" else "none")


@pytest.mark.asyncio
async def test_execution_auth_warning_survives_recognized_account_until_fresh_login() -> None:
    reads = 0

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal reads
        if method == "account/read":
            reads += 1
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "recovery-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=recovery",
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    await service.refresh()
    assert service.observe_execution_error({"codexErrorInfo": "unauthorized"}) == "auth_check"
    for _ in range(10):
        if reads == 2:
            break
        await asyncio.sleep(0)
    assert service.status()["state"] == "signed_in"
    assert service.status()["auth_warning"] == "execution_auth_error"
    assert service.status()["login_state"] == "idle"

    await service.start_login("chatgpt")
    assert service.status()["auth_warning"] == "execution_auth_error"
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "recovery-login", "success": True, "error": None},
        }
    )
    for _ in range(10):
        if reads == 3 and service.status()["login_result"] == "succeeded":
            break
        await asyncio.sleep(0)
    assert service.status()["login_result"] == "succeeded"
    assert service.status()["auth_warning"] == "none"


@pytest.mark.asyncio
async def test_auth_error_does_not_replace_active_login_waiting_state() -> None:
    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "active-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=active",
            }
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    await service.start_login("chatgpt")
    service.observe_execution_error({"codexErrorInfo": "unauthorized"})
    for _ in range(10):
        if service.status()["state"] == "signed_in":
            break
        await asyncio.sleep(0)
    assert service.status()["login_state"] == "waiting"
    assert service.status()["auth_warning"] == "execution_auth_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_read_fails", [False, True])
async def test_unauthorized_during_login_recovery_read_ends_as_failed(
    fresh_read_fails: bool,
) -> None:
    read_started = [asyncio.Event(), asyncio.Event()]
    read_release = [asyncio.Event(), asyncio.Event()]
    read_index = 0

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal read_index
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "recovery-race-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=race",
            }
        if method == "account/read":
            index = read_index
            read_index += 1
            read_started[index].set()
            await read_release[index].wait()
            if index == 1 and fresh_read_fails:
                raise RuntimeError("private account read error")
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    await service.start_login("chatgpt")
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "recovery-race-login", "success": True},
        }
    )
    await read_started[0].wait()

    service.observe_execution_error({"codexErrorInfo": "unauthorized"})
    await read_started[1].wait()
    read_release[0].set()
    read_release[1].set()
    for _ in range(10):
        if service.status()["login_result"] == "failed":
            break
        await asyncio.sleep(0)

    status = service.status()
    assert status["login_state"] == "failed"
    assert status["login_result"] == "failed"
    assert status["issue"] == "not_confirmed"
    assert status["auth_warning"] == "execution_auth_error"
    assert status["state"] == ("unknown" if fresh_read_fails else "signed_in")


@pytest.mark.asyncio
async def test_disconnect_old_start_response_cannot_clear_new_start_owner() -> None:
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    start_index = 0

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal start_index
        if method == "account/login/start":
            index = start_index
            start_index += 1
            started[index].set()
            await release[index].wait()
            return {
                "type": "chatgpt",
                "loginId": f"login-{index}",
                "authUrl": f"https://auth.openai.com/oauth/authorize?state={index}",
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    old_start = asyncio.create_task(service.start_login("chatgpt"))
    await started[0].wait()
    service.disconnect()
    new_start = asyncio.create_task(service.start_login("chatgpt"))
    await started[1].wait()

    release[0].set()
    with pytest.raises(AccountAuthError, match="connection_lost"):
        await old_start
    with pytest.raises(AccountAuthError, match="login_in_progress"):
        await service.start_login("chatgptDeviceCode")

    release[1].set()
    result = await new_start
    assert result["loginId"] == "login-1"
    assert service.status()["login_state"] == "waiting"
    assert service.status()["login_revision"] == 2


@pytest.mark.asyncio
async def test_completion_before_any_start_is_not_retained_for_future_attempt() -> None:
    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "reused-login-id",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=current",
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "reused-login-id", "success": True, "error": None},
        }
    )
    await service.start_login("chatgpt")
    assert service.status()["login_state"] == "waiting"
    assert service.status()["login_result"] == "none"


@pytest.mark.asyncio
async def test_service_login_completion_must_match_login_id_and_reads_account() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "login-1",
                "authUrl": "https://auth.openai.com/oauth/authorize?secret=one-time",
            }
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
                "userId": "must-not-escape",
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    started = await service.start_login("chatgpt")
    assert started["authUrl"].endswith("secret=one-time")
    assert service.status()["login_state"] == "waiting"
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "other-login", "success": True, "error": "SECRET"},
        }
    )
    assert service.status()["login_state"] == "waiting"
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "login-1", "success": True, "error": "SECRET"},
        }
    )
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "login-1", "success": True, "error": "SECRET"},
        }
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await service.refresh()
    state = service.status()
    assert state["state"] == "signed_in"
    assert state["login_state"] == "succeeded"
    assert state["login_result"] == "succeeded"
    assert "must-not-escape" not in repr(state)
    assert calls == [
        ("account/login/start", {"type": "chatgpt"}),
        ("account/read", {}),
    ]


@pytest.mark.asyncio
async def test_service_allows_only_one_start_rpc_while_first_response_is_pending() -> None:
    start_started = asyncio.Event()
    start_release = asyncio.Event()
    calls: list[str] = []

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        calls.append(method)
        if method == "account/login/start":
            start_started.set()
            await start_release.wait()
            return {
                "type": "chatgpt",
                "loginId": "single-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=single",
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    first = asyncio.create_task(service.start_login("chatgpt"))
    await start_started.wait()
    with pytest.raises(AccountAuthError, match="login_in_progress"):
        await service.start_login("chatgptDeviceCode")
    start_release.set()
    result = await first
    assert result["loginId"] == "single-login"
    assert calls == ["account/login/start"]


@pytest.mark.asyncio
async def test_account_updated_notifications_coalesce_to_one_safe_read() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    read_started = asyncio.Event()
    release = asyncio.Event()

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        read_started.set()
        await release.wait()
        return {
            "account": {"type": "chatgpt", "planType": "team"},
            "userId": "PRIVATE-USER-ID",
            "requiresOpenaiAuth": True,
        }

    service = AccountAuthService(request)
    for _ in range(3):
        service.on_notification(
            {
                "method": "account/updated",
                "params": {
                    "authMode": "chatgpt",
                    "planType": "team",
                    "reason": "unknown",
                    "email": "PRIVATE-EMAIL",
                },
            }
        )
    await read_started.wait()
    await asyncio.sleep(0)
    assert calls == [("account/read", {})]
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert service.status()["state"] == "signed_in"
    assert "PRIVATE" not in repr(service.status())


@pytest.mark.asyncio
async def test_service_accepts_completion_that_arrives_before_login_start_response() -> None:
    completed = asyncio.Event()
    calls: list[str] = []
    service: AccountAuthService

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        calls.append(method)
        if method == "account/login/start":
            service.on_notification(
                {
                    "method": "account/login/completed",
                    "params": {"loginId": "early-1", "success": True, "error": "PRIVATE"},
                }
            )
            await completed.wait()
            return {
                "type": "chatgpt",
                "loginId": "early-1",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=ok",
            }
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "free"},
                "requiresOpenaiAuth": True,
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    starting = asyncio.create_task(service.start_login("chatgpt"))
    await asyncio.sleep(0)
    completed.set()
    start_response = await starting
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert start_response["login_state"] == "succeeded"
    assert start_response["login_result"] == "succeeded"
    assert "authUrl" not in start_response
    assert service.status()["state"] == "signed_in"
    assert service.status()["login_state"] == "succeeded"
    assert calls == ["account/login/start", "account/read"]


@pytest.mark.asyncio
async def test_service_cancel_is_correlated_and_disconnect_discards_stale_read() -> None:
    read_started = asyncio.Event()
    read_release = asyncio.Event()
    calls: list[tuple[str, dict[str, object]]] = []

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "account/login/start":
            return {
                "type": "chatgptDeviceCode",
                "loginId": "login-2",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "ABCD-EFGH",
            }
        if method == "account/login/cancel":
            return {"status": "canceled"}
        if method == "account/read":
            read_started.set()
            await read_release.wait()
            return {"account": None, "requiresOpenaiAuth": True}
        raise AssertionError(method)

    service = AccountAuthService(request)
    started = await service.start_login("chatgptDeviceCode")
    login_revision = int(started["login_revision"])
    with pytest.raises(AccountAuthError):
        await service.cancel_login("wrong-id", login_revision)
    canceled = await service.cancel_login("login-2", login_revision)
    assert canceled["login_state"] == "cancelled"
    refresh = asyncio.create_task(service.refresh())
    await read_started.wait()
    service.disconnect()
    read_release.set()
    await refresh
    assert service.status()["state"] == "unknown"
    assert (await service.refresh())["state"] == "login_required"
    assert calls[:2] == [
        ("account/login/start", {"type": "chatgptDeviceCode"}),
        ("account/login/cancel", {"loginId": "login-2"}),
    ]
    assert calls.count(("account/read", {})) == 2


@pytest.mark.asyncio
async def test_completion_notification_wins_a_cancel_response_race() -> None:
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "race-1",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=race",
            }
        if method == "account/login/cancel":
            cancel_started.set()
            await cancel_release.wait()
            return {"status": "canceled"}
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        raise AssertionError(method)

    service = AccountAuthService(request)
    started = await service.start_login("chatgpt")
    cancel = asyncio.create_task(service.cancel_login("race-1", int(started["login_revision"])))
    await cancel_started.wait()
    service.on_notification(
        {
            "method": "account/login/completed",
            "params": {"loginId": "race-1", "success": True, "error": "PRIVATE"},
        }
    )
    cancel_release.set()
    await cancel
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert service.status()["state"] == "signed_in"
    assert service.status()["login_state"] == "succeeded"


@pytest.mark.asyncio
async def test_service_rejects_unsupported_or_unsafe_login_response() -> None:
    calls: list[str] = []

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        calls.append(method)
        if method == "account/login/start":
            return {
                "type": "chatgpt",
                "loginId": "login-unsafe",
                "authUrl": "https://auth.openai.com@attacker.invalid/oauth/authorize",
            }
        if method == "account/login/cancel":
            return {"status": "canceled"}
        raise AssertionError(method)

    service = AccountAuthService(request)
    started = await service.start_login("chatgpt")
    assert started["start_issue"] == "unsupported_login_response"
    assert "authUrl" not in started
    assert service.status()["login_state"] == "cancelled"
    assert service.status()["login_result"] == "cancelled"
    assert calls == ["account/login/start", "account/login/cancel"]


@pytest.mark.asyncio
async def test_service_discards_login_response_from_a_disconnected_generation() -> None:
    login_started = asyncio.Event()
    login_release = asyncio.Event()

    async def request(method: str, _params: dict[str, object]) -> dict[str, object]:
        if method == "account/login/start":
            login_started.set()
            await login_release.wait()
            return {
                "type": "chatgpt",
                "loginId": "stale-login",
                "authUrl": "https://auth.openai.com/oauth/authorize?state=stale",
            }
        return {"account": None, "requiresOpenaiAuth": False}

    service = AccountAuthService(request)
    starting = asyncio.create_task(service.start_login("chatgpt"))
    await login_started.wait()
    service.disconnect()
    login_release.set()
    with pytest.raises(AccountAuthError, match="connection_lost"):
        await starting
    assert service.status()["state"] == "unknown"
    assert service.status()["login_state"] == "idle"
