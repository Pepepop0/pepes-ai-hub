from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, ContentSwitcher, Input, Label, Select, Static

from .. import PRODUCT_NAME, display_version
from ..application_contracts import ServicesSnapshot, VramSnapshot
from ..config import InstalledConfig, UiConfig
from ..execution import ExecutionReport, PlanExecutor
from ..installation import (
    InstallLocation,
    InstallLocationError,
    InstallLocationMode,
    ResolvedInstallation,
    resolve_installation,
)
from ..integration import IntegrationSnapshot
from ..pets import PetState
from ..planning import build_install_plan
from ..profiles import Profile
from ..renderer import PetRenderer
from ..runtime import StackRuntime
from ..system import SystemFacts
from .appearance import TuiAppearance
from .application import InstallationSummary, InstallationView
from .navigation import (
    DEFAULT_ITEM,
    SECTION_ITEMS,
    SECTION_LABELS,
    Section,
    route_id,
    sidebar_button_id,
)
from .operations import OperationView
from .presenters import (
    DEFAULT_TRANSFER_WIDTH,
    display_path,
    placeholder,
    render_general_settings,
    render_install_plan,
    render_interface_settings,
    render_operation,
    render_overview,
    render_plan_execution,
    render_profile_details,
    render_service,
    render_vram,
    stack_summary,
)

PlanExecutorFactory = Callable[[ResolvedInstallation], PlanExecutor]


class InstallationLocationPanel(VerticalScroll):
    """Resolve installation intent for preview without touching the filesystem."""

    OPTIONS = tuple((mode.value.capitalize(), mode) for mode in InstallLocationMode)

    def __init__(self, location: InstallLocation, repo_root: Path) -> None:
        super().__init__(id=route_id(Section.INSTALLATION, "location"), classes="route-view")
        self.location = location
        self.repo_root = repo_root

    def compose(self) -> ComposeResult:
        yield Static("[b]Pasta da instalação[/b]", id="installation-location-title")
        yield Label("Como instalar", id="installation-mode-label")
        yield Select(
            self.OPTIONS,
            value=self.location.mode,
            allow_blank=False,
            id="installation-location-mode",
        )
        custom = self.location.root if self.location.mode is InstallLocationMode.CUSTOM else None
        yield Label("Pasta personalizada", id="installation-custom-label")
        yield Input(
            "" if custom is None else str(custom),
            placeholder=r"D:\PepitosAIHub",
            id="installation-custom-root",
        )
        yield Static("", id="installation-location-error")
        yield Static("", id="installation-location-preview")
        yield Static(
            "Apenas visualização. Nada é criado ao resolver esta pasta.",
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
            f"Pasta resolvida\n[b]{display_path(resolved.root)}[/b]"
        )


