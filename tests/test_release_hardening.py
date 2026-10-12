"""Release-candidate composition: the whole product path, in the real shape.

Every stack here serves from two hops below its registered root, the way a
real Open WebUI and ComfyUI do, so nothing below passes because the fake gave
the root the port. The path under proof:

    fresh root -> first run -> second run -> status / start / stop / restart
    -> Hub reopened -> Open WebUI action -> stop everything

plus the plausible states a machine is found in before any of that: a
registry naming a dead pid, a service installed but stopped, a server child
gone while its launcher lives, a root pid the OS handed to a stranger.
Expected everywhere: nothing external is touched, nothing is guessed, and a
retry gets the user back to a working stack without deleting the root.
"""

from __future__ import annotations

import pytest
from first_run_support import (
    CAPELLA,
    CAPELLA_PORTS,
    RTX_5090_MIB,
    capella_yaml,
    controlled_stack,
    restarted_hub,
)

from local_ai_stack.installation import detect_repo_root
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.processes import ProcessOrigin
from local_ai_stack.services import ServiceState

TEST = "test"
PEPE = "pepe"
RTX_5070_MIB = 12227
SERVICES = ("ollama", "open-webui", "comfyui")


def shipped(profile_id: str) -> str:
    return (detect_repo_root() / "profiles" / f"{profile_id}.yaml").read_text(encoding="utf-8")


def stack_for(profile_id: str, **kwargs):
    vram = RTX_5070_MIB if profile_id == PEPE else RTX_5090_MIB
    return controlled_stack(
        profile_yaml=shipped(profile_id),
        profile_id=profile_id,
        vram_mib=vram,
        listener_depth=2,
        **kwargs,
    )


def succeeded(result) -> bool:
    assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
    return True


def listener_pid(built, port: int) -> int | None:
    pids = [item.pid for item in built.inventory.listeners if item.port == port]
    return pids[0] if pids else None


def root_pid(built, service_id: str) -> int | None:
    entry = built.app.registry().load().get(service_id)
    return entry.pid if entry is not None else None


def kill_tree(built, service_id: str) -> None:
    """The whole tree dies -- a crash, a reboot -- and the registry is not told."""
    root = root_pid(built, service_id)
    for pid in built.launcher.started_trees.get(service_id, (root,)):
        built.inventory.kill(pid)


# --- 1. first run, every shipped profile -------------------------------------


@pytest.mark.parametrize("profile_id", [TEST, CAPELLA, PEPE])
def test_every_shipped_profile_completes_a_first_run(profile_id):
    built = stack_for(profile_id)
    try:
        assert succeeded(built.app.first_run("op-1", profile_id))
        report = built.app.last_first_run
        assert set(report.installed) == set(SERVICES)
        assert report.configured
        assert built.launcher.starts == list(SERVICES)
        for service_id in SERVICES:
            assert listener_pid(built, CAPELLA_PORTS[service_id]) == root_pid(built, service_id) + 2
        status = built.app.status(profile_id)
        assert status.installation_ready and status.stack_up
        profile = built.app.profile(profile_id)
        if profile.enables("chat"):
            assert built.ollama.pulls, "chat enabled but no model was pulled"
        else:
            assert built.ollama.pulls == []
        if profile.enables("image_generation"):
            assert any("/models/" in url for url in built.artifacts.requests)
        else:
            assert not any("/models/" in url for url in built.artifacts.requests)
    finally:
        built.release()


def test_an_invalid_profile_never_reaches_installation():
    yaml_text = capella_yaml().replace("primary: ollama/qwen3_8_27b_uncensored_q5", "primary: TBD")
    assert "primary: TBD" in yaml_text
    with pytest.raises(Exception, match="chat"):
        controlled_stack(profile_yaml=yaml_text)


# --- 2. second run ------------------------------------------------------------


@pytest.mark.parametrize("profile_id", [TEST, CAPELLA, PEPE])
def test_a_second_run_does_nothing_in_the_same_session(profile_id):
    built = stack_for(profile_id)
    try:
        assert succeeded(built.app.first_run("op-1", profile_id))
        downloads, pulls, starts = (
            len(built.artifacts.requests),
            list(built.ollama.pulls),
            list(built.launcher.starts),
        )
        assert succeeded(built.app.first_run("op-2", profile_id))
        report = built.app.last_first_run
        assert report.did_nothing
        assert report.installed == [] and report.acquired == []
        assert not report.configuration_changed
        assert len(built.artifacts.requests) == downloads
        assert built.ollama.pulls == pulls
        assert built.launcher.starts == starts
    finally:
        built.release()


