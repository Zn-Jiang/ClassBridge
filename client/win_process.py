"""Cheap process lookups on Windows.

``psutil.process_iter(["name"])`` costs ~1.2 s on a normal desktop because it
opens every process to read its name.  Polling that every five seconds (the
ClassIsland watchdog) kept the interpreter busy in long bursts and made the UI
stutter — most visibly while dragging the window.

The Win32 tool-help snapshot returns every process name in a single pass, so
the same question is answered in a few milliseconds.  ``psutil`` stays the
fallback for non-Windows platforms and for anything this module cannot do.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("kg.client.win_process")

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH = 260

_windows_ready = False
_kernel32 = None
_ERROR_NO_MORE_FILES = 18


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def _prepare() -> bool:
    """Bind the kernel32 functions once; returns False when unavailable."""
    global _windows_ready, _kernel32
    if _windows_ready:
        return True
    if sys.platform != "win32":
        return False

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = ctypes.c_int
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Win32 process snapshot unavailable: %s", exc)
        return False

    _kernel32 = kernel32
    _windows_ready = True
    return True


def list_process_names() -> Optional[List[Tuple[int, str]]]:
    """``[(pid, exe_name), …]`` for every process, or ``None`` on failure.

    One snapshot pass; typically a few milliseconds for hundreds of processes.
    """
    if not _prepare():
        return None

    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        logger.debug("CreateToolhelp32Snapshot failed (err=%s)", ctypes.get_last_error())
        return None

    entries: List[Tuple[int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            logger.debug("Process32FirstW failed (err=%s)", ctypes.get_last_error())
            return None
        while True:
            entries.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return entries


def find_pid_by_name(name: str) -> Optional[int]:
    """First PID whose executable name matches *name* (case-insensitive)."""
    return find_pid_by_names([name])


def find_pid_by_names(names: List[str]) -> Optional[int]:
    """First PID matching any of *names* (case-insensitive)."""
    wanted = {str(item).strip().lower() for item in names if str(item).strip()}
    if not wanted:
        return None
    entries = list_process_names()
    if entries is None:
        return None
    for pid, exe_name in entries:
        if exe_name.strip().lower() in wanted:
            return pid
    return None


def find_pid_by_prefix(prefix: str) -> Optional[Tuple[int, str]]:
    """First ``(pid, name)`` whose executable name starts with *prefix*."""
    needle = str(prefix).strip().lower()
    if not needle:
        return None
    entries = list_process_names()
    if entries is None:
        return None
    for pid, exe_name in entries:
        if exe_name.strip().lower().startswith(needle):
            return pid, exe_name
    return None


def available() -> bool:
    """Whether the fast Win32 path can be used on this machine."""
    return _prepare()


__all__ = [
    "available",
    "find_pid_by_name",
    "find_pid_by_names",
    "find_pid_by_prefix",
    "list_process_names",
]
