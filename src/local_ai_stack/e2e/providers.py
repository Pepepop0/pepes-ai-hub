"""Controlled providers: the fakes that let a full run happen offline.

A controlled run substitutes the side-effecting dependencies at the seams the
application already exposes — the command runner, the HTTP transport, the Ollama
client, the runtime probes and scans, the clock. Everything above those seams is
the real code: real planning, real provisioners, real resolver, real
``ServiceController``, real ownership rules.

That is the whole point. A fake that re-decided ownership or re-derived a path
would prove nothing, so none of these do. They answer questions; they never make
decisions that belong to a domain.

Default posture: no network, no GPU, no real process. Anything that would reach
outside the test root raises rather than silently succeeding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..inventory import (
    Listener,
    ListenerReport,
    ProcessInfo,
    ProcessReport,
    ScanOutcome,
)
from ..launcher import LaunchOutcome, LaunchResult, StopOutcome, StopResult
from ..ollama_models import OllamaClientError, OllamaModelEntry
from ..probes import ProbeOutcome, ProbeResult
from ..runner import CommandResult, CommandRunner
from ..system import GPUInfo, NvidiaFacts, SystemFacts
from .seams import Injection, InjectionPlan

CONTROLLED_PID = 44100
UNRELATED_PID = 9911
EXTERNAL_PID = 4242


class ControlledViolation(AssertionError):
    """A controlled run tried to reach something it must never reach."""


# --- clock -----------------------------------------------------------------


class StepClock:
    """A deterministic monotonic clock. No sleeping, no wall time.

    Readiness loops in the controller poll against a clock and a sleep; giving
    them this pair makes a timeout instantaneous and reproducible, which is why
    no scenario needs an arbitrary sleep.
    """

    def __init__(self, step: float = 0.25) -> None:
        self.step = step
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += max(seconds, self.step)

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- system facts ----------------------------------------------------------


def controlled_facts(*, cuda: bool = True, vram_mib: int = 12227) -> SystemFacts:
    """Plausible facts for the reference machine, or the same box without CUDA."""
    nvidia = (
        NvidiaFacts(
            smi_available=True,
            smi_path=r"C:\Windows\System32\nvidia-smi.exe",
            query_ok=True,
            driver_version="610.88",
            cuda_version_supported_by_driver="13.3",
            gpus=(GPUInfo(name="NVIDIA GeForce RTX 5070", vram_total_mib=vram_mib, index=0),),
            source="xml",
        )
        if cuda
        else NvidiaFacts(smi_available=False, error="nvidia-smi not found (controlled)")
    )
    return SystemFacts(
        os_name="Windows",
        os_release="10",
        python_version="3.11.9",
        git_available=True,
        nvidia_smi_available=nvidia.smi_available,
        gpus=nvidia.gpus,
        os_version="10.0.26200",
        architecture="64bit",
        machine="AMD64",
        windows_build=26200,
        nvidia=nvidia,
    )


# --- command runner --------------------------------------------------------


class ControlledRunner(CommandRunner):
    """Answers the probes provisioning makes without executing anything."""

    def __init__(
        self,
        *,
        on_path: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        responses: Mapping[str, CommandResult] | None = None,
    ) -> None:
        super().__init__()
        self.on_path = dict(on_path or {})
        self.fake_env = dict(env or {})
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return self.on_path.get(name)

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        merged = dict(self.fake_env)
        if extra:
            merged.update(extra)
        return merged

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(part) for part in argv)
        self.calls.append(items)
        joined = " ".join(items)
        for needle, result in self.responses.items():
            if needle in joined:
                return result
        if "--version" in items:
            # A managed binary answers when asked. This is the probe the
            # provisioner's own verify() makes; refusing it would fail the
            # install for a reason the scenario is not testing.
            return CommandResult(argv=items, returncode=0, stdout="version is 0.33.2\n")
        return CommandResult(argv=items, returncode=0, stdout="")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(call) for call in self.calls)


# --- HTTP transport --------------------------------------------------------


@dataclass
class ControlledArtifacts:
    """Bytes the controlled transport will serve, keyed by URL.

    A URL that was not registered is a violation rather than a 404: a
    controlled run must never depend on something nobody declared.
    """

    payloads: dict[str, bytes] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)

    def register(self, url: str, payload: bytes) -> str:
        self.payloads[url] = payload
        return hashlib.sha256(payload).hexdigest()

    def transport(self, plan: InjectionPlan | None = None) -> httpx.MockTransport:
        active = plan or InjectionPlan.none()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            if url not in self.payloads:
                raise ControlledViolation(f"controlled run requested an unknown URL: {url}")
            payload = self.payloads[url]
            if active.active(Injection.DOWNLOAD_INTERRUPTED):
                # A short write: the transport stops mid-body. The downloader
                # must not promote the partial file.
                raise httpx.ReadError("controlled transport closed mid-transfer")
            if active.active(Injection.CHECKSUM_MISMATCH):
                payload = payload + b"-tampered"
            return httpx.Response(200, content=payload)

        return httpx.MockTransport(handle)

    def client_factory(self, plan: InjectionPlan | None = None) -> Callable[[], httpx.Client]:
        transport = self.transport(plan)
        return lambda: httpx.Client(transport=transport)


def controlled_component_archive(executable: str) -> bytes:
    """A real zip containing one executable, for the extraction path.

    The provisioner under test downloads, verifies a SHA-256, extracts and
    marks. Giving it genuine zip bytes exercises every one of those steps; only
    the transport is controlled. Nothing here fakes verification.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(executable, "controlled component payload")
        bundle.writestr("lib/support.dll", "controlled support library")
    return buffer.getvalue()


