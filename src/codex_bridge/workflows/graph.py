from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import CompiledContract
from .interpreter import OwsError, validate_workflow

_TASK_OPERATIONS = frozenset({"do", "set", "switch", "for", "call"})


@dataclass
class _TaskRecord:
    node_id: str
    task_name: str
    operation: str
    scope_pointer: str
    order: int
    task_spec: dict[str, Any]


@dataclass
class _ScopeRecord:
    scope_pointer: str
    parent_scope: str | None
    owner_node: str | None
    tasks: list[_TaskRecord] = field(default_factory=list)
    child_scopes: dict[str, _ScopeRecord] = field(default_factory=dict)

    @property
    def entry_node(self) -> str:
        return f"@scope:{self.scope_pointer}:entry"

    @property
    def exit_node(self) -> str:
        return f"@scope:{self.scope_pointer}:exit"


def project_workflow_graph(
    definition: Mapping[str, Any],
    *,
    codex_job_live: bool = False,
    max_live_codex_jobs: int = 1,
    strict_contract: CompiledContract | None = None,
) -> dict[str, Any]:
    """Project a validated OWS subset definition into a static JSON graph."""
    if not isinstance(definition, Mapping):
        raise OwsError("workflow definition must be an object")
    if type(codex_job_live) is not bool:
        raise OwsError("codex_job_live must be a boolean")
    try:
        copied_definition = _copy_json_value(definition)
        json.dumps(copied_definition, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError, OwsError):
        raise OwsError("workflow definition must contain only JSON values") from None
    if not isinstance(copied_definition, dict):
        raise OwsError("workflow definition must be an object")

    validate_workflow(
        copied_definition,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
        strict_contract=strict_contract,
    )

    nodes: list[dict[str, Any]] = []
    scopes: list[_ScopeRecord] = []

    def collect_scope(
        tasks: list[dict[str, Any]],
        scope_pointer: str,
        parent_scope: str | None,
        owner_node: str | None,
    ) -> _ScopeRecord:
        scope = _ScopeRecord(
            scope_pointer=scope_pointer,
            parent_scope=parent_scope,
            owner_node=owner_node,
        )
        scopes.append(scope)
        for order, task_item in enumerate(tasks):
            task_name, task_spec = next(iter(task_item.items()))
            operation = _task_operation(task_spec)
            node_id = f"{scope_pointer}/{order}/{_escape_pointer_segment(task_name)}"
            record = _TaskRecord(
                node_id=node_id,
                task_name=task_name,
                operation=operation,
                scope_pointer=scope_pointer,
                order=order,
                task_spec=task_spec,
            )
            scope.tasks.append(record)
            nodes.append(
                {
                    "node_id": node_id,
                    "task_name": task_name,
                    "operation": operation,
                    "parent_scope": scope_pointer,
                    "order": order,
                    "attributes": {key: value for key, value in task_spec.items() if key != "do"},
                }
            )
            if operation in {"do", "for"}:
                child_pointer = f"{node_id}/do"
                scope.child_scopes[node_id] = collect_scope(
                    task_spec["do"],
                    child_pointer,
                    scope_pointer,
                    node_id,
                )
        return scope

    root_tasks = copied_definition.get("do")
    if not isinstance(root_tasks, list):
        raise OwsError("top-level do must be a non-empty task list")
    root_scope = collect_scope(root_tasks, "/do", None, None)

    edges: list[dict[str, Any]] = []

    def add_edge(
        source: str,
        target: str,
        kind: str,
        **metadata: Any,
    ) -> None:
        edge: dict[str, Any] = {
            "source": source,
            "target": target,
            "kind": kind,
            "order": len(edges),
        }
        edge.update(metadata)
        edges.append(edge)

    def continuation(
        scope: _ScopeRecord,
        task_index: int,
        directive: str,
    ) -> tuple[str, str]:
        if directive == "exit":
            return scope.exit_node, "scope_exit"
        if directive == "continue":
            next_index = task_index + 1
            if next_index < len(scope.tasks):
                return scope.tasks[next_index].node_id, "sequence"
            return scope.exit_node, "scope_exit"
        for target in scope.tasks:
            if target.task_name == directive:
                return target.node_id, "jump"
        raise OwsError("workflow jump target is not in its validated scope")

    def project_scope_edges(scope: _ScopeRecord) -> None:
        if not scope.tasks:
            raise OwsError("workflow scope must contain at least one task")
        add_edge(scope.entry_node, scope.tasks[0].node_id, "scope_entry")
        for task in scope.tasks:
            directive = task.task_spec.get("then", "continue")
            if task.operation == "switch":
                cases = task.task_spec["switch"]
                has_default = False
                for case_index, case in enumerate(cases):
                    case_name, case_spec = next(iter(case.items()))
                    is_default = "when" not in case_spec
                    has_default = has_default or is_default
                    target, _ = continuation(scope, task.order, case_spec["then"])
                    metadata: dict[str, Any] = {
                        "case_index": case_index,
                        "case_name": case_name,
                        "case_default": is_default,
                        "then": case_spec["then"],
                    }
                    if "when" in case_spec:
                        metadata["when"] = case_spec["when"]
                    add_edge(task.node_id, target, "branch", **metadata)
                if not has_default:
                    target, _ = continuation(scope, task.order, "continue")
                    add_edge(
                        task.node_id,
                        target,
                        "fallthrough",
                        case_index=len(cases),
                        case_name=None,
                        case_default=False,
                        then="continue",
                        no_match=True,
                    )
                continue

            target, target_kind = continuation(scope, task.order, directive)
            if task.operation == "do":
                child = scope.child_scopes[task.node_id]
                add_edge(task.node_id, child.entry_node, "scope_entry")
                add_edge(
                    child.exit_node,
                    target,
                    "scope_return",
                    then=directive,
                    continuation_kind=target_kind,
                )
                project_scope_edges(child)
            elif task.operation == "for":
                child = scope.child_scopes[task.node_id]
                add_edge(task.node_id, child.entry_node, "loop_body")
                add_edge(
                    task.node_id,
                    target,
                    "loop_exit",
                    iteration="zero",
                    then=directive,
                    continuation_kind=target_kind,
                )
                project_scope_edges(child)
                add_edge(
                    child.exit_node,
                    child.entry_node,
                    "loop_repeat",
                    iteration="after_body",
                )
                add_edge(
                    child.exit_node,
                    target,
                    "loop_exit",
                    iteration="after_body",
                    then=directive,
                    continuation_kind=target_kind,
                )
            else:
                add_edge(task.node_id, target, target_kind)

    project_scope_edges(root_scope)

    public_scopes = [
        {
            "scope_pointer": scope.scope_pointer,
            "parent_scope": scope.parent_scope,
            "owner_node": scope.owner_node,
            "child_task_ids": [task.node_id for task in scope.tasks],
            "entry_node": scope.entry_node,
            "exit_node": scope.exit_node,
        }
        for scope in scopes
    ]
    metadata = {
        key: copied_definition[key]
        for key in ("document", "evaluate", "input", "output")
        if key in copied_definition
    }
    return {
        "schema_version": 1,
        "metadata": metadata,
        "nodes": nodes,
        "edges": edges,
        "scopes": public_scopes,
        "static_task_count": len(nodes),
    }


def _task_operation(task_spec: dict[str, Any]) -> str:
    if "call" in task_spec:
        operation = "call"
    elif "for" in task_spec:
        operation = "for"
    elif "do" in task_spec:
        operation = "do"
    else:
        operations = _TASK_OPERATIONS.intersection(task_spec)
        if len(operations) != 1:
            raise OwsError("workflow task must have exactly one supported operation")
        operation = next(iter(operations))
    if operation not in _TASK_OPERATIONS:
        raise OwsError("workflow task operation is unsupported")
    return operation


def _escape_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _copy_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if type(value) is float:
        if math.isfinite(value):
            return value
        raise OwsError("workflow definition contains a non-finite number")
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise OwsError("workflow definition contains a non-string object key")
        return {key: _copy_json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json_value(child) for child in value]
    raise OwsError("workflow definition contains a non-JSON value")
