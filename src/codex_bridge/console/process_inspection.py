from __future__ import annotations

import math
import ntpath
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event
from typing import Any, cast

# Event delivery can lag process creation by a minute; a short after-window covers startup skew.
_ROOT_CREATED_BEFORE_COMMAND_SECONDS = 60.0
_ROOT_CREATED_AFTER_COMMAND_SECONDS = 10.0
_MAX_TRACKED_CHILDREN = 100
_MAX_SCANNED_PROCESSES = 5_000
_MAX_ROOT_CANDIDATES = 100
# Windows descendant trees can exceed one second while remaining bounded in the worker.
_MAX_SCAN_SECONDS = 4.0
_CODEX_POWERSHELL_UTF8_SETUP = (
    "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}"
)
_COMMAND_LINE_LIMIT = 8_000
_COMMAND_ARGUMENT_LIMIT = 256
_COMMAND_ARGUMENTS_TOTAL_LIMIT = 16_000


@dataclass(frozen=True, order=True)
class CommandKey:
    thread_id: str
    turn_id: str
    item_id: str


@dataclass(frozen=True)
class CommandDescriptor:
    key: CommandKey
    summary: str
    app_server_process_id: str
    started_at_seconds: float | None
    completed_at_seconds: float | None
    command_status: str
    exit_code: int | None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float


@dataclass(frozen=True)
class ProcessEvidence:
    app_server_os_pid: int | None = None
    app_server_identity: ProcessIdentity | None = None
    roots: tuple[tuple[CommandKey, ProcessIdentity], ...] = ()
    children: tuple[tuple[CommandKey, tuple[ProcessIdentity, ...]], ...] = ()


@dataclass(frozen=True)
class ProcessView:
    pid: int
    create_time: float
    name: str | None
    command_line: str | None
    ppid: int | None
    status: str | None
    state: str
    executable: str | None = None
    arguments: tuple[str, ...] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ProcessScan:
    processes: dict[int, tuple[ProcessIdentity, int, ProcessView]]
    complete: bool
    reason: str | None = None


@dataclass(frozen=True)
class CommandProcessState:
    command: CommandDescriptor
    root_status: str
    root: ProcessView | None
    children: tuple[ProcessView, ...]


@dataclass(frozen=True)
class InspectionResult:
    commands: tuple[CommandProcessState, ...]
    evidence: ProcessEvidence
    supported: bool = True
    message: str | None = None


