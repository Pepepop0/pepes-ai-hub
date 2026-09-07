"""Stable contracts between model acquisition and workflow/configuration.

This module deliberately contains no catalog loader, downloader, Ollama client,
or workflow implementation.  IC5 produces :class:`ResolvedModel` values; IC6
consumes them.  The two providers have different runtime locators and are not
forced into a fake common filesystem representation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias, runtime_checkable

from .artifacts import Artifact
from .layout import StackLayout

_MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9._-]*$")


class ModelContractError(ValueError):
    """A model declaration violates the shared IC5/IC6 contract."""


class ModelUnavailableError(RuntimeError):
    """A consumer requested a locator for a model that is not available."""


class ModelProvider(StrEnum):
    OLLAMA = "ollama"
    COMFYUI = "comfyui"


class ModelKind(StrEnum):
    CHAT = "chat"
    DIFFUSION = "diffusion"
    TEXT_ENCODER = "text-encoder"
    VAE = "vae"
    AUXILIARY = "auxiliary"


class ModelPlacement(StrEnum):
    """ComfyUI directory semantics, not absolute installation paths."""

    CHECKPOINTS = "checkpoints"
    DIFFUSION_MODELS = "diffusion_models"
    TEXT_ENCODERS = "text_encoders"
    VAE = "vae"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True, order=True)
class ModelId:
    """Canonical cross-layer identity: ``<provider>/<stable-name>``.

    Provider is part of identity because the same friendly name in Ollama and
    ComfyUI denotes different lifecycle and runtime semantics. Kind is metadata
    and is intentionally not part of identity.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ModelContractError("model id must be a string")
        if self.value != self.value.strip() or self.value.count("/") != 1:
            raise ModelContractError("model id must use canonical '<provider>/<stable-name>' form")
        raw_provider, name = self.value.split("/", 1)
        try:
            ModelProvider(raw_provider)
        except ValueError as exc:
            supported = ", ".join(provider.value for provider in ModelProvider)
            raise ModelContractError(
                f"unsupported model provider {raw_provider!r}: {supported}"
            ) from exc
        if not _MODEL_NAME_RE.fullmatch(name):
            raise ModelContractError(
                "model name must start with a lowercase letter and use only "
                "lowercase letters, digits, '.', '_' or '-'"
            )

    @classmethod
    def from_parts(cls, provider: ModelProvider, name: str) -> ModelId:
        return cls(f"{provider.value}/{name}")

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider(self.value.split("/", 1)[0])

    @property
    def name(self) -> str:
        return self.value.split("/", 1)[1]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OllamaModelSource:
    """What Ollama must pull and prove through its own model store."""

    reference: str
    digest: str | None = None

    def __post_init__(self) -> None:
        if not self.reference or self.reference != self.reference.strip():
            raise ModelContractError("Ollama reference must be a non-empty trimmed string")
        if self.digest is not None and (not self.digest or self.digest != self.digest.strip()):
            raise ModelContractError("Ollama digest must be a non-empty trimmed string")

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.OLLAMA


@dataclass(frozen=True)
class FileModelSource:
    """Validated bytes plus the semantic ComfyUI directory they belong in."""

    artifact: Artifact
    placement: ModelPlacement

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.COMFYUI


ModelSource: TypeAlias = OllamaModelSource | FileModelSource


@dataclass(frozen=True)
class ModelSpec:
    """Declarative catalog record. Loading it performs no I/O or mutation."""

    id: ModelId
    kind: ModelKind
    source: ModelSource

    def __post_init__(self) -> None:
        if self.id.provider is not self.source.provider:
            raise ModelContractError(
                f"model id provider {self.id.provider.value!r} does not match "
                f"source provider {self.source.provider.value!r}"
            )

    @property
    def provider(self) -> ModelProvider:
        return self.id.provider

    @property
    def revision(self) -> str | None:
        if isinstance(self.source, OllamaModelSource):
            return self.source.digest
        return self.source.artifact.version


