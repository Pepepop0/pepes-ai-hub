import asyncio
from pathlib import Path

from local_ai_stack.app import LocalAIStackApp, render_profile_details
from local_ai_stack.profiles import load_profile
from local_ai_stack.system import GPUInfo, SystemFacts

ROOT = Path(__file__).resolve().parents[1]


def test_profile_details_include_compatibility_and_dry_run_plan():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts(
        "Windows",
        "10",
        "3.11.9",
        True,
        True,
        (GPUInfo("NVIDIA GeForce RTX 5070", 12227),),
    )
    details = render_profile_details(profile, facts)
    assert "COMPATIBLE" in details
    assert "12227 MiB" in details
    assert "Install plan · DRY-RUN" in details
    assert "No commands or changes will be executed" in details


def test_profile_details_render_without_nvidia_smi():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, False, ())
    details = render_profile_details(profile, facts)
    assert "INCOMPATIBLE" in details
    assert "nvidia-smi" in details


def test_tui_mounts_without_nvidia_smi():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, False, ())

    async def mount_app() -> None:
        app = LocalAIStackApp(facts=facts, profiles=[profile])
        async with app.run_test() as pilot:
            await pilot.pause()

    asyncio.run(mount_app())
