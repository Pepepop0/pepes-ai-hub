from __future__ import annotations

from rich.markup import escape

from ..compatibility import evaluate_compatibility
from ..config import InstalledConfig
from ..planning import build_install_plan
from ..profiles import MIB_PER_GIB, Profile
from ..runtime import ServiceRuntime, StackRuntime
from ..system import SystemFacts


def terminal_text(value: str) -> str:
    """Keep natural-language Unicode while removing decorative punctuation."""
    return value.replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", "-")


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
    lines.extend(["", render_install_plan(profile)])
    return "\n".join(lines)


def render_install_plan(profile: Profile) -> str:
    plan = build_install_plan(profile)
    lines = [
        "[b]Install plan - DRY-RUN[/b]",
        "[dim]Preview only. No commands or changes will be executed.[/dim]",
    ]
    lines.extend(
        f"{step.sequence:02}. [cyan]{step.action.value}[/cyan] - "
        f"{escape(terminal_text(step.target))} - {escape(terminal_text(step.summary))}"
        for step in plan.steps
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


def render_service(service: ServiceRuntime | None, title: str) -> str:
    if service is None:
        return f"[b]{escape(title)}[/b]\n\nNo runtime data available."
    return "\n".join(
        [
            f"[b]{escape(service.name)}[/b]",
            "",
            f"Address: {escape(service.spec.address)}",
            f"Health: {_state(service.health.state.value)}",
            f"Process: {_state(service.process.state.value)}",
            f"Origin: {escape(service.process.origin.value)}",
            "",
            escape(terminal_text(service.health.detail or "No health detail.")),
        ]
    )


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
