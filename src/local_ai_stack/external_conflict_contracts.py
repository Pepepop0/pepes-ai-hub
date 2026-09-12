"""Frozen application contract for external runtime conflict resolution.

These values describe a preflight observation and a user's policy choice. They
do not stop a process, allocate a port, resolve an installation root or grant
authority over any PID, executable, path, process name, endpoint or shell text.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .components import ComponentId
from .operation_contracts import OperationState


def _identifier(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(not (char.isalnum() or char in "_-") for char in value)
    ):
        raise ValueError(f"{field} must be an opaque identifier")


def _message(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 240
        or not value.isprintable()
    ):
        raise ValueError(f"{field} must be printable, non-empty and at most 240 characters")


class ExternalConflictPolicy(StrEnum):
    """One policy for every conflict in the authoritative preflight snapshot."""

    INSTALL_ALONGSIDE = "install-alongside"
    STOP_CONFLICTING_EXTERNAL = "stop-conflicting-external"
    ABORT = "abort"


class ExternalConflictKind(StrEnum):
    ENDPOINT_OCCUPIED = "endpoint-occupied"


class ExternalConflictOrigin(StrEnum):
    EXTERNAL = "external"


class ConflictPreflightDisposition(StrEnum):
    READY = "ready"
    RESOLUTION_REQUIRED = "resolution-required"
    BLOCKED = "blocked"


class ConflictResolutionStatus(StrEnum):
    PROCEED = "proceed"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class ConflictEndpoint:
    """Read-only display projection produced by preflight, never command input."""

    host: str
    port: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not self.host.strip()
            or len(self.host) > 128
            or any(char.isspace() or not char.isprintable() for char in self.host)
        ):
            raise ValueError(
                "endpoint host must be printable, non-empty and at most 128 characters"
            )
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("endpoint port must be an integer from 1 to 65535")

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class ExternalServiceConflict:
    """One known service observed occupying a resource needed by this attempt.

    ``managed_endpoint`` is the already-configured isolated endpoint available
    to INSTALL_ALONGSIDE. It is not selected by the UI and is never allocated by
    this contract. Private process identity evidence stays in the application.
    """

    service_id: ComponentId
    kind: ExternalConflictKind
    origin: ExternalConflictOrigin
    endpoint: ConflictEndpoint
    reason: str
    can_stop_safely: bool
    managed_endpoint: ConflictEndpoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.service_id, ComponentId):
            raise TypeError("conflict service_id must be a known ComponentId")
        if not isinstance(self.kind, ExternalConflictKind):
            raise TypeError("conflict kind must be typed")
        if self.origin is not ExternalConflictOrigin.EXTERNAL:
            raise ValueError("only externally-owned runtime conflicts belong in this contract")
        if not isinstance(self.endpoint, ConflictEndpoint):
            raise TypeError("conflict endpoint must be a read-only projection")
        _message(self.reason, "conflict reason")
        if not isinstance(self.can_stop_safely, bool):
            raise TypeError("can_stop_safely must be a boolean")
        if self.managed_endpoint is not None:
            if not isinstance(self.managed_endpoint, ConflictEndpoint):
                raise TypeError("managed endpoint must be a read-only projection")
            if self.managed_endpoint == self.endpoint:
                raise ValueError("alongside requires a distinct managed endpoint")

    @property
    def can_install_alongside(self) -> bool:
        return self.managed_endpoint is not None


@dataclass(frozen=True)
class ConflictHardBlocker:
    code: str
    message: str

    def __post_init__(self) -> None:
        _identifier(self.code, "blocker code")
        _message(self.message, "blocker message")


@dataclass(frozen=True)
class ConflictPolicySelection:
    """The entire UI command: choose a policy for one preflight snapshot."""

    preflight_id: str
    policy: ExternalConflictPolicy

    def __post_init__(self) -> None:
        _identifier(self.preflight_id, "preflight_id")
        if not isinstance(self.policy, ExternalConflictPolicy):
            raise TypeError("policy must be an ExternalConflictPolicy")


@dataclass(frozen=True)
class ConflictPreflightReport:
    """Hard blockers and resolvable external conflicts are separate channels."""

    preflight_id: str
    conflicts: tuple[ExternalServiceConflict, ...] = ()
    hard_blockers: tuple[ConflictHardBlocker, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.preflight_id, "preflight_id")
        if isinstance(self.conflicts, (str, bytes)) or isinstance(self.hard_blockers, (str, bytes)):
            raise TypeError("conflicts and hard blockers must be collections")
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "hard_blockers", tuple(self.hard_blockers))
        if any(not isinstance(item, ExternalServiceConflict) for item in self.conflicts):
            raise TypeError("conflicts must contain ExternalServiceConflict values")
        if any(not isinstance(item, ConflictHardBlocker) for item in self.hard_blockers):
            raise TypeError("hard_blockers must contain ConflictHardBlocker values")
        service_ids = [item.service_id for item in self.conflicts]
        if len(service_ids) != len(set(service_ids)):
            raise ValueError("a preflight report cannot repeat a service conflict")
        managed = [item.managed_endpoint for item in self.conflicts if item.managed_endpoint]
        if len(managed) != len(set(managed)):
            raise ValueError("alongside managed endpoints must be unique")
        if self.conflicts and not self.hard_blockers and len(self.available_policies) == 1:
            raise ValueError("non-resolvable external conflicts require a hard blocker")

    @property
    def disposition(self) -> ConflictPreflightDisposition:
        if self.hard_blockers:
            return ConflictPreflightDisposition.BLOCKED
        if self.conflicts:
            return ConflictPreflightDisposition.RESOLUTION_REQUIRED
        return ConflictPreflightDisposition.READY

    @property
    def available_policies(self) -> tuple[ExternalConflictPolicy, ...]:
        if self.hard_blockers:
            return (ExternalConflictPolicy.ABORT,)
        if not self.conflicts:
            return ()
        policies: list[ExternalConflictPolicy] = []
        if all(conflict.can_install_alongside for conflict in self.conflicts):
            policies.append(ExternalConflictPolicy.INSTALL_ALONGSIDE)
        if all(conflict.can_stop_safely for conflict in self.conflicts):
            policies.append(ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL)
        policies.append(ExternalConflictPolicy.ABORT)
        return tuple(policies)

    @property
    def recommended_policy(self) -> ExternalConflictPolicy | None:
        if ExternalConflictPolicy.INSTALL_ALONGSIDE in self.available_policies:
            return ExternalConflictPolicy.INSTALL_ALONGSIDE
        if self.conflicts or self.hard_blockers:
            return ExternalConflictPolicy.ABORT
        return None

    def validate_selection(self, selection: ConflictPolicySelection) -> None:
        """Validate against this immutable observation without performing work."""
        if not isinstance(selection, ConflictPolicySelection):
            raise TypeError("selection must be a ConflictPolicySelection")
        if selection.preflight_id != self.preflight_id:
            raise ValueError("selection refers to a stale or different preflight")
        if selection.policy not in self.available_policies:
            raise ValueError("selected policy is unavailable for this preflight")


@dataclass(frozen=True)
class ConflictResolutionResult:
    preflight_id: str
    policy: ExternalConflictPolicy
    status: ConflictResolutionStatus
    message: str

    def __post_init__(self) -> None:
        _identifier(self.preflight_id, "preflight_id")
        if not isinstance(self.policy, ExternalConflictPolicy):
            raise TypeError("policy must be an ExternalConflictPolicy")
        if not isinstance(self.status, ConflictResolutionStatus):
            raise TypeError("status must be a ConflictResolutionStatus")
        _message(self.message, "resolution message")
        if (self.policy is ExternalConflictPolicy.ABORT) != (
            self.status is ConflictResolutionStatus.ABORTED
        ):
            raise ValueError("ABORT and ABORTED must occur together")


@runtime_checkable
class ExternalConflictResolver(Protocol):
    """Application authority over its current, privately-evidenced preflight.

    Implementations reject stale/used preflight IDs and unavailable policies.
    STOP revalidates private process identity immediately before a bounded stop.
    The method accepts no process or endpoint authority from a consumer.
    """

    def current_conflict_preflight(self) -> ConflictPreflightReport | None: ...

    def resolve_external_conflicts(
        self, selection: ConflictPolicySelection
    ) -> ConflictResolutionResult: ...


def installation_outcome_heading(state: OperationState) -> str:
    """Frozen terminal heading: success and failure can never share a title."""
    if state is OperationState.SUCCEEDED:
        return "Concluído"
    if state is OperationState.FAILED:
        return "Instalação não concluída"
    raise ValueError("an installation heading requires a terminal operation state")


__all__ = [
    "ConflictEndpoint",
    "ConflictHardBlocker",
    "ConflictPolicySelection",
    "ConflictPreflightDisposition",
    "ConflictPreflightReport",
    "ConflictResolutionResult",
    "ConflictResolutionStatus",
    "ExternalConflictKind",
    "ExternalConflictOrigin",
    "ExternalConflictPolicy",
    "ExternalConflictResolver",
    "ExternalServiceConflict",
    "installation_outcome_heading",
]
