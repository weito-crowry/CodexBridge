"""Isolated Windows state-root writer mutex primitives.

This module is intentionally not connected to the Workflow controller.  It
fails closed on non-Windows platforms and exposes no POSIX fallback.
"""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WindowsGuardError(RuntimeError):
    """Safe error whose message never includes a path, SID, or input value."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"Windows guard failed: {reason_code}")


class MutexWaitState(StrEnum):
    ACQUIRED = "ACQUIRED"
    ABANDONED = "ABANDONED"


@dataclass(frozen=True, slots=True)
class MutexIdentity:
    name: str
    state_root: str = field(repr=False)
    user_sid: str = field(repr=False)


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", wintypes.DWORD),
        ("SidStart", wintypes.DWORD),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    ]


_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_ALREADY_EXISTS = 183
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_KERNEL_OBJECT = 6
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_MUTEX_MODIFY_STATE = 0x0001
_READ_CONTROL = 0x00020000
_SYNCHRONIZE = 0x00100000
_MUTEX_ACCESS = _MUTEX_MODIFY_STATE | _READ_CONTROL | _SYNCHRONIZE
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_SYSTEM_SID = "S-1-5-18"
_SID_PATTERN = re.compile(r"^S-\d-\d+(?:-\d+)+$", re.IGNORECASE)
_GLOBAL_MUTEX_PREFIX = r"Global\CodexBridge.Workflow.Writer.v1."


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsGuardError("windows_only")


def _canonical_state_root(state_root: str | os.PathLike[str]) -> str:
    raw = os.fspath(state_root)
    if not isinstance(raw, str) or "\x00" in raw:
        raise WindowsGuardError("invalid_state_root")
    normalized_input = raw.replace("/", "\\")
    if not ntpath.isabs(normalized_input):
        raise WindowsGuardError("absolute_state_root_required")
    if normalized_input.startswith(("\\\\?\\", "\\\\.\\")):
        raise WindowsGuardError("device_path_rejected")
    drive, tail = ntpath.splitdrive(normalized_input)
    if not drive or not tail.startswith("\\"):
        raise WindowsGuardError("absolute_state_root_required")
    if any(part in {".", ".."} for part in tail.split("\\")):
        raise WindowsGuardError("ambiguous_state_root")
    if ":" in tail:
        raise WindowsGuardError("alternate_stream_rejected")
    return ntpath.normcase(ntpath.normpath(normalized_input))


def _frame(data: bytes) -> bytes:
    if len(data) > 0xFFFFFFFF:
        raise WindowsGuardError("identity_component_too_large")
    return len(data).to_bytes(4, "big") + data


def mutex_identity(state_root: str | os.PathLike[str], user_sid: str) -> MutexIdentity:
    """Return the stable Global mutex identity for an absolute root and SID."""
    if not _SID_PATTERN.fullmatch(user_sid):
        raise WindowsGuardError("invalid_user_sid")
    canonical_root = _canonical_state_root(state_root)
    try:
        framed = _frame(canonical_root.encode("utf-8", "strict")) + _frame(
            user_sid.upper().encode("ascii", "strict")
        )
    except UnicodeError as exc:
        raise WindowsGuardError("invalid_identity_encoding") from exc
    digest = hashlib.sha256(framed).hexdigest()
    return MutexIdentity(
        name=f"{_GLOBAL_MUTEX_PREFIX}{digest}",
        state_root=canonical_root,
        user_sid=user_sid.upper(),
    )


def _load_apis() -> tuple[Any, Any]:
    _require_windows()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise WindowsGuardError("win32_api_unavailable") from exc

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateMutexExW.argtypes = [
        ctypes.POINTER(_SecurityAttributes),
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.CreateMutexExW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return kernel32, advapi32


def current_user_sid() -> str:
    """Read the current process token's TokenUser SID using Win32 APIs."""
    kernel32, advapi32 = _load_apis()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise WindowsGuardError("token_open_failed")
    try:
        required = wintypes.DWORD()
        ctypes.set_last_error(0)
        ok = advapi32.GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(required))
        error = ctypes.get_last_error()
        if ok or error != _ERROR_INSUFFICIENT_BUFFER or not required.value:
            raise WindowsGuardError("token_user_query_failed")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            ctypes.cast(buffer, ctypes.c_void_p),
            required.value,
            ctypes.byref(required),
        ):
            raise WindowsGuardError("token_user_query_failed")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_to_string(advapi32, token_user.User.Sid, kernel32)
    finally:
        if token and not kernel32.CloseHandle(token):
            # Token cleanup failure is not allowed to turn an identity query
            # into a success path with uncertain resource ownership.
            raise WindowsGuardError("token_close_failed")


