"""Image generation request/result and provider observation seams, without IO."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .operation_contracts import (
    OperationDiagnostic,
    OperationEventSink,
    OperationProgress,
    OperationResult,
    OperationState,
    _identifier,
    _integer,
)


@dataclass(frozen=True)
class ImageGenerationRequest:
    """Select a profile and its declared text-to-image workflow, never model paths.

    The coordinator resolves profile.workflows[workflow].manifest_id and all
    required model roles using the existing domain before any submission.
    """

    profile_id: str
    prompt: str
    workflow: str = "text_to_image"
    width: int = 1024
    height: int = 1024
    steps: int = 20
    seed: int = 0

    def __post_init__(self) -> None:
        _identifier(self.profile_id)
        if self.workflow != "text_to_image":
            raise ValueError("only the existing text_to_image profile selection is supported")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be non-empty")
        for value in (self.width, self.height, self.steps):
            _integer(value, minimum=1)
        _integer(self.seed)


@dataclass(frozen=True)
class ImageGenerationResult(OperationResult):
    def __post_init__(self) -> None:
        super().__post_init__()
        if self.state is OperationState.SUCCEEDED and not self.output_references:
            raise ValueError("successful image generation requires an output reference")
        if self.state is OperationState.FAILED and self.output_references:
            raise ValueError("failed image generation cannot publish completed outputs")


@runtime_checkable
class ImageGenerationCoordinator(Protocol):
    """Synchronous application operation; an adapter supplies scheduling.

    The caller allocates a fresh opaque operation_id. Exactly one terminal event
    carries the returned result. Operational failures become FAILED results;
    invalid input/programming errors may raise before the operation is accepted.
    """

    def generate(
        self,
        operation_id: str,
        request: ImageGenerationRequest,
        *,
        emit: OperationEventSink | None = None,
    ) -> ImageGenerationResult: ...


class ImageJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class ImageJobReceipt:
    """Only a successful submission produces this receipt. job_id is prompt_id.

    This is an internal adapter value, never an OperationEvent/TUI field.
    """

    job_id: str

    def __post_init__(self) -> None:
        _identifier(self.job_id)


@dataclass(frozen=True)
class ImageJobObservation:
    receipt: ImageJobReceipt
    state: ImageJobState
    progress: OperationProgress | None = None
    output_references: tuple[str, ...] = ()
    diagnostic: OperationDiagnostic | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ImageJobReceipt) or not isinstance(
            self.state, ImageJobState
        ):
            raise TypeError("observation requires a receipt and typed job state")
        if self.progress is not None and not isinstance(self.progress, OperationProgress):
            raise ValueError("invalid job progress")
        if self.diagnostic is not None and not isinstance(self.diagnostic, OperationDiagnostic):
            raise ValueError("invalid job diagnostic")
        if isinstance(self.output_references, (str, bytes)):
            raise TypeError("output references must be a collection of identifiers")
        object.__setattr__(self, "output_references", tuple(self.output_references))
        for reference in self.output_references:
            _identifier(reference)
        if (self.state is ImageJobState.COMPLETED) != bool(self.output_references):
            raise ValueError("only completed jobs have outputs, and require at least one")
        if self.state is ImageJobState.FAILED and self.diagnostic is None:
            raise ValueError("failed job requires a diagnostic")


class ImageJobObservationError(RuntimeError):
    """Observation failed; this does not assert that the provider job failed."""

    def __init__(self, diagnostic: OperationDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


@runtime_checkable
class ImageJobObserver(Protocol):
    """One bounded observation of an accepted job; no polling/submission loop.

    timeout_seconds must be positive and finite. Missing/ambiguous provider data
    must not be guessed as QUEUED or COMPLETED: the adapter raises
    ImageJobObservationError. A provider job failure is a FAILED observation.
    """

    def observe(
        self, receipt: ImageJobReceipt, *, timeout_seconds: float
    ) -> ImageJobObservation: ...
