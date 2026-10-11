from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import types

from codex_bridge.config import ExecutionTargetConfig
from codex_bridge.execution_targets import (
    _REQUIRED_TARGET_TOOLS,
    ExecutionTargetClient,
    ExecutionTargetError,
    ExecutionTargetRouter,
    _extract_dict_result,
    _parse_request_handle,
    _request_handle,
)

LOCAL = ExecutionTargetConfig("main-pc", "Main PC", "local")
NOTEBOOK = ExecutionTargetConfig(
    "notebook", "Notebook PC", "remote", "https://notebook.example.test/mcp"
)
OTHER = ExecutionTargetConfig("other", "Other PC", "remote", "https://other.example.test/mcp")


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.pending_requests: set[int | str] = set()

    def has_pending_request(self, request_id: int | str) -> bool:
        return request_id in self.pending_requests

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, args, kwargs))
        return {
            "ok": True,
            "thread_id": args[0] if args and isinstance(args[0], str) else "native-local",
        }

    async def start(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: str | None = None,
        approvals_reviewer: str = "auto_review",
    ) -> dict[str, Any]:
        kwargs = {}
        if model is not None:
            kwargs["model"] = model
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if sandbox_mode is not None:
            kwargs["sandbox_mode"] = sandbox_mode
        kwargs["approvals_reviewer"] = approvals_reviewer
        self.calls.append(("start", (cwd, prompt), kwargs))
        return {"ok": True, "thread_id": "native-local", "turn_id": "turn-local"}

    async def model_capabilities(self) -> dict[str, Any]:
        return {
            "models": [
                {
                    "model": "model-a",
                    "display_name": "Model A",
                    "description": None,
                    "reasoning_efforts": [{"id": "high", "description": None}],
                    "default_reasoning_effort": "high",
                }
            ],
            "defaults": {"model": "model-a", "reasoning_effort": "high"},
            "execution_modes": [
                {"id": "inherit", "display_name": "Default"},
                {"id": "danger-full-access", "display_name": "Full access"},
            ],
        }

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]:
        return await self._call("continue", thread_id, prompt)

    async def wait(self, thread_id: str, turn_id: str, timeout_seconds=None) -> dict[str, Any]:
        return await self._call("wait", thread_id, turn_id, timeout_seconds=timeout_seconds)

    async def steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        return await self._call("steer", thread_id, turn_id, prompt)

    async def approve(self, request_id: int | str, decision: str) -> dict[str, Any]:
        return await self._call("approve", request_id, decision)

    async def answer_user_input(
        self, request_id: int | str, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        return await self._call("answer", request_id, answers)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self._call("interrupt", thread_id, turn_id)

    async def threads(self, thread_id=None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("threads", (thread_id,), kwargs))
        if thread_id:
            return {"thread": {"id": thread_id}}
        return {"threads": [{"id": "native-local"}], "next_cursor": "local-cursor"}

    async def status(self, thread_id: str, turn_id=None, activity_limit=20) -> dict[str, Any]:
        return await self._call("status", thread_id, turn_id, activity_limit=activity_limit)


class FakeRemote:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"ok": True, "thread_id": "native-remote"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.available = True
        self.error: Exception | None = None

    async def start(self) -> None:
        if self.error:
            raise self.error

    async def close(self) -> None:
        return None

    async def probe(self) -> bool:
        return self.available and self.error is None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.response

    async def setup_capabilities(self) -> dict[str, Any]:
        return await self.call_tool("codex_setup_capabilities", {"target_id": "local"})


def make_router(*targets: ExecutionTargetConfig, response=None):
    local = FakeBridge()
    remotes = {target.id: FakeRemote(response) for target in targets if target.kind == "remote"}
    return ExecutionTargetRouter(targets, lambda: local, remote_clients=remotes), local, remotes


@pytest.mark.asyncio
async def test_single_local_target_start_keeps_native_thread_id() -> None:
    router, bridge, _ = make_router(LOCAL)

    result = await router.codex_start("D:/repo", "do work")

    assert bridge.calls == [
        ("start", ("D:/repo", "do work"), {"approvals_reviewer": "auto_review"})
    ]
    assert result["thread_id"] == "native-local"
    assert result["native_thread_id"] == "native-local"
    assert result["target_id"] == "main-pc"


@pytest.mark.asyncio
async def test_multiple_targets_require_explicit_target_selection() -> None:
    router, _, _ = make_router(LOCAL, NOTEBOOK)

    with pytest.raises(ExecutionTargetError, match="target_id is required"):
        await router.codex_start("Z:/remote-path", "prompt")


@pytest.mark.asyncio
async def test_remote_start_forwards_remote_cwd_and_routes_public_thread_id() -> None:
    remote = FakeRemote({"ok": True, "thread_id": "019abc", "turn_id": "turn-1"})
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    result = await router.codex_start("Z:/Notebook/repo", "prompt", "notebook")

    assert remote.calls == [
        (
            "codex_start",
            {
                "cwd": "Z:/Notebook/repo",
                "prompt": "prompt",
                "approvals_reviewer": "auto_review",
            },
        )
    ]
    assert result["thread_id"] == "notebook::019abc"
    assert result["native_thread_id"] == "019abc"
    assert result["target_id"] == "notebook"


@pytest.mark.asyncio
async def test_remote_start_forwards_confirmed_model_and_effort_with_metadata() -> None:
    class ConfiguredRemote(FakeRemote):
        async def setup_capabilities(self) -> dict[str, Any]:
            self.calls.append(("codex_setup_capabilities", {"target_id": "leaf-main"}))
            return {
                "models": [
                    {
                        "model": "model-a",
                        "display_name": "Model A",
                        "description": None,
                        "reasoning_efforts": [{"id": "high", "description": None}],
                        "default_reasoning_effort": "high",
                    }
                ],
                "defaults": {"model": "model-a", "reasoning_effort": "high"},
            }

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            return {"ok": True, "thread_id": "native-remote", "turn_id": "turn-1"}

    remote = ConfiguredRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    result = await router.codex_start(
        "Z:/Notebook/repo", "prompt", "notebook", model="model-a", reasoning_effort="high"
    )

    assert remote.calls == [
        ("codex_setup_capabilities", {"target_id": "leaf-main"}),
        (
            "codex_start",
            {
                "cwd": "Z:/Notebook/repo",
                "prompt": "prompt",
                "model": "model-a",
                "reasoning_effort": "high",
                "approvals_reviewer": "auto_review",
            },
        ),
    ]
    assert result["thread_id"] == "notebook::native-remote"
    assert result["execution_config"] == {
        "target_id": "notebook",
        "model": "model-a",
        "reasoning_effort": "high",
        "approvals_reviewer": "auto_review",
    }


@pytest.mark.asyncio
async def test_local_full_access_is_forwarded_and_reported_without_model() -> None:
    router, bridge, _ = make_router(LOCAL)

    result = await router.codex_start("D:/repo", "do work", sandbox_mode="danger-full-access")

    assert bridge.calls == [
        (
            "start",
            ("D:/repo", "do work"),
            {"sandbox_mode": "danger-full-access", "approvals_reviewer": "auto_review"},
        )
    ]
    assert result["execution_config"] == {
        "target_id": "main-pc",
        "model": None,
        "reasoning_effort": None,
        "sandbox_mode": "danger-full-access",
        "approvals_reviewer": "auto_review",
    }


@pytest.mark.asyncio
async def test_remote_full_access_is_forwarded_and_inherit_is_omitted() -> None:
    class FullAccessRemote(FakeRemote):
        async def setup_capabilities(self) -> dict[str, Any]:
            self.calls.append(("codex_setup_capabilities", {"target_id": "local"}))
            return {
                "models": [
                    {
                        "model": "model-a",
                        "display_name": "Model A",
                        "description": None,
                        "reasoning_efforts": [{"id": "high", "description": None}],
                        "default_reasoning_effort": "high",
                    }
                ],
                "defaults": {"model": "model-a", "reasoning_effort": "high"},
                "execution_modes": [
                    {"id": "inherit", "display_name": "Default"},
                    {"id": "danger-full-access", "display_name": "Full access"},
                ],
            }

    remote = FullAccessRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    full_access = await router.codex_start(
        "Z:/Notebook/repo", "prompt", "notebook", sandbox_mode="danger-full-access"
    )
    inherited = await router.codex_start(
        "Z:/Notebook/repo", "prompt", "notebook", sandbox_mode="inherit"
    )

    assert remote.calls == [
        ("codex_setup_capabilities", {"target_id": "local"}),
        (
            "codex_start",
            {
                "cwd": "Z:/Notebook/repo",
                "prompt": "prompt",
                "sandbox_mode": "danger-full-access",
                "approvals_reviewer": "auto_review",
            },
        ),
        (
            "codex_start",
            {
                "cwd": "Z:/Notebook/repo",
                "prompt": "prompt",
                "approvals_reviewer": "auto_review",
            },
        ),
    ]
    assert full_access["execution_config"]["sandbox_mode"] == "danger-full-access"
    assert full_access["execution_config"]["approvals_reviewer"] == "auto_review"
    assert inherited["execution_config"]["sandbox_mode"] == "inherit"
    assert inherited["execution_config"]["approvals_reviewer"] == "auto_review"


@pytest.mark.asyncio
async def test_setup_capabilities_returns_local_catalog_with_target_metadata() -> None:
    router, _, _ = make_router(LOCAL)

    result = await router.setup_capabilities("main-pc")

    assert result["target"] == {
        "id": "main-pc",
        "name": "Main PC",
        "kind": "local",
        "available": True,
    }
    assert result["models"][0]["model"] == "model-a"
    assert result["execution_modes"] == [
        {"id": "inherit", "display_name": "Default"},
        {"id": "danger-full-access", "display_name": "Full access"},
    ]
    assert "url" not in str(result)


@pytest.mark.asyncio
async def test_setup_capabilities_forwards_only_selected_remote_and_safely_degrades() -> None:
    offline = FakeRemote()
    online = FakeRemote(
        {
            "target": {"id": "remote-local", "available": True},
            "models": [],
            "defaults": {"model": None, "reasoning_effort": None},
            "error": {"code": "capabilities_unavailable", "message": "safe"},
        }
    )
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK, OTHER),
        lambda: FakeBridge(),
        remote_clients={"notebook": online, "other": offline},
    )

    result = await router.setup_capabilities("notebook")

    assert [call[0] for call in online.calls] == ["codex_setup_capabilities"]
    assert offline.calls == []
    assert result["target"]["id"] == "notebook"
    assert result["target"]["available"] is True
    assert result["models"] == []
    assert result["error"]["code"] == "capabilities_unavailable"
    assert "example.test" not in str(result)


