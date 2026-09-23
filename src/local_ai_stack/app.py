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
from textual.css.query import NoMatches
from textual.events import Resize
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Input, Select

from .appearance import pet_state_for_stack
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
    detect_repo_root,
    resolve_installation,
)
from .integration import IntegrationSnapshot, detect_integration, plan_executor, provision_context
from .operation_contracts import OperationEvent, OperationSnapshot
from .profiles import Profile, load_profiles, offered_profiles
from .resources import resource_root
from .runtime import StackRuntime, stack_runtime
from .services import ServiceState
from .system import SystemFacts, detect_system
from .tui.appearance import TuiAppearance, resolve_tui_appearance, textual_theme
from .tui.application import (
    ConflictResolutionRequired,
    FolderPicker,
    HubApplication,
    HubError,
    HubUnavailable,
    InstallationSummary,
    InstallationView,
    InstallSelection,
    LaunchRefusal,
    LaunchTarget,
    OperationBusy,
    ProfileChoice,
    SwitchRequiresStop,
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
    InstallPanel,
    MainContent,
    RightRail,
    ServicesPanel,
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


def workspace_root() -> Path:
    """Where a portable stack goes: the checkout, or the folder holding the executable.

    Not :func:`project_root`: a frozen build reads its data from the bundle's
    private directory, and a stack must never be created inside that.
    """
    return detect_repo_root()


def default_executor_factory(installation: ResolvedInstallation) -> PlanExecutor:
    """The real executor, bound to a resolved install root. Runs nothing by itself."""
    return plan_executor(provision_context(installation))


# Below this many columns the right rail gives its width back to the content.
WIDE_TERMINAL_COLUMNS = 90

# After the browser is asked to open, the button stays down this long so a
# second press does not open a second tab.
LAUNCH_COOLDOWN_SECONDS = 2.0

UNWIRED_MESSAGE = "A instalação ainda não está conectada nesta versão do Hub."

# What the shell shows until the application has answered. A probe of the
# stack can take as long as the services take to answer, and an installation
# check reads the model tree; neither may run before the screen exists.
CHECKING_INSTALLATION = InstallationView(ready=False, checking=True)
UNPROBED_RUNTIME = StackRuntime((), ServiceState.UNKNOWN)
UNPROBED_FACTS = SystemFacts("", "", "", False, False, ())


class OperationObserved(Message):
    """An event from the application, carried onto the UI thread."""

    def __init__(self, event: OperationEvent) -> None:
        super().__init__()
        self.event = event


class ProjectionsLoaded(Message):
    """The application's read-only picture, gathered outside the render thread.

    Everything the Dashboard repeats -- is there a stack, which services
    answer, how much VRAM -- comes through here, so the shell is on screen
    before the first probe and stays responsive while later ones run.
    """

    def __init__(
        self,
        installation: InstallationView,
        services: ServicesSnapshot,
        vram: VramSnapshot,
        runtime: StackRuntime,
        facts: SystemFacts,
        integration: IntegrationSnapshot,
        generation: int,
        summaries: tuple[InstallationSummary, ...] = (),
    ) -> None:
        super().__init__()
        self.installation = installation
        self.services = services
        self.vram = vram
        self.runtime = runtime
        self.facts = facts
        self.integration = integration
        self.generation = generation
        self.summaries = summaries


class LaunchResolved(Message):
    """A bounded backend readiness check finished outside the render thread."""

    def __init__(self, target: LaunchTarget | LaunchRefusal, opened: bool = False) -> None:
        super().__init__()
        self.target = target
        self.opened = opened


class ProjectionsFailed(Message):
    def __init__(self, generation: int) -> None:
        super().__init__()
        self.generation = generation


class InstallSubmitted(Message):
    def __init__(self, selection: InstallSelection, error: HubError | None) -> None:
        super().__init__()
        self.selection = selection
        self.error = error


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
            for profile in offered_profiles(self.profiles)
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
        self.app.fit_to_width(event.size.width, event.size.height)  # type: ignore[attr-defined]


class LocalAIStackApp(App):
    TITLE = "Pepito's AI Hub"
    SUB_TITLE = "Local AI Stack Manager"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("1", "show_section('dashboard')", "Dashboard"),
        Binding("2", "show_section('installation')", "Instalação"),
        Binding("3", "show_section('settings')", "Configurações"),
        Binding("ctrl+r", "toggle_rail", "Barra lateral"),
        Binding("q", "quit", "Sair"),
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
        self._probe_facts = facts is None
        self.facts = facts if facts is not None else UNPROBED_FACTS
        # A caller that injected a runtime keeps it; otherwise the services are
        # probed by the first background refresh, never here.
        self._probe_runtime = runtime_snapshot is None
        self.runtime_snapshot = (
            runtime_snapshot if runtime_snapshot is not None else UNPROBED_RUNTIME
        )
        self.application: HubApplication = application or UnwiredHubApplication(
            self.facts, self.runtime_snapshot, self.profiles
        )
        self.open_browser = open_browser or webbrowser.open
        self.first_run_wizard = first_run_wizard
        # Injected, never constructed here: the TUI must not know which
        # operating-system dialog, if any, stands behind it.
        self.folder_picker = folder_picker
        # What the folder dialog is being opened for, when it is not the wizard.
        self._folder_purpose: str | None = None
        self._locating_root: Path | None = None
        self._pending_switch: Path | None = None
        # The entry a pending switch would replace, when it came from Locate.
        self._pending_replacing: Path | None = None
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
        self._submitting_install = False

        # The application is not asked anything yet: its answer can take as
        # long as the stack takes to answer, and the shell must be on screen
        # before then. The first refresh fills this in and picks the installed
        # profile; until then the Dashboard says it is checking.
        self._installation_view = CHECKING_INSTALLATION
        self._first_projections_pending = True
        self._projection_generation = 0
        self._projection_running = False
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
        self.executor_factory = executor_factory or default_executor_factory
        self._probe_integration = integration is None
        self.integration = integration if integration is not None else IntegrationSnapshot()

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
        return resolve_installation(self.installed_config.installation, repo_root=workspace_root())

    def _detect_integration(self) -> IntegrationSnapshot:
        return detect_integration(provision_context(self._resolved_installation()))

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        now = datetime.now(UTC)
        services, vram = ServicesSnapshot(now, ()), VramSnapshot(now, None, None)
        yield TopNavigation(self.active_section)
        with Horizontal(id="workspace"):
            yield ContextSidebar(self.active_section)
            yield MainContent(
                self.facts,
                self.runtime_snapshot,
                self.visible_profiles(),
                self.selected_profile,
                self.installed_config,
                workspace_root(),
                self._installation_view,
                services,
                vram,
                self.integration,
                self.executor_factory,
                can_pick_folder=self.folder_picker is not None,
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
        self.refresh_projections()

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
                for profile in self.visible_profiles()
            )
        # The wizard starts on the largest profile this machine can run, which
        # the application works out from the detected hardware. No profile is
        # preferred by name.
        recommended = next((choice.id for choice in choices if choice.recommended), None)
        self.push_screen(
            WizardScreen(
                choices,
                preselect=recommended,
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
        purpose, self._folder_purpose = self._folder_purpose, None
        wizard = self.wizard
        if wizard is not None and purpose is None:
            wizard.apply_picked_folder(message.folder)
            return
        if message.folder is None:
            return  # cancelled: nothing changes anywhere
        if purpose == "open":
            self._adopt_installation(message.folder)
        elif purpose == "locate":
            self._adopt_installation(message.folder, replacing=self._locating_root)

    def _ask_for_folder(self, purpose: str, initial: Path | None = None) -> None:
        """Open the system folder dialog for something other than the wizard."""
        if self.folder_picker is None or self._picking_folder:
            return
        self._folder_purpose = purpose
        self._picking_folder = True
        self.run_worker(lambda: self._pick_folder(initial), thread=True, group="folder-picker")

    def _adopt_installation(self, folder: Path, *, replacing: Path | None = None) -> None:
        """Validate a folder a person picked and switch to it, or say why not.

        ``replacing`` is the entry this folder was found for: relocating one
        installation, not adopting a second copy of it.
        """
        panel = self._install_panel()
        opener = getattr(self.application, "open_installation", None)
        relocate = getattr(self.application, "relocate_installation", None)
        if opener is None:
            return
        try:
            if replacing is not None and relocate is not None:
                relocate(replacing, folder)
            else:
                opener(folder)
        except SwitchRequiresStop as pending:
            # Nothing was adopted: switching to it is what has to wait.
            self._pending_switch = Path(folder)
            self._pending_replacing = replacing
            if panel is not None:
                panel.notice(self._stop_and_switch_question(pending))
            self._adopt_active_installation()
            return
        except HubError as exc:
            if panel is not None:
                panel.notice(str(exc))
            self._adopt_active_installation()
            return
        if panel is not None:
            panel.select_row(None)
            panel.notice("")
        self._adopt_active_installation()

    def _install_panel(self):
        try:
            return self.query_one(InstallPanel)
        except NoMatches:
            return None

    def _adopt_active_installation(self) -> None:
        """Read which installation is in use and make every surface say the same.

        The application is the authority: what it reports after a commit is
        what the configuration the screens read, the selected profile, the
        Installation screen and the Dashboard describe. Called after anything
        that may have moved the selection -- including the attempts that did
        not, where it puts the screens back on the installation still in use.
        """
        config = getattr(self.application, "config", None)
        if config is None:
            self.refresh_projections()
            return
        self.installed_config = config
        profile = next((item for item in self.profiles if item.id == config.profile_id), None)
        if profile is not None and profile.id != self.selected_profile.id:
            self.select_profile(profile.id)
        try:
            main = self.query_one(MainContent)
        except NoMatches:  # pragma: no cover - the shell is being taken down
            return
        main.update_config(config)
        self.refresh_projections()

    # --- managing installations ---------------------------------------------

    def on_install_panel_switch_requested(self, message: InstallPanel.SwitchRequested) -> None:
        panel = self._install_panel()
        switch = getattr(self.application, "switch_installation", None)
        if switch is None:
            return
        try:
            switch(message.root)
        except SwitchRequiresStop as pending:
            if self._pending_switch is not None and str(self._pending_switch) == str(message.root):
                # Asked twice for the same installation: that is the decision.
                replacing, self._pending_replacing = self._pending_replacing, None
                self._pending_switch = None
                self._operation_kind = "switch"
                try:
                    self.application.stop_and_switch(message.root, replacing=replacing)
                except HubError as exc:
                    self._operation_kind = None
                    if panel is not None:
                        panel.notice(str(exc))
                    self._adopt_active_installation()
                return
            self._pending_switch = message.root
            if panel is not None:
                panel.notice(self._stop_and_switch_question(pending))
            self._adopt_active_installation()
            return
        except HubError as exc:
            if panel is not None:
                panel.notice(str(exc))
            self._adopt_active_installation()
            return
        if panel is not None:
            panel.notice("")
        self._adopt_active_installation()

    def _stop_and_switch_question(self, pending: SwitchRequiresStop) -> str:
        """What has to happen before another installation can be used, in one paragraph."""
        running = pending.plan.running_profile or "A instalação atual"
        return (
            f"{running} ainda está em execução. Para usar a outra instalação, o Hub precisa "
            "parar os serviços gerenciados e confirmar que as portas foram liberadas. "
            "Processos externos não serão encerrados. Escolha a mesma instalação novamente "
            "para parar e trocar."
        )

    def on_install_panel_open_folder_requested(
        self, message: InstallPanel.OpenFolderRequested
    ) -> None:
        self._ask_for_folder("open")

    def on_install_panel_locate_requested(self, message: InstallPanel.LocateRequested) -> None:
        self._locating_root = message.root
        self._ask_for_folder("locate", message.root.parent)

    def on_install_panel_forget_requested(self, message: InstallPanel.ForgetRequested) -> None:
        panel = self._install_panel()
        forget = getattr(self.application, "forget_installation", None)
        if forget is None:
            return
        try:
            forget(message.root)
        except HubError as exc:
            if panel is not None:
                panel.notice(str(exc))
            return
        if panel is not None:
            panel.select_row(None)
            panel.notice("A instalação saiu da lista. Nenhum arquivo foi apagado.")
        self.refresh_projections()

    def on_install_panel_repair_requested(self, message: InstallPanel.RepairRequested) -> None:
        repair = getattr(self.application, "repair", None)
        if repair is None:
            return
        self._operation_kind = "repair"
        try:
            repair()
        except HubError as exc:
            self._operation_kind = None
            panel = self._install_panel()
            if panel is not None:
                panel.notice(str(exc))

    def on_install_panel_new_installation_requested(
        self, message: InstallPanel.NewInstallationRequested
    ) -> None:
        self.open_wizard()

    def on_install_panel_open_root_requested(self, message: InstallPanel.OpenRootRequested) -> None:
        panel = self._install_panel()
        if self.open_browser(str(message.root)) is False and panel is not None:
            panel.notice("Não foi possível abrir a pasta.")

    def on_wizard_screen_step_changed(self, message: WizardScreen.StepChanged) -> None:
        """The review says what the installation will download, once it is reached."""
        if message.step is not WizardStep.REVIEW:
            return
        wizard = self.wizard
        reader = getattr(self.application, "storage_estimate", None)
        if wizard is None or reader is None:
            return
        state = wizard.state
        if state.profile_id is None:
            return
        try:
            estimate = reader(state.profile_id, state.mode, state.custom_root.strip() or None)
        except Exception:  # noqa: BLE001 -- the review is shown with or without a number
            estimate = None
        wizard.set_storage(estimate)

    def on_wizard_screen_install_requested(self, message: WizardScreen.InstallRequested) -> None:
        self.select_profile(message.selection.profile_id)
        self._pending_install_selection = message.selection
        self._start_install(message.selection)

    def _start_install(self, selection: InstallSelection) -> None:
        """Preflight may scan processes; only its answer returns to the UI thread."""
        if self._submitting_install:
            return
        self._submitting_install = True
        self._operation_kind = "install"
        self.run_worker(lambda: self._submit_install(selection), thread=True, group="install")

    def _submit_install(self, selection: InstallSelection) -> None:
        error = None
        try:
            self.application.install(selection)
        except HubError as exc:
            error = exc
        except Exception:  # noqa: BLE001 -- preflight provider errors must remain recoverable
            error = HubError("Não foi possível preparar a instalação. Tente novamente.")
        self.post_message(InstallSubmitted(selection, error))

    def on_install_submitted(self, message: InstallSubmitted) -> None:
        self._submitting_install = False
        wizard = self.wizard
        selection = message.selection
        try:
            if message.error is not None:
                raise message.error
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
        """Coalesce refreshes without concurrent or stale backend observations."""
        self._projection_generation += 1
        if not self._projection_running:
            self._start_projection_worker()

    def _start_projection_worker(self) -> None:
        self._projection_running = True
        generation = self._projection_generation
        self.run_worker(
            lambda: self._load_projections(generation), thread=True, group="projections"
        )

    def _load_projections(self, generation: int) -> None:
        try:
            self._gather_projections(generation)
        except Exception:  # noqa: BLE001 -- a status failure must not crash the shell
            self.post_message(ProjectionsFailed(generation))

    def _gather_projections(self, generation: int) -> None:
        facts = detect_system() if self._probe_facts else self.facts
        integration = self._detect_integration() if self._probe_integration else self.integration
        runtime = stack_runtime() if self._probe_runtime else self.runtime_snapshot
        if isinstance(self.application, UnwiredHubApplication):
            self.application.facts = facts
            self.application.runtime_snapshot = runtime
        projections = getattr(self.application, "projections", None)
        if projections is None:
            installation = self._installation()
            services, vram = self._services(), self._vram()
        else:
            installation, services, vram = projections()
        summaries = self._installation_summaries()
        self.post_message(
            ProjectionsLoaded(
                installation,
                services,
                vram,
                runtime,
                facts,
                integration,
                generation,
                summaries,
            )
        )

    def _installation_summaries(self) -> tuple[InstallationSummary, ...]:
        """Optional on the application, like conflict resolution: absent means no list."""
        reader = getattr(self.application, "installation_summaries", None)
        if reader is None:
            return ()
        try:
            return tuple(reader())
        except Exception:  # noqa: BLE001 -- the list never fails the screen
            return ()

    def on_projections_failed(self, message: ProjectionsFailed) -> None:
        self._projection_running = False
        if message.generation != self._projection_generation:
            self._start_projection_worker()
            return
        self._notice("Não foi possível verificar a instalação. Tente novamente.", error=True)

    def on_projections_loaded(self, message: ProjectionsLoaded) -> None:
        self._projection_running = False
        if message.generation != self._projection_generation:
            self._start_projection_worker()
            return
        installation = message.installation
        self.facts = message.facts
        self._probe_facts = False
        self.integration = message.integration
        self.runtime_snapshot = message.runtime
        self._installation_view = installation
        try:
            main = self.query_one(MainContent)
        except NoMatches:
            # The answer arrived after the shell was taken down (the app is
            # exiting); there is nothing left to repaint.
            return
        main.refresh_facts(message.facts)
        if installation.ready and installation.profile_id:
            self.select_profile(installation.profile_id)
        main.refresh_overview(installation, message.services, message.vram, message.summaries)
        services_panel = main.query_one(ServicesPanel)
        services_panel.integration = message.integration
        services_panel.refresh_runtime(message.runtime)
        rail = self.query_one(RightRail)
        rail.facts = message.facts
        rail.runtime_snapshot = message.runtime
        rail.update_status(message.services, message.vram)
        rail.set_pet_state(pet_state_for_stack(message.runtime.state))
        if self._first_projections_pending:
            self._first_projections_pending = False
            if self.first_run_wizard and not installation.ready and self.wizard is None:
                self.open_wizard()

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
        opened = False
        if isinstance(target, LaunchTarget):
            try:
                opened = bool(self.open_browser(target.url))
            except Exception:  # noqa: BLE001 -- browser failures are shown without crashing
                opened = False
        self.post_message(LaunchResolved(target, opened))

    def on_launch_resolved(self, message: LaunchResolved) -> None:
        target = message.target
        if isinstance(target, LaunchRefusal):
            self._notice(target.reason)
            self._end_launch()
            return
        if message.opened:
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

    def visible_profiles(self) -> tuple[Profile, ...]:
        """The profiles the screens offer: the public ones, and the one in use.

        An internal profile is not something to choose -- it exists for
        development and for tests -- but an installation that already runs one
        still has to be able to name it, so the profile in use is never hidden
        from the screen that describes it.
        """
        offered = offered_profiles(self.profiles)
        in_use = next(
            (
                profile
                for profile in self.profiles
                if profile.id == self.installed_config.profile_id
            ),
            None,
        )
        if in_use is not None and in_use not in offered:
            return (*offered, in_use)
        return offered

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

    def fit_to_width(self, width: int, height: int | None = None) -> None:
        """Narrow terminals get a narrow rail; small ones get none at all."""
        rows = self.size.height if height is None else height
        for rail in self.query(RightRail):
            rail.fit_to(width, rows)

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
        event.stop()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "installation-location-mode":
            self.select_installation_mode(InstallLocationMode(event.value))
            event.stop()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "installation-custom-root":
            self.validate_custom_installation(event.value)
            event.stop()

    def action_quit(self) -> None:
        """Closing the Hub does not stop the stack -- but it does not cancel an operation.

        An installation or a switch keeps running in its own thread, and this
        version has no safe way to cancel it, so leaving is refused while one
        is in progress and the screen says what is happening. Everything else
        closes at once, with a reminder that the services stay up.
        """
        view = self.operation_view
        if view is not None and not view.terminal:
            self._notify_exit_refused()
            return
        self.exit()

    def _notify_exit_refused(self) -> None:
        message = (
            "Uma operação ainda está em andamento. Fechar o Hub agora não cancela "
            "esta operação de forma segura. Aguarde o fim da operação."
        )
        panel = self._install_panel()
        if panel is not None and self.active_section is Section.INSTALLATION:
            panel.notice(message)
            return
        try:
            self.query_one(DashboardPanel).show_notice(message, error=True)
        except NoMatches:  # pragma: no cover - the shell is already gone
            return

    def exit_notice(self) -> str:
        """What is true when the Hub closes: the stack keeps running without it."""
        if self._installation_view.ready:
            return "Fechar o Hub não para a stack. Os serviços continuam em execução."
        return ""

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
