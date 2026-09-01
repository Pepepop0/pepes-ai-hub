from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from local_ai_stack import launcher as launcher_module
from local_ai_stack.inventory import (
    LookupOutcome,
    ProcessInfo,
    ProcessLookup,
    ProcessReport,
    ScanOutcome,
    lookup_process,
)
from local_ai_stack.launcher import (
    LaunchOutcome,
    ProcessLauncher,
    ProcessLaunchSpec,
    SpawnedProcess,
    StopOutcome,
)
from local_ai_stack.logs import LogFiles, read_log_tail
from local_ai_stack.registry import ManagedProcess, ManagedProcessRegistry

SLEEP_SECONDS = 30


def sleeper_argv(prelude: str = "") -> tuple[str, ...]:
    """A child process fully controlled by the test: prints, then sleeps."""
    code = f"import sys, time; {prelude} time.sleep({SLEEP_SECONDS})"
    return (sys.executable, "-c", code)


@pytest.fixture
def reaper():
    """Guarantees no test leaves a child process behind."""
    started: list[int] = []
    yield started
    for pid in started:
        try:
            process = psutil.Process(pid)
            process.kill()
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.Error):
            continue


@pytest.fixture
def registry(tmp_path) -> ManagedProcessRegistry:
    return ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")


def wait_until_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lookup_process(pid).outcome is LookupOutcome.NOT_FOUND:
            return True
        time.sleep(0.05)
    return False


def fake_spawn(pid: int = 4242, calls: list | None = None):
    def spawn(argv, *, cwd, env, stdout, stderr):
        if calls is not None:
            calls.append({"argv": tuple(argv), "cwd": cwd, "env": dict(env)})
        return SpawnedProcess(pid)

    return spawn


def exploding_spawn(argv, *, cwd, env, stdout, stderr):
    raise OSError(13, "permission denied")


# --- launch spec -----------------------------------------------------------


def test_launch_spec_rejects_empty_argv():
    with pytest.raises(ValueError, match="argv"):
        ProcessLaunchSpec("x", ())


def test_launch_spec_rejects_non_positive_timeouts():
    with pytest.raises(ValueError, match="timeouts"):
        ProcessLaunchSpec("x", ("a",), stop_timeout=0)


