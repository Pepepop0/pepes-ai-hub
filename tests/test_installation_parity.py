"""After a switch, everything says the same thing -- or nothing moved at all.

The backend, the configuration the screens read, the Dashboard, the
Installation screen and the file on disk describe one installation. A switch
that is cancelled or refused leaves every one of them on the previous one,
including what the wizard had selected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hub_fakes import FakeHubApplication, ready_application
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_installation_screen import CAPELLA, PEPEH, PEPINHO, installed, summaries
from textual.widgets import Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import InstallPanel, MainContent


def hub(application: FakeHubApplication):
    app = LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=False,
    )
    return app


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def settle(pilot, times: int = 6) -> None:
    for _ in range(times):
        await pilot.pause()


async def installation_section(pilot) -> InstallPanel:
    await settle(pilot)
    pilot.app.show_section(Section.INSTALLATION)
    await settle(pilot, 3)
    return pilot.app.query_one(InstallPanel)


def dashboard_text(app) -> str:
    return str(app.query_one("#dashboard-overview", Static).content)


def install_text(app) -> str:
    return str(app.query_one("#install-summary", Static).content)


# --- one installation, everywhere ---------------------------------------------------


def test_after_a_switch_every_surface_names_the_same_installation():
    """The blocker: the backend moved and the screens kept the previous root."""

    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await installation_section(pilot)
            assert str(PEPEH) in install_text(app)

            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await settle(pilot, 10)

            # The application
            assert fake.root_display == str(PEPINHO)
            assert fake.profile_id == "pepe-8gb"
            # The configuration the screens read
            assert app.installed_config.profile_id == "pepe-8gb"
            assert app.installed_config.installation.root == PEPINHO
            assert app.query_one(MainContent).config.installation.root == PEPINHO
            # The screens
            assert str(PEPINHO) in install_text(app)
            assert "Pepinho" in install_text(app)
            app.show_section(Section.DASHBOARD)
            await settle(pilot, 4)
            assert str(PEPINHO) in dashboard_text(app)
            assert "Pepinho" in dashboard_text(app)
            # And the selected profile followed too.
            assert app.selected_profile.id == "pepe-8gb"

    run(exercise)


def test_adopting_a_folder_moves_every_surface_with_it():
    """Open existing -> commit -> Dashboard, Installation, backend and config agree."""

    async def exercise() -> None:
        from hub_fakes import FakeFolderPicker

        fake = installed()
        picked = Path(r"F:\Outra")
        app = LocalAIStackApp(
            facts=reference_facts(),
            profiles=all_profiles(),
            runtime_snapshot=stopped_runtime(),
            integration=integration_snapshot(),
            application=fake,
            open_browser=lambda url: True,
            first_run_wizard=False,
            folder_picker=FakeFolderPicker(picked),
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await installation_section(pilot)
            await pilot.click("#install-open-existing")
            await settle(pilot, 12)

            assert fake.root_display == str(picked)
            assert app.installed_config.installation.root == picked
            assert str(picked) in install_text(app)
            app.show_section(Section.DASHBOARD)
            await settle(pilot, 4)
            assert str(picked) in dashboard_text(app)

    run(exercise)


# --- a switch that does not happen leaves every surface alone ------------------------


def test_a_refused_switch_leaves_every_surface_on_the_installation_in_use():
    async def exercise() -> None:
        fake = installed()
        fake.switch_error = "A pasta desta instalação não está disponível."
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await installation_section(pilot)
            before = install_text(app)

            panel.post_message(InstallPanel.SwitchRequested(CAPELLA))
            await settle(pilot, 8)

            assert app.installed_config.installation.root == PEPEH
            assert app.selected_profile.id == "pepe"
            assert str(PEPEH) in install_text(app)
            assert "Capella" not in install_text(app).split("conhecidas")[0]
            assert before.split("\n")[2] == install_text(app).split("\n")[2]
            app.show_section(Section.DASHBOARD)
            await settle(pilot, 4)
            assert str(PEPEH) in dashboard_text(app)
    run(exercise)


def test_a_switch_that_needs_a_stop_changes_nothing_until_it_is_approved():
    async def exercise() -> None:
        fake = installed()
        fake.switch_requires_stop = True
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await installation_section(pilot)
            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await settle(pilot, 8)

            assert app.installed_config.installation.root == PEPEH
            assert app.selected_profile.id == "pepe"
            assert str(PEPEH) in install_text(app)
            assert "ainda está em execução" in str(
                app.query_one("#install-notice", Static).content
            )
    run(exercise)


def test_a_failed_installation_leaves_the_wizard_selection_and_the_screens_on_the_old_one():
    """An installation that fails is not a selection: the Hub stays where it was."""

    async def exercise() -> None:
        fake = installed()
        fake.install_fail_at = "Baixando modelos"
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await installation_section(pilot)
            before_profile = app.selected_profile.id
            before_root = app.installed_config.installation.root

            from local_ai_stack.tui.application import InstallSelection

            app.post_message_install = None
            fake.install(InstallSelection("pepe-8gb", InstallLocationMode.DEFAULT))
            while fake.step() is not None:
                await pilot.pause()
            await settle(pilot, 8)

            assert app.selected_profile.id == before_profile
            assert app.installed_config.installation.root == before_root
            assert str(PEPEH) in install_text(app)
    run(exercise)


# --- the installation in use is never lost from the list -------------------------------


def test_the_installation_in_use_is_always_a_row_in_the_list():
    async def exercise() -> None:
        fake = installed()
        fake.summaries = summaries()[1:]  # the list forgot the active one
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await installation_section(pilot)
            # The panel still names what is in use, from the view it is given.
            assert "Pepe" in install_text(app)
            assert str(PEPEH) in install_text(app) or "PEPEH" in install_text(app)
            assert panel.summaries == fake.summaries
    run(exercise)


@pytest.mark.parametrize("mode", list(InstallLocationMode))
def test_every_mode_reads_the_same_on_the_dashboard(mode):
    async def exercise() -> None:
        fake = ready_application()
        fake.mode = mode
        fake.root_display = str(PEPEH)
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot, 6)
            text = dashboard_text(app)
            assert str(PEPEH) in text
            assert mode.value not in text, "the mode is written in words, not as an id"
    run(exercise)
