"""A managed service is recognised by its tree, not only by its root pid.

The registry knows the pid the launcher spawned. The process that holds the
service port is usually a descendant of it: ``open-webui.exe`` starts a venv
interpreter that starts the real one; ComfyUI's venv ``python.exe`` runs
``main.py`` in a child. Every fake before this one gave the root the port, so
the gap between "the pid we registered" and "the pid on the port" was never
crossed, and a second first-run on a live stack reported its own services as
strangers squatting on managed ports.

Two halves. The unit half proves the ancestry primitive on hand-built process
tables -- what it accepts and, more importantly, everything it refuses. The
composition half runs the first-run flow with the port two hops below the
root, in the same session (job proof) and after a Hub restart (ancestry
proof), and then proves that nothing external is ever mistaken for ours.
"""

from __future__ import annotations

import pytest
from first_run_support import controlled_stack, restarted_hub

from local_ai_stack.inventory import (
    Listener,
    ListenerReport,
    ProcessInfo,
    ProcessReport,
    ScanOutcome,
)
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.processes import (
    OPEN_WEBUI_PROCESS,
    ProcessOrigin,
    RuntimeProcessState,
    discover_process,
)
from local_ai_stack.registry import (
    MAX_DESCENT_DEPTH,
    ManagedProcess,
    ManagedProcessRegistry,
    proven_descendants,
)

ROOT = ProcessInfo(1000, "open-webui.exe", r"C:\hub\open-webui.exe", "open-webui serve", 100.0, 7)
SHIM = ProcessInfo(
    1001, "python.exe", r"C:\hub\.venv\python.exe", "python open-webui", 100.01, 1000
)
SERVER = ProcessInfo(1002, "python.exe", r"C:\py\python.exe", "python open-webui", 100.02, 1001)
UNRELATED = ProcessInfo(2000, "python.exe", r"C:\py\python.exe", "python other", 100.03, 7)


def report(*processes: ProcessInfo) -> ProcessReport:
    return ProcessReport(ScanOutcome.OK, tuple(processes))


# --- the primitive ---------------------------------------------------------


def test_a_grandchild_whose_chain_reaches_the_root_is_proven():
    assert proven_descendants(ROOT, report(ROOT, SHIM, SERVER, UNRELATED)) == {1001, 1002}


def test_the_root_itself_is_not_its_own_descendant():
    assert proven_descendants(ROOT, report(ROOT)) == frozenset()


def test_a_process_whose_parent_is_not_in_the_table_is_not_proven():
    # The shim exited: the server's parent pid names nothing. Nothing is
    # guessed past a missing hop, even though the grandparent is our root.
    assert proven_descendants(ROOT, report(ROOT, SERVER)) == frozenset()


def test_a_reused_parent_pid_is_refused_by_creation_time():
    # 1001 died and the OS handed its number to a process born *after* the
    # server. A real parent is always older than its child, so this "parent"
    # cannot be one.
    recycled = ProcessInfo(1001, "notepad.exe", r"C:\w\notepad.exe", "notepad", 500.0, 1000)
    assert proven_descendants(ROOT, report(ROOT, recycled, SERVER)) == {1001}
    assert 1002 not in proven_descendants(ROOT, report(ROOT, recycled, SERVER))


def test_a_reused_root_pid_below_a_child_is_refused():
    # The child claims ppid 1000, but this 1000 is younger than the child: our
    # root got a number that used to belong to the child's real parent.
    younger_root = ProcessInfo(1000, "open-webui.exe", ROOT.exe, ROOT.cmdline, 200.0, 7)
    orphan = ProcessInfo(1001, "python.exe", SHIM.exe, SHIM.cmdline, 150.0, 1000)
    assert proven_descendants(younger_root, report(younger_root, orphan)) == frozenset()


def test_a_missing_creation_time_on_either_side_fails_closed():
    blind_child = ProcessInfo(1001, "python.exe", SHIM.exe, SHIM.cmdline, None, 1000)
    assert proven_descendants(ROOT, report(ROOT, blind_child)) == frozenset()
    blind_root = ProcessInfo(1000, "open-webui.exe", ROOT.exe, ROOT.cmdline, None, 7)
    assert proven_descendants(blind_root, report(blind_root, SHIM)) == frozenset()


