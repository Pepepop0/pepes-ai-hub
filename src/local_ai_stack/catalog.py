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
  detectable instead of silently followed;
- a :class:`PinnedPackage` for a published Python package, pinned to an exact
  version plus the interpreter range upstream supports (Open WebUI).

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
PACKAGE_MANIFEST = "packages.yaml"
MANIFEST_DIR = "manifests"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class CatalogError(ValueError):
    """A manifest that ships with this repository is malformed."""


@dataclass(frozen=True)
class PinnedDependencies:
    """What to install into a checkout's environment, decided explicitly.

    A repository's own requirements file may leave packages unversioned — as
    ComfyUI does for torch — so the ones that matter are pinned here. Wheels
    that do not come from PyPI, such as a CUDA build of torch, need the index
    that serves them named alongside.

    `verify_distribution` names a package whose presence and version prove the
    environment really got what was pinned, which is what makes verify() more
    than a file-existence check.
    """

    requirements: tuple[str, ...] = ()
    index_url: str | None = None
    extra_index_url: str | None = None
    use_repository_requirements: bool = False
    verify_distribution: str | None = None
    verify_version: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.requirements and not self.use_repository_requirements


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
    dependencies: PinnedDependencies | None = None
    python_minimum: tuple[int, int] = (3, 11)
    python_below: tuple[int, int] = (3, 13)

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
        str(key)
        for key in data
        if key not in {"url", "revision", "commit", "destination", "dependencies", "python"}
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

    python = data.get("python") or {}
    if not isinstance(python, dict):
        raise CatalogError(f"{source}: repositories.{component}.python must be a mapping")
    minimum = _version_pair(
        python.get("minimum", "3.11"), f"repositories.{component}.python.minimum", source
    )
    below = _version_pair(
        python.get("below", "3.13"), f"repositories.{component}.python.below", source
    )
    if below <= minimum:
        raise CatalogError(f"{source}: repositories.{component}.python.below must be above minimum")

    return PinnedRepository(
        component=component,
        url=url.strip(),
        revision=revision.strip(),
        commit=commit,
        destination=destination.strip() if isinstance(destination, str) else None,
        dependencies=_dependencies_from(data.get("dependencies"), component, source),
        python_minimum=minimum,
        python_below=below,
    )


