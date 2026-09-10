"""The event stream: order, resumption, and exactly one ending."""

from __future__ import annotations

import json
import threading

from local_ai_stack import test_contracts as contracts

from .conftest import (
    Client,
    ScriptedHarness,
    event,
    gated_script,
    open_stream,
    parse_frames,
    passing_script,
    report,
    stream_events,
    wait_for_run,
)

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


def finished_run(client, **overrides):
    run_id = client.create_run(**overrides).json()["run_id"]
    wait_for_run(client.api, run_id)
    return run_id


# --- shape ------------------------------------------------------------------


def test_a_stream_is_server_sent_events_and_nothing_else(api, client):
    run_id = finished_run(client)
    connection, response = open_stream(api, run_id)
    try:
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.getheader("Cache-Control") == "no-store"
        frames = parse_frames(response)
    finally:
        connection.close()
    assert frames


def test_every_frame_carries_the_id_kind_and_the_whole_event(api, client):
    run_id = finished_run(client, phases=["preflight", "plan"])
    frames = stream_events(api, run_id)
    for frame in frames:
        payload = json.loads(frame["data"])
        assert frame["id"] == str(payload["sequence"])
        assert frame["event"] == payload["kind"]
        assert payload["run_id"] == run_id
        assert set(payload) == {
            "run_id",
            "sequence",
            "kind",
            "timestamp",
            "phase",
            "message",
            "data",
        }


def test_the_sequence_is_monotonic_and_starts_at_one(api, client):
    run_id = finished_run(client, phases=["preflight", "plan", "cleanup"])
    frames = stream_events(api, run_id)
    sequences = [int(frame["id"]) for frame in frames]
    assert sequences == list(range(1, len(sequences) + 1))


def test_the_order_is_the_order_the_harness_emitted(api, client):
    run_id = finished_run(client, phases=["preflight", "plan"])
    frames = stream_events(api, run_id)
    kinds = [frame["event"] for frame in frames]
    assert kinds == [
        "phase-state",
        "progress",
        "phase-state",
        "phase-state",
        "progress",
        "phase-state",
        "result",
    ]


# --- the ending -------------------------------------------------------------


def test_a_stream_ends_with_exactly_one_result(api, client):
    run_id = finished_run(client, phases=["preflight", "plan"])
    frames = stream_events(api, run_id)
    terminal = [frame for frame in frames if frame["event"] == EventKind.RESULT.value]
    assert len(terminal) == 1
    assert frames[-1]["event"] == EventKind.RESULT.value


def test_a_second_result_from_the_harness_is_dropped(serve):
    def two_endings(run_id, spec, emit):
        emit(event(run_id, EventKind.RESULT, message="first"))
        emit(event(run_id, EventKind.RESULT, message="second"))
        emit(event(run_id, EventKind.PROGRESS, message="after the end"))
        return report(run_id, spec)

    api = serve(ScriptedHarness(two_endings))
    client = Client(api)
    run_id = finished_run(client)

    frames = stream_events(api, run_id)
    assert [frame["event"] for frame in frames] == ["result"]
    assert json.loads(frames[0]["data"])["message"] == "first"
    assert client.get(f"/runs/{run_id}").json()["diagnostics"]["events_dropped_after_result"] == 2


def test_a_harness_that_never_ends_the_stream_still_gets_an_ending(serve):
    """The API owes the reader a terminal event even when the producer forgot."""
    api = serve(ScriptedHarness(passing_script(emit_result=False)))
    client = Client(api)
    run_id = finished_run(client)

    frames = stream_events(api, run_id)
    assert frames[-1]["event"] == "result"
    payload = json.loads(frames[-1]["data"])
    assert payload["data"]["report"]["state"] == "passed"


def test_a_harness_that_raises_still_ends_the_stream(serve):
    def explode(run_id, spec, emit):
        emit(event(run_id, EventKind.PROGRESS, message="here we go"))
        raise RuntimeError("nope")

    api = serve(ScriptedHarness(explode))
    client = Client(api)
    run_id = finished_run(client)

    frames = stream_events(api, run_id)
    assert [frame["event"] for frame in frames] == ["progress", "result"]
    assert json.loads(frames[-1]["data"])["data"]["report"]["state"] == "failed"


# --- resuming ---------------------------------------------------------------


