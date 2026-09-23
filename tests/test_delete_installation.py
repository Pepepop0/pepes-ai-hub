"""Deleting an installation: its folder, then its entry -- and never anything else.

Real folders in a temporary directory, the real adapter, and the stub backend
that speaks the production contract (``test_installation_switching``); one
test runs against the real runtime to prove an external process survives.
Every deletion here is of a folder the test created.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from hub_fakes import press_visible
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_delivery_wave_a import composition  # noqa: F401 -- the real composition fixture
from test_installation_switching import World, custom, hub, install
from textual.widgets import Button, OptionList, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.deletion import deletion_refusal
from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.installations import read_state
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.resources import checkout_root
from local_ai_stack.tui.application import HubError, InstallSelection
from local_ai_stack.tui.dialogs import DeleteInstallationScreen
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import InstallPanel


@pytest.fixture
def world() -> World:
    return World()


def two(tmp_path, world):
    """Two installations with real files: ``second`` in use, ``first`` stopped."""
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(first))
    install(adapter, custom(second, "pepe-8gb"))
    for root in (first, second):
        (root / "models").mkdir(exist_ok=True)
        (root / "models" / "weights.bin").write_bytes(b"x" * 1024)
    return adapter, first, second


def ending(adapter) -> OperationState:
    return adapter.current_operation().last_event.state


def delete(adapter, root, *, stop=False) -> None:
    adapter.delete_installation(root, stop=stop)
    adapter.wait(10)


# --- what is deleted, and what stays ------------------------------------------------------


def test_deleting_a_stopped_installation_removes_its_folder_then_its_entry(tmp_path, world):
    adapter, first, second = two(tmp_path, world)
    try:
        plan = adapter.plan_delete(first)
        assert plan.ok and not plan.must_stop and not plan.active
        assert plan.root == first and plan.profile_name == "Pepe"

        delete(adapter, first)

        assert ending(adapter) is OperationState.SUCCEEDED
        assert not first.exists()
        assert adapter.state.entry_for(first) is None
        assert read_state(adapter.selection_path).entry_for(first) is None
        # The other installation: untouched, still in use.
        assert (second / "models" / "weights.bin").is_file()
        assert adapter.active_root() == second
        assert read_state(adapter.selection_path).active_root == second
        assert tmp_path.exists(), "nothing above the folder is touched"
    finally:
        adapter.close()


def test_deleting_the_installation_in_use_leaves_nothing_in_use(tmp_path, world):
    adapter, first, second = two(tmp_path, world)
    try:
        assert adapter.plan_delete(second).active

        delete(adapter, second)

        assert ending(adapter) is OperationState.SUCCEEDED
        assert not second.exists()
        assert adapter.config is None and adapter.active_root() is None
        persisted = read_state(adapter.selection_path)
        assert persisted.active is None, "nothing is chosen in its place"
        assert persisted.entry_for(first) is not None and persisted.entry_for(second) is None
        assert (first / "models" / "weights.bin").is_file()
        assert not adapter.installation().ready
    finally:
        adapter.close()


def test_a_running_installation_is_stopped_only_when_asked_then_deleted(tmp_path, world):
    adapter, _, second = two(tmp_path, world)
    world.running.add(second)
    try:
        plan = adapter.plan_delete(second)
        assert plan.ok and plan.must_stop
        with pytest.raises(HubError):
            adapter.delete_installation(second)  # not asked to stop
        assert second.exists() and world.commands_on(second).count("stop_stack") == 0

        delete(adapter, second, stop=True)

        assert "stop_stack" in world.commands_on(second)
        assert second not in world.running
        assert not second.exists()
        assert adapter.active_root() is None
    finally:
        adapter.close()


def test_a_stop_that_leaves_its_processes_alive_deletes_nothing(tmp_path, world):
    adapter, _, second = two(tmp_path, world)
    world.running.add(second)
    world.survivors.add(second)
    try:
        delete(adapter, second, stop=True)

        assert ending(adapter) is OperationState.FAILED
        assert (second / "models" / "weights.bin").is_file(), "nothing was deleted"
        assert adapter.state.entry_for(second) is not None
        assert adapter.active_root() == second
    finally:
        adapter.close()


@pytest.mark.skipif(sys.platform != "win32", reason="an open file blocks deletion on Windows")
def test_a_deletion_that_fails_keeps_the_entry_and_says_so(tmp_path, world):
    adapter, first, second = two(tmp_path, world)
    try:
        with open(first / "models" / "weights.bin", "rb"):
            delete(adapter, first)
            assert ending(adapter) is OperationState.FAILED
            message = adapter.current_operation().last_event.message
            assert "continua na lista" in message
        assert adapter.state.entry_for(first) is not None
        assert read_state(adapter.selection_path).entry_for(first) is not None
        assert (second / "models" / "weights.bin").is_file()
    finally:
        adapter.close()


def test_forgetting_still_deletes_nothing(tmp_path, world):
    adapter, first, _ = two(tmp_path, world)
    try:
        adapter.forget_installation(first)
        assert adapter.state.entry_for(first) is None
        assert (first / "models" / "weights.bin").is_file()
        assert (first / "state" / "config.yaml").is_file()
    finally:
        adapter.close()


# --- what is never deleted -------------------------------------------------------------------


def test_dangerous_folders_are_refused_by_path_alone(tmp_path):
    state_file = tmp_path / "hub" / "state" / "hub-selection.yaml"
    home = Path.home()
    anchor = Path(home.anchor)
    for folder in (
        anchor,
        home,
        home.parent,
        home / "Desktop",
        home / "Documents",
        checkout_root(),
        checkout_root().parent,
        tmp_path / "hub",  # holds the Hub's own state
        Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))),
    ):
        assert deletion_refusal(folder, state_file=state_file) is not None, folder
    assert deletion_refusal(Path("relative/folder"), state_file=state_file) is not None
    # Another installation inside it, or it inside another installation.
    outer, inner = tmp_path / "outer", tmp_path / "outer" / "inner"
    assert deletion_refusal(outer, state_file=state_file, other_installations=[inner])
    assert deletion_refusal(inner, state_file=state_file, other_installations=[outer])
    # And an ordinary installation folder is not refused.
    assert deletion_refusal(tmp_path / "PEPITO", state_file=state_file) is None


def test_the_adapter_refuses_a_dangerous_or_unknown_folder_before_touching_anything(
    tmp_path, world
):
    adapter, first, second = two(tmp_path, world)
    try:
        # Real folders are only ever judged, never handed to a deletion -- even a
        # refused one: a broken guard must not be able to cost anything here.
        for folder in (Path.home(), Path.home() / "Desktop", checkout_root()):
            assert not adapter.plan_delete(folder).ok, folder
        # The folder holding both installations: refused, and harmless if it were not.
        assert not adapter.plan_delete(tmp_path).ok
        with pytest.raises(HubError):
            adapter.delete_installation(tmp_path)
        stray = tmp_path / "stray"
        stray.mkdir()
        (stray / "keep.txt").write_text("keep", encoding="utf-8")
        assert not adapter.plan_delete(stray).ok, "not an installation the Hub knows"
        assert (stray / "keep.txt").is_file()
        assert first.exists() and second.exists()
    finally:
        adapter.close()


def test_a_known_folder_without_the_hubs_configuration_is_refused(tmp_path, world):
    adapter, first, _ = two(tmp_path, world)
    try:
        (first / "state" / "config.yaml").unlink()
        plan = adapter.plan_delete(first)
        assert not plan.ok and "configuração" in plan.refusal
        with pytest.raises(HubError):
            adapter.delete_installation(first)
        assert (first / "models" / "weights.bin").is_file()
        assert adapter.state.entry_for(first) is not None
    finally:
        adapter.close()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows construct")
def test_a_link_inside_the_folder_refuses_the_whole_deletion(tmp_path, world):
    import _winapi

    adapter, first, _ = two(tmp_path, world)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("precious", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(first / "models" / "linked"))
    try:
        plan = adapter.plan_delete(first)
        assert not plan.ok and "atalho" in plan.refusal
        with pytest.raises(HubError):
            adapter.delete_installation(first)
        assert (outside / "precious.txt").read_text(encoding="utf-8") == "precious"
        assert (first / "models" / "weights.bin").is_file()
    finally:
        os.rmdir(first / "models" / "linked")  # the junction itself, not its target
        adapter.close()


# --- against the real runtime: an external process is never killed -------------------------


def test_stopping_to_delete_stops_our_processes_and_never_an_external_one(composition):  # noqa: F811
    world, adapter, _ = composition()
    first, second = world.root.path / "first", world.root.path / "second"
    adapter.install(InstallSelection("capella", InstallLocationMode.CUSTOM, str(first)))
    adapter.wait(30)
    adapter.stop_stack()
    adapter.wait(30)
    adapter.install(InstallSelection("capella", InstallLocationMode.CUSTOM, str(second)))
    adapter.wait(30)
    assert adapter.active_root() == second
    assert adapter.backend.status("capella").stack_up
    world.inventory.occupy(39999, pid=9876)  # somebody else's process
    ours = set(world.launcher.spawned)

    plan = adapter.plan_delete(second)
    assert plan.ok and plan.must_stop
    adapter.delete_installation(second, stop=True)
    adapter.wait(30)

    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert not second.exists()
    assert 9876 in world.inventory.pids(), "the external process is alive"
    assert not ours & set(world.inventory.pids()), "none of ours is"
    assert first.exists() and adapter.state.entry_for(first) is not None
    assert adapter.active_root() is None


# --- the screens ---------------------------------------------------------------------------


def shell(adapter) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        installed_config=adapter.config,
        application=adapter,
        open_browser=lambda url: True,
        first_run_wizard=True,
    )


async def settle(pilot, times: int = 6) -> None:
    await pilot.app.workers.wait_for_complete()
    for _ in range(times):
        await pilot.pause()


press = press_visible


async def select_row(pilot, root: Path) -> InstallPanel:
    pilot.app.show_route(Section.INSTALLATION, "installations")
    await settle(pilot, 3)
    panel = pilot.app.query_one(InstallPanel)
    listing = panel.query_one("#install-known-list", OptionList)
    listing.focus()
    await pilot.pause()
    for _ in range(listing.option_count):
        if panel.selected_root == root:
            break
        await pilot.press("down")
        await pilot.pause()
    assert panel.selected_root == root
    return panel


def test_the_confirmation_names_the_profile_and_the_full_path_and_cancel_keeps_everything(
    tmp_path, world
):
    adapter, first, _ = two(tmp_path, world)
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await select_row(pilot, first)
            await press(pilot, "#install-delete")
            await settle(pilot, 3)
            dialog = app.screen
            assert isinstance(dialog, DeleteInstallationScreen)
            body = str(dialog.query_one("#delete-body", Static).content)
            assert "Pepe" in body and str(first) in body
            assert "apagará permanentemente" in body
            assert str(dialog.query_one("#delete-confirm", Button).label) == "Apagar instalação"
            assert dialog.query_one("#delete-cancel", Button).has_focus, "Enter never deletes"
            await press(pilot, "#delete-cancel")
            await settle(pilot, 3)

    try:
        asyncio.run(exercise())
        assert (first / "models" / "weights.bin").is_file()
        assert adapter.state.entry_for(first) is not None
    finally:
        adapter.close()


def test_deleting_the_installation_in_use_from_the_screen_leaves_the_hub_without_one(
    tmp_path, world
):
    adapter, first, second = two(tmp_path, world)
    world.running.add(second)
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await select_row(pilot, second)
            await press(pilot, "#install-delete")
            await settle(pilot, 3)
            dialog = app.screen
            assert isinstance(dialog, DeleteInstallationScreen)
            assert str(dialog.query_one("#delete-confirm", Button).label) == "Parar e apagar"
            await press(pilot, "#delete-confirm")
            await settle(pilot, 4)
            adapter.wait(10)
            await settle(pilot, 10)
            # No installation in use, nothing chosen in its place, no welcome.
            assert not isinstance(app.screen, DeleteInstallationScreen)
            assert type(app.screen).__name__ not in {"WelcomeScreen", "WizardScreen"}
            assert app.installed_config.installation.root is None
            app.show_section(Section.DASHBOARD)
            await settle(pilot, 3)
            overview = str(app.query_one("#dashboard-overview", Static).content)
            assert str(second) not in overview
            assert app.query_one("#dashboard-empty").display

    try:
        asyncio.run(exercise())
        assert not second.exists()
        assert adapter.active_root() is None
        assert (first / "models" / "weights.bin").is_file()
        assert "stop_stack" in world.commands_on(second)
    finally:
        adapter.close()
