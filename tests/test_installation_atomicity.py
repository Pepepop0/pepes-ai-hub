"""What must be true of a switch that cannot finish: nothing moved.

Memory, disk, backend and the registry are one decision. A write that fails
leaves the installation in use bound, shown and persisted; a stop that leaves
something running is not a stop; a folder a person picked and then abandoned
is not an installation the Hub knows.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_installation_switching import World, custom, hub, install

from local_ai_stack import installations as installations_module
from local_ai_stack.config import InstalledConfig
from local_ai_stack.installation import InstallLocation, InstallLocationMode
from local_ai_stack.installations import HubState, read_state, root_key
from local_ai_stack.tui.application import HubError


class Refuses:
    """A disk that will not take the write, for as long as it is armed."""

    def __init__(self, real) -> None:
        self.real = real
        # Disarmed until a test says otherwise: the installations it starts
        # from have to be written like any other.
        self.armed = False
        self.attempts = 0

    def __call__(self, path: Path, state: HubState) -> None:
        self.attempts += 1
        if self.armed:
            raise OSError("the state file could not be written")
        self.real(path, state)


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def refusing_disk(monkeypatch):
    refuses = Refuses(installations_module.write_state)
    monkeypatch.setattr("local_ai_stack.hub_application.write_state", refuses)
    return refuses


# --- a write that fails moves nothing ---------------------------------------------


def test_a_switch_whose_state_cannot_be_written_changes_nothing(tmp_path, world, refusing_disk):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)
        persisted_before = read_state(adapter.selection_path)
        assert persisted_before.active_root == first

        refusing_disk.armed = True
        with pytest.raises(HubError):
            adapter.switch_installation(second)

        # In memory, on disk, in the backend: still the first one.
        assert adapter.active_root() == first
        assert adapter.active_profile() == "pepe"
        assert adapter.config.installation.root == first
        assert adapter.backend.root == first
        assert read_state(adapter.selection_path).active_root == first
        assert adapter.state.active_root == first

        # And the commands still reach it.
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "start_stack"
        assert "start_stack" not in world.commands_on(second)
    finally:
        adapter.close()


def test_the_switch_works_once_the_disk_takes_the_write_again(tmp_path, world, refusing_disk):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)

        refusing_disk.armed = True
        with pytest.raises(HubError):
            adapter.switch_installation(second)
        refusing_disk.armed = False
        adapter.switch_installation(second)

        assert adapter.active_root() == second
        assert adapter.config.installation.root == second
        assert adapter.backend.root == second
        assert read_state(adapter.selection_path).active_root == second
    finally:
        adapter.close()


def test_an_installation_whose_state_cannot_be_written_is_not_adopted(
    tmp_path, world, refusing_disk
):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)
        adapter.forget_installation(second)

        refusing_disk.armed = True
        with pytest.raises(HubError):
            adapter.open_installation(second)

        assert adapter.active_root() == first
        assert [row.root for row in adapter.installation_summaries()] == [first]
        assert read_state(adapter.selection_path).entry_for(second) is None
    finally:
        adapter.close()


# --- a stop that leaves something running is not a stop -----------------------------


def test_a_stop_that_leaves_processes_running_does_not_switch(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)
        world.leaves_running.add(second)

        adapter.stop_and_switch(first)
        adapter.wait(10)

        assert adapter.active_root() == second, "the one that would not stop stays in use"
        assert adapter.backend.root == second
        assert read_state(adapter.selection_path).active_root == second
        assert world.commands_on(first) == ["first_run"], "nothing was bound to the target"
    finally:
        adapter.close()


def test_a_stop_that_leaves_a_port_occupied_does_not_switch(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)
        world.keeps_ports.add(second)

        adapter.stop_and_switch(first)
        adapter.wait(10)

        assert adapter.active_root() == second
        assert read_state(adapter.selection_path).active_root == second
    finally:
        adapter.close()


def test_a_clean_stop_switches(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)

        adapter.stop_and_switch(first)
        adapter.wait(10)

        assert adapter.active_root() == first
        assert adapter.backend.root == first
        assert read_state(adapter.selection_path).active_root == first
    finally:
        adapter.close()


# --- the registry always holds the installation in use -------------------------------


def config_for(root: str, profile: str = "pepe", mode=InstallLocationMode.CUSTOM):
    location = (
        InstallLocation.custom(root) if mode is InstallLocationMode.CUSTOM else InstallLocation(mode, None)
    )
    return InstalledConfig(profile, installation=location)


def test_a_state_with_an_active_installation_and_an_empty_list_reconciles_on_load():
    state = HubState(active=config_for(r"C:\Pepito\PEPEH"))
    assert state.known == (), "the state as written"

    revived = HubState.from_dict(state.to_dict())

    assert [str(entry.root) for entry in revived.ordered()] == [r"C:\Pepito\PEPEH"]
    assert revived.entry_for(r"C:\Pepito\PEPEH").last_known_profile == "pepe"
    assert revived.active_root is not None


def test_an_active_installation_missing_from_the_list_is_put_back_without_touching_the_rest():
    other = r"D:\Pepinho"
    state = HubState(active=config_for(r"C:\Pepito\PEPEH")).remembering(
        other, InstallLocationMode.CUSTOM, profile_id="pepe-8gb"
    )
    document = state.to_dict()
    document["known_installations"] = [
        entry for entry in document["known_installations"] if entry["root"] != r"C:\Pepito\PEPEH"
    ]

    revived = HubState.from_dict(document)

    roots = {str(entry.root) for entry in revived.known}
    assert roots == {r"C:\Pepito\PEPEH", other}
    assert revived.entry_for(other).last_known_profile == "pepe-8gb", "nothing else was disturbed"


def test_the_same_folder_written_twice_in_a_state_file_is_one_entry():
    state = HubState(active=config_for(r"C:\Pepito\PEPEH"))
    document = state.to_dict()
    document["known_installations"] = [
        {"root": r"C:\Pepito\PEPEH", "mode": "custom", "profile": "pepe"},
        {"root": r"c:\pepito\pepeh\\", "mode": "custom", "profile": "pepe"},
    ]

    revived = HubState.from_dict(document)

    assert len(revived.known) == 1
    assert revived.entry_for(r"C:\PEPITO\pepeh") is not None


# --- portable is an installation like any other, and it is never forgotten ------------


def test_a_portable_installation_in_use_is_identified_by_the_folder_it_resolved_to(
    tmp_path, world
):
    adapter = hub(tmp_path, world)
    try:
        install(adapter, InstallSelectionPortable())
        resolved = adapter.backend.root
        assert adapter.active_root() == resolved, "portable still has an identity"
        rows = adapter.installation_summaries()
        assert [row.root for row in rows] == [resolved]
        assert rows[0].active and rows[0].mode is InstallLocationMode.PORTABLE
        assert read_state(adapter.selection_path).entry_for(resolved) is not None
    finally:
        adapter.close()


def test_a_portable_installation_in_use_cannot_be_forgotten(tmp_path, world):
    adapter = hub(tmp_path, world)
    try:
        install(adapter, InstallSelectionPortable())
        resolved = adapter.backend.root

        with pytest.raises(HubError, match="em uso"):
            adapter.forget_installation(resolved)

        assert [row.root for row in adapter.installation_summaries()] == [resolved]
        assert resolved.exists(), "forgetting never touches a folder"
    finally:
        adapter.close()


def test_a_portable_configuration_still_names_no_root_of_its_own(tmp_path, world):
    """Relocation is what portable is for: the folder is resolved, never written down."""
    adapter = hub(tmp_path, world)
    try:
        install(adapter, InstallSelectionPortable())
        assert adapter.config.installation.mode is InstallLocationMode.PORTABLE
        assert adapter.config.installation.root is None
        persisted = read_state(adapter.selection_path)
        assert persisted.active.installation.root is None
        assert persisted.entry_for(adapter.backend.root) is not None
    finally:
        adapter.close()


def InstallSelectionPortable():
    from local_ai_stack.tui.application import InstallSelection

    return InstallSelection("pepe", InstallLocationMode.PORTABLE)


# --- adopting a folder is remembered only once it is in use ---------------------------


def test_a_folder_that_is_validated_but_not_switched_to_is_not_remembered(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)
        adapter.forget_installation(second)
        world.running.add(first)  # switching now needs a stop, which nobody approved

        from local_ai_stack.tui.application import SwitchRequiresStop

        with pytest.raises(SwitchRequiresStop):
            adapter.open_installation(second)

        assert [row.root for row in adapter.installation_summaries()] == [first]
        assert read_state(adapter.selection_path).entry_for(second) is None
    finally:
        adapter.close()


def test_a_folder_that_is_switched_to_is_remembered(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)
        adapter.forget_installation(second)

        adapter.open_installation(second)

        assert {row.root for row in adapter.installation_summaries()} == {first, second}
        assert read_state(adapter.selection_path).entry_for(second) is not None
    finally:
        adapter.close()


def test_a_folder_that_is_not_an_installation_is_never_remembered(tmp_path, world):
    first, elsewhere = tmp_path / "first", tmp_path / "elsewhere"
    elsewhere.mkdir()
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        with pytest.raises(HubError):
            adapter.open_installation(elsewhere)
        assert [row.root for row in adapter.installation_summaries()] == [first]
    finally:
        adapter.close()


# --- relocating an installation replaces its entry, and only when it worked -------------


def test_finding_a_moved_installation_replaces_the_old_entry(tmp_path, world):
    first, moved = tmp_path / "first", tmp_path / "moved"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(moved, "pepe-8gb"))
        adapter.switch_installation(first)
        gone = tmp_path / "gone"
        adapter._persist(
            adapter.state.remembering(gone, InstallLocationMode.CUSTOM, profile_id="pepe-8gb")
        )
        assert adapter.state.entry_for(gone) is not None

        adapter.relocate_installation(gone, moved)

        roots = {row.root for row in adapter.installation_summaries()}
        assert gone not in roots and moved in roots
        assert adapter.active_root() == moved
        assert read_state(adapter.selection_path).entry_for(gone) is None
    finally:
        adapter.close()


def test_a_relocation_that_needs_a_stop_keeps_the_old_entry_until_it_succeeds(tmp_path, world):
    first, moved = tmp_path / "first", tmp_path / "moved"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(moved, "pepe-8gb"))
        adapter.switch_installation(first)
        gone = tmp_path / "gone"
        adapter._persist(
            adapter.state.remembering(gone, InstallLocationMode.CUSTOM, profile_id="pepe-8gb")
        )
        world.running.add(first)

        from local_ai_stack.tui.application import SwitchRequiresStop

        with pytest.raises(SwitchRequiresStop):
            adapter.relocate_installation(gone, moved)
        assert adapter.state.entry_for(gone) is not None, "still there while nothing happened"
        assert adapter.active_root() == first

        adapter.stop_and_switch(moved, replacing=gone)
        adapter.wait(10)

        assert adapter.active_root() == moved
        assert adapter.state.entry_for(gone) is None
        assert read_state(adapter.selection_path).entry_for(gone) is None
        assert root_key(read_state(adapter.selection_path).active_root) == root_key(moved)
    finally:
        adapter.close()
