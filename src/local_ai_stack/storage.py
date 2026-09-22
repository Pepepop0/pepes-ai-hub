"""How much a profile will download, and how much room the disk has left.

Two honest numbers for the screen shown before an installation starts. Both
are cheap: the first adds up the sizes the manifests already record, the
second asks the operating system about one volume. Nothing is hashed, nothing
is scanned, and no folder is created to find out.

The download figure is a floor, never a total. A model pinned without a size,
a repository cloned rather than downloaded and the Python packages an
environment resolves at install time are all real bytes this cannot know, so
the number is reported as "at least" and the caller says so.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .artifacts import Artifact
from .models import FileModelSource, ModelSpec
from .profiles import Profile

GIB = 1024**3


@dataclass(frozen=True)
class DownloadEstimate:
    """What a profile is known to need from the network, and what is unknown.

    ``complete`` says every artifact the profile names had a recorded size; it
    is what tells a screen whether to write "at least".
    """

    known_bytes: int
    unknown_items: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unknown_items

    @property
    def known_gib(self) -> float:
        return self.known_bytes / GIB


@dataclass(frozen=True)
class DiskSpace:
    """Free space on the volume a folder lives on, for the nearest folder that exists."""

    root: Path
    measured_at: Path
    free_bytes: int

    @property
    def free_gib(self) -> float:
        return self.free_bytes / GIB


def _existing_ancestor(root: Path) -> Path | None:
    """The folder a not-yet-created root would be created inside of."""
    current = root if root.is_absolute() else root.absolute()
    for candidate in (current, *current.parents):
        if candidate.exists():
            return candidate
    return None


def free_space(root: Path) -> DiskSpace | None:
    """Free bytes on the volume that would hold ``root``. None when it cannot be asked.

    The folder itself need not exist: the nearest existing ancestor is on the
    same volume, which is the number that matters before installing.
    """
    where = _existing_ancestor(Path(root))
    if where is None:
        return None
    try:
        usage = shutil.disk_usage(where)
    except OSError:
        return None
    return DiskSpace(Path(root), where, usage.free)


def model_download_estimate(
    profile: Profile, specs: Mapping[str, ModelSpec] | None = None
) -> DownloadEstimate:
    """What the profile's models are known to weigh.

    A managed file carries its size in the artifact the manifest pins; a model
    pulled from a provider carries the size that provider's manifest records.
    Anything pinned without one is named as unknown rather than guessed at.
    """
    known = 0
    unknown: list[str] = []
    for requirement in profile.model_requirements:
        spec = (specs or {}).get(str(requirement.model_id))
        source = getattr(spec, "source", None)
        artifact: Artifact | None = getattr(source, "artifact", None)
        if isinstance(source, FileModelSource) and artifact is not None and artifact.size_bytes:
            known += artifact.size_bytes
            continue
        size = getattr(source, "size_bytes", None)
        if size:
            known += size
            continue
        unknown.append(str(requirement.model_id))
    return DownloadEstimate(known, tuple(unknown))


def enough_space(estimate: DownloadEstimate, space: DiskSpace | None) -> bool | None:
    """Whether what is known to be needed obviously does not fit. None when unknown.

    Deliberately not a margin: the answer is False only when the bytes the
    manifests already promise exceed the free space, which no amount of
    optimism resolves.
    """
    if space is None or not estimate.known_bytes:
        return None
    return space.free_bytes >= estimate.known_bytes


__all__ = [
    "GIB",
    "DiskSpace",
    "DownloadEstimate",
    "enough_space",
    "free_space",
    "model_download_estimate",
]
