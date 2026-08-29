import asyncio
import unicodedata
from pathlib import Path

from textual.widgets import Button, Input, Select, Static

from local_ai_stack.app import LocalAIStackApp, project_root, render_profile_details
from local_ai_stack.config import GeneralConfig, InstalledConfig, UiConfig
from local_ai_stack.execution import PlanExecutor
from local_ai_stack.installation import (
    PORTABLE_DIR_NAME,
    InstallLocation,
    InstallLocationMode,
    resolve_installation,
)
from local_ai_stack.integration import ComponentIntegration, IntegrationSnapshot
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.pets import PetState
from local_ai_stack.processes import (
    DEFAULT_PROCESS_SPECS,
    RuntimeProcessState,
    RuntimeProcessStatus,
)
from local_ai_stack.profiles import Profile, load_profile, load_profiles
from local_ai_stack.provisioning import (
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    RuntimeHandoff,
)
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


def integration_snapshot(
    availability: LaunchAvailability = LaunchAvailability.NOT_INSTALLED,
    *,
    ownership: Ownership = Ownership.NONE,
    executable: Path | None = None,
    detected_path: Path | None = None,
    detail: str = "Ollama is not installed, so there is nothing to start.",
) -> IntegrationSnapshot:
    """A provisioning verdict stated by the test, never read off this machine."""
    return IntegrationSnapshot(
        (
            ComponentIntegration(
                handoff=RuntimeHandoff(
                    component_id="ollama",
                    availability=availability,
                    ownership=ownership,
                    executable=executable,
                    detected_path=detected_path,
                    detail=detail,
                ),
                service_id="ollama",
            ),
        )
    )


def fake_executor_factory(installation):
    """A plan executor with no real provisioner behind it."""
    return PlanExecutor(context=ProvisionContext(installation=installation), provisioners={})


def make_app(
    *,
    profiles: list[Profile] | None = None,
    facts: SystemFacts | None = None,
    installed_config: InstalledConfig | None = None,
    integration: IntegrationSnapshot | None = None,
    executor_factory=fake_executor_factory,
) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=facts or reference_facts(),
        profiles=profiles or all_profiles(),
        runtime_snapshot=stopped_runtime(),
        installed_config=installed_config,
        integration=integration if integration is not None else integration_snapshot(),
        executor_factory=executor_factory,
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
            plan = str(app.query_one("#install-plan-steps", Static).content)
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
            assert pet.animator is not None
            assert pet.animator.pet.id == "claw"
            assert pet.animator.state is PetState.SLEEPING
            assert "(\\/)" in str(pet.content)
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


def test_install_location_modes_resolve_for_preview_without_creating_directories(tmp_path):
    custom_root = tmp_path / "custom-stack"
    # Derived, never spelled out: the portable root is whatever workspace the
    # app resolved plus PORTABLE_DIR_NAME, so this passes in a checkout or in a
    # worktree under any directory name.
    portable_root = resolve_installation(InstallLocation.portable(), repo_root=project_root()).root
    assert portable_root == project_root() / PORTABLE_DIR_NAME
    portable_tail = str(Path(portable_root.parent.name) / portable_root.name)

    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-location")
            selector = app.query_one("#installation-location-mode", Select)
            preview = app.query_one("#installation-location-preview", Static)

            selector.value = InstallLocationMode.PORTABLE
            await pilot.pause()
            assert app.installed_config.installation == InstallLocation.portable()
            assert str(preview.content).endswith(f"{portable_tail}[/b]")

            selector.value = InstallLocationMode.CUSTOM
            await pilot.pause()
            custom_input = app.query_one("#installation-custom-root", Input)
            assert custom_input.display
            custom_input.value = str(custom_root)
            await pilot.pause()
            assert app.installed_config.installation == InstallLocation.custom(custom_root)
            assert "custom-stack" in str(preview.content)

            selector.value = InstallLocationMode.DEFAULT
            await pilot.pause()
            assert app.installed_config.installation == InstallLocation.default()
            assert not custom_input.display

    asyncio.run(exercise())
    assert not custom_root.exists()
    assert not portable_root.exists()


