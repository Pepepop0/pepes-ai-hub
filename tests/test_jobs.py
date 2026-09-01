"""Ownership of a process tree, proved rather than inferred.

Every process these tests start is a Python child the test fully controls, and
each one is reaped. Nothing real is ever started or stopped.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from local_ai_stack import jobs
from local_ai_stack.jobs import Membership

WINDOWS_ONLY = pytest.mark.skipif(
    not jobs.jobs_supported(), reason="job objects are a Windows primitive"
)

SLEEPER = "import time; time.sleep(60)"
# A parent that spawns one grandchild and reports its pid, so the test knows a
# process it never launched itself.
PARENT_WITH_CHILD = (
    "import subprocess, sys, time;"
    "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "sys.stdout.write(str(kid.pid)); sys.stdout.write(chr(10)); sys.stdout.flush();"
    "time.sleep(60)"
)


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


def spawn(code: str, *, capture: bool = False) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
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
    jobs.close(None)  # must not raise


def test_a_job_name_is_unique_per_start():
    first, second = jobs.new_job_name("ollama"), jobs.new_job_name("ollama")
    assert first != second
    assert first.startswith("PepitosAIHub-ollama-")


def test_terminate_takes_a_job_not_a_pid():
    """There is no call here that could ever be handed a stranger's pid."""
    with pytest.raises(AttributeError):
        jobs.terminate(4242)  # type: ignore[arg-type]


# --- the proof itself ------------------------------------------------------


@WINDOWS_ONLY
def test_a_job_enumerates_the_tree_it_owns(reaper):
    job = jobs.create_job(jobs.new_job_name("fixture"))
    assert job is not None
    parent = spawn(PARENT_WITH_CHILD, capture=True)
    reaper.append(parent.pid)
    assert jobs.assign(job, int(parent._handle))
    grandchild = int(parent.stdout.readline().strip())
    reaper.append(grandchild)
    time.sleep(0.3)

    members = jobs.member_pids(job)
    assert members is not None
    assert parent.pid in members
    assert grandchild in members, "a process we never launched, proved ours"
    jobs.close(job)


@WINDOWS_ONLY
def test_a_process_we_did_not_start_is_never_a_member(reaper):
    job = jobs.create_job(jobs.new_job_name("fixture"))
    ours = spawn(SLEEPER)
    stranger = spawn(SLEEPER)
    reaper.extend([ours.pid, stranger.pid])
    assert jobs.assign(job, int(ours._handle))
    time.sleep(0.2)

    assert jobs.membership(job, ours.pid) is Membership.MEMBER
    assert jobs.membership(job, stranger.pid) is Membership.NOT_MEMBER
    jobs.close(job)


@WINDOWS_ONLY
def test_terminating_a_job_stops_the_tree_and_leaves_strangers_alone(reaper):
    job = jobs.create_job(jobs.new_job_name("fixture"))
    parent = spawn(PARENT_WITH_CHILD, capture=True)
    # Adopt immediately, the way the launcher does, before reading anything.
    assert jobs.assign(job, int(parent._handle))
    stranger = spawn(SLEEPER)
    reaper.extend([parent.pid, stranger.pid])
    grandchild = int(parent.stdout.readline().strip())
    reaper.append(grandchild)
    time.sleep(0.3)

    assert grandchild in (jobs.member_pids(job) or ())
    assert jobs.terminate(job)
    assert wait_gone(parent.pid)
    assert wait_gone(grandchild), "the child went with the tree"
    assert alive(stranger.pid), "a process outside the job is untouched"
    jobs.close(job)


