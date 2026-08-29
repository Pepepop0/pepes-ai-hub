from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from .compatibility import evaluate_compatibility
from .planning import build_install_plan
from .profiles import MIB_PER_GIB, Profile, load_profiles
from .system import SystemFacts, detect_system


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def render_profile_details(profile: Profile, facts: SystemFacts) -> str:
    report = evaluate_compatibility(facts, profile)
    plan = build_install_plan(profile)
    requirement = profile.hardware

    status = (
        "[bold green]COMPATIBLE[/bold green]"
        if report.compatible
        else "[bold red]INCOMPATIBLE[/bold red]"
    )
    lines = [
        f"[b]{escape(profile.name)}[/b]",
        "",
        escape(profile.description),
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
            f"GPU used: {escape(report.selected_gpu.name)} — "
            f"{report.selected_gpu.vram_mib} MiB "
            f"({report.selected_gpu.vram_mib / MIB_PER_GIB:.1f} GiB displayed)"
        )
    for blocker in report.blockers:
        lines.append(f"[red]BLOCKER · {escape(blocker.message)}[/red]")
    for warning in report.warnings:
        lines.append(f"[yellow]WARNING · {escape(warning.message)}[/yellow]")
    if not report.blockers and not report.warnings:
        lines.append("No compatibility issues detected.")

    lines.extend(
        [
            "",
            "[b]Install plan · DRY-RUN[/b]",
            "[dim]Preview only. No commands or changes will be executed.[/dim]",
        ]
    )
    lines.extend(
        f"{step.sequence:02}. [cyan]{step.action.value}[/cyan] · "
        f"{escape(step.target)} — {escape(step.summary)}"
        for step in plan.steps
    )
    return "\n".join(lines)


class LocalAIStackApp(App):
    TITLE = "Local AI Stack Manager"
    SUB_TITLE = "Windows / NVIDIA"
    CSS = """
    #sidebar { width: 34; border: round $primary; padding: 1; }
    #details { border: round $secondary; padding: 1 2; }
    ListView { height: 1fr; }
    """

    def __init__(
        self,
        *,
        facts: SystemFacts | None = None,
        profiles: list[Profile] | None = None,
    ) -> None:
        super().__init__()
        self.profiles = profiles
        self.facts = facts if facts is not None else detect_system()

    def compose(self) -> ComposeResult:
        if self.profiles is None:
            self.profiles = load_profiles(project_root() / "profiles")
        yield Header()
        with Horizontal():
            with VerticalScroll(id="sidebar"):
                yield Label("Profiles")
                yield ListView(
                    *[
                        ListItem(
                            Label(f"{escape(profile.name)}\n{escape(profile.description)}"),
                            id=f"profile-{profile.id}",
                        )
                        for profile in self.profiles
                    ]
                )
            yield Static(self._system_summary(), id="details")
        yield Footer()

    def _system_summary(self) -> str:
        gpu = (
            "\n".join(
                f"- {escape(item.name)}: {item.vram_mib} MiB "
                f"({item.vram_mib / MIB_PER_GIB:.1f} GiB displayed)"
                for item in self.facts.gpus
            )
            if self.facts.gpus
            else "- no NVIDIA GPU detected"
        )
        return (
            "[b]Detected system[/b]\n\n"
            f"OS: {escape(self.facts.os_name)} {escape(self.facts.os_release)}\n"
            f"Python: {escape(self.facts.python_version)}\n"
            f"Git: {'OK' if self.facts.git_available else 'missing'}\n"
            f"nvidia-smi: {'OK' if self.facts.nvidia_smi_available else 'missing'}\n\n"
            f"[b]GPU[/b]\n{gpu}\n\nSelect a profile to preview compatibility and its "
            "dry-run install plan."
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if self.profiles is None or index is None or index >= len(self.profiles):
            return
        self.query_one("#details", Static).update(
            render_profile_details(self.profiles[index], self.facts)
        )


def run_tui() -> None:
    LocalAIStackApp().run()
