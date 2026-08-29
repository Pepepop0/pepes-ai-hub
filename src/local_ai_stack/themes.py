"""Theme backend: named colour roles, loaded from YAML.

A theme is a small set of semantic roles ("what is an error coloured like?"),
never a set of widget rules. Nothing here imports Textual or emits CSS: values
are plain hex strings, and a future TUI adapter maps roles onto whatever the
toolkit wants. That keeps themes usable by the Doctor, by tests and by any
other renderer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .assets import default_themes_directory

THEME_SCHEMA_VERSION = 1
THEME_SUFFIX = ".yaml"
HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ThemeRole(str, Enum):
    """Semantic colour slots every theme must fill."""

    BACKGROUND = "background"
    SURFACE = "surface"
    BORDER = "border"
    TEXT = "text"
    MUTED = "muted"
    ACCENT = "accent"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"


class ThemeError(ValueError):
    """A theme file is missing, malformed or incomplete."""


def _error(source: Path | str, field: str, message: str) -> ThemeError:
    location = f"{source}: {field}" if field else str(source)
    return ThemeError(f"{location}: {message}")


@dataclass(frozen=True)
class ThemeDefinition:
    schema_version: int
    id: str
    display_name: str
    description: str
    dark: bool
    colors: Mapping[ThemeRole, str]
    source: Path | None = None

    def color(self, role: ThemeRole) -> str:
        return self.colors[role]

    def as_tokens(self) -> dict[str, str]:
        """Role name -> colour, for whatever styling layer consumes it."""
        return {role.value: value for role, value in sorted(self.colors.items())}


def _string(value: Any, source: Path | str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(source, field, "must be a non-empty string")
    return value


def validate_theme_data(data: Any, source: Path | str = "<theme>") -> ThemeDefinition:
    if not isinstance(data, dict):
        raise _error(source, "theme root", "must be a mapping")
    missing = [key for key in ("schema_version", "id", "display_name", "colors") if key not in data]
    if missing:
        raise _error(source, "theme root", f"missing required fields: {', '.join(missing)}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise _error(source, "schema_version", "must be an integer")
    if schema_version != THEME_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {schema_version}; expected {THEME_SCHEMA_VERSION}",
        )

    dark = data.get("dark", True)
    if not isinstance(dark, bool):
        raise _error(source, "dark", "must be a boolean")

    colors_data = data["colors"]
    if not isinstance(colors_data, dict):
        raise _error(source, "colors", "must be a mapping")
    known = {role.value for role in ThemeRole}
    unknown = sorted(str(key) for key in colors_data if str(key) not in known)
    if unknown:
        raise _error(source, "colors", f"unknown roles: {', '.join(unknown)}")
    missing_roles = sorted(known - {str(key) for key in colors_data})
    if missing_roles:
        raise _error(source, "colors", f"missing roles: {', '.join(missing_roles)}")

    colors: dict[ThemeRole, str] = {}
    for role in ThemeRole:
        value = _string(colors_data[role.value], source, f"colors.{role.value}")
        if not HEX_COLOR.match(value):
            raise _error(source, f"colors.{role.value}", "must be a hex colour like '#1a2b3c'")
        colors[role] = value.lower()

    return ThemeDefinition(
        schema_version,
        _string(data["id"], source, "id"),
        _string(data["display_name"], source, "display_name"),
        str(data.get("description", "")).strip(),
        dark,
        colors,
        Path(source) if isinstance(source, Path) else None,
    )


def load_theme(path: Path) -> ThemeDefinition:
    if not path.is_file():
        raise _error(path, "", "theme file not found")
    theme = validate_theme_data(yaml.safe_load(path.read_text(encoding="utf-8")), path)
    if theme.id != path.stem:
        raise _error(path, "id", f"must match the file name {path.stem!r}")
    return theme


def load_themes(directory: Path | None = None) -> tuple[ThemeDefinition, ...]:
    root = default_themes_directory() if directory is None else directory
    if not root.is_dir():
        raise _error(root, "", "theme directory not found")
    return tuple(load_theme(path) for path in sorted(root.glob(f"*{THEME_SUFFIX}")))


def available_themes(directory: Path | None = None) -> tuple[str, ...]:
    root = default_themes_directory() if directory is None else directory
    if not root.is_dir():
        return ()
    return tuple(sorted(path.stem for path in root.glob(f"*{THEME_SUFFIX}")))


def load_theme_by_id(theme_id: str, directory: Path | None = None) -> ThemeDefinition:
    root = default_themes_directory() if directory is None else directory
    candidate = root / f"{theme_id}{THEME_SUFFIX}"
    if not candidate.is_file():
        known = ", ".join(available_themes(root)) or "none"
        raise _error(root, theme_id, f"unknown theme (available: {known})")
    return load_theme(candidate)
