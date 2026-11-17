import copy
import json
from pathlib import Path

import pytest
import yaml

from local_ai_stack.profiles import (
    ProfileError,
    declared_workflows,
    load_profile,
    load_profiles,
    validate_profile_data,
)

ROOT = Path(__file__).resolve().parents[1]


def test_all_profiles_load():
    p = load_profiles(ROOT / "profiles")
    expected = {
        yaml.safe_load(path.read_text("utf-8"))["id"]
        for path in (ROOT / "profiles").glob("*.yaml")
        if not path.name.endswith(".overlay.yaml")
    }
    assert {x.id for x in p} == expected


def test_every_shipped_profile_carries_its_version():
    versions = {x.id: x.version for x in load_profiles(ROOT / "profiles")}
    assert {key: versions[key] for key in ("capella", "pepe", "pepe-8gb")} == {
        "capella": "5.2.0",
        "pepe": "1.2.0",
        "pepe-8gb": "1.2.0",
    }


def test_profiles_are_listed_by_id_whatever_their_file_names():
    ids = [x.id for x in load_profiles(ROOT / "profiles")]
    assert ids == sorted(ids)


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
    assert {str(item.model_id) for item in profiles["capella"].model_requirements} >= {
        "ollama/qwen3_8_27b_q5",
        "comfyui/qwen_image_2_1_int8",
        "comfyui/qwen3vl_8b_int8_encoder",
        "comfyui/qwen_image_2_1_vae",
        "comfyui/sam3_1_multiplex",
        "comfyui/depth_anything_3_mono_large",
    }
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
        (lambda data: data["hardware"].update(os="darwin"), "'windows' or 'linux'"),
        (lambda data: data["hardware"].update(gpu_vendor="amd"), "only 'nvidia'"),
        (lambda data: data.update(unexpected=True), "unknown fields"),
    ],
)
def test_formal_validation_rejects_invalid_profiles(mutation, expected_field):
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    mutation(data)
    with pytest.raises(ProfileError, match=expected_field):
        validate_profile_data(data)


@pytest.mark.parametrize(
    ("size", "steps", "field"),
    [
        ("1024", None, "workflows.text_to_image.size"),
        ("1024X1024", None, "workflows.text_to_image.size"),
        ("0x1024", None, "workflows.text_to_image.size"),
        (None, 0, "workflows.text_to_image.steps"),
        (None, "20", "workflows.text_to_image.steps"),
    ],
)
def test_a_workflow_size_or_step_count_is_validated_in_open_webui_spelling(size, steps, field):
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    if size is not None:
        data["workflows"]["text_to_image"]["size"] = size
    if steps is not None:
        data["workflows"]["text_to_image"]["steps"] = steps
    with pytest.raises(ProfileError, match=field):
        validate_profile_data(data)


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ("think=false", "models.chat.params"),
        ({"think": None}, "models.chat.params.think"),
        ({"think": [False]}, "models.chat.params.think"),
        ({"system": "   "}, "models.chat.params.system"),
    ],
)
def test_chat_model_parameters_must_be_named_scalars(params, field):
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    data["models"]["chat"]["params"] = params
    with pytest.raises(ProfileError, match=field):
        validate_profile_data(data)


def test_chat_model_parameters_are_read_as_declared_and_default_to_none():
    profiles = {p.id: p for p in load_profiles(ROOT / "profiles")}
    assert profiles["capella"].chat_params == {"think": False, "num_ctx": 16384}
    assert profiles["pepe"].chat_params == {} and profiles["pepe-8gb"].chat_params == {}
    data = copy.deepcopy(profiles["pepe"].raw)
    data["models"]["chat"]["params"] = {"think": False, "num_ctx": 8192, "system": "Be terse."}
    assert validate_profile_data(data).chat_params == {
        "think": False,
        "num_ctx": 8192,
        "system": "Be terse.",
    }


def test_a_workflow_declaration_may_carry_a_size_and_a_step_count():
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "pepe.yaml").raw)
    data["workflows"]["text_to_image"].update(size="1024x1024", steps=20)
    workflow = validate_profile_data(data).workflows["text_to_image"]
    assert (workflow.manifest_id, workflow.size, workflow.steps) == (
        "flux2_klein_t2i",
        "1024x1024",
        20,
    )
    for name in ("pepe", "pepe-8gb"):
        shipped = load_profile(ROOT / "profiles" / f"{name}.yaml").workflows["text_to_image"]
        assert (shipped.size, shipped.steps) == ("1024x1024", None)


