"""What the screens offer, and what they only describe.

An internal profile is for development: it is not a choice on any screen a
person reaches. An installation that already runs one is a different matter --
the Hub still has to say what it is running.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from hub_fakes import ready_application
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from textual.widgets import Button

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.config import InstalledConfig
from local_ai_stack.profiles import offered_profiles
from local_ai_stack.tui.navigation import Section

INTERNAL = "test"


def hub(*, profile_id: str = "pepe") -> LocalAIStackApp:
    """The shell over an installation that runs ``profile_id``."""
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        installed_config=InstalledConfig(profile_id=profile_id),
        application=ready_application(profile_id=profile_id),
        open_browser=lambda url: True,
        first_run_wizard=False,
    )


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def settle(pilot, times: int = 6) -> None:
    for _ in range(times):
        await pilot.pause()


def profile_buttons(app) -> set[str]:
    return {
        button.id.removeprefix("profile-")
        for button in app.query(".profile-button").results(Button)
    }


def test_the_shipped_profiles_include_an_internal_one():
    """Otherwise the tests below would prove nothing."""
    profiles = all_profiles()
    assert any(profile.id == INTERNAL and profile.internal for profile in profiles)
    assert INTERNAL not in {profile.id for profile in offered_profiles(profiles, env={})}


def test_the_profile_screen_does_not_offer_an_internal_profile():
    async def exercise() -> None:
        app = hub()
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 3)

            offered = profile_buttons(app)
            assert INTERNAL not in offered, "an internal profile is not a choice"
            assert {"pepe", "pepe-8gb", "capella"} <= offered

    run(exercise)


def test_the_wizard_does_not_offer_an_internal_profile():
    async def exercise() -> None:
        app = hub()
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.open_wizard()
            await settle(pilot, 4)

            offered = {choice.id for choice in app.wizard.choices}
            assert INTERNAL not in offered
            assert "pepe" in offered

    run(exercise)


def test_an_installation_that_runs_an_internal_profile_still_says_so():
    """Hidden from the offer is not hidden from the truth."""

    async def exercise() -> None:
        internal = next(profile for profile in all_profiles() if profile.id == INTERNAL)
        app = hub(profile_id=INTERNAL)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 3)

            assert app.selected_profile.id == INTERNAL
            assert INTERNAL in profile_buttons(app)
            assert app.query_one(f"#profile-{INTERNAL}", Button).variant == "primary"
            details = str(app.query_one("#profile-details").content)
            assert internal.name in details

    run(exercise)


def test_choosing_a_profile_keeps_working_when_one_is_hidden():
    async def exercise() -> None:
        app = hub()
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 3)

            # A profile with no button of its own must not break the ones that
            # have one: the panel paints whatever buttons it has. (A refresh
            # puts the selection back on the installed profile, so this asks
            # the panel the question directly.)
            app.select_profile("capella")
            assert app.selected_profile.id == "capella"
            assert app.query_one("#profile-capella", Button).variant == "primary"
            assert app.query_one("#profile-pepe", Button).variant == "default"
            assert "Capella" in str(app.query_one("#profile-details").content)

    run(exercise)


def test_an_internal_profile_in_use_does_not_become_a_second_button():
    """The profile in use is added once, not once per refresh."""

    async def exercise() -> None:
        app = hub(profile_id=INTERNAL)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            visible = app.visible_profiles()
            assert [profile.id for profile in visible].count(INTERNAL) == 1
            app.installed_config = replace(app.installed_config, profile_id="pepe")
            assert INTERNAL not in {profile.id for profile in app.visible_profiles()}

    run(exercise)
