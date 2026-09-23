"""What the Hub knows about installations, across all of them.

One machine can hold several installed stacks -- one per folder -- and the Hub
manages one at a time. Three kinds of truth are kept apart:

* **global to the Hub** (this module, ``state/hub-selection.yaml``): which
  installation is active, which ones are known, when each was last used, and
  the interface preferences that do not belong to any of them;
* **per installation** (``<root>/state/config.yaml``, :mod:`config`): the
  profile, the stack and the mode that installation was made with;
* **derived, never stored**: whether a stack is running, healthy, whether its
  folder still exists, how much space is left. Those are observed when asked
  for, because a file cannot know them.

A known installation is remembered by its normalized path: the same folder
written with a different case, a trailing separator or the other separator is
the same entry. Nothing here deletes anything on disk; forgetting an
installation removes the Hub's shortcut to it and not one of its files.

The file this reads and writes is the one schema version 1 wrote, so an
existing installation keeps working: a v1 document is migrated on load -- its
selection becomes the active installation and the first known one -- and only
written back in the new shape when something is saved.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from .config import (
    DEFAULT_STACK_ID,
    ConfigError,
    GeneralConfig,
    InstalledConfig,
)
from .config import (
    dumps as dumps_config,
)
from .installation import InstallLocation, InstallLocationMode

STATE_SCHEMA_VERSION = 2
STATE_FILENAME = "hub-selection.yaml"

# What an installation's history keeps. Not a log: one line each, so the Hub
# can say what happened last without keeping a database of what ever happened.
INSTALL = "install"
REPAIR = "repair"
FAILURE = "failure"


def root_key(root: str | os.PathLike[str]) -> str:
    """Identity of an installation folder: case, separators and a trailing one do not count.

    Symlinks and junctions are deliberately not resolved -- two spellings are
    the same entry only when the text says so, which is the rule the recent
    roots history has always used.
    """
    return os.path.normcase(os.path.normpath(os.fspath(root)))


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class OperationRecord:
    """The last time something happened to an installation, in one line."""

    at: datetime
    result: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"at": self.at.isoformat()}
        if self.result:
            record["result"] = self.result
        if self.summary:
            record["summary"] = self.summary
        return record

    @classmethod
    def from_dict(cls, data: Any) -> OperationRecord | None:
        if not isinstance(data, Mapping):
            return None
        at = _moment(data.get("at"))
        if at is None:
            return None
        return cls(at, _text(data.get("result")) or "", _text(data.get("summary")) or "")


@dataclass(frozen=True)
class KnownInstallation:
    """One installation the Hub has seen, as the Hub can describe it without looking.

    Enough to render a row and to recover from a folder that moved: where it
    was, how it was installed, when it was last used and what it last did.
    Whether it exists right now is not here; that is observed.
    """

    root: Path
    mode: InstallLocationMode = InstallLocationMode.CUSTOM
    last_used_at: datetime | None = None
    last_known_profile: str | None = None
    last_known_stack: str | None = None
    last_install: OperationRecord | None = None
    last_repair: OperationRecord | None = None
    last_failure: OperationRecord | None = None

    @property
    def key(self) -> str:
        return root_key(self.root)

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"root": str(self.root), "mode": self.mode.value}
        if self.last_used_at is not None:
            record["last_used_at"] = self.last_used_at.isoformat()
        if self.last_known_profile:
            record["profile"] = self.last_known_profile
        if self.last_known_stack:
            record["stack"] = self.last_known_stack
        for name, value in (
            ("last_install", self.last_install),
            ("last_repair", self.last_repair),
            ("last_failure", self.last_failure),
        ):
            if value is not None:
                record[name] = value.to_dict()
        return record

    @classmethod
    def from_dict(cls, data: Any, source: str = "<state>") -> KnownInstallation | None:
        if not isinstance(data, Mapping):
            return None
        root = _text(data.get("root"))
        if root is None:
            return None
        raw_mode = _text(data.get("mode")) or InstallLocationMode.CUSTOM.value
        try:
            mode = InstallLocationMode(raw_mode)
        except ValueError:
            mode = InstallLocationMode.CUSTOM
        return cls(
            root=Path(os.path.normpath(root)),
            mode=mode,
            last_used_at=_moment(data.get("last_used_at")),
            last_known_profile=_text(data.get("profile")),
            last_known_stack=_text(data.get("stack")),
            last_install=OperationRecord.from_dict(data.get("last_install")),
            last_repair=OperationRecord.from_dict(data.get("last_repair")),
            last_failure=OperationRecord.from_dict(data.get("last_failure")),
        )


@dataclass(frozen=True)
class HubState:
    """Everything the Hub keeps for itself: the active installation and the known ones."""

    active: InstalledConfig | None = None
    known: tuple[KnownInstallation, ...] = ()
    general: GeneralConfig = field(default_factory=GeneralConfig)
    schema_version: int = STATE_SCHEMA_VERSION

    # --- reading ------------------------------------------------------------

    @property
    def active_root(self) -> Path | None:
        if self.active is None:
            return None
        return self.active.installation.root

    def entry_for(self, root: str | os.PathLike[str]) -> KnownInstallation | None:
        wanted = root_key(root)
        return next((item for item in self.known if item.key == wanted), None)

    def ordered(self) -> tuple[KnownInstallation, ...]:
        """Known installations, most recently used first; never used ones last.

        Two entries can carry the same instant -- the clock's resolution is
        coarser than two switches in a row -- so the one touched last wins, and
        the order never depends on how a dictionary happened to iterate.
        """
        epoch = datetime.fromtimestamp(0, UTC)
        ranked = sorted(
            enumerate(self.known),
            key=lambda pair: (pair[1].last_used_at or epoch, pair[0]),
            reverse=True,
        )
        return tuple(entry for _, entry in ranked)

    # --- writing ------------------------------------------------------------

    def remembering(
        self,
        root: str | os.PathLike[str],
        mode: InstallLocationMode,
        *,
        profile_id: str | None = None,
        stack_id: str | None = None,
        used: bool = False,
    ) -> HubState:
        """This folder is an installation the Hub knows. Does not make it active."""
        existing = self.entry_for(root)
        entry = KnownInstallation(
            root=Path(os.path.normpath(os.fspath(root))),
            mode=mode,
            last_used_at=now() if used else (existing.last_used_at if existing else None),
            last_known_profile=profile_id or (existing.last_known_profile if existing else None),
            last_known_stack=stack_id or (existing.last_known_stack if existing else None),
            last_install=existing.last_install if existing else None,
            last_repair=existing.last_repair if existing else None,
            last_failure=existing.last_failure if existing else None,
        )
        return replace(self, known=self._replacing(entry))

    def recording(
        self,
        root: str | os.PathLike[str],
        kind: str,
        *,
        result: str = "",
        summary: str = "",
    ) -> HubState:
        """What this installation last did: an install, a repair, or a failure."""
        entry = self.entry_for(root)
        if entry is None:
            return self
        record = OperationRecord(now(), result, summary)
        field_name = {INSTALL: "last_install", REPAIR: "last_repair", FAILURE: "last_failure"}[kind]
        return replace(self, known=self._replacing(replace(entry, **{field_name: record})))

    def activating(self, config: InstalledConfig) -> HubState:
        """This installation is the active one.

        "Used just now" means the selection actually moved: re-activating what
        is already active -- a second run of the same installation, a Hub that
        reopens on it -- changes nothing, so a run that does no work writes
        nothing.
        """
        root = config.installation.root
        # Interface preferences and the roots history are the Hub's, not an
        # installation's: a configuration prepared a moment ago carries a copy
        # of them, and that copy must never overwrite what has happened since.
        state = replace(self, active=config.with_general(self.general))
        if root is None:
            return state
        # The same folder written another way is the same installation, so the
        # comparison is by identity of the path, not by the text of it.
        moved = (
            self.active is None
            or self.active_root is None
            or root_key(self.active_root) != root_key(root)
            or self.active.profile_id != config.profile_id
            or self.active.stack_id != config.stack_id
            or self.entry_for(root) is None
        )
        return state.remembering(
            root,
            config.installation.mode,
            profile_id=config.profile_id,
            stack_id=config.stack_id,
            used=moved,
        )

    def reconciled(self, resolved_root: Path | None = None) -> HubState:
        """The installation in use is always one the Hub knows about.

        A state file can name an active installation and not list it -- written
        by an older version, or edited by hand -- and an installation nobody
        can find in the list is one nobody can switch back to, protect from
        being forgotten, or read a history from. ``resolved_root`` is for the
        modes that name no root of their own: the folder they resolved to on
        this machine is their identity here.
        """
        if self.active is None:
            return self
        root = self.active.installation.root or resolved_root
        if root is None or self.entry_for(root) is not None:
            return self
        return self.remembering(
            root,
            self.active.installation.mode,
            profile_id=self.active.profile_id,
            stack_id=self.active.stack_id,
        )

    def forgetting(self, root: str | os.PathLike[str]) -> HubState:
        """Remove the Hub's shortcut to this installation. Deletes nothing on disk.

        The active installation is never forgotten: a caller switches away
        first, which is a decision, not a side effect of tidying a list.
        """
        wanted = root_key(root)
        if self.active_root is not None and root_key(self.active_root) == wanted:
            return self
        return replace(self, known=tuple(item for item in self.known if item.key != wanted))

    def with_general(self, general: GeneralConfig) -> HubState:
        active = None if self.active is None else self.active.with_general(general)
        return replace(self, active=active, general=general)

    def _replacing(self, entry: KnownInstallation) -> tuple[KnownInstallation, ...]:
        others = tuple(item for item in self.known if item.key != entry.key)
        return (*others, entry)

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {"schema_version": STATE_SCHEMA_VERSION}
        if self.active is not None:
            document["profile"] = {"id": self.active.profile_id}
            document["stack"] = {"id": self.active.stack_id}
            document["installation"] = self.active.installation.to_dict()
        document["general"] = self.general.to_dict()
        document["known_installations"] = [item.to_dict() for item in self.ordered()]
        return document

    @classmethod
    def from_dict(cls, data: Any, source: str = "<state>") -> HubState:
        if not isinstance(data, Mapping):
            raise ConfigError(f"{source}: the Hub state must be a mapping")
        version = data.get("schema_version")
        if version == STATE_SCHEMA_VERSION:
            return cls._from_v2(dict(data), source)
        # Version 1 is a single installed configuration: the selection it holds
        # is the active installation and the first one the Hub knows about.
        config = InstalledConfig.from_dict(data, source)
        return cls(active=config, general=config.general).activating(config)

    @classmethod
    def _from_v2(cls, data: dict[str, Any], source: str) -> HubState:
        general = GeneralConfig.from_dict(data.get("general", {}), source)
        active: InstalledConfig | None = None
        if "installation" in data and "profile" in data:
            active = InstalledConfig.from_dict(
                {
                    "schema_version": 1,
                    "profile": data["profile"],
                    "stack": data.get("stack", {"id": DEFAULT_STACK_ID}),
                    "installation": data["installation"],
                    "general": data.get("general", {}),
                },
                source,
            )
        known = tuple(
            entry
            for entry in (
                KnownInstallation.from_dict(item, source)
                for item in data.get("known_installations", []) or []
            )
            if entry is not None
        )
        seen: dict[str, KnownInstallation] = {}
        for entry in known:
            # A folder written twice, in two spellings, is one installation.
            seen[entry.key] = entry
        state = cls(active=active, known=tuple(seen.values()), general=general)
        return state.reconciled()


def dumps_state(state: HubState) -> str:
    return yaml.safe_dump(state.to_dict(), sort_keys=False, allow_unicode=True)


def loads_state(text: str, source: str = "<state>") -> HubState:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: could not parse the Hub state: {exc}") from exc
    return HubState.from_dict(data, source)


def read_state(path: Path) -> HubState | None:
    """The Hub's state, migrated if it was written by an older version. None when absent."""
    if not path.is_file():
        return None
    return loads_state(path.read_text(encoding="utf-8"), source=str(path))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_state(path: Path, state: HubState) -> None:
    """Write the Hub state, atomically, only when it would change the file."""
    text = dumps_state(state)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    _write(path, text)


def write_installed_config(path: Path, config: InstalledConfig) -> None:
    """Write an installation's own configuration, atomically and only when needed."""
    text = dumps_config(config)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    _write(path, text)


def known_from(roots: Iterable[tuple[str, InstallLocationMode]]) -> tuple[KnownInstallation, ...]:
    """Known installations from bare (root, mode) pairs, deduplicated."""
    seen: dict[str, KnownInstallation] = {}
    for root, mode in roots:
        entry = KnownInstallation(Path(os.path.normpath(root)), mode)
        seen.setdefault(entry.key, entry)
    return tuple(seen.values())


def location_of(entry: KnownInstallation) -> InstallLocation:
    """The install location a known installation stands for."""
    if entry.mode is InstallLocationMode.CUSTOM:
        return InstallLocation.custom(entry.root)
    return InstallLocation(entry.mode, None)


__all__ = [
    "FAILURE",
    "INSTALL",
    "REPAIR",
    "STATE_FILENAME",
    "STATE_SCHEMA_VERSION",
    "HubState",
    "KnownInstallation",
    "OperationRecord",
    "dumps_state",
    "known_from",
    "loads_state",
    "location_of",
    "now",
    "read_state",
    "root_key",
    "write_installed_config",
    "write_state",
]
