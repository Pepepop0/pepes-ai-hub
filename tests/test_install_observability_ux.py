"""Install observability UX: activity, transfer and history as one granular view.

Two layers, same as the download-progress round. Pure functions (folding
events into an ``OperationView``, then rendering it) need no Textual at all
and cover every scenario the spec calls out. A handful of wizard-integration
tests drive the real ``WizardScreen`` through Textual's pilot to prove the
details toggle and the resize-aware layout actually reach the screen.

Nothing here reads a process or stdout: every scenario is built from typed
``OperationActivity``/``OperationTimelineEntry`` values the way a sanitizing
adapter would hand them to the contract, per docs/OPERATION_CONTRACT.md.
"""

from __future__ import annotations

import pytest
from hub_fakes import FakeHubApplication, activity_event, detail_event, transfer_event
from test_download_progress_ux import state_event
from test_tui_operations import click, hub, pump, run, settle, text, walk_to_review
from textual.widgets import Button, Static

from local_ai_stack.operation_contracts import (
    DETAIL_HISTORY_LIMIT,
    OperationActivity,
    OperationActivityCategory,
    OperationDiagnostic,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationState,
    OperationTimelineEntry,
    TransferProgress,
)
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.tui.operations import observe
from local_ai_stack.tui.presenters import (
    activity_headline,
    format_elapsed,
    render_activity,
    render_install_progress,
    render_timeline,
)
from local_ai_stack.tui.wizard import WizardStep

MiB = 1024**2
GiB = 1024**3


def fold(*events):
    """Every event, in order, through the consumer's own reducer."""
    view = None
    for event in events:
        view = observe(view, event)
    return view


# --- activity: turning a producer action into a Portuguese sentence -----------


def test_activity_without_transfer_shows_headline_and_item():
    activity = OperationActivity(OperationActivityCategory.DEPENDENCY, "installing", "torch")
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando ComfyUI"),
        activity_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            activity,
        ),
    )
    rendered = render_install_progress(view)
    assert "Instalando dependências" in rendered
    assert "torch" in rendered
    assert view.transfer is None
    assert "MiB" not in rendered and "B/s" not in rendered


def test_activity_with_logical_progress_shows_step_counter_and_activity():
    activity = OperationActivity(OperationActivityCategory.MODEL, "downloading", "FLUX.2 Klein")
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            progress=OperationProgress(3, 11),
        ),
        activity_event(
            "op", 3, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos", activity
        ),
    )
    rendered = render_install_progress(view)
    assert "Etapa 3 de 11 — Baixando modelos" in rendered
    # The activity's own headline and item, distinct from the checklist step
    # text ("Baixando modelos") that merely shares a prefix with it.
    assert "Baixando modelo\nFLUX.2 Klein" in rendered


def test_activity_with_transfer_shows_both_channels_never_one_replacing_the_other():
    activity = OperationActivity(OperationActivityCategory.MODEL, "downloading", "FLUX.2 Klein")
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Baixando modelos",
            progress=OperationProgress(2, 4),
        ),
        activity_event(
            "op", 3, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos", activity
        ),
        transfer_event(
            "op",
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
    )
    rendered = render_install_progress(view)
    # Logical progress, the activity headline and the byte transfer all
    # appear together: none of the three channels stands in for another.
    assert "Baixando modelos  2/4" in rendered
    assert "Baixando modelo\nFLUX.2 Klein" in rendered
    assert "68%" in rendered
    assert "2.77 GiB / 4.07 GiB" in rendered
    assert "24.8 MiB/s" in rendered
    assert rendered.index("2/4") < rendered.index("68%")


def test_current_item_is_its_own_line_for_multi_item_categories():
    for category, noun in (
        (OperationActivityCategory.DEPENDENCY, "dependências"),
        (OperationActivityCategory.MODEL, "modelo"),
        (OperationActivityCategory.WORKFLOW, "workflow"),
    ):
        activity = OperationActivity(category, "installing", "some-item")
        headline, item = activity_headline(activity)
        assert noun in headline
        assert item == "some-item"


def test_readiness_activity_reads_as_waiting_for_the_service_to_respond():
    activity = OperationActivity(OperationActivityCategory.READINESS, "waiting", "ComfyUI")
    headline, item = activity_headline(activity)
    assert headline == "Aguardando ComfyUI responder"
    assert item == ""