@pytest.mark.asyncio
async def test_old_remote_without_setup_tool_stays_available_for_execution() -> None:
    class OldRemote(FakeRemote):
        async def setup_capabilities(self) -> dict[str, Any]:
            self.calls.append(("codex_setup_capabilities", {"target_id": "local"}))
            raise ExecutionTargetError("execution target returned an upstream tool error")

    old = OldRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": old}
    )

    result = await router.setup_capabilities("notebook")
    targets = await router.target_list()

    assert result["target"]["available"] is True
    assert result["error"]["code"] == "capabilities_unavailable"
    assert targets["targets"][1]["available"] is True
    assert old.calls[0][0] == "codex_setup_capabilities"


@pytest.mark.asyncio
async def test_remote_setup_call_uses_leaf_target_id_and_app_only_tool_call() -> None:
    class SetupSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls.append((name, arguments))
            if name == "codex_targets":
                return types.CallToolResult(
                    content=[],
                    structured_content={
                        "targets": [{"id": "leaf-main", "available": True}],
                        "selection_required": False,
                    },
                )
            return types.CallToolResult(
                content=[],
                structured_content={
                    "target": {"id": "leaf-main", "available": True},
                    "models": [],
                    "defaults": {"model": None, "reasoning_effort": None},
                },
            )

    client = ExecutionTargetClient(NOTEBOOK)
    client._connected = True
    session = SetupSession()
    client._session = session

    result = await client.setup_capabilities()

    assert result["target"]["id"] == "leaf-main"
    assert session.calls == [
        ("codex_targets", {}),
        ("codex_setup_capabilities", {"target_id": "leaf-main"}),
    ]


