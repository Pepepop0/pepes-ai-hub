"""Service specs, health checks and aggregated runtime status.

This is the runtime-manager health layer: it declares *what* a service is
(:class:`ServiceSpec`), decides *how healthy* it is right now
(:class:`ServiceStatus`) and rolls several services into a single
:class:`StackHealth`. It never installs, starts or stops anything.

Health strategy, per SPEC:

* if the local listener scan is trustworthy and nothing is listening on the
  service port, the service is DOWN — no request is sent at all;
* otherwise prefer a real HTTP health endpoint over "the port is open";
* fall back to a plain TCP probe only when no trustworthy endpoint exists,
  or when the HTTP request fails for a reason other than refusal/timeout.

The listener pre-check exists because a host may silently drop packets to a
closed loopback port instead of refusing them, which would otherwise report a
plainly stopped service as UNKNOWN after a full timeout. It does not change
what a timeout means: a real timeout is still UNKNOWN.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum

from .inventory import ListenerReport, list_listeners
from .probes import DEFAULT_TIMEOUT, ProbeOutcome, ProbeResult, http_probe, tcp_probe

HttpProbe = Callable[[str, float], ProbeResult]
TcpProbe = Callable[[str, int, float], ProbeResult]


class ServiceState(str, Enum):
    """Aggregated health verdict for one service or for the whole stack."""

    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ServiceSpec:
    """Declarative description of a service the runtime manager watches."""

    id: str
    name: str
    port: int
    host: str = "127.0.0.1"
    scheme: str = "http"
    health_path: str | None = None
    expected_status: tuple[int, ...] = (200,)
    expected_json_keys: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def endpoint(self) -> str | None:
        """Full health URL, or ``None`` when only a port probe is possible."""
        if self.health_path is None:
            return None
        return f"{self.scheme}://{self.address}{self.health_path}"


@dataclass(frozen=True)
class ServiceStatus:
    """Result of one health check."""

    spec: ServiceSpec
    state: ServiceState
    detail: str = ""
    checked_via: str = "tcp"
    latency_ms: float | None = None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def healthy(self) -> bool:
        return self.state is ServiceState.UP


@dataclass(frozen=True)
class StackHealth:
    """Every watched service plus the aggregated verdict."""

    services: tuple[ServiceStatus, ...]
    state: ServiceState

    def by_id(self, service_id: str) -> ServiceStatus | None:
        return next((s for s in self.services if s.id == service_id), None)


# /api/version is the cheapest documented Ollama endpoint and always returns
# {"version": "..."} — a 200 on "/" only proves the HTTP server is up.
OLLAMA_SPEC = ServiceSpec(
    id="ollama",
    name="Ollama",
    port=11434,
    health_path="/api/version",
    expected_json_keys=("version",),
)

# Open WebUI exposes /health, which answers {"status": true} without auth.
OPEN_WEBUI_SPEC = ServiceSpec(
    id="open-webui",
    name="Open WebUI",
    port=8080,
    health_path="/health",
    expected_json_keys=("status",),
)

# ComfyUI has no /health; /system_stats is the stable JSON probe and proves the
# backend answers, not just the web server.
COMFYUI_SPEC = ServiceSpec(
    id="comfyui",
    name="ComfyUI",
    port=8188,
    health_path="/system_stats",
    expected_json_keys=("system",),
)

DEFAULT_SERVICE_SPECS: tuple[ServiceSpec, ...] = (OLLAMA_SPEC, OPEN_WEBUI_SPEC, COMFYUI_SPEC)


def spec_by_id(
    service_id: str, specs: Sequence[ServiceSpec] = DEFAULT_SERVICE_SPECS
) -> ServiceSpec:
    for spec in specs:
        if spec.id == service_id:
            return spec
    known = ", ".join(s.id for s in specs)
    raise KeyError(f"unknown service id {service_id!r} (known: {known})")


def _validate_http(spec: ServiceSpec, result: ProbeResult) -> tuple[ServiceState, str]:
    """Turn a successful HTTP round-trip into UP or DEGRADED."""
    path = spec.health_path
    if result.status_code not in spec.expected_status:
        return ServiceState.DEGRADED, f"unexpected HTTP {result.status_code} on {path}"
    if not spec.expected_json_keys:
        return ServiceState.UP, f"HTTP {result.status_code} on {path}"
    try:
        payload = json.loads(result.body)
    except ValueError:
        return ServiceState.DEGRADED, f"{path} did not return JSON"
    if not isinstance(payload, dict):
        return ServiceState.DEGRADED, f"{path} returned JSON but not an object"
    missing = [key for key in spec.expected_json_keys if key not in payload]
    if missing:
        return ServiceState.DEGRADED, f"{path} missing key(s): {', '.join(missing)}"
    return ServiceState.UP, f"HTTP {result.status_code} on {path}"


def _tcp_status(
    spec: ServiceSpec, result: ProbeResult, *, fallback: bool, reason: str = ""
) -> ServiceStatus:
    """Turn a TCP probe into a status, as primary check or as HTTP fallback."""
    if result.outcome is ProbeOutcome.OK:
        if fallback:
            state = ServiceState.DEGRADED
            detail = f"port {spec.port} open but health endpoint unreachable"
        else:
            state = ServiceState.UP
            detail = f"port {spec.port} open"
    elif result.outcome is ProbeOutcome.REFUSED:
        state, detail = ServiceState.DOWN, "connection refused"
    elif result.outcome is ProbeOutcome.TIMEOUT:
        state, detail = ServiceState.UNKNOWN, result.detail
    else:
        state, detail = ServiceState.UNKNOWN, result.detail or "unreachable"
    if fallback and reason:
        detail = f"{reason}; port fallback: {detail}"
    return ServiceStatus(spec, state, detail, "tcp", result.latency_ms)


def check_service(
    spec: ServiceSpec,
    *,
    timeout: float | None = None,
    http: HttpProbe = http_probe,
    tcp: TcpProbe = tcp_probe,
    listeners: ListenerReport | None = None,
) -> ServiceStatus:
    """Check one service, preferring its HTTP health endpoint.

    ``listeners`` lets a caller reuse a single local scan across services; when
    omitted a fresh scan is taken. A report flagged unavailable simply skips the
    pre-check.
    """
    report = list_listeners() if listeners is None else listeners
    if report.usable and not report.on_port(spec.port, spec.host):
        return ServiceStatus(
            spec,
            ServiceState.DOWN,
            f"no local process listening on {spec.address}",
            "listener",
        )

    effective_timeout = spec.timeout if timeout is None else timeout
    endpoint = spec.endpoint
    if endpoint is None:
        return _tcp_status(spec, tcp(spec.host, spec.port, effective_timeout), fallback=False)

    result = http(endpoint, effective_timeout)
    if result.outcome is ProbeOutcome.OK:
        state, detail = _validate_http(spec, result)
        return ServiceStatus(spec, state, detail, "http", result.latency_ms)
    if result.outcome is ProbeOutcome.REFUSED:
        return ServiceStatus(
            spec, ServiceState.DOWN, "connection refused", "http", result.latency_ms
        )
    if result.outcome is ProbeOutcome.TIMEOUT:
        return ServiceStatus(spec, ServiceState.UNKNOWN, result.detail, "http", result.latency_ms)
    return _tcp_status(
        spec,
        tcp(spec.host, spec.port, effective_timeout),
        fallback=True,
        reason=result.detail or "http probe failed",
    )


def check_services(
    specs: Sequence[ServiceSpec] = DEFAULT_SERVICE_SPECS,
    *,
    timeout: float | None = None,
    http: HttpProbe = http_probe,
    tcp: TcpProbe = tcp_probe,
    listeners: ListenerReport | None = None,
) -> tuple[ServiceStatus, ...]:
    report = list_listeners() if listeners is None else listeners
    return tuple(
        check_service(s, timeout=timeout, http=http, tcp=tcp, listeners=report) for s in specs
    )


def aggregate_state(statuses: Iterable[ServiceStatus]) -> ServiceState:
    """Collapse several service statuses into one stack-level verdict."""
    return aggregate_states(status.state for status in statuses)


def aggregate_states(states: Iterable[ServiceState]) -> ServiceState:
    """Collapse bare states, for callers holding something other than a status."""
    states = list(states)
    if not states:
        return ServiceState.UNKNOWN
    unique = set(states)
    if len(unique) == 1:
        return states[0]
    if ServiceState.UP in unique or ServiceState.DEGRADED in unique:
        return ServiceState.DEGRADED
    return ServiceState.UNKNOWN


def check_stack(
    specs: Sequence[ServiceSpec] = DEFAULT_SERVICE_SPECS,
    *,
    timeout: float | None = None,
    http: HttpProbe = http_probe,
    tcp: TcpProbe = tcp_probe,
    listeners: ListenerReport | None = None,
) -> StackHealth:
    statuses = check_services(specs, timeout=timeout, http=http, tcp=tcp, listeners=listeners)
    return StackHealth(statuses, aggregate_state(statuses))
