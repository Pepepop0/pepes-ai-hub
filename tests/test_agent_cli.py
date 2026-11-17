"""The ``hub`` CLI an agent drives: JSON shapes, exit codes and the single-writer lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from test_installation_switching import PROJECT_ROOT, World, custom, hub, install

from local_ai_stack import desktop
from local_ai_stack.cli import main
from local_ai_stack.desktop.agent import hub_run, hub_state
from local_ai_stack.desktop.api import COMMANDS
from local_ai_stack.desktop.instance_lock import InstanceLock
from local_ai_stack.layout import ENV_ROOT


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """The default state folder, and so the lock, live under tmp_path."""
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "hub-root"))
    return tmp_path


@pytest.fixture
def installed(isolated):
    """A Hub whose state knows one installation; each factory call reopens it."""
    world = World()
    root = isolated / "installed"
    adapter = hub(isolated, world)
    install(adapter, custom(root))
    adapter.close()
    return world, root, lambda: hub(isolated, world)


@pytest.fixture
def window(isolated):
    """The desktop window's lock, held as an open window holds it."""
    lock = InstanceLock()
    assert lock.acquire()
    yield lock
    lock.release()


def lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


# --- the lock: one writer, readers always --------------------------------------------


def test_a_mutating_command_is_refused_while_the_window_is_open(window, installed, capsys):
    world, _, _ = installed
    calls = list(world.calls)

    assert main(["hub", "run", "start"]) == 1
    (reply,) = lines(capsys)
    assert reply["ok"] is False
    assert reply["code"] == "desktop_running"
    assert world.calls == calls, "nothing reached the backend"


def test_read_only_commands_still_work_while_the_window_is_open(window, installed, capsys):
    _, root, factory = installed

    assert hub_run("plan_switch", json.dumps({"root": str(root)}), app_factory=factory) == 0
    (reply,) = lines(capsys)
    assert reply["ok"] is True
    assert hub_state(app_factory=factory) == 0
    state = json.loads(capsys.readouterr().out)
    assert [row["root"] for row in state["installations"]] == [str(root)]


@pytest.fixture
def refused_startup(monkeypatch):
    """A desktop that got past the lock fails fast instead of serving forever."""

    def started(_root):
        raise RuntimeError("the desktop started although the lock was held")

    monkeypatch.setattr(desktop, "create_hub_application", started)


def test_a_window_cannot_open_while_a_cli_command_is_changing_the_hub(
    installed, refused_startup, capsys
):
    _, _, factory = installed
    refused = []

    def factory_observing_a_window():
        refused.append(desktop.run_desktop(browser=True))
        return factory()

    assert hub_run("start", app_factory=factory_observing_a_window) == 0
    assert refused == [1]
    assert desktop.ALREADY_OPEN in capsys.readouterr().err
    lock = InstanceLock()
    assert lock.acquire(), "the command released the lock when it ended"
    lock.release()


def test_a_second_desktop_is_refused_and_a_crashed_one_releases_the_hub(
    isolated, refused_startup, monkeypatch, capsys
):
    # The holder dies through os._exit: no finally, no release, as in a crash.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "from local_ai_stack.desktop.instance_lock import InstanceLock\n"
                "lock = InstanceLock()\n"
                "assert lock.acquire()\n"
                "print('held', flush=True)\n"
                "sys.stdin.readline()\n"
                "os._exit(9)\n"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(PROJECT_ROOT / "src"), os.environ.get("PYTHONPATH")])
            ),
        },
    )
    try:
        assert holder.stdout.readline().strip() == "held"

        assert desktop.run_desktop(browser=True, open_browser=False) == 1
        captured = capsys.readouterr()
        assert desktop.ALREADY_OPEN in captured.err
        assert captured.out == "", "a refused instance prints no URL"

        dialogs = []
        monkeypatch.setattr(
            desktop, "_message_box", lambda message, flags: dialogs.append(message) or 1
        )
        # A Hub window on screen is brought forward instead of refusing.
        focused = []
        monkeypatch.setattr(
            desktop, "focus_existing_window", lambda title: focused.append(title) or True
        )
        assert desktop.run_desktop() == 0
        assert (focused, dialogs) == ([desktop.PRODUCT_NAME], [])
        # The lock held by a command, with no window to show, is still refused.
        monkeypatch.setattr(desktop, "focus_existing_window", lambda title: False)
        assert desktop.run_desktop() == 1
        assert dialogs == [desktop.ALREADY_OPEN]
    finally:
        holder.communicate("\n", timeout=30)
    assert holder.returncode == 9

    lock = InstanceLock()
    # Windows releases a dead process's locks promptly, but not synchronously.
    deadline = time.monotonic() + 10
    while not lock.acquire():
        assert time.monotonic() < deadline, "the OS released the crashed process's lock"
        time.sleep(0.05)
    lock.release()


