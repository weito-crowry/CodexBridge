from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from .contracts import canonical_json

_BINDING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_SAFE_MESSAGES = {
    "invalid_prompt": "Prompt must be a string.",
    "invalid_unicode": "Prompt contains invalid Unicode.",
    "unclosed_placeholder": "Prompt contains an unclosed placeholder.",
    "invalid_placeholder": "Prompt contains an invalid placeholder.",
    "invalid_bindings": "Prompt bindings must be an object.",
    "invalid_binding_name": "Prompt binding name is invalid.",
    "unknown_binding": "Prompt references a binding that is not defined.",
    "unused_binding": "Prompt defines a binding that is not used.",
    "invalid_binding": "Prompt binding definition is invalid.",
    "invalid_binding_shape": "Prompt binding must contain only from and format.",
    "invalid_from": "Prompt binding from must be an explicit jq expression.",
    "invalid_format": "Prompt binding format must be text or json.",
    "invalid_evaluator": "Prompt binding evaluator is invalid.",
    "binding_evaluation_failed": "Prompt binding evaluation failed.",
    "text_binding_type": "Text prompt bindings must evaluate to strings.",
    "json_binding_type": "JSON prompt binding is not a JSON value.",
    "empty_prompt": "Rendered prompt must not be empty or whitespace only.",
}


class AgentPromptError(ValueError):
    """Safe, classified errors raised while parsing or rendering Agent prompts."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_MESSAGES else "invalid_prompt"
        super().__init__(_SAFE_MESSAGES[self.code])


@dataclass(frozen=True, slots=True)
class PromptPlaceholder:
    name: str


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    segments: tuple[str | PromptPlaceholder, ...]
    placeholder_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class PromptBinding:
    from_expression: str
    format: str


def parse_prompt_template(prompt: str) -> PromptTemplate:
    """Parse one literal prompt using the D2 placeholder and escape rules."""
    if type(prompt) is not str:
        raise AgentPromptError("invalid_prompt")
    try:
        prompt.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise AgentPromptError("invalid_unicode") from None

    segments: list[str | PromptPlaceholder] = []
    placeholder_names: set[str] = set()
    literal: list[str] = []
    index = 0

    def flush_literal() -> None:
        if literal:
            segments.append("".join(literal))
            literal.clear()

    while index < len(prompt):
        if prompt.startswith("{{{{", index):
            literal.append("{{")
            index += 4
            continue
        if prompt.startswith("{{", index):
            closing = prompt.find("}}", index + 2)
            if closing < 0:
                raise AgentPromptError("unclosed_placeholder")
            name = prompt[index + 2 : closing]
            if _BINDING_NAME.fullmatch(name) is None:
                raise AgentPromptError("invalid_placeholder")
            flush_literal()
            segments.append(PromptPlaceholder(name))
            placeholder_names.add(name)
            index = closing + 2
            continue
        literal.append(prompt[index])
        index += 1

    flush_literal()
    return PromptTemplate(tuple(segments), frozenset(placeholder_names))


def validate_prompt_bindings(
    bindings: object,
    placeholder_names: Collection[str],
) -> dict[str, PromptBinding]:
    """Validate the closed binding map and require exact placeholder coverage."""
    if type(bindings) is not dict:
        raise AgentPromptError("invalid_bindings")

    if any(type(name) is not str or _BINDING_NAME.fullmatch(name) is None for name in bindings):
        raise AgentPromptError("invalid_binding_name")
    if any(
        type(name) is not str or _BINDING_NAME.fullmatch(name) is None for name in placeholder_names
    ):
        raise AgentPromptError("invalid_placeholder")

    keys = set(bindings)
    names = set(placeholder_names)
    if names - keys:
        raise AgentPromptError("unknown_binding")
    if keys - names:
        raise AgentPromptError("unused_binding")

    validated: dict[str, PromptBinding] = {}
    for name, raw_definition in bindings.items():
        if type(raw_definition) is not dict:
            raise AgentPromptError("invalid_binding")
        if set(raw_definition) != {"from", "format"}:
            raise AgentPromptError("invalid_binding_shape")

        from_expression = raw_definition["from"]
        if type(from_expression) is not str or not _is_explicit_ows_expression(from_expression):
            raise AgentPromptError("invalid_from")
        binding_format = raw_definition["format"]
        if type(binding_format) is not str or binding_format not in {"text", "json"}:
            raise AgentPromptError("invalid_format")
        validated[name] = PromptBinding(from_expression, binding_format)
    return validated


def render_prompt_template(
    prompt: str,
    bindings: object,
    evaluate_single_result: Callable[[str], Any],
) -> str:
    """Render with one typed result per binding using an injected jq evaluator."""
    if not callable(evaluate_single_result):
        raise AgentPromptError("invalid_evaluator")
    template = parse_prompt_template(prompt)
    validated = validate_prompt_bindings(bindings, template.placeholder_names)

    evaluated: dict[str, str] = {}
    rendered: list[str] = []
    for segment in template.segments:
        if isinstance(segment, str):
            rendered.append(segment)
            continue
        if segment.name not in evaluated:
            binding = validated[segment.name]
            try:
                value = evaluate_single_result(binding.from_expression)
            except Exception:
                raise AgentPromptError("binding_evaluation_failed") from None
            if binding.format == "text":
                if type(value) is not str:
                    raise AgentPromptError("text_binding_type")
                evaluated[segment.name] = value
            else:
                try:
                    evaluated[segment.name] = canonical_json(value)
                except Exception:
                    raise AgentPromptError("json_binding_type") from None
        rendered.append(evaluated[segment.name])

    result = "".join(rendered)
    try:
        result.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise AgentPromptError("invalid_unicode") from None
    if not result.strip():
        raise AgentPromptError("empty_prompt")
    return result


def collect_agent_operation_expressions(operation: object) -> tuple[str, ...]:
    """Collect only the D2-I0 fields whose values may be explicit OWS jq."""
    if type(operation) is not dict:
        return ()

    expressions: list[str] = []

    def collect(value: object) -> None:
        if type(value) is str and _is_explicit_ows_expression(value):
            expressions.append(value)

    raw_bindings = operation.get("bindings")
    if type(raw_bindings) is dict:
        for raw_binding in raw_bindings.values():
            if type(raw_binding) is dict:
                collect(raw_binding.get("from"))

    raw_session = operation.get("session")
    if type(raw_session) is dict:
        collect(raw_session.get("thread_id"))

    collect(operation.get("handle"))

    raw_options = operation.get("options")
    if type(raw_options) is dict:
        for value in raw_options.values():
            collect(value)

    collect(operation.get("timeout_seconds"))
    collect(operation.get("wait"))
    return tuple(expressions)


def _is_explicit_ows_expression(value: str) -> bool:
    return value.startswith("${") and value.endswith("}") and bool(value[2:-1].strip())