def test_transfer_category_activity_uses_the_item_as_its_own_headline():
    activity = OperationActivity(OperationActivityCategory.TRANSFER, "downloading", "FLUX.2 Klein")
    headline, item = activity_headline(activity)
    assert headline == "FLUX.2 Klein"
    assert item == ""


def test_an_unknown_action_still_reads_as_something_sensible():
    activity = OperationActivity(OperationActivityCategory.SERVICE, "restarting", "Ollama")
    headline, _ = activity_headline(activity)
    assert headline == "Restarting Ollama"


# --- elapsed: no milliseconds, ever --------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00"),
        (18, "00:18"),
        (161, "02:41"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3852, "1:04:12"),
    ],
)
def test_elapsed_formats_without_milliseconds(seconds, expected):
    assert format_elapsed(seconds) == expected


def test_elapsed_is_the_last_line_after_any_transfer_bytes_or_speed():
    activity = OperationActivity(
        OperationActivityCategory.DEPENDENCY, "installing", "torch", elapsed_seconds=18
    )
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando ComfyUI"),
        activity_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            activity,
        ),
        transfer_event(
            "op",
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "torch",
            TransferProgress(int(158 * MiB), None, int(22.4 * MiB)),
        ),
    )
    rendered = render_install_progress(view)
    assert rendered.index("22.4 MiB/s") < rendered.index("Tempo: 00:18")


# --- details toggle and bounded history ----------------------------------------


def _timeline_view():
    activity = OperationActivity(OperationActivityCategory.DEPENDENCY, "installing", "torch")
    installed = OperationActivity(OperationActivityCategory.DEPENDENCY, "installed", "numpy")
    return fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando ComfyUI"),
        detail_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            installed,
        ),
        activity_event(
            "op",
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            activity,
        ),
    )


def test_details_collapsed_by_default_shows_no_timeline():
    rendered = render_install_progress(_timeline_view())
    assert "Detalhes" not in rendered


def test_details_expanded_shows_the_history_checklist_and_the_current_item():
    rendered = render_install_progress(_timeline_view(), expanded=True)
    assert "Detalhes" in rendered
    assert "[x]" in rendered
    assert "Instalado" in rendered and "numpy" in rendered
    assert "[>]" in rendered
    assert "Instalando dependências" in rendered and "torch" in rendered


def test_render_timeline_never_invents_a_pending_future_step():
    history = (
        OperationTimelineEntry(
            1,
            OperationPhase.EXECUTION,
            OperationActivity(OperationActivityCategory.ENVIRONMENT, "creating", "venv"),
        ),
    )
    lines = render_timeline(history)
    # Only what was observed: no synthesized "[ ]" line for a step nobody
    # reported yet.
    assert "[ ]" not in "\n".join(lines)


def test_timeline_with_no_history_yet_says_so_instead_of_nothing():
    lines = render_timeline(())
    assert "Nenhum detalhe registrado" in "\n".join(lines)


def test_bounded_history_keeps_only_the_newest_entries():
    events = [
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando dependências"),
    ]
    for index in range(25):
        events.append(
            detail_event(
                "op",
                3 + index,
                OperationState.RUNNING,
                OperationPhase.EXECUTION,
                "Instalando dependências",
                OperationActivity(
                    OperationActivityCategory.DEPENDENCY, "installed", f"package-{index}"
                ),
            )
        )
    view = fold(*events)
    assert len(view.history) == DETAIL_HISTORY_LIMIT
    # The oldest five are gone; the newest is still there.
    assert not any(entry.detail.item == "package-0" for entry in view.history)
    assert view.history[-1].detail.item == "package-24"


# --- transfer: known and unknown totals, unchanged rules -----------------------


def test_known_total_transfer_still_shows_bar_percentage_bytes_speed_eta():
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos"),
        transfer_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            TransferProgress(int(2.77 * GiB), int(4.07 * GiB), int(24.8 * MiB)),
        ),
    )
    rendered = render_install_progress(view)
    assert "68%" in rendered
    assert "restantes" in rendered
    assert unsafe_characters(rendered) == ()


def test_unknown_total_transfer_never_gets_a_bar_percentage_or_eta():
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos"),
        transfer_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "FLUX.2 Klein",
            TransferProgress(int(1.73 * GiB), None, int(24.8 * MiB)),
        ),
    )
    rendered = render_install_progress(view)
    assert "1.73 GiB transferidos" in rendered
    assert "24.8 MiB/s" in rendered
    for forbidden in ("%", "restantes", "[#", "[-"):
        assert forbidden not in rendered.replace("\\[", "[")


