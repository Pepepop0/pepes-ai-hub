from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from rich.markup import escape

from ..application_contracts import ServicesSnapshot, VramSnapshot
from ..compatibility import evaluate_compatibility
from ..config import InstalledConfig
from ..execution import ExecutionReport, ExecutionStep
from ..external_conflict_contracts import (
    ConflictPreflightReport,
    ExternalConflictPolicy,
    ExternalServiceConflict,
)
from ..integration import ComponentIntegration
from ..operation_contracts import (
    OperationActivity,
    OperationActivityCategory,
    OperationState,
    OperationTimelineEntry,
    TransferProgress,
)
from ..planning import InstallPlan, build_install_plan
from ..profiles import MIB_PER_GIB, Profile
from ..provisioning import LaunchAvailability
from ..runtime import ServiceRuntime
from ..system import SystemFacts
from .application import InstallationView
from .operations import STATE_HEADLINES, OperationView


def mask_home(value: str) -> str:
    """Replace the home directory wherever it appears inside a string.

    A Windows home directory carries the account name, and this UI never puts
    the user's real name on screen - not in a resolved root, not in a plan step
    and not in a path it merely observed on the machine.
    """
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):  # pragma: no cover - no home on this host
        return value
    return value.replace(home, "%USERPROFILE%") if home else value


def terminal_text(value: str) -> str:
    """Keep natural-language Unicode, drop decorative punctuation, mask the home path.

    Every user-facing string goes through here, which is what makes the masking
    a single choke point instead of a rule each presenter has to remember.
    """
    cleaned = value.replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", "-")
    return mask_home(cleaned)


def display_path(path: Path) -> str:
    """A path fit to show on screen, with the home directory masked."""
    try:
        relative = Path(path).relative_to(Path.home())
    except (ValueError, OSError, RuntimeError):
        return mask_home(str(path))
    return str(Path("%USERPROFILE%") / relative)


def _state_color(value: str) -> str:
    return {
        "UP": "green",
        "RUNNING": "green",
        "COMPATIBLE": "green",
        "DOWN": "red",
        "STOPPED": "red",
        "INCOMPATIBLE": "red",
        "DEGRADED": "yellow",
        "STARTING": "yellow",
        "STOPPING": "yellow",
        "FAILED": "red",
        # Install ownership. MANAGED is the only one this hub may control.
        "MANAGED": "green",
        "EXTERNAL": "yellow",
        "NONE": "dim",
        "UNKNOWN": "dim",
    }.get(value, "white")


def _state(value: str) -> str:
    color = _state_color(value)
    return f"[{color}]{value}[/{color}]"


def _state_cell(value: str, width: int) -> str:
    color = _state_color(value)
    return f"[{color}]{value:<{width}}[/{color}]"


def render_profile_details(profile: Profile, facts: SystemFacts) -> str:
    report = evaluate_compatibility(facts, profile)
    requirement = profile.hardware
    status = (
        "[bold green]COMPATIBLE[/bold green]"
        if report.compatible
        else "[bold red]INCOMPATIBLE[/bold red]"
    )
    lines = [
        f"[b]{escape(profile.name)}[/b]",
        "",
        escape(terminal_text(profile.description)),
        "",
        "[b]Requirements[/b]",
        f"OS: {escape(requirement.os)}",
        f"GPU vendor: {escape(requirement.gpu_vendor)}",
        f"Minimum VRAM: {requirement.min_vram_mib} MiB",
        f"Recommended VRAM: {requirement.recommended_vram_mib} MiB",
        f"VRAM reporting tolerance: {requirement.vram_tolerance_mib} MiB",
        "",
        f"[b]Compatibility[/b]  {status}",
    ]
    if report.selected_gpu is not None:
        lines.append(
            f"GPU used: {escape(report.selected_gpu.name)} - "
            f"{report.selected_gpu.vram_total_mib} MiB "
            f"({report.selected_gpu.vram_total_mib / MIB_PER_GIB:.1f} GiB displayed)"
        )
    for blocker in report.blockers:
        lines.append(f"[red]BLOCKER - {escape(terminal_text(blocker.message))}[/red]")
    for warning in report.warnings:
        lines.append(f"[yellow]WARNING - {escape(terminal_text(warning.message))}[/yellow]")
    if not report.blockers and not report.warnings:
        lines.append("No compatibility issues detected.")
    lines.extend(["", render_install_plan(build_install_plan(profile))])
    return "\n".join(lines)


