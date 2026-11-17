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

A registered process is the *root* of a service, and the process that serves
the port is often not the root: ``open-webui.exe`` starts a venv interpreter
that starts the real one, and ``main.py`` under a venv ``python.exe`` runs in a
child of it. So recognising the service also needs the root's descendants, and
:func:`proven_descendants` proves them one hop at a time -- every hop must lead
to a live process that was created *before* its child, which is exactly what a
reused parent pid cannot satisfy. That proof says which processes may be
*recognised* as the service's own; it is never a licence to stop them. Stopping
still acts on the verified root, or on a kernel job (see :mod:`jobs`).

Storage is one JSON document under ``StackLayout.state``, written atomically.
The registry itself has no background thread. Opted-in Linux entries also carry
the authenticated live subreaper proof used by :mod:`posix_runtime`; they never
degrade to a bare-PID identity when that proof is unavailable.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from . import posix_runtime
from .inventory import LookupOutcome, ProcessInfo, ProcessReport, lookup_process

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_NAME = "managed-processes"
# psutil reports creation time as a float; a whole second of slack is far more
# than any rounding, and far less than the gap between a pid dying and the OS
# handing the same number to a new process.
CREATE_TIME_TOLERANCE = 1.0
# A service tree is a launcher, an interpreter or two, and the process serving
# the port. A chain longer than this is not a tree this manager started.
MAX_DESCENT_DEPTH = 16


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Lock a stable private inode; never unlink it while other CLIs may wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("registry lock is not a regular file")
        if os.name == "nt":
            import msvcrt

            if info.st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            if info.st_uid != os.getuid():
                raise PermissionError("registry lock belongs to another user")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


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
    # The kernel job this process was launched into, when the host could make
    # one. It names a container that only lives as long as the session that
    # created it, so it is a record of how the process was started, never on
    # its own a licence to act on anything.
    job_name: str | None = None
    # Present only for Linux subreaper-managed trees. A missing/invalid live
    # capability must NEVER fall back to the weaker single-PID fingerprint.
    posix_session: posix_runtime.SessionProof | None = field(default=None, repr=False)

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
        document = {
            "service_id": self.service_id,
            "pid": self.pid,
            "executable": self.executable,
            "argv": list(self.argv),
            "started_at": self.started_at,
            "create_time": self.create_time,
            "log_file": self.log_file,
            "job_name": self.job_name,
        }
        if self.posix_session is not None:
            document["posix_session"] = self.posix_session.to_dict()
        return document

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
        job_name = data.get("job_name")
        session = data.get("posix_session")
        proof = posix_runtime.SessionProof.from_dict(session) if session is not None else None
        if "posix_session" in data and (proof is None or proof.root_pid != pid):
            return None
        return cls(
            service_id,
            pid,
            executable,
            argv_tuple,
            float(started_at) if isinstance(started_at, (int, float)) else 0.0,
            float(create_time) if isinstance(create_time, (int, float)) else None,
            log_file if isinstance(log_file, str) else None,
            job_name if isinstance(job_name, str) else None,
            proof,
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

    def _mutation_lock(self):
        return _file_lock(self.path.with_name(f".{self.path.name}.mutation-lock"))

    @contextmanager
    def service_lock(self, service_id: str, *, dry_run: bool = False) -> Iterator[None]:
        """Serialize one service's ownership check through spawn/commit or stop."""
        if dry_run:
            yield
            return
        key = hashlib.sha256(service_id.encode("utf-8")).hexdigest()[:24]
        with _file_lock(self.path.with_name(f".{self.path.name}.service-{key}.lock")):
            yield

    # --- persistence -------------------------------------------------------

    def load(self) -> RegistrySnapshot:
        """Read the state file. Never raises: a broken file is an empty one."""
        if not self.path.is_file():
            return RegistrySnapshot()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
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
        with self._mutation_lock():
            self._write(entries)

    def _write(self, entries: Iterable[ManagedProcess]) -> None:
        """Replace the document while the caller holds the mutation lock."""
        document = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "updated_at": self._clock(),
            "processes": [entry.to_dict() for entry in entries],
        }
        payload = json.dumps(document, indent=2, sort_keys=True)
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
        """Merge one entry under a cross-process read-modify-write lock."""
        if not dry_run:
            with self._mutation_lock():
                entries = dict(self.load().entries)
                entries[entry.service_id] = entry
                self._write(entries.values())
        return entry

    def unregister(
        self,
        service_id: str,
        *,
        dry_run: bool = False,
        expected: ManagedProcess | None = None,
    ) -> bool:
        """Remove only the captured ownership record when ``expected`` is given."""
        if dry_run:
            current = self.load().get(service_id)
            return current is not None and (expected is None or current == expected)
        with self._mutation_lock():
            entries = dict(self.load().entries)
            current = entries.get(service_id)
            if current is None or (expected is not None and current != expected):
                return False
            del entries[service_id]
            self._write(entries.values())
            return True

    def prune(self, *, dry_run: bool = False) -> tuple[str, ...]:
        """Drop only unchanged dead records; verification never holds the lock."""
        dead = {
            service_id: status.entry
            for service_id, status in self.statuses().items()
            if status.verdict in {IdentityVerdict.STALE, IdentityVerdict.MISMATCH}
        }
        if dry_run or not dead:
            return tuple(dead)
        with self._mutation_lock():
            entries = dict(self.load().entries)
            removed = tuple(
                service_id
                for service_id, expected in dead.items()
                if entries.get(service_id) == expected
            )
            if removed:
                for service_id in removed:
                    del entries[service_id]
                self._write(entries.values())
            return removed

    # --- verification ------------------------------------------------------

    def verify(
        self, entry: ManagedProcess, *, processes: ProcessReport | None = None
    ) -> EntryStatus:
        """Check one entry against the live process table."""
        if entry.posix_session is not None:
            # A usable process scan can predate an exit/reparenting event; the
            # authenticated live subreaper is the authority for this entry.
            try:
                snapshot = posix_runtime.inspect(entry.posix_session)
            except posix_runtime.SessionGone:
                try:
                    alive = posix_runtime.root_alive(
                        entry.posix_session
                    ) or posix_runtime.guardian_alive(entry.posix_session)
                except OSError:
                    alive = True
                if not alive:
                    return EntryStatus(
                        entry, IdentityVerdict.STALE, detail="Linux managed session is gone"
                    )
                return EntryStatus(
                    entry,
                    IdentityVerdict.UNKNOWN,
                    detail="Linux supervisor is gone; refusing single-PID fallback",
                )
            except posix_runtime.SessionError:
                return EntryStatus(
                    entry,
                    IdentityVerdict.UNKNOWN,
                    detail="Linux session ownership could not be authenticated",
                )
            process = None
            if snapshot.root_alive:
                process = processes.by_pid(entry.pid) if processes is not None else None
                if process is None:
                    process = lookup_process(entry.pid).process
                try:
                    if not posix_runtime.root_alive(entry.posix_session):
                        process = None
                except OSError:
                    process = None
            return EntryStatus(entry, IdentityVerdict.LIVE, process, "Linux session authenticated")
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
        """Pids we own *right now* — the input every discovery call expects.

        Every live root, plus the descendants a usable process report proves
        are its tree (see :func:`proven_descendants`). Without a report only
        the roots can be answered: descent is proved from the table, never
        from one pid at a time.
        """
        pids: set[int] = set()
        for status in self.statuses(processes=processes).values():
            if not status.live:
                continue
            if status.process is not None:
                pids.add(status.entry.pid)
            if processes is not None and processes.usable and status.process is not None:
                pids |= proven_descendants(status.process, processes)
        return frozenset(pids)


