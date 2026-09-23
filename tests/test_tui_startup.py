"""The shell is on screen before the application has answered anything.

On a root with large models the first status pass used to run inside the
App constructor: minutes of hashing before a single character was drawn, and
the same pass again on every refresh, on the render thread. Here the
application is made to *block* until the test releases it, and the boundary
is checked semantically: the Dashboard composes and says it is checking, the
keyboard still works, and only when the answer arrives does the overview
change. An event, not a sleep, decides when that happens.
"""

from __future__ import annotations

import asyncio
import threading

from hub_fakes import FakeHubApplication
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_tui_operations import run, settle, text
from textual.widgets import Button

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.wizard import WizardScreen


class BlockingApplication(FakeHubApplication):
    """Every projection waits for the gate; the constructor must never open it."""

    def __init__(self, **overrides) -> None:
        super().__init__(**overrides)
        self.gate = threading.Event()
        self.asked = threading.Event()
        self.calls = 0

    def _wait(self) -> None:
        self.calls += 1
        self.asked.set()
        assert self.gate.wait(10), "the projections gate was never opened"

    def installation(self):
        self._wait()
        return super().installation()

    def services_snapshot(self):
        self._wait()
        return super().services_snapshot()

    def vram_snapshot(self):
        self._wait()
        return super().vram_snapshot()


async def until(event: threading.Event, timeout: float = 5.0) -> None:
    """Wait for a worker-side event without holding the event loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set():
        assert asyncio.get_running_loop().time() < deadline, "the application was never asked"
        await asyncio.sleep(0.02)


def shell_for(application: FakeHubApplication) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=True,
    )


def test_the_constructor_asks_the_application_nothing():
    application = BlockingApplication()
    shell_for(application)
    assert application.calls == 0


def test_the_dashboard_draws_and_answers_keys_while_the_application_is_still_working():
    application = BlockingApplication(ready=True, profile_id="capella")

    async def exercise() -> None:
        app = shell_for(application)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            # Drawn, and honest about not knowing yet.
            assert not isinstance(app.screen, WizardScreen)
            assert "Verificando" in text(app, "#dashboard-overview")
            assert app.query_one("#dashboard-open-webui", Button).disabled
            await until(application.asked)
            assert application.calls >= 1
            # The render thread is free: navigation works with the answer pending.
            await pilot.press("3")
            await pilot.pause()
            assert app.active_section is Section.SETTINGS
            await pilot.press("1")
            await pilot.pause()
            assert app.active_section is Section.DASHBOARD
            assert "Verificando" in text(app, "#dashboard-overview")

            application.gate.set()
            await settle(pilot)
            overview = text(app, "#dashboard-overview")
            assert "Verificando" not in overview
            assert "Perfil: [b]Capella[/b]" in overview
            assert not app.query_one("#dashboard-open-webui", Button).disabled

    run(exercise)


def test_the_wizard_waits_for_the_answer_rather_than_guessing():
    application = BlockingApplication()  # nothing installed, but not known yet

    async def exercise() -> None:
        app = shell_for(application)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, WizardScreen)
            application.gate.set()
            await settle(pilot)
            assert isinstance(app.screen, WizardScreen)

    run(exercise)


def test_a_refresh_after_an_operation_does_not_block_the_screen():
    slow = BlockingApplication(ready=True, profile_id="capella")
    slow.gate.set()  # instant at first...

    async def exercise() -> None:
        app = shell_for(slow)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            assert "Perfil: [b]Capella[/b]" in text(app, "#dashboard-overview")
            slow.gate.clear()  # ...then every answer waits again
            slow.asked.clear()
            app.refresh_projections()
            await until(slow.asked)
            await pilot.press("3")
            await pilot.pause()
            assert app.active_section is Section.SETTINGS
            slow.gate.set()
            await settle(pilot)

    run(exercise)
