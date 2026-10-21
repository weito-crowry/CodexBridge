from __future__ import annotations

from dataclasses import dataclass

import pytest

from codex_bridge.console.process_inspection import (
    CommandDescriptor,
    CommandKey,
    InspectionResult,
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
    cmdline_value: list[str] | None = None
    status_value: str = "sleeping"
    running: bool = True

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

    def cmdline(self) -> list[str]:
        return list(self.cmdline_value or [])

    def ppid(self) -> int:
        return self.ppid_value

    def status(self) -> str:
        return self.status_value

    def is_running(self) -> bool:
        return self.running


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
    ) -> None:
        self.processes = processes
        self.listed = (
            listed
            if listed is not None
            else [process for process in processes.values() if isinstance(process, _Process)]
        )
        self.iter_calls = 0
        self.iter_attributes: list[str] = []

    def Process(self, pid: int) -> _Process:
        process = self.processes.get(pid)
        if isinstance(process, Exception):
            raise process
        if process is None:
            raise NoSuchProcess(pid)
        return process

    def process_iter(self, attrs: list[str]) -> list[_Process]:
        self.iter_calls += 1
        self.iter_attributes = attrs
        return self.listed


def _command(
    process_id: str = "100",
    *,
    start: float | None = 1_000,
    status: str = "in_progress",
) -> CommandDescriptor:
    return CommandDescriptor(
        key=CommandKey("thread-a", "turn-a", "item-a"),
        summary="Start-Sleep -Seconds 30",
        app_server_process_id=process_id,
        started_at_seconds=start,
        completed_at_seconds=None,
        command_status=status,
        exit_code=None,
    )


