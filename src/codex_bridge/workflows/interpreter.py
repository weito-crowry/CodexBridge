from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import jq
import yaml
from jsonschema import Draft202012Validator

from .job_adapter import JobRequestError, validate_mock_job_request

SCHEMA_PATH: Traversable = files("codex_bridge.workflows").joinpath("schema", "workflow.yaml")
SUPPORTED_TASKS = frozenset({"do", "set", "switch", "for", "call"})
SUPPORTED_DIRECTIVES = frozenset({"continue", "exit"})
_JQ_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Retained as the Phase 5E-0 fixture and historical evidence, not as a live allowlist.
SAFE_CODEX_JOB_PROMPT = (
    "2+2を計算し、外部ツール・コマンド・ファイル操作・Git操作・ネットワーク操作を行わず、"
    "回答本文に4の1文字だけを返すこと"
)


class OwsError(ValueError):
    """Raised when a workflow is invalid or outside this PoC's subset."""


def validate_codex_job_prompt(prompt: object) -> str:
    """Validate and preserve one literal Codex job prompt."""
    if not isinstance(prompt, str):
        raise OwsError("codex-job prompt must be a literal string")
    if not prompt or prompt.isspace():
        raise OwsError("codex-job prompt must not be empty or whitespace-only")
    try:
        encoded = prompt.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise OwsError("codex-job prompt must be valid UTF-8") from None
    if len(encoded) > 16 * 1024:
        raise OwsError("codex-job prompt exceeds the 16 KiB UTF-8 limit")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\t\n\r"
        for character in prompt
    ):
        raise OwsError("codex-job prompt contains a disallowed control character")
    return prompt


@dataclass(frozen=True)
class _ScopeResult:
    value: Any
    exited: bool = False


@dataclass
class _WorkflowContext:
    value: Any


StepRunner = Callable[[str, str, str, dict[str, Any], Any, dict[str, Any]], Any]


def parse_workflow_text(text: str) -> dict[str, Any]:
    try:
        definition = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise OwsError(f"YAML parse failed: {error}") from error
    if not isinstance(definition, dict):
        raise OwsError("workflow YAML root must be an object")
    return definition


def load_workflow(
    path: Path,
    schema_path: Path | Traversable | None = None,
) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise OwsError(f"cannot read workflow file {path}: {error}") from error
    definition = parse_workflow_text(text)
    validate_workflow(definition, schema_path=schema_path)
    return definition


def validate_official_schema(
    definition: Any,
    schema_path: Path | Traversable | None = None,
) -> None:
    try:
        schema_resource = SCHEMA_PATH if schema_path is None else schema_path
        schema = yaml.safe_load(schema_resource.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(definition),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except (OSError, yaml.YAMLError, ValueError) as load_error:
        raise OwsError(f"could not load official OWS v1.0.3 schema: {load_error}") from load_error
    if errors:
        validation_error = errors[0]
        location = "/" + "/".join(str(part) for part in validation_error.absolute_path)
        raise OwsError(
            f"official OWS v1.0.3 schema violation at {location}: {validation_error.message}"
        )


def validate_workflow(
    definition: Any,
    schema_path: Path | Traversable | None = None,
    *,
    codex_job_live: bool = False,
    max_live_codex_jobs: int = 1,
) -> None:
    _validate_subset(
        definition,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )
    validate_official_schema(definition, schema_path=schema_path)


def _validate_dataflow_config(
    config: Any,
    *,
    config_name: str,
    expression_key: str,
    path: str,
    jq_variables: frozenset[str],
) -> None:
    if not isinstance(config, dict):
        raise OwsError(f"unsupported {config_name} configuration at {path}; expected an object")
    unsupported = set(config) - {expression_key}
    if unsupported:
        key = sorted(unsupported)[0]
        raise OwsError(f"unsupported {config_name} property '{key}' at {path}")
    if expression_key not in config:
        return
    expression = config[expression_key]
    if not isinstance(expression, str):
        raise OwsError(
            f"unsupported {config_name}.{expression_key} type at {path}; "
            "only strict jq expression strings are supported"
        )
    _compile_required_expression(expression, f"{path}.{expression_key}", jq_variables)


def _validate_max_live_codex_jobs(value: object) -> int:
    if type(value) is not int or value not in {1, 2, 3}:
        raise OwsError("max_live_codex_jobs must be the integer 1, 2, or 3")
    return value


_THREE_JOB_INPUT_FROM = "${ {issues: (.issues | sort_by(.number))} }"
_THREE_JOB_PROMPT_PREFIX = "Reply with only the synthetic issue number: "
_THREE_JOB_PROMPT_FROM = "${ .number | tostring }"
_THREE_JOB_OUTPUT_AS = (
    "${ {number: $input.number, status: .status, job_id: .job_id, "
    "request_key: .request_key, final_text: .result.final_text} }"
)


def validate_three_live_codex_job_input(input_data: Any) -> tuple[dict[str, Any], ...]:
    """Validate and sort the bounded synthetic input used by the three-job mode."""
    if not isinstance(input_data, dict) or set(input_data) != {"issues"}:
        raise OwsError("three-job input must contain only an issues array")
    issues = input_data["issues"]
    if not isinstance(issues, list) or len(issues) > 4:
        raise OwsError("three-job input issues must be an array with at most four items")

    numbers: set[int] = set()
    enabled_count = 0
    normalized: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict) or set(issue) != {"number", "enabled"}:
            raise OwsError("each three-job issue must contain only number and enabled")
        number = issue["number"]
        enabled = issue["enabled"]
        if type(number) is not int or number <= 0 or number in numbers:
            raise OwsError("three-job issue numbers must be unique positive integers")
        if type(enabled) is not bool:
            raise OwsError("three-job issue enabled values must be booleans")
        numbers.add(number)
        enabled_count += int(enabled)
        normalized.append({"number": number, "enabled": enabled})
    if enabled_count > 3:
        raise OwsError("three-job input may enable at most three issues")
    return tuple(sorted(normalized, key=lambda issue: issue["number"]))


