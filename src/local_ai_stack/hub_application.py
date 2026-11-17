"""Product composition: the Hub protocol over the existing stack application.

The adapter schedules commands, persists the selection and projects answers.
Planning, installation, ownership, readiness and endpoints remain in the backend.

One installation is bound at a time, and binding it is a transaction. Choosing
another profile or another folder prepares a *candidate*: its own backend, its
own configuration, checked on its own. Until that candidate commits -- which
only a completed installation or an approved switch does -- the installation
the Dashboard shows is the installation Start, Stop and Restart reach.
Cancelling at the conflict preflight, failing to validate and failing to
install all leave the previous binding exactly as it was.

What survives the Hub closing lives in :mod:`installations`: the active
installation, the ones it knows about, and what each last did.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .application_contracts import ServicesSnapshot, ServiceView, VramSnapshot
from .chat_choice import (
    ChatChoice,
    ChatChoiceError,
    ImportedModel,
    allowed,
    chat_options,
    choice_path,
    parse_reference,
    write_choice,
)
from .compatibility import evaluate_compatibility
from .config import ConfigError, InstalledConfig, config_path, loads
from .deletion import delete_tree, deletion_refusal, first_link_inside
from .external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictPolicy,
)
from .external_conflicts import ConflictResolutionRequired as _BackendConflictRequired
from .installation import (
    InstallLocation,
    InstallLocationMode,
    ResolvedInstallation,
    resolve_installation,
)
from .installation_control import (
    CANCELLED_MESSAGE,
    InstallationCancelled,
    InstallationControl,
)
from .installations import (
    FAILURE,
    INSTALL,
    REPAIR,
    STATE_FILENAME,
    HubState,
    KnownInstallation,
    read_state,
    root_key,
    write_installed_config,
    write_state,
)
from .layout import StackLayout
from .model_catalog import model_catalog
from .models import ModelContractError, ModelId
from .ollama_models import STORE_UNREADABLE
from .operation_contracts import (
    OperationEvent,
    OperationEventKind,
    OperationEventSink,
    OperationPhase,
    OperationResult,
    OperationSnapshot,
    OperationState,
    advance_operation,
)
from .operations import OperationStream, diagnostic, safe_text, utc_now
from .profiles import CHAT_PRIMARY_ROLE, Profile, load_profiles, offered_profiles
from .stack_application import StackApplication, StackDependencies
from .storage import enough_space, free_space, model_download_estimate
from .tui.application import (
    COMPATIBLE,
    INSUFFICIENT,
    RECOMMENDED,
    UNAVAILABLE,
    ConflictResolutionRequired,
    DeletePlan,
    HubError,
    HubUnavailable,
    InstallationCheck,
    InstallationSummary,
    InstallationView,
    InstallSelection,
    LaunchRefusal,
    LaunchTarget,
    OperationBusy,
    ProfileChoice,
    StorageEstimate,
    SwitchPlan,
    SwitchRequiresStop,
)


@dataclass
class _Candidate:
    """An installation being prepared. None of it is the Hub's truth until it commits."""

    selection: InstallSelection
    installation: ResolvedInstallation
    backend: StackApplication
    config: InstalledConfig

    @property
    def root(self) -> Path:
        return self.installation.root


@dataclass(frozen=True)
class _InstallationAttempt:
    """Session-only retry identity, captured independently of the active binding."""

    operation_id: str
    candidate: _Candidate
    kind: str


def _succeeded(result: object) -> bool:
    """Whether an operation result says it succeeded, however the backend spells it."""
    state = getattr(result, "state", None)
    return str(getattr(state, "value", state)) == "succeeded"


# Said when the machine is not clear for the installation being switched to.
HANDOVER_REFUSED = (
    "Não foi possível trocar de instalação porque os serviços necessários ainda estão em uso."
)
# Said when a check could not look at the models because Ollama is stopped.
UNCHECKED_MODELS = (
    "Com o Ollama parado, os modelos não foram conferidos. "
    "Inicie a stack e verifique de novo para conferi-los."
)
# Said when a settle step failed in a way that has no message of its own.
UNSETTLED = "Não foi possível concluir a operação. Nada foi alterado."
# How often the Hub looks for Open WebUI's first account while an integration waits.
INTEGRATION_POLL_SECONDS = 3.0
# The services one can start, stop or restart on their own.
_SERVICE_IDS = frozenset({"ollama", "open-webui", "comfyui"})


def _pending(status) -> tuple[tuple[str, ...], bool]:
    """What an installation still needs, and whether its models could not be read.

    With Ollama stopped its model store cannot be read, so every model check
    says "not installed": that is an unknown, not something missing. Those
    items are set aside -- the second value says whether there were any --
    and everything else pending still counts.
    """
    ollama_up = any(item.service_id == "ollama" and item.up for item in status.components)
    unread = False
    pending: list[str] = []
    for item in status.pending_work:
        if not ollama_up and STORE_UNREADABLE in item:
            unread = True
            continue
        pending.append(item)
    return tuple(pending), unread


def _installed(status) -> bool:
    """Whether the installation is installed, as far as can be told right now.

    A stopped stack is still an installed one: models nobody could look at
    because Ollama is stopped do not make it "not installed".
    """
    return bool(status.installation_ready) or not _pending(status)[0]


class _Ending:
    """One operation's events on their way to the screens, its ending held back.

    The backend's terminal event says what the backend did. What that means
    for the Hub -- committed, recorded, refused -- is decided after it, so the
    ending reaches the screens only once that decision is final and the Hub is
    idle again: whoever reads the Hub on seeing it reads the settled truth.
    When the Hub refused what the backend did, the ending is rewritten to say
    so; when the backend never produced one, one is made from its result.
    Every launched operation ends exactly once.
    """

    def __init__(self, hub: HubApplicationAdapter, operation_id: str) -> None:
        self.hub = hub
        self.operation_id = operation_id
        self.snapshot: OperationSnapshot | None = None
        self.held: OperationEvent | None = None

    def emit(self, event: OperationEvent) -> None:
        if event.state.terminal:
            self.held = event
            return
        self._deliver(event)

    def cancelled(self) -> None:
        if (
            self.held is not None
            and self.held.result is not None
            and any(item.code == "cancelled" for item in self.held.result.diagnostics)
        ):
            return
        phase = (
            self.snapshot.last_event.phase
            if self.snapshot is not None
            else OperationPhase.PREPARATION
        )
        if self.snapshot is None:
            self._made(None, CANCELLED_MESSAGE)
        self.held = self._terminal(
            OperationState.FAILED,
            phase,
            CANCELLED_MESSAGE,
            sequence=self.held.sequence if self.held is not None else None,
            code="cancelled",
        )

    def close(self, result: object, refusal: str | None) -> None:
        ending = self.held
        if ending is None:
            ending = self._made(result, refusal)
        elif refusal is not None and ending.state is OperationState.SUCCEEDED:
            ending = self._terminal(
                OperationState.FAILED, ending.phase, refusal, sequence=ending.sequence
            )
        self._deliver(ending)

    def _made(self, result: object, refusal: str | None) -> OperationEvent:
        """An ending for a backend that returned without one."""
        if self.snapshot is None:
            self._deliver(
                OperationEvent(
                    operation_id=self.operation_id,
                    sequence=1,
                    kind=OperationEventKind.STATE,
                    state=OperationState.PREPARING,
                    timestamp=utc_now(),
                    phase=OperationPhase.PREPARATION,
                    message="Preparando",
                )
            )
        phase = (
            OperationPhase.PREPARATION if self.snapshot is None else self.snapshot.last_event.phase
        )
        if refusal is None and _succeeded(result):
            return self._terminal(OperationState.SUCCEEDED, phase, "Concluído")
        diagnostics = tuple(getattr(result, "diagnostics", ()) or ())
        message = refusal or (
            diagnostics[0].message if diagnostics else "A operação não foi concluída."
        )
        return self._terminal(OperationState.FAILED, phase, message)

    def _terminal(
        self,
        state: OperationState,
        phase: OperationPhase,
        message: str,
        *,
        sequence: int | None = None,
        code: str = "hub",
    ) -> OperationEvent:
        last = None if self.snapshot is None else self.snapshot.last_event
        number = sequence if sequence is not None else (1 if last is None else last.sequence + 1)
        text = safe_text(message)
        diagnostics = () if state is OperationState.SUCCEEDED else (diagnostic(code, text),)
        result = OperationResult(self.operation_id, state, diagnostics=diagnostics)
        return OperationEvent(
            operation_id=self.operation_id,
            sequence=number,
            kind=OperationEventKind.RESULT,
            state=state,
            timestamp=utc_now(),
            phase=phase,
            message=text,
            diagnostic=diagnostics[0] if diagnostics else None,
            result=result,
        )

    def _deliver(self, event: OperationEvent) -> None:
        with self.hub._lock:
            try:
                self.snapshot = advance_operation(self.snapshot, event)
            except ValueError:
                # The reducer is the authority, as it is for the backend's own
                # stream: an invalid transition never reaches an observer.
                return
            self.hub._snapshot = self.snapshot
            for subscription in tuple(self.hub._subscriptions):
                subscription.deliver(event)


