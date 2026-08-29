"""Where the stack is installed.

This layer owns one question only: which absolute root does this installation
live under? It maps an install location to a root, and hands that root to
StackLayout, which stays the authority on the paths inside it.

    InstallLocation -> resolve_install_root() -> StackLayout.from_root(root)

StackLayout deliberately learns nothing about default, portable or custom
installs, and nothing about profiles. It receives a root and nothing else.

This is also the only module that knows both the Git checkout and the install
tree exist. Portable mode is the single place where they meet, and it resolves
the workspace at call time instead of storing an absolute path, so a portable
install can be moved or copied without rewriting its configuration.

Resolving a location never creates a directory.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .layout import StackLayout, default_root

PORTABLE_DIR_NAME = "stack"
REPO_MARKERS = ("pyproject.toml", ".git")


class InstallLocationError(ValueError):
    """The requested install location cannot be represented or resolved."""


class InstallLocationMode(StrEnum):
    DEFAULT = "default"
    PORTABLE = "portable"
    CUSTOM = "custom"


def _absolute(root: str | os.PathLike[str], field: str) -> Path:
    expanded = Path(root).expanduser()
    if not expanded.is_absolute():
        raise InstallLocationError(
            f"{field} must be an absolute path, got {str(root)!r}. "
            "A relative path would depend on the current working directory."
        )
    return Path(os.path.normpath(expanded))


@dataclass(frozen=True)
class InstallLocation:
    """Declarative install location. Holds intent, not a resolved path.

    `root` is meaningful for CUSTOM only. DEFAULT and PORTABLE must leave it
    unset: their root is derived at resolve time, which is what keeps a
    portable install relocatable and keeps machine-specific absolute paths out
    of the persisted configuration.
    """

    mode: InstallLocationMode = InstallLocationMode.DEFAULT
    root: Path | None = None

    def __post_init__(self) -> None:
        if self.mode is InstallLocationMode.CUSTOM:
            if self.root is None:
                raise InstallLocationError("custom install location requires an explicit root")
            object.__setattr__(self, "root", _absolute(self.root, "custom root"))
        elif self.root is not None:
            raise InstallLocationError(
                f"{self.mode} install location must not carry an explicit root; "
                "it is derived when the location is resolved"
            )

    # --- construtores ------------------------------------------------------

    @classmethod
    def default(cls) -> InstallLocation:
        return cls(mode=InstallLocationMode.DEFAULT)

    @classmethod
    def portable(cls) -> InstallLocation:
        return cls(mode=InstallLocationMode.PORTABLE)

    @classmethod
    def custom(cls, root: str | os.PathLike[str]) -> InstallLocation:
        return cls(mode=InstallLocationMode.CUSTOM, root=Path(root))

    # --- consultas ---------------------------------------------------------

    @property
    def is_relocatable(self) -> bool:
        """True when the location stores no machine-specific absolute path."""
        return self.root is None

    # --- serializacao ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {"mode": str(self.mode), "root": None if self.root is None else str(self.root)}

    @classmethod
    def from_dict(cls, data: Any, source: str = "<installation>") -> InstallLocation:
        if not isinstance(data, dict):
            raise InstallLocationError(f"{source}: installation must be a mapping")
        unknown = sorted(str(key) for key in data if key not in {"mode", "root"})
        if unknown:
            raise InstallLocationError(f"{source}: unknown fields: {', '.join(unknown)}")
        raw_mode = data.get("mode")
        if not isinstance(raw_mode, str):
            raise InstallLocationError(f"{source}: installation.mode must be a string")
        try:
            mode = InstallLocationMode(raw_mode.strip().lower())
        except ValueError as exc:
            valid = ", ".join(str(m) for m in InstallLocationMode)
            raise InstallLocationError(
                f"{source}: unknown installation.mode {raw_mode!r}; expected one of {valid}"
            ) from exc
        raw_root = data.get("root")
        if raw_root is None:
            return cls(mode=mode)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise InstallLocationError(f"{source}: installation.root must be a non-empty string")
        return cls(mode=mode, root=Path(raw_root))


def detect_repo_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Find the workspace root by walking up to a pyproject.toml or .git marker.

    Used only by portable mode. `.git` is matched as a path, not a directory,
    so linked worktrees (where `.git` is a file) resolve correctly.
    """
    base = Path(start).expanduser() if start is not None else Path(__file__).resolve().parent
    base = Path(os.path.normpath(base if base.is_absolute() else base.resolve()))
    for candidate in (base, *base.parents):
        if any((candidate / marker).exists() for marker in REPO_MARKERS):
            return candidate
    raise InstallLocationError(
        f"no workspace root found at or above {base}: "
        f"expected one of {', '.join(REPO_MARKERS)}. "
        "Portable mode needs a checkout; use default or custom instead."
    )


def resolve_install_root(
    location: InstallLocation,
    *,
    repo_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a location to an absolute root. Creates nothing on disk."""
    if location.mode is InstallLocationMode.CUSTOM:
        assert location.root is not None  # garantido por __post_init__
        return location.root
    if location.mode is InstallLocationMode.PORTABLE:
        base = _absolute(repo_root, "repo root") if repo_root is not None else detect_repo_root()
        return base / PORTABLE_DIR_NAME
    return default_root(env)


@dataclass(frozen=True)
class ResolvedInstallation:
    """A location bound to a concrete root and layout, for this machine.

    Derived state: resolve it, do not persist it. The persisted form is the
    InstallLocation it came from.
    """

    location: InstallLocation
    root: Path
    layout: StackLayout

    @property
    def mode(self) -> InstallLocationMode:
        return self.location.mode


def resolve_installation(
    location: InstallLocation,
    *,
    repo_root: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedInstallation:
    """Resolve a location and build its StackLayout. Creates no directories."""
    root = resolve_install_root(location, repo_root=repo_root, env=env)
    return ResolvedInstallation(location=location, root=root, layout=StackLayout.from_root(root))
