"""Read models for consumers and the bounded single-GPU resource seam.

Only projections cross this boundary. Existing ServiceStatus, SystemFacts and
runtime objects stay in their domains, since they include transport/process data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from .operation_contracts import (
    OperationEventSink,
    OperationSnapshot,
    _identifier,
    _integer,
    _timestamp,
)


@dataclass(frozen=True)
class VramSnapshot:
    """One selected GPU, in MiB. Unknown capacity/free memory stays unknown."""

    observed_at: datetime
    total_mib: int | None
    free_mib: int | None

    def __post_init__(self) -> None:
        _timestamp(self.observed_at)
        if self.total_mib is not None:
            _integer(self.total_mib)
        if self.free_mib is not None:
            _integer(self.free_mib)
            if self.total_mib is None or self.free_mib > self.total_mib:
                raise ValueError("free VRAM requires a known, sufficient total")

    @property
    def used_mib(self) -> int | None:
        if self.total_mib is None or self.free_mib is None:
            return None
        return self.total_mib - self.free_mib

    def can_fit(self, required_mib: int) -> bool:
        """An observation, not a reservation or a guarantee against later OOM."""
        _integer(required_mib, minimum=1)
        return self.free_mib is not None and self.free_mib >= required_mib


@runtime_checkable
class ResourceCoordinator(Protocol):
    """No multi-GPU scheduling. All timeout values must be positive and finite.

    Release requests the conflicting application workload to unload, not service
    stop/process termination. True means acknowledged, not that VRAM is free.
    wait_for_available returns the last observation by the deadline, including
    insufficient/unknown VRAM; the caller checks can_fit and proceeds or fails.
    """

    def snapshot(self) -> VramSnapshot: ...

    def release_conflicting_workload(self, *, timeout_seconds: float) -> bool: ...

    def wait_for_available(self, required_mib: int, *, timeout_seconds: float) -> VramSnapshot: ...


@dataclass(frozen=True)
class ServiceView:
    """Health uses existing services.ServiceState.value vocabulary, not lifecycle."""

    service_id: str
    health: Literal["UP", "DOWN", "DEGRADED", "UNKNOWN"]

    def __post_init__(self) -> None:
        _identifier(self.service_id)
        if self.health not in ("UP", "DOWN", "DEGRADED", "UNKNOWN"):
            raise ValueError("unknown service health")


@dataclass(frozen=True)
class ServicesSnapshot:
    observed_at: datetime
    services: tuple[ServiceView, ...]

    def __post_init__(self) -> None:
        _timestamp(self.observed_at)
        object.__setattr__(self, "services", tuple(self.services))
        if any(not isinstance(service, ServiceView) for service in self.services):
            raise ValueError("services must be projections")
        if len({service.service_id for service in self.services}) != len(self.services):
            raise ValueError("duplicate service id")


class OperationSubscription(Protocol):
    """close is idempotent; after it returns there are no further callbacks."""

    def close(self) -> None: ...


@runtime_checkable
class ApplicationReadModel(Protocol):
    """Independent fakeable views; no concrete image coordinator is required.

    subscribe atomically replays the current operation's latest event, if any,
    before future events. Delivery is serialized in producer order. Consumers
    marshal callbacks to their UI thread and must not block or raise. The source
    isolates subscriber errors; no observer can change the operation outcome.
    """

    def current_operation(self) -> OperationSnapshot | None: ...

    def subscribe(self, sink: OperationEventSink) -> OperationSubscription: ...

    def services_snapshot(self) -> ServicesSnapshot: ...

    def vram_snapshot(self) -> VramSnapshot: ...
