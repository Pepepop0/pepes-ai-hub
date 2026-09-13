"""Product composition: the Hub protocol over the existing stack application.

The adapter schedules commands, persists the selection and projects answers.
Planning, installation, ownership, readiness and endpoints remain in the backend.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from .compatibility import evaluate_compatibility
from .config import InstalledConfig, config_path, dumps, loads
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


class HubApplicationAdapter:
    def __init__(
        self,
        *,
        profiles: Sequence[Profile],
        resolve: Callable[[InstallLocation], ResolvedInstallation],
        backend_factory: Callable[[ResolvedInstallation], StackApplication],
        selection_path: Path,
        config: InstalledConfig | None = None,
    ) -> None:
        self.profiles = tuple(profiles)
        self.resolve = resolve
        self.backend_factory = backend_factory
        self.selection_path = selection_path
        self.config = config
        installation = resolve(config.installation if config else InstallLocation.default())
        self.backend = backend_factory(installation)
        self._backends = {installation.root: self.backend}
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
        return self.config.general.recent_custom_roots if self.config else ()

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

    def _publish(self, event: OperationEvent) -> None:
        with self._lock:
            self._snapshot = self.backend.current_operation()
            for subscription in tuple(self._subscriptions):
                subscription.deliver(event)

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

    def install(self, selection: InstallSelection) -> str:
        with self._lock:
            operation_id = self._reserve()
            try:
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
                )
                # Bind the backend before checking for a conflict: a decision
                # the caller makes next must land on this exact session, and a
                # pending conflict is still "this selection", not a failure.
                self.config, self.backend = config, backend
                pending = backend.check_conflicts(selection.profile_id)
                if pending is not None:
                    raise _BackendConflictRequired(pending)
                if location.mode is InstallLocationMode.CUSTOM:
                    # The preflight accepted the attempt, so preserve its root
                    # even if provisioning later fails. A selection cancelled
                    # at conflict preflight never enters the history.
                    config = replace(
                        config,
                        general=config.general.remembering_custom_root(location.root),
                    )
                    self.config = config
                # Persist intent before accepting the operation. A failed first
                # run reopens the wizard, since readiness still comes from domain.
                _save_config(config_path(installation.layout), config)
                _save_config(self.selection_path, config)
                return self._schedule(operation_id, backend.first_run)
            except _BackendConflictRequired as exc:
                self._busy = False
                self._idle.set()
                raise ConflictResolutionRequired(exc.report) from exc
            except Exception as exc:
                self._busy = False
                self._idle.set()
                if isinstance(exc, HubError):
                    raise
                raise HubError(
                    "Não foi possível preparar a instalação. Verifique a pasta escolhida."
                ) from exc

    def current_conflict_preflight(self):
        """The conflict ``install`` last raised, without scanning again.

        Read-only; safe to poll while a decision is pending. Rescanning here
        would mint a new preflight id and strand whatever choice the caller is
        about to submit against the old one. ``None`` means there is nothing
        to resolve -- including when nothing has been selected for install yet.
        """
        if self.config is None:
            return None
        try:
            return self.backend.current_conflict_preflight()
        except Exception as exc:
            raise HubUnavailable(
                "Não foi possível verificar conflitos com serviços externos."
            ) from exc

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult:
        """Bridge the frozen consumer command to the bound backend session."""
        try:
            result = self.backend.resolve_conflict(selection)
            if result.status is ConflictResolutionStatus.FAILED:
                self.backend.check_conflicts(self._profile_id())
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


def _save_config(path: Path, config: InstalledConfig) -> None:
    if path.exists() and loads(path.read_text(encoding="utf-8")) == config:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(dumps(config), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_hub_application(
    repo_root: Path,
    *,
    dependencies: StackDependencies | None = None,
    resolve: Callable[[InstallLocation], ResolvedInstallation] | None = None,
    backend_factory: Callable[[ResolvedInstallation], StackApplication] | None = None,
) -> HubApplicationAdapter:
    resolver = resolve or (lambda location: resolve_installation(location, repo_root=repo_root))
    default = resolver(InstallLocation.default())
    portable = resolver(InstallLocation.portable())
    selection_path = default.layout.state / "hub-selection.yaml"
    config = None
    # A copied portable checkout resolves root:null here, at its new location.
    for path in (config_path(portable.layout), selection_path, config_path(default.layout)):
        if path.is_file():
            config = loads(path.read_text(encoding="utf-8"), source=str(path))
            break
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
        config=config,
    )
