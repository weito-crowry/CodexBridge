"""Classification of call targets found in the OWS task tree only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StrictTaskTreeKind(StrEnum):
    LEGACY = "legacy"
    STRICT_PURE = "strict_pure"
    STRICT_CODEX_JOB = "strict_codex_job"
    STRICT_AGENT = "strict_agent"


class TaskTreeClassificationError(ValueError):
    """A task tree cannot be classified or mixes external call families."""


@dataclass(frozen=True, slots=True)
class TaskCall:
    target: str
    task_name: str
    node_pointer: str
    under_for: bool
    task_spec: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class StrictTaskTreeClassification:
    kind: StrictTaskTreeKind
    calls: tuple[TaskCall, ...]

    @property
    def agent_calls(self) -> tuple[TaskCall, ...]:
        return tuple(call for call in self.calls if call.target == "codexbridge-agent")

    @property
    def codex_job_calls(self) -> tuple[TaskCall, ...]:
        return tuple(call for call in self.calls if call.target == "codex-job")

    @property
    def mock_job_calls(self) -> tuple[TaskCall, ...]:
        return tuple(call for call in self.calls if call.target == "mock-job")

    @property
    def agent_calls_under_for(self) -> tuple[TaskCall, ...]:
        return tuple(call for call in self.agent_calls if call.under_for)


def classify_task_tree(
    definition: object,
    *,
    strict_contract: bool,
) -> StrictTaskTreeClassification:
    """Classify real task calls without searching prompt, schema, or data objects."""
    if type(strict_contract) is not bool or type(definition) is not dict:
        raise TaskTreeClassificationError("workflow task tree is invalid")
    tasks = definition.get("do")
    if type(tasks) is not list:
        raise TaskTreeClassificationError("workflow task tree is invalid")
    calls: list[TaskCall] = []

    def walk(task_list: list[Any], pointer: str, *, under_for: bool = False) -> None:
        for index, task_item in enumerate(task_list):
            if type(task_item) is not dict or len(task_item) != 1:
                raise TaskTreeClassificationError("workflow task tree is invalid")
            task_name, task_spec = next(iter(task_item.items()))
            if type(task_name) is not str or not task_name or type(task_spec) is not dict:
                raise TaskTreeClassificationError("workflow task tree is invalid")
            escaped_name = task_name.replace("~", "~0").replace("/", "~1")
            node_pointer = f"{pointer}/{index}/{escaped_name}"
            action_keys = set(task_spec) - {"then", "input", "output", "export"}
            if "call" in action_keys:
                target = task_spec.get("call")
                if type(target) is not str:
                    raise TaskTreeClassificationError("workflow call target is invalid")
                calls.append(TaskCall(target, task_name, node_pointer, under_for, task_spec))
            elif "for" in action_keys and "do" in task_spec:
                nested = task_spec.get("do")
                if type(nested) is not list:
                    raise TaskTreeClassificationError("workflow loop body is invalid")
                walk(nested, f"{node_pointer}/do", under_for=True)
            elif "do" in action_keys:
                nested = task_spec.get("do")
                if type(nested) is not list:
                    raise TaskTreeClassificationError("workflow nested task list is invalid")
                walk(nested, f"{node_pointer}/do", under_for=under_for)

    walk(tasks, "/do")
    agent_calls = tuple(call for call in calls if call.target == "codexbridge-agent")
    codex_calls = tuple(call for call in calls if call.target == "codex-job")
    mock_calls = tuple(call for call in calls if call.target == "mock-job")
    if strict_contract and agent_calls and (codex_calls or mock_calls):
        raise TaskTreeClassificationError("Agent calls cannot be mixed with job calls")
    if not strict_contract:
        kind = StrictTaskTreeKind.LEGACY
    elif agent_calls:
        kind = StrictTaskTreeKind.STRICT_AGENT
    elif codex_calls:
        kind = StrictTaskTreeKind.STRICT_CODEX_JOB
    else:
        kind = StrictTaskTreeKind.STRICT_PURE
    return StrictTaskTreeClassification(kind, tuple(calls))


def definition_has_call_target(definition: object, target: str) -> bool:
    try:
        return any(
            call.target == target
            for call in classify_task_tree(definition, strict_contract=False).calls
        )
    except TaskTreeClassificationError:
        return False


__all__ = [
    "StrictTaskTreeClassification",
    "StrictTaskTreeKind",
    "TaskCall",
    "TaskTreeClassificationError",
    "classify_task_tree",
    "definition_has_call_target",
]