@pytest.mark.asyncio
async def test_explicit_start_config_is_validated_and_added_to_result() -> None:
    router, bridge, _ = make_router(LOCAL)

    result = await router.codex_start(
        "D:/repo", "do work", "main-pc", model="model-a", reasoning_effort="high"
    )

    assert bridge.calls[0] == (
        "start",
        ("D:/repo", "do work"),
        {"model": "model-a", "reasoning_effort": "high", "approvals_reviewer": "auto_review"},
    )
    assert result["execution_config"] == {
        "target_id": "main-pc",
        "model": "model-a",
        "reasoning_effort": "high",
        "approvals_reviewer": "auto_review",
    }


@pytest.mark.asyncio
async def test_explicit_start_rejects_stale_model_or_effort_without_creating_thread() -> None:
    router, bridge, _ = make_router(LOCAL)

    with pytest.raises(ExecutionTargetError, match="selected model is no longer available"):
        await router.codex_start("D:/repo", "do work", "main-pc", model="stale")
    with pytest.raises(ExecutionTargetError, match="reasoning effort is not supported"):
        await router.codex_start(
            "D:/repo", "do work", "main-pc", model="model-a", reasoning_effort="low"
        )
    with pytest.raises(ExecutionTargetError, match="requires an explicit model"):
        await router.codex_start("D:/repo", "do work", "main-pc", reasoning_effort="high")

    assert bridge.calls == []


