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
    assert {x.id for x in p} == {"pepe", "pepe-8gb", "capella", "test"}


def test_profiles_are_listed_by_id_whatever_their_file_names():
    assert [x.id for x in load_profiles(ROOT / "profiles")] == [
        "capella",
        "pepe",
        "pepe-8gb",
        "test",
    ]


def test_ids_unique():
    p = load_profiles(ROOT / "profiles")
    ids = [x.id for x in p]
    assert len(ids) == len(set(ids))


def test_capella_requires_more_vram():
    p = {x.id: x for x in load_profiles(ROOT / "profiles")}
    assert p["capella"].hardware.min_vram_mib > p["pepe"].hardware.min_vram_mib
    assert p["pepe"].hardware.min_vram_mib > p["pepe-8gb"].hardware.min_vram_mib


def test_the_8gb_profile_is_the_full_stack_of_pepe_on_a_smaller_card():
    """Same services, same capabilities and the same image models as Pepe; only the
    chat model and the VRAM class differ."""
    p = {x.id: x for x in load_profiles(ROOT / "profiles")}
    pepe, small = p["pepe"], p["pepe-8gb"]
    assert small.hardware.min_vram_mib == 8 * 1024
    assert small.hardware.recommended_vram_mib == 8 * 1024
    assert small.services == pepe.services
    assert small.features == pepe.features
    assert small.workflows == pepe.workflows
    assert small.model_for("chat.primary") != pepe.model_for("chat.primary")
    assert str(small.model_for("chat.primary")) == "ollama/hermes3_3b"
    for role in ("image.diffusion", "image.text_encoder", "image.vae"):
        assert small.model_for(role) == pepe.model_for(role)


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
    # The 8 GB profile borrows Pepe's look: no new pet or theme without approval.
    assert (
        profiles["pepe-8gb"].ui.default_pet,
        profiles["pepe-8gb"].ui.default_theme,
    ) == ("claw", "pepe")


def test_profile_vram_is_normalized_to_mib():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    assert profile.hardware.min_vram_mib == 12 * 1024
    assert profile.hardware.recommended_vram_mib == 12 * 1024
    assert profile.hardware.vram_tolerance_mib == 128


def test_profiles_expose_only_active_canonical_model_requirements():
    profiles = {profile.id: profile for profile in load_profiles(ROOT / "profiles")}

    assert {(item.role, str(item.model_id)) for item in profiles["pepe"].model_requirements} == {
        ("chat.primary", "ollama/hermes3_8b"),
        ("image.diffusion", "comfyui/flux2_klein_fp8"),
        ("image.text_encoder", "comfyui/qwen_3_4b_encoder"),
        ("image.vae", "comfyui/flux2_vae"),
    }
    assert {str(item.model_id) for item in profiles["capella"].model_requirements} == {
        "ollama/qwen3_5_9b",
        "comfyui/flux2_klein_fp8",
        "comfyui/qwen_3_4b_encoder",
        "comfyui/flux2_vae",
    }
    assert profiles["test"].model_requirements == ()
    assert {str(item.model_id) for item in profiles["pepe-8gb"].model_requirements} == {
        "ollama/hermes3_3b",
        "comfyui/flux2_klein_fp8",
        "comfyui/qwen_3_4b_encoder",
        "comfyui/flux2_vae",
    }


def test_legacy_model_references_are_rejected_at_profile_ingress():
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    data["models"]["chat"]["primary"] = "hermes3:8b"
    with pytest.raises(ProfileError, match="canonical"):
        validate_profile_data(data)


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
