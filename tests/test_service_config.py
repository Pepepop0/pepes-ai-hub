from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import (
    DIFFUSION_ID,
    FIXTURE_GRAPH,
    IMAGE_MODELS,
    FakeResolver,
    available_file_model,
)

from local_ai_stack.components import ComponentId
from local_ai_stack.endpoints import EndpointError, StackEndpoints, base_url
from local_ai_stack.layout import StackLayout
from local_ai_stack.managed_config import ManagedConfigWriter
from local_ai_stack.models import (
    FileModelLocation,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelState,
    OllamaModelLocation,
    ResolvedModel,
)
from local_ai_stack.profiles import load_profile
from local_ai_stack.service_config import (
    ENV_COMFYUI_BASE_URL,
    ENV_COMFYUI_WORKFLOW_NODES,
    ENV_DEFAULT_MODELS,
    ENV_ENABLE_IMAGE_GENERATION,
    ENV_IMAGE_GENERATION_MODEL,
    ENV_OLLAMA_BASE_URL,
    build_stack_configuration,
    comfyui_search_paths,
    comfyui_search_paths_document,
    open_webui_chat_configuration,
    open_webui_image_configuration,
    open_webui_ollama_env,
    open_webui_workflow_nodes,
)
from local_ai_stack.workflows import load_workflow_manifest, resolve_workflow

REPO = Path(__file__).resolve().parents[1]
CHAT_ID = ModelId("ollama/hermes3_8b")


@pytest.fixture
def layout(tmp_path) -> StackLayout:
    return StackLayout.from_root(tmp_path / "stack")


@pytest.fixture
def specs():
    return load_workflow_manifest(REPO / "manifests" / "workflows.yaml")


def profile_named(name: str):
    return load_profile(REPO / "profiles" / f"{name}.yaml")


def available_chat() -> ResolvedModel:
    return ResolvedModel(
        CHAT_ID, ModelKind.CHAT, ModelState.AVAILABLE, OllamaModelLocation("hermes3:8b")
    )


def full_resolver(tmp_path, *, chat: ResolvedModel | None = None) -> FakeResolver:
    """Every role the Pepe profile needs. ``chat`` overrides the primary model."""
    models: dict[ModelId, ResolvedModel] = {
        model_id: available_file_model(tmp_path, model_id) for model_id in IMAGE_MODELS
    }
    models[CHAT_ID] = available_chat() if chat is None else chat
    return FakeResolver(models)


def resolver_for(profile, tmp_path) -> FakeResolver:
    """Resolve whatever *this* profile asks for, whichever ids those are.

    Stands in for IC5: an Ollama role becomes a provider reference, a ComfyUI
    role becomes a managed file. Nothing here knows which profile it is.
    """
    models: dict[ModelId, ResolvedModel] = {}
    for requirement in profile.model_requirements:
        model_id = requirement.model_id
        if model_id.provider.value == ComponentId.OLLAMA:
            models[model_id] = ResolvedModel(
                model_id,
                ModelKind.CHAT,
                ModelState.AVAILABLE,
                OllamaModelLocation(model_id.name.replace("_", "-")),
            )
            continue
        placement = {
            "image.diffusion": ModelPlacement.DIFFUSION_MODELS,
            "image.text_encoder": ModelPlacement.TEXT_ENCODERS,
            "image.vae": ModelPlacement.VAE,
        }[requirement.role]
        path = tmp_path / "models" / "comfyui" / placement.value / f"{model_id.name}.safetensors"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        models[model_id] = ResolvedModel(
            model_id,
            ModelKind.AUXILIARY,
            ModelState.AVAILABLE,
            FileModelLocation(path, path.name, placement),
        )
    return FakeResolver(models)


def ready_t2i(specs, profile, tmp_path):
    template = tmp_path / "workflows" / "flux2-klein-t2i-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(FIXTURE_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
    return resolve_workflow(
        specs["flux2_klein_t2i"], profile, full_resolver(tmp_path), repo_root=tmp_path
    )


# --- endpoints are derived once ---------------------------------------------


def test_endpoints_come_from_the_profile_not_from_a_constant():
    pepe = profile_named("pepe")
    endpoints = StackEndpoints.from_profile(pepe)
    assert endpoints.ollama == "http://127.0.0.1:11434"
    assert endpoints.comfyui == "http://127.0.0.1:8188"
    assert endpoints.port(ComponentId.OPEN_WEBUI) == pepe.services["open-webui"].port


