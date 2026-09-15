from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import local_ai_stack
from local_ai_stack.models import (
    FileModelLocation,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelState,
    ModelUnavailableError,
    OllamaModelLocation,
    ResolvedModel,
)
from local_ai_stack.profiles import load_profile
from local_ai_stack.workflows import (
    NodeBinding,
    WorkflowError,
    WorkflowParameter,
    WorkflowParameters,
    WorkflowState,
    load_workflow_manifest,
    parse_workflow_manifest,
    render_workflow,
    resolve_workflow,
    resolve_workflows,
    verify_workflow_references,
)

REPO = Path(__file__).resolve().parents[1]
SRC = Path(local_ai_stack.__file__).resolve().parents[1]
# The graph this stack actually submits: exported from ComfyUI in API format
# and versioned in the repository. Tests read it rather than a stand-in, so a
# mapping that drifts from the real export fails here.
SHIPPED_GRAPH = REPO / "workflows" / "flux2-klein-t2i-api.json"

DIFFUSION_ID = ModelId("comfyui/flux2_klein_fp8")
ENCODER_ID = ModelId("comfyui/qwen_3_4b_encoder")
VAE_ID = ModelId("comfyui/flux2_vae")

# Every role the shipped t2i workflow declares, and the file each one resolves
# to. A workflow needs all of them; the tests below take one away at a time.
IMAGE_MODELS: dict[ModelId, tuple[ModelPlacement, str, ModelKind]] = {
    DIFFUSION_ID: (
        ModelPlacement.DIFFUSION_MODELS,
        "flux2-klein-fp8.safetensors",
        ModelKind.DIFFUSION,
    ),
    ENCODER_ID: (ModelPlacement.TEXT_ENCODERS, "qwen_3_4b.safetensors", ModelKind.TEXT_ENCODER),
    VAE_ID: (ModelPlacement.VAE, "flux2-vae.safetensors", ModelKind.VAE),
}


class FakeResolver:
    """Stands in for IC5. Returns exactly what it is told to, nothing more."""

    def __init__(self, models: dict[ModelId, ResolvedModel] | None = None) -> None:
        self.models = models or {}
        self.asked: list[ModelId] = []

    def resolve(self, model_id: ModelId) -> ResolvedModel:
        self.asked.append(model_id)
        if model_id in self.models:
            return self.models[model_id]
        return ResolvedModel(model_id, ModelKind.AUXILIARY, ModelState.NOT_INSTALLED)


def available_file_model(tmp_path, model_id: ModelId = DIFFUSION_ID) -> ResolvedModel:
    placement, filename, kind = IMAGE_MODELS[model_id]
    path = tmp_path / "models" / "comfyui" / placement.value / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"weights")
    return ResolvedModel(
        model_id,
        kind,
        ModelState.AVAILABLE,
        FileModelLocation(path, path.name, placement),
    )


def image_resolver(tmp_path, *, missing: ModelId | None = None, state=None) -> FakeResolver:
    """Every image role available, except one the caller wants to break."""
    models = {}
    for model_id in IMAGE_MODELS:
        if model_id == missing:
            if state is not None:
                models[model_id] = ResolvedModel(
                    model_id, IMAGE_MODELS[model_id][2], state, detail="broken on purpose"
                )
            continue
        models[model_id] = available_file_model(tmp_path, model_id)
    return FakeResolver(models)


@pytest.fixture
def profile():
    return load_profile(REPO / "profiles" / "pepe.yaml")


@pytest.fixture
def specs():
    return load_workflow_manifest(REPO / "manifests" / "workflows.yaml")


@pytest.fixture
def graph():
    return json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))


# --- the manifest is the only place node ids live ---------------------------


def test_the_shipped_manifest_parses_into_declarative_bindings(specs):
    t2i = specs["flux2_klein_t2i"]
    assert t2i.complete
    assert t2i.binding(WorkflowParameter.PROMPT) == NodeBinding("76", "value")
    assert t2i.binding(WorkflowParameter.MODEL) == NodeBinding("75:70", "unet_name")
    assert t2i.binding(WorkflowParameter.SEED) == NodeBinding("75:73", "noise_seed")