def inspect_command_processes(
    commands: Sequence[CommandDescriptor],
    evidence: ProcessEvidence | None = None,
    *,
    psutil_module: object | None = None,
    platform_name: str | None = None,
    observed_at: float | None = None,
    cancelled: Event | None = None,
) -> InspectionResult:
    """Inspect only process IDs supplied by selected-thread command events."""
    if evidence is None:
        evidence = ProcessEvidence()
    command_list = tuple(commands[-50:])
    if (platform_name or sys.platform) != "win32":
        return InspectionResult(
            _empty_states(command_list, "Windows only"), evidence, False, "Windows only"
        )
    if _is_cancelled(cancelled):
        return InspectionResult(_empty_states(command_list, "Unknown"), evidence)
    anchor_pid = _positive_integer(evidence.app_server_os_pid)
    if anchor_pid is None:
        return InspectionResult(
            _empty_states(command_list, "Unverified: App Server anchor unavailable"),
            ProcessEvidence(),
        )
    if psutil_module is None:
        try:
            import psutil  # type: ignore[import-untyped]

            psutil_module = psutil
        except ImportError:
            return InspectionResult(
                _empty_states(command_list, "Process inspection unavailable"),
                evidence,
                message="Process inspection unavailable",
            )
    runtime_psutil: Any = psutil_module

    now = time.time() if observed_at is None else observed_at
    errors = _psutil_errors(psutil_module)
    try:
        anchor_process = runtime_psutil.Process(anchor_pid)
        anchor_create_time = _number(anchor_process.create_time())
    except errors.access_denied:
        return InspectionResult(
            _empty_states(command_list, "Unverified: App Server anchor access denied"),
            _evidence_for_anchor(evidence, anchor_pid),
        )
    except errors.all:
        anchor_create_time = None
    if anchor_create_time is None:
        return InspectionResult(
            _empty_states(command_list, "Unverified: App Server anchor unavailable"),
            _evidence_for_anchor(evidence, anchor_pid),
        )

    anchor_identity = ProcessIdentity(anchor_pid, anchor_create_time)
    prior_anchor = (
        evidence.app_server_identity if evidence.app_server_os_pid == anchor_pid else None
    )
    if prior_anchor is not None and not _same_identity(prior_anchor, anchor_identity):
        return InspectionResult(
            _empty_states(command_list, "Unverified: App Server anchor PID reused"),
            ProcessEvidence(
                app_server_os_pid=anchor_pid,
                app_server_identity=anchor_identity,
            ),
        )

    command_keys = {command.key for command in command_list}
    old_roots = (
        {key: identity for key, identity in evidence.roots if key in command_keys}
        if prior_anchor is not None
        else {}
    )
    old_children = (
        {key: list(items) for key, items in evidence.children if key in command_keys}
        if prior_anchor is not None
        else {}
    )
    roots = dict(old_roots)
    children = {key: list(items) for key, items in old_children.items()}
    states: dict[CommandKey, CommandProcessState] = {}
    root_views: dict[CommandKey, ProcessView | None] = {}
    root_active: dict[CommandKey, ProcessIdentity] = {}
    child_views: dict[CommandKey, dict[ProcessIdentity, ProcessView]] = {
        command.key: {} for command in command_list
    }
    child_active: dict[CommandKey, set[ProcessIdentity]] = {
        command.key: set() for command in command_list
    }

    for command in command_list:
        prior = old_roots.get(command.key)
        if prior is None:
            continue
        state, view, active = _read_identity(runtime_psutil, prior, errors)
        # Keep the original identity as evidence after PID reuse. Dropping it
        # could let a replacement process match this command on the next poll.
        root_views[command.key] = view
        if active:
            root_active[command.key] = prior
        states[command.key] = CommandProcessState(command, state, view, ())

    scan = _scan_processes(anchor_process, errors, cancelled)
    descendants = _descendants(anchor_identity, scan.processes, now) if scan.complete else {}
    anchor_revalidation_denied = False
    try:
        after_scan_time = _number(runtime_psutil.Process(anchor_pid).create_time())
    except errors.access_denied:
        after_scan_time = None
        anchor_revalidation_denied = True
    except errors.all:
        after_scan_time = None
    if anchor_revalidation_denied or after_scan_time is None:
        scan = ProcessScan(
            scan.processes,
            False,
            "access denied" if anchor_revalidation_denied else "anchor unavailable",
        )
        descendants = {}
    elif not _same_identity(anchor_identity, ProcessIdentity(anchor_pid, after_scan_time)):
        return InspectionResult(
            _empty_states(command_list, "Unverified: App Server anchor changed during inspection"),
            ProcessEvidence(app_server_os_pid=anchor_pid),
        )

    candidate_views: dict[ProcessIdentity, tuple[str, ProcessView | None]] = {}
    matches: dict[CommandKey, list[tuple[ProcessIdentity, ProcessView, str]]] = {}
    unverified_reasons: dict[CommandKey, str] = {}
    candidate_limit_reached: set[CommandKey] = set()
    inspected_candidates = 0
    for command in command_list:
        if _is_cancelled(cancelled) or command.key in states:
            continue
        if not scan.complete:
            unverified_reasons[command.key] = _scan_unverified_reason(scan)
            continue
        if command.started_at_seconds is None:
            unverified_reasons[command.key] = "Unverified: command start timestamp unavailable"
            continue
        command_started_at = command.started_at_seconds
        if command_started_at < anchor_identity.create_time - 1.0:
            unverified_reasons[command.key] = (
                "Unverified: App Server anchor started after this command"
            )
            continue
        eligible_count = 0
        inaccessible = False
        unavailable_reason: str | None = None
        time_mismatch = False
        for identity in sorted(
            descendants,
            key=lambda candidate: abs(command_started_at - candidate.create_time),
        ):
            age = command_started_at - identity.create_time
            if (
                age > _ROOT_CREATED_BEFORE_COMMAND_SECONDS
                or age < -_ROOT_CREATED_AFTER_COMMAND_SECONDS
            ):
                time_mismatch = True
                continue
            scanned_process = scan.processes.get(identity.pid)
            scanned_name = scanned_process[2].name if scanned_process is not None else None
            if not _may_be_command_shell(command.summary, scanned_name):
                continue
            eligible_count += 1
            cached = candidate_views.get(identity)
            if cached is None:
                if inspected_candidates >= _MAX_ROOT_CANDIDATES:
                    candidate_limit_reached.add(command.key)
                    continue
                inspected_candidates += 1
                candidate_views[identity] = _observe_candidate(runtime_psutil, identity, errors)
                candidate_state, candidate_view = candidate_views[identity]
            else:
                candidate_state, candidate_view = cached
            if candidate_state == "Access denied":
                inaccessible = True
                continue
            if candidate_view is None:
                if candidate_state.startswith("Unverified:"):
                    unavailable_reason = candidate_state
                continue
            if _command_matches(command.summary, candidate_view):
                matches.setdefault(command.key, []).append(
                    (identity, candidate_view, candidate_state)
                )
        if eligible_count == 0 and time_mismatch:
            unverified_reasons[command.key] = (
                "Unverified: no Codex child within command time window"
            )
        elif inaccessible:
            unverified_reasons[command.key] = (
                "Unverified: Codex child command metadata access denied"
            )
        elif unavailable_reason is not None:
            unverified_reasons[command.key] = unavailable_reason
        elif not matches.get(command.key):
            unverified_reasons[command.key] = "Unverified: no matching Codex child"

    candidate_owners: dict[ProcessIdentity, list[CommandKey]] = {}
    for key, command_matches in matches.items():
        for identity, _view, _state in command_matches:
            candidate_owners.setdefault(identity, []).append(key)

    for command in command_list:
        if command.key in states:
            continue
        command_matches = matches.get(command.key, [])
        root_view: ProcessView | None = None
        if command.key in candidate_limit_reached:
            status = "Unverified: Codex child candidate limit reached"
        elif len(command_matches) > 1:
            status = "Unverified: ambiguous matching Codex children"
        elif command_matches and len(candidate_owners[command_matches[0][0]]) > 1:
            status = "Unverified: Codex child matches multiple commands"
        elif command_matches:
            identity, root_view, status = command_matches[0]
            roots[command.key] = identity
            root_views[command.key] = root_view
            if status == "Running":
                root_active[command.key] = identity
        else:
            status = unverified_reasons.get(command.key, "Unverified: no matching Codex child")
        states[command.key] = CommandProcessState(command, status, root_view, ())

    for command in command_list:
        if _is_cancelled(cancelled):
            break
        for identity in children.get(command.key, ()):
            state, view, active = _read_identity(psutil_module, identity, errors)
            child_views.setdefault(command.key, {})[identity] = view
            if active:
                child_active.setdefault(command.key, set()).add(identity)

    if scan.complete and (root_active or any(child_active.values())):
        command_by_key = {command.key: command for command in command_list}
        for key, identity in tuple(root_active.items()):
            state, view, active = _read_identity(runtime_psutil, identity, errors)
            root_views[key] = view
            states[key] = CommandProcessState(command_by_key[key], state, view, ())
            if not active:
                root_active.pop(key, None)
        _discover_children(
            command_list,
            scan.processes,
            runtime_psutil,
            errors,
            roots,
            root_active,
            children,
            child_views,
            child_active,
            observed_at=now,
            cancelled=cancelled,
        )

    for command in command_list:
        base = states.get(command.key)
        if base is None:
            continue
        visible_children = tuple(
            child_views.get(command.key, {}).get(identity)
            for identity in children.get(command.key, ())
        )
        state_children = tuple(view for view in visible_children if view is not None)
        states[command.key] = CommandProcessState(
            base.command, base.root_status, root_views.get(command.key), state_children
        )

    return InspectionResult(
        tuple(states[command.key] for command in command_list if command.key in states),
        ProcessEvidence(
            app_server_os_pid=anchor_pid,
            app_server_identity=anchor_identity,
            roots=tuple(roots.items()),
            children=tuple((key, tuple(items)) for key, items in children.items() if items),
        ),
    )


