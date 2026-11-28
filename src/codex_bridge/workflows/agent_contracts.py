"""Pure, versioned contracts for Workflow Agent requests and results.

The module validates the D2 wire shape and prepares detached JSON snapshots.
It does not register runtime operations, perform transport, or persist data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NoReturn, Protocol, cast, runtime_checkable

from .agent_prompt import (
    AgentPromptError,
    parse_prompt_template,
    render_prompt_template,
    validate_prompt_bindings,
)
from .contracts import (
    ContractError,
    ContractOutputInvalid,
    ContractResourceLimit,
    canonical_json,
    parse_strict_json,
    validate_json_value_schema,
    validate_schema_definition,
)

_VERSION = 1
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
_OPERATIONS = frozenset({"invoke", "observe", "wait", "interrupt"})
_SESSIONS = frozenset({"new", "continue"})
_STATUSES = frozenset(
    {
        "ACCEPTED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "TIMED_OUT",
        "UNKNOWN",
        "BLOCKED",
        "INTERRUPTED",
    }
)
_OBSERVATION_STATES = frozenset(
    {"RUNNING", "COMPLETED", "FAILED", "TIMED_OUT", "UNKNOWN", "BLOCKED", "INTERRUPTED"}
)
_DISPATCH_DISPOSITIONS = frozenset({"REJECTED", "ACCEPTED", "UNCERTAIN"})
_JSON_TYPES = frozenset({"null", "boolean", "integer", "number", "string", "array", "object"})
_SAFE_MESSAGES = {
    "invalid_request": "Agent request is invalid.",
    "unsupported_provider": "Agent provider is unsupported.",
    "unsupported_target": "Agent target is unsupported.",
    "unsupported_operation": "Agent operation is unsupported.",
    "unsupported_option": "Agent option is unsupported.",
    "unauthorized": "Agent request is not authorized.",
    "request_too_large": "Agent request exceeds the provider limit.",
    "preflight_failed": "Agent request could not be prepared.",
    "dispatch_rejected": "Agent request was rejected before acceptance.",
    "dispatch_uncertain": "Agent request acceptance could not be confirmed.",
    "observation_unavailable": "Agent state could not be observed.",
    "response_unavailable": "Agent final response is unavailable.",
    "remote_failed": "Agent execution failed.",
    "remote_interrupted": "Agent execution was interrupted.",
    "needs_approval": "Agent execution requires approval.",
    "needs_input": "Agent execution requires input.",
    "timeout": "Agent wait deadline was reached.",
    "output_invalid": "Agent response did not satisfy its response contract.",
    "output_too_large": "Agent response exceeds the supported value limits.",
}


class AgentContractError(ValueError):
    """Safe, stable error raised for invalid Agent contract values."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in _SAFE_MESSAGES else "invalid_request"
        super().__init__(_SAFE_MESSAGES[self.code])


def _fail(code: str = "invalid_request") -> NoReturn:
    raise AgentContractError(code) from None


def _json_copy(value: Any) -> Any:
    try:
        return parse_strict_json(canonical_json(value))
    except (ContractError, TypeError, ValueError, RecursionError, OverflowError):
        _fail()


def _json_object(value: Any) -> dict[str, Any]:
    detached = _json_copy(value)
    if type(detached) is not dict:
        _fail()
    return cast(dict[str, Any], detached)


def _load_json(payload: str | bytes) -> Any:
    try:
        return parse_strict_json(payload)
    except ContractError:
        _fail()


def _exact_object(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail()
    return cast(dict[str, Any], value)


def _identifier(value: Any) -> bool:
    return type(value) is str and _PROVIDER_ID.fullmatch(value) is not None


def _positive_integer(value: Any) -> bool:
    return type(value) is int and value > 0


def _is_expression(value: Any) -> bool:
    return (
        type(value) is str
        and value.startswith("${")
        and value.endswith("}")
        and bool(value[2:-1].strip())
    )


def _json_type(value: Any) -> str:
    value_type = type(value)
    if value is None:
        return "null"
    if value_type is bool:
        return "boolean"
    if value_type is int:
        return "integer"
    if value_type is float:
        return "number"
    if value_type is str:
        return "string"
    if value_type is list:
        return "array"
    if value_type is dict:
        return "object"
    return ""


def _matches_type(value: Any, allowed: tuple[str, ...]) -> bool:
    actual = _json_type(value)
    if actual == "integer":
        return "integer" in allowed or "number" in allowed
    if actual == "number":
        return "number" in allowed
    return actual in allowed


def _canonical_json_literal(value: Any) -> str:
    try:
        return canonical_json(value)
    except (ContractError, TypeError, ValueError, RecursionError, OverflowError):
        _fail()


@dataclass(frozen=True, slots=True)
class TargetContext:
    provider: str
    target: str
    contract_version: int
    endpoint: str | None = field(default=None, repr=False)
    workspace: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not _identifier(self.provider)
            or not _identifier(self.target)
            or type(self.contract_version) is not int
            or self.contract_version < 1
            or (self.endpoint is not None and type(self.endpoint) is not str)
            or (self.workspace is not None and type(self.workspace) is not str)
        ):
            _fail()
        _json_copy(self.to_dict())

    @property
    def identity(self) -> dict[str, Any]:
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "target": self.target,
            "contract_version": self.contract_version,
            "endpoint": self.endpoint,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TargetContext:
        item = _exact_object(
            value,
            frozenset({"provider", "target", "contract_version", "endpoint", "workspace"}),
        )
        return cls(
            provider=item["provider"],
            target=item["target"],
            contract_version=item["contract_version"],
            endpoint=item["endpoint"],
            workspace=item["workspace"],
        )


@dataclass(frozen=True, slots=True)
class OptionCapability:
    name: str
    session_modes: tuple[str, ...]
    value_types: tuple[str, ...]
    allowed_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not _identifier(self.name)
            or type(self.session_modes) is not tuple
            or not self.session_modes
            or any(type(mode) is not str or mode not in _SESSIONS for mode in self.session_modes)
            or len(set(self.session_modes)) != len(self.session_modes)
            or type(self.value_types) is not tuple
            or not self.value_types
            or any(
                type(value_type) is not str or value_type not in _JSON_TYPES
                for value_type in self.value_types
            )
            or len(set(self.value_types)) != len(self.value_types)
            or type(self.allowed_values) is not tuple
        ):
            _fail()
        for value in self.allowed_values:
            parsed = _load_json(value)
            if type(value) is not str or canonical_json(parsed) != value:
                _fail()