def refusing_client_factory() -> Callable[[], httpx.Client]:
    """For phases that must not touch the network at all."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise ControlledViolation(f"network access attempted: {request.url}")

    transport = httpx.MockTransport(explode)
    return lambda: httpx.Client(transport=transport)


# --- Ollama client ---------------------------------------------------------


class ControlledOllama:
    """A model store that can be present, empty, or unreachable.

    It never removes anything, exactly like the real one, and records pulls so a
    scenario can prove a second run pulled nothing.
    """

    def __init__(
        self,
        *,
        installed: Sequence[OllamaModelEntry] = (),
        reachable: bool = True,
        plan: InjectionPlan | None = None,
    ) -> None:
        self.installed = list(installed)
        self.reachable = reachable
        self.plan = plan or InjectionPlan.none()
        self.pulls: list[str] = []
        self.list_calls = 0

    def list_models(self) -> tuple[OllamaModelEntry, ...]:
        self.list_calls += 1
        if not self.reachable:
            raise OllamaClientError("controlled Ollama store is unreachable")
        if self.plan.active(Injection.MODEL_MISSING):
            return ()
        return tuple(self.installed)

    def pull(self, reference: str) -> None:
        if not self.reachable:
            raise OllamaClientError(f"could not pull {reference}: store unreachable")
        if self.plan.active(Injection.MODEL_MISSING):
            raise OllamaClientError(f"could not pull {reference}: controlled store refuses")
        self.pulls.append(reference)
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        self.installed.append(OllamaModelEntry(name=reference, digest=digest, size_bytes=1))


# --- probes ----------------------------------------------------------------


@dataclass
class ControlledEndpoints:
    """What answers on which port, and what never becomes ready."""

    ready: set[int] = field(default_factory=set)
    plan: InjectionPlan = field(default_factory=InjectionPlan.none)
    payloads: dict[str, object] = field(default_factory=dict)
    http_calls: list[str] = field(default_factory=list)
    tcp_calls: list[tuple[str, int]] = field(default_factory=list)

    def _port_of(self, url: str) -> int:
        try:
            return int(url.split("//", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            return -1

    def http(self, url: str, timeout: float) -> ProbeResult:
        self.http_calls.append(url)
        if self.plan.active(Injection.READINESS_TIMEOUT):
            return ProbeResult(ProbeOutcome.TIMEOUT, detail="controlled readiness timeout")
        port = self._port_of(url)
        if port not in self.ready:
            return ProbeResult(ProbeOutcome.REFUSED, detail="controlled: nothing listening")
        import json as _json

        body = self.payloads.get(url, {"status": True, "version": "controlled"})
        return ProbeResult(
            ProbeOutcome.OK,
            latency_ms=1.0,
            status_code=200,
            body=_json.dumps(body),
        )

    def tcp(self, host: str, port: int, timeout: float) -> ProbeResult:
        self.tcp_calls.append((host, port))
        if self.plan.active(Injection.READINESS_TIMEOUT):
            return ProbeResult(ProbeOutcome.TIMEOUT, detail="controlled readiness timeout")
        if port in self.ready:
            return ProbeResult(ProbeOutcome.OK, latency_ms=1.0)
        return ProbeResult(ProbeOutcome.REFUSED, detail="controlled: nothing listening")


# --- inventory -------------------------------------------------------------


@dataclass
class ControlledInventory:
    """Listener and process scans the controller reads.

    Holds the unrelated and external processes a scenario needs to prove
    survive, plus whatever the controlled launcher has spawned.
    """

    listeners: list[Listener] = field(default_factory=list)
    processes: list[ProcessInfo] = field(default_factory=list)
    listener_outcome: ScanOutcome = ScanOutcome.OK
    process_outcome: ScanOutcome = ScanOutcome.OK

    def list_listeners(self) -> ListenerReport:
        return ListenerReport(self.listener_outcome, tuple(self.listeners))

    def list_processes(self) -> ProcessReport:
        return ProcessReport(self.process_outcome, tuple(self.processes))

    def lookup_process(self, pid: int) -> object:
        from ..inventory import LookupOutcome, ProcessLookup

        for process in self.processes:
            if process.pid == pid:
                return ProcessLookup(LookupOutcome.FOUND, process)
        return ProcessLookup(LookupOutcome.MISSING, None)

    # --- mutação controlada -------------------------------------------------

    def occupy(self, port: int, *, pid: int = UNRELATED_PID, host: str = "127.0.0.1") -> None:
        """An unrelated process owns a managed port."""
        self.listeners.append(Listener(host, port, pid))
        self.processes.append(
            ProcessInfo(pid, "unrelated.exe", r"C:\Other\unrelated.exe", "unrelated --serve", 1.0)
        )

    def add_external(self, port: int, *, pid: int = EXTERNAL_PID, name: str = "ollama.exe") -> None:
        self.listeners.append(Listener("127.0.0.1", port, pid))
        self.processes.append(
            ProcessInfo(
                pid,
                name,
                rf"C:\Users\Someone\AppData\Local\Programs\Ollama\{name}",
                f"{name} serve",
                1.0,
            )
        )

    def spawn(self, pid: int, executable: str, port: int | None = None) -> None:
        self.processes.append(
            ProcessInfo(pid, Path(executable).name, executable, f"{executable} serve", 2.0)
        )
        if port is not None:
            self.listeners.append(Listener("127.0.0.1", port, pid))

    def kill(self, pid: int) -> None:
        self.processes = [item for item in self.processes if item.pid != pid]
        self.listeners = [item for item in self.listeners if item.pid != pid]

    def pids(self) -> tuple[int, ...]:
        return tuple(sorted(item.pid for item in self.processes))


# --- launcher --------------------------------------------------------------


class ControlledLauncher:
    """Spawns nothing. Records what a real launcher would have been asked.

    Ownership still comes from the real registry: this only reports the pid it
    "started", and the registry decides whether that pid is ours.
    """

    def __init__(
        self,
        registry: object,
        inventory: ControlledInventory,
        *,
        plan: InjectionPlan | None = None,
        pid: int = CONTROLLED_PID,
        ready_port: int | None = None,
    ) -> None:
        self.registry = registry
        self.inventory = inventory
        self.plan = plan or InjectionPlan.none()
        self.pid = pid
        self.ready_port = ready_port
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.closed = False

    # The controller calls these three.
    def start(self, spec: object, **kwargs: object) -> LaunchResult:
        """Record a start and register it, exactly as the real launcher does.

        Registration matters: ownership is decided by the registry, not by this
        fake. An entry that did not go through the registry would later be
        classified external and refused, which is the registry doing its job.
        """
        from ..registry import ManagedProcess

        service_id = getattr(spec, "service_id", "unknown")
        executable = getattr(spec, "executable", "controlled.exe")
        self.starts.append(service_id)
        if self.plan.active(Injection.SERVICE_CRASH):
            # Started, then gone. Nothing is registered, because nothing lives.
            return LaunchResult(
                outcome=LaunchOutcome.STARTED,
                service_id=service_id,
                pid=self.pid,
                detail="controlled process started and exited",
            )
        self.inventory.spawn(self.pid, executable, self.ready_port)
        create_time = next(
            (item.create_time for item in self.inventory.processes if item.pid == self.pid), None
        )
        entry = ManagedProcess(
            service_id=service_id,
            pid=self.pid,
            executable=str(executable),
            argv=tuple(getattr(spec, "argv", ()) or (str(executable),)),
            started_at=1.0,
            create_time=create_time,
        )
        self.registry.register(entry)
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            service_id=service_id,
            pid=self.pid,
            detail="controlled start",
            entry=entry,
        )

    def stop(self, service_id: str, **kwargs: object) -> StopResult:
        self.stops.append(service_id)
        if self.plan.active(Injection.STOP_FAILURE):
            return StopResult(
                outcome=StopOutcome.FAILED,
                service_id=service_id,
                pid=self.pid,
                detail="controlled stop refused",
            )
        self.inventory.kill(self.pid)
        self.registry.unregister(service_id)
        return StopResult(
            outcome=StopOutcome.STOPPED,
            service_id=service_id,
            pid=self.pid,
            detail="controlled stop",
            tree_pids=(self.pid,),
        )

    def forget_job(self, service_id: str) -> None:
        return None

    def close_all(self) -> None:
        if self.plan.active(Injection.CLEANUP_FAILURE):
            raise ControlledViolation("controlled cleanup failure")
        self.closed = True

    def owns(self, service_id: str, pid: int) -> object:
        from ..launcher import Membership

        return Membership.UNKNOWN

    def tree_pids(self, service_id: str, pid: int) -> tuple[int, ...] | None:
        return None

    def surviving_members(self, proved: tuple[int, ...] | None, root_pid: int) -> tuple[int, ...]:
        return ()


__all__ = [
    "CONTROLLED_PID",
    "EXTERNAL_PID",
    "UNRELATED_PID",
    "ControlledArtifacts",
    "ControlledEndpoints",
    "ControlledInventory",
    "ControlledLauncher",
    "ControlledOllama",
    "ControlledRunner",
    "ControlledViolation",
    "StepClock",
    "controlled_facts",
    "refusing_client_factory",
]
