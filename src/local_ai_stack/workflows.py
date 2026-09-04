"""Managed ComfyUI workflows: which models they need, and where values go.

A workflow in ComfyUI's API format is a graph whose nodes are addressed by
opaque ids. Those ids belong in one declarative place — the workflow manifest —
and nowhere else. Application code asks for ``prompt`` or ``steps``; this module
is the only thing that knows those are node ``76`` and node ``75:62``. Re-export
the graph from ComfyUI and the manifest changes; no caller does.

Models arrive as :class:`~local_ai_stack.models.ResolvedModel` and are used
through ``require_location()``: the ``runtime_name`` goes into the node, and the
absolute path is what the ComfyUI search-path configuration is built from. This
module never sees a URL, a checksum, a catalog or a downloader, and never turns
a filename back into a path.

Fail closed: a workflow declares *every* role it loads, and all of them must
resolve to ``AVAILABLE`` before it can be rendered. A graph that needs a
diffusion model, a text encoder and a VAE is not usable with two of the three,
and nothing here quietly waits for fewer: the manifest lists them, and a role
that is missing, invalid, external or undeclared blocks the workflow with that
reason attached.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .models import (
    FileModelLocation,
    ModelId,
    ModelResolver,
    ModelUnavailableError,
    ResolvedModel,
)
from .profiles import Profile

WORKFLOW_SCHEMA_VERSION = 1
UNRESOLVED = "TBD"


class WorkflowError(ValueError):
    """A workflow manifest is malformed, or asks for something impossible."""


class WorkflowParameter(StrEnum):
    """The vocabulary callers use. Node ids never leave the manifest."""

    PROMPT = "prompt"
    MODEL = "model"
    WIDTH = "width"
    HEIGHT = "height"
    STEPS = "steps"
    SEED = "seed"
    IMAGE = "image"


# Which parameter is filled from a resolved model rather than by the caller.
MODEL_PARAMETERS: frozenset[WorkflowParameter] = frozenset({WorkflowParameter.MODEL})


@dataclass(frozen=True)
class NodeBinding:
    """One parameter's address inside the graph: a node and a field on it."""

    node_id: str
    key: str

    def __post_init__(self) -> None:
        if not self.node_id or not self.key:
            raise WorkflowError("a node binding needs both a node id and a key")


@dataclass(frozen=True)
class WorkflowSpec:
    """A workflow the manager can materialise, declared entirely by manifest.

    ``required_roles`` is the whole set of models the graph loads, and
    ``parameter_role`` is the one of those that the ``model`` binding names.
    Both come from the manifest; neither has a default, because a hidden
    default is how a workflow ends up silently missing an encoder.
    """

    id: str
    file: Path
    bindings: Mapping[WorkflowParameter, NodeBinding]
    required_roles: tuple[str, ...]
    parameter_role: str
    status: str = ""
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.required_roles:
            raise WorkflowError(f"workflow {self.id!r} must declare the models it requires")
        if self.parameter_role not in self.required_roles:
            raise WorkflowError(
                f"workflow {self.id!r}: parameter role {self.parameter_role!r} is not one of "
                f"the required roles ({', '.join(self.required_roles)})"
            )

    @property
    def complete(self) -> bool:
        """Whether every parameter this workflow declares has an address."""
        return not self.unresolved

    @property
    def parameters(self) -> tuple[WorkflowParameter, ...]:
        return tuple(sorted(self.bindings, key=lambda item: item.value))

    def binding(self, parameter: WorkflowParameter) -> NodeBinding:
        try:
            return self.bindings[parameter]
        except KeyError:
            known = ", ".join(p.value for p in self.parameters)
            raise WorkflowError(
                f"workflow {self.id!r} has no binding for {parameter.value!r} (has: {known})"
            ) from None


@dataclass(frozen=True)
class WorkflowParameters:
    """What a caller chooses. The model is never one of these."""

    prompt: str = ""
    width: int = 1024
    height: int = 1024
    steps: int = 20
    seed: int = 0

    def as_mapping(self) -> dict[WorkflowParameter, Any]:
        return {
            WorkflowParameter.PROMPT: self.prompt,
            WorkflowParameter.WIDTH: self.width,
            WorkflowParameter.HEIGHT: self.height,
            WorkflowParameter.STEPS: self.steps,
            WorkflowParameter.SEED: self.seed,
        }


