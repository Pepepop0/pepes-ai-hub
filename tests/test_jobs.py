"""Ownership of a process tree, proved rather than inferred.

Every process these tests start is a Python child the test fully controls, and
each one is reaped. Nothing real is ever started or stopped.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from local_ai_stack import jobs
from local_ai_stack.jobs import Membership

WINDOWS_ONLY = pytest.mark.skipif(
    not jobs.jobs_supported(), reason="job objects are a Windows primitive"
)

SLEEPER = "import time; time.sleep(60)"

# The base interpreter, never the venv shim. A venv python.exe on Windows is a
# launcher that starts the real interpreter as a separate process and exits, so
# adopting the shim adopts something that is already on its way out and misses
# the process that does the work. Production hits the same hazard whenever a
# launch spec points at a shim; see docs/INTEGRATION.md.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

# A root that talks to the test through files, so every step is ordered rather
# than timed. It signals that it is up, waits for permission, spawns one child,
# publishes that child's pid, and then idles.
# A root that talks to the test through files, so every step is ordered rather
# than timed. It signals that it is up, waits for permission, spawns one child,
# and idles. The child publishes its own pid: on Windows a venv python.exe is a
# shim that exits once the real interpreter starts, so the pid the parent sees
# can already be dead and recycled by the time anyone reads it.
CHILD_SCRIPT = (
    "import os, sys, time;"
    "path = sys.argv[1];"
    "open(path + '.tmp', 'w').write(str(os.getpid()));"
    "os.replace(path + '.tmp', path);"
    "time.sleep(60)"
)
LISTENER_CHILD_SCRIPT = """
import os, socket, sys, time
path = sys.argv[1]
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
open(path + '.tmp', 'w').write(f'{os.getpid()}:{listener.getsockname()[1]}')
os.replace(path + '.tmp', path)
time.sleep(60)
"""

ROOT_SCRIPT = """
import os, subprocess, sys, time
ready, go, child_file, mode, child_script = sys.argv[1:6]
def spawn():
    subprocess.Popen([sys.executable, '-c', child_script, child_file])
if mode == 'eager':
    spawn()
    open(ready, 'w').write('up')
else:
    open(ready, 'w').write('up')
    while not os.path.exists(go):
        time.sleep(0.01)
    spawn()
time.sleep(60)
"""


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


class Handshake:
    """File-based rendezvous with a child process. No sleeps, no guessing."""

    def __init__(self, directory: Path, mode: str = "wait") -> None:
        self.ready = directory / "ready"
        self.go = directory / "go"
        self.child_file = directory / "child.pid"
        self.mode = mode

    @property
    def argv(self) -> tuple[str, ...]:
        return (
            PYTHON,
            "-c",
            ROOT_SCRIPT,
            str(self.ready),
            str(self.go),
            str(self.child_file),
            self.mode,
            CHILD_SCRIPT,
        )

    def _await(self, path: Path, what: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    def wait_until_up(self) -> None:
        self._await(self.ready, "the root to report it is running")

    def release(self) -> None:
        self.go.write_text("go", encoding="utf-8")

    def child_pid(self) -> int:
        return int(self._await(self.child_file, "the root to publish its child"))


class ListenerHandshake(Handshake):
    """A managed root whose child is the runtime that owns a TCP listener."""

    @property
    def argv(self) -> tuple[str, ...]:
        return (*super().argv[:-1], LISTENER_CHILD_SCRIPT)

    def runtime(self) -> tuple[int, int]:
        pid, port = self._await(self.child_file, "the runtime to publish its listener").split(":")
        return int(pid), int(port)


def spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != "zombie"
    except psutil.NoSuchProcess:
        return False


def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


# --- the contract, on any host ---------------------------------------------


def test_nothing_is_provable_without_a_job():
    assert jobs.membership(None, 1234) is Membership.UNPROVABLE
    assert jobs.member_pids(None) is None
    assert jobs.terminate(None) is False
    assert jobs.assign(None, 1234) is False
    assert jobs.retain_by_process(None, 1234) is False
    assert jobs.duplicate_process_handle(0) is None
    jobs.close(None)  # must not raise
    jobs.close_handle(None)


def test_a_job_name_is_unique_per_start():
    first, second = jobs.new_job_name("ollama"), jobs.new_job_name("ollama")
    assert first != second
    assert first.startswith("PepitosAIHub-ollama-")


def test_terminate_takes_a_job_not_a_pid():
    """There is no call here that could ever be handed a stranger's pid."""
    with pytest.raises(AttributeError):
        jobs.terminate(4242)  # type: ignore[arg-type]


