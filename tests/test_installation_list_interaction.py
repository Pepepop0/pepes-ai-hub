"""The list of installations, worked the way a person works it.

Nothing here calls the panel's methods: rows are chosen with the keyboard and
with the mouse, because a row nobody can select is a row whose actions nobody
can reach.
"""

from __future__ import annotations

import asyncio

import pytest
from hub_fakes import FakeFolderPicker
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_installation_screen import CAPELLA, PEPINHO, installed, summaries
from textual.widgets import Button, OptionList

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import InstallPanel

LIST = "#install-known-list"


def hub(application, *, picker=None):
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=False,
        folder_picker=picker,
    )


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def open_section(pilot, times: int = 4) -> InstallPanel:
    for _ in range(times):
        await pilot.pause()
    pilot.app.show_section(Section.INSTALLATION)
    for _ in range(times):
        await pilot.pause()
    return pilot.app.query_one(InstallPanel)


def rows(app) -> OptionList:
    return app.query_one(LIST, OptionList)


def row_line(listing: OptionList, index: int) -> int:
    """Where the widget drew that row, asking the widget itself.

    A row whose text wraps is more than one line tall, so the line a person
    would aim at is not the row number.
    """
    top = listing.content_region.y - listing.region.y - listing.scroll_offset.y
    return top + listing._index_to_line[index]


# --- the list is a control, not a paragraph -----------------------------------------


def test_the_list_is_a_focusable_control_with_one_row_per_installation():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_section(pilot)
            listing = rows(app)
            assert listing.can_focus, "a person must be able to reach it"
            assert listing.option_count == 3
            labels = [str(listing.get_option_at_index(i).prompt) for i in range(3)]
            assert any("PEPEH" in label for label in labels)
            assert any("Pepinho" in label for label in labels)
            assert any("Capella" in label for label in labels)
            assert any("Atual" in label for label in labels)

    run(exercise)


def test_a_row_chosen_with_the_keyboard_becomes_the_selected_installation():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_section(pilot)
            listing = rows(app)
            listing.focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()

            assert panel.selected_root is not None
            assert str(panel.selected_root) == str(PEPINHO)
            assert not app.query_one("#install-forget", Button).disabled

    run(exercise)


def test_a_row_chosen_with_the_mouse_becomes_the_selected_installation():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_section(pilot)
            listing = rows(app)
            index = next(
                i
                for i in range(listing.option_count)
                if "Capella" in str(listing.get_option_at_index(i).prompt)
            )
            await pilot.click(LIST, offset=(2, row_line(listing, index)))
            for _ in range(4):
                await pilot.pause()

            assert str(panel.selected_root) == str(CAPELLA)

    run(exercise)


def test_an_installation_whose_folder_is_gone_can_be_selected_and_located():
    async def exercise() -> None:
        fake = installed()
        fake.summaries = summaries(missing=True)
        app = hub(fake, picker=FakeFolderPicker(PEPINHO))
        async with app.run_test(size=(120, 40)) as pilot:
            panel = await open_section(pilot)
            listing = rows(app)
            listing.focus()
            await pilot.pause()
            for _ in range(2):
                await pilot.press("down")
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()

            assert str(panel.selected_root) == str(CAPELLA)
            assert not app.query_one("#install-locate", Button).disabled
            await pilot.click("#install-locate")
            for _ in range(10):
                await pilot.pause()
            assert any(name == "relocate" for name, _ in fake.managed)

    run(exercise)


def test_the_installation_in_use_cannot_be_forgotten_however_it_is_selected():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_section(pilot)
            listing = rows(app)
            listing.focus()
            await pilot.pause()
            await pilot.press("enter")  # the first row is the one in use
            for _ in range(4):
                await pilot.pause()

            assert app.query_one("#install-forget", Button).disabled
            assert fake.managed == []

    run(exercise)


def test_forgetting_a_selected_row_asks_the_application_and_leaves_files_alone():
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await open_section(pilot)
            listing = rows(app)
            listing.focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.click("#install-forget")
            for _ in range(6):
                await pilot.pause()

            assert ("forget", PEPINHO) in fake.managed
            assert str(PEPINHO) not in str(rows(app).get_option_at_index(0).prompt)

    run(exercise)


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 40)])
def test_the_list_stays_usable_in_a_small_terminal(size):
    async def exercise() -> None:
        fake = installed()
        app = hub(fake)
        async with app.run_test(size=size) as pilot:
            panel = await open_section(pilot)
            listing = rows(app)
            assert listing.display and listing.region.height > 0
            listing.focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            assert panel.selected_root is not None
            for button_id in ("install-forget", "install-locate", "install-repair"):
                assert app.query_one(f"#{button_id}", Button).display

    run(exercise)


def test_a_long_list_scrolls_rather_than_pushing_the_actions_away():
    async def exercise() -> None:
        from dataclasses import replace
        from pathlib import Path

        fake = installed()
        many = list(summaries())
        for index in range(12):
            many.append(
                replace(
                    summaries()[1],
                    root=Path(rf"D:\Instalacao{index}"),
                    profile_name=f"Pepinho {index}",
                )
            )
        fake.summaries = tuple(many)
        app = hub(fake)
        async with app.run_test(size=(100, 30)) as pilot:
            await open_section(pilot)
            listing = rows(app)
            assert listing.option_count == 15
            assert app.query_one("#install-repair", Button).display
            assert app.query_one("#install-open-wizard", Button).display

    run(exercise)