class WorkflowState(StrEnum):
    READY = "ready"
    MODEL_UNAVAILABLE = "model-unavailable"
    MODEL_NOT_DECLARED = "model-not-declared"
    MAPPING_INCOMPLETE = "mapping-incomplete"
    TEMPLATE_MISSING = "template-missing"


@dataclass(frozen=True)
class WorkflowResolution:
    """A workflow checked against one profile and one resolver.

    ``models`` holds every role the workflow declared, so a caller can see
    which one is missing rather than only that something is.
    """

    spec: WorkflowSpec
    state: WorkflowState
    models: Mapping[str, ResolvedModel] = field(default_factory=dict)
    template_path: Path | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def model(self) -> ResolvedModel | None:
        """The model the ``model`` parameter names, when it resolved at all."""
        return self.models.get(self.spec.parameter_role)

    @property
    def ready(self) -> bool:
        return self.state is WorkflowState.READY

    @property
    def runtime_name(self) -> str:
        """The file name ComfyUI nodes must use for this workflow's model."""
        location = self.require_model_location()
        return location.runtime_name

    @property
    def model_path(self) -> Path:
        location = self.require_model_location()
        return location.path

    def require_model_location(self) -> FileModelLocation:
        model = self.model
        if model is None:
            raise ModelUnavailableError(
                f"workflow {self.spec.id!r} has no resolved model: {'; '.join(self.problems)}"
            )
        location = model.require_location()
        if not isinstance(location, FileModelLocation):
            raise WorkflowError(
                f"workflow {self.spec.id!r} needs a file model, got {location.provider.value}"
            )
        return location


# --- manifest loading -------------------------------------------------------


def _model_roles(
    entry: dict[str, Any], workflow_id: str, source: str
) -> tuple[tuple[str, ...], str]:
    """Read the explicit model contract of one workflow. No defaults."""
    models = entry.get("models")
    if not isinstance(models, dict):
        raise WorkflowError(
            f"{source}: workflow {workflow_id!r} must declare a models block naming every "
            "role it loads"
        )
    unknown = sorted(str(key) for key in models if str(key) not in {"required", "parameter"})
    if unknown:
        raise WorkflowError(
            f"{source}: workflow {workflow_id!r}: models has unknown fields: {', '.join(unknown)}"
        )
    required = models.get("required")
    if not isinstance(required, list) or not required:
        raise WorkflowError(
            f"{source}: workflow {workflow_id!r}: models.required must be a non-empty list of roles"
        )
    roles = tuple(str(role) for role in required)
    parameter = models.get("parameter")
    if not isinstance(parameter, str) or not parameter:
        raise WorkflowError(
            f"{source}: workflow {workflow_id!r}: models.parameter must name the role the "
            "model input uses"
        )
    return roles, parameter


def _binding(value: Any, workflow_id: str, parameter: str) -> NodeBinding | None:
    """Parse one mapping entry. ``TBD`` means declared but not yet known."""
    if isinstance(value, str) and value.strip().upper() == UNRESOLVED:
        return None
    if not isinstance(value, dict):
        raise WorkflowError(
            f"workflow {workflow_id!r}: mapping.{parameter} must be a node binding or {UNRESOLVED}"
        )
    unknown = sorted(str(key) for key in value if str(key) not in {"node_id", "key"})
    if unknown:
        raise WorkflowError(
            f"workflow {workflow_id!r}: mapping.{parameter} has unknown fields: "
            f"{', '.join(unknown)}"
        )
    missing = [field_name for field_name in ("node_id", "key") if field_name not in value]
    if missing:
        raise WorkflowError(
            f"workflow {workflow_id!r}: mapping.{parameter} is missing {', '.join(missing)}"
        )
    return NodeBinding(str(value["node_id"]), str(value["key"]))


