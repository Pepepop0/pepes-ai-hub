"""The declarative model catalog.

Turns ``manifests/models.yaml`` into :class:`~local_ai_stack.models.ModelSpec`
records keyed by canonical :class:`~local_ai_stack.models.ModelId`. Loading is
pure: it parses and validates text, opens no socket and touches no destination.

Two rules keep the identity single:

- an id is canonical or it is an alias, never a second namespace. Aliases are
  resolved on the way in, so everything downstream — installer, resolver,
  profiles — only ever sees canonical ids;
- a managed file model without a SHA-256 is not a spec. It stays *declared*, so
  a profile may still name it, but it can never be downloaded from an
  unverified source. It resolves as not-installed with the reason attached.

The manifest owns pins and metadata. It does not own paths: where a file lands
is decided by ManagedModelPaths, and the catalog never computes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .artifacts import Artifact, ArtifactError
from .catalog import MANIFEST_DIR, manifest_dir
from .models import (
    FileModelSource,
    ModelContractError,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelProvider,
    ModelSpec,
    OllamaModelSource,
)

MODEL_MANIFEST = "models.yaml"
MODEL_SCHEMA_VERSION = 1

UNPINNED = "TBD"

_OLLAMA_FIELDS = {"kind", "reference", "digest", "aliases", "capabilities", "status"}
_COMFYUI_FIELDS = {
    "kind",
    "placement",
    "filename",
    "url",
    "sha256",
    "size_bytes",
    "version",
    "aliases",
    "status",
}


class ModelCatalogError(ValueError):
    """The shipped model manifest is malformed."""


def _error(source: Path | str, field_name: str, message: str) -> ModelCatalogError:
    return ModelCatalogError(f"{source}: {field_name}: {message}")


def _is_unpinned(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() == UNPINNED


def _string(value: Any, source: Path | str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(source, field_name, "must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class ModelCatalog:
    """Every model this checkout knows about, pinned or merely declared."""

    specs: dict[ModelId, ModelSpec] = field(default_factory=dict)
    unpinned: dict[ModelId, str] = field(default_factory=dict)
    aliases: dict[str, ModelId] = field(default_factory=dict)

    @property
    def declared_ids(self) -> frozenset[ModelId]:
        """Everything the manifest names, whether or not it can be installed."""
        return frozenset(self.specs) | frozenset(self.unpinned)

    def __contains__(self, model_id: ModelId) -> bool:
        return model_id in self.specs

    def get(self, model_id: ModelId) -> ModelSpec | None:
        return self.specs.get(model_id)

    def require(self, model_id: ModelId) -> ModelSpec:
        spec = self.specs.get(model_id)
        if spec is not None:
            return spec
        reason = self.unpinned.get(model_id)
        if reason is not None:
            raise ModelCatalogError(f"{model_id} is declared but not installable: {reason}")
        raise ModelCatalogError(f"unknown model {model_id}")

    def reason_for(self, model_id: ModelId) -> str | None:
        """Why a declared model has no spec, or None when it has one."""
        return self.unpinned.get(model_id)

    def is_declared(self, model_id: ModelId) -> bool:
        return model_id in self.specs or model_id in self.unpinned

    def normalize(self, raw: str) -> ModelId:
        """Canonicalize an id written by a human. Aliases resolve here only."""
        if not isinstance(raw, str):
            raise ModelCatalogError("model id must be a string")
        candidate = raw.strip()
        alias = self.aliases.get(candidate)
        if alias is not None:
            return alias
        try:
            return ModelId(candidate)
        except ModelContractError as exc:
            raise ModelCatalogError(str(exc)) from exc

    def for_provider(self, provider: ModelProvider) -> tuple[ModelSpec, ...]:
        return tuple(spec for spec in self.specs.values() if spec.provider is provider)


def _kind(value: Any, source: Path | str, field_name: str) -> ModelKind:
    raw = _string(value, source, field_name)
    try:
        return ModelKind(raw)
    except ValueError as exc:
        known = ", ".join(kind.value for kind in ModelKind)
        raise _error(source, field_name, f"unknown kind {raw!r}; expected one of {known}") from exc


def _placement(value: Any, source: Path | str, field_name: str) -> ModelPlacement:
    raw = _string(value, source, field_name)
    try:
        return ModelPlacement(raw)
    except ValueError as exc:
        known = ", ".join(item.value for item in ModelPlacement)
        raise _error(
            source, field_name, f"unknown placement {raw!r}; expected one of {known}"
        ) from exc


def _alias_list(value: Any, source: Path | str, field_name: str) -> list[Any]:
    """The alias declaration, as a list or not at all.

    A bare string is the trap this guards: it is iterable, so accepting one
    would silently register every character as its own alias. A mapping is
    iterable too, over its keys. Only a real list is a list of aliases, and an
    absent or null declaration means none.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise _error(
            source,
            f"{field_name}.aliases",
            f"must be a list of strings, got {type(value).__name__}"
            + (f"; write '- {value}' to declare a single alias" if isinstance(value, str) else ""),
        )
    return value


