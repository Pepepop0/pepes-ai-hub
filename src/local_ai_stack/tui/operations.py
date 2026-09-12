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
    DETAIL_HISTORY_LIMIT,
    OperationActivity,
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationState,
    OperationTimelineEntry,
    TransferProgress,
)

# Kinds that refine the current step rather than announce a new one. A
# STATE or RESULT event names the step; these only add detail to it.
_REFINING_KINDS = frozenset(
    {OperationEventKind.PROGRESS, OperationEventKind.ACTIVITY, OperationEventKind.DETAIL}
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
    transfer: TransferProgress | None = None
    activity: OperationActivity | None = None
    history: tuple[OperationTimelineEntry, ...] = ()
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

    same_operation = view is not None and view.operation_id == event.operation_id
    steps_before = len(view.steps) if same_operation else 0

    # A progress, activity or detail event refines the current step; it does
    # not add one. The first event seen always names a step, since a replay
    # may start mid-stream.
    if not steps or (event.kind not in _REFINING_KINDS and steps[-1] != event.message):
        steps = (*steps, event.message)
    new_step = len(steps) > steps_before

    diagnostics = _distinct(
        (event.diagnostic,) if event.diagnostic is not None else (),
        event.result.diagnostics if event.result is not None else (),
    )

    failure_result = (
        event.kind is OperationEventKind.RESULT
        and event.state is OperationState.FAILED
        and same_operation
    )
    # Logical progress and transfer bytes are observations *of the current
    # step*: several events can build up the same picture (activity, then
    # transfer, then more activity) without repeating what an earlier one
    # already said, so they carry forward within one step. A genuinely new
    # step starts blank - a later, unrelated step must not keep showing a
    # finished download's bar. Failure is the one exception: its result event
    # names a new "step" of its own, but must still show what was interrupted.
    sticky = (not new_step) or failure_result
    progress = event.progress
    if progress is None and sticky and same_operation:
        progress = view.progress
    transfer = event.transfer
    if transfer is None and sticky and same_operation:
        transfer = view.transfer

    # Current activity is sticky *within one step*, the same as progress and
    # transfer: several observations of the same work accumulate into one
    # picture, but a step that moves on to something else does not keep
    # showing what a much earlier step was doing. Success always clears it -
    # there is nothing "current" once done - and failure keeps whatever was
    # last known, for the same reason progress and transfer do.
    if event.state is OperationState.SUCCEEDED:
        activity = None
    elif event.activity is not None:
        activity = event.activity
    elif sticky and same_operation:
        activity = view.activity
    else:
        activity = None

    history = view.history if same_operation else ()
    if event.detail is not None:
        history = (*history, OperationTimelineEntry(event.sequence, event.phase, event.detail))[
            -DETAIL_HISTORY_LIMIT:
        ]

    fresh = OperationView(
        operation_id=event.operation_id,
        sequence=event.sequence,
        state=event.state,
        phase=event.phase,
        message=event.message,
        steps=steps,
        progress=progress,
        transfer=transfer,
        activity=activity,
        history=history,
        diagnostics=diagnostics,
    )
    if same_operation and view.diagnostics:
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
