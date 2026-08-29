from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class HardwareRequirement:
    os: str
    gpu_vendor: str
    min_vram_gb: int
    recommended_vram_gb: int


@dataclass(frozen=True)
class Profile:
    schema_version: int
    id: str
    name: str
    description: str
    hardware: HardwareRequirement
    raw: dict[str, Any]


class ProfileError(ValueError):
    pass


def load_profile(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: profile root must be a mapping")
    required = ("schema_version", "id", "name", "hardware")
    missing = [k for k in required if k not in data]
    if missing:
        raise ProfileError(f"{path}: missing required fields: {', '.join(missing)}")
    hw = data["hardware"]
    for key in ("os", "gpu_vendor", "min_vram_gb", "recommended_vram_gb"):
        if key not in hw:
            raise ProfileError(f"{path}: hardware.{key} is required")
    return Profile(
        int(data["schema_version"]),
        str(data["id"]),
        str(data["name"]),
        str(data.get("description", "")),
        HardwareRequirement(
            str(hw["os"]),
            str(hw["gpu_vendor"]),
            int(hw["min_vram_gb"]),
            int(hw["recommended_vram_gb"]),
        ),
        data,
    )


def load_profiles(directory: Path) -> list[Profile]:
    return [load_profile(p) for p in sorted(directory.glob("*.yaml"))]
