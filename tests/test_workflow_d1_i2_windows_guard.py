from __future__ import annotations

import ctypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_bridge.workflows import windows_guard
from codex_bridge.workflows.windows_guard import (
    MutexWaitState,
    WindowsGuardError,
    WindowsWriterMutex,
    current_user_sid,
    mutex_identity,
)

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="requires native Windows Win32 mutex APIs"
)


def test_mutex_identity_is_global_stable_and_root_sid_scoped(tmp_path: Path) -> None:
    root = str(tmp_path / "guard-state")
    sid = "S-1-5-21-100-200-300-1001"

    first = mutex_identity(root, sid)
    same = mutex_identity(root + "\\", sid)
    other_root = mutex_identity(str(tmp_path / "other-state"), sid)
    other_sid = mutex_identity(root, "S-1-5-21-100-200-300-1002")

    assert first.name.startswith(r"Global\CodexBridge.Workflow.Writer.v1.")
    assert first.name == same.name
    assert first.name != other_root.name
    assert first.name != other_sid.name


def test_mutex_identity_rejects_relative_path_and_invalid_sid() -> None:
    with pytest.raises(WindowsGuardError) as relative_error:
        mutex_identity("relative\\state", "S-1-5-21-100-200-300-1001")
    assert relative_error.value.reason_code == "absolute_state_root_required"
    assert "relative" not in str(relative_error.value)

    with pytest.raises(WindowsGuardError) as sid_error:
        mutex_identity(r"C:\state", "not-a-sid")
    assert sid_error.value.reason_code == "invalid_user_sid"


def test_mutex_identity_repr_hides_root_and_user_sid(tmp_path: Path) -> None:
    root = str(tmp_path / "private-state-root")
    sid = "S-1-5-21-100-200-300-1001"
    identity = mutex_identity(root, sid)
    rendered = repr(identity)

    assert identity.name.rsplit(".", maxsplit=1)[-1] in rendered
    assert root.casefold() not in rendered.casefold()
    assert sid not in rendered


@WINDOWS_ONLY
def test_current_user_sid_comes_from_windows_token() -> None:
    sid = current_user_sid()
    assert sid.startswith("S-1-")


@WINDOWS_ONLY
def test_global_mutex_creation_verifies_owner_and_protected_dacl(
    tmp_path: Path,
) -> None:
    guard = WindowsWriterMutex(str(tmp_path / "mutex-owner-dacl"), timeout_ms=100)
    try:
        assert guard.acquire() is MutexWaitState.ACQUIRED
        assert guard.identity.name.startswith(r"Global\CodexBridge.Workflow.Writer.v1.")
        assert guard.user_sid == current_user_sid()
    finally:
        if guard.wait_state is not None:
            guard.release()


@WINDOWS_ONLY
def test_mutex_contention_times_out_and_releases_for_same_session(
    tmp_path: Path,
) -> None:
    root = str(tmp_path / "mutex-contention")
    first = WindowsWriterMutex(root, timeout_ms=100)
    assert first.acquire() is MutexWaitState.ACQUIRED

    def contend() -> str:
        second = WindowsWriterMutex(root, timeout_ms=100)
        try:
            second.acquire()
        except WindowsGuardError as error:
            return error.reason_code
        else:
            second.release()
            return "unexpectedly_acquired"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(contend).result(timeout=10) == "mutex_wait_timeout"
    finally:
        first.release()

    next_owner = WindowsWriterMutex(root, timeout_ms=100)
    try:
        assert next_owner.acquire() is MutexWaitState.ACQUIRED
    finally:
        if next_owner.wait_state is not None:
            next_owner.release()


@WINDOWS_ONLY
def test_mutex_cannot_be_released_from_a_different_thread(tmp_path: Path) -> None:
    guard = WindowsWriterMutex(str(tmp_path / "mutex-thread-owner"), timeout_ms=100)
    assert guard.acquire() is MutexWaitState.ACQUIRED
    outcomes: list[str] = []

    def wrong_thread_release() -> None:
        try:
            guard.release()
        except WindowsGuardError as error:
            outcomes.append(error.reason_code)

    thread = threading.Thread(target=wrong_thread_release)
    thread.start()
    thread.join(timeout=10)
    try:
        assert not thread.is_alive()
        assert outcomes == ["mutex_wrong_owner_thread"]
        assert guard.wait_state is MutexWaitState.ACQUIRED
    finally:
        if guard.wait_state is not None:
            guard.release()