def _validate_three_live_codex_job_shape(
    definition: dict[str, Any], tasks: list[Any]
) -> tuple[str, str]:
    if set(definition) != {"document", "evaluate", "input", "do"}:
        raise OwsError("three-job live mode requires only document, evaluate, input, and do")
    if definition.get("input") != {"from": _THREE_JOB_INPUT_FROM}:
        raise OwsError("three-job live mode requires the fixed ascending issue-number input sort")
    if len(tasks) != 1 or not isinstance(tasks[0], dict) or len(tasks[0]) != 1:
        raise OwsError("three-job live mode requires one top-level for task")
    for_name, for_task = next(iter(tasks[0].items()))
    if not isinstance(for_name, str) or not for_name or not isinstance(for_task, dict):
        raise OwsError("three-job live mode requires a named top-level for task")
    if set(for_task) != {"for", "do"}:
        raise OwsError("three-job live for task may contain only for and do")
    if for_task.get("for") != {"in": "${ .issues }", "each": "issue", "at": "index"}:
        raise OwsError("three-job live mode requires the fixed issue loop")
    body = for_task.get("do")
    if not isinstance(body, list) or len(body) != 3:
        raise OwsError("three-job live loop requires switch, codex-job, and skip tasks")
    if any(not isinstance(item, dict) or len(item) != 1 for item in body):
        raise OwsError("three-job live loop requires three named tasks")
    body_names = [next(iter(item)) for item in body]
    if (
        any(not isinstance(name, str) or not name for name in body_names)
        or len(set(body_names)) != 3
    ):
        raise OwsError("three-job live loop task names must be distinct non-empty strings")
    switch_name, dispatch_name, skip_name = body_names
    switch_task = body[0][switch_name]
    dispatch_task = body[1][dispatch_name]
    skip_task = body[2][skip_name]
    if not isinstance(switch_task, dict) or set(switch_task) != {"switch"}:
        raise OwsError("three-job live loop must start with one switch task")
    if switch_task["switch"] != [
        {"enabled": {"when": "${ $issue.enabled == true }", "then": dispatch_name}},
        {"disabled": {"then": skip_name}},
    ]:
        raise OwsError("three-job live switch must dispatch enabled issues and skip the rest")
    if not isinstance(dispatch_task, dict) or set(dispatch_task) != {
        "input",
        "call",
        "with",
        "output",
        "then",
    }:
        raise OwsError("three-job live dispatch task has an unsupported shape")
    if (
        dispatch_task.get("input") != {"from": "${ {number: $issue.number} }"}
        or dispatch_task.get("call") != "codex-job"
        or dispatch_task.get("with")
        != {"prompt": _THREE_JOB_PROMPT_PREFIX, "prompt_from": _THREE_JOB_PROMPT_FROM}
        or dispatch_task.get("output") != {"as": _THREE_JOB_OUTPUT_AS}
        or dispatch_task.get("then") != "exit"
    ):
        raise OwsError("three-job live dispatch must use its fixed prompt, output, and exit flow")
    if not isinstance(skip_task, dict) or set(skip_task) != {"set"}:
        raise OwsError("three-job live loop must end with one skip set task")
    if skip_task["set"] != {
        "number": "${ $issue.number }",
        "status": "skipped",
        "job_id": None,
        "request_key": None,
        "final_text": None,
    }:
        raise OwsError("three-job live skip output has an unsupported shape")
    return for_name, dispatch_name


