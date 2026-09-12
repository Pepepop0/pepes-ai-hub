"""The producer side of the operation contract: one validated stream."""

from __future__ import annotations

from dataclasses import replace

import pytest

from local_ai_stack.operation_contracts import (
    OperationActivity,
    OperationActivityCategory,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationState,
    OperationTimelineEntry,
    TransferProgress,
    advance_operation,
)
from local_ai_stack.operations import (
    ACTIVITY_MIN_INTERVAL_SECONDS,
    OperationClosed,
    OperationStream,
    diagnostic,
    safe_text,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def torch_activity(**changes) -> OperationActivity:
    base = OperationActivity(OperationActivityCategory.DEPENDENCY, "installing", "torch")
    return replace(base, **changes) if changes else base


def test_a_stream_starts_at_sequence_one_preparing():
    stream = OperationStream("op")
    event = stream.begin("Preparando")
    assert (event.sequence, event.state, event.phase) == (
        1,
        OperationState.PREPARING,
        OperationPhase.PREPARATION,
    )


def test_every_event_passes_the_frozen_reducer_before_an_observer_sees_it():
    seen = []
    stream = OperationStream("op", emit=seen.append)
    stream.begin("a")
    stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "b")
    stream.progress(1, 3, "c")
    stream.succeed(OperationPhase.FINALIZATION, "d")
    snapshot = None
    for event in seen:
        snapshot = advance_operation(snapshot, event)
    assert snapshot.last_event.state is OperationState.SUCCEEDED


def test_nothing_can_be_emitted_after_the_result():
    stream = OperationStream("op")
    stream.begin("a")
    stream.fail(OperationPhase.PREPARATION, "b", [diagnostic("x", "why")])
    with pytest.raises(OperationClosed):
        stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "c")
    with pytest.raises(OperationClosed):
        stream.succeed(OperationPhase.FINALIZATION, "d")


def test_a_regression_never_reaches_an_observer():
    seen = []
    stream = OperationStream("op", emit=seen.append)
    stream.begin("a")
    stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "b")
    with pytest.raises(ValueError):
        stream.advance(OperationState.PREPARING, OperationPhase.PREPARATION, "back")
    assert len(seen) == 2
    assert stream.snapshot.last_event.state is OperationState.RUNNING


def test_an_observer_error_is_isolated_and_recorded():
    def broken(_event):
        raise RuntimeError("consumer bug")

    stream = OperationStream("op", emit=broken)
    stream.begin("a")
    result = stream.succeed(OperationPhase.FINALIZATION, "b")
    assert result.state is OperationState.SUCCEEDED
    assert stream.observer_errors and "consumer bug" in stream.observer_errors[0]


def test_a_failure_always_carries_a_diagnostic():
    stream = OperationStream("op")
    stream.begin("a")
    result = stream.fail(OperationPhase.EXECUTION, "b", [])
    assert result.diagnostics and result.diagnostics[0].code == "failure"


def test_terminal_events_are_result_events():
    stream = OperationStream("op")
    stream.begin("a")
    stream.succeed(OperationPhase.FINALIZATION, "b", output_references=("ollama",))
    last = stream.snapshot.last_event
    assert last.kind is OperationEventKind.RESULT
    assert last.result.output_references == ("ollama",)


def test_safe_text_drops_paths_control_characters_and_length():
    raw = "failed at C:" + chr(92) + "Users" + chr(92) + "stack" + chr(92) + "x.exe"
    raw += chr(10) + chr(9) + "with" + chr(7) + " noise " + "y" * 300
    text = safe_text(raw)
    assert "C:" + chr(92) not in text
    assert "<caminho>" in text
    assert chr(10) not in text and chr(7) not in text
    assert len(text) <= 240


def test_safe_text_has_a_fallback_for_nothing():
    assert safe_text("   ") == "sem detalhe"
    assert safe_text(None) == "sem detalhe"


# --- activity / detail -------------------------------------------------------


def test_activity_before_the_first_event_is_refused():
    stream = OperationStream("op")
    with pytest.raises(OperationClosed):
        stream.activity(torch_activity(), "Instalando dependências")


def test_detail_before_the_first_event_is_refused():
    stream = OperationStream("op")
    with pytest.raises(OperationClosed):
        stream.detail(torch_activity(), "torch instalado")


def test_the_first_activity_is_always_published():
    seen = []
    stream = OperationStream("op", emit=seen.append)
    stream.begin("a")

    event = stream.activity(torch_activity(), "Instalando dependências")

    assert event is not None
    assert event.kind is OperationEventKind.ACTIVITY
    assert event.activity == torch_activity()
    assert seen[-1] is event


def test_an_unchanged_activity_with_no_new_observation_is_dropped():
    stream = OperationStream("op")
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências")

    dropped = stream.activity(torch_activity(), "Instalando dependências")

    assert dropped is None
    assert stream.snapshot.last_event.activity == torch_activity()