# --- success and failure --------------------------------------------------------


def test_success_clears_activity_and_shows_a_short_summary_no_lingering_spinner():
    activity = OperationActivity(OperationActivityCategory.SERVICE, "starting", "ComfyUI")
    installed = OperationActivity(OperationActivityCategory.MODEL, "installed", "FLUX.2 Klein")
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Iniciando ComfyUI"),
        activity_event(
            "op", 3, OperationState.RUNNING, OperationPhase.EXECUTION, "Iniciando ComfyUI", activity
        ),
        detail_event(
            "op",
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Iniciando ComfyUI",
            installed,
        ),
        state_event(
            5,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Tudo pronto",
            kind=OperationEventKind.RESULT,
            result=OperationResult("op", OperationState.SUCCEEDED),
        ),
    )
    assert view.activity is None
    rendered = render_install_progress(view, heading="Concluído")
    assert "Concluído" in rendered
    assert "Tudo pronto" in rendered
    # No lingering "in progress" marker once it is actually done.
    assert "[>]" not in rendered
    assert "Instalado" in rendered and "FLUX.2 Klein" in rendered
    assert "Etapa" not in rendered


def test_failure_shows_last_known_activity_and_progress_with_the_reason():
    activity = OperationActivity(OperationActivityCategory.DEPENDENCY, "installing", "torch")
    diagnostic = OperationDiagnostic(
        "download-failed", "O download não foi concluído. Verifique a conexão."
    )
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(
            2,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            progress=OperationProgress(3, 11),
        ),
        activity_event(
            "op",
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            activity,
        ),
        transfer_event(
            "op",
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "torch",
            TransferProgress(int(50 * MiB), int(200 * MiB), int(10 * MiB)),
        ),
        state_event(
            5,
            OperationState.FAILED,
            OperationPhase.EXECUTION,
            "Não foi possível concluir",
            kind=OperationEventKind.RESULT,
            diagnostic=diagnostic,
            result=OperationResult("op", OperationState.FAILED, diagnostics=(diagnostic,)),
        ),
    )
    rendered = render_install_progress(view, heading="Instalação não concluída")
    assert "Instalação não concluída" in rendered
    assert "Última atividade:" in rendered
    assert "Instalando dependências" in rendered and "torch" in rendered
    assert "25%" in rendered  # last known progress, not invented
    assert "Motivo:" in rendered
    assert "Verifique a conexão" in rendered
    assert "Traceback" not in rendered and "Exception" not in rendered
    # A terminal event's own message is not a step name: no misleading
    # "Etapa N de M" pairing it with the failure sentence.
    assert "Etapa" not in rendered

    # A screen that already titles the outcome renders the same view without
    # it: the heading appears once, and nothing else about the failure is lost.
    body = render_install_progress(view, heading="Instalação não concluída", include_heading=False)
    assert "Instalação não concluída" not in body
    assert not body.startswith("\n")
    assert "[!]" in body and "Última atividade:" in body and "Motivo:" in body
    assert body == rendered.split("\n", 2)[2], "only the heading and its blank line differ"


def test_the_wizard_shows_the_failure_heading_exactly_once():
    """Regression: title and pane both printed "Instalação não concluída"."""

    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(install_fail_at="Instalando ComfyUI"))
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert app.screen.state.step is WizardStep.DONE
            screen_text = text(app, "#wizard-title") + "\n" + text(app, "#wizard-step-done")
            assert screen_text.count("Instalação não concluída") == 1
            assert "Última atividade:" in screen_text or "[!]" in screen_text

    run(exercise)


def test_legacy_event_without_activity_or_detail_still_folds_cleanly():
    """An event built the old way (no activity/detail kwargs at all) is valid input."""
    view = fold(
        state_event(
            1, OperationState.PREPARING, OperationPhase.PREPARATION, "Verificando ambiente"
        ),
        state_event(2, OperationState.RUNNING, OperationPhase.EXECUTION, "Instalando Ollama"),
        state_event(
            3,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Concluído",
            kind=OperationEventKind.RESULT,
            result=OperationResult("op", OperationState.SUCCEEDED),
        ),
    )
    assert view.activity is None
    assert view.history == ()
    assert view.succeeded
    rendered = render_install_progress(view, heading="Concluído")
    assert unsafe_characters(rendered) == ()


# --- no raw stdout, zero emoji anywhere ------------------------------------------


