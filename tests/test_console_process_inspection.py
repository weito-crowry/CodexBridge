from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from codex_bridge.console import process_inspection
from codex_bridge.console.process_inspection import (
    CommandDescriptor,
    CommandKey,
    InspectionResult,
    ProcessEvidence,
    inspect_command_processes,
)


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class ZombieProcess(Exception):
    pass


@dataclass
class _Process:
    pid: int
    create_time_value: float
    ppid_value: int = 1
    name_value: str = "pwsh.exe"
    exe_value: str | None = None
    cmdline_value: list[str] | None = None
    status_value: str = "sleeping"
    running: bool = True
    children_error: Exception | None = None
    _owner: _Psutil | None = None

    def __post_init__(self) -> None:
        if self.cmdline_value is None:
            self.cmdline_value = ["pwsh.exe", "-NoProfile"]
        self.info: dict[str, object] = {
            "pid": self.pid,
            "ppid": self.ppid_value,
            "create_time": self.create_time_value,
            "name": self.name_value,
            "cmdline": self.cmdline_value,
            "status": self.status_value,
        }

    def create_time(self) -> float:
        return self.create_time_value

    def name(self) -> str:
        return self.name_value

    def exe(self) -> str:
        return self.exe_value or f"C:\\Windows\\System32\\{self.name_value}"

    def cmdline(self) -> list[str]:
        return list(self.cmdline_value or [])

    def ppid(self) -> int:
        return self.ppid_value

    def status(self) -> str:
        return self.status_value

    def is_running(self) -> bool:
        return self.running

    def children(self, recursive: bool = False) -> list[_Process]:
        if self._owner is not None:
            self._owner.children_calls.append((self.pid, recursive))
        if self.children_error is not None:
            raise self.children_error
        if self._owner is None:
            return []
        direct = [process for process in self._owner.listed if process.ppid_value == self.pid]
        if not recursive:
            return direct
        descendants: list[_Process] = []
        frontier = direct
        visited = {self.pid}
        while frontier:
            next_frontier: list[_Process] = []
            for process in frontier:
                if process.pid in visited:
                    continue
                visited.add(process.pid)
                descendants.append(process)
                next_frontier.extend(
                    child for child in self._owner.listed if child.ppid_value == process.pid
                )
            frontier = next_frontier
        return descendants


class _ProcessWithoutCreateTime(_Process):
    def create_time(self) -> None:
        return None


class _Psutil:
    NoSuchProcess = NoSuchProcess
    AccessDenied = AccessDenied
    ZombieProcess = ZombieProcess

    def __init__(
        self,
        processes: dict[int, _Process | Exception],
        listed: list[_Process] | None = None,
        *,
        forbid_process_iter: bool = False,
    ) -> None:
        self.processes = processes
        self.listed = (
            listed
            if listed is not None
            else [process for process in processes.values() if isinstance(process, _Process)]
        )
        self.iter_calls = 0
        self.iter_attributes: list[str] = []
        self.process_calls: list[int] = []
        self.children_calls: list[tuple[int, bool]] = []
        self.forbid_process_iter = forbid_process_iter
        for process in self.listed:
            process._owner = self

    def Process(self, pid: int) -> _Process:
        self.process_calls.append(pid)
        process = self.processes.get(pid)
        if isinstance(process, Exception):
            raise process
        if process is None:
            raise NoSuchProcess(pid)
        return process

    def process_iter(self, attrs: list[str]) -> list[_Process]:
        self.iter_calls += 1
        self.iter_attributes = attrs
        if self.forbid_process_iter:
            raise AssertionError("global process_iter must not be used")
        return self.listed


def _command(
    process_id: str = "100",
    *,
    start: float | None = 1_000,
    status: str = "in_progress",
    summary: str = "Start-Sleep -Seconds 30",
) -> CommandDescriptor:
    return CommandDescriptor(
        key=CommandKey("thread-a", "turn-a", "item-a"),
        summary=summary,
        app_server_process_id=process_id,
        started_at_seconds=start,
        completed_at_seconds=None,
        command_status=status,
        exit_code=None,
    )


