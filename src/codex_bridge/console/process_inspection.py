from __future__ import annotations

import math
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Event
from typing import Any, cast

# Event delivery can lag process creation by a minute; a short after-window covers startup skew.
_ROOT_CREATED_BEFORE_COMMAND_SECONDS = 60.0
_ROOT_CREATED_AFTER_COMMAND_SECONDS = 10.0
_MAX_TRACKED_CHILDREN = 100
_MAX_SCANNED_PROCESSES = 5_000
_MAX_SCAN_SECONDS = 1.0
_PROCESS_ITER_ATTRIBUTES = ["pid", "ppid", "create_time"]


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
    command_keys = {command.key for command in command_list}
    old_roots = {key: identity for key, identity in evidence.roots if key in command_keys}
    old_children = {key: list(items) for key, items in evidence.children if key in command_keys}
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
        if _is_cancelled(cancelled):
            break
        pid = _windows_pid(command.app_server_process_id)
        prior = old_roots.get(command.key)
        if pid is None or (prior is not None and prior.pid != pid):
            roots.pop(command.key, None)
            children.pop(command.key, None)
            prior = None
        if pid is None:
            root_views[command.key] = None
            states[command.key] = CommandProcessState(
                command,
                "Unverified: App Server ID is not a decimal Windows PID",
                None,
                (),
            )
            continue
        try:
            process = runtime_psutil.Process(pid)
            create_time = _number(process.create_time())
        except errors.zombie:
            root_views[command.key] = _prior_view(prior, "Exited")
            states[command.key] = CommandProcessState(
                command, "Exited (zombie)", root_views[command.key], ()
            )
            continue
        except errors.no_such_process:
            root_views[command.key] = _prior_view(prior, "Exited")
            status = "Exited" if prior is not None else "Exited (not observed)"
            states[command.key] = CommandProcessState(command, status, root_views[command.key], ())
            continue
        except errors.access_denied:
            root_views[command.key] = _prior_view(prior, "Access denied")
            states[command.key] = CommandProcessState(
                command, "Access denied", root_views[command.key], ()
            )
            continue

        if create_time is None:
            root_views[command.key] = _prior_view(prior, "Unknown")
            states[command.key] = CommandProcessState(
                command, "Unknown: process creation time unavailable", root_views[command.key], ()
            )
            continue
        identity = ProcessIdentity(pid, create_time)
        if prior is not None and not _same_identity(prior, identity):
            root_views[command.key] = None
            states[command.key] = CommandProcessState(command, "PID reused", None, ())
            continue
        if prior is None:
            if command.started_at_seconds is None:
                root_views[command.key] = None
                states[command.key] = CommandProcessState(
                    command,
                    "Unverified: command start timestamp is unavailable",
                    None,
                    (),
                )
                continue
            age = command.started_at_seconds - create_time
            if age > _ROOT_CREATED_BEFORE_COMMAND_SECONDS:
                root_views[command.key] = None
                states[command.key] = CommandProcessState(
                    command,
                    "Unverified: process predates command by more than 60s",
                    None,
                    (),
                )
                continue
            if age < -_ROOT_CREATED_AFTER_COMMAND_SECONDS:
                root_views[command.key] = None
                states[command.key] = CommandProcessState(
                    command,
                    "Unverified: process was created more than 10s after command",
                    None,
                    (),
                )
                continue
        roots[command.key] = identity
        state, view, active = _read_live_process(process, identity, errors)
        root_views[command.key] = view
        if active:
            root_active[command.key] = identity
        states[command.key] = CommandProcessState(command, state, view, ())

    for command in command_list:
        if _is_cancelled(cancelled):
            break
        for identity in children.get(command.key, ()):
            state, view, active = _read_identity(psutil_module, identity, errors)
            child_views.setdefault(command.key, {})[identity] = view
            if active:
                child_active.setdefault(command.key, set()).add(identity)

    if any(root_active.values()) or any(child_active.values()):
        scan = _scan_processes(psutil_module, errors, cancelled)
        command_by_key = {command.key: command for command in command_list}
        for key, identity in tuple(root_active.items()):
            state, view, active = _read_identity(runtime_psutil, identity, errors)
            root_views[key] = view
            states[key] = CommandProcessState(command_by_key[key], state, view, ())
            if not active:
                root_active.pop(key, None)
        _discover_children(
            command_list,
            scan,
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
            roots=tuple(roots.items()),
            children=tuple((key, tuple(items)) for key, items in children.items() if items),
        ),
    )


def _empty_states(
    commands: Sequence[CommandDescriptor], message: str
) -> tuple[CommandProcessState, ...]:
    return tuple(CommandProcessState(command, message, None, ()) for command in commands)


def _windows_pid(value: str) -> int | None:
    if not value or not value.isascii() or not value.isdecimal():
        return None
    try:
        pid = int(value)
    except ValueError:
        return None
    return pid if 1 <= pid <= 4_294_967_295 else None


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


def _read_live_process(
    process: object, identity: ProcessIdentity, errors: Any
) -> tuple[str, ProcessView, bool]:
    runtime_process: Any = process
    name = _string_or_none(_safe_call(process, "name", errors))
    command_line = _command_line(_safe_call(process, "cmdline", errors))
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
    if create_time is None or not _same_identity(identity, current):
        return "PID reused", _identity_view(identity, "PID reused"), False
    return _read_live_process(process, identity, errors)


def _prior_view(identity: ProcessIdentity | None, state: str) -> ProcessView | None:
    if identity is None:
        return None
    return _identity_view(identity, state)


def _identity_view(identity: ProcessIdentity, state: str) -> ProcessView:
    return ProcessView(identity.pid, identity.create_time, None, None, None, None, state)


def _scan_processes(
    psutil_module: object, errors: Any, cancelled: Event | None
) -> dict[int, tuple[ProcessIdentity, int | None, ProcessView]]:
    result: dict[int, tuple[ProcessIdentity, int | None, ProcessView]] = {}
    started = time.monotonic()
    try:
        runtime_psutil: Any = psutil_module
        iterator = runtime_psutil.process_iter(attrs=_PROCESS_ITER_ATTRIBUTES)
        for index, process in enumerate(iterator):
            if index >= _MAX_SCANNED_PROCESSES or time.monotonic() - started >= _MAX_SCAN_SECONDS:
                break
            if _is_cancelled(cancelled):
                break
            info = getattr(process, "info", None)
            if not isinstance(info, Mapping):
                continue
            pid = _positive_integer(info.get("pid"))
            create_time = _number(info.get("create_time"))
            if pid is None or create_time is None:
                continue
            ppid = _positive_integer(info.get("ppid"))
            identity = ProcessIdentity(pid, create_time)
            result[pid] = (
                identity,
                ppid,
                ProcessView(pid, create_time, None, None, ppid, None, "Unknown"),
            )
    except errors.all:
        pass
    return result


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
    if not isinstance(value, (list, tuple)) or not all(isinstance(part, str) for part in value):
        return None
    if not value:
        return None
    return subprocess.list2cmdline(list(value))[:8_000]


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
