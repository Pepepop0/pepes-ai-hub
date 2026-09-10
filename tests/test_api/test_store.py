"""The run store on its own: identifiers, retention, ordering, bounds."""

from __future__ import annotations

import threading

import pytest

from local_ai_stack import test_contracts as contracts
from local_ai_stack.test_api.runs import (
    MAX_EVENTS_PER_RUN,
    EventsAhead,
    EventsExpired,
    RunInProgress,
    RunStore,
    UnknownRun,
    is_run_id,
    new_run_id,
    report_state,
    utc_now,
)

from .conftest import event

# Bound to names that pytest will not try to collect as test classes, the way
# tests/test_test_contracts.py already does.
Event = contracts.TestEvent
EventKind = contracts.TestEventKind
EventSink = contracts.TestEventSink
Phase = contracts.TestPhase
PhaseOutcome = contracts.TestPhaseOutcome
PhaseResult = contracts.TestPhaseResult
RunReport = contracts.TestRunReport
RunSpec = contracts.TestRunSpec
RunState = contracts.TestRunState


SPEC = RunSpec(profile_id="pepe", phases=(Phase.PREFLIGHT,))


@pytest.fixture
def store() -> RunStore:
    return RunStore()


# --- identifiers ------------------------------------------------------------


def test_a_generated_id_is_opaque_and_unique():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(is_run_id(value) for value in ids)
    assert all(len(value) == 32 for value in ids)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "..",
        "../..",
        "a/b",
        "a\\b",
        "C:\\temp",
        "/etc/passwd",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "z" * 32,
        "0123456789abcdef0123456789abcde!",
        "  " + "a" * 32,
    ],
)
def test_an_identifier_that_is_not_ours_is_not_accepted(value):
    """Hex only, fixed length: there is nothing here to interpret as a path."""
    assert not is_run_id(value)


def test_an_unknown_identifier_is_never_looked_up(store):
    with pytest.raises(UnknownRun):
        store.get("../../secrets")
    with pytest.raises(UnknownRun):
        store.get("a" * 32)


# --- one at a time ----------------------------------------------------------


def test_only_one_run_may_be_active(store):
    first = store.create(SPEC)
    with pytest.raises(RunInProgress):
        store.create(SPEC)

    store.finish(first.run_id, state=RunState.PASSED, report=None)
    assert store.create(SPEC).run_id != first.run_id


def test_the_active_run_is_the_one_still_going(store):
    record = store.create(SPEC)
    assert store.active_run().run_id == record.run_id
    store.mark_running(record.run_id)
    assert store.active_run().state is RunState.RUNNING
    store.finish(record.run_id, state=RunState.FAILED, report=None)
    assert store.active_run() is None
    assert store.counts()["failed"] == 1


# --- retention --------------------------------------------------------------


def test_old_runs_are_forgotten_so_the_process_does_not_grow():
    store = RunStore(max_runs=3)
    kept = []
    for _ in range(6):
        record = store.create(SPEC)
        store.finish(record.run_id, state=RunState.PASSED, report=None)
        kept.append(record.run_id)

    assert sum(store.counts().values()) == 3
    for run_id in kept[-3:]:
        assert store.get(run_id) is not None
    for run_id in kept[:-3]:
        with pytest.raises(UnknownRun):
            store.get(run_id)


def test_events_are_capped_per_run(store):
    record = store.create(SPEC)
    for index in range(MAX_EVENTS_PER_RUN + 50):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS, message=str(index)))

    assert len(record.events) == MAX_EVENTS_PER_RUN
    assert record.emitted == MAX_EVENTS_PER_RUN + 50
    assert record.events[-1].sequence == MAX_EVENTS_PER_RUN + 50


# --- ordering ---------------------------------------------------------------


def test_the_store_assigns_the_sequence_rather_than_trusting_it(store):
    record = store.create(SPEC)
    for _ in range(5):
        stored = store.append(record.run_id, event(record.run_id, EventKind.PROGRESS, sequence=99))
        assert stored is not None
    assert [item.sequence for item in record.events] == [1, 2, 3, 4, 5]


def test_an_event_for_another_run_is_refused(store):
    record = store.create(SPEC)
    with pytest.raises(Exception, match="different run"):
        store.append(record.run_id, event("b" * 32, EventKind.PROGRESS))


def test_nothing_is_stored_after_the_terminal_event(store):
    record = store.create(SPEC)
    store.append(record.run_id, event(record.run_id, EventKind.RESULT))
    assert store.append(record.run_id, event(record.run_id, EventKind.PROGRESS)) is None
    assert record.dropped_events == 1
    assert len(record.events) == 1


