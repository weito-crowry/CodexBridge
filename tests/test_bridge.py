from __future__ import annotations

import pytest

from codex_bridge.bridge import Bridge, BridgeError
from codex_bridge.jsonrpc import JsonRpcRemoteError
from codex_bridge.paths import AllowedPathPolicy, PathPolicyError
from codex_bridge.state import StateStore
from tests.bridge_test_fakes import make_bridge

pytest_plugins = ("tests.bridge_test_support",)


@pytest.mark.asyncio
async def test_rate_limits_uses_the_formal_app_server_request(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.rate_limits()

    assert result["rateLimits"]["primary"]["windowDurationMins"] == 300
    assert app.calls == [("account/rateLimits/read", {})]


@pytest.mark.asyncio
async def test_start_returns_native_ids_without_waiting_for_completion(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.start(str(allowed_dir), "inspect this")

    assert result["thread_id"] == "native-thread"
    assert result["turn_id"] == "native-turn"
    assert result["state"] == "in_progress"
    assert result["thread_metadata"] == {
        "model_provider": "openai",
        "model": "gpt-5",
        "reasoning_effort": "high",
        "cli_version": "0.1.2",
        "sandbox_mode": None,
        "approval_policy": "on-request",
        "approvals_reviewer": "auto_review",
    }
    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        },
    )
    assert app.methods == ["thread/start", "thread/name/set", "turn/start"]
    assert app.calls[2][1]["input"] == [{"type": "text", "text": "inspect this"}]
    status = await bridge.status(result["thread_id"], result["turn_id"])
    assert status["thread_metadata"]["approvals_reviewer"] == "auto_review"
    assert status["thread_metadata"]["approval_policy"] == "on-request"


@pytest.mark.asyncio
async def test_start_passes_explicit_model_and_effort_only_to_expected_wire_fields(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(
        str(allowed_dir), "inspect this", model="model-value", reasoning_effort="high"
    )

    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "model": "model-value",
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        },
    )
    assert app.calls[2][0] == "turn/start"
    assert app.calls[2][1]["model"] == "model-value"
    assert app.calls[2][1]["effort"] == "high"
    assert "reasoningEffort" not in app.calls[2][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox_mode", [None, "inherit"])
async def test_start_inherit_does_not_send_sandbox(allowed_dir, sandbox_mode) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(str(allowed_dir), "inspect this", sandbox_mode=sandbox_mode)

    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        },
    )


@pytest.mark.asyncio
async def test_start_full_access_sends_durable_permission_profile_override(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.start(str(allowed_dir), "inspect this", sandbox_mode="danger-full-access")

    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "config": {"default_permissions": ":danger-full-access"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        },
    )