def test_child_env_is_explicit_and_never_mutates_the_parent():
    spec = ProcessLaunchSpec("x", ("a",), env={"OLLAMA_HOST": "127.0.0.1:11434"})
    before = dict(os.environ)
    child = spec.child_env({"PATH": "/bin"})
    assert child["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert child["PATH"] == "/bin"
    assert dict(os.environ) == before
    assert "OLLAMA_HOST" not in os.environ


def test_child_env_can_start_from_nothing():
    spec = ProcessLaunchSpec("x", ("a",), env={"A": "1"}, inherit_env=False)
    assert spec.child_env({"PATH": "/bin"}) == {"A": "1"}


# --- start -----------------------------------------------------------------


def test_start_registers_only_after_a_successful_spawn(registry, tmp_path):
    calls: list = []
    launcher = ProcessLauncher(registry, spawn=fake_spawn(4242, calls), clock=lambda: 99.0)
    spec = ProcessLaunchSpec("fixture", (sys.executable, "-c", "pass"), cwd=tmp_path)
    result = launcher.start(spec)

    assert result.outcome is LaunchOutcome.STARTED
    assert result.started and result.pid == 4242
    entry = registry.load().get("fixture")
    assert entry.pid == 4242
    assert entry.argv == spec.argv
    assert calls[0]["cwd"] == str(tmp_path)


def test_failed_spawn_leaves_no_registry_entry(registry):
    launcher = ProcessLauncher(registry, spawn=exploding_spawn)
    result = launcher.start(ProcessLaunchSpec("fixture", (sys.executable, "-c", "pass")))
    assert result.outcome is LaunchOutcome.SPAWN_FAILED
    assert not result.started
    assert "permission denied" in result.detail
    assert registry.load().entries == {}


def test_missing_executable_is_reported_before_spawning(registry, tmp_path):
    spawned: list = []
    launcher = ProcessLauncher(registry, spawn=fake_spawn(calls=spawned))
    result = launcher.start(ProcessLaunchSpec("fixture", (str(tmp_path / "nope.exe"),)))
    assert result.outcome is LaunchOutcome.EXECUTABLE_NOT_FOUND
    assert spawned == []
    assert registry.load().entries == {}


def test_missing_working_directory_is_reported(registry, tmp_path):
    launcher = ProcessLauncher(registry, spawn=fake_spawn())
    spec = ProcessLaunchSpec("fixture", (sys.executable,), cwd=tmp_path / "ghost")
    result = launcher.start(spec)
    assert result.outcome is LaunchOutcome.SPAWN_FAILED
    assert "working directory" in result.detail


def test_dry_run_neither_spawns_nor_registers(registry):
    spawned: list = []
    launcher = ProcessLauncher(registry, dry_run=True, spawn=fake_spawn(calls=spawned))
    result = launcher.start(ProcessLaunchSpec("fixture", (sys.executable, "-c", "pass")))
    assert result.outcome is LaunchOutcome.DRY_RUN
    assert spawned == []
    assert not registry.path.exists()


def test_starting_an_already_managed_service_is_refused(registry, tmp_path, monkeypatch):
    registry.register(ManagedProcess("fixture", 4242, sys.executable, create_time=1.0))
    monkeypatch.setattr(
        launcher_module.ManagedProcessRegistry,
        "status",
        lambda self, service_id, processes=None: self.verify(
            self.load().get(service_id),
            processes=ProcessReport(
                ScanOutcome.OK, (ProcessInfo(4242, "python.exe", None, "", 1.0),)
            ),
        ),
    )
    launcher = ProcessLauncher(registry, spawn=fake_spawn())
    result = launcher.start(ProcessLaunchSpec("fixture", (sys.executable, "-c", "pass")))
    assert result.outcome is LaunchOutcome.ALREADY_MANAGED
    assert result.pid == 4242


def test_real_child_writes_to_its_log_file(registry, tmp_path, reaper):
    logs = LogFiles(tmp_path / "logs" / "fixture.log")
    launcher = ProcessLauncher(registry)
    spec = ProcessLaunchSpec(
        "fixture",
        sleeper_argv("sys.stdout.write('hello from child\\n'); sys.stdout.flush();"),
        cwd=tmp_path,
        logs=logs,
    )
    result = launcher.start(spec)
    reaper.append(result.pid)
    assert result.outcome is LaunchOutcome.STARTED

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not read_log_tail(logs.stdout):
        time.sleep(0.05)
    assert read_log_tail(logs.stdout) == ("hello from child",)
    assert registry.load().get("fixture").create_time is not None


def test_real_child_gets_the_declared_cwd_and_env(registry, tmp_path, reaper):
    logs = LogFiles(tmp_path / "logs" / "fixture.log")
    workdir = tmp_path / "work"
    workdir.mkdir()
    prelude = (
        "import os;"
        "sys.stdout.write(os.getcwd() + '|' + os.environ.get('PEPITO_TEST', 'unset') + '\\n');"
        "sys.stdout.flush();"
    )
    launcher = ProcessLauncher(registry)
    result = launcher.start(
        ProcessLaunchSpec(
            "fixture", sleeper_argv(prelude), cwd=workdir, env={"PEPITO_TEST": "yes"}, logs=logs
        )
    )
    reaper.append(result.pid)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not read_log_tail(logs.stdout):
        time.sleep(0.05)
    written = read_log_tail(logs.stdout)[0]
    assert written.endswith("|yes")
    assert Path(written.split("|")[0]).resolve() == workdir.resolve()
    assert "PEPITO_TEST" not in os.environ


def test_stderr_can_be_a_separate_file(registry, tmp_path, reaper):
    logs = LogFiles(tmp_path / "logs" / "fixture.log", tmp_path / "logs" / "fixture-stderr.log")
    prelude = "sys.stderr.write('boom\\n'); sys.stderr.flush();"
    launcher = ProcessLauncher(registry)
    result = launcher.start(
        ProcessLaunchSpec("fixture", sleeper_argv(prelude), cwd=tmp_path, logs=logs)
    )
    reaper.append(result.pid)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not read_log_tail(logs.stderr):
        time.sleep(0.05)
    assert read_log_tail(logs.stderr) == ("boom",)
    assert read_log_tail(logs.stdout) == ()


# --- stop ------------------------------------------------------------------


def test_stop_terminates_a_managed_child_and_cleans_the_registry(registry, tmp_path, reaper):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)

    result = launcher.stop("fixture", timeout=10)
    assert result.outcome is StopOutcome.STOPPED
    assert result.stopped and not result.forced
    assert wait_until_gone(started.pid)
    assert registry.load().entries == {}