@pytest.mark.asyncio
async def test_confirm_setup_revalidates_unknown_unavailable_and_stale_selections() -> None:
    router, _, remotes = make_router(LOCAL, NOTEBOOK)
    with pytest.raises(ExecutionTargetError, match="unknown execution target"):
        await router.confirm_setup("missing", "model-a", "high")

    remotes["notebook"].available = False
    with pytest.raises(ExecutionTargetError, match="execution target is unavailable"):
        await router.confirm_setup("notebook", "model-a", "high")

    with pytest.raises(ExecutionTargetError, match="selected model is no longer available"):
        await router.confirm_setup("main-pc", "stale-model", "high")
    with pytest.raises(ExecutionTargetError, match="reasoning effort is not supported"):
        await router.confirm_setup("main-pc", "model-a", "low")


@pytest.mark.asyncio
async def test_confirm_setup_validates_and_returns_sandbox_mode() -> None:
    router, _, _ = make_router(LOCAL)

    result = await router.confirm_setup(
        "main-pc", "model-a", "high", sandbox_mode="danger-full-access"
    )

    assert result["selection"]["sandbox_mode"] == "danger-full-access"
    assert result["selection"]["approvals_reviewer"] == "auto_review"
    manual = await router.confirm_setup("main-pc", "model-a", "high", approvals_reviewer="user")
    assert manual["selection"]["approvals_reviewer"] == "user"
    with pytest.raises(ExecutionTargetError, match="sandbox_mode"):
        await router.confirm_setup("main-pc", "model-a", "high", sandbox_mode="unknown")


@pytest.mark.asyncio
async def test_confirm_setup_rejects_invalid_reviewer() -> None:
    router, _, _ = make_router(LOCAL)

    with pytest.raises(ExecutionTargetError, match="approvals_reviewer"):
        await router.confirm_setup(
            "main-pc", "model-a", "high", approvals_reviewer="guardian_subagent"
        )


@pytest.mark.asyncio
async def test_remote_start_forwards_manual_reviewer_without_retrying_unsupported_argument() -> (
    None
):
    class UnsupportedReviewerRemote(FakeRemote):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((name, arguments))
            raise RuntimeError("unsupported approvals_reviewer")

    remote = UnsupportedReviewerRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    with pytest.raises(RuntimeError, match="unsupported approvals_reviewer"):
        await router.codex_start(
            "Z:/Notebook/repo", "prompt", "notebook", approvals_reviewer="user"
        )

    assert remote.calls == [
        (
            "codex_start",
            {
                "cwd": "Z:/Notebook/repo",
                "prompt": "prompt",
                "approvals_reviewer": "user",
            },
        )
    ]


@pytest.mark.asyncio
async def test_start_rejects_invalid_reviewer_before_target_call() -> None:
    router, bridge, remotes = make_router(LOCAL, NOTEBOOK)

    with pytest.raises(ExecutionTargetError, match="approvals_reviewer"):
        await router.codex_start(
            "Z:/Notebook/repo", "prompt", "notebook", approvals_reviewer="guardian_subagent"
        )

    assert bridge.calls == []
    assert remotes["notebook"].calls == []


@pytest.mark.asyncio
async def test_old_remote_refuses_full_access_start() -> None:
    router, _, remotes = make_router(LOCAL, NOTEBOOK)

    with pytest.raises(ExecutionTargetError, match="sandbox_mode is not supported"):
        await router.codex_start(
            "Z:/Notebook/repo", "prompt", "notebook", sandbox_mode="danger-full-access"
        )

    assert [name for name, _ in remotes["notebook"].calls] == ["codex_setup_capabilities"]


