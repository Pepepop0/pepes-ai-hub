from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .components import ComponentId, normalize_component_id
from .models import ModelContractError, ModelId, ModelRequirement

PROFILE_SCHEMA_VERSION = 1
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
    os: str
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
class ProfileUiDefaults:
    default_pet: str | None = None
    default_theme: str | None = None


@dataclass(frozen=True)
class Profile:
    schema_version: int
    id: str
    name: str
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
    raw: dict[str, Any]

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

    @property
    def chat_params(self) -> dict[str, bool | int | float | str]:
        """What the profile asks of its chat model at every request: Open WebUI's
        own model parameters (``think``, ``num_ctx``, ...), validated on load.
        Empty when the profile leaves the model's defaults alone."""
        return dict(self.models.get("chat", {}).get("params") or {})


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
    return tuple(problems)


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
    models: dict[str, dict[str, Any]], source: Path | str
) -> tuple[ModelRequirement, ...]:
    """Validate current profile roles without changing their public YAML shape."""
    requirements: list[ModelRequirement] = []
    active_roles = {
        "chat": ("primary",),
        "image": ("diffusion", "text_encoder", "vae"),
    }
    for group, fields in active_roles.items():
        values = models.get(group, {})
        for field in fields:
            if field not in values:
                continue
            model_id = _model_id(values[field], source, f"models.{group}.{field}")
            if model_id is not None:
                requirements.append(ModelRequirement(f"{group}.{field}", model_id))

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
    return tuple(requirements)


def validate_profile_data(data: Any, source: Path | str = "<profile>") -> Profile:
    root = _mapping(data, source, "profile root")
    _reject_unknown(
        root,
        {
            "schema_version",
            "id",
            "name",
            "description",
            "visibility",
            "ui",
            "hardware",
            "services",
            "features",
            "models",
            "workflows",
            "runtime",
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
    os_name = _string(hardware_data["os"], source, "hardware.os").lower()
    if os_name != "windows":
        raise _error(source, "hardware.os", "only 'windows' is supported")
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
    model_requirements = _model_contract(models, source)

    workflows_data = _mapping(root.get("workflows", {}), source, "workflows")
    workflows: dict[str, WorkflowConfig] = {}
    for workflow_id, workflow_value in workflows_data.items():
        workflow = _mapping(workflow_value, source, f"workflows.{workflow_id}")
        _reject_unknown(
            workflow,
            {"manifest_id", "size", "steps"},
            source,
            f"workflows.{workflow_id}",
        )
        _required(workflow, ("manifest_id",), source, f"workflows.{workflow_id}")
        size = workflow.get("size")
        if size is not None:
            size = _string(size, source, f"workflows.{workflow_id}.size")
            if not IMAGE_SIZE_PATTERN.match(size):
                raise _error(
                    source, f"workflows.{workflow_id}.size", "must be WIDTHxHEIGHT, e.g. 1024x1024"
                )
        steps = workflow.get("steps")
        if steps is not None:
            steps = _integer(steps, source, f"workflows.{workflow_id}.steps", minimum=1)
        workflows[_string(workflow_id, source, "workflows key")] = WorkflowConfig(
            manifest_id=_string(
                workflow["manifest_id"],
                source,
                f"workflows.{workflow_id}.manifest_id",
            ),
            size=size,
            steps=steps,
        )

    runtime_data = _mapping(root["runtime"], source, "runtime")
    runtime = {
        _string(key, source, "runtime key"): _boolean(value, source, f"runtime.{key}")
        for key, value in runtime_data.items()
    }

    visibility = _string(root.get("visibility", VISIBILITY_PUBLIC), source, "visibility")
    if visibility not in VISIBILITIES:
        raise _error(source, "visibility", f"must be one of: {', '.join(VISIBILITIES)}")

    profile = Profile(
        schema_version=schema_version,
        id=profile_id,
        name=_string(root["name"], source, "name"),
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
        raw=root,
    )
    problems = capability_problems(profile)
    if problems:
        raise _error(source, "features", "; ".join(problems))
    return profile


def load_profile(path: Path) -> Profile:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProfileError(f"{path}: could not read profile: {exc}") from exc
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
    """Every profile in ``directory``, ordered by id so a file's name decides nothing."""
    profiles = sorted(
        (load_profile(path) for path in sorted(directory.glob("*.yaml"))),
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
