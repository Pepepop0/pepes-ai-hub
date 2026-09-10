"""Run storage, scheduling, and the event sequence a stream replays.

This module holds the only state the API owns. It does not know what a phase
does; it knows that a run was asked for, that exactly one mutating run may be
in flight, that events arrive in order, and that exactly one terminal event
ends a run.

Two invariants are worth stating outright.

A run id is an opaque identifier and never a location. It is generated here,
matched against a fixed pattern on the way in, and never joined to a path, so
there is nothing for a caller to traverse.

Retention is bounded in both directions: a fixed number of runs, a fixed number
of events per run, and a bound on how much a single event may carry. A run that
talks forever cannot make this process grow forever.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from ..test_contracts import (
    TestEvent,
    TestEventKind,
    TestPhaseOutcome,
    TestRunReport,
    TestRunSpec,
    TestRunState,
)
from .sanitize import bound_text, json_safe

RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

MAX_RUNS = 32
MAX_EVENTS_PER_RUN = 512
MAX_EVENT_DATA_BYTES = 8192
MAX_EVENT_MESSAGE_CHARS = 500

ACTIVE_STATES = frozenset({TestRunState.QUEUED, TestRunState.RUNNING})


class RunError(RuntimeError):
    """A run could not be created, found, or streamed."""


class RunInProgress(RunError):
    """Another run is already queued or running, and only one may mutate."""


class UnknownRun(RunError):
    """No run with that identifier is retained."""


class EventsExpired(RunError):
    """The requested resume point has already left the bounded buffer."""


class EventsAhead(RunError):
    """The requested resume point is past anything this run has emitted."""


def new_run_id() -> str:
    """An opaque identifier with no meaning and no filesystem interpretation."""
    return secrets.token_hex(16)


def is_run_id(value: str) -> bool:
    """Whether a caller-supplied identifier is one this store could have issued."""
    return bool(RUN_ID_PATTERN.match(value or ""))


def utc_now() -> str:
    """An RFC 3339 timestamp in UTC."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class RunRecord:
    """One run: what was asked for, where it got to, and what it emitted."""

    run_id: str
    spec: TestRunSpec
    created_at: str
    state: TestRunState = TestRunState.QUEUED
    started_at: str | None = None
    finished_at: str | None = None
    report: TestRunReport | None = None
    detail: str = ""
    events: deque[TestEvent] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_RUN))
    emitted: int = 0
    dropped_events: int = 0
    truncated_events: int = 0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def terminal_emitted(self) -> bool:
        return any(event.kind is TestEventKind.RESULT for event in self.events)

    @property
    def oldest_sequence(self) -> int:
        return self.events[0].sequence if self.events else self.emitted + 1

    def diagnostics(self) -> dict[str, object]:
        """Bounded facts about the stream itself, never about the machine."""
        return {
            "events_emitted": self.emitted,
            "events_retained": len(self.events),
            "events_retained_from": self.oldest_sequence,
            "events_dropped_after_result": self.dropped_events,
            "events_truncated": self.truncated_events,
        }


def _bounded(event: TestEvent) -> tuple[TestEvent, bool]:
    """The same event, with its message and data held to a size."""
    data: Mapping[str, object] = event.data
    truncated = False
    if not json_safe(dict(data)):
        data = {"unserializable": True}
        truncated = True
    else:
        encoded = json.dumps(dict(data))
        if len(encoded) > MAX_EVENT_DATA_BYTES:
            data = {"truncated": True, "bytes": len(encoded)}
            truncated = True
    message = bound_text(event.message, limit=MAX_EVENT_MESSAGE_CHARS)
    if message == event.message and not truncated:
        return event, False
    return replace(event, message=message, data=data), truncated