def test_a_missing_parent_pid_fails_closed():
    no_parent = ProcessInfo(1001, "python.exe", SHIM.exe, SHIM.cmdline, 100.01, None)
    assert proven_descendants(ROOT, report(ROOT, no_parent)) == frozenset()


def test_the_same_executable_name_proves_nothing():
    # Identical name, path and command line as our server; a different tree.
    lookalike = ProcessInfo(3000, SERVER.name, SERVER.exe, SERVER.cmdline, 100.05, 7)
    assert 3000 not in proven_descendants(ROOT, report(ROOT, SHIM, SERVER, lookalike))


def test_a_cycle_in_parent_pids_ends_the_walk():
    a = ProcessInfo(5000, "a.exe", None, "", 100.0, 5001)
    b = ProcessInfo(5001, "b.exe", None, "", 100.0, 5000)
    assert proven_descendants(ROOT, report(ROOT, a, b)) == frozenset()


def test_a_process_that_is_its_own_parent_ends_the_walk():
    loop = ProcessInfo(5000, "a.exe", None, "", 100.0, 5000)
    assert proven_descendants(ROOT, report(ROOT, loop)) == frozenset()


def test_the_walk_is_bounded():
    chain = [ROOT]
    for depth in range(1, MAX_DESCENT_DEPTH + 3):
        chain.append(ProcessInfo(1000 + depth, "p.exe", None, "", 100.0 + depth, 999 + depth))
    proven = proven_descendants(ROOT, report(*chain))
    assert 1000 + MAX_DESCENT_DEPTH in proven
    assert 1000 + MAX_DESCENT_DEPTH + 1 not in proven


def test_an_unusable_report_proves_nothing():
    assert proven_descendants(ROOT, ProcessReport(ScanOutcome.UNAVAILABLE)) == frozenset()


# --- through the registry --------------------------------------------------


def make_registry(tmp_path) -> ManagedProcessRegistry:
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")
    registry.register(ManagedProcess("open-webui", 1000, ROOT.exe, ("open-webui",), 1.0, 100.0))
    return registry


def test_managed_pids_include_the_descendants_of_a_live_root(tmp_path):
    registry = make_registry(tmp_path)
    assert registry.managed_pids(processes=report(ROOT, SHIM, SERVER, UNRELATED)) == {
        1000,
        1001,
        1002,
    }


def test_a_recycled_root_owns_nothing_not_even_a_convincing_tree(tmp_path):
    # Same pid as we registered, different creation time: our process is gone
    # and this one is a stranger. Its children are strangers too, however well
    # their chain lines up.
    registry = make_registry(tmp_path)
    impostor = ProcessInfo(1000, ROOT.name, ROOT.exe, ROOT.cmdline, 900.0, 7)
    child = ProcessInfo(1001, SHIM.name, SHIM.exe, SHIM.cmdline, 900.01, 1000)
    assert registry.managed_pids(processes=report(impostor, child)) == frozenset()


def test_without_a_process_table_only_roots_are_answered(tmp_path, monkeypatch):
    from local_ai_stack import registry as registry_module
    from local_ai_stack.inventory import LookupOutcome, ProcessLookup

    registry = make_registry(tmp_path)
    monkeypatch.setattr(
        registry_module, "lookup_process", lambda pid: ProcessLookup(LookupOutcome.FOUND, ROOT)
    )
    assert registry.managed_pids() == {1000}


def test_discovery_reports_a_child_listener_as_managed():
    listeners = ListenerReport(ScanOutcome.OK, (Listener("127.0.0.1", 8080, 1002),))
    status = discover_process(
        OPEN_WEBUI_PROCESS,
        processes=report(ROOT, SHIM, SERVER),
        listeners=listeners,
        managed_pids=frozenset({1000, 1001, 1002}),
    )
    assert status.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.MANAGED
    assert 1002 in status.pids


def test_discovery_reports_an_unproven_listener_as_external():
    listeners = ListenerReport(ScanOutcome.OK, (Listener("127.0.0.1", 8080, 2000),))
    status = discover_process(
        OPEN_WEBUI_PROCESS,
        processes=report(UNRELATED),
        listeners=listeners,
        managed_pids=frozenset(),
    )
    assert status.origin is ProcessOrigin.EXTERNAL


