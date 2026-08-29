from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, ContentSwitcher, Label, Static

from ..profiles import Profile
from ..runtime import StackRuntime
from ..system import SystemFacts
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
    render_install_plan,
    render_install_stack,
    render_profile_details,
    render_service,
)


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
    ) -> None:
        super().__init__(
            id="main-content",
            initial=route_id(Section.DASHBOARD, DEFAULT_ITEM[Section.DASHBOARD]),
        )
        self.facts = facts
        self.runtime_snapshot = runtime
        self.profiles = profiles
        self.selected_profile = selected

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
        yield Static(
            placeholder(
                "Installation location",
                "Configuration backend pending. Opening this view creates no directories.",
            ),
            id=route_id(Section.INSTALLATION, "location"),
            classes="route-view",
        )
        yield Static(
            render_install_plan(self.selected_profile),
            id=route_id(Section.INSTALLATION, "plan"),
            classes="route-view",
        )

        for item_id, title, message in (
            ("general", "General settings", "Configuration Architecture integration pending."),
            ("interface", "Interface settings", "Theme backend integration pending."),
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


class PetSlot(Static):
    """Minimal text surface that a future pet backend may update."""

    def set_pet_text(self, text: str) -> None:
        self.update(text)


class RightRail(Vertical):
    def __init__(
        self,
        profile: Profile,
        facts: SystemFacts,
        runtime: StackRuntime,
    ) -> None:
        super().__init__(id="right-rail")
        self.profile = profile
        self.facts = facts
        self.runtime_snapshot = runtime

    def compose(self) -> ComposeResult:
        gpu_text = "No NVIDIA GPU"
        if self.facts.gpus:
            gpu = max(self.facts.gpus, key=lambda item: item.vram_total_mib)
            gpu_text = f"{gpu.name}\nVRAM {gpu.vram_total_mib} MiB"
        with Vertical(classes="rail-content"):
            yield Static("Profile", classes="rail-heading")
            yield Static(self.profile.name, id="rail-profile", classes="rail-value")
            yield Static("Pet area", classes="rail-heading")
            yield PetSlot("Pet backend not connected", id="pet-slot")
            yield Static("GPU / VRAM", classes="rail-heading")
            yield Static(gpu_text, id="rail-gpu")
            yield Static("Stack", classes="rail-heading")
            yield Static(self.runtime_snapshot.state.value, id="rail-stack", classes="rail-value")
        yield Button("Collapse", id="right-rail-toggle")

    @property
    def is_collapsed(self) -> bool:
        return self.has_class("collapsed")

    def toggle_collapsed(self) -> None:
        self.toggle_class("collapsed")
        self.query_one("#right-rail-toggle", Button).label = (
            "Expand" if self.is_collapsed else "Collapse"
        )

    def select_profile(self, profile: Profile) -> None:
        self.profile = profile
        self.query_one("#rail-profile", Static).update(profile.name)

    def set_pet_text(self, text: str) -> None:
        self.query_one(PetSlot).set_pet_text(text)


class FooterBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield Static("Pepito's AI Hub", id="footer-brand")
        yield Static("v0.1", id="footer-version")