class HubApplicationAdapter:
    def __init__(
        self,
        *,
        profiles: Sequence[Profile],
        resolve: Callable[[InstallLocation], ResolvedInstallation],
        backend_factory: Callable[[ResolvedInstallation], StackApplication],
        selection_path: Path,
        config: InstalledConfig | None = None,
        state: HubState | None = None,
    ) -> None:
        self.profiles = tuple(profiles)
        self.resolve = resolve
        self.backend_factory = backend_factory
        self.selection_path = selection_path
        if state is None:
            state = (
                HubState()
                if config is None
                else HubState(general=config.general).activating(config)
            )
        self.state = state
        self.config = state.active
        installation = resolve(
            self.config.installation if self.config else InstallLocation.default()
        )
        self.backend = backend_factory(installation)
        self._backends = {installation.root: self.backend}
        if self.config is not None:
            # Portable and default name no root of their own; the registry keeps
            # the folder they resolved to, so the list can show it.
            self.state = self.state.remembering(
                installation.root,
                self.config.installation.mode,
                profile_id=self.config.profile_id,
                stack_id=self.config.stack_id,
            )
        self._candidate: _Candidate | None = None
        # Why the last stop-and-switch did not finish, for the screen to show.
        self._switch_refusal: str | None = None
        self._catalog_specs: dict | None = None
        self._lock = threading.RLock()
        self._busy = False
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        self._closing = threading.Event()
        self._worker: threading.Thread | None = None
        # Watches for Open WebUI's first account while an integration waits on it.
        self._integration_watch: threading.Thread | None = None
        self.integration_poll_seconds = INTEGRATION_POLL_SECONDS
        self._snapshot: OperationSnapshot | None = None
        self._subscriptions: list[_Subscription] = []
        self._installation_control: InstallationControl | None = None
        self._installation_attempt: _InstallationAttempt | None = None

    def _profile_id(self) -> str:
        return self.config.profile_id if self.config else self.profiles[0].id

    def recent_custom_roots(self) -> tuple[str, ...]:
        """Custom install folders this machine attempted, newest first. At most five."""
        return self.state.general.recent_custom_roots

    def _root_display(self) -> str | None:
        """The folder every command reaches, as a screen may print it.

        Portable and default name no root of their own, so the one they
        resolved to is what a person needs to see: which installation this is.
        """
        root = self.active_root()
        if root is not None:
            return str(root)
        backend = getattr(self.backend, "installation", None)
        resolved = getattr(backend, "root", None)
        return None if resolved is None else str(resolved)

    def active_root(self) -> Path | None:
        """The folder every command reaches right now. None before the first install.

        A portable or default installation names no root of its own -- it is
        resolved where it is running -- so the folder its backend resolved to
        is its identity: for the list, for the current marker, and for the
        protection that refuses to forget the installation in use.
        """
        if self.config is None:
            return None
        named = self.config.installation.root
        if named is not None:
            return named
        installation = getattr(self.backend, "installation", None)
        resolved = getattr(installation, "root", None)
        return None if resolved is None else Path(resolved)

    def active_profile(self) -> str | None:
        return self.config.profile_id if self.config else None

    def known_installations(self) -> tuple[KnownInstallation, ...]:
        """Every installation the Hub knows, most recently used first."""
        return self.state.ordered()

    def _profile_name(self, profile_id: str | None) -> str | None:
        return next((p.name for p in self.profiles if p.id == profile_id), profile_id)

    def profile_version(self, profile_id: str | None) -> str | None:
        """The version this Hub ships of a profile; None for one it does not know."""
        return next((p.version for p in self.profiles if p.id == profile_id), None)

    def _observed_status(
        self, entry: KnownInstallation, active_status: object | None = None
    ) -> tuple[str, str, str | None]:
        """What this installation looks like right now: status, detail, profile.

        Cheap on purpose -- the folder, its configuration, and (only for the
        active one, which already has a backend) whether its services answer.
        Nothing is hashed, nothing is installed, nothing is created.
        """
        if not entry.root.exists():
            return "missing", "A pasta não está disponível.", entry.last_known_profile
        layout = StackLayout.from_root(entry.root)
        path = config_path(layout)
        config = _installed_config(path)
        if config is None:
            if path.exists():
                return "unreadable", "A configuração não pôde ser lida.", entry.last_known_profile
            return (
                "unreadable",
                "Sem a configuração do Hub nesta pasta.",
                entry.last_known_profile,
            )
        profile_id = config.profile_id if config else entry.last_known_profile
        if profile_id is not None and profile_id not in {p.id for p in self.profiles}:
            return "unreadable", f"O perfil {profile_id} não existe nesta versão.", profile_id
        active_root = self.active_root()
        if active_root is not None and root_key(active_root) == entry.key:
            try:
                status = (
                    active_status
                    if active_status is not None
                    else self.backend.status(self._profile_id())
                )
            except Exception:  # noqa: BLE001 -- a list row never fails the screen
                return "unknown", "", profile_id
            if status.stack_up:
                return "running", "", profile_id
            return ("ready", "", profile_id) if _installed(status) else ("stopped", "", profile_id)
        return "ready", "", profile_id

    def installation_summaries(
        self, *, active_status: object | None = None
    ) -> tuple[InstallationSummary, ...]:
        """Every known installation as a row, active one first, then most recent.

        ``active_status`` is an observation of the active backend already made by
        the caller (see :meth:`runtime_status`), so one screen probes services once.
        """
        active_root = self.active_root()
        rows = []
        for entry in self.state.ordered():
            status, detail, profile_id = self._observed_status(entry, active_status)
            rows.append(
                InstallationSummary(
                    root=entry.root,
                    profile_id=profile_id,
                    profile_name=self._profile_name(profile_id),
                    mode=entry.mode,
                    status=status,
                    active=active_root is not None and root_key(active_root) == entry.key,
                    last_used_at=entry.last_used_at,
                    detail=detail,
                )
            )
        return tuple(sorted(rows, key=lambda row: not row.active))

    # --- deleting an installation ---------------------------------------------------

    def _deletion_target(self, root: Path) -> tuple[DeletePlan, _Candidate | None]:
        """Judge one known installation for deletion. Reads; changes nothing."""
        root = Path(root)
        entry = self.state.entry_for(root)
        name = self._profile_name(entry.last_known_profile if entry else None) or "Instalação"
        active = self.active_root()
        is_active = active is not None and root_key(active) == root_key(root)

        def refused(reason: str) -> tuple[DeletePlan, None]:
            return DeletePlan(root, name, active=is_active, refusal=reason), None

        others = [item.root for item in self.state.known if item.key != root_key(root)]
        reason = deletion_refusal(root, state_file=self.selection_path, other_installations=others)
        if reason is not None:
            return refused(reason)
        if entry is None:
            return refused("Esta pasta não é uma instalação que o Hub conheça.")
        if not root.is_dir():
            return refused(
                "A pasta desta instalação não existe mais. Você pode removê-la da lista."
            )
        config = _installed_config(config_path(StackLayout.from_root(root)))
        if config is None:
            return refused("Esta pasta não tem a configuração do Hub e não será apagada por aqui.")
        name = self._profile_name(config.profile_id) or name
        link = first_link_inside(root)
        if link is not None:
            return refused(
                f"Esta instalação contém um atalho para outro lugar ({link}) e não será "
                "apagada por aqui."
            )
        try:
            target = self._target(root, config)
            surviving = tuple(target.backend.surviving_services())
            status = target.backend.status(config.profile_id)
        except Exception:  # noqa: BLE001 -- an unreadable answer is not a proof of rest
            return refused(
                "Não foi possível confirmar se os serviços desta instalação estão parados."
            )
        running = bool(surviving) or any(
            item.up and item.ownership == "managed" for item in status.components
        )
        return DeletePlan(root, name, active=is_active, must_stop=running), target

    def plan_delete(self, root: Path | str) -> DeletePlan:
        """What deleting this installation would mean, for the confirmation to show."""
        with self._lock:
            plan, _ = self._deletion_target(Path(root))
        return plan

    def delete_installation(self, root: Path | str, *, stop: bool = False) -> str:
        """Delete an installation's folder, then forget it. One operation.

        Validated again, here, whatever the confirmation said: a refusal is
        raised before anything is touched. Running managed services are
        stopped only when ``stop`` says the person asked for it, and only
        processes proved to be this installation's; the deletion then waits
        for the registry to show none of them alive. The entry leaves the
        list only after the folder is gone; a deletion that fails keeps it,
        and says so. The installation in use can be deleted: nothing is
        selected in its place.
        """
        with self._lock:
            plan, target = self._deletion_target(Path(root))
            if not plan.ok or target is None:
                raise HubError(plan.refusal or "Esta instalação não pode ser apagada.")
            if plan.must_stop and not stop:
                raise HubError(
                    "Os serviços desta instalação estão em execução. Pare-os antes de apagar."
                )
            operation_id = self._reserve()
        folder = plan.root
        backend, profile_id = target.backend, target.config.profile_id

        def run(emit) -> object:
            stream = OperationStream(operation_id, emit=emit)
            stream.begin("Verificando instalação")
            if plan.must_stop:
                stream.advance(
                    OperationState.RUNNING,
                    OperationPhase.RESOURCE_RELEASE,
                    "Parando os serviços desta instalação",
                )
                stopped = backend.stop_stack(uuid4().hex, profile_id)
                try:
                    surviving = tuple(backend.surviving_services())
                except Exception:  # noqa: BLE001 -- an unreadable registry proves nothing
                    surviving = ("?",)
                if not _succeeded(stopped) or surviving:
                    return stream.fail(
                        OperationPhase.RESOURCE_RELEASE,
                        "Os serviços desta instalação não pararam. Nada foi apagado.",
                        [diagnostic("delete-not-stopped", ", ".join(surviving) or "stop failed")],
                    )
            stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "Apagando arquivos")
            failures = delete_tree(folder)
            if failures:
                return stream.fail(
                    OperationPhase.EXECUTION,
                    "Não foi possível apagar todos os arquivos. A instalação continua na lista.",
                    [diagnostic("delete-failed", item) for item in failures[:4]],
                )
            return stream.succeed(OperationPhase.FINALIZATION, "Instalação apagada")

        def settle(result: object) -> str | None:
            if not _succeeded(result):
                return None
            try:
                self._forget_deleted(folder)
            except HubError:
                return (
                    "A instalação foi apagada, mas a lista não pôde ser atualizada. "
                    "Use Remover da lista."
                )
            return None

        return self._launch(operation_id, run, settle)

    def _forget_deleted(self, root: Path) -> None:
        """The folder is gone: its entry goes, and if it was in use, nothing is in use."""
        active = self.active_root()
        was_active = active is not None and root_key(active) == root_key(root)
        state = replace(
            self.state,
            active=None if was_active else self.state.active,
            known=tuple(item for item in self.state.known if item.key != root_key(root)),
        )
        # Raises when the disk refuses; the folder is gone either way, and the
        # entry left behind shows as a missing folder, forgettable by hand.
        self._persist(state)
        self._backends = {
            key: value for key, value in self._backends.items() if root_key(key) != root_key(root)
        }
        if was_active:
            self.config = None
            default = self.resolve(InstallLocation.default())
            self.backend = self._backends.get(default.root) or self.backend_factory(default)
            self._backends[default.root] = self.backend

    def last_switch_refusal(self) -> str | None:
        """Why the last stop-and-switch did not happen. Read once, then cleared."""
        refusal, self._switch_refusal = self._switch_refusal, None
        return refusal

    def forget_installation(self, root: Path | str) -> None:
        """Remove the Hub's shortcut to an installation. Deletes nothing, stops nothing."""
        with self._lock:
            active = self.active_root()
            if active is not None and root_key(active) == root_key(root):
                raise HubError(
                    "Esta é a instalação em uso. Troque de instalação antes de removê-la da lista."
                )
            self._persist(self.state.forgetting(root))

    # --- switching, as a transaction ---------------------------------------------

    def plan_switch(self, root: Path | str) -> SwitchPlan:
        """What switching would mean. Reads; changes nothing, stops nothing."""
        entry = self.state.entry_for(root)
        target = Path(root)
        mode = entry.mode if entry else InstallLocationMode.CUSTOM
        problems: list[str] = []
        profile_id = entry.last_known_profile if entry else None
        if not target.exists():
            problems.append("A pasta desta instalação não está disponível.")
        else:
            config = _installed_config(config_path(StackLayout.from_root(target)))
            if config is None:
                problems.append(
                    "Esta pasta parece uma instalação, mas não tem a configuração do Hub."
                    if _looks_installed(target)
                    else "Esta pasta não contém uma instalação que o Hub reconheça."
                )
            else:
                profile_id = config.profile_id
                if profile_id not in {p.id for p in self.profiles}:
                    problems.append(f"O perfil {profile_id} não existe nesta versão do Hub.")
                mode = config.installation.mode
        running_profile = None
        must_stop = False
        active = self.active_root()
        if active is not None and root_key(active) != root_key(target):
            try:
                status = self.backend.status(self._profile_id())
                must_stop = bool(status.stack_up)
            except Exception:  # noqa: BLE001 -- a stack nobody can read may be running
                must_stop = True
            if must_stop:
                running_profile = self._profile_id()
        return SwitchPlan(
            root=target,
            profile_id=profile_id,
            mode=mode,
            must_stop=must_stop,
            running_profile=running_profile,
            problems=tuple(problems),
        )

    def _target(self, root: Path, config: InstalledConfig) -> _Candidate:
        """An existing installation as a candidate: its backend and its configuration.

        A custom installation's configuration names the folder it was written
        in. After the folder is moved, the folder it was found in is the truth.
        """
        location = location_of_config(config, root)
        if config.installation.mode is InstallLocationMode.CUSTOM:
            config = config.with_installation(location)
        installation = self.resolve(location)
        backend = self._backends.get(installation.root)
        if backend is None:
            backend = self.backend_factory(installation)
            self._backends[installation.root] = backend
        return _Candidate(
            InstallSelection(
                config.profile_id,
                config.installation.mode,
                str(root) if config.installation.mode is InstallLocationMode.CUSTOM else None,
            ),
            installation,
            backend,
            config,
        )

    def _bind(self, root: Path, config: InstalledConfig, *, forgetting: Path | None = None) -> None:
        """Make this installation the active one, as one transaction."""
        self._commit(self._target(root, config), forgetting=forgetting)

    def switch_installation(self, root: Path | str) -> None:
        """Make a known installation the active one. Stopped stacks only.

        Raises :class:`SwitchRequiresStop` when the installation in use is
        running: stopping it is a decision, never a side effect of choosing
        another one from a list.
        """
        with self._lock:
            plan = self.plan_switch(root)
            if not plan.ok:
                raise HubError(plan.problems[0])
            if plan.must_stop:
                raise SwitchRequiresStop(plan)
            config = _installed_config(config_path(StackLayout.from_root(Path(root))))
            if config is None:  # pragma: no cover - plan_switch already refused it
                raise HubError("Esta pasta não contém uma instalação que o Hub reconheça.")
            self._bind(Path(root), config)

    def _handover_refusal(
        self, previous: StackApplication, result: object, target: _Candidate
    ) -> str | None:
        """Why the target must not be bound yet, or None when the machine is clear for it.

        A stop that reports success is not the proof. After it, the stopped
        installation's own registry must show nothing of it still alive, and
        the target's endpoints must be free under the same rule the managed
        preflight applies before anything starts -- external services,
        leftovers and unknown listeners all count. Both answers come from the
        real runtime, and an answer that cannot be read is a refusal.
        """
        if not _succeeded(result):
            return "Não foi possível parar os serviços da instalação atual. A troca foi cancelada."
        try:
            surviving = tuple(previous.surviving_services())
            collisions = tuple(target.backend.endpoint_collisions(target.config.profile_id))
        except Exception:  # noqa: BLE001 -- an unreadable answer is not a proof of freedom
            return f"{HANDOVER_REFUSED} Não foi possível confirmar que eles pararam."
        if surviving:
            return f"{HANDOVER_REFUSED} Ainda em execução: {', '.join(surviving)}."
        if collisions:
            details = "; ".join(issue.detail for issue in collisions)
            return f"{HANDOVER_REFUSED} Detalhes: {details}."
        return None

    def stop_and_switch(self, root: Path | str, *, replacing: Path | str | None = None) -> str:
        """Stop the managed services of the installation in use, then switch to another.

        Only processes this Hub can prove it owns are stopped; anything else on
        those ports is left alone. If stopping fails, the previous installation
        stays active and bound.
        """
        with self._lock:
            plan = self.plan_switch(root)
            if not plan.ok:
                raise HubError(plan.problems[0])
            config = _installed_config(config_path(StackLayout.from_root(Path(root))))
            if config is None:  # pragma: no cover - plan_switch already refused it
                raise HubError("Esta pasta não contém uma instalação que o Hub reconheça.")
            operation_id = self._reserve()
            try:
                previous = self.backend
                profile_id = self._profile_id()
                target = self._target(Path(root), config)
            except Exception:
                self._end_operation(None)
                raise
            forgetting = None if replacing is None else Path(replacing)
            self._switch_refusal = None

            def settle(result: object) -> str | None:
                try:
                    refusal = self._handover_refusal(previous, result, target)
                    if refusal is None:
                        self._commit(target, forgetting=forgetting)
                except HubError as exc:
                    refusal = str(exc)
                self._switch_refusal = refusal
                return refusal

            return self._launch(
                operation_id,
                lambda emit: previous.stop_stack(operation_id, profile_id, emit=emit),
                settle,
                candidate=target,
            )

    def open_installation(self, root: Path | str) -> None:
        """Adopt a folder the person picked: validate it, remember it, switch to it."""
        with self._lock:
            target = Path(root)
            if not target.exists():
                raise HubError("A pasta escolhida não existe.")
            config = _installed_config(config_path(StackLayout.from_root(target)))
            if config is None:
                if _looks_installed(target):
                    raise HubError(
                        "Esta pasta parece uma instalação, mas não tem a configuração do "
                        "Hub (state\\config.yaml). Ela pode ter sido criada por outra "
                        "versão ou fora do Hub. Instale nesta pasta para refazê-la."
                    )
                raise HubError(
                    "Esta pasta não contém uma instalação do Hub. "
                    "Escolha a pasta de uma instalação ou crie uma nova."
                )
            if config.profile_id not in {p.id for p in self.profiles}:
                raise HubError(f"O perfil {config.profile_id} não existe nesta versão do Hub.")
        # A folder becomes an installation the Hub knows when it becomes the one
        # in use -- not when it was merely looked at and then abandoned.
        self.switch_installation(target)

    def relocate_installation(self, old_root: Path | str, new_root: Path | str) -> None:
        """An installation was found somewhere else: switch to it and replace its entry.

        The old entry is kept until the new folder is actually in use, so a
        relocation that is refused -- or that needs a stop nobody approved --
        leaves the list exactly as it was.
        """
        with self._lock:
            target = Path(new_root)
            if not target.exists():
                raise HubError("A pasta escolhida não existe.")
            config = _installed_config(config_path(StackLayout.from_root(target)))
            if config is None:
                raise HubError(
                    "Esta pasta parece uma instalação, mas não tem a configuração do Hub."
                    if _looks_installed(target)
                    else "Esta pasta não contém uma instalação que o Hub reconheça."
                )
            if config.profile_id not in {p.id for p in self.profiles}:
                raise HubError(f"O perfil {config.profile_id} não existe nesta versão do Hub.")
            plan = self.plan_switch(target)
            if not plan.ok:
                raise HubError(plan.problems[0])
            if plan.must_stop:
                raise SwitchRequiresStop(plan)
            self._bind(target, config, forgetting=Path(old_root))

    def verify_installation(self) -> InstallationCheck:
        """Look at the installation in use and say what is wrong with it. Changes nothing.

        Read-only by construction: the folder, its configuration, and the
        status the Dashboard already reads -- which services answer and what a
        dry run of the plan would still do. Nothing is started, downloaded,
        installed, written or hashed. With Ollama stopped its model store
        cannot be read, which is not a problem with the installation: those
        checks are reported as not done, not as failed.
        """
        with self._lock:
            if self.config is None:
                raise HubError("Nenhuma instalação está selecionada.")
            backend, profile_id, root = self.backend, self._profile_id(), self.active_root()
        if root is None or not Path(root).exists():
            return InstallationCheck(("A pasta desta instalação não está disponível.",))
        if _installed_config(config_path(StackLayout.from_root(Path(root)))) is None:
            return InstallationCheck(("A configuração do Hub nesta pasta não pôde ser lida.",))
        try:
            status = backend.status(profile_id)
        except Exception as exc:
            raise HubError("Não foi possível verificar a instalação. Tente novamente.") from exc
        problems, unread = _pending(status)
        return InstallationCheck(problems, (UNCHECKED_MODELS,) if unread else ())

    def repair(self) -> str:
        """Repair the selected installation without changing its binding or running state."""
        with self._lock:
            if self.config is None:
                raise HubError("Nenhuma instalação está selecionada.")
            operation_id = self._reserve()
            candidate = _Candidate(
                InstallSelection(
                    self.config.profile_id,
                    self.config.installation.mode,
                    str(self.active_root())
                    if self.config.installation.mode is InstallLocationMode.CUSTOM
                    else None,
                ),
                self.backend.installation,
                self.backend,
                self.config,
            )
            return self._schedule_repair(operation_id, candidate)

    def _schedule_repair(self, operation_id: str, candidate: _Candidate) -> str:
        self._installation_attempt = _InstallationAttempt(operation_id, candidate, "repair")
        control = InstallationControl(operation_id)
        self._installation_control = control

        def settle(result: object) -> None:
            succeeded = _succeeded(result)
            self._record(
                candidate.root,
                REPAIR if succeeded else FAILURE,
                profile_id=candidate.config.profile_id,
                result="succeeded" if succeeded else "",
                summary="" if succeeded else "a verificação não pôde ser concluída",
            )

        return self._launch(
            operation_id,
            lambda emit: candidate.backend.repair(
                operation_id, candidate.config.profile_id, emit=emit
            ),
            settle,
            candidate=candidate,
            control=control,
        )

    # --- the chat model of the installation in use ---------------------------------

    def _chat_context(self) -> tuple[StackApplication, str, Profile, ChatChoice]:
        if self.config is None:
            raise HubError("Nenhuma instalação está selecionada.")
        backend, profile_id = self.backend, self._profile_id()
        base = next((item for item in self.profiles if item.id == profile_id), None)
        if base is None:
            raise HubError(f"O perfil {profile_id} não existe nesta versão do Hub.")
        return backend, profile_id, base, backend.chat_choice()

    def chat_models(self) -> dict | None:
        """The chat models the installation in use can run, and which one it runs."""
        with self._lock:
            if self.config is None:
                return None
            backend, profile_id, base, choice = self._chat_context()
        catalog = backend.catalog()
        selected = backend.profile(profile_id).model_for(CHAT_PRIMARY_ROLE)
        default = base.model_for(CHAT_PRIMARY_ROLE)
        options = []
        for model_id in dict.fromkeys([*chat_options(base), *(i.id for i in choice.imported)]):
            spec = catalog.get(model_id)
            if spec is None:
                continue
            options.append(
                {
                    "id": str(model_id),
                    "reference": spec.source.reference,
                    "display_name": spec.display_name or spec.source.reference,
                    "uncensored": spec.uncensored,
                    "imported": choice.imported_spec(model_id) is not None,
                    "default": model_id == default,
                    "selected": model_id == selected,
                    "size_bytes": getattr(spec.source, "size_bytes", None),
                }
            )
        return {"selected": str(selected), "default": str(default), "options": options}

    def choose_chat_model(self, model_id: str) -> str:
        """Use another chat model the profile offers or the installation imported.

        The choice is saved and the installation repaired, which acquires the
        model and points Open WebUI at it. A repair that fails puts the previous
        choice back, so a model that never arrived is never the one in use.
        """
        with self._lock:
            backend, profile_id, base, choice = self._chat_context()
            try:
                target = ModelId(model_id)
            except ModelContractError as exc:
                raise HubError("Este modelo não está disponível para esta instalação.") from exc
            if not allowed(base, choice, target):
                raise HubError("Este modelo não está disponível para esta instalação.")
            if target == backend.profile(profile_id).model_for(CHAT_PRIMARY_ROLE):
                raise HubError("Este já é o modelo de chat desta instalação.")
            chosen = replace(
                choice, model=None if target == base.model_for(CHAT_PRIMARY_ROLE) else target
            )
            return self._apply_chat_choice(backend, profile_id, choice, chosen)

    def import_chat_model(self, reference: str) -> str:
        """Add an Ollama or Hugging Face GGUF model to this installation and use it for chat."""
        with self._lock:
            backend, profile_id, _, choice = self._chat_context()
            try:
                item = ImportedModel(parse_reference(reference))
            except ChatChoiceError as exc:
                raise HubError(str(exc)) from exc
            chosen = replace(choice.with_imported(item), model=item.id)
            return self._apply_chat_choice(backend, profile_id, choice, chosen)

    def remove_imported_chat_model(self, model_id: str) -> None:
        """Forget an imported model and free its files. Never the model in use."""
        with self._lock:
            backend, profile_id, _, choice = self._chat_context()
            try:
                target = ModelId(model_id)
            except ModelContractError as exc:
                raise HubError("Este modelo não foi importado nesta instalação.") from exc
            spec = choice.imported_spec(target)
            if spec is None:
                raise HubError("Este modelo não foi importado nesta instalação.")
            if target == backend.profile(profile_id).model_for(CHAT_PRIMARY_ROLE):
                raise HubError("Este é o modelo de chat em uso. Escolha outro antes de removê-lo.")
            try:
                backend.delete_chat_model(profile_id, spec.source.reference)
            except Exception as exc:
                raise HubError(
                    "Não foi possível apagar os arquivos do modelo. Inicie os serviços e "
                    "tente de novo."
                ) from exc
            self._write_chat_choice(backend, choice.without_imported(target))

    @staticmethod
    def _write_chat_choice(backend: StackApplication, choice: ChatChoice) -> None:
        try:
            write_choice(choice_path(backend.layout.state), choice)
        except OSError as exc:
            raise HubError("Não foi possível salvar o modelo de chat. Nada foi alterado.") from exc

    def _apply_chat_choice(
        self, backend: StackApplication, profile_id: str, previous: ChatChoice, chosen: ChatChoice
    ) -> str:
        """Save ``chosen`` and repair; a repair that fails restores ``previous``."""
        operation_id = self._reserve()
        try:
            self._write_chat_choice(backend, chosen)
        except HubError:
            self._end_operation(None)
            raise
        root = self.active_root()

        def settle(result: object) -> None:
            succeeded = _succeeded(result)
            if not succeeded:
                with contextlib.suppress(HubError):
                    self._write_chat_choice(backend, previous)
            if root is not None:
                self._record(
                    root,
                    REPAIR if succeeded else FAILURE,
                    profile_id=profile_id,
                    result="succeeded" if succeeded else "",
                    summary="modelo de chat trocado"
                    if succeeded
                    else "o modelo de chat não foi trocado",
                )

        return self._launch(
            operation_id, lambda emit: backend.repair(operation_id, profile_id, emit=emit), settle
        )

    def installation(self) -> InstallationView:
        if self.config is None:
            return InstallationView(False)
        try:
            status = self.backend.status(self.config.profile_id)
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível verificar a instalação. Tente novamente."
            ) from exc
        return InstallationView(
            _installed(status),
            self.config.profile_id,
            self.config.installation.mode,
            self._root_display(),
        )

    def runtime_status(self) -> tuple[StackApplication, str, object]:
        """Probe the active backend once: which backend and profile, and what it said.

        The probe is the expensive part of every screen (ports, model store,
        dry-run). Callers that cache it must discard it when the backend or the
        profile it was made for is no longer the active one.
        """
        backend, profile_id = self.backend, self._profile_id()
        return backend, profile_id, backend.status(profile_id)

    def projections(
        self, status: object | None = None
    ) -> tuple[InstallationView, ServicesSnapshot, VramSnapshot]:
        """One authoritative observation for everything repeated by the Dashboard."""
        try:
            if status is None:
                status = self.backend.status(self._profile_id())
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível verificar a instalação. Tente novamente."
            ) from exc
        installation = (
            InstallationView(False)
            if self.config is None
            else InstallationView(
                _installed(status),
                self.config.profile_id,
                self.config.installation.mode,
                self._root_display(),
            )
        )
        services = ServicesSnapshot(
            status.observed_at,
            tuple(ServiceView(item.service_id, item.health) for item in status.components),
        )
        return installation, services, status.gpu

    def profile_choices(self) -> tuple[ProfileChoice, ...]:
        """The profiles to offer, and how each stands against this machine.

        The largest compatible profile is recommended. Incompatible profiles
        preserve their blocker codes so the interface distinguishes low VRAM
        from a missing prerequisite such as Git.
        """
        facts = self.backend.facts()
        offered = offered_profiles(self.profiles)
        reports = {profile.id: evaluate_compatibility(facts, profile) for profile in offered}
        fitting = [profile for profile in offered if reports[profile.id].compatible]
        best = max(
            fitting,
            key=lambda profile: profile.hardware.min_vram_mib,
            default=None,
        )
        choices = []
        for profile in sorted(offered, key=lambda item: item.hardware.min_vram_mib):
            report = reports[profile.id]
            blocker_codes = tuple(issue.code for issue in report.blockers)
            standing = (
                RECOMMENDED
                if best is not None and profile.id == best.id
                else (
                    COMPATIBLE
                    if report.compatible
                    else (INSUFFICIENT if blocker_codes == ("vram_below_minimum",) else UNAVAILABLE)
                )
            )
            choices.append(
                ProfileChoice(
                    profile.id,
                    profile.name,
                    profile.description,
                    report.compatible,
                    standing=standing,
                    required_vram_gb=profile.hardware.min_vram_gb,
                    blockers=blocker_codes,
                )
            )
        return tuple(choices)

    def storage_estimate(
        self, profile_id: str, mode: InstallLocationMode, custom_root: str | None = None
    ) -> StorageEstimate:
        """What installing this profile here is known to need, and whether it fits.

        Cheap and honest: the sizes the manifests record, and the free space of
        the volume the folder would be created in. Nothing is hashed, nothing
        is created, and what is not known is reported as not known.
        """
        profile = next((item for item in self.profiles if item.id == profile_id), None)
        if profile is None:
            return StorageEstimate()
        estimate = model_download_estimate(profile, self.catalog_specs())
        try:
            location = InstallLocation(mode, Path(custom_root) if custom_root else None)
            root = self.resolve(location).root
        except Exception:  # noqa: BLE001 -- an unresolvable folder simply has no number
            return StorageEstimate(estimate.known_bytes, estimate.complete)
        space = free_space(root)
        return StorageEstimate(
            estimate.known_bytes,
            estimate.complete,
            None if space is None else space.free_bytes,
            enough_space(estimate, space),
        )

    def catalog_specs(self) -> dict:
        """The model catalog, by canonical id, read once per adapter."""
        if self._catalog_specs is None:
            try:
                self._catalog_specs = {
                    str(key): value for key, value in model_catalog().specs.items()
                }
            except Exception:  # noqa: BLE001 -- without a catalog there is simply no estimate
                self._catalog_specs = {}
        return self._catalog_specs

    def recommended_profile(self) -> str | None:
        """Which profile a screen should start on. None when nothing fits."""
        return next(
            (choice.id for choice in self.profile_choices() if choice.recommended),
            None,
        )

    def services_snapshot(self):
        try:
            return self.backend.services_snapshot(self._profile_id())
        except Exception as exc:
            raise HubUnavailable("Não foi possível consultar os serviços.") from exc

    def vram_snapshot(self):
        try:
            return self.backend.vram_snapshot()
        except Exception as exc:
            raise HubUnavailable("Não foi possível consultar a GPU.") from exc

    def current_operation(self) -> OperationSnapshot | None:
        with self._lock:
            return self._snapshot

    def installation_control(self) -> dict | None:
        """Current installation only; a stale operation can never control a later one."""
        with self._lock:
            control = self._installation_control
            return control.snapshot() if control is not None else None

    def control_installation(self, action: str, operation_id: str) -> dict:
        with self._lock:
            control = self._installation_control
            if control is None or not operation_id or control.operation_id != operation_id:
                raise HubError("Esta instalação não está mais em andamento.")
            try:
                return control.request(action)
            except ValueError as exc:
                raise HubError(str(exc)) from exc

    def retryable_installation(self) -> dict | None:
        """Only the latest failed install/repair in this session can be retried."""
        with self._lock:
            attempt = self._installation_attempt
            snapshot = self._snapshot
            if (
                self._busy
                or attempt is None
                or snapshot is None
                or snapshot.last_event.operation_id != attempt.operation_id
                or snapshot.last_event.state is not OperationState.FAILED
            ):
                return None
            return {
                "operation_id": attempt.operation_id,
                "profile_id": attempt.candidate.config.profile_id,
                "root": str(attempt.candidate.root),
                "kind": attempt.kind,
            }

    def retry_installation(self, operation_id: str) -> str:
        """Retry the captured candidate, never the current selection or default folder."""
        with self._lock:
            if self._closed:
                raise HubUnavailable("O Hub está encerrando.")
            if self._busy:
                raise OperationBusy("Aguarde a operação atual terminar.")
            retryable = self.retryable_installation()
            if retryable is None or not operation_id or retryable["operation_id"] != operation_id:
                raise HubError("Esta tentativa de instalação não pode mais ser repetida.")
            attempt = self._installation_attempt
            candidate = attempt.candidate
            # No reservation yet: a conflict decision must keep the failed id
            # valid so resolve_conflicts can be followed by the very same retry.
            pending = candidate.backend.check_conflicts(candidate.config.profile_id)
            if pending is not None:
                self._candidate = candidate
                raise ConflictResolutionRequired(pending)
            new_id = self._reserve()
            self._candidate = candidate
            if attempt.kind == "repair":
                return self._schedule_repair(new_id, candidate)
            return self._schedule_install(new_id, candidate)

    def subscribe(self, sink: OperationEventSink):
        with self._lock:
            subscription = _Subscription(self, sink)
            self._subscriptions.append(subscription)
            if self._snapshot is not None:
                subscription.deliver(self._snapshot.last_event)
            return subscription

    def _reserve(self) -> str:
        """Begin an operation: the Hub is busy from here until its one ending."""
        if self._closed:
            raise HubUnavailable("O Hub está encerrando.")
        if self._busy:
            raise OperationBusy("Aguarde a operação atual terminar.")
        self._installation_attempt = None
        self._busy = True
        self._idle.clear()
        return uuid4().hex

    def _end_operation(self, candidate: _Candidate | None) -> None:
        """End an operation: its candidate let go, the Hub not busy, idle signalled.

        The only place any of the three is undone, so no path can undo one and
        forget another. A candidate that belongs to a different attempt -- one
        still waiting on a conflict decision -- is not this operation's to drop.
        """
        if candidate is not None and self._candidate is candidate:
            self._candidate = None
        if self._installation_control is not None:
            self._installation_control.close()
            self._installation_control = None
        self._busy = False
        self._idle.set()
        self._watch_integration()

    def _watch_integration(self) -> None:
        """Keep one watcher while the running Open WebUI waits for its first account.

        A new installation's Open WebUI is integrated before anyone has made an
        account in it, so nothing is synced (``complete_open_webui_integration``).
        The person makes that account in the browser, not here, and may never
        come back to the Hub before chatting; so the Hub looks for it itself.
        Called with the lock held.
        """
        if self._closed or self._integration_watch is not None:
            return
        if not _awaits_open_webui_admin(self.backend):
            return
        watch = threading.Thread(
            target=self._integration_loop, name="hub-open-webui-integration", daemon=True
        )
        self._integration_watch = watch
        watch.start()

    def _integration_loop(self) -> None:
        """Until nothing waits: look for the account, and sync once it exists.

        Looking reads one row and holds nothing. Syncing is the Hub's only work
        for its second or so: an operation asked for meanwhile is told to wait,
        exactly as for any other, and closing the Hub waits for it to end.
        """
        while not self._closing.wait(self.integration_poll_seconds):
            with self._lock:
                backend = self.backend
                if self._closed or not _awaits_open_webui_admin(backend):
                    self._integration_watch = None
                    return
            try:
                due = backend.open_webui_integration_due()
            except Exception:  # noqa: BLE001 -- an unreadable database is looked at again
                due = False
            if not due:
                continue
            with self._lock:
                if self._closed or self._busy or backend is not self.backend:
                    continue
                self._busy = True
                self._idle.clear()
            try:
                # Whatever goes wrong, it still waits, and the next look tries again.
                with contextlib.suppress(Exception):
                    backend.complete_open_webui_integration()
            finally:
                with self._lock:
                    self._end_operation(None)
        with self._lock:
            self._integration_watch = None

    def _launch(
        self,
        operation_id: str,
        run: Callable[[OperationEventSink], object],
        settle: Callable[[object], str | None] | None = None,
        *,
        candidate: _Candidate | None = None,
        control: InstallationControl | None = None,
    ) -> str:
        """Run one reserved operation on its own thread, with exactly one ending.

        ``run`` does the backend's work and returns its result. ``settle``
        decides what that result means for the Hub -- commit, record, refuse --
        and returns why a success was not accepted, when it was not. Whatever
        either does, raising included, the operation then ends the same way:
        its candidate let go, the Hub idle, and only after that the screens'
        one terminal event, rewritten as a failure when the Hub refused.
        """
        if candidate is not None:
            self._candidate = candidate
        ending = _Ending(self, operation_id)

        def execute() -> None:
            result = None
            refusal = None
            try:
                with control.scope() if control is not None else contextlib.nullcontext():
                    try:
                        result = run(ending.emit)
                    except InstallationCancelled:
                        raise
                    except Exception:  # noqa: BLE001 -- failure still closes its control gate
                        result = None
                    if control is not None:
                        while True:
                            if _succeeded(result):
                                control.checkpoint()
                            with self._lock:
                                if control.finish(failed=not _succeeded(result)):
                                    break
            except InstallationCancelled:
                if control is not None:
                    with contextlib.suppress(Exception):
                        control.cleanup_cancelled()
                result = OperationResult(
                    operation_id,
                    OperationState.FAILED,
                    diagnostics=(diagnostic("cancelled", CANCELLED_MESSAGE),),
                )
                ending.cancelled()
            except Exception:  # noqa: BLE001 -- the ending says it failed
                result = None
            finally:
                with self._lock:
                    try:
                        refusal = settle(result) if settle is not None else None
                    except HubError as exc:
                        refusal = str(exc)
                    except Exception:  # noqa: BLE001 -- nothing may skip the end
                        refusal = UNSETTLED
                    finally:
                        self._end_operation(candidate)
                        ending.close(result, refusal)

        worker = threading.Thread(target=execute, name="hub-operation")
        try:
            worker.start()
        except Exception as exc:
            self._end_operation(candidate)
            raise HubError(UNSETTLED) from exc
        self._worker = worker
        return operation_id

    def _record(
        self,
        root: Path,
        kind: str,
        *,
        profile_id: str | None = None,
        result: str = "",
        summary: str = "",
    ) -> None:
        """Write one line of an installation's history, if the disk will have it.

        History is kept on a best-effort basis: an installation that installed
        or repaired did so whether or not the line describing it was saved. A
        success also records the version of ``profile_id`` it applied.
        """
        version = self.profile_version(profile_id) if kind != FAILURE else None
        try:
            self._persist(
                self.state.recording(
                    root, kind, result=result, summary=summary, profile_version=version
                )
            )
        except HubError:
            pass

    def _prepare(self, selection: InstallSelection) -> _Candidate:
        """Everything the selection implies, bound to nothing. Never touches the active one."""
        profile = next((p for p in self.profiles if p.id == selection.profile_id), None)
        if profile is None:
            raise HubError("Perfil não encontrado. Selecione um perfil disponível.")
        location = selection.location(profile.name)
        installation = self.resolve(location)
        backend = self._backends.get(installation.root)
        if backend is None:
            backend = self.backend_factory(installation)
            self._backends[installation.root] = backend
        base = self.config or InstalledConfig(selection.profile_id)
        config = replace(
            base,
            profile_id=selection.profile_id,
            installation=location,
            general=self.state.general,
        )
        return _Candidate(selection, installation, backend, config)

    def _remember_attempt(self, candidate: _Candidate) -> None:
        """This folder is a place an installation was attempted, whatever happens next.

        Recorded once the preflight accepted the attempt, so a root left half
        installed can still be found; a selection cancelled at the conflict
        preflight never enters the history.
        """
        state = self.state.remembering(
            candidate.root,
            candidate.config.installation.mode,
            profile_id=candidate.config.profile_id,
            stack_id=candidate.config.stack_id,
        )
        if candidate.config.installation.mode is InstallLocationMode.CUSTOM:
            state = state.with_general(
                state.general.remembering_custom_root(candidate.selection.custom_root)
            )
        self._persist(state)

    def _commit(self, candidate: _Candidate, *, forgetting: Path | None = None) -> None:
        """The candidate becomes the Hub's truth -- or nothing does.

        The order is the guarantee: the new state is written first, and only a
        write that succeeded moves the binding. If the disk refuses, memory,
        the backend and the file still describe the installation that was in
        use, and the caller is told.
        """
        previous_config, previous_backend, previous_state = self.config, self.backend, self.state
        state = self.state.activating(candidate.config).reconciled(candidate.root)
        if forgetting is not None and root_key(forgetting) != root_key(candidate.root):
            # Relocation: the old folder stops being known once the new one is.
            state = state.forgetting(forgetting)
        try:
            self._persist(state, config=candidate.config)
        except HubError:
            self.config, self.backend, self.state = (
                previous_config,
                previous_backend,
                previous_state,
            )
            raise
        self.config = candidate.config
        self.backend = candidate.backend

    def _persist(self, state: HubState, *, config: InstalledConfig | None = None) -> None:
        """Write the Hub's state. Memory follows the disk, never the other way round."""
        try:
            write_state(self.selection_path, state)
        except Exception as exc:
            raise HubError("Não foi possível salvar a seleção do Hub. Nada foi alterado.") from exc
        self.state = state
        current = config if config is not None else self.config
        if current is not None and state.active is not None:
            self.config = replace(current, general=state.general)
        elif config is not None:
            self.config = config

    def install(self, selection: InstallSelection) -> str:
        with self._lock:
            operation_id = self._reserve()
            try:
                candidate = self._prepare(selection)
                pending = candidate.backend.check_conflicts(selection.profile_id)
                if pending is not None:
                    # A decision is owed on this candidate, so keep it for the
                    # resolver -- but the active installation is untouched.
                    self._candidate = candidate
                    raise _BackendConflictRequired(pending)
                return self._schedule_install(operation_id, candidate)
            except _BackendConflictRequired as exc:
                # The candidate stays: the decision owed is about it.
                self._end_operation(None)
                raise ConflictResolutionRequired(exc.report) from exc
            except Exception as exc:
                self._candidate = None
                self._end_operation(None)
                if isinstance(exc, HubError):
                    raise
                raise HubError(
                    "Não foi possível preparar a instalação. Verifique a pasta escolhida."
                ) from exc

    def _schedule_install(self, operation_id: str, candidate: _Candidate) -> str:
        self._installation_attempt = _InstallationAttempt(operation_id, candidate, "install")
        control = InstallationControl(operation_id)
        self._installation_control = control

        def run(emit) -> object:
            # Capture the retry context before any writes or installer work can
            # fail. Reuse the resolved candidate on retry, including its mode
            # and the original parent/create_subfolder choice.
            with self._lock:
                self._remember_attempt(candidate)
                path = config_path(candidate.installation.layout)
                existing = _installed_config(path)
                write_installed_config(
                    path,
                    candidate.config
                    if existing is None
                    else candidate.config.with_general(existing.general),
                )
            return candidate.backend.first_run(operation_id, candidate.config.profile_id, emit=emit)

        def settle(result: object) -> None:
            if not _succeeded(result):
                self._record(candidate.root, FAILURE, summary="a instalação não foi concluída")
                return
            # Raises when the disk refuses: the installation is then not adopted.
            self._commit(candidate)
            # A run that installed nothing did not install anything: "last
            # installed" keeps saying when work was last done.
            report = getattr(candidate.backend, "last_first_run", None)
            if not getattr(report, "did_nothing", False):
                self._record(
                    candidate.root,
                    INSTALL,
                    profile_id=candidate.config.profile_id,
                    result="succeeded",
                )

        return self._launch(
            operation_id,
            run,
            settle,
            candidate=candidate,
            control=control,
        )

    def cancel_pending_installation(self) -> None:
        """The person walked away from an installation waiting on a decision.

        The candidate it was waiting with is let go. Nothing had started, so
        nothing else changes; an installation already running is not touched
        -- this version cannot cancel one safely.
        """
        with self._lock:
            if not self._busy:
                self._candidate = None

    def current_conflict_preflight(self):
        """The conflict ``install`` last raised, without scanning again.

        Read-only; safe to poll while a decision is pending. Rescanning here
        would mint a new preflight id and strand whatever choice the caller is
        about to submit against the old one. ``None`` means there is nothing
        to resolve -- including when nothing has been selected for install yet.
        """
        session = self._candidate.backend if self._candidate else self.backend
        if self._candidate is None and self.config is None:
            return None
        try:
            return session.current_conflict_preflight()
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível verificar conflitos com serviços externos."
            ) from exc

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        """Bridge the frozen consumer command to the session the decision belongs to.

        A conflict is always about a candidate, so the candidate's backend
        answers; the active installation stays bound either way.
        """
        candidate = self._candidate
        session = candidate.backend if candidate else self.backend
        profile_id = candidate.config.profile_id if candidate else self._profile_id()
        try:
            result = session.resolve_conflict(selection)
            if result.status is ConflictResolutionStatus.FAILED:
                # The same decision may be tried again, about the same candidate.
                session.check_conflicts(profile_id)
        except Exception as exc:
            if selection.policy is ExternalConflictPolicy.ABORT:
                self.cancel_pending_installation()
            raise HubUnavailable(
                "Não foi possível processar a decisão sobre os serviços externos."
            ) from exc
        if result.status is ConflictResolutionStatus.ABORTED:
            self.cancel_pending_installation()
        return result

    def resolve_conflict(self, selection: ConflictPolicySelection) -> ConflictResolutionResult:
        """Compatibility alias for callers predating the consumer protocol name."""
        return self.resolve_external_conflicts(selection)

    def _command(self, name: str) -> str:
        with self._lock:
            operation_id = self._reserve()
            backend, profile_id = self.backend, self._profile_id()
            command = getattr(backend, name)
            return self._launch(
                operation_id, lambda emit: command(operation_id, profile_id, emit=emit)
            )

    def start_stack(self) -> str:
        return self._command("start_stack")

    def stop_stack(self) -> str:
        return self._command("stop_stack")

    def restart_stack(self) -> str:
        return self._command("restart_stack")

    def _service_command(self, name: str, service_id: str) -> str:
        if service_id not in _SERVICE_IDS:
            raise HubError("Serviço desconhecido.")
        with self._lock:
            operation_id = self._reserve()
            backend, profile_id = self.backend, self._profile_id()
            command = getattr(backend, name)
            return self._launch(
                operation_id, lambda emit: command(operation_id, profile_id, service_id, emit=emit)
            )

    def start_service(self, service_id: str) -> str:
        return self._service_command("start_service", service_id)

    def stop_service(self, service_id: str) -> str:
        return self._service_command("stop_service", service_id)

    def restart_service(self, service_id: str) -> str:
        return self._service_command("restart_service", service_id)

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal:
        with self._lock:
            operation_id = self._reserve()
        ending = _Ending(self, operation_id)
        launch = None
        try:
            launch = self.backend.open_webui(operation_id, self._profile_id(), emit=ending.emit)
            if not launch.ok:
                diagnostics = launch.result.diagnostics
                return LaunchRefusal(
                    diagnostics[0].message if diagnostics else "Open WebUI não está pronto.",
                    diagnostics[0].code if diagnostics else "not-ready",
                )
            return LaunchTarget(launch.target.service_id, launch.target.url, "Open WebUI")
        finally:
            with self._lock:
                self._end_operation(None)
            ending.close(None if launch is None else launch.result, None)

    def wait(self, timeout: float | None = None) -> None:
        if not self._idle.wait(timeout):
            raise TimeoutError("Hub operation has not finished")
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError("Hub operation has not finished")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._closing.set()
            watch = self._integration_watch
            if self._installation_control is not None:
                with contextlib.suppress(ValueError):
                    self._installation_control.request("cancel")
            for subscription in tuple(self._subscriptions):
                subscription.close()
        self.wait()
        if watch is not None:
            watch.join()
        for backend in self._backends.values():
            backend.close()