@dataclass(frozen=True, slots=True)
class OptionConstraint:
    """A declarative option dependency; None trigger value means any value."""

    trigger_option: str
    trigger_value_json: str | None
    required_option: str
    required_value_json: str | None

    def __post_init__(self) -> None:
        if not _identifier(self.trigger_option) or not _identifier(self.required_option):
            _fail()
        for value in (self.trigger_value_json, self.required_value_json):
            if value is None:
                continue
            parsed = _load_json(value)
            if type(value) is not str or canonical_json(parsed) != value:
                _fail()


@dataclass(frozen=True, slots=True, init=False)
class ProviderCapabilities:
    provider: str
    target: str
    operations: tuple[str, ...]
    session_modes: tuple[str, ...]
    option_rules: tuple[OptionCapability, ...]
    option_constraints: tuple[OptionConstraint, ...]
    usage_supported: bool
    contract_version: int
    endpoint: str | None = field(repr=False)
    workspace: str | None = field(repr=False)
    _transport_constraints_json: str = field(repr=False)

    def __init__(
        self,
        *,
        provider: str,
        target: str,
        operations: tuple[str, ...],
        session_modes: tuple[str, ...],
        option_rules: tuple[OptionCapability, ...] = (),
        option_constraints: tuple[OptionConstraint, ...] = (),
        usage_supported: bool = False,
        transport_constraints: object | None = None,
        contract_version: int = 1,
        endpoint: str | None = None,
        workspace: str | None = None,
    ) -> None:
        if (
            not _identifier(provider)
            or not _identifier(target)
            or type(operations) is not tuple
            or not operations
            or any(
                type(operation) is not str or operation not in _OPERATIONS
                for operation in operations
            )
            or len(set(operations)) != len(operations)
            or type(session_modes) is not tuple
            or any(type(mode) is not str or mode not in _SESSIONS for mode in session_modes)
            or len(set(session_modes)) != len(session_modes)
            or type(option_rules) is not tuple
            or any(type(rule) is not OptionCapability for rule in option_rules)
            or len({rule.name for rule in option_rules}) != len(option_rules)
            or any(not set(rule.session_modes).issubset(session_modes) for rule in option_rules)
            or type(option_constraints) is not tuple
            or any(type(item) is not OptionConstraint for item in option_constraints)
            or type(usage_supported) is not bool
            or type(contract_version) is not int
            or contract_version < 1
            or (endpoint is not None and type(endpoint) is not str)
            or (workspace is not None and type(workspace) is not str)
        ):
            _fail()
        rule_names = {rule.name for rule in option_rules}
        if any(
            item.trigger_option not in rule_names or item.required_option not in rule_names
            for item in option_constraints
        ):
            _fail()
        transport = _json_object({} if transport_constraints is None else transport_constraints)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "session_modes", session_modes)
        object.__setattr__(self, "option_rules", option_rules)
        object.__setattr__(self, "option_constraints", option_constraints)
        object.__setattr__(self, "usage_supported", usage_supported)
        object.__setattr__(self, "contract_version", contract_version)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "_transport_constraints_json", canonical_json(transport))
        TargetContext(provider, target, contract_version, endpoint, workspace)

    @property
    def target_context(self) -> TargetContext:
        return TargetContext(
            self.provider, self.target, self.contract_version, self.endpoint, self.workspace
        )

    @property
    def transport_constraints(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._transport_constraints_json))

    def option_rule(self, name: str, session_mode: str) -> OptionCapability | None:
        for rule in self.option_rules:
            if rule.name == name and session_mode in rule.session_modes:
                return rule
        return None


@runtime_checkable
class ProviderProtocol(Protocol):
    """Synchronous, side-effect-free preparation and explicit provider actions."""

    def capabilities(self, target: str) -> ProviderCapabilities: ...

    def prepare(
        self, request: AgentRequest, target_context: TargetContext
    ) -> PreparedProviderRequest: ...

    def dispatch(
        self, prepared: PreparedProviderRequest, client_request_id: str
    ) -> DispatchReceipt: ...

    def observe(self, handle: object, target_context: TargetContext) -> Observation: ...

    def interrupt(
        self, handle: object, target_context: TargetContext, client_request_id: str
    ) -> DispatchReceipt: ...


class ProviderRegistry:
    """Injected provider registry used only by pure wire validation."""

    __slots__ = ("_providers", "_default_targets")

    def __init__(
        self,
        providers: Mapping[str, ProviderProtocol],
        default_targets: Mapping[str, str] | None = None,
    ) -> None:
        copied: dict[str, ProviderProtocol] = {}
        for provider_id, provider in providers.items():
            if not _identifier(provider_id) or not isinstance(provider, ProviderProtocol):
                _fail()
            copied[provider_id] = provider
        defaults = dict(default_targets or {})
        if any(
            provider_id not in copied or not _identifier(target)
            for provider_id, target in defaults.items()
        ):
            _fail()
        self._providers = MappingProxyType(copied)
        self._default_targets = MappingProxyType(defaults)

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def resolve(self, provider: str, target: str | None) -> ProviderCapabilities:
        if not _identifier(provider) or (target is not None and not _identifier(target)):
            _fail("unsupported_target" if target is not None else "unsupported_provider")
        implementation = self._providers.get(provider)
        if implementation is None:
            _fail("unsupported_provider")
        resolved_target = target or self._default_targets.get(provider, "default")
        if not _identifier(resolved_target):
            _fail("unsupported_target")
        try:
            capabilities = implementation.capabilities(resolved_target)
        except Exception:
            _fail("unsupported_target")
        if (
            type(capabilities) is not ProviderCapabilities
            or capabilities.provider != provider
            or capabilities.target != resolved_target
        ):
            _fail("unsupported_target")
        return capabilities


@dataclass(frozen=True, slots=True)
class AgentCallDefinition:
    provider: str
    target: str
    operation: str
    capabilities: ProviderCapabilities
    _wire_json: str = field(repr=False)

    @property
    def target_context(self) -> TargetContext:
        return self.capabilities.target_context

    @property
    def wire(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._wire_json))


