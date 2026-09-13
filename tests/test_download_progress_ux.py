"""Download progress in the wizard: bar, byte sizes, speed, ETA.

Two layers are tested. Pure functions (formatting, the bar, folding a
TransferProgress into an OperationView) need no Textual at all. The wizard
integration drives the real WizardScreen through Textual's pilot with a fake
application whose install script is replaced with hand-built events, proving
the bar actually reaches the screen and reacts to a resize.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hub_fakes import FakeHubApplication
from test_tui_operations import click, hub, pump, run, settle, text, walk_to_review, wizard
from textual.widgets import Static

from local_ai_stack.operation_contracts import (
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationState,
    TransferProgress,
)
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.tui.operations import observe
from local_ai_stack.tui.presenters import (
    bar_width_for,
    format_bytes,
    format_eta,
    format_percentage,
    format_speed,
    render_bar,
    render_operation,
)
from local_ai_stack.tui.wizard import WizardStep

GiB = 1024**3
MiB = 1024**2

EPOCH = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)


def stamp(offset: float = 0.0) -> datetime:
    from datetime import timedelta

    return EPOCH + timedelta(seconds=offset)


def state_event(
    sequence: int,
    state: OperationState,
    phase: OperationPhase,
    message: str,
    *,
    operation_id: str = "op",
    kind: OperationEventKind = OperationEventKind.STATE,
    progress: OperationProgress | None = None,
    transfer: TransferProgress | None = None,
    diagnostic: OperationDiagnostic | None = None,
    result: OperationResult | None = None,
) -> OperationEvent:
    return OperationEvent(
        operation_id=operation_id,
        sequence=sequence,
        kind=kind,
        state=state,
        timestamp=stamp(sequence),
        phase=phase,
        message=message,
        progress=progress,
        transfer=transfer,
        diagnostic=diagnostic,
        result=result,
    )


# --- byte formatting ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1 KiB"),
        (int(812 * MiB), "812 MiB"),
        (int(1.27 * GiB), "1.27 GiB"),
        (int(2.77 * GiB), "2.77 GiB"),
        (int(4.07 * GiB), "4.07 GiB"),
        (int(24.8 * MiB), "24.8 MiB"),
    ],
)
def test_format_bytes_matches_the_documented_examples(value, expected):
    assert format_bytes(value) == expected


def test_format_bytes_never_shows_a_fake_trailing_zero():
    assert format_bytes(1024) == "1 KiB"
    assert format_bytes(2 * GiB) == "2 GiB"


def test_format_bytes_promotes_across_a_rounding_boundary():
    """1023.999 MiB must not render as the impossible '1024.00 MiB'."""
    almost_one_gib = int(1023.999 * MiB)
    result = format_bytes(almost_one_gib)
    assert "1024" not in result
    assert result.endswith("GiB")


def test_format_speed_appends_per_second():
    assert format_speed(24.8 * MiB) == "24.8 MiB/s"
    assert format_speed(0) == "0 B/s"


# --- percentage -------------------------------------------------------------------


def test_percentage_only_from_a_known_positive_total():
    assert format_percentage(0.684) == 68
    assert format_percentage(0.0) == 0
    assert format_percentage(1.0) == 100


def test_percentage_is_clamped_to_0_100():
    assert format_percentage(-0.2) == 0
    assert format_percentage(1.4) == 100


# --- ETA --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "~0 s restantes"),
        (42, "~42 s restantes"),
        (53, "~53 s restantes"),
        (59.4, "~1m 00s restantes"),
        (60, "~1m 00s restantes"),
        (133, "~2m 13s restantes"),
        (3600, "~1h 00m restantes"),
        (3600 + 4 * 60, "~1h 04m restantes"),
    ],
)
def test_format_eta_matches_the_documented_shapes(seconds, expected):
    assert format_eta(seconds) == expected


def test_eta_rounds_up_rather_than_down():
    """A person should never see '0 s' while a byte is still left to transfer."""
    assert format_eta(0.1) == "~1 s restantes"


def test_no_eta_when_total_is_unknown_or_speed_is_zero_or_absent():
    assert TransferProgress(100, None, 10).eta_seconds is None
    assert TransferProgress(100, 400, 0).eta_seconds is None
    assert TransferProgress(100, 400, None).eta_seconds is None
    assert TransferProgress(100, 400, 10).eta_seconds is not None


# --- the bar ------------------------------------------------------------------------


def test_the_bar_is_ascii_only():
    bar = render_bar(0.5, 20)
    assert bar == "[" + "#" * 10 + "-" * 10 + "]"
    assert unsafe_characters(bar) == ()
    assert all(ord(char) < 128 for char in bar)


def test_the_bar_clamps_visually_at_the_edges():
    assert render_bar(0.0, 10) == "[" + "-" * 10 + "]"
    assert render_bar(1.0, 10) == "[" + "#" * 10 + "]"
    assert render_bar(-0.3, 10) == "[" + "-" * 10 + "]"
    assert render_bar(1.9, 10) == "[" + "#" * 10 + "]"


def test_the_bar_shrinks_with_the_available_width_but_never_below_a_floor():
    widths = [bar_width_for(w) for w in (120, 80, 70, 40, 20, 10, 1)]
    assert widths == sorted(widths, reverse=True) or widths[-1] >= 10
    assert all(width >= 10 for width in widths)
    assert bar_width_for(120) <= 40, "a bar this wide would look absurd on a wide terminal"


# --- folding a TransferProgress into the view ----------------------------------------


def test_a_progress_event_with_only_transfer_is_valid_and_folds_in():
    view = observe(
        None,
        state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            kind=OperationEventKind.PROGRESS,
            transfer=TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
    )
    assert view.transfer is not None
    assert view.transfer.completed_bytes == int(2.77 * GiB)


def test_an_event_without_the_transfer_field_behaves_exactly_as_before():
    """An old-style event built without the new kwarg must not break anything."""
    old_style = OperationEvent(
        operation_id="op",
        sequence=1,
        kind=OperationEventKind.STATE,
        state=OperationState.PREPARING,
        timestamp=stamp(),
        phase=OperationPhase.PREPARATION,
        message="Verificando ambiente",
    )
    view = observe(None, old_style)
    assert view.transfer is None
    assert render_operation(view)  # renders without raising


def test_transfer_is_not_retained_once_a_later_event_omits_it():
    """Each event is a complete observation; a later step without transfer clears it."""
    view = observe(
        None,
        state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            kind=OperationEventKind.PROGRESS,
            transfer=TransferProgress(100, 400, 10),
        ),
    )
    assert view.transfer is not None
    view = observe(
        view,
        state_event(3, OperationState.RUNNING, OperationPhase.FINALIZATION, "Configurando"),
    )
    assert view.transfer is None


def test_a_successful_terminal_event_does_not_keep_the_bar_visually_active():
    view = observe(
        None,
        state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, "modelo"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "modelo",
            kind=OperationEventKind.PROGRESS,
            transfer=TransferProgress(400, 400, 10),
        ),
    )
    result = OperationResult("op", OperationState.SUCCEEDED)
    view = observe(
        view,
        state_event(
            3,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Concluído",
            kind=OperationEventKind.RESULT,
            result=result,
        ),
    )
    assert view.transfer is None
    assert "[#" not in render_operation(view)


def test_a_failed_terminal_event_preserves_the_last_observed_transfer():
    view = observe(
        None,
        state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, "modelo"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "modelo",
            kind=OperationEventKind.PROGRESS,
            transfer=TransferProgress(300, 1000, 10),
        ),
    )
    diagnostic = OperationDiagnostic("net-lost", "A conexão foi perdida.")
    result = OperationResult("op", OperationState.FAILED, diagnostics=(diagnostic,))
    view = observe(
        view,
        state_event(
            3,
            OperationState.FAILED,
            OperationPhase.EXECUTION,
            "Não foi possível concluir",
            kind=OperationEventKind.RESULT,
            diagnostic=diagnostic,
            result=result,
        ),
    )
    assert view.transfer == TransferProgress(300, 1000, 10)
    rendered = render_operation(view, heading="Instalação não concluída")
    assert "300 B / 1000 B" in rendered
    assert "[b]Instalação não concluída[/b]" in rendered
    assert "[b]Concluído[/b]" not in rendered


# --- rendering combinations ------------------------------------------------------------


def render_with_transfer(
    transfer: TransferProgress,
    *,
    width: int = 60,
    message: str = "item",
    step: str = "Baixando modelos",
) -> str:
    view = observe(None, state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, step))
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            message,
            kind=OperationEventKind.PROGRESS,
            transfer=transfer,
        ),
    )
    return render_operation(view, width=width)


def test_zero_percent_shows_an_empty_bar():
    rendered = render_with_transfer(TransferProgress(0, 1000, None))
    assert "0%" in rendered
    assert "[----" in rendered.replace("\\[", "[")


def test_intermediate_progress_shows_a_partially_filled_bar():
    rendered = render_with_transfer(
        TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)), message="FLUX.2 Klein"
    )
    assert "68%" in rendered
    assert "2.77 GiB / 4.07 GiB" in rendered
    assert "24.8 MiB/s" in rendered
    assert "restantes" in rendered
    assert "FLUX.2 Klein" in rendered


def test_one_hundred_percent_shows_a_full_bar():
    rendered = render_with_transfer(TransferProgress(1000, 1000, None))
    assert "100%" in rendered


def test_unknown_total_is_indeterminate_never_a_fake_percentage():
    rendered = render_with_transfer(TransferProgress(int(1.73 * GiB), None, int(24.8 * MiB)))
    assert "1.73 GiB transferidos" in rendered
    assert "24.8 MiB/s" in rendered
    for forbidden in ("0%", "100%", "?%", "[#", "[-"):
        assert forbidden not in rendered.replace("\\[", "[")


def test_unknown_total_without_speed_shows_no_placeholder():
    rendered = render_with_transfer(TransferProgress(int(1.73 * GiB), None, None))
    assert "1.73 GiB transferidos" in rendered
    assert "/s" not in rendered


def test_speed_present_and_absent():
    with_speed = render_with_transfer(TransferProgress(500, 1000, int(10 * MiB)))
    without_speed = render_with_transfer(TransferProgress(500, 1000, None))
    assert "MiB/s" in with_speed
    assert "/s" not in without_speed


def test_zero_speed_is_shown_but_never_produces_an_eta():
    rendered = render_with_transfer(TransferProgress(100, 400, 0))
    assert "0 B/s" in rendered
    assert "restantes" not in rendered


def test_eta_appears_only_with_total_and_positive_speed():
    with_eta = render_with_transfer(
        TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB))
    )
    assert "restantes" in with_eta
    no_total = render_with_transfer(TransferProgress(int(2.77 * GiB), None, int(24.8 * MiB)))
    assert "restantes" not in no_total
    no_speed = render_with_transfer(TransferProgress(int(2.77 * GiB), int(4.07 * GiB), None))
    assert "restantes" not in no_speed


def test_non_download_steps_are_unaffected_by_transfer_support():
    """A step with only logical progress renders exactly as it always did."""
    view = observe(
        None,
        state_event(1, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando ComfyUI"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            kind=OperationEventKind.PROGRESS,
            progress=OperationProgress(1, 3),
        ),
    )
    rendered = render_operation(view)
    assert "Instalando ComfyUI  1/3" in rendered
    assert "%" not in rendered
    assert "[#" not in rendered.replace("\\[", "[")


def test_logical_and_transfer_progress_are_shown_together_not_substituted():
    view = observe(
        None,
        state_event(1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando"),
    )
    view = observe(
        view,
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            progress=OperationProgress(2, 4),
        ),
    )
    view = observe(
        view,
        state_event(
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            kind=OperationEventKind.PROGRESS,
            progress=OperationProgress(2, 4),
            transfer=TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
    )
    rendered = render_operation(view)
    assert "Baixando modelos  2/4" in rendered
    assert "FLUX.2 Klein" in rendered
    assert "68%" in rendered
    assert "2.77 GiB / 4.07 GiB" in rendered
    # The logical count is never replaced by the byte percentage or vice versa.
    assert rendered.index("2/4") < rendered.index("68%")


def test_a_message_identical_to_the_step_is_not_repeated_as_a_label():
    rendered = render_with_transfer(
        TransferProgress(500, 1000, int(10 * MiB)),
        step="Baixando modelos",
        message="Baixando modelos",
    )
    assert rendered.count("Baixando modelos") == 1


def test_zero_emoji_anywhere_in_transfer_output():
    rendered = render_with_transfer(
        TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)), message="FLUX.2 Klein"
    )
    assert unsafe_characters(rendered) == ()


# --- wizard integration -----------------------------------------------------------------


def download_script(operation_id: str) -> list[OperationEvent]:
    return [
        state_event(
            1,
            OperationState.PREPARING,
            OperationPhase.PREPARATION,
            "Verificando ambiente",
            operation_id=operation_id,
        ),
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            operation_id=operation_id,
            progress=OperationProgress(2, 4),
        ),
        state_event(
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            operation_id=operation_id,
            kind=OperationEventKind.PROGRESS,
            progress=OperationProgress(2, 4),
            transfer=TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
        state_event(
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            operation_id=operation_id,
            kind=OperationEventKind.PROGRESS,
            progress=OperationProgress(2, 4),
            transfer=TransferProgress(int(4.07 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
        state_event(
            5,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Concluído",
            operation_id=operation_id,
            kind=OperationEventKind.RESULT,
            result=OperationResult(operation_id, OperationState.SUCCEEDED),
        ),
    ]


def failing_download_script(operation_id: str) -> list[OperationEvent]:
    diagnostic = OperationDiagnostic("connection-lost", "A conexão foi perdida durante o download.")
    return [
        state_event(
            1,
            OperationState.PREPARING,
            OperationPhase.PREPARATION,
            "Verificando ambiente",
            operation_id=operation_id,
        ),
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            operation_id=operation_id,
        ),
        state_event(
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            operation_id=operation_id,
            kind=OperationEventKind.PROGRESS,
            transfer=TransferProgress(int(1.5 * GiB), int(4.07 * GiB), int(12 * MiB)),
        ),
        state_event(
            4,
            OperationState.FAILED,
            OperationPhase.EXECUTION,
            "Não foi possível concluir",
            operation_id=operation_id,
            kind=OperationEventKind.RESULT,
            diagnostic=diagnostic,
            result=OperationResult(operation_id, OperationState.FAILED, diagnostics=(diagnostic,)),
        ),
    ]


def test_the_wizard_shows_the_bar_percentage_bytes_speed_and_eta_live():
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=download_script))
        async with app.run_test(size=(110, 36)) as pilot:
            await settle(pilot)
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 3)

            pane = text(app, "#wizard-step-progress")
            assert "Baixando modelos  2/4" in pane
            assert "FLUX.2 Klein" in pane
            assert "68%" in pane
            assert "2.77 GiB / 4.07 GiB" in pane
            assert "24.8 MiB/s" in pane
            assert "restantes" in pane
            assert unsafe_characters(pane) == ()

            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            done_pane = text(app, "#wizard-step-done")
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            assert "[b]Concluído[/b]" not in done_pane, "shown once, in the title"
            assert "[#" not in done_pane.replace("\\[", "[")

    run(exercise)


def test_a_download_that_fails_keeps_the_last_progress_with_the_failure_heading():
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=failing_download_script))
        async with app.run_test(size=(110, 36)) as pilot:
            await settle(pilot)
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            done_pane = text(app, "#wizard-step-done")
            assert "[b]Instalação não concluída[/b]" in text(app, "#wizard-title")
            assert "Instalação não concluída" not in done_pane, "shown once, in the title"
            assert "[b]Concluído[/b]" not in done_pane
            assert "1.5" in done_pane and "GiB" in done_pane
            assert "A conexão foi perdida durante o download." in done_pane

    run(exercise)


@pytest.mark.parametrize("width", [120, 80, 70])
def test_the_bar_fits_at_several_terminal_widths_without_overflowing(width):
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=download_script))
        async with app.run_test(size=(width, 32)) as pilot:
            await settle(pilot)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 3)

            pane_widget = app.screen.query_one("#wizard-step-progress", Static)
            rendered_lines = str(pane_widget.content).splitlines()
            bar_line = next(line for line in rendered_lines if "%" in line and "[" in line)
            visible = bar_line.replace("\\[", "[").replace("[dim]", "").replace("[/dim]", "")
            assert len(visible) <= pane_widget.size.width + 10, (
                f"bar line overflowed a {width}-column pane: {visible!r}"
            )

    run(exercise)


def test_resizing_live_redraws_the_bar_at_the_new_width():
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=download_script))
        async with app.run_test(size=(120, 36)) as pilot:
            await settle(pilot)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 3)

            wide_pane = str(app.screen.query_one("#wizard-step-progress", Static).content)

            # Narrow enough that the bar's own floor/ceiling clamp actually
            # produces a different length, not just a different pane width.
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            narrow_pane = str(app.screen.query_one("#wizard-step-progress", Static).content)

            assert wide_pane != narrow_pane
            assert "68%" in narrow_pane

            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            restored_pane = str(app.screen.query_one("#wizard-step-progress", Static).content)
            assert "68%" in restored_pane
            assert restored_pane == wide_pane

    run(exercise)
