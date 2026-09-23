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

import contextlib
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
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
from .endpoints import StackEndpoints
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
from .installation import ResolvedInstallation
from .integration import (
    IntegrationOptions,
    IntegrationSnapshot,
    ManagedPlanPhases,
    ManagedPreflightCode,
    ManagedPreflightIssue,
    ManagedPreflightReport,
    build_service_controller,
    default_install_actions,
    default_provisioners,
    detect_integration,
    endpoint_collisions,
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
from .models import ManagedModelPaths, OllamaModelSource
from .ollama_models import HttpOllamaClient
from .open_webui_filters import FilterSync, FilterSyncReport, sync_image_chat_filter
from .open_webui_image_edit import sync_image_edit
from .open_webui_models import sync_chat_model
from .operation_contracts import (
    OperationActivity,
    OperationActivityCategory,
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
from .profiles import (
    CHAT_PRIMARY_ROLE,
    FEATURE_IMAGE_EDITING,
    FEATURE_IMAGE_GENERATION,
    FEATURE_TOOLS,
    IMAGE_EDIT_WORKFLOW,
    Profile,
    load_profiles,
)
from .provisioning import ProvisionContext, ProvisionEvent, ProvisionOutcome
from .registry import IdentityVerdict, ManagedProcessRegistry
from .resources import resource_root
from .runner import CommandRunner
from .service_config import (
    ImageEditSettings,
    StackConfiguration,
    declared_capability_problems,
    open_webui_image_edit_settings,
)
from .services import ServiceState
from .system import SystemFacts, detect_system
from .workflows import (
    WorkflowError,
    load_workflow_graph,
    resolve_workflow,
    verify_workflow_references,
)

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
    # Keeps the Hub's image chat filter current inside a running Open WebUI.
    filter_sync: Callable[[Path, str], FilterSyncReport] = sync_image_chat_filter
    # Keeps the profile's chat model configured for tool calling in that Open WebUI.
    model_sync: Callable[..., FilterSyncReport] = sync_chat_model
    # Keeps that Open WebUI's image editing pointed at the profile's edit workflow.
    image_edit_sync: Callable[[Path, str, ImageEditSettings], FilterSyncReport] = sync_image_edit
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
        # Where profiles and manifests are read from: the checkout, or the
        # bundle of a frozen build. Not the executable's folder.
        self.repo_root = Path(repo_root) if repo_root is not None else resource_root()
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
        with self._detection_snapshot(profile):
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
        with self._detection_snapshot(profile):
            return self._status(profile)

    def _status(self, profile: Profile) -> AppStatus:
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
        with self._detection_snapshot(profile):
            return self._pending_work(profile)

    def _pending_work(self, profile: Profile) -> list[str]:
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

    @contextlib.contextmanager
    def _detection_snapshot(self, profile: Profile) -> Iterator[None]:
        """One model detection per model for the duration of a read-only pass.

        A status pass resolves the same model from the workflow graph, the
        service configuration and the plan's dry run. Inside this block the
        installer answers each model once; outside it, every question reads
        the world again. Operations that acquire models never run inside one.
        """
        with self.model_installer(profile).detection_snapshot():
            yield

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

    def managed_pids(self) -> frozenset[int]:
        """Every pid this manager can prove belongs to a service it started.

        The roots are the registry's verified entries. The process serving a
        port is usually a descendant of its root -- the interpreter behind
        ``open-webui.exe``, the child of a venv ``python.exe`` -- so a root
        alone would make the running stack look like a stranger on our port.
        Descendants are proved two ways, and either suffices:

        * the launcher's kernel job, when this session started the tree. The
          kernel enumerates the members; nothing is inferred;
        * ancestry within one process scan, creation-time ordered at every hop
          (:func:`registry.proven_descendants`). This is what remains after
          the Hub restarts and the job proof has died with its session.

        Anything neither proves is not ours, whatever port it holds or name
        it carries. This answers what may be *recognised* as the stack; what
        may be stopped is decided by the launcher on its own proof.
        """
        if not self.layout.root.exists():
            return frozenset()
        processes = self.deps.list_processes()
        registry = self.registry()
        pids = set(registry.managed_pids(processes=processes))
        launcher = self.launcher()
        for status in registry.statuses(processes=processes).values():
            if status.live:
                pids.update(launcher.tree_pids(status.service_id, status.entry.pid) or ())
        return frozenset(pids)

    def surviving_services(self) -> tuple[str, ...]:
        """Services this installation started that it cannot prove are gone.

        Asked of the managed-process registry, the authority on what this
        installation owns: an entry whose process is still alive -- or whose
        process can no longer be inspected -- is a service still running as
        far as anyone can prove. An entry whose process is gone, or whose pid
        now belongs to a stranger, is not. A registry that cannot be read
        proves nothing, so it raises instead of answering "none".
        """
        registry = self.registry()
        snapshot = registry.load()
        if not snapshot.ok:
            raise RuntimeError(f"managed-process registry is unreadable: {snapshot.error}")
        if not snapshot.entries:
            return ()
        statuses = registry.statuses(processes=self.deps.list_processes())
        return tuple(
            sorted(
                service_id
                for service_id, status in statuses.items()
                if status.verdict in (IdentityVerdict.LIVE, IdentityVerdict.UNKNOWN)
            )
        )

    def endpoint_collisions(self, profile_id: str) -> tuple[ManagedPreflightIssue, ...]:
        """What holds this profile's endpoints right now, as the preflight sees it.

        The port half of :meth:`preflight`, with the same rule and the same
        scans: an unreadable listener scan is an issue, and a port held by
        anything this installation cannot prove it started is a collision.
        Read-only; safe to ask before another installation takes the ports.
        """
        profile = self.profile(profile_id)
        return endpoint_collisions(
            self.options.endpoints_for(profile),
            profile,
            self.deps.list_listeners(),
            self.managed_pids(),
        )

    def preflight(self, profile: Profile) -> ManagedPreflightReport:
        # A managed port held by a process proved to be ours is the stack
        # already running, not somebody else on our port.
        return preflight_managed_stack(
            self.provision_context(),
            profile,
            self.options,
            provisioners=self.provisioners(),
            facts=self.facts(),
            listener_report=self.deps.list_listeners(),
            managed_pids=self.managed_pids(),
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
        managed_pids = self.managed_pids()
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

    def capability_problems(self, profile: Profile) -> tuple[str, ...]:
        """Enabled capabilities this profile cannot configure, before installing."""
        return declared_capability_problems(profile, self.install_actions(profile).workflow_specs)

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
        with self._detection_snapshot(profile):
            return self._stack_configuration(profile)

    def _stack_configuration(self, profile: Profile) -> StackConfiguration | None:
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

    def repair(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        """Bring an existing installation back to what its profile needs.

        The same idempotent steps as :meth:`first_run`, with one difference
        that matters to the person who asked: the stack ends the way it was
        found. A stopped stack is still stopped afterwards -- whatever the
        repair had to start to check and acquire models is stopped again --
        and a running one is left running. An external service in the way is
        reported, not resolved: a repair makes no conflict decisions.
        """
        from .first_run import FirstRunCoordinator

        profile = self.profile(profile_id)
        with self._operation_stream(operation_id, emit) as stream:
            self._active_profile = profile.id
            if self.check_conflicts(profile.id) is not None:
                stream.begin("Verificando instalação")
                stream.fail(
                    OperationPhase.PREPARATION,
                    "Um serviço externo está usando as portas desta instalação",
                    [
                        diagnostic(
                            "external-conflict",
                            "an external service holds this installation's endpoints; "
                            "a repair does not decide conflicts",
                        )
                    ],
                )
            else:
                coordinator = FirstRunCoordinator(self, profile, stream, keep_lifecycle=True)
                self.last_first_run = coordinator.report
                coordinator.run()
        return _result_of(stream)

    def start_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with (
            self._operation_stream(operation_id, emit) as stream,
            self._detection_snapshot(profile),
        ):
            self._active_profile = profile.id
            stream.begin("Verificando serviços")
            self._start_services(stream, profile, START_ORDER)
        return _result_of(stream)

    def stop_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with (
            self._operation_stream(operation_id, emit) as stream,
            self._detection_snapshot(profile),
        ):
            self._active_profile = profile.id
            stream.begin("Verificando serviços")
            self._stop_services(stream, profile)
        return _result_of(stream)

    def restart_stack(
        self, operation_id: str, profile_id: str, *, emit: OperationEventSink | None = None
    ) -> OperationResult:
        profile = self.profile(profile_id)
        with (
            self._operation_stream(operation_id, emit) as stream,
            self._detection_snapshot(profile),
        ):
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
        with (
            self._operation_stream(operation_id, emit) as stream,
            self._detection_snapshot(profile),
        ):
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
            warnings = []
            if current.health.state is ServiceState.UP:
                # Already serving: still make sure the Hub's filter is current
                # in it, so a stack started before an upgrade catches up here.
                warnings = self._integrate(stream, profile, service_id)
            else:
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
            result = stream.succeed(
                OperationPhase.FINALIZATION, "Open WebUI pronto", diagnostics=warnings
            )
            return OpenWebUILaunch(result, target)

    # --- integrações ---------------------------------------------------------

    def integrate_open_webui(
        self, stream: OperationStream, profile: Profile
    ) -> FilterSyncReport | None:
        """Once Open WebUI answers, keep what the profile needs from it current.

        Three things live in Open WebUI's own database and are synced through its
        API every time the service is up: the image chat filter that makes the
        UI's Image feature reach ComfyUI (``assets/open-webui/pepito_image_chat.py``,
        when the profile enables image generation), the chat model record that
        keeps Open WebUI's builtin tools out of the model's prompt and carries
        what the profile asks of the model at every request
        (``open_webui_models``, when the profile enables tools or declares
        ``models.chat.params``), and the image
        edit settings that point Open WebUI's own edit path at the profile's
        ComfyUI edit workflow (``open_webui_image_edit``, when the profile
        enables image editing and that workflow is ready). Each is installed
        once, updated when it drifted, and otherwise left alone. The report is
        the most significant of them; None when the profile has no use for any.
        """
        endpoints = self.options.endpoints_for(profile)
        if not endpoints.enabled(profile, str(ComponentId.OPEN_WEBUI)):
            return None
        syncs: list[tuple[str, Callable[[], FilterSyncReport]]] = []
        if profile.enables(FEATURE_IMAGE_GENERATION):
            syncs.append(
                (
                    "filtro de imagem",
                    lambda: self.deps.filter_sync(
                        self.layout.open_webui_data, endpoints.open_webui
                    ),
                )
            )
        chat_model = self._chat_model_reference(profile)
        # Two independent reasons to own the record: keeping Open WebUI's builtin
        # tools out of the prompt (the tools capability), and what the profile
        # asks of the model at every request (models.chat.params). Either one on
        # its own is enough; a profile with neither never touches the record.
        if chat_model is not None and (profile.enables(FEATURE_TOOLS) or profile.chat_params):
            extra = {"params": profile.chat_params} if profile.chat_params else {}
            syncs.append(
                (
                    "modelo de chat",
                    lambda: self.deps.model_sync(
                        self.layout.open_webui_data, endpoints.open_webui, chat_model, **extra
                    ),
                )
            )
        if profile.enables(FEATURE_IMAGE_EDITING):
            edit = self._image_edit_sync(profile, endpoints)
            if edit is not None:
                syncs.append(("edição de imagem", edit))
        if not syncs:
            return None
        stream.activity(
            OperationActivity(OperationActivityCategory.CONFIGURATION, "integrating", "Open WebUI"),
            "Integrando o Open WebUI",
        )
        reports = []
        for subject, sync in syncs:
            report = sync()
            reports.append(report)
            stream.detail(
                OperationActivity(
                    OperationActivityCategory.CONFIGURATION, report.outcome.value, "Open WebUI"
                ),
                f"Open WebUI: {subject} "
                f"{_INTEGRATION_WORDS.get(report.outcome.value, report.outcome.value)}",
            )
        return _combined_report(reports)

    def _image_edit_sync(
        self, profile: Profile, endpoints: StackEndpoints
    ) -> Callable[[], FilterSyncReport] | None:
        """The edit settings sync for this profile, or None while its workflow is unmapped.

        A workflow the manifest still declares as unmapped is reported by the
        run that skips it; there is nothing to sync yet. A ready one whose
        settings cannot be built is a warning with the reason, never a guess.
        """
        declaration = profile.workflows.get(IMAGE_EDIT_WORKFLOW)
        handlers = self.install_actions(profile)
        spec = handlers.workflow_specs.get(declaration.manifest_id) if declaration else None
        if spec is None or not spec.complete:
            return None
        resolver = self.resolver(profile)
        with resolver.installer.detection_snapshot():
            resolution = resolve_workflow(spec, profile, resolver, repo_root=self.repo_root)
        graph = None
        if resolution.ready:
            try:
                graph = load_workflow_graph(resolution)
                problems = verify_workflow_references(resolution, graph)
            except (OSError, ValueError, WorkflowError) as exc:
                problems = (str(exc),)
            if problems:
                detail = f"image editing: workflow {spec.id!r} does not match its graph: " + (
                    "; ".join(problems)
                )
                return lambda: FilterSyncReport(FilterSync.UNAVAILABLE, detail)
        settings, problems = open_webui_image_edit_settings(endpoints, resolution, graph)
        if settings is None:
            detail = "; ".join(problems)
            return lambda: FilterSyncReport(FilterSync.UNAVAILABLE, detail)
        return lambda: self.deps.image_edit_sync(
            self.layout.open_webui_data, endpoints.open_webui, settings
        )

    def _chat_model_reference(self, profile: Profile) -> str | None:
        """The Ollama tag of the profile's primary chat model, when the catalog pins one."""
        model_id = profile.model_for(CHAT_PRIMARY_ROLE)
        spec = self.catalog().get(model_id) if model_id is not None else None
        if spec is None or not isinstance(spec.source, OllamaModelSource):
            return None
        return spec.source.reference

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
        warnings = []
        started = []
        for service_id in order:
            if not endpoints.enabled(profile, service_id):
                continue
            current = controller.status(service_id)
            if current.health.state is ServiceState.UP:
                started.append(service_id)
                warnings.extend(self._integrate(stream, profile, service_id))
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
            warnings.extend(self._integrate(stream, profile, service_id))
            if service_id == str(ComponentId.OLLAMA):
                # The chat model lives in the Ollama that just came up. Launch
                # specs built while it was down could not configure Open WebUI's
                # chat (the model store was unreachable), so the services still
                # to start are built again on what Ollama answers now.
                self.model_installer(profile).forget_detections()
                controller = self._controller(profile, snapshot=snapshot)
        if not finish:
            return problems
        if problems:
            return stream.fail(OperationPhase.SERVICE_START, "Falha ao iniciar a stack", problems)
        return stream.succeed(
            OperationPhase.FINALIZATION,
            "Stack pronta" if started else "Nada para iniciar",
            output_references=tuple(started),
            diagnostics=warnings,
        )

    def _integrate(self, stream: OperationStream, profile: Profile, service_id: str) -> list:
        """What a running service still needs; a warning when it cannot be given."""
        if service_id != str(ComponentId.OPEN_WEBUI):
            return []
        report = self.integrate_open_webui(stream, profile)
        if report is None or report.ok:
            return []
        return [diagnostic("open-webui-integration", report.detail)]

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
            if result.left_running:
                # The registered process is gone, but members of its tree proved
                # to be ours are not: the service has not stopped.
                problems.append(
                    diagnostic(
                        "service-left-running",
                        f"{service_id}: {len(result.left_running)} process(es) still running",
                    )
                )
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


_INTEGRATION_WORDS = {
    "installed": "instalado",
    "updated": "atualizado",
    "unchanged": "já atual",
    "no-admin": "aguarda o primeiro administrador",
    "unavailable": "não pôde ser verificado",
    "refused": "pertence a outra configuração",
}


def _combined_report(reports: Sequence[FilterSyncReport]) -> FilterSyncReport:
    """One report for several syncs: a failure outranks work done, which outranks no-ops."""
    for outcome in (
        FilterSync.REFUSED,
        FilterSync.UNAVAILABLE,
        FilterSync.INSTALLED,
        FilterSync.UPDATED,
        FilterSync.NO_ADMIN,
    ):
        matching = [report for report in reports if report.outcome is outcome]
        if matching:
            return FilterSyncReport(outcome, "; ".join(report.detail for report in matching))
    return FilterSyncReport(FilterSync.UNCHANGED, "; ".join(report.detail for report in reports))


_DISPLAY = {
    str(ComponentId.OLLAMA): "Ollama",
    str(ComponentId.OPEN_WEBUI): "Open WebUI",
    str(ComponentId.COMFYUI): "ComfyUI",
}


def _display(service_id: str) -> str:
    return _DISPLAY.get(service_id, service_id)


def _provision_problem(item) -> str:
    """One component failure using only the domain's public-safe message."""
    if item.failure is None:
        return f"{item.component_id}: {item.detail}"
    return f"{item.component_id}: {item.failure.message}"


def execution_problems(report: ExecutionReport) -> list[str]:
    """Every failure an execution report carries, in the domain's own words."""
    problems = [_provision_problem(item) for item in report.results if not item.ok]
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