def test_the_edit_workflow_maps_exactly_what_a_caller_supplies(specs):
    edit = specs["flux2_klein_edit"]
    assert edit.complete and edit.status == "mapped_and_embedded"
    assert edit.file == Path("workflows/flux2-klein-edit-api.json")
    assert set(edit.parameters) == {
        WorkflowParameter.IMAGE,
        WorkflowParameter.PROMPT,
        WorkflowParameter.MODEL,
        WorkflowParameter.SEED,
    }
    assert edit.required_roles == ("image.diffusion", "image.text_encoder", "image.vae")


def test_a_workflow_still_being_mapped_is_incomplete():
    manifest = {
        "schema_version": 1,
        "workflows": {
            "draft": {
                "file": "workflows/draft.json",
                "models": {"required": ["image.diffusion"], "parameter": "image.diffusion"},
                "mapping": {"image": "TBD", "prompt": "TBD", "seed": {"node_id": "3", "key": "s"}},
            }
        },
    }
    draft = parse_workflow_manifest(manifest)["draft"]
    assert not draft.complete
    assert draft.unresolved == ("image", "prompt")


def test_no_node_id_appears_anywhere_but_the_manifest():
    """The point of the adapter: ids live in data, never in code."""
    offenders = []
    for path in (SRC / "local_ai_stack").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ('"75:70"', '"75:62"', '"75:73"', "'75:70'"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 9}, "unsupported workflow schema_version"),
        ({"workflows": []}, "'workflows' must be a mapping"),
    ],
)
def test_a_malformed_manifest_is_refused(mutation, message):
    data = {"schema_version": 1, "workflows": {}, **mutation}
    with pytest.raises(WorkflowError, match=message):
        parse_workflow_manifest(data)


def test_an_unknown_parameter_is_refused():
    data = {
        "schema_version": 1,
        "workflows": {"w": {"file": "w.json", "mapping": {"cfg": {"node_id": "1", "key": "v"}}}},
    }
    with pytest.raises(WorkflowError, match="unknown parameter"):
        parse_workflow_manifest(data)


def test_a_binding_missing_its_node_is_refused():
    data = {
        "schema_version": 1,
        "workflows": {"w": {"file": "w.json", "mapping": {"prompt": {"key": "value"}}}},
    }
    with pytest.raises(WorkflowError, match="missing node_id"):
        parse_workflow_manifest(data)


# --- fail closed on models --------------------------------------------------


def test_a_workflow_resolves_every_role_it_declares(specs, profile, tmp_path):
    resolver = image_resolver(tmp_path)
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver, repo_root=REPO)
    assert resolver.asked == [DIFFUSION_ID, ENCODER_ID, VAE_ID], "all three, not just the model"
    assert set(resolution.models) == {"image.diffusion", "image.text_encoder", "image.vae"}
    assert resolution.model.available


def test_the_manifest_declares_the_roles_with_no_hidden_default(specs):
    spec = specs["flux2_klein_t2i"]
    assert spec.required_roles == ("image.diffusion", "image.text_encoder", "image.vae")
    assert spec.parameter_role == "image.diffusion"


def test_a_workflow_without_a_models_block_is_refused():
    data = {
        "schema_version": 1,
        "workflows": {"w": {"file": "w.json", "mapping": {"prompt": {"node_id": "1", "key": "v"}}}},
    }
    with pytest.raises(WorkflowError, match="must declare a models block"):
        parse_workflow_manifest(data)


def test_a_parameter_role_outside_the_required_set_is_refused():
    data = {
        "schema_version": 1,
        "workflows": {
            "w": {
                "file": "w.json",
                "models": {"required": ["image.vae"], "parameter": "image.diffusion"},
                "mapping": {"prompt": {"node_id": "1", "key": "v"}},
            }
        },
    }
    with pytest.raises(WorkflowError, match="is not one of the required roles"):
        parse_workflow_manifest(data)


