from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .components import ComponentId, normalize_component_id
from .models import ModelContractError, ModelId, ModelRequirement

PROFILE_SCHEMA_VERSION = 1
# A profile's own release number, MAJOR.MINOR.PATCH: MAJOR when its model stack changes,
# MINOR when it gains a capability, PATCH for a fix. Shown wherever a profile is chosen.
PROFILE_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MIB_PER_GIB = 1024

# Who a profile is offered to. A profile is part of the product unless it says
# otherwise; one that exists to exercise the machinery says so, and screens
# built for a person do not offer it. Nothing reads a profile's id to decide.
VISIBILITY_PUBLIC = "public"
VISIBILITY_INTERNAL = "internal"
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_INTERNAL)
# How a developer asks for the internal ones anyway, without editing a profile.
ENV_SHOW_INTERNAL_PROFILES = "PEPITOS_AI_HUB_INTERNAL_PROFILES"

# Capabilities a profile can switch on, and what each one needs declared to be
# configurable at all. Enabled means "this profile has what it takes": a
# capability that is on while the data behind it is TBD or absent is not a
# softer kind of enabled, it is an incoherent profile, and it is refused here
# rather than discovered after minutes of installation.
FEATURE_CHAT = "chat"
FEATURE_VISION = "vision"
FEATURE_TOOLS = "tools"
FEATURE_IMAGE_GENERATION = "image_generation"
FEATURE_IMAGE_EDITING = "image_editing"

CHAT_PRIMARY_ROLE = "chat.primary"
TEXT_TO_IMAGE_WORKFLOW = "text_to_image"
IMAGE_EDIT_WORKFLOW = "image_edit"
# A chat variant's image models live in the ``models`` group it names (``images``): its
# own set, with the LoRAs or auxiliary models its workflows load. The roles are free
# names; the workflow manifest says which ones a workflow needs.
VARIANT_ROLE_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
# An edit with more input images is its own workflow, chosen by how many the
# turn has: ``image_edit_2`` for two, ``image_edit_3`` for three.
IMAGE_EDIT_REFERENCE_WORKFLOWS = {2: "image_edit_2", 3: "image_edit_3"}
# Workflows only the image filter drives, chosen by the router's answer, and the stack
# key each one becomes: restaging the chat's character (Identity Edit with the person
# beside the canvas), a removal, and a pose edit (one pose adapter). The filter sets
# what Open WebUI cannot in the graph.
FILTER_STACK_WORKFLOWS = {"restage_edit": "restage", "remove_edit": "remove", "pose_edit": "pose"}
# The "high quality" generation a variant offers next to its text_to_image.
TEXT_TO_IMAGE_HQ_WORKFLOW = "text_to_image_hq"
# A model role named ``pose_<adapter>`` is one of a pose edit's adapters.
POSE_ROLE_PREFIX = "pose_"
# Shared analysis/generation passes for character swaps, available on every chat stack.
CHARACTER_SWAP_PARTS = ("reference", "marked", "scene", "segment", "depth")
# ``scene`` (the identity edit that puts a character into a scene) is optional: a profile
# without it swaps with the Qwen passes alone.
CHARACTER_SWAP_REQUIRED_PARTS = tuple(part for part in CHARACTER_SWAP_PARTS if part != "scene")
# A profile overlay (``profiles/<name>.overlay.yaml``) extends a base profile by id and may
# add to these keys only.
OVERLAY_SUFFIX = ".overlay.yaml"
OVERLAY_KEYS = frozenset({"extends", "models", "variants", "character_swap", "behavior"})
CHARACTER_SWAP_MODEL_GROUP = "swap"
# The runtime flag that has the Hub own Open WebUI's web search settings (on or off,
# the engine, its address and key), set from the Hub's own web search settings.
WEB_SEARCH_RUNTIME_KEY = "web_search"
# The runtime flag that has the image chat filter route every Image turn (and
# so make every picture itself). A chat variant needs it: Open WebUI's own
# handler knows only one workflow, so without the filter the variant's
# pictures would come from the profile's models.
IMAGE_ROUTER_RUNTIME_KEY = "image_router"
# The runtime flag that switches off Open WebUI's follow-up and tag generation
# (title generation is kept): two extra chat-model calls after every answer that,
# on a card the chat and image models share, can land beside a running picture.
QUIET_BACKGROUND_TASKS_RUNTIME_KEY = "quiet_background_tasks"
# The runtime flag that has the Hub load the chat model into the GPU once the services are
# up, and the Ollama keep-alive (a string, not a flag) that keeps it there between turns.
PRELOAD_CHAT_MODEL_RUNTIME_KEY = "preload_chat_model"
CHAT_KEEP_ALIVE_RUNTIME_KEY = "chat_keep_alive"
# Hours a file stays in ComfyUI's input and output folders before the Hub removes it
# (a number, not a flag); absent: nothing is ever removed.
COMFYUI_RETENTION_RUNTIME_KEY = "comfyui_retention_hours"
KEEP_ALIVE_PATTERN = re.compile(r"-1|[0-9]+|(?:[0-9]+(?:\.[0-9]+)?(?:ns|us|µs|ms|s|m|h))+")

