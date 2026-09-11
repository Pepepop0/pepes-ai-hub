"""A ledger of the temporary directories this harness created.

Every temporary thing the suite makes -- an ephemeral root, a repository
fixture, an external installation -- is created here and forgotten here. That
buys two things a scattered ``mkdtemp`` cannot:

- a test can be *made* to clean up rather than remembered to. Anything still
  outstanding when a test ends is a leak, and a fixture can release it and say
  so, instead of the leak surviving as gigabytes in the user's temp directory;
- the cleanup is provably scoped. Only paths this process created are ever
  removed, by identity, not by matching a prefix against whatever else happens
  to be in the temp directory.

The prefixes exist so a human can recognise the directories, not so anything
here decides what to delete from them.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

# Every prefix the suite creates under, for reporting only.
EPHEMERAL_PREFIX = "pepito-e2e-"
REPO_PREFIX = "pepito-e2e-repo-"
EXTERNAL_PREFIX = "pepito-e2e-external-"

_CREATED: set[Path] = set()


def make(prefix: str) -> Path:
    """Create a tracked temporary directory."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    _CREATED.add(path)
    return path


def forget(path: Path | str) -> None:
    """Stop tracking a path whose owner has released it."""
    _CREATED.discard(Path(path))


def outstanding() -> tuple[Path, ...]:
    """Tracked paths that still exist. Non-empty at the end of a test is a leak."""
    return tuple(sorted(path for path in _CREATED if path.exists()))


def release(paths: Iterable[Path] | None = None) -> tuple[Path, ...]:
    """Remove tracked paths and return what was removed.

    Only ever removes paths this process created, because that is the only set
    it knows about.
    """
    targets = tuple(paths) if paths is not None else outstanding()
    removed: list[Path] = []
    for path in targets:
        if path not in _CREATED:
            raise ValueError(f"{path} was not created by this harness; refusing to remove it")
        shutil.rmtree(path, ignore_errors=True)
        _CREATED.discard(path)
        removed.append(path)
    return tuple(removed)


__all__ = [
    "EPHEMERAL_PREFIX",
    "EXTERNAL_PREFIX",
    "REPO_PREFIX",
    "forget",
    "make",
    "outstanding",
    "release",
]
