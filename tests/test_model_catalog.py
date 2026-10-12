"""The model catalog: canonical ids, aliases, and what it refuses to install.

Loading a manifest is pure. Nothing here downloads, pulls or writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.model_catalog import (
    ModelCatalog,
    ModelCatalogError,
    load_model_catalog,
    model_catalog,
    validate_model_manifest,
)
from local_ai_stack.models import (
    FileModelSource,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelProvider,
    OllamaModelSource,
)
from local_ai_stack.profiles import load_profiles

REPO = Path(__file__).resolve().parents[1]
SHA = "a" * 64


def manifest(**sections) -> dict:
    return {"schema_version": 1, **sections}


def file_entry(**overrides) -> dict:
    entry = {
        "kind": "diffusion",
        "placement": "diffusion_models",
        "filename": "model.safetensors",
        "url": "https://example.invalid/model.safetensors",
        "sha256": SHA,
    }
    entry.update(overrides)
    return entry


def ollama_entry(**overrides) -> dict:
    entry = {"kind": "chat", "reference": "demo:8b"}
    entry.update(overrides)
    return entry


# --- ids canonicos ---------------------------------------------------------


def test_ids_are_canonical_provider_slash_name():
    catalog = validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry()}))
    assert set(catalog.specs) == {ModelId("ollama/demo_8b")}
    assert ModelId("ollama/demo_8b").provider is ModelProvider.OLLAMA


def test_the_shipped_catalog_loads():
    catalog = model_catalog()
    assert catalog.specs
    assert all(isinstance(model_id, ModelId) for model_id in catalog.declared_ids)


def test_a_name_that_is_not_a_valid_model_name_is_rejected():
    with pytest.raises(ModelCatalogError):
        validate_model_manifest(manifest(ollama={"Demo": ollama_entry()}))


def test_an_unknown_provider_section_is_rejected():
    with pytest.raises(ModelCatalogError, match="unknown fields"):
        validate_model_manifest(manifest(vllm={"demo": {}}))


def test_a_wrong_schema_version_is_rejected():
    with pytest.raises(ModelCatalogError, match="schema_version"):
        validate_model_manifest({"schema_version": 99, "ollama": {}})


# --- aliases ---------------------------------------------------------------


def test_an_alias_normalizes_to_the_canonical_id():
    catalog = validate_model_manifest(
        manifest(ollama={"demo_8b": ollama_entry(aliases=["ollama/demo-8b"])})
    )
    assert catalog.normalize("ollama/demo-8b") == ModelId("ollama/demo_8b")
    assert catalog.normalize("ollama/demo_8b") == ModelId("ollama/demo_8b")


def test_a_list_of_aliases_is_the_declared_shape():
    catalog = validate_model_manifest(
        manifest(ollama={"demo_8b": ollama_entry(aliases=["ollama/legacy-a", "ollama/legacy-b"])})
    )
    assert set(catalog.aliases) == {"ollama/legacy-a", "ollama/legacy-b"}
    assert all(target == ModelId("ollama/demo_8b") for target in catalog.aliases.values())
    # Both spellings land on the one canonical id; neither becomes a spec.
    assert set(catalog.specs) == {ModelId("ollama/demo_8b")}


@pytest.mark.parametrize(
    "value, described",
    [
        ("legacy", "str"),
        (123, "int"),
        ({}, "dict"),
        ({"ollama/legacy": 1}, "dict"),
        (("ollama/legacy",), "tuple"),
        (True, "bool"),
    ],
)
def test_a_non_list_aliases_declaration_is_rejected(value, described):
    """A bare string is iterable, so accepting one would register a character
    per alias. Only a real list is a list of aliases."""
    with pytest.raises(ModelCatalogError, match="must be a list of strings") as exc:
        validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry(aliases=value)}))
    assert described in str(exc.value)
    assert "aliases" in str(exc.value)


def test_a_scalar_string_alias_is_never_iterated_into_characters():
    with pytest.raises(ModelCatalogError) as exc:
        validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry(aliases="legacy")}))
    message = str(exc.value)
    # The corrective hint names the whole word, never its letters.
    assert "- legacy" in message
    assert "'l'" not in message


@pytest.mark.parametrize("value", [[], None])
def test_no_aliases_stays_valid(value):
    """An absent or empty declaration is simply no alias, as it always was."""
    catalog = validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry(aliases=value)}))
    assert catalog.aliases == {}
    assert catalog.get(ModelId("ollama/demo_8b")) is not None


def test_a_non_string_inside_the_alias_list_is_rejected():
    with pytest.raises(ModelCatalogError, match="aliases\\[1\\]"):
        validate_model_manifest(
            manifest(ollama={"demo_8b": ollama_entry(aliases=["ollama/ok", 7])})
        )


def test_an_alias_is_not_a_second_namespace():
    """Only the canonical id exists as a spec; the alias is an input spelling."""
    catalog = validate_model_manifest(
        manifest(ollama={"demo_8b": ollama_entry(aliases=["ollama/demo-8b"])})
    )
    assert set(catalog.specs) == {ModelId("ollama/demo_8b")}
    assert catalog.get(catalog.normalize("ollama/demo-8b")) is not None


def test_an_alias_claimed_twice_is_rejected():
    with pytest.raises(ModelCatalogError, match="already claimed"):
        validate_model_manifest(
            manifest(
                ollama={
                    "one_8b": ollama_entry(aliases=["ollama/shared"]),
                    "two_8b": ollama_entry(aliases=["ollama/shared"]),
                }
            )
        )


def test_an_alias_colliding_with_a_canonical_id_is_rejected():
    with pytest.raises(ModelCatalogError, match="collides"):
        validate_model_manifest(
            manifest(
                ollama={
                    "one_8b": ollama_entry(aliases=["ollama/two_8b"]),
                    "two_8b": ollama_entry(),
                }
            )
        )


def test_the_shipped_aliases_all_resolve():
    catalog = model_catalog()
    for alias, target in catalog.aliases.items():
        assert catalog.normalize(alias) == target
        assert catalog.is_declared(target)


# --- duplicatas ------------------------------------------------------------


def test_the_same_name_cannot_appear_twice_in_a_provider(tmp_path):
    """YAML would collapse duplicate keys, so this is checked through the file."""
    path = tmp_path / "models.yaml"
    path.write_text(
        "schema_version: 1\n"
        "ollama:\n"
        "  demo_8b:\n    kind: chat\n    reference: a:8b\n"
        "  demo_8b:\n    kind: chat\n    reference: b:8b\n",
        encoding="utf-8",
    )
    catalog = load_model_catalog(path)
    # The last one wins in YAML; what matters is that one id maps to one spec.
    assert len(catalog.specs) == 1
    assert catalog.require(ModelId("ollama/demo_8b")).source.reference == "b:8b"


def test_one_id_maps_to_exactly_one_spec():
    catalog = model_catalog()
    for model_id in catalog.specs:
        assert catalog.require(model_id).id == model_id


# --- ollama ----------------------------------------------------------------


def test_an_ollama_spec_carries_a_reference_and_no_path():
    spec = validate_model_manifest(
        manifest(ollama={"demo_8b": ollama_entry(digest="abc123")})
    ).require(ModelId("ollama/demo_8b"))
    assert isinstance(spec.source, OllamaModelSource)
    assert spec.source.reference == "demo:8b"
    assert spec.revision == "abc123"
    assert not hasattr(spec.source, "path")


@pytest.mark.parametrize("reference", ["demo:latest", "demo"])
def test_a_floating_ollama_reference_is_rejected(reference):
    with pytest.raises(ModelCatalogError, match="explicit tag"):
        validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry(reference=reference)}))


def test_a_non_hex_digest_is_rejected():
    with pytest.raises(ModelCatalogError, match="hexadecimal"):
        validate_model_manifest(manifest(ollama={"demo_8b": ollama_entry(digest="zzz")}))


def test_the_shipped_ollama_models_are_pinned_with_explicit_tags():
    for spec in model_catalog().for_provider(ModelProvider.OLLAMA):
        assert ":" in spec.source.reference
        assert not spec.source.reference.endswith(":latest")
        assert spec.source.digest


# --- comfyui ---------------------------------------------------------------


def test_a_file_spec_carries_an_artifact_and_a_placement():
    spec = validate_model_manifest(manifest(comfyui={"demo": file_entry()})).require(
        ModelId("comfyui/demo")
    )
    assert isinstance(spec.source, FileModelSource)
    assert spec.source.placement is ModelPlacement.DIFFUSION_MODELS
    assert spec.source.artifact.sha256 == SHA
    assert spec.kind is ModelKind.DIFFUSION


@pytest.mark.parametrize("missing", ["url", "sha256"])
def test_a_file_model_without_a_checksum_or_url_is_declared_but_not_installable(missing):
    entry = file_entry()
    entry[missing] = "TBD"
    catalog = validate_model_manifest(manifest(comfyui={"demo": entry}))
    model_id = ModelId("comfyui/demo")
    assert model_id not in catalog.specs
    assert catalog.is_declared(model_id)
    assert missing in catalog.reason_for(model_id)


def test_requiring_an_unpinned_model_says_why():
    catalog = validate_model_manifest(
        manifest(comfyui={"demo": file_entry(sha256="TBD", url="TBD")})
    )
    with pytest.raises(ModelCatalogError, match="not installable"):
        catalog.require(ModelId("comfyui/demo"))


def test_a_non_https_model_url_is_rejected():
    with pytest.raises(ModelCatalogError, match="https"):
        validate_model_manifest(
            manifest(comfyui={"demo": file_entry(url="http://example.invalid/m.safetensors")})
        )


def test_an_unknown_placement_is_rejected():
    with pytest.raises(ModelCatalogError, match="placement"):
        validate_model_manifest(manifest(comfyui={"demo": file_entry(placement="loras")}))


SHIPPED_COMFYUI = {
    "comfyui/flux2_klein_fp8": (
        "black-forest-labs/FLUX.2-klein-4b-fp8",
        "5b4408e59397a4a37ccb46afe426d8ed86379441",
        "flux-2-klein-4b-fp8.safetensors",
        "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
        4070624520,
    ),
    "comfyui/qwen_3_4b_encoder": (
        "Comfy-Org/flux2-klein-4B",
        "5f526678002e43af5551dadb73ce2e8c91b43afe",
        "qwen_3_4b.safetensors",
        "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a",
        8044982048,
    ),
    "comfyui/flux2_vae": (
        "Comfy-Org/flux2-dev",
        "ab9055628ea245000e610f2aa2c96f4746093546",
        "flux2-vae.safetensors",
        "d64f3a68e1cc4f9f4e29b6e0da38a0204fe9a49f2d4053f0ec1fa1ca02f9c4b5",
        336213556,
    ),
    "comfyui/flux2_klein_9b_fp8": (
        "black-forest-labs/FLUX.2-klein-9b-fp8",
        "902d9d510b51533e07729f19211414a3648b77d2",
        "flux-2-klein-9b-fp8.safetensors",
        "865ba09f5b4c3cbd3468a4bd3acb9fcb2f8740c54317482f0bcd4ed1d3655cee",
        9433061528,
    ),
    "comfyui/qwen_3_8b_fp8mixed_encoder": (
        "Comfy-Org/vae-text-encorder-for-flux-klein-9b",
        "3f62d9d8ae1fec33c6e91453d5c712855b096b55",
        "qwen_3_8b_fp8mixed.safetensors",
        "abad16806e0cbabc54e0325d6565847443fe396d5f0be38bb3cd3fe75a1201d6",
        8664848742,
    ),
    "comfyui/flux2_small_decoder": (
        "black-forest-labs/FLUX.2-small-decoder",
        "a3efc24f613ef42d9428af62fdbd6f5fd8856c4a",
        "full_encoder_small_decoder.safetensors",
        "ea4273f02d1fafbf8e1d1c2cf6018ed8748652eb0bf34f2dd91171f16f15ab62",
        249519092,
    ),
}


def test_nothing_shipped_is_left_unpinned():
    """Every declared model is installable; no TBD survives."""
    assert model_catalog().unpinned == {}


@pytest.mark.parametrize("model_id, expected", SHIPPED_COMFYUI.items())
def test_each_comfyui_model_is_pinned_to_its_verified_artifact(model_id, expected):
    repository, commit, filename, sha256, size = expected
    spec = model_catalog().require(ModelId(model_id))
    artifact = spec.source.artifact

    assert artifact.filename == filename
    assert artifact.sha256 == sha256
    assert artifact.size_bytes == size
    # The source repository is named, so a same-named file elsewhere cannot
    # quietly take its place.
    assert f"huggingface.co/{repository}/" in artifact.url
    assert commit in artifact.url
    assert spec.revision == commit


@pytest.mark.parametrize("model_id", SHIPPED_COMFYUI)
def test_every_comfyui_url_is_pinned_to_a_commit_not_a_branch(model_id):
    """An immutable /resolve/<commit>/ URL, never a moving branch."""
    url = model_catalog().require(ModelId(model_id)).source.artifact.url
    assert "/resolve/" in url
    revision = url.split("/resolve/", 1)[1].split("/", 1)[0]
    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)
    assert "/resolve/main/" not in url
    assert "/blob/" not in url


def test_the_vae_comes_from_the_repository_the_tutorial_names():
    """A file with this exact name also exists in flux2-klein-4B with other
    bytes, so the source is part of the pin, not an implementation detail."""
    artifact = model_catalog().require(ModelId("comfyui/flux2_vae")).source.artifact
    assert "Comfy-Org/flux2-dev" in artifact.url
    assert "flux2-klein" not in artifact.url
    assert artifact.sha256 != "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3"


def test_the_diffusion_model_is_the_distilled_variant_not_the_base():
    artifact = model_catalog().require(ModelId("comfyui/flux2_klein_fp8")).source.artifact
    assert artifact.filename == "flux-2-klein-4b-fp8.safetensors"
    assert "klein-base" not in artifact.url


def test_placements_are_unchanged():
    catalog = model_catalog()
    assert catalog.require(ModelId("comfyui/flux2_klein_fp8")).source.placement is (
        ModelPlacement.DIFFUSION_MODELS
    )
    assert catalog.require(ModelId("comfyui/qwen_3_4b_encoder")).source.placement is (
        ModelPlacement.TEXT_ENCODERS
    )
    assert catalog.require(ModelId("comfyui/flux2_vae")).source.placement is ModelPlacement.VAE


def test_the_shipped_model_ids_are_unchanged():
    catalog = model_catalog()
    comfy = {str(m) for m in catalog.declared_ids if m.provider is ModelProvider.COMFYUI}
    assert comfy == set(SHIPPED_COMFYUI)


def test_every_pinned_file_model_has_a_checksum():
    for spec in model_catalog().for_provider(ModelProvider.COMFYUI):
        assert spec.source.artifact.has_checksum


# --- profiles --------------------------------------------------------------


def test_every_profile_model_id_is_declared_by_the_catalog():
    catalog = model_catalog()
    for profile in load_profiles(REPO / "profiles"):
        for requirement in profile.model_requirements:
            assert catalog.is_declared(requirement.model_id), (
                f"{profile.id}:{requirement.role} -> {requirement.model_id}"
            )


def test_loading_the_catalog_opens_no_socket(monkeypatch):
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - deve nunca rodar
        raise AssertionError("loading the catalog must not touch the network")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "stream", explode)
    assert model_catalog().specs


def test_an_empty_catalog_is_a_valid_value():
    empty = ModelCatalog()
    assert empty.declared_ids == frozenset()
    assert empty.get(ModelId("ollama/demo_8b")) is None
