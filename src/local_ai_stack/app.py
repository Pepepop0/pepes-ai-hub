from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Input, Select

from .config import InstalledConfig
from .installation import InstallLocationMode
from .profiles import Profile, load_profiles
from .runtime import StackRuntime, stack_runtime
from .system import SystemFacts, detect_system
from .tui.appearance import TuiAppearance, resolve_tui_appearance, textual_theme
from .tui.navigation import DEFAULT_ITEM, SIDEBAR_ROUTES, Section
from .tui.presenters import render_profile_details
from .tui.styles import APP_CSS
from .tui.widgets import (
    ContextSidebar,
    FooterBar,
    InstallationLocationPanel,
    MainContent,
    RightRail,
    TopNavigation,
)

__all__ = ["LocalAIStackApp", "render_profile_details", "run_tui"]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class LocalAIStackApp(App):
    TITLE = "Pepito's AI Hub"
    SUB_TITLE = "Local AI Stack Manager"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("1", "show_section('dashboard')", "Dashboard"),
        Binding("2", "show_section('installation')", "Instalação"),
        Binding("3", "show_section('settings')", "Configurações"),
        Binding("ctrl+r", "toggle_rail", "Right rail"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        facts: SystemFacts | None = None,
        profiles: list[Profile] | tuple[Profile, ...] | None = None,
        runtime_snapshot: StackRuntime | None = None,
        installed_config: InstalledConfig | None = None,
    ) -> None:
        super().__init__()
        self.profiles = tuple(
            profiles if profiles is not None else load_profiles(project_root() / "profiles")
        )
        if not self.profiles:
            raise ValueError("at least one profile is required")
        self.facts = facts if facts is not None else detect_system()
        self.runtime_snapshot = (
            runtime_snapshot if runtime_snapshot is not None else stack_runtime()
        )
        default_profile = next(
            (profile for profile in self.profiles if profile.id == "pepe"), self.profiles[0]
        )
        self.installed_config = installed_config or InstalledConfig(profile_id=default_profile.id)
        self.selected_profile = next(
            (
                profile
                for profile in self.profiles
                if profile.id == self.installed_config.profile_id
            ),
            default_profile,
        )
        if self.installed_config.profile_id != self.selected_profile.id:
            self.installed_config = replace(
                self.installed_config, profile_id=self.selected_profile.id
            )
        self.current_appearance = self._resolve_appearance(self.selected_profile)
        self.active_section = Section.DASHBOARD

    def compose(self) -> ComposeResult:
        yield TopNavigation(self.active_section)
        with Horizontal(id="workspace"):
            yield ContextSidebar(self.active_section)
            yield MainContent(
                self.facts,
                self.runtime_snapshot,
                self.profiles,
                self.selected_profile,
                self.installed_config,
                project_root(),
            )
            yield RightRail(
                self.selected_profile,
                self.facts,
                self.runtime_snapshot,
                self.installed_config.general.ui,
                self.current_appearance,
            )
        yield FooterBar()

    def on_mount(self) -> None:
        self._activate_theme(self.current_appearance)

    def show_section(self, section: Section) -> None:
        self.active_section = section
        self.query_one(TopNavigation).set_active(section)
        self.query_one(ContextSidebar).set_section(section)
        self.query_one(MainContent).show_route(section, DEFAULT_ITEM[section])

    def show_route(self, section: Section, item_id: str) -> None:
        if section is not self.active_section:
            self.show_section(section)
        self.query_one(ContextSidebar).set_active_item(item_id)
        self.query_one(MainContent).show_route(section, item_id)

    def select_profile(self, profile_id: str) -> None:
        profile = next(
            (candidate for candidate in self.profiles if candidate.id == profile_id), None
        )
        if profile is None:
            return
        self.selected_profile = profile
        self.installed_config = replace(self.installed_config, profile_id=profile.id)
        self.current_appearance = self._resolve_appearance(profile)
        self.query_one(MainContent).select_profile(profile)
        self.query_one(MainContent).update_config(self.installed_config)
        self.query_one(RightRail).select_profile(profile, self.current_appearance)
        self._activate_theme(self.current_appearance)

    def _resolve_appearance(self, profile: Profile) -> TuiAppearance:
        return resolve_tui_appearance(
            profile,
            self.installed_config.general.ui,
            self.runtime_snapshot,
        )

    def _activate_theme(self, appearance: TuiAppearance) -> None:
        if appearance.theme is None:
            self.theme = "textual-dark"
            return
        theme = textual_theme(appearance.theme)
        self.register_theme(theme)
        self.theme = theme.name

    def select_installation_mode(self, mode: InstallLocationMode) -> None:
        panel = self.query_one(InstallationLocationPanel)
        location = panel.select_mode(mode)
        if location is not None:
            self._set_installation(location)

    def validate_custom_installation(self, value: str) -> None:
        location = self.query_one(InstallationLocationPanel).validate_custom(value)
        if location is not None:
            self._set_installation(location)

    def _set_installation(self, location) -> None:
        self.installed_config = self.installed_config.with_installation(location)
        self.query_one(MainContent).update_config(self.installed_config)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("nav-"):
            self.show_section(Section(button_id.removeprefix("nav-")))
        elif button_id in SIDEBAR_ROUTES:
            self.show_route(*SIDEBAR_ROUTES[button_id])
        elif button_id.startswith("profile-"):
            self.select_profile(button_id.removeprefix("profile-"))
        elif button_id == "right-rail-toggle":
            self.query_one(RightRail).toggle_collapsed()
        event.stop()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "installation-location-mode":
            self.select_installation_mode(InstallLocationMode(event.value))
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "installation-custom-root":
            self.validate_custom_installation(event.value)
            event.stop()

    def action_show_section(self, section: str) -> None:
        self.show_section(Section(section))

    def action_toggle_rail(self) -> None:
        self.query_one(RightRail).toggle_collapsed()


def run_tui() -> None:
    LocalAIStackApp().run()
