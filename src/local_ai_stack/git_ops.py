"""Small, safe Git surface for provisioning.

Every call goes through CommandRunner, so there is no shell, argv is always a
list, timeouts are enforced and logs are sanitized.

Deliberate limits, because this layer will one day run unattended:

- global Git configuration is never touched; nothing here writes `--global`;
- a dirty working tree is never modified silently: checkout and fetch refuse
  it unless the caller explicitly allows it;
- `reset --hard` is never issued, implicitly or otherwise;
- clone refuses a destination that already holds files;
- pinning is explicit: clone and checkout take a revision, and nothing
  auto-updates.

Sophisticated update flows are out of scope. This is the minimum needed to
place a known revision of a repository at a known path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .runner import CommandResult, CommandRunner, OutputSink

GIT = "git"
DEFAULT_GIT_TIMEOUT = 300.0
DEFAULT_REMOTE = "origin"


class GitError(RuntimeError):
    """A Git operation failed."""

    def __init__(self, message: str, result: CommandResult | None = None) -> None:
        super().__init__(message)
        self.result = result


class GitNotAvailableError(GitError):
    """Git is not installed or not on PATH."""


class GitCommandError(GitError):
    """A Git command exited non-zero."""


class DirtyWorkingTreeError(GitError):
    """The repository has uncommitted changes and was left untouched."""


@dataclass(frozen=True)
class GitStatus:
    """What a path is, from Git's point of view."""

    path: Path
    is_repository: bool
    head: str | None = None
    remote_url: str | None = None
    dirty: bool | None = None


class GitClient:
    """Structured Git operations. Never mutates a repository implicitly."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        timeout: float = DEFAULT_GIT_TIMEOUT,
        dry_run: bool = False,
    ) -> None:
        self.runner = runner if runner is not None else CommandRunner(default_timeout=timeout)
        self.timeout = timeout
        self.dry_run = dry_run

    # --- infraestrutura ----------------------------------------------------

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        dry_run: bool | None = None,
        check: bool = False,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        is_dry = self.dry_run if dry_run is None else dry_run
        result = self.runner.run(
            [GIT, *args],
            cwd=cwd,
            timeout=self.timeout,
            dry_run=is_dry,
            on_output=on_output,
        )
        if result.not_found:
            raise GitNotAvailableError("git was not found on PATH", result)
        if check and not result.ok:
            raise GitCommandError(
                f"git {' '.join(args[:2])} failed: {result.stderr.strip()}", result
            )
        return result

    def available(self) -> bool:
        return self.runner.which(GIT) is not None

    def require_available(self) -> None:
        if not self.available():
            raise GitNotAvailableError("git was not found on PATH")

    def version(self) -> str | None:
        result = self._run(["--version"], dry_run=False)
        if not result.ok:
            return None
        text = result.stdout.strip()
        return text.split()[2] if text.lower().startswith("git version") else None

    # --- consultas read-only ----------------------------------------------

    def is_repository(self, path: Path) -> bool:
        """True when `path` is the working tree of a Git repository."""
        target = Path(path)
        if not target.is_dir():
            return False
        result = self._run(["rev-parse", "--is-inside-work-tree"], cwd=target, dry_run=False)
        return result.ok and result.stdout.strip().lower() == "true"

    def head(self, path: Path) -> str:
        """The full commit SHA currently checked out."""
        result = self._run(["rev-parse", "HEAD"], cwd=Path(path), dry_run=False, check=True)
        return result.stdout.strip()

    def remote_url(self, path: Path, remote: str = DEFAULT_REMOTE) -> str | None:
        result = self._run(["remote", "get-url", remote], cwd=Path(path), dry_run=False)
        return result.stdout.strip() if result.ok else None

    def is_dirty(self, path: Path) -> bool:
        """True when the working tree has staged or unstaged changes."""
        result = self._run(
            ["status", "--porcelain", "--untracked-files=normal"],
            cwd=Path(path),
            dry_run=False,
            check=True,
        )
        return bool(result.stdout.strip())

    def status(self, path: Path, remote: str = DEFAULT_REMOTE) -> GitStatus:
        """One read-only snapshot. Never raises for a non-repository path."""
        target = Path(path)
        if not self.is_repository(target):
            return GitStatus(path=target, is_repository=False)
        head = self._run(["rev-parse", "HEAD"], cwd=target, dry_run=False)
        dirty = self._run(["status", "--porcelain"], cwd=target, dry_run=False)
        return GitStatus(
            path=target,
            is_repository=True,
            head=head.stdout.strip() if head.ok else None,
            remote_url=self.remote_url(target, remote),
            dirty=bool(dirty.stdout.strip()) if dirty.ok else None,
        )

    # --- mutacoes ----------------------------------------------------------

    def clone(
        self,
        url: str,
        destination: Path,
        *,
        revision: str | None = None,
        depth: int | None = None,
        dry_run: bool | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Clone into an explicit destination, optionally pinned to a revision.

        Refuses a destination that already contains files, so an existing
        checkout is never clobbered. ``--progress`` forces Git to report
        transfer progress even though output is captured rather than a real
        terminal; ``on_output`` sees it live, one line at a time, when given.
        """
        target = Path(destination)
        is_dry = self.dry_run if dry_run is None else dry_run
        if target.exists() and not target.is_dir():
            raise GitError(f"clone destination is a file: {target}")
        if target.is_dir() and any(target.iterdir()):
            raise GitError(
                f"clone destination is not empty: {target}. "
                "Refusing to overwrite an existing directory."
            )

        args = ["clone", "--progress"]
        if depth is not None:
            if depth < 1:
                raise GitError("depth must be >= 1")
            if revision is not None:
                raise GitError(
                    "a shallow clone cannot be pinned by revision here; "
                    "clone fully and check the revision out, or pin by branch"
                )
            args += ["--depth", str(depth)]
        args += ["--", url, str(target)]

        result = self._run(args, dry_run=is_dry, check=not is_dry, on_output=on_output)
        if revision is not None and not is_dry:
            self.checkout(target, revision)
        return result

    def checkout(
        self,
        path: Path,
        revision: str,
        *,
        allow_dirty: bool = False,
        dry_run: bool | None = None,
    ) -> CommandResult:
        """Check out an explicit commit, tag or branch. Refuses a dirty tree."""
        target = Path(path)
        if not revision.strip():
            raise GitError("revision must not be empty")
        is_dry = self.dry_run if dry_run is None else dry_run
        if not is_dry:
            self._require_clean(target, "checkout", allow_dirty)
        return self._run(
            ["checkout", "--detach", revision], cwd=target, dry_run=is_dry, check=not is_dry
        )

    def fetch(
        self,
        path: Path,
        *,
        remote: str = DEFAULT_REMOTE,
        allow_dirty: bool = False,
        dry_run: bool | None = None,
    ) -> CommandResult:
        """Fetch from a remote. Never merges, rebases or resets."""
        target = Path(path)
        is_dry = self.dry_run if dry_run is None else dry_run
        if not is_dry:
            self._require_clean(target, "fetch", allow_dirty)
        return self._run(
            ["fetch", "--tags", "--prune", remote], cwd=target, dry_run=is_dry, check=not is_dry
        )

    def _require_clean(self, path: Path, operation: str, allow_dirty: bool) -> None:
        if allow_dirty:
            return
        if self.is_dirty(path):
            raise DirtyWorkingTreeError(
                f"refusing to {operation} in {path}: the working tree has uncommitted changes. "
                "Commit, stash or discard them first."
            )