@WINDOWS_ONLY
def test_the_win32_signatures_survive_a_64_bit_handle():
    """A truncated HANDLE would look like a permission error, not a bug."""
    job = jobs.create_job(jobs.new_job_name("abi"))
    assert job is not None and job.handle > 0
    assert jobs.member_pids(job) == ()
    assert jobs.membership_is_partial(job) is False
    reopened = jobs.open_job(job.name)
    assert reopened is not None and reopened.handle != job.handle
    jobs.close(reopened)
    jobs.close(job)


# --- the proof itself ------------------------------------------------------


@WINDOWS_ONLY
def test_a_job_enumerates_a_child_created_after_adoption(tmp_path, reaper):
    """Ordered by handshake: adopt first, only then let the child exist."""
    hand = Handshake(tmp_path)
    root = subprocess.Popen(hand.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reaper.append(root.pid)
    hand.wait_until_up()

    job = jobs.create_job(jobs.new_job_name("fixture"))
    assert jobs.assign(job, int(root._handle))
    hand.release()  # the child is created strictly after adoption
    child = hand.child_pid()
    reaper.append(child)

    members = jobs.member_pids(job)
    assert members is not None
    assert root.pid in members
    assert child in members, "a process we never launched, proved ours"
    jobs.close(job)


@WINDOWS_ONLY
def test_a_process_we_did_not_start_is_never_a_member(reaper):
    job = jobs.create_job(jobs.new_job_name("fixture"))
    ours = spawn(SLEEPER)
    stranger = spawn(SLEEPER)
    reaper.extend([ours.pid, stranger.pid])
    assert jobs.assign(job, int(ours._handle))

    assert jobs.membership(job, ours.pid) is Membership.MEMBER
    assert jobs.membership(job, stranger.pid) is Membership.NOT_MEMBER
    jobs.close(job)


@WINDOWS_ONLY
def test_terminating_a_job_stops_the_tree_and_leaves_strangers_alone(tmp_path, reaper):
    hand = Handshake(tmp_path)
    root = subprocess.Popen(hand.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stranger = spawn(SLEEPER)
    reaper.extend([root.pid, stranger.pid])
    hand.wait_until_up()

    job = jobs.create_job(jobs.new_job_name("fixture"))
    assert jobs.assign(job, int(root._handle))
    hand.release()
    child = hand.child_pid()
    reaper.append(child)
    assert child in (jobs.member_pids(job) or ())

    assert jobs.terminate(job)
    assert wait_gone(root.pid)
    assert wait_gone(child), "the child went with the tree"
    assert alive(stranger.pid), "a process outside the job is untouched"
    jobs.close(job)


@WINDOWS_ONLY
def test_a_descendant_born_before_adoption_is_not_provable_and_is_left_alone(tmp_path, reaper):
    """The known window, ordered by handshake rather than by luck.

    The root creates its child *before* anyone adopts it, so that child is
    outside the job. It is therefore not provably ours — and the safe failure
    is to leave it running, never to guess.
    """
    hand = Handshake(tmp_path, mode="eager")
    root = subprocess.Popen(hand.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reaper.append(root.pid)
    hand.wait_until_up()
    child = hand.child_pid()  # already exists before adoption
    reaper.append(child)

    job = jobs.create_job(jobs.new_job_name("fixture"))
    assert jobs.assign(job, int(root._handle))
    assert jobs.membership(job, root.pid) is Membership.MEMBER
    assert jobs.membership(job, child) is Membership.NOT_MEMBER

    assert jobs.terminate(job)
    assert wait_gone(root.pid)
    assert alive(child), "left running, because it could not be proved ours"
    jobs.close(job)


@WINDOWS_ONLY
def test_proof_does_not_survive_losing_the_handle(reaper):
    """The boundary this design is honest about, pinned as behaviour."""
    name = jobs.new_job_name("fixture")
    job = jobs.create_job(name)
    child = spawn(SLEEPER)
    reaper.append(child.pid)
    assert jobs.assign(job, int(child._handle))

    jobs.close(job)
    assert jobs.open_job(name) is None, "the name no longer resolves"
    assert alive(child.pid), "but the service itself keeps running"
    assert jobs.membership(None, child.pid) is Membership.UNPROVABLE


@WINDOWS_ONLY
def test_opted_in_tree_proof_survives_manager_restart_and_stops_the_runner(tmp_path, reaper):
    """A detached Ollama root must keep its Job alive for the next Hub session."""
    from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec, StopOutcome
    from local_ai_stack.registry import ManagedProcessRegistry

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    first = ProcessLauncher(registry)
    hand = Handshake(tmp_path)
    started = first.start(ProcessLaunchSpec("ollama", hand.argv, allow_force_kill=True))
    reaper.append(started.pid)
    hand.wait_until_up()
    hand.release()
    child = hand.child_pid()
    reaper.append(child)
    assert child in (first.tree_pids("ollama", started.pid) or ())

    first.close_all()  # the command/TUI process exits while Ollama stays up
    assert alive(started.pid)
    assert alive(child)

    restarted = ProcessLauncher(registry)
    assert child in (restarted.tree_pids("ollama", started.pid) or ())
    result = restarted.stop("ollama", timeout=10, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert child in result.tree_pids
    assert wait_gone(started.pid)
    assert wait_gone(child)
    assert registry.load().entries == {}
    restarted.close_all()


@WINDOWS_ONLY
def test_comfyui_tree_proof_survives_manager_restart_and_stops_the_listener(tmp_path, reaper):
    """ComfyUI's Python runtime must remain provable after the first Hub exits."""
    from local_ai_stack.adapters import comfyui_control_spec
    from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec, StopOutcome
    from local_ai_stack.registry import ManagedProcessRegistry

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    policy = comfyui_control_spec(PYTHON).launch.allow_force_kill
    first = ProcessLauncher(registry)
    hand = ListenerHandshake(tmp_path)
    started = first.start(ProcessLaunchSpec("comfyui", hand.argv, allow_force_kill=policy))
    reaper.append(started.pid)
    hand.wait_until_up()
    hand.release()
    runtime_pid, port = hand.runtime()
    reaper.append(runtime_pid)
    assert any(
        item.pid == runtime_pid and item.laddr.port == port for item in psutil.net_connections()
    )
    assert runtime_pid in (first.tree_pids("comfyui", started.pid) or ())

    first.close_all()  # normal Hub exit; the managed service must remain up
    assert alive(started.pid)
    assert alive(runtime_pid)

    later = ProcessLauncher(registry)
    assert runtime_pid in (later.tree_pids("comfyui", started.pid) or ())
    result = later.stop("comfyui", timeout=10, allow_force=policy)

    assert result.outcome is StopOutcome.STOPPED
    assert runtime_pid in result.tree_pids
    assert wait_gone(started.pid)
    assert wait_gone(runtime_pid)
    assert not any(item.laddr.port == port for item in psutil.net_connections())
    assert registry.load().entries == {}
    later.close_all()


# --- force policy ----------------------------------------------------------


def launcher_for(tmp_path):
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    return ProcessLauncher(registry), registry


def start_root(launcher, tmp_path, mode: str = "wait"):
    from local_ai_stack.launcher import ProcessLaunchSpec

    hand = Handshake(tmp_path, mode=mode)
    started = launcher.start(ProcessLaunchSpec("fixture", hand.argv))
    hand.wait_until_up()
    return hand, started


@WINDOWS_ONLY
def test_a_normal_stop_never_terminates_the_tree(tmp_path, reaper):
    """allow_force=False keeps its meaning: the root only, nothing wider."""
    from local_ai_stack.launcher import StopOutcome

    launcher, registry = launcher_for(tmp_path)
    hand, started = start_root(launcher, tmp_path)
    reaper.append(started.pid)
    hand.release()
    child = hand.child_pid()
    reaper.append(child)
    assert child in (launcher.tree_pids("fixture", started.pid) or ())

    result = launcher.stop("fixture", timeout=10)

    assert result.outcome is StopOutcome.STOPPED
    assert result.forced is False
    assert result.tree_pids == (), "no tree was terminated"
    assert child in result.left_running
    assert "force kill" in result.detail
    assert wait_gone(started.pid), "the registered process was stopped"
    assert alive(child), "its child was proved ours and deliberately left alone"
    assert registry.load().entries == {}
    launcher.close_all()


@WINDOWS_ONLY
def test_force_is_what_authorises_terminating_the_tree(tmp_path, reaper):
    from local_ai_stack.launcher import StopOutcome

    launcher, registry = launcher_for(tmp_path)
    hand, started = start_root(launcher, tmp_path)
    reaper.append(started.pid)
    hand.release()
    child = hand.child_pid()
    reaper.append(child)

    result = launcher.stop("fixture", timeout=10, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert result.forced is True
    assert started.pid in result.tree_pids
    assert child in result.tree_pids
    assert wait_gone(child), "the proved child went with the tree"
    assert registry.load().entries == {}
    launcher.close_all()


# --- end to end ------------------------------------------------------------


@WINDOWS_ONLY
def test_the_launcher_stops_exactly_the_tree_it_can_prove(tmp_path, reaper):
    from local_ai_stack.launcher import StopOutcome

    launcher, registry = launcher_for(tmp_path)
    stranger = spawn(SLEEPER)
    reaper.append(stranger.pid)

    hand, started = start_root(launcher, tmp_path)
    reaper.append(started.pid)
    hand.release()
    child = hand.child_pid()
    reaper.append(child)

    entry = registry.load().get("fixture")
    assert entry.job_name is not None
    assert launcher.owns("fixture", started.pid) is Membership.MEMBER
    assert launcher.owns("fixture", child) is Membership.MEMBER
    assert launcher.owns("fixture", stranger.pid) is Membership.NOT_MEMBER

    result = launcher.stop("fixture", timeout=10, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert wait_gone(started.pid), "the root died"
    assert wait_gone(child), "the proved descendant died"
    assert alive(stranger.pid), "the stranger survived"
    assert registry.load().entries == {}, "the registry is clean"
    launcher.close_all()


@WINDOWS_ONLY
def test_losing_the_job_never_widens_authority(tmp_path, reaper):
    """A restarted manager stops the root and leaves what it cannot prove."""
    from local_ai_stack.launcher import StopOutcome

    launcher, registry = launcher_for(tmp_path)
    hand, started = start_root(launcher, tmp_path)
    reaper.append(started.pid)
    hand.release()
    child = hand.child_pid()
    reaper.append(child)

    launcher.forget_job("fixture")  # exactly what a manager restart leaves behind
    assert launcher.owns("fixture", started.pid) is Membership.UNPROVABLE
    assert launcher.tree_pids("fixture", started.pid) is None

    result = launcher.stop("fixture", timeout=10, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert result.tree_pids == (), "no tree was claimed without proof"
    assert result.left_running == (), "and none was reported, because none was known"
    assert wait_gone(started.pid)
    assert alive(child), "an unproved descendant is never touched"
    assert registry.load().entries == {}
    launcher.close_all()


# --- handle ownership ------------------------------------------------------


class HandleLedger:
    """Counts every handle jobs.py hands out, and every one it takes back."""

    def __init__(self, monkeypatch, *, create: bool = True, assign: bool = True) -> None:
        from local_ai_stack import jobs as module

        self.open_jobs: list[str] = []
        self.closed_jobs: list[str] = []
        self.duplicated: list[int] = []
        self.closed_handles: list[int] = []
        self._next = 1000

        def fake_create(name):
            if not create:
                return None
            self._next += 1
            self.open_jobs.append(name)
            return module.ProcessJob(name, self._next)

        def fake_close(job):
            if job is not None:
                self.closed_jobs.append(job.name)

        def fake_duplicate(handle):
            self._next += 1
            self.duplicated.append(self._next)
            return self._next

        monkeypatch.setattr(module, "create_job", fake_create)
        monkeypatch.setattr(module, "close", fake_close)
        monkeypatch.setattr(module, "duplicate_process_handle", fake_duplicate)
        monkeypatch.setattr(module, "close_handle", lambda h: self.closed_handles.append(h))
        monkeypatch.setattr(module, "assign", lambda job, handle: assign)

    @property
    def balanced(self) -> bool:
        jobs_balanced = sorted(self.open_jobs) == sorted(self.closed_jobs)
        handles_balanced = sorted(self.duplicated) == sorted(self.closed_handles)
        return jobs_balanced and handles_balanced


def fake_spawn_returning(pid: int):
    from local_ai_stack.launcher import SpawnedProcess, _process_handle

    class FakePopen:
        _handle = 4242

    def spawn(argv, **kwargs):
        return SpawnedProcess(pid, _process_handle(FakePopen()))

    return spawn


def launch_spec(service_id: str = "fixture"):
    from local_ai_stack.launcher import ProcessLaunchSpec

    return ProcessLaunchSpec(service_id, (PYTHON, "-c", "pass"))


def test_a_failed_assignment_leaks_neither_job_nor_handle(tmp_path, monkeypatch):
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch, assign=False)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(4321))
    result = launcher.start(launch_spec())

    assert result.started, "a job we could not use must not fail the start"
    assert registry.load().get("fixture").job_name is None
    assert ledger.balanced, "the job and the duplicated handle were both released"


def test_a_host_without_jobs_still_closes_the_duplicated_handle(tmp_path, monkeypatch):
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch, create=False)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(4321))
    launcher.start(launch_spec())

    assert ledger.open_jobs == []
    assert ledger.closed_handles == ledger.duplicated
    assert ledger.balanced


def test_a_stale_entry_releases_the_job_with_the_record(tmp_path, monkeypatch):
    from local_ai_stack.launcher import ProcessLauncher, StopOutcome
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(999999))
    launcher.start(launch_spec())
    assert ledger.open_jobs and not ledger.closed_jobs

    result = launcher.stop("fixture")  # pid 999999 does not exist: STALE

    assert result.outcome is StopOutcome.NOT_RUNNING
    assert registry.load().entries == {}
    assert ledger.balanced, "a dropped record drops its proof too"


def test_a_mismatched_identity_releases_the_job_too(tmp_path, monkeypatch):
    from local_ai_stack.inventory import ProcessInfo, ProcessReport, ScanOutcome
    from local_ai_stack.launcher import ProcessLauncher, StopOutcome
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(5555))
    launcher.start(launch_spec())

    impostor = ProcessReport(
        ScanOutcome.OK, (ProcessInfo(5555, "notepad.exe", "notepad.exe", "notepad", 1.0),)
    )
    result = launcher.stop("fixture", processes=impostor)

    assert result.outcome is StopOutcome.IDENTITY_MISMATCH
    assert ledger.balanced


def test_close_all_releases_every_held_job(tmp_path, monkeypatch):
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(4321))
    for service in ("one", "two"):
        launcher.start(launch_spec(service))
    assert len(ledger.open_jobs) == 2

    launcher.close_all()

    assert ledger.balanced
    assert launcher._jobs == {}


