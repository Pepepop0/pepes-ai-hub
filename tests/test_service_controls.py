"""Ownership rules, proved for every service the runtime can control.

The point of these tests is that Open WebUI and ComfyUI obey exactly the same
rule Ollama already did: only a process this manager started and still owns can
be stopped, and only a MANAGED install can be started.
"""

from __future__ import annotations

import sys

import pytest
from helpers import http_answering, http_failing, listening, running, tcp_forbidden

from local_ai_stack.adapters import control_spec_for
from local_ai_stack.controller import ServiceController
from local_ai_stack.inventory import (
    ListenerReport,
    LookupOutcome,
    ProcessInfo,
    ProcessLookup,
    ProcessReport,
    ScanOutcome,
)
from local_ai_stack.launcher import LaunchOutcome, ProcessLauncher, SpawnedProcess, StopOutcome
from local_ai_stack.layout import StackLayout
from local_ai_stack.probes import ProbeOutcome
from local_ai_stack.processes import ProcessOrigin, RuntimeProcessState
from local_ai_stack.registry import ManagedProcess, ManagedProcessRegistry
from local_ai_stack.services import COMFYUI_SPEC, OPEN_WEBUI_SPEC, ServiceState

EXECUTABLE = sys.executable

SERVICES = [
    pytest.param(OPEN_WEBUI_SPEC, {"version": "x"}, id="open-webui"),
    pytest.param(COMFYUI_SPEC, {"system": {}}, id="comfyui"),
]

HEALTHY_PAYLOADS = {
    "open-webui": {"status": True},
    "comfyui": {"system": {"os": "nt"}},
}


def process_for(spec, pid: int = 6001) -> ProcessInfo:
    return ProcessInfo(pid, "python.exe", EXECUTABLE, f"python.exe {spec.id}", 100.0)


@pytest.fixture
def registry(tmp_path) -> ManagedProcessRegistry:
    return ManagedProcessRegistry(tmp_path / "state" / "managed.json")