def test_moving_a_port_in_the_profile_moves_every_url():
    profile = profile_named("pepe")
    moved = replace(
        profile,
        services={**profile.services, "ollama": replace(profile.services["ollama"], port=21434)},
    )
    endpoints = StackEndpoints.from_profile(moved)
    assert endpoints.ollama == "http://127.0.0.1:21434"
    assert open_webui_ollama_env(endpoints)[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:21434"


def test_a_health_path_never_leaks_into_a_base_url():
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    spec = endpoints.spec(ComponentId.OLLAMA)
    assert spec.endpoint.endswith("/api/version")
    assert base_url(spec) == "http://127.0.0.1:11434"


def test_an_unknown_service_is_a_clear_error():
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    with pytest.raises(EndpointError, match="unknown service"):
        endpoints.spec("stable-diffusion")


# --- Open WebUI -> Ollama ----------------------------------------------------


def test_open_webui_is_pointed_at_the_stack_ollama():
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    env = open_webui_ollama_env(endpoints)
    assert env[ENV_OLLAMA_BASE_URL] == endpoints.ollama
    assert env["ENABLE_OLLAMA_API"] == "true"


def test_an_available_primary_model_becomes_the_default_model():
    """The provider reference IC5 verified, used verbatim."""
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    configuration = open_webui_chat_configuration(endpoints, available_chat())

    assert configuration.ok
    assert configuration.env[ENV_DEFAULT_MODELS] == "hermes3:8b"
    assert configuration.env[ENV_OLLAMA_BASE_URL] == endpoints.ollama


@pytest.mark.parametrize(
    "state", [ModelState.NOT_INSTALLED, ModelState.INVALID, ModelState.EXTERNAL]
)
def test_a_primary_model_that_is_not_available_blocks_chat(state):
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    unusable = ResolvedModel(CHAT_ID, ModelKind.CHAT, state, detail="not ready")

    configuration = open_webui_chat_configuration(endpoints, unusable)

    assert not configuration.ok
    assert configuration.env == {}, "nothing is configured on a model we cannot use"
    assert state.value in configuration.problems[0]


def test_a_profile_without_a_primary_model_blocks_chat():
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    configuration = open_webui_chat_configuration(endpoints, None)
    assert not configuration.ok
    assert configuration.env == {}
    assert "no primary chat model" in configuration.problems[0]


def test_the_chat_configuration_never_carries_a_path(tmp_path):
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    configuration = open_webui_chat_configuration(endpoints, available_chat())
    for value in configuration.env.values():
        assert "\\" not in value and "/models/" not in value


def test_the_ollama_model_is_used_as_a_provider_reference(tmp_path, layout, specs):
    """An Ollama model is a reference; it never becomes a path."""
    resolver = full_resolver(tmp_path)
    chat = resolver.resolve(CHAT_ID)
    location = chat.require_location()
    assert isinstance(location, OllamaModelLocation)
    assert location.reference == "hermes3:8b"
    assert not hasattr(location, "path")


# --- ComfyUI search paths ----------------------------------------------------


def test_comfyui_is_pointed_at_the_directories_the_resolver_reported(tmp_path):
    resolver = full_resolver(tmp_path)
    models = [resolver.resolve(model_id) for model_id in resolver.models]
    base, directories = comfyui_search_paths(models)

    assert base == "managed"
    assert set(directories) == {"diffusion_models", "text_encoders", "vae"}
    expected = tmp_path / "models" / "comfyui" / "diffusion_models"
    assert directories["diffusion_models"] == str(expected)


def test_an_unavailable_model_contributes_no_search_path(tmp_path):
    missing = ResolvedModel(DIFFUSION_ID, ModelKind.DIFFUSION, ModelState.NOT_INSTALLED)
    _, directories = comfyui_search_paths([missing])
    assert directories == {}


def test_the_search_path_document_lands_in_the_comfyui_data_directory(tmp_path, layout):
    resolver = full_resolver(tmp_path)
    models = [resolver.resolve(model_id) for model_id in resolver.models]
    document = comfyui_search_paths_document(layout, models)
    assert document is not None
    assert document.path == layout.comfyui_data / "extra_model_paths.yaml"
    assert "diffusion_models:" in document.body


# --- Open WebUI -> ComfyUI ---------------------------------------------------


def test_the_node_mapping_matches_the_shape_open_webui_reads(specs, tmp_path):
    """Open WebUI 0.11.3 reads a list of {type, key, node_ids} entries."""
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)
    nodes = open_webui_workflow_nodes(resolution)

    assert isinstance(nodes, list)
    assert all(set(entry) == {"type", "key", "node_ids"} for entry in nodes)
    assert all(isinstance(entry["node_ids"], list) for entry in nodes)
    assert all(isinstance(node_id, str) for entry in nodes for node_id in entry["node_ids"])
    assert {entry["type"] for entry in nodes} == {
        "prompt",
        "model",
        "width",
        "height",
        "steps",
        "seed",
    }
    by_type = {entry["type"]: entry for entry in nodes}
    assert by_type["prompt"] == {"type": "prompt", "key": "value", "node_ids": ["76"]}
    assert by_type["model"] == {"type": "model", "key": "unet_name", "node_ids": ["75:70"]}