@WINDOWS_ONLY
def test_mutex_security_readback_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = WindowsWriterMutex(str(tmp_path / "mutex-bad-readback"), timeout_ms=100)

    def reject_readback(*args: object, **kwargs: object) -> None:
        raise WindowsGuardError("mutex_dacl_mismatch")

    monkeypatch.setattr(windows_guard, "_verify_mutex_security", reject_readback)
    with pytest.raises(WindowsGuardError) as error:
        guard.acquire()
    assert error.value.reason_code == "mutex_dacl_mismatch"
    assert guard.wait_state is None


@WINDOWS_ONLY
def test_localfree_success_is_zero_in_real_win32_calls() -> None:
    kernel32, advapi32 = windows_guard._load_apis()
    sid = current_user_sid()

    with windows_guard._security_attributes(advapi32, kernel32, sid):
        pass


class _FakeKernel:
    def __init__(self, *, create_error: int = 0, wait_result: int = 0) -> None:
        self.create_error = create_error
        self.wait_result = wait_result
        self.create_flags: int | None = None
        self.wait_calls = 0
        self.release_calls = 0
        self.closed_handles: list[object] = []
        self.real_kernel32, _ = windows_guard._load_apis()

    def CreateMutexExW(
        self, attributes: object, name: str, flags: int, access: int
    ) -> ctypes.c_void_p:
        del attributes, name, access
        self.create_flags = flags
        ctypes.set_last_error(self.create_error)
        return ctypes.c_void_p(0x1234)

    def WaitForSingleObject(self, handle: object, timeout_ms: int) -> int:
        del handle, timeout_ms
        self.wait_calls += 1
        return self.wait_result

    def GetCurrentThreadId(self) -> int:
        return 4321

    def ReleaseMutex(self, handle: object) -> int:
        del handle
        self.release_calls += 1
        return 1

    def CloseHandle(self, handle: object) -> int:
        self.closed_handles.append(handle)
        return 1

    def LocalFree(self, pointer: object) -> object:
        return self.real_kernel32.LocalFree(pointer)


def _install_fake_mutex_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_error: int = 0,
    wait_result: int = 0,
) -> _FakeKernel:
    _, advapi32 = windows_guard._load_apis()
    fake = _FakeKernel(create_error=create_error, wait_result=wait_result)
    monkeypatch.setattr(windows_guard, "current_user_sid", lambda: "S-1-5-21-1-2-3-4")
    monkeypatch.setattr(windows_guard, "_load_apis", lambda: (fake, advapi32))
    monkeypatch.setattr(windows_guard, "_verify_mutex_security", lambda *args: None)
    return fake


@WINDOWS_ONLY
def test_create_mutex_uses_zero_flags_and_closes_on_unexpected_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_mutex_api(monkeypatch, create_error=5)
    guard = WindowsWriterMutex(str(tmp_path / "fake-create-status"), timeout_ms=100)

    with pytest.raises(WindowsGuardError) as error:
        guard.acquire()

    assert error.value.reason_code == "mutex_create_status_invalid"
    assert fake.create_flags == 0
    assert fake.wait_calls == 0
    assert len(fake.closed_handles) == 1
    assert guard.wait_state is None


@WINDOWS_ONLY
def test_abandoned_mutex_is_not_ready_but_same_thread_cleanup_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_mutex_api(monkeypatch, wait_result=0x80)
    guard = WindowsWriterMutex(str(tmp_path / "fake-abandoned"), timeout_ms=100)

    assert guard.acquire() is MutexWaitState.ABANDONED
    assert guard.needs_reconciliation
    assert not guard.is_ready
    with pytest.raises(WindowsGuardError) as error:
        guard.mark_reconciled()
    assert error.value.reason_code == "mutex_reconciliation_unavailable"

    guard.close()
    assert fake.release_calls == 1
    assert len(fake.closed_handles) == 1
    assert guard.wait_state is None


@WINDOWS_ONLY
def test_abandoned_mutex_context_never_enters_body_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install_fake_mutex_api(monkeypatch, wait_result=0x80)
    guard = WindowsWriterMutex(str(tmp_path / "fake-abandoned-exception"), timeout_ms=100)
    body_entered = False

    with pytest.raises(WindowsGuardError) as error:
        with guard:
            body_entered = True

    assert error.value.reason_code == "mutex_reconciliation_required"
    assert not body_entered
    assert fake.release_calls == 1
    assert len(fake.closed_handles) == 1
    assert guard.wait_state is None


@pytest.mark.skipif(os.name == "nt", reason="non-Windows fail-closed path only")
def test_runtime_mutex_fails_closed_without_posix_fallback(tmp_path: Path) -> None:
    with pytest.raises(WindowsGuardError) as error:
        WindowsWriterMutex(str(tmp_path / "not-a-windows-mutex"))
    assert error.value.reason_code == "windows_only"