def render_install_plan(
    plan: InstallPlan,
    resolution: Sequence[ExecutionStep] | None = None,
) -> str:
    """The plan, and - when resolved - which steps this build can execute.

    A step nobody implements yet is labelled UNSUPPORTED with its reason. It is
    never omitted and never shown as if it were already done.
    """
    lines = [
        "[b]Install plan - DRY-RUN[/b]",
        "[dim]Preview only. No commands or changes will be executed.[/dim]",
    ]
    reasons = {entry.step.sequence: entry for entry in resolution or ()}
    for step in plan.steps:
        row = (
            f"{step.sequence:02}. [cyan]{step.action.value}[/cyan] - "
            f"{escape(terminal_text(step.target))} - {escape(terminal_text(step.summary))}"
        )
        entry = reasons.get(step.sequence)
        if entry is None:
            lines.append(row)
        elif entry.supported:
            lines.append(f"{row}\n    [green]SUPPORTED[/green]")
        else:
            lines.append(
                f"{row}\n    [yellow]UNSUPPORTED[/yellow] - {escape(terminal_text(entry.reason))}"
            )
    if resolution:
        supported = sum(1 for entry in resolution if entry.supported)
        lines.extend(
            [
                "",
                f"Executable in this build: {supported} of {len(resolution)} steps.",
            ]
        )
    return "\n".join(lines)


def render_plan_execution(report: ExecutionReport | None) -> str:
    """What a dry-run produced. Never shown before one has actually run."""
    if report is None:
        return (
            "[b]Dry-run preview[/b]\n\n"
            "Not run yet. It resolves the plan and asks each provisioner what it "
            "would do.\nNothing is downloaded, created or installed."
        )
    lines = [
        "[b]Dry-run preview[/b]",
        (
            f"[dim]Profile {escape(report.plan_profile_id)}. "
            "No directory was created and no download was started.[/dim]"
        ),
        "",
        f"Directories that would be created: {len(report.created_directories)}",
    ]
    for result in report.results:
        outcome = result.outcome.value.upper()
        detail = result.detail
        if result.failure is not None:
            detail = f"{result.failure.reason.value}: {result.failure.message}"
        lines.append(
            f"- {escape(result.component_id)}: [cyan]{outcome}[/cyan] - "
            f"{escape(terminal_text(detail or 'no detail'))}"
        )
    for result in report.action_results:
        outcome = "OK" if result.ok else "BLOCKED"
        colour = "cyan" if result.ok else "yellow"
        lines.append(
            f"- {escape(result.action.value)} {escape(terminal_text(result.target))}: "
            f"[{colour}]{outcome}[/{colour}] - {escape(terminal_text(result.detail))}"
        )
    if report.unsupported:
        lines.extend(["", f"[yellow]Unsupported steps: {len(report.unsupported)}[/yellow]"])
        lines.extend(
            f"- {escape(entry.step.action.value)} {escape(terminal_text(entry.step.target))} - "
            f"{escape(terminal_text(entry.reason))}"
            for entry in report.unsupported
        )
    return "\n".join(lines)


NO_PROVISIONER_REASON = (
    "No provisioner owns this component yet, so this hub cannot start or stop it."
)


def render_ownership(component: ComponentIntegration | None) -> str:
    """Install ownership, which is not the same question as who runs the process."""
    if component is None:
        return "UNKNOWN"
    if component.availability is LaunchAvailability.NOT_INSTALLED:
        return "NONE"
    return component.ownership.value.upper()


