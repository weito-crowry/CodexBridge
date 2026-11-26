"""Codex UI API implementation for the D2-I3 Agent Provider contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..paths import AllowedPathPolicy, PathPolicyError
from .agent_contracts import (
    AgentContractError,
    AgentRequest,
    DispatchReceipt,
    Observation,
    OptionCapability,
    PreparedProviderRequest,
    ProviderCapabilities,
    TargetContext,
)
from .codex_job_adapter import CodexJobAdapter
from .http_client import (
    CodexAgentHTTPClient,
    CodexHTTPError,
    CodexHTTPStatusError,
    CodexPreflightError,
    CodexProtocolError,
    validate_agent_thread_payload,
)

_TARGET = "default"
_BRIDGE_STATES = frozenset(
    {
        "in_progress",
        "needs_approval",
        "needs_input",
        "completed",
        "failed",
        "interrupted",
        "not_loaded",
        "unknown",
    }
)
_ACTIVE_STATES = frozenset({"in_progress", "needs_approval", "needs_input"})
_TERMINAL_STATES = frozenset({"completed", "failed", "interrupted"})
_CODEX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_MAX_PROMPT_CHARS = 32_000
_MAX_REQUEST_BYTES = 65_536
_MAX_RESPONSE_BYTES = 1_048_576


class CodexAgentProvider:
    """A local Bridge provider with wait=true invoke and internal exact-turn observe."""

    def __init__(
        self,
        *,
        port: int,
        allowed_roots: tuple[str, ...],
        workspace_root: str,
        http_client: CodexAgentHTTPClient | None = None,
    ) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise AgentContractError("unsupported_target")
        if type(allowed_roots) is not tuple or not allowed_roots:
            raise AgentContractError("unsupported_target")
        if type(workspace_root) is not str:
            raise AgentContractError("unsupported_target")
        try:
            policy = AllowedPathPolicy(allowed_roots)
            workspace = policy.validate_cwd(workspace_root)
        except (PathPolicyError, OSError):
            raise AgentContractError("unsupported_target") from None
        self._workspace = workspace
        self._context = TargetContext(
            "codex",
            _TARGET,
            1,
            endpoint=f"http://127.0.0.1:{port}",
            workspace=self._workspace,
        )
        self._client = http_client or CodexAgentHTTPClient(port=port)
        if getattr(self._client, "port", port) != port:
            raise AgentContractError("unsupported_target")

    def capabilities(self, target: str) -> ProviderCapabilities:
        if type(target) is not str or target != _TARGET:
            raise AgentContractError("unsupported_target")
        return ProviderCapabilities(
            provider="codex",
            target=_TARGET,
            operations=("invoke", "observe"),
            session_modes=("new",),
            option_rules=(
                OptionCapability("title", ("new",), ("string",)),
                OptionCapability(
                    "sandbox_mode",
                    ("new",),
                    ("string",),
                    allowed_values=('"inherit"',),
                ),
                OptionCapability(
                    "confirm_full_access",
                    ("new",),
                    ("boolean",),
                    allowed_values=("false",),
                ),
            ),
            usage_supported=False,
            transport_constraints={
                "max_request_bytes": _MAX_REQUEST_BYTES,
                "max_prompt_characters": _MAX_PROMPT_CHARS,
                "max_response_bytes": _MAX_RESPONSE_BYTES,
            },
            contract_version=self._context.contract_version,
            endpoint=self._context.endpoint,
            workspace=self._context.workspace,
        )

    def prepare(
        self,
        request: AgentRequest,
        target_context: TargetContext,
    ) -> PreparedProviderRequest:
        if type(target_context) is not TargetContext or target_context != self._context:
            raise AgentContractError("unsupported_target")
        if type(request) is not AgentRequest or request.target_context != self._context:
            raise AgentContractError("unsupported_target")
        if request.operation != "invoke":
            raise AgentContractError("unsupported_operation")
        if request.session is None or request.session.get("mode") != "new":
            raise AgentContractError("unsupported_operation")
        if request.wait is not True:
            raise AgentContractError("unsupported_operation")
        prompt = request.prompt
        if type(prompt) is not str or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS:
            raise AgentContractError("invalid_request")
        try:
            prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise AgentContractError("invalid_request") from None
        options = request.options or {}
        if set(options) - {"title", "sandbox_mode", "confirm_full_access"}:
            raise AgentContractError("unsupported_option")
        if "title" in options and (
            type(options["title"]) is not str or not options["title"].strip()
        ):
            raise AgentContractError("invalid_request")
        if options.get("sandbox_mode", "inherit") != "inherit":
            raise AgentContractError("unsupported_option")
        if options.get("confirm_full_access", False) is not False:
            raise AgentContractError("unsupported_option")
        prepared = PreparedProviderRequest(request, self._context)
        self._validate_prepared(prepared)
        return prepared

    def preflight(self, prepared: PreparedProviderRequest) -> None:
        self._validate_prepared(prepared)
        try:
            self._client.preflight()
        except CodexPreflightError as error:
            message = str(error).casefold()
            code = "unauthorized" if "token" in message else "preflight_failed"
            raise AgentContractError(code) from None
        except CodexHTTPError:
            raise AgentContractError("preflight_failed") from None

    def dispatch(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> DispatchReceipt:
        try:
            self._validate_prepared(prepared)
            if (
                type(client_request_id) is not str
                or re.fullmatch(r"cba-[0-9a-f]{64}", client_request_id, re.ASCII) is None
            ):
                return DispatchReceipt.rejected("invalid_request")
            body = self._request_body(prepared, client_request_id)
            validate_agent_thread_payload(body)
            response = self._client.create_agent_thread(body)
        except CodexHTTPStatusError as error:
            if error.status_code == 403:
                return DispatchReceipt.rejected("unauthorized")
            if error.status_code == 413:
                return DispatchReceipt.rejected("request_too_large")
            if error.status_code == 404:
                return DispatchReceipt.rejected("dispatch_rejected")
            return DispatchReceipt.uncertain()
        except (CodexHTTPError, OSError, TimeoutError):
            return DispatchReceipt.uncertain()
        except AgentContractError:
            return DispatchReceipt.uncertain()
        except Exception:
            return DispatchReceipt.uncertain()
        if (
            type(response) is not dict
            or not {"thread_id", "turn_id", "state"}.issubset(response)
            or type(response.get("thread_id")) is not str
            or type(response.get("turn_id")) is not str
            or _CODEX_ID.fullmatch(response["thread_id"]) is None
            or _CODEX_ID.fullmatch(response["turn_id"]) is None
            or type(response.get("state")) is not str
            or response["state"] not in _ACTIVE_STATES | _TERMINAL_STATES
        ):
            return DispatchReceipt.uncertain()
        return DispatchReceipt.accepted(
            handle={"thread_id": response["thread_id"], "turn_id": response["turn_id"]},
            remote_state=response["state"],
        )

    def observe(self, handle: object, target_context: TargetContext) -> Observation:
        if type(target_context) is not TargetContext or target_context != self._context:
            return _unknown_observation(handle)
        parsed_handle = _validate_handle(handle)
        if parsed_handle is None:
            return _unknown_observation(handle)
        thread_id = parsed_handle["thread_id"]
        turn_id = parsed_handle["turn_id"]
        try:
            status = self._client.get_agent_thread_status(thread_id, turn_id)
            if (
                type(status) is not dict
                or status.get("thread_id") != thread_id
                or status.get("turn_id") != turn_id
                or type(status.get("state")) is not str
            ):
                return _unknown_observation(parsed_handle)
            status_state = status["state"]
            if status_state == "in_progress":
                return Observation("RUNNING", handle=parsed_handle, remote_state=status_state)
            if status_state in {"needs_approval", "needs_input"}:
                return Observation(
                    "BLOCKED",
                    handle=parsed_handle,
                    remote_state=status_state,
                    error={"code": status_state, "message": _safe_message(status_state)},
                )
            if status_state not in _TERMINAL_STATES | {"not_loaded", "unknown"}:
                return _unknown_observation(parsed_handle)
            exact_state = self._find_exact_turn_state(thread_id, turn_id)
            if exact_state is None:
                return _unknown_observation(parsed_handle)
            if status_state in _TERMINAL_STATES and exact_state != status_state:
                return _unknown_observation(parsed_handle)
            if exact_state == "in_progress":
                return Observation("RUNNING", handle=parsed_handle, remote_state=exact_state)
            if exact_state in {"needs_approval", "needs_input"}:
                return Observation(
                    "BLOCKED",
                    handle=parsed_handle,
                    remote_state=exact_state,
                    error={"code": exact_state, "message": _safe_message(exact_state)},
                )
            if exact_state == "failed":
                return Observation(
                    "FAILED",
                    handle=parsed_handle,
                    remote_state=exact_state,
                    error={"code": "remote_failed", "message": _safe_message("remote_failed")},
                )
            if exact_state == "interrupted":
                return Observation(
                    "INTERRUPTED",
                    handle=parsed_handle,
                    remote_state=exact_state,
                    error={
                        "code": "remote_interrupted",
                        "message": _safe_message("remote_interrupted"),
                    },
                )
            if exact_state == "completed":
                final_text = self._final_answer(thread_id, turn_id)
                if final_text is None:
                    return Observation(
                        "UNKNOWN",
                        handle=parsed_handle,
                        remote_state=exact_state,
                        error={
                            "code": "response_unavailable",
                            "message": _safe_message("response_unavailable"),
                        },
                    )
                return Observation(
                    "COMPLETED",
                    handle=parsed_handle,
                    remote_state=exact_state,
                    final_text=final_text,
                )
            return _unknown_observation(parsed_handle)
        except Exception:
            return _unknown_observation(parsed_handle)

    def interrupt(
        self,
        handle: object,
        target_context: TargetContext,
        client_request_id: str,
    ) -> DispatchReceipt:
        del handle, target_context, client_request_id
        raise AgentContractError("unsupported_operation")

    def _validate_prepared(self, prepared: PreparedProviderRequest) -> None:
        if type(prepared) is not PreparedProviderRequest:
            raise AgentContractError("invalid_request")
        try:
            normalized = PreparedProviderRequest.from_json(prepared.to_json())
        except Exception:
            raise AgentContractError("invalid_request") from None
        if (
            normalized.operation != "invoke"
            or normalized.provider != "codex"
            or normalized.target != _TARGET
            or normalized.target_identity != self._context.to_dict()
            or normalized.wait is not True
            or normalized.session is None
            or normalized.session.get("mode") != "new"
        ):
            raise AgentContractError("unsupported_operation")
        request = _request_from_prepared(normalized)
        prompt = request.prompt
        options = request.options or {}
        if (
            type(prompt) is not str
            or not prompt.strip()
            or len(prompt) > _MAX_PROMPT_CHARS
            or set(options) - {"title", "sandbox_mode", "confirm_full_access"}
            or (
                "title" in options
                and (type(options["title"]) is not str or not options["title"].strip())
            )
            or options.get("sandbox_mode", "inherit") != "inherit"
            or options.get("confirm_full_access", False) is not False
        ):
            raise AgentContractError("unsupported_option")
        body = self._request_body_unchecked(normalized, "cba-" + "0" * 64)
        try:
            validate_agent_thread_payload(body)
        except CodexProtocolError:
            raise AgentContractError("request_too_large") from None

    def _request_body(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> dict[str, Any]:
        self._validate_prepared(prepared)
        return self._request_body_unchecked(prepared, client_request_id)

    def _request_body_unchecked(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> dict[str, Any]:
        request = _request_from_prepared(prepared)
        options = request.options or {}
        body: dict[str, Any] = {
            "client_request_id": client_request_id,
            "cwd": self._workspace,
            "prompt": request.prompt,
            "sandbox_mode": options.get("sandbox_mode", "inherit"),
            "confirm_full_access": options.get("confirm_full_access", False),
        }
        if "title" in options:
            body["title"] = options["title"]
        return body

    def _find_exact_turn_state(self, thread_id: str, turn_id: str) -> str | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matched_state: str | None = None
        found = False
        while True:
            page = self._client.read_agent_turns_page(thread_id, cursor=cursor)
            if type(page) is not dict or page.get("thread_id") != thread_id:
                return None
            turns = page.get("turns")
            if type(turns) is not list:
                return None
            for turn in turns:
                if not isinstance(turn, Mapping):
                    continue
                candidate_id = turn.get("id")
                if candidate_id == turn_id:
                    if found or type(turn.get("status")) is not str:
                        return None
                    found = True
                    matched_state = turn["status"]
            next_cursor = page.get("next_cursor")
            truncated = page.get("truncated", False)
            if type(truncated) is not bool:
                return None
            if next_cursor is None:
                if truncated:
                    return None
                break
            if type(next_cursor) is not str or not next_cursor or next_cursor in seen_cursors:
                return None
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return matched_state if found and matched_state in _BRIDGE_STATES else None

    def _final_answer(self, thread_id: str, turn_id: str) -> str | None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        found_text: str | None = None
        found_answer = False
        while True:
            page = self._client.read_agent_items_page(thread_id, turn_id, cursor=cursor)
            if (
                type(page) is not dict
                or page.get("thread_id") != thread_id
                or page.get("turn_id") != turn_id
                or type(page.get("items")) is not list
            ):
                return None
            items = page["items"]
            for entry in items:
                if not isinstance(entry, Mapping) or entry.get("turn_id") != turn_id:
                    return None
            safe_final_count = sum(
                CodexJobAdapter._final_text([entry], turn_id) is not None
                for entry in items
                if isinstance(entry, dict)
            )
            if safe_final_count > 1:
                return None
            page_text = CodexJobAdapter._final_text(items, turn_id)
            if page_text is not None:
                if found_answer:
                    return None
                found_answer = True
                found_text = page_text
            next_cursor = page.get("next_cursor")
            truncated = page.get("truncated", False)
            if type(truncated) is not bool:
                return None
            if next_cursor is None:
                if truncated:
                    return None
                break
            if type(next_cursor) is not str or not next_cursor or next_cursor in seen_cursors:
                return None
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return found_text if found_answer else None


def _validate_handle(value: object) -> dict[str, str] | None:
    if (
        type(value) is not dict
        or set(value) != {"thread_id", "turn_id"}
        or type(value.get("thread_id")) is not str
        or type(value.get("turn_id")) is not str
        or _CODEX_ID.fullmatch(value["thread_id"]) is None
        or _CODEX_ID.fullmatch(value["turn_id"]) is None
    ):
        return None
    return {"thread_id": value["thread_id"], "turn_id": value["turn_id"]}


def _request_from_prepared(prepared: PreparedProviderRequest) -> AgentRequest:
    payload = prepared.to_dict()
    return AgentRequest(
        operation=payload["operation"],
        provider=payload["provider"],
        target=payload["target"],
        target_context=TargetContext.from_dict(payload["target_identity"]),
        session=payload["session"],
        handle=payload["handle"],
        options=payload["options"],
        prompt=payload["prompt"],
        response=payload["response"],
        wait=payload["wait"],
        timeout_seconds=payload["timeout_seconds"],
    )


def _unknown_observation(handle: object) -> Observation:
    normalized = _validate_handle(handle)
    return Observation(
        "UNKNOWN",
        handle=normalized,
        error={
            "code": "observation_unavailable",
            "message": _safe_message("observation_unavailable"),
        },
    )


def _safe_message(code: str) -> str:
    messages = {
        "needs_approval": "Agent execution requires approval.",
        "needs_input": "Agent execution requires input.",
        "remote_failed": "Agent execution failed.",
        "remote_interrupted": "Agent execution was interrupted.",
        "response_unavailable": "Agent final response is unavailable.",
        "observation_unavailable": "Agent state could not be observed.",
    }
    return messages[code]


__all__ = ["CodexAgentProvider"]
