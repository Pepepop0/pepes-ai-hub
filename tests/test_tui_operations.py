"""Wave A: the first-run wizard, the Dashboard, and operations as the TUI sees them.

Everything runs against a scripted application. No service starts, nothing is
downloaded, no browser opens: the launcher is a list the test reads back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path

import pytest
from hub_fakes import (
    INSTALL_STEPS,
    SERVICE_IDS,
    FakeHubApplication,
    alongside_conflict,
    conflict_report,
    install_script,
    ready_application,
    unsafe_conflict,
)
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from textual.widgets import Button, Input, Static

from local_ai_stack.app import LocalAIStackApp, OperationObserved, UnwiredHubApplication
from local_ai_stack.components import ComponentId
from local_ai_stack.config import GeneralConfig, InstalledConfig, UiConfig
from local_ai_stack.external_conflict_contracts import (
    ConflictHardBlocker,
    ExternalConflictPolicy,
)
from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.operation_contracts import OperationEventKind, OperationState
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.profiles import offered_profiles
from local_ai_stack.tui.application import (
    COMPATIBLE,
    HubApplication,
    InstallSelection,
    LaunchRefusal,
)
from local_ai_stack.tui.navigation import SECTION_ITEMS, Section
from local_ai_stack.tui.operations import observe
from local_ai_stack.tui.presenters import render_operation
from local_ai_stack.tui.widgets import DashboardPanel, RightRail
from local_ai_stack.tui.wizard import CHOICE_STEPS, WizardScreen, WizardStep

# --- helpers ------------------------------------------------------------------


def run(coroutine: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(coroutine())


def hub(
    application: FakeHubApplication | None = None,
    *,
    first_run_wizard: bool = True,
    installed_config: InstalledConfig | None = None,
) -> tuple[LocalAIStackApp, FakeHubApplication, list[str]]:
    opened: list[str] = []
    fake = application if application is not None else FakeHubApplication()
    app = LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        installed_config=installed_config,
        integration=integration_snapshot(),
        application=fake,
        open_browser=lambda url: opened.append(url) or True,
        first_run_wizard=first_run_wizard,
    )
    return app, fake, opened


async def click(pilot, selector: str) -> None:
    """Press a button now. The press animation would otherwise swallow a quick second click."""
    button = pilot.app.screen.query_one(selector, Button)
    button.active_effect_duration = 0
    button.remove_class("-active")
    await pilot.click(selector)
    await pilot.pause()


async def pump(pilot, fake: FakeHubApplication, count: int | None = None) -> None:
    """Deliver scripted events one by one, letting the screen react between them."""
    delivered = 0
    while count is None or delivered < count:
        if fake.step() is None:
            break
        delivered += 1
        await pilot.pause()


async def settle(pilot) -> None:
    """Let the shell finish its first background refresh and repaint.

    The Hub draws before it asks the application anything; the answer -- and
    the wizard it may open -- arrives from a worker. A test that looks at the
    screen right after mounting waits for that answer here, as a person does.
    """
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def wizard(app: LocalAIStackApp) -> WizardScreen:
    screen = app.screen
    assert isinstance(screen, WizardScreen), type(screen).__name__
    return screen


def text(app: LocalAIStackApp, selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


async def walk_to_review(pilot, *, profile: str = "capella", mode: str = "default") -> None:
    await click(pilot, "#wizard-next")
    await click(pilot, f"#wizard-profile-{profile}")
    await click(pilot, "#wizard-next")
    await click(pilot, f"#wizard-mode-{mode}")
    await click(pilot, "#wizard-next")


# --- the fake honours the protocol ----------------------------------------------


def test_the_fake_is_an_application_and_its_streams_are_valid():
    fake = FakeHubApplication()
    assert isinstance(fake, HubApplication)
    fake.install(InstallSelection("capella"))
    delivered = fake.run_to_end()
    assert [event.sequence for event in delivered] == list(range(1, len(delivered) + 1))
    assert delivered[0].state is OperationState.PREPARING
    assert delivered[-1].kind is OperationEventKind.RESULT
    assert sum(event.kind is OperationEventKind.RESULT for event in delivered) == 1
    assert fake.ready and fake.profile_id == "capella"


# --- first run --------------------------------------------------------------------


def test_opening_without_an_installation_shows_the_wizard():
    async def exercise() -> None:
        app, _, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            assert screen.state.step is WizardStep.WELCOME
            assert "Bem-vindo" in text(app, "#wizard-step-welcome")
            assert "passo 1 de 4" in text(app, "#wizard-title")
            assert "Pepito's AI Hub" in text(app, "#wizard-footer")

    run(exercise)


def test_opening_with_an_installation_goes_straight_to_the_dashboard():
    async def exercise() -> None:
        app, _, _ = hub(ready_application())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, WizardScreen)
            overview = str(app.query_one("#dashboard-overview", Static).content)
            assert "Perfil: [b]Capella[/b]" in overview
            assert overview.count("Em execução") == 3
            assert "Stack: [b]Pronta[/b]" in overview
            assert not app.query_one("#dashboard-open-webui", Button).disabled

    run(exercise)


def test_the_wizard_walks_welcome_profile_mode_review():
    async def exercise() -> None:
        app, _, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            seen = [screen.state.step]
            for _ in range(3):
                await click(pilot, "#wizard-next")
                seen.append(screen.state.step)
            assert seen == list(CHOICE_STEPS)
            assert str(screen.query_one("#wizard-next", Button).label) == "Instalar"

            await click(pilot, "#wizard-back")
            assert screen.state.step is WizardStep.MODE
            await pilot.press("escape")
            await pilot.pause()
            assert screen.state.step is WizardStep.PROFILE

    run(exercise)


def test_the_wizard_can_be_skipped_for_now():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#wizard-skip")
            assert not isinstance(app.screen, WizardScreen)
            assert fake.calls == []
            assert "Nenhuma stack instalada" in str(
                app.query_one("#dashboard-overview", Static).content
            )

    run(exercise)


# --- profile selection ------------------------------------------------------------


def test_the_largest_profile_that_fits_is_preselected_and_the_others_are_offered():
    """No profile is preferred by name: the application says which one fits best."""

    async def exercise() -> None:
        app, _, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            labels = [str(b.label) for b in screen.query("#wizard-profile-buttons Button")]
            assert labels == ["Pepinho", "Pepe", "Capella"]
            assert screen.state.profile_id == "pepe"
            assert screen.query_one("#wizard-profile-pepe", Button).variant == "primary"
            assert "Recomendado" in text(app, "#wizard-profile-summary")

    run(exercise)


def test_choosing_a_profile_updates_the_summary_and_the_selection():
    async def exercise() -> None:
        app, _, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-profile-pepe-8gb")
            assert screen.state.profile_id == "pepe-8gb"
            assert screen.query_one("#wizard-profile-pepe-8gb", Button).variant == "primary"
            assert screen.query_one("#wizard-profile-capella", Button).variant == "default"
            summary = text(app, "#wizard-profile-summary")
            assert "8 GB" in summary and "Compatível" in summary

    run(exercise)


def test_without_a_recommendation_nothing_is_preselected_and_continue_waits():
    async def exercise() -> None:
        fake = FakeHubApplication(
            profile_choices_value=tuple(
                replace(choice, standing=COMPATIBLE)
                for choice in FakeHubApplication().profile_choices()
            )
        )
        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            assert screen.state.profile_id is None
            assert screen.query_one("#wizard-next", Button).disabled
            await click(pilot, "#wizard-profile-pepe")
            assert not screen.query_one("#wizard-next", Button).disabled

    run(exercise)


# --- install mode -------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "portable"])
def test_default_and_portable_need_no_path(mode):
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot, mode=mode)
            assert screen.state.mode is InstallLocationMode(mode)
            assert not screen.query_one("#wizard-custom-root", Input).display
            await click(pilot, "#wizard-next")
            selection = fake.calls[0][1]
            assert selection == InstallSelection("capella", InstallLocationMode(mode))

    run(exercise)


def test_custom_needs_an_absolute_path_and_refuses_a_relative_one():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-mode-custom")
            assert screen.query_one("#wizard-custom-root", Input).display
            assert screen.query_one("#wizard-next", Button).disabled

            screen.set_custom_root("relative\\folder")
            await pilot.pause()
            assert "Caminho inválido" in text(app, "#wizard-custom-error")
            assert screen.query_one("#wizard-next", Button).disabled

            screen.set_custom_root(r"D:\PepitosAIHub")
            await pilot.pause()
            assert text(app, "#wizard-custom-error") == ""
            assert not screen.query_one("#wizard-next", Button).disabled

            await click(pilot, "#wizard-next")
            assert r"D:\PepitosAIHub" in text(app, "#wizard-step-review")
            await click(pilot, "#wizard-next")
            assert fake.calls[0][1] == InstallSelection(
                "pepe", InstallLocationMode.CUSTOM, r"D:\PepitosAIHub"
            )

    run(exercise)


# --- installing -----------------------------------------------------------------------


def test_install_shows_every_stage_the_producer_reports_in_order():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            assert screen.state.step is WizardStep.PROGRESS
            assert fake.calls == [("install", InstallSelection("capella"))]

            seen: list[str] = []
            while (event := fake.step()) is not None:
                await pilot.pause()
                if event.kind is not OperationEventKind.PROGRESS:
                    seen.append(event.message)
                    pane = text(
                        app,
                        "#wizard-step-progress"
                        if not event.state.terminal
                        else "#wizard-step-done",
                    )
                    assert event.message in pane
            assert seen[:7] == list(INSTALL_STEPS)
            assert seen[-1] == "Concluído"

    run(exercise)


def test_progress_within_a_stage_is_shown_as_a_count_not_a_percentage():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 6)  # up to and including the first download tick
            pane = text(app, "#wizard-step-progress")
            assert "Baixando modelos  1/3" in pane
            assert "%" not in pane
            assert "[b]Instalando[/b]" in text(app, "#wizard-title")
            assert "[b]Instalando[/b]" not in pane, "the title already names the step"

    run(exercise)


def test_a_successful_install_ends_on_done_with_open_webui():
    async def exercise() -> None:
        app, fake, opened = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            # The frozen installation heading, not the generic operation headline.
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            assert "[b]Concluído[/b]" not in text(app, "#wizard-step-done"), "shown once"
            visible = [b.id for b in screen.query("#wizard-actions Button") if b.display]
            assert visible == ["wizard-details-toggle", "wizard-open-webui", "wizard-finish"]

            await click(pilot, "#wizard-open-webui")
            assert opened == ["http://127.0.0.1:8080/"]

            await click(pilot, "#wizard-finish")
            assert not isinstance(app.screen, WizardScreen)
            overview = str(app.query_one("#dashboard-overview", Static).content)
            assert "Perfil: [b]Capella[/b]" in overview
            assert overview.count("Em execução") == 3
            assert str(app.query_one("#rail-profile", Static).content) == "Capella"
            assert str(app.query_one("#rail-stack", Static).content) == "Pronta"

    run(exercise)


def test_a_failed_install_shows_a_short_reason_and_offers_a_retry():
    async def exercise() -> None:
        app, fake, _ = hub(FakeHubApplication(install_fail_at="Instalando ComfyUI"))
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            pane = text(app, "#wizard-step-done")
            # The frozen heading: never "Concluído" on a failure.
            assert "[b]Instalação não concluída[/b]" in text(app, "#wizard-title")
            assert "Instalação não concluída" not in pane, "shown once, in the title"
            assert "[b]Concluído[/b]" not in pane
            assert "[!]" in pane and "Instalando ComfyUI" in pane
            assert "Verifique a conexão" in pane
            assert "Código: download-failed" in pane
            assert "Traceback" not in pane and "Exception" not in pane
            visible = [b.id for b in screen.query("#wizard-actions Button") if b.display]
            assert visible == ["wizard-details-toggle", "wizard-retry"]
            assert not fake.ready

            fake.install_fail_at = None
            await click(pilot, "#wizard-retry")
            assert screen.state.step is WizardStep.REVIEW
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            assert len(fake.calls) == 2, "a retry is a new operation, not a reopened one"

    run(exercise)


def test_pressing_install_twice_starts_one_operation():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            button = screen.query_one("#wizard-next", Button)
            button.active_effect_duration = 0
            await pilot.click("#wizard-next")
            await pilot.click("#wizard-next")
            await pilot.pause()
            assert len(fake.calls) == 1
            assert button.disabled

    run(exercise)


def test_a_refused_install_stays_on_review_and_says_why():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            fake.unavailable = True
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            # The refusal lands the wizard back on review with the reason.
            assert "não está disponível" in text(app, "#wizard-step-review")
            assert screen.query_one("#wizard-next", Button).disabled

    run(exercise)


# --- operation events, as the contract defines them ------------------------------------


def test_the_view_reports_only_states_the_contract_defines():
    view = None
    states = []
    for event in install_script("op"):
        view = observe(view, event)
        states.append(view.state)
    assert set(states) <= set(OperationState)
    assert states[0] is OperationState.PREPARING
    assert OperationState.RUNNING in states
    assert OperationState.FINALIZING in states
    assert states[-1] is OperationState.SUCCEEDED
    assert view.terminal and view.succeeded


def test_the_view_refuses_duplicates_regressions_and_post_terminal_events():
    events = install_script("op")
    view = None
    for event in events:
        view = observe(view, event)
    terminal = view
    assert observe(terminal, events[3]) is terminal, "an old sequence changes nothing"
    assert observe(terminal, events[-1]) is terminal, "a repeated result changes nothing"

    partial = observe(observe(None, events[0]), events[1])
    assert observe(partial, events[1]) is partial, "a duplicate is not a new step"
    assert observe(partial, events[0]) is partial, "sequence never goes backwards"


def test_a_new_operation_id_replaces_the_view():
    first = install_script("first")
    view = None
    for event in first:
        view = observe(view, event)
    fresh = observe(view, install_script("second")[0])
    assert fresh.operation_id == "second"
    assert fresh.steps == ("Verificando ambiente",)
    assert not fresh.terminal


def test_a_replay_that_starts_mid_stream_seeds_the_view():
    """A subscription replays the latest event, which may be far from sequence 1."""
    events = install_script("op")
    view = observe(None, events[5])
    assert view.sequence == 6
    assert view.steps == (events[5].message,)


def test_the_terminal_event_ends_the_operation_on_screen():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            assert app.operation_view.terminal
            assert app.operation_view.steps[-1] == "Concluído"
            # Nothing after the terminal event is shown, whatever a producer does.
            late = install_script(app.operation_view.operation_id)[2]
            before = app.operation_view
            app.post_message(OperationObserved(late))
            await pilot.pause()
            assert app.operation_view is before

    run(exercise)


def test_rendering_an_operation_marks_done_current_and_failed_steps():
    view = None
    for event in install_script("op")[:4]:
        view = observe(view, event)
    rendered = render_operation(view)
    assert rendered.index("[x]") < rendered.index("[>]")
    assert rendered.count("[x]") == 3 and rendered.count("[>]") == 1

    failed = None
    for event in install_script("op", fail_at="Instalando Ollama"):
        failed = observe(failed, event)
    rendered = render_operation(failed)
    assert rendered.count("[!]") == 1
    assert "download-failed" in rendered


# --- the dashboard ------------------------------------------------------------------------


def test_the_dashboard_shows_profile_services_gpu_and_one_primary_action():
    async def exercise() -> None:
        app, _, _ = hub(ready_application())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            overview = str(app.query_one("#dashboard-overview", Static).content)
            assert "Perfil: [b]Capella[/b]" in overview
            for name in ("Ollama", "Open WebUI", "ComfyUI"):
                assert name in overview
            assert "NVIDIA GeForce RTX 5070" in overview
            assert "VRAM 2427 / 12227 MiB em uso" in overview
            primary = [str(b.label) for b in app.query("#dashboard-primary Button")]
            secondary = [str(b.label) for b in app.query("#dashboard-secondary Button")]
            assert primary == ["Abrir Open WebUI"]
            assert secondary == ["Iniciar stack", "Reiniciar", "Parar stack"]
            # No cockpit: the overview never names a port, a path or a process.
            for word in ("8080", "PID", "venv", ":\\", "Process"):
                assert word not in overview

    run(exercise)


def test_service_status_changes_are_reflected_after_a_refresh():
    async def exercise() -> None:
        fake = ready_application()
        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            fake.health["comfyui"] = "DOWN"
            app.refresh_projections()
            await pilot.pause()
            overview = str(app.query_one("#dashboard-overview", Static).content)
            assert "Parada" in overview and overview.count("Em execução") == 2
            assert "Parcialmente disponível" in overview
            assert str(app.query_one("#rail-stack", Static).content) == "Parcialmente disponível"

    run(exercise)


@pytest.mark.parametrize(
    ("button", "command", "health"),
    [
        ("dashboard-stop", "stop", "DOWN"),
        ("dashboard-start", "start", "UP"),
        ("dashboard-restart", "restart", "UP"),
    ],
)
def test_stack_commands_ask_the_application_and_follow_the_operation(button, command, health):
    async def exercise() -> None:
        fake = ready_application()
        if command == "start":
            fake.health = dict.fromkeys(fake.health, "DOWN")
        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, f"#{button}")
            assert fake.calls == [(command, None)]
            await pump(pilot, fake, 2)
            panel = app.query_one(DashboardPanel)
            for button_id in ("dashboard-start", "dashboard-stop", "dashboard-restart"):
                assert panel.query_one(f"#{button_id}", Button).disabled, "busy while it runs"
            assert "[b]Em andamento[/b]" in str(
                app.query_one("#dashboard-operation", Static).content
            )
            await pump(pilot, fake)
            # The buttons follow the stack: a stopped one is started, a running
            # one is restarted or stopped.
            settled = "dashboard-restart" if health == "UP" else "dashboard-start"
            assert not panel.query_one(f"#{settled}", Button).disabled
            overview = str(app.query_one("#dashboard-overview", Static).content)
            # Once per service; the line that sums the stack up says "Pronta".
            expected = "Em execução" if health == "UP" else "Parada"
            assert overview.count(expected) == (3 if health == "UP" else 4)

    run(exercise)


def test_a_second_command_while_one_runs_is_refused_not_queued():
    async def exercise() -> None:
        fake = ready_application()
        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-restart")
            await pump(pilot, fake, 1)
            # The button is disabled; ask the app directly, as a keyboard shortcut might.
            app.run_stack_command("stop")
            await pilot.pause()
            assert [name for name, _ in fake.calls] == ["restart"]
            assert "em andamento" in str(app.query_one("#dashboard-notice", Static).content)

    run(exercise)


def test_a_failed_command_is_reported_without_a_traceback():
    async def exercise() -> None:
        fake = ready_application(lifecycle_fails=True)
        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-stop")
            await pump(pilot, fake)
            panel = str(app.query_one("#dashboard-operation", Static).content)
            assert "[b]Não foi possível concluir[/b]" in panel
            assert "Um serviço não respondeu a tempo." in panel
            assert "Traceback" not in panel

    run(exercise)


# --- Open WebUI -----------------------------------------------------------------------------


def test_open_webui_asks_for_a_target_and_never_builds_the_address():
    async def exercise() -> None:
        fake = ready_application()
        app, _, opened = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-open-webui")
            assert fake.calls == [("open_webui_target", None)]
            assert opened == ["http://127.0.0.1:8080/"]
            assert "Abrindo Open WebUI" in str(app.query_one("#dashboard-notice", Static).content)

    run(exercise)


def test_open_webui_with_the_backend_unavailable_says_so():
    async def exercise() -> None:
        fake = ready_application()
        app, _, opened = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            fake.unavailable = True
            await click(pilot, "#dashboard-open-webui")
            assert opened == []
            assert "não está disponível" in str(app.query_one("#dashboard-notice", Static).content)
            assert not app.query_one("#dashboard-open-webui", Button).disabled, "usable again"

    run(exercise)


def test_open_webui_that_is_not_ready_is_refused_with_the_reason():
    async def exercise() -> None:
        fake = ready_application(
            webui=LaunchRefusal("O Open WebUI ainda está iniciando. Tente em instantes.")
        )
        app, _, opened = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-open-webui")
            assert opened == []
            assert "ainda está iniciando" in str(app.query_one("#dashboard-notice", Static).content)

    run(exercise)


def test_a_double_click_opens_the_browser_once():
    async def exercise() -> None:
        fake = ready_application()
        app, _, opened = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            button = app.query_one("#dashboard-open-webui", Button)
            button.active_effect_duration = 0
            await pilot.click("#dashboard-open-webui")
            await pilot.click("#dashboard-open-webui")
            await pilot.pause()
            assert opened == ["http://127.0.0.1:8080/"]
            assert fake.calls.count(("open_webui_target", None)) == 1
            assert button.disabled, "held down for the cooldown"

    run(exercise)


def test_the_browser_refusing_to_open_is_reported_with_the_address():
    async def exercise() -> None:
        fake = ready_application()
        app, _, _ = hub(fake)
        app.open_browser = lambda url: False
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-open-webui")
            notice = str(app.query_one("#dashboard-notice", Static).content)
            assert "não abriu" in notice and "http://127.0.0.1:8080/" in notice

    run(exercise)


# --- right rail -------------------------------------------------------------------------


def test_the_rail_shows_the_pet_for_the_installed_profile():
    async def exercise() -> None:
        app, _, _ = hub(ready_application())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            rail = app.query_one(RightRail)
            assert str(app.query_one("#rail-profile", Static).content) == "Capella"
            assert rail.appearance.resolved.pet_id == "hello-kitty"
            assert "VRAM 2427 / 12227 MiB em uso" in str(app.query_one("#rail-gpu", Static).content)

    run(exercise)


def test_the_test_profile_has_no_pet_and_the_rail_degrades_to_text():
    async def exercise() -> None:
        app, _, _ = hub(ready_application(profile_id="test"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            rail = app.query_one(RightRail)
            assert str(app.query_one("#rail-profile", Static).content) == "Test"
            assert rail.appearance.resolved.pet_id == "none"
            assert rail.appearance.animator is None
            slot = str(app.query_one("#pet-slot", Static).content)
            assert unsafe_characters(slot) == ()

    run(exercise)


def test_a_general_pet_override_wins_over_the_profile():
    async def exercise() -> None:
        config = InstalledConfig(
            profile_id="capella", general=GeneralConfig(ui=UiConfig(pet="claw"))
        )
        app, _, _ = hub(ready_application(), installed_config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.query_one(RightRail).appearance.resolved.pet_id == "claw"

    run(exercise)


def test_a_missing_pet_degrades_safely():
    async def exercise() -> None:
        config = InstalledConfig(
            profile_id="capella", general=GeneralConfig(ui=UiConfig(pet="does-not-exist"))
        )
        app, _, _ = hub(ready_application(), installed_config=config)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            rail = app.query_one(RightRail)
            assert rail.appearance.animator is None
            assert "Pet unavailable" in rail.appearance.pet_text
            assert "Pet unavailable" in str(app.query_one("#pet-slot", Static).content)

    run(exercise)


def test_the_rail_survives_a_resize_around_the_wizard_and_the_dashboard():
    async def exercise() -> None:
        app, _, _ = hub()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await click(pilot, "#wizard-skip")
            rail = app.query_one(RightRail)
            assert not rail.has_class("narrow")
            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            assert rail.has_class("narrow")
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert not rail.has_class("narrow")

    run(exercise)


# --- navigation and settings -----------------------------------------------------------------


def test_the_three_sections_and_their_routes_stay_simple():
    assert [item.label for item in SECTION_ITEMS[Section.DASHBOARD]] == ["Visão geral", "Serviços"]
    assert [item.label for item in SECTION_ITEMS[Section.INSTALLATION]] == [
        "Instalar",
        "Perfil",
        "Local",
        "Plano",
    ]
    assert [item.label for item in SECTION_ITEMS[Section.SETTINGS]] == ["Geral", "Interface"]


def test_settings_show_only_real_options():
    async def exercise() -> None:
        app, _, _ = hub(ready_application())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("3")
            await pilot.pause()
            general = str(app.query_one("#view-settings-general", Static).content)
            assert "Perfil: capella" in general
            assert "Como instalada:" in general
            # Settings describes the installation; changing it lives elsewhere.
            assert "Instalação" in general
            await click(pilot, "#side-settings-interface")
            interface = str(app.query_one("#view-settings-interface", Static).content)
            assert "Tema:" in interface and "Mascote:" in interface
            assert "Barra lateral" in interface
            labels = [str(b.label) for b in app.query("#sidebar-settings Button")]
            assert labels == ["Geral", "Interface"]

    run(exercise)


def test_the_installation_section_reopens_the_wizard():
    async def exercise() -> None:
        app, fake, _ = hub(ready_application())
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press("2")
            await pilot.pause()
            summary = str(app.query_one("#install-summary", Static).content)
            assert "[b]Capella[/b]" in summary
            # Installing again is a new installation, not a reinstall of this one:
            # checking and repairing what is here is its own action.
            assert str(app.query_one("#install-open-wizard", Button).label) == "Nova instalação"
            assert not app.query_one("#install-repair", Button).disabled
            await click(pilot, "#install-open-wizard")
            assert isinstance(app.screen, WizardScreen)
            await click(pilot, "#wizard-skip")
            assert not isinstance(app.screen, WizardScreen)
            assert fake.calls == []

    run(exercise)


def test_no_emoji_anywhere_on_screen():
    async def exercise() -> None:
        app, fake, _ = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            shown = " ".join(str(w.content) for w in app.screen.query(Static))
            await click(pilot, "#wizard-finish")
            shown += " ".join(str(w.content) for w in app.query(Static))
            shown += " ".join(str(b.label) for b in app.query(Button))
            assert unsafe_characters(shown) == ()

    run(exercise)


# --- the unwired composition -----------------------------------------------------------------


def test_the_unwired_application_reads_the_machine_and_refuses_commands():
    unwired = UnwiredHubApplication(reference_facts(), stopped_runtime(), tuple(all_profiles()))
    assert isinstance(unwired, HubApplication)
    assert not unwired.installation().ready
    assert {view.health for view in unwired.services_snapshot().services} == {"DOWN"}
    assert unwired.vram_snapshot().total_mib == 12227
    # Even with nothing wired up, the offer follows the visibility policy.
    assert [choice.id for choice in unwired.profile_choices()] == [
        profile.id for profile in offered_profiles(all_profiles(), env={})
    ]
    with pytest.raises(Exception, match="não está conectada"):
        unwired.install(InstallSelection("capella"))
    refusal = unwired.open_webui_target()
    assert isinstance(refusal, LaunchRefusal)


# --- the boundary -------------------------------------------------------------------------------


def test_the_tui_imports_contracts_never_the_backend():
    """Claude 1's implementation is never reached from here, only the frozen contracts."""
    import ast

    import local_ai_stack

    package = Path(local_ai_stack.__file__).resolve().parent
    sources = [package / "app.py", *sorted((package / "tui").glob("*.py"))]
    forbidden = {"image_generation", "resource_coordination", "comfyui_jobs", "e2e"}
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                head = node.module.split(".")[0]
                if head in forbidden or any(part in forbidden for part in node.module.split(".")):
                    offenders.append(f"{path.name}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(part in forbidden for part in alias.name.split(".")):
                        offenders.append(f"{path.name}: {alias.name}")
    assert offenders == []


def test_the_frozen_contracts_were_not_edited_here():
    """Ownership: those three files belong to Codex and are read, never written, by Wave A."""
    import subprocess

    import local_ai_stack

    package = Path(local_ai_stack.__file__).resolve().parent
    root = package.parents[1]
    if not (root / ".git").exists():
        pytest.skip("not a checkout")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    frozen = {
        "src/local_ai_stack/operation_contracts.py",
        "src/local_ai_stack/image_generation_contracts.py",
        "src/local_ai_stack/application_contracts.py",
        "src/local_ai_stack/external_conflict_contracts.py",
    }
    assert not (frozen & set(changed))


# --- external conflict UX ---------------------------------------------------
#
# Preflight reports a ConflictPreflightReport before an operation exists. The
# wizard resolves it, then submits the preserved selection as a real install.


async def walk_to_conflict(
    pilot, fake: FakeHubApplication, *, profile: str = "capella", mode: str = "default"
) -> None:
    """Reach REVIEW and surface preflight conflict without creating an operation."""
    await walk_to_review(pilot, profile=profile, mode=mode)
    await click(pilot, "#wizard-next")
    assert fake.current_operation() is None
    assert fake.emitted == []


def test_no_conflicts_is_the_ordinary_flow_untouched():
    """The blocker: a machine with nothing else running never sees a conflict step."""

    async def exercise() -> None:
        app, fake, opened = hub()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            await click(pilot, "#wizard-open-webui")
            assert opened == ["http://127.0.0.1:8080/"]

    run(exercise)


def test_an_alongside_conflict_shows_the_choice_screen():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict(), alongside_conflict(ComponentId.COMFYUI))
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            assert screen.state.step is WizardStep.CONFLICT
            pane = text(app, "#wizard-conflict-report")
            assert "Encontramos serviços de IA já em execução." in pane
            assert "Ollama" in pane and "127.0.0.1:11434" in pane
            assert "ComfyUI" in pane and "127.0.0.1:8188" in pane
            # Only useful information: no internals, no technical error codes here.
            assert "preflight-alongside-required" not in pane
            assert "preflight-endpoint-collision" not in pane
            assert "Traceback" not in pane

            choices = text(app, "#wizard-conflict-choices")
            assert "Instalar ao lado" in choices
            assert "Mantém os programas atuais em execução e instala uma cópia isolada" in choices
            assert "Parar conflitos e instalar" in choices
            assert "Encerra somente os serviços externos que impedem a instalação" in choices

            alongside_button = screen.query_one("#wizard-conflict-alongside", Button)
            assert alongside_button.display and alongside_button.variant == "primary"
            assert screen.query_one("#wizard-conflict-stop", Button).display
            assert screen.query_one("#wizard-conflict-abort", Button).display

    run(exercise)


