# server/instance.py
"""Keep one server process per storage directory.

Two server processes that share ``WEBDAV_SHARE_DIR`` (and therefore the DAV
state tables and the auth-cache generation file) would fight over the lock
table and invalidate each other's caches.  ``manage`` commands are fine - they
only touch the database and the generation token - but a second *server* is
almost always a mistake (a manual start next to the systemd unit, or an
overlapping restart).

:func:`acquire` takes a non-blocking ``flock`` on ``<SHARE_DIR>/.server.lock``
and keeps the file descriptor open for the lifetime of the process; the kernel
releases the lock when the process exits, so a crash does not leave a stale
lock behind.  The PID is written to the file for diagnostics.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:  # pragma: no cover - the server targets Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows development
    fcntl = None

logger = logging.getLogger(__name__)

__all__ = ["AlreadyRunningError", "InstanceLock", "acquire"]


class AlreadyRunningError(RuntimeError):
    """Raised when another process already holds the instance lock."""


class InstanceLock:
    """A held lock; call :meth:`release` or use it as a context manager."""

    def __init__(self, path: Path, fd):
        self.path = path
        self._fd = fd

    def __repr__(self):
        return f"InstanceLock({str(self.path)!r})"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


def _read_pid(path: Path):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def acquire(path) -> InstanceLock:
    """Lock ``path`` or raise :class:`AlreadyRunningError`.

    The lock is advisory and per file: it protects against accidentally running
    two servers for the same storage directory, not against a hostile local
    user (who could always start a process with a different ``SHARE_DIR``).
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None:  # pragma: no cover - Windows development
        logger.warning(
            "file locking is not available on this platform; "
            "the single-instance guard is disabled"
        )
        return InstanceLock(path, None)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        pid = _read_pid(path)
        suffix = f", pid {pid}" if pid else ""
        raise AlreadyRunningError(
            f"another server instance is already running for this storage "
            f"directory (lock {path}{suffix})"
        ) from exc

    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        # Diagnostics only: failing to write the PID must not fail startup.
        logger.debug("could not write the PID to %s", path, exc_info=True)
    return InstanceLock(path, fd)
