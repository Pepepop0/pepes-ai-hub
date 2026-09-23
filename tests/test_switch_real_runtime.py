"""Stop-and-switch against the real runtime: real StackApplications, real registry.

Two installations of the shipped Capella profile are installed through the
real adapter and the real first run, on one controlled machine: one process
table, one listener table, shared by both, exactly as two folders on one PC
share one set of ports. Only the OS seams are controlled (see
``first_run_support``). What decides whether a switch may go through is the
production code: ``StackApplication.stop_stack`` and its ``OperationResult``,
the managed-process registry behind ``surviving_services``, and the preflight's
own collision rule behind ``endpoint_collisions``.
"""

from __future__ import annotations

import dataclasses

from test_delivery_wave_a import composition  # noqa: F401 -- the real composition fixture

from local_ai_stack.installation import InstallLocationMode
from local_ai_stack.installations import read_state
from local_ai_stack.operation_contracts import OperationResult, OperationState
from local_ai_stack.stack_application import StackApplication
from local_ai_stack.tui.application import InstallSelection

REFUSED = "Não foi possível trocar de instalação porque os serviços necessários ainda estão em uso."


def two_installations(composition):  # noqa: F811 -- the fixture, passed in
    """B installed and stopped, then A installed and left running, as in use."""
    world, adapter, reopen = composition()
    second = world.root.path / "second"
    adapter.install(InstallSelection("capella", InstallLocationMode.CUSTOM, str(second)))
    adapter.wait(30)
    assert adapter.active_root() == second
    adapter.stop_stack()
    adapter.wait(30)
    adapter.install(InstallSelection("capella"))
    adapter.wait(30)
    first = adapter.active_root()
    assert first is not None and first != second
    assert adapter.backend.status("capella").stack_up, "the installation in use is running"
    assert adapter.plan_switch(second).must_stop
    return world, adapter, reopen, first, second


def assert_stayed_on(adapter, first, second) -> None:
    """The first installation -- the default one -- is still selected; second is not bound."""
    assert adapter._candidate is None and adapter._busy is False and adapter._idle.is_set()
    assert adapter.active_root() == first
    assert adapter.backend.installation.root == first
    assert adapter.config.installation.mode is InstallLocationMode.DEFAULT
    persisted = read_state(adapter.selection_path).active
    assert persisted.installation.mode is InstallLocationMode.DEFAULT
    assert persisted.installation.root != second


# --- the real stop contract ------------------------------------------------------------


def test_the_real_stop_returns_an_operation_result_and_nothing_else(composition):  # noqa: F811
    _, adapter, _, _, _ = two_installations(composition)
    backend: StackApplication = adapter.backend

    result = backend.stop_stack("stop-contract", "capella")

    assert type(result) is OperationResult
    assert result.state is OperationState.SUCCEEDED
    assert "left_running" not in {field.name for field in dataclasses.fields(result)}
    assert not hasattr(backend, "listening_ports")
    # After a real stop, the real post-condition is clear.
    assert backend.surviving_services() == ()
    assert backend.endpoint_collisions("capella") == ()


# --- the switch, decided by the real runtime ---------------------------------------------


def test_with_the_machine_clear_the_real_switch_goes_through(composition):  # noqa: F811
    _, adapter, reopen, _, second = two_installations(composition)

    adapter.stop_and_switch(second)
    adapter.wait(30)

    assert adapter.last_switch_refusal() is None
    assert adapter._candidate is None and adapter._busy is False
    assert adapter.active_root() == second
    assert adapter.backend.installation.root == second
    assert adapter.config.installation.root == second
    assert read_state(adapter.selection_path).active_root == second
    assert not adapter.backend.status("capella").stack_up, "switching starts nothing"

    # A Hub opened again finds the switch.
    again = reopen()
    assert again.active_root() == second
    assert again.backend.installation.root == second


def test_a_managed_process_the_stop_left_alive_denies_the_real_switch(composition):  # noqa: F811
    world, adapter, _, first, second = two_installations(composition)
    backend = adapter.backend
    entry = backend.registry().load().get("ollama")
    process = next(item for item in world.inventory.processes if item.pid == entry.pid)
    real_stop = backend.stop_stack

    def stop_that_leaves_ollama(operation_id, profile_id, *, emit=None):
        result = real_stop(operation_id, profile_id, emit=emit)
        # It reported success; the process is back in the table and in the
        # registry, with the identity the registry proves it by.
        world.inventory.processes.append(process)
        backend.registry().register(entry)
        return result

    backend.stop_stack = stop_that_leaves_ollama
    adapter.stop_and_switch(second)
    adapter.wait(30)

    refusal = adapter.last_switch_refusal()
    assert refusal is not None and refusal.startswith(REFUSED)
    assert "ollama" in refusal
    assert_stayed_on(adapter, first, second)


def test_an_external_listener_on_a_needed_port_denies_the_real_switch(composition):  # noqa: F811
    world, adapter, _, first, second = two_installations(composition)
    backend = adapter.backend
    real_stop = backend.stop_stack

    def stop_then_a_stranger_takes_the_port(operation_id, profile_id, *, emit=None):
        result = real_stop(operation_id, profile_id, emit=emit)
        world.inventory.occupy(8080, pid=9876)
        return result

    backend.stop_stack = stop_then_a_stranger_takes_the_port
    adapter.stop_and_switch(second)
    adapter.wait(30)

    refusal = adapter.last_switch_refusal()
    assert refusal is not None and refusal.startswith(REFUSED)
    assert "8080" in refusal and "9876" in refusal
    assert_stayed_on(adapter, first, second)
    assert 9876 in world.inventory.pids(), "the external process is not touched"


def test_a_listener_scan_that_fails_denies_the_real_switch(composition):  # noqa: F811
    from local_ai_stack.inventory import ScanOutcome

    world, adapter, _, first, second = two_installations(composition)
    backend = adapter.backend
    real_stop = backend.stop_stack

    def stop_then_the_scan_fails(operation_id, profile_id, *, emit=None):
        result = real_stop(operation_id, profile_id, emit=emit)
        world.inventory.listener_outcome = ScanOutcome.UNAVAILABLE
        return result

    backend.stop_stack = stop_then_the_scan_fails
    try:
        adapter.stop_and_switch(second)
        adapter.wait(30)
        refusal = adapter.last_switch_refusal()
        assert refusal is not None and refusal.startswith(REFUSED)
        assert_stayed_on(adapter, first, second)
    finally:
        world.inventory.listener_outcome = ScanOutcome.OK


def test_a_stop_that_leaves_proven_members_running_is_a_failed_stop(composition):  # noqa: F811
    """The launcher's own report of survivors reaches the real operation result."""
    _, adapter, _, first, second = two_installations(composition)
    backend = adapter.backend
    launcher = backend.launcher()
    real = launcher.stop

    def stop_leaving_a_child(service_id, **kwargs):
        result = real(service_id, **kwargs)
        if service_id != "ollama":
            return result
        child = result.tree_pids[-1] if result.tree_pids else None
        return dataclasses.replace(result, left_running=(child,) if child else (4242,))

    launcher.stop = stop_leaving_a_child
    adapter.stop_and_switch(second)
    adapter.wait(30)

    refusal = adapter.last_switch_refusal()
    assert refusal is not None and "parar os serviços" in refusal
    assert adapter.current_operation().last_event.state is OperationState.FAILED
    assert_stayed_on(adapter, first, second)
