"""Proof that an install belongs to this hub.

Every managed component writes one marker file into its own directory. The
marker is what separates MANAGED from EXTERNAL: without it, files found on disk
are somebody else's or the remains of an interrupted install, and either way
they are not ours to modify.

Two rules make that trustworthy:

- the marker is written **last**, after the component is fully in place, so an
  interrupted install leaves files without a marker and reads as INVALID rather
  than as finished;
- it is written atomically, through a temporary file and a replace, so a
  crash mid-write cannot leave a half-parsed marker that looks valid.

It also records *what* was installed — the pinned source id, the version and
the checksum or commit — so a later run can tell an install that matches the
current pin from one that no longer does.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MARKER_NAME = ".pepito-managed.json"
MARKER_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ManagedMarker:
    """What this hub installed into one directory, and from which pin."""

    component: str
    source_id: str
    version: str | None = None
    checksum: str | None = None
    installed_at: float = 0.0
    schema_version: int = MARKER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "component": self.component,
            "source_id": self.source_id,
            "version": self.version,
            "checksum": self.checksum,
            "installed_at": self.installed_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ManagedMarker | None:
        """Parse a marker, or None when it is unusable.

        Anything unreadable, of another schema, or missing its identity is
        treated as no marker at all. A component then reports INVALID instead
        of trusting a file it cannot understand.
        """
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != MARKER_SCHEMA_VERSION:
            return None
        component = data.get("component")
        source_id = data.get("source_id")
        if not isinstance(component, str) or not isinstance(source_id, str):
            return None
        version = data.get("version")
        checksum = data.get("checksum")
        installed_at = data.get("installed_at")
        return cls(
            component=component,
            source_id=source_id,
            version=version if isinstance(version, str) else None,
            checksum=checksum if isinstance(checksum, str) else None,
            installed_at=float(installed_at) if isinstance(installed_at, (int, float)) else 0.0,
        )


def marker_path(directory: Path) -> Path:
    return Path(directory) / MARKER_NAME


def read_marker(directory: Path, *, component: str | None = None) -> ManagedMarker | None:
    """Read the marker in a directory. None when absent, broken or foreign."""
    try:
        data = json.loads(marker_path(directory).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    marker = ManagedMarker.from_dict(data)
    if marker is None:
        return None
    if component is not None and marker.component != component:
        return None
    return marker


def write_marker(
    directory: Path,
    *,
    component: str,
    source_id: str,
    version: str | None = None,
    checksum: str | None = None,
) -> ManagedMarker:
    """Claim a directory as ours. Call this only once the install is complete."""
    marker = ManagedMarker(
        component=component,
        source_id=source_id,
        version=version,
        checksum=checksum,
        installed_at=time.time(),
    )
    path = marker_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(marker.to_dict(), indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return marker
