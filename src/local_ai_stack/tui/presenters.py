from __future__ import annotations

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
from ..planning import InstallPlan, build_install_plan
from ..profiles import MIB_PER_GIB, Profile
from ..provisioning import LaunchAvailability
from ..runtime import ServiceRuntime
from ..system import SystemFacts
from .application import InstallationView
from .operations import OperationView


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


def render_operation(
    view: OperationView | None, *, idle_text: str = "", heading: str | None = None
) -> str:
    """One operation as a checklist: done steps, the current one, and how it ended.

    ``heading`` overrides the generic per-state headline. A caller that knows
    this view is an installation's outcome passes the frozen, unambiguous
    heading from ``installation_outcome_heading`` instead of the shorter,
    generic wording every other operation shares.
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
    if view.failed:
        lines.append("")
        for diagnostic in view.diagnostics:
            lines.append(f"[red]{escape(diagnostic.message)}[/red]")
            lines.append(f"[dim]Código: {escape(diagnostic.code)}[/dim]")
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
