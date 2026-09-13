"""The configuration of an installed hub.

A profile definition and an installed configuration are different documents. A
profile in profiles/ describes what Pepe or Capella *is*, and ships with the
repository. The installed configuration records what this machine actually
chose: which profile, which stack, where it lives, and how the interface should
behave. It is written by the manager, not by hand, and it is versioned so a
later schema can migrate it.

    schema_version: 1
    profile:      { id: pepe }        # which profile definition is selected
    stack:        { id: standard }    # which stack is installed
    installation: { mode: portable, root: null }
    general:      { ui: { ... } }     # manager behaviour, profile-independent

General settings are profile-independent by design. `theme` and `pet` default
to None, meaning "not set here": a profile may supply a default later and a
value set here overrides it. Resolving that precedence needs the profile-side
defaults to exist first, so this module only models the override slot and does
not reach into profiles.

This module serializes and validates. It does not read or write files: nothing
is persisted automatically at this stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .installation import InstallLocation, InstallLocationError, InstallLocationMode
from .layout import StackLayout
from .stacks import DEFAULT_STACK_ID

CONFIG_SCHEMA_VERSION = 1
CONFIG_FILENAME = "config.yaml"
RECENT_CUSTOM_ROOTS_LIMIT = 5


class ConfigError(ValueError):
    """The installed configuration is malformed or unsupported."""


def _mapping(value: Any, source: str, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{source}: {field_name} must be a mapping")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], source: str, field_name: str) -> None:
    unknown = sorted((str(key) for key in data if key not in allowed), key=str.casefold)
    if unknown:
        raise ConfigError(f"{source}: {field_name}: unknown fields: {', '.join(unknown)}")


def _boolean(value: Any, source: str, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{source}: {field_name} must be a boolean")
    return value


def _slug(value: Any, source: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{source}: {field_name} must be a non-empty string")
    slug = value.strip()
    if not slug[0].isalpha() or not all(c.islower() or c.isdigit() or c in "_-" for c in slug):
        raise ConfigError(
            f"{source}: {field_name} must use lowercase letters, digits, '_' or '-' "
            "and start with a letter"
        )
    return slug


def _optional_slug(value: Any, source: str, field_name: str) -> str | None:
    return None if value is None else _slug(value, source, field_name)


def _nested_id(data: dict[str, Any], source: str, field_name: str) -> str:
    section = _mapping(data.get(field_name), source, field_name)
    _reject_unknown(section, {"id"}, source, field_name)
    if "id" not in section:
        raise ConfigError(f"{source}: {field_name}.id is required")
    return _slug(section["id"], source, f"{field_name}.id")


@dataclass(frozen=True)
class UiConfig:
    """Interface preferences.

    `theme` and `pet` are None when unset, which lets a profile-supplied default
    apply. A value here always wins over that default.
    """

    theme: str | None = None
    pet: str | None = None
    right_rail_visible: bool = True
    right_rail_expanded: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "pet": self.pet,
            "right_rail_visible": self.right_rail_visible,
            "right_rail_expanded": self.right_rail_expanded,
        }

    @classmethod
    def from_dict(cls, data: Any, source: str = "<config>") -> UiConfig:
        section = _mapping(data, source, "general.ui")
        _reject_unknown(
            section,
            {"theme", "pet", "right_rail_visible", "right_rail_expanded"},
            source,
            "general.ui",
        )
        defaults = cls()
        return cls(
            theme=_optional_slug(section.get("theme"), source, "general.ui.theme"),
            pet=_optional_slug(section.get("pet"), source, "general.ui.pet"),
            right_rail_visible=_boolean(
                section.get("right_rail_visible", defaults.right_rail_visible),
                source,
                "general.ui.right_rail_visible",
            ),
            right_rail_expanded=_boolean(
                section.get("right_rail_expanded", defaults.right_rail_expanded),
                source,
                "general.ui.right_rail_expanded",
            ),
        )


def _root_key(root: str) -> str:
    """Identity of a recent root: same folder regardless of case, slashes or a trailing separator.

    Symlinks and junctions are deliberately not resolved; two spellings of one
    folder only count as the same entry when the text says so.
    """
    return os.path.normcase(os.path.normpath(root))


def _recent_roots(roots: Any) -> tuple[str, ...]:
    """Newest first, deduplicated by :func:`_root_key`, never more than the limit."""
    kept: list[str] = []
    seen: set[str] = set()
    for root in roots:
        key = _root_key(root)
        if key in seen:
            continue
        seen.add(key)
        kept.append(os.path.normpath(root))
        if len(kept) == RECENT_CUSTOM_ROOTS_LIMIT:
            break
    return tuple(kept)


@dataclass(frozen=True)
class GeneralConfig:
    """Manager behaviour that does not depend on the selected profile.

    ``recent_custom_roots`` is the machine's short history of custom install
    folders, newest first. It is recorded when an installation is attempted,
    not when one succeeds: a root left half-installed by a failed attempt is
    exactly the one a person needs to find again.
    """

    ui: UiConfig = field(default_factory=UiConfig)
    recent_custom_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "recent_custom_roots", _recent_roots(self.recent_custom_roots))

    def remembering_custom_root(self, root: str | os.PathLike[str]) -> GeneralConfig:
        """A copy with ``root`` at the top of the history, moved there if already present."""
        return replace(self, recent_custom_roots=(os.fspath(root), *self.recent_custom_roots))

    def to_dict(self) -> dict[str, Any]:
        return {"ui": self.ui.to_dict(), "recent_custom_roots": list(self.recent_custom_roots)}

    @classmethod
    def from_dict(cls, data: Any, source: str = "<config>") -> GeneralConfig:
        section = _mapping(data, source, "general")
        _reject_unknown(section, {"ui", "recent_custom_roots"}, source, "general")
        raw_roots = section.get("recent_custom_roots", [])
        if not isinstance(raw_roots, list) or not all(
            isinstance(root, str) and root.strip() for root in raw_roots
        ):
            raise ConfigError(
                f"{source}: general.recent_custom_roots must be a list of non-empty strings"
            )
        return cls(
            ui=UiConfig.from_dict(section.get("ui", {}), source),
            recent_custom_roots=tuple(raw_roots),
        )


@dataclass(frozen=True)
class InstalledConfig:
    """What this machine installed: profile, stack, location and preferences."""

    profile_id: str
    stack_id: str = DEFAULT_STACK_ID
    installation: InstallLocation = field(default_factory=InstallLocation.default)
    general: GeneralConfig = field(default_factory=GeneralConfig)
    schema_version: int = CONFIG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _slug(self.profile_id, "<config>", "profile.id"))
        object.__setattr__(self, "stack_id", _slug(self.stack_id, "<config>", "stack.id"))

    def with_installation(self, installation: InstallLocation) -> InstalledConfig:
        """A copy pointing at another install location. The profile is unchanged.

        Location and profile are orthogonal: any profile is valid in any mode.
        """
        return replace(self, installation=installation)

    def with_general(self, general: GeneralConfig) -> InstalledConfig:
        return replace(self, general=general)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile": {"id": self.profile_id},
            "stack": {"id": self.stack_id},
            "installation": self.installation.to_dict(),
            "general": self.general.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any, source: str = "<config>") -> InstalledConfig:
        root = _mapping(data, source, "config root")
        _reject_unknown(
            root,
            {"schema_version", "profile", "stack", "installation", "general"},
            source,
            "config root",
        )
        for required in ("schema_version", "profile", "installation"):
            if required not in root:
                raise ConfigError(f"{source}: {required} is required")

        raw_version = root["schema_version"]
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise ConfigError(f"{source}: schema_version must be an integer")
        if raw_version != CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"{source}: unsupported schema_version {raw_version}; "
                f"expected {CONFIG_SCHEMA_VERSION}"
            )

        try:
            installation = InstallLocation.from_dict(root["installation"], source)
        except InstallLocationError as exc:
            raise ConfigError(str(exc)) from exc

        raw_general = root.get("general", {})
        general_section = _mapping(raw_general, source, "general")
        general = GeneralConfig.from_dict(general_section, source)
        if (
            "recent_custom_roots" not in general_section
            and installation.mode is InstallLocationMode.CUSTOM
            and installation.root is not None
        ):
            # Schema v1 predates the recent-roots field. Its selected CUSTOM
            # installation is already a proven install root, so expose it as
            # the first migrated history entry without writing during load.
            general = general.remembering_custom_root(installation.root)

        stack_id = _nested_id(root, source, "stack") if "stack" in root else DEFAULT_STACK_ID
        return cls(
            profile_id=_nested_id(root, source, "profile"),
            stack_id=stack_id,
            installation=installation,
            general=general,
            schema_version=raw_version,
        )


def dumps(config: InstalledConfig) -> str:
    """Serialize to YAML text. Writing it somewhere is the caller's decision."""
    return yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=True)


def loads(text: str, source: str = "<config>") -> InstalledConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: could not parse configuration: {exc}") from exc
    return InstalledConfig.from_dict(data, source)


def config_path(layout: StackLayout) -> Path:
    """Where the installed configuration belongs, once something writes it."""
    return layout.state / CONFIG_FILENAME
