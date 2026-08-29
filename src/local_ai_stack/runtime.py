"""Aggregated runtime status: process state and health, side by side.

Process state answers "is something running?"; health answers "is it actually
serving?". They stay separate on purpose — a ComfyUI process that is still
loading models is RUNNING but not UP — and are only combined into a single
convenience verdict by :func:`combine_state`.

This module reads. Start/stop/restart belong to a later step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .inventory import ListenerReport, ProcessReport, list_listeners, list_processes
from .probes import http_probe, tcp_probe
from .processes import (
    DEFAULT_PROCESS_SPECS,
    RuntimeProcessSpec,
    RuntimeProcessState,
    RuntimeProcessStatus,
    discover_process,
)
from .services import (
    DEFAULT_SERVICE_SPECS,
    HttpProbe,
    ServiceSpec,
    ServiceState,
    ServiceStatus,
    StackHealth,
    TcpProbe,
    aggregate_state,
    aggregate_states,
    check_service,
)


def combine_state(health: ServiceState, process: RuntimeProcessState) -> ServiceState:
    """Single verdict for a service, without losing what each layer saw."""
    if health is ServiceState.UP:
        return ServiceState.UP
    if health is ServiceState.DEGRADED:
        return ServiceState.DEGRADED
    # A live process that does not serve is worse than healthy and better than
    # absent: report it as DEGRADED so the user knows something is there.
    if process is RuntimeProcessState.RUNNING:
        return ServiceState.DEGRADED
    if health is ServiceState.DOWN:
        return ServiceState.DOWN
    return ServiceState.UNKNOWN


@dataclass(frozen=True)
class ServiceRuntime:
    """One service seen from both angles."""

    health: ServiceStatus
    process: RuntimeProcessStatus
    state: ServiceState

    @property
    def id(self) -> str:
        return self.health.spec.id

    @property
    def name(self) -> str:
        return self.health.spec.name

    @property
    def spec(self) -> ServiceSpec:
        return self.health.spec


@dataclass(frozen=True)
class StackRuntime:
    services: tuple[ServiceRuntime, ...]
    state: ServiceState

    def by_id(self, service_id: str) -> ServiceRuntime | None:
        return next((s for s in self.services if s.id == service_id), None)

    @property
    def health(self) -> StackHealth:
        """The health-only view, for callers that predate the runtime layer."""
        statuses = tuple(s.health for s in self.services)
        return StackHealth(statuses, aggregate_state(statuses))


def _process_spec_for(
    service_id: str, specs: Sequence[RuntimeProcessSpec]
) -> RuntimeProcessSpec | None:
    return next((s for s in specs if s.service_id == service_id), None)


def service_runtime(
    spec: ServiceSpec,
    process_spec: RuntimeProcessSpec | None,
    *,
    timeout: float | None = None,
    http: HttpProbe = http_probe,
    tcp: TcpProbe = tcp_probe,
    listeners: ListenerReport | None = None,
    processes: ProcessReport | None = None,
    managed_pids: frozenset[int] = frozenset(),
) -> ServiceRuntime:
    listener_report = list_listeners() if listeners is None else listeners
    process_report = list_processes() if processes is None else processes
    health = check_service(spec, timeout=timeout, http=http, tcp=tcp, listeners=listener_report)
    if process_spec is None:
        process_spec = RuntimeProcessSpec(spec.id, spec.name, port=spec.port)
    process = discover_process(
        process_spec,
        processes=process_report,
        listeners=listener_report,
        managed_pids=managed_pids,
    )
    return ServiceRuntime(health, process, combine_state(health.state, process.state))


def stack_runtime(
    specs: Sequence[ServiceSpec] = DEFAULT_SERVICE_SPECS,
    process_specs: Sequence[RuntimeProcessSpec] = DEFAULT_PROCESS_SPECS,
    *,
    timeout: float | None = None,
    http: HttpProbe = http_probe,
    tcp: TcpProbe = tcp_probe,
    listeners: ListenerReport | None = None,
    processes: ProcessReport | None = None,
    managed_pids: frozenset[int] = frozenset(),
) -> StackRuntime:
    """One listener scan plus one process scan, reused across every service."""
    listener_report = list_listeners() if listeners is None else listeners
    process_report = list_processes() if processes is None else processes
    services = tuple(
        service_runtime(
            spec,
            _process_spec_for(spec.id, process_specs),
            timeout=timeout,
            http=http,
            tcp=tcp,
            listeners=listener_report,
            processes=process_report,
            managed_pids=managed_pids,
        )
        for spec in specs
    )
    return StackRuntime(services, aggregate_states(s.state for s in services))