def test_verifies_command_descendant_and_keeps_witnessed_children_after_shell_exit() -> None:
    anchor = _Process(
        220132,
        990.0,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    root = _Process(
        197728,
        999.5,
        ppid_value=220132,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    child = _Process(200, 1_000.0, ppid_value=197728, name_value="python.exe")
    grandchild = _Process(201, 1_000.5, ppid_value=200, name_value="python.exe")
    psutil = _Psutil({220132: anchor, 197728: root, 200: child, 201: grandchild})
    command = _command("pty-42", summary=_POWERSHELL_SUMMARY)

    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert first.commands[0].root_status == "Running"
    assert first.commands[0].root is not None
    assert first.commands[0].root.pid == 197728
    assert first.commands[0].root.name == "pwsh.exe"
    assert first.commands[0].root.command_line is not None
    assert _POWERSHELL_SCRIPT in first.commands[0].root.command_line
    assert [process.pid for process in first.commands[0].children] == [200, 201]
    assert psutil.iter_calls == 0
    assert psutil.children_calls == [(220132, True)]

    residual = _Psutil(
        {
            220132: _Process(
                220132,
                990.0,
                name_value="codex.exe",
                exe_value="C:\\Program Files\\Codex\\codex.exe",
            ),
            197728: NoSuchProcess(197728),
            200: _Process(200, 1_000.0, ppid_value=1, name_value="python.exe"),
            201: _Process(201, 1_000.5, ppid_value=200, name_value="python.exe"),
        },
        listed=[
            _Process(
                220132,
                990.0,
                name_value="codex.exe",
                exe_value="C:\\Program Files\\Codex\\codex.exe",
            ),
            _Process(200, 1_000.0, ppid_value=1, name_value="python.exe"),
            _Process(201, 1_000.5, ppid_value=200, name_value="python.exe"),
        ],
    )
    second = inspect_command_processes(
        (_command("pty-42", status="completed", summary=_POWERSHELL_SUMMARY),),
        first.evidence,
        psutil_module=residual,
        platform_name="win32",
        observed_at=1_010.0,
    )

    assert second.commands[0].root_status == "Exited"
    assert [process.pid for process in second.commands[0].children] == [200, 201]
    assert all(process.state == "Running" for process in second.commands[0].children)


@pytest.mark.parametrize(
    ("start", "anchor_create_time", "child_create_time", "observed_at", "expected"),
    [
        (
            None,
            990.0,
            999.5,
            1_001.0,
            "Unverified: command start timestamp unavailable",
        ),
        (
            1_000.0,
            800.0,
            900.0,
            1_001.0,
            "Unverified: no Codex child within command time window",
        ),
        (
            1_000.0,
            990.0,
            1_011.0,
            1_012.0,
            "Unverified: no Codex child within command time window",
        ),
    ],
)
def test_requires_command_timestamp_and_candidate_creation_time_window(
    start: float | None,
    anchor_create_time: float,
    child_create_time: float,
    observed_at: float,
    expected: str,
) -> None:
    psutil = _anchored_processes(
        anchor_create_time=anchor_create_time,
        child_create_time=child_create_time,
    )

    result = inspect_command_processes(
        (_command("pty-42", start=start, summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=observed_at,
    )

    assert result.commands[0].root_status == expected
    assert result.commands[0].root is None


def test_numeric_app_server_id_is_not_used_as_a_windows_pid() -> None:
    psutil = _Psutil({11385: _Process(11385, 990.0)})

    result = inspect_command_processes(
        (_command("11385"),),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_000.0,
    )

    assert result.commands[0].root_status == "Unverified: App Server anchor unavailable"
    assert result.commands[0].root is None
    assert psutil.process_calls == []


def test_matches_dogfood_command_from_rooted_three_level_descendant_tree() -> None:
    anchor = _Process(
        217648,
        200_000.0,
        ppid_value=1,
        name_value="cmd.exe",
        exe_value="C:\\Windows\\System32\\cmd.exe",
    )
    node = _Process(
        222124,
        200_100.0,
        ppid_value=217648,
        name_value="node.exe",
        exe_value="C:\\Program Files\\nodejs\\node.exe",
    )
    codex = _Process(
        221256,
        200_200.0,
        ppid_value=222124,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    shell = _Process(
        200676,
        200_700.0,
        ppid_value=221256,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    unrelated_opaque_pid = _Process(
        91919,
        200_700.0,
        ppid_value=1,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    psutil = _Psutil(
        {process.pid: process for process in (anchor, node, codex, shell, unrelated_opaque_pid)},
        [anchor, node, codex, shell, unrelated_opaque_pid],
        forbid_process_iter=True,
    )

    result = inspect_command_processes(
        (_command("91919", start=200_700.0, summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=217648),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=200_701.0,
    )

    command = result.commands[0]
    assert command.root_status == "Running"
    assert command.root is not None and command.root.pid == 200676
    assert psutil.iter_calls == 0
    assert psutil.children_calls == [(217648, True)]
    assert 91919 not in psutil.process_calls


def test_access_denied_rooted_enumeration_is_explicitly_incomplete() -> None:
    anchor = _Process(220132, 990.0, name_value="codex.exe", children_error=AccessDenied())
    candidate = _Process(
        197728,
        999.5,
        ppid_value=220132,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    psutil = _Psutil({220132: anchor, 197728: candidate}, [anchor, candidate])

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == ("Unverified: process enumeration access denied")
    assert result.commands[0].root is None
    assert psutil.iter_calls == 0


def test_disappeared_anchor_during_enumeration_preserves_verified_command_evidence() -> None:
    command = _command("pty-42", summary=_POWERSHELL_SUMMARY)
    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_anchored_processes(),
        platform_name="win32",
        observed_at=1_001.0,
    )
    source = _anchored_processes()

    class GoneAfterScan(_Psutil):
        anchor_queries = 0

        def Process(self, pid: int) -> _Process:
            if pid == 220132:
                self.anchor_queries += 1
                if self.anchor_queries == 2:
                    raise NoSuchProcess(pid)
            return super().Process(pid)

    psutil = GoneAfterScan(source.processes, source.listed)

    result = inspect_command_processes(
        (command,),
        first.evidence,
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_005.0,
    )

    assert result.commands[0].root_status == "Running"
    assert result.commands[0].root is not None
    assert result.commands[0].root.pid == 197728
    assert result.evidence.roots == first.evidence.roots
    assert result.evidence.app_server_identity == first.evidence.app_server_identity


def test_missing_anchor_child_list_is_incomplete_not_no_matching_child() -> None:
    anchor = _Process(220132, 990.0, name_value="codex.exe", children_error=NoSuchProcess())
    candidate = _Process(
        197728,
        999.5,
        ppid_value=220132,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    psutil = _Psutil({220132: anchor, 197728: candidate}, [anchor, candidate])

    result = inspect_command_processes(
        (_command("pty-42", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: process enumeration incomplete"
    assert result.commands[0].root is None


def test_truncated_rooted_enumeration_cannot_claim_unique_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor = _Process(220132, 990.0, name_value="cmd.exe")
    node = _Process(222124, 991.0, ppid_value=220132, name_value="node.exe")
    shell = _Process(
        197728,
        999.5,
        ppid_value=222124,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    psutil = _Psutil({220132: anchor, 222124: node, 197728: shell}, [anchor, node, shell])
    monkeypatch.setattr(process_inspection, "_MAX_SCANNED_PROCESSES", 1)

    result = inspect_command_processes(
        (_command("pty-42", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: process enumeration incomplete"
    assert result.commands[0].root is None
    assert not result.evidence.roots
    assert psutil.iter_calls == 0


def test_slow_rooted_enumeration_is_incomplete_not_a_negative_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psutil = _anchored_processes()
    clock_values = iter((0.0, 0.0, 5.0))
    monkeypatch.setattr(
        process_inspection,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )

    result = inspect_command_processes(
        (_command("pty-42", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: process enumeration incomplete"
    assert result.commands[0].root is None
    assert not result.evidence.roots
    assert psutil.iter_calls == 0


def test_incomplete_scan_preserves_previously_verified_root_evidence() -> None:
    command = _command("pty-42", summary=_POWERSHELL_SUMMARY)
    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_anchored_processes(),
        platform_name="win32",
        observed_at=1_001.0,
    )
    inaccessible = _anchored_processes()
    anchor = inaccessible.processes[220132]
    assert isinstance(anchor, _Process)
    anchor.children_error = AccessDenied()

    result = inspect_command_processes(
        (command,),
        first.evidence,
        psutil_module=inaccessible,
        platform_name="win32",
        observed_at=1_005.0,
    )

    assert result.commands[0].root_status == "Running"
    assert result.commands[0].root is not None
    assert result.commands[0].root.pid == 197728
    assert result.evidence.roots == first.evidence.roots
    assert inaccessible.iter_calls == 0


_POWERSHELL_SCRIPT = "1..12 | ForEach-Object { $_ }"
_POWERSHELL_SUMMARY = f"pwsh.exe -NoProfile -Command '{_POWERSHELL_SCRIPT}'"
_POWERSHELL_EXE = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"


def _anchored_processes(
    *,
    child_pid: int = 197728,
    anchor_create_time: float = 990.0,
    child_create_time: float = 999.5,
    child_ppid: int = 220132,
    child_script: str = _POWERSHELL_SCRIPT,
    second_matching_child: bool = False,
    include_opaque_pid: bool = True,
    include_intermediate_parent: bool = False,
) -> _Psutil:
    anchor = _Process(
        220132,
        anchor_create_time,
        ppid_value=1,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    parent_pid = 219728 if include_intermediate_parent else 220132
    if include_intermediate_parent:
        intermediate = _Process(
            parent_pid,
            max(anchor_create_time + 1.0, child_create_time - 1.0),
            ppid_value=220132,
            name_value="node.exe",
            exe_value="C:\\Program Files\\nodejs\\node.exe",
        )
    else:
        intermediate = None
    child = _Process(
        child_pid,
        child_create_time,
        ppid_value=parent_pid if child_ppid == 220132 else child_ppid,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", child_script],
    )
    processes: dict[int, _Process | Exception] = {220132: anchor, child_pid: child}
    listed = [anchor, child]
    if intermediate is not None:
        processes[intermediate.pid] = intermediate
        listed.insert(1, intermediate)
    if second_matching_child:
        second = _Process(
            child_pid + 1,
            child_create_time + 0.1,
            ppid_value=220132,
            name_value="pwsh.exe",
            exe_value=_POWERSHELL_EXE,
            cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", child_script],
        )
        processes[second.pid] = second
        listed.append(second)
    if include_opaque_pid:
        opaque_id_process = _Process(
            11385,
            999.5,
            ppid_value=1,
            name_value="pwsh.exe",
            exe_value=_POWERSHELL_EXE,
            cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", child_script],
        )
        processes[11385] = opaque_id_process
        listed.append(opaque_id_process)
    return _Psutil(processes, listed)


def test_matches_exact_powershell_command_beneath_actual_app_server_os_pid() -> None:
    psutil = _anchored_processes(include_intermediate_parent=True)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    command = result.commands[0]
    assert command.root_status == "Running"
    assert command.root is not None
    assert command.root.pid == 197728
    assert command.root.name == "pwsh.exe"
    assert command.root.executable == _POWERSHELL_EXE
    assert command.root.create_time == 999.5
    assert command.root.ppid == 219728
    assert command.root.status == "sleeping"
    assert command.root.command_line is not None
    assert _POWERSHELL_SCRIPT in command.root.command_line
    assert 11385 not in psutil.process_calls
    assert dict(result.evidence.roots)[command.command.key].pid == 197728


def test_missing_matching_child_is_unverified_and_not_reported_exited() -> None:
    psutil = _anchored_processes(child_script="Write-Output 'different script'")

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: no matching Codex child"
    assert result.commands[0].root is None
    assert 11385 not in psutil.process_calls


def test_mismatched_executable_path_is_not_linked() -> None:
    psutil = _anchored_processes()
    candidate = psutil.processes[197728]
    assert isinstance(candidate, _Process)
    candidate.exe_value = "C:\\OtherPowerShell\\pwsh.exe"

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: no matching Codex child"
    assert result.commands[0].root is None


def test_same_name_and_command_outside_codex_ancestry_is_not_linked() -> None:
    psutil = _anchored_processes(child_ppid=1)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: no matching Codex child"
    assert result.commands[0].root is None


def test_ambiguous_matching_codex_shells_are_unverified() -> None:
    psutil = _anchored_processes(second_matching_child=True)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: ambiguous matching Codex children"
    assert result.commands[0].root is None


def test_root_candidate_probe_limit_never_claims_a_unique_match() -> None:
    psutil = _anchored_processes()
    for pid in range(300, 401):
        process = _Process(
            pid,
            999.5 + (pid - 300) * 0.001,
            ppid_value=220132,
            name_value="pwsh.exe",
            exe_value=_POWERSHELL_EXE,
            cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
        )
        psutil.processes[pid] = process
        psutil.listed.append(process)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Unverified: Codex child candidate limit reached"
    assert result.commands[0].root is None
    assert sum(pid != 220132 for pid in psutil.process_calls) <= 100


def test_absolute_powershell_path_and_quoting_are_matched() -> None:
    psutil = _anchored_processes()
    summary = f'"{_POWERSHELL_EXE}" -NoProfile -Command "{_POWERSHELL_SCRIPT}"'

    result = inspect_command_processes(
        (_command("65448", summary=summary),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == "Running"
    assert result.commands[0].root is not None
    assert result.commands[0].root.pid == 197728


def test_candidate_access_denied_or_gone_is_unverified_not_exited() -> None:
    denied = _anchored_processes()
    denied.processes[197728] = AccessDenied(197728)
    gone = _anchored_processes()
    gone.processes[197728] = NoSuchProcess(197728)

    denied_result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=denied,
        platform_name="win32",
        observed_at=1_001.0,
    )
    gone_result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=gone,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert denied_result.commands[0].root_status == (
        "Unverified: Codex child command metadata access denied"
    )
    assert gone_result.commands[0].root_status == (
        "Unverified: Codex child disappeared before inspection"
    )
    assert denied_result.commands[0].root_status != "Exited"
    assert gone_result.commands[0].root_status != "Exited"


def test_candidate_without_creation_time_is_unverified() -> None:
    psutil = _anchored_processes()
    psutil.processes[197728] = _ProcessWithoutCreateTime(197728, 999.5)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == ("Unverified: Codex child creation time unavailable")
    assert result.commands[0].root is None


def test_verified_pid_without_creation_time_is_unknown_not_exited_or_reused() -> None:
    command = _command("11385", summary=_POWERSHELL_SUMMARY)
    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_anchored_processes(),
        platform_name="win32",
        observed_at=1_001.0,
    )
    missing_time = _anchored_processes()
    missing_time.processes[197728] = _ProcessWithoutCreateTime(197728, 999.5)

    result = inspect_command_processes(
        (command,),
        first.evidence,
        psutil_module=missing_time,
        platform_name="win32",
        observed_at=1_005.0,
    )

    assert result.commands[0].root_status == "Unknown: creation time unavailable"


def test_reused_app_server_anchor_discards_old_command_evidence() -> None:
    command = _command("11385", summary=_POWERSHELL_SUMMARY)
    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_anchored_processes(),
        platform_name="win32",
        observed_at=1_001.0,
    )
    replacement_anchor = _Process(
        220132,
        1_005.0,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )

    result = inspect_command_processes(
        (command,),
        first.evidence,
        psutil_module=_Psutil({220132: replacement_anchor}),
        platform_name="win32",
        observed_at=1_006.0,
    )

    assert result.commands[0].root_status == "Unverified: App Server anchor PID reused"
    assert result.commands[0].root is None
    assert not result.evidence.roots
    assert not result.evidence.children


def test_reused_app_server_anchor_during_scan_discards_candidate_matches() -> None:
    class ReusedDuringScan(_Psutil):
        anchor_queries = 0

        def Process(self, pid: int) -> _Process:
            if pid == 220132:
                self.anchor_queries += 1
                if self.anchor_queries > 1:
                    return _Process(
                        220132,
                        1_005.0,
                        name_value="codex.exe",
                        exe_value="C:\\Program Files\\Codex\\codex.exe",
                    )
            return super().Process(pid)

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=ReusedDuringScan(_anchored_processes().processes),
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert result.commands[0].root_status == (
        "Unverified: App Server anchor changed during inspection"
    )
    assert result.commands[0].root is None
    assert not result.evidence.roots


def test_reports_access_denied_and_process_gone_before_first_probe() -> None:
    denied = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_Psutil({220132: AccessDenied(220132)}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    gone = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_Psutil({}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    zombie = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_Psutil({220132: ZombieProcess(220132)}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    missing_creation_time = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_Psutil({220132: _ProcessWithoutCreateTime(220132, 990.0)}),
        platform_name="win32",
        observed_at=1_000.0,
    )

    assert denied.commands[0].root_status == "Unverified: App Server anchor access denied"
    assert gone.commands[0].root_status == "Unverified: App Server anchor unavailable"
    assert zombie.commands[0].root_status == "Unverified: App Server anchor unavailable"
    assert (
        missing_creation_time.commands[0].root_status == "Unverified: App Server anchor unavailable"
    )


def test_rejects_reused_root_pid_and_never_links_children_through_reused_child_pid() -> None:
    command = _command("11385", summary=_POWERSHELL_SUMMARY)
    anchor = _Process(
        220132,
        990.0,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    root = _Process(
        197728,
        999.5,
        ppid_value=220132,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    child = _Process(200, 1_000.0, ppid_value=197728, name_value="same-name.exe")
    first = inspect_command_processes(
        (command,),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_Psutil({220132: anchor, 197728: root, 200: child}),
        platform_name="win32",
        observed_at=1_000.0,
    )

    root_reused = inspect_command_processes(
        (command,),
        first.evidence,
        psutil_module=_Psutil(
            {
                220132: anchor,
                197728: _Process(
                    197728,
                    1_005.0,
                    ppid_value=220132,
                    name_value="pwsh.exe",
                    exe_value=_POWERSHELL_EXE,
                    cmdline_value=[
                        _POWERSHELL_EXE,
                        "-NoProfile",
                        "-Command",
                        _POWERSHELL_SCRIPT,
                    ],
                ),
            }
        ),
        platform_name="win32",
        observed_at=1_010.0,
    )
    assert root_reused.commands[0].root_status == "PID reused"
    assert root_reused.commands[0].root is not None
    assert root_reused.commands[0].root.pid == 197728
    root_still_reused = inspect_command_processes(
        (command,),
        root_reused.evidence,
        psutil_module=_Psutil(
            {
                220132: anchor,
                197728: _Process(
                    197728,
                    1_005.0,
                    ppid_value=220132,
                    name_value="pwsh.exe",
                    exe_value=_POWERSHELL_EXE,
                    cmdline_value=[
                        _POWERSHELL_EXE,
                        "-NoProfile",
                        "-Command",
                        _POWERSHELL_SCRIPT,
                    ],
                ),
            }
        ),
        platform_name="win32",
        observed_at=1_020.0,
    )
    assert root_still_reused.commands[0].root_status == "PID reused"
    assert root_still_reused.commands[0].root is not None
    assert root_still_reused.commands[0].root.state == "PID reused"

    child_reused = _Psutil(
        {
            220132: anchor,
            197728: root,
            200: _Process(200, 1_060.0, ppid_value=1, name_value="same-name.exe"),
            300: _Process(300, 1_070.0, ppid_value=200, name_value="same-name.exe"),
        },
        listed=[
            anchor,
            root,
            _Process(200, 1_060.0, ppid_value=1, name_value="same-name.exe"),
            _Process(300, 1_070.0, ppid_value=200, name_value="same-name.exe"),
        ],
    )
    second = inspect_command_processes(
        (_command("11385", status="completed", summary=_POWERSHELL_SUMMARY),),
        first.evidence,
        psutil_module=child_reused,
        platform_name="win32",
        observed_at=1_080.0,
    )

    assert [process.state for process in second.commands[0].children] == ["PID reused"]
    assert child_reused.iter_calls == 0
    assert child_reused.children_calls == [(220132, True)]


def test_other_platform_is_explicitly_unsupported_without_inspecting_processes() -> None:
    psutil = _Psutil({100: _Process(100, 950.0)})

    result: InspectionResult = inspect_command_processes(
        (_command(),),
        psutil_module=psutil,
        platform_name="linux",
    )

    assert not result.supported
    assert result.message == "Windows only"
    assert psutil.iter_calls == 0


def test_changed_app_server_os_pid_discards_previous_process_evidence() -> None:
    first = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=_anchored_processes(),
        platform_name="win32",
        observed_at=1_000.0,
    )
    new_anchor = _Process(
        330,
        1_005.0,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    changed = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=330),
        psutil_module=_Psutil({330: new_anchor}),
        platform_name="win32",
        observed_at=1_006.0,
    )

    assert changed.commands[0].root_status == (
        "Unverified: App Server anchor started after this command"
    )
    assert changed.commands[0].root is None
    assert not changed.commands[0].children
    assert first.evidence.roots
    assert not changed.evidence.roots


def test_related_process_tracking_is_bounded_to_one_hundred_children() -> None:
    anchor = _Process(
        220132,
        900.0,
        name_value="codex.exe",
        exe_value="C:\\Program Files\\Codex\\codex.exe",
    )
    root = _Process(
        197728,
        999.5,
        ppid_value=220132,
        name_value="pwsh.exe",
        exe_value=_POWERSHELL_EXE,
        cmdline_value=[_POWERSHELL_EXE, "-NoProfile", "-Command", _POWERSHELL_SCRIPT],
    )
    children = [
        _Process(
            pid,
            1_000.0 + (pid - 200) * 0.01,
            ppid_value=197728,
            name_value="child.exe",
        )
        for pid in range(200, 320)
    ]
    psutil = _Psutil(
        {
            220132: anchor,
            197728: root,
            **{process.pid: process for process in children},
        },
        [anchor, root, *children],
    )

    result = inspect_command_processes(
        (_command("11385", summary=_POWERSHELL_SUMMARY),),
        ProcessEvidence(app_server_os_pid=220132),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert len(result.commands[0].children) == 100
    assert psutil.iter_calls == 0
    assert psutil.children_calls == [(220132, True)]
