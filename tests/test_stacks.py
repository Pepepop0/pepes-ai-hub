"""Tests for the stack definition layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.layout import StackLayout
from local_ai_stack.profiles import load_profile
from local_ai_stack.stacks import (
    DEFAULT_STACK_ID,
    STANDARD_STACK,
    StackError,
    available_stacks,
    get_stack,
)

REPO = Path(__file__).resolve().parents[1]


def test_standard_stack_holds_the_three_programs():
    assert STANDARD_STACK.id == "standard"
    assert STANDARD_STACK.component_ids == ("ollama", "open-webui", "comfyui")


def test_standard_is_the_default_and_only_stack():
    assert DEFAULT_STACK_ID == "standard"
    assert get_stack() is STANDARD_STACK
    assert available_stacks() == (STANDARD_STACK,)


def test_unknown_stack_fails_clearly():
    with pytest.raises(StackError) as exc:
        get_stack("custom-stack")
    assert "standard" in str(exc.value)


def test_unknown_component_fails_clearly():
    with pytest.raises(StackError):
        STANDARD_STACK.component("stable-diffusion")


def test_components_resolve_their_directories_from_a_layout(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    assert STANDARD_STACK.component("ollama").app_dir(layout) == layout.ollama_app
    assert STANDARD_STACK.component("open-webui").app_dir(layout) == layout.open_webui_app
    assert STANDARD_STACK.component("comfyui").app_dir(layout) == layout.comfyui_app
    assert STANDARD_STACK.component("open-webui").data_dir(layout) == layout.open_webui_data
    assert STANDARD_STACK.component("comfyui").data_dir(layout) == layout.comfyui_data


def test_ollama_has_no_dedicated_data_directory(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    assert STANDARD_STACK.component("ollama").data_dir(layout) is None


def test_stack_directories_are_a_subset_of_the_layout(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    assert set(STANDARD_STACK.directories(layout)) <= set(layout.all_directories)


def test_stack_directories_follow_the_resolved_installation(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "AI's"))
    for path in STANDARD_STACK.directories(resolved.layout):
        assert resolved.root in path.parents


def test_component_ids_join_with_profile_service_ids():
    """A stack component and a profile service must key on the same id."""
    profile = load_profile(REPO / "profiles" / "pepe.yaml")
    assert set(STANDARD_STACK.component_ids) == set(profile.services)


def test_stack_definition_is_immutable():
    with pytest.raises(AttributeError):
        STANDARD_STACK.id = "other"


def test_stack_knows_nothing_about_profiles_or_install_modes():
    import local_ai_stack.stacks as stacks_module

    source = Path(stacks_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("pepe", "capella", "portable", "InstallLocation"):
        assert forbidden not in source