# A capability that only refines another one: vision and tool calling are
# things a chat model does, and editing an image is image generation plus an
# input. Enabling the refinement without its base configures nothing.
FEATURE_REFINES: dict[str, str] = {
    FEATURE_VISION: FEATURE_CHAT,
    FEATURE_TOOLS: FEATURE_CHAT,
    FEATURE_IMAGE_EDITING: FEATURE_IMAGE_GENERATION,
}


@dataclass(frozen=True)
class HardwareRequirement:
    os: tuple[str, ...]
    gpu_vendor: str
    min_vram_mib: int
    recommended_vram_mib: int
    vram_tolerance_mib: int
    notes: str = ""

    @property
    def min_vram_gb(self) -> int | float:
        return _gib_value(self.min_vram_mib)

    @property
    def recommended_vram_gb(self) -> int | float:
        return _gib_value(self.recommended_vram_mib)


@dataclass(frozen=True)
class ServiceConfig:
    enabled: bool
    port: int


# The size a profile may ask its text-to-image workflow for: Open WebUI's own
# "WxH" spelling (IMAGE_SIZE), the one it validates before submitting.
IMAGE_SIZE_PATTERN = re.compile(r"^[1-9]\d*x[1-9]\d*$")


@dataclass(frozen=True)
class WorkflowConfig:
    """A workflow the profile drives, and how hard: size and steps are optional,
    and only meaningful for a workflow whose graph maps them."""

    manifest_id: str
    size: str | None = None
    steps: int | None = None


@dataclass(frozen=True)
class PoseBehavior:
    """A named pose: the adapter (a role of the models group) and the sentence it was
    trained on, which the image filter sends as the edit's instruction."""

    adapter: str
    prompt: str


@dataclass(frozen=True)
class Behavior:
    """What a profile or variant adds to the image filter's behaviour: extra rules for
    its router and the named poses it offers. Empty means the filter's defaults only."""

    router_rules: tuple[str, ...] = ()
    poses: dict[str, PoseBehavior] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatVariant:
    """Another Open WebUI model on the same chat model with its own image workflows.

    It appears in Open WebUI's model list as ``name`` (record id ``id``) and
    makes and edits pictures with ``workflows`` instead of the profile's own.
    Its image models are the roles of ``models.<images>``.
    """

    id: str
    name: str
    images: str
    workflows: dict[str, WorkflowConfig]
    behavior: Behavior = field(default_factory=Behavior)


@dataclass(frozen=True)
class ProfileUiDefaults:
    default_pet: str | None = None
    default_theme: str | None = None


@dataclass(frozen=True)
class Profile:
    schema_version: int
    id: str
    name: str
    version: str
    description: str
    visibility: str
    ui: ProfileUiDefaults
    hardware: HardwareRequirement
    services: dict[str, ServiceConfig]
    features: dict[str, bool]
    models: dict[str, dict[str, Any]]
    model_requirements: tuple[ModelRequirement, ...]
    workflows: dict[str, WorkflowConfig]
    runtime: dict[str, bool]
    # Ollama's keep-alive for the chat model (``"60m"``, ``"-1"``); None leaves its default.
    chat_keep_alive: str | None
    raw: dict[str, Any]
    variants: tuple[ChatVariant, ...] = ()
    comfyui_retention_hours: float | None = None
    character_swap: dict[str, WorkflowConfig] = field(default_factory=dict)
    behavior: Behavior = field(default_factory=Behavior)

    @property
    def internal(self) -> bool:
        """Whether this profile is machinery rather than a product choice."""
        return self.visibility == VISIBILITY_INTERNAL

    def enables(self, feature: str) -> bool:
        """Whether this profile switches ``feature`` on. Absent means off."""
        return self.features.get(feature, False)

    def enables_service(self, service_id: str) -> bool:
        service = self.services.get(normalize_component_id(service_id))
        return service is not None and service.enabled

    def model_for(self, role: str) -> ModelId | None:
        """The model a role names, or None when it is absent or TBD."""
        return next((item.model_id for item in self.model_requirements if item.role == role), None)

    def workflow(self, key: str) -> WorkflowConfig | None:
        """The declaration a plan step names: ``<id>`` or ``variant.<variant>.<id>``."""
        if key.startswith("character_swap."):
            return self.character_swap.get(key.removeprefix("character_swap."))
        if key.startswith("variant."):
            variant_id, _, workflow_id = key[8:].partition(".")
            variant = next((item for item in self.variants if item.id == variant_id), None)
            return None if variant is None else variant.workflows.get(workflow_id)
        return self.workflows.get(key)

    @property
    def chat_params(self) -> dict[str, bool | int | float | str]:
        """What the profile asks of its chat model at every request: Open WebUI's
        own model parameters (``think``, ``num_ctx``, ...), validated on load.
        Empty when the profile leaves the model's defaults alone."""
        return dict(self.models.get("chat", {}).get("params") or {})

    @property
    def chat_system_prompt(self) -> str | None:
        """The system prompt the chat model starts with in Open WebUI, or None.

        A seed, not a setting the Hub keeps enforcing: it is written only into
        a model record that has no system prompt yet, so an administrator's own
        edit in Admin → Models survives every later start."""
        value = self.models.get("chat", {}).get("system_prompt")
        return value.strip() if isinstance(value, str) and value.strip() else None


