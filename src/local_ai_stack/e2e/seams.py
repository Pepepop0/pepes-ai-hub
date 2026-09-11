"""Named failure injections, and the narrow seams they are allowed to use.

Injection exists only here and in the controlled providers that read it. No
production module imports this file, so a normal run has no path through which a
failure could be injected — that is enforced by a test, not by convention.

Every injection obeys four rules from the contract:

- it is **named** in the run report, so a failure is never anonymous;
- it is **deterministic** — same plan, same behaviour, no randomness;
- it acts only at a replaceable dependency seam: command execution, HTTP
  transport, the Ollama client, runtime probes and launching, managed config
  writes, the repository a run reads its profiles and workflow manifests from,
  clocks and event sinks;
- it cannot patch an ownership check, substitute an arbitrary executable,
  redirect a managed path, or touch an external provider.

The last rule is the important one. An injection makes a dependency misbehave;
it never makes the hub lie about who owns a process.

There is a fifth rule, learned the hard way: **an injection that nothing
consumes must not exist.** A port already owned by somebody else, or a machine
without CUDA, is not a dependency misbehaving -- it is what the world *is*, and
a scenario says so by building that world (``controlled_world(cuda=False)``, an
occupied listener in the inventory). Declaring an injection for it put a name
in the report that no seam ever read, so running with and without it produced
identical behaviour. Every member below is consumed by a seam, and a
parametrised test proves each one is observable there.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class Seam(StrEnum):
    """Where an injection is permitted to act."""

    COMMAND = "command"
    HTTP_TRANSPORT = "http-transport"
    OLLAMA_CLIENT = "ollama-client"
    RUNTIME_PROBE = "runtime-probe"
    PROCESS_LAUNCH = "process-launch"
    CONFIG_WRITE = "config-write"
    WORKFLOW_SOURCE = "workflow-source"
    MODEL_STORE = "model-store"
    CLOCK = "clock"
    EVENT_SINK = "event-sink"


class Injection(StrEnum):
    """The failures the P0 matrix requires us to be able to cause."""

    DOWNLOAD_INTERRUPTED = "download-interrupted"
    CHECKSUM_MISMATCH = "checksum-mismatch"
    CONFIG_TAMPERED = "config-tampered"
    INVALID_WORKFLOW = "invalid-workflow"
    READINESS_TIMEOUT = "readiness-timeout"
    SERVICE_CRASH = "service-crash"
    MODEL_MISSING = "model-missing"
    MODEL_TAMPERED = "model-tampered"
    STOP_FAILURE = "stop-failure"
    CLEANUP_FAILURE = "cleanup-failure"


# Which seam each injection is allowed to act through. An injection that tried
# to reach any other dependency would be a contract violation, so the mapping is
# explicit rather than implied by the implementation.
SEAM_FOR: Mapping[Injection, Seam] = MappingProxyType(
    {
        Injection.DOWNLOAD_INTERRUPTED: Seam.HTTP_TRANSPORT,
        Injection.CHECKSUM_MISMATCH: Seam.HTTP_TRANSPORT,
        Injection.CONFIG_TAMPERED: Seam.CONFIG_WRITE,
        Injection.INVALID_WORKFLOW: Seam.WORKFLOW_SOURCE,
        Injection.READINESS_TIMEOUT: Seam.RUNTIME_PROBE,
        Injection.SERVICE_CRASH: Seam.PROCESS_LAUNCH,
        Injection.MODEL_MISSING: Seam.OLLAMA_CLIENT,
        Injection.MODEL_TAMPERED: Seam.MODEL_STORE,
        Injection.STOP_FAILURE: Seam.PROCESS_LAUNCH,
        Injection.CLEANUP_FAILURE: Seam.PROCESS_LAUNCH,
    }
)


@dataclass(frozen=True)
class InjectionPlan:
    """The injections active for one scenario, named for the report."""

    injections: frozenset[Injection] = frozenset()
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "injections", frozenset(self.injections))
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    @classmethod
    def of(cls, *injections: Injection, **detail: object) -> InjectionPlan:
        return cls(injections=frozenset(injections), detail=detail)

    @classmethod
    def none(cls) -> InjectionPlan:
        return cls()

    def __contains__(self, injection: object) -> bool:
        return injection in self.injections

    def __bool__(self) -> bool:
        return bool(self.injections)

    def active(self, injection: Injection) -> bool:
        return injection in self.injections

    @property
    def names(self) -> tuple[str, ...]:
        """Stable, sorted names. A report must not depend on set ordering."""
        return tuple(sorted(str(item) for item in self.injections))

    @property
    def seams(self) -> tuple[str, ...]:
        return tuple(sorted({str(SEAM_FOR[item]) for item in self.injections}))

    def as_report_data(self) -> dict[str, object]:
        return {
            "injections": list(self.names),
            "seams": list(self.seams),
            "detail": dict(self.detail),
        }

    def with_(self, *injections: Injection) -> InjectionPlan:
        return InjectionPlan(injections=self.injections | frozenset(injections), detail=self.detail)


def describe(injections: Iterable[Injection]) -> str:
    names = sorted(str(item) for item in injections)
    return ", ".join(names) if names else "none"


__all__ = [
    "SEAM_FOR",
    "Injection",
    "InjectionPlan",
    "Seam",
    "describe",
]
