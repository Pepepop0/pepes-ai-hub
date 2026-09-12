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
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

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

    # --- deteccao ----------------------------------------------------------

    def detect(self, spec: ModelSpec) -> ModelStatus:
        """Read-only. Downloads nothing, pulls nothing, creates nothing."""
        if isinstance(spec.source, FileModelSource):
            return self._detect_file(spec, spec.source)
        return self._detect_ollama(spec, spec.source)

    def _detect_file(self, spec: ModelSpec, source: FileModelSource) -> ModelStatus:
        destination = self.paths.destination_for(spec)
        if not destination.is_file():
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
        actual = sha256_of(destination)
        if actual != expected:
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
        return ModelStatus(
            id=spec.id,
            kind=spec.kind,
            state=ModelState.AVAILABLE,
            detail="verified against the pinned checksum",
            path=destination,
            revision=spec.revision,
        )

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
        if isinstance(spec.source, FileModelSource):
            return self._acquire_file(spec, spec.source, plan, progress)
        return self._acquire_ollama(spec, spec.source, plan)

    def _acquire_file(
        self,
        spec: ModelSpec,
        source: FileModelSource,
        plan: ModelPlan,
        progress: ProgressCallback | None,
    ) -> ModelResult:
        destination = self.paths.destination_for(spec)
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
            self.detect(spec),
            steps=plan.steps,
            detail=detail,
            failure=failure,
        )

    # --- verificacao -------------------------------------------------------

    def verify(self, spec: ModelSpec) -> ModelStatus:
        """Re-read the world. Detection is already the proof for both providers."""
        return self.detect(spec)

    def apply_all(
        self, specs: Sequence[ModelSpec], *, dry_run: bool = False
    ) -> tuple[ModelResult, ...]:
        return tuple(self.apply(spec, dry_run=dry_run) for spec in specs)


__all__ = [
    "ModelFailure",
    "ModelInstaller",
    "ModelOutcome",
    "ModelPlan",
    "ModelResult",
    "ModelStatus",
    "ModelStep",
]
