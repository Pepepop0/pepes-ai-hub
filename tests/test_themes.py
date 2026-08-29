from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.themes import (
    ThemeError,
    ThemeRole,
    available_themes,
    load_theme,
    load_theme_by_id,
    load_themes,
    validate_theme_data,
)

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "themes"

COLORS = {role.value: "#101010" for role in ThemeRole}
THEME = {
    "schema_version": 1,
    "id": "fixture",
    "display_name": "Fixture",
    "description": "test theme",
    "dark": True,
    "colors": COLORS,
}


def write_theme(root: Path, name: str = "fixture", **overrides) -> Path:
    import yaml

    data = {**THEME, **overrides}
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_valid_theme_exposes_roles_and_tokens():
    theme = validate_theme_data(THEME)
    assert (theme.id, theme.display_name, theme.dark) == ("fixture", "Fixture", True)
    assert theme.color(ThemeRole.ACCENT) == "#101010"
    assert set(theme.as_tokens()) == {role.value for role in ThemeRole}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 9}, "unsupported version"),
        ({"colors": {**COLORS, "extra": "#fff"}}, "unknown roles"),
        ({"colors": {k: v for k, v in COLORS.items() if k != "accent"}}, "missing roles"),
        ({"colors": {**COLORS, "accent": "green"}}, "hex colour"),
        ({"colors": {**COLORS, "accent": "#12345"}}, "hex colour"),
        ({"dark": "yes"}, "must be a boolean"),
        ({"display_name": ""}, "non-empty string"),
    ],
)
def test_theme_errors_are_specific(mutation, message):
    with pytest.raises(ThemeError, match=message):
        validate_theme_data({**THEME, **mutation})


def test_missing_required_field_is_reported():
    with pytest.raises(ThemeError, match="missing required fields: colors"):
        validate_theme_data({k: v for k, v in THEME.items() if k != "colors"})


def test_short_hex_is_accepted_and_normalised():
    theme = validate_theme_data({**THEME, "colors": {**COLORS, "accent": "#ABC"}})
    assert theme.color(ThemeRole.ACCENT) == "#abc"


def test_load_theme_requires_the_id_to_match_the_filename(tmp_path):
    path = write_theme(tmp_path, name="other")
    with pytest.raises(ThemeError, match="must match the file name"):
        load_theme(path)


def test_load_and_list_themes(tmp_path):
    write_theme(tmp_path)
    assert available_themes(tmp_path) == ("fixture",)
    assert [t.id for t in load_themes(tmp_path)] == ["fixture"]
    assert load_theme_by_id("fixture", tmp_path).display_name == "Fixture"


def test_unknown_theme_lists_what_is_available(tmp_path):
    write_theme(tmp_path)
    with pytest.raises(ThemeError, match="available: fixture"):
        load_theme_by_id("nope", tmp_path)


def test_shipped_themes_load():
    themes = {theme.id: theme for theme in load_themes(ASSETS)}
    assert set(themes) == {"pepe", "capella"}
    for theme in themes.values():
        assert set(theme.as_tokens()) == {role.value for role in ThemeRole}
        assert theme.color(ThemeRole.BACKGROUND).startswith("#")
    assert themes["pepe"].display_name == "Pepe"
    assert themes["capella"].display_name == "Capella"
