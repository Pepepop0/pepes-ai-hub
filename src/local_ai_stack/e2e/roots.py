"""Test roots, and the rule about what may be deleted.

Two modes, from the shared contract:

- ``EPHEMERAL`` — a fresh temporary directory the harness owns outright. It is
  removed on cleanup, always, including after an exception.
- ``CONFIGURED`` — a root the caller names. It is used and left alone. Cleanup
  releases processes and reports what it found, but never deletes persistent
  data, because that data is not the harness's to destroy.

The asymmetry is deliberate: a test that forgets to clean an ephemeral root
leaves gigabytes behind, and a test that "cleans" a configured root destroys a
real install.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..installation import InstallLocation, ResolvedInstallation, resolve_installation
from ..test_contracts import TestRootMode
from .tracking import EPHEMERAL_PREFIX, forget, make


class RootError(RuntimeError):
    """A root could not be allocated or released."""


@dataclass
class TestRoot:
    """One managed root for one run, plus the promise about deleting it."""

    mode: TestRootMode
    path: Path
    owned: bool
    created: bool = False
    removed: bool = False
    cleanup_detail: str = ""
    _preexisting: bool = field(default=False, repr=False)

    @property
    def installation(self) -> ResolvedInstallation:
        """The resolved installation for this root. Creates nothing."""
        return resolve_installation(InstallLocation.custom(self.path))

    @property
    def removable(self) -> bool:
        """Only a root this harness created from scratch may be deleted."""
        return self.owned and self.mode is TestRootMode.EPHEMERAL

    def exists(self) -> bool:
        return self.path.exists()

    def release(self) -> str:
        """Remove an ephemeral root; leave a configured one untouched."""
        if not self.removable:
            self.cleanup_detail = (
                f"{self.mode} root left in place at {self.path}; "
                "persistent data is never deleted by the harness"
            )
            return self.cleanup_detail
        if not self.path.exists():
            self.removed = True
            forget(self.path)
            self.cleanup_detail = "ephemeral root was already gone"
            return self.cleanup_detail
        try:
            shutil.rmtree(self.path)
        except OSError as exc:
            self.cleanup_detail = f"could not remove the ephemeral root {self.path}: {exc}"
            raise RootError(self.cleanup_detail) from exc
        self.removed = True
        forget(self.path)
        self.cleanup_detail = f"ephemeral root removed: {self.path}"
        return self.cleanup_detail

    def as_report_data(self) -> dict[str, object]:
        return {
            "mode": str(self.mode),
            "path": str(self.path),
            "owned": self.owned,
            "created": self.created,
            "removed": self.removed,
            "removable": self.removable,
            "preexisting": self._preexisting,
            "cleanup_detail": self.cleanup_detail,
        }


def allocate_root(
    mode: TestRootMode = TestRootMode.EPHEMERAL,
    *,
    configured: Path | str | None = None,
) -> TestRoot:
    """Pick the root for a run without creating the managed layout.

    An ephemeral root gets a fresh temporary directory. A configured root must
    be named explicitly and is never invented, because guessing a persistent
    path is how a test ends up writing into a real installation.
    """
    if mode is TestRootMode.EPHEMERAL:
        if configured is not None:
            raise RootError("an ephemeral root cannot also name a configured path")
        return TestRoot(mode=mode, path=make(EPHEMERAL_PREFIX), owned=True, created=True)

    if configured is None:
        raise RootError("a configured root mode requires an explicit path")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise RootError(f"a configured root must be absolute, got {configured!r}")
    return TestRoot(
        mode=mode,
        path=path,
        owned=False,
        created=False,
        _preexisting=path.exists(),
    )


__all__ = ["EPHEMERAL_PREFIX", "RootError", "TestRoot", "allocate_root"]
