"""Start and stop the processes this manager owns.

Two rules shape every function here.

**Only ours.** ``stop`` takes a service id, looks it up in the registry and
verifies identity before touching anything. There is deliberately no API that
takes a bare pid and terminates it: a process we did not start cannot be stopped
through this module at all, whatever flags a caller passes. ``allow_force`` only
escalates ``terminate`` to ``kill`` for a process already proven to be ours.

**Spawned is not healthy.** Starting a process yields a pid and nothing more.
Whether the service actually serves is a health question, answered by
:mod:`local_ai_stack.services`, and the two are never merged into one verdict.

Launching is generic: it knows argv, cwd, env and log files, not services.

**A tree is stopped only where it can be proved.** A managed process is
launched into a kernel job (see :mod:`local_ai_stack.jobs`), the only thing on
Windows that can answer "did I start this process?" for a child we never saw
being created. Where that proof exists, stopping asks the kernel for the
membership and terminates exactly that set. Where it does not — another session
created the job, or the host cannot make one — stopping falls back to the single
process whose identity the registry can still verify, and says so. Descendants
are never inferred from parent pids. See docs/INTEGRATION.md for the two
boundaries and what closing them would cost.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import jobs
from .inventory import LookupOutcome, ProcessReport, lookup_process
from .jobs import Membership, ProcessJob
from .logs import LogFiles
from .registry import IdentityVerdict, ManagedProcess, ManagedProcessRegistry, build_entry

logger = logging.getLogger("local_ai_stack.launcher")

DEFAULT_STOP_TIMEOUT = 10.0
DEFAULT_STARTUP_TIMEOUT = 20.0
STOP_POLL_INTERVAL = 0.1

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None


class LaunchOutcome(str, Enum):
    STARTED = "started"
    ALREADY_MANAGED = "already_managed"
    ALREADY_RUNNING_EXTERNAL = "already_running_external"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    SPAWN_FAILED = "spawn_failed"
    DRY_RUN = "dry_run"


class StopOutcome(str, Enum):
    STOPPED = "stopped"
    NOT_RUNNING = "not_running"
    NOT_MANAGED = "not_managed"
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_UNKNOWN = "identity_unknown"
    TIMEOUT = "timeout"
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class ProcessLaunchSpec:
    """Everything needed to spawn one managed process. No service knowledge."""

    service_id: str
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    inherit_env: bool = True
    logs: LogFiles | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    stop_timeout: float = DEFAULT_STOP_TIMEOUT
    allow_force_kill: bool = False

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv must not be empty")
        if self.startup_timeout <= 0 or self.stop_timeout <= 0:
            raise ValueError("timeouts must be > 0")

    @property
    def executable(self) -> str:
        return self.argv[0]

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The child's environment. os.environ is read, never written."""
        source = os.environ if base is None else base
        merged = dict(source) if self.inherit_env else {}
        merged.update(self.env)
        return merged


@dataclass(frozen=True)
class SpawnedProcess:
    pid: int
    # The handle the spawn already owns. Assigning a job through it avoids
    # reopening the pid, which is where a pid-reuse race would creep in.
    handle: int | None = None


@dataclass(frozen=True)
class LaunchResult:
    outcome: LaunchOutcome
    service_id: str
    pid: int | None = None
    detail: str = ""
    entry: ManagedProcess | None = None
    logs: LogFiles | None = None

    @property
    def started(self) -> bool:
        """The process exists now. It says nothing about the service serving."""
        return self.outcome is LaunchOutcome.STARTED


@dataclass(frozen=True)
class StopResult:
    outcome: StopOutcome
    service_id: str
    pid: int | None = None
    detail: str = ""
    forced: bool = False
    # Every pid the kernel proved was in our tree and was terminated with it.
    # Empty when only the registered process itself was stopped.
    tree_pids: tuple[int, ...] = ()

    @property
    def stopped_tree(self) -> bool:
        return len(self.tree_pids) > 1

    @property
    def stopped(self) -> bool:
        """True when the process is no longer running, however that happened."""
        return self.outcome in {StopOutcome.STOPPED, StopOutcome.NOT_RUNNING}


