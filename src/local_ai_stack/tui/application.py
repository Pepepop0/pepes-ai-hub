"""What the TUI asks of the application layer, and nothing more.

The frozen contracts describe how an operation is observed
(:mod:`local_ai_stack.application_contracts`). They do not yet say how one is
asked for. This module is the consumer's side of that conversation: the
handful of commands the Hub screens need, expressed as capability requests
over projections, so the TUI can be built against a fake today and against
the application tomorrow without either side learning the other's internals.

Nothing here carries a path the TUI could open, a port it could compose into a
URL, or a process it could signal. The Open WebUI button asks for a
:class:`LaunchTarget` and receives one or a refusal; it never builds
``localhost:8080`` itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..application_contracts import ApplicationReadModel
from ..external_conflict_contracts import ConflictPreflightReport
from ..installation import InstallLocationMode


class HubError(RuntimeError):
    """The application could not honour a request. Safe to show, never a traceback."""


class HubUnavailable(HubError):
    """The application layer is not reachable at all."""


class OperationBusy(HubError):
    """Another operation is still running; the request was not started."""


class ConflictResolutionRequired(HubError):
    """Preflight needs a policy choice; no installation operation exists yet."""

    def __init__(self, report: ConflictPreflightReport) -> None:
        super().__init__("Serviços externos conflitantes foram encontrados.")
        self.report = report


class SwitchRequiresStop(HubError):
    """The installation in use is running; switching needs that stopped first.

    Carries the plan so the screen can name both installations in the question
    it asks. Nothing has been stopped or changed when this is raised.
    """

    def __init__(self, plan: SwitchPlan) -> None:
        super().__init__("A instalação atual ainda está em execução.")
        self.plan = plan


# How a profile stands against the machine it would be installed on.
RECOMMENDED = "recommended"
COMPATIBLE = "compatible"
INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class ProfileChoice:
    """One profile as the wizard presents it: a name and a one-line reason to pick it.

    ``standing`` is what the detected hardware says: the largest profile this
    machine can run is recommended, the smaller ones that also fit are
    compatible, and the ones that ask for more VRAM than there is say so
    instead of being offered as if they were a choice.
    """

    id: str
    name: str
    summary: str
    compatible: bool | None = None
    standing: str | None = None
    required_vram_gb: int | float | None = None

    @property
    def recommended(self) -> bool:
        return self.standing == RECOMMENDED


@dataclass(frozen=True)
class InstallSelection:
    """Everything the wizard collected. A custom root is text the application resolves."""

    profile_id: str
    mode: InstallLocationMode = InstallLocationMode.DEFAULT
    custom_root: str | None = None

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("a profile is required")
        if self.mode is InstallLocationMode.CUSTOM and not (self.custom_root or "").strip():
            raise ValueError("a custom installation needs a root")
        if self.mode is not InstallLocationMode.CUSTOM and self.custom_root:
            raise ValueError("only a custom installation names a root")


@dataclass(frozen=True)
class InstallationSummary:
    """One installation as a list row: what it is, where it is, and how it stands.

    ``status`` is observed, never stored: an installation whose folder is gone
    reads ``missing`` and keeps its place in the list, because the way back is
    to find the folder, not to forget it happened.
    """

    root: Path
    profile_id: str | None
    profile_name: str | None
    mode: InstallLocationMode
    status: str = "unknown"  # ready | stopped | running | missing | unreadable | unknown
    active: bool = False
    last_used_at: datetime | None = None
    detail: str = ""

    @property
    def missing(self) -> bool:
        return self.status == "missing"

    @property
    def usable(self) -> bool:
        return self.status in {"ready", "stopped", "running"}


@dataclass(frozen=True)
class StorageEstimate:
    """What an installation is known to download, and what the disk has for it.

    ``complete`` is False when something the profile needs was pinned without a
    recorded size, and the screen then says "at least". ``fits`` is None when
    nothing can be said -- an unknown download, an unreadable volume -- and the
    screen says nothing rather than guessing.
    """

    known_bytes: int = 0
    complete: bool = True
    free_bytes: int | None = None
    fits: bool | None = None


@dataclass(frozen=True)
class SwitchPlan:
    """What switching to another installation would do, before anything is done."""

    root: Path
    profile_id: str | None
    mode: InstallLocationMode
    must_stop: bool = False
    running_profile: str | None = None
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class InstallationView:
    """Whether there is a stack to show, and which one. ``root_display`` is masked text.

    ``checking`` is the view the shell shows before the application has
    answered: not "nothing installed", just "not known yet".
    """

    ready: bool
    profile_id: str | None = None
    mode: InstallLocationMode | None = None
    root_display: str | None = None
    checking: bool = False


@dataclass(frozen=True)
class LaunchTarget:
    """Where a browser should go, decided by the application from its own endpoints."""

    service_id: str
    url: str
    label: str


@dataclass(frozen=True)
class LaunchRefusal:
    """Why there is nothing to open right now, in words a person can act on."""

    reason: str
    code: str = "not-ready"


@dataclass(frozen=True)
class InstallationCheck:
    """What a read-only check of the installation in use found.

    ``problems`` are things a repair would fix; ``notes`` are things the check
    could not look at right now -- models behind a stopped Ollama -- which are
    not problems, only unknowns.
    """

    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


class FolderPicker(Protocol):
    """Asks the operating system for a folder. The TUI never sees how.

    Blocking: the implementation shows a modal dialog and returns when it
    closes, so the app calls it from a worker thread. ``initial_path`` is a
    hint the dialog may open on; ``None`` back means the person cancelled or no
    dialog exists on this platform - either way, nothing should change.
    """

    def choose_folder(self, initial_path: Path | None = None) -> Path | None: ...


@runtime_checkable
class HubApplication(ApplicationReadModel, Protocol):
    """The read model plus the five things a Hub screen can ask for.

    Every command that changes the machine returns an operation id and is then
    observed only through :meth:`subscribe`, so the TUI never has a second
    channel for outcomes. Commands raise :class:`OperationBusy` rather than
    queueing when one is already running, and :class:`HubUnavailable` when the
    application cannot be reached.

    An implementation may additionally satisfy
    :class:`~local_ai_stack.external_conflict_contracts.ExternalConflictResolver`
    (``current_conflict_preflight`` / ``resolve_external_conflicts``). It is
    deliberately not required here: this Protocol is used in ``isinstance``
    checks against real application adapters, some of which predate conflict
    resolution. The TUI calls those two methods defensively - see
    ``LocalAIStackApp._check_conflict`` - so an adapter without them behaves
    exactly as if no conflict were ever reported.

    ``recent_custom_roots() -> tuple[str, ...]`` follows the same rule for the
    same reason: optional, called defensively, and an adapter without it simply
    has no history to offer. So do ``last_switch_refusal() -> str | None`` (why
    the last stop-and-switch did not switch) and ``cancel_pending_installation()``
    (let go of an installation paused on a conflict decision).
    """

    def installation(self) -> InstallationView: ...

    def profile_choices(self) -> tuple[ProfileChoice, ...]: ...

    def install(self, selection: InstallSelection) -> str: ...

    def start_stack(self) -> str: ...

    def stop_stack(self) -> str: ...

    def restart_stack(self) -> str: ...

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal: ...


__all__ = [
    "ConflictResolutionRequired",
    "FolderPicker",
    "HubApplication",
    "HubError",
    "HubUnavailable",
    "InstallSelection",
    "InstallationCheck",
    "InstallationSummary",
    "InstallationView",
    "LaunchRefusal",
    "LaunchTarget",
    "OperationBusy",
    "ProfileChoice",
    "StorageEstimate",
    "SwitchPlan",
    "SwitchRequiresStop",
]