# --- JSON shapes and exit codes --------------------------------------------------


@pytest.mark.parametrize(
    "argv,code",
    [
        (["hub", "run", "format_disk"], "unknown_command"),
        (["hub", "run", "choose_folder"], "interactive_only"),
        (
            ["hub", "run", "install_git", "--args", '{"confirmation": "install-git"}'],
            "interactive_only",
        ),
        (["hub", "run", "plan", "--args", "{not json"], "invalid_args"),
        (["hub", "run", "plan", "--args", "[1, 2]"], "invalid_args"),
        (["hub", "run", "start", "--args", '{"root": "C:/x"}'], "invalid_args"),
        (["hub", "run", "plan", "--args", '{"profile_id": "pepe"}'], "invalid_args"),
    ],
)
def test_usage_errors_are_json_and_exit_2(isolated, capsys, argv, code):
    assert main(argv) == 2
    (reply,) = lines(capsys)
    assert reply["ok"] is False
    assert reply["code"] == code
    assert isinstance(reply["error"], str) and reply["error"]


def test_the_controller_keeps_its_safety_checks_and_refusals_exit_1(installed, capsys):
    world, root, factory = installed
    args = json.dumps({"root": str(root), "confirmation": "yes"})

    assert hub_run("delete", args, app_factory=factory) == 1
    (reply,) = lines(capsys)
    assert reply == {"ok": False, "error": reply["error"], "code": "confirmation_required"}
    assert root.is_dir()
    assert "delete" not in " ".join(world.commands_on(root))


def test_commands_are_listed_from_the_allowlist(isolated, capsys):
    assert main(["hub", "commands"]) == 0
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)["commands"]}
    assert rows.keys() == COMMANDS.keys()
    assert rows["plan"]["read_only"] is True and rows["plan"]["cli"] is True
    assert rows["delete"]["required"] == ["confirmation", "root"]
    assert rows["delete"]["optional"] == ["stop"]
    assert rows["delete"]["read_only"] is False
    assert rows["choose_folder"]["cli"] is False
    assert rows["choose_folder"]["unavailable_reason"]
    assert rows["install_git"]["cli"] is False
    assert rows["install_git"]["unavailable_reason"]


# --- following an operation --------------------------------------------------------


def test_wait_streams_the_operation_and_exits_0_when_it_succeeds(installed, capsys):
    world, root, factory = installed

    assert hub_run("start", wait=True, app_factory=factory) == 0
    reply, *events = lines(capsys)
    operation_id = reply["result"]["operation_id"]
    assert events and all(event["operation_id"] == operation_id for event in events)
    assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)
    assert (events[-1]["kind"], events[-1]["state"]) == ("result", "succeeded")
    assert "start_stack" in world.commands_on(root)


def test_wait_exits_1_when_the_operation_fails(installed, tmp_path, capsys):
    world, _, factory = installed
    broken = tmp_path / "broken"
    world.failing.add(broken)
    args = json.dumps(
        {
            "profile_id": "pepe",
            "mode": "custom",
            "custom_root": str(broken),
            "create_subfolder": False,
        }
    )

    assert hub_run("install", args, wait=True, app_factory=factory) == 1
    reply, *events = lines(capsys)
    assert reply["ok"] is True
    assert events[-1]["state"] == "failed"
    assert events[-1]["result"]["diagnostics"]