def _empty_states(
    commands: Sequence[CommandDescriptor], message: str
) -> tuple[CommandProcessState, ...]:
    return tuple(CommandProcessState(command, message, None, ()) for command in commands)


def _evidence_for_anchor(evidence: ProcessEvidence, anchor_pid: int) -> ProcessEvidence:
    if evidence.app_server_os_pid == anchor_pid:
        return evidence
    return ProcessEvidence(app_server_os_pid=anchor_pid)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _same_identity(first: ProcessIdentity, second: ProcessIdentity) -> bool:
    return first.pid == second.pid and abs(first.create_time - second.create_time) <= 0.01


def _psutil_errors(module: object) -> Any:
    def get(name: str) -> type[BaseException]:
        candidate = getattr(module, name, ())
        return candidate if isinstance(candidate, type) else Exception

    return type(
        "ProcessErrors",
        (),
        {
            "no_such_process": get("NoSuchProcess"),
            "access_denied": get("AccessDenied"),
            "zombie": get("ZombieProcess"),
            "all": tuple(
                dict.fromkeys((get("ZombieProcess"), get("NoSuchProcess"), get("AccessDenied")))
            ),
        },
    )


def _safe_call(process: object, name: str, errors: Any) -> object | None:
    try:
        return cast(object | None, getattr(process, name)())
    except errors.all:
        return None
    except (AttributeError, OSError, TypeError):
        return None