@pytest.mark.parametrize("profile_id", [TEST, CAPELLA, PEPE])
def test_a_second_run_does_nothing_after_the_hub_is_reopened(profile_id):
    built = stack_for(profile_id)
    try:
        assert succeeded(built.app.first_run("op-1", profile_id))
        downloads, pulls = len(built.artifacts.requests), list(built.ollama.pulls)
        again = restarted_hub(built)
        assert succeeded(again.first_run("op-2", profile_id))
        assert again.last_first_run.did_nothing
        assert again.launcher().starts == []
        assert len(built.artifacts.requests) == downloads and built.ollama.pulls == pulls
        assert again.status(profile_id).stack_up
    finally:
        built.release()


# --- 3. lifecycle, per service ------------------------------------------------


@pytest.fixture
def running():
    built = stack_for(TEST)
    try:
        assert succeeded(built.app.first_run("op-1", TEST))
        yield built
    finally:
        built.release()


@pytest.mark.parametrize("service_id", SERVICES)
def test_each_service_can_be_stopped_started_and_restarted(running, service_id):
    controller = running.app._controller(running.app.profile(TEST))
    port = CAPELLA_PORTS[service_id]
    # Strangers that must survive: unrelated, and one with our own executable.
    running.inventory.occupy(39990)
    running.inventory.spawn(
        7000, running.app.registry().load().get(service_id).executable, 39991, ppid=1
    )
    strangers = {39990, 39991}

    status = controller.status(service_id)
    assert status.origin is ProcessOrigin.MANAGED and status.stoppable
    assert status.health.state is ServiceState.UP

    stopped = controller.stop(service_id)
    assert stopped.stopped, stopped.detail
    assert listener_pid(running, port) is None
    assert running.app.registry().load().get(service_id) is None
    assert controller.status(service_id).health.state is not ServiceState.UP

    started = controller.start(service_id, wait=True)
    assert started.ready, started.detail
    assert listener_pid(running, port) == started.pid + 2
    assert root_pid(running, service_id) == started.pid

    restarted = controller.restart(service_id, wait=True)
    assert restarted.restarted and restarted.start.ready, restarted.detail
    assert listener_pid(running, port) == restarted.start.pid + 2
    assert strangers <= {item.port for item in running.inventory.listeners}
    others = [sid for sid in SERVICES if sid != service_id]
    for other in others:
        assert controller.status(other).health.state is ServiceState.UP, other


# --- 4. stale and abrupt states ----------------------------------------------


def test_a_registry_naming_a_dead_tree_is_stale_not_a_collision(running):
    kill_tree(running, "open-webui")
    again = restarted_hub(running)

    component = again.status(TEST).component("open-webui")
    assert component.runtime == "STOPPED" and component.health != "UP"
    assert "open-webui" not in again.managed_pids()  # the entry proves nothing now

    assert succeeded(again.first_run("op-2", TEST))
    report = again.last_first_run
    assert report.installed == [] and report.started == ["open-webui"]
    assert again.status(TEST).stack_up
    assert root_pid(running, "open-webui") == again.launcher().members["open-webui"][0]


def test_an_installed_but_stopped_stack_is_started_not_reinstalled(running):
    assert succeeded(running.app.stop_stack("op-2", TEST))
    assert running.app.registry().load().entries == {}
    assert not any(item.port in CAPELLA_PORTS.values() for item in running.inventory.listeners)
    downloads = len(running.artifacts.requests)

    again = restarted_hub(running)
    assert succeeded(again.start_stack("op-3", TEST))
    assert again.status(TEST).stack_up
    assert len(running.artifacts.requests) == downloads
    assert again.launcher().starts == list(SERVICES)


def test_a_server_child_that_died_under_a_living_launcher_is_recoverable(running):
    # The interpreter serving the port is gone; the registered root lives on.
    running.inventory.kill(listener_pid(running, 8080))
    status = running.app.status(TEST).component("open-webui")
    assert status.runtime == "RUNNING" and status.health != "UP"

    started = running.app.start_stack("op-2", TEST)
    assert started.state is OperationState.FAILED
    assert any(d.code == "service-not-ready" for d in started.diagnostics)

    assert succeeded(running.app.restart_stack("op-3", TEST))
    assert running.app.status(TEST).stack_up


