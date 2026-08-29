from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, ContentSwitcher, Input, Label, Select, Static

from ..config import InstalledConfig, UiConfig
from ..installation import (
    InstallLocation,
    InstallLocationError,
    InstallLocationMode,
    resolve_installation,
)
from ..pets import PetAnimator, PetState
from ..profiles import Profile
from ..runtime import StackRuntime
from ..system import SystemFacts
from .appearance import TuiAppearance
from .navigation import (
    DEFAULT_ITEM,
    SECTION_ITEMS,
    SECTION_LABELS,
    Section,
    route_id,
    sidebar_button_id,
)
from .presenters import (
    placeholder,
    render_dashboard,
    render_general_settings,
    render_install_plan,
    render_install_stack,
    render_interface_settings,
    render_profile_details,
    render_service,
)


class InstallationLocationPanel(VerticalScroll):
    """Resolve installation intent for preview without touching the filesystem."""

    OPTIONS = tuple((mode.value.capitalize(), mode) for mode in InstallLocationMode)

    def __init__(self, location: InstallLocation, repo_root: Path) -> None:
        super().__init__(id=route_id(Section.INSTALLATION, "location"), classes="route-view")
        self.location = location
        self.repo_root = repo_root

    def compose(self) -> ComposeResult:
        yield Static("[b]Installation location[/b]", id="installation-location-title")
        yield Label("Mode", id="installation-mode-label")
        yield Select(
            self.OPTIONS,
            value=self.location.mode,
            allow_blank=False,
            id="installation-location-mode",
        )
        custom = self.location.root if self.location.mode is InstallLocationMode.CUSTOM else None
        yield Label("Absolute custom path", id="installation-custom-label")
        yield Input(
            "" if custom is None else str(custom),
            placeholder=r"D:\PepitosAIHub",
            id="installation-custom-root",
        )
        yield Static("", id="installation-location-error")
        yield Static("", id="installation-location-preview")
        yield Static(
            "Preview only. Resolving this location creates no directories.",
            id="installation-location-note",
        )

    def on_mount(self) -> None:
        for indicator in self.query(".down-arrow"):
            indicator.update("v")
        for indicator in self.query(".up-arrow"):
            indicator.update("^")
        self._show_custom(self.location.mode is InstallLocationMode.CUSTOM)
        self._show_resolved(self.location)

    def select_mode(self, mode: InstallLocationMode) -> InstallLocation | None:
        self._show_custom(mode is InstallLocationMode.CUSTOM)
        if mode is InstallLocationMode.CUSTOM:
            return self.validate_custom(self.query_one("#installation-custom-root", Input).value)
        location = InstallLocation(mode=mode)
        self._show_resolved(location)
        return location

    def validate_custom(self, value: str) -> InstallLocation | None:
        if not value.strip():
            self._show_error("Enter an absolute path for the Custom location.")
            return None
        try:
            location = InstallLocation.custom(value)
        except InstallLocationError as exc:
            self._show_error(f"Invalid custom location: {exc}")
            return None
        self._show_resolved(location)
        return location

    def _show_custom(self, visible: bool) -> None:
        self.query_one("#installation-custom-label", Label).display = visible
        self.query_one("#installation-custom-root", Input).display = visible

    def _show_error(self, message: str) -> None:
        self.query_one("#installation-location-error", Static).update(message)
        self.query_one("#installation-location-preview", Static).update("")

    def _show_resolved(self, location: InstallLocation) -> None:
        try:
            resolved = resolve_installation(location, repo_root=self.repo_root)
        except InstallLocationError as exc:
            self._show_error(f"Could not resolve location: {exc}")
            return
        self.location = location
        self.query_one("#installation-location-error", Static).update("")
        self.query_one("#installation-location-preview", Static).update(
            f"Resolved root\n[b]{self._display_path(resolved.root)}[/b]"
        )

    @staticmethod
    def _display_path(path: Path) -> str:
        try:
            relative = path.relative_to(Path.home())
        except ValueError:
            return str(path)
        return str(Path("%USERPROFILE%") / relative)


class TopNavigation(Horizontal):
    def __init__(self, active: Section = Section.DASHBOARD) -> None:
        super().__init__(id="top-navigation")
        self.active = active

    def compose(self) -> ComposeResult:
        for section in Section:
            yield Button(
                SECTION_LABELS[section],
                id=f"nav-{section.value}",
                variant="primary" if section is self.active else "default",
            )

    def set_active(self, section: Section) -> None:
        self.active = section
        for candidate in Section:
            self.query_one(f"#nav-{candidate.value}", Button).variant = (
                "primary" if candidate is section else "default"
            )


