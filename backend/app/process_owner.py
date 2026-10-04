"""Process birth identity avoids recovering another live CLI's runs on Web startup."""

import os
from pathlib import Path


def birth(pid):
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                *([ctypes.POINTER(wintypes.FILETIME)] * 4),
            ]
            api.GetProcessTimes.restype = wintypes.BOOL
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not api.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(ended),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                if ended.dwLowDateTime or ended.dwHighDateTime:
                    return None
                return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            finally:
                api.CloseHandle(handle)
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rfind(")") + 2 :].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def claim(conn, kind, key):
    conn.execute(
        "INSERT OR REPLACE INTO process_owners VALUES(?,?,?,?)",
        (kind, key, os.getpid(), birth(os.getpid())),
    )


def alive(conn, kind, key):
    row = conn.execute(
        "SELECT pid,birth FROM process_owners WHERE kind=? AND id=?", (kind, key)
    ).fetchone()
    return bool(row and row[1] is not None and birth(row[0]) == row[1])
