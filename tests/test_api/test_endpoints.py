"""The five routes: what they accept, what they answer, what they refuse."""

from __future__ import annotations

import threading

import pytest

from local_ai_stack.test_api.payloads import MAX_BODY_BYTES, run_report_payload
from local_ai_stack.test_api.server import API_PREFIX

from .conftest import (
    Client,
    ScriptedHarness,
    exploding_script,
    failing_script,
    gated_script,
    passing_script,
    wait_for_run,
)

RUN_ID = "a" * 32


# --- surface ----------------------------------------------------------------


def test_the_surface_is_the_five_routes_the_contract_names(client):
    assert client.get("/health").status == 200
    assert client.get("/status").status == 200
    assert client.create_run().status == 202
    # The two run routes are covered below; nothing else exists.
    for path in ("/plan", "/start", "/stop", "/services", "/models", "/runs/current"):
        assert client.get(path).status in {404, 405}, path


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def test_no_other_method_is_offered(client, method):
    reply = client.request(method, "/runs")
    assert reply.status == 405


def test_no_cors_header_is_ever_sent(client):
    reply = client.get("/health")
    assert not any(name.lower().startswith("access-control") for name in reply.headers)


def test_the_server_does_not_announce_its_python(client):
    reply = client.get("/health")
    assert "Python" not in reply.headers.get("Server", "")
    assert reply.headers.get("Cache-Control") == "no-store"


# --- status -----------------------------------------------------------------


def test_status_reports_the_server_and_its_runs(client, api):
    body = client.get("/status").json()
    assert body["server"]["state"] == "running"
    assert body["server"]["port"] == api.port
    assert body["server"]["real_providers_enabled"] is False
    assert body["active_run"] is None
    assert set(body) >= {"server", "runs", "active_run", "system", "gpu", "services", "models"}


def test_status_shows_the_run_that_is_in_flight(serve):
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)
    try:
        body = client.get("/status").json()
        assert body["active_run"]["run_id"] == run_id
        assert body["active_run"]["state"] == "running"
        assert body["runs"]["running"] == 1
    finally:
        release.set()
    wait_for_run(api, run_id)
    assert client.get("/status").json()["active_run"] is None


def test_status_sanitizes_whatever_a_source_hands_it(serve):
    class LeakySource:
        def snapshot(self):
            return {
                "system": {"root": r"C:\Users\Pedro\AppData\Local", "token": "s3cret"},
                "services": {"ollama": "http://127.0.0.1:11434", "telemetry": "https://api.x.io"},
            }

    api = serve()
    api.status_source = LeakySource()
    body = Client(api).get("/status").json()
    assert body["system"]["root"] == "[redacted]"
    assert body["system"]["token"] == "[redacted]"
    assert body["services"]["ollama"] == "http://127.0.0.1:11434", "a local address is the answer"
    assert body["services"]["telemetry"] == "[redacted]"


# --- creating a run ---------------------------------------------------------


def test_a_run_is_accepted_and_addressable(client, harness):
    reply = client.create_run(phases=["preflight", "plan"])
    assert reply.status == 202
    body = reply.json()
    assert body["state"] == "queued"
    assert body["run"] == f"{API_PREFIX}/runs/{body['run_id']}"
    assert body["events"] == f"{API_PREFIX}/runs/{body['run_id']}/events"

    wait_for_run(client.api, body["run_id"])
    assert [spec.profile_id for _, spec in harness.calls] == ["pepe"]
    assert [phase.value for phase in harness.calls[0][1].phases] == ["preflight", "plan"]


def test_a_finished_run_reports_what_the_harness_returned(client):
    run_id = client.create_run().json()["run_id"]
    wait_for_run(client.api, run_id)

    body = client.get(f"/runs/{run_id}").json()
    assert body["state"] == "passed"
    assert body["report"]["state"] == "passed"
    assert [phase["phase"] for phase in body["report"]["phases"]] == ["preflight"]
    assert body["started_at"] and body["finished_at"]
    assert body["diagnostics"]["events_emitted"] >= 1


def test_a_failing_run_is_reported_as_failed(serve):
    api = serve(ScriptedHarness(failing_script("preflight said no")))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    wait_for_run(api, run_id)

    body = client.get(f"/runs/{run_id}").json()
    assert body["state"] == "failed"
    assert body["report"]["detail"] == "preflight said no"
    assert body["report"]["phases"][0]["outcome"] == "failed"


def test_a_harness_that_raises_still_produces_a_failed_run(serve):
    api = serve(ScriptedHarness(exploding_script(RuntimeError("the disk went away"))))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    wait_for_run(api, run_id)

    body = client.get(f"/runs/{run_id}").json()
    assert body["state"] == "failed"
    assert "RuntimeError" in body["report"]["detail"]
    assert "the disk went away" in body["report"]["detail"]


