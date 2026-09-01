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

* **EXTERNAL** — Ollama exists but somebody else installed it. It is watched,
  never controlled. No launch spec is built, so start has nothing to spawn and
  stop is refused by the registry, which never claimed the process.
* **MANAGED** — we installed it, the marker proves it, and the executable is
  present. Only then is a control spec with a launch spec built.
* **NOT_INSTALLED / UNAVAILABLE** — no executable is invented. The service stays
  observation-only and the reason travels with it, so a UI can say why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .adapters import observation_only_specs, ollama_control_spec
from .controller import ServiceController, ServiceControlSpec
from .execution import PlanExecutor, ProvisionerRegistry
from .installation import ResolvedInstallation
from .launcher import ProcessLauncher
from .ollama import COMPONENT_ID as OLLAMA_COMPONENT_ID
from .ollama import OllamaProvisioner
from .processes import OLLAMA_PROCESS
from .provisioning import (
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    Provisioner,
    RuntimeHandoff,
)
from .registry import ManagedProcessRegistry
from .runner import CommandRunner
from .services import OLLAMA_SPEC

__all__ = [
    "ComponentIntegration",
    "IntegrationSnapshot",
    "build_service_controller",
    "control_specs_for",
    "default_provisioners",
    "detect_integration",
    "plan_executor",
    "provision_context",
]


def default_provisioners() -> dict[str, Provisioner]:
    """Every component with a real provisioner today.

    Ollama is deliberately constructed without an artifact: no production
    archive is pinned yet, so a real install refuses with PRECONDITION instead
    of guessing a URL. Detection, planning and dry-run all work regardless.
    """
    return {OLLAMA_COMPONENT_ID: OllamaProvisioner()}


def provision_context(
    installation: ResolvedInstallation,
    *,
    runner: CommandRunner | None = None,
    events=None,
) -> ProvisionContext:
    """A context bound to one resolved installation. Touches no disk."""
    return ProvisionContext(
        installation=installation,
        runner=runner if runner is not None else CommandRunner(),
        events=events,
    )


def plan_executor(
    context: ProvisionContext,
    provisioners: ProvisionerRegistry | None = None,
) -> PlanExecutor:
    """The executor the application uses. Nothing runs until it is asked to."""
    return PlanExecutor(
        context=context,
        provisioners=dict(default_provisioners() if provisioners is None else provisioners),
    )


@dataclass(frozen=True)
class ComponentIntegration:
    """One component, seen from provisioning, ready for the runtime layer."""

    handoff: RuntimeHandoff
    service_id: str

    @property
    def component_id(self) -> str:
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
) -> IntegrationSnapshot:
    """Ask every provisioner what the runtime may do. Read-only, creates nothing.

    A component id and a service id are the same canonical string (see
    :mod:`components`), so there is no translation table here to drift.
    """
    registry = default_provisioners() if provisioners is None else provisioners
    components = tuple(
        ComponentIntegration(
            handoff=provisioner.runtime_handoff(context),
            service_id=component_id,
        )
        for component_id, provisioner in sorted(registry.items())
    )
    return IntegrationSnapshot(components)


def control_specs_for(
    snapshot: IntegrationSnapshot,
    *,
    layout=None,
    models_dir: str | Path | None = None,
) -> tuple[ServiceControlSpec, ...]:
    """Build control specs from what provisioning found.

    A launch spec is built only for a MANAGED, usable install. An external or
    missing Ollama yields the same spec the read-only layers already use, so it
    keeps showing health and process state while start and stop have nothing to
    act on.

    A managed Ollama keeps its models inside the stack layout unless a caller
    says otherwise, so a managed install never writes into the model directory
    an external install owns.
    """
    ollama = snapshot.by_component(OLLAMA_COMPONENT_ID)
    if ollama is not None and ollama.controllable and ollama.executable is not None:
        if models_dir is None and layout is not None:
            models_dir = layout.models
        controlled = ollama_control_spec(
            ollama.executable,
            layout=layout,
            models_dir=models_dir,
        )
    else:
        controlled = ServiceControlSpec(OLLAMA_SPEC, OLLAMA_PROCESS)
    return (controlled, *observation_only_specs())


def build_service_controller(
    snapshot: IntegrationSnapshot,
    registry: ManagedProcessRegistry,
    *,
    layout=None,
    launcher: ProcessLauncher | None = None,
    specs: Sequence[ServiceControlSpec] | None = None,
    models_dir: str | Path | None = None,
    **controller_options,
) -> ServiceController:
    """A controller that can only ever control what provisioning owns.

    Extra keyword arguments go straight to :class:`ServiceController`, which is
    how probes and scans are injected instead of reaching into the instance.
    """
    control_specs = (
        control_specs_for(snapshot, layout=layout, models_dir=models_dir)
        if specs is None
        else tuple(specs)
    )
    return ServiceController(control_specs, registry, launcher, **controller_options)
