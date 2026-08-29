"""Local OS inventory: which TCP sockets are listening and which processes exist.

psutil is a declared dependency, but every call here degrades gracefully when it
is missing or the OS refuses the query: callers get a report flagged
``UNAVAILABLE`` instead of an exception, so a missing permission never turns
into a wrong health verdict.

This module only observes. It does not start, stop or classify anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None

WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ScanOutcome(str, Enum):
    """Whether a local scan produced trustworthy data."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Listener:
    host: str
    port: int
    pid: int | None = None


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str = ""
    exe: str | None = None
    cmdline: str = ""
    create_time: float | None = None


class LookupOutcome(str, Enum):
    """Result of asking the OS about one specific pid."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProcessLookup:
    """One pid, answered honestly.

    ``NOT_FOUND`` means the pid is gone. ``UNAVAILABLE`` means we could not
    look: the two must never be confused, because only the first one proves a
    registered process is really dead.
    """

    outcome: LookupOutcome
    process: ProcessInfo | None = None
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.outcome is LookupOutcome.FOUND


@dataclass(frozen=True)
class ListenerReport:
    outcome: ScanOutcome
    listeners: tuple[Listener, ...] = ()
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome is ScanOutcome.OK

    def on_port(self, port: int, host: str = "127.0.0.1") -> tuple[Listener, ...]:
        return tuple(
            listener
            for listener in self.listeners
            if listener.port == port and host_matches(listener.host, host)
        )


@dataclass(frozen=True)
class ProcessReport:
    outcome: ScanOutcome
    processes: tuple[ProcessInfo, ...] = ()
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome is ScanOutcome.OK

    def by_pid(self, pid: int) -> ProcessInfo | None:
        return next((p for p in self.processes if p.pid == pid), None)


def host_matches(bound: str, wanted: str) -> bool:
    """True when a socket bound to ``bound`` also serves ``wanted``."""
    if bound in WILDCARD_HOSTS or bound == wanted:
        return True
    return bound in LOOPBACK_HOSTS and wanted in LOOPBACK_HOSTS


def _unavailable_detail(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}".strip().rstrip(":")


def list_listeners() -> ListenerReport:
    """Every TCP socket in LISTEN state, or a report saying why we cannot tell."""
    if psutil is None:
        return ListenerReport(ScanOutcome.UNAVAILABLE, detail="psutil is not installed")
    try:
        connections = list(psutil.net_connections(kind="tcp"))
    except (psutil.Error, NotImplementedError, OSError) as exc:
        return ListenerReport(ScanOutcome.UNAVAILABLE, detail=_unavailable_detail(exc))
    listeners = [
        Listener(conn.laddr.ip, conn.laddr.port, conn.pid)
        for conn in connections
        if conn.status == psutil.CONN_LISTEN and conn.laddr
    ]
    return ListenerReport(ScanOutcome.OK, tuple(listeners))


def list_processes(*, exclude_self: bool = True) -> ProcessReport:
    """Every visible process, or a report saying why we cannot tell."""
    if psutil is None:
        return ProcessReport(ScanOutcome.UNAVAILABLE, detail="psutil is not installed")
    own_pid = os.getpid()
    try:
        raw = list(psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"]))
    except (psutil.Error, NotImplementedError, OSError) as exc:
        return ProcessReport(ScanOutcome.UNAVAILABLE, detail=_unavailable_detail(exc))
    processes = []
    for proc in raw:
        info = getattr(proc, "info", None) or {}
        pid = info.get("pid")
        if pid is None or (exclude_self and pid == own_pid):
            continue
        cmdline = info.get("cmdline") or []
        processes.append(
            ProcessInfo(
                pid,
                info.get("name") or "",
                info.get("exe"),
                " ".join(cmdline),
                info.get("create_time"),
            )
        )
    return ProcessReport(ScanOutcome.OK, tuple(processes))


def lookup_process(pid: int) -> ProcessLookup:
    """Inspect a single pid, distinguishing "gone" from "cannot tell"."""
    if psutil is None:
        return ProcessLookup(LookupOutcome.UNAVAILABLE, detail="psutil is not installed")
    if pid <= 0:
        return ProcessLookup(LookupOutcome.NOT_FOUND, detail=f"invalid pid {pid}")
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            name = _best_effort(proc.name) or ""
            exe = _best_effort(proc.exe)
            cmdline = _best_effort(proc.cmdline) or []
            create_time = _best_effort(proc.create_time)
    except psutil.NoSuchProcess:
        return ProcessLookup(LookupOutcome.NOT_FOUND, detail=f"no process with pid {pid}")
    except (psutil.Error, NotImplementedError, OSError) as exc:
        return ProcessLookup(LookupOutcome.UNAVAILABLE, detail=_unavailable_detail(exc))
    return ProcessLookup(
        LookupOutcome.FOUND,
        ProcessInfo(pid, name, exe, " ".join(cmdline), create_time),
    )


def _best_effort(getter):
    """Read one psutil attribute, tolerating a denied or racing process."""
    try:
        return getter()
    except (psutil.Error, OSError):
        return None