class _Subscription:
    def __init__(self, app: HubApplicationAdapter, sink: OperationEventSink) -> None:
        self.app, self.sink = app, sink
        self.closed = False

    def deliver(self, event: OperationEvent) -> None:
        if not self.closed:
            try:
                self.sink(event)
            except Exception:  # noqa: BLE001 -- subscribers cannot change an operation result
                return

    def close(self) -> None:
        with self.app._lock:
            if not self.closed:
                self.closed = True
                self.app._subscriptions.remove(self)


def _awaits_open_webui_admin(backend: object) -> bool:
    waiting = getattr(backend, "open_webui_integration_waiting", None)
    return waiting is not None and bool(waiting())


def location_of_config(config: InstalledConfig, root: Path) -> InstallLocation:
    """Where an installation lives: its own mode, and the folder it was found in.

    A portable or default installation names no root of its own -- it resolves
    to one at runtime -- so the folder the Hub found it in is used as the custom
    root only when the configuration says custom.
    """
    if config.installation.mode is InstallLocationMode.CUSTOM:
        return InstallLocation.custom(root)
    return InstallLocation(config.installation.mode, None)


def _looks_installed(root: Path) -> bool:
    """Whether a folder holds what an installation holds, configuration aside."""
    return any((root / name).is_dir() for name in ("apps", "models"))


