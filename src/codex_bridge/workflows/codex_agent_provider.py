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
    PreparedProviderRequest,
    ProviderCapabilities,
    TargetContext,
    codex_agent_capabilities,
)
from .codex_job_adapter import CodexJobAdapter
from .http_client import (
    CodexAgentHTTPClient,
    CodexHTTPError,
    CodexHTTPStatusError,
    CodexPreflightError,
    CodexProtocolError,
    validate_agent_continue_payload,
    validate_agent_interrupt_payload,
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
        "timed_out",
        "not_loaded",
        "unknown",
    }
)
_ACTIVE_STATES = frozenset({"in_progress", "needs_approval", "needs_input"})
_TERMINAL_STATES = frozenset({"completed", "failed", "interrupted", "timed_out"})
_CODEX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,99}\Z", re.ASCII)
_MAX_PROMPT_CHARS = 32_000
_MAX_REQUEST_BYTES = 65_536
_MAX_RESPONSE_BYTES = 1_048_576


class CodexAgentProvider:
    """A local Bridge provider for invoke and read-only exact-turn operations."""

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
        capabilities = codex_agent_capabilities(self._context)
        return ProviderCapabilities(
            provider=capabilities.provider,
            target=capabilities.target,
            operations=capabilities.operations,
            session_modes=capabilities.session_modes,
            option_rules=capabilities.option_rules,
            option_constraints=capabilities.option_constraints,
            usage_supported=capabilities.usage_supported,
            transport_constraints={
                "max_request_bytes": _MAX_REQUEST_BYTES,
                "max_prompt_characters": _MAX_PROMPT_CHARS,
                "max_response_bytes": _MAX_RESPONSE_BYTES,
            },
            contract_version=capabilities.contract_version,
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
        if request.operation == "invoke":
            if request.session is None or request.session.get("mode") not in {"new", "continue"}:
                raise AgentContractError("unsupported_operation")
            if type(request.wait) is not bool:
                raise AgentContractError("invalid_request")
            prompt = request.prompt
            if type(prompt) is not str or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS:
                raise AgentContractError("invalid_request")
            try:
                prompt.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise AgentContractError("invalid_request") from None
            options = request.options or {}
            if request.session["mode"] == "new":
                _validate_new_options(options)
            else:
                if _validate_codex_id(request.session.get("thread_id")) is None:
                    raise AgentContractError("invalid_request")
                if options:
                    raise AgentContractError("unsupported_option")
        elif request.operation == "interrupt":
            if _validate_handle(request.handle) is None:
                raise AgentContractError("invalid_request")
        elif request.operation in {"observe", "wait"}:
            if _validate_handle(request.handle) is None:
                raise AgentContractError("invalid_request")
        else:
            raise AgentContractError("unsupported_operation")
        prepared = PreparedProviderRequest(request, self._context)
        self._validate_prepared(prepared)
        return prepared

    def preflight(self, prepared: PreparedProviderRequest) -> None:
        if type(prepared) is not PreparedProviderRequest or prepared.operation not in {
            "invoke",
            "interrupt",
        }:
            raise AgentContractError("unsupported_operation")
        self._validate_prepared(prepared)
        try:
            self._client.preflight()
            if (
                prepared.operation == "invoke"
                and prepared.session is not None
                and prepared.session.get("mode") == "new"
                and prepared.options is not None
                and "model" in prepared.options
            ):
                capabilities = self._client.get_control_capabilities()
                _validate_current_model_catalog(prepared.options, capabilities)
        except CodexHTTPStatusError as error:
            code = "unauthorized" if error.status_code == 403 else "preflight_failed"
            raise AgentContractError(code) from None
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
        if type(prepared) is not PreparedProviderRequest or prepared.operation not in {
            "invoke",
            "interrupt",
        }:
            raise AgentContractError("unsupported_operation")
        request = _request_from_prepared(prepared)
        known_handle = request.handle if request.operation == "interrupt" else None
        try:
            self._validate_prepared(prepared)
            if (
                type(client_request_id) is not str
                or re.fullmatch(r"cba-[0-9a-f]{64}", client_request_id, re.ASCII) is None
            ):
                return DispatchReceipt.rejected("invalid_request", handle=known_handle)
            if request.operation == "invoke" and request.session is not None:
                if request.session.get("mode") == "new":
                    body = self._request_body(prepared, client_request_id)
                    validate_agent_thread_payload(body)
                    response = self._client.create_agent_thread(body)
                else:
                    body = self._continue_body(prepared, client_request_id)
                    validate_agent_continue_payload(body)
                    response = self._client.continue_agent_thread(
                        request.session["thread_id"], body
                    )
            else:
                body = self._interrupt_body(prepared, client_request_id)
                validate_agent_interrupt_payload(body)
                handle = _validate_handle(request.handle)
                if handle is None:
                    return DispatchReceipt.rejected("invalid_request")
                response = self._client.interrupt_agent_turn(
                    handle["thread_id"], handle["turn_id"], body
                )
        except CodexHTTPStatusError as error:
            if error.status_code == 403:
                return DispatchReceipt.rejected("unauthorized", handle=known_handle)
            if error.status_code == 413:
                return DispatchReceipt.rejected("request_too_large", handle=known_handle)
            if (
                error.status_code == 404
                and request.operation == "invoke"
                and request.session is not None
                and request.session.get("mode") == "new"
            ):
                return DispatchReceipt.rejected("dispatch_rejected")
            return DispatchReceipt.uncertain(handle=known_handle)
        except (CodexHTTPError, OSError, TimeoutError):
            return DispatchReceipt.uncertain(handle=known_handle)
        except AgentContractError:
            return DispatchReceipt.uncertain(handle=known_handle)
        except Exception:
            return DispatchReceipt.uncertain(handle=known_handle)
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
            return DispatchReceipt.uncertain(handle=known_handle)
        response_handle = {"thread_id": response["thread_id"], "turn_id": response["turn_id"]}
        if request.operation == "invoke" and request.session is not None:
            if request.session.get("mode") == "continue" and response_handle[
                "thread_id"
            ] != request.session.get("thread_id"):
                return DispatchReceipt.uncertain()
        elif response_handle != known_handle:
            return DispatchReceipt.uncertain(handle=known_handle)
        return DispatchReceipt.accepted(
            handle=response_handle,
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
            if exact_state == "unknown":
                return Observation(
                    "UNKNOWN",
                    handle=parsed_handle,
                    remote_state="unknown",
                    error={
                        "code": "observation_unavailable",
                        "message": _safe_message("observation_unavailable"),
                    },
                )
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
            if exact_state == "timed_out":
                return Observation(
                    "TIMED_OUT",
                    handle=parsed_handle,
                    remote_state=exact_state,
                    error={"code": "timeout", "message": _safe_message("timeout")},
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
        if type(target_context) is not TargetContext or target_context != self._context:
            raise AgentContractError("unsupported_target")
        parsed_handle = _validate_handle(handle)
        if parsed_handle is None:
            raise AgentContractError("invalid_request")
        request = AgentRequest(
            operation="interrupt",
            provider="codex",
            target=_TARGET,
            target_context=self._context,
            session=None,
            handle=parsed_handle,
            options=None,
            prompt=None,
            response=None,
            wait=None,
            timeout_seconds=None,
        )
        prepared = PreparedProviderRequest(request, self._context)
        return self.dispatch(prepared, client_request_id)

    def _validate_prepared(self, prepared: PreparedProviderRequest) -> None:
        if type(prepared) is not PreparedProviderRequest:
            raise AgentContractError("invalid_request")
        try:
            normalized = PreparedProviderRequest.from_json(prepared.to_json())
        except Exception:
            raise AgentContractError("invalid_request") from None
        if (
            normalized.provider != "codex"
            or normalized.target != _TARGET
            or normalized.target_identity != self._context.to_dict()
        ):
            raise AgentContractError("unsupported_operation")
        request = _request_from_prepared(normalized)
        if normalized.operation in {"observe", "wait"}:
            if (
                request.session is not None
                or request.options is not None
                or request.prompt is not None
                or request.wait is not None
                or _validate_handle(request.handle) is None
                or (normalized.operation == "observe" and request.timeout_seconds is not None)
            ):
                raise AgentContractError("invalid_request")
            return
        if normalized.operation == "interrupt":
            if (
                _validate_handle(request.handle) is None
                or request.session is not None
                or request.options is not None
                or request.prompt is not None
                or request.response is not None
                or request.wait is not None
                or request.timeout_seconds is not None
            ):
                raise AgentContractError("invalid_request")
            return
        if normalized.operation != "invoke" or request.session is None:
            raise AgentContractError("unsupported_operation")
        if (
            request.session.get("mode") not in {"new", "continue"}
            or type(normalized.wait) is not bool
        ):
            raise AgentContractError("unsupported_operation")
        prompt = request.prompt
        options = request.options or {}
        if type(prompt) is not str or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS:
            raise AgentContractError("invalid_request")
        if request.session["mode"] == "new":
            _validate_new_options(options)
            body = self._request_body_unchecked(normalized, "cba-" + "0" * 64)
            validator = validate_agent_thread_payload
        else:
            if _validate_codex_id(request.session.get("thread_id")) is None:
                raise AgentContractError("invalid_request")
            if options:
                raise AgentContractError("unsupported_option")
            body = self._continue_body_unchecked(normalized, "cba-" + "0" * 64)
            validator = validate_agent_continue_payload
        try:
            validator(body)
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
        for option in ("model", "reasoning_effort", "title"):
            if option in options:
                body[option] = options[option]
        return body

    def _continue_body(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> dict[str, Any]:
        self._validate_prepared(prepared)
        return self._continue_body_unchecked(prepared, client_request_id)

    def _continue_body_unchecked(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> dict[str, Any]:
        request = _request_from_prepared(prepared)
        return {
            "client_request_id": client_request_id,
            "prompt": request.prompt,
        }

    def _interrupt_body(
        self,
        prepared: PreparedProviderRequest,
        client_request_id: str,
    ) -> dict[str, Any]:
        self._validate_prepared(prepared)
        return {"client_request_id": client_request_id}

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


def _validate_codex_id(value: object) -> str | None:
    if type(value) is not str or _CODEX_ID.fullmatch(value) is None:
        return None
    return value


def _validate_new_options(options: Mapping[str, Any]) -> None:
    if set(options) - {
        "model",
        "reasoning_effort",
        "title",
        "sandbox_mode",
        "confirm_full_access",
    }:
        raise AgentContractError("unsupported_option")
    for name in ("model", "reasoning_effort", "title"):
        if name in options and (type(options[name]) is not str or not options[name].strip()):
            raise AgentContractError("invalid_request")
    if "reasoning_effort" in options and "model" not in options:
        raise AgentContractError("invalid_request")
    sandbox_mode = options.get("sandbox_mode", "inherit")
    if type(sandbox_mode) is not str or sandbox_mode not in {"inherit", "danger-full-access"}:
        raise AgentContractError("invalid_request")
    confirm = options.get("confirm_full_access", False)
    if type(confirm) is not bool:
        raise AgentContractError("invalid_request")
    if sandbox_mode == "danger-full-access" and confirm is not True:
        raise AgentContractError("invalid_request")


def _validate_current_model_catalog(options: Mapping[str, Any], catalog: object) -> None:
    if type(catalog) is not dict or type(catalog.get("models")) is not list:
        raise AgentContractError("preflight_failed")
    models: dict[str, set[str]] = {}
    for item in catalog["models"]:
        if (
            not isinstance(item, Mapping)
            or type(item.get("model")) is not str
            or not item["model"].strip()
            or item["model"] in models
        ):
            raise AgentContractError("preflight_failed")
        raw_efforts = item.get("reasoning_efforts")
        if type(raw_efforts) is not list or not raw_efforts:
            raise AgentContractError("preflight_failed")
        efforts: set[str] = set()
        for effort in raw_efforts:
            if (
                not isinstance(effort, Mapping)
                or type(effort.get("id")) is not str
                or not effort["id"].strip()
                or effort["id"] in efforts
            ):
                raise AgentContractError("preflight_failed")
            efforts.add(effort["id"])
        models[item["model"]] = efforts
    model_name = options.get("model")
    if type(model_name) is not str or model_name not in models:
        raise AgentContractError("unsupported_option")
    if "reasoning_effort" not in options:
        return
    if options["reasoning_effort"] not in models[model_name]:
        raise AgentContractError("unsupported_option")


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
        "timeout": "Agent wait deadline was reached.",
        "response_unavailable": "Agent final response is unavailable.",
        "observation_unavailable": "Agent state could not be observed.",
    }
    return messages[code]


__all__ = ["CodexAgentProvider"]