def test_verifies_root_by_creation_time_and_tracks_nested_children_after_parent_exits() -> None:
    root = _Process(100, 950.0)
    child = _Process(200, 960.0, ppid_value=100, name_value="python.exe")
    grandchild = _Process(201, 970.0, ppid_value=200, name_value="python.exe")
    psutil = _Psutil({100: root, 200: child, 201: grandchild})

    first = inspect_command_processes(
        (_command(),),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert first.commands[0].root_status == "Running"
    assert first.commands[0].root is not None
    assert first.commands[0].root.pid == 100
    assert first.commands[0].root.name == "pwsh.exe"
    assert first.commands[0].root.command_line == "pwsh.exe -NoProfile"
    assert first.commands[0].root.ppid == 1
    assert first.commands[0].root.status == "sleeping"
    assert [process.pid for process in first.commands[0].children] == [200, 201]
    assert psutil.iter_calls == 1
    assert psutil.iter_attributes == ["pid", "ppid", "create_time"]

    residual = _Psutil(
        {
            100: NoSuchProcess(100),
            200: _Process(200, 960.0, ppid_value=1, name_value="python.exe"),
            201: _Process(201, 970.0, ppid_value=200, name_value="python.exe"),
        },
        listed=[
            _Process(200, 960.0, ppid_value=1, name_value="python.exe"),
            _Process(201, 970.0, ppid_value=200, name_value="python.exe"),
        ],
    )
    second = inspect_command_processes(
        (_command(status="completed"),),
        first.evidence,
        psutil_module=residual,
        platform_name="win32",
        observed_at=1_010.0,
    )

    assert second.commands[0].root_status == "Exited"
    assert [process.pid for process in second.commands[0].children] == [200, 201]
    assert all(process.state == "Running" for process in second.commands[0].children)


@pytest.mark.parametrize(
    ("process_id", "start", "create_time", "expected"),
    [
        ("pty-42", 1_000.0, 990.0, "Unverified: App Server ID is not a decimal Windows PID"),
        ("100", None, 990.0, "Unverified: command start timestamp is unavailable"),
        ("100", 1_000.0, 900.0, "Unverified: process predates command by more than 60s"),
        ("100", 1_000.0, 1_011.0, "Unverified: process was created more than 10s after command"),
        ("0", 1_000.0, 990.0, "Unverified: App Server ID is not a decimal Windows PID"),
        ("4294967296", 1_000.0, 990.0, "Unverified: App Server ID is not a decimal Windows PID"),
    ],
)
def test_rejects_opaque_missing_timestamp_and_mismatched_pid_candidates(
    process_id: str, start: float | None, create_time: float, expected: str
) -> None:
    psutil = _Psutil({100: _Process(100, create_time)})

    result = inspect_command_processes(
        (_command(process_id, start=start),),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_000.0,
    )

    assert result.commands[0].root_status == expected
    assert result.commands[0].root is None
    assert psutil.iter_calls == 0


def test_reports_access_denied_and_process_gone_before_first_probe() -> None:
    denied = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil({100: AccessDenied(100)}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    gone = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil({100: NoSuchProcess(100)}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    zombie = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil({100: ZombieProcess(100)}),
        platform_name="win32",
        observed_at=1_000.0,
    )
    missing_creation_time = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil({100: _ProcessWithoutCreateTime(100, 990.0)}),
        platform_name="win32",
        observed_at=1_000.0,
    )

    assert denied.commands[0].root_status == "Access denied"
    assert gone.commands[0].root_status == "Exited (not observed)"
    assert zombie.commands[0].root_status == "Exited (zombie)"
    assert missing_creation_time.commands[0].root_status == (
        "Unknown: process creation time unavailable"
    )


def test_rejects_reused_root_pid_and_never_links_children_through_reused_child_pid() -> None:
    root = _Process(100, 950.0)
    child = _Process(200, 960.0, ppid_value=100, name_value="same-name.exe")
    first = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil({100: root, 200: child}),
        platform_name="win32",
        observed_at=1_000.0,
    )

    root_reused = inspect_command_processes(
        (_command(),),
        first.evidence,
        psutil_module=_Psutil({100: _Process(100, 1_005.0)}),
        platform_name="win32",
        observed_at=1_010.0,
    )
    assert root_reused.commands[0].root_status == "PID reused"
    assert root_reused.commands[0].root is None
    assert [(process.pid, process.state) for process in root_reused.commands[0].children] == [
        (200, "Exited")
    ]

    child_reused = _Psutil(
        {
            100: _Process(100, 950.0),
            200: _Process(200, 1_060.0, ppid_value=1, name_value="same-name.exe"),
            300: _Process(300, 1_070.0, ppid_value=200, name_value="same-name.exe"),
        },
        listed=[
            _Process(200, 1_060.0, ppid_value=1, name_value="same-name.exe"),
            _Process(300, 1_070.0, ppid_value=200, name_value="same-name.exe"),
        ],
    )
    second = inspect_command_processes(
        (_command(status="completed"),),
        first.evidence,
        psutil_module=child_reused,
        platform_name="win32",
        observed_at=1_080.0,
    )

    assert [process.state for process in second.commands[0].children] == ["PID reused"]
    assert child_reused.iter_calls == 1


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


def test_changed_app_server_id_discards_process_evidence_from_previous_candidate() -> None:
    first = inspect_command_processes(
        (_command(),),
        psutil_module=_Psutil(
            {
                100: _Process(100, 950.0),
                200: _Process(200, 960.0, ppid_value=100),
            }
        ),
        platform_name="win32",
        observed_at=1_000.0,
    )
    changed = inspect_command_processes(
        (_command("300"),),
        first.evidence,
        psutil_module=_Psutil({300: _Process(300, 990.0)}),
        platform_name="win32",
        observed_at=1_001.0,
    )

    assert changed.commands[0].root_status == "Running"
    assert changed.commands[0].root is not None
    assert changed.commands[0].root.pid == 300
    assert not changed.commands[0].children
    assert dict(changed.evidence.roots)[changed.commands[0].command.key].pid == 300


def test_related_process_tracking_is_bounded_to_one_hundred_children() -> None:
    root = _Process(100, 950.0)
    children = [
        _Process(pid, 960.0 + (pid - 200), ppid_value=100, name_value="child.exe")
        for pid in range(200, 320)
    ]
    psutil = _Psutil({100: root, **{process.pid: process for process in children}}, [*children])

    result = inspect_command_processes(
        (_command(),),
        psutil_module=psutil,
        platform_name="win32",
        observed_at=1_100.0,
    )

    assert len(result.commands[0].children) == 100
    assert psutil.iter_calls == 1