@pytest.mark.parametrize(
    ("missing", "role"),
    [
        (DIFFUSION_ID, "image.diffusion"),
        (ENCODER_ID, "image.text_encoder"),
        (VAE_ID, "image.vae"),
    ],
)
@pytest.mark.parametrize(
    "state", [ModelState.NOT_INSTALLED, ModelState.INVALID, ModelState.EXTERNAL]
)
def test_any_missing_role_blocks_the_workflow(specs, profile, tmp_path, missing, role, state):
    """One broken role out of three is enough; there is no partial readiness."""
    resolver = image_resolver(tmp_path, missing=missing, state=state)
    template = tmp_path / "workflows" / "flux2-klein-t2i-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(SHIPPED_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")

    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver, repo_root=tmp_path)

    assert resolution.state is WorkflowState.MODEL_UNAVAILABLE
    assert not resolution.ready
    assert any(role in problem and state.value in problem for problem in resolution.problems)
    with pytest.raises(ModelUnavailableError):
        render_workflow(resolution, WorkflowParameters(prompt="x"))


def test_a_model_that_is_not_available_never_exposes_a_location(specs, profile):
    resolver = FakeResolver()
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver)

    assert resolution.state is WorkflowState.MODEL_UNAVAILABLE
    assert not resolution.ready
    with pytest.raises(ModelUnavailableError):
        resolution.require_model_location()


def test_an_unavailable_model_can_never_be_rendered(specs, profile, graph):
    resolver = FakeResolver()
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver)
    with pytest.raises(ModelUnavailableError, match="model-unavailable"):
        render_workflow(resolution, WorkflowParameters(prompt="a cat"), graph=graph)


def test_an_incomplete_mapping_blocks_the_workflow(specs, profile, tmp_path):
    from dataclasses import replace

    resolver = image_resolver(tmp_path)
    edit = replace(specs["flux2_klein_edit"], unresolved=("image",))
    resolution = resolve_workflow(edit, profile, resolver, repo_root=tmp_path)
    assert resolution.state is WorkflowState.MAPPING_INCOMPLETE
    assert "no node binding" in resolution.problems[0]


def test_a_role_the_profile_does_not_declare_blocks_the_workflow(specs, profile, tmp_path):
    from dataclasses import replace

    spec = replace(
        specs["flux2_klein_t2i"],
        required_roles=("image.diffusion", "image.upscaler"),
        parameter_role="image.diffusion",
    )
    resolution = resolve_workflow(spec, profile, image_resolver(tmp_path))
    assert resolution.state is WorkflowState.MODEL_NOT_DECLARED
    assert "image.upscaler" in resolution.problems[0]


def test_a_comfyui_model_cannot_carry_an_ollama_location():
    """The contract itself refuses the mix, so no workflow can be handed one."""
    from local_ai_stack.models import ModelContractError

    with pytest.raises(ModelContractError, match="does not match model id"):
        ResolvedModel(
            ModelId("comfyui/flux2_klein_fp8"),
            ModelKind.DIFFUSION,
            ModelState.AVAILABLE,
            OllamaModelLocation("hermes3:8b"),
        )


def test_a_workflow_refuses_a_model_that_is_not_a_file(specs, profile, tmp_path):
    """An Ollama model is a reference, and a node needs a name on disk."""
    chat_id = ModelId("ollama/hermes3_8b")
    resolution_input = ResolvedModel(
        chat_id, ModelKind.CHAT, ModelState.AVAILABLE, OllamaModelLocation("hermes3:8b")
    )
    models = {model_id: available_file_model(tmp_path, model_id) for model_id in IMAGE_MODELS}
    models[DIFFUSION_ID] = resolution_input
    resolution = resolve_workflow(
        specs["flux2_klein_t2i"], profile, FakeResolver(models), repo_root=tmp_path
    )
    with pytest.raises(WorkflowError, match="needs a file model"):
        resolution.require_model_location()


# --- rendering --------------------------------------------------------------


def ready_resolution(specs, profile, tmp_path):
    resolver = image_resolver(tmp_path)
    template = tmp_path / "workflows" / "flux2-klein-t2i-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(SHIPPED_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
    return resolve_workflow(specs["flux2_klein_t2i"], profile, resolver, repo_root=tmp_path)