def render_controls(component: ComponentIntegration | None) -> tuple[str, str]:
    """Whether start/stop/restart mean anything here, and why."""
    if component is None:
        return "not available", NO_PROVISIONER_REASON
    if component.controllable:
        return "available", component.reason
    return "not available", component.reason


def render_service(
    service: ServiceRuntime | None,
    title: str,
    component: ComponentIntegration | None = None,
) -> str:
    if service is None:
        return f"[b]{escape(title)}[/b]\n\nNo runtime data available."
    ownership = render_ownership(component)
    availability, reason = render_controls(component)
    colour = "green" if availability == "available" else "yellow"
    lines = [
        f"[b]{escape(service.name)}[/b]",
        "",
        f"Address: {escape(service.spec.address)}",
        f"Health: {_state(service.health.state.value)}",
        f"Process: {_state(service.process.state.value)}",
        f"Process origin: {escape(service.process.origin.value)}",
        f"Install ownership: {_state(ownership)}",
    ]
    if component is not None and component.detected_path is not None:
        lines.append(f"Found at: {escape(display_path(component.detected_path))}")
    if component is not None and component.version:
        lines.append(f"Version: {escape(component.version)}")
    lines.extend(
        [
            "",
            f"Start / stop / restart: [{colour}]{availability}[/{colour}]",
            escape(terminal_text(reason)),
            "",
            escape(terminal_text(service.health.detail or "No health detail.")),
        ]
    )
    return "\n".join(lines)


def render_install_stack(profile: Profile) -> str:
    lines = [
        "[b]Stack selection[/b]",
        "",
        f"Profile: {escape(profile.name)}",
        "Enabled services:",
    ]
    lines.extend(
        f"- {escape(service_id)} on port {service.port}"
        for service_id, service in sorted(profile.services.items())
        if service.enabled
    )
    lines.extend(
        [
            "",
            "[dim]Planning view only. Installation is not available in this round.[/dim]",
        ]
    )
    return "\n".join(lines)


def render_general_settings(config: InstalledConfig) -> str:
    return "\n".join(
        [
            "[b]General settings[/b]",
            "",
            f"Profile: {escape(config.profile_id)}",
            f"Stack: {escape(config.stack_id)}",
            f"Installation mode: {escape(config.installation.mode.value)}",
            "",
            "[dim]Configuration is active in memory only. Nothing is persisted yet.[/dim]",
        ]
    )


def render_interface_settings(config: InstalledConfig) -> str:
    ui = config.general.ui
    theme = ui.theme or "Profile default"
    pet = ui.pet or "Profile default"
    return "\n".join(
        [
            "[b]Interface settings[/b]",
            "",
            f"Theme: {escape(theme)}",
            f"Pet: {escape(pet)}",
            f"Right rail visible: {'yes' if ui.right_rail_visible else 'no'}",
            f"Right rail expanded: {'yes' if ui.right_rail_expanded else 'no'}",
            "",
            "[dim]Unset theme and pet values inherit the selected profile defaults.[/dim]",
        ]
    )


def placeholder(title: str, message: str) -> str:
    return f"[b]{escape(title)}[/b]\n\n{escape(terminal_text(message))}"


# --- the Hub screens: overview and operations ---------------------------------

# Service health as a person reads it. The vocabulary is the contract's; the
# words are ours.
HEALTH_LABELS: dict[str, str] = {
    "UP": "Running",
    "DOWN": "Stopped",
    "DEGRADED": "Degraded",
    "UNKNOWN": "Unknown",
}

SERVICE_NAMES: dict[str, str] = {
    "ollama": "Ollama",
    "open-webui": "Open WebUI",
    "comfyui": "ComfyUI",
}


def health_label(health: str) -> str:
    return HEALTH_LABELS.get(health, health)