def codex_agent_capabilities(
    target_context: TargetContext | None = None,
) -> ProviderCapabilities:
    """Return pure Codex capability metadata; this does not register a provider."""
    context = target_context or TargetContext("codex", "default", 1)
    if context.provider != "codex":
        _fail()
    return ProviderCapabilities(
        provider=context.provider,
        target=context.target,
        operations=("invoke", "observe", "wait", "interrupt"),
        session_modes=("new", "continue"),
        option_rules=(
            OptionCapability("model", ("new",), ("string",)),
            OptionCapability("reasoning_effort", ("new",), ("string",)),
            OptionCapability("title", ("new",), ("string",)),
            OptionCapability(
                "sandbox_mode",
                ("new",),
                ("string",),
                allowed_values=('"inherit"', '"danger-full-access"'),
            ),
            OptionCapability("confirm_full_access", ("new",), ("boolean",)),
        ),
        option_constraints=(
            OptionConstraint("reasoning_effort", None, "model", None),
            OptionConstraint("sandbox_mode", '"danger-full-access"', "confirm_full_access", "true"),
        ),
        usage_supported=False,
        contract_version=context.contract_version,
        endpoint=context.endpoint,
        workspace=context.workspace,
    )


class _StaticCodexAgentProvider:
    """Capability-only provider used for saved-definition validation."""

    def capabilities(self, target: str) -> ProviderCapabilities:
        if type(target) is not str or target != "default":
            _fail("unsupported_target")
        return codex_agent_capabilities(TargetContext("codex", "default", 1))

    def prepare(
        self, request: AgentRequest, target_context: TargetContext
    ) -> PreparedProviderRequest:
        _fail("unsupported_operation")

    def dispatch(
        self, prepared: PreparedProviderRequest, client_request_id: str
    ) -> DispatchReceipt:
        _fail("unsupported_operation")

    def observe(self, handle: object, target_context: TargetContext) -> Observation:
        _fail("unsupported_operation")

    def interrupt(
        self, handle: object, target_context: TargetContext, client_request_id: str
    ) -> DispatchReceipt:
        _fail("unsupported_operation")


def codex_agent_i4b_registry() -> ProviderRegistry:
    """Return the static Codex Agent operation and option surface for definitions."""
    return ProviderRegistry(
        {"codex": _StaticCodexAgentProvider()},
        {"codex": "default"},
    )


def _option_value_matches(rule: OptionCapability, value: Any) -> bool:
    if not _matches_type(value, rule.value_types):
        return False
    if rule.allowed_values and _canonical_json_literal(value) not in rule.allowed_values:
        return False
    return True


def _check_option_constraints(
    options: Mapping[str, Any], capabilities: ProviderCapabilities
) -> None:
    for constraint in capabilities.option_constraints:
        if constraint.trigger_option not in options:
            continue
        trigger = options[constraint.trigger_option]
        if (
            constraint.trigger_value_json is not None
            and _canonical_json_literal(trigger) != constraint.trigger_value_json
        ):
            continue
        if constraint.required_option not in options:
            _fail()
        if (
            constraint.required_value_json is not None
            and _canonical_json_literal(options[constraint.required_option])
            != constraint.required_value_json
        ):
            _fail()


def _check_static_option_constraints(
    options: Mapping[str, Any], capabilities: ProviderCapabilities
) -> None:
    for constraint in capabilities.option_constraints:
        if constraint.trigger_option not in options:
            continue
        trigger_raw = options.get(constraint.trigger_option)
        required_raw = options.get(constraint.required_option)
        if _is_expression(trigger_raw):
            continue
        trigger_json = _canonical_json_literal(trigger_raw)
        if (
            constraint.trigger_value_json is not None
            and trigger_json != constraint.trigger_value_json
        ):
            continue
        if constraint.required_option not in options or _is_expression(required_raw):
            if constraint.required_option in options:
                continue
            _fail()
        if (
            constraint.required_value_json is not None
            and _canonical_json_literal(required_raw) != constraint.required_value_json
        ):
            _fail()


def _validate_response(response: Any, *, default: bool) -> dict[str, Any]:
    if response is None and default:
        response = {"format": "text"}
    if type(response) is not dict or set(response) not in (
        {"format"},
        {"format", "schema"},
    ):
        _fail()
    response_format = response.get("format")
    if type(response_format) is not str or response_format not in {"text", "json"}:
        _fail()
    if "schema" in response:
        if response_format != "json" or type(response["schema"]) is not dict:
            _fail()
        try:
            schema = validate_schema_definition(response["schema"])
        except ContractError:
            _fail()
        return {"format": response_format, "schema": schema}
    return {"format": response_format}


def _validate_dynamic_json(value: Any) -> None:
    if _is_expression(value):
        return
    _json_copy(value)


def _validate_timeout_wire(value: Any) -> None:
    _validate_dynamic_json(value)
    if _is_expression(value) or value is None:
        return
    if not _positive_integer(value):
        _fail()


def _validate_wait_wire(value: Any) -> None:
    _validate_dynamic_json(value)
    if _is_expression(value):
        return
    if type(value) is not bool:
        _fail()


def _validate_handle_wire(value: Any) -> None:
    if _is_expression(value):
        return
    _json_object(value)


def _wire_keys(operation: str) -> frozenset[str]:
    common = {"operation", "provider", "target"}
    if operation == "invoke":
        return frozenset(
            common
            | {
                "session",
                "prompt",
                "bindings",
                "options",
                "response",
                "wait",
                "timeout_seconds",
            }
        )
    if operation in {"observe", "wait"}:
        keys = common | {"handle", "response"}
        if operation == "wait":
            keys.add("timeout_seconds")
        return frozenset(keys)
    return frozenset(common | {"handle"})


