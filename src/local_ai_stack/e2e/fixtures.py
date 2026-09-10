"""Repository fixtures: real manifests, real loaders, one declared profile.

Two P0 rows need a run that actually reaches managed configuration. The shipped
``pepe`` profile cannot: it requires 12.45 GB of ComfyUI weights, so a
controlled run resolves those roles as unavailable and configuration is blocked
long before a config file exists. Blocked is the correct answer there — it is
simply not the answer those two scenarios are about.

So a scenario points ``repo_root`` at a fixture repository instead. The
manifests and the workflow graphs are copied verbatim from the real repository;
only the profile is written here, and a profile is data the loader validates
like any other. Nothing about workflow resolution, configuration generation or
ownership is reimplemented — those still run from ``src`` unmodified.

The fixture lives in its own temporary directory and is removed by the
scenario's teardown, never inside the managed root, so it cannot be mistaken for
something the run installed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..artifacts import Artifact
from ..installation import detect_repo_root
from ..model_catalog import ModelCatalog, model_catalog
from ..models import (
    FileModelSource,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelProvider,
    ModelSpec,
)
from ..workflows import WorkflowParameter, load_workflow_manifest
from .seams import Injection, InjectionPlan
from .tracking import EXTERNAL_PREFIX, REPO_PREFIX, forget, make

FIXTURE_PREFIX = REPO_PREFIX

# What a repository has to hold for the install actions to bind to it: the
# profiles, the workflow manifest, and the graphs the manifest points at.
COPIED = ("manifests", "workflows")

CHAT_ONLY_PROFILE = "e2e-chat"
BROKEN_WORKFLOW_PROFILE = "e2e-workflow"
STACK_PROFILE = "e2e-stack"

CHAT_REFERENCE = "hermes3:8b"
CHAT_DIGEST = "4f6b83f30b62aaaa"

# Three fixture weights, one per role the shipped text-to-image graph loads.
# They are a few dozen bytes each: small enough to serve from memory, real
# enough that the installer downloads them, checks the SHA-256 it was pinned
# to, and places them through ManagedModelPaths like any other file model.
FIXTURE_MODELS = (
    (
        "image.diffusion",
        "e2e_diffusion",
        ModelKind.DIFFUSION,
        ModelPlacement.DIFFUSION_MODELS,
        "e2e-diffusion.safetensors",
    ),
    (
        "image.text_encoder",
        "e2e_text_encoder",
        ModelKind.TEXT_ENCODER,
        ModelPlacement.TEXT_ENCODERS,
        "e2e-text-encoder.safetensors",
    ),
    ("image.vae", "e2e_vae", ModelKind.VAE, ModelPlacement.VAE, "e2e-vae.safetensors"),
)

MODEL_URL = "https://controlled.invalid/models/{filename}"

# The workflow the image profile declares, and therefore the graph the
# workflow-source seam corrupts.
IMAGE_WORKFLOW_ID = "flux2_klein_t2i"

# Where each provisioner looks for an installation it does not own. These
# are the provisioner's own hints, so an external fixture is found by the
# real detection path rather than by anything declared here.
EXTERNAL_LAYOUT = {
    "ollama": ("LOCALAPPDATA", Path("Programs") / "Ollama", "ollama.exe"),
    "comfyui": ("LOCALAPPDATA", Path("Comfy-Desktop"), None),
}

_HEADER = """schema_version: 1
id: {id}
name: {name}
ui:
  default_pet: none
  default_theme: neutral
description: {description}

hardware:
  os: windows
  gpu_vendor: nvidia
  min_vram_gb: 12
  recommended_vram_gb: 12
  vram_tolerance_mib: 128
  notes: Mirrors the controlled system facts, so compatibility is not the subject.
"""

_RUNTIME = """
runtime:
  aggressive_vram_unload: true
  prefer_single_heavy_model_resident: true
  open_browser: false
"""

# Chat only. Every role this profile declares resolves against the controlled
# Ollama store, so configuration is genuinely generated and written.
CHAT_ONLY_YAML = (
    _HEADER.format(
        id=CHAT_ONLY_PROFILE,
        name="E2E chat only",
        description="Fixture profile whose declared models all resolve in a controlled run.",
    )
    + """
services:
  ollama: {enabled: true, port: 11434}
  open-webui: {enabled: true, port: 8080}
  comfyui: {enabled: false, port: 8188}

