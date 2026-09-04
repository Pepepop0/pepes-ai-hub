"""Configuration this manager generates, and is therefore allowed to rewrite.

Provisioning owns binaries; this owns the files that tell those binaries how to
find each other. The two never mix: nothing here downloads, extracts or
installs, and nothing here decides what a service *is*.

Ownership is the whole design. A generated file carries a marker naming this
manager and the exact content it wrote. Reading it back gives three answers and
only three:

``ABSENT``
    nothing is there. Writing is safe.
``MANAGED``
    our marker is present. The file is ours to regenerate.
``EXTERNAL``
    a file exists that we did not write. It is never touched, never merged and
    never backed up over: the plan reports a conflict and stops.

Everything else follows from that. Generation is deterministic, so regenerating
identical content is ``UNCHANGED`` rather than a write, which makes apply
idempotent. A dry run produces the same plan and touches nothing. Writes go
through a temporary file and a rename, so a crash cannot leave a half-written
config where a whole one used to be.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

MANAGED_BY = "PepitosAIHub"
MARKER_KEY = "x-managed-by"
DIGEST_KEY = "x-content-digest"
MARKER_COMMENT_PREFIX = "#"


class ConfigOwnershipError(RuntimeError):
    """Refusing to write over configuration this manager does not own."""


class Ownership(StrEnum):
    ABSENT = "absent"
    MANAGED = "managed"
    EXTERNAL = "external"


class ConfigAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


class ConfigFormat(StrEnum):
    """How the marker is expressed, which depends on what reads the file."""

    JSON = "json"
    ENV = "env"
    YAML = "yaml"


def content_digest(body: str) -> str:
    """Digest of the generated body, marker excluded, so it is stable."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConfigDocument:
    """One file this manager generates, plus the marker that claims it."""

    path: Path
    body: str
    fmt: ConfigFormat = ConfigFormat.ENV
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        if not self.path.is_absolute():
            raise ValueError("a managed config document needs an absolute path")

    @property
    def digest(self) -> str:
        return content_digest(self.body)

    def rendered(self) -> str:
        """The bytes that actually land on disk: marker plus body."""
        if self.fmt is ConfigFormat.JSON:
            payload = json.loads(self.body)
            if not isinstance(payload, dict):
                raise ValueError("a JSON managed config must be an object to carry its marker")
            marked = {MARKER_KEY: MANAGED_BY, DIGEST_KEY: self.digest, **payload}
            return json.dumps(marked, indent=2, sort_keys=True) + "\n"
        header = (
            f"{MARKER_COMMENT_PREFIX} {MARKER_KEY}: {MANAGED_BY}\n"
            f"{MARKER_COMMENT_PREFIX} {DIGEST_KEY}: {self.digest}\n"
            f"{MARKER_COMMENT_PREFIX} Generated file. Edits are replaced on the next apply.\n"
        )
        body = self.body if self.body.endswith("\n") else self.body + "\n"
        return header + body


@dataclass(frozen=True)
class ConfigChange:
    """What apply would do to one file, and why."""

    document: ConfigDocument
    action: ConfigAction
    ownership: Ownership
    detail: str = ""

    @property
    def path(self) -> Path:
        return self.document.path

    @property
    def writes(self) -> bool:
        return self.action in {ConfigAction.CREATE, ConfigAction.UPDATE}


@dataclass(frozen=True)
class ConfigPlan:
    """Every change one apply would make. Reading it changes nothing."""

    changes: tuple[ConfigChange, ...] = ()

    @property
    def conflicts(self) -> tuple[ConfigChange, ...]:
        return tuple(c for c in self.changes if c.action is ConfigAction.CONFLICT)

    @property
    def writes(self) -> tuple[ConfigChange, ...]:
        return tuple(c for c in self.changes if c.writes)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)

    @property
    def up_to_date(self) -> bool:
        return not self.writes and not self.conflicts


@dataclass(frozen=True)
class ConfigApplyReport:
    written: tuple[Path, ...] = ()
    unchanged: tuple[Path, ...] = ()
    refused: tuple[Path, ...] = ()
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.refused


@dataclass(frozen=True)
class ConfigVerifyReport:
    """Whether what is on disk is still what this manager generated."""

    matching: tuple[Path, ...] = ()
    drifted: tuple[Path, ...] = ()
    missing: tuple[Path, ...] = ()
    foreign: tuple[Path, ...] = ()
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not (self.drifted or self.missing or self.foreign)


def read_marker(path: Path) -> tuple[Ownership, str | None]:
    """Who owns the file on disk, and which content it claims to hold."""
    if not path.is_file():
        return Ownership.ABSENT, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return Ownership.EXTERNAL, None
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            return Ownership.EXTERNAL, None
        if isinstance(payload, dict) and payload.get(MARKER_KEY) == MANAGED_BY:
            digest = payload.get(DIGEST_KEY)
            return Ownership.MANAGED, digest if isinstance(digest, str) else None
        return Ownership.EXTERNAL, None
    digest: str | None = None
    owned = False
    for line in text.splitlines():
        if not line.startswith(MARKER_COMMENT_PREFIX):
            break
        if f"{MARKER_KEY}: {MANAGED_BY}" in line:
            owned = True
        if DIGEST_KEY in line:
            digest = line.split(":", 2)[-1].strip()
    return (Ownership.MANAGED, digest) if owned else (Ownership.EXTERNAL, None)


