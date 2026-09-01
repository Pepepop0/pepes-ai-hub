"""Controlled end-to-end smoke for M5.

Everything here is deliberately safe: the only process ever started is a Python
child this test fully controls, no real service is started or stopped, no real
port is required, and every file lands under ``tmp_path``. Real Ollama is only
ever *observed*.
"""

from __future__ import annotations

import sys
import time

import psutil
import pytest
from helpers import http_answering, http_failing, tcp_forbidden

from local_ai_stack.controller import ServiceController, ServiceControlSpec
from local_ai_stack.doctor import doctor_text
from local_ai_stack.inventory import (
    ListenerReport,
    LookupOutcome,
    ScanOutcome,
    list_processes,
    lookup_process,
)
from local_ai_stack.launcher import LaunchOutcome, ProcessLaunchSpec, StopOutcome
from local_ai_stack.layout import StackLayout
from local_ai_stack.logs import read_log_tail, service_log_files
from local_ai_stack.probes import ProbeOutcome
from local_ai_stack.processes import (
    COMFYUI_PROCESS,
    OLLAMA_PROCESS,
    OPEN_WEBUI_PROCESS,
    ProcessOrigin,
    RuntimeProcessSpec,
    RuntimeProcessState,
    discover_process,
)
from local_ai_stack.registry import IdentityVerdict, ManagedProcessRegistry
from local_ai_stack.services import ServiceSpec

MARKER = "pepito-smoke-child"
NO_LISTENER_SCAN = ListenerReport(ScanOutcome.UNAVAILABLE, detail="not used in this smoke")


def child_argv() -> tuple[str, ...]:
    code = (
        f"import sys, time; sys.stdout.write('{MARKER} up\\n'); sys.stdout.flush(); time.sleep(60)"
    )
    return (sys.executable, "-c", code)


@pytest.fixture
def reaper():
    started: list[int] = []
    yield started
    for pid in started:
        try:
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.Error):
            continue


def wait_until_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lookup_process(pid).outcome is LookupOutcome.NOT_FOUND:
            return True
        time.sleep(0.05)
    return False


def build_controller(tmp_path, *, http):
    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    registry = ManagedProcessRegistry.from_layout(layout)
    spec = ServiceControlSpec(
        # A fake service: no real port, so nothing on the machine is involved.
        service=ServiceSpec(
            id="smoke", name="Smoke", port=0, health_path="/api/version", timeout=0.5
        ),
        process=RuntimeProcessSpec("smoke", "Smoke", cmdline_markers=(MARKER,)),
        launch=ProcessLaunchSpec(
            "smoke",
            child_argv(),
            cwd=tmp_path,
            logs=service_log_files(layout, "smoke"),
            startup_timeout=15.0,
            stop_timeout=10.0,
        ),
    )
    controller = ServiceController(
        (spec,),
        registry,
        http=http,
        tcp=tcp_forbidden,
        list_listeners_fn=lambda: NO_LISTENER_SCAN,
        poll_interval=0.05,
    )
    return controller, registry, layout


def test_managed_lifecycle_start_detect_stop_cleanup(tmp_path, reaper):
    controller, registry, layout = build_controller(
        tmp_path, http=http_answering(payload={"version": "smoke"})
    )

    started = controller.start("smoke", wait=True)
    reaper.append(started.pid)
    assert started.outcome is LaunchOutcome.STARTED
    assert started.process_state is RuntimeProcessState.RUNNING
    assert started.ready

    entry = registry.load().get("smoke")
    assert entry.pid == started.pid
    assert entry.create_time is not None
    assert registry.status("smoke").verdict is IdentityVerdict.LIVE

    running = controller.status("smoke")
    assert running.runtime.state is RuntimeProcessState.RUNNING
    assert running.origin is ProcessOrigin.MANAGED
    assert running.stoppable
    assert started.pid in running.runtime.pids

    deadline = time.monotonic() + 10
    log = layout.log_file("smoke")
    while time.monotonic() < deadline and not read_log_tail(log):
        time.sleep(0.05)
    assert read_log_tail(log) == (f"{MARKER} up",)

    stopped = controller.stop("smoke")
    assert stopped.outcome is StopOutcome.STOPPED
    assert wait_until_gone(started.pid)
    assert registry.load().entries == {}

    after = controller.status("smoke")
    assert after.runtime.state is RuntimeProcessState.STOPPED
    assert after.managed is None
    assert not after.stoppable