@pytest.mark.asyncio
async def test_start_rejects_unsupported_sandbox_mode(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(ValueError, match="sandbox_mode"):
        await bridge.start(
            str(allowed_dir), "inspect this", sandbox_mode="read-only", title="Thread title"
        )

    assert app.calls == []


@pytest.mark.asyncio
async def test_full_access_still_validates_allowed_root(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(PathPolicyError):
        await bridge.start(
            str(tmp_path / "outside"), "inspect this", sandbox_mode="danger-full-access"
        )

    assert app.calls == []


@pytest.mark.asyncio
async def test_start_projects_effective_execution_settings_from_app_server(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_settings = {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": {"type": "dangerFullAccess"},
        "activePermissionProfile": {"id": ":danger-full-access"},
    }

    result = await bridge.start(
        str(allowed_dir),
        "inspect this",
        sandbox_mode="danger-full-access",
        approvals_reviewer="user",
    )

    assert result["thread_metadata"]["sandbox_mode"] == "danger-full-access"
    assert result["thread_metadata"]["approval_policy"] == "on-request"
    assert result["thread_metadata"]["approvals_reviewer"] == "user"
    assert "active_permission_profile" not in result["thread_metadata"]
    assert app.calls[0][1]["approvalPolicy"] == "on-request"
    assert app.calls[0][1]["config"]["default_permissions"] == ":danger-full-access"


@pytest.mark.asyncio
async def test_start_manual_reviewer_preserves_on_request_default_flow(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    result = await bridge.start(str(allowed_dir), "inspect this", approvals_reviewer="user")

    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        },
    )
    assert result["thread_metadata"]["approvals_reviewer"] == "user"
    assert result["thread_metadata"]["approval_policy"] == "on-request"


@pytest.mark.asyncio
async def test_start_rejects_invalid_reviewer_before_creating_thread(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(ValueError, match="approvals_reviewer"):
        await bridge.start(str(allowed_dir), "inspect this", approvals_reviewer="guardian_subagent")

    assert app.calls == []


@pytest.mark.asyncio
async def test_start_does_not_start_turn_if_reviewer_metadata_mismatches(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_settings = {"approvalsReviewer": "auto_review"}

    with pytest.raises(BridgeError, match="empty thread native-thread.*approvals_reviewer"):
        await bridge.start(str(allowed_dir), "inspect this", approvals_reviewer="user")

    assert app.methods == ["thread/start"]


@pytest.mark.asyncio
async def test_start_does_not_start_turn_if_auto_review_policy_metadata_mismatches(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_settings = {"approvalPolicy": "never"}

    with pytest.raises(BridgeError, match="empty thread.*approval_policy"):
        await bridge.start(str(allowed_dir), "inspect this")

    assert app.methods == ["thread/start"]


@pytest.mark.asyncio
async def test_start_manual_reviewer_does_not_start_turn_if_policy_is_not_on_request(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_settings = {"approvalPolicy": "never"}

    with pytest.raises(BridgeError, match="empty thread native-thread.*approval_policy"):
        await bridge.start(str(allowed_dir), "inspect this", approvals_reviewer="user")

    assert app.methods == ["thread/start"]


@pytest.mark.asyncio
async def test_start_reports_rejected_reviewer_without_retry(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_start_error = JsonRpcRemoteError("unknown field approvalsReviewer")

    with pytest.raises(BridgeError, match="App Server rejected.*thread/start"):
        await bridge.start(str(allowed_dir), "inspect this")

    assert app.methods == ["thread/start"]


@pytest.mark.asyncio
async def test_model_capabilities_reads_all_pages_and_only_projects_safe_fields(
    allowed_dir,
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.model_pages = {
        None: {
            "data": [
                {
                    "id": "private-id",
                    "model": "model-a",
                    "displayName": "Model A",
                    "description": "Safe description",
                    "hidden": False,
                    "isDefault": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    "defaultReasoningEffort": "low",
                }
            ],
            "nextCursor": "page-two",
        },
        "page-two": {
            "data": [
                {
                    "model": "hidden-model",
                    "displayName": "Hidden",
                    "description": "",
                    "hidden": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    "defaultReasoningEffort": "low",
                }
            ]
        },
    }
    app.config_response = {
        "config": {"model": "model-a", "model_reasoning_effort": "low", "api_key": "secret"},
        "layers": {"raw": "never return"},
    }

    result = await bridge.model_capabilities()

    assert result["defaults"] == {"model": "model-a", "reasoning_effort": "low"}
    assert [entry["model"] for entry in result["models"]] == ["model-a"]
    assert all("id" not in entry for entry in result["models"])
    assert "secret" not in str(result)
    assert app.calls[-1] == (
        "config/read",
        {"includeLayers": False, "cwd": None},
    )
    assert [call for call in app.calls if call[0] == "model/list"] == [
        ("model/list", {}),
        ("model/list", {"cursor": "page-two"}),
    ]


@pytest.mark.asyncio
async def test_model_capabilities_pagination_cycle_and_empty_catalog_are_safe(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    with pytest.raises(BridgeError, match="model capabilities are unavailable") as exc_info:
        await bridge.model_capabilities()
    assert "api" not in str(exc_info.value)

    entry = {
        "id": "model-id",
        "model": "model-value",
        "displayName": "Model",
        "description": "",
        "hidden": False,
        "supportedReasoningEfforts": [{"reasoningEffort": "effort"}],
        "defaultReasoningEffort": "effort",
    }
    app.model_pages = {
        None: {"data": [entry], "nextCursor": "loop"},
        "loop": {"data": [entry], "nextCursor": "loop"},
    }

    with pytest.raises(BridgeError, match="model capabilities are unavailable"):
        await bridge.model_capabilities()


@pytest.mark.asyncio
async def test_continue_resumes_a_thread_not_loaded_in_this_process(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["persisted-thread"] = str(allowed_dir)

    await bridge.continue_thread("persisted-thread", "continue")

    assert app.methods == ["thread/read", "thread/resume", "turn/start"]
    assert app.calls[1] == ("thread/resume", {"threadId": "persisted-thread"})
    assert "approvalsReviewer" not in app.calls[2][1]
    assert "approvalPolicy" not in app.calls[2][1]


@pytest.mark.asyncio
async def test_cold_resume_uses_effective_metadata_without_sandbox_overrides(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    await bridge.start(str(allowed_dir), "materialize", sandbox_mode="danger-full-access")
    app.thread_cwds["native-thread"] = str(allowed_dir)
    app.thread_resume_settings = {
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": {"type": "dangerFullAccess"},
        "activePermissionProfile": {"id": ":danger-full-access"},
    }
    cold_bridge = Bridge(app, StateStore(), AllowedPathPolicy((str(allowed_dir),)))

    result = await cold_bridge.continue_thread("native-thread", "continue")

    assert app.calls[0] == (
        "thread/start",
        {
            "cwd": str(allowed_dir),
            "config": {"default_permissions": ":danger-full-access"},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
        },
    )
    assert app.calls[4] == ("thread/resume", {"threadId": "native-thread"})
    assert result["thread_metadata"]["sandbox_mode"] == "danger-full-access"
    assert result["thread_metadata"]["approval_policy"] == "on-request"


@pytest.mark.asyncio
async def test_continue_validates_persisted_thread_cwd_before_resume(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["persisted-thread"] = str(allowed_dir)

    await bridge.continue_thread("persisted-thread", "continue")

    assert app.methods == ["thread/read", "thread/resume", "turn/start"]


@pytest.mark.asyncio
async def test_continue_rejects_out_of_root_thread_before_resume(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-continue"
    outside.mkdir()
    app.thread_cwds["outside-thread"] = str(outside)

    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("outside-thread", "continue")

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_threads_rejects_out_of_root_detail_before_history(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-read"
    outside.mkdir()
    app.thread_cwds["outside-thread"] = str(outside)

    with pytest.raises(PathPolicyError):
        await bridge.threads("outside-thread", include_history=True)

    assert app.methods == ["thread/read"]


@pytest.mark.asyncio
async def test_threads_filters_list_rows_by_canonical_cwd(allowed_dir, tmp_path) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-list"
    outside.mkdir()
    app.thread_list = [
        {"id": "allowed", "cwd": str(allowed_dir)},
        {"id": "outside", "cwd": str(outside)},
    ]

    result = await bridge.threads(limit=10)

    assert [thread["id"] for thread in result["threads"]] == ["allowed"]


@pytest.mark.asyncio
async def test_persisted_thread_path_policy_rejects_sibling_prefix_and_case_variant(
    allowed_dir, tmp_path
) -> None:
    bridge, app, _ = make_bridge(allowed_dir)
    sibling = allowed_dir.parent / f"{allowed_dir.name}-sibling"
    sibling.mkdir()
    app.thread_cwds["sibling-thread"] = str(sibling)
    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("sibling-thread", "continue")

    case_variant = str(sibling).swapcase()
    app.thread_cwds["case-thread"] = case_variant
    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("case-thread", "continue")


@pytest.mark.asyncio
async def test_persisted_thread_path_policy_rejects_symlink_escape(allowed_dir, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed_dir / "linked-thread-cwd"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    bridge, app, _ = make_bridge(allowed_dir)
    app.thread_cwds["linked-thread"] = str(link)

    with pytest.raises(PathPolicyError):
        await bridge.continue_thread("linked-thread", "continue")


@pytest.mark.asyncio
async def test_steer_uses_expected_turn_id(allowed_dir) -> None:
    bridge, app, _ = make_bridge(allowed_dir)

    await bridge.steer("thread", "turn", "change direction")

    assert app.calls[-1][1]["expectedTurnId"] == "turn"
