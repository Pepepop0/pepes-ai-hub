"""The command line that starts the server, and the ways it refuses to."""

from __future__ import annotations

import threading

import pytest

from local_ai_stack.test_api.__main__ import UnwiredHarness, build_parser, main
from local_ai_stack.test_api.auth import ENV_TOKEN

from .conftest import (
    SECRET,
    Client,
    Phase,
    RunSpec,
    ScriptedHarness,
    gated_script,
    open_stream,
)

SPEC = RunSpec(profile_id="pepe", phases=(Phase.PREFLIGHT,))


def test_a_profile_must_be_named():
    """Nothing is discovered from disk: the caller says which profiles exist."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    arguments = build_parser().parse_args(["--profile", "pepe", "--profile", "capella"])
    assert arguments.profile == ["pepe", "capella"]


def test_everything_dangerous_is_off_unless_asked_for():
    arguments = build_parser().parse_args(["--profile", "pepe"])
    assert arguments.host == "127.0.0.1"
    assert arguments.port == 0
    assert not arguments.allow_real_providers
    assert not arguments.allow_network
    assert not arguments.allow_gpu


def test_there_is_no_flag_that_takes_a_command_or_a_path():
    options = {action.dest for action in build_parser()._actions}
    assert options == {
        "help",
        "host",
        "port",
        "profile",
        "allow_real_providers",
        "allow_network",
        "allow_gpu",
    }


def test_without_a_token_nothing_starts(monkeypatch, capsys):
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv("PEPITOS_AI_HUB_TEST_TOKEN_FILE", raising=False)
    assert main(["--profile", "pepe"]) == 2
    assert ENV_TOKEN in capsys.readouterr().err


def test_a_public_bind_is_refused_at_the_command_line(monkeypatch, capsys):
    monkeypatch.setenv(ENV_TOKEN, SECRET)
    assert main(["--profile", "pepe", "--host", "0.0.0.0"]) == 2
    assert "loopback-only" in capsys.readouterr().err


def test_the_token_is_never_echoed(monkeypatch, capsys):
    """A started server prints its address, never its secret."""
    monkeypatch.setenv(ENV_TOKEN, SECRET)
    finished = threading.Event()

    def run() -> None:
        main(["--profile", "pepe"])
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    # main() blocks on the serving thread; stopping the test is enough, because
    # every thread it owns is a daemon. What matters is what it printed.
    worker.join(timeout=1.0)
    captured = capsys.readouterr().err
    assert "listening on http://127.0.0.1:" in captured
    assert SECRET not in captured


def test_the_placeholder_harness_fails_rather_than_pretends():
    report = UnwiredHarness().run("a" * 32, SPEC)
    assert report.state.value == "failed"
    assert "no test harness" in report.detail
    assert report.phases == ()


def test_an_abrupt_disconnect_is_not_a_server_error(serve):
    """A reader that vanishes mid-stream leaves nothing behind and logs nothing."""
    started, release = threading.Event(), threading.Event()
    api = serve(ScriptedHarness(gated_script(started, release)))
    run_id = Client(api).create_run().json()["run_id"]
    assert started.wait(timeout=10)

    connection, response = open_stream(api, run_id)
    response.readline()
    connection.close()

    release.set()
    api.store.wait_for_finish(run_id, timeout=10)
    assert api.handler_errors == []
    assert Client(api).get("/health").status == 200, "the server is still serving"
