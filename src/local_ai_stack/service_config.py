"""Generated configuration that wires the three services to each other.

Everything here is derived, never chosen twice: addresses come from
:mod:`local_ai_stack.endpoints`, which reads the profile; models arrive already
resolved from IC5 and are used through ``require_location()``. No profile id is
branched on, no port is written down a second time, and no model file is looked
up by name.

Three wirings, and nothing else:

* **Open WebUI to Ollama** — the chat backend, addressed by URL.
* **ComfyUI search paths** — where the managed model tree lives, taken from the
  parents of the resolved files rather than rebuilt from a placement table.
* **Open WebUI to ComfyUI** — the image capability, including the workflow graph
  and the node mapping the workflow domain owns.

Values are produced as environment variables where upstream documents one, and
as generated files where a file is what the service reads. Neither ever touches
a database: SPEC forbids reaching into Open WebUI's SQLite, so anything only
expressible there is reported as a manual step instead of being forced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .components import ComponentId
from .endpoints import StackEndpoints
from .layout import StackLayout
from .managed_config import ConfigDocument, env_document, json_document, yaml_document
from .models import (
    FileModelLocation,
    ModelResolver,
    ModelState,
    OllamaModelLocation,
    ResolvedModel,
)
from .profiles import Profile
from .workflows import (
    WorkflowParameter,
    WorkflowResolution,
    WorkflowSpec,
    resolve_workflows,
)

OPEN_WEBUI_ENV_FILE = "open-webui.env"
COMFYUI_SEARCH_PATHS_FILE = "extra_model_paths.yaml"
COMFYUI_WORKFLOW_FILE = "open-webui-workflow.json"
COMFYUI_WORKFLOW_NODES_FILE = "open-webui-workflow-nodes.json"

# Open WebUI reads these on every supported install path. They are the
# documented names; nothing is written into its database.
ENV_OLLAMA_BASE_URL = "OLLAMA_BASE_URL"
ENV_ENABLE_OLLAMA_API = "ENABLE_OLLAMA_API"
ENV_ENABLE_IMAGE_GENERATION = "ENABLE_IMAGE_GENERATION"
ENV_IMAGE_GENERATION_ENGINE = "IMAGE_GENERATION_ENGINE"
ENV_COMFYUI_BASE_URL = "COMFYUI_BASE_URL"
ENV_COMFYUI_WORKFLOW = "COMFYUI_WORKFLOW"
ENV_COMFYUI_WORKFLOW_NODES = "COMFYUI_WORKFLOW_NODES"
ENV_IMAGE_GENERATION_MODEL = "IMAGE_GENERATION_MODEL"
ENV_IMAGE_SIZE = "IMAGE_SIZE"
ENV_IMAGE_STEPS = "IMAGE_STEPS"
ENV_DEFAULT_MODELS = "DEFAULT_MODELS"

CHAT_ROLE = "chat.primary"

# How Open WebUI names the workflow inputs it can drive. The node ids stay in
# the workflow manifest; this only translates our vocabulary into theirs.
OPEN_WEBUI_NODE_TYPES: Mapping[WorkflowParameter, str] = {
    WorkflowParameter.PROMPT: "prompt",
    WorkflowParameter.MODEL: "model",
    WorkflowParameter.WIDTH: "width",
    WorkflowParameter.HEIGHT: "height",
    WorkflowParameter.STEPS: "steps",
    WorkflowParameter.SEED: "seed",
}


@dataclass(frozen=True)
class ServiceConfiguration:
    """What one service needs: env to launch with, files to materialise."""

    service_id: str
    env: Mapping[str, str] = field(default_factory=dict)
    documents: tuple[ConfigDocument, ...] = ()
    problems: tuple[str, ...] = ()
    manual_steps: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def merged_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Generated values on top of whatever the caller already had."""
        merged = dict(base or {})
        merged.update(self.env)
        return merged


@dataclass(frozen=True)
class StackConfiguration:
    """Every generated configuration for one profile, in one object."""

    profile_id: str
    services: Mapping[str, ServiceConfiguration]
    workflows: Mapping[str, WorkflowResolution] = field(default_factory=dict)

    @property
    def documents(self) -> tuple[ConfigDocument, ...]:
        return tuple(
            document for service in self.services.values() for document in service.documents
        )

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(problem for service in self.services.values() for problem in service.problems)

    @property
    def manual_steps(self) -> tuple[str, ...]:
        return tuple(step for service in self.services.values() for step in service.manual_steps)

    @property
    def ok(self) -> bool:
        return not self.problems

    def service(self, service_id: str) -> ServiceConfiguration:
        return self.services[service_id]

    def env_for(self, service_id: str) -> Mapping[str, str]:
        configuration = self.services.get(service_id)
        return configuration.env if configuration is not None else {}


# --- Open WebUI -> Ollama ---------------------------------------------------