def test_install_alongside_starts_the_first_real_operation_after_preflight():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, opened = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)
            assert app.operation_view is None

            await click(pilot, "#wizard-conflict-alongside")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert len(fake.resolutions) == 1
            assert fake.resolutions[0].policy is ExternalConflictPolicy.INSTALL_ALONGSIDE
            # Only the opaque id and the policy: nothing the resolver could act
            # on beyond that was ever constructed.
            assert set(vars(fake.resolutions[0])) == {"preflight_id", "policy"}

            assert screen.state.step is WizardStep.PROGRESS
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            assert app.operation_view.operation_id == "install-1"
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            assert len(fake.calls) == 2, "preflight attempt plus the accepted install"
            assert {event.operation_id for event in fake.emitted} == {"install-1"}

            await click(pilot, "#wizard-open-webui")
            assert opened == ["http://127.0.0.1:8080/"]

    run(exercise)


def test_stop_requires_confirmation_naming_exactly_what_the_application_can_stop():
    async def exercise() -> None:
        report = conflict_report(
            alongside_conflict(ComponentId.OLLAMA), alongside_conflict(ComponentId.COMFYUI)
        )
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            await click(pilot, "#wizard-conflict-stop")
            assert screen.state.step is WizardStep.CONFLICT_CONFIRM
            assert fake.resolutions == [], "nothing is sent until confirmed"

            confirm_text = text(app, "#wizard-confirm-text")
            assert "O Pepito vai parar temporariamente:" in confirm_text
            assert "- Ollama externo" in confirm_text
            assert "- ComfyUI externo" in confirm_text
            assert "Nenhum arquivo será removido." in confirm_text

            await click(pilot, "#wizard-confirm-back")
            assert screen.state.step is WizardStep.CONFLICT
            assert fake.resolutions == []

    run(exercise)