@pytest.mark.asyncio
async def test_routed_thread_operations_keep_target_affinity() -> None:
    remote = FakeRemote({"ok": True, "thread_id": "native-remote"})
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK, OTHER), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    await router.codex_continue("notebook::native-remote", "continue")
    await router.codex_wait("notebook::native-remote", "turn-1", 8)
    await router.codex_steer("notebook::native-remote", "turn-1", "steer")
    await router.codex_interrupt("notebook::native-remote", "turn-1")
    await router.codex_status("notebook::native-remote", "turn-1", 9)

    assert [call[0] for call in remote.calls] == [
        "codex_continue",
        "codex_wait",
        "codex_steer",
        "codex_interrupt",
        "codex_status",
    ]
    assert all(call[1]["thread_id"] == "native-remote" for call in remote.calls)
    assert remote.calls[1][1]["timeout_seconds"] == 8


@pytest.mark.asyncio
async def test_remote_failure_does_not_fallback_to_another_target() -> None:
    first = FakeRemote()
    second = FakeRemote()
    first.error = ExecutionTargetError("execution target is unavailable")
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK, OTHER),
        lambda: FakeBridge(),
        remote_clients={"notebook": first, "other": second},
    )

    with pytest.raises(ExecutionTargetError, match="unavailable"):
        await router.codex_status("notebook::native-id")

    assert not second.calls


@pytest.mark.asyncio
async def test_unknown_or_mismatched_routed_thread_target_is_rejected() -> None:
    router, _, _ = make_router(LOCAL, NOTEBOOK)

    with pytest.raises(ExecutionTargetError, match="unknown execution target"):
        await router.codex_status("unknown::native")
    with pytest.raises(ExecutionTargetError, match="does not match"):
        await router.codex_threads("notebook::native", target_id="main-pc")


@pytest.mark.asyncio
async def test_remote_thread_list_is_scoped_and_native_id_can_be_adopted() -> None:
    remote = FakeRemote({"threads": [{"id": "native-remote"}], "next_cursor": "remote-cursor"})
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    listing = await router.codex_threads(target_id="notebook", cursor="remote-only")
    adopted = await router.codex_threads("native-existing", target_id="notebook")

    assert remote.calls[0][1]["cursor"] == "remote-only"
    assert listing["next_cursor"] == "remote-cursor"
    assert listing["threads"][0] == {
        "id": "notebook::native-remote",
        "native_thread_id": "native-remote",
        "target_id": "notebook",
    }
    assert remote.calls[1][1]["thread_id"] == "native-existing"
    assert adopted["threads"][0]["id"] == "notebook::native-remote"


@pytest.mark.asyncio
async def test_thread_list_requires_selection_when_multiple_targets_are_configured() -> None:
    router, _, _ = make_router(LOCAL, NOTEBOOK, OTHER)

    with pytest.raises(ExecutionTargetError, match="target_id is required"):
        await router.codex_threads()


@pytest.mark.parametrize("native_id", [1, "1"])
def test_request_handles_preserve_original_request_id_type_and_scope(native_id: int | str) -> None:
    handle = _request_handle("notebook", native_id)

    assert handle.startswith("notebook::request::")
    assert _parse_request_handle(handle) == ("notebook", native_id)
    assert _request_handle("other", native_id) != handle


@pytest.mark.parametrize(
    "payload",
    [
        "e30",  # Empty object has no original request ID.
        "eyJyZXF1ZXN0X2lkIjp0cnVlfQ",  # Boolean is not a valid integer request ID.
        "eyJyZXF1ZXN0X2lkIjoxLCJleHRyYSI6Mn0",  # Payload contains more than the original ID.
    ],
)
def test_malformed_request_handle_payload_is_rejected(payload: str) -> None:
    with pytest.raises(ExecutionTargetError, match="malformed"):
        _parse_request_handle(f"notebook::request::{payload}")


@pytest.mark.asyncio
async def test_remote_pending_request_uses_target_handle_and_is_rewritten() -> None:
    remote = FakeRemote(
        {
            "thread_id": "native-remote",
            "pending_request": {"thread_id": "native-remote", "request_id": 1},
            "latest_activity": {"thread_id": "native-remote"},
            "recent_activities": [{"thread_id": "native-remote"}],
        }
    )
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    result = await router.codex_status("notebook::native-remote")

    handle = result["pending_request"]["request_id"]
    assert _parse_request_handle(handle) == ("notebook", 1)
    assert result["thread_id"] == "notebook::native-remote"
    assert result["pending_request"]["thread_id"] == "notebook::native-remote"
    assert result["latest_activity"]["thread_id"] == "notebook::native-remote"
    assert result["recent_activities"][0]["thread_id"] == "notebook::native-remote"


