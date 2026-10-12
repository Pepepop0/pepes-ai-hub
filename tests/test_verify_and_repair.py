"""Checking an installation reads; repairing it is asked for, and leaves it as it was.

Against the real composition: the real adapter, the real ``StackApplication``,
the real first-run coordinator and registry; only the OS seams are controlled
(see ``first_run_support``). "Nothing started" is the controlled launcher's
own record of every start; "nothing downloaded" is the artifact transport's
and the Ollama store's records; "nothing written" is the files themselves.
"""

from __future__ import annotations

from first_run_support import CHAT_REFERENCE
from test_delivery_wave_a import composition  # noqa: F401 -- the real composition fixture

from local_ai_stack.config import config_path
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.tui.application import InstallationCheck, InstallSelection


def installed_and_stopped(composition):  # noqa: F811 -- the fixture, passed in
    world, adapter, _ = composition()
    adapter.install(InstallSelection("capella"))
    adapter.wait(30)
    assert adapter.installation().ready
    adapter.stop_stack()
    adapter.wait(30)
    assert not adapter.backend.status("capella").stack_up
    return world, adapter


def fingerprint(world, adapter):
    """Everything a check must not touch, as it is right now."""
    files = (config_path(adapter.backend.layout), adapter.selection_path)
    return (
        tuple(world.launcher.starts),
        tuple(world.launcher.stops),
        len(world.artifacts.requests),
        tuple(world.ollama.pulls),
        {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files},
        adapter.active_root(),
    )


# --- Verificar instalação: read-only ------------------------------------------------------


def test_verifying_a_stopped_installation_starts_downloads_and_writes_nothing(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    before = fingerprint(world, adapter)

    check = adapter.verify_installation()

    assert isinstance(check, InstallationCheck)
    assert check.ok, check.problems
    assert fingerprint(world, adapter) == before, "a check changes nothing"
    assert not adapter.backend.status("capella").stack_up, "and starts nothing"
    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED, (
        "a check is not an operation: the last one is still the stop"
    )


def test_verifying_reports_what_a_repair_would_fix(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    chat = next(entry for entry in world.ollama.installed if entry.name == CHAT_REFERENCE)
    world.ollama.installed.remove(chat)
    before = fingerprint(world, adapter)

    check = adapter.verify_installation()

    assert not check.ok
    assert any("chat" in problem or chat.name in problem for problem in check.problems)
    assert fingerprint(world, adapter) == before, "finding a problem fixes nothing"


def test_with_ollama_stopped_models_are_not_checked_and_not_reported_as_problems(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    world.ollama.reachable = False
    try:
        check = adapter.verify_installation()
    finally:
        world.ollama.reachable = True
    assert check.ok, check.problems
    assert check.notes and "não foram conferidos" in check.notes[0]


# --- Reparar: explicit, and the stack ends the way it was found ----------------------------


def test_repairing_a_stopped_installation_leaves_it_stopped(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    starts_before = list(world.launcher.starts)

    adapter.repair()
    adapter.wait(30)

    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    status = adapter.backend.status("capella")
    assert not status.stack_up
    assert all(not item.up for item in status.components), "stopped stays stopped"
    started = world.launcher.starts[len(starts_before) :]
    # Only what the repair needed, and only for as long as it needed it.
    assert set(started) <= {"ollama"}
    assert "open-webui" not in started and "comfyui" not in started
    assert world.launcher.registry.load().entries == {}, "nothing it started is left behind"


def test_an_explicit_repair_still_repairs(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    chat = next(entry for entry in world.ollama.installed if entry.name == CHAT_REFERENCE)
    world.ollama.installed.remove(chat)
    assert not adapter.verify_installation().ok

    adapter.repair()
    adapter.wait(30)

    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert chat.name in world.ollama.pulls, "the missing model was acquired again"
    assert adapter.verify_installation().ok
    assert not adapter.backend.status("capella").stack_up, "and the stack is still stopped"


def test_repairing_a_running_installation_leaves_it_running(composition):  # noqa: F811
    world, adapter = installed_and_stopped(composition)
    adapter.start_stack()
    adapter.wait(30)
    assert adapter.backend.status("capella").stack_up
    stops_before = list(world.launcher.stops)

    adapter.repair()
    adapter.wait(30)

    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert adapter.backend.status("capella").stack_up, "running stays running"
    assert world.launcher.stops == stops_before, "nothing was stopped"


# --- a stopped installation is still an installation --------------------------------------


def test_a_stopped_installation_is_shown_as_installed_and_can_be_started(composition):  # noqa: F811
    """With Ollama stopped its models cannot be read; that must not read as "nothing
    installed" -- which also disabled Start, the one way to make them readable."""
    import asyncio

    from test_delivery_wave_a import settle, tui
    from textual.widgets import Button, Static

    world, adapter = installed_and_stopped(composition)
    world.ollama.reachable = False
    try:
        assert adapter.installation().ready
        view, _, _ = adapter.projections()
        assert view.ready
        [row] = [row for row in adapter.installation_summaries() if row.active]
        assert row.status == "ready"
        app, _ = tui(adapter)

        async def exercise():
            async with app.run_test(size=(120, 40)) as pilot:
                await settle(pilot)
                overview = str(app.query_one("#dashboard-overview", Static).content)
                assert "Nenhuma stack instalada" not in overview
                assert not app.query_one("#dashboard-start", Button).disabled

        asyncio.run(exercise())
    finally:
        world.ollama.reachable = True