def _validate_two_live_codex_job_shape(definition: dict[str, Any], tasks: list[Any]) -> None:
    if "input" in definition or "output" in definition:
        raise OwsError("two-job live mode does not support top-level input or output")
    if len(tasks) != 2:
        raise OwsError("two-job live mode requires exactly two top-level codex-job calls")
    task_names: set[str] = set()
    for index, item in enumerate(tasks):
        expected_arguments = {"prompt"} if index == 0 else {"prompt", "prompt_from"}
        if not isinstance(item, dict) or len(item) != 1:
            raise OwsError("two-job live mode requires two named top-level codex-job calls")
        task_name, task_spec = next(iter(item.items()))
        if not isinstance(task_name, str) or not task_name or task_name in task_names:
            raise OwsError("two-job live mode requires distinct non-empty task names")
        task_names.add(task_name)
        if not isinstance(task_spec, dict) or set(task_spec) != {"call", "with"}:
            raise OwsError("two-job live tasks may contain only call and with")
        if task_spec.get("call") != "codex-job":
            raise OwsError("two-job live mode permits only codex-job calls")
        arguments = task_spec.get("with")
        if not isinstance(arguments, dict) or set(arguments) != expected_arguments:
            raise OwsError("two-job live mode has an invalid codex-job argument shape")
        if index == 0:
            validate_codex_job_prompt(arguments["prompt"])
        elif not isinstance(arguments["prompt"], str):
            raise OwsError("second codex-job prompt must be a literal string prefix")


def _validate_subset(
    definition: Any,
    *,
    codex_job_live: bool = False,
    max_live_codex_jobs: int = 1,
) -> None:
    max_live_codex_jobs = _validate_max_live_codex_jobs(max_live_codex_jobs)
    if max_live_codex_jobs in {2, 3} and not codex_job_live:
        raise OwsError(f"max_live_codex_jobs={max_live_codex_jobs} requires codex-job live mode")
    if not isinstance(definition, dict):
        raise OwsError("workflow definition must be an object")

    for key in definition:
        if key not in {"document", "evaluate", "do", "input", "output"}:
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

    if "input" in definition:
        _validate_dataflow_config(
            definition["input"],
            config_name="input",
            expression_key="from",
            path="input",
            jq_variables=frozenset(),
        )
    if "output" in definition:
        _validate_dataflow_config(
            definition["output"],
            config_name="output",
            expression_key="as",
            path="output",
            jq_variables=frozenset({"context"}),
        )

    tasks = definition.get("do")
    if not isinstance(tasks, list) or not tasks:
        raise OwsError("top-level do must be a non-empty task list")
    codex_calls, mock_calls, looped_codex_calls = _find_job_calls(tasks)
    if codex_calls and not codex_job_live:
        raise OwsError("codex-job requires --codex-job-live")
    if codex_job_live:
        if max_live_codex_jobs == 1 and len(codex_calls) != 1:
            raise OwsError("codex-job live mode requires exactly one codex-job call")
        if max_live_codex_jobs == 2:
            _validate_two_live_codex_job_shape(definition, tasks)
            if len(codex_calls) != 2:
                raise OwsError("two-job live mode requires exactly two codex-job calls")
        if max_live_codex_jobs == 3:
            _validate_three_live_codex_job_shape(definition, tasks)
            if len(codex_calls) != 1 or not looped_codex_calls:
                raise OwsError(
                    "three-job live mode requires one codex-job call inside its fixed loop"
                )
        if mock_calls:
            raise OwsError("codex-job live mode cannot be mixed with mock-job")
        if looped_codex_calls and max_live_codex_jobs != 3:
            raise OwsError("codex-job may not be nested under for")
    _validate_task_list(
        tasks,
        "do",
        frozenset(),
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )


def _find_job_calls(tasks: list[dict[str, Any]]) -> tuple[list[str], int, bool]:
    codex_calls: list[str] = []
    mock_calls = 0
    looped_codex_calls = False

    def visit(task_list: list[dict[str, Any]], *, inside_for: bool = False) -> None:
        nonlocal mock_calls, looped_codex_calls
        for task_item in task_list:
            if not isinstance(task_item, dict) or len(task_item) != 1:
                continue
            task_name, task_spec = next(iter(task_item.items()))
            if not isinstance(task_spec, dict):
                continue
            if task_spec.get("call") == "codex-job":
                codex_calls.append(str(task_name))
                looped_codex_calls = looped_codex_calls or inside_for
            elif task_spec.get("call") == "mock-job":
                mock_calls += 1
            nested = task_spec.get("do")
            if isinstance(nested, list):
                visit(nested, inside_for=inside_for or "for" in task_spec)

    visit(tasks)
    return codex_calls, mock_calls, looped_codex_calls


def _validate_task_list(
    tasks: Any,
    scope_path: str,
    jq_variables: frozenset[str],
    *,
    codex_job_live: bool = False,
    max_live_codex_jobs: int = 1,
) -> None:
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

        rejected_attrs = {"if"}.intersection(task_spec)
        if rejected_attrs:
            attr = sorted(rejected_attrs)[0]
            raise OwsError(f"unsupported task attribute '{attr}' on '{task_name}'")

        operation, action = _task_action(task_name, task_spec)
        if operation not in SUPPORTED_TASKS:
            raise OwsError(f"unsupported task '{operation}' at {scope_path}[{index}]")
        unsupported = set(task_spec) - {"then", operation, "input", "output", "export"}
        if operation == "for":
            unsupported = set(task_spec) - {
                "then",
                "for",
                "do",
                "input",
                "output",
                "export",
            }
        elif operation == "call":
            unsupported = set(task_spec) - {
                "then",
                "call",
                "with",
                "input",
                "output",
                "export",
            }
        if unsupported:
            key = sorted(unsupported)[0]
            raise OwsError(f"unsupported task key '{key}' on '{task_name}'")

        task_path = f"{scope_path}[{index}].{task_name}"
        if "input" in task_spec:
            _validate_dataflow_config(
                task_spec["input"],
                config_name="input",
                expression_key="from",
                path=f"{task_path}.input",
                jq_variables=jq_variables.union({"context"}),
            )
        if "output" in task_spec:
            _validate_dataflow_config(
                task_spec["output"],
                config_name="output",
                expression_key="as",
                path=f"{task_path}.output",
                jq_variables=jq_variables.union({"context", "input"}),
            )
        if "export" in task_spec:
            _validate_dataflow_config(
                task_spec["export"],
                config_name="export",
                expression_key="as",
                path=f"{task_path}.export",
                jq_variables=jq_variables.union({"context", "input", "output"}),
            )
        _validate_action(
            operation,
            action,
            task_name,
            task_path,
            jq_variables,
            codex_job_live=codex_job_live,
            max_live_codex_jobs=max_live_codex_jobs,
        )
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
    action_keys = set(task_spec) - {"then", "input", "output", "export"}
    if "call" in action_keys:
        operation = "call"
        action = {"call": task_spec["call"], "with": task_spec.get("with")}
    elif "for" in action_keys:
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
    *,
    codex_job_live: bool = False,
    max_live_codex_jobs: int = 1,
) -> None:
    if operation == "call":
        target = action["call"]
        if target == "codex-job":
            if not codex_job_live:
                raise OwsError("codex-job requires --codex-job-live")
            arguments = action.get("with")
            if not isinstance(arguments, dict):
                raise OwsError("codex-job with must be an argument map")
            allowed_arguments = {"prompt"}
            if max_live_codex_jobs in {2, 3}:
                allowed_arguments.add("prompt_from")
            unsupported = set(arguments) - allowed_arguments
            if unsupported:
                key = sorted(unsupported)[0]
                raise OwsError(f"unsupported codex-job argument '{key}'")
            if set(arguments) not in ({"prompt"}, {"prompt", "prompt_from"}):
                raise OwsError("codex-job requires argument 'prompt'")
            if "prompt_from" not in arguments:
                validate_codex_job_prompt(arguments["prompt"])
            else:
                if max_live_codex_jobs not in {2, 3}:
                    raise OwsError("codex-job prompt_from requires max_live_codex_jobs=2 or 3")
                if not isinstance(arguments["prompt"], str):
                    raise OwsError("second codex-job prompt must be a literal string prefix")
                _compile_required_expression(
                    arguments["prompt_from"],
                    f"{task_path}.with.prompt_from",
                    jq_variables,
                )
            return
        if target != "mock-job":
            raise OwsError(f"unsupported call target {target!r}; only 'mock-job' is allowed")
        arguments = action.get("with")
        if not isinstance(arguments, dict):
            raise OwsError("mock-job with must be an argument map")
        allowed = {"operation", "payload", "duration_ms"}
        unsupported = set(arguments) - allowed
        if unsupported:
            key = sorted(unsupported)[0]
            raise OwsError(f"unsupported mock-job argument '{key}'")
        if set(arguments) != allowed:
            missing = sorted(allowed - set(arguments))[0]
            raise OwsError(f"mock-job requires argument '{missing}'")
        if arguments["operation"] != "process":
            raise OwsError("mock-job operation must be 'process'")
        duration = arguments["duration_ms"]
        if isinstance(duration, str):
            expression = _optional_expression(duration, f"{task_path}.with.duration_ms")
            if expression is None:
                raise OwsError("mock-job duration_ms must be an integer expression or literal")
            _compile_jq(
                expression,
                f"{task_path}.with.duration_ms",
                jq_variables.union({"context", "input"}),
            )
        elif type(duration) is not int or not 0 <= duration <= 2000:
            raise OwsError("mock-job duration_ms must be an integer from 0 to 2000")
        _validate_set_value(
            arguments["payload"],
            f"{task_path}.with.payload",
            jq_variables.union({"context", "input"}),
        )
        return
    if operation == "set":
        value = action["set"]
        if not isinstance(value, (dict, str)):
            raise OwsError(f"set on '{task_name}' must be a map or string")
        _validate_set_value(value, task_path, jq_variables.union({"context", "input"}))
        return
    if operation == "do":
        _validate_task_list(
            action["do"],
            f"{task_path}.do",
            jq_variables,
            codex_job_live=codex_job_live,
            max_live_codex_jobs=max_live_codex_jobs,
        )
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
        _compile_required_expression(
            for_spec["in"],
            f"{task_path}.for.in",
            jq_variables.union({"context", "input"}),
        )
        each_name = for_spec.get("each", "item")
        at_name = for_spec.get("at", "index")
        if not isinstance(each_name, str) or not _JQ_NAME.fullmatch(each_name):
            raise OwsError(f"for.each on '{task_name}' must be a jq variable name")
        if not isinstance(at_name, str) or not _JQ_NAME.fullmatch(at_name):
            raise OwsError(f"for.at on '{task_name}' must be a jq variable name")
        if each_name == at_name:
            raise OwsError(f"for.each and for.at must use different names on '{task_name}'")
        if {each_name, at_name}.intersection({"context", "input", "output"}):
            raise OwsError(
                f"for.each and for.at on '{task_name}' cannot shadow OWS dataflow variables"
            )
        _validate_task_list(
            body,
            f"{task_path}.do",
            jq_variables.union({each_name, at_name}),
            codex_job_live=codex_job_live,
            max_live_codex_jobs=max_live_codex_jobs,
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
                    jq_variables.union({"context", "input"}),
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
    if operation == "call":
        if task_spec.get("call") == "codex-job":
            raise OwsError("call requires the codex-job live adapter")
        raise OwsError("call requires the mock-job DBOS adapter")
    raise OwsError(f"unsupported task '{operation}'")


def compute_mock_job_request(
    task_spec: Mapping[str, Any],
    input_data: Any,
    variables: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        evaluated = _evaluate_set_value(task_spec["with"], input_data, variables)
        return validate_mock_job_request(evaluated)
    except JobRequestError as error:
        raise OwsError(str(error)) from error


def compute_codex_job_request(
    task_spec: Mapping[str, Any],
    input_data: Any = None,
    variables: Mapping[str, Any] | None = None,
    *,
    max_live_codex_jobs: int = 1,
) -> dict[str, str]:
    max_live_codex_jobs = _validate_max_live_codex_jobs(max_live_codex_jobs)
    arguments = task_spec.get("with")
    if not isinstance(arguments, Mapping):
        raise OwsError("codex-job with must be an argument map")
    if set(arguments) == {"prompt"}:
        return {"prompt": validate_codex_job_prompt(arguments["prompt"])}
    if max_live_codex_jobs not in {2, 3} or set(arguments) != {"prompt", "prompt_from"}:
        raise OwsError("codex-job prompt_from requires the two-job or three-job live subset")
    prefix = arguments["prompt"]
    if not isinstance(prefix, str):
        raise OwsError("second codex-job prompt must be a literal string prefix")
    try:
        evaluated = evaluate_jq(arguments["prompt_from"], input_data, variables)
    except Exception:
        raise OwsError("codex-job prompt_from evaluation failed") from None
    if not isinstance(evaluated, str) or not evaluated:
        raise OwsError("codex-job prompt_from must produce a non-empty string")
    return {"prompt": validate_codex_job_prompt(prefix + evaluated)}


class Interpreter:
    def __init__(
        self,
        definition: dict[str, Any],
        *,
        max_tasks: int = 10_000,
        step_runner: StepRunner | None = None,
        codex_job_live: bool = False,
        max_live_codex_jobs: int = 1,
    ) -> None:
        validate_workflow(
            definition,
            codex_job_live=codex_job_live,
            max_live_codex_jobs=max_live_codex_jobs,
        )
        if max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        self.definition = definition
        self.max_tasks = max_tasks
        self.step_runner = step_runner
        self.max_live_codex_jobs = max_live_codex_jobs
        self._task_count = 0

    def run(self, input_data: Any) -> Any:
        self._task_count = 0
        if self.max_live_codex_jobs == 3:
            validate_three_live_codex_job_input(input_data)
        workflow_input = input_data
        workflow_input_config = self.definition.get("input", {})
        if "from" in workflow_input_config:
            workflow_input = evaluate_jq(workflow_input_config["from"], input_data)
        context = _WorkflowContext(workflow_input)
        result = self._run_scope(self.definition["do"], workflow_input, {}, context, "do").value
        workflow_output_config = self.definition.get("output", {})
        if "as" in workflow_output_config:
            return evaluate_jq(workflow_output_config["as"], result, {"context": context.value})
        return result

    def _run_scope(
        self,
        tasks: list[dict[str, Any]],
        input_data: Any,
        variables: dict[str, Any],
        context: _WorkflowContext,
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
            task_input = current
            task_input_variables = dict(variables)
            task_input_variables["context"] = context.value
            input_config = task_spec.get("input", {})
            if "from" in input_config:
                task_input = evaluate_jq(input_config["from"], current, task_input_variables)
            task_variables = dict(variables)
            task_variables["context"] = context.value
            task_variables["input"] = task_input
            action_result = self._execute_step(
                task_path, task_name, operation, task_spec, task_input, task_variables
            )

            if operation == "do":
                nested = self._run_scope(
                    task_spec["do"],
                    task_input,
                    dict(variables),
                    context,
                    f"{task_path}.do",
                )
                raw_task_output = nested.value
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
                        task_input,
                        loop_variables,
                        context,
                        f"{task_path}.iteration[{loop_index}].do",
                    )
                    loop_outputs.append(nested.value)
                raw_task_output = loop_outputs
            elif operation == "switch":
                raw_task_output = task_input
            else:
                raw_task_output = action_result

            output_config = task_spec.get("output", {})
            if "as" in output_config:
                raw_output_variables = dict(variables)
                raw_output_variables.update({"context": context.value, "input": task_input})
                transformed_task_output = evaluate_jq(
                    output_config["as"], raw_task_output, raw_output_variables
                )
            else:
                transformed_task_output = raw_task_output

            export_config = task_spec.get("export", {})
            if "as" in export_config:
                export_variables = dict(variables)
                export_variables.update(
                    {
                        "context": context.value,
                        "input": task_input,
                        "output": transformed_task_output,
                    }
                )
                context.value = evaluate_jq(
                    export_config["as"], transformed_task_output, export_variables
                )

            current = transformed_task_output

            if operation == "switch":
                directive = action_result["__ows_control__"]
                if directive == "exit":
                    return _ScopeResult(current, exited=True)
                if directive == "continue":
                    index += 1
                else:
                    index = name_to_index[directive]
                continue

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