def test_machine_readable_schema_is_valid_json():
    schema = json.loads((ROOT / "schemas" / "profile.schema.json").read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"] == {"const": 1}
    swap = schema["properties"]["character_swap"]
    assert swap["additionalProperties"] is False
    assert set(swap["required"]) == {"reference", "marked", "segment", "depth"}
    assert set(swap["properties"]) == {*swap["required"], "scene"}


def variant_data():
    data = yaml.safe_load((ROOT / "profiles/capella.yaml").read_text("utf-8"))
    data["models"]["variant_image"] = copy.deepcopy(data["models"]["image"])
    data["variants"] = [
        {
            "id": "capella-studio",
            "name": "Capella Studio",
            "images": "variant_image",
            "workflows": copy.deepcopy(data["workflows"]),
        }
    ]
    return data


def test_a_chat_variant_needs_the_router_and_a_plain_id():
    """Without the router, Open WebUI's own handler would make the variant's pictures
    with the profile's models; the id becomes an Open WebUI model id."""
    data = variant_data()
    profile = validate_profile_data(copy.deepcopy(data))
    baseline = profile.variants[0]
    assert baseline.id == "capella-studio"
    assert profile.workflow("variant.capella-studio.image_edit") == baseline.workflows["image_edit"]
    assert profile.workflow("image_edit") == profile.workflows["image_edit"]
    assert profile.workflow("variant.capella-studio.missing") is None
    assert profile.workflow("variant.nobody.image_edit") is None

    no_router = copy.deepcopy(data)
    no_router["runtime"]["image_router"] = False
    with pytest.raises(ProfileError, match="runtime.image_router"):
        validate_profile_data(no_router)

    shouting = copy.deepcopy(data)
    shouting["variants"][0]["id"] = "Capella Studio"
    with pytest.raises(ProfileError, match=r"variants\[0\].id"):
        validate_profile_data(shouting)

    no_edit = copy.deepcopy(data)
    del no_edit["variants"][0]["workflows"]["image_edit"]
    with pytest.raises(ProfileError, match=r"variants\[capella-studio\].workflows.image_edit"):
        validate_profile_data(no_edit)


def test_several_variants_each_own_their_models_group_and_may_not_share_ids():
    data = variant_data()
    second = copy.deepcopy(data["variants"][0])
    second.update(id="second", name="Second", images="second_image")
    data["models"]["second_image"] = {
        "diffusion": "comfyui/qwen_image_2_1_int8",
        "extra_lora": "TBD",
    }
    data["variants"].append(second)
    profile = validate_profile_data(copy.deepcopy(data))
    assert [item.id for item in profile.variants] == ["capella-studio", "second"]
    assert profile.model_for("second_image.diffusion") is not None
    assert profile.model_for("second_image.extra_lora") is None, "TBD names no model"
    keys = [key for key, _field, _decl in declared_workflows(profile)]
    assert "variant.second.text_to_image" in keys and "variant.capella-studio.image_edit" in keys

    clash = copy.deepcopy(data)
    clash["variants"][1]["id"] = "capella-studio"
    with pytest.raises(ProfileError, match="duplicate variant id"):
        validate_profile_data(clash)
    shared = copy.deepcopy(data)
    shared["variants"][1]["images"] = "variant_image"
    with pytest.raises(ProfileError, match="duplicate variant images"):
        validate_profile_data(shared)
    stolen = copy.deepcopy(data)
    stolen["variants"][1]["images"] = "image"
    with pytest.raises(ProfileError, match="own models group"):
        validate_profile_data(stolen)
    empty = copy.deepcopy(data)
    del empty["models"]["second_image"]
    with pytest.raises(ProfileError, match="names no models"):
        validate_profile_data(empty)
    badrole = copy.deepcopy(data)
    badrole["models"]["second_image"]["Bad-Role"] = "comfyui/qwen_image_2_1_vae"
    with pytest.raises(ProfileError, match="Bad-Role"):
        validate_profile_data(badrole)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["character_swap"].pop("depth"), "character_swap.depth"),
        (lambda data: data["character_swap"].update(unexpected={}), "manifest_id"),
        (lambda data: data.update(character_swap=[]), "must be a mapping"),
        (lambda data: data["models"].pop("swap"), "models.swap"),
        (
            lambda data: data["features"].update(image_editing=False),
            "character_swap is declared but features.image_editing",
        ),
    ],
)
def test_character_swap_section_is_validated(mutation, message):
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "capella.yaml").raw)
    mutation(data)
    with pytest.raises(ProfileError, match=message):
        validate_profile_data(data)


def test_character_swap_without_the_router_loads_but_is_never_offered():
    """The swap is only reachable through the image router: a profile that turns the
    router off keeps loading, and its swap is neither planned nor installed."""
    data = copy.deepcopy(load_profile(ROOT / "profiles" / "capella.yaml").raw)
    data.pop("variants", None)
    data["runtime"]["image_router"] = False
    profile = validate_profile_data(copy.deepcopy(data))
    assert profile.character_swap == {}
    assert not any(key.startswith("character_swap.") for key, _, _ in declared_workflows(profile))
    assert not any(item.role.startswith("swap.") for item in profile.model_requirements)
    data["character_swap"].pop("depth")
    with pytest.raises(ProfileError, match="character_swap.depth"):
        validate_profile_data(data)