def _sid_to_string(advapi32: Any, sid: ctypes.c_void_p, kernel32: Any) -> str:
    if not sid:
        raise WindowsGuardError("sid_missing")
    string_sid = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
        raise WindowsGuardError("sid_conversion_failed")
    try:
        value = string_sid.value
        if not value or not _SID_PATTERN.fullmatch(value):
            raise WindowsGuardError("sid_conversion_invalid")
        return value.upper()
    finally:
        if kernel32.LocalFree(ctypes.cast(string_sid, ctypes.c_void_p)):
            raise WindowsGuardError("sid_string_free_failed")


@contextmanager
def _security_attributes(
    advapi32: Any, kernel32: Any, user_sid: str
) -> Iterator[_SecurityAttributes]:
    sddl = f"O:{user_sid}D:P(A;;0x{_MUTEX_ACCESS:08X};;;{user_sid})(A;;0x{_MUTEX_ACCESS:08X};;;SY)"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise WindowsGuardError("security_descriptor_create_failed")
    attributes = _SecurityAttributes(
        nLength=ctypes.sizeof(_SecurityAttributes),
        lpSecurityDescriptor=descriptor,
        bInheritHandle=False,
    )
    try:
        yield attributes
    finally:
        if descriptor and kernel32.LocalFree(descriptor):
            raise WindowsGuardError("security_descriptor_free_failed")


def _verify_mutex_security(
    advapi32: Any, kernel32: Any, handle: wintypes.HANDLE, user_sid: str
) -> None:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetSecurityInfo(
        handle,
        _SE_KERNEL_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0 or not descriptor or not owner or not dacl:
        raise WindowsGuardError("mutex_security_read_failed")
    try:
        if _sid_to_string(advapi32, owner, kernel32) != user_sid:
            raise WindowsGuardError("mutex_owner_mismatch")
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise WindowsGuardError("mutex_security_control_failed")
        if not control.value & _SE_DACL_PROTECTED:
            raise WindowsGuardError("mutex_dacl_not_protected")

        acl_info = _AclSizeInformation()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION,
        ):
            raise WindowsGuardError("mutex_dacl_read_failed")
        if acl_info.AceCount != 2:
            raise WindowsGuardError("mutex_dacl_mismatch")

        observed: list[tuple[str, int, int]] = []
        for index in range(acl_info.AceCount):
            ace_pointer = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise WindowsGuardError("mutex_ace_read_failed")
            if not ace_pointer:
                raise WindowsGuardError("mutex_ace_missing")
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
            if ace.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                raise WindowsGuardError("mutex_ace_type_rejected")
            ace_address = ace_pointer.value
            if ace_address is None:
                raise WindowsGuardError("mutex_ace_missing")
            sid_pointer = ctypes.c_void_p(ace_address + 8)
            ace_sid = _sid_to_string(advapi32, sid_pointer, kernel32)
            observed.append((ace_sid, int(ace.Mask), int(ace.Header.AceFlags)))

        expected = {
            (user_sid, _MUTEX_ACCESS, 0),
            (_SYSTEM_SID, _MUTEX_ACCESS, 0),
        }
        if set(observed) != expected:
            raise WindowsGuardError("mutex_dacl_mismatch")
    finally:
        if descriptor and kernel32.LocalFree(descriptor):
            raise WindowsGuardError("mutex_security_free_failed")


