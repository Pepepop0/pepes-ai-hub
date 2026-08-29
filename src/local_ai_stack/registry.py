"""Registry of processes this manager started itself.

Ownership is the whole point of this module. Pepito's AI Hub may only stop what
it started, and "what it started" is not a pid: pids are recycled by the OS, so
a bare pid is never proof of ownership. Every entry therefore carries an
identity fingerprint — the process creation time first, the executable and argv
as a fallback — and every read re-verifies it against the live process before
anything is allowed to act on it.

The four verdicts are deliberately distinct:

``LIVE``
    the pid exists and the identity matches: this is ours.
``STALE``
    the pid is gone: our process died, drop the entry.
``MISMATCH``
    the pid exists but is somebody else's process, almost certainly a recycled
    pid. Our process is gone, so the entry is dropped — but the live process is
    never touched.
``UNKNOWN``
    we could not inspect the pid. Nothing is assumed and nothing is acted on.

Storage is one JSON document under ``StackLayout.state``, written atomically.
No database, no daemon, no background thread.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .inventory import LookupOutcome, ProcessInfo, ProcessReport, lookup_process

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_NAME = "managed-processes"
# psutil reports creation time as a float; a whole second of slack is far more
# than any rounding, and far less than the gap between a pid dying and the OS
# handing the same number to a new process.
CREATE_TIME_TOLERANCE = 1.0


class IdentityVerdict(str, Enum):
    LIVE = "live"
    STALE = "stale"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1].casefold()


@dataclass(frozen=True)
class ManagedProcess:
    """One process this manager started, with enough identity to prove it."""

    service_id: str
    pid: int
    executable: str
    argv: tuple[str, ...] = ()
    started_at: float = 0.0
    create_time: float | None = None
    log_file: str | None = None

    @property
    def fingerprint(self) -> str:
        """Human-readable identity, for logs and error messages."""
        return f"{_basename(self.executable)}#{self.pid}"

    def identity_matches(self, process: ProcessInfo) -> bool:
        """Is ``process`` the process we started, or a pid recycled by the OS?"""
        if process.pid != self.pid:
            return False
        if self.create_time is not None and process.create_time is not None:
            return abs(process.create_time - self.create_time) <= CREATE_TIME_TOLERANCE
        # No creation time on one of the sides: fall back to what the process
        # claims to be. Weaker, so it is only ever a fallback.
        candidates = {_basename(process.exe or ""), _basename(process.name or "")}
        candidates.discard("")
        if candidates and _basename(self.executable) in candidates:
            return True
        return bool(self.argv) and self.argv[0].casefold() in process.cmdline.casefold()

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "pid": self.pid,
            "executable": self.executable,
            "argv": list(self.argv),
            "started_at": self.started_at,
            "create_time": self.create_time,
            "log_file": self.log_file,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ManagedProcess | None:
        """Parse one entry, returning ``None`` for anything malformed."""
        if not isinstance(data, dict):
            return None
        service_id = data.get("service_id")
        pid = data.get("pid")
        executable = data.get("executable")
        if not isinstance(service_id, str) or not service_id:
            return None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        if not isinstance(executable, str) or not executable:
            return None
        argv = data.get("argv")
        argv_tuple = tuple(str(item) for item in argv) if isinstance(argv, list) else ()
        started_at = data.get("started_at")
        create_time = data.get("create_time")
        log_file = data.get("log_file")
        return cls(
            service_id,
            pid,
            executable,
            argv_tuple,
            float(started_at) if isinstance(started_at, (int, float)) else 0.0,
            float(create_time) if isinstance(create_time, (int, float)) else None,
            log_file if isinstance(log_file, str) else None,
        )


@dataclass(frozen=True)
class EntryStatus:
    """A registry entry checked against the live machine."""

    entry: ManagedProcess
    verdict: IdentityVerdict
    process: ProcessInfo | None = None
    detail: str = ""

    @property
    def service_id(self) -> str:
        return self.entry.service_id

    @property
    def live(self) -> bool:
        return self.verdict is IdentityVerdict.LIVE

    @property
    def actionable(self) -> bool:
        """Whether stopping this entry's pid is allowed at all."""
        return self.verdict is IdentityVerdict.LIVE


@dataclass(frozen=True)
class RegistrySnapshot:
    """What the state file currently holds, plus why it may be empty."""

    entries: Mapping[str, ManagedProcess] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def get(self, service_id: str) -> ManagedProcess | None:
        return self.entries.get(service_id)


