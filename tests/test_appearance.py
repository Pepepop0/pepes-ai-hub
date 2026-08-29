from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import local_ai_stack
from local_ai_stack.appearance import (
    DEFAULT_APPEARANCE_BY_PROFILE,
    FALLBACK_APPEARANCE,
    Appearance,
    pet_state_for_stack,
    resolve_appearance,
)
from local_ai_stack.pets import PetAnimator, PetState, available_pets, load_pet_by_id
from local_ai_stack.services import ServiceState
from local_ai_stack.themes import available_themes

REPO = Path(__file__).resolve().parents[1]
SRC = Path(local_ai_stack.__file__).resolve().parents[1]
PET_ASSETS = REPO / "assets" / "pets"
THEME_ASSETS = REPO / "assets" / "themes"


@pytest.mark.parametrize(
    ("stack", "expected"),
    [
        (ServiceState.UP, PetState.IDLE),
        (ServiceState.DEGRADED, PetState.WARNING),
        (ServiceState.DOWN, PetState.SLEEPING),
        (ServiceState.UNKNOWN, PetState.WARNING),
    ],
)
def test_stack_health_maps_to_a_pet_mood(stack, expected):
    assert pet_state_for_stack(stack) is expected


def test_every_service_state_has_a_mapping():
    assert {pet_state_for_stack(state) for state in ServiceState} <= set(PetState)


def test_pet_state_and_service_state_stay_separate_types():
    assert not isinstance(PetState.IDLE, ServiceState)
    assert {s.value for s in PetState}.isdisjoint({s.value for s in ServiceState})


def test_profile_defaults_are_declared():
    assert resolve_appearance("pepe") == Appearance("claw", "pepe")
    assert resolve_appearance("capella") == DEFAULT_APPEARANCE_BY_PROFILE["capella"]
    assert resolve_appearance("unknown-profile") == FALLBACK_APPEARANCE


def test_overrides_win_over_profile_defaults():
    assert resolve_appearance("pepe", theme_id="capella") == Appearance("claw", "capella")
    assert resolve_appearance("capella", pet_id="claw").pet_id == "claw"


def test_pepe_appearance_resolves_to_shipped_assets():
    appearance = resolve_appearance("pepe")
    assert appearance.pet_id in available_pets(PET_ASSETS)
    assert appearance.theme_id in available_themes(THEME_ASSETS)


def test_capella_pet_is_declared_but_not_shipped_yet():
    # Adding assets/pets/hello-kitty/ is enough to satisfy this binding: the
    # mapping already points at it and no code has to change.
    assert DEFAULT_APPEARANCE_BY_PROFILE["capella"].pet_id not in available_pets(PET_ASSETS)
    assert DEFAULT_APPEARANCE_BY_PROFILE["capella"].theme_id in available_themes(THEME_ASSETS)


def test_stack_state_drives_the_animator_end_to_end():
    claw = load_pet_by_id("claw", PET_ASSETS)
    animator = PetAnimator(claw, now=0.0)
    assert animator.set_state(pet_state_for_stack(ServiceState.DOWN), now=0.0) is True
    assert animator.state is PetState.SLEEPING
    assert "z" in animator.current_text(0.0)
    animator.set_state(pet_state_for_stack(ServiceState.UP), now=5.0)
    assert animator.state is PetState.IDLE
    assert animator.elapsed(5.0) == 0.0


def test_pet_and_theme_backends_do_not_import_the_tui():
    code = (
        "import sys;"
        "import local_ai_stack.pets, local_ai_stack.themes, local_ai_stack.appearance;"
        "print(any(m in sys.modules for m in ('textual', 'rich')))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == "False"