def _read_live_process(
    process: object, identity: ProcessIdentity, errors: Any
) -> tuple[str, ProcessView, bool]:
    runtime_process: Any = process
    name = _string_or_none(_safe_call(process, "name", errors))
    executable = _string_or_none(_safe_call(process, "exe", errors))
    arguments = _command_arguments(_safe_call(process, "cmdline", errors))
    command_line = _command_line(arguments)
    ppid = _positive_integer(_safe_call(process, "ppid", errors))
    ended = False
    inaccessible = False
    try:
        process_status = _string_or_none(runtime_process.status())
    except errors.zombie:
        process_status = "zombie"
        ended = True
    except errors.no_such_process:
        process_status = None
        ended = True
    except errors.access_denied:
        process_status = None
        inaccessible = True
    try:
        running_value = runtime_process.is_running()
    except errors.zombie:
        running_value = False
        ended = True
    except errors.no_such_process:
        running_value = False
        ended = True
    except errors.access_denied:
        running_value = None
        inaccessible = True
    if ended or running_value is False or process_status in {"zombie", "dead"}:
        state = "Exited"
    elif running_value is None and (inaccessible or process_status is None):
        state = "Access denied"
    else:
        state = "Running"
    return (
        state,
        ProcessView(
            identity.pid,
            identity.create_time,
            name,
            command_line,
            ppid,
            process_status,
            state,
            executable,
            arguments,
        ),
        state == "Running",
    )


def _read_identity(
    psutil_module: object, identity: ProcessIdentity, errors: Any
) -> tuple[str, ProcessView, bool]:
    try:
        runtime_psutil: Any = psutil_module
        process = runtime_psutil.Process(identity.pid)
        create_time = _number(process.create_time())
    except errors.zombie:
        return "Exited", _identity_view(identity, "Exited"), False
    except errors.no_such_process:
        return "Exited", _identity_view(identity, "Exited"), False
    except errors.access_denied:
        return "Access denied", _identity_view(identity, "Access denied"), False
    current = ProcessIdentity(identity.pid, create_time or 0.0)
    if create_time is None:
        return (
            "Unknown: creation time unavailable",
            _identity_view(identity, "Unknown: creation time unavailable"),
            False,
        )
    if not _same_identity(identity, current):
        return "PID reused", _identity_view(identity, "PID reused"), False
    return _read_live_process(process, identity, errors)


def _prior_view(identity: ProcessIdentity | None, state: str) -> ProcessView | None:
    if identity is None:
        return None
    return _identity_view(identity, state)


def _identity_view(identity: ProcessIdentity, state: str) -> ProcessView:
    return ProcessView(identity.pid, identity.create_time, None, None, None, None, state)