def _reject_unknown(
    record: dict[str, Any], allowed: set[str], source: Path | str, field_name: str
) -> None:
    unknown = sorted(str(key) for key in record if key not in allowed)
    if unknown:
        raise _error(source, field_name, f"unknown fields: {', '.join(unknown)}")


def _ollama_spec(
    model_id: ModelId, record: dict[str, Any], source: Path | str, field_name: str
) -> tuple[ModelSpec | None, str | None]:
    _reject_unknown(record, _OLLAMA_FIELDS, source, field_name)
    if "reference" not in record or _is_unpinned(record.get("reference")):
        return None, "no provider reference is pinned"
    reference = _string(record["reference"], source, f"{field_name}.reference")
    if reference.endswith(":latest") or ":" not in reference:
        raise _error(
            source,
            f"{field_name}.reference",
            f"must pin an explicit tag, got {reference!r}",
        )
    digest = record.get("digest")
    if digest is not None and not _is_unpinned(digest):
        digest = _string(digest, source, f"{field_name}.digest").lower()
        if not all(character in "0123456789abcdef" for character in digest):
            raise _error(source, f"{field_name}.digest", "must be hexadecimal")
    else:
        digest = None
    try:
        source_record = OllamaModelSource(reference=reference, digest=digest)
        return ModelSpec(
            id=model_id,
            kind=_kind(record.get("kind", "chat"), source, f"{field_name}.kind"),
            source=source_record,
        ), None
    except ModelContractError as exc:
        raise _error(source, field_name, str(exc)) from exc


def _comfyui_spec(
    model_id: ModelId, record: dict[str, Any], source: Path | str, field_name: str
) -> tuple[ModelSpec | None, str | None]:
    _reject_unknown(record, _COMFYUI_FIELDS, source, field_name)
    filename = _string(record.get("filename"), source, f"{field_name}.filename")

    # A managed file is only installable when the bytes can be proven. Without
    # a checksum the entry stays declared, never downloaded on trust.
    missing = [key for key in ("url", "sha256") if key not in record or _is_unpinned(record[key])]
    if missing:
        return None, f"no {' and no '.join(missing)} is pinned for {filename}"

    payload: dict[str, Any] = {
        "id": f"{model_id.provider.value}-{model_id.name}".replace("_", "-"),
        "url": _string(record["url"], source, f"{field_name}.url"),
        "filename": filename,
        "sha256": _string(record["sha256"], source, f"{field_name}.sha256"),
    }
    if record.get("size_bytes") is not None:
        payload["size_bytes"] = record["size_bytes"]
    if record.get("version") is not None and not _is_unpinned(record["version"]):
        payload["version"] = record["version"]

    try:
        artifact = Artifact.from_dict(payload, f"{source}:{field_name}")
    except ArtifactError as exc:
        raise _error(source, field_name, str(exc)) from exc

    try:
        return ModelSpec(
            id=model_id,
            kind=_kind(record.get("kind"), source, f"{field_name}.kind"),
            source=FileModelSource(
                artifact=artifact,
                placement=_placement(record.get("placement"), source, f"{field_name}.placement"),
            ),
        ), None
    except ModelContractError as exc:
        raise _error(source, field_name, str(exc)) from exc


