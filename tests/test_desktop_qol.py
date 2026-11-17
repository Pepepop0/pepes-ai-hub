"""Logs, disk usage, service addresses and window geometry, as the desktop sees them."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest
from test_installation_switching import World, custom, hub, install

from local_ai_stack.desktop import api
from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.desktop.window_state import (
    MIN_HEIGHT,
    MIN_WIDTH,
    WindowGeometry,
    load_geometry,
    save_geometry,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")


@pytest.fixture
def desktop(tmp_path):
    world = World()
    app = hub(tmp_path, world)
    root = tmp_path / "installed"
    install(app, custom(root))
    opened: list[str] = []
    controller = DesktopController(
        app, folder_opener=opened.append, url_opener=opened.append, folder_picker=object()
    )
    yield controller, world, root, opened
    controller.close()


def junction(link, target) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def write_log(root, service, lines, *, mtime=None):
    folder = root / "logs"
    folder.mkdir(exist_ok=True)
    path = folder / f"{service}.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def refused(controller, command, args) -> str:
    with pytest.raises(DesktopError) as caught:
        controller.command(command, args)
    return caught.value.code


# --- one service at a time ----------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "backend_call"),
    [
        ("service_stop", "stop_service:open-webui"),
        ("service_start", "start_service:open-webui"),
        ("service_restart", "restart_service:open-webui"),
    ],
)
def test_one_service_reaches_the_installation_in_use(desktop, command, backend_call):
    controller, world, root, _ = desktop

    reply = controller.command(command, {"service_id": "open-webui"})
    controller.app.wait(10)

    assert reply["operation_id"]
    assert world.commands_on(root)[-1] == backend_call


def test_an_unknown_service_is_refused_without_an_operation(desktop):
    controller, world, root, _ = desktop
    before = list(world.commands_on(root))

    assert refused(controller, "service_stop", {"service_id": "postgres"}) == "refused"
    assert world.commands_on(root) == before


# --- logs ---------------------------------------------------------------------------


def test_logs_tail_the_most_recent_service_by_default_and_any_listed_one(desktop):
    controller, _, root, _ = desktop
    write_log(root, "ollama", [f"ollama {index}" for index in range(300)], mtime=1_000)
    write_log(root, "comfyui", ["comfy ready"], mtime=2_000)

    latest = controller.command("logs", {"root": str(root)})
    assert [item["service"] for item in latest["files"]] == ["comfyui", "ollama"]
    assert (latest["service"], latest["lines"], latest["truncated"]) == (
        "comfyui",
        ["comfy ready"],
        False,
    )

    tail = controller.command("logs", {"root": str(root), "service": "ollama", "lines": 50})
    assert tail["lines"] == [f"ollama {index}" for index in range(250, 300)]
    assert tail["truncated"] is True


def test_a_log_larger_than_the_read_window_drops_its_cut_first_line(desktop, monkeypatch):
    controller, _, root, _ = desktop
    write_log(root, "ollama", ["x" * 30, "second", "third"])
    monkeypatch.setattr(api, "LOG_TAIL_MAX_BYTES", len("second\nthird\n") + 3)

    tail = controller.command("logs", {"root": str(root), "service": "ollama"})

    assert tail["lines"] == ["second", "third"]
    assert tail["truncated"] is True


def test_an_installation_without_logs_answers_empty(desktop):
    controller, _, root, _ = desktop
    result = controller.command("logs", {"root": str(root)})
    assert (result["files"], result["service"], result["lines"]) == ([], None, [])


@pytest.mark.parametrize(
    "args,code",
    [
        ({"service": "../state/config"}, "missing_log"),
        ({"service": "openwebui"}, "missing_log"),
        ({"lines": 0}, "invalid_request"),
        ({"lines": 1001}, "invalid_request"),
        ({"lines": True}, "invalid_request"),
    ],
)
def test_logs_read_only_listed_files_within_bounds(desktop, args, code):
    controller, _, root, _ = desktop
    write_log(root, "ollama", ["ready"])
    assert refused(controller, "logs", {"root": str(root), **args}) == code


def test_logs_and_measure_refuse_folders_the_hub_does_not_know(desktop, tmp_path):
    controller, _, _, _ = desktop
    stranger = tmp_path / "stranger"
    (stranger / "logs").mkdir(parents=True)
    assert refused(controller, "logs", {"root": str(stranger)}) == "unknown_installation"
    assert refused(controller, "measure", {"root": str(stranger)}) == "unknown_installation"


@windows_only
def test_a_logs_folder_that_leads_elsewhere_is_not_read(desktop, tmp_path):
    controller, _, root, _ = desktop
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_text("not the hub's", encoding="utf-8")
    junction(root / "logs", outside)
    assert refused(controller, "logs", {"root": str(root)}) == "unsafe_path"
    assert refused(controller, "open_folder", {"root": str(root), "kind": "logs"}) == "unsafe_path"


def test_logs_answer_while_another_command_is_running(desktop):
    controller, _, root, _ = desktop
    write_log(root, "ollama", ["ready"])
    with controller._commands:  # a slow command holds the command slot
        assert controller.command("logs", {"root": str(root)})["lines"] == ["ready"]
        assert refused(controller, "verify", {}) == "busy"


# --- disk usage -----------------------------------------------------------------------


@windows_only
def test_measure_counts_bytes_and_files_without_following_links(desktop, tmp_path):
    controller, _, root, _ = desktop
    before = controller.command("measure", {"root": str(root)})
    (root / "models").mkdir(exist_ok=True)
    (root / "models" / "weights.bin").write_bytes(b"\0" * 4096)
    (root / "apps").mkdir(exist_ok=True)
    (root / "apps" / "app.py").write_bytes(b"\0" * 100)
    outside = tmp_path / "huge"
    outside.mkdir()
    (outside / "elsewhere.bin").write_bytes(b"\0" * 1_000_000)
    junction(root / "models" / "linked", outside)

    after = controller.command("measure", {"root": str(root)})

    assert after["bytes"] - before["bytes"] == 4196
    assert after["files"] - before["files"] == 2
    assert after["partial"] is False


def test_measure_stops_at_its_deadline_and_says_so(desktop, monkeypatch):
    controller, _, root, _ = desktop
    monkeypatch.setattr(api, "MEASURE_DEADLINE_SECONDS", 0)
    result = controller.command("measure", {"root": str(root)})
    assert result["partial"] is True


# --- service addresses ----------------------------------------------------------------


def wait_for(probe, *, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def test_service_targets_follow_the_running_state(desktop):
    controller, world, root, opened = desktop
    stopped = controller.command("service_targets", {})
    assert {row["service_id"] for row in stopped} == {"ollama", "open-webui", "comfyui"}
    assert all(row["url"].startswith("http://127.0.0.1:") for row in stopped)
    assert {row["health"] for row in stopped} == {"DOWN"}
    assert refused(controller, "open_service", {"service_id": "comfyui"}) == "not_ready"
    assert refused(controller, "open_service", {"service_id": "ollama"}) == "refused"
    assert opened == []

    world.running.add(root)
    controller.command("verify", {})  # any command wakes the observer
    wait_for(
        lambda: all(row["health"] == "UP" for row in controller.command("service_targets", {}))
    )

    result = controller.command("open_service", {"service_id": "comfyui"})
    comfy = next(row for row in stopped if row["service_id"] == "comfyui")
    assert result == {"url": comfy["url"]}
    assert opened == [comfy["url"]]
    assert controller.managed_services_running() is True


# --- window geometry ------------------------------------------------------------------

SCREEN = (0, 0, 1920, 1080)


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", json.dumps({"width": "wide", "height": 900})],
)
def test_an_unusable_window_file_opens_with_defaults(tmp_path, content):
    path = tmp_path / "window.json"
    path.write_text(content, encoding="utf-8")
    assert load_geometry(path, SCREEN) == WindowGeometry()


def test_a_saved_window_comes_back_where_it_was(tmp_path):
    path = tmp_path / "window.json"
    save_geometry(WindowGeometry(1500, 950, 120, 80, True), path)
    assert load_geometry(path, SCREEN) == WindowGeometry(1500, 950, 120, 80, True)


def test_a_window_left_on_a_missing_screen_keeps_its_size_not_its_place(tmp_path):
    path = tmp_path / "window.json"
    save_geometry(WindowGeometry(1400, 900, 4000, 200), path)
    assert load_geometry(path, SCREEN) == WindowGeometry(1400, 900)


def test_a_tiny_saved_window_is_raised_to_the_minimum(tmp_path):
    path = tmp_path / "window.json"
    save_geometry(WindowGeometry(200, 100, 10, 10), path)
    restored = load_geometry(path, SCREEN)
    assert (restored.width, restored.height) == (MIN_WIDTH, MIN_HEIGHT)