features:
  chat: true
  vision: false
  tools: true
  image_generation: false
  image_editing: false

models:
  chat:
    primary: ollama/hermes3_8b
    status: candidate
"""
    + _RUNTIME
)

# Chat resolves, image generation is enabled, and the declared text_to_image
# workflow requires three model roles this profile never declares. The workflow
# domain answers that with MODEL_NOT_DECLARED; nothing here decides it.
BROKEN_WORKFLOW_YAML = (
    _HEADER.format(
        id=BROKEN_WORKFLOW_PROFILE,
        name="E2E broken workflow",
        description="Fixture profile whose image workflow requires roles it never declares.",
    )
    + """
services:
  ollama: {enabled: true, port: 11434}
  open-webui: {enabled: true, port: 8080}
  comfyui: {enabled: true, port: 8188}

features:
  chat: true
  vision: false
  tools: true
  image_generation: true
  image_editing: false

models:
  chat:
    primary: ollama/hermes3_8b
    status: candidate

workflows:
  text_to_image: {manifest_id: flux2_klein_t2i}
"""
    + _RUNTIME
)

# Everything a text-to-image run needs, all of it installable offline: the chat
# model comes from the controlled Ollama store, the three image roles from the
# fixture catalog below. This is the only profile that reaches image smoke.
STACK_YAML = (
    _HEADER.format(
        id=STACK_PROFILE,
        name="E2E stack",
        description="Fixture profile whose every declared model is installable offline.",
    )
    + """
services:
  ollama: {enabled: true, port: 11434}
  open-webui: {enabled: true, port: 8080}
  comfyui: {enabled: true, port: 8188}

features:
  chat: true
  vision: false
  tools: true
  image_generation: true
  image_editing: false

models:
  chat:
    primary: ollama/hermes3_8b
    status: candidate
  image:
    diffusion: comfyui/e2e_diffusion
    text_encoder: comfyui/e2e_text_encoder
    vae: comfyui/e2e_vae

workflows:
  text_to_image: {manifest_id: flux2_klein_t2i}
