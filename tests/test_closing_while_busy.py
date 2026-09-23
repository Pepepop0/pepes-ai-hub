"""Closing the Hub, and the work that must not be cut in half.

An operation runs in its own thread and cannot be cancelled in this version,
so the Hub refuses to close while one is under way -- from the moment the
command is launched, not from the moment its first event happens to arrive.
The pet is the other half of the same idea: what nobody can see costs nothing.
"""

from __future__ import annotations

import asyncio

from hub_fakes import FakeHubApplication, ready_application
from test_app import all_profiles, integration_snapshot, reference_facts, stopped_runtime

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.tui.widgets import PetSlot, RightRail


def hub(application: FakeHubApplication) -> LocalAIStackApp:
    return LocalAIStackApp(
        facts=reference_facts(),
        profiles=all_profiles(),
        runtime_snapshot=stopped_runtime(),
        integration=integration_snapshot(),
        application=application,
        open_browser=lambda url: True,
        first_run_wizard=False,
    )


def run(coroutine) -> None:
    asyncio.run(coroutine())


async def settle(pilot, times: int = 6) -> None:
    for _ in range(times):
        await pilot.pause()


def refusal(app) -> str:
    """What the screen in front of the person says about the refusal."""
    return str(app.query_one("#dashboard-notice").content)


# --- an operation that has not reported yet is still an operation -------------------


def test_the_hub_refuses_to_close_while_a_command_has_not_reported_yet():
    """The blocker: between launching and the first event, q closed the Hub."""

    async def exercise() -> None:
        fake = ready_application()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)

            app.run_stack_command("start")
            assert fake.calls, "the command was launched"
            assert app.operation_view is None, "and has not reported anything yet"
            assert app.busy_with_operation()

            await pilot.press("q")
            await settle(pilot, 4)

            assert app.is_running, "the Hub stayed open"
            assert "ainda está em andamento" in refusal(app)

    run(exercise)


def test_the_hub_closes_once_the_operation_has_ended():
    async def exercise() -> None:
        fake = ready_application()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.run_stack_command("start")
            while fake.step() is not None:
                await pilot.pause()
            await settle(pilot, 4)

            assert app.operation_view is not None and app.operation_view.terminal
            assert not app.busy_with_operation()

            await pilot.press("q")
            await settle(pilot, 4)
            assert not app.is_running, "nothing was running, so it closed"

    run(exercise)


def test_a_command_that_never_started_does_not_hold_the_hub_open():
    async def exercise() -> None:
        fake = ready_application()
        fake.unavailable = True  # the command is refused, so nothing is running
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)

            app.run_stack_command("start")
            await settle(pilot, 3)

            assert not app.busy_with_operation()
            await pilot.press("q")
            await settle(pilot, 4)
            assert not app.is_running

    run(exercise)


def test_an_installation_is_protected_from_the_moment_it_is_submitted():
    async def exercise() -> None:
        from local_ai_stack.installation import InstallLocationMode
        from local_ai_stack.tui.application import InstallSelection

        fake = FakeHubApplication()
        app = hub(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)

            app._start_install(InstallSelection("pepe", InstallLocationMode.DEFAULT))
            await settle(pilot, 6)

            assert app.busy_with_operation()
            await pilot.press("q")
            await settle(pilot, 4)
            assert app.is_running

            while fake.step() is not None:
                await pilot.pause()
            await settle(pilot, 4)
            assert not app.busy_with_operation()

    run(exercise)


# --- the pet only costs anything while it is on screen ------------------------------


def test_the_pet_stops_animating_when_the_rail_is_too_small_to_show_it():
    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            pet = app.query_one(PetSlot)
            assert pet.animating, "on screen, the pet moves"

            await pilot.resize_terminal(80, 24)
            await settle(pilot, 6)

            assert not app.query_one(RightRail).display
            assert not pet.animating, "a pet nobody can see does not animate"

    run(exercise)


def test_the_pet_animates_again_when_the_terminal_has_room_for_it():
    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            pet = app.query_one(PetSlot)
            assert not pet.animating

            await pilot.resize_terminal(120, 40)
            await settle(pilot, 6)

            assert app.query_one(RightRail).display
            assert pet.animating

    run(exercise)


def test_a_hidden_pet_draws_no_frames():
    """Not just a flag: the hidden pet's renderer is never asked for a frame."""

    async def exercise() -> None:
        app = hub(ready_application())
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            pet = app.query_one(PetSlot)
            await pilot.resize_terminal(80, 24)
            await settle(pilot, 6)

            asked: list[float | None] = []
            animator = pet.animator
            assert animator is not None
            original = animator.current_text

            def counting(now: float | None = None) -> str:
                asked.append(now)
                return original(now)

            animator.current_text = counting  # type: ignore[method-assign]
            await asyncio.sleep(PetSlot.REFRESH_INTERVAL * 4)
            await settle(pilot, 6)
            assert asked == [], "a paused timer asks for nothing"

    run(exercise)
