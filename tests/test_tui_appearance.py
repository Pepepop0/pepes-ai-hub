from local_ai_stack.config import UiConfig
from local_ai_stack.profiles import load_profile
from local_ai_stack.themes import ThemeRole
from local_ai_stack.tui.appearance import resolve_tui_appearance, textual_theme
from tests.test_app import ROOT, stopped_runtime


def test_missing_capella_pet_and_theme_degrade_without_identity_substitution(tmp_path):
    empty_pets = tmp_path / "pets"
    empty_themes = tmp_path / "themes"
    empty_pets.mkdir()
    empty_themes.mkdir()
    capella = load_profile(ROOT / "profiles" / "capella.yaml")

    appearance = resolve_tui_appearance(
        capella,
        UiConfig(),
        stopped_runtime(),
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
    appearance = resolve_tui_appearance(pepe, UiConfig(), stopped_runtime(), now=0.0)
    assert appearance.theme is not None
    theme = textual_theme(appearance.theme)
    assert theme.name == "pepitos-pepe"
    assert theme.primary == appearance.theme.color(ThemeRole.ACCENT)
