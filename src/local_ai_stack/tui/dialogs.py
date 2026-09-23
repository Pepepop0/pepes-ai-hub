"""The two questions the Hub asks in a window of their own.

A welcome for a machine with no installation yet -- an offer, never a detour
into the wizard -- and the choice of which known installation to use. Both are
modal: they ask one thing, and whatever the person answers comes back to the
app as the screen's result; neither changes anything by itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from .application import DeletePlan, InstallationSummary

# How a known installation's observed state is written in these lists. The
# same words the Installations screen uses.
_STATUS = {
    "ready": "Pronta",
    "running": "Em execução",
    "stopped": "Parada",
    "missing": "Pasta indisponível",
    "unreadable": "Configuração ilegível",
    "unknown": "Estado desconhecido",
}


class WelcomeScreen(ModalScreen[bool]):
    """First opening of a Hub with no installation: offer the first one, do not impose it."""

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "later", "Agora não")]

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-dialog", classes="dialog"):
            yield Static("[b]Bem-vindo ao Pepito's AI Hub[/b]", id="welcome-title")
            yield Static(
                "Nenhuma instalação está configurada ainda.\n\n"
                "Deseja iniciar sua primeira instalação agora? Você escolhe o perfil "
                "e a pasta, e vê o plano antes de qualquer download.",
                id="welcome-body",
            )
            with Horizontal(classes="dialog-actions"):
                yield Button("Iniciar primeira instalação", id="welcome-start", variant="primary")
                yield Button("Agora não", id="welcome-later")

    def on_mount(self) -> None:
        self.query_one("#welcome-start", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "welcome-start")

    def action_later(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True)
class SwitchChoice:
    """What the person chose in the switch dialog.

    ``action`` is ``"use"`` (with the ``root`` to switch to), ``"open-existing"``
    or ``"new"``. A cancel is no choice at all: the dialog returns ``None``.
    """

    action: str
    root: Path | None = None


def installation_row(row: InstallationSummary) -> str:
    """One installation as two lines: who it is, then where it lives."""
    mark = "[b]\\[Atual][/b] " if row.active else ""
    name = escape(row.profile_name or row.profile_id or "Instalação")
    state = _STATUS.get(row.status, _STATUS["unknown"])
    return f"{mark}{name}  [dim]{state}[/dim]\n  {escape(str(row.root))}"


class SwitchInstallationScreen(ModalScreen[SwitchChoice | None]):
    """Every installation the Hub knows, the one in use included, to choose from.

    Choosing is two steps on purpose: moving through the list only points at
    an installation; "Usar instalação selecionada" is what asks for the
    switch, and on the installation already in use it does nothing.
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancelar")]

    def __init__(
        self,
        summaries: tuple[InstallationSummary, ...],
        *,
        can_pick_folder: bool = False,
    ) -> None:
        super().__init__()
        self.summaries = summaries
        self.can_pick_folder = can_pick_folder
        self.selected: InstallationSummary | None = next(
            (row for row in summaries if row.active), summaries[0] if summaries else None
        )

    @property
    def _has_others(self) -> bool:
        return any(not row.active for row in self.summaries)

    def compose(self) -> ComposeResult:
        with Vertical(id="switch-dialog", classes="dialog"):
            yield Static("[b]Trocar instalação[/b]", id="switch-title")
            yield OptionList(
                *(Option(installation_row(row), id=str(row.root)) for row in self.summaries),
                id="switch-list",
            )
            if not self.summaries:
                yield Static("Nenhuma instalação conhecida ainda.", id="switch-empty")
            elif not self._has_others:
                yield Static("Nenhuma outra instalação conhecida.", id="switch-empty")
            yield Static("", id="switch-hint")
            with Horizontal(classes="dialog-actions"):
                if self._has_others:
                    yield Button("Usar instalação selecionada", id="switch-use", variant="primary")
                yield Button("Abrir instalação existente", id="switch-open-existing")
                if not self._has_others:
                    yield Button("Nova instalação", id="switch-new")
                yield Button("Cancelar", id="switch-cancel")

    def on_mount(self) -> None:
        listing = self.query_one("#switch-list", OptionList)
        if self.selected is not None:
            index = self.summaries.index(self.selected)
            listing.highlighted = index
        listing.focus()
        self.query_one("#switch-open-existing", Button).disabled = not self.can_pick_folder
        self._show_selection()

    def _row_for(self, option_id: str | None) -> InstallationSummary | None:
        return next((row for row in self.summaries if str(row.root) == option_id), None)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option is not None:
            self.selected = self._row_for(event.option.id)
            self._show_selection()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.selected = self._row_for(event.option.id)
        self._show_selection()

    def _show_selection(self) -> None:
        """Say what "Usar" will do with the installation under the cursor."""
        row = self.selected
        if row is None:
            hint = ""
        elif row.active:
            hint = "Esta é a instalação em uso."
        elif row.missing:
            hint = "A pasta desta instalação não está disponível. Localize-a em Instalações."
        elif not row.usable:
            hint = "Esta instalação não pode ser usada agora."
        else:
            hint = ""
        self.query_one("#switch-hint", Static).update(f"[dim]{escape(hint)}[/dim]" if hint else "")
        if self._has_others:
            self.query_one("#switch-use", Button).disabled = row is None or (
                not row.active and not row.usable
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button.id
        if button == "switch-use":
            row = self.selected
            if row is None or row.active:
                # The installation already in use: nothing to switch to.
                self.dismiss(None)
            elif row.usable:
                self.dismiss(SwitchChoice("use", row.root))
        elif button == "switch-open-existing":
            self.dismiss(SwitchChoice("open-existing"))
        elif button == "switch-new":
            self.dismiss(SwitchChoice("new"))
        elif button == "switch-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DeleteInstallationScreen(ModalScreen[bool]):
    """Confirm deleting one installation's files. Always names the full path.

    Cancel is where the focus starts: deleting is the one thing the Hub
    cannot undo, so it is never what Enter does by accident.
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, plan: DeletePlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        plan = self.plan
        with Vertical(id="delete-dialog", classes="dialog"):
            yield Static("[b]Apagar instalação?[/b]", id="delete-title")
            lines = [f"[b]{escape(plan.profile_name)}[/b]", escape(str(plan.root)), ""]
            lines.append("Esta ação apagará permanentemente os arquivos desta instalação.")
            if plan.active:
                lines.append("É a instalação em uso: nenhuma outra será escolhida no lugar.")
            if plan.must_stop:
                lines.append(
                    "Os serviços desta instalação estão em execução e serão parados antes. "
                    "Processos externos não são encerrados."
                )
            yield Static("\n".join(lines), id="delete-body")
            with Horizontal(classes="dialog-actions"):
                yield Button(
                    "Parar e apagar" if plan.must_stop else "Apagar instalação",
                    id="delete-confirm",
                    variant="error",
                )
                yield Button("Cancelar", id="delete-cancel")

    def on_mount(self) -> None:
        self.query_one("#delete-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "delete-confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = [
    "DeleteInstallationScreen",
    "SwitchChoice",
    "SwitchInstallationScreen",
    "WelcomeScreen",
    "installation_row",
]
