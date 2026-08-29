from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path

from textual.theme import Theme

from ..appearance import Appearance, pet_state_for_stack, resolve_appearance
from ..config import UiConfig
from ..pets import PetError, load_pet_by_id
from ..profiles import Profile
from ..renderer import PetRenderer, build_pet_renderer
from ..runtime import StackRuntime
from ..themes import ThemeDefinition, ThemeError, ThemeRole, load_theme_by_id


@dataclass(frozen=True)
class TuiAppearance:
    """Resolved visuals for one profile.

    ``animator`` is whatever renders the pet: a plain :class:`PetAnimator` for a
    simple asset, or a behavior-composing renderer for an asset that ships one.
    The rail treats both the same way, so no widget knows which pet it has.
    """

    resolved: Appearance
    animator: PetRenderer | None
    theme: ThemeDefinition | None
    pet_text: str
    theme_text: str


def resolve_tui_appearance(
    profile: Profile,
    ui: UiConfig,
    runtime: StackRuntime,
    *,
    pets_directory: Path | None = None,
    themes_directory: Path | None = None,
    now: float | None = None,
    rng: random.Random | None = None,
) -> TuiAppearance:
    """Load resolved visual assets; unavailable identities degrade to text only."""
    resolved = resolve_appearance(profile, pet_id=ui.pet, theme_id=ui.theme)
    moment = time.monotonic() if now is None else now
    try:
        pet = load_pet_by_id(resolved.pet_id, pets_directory)
    except PetError:
        animator = None
        pet_text = f"Pet unavailable: {resolved.pet_id}"
    else:
        animator = build_pet_renderer(pet, pet_state_for_stack(runtime.state), now=moment, rng=rng)
        pet_text = animator.current_text(moment)

    try:
        theme = load_theme_by_id(resolved.theme_id, themes_directory)
    except ThemeError:
        theme = None
        theme_text = f"Theme unavailable: {resolved.theme_id}"
    else:
        theme_text = theme.display_name

    return TuiAppearance(resolved, animator, theme, pet_text, theme_text)


def textual_theme(definition: ThemeDefinition) -> Theme:
    """Map backend semantic roles to Textual without leaking toolkit concerns."""
    color = definition.color
    return Theme(
        name=f"pepitos-{definition.id}",
        primary=color(ThemeRole.ACCENT),
        secondary=color(ThemeRole.INFO),
        warning=color(ThemeRole.WARNING),
        error=color(ThemeRole.ERROR),
        success=color(ThemeRole.SUCCESS),
        accent=color(ThemeRole.ACCENT),
        foreground=color(ThemeRole.TEXT),
        background=color(ThemeRole.BACKGROUND),
        surface=color(ThemeRole.SURFACE),
        panel=color(ThemeRole.SURFACE),
        boost=color(ThemeRole.BORDER),
        dark=definition.dark,
        variables={"text-muted": color(ThemeRole.MUTED)},
    )