def test_restarting_a_service_replaces_its_job_instead_of_stacking_them(tmp_path, monkeypatch):
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry, spawn=fake_spawn_returning(4321))
    launcher.start(launch_spec())
    monkeypatch.setattr(
        "local_ai_stack.registry.ManagedProcessRegistry.status",
        lambda self, service_id, processes=None: None,
    )
    launcher.start(launch_spec())

    assert len(ledger.open_jobs) == 2
    assert len(ledger.closed_jobs) == 1, "the previous job was released on replacement"
    launcher.close_all()
    assert ledger.balanced


# --- a failed tree termination must still let go ---------------------------


def stop_scenario(
    monkeypatch,
    tmp_path,
    *,
    root: int,
    child: int,
    terminate_ok: bool,
    child_exit_after: int = 0,
):
    """A root that exits, a proved descendant that does not, and a chosen
    outcome for TerminateJobObject. No real process is involved: every kernel
    answer is scripted, so the path under test is exact.
    """
    from local_ai_stack import jobs as job_module
    from local_ai_stack import launcher as launcher_module
    from local_ai_stack import registry as registry_module
    from local_ai_stack.inventory import LookupOutcome, ProcessInfo, ProcessLookup
    from local_ai_stack.launcher import ProcessLauncher
    from local_ai_stack.registry import ManagedProcessRegistry

    ledger = HandleLedger(monkeypatch)
    monkeypatch.setattr(job_module, "member_pids", lambda job: (root, child))
    monkeypatch.setattr(
        job_module,
        "membership",
        lambda job, pid: Membership.MEMBER if job is not None else Membership.UNPROVABLE,
    )

    # The root is alive and ours until it is asked to stop; the child stays.
    root_info = ProcessInfo(root, "python.exe", PYTHON, "root", 500.0)
    state = {
        "root_alive": True,
        "tree_terminated": False,
        "child_checks_after_terminate": 0,
    }

    def terminate_tree(job, exit_code=1):
        if terminate_ok:
            state["tree_terminated"] = True
        return terminate_ok

    monkeypatch.setattr(job_module, "terminate", terminate_tree)

    def fake_lookup(pid: int) -> ProcessLookup:
        if pid == root and not state["root_alive"]:
            return ProcessLookup(LookupOutcome.NOT_FOUND, detail="gone")
        if pid == child and state["tree_terminated"]:
            state["child_checks_after_terminate"] += 1
            if state["child_checks_after_terminate"] > child_exit_after:
                return ProcessLookup(LookupOutcome.NOT_FOUND, detail="gone")
        info = root_info if pid == root else ProcessInfo(pid, "python.exe", PYTHON, "child", 600.0)
        return ProcessLookup(LookupOutcome.FOUND, info)

    monkeypatch.setattr(registry_module, "lookup_process", fake_lookup)
    monkeypatch.setattr(launcher_module, "lookup_process", fake_lookup)

    def fake_terminate_process(pid: int) -> None:
        assert pid == root, "only the registered root is ever asked to stop"
        state["root_alive"] = False

    monkeypatch.setattr(launcher_module, "_terminate", fake_terminate_process)

    def forbidden_kill(pid: int) -> None:
        raise AssertionError("a failed tree termination must not fall back to killing pids")

    monkeypatch.setattr(launcher_module, "_kill", forbidden_kill)

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(
        registry,
        spawn=fake_spawn_returning(root),
        clock=lambda: 500.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    launcher.start(launch_spec())
    return launcher, registry, ledger, state


def test_a_failed_tree_termination_still_releases_the_record_and_the_job(tmp_path, monkeypatch):
    """The blocker: root gone, tree termination failed, state must be let go."""
    from local_ai_stack.launcher import StopOutcome

    launcher, registry, ledger, _ = stop_scenario(
        monkeypatch, tmp_path, root=7001, child=7002, terminate_ok=False
    )

    result = launcher.stop("fixture", timeout=5, allow_force=True)

    # The blocker itself: nothing usable may be retained once the root is gone.
    assert registry.load().entries == {}, "the record described a dead process"
    assert launcher._jobs == {}, "the job handle was released"
    assert ledger.balanced, "no handle was leaked"

    # And the result stays honest about all three facts at once.
    assert result.outcome is StopOutcome.STOPPED, "the root really did stop"
    assert result.forced is False, "nothing was force terminated"
    assert result.tree_pids == (), "no tree was claimed"
    assert result.left_running == (7002,), "the descendant is reported as possibly alive"
    assert "could not be terminated" in result.detail
    assert "may still be running" in result.detail


def test_a_successful_tree_termination_still_releases_everything(tmp_path, monkeypatch):
    """The sibling path, so the fix cannot regress it."""
    from local_ai_stack.launcher import StopOutcome

    launcher, registry, ledger, _ = stop_scenario(
        monkeypatch, tmp_path, root=7101, child=7102, terminate_ok=True
    )

    result = launcher.stop("fixture", timeout=5, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert result.forced is True
    assert result.tree_pids == (7101, 7102)
    assert registry.load().entries == {}
    assert launcher._jobs == {}
    assert ledger.balanced


def test_tree_termination_waits_for_every_proved_descendant(tmp_path, monkeypatch):
    from local_ai_stack.launcher import StopOutcome

    launcher, registry, ledger, state = stop_scenario(
        monkeypatch,
        tmp_path,
        root=7151,
        child=7152,
        terminate_ok=True,
        child_exit_after=3,
    )

    result = launcher.stop("fixture", timeout=5, allow_force=True)

    assert result.outcome is StopOutcome.STOPPED
    assert state["child_checks_after_terminate"] == 4
    assert result.tree_pids == (7151, 7152)
    assert registry.load().entries == {}
    assert launcher._jobs == {}
    assert ledger.balanced


def test_a_root_that_exits_without_force_releases_everything_too(tmp_path, monkeypatch):
    """And the conservative path keeps its own cleanup."""
    from local_ai_stack.launcher import StopOutcome

    launcher, registry, ledger, _ = stop_scenario(
        monkeypatch, tmp_path, root=7201, child=7202, terminate_ok=False
    )

    result = launcher.stop("fixture", timeout=5)

    assert result.outcome is StopOutcome.STOPPED
    assert result.tree_pids == ()
    assert result.left_running == (7202,)
    assert "force was not allowed" in result.detail
    assert registry.load().entries == {}
    assert launcher._jobs == {}
    assert ledger.balanced
