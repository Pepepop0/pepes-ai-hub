"""The Hub's own state: what it knows about installations, and how it survives.

One file, in the Hub's state folder, holding what no single installation can:
which one is active, which ones exist, when each was last used and what each
last did. It is the file schema version 1 wrote, so an existing machine keeps
its installation when this version reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from local_ai_stack.config import GeneralConfig, InstalledConfig, UiConfig
from local_ai_stack.installation import InstallLocation, InstallLocationMode
from local_ai_stack.installations import (
    FAILURE,
    INSTALL,
    REPAIR,
    STATE_SCHEMA_VERSION,
    HubState,
    dumps_state,
    loads_state,
    read_state,
    root_key,
    write_state,
)

PEPEH = r"C:\Users\Pedro\Desktop\PEPEH"
OTHER = r"C:\Users\Pedro\Desktop\PEPITO_8GB_TEST"

VERSION_ONE = f"""schema_version: 1
profile:
  id: pepe
stack:
  id: standard
installation:
  mode: custom
  root: {PEPEH}
general:
  ui:
    theme: pepe
    pet: claw
    right_rail_visible: true
    right_rail_expanded: false
  recent_custom_roots:
  - {PEPEH}
  - {OTHER}
"""


def config_for(root: str, profile: str = "pepe") -> InstalledConfig:
    return InstalledConfig(profile, installation=InstallLocation.custom(root))


# --- migration ------------------------------------------------------------------


def test_a_version_one_state_keeps_its_installation_its_history_and_its_preferences():
    state = loads_state(VERSION_ONE)

    assert state.active is not None and state.active.profile_id == "pepe"
    assert str(state.active_root) == PEPEH
    assert state.general.ui.theme == "pepe" and state.general.ui.pet == "claw"
    assert state.general.ui.right_rail_expanded is False
    assert state.general.recent_custom_roots == (PEPEH, OTHER)
    assert [str(entry.root) for entry in state.ordered()] == [PEPEH]
    assert state.entry_for(PEPEH).last_known_profile == "pepe"


def test_a_migrated_state_is_written_back_in_the_new_shape_without_losing_anything(tmp_path):
    path = tmp_path / "hub-selection.yaml"
    path.write_text(VERSION_ONE, encoding="utf-8")

    migrated = read_state(path)
    write_state(path, migrated)
    reread = read_state(path)

    assert f"schema_version: {STATE_SCHEMA_VERSION}" in path.read_text(encoding="utf-8")
    assert reread.active_root == migrated.active_root
    assert reread.general == migrated.general
    assert [entry.root for entry in reread.ordered()] == [
        entry.root for entry in migrated.ordered()
    ]


def test_an_absent_state_file_is_simply_absent(tmp_path):
    assert read_state(tmp_path / "nothing.yaml") is None


# --- identity of a folder --------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [PEPEH, PEPEH.upper(), PEPEH.lower(), PEPEH + "\\", PEPEH.replace("\\", "/")],
)
def test_one_folder_written_five_ways_is_one_installation(written):
    state = HubState().remembering(PEPEH, InstallLocationMode.CUSTOM)
    state = state.remembering(written, InstallLocationMode.CUSTOM, profile_id="pepe")

    assert len(state.known) == 1
    assert state.entry_for(written) is not None
    assert root_key(written) == root_key(PEPEH)


# --- order, history, forgetting ---------------------------------------------------


def test_the_most_recently_used_installation_comes_first():
    state = HubState()
    state = state.activating(config_for(OTHER, "pepe-8gb"))
    state = state.activating(config_for(PEPEH, "pepe"))

    assert [str(entry.root) for entry in state.ordered()] == [PEPEH, OTHER]
    assert state.active_root is not None and str(state.active_root) == PEPEH


def test_an_installation_keeps_one_line_of_what_it_last_did():
    state = HubState().activating(config_for(PEPEH))
    state = state.recording(PEPEH, INSTALL, result="succeeded")
    state = state.recording(PEPEH, REPAIR, result="succeeded")
    state = state.recording(PEPEH, FAILURE, summary="download interrompido")

    entry = state.entry_for(PEPEH)
    assert entry.last_install.result == "succeeded"
    assert entry.last_repair.result == "succeeded"
    assert entry.last_failure.summary == "download interrompido"
    assert entry.last_install.at <= entry.last_failure.at

    revived = loads_state(dumps_state(state))
    assert revived.entry_for(PEPEH).last_failure.summary == "download interrompido"


def test_forgetting_removes_the_entry_and_never_the_active_one():
    state = HubState().activating(config_for(PEPEH)).remembering(OTHER, InstallLocationMode.CUSTOM)

    assert [str(e.root) for e in state.forgetting(OTHER).ordered()] == [PEPEH]
    assert state.forgetting(PEPEH) == state, "the active installation is not forgotten"


def test_history_is_kept_when_a_known_installation_is_seen_again():
    state = HubState().activating(config_for(PEPEH))
    state = state.recording(PEPEH, INSTALL, result="succeeded")
    used_at = state.entry_for(PEPEH).last_used_at

    later = state.remembering(PEPEH, InstallLocationMode.CUSTOM, profile_id="pepe")

    entry = later.entry_for(PEPEH)
    assert entry.last_install is not None and entry.last_used_at == used_at


# --- what is derived is never stored ----------------------------------------------


def test_nothing_about_running_or_health_is_persisted():
    state = HubState().activating(config_for(PEPEH)).recording(PEPEH, INSTALL, result="succeeded")
    document = dumps_state(state)

    for word in ("running", "healthy", "health", "up:", "ready"):
        assert word not in document


def test_preferences_belong_to_the_hub_and_not_to_an_installation():
    general = GeneralConfig(ui=UiConfig(theme="capella", pet="hello-kitty"))
    state = HubState(general=general).activating(config_for(PEPEH))

    # The configuration carried in is given the Hub's preferences, not the
    # other way round: a stale copy never overwrites what the person chose.
    assert state.general == general
    assert state.active.general == general


def test_a_portable_installation_is_known_by_the_folder_it_resolved_to():
    portable = InstalledConfig("pepe", installation=InstallLocation.portable())
    state = HubState().activating(portable)

    assert state.active_root is None, "portable names no root of its own"
    assert state.known == ()

    # The Hub adds the folder it resolved to, which is what a list can show.
    state = state.remembering(OTHER, InstallLocationMode.PORTABLE, profile_id="pepe")
    entry = state.entry_for(OTHER)
    assert entry is not None and entry.mode is InstallLocationMode.PORTABLE


# --- writing ----------------------------------------------------------------------


def test_writing_the_same_state_twice_does_not_touch_the_file(tmp_path):
    path = tmp_path / "hub-selection.yaml"
    state = HubState().activating(config_for(PEPEH))

    write_state(path, state)
    stamp = path.stat().st_mtime_ns
    write_state(path, state)

    assert path.stat().st_mtime_ns == stamp


def test_a_state_written_by_a_newer_hub_is_read_for_what_it_holds(tmp_path):
    """Unknown fields do not stop the Hub from finding the installations."""
    path = tmp_path / "hub-selection.yaml"
    state = HubState().activating(config_for(PEPEH))
    document = dumps_state(state).replace(
        "known_installations:", "known_installations:\n- root: ''\n  mode: custom"
    )
    path.write_text(document, encoding="utf-8")

    reread = read_state(path)
    assert [str(entry.root) for entry in reread.ordered()] == [PEPEH]


def test_entries_never_used_sort_after_the_ones_that_were():
    older = datetime.now(UTC) - timedelta(days=3)
    state = HubState().remembering(OTHER, InstallLocationMode.CUSTOM)
    state = state.activating(config_for(PEPEH))
    state = HubState(
        active=state.active,
        known=tuple(
            entry
            if entry.key != root_key(PEPEH)
            else type(entry)(**{**entry.__dict__, "last_used_at": older})
            for entry in state.known
        ),
        general=state.general,
    )

    assert [str(entry.root) for entry in state.ordered()] == [PEPEH, OTHER]
    assert Path(str(state.ordered()[0].root)) == Path(PEPEH)