class RunStore:
    """Every run this server remembers, and the events each one produced."""

    def __init__(self, *, max_runs: int = MAX_RUNS) -> None:
        self._max_runs = max_runs
        self._records: dict[str, RunRecord] = {}
        self._order: deque[str] = deque()
        self._condition = threading.Condition()

    # --- lifecycle ----------------------------------------------------------

    def create(self, spec: TestRunSpec) -> RunRecord:
        """Register a queued run, refusing a second one while any is active."""
        with self._condition:
            if any(record.active for record in self._records.values()):
                raise RunInProgress("a test run is already queued or running")
            record = RunRecord(run_id=new_run_id(), spec=spec, created_at=utc_now())
            self._records[record.run_id] = record
            self._order.append(record.run_id)
            self._evict_locked()
            return record

    def _evict_locked(self) -> None:
        while len(self._order) > self._max_runs:
            oldest = self._order[0]
            record = self._records.get(oldest)
            if record is not None and record.active:
                # Never forget a run that is still producing events.
                break
            self._order.popleft()
            self._records.pop(oldest, None)

    def get(self, run_id: str) -> RunRecord:
        if not is_run_id(run_id):
            raise UnknownRun("no such run")
        with self._condition:
            record = self._records.get(run_id)
            if record is None:
                raise UnknownRun("no such run")
            return record

    def active_run(self) -> RunRecord | None:
        with self._condition:
            for run_id in reversed(self._order):
                record = self._records[run_id]
                if record.active:
                    return record
        return None

    def counts(self) -> dict[str, int]:
        with self._condition:
            tally = {state.value: 0 for state in TestRunState}
            for record in self._records.values():
                tally[record.state.value] += 1
            return tally

    # --- state transitions --------------------------------------------------

    def mark_running(self, run_id: str) -> None:
        with self._condition:
            record = self._records[run_id]
            record.state = TestRunState.RUNNING
            record.started_at = utc_now()
            self._condition.notify_all()

    def finish(
        self,
        run_id: str,
        *,
        state: TestRunState,
        report: TestRunReport | None,
        detail: str = "",
    ) -> RunRecord:
        with self._condition:
            record = self._records[run_id]
            record.state = state
            record.report = report
            record.detail = bound_text(detail, limit=MAX_EVENT_MESSAGE_CHARS)
            record.finished_at = utc_now()
            self._condition.notify_all()
            return record

    def wait_for_finish(self, run_id: str, timeout: float = 10.0) -> RunRecord:
        """Block until a run leaves the active states. Used by callers, not routes."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                record = self._records.get(run_id)
                if record is None:
                    raise UnknownRun("no such run")
                if not record.active:
                    return record
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"run {run_id} did not finish in {timeout}s")
                self._condition.wait(timeout=remaining)

    # --- events -------------------------------------------------------------

    def append(self, run_id: str, event: TestEvent) -> TestEvent | None:
        """Store one event in order, or drop it if the run already ended.

        The sequence is assigned here rather than trusted, so ordering is a
        property of the store and not of whoever emitted. Everything else about
        the event is kept as the producer meant it, bounded but not rewritten.
        """
        with self._condition:
            record = self._records.get(run_id)
            if record is None:
                raise UnknownRun("no such run")
            if event.run_id != run_id:
                raise RunError("an event was emitted for a different run")
            if record.terminal_emitted:
                record.dropped_events += 1
                return None
            record.emitted += 1
            bounded, truncated = _bounded(replace(event, sequence=record.emitted))
            record.truncated_events += int(truncated)
            record.events.append(bounded)
            self._condition.notify_all()
            return bounded

    def events_since(self, run_id: str, after: int) -> list[TestEvent]:
        """Retained events with a sequence above ``after``.

        A cursor has to land inside the range this run actually produced. Below
        the buffer it is expired; above everything emitted it names an event
        that never existed, and answering "nothing new yet" would tell a client
        it is caught up on a stream it has never seen.
        """
        with self._condition:
            record = self._records.get(run_id)
            if record is None:
                raise UnknownRun("no such run")
            if after > record.emitted:
                raise EventsAhead(
                    f"resume point {after} is past the {record.emitted} event(s) this run emitted"
                )
            if after and record.events and after < record.events[0].sequence - 1:
                raise EventsExpired("the requested resume point is no longer buffered")
            return [event for event in record.events if event.sequence > after]

    def follow(
        self,
        run_id: str,
        *,
        after: int = 0,
        stopping: threading.Event,
        keepalive_seconds: float = 15.0,
    ) -> Iterator[TestEvent | None]:
        """Yield events in order, then stop after the terminal one.

        ``None`` is yielded when nothing arrived within the keep-alive window,
        which lets the caller write a comment line that carries no application
        state.
        """
        cursor = after
        for event in self.events_since(run_id, after):
            cursor = event.sequence
            yield event
            if event.kind is TestEventKind.RESULT:
                return
        while not stopping.is_set():
            with self._condition:
                record = self._records.get(run_id)
                if record is None:
                    return
                pending = [event for event in record.events if event.sequence > cursor]
                if not pending:
                    if not record.active:
                        # Finished, and the terminal event is no longer buffered.
                        return
                    self._condition.wait(timeout=keepalive_seconds)
                    pending = [event for event in record.events if event.sequence > cursor]
            if not pending:
                yield None
                continue
            for event in pending:
                cursor = event.sequence
                yield event
                if event.kind is TestEventKind.RESULT:
                    return


def failed_report(run_id: str, spec: TestRunSpec, detail: str) -> TestRunReport:
    """A report for a run whose harness never produced one."""
    return TestRunReport(
        run_id=run_id,
        state=TestRunState.FAILED,
        spec=spec,
        detail=bound_text(detail, limit=MAX_EVENT_MESSAGE_CHARS),
    )


def report_state(report: TestRunReport) -> TestRunState:
    """The state a report implies, without second-guessing the harness."""
    if report.state in {TestRunState.PASSED, TestRunState.FAILED}:
        return report.state
    failed = any(
        phase.outcome in {TestPhaseOutcome.FAILED, TestPhaseOutcome.BLOCKED}
        for phase in report.phases
    )
    return TestRunState.FAILED if failed else TestRunState.PASSED


__all__ = [
    "ACTIVE_STATES",
    "MAX_EVENTS_PER_RUN",
    "MAX_EVENT_DATA_BYTES",
    "MAX_RUNS",
    "RUN_ID_PATTERN",
    "EventsAhead",
    "EventsExpired",
    "RunError",
    "RunInProgress",
    "RunRecord",
    "RunStore",
    "UnknownRun",
    "failed_report",
    "is_run_id",
    "new_run_id",
    "report_state",
    "utc_now",
]