def test_last_event_id_resumes_after_that_event(api, client):
    run_id = finished_run(client, phases=["preflight", "plan"])
    everything = stream_events(api, run_id)
    assert len(everything) > 3

    resumed = stream_events(api, run_id, last_event_id=3)
    assert [frame["id"] for frame in resumed] == [frame["id"] for frame in everything[3:]]
    assert resumed[0]["id"] == "4"


def test_resuming_from_the_end_yields_nothing_more(api, client):
    run_id = finished_run(client)
    everything = stream_events(api, run_id)
    assert stream_events(api, run_id, last_event_id=int(everything[-1]["id"])) == []


def test_resuming_from_zero_replays_everything(api, client):
    run_id = finished_run(client)
    assert stream_events(api, run_id, last_event_id=0) == stream_events(api, run_id)


def test_a_malformed_resume_point_is_refused(api, client):
    run_id = finished_run(client)
    for value in ("not-a-number", "-1", "1.5", ""):
        connection, response = open_stream(api, run_id, last_event_id=value)
        try:
            assert response.status == 400, value
        finally:
            connection.close()


def test_a_resume_point_that_left_the_buffer_is_refused(serve):
    """Retention is bounded, so an old cursor is an error rather than a silent gap."""

    def chatty(run_id, spec, emit):
        for index in range(12):
            emit(event(run_id, EventKind.PROGRESS, message=f"step {index}"))
        emit(event(run_id, EventKind.RESULT, message="done"))
        return report(run_id, spec)

    api = serve(ScriptedHarness(chatty))
    client = Client(api)
    run_id = finished_run(client)

    # Age the buffer the way retention would, then ask for a cursor behind it.
    record = api.store.get(run_id)
    kept = list(record.events)[-3:]
    record.events.clear()
    record.events.extend(kept)

    connection, response = open_stream(api, run_id, last_event_id=1)
    try:
        assert response.status == 409
        assert json.loads(response.read())["error"] == "events-expired"
    finally:
        connection.close()


# --- live streams -----------------------------------------------------------


def test_a_reader_attached_before_the_end_sees_the_whole_run(serve):
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)

    collected: list[list[dict[str, str]]] = []
    connection, response = open_stream(api, run_id)

    def read() -> None:
        collected.append(parse_frames(response))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    release.set()
    reader.join(timeout=10)
    connection.close()

    assert collected, "the reader finished"
    frames = collected[0]
    assert frames[0]["event"] == "run-state"
    assert frames[-1]["event"] == "result"


def test_two_readers_see_the_same_stream(api, client):
    run_id = finished_run(client, phases=["preflight", "plan"])
    results: list[list[dict[str, str]]] = []
    lock = threading.Lock()

    def read() -> None:
        frames = stream_events(api, run_id)
        with lock:
            results.append(frames)

    readers = [threading.Thread(target=read, daemon=True) for _ in range(2)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=10)

    assert len(results) == 2
    assert results[0] == results[1]


def test_the_stream_never_accepts_a_command(api, client):
    """SSE is output only: a body on the events route changes nothing."""
    run_id = finished_run(client)
    reply = client.request("POST", f"/runs/{run_id}/events", body={"stop": True})
    assert reply.status == 404


# --- bounding ---------------------------------------------------------------


def test_an_oversized_event_is_bounded_rather_than_streamed(serve):
    def noisy(run_id, spec, emit):
        emit(
            event(
                run_id,
                EventKind.LOG_REFERENCE,
                message="x" * 5000,
                data={"tail": "y" * 20000},
            )
        )
        emit(event(run_id, EventKind.RESULT, message="done"))
        return report(run_id, spec)

    api = serve(ScriptedHarness(noisy))
    client = Client(api)
    run_id = finished_run(client)

    frames = stream_events(api, run_id)
    payload = json.loads(frames[0]["data"])
    assert payload["data"]["truncated"] is True
    assert payload["data"]["bytes"] > 20000
    assert len(payload["message"]) < 600
    assert client.get(f"/runs/{run_id}").json()["diagnostics"]["events_truncated"] == 1


def test_a_report_is_still_the_harness_report_after_streaming(serve):
    def script(run_id, spec, emit):
        emit(event(run_id, EventKind.RESULT, message="done"))
        return RunReport(
            run_id=run_id,
            state=RunState.PASSED,
            spec=spec,
            detail="all good",
        )

    api = serve(ScriptedHarness(script))
    client = Client(api)
    run_id = finished_run(client)
    body = client.get(f"/runs/{run_id}").json()
    assert body["report"]["detail"] == "all good"
    assert body["report"]["state"] == "passed"