"""
    + _RUNTIME
)

PROFILE_YAML = {
    CHAT_ONLY_PROFILE: CHAT_ONLY_YAML,
    BROKEN_WORKFLOW_PROFILE: BROKEN_WORKFLOW_YAML,
    STACK_PROFILE: STACK_YAML,
}


def fixture_model_payload(name: str) -> bytes:
    """Deterministic bytes for one fixture weight.

    Deterministic matters twice over: the checksum in the catalog is the digest
    of exactly these bytes, and a run that produced different bytes each time
    could not tell a corrupted file from a new one.
    """
    return f"pepito-e2e fixture weights for {name}\n".encode()


def fixture_model_catalog(register) -> ModelCatalog:
    """The shipped catalog plus three installable fixture weights.

    ``register(url, payload) -> sha256`` is the controlled transport's own
    registration, so the checksum pinned here is genuinely the digest of the
    bytes that will be served. Nothing about verification is faked: the
    installer downloads, hashes and compares exactly as it does for a real
    12 GB weight.
    """
    shipped = model_catalog()
    specs = dict(shipped.specs)
    for _role, name, kind, placement, filename in FIXTURE_MODELS:
        model_id = ModelId.from_parts(ModelProvider.COMFYUI, name)
        url = MODEL_URL.format(filename=filename)
        payload = fixture_model_payload(name)
        digest = register(url, payload)
        specs[model_id] = ModelSpec(
            id=model_id,
            kind=kind,
            source=FileModelSource(
                artifact=Artifact(
                    id=f"fixture-{name}",
                    url=url,
                    filename=filename,
                    sha256=digest,
                    size_bytes=len(payload),
                    version="e2e",
                    component="comfyui",
                ),
                placement=placement,
            ),
        )
    return ModelCatalog(specs=specs, unpinned=dict(shipped.unpinned), aliases=dict(shipped.aliases))


@dataclass
class ExternalInstall:
    """An installation this hub does not own, on disk where the hub looks.

    Detection reads the filesystem, so an external component has to actually be
    one: a directory, and for Ollama an executable, outside the managed root.
    ``env`` and ``on_path`` are what the controlled runner reports, exactly as a
    real environment would.
    """

    component: str
    base: Path
    path: Path
    env: dict[str, str]
    on_path: dict[str, str]

    def snapshot(self) -> dict[str, bytes]:
        """Every file under the external installation, by content.

        Compared before and after a run, this is what proves the hub did not
        modify, replace or remove something it does not own.
        """
        return {
            str(item.relative_to(self.base)): item.read_bytes()
            for item in sorted(self.base.rglob("*"))
            if item.is_file()
        }

    def release(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)
        forget(self.base)


def external_install(component: str) -> ExternalInstall:
    """Create an external installation of one component and say where it is."""
    try:
        variable, relative, executable = EXTERNAL_LAYOUT[component]
    except KeyError:
        known = ", ".join(sorted(EXTERNAL_LAYOUT))
        raise KeyError(f"no external layout for {component!r}; known: {known}") from None

    base = make(f"{EXTERNAL_PREFIX}{component}-")
    directory = base / relative
    directory.mkdir(parents=True)
    path = directory
    on_path: dict[str, str] = {}
    if executable is not None:
        path = directory / executable
        path.write_bytes(b"external installation; never touched by the hub\n")
        on_path[component] = str(path)
    return ExternalInstall(
        component=component,
        base=base,
        path=path,
        env={variable: str(base)},
        on_path=on_path,
    )


@dataclass
class RepoFixture:
    """A repository root a run can be pointed at, and the promise to remove it."""

    path: Path
    profile_id: str
    # Node ids the workflow-source seam removed, empty when nothing was injected.
    broken_nodes: tuple[str, ...] = ()

    def release(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
        forget(self.path)


def repo_fixture(
    profile_id: str,
    *,
    source: Path | None = None,
    injections: InjectionPlan | None = None,
) -> RepoFixture:
    """Copy the real manifests and graphs, then declare one fixture profile.

    This is the ``workflow-source`` seam: what a run reads its profiles,
    manifests and workflow graphs from. Under ``INVALID_WORKFLOW`` the graph it
    copies is corrupted on the way in, so the workflow domain is handed a real
    template that no longer matches its own manifest.
    """
    try:
        body = PROFILE_YAML[profile_id]
    except KeyError:
        known = ", ".join(sorted(PROFILE_YAML))
        raise KeyError(f"unknown fixture profile {profile_id!r}; known: {known}") from None

    plan = injections or InjectionPlan.none()
    origin = Path(source) if source is not None else detect_repo_root()
    root = make(FIXTURE_PREFIX)
    for name in COPIED:
        shutil.copytree(origin / name, root / name)
    profiles = root / "profiles"
    profiles.mkdir()
    (profiles / f"{profile_id}.yaml").write_text(body, encoding="utf-8")

    broken = ()
    if plan.active(Injection.INVALID_WORKFLOW):
        broken = break_workflow_template(root)
    return RepoFixture(path=root, profile_id=profile_id, broken_nodes=broken)


def break_workflow_template(root: Path, *, workflow: str = IMAGE_WORKFLOW_ID) -> tuple[str, ...]:
    """Remove the node the manifest's model binding addresses.

    A structural defect, not a missing model: the manifest still declares
    ``model -> node 75:70, unet_name`` and the graph no longer has that node, so
    the binding addresses nothing. ``verify_workflow_references`` is what says
    that, here as everywhere else -- this only edits the file.

    Returns the node ids it removed, so a scenario can prove the corruption
    happened rather than assume it.
    """
    specs = load_workflow_manifest(root / "manifests" / "workflows.yaml")
    spec = specs[workflow]
    template = root / spec.file
    graph = json.loads(template.read_text(encoding="utf-8"))

    removed = []
    node_id = spec.binding(WorkflowParameter.MODEL).node_id
    if node_id in graph:
        del graph[node_id]
        removed.append(node_id)
    template.write_text(json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    return tuple(removed)


__all__ = [
    "BROKEN_WORKFLOW_PROFILE",
    "CHAT_DIGEST",
    "CHAT_ONLY_PROFILE",
    "CHAT_REFERENCE",
    "EXTERNAL_LAYOUT",
    "FIXTURE_MODELS",
    "FIXTURE_PREFIX",
    "IMAGE_WORKFLOW_ID",
    "PROFILE_YAML",
    "STACK_PROFILE",
    "ExternalInstall",
    "RepoFixture",
    "break_workflow_template",
    "external_install",
    "fixture_model_catalog",
    "fixture_model_payload",
    "repo_fixture",
]
