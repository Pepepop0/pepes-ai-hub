"""One service, seen and controlled through a single object.

A :class:`ServiceControlSpec` ties the four things that already exist for a
service — how to probe its health, how to recognise its process, how to launch
it, and the registry of what we own — into one place, so callers do not have to
assemble them by hand.

Three vocabularies stay separate on purpose and are never merged:

* ``ServiceState`` — is the service *serving*? (health)
* ``RuntimeProcessState`` — is a process *running*? (lifecycle)
* ``ProcessOrigin`` — did *we* start it? (ownership)

Starting a process therefore never asserts health, and a service that answers
its endpoint is not assumed to be ours. Readiness is an explicit, optional wait
that polls the existing health contract; a probe timeout stays UNKNOWN.

A service without a launch spec is observation-only: status works, start and
stop refuse. That is how Open WebUI and ComfyUI behave in this milestone.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .inventory import ListenerReport, LookupOutcome, ProcessReport, list_listeners
from .inventory import list_processes as default_list_processes
from .inventory import lookup_process as default_lookup_process
from .launcher import (
    LaunchOutcome,
    LaunchResult,
    ProcessLauncher,
    ProcessLaunchSpec,
    StopOutcome,
    StopResult,
)
from .probes import http_probe, tcp_probe
from .processes import (
    ProcessOrigin,
    RuntimeProcessSpec,
    RuntimeProcessState,
    RuntimeProcessStatus,
    discover_process,
)
from .registry import ManagedProcess, ManagedProcessRegistry
from .runtime import combine_state
from .services import (
    HttpProbe,
    ServiceSpec,
    ServiceState,
    ServiceStatus,
    TcpProbe,
    check_service,
)

DEFAULT_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class ServiceControlSpec:
    """Health spec, process spec and (optionally) how to launch the service."""

    service: ServiceSpec
    process: RuntimeProcessSpec
    launch: ProcessLaunchSpec | None = None

    def __post_init__(self) -> None:
        if self.service.id != self.process.service_id:
            raise ValueError(
                f"service id mismatch: {self.service.id!r} vs {self.process.service_id!r}"
            )
        if self.launch is not None and self.launch.service_id != self.service.id:
            raise ValueError(
                f"launch spec belongs to {self.launch.service_id!r}, not {self.service.id!r}"
            )

    @property
    def service_id(self) -> str:
        return self.service.id

    @property
    def name(self) -> str:
        return self.service.name

    @property
    def controllable(self) -> bool:
        """False for services we can watch but not start in this milestone."""
        return self.launch is not None


@dataclass(frozen=True)
class ServiceControlStatus:
    """Everything known about one service, with each concept kept apart."""

    spec: ServiceControlSpec
    runtime: RuntimeProcessStatus
    health: ServiceStatus
    managed: ManagedProcess | None = None

    @property
    def service_id(self) -> str:
        return self.spec.service_id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def origin(self) -> ProcessOrigin:
        return self.runtime.origin

    @property
    def controllable(self) -> bool:
        return self.spec.controllable

    @property
    def stoppable(self) -> bool:
        """We may only stop what we started and still own."""
        return self.managed is not None and self.runtime.origin is ProcessOrigin.MANAGED

    @property
    def state(self) -> ServiceState:
        """Convenience roll-up. The two source concepts stay available above."""
        return combine_state(self.health.state, self.runtime.state)


@dataclass(frozen=True)
class StartReport:
    """What starting produced: a launch outcome and, separately, a readiness."""

    launch: LaunchResult
    process_state: RuntimeProcessState
    health: ServiceStatus | None = None
    detail: str = ""

    @property
    def service_id(self) -> str:
        return self.launch.service_id

    @property
    def outcome(self) -> LaunchOutcome:
        return self.launch.outcome

    @property
    def pid(self) -> int | None:
        return self.launch.pid

    @property
    def ready(self) -> bool:
        """Process running *and* the service answering. Never inferred."""
        return (
            self.process_state is RuntimeProcessState.RUNNING
            and self.health is not None
            and self.health.state is ServiceState.UP
        )


@dataclass(frozen=True)
class RestartReport:
    stop: StopResult
    start: StartReport | None = None
    detail: str = ""

    @property
    def restarted(self) -> bool:
        return self.start is not None and self.start.launch.started


class ServiceController:
    """Start, stop, restart and inspect the services we know how to control."""

    def __init__(
        self,
        specs: Sequence[ServiceControlSpec] | Mapping[str, ServiceControlSpec],
        registry: ManagedProcessRegistry,
        launcher: ProcessLauncher | None = None,
        *,
        http: HttpProbe = http_probe,
        tcp: TcpProbe = tcp_probe,
        list_processes: Callable[[], ProcessReport] = default_list_processes,
        list_listeners_fn: Callable[[], ListenerReport] = list_listeners,
        lookup_process: Callable[[int], object] = default_lookup_process,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        values = specs.values() if isinstance(specs, Mapping) else specs
        self.specs: dict[str, ServiceControlSpec] = {spec.service_id: spec for spec in values}
        self.registry = registry
        self.launcher = launcher or ProcessLauncher(registry)
        self._http = http
        self._tcp = tcp
        self._list_processes = list_processes
        self._list_listeners = list_listeners_fn
        self._lookup_process = lookup_process
        self._monotonic = monotonic
        self._sleep = sleep
        self._poll_interval = poll_interval

    def spec_for(self, service_id: str) -> ServiceControlSpec:
        try:
            return self.specs[service_id]
        except KeyError:
            known = ", ".join(sorted(self.specs)) or "none"
            raise KeyError(f"unknown service {service_id!r} (known: {known})") from None

    # --- status ------------------------------------------------------------

    def status(
        self,
        service_id: str,
        *,
        processes: ProcessReport | None = None,
        listeners: ListenerReport | None = None,
    ) -> ServiceControlStatus:
        spec = self.spec_for(service_id)
        process_report = self._list_processes() if processes is None else processes
        listener_report = self._list_listeners() if listeners is None else listeners
        entry_status = self.registry.status(service_id, processes=process_report)
        managed_pids = (
            frozenset({entry_status.entry.pid})
            if entry_status is not None and entry_status.live
            else frozenset()
        )
        runtime = discover_process(
            spec.process,
            processes=process_report,
            listeners=listener_report,
            managed_pids=managed_pids,
        )
        health = check_service(
            spec.service, http=self._http, tcp=self._tcp, listeners=listener_report
        )
        managed = entry_status.entry if entry_status is not None and entry_status.live else None
        return ServiceControlStatus(spec, runtime, health, managed)

    def statuses(self) -> dict[str, ServiceControlStatus]:
        processes = self._list_processes()
        listeners = self._list_listeners()
        return {
            service_id: self.status(service_id, processes=processes, listeners=listeners)
            for service_id in self.specs
        }

    # --- start -------------------------------------------------------------

    def start(
        self,
        service_id: str,
        *,
        wait: bool = False,
        timeout: float | None = None,
        dry_run: bool | None = None,
    ) -> StartReport:
        """Start the managed process. Refuses when someone else already runs it."""
        spec = self.spec_for(service_id)
        if spec.launch is None:
            return StartReport(
                LaunchResult(
                    LaunchOutcome.SPAWN_FAILED,
                    service_id,
                    detail=f"{spec.name} has no launch spec in this milestone",
                ),
                RuntimeProcessState.UNKNOWN,
            )

        current = self.status(service_id)
        if current.runtime.state is RuntimeProcessState.RUNNING:
            if current.origin is ProcessOrigin.MANAGED:
                return StartReport(
                    LaunchResult(
                        LaunchOutcome.ALREADY_MANAGED,
                        service_id,
                        current.runtime.pid,
                        f"already managed as pid {current.runtime.pid}",
                        current.managed,
                    ),
                    RuntimeProcessState.RUNNING,
                    current.health,
                )
            return StartReport(
                LaunchResult(
                    LaunchOutcome.ALREADY_RUNNING_EXTERNAL,
                    service_id,
                    current.runtime.pid,
                    (
                        f"{spec.name} is already running as pid {current.runtime.pid}, "
                        "started outside this manager; refusing to start a second one"
                    ),
                ),
                RuntimeProcessState.RUNNING,
                current.health,
            )

        launch = self.launcher.start(spec.launch, dry_run=dry_run)
        if not launch.started:
            state = (
                RuntimeProcessState.STOPPED
                if launch.outcome is LaunchOutcome.DRY_RUN
                else RuntimeProcessState.FAILED
            )
            return StartReport(launch, state, detail=launch.detail)
        if not wait:
            return StartReport(
                launch, RuntimeProcessState.STARTING, detail="spawned; readiness not checked"
            )
        limit = spec.launch.startup_timeout if timeout is None else timeout
        state, health, detail = self._await_ready(spec, launch.pid, limit)
        return StartReport(launch, state, health, detail)

    def _check(self, spec: ServiceControlSpec, listeners: ListenerReport) -> ServiceStatus:
        return check_service(spec.service, http=self._http, tcp=self._tcp, listeners=listeners)

    def _await_ready(
        self, spec: ServiceControlSpec, pid: int | None, timeout: float
    ) -> tuple[RuntimeProcessState, ServiceStatus, str]:
        """Poll health until the service answers, the process dies, or time runs out."""
        deadline = self._monotonic() + timeout
        while True:
            # Rescan every round: a service that is starting up has not opened
            # its port yet, and a stale scan would hide the moment it does.
            listeners = self._list_listeners()
            if pid is not None and self._lookup_process(pid).outcome is LookupOutcome.NOT_FOUND:
                health = self._check(spec, listeners)
                return RuntimeProcessState.FAILED, health, f"process {pid} exited during startup"
            health = self._check(spec, listeners)
            if health.state is ServiceState.UP:
                return RuntimeProcessState.RUNNING, health, "process running and service healthy"
            if self._monotonic() >= deadline:
                return (
                    RuntimeProcessState.FAILED,
                    health,
                    f"process is running but did not become healthy within {timeout:.1f}s",
                )
            self._sleep(self._poll_interval)

    # --- stop / restart ----------------------------------------------------

    def stop(
        self,
        service_id: str,
        *,
        allow_force: bool | None = None,
        timeout: float | None = None,
        dry_run: bool | None = None,
    ) -> StopResult:
        """Stop our process. An externally started one is refused, never killed."""
        spec = self.spec_for(service_id)
        current = self.status(service_id)
        if current.managed is None and current.runtime.state is RuntimeProcessState.RUNNING:
            return StopResult(
                StopOutcome.NOT_MANAGED,
                service_id,
                current.runtime.pid,
                (
                    f"{spec.name} was started outside this manager "
                    f"(pid {current.runtime.pid}); refusing to stop it"
                ),
            )
        launch = spec.launch
        force = launch.allow_force_kill if launch is not None else False
        if allow_force is not None:
            force = allow_force
        limit = (
            (launch.stop_timeout if launch is not None else 10.0) if timeout is None else timeout
        )
        return self.launcher.stop(service_id, timeout=limit, allow_force=force, dry_run=dry_run)

    def restart(
        self,
        service_id: str,
        *,
        wait: bool = False,
        allow_force: bool | None = None,
        timeout: float | None = None,
        dry_run: bool | None = None,
    ) -> RestartReport:
        """Stop then start. No stop logic is duplicated, and neither is start."""
        stopped = self.stop(service_id, allow_force=allow_force, timeout=timeout, dry_run=dry_run)
        if not (stopped.stopped or stopped.outcome is StopOutcome.DRY_RUN):
            return RestartReport(stopped, detail=f"not restarting: {stopped.detail}")
        started = self.start(service_id, wait=wait, timeout=timeout, dry_run=dry_run)
        return RestartReport(stopped, started, detail=started.detail or started.launch.detail)


def control_spec(
    service: ServiceSpec,
    process: RuntimeProcessSpec,
    launch: ProcessLaunchSpec | None = None,
) -> ServiceControlSpec:
    return ServiceControlSpec(service, process, launch)


def registry_for(root: Path | str) -> ManagedProcessRegistry:
    """Convenience for callers holding a stack root rather than a layout."""
    from .layout import StackLayout

    return ManagedProcessRegistry.from_layout(StackLayout.from_root(root))
