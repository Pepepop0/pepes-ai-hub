from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.events import Resize
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Input, Select

from .application_contracts import ServicesSnapshot, ServiceView, VramSnapshot
from .compatibility import evaluate_compatibility
from .config import InstalledConfig
from .execution import PlanExecutor
from .external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictPolicy,
    installation_outcome_heading,
)
from .installation import (
    InstallLocation,
    InstallLocationMode,
    ResolvedInstallation,
    resolve_installation,
)
from .integration import IntegrationSnapshot, detect_integration, plan_executor, provision_context
from .operation_contracts import OperationEvent, OperationSnapshot
from .profiles import Profile, load_profiles
from .resources import resource_root
from .runtime import StackRuntime, stack_runtime
from .system import SystemFacts, detect_system
from .tui.appearance import TuiAppearance, resolve_tui_appearance, textual_theme
from .tui.application import (
    ConflictResolutionRequired,
    FolderPicker,
    HubApplication,
    HubError,
    HubUnavailable,
    InstallationView,
    InstallSelection,
    LaunchRefusal,
    LaunchTarget,
    OperationBusy,
    ProfileChoice,
)
from .tui.navigation import DEFAULT_ITEM, SIDEBAR_ROUTES, Section
from .tui.operations import OperationView, observe
from .tui.presenters import render_profile_details
from .tui.styles import APP_CSS
from .tui.widgets import (
    ContextSidebar,
    DashboardPanel,
    FooterBar,
    InstallationLocationPanel,
    MainContent,
    RightRail,
    TopNavigation,
)
from .tui.wizard import WizardScreen, WizardStep

__all__ = [
    "LocalAIStackApp",
    "UnwiredHubApplication",
    "render_profile_details",
    "run_tui",
]


def project_root() -> Path:
    """Where profiles, manifests and workflows live - checkout or frozen bundle alike."""
    return resource_root()


def default_executor_factory(installation: ResolvedInstallation) -> PlanExecutor:
    """The real executor, bound to a resolved install root. Runs nothing by itself."""
    return plan_executor(provision_context(installation))


# Below this many columns the right rail gives its width back to the content.
WIDE_TERMINAL_COLUMNS = 90

# After the browser is asked to open, the button stays down this long so a
# second press does not open a second tab.
LAUNCH_COOLDOWN_SECONDS = 2.0

# The wizard highlights this profile when it is offered, because it is the
# path the first-run flow is built around. It is a suggestion, not a choice.
SUGGESTED_PROFILE = "capella"

UNWIRED_MESSAGE = "A instalação ainda não está conectada nesta versão do Hub."


class OperationObserved(Message):
    """An event from the application, carried onto the UI thread."""

    def __init__(self, event: OperationEvent) -> None:
        super().__init__()
        self.event = event


class LaunchResolved(Message):
    """A bounded backend readiness check finished outside the render thread."""

    def __init__(self, target: LaunchTarget | LaunchRefusal) -> None:
        super().__init__()
        self.target = target


class ConflictResolved(Message):
    """The application's answer to a chosen conflict policy, off the render thread."""

    def __init__(self, result: ConflictResolutionResult) -> None:
        super().__init__()
        self.result = result


class FolderPicked(Message):
    """The system folder dialog closed. ``None`` is a cancel, or no dialog at all."""

    def __init__(self, folder: Path | None) -> None:
        super().__init__()
        self.folder = folder


