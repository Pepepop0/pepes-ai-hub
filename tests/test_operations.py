"""The producer side of the operation contract: one validated stream."""

from __future__ import annotations

import pytest

from local_ai_stack.operation_contracts import (
    OperationEventKind,
    OperationPhase,
    OperationState,
    advance_operation,
)
from local_ai_stack.operations import OperationClosed, OperationStream, diagnostic, safe_text


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
