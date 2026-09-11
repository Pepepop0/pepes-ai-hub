"""Frozen application operation values; no provider, transport or UI dependency.

The application validates each event with ``advance_operation`` before publishing
it. This is an in-memory reducer, not a coordinator, event broker or scheduler.
See docs/OPERATION_CONTRACT.md for producer and consumer obligations.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


def _identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
    ):
        raise ValueError("expected an opaque identifier, at most 128 characters")


def _message(value: str) -> None:
    # Shape validation is not secret redaction. Producers supply safe summaries.
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 240
        or not value.isprintable()
    ):
        raise ValueError("message must be printable, non-empty and at most 240 characters")


def _integer(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"expected an integer >= {minimum}")


def _timestamp(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be a timezone-aware UTC datetime")


class OperationState(StrEnum):
    PREPARING = "preparing"
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in (OperationState.SUCCEEDED, OperationState.FAILED)


class OperationPhase(StrEnum):
    PREPARATION = "preparation"
    RESOURCE_RELEASE = "resource-release"
    SERVICE_START = "service-start"
    SUBMISSION = "submission"
    EXECUTION = "execution"
    FINALIZATION = "finalization"


class OperationEventKind(StrEnum):
    STATE = "state"
    PROGRESS = "progress"
    RESULT = "result"


@dataclass(frozen=True)
class OperationProgress:
    """Work units within one phase. Unknown total means no fraction/percentage."""

    completed: int
    total: int | None = None

    def __post_init__(self) -> None:
        _integer(self.completed)
        if self.total is not None:
            _integer(self.total, minimum=1)
            if self.completed > self.total:
                raise ValueError("completed exceeds total")

    @property
    def fraction(self) -> float | None:
        return None if self.total is None else self.completed / self.total


@dataclass(frozen=True)
class OperationDiagnostic:
    code: str
    message: str
    reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.code)
        _message(self.message)
        if self.reference is not None:
            _identifier(self.reference)


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    state: OperationState
    output_references: tuple[str, ...] = ()
    diagnostics: tuple[OperationDiagnostic, ...] = ()
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        if not isinstance(self.state, OperationState) or not self.state.terminal:
            raise ValueError("a result requires SUCCEEDED or FAILED")
        if isinstance(self.output_references, (str, bytes)):
            raise TypeError("output references must be a collection of identifiers")
        object.__setattr__(self, "output_references", tuple(self.output_references))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        for reference in self.output_references:
            _identifier(reference)
        if len(self.diagnostics) > 8 or any(
            not isinstance(item, OperationDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("at most eight typed diagnostics are allowed")
        if self.state is OperationState.FAILED and not self.diagnostics:
            raise ValueError("failure requires a diagnostic")
        if self.duration_seconds is not None and (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
        ):
            raise ValueError("duration must be finite and non-negative")


@dataclass(frozen=True)
class OperationEvent:
    operation_id: str
    sequence: int
    kind: OperationEventKind
    state: OperationState
    timestamp: datetime
    phase: OperationPhase
    message: str
    progress: OperationProgress | None = None
    diagnostic: OperationDiagnostic | None = None
    result: OperationResult | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id)
        _integer(self.sequence, minimum=1)
        _timestamp(self.timestamp)
        _message(self.message)
        for value, enum in (
            (self.kind, OperationEventKind),
            (self.state, OperationState),
            (self.phase, OperationPhase),
        ):
            if not isinstance(value, enum):
                raise TypeError("event kind, state and phase must be typed enums")
        if self.progress is not None and not isinstance(self.progress, OperationProgress):
            raise ValueError("invalid progress")
        if self.diagnostic is not None and not isinstance(self.diagnostic, OperationDiagnostic):
            raise ValueError("invalid diagnostic")
        if self.kind is OperationEventKind.PROGRESS and self.progress is None:
            raise ValueError("a progress event requires progress")
        terminal_event = self.kind is OperationEventKind.RESULT
        if terminal_event != self.state.terminal or terminal_event != (self.result is not None):
            raise ValueError("terminal state, RESULT kind and result must occur together")
        if self.result is not None and (
            not isinstance(self.result, OperationResult)
            or self.result.operation_id != self.operation_id
            or self.result.state is not self.state
        ):
            raise ValueError("event and result identity/state disagree")


OperationEventSink = Callable[[OperationEvent], None]


@dataclass(frozen=True)
class OperationSnapshot:
    """The latest full observation, retained after completion. None means idle."""

    started_at: datetime
    last_event: OperationEvent

    def __post_init__(self) -> None:
        _timestamp(self.started_at)
        if not isinstance(self.last_event, OperationEvent):
            raise TypeError("snapshot requires an operation event")


def advance_operation(
    current: OperationSnapshot | None, event: OperationEvent
) -> OperationSnapshot:
    """Validate one serialized stream; reject gaps, regression and post-terminal events.

    Sequence is authoritative even if the wall clock goes backwards. Lifecycle
    stages may be skipped when unobserved, but never regress. Phase-local work
    counts need not be monotonic across events; there is no overall percentage.
    """
    if current is None:
        if (
            event.sequence != 1
            or event.state is not OperationState.PREPARING
            or event.kind is not OperationEventKind.STATE
            or event.phase is not OperationPhase.PREPARATION
        ):
            raise ValueError("first event must be sequence 1, STATE/PREPARING/PREPARATION")
        return OperationSnapshot(event.timestamp, event)
    previous = current.last_event
    if previous.state.terminal:
        raise ValueError("operation already terminal")
    if event.operation_id != previous.operation_id or event.sequence != previous.sequence + 1:
        raise ValueError("operation identity or sequence mismatch")
    states = tuple(OperationState)
    if states.index(event.state) < states.index(previous.state):
        raise ValueError("operation state cannot regress")
    if event.kind is OperationEventKind.PROGRESS and (
        event.state is not previous.state or event.phase is not previous.phase
    ):
        raise ValueError("progress cannot change state or phase")
    return OperationSnapshot(current.started_at, event)
