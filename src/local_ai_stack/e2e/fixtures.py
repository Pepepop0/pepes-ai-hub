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

from ..installation import detect_repo_root

FIXTURE_PREFIX = "pepito-e2e-repo-"

# What a repository has to hold for the install actions to bind to it: the
# profiles, the workflow manifest, and the graphs the manifest points at.
COPIED = ("manifests", "workflows")

CHAT_ONLY_PROFILE = "e2e-chat"
BROKEN_WORKFLOW_PROFILE = "e2e-workflow"

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

PROFILE_YAML = {
    CHAT_ONLY_PROFILE: CHAT_ONLY_YAML,
    BROKEN_WORKFLOW_PROFILE: BROKEN_WORKFLOW_YAML,
}


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
    "CHAT_ONLY_PROFILE",
    "FIXTURE_PREFIX",
    "PROFILE_YAML",
    "RepoFixture",
    "repo_fixture",
]