def test_confirming_stop_sends_only_the_frozen_selection_and_resumes():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)
            await click(pilot, "#wizard-conflict-stop")
            await click(pilot, "#wizard-confirm-stop")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert len(fake.resolutions) == 1
            selection = fake.resolutions[0]
            assert selection.policy is ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            assert selection.preflight_id == report.preflight_id
            assert set(vars(selection)) == {"preflight_id", "policy"}

            assert screen.state.step is WizardStep.PROGRESS
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")

    run(exercise)


def test_an_unsafe_conflict_never_offers_stop_but_alongside_still_works():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict(ComponentId.OLLAMA), unsafe_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            assert not screen.query_one("#wizard-conflict-stop", Button).display
            assert screen.query_one("#wizard-conflict-alongside", Button).display

            pane = text(app, "#wizard-conflict-report")
            assert "Este serviço não pode ser encerrado com segurança pelo Hub." in pane

            await click(pilot, "#wizard-conflict-alongside")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.state.step is WizardStep.PROGRESS
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE

    run(exercise)


def test_abort_returns_to_review_without_installing_anything():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            await click(pilot, "#wizard-conflict-abort")

            assert screen.state.step is WizardStep.REVIEW
            assert len(fake.resolutions) == 1
            assert fake.resolutions[0].policy is ExternalConflictPolicy.ABORT
            assert not fake.ready
            assert fake.health == dict.fromkeys(SERVICE_IDS, "DOWN")
            # The choice was preserved; nothing about the wizard was reset.
            assert screen.state.profile_id == "capella"
            assert screen.state.mode is InstallLocationMode.DEFAULT
            assert "Perfil: [b]Capella[/b]" in text(app, "#wizard-step-review")

    run(exercise)


