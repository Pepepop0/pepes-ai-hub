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
  and the node mapping the workflow domain owns. Image editing shares the
  translation (:func:`open_webui_image_edit_settings`), but its values are only
  expressible in Open WebUI's persisted settings, so they are handed to the
  settings sync rather than to the launch environment.

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
from .profiles import (
    CHAT_PRIMARY_ROLE,
    FEATURE_CHAT,
    FEATURE_IMAGE_EDITING,
    FEATURE_IMAGE_GENERATION,
    IMAGE_EDIT_WORKFLOW,
    TEXT_TO_IMAGE_WORKFLOW,
    Profile,
    capability_problems,
)
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

CHAT_ROLE = CHAT_PRIMARY_ROLE

# How Open WebUI names the workflow inputs it can drive. The node ids stay in
# the workflow manifest; this only translates our vocabulary into theirs.
OPEN_WEBUI_NODE_TYPES: Mapping[WorkflowParameter, str] = {
    WorkflowParameter.PROMPT: "prompt",
    WorkflowParameter.MODEL: "model",
    WorkflowParameter.WIDTH: "width",
    WorkflowParameter.HEIGHT: "height",
    WorkflowParameter.STEPS: "steps",
    WorkflowParameter.SEED: "seed",
    WorkflowParameter.IMAGE: "image",
}

# What Open WebUI 0.11.3's edit path fills in (routers/images.py image_edits ->
# ComfyUIEditImageForm): the uploaded image(s), the instruction, the configured
# model and a seed. Width and height only when an edit size is configured, and
# steps never, so a graph that maps those would receive None for them.
OPEN_WEBUI_EDIT_PARAMETERS: frozenset[WorkflowParameter] = frozenset(
    {
        WorkflowParameter.IMAGE,
        WorkflowParameter.PROMPT,
        WorkflowParameter.MODEL,
        WorkflowParameter.SEED,
    }
)


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


def open_webui_chat_disabled_env() -> dict[str, str]:
    """What Open WebUI gets from a profile that does not enable chat.

    Not "nothing": left unset, Open WebUI connects to an Ollama on its default
    address, which may well be one this stack does not own. Saying outright
    that the Ollama API is off is the only value that configures exactly what
    the profile asked for -- no chat -- without inventing an address or a model.
    """
    return {ENV_ENABLE_OLLAMA_API: "false"}


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
    *,
    size: str | None = None,
    steps: int | None = None,
) -> ServiceConfiguration:
    """Everything Open WebUI needs to drive ComfyUI, or the reason it cannot.

    Fail closed on the graph as well as on the models: without the workflow to
    submit there is nothing to enable, and enabling image generation anyway
    would let Open WebUI fall back to a default workflow that knows nothing
    about this stack's models.

    ``size`` and ``steps`` are what the profile asks the workflow for. They go
    out as Open WebUI's own IMAGE_SIZE and IMAGE_STEPS, which seed its persisted
    settings on first start and are then the administrator's to change; a
    profile that asks for neither leaves Open WebUI's defaults alone. Asking a
    graph for a size or a step count it does not map is a profile error, not
    something to send and have silently ignored.
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

    parameters = set(resolution.spec.parameters)
    unmapped = []
    if size is not None and not {WorkflowParameter.WIDTH, WorkflowParameter.HEIGHT} <= parameters:
        unmapped.append(f"a size of {size}, but it maps no width and height")
    if steps is not None and WorkflowParameter.STEPS not in parameters:
        unmapped.append(f"{steps} steps, but it maps no step count")
    if unmapped:
        return ServiceConfiguration(
            ComponentId.OPEN_WEBUI,
            problems=tuple(
                f"image generation is not configurable: the profile asks workflow "
                f"{resolution.spec.id!r} for {item}"
                for item in unmapped
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
    if size is not None:
        env[ENV_IMAGE_SIZE] = size
    if steps is not None:
        env[ENV_IMAGE_STEPS] = str(steps)
    return ServiceConfiguration(ComponentId.OPEN_WEBUI, env, documents)


@dataclass(frozen=True)
class ImageEditSettings:
    """What Open WebUI's image edit needs to drive ComfyUI with a managed workflow."""

    base_url: str
    model: str
    workflow: Mapping[str, object]
    nodes: tuple[Mapping[str, object], ...]


