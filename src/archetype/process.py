"""
pidfile helpers — so we can stop the daemon cleanly and avoid zombie ports.

basically: write our pid on start, remove on exit, refuse if already running.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path


def pidfile_path(data_dir: Path) -> Path:
    """where we stash the daemon pid."""
    return data_dir / "daemon.pid"


def read_pid(path: Path) -> int | None:
    """read pid from file, or None if missing/junk."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def is_process_alive(pid: int) -> bool:
    """true if pid exists (and isnt a zombie we cant signal)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # process exists but we cant touch it — treat as alive
        return True
    return True


def already_running(path: Path) -> int | None:
    """
    if pidfile points at a live process, return that pid.

    stale pidfiles get deleted.
    """
    pid = read_pid(path)
    if pid is None:
        return None
    if is_process_alive(pid):
        return pid
    # leftover file from a crash / kill -9
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def write_pidfile(path: Path, pid: int | None = None) -> None:
    """create data dir + write current pid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid or os.getpid()), encoding="utf-8")


def remove_pidfile(path: Path) -> None:
    """best-effort cleanup on shutdown."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def stop_daemon(path: Path, timeout_sec: float = 8.0) -> tuple[bool, str]:
    """
    send SIGTERM to the pid in the pidfile, wait a bit, escalate to SIGKILL.

    returns (ok, message).
    """
    import time

    pid = already_running(path)
    if pid is None:
        remove_pidfile(path)
        return False, "no running daemon found (pidfile missing or stale)"

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pidfile(path)
        return True, f"process {pid} already gone; cleared pidfile"
    except PermissionError as exc:
        return False, f"cannot signal pid {pid}: {exc}"

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            remove_pidfile(path)
            return True, f"stopped daemon pid {pid}"
        time.sleep(0.15)

    # still alive — force it
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        remove_pidfile(path)
        return True, f"stopped daemon pid {pid}"
    except PermissionError as exc:
        return False, f"daemon pid {pid} ignored SIGTERM and cannot SIGKILL: {exc}"

    time.sleep(0.2)
    remove_pidfile(path)
    if is_process_alive(pid):
        return False, f"failed to kill pid {pid}; port may still be held"
    return True, f"force-killed daemon pid {pid}"
