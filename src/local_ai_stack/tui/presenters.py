from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.markup import escape

from ..compatibility import evaluate_compatibility
from ..config import InstalledConfig
from ..execution import ExecutionReport, ExecutionStep
from ..integration import ComponentIntegration
from ..planning import InstallPlan, build_install_plan
from ..profiles import MIB_PER_GIB, Profile
from ..provisioning import LaunchAvailability
from ..runtime import ServiceRuntime, StackRuntime
from ..system import SystemFacts


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


def render_dashboard(facts: SystemFacts, runtime: StackRuntime, profile: Profile) -> str:
    report = evaluate_compatibility(facts, profile)
    compatibility = "COMPATIBLE" if report.compatible else "INCOMPATIBLE"
    lines = [
        "[b]Stack status[/b]",
        "",
        "Service       Health    Process",
        "-------------------------------",
    ]
    lines.extend(
        f"{service.name:<13} {_state_cell(service.health.state.value, 9)} "
        f"{_state(service.process.state.value)}"
        for service in runtime.services
    )
    lines.extend(
        [
            "",
            f"Stack: {_state(runtime.state.value)}",
            f"Profile: [b]{escape(profile.name)}[/b]",
            f"Compatibility: {_state(compatibility)}",
            "",
            "[b]GPU[/b]",
        ]
    )
    if facts.gpus:
        gpu = max(facts.gpus, key=lambda item: item.vram_total_mib)
        lines.extend([escape(gpu.name), f"VRAM {gpu.vram_total_mib} MiB"])
    else:
        lines.append("No NVIDIA GPU detected")
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
