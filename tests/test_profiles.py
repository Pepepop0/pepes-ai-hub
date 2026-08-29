import copy
import json
from pathlib import Path

import pytest

from local_ai_stack.profiles import (
    ProfileError,
    load_profile,
    load_profiles,
    validate_profile_data,
)

ROOT = Path(__file__).resolve().parents[1]


def test_all_profiles_load():
    p = load_profiles(ROOT / "profiles")
    assert {x.id for x in p} == {"pepe", "capella", "test"}


def test_ids_unique():
    p = load_profiles(ROOT / "profiles")
    ids = [x.id for x in p]
    assert len(ids) == len(set(ids))


def test_capella_requires_more_vram():
    p = {x.id: x for x in load_profiles(ROOT / "profiles")}
    assert p["capella"].hardware.min_vram_mib > p["pepe"].hardware.min_vram_mib


def test_profiles_declare_ui_defaults_separately_from_installed_config():
    profiles = {profile.id: profile for profile in load_profiles(ROOT / "profiles")}
    assert (
        profiles["pepe"].ui.default_pet,
        profiles["pepe"].ui.default_theme,
    ) == ("claw", "pepe")
    assert (
        profiles["capella"].ui.default_pet,
        profiles["capella"].ui.default_theme,
    ) == ("hello-kitty", "capella")
    assert (
        profiles["test"].ui.default_pet,
        profiles["test"].ui.default_theme,
    ) == ("none", "neutral")


def test_profile_vram_is_normalized_to_mib():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    assert profile.hardware.min_vram_mib == 12 * 1024
    assert profile.hardware.recommended_vram_mib == 12 * 1024
    assert profile.hardware.vram_tolerance_mib == 128


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        (lambda data: data.update(schema_version=2), "schema_version"),
        (
            lambda data: data["hardware"].update(recommended_vram_gb=4),
            "hardware.recommended_vram_gb",
        ),
        (lambda data: data["services"]["ollama"].update(enabled="yes"), "enabled"),
        (lambda data: data["hardware"].update(os="linux"), "only 'windows'"),
        (lambda data: data["hardware"].update(gpu_vendor="amd"), "only 'nvidia'"),
        (lambda data: data.update(unexpected=True), "unknown fields"),
    ],
)
def test_formal_validation_rejects_invalid_profiles(mutation, expected_field):
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    mutation(data)
    with pytest.raises(ProfileError, match=expected_field):
        validate_profile_data(data)


def test_machine_readable_schema_is_valid_json():
    schema = json.loads((ROOT / "schemas" / "profile.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"] == {"const": 1}