@pytest.mark.asyncio
async def test_approval_and_user_input_decode_target_request_handles() -> None:
    remote = FakeRemote({"thread_id": "native-remote"})
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK), lambda: FakeBridge(), remote_clients={"notebook": remote}
    )

    await router.codex_approval(_request_handle("notebook", 1), "accept")
    await router.codex_user_input(_request_handle("notebook", "1"), {"q": ["answer"]})

    assert remote.calls == [
        ("codex_approval", {"request_id": 1, "decision": "accept"}),
        ("codex_user_input", {"request_id": "1", "answers": {"q": ["answer"]}}),
    ]


@pytest.mark.asyncio
async def test_local_pending_approval_id_takes_precedence_over_remote_handle() -> None:
    router, bridge, remotes = make_router(LOCAL, NOTEBOOK)
    collision_id = _request_handle("notebook", 1)
    bridge.pending_requests.add(collision_id)

    await router.codex_approval(collision_id, "accept")

    assert bridge.calls == [("approve", (collision_id, "accept"), {})]
    assert remotes["notebook"].calls == []


@pytest.mark.asyncio
async def test_local_pending_user_input_id_takes_precedence_over_remote_handle() -> None:
    router, bridge, remotes = make_router(LOCAL, NOTEBOOK)
    collision_id = _request_handle("notebook", 1)
    bridge.pending_requests.add(collision_id)

    await router.codex_user_input(collision_id, {"q": ["answer"]})

    assert bridge.calls == [("answer", (collision_id, {"q": ["answer"]}), {})]
    assert remotes["notebook"].calls == []


@pytest.mark.parametrize(
    "handle", ["bad::request::e30", "unknown::request::e30", "notebook::request::@@"]
)
@pytest.mark.asyncio
async def test_malformed_or_unknown_request_handles_are_rejected(handle: str) -> None:
    router, _, _ = make_router(LOCAL, NOTEBOOK)

    with pytest.raises(ExecutionTargetError):
        await router.codex_approval(handle, "accept")


@pytest.mark.asyncio
async def test_raw_local_request_id_remains_unchanged() -> None:
    router, bridge, _ = make_router(LOCAL)

    await router.codex_approval("local::native-request", "accept")

    assert bridge.calls[0][0:2] == ("approve", ("local::native-request", "accept"))


@pytest.mark.asyncio
async def test_remote_target_availability_is_independent() -> None:
    offline = FakeRemote()
    offline.available = False
    online = FakeRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK, OTHER),
        lambda: FakeBridge(),
        remote_clients={"notebook": offline, "other": online},
    )

    result = await router.target_list()

    assert result["selection_required"] is True
    assert result["targets"] == [
        {"id": "main-pc", "name": "Main PC", "kind": "local", "available": True},
        {
            "id": "notebook",
            "name": "Notebook PC",
            "kind": "remote",
            "available": False,
        },
        {"id": "other", "name": "Other PC", "kind": "remote", "available": True},
    ]
    assert all("url" not in target for target in result["targets"])


class ConcurrentSession:
    def __init__(self) -> None:
        self.entered = 0
        self.one_entered = asyncio.Event()
        self.both_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        self.entered += 1
        self.one_entered.set()
        if self.entered == 2:
            self.both_entered.set()
        await self.release.wait()
        from mcp import types

        return types.CallToolResult(content=[], structuredContent={"ok": True})


@pytest.mark.asyncio
async def test_remote_tool_calls_on_one_target_are_not_serialized() -> None:
    client = ExecutionTargetClient(NOTEBOOK)
    client._connected = True
    session = ConcurrentSession()
    client._session = session

    calls = [
        asyncio.create_task(client.call_tool("codex_status", {"thread_id": str(index)}))
        for index in range(2)
    ]
    await asyncio.wait_for(session.both_entered.wait(), timeout=1)
    session.release.set()
    await asyncio.gather(*calls)