def parse_workflow_manifest(
    data: Any, *, source: str = "<workflows>", model_roles: Mapping[str, str] | None = None
) -> dict[str, WorkflowSpec]:
    """Turn a workflow manifest into specs. Node ids stop here."""
    if not isinstance(data, dict):
        raise WorkflowError(f"{source}: workflow manifest must be a mapping")
    version = data.get("schema_version")
    if version != WORKFLOW_SCHEMA_VERSION:
        raise WorkflowError(
            f"{source}: unsupported workflow schema_version {version!r}; "
            f"expected {WORKFLOW_SCHEMA_VERSION}"
        )
    entries = data.get("workflows")
    if not isinstance(entries, dict):
        raise WorkflowError(f"{source}: 'workflows' must be a mapping")

    roles = dict(model_roles or {})
    specs: dict[str, WorkflowSpec] = {}
    for workflow_id, entry in entries.items():
        if not isinstance(entry, dict):
            raise WorkflowError(f"{source}: workflow {workflow_id!r} must be a mapping")
        file_value = entry.get("file")
        if not isinstance(file_value, str) or not file_value.strip():
            raise WorkflowError(f"{source}: workflow {workflow_id!r} must declare a file")
        mapping = entry.get("mapping")
        if not isinstance(mapping, dict):
            raise WorkflowError(f"{source}: workflow {workflow_id!r} must declare a mapping")

        bindings: dict[WorkflowParameter, NodeBinding] = {}
        unresolved: list[str] = []
        for raw_parameter, raw_binding in mapping.items():
            try:
                parameter = WorkflowParameter(str(raw_parameter))
            except ValueError:
                known = ", ".join(p.value for p in WorkflowParameter)
                raise WorkflowError(
                    f"{source}: workflow {workflow_id!r} maps unknown parameter "
                    f"{raw_parameter!r} (known: {known})"
                ) from None
            binding = _binding(raw_binding, str(workflow_id), parameter.value)
            if binding is None:
                unresolved.append(parameter.value)
            else:
                bindings[parameter] = binding

        required_roles, parameter_role = _model_roles(entry, str(workflow_id), source)
        specs[str(workflow_id)] = WorkflowSpec(
            id=str(workflow_id),
            file=Path(file_value),
            bindings=bindings,
            required_roles=required_roles,
            parameter_role=roles.get(str(workflow_id), parameter_role),
            status=str(entry.get("status", "")),
            unresolved=tuple(sorted(unresolved)),
        )
    return specs


def load_workflow_manifest(
    path: Path, *, model_roles: Mapping[str, str] | None = None
) -> dict[str, WorkflowSpec]:
    if not path.is_file():
        raise WorkflowError(f"{path}: workflow manifest not found")
    return parse_workflow_manifest(
        yaml.safe_load(path.read_text(encoding="utf-8")),
        source=str(path),
        model_roles=model_roles,
    )


# --- resolution -------------------------------------------------------------


def _profile_model_id(profile: Profile, role: str) -> ModelId | None:
    for requirement in profile.model_requirements:
        if requirement.role == role:
            return requirement.model_id
    return None


def resolve_workflow(
    spec: WorkflowSpec,
    profile: Profile,
    resolver: ModelResolver,
    *,
    repo_root: Path | None = None,
) -> WorkflowResolution:
    """Check one workflow against the profile's models and the graph on disk.

    Every declared role is resolved, not just the one the ``model`` input
    names: a graph that loads a text encoder and a VAE is unusable without
    them, whatever the diffusion model says. Every failure is a refusal with a
    reason; nothing is invented.
    """
    resolved: dict[str, ResolvedModel] = {}
    undeclared: list[str] = []
    unavailable: list[str] = []

    for role in spec.required_roles:
        model_id = _profile_model_id(profile, role)
        if model_id is None:
            undeclared.append(role)
            continue
        model = resolver.resolve(model_id)
        resolved[role] = model
        if not model.available:
            detail = f": {model.detail}" if model.detail else ""
            unavailable.append(f"{role} ({model_id}) is {model.state.value}{detail}")

    if undeclared:
        return WorkflowResolution(
            spec,
            WorkflowState.MODEL_NOT_DECLARED,
            resolved,
            problems=tuple(
                f"profile {profile.id!r} declares no model for role {role!r}" for role in undeclared
            ),
        )
    if unavailable:
        return WorkflowResolution(
            spec,
            WorkflowState.MODEL_UNAVAILABLE,
            resolved,
            problems=tuple(f"model for {problem} and cannot be used" for problem in unavailable),
        )

    template = (repo_root / spec.file) if repo_root is not None else spec.file
    if not spec.complete:
        return WorkflowResolution(
            spec,
            WorkflowState.MAPPING_INCOMPLETE,
            resolved,
            template,
            (f"workflow {spec.id!r} has no node binding for: {', '.join(spec.unresolved)}",),
        )
    if not template.is_file():
        return WorkflowResolution(
            spec,
            WorkflowState.TEMPLATE_MISSING,
            resolved,
            template,
            (f"workflow graph not present in the repository: {spec.file}",),
        )
    return WorkflowResolution(spec, WorkflowState.READY, resolved, template)