def make_controller(
    spec,
    registry,
    *,
    executable=EXECUTABLE,
    layout=None,
    http=None,
    processes=None,
    listeners=None,
    lookup=None,
    spawn_pid: int = 6001,
) -> ServiceController:
    control = control_spec_for(spec.id, executable, layout=layout)
    launcher = ProcessLauncher(
        registry,
        spawn=lambda argv, **kwargs: SpawnedProcess(spawn_pid),
        clock=lambda: 10.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    clock = iter(range(10000))
    return ServiceController(
        (control,),
        registry,
        launcher,
        http=http if http is not None else http_failing(ProbeOutcome.REFUSED),
        tcp=tcp_forbidden,
        list_processes=lambda: processes if processes is not None else running(),
        list_listeners_fn=lambda: listeners if listeners is not None else listening(),
        lookup_process=lookup
        or (lambda pid: ProcessLookup(LookupOutcome.FOUND, process_for(OPEN_WEBUI_SPEC, pid))),
        monotonic=lambda: next(clock),
        sleep=lambda seconds: None,
        poll_interval=0.0,
    )


# --- MISSING ---------------------------------------------------------------


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_a_service_without_an_install_is_watched_not_controlled(spec, _payload, registry):
    controller = make_controller(spec, registry, executable=None)
    report = controller.start(spec.id)
    assert report.outcome is LaunchOutcome.SPAWN_FAILED
    assert "no launch spec" in report.launch.detail
    assert registry.load().entries == {}
    status = controller.status(spec.id)
    assert status.controllable is False
    assert status.runtime.state is RuntimeProcessState.STOPPED
    assert status.health.state is ServiceState.DOWN


# --- EXTERNAL --------------------------------------------------------------


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_an_externally_started_service_is_never_adopted(spec, _payload, registry):
    external = process_for(spec)
    controller = make_controller(
        spec,
        registry,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        processes=running(external),
        listeners=listening(spec.port, pid=external.pid),
    )
    status = controller.status(spec.id)
    assert status.runtime.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.EXTERNAL
    assert status.managed is None
    assert status.stoppable is False
    assert status.health.state is ServiceState.UP


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_starting_over_an_external_process_is_refused(spec, _payload, registry):
    external = process_for(spec)
    controller = make_controller(
        spec,
        registry,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        processes=running(external),
        listeners=listening(spec.port, pid=external.pid),
    )
    report = controller.start(spec.id)
    assert report.outcome is LaunchOutcome.ALREADY_RUNNING_EXTERNAL
    assert "outside this manager" in report.launch.detail
    assert registry.load().entries == {}


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_stopping_an_external_process_is_refused(spec, _payload, registry):
    external = process_for(spec)
    controller = make_controller(
        spec,
        registry,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        processes=running(external),
        listeners=listening(spec.port, pid=external.pid),
    )
    result = controller.stop(spec.id)
    assert result.outcome is StopOutcome.NOT_MANAGED
    assert "started outside this manager" in result.detail


# --- MANAGED ---------------------------------------------------------------


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_a_managed_service_starts_and_is_registered(spec, _payload, registry, tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    controller = make_controller(spec, registry, layout=layout)
    report = controller.start(spec.id)
    assert report.outcome is LaunchOutcome.STARTED
    assert report.process_state is RuntimeProcessState.STARTING
    assert report.health is None, "a spawn never asserts health"
    entry = registry.load().get(spec.id)
    assert entry.pid == 6001
    assert entry.argv[0] == EXECUTABLE


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_a_managed_service_reaches_running_once_it_answers(spec, _payload, registry, tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    controller = make_controller(
        spec,
        registry,
        layout=layout,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        listeners=listening(spec.port),
    )
    report = controller.start(spec.id, wait=True, timeout=5)
    assert report.process_state is RuntimeProcessState.RUNNING
    assert report.health.state is ServiceState.UP
    assert report.ready


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_a_managed_service_that_never_answers_is_failed_not_killed(
    spec, _payload, registry, tmp_path
):
    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    controller = make_controller(spec, registry, layout=layout, listeners=listening(spec.port))
    report = controller.start(spec.id, wait=True, timeout=2)
    assert report.process_state is RuntimeProcessState.FAILED
    assert "did not become healthy" in report.detail
    assert registry.load().get(spec.id) is not None, "still ours, so still stoppable"


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_our_own_process_is_managed_and_stoppable(spec, _payload, registry, tmp_path):
    mine = process_for(spec)
    registry.register(ManagedProcess(spec.id, mine.pid, EXECUTABLE, create_time=mine.create_time))
    controller = make_controller(
        spec,
        registry,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        processes=running(mine),
        listeners=listening(spec.port, pid=mine.pid),
    )
    status = controller.status(spec.id)
    assert status.origin is ProcessOrigin.MANAGED
    assert status.stoppable is True
    assert status.managed.pid == mine.pid
    assert controller.start(spec.id).outcome is LaunchOutcome.ALREADY_MANAGED


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_a_recycled_pid_is_never_stopped(spec, _payload, registry, tmp_path):
    """The registry, not the port or the name, decides what may be stopped."""
    impostor = ProcessInfo(6001, "notepad.exe", "notepad.exe", "notepad", 9999.0)
    registry.register(ManagedProcess(spec.id, 6001, EXECUTABLE, create_time=100.0))
    controller = make_controller(
        spec,
        registry,
        processes=running(impostor),
        listeners=listening(spec.port, pid=impostor.pid),
    )
    # The controller refuses first, because the live process is not ours.
    result = controller.stop(spec.id)
    assert result.outcome is StopOutcome.NOT_MANAGED
    # And the launcher, asked directly with the same view of the machine,
    # still refuses on identity alone.
    direct = controller.launcher.stop(spec.id, processes=running(impostor))
    assert direct.outcome is StopOutcome.IDENTITY_MISMATCH
    assert "refusing to stop" in direct.detail


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_restart_of_an_external_service_never_starts_a_second_one(spec, _payload, registry):
    external = process_for(spec)
    controller = make_controller(
        spec,
        registry,
        http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]),
        processes=running(external),
        listeners=listening(spec.port, pid=external.pid),
    )
    report = controller.restart(spec.id)
    assert report.stop.outcome is StopOutcome.NOT_MANAGED
    assert report.start is None
    assert not report.restarted
    assert registry.load().entries == {}


@pytest.mark.parametrize(("spec", "_payload"), SERVICES)
def test_nothing_is_assumed_when_the_machine_cannot_be_inspected(spec, _payload, registry):
    registry.register(ManagedProcess(spec.id, 6001, EXECUTABLE, create_time=100.0))
    controller = make_controller(
        spec,
        registry,
        processes=ProcessReport(ScanOutcome.UNAVAILABLE, detail="psutil missing"),
        listeners=ListenerReport(ScanOutcome.UNAVAILABLE, detail="psutil missing"),
        lookup=lambda pid: ProcessLookup(LookupOutcome.UNAVAILABLE, detail="psutil missing"),
    )
    status = controller.status(spec.id)
    assert status.managed is None
    assert status.stoppable is False
    assert status.runtime.state is RuntimeProcessState.UNKNOWN