def test_the_node_mapping_env_value_is_a_json_list(specs, layout, tmp_path):
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)
    graph = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))

    configuration = open_webui_image_configuration(layout, endpoints, resolution, graph)

    payload = json.loads(configuration.env[ENV_COMFYUI_WORKFLOW_NODES])
    assert isinstance(payload, list)
    assert payload == open_webui_workflow_nodes(resolution)


def test_image_configuration_uses_the_runtime_name_and_the_comfyui_url(specs, layout, tmp_path):
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)
    graph = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))

    configuration = open_webui_image_configuration(layout, endpoints, resolution, graph)

    assert configuration.ok
    assert configuration.env[ENV_ENABLE_IMAGE_GENERATION] == "true"
    assert configuration.env[ENV_COMFYUI_BASE_URL] == endpoints.comfyui
    assert configuration.env[ENV_IMAGE_GENERATION_MODEL] == resolution.runtime_name
    assert str(resolution.model_path) not in json.dumps(dict(configuration.env))


def test_image_configuration_refuses_an_unusable_workflow(specs, layout):
    profile = profile_named("pepe")
    endpoints = StackEndpoints.from_profile(profile)
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, FakeResolver())

    configuration = open_webui_image_configuration(layout, endpoints, resolution)

    assert not configuration.ok
    assert configuration.env == {}
    assert configuration.documents == ()
    assert "not configurable" in configuration.problems[0]


def test_a_missing_graph_never_enables_image_generation(specs, layout, tmp_path):
    """Fail closed: without our graph, Open WebUI would submit its own."""
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)

    configuration = open_webui_image_configuration(layout, endpoints, resolution, None)

    assert not configuration.ok
    assert ENV_ENABLE_IMAGE_GENERATION not in configuration.env
    assert configuration.env == {}
    assert configuration.documents == ()
    assert "default workflow" in configuration.problems[0]
    assert "API format" in configuration.manual_steps[0]


def test_a_stack_without_the_graph_does_not_enable_image_generation(specs, layout, tmp_path):
    configuration = build_stack_configuration(
        profile_named("pepe"),
        full_resolver(tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
    )
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert not configuration.ok
    assert ENV_ENABLE_IMAGE_GENERATION not in open_webui.env
    assert ENV_DEFAULT_MODELS in open_webui.env, "chat is configured independently"


# --- the whole stack ---------------------------------------------------------


def stage_graph(tmp_path) -> None:
    """Put the workflow graph where a repository checkout would have it."""
    template = tmp_path / "workflows" / "flux2-klein-t2i-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(FIXTURE_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")


def test_a_profile_produces_a_complete_configuration(specs, layout, tmp_path):
    profile = profile_named("pepe")
    stage_graph(tmp_path)
    configuration = build_stack_configuration(
        profile,
        full_resolver(tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))},
    )

    assert configuration.profile_id == "pepe"
    assert configuration.ok, configuration.problems
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert open_webui.env[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:11434"
    assert open_webui.env[ENV_DEFAULT_MODELS] == "hermes3:8b"
    assert open_webui.env[ENV_COMFYUI_BASE_URL] == "http://127.0.0.1:8188"
    assert any(doc.path.name == "open-webui.env" for doc in open_webui.documents)
    assert any(doc.path.name == "extra_model_paths.yaml" for doc in configuration.documents)


def test_a_different_profile_produces_its_own_configuration(specs, layout, tmp_path):
    """Nothing branches on a profile id; the differences come from the files.

    Pepe has chosen a chat model, Capella has not yet (``primary: TBD``), and
    Test has chosen nothing at all. Each configuration says exactly that.
    """
    stage_graph(tmp_path)
    built = {}
    for name in ("pepe", "capella", "test"):
        profile = profile_named(name)
        built[name] = build_stack_configuration(
            profile,
            resolver_for(profile, tmp_path),
            layout,
            workflow_specs=specs,
            repo_root=tmp_path,
            graphs={"flux2_klein_t2i": json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))},
        )

    pepe = built["pepe"].service(ComponentId.OPEN_WEBUI)
    assert pepe.env[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:11434"
    assert pepe.env[ENV_DEFAULT_MODELS] == "hermes3-8b"
    assert pepe.env[ENV_ENABLE_IMAGE_GENERATION] == "true"

    capella = built["capella"].service(ComponentId.OPEN_WEBUI)
    assert ENV_DEFAULT_MODELS not in capella.env, "no chat model has been chosen yet"
    assert any("no primary chat model" in problem for problem in capella.problems)
    assert capella.env[ENV_ENABLE_IMAGE_GENERATION] == "true", "images still work"

    test = built["test"].service(ComponentId.OPEN_WEBUI)
    assert test.env == {}, "a profile that has chosen nothing configures nothing"
    assert built["test"].service(ComponentId.COMFYUI).documents == ()


def test_a_port_moved_in_a_profile_moves_the_generated_configuration(specs, layout, tmp_path):
    stage_graph(tmp_path)
    profile = profile_named("pepe")
    moved = replace(
        profile,
        services={**profile.services, "comfyui": replace(profile.services["comfyui"], port=9188)},
    )
    configuration = build_stack_configuration(
        moved,
        resolver_for(moved, tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))},
    )
    assert (
        configuration.env_for(ComponentId.OPEN_WEBUI)[ENV_COMFYUI_BASE_URL]
        == "http://127.0.0.1:9188"
    )