# --- composition: the first run --------------------------------------------

TEST_PROFILE = "test"


def shipped_test_yaml() -> str:
    from local_ai_stack.installation import detect_repo_root

    return (detect_repo_root() / "profiles" / "test.yaml").read_text(encoding="utf-8")


@pytest.fixture
def live_stack():
    """A stack whose services serve from two hops below their roots."""
    built = controlled_stack(
        profile_yaml=shipped_test_yaml(), profile_id=TEST_PROFILE, listener_depth=2
    )
    try:
        first = built.app.first_run("op-1", TEST_PROFILE)
        assert first.state is OperationState.SUCCEEDED, [d.message for d in first.diagnostics]
        yield built
    finally:
        built.release()


def listener_pid(built, port: int) -> int:
    (listener,) = [item for item in built.inventory.listeners if item.port == port]
    return listener.pid


def test_the_fixture_puts_the_port_below_the_root(live_stack):
    for service_id, port in (("open-webui", 8080), ("comfyui", 8188), ("ollama", 11434)):
        root = live_stack.app.registry().load().get(service_id).pid
        assert listener_pid(live_stack, port) == root + 2, service_id


def test_a_second_run_in_the_same_session_recognises_its_own_stack(live_stack):
    result = live_stack.app.first_run("op-2", TEST_PROFILE)
    assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
    assert live_stack.app.last_first_run.did_nothing
    assert live_stack.launcher.starts == ["ollama", "open-webui", "comfyui"]


def test_a_second_run_after_a_hub_restart_recognises_its_own_stack(live_stack):
    """The job handles died with the session; ancestry is all that is left."""
    again = restarted_hub(live_stack)
    assert again.launcher().tree_pids("open-webui", listener_pid(live_stack, 8080)) is None

    result = again.first_run("op-2", TEST_PROFILE)

    assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
    assert again.last_first_run.did_nothing
    assert again.launcher().starts == []
    assert listener_pid(live_stack, 8080) in again.managed_pids()
    assert listener_pid(live_stack, 8188) in again.managed_pids()


def test_status_after_a_restart_shows_the_stack_as_managed_and_up(live_stack):
    status = restarted_hub(live_stack).status(TEST_PROFILE)
    assert status.stack_up
    for component in status.components:
        assert component.runtime == "RUNNING", component


def test_lifecycle_acts_on_the_tree_the_launcher_proves(live_stack):
    """Stop, start and restart keep working with the port below the root."""
    app = live_stack.app
    controller = app._controller(app.profile(TEST_PROFILE))
    live_stack.inventory.occupy(9999)  # a stranger, on another port, that must survive
    before = set(live_stack.inventory.pids())

    current = controller.status("open-webui")
    assert current.runtime.origin is ProcessOrigin.MANAGED and current.managed is not None
    stopped = controller.stop("open-webui")
    assert stopped.stopped
    assert not any(item.port == 8080 for item in live_stack.inventory.listeners)
    assert 9999 in {item.port for item in live_stack.inventory.listeners}

    started = controller.start("open-webui", wait=True)
    assert started.ready, started.detail
    assert listener_pid(live_stack, 8080) == started.launch.pid + 2

    restarted = controller.restart("open-webui", wait=True)
    assert restarted.restarted and restarted.start.ready, restarted.detail
    gone = before - set(live_stack.inventory.pids())
    assert all(pid in live_stack.launcher.spawned for pid in gone), "a stranger was stopped"


# --- composition: nothing external is adopted -------------------------------


def test_a_stranger_on_the_port_is_still_a_collision_after_a_restart(live_stack):
    # Our tree is gone; something unrelated took the port. The registry entry
    # is stale, so the unrelated process must be a collision, not our stack.
    root = live_stack.app.registry().load().get("open-webui").pid
    for pid in (root, root + 1, root + 2):
        live_stack.inventory.kill(pid)
    live_stack.inventory.occupy(8080)

    result = restarted_hub(live_stack).first_run("op-2", TEST_PROFILE)

    assert result.state is OperationState.FAILED
    assert any("8080" in d.message and "occupied" in d.message for d in result.diagnostics)


