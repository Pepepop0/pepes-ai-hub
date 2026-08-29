"""Declarative description of installable artifacts.

An artifact is one downloadable file: a program archive, a model weight, a
helper tool. It says what to fetch and how to prove the bytes are right. It
says nothing about who wants it or what happens afterwards, so the same record
serves Ollama, Open WebUI, ComfyUI and auxiliary tooling.

This is not a package manager. There are no dependencies, no resolution and no
install rules here. Loading a manifest parses and validates text; it never
opens a socket and never touches the destination.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ARTIFACT_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_ALLOWED_SCHEMES = ("https://",)


class ArtifactError(ValueError):
    """An artifact record is malformed or unsupported."""


def _error(source: Path | str, field: str, message: str) -> ArtifactError:
    location = f"{source}: {field}" if field else str(source)
    return ArtifactError(f"{location}: {message}")


def _mapping(value: Any, source: Path | str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(source, field, "must be a mapping")
    return value


def _string(value: Any, source: Path | str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(source, field, "must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class Artifact:
    """One downloadable file, identified and (ideally) checksummed.

    `sha256` is optional in the schema because not every upstream publishes
    one, but when it is present the downloader treats it as mandatory: bytes
    that do not match are never promoted to the destination.
    """

    id: str
    url: str
    filename: str
    sha256: str | None = None
    size_bytes: int | None = None
    version: str | None = None

    @property
    def has_checksum(self) -> bool:
        return self.sha256 is not None

    def destination_in(self, directory: Path) -> Path:
        """Where this artifact lands inside a directory chosen by the caller."""
        return Path(directory) / self.filename

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "url": self.url, "filename": self.filename}
        if self.sha256 is not None:
            data["sha256"] = self.sha256
        if self.size_bytes is not None:
            data["size_bytes"] = self.size_bytes
        if self.version is not None:
            data["version"] = self.version
        return data

    @classmethod
    def from_dict(cls, data: Any, source: Path | str = "<artifact>") -> Artifact:
        record = _mapping(data, source, "artifact")
        allowed = {"id", "url", "filename", "sha256", "size_bytes", "version"}
        unknown = sorted((str(key) for key in record if key not in allowed), key=str.casefold)
        if unknown:
            raise _error(source, "artifact", f"unknown fields: {', '.join(unknown)}")
        for required in ("id", "url", "filename"):
            if required not in record:
                raise _error(source, required, "is required")

        artifact_id = _string(record["id"], source, "id")
        if not _ID_RE.match(artifact_id):
            raise _error(
                source,
                "id",
                "must use lowercase letters, digits, '.', '_' or '-' and start with a letter",
            )

        url = _string(record["url"], source, "url")
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            raise _error(source, "url", f"must start with one of: {', '.join(_ALLOWED_SCHEMES)}")

        filename = _string(record["filename"], source, "filename")
        if "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise _error(source, "filename", "must be a bare file name without any path separator")

        sha256 = record.get("sha256")
        if sha256 is not None:
            sha256 = _string(sha256, source, "sha256").lower()
            if not _SHA256_RE.match(sha256):
                raise _error(source, "sha256", "must be 64 hexadecimal characters")

        size_bytes = record.get("size_bytes")
        if size_bytes is not None and (
            not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0
        ):
            raise _error(source, "size_bytes", "must be an integer >= 0")

        version = record.get("version")
        if version is not None:
            version = _string(version, source, "version")

        return cls(
            id=artifact_id,
            url=url,
            filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
            version=version,
        )


def validate_artifact_manifest(data: Any, source: Path | str = "<manifest>") -> dict[str, Artifact]:
    """Validate a manifest document into artifacts keyed by id."""
    root = _mapping(data, source, "manifest root")
    unknown = sorted(
        (str(key) for key in root if key not in {"schema_version", "artifacts"}),
        key=str.casefold,
    )
    if unknown:
        raise _error(source, "manifest root", f"unknown fields: {', '.join(unknown)}")

    raw_version = root.get("schema_version")
    if not isinstance(raw_version, int) or isinstance(raw_version, bool):
        raise _error(source, "schema_version", "must be an integer")
    if raw_version != ARTIFACT_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {raw_version}; expected {ARTIFACT_SCHEMA_VERSION}",
        )

    entries = root.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise _error(source, "artifacts", "must be a non-empty list")

    artifacts: dict[str, Artifact] = {}
    for index, entry in enumerate(entries):
        artifact = Artifact.from_dict(entry, f"{source}[{index}]")
        if artifact.id in artifacts:
            raise _error(source, "artifacts", f"duplicate artifact id: {artifact.id}")
        artifacts[artifact.id] = artifact
    return artifacts


def load_artifact_manifest(path: Path) -> dict[str, Artifact]:
    """Read and validate a manifest file. Downloads nothing."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ArtifactError(f"{path}: could not read artifact manifest: {exc}") from exc
    return validate_artifact_manifest(data, path)