def test_relative_custom_location_shows_inline_error_and_is_not_accepted():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            selector = app.query_one("#installation-location-mode", Select)
            selector.value = InstallLocationMode.CUSTOM
            custom_input = app.query_one("#installation-custom-root", Input)
            custom_input.value = "relative/stack"
            await pilot.pause()
            error = str(app.query_one("#installation-location-error", Static).content)
            assert "Invalid custom location" in error
            assert "absolute path" in error
            assert app.installed_config.installation == InstallLocation.default()

    asyncio.run(exercise())


def test_ui_config_controls_rail_and_settings_views():
    config = InstalledConfig(
        profile_id="capella",
        general=GeneralConfig(
            ui=UiConfig(
                theme="custom-theme",
                pet="custom-pet",
                right_rail_visible=False,
                right_rail_expanded=False,
            )
        ),
    )

    async def exercise() -> None:
        app = make_app(installed_config=config)
        async with app.run_test():
            rail = app.query_one(RightRail)
            assert not rail.display
            assert rail.is_collapsed
            assert app.selected_profile.id == "capella"

            interface = str(app.query_one("#view-settings-interface", Static).content)
            assert "Theme: custom-theme" in interface
            assert "Pet: custom-pet" in interface
            assert "Right rail visible: no" in interface
            assert "Right rail expanded: no" in interface

    asyncio.run(exercise())


def test_capella_missing_pet_is_text_only_and_does_not_reuse_claw():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            await pilot.press("2")
            await pilot.click("#profile-capella")
            await pilot.pause()
            pet = app.query_one(PetSlot)
            assert pet.animator is None
            assert str(pet.content) == "Pet unavailable: hello-kitty"
            assert "(\\/)" not in str(pet.content)
            assert str(app.query_one("#rail-theme", Static).content) == "Capella"
            assert app.theme == "pepitos-capella"

    asyncio.run(exercise())


def test_general_appearance_override_wins_in_tui():
    config = InstalledConfig(
        profile_id="capella",
        general=GeneralConfig(ui=UiConfig(pet="claw", theme="pepe")),
    )

    async def exercise() -> None:
        app = make_app(installed_config=config)
        async with app.run_test():
            pet = app.query_one(PetSlot)
            assert pet.animator is not None
            assert pet.animator.pet.id == "claw"
            assert app.current_appearance.resolved.pet_id == "claw"
            assert app.current_appearance.resolved.theme_id == "pepe"
            assert app.theme == "pepitos-pepe"

    asyncio.run(exercise())


def test_pet_animation_and_state_survive_rail_collapse():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test() as pilot:
            pet = app.query_one(PetSlot)
            rail = app.query_one(RightRail)
            animator = pet.animator
            assert animator is not None
            started_at = animator.started_at

            pet.refresh_frame(started_at)
            first = str(pet.content)
            assert pet.set_state(PetState.SLEEPING, now=started_at + 0.5) is False
            assert animator.started_at == started_at
            pet.refresh_frame(started_at + 1.5)
            assert str(pet.content) != first

            await pilot.click("#right-rail-toggle")
            await pilot.pause()
            assert rail.is_collapsed
            assert pet.animator is animator
            await pilot.press("ctrl+r")
            await pilot.pause()
            assert not rail.is_collapsed
            assert pet.animator is animator

    asyncio.run(exercise())


# --- ownership and plan execution in the UI --------------------------------


def external_snapshot() -> IntegrationSnapshot:
    return integration_snapshot(
        LaunchAvailability.EXTERNAL,
        ownership=Ownership.EXTERNAL,
        detected_path=Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.EXE",
        detail=(
            "Ollama is installed outside this hub. It is observed only: this manager "
            "never starts, stops, replaces or uninstalls it."
        ),
    )


def managed_snapshot(executable: Path) -> IntegrationSnapshot:
    return integration_snapshot(
        LaunchAvailability.AVAILABLE,
        ownership=Ownership.MANAGED,
        executable=executable,
        detected_path=executable,
        detail="Ollama is managed by this hub and can be started.",
    )


def ollama_view(app: LocalAIStackApp) -> str:
    return str(app.query_one(f"#{route_id(Section.DASHBOARD, 'ollama')}", Static).content)


