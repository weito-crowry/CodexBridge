from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jq
import yaml
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "workflow.yaml"
SUPPORTED_TASKS = frozenset({"do", "set", "switch", "for"})
SUPPORTED_DIRECTIVES = frozenset({"continue", "exit"})
_JQ_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OwsError(ValueError):
    """Raised when a workflow is invalid or outside this PoC's subset."""


@dataclass(frozen=True)
class _ScopeResult:
    value: Any
    exited: bool = False


StepRunner = Callable[[str, str, str, dict[str, Any], Any, dict[str, Any]], Any]


def parse_workflow_text(text: str) -> dict[str, Any]:
    try:
        definition = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise OwsError(f"YAML parse failed: {error}") from error
    if not isinstance(definition, dict):
        raise OwsError("workflow YAML root must be an object")
    return definition


def load_workflow(path: Path, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise OwsError(f"cannot read workflow file {path}: {error}") from error
    definition = parse_workflow_text(text)
    validate_workflow(definition, schema_path=schema_path)
    return definition


def validate_official_schema(definition: Any, schema_path: Path = SCHEMA_PATH) -> None:
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(definition),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise OwsError(f"could not load official OWS v1.0.3 schema: {error}") from error
    if errors:
        error = errors[0]
        location = "/" + "/".join(str(part) for part in error.absolute_path)
        raise OwsError(f"official OWS v1.0.3 schema violation at {location}: {error.message}")


def validate_workflow(definition: Any, schema_path: Path = SCHEMA_PATH) -> None:
    _validate_subset(definition)
    validate_official_schema(definition, schema_path=schema_path)


def _validate_subset(definition: Any) -> None:
    if not isinstance(definition, dict):
        raise OwsError("workflow definition must be an object")

    for key in definition:
        if key not in {"document", "evaluate", "do"}:
            if key == "input":
                raise OwsError("unsupported top-level input; input.from is outside this subset")
            if key == "output":
                raise OwsError("unsupported top-level output; output.as is outside this subset")
            if key == "use":
                raise OwsError("unsupported top-level use")
            raise OwsError(f"unsupported top-level key '{key}'")

    document = definition.get("document")
    if not isinstance(document, dict):
        raise OwsError("document must be an object")
    if document.get("dsl") != "1.0.3" or not isinstance(document.get("dsl"), str):
        raise OwsError("DSL version must be the string '1.0.3'")

    evaluate = definition.get("evaluate")
    if evaluate != {"language": "jq", "mode": "strict"}:
        raise OwsError("evaluate must explicitly set language=jq and mode=strict")

    tasks = definition.get("do")
    if not isinstance(tasks, list) or not tasks:
        raise OwsError("top-level do must be a non-empty task list")
    _validate_task_list(tasks, "do", frozenset())


def _validate_task_list(tasks: Any, scope_path: str, jq_variables: frozenset[str]) -> None:
    if not isinstance(tasks, list) or not tasks:
        raise OwsError(f"{scope_path} must be a non-empty task list")

    names: list[str] = []
    parsed_tasks: list[tuple[str, str, dict[str, Any]]] = []
    for index, task_item in enumerate(tasks):
        if not isinstance(task_item, dict) or len(task_item) != 1:
            raise OwsError(f"{scope_path}[{index}] must contain exactly one named task")
        task_name, task_spec = next(iter(task_item.items()))
        if not isinstance(task_name, str) or not task_name:
            raise OwsError(f"{scope_path}[{index}] task name must be a non-empty string")
        if task_name in names:
            raise OwsError(f"duplicate task name '{task_name}' in {scope_path}")
        names.append(task_name)
        if not isinstance(task_spec, dict):
            raise OwsError(f"task '{task_name}' definition must be an object")

        rejected_attrs = {"if", "input", "output", "export"}.intersection(task_spec)
        if rejected_attrs:
            attr = sorted(rejected_attrs)[0]
            suffix = ".from" if attr == "input" else ".as" if attr in {"output", "export"} else ""
            raise OwsError(f"unsupported task attribute '{attr}{suffix}' on '{task_name}'")

        operation, action = _task_action(task_name, task_spec)
        if operation not in SUPPORTED_TASKS:
            raise OwsError(f"unsupported task '{operation}' at {scope_path}[{index}]")
        unsupported = set(task_spec) - {"then", operation}
        if operation == "for":
            unsupported = set(task_spec) - {"then", "for", "do"}
        if unsupported:
            key = sorted(unsupported)[0]
            raise OwsError(f"unsupported task key '{key}' on '{task_name}'")

        task_path = f"{scope_path}[{index}].{task_name}"
        _validate_action(operation, action, task_name, task_path, jq_variables)
        directive = task_spec.get("then")
        if directive is not None:
            _validate_directive_shape(directive, task_name)
        if operation == "switch" and directive is not None:
            raise OwsError(f"switch task '{task_name}' must put its flow directive in a case")
        parsed_tasks.append((task_name, operation, task_spec))

    name_to_index = {name: index for index, name in enumerate(names)}
    for task_name, operation, task_spec in parsed_tasks:
        targets: list[str] = []
        if operation == "switch":
            targets.extend(_switch_targets(task_spec["switch"], task_name))
        else:
            directive = task_spec.get("then")
            if isinstance(directive, str) and directive not in SUPPORTED_DIRECTIVES:
                targets.append(directive)
        for target in targets:
            if target not in name_to_index:
                raise OwsError(
                    f"jump target '{target}' from task '{task_name}' is not in the same do scope"
                )

    _reject_jump_cycles(parsed_tasks, name_to_index, scope_path)


def _task_action(task_name: str, task_spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action_keys = set(task_spec) - {"then"}
    if "for" in action_keys:
        operation = "for"
        action = {key: task_spec[key] for key in ("for", "do") if key in task_spec}
    elif "do" in action_keys:
        operation = "do"
        action = {"do": task_spec["do"]}
    elif len(action_keys) == 1:
        operation = next(iter(action_keys))
        action = {operation: task_spec[operation]}
    elif not action_keys:
        raise OwsError(f"task '{task_name}' has no task type")
    else:
        unsupported = sorted(action_keys - SUPPORTED_TASKS)
        if unsupported:
            operation = unsupported[0]
            raise OwsError(f"unsupported task '{operation}' on '{task_name}'")
        raise OwsError(f"task '{task_name}' must define exactly one task type")
    if operation not in SUPPORTED_TASKS:
        raise OwsError(f"unsupported task '{operation}' on '{task_name}'")
    return operation, action


def _validate_action(
    operation: str,
    action: dict[str, Any],
    task_name: str,
    task_path: str,
    jq_variables: frozenset[str],
) -> None:
    if operation == "set":
        value = action["set"]
        if not isinstance(value, (dict, str)):
            raise OwsError(f"set on '{task_name}' must be a map or string")
        _validate_set_value(value, task_path, jq_variables)
        return
    if operation == "do":
        _validate_task_list(action["do"], f"{task_path}.do", jq_variables)
        return
    if operation == "for":
        for_spec = action.get("for")
        body = action.get("do")
        if not isinstance(for_spec, dict):
            raise OwsError(f"for on '{task_name}' must be an object")
        unsupported = set(for_spec) - {"in", "each", "at"}
        if unsupported:
            key = sorted(unsupported)[0]
            if key == "while":
                raise OwsError("unsupported for.while")
            raise OwsError(f"unsupported for key '{key}' on '{task_name}'")
        if "in" not in for_spec:
            raise OwsError(f"for.in is required on '{task_name}'")
        _compile_required_expression(for_spec["in"], f"{task_path}.for.in", jq_variables)
        each_name = for_spec.get("each", "item")
        at_name = for_spec.get("at", "index")
        if not isinstance(each_name, str) or not _JQ_NAME.fullmatch(each_name):
            raise OwsError(f"for.each on '{task_name}' must be a jq variable name")
        if not isinstance(at_name, str) or not _JQ_NAME.fullmatch(at_name):
            raise OwsError(f"for.at on '{task_name}' must be a jq variable name")
        if each_name == at_name:
            raise OwsError(f"for.each and for.at must use different names on '{task_name}'")
        _validate_task_list(
            body,
            f"{task_path}.do",
            jq_variables.union({each_name, at_name}),
        )
        return
    if operation == "switch":
        cases = action["switch"]
        if not isinstance(cases, list) or not cases:
            raise OwsError(f"switch on '{task_name}' must be a non-empty case list")
        defaults = 0
        for case_index, case in enumerate(cases):
            if not isinstance(case, dict) or len(case) != 1:
                raise OwsError(f"switch case {case_index} on '{task_name}' must have one name")
            case_name, case_spec = next(iter(case.items()))
            if not isinstance(case_name, str) or not isinstance(case_spec, dict):
                raise OwsError(f"invalid switch case on '{task_name}'")
            if set(case_spec) - {"when", "then"}:
                raise OwsError(f"unsupported switch case key on '{task_name}'")
            if "then" not in case_spec:
                raise OwsError(f"switch case '{case_name}' requires then")
            _validate_directive_shape(case_spec["then"], task_name)
            if "when" not in case_spec:
                defaults += 1
                if case_index != len(cases) - 1:
                    raise OwsError("switch default case must be last")
            else:
                _compile_required_expression(
                    case_spec["when"],
                    f"{task_path}.switch.{case_name}.when",
                    jq_variables,
                )
        if defaults > 1:
            raise OwsError(f"switch on '{task_name}' has more than one default")


def _validate_set_value(value: Any, path: str, jq_variables: frozenset[str]) -> None:
    if isinstance(value, str):
        expression = _optional_expression(value, path)
        if expression is not None:
            _compile_jq(expression, path, jq_variables)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_set_value(child, f"{path}.{key}", jq_variables)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_set_value(child, f"{path}[{index}]", jq_variables)


def _validate_directive_shape(directive: Any, task_name: str) -> None:
    if not isinstance(directive, str) or not directive:
        raise OwsError(f"then on '{task_name}' must be a non-empty string")
    if directive == "end":
        raise OwsError("unsupported flow directive 'end'")


def _switch_targets(cases: Any, task_name: str) -> list[str]:
    targets: list[str] = []
    for case in cases:
        case_spec = next(iter(case.values()))
        target = case_spec["then"]
        if target not in SUPPORTED_DIRECTIVES:
            targets.append(target)
    return targets


def _reject_jump_cycles(
    tasks: list[tuple[str, str, dict[str, Any]]],
    name_to_index: dict[str, int],
    scope_path: str,
) -> None:
    edges: dict[int, set[int]] = {index: set() for index in range(len(tasks))}
    for index, (_, operation, task_spec) in enumerate(tasks):
        next_index = index + 1
        if operation == "switch":
            has_default = False
            for case in task_spec["switch"]:
                case_name, case_spec = next(iter(case.items()))
                has_default = has_default or "when" not in case_spec
                directive = case_spec["then"]
                if directive in SUPPORTED_DIRECTIVES:
                    if directive == "continue" and next_index < len(tasks):
                        edges[index].add(next_index)
                else:
                    edges[index].add(name_to_index[directive])
            if not has_default and next_index < len(tasks):
                edges[index].add(next_index)
        else:
            directive = task_spec.get("then", "continue")
            if directive == "continue":
                if next_index < len(tasks):
                    edges[index].add(next_index)
            elif directive != "exit":
                edges[index].add(name_to_index[directive])

    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(index: int) -> None:
        if index in visiting:
            raise OwsError(f"jump cycle detected in {scope_path}")
        if index in visited:
            return
        visiting.add(index)
        for target in edges[index]:
            visit(target)
        visiting.remove(index)
        visited.add(index)

    for index in range(len(tasks)):
        visit(index)


def _optional_expression(value: str, path: str) -> str | None:
    if value.startswith("${") and value.endswith("}"):
        source = value[2:-1].strip()
        if not source:
            raise OwsError(f"empty jq expression at {path}")
        return source
    if "${" in value or value.startswith("${"):
        raise OwsError(f"strict jq expression must be a full ${{...}} value at {path}")
    return None


def _required_expression(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise OwsError(f"strict jq expression at {path} must be a string")
    expression = _optional_expression(value, path)
    if expression is None:
        raise OwsError(f"strict jq expression must use ${{...}} at {path}")
    return expression


def _compile_required_expression(value: Any, path: str, jq_variables: frozenset[str]) -> None:
    _compile_jq(_required_expression(value, path), path, jq_variables)


def _compile_jq(expression: str, path: str, jq_variables: frozenset[str]) -> None:
    args = {name: None for name in jq_variables}
    try:
        jq.compile(expression, args=args)
    except Exception as error:
        raise OwsError(f"jq compile failed at {path}: {error}") from error


def _normalize_jq_outputs(outputs: Sequence[Any]) -> Any:
    if len(outputs) == 0:
        return None
    if len(outputs) == 1:
        return outputs[0]
    return list(outputs)


def evaluate_jq(value: str, data: Any, variables: Mapping[str, Any] | None = None) -> Any:
    expression = _required_expression(value, "runtime")
    args = dict(variables or {})
    try:
        outputs = jq.compile(expression, args=args).input_value(data).all()
    except Exception as error:
        raise OwsError(f"jq evaluation failed for {value!r}: {error}") from error
    return _normalize_jq_outputs(outputs)


def _evaluate_set_value(value: Any, data: Any, variables: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        expression = _optional_expression(value, "set")
        if expression is None:
            return value
        return evaluate_jq(value, data, variables)
    if isinstance(value, dict):
        return {key: _evaluate_set_value(child, data, variables) for key, child in value.items()}
    if isinstance(value, list):
        return [_evaluate_set_value(child, data, variables) for child in value]
    return value


def compute_task_action(
    operation: str,
    task_spec: Mapping[str, Any],
    input_data: Any,
    variables: Mapping[str, Any],
) -> Any:
    """Compute one pure supported task action for direct or DBOS execution."""
    if operation == "set":
        return _evaluate_set_value(task_spec["set"], input_data, variables)
    if operation == "do":
        return input_data
    if operation == "for":
        value = evaluate_jq(task_spec["for"]["in"], input_data, variables)
        if not isinstance(value, list):
            raise OwsError("for.in must produce an array")
        return value
    if operation == "switch":
        default: str | None = None
        for case in task_spec["switch"]:
            _, case_spec = next(iter(case.items()))
            if "when" not in case_spec:
                default = case_spec["then"]
                continue
            condition = evaluate_jq(case_spec["when"], input_data, variables)
            if not isinstance(condition, bool):
                raise OwsError("switch condition must produce a boolean")
            if condition:
                return {"__ows_control__": case_spec["then"]}
        return {"__ows_control__": default or "continue"}
    raise OwsError(f"unsupported task '{operation}'")


class Interpreter:
    def __init__(
        self,
        definition: dict[str, Any],
        *,
        max_tasks: int = 10_000,
        step_runner: StepRunner | None = None,
    ) -> None:
        validate_workflow(definition)
        if max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        self.definition = definition
        self.max_tasks = max_tasks
        self.step_runner = step_runner
        self._task_count = 0

    def run(self, input_data: Any) -> Any:
        self._task_count = 0
        result = self._run_scope(self.definition["do"], input_data, {}, "do")
        return result.value

    def _run_scope(
        self,
        tasks: list[dict[str, Any]],
        input_data: Any,
        variables: dict[str, Any],
        scope_path: str,
    ) -> _ScopeResult:
        names = [next(iter(task)) for task in tasks]
        name_to_index = {name: index for index, name in enumerate(names)}
        current = input_data
        index = 0
        while index < len(tasks):
            self._task_count += 1
            if self._task_count > self.max_tasks:
                raise OwsError(f"maximum task execution count {self.max_tasks} exceeded")
            task_name, task_spec = next(iter(tasks[index].items()))
            operation, _ = _task_action(task_name, task_spec)
            task_path = f"{scope_path}[{index}].{task_name}"
            action_result = self._execute_step(
                task_path, task_name, operation, task_spec, current, variables
            )

            if operation == "do":
                nested = self._run_scope(
                    task_spec["do"], current, dict(variables), f"{task_path}.do"
                )
                current = nested.value
            elif operation == "for":
                for_spec = task_spec["for"]
                each_name = for_spec.get("each", "item")
                at_name = for_spec.get("at", "index")
                loop_outputs: list[Any] = []
                for loop_index, item in enumerate(action_result):
                    loop_variables = dict(variables)
                    loop_variables[each_name] = item
                    loop_variables[at_name] = loop_index
                    nested = self._run_scope(
                        task_spec["do"],
                        current,
                        loop_variables,
                        f"{task_path}.iteration[{loop_index}].do",
                    )
                    loop_outputs.append(nested.value)
                current = loop_outputs
            elif operation == "switch":
                directive = action_result["__ows_control__"]
                if directive == "exit":
                    return _ScopeResult(current, exited=True)
                if directive == "continue":
                    index += 1
                else:
                    index = name_to_index[directive]
                continue
            else:
                current = action_result

            directive = task_spec.get("then", "continue")
            if directive == "exit":
                return _ScopeResult(current, exited=True)
            if directive == "continue":
                index += 1
            else:
                index = name_to_index[directive]
        return _ScopeResult(current)

    def _execute_step(
        self,
        identity: str,
        task_name: str,
        operation: str,
        task_spec: dict[str, Any],
        input_data: Any,
        variables: dict[str, Any],
    ) -> Any:
        if self.step_runner is not None:
            return self.step_runner(
                identity,
                task_name,
                operation,
                task_spec,
                input_data,
                variables,
            )
        return compute_task_action(operation, task_spec, input_data, variables)
