import random
from pathlib import Path

from local_ai_stack.config import UiConfig
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.profiles import load_profile
from local_ai_stack.renderer import MicroBehaviorRenderer
from local_ai_stack.runtime import StackRuntime
from local_ai_stack.services import ServiceState
from local_ai_stack.themes import ThemeRole
from local_ai_stack.tui.appearance import resolve_tui_appearance, textual_theme

ROOT = Path(__file__).resolve().parents[1]
STOPPED_RUNTIME = StackRuntime((), ServiceState.DOWN)


def test_capella_resolves_the_hello_kitty_asset():
    capella = load_profile(ROOT / "profiles" / "capella.yaml")
    appearance = resolve_tui_appearance(
        capella, UiConfig(), STOPPED_RUNTIME, now=0.0, rng=random.Random(3)
    )
    assert appearance.resolved.pet_id == "hello-kitty"
    assert appearance.animator is not None
    assert appearance.animator.pet.id == "hello-kitty"
    assert isinstance(appearance.animator, MicroBehaviorRenderer)
    assert "Pet unavailable" not in appearance.pet_text
    assert appearance.pet_text == appearance.animator.current_text(0.0)
    assert appearance.theme_text == "Capella"


def test_capella_pet_text_is_terminal_safe_and_stable_in_size():
    capella = load_profile(ROOT / "profiles" / "capella.yaml")
    appearance = resolve_tui_appearance(
        capella, UiConfig(), STOPPED_RUNTIME, now=0.0, rng=random.Random(3)
    )
    renderer = appearance.animator
    sizes = set()
    moment = 0.0
    while moment < 30:
        text = renderer.current_text(moment)
        assert unsafe_characters(text) == ()
        lines = text.splitlines()
        sizes.add((len(lines), max(len(line) for line in lines)))
        moment += 0.25
    assert len(sizes) == 1


def test_pepe_keeps_the_claw_asset():
    pepe = load_profile(ROOT / "profiles" / "pepe.yaml")
    appearance = resolve_tui_appearance(pepe, UiConfig(), STOPPED_RUNTIME, now=0.0)
    assert appearance.resolved.pet_id == "claw"
    assert appearance.animator.pet.id == "claw"
    assert not isinstance(appearance.animator, MicroBehaviorRenderer)


def test_missing_capella_pet_and_theme_degrade_without_identity_substitution(tmp_path):
    empty_pets = tmp_path / "pets"
    empty_themes = tmp_path / "themes"
    empty_pets.mkdir()
    empty_themes.mkdir()
    capella = load_profile(ROOT / "profiles" / "capella.yaml")

    appearance = resolve_tui_appearance(
        capella,
        UiConfig(),
        STOPPED_RUNTIME,
        pets_directory=empty_pets,
        themes_directory=empty_themes,
        now=0.0,
    )

    assert appearance.animator is None
    assert appearance.theme is None
    assert appearance.resolved.pet_id == "hello-kitty"
    assert appearance.pet_text == "Pet unavailable: hello-kitty"
    assert appearance.theme_text == "Theme unavailable: capella"


def test_theme_adapter_maps_semantic_roles_without_widget_hex_values():
    pepe = load_profile(ROOT / "profiles" / "pepe.yaml")
    appearance = resolve_tui_appearance(pepe, UiConfig(), STOPPED_RUNTIME, now=0.0)
    assert appearance.theme is not None
    theme = textual_theme(appearance.theme)
    assert theme.name == "pepitos-pepe"
    assert theme.primary == appearance.theme.color(ThemeRole.ACCENT)