class InstallPlanPanel(VerticalScroll):
    """The plan, which steps this build can execute, and a dry-run preview.

    Resolving a plan is pure: it maps steps to provisioners and reads nothing
    from disk, so it is safe to do while the view is being built. Executing the
    dry-run is not automatic - it happens only when asked - and even then no
    directory is created, no archive is downloaded and no provisioner mutates
    anything, because every step receives dry_run=True.

    Real installation remains in the installation wizard. Resolution and
    dry-run provider calls run on workers, including when this hidden panel
    is first composed behind the Dashboard.
    """

    class Loaded(Message):
        def __init__(self, generation: int, plan_text: str, report_text: str, report) -> None:
            super().__init__()
            self.generation = generation
            self.plan_text = plan_text
            self.report_text = report_text
            self.report = report

    def __init__(
        self,
        profile: Profile,
        config: InstalledConfig,
        repo_root: Path,
        executor_factory: PlanExecutorFactory | None = None,
    ) -> None:
        super().__init__(id=route_id(Section.INSTALLATION, "plan"), classes="route-view")
        self.profile = profile
        self.config = config
        self.repo_root = repo_root
        self.executor_factory = executor_factory
        self.report: ExecutionReport | None = None
        self._generation = 0
        self._loading = False
        self._dry_run_requested = False

    def compose(self) -> ComposeResult:
        yield Static(self._plan_text(), id="install-plan-steps")
        yield Button("Ver plano", id="plan-dry-run", variant="primary")
        yield Static(render_plan_execution(None), id="install-plan-dry-run")
        yield Static(
            "[dim]Esta prévia não altera a instalação. Use o assistente de instalação "
            "para aplicar o plano.[/dim]",
            id="install-plan-note",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._request_plan)

    # --- estado ------------------------------------------------------------

    def _installation(self) -> ResolvedInstallation | None:
        try:
            return resolve_installation(self.config.installation, repo_root=self.repo_root)
        except InstallLocationError:
            return None

    def _plan_text(self) -> str:
        installation = self._installation()
        plan = build_install_plan(self.profile, installation=installation)
        return render_install_plan(plan)

    def _executor(self, installation: ResolvedInstallation | None) -> PlanExecutor | None:
        if installation is None or self.executor_factory is None:
            return None
        return self.executor_factory(installation)

    def refresh_plan(
        self, profile: Profile | None = None, config: InstalledConfig | None = None
    ) -> None:
        if profile is not None:
            self.profile = profile
        if config is not None:
            self.config = config
        # A different profile or root describes different work, so the previous
        # preview no longer belongs to what is on screen.
        self.report = None
        self.query_one("#install-plan-steps", Static).update(self._plan_text())
        self.query_one("#install-plan-dry-run", Static).update(render_plan_execution(None))
        self._request_plan()

    def run_dry_run(self) -> ExecutionReport | None:
        """Queue a read-only preview while the screen continues processing input."""
        self.query_one("#install-plan-dry-run", Static).update("Verificando o plano...")
        self._request_plan(dry_run=True)
        return None

    def _request_plan(self, *, dry_run: bool = False) -> None:
        self._generation += 1
        self._dry_run_requested = dry_run
        if not self._loading:
            self._start_loading()

    def _start_loading(self) -> None:
        self._loading = True
        generation, profile, config = self._generation, self.profile, self.config
        dry_run = self._dry_run_requested
        self.query_one("#plan-dry-run", Button).disabled = True
        self.run_worker(
            lambda: self._load_plan(generation, profile, config, dry_run),
            thread=True,
            group="install-plan",
        )

    def _load_plan(
        self, generation: int, profile: Profile, config: InstalledConfig, dry_run: bool
    ) -> None:
        try:
            installation = resolve_installation(config.installation, repo_root=self.repo_root)
        except InstallLocationError:
            installation = None
        plan = build_install_plan(profile, installation=installation)
        try:
            plan_text, report_text, report = self._resolve_plan(plan, installation, dry_run)
        except Exception:  # noqa: BLE001 -- provider failure must leave the TUI usable
            plan_text = render_install_plan(plan)
            report_text = "Não foi possível verificar o plano. Tente novamente."
            report = None
        self.post_message(self.Loaded(generation, plan_text, report_text, report))

    def _resolve_plan(self, plan, installation, dry_run):
        executor = self._executor(installation)
        resolution = None if executor is None else executor.resolve(plan)
        plan_text = render_install_plan(plan, resolution)
        if not dry_run:
            return plan_text, render_plan_execution(None), None
        if executor is None or installation is None:
            return (
                plan_text,
                placeholder(
                    "Plano detalhado",
                    "The install location could not be resolved, so there is no plan to preview.",
                ),
                None,
            )
        report = executor.execute(plan, dry_run=True)
        return plan_text, render_plan_execution(report), report

    def on_install_plan_panel_loaded(self, message: Loaded) -> None:
        self._loading = False
        if message.generation != self._generation:
            self._start_loading()
            return
        self.report = message.report
        self.query_one("#install-plan-steps", Static).update(message.plan_text)
        self.query_one("#install-plan-dry-run", Static).update(message.report_text)
        self.query_one("#plan-dry-run", Button).disabled = False
        message.stop()


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