def _installed_config(path: Path) -> InstalledConfig | None:
    """An installation's own configuration, or None when it is absent or unreadable.

    Unreadable is not the same as absent, and the difference belongs to the
    screen that offers a way out of it; at startup the Hub simply does not
    treat a broken file as a selection.
    """
    if not path.is_file():
        return None
    try:
        return loads(path.read_text(encoding="utf-8"), source=str(path))
    except ConfigError:
        return None


def create_hub_application(
    repo_root: Path,
    *,
    dependencies: StackDependencies | None = None,
    resolve: Callable[[InstallLocation], ResolvedInstallation] | None = None,
    backend_factory: Callable[[ResolvedInstallation], StackApplication] | None = None,
) -> HubApplicationAdapter:
    # ``repo_root`` is where profiles and manifests are read from. A portable
    # stack is resolved against the workspace instead -- the checkout, or the
    # folder the executable was unpacked into -- never inside a frozen bundle.
    resolver = resolve or (lambda location: resolve_installation(location))
    default = resolver(InstallLocation.default())
    portable = resolver(InstallLocation.portable())
    selection_path = default.layout.state / STATE_FILENAME
    # The Hub's own state, migrated from schema 1 when that is what is there.
    state = read_state(selection_path) or HubState()
    # A portable stack beside the executable still wins: a copied folder is the
    # installation the person is running, wherever the machine's state points.
    # Everything else the state knows -- the other installations, the interface
    # preferences -- is kept.
    active = (
        _installed_config(config_path(portable.layout))
        or state.active
        or _installed_config(config_path(default.layout))
    )
    if active is not None:
        state = state.activating(active.with_general(state.general))
    profiles = load_profiles(repo_root / "profiles")
    return HubApplicationAdapter(
        profiles=profiles,
        resolve=resolver,
        backend_factory=backend_factory
        or (
            lambda installation: StackApplication(
                installation, profiles=profiles, repo_root=repo_root, dependencies=dependencies
            )
        ),
        selection_path=selection_path,
        state=state,
    )