class ProfileError(ValueError):
    pass


def capability_problems(profile: Profile) -> tuple[str, ...]:
    """Every enabled capability the profile does not declare enough to configure.

    Reads only what the profile itself says, so it can run the moment a
    profile is loaded. Whether a declared workflow is mapped, or a declared
    model is installable, depends on the manifests and is checked where they
    are known; this is the part no manifest can rescue. No profile id is
    looked at: the rule is the same for every profile.
    """
    problems: list[str] = []
    for feature, base in FEATURE_REFINES.items():
        if profile.enables(feature) and not profile.enables(base):
            problems.append(f"features.{feature} is enabled but features.{base} is not")

    if profile.enables(FEATURE_CHAT):
        if not profile.enables_service(ComponentId.OLLAMA):
            problems.append("features.chat is enabled but services.ollama is not")
        if profile.model_for(CHAT_PRIMARY_ROLE) is None:
            problems.append("features.chat is enabled but models.chat.primary names no model")

    if profile.enables(FEATURE_IMAGE_GENERATION):
        if not profile.enables_service(ComponentId.COMFYUI):
            problems.append("features.image_generation is enabled but services.comfyui is not")
        if TEXT_TO_IMAGE_WORKFLOW not in profile.workflows:
            problems.append(
                "features.image_generation is enabled but workflows.text_to_image is not declared"
            )

    if profile.enables(FEATURE_IMAGE_EDITING) and IMAGE_EDIT_WORKFLOW not in profile.workflows:
        problems.append(
            "features.image_editing is enabled but workflows.image_edit is not declared"
        )

    # No router check: a profile with the router off loads with no swap at all (see
    # ``validate_profile_data``), since only the router could offer it.
    if profile.character_swap and not profile.enables(FEATURE_IMAGE_EDITING):
        problems.append("character_swap is declared but features.image_editing is not enabled")

    for variant in profile.variants:
        where = f"variants[{variant.id}]"
        if not profile.enables(FEATURE_CHAT):
            problems.append(f"{where} is declared but features.chat is not enabled")
        if not profile.enables(FEATURE_IMAGE_GENERATION):
            problems.append(f"{where} is declared but features.image_generation is not enabled")
        elif TEXT_TO_IMAGE_WORKFLOW not in variant.workflows:
            problems.append(
                f"features.image_generation is enabled but {where}.workflows.text_to_image "
                "is not declared"
            )
        if profile.enables(FEATURE_IMAGE_EDITING) and IMAGE_EDIT_WORKFLOW not in variant.workflows:
            problems.append(
                f"features.image_editing is enabled but {where}.workflows.image_edit "
                "is not declared"
            )
        if not profile.runtime.get(IMAGE_ROUTER_RUNTIME_KEY):
            problems.append(
                f"{where} is declared but runtime.{IMAGE_ROUTER_RUNTIME_KEY} is not enabled"
            )
    return tuple(problems)


def declared_workflows(profile: Profile) -> tuple[tuple[str, str, WorkflowConfig], ...]:
    """Every workflow the profile declares: (key, field, declaration).

    The profile's own are keyed by their workflow id, a variant's by
    ``variant.<variant>.<id>``; ``field`` is where the declaration is written.
    """
    own = tuple((key, f"workflows.{key}", value) for key, value in profile.workflows.items())
    variants = tuple(
        (
            f"variant.{variant.id}.{key}",
            f"variants[{variant.id}].workflows.{key}",
            value,
        )
        for variant in profile.variants
        for key, value in variant.workflows.items()
    )
    swap = tuple(
        (f"character_swap.{key}", f"character_swap.{key}", value)
        for key, value in profile.character_swap.items()
    )
    return own + variants + swap


def _gib_value(vram_mib: int) -> int | float:
    value = vram_mib / MIB_PER_GIB
    return int(value) if value.is_integer() else value


def _error(source: Path | str, field: str, message: str) -> ProfileError:
    location = f"{source}: {field}" if field else str(source)
    return ProfileError(f"{location}: {message}")