def proven_descendants(root: ProcessInfo, processes: ProcessReport) -> frozenset[int]:
    """Every process in the report whose ancestry provably reaches ``root``.

    ``root`` must already be identity-verified (a ``LIVE`` entry's process).
    A process is proved one hop at a time, from itself up to the root, and a
    single doubtful hop rejects it:

    * the recorded parent pid must name a process in the same report -- a
      parent that exited breaks the chain, and nothing is guessed past it;
    * both sides of a hop must carry a creation time, and the parent's must
      not be later than the child's. A parent is alive when its child is
      created, so a pid that was reused since belongs to a process created
      *after* the child, which this rejects. That is the pid-reuse guard;
    * the chain is bounded (:data:`MAX_DESCENT_DEPTH`) and a pid seen twice
      ends it: a cycle is not a tree.

    A parent pid is what the kernel recorded at creation, so this proves that
    the root's tree created the process. It does not prove the process is
    still in the tree's job, and it is never used to stop anything.
    """
    if not processes.usable:
        return frozenset()
    by_pid = {process.pid: process for process in processes.processes}
    proven: set[int] = set()
    for candidate in processes.processes:
        if candidate.pid != root.pid and _descends_from(candidate, root, by_pid):
            proven.add(candidate.pid)
    return frozenset(proven)


def _descends_from(candidate: ProcessInfo, root: ProcessInfo, by_pid: dict) -> bool:
    current = candidate
    visited = {candidate.pid}
    for _ in range(MAX_DESCENT_DEPTH):
        parent = by_pid.get(current.ppid) if current.ppid else None
        if parent is None or parent.pid in visited:
            return False
        if parent.create_time is None or current.create_time is None:
            return False
        if parent.create_time > current.create_time:
            return False
        if parent.pid == root.pid:
            return True
        visited.add(parent.pid)
        current = parent
    return False


def build_entry(
    service_id: str,
    pid: int,
    executable: str | Path,
    argv: Sequence[str] = (),
    *,
    started_at: float | None = None,
    log_file: str | Path | None = None,
    job_name: str | None = None,
    posix_session: posix_runtime.SessionProof | None = None,
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
        job_name,
        posix_session,
    )


def with_create_time(entry: ManagedProcess, create_time: float | None) -> ManagedProcess:
    return replace(entry, create_time=create_time)
