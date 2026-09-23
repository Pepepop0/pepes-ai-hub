"""The seam where planning, provisioning and runtime meet.

Three layers exist and none of them knows the other two:

    planning      describes work            (InstallPlan)
    provisioning  installs a component      (Provisioner, Detection, Ownership)
    runtime       starts and watches it     (ServiceController, registry)

This module is the only place that wires them together, and it is deliberately
thin. It composes; it does not re-implement. Path logic stays in StackLayout
and in the provisioner, launch mechanics stay in the launcher, and health stays
in the services layer.

The handover is one-directional and narrow. Provisioning answers, per
component, "may this be launched by us, and with which executable?" — a
:class:`~local_ai_stack.provisioning.RuntimeHandoff`. The runtime layer reads
that and nothing else: it never sees an archive, a marker file, a download or
an install root, and it never searches the machine for a binary.

Three cases, and their consequences, are decided here once:

* **EXTERNAL** — the component exists but somebody else installed it. It is
  watched, never controlled. No launch spec is built, so start has nothing to
  spawn and stop is refused by the registry, which never claimed the process.
* **MANAGED** — we installed it, the marker proves it, and the executable is
  present. Only then is a control spec with a launch spec built.
* **NOT_INSTALLED / UNAVAILABLE** — no executable is invented. The service stays
  observation-only and the reason travels with it, so a UI can say why.

The three services are handled by exactly the same rule. Their provisioners
may reach MANAGED only after a complete, verified install; a blocked or partial
install stays observation-only with no executable in its handoff.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from .adapters import control_spec_for
from .catalog import artifact_for, package_for, repository_for
from .comfyui import COMPONENT_ID as COMFYUI_COMPONENT_ID
from .comfyui import provisioner_from_pin as comfyui_provisioner
from .compatibility import CompatibilityReport, evaluate_compatibility
from .components import CANONICAL_IDS
from .controller import ServiceController, ServiceControlSpec
from .download import DownloadProgress
from .endpoints import DEFAULT_HOST, StackEndpoints
from .execution import PlanExecutor, ProvisionerRegistry
from .install_actions import InstallActionHandlers
from .installation import ResolvedInstallation
from .inventory import ListenerReport, ScanOutcome, list_listeners
from .launcher import ProcessLauncher
from .managed_config import ManagedConfigWriter
from .model_catalog import ModelCatalog, model_catalog
from .model_installer import ModelInstaller
from .models import ManagedModelPaths, ModelProvider
from .ollama import COMPONENT_ID as OLLAMA_COMPONENT_ID
from .ollama import OllamaProvisioner
from .ollama_models import HttpOllamaClient
from .open_webui import COMPONENT_ID as OPEN_WEBUI_COMPONENT_ID
from .open_webui import OpenWebUIProvisioner
from .planning import InstallPlan, PlanAction
from .profiles import Profile, load_profiles
from .provisioning import (
    FailureReason,
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    Provisioner,
    RuntimeHandoff,
)
from .python_runtime import COMPONENT_ID as PYTHON_RUNTIME_COMPONENT_ID
from .python_runtime import managed_python_runtime
from .registry import ManagedProcessRegistry
from .resources import resource_root
from .runner import CommandRunner
from .service_config import (
    COMFYUI_SEARCH_PATHS_FILE,
    ServiceConfiguration,
    StackConfiguration,
)
from .services import DEFAULT_SERVICE_SPECS, OLLAMA_SPEC
from .system import SystemFacts, detect_system
from .workflows import WorkflowSpec, load_workflow_manifest

__all__ = [
    "ComponentIntegration",
    "IntegrationOptions",
    "IntegrationSnapshot",
    "ManagedPlanPhases",
    "ManagedPreflightCode",
    "ManagedPreflightIssue",
    "ManagedPreflightReport",
    "build_service_controller",
    "control_specs_for",
    "default_install_actions",
    "default_provisioners",
    "detect_integration",
    "plan_executor",
    "preflight_managed_stack",
    "provision_context",
    "stage_managed_plan",
]


@dataclass(frozen=True)
class IntegrationOptions:
    """Explicit opt-ins shared by provisioning, models, config and runtime.

    Defaults preserve the conservative policy: an external installation causes
    provisioning to skip it, and profile endpoints are used unchanged.  An
    isolated managed stack must opt into coexistence per component and name
    deterministic endpoint overrides here, once.
    """

    host: str = DEFAULT_HOST
    port_overrides: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    alongside_external: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("managed endpoint host must be a non-empty string")
        unknown = sorted(set(self.alongside_external) - set(CANONICAL_IDS))
        if unknown:
            raise ValueError(f"alongside_external names unknown components: {', '.join(unknown)}")
        object.__setattr__(self, "host", self.host.strip())
        object.__setattr__(self, "port_overrides", MappingProxyType(dict(self.port_overrides)))
        object.__setattr__(self, "alongside_external", frozenset(self.alongside_external))

    def endpoints_for(self, profile: Profile) -> StackEndpoints:
        return StackEndpoints.from_profile(
            profile,
            host=self.host,
            port_overrides=self.port_overrides,
        )

    def allows_alongside(self, component_id: str) -> bool:
        return component_id in self.alongside_external


class ManagedPreflightCode(StrEnum):
    INCOMPATIBLE = "incompatible"
    DISK_SPACE = "disk-space"
    LISTENER_SCAN = "listener-scan"
    ENDPOINT_COLLISION = "endpoint-collision"
    DUPLICATE_ENDPOINT = "duplicate-endpoint"
    ALONGSIDE_REQUIRED = "alongside-required"
    PATH_OVERLAP = "path-overlap"


@dataclass(frozen=True)
class ManagedPreflightIssue:
    code: ManagedPreflightCode
    detail: str
    reason: FailureReason = FailureReason.PRECONDITION
    # The component this issue is about, when it is about exactly one. A
    # machine-wide issue (incompatible hardware, low disk, an unusable listener
    # scan) has none. This is how a caller distinguishes "this profile is not
    # installable at all" from "this one external service is in the way",
    # without parsing ``detail``.
    component_id: str | None = None


@dataclass(frozen=True)
class ManagedPreflightReport:
    """Read-only proof that an isolated managed plan may mutate its root."""

    endpoints: StackEndpoints
    model_endpoint: str
    model_store: Path
    free_bytes: int
    compatibility: CompatibilityReport
    external_components: tuple[str, ...] = ()
    issues: tuple[ManagedPreflightIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class ManagedPlanPhases:
    """The runtime boundary inside a full managed install plan.

    Provisioning runs first. The caller then starts the verified managed
    Ollama through ``ServiceController`` and waits for readiness before handing
    ``after_ollama_ready`` to ``PlanExecutor``. The executor never starts a
    service and therefore cannot accidentally fall back to another endpoint.
    """

    provisioning: InstallPlan
    after_ollama_ready: InstallPlan


def _plan_steps(plan: InstallPlan, actions: frozenset[PlanAction]) -> InstallPlan:
    return InstallPlan(
        profile_id=plan.profile_id,
        steps=tuple(step for step in plan.steps if step.action in actions),
        stack_id=plan.stack_id,
        install_mode=plan.install_mode,
        install_root=plan.install_root,
    )


def stage_managed_plan(plan: InstallPlan) -> ManagedPlanPhases:
    """Split work around managed Ollama startup without duplicating runtime."""
    provisioning_actions = frozenset(
        {PlanAction.CHECK, PlanAction.PREPARE_LAYOUT, PlanAction.INSTALL}
    )
    post_start_actions = frozenset(
        {PlanAction.ACQUIRE_MODEL, PlanAction.APPLY_WORKFLOW, PlanAction.CONFIGURE}
    )
    return ManagedPlanPhases(
        provisioning=_plan_steps(plan, provisioning_actions),
        after_ollama_ready=_plan_steps(plan, post_start_actions),
    )


def default_provisioners(options: IntegrationOptions | None = None) -> dict[str, Provisioner]:
    """Every component with a real provisioner today.

    Each one is handed whatever the shipped manifests pin for it. A component
    with nothing pinned still detects, plans and dry-runs; only a real install
    refuses, with PRECONDITION, rather than guessing a URL or a revision.
    """
    resolved_options = options or IntegrationOptions()
    open_webui_pin = package_for(OPEN_WEBUI_COMPONENT_ID)
    # A frozen build gets the pinned managed Python; a checkout gets None and
    # keeps using its own venv. Both Python components share the one runtime.
    python_runtime = managed_python_runtime(artifact_for(PYTHON_RUNTIME_COMPONENT_ID))
    return {
        OLLAMA_COMPONENT_ID: OllamaProvisioner(
            artifact=artifact_for(OLLAMA_COMPONENT_ID),
            install_alongside_external=resolved_options.allows_alongside(OLLAMA_COMPONENT_ID),
        ),
        OPEN_WEBUI_COMPONENT_ID: OpenWebUIProvisioner(
            version=None if open_webui_pin is None else open_webui_pin.version,
            install_alongside_external=resolved_options.allows_alongside(OPEN_WEBUI_COMPONENT_ID),
            python_runtime=python_runtime,
        ),
        COMFYUI_COMPONENT_ID: comfyui_provisioner(
            repository_for(COMFYUI_COMPONENT_ID),
            install_alongside_external=resolved_options.allows_alongside(COMFYUI_COMPONENT_ID),
            python_runtime=python_runtime,
        ),
    }


def _disk_root(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def endpoint_collisions(
    endpoints: StackEndpoints,
    profile: Profile,
    listeners: ListenerReport,
    managed_pids: frozenset[int] = frozenset(),
) -> tuple[ManagedPreflightIssue, ...]:
    """What holds this profile's enabled endpoints: the port half of the preflight.

    A listener scan that cannot be read proves nothing, so it is an issue of
    its own. Otherwise every enabled endpoint held by a process ``managed_pids``
    does not name -- an external service, another installation's leftover, a
    listener nobody can identify -- is a collision.
    """
    if listeners.outcome is ScanOutcome.UNAVAILABLE:
        return (
            ManagedPreflightIssue(
                ManagedPreflightCode.LISTENER_SCAN,
                f"could not prove managed ports are free: {listeners.detail}",
            ),
        )
    issues: list[ManagedPreflightIssue] = []
    for component_id, service in profile.services.items():
        if not service.enabled:
            continue
        spec = endpoints.spec(component_id)
        collisions = [
            listener
            for listener in listeners.on_port(spec.port, spec.host)
            if listener.pid is None or listener.pid not in managed_pids
        ]
        if not collisions:
            continue
        pids = sorted({listener.pid for listener in collisions if listener.pid is not None})
        suffix = f" (pids: {', '.join(map(str, pids))})" if pids else ""
        issues.append(
            ManagedPreflightIssue(
                ManagedPreflightCode.ENDPOINT_COLLISION,
                f"managed endpoint {spec.host}:{spec.port} for {spec.id} is occupied{suffix}",
                component_id=spec.id,
            )
        )
    return tuple(issues)


def preflight_managed_stack(
    context: ProvisionContext,
    profile: Profile,
    options: IntegrationOptions,
    *,
    provisioners: ProvisionerRegistry | None = None,
    facts: SystemFacts | None = None,
    listener_report: ListenerReport | None = None,
    minimum_free_bytes: int = 0,
    managed_pids: frozenset[int] = frozenset(),
) -> ManagedPreflightReport:
    """Prove isolation before an opt-in managed plan mutates anything.

    This function is deliberately read-only.  Callers must require ``ok``
    before layout preparation or any provisioner ``apply``.  An unavailable
    listener scan fails closed because it cannot prove that the selected ports
    are free.

    ``managed_pids`` names processes this manager itself started and still
    owns -- the registry's verified roots and the descendants proved to be
    their tree (``StackApplication.managed_pids``).  A managed port held by one
    of them is not a collision: it is the stack already running, even when the
    process on the port is the interpreter behind a launcher rather than the
    launcher itself.  Anything else on a managed port still is.
    """
    endpoints = options.endpoints_for(profile)
    compatibility = evaluate_compatibility(facts or detect_system(), profile)
    model_paths = ManagedModelPaths.from_layout(context.layout)
    model_store = model_paths.provider_root(ModelProvider.OLLAMA)
    free_bytes = shutil.disk_usage(_disk_root(context.layout.root)).free
    issues: list[ManagedPreflightIssue] = [
        ManagedPreflightIssue(ManagedPreflightCode.INCOMPATIBLE, blocker.message)
        for blocker in compatibility.blockers
    ]
    if minimum_free_bytes < 0:
        raise ValueError("minimum_free_bytes cannot be negative")
    if free_bytes < minimum_free_bytes:
        issues.append(
            ManagedPreflightIssue(
                ManagedPreflightCode.DISK_SPACE,
                f"managed root has {free_bytes} free bytes; {minimum_free_bytes} required",
            )
        )

    registry = dict(default_provisioners(options) if provisioners is None else provisioners)
    external: list[str] = []
    for component_id, provisioner in registry.items():
        detection = provisioner.detect(context)
        if not detection.is_external:
            continue
        external.append(component_id)
        configured = profile.services.get(component_id)
        option_enabled = options.allows_alongside(component_id)
        provisioner_enabled = bool(getattr(provisioner, "install_alongside_external", False))
        if (
            configured is not None
            and configured.enabled
            and not (option_enabled and provisioner_enabled)
        ):
            issues.append(
                ManagedPreflightIssue(
                    ManagedPreflightCode.ALONGSIDE_REQUIRED,
                    f"{component_id} is external and alongside provisioning is not active "
                    "in both the integration request and provisioner",
                    component_id=component_id,
                )
            )
        if detection.path is not None and _paths_overlap(context.layout.root, detection.path):
            issues.append(
                ManagedPreflightIssue(
                    ManagedPreflightCode.PATH_OVERLAP,
                    f"managed root overlaps the external {component_id} path",
                    component_id=component_id,
                )
            )

    enabled_specs = [
        endpoints.spec(component_id)
        for component_id, service in profile.services.items()
        if service.enabled
    ]
    occupied: dict[tuple[str, int], str] = {}
    for spec in enabled_specs:
        key = (spec.host, spec.port)
        previous = occupied.get(key)
        if previous is not None:
            issues.append(
                ManagedPreflightIssue(
                    ManagedPreflightCode.DUPLICATE_ENDPOINT,
                    f"{previous} and {spec.id} both resolve to {spec.host}:{spec.port}",
                )
            )
        occupied[key] = spec.id

    listeners = listener_report or list_listeners()
    issues.extend(endpoint_collisions(endpoints, profile, listeners, managed_pids))

    return ManagedPreflightReport(
        endpoints=endpoints,
        model_endpoint=endpoints.ollama,
        model_store=model_store,
        free_bytes=free_bytes,
        compatibility=compatibility,
        external_components=tuple(sorted(external)),
        issues=tuple(issues),
    )


def provision_context(
    installation: ResolvedInstallation,
    *,
    runner: CommandRunner | None = None,
    events=None,
    monotonic=None,
) -> ProvisionContext:
    """A context bound to one resolved installation. Touches no disk."""
    kwargs = {} if monotonic is None else {"monotonic": monotonic}
    return ProvisionContext(
        installation=installation,
        runner=runner if runner is not None else CommandRunner(),
        events=events,
        **kwargs,
    )


def plan_executor(
    context: ProvisionContext,
    provisioners: ProvisionerRegistry | None = None,
    *,
    actions: InstallActionHandlers | None = None,
    options: IntegrationOptions | None = None,
) -> PlanExecutor:
    """The executor the application uses. Nothing runs until it is asked to."""
    bound_actions = (
        actions if actions is not None else default_install_actions(context, options=options)
    )
    return PlanExecutor(
        context=context,
        provisioners=dict(default_provisioners(options) if provisioners is None else provisioners),
        action_handlers=bound_actions.handlers(),
    )


def default_install_actions(
    context: ProvisionContext,
    *,
    repo_root: Path | None = None,
    profiles: Sequence[Profile] | None = None,
    catalog: ModelCatalog | None = None,
    workflow_specs: Mapping[str, WorkflowSpec] | None = None,
    installer_factory: Callable[[Profile], ModelInstaller] | None = None,
    config_writer: ManagedConfigWriter | None = None,
    options: IntegrationOptions | None = None,
    on_transfer: Callable[[str, DownloadProgress], None] | None = None,
) -> InstallActionHandlers:
    """Bind the shipped profiles/manifests to the three executable domains.

    ``repo_root`` is where the product's own data lives -- ``profiles/``,
    ``manifests/``, ``workflows/`` -- and defaults to :func:`resource_root`,
    which is the checkout on a checkout and the bundle inside a frozen build.
    It is never the folder the executable sits in: that folder is where a
    portable *stack* goes, and holds no manifests.
    """
    root = Path(repo_root) if repo_root is not None else resource_root()
    known_profiles = tuple(profiles) if profiles is not None else load_profiles(root / "profiles")
    known_catalog = catalog if catalog is not None else model_catalog()
    known_workflows = (
        workflow_specs
        if workflow_specs is not None
        else load_workflow_manifest(root / "manifests" / "workflows.yaml")
    )

    resolved_options = options or IntegrationOptions()

    def build_installer(profile: Profile) -> ModelInstaller:
        if installer_factory is not None:
            return installer_factory(profile)
        endpoint = resolved_options.endpoints_for(profile).ollama
        return ModelInstaller(
            ManagedModelPaths.from_layout(context.layout),
            ollama=HttpOllamaClient(endpoint),
        )

    return InstallActionHandlers(
        profiles={profile.id: profile for profile in known_profiles},
        catalog=known_catalog,
        installer_factory=build_installer,
        workflow_specs=known_workflows,
        repo_root=root,
        layout=context.layout,
        config_writer=config_writer or ManagedConfigWriter(),
        endpoints_for=resolved_options.endpoints_for,
        on_transfer=on_transfer,
    )


@dataclass(frozen=True)
class ComponentIntegration:
    """One component, seen from provisioning, ready for the runtime layer."""

    handoff: RuntimeHandoff

    @property
    def component_id(self) -> str:
        return self.handoff.component_id

    @property
    def service_id(self) -> str:
        """The canonical id is shared by provisioning and runtime."""
        return self.handoff.component_id

    @property
    def ownership(self) -> Ownership:
        return self.handoff.ownership

    @property
    def availability(self) -> LaunchAvailability:
        return self.handoff.availability

    @property
    def controllable(self) -> bool:
        """Start/stop/restart are meaningful only for a managed install."""
        return self.handoff.launchable

    @property
    def executable(self) -> Path | None:
        """The path we may launch. None whenever the install is not ours."""
        return self.handoff.executable

    @property
    def detected_path(self) -> Path | None:
        """Where the component was found, ours or not. Observational only."""
        return self.handoff.detected_path

    @property
    def version(self) -> str | None:
        return self.handoff.version

    @property
    def reason(self) -> str:
        """Why control is or is not available, in words a UI can show as-is."""
        return self.handoff.detail


@dataclass(frozen=True)
class IntegrationSnapshot:
    """What provisioning currently says about every component we wire up."""

    components: tuple[ComponentIntegration, ...] = ()

    def by_component(self, component_id: str) -> ComponentIntegration | None:
        return next((c for c in self.components if c.component_id == component_id), None)

    def by_service(self, service_id: str) -> ComponentIntegration | None:
        return next((c for c in self.components if c.service_id == service_id), None)

    @property
    def controllable_ids(self) -> tuple[str, ...]:
        return tuple(c.service_id for c in self.components if c.controllable)


def detect_integration(
    context: ProvisionContext,
    provisioners: ProvisionerRegistry | None = None,
    *,
    options: IntegrationOptions | None = None,
) -> IntegrationSnapshot:
    """Ask every provisioner what the runtime may do. Read-only, creates nothing.

    A component id and a service id are the same canonical string (see
    :mod:`components`), so there is no translation table here to drift.
    """
    registry = default_provisioners(options) if provisioners is None else provisioners
    components = tuple(
        ComponentIntegration(
            handoff=provisioner.runtime_handoff(context),
        )
        for component_id, provisioner in sorted(registry.items())
    )
    return IntegrationSnapshot(components)


def control_specs_for(
    snapshot: IntegrationSnapshot,
    *,
    layout=None,
    models_dir: str | Path | None = None,
    service_options: Mapping[str, Mapping[str, object]] | None = None,
    configuration: StackConfiguration | None = None,
    config_writer: ManagedConfigWriter | None = None,
    endpoints: StackEndpoints | None = None,
) -> tuple[ServiceControlSpec, ...]:
    """Build one control spec per service from what provisioning found.

    A launch spec is built only where a handoff carried an executable, which
    happens only for a MANAGED, usable install. External and missing components
    get the same watch-only spec the read-only layers already use, so health and
    process state keep working while start and stop have nothing to act on.

    Defaults come from the stack layout, never from a guess: a managed install
    keeps its models and its data inside the layout, so it can never write into
    a directory an external install owns.
    """
    options: dict[str, dict[str, object]] = {
        service_id: dict(values) for service_id, values in (service_options or {}).items()
    }
    if models_dir is not None:
        options.setdefault(OLLAMA_SPEC.id, {}).setdefault("models_dir", models_dir)
    elif layout is not None:
        managed_paths = ManagedModelPaths.from_layout(layout)
        options.setdefault(OLLAMA_SPEC.id, {}).setdefault(
            "models_dir", managed_paths.provider_root(ModelProvider.OLLAMA)
        )
    if endpoints is not None:
        for service in DEFAULT_SERVICE_SPECS:
            endpoint = endpoints.spec(service.id)
            service_values = options.setdefault(service.id, {})
            for field_name, expected in (("host", endpoint.host), ("port", endpoint.port)):
                supplied = service_values.get(field_name, expected)
                if supplied != expected:
                    raise ValueError(
                        f"{service.id} {field_name} {supplied!r} conflicts with the "
                        f"managed endpoint authority {expected!r}"
                    )
                service_values[field_name] = expected
    if layout is not None:
        options.setdefault(COMFYUI_COMPONENT_ID, {}).setdefault(
            "script", layout.comfyui_app / "main.py"
        )

    specs: list[ServiceControlSpec] = []
    for service in DEFAULT_SERVICE_SPECS:
        component = snapshot.by_service(service.id)
        executable = (
            component.executable if component is not None and component.controllable else None
        )
        if executable is not None and configuration is not None and service.id != OLLAMA_SPEC.id:
            configured = configuration.services.get(service.id)
            if not _verified_configuration(configured, config_writer):
                executable = None
            elif service.id == OPEN_WEBUI_COMPONENT_ID:
                existing = options.setdefault(service.id, {}).get("env")
                base_env = existing if isinstance(existing, Mapping) else None
                options[service.id]["env"] = configured.merged_env(base_env)
            elif service.id == COMFYUI_COMPONENT_ID:
                document = next(
                    (
                        item
                        for item in configured.documents
                        if item.path.name == COMFYUI_SEARCH_PATHS_FILE
                    ),
                    None,
                )
                # A verified configuration with no search paths is one that
                # gives ComfyUI no managed models to find -- a profile that
                # does not enable image generation. It still launches, on its
                # own model tree; configuration that needed models and lacks
                # them is not ok, and never reaches this point.
                if document is not None:
                    options.setdefault(service.id, {})["extra_model_paths_config"] = document.path
        specs.append(
            control_spec_for(
                service.id,
                executable,
                layout=layout,
                **options.get(service.id, {}),
            )
        )
    return tuple(specs)


def _verified_configuration(
    configuration: ServiceConfiguration | None,
    writer: ManagedConfigWriter | None,
) -> bool:
    """Only generated bytes verified on disk are eligible for a managed launch.

    A configuration that is ok and generates no documents has nothing to
    verify, which is not the same as one that was never built: the second is
    ``None`` or not ok, and both still refuse the launch.
    """
    if configuration is None or not configuration.ok:
        return False
    if not configuration.documents:
        return True
    verifier = writer if writer is not None else ManagedConfigWriter()
    return verifier.verify(configuration.documents).ok


def build_service_controller(
    snapshot: IntegrationSnapshot,
    registry: ManagedProcessRegistry,
    *,
    layout=None,
    launcher: ProcessLauncher | None = None,
    specs: Sequence[ServiceControlSpec] | None = None,
    models_dir: str | Path | None = None,
    service_options: Mapping[str, Mapping[str, object]] | None = None,
    configuration: StackConfiguration | None = None,
    config_writer: ManagedConfigWriter | None = None,
    endpoints: StackEndpoints | None = None,
    **controller_options,
) -> ServiceController:
    """A controller that can only ever control what provisioning owns.

    Extra keyword arguments go straight to :class:`ServiceController`, which is
    how probes and scans are injected instead of reaching into the instance.
    """
    control_specs = (
        control_specs_for(
            snapshot,
            layout=layout,
            models_dir=models_dir,
            service_options=service_options,
            configuration=configuration,
            config_writer=config_writer,
            endpoints=endpoints,
        )
        if specs is None
        else tuple(specs)
    )
    return ServiceController(control_specs, registry, launcher, **controller_options)