class ManagedProcessRegistry:
    """The on-disk list of processes we own. Reads verify, writes are atomic."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self._clock = clock

    @classmethod
    def from_layout(cls, layout, *, clock=time.time) -> ManagedProcessRegistry:
        return cls(layout.state_file(REGISTRY_NAME), clock=clock)

    # --- persistence -------------------------------------------------------

    def load(self) -> RegistrySnapshot:
        """Read the state file. Never raises: a broken file is an empty one."""
        if not self.path.is_file():
            return RegistrySnapshot()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return RegistrySnapshot(error=f"unreadable registry: {exc.__class__.__name__}")
        if not isinstance(raw, dict):
            return RegistrySnapshot(error="registry root is not an object")
        version = raw.get("schema_version")
        if version != REGISTRY_SCHEMA_VERSION:
            return RegistrySnapshot(error=f"unsupported registry schema_version {version!r}")
        processes = raw.get("processes")
        if not isinstance(processes, list):
            return RegistrySnapshot(error="registry 'processes' is not a list")
        entries: dict[str, ManagedProcess] = {}
        skipped = 0
        for item in processes:
            entry = ManagedProcess.from_dict(item)
            if entry is None:
                skipped += 1
                continue
            entries[entry.service_id] = entry
        error = f"ignored {skipped} malformed entrie(s)" if skipped else ""
        return RegistrySnapshot(entries, error)

    def save(self, entries: Iterable[ManagedProcess], *, dry_run: bool = False) -> None:
        """Replace the state file atomically. A dry run writes nothing."""
        if dry_run:
            return
        document = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "updated_at": self._clock(),
            "processes": [entry.to_dict() for entry in entries],
        }
        payload = json.dumps(document, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f"{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    # --- mutation ----------------------------------------------------------

    def register(self, entry: ManagedProcess, *, dry_run: bool = False) -> ManagedProcess:
        """Record a process we just started, replacing any entry for its service."""
        entries = dict(self.load().entries)
        entries[entry.service_id] = entry
        self.save(entries.values(), dry_run=dry_run)
        return entry

    def unregister(self, service_id: str, *, dry_run: bool = False) -> bool:
        entries = dict(self.load().entries)
        if service_id not in entries:
            return False
        del entries[service_id]
        self.save(entries.values(), dry_run=dry_run)
        return True

    def prune(self, *, dry_run: bool = False) -> tuple[str, ...]:
        """Drop entries whose process is provably gone. Never touches a process."""
        statuses = self.statuses()
        dead = tuple(
            service_id
            for service_id, status in statuses.items()
            if status.verdict in {IdentityVerdict.STALE, IdentityVerdict.MISMATCH}
        )
        if dead:
            kept = [s.entry for s in statuses.values() if s.service_id not in dead]
            self.save(kept, dry_run=dry_run)
        return dead

    # --- verification ------------------------------------------------------

    def verify(
        self, entry: ManagedProcess, *, processes: ProcessReport | None = None
    ) -> EntryStatus:
        """Check one entry against the live process table."""
        if processes is not None and processes.usable:
            found = processes.by_pid(entry.pid)
            if found is None:
                return EntryStatus(entry, IdentityVerdict.STALE, detail=f"pid {entry.pid} is gone")
            return self._compare(entry, found)
        lookup = lookup_process(entry.pid)
        if lookup.outcome is LookupOutcome.NOT_FOUND:
            return EntryStatus(entry, IdentityVerdict.STALE, detail=lookup.detail)
        if lookup.outcome is LookupOutcome.UNAVAILABLE or lookup.process is None:
            return EntryStatus(
                entry,
                IdentityVerdict.UNKNOWN,
                detail=lookup.detail or "process inspection unavailable",
            )
        return self._compare(entry, lookup.process)

    def _compare(self, entry: ManagedProcess, process: ProcessInfo) -> EntryStatus:
        if entry.identity_matches(process):
            return EntryStatus(entry, IdentityVerdict.LIVE, process, "identity matches")
        return EntryStatus(
            entry,
            IdentityVerdict.MISMATCH,
            process,
            f"pid {entry.pid} now belongs to {process.name or 'another process'!r}",
        )

    def statuses(self, *, processes: ProcessReport | None = None) -> dict[str, EntryStatus]:
        return {
            service_id: self.verify(entry, processes=processes)
            for service_id, entry in self.load().entries.items()
        }

    def status(
        self, service_id: str, *, processes: ProcessReport | None = None
    ) -> EntryStatus | None:
        entry = self.load().get(service_id)
        return None if entry is None else self.verify(entry, processes=processes)

    def managed_pids(self, *, processes: ProcessReport | None = None) -> frozenset[int]:
        """Pids we own *right now* — the input every discovery call expects."""
        return frozenset(
            status.entry.pid
            for status in self.statuses(processes=processes).values()
            if status.live
        )


def build_entry(
    service_id: str,
    pid: int,
    executable: str | Path,
    argv: Sequence[str] = (),
    *,
    started_at: float | None = None,
    log_file: str | Path | None = None,
    clock=time.time,
) -> ManagedProcess:
    """Create an entry, capturing the live creation time when we can read it."""
    lookup = lookup_process(pid)
    create_time = lookup.process.create_time if lookup.process is not None else None
    return ManagedProcess(
        service_id,
        pid,
        str(executable),
        tuple(str(item) for item in argv),
        clock() if started_at is None else started_at,
        create_time,
        None if log_file is None else str(log_file),
    )


def with_create_time(entry: ManagedProcess, create_time: float | None) -> ManagedProcess:
    return replace(entry, create_time=create_time)