def open_webui_image_edit_settings(
    endpoints: StackEndpoints,
    resolution: WorkflowResolution,
    graph: Mapping[str, object] | None,
) -> tuple[ImageEditSettings | None, tuple[str, ...]]:
    """The edit settings for a ready workflow, or the reasons there are none.

    Fail closed like generation: no ready workflow, no graph, a graph without
    an image input, or a binding Open WebUI's edit path would leave empty all
    mean nothing is configured.
    """
    if not resolution.ready:
        return None, (
            (
                f"image editing is not configurable: workflow {resolution.spec.id!r} is "
                f"{resolution.state.value}"
            ),
            *resolution.problems,
        )
    if graph is None:
        return None, (
            (
                "image editing is not configurable: no workflow graph is available for "
                f"{resolution.spec.id!r}"
            ),
        )
    parameters = set(resolution.spec.parameters)
    problems = []
    if WorkflowParameter.IMAGE not in parameters or WorkflowParameter.PROMPT not in parameters:
        problems.append(
            f"image editing is not configurable: workflow {resolution.spec.id!r} must map "
            "both an image and a prompt"
        )
    unsupported = sorted(p.value for p in parameters - OPEN_WEBUI_EDIT_PARAMETERS)
    if unsupported:
        problems.append(
            f"image editing is not configurable: workflow {resolution.spec.id!r} maps "
            f"{', '.join(unsupported)}, which Open WebUI's edit path does not fill in"
        )
    if problems:
        return None, tuple(problems)
    settings = ImageEditSettings(
        base_url=endpoints.comfyui,
        model=resolution.runtime_name,
        workflow=dict(graph),
        nodes=tuple(open_webui_workflow_nodes(resolution)),
    )
    return settings, ()


# --- the whole stack --------------------------------------------------------


def workflow_manifest_problems(
    profile: Profile, workflow_specs: Mapping[str, WorkflowSpec]
) -> dict[str, tuple[str, ...]]:
    """Every workflow the profile declares under a name the manifest does not.

    Settled by the profile and the shipped manifest alone, for every workflow
    the schema allows and not only the two this version configures: a name
    nothing declares can never resolve, so it is worth saying before anything
    is installed. Keyed by the profile's workflow id, so a caller can refuse
    exactly the workflow that named it.
    """
    unknown = "which the workflow manifest does not declare"
    return {
        workflow_id: (f"workflows.{workflow_id} names {declared.manifest_id!r}, {unknown}",)
        for workflow_id, declared in sorted(profile.workflows.items())
        if declared.manifest_id not in workflow_specs
    }


def workflow_drive_problems(
    profile: Profile, workflow_specs: Mapping[str, WorkflowSpec]
) -> dict[str, tuple[str, ...]]:
    """Every size or step count the profile asks of a workflow that cannot take it.

    A declaration nothing can act on is an error wherever it appears, not only
    on the text-to-image workflow: Open WebUI's edit path fills in neither a
    size nor a step count, and a graph that maps no width, height or steps
    would be submitted with the profile's numbers quietly dropped. Keyed by the
    profile's workflow id so a caller can refuse exactly the workflow it was
    about to configure. Nothing is said about a workflow whose manifest id does
    not exist: there is no spec to ask, ``workflow_manifest_problems`` owns
    that, and "it maps no width and height" about a workflow that was never
    declared would only bury the real reason.
    """
    problems: dict[str, tuple[str, ...]] = {}
    size_parameters = {WorkflowParameter.WIDTH, WorkflowParameter.HEIGHT}
    for workflow_id, declared in sorted(profile.workflows.items()):
        if declared.size is None and declared.steps is None:
            continue
        spec = workflow_specs.get(declared.manifest_id)
        if spec is None:
            continue
        mapped = set(spec.bindings)
        unmapped = []
        if declared.size is not None and not size_parameters <= mapped:
            unmapped.append(f"a size of {declared.size}, but it maps no width and height")
        if declared.steps is not None and WorkflowParameter.STEPS not in mapped:
            unmapped.append(f"{declared.steps} steps, but it maps no step count")
        if unmapped:
            problems[workflow_id] = tuple(
                f"workflows.{workflow_id} asks {spec.id!r} for {item}" for item in unmapped
            )
    return problems


def declared_capability_problems(
    profile: Profile, workflow_specs: Mapping[str, WorkflowSpec]
) -> tuple[str, ...]:
    """What would make configuration fail, known before anything is installed.

    The profile's own coherence, plus what the workflow manifest settles: an
    enabled image generation needs its text-to-image workflow to exist, to be
    mapped, to generate from text alone (a graph that maps an input image is an
    edit graph), and to name only models the profile declares. None of this
    waits for an install -- a workflow that is not mapped today is not mapped
    after downloading twelve gigabytes either. Whether a declared model can
    actually be installed is ``unresolved_roles``'s question, not this one.

    An image-edit workflow still being mapped is deliberately not here: it
    does not block configuration, and is reported as a warning when it runs.
    A mapped one must take an input image, or it cannot edit anything. What a
    profile asks each workflow to be driven with is checked for all of them.
    """
    problems = list(capability_problems(profile))
    unknown = workflow_manifest_problems(profile, workflow_specs)
    for messages in unknown.values():
        problems.extend(messages)
    for messages in workflow_drive_problems(profile, workflow_specs).values():
        problems.extend(messages)
    if not profile.enables(FEATURE_IMAGE_GENERATION):
        return tuple(problems)
    declared = profile.workflows.get(TEXT_TO_IMAGE_WORKFLOW)
    if declared is None:
        return tuple(problems)
    spec = workflow_specs.get(declared.manifest_id)
    if spec is None:
        pass  # said once already, by the check every workflow goes through
    elif not spec.complete:
        problems.append(
            f"workflows.text_to_image names {spec.id!r}, which is not mapped yet "
            f"({', '.join(spec.unresolved)})"
        )
    elif WorkflowParameter.IMAGE in spec.bindings:
        problems.append(
            f"workflows.text_to_image names {spec.id!r}, which edits an input image "
            "and cannot generate from text"
        )
    else:
        missing = [role for role in spec.required_roles if profile.model_for(role) is None]
        if missing:
            problems.append(
                f"workflow {spec.id!r} loads {', '.join(missing)}, which the profile does not name"
            )
    edit = profile.workflows.get(IMAGE_EDIT_WORKFLOW)
    edit_spec = workflow_specs.get(edit.manifest_id) if edit is not None else None
    if (
        profile.enables(FEATURE_IMAGE_EDITING)
        and edit_spec is not None
        and edit_spec.complete
        and WorkflowParameter.IMAGE not in edit_spec.bindings
    ):
        problems.append(f"workflows.image_edit names {edit_spec.id!r}, which takes no input image")
    return tuple(problems)