def test_stop_is_idempotent_and_cleans_a_stale_entry(registry, tmp_path, reaper):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)
    assert launcher.stop("fixture", timeout=10).outcome is StopOutcome.STOPPED

    registry.register(ManagedProcess("fixture", started.pid, sys.executable, create_time=1.0))
    again = launcher.stop("fixture")
    assert again.outcome is StopOutcome.NOT_RUNNING
    assert again.stopped
    assert registry.load().entries == {}


def test_stop_of_an_unregistered_service_is_refused(registry):
    result = ProcessLauncher(registry).stop("ollama")
    assert result.outcome is StopOutcome.NOT_MANAGED
    assert "no managed process" in result.detail


def test_stop_refuses_a_recycled_pid_and_never_touches_it(registry, tmp_path, reaper):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)
    # Same pid, creation time from another era: this is not our process.
    registry.register(ManagedProcess("fixture", started.pid, sys.executable, create_time=1.0))

    result = launcher.stop("fixture")
    assert result.outcome is StopOutcome.IDENTITY_MISMATCH
    assert "refusing to stop" in result.detail
    assert lookup_process(started.pid).outcome is LookupOutcome.FOUND
    assert registry.load().entries == {}


def test_stop_refuses_when_identity_cannot_be_verified(registry, monkeypatch):
    registry.register(ManagedProcess("fixture", 4242, sys.executable, create_time=1.0))
    monkeypatch.setattr(
        "local_ai_stack.registry.lookup_process",
        lambda pid: ProcessLookup(LookupOutcome.UNAVAILABLE, detail="psutil is not installed"),
    )
    killed: list[int] = []
    monkeypatch.setattr(launcher_module, "_terminate", lambda pid: killed.append(pid))

    result = ProcessLauncher(registry).stop("fixture")
    assert result.outcome is StopOutcome.IDENTITY_UNKNOWN
    assert killed == []
    assert registry.load().get("fixture") is not None


def test_stop_times_out_when_the_process_ignores_terminate(registry, tmp_path, reaper, monkeypatch):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)
    # This is the single-process path: give up the job so the tree is not
    # provable, which is what a restarted manager would face.
    launcher.forget_job("fixture")
    monkeypatch.setattr(launcher_module, "_terminate", lambda pid: None)

    result = launcher.stop("fixture", timeout=0.2)
    assert result.outcome is StopOutcome.TIMEOUT
    assert "force kill not allowed" in result.detail
    assert lookup_process(started.pid).outcome is LookupOutcome.FOUND
    assert registry.load().get("fixture") is not None


def test_force_kill_is_used_only_when_the_policy_allows_it(registry, tmp_path, reaper, monkeypatch):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)
    launcher.forget_job("fixture")
    monkeypatch.setattr(launcher_module, "_terminate", lambda pid: None)

    result = launcher.stop("fixture", timeout=0.2, allow_force=True)
    assert result.outcome is StopOutcome.STOPPED
    assert result.forced
    assert wait_until_gone(started.pid)
    assert registry.load().entries == {}


def test_stop_reports_a_terminate_failure_without_dropping_the_entry(registry, monkeypatch):
    registry.register(ManagedProcess("fixture", 4242, sys.executable, create_time=1.0))
    monkeypatch.setattr(
        "local_ai_stack.registry.lookup_process",
        lambda pid: ProcessLookup(
            LookupOutcome.FOUND, ProcessInfo(4242, "python.exe", sys.executable, "", 1.0)
        ),
    )

    def refuse(pid):
        raise RuntimeError("access denied")

    monkeypatch.setattr(launcher_module, "_terminate", refuse)
    result = ProcessLauncher(registry).stop("fixture")
    assert result.outcome is StopOutcome.FAILED
    assert registry.load().get("fixture") is not None


def test_stop_dry_run_touches_nothing(registry, tmp_path, reaper):
    launcher = ProcessLauncher(registry)
    started = launcher.start(ProcessLaunchSpec("fixture", sleeper_argv(), cwd=tmp_path))
    reaper.append(started.pid)

    result = launcher.stop("fixture", dry_run=True)
    assert result.outcome is StopOutcome.DRY_RUN
    assert lookup_process(started.pid).outcome is LookupOutcome.FOUND
    assert registry.load().get("fixture") is not None


# --- ownership boundary ----------------------------------------------------


def test_module_exposes_no_public_way_to_kill_an_arbitrary_pid():
    public = [name for name in dir(launcher_module) if not name.startswith("_")]
    assert not [name for name in public if "kill" in name.lower() or "terminate" in name.lower()]
