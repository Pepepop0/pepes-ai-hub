import asyncio
import unicodedata
from pathlib import Path

from textual.widgets import Button, Static

from local_ai_stack.app import LocalAIStackApp, render_profile_details
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.processes import (
    DEFAULT_PROCESS_SPECS,
    RuntimeProcessState,
    RuntimeProcessStatus,
)
from local_ai_stack.profiles import Profile, load_profile, load_profiles
from local_ai_stack.runtime import ServiceRuntime, StackRuntime
from local_ai_stack.services import DEFAULT_SERVICE_SPECS, ServiceState, ServiceStatus
from local_ai_stack.system import GPUInfo, SystemFacts
from local_ai_stack.tui.navigation import SECTION_ITEMS, Section, route_id
from local_ai_stack.tui.widgets import (
    ContextSidebar,
    MainContent,
    PetSlot,
    RightRail,
    TopNavigation,
)

ROOT = Path(__file__).resolve().parents[1]


def reference_facts() -> SystemFacts:
    return SystemFacts(
        "Windows",
        "10",
        "3.11.9",
        True,
        True,
        (GPUInfo("NVIDIA GeForce RTX 5070", 12227),),
    )


def stopped_runtime() -> StackRuntime:
    process_specs = {spec.service_id: spec for spec in DEFAULT_PROCESS_SPECS}
    services = tuple(
        ServiceRuntime(
            ServiceStatus(spec, ServiceState.DOWN, "no local listener", "listener"),
            RuntimeProcessStatus(
                process_specs[spec.id],
                RuntimeProcessState.STOPPED,
                detail="no matching process found",
            ),
            ServiceState.DOWN,
        )
        for spec in DEFAULT_SERVICE_SPECS
    )
    return StackRuntime(services, ServiceState.DOWN)


def all_profiles() -> list[Profile]:
    return load_profiles(ROOT / "profiles")


def make_app(
    *, profiles: list[Profile] | None = None, facts: SystemFacts | None = None
) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=facts or reference_facts(),
        profiles=profiles or all_profiles(),
        runtime_snapshot=stopped_runtime(),
    )


def test_profile_details_include_compatibility_and_ascii_dry_run_plan():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    details = render_profile_details(profile, reference_facts())
    assert "COMPATIBLE" in details
    assert "12227 MiB" in details
    assert "Install plan - DRY-RUN" in details
    assert "No commands or changes will be executed" in details
    assert "\u2014" not in details
    assert "\u00b7" not in details


def test_profile_details_render_without_nvidia_smi():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, False, ())
    details = render_profile_details(profile, facts)
    assert "INCOMPATIBLE" in details
    assert "nvidia-smi" in details


def test_top_navigation_has_exactly_three_sections():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            labels = [str(button.label) for button in app.query_one(TopNavigation).query(Button)]
            assert labels == ["Dashboard", "Instalação", "Configurações"]

    asyncio.run(exercise())


def test_section_change_updates_contextual_sidebar_and_default_view():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#nav-installation")
            await pilot.pause()
            assert app.active_section is Section.INSTALLATION
            sidebar = app.query_one(ContextSidebar)
            assert sidebar.active is Section.INSTALLATION
            assert app.query_one("#sidebar-installation").display
            assert not app.query_one("#sidebar-dashboard").display
            assert app.query_one(MainContent).current == route_id(Section.INSTALLATION, "profile")
            labels = [
                str(button.label) for button in app.query_one("#sidebar-installation").query(Button)
            ]
            assert labels == [item.label for item in SECTION_ITEMS[Section.INSTALLATION]]

    asyncio.run(exercise())


def test_sidebar_changes_route_within_current_section():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-plan")
            await pilot.pause()
            content = app.query_one(MainContent)
            assert content.current == route_id(Section.INSTALLATION, "plan")
            plan = str(app.query_one("#view-installation-plan", Static).content)
            assert "Install plan - DRY-RUN" in plan

    asyncio.run(exercise())


def test_right_rail_is_persistent_and_can_collapse():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            rail = app.query_one(RightRail)
            assert not rail.is_collapsed
            assert "Pepe" in str(app.query_one("#rail-profile", Static).content)

            await pilot.click("#right-rail-toggle")
            await pilot.pause()
            assert rail.is_collapsed
            assert str(app.query_one("#right-rail-toggle", Button).label) == "Expand"

            await pilot.click("#nav-settings")
            await pilot.pause()
            assert app.query_one(RightRail) is rail
            assert rail.is_collapsed

            await pilot.press("ctrl+r")
            await pilot.pause()
            assert not rail.is_collapsed

    asyncio.run(exercise())


def test_dashboard_renders_existing_hardware_and_runtime_contracts():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            dashboard = str(app.query_one("#view-dashboard-status", Static).content)
            assert "NVIDIA GeForce RTX 5070" in dashboard
            assert "VRAM 12227 MiB" in dashboard
            for service in ("Ollama", "Open WebUI", "ComfyUI"):
                assert service in dashboard
            assert dashboard.count("DOWN") >= 3
            assert dashboard.count("STOPPED") == 3

    asyncio.run(exercise())


def test_profile_selection_updates_compatibility_plan_and_right_rail():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.click("#nav-installation")
            await pilot.click("#profile-capella")
            await pilot.pause()
            details = str(app.query_one("#profile-details", Static).content)
            assert "INCOMPATIBLE" in details
            assert "Install plan - DRY-RUN" in details
            assert str(app.query_one("#rail-profile", Static).content) == "Capella"

            await pilot.click("#profile-pepe")
            await pilot.pause()
            details = str(app.query_one("#profile-details", Static).content)
            assert "[bold green]COMPATIBLE[/bold green]" in details
            assert str(app.query_one("#rail-profile", Static).content) == "Pepe"

    asyncio.run(exercise())


def test_ui_does_not_expose_real_user_name_and_pet_slot_is_text_only():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            text = " ".join(str(widget.content) for widget in app.query(Static))
            labels = " ".join(str(button.label) for button in app.query(Button))
            assert "Pedro" not in f"{text} {labels}"
            assert "Profile" in text and "Pepe" in text
            assert not any(
                unicodedata.category(character) == "So" for character in f"{text} {labels}"
            )
            pet = app.query_one(PetSlot)
            assert "backend not connected" in str(pet.content)
            pet.set_pet_text("Pet backend ready")
            assert str(pet.content) == "Pet backend ready"

    asyncio.run(exercise())


def test_tui_mounts_without_nvidia_smi():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    facts = SystemFacts("Windows", "10", "3.11.9", True, False, ())

    async def mount_app() -> None:
        app = make_app(profiles=[profile], facts=facts)
        async with app.run_test(size=(70, 24)) as pilot:
            await pilot.pause()
            assert app.query_one(RightRail)

    asyncio.run(mount_app())


def test_opening_tui_does_not_create_stack_layout(tmp_path, monkeypatch):
    stack_root = tmp_path / "stack-must-not-exist"
    monkeypatch.setenv(ENV_ROOT, str(stack_root))

    async def mount_app() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.pause()

    asyncio.run(mount_app())
    assert not stack_root.exists()
