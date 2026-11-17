"""Cooperative controls scoped to one installation, never to an OS process."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

CANCELLED_MESSAGE = (
    "Instalação cancelada. Os arquivos concluídos foram mantidos para reparar ou tentar novamente."
)


class InstallationCancelled(RuntimeError):
    """The current installation reached a cancellation checkpoint."""


class InstallationControl:
    """One request gate shared by the coordinator and its download workers.

    A pause is acknowledged only when every participant is at a checkpoint.
    Condition.wait releases the condition lock; no caller holds the Hub lock
    while waiting here. External commands finish normally before checking in.
    """

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self._condition = threading.Condition()
        self._pause = False
        self._cancel = False
        self._closed = False
        self._participants: dict[int, int] = {}
        self._parked: set[int] = set()
        self._cancel_cleanup: Callable[[], None] | None = None

    def _snapshot(self) -> dict:
        state = "running"
        if self._cancel:
            state = "cancelling"
        elif self._pause:
            state = (
                "paused"
                if self._participants and self._parked == self._participants.keys()
                else "pausing"
            )
        return {
            "operation_id": self.operation_id,
            "state": state,
            "can_pause": not self._closed and state == "running",
            "can_cancel": not self._closed and not self._cancel,
        }

    def snapshot(self) -> dict:
        with self._condition:
            return self._snapshot()

    def request(self, action: str) -> dict:
        with self._condition:
            if self._closed:
                raise ValueError("Esta instalação já terminou.")
            if action not in {"pause", "resume", "cancel"}:
                raise ValueError("Controle de instalação inválido.")
            if self._cancel and action != "cancel":
                raise ValueError("A instalação já está sendo cancelada.")
            if action == "cancel":
                self._cancel = True
                self._pause = False
            else:
                self._pause = action == "pause"
            self._condition.notify_all()
            return self._snapshot()

    @property
    def interruption_requested(self) -> bool:
        """For a provider that must close its stream *before* acknowledging pause."""
        with self._condition:
            return self._pause or self._cancel

    def checkpoint(self, *, stop: threading.Event | None = None) -> None:
        identity = threading.get_ident()
        with self._condition:
            try:
                while (
                    self._pause
                    and not self._cancel
                    and not self._closed
                    and not (stop is not None and stop.is_set())
                ):
                    self._parked.add(identity)
                    self._condition.notify_all()
                    self._condition.wait(timeout=0.1 if stop is not None else None)
                if self._cancel:
                    raise InstallationCancelled(CANCELLED_MESSAGE)
            finally:
                self._parked.discard(identity)

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Bind explicitly in every worker; ContextVars do not cross thread pools."""
        identity = threading.get_ident()
        with self._condition:
            self._participants[identity] = self._participants.get(identity, 0) + 1
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)
            with self._condition:
                depth = self._participants[identity] - 1
                if depth:
                    self._participants[identity] = depth
                else:
                    del self._participants[identity]
                    self._parked.discard(identity)
                self._condition.notify_all()

    def finish(self, *, failed: bool = False) -> bool:
        """Fence the final commit against requests; never wait under a Hub lock."""
        with self._condition:
            if self._cancel:
                raise InstallationCancelled(CANCELLED_MESSAGE)
            if self._pause and not failed:
                return False
            self._closed = True
            self._pause = False
            self._condition.notify_all()
            return True

    def set_cancel_cleanup(self, cleanup: Callable[[], None]) -> None:
        """Register the coordinator's owned-service cleanup on its worker."""
        with self._condition:
            self._cancel_cleanup = cleanup

    def cleanup_cancelled(self) -> None:
        with self._condition:
            cleanup = self._cancel_cleanup
            self._cancel_cleanup = None
        if cleanup is not None:
            with installation_cleanup():
                cleanup()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pause = False
            self._cancel_cleanup = None
            self._condition.notify_all()


_current: ContextVar[InstallationControl | None] = ContextVar("installation_control", default=None)


def current_installation_control() -> InstallationControl | None:
    return _current.get()


def installation_checkpoint() -> None:
    control = _current.get()
    if control is not None:
        control.checkpoint()


@contextmanager
def installation_cleanup() -> Iterator[None]:
    """Allow only lifecycle cleanup to finish after a cancellation checkpoint."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)
