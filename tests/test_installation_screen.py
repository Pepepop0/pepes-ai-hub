"""The Installation screen: the installation in use, and every one the Hub knows.

Four ways in, all of them exercised here against a scripted application: the
quick switch, the list with what can be done to each entry, the folder picker
for an installation the Hub has never seen or one whose folder moved, and a
new installation that never replaces the one in use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hub_fakes import FakeFolderPicker, FakeHubApplication, ready_application
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from textual.widgets import Button, OptionList, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.tui.application import InstallationSummary
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import InstallPanel
from local_ai_stack.tui.wizard import WizardScreen

PEPEH = Path(r"C:\Users\Pedro\Desktop\PEPEH")
PEPINHO = Path(r"D:\Pepinho")
CAPELLA = Path(r"E:\AI\Capella")


def summaries(*, missing: bool = False) -> tuple[InstallationSummary, ...]:
    return (
        InstallationSummary(
            PEPEH, "pepe", "Pepe", InstallLocationMode.CUSTOM, "ready", active=True
        ),
        InstallationSummary(PEPINHO, "pepe-8gb", "Pepinho", InstallLocationMode.CUSTOM, "stopped"),
        InstallationSummary(
            CAPELLA,
            "capella",
            "Capella",
            InstallLocationMode.CUSTOM,
            "missing" if missing else "ready",
            detail="A pasta não está disponível." if missing else "",
        ),
    )


def hub(application: FakeHubApplication, *, picker: FakeFolderPicker | None = None):
    opened: list[str] = []
    app = LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: opened.append(url) or True,
        first_run_wizard=False,
        folder_picker=picker,
    )
    return app, opened


def installed(**overrides) -> FakeHubApplication:
    fake = ready_application(**overrides)
    fake.summaries = summaries()
    fake.root_display = str(PEPEH)
    fake.profile_id = "pepe"
    fake.mode = InstallLocationMode.CUSTOM
    return fake


async def open_installation_section(pilot) -> InstallPanel:
    await pilot.pause()
    pilot.app.show_section(Section.INSTALLATION)
    await pilot.pause()
    await pilot.pause()
    return pilot.app.query_one(InstallPanel)


def run(coroutine) -> None:
    asyncio.run(coroutine())


def text_of(app, selector: str) -> str:
    return str(app.query_one(selector, Static).content)


def known_text(app) -> str:
    """Everything the list of known installations says, rows and help together."""
    listing = app.query_one("#install-known-list", OptionList)
    rows = [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]
    return "\n".join(
        [
            str(app.query_one("#install-known", Static).content),
            *rows,
            str(app.query_one("#install-known-help", Static).content),
        ]
    )


def switch_options(app) -> set[str]:
    """What the quick switch offers, as the folders behind the labels."""
    panel = app.query_one(InstallPanel)
    return {value for _, value in panel._switch_options()}


# --- what the screen shows ------------------------------------------------------


def test_the_screen_names_the_installation_in_use_and_lists_the_others():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            current = text_of(app, "#install-summary")
            assert "[b]Pepe[/b]" in current
            assert str(PEPEH) in current
            assert "Personalizada" in current and "Pronta" in current

            known = known_text(app)
            for root in (PEPEH, PEPINHO, CAPELLA):
                assert str(root) in known
            assert "Atual" in known
            assert "Nenhum arquivo será apagado" in known

    run(exercise)


def test_the_quick_switch_offers_the_other_installations_and_not_the_one_in_use():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            options = switch_options(app)
            assert str(PEPEH) not in options, "the one in use is not something to switch to"
            assert options == {str(PEPINHO), str(CAPELLA)}

    run(exercise)


def test_an_installation_whose_folder_is_gone_is_shown_and_not_offered():
    async def exercise() -> None:
        fake = installed()
        fake.summaries = summaries(missing=True)
        app, _ = hub(fake, picker=FakeFolderPicker(PEPINHO))
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            known = known_text(app)
            assert "Pasta indisponível" in known
            assert "A pasta não está disponível." in known
            assert str(CAPELLA) not in switch_options(app)

            # Its way back is to find the folder, which is offered once selected.
            panel.select_row(CAPELLA)
            await pilot.pause()
            assert not app.query_one("#install-locate", Button).disabled

    run(exercise)


# --- switching ------------------------------------------------------------------


def test_choosing_from_the_quick_switch_asks_the_application_to_switch():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await pilot.pause()
            await pilot.pause()
            assert fake.managed == [("switch", PEPINHO)]
            assert fake.profile_id == "pepe-8gb"

    run(exercise)


def test_asking_for_the_same_installation_twice_is_the_decision_to_stop_and_switch():
    """The first request explains; the second is the person saying yes."""

    async def exercise() -> None:
        fake = installed()
        fake.switch_requires_stop = True
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await pilot.pause()
            await pilot.pause()
            assert [name for name, _ in fake.managed] == ["switch"]

            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await pilot.pause()
            await pilot.pause()
            # The second request checks again before stopping anything.
            assert [name for name, _ in fake.managed] == ["switch", "switch", "stop_and_switch"]
            assert fake.profile_id == "pepe-8gb"

    run(exercise)


def test_a_running_installation_is_not_stopped_by_choosing_another_one():
    async def exercise() -> None:
        fake = installed()
        fake.switch_requires_stop = True
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.post_message(InstallPanel.SwitchRequested(PEPINHO))
            await pilot.pause()
            await pilot.pause()
            assert fake.managed == [("switch", PEPINHO)], "nothing was stopped"
            notice = text_of(app, "#install-notice")
            assert "ainda está em execução" in notice
            assert "Processos externos não serão encerrados" in notice
            assert fake.profile_id == "pepe", "the installation in use did not move"

    run(exercise)


def test_a_switch_the_application_refuses_says_why_and_changes_nothing():
    async def exercise() -> None:
        fake = installed()
        fake.switch_error = "A pasta desta instalação não está disponível."
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.post_message(InstallPanel.SwitchRequested(CAPELLA))
            await pilot.pause()
            await pilot.pause()
            assert "não está disponível" in text_of(app, "#install-notice")
            assert fake.profile_id == "pepe"

    run(exercise)


# --- the folder picker ----------------------------------------------------------


def test_opening_an_existing_installation_uses_the_folder_picker():
    async def exercise() -> None:
        fake = installed()
        picked = Path(r"F:\Outra")
        app, _ = hub(fake, picker=FakeFolderPicker(picked))
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            await pilot.click("#install-open-existing")
            for _ in range(8):
                await pilot.pause()
            assert ("open", picked) in fake.managed
            assert str(picked) in known_text(app)

    run(exercise)


def test_a_folder_that_is_not_an_installation_is_refused_in_plain_words():
    async def exercise() -> None:
        fake = installed()
        fake.open_error = "Esta pasta não contém uma instalação do Hub."
        app, _ = hub(fake, picker=FakeFolderPicker(Path(r"F:\Fotos")))
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            await pilot.click("#install-open-existing")
            for _ in range(8):
                await pilot.pause()
            assert "não contém uma instalação" in text_of(app, "#install-notice")
            assert fake.profile_id == "pepe"

    run(exercise)


def test_without_a_folder_picker_the_button_is_not_offered():
    async def exercise() -> None:
        app, _ = hub(installed())
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            assert app.query_one("#install-open-existing", Button).disabled

    run(exercise)


# --- forgetting -----------------------------------------------------------------


def test_forgetting_asks_the_application_and_says_nothing_was_deleted():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.select_row(PEPINHO)
            await pilot.pause()
            await pilot.click("#install-forget")
            for _ in range(6):
                await pilot.pause()
            assert ("forget", PEPINHO) in fake.managed
            assert "Nenhum arquivo foi apagado" in text_of(app, "#install-notice")

    run(exercise)


def test_the_installation_in_use_cannot_be_forgotten_from_the_screen():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_installation_section(pilot)
            panel.select_row(PEPEH)
            await pilot.pause()
            assert app.query_one("#install-forget", Button).disabled
            assert fake.managed == []

    run(exercise)


# --- repair and new installation -------------------------------------------------


def test_checking_and_repairing_runs_on_the_installation_in_use():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            assert str(app.query_one("#install-repair", Button).label) == "Verificar e reparar"
            await pilot.click("#install-repair")
            for _ in range(6):
                await pilot.pause()
            assert ("repair", None) in fake.managed

    run(exercise)


def test_installing_again_is_a_new_installation_and_opens_the_wizard():
    async def exercise() -> None:
        fake = installed()
        app, _ = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_installation_section(pilot)
            assert str(app.query_one("#install-open-wizard", Button).label) == "Nova instalação"
            await pilot.click("#install-open-wizard")
            await pilot.pause()
            assert isinstance(app.screen, WizardScreen)
            assert fake.calls == [], "opening the wizard installs nothing"

    run(exercise)


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40), (160, 50)])
def test_the_screen_keeps_its_actions_reachable_at_every_supported_size(size):
    async def exercise() -> None:
        app, _ = hub(installed())
        async with app.run_test(size=size) as pilot:
            await open_installation_section(pilot)
            for button_id in (
                "install-change",
                "install-repair",
                "install-open-wizard",
                "install-open-existing",
            ):
                button = app.query_one(f"#{button_id}", Button)
                assert button.display, f"{button_id} is not on screen at {size}"

    run(exercise)
