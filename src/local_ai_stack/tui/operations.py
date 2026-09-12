"""The consumer's view of one operation, folded from the events it observed.

The application already validated the stream with ``advance_operation``; this
side does not re-run that reducer, because a subscription may begin with a
replayed event whose sequence is greater than one, or which is already
terminal. What the TUI needs is simpler: the latest state, the steps it has
seen in order, any progress within the current step, and the diagnostics that
explain a failure. Everything here is derived from event fields the contract
defines. No stage is invented and no percentage is guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..operation_contracts import (
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationState,
)

# What each lifecycle state reads as on screen. Short, and the same for every
# kind of operation; the steps say what is actually happening.
STATE_HEADLINES: dict[OperationState, str] = {
    OperationState.PREPARING: "Preparando",
    OperationState.QUEUED: "Na fila",
    OperationState.RUNNING: "Em andamento",
    OperationState.FINALIZING: "Finalizando",
    OperationState.SUCCEEDED: "Tudo pronto",
    OperationState.FAILED: "Não foi possível concluir",
}


@dataclass(frozen=True)
class OperationView:
    """Where an operation is, as far as this consumer has seen."""

    operation_id: str
    sequence: int
    state: OperationState
    phase: OperationPhase
    message: str
    steps: tuple[str, ...] = ()
    progress: OperationProgress | None = None
    diagnostics: tuple[OperationDiagnostic, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    @property
    def succeeded(self) -> bool:
        return self.state is OperationState.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.state is OperationState.FAILED

    @property
    def headline(self) -> str:
        return STATE_HEADLINES[self.state]

    @property
    def progress_text(self) -> str:
        """``3/8`` when the total is known, ``3`` when only the count is, else nothing."""
        if self.progress is None:
            return ""
        if self.progress.total is None:
            return str(self.progress.completed)
        return f"{self.progress.completed}/{self.progress.total}"


def observe(view: OperationView | None, event: OperationEvent) -> OperationView | None:
    """Fold one event into the view. Returns the same object when nothing changed.

    A different operation id replaces the view: the application selected a new
    operation. Within one operation, anything at or below the last sequence is
    a duplicate and anything after a terminal event is refused, as the
    contract requires of every consumer.
    """
    if view is not None and event.operation_id == view.operation_id:
        if view.terminal or event.sequence <= view.sequence:
            return view
        steps = view.steps
    else:
        steps = ()

    # A progress event refines the current step; it does not add one. The first
    # event seen always names a step, since a replay may start mid-stream.
    if not steps or (event.kind is not OperationEventKind.PROGRESS and steps[-1] != event.message):
        steps = (*steps, event.message)

    diagnostics = _distinct(
        (event.diagnostic,) if event.diagnostic is not None else (),
        event.result.diagnostics if event.result is not None else (),
    )

    fresh = OperationView(
        operation_id=event.operation_id,
        sequence=event.sequence,
        state=event.state,
        phase=event.phase,
        message=event.message,
        steps=steps,
        progress=event.progress,
        diagnostics=diagnostics,
    )
    if view is not None and view.operation_id == event.operation_id and view.diagnostics:
        # Keep earlier diagnostics a failure path may have reported on the way.
        fresh = replace(fresh, diagnostics=_distinct(view.diagnostics, fresh.diagnostics))
    return fresh


def _distinct(*groups: tuple[OperationDiagnostic, ...]) -> tuple[OperationDiagnostic, ...]:
    """The same diagnostic on the event and in its result is one diagnostic."""
    seen: set[tuple[str, str]] = set()
    kept: list[OperationDiagnostic] = []
    for group in groups:
        for item in group:
            key = (item.code, item.message)
            if key not in seen:
                seen.add(key)
                kept.append(item)
    return tuple(kept)


__all__ = ["STATE_HEADLINES", "OperationView", "observe"]
