"""One writer per store (D11).

Two writers on one store (tox -p, an agent's nightward_run next to a human's
`nightward run`) interleave their flushes and recomputes; the survivor then
reports behaviors the other clobbered as REMOVED. Every writer - run (pytest +
recompute), approve, reject - holds `<store>/.lock` while it works. The file is
created with O_EXCL and names its holder; a second writer fails fast. A lock
whose holder process is gone (killed run) is taken over.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import NightwardError

LOCK_NAME = ".lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) would TerminateProcess on Windows; ask the kernel instead.
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5   # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259              # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(root: Path | str) -> dict | None:
    """The current holder's record, {} when unreadable, None when unlocked."""
    try:
        text = (Path(root) / LOCK_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return info if isinstance(info, dict) else {}


def _is_stale(info: dict) -> bool:
    # Only a holder on this machine can be checked; an unreadable record may be
    # a writer between create and write - treat it as alive.
    return (info.get("host") == socket.gethostname()
            and isinstance(info.get("pid"), int) and not _pid_alive(info["pid"]))


def _try_create(path: Path, info: dict) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(info, fh)
    return True


@contextmanager
def store_lock(root: Path | str, command: str, token: str | None = None) -> Iterator[None]:
    """Hold the store's writer lock for the duration of the block.

    token identifies the holder (the runner passes its run id, so its own
    pytest child can recognize the lock it flushes under).
    """
    path = Path(root) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": os.getpid(), "host": socket.gethostname(), "command": command,
            "since": datetime.datetime.now().isoformat(timespec="seconds"), "token": token}
    if not _try_create(path, info):
        holder = read_lock(root)
        if holder is None:              # released in between
            acquired = _try_create(path, info)
        else:
            acquired = _is_stale(holder) and _take_over(path, holder, info)
        if not acquired:
            raise NightwardError(_busy_message(Path(root), path, read_lock(root) or holder))
    try:
        yield
    finally:
        if read_lock(root) == info:
            path.unlink(missing_ok=True)


def _take_over(path: Path, holder: dict, info: dict) -> bool:
    # Re-check right before removing: another writer may have taken it over.
    if read_lock(path.parent) != holder:
        return False
    path.unlink(missing_ok=True)
    return _try_create(path, info)


def _busy_message(root: Path, path: Path, holder: dict | None) -> str:
    who = ""
    if holder:
        who = (f": {holder.get('command', '?')} (pid {holder.get('pid', '?')} on "
               f"{holder.get('host', '?')}, since {holder.get('since', '?')})")
    return (f"another nightward process is writing {root}{who}. Wait for it to finish "
            f"and retry; if no nightward process is running, delete {path}")
