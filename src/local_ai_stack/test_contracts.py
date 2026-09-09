"""Internal contracts shared by post-MVP test tooling.

This module is not a product API.  It contains transport-neutral values so the
E2E harness and the opt-in local Test Control API can evolve on separate
branches without inventing incompatible phase, result, or event shapes.
Neither side gains authority here: paths, processes, provisioning, and runtime
control remain owned by the existing application domains.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class TestPhase(StrEnum):
    PREFLIGHT = "preflight"
    PLAN = "plan"
    PROVISION = "provision"
    START = "start"
    ACQUIRE_MODELS = "acquire_models"
    CONFIGURE = "configure"
    CHAT_SMOKE = "chat_smoke"
    IMAGE_SMOKE = "image_smoke"
    STOP = "stop"
    CLEANUP = "cleanup"
    SECOND_PLAN = "second_plan"


class TestRootMode(StrEnum):
    """How a caller selects a root without accepting an arbitrary path."""

    EPHEMERAL = "ephemeral"
    CONFIGURED = "configured"


class TestProviderMode(StrEnum):
    CONTROLLED = "controlled"
    LOCAL_REAL = "local-real"


class TestRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class TestPhaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class TestEventKind(StrEnum):
    RUN_STATE = "run-state"
    PHASE_STATE = "phase-state"
    PROGRESS = "progress"
    LOG_REFERENCE = "log-reference"
    RESULT = "result"


@dataclass(frozen=True)
class TestRunSpec:
    """A capability request after transport input has been validated."""

    profile_id: str
    phases: tuple[TestPhase, ...]
    root_mode: TestRootMode = TestRootMode.EPHEMERAL
    provider_mode: TestProviderMode = TestProviderMode.CONTROLLED
    allow_network: bool = False
    allow_gpu: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id must be non-empty")
        if not self.phases:
            raise ValueError("a test run requires at least one phase")
        if len(set(self.phases)) != len(self.phases):
            raise ValueError("a test run cannot repeat a phase")


@dataclass(frozen=True)
class TestPhaseResult:
    phase: TestPhase
    outcome: TestPhaseOutcome
    detail: str = ""
    duration_seconds: float | None = None
    data: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_seconds is not None and self.duration_seconds < 0:
            raise ValueError("phase duration cannot be negative")


@dataclass(frozen=True)
class TestRunReport:
    run_id: str
    state: TestRunState
    spec: TestRunSpec
    phases: tuple[TestPhaseResult, ...] = ()
    started_at: str | None = None
    finished_at: str | None = None
    detail: str = ""
    context: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


@dataclass(frozen=True)
class TestEvent:
    run_id: str
    sequence: int
    kind: TestEventKind
    timestamp: str
    phase: TestPhase | None = None
    message: str = ""
    data: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")
        if self.sequence < 1:
            raise ValueError("event sequence starts at 1")
        if not self.timestamp.strip():
            raise ValueError("event timestamp must be non-empty")


TestEventSink = Callable[[TestEvent], None]


@runtime_checkable
class TestHarness(Protocol):
    """Synchronous core implemented by the harness and scheduled by adapters."""

    def run(
        self,
        run_id: str,
        spec: TestRunSpec,
        *,
        emit: TestEventSink | None = None,
    ) -> TestRunReport: ...


__all__ = [
    "TestEvent",
    "TestEventKind",
    "TestEventSink",
    "TestHarness",
    "TestPhase",
    "TestPhaseOutcome",
    "TestPhaseResult",
    "TestProviderMode",
    "TestRootMode",
    "TestRunReport",
    "TestRunSpec",
    "TestRunState",
]
