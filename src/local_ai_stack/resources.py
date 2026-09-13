"""Where the shipped, read-only data lives: profiles, manifests, workflows, assets.

The program runs in two shapes and must find the same files in both:

- a source checkout, where these directories sit beside ``src/`` and
  ``pyproject.toml``;
- a frozen Windows build, where PyInstaller copies them into the bundle's
  private directory and tells us where that is through ``sys._MEIPASS``.

Nothing here depends on the working directory, and nothing here is ever
written to. State goes through :mod:`layout`; this module only answers "where
was the product's data put?". Anything that used to walk up from ``__file__``
looking for ``pyproject.toml`` asks here instead, because inside a bundle there
is no ``pyproject.toml`` above ``__file__`` to find.

``PEPITOS_AI_HUB_RESOURCES`` overrides the root for tests and for running a
build against a checkout's data without rebuilding.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_RESOURCE_ROOT = "PEPITOS_AI_HUB_RESOURCES"

# Every directory the running product reads. The build script bundles exactly
# this list, and the packaged smoke test checks exactly this list.
RESOURCE_DIRECTORIES: tuple[str, ...] = ("profiles", "manifests", "workflows", "assets")

# Files whose absence means a resource directory is not the product's own.
RESOURCE_SENTINELS: dict[str, str] = {
    "profiles": "capella.yaml",
    "manifests": "artifacts.yaml",
    "workflows": "flux2-klein-t2i-api.json",
    "assets": "themes",
}


def is_frozen() -> bool:
    """True inside a PyInstaller build, where ``sys.executable`` is the product itself."""
    return bool(getattr(sys, "frozen", False))


def bundle_directory() -> Path | None:
    """The bundle's private directory (``_internal``), or None outside a build."""
    location = getattr(sys, "_MEIPASS", None)
    return Path(location) if location else None


def executable_directory() -> Path:
    """The directory holding the running program: the app folder when frozen."""
    return Path(sys.executable).resolve().parent


def checkout_root() -> Path:
    """The source checkout this module was imported from. Meaningless when frozen."""
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """The directory that contains ``profiles/``, ``manifests/`` and the rest."""
    override = os.environ.get(ENV_RESOURCE_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    bundle = bundle_directory()
    if is_frozen() and bundle is not None:
        return bundle
    return checkout_root()


def resource_directory(name: str) -> Path:
    if name not in RESOURCE_DIRECTORIES:
        raise ValueError(f"not a product resource directory: {name!r}")
    return resource_root() / name


def missing_resources(root: Path | None = None) -> tuple[str, ...]:
    """Which resource directories are absent or not the product's own, at ``root``."""
    base = resource_root() if root is None else Path(root)
    missing: list[str] = []
    for name in RESOURCE_DIRECTORIES:
        directory = base / name
        if not directory.is_dir() or not (directory / RESOURCE_SENTINELS[name]).exists():
            missing.append(name)
    return tuple(missing)


__all__ = [
    "ENV_RESOURCE_ROOT",
    "RESOURCE_DIRECTORIES",
    "RESOURCE_SENTINELS",
    "bundle_directory",
    "checkout_root",
    "executable_directory",
    "is_frozen",
    "missing_resources",
    "resource_directory",
    "resource_root",
]