class WindowsWriterMutex:
    """Thread-owned Global mutex with explicit DACL and finite acquisition.

    ``WAIT_ABANDONED`` is returned as ``MutexWaitState.ABANDONED`` and is not
    considered ready. Reconciliation is not implemented in this slice, so an
    abandoned mutex cannot be marked ready. Its owner thread can still release
    it to clean up the native handle without authorizing Workflow execution.
    """

    def __init__(self, state_root: str | os.PathLike[str], timeout_ms: int = 5_000) -> None:
        _require_windows()
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise WindowsGuardError("invalid_timeout")
        if timeout_ms < 0 or timeout_ms >= 0xFFFFFFFF:
            raise WindowsGuardError("invalid_timeout")
        self.state_root = _canonical_state_root(state_root)
        self.user_sid = current_user_sid()
        self.identity = mutex_identity(self.state_root, self.user_sid)
        self.timeout_ms = timeout_ms
        self.wait_state: MutexWaitState | None = None
        self._handle: wintypes.HANDLE | None = None
        self._owner_thread_id: int | None = None

    @property
    def needs_reconciliation(self) -> bool:
        return self.wait_state is MutexWaitState.ABANDONED

    @property
    def is_ready(self) -> bool:
        return self.wait_state is MutexWaitState.ACQUIRED

    def acquire(self) -> MutexWaitState:
        if self._handle is not None:
            raise WindowsGuardError("mutex_already_open")
        kernel32, advapi32 = _load_apis()
        with _security_attributes(advapi32, kernel32, self.user_sid) as attributes:
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexExW(
                ctypes.byref(attributes),
                self.identity.name,
                0,
                _MUTEX_ACCESS,
            )
            create_error = ctypes.get_last_error()
        if not handle:
            # ERROR_INVALID_HANDLE also covers a same-name non-mutex object;
            # never fall back to another namespace or synchronization type.
            raise WindowsGuardError("mutex_create_failed")
        self._handle = handle
        try:
            if create_error not in (0, _ERROR_ALREADY_EXISTS):
                raise WindowsGuardError("mutex_create_status_invalid")
            _verify_mutex_security(advapi32, kernel32, handle, self.user_sid)
            ctypes.set_last_error(0)
            wait_result = int(kernel32.WaitForSingleObject(handle, self.timeout_ms))
            if wait_result == _WAIT_OBJECT_0:
                self.wait_state = MutexWaitState.ACQUIRED
            elif wait_result == _WAIT_ABANDONED:
                self.wait_state = MutexWaitState.ABANDONED
            elif wait_result == _WAIT_TIMEOUT:
                raise WindowsGuardError("mutex_wait_timeout")
            elif wait_result == _WAIT_FAILED:
                raise WindowsGuardError("mutex_wait_failed")
            else:
                raise WindowsGuardError("mutex_wait_unexpected")
            self._owner_thread_id = int(kernel32.GetCurrentThreadId())
            return self.wait_state
        except BaseException:
            if self.wait_state is None:
                self._close_unowned_handle(kernel32)
            raise

    def mark_reconciled(self) -> None:
        """Fail closed because I2 does not implement reconciliation."""
        raise WindowsGuardError("mutex_reconciliation_unavailable")

    def release(self) -> None:
        if self._handle is None or self.wait_state is None:
            raise WindowsGuardError("mutex_not_owned")
        kernel32, _ = _load_apis()
        if self._current_thread_id() != self._owner_thread_id:
            raise WindowsGuardError("mutex_wrong_owner_thread")
        if not kernel32.ReleaseMutex(self._handle):
            raise WindowsGuardError("mutex_release_failed")
        self.wait_state = None
        self._owner_thread_id = None
        handle, self._handle = self._handle, None
        if not kernel32.CloseHandle(handle):
            raise WindowsGuardError("mutex_handle_close_failed")

    def close(self) -> None:
        if self._handle is None:
            return
        if self.wait_state is not None:
            self.release()
            return
        kernel32, _ = _load_apis()
        handle, self._handle = self._handle, None
        if not kernel32.CloseHandle(handle):
            raise WindowsGuardError("mutex_handle_close_failed")

    def _current_thread_id(self) -> int:
        kernel32, _ = _load_apis()
        return int(kernel32.GetCurrentThreadId())

    def _close_unowned_handle(self, kernel32: Any) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and not kernel32.CloseHandle(handle):
            raise WindowsGuardError("mutex_handle_close_failed")

    def __enter__(self) -> WindowsWriterMutex:
        state = self.acquire()
        if state is MutexWaitState.ABANDONED:
            self.release()
            raise WindowsGuardError("mutex_reconciliation_required")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = [
    "MutexIdentity",
    "MutexWaitState",
    "WindowsGuardError",
    "WindowsWriterMutex",
    "current_user_sid",
    "mutex_identity",
]
