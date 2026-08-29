"""Bridge between what the stack is doing and what the pet shows.

Two translations live here, and nothing else:

* stack health -> :class:`~local_ai_stack.pets.PetState`;
* profile id -> which pet and theme to use.

Runtime state and pet state stay separate types on purpose. ``ServiceState`` and
``RuntimeProcessState`` describe the machine and are never widened for cosmetic
reasons; ``PetState`` describes a drawing. This module is the only place the two
vocabularies meet.

Integration point for Codex / Claude 1: the per-profile defaults below are a
placeholder for a future ``ui:`` block in the profile schema (``ui.pet``,
``ui.theme``), plus a general-settings override. Profile schema is not touched
in this branch to avoid conflicting with in-flight work; when that block lands,
pass its values as ``pet_id`` / ``theme_id`` to :func:`resolve_appearance` and
the table becomes a fallback only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .pets import PetState
from .services import ServiceState

STACK_PET_STATES: Mapping[ServiceState, PetState] = {
    ServiceState.UP: PetState.IDLE,
    ServiceState.DEGRADED: PetState.WARNING,
    ServiceState.DOWN: PetState.SLEEPING,
    # We could not determine the stack state: that is worth showing, and it is
    # not the same as "everything is off", which is SLEEPING.
    ServiceState.UNKNOWN: PetState.WARNING,
}

DEFAULT_PET = "claw"
DEFAULT_THEME = "pepe"


@dataclass(frozen=True)
class Appearance:
    """Which pet and theme a profile asks for. Ids only — nothing is loaded."""

    pet_id: str
    theme_id: str


# Declared bindings. The Capella pet asset does not exist yet: adding
# assets/pets/hello-kitty/ is enough to make it work, with no code change.
DEFAULT_APPEARANCE_BY_PROFILE: Mapping[str, Appearance] = {
    "pepe": Appearance("claw", "pepe"),
    "capella": Appearance("hello-kitty", "capella"),
    "test": Appearance("claw", "pepe"),
}

FALLBACK_APPEARANCE = Appearance(DEFAULT_PET, DEFAULT_THEME)


def pet_state_for_stack(state: ServiceState) -> PetState:
    """Translate an aggregated stack health verdict into a pet mood."""
    return STACK_PET_STATES.get(state, PetState.WARNING)


def resolve_appearance(
    profile_id: str,
    *,
    pet_id: str | None = None,
    theme_id: str | None = None,
    defaults: Mapping[str, Appearance] = DEFAULT_APPEARANCE_BY_PROFILE,
) -> Appearance:
    """Pick a pet and theme for a profile; explicit overrides always win."""
    base = defaults.get(profile_id, FALLBACK_APPEARANCE)
    return Appearance(pet_id or base.pet_id, theme_id or base.theme_id)