def _scan_processes(anchor_process: object, errors: Any, cancelled: Event | None) -> ProcessScan:
    result: dict[int, tuple[ProcessIdentity, int, ProcessView]] = {}
    started = time.monotonic()
    if _is_cancelled(cancelled):
        return ProcessScan(result, False, "incomplete")
    try:
        runtime_process: Any = anchor_process
        descendants = runtime_process.children(recursive=True)
        if time.monotonic() - started >= _MAX_SCAN_SECONDS:
            return ProcessScan(result, False, "incomplete")
        iterator = iter(descendants)
    except errors.access_denied:
        return ProcessScan(result, False, "access denied")
    except Exception:
        return ProcessScan(result, False, "incomplete")

    while True:
        if _is_cancelled(cancelled):
            return ProcessScan(result, False, "incomplete")
        if time.monotonic() - started >= _MAX_SCAN_SECONDS:
            return ProcessScan(result, False, "incomplete")
        try:
            process = next(iterator)
        except StopIteration:
            return ProcessScan(result, True)
        except errors.access_denied:
            return ProcessScan(result, False, "access denied")
        except Exception:
            return ProcessScan(result, False, "incomplete")

        if len(result) >= _MAX_SCANNED_PROCESSES:
            return ProcessScan(result, False, "incomplete")
        try:
            runtime_process = process
            pid = _positive_integer(runtime_process.pid)
            create_time = _number(runtime_process.create_time())
            ppid = _positive_integer(runtime_process.ppid())
            name = _string_or_none(runtime_process.name())
        except errors.access_denied:
            return ProcessScan(result, False, "access denied")
        except Exception:
            return ProcessScan(result, False, "incomplete")
        if pid is None or create_time is None or ppid is None:
            return ProcessScan(result, False, "incomplete")
        identity = ProcessIdentity(pid, create_time)
        result[pid] = (
            identity,
            ppid,
            ProcessView(pid, create_time, name, None, ppid, None, "Unknown"),
        )


def _scan_unverified_reason(scan: ProcessScan) -> str:
    if scan.reason == "access denied":
        return "Unverified: process enumeration access denied"
    if scan.reason == "anchor unavailable":
        return "Unverified: App Server anchor unavailable"
    return "Unverified: process enumeration incomplete"


def _descendants(
    anchor: ProcessIdentity,
    scanned: Mapping[int, tuple[ProcessIdentity, int | None, ProcessView]],
    observed_at: float,
) -> dict[ProcessIdentity, int | None]:
    """Return only processes whose observed parent chain reaches this anchor identity."""
    by_pid: dict[int, ProcessIdentity] = {anchor.pid: anchor}
    descendants: dict[ProcessIdentity, int | None] = {}
    changed = True
    while changed:
        changed = False
        for identity, parent_pid, _view in scanned.values():
            if identity.pid in by_pid or parent_pid is None:
                continue
            parent = by_pid.get(parent_pid)
            if parent is None or identity.create_time < parent.create_time:
                continue
            if identity.create_time > observed_at + 10:
                continue
            by_pid[identity.pid] = identity
            descendants[identity] = parent_pid
            changed = True
    return descendants


def _observe_candidate(
    psutil_module: object, identity: ProcessIdentity, errors: Any
) -> tuple[str, ProcessView | None]:
    try:
        runtime_psutil: Any = psutil_module
        process = runtime_psutil.Process(identity.pid)
        create_time = _number(process.create_time())
    except errors.access_denied:
        return "Access denied", _identity_view(identity, "Access denied")
    except errors.all:
        return "Unverified: Codex child disappeared before inspection", None
    current = ProcessIdentity(identity.pid, create_time or 0.0)
    if create_time is None:
        return "Unverified: Codex child creation time unavailable", None
    if not _same_identity(identity, current):
        return "Unverified: Codex child PID changed during inspection", None
    state, view, _active = _read_live_process(process, identity, errors)
    if state == "Exited":
        return "Unverified: Codex child exited before verification", None
    return state, view


