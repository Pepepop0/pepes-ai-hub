"""Canonical component identifiers.

One id per component, used everywhere: stack definition, profile services,
plan steps, provisioners, runtime service specs, process specs and the
integration seam. A plan step target and a runtime service id are the same
string, so the two layers join directly instead of through a translation table.

The canonical form is hyphenated, matching what upstream calls itself
(`open-webui`), what the layout writes on disk (`apps/open-webui`) and what the
runtime and the interface already used. Python attribute names cannot contain a
hyphen, so `ComponentId.attr` derives the underscored form where an identifier
is needed — that derivation is the only place the two spellings meet.

`LEGACY_IDS` exists so a profile written before this rule still loads. It is
deliberately the single alias table in the codebase: normalize on the way in,
and everything downstream sees canonical ids only.
"""

from __future__ import annotations

from enum import StrEnum


class ComponentId(StrEnum):
    OLLAMA = "ollama"
    OPEN_WEBUI = "open-webui"
    COMFYUI = "comfyui"

    @property
    def attr(self) -> str:
        """The identifier-safe form, for attribute names like `open_webui_app`."""
        return self.value.replace("-", "_")

    @property
    def dir_name(self) -> str:
        """The directory name on disk, which is the canonical id itself."""
        return self.value


CANONICAL_IDS: tuple[str, ...] = tuple(str(member) for member in ComponentId)

# Spellings accepted on input and rewritten to canonical. The only alias table.
LEGACY_IDS: dict[str, str] = {
    "open_webui": ComponentId.OPEN_WEBUI,
    "openwebui": ComponentId.OPEN_WEBUI,
    "comfy_ui": ComponentId.COMFYUI,
}


def normalize_component_id(raw: str) -> str:
    """Canonicalize an id read from configuration.

    Unknown ids pass through unchanged: this normalizes spelling, it does not
    decide which components exist. That is the stack definition's job.
    """
    if not isinstance(raw, str):
        return raw
    candidate = raw.strip()
    return LEGACY_IDS.get(candidate, candidate)


def is_canonical(raw: str) -> bool:
    return raw in CANONICAL_IDS