def validate_agent_wire(raw: object, registry: ProviderRegistry) -> AgentCallDefinition:
    """Validate one closed D2 wire object against injected provider capabilities."""
    if type(raw) is not dict or type(registry) is not ProviderRegistry:
        _fail()
    wire = cast(dict[str, Any], _json_copy(raw))
    operation = wire.get("operation", "invoke")
    if type(operation) is not str or operation not in _OPERATIONS:
        _fail("unsupported_operation")
    if not set(wire).issubset(_wire_keys(operation)):
        _fail()
    provider = wire.get("provider")
    if not _identifier(provider):
        _fail()
    provider_id = cast(str, provider)
    target_value = wire.get("target")
    if "target" in wire and not _identifier(target_value):
        _fail("unsupported_target")
    capabilities = registry.resolve(provider_id, target_value)
    target = capabilities.target
    if operation not in capabilities.operations:
        _fail("unsupported_operation")

    if operation == "invoke":
        if "session" not in wire or "prompt" not in wire:
            _fail()
        session = wire["session"]
        if type(session) is not dict or "mode" not in session:
            _fail()
        mode = session["mode"]
        if type(mode) is not str or mode not in _SESSIONS:
            _fail()
        if mode not in capabilities.session_modes:
            _fail("unsupported_operation")
        expected_session_keys = {"mode"} if mode == "new" else {"mode", "thread_id"}
        if set(session) != expected_session_keys:
            _fail()
        if mode == "continue":
            thread_id = session["thread_id"]
            _validate_dynamic_json(thread_id)
            if not _is_expression(thread_id) and (type(thread_id) is not str or not thread_id):
                _fail()

        prompt = wire["prompt"]
        if type(prompt) is not str:
            _fail()
        try:
            template = parse_prompt_template(prompt)
            bindings = wire.get("bindings", {})
            validate_prompt_bindings(bindings, template.placeholder_names)
        except AgentPromptError:
            _fail()

        options = wire.get("options", {})
        if type(options) is not dict:
            _fail()
        for name, value in options.items():
            if type(name) is not str:
                _fail("unsupported_option")
            rule = capabilities.option_rule(name, mode)
            if rule is None:
                _fail("unsupported_option")
            _validate_dynamic_json(value)
            if not _is_expression(value) and not _option_value_matches(rule, value):
                _fail()
        _check_static_option_constraints(options, capabilities)

        response = _validate_response(wire.get("response"), default="response" not in wire)
        wait = wire.get("wait", True)
        _validate_wait_wire(wait)
        timeout_seconds = wire.get("timeout_seconds")
        _validate_timeout_wire(timeout_seconds)
        if wait is False and timeout_seconds is not None and not _is_expression(timeout_seconds):
            _fail()
        normalized = dict(wire)
        normalized.update(
            {
                "operation": operation,
                "provider": provider_id,
                "target": target,
                "session": session,
                "prompt": prompt,
                "bindings": bindings,
                "options": options,
                "response": response,
                "wait": wait,
                "timeout_seconds": timeout_seconds,
            }
        )
    else:
        if "handle" not in wire:
            _fail()
        handle = wire["handle"]
        _validate_handle_wire(handle)
        normalized = dict(wire)
        normalized.update({"operation": operation, "provider": provider, "target": target})
        if operation in {"observe", "wait"}:
            normalized["response"] = _validate_response(
                wire.get("response"), default="response" not in wire
            )
            if operation == "wait":
                timeout_seconds = wire.get("timeout_seconds")
                _validate_timeout_wire(timeout_seconds)
                normalized["timeout_seconds"] = timeout_seconds

    return AgentCallDefinition(
        provider=provider_id,
        target=target,
        operation=operation,
        capabilities=capabilities,
        _wire_json=canonical_json(normalized),
    )


def _evaluate_dynamic(value: Any, evaluate_single_result: Any) -> Any:
    if not _is_expression(value):
        return _json_copy(value)
    try:
        result = evaluate_single_result(value)
    except Exception:
        _fail("preflight_failed")
    return _json_copy(result)


def _evaluated_timeout(value: Any, evaluate_single_result: Any) -> int | None:
    result = _evaluate_dynamic(value, evaluate_single_result)
    if result is not None and not _positive_integer(result):
        _fail()
    return cast(int | None, result)