def _command_matches(summary: str, process: ProcessView) -> bool:
    executable = process.executable
    name = process.name
    arguments = process.arguments
    if not executable or not name or not arguments:
        return False
    executable_name = ntpath.basename(executable).casefold()
    if ntpath.basename(name).casefold() != executable_name:
        return False
    argument_executable = arguments[0]
    if ntpath.basename(argument_executable).casefold() != executable_name:
        return False
    if _has_windows_path(argument_executable) and not _same_windows_path(
        argument_executable, executable
    ):
        return False

    token, remainder = _split_command_prefix(summary)
    token_matches = bool(token) and _executable_token_matches(token, executable)
    explicit_executable = token_matches or _explicit_executable_token(token)
    if explicit_executable:
        if not token_matches:
            return False
        expected_body = _summary_command_body(remainder)
        actual_payload = _argv_command_payload(arguments[1:])
        actual_body = actual_payload
        if actual_body is None:
            actual_body = _argv_command_body(arguments[1:])
        return (
            expected_body is not None
            and actual_body is not None
            and _command_payload_matches(
                expected_body,
                actual_body,
                executable_name,
                allow_codex_utf8_setup=actual_payload is not None,
            )
        )

    if executable_name not in {"pwsh.exe", "powershell.exe", "cmd.exe"}:
        return False
    actual_script = _argv_command_payload(arguments[1:])
    return actual_script is not None and _command_payload_matches(
        summary, actual_script, executable_name, allow_codex_utf8_setup=True
    )


def _command_payload_matches(
    expected: str,
    actual: str,
    executable_name: str,
    *,
    allow_codex_utf8_setup: bool,
) -> bool:
    normalized_expected = _normalize_command(expected)
    if normalized_expected == _normalize_command(actual):
        return True
    if not allow_codex_utf8_setup or executable_name not in {"pwsh.exe", "powershell.exe"}:
        return False
    actual_without_setup = _remove_codex_powershell_utf8_setup(actual)
    return actual_without_setup is not None and normalized_expected == _normalize_command(
        actual_without_setup
    )


def _remove_codex_powershell_utf8_setup(actual_payload: str) -> str | None:
    if not actual_payload.startswith(_CODEX_POWERSHELL_UTF8_SETUP):
        return None
    suffix = actual_payload[len(_CODEX_POWERSHELL_UTF8_SETUP) :]
    if suffix.startswith("\r\n"):
        payload = suffix[2:]
    elif suffix.startswith("\n"):
        payload = suffix[1:]
    else:
        return None
    return payload if payload and not payload[0].isspace() else None


def _may_be_command_shell(summary: str, process_name: str | None) -> bool:
    if process_name is None:
        return True
    token, _remainder = _split_command_prefix(summary)
    name = ntpath.basename(process_name).casefold()
    if _explicit_executable_token(token):
        return _executable_token_matches(token, process_name)
    return name in {
        "pwsh.exe",
        "powershell.exe",
        "cmd.exe",
        "bash.exe",
        "sh.exe",
        "wsl.exe",
    }