def test_a_same_named_process_on_the_port_is_not_adopted(live_stack):
    root_entry = live_stack.app.registry().load().get("open-webui")
    for offset in range(3):
        live_stack.inventory.kill(root_entry.pid + offset)
    # Same executable path as the tree we started, parent unknown to us.
    live_stack.inventory.spawn(7777, root_entry.executable, 8080, ppid=1, create_time=50.0)

    result = restarted_hub(live_stack).first_run("op-2", TEST_PROFILE)

    assert result.state is OperationState.FAILED
    assert any("7777" in d.message for d in result.diagnostics)


def test_a_fabricated_parent_pid_older_than_the_root_is_not_adopted(live_stack):
    # A stranger whose ppid points at our root but which was created before
    # the root existed: that ppid is stale, our root merely reused the number.
    root_entry = live_stack.app.registry().load().get("open-webui")
    live_stack.inventory.kill(root_entry.pid + 2)  # our server is gone...
    live_stack.inventory.spawn(  # ...and this took its port, claiming our root
        8888,
        r"C:\Other\python.exe",
        8080,
        ppid=root_entry.pid,
        create_time=root_entry.create_time - 5,
    )

    result = restarted_hub(live_stack).first_run("op-2", TEST_PROFILE)

    assert result.state is OperationState.FAILED
    assert any("8888" in d.message for d in result.diagnostics)


def test_a_recycled_root_pid_makes_the_whole_tree_foreign(live_stack):
    root_entry = live_stack.app.registry().load().get("open-webui")
    for offset in range(3):
        live_stack.inventory.kill(root_entry.pid + offset)
    # The same pids come back, later, with a perfectly ordered chain -- but
    # the root is not the process we registered.
    born = root_entry.create_time + 1000
    live_stack.inventory.spawn(root_entry.pid, root_entry.executable, ppid=1, create_time=born)
    live_stack.inventory.spawn(
        root_entry.pid + 1, "worker", ppid=root_entry.pid, create_time=born + 0.001
    )
    live_stack.inventory.spawn(
        root_entry.pid + 2, "server", 8080, ppid=root_entry.pid + 1, create_time=born + 0.002
    )

    again = restarted_hub(live_stack)
    assert root_entry.pid + 2 not in again.managed_pids()
    result = again.first_run("op-2", TEST_PROFILE)
    assert result.state is OperationState.FAILED
    assert any("8080" in d.message for d in result.diagnostics)


def test_an_unrelated_descendant_of_a_stranger_is_not_ours(live_stack):
    live_stack.inventory.spawn(6000, r"C:\Other\tool.exe", ppid=1, create_time=10.0)
    live_stack.inventory.spawn(6001, r"C:\Other\child.exe", ppid=6000, create_time=10.5)
    managed = restarted_hub(live_stack).managed_pids()
    assert 6000 not in managed and 6001 not in managed


def test_an_external_install_serving_through_its_own_child_is_still_external():
    """The external-conflict policy is untouched by descent proof.

    An external Ollama whose port is held by a child of its own root is an
    external conflict exactly as it was when the root held the port: resolution
    is required, the child is never counted as ours, and after INSTALL_ALONGSIDE
    the external tree is left running.
    """
    from first_run_support import CAPELLA

    from local_ai_stack.e2e.fixtures import external_install
    from local_ai_stack.external_conflict_contracts import (
        ConflictPolicySelection,
        ExternalConflictPolicy,
    )
    from local_ai_stack.external_conflicts import ALONGSIDE_PORTS, ConflictResolutionRequired

    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188, ALONGSIDE_PORTS["ollama"]))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.spawn(9300, str(ollama_ext.path), ppid=1, create_time=5.0)
        built.inventory.spawn(9301, str(ollama_ext.path), 11434, ppid=9300, create_time=5.1)
        original = built.app.deps.launcher

        def make(registry):
            launcher = original(registry)
            launcher.ready_ports["ollama"] = ALONGSIDE_PORTS["ollama"]
            return launcher

        built.app.deps.launcher = make

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        assert [str(c.service_id) for c in report.conflicts] == ["ollama"]
        assert not built.app.managed_pids() & {9300, 9301}

        built.app.resolve_conflict(
            ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE)
        )
        result = built.app.first_run("op-2", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert {9300, 9301} <= set(built.inventory.pids())
        assert not built.app.managed_pids() & {9300, 9301}
    finally:
        built.release()
        ollama_ext.release()