SpawnFunction = Callable[..., SpawnedProcess]


def _creation_flags() -> dict[str, object]:
    """Detach the child so it outlives this call but stays addressable."""
    if os.name != "nt":
        return {"start_new_session": True}
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        flags |= getattr(subprocess, name, 0)
    return {"creationflags": flags} if flags else {}


def _process_handle(process: subprocess.Popen) -> int | None:
    """Our own copy of the process handle, which outlives the Popen object."""
    handle = getattr(process, "_handle", None)
    return jobs.duplicate_process_handle(int(handle)) if handle else None


def default_spawn(
    argv: Sequence[str],
    *,
    cwd: str | None,
    env: Mapping[str, str],
    stdout,
    stderr,
) -> SpawnedProcess:
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        shell=False,
        close_fds=True,
        **_creation_flags(),
    )
    return SpawnedProcess(process.pid, _process_handle(process))


class ProcessLauncher:
    """Spawns and stops managed processes, and keeps the registry honest."""

    def __init__(
        self,
        registry: ManagedProcessRegistry,
        *,
        dry_run: bool = False,
        spawn: SpawnFunction = default_spawn,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: logging.Logger | None = None,
    ) -> None:
        self.registry = registry
        self.dry_run = dry_run
        self._spawn = spawn
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self.log = log or logger
        # Job handles live exactly as long as this launcher does. A job whose
        # last handle closes stops existing, so ownership proof cannot outlive
        # the session that created it, and is never faked across one.
        self._jobs: dict[str, ProcessJob] = {}

    # --- start -------------------------------------------------------------

    def start(
        self,
        spec: ProcessLaunchSpec,
        *,
        processes: ProcessReport | None = None,
        dry_run: bool | None = None,
    ) -> LaunchResult:
        """Spawn the process, then register it. Never registers a failed spawn."""
        is_dry = self.dry_run if dry_run is None else dry_run

        existing = self.registry.status(spec.service_id, processes=processes)
        if existing is not None and existing.verdict is IdentityVerdict.LIVE:
            return LaunchResult(
                LaunchOutcome.ALREADY_MANAGED,
                spec.service_id,
                existing.entry.pid,
                f"already running as pid {existing.entry.pid}",
                existing.entry,
            )

        executable = Path(spec.executable)
        if not executable.is_file():
            return LaunchResult(
                LaunchOutcome.EXECUTABLE_NOT_FOUND,
                spec.service_id,
                detail=f"executable not found: {spec.executable}",
            )
        if spec.cwd is not None and not Path(spec.cwd).is_dir():
            return LaunchResult(
                LaunchOutcome.SPAWN_FAILED,
                spec.service_id,
                detail=f"working directory not found: {spec.cwd}",
            )

        if is_dry:
            return LaunchResult(
                LaunchOutcome.DRY_RUN,
                spec.service_id,
                detail=f"[dry-run] would start {' '.join(spec.argv)}",
                logs=spec.logs,
            )

        job = jobs.create_job(jobs.new_job_name(spec.service_id))
        streams = _open_logs(spec.logs)
        try:
            spawned = self._spawn(
                spec.argv,
                cwd=str(spec.cwd) if spec.cwd is not None else None,
                env=spec.child_env(),
                stdout=streams.stdout,
                stderr=streams.stderr,
            )
            # Adopt before anything else runs. The child is already executing,
            # so this window is small but not zero: see _adopt.
            job = self._adopt(spec.service_id, job, spawned)
        except (OSError, ValueError) as exc:
            jobs.close(job)
            return LaunchResult(
                LaunchOutcome.SPAWN_FAILED,
                spec.service_id,
                detail=f"spawn failed: {exc.__class__.__name__}: {exc}",
            )
        finally:
            streams.close()

        entry = build_entry(
            spec.service_id,
            spawned.pid,
            spec.executable,
            spec.argv,
            log_file=spec.logs.stdout if spec.logs is not None else None,
            job_name=job.name if job is not None else None,
            clock=self._clock,
        )
        self.registry.register(entry)
        self.log.info("started %s as pid %s", spec.service_id, spawned.pid)
        return LaunchResult(
            LaunchOutcome.STARTED,
            spec.service_id,
            spawned.pid,
            f"started pid {spawned.pid}",
            entry,
            spec.logs,
        )

    def _adopt(
        self, service_id: str, job: ProcessJob | None, spawned: SpawnedProcess
    ) -> ProcessJob | None:
        """Put the new process in its job, or give the job up entirely.

        A job that could not take the process is worse than no job at all: it
        would name a container the process is not in. So it is closed and
        forgotten, and the launcher falls back to single-process ownership.

        Known window: the child is already running by the time Popen returns,
        so a descendant it creates in those first instructions is never in the
        job. That descendant is then simply not provable, and this manager
        leaves it alone rather than guessing. Closing the window needs the
        process to be created suspended and resumed after assignment, which
        subprocess cannot express; see the module docstring.
        """
        if job is None:
            return None
        previous = self._jobs.pop(service_id, None)
        if previous is not None:
            jobs.close(previous)
        try:
            if not jobs.assign(job, spawned.handle or 0):
                self.log.debug("could not assign %s to a job; tree stop unavailable", service_id)
                jobs.close(job)
                return None
        finally:
            jobs.close_handle(spawned.handle)
        self._jobs[service_id] = job
        return job

    # --- ownership proof ---------------------------------------------------

    def tree_pids(self, service_id: str, pid: int) -> tuple[int, ...] | None:
        """Every process the kernel proves belongs to the tree we started.

        ``None`` means unprovable, which is not the same as "no children": a
        manager that restarted, or a host without job objects, gets ``None``
        and must act on the single verified process instead.
        """
        job = self._jobs.get(service_id)
        if job is None or jobs.membership(job, pid) is not Membership.MEMBER:
            return None
        return jobs.member_pids(job)

    def owns(self, service_id: str, pid: int) -> Membership:
        """Can this manager prove ``pid`` is part of the tree it started?"""
        return jobs.membership(self._jobs.get(service_id), pid)

    def forget_job(self, service_id: str) -> None:
        """Release a job handle. Members keep running; proof is given up."""
        jobs.close(self._jobs.pop(service_id, None))

    # --- stop --------------------------------------------------------------

    def stop(
        self,
        service_id: str,
        *,
        timeout: float = DEFAULT_STOP_TIMEOUT,
        allow_force: bool = False,
        processes: ProcessReport | None = None,
        dry_run: bool | None = None,
    ) -> StopResult:
        """Stop a process we own. Anything else is refused, not killed."""
        is_dry = self.dry_run if dry_run is None else dry_run
        status = self.registry.status(service_id, processes=processes)
        if status is None:
            return StopResult(
                StopOutcome.NOT_MANAGED,
                service_id,
                detail="no managed process registered for this service",
            )

        pid = status.entry.pid
        if status.verdict is IdentityVerdict.STALE:
            if not is_dry:
                self.registry.unregister(service_id)
            return StopResult(
                StopOutcome.NOT_RUNNING, service_id, pid, "process already gone, entry cleaned"
            )
        if status.verdict is IdentityVerdict.MISMATCH:
            if not is_dry:
                self.registry.unregister(service_id)
            return StopResult(
                StopOutcome.IDENTITY_MISMATCH,
                service_id,
                pid,
                f"refusing to stop pid {pid}: {status.detail}",
            )
        if status.verdict is IdentityVerdict.UNKNOWN:
            return StopResult(
                StopOutcome.IDENTITY_UNKNOWN,
                service_id,
                pid,
                f"refusing to stop pid {pid}: {status.detail}",
            )

        if is_dry:
            return StopResult(
                StopOutcome.DRY_RUN, service_id, pid, f"[dry-run] would stop pid {pid}"
            )

        proved = self.tree_pids(service_id, pid)
        if proved:
            return self._stop_tree(service_id, pid, proved, timeout)

        try:
            _terminate(pid)
        except (OSError, RuntimeError) as exc:
            return StopResult(
                StopOutcome.FAILED, service_id, pid, f"terminate failed: {exc.__class__.__name__}"
            )

        if self._wait_for_exit(pid, timeout):
            self.registry.unregister(service_id)
            return StopResult(
                StopOutcome.STOPPED, service_id, pid, self._single_detail(status.entry)
            )

        if not allow_force:
            return StopResult(
                StopOutcome.TIMEOUT,
                service_id,
                pid,
                f"pid {pid} still alive after {timeout:.1f}s; force kill not allowed",
            )

        try:
            _kill(pid)
        except (OSError, RuntimeError) as exc:
            return StopResult(
                StopOutcome.FAILED, service_id, pid, f"kill failed: {exc.__class__.__name__}"
            )
        if self._wait_for_exit(pid, timeout):
            self.registry.unregister(service_id)
            return StopResult(StopOutcome.STOPPED, service_id, pid, "force killed", forced=True)
        return StopResult(
            StopOutcome.FAILED, service_id, pid, f"pid {pid} survived force kill", forced=True
        )

    def _single_detail(self, entry: ManagedProcess) -> str:
        """Say plainly when only the registered process could be acted on."""
        base = "terminated gracefully"
        if entry.job_name is None:
            return base
        if not jobs.jobs_supported():
            return f"{base}; tree ownership is not provable on this host"
        return (
            f"{base}; the job that proved this tree belonged to another session, "
            "so any child processes were left running"
        )

    def _stop_tree(
        self, service_id: str, pid: int, proved: tuple[int, ...], timeout: float
    ) -> StopResult:
        """Terminate exactly the processes the kernel says are in our job."""
        job = self._jobs.get(service_id)
        if not jobs.terminate(job):
            return StopResult(
                StopOutcome.FAILED, service_id, pid, "could not terminate the process tree"
            )
        if not self._wait_for_exit(pid, timeout):
            return StopResult(
                StopOutcome.TIMEOUT,
                service_id,
                pid,
                f"pid {pid} still alive after {timeout:.1f}s despite terminating its tree",
                tree_pids=proved,
            )
        self.forget_job(service_id)
        self.registry.unregister(service_id)
        others = len(proved) - 1
        detail = "terminated the process tree we started"
        if others > 0:
            detail = f"{detail} ({others} child process(es) proved ours and stopped with it)"
        return StopResult(StopOutcome.STOPPED, service_id, pid, detail, tree_pids=proved)

    def _wait_for_exit(self, pid: int, timeout: float) -> bool:
        deadline = self._monotonic() + timeout
        while True:
            if lookup_process(pid).outcome is LookupOutcome.NOT_FOUND:
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(STOP_POLL_INTERVAL)