def _health_cell(health: str, width: int) -> str:
    colour = _state_color(health)
    return f"[{colour}]{health_label(health):<{width}}[/{colour}]"


def stack_summary(services: ServicesSnapshot) -> str:
    """One line for the whole stack: ready, stopped, or somewhere in between."""
    states = {service.health for service in services.services}
    if not states:
        return "Sem serviços"
    if states == {"UP"}:
        return "Pronta"
    if states == {"DOWN"}:
        return "Parada"
    if "DEGRADED" in states or ("UP" in states and "DOWN" in states):
        return "Parcialmente disponível"
    return "Desconhecida"


def render_vram(vram: VramSnapshot, gpu_name: str | None = None) -> str:
    lines = [gpu_name] if gpu_name else []
    if vram.total_mib is None:
        lines.append("VRAM desconhecida")
    elif vram.free_mib is None:
        lines.append(f"VRAM {vram.total_mib} MiB")
    else:
        lines.append(f"VRAM {vram.used_mib} / {vram.total_mib} MiB em uso")
    return "\n".join(escape(line) for line in lines)


def render_overview(
    installation: InstallationView,
    profile_name: str,
    services: ServicesSnapshot,
    vram: VramSnapshot,
    gpu_name: str | None,
) -> str:
    """The Dashboard as the first thing a person sees: what is there, and whether it runs."""
    lines = [f"Profile: [b]{escape(profile_name)}[/b]", ""]
    if installation.checking:
        lines.append("[dim]Verificando a instalação...[/dim]")
        return "\n".join(lines)
    if not installation.ready:
        lines.append("[yellow]Nenhuma stack instalada ainda.[/yellow]")
        lines.append("Use Instalação para preparar esta máquina.")
        return "\n".join(lines)
    width = max(len(name) for name in SERVICE_NAMES.values()) + 2
    for service in services.services:
        name = SERVICE_NAMES.get(service.service_id, service.service_id)
        lines.append(f"{escape(name):<{width}} {_health_cell(service.health, 9)}")
    lines.extend(
        [
            "",
            "[b]GPU[/b]",
            render_vram(vram, gpu_name) if gpu_name or vram.total_mib is not None else "Sem GPU",
            "",
            f"Stack: [b]{escape(stack_summary(services))}[/b]",
        ]
    )
    return "\n".join(lines)


# --- byte transfer: bar, sizes, speed, ETA -------------------------------------
#
# Every value here is derived at render time from one TransferProgress and
# nothing else is stored. A percentage only exists when total_bytes is known;
# an ETA only exists when total_bytes and a positive speed are both known
# (TransferProgress.eta_seconds already encodes exactly that rule). Nothing
# here estimates a percentage from elapsed time, phase count or guesswork.

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")

DEFAULT_TRANSFER_WIDTH = 60
MIN_BAR_WIDTH = 10
MAX_BAR_WIDTH = 40
# Room reserved for "[" + bar + "] " + "100%".
_BAR_CHROME = len("[] ") + len("100%")


def format_bytes(value: float) -> str:
    """A human size: consistent two-decimal precision, no fake trailing zeros.

    ``KiB``/``MiB``/``GiB``/``TiB`` show up to two decimals, trimmed to a whole
    number when the value is exact (so ``812 MiB``, not ``812.00 MiB``); plain
    ``B`` is always a whole number, since a fractional byte does not exist.
    """
    size = max(0.0, float(value))
    index = 0
    while size >= 1024 and index < len(_BYTE_UNITS) - 1:
        size /= 1024
        index += 1
    text = f"{size:.2f}"
    # A value that rounds up across a unit boundary (1023.997 -> "1024.00")
    # belongs in the next unit instead.
    if float(text) >= 1024 and index < len(_BYTE_UNITS) - 1:
        index += 1
        size /= 1024
        text = f"{size:.2f}"
    unit = _BYTE_UNITS[index]
    if unit == "B":
        return f"{int(size)} {unit}"
    text = text.rstrip("0").rstrip(".")
    return f"{text} {unit}"


