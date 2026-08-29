"""Port-level health helpers.

Kept for backwards compatibility with callers written before the service
layer existed. New code should use :mod:`local_ai_stack.services`, which
prefers real HTTP health endpoints and reports UP/DOWN/DEGRADED/UNKNOWN.
"""

from __future__ import annotations

from dataclasses import dataclass

from .probes import ProbeOutcome, tcp_probe
from .services import DEFAULT_SERVICE_SPECS

LEGACY_TIMEOUT = 0.35


@dataclass(frozen=True)
class PortHealth:
    name: str
    host: str
    port: int
    reachable: bool


DEFAULT_SERVICES = tuple((s.name, s.host, s.port) for s in DEFAULT_SERVICE_SPECS)


def check_port(name: str, host: str, port: int, timeout: float = LEGACY_TIMEOUT) -> PortHealth:
    result = tcp_probe(host, port, timeout)
    return PortHealth(name, host, port, result.outcome is ProbeOutcome.OK)


def check_default_services() -> list[PortHealth]:
    return [check_port(*s) for s in DEFAULT_SERVICES]
