"""Starting, serving several callers at once, and leaving nothing behind."""

from __future__ import annotations

import socket
import threading
from http.client import HTTPConnection

import pytest

from local_ai_stack.test_api import ShutdownTimeout, TestApi, TestApiConfig, TestApiToken
from local_ai_stack.test_api.server import API_PREFIX

from .conftest import (
    KNOWN_PROFILES,
    SECRET,
    Client,
    ScriptedHarness,
    gated_script,
    open_stream,
    parse_frames,
    report,
    wait_for_run,
)


def api_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith("test-api")
    ]


def port_is_free(host: str, port: int) -> bool:
    probe = socket.socket()
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# --- opt in -----------------------------------------------------------------


def test_nothing_listens_until_something_starts_it():
    """Importing the package binds no socket and starts no thread."""
    before = api_threads()
    config = TestApiConfig(token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES)
    api = TestApi(config, ScriptedHarness())
    assert api_threads() == before
    with pytest.raises(RuntimeError, match="not running"):
        _ = api.port

    api.start()
    try:
        assert api.port > 0
    finally:
        api.stop()


def test_starting_twice_is_refused(api):
    with pytest.raises(RuntimeError, match="already running"):
        api.start()


def test_the_server_can_be_used_as_a_context_manager():
    config = TestApiConfig(token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES)
    with TestApi(config, ScriptedHarness()) as api:
        host, port = api.address
        assert Client(api).get("/health").status == 200
    assert port_is_free(host, port)


# --- concurrency ------------------------------------------------------------


def test_readers_are_served_while_a_run_is_in_flight(serve):
    """Only mutation is serialized; reading is not."""
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)

    replies: list[int] = []
    lock = threading.Lock()

    def read(path: str) -> None:
        status = Client(api).get(path).status
        with lock:
            replies.append(status)

    readers = [
        threading.Thread(target=read, args=(path,), daemon=True)
        for path in ("/health", "/status", f"/runs/{run_id}", "/health", "/status")
    ]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join(timeout=10)

    assert replies == [200] * 5
    release.set()
    wait_for_run(api, run_id)


def test_many_creators_produce_exactly_one_run(serve):
    """The single-mutating-run rule holds under a race, not just in sequence."""
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))

    statuses: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def create() -> None:
        barrier.wait(timeout=10)
        status = Client(api).create_run().status
        with lock:
            statuses.append(status)

    creators = [threading.Thread(target=create, daemon=True) for _ in range(6)]
    for creator in creators:
        creator.start()
    for creator in creators:
        creator.join(timeout=15)

    assert statuses.count(202) == 1, statuses
    assert statuses.count(409) == 5, statuses

    release.set()
    active = api.store.active_run()
    if active is not None:
        wait_for_run(api, active.run_id)


def test_a_stream_and_a_request_share_the_server(serve):
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    client = Client(api)
    run_id = client.create_run().json()["run_id"]
    assert started.wait(timeout=10)

    connection, response = open_stream(api, run_id)
    frames: list[list[dict[str, str]]] = []
    reader = threading.Thread(target=lambda: frames.append(parse_frames(response)), daemon=True)
    reader.start()
    try:
        assert client.get("/status").json()["active_run"]["run_id"] == run_id
    finally:
        release.set()
    reader.join(timeout=10)
    connection.close()
    assert frames and frames[0][-1]["event"] == "result"


# --- cleanup ----------------------------------------------------------------


def test_stopping_releases_the_port_and_the_threads():
    before = set(api_threads())
    config = TestApiConfig(token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES)
    api = TestApi(config, ScriptedHarness())
    api.start()
    host, port = api.address
    run_id = Client(api).create_run().json()["run_id"]
    wait_for_run(api, run_id)

    api.stop()

    assert port_is_free(host, port), "the socket was closed, not merely abandoned"
    assert set(api_threads()) <= before, "no serving or run thread outlived the server"
    with pytest.raises(RuntimeError, match="not running"):
        _ = api.address


def test_stopping_is_idempotent():
    before = set(api_threads())
    config = TestApiConfig(token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES)
    api = TestApi(config, ScriptedHarness())
    api.start()
    api.stop()
    api.stop()
    assert set(api_threads()) <= before