def test_only_one_run_may_be_in_flight(serve):
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    client = Client(api)
    first = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)
    try:
        second = client.create_run()
        assert second.status == 409
        assert second.json()["error"] == "run-in-progress"
    finally:
        release.set()
    wait_for_run(api, first)
    assert client.create_run().status == 202, "the next run is accepted once the first ends"


# --- reading a run ----------------------------------------------------------


def test_an_unknown_run_is_not_found(client):
    assert client.get(f"/runs/{RUN_ID}").status == 404
    assert client.get(f"/runs/{RUN_ID}/events").status == 404


@pytest.mark.parametrize(
    "run_id",
    [
        "..",
        "../../etc/passwd",
        "..%2f..%2fwindows",
        "C:\\Windows\\System32",
        "a" * 31,
        "a" * 33,
        "A" * 32,
        "g" * 32,
        "run-1",
        "",
    ],
)
def test_a_run_id_that_could_be_a_path_is_not_a_run_id(client, run_id):
    """The identifier is opaque; there is nothing in it to traverse."""
    assert client.get(f"/runs/{run_id}").status in {404, 405}


# --- request validation -----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"phases": ["preflight"]},
        {"profile_id": "pepe"},
        {"profile_id": "", "phases": ["preflight"]},
        {"profile_id": "pepe", "phases": []},
        {"profile_id": "pepe", "phases": "preflight"},
        {"profile_id": "pepe", "phases": ["preflight", "preflight"]},
        {"profile_id": "pepe", "phases": ["not-a-phase"]},
        {"profile_id": "pepe", "phases": [1]},
        {"profile_id": "unknown-profile", "phases": ["preflight"]},
        {"profile_id": "pepe", "phases": ["preflight"], "root_mode": "anywhere"},
        {"profile_id": "pepe", "phases": ["preflight"], "provider_mode": "cloud"},
        {"profile_id": "pepe", "phases": ["preflight"], "allow_network": "yes"},
        {"profile_id": "pepe", "phases": ["preflight"], "allow_gpu": 1},
        [1, 2, 3],
        "preflight",
        42,
    ],
)
def test_a_malformed_request_never_reaches_the_harness(client, harness, body):
    reply = client.post("/runs", body=body)
    assert reply.status == 400
    assert reply.json()["error"] == "invalid-request"
    assert harness.calls == []


def test_the_refusal_does_not_enumerate_the_profiles(client):
    reply = client.create_run(profile_id="nope")
    assert reply.status == 400
    assert "capella" not in reply.body.decode("utf-8")


def test_a_body_that_is_not_json_is_refused(client, harness):
    assert client.post("/runs", body="{not json").status == 400
    assert (
        client.post("/runs", body={"profile_id": "pepe"}, content_type="text/plain").status == 415
    )
    assert harness.calls == []


def test_an_oversized_body_is_refused_before_it_is_read(client, harness):
    padding = "x" * (MAX_BODY_BYTES + 100)
    reply = client.post("/runs", body={"profile_id": "pepe", "phases": [padding]})
    assert reply.status == 413
    assert harness.calls == []


def test_a_body_without_a_length_is_refused(client):
    reply = client.request(
        "POST",
        "/runs",
        body='{"profile_id": "pepe", "phases": ["preflight"]}',
        headers={"Transfer-Encoding": "chunked"},
    )
    assert reply.status == 411


# --- gates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_mode", "local-real"),
        ("allow_network", True),
        ("allow_gpu", True),
    ],
)
def test_a_capability_the_server_did_not_enable_is_refused(client, harness, field, value):
    reply = client.create_run(**{field: value})
    assert reply.status == 403
    assert reply.json()["error"] == "not-enabled"
    assert harness.calls == []


def test_a_capability_the_server_enabled_is_allowed(serve):
    harness = ScriptedHarness(passing_script())
    api = serve(harness, allow_real_providers=True, allow_network=True, allow_gpu=True)
    client = Client(api)
    reply = client.create_run(provider_mode="local-real", allow_network=True, allow_gpu=True)
    assert reply.status == 202
    wait_for_run(api, reply.json()["run_id"])
    _, spec = harness.calls[0]
    assert spec.provider_mode.value == "local-real"
    assert spec.allow_network and spec.allow_gpu


# --- equivalence ------------------------------------------------------------


def test_the_api_reports_exactly_what_the_harness_reported(serve):
    """The API has no authority to add a phase, an outcome, or a verdict."""
    harness = ScriptedHarness(passing_script())
    api = serve(harness)
    client = Client(api)

    reply = client.create_run(phases=["preflight", "plan", "cleanup"])
    run_id = reply.json()["run_id"]
    wait_for_run(api, run_id)
    through_api = client.get(f"/runs/{run_id}").json()["report"]

    _, spec = harness.calls[0]
    direct = run_report_payload(harness.run("direct", spec))

    assert through_api["state"] == direct["state"]
    assert through_api["phases"] == direct["phases"]
    assert through_api["spec"] == direct["spec"]
    assert through_api["detail"] == direct["detail"]
    assert through_api["run_id"] == run_id != direct["run_id"], "only the id differs"
