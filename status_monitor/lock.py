"""Single-instance lock. The file stays open for the life of the process."""

from __future__ import annotations

import fcntl
import os

from status_monitor.models import LOCK_FILENAME


def lock_path(log_dir: str) -> str:
    return os.path.join(log_dir, LOCK_FILENAME)


def acquire_lock(log_dir: str):
    """
    Opens in append mode (not 'w') so we never truncate another running
    instance's lock file content before we know whether we actually hold
    the lock. Only truncate + write our own PID after flock succeeds.
    """
    os.makedirs(log_dir, exist_ok=True)
    handle = open(lock_path(log_dir), "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def release_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()