def test_external_ollama_is_shown_as_external_and_not_controllable():
    async def exercise() -> None:
        app = make_app(integration=external_snapshot())
        async with app.run_test() as pilot:
            await pilot.press("1")
            await pilot.click("#side-dashboard-ollama")
            await pilot.pause()
            view = ollama_view(app)
            assert "Install ownership: [yellow]EXTERNAL[/yellow]" in view
            assert "Start / stop / restart: [yellow]not available[/yellow]" in view
            # The reason is shown, not hidden.
            assert "never starts, stops, replaces or uninstalls it" in view
            # And the observed path never carries the real account name.
            assert "%USERPROFILE%" in view
            assert Path.home().name not in view

    asyncio.run(exercise())


def test_managed_ollama_is_shown_as_controllable(tmp_path):
    executable = tmp_path / "apps" / "ollama" / "ollama.exe"

    async def exercise() -> None:
        app = make_app(integration=managed_snapshot(executable))
        async with app.run_test() as pilot:
            await pilot.press("1")
            await pilot.click("#side-dashboard-ollama")
            await pilot.pause()
            view = ollama_view(app)
            assert "Install ownership: [green]MANAGED[/green]" in view
            assert "Start / stop / restart: [green]available[/green]" in view

    asyncio.run(exercise())


def test_service_without_a_provisioner_reports_unknown_ownership():
    async def exercise() -> None:
        app = make_app(integration=external_snapshot())
        async with app.run_test() as pilot:
            await pilot.press("1")
            await pilot.click("#side-dashboard-comfyui")
            await pilot.pause()
            view = str(app.query_one(f"#{route_id(Section.DASHBOARD, 'comfyui')}", Static).content)
            assert "Install ownership: [dim]UNKNOWN[/dim]" in view
            assert "No provisioner owns this component yet" in view

    asyncio.run(exercise())


def test_plan_view_separates_supported_from_unsupported_steps():
    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-plan")
            await pilot.pause()
            plan = str(app.query_one("#install-plan-steps", Static).content)
            assert "SUPPORTED" in plan
            assert "UNSUPPORTED" in plan
            assert "no provisioner is registered for comfyui" in plan
            assert "Executable in this build:" in plan

    asyncio.run(exercise())


def test_opening_the_plan_view_runs_nothing_and_creates_nothing(tmp_path, monkeypatch):
    stack_root = tmp_path / "PepitosAIHub"
    monkeypatch.setenv(ENV_ROOT, str(stack_root))

    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-plan")
            await pilot.pause()
            preview = str(app.query_one("#install-plan-dry-run", Static).content)
            assert "Not run yet" in preview

    asyncio.run(exercise())
    assert not stack_root.exists()


def test_dry_run_button_previews_without_creating_directories(tmp_path, monkeypatch):
    stack_root = tmp_path / "PepitosAIHub"
    monkeypatch.setenv(ENV_ROOT, str(stack_root))

    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-plan")
            await pilot.pause()
            app.query_one("#plan-dry-run", Button).press()
            await pilot.pause()
            preview = str(app.query_one("#install-plan-dry-run", Static).content)
            assert "Dry-run preview" in preview
            assert "No directory was created and no download was started" in preview
            assert "Directories that would be created:" in preview
            assert "Unsupported steps:" in preview

    asyncio.run(exercise())
    assert not stack_root.exists()
    assert not (tmp_path / "downloads").exists()


def test_changing_the_install_location_rebuilds_the_plan_and_drops_the_preview(tmp_path):
    custom_root = tmp_path / "elsewhere"

    async def exercise() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            await pilot.click("#side-installation-plan")
            await pilot.pause()
            app.query_one("#plan-dry-run", Button).press()
            await pilot.pause()
            assert "Dry-run preview" in str(app.query_one("#install-plan-dry-run", Static).content)

            await pilot.click("#side-installation-location")
            selector = app.query_one("#installation-location-mode", Select)
            selector.value = InstallLocationMode.CUSTOM
            await pilot.pause()
            app.query_one("#installation-custom-root", Input).value = str(custom_root)
            await pilot.pause()

            await pilot.click("#side-installation-plan")
            await pilot.pause()
            plan = str(app.query_one("#install-plan-steps", Static).content)
            assert "elsewhere" in plan
            assert "Not run yet" in str(app.query_one("#install-plan-dry-run", Static).content)

    asyncio.run(exercise())
    assert not custom_root.exists()
