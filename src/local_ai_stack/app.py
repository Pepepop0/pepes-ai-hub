from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

from .profiles import load_profiles
from .system import detect_system


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class LocalAIStackApp(App):
    TITLE = "Local AI Stack Manager"
    SUB_TITLE = "Windows / NVIDIA"
    CSS = "#sidebar { width: 34; border: round $primary; padding: 1; } #details { border: round $secondary; padding: 1 2; } ListView { height: 1fr; }"

    def __init__(self):
        super().__init__()
        self.profiles = []
        self.facts = detect_system()

    def compose(self) -> ComposeResult:
        self.profiles = load_profiles(project_root() / "profiles")
        yield Header()
        with Horizontal():
            with VerticalScroll(id="sidebar"):
                yield Label("Profiles")
                yield ListView(
                    *[
                        ListItem(Label(f"{p.name}\n{p.description}"), id=f"profile-{p.id}")
                        for p in self.profiles
                    ]
                )
            yield Static(self._system_summary(), id="details")
        yield Footer()

    def _system_summary(self) -> str:
        gpu = (
            "\n".join(f"- {g.name}: {g.vram_mb / 1024:.1f} GB" for g in self.facts.gpus)
            if self.facts.gpus
            else "- nenhuma GPU NVIDIA detectada"
        )
        return f"[b]Detected system[/b]\n\nOS: {self.facts.os_name} {self.facts.os_release}\nPython: {self.facts.python_version}\nGit: {'OK' if self.facts.git_available else 'missing'}\nnvidia-smi: {'OK' if self.facts.nvidia_smi_available else 'missing'}\n\n[b]GPU[/b]\n{gpu}\n\nSelecione um profile."

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        i = event.list_view.index
        if i is None or i >= len(self.profiles):
            return
        p = self.profiles[i]
        self.query_one("#details", Static).update(
            f"[b]{p.name}[/b]\n\n{p.description}\n\nMin VRAM: {p.hardware.min_vram_gb} GB\nRecommended: {p.hardware.recommended_vram_gb} GB\nGPU vendor: {p.hardware.gpu_vendor}\n\n[i]Next:[/i] compatibility report + install plan."
        )


def run_tui() -> None:
    LocalAIStackApp().run()
