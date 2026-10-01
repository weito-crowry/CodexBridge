from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from copy import deepcopy
from typing import Any, Protocol, cast

from mcp import Client, MCPError, types
from mcp.client.streamable_http import (  # type: ignore[attr-defined]
    create_mcp_http_client,
    streamable_http_client,
)

from .config import ExecutionTargetConfig
from .logging_utils import log_event
from .models import ApprovalDecision, RequestId, SandboxMode, validate_sandbox_mode
from .setup_capabilities import project_public_capabilities

_REQUIRED_TARGET_TOOLS = frozenset(
    {
        "codex_targets",
        "codex_start",
        "codex_continue",
        "codex_wait",
        "codex_steer",
        "codex_approval",
        "codex_user_input",
        "codex_interrupt",
        "codex_threads",
        "codex_status",
    }
)
_WRITE_TOOLS = frozenset(
    {
        "codex_start",
        "codex_continue",
        "codex_steer",
        "codex_approval",
        "codex_user_input",
        "codex_interrupt",
    }
)
_CONNECT_TIMEOUT_SECONDS = 3.0


class ExecutionTargetError(ValueError):
    """An execution target failed with a safe, user-facing message."""


class BridgePort(Protocol):
    def has_pending_request(self, request_id: RequestId) -> bool: ...

    async def start(
        self,
        cwd: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: SandboxMode | None = None,
    ) -> dict[str, Any]: ...

    async def model_capabilities(self) -> dict[str, Any]: ...

    async def continue_thread(self, thread_id: str, prompt: str) -> dict[str, Any]: ...

    async def wait(
        self, thread_id: str, turn_id: str, timeout_seconds: float | None = None
    ) -> dict[str, Any]: ...

    async def steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]: ...

    async def approve(
        self, request_id: RequestId, decision: ApprovalDecision
    ) -> dict[str, Any]: ...

    async def answer_user_input(
        self, request_id: RequestId, answers: dict[str, list[str]]
    ) -> dict[str, Any]: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]: ...

    async def threads(
        self,
        thread_id: str | None = None,
        *,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    async def status(
        self, thread_id: str, turn_id: str | None = None, activity_limit: int = 20
    ) -> dict[str, Any]: ...


def _extract_dict_result(result: object) -> dict[str, Any]:
    if isinstance(result, types.InputRequiredResult):
        raise ExecutionTargetError(
            "execution target returned an incompatible input-required result"
        )
    if not isinstance(result, types.CallToolResult):
        raise ExecutionTargetError("execution target returned an incompatible result")
    if result.is_error:
        raise ExecutionTargetError("execution target returned an upstream tool error")
    structured = result.structured_content
    if isinstance(structured, dict):
        return structured
    if len(result.content) == 1 and isinstance(result.content[0], types.TextContent):
        try:
            parsed = json.loads(result.content[0].text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ExecutionTargetError("execution target returned a malformed result")


class ExecutionTargetClient:
    """Persistent MCP connection for one remote leaf CodexBridge."""

    def __init__(
        self,
        config: ExecutionTargetConfig,
        *,
        http_client_factory: Any = create_mcp_http_client,
        transport_factory: Any = streamable_http_client,
        client_factory: Any = Client,
    ) -> None:
        if config.kind != "remote" or config.url is None:
            raise ValueError("execution target client requires a remote target")
        self.config = config
        self._http_client_factory = http_client_factory
        self._transport_factory = transport_factory
        self._client_factory = client_factory
        self._connect_lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._session: Any | None = None
        self._connected = False
        self._leaf_target_id: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                async with self._connect_lock:
                    if self._connected:
                        return
                    await self._close_stack()
                    await self._open()
                    await self._validate_leaf()
                    self._connected = True
            log_event("target.connect", target_id=self.config.id)
        except Exception:
            async with self._connect_lock:
                await self._close_stack()
            self._connected = False
            log_event("target.disconnect", target_id=self.config.id, error_category="unavailable")
            raise ExecutionTargetError("execution target is unavailable") from None

    async def _open(self) -> None:
        stack = AsyncExitStack()
        try:
            http_client = await stack.enter_async_context(self._http_client_factory())
            transport = self._transport_factory(
                self.config.url,
                http_client=http_client,
                terminate_on_close=True,
            )
            client = self._client_factory(transport, mode="auto", cache=None)
            await stack.enter_async_context(client)
            self._session = client.session
            self._stack = stack
        except BaseException:
            await stack.aclose()
            raise

    async def _list_tools(self, session: Any) -> set[str]:
        names: set[str] = set()
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            if cursor is None:
                page = await session.list_tools()
            else:
                page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            names.update(tool.name for tool in page.tools)
            next_cursor = page.next_cursor
            if next_cursor is None:
                return names
            if next_cursor in seen:
                raise ExecutionTargetError("execution target returned a tool-list pagination cycle")
            seen.add(next_cursor)
            cursor = next_cursor

    async def _validate_leaf(self) -> None:
        session = self._session
        if session is None:
            raise ExecutionTargetError("execution target is unavailable")
        if not _REQUIRED_TARGET_TOOLS.issubset(await self._list_tools(session)):
            raise ExecutionTargetError("execution target is protocol-incompatible")
        targets = _extract_dict_result(await session.call_tool("codex_targets", {}))
        rows = targets.get("targets")
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or targets.get("selection_required") is not False
            or not isinstance(rows[0], dict)
            or not isinstance(rows[0].get("id"), str)
            or not rows[0]["id"]
        ):
            raise ExecutionTargetError("execution target must be a single-target leaf")
        self._leaf_target_id = rows[0]["id"]

    async def _close_stack(self) -> None:
        stack = self._stack
        self._stack = None
        self._session = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def _session_for_call(self) -> Any:
        if not self._connected:
            await self.start()
        session = self._session
        if not self._connected or session is None:
            raise ExecutionTargetError("execution target is unavailable")
        return session

    async def probe(self) -> bool:
        try:
            session = await self._session_for_call()
            async with asyncio.timeout(_CONNECT_TIMEOUT_SECONDS):
                result = _extract_dict_result(await session.call_tool("codex_targets", {}))
            rows = result.get("targets")
            available = (
                isinstance(rows, list)
                and len(rows) == 1
                and result.get("selection_required") is False
            )
            self._connected = available
            return available
        except Exception:
            self._connected = False
            log_event("target.disconnect", target_id=self.config.id, error_category="unavailable")
            return False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = await self._session_for_call()
        try:
            result = await session.call_tool(name, arguments)
            return _extract_dict_result(result)
        except ExecutionTargetError:
            raise
        except Exception as exc:
            self._connected = False
            if isinstance(exc, MCPError) and exc.code not in {
                types.CONNECTION_CLOSED,
                types.REQUEST_TIMEOUT,
            }:
                raise ExecutionTargetError("execution target returned an upstream error") from None
            if name in _WRITE_TOOLS:
                raise ExecutionTargetError(
                    "execution target call outcome unknown; request was not retried"
                ) from None
            raise ExecutionTargetError("execution target call failed") from None

    async def setup_capabilities(self) -> dict[str, Any]:
        session = await self._session_for_call()
        target_id = self._leaf_target_id
        if target_id is None:
            targets = _extract_dict_result(await session.call_tool("codex_targets", {}))
            rows = targets.get("targets")
            if (
                not isinstance(rows, list)
                or len(rows) != 1
                or not isinstance(rows[0], dict)
                or not isinstance(rows[0].get("id"), str)
            ):
                raise ExecutionTargetError("execution target is unavailable")
            target_id = rows[0]["id"]
            self._leaf_target_id = target_id
        return await self.call_tool("codex_setup_capabilities", {"target_id": target_id})

    async def close(self) -> None:
        async with self._connect_lock:
            was_connected = self._connected
            self._connected = False
            await self._close_stack()
        if was_connected:
            log_event("target.disconnect", target_id=self.config.id, error_category="shutdown")


def _public_thread_id(target: ExecutionTargetConfig, native_thread_id: str) -> str:
    return native_thread_id if target.kind == "local" else f"{target.id}::{native_thread_id}"


def _request_handle(target_id: str, request_id: object) -> str:
    if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
        raise ExecutionTargetError("remote pending request id is malformed")
    payload = json.dumps({"request_id": request_id}, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{target_id}::request::{encoded}"


def _parse_request_handle(value: RequestId) -> tuple[str, RequestId] | None:
    if not isinstance(value, str) or "::request::" not in value:
        return None
    parts = value.split("::")
    if len(parts) != 3 or parts[1] != "request" or not parts[0] or not parts[2]:
        raise ExecutionTargetError("request handle is malformed")
    encoded = parts[2]
    if any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        raise ExecutionTargetError("request handle is malformed")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ExecutionTargetError("request handle is malformed") from None
    request_id = (
        decoded.get("request_id")
        if isinstance(decoded, dict) and set(decoded) == {"request_id"}
        else None
    )
    if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
        raise ExecutionTargetError("request handle is malformed")
    return parts[0], request_id


class ExecutionTargetRouter:
    """Route public MCP execution tools to a local bridge or configured remote leaf."""

    def __init__(
        self,
        targets: tuple[ExecutionTargetConfig, ...],
        local_bridge: Any,
        *,
        remote_clients: Mapping[str, Any] | None = None,
    ) -> None:
        self.targets = targets
        self._local_bridge = local_bridge
        supplied = {} if remote_clients is None else dict(remote_clients)
        self._remote_clients = {
            target.id: supplied.get(target.id, ExecutionTargetClient(target))
            for target in targets
            if target.kind == "remote"
        }
        self._by_id = {target.id: target for target in targets}
        self._local = next(target for target in targets if target.kind == "local")

    async def start(self) -> None:
        await asyncio.gather(
            *(client.start() for client in self._remote_clients.values()),
            return_exceptions=True,
        )

    async def close(self) -> None:
        await asyncio.gather(
            *(client.close() for client in self._remote_clients.values()),
            return_exceptions=True,
        )

    async def target_list(self) -> dict[str, Any]:
        remote_ids = tuple(self._remote_clients)
        availability = await asyncio.gather(
            *(self._remote_clients[target_id].probe() for target_id in remote_ids),
            return_exceptions=True,
        )
        states = {
            target_id: result is True
            for target_id, result in zip(remote_ids, availability, strict=True)
        }
        return {
            "targets": [
                {
                    "id": target.id,
                    "name": target.name,
                    "kind": target.kind,
                    "available": True if target.kind == "local" else states[target.id],
                }
                for target in self.targets
            ],
            "selection_required": len(self.targets) > 1,
        }

    @staticmethod
    def _capability_error(target: dict[str, Any]) -> dict[str, Any]:
        return {
            "target": target,
            "models": [],
            "defaults": {"model": None, "reasoning_effort": None},
            "execution_modes": [{"id": "inherit", "display_name": "Default"}],
            "error": {
                "code": "capabilities_unavailable",
                "message": "Model capabilities are unavailable for this target.",
            },
        }

    async def setup_capabilities(self, target_id: str) -> dict[str, Any]:
        target = self._select_target(target_id)
        public_target = {
            "id": target.id,
            "name": target.name,
            "kind": target.kind,
            "available": True,
        }
        try:
            if target.kind == "local":
                capabilities = await self._bridge().model_capabilities()
            else:
                client = self._remote_clients[target.id]
                if not await client.probe():
                    public_target["available"] = False
                    return self._capability_error(public_target)
                capabilities = await client.setup_capabilities()
            if capabilities.get("error") is not None:
                return self._capability_error(public_target)
            return {"target": public_target, **project_public_capabilities(capabilities)}
        except Exception:
            return self._capability_error(public_target)

    async def confirm_setup(
        self,
        target_id: str,
        model: str,
        reasoning_effort: str,
        sandbox_mode: SandboxMode = "inherit",
    ) -> dict[str, Any]:
        try:
            sandbox_mode = validate_sandbox_mode(sandbox_mode) or "inherit"
        except ValueError as exc:
            raise ExecutionTargetError(str(exc)) from None
        capabilities = await self.setup_capabilities(target_id)
        target = capabilities["target"]
        if not target["available"]:
            raise ExecutionTargetError("execution target is unavailable")
        if "error" in capabilities:
            raise ExecutionTargetError("model capabilities are unavailable for this target")
        selected = next(
            (
                entry
                for entry in capabilities["models"]
                if isinstance(entry, dict) and entry.get("model") == model
            ),
            None,
        )
        if selected is None:
            raise ExecutionTargetError("selected model is no longer available on this target")
        efforts = selected.get("reasoning_efforts")
        if not isinstance(efforts, list) or not any(
            isinstance(entry, dict) and entry.get("id") == reasoning_effort for entry in efforts
        ):
            raise ExecutionTargetError("selected reasoning effort is not supported by this model")
        if not any(
            isinstance(entry, dict) and entry.get("id") == sandbox_mode
            for entry in capabilities.get("execution_modes", [])
        ):
            raise ExecutionTargetError("selected sandbox_mode is not supported by this target")
        return {
            "confirmed": True,
            "selection": {
                "target_id": target["id"],
                "target_name": target["name"],
                "model": model,
                "reasoning_effort": reasoning_effort,
                "sandbox_mode": sandbox_mode,
            },
        }

    def _select_target(self, target_id: str | None) -> ExecutionTargetConfig:
        if target_id is None:
            if len(self.targets) != 1:
                raise ExecutionTargetError(
                    "target_id is required because multiple execution targets are configured; "
                    "call codex_targets and ask the user which machine to use"
                )
            return self.targets[0]
        target = self._by_id.get(target_id)
        if target is None:
            raise ExecutionTargetError("unknown execution target")
        return target

    def _thread_target(
        self, thread_id: str, target_id: str | None = None
    ) -> tuple[ExecutionTargetConfig, str]:
        if "::" in thread_id:
            target_prefix, native_id = thread_id.split("::", 1)
            target = self._by_id.get(target_prefix)
            if target is None or target.kind != "remote" or not native_id:
                raise ExecutionTargetError("thread id contains an unknown execution target")
            if target_id is not None and target_id != target.id:
                raise ExecutionTargetError("thread id does not match target_id")
            return target, native_id
        target = self._local if target_id is None else self._select_target(target_id)
        return target, thread_id

    def _bridge(self) -> BridgePort:
        value = self._local_bridge()
        if value is None:
            raise RuntimeError("CodexBridge runtime is not started")
        return cast(BridgePort, value)

    async def _call(
        self,
        target: ExecutionTargetConfig,
        name: str,
        arguments: dict[str, Any],
        local_call: Any,
    ) -> dict[str, Any]:
        if target.kind == "local":
            result = await local_call()
            return self._rewrite_result(target, result)
        log_event("target.route", target_id=target.id, tool_name=name)
        result = await self._remote_clients[target.id].call_tool(name, arguments)
        return self._rewrite_result(target, result)

    def _rewrite_result(
        self, target: ExecutionTargetConfig, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = deepcopy(dict(value))
        result["target_id"] = target.id

        def rewrite_thread_field(obj: object) -> None:
            if not isinstance(obj, dict):
                return
            native = obj.get("thread_id")
            if isinstance(native, str):
                obj["native_thread_id"] = native
                obj["thread_id"] = _public_thread_id(target, native)
                obj["target_id"] = target.id

        rewrite_thread_field(result)
        pending = result.get("pending_request")
        if isinstance(pending, dict):
            rewrite_thread_field(pending)
            request_id = pending.get("request_id")
            if target.kind == "remote" and request_id is not None:
                pending["request_id"] = _request_handle(target.id, request_id)
        rewrite_thread_field(result.get("latest_activity"))
        recent = result.get("recent_activities")
        if isinstance(recent, list):
            for activity in recent:
                rewrite_thread_field(activity)

        for key in ("thread",):
            row = result.get(key)
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                native = row["id"]
                row["native_thread_id"] = native
                row["id"] = _public_thread_id(target, native)
                row["target_id"] = target.id
        rows = result.get("threads")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    native = row["id"]
                    row["native_thread_id"] = native
                    row["id"] = _public_thread_id(target, native)
                    row["target_id"] = target.id
        return result

    async def codex_start(
        self,
        cwd: str,
        prompt: str,
        target_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox_mode: SandboxMode | None = None,
    ) -> dict[str, Any]:
        try:
            sandbox_mode = validate_sandbox_mode(sandbox_mode)
        except ValueError as exc:
            raise ExecutionTargetError(str(exc)) from None
        if reasoning_effort is not None and model is None:
            raise ExecutionTargetError("reasoning_effort requires an explicit model")
        target = self._select_target(target_id)
        capabilities: dict[str, Any] | None = None
        if model is not None:
            capabilities = await self.setup_capabilities(target.id)
            if not capabilities["target"]["available"]:
                raise ExecutionTargetError("execution target is unavailable")
            if "error" in capabilities:
                raise ExecutionTargetError("model capabilities are unavailable for this target")
            selected = next(
                (
                    entry
                    for entry in capabilities["models"]
                    if isinstance(entry, dict) and entry.get("model") == model
                ),
                None,
            )
            if selected is None:
                raise ExecutionTargetError("selected model is no longer available on this target")
            if reasoning_effort is not None:
                efforts = selected.get("reasoning_efforts")
                if not isinstance(efforts, list) or not any(
                    isinstance(entry, dict) and entry.get("id") == reasoning_effort
                    for entry in efforts
                ):
                    raise ExecutionTargetError(
                        "selected reasoning effort is not supported by this model"
                    )

        if target.kind == "remote" and sandbox_mode == "danger-full-access":
            if capabilities is None:
                capabilities = await self.setup_capabilities(target.id)
            if not any(
                isinstance(entry, dict) and entry.get("id") == sandbox_mode
                for entry in capabilities.get("execution_modes", [])
            ):
                raise ExecutionTargetError("selected sandbox_mode is not supported by this target")

        arguments: dict[str, Any] = {"cwd": cwd, "prompt": prompt}
        if model is not None:
            arguments["model"] = model
        if reasoning_effort is not None:
            arguments["reasoning_effort"] = reasoning_effort
        if sandbox_mode == "danger-full-access":
            arguments["sandbox_mode"] = sandbox_mode
        bridge = self._bridge() if target.kind == "local" else None

        async def local_start() -> dict[str, Any] | None:
            if bridge is None:
                return None
            if model is None and reasoning_effort is None:
                if sandbox_mode is None:
                    return await bridge.start(cwd, prompt)
                return await bridge.start(cwd, prompt, sandbox_mode=sandbox_mode)
            return await bridge.start(
                cwd,
                prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                **({"sandbox_mode": sandbox_mode} if sandbox_mode is not None else {}),
            )

        result = await self._call(
            target,
            "codex_start",
            arguments,
            local_start,
        )
        if model is not None or sandbox_mode is not None:
            execution_config: dict[str, Any] = {
                "target_id": target.id,
                "model": model,
                "reasoning_effort": reasoning_effort,
            }
            if sandbox_mode is not None:
                execution_config["sandbox_mode"] = sandbox_mode
            result["execution_config"] = execution_config
        return result

    async def _thread_call(
        self,
        name: str,
        thread_id: str,
        arguments: dict[str, Any],
        local_call: Any,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        target, native_id = self._thread_target(thread_id, target_id)
        args = dict(arguments)
        args["thread_id"] = native_id
        return await self._call(target, name, args, local_call(native_id))

    async def codex_continue(
        self, thread_id: str, prompt: str, target_id: str | None = None
    ) -> dict[str, Any]:
        return await self._thread_call(
            "codex_continue",
            thread_id,
            {"prompt": prompt},
            lambda native: lambda: self._bridge().continue_thread(native, prompt),
            target_id,
        )

    async def codex_wait(
        self,
        thread_id: str,
        turn_id: str,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"turn_id": turn_id}
        if timeout_seconds is not None:
            args["timeout_seconds"] = timeout_seconds
        return await self._thread_call(
            "codex_wait",
            thread_id,
            args,
            lambda native: lambda: self._bridge().wait(native, turn_id, timeout_seconds),
        )

    async def codex_steer(self, thread_id: str, turn_id: str, prompt: str) -> dict[str, Any]:
        return await self._thread_call(
            "codex_steer",
            thread_id,
            {"turn_id": turn_id, "prompt": prompt},
            lambda native: lambda: self._bridge().steer(native, turn_id, prompt),
        )

    async def codex_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self._thread_call(
            "codex_interrupt",
            thread_id,
            {"turn_id": turn_id},
            lambda native: lambda: self._bridge().interrupt(native, turn_id),
        )

    async def codex_status(
        self, thread_id: str, turn_id: str | None = None, activity_limit: int = 20
    ) -> dict[str, Any]:
        return await self._thread_call(
            "codex_status",
            thread_id,
            {"turn_id": turn_id, "activity_limit": activity_limit},
            lambda native: lambda: self._bridge().status(native, turn_id, activity_limit),
        )

    async def codex_threads(
        self,
        thread_id: str | None = None,
        *,
        target_id: str | None = None,
        include_history: bool = False,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if thread_id is None:
            target = self._select_target(target_id)
            native_id = None
        else:
            target, native_id = self._thread_target(thread_id, target_id)
        arguments: dict[str, Any] = {
            "thread_id": native_id,
            "include_history": include_history,
            "limit": limit,
            "cursor": cursor,
        }
        if target.kind == "remote":
            result = await self._remote_clients[target.id].call_tool("codex_threads", arguments)
            return self._rewrite_result(target, result)
        return self._rewrite_result(
            target,
            await self._bridge().threads(
                native_id,
                include_history=include_history,
                limit=limit,
                cursor=cursor,
            ),
        )

    async def _request_call(
        self, name: str, request_id: RequestId, values: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            raise ExecutionTargetError("request id is malformed")
        bridge = self._bridge()
        if bridge.has_pending_request(request_id):
            target = self._local
            native_request_id = request_id
        else:
            handle = _parse_request_handle(request_id)
            if handle is None:
                target = self._local
                native_request_id = request_id
            else:
                target_id, native_request_id = handle
                selected_target = self._by_id.get(target_id)
                if selected_target is None or selected_target.kind != "remote":
                    raise ExecutionTargetError("request handle references an unknown target")
                target = selected_target
        arguments = {"request_id": native_request_id, **values}
        if target.kind == "remote":
            result = await self._remote_clients[target.id].call_tool(name, arguments)
        elif name == "codex_approval":
            result = await bridge.approve(
                native_request_id, cast(ApprovalDecision, values["decision"])
            )
        else:
            result = await bridge.answer_user_input(
                native_request_id, cast(dict[str, list[str]], values["answers"])
            )
        return self._rewrite_result(target, result)

    async def codex_approval(
        self, request_id: RequestId, decision: ApprovalDecision
    ) -> dict[str, Any]:
        return await self._request_call("codex_approval", request_id, {"decision": decision})

    async def codex_user_input(
        self, request_id: RequestId, answers: dict[str, list[str]]
    ) -> dict[str, Any]:
        return await self._request_call("codex_user_input", request_id, {"answers": answers})