class OperationPanel(Static):
    """One operation as a checklist. Fed by the app; never subscribes itself."""

    def __init__(self, idle_text: str, *, id: str | None = None) -> None:
        super().__init__(render_operation(None, idle_text=idle_text), id=id)
        self.idle_text = idle_text
        self.view: OperationView | None = None
        self.heading: str | None = None

    def show(self, view: OperationView | None, *, heading: str | None = None) -> None:
        self.view = view
        self.heading = heading
        self._repaint()

    def _repaint(self) -> None:
        # A transfer bar is sized to whatever this panel actually has, so a
        # narrow Dashboard column shrinks the bar instead of overflowing it.
        width = self.size.width or DEFAULT_TRANSFER_WIDTH
        self.update(
            render_operation(self.view, idle_text=self.idle_text, heading=self.heading, width=width)
        )

    def on_resize(self) -> None:
        self._repaint()


class DashboardPanel(VerticalScroll):
    """What a person sees every day: the profile, whether it runs, and one button.

    The overview is rebuilt from application projections whenever the app asks.
    The buttons only ask the app; the app decides whether anything happens.
    """

    def __init__(
        self,
        installation: InstallationView,
        profile_name: str,
        services: ServicesSnapshot,
        vram: VramSnapshot,
        gpu_name: str | None,
    ) -> None:
        super().__init__(id=route_id(Section.DASHBOARD, "overview"), classes="route-view")
        self.installation = installation
        self.profile_name = profile_name
        self.services = services
        self.vram = vram
        self.gpu_name = gpu_name
        self.launching = False

    def compose(self) -> ComposeResult:
        yield Static(self._overview(), id="dashboard-overview")
        with Horizontal(id="dashboard-primary"):
            yield Button("Abrir Open WebUI", id="dashboard-open-webui", variant="primary")
        with Horizontal(id="dashboard-secondary"):
            yield Button("Iniciar stack", id="dashboard-start")
            yield Button("Reiniciar", id="dashboard-restart")
            yield Button("Parar stack", id="dashboard-stop")
        yield OperationPanel("", id="dashboard-operation")
        yield Static("", id="dashboard-notice")

    def on_mount(self) -> None:
        self.set_busy(None)

    def _overview(self) -> str:
        return render_overview(
            self.installation, self.profile_name, self.services, self.vram, self.gpu_name
        )

    def refresh_overview(
        self,
        installation: InstallationView,
        profile_name: str,
        services: ServicesSnapshot,
        vram: VramSnapshot,
    ) -> None:
        self.installation = installation
        self.profile_name = profile_name
        self.services = services
        self.vram = vram
        self.query_one("#dashboard-overview", Static).update(self._overview())
        self.set_busy(self.query_one(OperationPanel).view)

    def show_operation(self, view: OperationView | None, *, heading: str | None = None) -> None:
        self.query_one(OperationPanel).show(view, heading=heading)
        self.set_busy(view)

    def set_busy(self, view: OperationView | None) -> None:
        """While an operation runs, nothing else may be asked for.

        Outside an operation the stack decides what is worth offering: a
        stopped stack is started, a running one is opened, restarted or
        stopped. Nothing is hidden that a person may still need -- a degraded
        stack can be restarted -- it is only ordered by what is useful now.
        """
        busy = view is not None and not view.terminal
        ready = self.installation.ready
        state = stack_summary(self.services) if ready else ""
        # Only what plainly does not apply is taken away: a stack that is
        # already ready is not started, one that is plainly stopped is not
        # stopped again. Anything in between stays available, because the
        # application is the one that decides what a command can do.
        self.query_one("#dashboard-start", Button).disabled = busy or not ready or state == "Pronta"
        for button_id in ("dashboard-restart", "dashboard-stop"):
            self.query_one(f"#{button_id}", Button).disabled = (
                busy or not ready or state == "Parada"
            )
        self.query_one("#dashboard-open-webui", Button).disabled = (
            busy or not ready or self.launching
        )

    def set_launching(self, launching: bool) -> None:
        self.launching = launching
        self.set_busy(self.query_one(OperationPanel).view)

    def show_notice(self, message: str, *, error: bool = False) -> None:
        colour = "red" if error else "yellow"
        text = f"[{colour}]{escape(message)}[/{colour}]" if message else ""
        self.query_one("#dashboard-notice", Static).update(text)


