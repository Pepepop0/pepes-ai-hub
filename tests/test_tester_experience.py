"""What a tester meets: the review before installing, leaving, and small terminals.

Three things the audit found and this covers: an installation announced as
"a few minutes" when it downloads tens of gigabytes, a Hub that disappears
while an installation is still running, and a window too small to reach the
buttons that matter.
"""

from __future__ import annotations

import asyncio

import pytest
from hub_fakes import FakeHubApplication, ready_application
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_installation_screen import installed
from test_tui_operations import click, pump
from textual.widgets import Button, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.tui.application import StorageEstimate
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import DashboardPanel
from local_ai_stack.tui.wizard import WizardScreen, WizardStep

GIB = 1024**3


def hub(application: FakeHubApplication, *, first_run_wizard: bool = False):
    app = LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=first_run_wizard,
    )
    return app


def run(coroutine) -> None:
    asyncio.run(coroutine())


def text_of(app, selector: str) -> str:
    """Read a widget on whatever screen is in front, wizard included."""
    return str(app.screen.query_one(selector, Static).content)


async def walk_to_review(pilot) -> WizardScreen:
    await pilot.pause()
    screen = pilot.app.screen
    assert isinstance(screen, WizardScreen)
    for _ in range(3):
        await click(pilot, "#wizard-next")
    assert screen.state.step is WizardStep.REVIEW
    for _ in range(4):
        await pilot.pause()
    return screen


# --- what an installation will actually cost ------------------------------------


def test_the_review_says_what_will_be_downloaded_and_what_the_disk_has():
    async def exercise() -> None:
        fake = FakeHubApplication()
        fake.storage = StorageEstimate(17 * GIB, True, 84 * GIB, True)
        app = hub(fake, first_run_wizard=True)
        async with app.run_test(size=(110, 36)) as pilot:
            await walk_to_review(pilot)
            review = text_of(app, "#wizard-step-review")
            assert "Downloads conhecidos" in review and "17 GiB" in review
            assert "Espaço livre" in review and "84 GiB" in review
            assert "12 GB de VRAM" in review, "the profile's own requirement"
            # Not "a few minutes" for tens of gigabytes.
            assert "alguns minutos" not in review
            assert "pode levar bastante tempo" in review
            assert ("storage", ("pepe", app.screen.state.mode, None)) in fake.managed

    run(exercise)


def test_an_unknown_size_is_said_as_at_least_and_never_invented():
    async def exercise() -> None:
        fake = FakeHubApplication()
        fake.storage = StorageEstimate(12 * GIB, False, 84 * GIB, True)
        app = hub(fake, first_run_wizard=True)
        async with app.run_test(size=(110, 36)) as pilot:
            await walk_to_review(pilot)
            review = text_of(app, "#wizard-step-review")
            assert "pelo menos" in review and "12 GiB" in review

    run(exercise)


def test_a_disk_that_cannot_hold_the_download_says_so_on_the_review():
    async def exercise() -> None:
        fake = FakeHubApplication()
        fake.storage = StorageEstimate(30 * GIB, True, 4 * GIB, False)
        app = hub(fake, first_run_wizard=True)
        async with app.run_test(size=(110, 36)) as pilot:
            await walk_to_review(pilot)
            review = text_of(app, "#wizard-step-review")
            assert "espaço livre é menor que o necessário" in review

    run(exercise)


def test_a_review_without_an_answer_simply_says_nothing_about_storage():
    async def exercise() -> None:
        fake = FakeHubApplication()
        fake.storage = StorageEstimate()
        app = hub(fake, first_run_wizard=True)
        async with app.run_test(size=(110, 36)) as pilot:
            await walk_to_review(pilot)
            review = text_of(app, "#wizard-step-review")
            assert "Downloads conhecidos" not in review
            assert "Espaço livre" not in review

    run(exercise)


# --- leaving ---------------------------------------------------------------------


def test_the_hub_refuses_to_close_while_an_operation_is_running():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            app.show_section(Section.INSTALLATION)
            await pilot.pause()
            await pilot.click("#install-repair")
            for _ in range(4):
                await pilot.pause()
            await pump(pilot, fake, 2)  # the operation is under way, not terminal

            app.action_quit()
            await pilot.pause()
            assert app.is_running, "the Hub stays open while an operation runs"
            notice = text_of(app, "#install-notice")
            assert "ainda está em andamento" in notice
            assert "não cancela" in notice

    run(exercise)


def test_closing_is_allowed_once_nothing_is_running_and_says_the_stack_stays_up():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            assert "não para a stack" in app.exit_notice()
            app.action_quit()
            await pilot.pause()
            assert not app.is_running

    run(exercise)


# --- small terminals ---------------------------------------------------------------


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40), (160, 50)])
def test_the_dashboard_keeps_its_actions_reachable_at_every_supported_size(size):
    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            panel = app.query_one(DashboardPanel)
            for button_id in ("dashboard-open-webui", "dashboard-start", "dashboard-restart"):
                button = panel.query_one(f"#{button_id}", Button)
                assert button.display, f"{button_id} is off screen at {size}"
                assert button.region.width > 0, f"{button_id} has no width at {size}"

    run(exercise)


def test_the_smallest_supported_terminal_drops_the_rail_and_keeps_the_content():
    """At 80x24 every column belongs to what a person must read and press."""

    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.query_one("#right-rail").display
            overview = app.query_one("#dashboard-overview", Static)
            assert overview.region.width > 0

    run(exercise)


def test_a_narrow_terminal_keeps_the_rail_but_gives_back_its_width():
    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            rail = app.query_one("#right-rail")
            assert rail.display and rail.has_class("narrow")
            assert app.query_one("#dashboard-overview", Static).region.width > 0

    run(exercise)


@pytest.mark.parametrize("size", [(120, 40), (160, 50)])
def test_a_wide_terminal_keeps_the_rail(size):
    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.query_one("#right-rail").display

    run(exercise)


def test_nothing_critical_lives_only_in_the_rail():
    """Whatever the rail shows, the main content says too."""

    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            overview = text_of(app, "#dashboard-overview")
            assert "Perfil:" in overview
            assert "GPU" in overview
            assert app.query_one("#dashboard-open-webui", Button).display

    run(exercise)