class ManagedConfigWriter:
    """Plans, applies and verifies generated configuration files."""

    def __init__(self, *, managed_by: str = MANAGED_BY) -> None:
        self.managed_by = managed_by

    # --- plan --------------------------------------------------------------

    def plan(self, documents: Iterable[ConfigDocument]) -> ConfigPlan:
        """Decide what would change. Never writes, never creates a directory."""
        return ConfigPlan(tuple(self._change_for(document) for document in documents))

    def _change_for(self, document: ConfigDocument) -> ConfigChange:
        ownership, digest = read_marker(document.path)
        if ownership is Ownership.ABSENT:
            return ConfigChange(document, ConfigAction.CREATE, ownership, "no file yet")
        if ownership is Ownership.EXTERNAL:
            return ConfigChange(
                document,
                ConfigAction.CONFLICT,
                ownership,
                "a file exists that this manager did not generate; it is left untouched",
            )
        if digest == document.digest:
            return ConfigChange(
                document, ConfigAction.UNCHANGED, ownership, "already the generated content"
            )
        return ConfigChange(document, ConfigAction.UPDATE, ownership, "generated content changed")

    # --- apply -------------------------------------------------------------

    def apply(
        self, documents: Iterable[ConfigDocument], *, dry_run: bool = False
    ) -> ConfigApplyReport:
        """Write what the plan says, or nothing at all when dry.

        A conflict is refused rather than resolved: the file that was already
        there is somebody else's, and merging into it would be a guess.
        """
        plan = self.plan(documents)
        written: list[Path] = []
        unchanged: list[Path] = []
        refused: list[Path] = [change.path for change in plan.conflicts]
        for change in plan.changes:
            if change.action is ConfigAction.UNCHANGED:
                unchanged.append(change.path)
                continue
            if not change.writes:
                continue
            if not dry_run:
                _atomic_write(change.path, change.document.rendered())
            written.append(change.path)
        return ConfigApplyReport(tuple(written), tuple(unchanged), tuple(refused), dry_run)

    # --- verify ------------------------------------------------------------

    def verify(self, documents: Iterable[ConfigDocument]) -> ConfigVerifyReport:
        """Is the configuration on disk still the configuration we generated?"""
        matching: list[Path] = []
        drifted: list[Path] = []
        missing: list[Path] = []
        foreign: list[Path] = []
        problems: list[str] = []
        for document in documents:
            ownership, digest = read_marker(document.path)
            if ownership is Ownership.ABSENT:
                missing.append(document.path)
                problems.append(f"{document.path.name}: not generated yet")
            elif ownership is Ownership.EXTERNAL:
                foreign.append(document.path)
                problems.append(f"{document.path.name}: exists but is not ours")
            elif digest != document.digest:
                drifted.append(document.path)
                problems.append(f"{document.path.name}: on disk differs from what we generate")
            else:
                matching.append(document.path)
        return ConfigVerifyReport(
            tuple(matching), tuple(drifted), tuple(missing), tuple(foreign), tuple(problems)
        )


def _atomic_write(path: Path, text: str) -> None:
    """Replace the file in one step, so a crash never truncates a config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def env_document(path: Path, values: Mapping[str, str], description: str = "") -> ConfigDocument:
    """An environment file, ordered so identical settings render identically."""
    body = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    return ConfigDocument(path, body, ConfigFormat.ENV, description)


def json_document(
    path: Path, payload: Mapping[str, object], description: str = ""
) -> ConfigDocument:
    body = json.dumps(dict(payload), indent=2, sort_keys=True)
    return ConfigDocument(path, body, ConfigFormat.JSON, description)


def yaml_document(path: Path, body: str, description: str = "") -> ConfigDocument:
    return ConfigDocument(path, body, ConfigFormat.YAML, description)


def describe_plan(plan: ConfigPlan) -> tuple[str, ...]:
    """One readable line per change, for a UI or a log."""
    return tuple(
        f"{change.action.value}: {change.path.name} ({change.detail})" for change in plan.changes
    )


__all__: Sequence[str] = (
    "ConfigAction",
    "ConfigApplyReport",
    "ConfigChange",
    "ConfigDocument",
    "ConfigFormat",
    "ConfigOwnershipError",
    "ConfigPlan",
    "ConfigVerifyReport",
    "ManagedConfigWriter",
    "Ownership",
    "content_digest",
    "describe_plan",
    "env_document",
    "json_document",
    "read_marker",
    "yaml_document",
)