def test_stopping_while_a_reader_is_attached_does_not_hang(serve):
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    run_id = Client(api).create_run().json()["run_id"]
    assert started.wait(timeout=10)

    connection, response = open_stream(api, run_id)
    reader = threading.Thread(target=lambda: parse_frames(response), daemon=True)
    reader.start()

    release.set()
    wait_for_run(api, run_id)
    api.stop(timeout=5)
    reader.join(timeout=5)
    connection.close()
    assert not reader.is_alive()


def test_a_stopped_server_answers_nothing():
    config = TestApiConfig(token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES)
    api = TestApi(config, ScriptedHarness())
    api.start()
    host, port = api.address
    api.stop()

    connection = HTTPConnection(host, port, timeout=2)
    with pytest.raises(OSError):
        connection.request("GET", f"{API_PREFIX}/health")
        connection.getresponse()


# --- stopping with a run still going ----------------------------------------


def test_stop_refuses_to_claim_a_shutdown_it_could_not_finish(serve):
    """The blocker: stop() used to return while the harness was still working."""
    before = set(api_threads())
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    host, port = api.address
    run_id = Client(api).create_run().json()["run_id"]
    assert started.wait(timeout=10)

    with pytest.raises(ShutdownTimeout, match="still"):
        api.stop(timeout=0.2)

    # Nothing was released, and nothing was cut short.
    assert not port_is_free(host, port), "the socket is still held"
    assert api.address == (host, port), "the server does not pretend to be gone"
    assert api.store.get(run_id).active, "the run was not interrupted"
    assert set(api_threads()) > before, "the worker is still there, by design"

    release.set()
    finished = wait_for_run(api, run_id)
    assert finished.state.value == "passed", "the run finished on its own terms"

    api.stop(timeout=10)

    assert port_is_free(host, port)
    assert set(api_threads()) <= before
    with pytest.raises(RuntimeError, match="not running"):
        _ = api.address


def test_a_refused_stop_lets_the_harness_finish_its_work(serve):
    """No thread is killed, so whatever the harness was doing still completes."""
    started, release = threading.Event(), threading.Event()
    done: list[str] = []

    def slow(run_id, spec, emit):
        started.set()
        release.wait(timeout=10)
        done.append("finished")
        return report(run_id, spec)

    api = serve(ScriptedHarness(slow))
    run_id = Client(api).create_run().json()["run_id"]
    assert started.wait(timeout=10)

    with pytest.raises(ShutdownTimeout):
        api.stop(timeout=0.2)
    assert done == [], "still in the middle of it"

    release.set()
    wait_for_run(api, run_id)
    assert done == ["finished"]
    assert api.store.get(run_id).report is not None

    api.stop(timeout=10)


def test_a_refused_stop_has_already_stopped_accepting_requests(serve):
    """What did happen is that no new work can arrive while it winds down."""
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    Client(api).create_run()
    assert started.wait(timeout=10)

    with pytest.raises(ShutdownTimeout):
        api.stop(timeout=0.2)

    with pytest.raises(OSError):
        Client(api, timeout=0.4).get("/health")

    release.set()
    api.stop(timeout=10)


def test_stopping_a_second_time_after_the_run_ends_completes_the_shutdown(serve):
    before = set(api_threads())
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    host, port = api.address
    Client(api).create_run()
    assert started.wait(timeout=10)

    for _ in range(3):
        with pytest.raises(ShutdownTimeout):
            api.stop(timeout=0.1)
        assert not port_is_free(host, port)

    release.set()
    api.stop(timeout=10)

    assert port_is_free(host, port)
    assert set(api_threads()) <= before
    api.stop(timeout=1)  # and it stays stopped


def test_a_stop_with_no_run_in_flight_still_finishes_in_one_call(serve):
    before = set(api_threads())
    api = serve()
    host, port = api.address
    run_id = Client(api).create_run().json()["run_id"]
    wait_for_run(api, run_id)

    api.stop(timeout=10)
    assert port_is_free(host, port)
    assert set(api_threads()) <= before