_BUILDERS = {
    ModelProvider.OLLAMA: _ollama_spec,
    ModelProvider.COMFYUI: _comfyui_spec,
}


def validate_model_manifest(data: Any, source: Path | str = "<models>") -> ModelCatalog:
    """Validate a manifest document into a catalog. Performs no I/O."""
    if not isinstance(data, dict):
        raise _error(source, "manifest root", "must be a mapping")

    allowed = {"schema_version", *(provider.value for provider in ModelProvider)}
    _reject_unknown(data, allowed, source, "manifest root")

    raw_version = data.get("schema_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise _error(source, "schema_version", "must be an integer")
    if raw_version != MODEL_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {raw_version}; expected {MODEL_SCHEMA_VERSION}",
        )

    specs: dict[ModelId, ModelSpec] = {}
    unpinned: dict[ModelId, str] = {}
    aliases: dict[str, ModelId] = {}

    for provider in ModelProvider:
        section = data.get(provider.value)
        if section is None:
            continue
        if not isinstance(section, dict):
            raise _error(source, provider.value, "must be a mapping")

        for raw_name, record in section.items():
            field_name = f"{provider.value}.{raw_name}"
            if not isinstance(record, dict):
                raise _error(source, field_name, "must be a mapping")
            try:
                model_id = ModelId.from_parts(provider, str(raw_name))
            except ModelContractError as exc:
                raise _error(source, field_name, str(exc)) from exc
            if model_id in specs or model_id in unpinned:
                raise _error(source, field_name, f"duplicate model id {model_id}")

            spec, reason = _BUILDERS[provider](model_id, record, source, field_name)
            if spec is not None:
                specs[model_id] = spec
            else:
                unpinned[model_id] = reason or "not installable"

            for index, alias in enumerate(_alias_list(record.get("aliases"), source, field_name)):
                label = f"{field_name}.aliases[{index}]"
                text = _string(alias, source, label)
                if text in aliases and aliases[text] != model_id:
                    raise _error(source, label, f"alias {text!r} is already claimed")
                aliases[text] = model_id

    declared = set(specs) | set(unpinned)
    for alias, target in aliases.items():
        if any(alias == str(known) for known in declared):
            raise _error(source, "aliases", f"alias {alias!r} collides with a canonical id")
        if target not in declared:
            raise _error(source, "aliases", f"alias {alias!r} points at an unknown model")

    return ModelCatalog(specs=specs, unpinned=unpinned, aliases=aliases)


def load_model_catalog(path: Path) -> ModelCatalog:
    """Read and validate a model manifest file. Downloads nothing."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ModelCatalogError(f"{path}: could not read model manifest: {exc}") from exc
    return validate_model_manifest(data, path)


@lru_cache(maxsize=1)
def model_catalog() -> ModelCatalog:
    """The catalog that ships with this checkout. Empty when unavailable."""
    directory = manifest_dir()
    if directory is None:
        return ModelCatalog()
    path = directory / MODEL_MANIFEST
    if not path.is_file():
        return ModelCatalog()
    return load_model_catalog(path)


def clear_cache() -> None:
    """Forget the parsed manifest. For tests pointing at another checkout."""
    model_catalog.cache_clear()


__all__ = [
    "MANIFEST_DIR",
    "MODEL_MANIFEST",
    "MODEL_SCHEMA_VERSION",
    "ModelCatalog",
    "ModelCatalogError",
    "clear_cache",
    "load_model_catalog",
    "model_catalog",
    "validate_model_manifest",
]
