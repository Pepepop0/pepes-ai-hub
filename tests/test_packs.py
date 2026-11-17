"""Optional files extend the public resources without replacing them."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from local_ai_stack.comfyui_retention import managed_output_prefixes
from local_ai_stack.model_catalog import ModelCatalogError, load_model_catalog
from local_ai_stack.profiles import ProfileError, load_profiles, validate_profile_data
from local_ai_stack.workflows import WorkflowError, load_workflow_manifest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "apply_pack_under_test", ROOT / "scripts/apply_pack.py"
)
pack_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_script)


def put(root: Path, relative: str, value) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def base_profile() -> dict:
    return yaml.safe_load((ROOT / "profiles/pepe.yaml").read_text(encoding="utf-8"))


def test_overlays_deep_merge_maps_and_variants_by_id_without_becoming_profiles(tmp_path):
    data = base_profile()
    data["models"]["studio"] = copy.deepcopy(data["models"]["image"])
    data["variants"] = [
        {
            "id": "studio",
            "name": "Studio",
            "images": "studio",
            "workflows": copy.deepcopy(data["workflows"]),
        }
    ]
    data["runtime"]["image_router"] = True
    put(tmp_path, "base.yaml", data)
    put(
        tmp_path,
        "first.overlay.yaml",
        {
            "extends": data["id"],
            "models": {"chat": {"params": {"think": False}}},
            "variants": [{"id": "studio", "behavior": {"router_rules": ["Keep the canvas."]}}],
        },
    )
    put(
        tmp_path,
        "second.overlay.yaml",
        {
            "extends": data["id"],
            "models": {"second": copy.deepcopy(data["models"]["image"])},
            "variants": [
                {
                    "id": "second",
                    "name": "Second",
                    "images": "second",
                    "workflows": copy.deepcopy(data["workflows"]),
                }
            ],
        },
    )
    (profile,) = load_profiles(tmp_path)
    assert profile.id == data["id"] and profile.model_for("chat.primary") is not None
    assert profile.chat_params == {"think": False}
    assert [variant.id for variant in profile.variants] == ["studio", "second"]
    assert profile.variants[0].behavior.router_rules == ("Keep the canvas.",)
    assert profile.variants[0].workflows
    assert yaml.safe_load((tmp_path / "base.yaml").read_text("utf-8")) == data


def test_orphan_overlay_is_an_error(tmp_path):
    put(tmp_path, "missing.overlay.yaml", {"extends": "missing"})
    with pytest.raises(ProfileError, match="no profile with id"):
        load_profiles(tmp_path)


def test_behavior_names_an_existing_adapter_role_and_rejects_unknown_fields():
    data = base_profile()
    data["behavior"] = {
        "router_rules": ["Keep the scene lighting."],
        "poses": {"standing": {"adapter": "diffusion", "prompt": "Stand upright."}},
    }
    behavior = validate_profile_data(data).behavior
    assert behavior.poses["standing"].adapter == "diffusion"
    data["behavior"]["poses"]["standing"]["adapter"] = "missing"
    with pytest.raises(ProfileError, match="must name a role"):
        validate_profile_data(data)
    data["behavior"] = {"unknown": True}
    with pytest.raises(ProfileError, match="unknown fields"):
        validate_profile_data(data)


@pytest.mark.parametrize("kind", ["models", "workflows"])
def test_manifest_extras_are_loaded_and_duplicate_ids_are_errors(tmp_path, kind):
    source = yaml.safe_load((ROOT / f"manifests/{kind}.yaml").read_text("utf-8"))
    section = "ollama" if kind == "models" else "workflows"
    identifier, record = next(iter(source[section].items()))
    main = put(tmp_path, f"{kind}.yaml", {"schema_version": 1, section: {identifier: record}})
    extra = {"schema_version": 1, section: {"extra": copy.deepcopy(record)}}
    if kind == "models":
        extra[section]["extra"].pop("aliases", None)
    put(tmp_path, f"{kind}.d/extra.yaml", extra)
    loader = load_model_catalog if kind == "models" else load_workflow_manifest
    result = loader(main)
    keys = {str(key) for key in result.declared_ids} if kind == "models" else set(result)
    assert ("ollama/extra" if kind == "models" else "extra") in keys
    put(tmp_path, f"{kind}.d/extra.yaml", {"schema_version": 1, section: {identifier: record}})
    error = ModelCatalogError if kind == "models" else WorkflowError
    with pytest.raises(error, match="already declared"):
        loader(main)


@pytest.mark.parametrize("kind", ["models", "workflows"])
def test_extra_manifests_receive_the_same_root_validation(tmp_path, kind):
    source = yaml.safe_load((ROOT / f"manifests/{kind}.yaml").read_text("utf-8"))
    main = put(tmp_path, f"{kind}.yaml", source)
    section = "ollama" if kind == "models" else "workflows"
    put(tmp_path, f"{kind}.d/extra.yaml", {"schema_version": True, section: {}})
    loader = load_model_catalog if kind == "models" else load_workflow_manifest
    error = ModelCatalogError if kind == "models" else WorkflowError
    with pytest.raises(error):
        loader(main)


def test_apply_pack_is_idempotent_and_archival_originals_are_never_installed(tmp_path):
    put(tmp_path, "packs/studio/profiles/studio.overlay.yaml", {"extends": "pepe"})
    put(tmp_path, "packs/studio/archive/docs/original.yaml", {"private": "original"})
    put(tmp_path, "packs/studio/notes.yaml", {"note": "not installable"})
    assert pack_script.apply_pack("studio", tmp_path) == [Path("profiles/studio.overlay.yaml")]
    assert pack_script.apply_pack("studio", tmp_path) == []
    assert not (tmp_path / "archive").exists() and not (tmp_path / "docs/original.yaml").exists()
    assert not (tmp_path / "notes.yaml").exists()


def test_collision_preflight_writes_nothing_and_preserves_public_bytes(tmp_path):
    put(tmp_path, "profiles/base.yaml", {"public": True})
    put(tmp_path, "packs/studio/profiles/base.yaml", {"public": False})
    put(tmp_path, "packs/studio/manifests/models.d/extra.yaml", {"schema_version": 1})
    before = (tmp_path / "profiles/base.yaml").read_bytes()
    with pytest.raises(pack_script.PackError, match="collides"):
        pack_script.apply_pack("studio", tmp_path)
    assert (tmp_path / "profiles/base.yaml").read_bytes() == before
    assert not (tmp_path / "manifests").exists()


def test_collision_with_a_parent_file_is_detected_before_copying(tmp_path):
    (tmp_path / "profiles").write_text("not a directory", encoding="utf-8")
    put(tmp_path, "packs/studio/profiles/new.yaml", {"extends": "pepe"})
    with pytest.raises(pack_script.PackError, match="collides"):
        pack_script.apply_pack("studio", tmp_path)


@pytest.mark.parametrize("name", ["../studio", "studio/other", "", "C:studio"])
def test_pack_names_cannot_escape_the_pack_tree(tmp_path, name):
    with pytest.raises(pack_script.PackError, match="invalid pack name"):
        pack_script.apply_pack(name, tmp_path)


def test_retention_only_owns_safe_saveimage_prefixes(tmp_path):
    source = yaml.safe_load((ROOT / "manifests/workflows.yaml").read_text("utf-8"))
    record = copy.deepcopy(next(iter(source["workflows"].values())))
    record["file"] = "workflows/custom.json"
    put(
        tmp_path, "manifests/workflows.yaml", {"schema_version": 1, "workflows": {"custom": record}}
    )
    graph = {
        "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "Pack/owned"}},
        "2": {"class_type": "Other", "inputs": {"filename_prefix": "not_owned"}},
        "3": {"class_type": "SaveImage", "inputs": {"filename_prefix": "../outside"}},
        "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "/absolute"}},
    }
    path = tmp_path / record["file"]
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(graph), encoding="utf-8")
    assert managed_output_prefixes(tmp_path) == ((), {"Pack": ("owned",)})


@pytest.mark.parametrize("linked_tree", ["source", "destination"])
def test_pack_refuses_links_before_writing(tmp_path, linked_tree):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.yaml").write_text("original", encoding="utf-8")
    pack = tmp_path / "packs/studio"
    pack.mkdir(parents=True)
    link = pack / "profiles" if linked_tree == "source" else tmp_path / "profiles"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    if linked_tree == "destination":
        put(tmp_path, "packs/studio/profiles/model.yaml", {"new": True})
    with pytest.raises(pack_script.PackError, match="links/reparse"):
        pack_script.apply_pack("studio", tmp_path)
    assert (outside / "model.yaml").read_text("utf-8") == "original"


def test_linux_bundle_omits_pack_sources_and_includes_only_selected_pack_resources(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "build_linux_under_test", ROOT / "scripts/build_linux.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    for directory in builder.TREES:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for relative in {*builder.FILES, *builder.REQUIRED, "desktop-ui/dist/assets/app.js"}:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source", encoding="utf-8")
    put(tmp_path, "packs/studio/profiles/studio.overlay.yaml", {"extends": "capella"})
    put(tmp_path, "packs/studio/manifests/models.d/studio.yaml", {"schema_version": 1})
    put(tmp_path, "packs/studio/archive/docs/original.yaml", {"private": True})
    public = builder.source_files()
    assert "profiles/studio.overlay.yaml" not in public
    private = builder.source_files("studio")
    assert "profiles/studio.overlay.yaml" in private
    assert "manifests/models.d/studio.yaml" in private
    assert private["profiles/capella.yaml"] == tmp_path / "profiles/capella.yaml"
    assert all(not name.startswith(("packs/", "archive/")) for name in private)
