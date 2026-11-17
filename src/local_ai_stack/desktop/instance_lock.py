"""One writer at a time: the desktop window, or a CLI command that changes the Hub.

The lock is an exclusive OS lock on a file in the default state folder, not a
pid file: the operating system drops it when the holder exits or crashes, so a
killed window never leaves the Hub locked. Python opens files non-inheritable,
so services the Hub launches never carry the lock past the Hub's own exit.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO

from ..installation import InstallLocation, resolve_installation

LOCK_FILENAME = "desktop.lock"


def instance_lock_path() -> Path:
    """The lock beside the Hub's state, following ``PEPITOS_AI_HUB_ROOT`` when set."""
    return resolve_installation(InstallLocation.default()).layout.state / LOCK_FILENAME


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    """Non-blocking exclusive lock; ``acquire`` answers whether this process holds it."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else instance_lock_path()
        self._handle: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """True once held; False when another window or command holds it."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 -- kept open while the lock is held
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        """Idempotent. The file stays; deleting it would race the next holder."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()
