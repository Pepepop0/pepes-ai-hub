from __future__ import annotations

import sys
from pathlib import Path

import pytest
from helpers import http_answering, http_failing, listening, running, tcp_forbidden

from local_ai_stack.adapters import (
    default_control_specs,
    observation_only_specs,
    ollama_control_spec,
    ollama_launch_spec,
)
from local_ai_stack.controller import (
    ServiceController,
    ServiceControlSpec,
    StartReport,
)
from local_ai_stack.inventory import (
    ListenerReport,
    LookupOutcome,
    ProcessInfo,
    ProcessLookup,
    ProcessReport,
    ScanOutcome,
)
from local_ai_stack.launcher import (
    LaunchOutcome,
    LaunchResult,
    ProcessLauncher,
    ProcessLaunchSpec,
    SpawnedProcess,
    StopOutcome,
    StopResult,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.logs import LogFiles
from local_ai_stack.probes import ProbeOutcome
from local_ai_stack.processes import OLLAMA_PROCESS, ProcessOrigin, RuntimeProcessState
from local_ai_stack.registry import ManagedProcess, ManagedProcessRegistry
from local_ai_stack.services import OLLAMA_SPEC, OPEN_WEBUI_SPEC, ServiceState

OLLAMA_EXE = sys.executable  # a real file, never actually started in these tests
OLLAMA_PROC = ProcessInfo(
    5001, "ollama.exe", r"C:\apps\ollama\ollama.exe", "ollama.exe serve", 100.0
)
HEALTHY = http_answering(payload={"version": "0.5.4"})
UNREACHABLE = http_failing(ProbeOutcome.REFUSED)


@pytest.fixture
def registry(tmp_path) -> ManagedProcessRegistry:
    return ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")


def spec_for(tmp_path, **kwargs) -> ServiceControlSpec:
    kwargs.setdefault("logs", LogFiles(tmp_path / "logs" / "ollama.log"))
    return ollama_control_spec(OLLAMA_EXE, cwd=tmp_path, **kwargs)


def make_controller(
    tmp_path,
    registry,
    *,
    http=UNREACHABLE,
    processes: ProcessReport | None = None,
    listeners=None,
    spawn_pid: int | None = None,
    lookup=None,
    specs=None,
    poll_interval: float = 0.0,
) -> ServiceController:
    launcher = ProcessLauncher(
        registry,
        spawn=lambda argv, **kwargs: SpawnedProcess(spawn_pid or 5001),
        clock=lambda: 10.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    clock = iter(range(10000))
    return ServiceController(
        specs if specs is not None else (spec_for(tmp_path),),
        registry,
        launcher,
        http=http,
        tcp=tcp_forbidden,
        list_processes=processes if callable(processes) else (lambda: processes or running()),
        list_listeners_fn=lambda: listeners if listeners is not None else listening(),
        lookup_process=lookup or (lambda pid: ProcessLookup(LookupOutcome.FOUND, OLLAMA_PROC)),
        monotonic=lambda: next(clock),
        sleep=lambda seconds: None,
        poll_interval=poll_interval,
    )


# --- specs -----------------------------------------------------------------


def test_ollama_launch_spec_is_explicit_about_host_and_env(tmp_path):
    launch = ollama_launch_spec(OLLAMA_EXE, port=11500, models_dir=tmp_path / "models")
    assert launch.argv == (OLLAMA_EXE, "serve")
    assert launch.env["OLLAMA_HOST"] == "127.0.0.1:11500"
    assert launch.env["OLLAMA_MODELS"] == str(tmp_path / "models")
    assert launch.allow_force_kill is True


def test_ollama_control_spec_keeps_health_and_process_ports_aligned(tmp_path):
    spec = spec_for(tmp_path, port=11500)
    assert spec.service.port == 11500
    assert spec.process.port == 11500
    assert spec.service.endpoint == "http://127.0.0.1:11500/api/version"
    assert spec.controllable


def test_ollama_logs_default_to_the_stack_layout(tmp_path):
    layout = StackLayout.from_root(tmp_path / "root")
    spec = ollama_control_spec(OLLAMA_EXE, layout=layout)
    assert spec.launch.logs.stdout == layout.log_file("ollama")


def test_other_services_are_observation_only():
    specs = {spec.service_id: spec for spec in observation_only_specs()}
    assert set(specs) == {"open-webui", "comfyui"}
    assert not any(spec.controllable for spec in specs.values())


def test_default_specs_include_ollama_uncontrollable_without_an_executable():
    specs = {spec.service_id: spec for spec in default_control_specs()}
    assert set(specs) == {"ollama", "open-webui", "comfyui"}
    assert not specs["ollama"].controllable


def test_control_spec_rejects_mismatched_ids():
    with pytest.raises(ValueError, match="service id mismatch"):
        ServiceControlSpec(OPEN_WEBUI_SPEC, OLLAMA_PROCESS)
    with pytest.raises(ValueError, match="launch spec belongs to"):
        ServiceControlSpec(OLLAMA_SPEC, OLLAMA_PROCESS, ProcessLaunchSpec("comfyui", (OLLAMA_EXE,)))


# --- status ----------------------------------------------------------------


def test_status_of_a_stopped_service_keeps_health_and_process_separate(tmp_path, registry):
    controller = make_controller(tmp_path, registry)
    status = controller.status("ollama")
    assert status.runtime.state is RuntimeProcessState.STOPPED
    assert status.health.state is ServiceState.DOWN
    assert status.origin is ProcessOrigin.UNKNOWN
    assert status.managed is None
    assert not status.stoppable
    assert status.state is ServiceState.DOWN


def test_status_marks_our_own_process_as_managed(tmp_path, registry):
    registry.register(
        ManagedProcess("ollama", OLLAMA_PROC.pid, OLLAMA_EXE, create_time=OLLAMA_PROC.create_time)
    )
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    status = controller.status("ollama")
    assert status.runtime.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.MANAGED
    assert status.managed.pid == OLLAMA_PROC.pid
    assert status.stoppable
    assert status.health.state is ServiceState.UP
    assert status.state is ServiceState.UP


def test_status_preserves_external_origin_when_we_did_not_start_it(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    status = controller.status("ollama")
    assert status.runtime.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.EXTERNAL
    assert status.managed is None
    assert not status.stoppable


def test_unknown_service_is_a_clear_error(tmp_path, registry):
    with pytest.raises(KeyError, match="unknown service"):
        make_controller(tmp_path, registry).status("flux")


def test_statuses_covers_every_declared_service(tmp_path, registry):
    controller = make_controller(
        tmp_path, registry, specs=(spec_for(tmp_path), *observation_only_specs())
    )
    assert set(controller.statuses()) == {"ollama", "open-webui", "comfyui"}


# --- start -----------------------------------------------------------------


def test_start_spawns_and_reports_starting_without_claiming_health(tmp_path, registry):
    controller = make_controller(tmp_path, registry)
    report = controller.start("ollama")
    assert report.outcome is LaunchOutcome.STARTED
    assert report.process_state is RuntimeProcessState.STARTING
    assert report.health is None
    assert not report.ready
    assert registry.load().get("ollama").pid == 5001


def test_start_refuses_when_an_external_ollama_is_already_running(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    report = controller.start("ollama")
    assert report.outcome is LaunchOutcome.ALREADY_RUNNING_EXTERNAL
    assert "outside this manager" in report.launch.detail
    assert registry.load().entries == {}


def test_start_is_idempotent_for_a_process_we_already_manage(tmp_path, registry):
    registry.register(
        ManagedProcess("ollama", OLLAMA_PROC.pid, OLLAMA_EXE, create_time=OLLAMA_PROC.create_time)
    )
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    report = controller.start("ollama")
    assert report.outcome is LaunchOutcome.ALREADY_MANAGED
    assert report.process_state is RuntimeProcessState.RUNNING


def test_start_waits_for_health_and_reports_running(tmp_path, registry):
    controller = make_controller(tmp_path, registry, http=HEALTHY, listeners=listening(11434))
    report = controller.start("ollama", wait=True, timeout=5)
    assert report.process_state is RuntimeProcessState.RUNNING
    assert report.health.state is ServiceState.UP
    assert report.ready


def test_start_reports_failed_when_the_process_exits_during_startup(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        lookup=lambda pid: ProcessLookup(LookupOutcome.NOT_FOUND, detail="gone"),
    )
    report = controller.start("ollama", wait=True, timeout=5)
    assert report.process_state is RuntimeProcessState.FAILED
    assert "exited during startup" in report.detail
    assert not report.ready


def test_start_reports_failed_when_health_never_comes_up(tmp_path, registry):
    controller = make_controller(tmp_path, registry, http=UNREACHABLE, listeners=listening(11434))
    report = controller.start("ollama", wait=True, timeout=2)
    assert report.process_state is RuntimeProcessState.FAILED
    assert "did not become healthy" in report.detail
    assert report.health.state is ServiceState.DOWN
    # The process is still ours and still registered, so it can be stopped.
    assert registry.load().get("ollama") is not None


def test_start_keeps_probe_timeout_as_unknown(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        http=http_failing(ProbeOutcome.TIMEOUT, "http timeout after 1.50s"),
        listeners=listening(11434),
    )
    report = controller.start("ollama", wait=True, timeout=2)
    assert report.health.state is ServiceState.UNKNOWN
    assert report.process_state is RuntimeProcessState.FAILED


def test_start_of_an_observation_only_service_is_refused(tmp_path, registry):
    controller = make_controller(tmp_path, registry, specs=observation_only_specs())
    report = controller.start("comfyui")
    assert report.outcome is LaunchOutcome.SPAWN_FAILED
    assert "no launch spec" in report.launch.detail
    assert registry.load().entries == {}


def test_start_dry_run_writes_nothing(tmp_path, registry):
    controller = make_controller(tmp_path, registry)
    report = controller.start("ollama", dry_run=True)
    assert report.outcome is LaunchOutcome.DRY_RUN
    assert not registry.path.exists()


# --- stop ------------------------------------------------------------------


def test_stop_refuses_an_externally_started_process(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    result = controller.stop("ollama")
    assert result.outcome is StopOutcome.NOT_MANAGED
    assert "started outside this manager" in result.detail


def test_stop_delegates_to_the_launcher_for_our_own_process(tmp_path, registry):
    registry.register(
        ManagedProcess("ollama", OLLAMA_PROC.pid, OLLAMA_EXE, create_time=OLLAMA_PROC.create_time)
    )
    calls: list = []
    controller = make_controller(
        tmp_path, registry, processes=running(OLLAMA_PROC), listeners=listening(11434)
    )

    def record(service_id, **kwargs):
        calls.append((service_id, kwargs))
        return StopResult(StopOutcome.STOPPED, service_id, OLLAMA_PROC.pid, "terminated")

    controller.launcher.stop = record
    assert controller.stop("ollama", allow_force=True, timeout=3).stopped
    assert calls[0][0] == "ollama"
    assert calls[0][1]["allow_force"] is True
    assert calls[0][1]["timeout"] == 3


def test_ollama_stop_uses_runner_tree_cleanup_policy_by_default(tmp_path, registry):
    registry.register(
        ManagedProcess("ollama", OLLAMA_PROC.pid, OLLAMA_EXE, create_time=OLLAMA_PROC.create_time)
    )
    calls: list = []
    controller = make_controller(
        tmp_path,
        registry,
        specs=(spec_for(tmp_path, stop_timeout=7.0),),
        processes=running(OLLAMA_PROC),
        listeners=listening(11434),
    )

    def record(service_id, **kwargs):
        calls.append(kwargs)
        return StopResult(StopOutcome.STOPPED, service_id, OLLAMA_PROC.pid, "terminated")

    controller.launcher.stop = record
    controller.stop("ollama")
    assert calls[0]["allow_force"] is True
    assert calls[0]["timeout"] == 7.0


def test_stop_of_a_service_we_never_started_is_not_managed(tmp_path, registry):
    controller = make_controller(tmp_path, registry)
    assert controller.stop("ollama").outcome is StopOutcome.NOT_MANAGED


# --- restart ---------------------------------------------------------------


def test_restart_composes_stop_and_start(tmp_path, registry):
    registry.register(
        ManagedProcess("ollama", OLLAMA_PROC.pid, OLLAMA_EXE, create_time=OLLAMA_PROC.create_time)
    )
    order: list[str] = []
    machine = {"processes": running(OLLAMA_PROC)}
    controller = make_controller(tmp_path, registry, processes=lambda: machine["processes"])

    def fake_stop(service_id, **kwargs):
        order.append("stop")
        registry.unregister(service_id)
        machine["processes"] = running()
        return StopResult(StopOutcome.STOPPED, service_id, OLLAMA_PROC.pid, "terminated")

    real_start = controller.start

    def traced_start(service_id, **kwargs):
        order.append("start")
        return real_start(service_id, **kwargs)

    controller.launcher.stop = fake_stop
    controller.start = traced_start

    report = controller.restart("ollama")
    assert order == ["stop", "start"]
    assert report.stop.outcome is StopOutcome.STOPPED
    assert report.restarted
    assert registry.load().get("ollama").pid == 5001


def test_restart_does_not_start_when_stop_was_refused(tmp_path, registry):
    controller = make_controller(
        tmp_path,
        registry,
        http=HEALTHY,
        processes=running(OLLAMA_PROC),
        listeners=listening(11434, pid=OLLAMA_PROC.pid),
    )
    report = controller.restart("ollama")
    assert report.stop.outcome is StopOutcome.NOT_MANAGED
    assert report.start is None
    assert not report.restarted
    assert "not restarting" in report.detail
    assert registry.load().entries == {}


# --- report shape ----------------------------------------------------------


def test_start_report_never_infers_health_from_a_spawn():
    report = StartReport(
        LaunchResult(LaunchOutcome.STARTED, "ollama", 1), RuntimeProcessState.STARTING
    )
    assert report.launch.started
    assert not report.ready


def test_unusable_scans_do_not_make_the_controller_claim_ownership(tmp_path, registry):
    registry.register(ManagedProcess("ollama", 5001, OLLAMA_EXE, create_time=1.0))
    controller = make_controller(
        tmp_path,
        registry,
        processes=ProcessReport(ScanOutcome.UNAVAILABLE, detail="psutil missing"),
        listeners=ListenerReport(ScanOutcome.UNAVAILABLE, detail="psutil missing"),
        lookup=lambda pid: ProcessLookup(LookupOutcome.UNAVAILABLE, detail="psutil missing"),
    )
    status = controller.status("ollama")
    assert status.managed is None
    assert not status.stoppable
    assert status.runtime.state is RuntimeProcessState.UNKNOWN


def test_registry_path_is_derived_from_the_stack_layout(tmp_path):
    from local_ai_stack.controller import registry_for

    registry = registry_for(tmp_path / "root")
    assert registry.path == StackLayout.from_root(tmp_path / "root").state_file("managed-processes")


def test_controller_never_touches_a_real_ollama(tmp_path, registry):
    """Sanity: the specs used in these tests point at a Python interpreter."""
    spec = spec_for(tmp_path)
    assert Path(spec.launch.executable) == Path(sys.executable)
    assert spec.launch.argv[1:] == ("serve",)
