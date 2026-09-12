"""The application service behind the first-run button.

This is the layer a UI talks to. It composes the existing domains -- planning,
provisioning, models, managed configuration, the runtime controller -- into
the handful of things a person actually asks for: install this profile, start
the stack, stop it, open Open WebUI, what is the state of things. It decides
none of the answers itself. Ownership, verification, endpoints and readiness
stay where they already live; this module asks and relays.

Three rules:

- **one operation at a time.** Every mutating action is an operation with an
  id, a validated event stream and exactly one result. A second request while
  one is in flight is refused, not queued.
- **projections cross the boundary, not domain objects.** A UI gets
  ``AppStatus``, ``ServicesSnapshot``, ``VramSnapshot`` and a launch target;
  never a ``ServiceControlStatus``, a path or a process id.
- **external is observed, never touched.** A component someone else installed
  or started is reported as such; start, stop and restart skip it and say so.

Nothing here opens a browser, prints, sleeps by default or reads global state.
Every dependency with a side effect is a field of :class:`StackDependencies`,
with the real implementation as its default, so a test can bind controlled
providers to the same composition the product runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .application_contracts import (
    OperationSubscription,
    ServicesSnapshot,
    ServiceView,
    VramSnapshot,
)
from .components import ComponentId
from .controller import ServiceController
from .download import Downloader, DownloadProgress
from .execution import ExecutionReport, PlanExecutor
from .external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
)
from .external_conflicts import (
    ConflictResolutionRequired,
    ExternalConflictSession,
    merged_options,
    terminate_by_pid,
)
from .install_actions import InstallActionHandlers
from .installation import ResolvedInstallation, detect_repo_root
from .integration import (
    IntegrationOptions,
    IntegrationSnapshot,
    ManagedPlanPhases,
    ManagedPreflightCode,
    ManagedPreflightReport,
    build_service_controller,
    default_install_actions,
    default_provisioners,
    detect_integration,
    plan_executor,
    preflight_managed_stack,
    provision_context,
    stage_managed_plan,
)
from .inventory import (
    ListenerReport,
    ProcessReport,
    ScanOutcome,
    list_listeners,
    list_processes,
)
from .inventory import lookup_process as default_lookup_process
from .launcher import ProcessLauncher
from .managed_config import ManagedConfigWriter
from .model_catalog import ModelCatalog, model_catalog
from .model_installer import ModelInstaller
from .model_resolver import StackModelResolver, unresolved_roles
from .models import ManagedModelPaths
from .ollama_models import HttpOllamaClient
from .operation_contracts import (
    OperationEvent,
    OperationEventSink,
    OperationPhase,
    OperationResult,
    OperationSnapshot,
    OperationState,
)
from .operations import OperationStream, diagnostic, safe_text, utc_now
from .planning import InstallPlan, PlanAction, build_install_plan
from .probes import http_probe, tcp_probe
from .processes import ProcessOrigin, RuntimeProcessState
from .profiles import Profile, load_profiles
from .provisioning import ProvisionContext, ProvisionEvent, ProvisionOutcome
from .registry import ManagedProcessRegistry
from .runner import CommandRunner
from .service_config import StackConfiguration
from .services import ServiceState
from .system import SystemFacts, detect_system

# Services start in dependency order and stop in the reverse. Open WebUI talks
# to Ollama and ComfyUI needs the models the plan placed, so Ollama goes first.
START_ORDER: tuple[str, ...] = (
    str(ComponentId.OLLAMA),
    str(ComponentId.OPEN_WEBUI),
    str(ComponentId.COMFYUI),
)

# Hosts a launch target may point at. The endpoint domain derives the URL; this
# is the last check before handing it to something that will open it.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _external_endpoint_occupants(
    report: ManagedPreflightReport,
    component_id: str,
    listeners: ListenerReport,
    managed_pids: frozenset[int],
):
    spec = report.endpoints.spec(component_id)
    return tuple(
        listener
        for listener in listeners.on_port(spec.port, spec.host)
        if listener.pid is None or listener.pid not in managed_pids
    )


class OperationBusy(RuntimeError):
    """Another operation is running. One at a time, no queue."""


class UnknownProfile(LookupError):
    """The requested profile is not one this application loaded."""


# --- dependências --------------------------------------------------------------


@dataclass
class StackDependencies:
    """Every side-effecting dependency, with the real one as the default.

    A test binds controlled providers here; the application above is the same
    code either way. ``None`` for a factory means "use the domain default".
    """

    runner: CommandRunner = field(default_factory=CommandRunner)
    detect_facts: Callable[[], SystemFacts] = detect_system
    provisioners: Callable[[IntegrationOptions], Mapping[str, Any]] = default_provisioners
    catalog: ModelCatalog | None = None
    downloader: Callable[[], Downloader] | None = None
    ollama_client: Callable[[str], Any] | None = None
    config_writer: ManagedConfigWriter | None = None
    launcher: Callable[[ManagedProcessRegistry], ProcessLauncher] | None = None
    http: Callable[..., Any] = http_probe
    tcp: Callable[..., Any] = tcp_probe
    list_processes: Callable[[], ProcessReport] = list_processes
    list_listeners: Callable[[], ListenerReport] = list_listeners
    lookup_process: Callable[[int], Any] = default_lookup_process
    # Ends one already identity-confirmed external process; see
    # external_conflicts.terminate_by_pid for why this is not launcher.py.
    terminate_process: Callable[[int, float], bool] = terminate_by_pid
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = utc_now


# --- projeções ----------------------------------------------------------------


@dataclass(frozen=True)
class ComponentStatus:
    """One component as a UI may show it. No path, no pid."""

    service_id: str
    enabled: bool
    ownership: str
    availability: str
    health: str
    runtime: str
    controllable: bool
    endpoint: str | None
    detail: str

    @property
    def external(self) -> bool:
        return self.ownership == "external"

    @property
    def up(self) -> bool:
        return self.health == ServiceState.UP.value


@dataclass(frozen=True)
class AppStatus:
    """The read-only picture of the installation for the selected profile."""

    observed_at: datetime
    profile_id: str
    installation_ready: bool
    pending_work: tuple[str, ...]
    components: tuple[ComponentStatus, ...]
    gpu: VramSnapshot
    operation: OperationSnapshot | None

    def component(self, service_id: str) -> ComponentStatus | None:
        return next((item for item in self.components if item.service_id == service_id), None)

    @property
    def stack_up(self) -> bool:
        return all(item.up for item in self.components if item.enabled)


@dataclass(frozen=True)
class OpenWebUILaunchTarget:
    """Where a UI may open Open WebUI. Only ever built from the endpoint domain."""

    url: str
    service_id: str
    origin: str
    ready: bool
    detail: str

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or parts.hostname not in LOOPBACK_HOSTS:
            raise ValueError("a launch target must be a loopback http(s) URL")


@dataclass(frozen=True)
class OpenWebUILaunch:
    """The outcome of asking for Open WebUI: an operation, and maybe a target."""

    result: OperationResult
    target: OpenWebUILaunchTarget | None

    @property
    def ok(self) -> bool:
        return self.target is not None and self.target.ready


# --- a aplicação -------------------------------------------------------------


class StackApplication:
    """Composition root for the product flow. See the module docstring."""

    def __init__(
        self,
        installation: ResolvedInstallation,
        *,
        profiles: Sequence[Profile] | None = None,
        repo_root: Path | None = None,
        options: IntegrationOptions | None = None,
        dependencies: StackDependencies | None = None,
    ) -> None:
        self.installation = installation
        self.repo_root = Path(repo_root) if repo_root is not None else detect_repo_root()
        loaded = (
            tuple(profiles)
            if profiles is not None
            else tuple(load_profiles(self.repo_root / "profiles"))
        )
        self.profiles: dict[str, Profile] = {profile.id: profile for profile in loaded}
        self.options = options or IntegrationOptions()
        self.deps = dependencies or StackDependencies()

        self._lock = threading.Lock()
        self._busy = False
        self._operation: OperationSnapshot | None = None
        self._subscribers: list[_Subscription] = []
        self._active_profile: str | None = None
        self._facts: SystemFacts | None = None
        self._installers: dict[str, ModelInstaller] = {}
        self._registry: ManagedProcessRegistry | None = None
        self._launcher: ProcessLauncher | None = None
        self.last_first_run = None
        # Set only for the duration of a download-bearing step (see
        # first_run._acquire_models); None means nobody is listening, so
        # install_actions never even builds a progress callback.
        self._transfer_sink: Callable[[str, DownloadProgress], None] | None = None
        # Set only for the duration of a provisioning step (see
        # first_run.FirstRunCoordinator); None means nobody is listening, so
        # a provisioner's context.emit() only logs, same as before this seam
        # existed.
        self._activity_sink: Callable[[ProvisionEvent], None] | None = None
        # Indirected through ``self.deps`` at call time, not captured now: a
        # test that reassigns a dependency after construction (as several
        # already do for the launcher) must still reach the session.
        self._conflicts = ExternalConflictSession(
            lookup_process=lambda pid: self.deps.lookup_process(pid),
            list_listeners=lambda: self.deps.list_listeners(),
            external_path=self._external_install_path,
            terminate_process=lambda pid, create_time: self.deps.terminate_process(
                pid, create_time
            ),
            monotonic=lambda: self.deps.monotonic(),
            sleep=lambda seconds: self.deps.sleep(seconds),
        )

    # --- leitura -------------------------------------------------------------

    @property
    def layout(self):
        return self.installation.layout

    def profile(self, profile_id: str) -> Profile:
        try:
            return self.profiles[profile_id]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "none"
            raise UnknownProfile(f"unknown profile {profile_id!r}; known: {known}") from None

    @property
    def active_profile(self) -> str | None:
        return self._active_profile

    def facts(self) -> SystemFacts:
        if self._facts is None:
            self._facts = self.deps.detect_facts()
        return self._facts

    def current_operation(self) -> OperationSnapshot | None:
        return self._operation

    def subscribe(self, sink: OperationEventSink) -> OperationSubscription:
        """Replay the latest event, if any, then deliver future ones."""
        subscription = _Subscription(self, sink)
        with self._lock:
            self._subscribers.append(subscription)
            latest = self._operation.last_event if self._operation is not None else None
        if latest is not None:
            subscription.deliver(latest)
        return subscription

    def services_snapshot(self, profile_id: str | None = None) -> ServicesSnapshot:
        profile = self.profile(profile_id or self._active_profile or self._default_profile())
        controller = self._controller(profile)
        return ServicesSnapshot(
            observed_at=self.deps.clock(),
            services=tuple(
                ServiceView(service_id=service_id, health=status.health.state.value)
                for service_id, status in controller.statuses().items()
            ),
        )

    def vram_snapshot(self) -> VramSnapshot:
        """The first GPU's capacity. Free memory is not observed by the domain yet."""
        facts = self.facts()
        total = facts.gpus[0].vram_total_mib if facts.gpus else None
        return VramSnapshot(observed_at=self.deps.clock(), total_mib=total, free_mib=None)

    def status(self, profile_id: str | None = None) -> AppStatus:
        """What is installed, what is running, what still needs doing."""
        profile = self.profile(profile_id or self._active_profile or self._default_profile())
        snapshot = self._integration()
        statuses = self._controller(profile, snapshot=snapshot).statuses()
        endpoints = self.options.endpoints_for(profile)
        components = []
        for service_id in START_ORDER:
            component = snapshot.by_service(service_id)
            status = statuses.get(service_id)
            configured = profile.services.get(service_id)
            components.append(
                ComponentStatus(
                    service_id=service_id,
                    enabled=bool(configured and configured.enabled),
                    ownership=_value(component.ownership) if component else "none",
                    availability=_value(component.availability) if component else "unknown",
                    health=status.health.state.value if status else ServiceState.UNKNOWN.value,
                    runtime=_value(status.runtime.state) if status else "UNKNOWN",
                    controllable=bool(component and component.controllable),
                    endpoint=endpoints.base_url(service_id),
                    detail=safe_text(component.reason if component else "not detected"),
                )
            )
        pending = self.pending_work(profile)
        return AppStatus(
            observed_at=self.deps.clock(),
            profile_id=profile.id,
            installation_ready=not pending,
            pending_work=tuple(pending),
            components=tuple(components),
            gpu=self.vram_snapshot(),
            operation=self._operation,
        )

    def pending_work(self, profile: Profile) -> list[str]:
        """What a dry run of the whole plan would still do. Empty means ready.

        The same measurement idempotence is judged by: a component that would
        install, a model that would be acquired, a configuration that would be
        written. Read-only.
        """
        if not self.layout.root.exists():
            return ["installation root does not exist"]
        plan = self.build_plan(profile)
        phases = stage_managed_plan(plan)
        executor = self.executor(profile)
        pending: list[str] = []
        provisioning = executor.execute(phases.provisioning, dry_run=True)
        for result in provisioning.results:
            if result.outcome is ProvisionOutcome.DRY_RUN:
                pending.append(f"install {result.component_id}")
            elif result.outcome is ProvisionOutcome.FAILED:
                pending.append(f"{result.component_id}: {safe_text(result.detail)}")
        unmapped = self.unmapped_workflows(profile)
        after = executor.execute(phases.after_ollama_ready, dry_run=True)
        for action in after.action_results:
            if action.action is PlanAction.APPLY_WORKFLOW and action.target in unmapped:
                continue
            if not action.ok:
                pending.append(f"{action.action}/{action.target}: {safe_text(action.detail)}")
            elif action.changed:
                pending.append(f"{action.action} {action.target}")
        return pending

    # --- composição ----------------------------------------------------------

    def provision_context(self) -> ProvisionContext:
        return provision_context(
            self.installation,
            runner=self.deps.runner,
            events=self._activity_sink,
            monotonic=self.deps.monotonic,
        )

    def provisioners(self) -> Mapping[str, Any]:
        return self.deps.provisioners(self.options)

    def catalog(self) -> ModelCatalog:
        return self.deps.catalog if self.deps.catalog is not None else model_catalog()

    def model_installer(self, profile: Profile) -> ModelInstaller:
        """One installer per profile, shared by the executor and the resolver."""
        installer = self._installers.get(profile.id)
        if installer is None:
            endpoint = self.options.endpoints_for(profile).ollama
            ollama = (
                self.deps.ollama_client(endpoint)
                if self.deps.ollama_client is not None
                else HttpOllamaClient(endpoint)
            )
            downloader = (
                self.deps.downloader() if self.deps.downloader is not None else Downloader()
            )
            installer = ModelInstaller(
                ManagedModelPaths.from_layout(self.layout), downloader=downloader, ollama=ollama
            )
            self._installers[profile.id] = installer
        return installer

    def resolver(self, profile: Profile) -> StackModelResolver:
        return StackModelResolver(installer=self.model_installer(profile), catalog=self.catalog())

    def install_actions(self, profile: Profile) -> InstallActionHandlers:
        return default_install_actions(
            self.provision_context(),
            repo_root=self.repo_root,
            installer_factory=lambda _profile: self.model_installer(profile),
            catalog=self.catalog(),
            config_writer=self.deps.config_writer,
            options=self.options,
            on_transfer=self._transfer_sink,
        )

    def executor(self, profile: Profile) -> PlanExecutor:
        context = self.provision_context()
        return plan_executor(
            context,
            self.provisioners(),
            actions=self.install_actions(profile),
            options=self.options,
        )

    def registry(self) -> ManagedProcessRegistry:
        if self._registry is None:
            self._registry = ManagedProcessRegistry.from_layout(self.layout)
        return self._registry

    def launcher(self) -> ProcessLauncher:
        """One launcher for the application's lifetime.

        The launcher holds the proof of what it started -- job handles in the
        real one, membership in a controlled one. A launcher per controller
        would lose that proof between two operations, and a later stop could
        no longer tell a descendant from a stranger.
        """
        if self._launcher is None:
            registry = self.registry()
            self._launcher = (
                self.deps.launcher(registry)
                if self.deps.launcher is not None
                else ProcessLauncher(registry)
            )
        return self._launcher

    def preflight(self, profile: Profile) -> ManagedPreflightReport:
        # A managed port held by a process the registry proves is ours is the
        # stack already running, not somebody else on our port.
        managed = frozenset()
        if self.layout.root.exists():
            managed = self.registry().managed_pids(processes=self.deps.list_processes())
        return preflight_managed_stack(
            self.provision_context(),
            profile,
            self.options,
            provisioners=self.provisioners(),
            facts=self.facts(),
            listener_report=self.deps.list_listeners(),
            managed_pids=managed,
        )

    def _external_install_path(self, component_id: str) -> Path | None:
        provisioner = self.provisioners().get(component_id)
        if provisioner is None:
            return None
        detection = provisioner.detect(self.provision_context())
        return detection.path if detection.is_external else None

    def check_conflicts(self, profile_id: str) -> ConflictPreflightReport | None:
        """A typed, resolvable view of any external conflict blocking this profile.

        Performs no filesystem or process mutation and is safe to call as often
        as needed. Returns ``None`` when
        there is nothing a policy could resolve here -- including when the
        profile is simply not installable for an unrelated reason (incompatible
        hardware, low disk, an unrelated port squatter), which stays a normal
        preflight failure reported by :meth:`first_run` itself, exactly as
        before this existed.
        """
        profile = self.profile(profile_id)
        raw = self.preflight(profile)
        listeners = self.deps.list_listeners()
        managed_pids = (
            self.registry().managed_pids(processes=self.deps.list_processes())
            if self.layout.root.exists()
            else frozenset()
        )
        if listeners.outcome is ScanOutcome.OK:
            passive = {
                issue.component_id
                for issue in raw.issues
                if issue.code is ManagedPreflightCode.ALONGSIDE_REQUIRED
                and issue.component_id is not None
                and not _external_endpoint_occupants(
                    raw, issue.component_id, listeners, managed_pids
                )
            }
            if passive:
                self.options = IntegrationOptions(
                    host=self.options.host,
                    port_overrides=self.options.port_overrides,
                    alongside_external=frozenset(self.options.alongside_external | passive),
                )
                self._installers.clear()
                raw = self.preflight(profile)
                listeners = self.deps.list_listeners()
        return self._conflicts.observe(
            profile=profile,
            options=self.options,
            raw_report=raw,
            listeners=listeners,
            managed_pids=managed_pids,
        )

    def current_conflict_preflight(self) -> ConflictPreflightReport | None:
        """The most recently observed conflict, without scanning again.

        Unlike :meth:`check_conflicts`, this never produces a new preflight id:
        it is the read a chooser polls while a decision is pending, so it must
        not invalidate the very id that decision is about to reference.
        """
        return self._conflicts.current_conflict_preflight()

    def resolve_conflict(self, selection: ConflictPolicySelection) -> ConflictResolutionResult:
        """Authorize -- and for STOP, execute -- one policy for the pending conflict.

        On PROCEED this durably folds the resolution into this application's
        own options, so a later :meth:`first_run` finds nothing left to
        resolve and every later preflight sees the isolated endpoint or the
        freed port, never the same conflict again.
        """
        report = self._conflicts.current_conflict_preflight()
        result = self._conflicts.resolve_external_conflicts(selection)
        if result.status is ConflictResolutionStatus.PROCEED and report is not None:
            self.options = merged_options(self.options, report, result.policy)
            self._installers.clear()  # an isolated endpoint may have just changed

        return result

    def unresolved_models(self, profile: Profile) -> Mapping[str, str]:
        return unresolved_roles(profile, self.catalog())

    def unmapped_workflows(self, profile: Profile) -> frozenset[str]:
        """Workflows the manifest itself declares as not mapped yet.

        Their bindings are still ``TBD``, which the manifest says in so many
        words. Nothing can make them ready in this run, so they are reported
        rather than counted as pending work or as a failure.
        """
        specs = self.install_actions(profile).workflow_specs
        return frozenset(
            target
            for target, declaration in profile.workflows.items()
            if (spec := specs.get(declaration.manifest_id)) is not None and not spec.complete
        )

    def build_plan(self, profile: Profile) -> InstallPlan:
        return build_install_plan(profile, installation=self.installation, stack=None)

    def stage(self, plan: InstallPlan) -> ManagedPlanPhases:
        return stage_managed_plan(plan)

    def stack_configuration(self, profile: Profile) -> StackConfiguration | None:
        """The configuration the configure step generates, without writing it.

        Asked through the handler's own dry run, so it is the same object the
        real apply produces. The controller needs it to know which managed
        services have verified configuration and may therefore be launched.
        """
        plan = self.build_plan(profile)
        step = next((item for item in plan.steps if item.action is PlanAction.CONFIGURE), None)
        if step is None:
            return None
        result = self.install_actions(profile).configure(plan, step, True)
        payload = result.payload
        if isinstance(payload, tuple) and payload and isinstance(payload[0], StackConfiguration):
            return payload[0]
        return payload if isinstance(payload, StackConfiguration) else None

    def _integration(self) -> IntegrationSnapshot:
        return detect_integration(self.provision_context(), self.provisioners())

    def _controller(
        self, profile: Profile, *, snapshot: IntegrationSnapshot | None = None
    ) -> ServiceController:
        """A controller for what provisioning owns right now."""
        integration = snapshot if snapshot is not None else self._integration()
        configuration = self.stack_configuration(profile) if self.layout.root.exists() else None
        return build_service_controller(
            integration,
            self.registry(),
            layout=self.layout,
            launcher=self.launcher(),
            configuration=configuration,
            config_writer=self.deps.config_writer,
            endpoints=self.options.endpoints_for(profile),
            http=self.deps.http,
            tcp=self.deps.tcp,
            list_processes=self.deps.list_processes,
            list_listeners_fn=self.deps.list_listeners,
            lookup_process=self.deps.lookup_process,
            monotonic=self.deps.monotonic,
            sleep=self.deps.sleep,
        )

    def _default_profile(self) -> str:
        if not self.profiles:
            raise UnknownProfile("this application loaded no profiles")
        return next(iter(self.profiles))

    # --- operações -----------------------------------------------------------

    def first_run(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        """Install, acquire, configure and start everything the profile needs.

        Raises :class:`ConflictResolutionRequired` -- before anything is
        created, no operation and no terminal result -- when an external
        service conflicts with this profile and nobody has resolved it yet.
        Call :meth:`resolve_conflict` first; once it reports PROCEED, this
        proceeds exactly as it would with no conflict at all.
        """
        from .first_run import FirstRunCoordinator

        profile = self.profile(profile_id)
        pending = self.check_conflicts(profile.id)
        if pending is not None:
            raise ConflictResolutionRequired(pending)
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            coordinator = FirstRunCoordinator(self, profile, stream)
            self.last_first_run = coordinator.report
            coordinator.run()
        return _result_of(stream)

    def start_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            stream.begin("Verificando serviços")
            self._start_services(stream, profile, START_ORDER)
        return _result_of(stream)

    def stop_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            stream.begin("Verificando serviços")
            self._stop_services(stream, profile)
        return _result_of(stream)

    def restart_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            stream.begin("Verificando serviços")
            stopped = self._stop_services(stream, profile, finish=False)
            if stopped:
                stream.fail(OperationPhase.RESOURCE_RELEASE, "Não foi possível reiniciar", stopped)
            else:
                self._start_services(stream, profile, START_ORDER)
        return _result_of(stream)

    def open_webui(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OpenWebUILaunch:
        """Make Open WebUI reachable and say where. Opens nothing itself.

        The URL comes from the endpoint domain for this profile and is checked
        to be loopback before it is handed over; a UI opens it or does not.
        """
        profile = self.profile(profile_id)
        service_id = str(ComponentId.OPEN_WEBUI)
        launch: OpenWebUILaunch | None = None
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            stream.begin("Verificando Open WebUI")
            launch = self._open_webui(stream, profile, service_id)
        return launch if launch is not None else OpenWebUILaunch(_result_of(stream), None)

    def _open_webui(
        self, stream: OperationStream, profile: Profile, service_id: str
    ) -> OpenWebUILaunch:
        if True:
            endpoints = self.options.endpoints_for(profile)
            if not endpoints.enabled(profile, service_id):
                result = stream.fail(
                    OperationPhase.PREPARATION,
                    "Este perfil não habilita o Open WebUI",
                    [diagnostic("open-webui-disabled", "profile does not enable open-webui")],
                )
                return OpenWebUILaunch(result, None)

            controller = self._controller(profile)
            current = controller.status(service_id)
            if current.health.state is not ServiceState.UP:
                # Dependencies first, then the service itself, all bounded by the
                # controller's own readiness wait.
                failure = self._start_services(
                    stream,
                    profile,
                    START_ORDER[: START_ORDER.index(service_id) + 1],
                    finish=False,
                    controller=controller,
                )
                if failure:
                    result = stream.fail(
                        OperationPhase.SERVICE_START, "Open WebUI não ficou pronto", failure
                    )
                    return OpenWebUILaunch(result, None)
                current = controller.status(service_id)

            ready = current.health.state is ServiceState.UP
            target = OpenWebUILaunchTarget(
                url=endpoints.open_webui,
                service_id=service_id,
                origin=_value(current.origin),
                ready=ready,
                detail=safe_text(current.health.detail),
            )
            if not ready:
                result = stream.fail(
                    OperationPhase.SERVICE_START,
                    "Open WebUI não está respondendo",
                    [diagnostic("open-webui-not-ready", current.health.detail)],
                )
                return OpenWebUILaunch(result, target)
            result = stream.succeed(OperationPhase.FINALIZATION, "Open WebUI pronto")
            return OpenWebUILaunch(result, target)

    # --- ciclo de vida -------------------------------------------------------

    def _start_services(
        self,
        stream: OperationStream,
        profile: Profile,
        order: Sequence[str],
        *,
        finish: bool = True,
        controller: ServiceController | None = None,
    ):
        """Start each enabled managed service in order. External is observed only.

        Returns the diagnostics when ``finish`` is false, so a caller can decide
        the terminal message; otherwise finishes the stream itself.
        """
        snapshot = self._integration()
        controller = (
            controller if controller is not None else self._controller(profile, snapshot=snapshot)
        )
        endpoints = self.options.endpoints_for(profile)
        problems = []
        started = []
        for service_id in order:
            if not endpoints.enabled(profile, service_id):
                continue
            current = controller.status(service_id)
            if current.health.state is ServiceState.UP:
                started.append(service_id)
                continue
            component = snapshot.by_service(service_id)
            external = current.origin is ProcessOrigin.EXTERNAL or (
                component is not None and _value(component.availability) == "external"
            )
            if external:
                # Somebody else's install or process: never started by the hub.
                problems.append(
                    diagnostic(
                        "external-not-ready",
                        f"{service_id} is external and not answering; the hub never starts it",
                    )
                )
                continue
            if not current.spec.controllable:
                reason = component.reason if component is not None else "not detected"
                problems.append(
                    diagnostic("not-launchable", f"{service_id} cannot be started: {reason}")
                )
                continue
            stream.advance(
                OperationState.FINALIZING if finish else OperationState.PREPARING,
                OperationPhase.SERVICE_START,
                f"Iniciando {_display(service_id)}",
            )
            report = controller.start(service_id, wait=True)
            if not report.ready:
                problems.append(diagnostic("service-not-ready", f"{service_id}: {report.detail}"))
                break
            started.append(service_id)
        if not finish:
            return problems
        if problems:
            return stream.fail(OperationPhase.SERVICE_START, "Falha ao iniciar a stack", problems)
        return stream.succeed(
            OperationPhase.FINALIZATION,
            "Stack pronta" if started else "Nada para iniciar",
            output_references=tuple(started),
        )

    def _stop_services(self, stream: OperationStream, profile: Profile, *, finish: bool = True):
        controller = self._controller(profile)
        problems = []
        stopped = []
        for service_id in reversed(START_ORDER):
            current = controller.status(service_id)
            if current.runtime.state is not RuntimeProcessState.RUNNING:
                continue
            if not current.stoppable:
                # External or unowned: reported, never killed.
                continue
            stream.advance(
                OperationState.RUNNING,
                OperationPhase.RESOURCE_RELEASE,
                f"Parando {_display(service_id)}",
            )
            result = controller.stop(service_id)
            if not result.stopped:
                problems.append(diagnostic("service-not-stopped", f"{service_id}: {result.detail}"))
                continue
            stopped.append(service_id)
        if not finish:
            return problems
        if problems:
            return stream.fail(OperationPhase.RESOURCE_RELEASE, "Falha ao parar a stack", problems)
        return stream.succeed(
            OperationPhase.FINALIZATION,
            "Stack parada" if stopped else "Nada para parar",
            output_references=tuple(stopped),
        )

    # --- stream --------------------------------------------------------------

    def _operation_stream(self, operation_id: str, emit: OperationEventSink | None):
        return _OperationScope(self, operation_id, emit)

    def _publish(self, event: OperationEvent) -> None:
        """Store first, then tell every subscriber. Observers cannot raise into us."""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscription in subscribers:
            subscription.deliver(event)

    def _store(self, snapshot: OperationSnapshot | None) -> None:
        with self._lock:
            self._operation = snapshot


class _OperationScope:
    """Holds the single-operation lock for the duration of one operation.

    Whatever happens inside -- including an exception the coordinator failed to
    turn into a result -- the stream ends with exactly one terminal event.
    """

    def __init__(
        self, app: StackApplication, operation_id: str, emit: OperationEventSink | None
    ) -> None:
        self.app = app
        self.stream = OperationStream(
            operation_id,
            emit=self._emit,
            clock=app.deps.clock,
            monotonic=app.deps.monotonic,
        )
        self._external_sink = emit
        self._acquired = False

    def _emit(self, event: OperationEvent) -> None:
        self.app._store(self.stream.snapshot)
        if self._external_sink is not None:
            try:
                self._external_sink(event)
            except Exception as exc:  # noqa: BLE001 - observers cannot change the outcome
                self.stream.observer_errors.append(f"{type(exc).__name__}: {safe_text(exc)}")
        self.app._publish(event)

    def __enter__(self) -> OperationStream:
        with self.app._lock:
            if self.app._busy:
                raise OperationBusy("another operation is already running")
            self.app._busy = True
        self._acquired = True
        return self.stream

    def __exit__(self, exc_type, exc, _tb) -> bool:
        try:
            if exc is not None and not self.stream.closed:
                phase = self.stream.phase or OperationPhase.PREPARATION
                if self.stream.snapshot is None:
                    self.stream.begin("Preparando")
                self.stream.fail(
                    phase,
                    "Não foi possível concluir a operação",
                    [diagnostic("unexpected-error", f"{type(exc).__name__}: {exc}")],
                )
            elif exc is None and not self.stream.closed:
                # A coordinator that returned without a result is a bug, and the
                # stream must still end.
                phase = self.stream.phase or OperationPhase.PREPARATION
                if self.stream.snapshot is None:
                    self.stream.begin("Preparando")
                self.stream.fail(
                    phase,
                    "A operação terminou sem resultado",
                    [diagnostic("missing-result", "the coordinator produced no result")],
                )
        finally:
            if self._acquired:
                with self.app._lock:
                    self.app._busy = False
        # An exception is turned into a FAILED result; it does not propagate.
        return exc is not None


class _Subscription:
    def __init__(self, app: StackApplication, sink: OperationEventSink) -> None:
        self._app = app
        self._sink = sink
        self._closed = False
        self._seen: tuple[str, int] | None = None

    def deliver(self, event: OperationEvent) -> None:
        if self._closed:
            return
        key = (event.operation_id, event.sequence)
        if self._seen == key:
            return
        self._seen = key
        try:
            self._sink(event)
        except Exception:  # noqa: BLE001 - a consumer error never reaches the producer
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._app._lock:
            if self in self._app._subscribers:
                self._app._subscribers.remove(self)


# --- helpers -----------------------------------------------------------------


def _result_of(stream: OperationStream) -> OperationResult:
    """The result a scope guaranteed. Never None once the scope has exited."""
    if stream.result is None:  # pragma: no cover - the scope always ends the stream
        raise RuntimeError("an operation ended without a result")
    return stream.result


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


_DISPLAY = {
    str(ComponentId.OLLAMA): "Ollama",
    str(ComponentId.OPEN_WEBUI): "Open WebUI",
    str(ComponentId.COMFYUI): "ComfyUI",
}


def _display(service_id: str) -> str:
    return _DISPLAY.get(service_id, service_id)


def execution_problems(report: ExecutionReport) -> list[str]:
    """Every failure an execution report carries, in the domain's own words."""
    problems = [
        f"{item.component_id}: {item.failure.message if item.failure is not None else item.detail}"
        for item in report.results
        if not item.ok
    ]
    problems += [
        f"{item.action}/{item.target}: {item.detail}"
        for item in report.action_results
        if not item.ok
    ]
    return problems


__all__ = [
    "LOOPBACK_HOSTS",
    "START_ORDER",
    "AppStatus",
    "ComponentStatus",
    "ConflictResolutionRequired",
    "OpenWebUILaunch",
    "OpenWebUILaunchTarget",
    "OperationBusy",
    "StackApplication",
    "StackDependencies",
    "UnknownProfile",
    "execution_problems",
]
