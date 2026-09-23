"""The last findings of the V0 visual smoke, each one worked the way a person works it.

- a first opening offers the first installation in a dialog, and never walks
  into the wizard by itself;
- Instalação has two places: Instalar creates, Instalações manages;
- "Trocar instalação" opens a list of every known installation, the one in use
  included, even when it is the only one;
- switching the way the smoke did -- choose, cancel, choose again, use -- raises
  nothing (the InvalidSelectValueError came from resetting a Select with
  ``Select.BLANK``, which in Textual 8 is the widget's ``BLANK = False``
  rendering flag, not a selection sentinel);
- choosing an installation does not start it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hub_fakes import FakeFolderPicker, FakeHubApplication
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_installation_screen import CAPELLA, PEPEH, PEPINHO, installed, summaries
from test_installation_switching import World, custom, hub, install
from textual.widgets import Button, OptionList, Select, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.installations import read_state
from local_ai_stack.tui.dialogs import SwitchInstallationScreen, WelcomeScreen
from local_ai_stack.tui.navigation import SECTION_ITEMS, Section, route_id
from local_ai_stack.tui.widgets import InstallPanel, MainContent, NewInstallationPanel
from local_ai_stack.tui.wizard import WizardScreen

SOURCE = Path(__file__).resolve().parents[1] / "src" / "local_ai_stack"


def shell(application, *, first_run_wizard: bool = False, picker=None) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        installed_config=getattr(application, "config", None),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=first_run_wizard,
        folder_picker=picker,
    )


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def settle(pilot, times: int = 6) -> None:
    await pilot.app.workers.wait_for_complete()
    for _ in range(times):
        await pilot.pause()


async def press(pilot, selector: str) -> None:
    """Click a button on whatever screen is in front, once it can be clicked."""
    button = pilot.app.screen.query_one(selector, Button)
    button.scroll_visible(animate=False)
    await pilot.pause()
    assert await pilot.click(selector), f"the click did not land on {selector}"
    await pilot.pause()


async def installations(pilot) -> InstallPanel:
    pilot.app.show_route(Section.INSTALLATION, "installations")
    await settle(pilot, 3)
    return pilot.app.query_one(InstallPanel)


async def open_switch(pilot) -> SwitchInstallationScreen:
    await press(pilot, "#install-change")
    await settle(pilot, 3)
    screen = pilot.app.screen
    assert isinstance(screen, SwitchInstallationScreen), type(screen).__name__
    return screen


def rows(pilot) -> list[str]:
    listing = pilot.app.screen.query_one("#switch-list", OptionList)
    return [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]


def row_line(listing: OptionList, index: int) -> int:
    top = listing.content_region.y - listing.region.y - listing.scroll_offset.y
    return top + listing._index_to_line[index]


# --- onboarding ---------------------------------------------------------------------------


def test_a_first_opening_offers_the_first_installation_and_does_not_walk_into_the_wizard():
    async def exercise() -> None:
        app = shell(FakeHubApplication(), first_run_wizard=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert isinstance(app.screen, WelcomeScreen)
            body = str(app.screen.query_one("#welcome-body", Static).content)
            assert "Nenhuma instalação está configurada ainda" in body
            assert not isinstance(app.screen, WizardScreen)

    run(exercise)


def test_accepting_the_welcome_opens_the_first_installation():
    async def exercise() -> None:
        fake = FakeHubApplication()
        app = shell(fake, first_run_wizard=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await press(pilot, "#welcome-start")
            assert isinstance(app.screen, WizardScreen)
            assert fake.calls == [], "opening the wizard installs nothing"

    run(exercise)


def test_declining_leaves_a_usable_hub_that_still_offers_the_first_installation():
    async def exercise() -> None:
        fake = FakeHubApplication()
        app = shell(fake, first_run_wizard=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            await press(pilot, "#welcome-later")
            assert not isinstance(app.screen, (WelcomeScreen, WizardScreen))
            # Usable: the sections answer.
            await pilot.press("2")
            await settle(pilot, 2)
            assert app.active_section is Section.INSTALLATION
            await pilot.press("1")
            await settle(pilot, 2)
            # The empty Dashboard keeps the same invitation.
            assert app.query_one("#dashboard-empty").display
            # Not again this session, however often the Hub refreshes.
            app.refresh_projections()
            await settle(pilot)
            app.offer_first_installation()
            await settle(pilot, 2)
            assert not isinstance(app.screen, WelcomeScreen)
            await press(pilot, "#dashboard-install")
            assert isinstance(app.screen, WizardScreen)
            assert fake.calls == []

    run(exercise)


def test_an_installation_that_is_configured_is_never_greeted_as_a_first_one():
    """Configured but not ready -- a stopped stack whose models cannot be read -- is
    not "no installation": no welcome, no wizard."""

    async def exercise() -> None:
        fake = installed()
        fake.ready = False
        app = shell(fake, first_run_wizard=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert not isinstance(app.screen, (WelcomeScreen, WizardScreen))
            assert not app.query_one("#dashboard-empty").display

    run(exercise)


# --- navigation ---------------------------------------------------------------------------


def test_instalar_and_instalacoes_are_side_by_side_in_the_sidebar():
    labels = [item.label for item in SECTION_ITEMS[Section.INSTALLATION]]
    assert labels[:2] == ["Instalar", "Instalações"]

    async def exercise() -> None:
        app = shell(installed())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await pilot.press("2")
            await settle(pilot, 2)
            shown = [str(b.label) for b in app.query("#sidebar-installation Button")]
            assert shown[:2] == ["Instalar", "Instalações"]
            for item in ("install", "installations", "profile", "location", "plan"):
                assert await pilot.click(f"#side-installation-{item}"), item
                await pilot.pause()
                assert app.query_one(MainContent).current == route_id(Section.INSTALLATION, item)

    run(exercise)


def test_instalar_is_for_a_new_installation_only():
    async def exercise() -> None:
        fake = installed()
        app = shell(fake)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.show_route(Section.INSTALLATION, "install")
            await settle(pilot, 2)
            panel = app.query_one(NewInstallationPanel)
            assert "Nova instalação" in str(panel.query_one("#new-install-summary").content)
            # No management here: no list, no switching, no repair.
            assert not panel.query(OptionList) and not panel.query(Select)
            assert not panel.query("#install-change") and not panel.query("#install-repair")
            await press(pilot, "#new-install-start")
            assert isinstance(app.screen, WizardScreen)
            assert fake.calls == []

    run(exercise)


def test_instalacoes_is_where_installations_are_managed_and_has_no_dropdown():
    async def exercise() -> None:
        app = shell(installed())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            panel = await installations(pilot)
            assert app.query_one(MainContent).current == route_id(
                Section.INSTALLATION, "installations"
            )
            assert not panel.query(Select), "the inline quick switch is gone"
            for button_id, label in (
                ("install-change", "Trocar instalação"),
                ("install-open-root", "Abrir pasta"),
                ("install-verify", "Verificar instalação"),
                ("install-repair", "Reparar"),
                ("install-use", "Usar esta instalação"),
                ("install-open-selected", "Abrir pasta"),
                ("install-locate", "Localizar pasta"),
                ("install-forget", "Esquecer da lista"),
                ("install-open-existing", "Abrir instalação existente"),
                ("install-open-wizard", "Nova instalação"),
            ):
                assert str(panel.query_one(f"#{button_id}", Button).label) == label
            assert panel.query_one("#install-known-list", OptionList).option_count == 3

    run(exercise)


# --- the switch dialog ------------------------------------------------------------------------


def test_the_switch_dialog_opens_with_a_single_known_installation():
    async def exercise() -> None:
        fake = installed()
        fake.summaries = summaries()[:1]
        app = shell(fake)
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await installations(pilot)
            dialog = await open_switch(pilot)
            [only] = rows(pilot)
            assert "[Atual]" in only and "Pepe" in only and str(PEPEH) in only
            empty = str(dialog.query_one("#switch-empty", Static).content)
            assert "Nenhuma outra instalação conhecida" in empty
            assert not dialog.query("#switch-use"), "nothing to switch to"
            assert dialog.query_one("#switch-new", Button)
            await press(pilot, "#switch-cancel")
            assert not isinstance(app.screen, SwitchInstallationScreen)
            assert fake.managed == []

    run(exercise)


def test_the_switch_dialog_works_from_the_keyboard():
    async def exercise() -> None:
        fake = installed()
        app = shell(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await installations(pilot)
            dialog = await open_switch(pilot)
            assert dialog.query_one("#switch-list", OptionList).has_focus, "focus is visible"
            await pilot.press("down")
            await pilot.pause()
            assert dialog.selected.root == PEPINHO
            await press(pilot, "#switch-use")
            await settle(pilot, 3)
            assert ("switch", PEPINHO) in fake.managed

    run(exercise)


def test_the_switch_dialog_works_with_the_mouse():
    async def exercise() -> None:
        fake = installed()
        app = shell(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await installations(pilot)
            dialog = await open_switch(pilot)
            listing = dialog.query_one("#switch-list", OptionList)
            index = next(i for i, text in enumerate(rows(pilot)) if str(CAPELLA) in text)
            assert await pilot.click("#switch-list", offset=(2, row_line(listing, index)))
            await pilot.pause()
            assert dialog.selected.root == CAPELLA
            await press(pilot, "#switch-use")
            await settle(pilot, 3)
            assert ("switch", CAPELLA) in fake.managed

    run(exercise)


def test_cancelling_and_using_the_current_one_change_nothing():
    async def exercise() -> None:
        fake = installed()
        app = shell(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await installations(pilot)
            await open_switch(pilot)
            await pilot.press("escape")
            await settle(pilot, 2)
            assert not isinstance(app.screen, SwitchInstallationScreen)
            dialog = await open_switch(pilot)
            assert dialog.selected.active, "it opens on the installation in use"
            await press(pilot, "#switch-use")
            await settle(pilot, 2)
            assert not isinstance(app.screen, SwitchInstallationScreen)
            assert fake.managed == [], "using the one in use is a no-op"

    run(exercise)


# --- the crash, the way the smoke hit it, over the real adapter ------------------------------


@pytest.fixture
def world() -> World:
    return World()


def test_choose_cancel_choose_again_and_switch_raises_nothing(tmp_path, world):
    """A and B known, A in use: open, pick B, cancel, open again, pick B, use it."""
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await installations(pilot)
            await open_switch(pilot)
            await pilot.press("down")
            await press(pilot, "#switch-cancel")
            await settle(pilot, 2)
            dialog = await open_switch(pilot)
            await pilot.press("down")
            await pilot.pause()
            assert dialog.selected.root == second
            await press(pilot, "#switch-use")
            await settle(pilot)
            assert not isinstance(app.screen, SwitchInstallationScreen)
            # No Select anywhere is left holding a value it does not offer.
            for select in app.query(Select):
                assert select.value is not False

    try:
        run(exercise)
        assert adapter.active_root() == second
        assert read_state(adapter.selection_path).active_root == second
    finally:
        adapter.close()


def test_a_single_known_installation_opens_and_closes_without_error(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    install(adapter, custom(first))
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await installations(pilot)
            await open_switch(pilot)
            assert len(rows(pilot)) == 1 and str(first) in rows(pilot)[0]
            await pilot.press("escape")
            await settle(pilot, 2)
            assert not isinstance(app.screen, SwitchInstallationScreen)

    try:
        run(exercise)
        assert adapter.active_root() == first
    finally:
        adapter.close()


def test_no_select_is_reset_with_the_widget_blank_flag():
    """The root cause, kept out: ``Select.BLANK`` is ``Widget.BLANK = False``."""
    offenders = [
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*.py")
        if "Select.BLANK" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert Select.BLANK is False, "if this changes, the guard above can be revisited"


# --- choosing an installation does not start it ---------------------------------------------


def test_switching_to_a_stopped_installation_leaves_it_stopped(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            await settle(pilot)
            await installations(pilot)
            await open_switch(pilot)
            await pilot.press("down")
            await press(pilot, "#switch-use")
            await settle(pilot)

    try:
        run(exercise)
        assert adapter.active_root() == second
        assert second not in world.running, "chosen, not started"
        assert world.commands_on(second) == ["first_run"], "no start reached it"
        assert not adapter.backend.status("pepe-8gb").stack_up
    finally:
        adapter.close()


def test_the_row_action_switches_without_starting_either(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    app = shell(adapter, picker=FakeFolderPicker(None))

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            panel = await installations(pilot)
            listing = panel.query_one("#install-known-list", OptionList)
            listing.focus()
            await pilot.press("down")
            await pilot.pause()
            assert panel.selected_root == second
            await press(pilot, "#install-use")
            await settle(pilot)

    try:
        run(exercise)
        assert adapter.active_root() == second
        assert "start_stack" not in world.commands_on(second)
        assert "start_stack" not in world.commands_on(first)
    finally:
        adapter.close()
