from pathlib import Path

import pytest

from local_ai_stack.compatibility import evaluate_compatibility
from local_ai_stack.profiles import load_profile
from local_ai_stack.system import GPUInfo, SystemFacts

ROOT = Path(__file__).resolve().parents[1]


def facts_with_vram(vram_mib: int) -> SystemFacts:
    return SystemFacts(
        os_name="Windows",
        os_release="10",
        python_version="3.11.9",
        git_available=True,
        nvidia_smi_available=True,
        gpus=(GPUInfo("NVIDIA GeForce RTX 5070", vram_mib),),
    )


def test_pepe_accepts_reference_rtx_5070_reported_vram():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    report = evaluate_compatibility(facts_with_vram(12227), profile)
    assert report.compatible
    assert [warning.code for warning in report.warnings] == ["vram_within_tolerance"]


@pytest.mark.parametrize(
    ("vram_mib", "compatible"),
    [(12160, True), (12159, False)],
)
def test_vram_tolerance_has_an_exact_mib_boundary(vram_mib, compatible):
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    assert evaluate_compatibility(facts_with_vram(vram_mib), profile).compatible is compatible


@pytest.mark.parametrize(
    ("gpu", "vram_mib", "compatible"),
    [
        ("NVIDIA GeForce RTX 4060", 8188, True),  # what 8 GB cards report
        ("NVIDIA GeForce RTX 3070", 8192, True),
        ("NVIDIA GeForce RTX 5070", 12227, True),
        ("NVIDIA GeForce RTX 3060", 6144, False),  # the class below
        ("NVIDIA GeForce GTX 1650", 4096, False),
    ],
)
def test_the_8gb_profile_takes_8gb_cards_and_refuses_the_class_below(gpu, vram_mib, compatible):
    profile = load_profile(ROOT / "profiles" / "pepe-8gb.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, True, (GPUInfo(gpu, vram_mib),))
    report = evaluate_compatibility(facts, profile)
    assert report.compatible is compatible
    if not compatible:
        assert "vram_below_minimum" in {blocker.code for blocker in report.blockers}


def test_capella_is_incompatible_on_reference_machine():
    profile = load_profile(ROOT / "profiles" / "capella.yaml")
    report = evaluate_compatibility(facts_with_vram(12227), profile)
    assert not report.compatible
    assert "vram_below_minimum" in {blocker.code for blocker in report.blockers}


def test_missing_nvidia_gpu_is_a_blocker():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, False, ())
    report = evaluate_compatibility(facts, profile)
    assert not report.compatible
    assert "nvidia_gpu_missing" in {blocker.code for blocker in report.blockers}