def _evaluated_options(
    raw_options: dict[str, Any],
    session_mode: str,
    capabilities: ProviderCapabilities,
    evaluate_single_result: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, raw_value in raw_options.items():
        rule = capabilities.option_rule(name, session_mode)
        if rule is None:
            _fail("unsupported_option")
        value = _evaluate_dynamic(raw_value, evaluate_single_result)
        if not _option_value_matches(rule, value):
            _fail()
        result[name] = value
    _check_option_constraints(result, capabilities)
    return result


def _response_from_wire(value: Any) -> dict[str, Any]:
    return _validate_response(value, default=False)


def evaluate_agent_request(
    definition: AgentCallDefinition,
    evaluate_single_result: Any,
) -> AgentRequest:
    """Evaluate only declared jq fields and produce an immutable typed request."""
    if type(definition) is not AgentCallDefinition or not callable(evaluate_single_result):
        _fail()
    wire = definition.wire
    operation = definition.operation
    context = definition.target_context

    if operation == "invoke":
        mode = wire["session"]["mode"]
        if mode == "new":
            session: dict[str, Any] = {"mode": "new"}
        else:
            thread_id = _evaluate_dynamic(wire["session"]["thread_id"], evaluate_single_result)
            if type(thread_id) is not str or not thread_id:
                _fail()
            session = {"mode": "continue", "thread_id": thread_id}

        try:
            prompt = render_prompt_template(
                wire["prompt"], wire["bindings"], evaluate_single_result
            )
        except AgentPromptError as error:
            _fail(
                "preflight_failed"
                if error.code == "binding_evaluation_failed"
                else "invalid_request"
            )

        options = _evaluated_options(
            wire["options"], mode, definition.capabilities, evaluate_single_result
        )
        wait_value = _evaluate_dynamic(wire["wait"], evaluate_single_result)
        if type(wait_value) is not bool:
            _fail()
        timeout_seconds = _evaluated_timeout(wire["timeout_seconds"], evaluate_single_result)
        if wait_value is False and timeout_seconds is not None:
            _fail()
        return AgentRequest(
            operation=operation,
            provider=definition.provider,
            target=definition.target,
            target_context=context,
            session=session,
            handle=None,
            options=options,
            prompt=prompt,
            response=_response_from_wire(wire["response"]),
            wait=wait_value,
            timeout_seconds=timeout_seconds,
        )

    handle_value = _evaluate_dynamic(wire["handle"], evaluate_single_result)
    if type(handle_value) is not dict:
        _fail()
    if operation == "interrupt":
        return AgentRequest(
            operation=operation,
            provider=definition.provider,
            target=definition.target,
            target_context=context,
            session=None,
            handle=handle_value,
            options=None,
            prompt=None,
            response=None,
            wait=None,
            timeout_seconds=None,
        )
    timeout_seconds = (
        _evaluated_timeout(wire["timeout_seconds"], evaluate_single_result)
        if operation == "wait"
        else None
    )
    return AgentRequest(
        operation=operation,
        provider=definition.provider,
        target=definition.target,
        target_context=context,
        session=None,
        handle=handle_value,
        options=None,
        prompt=None,
        response=_response_from_wire(wire["response"]),
        wait=None,
        timeout_seconds=timeout_seconds,
    )


def _validate_request_fields(
    operation: Any,
    provider: Any,
    target: Any,
    target_context: Any,
    session: Any,
    handle: Any,
    options: Any,
    prompt: Any,
    response: Any,
    wait: Any,
    timeout_seconds: Any,
) -> tuple[
    TargetContext,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    if type(operation) is not str or operation not in _OPERATIONS:
        _fail()
    if not _identifier(provider) or not _identifier(target):
        _fail()
    context = TargetContext.from_dict(target_context) if type(target_context) is dict else None
    if context is None or context.provider != provider or context.target != target:
        _fail()
    normalized_session = None
    normalized_handle = None
    normalized_options = None
    normalized_response = None
    if operation == "invoke":
        if (
            type(session) is not dict
            or type(options) is not dict
            or type(prompt) is not str
            or handle is not None
        ):
            _fail()
        if type(wait) is not bool or (
            timeout_seconds is not None and not _positive_integer(timeout_seconds)
        ):
            _fail()
        if wait is False and timeout_seconds is not None:
            _fail()
        session_mode = session.get("mode")
        expected = {"mode"} if session_mode == "new" else {"mode", "thread_id"}
        if (
            type(session_mode) is not str
            or session_mode not in _SESSIONS
            or set(session) != expected
        ):
            _fail()
        if session_mode == "continue" and (
            type(session.get("thread_id")) is not str or not session["thread_id"]
        ):
            _fail()
        normalized_session = _json_object(session)
        normalized_options = _json_object(options)
        try:
            prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _fail()
        if not prompt.strip():
            _fail()
        normalized_response = _validate_response(response, default=False)
    else:
        if session is not None or options is not None or prompt is not None or wait is not None:
            _fail()
        if type(handle) is not dict:
            _fail()
        normalized_handle = _json_object(handle)
        if timeout_seconds is not None and (
            operation != "wait" or not _positive_integer(timeout_seconds)
        ):
            _fail()
        if operation == "interrupt":
            if response is not None or timeout_seconds is not None:
                _fail()
        else:
            normalized_response = _validate_response(response, default=False)
    return context, normalized_session, normalized_handle, normalized_options, normalized_response


_REQUEST_KEYS = frozenset(
    {
        "version",
        "operation",
        "provider",
        "target",
        "target_context",
        "session",
        "handle",
        "options",
        "prompt",
        "response",
        "wait",
        "timeout_seconds",
    }
)


@dataclass(frozen=True, slots=True, init=False)
class AgentRequest:
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        operation: str,
        provider: str,
        target: str,
        target_context: TargetContext,
        session: object | None,
        handle: object | None,
        options: object | None,
        prompt: str | None,
        response: object | None,
        wait: bool | None,
        timeout_seconds: int | None,
    ) -> None:
        if type(target_context) is not TargetContext:
            _fail()
        payload = {
            "version": _VERSION,
            "operation": operation,
            "provider": provider,
            "target": target,
            "target_context": target_context.to_dict(),
            "session": session,
            "handle": handle,
            "options": options,
            "prompt": prompt,
            "response": response,
            "wait": wait,
            "timeout_seconds": timeout_seconds,
        }
        (
            context,
            normalized_session,
            normalized_handle,
            normalized_options,
            normalized_response,
        ) = _validate_request_fields(
            operation,
            provider,
            target,
            payload["target_context"],
            session,
            handle,
            options,
            prompt,
            response,
            wait,
            timeout_seconds,
        )
        payload.update(
            {
                "target_context": context.to_dict(),
                "session": normalized_session,
                "handle": normalized_handle,
                "options": normalized_options,
                "response": normalized_response,
            }
        )
        object.__setattr__(self, "_payload_json", canonical_json(_json_object(payload)))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    @classmethod
    def from_dict(cls, value: Any) -> AgentRequest:
        item = _exact_object(value, _REQUEST_KEYS)
        if type(item["version"]) is not int or item["version"] != _VERSION:
            _fail()
        context = TargetContext.from_dict(item["target_context"])
        return cls(
            operation=item["operation"],
            provider=item["provider"],
            target=item["target"],
            target_context=context,
            session=item["session"],
            handle=item["handle"],
            options=item["options"],
            prompt=item["prompt"],
            response=item["response"],
            wait=item["wait"],
            timeout_seconds=item["timeout_seconds"],
        )

    @classmethod
    def from_json(cls, source: str | bytes) -> AgentRequest:
        return cls.from_dict(_load_json(source))

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def version(self) -> int:
        return _VERSION

    @property
    def operation(self) -> str:
        return cast(str, self._value("operation"))

    @property
    def provider(self) -> str:
        return cast(str, self._value("provider"))

    @property
    def target(self) -> str:
        return cast(str, self._value("target"))

    @property
    def target_context(self) -> TargetContext:
        return TargetContext.from_dict(self._value("target_context"))

    @property
    def target_identity(self) -> dict[str, Any]:
        return self.target_context.identity

    @property
    def session(self) -> dict[str, Any] | None:
        value = self._value("session")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def handle(self) -> dict[str, Any] | None:
        value = self._value("handle")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def options(self) -> dict[str, Any] | None:
        value = self._value("options")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def prompt(self) -> str | None:
        return cast(str | None, self._value("prompt"))

    @property
    def response(self) -> dict[str, Any] | None:
        value = self._value("response")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def wait(self) -> bool | None:
        return cast(bool | None, self._value("wait"))

    @property
    def timeout_seconds(self) -> int | None:
        return cast(int | None, self._value("timeout_seconds"))


_PREPARED_KEYS = frozenset(
    {
        "version",
        "operation",
        "provider",
        "target",
        "target_identity",
        "session",
        "handle",
        "options",
        "prompt",
        "response",
        "wait",
        "timeout_seconds",
    }
)


@dataclass(frozen=True, slots=True, init=False)
class PreparedProviderRequest:
    _payload_json: str = field(repr=False)

    def __init__(self, request: AgentRequest, target_context: TargetContext) -> None:
        if (
            type(request) is not AgentRequest
            or type(target_context) is not TargetContext
            or request.target_context != target_context
        ):
            _fail()
        request_payload = request.to_dict()
        payload = {
            "version": _VERSION,
            "operation": request.operation,
            "provider": request.provider,
            "target": request.target,
            "target_identity": target_context.identity,
            "session": request_payload["session"],
            "handle": request_payload["handle"],
            "options": request_payload["options"],
            "prompt": request_payload["prompt"],
            "response": request_payload["response"],
            "wait": request_payload["wait"],
            "timeout_seconds": request_payload["timeout_seconds"],
        }
        self._initialize(payload)

    def _initialize(self, raw: Any) -> None:
        item = _exact_object(raw, _PREPARED_KEYS)
        if type(item["version"]) is not int or item["version"] != _VERSION:
            _fail()
        context = TargetContext.from_dict(item["target_identity"])
        if context.provider != item["provider"] or context.target != item["target"]:
            _fail()
        # Reuse the request validator while keeping the prepared wire's prescribed identity key.
        request = AgentRequest(
            operation=item["operation"],
            provider=item["provider"],
            target=item["target"],
            target_context=context,
            session=item["session"],
            handle=item["handle"],
            options=item["options"],
            prompt=item["prompt"],
            response=item["response"],
            wait=item["wait"],
            timeout_seconds=item["timeout_seconds"],
        )
        if request.operation != item["operation"]:
            _fail()
        normalized = {
            "version": _VERSION,
            "operation": request.operation,
            "provider": request.provider,
            "target": request.target,
            "target_identity": context.identity,
            "session": request.to_dict()["session"],
            "handle": request.to_dict()["handle"],
            "options": request.to_dict()["options"],
            "prompt": request.to_dict()["prompt"],
            "response": request.to_dict()["response"],
            "wait": request.to_dict()["wait"],
            "timeout_seconds": request.to_dict()["timeout_seconds"],
        }
        object.__setattr__(self, "_payload_json", canonical_json(_json_object(normalized)))

    @classmethod
    def from_dict(cls, value: Any) -> PreparedProviderRequest:
        instance = object.__new__(cls)
        instance._initialize(value)
        return instance

    @classmethod
    def from_json(cls, source: str | bytes) -> PreparedProviderRequest:
        return cls.from_dict(_load_json(source))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def version(self) -> int:
        return _VERSION

    @property
    def operation(self) -> str:
        return cast(str, self._value("operation"))

    @property
    def provider(self) -> str:
        return cast(str, self._value("provider"))

    @property
    def target(self) -> str:
        return cast(str, self._value("target"))

    @property
    def target_identity(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._value("target_identity"))

    @property
    def session(self) -> dict[str, Any] | None:
        value = self._value("session")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def handle(self) -> dict[str, Any] | None:
        value = self._value("handle")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def options(self) -> dict[str, Any] | None:
        value = self._value("options")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def prompt(self) -> str | None:
        return cast(str | None, self._value("prompt"))

    @property
    def response(self) -> dict[str, Any] | None:
        value = self._value("response")
        return None if value is None else cast(dict[str, Any], value)

    @property
    def wait(self) -> bool | None:
        return cast(bool | None, self._value("wait"))

    @property
    def timeout_seconds(self) -> int | None:
        return cast(int | None, self._value("timeout_seconds"))


@dataclass(frozen=True, slots=True)
class AgentError:
    code: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _SAFE_MESSAGES:
            _fail()

    @property
    def message(self) -> str:
        return _SAFE_MESSAGES[self.code]

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, value: Any) -> AgentError:
        item = _exact_object(value, frozenset({"code", "message"}))
        if type(item["code"]) is not str or item["code"] not in _SAFE_MESSAGES:
            _fail()
        error = cls(item["code"])
        if type(item["message"]) is not str or item["message"] != error.message:
            _fail()
        return error


