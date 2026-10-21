from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .paths import AllowedPathPolicy, PathPolicyError

GIT_TIMEOUT_SECONDS = 8.0
MAX_GIT_OUTPUT_BYTES = 256 * 1024
_STDERR_LIMIT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class _GitOutput:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool
    launch_failed: bool = False


def _run_git(args: list[str], *, cwd: str, output_limit: int = MAX_GIT_OUTPUT_BYTES) -> _GitOutput:
    env = os.environ.copy()
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"})
    try:
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                *args,
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError:
        return _GitOutput(127, b"", b"", False, False, True)

    stdout = bytearray()
    stderr = bytearray()
    truncated = threading.Event()

    def drain(stream: BinaryIO, destination: bytearray, limit: int, mark_truncated: bool) -> None:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            remaining = max(0, limit - len(destination))
            if remaining:
                destination.extend(chunk[:remaining])
            if mark_truncated and len(chunk) > remaining:
                truncated.set()

    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout, output_limit, True),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr, _STDERR_LIMIT_BYTES, False),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    timed_out = False
    while process.poll() is None:
        if truncated.is_set():
            process.terminate()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.terminate()
            break
        time.sleep(0.01)
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)
    return _GitOutput(
        process.returncode or 0,
        bytes(stdout),
        bytes(stderr),
        truncated.is_set(),
        timed_out,
    )


def _error(category: str) -> dict[str, object]:
    return {"ok": False, "error": category}


def _repo_root(repo_path: str, allowed_roots: tuple[str, ...]) -> str | dict[str, object]:
    if not isinstance(repo_path, str) or not Path(repo_path).is_absolute():
        return _error("repo_path_not_absolute")
    try:
        policy = AllowedPathPolicy(allowed_roots)
        canonical = policy.validate_cwd(repo_path)
    except (OSError, RuntimeError, PathPolicyError, TypeError) as exc:
        if isinstance(exc, PathPolicyError) and exc.category == "cwd_outside_allowed_root":
            return _error("repo_outside_allowed_root")
        return _error("repo_path_invalid_or_outside_allowed_root")
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=canonical, output_limit=4096)
    if result.timed_out:
        return {**_error("git_timeout"), "timed_out": True}
    if result.returncode != 0:
        if result.launch_failed:
            return _error("git_unavailable")
        if b"not a git repository" in result.stderr.lower():
            return _error("not_a_git_repository")
        return _error("git_failed")
    try:
        root = policy.validate_cwd(os.fsdecode(result.stdout).strip())
    except (OSError, RuntimeError, PathPolicyError):
        return _error("repo_outside_allowed_root")
    return root


def _validate_pathspec(path: str | None) -> str | None | dict[str, object]:
    if path is None:
        return None
    if not isinstance(path, str) or not path or "\x00" in path:
        return _error("invalid_pathspec")
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith(":"):
        return _error("invalid_pathspec")
    if any(part in {".", ".."} for part in normalized.split("/")):
        return _error("invalid_pathspec")
    if len(normalized) > 4096:
        return _error("invalid_pathspec")
    return path


def git_status(repo_path: str, *, allowed_roots: tuple[str, ...]) -> dict[str, object]:
    """Return bounded read-only status data for a repository inside allowed roots."""
    resolved = _repo_root(repo_path, allowed_roots)
    if isinstance(resolved, dict):
        return resolved
    cwd = resolved
    output = _run_git(
        ["status", "--porcelain=v1", "-z", "--branch", "--untracked-files=normal"],
        cwd=cwd,
    )
    if output.timed_out:
        return {**_error("git_timeout"), "timed_out": True}
    if output.returncode != 0:
        return _error("git_failed")
    raw = output.stdout
    header, separator, entries_data = raw.partition(b"\x00")
    branch = header.removeprefix(b"## ").decode("utf-8", errors="replace") if separator else ""
    entries = entries_data.split(b"\x00") if separator else []
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    index = 0
    while index < len(entries):
        item = entries[index]
        index += 1
        if len(item) < 4:
            continue
        status = item[:2].decode("ascii", errors="replace")
        path = os.fsdecode(item[3:])
        if status == "??":
            untracked.append(path)
            continue
        if status[0] not in {" ", "?"}:
            staged.append(path)
        if status[1] not in {" ", "?"}:
            unstaged.append(path)
        if "R" in status or "C" in status:
            if index < len(entries):
                index += 1
    return {
        "ok": True,
        "branch": branch,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "truncated": output.truncated,
    }


def git_diff(
    repo_path: str,
    staged: bool = False,
    path: str | None = None,
    *,
    allowed_roots: tuple[str, ...],
) -> dict[str, object]:
    """Return a bounded tracked-file patch; untracked file contents are excluded."""
    if not isinstance(staged, bool):
        return _error("invalid_staged_option")
    pathspec = _validate_pathspec(path)
    if isinstance(pathspec, dict):
        return pathspec
    resolved = _repo_root(repo_path, allowed_roots)
    if isinstance(resolved, dict):
        return resolved
    cwd = resolved
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    if staged:
        args.append("--cached")
    args.extend(["--binary", "--"])
    if pathspec is not None:
        args.append(pathspec)
    output = _run_git(args, cwd=cwd)
    if output.timed_out:
        return {
            **_error("git_timeout"),
            "timed_out": True,
            "untracked_included": False,
        }
    if output.returncode != 0:
        return _error("git_failed")
    diff = output.stdout.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "diff": diff,
        "has_changes": bool(diff),
        "staged": staged,
        "path": pathspec,
        "truncated": output.truncated,
        "untracked_included": False,
    }