def test_a_changed_item_is_never_throttled():
    clock = FakeClock()
    stream = OperationStream("op", monotonic=clock)
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências")

    changed = stream.activity(torch_activity(item="numpy"), "Instalando dependências")

    assert changed is not None
    assert changed.activity.item == "numpy"


def test_a_repeated_activity_with_a_fresh_transfer_is_throttled_by_interval():
    clock = FakeClock()
    stream = OperationStream("op", monotonic=clock)
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências", transfer=TransferProgress(10))

    too_soon = stream.activity(
        torch_activity(), "Instalando dependências", transfer=TransferProgress(20)
    )
    assert too_soon is None

    clock.advance(ACTIVITY_MIN_INTERVAL_SECONDS)
    later = stream.activity(
        torch_activity(), "Instalando dependências", transfer=TransferProgress(30)
    )
    assert later is not None
    assert later.transfer.completed_bytes == 30


def test_activity_can_carry_logical_progress_and_transfer_together():
    stream = OperationStream("op")
    stream.begin("a")

    event = stream.activity(
        torch_activity(),
        "Baixando modelo",
        progress=OperationProgress(1, 3),
        transfer=TransferProgress(158 * 1024**2),
    )

    assert event.progress == OperationProgress(1, 3)
    assert event.transfer.completed_bytes == 158 * 1024**2
    assert event.activity == torch_activity()


def test_detail_is_never_throttled_or_deduplicated():
    stream = OperationStream("op")
    stream.begin("a")

    first = stream.detail(torch_activity(action="installed"), "torch instalado")
    second = stream.detail(torch_activity(action="installed"), "torch instalado")

    assert first is not None and second is not None
    assert stream.snapshot.details == (
        OperationTimelineEntry(first.sequence, OperationPhase.PREPARATION, first.detail),
        OperationTimelineEntry(second.sequence, OperationPhase.PREPARATION, second.detail),
    )


def test_activity_and_detail_are_independent_dedupe_tracks():
    stream = OperationStream("op")
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências")

    # A detail milestone for the same semantic activity is not swallowed by
    # the activity dedupe -- it is a distinct kind with distinct meaning.
    detail_event = stream.detail(torch_activity(action="installed"), "torch instalado")

    assert detail_event is not None
    assert detail_event.kind is OperationEventKind.DETAIL


def test_activity_and_detail_cannot_change_state_or_phase():
    stream = OperationStream("op")
    stream.begin("a")
    stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "b")
    # advance() rebuilds the snapshot at RUNNING/EXECUTION; activity()/detail()
    # publish there, inheriting state and phase rather than choosing them.
    event = stream.activity(torch_activity(), "Instalando dependências")
    assert (event.state, event.phase) == (OperationState.RUNNING, OperationPhase.EXECUTION)


def test_a_failed_result_preserves_the_last_activity_on_the_snapshot():
    stream = OperationStream("op")
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências")

    stream.fail(OperationPhase.EXECUTION, "falhou", [diagnostic("x", "why")])

    assert stream.snapshot.activity == torch_activity()


def test_transfer_can_carry_an_activity_in_the_same_event():
    stream = OperationStream("op")
    stream.begin("a")

    event = stream.transfer(
        TransferProgress(10, 20), "Baixando modelo", activity=torch_activity(item="flux2_klein")
    )

    assert event.transfer.completed_bytes == 10
    assert event.activity.item == "flux2_klein"
    assert stream.snapshot.activity.item == "flux2_klein"


def test_transfer_is_never_throttled_even_with_a_repeated_activity_and_no_elapsed_time():
    """A regression: transfer must not be silently dropped by activity's dedupe.

    A TransferSampler already decides when a sample is worth publishing --
    including guaranteeing the very first and the very last are never
    skipped. If transfer() routed through activity()'s own throttle, two
    samples for the same model arriving within the same instant (as they do
    in a fast synchronous download, real or faked) would collapse into one,
    silently losing the final, complete byte count.
    """
    clock = FakeClock()
    stream = OperationStream("op", monotonic=clock)
    stream.begin("a")
    same_activity = torch_activity(category=OperationActivityCategory.MODEL, action="downloading")

    first = stream.transfer(TransferProgress(8, 47), "Baixando modelo", activity=same_activity)
    # No time advances between these two calls, unlike activity()'s own dedupe
    # window -- transfer() must still publish both.
    second = stream.transfer(TransferProgress(47, 47), "Baixando modelo", activity=same_activity)

    assert first is not None and second is not None
    assert second.transfer.completed_bytes == 47


def test_a_succeeded_result_clears_the_last_activity_on_the_snapshot():
    stream = OperationStream("op")
    stream.begin("a")
    stream.activity(torch_activity(), "Instalando dependências")

    stream.succeed(OperationPhase.FINALIZATION, "pronto")

    assert stream.snapshot.activity is None
