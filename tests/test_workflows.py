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
FIXTURE_GRAPH = Path(__file__).parent / "fixtures" / "flux2-klein-t2i-api.json"

DIFFUSION_ID = ModelId("comfyui/flux2_klein_fp8")


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
    path = tmp_path / "models" / "comfyui" / "diffusion_models" / "flux2-klein-fp8.safetensors"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"weights")
    return ResolvedModel(
        model_id,
        ModelKind.DIFFUSION,
        ModelState.AVAILABLE,
        FileModelLocation(path, path.name, ModelPlacement.DIFFUSION_MODELS),
    )


@pytest.fixture
def profile():
    return load_profile(REPO / "profiles" / "pepe.yaml")


@pytest.fixture
def specs():
    return load_workflow_manifest(REPO / "manifests" / "workflows.yaml")


@pytest.fixture
def graph():
    return json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))


# --- the manifest is the only place node ids live ---------------------------


def test_the_shipped_manifest_parses_into_declarative_bindings(specs):
    t2i = specs["flux2_klein_t2i"]
    assert t2i.complete
    assert t2i.binding(WorkflowParameter.PROMPT) == NodeBinding("76", "value")
    assert t2i.binding(WorkflowParameter.MODEL) == NodeBinding("75:70", "unet_name")
    assert t2i.binding(WorkflowParameter.SEED) == NodeBinding("75:73", "noise_seed")


def test_a_workflow_still_being_mapped_is_incomplete(specs):
    edit = specs["flux2_klein_edit"]
    assert not edit.complete
    assert "prompt" in edit.unresolved and "model" in edit.unresolved


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


def test_a_workflow_needs_an_available_model(specs, profile, tmp_path):
    resolver = FakeResolver({DIFFUSION_ID: available_file_model(tmp_path)})
    resolution = resolve_workflow(
        specs["flux2_klein_t2i"], profile, resolver, repo_root=FIXTURE_GRAPH.parent.parent
    )
    assert resolver.asked == [DIFFUSION_ID], "only the role the workflow needs is asked for"
    assert resolution.state is WorkflowState.TEMPLATE_MISSING, "the graph is not in the repo yet"
    assert resolution.model.available


@pytest.mark.parametrize(
    "state", [ModelState.NOT_INSTALLED, ModelState.INVALID, ModelState.EXTERNAL]
)
def test_a_model_that_is_not_available_blocks_the_workflow(specs, profile, state):
    resolver = FakeResolver(
        {DIFFUSION_ID: ResolvedModel(DIFFUSION_ID, ModelKind.DIFFUSION, state, detail="nope")}
    )
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver)

    assert resolution.state is WorkflowState.MODEL_UNAVAILABLE
    assert not resolution.ready
    assert state.value in resolution.problems[0]
    with pytest.raises(ModelUnavailableError):
        resolution.require_model_location()


def test_an_unavailable_model_can_never_be_rendered(specs, profile, graph):
    resolver = FakeResolver()
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver)
    with pytest.raises(ModelUnavailableError, match="model-unavailable"):
        render_workflow(resolution, WorkflowParameters(prompt="a cat"), graph=graph)


def test_an_incomplete_mapping_blocks_the_workflow(specs, profile, tmp_path):
    resolver = FakeResolver({DIFFUSION_ID: available_file_model(tmp_path)})
    edit = specs["flux2_klein_edit"]
    resolution = resolve_workflow(edit, profile, resolver, repo_root=tmp_path)
    assert resolution.state is WorkflowState.MAPPING_INCOMPLETE
    assert "no node binding" in resolution.problems[0]


def test_a_role_the_profile_does_not_declare_blocks_the_workflow(specs, profile, tmp_path):
    from dataclasses import replace

    spec = replace(specs["flux2_klein_t2i"], model_role="image.upscaler")
    resolution = resolve_workflow(spec, profile, FakeResolver())
    assert resolution.state is WorkflowState.MODEL_NOT_DECLARED
    assert "declares no model for role" in resolution.problems[0]


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


def test_a_workflow_refuses_a_model_that_is_not_a_file(specs, profile):
    """An Ollama model is a reference, and a node needs a name on disk."""
    chat_id = ModelId("ollama/hermes3_8b")
    resolution_input = ResolvedModel(
        chat_id, ModelKind.CHAT, ModelState.AVAILABLE, OllamaModelLocation("hermes3:8b")
    )
    resolver = FakeResolver({DIFFUSION_ID: resolution_input})
    resolution = resolve_workflow(specs["flux2_klein_t2i"], profile, resolver)
    with pytest.raises(WorkflowError, match="needs a file model"):
        resolution.require_model_location()


# --- rendering --------------------------------------------------------------


def ready_resolution(specs, profile, tmp_path):
    resolver = FakeResolver({DIFFUSION_ID: available_file_model(tmp_path)})
    template = tmp_path / "workflows" / "flux2-klein-t2i-api.json"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(FIXTURE_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
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