_USAGE_KEYS = frozenset({"input_tokens", "output_tokens", "total_tokens", "source"})


@dataclass(frozen=True, slots=True, init=False)
class AgentUsage:
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        source: str | None,
    ) -> None:
        self._initialize(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "source": source,
            }
        )

    def _initialize(self, value: Any) -> None:
        item = _exact_object(value, _USAGE_KEYS)
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            token_count = item[name]
            if token_count is not None and (type(token_count) is not int or token_count < 0):
                _fail()
        if item["source"] is not None and type(item["source"]) is not str:
            _fail()
        object.__setattr__(self, "_payload_json", canonical_json(item))

    @classmethod
    def from_dict(cls, value: Any) -> AgentUsage:
        instance = object.__new__(cls)
        instance._initialize(value)
        return instance

    @classmethod
    def from_json(cls, source: str | bytes) -> AgentUsage:
        return cls.from_dict(_load_json(source))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def input_tokens(self) -> int | None:
        return cast(int | None, self._value("input_tokens"))

    @property
    def output_tokens(self) -> int | None:
        return cast(int | None, self._value("output_tokens"))

    @property
    def total_tokens(self) -> int | None:
        return cast(int | None, self._value("total_tokens"))

    @property
    def source(self) -> str | None:
        return cast(str | None, self._value("source"))


def _normalize_usage(value: Any) -> AgentUsage | None:
    if value is None:
        return None
    if type(value) is AgentUsage:
        return value
    return AgentUsage.from_dict(value)


def _normalize_error(value: Any) -> AgentError | None:
    if value is None:
        return None
    if type(value) is AgentError:
        return value
    return AgentError.from_dict(value)