def test_a_recycled_root_pid_is_dropped_and_the_stranger_left_alone(running):
    root = root_pid(running, "comfyui")
    kill_tree(running, "comfyui")
    # The OS handed our old root pid to something else, on no port of ours.
    running.inventory.spawn(root, r"C:\Other\tool.exe", 39995, ppid=1, create_time=999.0)

    again = restarted_hub(running)
    component = again.status(TEST).component("comfyui")
    assert component.runtime == "STOPPED"
    assert root not in again.managed_pids()

    assert succeeded(again.first_run("op-2", TEST))
    assert again.last_first_run.started == ["comfyui"]
    assert root in running.inventory.pids(), "the stranger holding our old pid was killed"
    assert root_pid(running, "comfyui") != root


def test_the_reopened_hub_sees_the_same_stack(running):
    before = running.app.status(TEST)
    after = restarted_hub(running).status(TEST)
    assert after.stack_up and after.installation_ready
    assert [(c.service_id, c.ownership, c.health) for c in after.components] == [
        (c.service_id, c.ownership, c.health) for c in before.components
    ]


# --- 7. clean shutdown --------------------------------------------------------


def test_stopping_the_stack_leaves_no_managed_process_listener_or_record(running):
    trees = {sid: running.launcher.members_started(sid) for sid in SERVICES}
    running.inventory.occupy(39990)

    result = running.app.stop_stack("op-2", TEST)

    assert succeeded(result)
    assert set(result.output_references) == set(SERVICES)
    alive = set(running.inventory.pids())
    for service_id, tree in trees.items():
        assert not alive & set(tree), f"{service_id} left {alive & set(tree)} running"
    assert not any(item.port in CAPELLA_PORTS.values() for item in running.inventory.listeners)
    assert 39990 in {item.port for item in running.inventory.listeners}
    assert running.app.registry().load().entries == {}
    assert running.launcher.members == {}
    assert not running.app.status(TEST).stack_up


def test_stopping_twice_is_harmless(running):
    assert succeeded(running.app.stop_stack("op-2", TEST))
    again = running.app.stop_stack("op-3", TEST)
    assert succeeded(again)
    assert again.output_references == ()


# --- 8. the Open WebUI action -------------------------------------------------


def test_opening_open_webui_on_a_ready_stack_restarts_nothing(running):
    starts = list(running.launcher.starts)
    launch = running.app.open_webui("op-2", TEST)
    assert launch.ok
    assert launch.target.url == "http://127.0.0.1:8080"
    assert launch.target.origin == "managed"
    assert running.launcher.starts == starts
    assert running.launcher.stops == []


def test_opening_open_webui_on_a_stopped_stack_starts_only_what_it_needs(running):
    assert succeeded(running.app.stop_stack("op-2", TEST))
    before = len(running.launcher.starts)
    launch = running.app.open_webui("op-3", TEST)
    assert launch.ok, [d.message for d in launch.result.diagnostics]
    assert running.launcher.starts[before:] == ["ollama", "open-webui"]
    assert running.app.status(TEST).component("comfyui").health != "UP"


def test_opening_open_webui_after_a_hub_restart_reuses_the_running_service(running):
    again = restarted_hub(running)
    launch = again.open_webui("op-2", TEST)
    assert launch.ok
    assert again.launcher().starts == []
    assert launch.target.origin == "managed"


def test_opening_open_webui_is_refused_when_the_profile_disables_it():
    yaml_text = shipped(TEST).replace(
        "open-webui: {enabled: true, port: 8080}", "open-webui: {enabled: false, port: 8080}"
    )
    assert "enabled: false" in yaml_text
    built = controlled_stack(profile_yaml=yaml_text, profile_id=TEST, listener_depth=2)
    try:
        launch = built.app.open_webui("op-1", TEST)
        assert not launch.ok and launch.target is None
        assert any(d.code == "open-webui-disabled" for d in launch.result.diagnostics)
    finally:
        built.release()


# --- 9. observability at the boundary ----------------------------------------


def test_an_unexpected_exception_ends_as_a_sanitised_failure(running):
    def explode(*_args, **_kwargs):
        raise RuntimeError(r"boom at C:\Users\someone\secret\place.txt")

    running.app.deps.list_listeners = explode
    result = running.app.start_stack("op-2", TEST)

    assert result.state is OperationState.FAILED
    (diag,) = [d for d in result.diagnostics if d.code == "unexpected-error"]
    assert "Traceback" not in diag.message
    assert r"C:\Users" not in diag.message and "<caminho>" in diag.message
    assert running.app.current_operation().last_event.state.terminal