class ContextSidebar(VerticalScroll):
    def __init__(self, active: Section = Section.DASHBOARD) -> None:
        super().__init__(id="context-sidebar")
        self.active = active

    def compose(self) -> ComposeResult:
        yield Label(SECTION_LABELS[self.active], id="sidebar-title")
        for section, items in SECTION_ITEMS.items():
            with Vertical(
                id=f"sidebar-{section.value}",
                classes="sidebar-menu active" if section is self.active else "sidebar-menu",
            ):
                for item in items:
                    yield Button(
                        item.label,
                        id=sidebar_button_id(section, item.id),
                        variant=(
                            "primary"
                            if section is self.active and item.id == DEFAULT_ITEM[section]
                            else "default"
                        ),
                    )

    def set_section(self, section: Section) -> None:
        self.active = section
        self.query_one("#sidebar-title", Label).update(SECTION_LABELS[section])
        for candidate in Section:
            menu = self.query_one(f"#sidebar-{candidate.value}", Vertical)
            menu.set_class(candidate is section, "active")
        self.set_active_item(DEFAULT_ITEM[section])

    def set_active_item(self, item_id: str) -> None:
        for item in SECTION_ITEMS[self.active]:
            self.query_one(f"#{sidebar_button_id(self.active, item.id)}", Button).variant = (
                "primary" if item.id == item_id else "default"
            )


class ProfilePanel(VerticalScroll):
    def __init__(
        self,
        profiles: tuple[Profile, ...],
        selected: Profile,
        facts: SystemFacts,
    ) -> None:
        super().__init__(id=route_id(Section.INSTALLATION, "profile"), classes="route-view")
        self.profiles = profiles
        self.selected = selected
        self.facts = facts

    def compose(self) -> ComposeResult:
        with Horizontal(id="profile-actions"):
            for profile in self.profiles:
                yield Button(
                    profile.name,
                    id=f"profile-{profile.id}",
                    classes="profile-button",
                    variant="primary" if profile.id == self.selected.id else "default",
                )
        yield Static(
            render_profile_details(self.selected, self.facts),
            id="profile-details",
        )

    def select_profile(self, profile: Profile) -> None:
        self.selected = profile
        self.query_one("#profile-details", Static).update(
            render_profile_details(profile, self.facts)
        )
        for candidate in self.profiles:
            self.query_one(f"#profile-{candidate.id}", Button).variant = (
                "primary" if candidate.id == profile.id else "default"
            )


class MainContent(ContentSwitcher):
    def __init__(
        self,
        facts: SystemFacts,
        runtime: StackRuntime,
        profiles: tuple[Profile, ...],
        selected: Profile,
        config: InstalledConfig,
        repo_root: Path,
    ) -> None:
        super().__init__(
            id="main-content",
            initial=route_id(Section.DASHBOARD, DEFAULT_ITEM[Section.DASHBOARD]),
        )
        self.facts = facts
        self.runtime_snapshot = runtime
        self.profiles = profiles
        self.selected_profile = selected
        self.config = config
        self.repo_root = repo_root

    def compose(self) -> ComposeResult:
        yield Static(
            render_dashboard(self.facts, self.runtime_snapshot, self.selected_profile),
            id=route_id(Section.DASHBOARD, "status"),
            classes="route-view",
        )
        yield Static(
            placeholder(
                "Logs",
                "Log aggregation is not connected yet. Runtime data remains read-only.",
            ),
            id=route_id(Section.DASHBOARD, "logs"),
            classes="route-view",
        )
        for item_id, title, service_id in (
            ("ollama", "Ollama", "ollama"),
            ("open-webui", "Open WebUI", "open-webui"),
            ("comfyui", "ComfyUI", "comfyui"),
        ):
            yield Static(
                render_service(self.runtime_snapshot.by_id(service_id), title),
                id=route_id(Section.DASHBOARD, item_id),
                classes="route-view",
            )

        yield ProfilePanel(self.profiles, self.selected_profile, self.facts)
        yield Static(
            render_install_stack(self.selected_profile),
            id=route_id(Section.INSTALLATION, "stack"),
            classes="route-view",
        )
        yield InstallationLocationPanel(self.config.installation, self.repo_root)
        yield Static(
            render_install_plan(self.selected_profile),
            id=route_id(Section.INSTALLATION, "plan"),
            classes="route-view",
        )

        yield Static(
            render_general_settings(self.config),
            id=route_id(Section.SETTINGS, "general"),
            classes="route-view",
        )
        yield Static(
            render_interface_settings(self.config),
            id=route_id(Section.SETTINGS, "interface"),
            classes="route-view",
        )
        for item_id, title, message in (
            ("runtime", "Runtime settings", "Runtime controls are not available in this round."),
            ("advanced", "Advanced settings", "No advanced settings are exposed yet."),
        ):
            yield Static(
                placeholder(title, message),
                id=route_id(Section.SETTINGS, item_id),
                classes="route-view",
            )

    def show_route(self, section: Section, item_id: str) -> None:
        self.current = route_id(section, item_id)

    def select_profile(self, profile: Profile) -> None:
        self.selected_profile = profile
        self.query_one(ProfilePanel).select_profile(profile)
        self.query_one(f"#{route_id(Section.INSTALLATION, 'stack')}", Static).update(
            render_install_stack(profile)
        )
        self.query_one(f"#{route_id(Section.INSTALLATION, 'plan')}", Static).update(
            render_install_plan(profile)
        )
        self.query_one(f"#{route_id(Section.DASHBOARD, 'status')}", Static).update(
            render_dashboard(self.facts, self.runtime_snapshot, profile)
        )

    def update_config(self, config: InstalledConfig) -> None:
        self.config = config
        self.query_one(f"#{route_id(Section.SETTINGS, 'general')}", Static).update(
            render_general_settings(config)
        )
        self.query_one(f"#{route_id(Section.SETTINGS, 'interface')}", Static).update(
            render_interface_settings(config)
        )


