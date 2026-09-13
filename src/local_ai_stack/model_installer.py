"""Model acquisition: detect, plan, apply, verify.

The same four questions the component provisioners answer, asked per model. The
two providers answer them very differently and are not forced into a shared
fake filesystem:

- a **ComfyUI** model is a file this hub owns. It is downloaded through the
  existing Downloader, so bytes land in a ``.part`` and are promoted atomically
  only after the SHA-256 matches. A checksum is mandatory: the catalog refuses
  to build a file source without one, so an unverifiable model is never
  fetched. A file whose hash does not match the current pin is INVALID, which
  is also what makes a re-pin visible rather than silently accepted.
- an **Ollama** model lives in Ollama's own store. There is no file to place
  and no path to invent, so acquisition is a pull and verification is asking
  the store what it holds.

Nothing here removes or overwrites a model somebody else installed, and nothing
here configures a service or a workflow — that is IC6's half of the contract.

**Verification is remembered.** Hashing a multi-gigabyte file is an integrity
operation, not a health check, and the Hub asks "is this model available?"
every time it draws the Dashboard, computes pending work or opens Open WebUI.
So a file that passed a full SHA-256 check gets a *verification record* beside
it: the model id, the checksum it was verified against, and the size and
mtime the file had at that moment. A later look is a ``stat``: the record
still describes this file and this pin, so the file is AVAILABLE without
reading it. Anything else — no record, a different pin, a different size or
mtime, a record for another model, a record that does not parse — falls back
to the full hash, exactly as before, and a hash that fails discards the record.
A record is only ever written after a full hash has agreed with the pin; the
download itself still verifies every byte before the file is promoted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import stat as stat_module
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .download import (
    ChecksumMismatchError,
    Downloader,
    DownloadError,
    DownloadSpec,
    ProgressCallback,
    sha256_of,
)
from .models import (
    FileModelSource,
    ManagedModelPaths,
    ModelId,
    ModelKind,
    ModelSpec,
    ModelState,
    OllamaModelSource,
)
from .ollama_models import OllamaClientError, OllamaModelClient

logger = logging.getLogger(__name__)

VERIFICATION_SUFFIX = ".pepito-verified.json"
VERIFICATION_SCHEMA_VERSION = 2


def verification_path(destination: Path) -> Path:
    """Where the record for a managed model file lives: beside it, never inside it."""
    return destination.with_name(destination.name + VERIFICATION_SUFFIX)


@dataclass(frozen=True)
class FileVerification:
    """Proof that one managed file was hashed and matched one pin.

    Bound to the model, checksum, canonical path and filesystem identity at
    verification time. Copying a file together with its record is not proof
    for the new path; replacing it with matching timestamps is not proof either.
    """

    model_id: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    path: str
    device: int
    inode: int
    ctime_ns: int
    verified_at: float = 0.0
    schema_version: int = VERIFICATION_SCHEMA_VERSION

    def covers(
        self, spec: ModelSpec, expected_sha256: str, destination: Path, stat: os.stat_result
    ) -> bool:
        """True when this record describes exactly this file under exactly this pin."""
        return (
            self.schema_version == VERIFICATION_SCHEMA_VERSION
            and self.model_id == str(spec.id)
            and self.sha256 == expected_sha256
            and self.path == _canonical_path(destination)
            and self.device == stat.st_dev
            and self.inode == stat.st_ino
            and self.size_bytes == stat.st_size
            and self.mtime_ns == stat.st_mtime_ns
            and self.ctime_ns == stat.st_ctime_ns
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "path": self.path,
            "device": self.device,
            "inode": self.inode,
            "ctime_ns": self.ctime_ns,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> FileVerification | None:
        """Parse one record; anything malformed is no record at all."""
        if not isinstance(data, dict):
            return None
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != VERIFICATION_SCHEMA_VERSION
        ):
            return None
        if any(
            not isinstance(data.get(key), str) or not data[key]
            for key in ("model_id", "sha256", "path")
        ):
            return None
        if len(data["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in data["sha256"]):
            return None
        integer_fields = ("size_bytes", "mtime_ns", "device", "inode", "ctime_ns")
        if any(type(data.get(key)) is not int or data[key] < 0 for key in integer_fields):
            return None
        verified_at = data.get("verified_at")
        if type(verified_at) not in (int, float) or verified_at < 0:
            return None
        try:
            if not math.isfinite(verified_at):
                return None
        except OverflowError:
            return None
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


def _canonical_path(destination: Path) -> str:
    return os.path.normcase(str(destination.resolve()))


def _file_identity(stat: os.stat_result) -> tuple[int, ...]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_verification(destination: Path) -> FileVerification | None:
    try:
        data = json.loads(verification_path(destination).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return FileVerification.from_dict(data)


def write_verification(destination: Path, record: FileVerification) -> None:
    """Atomic, like a marker: a crash mid-write leaves no half-parsed proof."""
    path = verification_path(destination)
    # Unique temporaries avoid one status worker replacing another's partial record.
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(record.to_dict(), indent=2))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def discard_verification(destination: Path) -> None:
    with contextlib.suppress(OSError):
        verification_path(destination).unlink()


class ModelOutcome(StrEnum):
    ACQUIRED = "acquired"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry-run"


class ModelFailure(StrEnum):
    UNPINNED = "unpinned"
    CHECKSUM = "checksum"
    NETWORK = "network"
    PROVIDER = "provider"
    FILESYSTEM = "filesystem"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelStatus:
    """What is true about one model right now. Read-only to produce."""

    id: ModelId
    kind: ModelKind
    state: ModelState
    detail: str = ""
    revision: str | None = None
    path: Path | None = None
    reference: str | None = None

    @property
    def available(self) -> bool:
        return self.state is ModelState.AVAILABLE


@dataclass(frozen=True)
class ModelStep:
    action: str
    target: str
    summary: str


@dataclass(frozen=True)
class ModelPlan:
    model_id: ModelId
    steps: tuple[ModelStep, ...]
    status: ModelStatus

    @property
    def is_noop(self) -> bool:
        return not self.steps


@dataclass(frozen=True)
class ModelResult:
    model_id: ModelId
    outcome: ModelOutcome
    status: ModelStatus
    steps: tuple[ModelStep, ...] = ()
    detail: str = ""
    failure: ModelFailure | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not ModelOutcome.FAILED

    @property
    def changed(self) -> bool:
        return self.outcome is ModelOutcome.ACQUIRED


def _snapshot_owner() -> tuple[int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass
class _DetectionSnapshot:
    owner: tuple[int, object]
    generation: object
    memo: dict[ModelSpec, ModelStatus] = field(default_factory=dict)


@dataclass
class ModelInstaller:
    """Acquires one catalog of models into one installation.

    ``paths`` is the only authority on where a managed file goes; this class
    never composes a destination itself.
    """

    paths: ManagedModelPaths
    downloader: Downloader = field(default_factory=Downloader)
    ollama: OllamaModelClient | None = None
    log: logging.Logger = logger
    # Answers memoised for the duration of one snapshot (see below); None
    # outside one, so every detection reads the world afresh.
    _snapshot: ContextVar[_DetectionSnapshot | None] = field(
        default_factory=lambda: ContextVar("model_detection_snapshot", default=None),
        init=False,
        repr=False,
    )
    _generation: object = field(default_factory=object, init=False, repr=False)

    # --- deteccao ----------------------------------------------------------

    @contextlib.contextmanager
    def detection_snapshot(self) -> Iterator[None]:
        """One answer per model while this is open.

        A status pass asks about the same model from several directions: the
        workflow graph, the service configuration, the plan's dry run. Inside
        the snapshot the first answer is reused, so a pass never queries the
        Ollama store or the model tree twice for one model. The memo dies with
        the block -- it is never global -- and a snapshot opened inside another
        joins it rather than starting over. Separate threads/tasks cannot share
        its answers. Acquisition invalidates every open snapshot on this installer.
        """
        if self._current_snapshot() is not None:
            yield
            return
        token = self._snapshot.set(_DetectionSnapshot(_snapshot_owner(), self._generation))
        try:
            yield
        finally:
            self._snapshot.reset(token)

    def _current_snapshot(self) -> _DetectionSnapshot | None:
        snapshot = self._snapshot.get()
        if snapshot is None or snapshot.owner != _snapshot_owner():
            return None
        if snapshot.generation is not self._generation:
            snapshot.memo.clear()
            snapshot.generation = self._generation
        return snapshot

    def _invalidate_snapshot(self) -> None:
        self._generation = object()

    def detect(self, spec: ModelSpec) -> ModelStatus:
        """Downloads nothing, pulls nothing.

        The only thing it may write is a verification record beside a file
        that has just passed a full checksum, so the next look is a ``stat``.
        """
        snapshot = self._current_snapshot()
        if snapshot is not None and spec in snapshot.memo:
            return snapshot.memo[spec]
        generation = self._generation
        if isinstance(spec.source, FileModelSource):
            status = self._detect_file(spec, spec.source)
        else:
            status = self._detect_ollama(spec, spec.source)
        if snapshot is not None and generation is self._generation:
            snapshot.memo[spec] = status
        return status

    def _detect_file(self, spec: ModelSpec, source: FileModelSource) -> ModelStatus:
        destination = self.paths.destination_for(spec)
        try:
            stat = destination.stat()
        except OSError:
            stat = None
        if stat is None or not stat_module.S_ISREG(stat.st_mode):
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.NOT_INSTALLED,
                detail=f"{source.artifact.filename} is not in the managed model tree.",
            )
        expected = source.artifact.sha256
        if expected is None:
            # The catalog does not build a file source without a checksum, so
            # this is unreachable through it; refuse rather than trust bytes.
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.INVALID,
                detail="the file has no pinned checksum, so it cannot be verified",
                path=destination,
            )
        record = read_verification(destination)
        if record is not None and record.covers(spec, expected, destination, stat):
            try:
                if _file_identity(destination.stat()) != _file_identity(stat):
                    return self._unstable_file(spec, destination)
            except OSError:
                return self._unstable_file(spec, destination)
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.AVAILABLE,
                detail="verified against the pinned checksum (recorded)",
                path=destination,
                revision=spec.revision,
            )
        # No usable proof: read every byte, as acquisition did. A record that
        # is present but does not cover this file or this pin is stale and is
        # replaced by the outcome below, never trusted.
        try:
            actual = sha256_of(destination)
            fresh = destination.stat()
        except OSError:
            return self._unstable_file(spec, destination)
        if _file_identity(fresh) != _file_identity(stat):
            return self._unstable_file(spec, destination)
        if actual != expected:
            discard_verification(destination)
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.INVALID,
                detail=(
                    f"{source.artifact.filename} does not match the pinned checksum "
                    "(a partial, corrupted or re-pinned file)."
                ),
                path=destination,
                revision=spec.revision,
            )
        self._record_verification(spec, destination, expected, fresh)
        return ModelStatus(
            id=spec.id,
            kind=spec.kind,
            state=ModelState.AVAILABLE,
            detail="verified against the pinned checksum",
            path=destination,
            revision=spec.revision,
        )

    def _unstable_file(self, spec: ModelSpec, destination: Path) -> ModelStatus:
        discard_verification(destination)
        return ModelStatus(
            id=spec.id,
            kind=spec.kind,
            state=ModelState.INVALID,
            detail="the model file changed or became unreadable during checksum verification",
            path=destination,
            revision=spec.revision,
        )

    def _record_verification(
        self, spec: ModelSpec, destination: Path, sha256: str, stat: os.stat_result
    ) -> None:
        """Remember a full hash that agreed with the pin.

        Best effort: a record that cannot be written only means the next look
        hashes again. ``stat`` was checked both before and after the full hash.
        """
        record = FileVerification(
            model_id=str(spec.id),
            sha256=sha256,
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            path=_canonical_path(destination),
            device=stat.st_dev,
            inode=stat.st_ino,
            ctime_ns=stat.st_ctime_ns,
            verified_at=time.time(),
        )
        try:
            write_verification(destination, record)
        except OSError as exc:
            self.log.debug("could not record verification for %s: %s", spec.id, exc)

    def _detect_ollama(self, spec: ModelSpec, source: OllamaModelSource) -> ModelStatus:
        if self.ollama is None:
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.NOT_INSTALLED,
                detail="no Ollama client is configured, so its store cannot be read",
                reference=source.reference,
            )
        try:
            entries = self.ollama.list_models()
        except OllamaClientError as exc:
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.NOT_INSTALLED,
                detail=str(exc),
                reference=source.reference,
            )
        entry = next((item for item in entries if item.name == source.reference), None)
        if entry is None:
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.NOT_INSTALLED,
                detail=f"{source.reference} is not in the Ollama model store.",
                reference=source.reference,
            )
        if not entry.matches_digest(source.digest):
            return ModelStatus(
                id=spec.id,
                kind=spec.kind,
                state=ModelState.INVALID,
                detail=(
                    f"{source.reference} is present but its digest does not match the "
                    f"pinned {source.digest}; the tag was republished upstream."
                ),
                reference=source.reference,
                revision=spec.revision,
            )
        return ModelStatus(
            id=spec.id,
            kind=spec.kind,
            state=ModelState.AVAILABLE,
            detail="present in the Ollama model store",
            reference=source.reference,
            revision=spec.revision,
        )

    # --- plano -------------------------------------------------------------

    def plan(self, spec: ModelSpec) -> ModelPlan:
        status = self.detect(spec)
        if status.available:
            return ModelPlan(spec.id, (), status)
        if isinstance(spec.source, FileModelSource):
            destination = self.paths.destination_for(spec)
            steps = (
                ModelStep(
                    action="download",
                    target=spec.source.artifact.filename,
                    summary=(
                        f"Download {spec.source.artifact.filename} and verify its SHA-256, "
                        f"then place it in {destination.parent}."
                    ),
                ),
            )
        else:
            steps = (
                ModelStep(
                    action="pull",
                    target=spec.source.reference,
                    summary=f"Pull {spec.source.reference} into the Ollama model store.",
                ),
            )
        return ModelPlan(spec.id, steps, status)

    # --- aplicacao ---------------------------------------------------------

    def apply(
        self, spec: ModelSpec, *, dry_run: bool = False, progress: ProgressCallback | None = None
    ) -> ModelResult:
        """Acquire one model. ``progress`` is forwarded to the Downloader as-is.

        It fires only for a real, in-flight HTTP transfer: a dry run returns
        before touching the network, an Ollama pull has no byte-level source to
        report, and an already-satisfied file is a SKIP, never a transfer.
        """
        if not dry_run:
            self._invalidate_snapshot()
        plan = self.plan(spec)
        if plan.is_noop:
            return ModelResult(
                spec.id, ModelOutcome.SKIPPED, plan.status, detail="already available"
            )
        if dry_run:
            return ModelResult(
                spec.id,
                ModelOutcome.DRY_RUN,
                plan.status,
                steps=plan.steps,
                detail=plan.steps[0].summary,
            )
        try:
            if isinstance(spec.source, FileModelSource):
                return self._acquire_file(spec, spec.source, plan, progress)
            return self._acquire_ollama(spec, spec.source, plan)
        finally:
            self._invalidate_snapshot()

    def _acquire_file(
        self,
        spec: ModelSpec,
        source: FileModelSource,
        plan: ModelPlan,
        progress: ProgressCallback | None,
    ) -> ModelResult:
        destination = self.paths.destination_for(spec)
        discard_verification(destination)
        download = DownloadSpec(
            url=source.artifact.url,
            destination=destination,
            sha256=source.artifact.sha256,
            size_bytes=source.artifact.size_bytes,
        )
        try:
            self.downloader.fetch(download, progress=progress)
        except ChecksumMismatchError as exc:
            return self._failed(spec, plan, ModelFailure.CHECKSUM, str(exc))
        except DownloadError as exc:
            return self._failed(spec, plan, ModelFailure.NETWORK, str(exc))
        except OSError as exc:
            return self._failed(spec, plan, ModelFailure.FILESYSTEM, str(exc))
        return self._confirm(spec, plan)

    def _acquire_ollama(
        self, spec: ModelSpec, source: OllamaModelSource, plan: ModelPlan
    ) -> ModelResult:
        # No `progress` parameter here: a pull has no Downloader-shaped byte
        # stream to report on (see HttpOllamaClient.pull), so acquisition stays
        # logical for this source, same as it always has.
        if self.ollama is None:
            return self._failed(
                spec,
                plan,
                ModelFailure.PROVIDER,
                "no Ollama client is configured, so nothing can be pulled",
            )
        try:
            self.ollama.pull(source.reference)
        except OllamaClientError as exc:
            return self._failed(spec, plan, ModelFailure.PROVIDER, str(exc))
        return self._confirm(spec, plan)

    def _confirm(self, spec: ModelSpec, plan: ModelPlan) -> ModelResult:
        """Acquisition only counts once verification agrees."""
        status = self.verify(spec)
        if not status.available:
            return ModelResult(
                spec.id,
                ModelOutcome.FAILED,
                status,
                steps=plan.steps,
                detail=status.detail,
                failure=ModelFailure.UNKNOWN,
            )
        return ModelResult(spec.id, ModelOutcome.ACQUIRED, status, steps=plan.steps)

    def _failed(
        self, spec: ModelSpec, plan: ModelPlan, failure: ModelFailure, detail: str
    ) -> ModelResult:
        return ModelResult(
            spec.id,
            ModelOutcome.FAILED,
            self.verify(spec),
            steps=plan.steps,
            detail=detail,
            failure=failure,
        )

    # --- verificacao -------------------------------------------------------

    def verify(self, spec: ModelSpec) -> ModelStatus:
        """Re-read the world. Detection is already the proof for both providers."""
        self._invalidate_snapshot()
        return self.detect(spec)

    def apply_all(
        self, specs: Sequence[ModelSpec], *, dry_run: bool = False
    ) -> tuple[ModelResult, ...]:
        return tuple(self.apply(spec, dry_run=dry_run) for spec in specs)


__all__ = [
    "VERIFICATION_SUFFIX",
    "FileVerification",
    "ModelFailure",
    "ModelInstaller",
    "ModelOutcome",
    "ModelPlan",
    "ModelResult",
    "ModelStatus",
    "ModelStep",
    "discard_verification",
    "read_verification",
    "verification_path",
    "write_verification",
]
