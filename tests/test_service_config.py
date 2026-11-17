from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import (
    DIFFUSION_ID,
    IMAGE_MODELS,
    SHIPPED_GRAPH,
    FakeResolver,
    available_file_model,
)

from local_ai_stack.components import ComponentId
from local_ai_stack.endpoints import (
    EndpointConfigurationError,
    EndpointError,
    StackEndpoints,
    base_url,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.managed_config import ManagedConfigWriter
from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.models import (
    FileModelLocation,
    ModelId,
    ModelKind,
    ModelState,
    OllamaModelLocation,
    ResolvedModel,
)
from local_ai_stack.profiles import declared_workflows, load_profile, validate_profile_data
from local_ai_stack.service_config import (
    COMFYUI_SEARCH_PATHS_FILE,
    ENV_COMFYUI_BASE_URL,
    ENV_COMFYUI_WORKFLOW_NODES,
    ENV_DEFAULT_MODELS,
    ENV_ENABLE_FOLLOW_UP_GENERATION,
    ENV_ENABLE_IMAGE_GENERATION,
    ENV_ENABLE_OLLAMA_API,
    ENV_ENABLE_TAGS_GENERATION,
    ENV_EXCLUSIVE_VRAM,
    ENV_IMAGE_GENERATION_MODEL,
    ENV_IMAGE_PROMPT_GENERATION,
    ENV_IMAGE_ROUTER,
    ENV_IMAGE_SIZE,
    ENV_IMAGE_STACKS,
    ENV_IMAGE_STEPS,
    ENV_OLLAMA_BASE_URL,
    ENV_PEPITO_COMFYUI_URL,
    ENV_PEPITO_OLLAMA_URL,
    IMAGE_STACKS_FILE,
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
        placement = model_catalog().require(model_id).source.placement
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
    template.write_text(SHIPPED_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
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


def test_explicit_port_overrides_move_config_from_one_endpoint_authority(tmp_path, layout, specs):
    profile = profile_named("pepe")
    endpoints = StackEndpoints.from_profile(
        profile,
        port_overrides={"ollama": 12434, "open-webui": 18080, "comfyui": 18188},
    )
    configuration = build_stack_configuration(
        profile,
        full_resolver(tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=REPO,
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
        endpoints=endpoints,
    )

    env = configuration.service("open-webui").env
    assert env[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:12434"
    assert env[ENV_COMFYUI_BASE_URL] == "http://127.0.0.1:18188"


@pytest.mark.parametrize("port", [0, 65536, True, "12434"])
def test_invalid_endpoint_override_is_rejected(port):
    with pytest.raises(EndpointConfigurationError):
        StackEndpoints.from_profile(profile_named("pepe"), port_overrides={"ollama": port})


def test_unknown_endpoint_override_is_rejected():
    with pytest.raises(EndpointConfigurationError, match="unknown services"):
        StackEndpoints.from_profile(profile_named("pepe"), port_overrides={"other": 12345})


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
    graph = json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))

    configuration = open_webui_image_configuration(layout, endpoints, resolution, graph)

    payload = json.loads(configuration.env[ENV_COMFYUI_WORKFLOW_NODES])
    assert isinstance(payload, list)
    assert payload == open_webui_workflow_nodes(resolution)


def test_image_configuration_uses_the_runtime_name_and_the_comfyui_url(specs, layout, tmp_path):
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)
    graph = json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))

    configuration = open_webui_image_configuration(layout, endpoints, resolution, graph)

    assert configuration.ok
    assert configuration.env[ENV_ENABLE_IMAGE_GENERATION] == "true"
    assert configuration.env[ENV_COMFYUI_BASE_URL] == endpoints.comfyui
    assert configuration.env[ENV_IMAGE_GENERATION_MODEL] == resolution.runtime_name
    assert str(resolution.model_path) not in json.dumps(dict(configuration.env))


def test_a_profile_that_asks_for_a_size_and_steps_seeds_open_webui_with_them(
    specs, layout, tmp_path
):
    """IMAGE_SIZE and IMAGE_STEPS, in Open WebUI's own spelling, only when asked for."""
    endpoints = StackEndpoints.from_profile(profile_named("pepe"))
    resolution = ready_t2i(specs, profile_named("pepe"), tmp_path)
    graph = json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))

    plain = open_webui_image_configuration(layout, endpoints, resolution, graph)
    assert ENV_IMAGE_SIZE not in plain.env and ENV_IMAGE_STEPS not in plain.env

    asked = open_webui_image_configuration(
        layout, endpoints, resolution, graph, size="1024x1024", steps=20
    )
    assert asked.ok
    assert asked.env[ENV_IMAGE_SIZE] == "1024x1024"
    assert asked.env[ENV_IMAGE_STEPS] == "20"
    assert asked.documents == plain.documents