@pytest.mark.asyncio
async def test_remote_tool_calls_on_different_targets_are_not_serialized() -> None:
    first_client = ExecutionTargetClient(NOTEBOOK)
    second_client = ExecutionTargetClient(OTHER)
    first_client._connected = True
    second_client._connected = True
    first_session = ConcurrentSession()
    second_session = ConcurrentSession()
    first_client._session = first_session
    second_client._session = second_session

    calls = [
        asyncio.create_task(first_client.call_tool("codex_status", {"thread_id": "one"})),
        asyncio.create_task(second_client.call_tool("codex_status", {"thread_id": "two"})),
    ]
    await asyncio.wait_for(
        asyncio.gather(first_session.one_entered.wait(), second_session.one_entered.wait()),
        timeout=1,
    )
    first_session.release.set()
    second_session.release.set()
    await asyncio.gather(*calls)


class ToolListSession:
    def __init__(self, tools: set[str], targets: dict[str, Any]) -> None:
        self.tools = tools
        self.targets = targets

    async def list_tools(self, *, params=None):
        return types.ListToolsResult(
            tools=[types.Tool(name=name, inputSchema={"type": "object"}) for name in self.tools]
        )

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> types.CallToolResult:
        return types.CallToolResult(content=[], structured_content=self.targets)


@pytest.mark.asyncio
async def test_remote_compatibility_requires_all_tools_and_a_leaf_gateway() -> None:
    compatible = ExecutionTargetClient(NOTEBOOK)
    compatible._session = ToolListSession(
        set(_REQUIRED_TARGET_TOOLS),
        {"targets": [{"id": "local"}], "selection_required": False},
    )
    await compatible._validate_leaf()
    assert not _REQUIRED_TARGET_TOOLS.intersection(
        {"codex_setup", "codex_setup_capabilities", "codex_setup_confirm"}
    )

    missing = ExecutionTargetClient(NOTEBOOK)
    missing._session = ToolListSession(
        set(_REQUIRED_TARGET_TOOLS) - {"codex_steer"},
        {"targets": [{"id": "local"}], "selection_required": False},
    )
    with pytest.raises(ExecutionTargetError, match="protocol-incompatible"):
        await missing._validate_leaf()

    nested = ExecutionTargetClient(NOTEBOOK)
    nested._session = ToolListSession(
        set(_REQUIRED_TARGET_TOOLS),
        {"targets": [{"id": "local"}, {"id": "other"}], "selection_required": True},
    )
    with pytest.raises(ExecutionTargetError, match="single-target leaf"):
        await nested._validate_leaf()


def test_remote_result_extraction_prefers_structured_content() -> None:
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"source":"text"}')],
        structured_content={"source": "structured"},
    )

    assert _extract_dict_result(result) == {"source": "structured"}


def test_remote_result_extraction_accepts_one_json_text_content() -> None:
    result = types.CallToolResult(content=[types.TextContent(type="text", text='{"ok":true}')])

    assert _extract_dict_result(result) == {"ok": True}


def test_remote_input_required_and_error_results_are_not_success() -> None:
    with pytest.raises(ExecutionTargetError, match="input-required"):
        _extract_dict_result(types.InputRequiredResult(inputRequests={}, requestState="state"))

    with pytest.raises(ExecutionTargetError, match="upstream tool error"):
        _extract_dict_result(
            types.CallToolResult(
                content=[types.TextContent(type="text", text="unsafe detail")],
                isError=True,
            )
        )


class RaisingSession:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> None:
        self.calls += 1
        raise TimeoutError("private url and prompt must not leak")


@pytest.mark.asyncio
async def test_remote_write_timeout_is_not_retried_or_echoed() -> None:
    client = ExecutionTargetClient(NOTEBOOK)
    client._connected = True
    session = RaisingSession()
    client._session = session

    with pytest.raises(
        ExecutionTargetError, match="outcome unknown; request was not retried"
    ) as exc:
        await client.call_tool("codex_start", {"cwd": "private", "prompt": "private"})

    assert session.calls == 1
    assert "private" not in str(exc.value)


@pytest.mark.asyncio
async def test_remote_startup_failure_is_isolated_from_other_targets() -> None:
    offline = FakeRemote()
    offline.error = ExecutionTargetError("private endpoint URL")
    online = FakeRemote()
    router = ExecutionTargetRouter(
        (LOCAL, NOTEBOOK, OTHER),
        lambda: FakeBridge(),
        remote_clients={"notebook": offline, "other": online},
    )

    await router.start()
    result = await router.target_list()

    assert [item["available"] for item in result["targets"]] == [True, False, True]
    assert "private endpoint URL" not in str(result)