def test_restart_of_a_managed_child_replaces_the_process(tmp_path, reaper):
    controller, registry, _ = build_controller(
        tmp_path, http=http_answering(payload={"version": "smoke"})
    )
    first = controller.start("smoke")
    reaper.append(first.pid)

    report = controller.restart("smoke", wait=True)
    if report.start is not None and report.start.pid is not None:
        reaper.append(report.start.pid)

    assert report.stop.outcome is StopOutcome.STOPPED
    assert report.restarted
    assert report.start.pid != first.pid
    assert wait_until_gone(first.pid)
    assert registry.load().get("smoke").pid == report.start.pid

    controller.stop("smoke")


def test_a_child_that_never_gets_healthy_is_reported_failed_not_killed(tmp_path, reaper):
    controller, registry, _ = build_controller(tmp_path, http=http_failing(ProbeOutcome.REFUSED))
    report = controller.start("smoke", wait=True, timeout=0.2)
    reaper.append(report.pid)

    assert report.launch.started
    assert report.process_state is RuntimeProcessState.FAILED
    assert not report.ready
    # Still running and still ours: the manager reports, it does not clean up
    # behind the user's back.
    assert lookup_process(report.pid).outcome is LookupOutcome.FOUND
    assert registry.load().get("smoke") is not None
    assert controller.stop("smoke").outcome is StopOutcome.STOPPED


# --- real machine, observation only ----------------------------------------


def test_real_ollama_is_only_observed_never_touched():
    """If Ollama happens to be running on this machine, we must not own it."""
    status = discover_process(OLLAMA_PROCESS, processes=list_processes())
    assert status.state in set(RuntimeProcessState)
    if status.state is RuntimeProcessState.RUNNING:
        assert status.origin is ProcessOrigin.EXTERNAL


def test_stopping_a_service_we_never_started_is_refused(tmp_path):
    controller, registry, _ = build_controller(tmp_path, http=http_failing(ProbeOutcome.REFUSED))
    result = controller.stop("smoke")
    assert result.outcome is StopOutcome.NOT_MANAGED
    assert registry.load().entries == {}


def test_open_webui_adapter_drives_a_real_child_through_its_lifecycle(tmp_path, reaper):
    """The shipped Open WebUI spec, pointed at a child this test fully owns.

    Only the executable and its arguments are replaced: the environment, the
    log files, the registry and the ownership rules are the real ones.
    """
    from local_ai_stack.adapters import open_webui_control_spec

    layout = StackLayout.from_root(tmp_path / "stack")
    layout.ensure()
    registry = ManagedProcessRegistry.from_layout(layout)
    code = (
        f"import sys, time; sys.stdout.write('{MARKER} open-webui\\n'); "
        "sys.stdout.flush(); time.sleep(60)"
    )
    spec = open_webui_control_spec(
        sys.executable,
        layout=layout,
        args=("-c", code),
        startup_timeout=15.0,
    )
    controller = ServiceController(
        (spec,),
        registry,
        http=http_answering(payload={"status": True}),
        tcp=tcp_forbidden,
        list_listeners_fn=lambda: NO_LISTENER_SCAN,
        poll_interval=0.05,
    )

    started = controller.start("open-webui", wait=True)
    reaper.append(started.pid)
    assert started.outcome is LaunchOutcome.STARTED
    assert started.ready

    running_status = controller.status("open-webui")
    assert running_status.origin is ProcessOrigin.MANAGED
    assert running_status.stoppable

    deadline = time.monotonic() + 10
    log = layout.log_file("open-webui")
    while time.monotonic() < deadline and not read_log_tail(log):
        time.sleep(0.05)
    assert read_log_tail(log) == (f"{MARKER} open-webui",)

    assert controller.stop("open-webui").outcome is StopOutcome.STOPPED
    assert wait_until_gone(started.pid)
    assert registry.load().entries == {}


def test_real_open_webui_and_comfyui_are_only_observed():
    """Whatever is running on this machine, we must not claim to own it."""
    processes = list_processes()
    for spec in (OPEN_WEBUI_PROCESS, COMFYUI_PROCESS):
        status = discover_process(spec, processes=processes)
        assert status.state in set(RuntimeProcessState)
        if status.state is RuntimeProcessState.RUNNING:
            assert status.origin is ProcessOrigin.EXTERNAL


def test_doctor_still_runs_against_the_real_machine():
    text = doctor_text()
    assert "Services:" in text
    assert "Stack:" in text