@dataclass
class UnwiredHubApplication:
    """What the composition layer has until the application lands.

    It reads what this machine already knows - detected services and GPU - and
    refuses every command with the same sentence, so the screens are truthful
    without pretending anything can be installed or started from here yet.
    """

    facts: SystemFacts
    runtime_snapshot: StackRuntime
    profiles: tuple[Profile, ...]

    def current_operation(self) -> OperationSnapshot | None:
        return None

    def subscribe(self, sink):
        return _NoSubscription()

    def services_snapshot(self) -> ServicesSnapshot:
        return ServicesSnapshot(
            datetime.now(UTC),
            tuple(
                ServiceView(service.spec.id, service.health.state.value)
                for service in self.runtime_snapshot.services
            ),
        )

    def vram_snapshot(self) -> VramSnapshot:
        total = None
        if self.facts.gpus:
            total = max(gpu.vram_total_mib for gpu in self.facts.gpus)
        return VramSnapshot(datetime.now(UTC), total, None)

    def installation(self) -> InstallationView:
        return InstallationView(ready=False)

    def profile_choices(self) -> tuple[ProfileChoice, ...]:
        return tuple(
            ProfileChoice(
                profile.id,
                profile.name,
                profile.description,
                evaluate_compatibility(self.facts, profile).compatible,
            )
            for profile in self.profiles
        )

    def install(self, selection: InstallSelection) -> str:
        raise HubUnavailable(UNWIRED_MESSAGE)

    def start_stack(self) -> str:
        raise HubUnavailable(UNWIRED_MESSAGE)

    def stop_stack(self) -> str:
        raise HubUnavailable(UNWIRED_MESSAGE)

    def restart_stack(self) -> str:
        raise HubUnavailable(UNWIRED_MESSAGE)

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal:
        return LaunchRefusal(UNWIRED_MESSAGE, code="unwired")

    def current_conflict_preflight(self) -> ConflictPreflightReport | None:
        return None

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        raise HubUnavailable(UNWIRED_MESSAGE)


class _NoSubscription:
    def close(self) -> None:
        return None


