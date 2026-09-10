"""Fixtures for the test control API.

Everything here is in-process and loopback: no service is started, no network
leaves the machine, no GPU is touched. The harness is a script the test writes,
which is the point — this suite proves what the API does with a harness, not
what a harness does.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse

import pytest

from local_ai_stack import test_contracts as contracts
from local_ai_stack.test_api import TestApi, TestApiConfig, TestApiToken
from local_ai_stack.test_api.server import API_PREFIX

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

SECRET = "0" * 20 + "a-token-with-enough-entropy-for-the-server"
KNOWN_PROFILES = frozenset({"pepe", "capella"})

Script = Callable[[str, RunSpec, EventSink | None], RunReport]


# --- harness doubles --------------------------------------------------------


class ScriptedHarness:
    """A harness that does exactly what the test told it to do."""

    def __init__(self, script: Script | None = None) -> None:
        self.script = script or passing_script()
        self.calls: list[tuple[str, RunSpec]] = []

    def run(
        self,
        run_id: str,
        spec: RunSpec,
        *,
        emit: EventSink | None = None,
    ) -> RunReport:
        self.calls.append((run_id, spec))
        return self.script(run_id, spec, emit)


def event(
    run_id: str,
    kind: EventKind,
    *,
    sequence: int = 1,
    phase: Phase | None = None,
    message: str = "",
    data: dict[str, object] | None = None,
) -> Event:
    return Event(
        run_id=run_id,
        sequence=sequence,
        kind=kind,
        timestamp="2026-09-09T00:00:00.000Z",
        phase=phase,
        message=message,
        data=data or {},
    )


def report(
    run_id: str,
    spec: RunSpec,
    *,
    state: RunState = RunState.PASSED,
    outcome: PhaseOutcome = PhaseOutcome.PASSED,
    detail: str = "",
) -> RunReport:
    return RunReport(
        run_id=run_id,
        state=state,
        spec=spec,
        phases=tuple(PhaseResult(phase=phase, outcome=outcome) for phase in spec.phases),
        started_at="2026-09-09T00:00:00.000Z",
        finished_at="2026-09-09T00:00:01.000Z",
        detail=detail,
    )


def passing_script(*, emit_result: bool = True, progress: int = 1) -> Script:
    """Emits phase and progress events, then a terminal result."""

    def script(run_id: str, spec: RunSpec, emit: EventSink | None) -> RunReport:
        finished = report(run_id, spec)
        if emit is not None:
            for phase in spec.phases:
                emit(event(run_id, EventKind.PHASE_STATE, phase=phase, message="started"))
                for step in range(progress):
                    emit(
                        event(
                            run_id,
                            EventKind.PROGRESS,
                            phase=phase,
                            message=f"step {step}",
                            data={"step": step},
                        )
                    )
                emit(event(run_id, EventKind.PHASE_STATE, phase=phase, message="passed"))
            if emit_result:
                emit(event(run_id, EventKind.RESULT, message="run passed"))
        return finished

    return script


def failing_script(detail: str = "phase blew up") -> Script:
    def script(run_id: str, spec: RunSpec, emit: EventSink | None) -> RunReport:
        if emit is not None:
            emit(event(run_id, EventKind.PHASE_STATE, phase=spec.phases[0], message="started"))
        return report(
            run_id,
            spec,
            state=RunState.FAILED,
            outcome=PhaseOutcome.FAILED,
            detail=detail,
        )

    return script


def exploding_script(error: Exception) -> Script:
    def script(run_id: str, spec: RunSpec, emit: EventSink | None) -> RunReport:
        raise error

    return script


def gated_script(started, release, *, inner: Script | None = None) -> Script:
    """Blocks inside the harness so a test can observe a run in flight."""

    body = inner or passing_script()

    def script(run_id: str, spec: RunSpec, emit: EventSink | None) -> RunReport:
        if emit is not None:
            emit(event(run_id, EventKind.RUN_STATE, message="running"))
        started.set()
        release.wait(timeout=10)
        return body(run_id, spec, emit)

    return script


# --- transport --------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class Client:
    """A small HTTP client so header handling stays explicit in the tests."""

    def __init__(self, api: TestApi, token: str | None = SECRET) -> None:
        self.api = api
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object | str | None = None,
        headers: dict[str, str] | None = None,
        token: str | None | object = ...,
        content_type: str | None = "application/json",
    ) -> Reply:
        sent = dict(headers or {})
        secret = self.token if token is ... else token
        if secret is not None:
            sent.setdefault("Authorization", f"Bearer {secret}")
        payload: bytes | None = None
        if body is not None:
            payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode()
            if content_type is not None:
                sent.setdefault("Content-Type", content_type)
            sent.setdefault("Content-Length", str(len(payload)))
        connection = HTTPConnection(*self.api.address, timeout=10)
        try:
            connection.request(method, f"{API_PREFIX}{path}", body=payload, headers=sent)
            response = connection.getresponse()
            return Reply(response.status, dict(response.getheaders()), response.read())
        finally:
            connection.close()

    def get(self, path: str, **kwargs: object) -> Reply:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: object) -> Reply:
        return self.request("POST", path, **kwargs)

    def create_run(self, **overrides: object) -> Reply:
        body: dict[str, object] = {"profile_id": "pepe", "phases": ["preflight"]}
        body.update(overrides)
        return self.post("/runs", body=body)


def parse_frames(response: HTTPResponse, *, limit: int | None = None) -> list[dict[str, str]]:
    """Read SSE frames until the terminal event, EOF, or a frame budget."""
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    while True:
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8").rstrip("\n")
        if line.startswith(":"):
            continue
        if not line:
            if current:
                frames.append(current)
                if current.get("event") == EventKind.RESULT.value:
                    break
                if limit is not None and len(frames) >= limit:
                    break
                current = {}
            continue
        name, _, value = line.partition(":")
        current[name.strip()] = value.strip()
    return frames


def open_stream(
    api: TestApi,
    run_id: str,
    *,
    token: str | None = SECRET,
    last_event_id: int | None = None,
    timeout: float = 10.0,
) -> tuple[HTTPConnection, HTTPResponse]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    connection = HTTPConnection(*api.address, timeout=timeout)
    connection.request("GET", f"{API_PREFIX}/runs/{run_id}/events", headers=headers)
    return connection, connection.getresponse()


def stream_events(
    api: TestApi,
    run_id: str,
    *,
    last_event_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, str]]:
    connection, response = open_stream(api, run_id, last_event_id=last_event_id)
    try:
        return parse_frames(response, limit=limit)
    finally:
        connection.close()


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def serve():
    """Start a server for one test and stop it afterwards, whatever happens."""
    started: list[TestApi] = []

    def _serve(harness: object | None = None, **overrides: object) -> TestApi:
        config = TestApiConfig(
            token=TestApiToken(SECRET),
            known_profiles=KNOWN_PROFILES,
            keepalive_seconds=float(overrides.pop("keepalive_seconds", 0.2)),
            **overrides,
        )
        api = TestApi(config, harness or ScriptedHarness())
        api.start()
        started.append(api)
        return api

    yield _serve
    for api in started:
        api.stop()


@pytest.fixture
def harness() -> ScriptedHarness:
    return ScriptedHarness()


@pytest.fixture
def api(serve, harness) -> TestApi:
    return serve(harness)


@pytest.fixture
def client(api) -> Client:
    return Client(api)


def wait_for_run(api: TestApi, run_id: str, timeout: float = 10.0):
    return api.store.wait_for_finish(run_id, timeout=timeout)


def phases(*names: str) -> Sequence[str]:
    return list(names)