class PetSlot(Static):
    """Textual-owned refresh loop over the backend's passive animator."""

    REFRESH_INTERVAL = 0.25

    def __init__(
        self,
        text: str = "",
        *,
        animator: PetAnimator | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(text, markup=False, id=id)
        self.animator = animator

    def on_mount(self) -> None:
        self.set_interval(self.REFRESH_INTERVAL, self.refresh_frame)

    def refresh_frame(self, now: float | None = None) -> None:
        if self.animator is not None:
            self.update(self.animator.current_text(now))

    def set_animator(self, animator: PetAnimator | None, fallback_text: str) -> None:
        self.animator = animator
        self.update(fallback_text if animator is None else animator.current_text())

    def set_state(self, state: PetState, *, now: float | None = None) -> bool:
        if self.animator is None:
            return False
        changed = self.animator.set_state(state, now=now)
        self.refresh_frame(now)
        return changed

    def set_pet_text(self, text: str) -> None:
        self.update(text)


class RightRail(Vertical):
    def __init__(
        self,
        profile: Profile,
        facts: SystemFacts,
        runtime: StackRuntime,
        ui: UiConfig,
        appearance: TuiAppearance,
    ) -> None:
        classes = []
        if not ui.right_rail_visible:
            classes.append("hidden")
        if not ui.right_rail_expanded:
            classes.append("collapsed")
        super().__init__(id="right-rail", classes=" ".join(classes))
        self.profile = profile
        self.facts = facts
        self.runtime_snapshot = runtime
        self.appearance = appearance

    def compose(self) -> ComposeResult:
        gpu_text = "No NVIDIA GPU"
        if self.facts.gpus:
            gpu = max(self.facts.gpus, key=lambda item: item.vram_total_mib)
            gpu_text = f"{gpu.name}\nVRAM {gpu.vram_total_mib} MiB"
        with Vertical(classes="rail-content"):
            yield Static("Profile", classes="rail-heading")
            yield Static(self.profile.name, id="rail-profile", classes="rail-value")
            yield Static("Pet area", classes="rail-heading")
            yield PetSlot(
                self.appearance.pet_text,
                animator=self.appearance.animator,
                id="pet-slot",
            )
            yield Static("Theme", classes="rail-heading")
            yield Static(self.appearance.theme_text, id="rail-theme", classes="rail-value")
            yield Static("GPU / VRAM", classes="rail-heading")
            yield Static(gpu_text, id="rail-gpu")
            yield Static("Stack", classes="rail-heading")
            yield Static(self.runtime_snapshot.state.value, id="rail-stack", classes="rail-value")
        yield Button("Expand" if self.is_collapsed else "Collapse", id="right-rail-toggle")

    @property
    def is_collapsed(self) -> bool:
        return self.has_class("collapsed")

    def toggle_collapsed(self) -> None:
        self.toggle_class("collapsed")
        self.query_one("#right-rail-toggle", Button).label = (
            "Expand" if self.is_collapsed else "Collapse"
        )

    def select_profile(self, profile: Profile, appearance: TuiAppearance) -> None:
        self.profile = profile
        self.appearance = appearance
        self.query_one("#rail-profile", Static).update(profile.name)
        self.query_one("#rail-theme", Static).update(appearance.theme_text)
        self.query_one(PetSlot).set_animator(appearance.animator, appearance.pet_text)

    def set_pet_text(self, text: str) -> None:
        self.query_one(PetSlot).set_pet_text(text)


class FooterBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield Static("Pepito's AI Hub", id="footer-brand")
        yield Static("v0.1", id="footer-version")
