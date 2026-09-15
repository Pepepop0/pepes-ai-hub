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

* a normal job is destroyed as soon as the last *handle* to it closes, even
  while its members keep running. Members survive and its name becomes
  unopenable, so ordinary services retain the conservative session boundary;
* a service that explicitly permits proved-tree cleanup can give its root a
  duplicate handle and set ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The detached
  root then keeps the named job alive across Hub sessions. Root exit closes the
  last service-held handle and prevents a proved runner from becoming orphaned.

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
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_ALL_ACCESS = 0x1F001F
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
MAX_TRACKED_MEMBERS = 1024

if WINDOWS:  # pragma: no cover - exercised on Windows, skipped elsewhere
    import ctypes
    from ctypes import wintypes

    # Every signature is declared. ctypes defaults to int arguments and an int
    # return, which silently truncates a 64-bit HANDLE on win64 and would make
    # these calls fail in ways that look like "no permission" rather than like
    # a bug. LPVOID is the handle type; ULONG_PTR is pointer-sized.
    ULONG_PTR = ctypes.c_size_t

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenJobObjectW.restype = wintypes.HANDLE

    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,  # hJob
        ctypes.c_int,  # JOBOBJECTINFOCLASS
        wintypes.LPVOID,  # lpJobObjectInformation
        wintypes.DWORD,  # cbJobObjectInformationLength
        wintypes.LPDWORD,  # lpReturnLength
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL

    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    _kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,  # hSourceProcessHandle
        wintypes.HANDLE,  # hSourceHandle
        wintypes.HANDLE,  # hTargetProcessHandle
        ctypes.POINTER(wintypes.HANDLE),  # lpTargetHandle
        wintypes.DWORD,  # dwDesiredAccess
        wintypes.BOOL,  # bInheritHandle
        wintypes.DWORD,  # dwOptions
    ]
    _kernel32.DuplicateHandle.restype = wintypes.BOOL

    class _ProcessIdList(ctypes.Structure):
        """JOBOBJECT_BASIC_PROCESS_ID_LIST with room for a bounded membership.

        ``ProcessIdList`` is declared ULONG_PTR by Windows, so it is 64-bit
        wide on win64. A job with more members than fit returns what fits and
        reports the count in ``NumberOfProcessIdsInList``; the caller compares
        it against ``NumberOfAssignedProcesses`` to know the answer is partial.
        """

        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ULONG_PTR * MAX_TRACKED_MEMBERS),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
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

    Succeeds only while some handle to it is still open. This is normally the
    creating manager session; opted-in roots may retain their own handle so a
    restarted manager can recover the same kernel proof.
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


def retain_by_process(job: ProcessJob | None, process_handle: int) -> bool:
    """Keep an opted-in job alive for exactly as long as its root process.

    The duplicate is created inside the root process. This manager can close
    its own handle while a detached service keeps the named job re-openable.
    When the root exits, its handle closes and ``KILL_ON_JOB_CLOSE`` ensures
    that any runner still in the proved job cannot become an orphan.
    """
    if job is None or not jobs_supported() or not process_handle:
        return False
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = _kernel32.SetInformationJobObject(
        job.handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not configured:
        return False
    target_handle = wintypes.HANDLE()
    current = _kernel32.GetCurrentProcess()
    retained = _kernel32.DuplicateHandle(
        current,
        job.handle,
        int(process_handle),
        ctypes.byref(target_handle),
        0,
        False,
        DUPLICATE_SAME_ACCESS,
    )
    if retained and target_handle.value:
        return True

    # Manager exit must not kill a detached service unless the root owns the
    # handle that makes the job lifetime match the service lifetime.
    limits.BasicLimitInformation.LimitFlags = 0
    _kernel32.SetInformationJobObject(
        job.handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    return False


def member_pids(job: ProcessJob | None) -> tuple[int, ...] | None:
    """Exactly which processes the kernel says are in this job.

    ``None`` means the question could not be answered, which is never the same
    as an empty tuple: one is "cannot tell", the other is "nothing is in it".
    """
    if job is None or not jobs_supported():
        return None
    info = _ProcessIdList()
    returned = wintypes.DWORD(0)
    ok = _kernel32.QueryInformationJobObject(
        job.handle,
        JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    )
    if not ok:
        return None
    count = min(int(info.NumberOfProcessIdsInList), MAX_TRACKED_MEMBERS)
    return tuple(int(info.ProcessIdList[index]) for index in range(count))


def membership_is_partial(job: ProcessJob | None) -> bool:
    """True when the job holds more processes than one query can return."""
    if job is None or not jobs_supported():
        return False
    info = _ProcessIdList()
    returned = wintypes.DWORD(0)
    ok = _kernel32.QueryInformationJobObject(
        job.handle,
        JOB_OBJECT_BASIC_PROCESS_ID_LIST,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    )
    if not ok:
        return False
    return int(info.NumberOfAssignedProcesses) > int(info.NumberOfProcessIdsInList)


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