def test_no_profile_id_is_branched_on_in_the_configuration_layer():
    for name in ("service_config.py", "endpoints.py", "workflows.py", "managed_config.py"):
        text = (REPO / "src" / "local_ai_stack" / name).read_text(encoding="utf-8").lower()
        for profile_id in ("pepe", "capella"):
            assert profile_id not in text, f"{name} mentions {profile_id}"


def test_an_unavailable_model_removes_the_capability_it_supported(specs, layout, tmp_path):
    """Fail closed: the stack configures less, rather than configuring a lie."""
    configuration = build_stack_configuration(
        profile_named("pepe"), FakeResolver(), layout, workflow_specs=specs, repo_root=tmp_path
    )

    assert not configuration.ok
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert ENV_ENABLE_IMAGE_GENERATION not in open_webui.env
    assert ENV_DEFAULT_MODELS not in open_webui.env
    assert open_webui.env == {}, "no chat model means no chat configuration"
    comfyui = configuration.service(ComponentId.COMFYUI)
    assert comfyui.documents == ()
    assert any("no managed model is available" in problem for problem in comfyui.problems)


def test_an_unavailable_chat_model_leaves_image_generation_alone(specs, layout, tmp_path):
    """The two capabilities fail independently, each for its own reason."""
    stage_graph(tmp_path)
    unusable_chat = ResolvedModel(CHAT_ID, ModelKind.CHAT, ModelState.NOT_INSTALLED)
    configuration = build_stack_configuration(
        profile_named("pepe"),
        full_resolver(tmp_path, chat=unusable_chat),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))},
    )

    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert not configuration.ok
    assert ENV_DEFAULT_MODELS not in open_webui.env
    assert open_webui.env[ENV_ENABLE_IMAGE_GENERATION] == "true"
    assert any("chat is not configurable" in problem for problem in open_webui.problems)


def test_generated_configuration_is_deterministic(specs, layout, tmp_path):
    first = build_stack_configuration(
        profile_named("pepe"), full_resolver(tmp_path), layout, workflow_specs=specs
    )
    second = build_stack_configuration(
        profile_named("pepe"), full_resolver(tmp_path), layout, workflow_specs=specs
    )
    assert [doc.digest for doc in first.documents] == [doc.digest for doc in second.documents]


# --- and it survives the config domain ---------------------------------------


def test_the_generated_configuration_applies_verifies_and_is_idempotent(specs, layout, tmp_path):
    writer = ManagedConfigWriter()
    stage_graph(tmp_path)
    configuration = build_stack_configuration(
        profile_named("pepe"),
        full_resolver(tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))},
    )
    documents = configuration.documents

    dry = writer.apply(documents, dry_run=True)
    assert dry.written and not any(path.exists() for path in dry.written)

    first = writer.apply(documents)
    assert first.ok and first.written
    assert writer.verify(documents).ok

    second = writer.apply(documents)
    assert not second.written, "applying twice changes nothing"


def test_a_hand_written_config_is_never_overwritten(specs, layout, tmp_path):
    writer = ManagedConfigWriter()
    configuration = build_stack_configuration(
        profile_named("pepe"), full_resolver(tmp_path), layout, workflow_specs=specs
    )
    target = layout.open_webui_data / "open-webui.env"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("OLLAMA_BASE_URL=http://somewhere-else\n", encoding="utf-8")

    report = writer.apply(configuration.documents)

    assert not report.ok
    assert target in report.refused
    assert "somewhere-else" in target.read_text(encoding="utf-8")
