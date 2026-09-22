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
    STATE_FILENAME,
    HubState,
    KnownInstallation,
    read_state,
    write_installed_config,
    write_state,
)
from .operation_contracts import OperationEvent, OperationEventSink, OperationSnapshot
from .profiles import Profile, load_profiles
from .stack_application import StackApplication, StackDependencies
from .tui.application import (
    ConflictResolutionRequired,
    HubError,
    HubUnavailable,
    InstallationView,
    InstallSelection,
    LaunchRefusal,
    LaunchTarget,
    OperationBusy,
    ProfileChoice,
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

    def active_root(self) -> Path | None:
        """The folder every command reaches right now. None before the first install."""
        return self.config.installation.root if self.config else None

    def active_profile(self) -> str | None:
        return self.config.profile_id if self.config else None

    def known_installations(self) -> tuple[KnownInstallation, ...]:
        """Every installation the Hub knows, most recently used first."""
        return self.state.ordered()

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
            status.installation_ready, self.config.profile_id, self.config.installation.mode
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
                status.installation_ready, self.config.profile_id, self.config.installation.mode
            )
        )
        services = ServicesSnapshot(
            status.observed_at,
            tuple(ServiceView(item.service_id, item.health) for item in status.components),
        )
        return installation, services, status.gpu

    def profile_choices(self) -> tuple[ProfileChoice, ...]:
        return tuple(
            ProfileChoice(
                p.id,
                p.name,
                p.description,
                evaluate_compatibility(self.backend.facts(), p).compatible,
            )
            for p in self.profiles
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

    def _commit(self, candidate: _Candidate) -> None:
        """The candidate becomes the Hub's truth: binding, selection and history."""
        self.config = candidate.config
        self.backend = candidate.backend
        self._persist(self.state.activating(candidate.config))

    def _persist(self, state: HubState) -> None:
        self.state = state
        if self.config is not None and state.active is not None:
            self.config = replace(self.config, general=state.general)
        write_state(self.selection_path, state)

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
