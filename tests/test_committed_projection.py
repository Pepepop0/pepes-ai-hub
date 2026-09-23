"""The screens describe what the application committed -- never a candidate.

These run the real TUI over the real adapter; only the backends are the stub
from ``test_installation_switching``, which speaks the production contract.
A candidate may be shown while its attempt runs; once the attempt is over --
succeeded, failed, refused, cancelled -- every surface is projected again
from the installation the application committed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime
from test_installation_switching import World, custom, hub, install
from textual.widgets import Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.installations import read_state
from local_ai_stack.tui.navigation import Section
from local_ai_stack.tui.widgets import InstallPanel, MainContent
from local_ai_stack.tui.wizard import WizardScreen


@pytest.fixture
def world() -> World:
    return World()


def shell(adapter) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        installed_config=adapter.config,
        application=adapter,
        open_browser=lambda url: True,
        first_run_wizard=False,
    )


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def settle(pilot, times: int = 8) -> None:
    for _ in range(times):
        await pilot.pause()


async def finish(pilot, adapter) -> None:
    """Let a launched operation end and its ending reach the screens."""
    await pilot.app.workers.wait_for_complete()
    adapter.wait(10)
    await settle(pilot, 10)
    await pilot.app.workers.wait_for_complete()
    await settle(pilot, 6)


async def surfaces(pilot) -> dict[str, str]:
    """What every surface says, read from the screens themselves."""
    app = pilot.app
    app.show_section(Section.DASHBOARD)
    await settle(pilot, 4)
    dashboard = str(app.query_one("#dashboard-overview", Static).content)
    app.show_section(Section.INSTALLATION)
    await settle(pilot, 4)
    installation = str(app.query_one("#install-summary", Static).content)
    return {"dashboard": dashboard, "installation": installation}


def assert_everything_on(app, adapter, root: Path, profile_id: str, seen: dict) -> None:
    assert adapter.active_root() == root
    assert adapter.backend.root == root
    assert adapter.config.installation.root == root
    assert read_state(adapter.selection_path).active_root == root
    assert app.installed_config.installation.root == root
    assert app.installed_config.profile_id == profile_id
    assert app.query_one(MainContent).config.installation.root == root
    assert app.selected_profile.id == profile_id
    assert str(root) in seen["dashboard"]
    assert str(root) in seen["installation"]


# --- 4: an installation that fails takes its candidate off the screens ------------------


def test_a_failed_installation_puts_every_surface_back_on_the_committed_one(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(first))
    world.failing.add(second)
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            # The wizard's request, as the wizard sends it: the candidate is
            # previewed -- its profile, its folder -- while it runs.
            app.post_message(WizardScreen.InstallRequested(custom(second, "pepe-8gb")))
            await settle(pilot, 2)
            await finish(pilot, adapter)

            assert adapter._candidate is None and not adapter._busy
            assert not app.busy_with_operation()
            seen = await surfaces(pilot)
            assert_everything_on(app, adapter, first, "pepe", seen)
            assert str(second) not in seen["dashboard"]

    try:
        run(exercise)
    finally:
        adapter.close()


# --- 3: leaving the wizard while a decision is owed lets go of the candidate --------------


def test_closing_the_wizard_on_a_pending_conflict_releases_the_candidate(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(first))
    world.conflicting.add(second)
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.post_message(WizardScreen.InstallRequested(custom(second, "pepe-8gb")))
            await settle(pilot, 2)
            await app.workers.wait_for_complete()
            await settle(pilot, 4)
            assert adapter._candidate is not None, "the conflict decision is owed"

            app.post_message(WizardScreen.Finished())
            await settle(pilot, 6)

            assert adapter._candidate is None
            assert_everything_on(app, adapter, first, "pepe", await surfaces(pilot))

    try:
        run(exercise)
    finally:
        adapter.close()


# --- 5: an asynchronous stop-and-switch that succeeds moves every surface ------------------


def ask_twice(panel: InstallPanel, root: Path) -> None:
    """The first request explains; the second is the person saying yes."""
    panel.post_message(InstallPanel.SwitchRequested(root))
    panel.post_message(InstallPanel.SwitchRequested(root))


def test_a_stop_and_switch_that_succeeds_moves_every_surface(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    world.running.add(first)
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 4)
            ask_twice(app.query_one(InstallPanel), second)
            await settle(pilot, 4)
            await finish(pilot, adapter)

            assert first not in world.running, "the installation in use was stopped"
            assert adapter._candidate is None and not adapter._busy
            assert_everything_on(app, adapter, second, "pepe-8gb", await surfaces(pilot))
            assert str(app.query_one("#install-notice", Static).content) == ""

    try:
        run(exercise)
    finally:
        adapter.close()


def test_a_stop_and_switch_refused_after_the_stop_leaves_every_surface_on_the_first(
    tmp_path, world
):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    world.running.add(first)
    world.after_stop.append(lambda root: world.external_ports.add(8080))
    app = shell(adapter)

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 4)
            ask_twice(app.query_one(InstallPanel), second)
            await settle(pilot, 4)
            await finish(pilot, adapter)

            notice = str(app.query_one("#install-notice", Static).content)
            assert "Não foi possível trocar de instalação porque os serviços" in notice
            assert app.operation_view is not None and app.operation_view.failed
            assert_everything_on(app, adapter, first, "pepe", await surfaces(pilot))

    try:
        run(exercise)
    finally:
        adapter.close()


# --- 12: A -> B, and a Hub opened again finds B everywhere ---------------------------------


def test_a_switch_survives_the_hub_being_opened_again(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    world.running.add(first)
    app = shell(adapter)

    async def switch() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.show_section(Section.INSTALLATION)
            await settle(pilot, 4)
            ask_twice(app.query_one(InstallPanel), second)
            await settle(pilot, 4)
            await finish(pilot, adapter)
            assert_everything_on(app, adapter, second, "pepe-8gb", await surfaces(pilot))

    try:
        run(switch)
    finally:
        adapter.close()

    reopened = hub(tmp_path, world)
    again = shell(reopened)

    async def after_restart() -> None:
        async with again.run_test(size=(120, 40)) as pilot:
            await settle(pilot, 10)
            assert_everything_on(again, reopened, second, "pepe-8gb", await surfaces(pilot))

    try:
        run(after_restart)
    finally:
        reopened.close()
