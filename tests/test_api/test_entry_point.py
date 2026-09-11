"""The command line that starts the server, and the ways it refuses to."""

from __future__ import annotations

import threading

import pytest

from local_ai_stack.test_api import TestApi as Api
from local_ai_stack.test_api.__main__ import build_parser, main
from local_ai_stack.test_api.auth import ENV_TOKEN

from .conftest import (
    SECRET,
    Client,
    ScriptedHarness,
    gated_script,
    open_stream,
)


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
    monkeypatch.setattr(Api, "wait", lambda self: None)
    assert main(["--profile", "pepe"]) == 0
    captured = capsys.readouterr().err
    assert "listening on http://127.0.0.1:" in captured
    assert SECRET not in captured


def test_the_entry_point_executes_the_real_harness(monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, SECRET)
    reports = []

    def exercise(self):
        run_id = Client(self).create_run(phases=["preflight", "plan"]).json()["run_id"]
        record = self.store.wait_for_finish(run_id)
        reports.append(record.report)

    monkeypatch.setattr(Api, "wait", exercise)
    assert main(["--profile", "pepe"]) == 0
    assert reports[0].state.value == "passed"
    assert reports[0].context["provider_mode"] == "controlled"
    assert reports[0].context["root"]["removed"] is True


def test_real_providers_remain_unsupported_even_with_the_flag(monkeypatch, capsys):
    monkeypatch.setenv(ENV_TOKEN, SECRET)
    assert main(["--profile", "pepe", "--allow-real-providers"]) == 2
    assert "local-real providers are unsupported" in capsys.readouterr().err


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
