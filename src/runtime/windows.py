"""Minimal Windows kernel primitives for one local runtime supervisor."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

_MUTEX_NAME = "Local\\AutomationFoundryRuntimeSupervisor"
_STOP_EVENT_NAME = "Local\\AutomationFoundryRuntimeStop"
_JOB_NAME = "Local\\AutomationFoundryRuntimeJob"
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class RuntimeAlreadyRunningError(RuntimeError):
    """Raised when another supervisor owns the named Windows mutex."""


@dataclass(slots=True)
class WindowsRuntimeHandles:
    """Owned Windows handles for the supervisor lifetime."""

    kernel32: Any
    mutex: int
    stop_event: int
    job: int

    def close(self) -> None:
        """Close the job before releasing the singleton mutex."""
        for field in ("stop_event", "job", "mutex"):
            handle = int(getattr(self, field))
            if handle:
                self.kernel32.CloseHandle(handle)
                setattr(self, field, 0)


def acquire_runtime_handles() -> WindowsRuntimeHandles:
    """Acquire singleton, stop event, and kill-on-close job handles."""
    if sys.platform != "win32":
        raise OSError("runtime supervision is supported only on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_kernel32(kernel32)
    mutex = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        raise RuntimeAlreadyRunningError("another runtime supervisor is active")

    stop_event = kernel32.CreateEventW(None, True, False, _STOP_EVENT_NAME)
    if not stop_event:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(mutex)
        raise ctypes.WinError(error)
    kernel32.ResetEvent(stop_event)

    job = kernel32.CreateJobObjectW(None, _JOB_NAME)
    if not job:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(stop_event)
        kernel32.CloseHandle(mutex)
        raise ctypes.WinError(error)
    information = _job_limit_information()
    if not kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        kernel32.CloseHandle(stop_event)
        kernel32.CloseHandle(mutex)
        raise ctypes.WinError(error)
    return WindowsRuntimeHandles(
        kernel32=kernel32,
        mutex=int(mutex),
        stop_event=int(stop_event),
        job=int(job),
    )


def assign_process_to_job(handles: WindowsRuntimeHandles, process_handle: int) -> None:
    """Assign one exact child process handle to the kill-on-close job."""
    if not handles.kernel32.AssignProcessToJobObject(handles.job, process_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def wait_for_stop(handles: WindowsRuntimeHandles, *, timeout_ms: int) -> bool:
    """Wait for an explicit stop signal without blocking health monitoring forever."""
    result = handles.kernel32.WaitForSingleObject(handles.stop_event, timeout_ms)
    return int(result) == _WAIT_OBJECT_0


def signal_runtime_stop() -> bool:
    """Signal the live supervisor, returning false when none owns the event."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, _STOP_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def runtime_mutex_is_owned() -> bool:
    """Check singleton ownership without acquiring or changing it."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, _MUTEX_NAME)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _configure_kernel32(kernel32: Any) -> None:
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _job_limit_information() -> Any:
    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return information