def _normalize_handle(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_object(value)


_RECEIPT_KEYS = frozenset({"version", "disposition", "handle", "remote_state", "error"})


@dataclass(frozen=True, slots=True, init=False)
class DispatchReceipt:
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        disposition: str,
        *,
        handle: object | None = None,
        remote_state: str | None = None,
        error: AgentError | dict[str, Any] | None = None,
    ) -> None:
        if type(disposition) is not str or disposition not in _DISPATCH_DISPOSITIONS:
            _fail()
        if remote_state is not None and type(remote_state) is not str:
            _fail()
        normalized_error = _normalize_error(error)
        if disposition == "REJECTED" and normalized_error is None:
            normalized_error = AgentError("dispatch_rejected")
        if disposition == "UNCERTAIN" and normalized_error is None:
            normalized_error = AgentError("dispatch_uncertain")
        if disposition == "ACCEPTED" and normalized_error is not None:
            _fail()
        payload = {
            "version": _VERSION,
            "disposition": disposition,
            "handle": _normalize_handle(handle),
            "remote_state": remote_state,
            "error": None if normalized_error is None else normalized_error.to_dict(),
        }
        object.__setattr__(self, "_payload_json", canonical_json(_json_object(payload)))

    @classmethod
    def accepted(
        cls, *, handle: object | None = None, remote_state: str | None = None
    ) -> DispatchReceipt:
        return cls("ACCEPTED", handle=handle, remote_state=remote_state)

    @classmethod
    def rejected(
        cls,
        code: str = "dispatch_rejected",
        *,
        handle: object | None = None,
        remote_state: str | None = None,
    ) -> DispatchReceipt:
        return cls("REJECTED", handle=handle, remote_state=remote_state, error=AgentError(code))

    @classmethod
    def uncertain(
        cls, *, handle: object | None = None, remote_state: str | None = None
    ) -> DispatchReceipt:
        return cls(
            "UNCERTAIN",
            handle=handle,
            remote_state=remote_state,
            error=AgentError("dispatch_uncertain"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> DispatchReceipt:
        item = _exact_object(value, _RECEIPT_KEYS)
        if type(item["version"]) is not int or item["version"] != _VERSION:
            _fail()
        if type(item["disposition"]) is not str:
            _fail()
        if item["disposition"] in {"REJECTED", "UNCERTAIN"} and item["error"] is None:
            _fail()
        error = None if item["error"] is None else AgentError.from_dict(item["error"])
        return cls(
            item["disposition"],
            handle=item["handle"],
            remote_state=item["remote_state"],
            error=error,
        )

    @classmethod
    def from_json(cls, source: str | bytes) -> DispatchReceipt:
        return cls.from_dict(_load_json(source))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def version(self) -> int:
        return _VERSION

    @property
    def disposition(self) -> str:
        return cast(str, self._value("disposition"))

    @property
    def handle(self) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self._value("handle"))

    @property
    def remote_state(self) -> str | None:
        return cast(str | None, self._value("remote_state"))

    @property
    def error(self) -> dict[str, str] | None:
        return cast(dict[str, str] | None, self._value("error"))


_OBSERVATION_KEYS = frozenset(
    {"version", "state", "handle", "remote_state", "final_text", "usage", "error"}
)


@dataclass(frozen=True, slots=True, init=False)
class Observation:
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        state: str,
        *,
        handle: object | None = None,
        remote_state: str | None = None,
        final_text: str | None = None,
        usage: AgentUsage | dict[str, Any] | None = None,
        error: AgentError | dict[str, Any] | None = None,
    ) -> None:
        if (
            type(state) is not str
            or state not in _OBSERVATION_STATES
            or (remote_state is not None and type(remote_state) is not str)
            or (final_text is not None and type(final_text) is not str)
        ):
            _fail()
        normalized_usage = _normalize_usage(usage)
        normalized_error = _normalize_error(error)
        payload = {
            "version": _VERSION,
            "state": state,
            "handle": _normalize_handle(handle),
            "remote_state": remote_state,
            "final_text": final_text,
            "usage": None if normalized_usage is None else normalized_usage.to_dict(),
            "error": None if normalized_error is None else normalized_error.to_dict(),
        }
        object.__setattr__(self, "_payload_json", canonical_json(_json_object(payload)))

    @classmethod
    def from_dict(cls, value: Any) -> Observation:
        item = _exact_object(value, _OBSERVATION_KEYS)
        if type(item["version"]) is not int or item["version"] != _VERSION:
            _fail()
        usage = None if item["usage"] is None else AgentUsage.from_dict(item["usage"])
        error = None if item["error"] is None else AgentError.from_dict(item["error"])
        return cls(
            item["state"],
            handle=item["handle"],
            remote_state=item["remote_state"],
            final_text=item["final_text"],
            usage=usage,
            error=error,
        )

    @classmethod
    def from_json(cls, source: str | bytes) -> Observation:
        return cls.from_dict(_load_json(source))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def version(self) -> int:
        return _VERSION

    @property
    def state(self) -> str:
        return cast(str, self._value("state"))

    @property
    def handle(self) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self._value("handle"))

    @property
    def remote_state(self) -> str | None:
        return cast(str | None, self._value("remote_state"))

    @property
    def final_text(self) -> str | None:
        return cast(str | None, self._value("final_text"))

    @property
    def usage(self) -> AgentUsage | None:
        value = self._value("usage")
        return None if value is None else AgentUsage.from_dict(value)

    @property
    def error(self) -> dict[str, str] | None:
        return cast(dict[str, str] | None, self._value("error"))


_RESULT_KEYS = frozenset(
    {
        "version",
        "invocation_id",
        "operation",
        "provider",
        "target",
        "status",
        "handle",
        "remote_state",
        "final_text",
        "data",
        "usage",
        "error",
    }
)


