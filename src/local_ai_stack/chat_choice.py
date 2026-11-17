"""Which chat model an installation uses, when it is not its profile's default.

A profile names a primary chat model and a few alternatives the catalog pins.
An installation may use one of those instead, or a model the person imported
from Ollama or a Hugging Face GGUF repository. That choice belongs to the
installation, so it lives in its own state folder::

    <root>/state/chat-model.yaml
    schema_version: 1
    model: ollama/qwen3_5_9b   # absent: the profile's primary
    imported:
      - reference: hf.co/someone/some-model-GGUF:Q4_K_M

Imported models are not pinned by digest: the person chose them by name, and
Ollama resolves the name. They are catalog entries for this installation only
(``ollama/user_<slug>``), never written to the shipped manifest.

:func:`effective_profile` is the one place a choice changes a profile: the
chat role points at the chosen model, and the model's own parameters join the
profile's. Everything downstream -- acquisition, configuration, the Open WebUI
record -- sees an ordinary profile and needs no knowledge of choices.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from .model_catalog import ModelCatalog
from .models import (
    ModelContractError,
    ModelId,
    ModelKind,
    ModelRequirement,
    ModelSpec,
    OllamaModelSource,
)
from .profiles import CHAT_PRIMARY_ROLE, Profile

CHOICE_FILENAME = "chat-model.yaml"
CHOICE_SCHEMA_VERSION = 1
IMPORTED_PREFIX = "user_"

# name:tag, namespace/name:tag, or hf.co/user/repo:quant. A tag is required so
# the same import keeps meaning the same model.
_REFERENCE = re.compile(
    r"^(?:hf\.co/[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*"
    r"|(?:[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)"
    r":[A-Za-z0-9][\w.-]*$"
)
# Prefixes people paste with the name: commands and the pages they copied from.
_PASTED_PREFIXES = (
    (re.compile(r"^ollama\s+(?:run|pull)\s+", re.IGNORECASE), ""),
    (re.compile(r"^https?://(?:www\.)?ollama\.com/(?:library/)?", re.IGNORECASE), ""),
    (re.compile(r"^https?://(?:www\.)?huggingface\.co/", re.IGNORECASE), "hf.co/"),
)


class ChatChoiceError(ValueError):
    """A chat choice or import that cannot be honoured. Safe to show."""


@dataclass(frozen=True)
class ImportedModel:
    reference: str

    @property
    def id(self) -> ModelId:
        slug = re.sub(r"[^a-z0-9]+", "_", self.reference.lower()).strip("_")
        return ModelId(f"ollama/{IMPORTED_PREFIX}{slug}")

    def spec(self) -> ModelSpec:
        return ModelSpec(self.id, ModelKind.CHAT, OllamaModelSource(self.reference))


@dataclass(frozen=True)
class ChatChoice:
    model: ModelId | None = None
    imported: tuple[ImportedModel, ...] = ()

    def imported_spec(self, model_id: ModelId) -> ModelSpec | None:
        return next((item.spec() for item in self.imported if item.id == model_id), None)

    def with_imported(self, item: ImportedModel) -> ChatChoice:
        if any(existing.id == item.id for existing in self.imported):
            return self
        return replace(self, imported=(*self.imported, item))

    def without_imported(self, model_id: ModelId) -> ChatChoice:
        return replace(self, imported=tuple(item for item in self.imported if item.id != model_id))


def choice_path(state_dir: Path) -> Path:
    return Path(state_dir) / CHOICE_FILENAME


def parse_reference(text: str) -> str:
    """The Ollama reference a person pasted, normalized; refuses what Ollama cannot pull."""
    reference = (text or "").strip()
    for pattern, replacement in _PASTED_PREFIXES:
        reference = pattern.sub(replacement, reference)
    reference = reference.strip().strip("/")
    if ":" not in reference.rsplit("/", 1)[-1]:
        raise ChatChoiceError(
            "Inclua a tag do modelo, por exemplo qwen3:8b ou hf.co/usuario/repositorio-GGUF:Q4_K_M."
        )
    if reference.endswith(":latest"):
        raise ChatChoiceError("Use uma tag específica em vez de :latest, para o modelo não mudar.")
    if not _REFERENCE.fullmatch(reference):
        raise ChatChoiceError(
            "Use o nome do Ollama (usuario/modelo:tag) ou um GGUF do Hugging Face "
            "(hf.co/usuario/repositorio:Q4_K_M)."
        )
    try:
        ImportedModel(reference).spec()
    except ModelContractError as exc:
        raise ChatChoiceError(str(exc)) from exc
    return reference


def read_choice(path: Path) -> ChatChoice:
    """The saved choice; the profile default when there is none or it cannot be read."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return ChatChoice()
    if not isinstance(data, dict) or data.get("schema_version") != CHOICE_SCHEMA_VERSION:
        return ChatChoice()
    imported = []
    for entry in data.get("imported") or []:
        reference = entry.get("reference") if isinstance(entry, dict) else None
        try:
            imported.append(ImportedModel(parse_reference(reference)))
        except (ChatChoiceError, TypeError):
            continue
    model = None
    if isinstance(data.get("model"), str):
        try:
            model = ModelId(data["model"])
        except ModelContractError:
            model = None
    return ChatChoice(model, tuple(imported))