# How an installation's observed state is written for a person.
STATUS_WORDS = {
    "ready": "Pronta",
    "running": "Em execução",
    "stopped": "Parada",
    "missing": "Pasta indisponível",
    "unreadable": "Configuração ilegível",
    "unknown": "Estado desconhecido",
}

MODE_WORDS = {
    InstallLocationMode.DEFAULT: "Padrão",
    InstallLocationMode.PORTABLE: "Portátil",
    InstallLocationMode.CUSTOM: "Personalizada",
}


def installation_label(summary: InstallationSummary) -> str:
    """One short line for the quick switch: which profile, and which folder."""
    name = summary.profile_name or summary.profile_id or "Instalação"
    root = str(summary.root)
    if len(root) > 46:
        root = f"...{root[-43:]}"
    return f"{name} - {root}"


class InstallPanel(VerticalScroll):
    """Where installations are managed: the one in use, and every one the Hub knows.

    Four ways in, on purpose. The quick switch is for changing installation in
    one movement; the list is for seeing what exists, what state it is in and
    what can be done about it; the folder picker adopts an installation the Hub
    has never seen or finds one whose folder moved; and a new installation
    never replaces the one in use until it finishes.
    """

    class SwitchRequested(Message):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root

    class OpenFolderRequested(Message):
        """Pick a folder that already holds an installation."""

    class LocateRequested(Message):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root

    class ForgetRequested(Message):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root

    class RepairRequested(Message):
        """Check the installation in use and repair what is missing."""

    class NewInstallationRequested(Message):
        """Install another stack, keeping the one in use."""

    class OpenRootRequested(Message):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root

    def __init__(
        self,
        installation: InstallationView,
        profile_name: str,
        summaries: tuple[InstallationSummary, ...] = (),
        *,
        can_pick_folder: bool = False,
    ) -> None:
        super().__init__(id=route_id(Section.INSTALLATION, "install"), classes="route-view")
        self.installation = installation
        self.profile_name = profile_name
        self.summaries = summaries
        self.can_pick_folder = can_pick_folder
        self.selected_root: Path | None = None

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._current_text(), id="install-summary")
        with Horizontal(id="install-quick-switch"):
            yield Select(
                self._switch_options(),
                prompt="Trocar instalação",
                allow_blank=True,
                id="install-switch-select",
            )
        with Horizontal(id="install-current-actions"):
            yield Button("Trocar instalação", id="install-change", variant="primary")
            yield Button("Abrir pasta", id="install-open-root")
            yield Button("Verificar e reparar", id="install-repair")
        yield Static(self._known_text(), id="install-known")
        with Horizontal(id="install-known-actions"):
            yield Button("Abrir instalação existente", id="install-open-existing")
            yield Button("Nova instalação", id="install-open-wizard")
        with Horizontal(id="install-row-actions"):
            yield Button("Localizar pasta", id="install-locate")
            yield Button("Esquecer da lista", id="install-forget")
        yield Static("", id="install-notice")
        yield OperationPanel("Nenhuma operação em andamento.", id="install-operation")

    def on_mount(self) -> None:
        for indicator in self.query(".down-arrow"):
            indicator.update("v")
        for indicator in self.query(".up-arrow"):
            indicator.update("^")
        self._apply()

    # --- what the screen says -----------------------------------------------

    def _active(self) -> InstallationSummary | None:
        return next((row for row in self.summaries if row.active), None)

    def _others(self) -> tuple[InstallationSummary, ...]:
        return tuple(row for row in self.summaries if not row.active)

    def _switch_options(self) -> list[tuple[str, str]]:
        return [(installation_label(row), str(row.root)) for row in self._others() if row.usable]

    def _current_text(self) -> str:
        if self.installation.checking:
            return "[b]Instalação atual[/b]\n\n[dim]Verificando a instalação...[/dim]"
        active = self._active()
        if not self.installation.ready and active is None:
            return (
                "[b]Instalação atual[/b]\n\n"
                "Nenhuma instalação nesta máquina.\n"
                "O assistente escolhe o perfil e a pasta e cuida do resto."
            )
        name = (active.profile_name if active else None) or self.profile_name
        lines = ["[b]Instalação atual[/b]", "", f"[b]{escape(name)}[/b]"]
        if active is not None:
            state = STATUS_WORDS.get(active.status, STATUS_WORDS["unknown"])
            lines.append(f"{state} - {MODE_WORDS.get(active.mode, active.mode.value)}")
            lines.append(escape(str(active.root)))
        else:
            if self.installation.mode is not None:
                lines.append(MODE_WORDS.get(self.installation.mode, self.installation.mode.value))
            if self.installation.root_display:
                lines.append(escape(self.installation.root_display))
        return "\n".join(lines)

    def _known_text(self) -> str:
        lines = ["[b]Instalações conhecidas[/b]", ""]
        if not self.summaries:
            lines.append("[dim]Nenhuma instalação conhecida ainda.[/dim]")
            return "\n".join(lines)
        for row in self.summaries:
            mark = "[b]Atual[/b]" if row.active else (">" if self._is_selected(row) else " ")
            name = escape(row.profile_name or row.profile_id or "Instalação")
            state = STATUS_WORDS.get(row.status, STATUS_WORDS["unknown"])
            lines.append(f"{mark:6} {name:10} {state:20} {escape(str(row.root))}")
            if row.detail:
                lines.append(f"       [dim]{escape(row.detail)}[/dim]")
        lines.append("")
        lines.append(
            "[dim]Esquecer remove apenas esta instalação da lista. "
            "Nenhum arquivo será apagado.[/dim]"
        )
        return "\n".join(lines)

    def _is_selected(self, row: InstallationSummary) -> bool:
        return self.selected_root is not None and str(row.root) == str(self.selected_root)

    # --- state --------------------------------------------------------------

    def refresh_installation(
        self,
        installation: InstallationView,
        profile_name: str,
        summaries: tuple[InstallationSummary, ...] | None = None,
    ) -> None:
        self.installation = installation
        self.profile_name = profile_name
        if summaries is not None:
            self.summaries = summaries
            known = {str(row.root) for row in summaries}
            if self.selected_root is not None and str(self.selected_root) not in known:
                self.selected_root = None
        self._apply()

    def notice(self, message: str) -> None:
        self.query_one("#install-notice", Static).update(escape(message) if message else "")

    def _apply(self) -> None:
        self.query_one("#install-summary", Static).update(self._current_text())
        self.query_one("#install-known", Static).update(self._known_text())
        select = self.query_one("#install-switch-select", Select)
        options = self._switch_options()
        select.set_options(options)
        select.disabled = not options
        installed = self.installation.ready or self._active() is not None
        self.query_one("#install-open-wizard", Button).label = (
            "Nova instalação" if installed else "Instalar"
        )
        self.query_one("#install-change", Button).disabled = not options
        self.query_one("#install-repair", Button).disabled = not installed
        self.query_one("#install-open-root", Button).disabled = self._active() is None
        self.query_one("#install-open-existing", Button).disabled = not self.can_pick_folder
        selected = next(
            (row for row in self.summaries if self._is_selected(row)),
            None,
        )
        self.query_one("#install-locate", Button).disabled = not (
            selected is not None and selected.missing and self.can_pick_folder
        )
        self.query_one("#install-forget", Button).disabled = selected is None or selected.active

    def select_row(self, root: Path | str | None) -> None:
        """Point at one known installation, so the row actions apply to it."""
        self.selected_root = None if root is None else Path(str(root))
        self._apply()

    def show_operation(self, view: OperationView | None, *, heading: str | None = None) -> None:
        self.query_one(OperationPanel).show(view, heading=heading)
        busy = view is not None and not view.terminal
        for button_id in (
            "install-open-wizard",
            "install-change",
            "install-repair",
            "install-open-existing",
            "install-locate",
            "install-forget",
        ):
            self.query_one(f"#{button_id}", Button).disabled = (
                True if busy else self.query_one(f"#{button_id}", Button).disabled
            )
        if not busy:
            self._apply()

    # --- input --------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "install-switch-select":
            return
        value = event.value
        if value is Select.BLANK or not value:
            return
        event.select.value = Select.BLANK
        self.post_message(self.SwitchRequested(Path(str(value))))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id not in {
            "install-change",
            "install-open-root",
            "install-repair",
            "install-open-existing",
            "install-open-wizard",
            "install-locate",
            "install-forget",
        }:
            return
        event.stop()
        if button_id == "install-change":
            select = self.query_one("#install-switch-select", Select)
            select.focus()
            self.notice("Escolha a instalação para a qual quer trocar.")
        elif button_id == "install-open-root":
            active = self._active()
            if active is not None:
                self.post_message(self.OpenRootRequested(active.root))
        elif button_id == "install-repair":
            self.post_message(self.RepairRequested())
        elif button_id == "install-open-existing":
            self.post_message(self.OpenFolderRequested())
        elif button_id == "install-open-wizard":
            self.post_message(self.NewInstallationRequested())
        elif button_id == "install-locate" and self.selected_root is not None:
            self.post_message(self.LocateRequested(self.selected_root))
        elif button_id == "install-forget" and self.selected_root is not None:
            self.post_message(self.ForgetRequested(self.selected_root))


