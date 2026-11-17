"""The desktop state stays true to the installation in use while probes are cached."""

from __future__ import annotations

import pytest
from test_installation_switching import World, custom, hub, install

from local_ai_stack.desktop import api
from local_ai_stack.desktop.api import DesktopController


def test_state_answers_for_the_installation_switched_to_not_the_cached_probe(tmp_path, monkeypatch):
    # The observer would otherwise refresh on its own and hide a stale answer.
    monkeypatch.setattr(api, "OBSERVE_INTERVAL_SECONDS", 3600)
    world = World()
    app = hub(tmp_path, world)
    first, second = tmp_path / "first", tmp_path / "second"
    install(app, custom(first))
    install(app, custom(second, "pepe-8gb"))
    app.switch_installation(first)
    controller = DesktopController(app, folder_picker=object())
    try:
        before = controller.state()
        assert before["installations"][0]["root"] == str(first)
        assert before["installations"][0]["status"] != "running"

        world.running.add(second)
        app.switch_installation(second)
        after = controller.state()

        active = after["installations"][0]
        assert (active["root"], active["active"], active["status"]) == (
            str(second),
            True,
            "running",
        )
        assert after["installation"]["profile_id"] == "pepe-8gb"
        assert {item["health"] for item in after["services"]["services"]} == {"UP"}
    finally:
        controller.close()


def test_state_tells_the_installed_profile_version_from_the_one_the_hub_ships(tmp_path):
    world = World()
    app = hub(tmp_path, world)
    install(app, custom(tmp_path / "only"))
    controller = DesktopController(app, folder_picker=object())
    try:
        state = controller.state()

        shipped = {profile.id: profile.version for profile in app.profiles}
        assert shipped["pepe"] == "1.2.0"
        assert state["profiles"]
        assert all(item["version"] == shipped[item["id"]] for item in state["profiles"])
        row = state["installations"][0]
        assert (row["profile_version"], row["hub_profile_version"]) == ("1.2.0", "1.2.0")
    finally:
        controller.close()


def test_a_failing_probe_reports_the_hub_unavailable(tmp_path, monkeypatch):
    world = World()
    app = hub(tmp_path, world)
    install(app, custom(tmp_path / "only"))

    def broken(profile_id=None):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(app.backend, "status", broken)
    controller = DesktopController(app, folder_picker=object())
    try:
        with pytest.raises(api.HubUnavailable):
            controller.state()
    finally:
        controller.close()