def build_stack_configuration(
    profile: Profile,
    resolver: ModelResolver,
    layout: StackLayout,
    *,
    workflow_specs: Mapping[str, WorkflowSpec] | None = None,
    repo_root: Path | None = None,
    graphs: Mapping[str, Mapping[str, object]] | None = None,
    image_workflow: str = "text_to_image",
    endpoints: StackEndpoints | None = None,
) -> StackConfiguration:
    """Generate every service configuration this profile implies.

    Deterministic: the same profile and the same resolver produce the same
    documents, byte for byte. Each integration follows its capability, never
    the mere presence of a service: an enabled capability whose requirements
    are missing fails closed and says why, while a disabled one is simply not
    configured, and its absent requirements are not a problem. A model that is
    not AVAILABLE removes the capability that needed it, instead of being
    configured around.
    """
    endpoints = endpoints or StackEndpoints.from_profile(profile)
    resolved_models = {
        requirement.role: resolver.resolve(requirement.model_id)
        for requirement in profile.model_requirements
    }

    env: dict[str, str] = {}
    problems: list[str] = []
    manual: list[str] = []
    open_webui_documents: list[ConfigDocument] = []

    if not profile.enables(FEATURE_CHAT):
        env.update(open_webui_chat_disabled_env())
    elif endpoints.enabled(profile, ComponentId.OLLAMA):
        chat = open_webui_chat_configuration(endpoints, resolved_models.get(CHAT_ROLE))
        env.update(chat.env)
        problems.extend(chat.problems)
    else:
        problems.append("this profile enables chat but not Ollama, so chat cannot be configured")

    workflows: dict[str, WorkflowResolution] = {}
    # Every workflow the profile declares: the manifest id it names, and then
    # what it asks that workflow to be driven with. A workflow either check
    # refuses is not configured at all below, rather than configured in part.
    refused = workflow_manifest_problems(profile, workflow_specs or {})
    drive_problems = workflow_drive_problems(profile, workflow_specs or {})
    for source in (refused, drive_problems):
        for messages in source.values():
            problems.extend(messages)
    refused = {**refused, **drive_problems}
    if workflow_specs:
        wanted = {config.manifest_id for config in profile.workflows.values()}
        workflows = resolve_workflows(
            workflow_specs, profile, resolver, repo_root=repo_root, only=wanted
        )

    image_enabled = profile.enables(FEATURE_IMAGE_GENERATION)
    if image_enabled and not endpoints.enabled(profile, ComponentId.COMFYUI):
        problems.append(
            "this profile enables image generation but not ComfyUI, "
            "so image generation cannot be configured"
        )
    elif image_enabled:
        declared = profile.workflows.get(image_workflow)
        resolution = workflows.get(declared.manifest_id) if declared else None
        if image_workflow in refused:
            pass  # already refused, with the reason, above
        elif resolution is None:
            problems.append(
                f"profile {profile.id!r} enables image generation but declares no usable "
                f"{image_workflow!r} workflow"
            )
        else:
            image = open_webui_image_configuration(
                layout,
                endpoints,
                resolution,
                (graphs or {}).get(resolution.spec.id),
                size=declared.size,
                steps=declared.steps,
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
    # Managed models are what ComfyUI is pointed at, and only image generation
    # needs them. With it off, ComfyUI still runs on its own model tree; having
    # nothing managed to point it at is then expected, not a failure.
    if (
        comfyui_document is None
        and image_enabled
        and endpoints.enabled(profile, ComponentId.COMFYUI)
    ):
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
    "OPEN_WEBUI_EDIT_PARAMETERS",
    "OPEN_WEBUI_ENV_FILE",
    "ImageEditSettings",
    "ServiceConfiguration",
    "StackConfiguration",
    "build_stack_configuration",
    "comfyui_search_paths",
    "comfyui_search_paths_document",
    "declared_capability_problems",
    "open_webui_chat_configuration",
    "open_webui_image_configuration",
    "open_webui_image_edit_settings",
    "open_webui_ollama_env",
    "open_webui_workflow_nodes",
    "workflow_drive_problems",
    "workflow_manifest_problems",
)
