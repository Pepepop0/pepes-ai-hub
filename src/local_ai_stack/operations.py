"""One validated operation stream, and the safe text that goes into it.

The frozen contract (:mod:`operation_contracts`) is a reducer: it says whether
an event may follow the previous one. This module is the producer side of that
bargain. Every event a coordinator emits goes through ``advance_operation``
*before* any observer sees it, so a gap, a regression or an event after the
terminal result is impossible to publish rather than merely discouraged.

Two more obligations from the contract live here because every producer needs
them and none should re-decide them:

- **text is bounded and printable.** Domain details are useful and often long;
  an event message is a single safe line. ``safe_text`` collapses whitespace,
  drops control characters, strips absolute paths and cuts to the contract's
  limit. It bounds shape; it does not claim to redact secrets from prose.
- **observers cannot change the outcome.** An observer that raises is
  isolated: the snapshot was already advanced, the terminal result is still
  returned, and the failure is recorded on the stream for a log to pick up.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .operation_contracts import (
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationEventSink,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationSnapshot,
    OperationState,
    TransferProgress,
    advance_operation,
)

MESSAGE_LIMIT = 240
MAX_DIAGNOSTICS = 8

# Absolute paths are the most common thing a domain detail carries that an
# event must not: they name the machine. Windows drives, UNC shares and POSIX
# roots are all replaced by a marker so the rest of the sentence still reads.
_PATH = re.compile(r"(?:[A-Za-z]:\\|\\\\|/(?:home|Users|tmp|var|opt|mnt)/)[^\s'\"]*")
_SPACE = re.compile(r"\s+")


def safe_text(text: object, *, limit: int = MESSAGE_LIMIT, fallback: str = "sem detalhe") -> str:
    """A single printable line, bounded, with absolute paths removed."""
    raw = str(text) if text is not None else ""
    raw = _PATH.sub("<caminho>", raw)
    raw = "".join(ch for ch in raw if ch.isprintable() or ch in " \t\r\n")
    raw = _SPACE.sub(" ", raw).strip()
    if not raw:
        raw = fallback
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    return raw


def diagnostic(code: str, message: object, *, reference: str | None = None) -> OperationDiagnostic:
    """A typed diagnostic with a safe message."""
    return OperationDiagnostic(code=code, message=safe_text(message), reference=reference)


def utc_now() -> datetime:
    return datetime.now(UTC)


class OperationClosed(RuntimeError):
    """An event was attempted after the terminal result."""


@dataclass
class OperationStream:
    """The producer of one operation's events, validated on the way out.

    ``snapshot`` is the authoritative view the application stores; observers
    are told afterwards. ``result`` is set exactly once, by ``succeed`` or
    ``fail``; after that every attempt to emit raises ``OperationClosed``.
    """

    operation_id: str
    emit: OperationEventSink | None = None
    clock: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    snapshot: OperationSnapshot | None = None
    result: OperationResult | None = None
    observer_errors: list[str] = field(default_factory=list)
    _started: float | None = field(default=None, repr=False)
    _sequence: int = field(default=0, repr=False)

    # --- estado ------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self.result is not None

    @property
    def state(self) -> OperationState | None:
        return None if self.snapshot is None else self.snapshot.last_event.state

    @property
    def phase(self) -> OperationPhase | None:
        return None if self.snapshot is None else self.snapshot.last_event.phase

    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return max(0.0, self.monotonic() - self._started)

    # --- emissão -----------------------------------------------------------

    def begin(self, message: str = "Preparando") -> OperationEvent:
        """The mandatory first event: STATE / PREPARING / PREPARATION."""
        self._started = self.monotonic()
        return self.advance(OperationState.PREPARING, OperationPhase.PREPARATION, message)

    def advance(
        self,
        state: OperationState,
        phase: OperationPhase,
        message: str,
        *,
        diagnostic: OperationDiagnostic | None = None,
    ) -> OperationEvent:
        """A non-terminal STATE event."""
        if state.terminal:
            raise ValueError("use succeed() or fail() for a terminal state")
        return self._publish(
            kind=OperationEventKind.STATE,
            state=state,
            phase=phase,
            message=message,
            diagnostic=diagnostic,
        )

    def progress(self, completed: int, total: int | None, message: str) -> OperationEvent:
        """Work units inside the current state and phase."""
        if self.snapshot is None:
            raise OperationClosed("progress before the first event")
        last = self.snapshot.last_event
        return self._publish(
            kind=OperationEventKind.PROGRESS,
            state=last.state,
            phase=last.phase,
            message=message,
            progress=OperationProgress(completed=completed, total=total),
        )

    def transfer(
        self,
        transfer: TransferProgress,
        message: str,
        *,
        progress: OperationProgress | None = None,
    ) -> OperationEvent:
        """Byte-transfer observation inside the current state and phase.

        Independent of :meth:`progress`: a coordinator downloading one model
        while reporting "step 2 of 5" logical progress emits both, and a
        consumer that only understands one of them still sees a valid event.
        """
        if self.snapshot is None:
            raise OperationClosed("progress before the first event")
        last = self.snapshot.last_event
        return self._publish(
            kind=OperationEventKind.PROGRESS,
            state=last.state,
            phase=last.phase,
            message=message,
            progress=progress,
            transfer=transfer,
        )

    def succeed(
        self,
        phase: OperationPhase,
        message: str,
        *,
        output_references: Iterable[str] = (),
        diagnostics: Iterable[OperationDiagnostic] = (),
    ) -> OperationResult:
        return self._finish(
            OperationState.SUCCEEDED,
            phase,
            message,
            output_references=tuple(output_references),
            diagnostics=tuple(diagnostics)[:MAX_DIAGNOSTICS],
        )

    def fail(
        self,
        phase: OperationPhase,
        message: str,
        diagnostics: Iterable[OperationDiagnostic],
    ) -> OperationResult:
        items = tuple(diagnostics)[:MAX_DIAGNOSTICS]
        if not items:
            items = (diagnostic("failure", message),)
        return self._finish(OperationState.FAILED, phase, message, diagnostics=items)

    # --- internos ----------------------------------------------------------

    def _finish(
        self,
        state: OperationState,
        phase: OperationPhase,
        message: str,
        *,
        output_references: tuple[str, ...] = (),
        diagnostics: tuple[OperationDiagnostic, ...] = (),
    ) -> OperationResult:
        result = OperationResult(
            operation_id=self.operation_id,
            state=state,
            output_references=output_references,
            diagnostics=diagnostics,
            duration_seconds=self.elapsed(),
        )
        self._publish(
            kind=OperationEventKind.RESULT,
            state=state,
            phase=phase,
            message=message,
            diagnostic=diagnostics[0] if diagnostics else None,
            result=result,
        )
        self.result = result
        return result

    def _publish(
        self,
        *,
        kind: OperationEventKind,
        state: OperationState,
        phase: OperationPhase,
        message: str,
        progress: OperationProgress | None = None,
        transfer: TransferProgress | None = None,
        diagnostic: OperationDiagnostic | None = None,
        result: OperationResult | None = None,
    ) -> OperationEvent:
        if self.closed:
            raise OperationClosed(f"operation {self.operation_id} already has a result")
        event = OperationEvent(
            operation_id=self.operation_id,
            sequence=self._sequence + 1,
            kind=kind,
            state=state,
            timestamp=self.clock(),
            phase=phase,
            message=safe_text(message),
            progress=progress,
            transfer=transfer,
            diagnostic=diagnostic,
            result=result,
        )
        # The reducer is the authority: an invalid transition never reaches an
        # observer, and never changes the snapshot.
        self.snapshot = advance_operation(self.snapshot, event)
        self._sequence = event.sequence
        if self.emit is not None:
            try:
                self.emit(event)
            except Exception as exc:  # noqa: BLE001 - observers cannot change the outcome
                self.observer_errors.append(f"{type(exc).__name__}: {safe_text(exc)}")
        return event


__all__ = [
    "MAX_DIAGNOSTICS",
    "MESSAGE_LIMIT",
    "OperationClosed",
    "OperationStream",
    "diagnostic",
    "safe_text",
    "utc_now",
]
