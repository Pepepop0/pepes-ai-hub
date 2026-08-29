from __future__ import annotations

from dataclasses import dataclass

from .profiles import Profile
from .system import GPUInfo, SystemFacts

MIN_PYTHON_VERSION = (3, 11)


@dataclass(frozen=True)
class CompatibilityIssue:
    code: str
    message: str


@dataclass(frozen=True)
class CompatibilityReport:
    profile_id: str
    selected_gpu: GPUInfo | None
    warnings: tuple[CompatibilityIssue, ...]
    blockers: tuple[CompatibilityIssue, ...]

    @property
    def compatible(self) -> bool:
        return not self.blockers


def _python_version(value: str) -> tuple[int, int] | None:
    try:
        major, minor, *_ = value.split(".")
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


def evaluate_compatibility(facts: SystemFacts, profile: Profile) -> CompatibilityReport:
    warnings: list[CompatibilityIssue] = []
    blockers: list[CompatibilityIssue] = []

    if facts.os_name.casefold() != profile.hardware.os.casefold():
        blockers.append(
            CompatibilityIssue(
                "os_mismatch",
                f"Profile requires {profile.hardware.os}; detected {facts.os_name}.",
            )
        )

    detected_python = _python_version(facts.python_version)
    if detected_python is None or detected_python < MIN_PYTHON_VERSION:
        blockers.append(
            CompatibilityIssue(
                "python_unsupported",
                f"Python 3.11+ is required; detected {facts.python_version}.",
            )
        )

    if not facts.git_available:
        blockers.append(CompatibilityIssue("git_missing", "Git is required but was not detected."))

    selected_gpu = max(facts.gpus, key=lambda gpu: gpu.vram_mib, default=None)
    if profile.hardware.gpu_vendor == "nvidia" and (
        not facts.nvidia_smi_available or selected_gpu is None
    ):
        blockers.append(
            CompatibilityIssue(
                "nvidia_gpu_missing",
                "An NVIDIA GPU visible to nvidia-smi is required but was not detected.",
            )
        )
    elif selected_gpu is not None:
        available = selected_gpu.vram_mib
        required = profile.hardware.min_vram_mib
        recommended = profile.hardware.recommended_vram_mib
        tolerance = profile.hardware.vram_tolerance_mib
        if available + tolerance < required:
            blockers.append(
                CompatibilityIssue(
                    "vram_below_minimum",
                    f"GPU reports {available} MiB VRAM; profile requires {required} MiB "
                    f"(reporting tolerance: {tolerance} MiB).",
                )
            )
        elif available < required:
            warnings.append(
                CompatibilityIssue(
                    "vram_within_tolerance",
                    f"GPU reports {available} MiB VRAM, accepted within the profile's "
                    f"{tolerance} MiB reporting tolerance.",
                )
            )
        elif available + tolerance < recommended:
            warnings.append(
                CompatibilityIssue(
                    "vram_below_recommended",
                    f"GPU reports {available} MiB VRAM; {recommended} MiB is recommended.",
                )
            )

    return CompatibilityReport(
        profile_id=profile.id,
        selected_gpu=selected_gpu,
        warnings=tuple(warnings),
        blockers=tuple(blockers),
    )
