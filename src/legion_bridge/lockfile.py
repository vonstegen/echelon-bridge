"""Single-host enforcement via fcntl flock on a state file.

Refuses to start a second listener on the same host while the lock
is held. The lock is released on process exit. Crashed listeners
release the lock automatically (kernel reclaims on process death).
"""
from __future__ import annotations

import fcntl
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class ListenerLockError(RuntimeError):
    pass


@dataclass
class ListenerLock:
    path: Path
    worker_id: str
    hostname: str
    pid: int
    _fd: int | None = None

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def acquire(state_root: Path, worker_id: str) -> ListenerLock:
    """Acquire an exclusive lock on `state_root/listener.lock`.

    Raises ListenerLockError if the lock is already held by a live
    process. Writes the holder's identity into the lock file so an
    operator can diagnose stuck locks.
    """
    state_root = Path(state_root)
    path = state_root / "listener.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        # Lock held by another process. Read holder identity if any.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
        except OSError:
            holder = "(unknown)"
        os.close(fd)
        raise ListenerLockError(
            f"listener lock {path} held by: {holder or '(unknown)'}: {e}"
        ) from e
    # Write holder identity
    hostname = socket.gethostname()
    pid = os.getpid()
    payload = f"worker_id={worker_id}\nhostname={hostname}\npid={pid}\n"
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
    return ListenerLock(path=path, worker_id=worker_id, hostname=hostname, pid=pid, _fd=fd)


@contextmanager
def listener_lock(state_root: Path, worker_id: str) -> Iterator[ListenerLock]:
    lock = acquire(state_root, worker_id)
    try:
        yield lock
    finally:
        lock.release()