class HubScreen(Screen):
    """The main screen. It is the one thing that is told when the terminal resizes."""

    def on_resize(self, event: Resize) -> None:
        self.app.fit_to_width(event.size.width)  # type: ignore[attr-defined]


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
        integration: IntegrationSnapshot | None = None,
        executor_factory: Callable[[ResolvedInstallation], PlanExecutor] | None = None,
        application: HubApplication | None = None,
        open_browser: Callable[[str], bool] | None = None,
        first_run_wizard: bool = True,
        folder_picker: FolderPicker | None = None,
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
        self.application: HubApplication = application or UnwiredHubApplication(
            self.facts, self.runtime_snapshot, self.profiles
        )
        self.open_browser = open_browser or webbrowser.open
        self.first_run_wizard = first_run_wizard
        # Injected, never constructed here: the TUI must not know which
        # operating-system dialog, if any, stands behind it.
        self.folder_picker = folder_picker
        self._picking_folder = False
        self.operation_view: OperationView | None = None
        self.notices: list[str] = []
        self._subscription = None
        self._launching = False
        # Which command started the operation currently being observed. Only
        # an install gets the frozen installation heading; a lifecycle command
        # that fails is not "an installation that did not conclude".
        self._operation_kind: str | None = None
        self._pending_install_selection: InstallSelection | None = None

        installation = self._installation()
        installed_profile = installation.profile_id if installation.ready else None
        default_profile = next(
            (profile for profile in self.profiles if profile.id == "pepe"), self.profiles[0]
        )
        self.installed_config = installed_config or InstalledConfig(
            profile_id=installed_profile or default_profile.id
        )
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
        self.executor_factory = executor_factory or default_executor_factory
        # Read-only: it detects what is installed and who owns it. Detection
        # creates no directory and starts nothing.
        self.integration = integration if integration is not None else self._detect_integration()

    # --- projections, with the application possibly absent -----------------

    def _installation(self) -> InstallationView:
        try:
            return self.application.installation()
        except HubUnavailable:
            return InstallationView(ready=False)

    def _services(self) -> ServicesSnapshot:
        try:
            return self.application.services_snapshot()
        except HubUnavailable:
            return ServicesSnapshot(datetime.now(UTC), ())

    def _vram(self) -> VramSnapshot:
        try:
            return self.application.vram_snapshot()
        except HubUnavailable:
            return VramSnapshot(datetime.now(UTC), None, None)

    def _resolved_installation(self) -> ResolvedInstallation:
        return resolve_installation(self.installed_config.installation, repo_root=project_root())

    def _detect_integration(self) -> IntegrationSnapshot:
        return detect_integration(provision_context(self._resolved_installation()))

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        services, vram = self._services(), self._vram()
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
                self._installation(),
                services,
                vram,
                self.integration,
                self.executor_factory,
            )
            yield RightRail(
                self.selected_profile,
                self.facts,
                self.runtime_snapshot,
                self.installed_config.general.ui,
                self.current_appearance,
                services,
                vram,
            )
        yield FooterBar()

    def on_mount(self) -> None:
        self._activate_theme(self.current_appearance)
        self._subscribe()
        if self.first_run_wizard and not self._installation().ready:
            self.open_wizard()

    def on_unmount(self) -> None:
        self._unsubscribe()

    def _subscribe(self) -> None:
        try:
            self._subscription = self.application.subscribe(
                lambda event: self.post_message(OperationObserved(event))
            )
        except HubUnavailable:
            self._subscription = None

    def _unsubscribe(self) -> None:
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            subscription.close()

    # --- the wizard ---------------------------------------------------------

    @property
    def wizard(self) -> WizardScreen | None:
        screen = self.screen
        return screen if isinstance(screen, WizardScreen) else None

    def open_wizard(self) -> None:
        if self.wizard is not None:
            return
        try:
            choices = self.application.profile_choices()
        except HubUnavailable:
            choices = tuple(
                ProfileChoice(profile.id, profile.name, profile.description)
                for profile in self.profiles
            )
        self.push_screen(
            WizardScreen(
                choices,
                preselect=SUGGESTED_PROFILE,
                recent_roots=self._recent_custom_roots(),
                can_pick_folder=self.folder_picker is not None,
            )
        )

    def _recent_custom_roots(self) -> tuple[str, ...]:
        """Optional on the application, like conflict resolution: absent means no history."""
        reader = getattr(self.application, "recent_custom_roots", None)
        if reader is None:
            return ()
        try:
            return tuple(reader())
        except Exception:  # noqa: BLE001 -- a missing history must never block the wizard
            return ()

    # --- the folder picker --------------------------------------------------

    def on_wizard_screen_folder_pick_requested(
        self, message: WizardScreen.FolderPickRequested
    ) -> None:
        if self.folder_picker is None:
            wizard = self.wizard
            if wizard is not None:
                wizard.apply_picked_folder(None)
            return
        if self._picking_folder:
            return  # the dialog already open will answer
        self._picking_folder = True
        initial = message.initial_path
        # The dialog is modal and blocks its thread until it closes; the event
        # loop keeps drawing because the call lives on a worker thread.
        self.run_worker(lambda: self._pick_folder(initial), thread=True, group="folder-picker")

    def _pick_folder(self, initial: Path | None) -> None:
        picker = self.folder_picker
        try:
            folder = picker.choose_folder(initial) if picker is not None else None
        except Exception:  # noqa: BLE001 -- a dialog failure is a cancel, not a crash
            folder = None
        self.post_message(FolderPicked(folder))

    def on_folder_picked(self, message: FolderPicked) -> None:
        self._picking_folder = False
        wizard = self.wizard
        if wizard is not None:
            wizard.apply_picked_folder(message.folder)

    def on_wizard_screen_install_requested(self, message: WizardScreen.InstallRequested) -> None:
        self.select_profile(message.selection.profile_id)
        self._pending_install_selection = message.selection
        self._start_install(message.selection)

    def _start_install(self, selection: InstallSelection) -> None:
        """Run preflight synchronously; an operation exists only after it accepts."""
        wizard = self.wizard
        try:
            self._operation_kind = "install"
            self.application.install(selection)
            self._set_installation(
                InstallLocation(
                    selection.mode,
                    Path(selection.custom_root) if selection.custom_root else None,
                )
            )
            if wizard is not None:
                # The attempt has just been recorded; a retry should already offer it.
                wizard.update_recent_roots(self._recent_custom_roots())
        except ConflictResolutionRequired as conflict:
            self._operation_kind = None
            report = conflict.report
            checker = getattr(self.application, "current_conflict_preflight", None)
            if checker is not None:
                try:
                    report = checker() or report
                except HubUnavailable:
                    pass
            if wizard is not None:
                wizard.show_conflict(report)
        except HubError as error:
            self._operation_kind = None
            if wizard is not None:
                wizard.go_to(WizardStep.REVIEW)
                wizard.show_refusal(str(error))
            self._notice(str(error), error=True)

    def on_wizard_screen_open_web_ui_requested(self, _: WizardScreen.OpenWebUiRequested) -> None:
        self.open_webui()

    def on_wizard_screen_finished(self, _: WizardScreen.Finished) -> None:
        if self.wizard is not None:
            self.pop_screen()
        self.refresh_projections()
        self.show_section(Section.DASHBOARD)

    # --- operations ---------------------------------------------------------

    def on_operation_observed(self, message: OperationObserved) -> None:
        view = observe(self.operation_view, message.event)
        if view is self.operation_view:
            return
        self.operation_view = view
        heading = None
        if view is not None and view.terminal and self._operation_kind == "install":
            heading = installation_outcome_heading(view.state)
        self.query_one(MainContent).show_operation(view, heading=heading)
        wizard = self.wizard
        if wizard is not None:
            wizard.show_operation(view)
        if view is not None and view.terminal:
            self.refresh_projections()

    def on_wizard_screen_conflict_policy_chosen(
        self, message: WizardScreen.ConflictPolicyChosen
    ) -> None:
        self.run_worker(
            lambda: self._resolve_conflict(message.selection), thread=True, group="conflict"
        )

    def _resolve_conflict(self, selection: ConflictPolicySelection) -> None:
        try:
            result = self.application.resolve_external_conflicts(selection)
        except Exception:  # noqa: BLE001 -- a resolver failure must not escape into the UI
            if selection.policy is ExternalConflictPolicy.ABORT:
                result = ConflictResolutionResult(
                    selection.preflight_id,
                    selection.policy,
                    ConflictResolutionStatus.ABORTED,
                    "Instalação cancelada.",
                )
            else:
                result = ConflictResolutionResult(
                    selection.preflight_id,
                    selection.policy,
                    ConflictResolutionStatus.FAILED,
                    "Não foi possível continuar. Tente novamente.",
                )
        self.post_message(ConflictResolved(result))

    def on_conflict_resolved(self, message: ConflictResolved) -> None:
        wizard = self.wizard
        if message.result.status is ConflictResolutionStatus.FAILED and wizard is not None:
            checker = getattr(self.application, "current_conflict_preflight", None)
            if checker is not None:
                try:
                    report = checker()
                except HubUnavailable:
                    report = None
                if report is not None:
                    wizard.show_conflict(report)
            wizard.apply_resolution(message.result)
            return
        if wizard is not None:
            wizard.apply_resolution(message.result)
        if message.result.status is ConflictResolutionStatus.ABORTED:
            self._operation_kind = None
            self._pending_install_selection = None
        elif message.result.status is ConflictResolutionStatus.PROCEED:
            selection = self._pending_install_selection
            if selection is None:
                if wizard is not None:
                    wizard.apply_resolution(
                        ConflictResolutionResult(
                            message.result.preflight_id,
                            message.result.policy,
                            ConflictResolutionStatus.FAILED,
                            "A seleção de instalação não está mais disponível.",
                        )
                    )
                return
            self._start_install(selection)

    def refresh_projections(self) -> None:
        """Ask the application again and repaint whatever repeats its answers."""
        installation = self._installation()
        if installation.ready and installation.profile_id:
            self.select_profile(installation.profile_id)
        services, vram = self._services(), self._vram()
        self.query_one(MainContent).refresh_overview(installation, services, vram)
        self.query_one(RightRail).update_status(services, vram)

    def run_stack_command(self, verb: str) -> None:
        """Start, stop or restart, if the application will have it."""
        command = {
            "start": self.application.start_stack,
            "stop": self.application.stop_stack,
            "restart": self.application.restart_stack,
        }[verb]
        try:
            self._operation_kind = verb
            command()
        except OperationBusy as error:
            self._notice(str(error))
        except HubError as error:
            self._notice(str(error), error=True)
        else:
            self._notice("")

    # --- Open WebUI ---------------------------------------------------------

    def open_webui(self) -> bool:
        """Accept one launch request; readiness runs outside the render thread."""
        if self._launching:
            return False
        self._launching = True
        self._set_launching(True)
        self.run_worker(self._resolve_launch, thread=True, group="open-webui")
        return True

    def _resolve_launch(self) -> None:
        try:
            target = self.application.open_webui_target()
        except HubError as error:
            target = LaunchRefusal(str(error))
        except Exception:  # noqa: BLE001 -- provider errors must not escape into the UI
            target = LaunchRefusal("Não foi possível preparar o Open WebUI. Tente novamente.")
        self.post_message(LaunchResolved(target))

    def on_launch_resolved(self, message: LaunchResolved) -> None:
        target = message.target
        if isinstance(target, LaunchRefusal):
            self._notice(target.reason)
            self._end_launch()
            return
        try:
            opened = bool(self.open_browser(target.url))
        except Exception:  # noqa: BLE001 -- browser launch failure is a user-facing refusal
            opened = False
        if opened:
            self._notice(f"Abrindo {target.label} no navegador.")
            self.set_timer(LAUNCH_COOLDOWN_SECONDS, self._end_launch)
        else:
            self._notice(f"O navegador não abriu. Endereço: {target.url}", error=True)
            self._end_launch()

    def _set_launching(self, launching: bool) -> None:
        for panel in self.query(DashboardPanel):
            panel.set_launching(launching)
            if not launching:
                panel.set_busy(self.operation_view)
        wizard = self.wizard
        if wizard is not None:
            wizard.query_one("#wizard-open-webui", Button).disabled = launching

    def _end_launch(self) -> None:
        self._launching = False
        self._set_launching(False)

    def _notice(self, message: str, *, error: bool = False) -> None:
        if message:
            self.notices.append(message)
        for panel in self.query(DashboardPanel):
            panel.show_notice(message, error=error)

    # --- navigation ---------------------------------------------------------

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

    def get_default_screen(self) -> Screen:
        return HubScreen()

    def fit_to_width(self, width: int) -> None:
        """Narrow terminals get a narrow rail; the pet scales itself to match."""
        for rail in self.query(RightRail):
            rail.set_narrow(width < WIDE_TERMINAL_COLUMNS)

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
        elif button_id == "plan-dry-run":
            # Preview only: every step runs with dry_run=True, so nothing is
            # created, downloaded or installed.
            self.query_one(MainContent).run_plan_dry_run()
        elif button_id == "dashboard-open-webui":
            self.open_webui()
        elif button_id in ("dashboard-start", "dashboard-stop", "dashboard-restart"):
            self.run_stack_command(button_id.removeprefix("dashboard-"))
        elif button_id == "install-open-wizard":
            self.open_wizard()
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
    from .folder_picker import default_folder_picker
    from .hub_application import create_hub_application

    application = create_hub_application(project_root())
    try:
        LocalAIStackApp(
            application=application,
            installed_config=application.config,
            folder_picker=default_folder_picker(),
        ).run()
    finally:
        application.close()