def test_abort_can_be_followed_by_a_normal_install():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)
            await click(pilot, "#wizard-conflict-abort")

            fake.conflict_report = None
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")
            assert fake.ready

    run(exercise)


def test_a_conflict_that_fails_to_resolve_stays_on_the_conflict_step():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(
            FakeHubApplication(conflict_report=report, conflict_resolution_fails=True)
        )
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            await click(pilot, "#wizard-conflict-alongside")
            await pilot.pause()

            assert screen.state.step is WizardStep.CONFLICT
            assert "Não foi possível aplicar a política escolhida." in text(
                app, "#wizard-conflict-report"
            )
            assert not fake.ready

    run(exercise)


def test_custom_path_stays_correct_through_a_conflict_and_is_not_confused_for_invalid():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-profile-pepe-8gb")
            await click(pilot, "#wizard-next")
            await click(pilot, "#wizard-mode-custom")
            screen.set_custom_root(r"C:\Users\Pedro\Desktop\loira-40k")
            await pilot.pause()
            assert text(app, "#wizard-custom-error") == ""
            await click(pilot, "#wizard-next")

            review = text(app, "#wizard-step-review")
            assert "Perfil: [b]Pepinho[/b]" in review
            assert "Local: [b]Personalizado[/b]" in review
            assert r"Pasta: C:\Users\Pedro\Desktop\loira-40k" in review

            await click(pilot, "#wizard-next")
            await pump(pilot, fake, 1)

            assert screen.state.step is WizardStep.CONFLICT
            # A runtime conflict must never look like the chosen folder was rejected.
            assert text(app, "#wizard-custom-error") == ""
            assert "inválid" not in text(app, "#wizard-conflict-report").lower()

            await click(pilot, "#wizard-conflict-alongside")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pump(pilot, fake)

            assert screen.state.step is WizardStep.DONE
            assert fake.mode is InstallLocationMode.CUSTOM
            assert fake.root_display == r"C:\Users\Pedro\Desktop\loira-40k"

            await click(pilot, "#wizard-finish")
            assert not isinstance(app.screen, WizardScreen)
            install_summary = str(app.query_one("#install-summary", Static).content)
            assert "Personalizada" in install_summary, "the mode, in the screen's words"
            assert r"C:\Users\Pedro\Desktop\loira-40k" in install_summary

    run(exercise)