def clone_argv(url: str, destination: Path, *, depth: int | None = None) -> list[str]:
    """The exact argv a clone would use. Useful for plans and for tests."""
    args = [GIT, "clone", "--progress"]
    if depth is not None:
        args += ["--depth", str(depth)]
    return [*args, "--", url, str(Path(os.fspath(destination)))]


# --- observacao de clone -----------------------------------------------------

_RECEIVING_RE = re.compile(r"Receiving objects:\s+(\d+)%\s+\((\d+)/(\d+)\)")
_RESOLVING_RE = re.compile(r"Resolving deltas:\s+(\d+)%\s+\((\d+)/(\d+)\)")


@dataclass(frozen=True)
class GitCloneEvent:
    """One sanitized fact parsed from Git's own ``--progress`` output.

    The percentage is Git's own count of objects or deltas, never a guess:
    with ``--progress`` a clone always reports a real total, so there is no
    "unknown total" case here the way there is for an HTTP download.
    """

    action: str  # "receiving" | "resolving"
    completed: int
    total: int
    percent: int


GitCloneObserver = Callable[[GitCloneEvent], None]


class GitCloneOutputParser:
    """Turns one ``git clone --progress`` invocation's stderr into events."""

    def __init__(self, on_event: GitCloneObserver) -> None:
        self._on_event = on_event

    def feed(self, _stream: str, line: str) -> None:
        text = line.strip()
        match = _RECEIVING_RE.search(text)
        if match:
            percent, completed, total = (int(match.group(i)) for i in (1, 2, 3))
            self._on_event(GitCloneEvent("receiving", completed, total, percent))
            return
        match = _RESOLVING_RE.search(text)
        if match:
            percent, completed, total = (int(match.group(i)) for i in (1, 2, 3))
            self._on_event(GitCloneEvent("resolving", completed, total, percent))
