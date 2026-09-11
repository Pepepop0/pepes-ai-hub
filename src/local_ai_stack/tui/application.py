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
from typing import Protocol, runtime_checkable

from ..application_contracts import ApplicationReadModel
from ..installation import InstallLocationMode


class HubError(RuntimeError):
    """The application could not honour a request. Safe to show, never a traceback."""


class HubUnavailable(HubError):
    """The application layer is not reachable at all."""


class OperationBusy(HubError):
    """Another operation is still running; the request was not started."""


@dataclass(frozen=True)
class ProfileChoice:
    """One profile as the wizard presents it: a name and a one-line reason to pick it."""

    id: str
    name: str
    summary: str
    compatible: bool | None = None


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
class InstallationView:
    """Whether there is a stack to show, and which one. ``root_display`` is masked text."""

    ready: bool
    profile_id: str | None = None
    mode: InstallLocationMode | None = None
    root_display: str | None = None


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


@runtime_checkable
class HubApplication(ApplicationReadModel, Protocol):
    """The read model plus the five things a Hub screen can ask for.

    Every command that changes the machine returns an operation id and is then
    observed only through :meth:`subscribe`, so the TUI never has a second
    channel for outcomes. Commands raise :class:`OperationBusy` rather than
    queueing when one is already running, and :class:`HubUnavailable` when the
    application cannot be reached.
    """

    def installation(self) -> InstallationView: ...

    def profile_choices(self) -> tuple[ProfileChoice, ...]: ...

    def install(self, selection: InstallSelection) -> str: ...

    def start_stack(self) -> str: ...

    def stop_stack(self) -> str: ...

    def restart_stack(self) -> str: ...

    def open_webui_target(self) -> LaunchTarget | LaunchRefusal: ...


__all__ = [
    "HubApplication",
    "HubError",
    "HubUnavailable",
    "InstallSelection",
    "InstallationView",
    "LaunchRefusal",
    "LaunchTarget",
    "OperationBusy",
    "ProfileChoice",
]
