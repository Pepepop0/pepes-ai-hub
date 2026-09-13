"""Provisioner contract.

A provisioner owns one component of the stack. It answers four questions:

    detect()  what is on this machine right now?
    plan()    what would I do about it?
    apply()   do it, idempotently, or refuse with a reason
    verify()  is the result actually usable?

Two axes are kept apart on purpose. `ComponentState` says whether something is
installed and usable. `Ownership` says who put it there. A working Ollama that
the user installed themselves is INSTALLED and EXTERNAL, and that combination
must never be treated as ours to modify, replace or remove.

A provisioner never decides *what* should be installed: the profile and the
stack decide that. It only performs the action for its component, and reports
structured facts back. It never prompts, never renders and never assumes a
terminal is attached; progress is emitted as events a caller may consume.

Detection is read-only. Directories are created by apply(), never by detect().

Handing over to the runtime
---------------------------
`runtime_handoff()` is the whole surface the runtime layer is allowed to see.
It answers one question — may this component be launched by us, and with which
executable — and nothing about archives, markers, downloads or extraction. A
MANAGED install is the only one that ever yields an executable; an EXTERNAL
install reports itself as EXTERNAL with no executable, so a caller physically
cannot use this contract to take ownership of someone else's binary.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .installation import ResolvedInstallation
from .layout import StackLayout
from .operation_contracts import OperationActivity, OperationActivityCategory, TransferProgress
from .operations import safe_text
from .pyenv import EnvironmentFailure, EnvironmentFailureKind, PipFailure, PipFailureKind
from .runner import CommandRunner, sanitize

logger = logging.getLogger(__name__)


def build_activity(
    category: OperationActivityCategory,
    action: str,
    item: str,
    *,
    detail: str | None = None,
    elapsed_seconds: float | None = None,
) -> OperationActivity:
    """The one place a provisioner turns its own facts into a safe activity.

    ``item`` and ``detail`` are sanitized here rather than trusted from the
    caller: a package name or a repository slug is ordinarily harmless, but a
    provisioner-specific parser is exactly the boundary the contract asks to
    sanitize at, not a place to assume upstream text is already safe.
    """
    return OperationActivity(
        category=category,
        action=action,
        item=safe_text(sanitize(item)),
        detail=safe_text(sanitize(detail)) if detail is not None else None,
        elapsed_seconds=elapsed_seconds,
    )


class ComponentState(StrEnum):
    """Is the component present and usable?"""

    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class Ownership(StrEnum):
    """Who installed it? Decides what we are allowed to touch."""

    NONE = "none"
    MANAGED = "managed"
    EXTERNAL = "external"


class ProvisionOutcome(StrEnum):
    INSTALLED = "installed"
    SKIPPED = "skipped"
    FAILED = "failed"
    DRY_RUN = "dry-run"


class FailureReason(StrEnum):
    """Why an apply did not install anything."""

    UNSUPPORTED = "unsupported"
    PRECONDITION = "precondition"
    EXTERNAL_PRESENT = "external-present"
    COMMAND_FAILED = "command-failed"
    NETWORK = "network"
    CHECKSUM = "checksum"
    FILESYSTEM = "filesystem"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


_PIP_FAILURE_REASONS = {
    PipFailureKind.NETWORK: FailureReason.NETWORK,
    PipFailureKind.CERTIFICATE: FailureReason.NETWORK,
    PipFailureKind.NO_MATCHING_DISTRIBUTION: FailureReason.PRECONDITION,
    PipFailureKind.RESOLUTION_CONFLICT: FailureReason.PRECONDITION,
    PipFailureKind.INVALID_INVOCATION: FailureReason.PRECONDITION,
    PipFailureKind.NO_DISK_SPACE: FailureReason.FILESYSTEM,
}


def reason_for_pip_failure(failure: PipFailure | None) -> FailureReason:
    """The failure reason a classified pip problem really is.

    Unclassified, a pip problem stays COMMAND_FAILED, which is what every pip
    failure used to be: a network outage and a requirement that does not exist
    are different problems, and only the ones pip named plainly are separated.
    """
    if failure is None:
        return FailureReason.COMMAND_FAILED
    return _PIP_FAILURE_REASONS.get(failure.kind, FailureReason.COMMAND_FAILED)


_ENVIRONMENT_FAILURE_REASONS = {
    EnvironmentFailureKind.INTERPRETER_IN_USE: FailureReason.FILESYSTEM,
    EnvironmentFailureKind.ACCESS_DENIED: FailureReason.FILESYSTEM,
}


def environment_failure_message(
    base: str, failure: PipFailure | EnvironmentFailure | None
) -> tuple[FailureReason, str]:
    """The public reason and message for a failed environment creation.

    Only a classified :class:`EnvironmentFailure` adds anything to ``base``,
    and what it adds is its own short summary: the message is what reaches the
    operation's public diagnostic, so nothing of Python's output goes in it.
    """
    if not isinstance(failure, EnvironmentFailure):
        return FailureReason.COMMAND_FAILED, base
    reason = _ENVIRONMENT_FAILURE_REASONS.get(failure.kind, FailureReason.COMMAND_FAILED)
    return reason, f"{base} {failure.summary[:1].upper()}{failure.summary[1:]}."


class EventLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ProvisionEvent:
    """A progress note a caller may render. Text is already sanitized.

    ``activity`` is additive: a plain text note has none, and a caller that
    only understands ``message`` loses nothing. When present, it is the
    typed, bounded semantic observation this note is really about -- the one
    an :class:`~local_ai_stack.operations.OperationStream` can publish as an
    ``OperationActivity`` without re-deriving it from prose.
    """

    component_id: str
    level: EventLevel
    message: str
    activity: OperationActivity | None = None
    # Additive, like ``activity``: a byte-transfer observation alongside the
    # activity it belongs to (pip downloading one package, say), never on its
    # own. A caller that only understands ``activity`` loses nothing.
    transfer: TransferProgress | None = None
    # False: this is current, ongoing work (a coordinator publishes it as the
    # active item, replacing whatever it published before). True: this is a
    # completed or otherwise meaningful milestone (a coordinator appends it to
    # the bounded timeline instead). Meaningless without ``activity``.
    milestone: bool = False


EventSink = Callable[[ProvisionEvent], None]


@dataclass(frozen=True)
class Detection:
    """What was found on disk, and who owns it."""

    component_id: str
    state: ComponentState
    ownership: Ownership = Ownership.NONE
    version: str | None = None
    path: Path | None = None
    detail: str = ""
    evidence: tuple[str, ...] = ()

    @property
    def is_present(self) -> bool:
        return self.state in (ComponentState.INSTALLED, ComponentState.INVALID)

    @property
    def is_usable(self) -> bool:
        return self.state is ComponentState.INSTALLED

    @property
    def is_managed(self) -> bool:
        return self.ownership is Ownership.MANAGED

    @property
    def is_external(self) -> bool:
        return self.ownership is Ownership.EXTERNAL


@dataclass(frozen=True)
class ProvisionStep:
    """One unit of work a provisioner would perform."""

    action: str
    target: str
    summary: str


@dataclass(frozen=True)
class ProvisionPlan:
    component_id: str
    steps: tuple[ProvisionStep, ...]
    detection: Detection

    @property
    def is_noop(self) -> bool:
        return not self.steps


@dataclass(frozen=True)
class ProvisionFailure:
    """Structured failure, so a caller never has to parse a message."""

    reason: FailureReason
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.message}"


@dataclass(frozen=True)
class ProvisionResult:
    component_id: str
    outcome: ProvisionOutcome
    detection: Detection
    steps: tuple[ProvisionStep, ...] = ()
    detail: str = ""
    failure: ProvisionFailure | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (
            ProvisionOutcome.INSTALLED,
            ProvisionOutcome.SKIPPED,
            ProvisionOutcome.DRY_RUN,
        )

    @property
    def changed(self) -> bool:
        return self.outcome is ProvisionOutcome.INSTALLED


class LaunchAvailability(StrEnum):
    """May the runtime layer start this component, and if not, why not?"""

    AVAILABLE = "available"
    EXTERNAL = "external"
    NOT_INSTALLED = "not-installed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RuntimeHandoff:
    """What provisioning tells runtime about one component.

    ``executable`` is set only for AVAILABLE, which in turn happens only for a
    MANAGED install we can prove is ours. Every other availability carries no
    executable, so the runtime layer has nothing to launch and cannot invent
    one. ``detected_path`` is separate and merely observational: it is where
    the component was found, including when it belongs to somebody else.

    ``detail`` says why, in words a UI can show unchanged, and deliberately
    contains no filesystem path: a path may hold the user's account name, and
    rendering one is the presentation layer's job, not this layer's.
    """

    component_id: str
    availability: LaunchAvailability
    ownership: Ownership = Ownership.NONE
    executable: Path | None = None
    detected_path: Path | None = None
    version: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.availability is LaunchAvailability.AVAILABLE:
            if self.ownership is not Ownership.MANAGED:
                raise ValueError("an available handoff must be owned by this manager")
            if self.executable is None:
                raise ValueError("an available handoff must carry the managed executable")
        elif self.executable is not None:
            raise ValueError(
                f"{self.availability} handoff must not carry an executable; "
                "only a managed install may be launched by this manager"
            )
        if (self.availability is LaunchAvailability.EXTERNAL) is not (
            self.ownership is Ownership.EXTERNAL
        ):
            raise ValueError("external availability and ownership must be declared together")

    @property
    def launchable(self) -> bool:
        return self.availability is LaunchAvailability.AVAILABLE

    @property
    def is_external(self) -> bool:
        return self.ownership is Ownership.EXTERNAL


@dataclass
class ProvisionContext:
    """Everything a provisioner is allowed to touch, passed in explicitly."""

    installation: ResolvedInstallation
    runner: CommandRunner = field(default_factory=CommandRunner)
    events: EventSink | None = None
    log: logging.Logger = logger
    # A producer's elapsed-time authority (see docs/OPERATION_CONTRACT.md):
    # a provisioner samples this, never wall-clock timestamps, to report how
    # long a step has been running.
    monotonic: Callable[[], float] = time.monotonic

    @property
    def layout(self) -> StackLayout:
        return self.installation.layout

    def emit(
        self,
        component_id: str,
        message: str,
        level: EventLevel = EventLevel.INFO,
        *,
        activity: OperationActivity | None = None,
        transfer: TransferProgress | None = None,
        milestone: bool = False,
    ) -> None:
        """Publish a progress note. Sanitized before it leaves the backend."""
        event = ProvisionEvent(
            component_id,
            level,
            safe_text(sanitize(message)),
            activity,
            transfer,
            milestone,
        )
        self.log.log(
            {
                EventLevel.INFO: logging.INFO,
                EventLevel.WARNING: logging.WARNING,
                EventLevel.ERROR: logging.ERROR,
            }[level],
            "[%s] %s",
            component_id,
            event.message,
        )
        if self.events is not None:
            self.events(event)


class Provisioner:
    """Base class for component provisioners.

    Subclasses implement `detect`. `plan`, `apply` and `verify` have safe
    defaults: a provisioner that cannot install anything yet still reports
    honestly instead of pretending to work.
    """

    component_id: str = ""
    display_name: str = ""
    supports_apply: bool = False

    def detect(self, context: ProvisionContext) -> Detection:
        raise NotImplementedError

    def verify(self, context: ProvisionContext) -> Detection:
        """Re-detect and confirm usability. Read-only by contract."""
        return self.detect(context)

    def managed_executable(self, context: ProvisionContext) -> Path | None:
        """The executable of *our* install, if this component has one.

        Subclasses that install a program override this. Returning a path is
        not a claim that it exists: `runtime_handoff` decides that against the
        detection, which is the only place ownership is established.
        """
        return None

    def runtime_handoff(self, context: ProvisionContext) -> RuntimeHandoff:
        """Translate the current state into what the runtime may do.

        The mapping is deliberately narrow. Only a MANAGED install that is
        actually usable becomes AVAILABLE, and only then is an executable
        handed over. EXTERNAL is reported as EXTERNAL so a caller can show it
        and refuse to control it; nothing else can ever yield a path.

        The question is put to `verify`, not to `detect`. Detection answers
        "are the files there"; verification answers "does it actually work".
        Handing the runtime an executable for an install whose dependencies
        never finished installing turns a provisioning problem into a launch
        failure, where it is far harder to diagnose. For a provisioner that
        does not override `verify`, this is exactly the old behaviour.
        """
        detection = self.verify(context)
        name = self.display_name or self.component_id
        if detection.is_external:
            return RuntimeHandoff(
                component_id=self.component_id,
                availability=LaunchAvailability.EXTERNAL,
                ownership=Ownership.EXTERNAL,
                detected_path=detection.path,
                version=detection.version,
                detail=(
                    f"{name} is installed outside this hub. "
                    "It is observed only: this manager never starts, stops, replaces "
                    "or uninstalls it."
                ),
            )
        if detection.state is ComponentState.NOT_INSTALLED:
            return RuntimeHandoff(
                component_id=self.component_id,
                availability=LaunchAvailability.NOT_INSTALLED,
                ownership=Ownership.NONE,
                detail=f"{name} is not installed, so there is nothing to start.",
            )
        executable = self.managed_executable(context)
        if not detection.is_managed or not detection.is_usable:
            return RuntimeHandoff(
                component_id=self.component_id,
                availability=LaunchAvailability.UNAVAILABLE,
                ownership=detection.ownership,
                detected_path=detection.path,
                detail=detection.detail or f"{name} is present but not usable.",
            )
        if executable is None or not executable.is_file():
            return RuntimeHandoff(
                component_id=self.component_id,
                availability=LaunchAvailability.UNAVAILABLE,
                ownership=Ownership.MANAGED,
                detected_path=executable,
                detail=f"{name} is registered as managed but its executable is missing.",
            )
        return RuntimeHandoff(
            component_id=self.component_id,
            availability=LaunchAvailability.AVAILABLE,
            ownership=Ownership.MANAGED,
            executable=executable,
            detected_path=executable,
            version=detection.version,
            detail=f"{name} is managed by this hub and can be started.",
        )

    def plan(self, context: ProvisionContext) -> ProvisionPlan:
        detection = self.detect(context)
        if detection.is_usable:
            return ProvisionPlan(self.component_id, (), detection)
        return ProvisionPlan(
            self.component_id,
            (
                ProvisionStep(
                    action="install",
                    target=self.component_id,
                    summary=f"Install {self.display_name or self.component_id}.",
                ),
            ),
            detection,
        )

    def apply(self, context: ProvisionContext, *, dry_run: bool = False) -> ProvisionResult:
        detection = self.detect(context)
        return self.unsupported(
            detection,
            f"{self.display_name or self.component_id} cannot be installed automatically yet.",
        )

    # --- helpers para subclasses ------------------------------------------

    def skipped(self, detection: Detection, detail: str) -> ProvisionResult:
        return ProvisionResult(
            component_id=self.component_id,
            outcome=ProvisionOutcome.SKIPPED,
            detection=detection,
            detail=detail,
        )

    def installed(
        self, detection: Detection, steps: Sequence[ProvisionStep], detail: str = ""
    ) -> ProvisionResult:
        return ProvisionResult(
            component_id=self.component_id,
            outcome=ProvisionOutcome.INSTALLED,
            detection=detection,
            steps=tuple(steps),
            detail=detail,
        )

    def dry_run_result(
        self, detection: Detection, steps: Sequence[ProvisionStep], detail: str = ""
    ) -> ProvisionResult:
        return ProvisionResult(
            component_id=self.component_id,
            outcome=ProvisionOutcome.DRY_RUN,
            detection=detection,
            steps=tuple(steps),
            detail=detail,
        )

    def failed(
        self,
        detection: Detection,
        reason: FailureReason,
        message: str,
        detail: str = "",
    ) -> ProvisionResult:
        return ProvisionResult(
            component_id=self.component_id,
            outcome=ProvisionOutcome.FAILED,
            detection=detection,
            failure=ProvisionFailure(reason, sanitize(message), sanitize(detail)),
        )

    def unsupported(self, detection: Detection, message: str) -> ProvisionResult:
        return self.failed(detection, FailureReason.UNSUPPORTED, message)