def test_a_size_or_steps_the_workflow_does_not_map_is_refused_not_ignored(specs, layout, tmp_path):
    """The edit graph maps neither width nor steps; asking it for them is a problem."""
    profile = profile_named("pepe")
    endpoints = StackEndpoints.from_profile(profile)
    template = tmp_path / "workflows" / "flux2-klein-edit-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        (REPO / "workflows" / "flux2-klein-edit-api.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    resolution = resolve_workflow(
        specs["flux2_klein_edit"], profile, full_resolver(tmp_path), repo_root=tmp_path
    )
    graph = json.loads(template.read_text(encoding="utf-8"))

    configuration = open_webui_image_configuration(
        layout, endpoints, resolution, graph, size="1024x1024", steps=8
    )

    assert not configuration.ok and configuration.env == {}
    assert len(configuration.problems) == 2
    assert "maps no width and height" in configuration.problems[0]
    assert "maps no step count" in configuration.problems[1]


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
    template.write_text(SHIPPED_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")


def stage_profile_graphs(specs, profile, tmp_path) -> dict[str, dict]:
    """Stage every graph the profile's workflows name, its variant's included, as a
    checkout holds them.

    Returns the graphs by manifest id, the shape ``build_stack_configuration``
    takes them in, so a profile is composed from its own workflows."""
    graphs = {}
    for _key, _field, declared in declared_workflows(profile):
        spec = specs.get(declared.manifest_id)
        if spec is None:
            continue
        text = (REPO / spec.file).read_text(encoding="utf-8")
        template = tmp_path / spec.file
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text(text, encoding="utf-8")
        graphs[spec.id] = json.loads(text)
    return graphs


def test_a_profile_produces_a_complete_configuration(specs, layout, tmp_path):
    profile = profile_named("pepe")
    stage_graph(tmp_path)
    configuration = build_stack_configuration(
        profile,
        full_resolver(tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
    )

    assert configuration.profile_id == "pepe"
    assert configuration.ok, configuration.problems
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert open_webui.env[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:11434"
    assert open_webui.env[ENV_DEFAULT_MODELS] == "hermes3:8b"
    assert open_webui.env[ENV_COMFYUI_BASE_URL] == "http://127.0.0.1:8188"
    assert any(doc.path.name == "open-webui.env" for doc in open_webui.documents)
    assert any(doc.path.name == "extra_model_paths.yaml" for doc in configuration.documents)


def test_a_size_asked_of_a_workflow_that_cannot_take_it_fails_the_whole_composition(
    specs, layout, tmp_path
):
    """The edit graph maps neither width nor steps. A profile that asks it for them
    is refused by the composition itself -- not only by the text-to-image helper --
    and nothing is configured from it."""
    data = deepcopy(profile_named("capella").raw)
    data["workflows"]["image_edit"].update(size="1024x1024", steps=20)
    profile = validate_profile_data(data)
    graphs = stage_profile_graphs(specs, profile, tmp_path)

    configuration = build_stack_configuration(
        profile,
        resolver_for(profile, tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs=graphs,
    )

    assert not configuration.ok
    problems = "\n".join(configuration.problems)
    edit = profile.workflows["image_edit"].manifest_id
    assert "workflows.image_edit" in problems and edit in problems
    assert "maps no width and height" in problems and "maps no step count" in problems


@pytest.mark.parametrize("workflow", ["image_edit", "text_to_image"])
def test_a_workflow_manifest_that_does_not_exist_invalidates_the_configuration(
    specs, layout, tmp_path, workflow
):
    """An unknown manifest id is a configuration problem for any workflow that names
    it, and the workflow it names is not composed even in part."""
    stage_graph(tmp_path)
    data = deepcopy(profile_named("capella").raw)
    data["workflows"][workflow] = {"manifest_id": "missing", "size": "1024x1024", "steps": 20}
    profile = validate_profile_data(data)

    configuration = build_stack_configuration(
        profile,
        resolver_for(profile, tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
    )

    assert not configuration.ok
    problems = "\n".join(configuration.problems)
    assert f"workflows.{workflow} names 'missing'" in problems
    assert "the workflow manifest does not declare" in problems
    assert "maps no" not in problems, "nothing is derived from a spec that does not exist"
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    if workflow == "text_to_image":
        assert ENV_ENABLE_IMAGE_GENERATION not in open_webui.env
        assert ENV_IMAGE_SIZE not in open_webui.env and ENV_IMAGE_STEPS not in open_webui.env
        assert open_webui.documents == tuple(
            d for d in open_webui.documents if d.path.name == "open-webui.env"
        )


def test_the_shipped_capella_asks_only_what_its_workflows_map(specs, layout, tmp_path):
    """The valid half of the contraproof: Qwen-Image's 1024x1024 at its template's 25
    steps on the text-to-image workflow, nothing asked of the edit workflow."""
    profile = profile_named("capella")
    assert profile.workflows["image_edit"].size is None
    assert profile.workflows["image_edit"].steps is None

    configuration = build_stack_configuration(
        profile,
        resolver_for(profile, tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs=stage_profile_graphs(specs, profile, tmp_path),
    )

    assert configuration.ok, configuration.problems
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert (open_webui.env[ENV_IMAGE_SIZE], open_webui.env[ENV_IMAGE_STEPS]) == ("1024x1024", "25")


def disabled_profile():
    """An internal smoke fixture, independent of optional installed profiles."""
    data = deepcopy(profile_named("pepe").raw)
    data.update(id="test", name="Test", visibility="internal", models={}, workflows={}, runtime={})
    data["features"] = {key: False for key in data["features"]}
    return validate_profile_data(data)


def test_a_different_profile_produces_its_own_configuration(specs, layout, tmp_path):
    """Nothing branches on a profile id; the differences come from the files.

    Pepe and Capella select different chat models; Test selects none.
    Each configuration uses the reference returned by this fixture's resolver.
    """
    built = {}
    for profile in (profile_named("pepe"), profile_named("capella"), disabled_profile()):
        name = profile.id
        built[name] = build_stack_configuration(
            profile,
            resolver_for(profile, tmp_path),
            layout,
            workflow_specs=specs,
            repo_root=tmp_path,
            graphs=stage_profile_graphs(specs, profile, tmp_path),
        )

    pepe = built["pepe"].service(ComponentId.OPEN_WEBUI)
    assert pepe.env[ENV_OLLAMA_BASE_URL] == "http://127.0.0.1:11434"
    assert pepe.env[ENV_DEFAULT_MODELS] == "hermes3-8b"
    assert pepe.env[ENV_ENABLE_IMAGE_GENERATION] == "true"

    capella = built["capella"].service(ComponentId.OPEN_WEBUI)
    assert capella.env[ENV_DEFAULT_MODELS] == "qwen3-8-27b-q5"
    assert capella.problems == ()
    assert capella.env[ENV_ENABLE_IMAGE_GENERATION] == "true", "images still work"
    # Capella asks its workflow for the card's room and its template's steps; Pepe asks for
    # FLUX.2 klein's native size and leaves the steps alone.
    assert (capella.env[ENV_IMAGE_SIZE], capella.env[ENV_IMAGE_STEPS]) == ("1024x1024", "25")
    assert pepe.env[ENV_IMAGE_SIZE] == "1024x1024"
    assert ENV_IMAGE_STEPS not in pepe.env

    test = built["test"].service(ComponentId.OPEN_WEBUI)
    # Chat is disabled, so no backend address and no model -- but not an empty
    # env either: Open WebUI 0.11.3 defaults ENABLE_OLLAMA_API to true and the
    # base URL to localhost:11434, which could be an Ollama this stack does not
    # own. Disabled is said explicitly.
    assert test.env == {ENV_ENABLE_OLLAMA_API: "false"}
    assert test.problems == ()
    assert built["test"].ok, built["test"].problems
    assert built["test"].service(ComponentId.COMFYUI).documents == ()


def test_quiet_background_tasks_seed_follow_ups_and_tags_off_but_not_titles(
    specs, layout, tmp_path
):
    """``runtime.quiet_background_tasks`` seeds both switches off on a new database
    (an existing one is switched through the admin API); titles keep their default,
    and a profile without the flag (Pepe) seeds nothing."""
    env = {}
    for name in ("capella", "pepe"):
        profile = profile_named(name)
        env[name] = (
            build_stack_configuration(
                profile,
                resolver_for(profile, tmp_path),
                layout,
                workflow_specs=specs,
                repo_root=tmp_path,
                graphs=stage_profile_graphs(specs, profile, tmp_path),
            )
            .service(ComponentId.OPEN_WEBUI)
            .env
        )
    assert env["capella"][ENV_ENABLE_FOLLOW_UP_GENERATION] == "false"
    assert env["capella"][ENV_ENABLE_TAGS_GENERATION] == "false"
    assert "ENABLE_TITLE_GENERATION" not in env["capella"]
    assert not {ENV_ENABLE_FOLLOW_UP_GENERATION, ENV_ENABLE_TAGS_GENERATION} & set(env["pepe"])


EXCLUSIVE_VRAM_ENV = (
    ENV_EXCLUSIVE_VRAM,
    ENV_PEPITO_OLLAMA_URL,
    ENV_PEPITO_COMFYUI_URL,
    ENV_IMAGE_PROMPT_GENERATION,
)


def test_exclusive_vram_reaches_open_webui_only_from_a_profile_that_asks_for_it(
    specs, layout, tmp_path
):
    """``runtime.exclusive_image_vram`` hands the image filter the two services
    it unloads between, and turns off Open WebUI's own prompt generation, which
    would load the chat model back beside the image model. The same profile
    without the flag gets none of it, and still generates images. Profiles
    validated without the swap (Pepe, Pepe 8 GB) do not opt in."""
    capella = profile_named("capella")
    assert not profile_named("pepe").runtime.get("exclusive_image_vram")
    assert not profile_named("pepe-8gb").runtime.get("exclusive_image_vram")
    shared = replace(
        capella,
        runtime={**capella.runtime, "exclusive_image_vram": False, "image_router": False},
    )
    graphs = stage_profile_graphs(specs, capella, tmp_path)
    env = {
        key: build_stack_configuration(
            profile,
            resolver_for(profile, tmp_path),
            layout,
            workflow_specs=specs,
            repo_root=tmp_path,
            graphs=graphs,
        )
        .service(ComponentId.OPEN_WEBUI)
        .env
        for key, profile in (("exclusive", capella), ("shared", shared))
    }

    endpoints = StackEndpoints.from_profile(capella)
    assert {name: env["exclusive"][name] for name in EXCLUSIVE_VRAM_ENV} == {
        ENV_EXCLUSIVE_VRAM: "true",
        ENV_PEPITO_OLLAMA_URL: endpoints.ollama,
        ENV_PEPITO_COMFYUI_URL: endpoints.comfyui,
        ENV_IMAGE_PROMPT_GENERATION: "false",
    }
    assert not set(EXCLUSIVE_VRAM_ENV) & set(env["shared"])
    assert env["shared"][ENV_ENABLE_IMAGE_GENERATION] == "true"


def test_the_image_router_reaches_open_webui_only_from_a_profile_that_asks_for_it(
    specs, layout, tmp_path
):
    """``runtime.image_router`` hands the filter the switch and the Ollama it asks;
    without it the word rules decide, as on Pepe and Pepe 8 GB."""
    capella = profile_named("capella")
    assert profile_named("pepe").runtime.get("image_router") is None
    assert profile_named("pepe-8gb").runtime.get("image_router") is None
    graphs = stage_profile_graphs(specs, capella, tmp_path)
    variants = {
        "router": replace(capella, runtime={**capella.runtime, "exclusive_image_vram": False}),
        "rules": replace(
            capella,
            runtime={**capella.runtime, "exclusive_image_vram": False, "image_router": False},
        ),
    }
    env = {
        key: build_stack_configuration(
            profile,
            resolver_for(profile, tmp_path),
            layout,
            workflow_specs=specs,
            repo_root=tmp_path,
            graphs=graphs,
        )
        .service(ComponentId.OPEN_WEBUI)
        .env
        for key, profile in variants.items()
    }
    endpoints = StackEndpoints.from_profile(capella)
    assert env["router"][ENV_IMAGE_ROUTER] == "true"
    assert env["router"][ENV_PEPITO_OLLAMA_URL] == endpoints.ollama
    assert ENV_IMAGE_ROUTER not in env["rules"]
    assert ENV_PEPITO_OLLAMA_URL not in env["rules"]
    # Without the router no image stacks are written, so the character swap is never offered.
    assert ENV_IMAGE_STACKS in env["router"] and ENV_IMAGE_STACKS not in env["rules"]


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
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
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
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
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
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
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


def swap_stacks(profile, specs, layout, tmp_path, *, missing_model=None, missing_graph=None):
    graphs = stage_profile_graphs(specs, profile, tmp_path)
    resolver = resolver_for(profile, tmp_path)
    if missing_model:
        model_id = profile.model_for(missing_model)
        resolver.models[model_id] = replace(
            resolver.models[model_id], state=ModelState.NOT_INSTALLED, location=None
        )
    if missing_graph:
        graphs.pop(missing_graph)
    configuration = build_stack_configuration(
        profile, resolver, layout, workflow_specs=specs, repo_root=tmp_path, graphs=graphs
    )
    document = next(
        (doc for doc in configuration.documents if doc.path.name == IMAGE_STACKS_FILE), None
    )
    return (None if document is None else json.loads(document.body)), configuration


def test_character_swap_is_shared_by_both_stacks_with_existing_part_shape(specs, layout, tmp_path):
    profile = profile_named("capella")
    stacks, configuration = swap_stacks(profile, specs, layout, tmp_path)
    assert configuration.ok, configuration.problems
    swaps = [stack["swap"] for stack in stacks.values()]
    assert len(swaps) == 1 + len(profile.variants)
    swap = swaps[0]
    assert set(swap) == set(profile.character_swap)
    for name, part in swap.items():
        assert {
            "workflow",
            "nodes",
            "model",
            "reference_count",
            "reference_policy",
            "family",
        } <= part.keys()
        graph = json.loads(part["workflow"])
        for binding in part["nodes"]:
            assert set(binding) == {"type", "key", "node_ids"}
            for node_id in binding["node_ids"]:
                assert binding["key"] in graph[node_id]["inputs"]
        images = next(binding for binding in part["nodes"] if binding["type"] == "image")
        assert part["reference_count"] == len(images["node_ids"])
        assert part["reference_count"] == (2 if name in ("marked", "scene") else 1)
    assert {node["type"] for node in swap["segment"]["nodes"]} == {"image", "prompt", "model"}
    assert {node["type"] for node in swap["depth"]["nodes"]} == {"image", "model"}
    assert swap["marked"]["syntax"] == "indexed"
    search = next(
        doc for doc in configuration.documents if doc.path.name == COMFYUI_SEARCH_PATHS_FILE
    )
    assert "checkpoints:" in search.body and "geometry_estimation:" in search.body


@pytest.mark.parametrize(
    "role",
    ["swap.segment", "swap.depth"],
)
def test_missing_swap_weight_disables_entire_pipeline(specs, layout, tmp_path, role):
    stacks, _ = swap_stacks(profile_named("capella"), specs, layout, tmp_path, missing_model=role)
    assert stacks
    assert all("swap" not in stack for stack in stacks.values())


def test_missing_main_image_weight_writes_no_stacks_so_no_swap(specs, layout, tmp_path):
    """Every main-stack image role is also one of text-to-image's, so without it image
    generation is not configured, no image stacks are written, and no model offers the swap."""
    stacks, configuration = swap_stacks(
        profile_named("capella"), specs, layout, tmp_path, missing_model="image.text_encoder"
    )
    assert stacks is None
    assert ENV_IMAGE_STACKS not in configuration.service(ComponentId.OPEN_WEBUI).env


@pytest.mark.parametrize(
    "manifest_id",
    [
        "qwen_image_2_1_edit",
        "qwen_image_2_1_edit_2",
        "sam3_people",
        "da3_depth",
    ],
)
def test_missing_swap_graph_disables_entire_pipeline(specs, layout, tmp_path, manifest_id):
    stacks, _ = swap_stacks(
        profile_named("capella"), specs, layout, tmp_path, missing_graph=manifest_id
    )
    assert stacks
    assert all("swap" not in stack for stack in stacks.values())


def test_profile_without_character_swap_emits_no_swap(specs, layout, tmp_path):
    data = deepcopy(profile_named("capella").raw)
    data.pop("character_swap")
    profile = validate_profile_data(data)
    stacks, configuration = swap_stacks(profile, specs, layout, tmp_path)
    assert configuration.ok, configuration.problems
    assert all("swap" not in stack for stack in stacks.values())
    assert not any(
        requirement.role.startswith("swap.") for requirement in profile.model_requirements
    )


def test_swap_declarations_enter_install_plan_resolution_and_size_estimate(specs):
    from local_ai_stack.planning import PlanAction, build_install_plan
    from local_ai_stack.storage import model_download_estimate

    profile = profile_named("capella")
    requirements = {item.role: item.model_id for item in profile.model_requirements}
    plan = build_install_plan(profile)
    acquired = {step.target for step in plan.steps if step.action == PlanAction.ACQUIRE_MODEL}
    for name, declared in profile.character_swap.items():
        key = f"character_swap.{name}"
        assert profile.workflow(key) == declared
        assert any(
            step.action == PlanAction.APPLY_WORKFLOW and step.target == key for step in plan.steps
        )
        for role in specs[declared.manifest_id].required_roles:
            assert str(requirements[role]) in acquired
    models = {str(key): value for key, value in model_catalog().specs.items()}
    estimate = model_download_estimate(profile, models)
    without = replace(
        profile,
        model_requirements=tuple(
            item for item in profile.model_requirements if not item.role.startswith("swap.")
        ),
    )
    assert estimate.complete
    assert estimate.known_bytes - model_download_estimate(without, models).known_bytes == 3082294904