class ServicesPanel(VerticalScroll):
    """The three services in detail. Read-only, for when the overview is not enough."""

    def __init__(self, runtime: StackRuntime, integration: IntegrationSnapshot) -> None:
        super().__init__(id=route_id(Section.DASHBOARD, "services"), classes="route-view")
        self.runtime_snapshot = runtime
        self.integration = integration

    SERVICES = (("Ollama", "ollama"), ("Open WebUI", "open-webui"), ("ComfyUI", "comfyui"))

    def compose(self) -> ComposeResult:
        for title, service_id in self.SERVICES:
            yield Static(
                self._card(title, service_id), id=f"service-{service_id}", classes="service-card"
            )

    def _card(self, title: str, service_id: str) -> str:
        return render_service(
            self.runtime_snapshot.by_id(service_id), title, self.integration.by_service(service_id)
        )

    def refresh_runtime(self, runtime: StackRuntime) -> None:
        """A fresh probe of the services, gathered off the render thread."""
        self.runtime_snapshot = runtime
        for title, service_id in self.SERVICES:
            self.query_one(f"#service-{service_id}", Static).update(self._card(title, service_id))


class MainContent(ContentSwitcher):
    def __init__(
        self,
        facts: SystemFacts,
        runtime: StackRuntime,
        profiles: tuple[Profile, ...],
        selected: Profile,
        config: InstalledConfig,
        repo_root: Path,
        installation: InstallationView,
        services: ServicesSnapshot,
        vram: VramSnapshot,
        integration: IntegrationSnapshot | None = None,
        executor_factory: PlanExecutorFactory | None = None,
        summaries: tuple[InstallationSummary, ...] = (),
        can_pick_folder: bool = False,
    ) -> None:
        super().__init__(
            id="main-content",
            initial=route_id(Section.DASHBOARD, DEFAULT_ITEM[Section.DASHBOARD]),
        )
        self.summaries = summaries
        self.can_pick_folder = can_pick_folder
        self.facts = facts
        self.runtime_snapshot = runtime
        self.profiles = profiles
        self.selected_profile = selected
        self.config = config
        self.repo_root = repo_root
        self.installation = installation
        self.services = services
        self.vram = vram
        self.integration = integration or IntegrationSnapshot()
        self.executor_factory = executor_factory

    def _gpu_name(self) -> str | None:
        if not self.facts.gpus:
            return None
        return max(self.facts.gpus, key=lambda item: item.vram_total_mib).name

    def compose(self) -> ComposeResult:
        yield DashboardPanel(
            self.installation,
            self.selected_profile.name,
            self.services,
            self.vram,
            self._gpu_name(),
        )
        yield ServicesPanel(self.runtime_snapshot, self.integration)

        yield InstallPanel(
            self.installation,
            self.selected_profile.name,
            self.summaries,
            can_pick_folder=self.can_pick_folder,
        )
        yield ProfilePanel(self.profiles, self.selected_profile, self.facts)
        yield InstallationLocationPanel(self.config.installation, self.repo_root)
        yield InstallPlanPanel(
            self.selected_profile,
            self.config,
            self.repo_root,
            self.executor_factory,
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

    def show_route(self, section: Section, item_id: str) -> None:
        self.current = route_id(section, item_id)

    def select_profile(self, profile: Profile) -> None:
        self.selected_profile = profile
        self.query_one(ProfilePanel).select_profile(profile)
        self.query_one(InstallPlanPanel).refresh_plan(profile=profile)
        self.refresh_overview(self.installation, self.services, self.vram)

    def refresh_facts(self, facts: SystemFacts) -> None:
        self.facts = facts
        profiles = self.query_one(ProfilePanel)
        profiles.facts = facts
        profiles.select_profile(self.selected_profile)
        self.query_one(DashboardPanel).gpu_name = self._gpu_name()

    def update_config(self, config: InstalledConfig) -> None:
        self.config = config
        self.query_one(f"#{route_id(Section.SETTINGS, 'general')}", Static).update(
            render_general_settings(config)
        )
        self.query_one(f"#{route_id(Section.SETTINGS, 'interface')}", Static).update(
            render_interface_settings(config)
        )
        # The plan states where the work would land, so a new install location
        # is a new plan.
        self.query_one(InstallPlanPanel).refresh_plan(config=config)

    def refresh_overview(
        self,
        installation: InstallationView,
        services: ServicesSnapshot,
        vram: VramSnapshot,
        summaries: tuple[InstallationSummary, ...] | None = None,
    ) -> None:
        self.installation = installation
        self.services = services
        self.vram = vram
        if summaries is not None:
            self.summaries = summaries
        name = self.selected_profile.name
        self.query_one(DashboardPanel).refresh_overview(installation, name, services, vram)
        self.query_one(InstallPanel).refresh_installation(installation, name, self.summaries)

    def show_operation(self, view: OperationView | None, *, heading: str | None = None) -> None:
        self.query_one(DashboardPanel).show_operation(view, heading=heading)
        self.query_one(InstallPanel).show_operation(view, heading=heading)

    def run_plan_dry_run(self) -> ExecutionReport | None:
        return self.query_one(InstallPlanPanel).run_dry_run()


class PetSlot(Static):
    """Textual-owned refresh loop over the backend's passive renderer.

    The widget knows one verb: ask for the current text. Whether that text comes
    from a simple frame sequence or from composed micro-behaviors is the asset's
    business, decided in :mod:`local_ai_stack.renderer`.
    """

    # Fast enough that a blink and the arc of a hop both read as motion.
    REFRESH_INTERVAL = 0.12

    def __init__(
        self,
        text: str = "",
        *,
        animator: PetRenderer | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(text, markup=False, id=id)
        self.animator = animator

    def on_mount(self) -> None:
        self.set_interval(self.REFRESH_INTERVAL, self.refresh_frame)

    def on_resize(self) -> None:
        """Let the art re-fit the slot. A renderer without art ignores this."""
        if self._fit_to_slot():
            self.refresh_frame()

    def _fit_to_slot(self) -> bool:
        fit = getattr(self.animator, "fit_to_width", None)
        return bool(fit and self.size.width and fit(self.size.width))

    def refresh_frame(self, now: float | None = None) -> None:
        if self.animator is not None:
            self.update(self.animator.current_text(now))

    def set_animator(self, animator: PetRenderer | None, fallback_text: str) -> None:
        self.animator = animator
        if animator is None:
            self.update(fallback_text)
            return
        self._fit_to_slot()
        self.update(animator.current_text())

    def set_state(self, state: PetState, *, now: float | None = None) -> bool:
        if self.animator is None:
            return False
        changed = self.animator.set_state(state, now=now)
        self.refresh_frame(now)
        return changed

    def set_pet_text(self, text: str) -> None:
        self.update(text)


# Below this width the rail is narrow; below the next, it is not drawn at all,
# because an 80x24 terminal needs every column for what a person must reach.
RAIL_NARROW_COLUMNS = 120
RAIL_MINIMUM_COLUMNS = 90
RAIL_MINIMUM_ROWS = 25


class RightRail(Vertical):
    def __init__(
        self,
        profile: Profile,
        facts: SystemFacts,
        runtime: StackRuntime,
        ui: UiConfig,
        appearance: TuiAppearance,
        services: ServicesSnapshot | None = None,
        vram: VramSnapshot | None = None,
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
        self.services = services
        self.vram = vram

    def _gpu_text(self) -> str:
        gpu = (
            max(self.facts.gpus, key=lambda item: item.vram_total_mib) if self.facts.gpus else None
        )
        if self.vram is not None and (gpu is not None or self.vram.total_mib is not None):
            return render_vram(self.vram, gpu.name if gpu else None)
        if gpu is None:
            return "No NVIDIA GPU"
        return f"{gpu.name}\nVRAM {gpu.vram_total_mib} MiB"

    def _stack_text(self) -> str:
        if self.services is not None:
            return stack_summary(self.services)
        return self.runtime_snapshot.state.value

    def compose(self) -> ComposeResult:
        with Vertical(classes="rail-content"):
            yield Static("Perfil", classes="rail-heading")
            yield Static(self.profile.name, id="rail-profile", classes="rail-value")
            yield PetSlot(
                self.appearance.pet_text,
                animator=self.appearance.animator,
                id="pet-slot",
            )
            yield Static("GPU / VRAM", classes="rail-heading")
            yield Static(self._gpu_text(), id="rail-gpu")
            yield Static("Stack", classes="rail-heading")
            yield Static(self._stack_text(), id="rail-stack", classes="rail-value")
        yield Button("Expandir" if self.is_collapsed else "Recolher", id="right-rail-toggle")

    def update_status(self, services: ServicesSnapshot, vram: VramSnapshot) -> None:
        """Fresh projections from the application; the rail only repeats them."""
        self.services = services
        self.vram = vram
        self.query_one("#rail-gpu", Static).update(self._gpu_text())
        self.query_one("#rail-stack", Static).update(self._stack_text())

    @property
    def is_collapsed(self) -> bool:
        return self.has_class("collapsed")

    def set_narrow(self, narrow: bool) -> None:
        """Give back rail width when the terminal cannot spare it."""
        self.set_class(narrow, "narrow")

    def fit_to(self, width: int, height: int) -> None:
        """Three sizes of terminal, three answers.

        Wide enough: the rail as it is. Narrow: the rail, narrow, so the
        content keeps the columns it needs. Small enough that the rail would
        cost the content its buttons: no rail at all -- nothing critical is
        ever only here, and ctrl+r brings it back.
        """
        hidden = width < RAIL_MINIMUM_COLUMNS or height < RAIL_MINIMUM_ROWS
        self.display = not hidden
        self.set_narrow(width < RAIL_NARROW_COLUMNS)
        pet = self.query(PetSlot)
        for slot in pet:
            # A pet nobody can see does not animate.
            slot.display = not hidden

    def toggle_collapsed(self) -> None:
        self.toggle_class("collapsed")
        self.query_one("#right-rail-toggle", Button).label = (
            "Expandir" if self.is_collapsed else "Recolher"
        )

    def select_profile(self, profile: Profile, appearance: TuiAppearance) -> None:
        self.profile = profile
        self.appearance = appearance
        self.query_one("#rail-profile", Static).update(profile.name)
        self.query_one(PetSlot).set_animator(appearance.animator, appearance.pet_text)

    def set_pet_text(self, text: str) -> None:
        self.query_one(PetSlot).set_pet_text(text)

    def set_pet_state(self, state: PetState) -> None:
        self.query_one(PetSlot).set_state(state)


class FooterBar(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield Static(PRODUCT_NAME, id="footer-brand")
        yield Static(display_version(), id="footer-version")