def _dependencies_from(data: Any, component: str, source: Path | str) -> PinnedDependencies | None:
    """Parse the explicit dependency decision, rejecting floating versions."""
    if data is None:
        return None
    field = f"repositories.{component}.dependencies"
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: {field} must be a mapping")
    allowed = {
        "requirements",
        "index_url",
        "extra_index_url",
        "use_repository_requirements",
        "verify_distribution",
        "verify_version",
    }
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise CatalogError(f"{source}: {field}: unknown fields: {', '.join(unknown)}")

    raw_requirements = data.get("requirements", [])
    if not isinstance(raw_requirements, list):
        raise CatalogError(f"{source}: {field}.requirements must be a list")
    requirements: list[str] = []
    for index, entry in enumerate(raw_requirements):
        if not isinstance(entry, str) or not entry.strip():
            raise CatalogError(f"{source}: {field}.requirements[{index}] must be a string")
        requirement = entry.strip()
        if "==" not in requirement:
            raise CatalogError(
                f"{source}: {field}.requirements[{index}] must pin an exact version "
                f"with '==', got {requirement!r}"
            )
        requirements.append(requirement)

    urls: dict[str, str | None] = {}
    for key in ("index_url", "extra_index_url"):
        value = data.get(key)
        if value is None:
            urls[key] = None
            continue
        if not isinstance(value, str) or not value.lower().startswith("https://"):
            raise CatalogError(f"{source}: {field}.{key} must be an https URL")
        urls[key] = value.strip()

    use_repository = data.get("use_repository_requirements", False)
    if not isinstance(use_repository, bool):
        raise CatalogError(f"{source}: {field}.use_repository_requirements must be a boolean")

    verify_distribution = data.get("verify_distribution")
    if verify_distribution is not None and (
        not isinstance(verify_distribution, str) or not verify_distribution.strip()
    ):
        raise CatalogError(f"{source}: {field}.verify_distribution must be a string")
    verify_version = data.get("verify_version")
    if verify_version is not None and (
        not isinstance(verify_version, str) or not verify_version.strip()
    ):
        raise CatalogError(f"{source}: {field}.verify_version must be a string")

    return PinnedDependencies(
        requirements=tuple(requirements),
        index_url=urls["index_url"],
        extra_index_url=urls["extra_index_url"],
        use_repository_requirements=use_repository,
        verify_distribution=(
            verify_distribution.strip() if isinstance(verify_distribution, str) else None
        ),
        verify_version=verify_version.strip() if isinstance(verify_version, str) else None,
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


@dataclass(frozen=True)
class PinnedPackage:
    """A published Python package pinned to an exact version."""

    component: str
    distribution: str
    version: str
    python_minimum: tuple[int, int] = (3, 11)
    python_below: tuple[int, int] = (3, 13)

    @property
    def requirement(self) -> str:
        """The exact requirement string handed to pip."""
        return f"{self.distribution}=={self.version}"


def _version_pair(raw: Any, field: str, source: Path | str) -> tuple[int, int]:
    if not isinstance(raw, str):
        raise CatalogError(f"{source}: {field} must be a string like '3.11'")
    parts = raw.strip().split(".")
    if len(parts) != 2:
        raise CatalogError(f"{source}: {field} must look like '3.11'")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise CatalogError(f"{source}: {field} must be numeric, got {raw!r}") from exc


def _package_from(data: Any, component: str, source: Path | str) -> PinnedPackage:
    if not isinstance(data, dict):
        raise CatalogError(f"{source}: packages.{component} must be a mapping")
    unknown = sorted(str(key) for key in data if key not in {"distribution", "version", "python"})
    if unknown:
        raise CatalogError(f"{source}: packages.{component}: unknown fields: {', '.join(unknown)}")
    distribution = data.get("distribution")
    if not isinstance(distribution, str) or not distribution.strip():
        raise CatalogError(
            f"{source}: packages.{component}.distribution must be a non-empty string"
        )
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise CatalogError(f"{source}: packages.{component}.version must be a non-empty string")
    if version.strip().lower() in {"latest", "*"}:
        raise CatalogError(f"{source}: packages.{component}.version must be exact, not {version!r}")
    python = data.get("python") or {}
    if not isinstance(python, dict):
        raise CatalogError(f"{source}: packages.{component}.python must be a mapping")
    minimum = _version_pair(
        python.get("minimum", "3.11"), f"packages.{component}.python.minimum", source
    )
    below = _version_pair(python.get("below", "3.13"), f"packages.{component}.python.below", source)
    if below <= minimum:
        raise CatalogError(f"{source}: packages.{component}.python.below must be above minimum")
    return PinnedPackage(
        component=component,
        distribution=distribution.strip(),
        version=version.strip(),
        python_minimum=minimum,
        python_below=below,
    )


@lru_cache(maxsize=1)
def package_catalog() -> dict[str, PinnedPackage]:
    """Every pinned Python package, keyed by canonical component id."""
    path = _manifest_path(PACKAGE_MANIFEST)
    if path is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"{path}: could not read package manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: manifest root must be a mapping")
    entries = data.get("packages")
    if not isinstance(entries, dict) or not entries:
        raise CatalogError(f"{path}: packages must be a non-empty mapping")
    catalog: dict[str, PinnedPackage] = {}
    for raw_component, record in entries.items():
        component = normalize_component_id(str(raw_component))
        if component in catalog:
            raise CatalogError(f"{path}: duplicate package for {component}")
        catalog[component] = _package_from(record, component, path)
    return catalog


def package_for(component_id: str) -> PinnedPackage | None:
    """The pinned package for a component, or None when nothing is pinned."""
    return package_catalog().get(normalize_component_id(component_id))


def clear_cache() -> None:
    """Forget the parsed manifests. For tests that point at another checkout."""
    artifact_catalog.cache_clear()
    repository_catalog.cache_clear()
    package_catalog.cache_clear()