def test_every_parameter_lands_on_the_node_the_manifest_names(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    assert resolution.ready

    rendered = render_workflow(
        resolution,
        WorkflowParameters(prompt="a small crab", width=768, height=512, steps=28, seed=99),
    )

    assert rendered["76"]["inputs"]["value"] == "a small crab"
    assert rendered["75:68"]["inputs"]["value"] == 768
    assert rendered["75:69"]["inputs"]["value"] == 512
    assert rendered["75:62"]["inputs"]["steps"] == 28
    assert rendered["75:73"]["inputs"]["noise_seed"] == 99


def test_the_model_node_receives_the_runtime_name_not_a_path(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    rendered = render_workflow(resolution, WorkflowParameters(prompt="x"))

    unet = rendered["75:70"]["inputs"]["unet_name"]
    assert unet == "flux2-klein-fp8.safetensors"
    assert unet == resolution.runtime_name
    assert os.sep not in unet, "a node gets a name, never a path"
    assert str(resolution.model_path) not in json.dumps(rendered)


def test_rendering_never_mutates_the_template(specs, profile, tmp_path, graph):
    resolution = ready_resolution(specs, profile, tmp_path)
    before = json.dumps(graph, sort_keys=True)
    render_workflow(resolution, WorkflowParameters(prompt="x", steps=1), graph=graph)
    assert json.dumps(graph, sort_keys=True) == before


def test_rendering_is_deterministic(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    params = WorkflowParameters(prompt="same", width=640, height=640, steps=12, seed=7)
    first = render_workflow(resolution, params)
    second = render_workflow(resolution, params)
    assert first == second


def test_a_binding_that_does_not_exist_in_the_graph_is_an_error(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    partial = {"76": {"inputs": {"value": ""}}}  # the width node is not here
    with pytest.raises(WorkflowError, match="not in the graph"):
        render_workflow(resolution, WorkflowParameters(prompt="x"), graph=partial)


def test_a_node_without_the_declared_field_is_an_error(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    with pytest.raises(WorkflowError, match="has no field"):
        render_workflow(resolution, WorkflowParameters(prompt="x"), graph={"76": {"inputs": {}}})


# --- verification -----------------------------------------------------------


def test_references_verify_against_a_real_graph(specs, profile, tmp_path, graph):
    resolution = ready_resolution(specs, profile, tmp_path)
    assert verify_workflow_references(resolution, graph) == ()


def test_verification_names_every_missing_reference(specs, profile, tmp_path):
    resolution = ready_resolution(specs, profile, tmp_path)
    problems = verify_workflow_references(resolution, {"76": {"inputs": {"value": ""}}})
    assert any("model" in problem for problem in problems)
    assert all("prompt" not in problem for problem in problems)


def test_resolving_many_workflows_filters_to_what_was_asked_for(specs, profile, tmp_path):
    resolver = FakeResolver({DIFFUSION_ID: available_file_model(tmp_path)})
    resolutions = resolve_workflows(specs, profile, resolver, only={"flux2_klein_t2i"})
    assert set(resolutions) == {"flux2_klein_t2i"}


# --- boundary ---------------------------------------------------------------


IC6_MODULES = ("workflows.py", "service_config.py", "managed_config.py", "endpoints.py")

# The acquisition side of the contract. IC6 consumes ResolvedModel and must not
# reach past it. models.py itself is the shared contract and is allowed.
FORBIDDEN_IMPORTS = frozenset({"catalog", "download", "artifacts", "git_ops"})


def imported_modules(path: Path) -> set[str]:
    """Every module this file imports, by AST rather than by grepping prose."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[-1])
    return names


def test_no_ic6_module_imports_the_acquisition_side():
    """Prose may name the boundary; code may not cross it."""
    for name in IC6_MODULES:
        imported = imported_modules(SRC / "local_ai_stack" / name)
        crossed = imported & FORBIDDEN_IMPORTS
        assert crossed == set(), f"{name} imports {sorted(crossed)}"


def test_the_workflow_domain_reaches_ic5_only_through_the_contract():
    """models.py is the frozen boundary; catalog and downloader are behind it."""
    code = (
        "import sys;"
        "import local_ai_stack.workflows, local_ai_stack.service_config,"
        " local_ai_stack.managed_config, local_ai_stack.endpoints;"
        "banned = ('local_ai_stack.catalog', 'local_ai_stack.download');"
        "print([name for name in banned if name in sys.modules])"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == "[]"


def test_no_ic6_module_names_a_url_or_a_checksum_in_code():
    """String literals, not comments: the boundary is about values, not words."""
    import ast

    for name in IC6_MODULES:
        tree = ast.parse((SRC / "local_ai_stack" / name).read_text(encoding="utf-8"))
        literals = [
            node.value.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for text in literals:
            assert "https://" not in text, f"{name} carries a URL literal"
            assert "sha256" not in text, f"{name} carries a checksum literal"


# --- the graph exported from ComfyUI ----------------------------------------


def test_the_shipped_graph_is_api_format_not_the_ui_export(graph):
    """The UI export nests nodes under "nodes" with numeric ids and link arrays."""
    assert graph, "the export is versioned in the repository"
    assert "nodes" not in graph, "a UI export would be rejected by ComfyUI's /prompt"
    assert all(isinstance(node, dict) and "class_type" in node for node in graph.values())


def test_the_graph_loads_a_model_for_every_role_the_manifest_requires(specs, graph):
    """Three loaders in the graph, three roles in the manifest, no coincidence."""
    loaders = {node["class_type"] for node in graph.values()} & {
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
    }
    assert loaders == {"UNETLoader", "CLIPLoader", "VAELoader"}
    assert specs["flux2_klein_t2i"].required_roles == (
        "image.diffusion",
        "image.text_encoder",
        "image.vae",
    )


def test_the_model_binding_addresses_the_diffusion_loader(specs, graph):
    """The node id comes from the manifest; only the class is asserted here."""
    binding = specs["flux2_klein_t2i"].binding(WorkflowParameter.MODEL)
    assert graph[binding.node_id]["class_type"] == "UNETLoader"
    assert binding.key in graph[binding.node_id]["inputs"]


def test_every_binding_the_manifest_declares_exists_in_the_real_graph(
    specs, profile, tmp_path, graph
):
    resolution = resolve_workflow(
        specs["flux2_klein_t2i"], profile, image_resolver(tmp_path), repo_root=REPO
    )
    assert verify_workflow_references(resolution, graph) == ()


def test_rendering_the_real_graph_replaces_everything_it_was_exported_with(
    specs, profile, tmp_path
):
    """Whatever the export happened to carry is a value, never a default."""
    resolution = resolve_workflow(
        specs["flux2_klein_t2i"], profile, image_resolver(tmp_path), repo_root=REPO
    )
    before = SHIPPED_GRAPH.read_text(encoding="utf-8")
    exported = json.loads(before)

    rendered = render_workflow(
        resolution,
        WorkflowParameters(prompt="a small crab", width=768, height=512, steps=28, seed=99),
    )

    spec = resolution.spec
    for parameter, expected in (
        (WorkflowParameter.PROMPT, "a small crab"),
        (WorkflowParameter.WIDTH, 768),
        (WorkflowParameter.HEIGHT, 512),
        (WorkflowParameter.STEPS, 28),
        (WorkflowParameter.SEED, 99),
        (WorkflowParameter.MODEL, resolution.runtime_name),
    ):
        binding = spec.binding(parameter)
        assert rendered[binding.node_id]["inputs"][binding.key] == expected
        assert exported[binding.node_id]["inputs"][binding.key] != expected, (
            f"{parameter.value} would pass even without being written"
        )

    assert SHIPPED_GRAPH.read_text(encoding="utf-8") == before, "the export on disk is read-only"


# --- the image edit graph: embedded, mapped, and made of the models Pepe has ----------

SHIPPED_EDIT_GRAPH = REPO / "workflows" / "flux2-klein-edit-api.json"


def test_the_edit_graph_is_embedded_and_every_binding_addresses_a_real_field(specs, profile):
    """A: the resource is in the repository; B: the mapping matches the export."""
    assert SHIPPED_EDIT_GRAPH.is_file()
    edit = specs["flux2_klein_edit"]
    resolution = resolve_workflow(edit, profile, image_resolver_repo(), repo_root=REPO)
    assert resolution.ready, resolution.problems
    assert resolution.template_path == REPO / "workflows" / "flux2-klein-edit-api.json"
    graph = json.loads(SHIPPED_EDIT_GRAPH.read_text(encoding="utf-8"))
    assert verify_workflow_references(resolution, graph) == ()
    kinds = {
        parameter: graph[edit.binding(parameter).node_id]["class_type"]
        for parameter in edit.parameters
    }
    assert kinds == {
        WorkflowParameter.IMAGE: "LoadImage",
        WorkflowParameter.PROMPT: "CLIPTextEncode",
        WorkflowParameter.MODEL: "UNETLoader",
        WorkflowParameter.SEED: "RandomNoise",
    }


def test_the_edit_graph_takes_one_image_and_saves_one_output(graph_edit):
    classes = [node["class_type"] for node in graph_edit.values()]
    assert classes.count("LoadImage") == 1, "one input image, one mapped node"
    assert classes.count("SaveImage") == 1
    # The size comes from the input image, and every input references a node that exists.
    assert "EmptyFlux2LatentImage" in classes and "GetImageSize" in classes
    for node in graph_edit.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in graph_edit, value


def test_the_edit_graph_loads_exactly_the_models_the_catalog_already_pins(graph_edit):
    """No new download: the three files Pepe's text-to-image already installed."""
    from local_ai_stack.model_catalog import model_catalog

    catalog = model_catalog()
    pinned = {
        catalog.require(model_id).source.artifact.filename
        for model_id in (DIFFUSION_ID, ENCODER_ID, VAE_ID)
    }
    loaded = {
        value
        for node in graph_edit.values()
        for key, value in node["inputs"].items()
        if key in {"unet_name", "clip_name", "vae_name"}
    }
    assert (
        loaded
        == pinned
        == {
            "flux-2-klein-4b-fp8.safetensors",
            "qwen_3_4b.safetensors",
            "flux2-vae.safetensors",
        }
    )


def test_the_edit_graph_carries_no_machine_path(graph_edit):
    text = SHIPPED_EDIT_GRAPH.read_text(encoding="utf-8")
    for needle in (":\\", "C:/", "/Users/", "\\\\", "PEPEH", "scratchpad", "Temp"):
        assert needle not in text, needle
    assert json.loads(text) == graph_edit


def test_rendering_the_edit_graph_puts_prompt_model_and_seed_where_mapped(specs, profile):
    resolution = resolve_workflow(
        specs["flux2_klein_edit"], profile, image_resolver_repo(), repo_root=REPO
    )
    rendered = render_workflow(resolution, WorkflowParameters(prompt="make it green", seed=5))
    assert rendered["75:74"]["inputs"]["text"] == "make it green"
    assert rendered["75:73"]["inputs"]["noise_seed"] == 5
    assert rendered["75:70"]["inputs"]["unet_name"] == resolution.runtime_name
    assert rendered["76"]["inputs"]["image"] == "handbag_white.png", "the image is Open WebUI's"


@pytest.fixture
def graph_edit():
    return json.loads(SHIPPED_EDIT_GRAPH.read_text(encoding="utf-8"))


def image_resolver_repo() -> FakeResolver:
    """Every image role available under its real catalog filename, without touching disk."""
    from local_ai_stack.model_catalog import model_catalog

    catalog = model_catalog()
    models = {}
    for model_id, (placement, _, kind) in IMAGE_MODELS.items():
        filename = catalog.require(model_id).source.artifact.filename
        path = Path(os.path.abspath(os.sep)) / "stack" / "models" / placement.value / filename
        models[model_id] = ResolvedModel(
            model_id, kind, ModelState.AVAILABLE, FileModelLocation(path, filename, placement)
        )
    return FakeResolver(models)