def resolve_workflows(
    specs: Mapping[str, WorkflowSpec],
    profile: Profile,
    resolver: ModelResolver,
    *,
    repo_root: Path | None = None,
    only: Iterable[str] | None = None,
) -> dict[str, WorkflowResolution]:
    wanted = set(only) if only is not None else set(specs)
    return {
        workflow_id: resolve_workflow(spec, profile, resolver, repo_root=repo_root)
        for workflow_id, spec in specs.items()
        if workflow_id in wanted
    }


# --- materialisation --------------------------------------------------------


def _assign(graph: dict[str, Any], binding: NodeBinding, value: Any, workflow_id: str) -> None:
    node = graph.get(binding.node_id)
    if not isinstance(node, dict):
        raise WorkflowError(
            f"workflow {workflow_id!r}: node {binding.node_id!r} is not in the graph"
        )
    inputs = node.get("inputs")
    target = inputs if isinstance(inputs, dict) else node
    if binding.key not in target:
        raise WorkflowError(
            f"workflow {workflow_id!r}: node {binding.node_id!r} has no field {binding.key!r}"
        )
    target[binding.key] = value


def render_workflow(
    resolution: WorkflowResolution,
    parameters: WorkflowParameters,
    *,
    graph: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the API-format graph to submit, with every value in place.

    The model field is filled from the resolved location's ``runtime_name`` —
    the name ComfyUI itself will look up — and never from a path or an id.
    """
    if not resolution.ready:
        raise ModelUnavailableError(
            f"workflow {resolution.spec.id!r} is {resolution.state.value}: "
            + "; ".join(resolution.problems)
        )
    source = graph if graph is not None else load_workflow_graph(resolution)
    rendered = deepcopy(dict(source))
    spec = resolution.spec

    for parameter, value in parameters.as_mapping().items():
        if parameter in spec.bindings:
            _assign(rendered, spec.binding(parameter), value, spec.id)
    if WorkflowParameter.MODEL in spec.bindings:
        _assign(rendered, spec.binding(WorkflowParameter.MODEL), resolution.runtime_name, spec.id)
    return rendered


def load_workflow_graph(resolution: WorkflowResolution) -> dict[str, Any]:
    """Load the declared API graph for a ready workflow without mutating it."""
    import json

    path = resolution.template_path
    if path is None or not path.is_file():
        raise WorkflowError(f"workflow graph not available for {resolution.spec.id!r}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WorkflowError(f"workflow graph for {resolution.spec.id!r} must be an object")
    return payload


def verify_workflow_references(
    resolution: WorkflowResolution, graph: Mapping[str, Any]
) -> tuple[str, ...]:
    """Every declared binding must address a field that exists in the graph."""
    problems: list[str] = []
    for parameter in resolution.spec.parameters:
        binding = resolution.spec.binding(parameter)
        node = graph.get(binding.node_id)
        if not isinstance(node, dict):
            problems.append(f"{parameter.value}: node {binding.node_id} is missing")
            continue
        inputs = node.get("inputs")
        target = inputs if isinstance(inputs, dict) else node
        if binding.key not in target:
            problems.append(f"{parameter.value}: node {binding.node_id} has no field {binding.key}")
    return tuple(problems)


__all__: Sequence[str] = (
    "MODEL_PARAMETERS",
    "NodeBinding",
    "WorkflowError",
    "WorkflowParameter",
    "WorkflowParameters",
    "WorkflowResolution",
    "WorkflowSpec",
    "WorkflowState",
    "load_workflow_graph",
    "load_workflow_manifest",
    "parse_workflow_manifest",
    "render_workflow",
    "resolve_workflow",
    "resolve_workflows",
    "verify_workflow_references",
)