@dataclass
class _Streams:
    stdout: object
    stderr: object
    _opened: list = field(default_factory=list)

    def close(self) -> None:
        for handle in self._opened:
            handle.close()


def _open_logs(logs: LogFiles | None) -> _Streams:
    """Append-mode handles the child inherits; the parent closes its own copy."""
    if logs is None:
        return _Streams(subprocess.DEVNULL, subprocess.DEVNULL)
    logs.ensure_parents()
    # Not a context manager on purpose: the handle must stay open across the
    # spawn so the child inherits it, and is closed by _Streams.close() after.
    stdout = open(logs.stdout, "ab", buffering=0)  # noqa: SIM115
    if logs.merged:
        return _Streams(stdout, subprocess.STDOUT, [stdout])
    stderr = open(logs.stderr, "ab", buffering=0)  # noqa: SIM115
    return _Streams(stdout, stderr, [stdout, stderr])


def _terminate(pid: int) -> None:
    if psutil is not None:
        try:
            psutil.Process(pid).terminate()
            return
        except psutil.NoSuchProcess:
            return
        except psutil.Error as exc:
            raise RuntimeError(str(exc)) from exc
    import signal

    os.kill(pid, getattr(signal, "SIGTERM", signal.SIGINT))


def _kill(pid: int) -> None:
    if psutil is not None:
        try:
            psutil.Process(pid).kill()
            return
        except psutil.NoSuchProcess:
            return
        except psutil.Error as exc:
            raise RuntimeError(str(exc)) from exc
    import signal

    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
