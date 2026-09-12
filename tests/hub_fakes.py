"""A scripted application for the Hub screens.

Everything the TUI can ask for, answered from memory. Operations are scripted
as event lists and delivered one at a time by the test, so a screen can be
observed between any two events. Every event still passes through the frozen
reducer, which is what makes the script a valid stream rather than a fixture
the TUI merely tolerates.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from local_ai_stack.application_contracts import ServicesSnapshot, ServiceView, VramSnapshot
from local_ai_stack.components import ComponentId
from local_ai_stack.external_conflict_contracts import (
    ConflictEndpoint,
    ConflictHardBlocker,
    ConflictPolicySelection,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictKind,
    ExternalConflictOrigin,
    ExternalConflictPolicy,
    ExternalServiceConflict,
)
from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.operation_contracts import (
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationEventSink,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationSnapshot,
    OperationState,
    TransferProgress,
    advance_operation,
)
from local_ai_stack.tui.application import (
    ConflictResolutionRequired,
    HubUnavailable,
    InstallationView,
    InstallSelection,
    LaunchRefusal,
    LaunchTarget,
    OperationBusy,
    ProfileChoice,
)

SERVICE_IDS = ("ollama", "open-webui", "comfyui")

PROFILE_CHOICES = (
    ProfileChoice("capella", "Capella", "Chat e imagens. Recomendado para esta máquina.", True),
    ProfileChoice("pepe", "Pepe", "Perfil pessoal de referência.", True),
    ProfileChoice("test", "Test", "Sem modelos pesados, para experimentar o Hub.", True),
)

# The stages a real installation reports, in the words the producer uses.
INSTALL_STEPS = (
    "Verificando ambiente",
    "Instalando Ollama",
    "Instalando Open WebUI",
    "Instalando ComfyUI",
    "Baixando modelos",
    "Configurando",
    "Inicializando",
)

EPOCH = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _stamp(index: int) -> datetime:
    return EPOCH + timedelta(seconds=index)


# --- external conflicts ------------------------------------------------------

OLLAMA_ENDPOINT = ConflictEndpoint("127.0.0.1", 11434)
OLLAMA_MANAGED_ENDPOINT = ConflictEndpoint("127.0.0.1", 21434)
COMFYUI_ENDPOINT = ConflictEndpoint("127.0.0.1", 8188)
COMFYUI_MANAGED_ENDPOINT = ConflictEndpoint("127.0.0.1", 28188)
OPEN_WEBUI_ENDPOINT = ConflictEndpoint("127.0.0.1", 8080)
OPEN_WEBUI_MANAGED_ENDPOINT = ConflictEndpoint("127.0.0.1", 28080)

# Distinct endpoints per service, so combining conflicts in one report never
# collides on a managed endpoint by accident.
_DEFAULT_ENDPOINTS: dict[ComponentId, tuple[ConflictEndpoint, ConflictEndpoint]] = {
    ComponentId.OLLAMA: (OLLAMA_ENDPOINT, OLLAMA_MANAGED_ENDPOINT),
    ComponentId.COMFYUI: (COMFYUI_ENDPOINT, COMFYUI_MANAGED_ENDPOINT),
    ComponentId.OPEN_WEBUI: (OPEN_WEBUI_ENDPOINT, OPEN_WEBUI_MANAGED_ENDPOINT),
}


def alongside_conflict(
    service_id: ComponentId = ComponentId.OLLAMA,
    *,
    endpoint: ConflictEndpoint | None = None,
    managed_endpoint: ConflictEndpoint | None = None,
    can_stop_safely: bool = True,
) -> ExternalServiceConflict:
    """A conflict a real preflight would report: safe to stop, safe to work around."""
    default_endpoint, default_managed = _DEFAULT_ENDPOINTS[service_id]
    return ExternalServiceConflict(
        service_id=service_id,
        kind=ExternalConflictKind.ENDPOINT_OCCUPIED,
        origin=ExternalConflictOrigin.EXTERNAL,
        endpoint=endpoint or default_endpoint,
        reason="Um serviço externo já ocupa este endereço.",
        can_stop_safely=can_stop_safely,
        managed_endpoint=managed_endpoint if managed_endpoint is not None else default_managed,
    )


def unsafe_conflict(
    service_id: ComponentId = ComponentId.COMFYUI,
    *,
    endpoint: ConflictEndpoint | None = None,
    managed_endpoint: ConflictEndpoint | None = None,
) -> ExternalServiceConflict:
    """Alongside remains possible; stopping it does not, for lack of proof."""
    default_endpoint, default_managed = _DEFAULT_ENDPOINTS[service_id]
    return ExternalServiceConflict(
        service_id=service_id,
        kind=ExternalConflictKind.ENDPOINT_OCCUPIED,
        origin=ExternalConflictOrigin.EXTERNAL,
        endpoint=endpoint or default_endpoint,
        reason="Identidade do processo não pôde ser confirmada.",
        can_stop_safely=False,
        managed_endpoint=managed_endpoint if managed_endpoint is not None else default_managed,
    )


def conflict_report(
    *conflicts: ExternalServiceConflict,
    preflight_id: str = "preflight-1",
    hard_blockers: tuple[ConflictHardBlocker, ...] = (),
) -> ConflictPreflightReport:
    return ConflictPreflightReport(preflight_id, conflicts, hard_blockers)


def _event(
    operation_id: str,
    sequence: int,
    kind: OperationEventKind,
    state: OperationState,
    phase: OperationPhase,
    message: str,
    *,
    progress: OperationProgress | None = None,
    transfer: TransferProgress | None = None,
    diagnostic: OperationDiagnostic | None = None,
    result: OperationResult | None = None,
) -> OperationEvent:
    return OperationEvent(
        operation_id=operation_id,
        sequence=sequence,
        kind=kind,
        state=state,
        timestamp=_stamp(sequence),
        phase=phase,
        message=message,
        progress=progress,
        diagnostic=diagnostic,
        result=result,
        transfer=transfer,
    )


def transfer_event(
    operation_id: str,
    sequence: int,
    state: OperationState,
    phase: OperationPhase,
    message: str,
    transfer: TransferProgress,
    *,
    kind: OperationEventKind = OperationEventKind.PROGRESS,
    progress: OperationProgress | None = None,
    diagnostic: OperationDiagnostic | None = None,
    result: OperationResult | None = None,
) -> OperationEvent:
    """A public constructor for byte-transfer events. Everything else uses ``_event``."""
    return _event(
        operation_id,
        sequence,
        kind,
        state,
        phase,
        message,
        progress=progress,
        transfer=transfer,
        diagnostic=diagnostic,
        result=result,
    )


def install_script(operation_id: str, *, fail_at: str | None = None) -> list[OperationEvent]:
    """The events a full installation produces, or the ones up to a failure."""
    state = OperationEventKind.STATE
    events: list[OperationEvent] = [
        _event(
            operation_id,
            1,
            state,
            OperationState.PREPARING,
            OperationPhase.PREPARATION,
            INSTALL_STEPS[0],
        )
    ]
    sequence = 1
    for step in INSTALL_STEPS[1:5]:
        if step == fail_at:
            sequence += 1
            diagnostic = OperationDiagnostic(
                code="download-failed",
                message="O download não foi concluído. Verifique a conexão e tente novamente.",
                reference="install-log",
            )
            events.append(
                _event(
                    operation_id,
                    sequence,
                    OperationEventKind.RESULT,
                    OperationState.FAILED,
                    OperationPhase.EXECUTION,
                    f"Não foi possível concluir a etapa: {step}",
                    diagnostic=diagnostic,
                    result=OperationResult(
                        operation_id, OperationState.FAILED, diagnostics=(diagnostic,)
                    ),
                )
            )
            return events
        sequence += 1
        events.append(
            _event(
                operation_id,
                sequence,
                state,
                OperationState.RUNNING,
                OperationPhase.EXECUTION,
                step,
            )
        )
        if step == "Baixando modelos":
            for completed in (1, 2, 3):
                sequence += 1
                events.append(
                    _event(
                        operation_id,
                        sequence,
                        OperationEventKind.PROGRESS,
                        OperationState.RUNNING,
                        OperationPhase.EXECUTION,
                        step,
                        progress=OperationProgress(completed, 3),
                    )
                )
    for step, phase in (
        (INSTALL_STEPS[5], OperationPhase.FINALIZATION),
        (INSTALL_STEPS[6], OperationPhase.SERVICE_START),
    ):
        sequence += 1
        events.append(_event(operation_id, sequence, state, OperationState.FINALIZING, phase, step))
    sequence += 1
    events.append(
        _event(
            operation_id,
            sequence,
            OperationEventKind.RESULT,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Concluído",
            result=OperationResult(operation_id, OperationState.SUCCEEDED),
        )
    )
    return events


def lifecycle_script(operation_id: str, verb: str, *, fail: bool = False) -> list[OperationEvent]:
    """Start, stop or restart: prepare, do it, done."""
    events = [
        _event(
            operation_id,
            1,
            OperationEventKind.STATE,
            OperationState.PREPARING,
            OperationPhase.PREPARATION,
            f"Preparando: {verb}",
        ),
        _event(
            operation_id,
            2,
            OperationEventKind.STATE,
            OperationState.RUNNING,
            OperationPhase.SERVICE_START,
            f"{verb} serviços",
        ),
    ]
    if fail:
        diagnostic = OperationDiagnostic(
            code="service-refused",
            message="Um serviço não respondeu a tempo.",
        )
        events.append(
            _event(
                operation_id,
                3,
                OperationEventKind.RESULT,
                OperationState.FAILED,
                OperationPhase.SERVICE_START,
                "Não foi possível concluir",
                diagnostic=diagnostic,
                result=OperationResult(
                    operation_id, OperationState.FAILED, diagnostics=(diagnostic,)
                ),
            )
        )
    else:
        events.append(
            _event(
                operation_id,
                3,
                OperationEventKind.RESULT,
                OperationState.SUCCEEDED,
                OperationPhase.FINALIZATION,
                "Concluído",
                result=OperationResult(operation_id, OperationState.SUCCEEDED),
            )
        )
    return events


class _Subscription:
    def __init__(self, owner: FakeHubApplication, sink: OperationEventSink) -> None:
        self._owner = owner
        self._sink = sink
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self._owner._sinks = [entry for entry in self._owner._sinks if entry is not self]

    def deliver(self, event: OperationEvent) -> None:
        if self.closed:
            return
        try:
            self._sink(event)
        except Exception as error:  # noqa: BLE001 - observer isolation is the contract
            self._owner.subscriber_errors.append(error)


@dataclass
class FakeHubApplication:
    """The application, scripted. Nothing runs; the test turns the crank."""

    ready: bool = False
    profile_id: str | None = None
    mode: InstallLocationMode = InstallLocationMode.DEFAULT
    root_display: str | None = None
    health: dict[str, str] = field(default_factory=lambda: dict.fromkeys(SERVICE_IDS, "DOWN"))
    vram_total: int | None = 12227
    vram_free: int | None = 9800
    install_fail_at: str | None = None
    lifecycle_fails: bool = False
    webui: LaunchTarget | LaunchRefusal = field(
        default_factory=lambda: LaunchTarget("open-webui", "http://127.0.0.1:8080/", "Open WebUI")
    )
    unavailable: bool = False
    profile_choices_value: tuple[ProfileChoice, ...] = PROFILE_CHOICES

    # Set before calling install() to make the application pause with a
    # resolvable (or blocked) preflight instead of running straight through.
    conflict_report: ConflictPreflightReport | None = None
    conflict_resolution_fails: bool = False

    # Set before calling install() to replace the ordinary script with a
    # hand-built one - the transfer-progress tests use this to inject events
    # install_script() itself never produces.
    custom_script: Callable[[str], list[OperationEvent]] | None = None

    # Test-facing record of everything that happened.
    calls: list[tuple[str, object]] = field(default_factory=list)
    emitted: list[OperationEvent] = field(default_factory=list)
    subscriber_errors: list[Exception] = field(default_factory=list)
    resolutions: list[ConflictPolicySelection] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._snapshot: OperationSnapshot | None = None
        self._pending: list[OperationEvent] = []
        self._active_conflict: ConflictPreflightReport | None = None
        self._sinks: list[_Subscription] = []
        self._operation_kind: str | None = None
        self._selection: InstallSelection | None = None
        self._counter = 0

    # --- read model ---------------------------------------------------------

    def current_operation(self) -> OperationSnapshot | None:
        self._check_available()
        return self._snapshot

    def subscribe(self, sink: OperationEventSink):
        self._check_available()
        subscription = _Subscription(self, sink)
        self._sinks.append(subscription)
        if self._snapshot is not None:
            subscription.deliver(self._snapshot.last_event)
        return subscription

    def services_snapshot(self) -> ServicesSnapshot:
        self._check_available()
        return ServicesSnapshot(
            _stamp(0),
            tuple(ServiceView(service_id, self.health[service_id]) for service_id in SERVICE_IDS),
        )

    def vram_snapshot(self) -> VramSnapshot:
        self._check_available()
        return VramSnapshot(_stamp(0), self.vram_total, self.vram_free)

    # --- commands -----------------------------------------------------------

    def installation(self) -> InstallationView:
        self._check_available()
        return InstallationView(self.ready, self.profile_id, self.mode, self.root_display)

    def profile_choices(self) -> tuple[ProfileChoice, ...]:
        self._check_available()
        return self.profile_choices_value

    def install(self, selection: InstallSelection) -> str:
        self._check_available()
        self.calls.append(("install", selection))
        self._selection = selection
        if self.conflict_report is not None:
            self._active_conflict = self.conflict_report
            raise ConflictResolutionRequired(self.conflict_report)
        operation_id = self._begin("install")
        self._active_conflict = None
        if self.custom_script is not None:
            self._pending = self.custom_script(operation_id)
        else:
            self._pending = install_script(operation_id, fail_at=self.install_fail_at)
        return operation_id

    def current_conflict_preflight(self) -> ConflictPreflightReport | None:
        self._check_available()
        return self._active_conflict

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        self._check_available()
        if self._active_conflict is None:
            raise ValueError("no active conflict to resolve")
        # Same validation a real resolver performs against its own snapshot.
        self._active_conflict.validate_selection(selection)
        self.resolutions.append(selection)
        if selection.policy is ExternalConflictPolicy.ABORT:
            self._active_conflict = None
            self._pending = []
            return ConflictResolutionResult(
                selection.preflight_id,
                selection.policy,
                ConflictResolutionStatus.ABORTED,
                "Instalação cancelada.",
            )
        if self.conflict_resolution_fails:
            return ConflictResolutionResult(
                selection.preflight_id,
                selection.policy,
                ConflictResolutionStatus.FAILED,
                "Não foi possível aplicar a política escolhida.",
            )
        self._active_conflict = None
        self.conflict_report = None
        return ConflictResolutionResult(
            selection.preflight_id,
            selection.policy,
            ConflictResolutionStatus.PROCEED,
            "Continuando a instalação.",
        )

    def start_stack(self) -> str:
        return self._lifecycle("start", "Iniciando")

    def stop_stack(self) -> str:
        return self._lifecycle("stop", "Parando")

    def restart_stack(self) -> str:
        return self._lifecycle("restart", "Reiniciando")

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal:
        self._check_available()
        self.calls.append(("open_webui_target", None))
        return self.webui

    # --- the crank ----------------------------------------------------------

    def step(self) -> OperationEvent | None:
        """Deliver the next scripted event, or nothing if the script is done."""
        if not self._pending:
            return None
        event = self._pending.pop(0)
        self._snapshot = advance_operation(self._snapshot, event)
        self.emitted.append(event)
        for subscription in list(self._sinks):
            subscription.deliver(event)
        if event.state.terminal:
            self._settle(event)
        return event

    def run_to_end(self) -> list[OperationEvent]:
        delivered = []
        while (event := self.step()) is not None:
            delivered.append(event)
        return delivered

    @property
    def subscribers(self) -> int:
        return len(self._sinks)

    # --- internals ----------------------------------------------------------

    def _check_available(self) -> None:
        if self.unavailable:
            raise HubUnavailable("A aplicação não está disponível.")

    def _begin(self, kind: str) -> str:
        if self._snapshot is not None and not self._snapshot.last_event.state.terminal:
            raise OperationBusy("Já existe uma operação em andamento.")
        self._counter += 1
        self._operation_kind = kind
        self._snapshot = None
        return f"{kind}-{self._counter}"

    def _lifecycle(self, kind: str, verb: str) -> str:
        self._check_available()
        operation_id = self._begin(kind)
        self.calls.append((kind, None))
        self._pending = lifecycle_script(operation_id, verb, fail=self.lifecycle_fails)
        return operation_id

    def _settle(self, event: OperationEvent) -> None:
        """What the world looks like once an operation ends, as a fake decides it."""
        succeeded = event.state is OperationState.SUCCEEDED
        if self._operation_kind == "install" and succeeded and self._selection is not None:
            self.ready = True
            self.profile_id = self._selection.profile_id
            self.mode = self._selection.mode
            self.root_display = (
                self._selection.custom_root
                if self._selection.mode is InstallLocationMode.CUSTOM
                else None
            )
            self.health = dict.fromkeys(SERVICE_IDS, "UP")
        elif self._operation_kind in {"start", "restart"} and succeeded:
            self.health = dict.fromkeys(SERVICE_IDS, "UP")
        elif self._operation_kind == "stop" and succeeded:
            self.health = dict.fromkeys(SERVICE_IDS, "DOWN")


def ready_application(**overrides: object) -> FakeHubApplication:
    """A machine where Capella is installed and every service is up."""
    values: dict[str, object] = {
        "ready": True,
        "profile_id": "capella",
        "health": dict.fromkeys(SERVICE_IDS, "UP"),
    }
    values.update(overrides)
    return FakeHubApplication(**values)  # type: ignore[arg-type]


def sequence_of(events: Sequence[OperationEvent]) -> list[int]:
    return [event.sequence for event in events]
