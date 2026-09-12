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
    ACTIVITY = "activity"
    DETAIL = "detail"
    RESULT = "result"


class OperationActivityCategory(StrEnum):
    PREPARATION = "preparation"
    TRANSFER = "transfer"
    ENVIRONMENT = "environment"
    DEPENDENCY = "dependency"
    CONFIGURATION = "configuration"
    SERVICE = "service"
    READINESS = "readiness"
    MODEL = "model"
    WORKFLOW = "workflow"


@dataclass(frozen=True)
class OperationActivity:
    """Sanitized semantic activity emitted by a domain-specific observer."""

    category: OperationActivityCategory
    action: str
    item: str
    detail: str | None = None
    elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, OperationActivityCategory):
            raise TypeError("activity category must be a typed enum")
        _identifier(self.action)
        _message(self.item)
        if self.detail is not None:
            _message(self.detail)
        if self.elapsed_seconds is not None and (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed time must be finite and non-negative")


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
class TransferProgress:
    """Observed transfer bytes. Unknown total means no fraction or ETA."""

    completed_bytes: int
    total_bytes: int | None = None
    bytes_per_second: float | None = None

    def __post_init__(self) -> None:
        _integer(self.completed_bytes)
        if self.total_bytes is not None:
            _integer(self.total_bytes, minimum=1)
            if self.completed_bytes > self.total_bytes:
                raise ValueError("completed bytes exceed total bytes")
        if self.bytes_per_second is not None and (
            isinstance(self.bytes_per_second, bool)
            or not isinstance(self.bytes_per_second, (int, float))
            or not math.isfinite(self.bytes_per_second)
            or self.bytes_per_second < 0
        ):
            raise ValueError("transfer speed must be finite and non-negative")

    @property
    def fraction(self) -> float | None:
        return None if self.total_bytes is None else self.completed_bytes / self.total_bytes

    @property
    def eta_seconds(self) -> float | None:
        if self.total_bytes is None or not self.bytes_per_second:
            return None
        return (self.total_bytes - self.completed_bytes) / self.bytes_per_second


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
    transfer: TransferProgress | None = None
    activity: OperationActivity | None = None
    detail: OperationActivity | None = None

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
        if self.transfer is not None and not isinstance(self.transfer, TransferProgress):
            raise ValueError("invalid transfer progress")
        if self.activity is not None and not isinstance(self.activity, OperationActivity):
            raise ValueError("invalid activity")
        if self.detail is not None and not isinstance(self.detail, OperationActivity):
            raise ValueError("invalid detail")
        if self.diagnostic is not None and not isinstance(self.diagnostic, OperationDiagnostic):
            raise ValueError("invalid diagnostic")
        if (
            self.kind is OperationEventKind.PROGRESS
            and self.progress is None
            and self.transfer is None
        ):
            raise ValueError("a progress event requires logical or transfer progress")
        if self.kind is OperationEventKind.ACTIVITY and self.activity is None:
            raise ValueError("an activity event requires current activity")
        if self.kind is OperationEventKind.DETAIL and self.detail is None:
            raise ValueError("a detail event requires timeline detail")
        terminal_event = self.kind is OperationEventKind.RESULT
        if terminal_event != self.state.terminal or terminal_event != (self.result is not None):
            raise ValueError("terminal state, RESULT kind and result must occur together")
        if self.result is not None and (
            not isinstance(self.result, OperationResult)
            or self.result.operation_id != self.operation_id
            or self.result.state is not self.state
        ):
            raise ValueError("event and result identity/state disagree")
        if self.state is OperationState.SUCCEEDED and self.activity is not None:
            raise ValueError("successful completion clears current activity")


OperationEventSink = Callable[[OperationEvent], None]

DETAIL_HISTORY_LIMIT = 20


@dataclass(frozen=True)
class OperationTimelineEntry:
    sequence: int
    phase: OperationPhase
    detail: OperationActivity

    def __post_init__(self) -> None:
        _integer(self.sequence, minimum=1)
        if not isinstance(self.phase, OperationPhase):
            raise TypeError("timeline phase must be a typed enum")
        if not isinstance(self.detail, OperationActivity):
            raise TypeError("timeline detail must be an activity")


@dataclass(frozen=True)
class OperationSnapshot:
    """The latest full observation, retained after completion. None means idle."""

    started_at: datetime
    last_event: OperationEvent
    activity: OperationActivity | None = None
    details: tuple[OperationTimelineEntry, ...] = ()
    last_progress: OperationProgress | None = None
    last_transfer: TransferProgress | None = None

    def __post_init__(self) -> None:
        _timestamp(self.started_at)
        if not isinstance(self.last_event, OperationEvent):
            raise TypeError("snapshot requires an operation event")
        if self.activity is not None and not isinstance(self.activity, OperationActivity):
            raise TypeError("snapshot activity must be typed")
        if self.last_progress is not None and not isinstance(self.last_progress, OperationProgress):
            raise TypeError("snapshot progress must be typed")
        if self.last_transfer is not None and not isinstance(self.last_transfer, TransferProgress):
            raise TypeError("snapshot transfer must be typed")
        if isinstance(self.details, (str, bytes)):
            raise TypeError("snapshot details must be timeline entries")
        object.__setattr__(self, "details", tuple(self.details))
        if len(self.details) > DETAIL_HISTORY_LIMIT or any(
            not isinstance(item, OperationTimelineEntry) for item in self.details
        ):
            raise ValueError(f"at most {DETAIL_HISTORY_LIMIT} timeline details are retained")
        sequences = tuple(item.sequence for item in self.details)
        if sequences != tuple(sorted(set(sequences))) or any(
            sequence > self.last_event.sequence for sequence in sequences
        ):
            raise ValueError("timeline detail sequences must be unique, ordered and observed")
        if self.last_event.state is OperationState.SUCCEEDED and self.activity is not None:
            raise ValueError("successful snapshots have no current activity")


def _snapshot_after(current: OperationSnapshot | None, event: OperationEvent) -> OperationSnapshot:
    details = () if current is None else current.details
    if event.detail is not None:
        details = (*details, OperationTimelineEntry(event.sequence, event.phase, event.detail))[
            -DETAIL_HISTORY_LIMIT:
        ]
    if event.state is OperationState.SUCCEEDED:
        activity = None
    elif event.activity is not None:
        activity = event.activity
    else:
        activity = None if current is None else current.activity
    previous_progress = None if current is None else current.last_progress
    previous_transfer = None if current is None else current.last_transfer
    last_progress = event.progress if event.progress is not None else previous_progress
    last_transfer = event.transfer if event.transfer is not None else previous_transfer
    started_at = event.timestamp if current is None else current.started_at
    return OperationSnapshot(started_at, event, activity, details, last_progress, last_transfer)


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
        return _snapshot_after(None, event)
    previous = current.last_event
    if previous.state.terminal:
        raise ValueError("operation already terminal")
    if event.operation_id != previous.operation_id or event.sequence != previous.sequence + 1:
        raise ValueError("operation identity or sequence mismatch")
    states = tuple(OperationState)
    if states.index(event.state) < states.index(previous.state):
        raise ValueError("operation state cannot regress")
    if event.kind in {
        OperationEventKind.PROGRESS,
        OperationEventKind.ACTIVITY,
        OperationEventKind.DETAIL,
    } and (event.state is not previous.state or event.phase is not previous.phase):
        raise ValueError("observation cannot change state or phase")
    return _snapshot_after(current, event)