def test_events_since_returns_only_what_follows(store):
    record = store.create(SPEC)
    for _ in range(4):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS))

    assert [item.sequence for item in store.events_since(record.run_id, 0)] == [1, 2, 3, 4]
    assert [item.sequence for item in store.events_since(record.run_id, 2)] == [3, 4]
    assert store.events_since(record.run_id, 4) == []


def test_a_cursor_behind_the_buffer_is_an_error_not_a_gap(store):
    record = store.create(SPEC)
    for _ in range(6):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS))
    kept = list(record.events)[-2:]
    record.events.clear()
    record.events.extend(kept)

    assert [item.sequence for item in store.events_since(record.run_id, 4)] == [5, 6]
    with pytest.raises(EventsExpired):
        store.events_since(record.run_id, 1)


# --- waiting ----------------------------------------------------------------


def test_waiting_returns_as_soon_as_the_run_ends(store):
    record = store.create(SPEC)

    def finish() -> None:
        store.finish(record.run_id, state=RunState.PASSED, report=None)

    threading.Timer(0.05, finish).start()
    assert store.wait_for_finish(record.run_id, timeout=5).state is RunState.PASSED


def test_waiting_gives_up_rather_than_hanging(store):
    record = store.create(SPEC)
    with pytest.raises(TimeoutError):
        store.wait_for_finish(record.run_id, timeout=0.05)


# --- report reading ---------------------------------------------------------


def report_with(state: RunState, *outcomes: PhaseOutcome) -> RunReport:
    return RunReport(
        run_id="a" * 32,
        state=state,
        spec=SPEC,
        phases=tuple(PhaseResult(phase=Phase.PREFLIGHT, outcome=outcome) for outcome in outcomes),
    )


@pytest.mark.parametrize(
    ("state", "outcomes", "expected"),
    [
        (RunState.PASSED, (PhaseOutcome.PASSED,), RunState.PASSED),
        (RunState.FAILED, (PhaseOutcome.PASSED,), RunState.FAILED),
        (RunState.RUNNING, (PhaseOutcome.PASSED,), RunState.PASSED),
        (RunState.RUNNING, (PhaseOutcome.FAILED,), RunState.FAILED),
        (RunState.RUNNING, (PhaseOutcome.BLOCKED,), RunState.FAILED),
        (RunState.RUNNING, (PhaseOutcome.SKIPPED,), RunState.PASSED),
    ],
)
def test_a_report_is_read_not_second_guessed(state, outcomes, expected):
    """A harness that states its verdict keeps it; one that does not is read."""
    assert report_state(report_with(state, *outcomes)) is expected


def test_timestamps_are_utc_and_ordered(store):
    first = utc_now()
    record = store.create(SPEC)
    assert record.created_at.endswith("Z")
    assert first <= record.created_at


# --- a cursor ahead of the run ----------------------------------------------


def test_a_cursor_past_everything_emitted_is_an_error(store):
    """Answering "nothing new" would claim the reader is caught up on nothing."""
    record = store.create(SPEC)
    for _ in range(3):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS))

    assert [item.sequence for item in store.events_since(record.run_id, 3)] == []
    with pytest.raises(EventsAhead, match="3 event"):
        store.events_since(record.run_id, 4)
    with pytest.raises(EventsAhead):
        store.events_since(record.run_id, 9999)


def test_a_cursor_on_a_run_that_has_emitted_nothing_is_ahead(store):
    record = store.create(SPEC)
    assert store.events_since(record.run_id, 0) == []
    with pytest.raises(EventsAhead):
        store.events_since(record.run_id, 1)


def test_the_two_out_of_range_cursors_are_different_errors(store):
    record = store.create(SPEC)
    for _ in range(6):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS))
    kept = list(record.events)[-2:]
    record.events.clear()
    record.events.extend(kept)

    with pytest.raises(EventsExpired):
        store.events_since(record.run_id, 1)
    with pytest.raises(EventsAhead):
        store.events_since(record.run_id, 7)


def test_eviction_does_not_turn_a_valid_cursor_into_a_future_one(store):
    """`emitted` counts everything, so trimming the buffer never moves the ceiling."""
    record = store.create(SPEC)
    for _ in range(5):
        store.append(record.run_id, event(record.run_id, EventKind.PROGRESS))
    record.events.clear()

    assert record.emitted == 5
    assert store.events_since(record.run_id, 5) == []
    with pytest.raises(EventsAhead):
        store.events_since(record.run_id, 6)