def write_choice(path: Path, choice: ChatChoice) -> None:
    """Atomic; a choice equal to the profile default with no imports removes the file."""
    from .installations import _write

    path = Path(path)
    if choice == ChatChoice():
        path.unlink(missing_ok=True)
        return
    data: dict = {"schema_version": CHOICE_SCHEMA_VERSION}
    if choice.model is not None:
        data["model"] = str(choice.model)
    if choice.imported:
        data["imported"] = [{"reference": item.reference} for item in choice.imported]
    _write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def chat_options(profile: Profile) -> tuple[ModelId, ...]:
    """The chat models a profile offers: its primary, then its alternatives, deduplicated."""
    options: list[ModelId] = []
    primary = profile.model_for(CHAT_PRIMARY_ROLE)
    if primary is not None:
        options.append(primary)
    for value in profile.models.get("chat", {}).get("alternatives", []) or []:
        model_id = ModelId(value)
        if model_id not in options:
            options.append(model_id)
    return tuple(options)


def allowed(profile: Profile, choice: ChatChoice, model_id: ModelId) -> bool:
    return model_id in chat_options(profile) or choice.imported_spec(model_id) is not None


def effective_profile(profile: Profile, choice: ChatChoice, catalog: ModelCatalog) -> Profile:
    """The profile with the installation's chat model, or the profile itself.

    A choice the profile does not offer, or one the catalog cannot resolve, is
    ignored rather than half-applied: the installation keeps working on its
    default. The model's own parameters go first, so a profile's explicit
    parameter always wins.
    """
    model_id = choice.model
    if model_id is None or model_id == profile.model_for(CHAT_PRIMARY_ROLE):
        return profile
    if not allowed(profile, choice, model_id) or catalog.get(model_id) is None:
        return profile
    spec = catalog.get(model_id)
    chat = dict(profile.models.get("chat", {}))
    chat["primary"] = str(model_id)
    params = {**dict(spec.params), **(chat.get("params") or {})}
    if params:
        chat["params"] = params
    requirements = tuple(
        ModelRequirement(CHAT_PRIMARY_ROLE, model_id) if item.role == CHAT_PRIMARY_ROLE else item
        for item in profile.model_requirements
    )
    return replace(
        profile, models={**profile.models, "chat": chat}, model_requirements=requirements
    )


def with_imports(catalog: ModelCatalog, choice: ChatChoice) -> ModelCatalog:
    """The catalog this installation resolves against: shipped entries plus its imports."""
    if not choice.imported:
        return catalog
    specs = dict(catalog.specs)
    for item in choice.imported:
        specs.setdefault(item.id, item.spec())
    return replace(catalog, specs=specs)


__all__ = [
    "CHOICE_FILENAME",
    "ChatChoice",
    "ChatChoiceError",
    "ImportedModel",
    "allowed",
    "chat_options",
    "choice_path",
    "effective_profile",
    "parse_reference",
    "read_choice",
    "with_imports",
    "write_choice",
]
