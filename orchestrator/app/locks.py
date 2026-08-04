"""Small cross-process file locking primitives."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX is the production target (Docker, Linux, and macOS).
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is process-local.
    fcntl = None  # type: ignore[assignment]

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class LockTimeout(TimeoutError):
    """Raised when a lock could not be acquired within its deadline.

    Distinct from a task or command deadline so callers can report lock
    contention as contention rather than as a generic timeout.
    """


def _local_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def file_lock(path: str | Path, timeout_s: float = 60.0) -> Iterator[None]:
    """Acquire an exclusive process and cross-process lock with a deadline.

    `timeout_s` bounds the total wait. The in-process and cross-process waits
    share one deadline rather than each getting the full budget.
    """

    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout_s)
    local = _local_lock(lock_path)
    if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise LockTimeout(f"timed out acquiring lock: {lock_path}")

    descriptor: int | None = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass

        if fcntl is not None:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"timed out acquiring lock: {lock_path}"
                        ) from exc
                    time.sleep(0.05)
        yield
    finally:
        if descriptor is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)
        local.release()