def open_webui_ollama_env(
    endpoints: StackEndpoints, chat: ResolvedModel | None = None
) -> dict[str, str]:
    """Point Open WebUI's chat backend at the Ollama this stack runs.

    The default model is the provider reference IC5 verified, never a name this
    layer assembled. Passing no model configures the connection only, which is
    why :func:`open_webui_chat_configuration` is what callers use.
    """
    env = {
        ENV_OLLAMA_BASE_URL: endpoints.ollama,
        ENV_ENABLE_OLLAMA_API: "true",
    }
    if chat is not None:
        location = chat.require_location()
        if not isinstance(location, OllamaModelLocation):
            raise TypeError("the chat model must resolve to an Ollama provider reference")
        env[ENV_DEFAULT_MODELS] = location.reference
    return env


def open_webui_chat_configuration(
    endpoints: StackEndpoints, chat: ResolvedModel | None
) -> ServiceConfiguration:
    """Chat, or the reason there is none.

    Fail closed: a stack whose primary model is missing, invalid or external is
    a stack with no chat model to select, so nothing is emitted and the reason
    travels with the result. No reference is invented and no path is used.
    """
    if chat is None:
        return ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            problems=("this profile declares no primary chat model, so chat cannot be configured",),
        )
    if not chat.available:
        return ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            problems=(
                f"chat is not configurable: model {chat.id} is {chat.state.value}"
                + (f" ({chat.detail})" if chat.detail else ""),
            ),
        )
    return ServiceConfiguration(ComponentId.OPEN_WEBUI, open_webui_ollama_env(endpoints, chat))


# --- ComfyUI model visibility ----------------------------------------------


def comfyui_search_paths(
    models: Sequence[ResolvedModel], *, base: str = "managed"
) -> tuple[str, dict[str, str]]:
    """The directories ComfyUI must search, taken from resolved locations.

    Each placement's directory is the parent of a file IC5 already put there.
    Nothing is rebuilt from a filename, and a model that is not AVAILABLE
    contributes nothing at all.
    """
    directories: dict[str, str] = {}
    for model in models:
        if not model.available:
            continue
        location = model.location
        if not isinstance(location, FileModelLocation):
            continue
        directories[location.placement.value] = str(location.path.parent)
    return base, directories


def comfyui_search_paths_document(
    layout: StackLayout, models: Sequence[ResolvedModel], *, base: str = "managed"
) -> ConfigDocument | None:
    """``extra_model_paths.yaml``, ComfyUI's documented way to add a tree."""
    name, directories = comfyui_search_paths(models, base=base)
    if not directories:
        return None
    lines = [f"{name}:"]
    lines.extend(f"  {placement}: {path}" for placement, path in sorted(directories.items()))
    return yaml_document(
        layout.comfyui_data / COMFYUI_SEARCH_PATHS_FILE,
        "\n".join(lines),
        "directories ComfyUI should search for managed models",
    )


# --- Open WebUI -> ComfyUI --------------------------------------------------


def open_webui_workflow_nodes(resolution: WorkflowResolution) -> list[dict[str, object]]:
    """Translate our parameter bindings into Open WebUI's node mapping.

    Open WebUI 0.11.3 reads a *list* of entries, each naming an input type, the
    field on the node, and the node ids that input drives::

        [{"type": "prompt", "key": "value", "node_ids": ["76"]}, ...]

    This is the only translation between the two vocabularies, and it reads the
    workflow manifest rather than repeating any node id.
    """
    nodes: list[dict[str, object]] = []
    for parameter in resolution.spec.parameters:
        node_type = OPEN_WEBUI_NODE_TYPES.get(parameter)
        if node_type is None:
            continue
        binding = resolution.spec.binding(parameter)
        nodes.append({"type": node_type, "key": binding.key, "node_ids": [binding.node_id]})
    return nodes


def open_webui_image_configuration(
    layout: StackLayout,
    endpoints: StackEndpoints,
    resolution: WorkflowResolution,
    graph: Mapping[str, object] | None = None,
) -> ServiceConfiguration:
    """Everything Open WebUI needs to drive ComfyUI, or the reason it cannot.

    Fail closed on the graph as well as on the models: without the workflow to
    submit there is nothing to enable, and enabling image generation anyway
    would let Open WebUI fall back to a default workflow that knows nothing
    about this stack's models.
    """
    if not resolution.ready:
        return ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            problems=(
                (
                    "image generation is not configurable: workflow "
                    f"{resolution.spec.id!r} is {resolution.state.value}"
                ),
                *resolution.problems,
            ),
        )
    if graph is None:
        return ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            problems=(
                (
                    "image generation is not configurable: no workflow graph is available "
                    f"for {resolution.spec.id!r}, and enabling it without one would let "
                    "Open WebUI submit a default workflow this stack does not control"
                ),
            ),
            manual_steps=(
                (
                    "Export the ComfyUI graph in API format to "
                    f"{resolution.spec.file} and re-run configuration."
                ),
            ),
        )

    workflow_path = layout.open_webui_data / COMFYUI_WORKFLOW_FILE
    nodes_path = layout.open_webui_data / COMFYUI_WORKFLOW_NODES_FILE
    nodes = open_webui_workflow_nodes(resolution)
    documents = (
        json_document(nodes_path, {"nodes": nodes}, "which workflow node each input drives"),
        json_document(workflow_path, dict(graph), "the workflow graph Open WebUI submits"),
    )
    env = {
        ENV_ENABLE_IMAGE_GENERATION: "true",
        ENV_IMAGE_GENERATION_ENGINE: "comfyui",
        ENV_COMFYUI_BASE_URL: endpoints.comfyui,
        ENV_COMFYUI_WORKFLOW: json.dumps(dict(graph), sort_keys=True),
        ENV_COMFYUI_WORKFLOW_NODES: json.dumps(nodes),
        ENV_IMAGE_GENERATION_MODEL: resolution.runtime_name,
    }
    return ServiceConfiguration(ComponentId.OPEN_WEBUI, env, documents)