@dataclass(frozen=True, slots=True, init=False)
class AgentResult:
    _payload_json: str = field(repr=False)

    def __init__(
        self,
        *,
        invocation_id: str,
        operation: str,
        provider: str,
        target: str,
        status: str,
        handle: object | None = None,
        remote_state: str | None = None,
        final_text: str | None = None,
        data: Any = None,
        usage: AgentUsage | dict[str, Any] | None = None,
        error: AgentError | dict[str, Any] | None = None,
    ) -> None:
        if (
            type(invocation_id) is not str
            or not invocation_id
            or type(operation) is not str
            or operation not in _OPERATIONS
            or not _identifier(provider)
            or not _identifier(target)
            or type(status) is not str
            or status not in _STATUSES
            or (remote_state is not None and type(remote_state) is not str)
            or (final_text is not None and type(final_text) is not str)
        ):
            _fail()
        normalized_usage = _normalize_usage(usage)
        normalized_error = _normalize_error(error)
        payload = {
            "version": _VERSION,
            "invocation_id": invocation_id,
            "operation": operation,
            "provider": provider,
            "target": target,
            "status": status,
            "handle": _normalize_handle(handle),
            "remote_state": remote_state,
            "final_text": final_text,
            "data": _json_copy(data),
            "usage": None if normalized_usage is None else normalized_usage.to_dict(),
            "error": None if normalized_error is None else normalized_error.to_dict(),
        }
        object.__setattr__(self, "_payload_json", canonical_json(_json_object(payload)))

    @classmethod
    def from_dict(cls, value: Any) -> AgentResult:
        item = _exact_object(value, _RESULT_KEYS)
        if type(item["version"]) is not int or item["version"] != _VERSION:
            _fail()
        usage = None if item["usage"] is None else AgentUsage.from_dict(item["usage"])
        error = None if item["error"] is None else AgentError.from_dict(item["error"])
        return cls(
            invocation_id=item["invocation_id"],
            operation=item["operation"],
            provider=item["provider"],
            target=item["target"],
            status=item["status"],
            handle=item["handle"],
            remote_state=item["remote_state"],
            final_text=item["final_text"],
            data=item["data"],
            usage=usage,
            error=error,
        )

    @classmethod
    def from_json(cls, source: str | bytes) -> AgentResult:
        return cls.from_dict(_load_json(source))

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _load_json(self._payload_json))

    def to_json(self) -> str:
        return self._payload_json

    def _value(self, name: str) -> Any:
        return self.to_dict()[name]

    @property
    def version(self) -> int:
        return _VERSION

    @property
    def invocation_id(self) -> str:
        return cast(str, self._value("invocation_id"))

    @property
    def operation(self) -> str:
        return cast(str, self._value("operation"))

    @property
    def provider(self) -> str:
        return cast(str, self._value("provider"))

    @property
    def target(self) -> str:
        return cast(str, self._value("target"))

    @property
    def status(self) -> str:
        return cast(str, self._value("status"))

    @property
    def handle(self) -> dict[str, Any] | None:
        return cast(dict[str, Any] | None, self._value("handle"))

    @property
    def remote_state(self) -> str | None:
        return cast(str | None, self._value("remote_state"))

    @property
    def final_text(self) -> str | None:
        return cast(str | None, self._value("final_text"))

    @property
    def data(self) -> Any:
        return self._value("data")

    @property
    def usage(self) -> AgentUsage | None:
        value = self._value("usage")
        return None if value is None else AgentUsage.from_dict(value)

    @property
    def error(self) -> dict[str, str] | None:
        return cast(dict[str, str] | None, self._value("error"))


def _result(
    invocation_id: str,
    request: AgentRequest,
    *,
    status: str,
    handle: object | None = None,
    remote_state: str | None = None,
    final_text: str | None = None,
    data: Any = None,
    usage: AgentUsage | None = None,
    error: AgentError | None = None,
) -> AgentResult:
    return AgentResult(
        invocation_id=invocation_id,
        operation=request.operation,
        provider=request.provider,
        target=request.target,
        status=status,
        handle=handle,
        remote_state=remote_state,
        final_text=final_text,
        data=data,
        usage=usage,
        error=error,
    )


def result_from_dispatch_receipt(
    invocation_id: str,
    request: AgentRequest,
    receipt: DispatchReceipt,
) -> AgentResult:
    """Map dispatch certainty to a result without treating acceptance as success."""
    if type(request) is not AgentRequest or type(receipt) is not DispatchReceipt:
        _fail()
    if receipt.disposition == "ACCEPTED":
        status = "ACCEPTED"
        error = None
    elif receipt.disposition == "REJECTED":
        status = "FAILED"
        error = _normalize_error(receipt.error) or AgentError("dispatch_rejected")
    else:
        status = "UNKNOWN"
        error = _normalize_error(receipt.error) or AgentError("dispatch_uncertain")
    return _result(
        invocation_id,
        request,
        status=status,
        handle=receipt.handle,
        remote_state=receipt.remote_state,
        error=error,
    )


def result_from_observation(
    invocation_id: str,
    request: AgentRequest,
    observation: Observation,
) -> AgentResult:
    """Map one provider observation and strictly validate a completed response."""
    if type(request) is not AgentRequest or type(observation) is not Observation:
        _fail()
    state = observation.state
    handle = observation.handle
    remote_state = observation.remote_state
    final_text = observation.final_text
    usage = observation.usage
    error = None if observation.error is None else AgentError.from_dict(observation.error)
    if state == "RUNNING":
        return _result(
            invocation_id,
            request,
            status="RUNNING",
            handle=handle,
            remote_state=remote_state,
            usage=usage,
        )
    if state == "COMPLETED":
        if final_text is None:
            return _result(
                invocation_id,
                request,
                status="UNKNOWN",
                handle=handle,
                remote_state=remote_state,
                usage=usage,
                error=AgentError("response_unavailable"),
            )
        response = request.response or {"format": "text"}
        if response["format"] == "text":
            return _result(
                invocation_id,
                request,
                status="SUCCEEDED",
                handle=handle,
                remote_state=remote_state,
                final_text=final_text,
                usage=usage,
            )
        try:
            parsed = parse_strict_json(final_text)
            if "schema" in response:
                validate_json_value_schema(response["schema"], parsed)
        except ContractResourceLimit:
            error = AgentError("output_too_large")
        except (ContractOutputInvalid, ContractError, TypeError, ValueError, RecursionError):
            error = AgentError("output_invalid")
        else:
            return _result(
                invocation_id,
                request,
                status="SUCCEEDED",
                handle=handle,
                remote_state=remote_state,
                final_text=final_text,
                data=parsed,
                usage=usage,
            )
        return _result(
            invocation_id,
            request,
            status="FAILED",
            handle=handle,
            remote_state=remote_state,
            final_text=final_text,
            usage=usage,
            error=error,
        )
    if state == "FAILED":
        error = error or AgentError("remote_failed")
    elif state == "TIMED_OUT":
        error = error or AgentError("timeout")
    elif state == "UNKNOWN":
        error = error or AgentError("observation_unavailable")
    return _result(
        invocation_id,
        request,
        status=state,
        handle=handle,
        remote_state=remote_state,
        final_text=final_text,
        usage=usage,
        error=error,
    )


__all__ = [
    "AgentCallDefinition",
    "AgentContractError",
    "AgentError",
    "AgentRequest",
    "AgentResult",
    "AgentUsage",
    "DispatchReceipt",
    "Observation",
    "OptionCapability",
    "OptionConstraint",
    "PreparedProviderRequest",
    "ProviderCapabilities",
    "ProviderProtocol",
    "ProviderRegistry",
    "TargetContext",
    "codex_agent_capabilities",
    "codex_agent_i4b_registry",
    "evaluate_agent_request",
    "result_from_dispatch_receipt",
    "result_from_observation",
    "validate_agent_wire",
]
