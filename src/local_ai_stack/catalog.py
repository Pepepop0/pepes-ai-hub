"""What this checkout is pinned to.

The catalog answers one question: for this component, which exact upstream
release did we commit to? It reads the manifests that ship with the repository
and hands back declarative records. It resolves nothing at runtime, contacts no
network and never asks upstream what "latest" means — a version bump is an edit
to a manifest file, reviewed like any other change.

Two kinds of pin exist, because the components are distributed differently:

- an :class:`~local_ai_stack.artifacts.Artifact` for a downloadable archive,
  pinned by version and SHA-256 (Ollama);
- a :class:`PinnedRepository` for a source checkout, pinned by tag *and* the
  commit that tag pointed at when it was recorded (ComfyUI), so a moved tag is
  detectable instead of silently followed.

Manifests live beside the source in the repository, so a wheel install without
them simply has no pins. That is reported as an empty catalog, and a
provisioner then refuses to install rather than guessing a URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .artifacts import Artifact, ArtifactError, load_artifact_manifest
from .components import normalize_component_id
from .installation import InstallLocationError, detect_repo_root

ARTIFACT_MANIFEST = "artifacts.yaml"
REPOSITORY_MANIFEST = "repositories.yaml"
MANIFEST_DIR = "manifests"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class CatalogError(ValueError):
    """A manifest that ships with this repository is malformed."""


@dataclass(frozen=True)
class PinnedRepository:
    """A source checkout pinned to an exact revision.

    `revision` is what gets checked out. `commit`, when recorded, is what that
    revision resolved to at pinning time, which lets a provisioner notice a tag
    that moved under it.
    """

    component: str
    url: str
    revision: str
    commit: str | None = None
    destination: str | None = None

    @property
    def is_pinned(self) -> bool:
        """False for a placeholder that no one has pinned yet."""
        return bool(self.revision) and self.revision.upper() != "TBD"


def manifest_dir() -> Path | None:
    """Where the shipped manifests live, or None outside a checkout."""
    try:
        return detect_repo_root() / MANIFEST_DIR
    except InstallLocationError:
        return None


def _manifest_path(name: str) -> Path | None:
    directory = manifest_dir()
    if directory is None:
        return None
    path = directory / name
    return path if path.is_file() else None


@lru_cache(maxsize=1)
def artifact_catalog() -> dict[str, Artifact]:
    """Every pinned artifact, keyed by artifact id. Empty when unavailable."""
    path = _manifest_path(ARTIFACT_MANIFEST)
    if path is None:
        return {}
    try:
        return load_artifact_manifest(path)
    except ArtifactError as exc:
        raise CatalogError(str(exc)) from exc


def artifact_for(component_id: str) -> Artifact | None:
    """The pinned artifact for a component, or None when nothing is pinned."""
    wanted = normalize_component_id(component_id)
    matches = [a for a in artifact_catalog().values() if a.component == wanted]
    if not matches:
        return None
    if len(matches) > 1:
        listed = ", ".join(sorted(a.id for a in matches))
        raise CatalogError(f"more than one artifact pinned for {wanted}: {listed}")
    return matches[0]


def _repository_from(data: Any, component: str, source: Path | str) -> PinnedRepository:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: repositories.{component} must be a mapping")
    unknown = sorted(
        str(key) for key in data if key not in {"url", "revision", "commit", "destination"}
    )
    if unknown:
        raise CatalogError(
            f"{source}: repositories.{component}: unknown fields: {', '.join(unknown)}"
        )
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise CatalogError(f"{source}: repositories.{component}.url must be a non-empty string")
    if not url.lower().startswith("https://"):
        raise CatalogError(f"{source}: repositories.{component}.url must start with https://")
    revision = data.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise CatalogError(
            f"{source}: repositories.{component}.revision must be a non-empty string"
        )
    commit = data.get("commit")
    if commit is not None:
        if not isinstance(commit, str) or not _SHA1_RE.match(commit.strip().lower()):
            raise CatalogError(
                f"{source}: repositories.{component}.commit must be a full 40-character sha"
            )
        commit = commit.strip().lower()
    destination = data.get("destination")
    if destination is not None and (not isinstance(destination, str) or not destination.strip()):
        raise CatalogError(f"{source}: repositories.{component}.destination must be a string")
    return PinnedRepository(
        component=component,
        url=url.strip(),
        revision=revision.strip(),
        commit=commit,
        destination=destination.strip() if isinstance(destination, str) else None,
    )


@lru_cache(maxsize=1)
def repository_catalog() -> dict[str, PinnedRepository]:
    """Every pinned repository, keyed by canonical component id."""
    path = _manifest_path(REPOSITORY_MANIFEST)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"{path}: could not read repository manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: manifest root must be a mapping")
    entries = data.get("repositories")
    if not isinstance(entries, dict) or not entries:
        raise CatalogError(f"{path}: repositories must be a non-empty mapping")
    catalog: dict[str, PinnedRepository] = {}
    for raw_component, record in entries.items():
        component = normalize_component_id(str(raw_component))
        if component in catalog:
            raise CatalogError(f"{path}: duplicate repository for {component}")
        catalog[component] = _repository_from(record, component, path)
    return catalog


def repository_for(component_id: str) -> PinnedRepository | None:
    """The pinned repository for a component, or None when nothing is pinned."""
    entry = repository_catalog().get(normalize_component_id(component_id))
    return entry if entry is not None and entry.is_pinned else None


def clear_cache() -> None:
    """Forget the parsed manifests. For tests that point at another checkout."""
    artifact_catalog.cache_clear()
    repository_catalog.cache_clear()
