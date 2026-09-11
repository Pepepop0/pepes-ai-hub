"""Real HTTP -> scheduler -> StackTestHarness, using only controlled worlds."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from local_ai_stack.e2e import tracking
from local_ai_stack.e2e.harness import StackTestHarness
from local_ai_stack.e2e.scenarios import P0_SCENARIOS, scenario_by_id
from local_ai_stack.test_api import ShutdownTimeout, TestApiConfig, TestApiToken
from local_ai_stack.test_api.binding import BindRefused
from local_ai_stack.test_api.payloads import run_report_payload, run_spec_payload
from local_ai_stack.test_application import (
    ControlledApplication,
    create_test_api,
    new_controlled_harness,
)

from .conftest import (
    SECRET,
    Client,
    Phase,
    PhaseOutcome,
    RunSpec,
    RunState,
    open_stream,
    parse_frames,
    stream_events,
    wait_for_run,
)
from .test_lifecycle import port_is_free


@pytest.fixture
def integrated():
    """Own every fixture, server, socket and worker this module creates."""
    before_paths = set(tracking.outstanding())
    before_threads = set(threading.enumerate())
    apis, setups, harnesses, addresses = [], [], [], []

    def build(setup=None, **overrides):
        if setup is not None:
            setups.append(setup)

        def factory(spec):
            if setup is not None:
                assert spec == setup.spec
                harness = StackTestHarness(setup.world)
            else:
                harness = new_controlled_harness(spec)
            harnesses.append(harness)
            return harness

        config = TestApiConfig(
            token=TestApiToken(SECRET),
            known_profiles=frozenset({setup.spec.profile_id if setup else "pepe"}),
            allow_gpu=bool(setup and setup.spec.allow_gpu),
            keepalive_seconds=0.05,
            **overrides,
        )
        allocated = len(harnesses)
        api = create_test_api(config, harness_factory=factory)
        apis.append(api)
        assert len(harnesses) == allocated, "constructing the API allocated a world"
        api.start()
        addresses.append(api.address)
        return api, harnesses

    yield build

    try:
        for api in apis:
            api.stop(timeout=10)
    finally:
        for harness in harnesses:
            harness.world.root.release()
        for setup in setups:
            setup.world.root.release()
            if setup.teardown:
                setup.teardown()
    # Request handlers are daemon threads, so explicitly account for their exit.
    for thread in set(threading.enumerate()) - before_threads:
        thread.join(timeout=2)
        assert not thread.is_alive(), thread.name
    for address in addresses:
        assert port_is_free(*address), address
    assert set(tracking.outstanding()) == before_paths
    for api in apis:
        assert not api.scheduler.busy
        assert api.handler_errors == []


def submit(api, spec):
    response = Client(api).post("/runs", body=run_spec_payload(spec))
    assert response.status == 202, response.body
    run_id = response.json()["run_id"]
    return wait_for_run(api, run_id)


def assert_stream(api, record):
    connection, response = open_stream(api, record.run_id)
    try:
        assert response.status == 200
        frames = parse_frames(response)
        assert response.read() == b"", "an event arrived after the terminal event"
    finally:
        response.close()
        connection.close()
    assert [int(f["id"]) for f in frames] == list(range(1, len(frames) + 1))
    assert [f["event"] for f in frames].count("result") == 1
    assert frames[-1]["event"] == "result"
    assert json.loads(frames[-1]["data"])["data"]["state"] == record.report.state.value
    assert SECRET not in json.dumps(frames)
    assert record.dropped_events == 0
    return frames


@pytest.mark.parametrize("scenario", P0_SCENARIOS, ids=lambda row: row.id)
def test_harness_p0_through_http(integrated, scenario):
    setup = scenario.setup()
    api, harnesses = integrated(setup)
    record = submit(api, setup.spec)
    assert len(harnesses) == 1 and type(harnesses[0]) is StackTestHarness
    assert record.spec == setup.spec
    assert scenario.verdict(record.report) == []
    assert scenario.verify(record.report, setup.world) == []
    response = Client(api).get(f"/runs/{record.run_id}")
    assert response.status == 200
    assert response.json()["report"] == json.loads(json.dumps(run_report_payload(record.report)))
    assert_stream(api, record)
    # Independently enforce cleanup even on rows expecting a failed phase.
    assert setup.world.managed_residual() == ()
    assert setup.world.external_lost() == ()
    cleanup = next(p for p in record.report.phases if p.phase is Phase.CLEANUP)
    assert cleanup.data["membership"] == {}
    assert cleanup.data["registry"] == []
    assert cleanup.data["job_handles_released"]
    assert not setup.world.root.exists()


@pytest.mark.parametrize("phases", [(Phase.PREFLIGHT,), (Phase.PREFLIGHT, Phase.PLAN)])
def test_api_and_direct_harness_have_equivalent_results(integrated, phases):
    spec = RunSpec(profile_id="pepe", phases=phases)
    direct_harness = new_controlled_harness(spec)
    direct = direct_harness.run("direct-equivalence", spec)
    api, _ = integrated()
    via_http = submit(api, spec).report
    assert via_http.state is direct.state
    assert [(p.phase, p.outcome) for p in via_http.phases] == [
        (p.phase, p.outcome) for p in direct.phases
    ]
    for phase in phases:
        actual = next(p for p in via_http.phases if p.phase is phase)
        expected = next(p for p in direct.phases if p.phase is phase)
        # Root paths and disk free space are per-run observations, not equivalence keys.
        if phase is Phase.PLAN:

            def stable_steps(result):
                return [
                    {**step, "target": "<root>"} if step["action"] == "prepare-layout" else step
                    for step in result.data["steps"]
                ]

            assert stable_steps(actual) == stable_steps(expected)
        else:
            assert actual.data["ok"] == expected.data["ok"]
            assert actual.data["issues"] == expected.data["issues"]


def test_consecutive_requests_get_fresh_worlds(integrated):
    api, harnesses = integrated()
    for _ in range(2):
        record = submit(api, RunSpec(profile_id="pepe", phases=(Phase.PREFLIGHT, Phase.PLAN)))
        assert record.state is RunState.PASSED
        assert not harnesses[-1].world.root.exists()
    assert harnesses[0].world is not harnesses[1].world
    assert harnesses[0].world.root.path != harnesses[1].world.root.path


def test_full_run_equivalence_includes_acquisition_config_and_smokes(integrated):
    phases = (
        Phase.PREFLIGHT,
        Phase.PLAN,
        Phase.PROVISION,
        Phase.START,
        Phase.ACQUIRE_MODELS,
        Phase.CONFIGURE,
        Phase.CHAT_SMOKE,
        Phase.IMAGE_SMOKE,
        Phase.STOP,
        Phase.SECOND_PLAN,
    )
    direct_setup = scenario_by_id("second-run").setup()
    direct_setup.spec = replace(direct_setup.spec, phases=phases, allow_gpu=True)
    try:
        direct = StackTestHarness(direct_setup.world).run("direct-full", direct_setup.spec)
    finally:
        direct_setup.world.root.release()
        direct_setup.teardown()
    setup = scenario_by_id("second-run").setup()
    setup.spec = replace(setup.spec, phases=phases, allow_gpu=True)
    api, _ = integrated(setup)
    record = submit(api, setup.spec)
    assert direct.state is record.state is RunState.PASSED
    assert all(p.outcome is PhaseOutcome.PASSED for p in record.report.phases)
    expected = {p.phase: p for p in direct.phases}
    actual = {p.phase: p for p in record.report.phases}
    assert actual.keys() == expected.keys()
    for phase, keys in {
        Phase.PROVISION: ("ok", "components", "results"),
        Phase.ACQUIRE_MODELS: ("ok", "actions", "pulls", "downloads"),
        Phase.CONFIGURE: ("ok", "roles"),
        Phase.CHAT_SMOKE: ("model", "answered", "done"),
        Phase.IMAGE_SMOKE: ("workflow", "runtime_name", "submitted", "images"),
        Phase.SECOND_PLAN: ("ok", "would_install", "would_acquire", "proposed_steps"),
        Phase.CLEANUP: ("membership", "registry", "job_handles_released"),
    }.items():
        for key in keys:
            assert actual[phase].data[key] == expected[phase].data[key], (phase, key)
    assert Client(api).get(f"/runs/{record.run_id}").json()["report"] == json.loads(
        json.dumps(run_report_payload(record.report))
    )
    assert_stream(api, record)


def test_composition_itself_opens_no_socket_thread_or_root():
    before_paths = set(tracking.outstanding())
    before_threads = set(threading.enumerate())
    api = create_test_api(
        TestApiConfig(token=TestApiToken(SECRET), known_profiles=frozenset({"pepe"}))
    )
    assert isinstance(api.harness, ControlledApplication)
    assert api.status_source.snapshot() == {}
    with pytest.raises(RuntimeError, match="not running"):
        _ = api.address
    assert set(tracking.outstanding()) == before_paths
    assert set(threading.enumerate()) == before_threads


def test_real_events_support_concurrent_replay_and_reconnect(integrated):
    setup = scenario_by_id("stop-cleanup").setup()
    api, _ = integrated(setup)
    record = submit(api, setup.spec)
    frames = assert_stream(api, record)
    events = [json.loads(frame["data"]) for frame in frames]
    for phase in record.report.phases:
        states = [e["data"]["state"] for e in events if e["phase"] == phase.phase.value]
        assert states == ["running", phase.outcome.value]
    with ThreadPoolExecutor(max_workers=2) as readers:
        replies = list(readers.map(lambda _: stream_events(api, record.run_id), range(2)))
    assert replies == [frames, frames]
    cursor = int(frames[2]["id"])
    assert stream_events(api, record.run_id, last_event_id=cursor) == frames[3:]
    assert stream_events(api, record.run_id, last_event_id=int(frames[-1]["id"])) == []


@pytest.fixture
def held_start(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = StackTestHarness._phase_start

    def hold(self, context):
        result = original(self, context)
        entered.set()
        assert release.wait(timeout=10), "the integration test did not release the harness"
        return result

    monkeypatch.setattr(StackTestHarness, "_phase_start", hold)
    yield entered, release
    release.set()


def test_shutdown_timeout_preserves_harness_cleanup(integrated, held_start):
    entered, release = held_start
    setup = scenario_by_id("stop-cleanup").setup()
    api, _ = integrated(setup)
    response = Client(api).post("/runs", body=run_spec_payload(setup.spec))
    assert response.status == 202
    run_id = response.json()["run_id"]
    assert entered.wait(timeout=5)
    assert len(setup.world.managed_residual()) >= 2
    address = api.address
    try:
        with pytest.raises(ShutdownTimeout):
            api.stop(timeout=0.01)
        assert api.address == address and not port_is_free(*address)
        assert api.scheduler.busy and setup.world.root.exists()
    finally:
        release.set()
    assert wait_for_run(api, run_id).state is RunState.PASSED
    assert setup.world.managed_residual() == () and not setup.world.root.exists()
    api.stop(timeout=5)
    assert port_is_free(*address) and not api.scheduler.busy


def test_phase_failure_is_reported_and_cleanup_still_runs(integrated, monkeypatch):
    setup = scenario_by_id("stop-cleanup").setup()

    def fail(self, context):
        assert self.world.managed_residual()
        raise RuntimeError("integration phase failure")

    monkeypatch.setattr(StackTestHarness, "_phase_stop", fail)
    api, _ = integrated(setup)
    record = submit(api, setup.spec)
    assert record.state is RunState.FAILED
    assert (
        next(p for p in record.report.phases if p.phase is Phase.CLEANUP).outcome
        is PhaseOutcome.PASSED
    )
    assert setup.world.managed_residual() == () and setup.world.external_lost() == ()
    assert not setup.world.root.exists()
    frames = assert_stream(api, record)
    failures = [json.loads(f["data"]) for f in frames if "integration phase failure" in f["data"]]
    assert failures and failures[0]["data"]["state"] == "failed"
    assert Client(api).get(f"/runs/{record.run_id}").json()["state"] == "failed"


def test_sse_disconnect_during_real_run_does_not_own_its_lifetime(integrated, held_start):
    entered, release = held_start
    setup = scenario_by_id("stop-cleanup").setup()
    api, _ = integrated(setup)
    response = Client(api).post("/runs", body=run_spec_payload(setup.spec))
    run_id = response.json()["run_id"]
    assert entered.wait(timeout=5)
    connection, stream = open_stream(api, run_id)
    first = parse_frames(stream, limit=2)
    stream.close()
    connection.close()
    assert api.scheduler.busy and setup.world.managed_residual()
    release.set()
    record = wait_for_run(api, run_id)
    assert record.state is RunState.PASSED
    replay = stream_events(api, run_id, last_event_id=int(first[-1]["id"]))
    assert replay[-1]["event"] == "result"
    assert setup.world.managed_residual() == () and not setup.world.root.exists()


def test_status_reads_detached_controlled_observations(integrated, monkeypatch):
    setup = scenario_by_id("invalid-workflow").setup()
    api, harnesses = integrated(setup)
    initial = Client(api).get("/status").json()
    assert all(initial[key] == {} for key in ("system", "gpu", "services", "models"))
    assert harnesses == []
    record = submit(api, setup.spec)
    before = (
        list(setup.world.artifacts.requests),
        list(setup.world.ollama.pulls),
        setup.world.inventory.pids(),
    )

    def refuse(*args, **kwargs):
        raise AssertionError("status tried to run a harness")

    monkeypatch.setattr(api.harness, "run", refuse)
    monkeypatch.setattr(api.harness, "_factory", refuse)
    for _ in range(2):
        status = Client(api).get("/status").json()
        assert status["system"]["provider_mode"] == "controlled"
        assert status["gpu"]["devices"]
        assert status["services"]["managed_residual"] == []
        assert status["models"]["observation"] == "resolution-before-cleanup"
        assert len(status["models"]["roles"]) == 4
        assert status["system"]["run_id"] == record.run_id
        assert SECRET not in json.dumps(status)
    detached = api.status_source.snapshot()
    detached["system"]["os"] = "mutated by reader"
    assert api.status_source.snapshot()["system"]["os"] != "mutated by reader"
    assert before == (
        setup.world.artifacts.requests,
        setup.world.ollama.pulls,
        setup.world.inventory.pids(),
    )
    assert not setup.world.root.exists()


def test_security_gates_still_precede_real_harness_allocation(integrated):
    api, harnesses = integrated()
    client = Client(api)
    for path in ("/health", "/status", "/unknown", "/runs", "/runs/bad/events"):
        assert client.get(path, token=None).status == 401
        assert client.get(path, token="wrong").status == 401
    assert client.post("/unknown", body={}, token=None).status == 401
    assert client.get(f"/health?token={SECRET}", token=None).status == 401
    for key, value in {
        "shell": True,
        "command": "calc.exe",
        "executable": "cmd.exe",
        "path": "C:/outside",
        "root": "C:/outside",
        "pid": 1234,
        "url": "https://example.invalid",
        "adopt_external": True,
        "kill": True,
    }.items():
        assert client.create_run(**{key: value}).status == 400
    assert client.create_run(profile_id="unknown").status == 400
    for capability in (
        {"provider_mode": "local-real"},
        {"allow_network": True},
        {"allow_gpu": True},
    ):
        assert client.create_run(**capability).status == 403
    assert harnesses == []


def test_application_remains_fail_closed_for_unsupported_modes(integrated):
    api, harnesses = integrated()
    response = Client(api).create_run(root_mode="configured")
    record = wait_for_run(api, response.json()["run_id"])
    assert record.state is RunState.FAILED and harnesses == []
    assert "ephemeral roots only" in record.detail
    frames = stream_events(api, record.run_id)
    assert [f["event"] for f in frames].count("result") == 1
    assert json.loads(frames[-1]["data"])["data"]["report"]["state"] == "failed"
    from local_ai_stack.test_contracts import TestProviderMode as ProviderMode

    with pytest.raises(ValueError, match="local-real"):
        ControlledApplication().run(
            "unsupported",
            RunSpec(
                profile_id="pepe", phases=(Phase.PREFLIGHT,), provider_mode=ProviderMode.LOCAL_REAL
            ),
        )
    with pytest.raises(ValueError, match="local-real"):
        create_test_api(replace(api.config, allow_real_providers=True))
    assert harnesses == []


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.1", "localhost", "::1"])
def test_composed_application_is_still_loopback_only(host):
    api = create_test_api(
        TestApiConfig(token=TestApiToken(SECRET), known_profiles=frozenset({"pepe"}), host=host)
    )
    with pytest.raises(BindRefused):
        api.start()
    assert api.status_source.snapshot() == {} and not api.scheduler.busy