# --- the whole stack --------------------------------------------------------


def build_stack_configuration(
    profile: Profile,
    resolver: ModelResolver,
    layout: StackLayout,
    *,
    workflow_specs: Mapping[str, WorkflowSpec] | None = None,
    repo_root: Path | None = None,
    graphs: Mapping[str, Mapping[str, object]] | None = None,
    image_workflow: str = "text_to_image",
) -> StackConfiguration:
    """Generate every service configuration this profile implies.

    Deterministic: the same profile and the same resolver produce the same
    documents, byte for byte. Fail closed: a model that is not AVAILABLE
    removes the capability that needed it and says so, instead of being
    configured around.
    """
    endpoints = StackEndpoints.from_profile(profile)
    resolved_models = {
        requirement.role: resolver.resolve(requirement.model_id)
        for requirement in profile.model_requirements
    }

    env: dict[str, str] = {}
    problems: list[str] = []
    manual: list[str] = []
    open_webui_documents: list[ConfigDocument] = []

    if endpoints.enabled(profile, ComponentId.OLLAMA):
        chat = open_webui_chat_configuration(endpoints, resolved_models.get(CHAT_ROLE))
        env.update(chat.env)
        problems.extend(chat.problems)
    else:
        problems.append("this profile does not enable Ollama, so chat cannot be configured")

    workflows: dict[str, WorkflowResolution] = {}
    if workflow_specs:
        wanted = {config.manifest_id for config in profile.workflows.values()}
        workflows = resolve_workflows(
            workflow_specs, profile, resolver, repo_root=repo_root, only=wanted
        )

    if profile.features.get("image_generation") and endpoints.enabled(profile, ComponentId.COMFYUI):
        declared = profile.workflows.get(image_workflow)
        resolution = workflows.get(declared.manifest_id) if declared else None
        if resolution is None:
            problems.append(
                f"profile {profile.id!r} enables image generation but declares no usable "
                f"{image_workflow!r} workflow"
            )
        else:
            image = open_webui_image_configuration(
                layout, endpoints, resolution, (graphs or {}).get(resolution.spec.id)
            )
            env.update(image.env)
            open_webui_documents.extend(image.documents)
            problems.extend(image.problems)
            manual.extend(image.manual_steps)

    open_webui_documents.insert(
        0,
        env_document(
            layout.open_webui_data / OPEN_WEBUI_ENV_FILE,
            env,
            "environment Open WebUI is launched with",
        ),
    )

    services: dict[str, ServiceConfiguration] = {
        ComponentId.OPEN_WEBUI: ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            env,
            tuple(open_webui_documents),
            tuple(problems),
            tuple(manual),
        )
    }

    models = list(resolved_models.values())
    comfyui_document = comfyui_search_paths_document(layout, models)
    comfyui_problems: tuple[str, ...] = ()
    if comfyui_document is None and endpoints.enabled(profile, ComponentId.COMFYUI):
        unavailable = [
            f"{model.id} is {model.state.value}"
            for model in models
            if model.provider.value == ComponentId.COMFYUI
            and model.state is not ModelState.AVAILABLE
        ]
        comfyui_problems = (
            "no managed model is available, so ComfyUI has nothing to be pointed at",
            *unavailable,
        )
    services[ComponentId.COMFYUI] = ServiceConfiguration(
        ComponentId.COMFYUI,
        documents=() if comfyui_document is None else (comfyui_document,),
        problems=comfyui_problems,
    )

    return StackConfiguration(profile.id, services, workflows)


__all__: Sequence[str] = (
    "COMFYUI_SEARCH_PATHS_FILE",
    "COMFYUI_WORKFLOW_FILE",
    "COMFYUI_WORKFLOW_NODES_FILE",
    "OPEN_WEBUI_ENV_FILE",
    "ServiceConfiguration",
    "StackConfiguration",
    "build_stack_configuration",
    "comfyui_search_paths",
    "comfyui_search_paths_document",
    "open_webui_chat_configuration",
    "open_webui_image_configuration",
    "open_webui_ollama_env",
    "open_webui_workflow_nodes",
)