def test_activity_and_timeline_rendering_have_zero_emoji():
    activity = OperationActivity(
        OperationActivityCategory.MODEL, "downloading", "FLUX.2 Klein", detail="camada 3 de 5"
    )
    assert unsafe_characters("\n".join(render_activity(activity))) == ()
    history = (OperationTimelineEntry(1, OperationPhase.EXECUTION, activity),)
    assert unsafe_characters("\n".join(render_timeline(history, current=activity))) == ()


def test_rendering_only_ever_shows_the_sanitized_fields_it_was_given():
    """No stdout, no pip-style log lines: only what the typed activity carries.

    ``render_activity``/``render_timeline`` take an ``OperationActivity``, not
    a string of process output - there is no parameter through which a raw
    log line could arrive. This pins the observable contract: the rendered
    text is exactly the headline/item/detail this activity carries, nothing
    scraped or inferred from anywhere else.
    """
    activity = OperationActivity(
        OperationActivityCategory.DEPENDENCY,
        "installing",
        "torch",
        detail="reutilizando cache local",
    )
    rendered = "\n".join(render_activity(activity))
    assert rendered == "Instalando dependências\ntorch\n[dim]reutilizando cache local[/dim]"
    for forbidden in ("Collecting", "Downloading torch-", "$ pip", "==>", "Traceback"):
        assert forbidden not in rendered


# --- wizard integration: the details toggle and layout at real widths ----------


def observability_script(operation_id: str) -> list:
    activity = OperationActivity(
        OperationActivityCategory.DEPENDENCY, "installing", "torch", elapsed_seconds=18
    )
    installed = OperationActivity(OperationActivityCategory.DEPENDENCY, "installed", "numpy")
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
            "Instalando ComfyUI",
            operation_id=operation_id,
            progress=OperationProgress(3, 11),
        ),
        detail_event(
            operation_id,
            3,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            installed,
        ),
        activity_event(
            operation_id,
            4,
            OperationState.RUNNING,
            OperationPhase.EXECUTION,
            "Instalando ComfyUI",
            activity,
            transfer=TransferProgress(int(158 * MiB), None, int(22.4 * MiB)),
        ),
        state_event(
            5,
            OperationState.SUCCEEDED,
            OperationPhase.FINALIZATION,
            "Tudo pronto",
            operation_id=operation_id,
            kind=OperationEventKind.RESULT,
            result=OperationResult(operation_id, OperationState.SUCCEEDED),
        ),
    ]


def test_the_wizard_shows_activity_and_transfer_together_and_the_toggle_reveals_history():
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=observability_script))
        async with app.run_test(size=(110, 36)) as pilot:
            await settle(pilot)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 4)

            pane = text(app, "#wizard-step-progress")
            assert "Etapa 3 de 11 — Instalando ComfyUI" in pane
            assert "Instalando dependências" in pane and "torch" in pane
            assert "158 MiB transferidos" in pane
            assert "22.4 MiB/s" in pane
            assert "Tempo: 00:18" in pane
            assert "Detalhes" not in pane

            toggle = app.screen.query_one("#wizard-details-toggle", Button)
            assert toggle.display and str(toggle.label) == "Mostrar detalhes"
            await click(pilot, "#wizard-details-toggle")
            assert str(toggle.label) == "Ocultar detalhes"
            expanded = text(app, "#wizard-step-progress")
            assert "Detalhes" in expanded
            assert "Instalado" in expanded and "numpy" in expanded

            await click(pilot, "#wizard-details-toggle")
            assert str(toggle.label) == "Mostrar detalhes"
            assert "Detalhes" not in text(app, "#wizard-step-progress")

            await pump(pilot, fake)
            assert app.screen.state.step is WizardStep.DONE

    run(exercise)


@pytest.mark.parametrize("width", [120, 100, 80, 70])
def test_the_granular_view_fits_at_several_terminal_widths(width):
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(custom_script=observability_script))
        async with app.run_test(size=(width, 32)) as pilot:
            await settle(pilot)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 4)

            pane_widget = app.screen.query_one("#wizard-step-progress", Static)
            rendered_lines = str(pane_widget.content).splitlines()
            assert rendered_lines, "the pane must not go blank at a narrow width"
            for line in rendered_lines:
                visible = line.replace("\\[", "[").replace("[dim]", "").replace("[/dim]", "")
                assert len(visible) <= pane_widget.size.width + 10, (
                    f"line overflowed a {width}-column pane: {visible!r}"
                )

    run(exercise)