class ModelState(StrEnum):
    NOT_INSTALLED = "not-installed"
    AVAILABLE = "available"
    INVALID = "invalid"
    EXTERNAL = "external"


@dataclass(frozen=True)
class OllamaModelLocation:
    """Provider reference consumed by Ollama; deliberately has no path."""

    reference: str

    def __post_init__(self) -> None:
        if not self.reference or self.reference != self.reference.strip():
            raise ModelContractError("Ollama location must contain a trimmed reference")

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.OLLAMA


@dataclass(frozen=True)
class FileModelLocation:
    """Absolute managed file and the name ComfyUI nodes must use."""

    path: Path
    runtime_name: str
    placement: ModelPlacement

    def __post_init__(self) -> None:
        path = Path(self.path)
        object.__setattr__(self, "path", path)
        if not path.is_absolute():
            raise ModelContractError("resolved file model path must be absolute")
        if not self.runtime_name or Path(self.runtime_name).name != self.runtime_name:
            raise ModelContractError("runtime_name must be a bare file name")
        if path.name != self.runtime_name:
            raise ModelContractError("runtime_name must match the resolved file name")

    @property
    def provider(self) -> ModelProvider:
        return ModelProvider.COMFYUI


ModelLocation: TypeAlias = OllamaModelLocation | FileModelLocation


@dataclass(frozen=True)
class ResolvedModel:
    """The only model value IC6 may consume.

    ``AVAILABLE`` means IC5 verified the provider reference or file integrity.
    Every other state withholds the runtime locator, so invalid, missing, and
    external models cannot accidentally become workflow inputs.
    """

    id: ModelId
    kind: ModelKind
    state: ModelState
    location: ModelLocation | None = None
    revision: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state is ModelState.AVAILABLE:
            if self.location is None:
                raise ModelContractError("an available model must have a runtime location")
            if self.id.provider is not self.location.provider:
                raise ModelContractError("resolved location provider does not match model id")
        elif self.location is not None:
            raise ModelContractError(
                f"a {self.state.value} model must not expose a runtime location"
            )

    @property
    def available(self) -> bool:
        return self.state is ModelState.AVAILABLE

    @property
    def provider(self) -> ModelProvider:
        return self.id.provider

    def require_location(self) -> ModelLocation:
        if not self.available or self.location is None:
            raise ModelUnavailableError(
                f"model {self.id} is {self.state.value} and cannot be consumed"
            )
        return self.location


@dataclass(frozen=True)
class ModelRequirement:
    """One active profile role bound to one canonical model identity."""

    role: str
    model_id: ModelId

    def __post_init__(self) -> None:
        if not _ROLE_RE.fullmatch(self.role):
            raise ModelContractError(f"invalid model role {self.role!r}")


@dataclass(frozen=True)
class ManagedModelPaths:
    """The single owner of managed file-model placement.

    Files land directly in the canonical tree. IC5 creates them there; IC6 is
    given resolved paths/runtime names and never recreates this mapping.
    """

    root: Path

    def __post_init__(self) -> None:
        root = Path(self.root)
        object.__setattr__(self, "root", root)
        if not root.is_absolute():
            raise ModelContractError("managed model root must be absolute")

    @classmethod
    def from_layout(cls, layout: StackLayout) -> ManagedModelPaths:
        return cls(layout.models)

    def provider_root(self, provider: ModelProvider) -> Path:
        """The isolated root owned by one model provider."""
        return self.root / provider.value

    def destination_for(self, spec: ModelSpec) -> Path:
        if not isinstance(spec.source, FileModelSource):
            raise ModelContractError("Ollama models have no managed file destination")
        return (
            self.provider_root(spec.provider)
            / spec.source.placement.value
            / spec.source.artifact.filename
        )


@runtime_checkable
class ModelResolver(Protocol):
    """IC5 implementation surface visible to IC6."""

    def resolve(self, model_id: ModelId) -> ResolvedModel: ...
