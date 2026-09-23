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

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .application_contracts import ServicesSnapshot, ServiceView, VramSnapshot
from .compatibility import evaluate_compatibility
from .config import ConfigError, InstalledConfig, config_path, loads
from .external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictResolutionResult,
    ConflictResolutionStatus,
)
from .external_conflicts import ConflictResolutionRequired as _BackendConflictRequired
from .installation import (
    InstallLocation,
    InstallLocationMode,
    ResolvedInstallation,
    resolve_installation,
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
from .operation_contracts import OperationEvent, OperationEventSink, OperationSnapshot
from .profiles import Profile, load_profiles, offered_profiles
from .stack_application import StackApplication, StackDependencies
from .storage import enough_space, free_space, model_download_estimate
from .tui.application import (
    COMPATIBLE,
    INSUFFICIENT,
    RECOMMENDED,
    ConflictResolutionRequired,
    HubError,
    HubUnavailable,
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


def _succeeded(result: object) -> bool:
    """Whether an operation result says it succeeded, however the backend spells it."""
    state = getattr(result, "state", None)
    return str(getattr(state, "value", state)) == "succeeded"


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
        self._worker: threading.Thread | None = None
        self._snapshot: OperationSnapshot | None = None
        self._subscriptions: list[_Subscription] = []

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

    def _observed_status(self, entry: KnownInstallation) -> tuple[str, str, str | None]:
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
                status = self.backend.status(self._profile_id())
            except Exception:  # noqa: BLE001 -- a list row never fails the screen
                return "unknown", "", profile_id
            if status.stack_up:
                return "running", "", profile_id
            return (
                ("ready", "", profile_id)
                if status.installation_ready
                else ("stopped", "", profile_id)
            )
        return "ready", "", profile_id

    def installation_summaries(self) -> tuple[InstallationSummary, ...]:
        """Every known installation as a row, active one first, then most recent."""
        active_root = self.active_root()
        rows = []
        for entry in self.state.ordered():
            status, detail, profile_id = self._observed_status(entry)
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
                must_stop = status.stack_up
            except Exception:  # noqa: BLE001 -- treat an unreadable stack as stopped
                must_stop = False
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

    def _bind(self, root: Path, config: InstalledConfig, *, forgetting: Path | None = None) -> None:
        """Make this installation the active one, as one transaction."""
        installation = self.resolve(location_of_config(config, root))
        backend = self._backends.get(installation.root)
        if backend is None:
            backend = self.backend_factory(installation)
            self._backends[installation.root] = backend
        candidate = _Candidate(
            InstallSelection(
                config.profile_id,
                config.installation.mode,
                str(root) if config.installation.mode is InstallLocationMode.CUSTOM else None,
            ),
            installation,
            backend,
            config,
        )
        self._commit(candidate, forgetting=forgetting)

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

    def _stop_was_complete(self, backend, result) -> str | None:
        """Why the target must not be bound yet, or None when the way is clear.

        Two things have to be true after stopping: the Hub's own processes are
        gone, and the addresses they held answer to nobody. Both are asked of
        the runtime that already owns them; nothing is re-implemented here.
        """
        if not _succeeded(result):
            return "Não foi possível parar os serviços da instalação atual."
        left = tuple(getattr(result, "left_running", ()) or ())
        if left:
            return (
                "Alguns serviços gerenciados continuam em execução, então a troca foi "
                "cancelada. Tente novamente ou reinicie a máquina."
            )
        listening = getattr(backend, "listening_ports", None)
        try:
            ports = tuple(listening() or ()) if callable(listening) else ()
        except Exception:  # noqa: BLE001 -- an unreadable answer is not a proof of freedom
            return "Não foi possível confirmar que as portas foram liberadas."
        if ports:
            spelled = ", ".join(str(port) for port in ports)
            return (
                f"As portas {spelled} continuam ocupadas, então a troca foi cancelada. "
                "Processos externos não são encerrados pelo Hub."
            )
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
            previous = self.backend
            profile_id = self._profile_id()
            target = Path(root)

            def execute():
                result = None
                try:
                    result = previous.stop_stack(
                        operation_id, profile_id, emit=self._publisher(previous)
                    )
                finally:
                    with self._lock:
                        refusal = self._stop_was_complete(previous, result)
                        if refusal is None:
                            try:
                                self._bind(
                                    target,
                                    config,
                                    forgetting=None if replacing is None else Path(replacing),
                                )
                            except HubError as exc:
                                self._switch_refusal = str(exc)
                        else:
                            self._switch_refusal = refusal
                        self._busy = False
                        self._idle.set()

            self._worker = threading.Thread(target=execute, name="hub-operation")
            self._worker.start()
            return operation_id

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

    def repair(self) -> str:
        """Check the active installation and repair what is missing, in place.

        The same idempotent first run the installer uses, with the root, the
        profile and the mode it already has: nothing is asked again, and what
        is already correct is left alone.
        """
        with self._lock:
            if self.config is None:
                raise HubError("Nenhuma instalação está selecionada.")
            operation_id = self._reserve()
            backend = self.backend
            config = self.config
            root = self.active_root()

            def execute():
                result = None
                try:
                    result = backend.first_run(
                        operation_id, config.profile_id, emit=self._publisher(backend)
                    )
                finally:
                    with self._lock:
                        if root is not None:
                            self._persist(
                                self.state.recording(
                                    root,
                                    REPAIR if _succeeded(result) else FAILURE,
                                    result="succeeded" if _succeeded(result) else "",
                                    summary=""
                                    if _succeeded(result)
                                    else "a verificação não pôde ser concluída",
                                )
                            )
                        self._busy = False
                        self._idle.set()

            self._worker = threading.Thread(target=execute, name="hub-operation")
            self._worker.start()
            return operation_id

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
            status.installation_ready,
            self.config.profile_id,
            self.config.installation.mode,
            self._root_display(),
        )

    def projections(self) -> tuple[InstallationView, ServicesSnapshot, VramSnapshot]:
        """One authoritative observation for everything repeated by the Dashboard."""
        try:
            status = self.backend.status(self._profile_id())
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível verificar a instalação. Tente novamente."
            ) from exc
        installation = (
            InstallationView(False)
            if self.config is None
            else InstallationView(
                status.installation_ready,
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

        The largest one that fits is the recommendation -- no profile is
        preferred by name -- and the ones that do not fit are presented as
        asking for more than there is, never as an ordinary choice.
        """
        facts = self.backend.facts()
        offered = offered_profiles(self.profiles)
        compatible = {
            profile.id: evaluate_compatibility(facts, profile).compatible for profile in offered
        }
        fitting = [profile for profile in offered if compatible[profile.id]]
        best = max(
            fitting,
            key=lambda profile: profile.hardware.min_vram_mib,
            default=None,
        )
        return tuple(
            ProfileChoice(
                profile.id,
                profile.name,
                profile.description,
                compatible[profile.id],
                standing=(
                    RECOMMENDED
                    if best is not None and profile.id == best.id
                    else (COMPATIBLE if compatible[profile.id] else INSUFFICIENT)
                ),
                required_vram_gb=profile.hardware.min_vram_gb,
            )
            for profile in sorted(offered, key=lambda item: item.hardware.min_vram_mib)
        )

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

    def subscribe(self, sink: OperationEventSink):
        with self._lock:
            subscription = _Subscription(self, sink)
            self._subscriptions.append(subscription)
            if self._snapshot is not None:
                subscription.deliver(self._snapshot.last_event)
            return subscription

    def _publisher(self, backend: StackApplication) -> OperationEventSink:
        """Events are observed from the session that produced them.

        An installation runs on its candidate's backend, which is not the bound
        one until it commits; asking the bound backend for the snapshot would
        describe the wrong session -- or none at all.
        """

        def publish(event: OperationEvent) -> None:
            with self._lock:
                self._snapshot = backend.current_operation()
                for subscription in tuple(self._subscriptions):
                    subscription.deliver(event)

        return publish

    def _publish(self, event: OperationEvent) -> None:
        self._publisher(self.backend)(event)

    def _reserve(self) -> str:
        if self._closed:
            raise HubUnavailable("O Hub está encerrando.")
        if self._busy:
            raise OperationBusy("Aguarde a operação atual terminar.")
        self._busy = True
        self._idle.clear()
        return uuid4().hex

    def _schedule(self, operation_id: str, command: Callable) -> str:
        def execute():
            try:
                command(operation_id, self._profile_id(), emit=self._publish)
            finally:
                with self._lock:
                    self._busy = False
                    self._idle.set()

        self._worker = threading.Thread(target=execute, name="hub-operation")
        self._worker.start()
        return operation_id

    def _prepare(self, selection: InstallSelection) -> _Candidate:
        """Everything the selection implies, bound to nothing. Never touches the active one."""
        if selection.profile_id not in {p.id for p in self.profiles}:
            raise HubError("Perfil não encontrado. Selecione um perfil disponível.")
        location = InstallLocation(
            selection.mode,
            Path(selection.custom_root) if selection.custom_root else None,
        )
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
                state.general.remembering_custom_root(candidate.config.installation.root)
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
        except OSError as exc:
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
                self._candidate = candidate
                self._remember_attempt(candidate)
                # The installation's own configuration belongs to its folder; the
                # Hub's pointer to it is only written once the run succeeds. The
                # interface preferences it carries are the Hub's, so whatever the
                # folder already holds is kept: a second run rewrites nothing.
                path = config_path(candidate.installation.layout)
                existing = _installed_config(path)
                write_installed_config(
                    path,
                    candidate.config
                    if existing is None
                    else candidate.config.with_general(existing.general),
                )
                return self._schedule_install(operation_id, candidate)
            except _BackendConflictRequired as exc:
                self._release()
                raise ConflictResolutionRequired(exc.report) from exc
            except Exception as exc:
                self._candidate = None
                self._release()
                if isinstance(exc, HubError):
                    raise
                raise HubError(
                    "Não foi possível preparar a instalação. Verifique a pasta escolhida."
                ) from exc

    def _schedule_install(self, operation_id: str, candidate: _Candidate) -> str:
        def execute():
            result = None
            try:
                result = candidate.backend.first_run(
                    operation_id,
                    candidate.config.profile_id,
                    emit=self._publisher(candidate.backend),
                )
            finally:
                with self._lock:
                    if _succeeded(result):
                        self._commit(candidate)
                        # A run that installed nothing did not install anything:
                        # "last installed" keeps saying when work was last done.
                        report = getattr(candidate.backend, "last_first_run", None)
                        if not getattr(report, "did_nothing", False):
                            self._persist(
                                self.state.recording(candidate.root, INSTALL, result="succeeded")
                            )
                    else:
                        self._persist(
                            self.state.recording(
                                candidate.root,
                                FAILURE,
                                summary="a instalação não foi concluída",
                            )
                        )
                    self._candidate = None
                    self._busy = False
                    self._idle.set()

        self._worker = threading.Thread(target=execute, name="hub-operation")
        self._worker.start()
        return operation_id

    def _release(self) -> None:
        self._busy = False
        self._idle.set()

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
                session.check_conflicts(profile_id)
            return result
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível processar a decisão sobre os serviços externos."
            ) from exc

    def resolve_conflict(self, selection: ConflictPolicySelection) -> ConflictResolutionResult:
        """Compatibility alias for callers predating the consumer protocol name."""
        return self.resolve_external_conflicts(selection)

    def _command(self, name: str) -> str:
        with self._lock:
            operation_id = self._reserve()
            try:
                return self._schedule(operation_id, getattr(self.backend, name))
            except Exception:
                self._busy = False
                self._idle.set()
                raise

    def start_stack(self) -> str:
        return self._command("start_stack")

    def stop_stack(self) -> str:
        return self._command("stop_stack")

    def restart_stack(self) -> str:
        return self._command("restart_stack")

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal:
        with self._lock:
            operation_id = self._reserve()
        try:
            launch = self.backend.open_webui(operation_id, self._profile_id(), emit=self._publish)
            if not launch.ok:
                diagnostics = launch.result.diagnostics
                return LaunchRefusal(
                    diagnostics[0].message if diagnostics else "Open WebUI não está pronto.",
                    diagnostics[0].code if diagnostics else "not-ready",
                )
            return LaunchTarget(launch.target.service_id, launch.target.url, "Open WebUI")
        finally:
            with self._lock:
                self._busy = False
                self._idle.set()

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
            for subscription in tuple(self._subscriptions):
                subscription.close()
        self.wait()


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