@WINDOWS_ONLY
def test_a_descendant_born_before_adoption_is_not_provable_and_is_left_alone(reaper):
    """The known window, pinned as behaviour rather than hidden.

    A process assigned to a job after it is already running may have spawned a
    child in the meantime. That child is outside the job, so it is not provably
    ours — and the safe failure is to leave it running, never to guess.
    """
    job = jobs.create_job(jobs.new_job_name("fixture"))
    parent = spawn(PARENT_WITH_CHILD, capture=True)
    reaper.append(parent.pid)
    grandchild = int(parent.stdout.readline().strip())  # born before adoption
    reaper.append(grandchild)
    assert jobs.assign(job, int(parent._handle))
    time.sleep(0.2)

    assert jobs.membership(job, parent.pid) is Membership.MEMBER
    assert jobs.membership(job, grandchild) is Membership.NOT_MEMBER

    assert jobs.terminate(job)
    assert wait_gone(parent.pid)
    assert alive(grandchild), "left running, because it could not be proved ours"
    jobs.close(job)


@WINDOWS_ONLY
def test_proof_does_not_survive_losing_the_handle(reaper):
    """The boundary this design is honest about, pinned as behaviour.

    A job stops existing when its last handle closes, even though its members
    keep running. A manager that restarts therefore cannot reopen the job and
    must not claim it owns the tree.
    """
    name = jobs.new_job_name("fixture")
    job = jobs.create_job(name)
    child = spawn(SLEEPER)
    reaper.append(child.pid)
    assert jobs.assign(job, int(child._handle))

    jobs.close(job)
    time.sleep(0.2)
    assert jobs.open_job(name) is None, "the name no longer resolves"
    assert alive(child.pid), "but the service itself keeps running"
    assert jobs.membership(None, child.pid) is Membership.UNPROVABLE


# --- through the launcher --------------------------------------------------


@WINDOWS_ONLY
def test_the_launcher_stops_the_tree_it_can_prove(tmp_path, reaper):
    """End to end: a managed start owns its children, and only its children."""
    from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec, StopOutcome
    from local_ai_stack.logs import LogFiles
    from local_ai_stack.registry import ManagedProcessRegistry

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry)
    logs = LogFiles(tmp_path / "logs" / "fixture.log")
    spec = ProcessLaunchSpec("fixture", (sys.executable, "-c", PARENT_WITH_CHILD), logs=logs)

    stranger = spawn(SLEEPER)
    reaper.append(stranger.pid)

    started = launcher.start(spec)
    reaper.append(started.pid)
    assert started.started
    entry = registry.load().get("fixture")
    assert entry.job_name is not None, "the start recorded the container it used"

    # The child announces itself through the service log, not through a pipe.
    deadline = time.monotonic() + 10
    child_pid = None
    while time.monotonic() < deadline and child_pid is None:
        text = logs.stdout.read_text(encoding="utf-8").strip() if logs.stdout.exists() else ""
        if text:
            child_pid = int(text.splitlines()[0])
        time.sleep(0.05)
    assert child_pid is not None
    reaper.append(child_pid)

    assert launcher.owns("fixture", started.pid) is Membership.MEMBER
    assert launcher.owns("fixture", stranger.pid) is Membership.NOT_MEMBER
    proved = launcher.tree_pids("fixture", started.pid)
    assert proved is not None and started.pid in proved

    result = launcher.stop("fixture", timeout=10)
    assert result.outcome is StopOutcome.STOPPED
    assert result.tree_pids
    assert wait_gone(started.pid)
    assert alive(stranger.pid), "nothing outside the tree was touched"
    assert registry.load().entries == {}


@WINDOWS_ONLY
def test_without_the_job_the_launcher_stops_only_what_it_can_verify(tmp_path, reaper):
    """A restarted manager: the entry survives, the proof does not."""
    from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec, StopOutcome
    from local_ai_stack.registry import ManagedProcessRegistry

    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launcher = ProcessLauncher(registry)
    spec = ProcessLaunchSpec("fixture", (sys.executable, "-c", SLEEPER))
    started = launcher.start(spec)
    reaper.append(started.pid)

    # Drop the handle the way a manager restart would.
    launcher.forget_job("fixture")
    assert launcher.owns("fixture", started.pid) is Membership.UNPROVABLE
    assert launcher.tree_pids("fixture", started.pid) is None

    result = launcher.stop("fixture", timeout=10)
    assert result.outcome is StopOutcome.STOPPED
    assert result.tree_pids == (), "no tree was claimed"
    assert "another session" in result.detail
    assert wait_gone(started.pid)