def format_speed(bytes_per_second: float) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def format_percentage(fraction: float) -> int:
    """0-100, clamped. The only place a percentage is computed - never stored."""
    return round(max(0.0, min(1.0, fraction)) * 100)


def format_eta(seconds: float) -> str:
    """``~42 s``, ``~2m 13s`` or ``~1h 04m``. Rounded up: time remaining, not passed."""
    total = max(0, math.ceil(seconds))
    if total < 60:
        return f"~{total} s restantes"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"~{minutes}m {secs:02d}s restantes"
    hours, minutes = divmod(minutes, 60)
    return f"~{hours}h {minutes:02d}m restantes"


def bar_width_for(available: int) -> int:
    """The bar shrinks with the panel; it never overflows a narrow terminal."""
    usable = available - _BAR_CHROME
    return max(MIN_BAR_WIDTH, min(MAX_BAR_WIDTH, usable))


def render_bar(fraction: float, width: int) -> str:
    """A terminal-safe ``[####------]``. ASCII only: no font can fail to show it."""
    width = max(1, width)
    clamped = max(0.0, min(1.0, fraction))
    filled = max(0, min(width, round(clamped * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def render_transfer(transfer: TransferProgress, *, label: str, width: int) -> list[str]:
    """The lines for one byte transfer: determinate bar or indeterminate label.

    A known total gets a bar, a percentage and - when a total and a positive
    speed both exist - an ETA. An unknown total never gets a bar, a percentage
    or an ETA: those would all be invented. Speed is shown only when observed;
    an absent speed shows nothing rather than a placeholder.
    """
    lines: list[str] = []
    if label:
        lines.append(escape(label))
    if transfer.total_bytes is None:
        lines.append(f"[dim]{escape(format_bytes(transfer.completed_bytes))} transferidos[/dim]")
        if transfer.bytes_per_second is not None:
            lines.append(f"[dim]{escape(format_speed(transfer.bytes_per_second))}[/dim]")
        return lines
    fraction = transfer.fraction or 0.0
    bar = render_bar(fraction, bar_width_for(width))
    lines.append(f"{escape(bar)} {format_percentage(fraction)}%")
    lines.append(
        f"[dim]{escape(format_bytes(transfer.completed_bytes))} / "
        f"{escape(format_bytes(transfer.total_bytes))}[/dim]"
    )
    if transfer.bytes_per_second is not None:
        lines.append(f"[dim]{escape(format_speed(transfer.bytes_per_second))}[/dim]")
    eta = transfer.eta_seconds
    if eta is not None:
        lines.append(f"[dim]{escape(format_eta(eta))}[/dim]")
    return lines


def render_operation(
    view: OperationView | None,
    *,
    idle_text: str = "",
    heading: str | None = None,
    width: int = DEFAULT_TRANSFER_WIDTH,
) -> str:
    """One operation as a checklist: done steps, the current one, and how it ended.

    ``heading`` overrides the generic per-state headline. A caller that knows
    this view is an installation's outcome passes the frozen, unambiguous
    heading from ``installation_outcome_heading`` instead of the shorter,
    generic wording every other operation shares.

    ``width`` bounds the transfer bar for whatever panel is rendering this, so
    a narrow terminal shrinks the bar instead of overflowing the layout.
    """
    if view is None:
        return escape(idle_text)
    lines = [f"[b]{escape(heading if heading is not None else view.headline)}[/b]", ""]
    steps = list(view.steps)
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        if not last or view.succeeded:
            marker, colour = "[x]", "green"
        elif view.failed:
            marker, colour = "[!]", "red"
        else:
            marker, colour = "[>]", "yellow"
        suffix = f"  {view.progress_text}" if last and view.progress_text else ""
        # The markers are literal brackets, not markup tags.
        lines.append(f"[{colour}]{escape(marker)}[/{colour}] {escape(step)}{escape(suffix)}")
        if last and view.transfer is not None:
            # A transfer event may repeat the step's own text as its message
            # (nothing more specific to say); showing it twice would not help.
            label = view.message if view.message != step else ""
            lines.extend(render_transfer(view.transfer, label=label, width=width))
    if view.failed:
        lines.append("")
        for diagnostic in view.diagnostics:
            lines.append(f"[red]{escape(diagnostic.message)}[/red]")
            lines.append(f"[dim]Código: {escape(diagnostic.code)}[/dim]")
    return "\n".join(lines)


# --- semantic activity: what a step is doing right now, in plain words --------
#
# ``action`` is a short producer-chosen identifier (contract example: creating,
# installing, checking, waiting), never a display string. This is the one place
# that turns it into Portuguese; an action this table does not know still shows
# something readable instead of failing, since the contract only allowlists the
# *shape* of an action, not an exhaustive vocabulary of every one a provisioner
# might someday emit.

_ACTIVITY_VERBS: dict[str, str] = {
    "creating": "Criando",
    "installing": "Instalando",
    "installed": "Instalado",
    "checking": "Verificando",
    "verifying": "Verificando",
    "waiting": "Aguardando",
    "configuring": "Configurando",
    "starting": "Iniciando",
    "downloading": "Baixando",
    "preparing": "Preparando",
    "resolving": "Resolvendo",
    "cloning": "Clonando",
    "extracting": "Extraindo",
    "validating": "Validando",
    "verified": "Verificado",
    "ready": "Pronto",
    "available": "Disponível",
    "applied": "Aplicado",
    "receiving": "Recebendo",
}

# The noun a category names when the specific item belongs on its own line
# below the headline (there can be many, one after another: many packages,
# many models), rather than folded into one sentence.
_ACTIVITY_NOUNS: dict[OperationActivityCategory, str] = {
    OperationActivityCategory.DEPENDENCY: "dependências",
    OperationActivityCategory.MODEL: "modelo",
    OperationActivityCategory.WORKFLOW: "workflow",
}


def _activity_verb(action: str) -> str:
    return _ACTIVITY_VERBS.get(action, action.replace("_", " ").replace("-", " ").capitalize())


def activity_headline(activity: OperationActivity) -> tuple[str, str]:
    """(headline, item-or-empty). The item is its own line only when useful."""
    verb = _activity_verb(activity.action)
    category = activity.category
    if activity.action == "ready":
        return f"{activity.item} pronto", ""
    if activity.action == "available":
        return f"{activity.item} disponível", ""
    if category is OperationActivityCategory.READINESS:
        return f"{verb} {activity.item} responder", ""
    if category is OperationActivityCategory.TRANSFER:
        return activity.item, ""
    noun = _ACTIVITY_NOUNS.get(category)
    if noun is not None:
        return f"{verb} {noun}", activity.item
    return f"{verb} {activity.item}", ""


def format_elapsed(seconds: float) -> str:
    """``00:18``, ``02:41`` or ``1:04:12``. No milliseconds, ever."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_activity(activity: OperationActivity) -> list[str]:
    """The current semantic activity: headline, item and optional note.

    Elapsed time is not part of this: it is the last line of the *combined*
    activity-and-transfer block (see :func:`render_install_progress`), after
    whatever bytes or speed a transfer contributed - not a property of the
    activity headline alone.
    """
    headline, item = activity_headline(activity)
    lines = [escape(headline)]
    if item:
        lines.append(escape(item))
    if activity.detail:
        lines.append(f"[dim]{escape(activity.detail)}[/dim]")
    return lines


def render_timeline(
    history: tuple[OperationTimelineEntry, ...], *, current: OperationActivity | None = None
) -> list[str]:
    """The bounded detail history as a checklist. Never a step nobody observed yet.

    The frozen history is retained newest-last; the newest entries are what a
    person just did, so they stay at the bottom, next to whatever is current.
    """
    lines = ["[b]Detalhes[/b]", ""]
    for entry in history:
        headline, item = activity_headline(entry.detail)
        text = f"{headline} - {item}" if item else headline
        lines.append(f"[green]{escape('[x]')}[/green] {escape(text)}")
    if current is not None:
        headline, item = activity_headline(current)
        text = f"{headline} - {item}" if item else headline
        lines.append(f"[yellow]{escape('[>]')}[/yellow] {escape(text)}")
    if len(lines) == 2:
        lines.append("[dim]Nenhum detalhe registrado ainda.[/dim]")
    return lines


def render_install_progress(
    view: OperationView | None,
    *,
    idle_text: str = "",
    heading: str | None = None,
    active_heading: str = "Instalando",
    width: int = DEFAULT_TRANSFER_WIDTH,
    expanded: bool = False,
    include_heading: bool = True,
) -> str:
    """The wizard's granular install view: checklist, activity, transfer, history.

    ``include_heading=False`` leaves the title line out, for a caller whose
    screen already shows it -- the wizard puts the outcome in its own title,
    and repeating it in the pane below printed the same heading twice.

    Separate from :func:`render_operation`, which the Dashboard's compact
    operation panels keep using for any lifecycle command. An install
    specifically wants a step counter, a current activity set apart from the
    checklist, and an optional expandable timeline - detail this contract adds
    that a plain start/stop/restart view has no use for.

    Every number here is read from the view, never invented: the step counter
    only appears when a real ``OperationProgress`` total is known, the transfer
    bar only when a real total is known, and the timeline only lists detail
    entries this operation actually reported.
    """
    if view is None:
        return escape(idle_text)

    lines: list[str] = []
    if view.terminal:
        if include_heading:
            title = heading if heading is not None else view.headline
            lines.append(f"[b]{escape(title)}[/b]")
        if view.succeeded:
            lines.append(escape(STATE_HEADLINES[OperationState.SUCCEEDED]))
    elif include_heading:
        lines.append(f"[b]{escape(active_heading)}[/b]")
    if lines:
        lines.append("")

    # The step counter names *where in the plan* the current step sits, so it
    # only makes sense while a step is actually in progress. A terminal event's
    # own message is a result ("Não foi possível concluir"), not a step name -
    # pairing it with the last-known step count would misstate which step that
    # sentence is about. Success and failure say what they need to elsewhere.
    if not view.terminal and view.progress is not None and view.progress.total is not None:
        # The step's own name, not the latest event's message: an activity or
        # transfer event refining this step reuses ``message`` for its own
        # label (an item, a filename), which is not what step the count is of.
        current_step = view.steps[-1] if view.steps else view.message
        lines.append(
            f"Etapa {view.progress.completed} de {view.progress.total} — {escape(current_step)}"
        )
        lines.append("")

    steps = list(view.steps)
    for index, step in enumerate(steps):
        last = index == len(steps) - 1
        if not last or view.succeeded:
            marker, colour = "[x]", "green"
        elif view.failed:
            marker, colour = "[!]", "red"
        else:
            marker, colour = "[>]", "yellow"
        suffix = f"  {view.progress_text}" if last and view.progress_text else ""
        lines.append(f"[{colour}]{escape(marker)}[/{colour}] {escape(step)}{escape(suffix)}")

    block: list[str] = []
    if view.activity is not None:
        if view.failed:
            block.append("[b]Última atividade:[/b]")
        block.extend(render_activity(view.activity))
    if view.transfer is not None:
        if block:
            block.append("")
        # An activity already names what is being transferred; fall back to
        # the bare event message only when no activity supplied that label.
        label = ""
        if view.activity is None and steps and view.message != steps[-1]:
            label = view.message
        block.extend(render_transfer(view.transfer, label=label, width=width))
    if view.activity is not None and view.activity.elapsed_seconds is not None:
        # Elapsed is the last word on the whole block, after any transfer
        # bytes or speed it reported - not a property of the activity alone.
        block.append(f"[dim]Tempo: {escape(format_elapsed(view.activity.elapsed_seconds))}[/dim]")
    if block:
        lines.append("")
        lines.extend(block)

    if view.failed:
        lines.append("")
        lines.append("Motivo:")
        for diagnostic in view.diagnostics:
            lines.append(f"[red]{escape(diagnostic.message)}[/red]")
            lines.append(f"[dim]Código: {escape(diagnostic.code)}[/dim]")

    if view.succeeded and view.history:
        lines.append("")
        for entry in view.history:
            headline, item = activity_headline(entry.detail)
            text = f"{headline} - {item}" if item else headline
            lines.append(escape(text))

    if expanded:
        lines.append("")
        lines.extend(render_timeline(view.history, current=view.activity))

    return "\n".join(lines)


# --- external conflict, first-run wizard ---------------------------------------

CONFLICT_HEADING = "Encontramos serviços de IA já em execução."
BLOCKED_HEADING = "Não foi possível continuar agora."

ALONGSIDE_DESCRIPTION = (
    "Mantém os programas atuais em execução e instala uma cópia isolada "
    "do Pepito usando endpoints separados."
)
STOP_DESCRIPTION = (
    "Encerra somente os serviços externos que impedem a instalação. "
    "Nenhum programa ou arquivo será removido."
)
UNSAFE_STOP_NOTE = "Este serviço não pode ser encerrado com segurança pelo Hub."


def conflict_service_name(conflict: ExternalServiceConflict) -> str:
    return SERVICE_NAMES.get(conflict.service_id, str(conflict.service_id))


def render_conflict_report(report: ConflictPreflightReport) -> str:
    """The conflict/blocker list only: names, origin, endpoint, nothing internal."""
    lines: list[str] = []
    if report.hard_blockers:
        lines.append(f"[b]{escape(BLOCKED_HEADING)}[/b]")
        lines.append("")
        for blocker in report.hard_blockers:
            lines.append(f"[red]{escape(blocker.message)}[/red]")
            lines.append(f"[dim]Código: {escape(blocker.code)}[/dim]")
        if report.conflicts:
            lines.append("")
    if report.conflicts:
        lines.append(f"[b]{escape(CONFLICT_HEADING)}[/b]")
        lines.append("")
        for conflict in report.conflicts:
            lines.append(f"[b]{escape(conflict_service_name(conflict))}[/b]")
            lines.append(f"Externo — {escape(conflict.endpoint.display)}")
            if not conflict.can_stop_safely:
                lines.append(f"[dim]{escape(UNSAFE_STOP_NOTE)}[/dim]")
            lines.append("")
    return "\n".join(lines).rstrip()


def render_conflict_choices(report: ConflictPreflightReport) -> str:
    """The description under each available action. Buttons decide visibility."""
    recommended = report.recommended_policy
    lines: list[str] = []
    if ExternalConflictPolicy.INSTALL_ALONGSIDE in report.available_policies:
        suffix = " (recomendado)" if recommended is ExternalConflictPolicy.INSTALL_ALONGSIDE else ""
        lines.append(f"[b]Instalar ao lado{suffix}[/b]")
        lines.append(escape(ALONGSIDE_DESCRIPTION))
        lines.append("")
    if ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL in report.available_policies:
        lines.append("[b]Parar conflitos e instalar[/b]")
        lines.append(escape(STOP_DESCRIPTION))
        lines.append("")
    return "\n".join(lines).rstrip()


def render_stop_confirmation(report: ConflictPreflightReport) -> str:
    """Exactly the services the application said it can stop. Nothing else."""
    lines = ["[b]O Pepito vai parar temporariamente:[/b]", ""]
    lines.extend(
        f"- {escape(conflict_service_name(conflict))} externo" for conflict in report.conflicts
    )
    lines.extend(["", "Nenhum arquivo será removido."])
    return "\n".join(lines)