def _mapping(value: Any, source: Path | str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(source, field, "must be a mapping")
    return value


def _required(
    data: dict[str, Any], fields: tuple[str, ...], source: Path | str, prefix: str = ""
) -> None:
    missing = [field for field in fields if field not in data]
    if missing:
        label = f"{prefix}." if prefix else ""
        raise _error(
            source,
            prefix,
            f"missing required fields: {', '.join(label + key for key in missing)}",
        )


def _reject_unknown(
    data: dict[str, Any], allowed: set[str], source: Path | str, field: str
) -> None:
    unknown = sorted((str(key) for key in data if key not in allowed), key=str.casefold)
    if unknown:
        raise _error(source, field, f"unknown fields: {', '.join(unknown)}")


def _string(value: Any, source: Path | str, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise _error(source, field, f"must be {qualifier}")
    return value


def _integer(value: Any, source: Path | str, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _error(source, field, f"must be an integer >= {minimum}")
    return value


def _boolean(value: Any, source: Path | str, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(source, field, "must be a boolean")
    return value


def _model_id(value: Any, source: Path | str, field: str) -> ModelId | None:
    raw = _string(value, source, field)
    if raw.upper() == "TBD":
        return None
    try:
        return ModelId(raw)
    except ModelContractError as exc:
        raise _error(source, field, str(exc)) from exc


def _model_contract(
    models: dict[str, dict[str, Any]],
    source: Path | str,
    variant_groups: Sequence[str] = (),
) -> tuple[ModelRequirement, ...]:
    """Validate current profile roles without changing their public YAML shape."""
    requirements: list[ModelRequirement] = []
    active_roles: dict[str, Sequence[str]] = {
        "chat": ("primary",),
        "image": ("diffusion", "text_encoder", "vae"),
    }
    for group in variant_groups:
        roles = tuple(models.get(group, {}))
        for role in roles:
            if not isinstance(role, str) or not VARIANT_ROLE_PATTERN.fullmatch(role):
                raise _error(
                    source,
                    f"models.{group}",
                    f"role {role!r} must be lowercase letters, digits or '_' and start with a letter",
                )
        active_roles[group] = roles
    for group, fields in active_roles.items():
        values = models.get(group, {})
        for role_name in fields:
            if role_name not in values:
                continue
            model_id = _model_id(values[role_name], source, f"models.{group}.{role_name}")
            if model_id is not None:
                requirements.append(ModelRequirement(f"{group}.{role_name}", model_id))

    chat_alternatives = models.get("chat", {}).get("alternatives", [])
    if not isinstance(chat_alternatives, list):
        raise _error(source, "models.chat.alternatives", "must be a list")
    for index, value in enumerate(chat_alternatives):
        _model_id(value, source, f"models.chat.alternatives[{index}]")

    # Model parameters travel to Open WebUI's model record as they are written
    # here, so only what that record can hold is accepted: named scalars.
    chat_params = models.get("chat", {}).get("params", {})
    if not isinstance(chat_params, dict):
        raise _error(source, "models.chat.params", "must be a mapping")
    for key, value in chat_params.items():
        _string(key, source, "models.chat.params key")
        if not isinstance(value, (bool, int, float, str)) or (
            isinstance(value, str) and not value.strip()
        ):
            raise _error(
                source, f"models.chat.params.{key}", "must be a boolean, a number or a string"
            )
    system_prompt = models.get("chat", {}).get("system_prompt")
    if system_prompt is not None and (
        not isinstance(system_prompt, str) or not system_prompt.strip()
    ):
        raise _error(source, "models.chat.system_prompt", "must be a non-empty string")
    return tuple(requirements)


@lru_cache(maxsize=4)
def _shipped_workflow_roles(
    path: Path, versions: tuple[tuple[str, int], ...]
) -> dict[str, tuple[str, ...]]:
    """Each manifest workflow's required model roles, read once per manifest version.

    ``versions`` (every manifest file with its mtime) only keys the cache, so a pack added
    or removed beside the manifest is seen.
    """
    from .workflows import WorkflowError, load_workflow_manifest

    try:
        specs = load_workflow_manifest(path)
    except WorkflowError as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    return {workflow_id: spec.required_roles for workflow_id, spec in specs.items()}


def _swap_requirements(
    models: dict[str, dict[str, Any]],
    source: Path | str,
    character_swap: Mapping[str, WorkflowConfig],
    requirements: tuple[ModelRequirement, ...],
) -> tuple[ModelRequirement, ...]:
    """The contract plus every role the swap's workflows load, whichever group it is in.

    A swap part may run another stack's workflow (a pack's ``scene`` is a
    Krea 2 identity edit), so its roles are required even when no variant names that
    group; a role the profile does not declare at all is refused here, not at first run.
    The roles come from the shipped workflow manifest; a name it does not declare is
    reported where the manifest is checked against the profile.
    """
    from .resources import resource_root
    from .workflows import workflow_manifest_files

    path = resource_root() / "manifests" / "workflows.yaml"
    if not path.is_file():
        return requirements
    versions = tuple((str(file), file.stat().st_mtime_ns) for file in workflow_manifest_files(path))
    roles = _shipped_workflow_roles(path, versions)
    active = {item.role for item in requirements}
    added: list[ModelRequirement] = []
    for part, declared in character_swap.items():
        for role in roles.get(declared.manifest_id, ()):
            if role in active:
                continue
            group, _, name = role.partition(".")
            values = models.get(group)
            if not isinstance(values, dict) or name not in values:
                raise _error(
                    source,
                    f"models.{role}",
                    f"character_swap.{part} ({declared.manifest_id}) needs role {role!r}, "
                    "which the profile does not declare",
                )
            active.add(role)
            model_id = _model_id(values[name], source, f"models.{role}")
            if model_id is not None:
                added.append(ModelRequirement(role, model_id))
    return requirements + tuple(added)


def _workflow_configs(value: Any, source: Path | str, field: str) -> dict[str, WorkflowConfig]:
    workflows_data = _mapping(value, source, field)
    workflows: dict[str, WorkflowConfig] = {}
    for workflow_id, workflow_value in workflows_data.items():
        prefix = f"{field}.{workflow_id}"
        workflow = _mapping(workflow_value, source, prefix)
        _reject_unknown(workflow, {"manifest_id", "size", "steps"}, source, prefix)
        _required(workflow, ("manifest_id",), source, prefix)
        size = workflow.get("size")
        if size is not None:
            size = _string(size, source, f"{prefix}.size")
            if not IMAGE_SIZE_PATTERN.match(size):
                raise _error(source, f"{prefix}.size", "must be WIDTHxHEIGHT, e.g. 1024x1024")
        steps = workflow.get("steps")
        if steps is not None:
            steps = _integer(steps, source, f"{prefix}.steps", minimum=1)
        workflows[_string(workflow_id, source, f"{field} key")] = WorkflowConfig(
            manifest_id=_string(workflow["manifest_id"], source, f"{prefix}.manifest_id"),
            size=size,
            steps=steps,
        )
    return workflows


def _behavior(value: Any, source: Path | str, field: str, models: Mapping[str, Any]) -> Behavior:
    """``router_rules`` and ``poses``; a pose names an adapter of ``models``."""
    data = _mapping(value, source, field)
    _reject_unknown(data, {"router_rules", "poses"}, source, field)
    rules_value = data.get("router_rules", [])
    if not isinstance(rules_value, list):
        raise _error(source, f"{field}.router_rules", "must be a list of strings")
    rules = tuple(
        _string(rule, source, f"{field}.router_rules[{index}]").strip()
        for index, rule in enumerate(rules_value)
    )
    poses: dict[str, PoseBehavior] = {}
    for name, pose_value in _mapping(data.get("poses", {}), source, f"{field}.poses").items():
        label = f"{field}.poses.{name}"
        if not isinstance(name, str) or not VARIANT_ROLE_PATTERN.fullmatch(name):
            raise _error(source, label, "pose names use lowercase letters, digits and '_'")
        pose = _mapping(pose_value, source, label)
        _reject_unknown(pose, {"adapter", "prompt"}, source, label)
        _required(pose, ("adapter", "prompt"), source, label)
        adapter = _string(pose["adapter"], source, f"{label}.adapter")
        if adapter not in models:
            raise _error(source, f"{label}.adapter", "must name a role of the models group it uses")
        poses[name] = PoseBehavior(
            adapter, _string(pose["prompt"], source, f"{label}.prompt").strip()
        )
    return Behavior(router_rules=rules, poses=poses)


def _chat_variants(
    value: Any, source: Path | str, models: Mapping[str, Mapping[str, Any]]
) -> tuple[ChatVariant, ...]:
    if not isinstance(value, list) or not value:
        raise _error(source, "variants", "must be a non-empty list")
    variants: list[ChatVariant] = []
    for index, item in enumerate(value):
        field = f"variants[{index}]"
        data = _mapping(item, source, field)
        _reject_unknown(data, {"id", "name", "images", "workflows", "behavior"}, source, field)
        _required(data, ("id", "name", "images", "workflows"), source, field)
        variant_id = _string(data["id"], source, f"{field}.id")
        if not variant_id[0].isalpha() or not all(
            c.islower() or c.isdigit() or c == "-" for c in variant_id
        ):
            raise _error(
                source,
                f"{field}.id",
                "must use lowercase letters, digits or '-' and start with a letter",
            )
        images = _string(data["images"], source, f"{field}.images")
        if images in ("chat", "image") or not VARIANT_ROLE_PATTERN.fullmatch(images):
            raise _error(
                source,
                f"{field}.images",
                "must name its own models group (lowercase letters, digits, '_'), "
                "not 'chat' or 'image'",
            )
        variants.append(
            ChatVariant(
                id=variant_id,
                name=_string(data["name"], source, f"{field}.name").strip(),
                images=images,
                workflows=_workflow_configs(data["workflows"], source, f"{field}.workflows"),
                behavior=_behavior(
                    data.get("behavior", {}), source, f"{field}.behavior", models.get(images, {})
                ),
            )
        )
    for attribute in ("id", "name", "images"):
        seen = [getattr(variant, attribute) for variant in variants]
        if len(set(seen)) != len(seen):
            raise _error(source, "variants", f"duplicate variant {attribute}")
    return tuple(variants)


def validate_profile_data(data: Any, source: Path | str = "<profile>") -> Profile:
    root = _mapping(data, source, "profile root")
    _reject_unknown(
        root,
        {
            "schema_version",
            "id",
            "name",
            "version",
            "description",
            "visibility",
            "ui",
            "hardware",
            "services",
            "features",
            "models",
            "workflows",
            "runtime",
            "variants",
            "character_swap",
            "behavior",
        },
        source,
        "profile root",
    )
    _required(
        root,
        (
            "schema_version",
            "id",
            "name",
            "version",
            "hardware",
            "services",
            "features",
            "models",
            "runtime",
        ),
        source,
    )

    schema_version = _integer(root["schema_version"], source, "schema_version", minimum=1)
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {schema_version}; expected {PROFILE_SCHEMA_VERSION}",
        )

    profile_id = _string(root["id"], source, "id")
    if not profile_id[0].isalpha() or not all(
        c.islower() or c.isdigit() or c in "_-" for c in profile_id
    ):
        raise _error(
            source,
            "id",
            "must use lowercase letters, digits, '_' or '-' and start with a letter",
        )

    ui_data = _mapping(root.get("ui", {}), source, "ui")
    _reject_unknown(ui_data, {"default_pet", "default_theme"}, source, "ui")

    def optional_ui_id(field: str) -> str | None:
        value = ui_data.get(field)
        if value is None:
            return None
        identifier = _string(value, source, f"ui.{field}")
        if not identifier[0].isalpha() or not all(
            char.islower() or char.isdigit() or char in "_-" for char in identifier
        ):
            raise _error(
                source,
                f"ui.{field}",
                "must use lowercase letters, digits, '_' or '-' and start with a letter",
            )
        return identifier

    hardware_data = _mapping(root["hardware"], source, "hardware")
    _reject_unknown(
        hardware_data,
        {
            "os",
            "gpu_vendor",
            "min_vram_gb",
            "recommended_vram_gb",
            "vram_tolerance_mib",
            "notes",
        },
        source,
        "hardware",
    )
    _required(
        hardware_data,
        ("os", "gpu_vendor", "min_vram_gb", "recommended_vram_gb"),
        source,
        "hardware",
    )
    min_vram_gib = _integer(hardware_data["min_vram_gb"], source, "hardware.min_vram_gb", minimum=1)
    recommended_vram_gib = _integer(
        hardware_data["recommended_vram_gb"],
        source,
        "hardware.recommended_vram_gb",
        minimum=1,
    )
    if recommended_vram_gib < min_vram_gib:
        raise _error(
            source,
            "hardware.recommended_vram_gb",
            "must be >= hardware.min_vram_gb",
        )
    tolerance_mib = _integer(
        hardware_data.get("vram_tolerance_mib", 0),
        source,
        "hardware.vram_tolerance_mib",
    )
    if tolerance_mib > MIB_PER_GIB:
        raise _error(source, "hardware.vram_tolerance_mib", f"must be <= {MIB_PER_GIB}")
    raw_os = hardware_data["os"]
    os_values = [raw_os] if isinstance(raw_os, str) else raw_os
    if not isinstance(os_values, list) or not os_values:
        raise _error(source, "hardware.os", "must name one or more supported operating systems")
    os_names = tuple(_string(value, source, "hardware.os").lower() for value in os_values)
    if len(set(os_names)) != len(os_names) or any(
        value not in {"windows", "linux"} for value in os_names
    ):
        raise _error(source, "hardware.os", "must contain unique 'windows' or 'linux' values")
    os_name = os_names
    gpu_vendor = _string(hardware_data["gpu_vendor"], source, "hardware.gpu_vendor").lower()
    if gpu_vendor != "nvidia":
        raise _error(source, "hardware.gpu_vendor", "only 'nvidia' is supported")

    services_data = _mapping(root["services"], source, "services")
    if not services_data:
        raise _error(source, "services", "must define at least one service")
    services: dict[str, ServiceConfig] = {}
    for raw_service_id, service_value in services_data.items():
        _string(raw_service_id, source, "services key")
        # Canonicalize once, on the way in: everything downstream — plan steps,
        # provisioners, runtime service ids — sees only canonical ids.
        service_id = normalize_component_id(raw_service_id)
        if service_id in services:
            raise _error(source, "services", f"duplicate service id: {service_id}")
        service = _mapping(service_value, source, f"services.{service_id}")
        _reject_unknown(
            service,
            {"enabled", "port"},
            source,
            f"services.{service_id}",
        )
        _required(service, ("enabled", "port"), source, f"services.{service_id}")
        port = _integer(service["port"], source, f"services.{service_id}.port", minimum=1)
        if port > 65535:
            raise _error(source, f"services.{service_id}.port", "must be <= 65535")
        services[service_id] = ServiceConfig(
            enabled=_boolean(service["enabled"], source, f"services.{service_id}.enabled"),
            port=port,
        )

    features_data = _mapping(root["features"], source, "features")
    features = {
        _string(key, source, "features key"): _boolean(value, source, f"features.{key}")
        for key, value in features_data.items()
    }

    models_data = _mapping(root["models"], source, "models")
    models = {
        _string(key, source, "models key"): _mapping(value, source, f"models.{key}")
        for key, value in models_data.items()
    }
    variants = _chat_variants(root["variants"], source, models) if "variants" in root else ()
    behavior = _behavior(root.get("behavior", {}), source, "behavior", models.get("image", {}))
    for variant in variants:
        if not models.get(variant.images):
            raise _error(
                source, f"models.{variant.images}", f"variant {variant.id!r} names no models here"
            )
    character_swap = (
        _workflow_configs(root["character_swap"], source, "character_swap")
        if "character_swap" in root
        else {}
    )
    groups = [item.images for item in variants]
    if "character_swap" in root:
        _reject_unknown(root["character_swap"], set(CHARACTER_SWAP_PARTS), source, "character_swap")
        _required(root["character_swap"], CHARACTER_SWAP_REQUIRED_PARTS, source, "character_swap")
        if not models.get(CHARACTER_SWAP_MODEL_GROUP):
            raise _error(source, "models.swap", "character_swap names no models here")

    workflows = _workflow_configs(root.get("workflows", {}), source, "workflows")

    runtime_data = _mapping(root["runtime"], source, "runtime")
    keep_alive_value = runtime_data.get(CHAT_KEEP_ALIVE_RUNTIME_KEY)
    chat_keep_alive = None
    if keep_alive_value is not None:
        chat_keep_alive = _string(
            keep_alive_value, source, f"runtime.{CHAT_KEEP_ALIVE_RUNTIME_KEY}"
        )
        if not KEEP_ALIVE_PATTERN.fullmatch(chat_keep_alive):
            raise _error(
                source,
                f"runtime.{CHAT_KEEP_ALIVE_RUNTIME_KEY}",
                'must be an Ollama duration such as "60m" or "-1"',
            )
    retention_value = runtime_data.get(COMFYUI_RETENTION_RUNTIME_KEY)
    comfyui_retention_hours = None
    if retention_value is not None:
        if (
            not isinstance(retention_value, int | float)
            or isinstance(retention_value, bool)
            or not retention_value > 0
        ):
            raise _error(
                source,
                f"runtime.{COMFYUI_RETENTION_RUNTIME_KEY}",
                "must be a number of hours > 0",
            )
        comfyui_retention_hours = float(retention_value)
    runtime = {
        _string(key, source, "runtime key"): _boolean(value, source, f"runtime.{key}")
        for key, value in runtime_data.items()
        if key not in (CHAT_KEEP_ALIVE_RUNTIME_KEY, COMFYUI_RETENTION_RUNTIME_KEY)
    }
    # The swap is reached only through the image router: without it nothing could offer
    # the swap, so the profile still loads but its parts and weights are neither planned
    # nor installed.
    if not runtime.get(IMAGE_ROUTER_RUNTIME_KEY):
        character_swap = {}
    if character_swap:
        groups.append(CHARACTER_SWAP_MODEL_GROUP)
    model_requirements = _model_contract(models, source, tuple(dict.fromkeys(groups)))
    if character_swap:
        model_requirements = _swap_requirements(models, source, character_swap, model_requirements)

    version = _string(root["version"], source, "version")
    if not PROFILE_VERSION_PATTERN.fullmatch(version):
        raise _error(source, "version", "must be MAJOR.MINOR.PATCH, e.g. 1.0.0")

    visibility = _string(root.get("visibility", VISIBILITY_PUBLIC), source, "visibility")
    if visibility not in VISIBILITIES:
        raise _error(source, "visibility", f"must be one of: {', '.join(VISIBILITIES)}")

    profile = Profile(
        schema_version=schema_version,
        id=profile_id,
        name=_string(root["name"], source, "name"),
        version=version,
        description=_string(root.get("description", ""), source, "description", allow_empty=True),
        visibility=visibility,
        ui=ProfileUiDefaults(
            default_pet=optional_ui_id("default_pet"),
            default_theme=optional_ui_id("default_theme"),
        ),
        hardware=HardwareRequirement(
            os=os_name,
            gpu_vendor=gpu_vendor,
            min_vram_mib=min_vram_gib * MIB_PER_GIB,
            recommended_vram_mib=recommended_vram_gib * MIB_PER_GIB,
            vram_tolerance_mib=tolerance_mib,
            notes=_string(
                hardware_data.get("notes", ""),
                source,
                "hardware.notes",
                allow_empty=True,
            ),
        ),
        services=services,
        features=features,
        models=models,
        model_requirements=model_requirements,
        workflows=workflows,
        runtime=runtime,
        chat_keep_alive=chat_keep_alive,
        comfyui_retention_hours=comfyui_retention_hours,
        raw=root,
        variants=variants,
        character_swap=character_swap,
        behavior=behavior,
    )
    problems = capability_problems(profile)
    if problems:
        raise _error(source, "features", "; ".join(problems))
    return profile


def _read_profile_data(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path}: could not read profile: {exc}") from exc


def _merge_overlay(base: Any, overlay: Any) -> Any:
    """Deep merge: mappings merge key by key, anything else is replaced by the overlay."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _merge_overlay(base[key], value) if key in base else value
        return merged
    return overlay


def _apply_overlay(base: dict[str, Any], overlay: dict[str, Any], source: Path) -> dict[str, Any]:
    """The base profile with one overlay laid over it.

    ``variants`` is a list: the overlay's entries are matched to the base's by ``id`` (merged
    when both have it, appended otherwise).
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key == "extends":
            continue
        if key == "variants":
            if not isinstance(value, list):
                raise _error(source, "variants", "must be a list")
            variants = list(merged.get("variants") or [])
            for index, item in enumerate(value):
                entry = _mapping(item, source, f"variants[{index}]")
                position = next(
                    (i for i, known in enumerate(variants) if known.get("id") == entry.get("id")),
                    None,
                )
                if position is None:
                    variants.append(entry)
                else:
                    variants[position] = _merge_overlay(variants[position], entry)
            merged["variants"] = variants
        else:
            merged[key] = _merge_overlay(merged.get(key), value)
    return merged


def _overlay_files(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every ``*.overlay.yaml`` of ``directory``, read and checked, in name order."""
    overlays: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob(f"*{OVERLAY_SUFFIX}")):
        data = _mapping(_read_profile_data(path), path, "overlay root")
        _reject_unknown(data, OVERLAY_KEYS, path, "overlay root")
        _required(data, ("extends",), path, "overlay root")
        _string(data["extends"], path, "extends")
        overlays.append((path, data))
    return overlays


def _with_overlays(data: Any, overlays: Sequence[tuple[Path, dict[str, Any]]]) -> Any:
    if not isinstance(data, dict):
        return data
    for overlay_path, overlay in overlays:
        if overlay["extends"] == data.get("id"):
            data = _apply_overlay(data, overlay, overlay_path)
    return data


def load_profile(path: Path) -> Profile:
    """One profile, with every overlay beside it that extends it laid over it."""
    data = _with_overlays(_read_profile_data(path), _overlay_files(path.parent))
    return validate_profile_data(data, path)


def offered_profiles(
    profiles: Sequence[Profile], *, env: Mapping[str, str] | None = None
) -> tuple[Profile, ...]:
    """The profiles a screen should offer a person.

    Internal ones are left out unless this machine asks for them through
    ``PEPITOS_AI_HUB_INTERNAL_PROFILES``, which is how a developer or a test
    gets at them without a profile id being special anywhere.
    """
    environment = os.environ if env is None else env
    if (environment.get(ENV_SHOW_INTERNAL_PROFILES, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return tuple(profiles)
    return tuple(profile for profile in profiles if not profile.internal)


def load_profiles(directory: Path) -> list[Profile]:
    """Every profile in ``directory``, ordered by id so a file's name decides nothing.

    ``*.overlay.yaml`` files are not profiles: each extends the profile whose id it names
    (an id that no profile has is an error).
    """
    overlays = _overlay_files(directory)
    loaded: list[tuple[Path, Any]] = [
        (path, _read_profile_data(path))
        for path in sorted(directory.glob("*.yaml"))
        if not path.name.endswith(OVERLAY_SUFFIX)
    ]
    known = {data.get("id") for _, data in loaded if isinstance(data, dict)}
    for overlay_path, overlay in overlays:
        if overlay["extends"] not in known:
            raise _error(
                overlay_path, "extends", f"no profile with id {overlay['extends']!r} to extend"
            )
    profiles = sorted(
        (validate_profile_data(_with_overlays(data, overlays), path) for path, data in loaded),
        key=lambda profile: profile.id,
    )
    seen: set[str] = set()
    duplicates: set[str] = set()
    for profile in profiles:
        if profile.id in seen:
            duplicates.add(profile.id)
        seen.add(profile.id)
    if duplicates:
        raise ProfileError(f"{directory}: duplicate profile ids: {', '.join(sorted(duplicates))}")
    return profiles
