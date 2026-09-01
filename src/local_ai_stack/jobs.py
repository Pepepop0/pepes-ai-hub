"""Proving that a process belongs to a tree this manager started.

The rule this module exists to enforce: *never kill a process merely because it
descends from a pid that was once ours.* Descent is not proof. On Windows a
parent pid is a stale number the moment the parent exits, and pids are reused,
so "walk the ancestry" can and does point at strangers. Anything built on
``children(recursive=True)`` inherits that flaw, and ``taskkill /T`` is the same
mistake in a shorter spelling.

What is actual proof
--------------------
A **job object** is a kernel container. A process assigned to one cannot leave
it, every process it creates joins it automatically, and the kernel will
enumerate the exact membership on request. So the question "is this process part
of the tree I started?" stops being an inference and becomes a lookup:
:func:`member_pids` returns the answer, and ``TerminateJobObject`` acts on
exactly that set and nothing else. A process that was never assigned — anything
the user started — can never appear in it.

Two properties, both verified on Windows 11 before this was written:

* a job is destroyed as soon as the last *handle* to it closes, even while its
  members keep running. Members survive; the name becomes unopenable. Ownership
  proof is therefore **scoped to one manager session**, and a manager that
  restarts can no longer prove the tree it once started. That boundary is real
  and is reported rather than papered over;
* ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` would make the proof outlive nothing
  and kill every service when the Hub exits, which is the opposite of what a
  detached service should do. It is deliberately not set.

Everywhere else — any non-Windows host, a kernel call that fails, a job we no
longer hold — this module answers "cannot tell", and the caller must fall back
to acting on the single process whose identity it can still verify.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from enum import Enum

WINDOWS = os.name == "nt"
JOB_NAME_PREFIX = "PepitosAIHub"
# JOBOBJECTINFOCLASS
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
JOB_OBJECT_ALL_ACCESS = 0x1F001F
MAX_TRACKED_MEMBERS = 1024

if WINDOWS:  # pragma: no cover - exercised on Windows, skipped elsewhere
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]

    class _ProcessIdList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_void_p * MAX_TRACKED_MEMBERS),
        ]
else:  # pragma: no cover - only the import shape matters off Windows
    ctypes = None  # type: ignore[assignment]
    _kernel32 = None


class Membership(str, Enum):
    """Whether a process can be *proved* to belong to a tree we started."""

    MEMBER = "member"
    NOT_MEMBER = "not_member"
    UNPROVABLE = "unprovable"


@dataclass(frozen=True)
class ProcessJob:
    """A live handle to one kernel job, valid for this manager session only."""

    name: str
    handle: int

    @property
    def supported(self) -> bool:
        return bool(self.handle)


def jobs_supported() -> bool:
    """Whether this host can prove tree membership at all."""
    return WINDOWS and _kernel32 is not None


def new_job_name(service_id: str) -> str:
    """A name unique to one start, so two starts never share a container."""
    return f"{JOB_NAME_PREFIX}-{service_id}-{uuid.uuid4().hex[:12]}"


def create_job(name: str) -> ProcessJob | None:
    """Create the container a managed process will be launched into."""
    if not jobs_supported():
        return None
    handle = _kernel32.CreateJobObjectW(None, name)
    if not handle:
        return None
    return ProcessJob(name, int(handle))


def open_job(name: str) -> ProcessJob | None:
    """Reopen a job by name.

    Succeeds only while some handle to it is still open, which in practice
    means within the session that created it. A restarted manager gets ``None``
    and must not pretend it owns the tree.
    """
    if not jobs_supported():
        return None
    handle = _kernel32.OpenJobObjectW(JOB_OBJECT_ALL_ACCESS, False, name)
    if not handle:
        return None
    return ProcessJob(name, int(handle))


DUPLICATE_SAME_ACCESS = 0x00000002


def duplicate_process_handle(handle: int) -> int | None:
    """Take our own copy of a process handle.

    ``subprocess`` closes its handle as soon as the Popen object is collected,
    which can happen before the process is adopted. Assigning through a closed
    handle would silently fail and leave the tree unprovable, so ownership of a
    handle is made explicit here.
    """
    if not jobs_supported() or not handle:
        return None
    duplicate = wintypes.HANDLE()
    current = _kernel32.GetCurrentProcess()
    ok = _kernel32.DuplicateHandle(
        current, handle, current, ctypes.byref(duplicate), 0, False, DUPLICATE_SAME_ACCESS
    )
    return int(duplicate.value) if ok and duplicate.value else None


def close_handle(handle: int | None) -> None:
    """Release a handle this module handed out."""
    if not jobs_supported() or not handle:
        return
    _kernel32.CloseHandle(handle)


def assign(job: ProcessJob | None, process_handle: int) -> bool:
    """Put a freshly created process, and everything it spawns, into the job."""
    if job is None or not jobs_supported() or not process_handle:
        return False
    return bool(_kernel32.AssignProcessToJobObject(job.handle, int(process_handle)))


def member_pids(job: ProcessJob | None) -> tuple[int, ...] | None:
    """Exactly which processes the kernel says are in this job.

    ``None`` means the question could not be answered, which is never the same
    as an empty tuple: one is "cannot tell", the other is "nothing is in it".
    """
    if job is None or not jobs_supported():
        return None
    info = _ProcessIdList()
    ok = _kernel32.QueryInformationJobObject(
        job.handle,
        JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    )
    if not ok:
        return None
    count = min(info.NumberOfProcessIdsInList, MAX_TRACKED_MEMBERS)
    return tuple(int(info.ProcessIdList[index]) for index in range(count))


def membership(job: ProcessJob | None, pid: int) -> Membership:
    """The whole point of this module, for one pid."""
    pids = member_pids(job)
    if pids is None:
        return Membership.UNPROVABLE
    return Membership.MEMBER if pid in pids else Membership.NOT_MEMBER


def terminate(job: ProcessJob | None, exit_code: int = 1) -> bool:
    """Kill every member of the job, and nothing else.

    The kernel decides the set; this manager never assembles a kill list from
    parent pids.
    """
    if job is None or not jobs_supported():
        return False
    return bool(_kernel32.TerminateJobObject(job.handle, exit_code))


def close(job: ProcessJob | None) -> None:
    """Release the handle. Members keep running; the name stops resolving."""
    if job is None or not jobs_supported() or not job.handle:
        return
    _kernel32.CloseHandle(job.handle)
