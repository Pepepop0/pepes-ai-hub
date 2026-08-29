from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class GPUInfo:
    name: str
    vram_mib: int

    @property
    def vram_mb(self) -> int:
        """Backward-compatible alias; nvidia-smi reports this value in MiB."""
        return self.vram_mib


@dataclass(frozen=True)
class SystemFacts:
    os_name: str
    os_release: str
    python_version: str
    git_available: bool
    nvidia_smi_available: bool
    gpus: tuple[GPUInfo, ...]


def _detect_gpus() -> tuple[GPUInfo, ...]:
    if shutil.which("nvidia-smi") is None:
        return ()
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if r.returncode != 0:
        return ()
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        try:
            name, memory = [x.strip() for x in line.rsplit(",", 1)]
            out.append(GPUInfo(name, int(memory)))
        except (ValueError, TypeError):
            continue
    return tuple(out)


def detect_system() -> SystemFacts:
    return SystemFacts(
        platform.system(),
        platform.release(),
        sys.version.split()[0],
        shutil.which("git") is not None,
        shutil.which("nvidia-smi") is not None,
        _detect_gpus(),
    )
