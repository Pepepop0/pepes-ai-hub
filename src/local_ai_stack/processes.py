"""Runtime process specs, states and discovery.

Discovery only observes: it reports what is running right now, including
processes the user started outside this manager (ComfyUI Desktop, a manual
``ollama serve``, ``uvx open-webui serve`` in a terminal). Nothing here starts,
stops or restarts anything — that is a later step.

``RuntimeProcessState`` already declares the full lifecycle so the future
launcher does not have to change this contract. Discovery alone can only
produce ``RUNNING``, ``STOPPED`` and ``UNKNOWN``; ``STARTING``, ``STOPPING``
and ``FAILED`` are reserved for processes the manager itself supervises.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from .components import ComponentId
from .inventory import ListenerReport, ProcessInfo, ProcessReport, list_listeners, list_processes


class RuntimeProcessState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ProcessOrigin(str, Enum):
    """Who started the process we found."""

    MANAGED = "managed"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass(frozen=True)
class RuntimeProcessSpec:
    """How to recognise a service's process on this machine."""

    service_id: str
    name: str
    executables: tuple[str, ...] = ()
    cmdline_markers: tuple[str, ...] = ()
    port: int | None = None

    def matches(self, process: ProcessInfo) -> bool:
        candidates = {process.name.lower(), _basename((process.exe or "").lower())}
        if any(executable in candidates for executable in self.executables):
            return True
        cmdline = process.cmdline.lower()
        return any(marker in cmdline for marker in self.cmdline_markers)


@dataclass(frozen=True)
class RuntimeProcessStatus:
    spec: RuntimeProcessSpec
    state: RuntimeProcessState
    processes: tuple[ProcessInfo, ...] = ()
    origin: ProcessOrigin = ProcessOrigin.UNKNOWN
    detail: str = ""

    @property
    def service_id(self) -> str:
        return self.spec.service_id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def pids(self) -> tuple[int, ...]:
        return tuple(p.pid for p in self.processes)

    @property
    def pid(self) -> int | None:
        return self.processes[0].pid if self.processes else None

    @property
    def running(self) -> bool:
        return self.state is RuntimeProcessState.RUNNING


OLLAMA_PROCESS = RuntimeProcessSpec(
    service_id=ComponentId.OLLAMA,
    name="Ollama",
    executables=("ollama.exe", "ollama app.exe", "ollama"),
    cmdline_markers=("ollama serve",),
    port=11434,
)

# Open WebUI runs inside python.exe (uvx/pip install), so the command line is
# the only reliable marker.
OPEN_WEBUI_PROCESS = RuntimeProcessSpec(
    service_id=ComponentId.OPEN_WEBUI,
    name="Open WebUI",
    executables=("open-webui.exe", "open_webui.exe"),
    cmdline_markers=("open_webui", "open-webui"),
    port=8080,
)

COMFYUI_PROCESS = RuntimeProcessSpec(
    service_id=ComponentId.COMFYUI,
    name="ComfyUI",
    executables=("comfyui.exe", "comfyui desktop.exe"),
    cmdline_markers=("comfyui/main.py", "comfyui\\main.py", "comfy-desktop"),
    port=8188,
)

DEFAULT_PROCESS_SPECS: tuple[RuntimeProcessSpec, ...] = (
    OLLAMA_PROCESS,
    OPEN_WEBUI_PROCESS,
    COMFYUI_PROCESS,
)


def process_spec_by_id(
    service_id: str, specs: Sequence[RuntimeProcessSpec] = DEFAULT_PROCESS_SPECS
) -> RuntimeProcessSpec:
    for spec in specs:
        if spec.service_id == service_id:
            return spec
    known = ", ".join(s.service_id for s in specs)
    raise KeyError(f"unknown service id {service_id!r} (known: {known})")


def _origin(pids: Sequence[int], managed_pids: frozenset[int]) -> ProcessOrigin:
    if not pids:
        return ProcessOrigin.UNKNOWN
    if any(pid in managed_pids for pid in pids):
        return ProcessOrigin.MANAGED
    return ProcessOrigin.EXTERNAL


def discover_process(
    spec: RuntimeProcessSpec,
    *,
    processes: ProcessReport | None = None,
    listeners: ListenerReport | None = None,
    managed_pids: frozenset[int] = frozenset(),
) -> RuntimeProcessStatus:
    """Find the processes that belong to ``spec`` without assuming we started them."""
    process_report = list_processes() if processes is None else processes
    listener_report = list_listeners() if listeners is None else listeners

    matched: list[ProcessInfo] = []
    if process_report.usable:
        matched = [p for p in process_report.processes if spec.matches(p)]

    listener_pids: set[int] = set()
    if listener_report.usable and spec.port is not None:
        listener_pids = {
            listener.pid for listener in listener_report.on_port(spec.port) if listener.pid
        }

    # Whatever holds the service port is the service, even under a name we do
    # not recognise (custom launcher, renamed binary, wrapper script).
    known_pids = {p.pid for p in matched}
    for pid in sorted(listener_pids - known_pids):
        found = process_report.by_pid(pid) if process_report.usable else None
        matched.append(found or ProcessInfo(pid))

    found_processes = tuple(sorted(matched, key=lambda p: p.pid))
    origin = _origin([p.pid for p in found_processes], managed_pids)

    if found_processes:
        detail = f"{len(found_processes)} process(es) on this machine"
        if listener_pids:
            detail = f"{detail}, holding port {spec.port}"
        return RuntimeProcessStatus(
            spec, RuntimeProcessState.RUNNING, found_processes, origin, detail
        )
    if process_report.usable:
        return RuntimeProcessStatus(
            spec, RuntimeProcessState.STOPPED, detail="no matching process found"
        )
    if listener_report.usable and spec.port is not None:
        return RuntimeProcessStatus(
            spec,
            RuntimeProcessState.STOPPED,
            detail=f"nothing listening on port {spec.port}",
        )
    reason = process_report.detail or listener_report.detail or "process inspection unavailable"
    return RuntimeProcessStatus(spec, RuntimeProcessState.UNKNOWN, detail=reason)


def discover_processes(
    specs: Sequence[RuntimeProcessSpec] = DEFAULT_PROCESS_SPECS,
    *,
    processes: ProcessReport | None = None,
    listeners: ListenerReport | None = None,
    managed_pids: frozenset[int] = frozenset(),
) -> tuple[RuntimeProcessStatus, ...]:
    process_report = list_processes() if processes is None else processes
    listener_report = list_listeners() if listeners is None else listeners
    return tuple(
        discover_process(
            spec,
            processes=process_report,
            listeners=listener_report,
            managed_pids=managed_pids,
        )
        for spec in specs
    )
