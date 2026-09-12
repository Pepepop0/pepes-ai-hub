"""First use: pick a profile, pick where it goes, install, watch, done.

The wizard is a screen pushed over the Hub when there is no stack to show. It
collects one :class:`InstallSelection` and asks the app to install it; from
then on it only watches the operation the app relays to it. It never talks to
the application layer itself, and the only technical word it needs a person
to read is the name of the profile they are choosing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, Input, Static

from ..external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictPolicy,
    installation_outcome_heading,
)
from ..installation import InstallLocation, InstallLocationError, InstallLocationMode
from .application import InstallSelection, ProfileChoice
from .operations import OperationView
from .presenters import (
    DEFAULT_TRANSFER_WIDTH,
    render_conflict_choices,
    render_conflict_report,
    render_operation,
    render_stop_confirmation,
)


class WizardStep(StrEnum):
    WELCOME = "welcome"
    PROFILE = "profile"
    MODE = "mode"
    REVIEW = "review"
    PROGRESS = "progress"
    CONFLICT = "conflict"
    CONFLICT_CONFIRM = "conflict-confirm"
    DONE = "done"


# The steps a person walks through; PROGRESS and DONE follow from "Instalar".
# CONFLICT and CONFLICT_CONFIRM are not numbered among them: they interrupt
# PROGRESS rather than extending the count a person was told to expect.
CHOICE_STEPS: tuple[WizardStep, ...] = (
    WizardStep.WELCOME,
    WizardStep.PROFILE,
    WizardStep.MODE,
    WizardStep.REVIEW,
)

MODE_LABELS: dict[InstallLocationMode, tuple[str, str]] = {
    InstallLocationMode.DEFAULT: (
        "Padrão",
        "Instala na pasta de dados do seu usuário. Escolha esta se não tiver certeza.",
    ),
    InstallLocationMode.PORTABLE: (
        "Portátil",
        "Instala ao lado do Hub, para levar tudo junto em outra pasta ou disco.",
    ),
    InstallLocationMode.CUSTOM: (
        "Personalizado",
        "Você escolhe a pasta. Precisa ser um caminho completo.",
    ),
}

WELCOME_TEXT = (
    "[b]Bem-vindo ao Pepito's AI Hub[/b]\n\n"
    "Este assistente prepara esta máquina para conversar com modelos e gerar "
    "imagens localmente.\n\n"
    "Você vai escolher um perfil e onde instalar. O resto acontece sozinho, e "
    "ao final o Open WebUI abre no navegador."
)


def step_pane_id(step: WizardStep, *, prefix: str = "") -> str:
    return f"{prefix}wizard-step-{step.value}"


@dataclass(frozen=True)
class WizardState:
    """What has been chosen so far. Immutable, so a step change is a new value."""

    step: WizardStep = WizardStep.WELCOME
    profile_id: str | None = None
    mode: InstallLocationMode = InstallLocationMode.DEFAULT
    custom_root: str = ""
    custom_error: str = ""

    @property
    def selection(self) -> InstallSelection | None:
        """The complete request, or nothing while a required choice is missing."""
        if self.profile_id is None:
            return None
        if self.mode is InstallLocationMode.CUSTOM:
            if not self.custom_root.strip() or self.custom_error:
                return None
            return InstallSelection(self.profile_id, self.mode, self.custom_root.strip())
        return InstallSelection(self.profile_id, self.mode)

    @property
    def can_continue(self) -> bool:
        if self.step is WizardStep.PROFILE:
            return self.profile_id is not None
        if self.step is WizardStep.MODE:
            return self.mode is not InstallLocationMode.CUSTOM or self.selection is not None
        return self.step in CHOICE_STEPS


def validate_custom_root(value: str) -> str:
    """An empty string when the path would be accepted, else the reason it is not."""
    if not value.strip():
        return "Informe um caminho completo para a pasta de instalação."
    try:
        InstallLocation.custom(value.strip())
    except InstallLocationError as error:
        return f"Caminho inválido: {error}"
    return ""


class WizardScreen(Screen):
    """The first-run flow. Emits messages; the app decides what they mean."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "back", "Voltar", show=False),
    ]

    class InstallRequested(Message):
        def __init__(self, selection: InstallSelection) -> None:
            super().__init__()
            self.selection = selection

    class OpenWebUiRequested(Message):
        pass

    class Finished(Message):
        pass

    class ConflictPolicyChosen(Message):
        """The entire outgoing command: an opaque preflight id and a policy.

        Nothing else is ever attached - no PID, no executable, no path, no
        host or port. The app forwards this unchanged to the application.
        """

        def __init__(self, selection: ConflictPolicySelection) -> None:
            super().__init__()
            self.selection = selection

    def __init__(
        self,
        choices: tuple[ProfileChoice, ...],
        *,
        preselect: str | None = None,
    ) -> None:
        super().__init__(id="wizard")
        if not choices:
            raise ValueError("the wizard needs at least one profile to offer")
        self.choices = choices
        preferred = preselect if any(choice.id == preselect for choice in choices) else None
        self.state = WizardState(profile_id=preferred)
        self.operation: OperationView | None = None
        self.conflict: ConflictPreflightReport | None = None
        self._conflict_error: str = ""

    # --- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-frame"):
            yield Static("", id="wizard-title")
            with ContentSwitcher(id="wizard-steps", initial=step_pane_id(WizardStep.WELCOME)):
                yield Static(
                    WELCOME_TEXT, id=step_pane_id(WizardStep.WELCOME), classes="wizard-pane"
                )
                with VerticalScroll(id=step_pane_id(WizardStep.PROFILE), classes="wizard-pane"):
                    yield Static(
                        "[b]Qual perfil descreve esta máquina?[/b]", classes="wizard-question"
                    )
                    with Horizontal(id="wizard-profile-buttons"):
                        for choice in self.choices:
                            yield Button(
                                choice.name,
                                id=f"wizard-profile-{choice.id}",
                                classes="wizard-choice",
                            )
                    yield Static("", id="wizard-profile-summary")
                with VerticalScroll(id=step_pane_id(WizardStep.MODE), classes="wizard-pane"):
                    yield Static("[b]Onde instalar?[/b]", classes="wizard-question")
                    with Horizontal(id="wizard-mode-buttons"):
                        for mode in InstallLocationMode:
                            yield Button(
                                MODE_LABELS[mode][0],
                                id=f"wizard-mode-{mode.value}",
                                classes="wizard-choice",
                            )
                    yield Static("", id="wizard-mode-summary")
                    yield Input(placeholder=r"D:\PepitosAIHub", id="wizard-custom-root")
                    yield Static("", id="wizard-custom-error")
                yield Static("", id=step_pane_id(WizardStep.REVIEW), classes="wizard-pane")
                yield Static("", id=step_pane_id(WizardStep.PROGRESS), classes="wizard-pane")
                with VerticalScroll(id=step_pane_id(WizardStep.CONFLICT), classes="wizard-pane"):
                    yield Static("", id="wizard-conflict-report")
                    yield Static("", id="wizard-conflict-choices")
                    with Horizontal(id="wizard-conflict-buttons"):
                        yield Button(
                            "Instalar ao lado", id="wizard-conflict-alongside", variant="primary"
                        )
                        yield Button("Parar conflitos e instalar", id="wizard-conflict-stop")
                        yield Button("Cancelar", id="wizard-conflict-abort")
                with VerticalScroll(
                    id=step_pane_id(WizardStep.CONFLICT_CONFIRM), classes="wizard-pane"
                ):
                    yield Static("", id="wizard-confirm-text")
                    with Horizontal(id="wizard-confirm-buttons"):
                        yield Button(
                            "Confirmar e continuar", id="wizard-confirm-stop", variant="primary"
                        )
                        yield Button("Voltar", id="wizard-confirm-back")
                yield Static("", id=step_pane_id(WizardStep.DONE), classes="wizard-pane")
            with Horizontal(id="wizard-actions"):
                yield Button("Pular por agora", id="wizard-skip")
                yield Button("Voltar", id="wizard-back")
                yield Button("Continuar", id="wizard-next", variant="primary")
                yield Button("Abrir Open WebUI", id="wizard-open-webui", variant="primary")
                yield Button("Ir para o Dashboard", id="wizard-finish")
                yield Button("Tentar novamente", id="wizard-retry", variant="primary")
        yield Static("Pepito's AI Hub  v0.1", id="wizard-footer")

    def on_mount(self) -> None:
        self._repaint()

    def on_resize(self) -> None:
        # A transfer bar is sized to the pane it lives in, so a terminal
        # resize has to redraw it rather than leave the old width in place.
        self._repaint()

    @staticmethod
    def _pane_width(pane: Static) -> int:
        return pane.size.width or DEFAULT_TRANSFER_WIDTH

    # --- state --------------------------------------------------------------

    def _set_state(self, state: WizardState) -> None:
        self.state = state
        self._repaint()

    def go_to(self, step: WizardStep) -> None:
        self._set_state(replace(self.state, step=step))

    def select_profile(self, profile_id: str) -> None:
        if any(choice.id == profile_id for choice in self.choices):
            self._set_state(replace(self.state, profile_id=profile_id))

    def select_mode(self, mode: InstallLocationMode) -> None:
        error = (
            validate_custom_root(self.state.custom_root)
            if mode is InstallLocationMode.CUSTOM
            else ""
        )
        self._set_state(replace(self.state, mode=mode, custom_error=error))

    def set_custom_root(self, value: str) -> None:
        self._set_state(
            replace(self.state, custom_root=value, custom_error=validate_custom_root(value))
        )

    def show_operation(self, view: OperationView | None) -> None:
        """The app relays the installation here; the wizard only reads it."""
        self.operation = view
        if view is None:
            return
        if view.terminal:
            self.conflict = None
            if self.state.step in (
                WizardStep.PROGRESS,
                WizardStep.CONFLICT,
                WizardStep.CONFLICT_CONFIRM,
            ):
                self.go_to(WizardStep.DONE)
                return
        self._repaint()

    def show_conflict(self, report: ConflictPreflightReport) -> None:
        """The application paused before mutating anything; present the choice.

        Preflight runs after the person requests installation but before an
        operation id exists. A successful resolution starts the real operation.
        """
        if self.state.step not in (WizardStep.PROGRESS, WizardStep.CONFLICT):
            return
        self.conflict = report
        self._conflict_error = ""
        self.go_to(WizardStep.CONFLICT)

    def apply_resolution(self, result: ConflictResolutionResult) -> None:
        """What the application decided about the chosen policy."""
        if result.status is ConflictResolutionStatus.ABORTED:
            self.conflict = None
            self._conflict_error = ""
            self.go_to(WizardStep.REVIEW)
            return
        if result.status is ConflictResolutionStatus.FAILED:
            self._conflict_error = result.message
            self.go_to(WizardStep.CONFLICT)
            return
        # PROCEED: the app now submits the preserved install selection.
        self.conflict = None
        self._conflict_error = ""
        if self.state.step is not WizardStep.PROGRESS:
            self.go_to(WizardStep.PROGRESS)
        else:
            self._repaint()

    def show_refusal(self, message: str) -> None:
        """The app could not start the installation; stay on the review step and say so."""
        self.query_one(step_pane_id(WizardStep.REVIEW, prefix="#"), Static).update(
            self._review_text() + f"\n\n[red]{escape(message)}[/red]"
        )
        self._set_buttons(review_refused=True)

    # --- rendering ----------------------------------------------------------

    def _chosen(self) -> ProfileChoice | None:
        return next((c for c in self.choices if c.id == self.state.profile_id), None)

    def _review_text(self) -> str:
        chosen = self._chosen()
        mode_label = MODE_LABELS[self.state.mode][0]
        lines = [
            "[b]Revisar[/b]",
            "",
            f"Perfil: [b]{escape(chosen.name if chosen else '-')}[/b]",
            f"Local: [b]{escape(mode_label)}[/b]",
        ]
        if self.state.mode is InstallLocationMode.CUSTOM:
            lines.append(f"Pasta: {escape(self.state.custom_root.strip())}")
        lines.extend(
            [
                "",
                "Ao instalar, o Hub baixa e configura tudo sozinho.",
                "Pode demorar alguns minutos.",
            ]
        )
        return "\n".join(lines)

    def _repaint(self) -> None:
        state = self.state
        self.query_one("#wizard-steps", ContentSwitcher).current = step_pane_id(state.step)
        position = (
            CHOICE_STEPS.index(state.step) + 1 if state.step in CHOICE_STEPS else len(CHOICE_STEPS)
        )
        if state.step is WizardStep.DONE and self.operation is not None and self.operation.terminal:
            # The frozen, unambiguous heading: success and failure never share a title.
            title = installation_outcome_heading(self.operation.state)
        else:
            title = {
                WizardStep.WELCOME: "Primeiro uso",
                WizardStep.PROFILE: "Perfil",
                WizardStep.MODE: "Local de instalação",
                WizardStep.REVIEW: "Revisar",
                WizardStep.PROGRESS: "Instalando",
                WizardStep.CONFLICT: "Conflito encontrado",
                WizardStep.CONFLICT_CONFIRM: "Confirmar",
                WizardStep.DONE: "Concluído",
            }[state.step]
        if state.step in CHOICE_STEPS:
            title = f"{title}  -  passo {position} de {len(CHOICE_STEPS)}"
        self.query_one("#wizard-title", Static).update(f"[b]{escape(title)}[/b]")

        chosen = self._chosen()
        for choice in self.choices:
            self.query_one(f"#wizard-profile-{choice.id}", Button).variant = (
                "primary" if choice.id == state.profile_id else "default"
            )
        self.query_one("#wizard-profile-summary", Static).update(
            escape(chosen.summary) if chosen else "Escolha um perfil para continuar."
        )

        for mode in InstallLocationMode:
            self.query_one(f"#wizard-mode-{mode.value}", Button).variant = (
                "primary" if mode is state.mode else "default"
            )
        self.query_one("#wizard-mode-summary", Static).update(escape(MODE_LABELS[state.mode][1]))
        custom = state.mode is InstallLocationMode.CUSTOM
        self.query_one("#wizard-custom-root", Input).display = custom
        self.query_one("#wizard-custom-error", Static).display = custom
        self.query_one("#wizard-custom-error", Static).update(
            f"[red]{escape(state.custom_error)}[/red]" if state.custom_error else ""
        )

        self.query_one(step_pane_id(WizardStep.REVIEW, prefix="#"), Static).update(
            self._review_text()
        )
        progress_pane = self.query_one(step_pane_id(WizardStep.PROGRESS, prefix="#"), Static)
        progress_text = render_operation(
            self.operation,
            idle_text="Aguardando o início da instalação.",
            width=self._pane_width(progress_pane),
        )
        progress_pane.update(progress_text)
        done_heading = (
            installation_outcome_heading(self.operation.state)
            if self.operation is not None and self.operation.terminal
            else None
        )
        done_pane = self.query_one(step_pane_id(WizardStep.DONE, prefix="#"), Static)
        done_text = render_operation(
            self.operation, heading=done_heading, width=self._pane_width(done_pane)
        )
        done_pane.update(done_text)

        self._repaint_conflict()
        self._set_buttons()

    def _repaint_conflict(self) -> None:
        for button_id in (
            "wizard-conflict-alongside",
            "wizard-conflict-stop",
            "wizard-conflict-abort",
            "wizard-confirm-stop",
            "wizard-confirm-back",
        ):
            self.query_one(f"#{button_id}", Button).disabled = False
        report = self.conflict
        if report is None:
            self.query_one("#wizard-conflict-report", Static).update("")
            self.query_one("#wizard-conflict-choices", Static).update("")
            self.query_one("#wizard-confirm-text", Static).update("")
            return
        report_text = render_conflict_report(report)
        if self._conflict_error:
            report_text = f"{report_text}\n\n[red]{escape(self._conflict_error)}[/red]"
        self.query_one("#wizard-conflict-report", Static).update(report_text)
        self.query_one("#wizard-conflict-choices", Static).update(render_conflict_choices(report))
        self.query_one("#wizard-confirm-text", Static).update(render_stop_confirmation(report))

        available = report.available_policies
        recommended = report.recommended_policy
        alongside = self.query_one("#wizard-conflict-alongside", Button)
        alongside.display = ExternalConflictPolicy.INSTALL_ALONGSIDE in available
        alongside.variant = (
            "primary" if recommended is ExternalConflictPolicy.INSTALL_ALONGSIDE else "default"
        )
        stop = self.query_one("#wizard-conflict-stop", Button)
        stop.display = ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL in available
        stop.variant = (
            "primary"
            if recommended is ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            else "default"
        )
        abort = self.query_one("#wizard-conflict-abort", Button)
        abort.variant = "primary" if recommended is ExternalConflictPolicy.ABORT else "default"

    def _set_buttons(self, *, review_refused: bool = False) -> None:
        state = self.state
        view = self.operation
        show = {
            "wizard-skip": state.step is WizardStep.WELCOME,
            "wizard-back": state.step in (WizardStep.PROFILE, WizardStep.MODE, WizardStep.REVIEW),
            "wizard-next": state.step in CHOICE_STEPS,
            "wizard-open-webui": state.step is WizardStep.DONE and bool(view and view.succeeded),
            "wizard-finish": state.step is WizardStep.DONE and bool(view and view.succeeded),
            "wizard-retry": state.step is WizardStep.DONE and bool(view and view.failed),
        }
        for button_id, visible in show.items():
            self.query_one(f"#{button_id}", Button).display = visible
        nxt = self.query_one("#wizard-next", Button)
        nxt.label = "Instalar" if state.step is WizardStep.REVIEW else "Continuar"
        nxt.disabled = not state.can_continue or review_refused

    # --- input --------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        event.stop()
        if button_id.startswith("wizard-profile-"):
            self.select_profile(button_id.removeprefix("wizard-profile-"))
        elif button_id.startswith("wizard-mode-"):
            self.select_mode(InstallLocationMode(button_id.removeprefix("wizard-mode-")))
        elif button_id == "wizard-back":
            self.action_back()
        elif button_id == "wizard-next":
            self.action_next()
        elif button_id == "wizard-open-webui":
            self.post_message(self.OpenWebUiRequested())
        elif button_id in ("wizard-finish", "wizard-skip"):
            self.post_message(self.Finished())
        elif button_id == "wizard-retry":
            self.operation = None
            self.go_to(WizardStep.REVIEW)
        elif button_id == "wizard-conflict-alongside":
            self._choose_policy(ExternalConflictPolicy.INSTALL_ALONGSIDE)
        elif button_id == "wizard-conflict-stop":
            self.go_to(WizardStep.CONFLICT_CONFIRM)
        elif button_id == "wizard-conflict-abort":
            self._choose_policy(ExternalConflictPolicy.ABORT)
        elif button_id == "wizard-confirm-stop":
            self._choose_policy(ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL)
        elif button_id == "wizard-confirm-back":
            self.go_to(WizardStep.CONFLICT)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "wizard-custom-root":
            event.stop()
            self.set_custom_root(event.value)

    def _choose_policy(self, policy: ExternalConflictPolicy) -> None:
        """The whole outgoing command: an opaque preflight id and the policy."""
        if self.conflict is None:
            return
        selection = ConflictPolicySelection(self.conflict.preflight_id, policy)
        # Disable both action rows so a second click while the app decides
        # cannot send a second, conflicting command.
        for button_id in (
            "wizard-conflict-alongside",
            "wizard-conflict-stop",
            "wizard-conflict-abort",
            "wizard-confirm-stop",
            "wizard-confirm-back",
        ):
            self.query_one(f"#{button_id}", Button).disabled = True
        self.post_message(self.ConflictPolicyChosen(selection))

    def action_back(self) -> None:
        if self.state.step is WizardStep.CONFLICT_CONFIRM:
            self.go_to(WizardStep.CONFLICT)
            return
        index = CHOICE_STEPS.index(self.state.step) if self.state.step in CHOICE_STEPS else -1
        if index > 0:
            self.go_to(CHOICE_STEPS[index - 1])

    def action_next(self) -> None:
        if not self.state.can_continue:
            return
        if self.state.step is WizardStep.REVIEW:
            selection = self.state.selection
            if selection is None:
                return
            # Disable before asking, so a second press while the app decides
            # cannot ask again.
            self.query_one("#wizard-next", Button).disabled = True
            self.go_to(WizardStep.PROGRESS)
            self.post_message(self.InstallRequested(selection))
            return
        self.go_to(CHOICE_STEPS[CHOICE_STEPS.index(self.state.step) + 1])


__all__ = [
    "CHOICE_STEPS",
    "MODE_LABELS",
    "WizardScreen",
    "WizardState",
    "WizardStep",
    "step_pane_id",
    "validate_custom_root",
]