def test_a_hard_blocker_offers_only_cancel():
    async def exercise() -> None:
        report = conflict_report(
            hard_blockers=(ConflictHardBlocker("space-low", "Espaço em disco insuficiente."),)
        )
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            assert screen.state.step is WizardStep.CONFLICT
            assert not screen.query_one("#wizard-conflict-alongside", Button).display
            assert not screen.query_one("#wizard-conflict-stop", Button).display
            pane = text(app, "#wizard-conflict-report")
            assert "Espaço em disco insuficiente." in pane
            assert "Código: space-low" in pane

            await click(pilot, "#wizard-conflict-abort")
            assert screen.state.step is WizardStep.REVIEW

    run(exercise)


def test_a_pressed_conflict_button_cannot_be_pressed_twice():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)

            button = screen.query_one("#wizard-conflict-alongside", Button)
            button.active_effect_duration = 0
            await pilot.click("#wizard-conflict-alongside")
            await pilot.click("#wizard-conflict-alongside")
            await pilot.pause()

            assert len(fake.resolutions) == 1

    run(exercise)


class _NoConflictSupport:
    """Delegates everything except conflict resolution, which it never had."""

    def __init__(self, inner: FakeHubApplication) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name in ("current_conflict_preflight", "resolve_external_conflicts"):
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_an_application_without_conflict_support_behaves_as_before():
    """An adapter predating this feature has no such method; that is not a conflict."""

    async def exercise() -> None:
        inner = FakeHubApplication()
        fake = _NoConflictSupport(inner)
        assert not hasattr(fake, "current_conflict_preflight")
        assert isinstance(fake, HubApplication)

        app, _, _ = hub(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_review(pilot)
            await click(pilot, "#wizard-next")
            await pump(pilot, fake)
            assert screen.state.step is WizardStep.DONE
            assert "[b]Concluído[/b]" in text(app, "#wizard-title")

    run(exercise)


def test_the_ui_command_carries_no_process_or_endpoint_authority():
    """AST proof: the wizard never references PID, executable, path or arbitrary host/port."""
    import ast

    import local_ai_stack.tui.wizard as wizard_module

    tree = ast.parse(Path(wizard_module.__file__).read_text(encoding="utf-8"))
    forbidden = {"pid", "executable", "subprocess", "psutil", "os"}
    names = {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not (forbidden & names)


def test_zero_emoji_in_the_conflict_screens():
    async def exercise() -> None:
        report = conflict_report(alongside_conflict(), unsafe_conflict())
        app, fake, _ = hub(FakeHubApplication(conflict_report=report))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            screen = wizard(app)
            await walk_to_conflict(pilot, fake)
            shown = " ".join(str(w.content) for w in screen.query(Static))
            shown += " ".join(str(b.label) for b in screen.query(Button))
            await click(pilot, "#wizard-conflict-stop")
            shown += text(app, "#wizard-confirm-text")
            assert unsafe_characters(shown) == ()

    run(exercise)
