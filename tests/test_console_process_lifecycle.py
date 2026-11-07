from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codex_bridge.console.process_inspection import (
    CommandDescriptor,
    CommandKey,
    ProcessEvidence,
    ProcessIdentity,
    inspect_command_processes,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process lifecycle")


def _wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None, "Owned process exited before its child was ready"
        time.sleep(0.01)
    assert path.exists(), "Owned child did not announce readiness"


@pytest.mark.parametrize(("status", "exit_code"), [("completed", 0), ("failed", 7)])
def test_windows_owned_process_exit_preserves_running_residual_then_observes_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, exit_code: int
) -> None:
    """Catch dropped residual evidence or false liveness after a real Windows parent exits."""
    psutil = pytest.importorskip("psutil")
    ready = tmp_path / "child-ready"
    root_stop = tmp_path / "root-stop"
    child_stop = tmp_path / "child-stop"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "ready = Path(sys.argv[1])\n"
        "ready.with_suffix('.pending').write_text(str(os.getpid()))\n"
        "ready.with_suffix('.pending').rename(ready)\n"
        "deadline = time.monotonic() + 30\n"
        "while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    root_script = tmp_path / "root.py"
    root_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]])\n"
        "deadline = time.monotonic() + 30\n"
        "while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "sys.exit(int(sys.argv[5]))\n",
        encoding="utf-8",
    )
    argv = [
        str(Path(sys._base_executable).resolve()),
        str(root_script),
        str(child_script),
        str(ready),
        str(child_stop),
        str(root_stop),
        str(exit_code),
    ]
    command = CommandDescriptor(
        CommandKey("owned-thread", "owned-turn", "owned-command"),
        subprocess.list2cmdline(argv),
        "opaque-session-id",
        time.time(),
        None,
        "in_progress",
        None,
    )

    # The monitor may inspect processes, but it must never control even these owned ones.
    def reject_control(*args: object, **kwargs: object) -> None:
        pytest.fail("Process monitor attempted to control an OS process")

    for method in ("terminate", "kill", "send_signal", "suspend", "resume"):
        monkeypatch.setattr(psutil.Process, method, reject_control)

    child = None
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL) as root:
        try:
            _wait_for_file(ready, root)
            child_pid = int(ready.read_text())
            child = psutil.Process(child_pid)
            child_created = child.create_time()
            root_created = psutil.Process(root.pid).create_time()
            first = inspect_command_processes(
                (command,), ProcessEvidence(app_server_os_pid=os.getpid())
            )
            state = first.commands[0]
            assert state.root_status == "Running", (
                command.summary,
                psutil.Process(root.pid).as_dict(attrs=["pid", "ppid", "name", "exe", "cmdline"]),
                state,
            )
            assert state.root is not None
            assert state.root.pid == root.pid
            assert state.root.create_time == pytest.approx(root_created, abs=0.01)
            assert state.root.ppid == os.getpid()
            assert state.root.arguments == tuple(argv)
            assert state.root.name and state.root.name.lower() == "python.exe"
            assert len(state.children) == 1
            assert state.children[0].pid == child_pid
            assert state.children[0].ppid == root.pid
            assert state.children[0].create_time == pytest.approx(child_created, abs=0.01)
            assert state.children[0].state == "Running"
            assert root.poll() is None and child.is_running()

            root_stop.touch()
            assert root.wait(timeout=10) == exit_code
            ended = replace(
                command,
                command_status=status,
                completed_at_seconds=time.time(),
                exit_code=exit_code,
            )
            second = inspect_command_processes((ended,), first.evidence)
            state = second.commands[0]
            assert state.command.command_status == status
            assert state.command.exit_code == exit_code
            assert state.root_status == "Exited"
            assert state.root is not None and state.root.pid == root.pid
            assert [(item.pid, item.state) for item in state.children] == [(child_pid, "Running")]
            assert dict(second.evidence.roots)[command.key] == ProcessIdentity(
                root.pid, root_created
            )
            assert dict(second.evidence.children)[command.key] == (
                ProcessIdentity(child_pid, child_created),
            )

            child_stop.touch()
            child.wait(timeout=10)
            final = inspect_command_processes((ended,), second.evidence)
            assert final.commands[0].root_status == "Exited"
            assert [(item.pid, item.state) for item in final.commands[0].children] == [
                (child_pid, "Exited")
            ]
        finally:
            # Release only this test's file gates; neither monitor nor cleanup sends OS signals.
            root_stop.touch()
            child_stop.touch()
            root.wait(timeout=10)
            if child is not None:
                child.wait(timeout=10)
