from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import cast

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


def process_parent_pid(process_id: int) -> int | None:
    if os.name != "nt":
        raise RuntimeError("PoC process identity checks require Windows")
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            if int(entry.th32ProcessID) == process_id:
                return int(entry.th32ParentProcessID)
            has_entry = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return None
    finally:
        _kernel32.CloseHandle(snapshot)


def _open_process(process_id: int, access: int) -> wintypes.HANDLE | None:
    if os.name != "nt":
        raise RuntimeError("PoC process identity checks require Windows")
    if process_id < 1:
        return None
    handle = _kernel32.OpenProcess(access, False, process_id)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no process owns this PID.
            return None
        raise ctypes.WinError(error)
    return cast(wintypes.HANDLE, handle)


def _creation_time(handle: wintypes.HANDLE) -> int:
    creation = _FileTime()
    exit_time = _FileTime()
    kernel_time = _FileTime()
    user_time = _FileTime()
    if not _kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)


def _image_path(handle: wintypes.HANDLE) -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _is_active_handle(handle: wintypes.HANDLE) -> bool:
    status = int(_kernel32.WaitForSingleObject(handle, 0))
    if status == _WAIT_OBJECT_0:
        return False
    if status == _WAIT_TIMEOUT:
        return True
    raise ctypes.WinError(ctypes.get_last_error())


def get_process_identity(
    process_id: int, *, include_image_path: bool = True
) -> dict[str, int | str | bool | None] | None:
    handle = _open_process(process_id, _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE)
    if handle is None:
        return None
    try:
        active = _is_active_handle(handle)
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        image_path = None
        if active and include_image_path:
            try:
                image_path = _image_path(handle)
            except OSError:
                if _is_active_handle(handle):
                    raise
                active = False
        return {
            "pid": process_id,
            "parent_pid": process_parent_pid(process_id) if active else None,
            "creation_time_100ns": _creation_time(handle),
            "image_path": image_path,
            "active": active,
            "exit_code": int(exit_code.value),
        }
    finally:
        _kernel32.CloseHandle(handle)


def process_is_active(
    process_id: int,
    *,
    expected_creation_time_100ns: int,
) -> bool:
    identity = get_process_identity(process_id, include_image_path=False)
    if identity is None:
        return False
    if identity["creation_time_100ns"] != expected_creation_time_100ns:
        raise RuntimeError(f"PID {process_id} was reused after the recorded process exited")
    if not identity["active"]:
        return False
    return bool(identity["active"])


def terminate_spawned_python(
    process_id: int,
    expected_parent_pid: int,
    *,
    expected_creation_time_100ns: int,
    expected_image_path: str,
) -> None:
    """Terminate one verified Python child of the Popen launcher, by its own handle."""
    if os.name != "nt":
        raise RuntimeError("PoC process termination requires Windows")
    actual_parent_pid = process_parent_pid(process_id)
    if actual_parent_pid != expected_parent_pid and process_id != expected_parent_pid:
        raise RuntimeError(
            f"refusing to terminate PID {process_id}: parent {actual_parent_pid} "
            f"does not match Popen PID {expected_parent_pid}"
        )
    handle = _open_process(
        process_id,
        _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
    )
    if handle is None:
        return
    try:
        identity = get_process_identity(process_id)
        if identity is None or not identity["active"]:
            return
        if identity["parent_pid"] != expected_parent_pid and process_id != expected_parent_pid:
            raise RuntimeError(
                f"refusing to terminate PID {process_id}: live parent "
                f"{identity['parent_pid']} differs from Popen PID {expected_parent_pid}"
            )
        try:
            image_path = _image_path(handle)
        except OSError:
            if not _is_active_handle(handle):
                return
            raise
        if image_path.casefold() != expected_image_path.casefold():
            raise RuntimeError(
                f"refusing to terminate PID {process_id}: executable identity changed"
            )
        if _creation_time(handle) != expected_creation_time_100ns:
            raise RuntimeError(f"refusing to terminate PID {process_id}: creation time changed")
        if image_path.rsplit("\\", 1)[-1].casefold() != "python.exe":
            raise RuntimeError(f"refusing to terminate non-Python process PID {process_id}")
        exit_code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _is_active_handle(handle):
            return
        if not _kernel32.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(handle)