def _split_command_prefix(command: str) -> tuple[str, str]:
    value = command.strip()
    if not value:
        return "", ""
    if value[0] in {'"', "'"}:
        closing = value.find(value[0], 1)
        if closing > 0:
            return value[1:closing], value[closing + 1 :].strip()
    parts = value.split(None, 1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def _explicit_executable_token(token: str) -> bool:
    if not token or any(character.isspace() for character in token):
        return False
    basename = ntpath.basename(token).casefold()
    return (
        basename in {"pwsh", "pwsh.exe", "powershell", "powershell.exe", "cmd", "cmd.exe"}
        or token.casefold().endswith(".exe")
        or _has_windows_path(token)
    )


def _executable_token_matches(token: str, executable: str) -> bool:
    if _has_windows_path(token):
        return _same_windows_path(token, executable)
    expected = ntpath.basename(token).casefold()
    actual = ntpath.basename(executable).casefold()
    if expected.endswith(".exe"):
        expected = expected[:-4]
    if actual.endswith(".exe"):
        actual = actual[:-4]
    return expected == actual


def _has_windows_path(value: str) -> bool:
    return "\\" in value or "/" in value or (len(value) > 1 and value[1] == ":")


def _same_windows_path(first: str, second: str) -> bool:
    return ntpath.normcase(ntpath.normpath(first)) == ntpath.normcase(ntpath.normpath(second))


def _summary_command_body(value: str) -> str | None:
    match = re.search(r"(?i)(?:^|\s)(?:-command|-c|/c|/k)\s+(.+)$", value, re.DOTALL)
    if match is not None:
        return _strip_outer_quotes(match.group(1))
    return value


def _argv_command_body(arguments: Sequence[str]) -> str | None:
    if not arguments:
        return ""
    return subprocess.list2cmdline(list(arguments))


def _argv_command_payload(arguments: Sequence[str]) -> str | None:
    for index, argument in enumerate(arguments):
        if argument.casefold() in {"-command", "-c", "/c", "/k"}:
            payload = arguments[index + 1 :]
            if not payload:
                return None
            return " ".join(payload)
    return None


def _strip_outer_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def _normalize_command(value: str) -> str:
    value = _strip_outer_quotes(value)
    output: list[str] = []
    quote: str | None = None
    pending_space = False
    escaped = False
    for character in value:
        if quote is None:
            if character.isspace():
                pending_space = bool(output)
                continue
            if pending_space:
                output.append(" ")
                pending_space = False
            output.append(character)
            if character in {'"', "'"}:
                quote = character
            continue
        output.append(character)
        if character == quote and not escaped:
            quote = None
        escaped = character == "`" if quote == '"' else False
    return "".join(output)


def _discover_children(
    commands: Sequence[CommandDescriptor],
    scanned: Mapping[int, tuple[ProcessIdentity, int | None, ProcessView]],
    psutil_module: object,
    errors: Any,
    roots: Mapping[CommandKey, ProcessIdentity],
    root_active: Mapping[CommandKey, ProcessIdentity],
    children: dict[CommandKey, list[ProcessIdentity]],
    child_views: dict[CommandKey, dict[ProcessIdentity, ProcessView]],
    child_active: dict[CommandKey, set[ProcessIdentity]],
    *,
    observed_at: float,
    cancelled: Event | None,
) -> None:
    tracked_total = sum(len(items) for items in children.values())
    for command in commands:
        known = children.setdefault(command.key, [])
        known_identities = set(known)
        visible_parents: dict[int, ProcessIdentity] = {}
        root_identity = roots.get(command.key)
        if root_identity is not None:
            root_in_scan = scanned.get(root_identity.pid)
            root_witnessed = root_in_scan is not None and _same_identity(
                root_identity, root_in_scan[0]
            )
            if root_active.get(command.key) == root_identity or root_witnessed:
                visible_parents[root_identity.pid] = root_identity
        for identity in known:
            if identity in child_active.get(command.key, set()):
                visible_parents[identity.pid] = identity
        changed = True
        while changed and tracked_total < _MAX_TRACKED_CHILDREN and not _is_cancelled(cancelled):
            changed = False
            for identity, parent_pid, _scan_view in scanned.values():
                if tracked_total >= _MAX_TRACKED_CHILDREN:
                    break
                if identity in known_identities or parent_pid is None:
                    continue
                parent = visible_parents.get(parent_pid)
                if parent is None:
                    continue
                if (
                    identity.create_time < parent.create_time
                    or identity.create_time > observed_at + 10
                ):
                    continue
                known.append(identity)
                known_identities.add(identity)
                _state, view, active = _read_identity(psutil_module, identity, errors)
                child_views.setdefault(command.key, {})[identity] = view
                if active:
                    child_active.setdefault(command.key, set()).add(identity)
                    visible_parents[identity.pid] = identity
                tracked_total += 1
                changed = True


def _command_line(value: object) -> str | None:
    if not isinstance(value, tuple) or not value:
        return None
    return subprocess.list2cmdline(list(value))[:_COMMAND_LINE_LIMIT]


def _command_arguments(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) > _COMMAND_ARGUMENT_LIMIT or not all(
        isinstance(part, str) and len(part) <= _COMMAND_ARGUMENTS_TOTAL_LIMIT for part in value
    ):
        return None
    arguments = tuple(value)
    if sum(len(argument) for argument in arguments) > _COMMAND_ARGUMENTS_TOTAL_LIMIT:
        return None
    return arguments


def _string_or_none(value: object) -> str | None:
    if (
        isinstance(value, str)
        and value
        and value.casefold() not in {"accessdenied", "zombieprocess"}
    ):
        return value[:1_000]
    return None


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value if value <= 4_294_967_295 else None


def _is_cancelled(cancelled: Event | None) -> bool:
    return cancelled is not None and cancelled.is_set()
