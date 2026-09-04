from pathlib import Path

import pytest
import yaml

from local_ai_stack.artifacts import Artifact
from local_ai_stack.layout import StackLayout
from local_ai_stack.models import (
    FileModelLocation,
    FileModelSource,
    ManagedModelPaths,
    ModelContractError,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelProvider,
    ModelResolver,
    ModelSpec,
    ModelState,
    ModelUnavailableError,
    OllamaModelLocation,
    OllamaModelSource,
    ResolvedModel,
)
from local_ai_stack.profiles import load_profiles

ROOT = Path(__file__).resolve().parents[1]


def file_spec() -> ModelSpec:
    return ModelSpec(
        id=ModelId("comfyui/flux2_klein_fp8"),
        kind=ModelKind.DIFFUSION,
        source=FileModelSource(
            artifact=Artifact(
                id="flux2-klein-fp8",
                url="https://example.invalid/flux.safetensors",
                filename="flux.safetensors",
                sha256="a" * 64,
                version="1",
            ),
            placement=ModelPlacement.DIFFUSION_MODELS,
        ),
    )


def test_model_id_has_one_canonical_cross_layer_namespace():
    model_id = ModelId.from_parts(ModelProvider.OLLAMA, "hermes3_8b")
    assert str(model_id) == "ollama/hermes3_8b"
    assert model_id.provider is ModelProvider.OLLAMA
    assert model_id.name == "hermes3_8b"


@pytest.mark.parametrize(
    "value",
    ["hermes3:8b", "ollama//hermes", "OLLAMA/hermes", "unknown/hermes", "ollama/../x"],
)
def test_noncanonical_model_ids_are_rejected(value):
    with pytest.raises(ModelContractError):
        ModelId(value)


def test_kind_is_metadata_not_part_of_model_identity():
    model_id = ModelId("comfyui/shared_model")
    diffusion = ModelSpec(
        model_id,
        ModelKind.DIFFUSION,
        FileModelSource(
            Artifact("one", "https://example.invalid/a", "a.safetensors"),
            ModelPlacement.DIFFUSION_MODELS,
        ),
    )
    auxiliary = ModelSpec(model_id, ModelKind.AUXILIARY, diffusion.source)
    assert diffusion.id == auxiliary.id


def test_spec_rejects_provider_source_mismatch():
    with pytest.raises(ModelContractError, match="does not match"):
        ModelSpec(
            ModelId("ollama/not_a_file"),
            ModelKind.CHAT,
            file_spec().source,
        )


def test_file_model_path_policy_is_single_direct_placement_and_read_only(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    destination = ManagedModelPaths.from_layout(layout).destination_for(file_spec())
    assert destination == (layout.models / "comfyui" / "diffusion_models" / "flux.safetensors")
    assert not layout.root.exists()


def test_ollama_spec_has_a_provider_reference_and_no_artificial_path(tmp_path):
    spec = ModelSpec(
        ModelId("ollama/hermes3_8b"),
        ModelKind.CHAT,
        OllamaModelSource("hermes3:8b", digest="sha256:123"),
    )
    assert spec.provider is ModelProvider.OLLAMA
    assert spec.revision == "sha256:123"
    with pytest.raises(ModelContractError, match="no managed file destination"):
        ManagedModelPaths(tmp_path.resolve()).destination_for(spec)


def test_available_file_model_carries_absolute_path_and_runtime_name(tmp_path):
    path = (tmp_path / "models" / "flux.safetensors").resolve()
    resolved = ResolvedModel(
        ModelId("comfyui/flux2_klein_fp8"),
        ModelKind.DIFFUSION,
        ModelState.AVAILABLE,
        FileModelLocation(path, "flux.safetensors", ModelPlacement.DIFFUSION_MODELS),
        revision="1",
    )
    location = resolved.require_location()
    assert resolved.provider is ModelProvider.COMFYUI
    assert isinstance(location, FileModelLocation)
    assert location.path == path
    assert location.runtime_name == "flux.safetensors"


def test_available_ollama_model_carries_only_the_provider_reference():
    resolved = ResolvedModel(
        ModelId("ollama/hermes3_8b"),
        ModelKind.CHAT,
        ModelState.AVAILABLE,
        OllamaModelLocation("hermes3:8b"),
    )
    location = resolved.require_location()
    assert isinstance(location, OllamaModelLocation)
    assert location.reference == "hermes3:8b"
    assert not hasattr(location, "path")


@pytest.mark.parametrize(
    "state", [ModelState.NOT_INSTALLED, ModelState.INVALID, ModelState.EXTERNAL]
)
def test_unavailable_model_never_exposes_a_workflow_location(state):
    resolved = ResolvedModel(
        ModelId("comfyui/flux2_klein_fp8"),
        ModelKind.DIFFUSION,
        state,
        detail="not consumable",
    )
    assert not resolved.available
    with pytest.raises(ModelUnavailableError):
        resolved.require_location()


def test_non_available_state_cannot_smuggle_a_location(tmp_path):
    with pytest.raises(ModelContractError, match="must not expose"):
        ResolvedModel(
            ModelId("comfyui/flux2_klein_fp8"),
            ModelKind.DIFFUSION,
            ModelState.INVALID,
            FileModelLocation(
                Path(tmp_path / "flux.safetensors").resolve(),
                "flux.safetensors",
                ModelPlacement.DIFFUSION_MODELS,
            ),
        )


def test_model_resolver_protocol_is_the_only_ic5_surface_ic6_needs():
    class Resolver:
        def resolve(self, model_id: ModelId) -> ResolvedModel:
            return ResolvedModel(model_id, ModelKind.CHAT, ModelState.NOT_INSTALLED)

    assert isinstance(Resolver(), ModelResolver)


def test_profile_and_manifest_layers_use_the_same_canonical_ids():
    data = yaml.safe_load((ROOT / "manifests" / "models.yaml").read_text(encoding="utf-8"))
    declared = {
        ModelId(f"{provider}/{name}")
        for provider in ("ollama", "comfyui")
        for name in data[provider]
    }
    required = {
        requirement.model_id
        for profile in load_profiles(ROOT / "profiles")
        for requirement in profile.model_requirements
    }
    assert required <= declared
