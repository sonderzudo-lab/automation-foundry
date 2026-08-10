"""Cooperative named-event shutdown for supervised Windows host processes."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from threading import Event, Thread
from typing import Any

STOP_EVENT_ENV = "AUTOMATION_FOUNDRY_STOP_EVENT"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


@dataclass(slots=True)
class StopEventWatcher:
    """Owned event handle and cancellable watcher thread."""

    kernel32: Any
    handle: int
    cancelled: Event
    thread: Thread

    def close(self) -> None:
        self.cancelled.set()
        self.thread.join(timeout=1)
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = 0


def start_stop_event_watcher(callback: Callable[[], None]) -> StopEventWatcher | None:
    """Start a daemon watcher only inside a supervisor-owned Windows child."""
    event_name = os.getenv(STOP_EVENT_ENV)
    if sys.platform != "win32" or not event_name:
        return None
    if not _valid_event_name(event_name):
        raise ValueError("runtime stop event name is invalid")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateEventW(None, True, False, event_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    cancelled = Event()

    def watch() -> None:
        while not cancelled.is_set():
            if int(kernel32.WaitForSingleObject(handle, 200)) == _WAIT_OBJECT_0:
                callback()
                return

    thread = Thread(target=watch, name="foundry-stop-event", daemon=True)
    thread.start()
    return StopEventWatcher(kernel32, int(handle), cancelled, thread)


def signal_stop_event(event_name: str) -> bool:
    """Set one exact allowlisted child stop event if its process created it."""
    if sys.platform != "win32" or not _valid_event_name(event_name):
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, event_name)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def _valid_event_name(event_name: str) -> bool:
    prefix = "Local\\AutomationFoundryRuntimeChildStop-"
    return event_name.startswith(prefix) and len(event_name) <= 200
