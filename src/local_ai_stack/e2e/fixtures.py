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

import shutil
import tempfile
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

FIXTURE_PREFIX = "pepito-e2e-repo-"

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
class RepoFixture:
    """A repository root a run can be pointed at, and the promise to remove it."""

    path: Path
    profile_id: str

    def release(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def repo_fixture(profile_id: str, *, source: Path | None = None) -> RepoFixture:
    """Copy the real manifests and graphs, then declare one fixture profile."""
    try:
        body = PROFILE_YAML[profile_id]
    except KeyError:
        known = ", ".join(sorted(PROFILE_YAML))
        raise KeyError(f"unknown fixture profile {profile_id!r}; known: {known}") from None

    origin = Path(source) if source is not None else detect_repo_root()
    root = Path(tempfile.mkdtemp(prefix=FIXTURE_PREFIX))
    for name in COPIED:
        shutil.copytree(origin / name, root / name)
    profiles = root / "profiles"
    profiles.mkdir()
    (profiles / f"{profile_id}.yaml").write_text(body, encoding="utf-8")
    return RepoFixture(path=root, profile_id=profile_id)


__all__ = [
    "BROKEN_WORKFLOW_PROFILE",
    "CHAT_DIGEST",
    "CHAT_ONLY_PROFILE",
    "CHAT_REFERENCE",
    "FIXTURE_MODELS",
    "FIXTURE_PREFIX",
    "PROFILE_YAML",
    "STACK_PROFILE",
    "RepoFixture",
    "fixture_model_catalog",
    "fixture_model_payload",
    "repo_fixture",
]
