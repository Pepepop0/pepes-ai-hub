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
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..inventory import (
    Listener,
    ListenerReport,
    ProcessInfo,
    ProcessReport,
    ScanOutcome,
)
from ..jobs import Membership
from ..launcher import LaunchOutcome, LaunchResult, StopOutcome, StopResult
from ..managed_config import ManagedConfigWriter, ManagedFile, read_managed
from ..model_installer import ModelInstaller, ModelStatus
from ..models import FileModelSource
from ..ollama_models import STORE_UNREADABLE, OllamaClientError, OllamaModelEntry
from ..probes import ProbeOutcome, ProbeResult
from ..runner import CommandResult, CommandRunner
from ..system import GPUInfo, NvidiaFacts, SystemFacts
from .seams import Injection, InjectionPlan

# Small enough that a fixture weight of a few dozen bytes is delivered in
# several pieces, so an interruption really happens mid-transfer instead of
# before the first chunk is handed to the downloader.
TRANSFER_CHUNK = 8

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


class ControlledToolchain(ControlledRunner):
    """The command seam with git, venv and pip behind it.

    ComfyUI and Open WebUI are provisioned through commands: clone a checkout,
    create an environment, install pinned requirements, read versions back.
    This runner answers those commands and materialises exactly what each one
    would leave on disk -- a checkout with its entry point, an environment with
    its interpreter, a console script, a recorded distribution version -- so
    the real provisioners drive the whole install and their own ``verify``
    decides whether it holds. Nothing about provisioning is faked: the seam is
    the command runner, which the product already declares.

    What it will not do is invent a commit. ``heads`` maps a repository URL to
    the commit ``rev-parse HEAD`` reports, and a test supplies it from the
    provisioner's own pin.
    """

    def __init__(self, *, heads: Mapping[str, str] | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.heads = dict(heads or {})
        self.checkouts: dict[str, str] = {}
        self.environments: set[str] = set()
        self.installed: dict[str, dict[str, str]] = {}
        self.commands: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        found = super().which(name, extra_env)
        if found is not None:
            return found
        if name in ("git", "py", "python", "python3"):
            return str(Path("C:/Controlled") / f"{name}.exe")
        return None

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(part) for part in argv)
        self.commands.append(items)
        cwd = kwargs.get("cwd")
        answer = self._answer(items, Path(str(cwd)) if cwd else None)
        if answer is not None:
            return answer
        return super().run(argv, **kwargs)

    def _answer(self, items: tuple[str, ...], cwd: Path | None) -> CommandResult | None:
        def ok(stdout: str = "") -> CommandResult:
            return CommandResult(argv=items, returncode=0, stdout=stdout)

        def fail(stderr: str) -> CommandResult:
            return CommandResult(argv=items, returncode=1, stderr=stderr)

        if len(items) >= 3 and items[-2] == "-c" and "sys.version_info" in items[-1]:
            return ok("3.11" + "\n")
        if len(items) >= 3 and items[-2] == "-c" and "importlib.metadata" in items[-1]:
            env_root = str(Path(items[0]).parent.parent)
            distribution = items[-1].split("m.version(", 1)[1].split(")", 1)[0].strip("'\"")
            version = self.installed.get(env_root, {}).get(distribution)
            return ok(version + "\n" if version else "")
        if "-m" in items and "venv" in items:
            root = Path(items[items.index("venv") + 1])
            scripts = root / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_bytes(b"controlled interpreter")
            self.environments.add(str(root))
            return ok()
        if "-m" in items and "pip" in items and "install" in items:
            env_root = str(Path(items[0]).parent.parent)
            record = self.installed.setdefault(env_root, {})
            for requirement in items[items.index("install") + 1 :]:
                if requirement.startswith("-") or "==" not in requirement:
                    continue
                name, version = requirement.split("==", 1)
                record[name] = version
                script = Path(env_root) / "Scripts" / f"{name}.exe"
                script.write_bytes(b"controlled console script")
            return ok()
        if Path(items[0]).name in ("git", "git.exe"):
            return self._git(items[1:], cwd, ok, fail)
        return None

    def _git(self, args: tuple[str, ...], cwd: Path | None, ok, fail) -> CommandResult:
        if not args:
            return fail("no git command")
        if args[0] == "--version":
            return ok("git version 2.45.0" + "\n")
        if args[0] == "clone":
            url, destination = args[-2], Path(args[-1])
            head = self.heads.get(url)
            if head is None:
                return fail(f"controlled toolchain has no checkout for {url}")
            (destination / ".git").mkdir(parents=True, exist_ok=True)
            (destination / "main.py").write_text("# controlled checkout", encoding="utf-8")
            (destination / "requirements.txt").write_text("", encoding="utf-8")
            self.checkouts[str(destination)] = head
            return ok()
        if args[0] == "rev-parse" and "--is-inside-work-tree" in args:
            inside = cwd is not None and (cwd / ".git").is_dir()
            return ok("true") if inside else fail("not a git repository")
        if args[0] == "rev-parse" and "HEAD" in args:
            head = self.checkouts.get(str(cwd)) if cwd else None
            return ok(head + "\n") if head else fail("not a git repository")
        return ok()


# --- HTTP transport --------------------------------------------------------


@dataclass
class ControlledArtifacts:
    """Bytes the controlled transport will serve, keyed by URL.

    A URL that was not registered is a violation rather than a 404: a
    controlled run must never depend on something nobody declared.
    """

    payloads: dict[str, bytes] = field(default_factory=dict)
    # Which kind of thing each URL serves. A download interruption is a claim
    # about *model* acquisition, so interrupting the component binary instead
    # would prove a different row than the one the matrix names.
    kinds: dict[str, str] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    # What the injections actually did here, so a report can be checked against
    # the seam instead of against its own declaration.
    interrupted: list[str] = field(default_factory=list)
    corrupted: list[str] = field(default_factory=list)
    # Bytes that reached the client before an interruption, per URL. An
    # interruption that delivered nothing is a refused connection, not a
    # transfer that stopped half way, and the two fail in different places.
    delivered: dict[str, int] = field(default_factory=dict)

    def register(self, url: str, payload: bytes, *, kind: str = "component") -> str:
        self.payloads[url] = payload
        self.kinds[url] = kind
        return hashlib.sha256(payload).hexdigest()

    def register_model(self, url: str, payload: bytes) -> str:
        return self.register(url, payload, kind="model")

    def transport(self, plan: InjectionPlan | None = None) -> httpx.MockTransport:
        active = plan or InjectionPlan.none()

        def handle(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            if url not in self.payloads:
                raise ControlledViolation(f"controlled run requested an unknown URL: {url}")
            payload = self.payloads[url]
            if active.active(Injection.DOWNLOAD_INTERRUPTED) and self.kinds.get(url) == "model":
                # A real short transfer: the response starts, part of the body
                # arrives, and then the connection dies. The downloader is
                # already writing into its `.part` file by that point, which is
                # the state this row is about.
                self.interrupted.append(url)
                return httpx.Response(
                    200,
                    headers={"content-length": str(len(payload))},
                    content=self._interrupted_body(url, payload),
                )
            if active.active(Injection.CHECKSUM_MISMATCH):
                self.corrupted.append(url)
                payload = payload + b"-tampered"
            return httpx.Response(200, content=payload)

        return httpx.MockTransport(handle)

    def _interrupted_body(self, url: str, payload: bytes) -> Iterator[bytes]:
        """Deliver the first half in transfer-sized pieces, then die."""
        half = max(TRANSFER_CHUNK, len(payload) // 2)
        sent = 0
        for start in range(0, half, TRANSFER_CHUNK):
            chunk = payload[start : start + TRANSFER_CHUNK]
            if not chunk:
                break
            sent += len(chunk)
            self.delivered[url] = sent
            yield chunk
        raise httpx.ReadError("controlled transport closed mid-transfer")

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
        pinned: Mapping[str, str] | None = None,
    ) -> None:
        self.installed = list(installed)
        # What a registry would serve for each reference. A store that invented
        # its own digest would make every freshly pulled model look republished,
        # which is a real failure mode and not the one under test here.
        self.pinned = dict(pinned or {})
        self.reachable = reachable
        self.plan = plan or InjectionPlan.none()
        self.pulls: list[str] = []
        self.list_calls = 0
        self.chats: list[tuple[str, str, str]] = []

    def list_models(self) -> tuple[OllamaModelEntry, ...]:
        self.list_calls += 1
        if not self.reachable:
            raise OllamaClientError(f"{STORE_UNREADABLE}: controlled store is unreachable")
        if self.plan.active(Injection.MODEL_MISSING):
            return ()
        return tuple(self.installed)

    def pull(self, reference: str) -> None:
        if not self.reachable:
            raise OllamaClientError(f"could not pull {reference}: store unreachable")
        if self.plan.active(Injection.MODEL_MISSING):
            raise OllamaClientError(f"could not pull {reference}: controlled store refuses")
        self.pulls.append(reference)
        digest = self.pinned.get(reference) or hashlib.sha256(reference.encode("utf-8")).hexdigest()
        self.installed.append(OllamaModelEntry(name=reference, digest=digest, size_bytes=1))

    # --- rota de chat ------------------------------------------------------

    def chat(self, endpoint: str, reference: str, prompt: str) -> ChatReply:
        """A synthetic completion from the model the caller named.

        The reference is checked against the store rather than accepted. A chat
        route that answered for a model nobody installed would let a run report
        a capability it does not have, which is the failure this row exists to
        rule out.
        """
        if not self.reachable:
            raise OllamaClientError("controlled Ollama store is unreachable")
        known = [entry.name for entry in self.list_models()]
        if reference not in known:
            raise OllamaClientError(
                f"{reference} is not in the controlled store; it holds: "
                f"{', '.join(known) or 'nothing'}"
            )
        self.chats.append((endpoint, reference, prompt))
        return ChatReply(
            endpoint=endpoint,
            model=reference,
            prompt=prompt,
            content=f"controlled reply from {reference}",
        )


@dataclass(frozen=True)
class ChatReply:
    """One synthetic completion, and what was asked to produce it."""

    endpoint: str
    model: str
    prompt: str
    content: str
    done: bool = True

    @property
    def ok(self) -> bool:
        return self.done and bool(self.content)


# --- ComfyUI ---------------------------------------------------------------


@dataclass(frozen=True)
class ImageJob:
    """One accepted workflow submission, kept whole."""

    prompt_id: str
    endpoint: str
    graph: Mapping[str, Any]


@dataclass
class ControlledComfyUI:
    """A ComfyUI that accepts a workflow and answers about it.

    Deliberately not a health probe. It takes the rendered API graph, keeps it,
    and answers with a prompt id and a synthetic output. That is what makes it
    possible to assert on what was *submitted* -- the model binding, the
    parameters, the node addresses the manifest declared -- instead of only that
    something answered.

    It executes nothing: no GPU is involved and no image exists.
    """

    submissions: list[ImageJob] = field(default_factory=list)
    reachable: bool = True

    def submit(self, endpoint: str, graph: Mapping[str, Any]) -> ImageJob:
        if not self.reachable:
            raise ControlledViolation("controlled ComfyUI is not reachable")
        if not isinstance(graph, Mapping) or not graph:
            raise ControlledViolation("a workflow submission needs a non-empty graph")
        job = ImageJob(
            prompt_id=f"controlled-{len(self.submissions) + 1}",
            endpoint=endpoint,
            graph=dict(graph),
        )
        self.submissions.append(job)
        return job

    def result(self, prompt_id: str) -> dict[str, Any]:
        job = next((item for item in self.submissions if item.prompt_id == prompt_id), None)
        if job is None:
            raise ControlledViolation(f"unknown prompt id {prompt_id!r}")
        return {
            "prompt_id": prompt_id,
            "status": "success",
            "outputs": {"save": {"images": [{"filename": f"{prompt_id}.png", "type": "output"}]}},
        }

    def submitted(self, node_id: str, key: str) -> Any:
        """Read one field back out of the last submitted graph, by its address."""
        if not self.submissions:
            raise ControlledViolation("nothing was submitted")
        node = self.submissions[-1].graph.get(node_id)
        if not isinstance(node, Mapping):
            return None
        inputs = node.get("inputs")
        target = inputs if isinstance(inputs, Mapping) else node
        return target.get(key)


# --- probes ----------------------------------------------------------------


@dataclass
class ControlledEndpoints:
    """What answers on which port, and what never becomes ready."""

    ready: set[int] = field(default_factory=set)
    plan: InjectionPlan = field(default_factory=InjectionPlan.none)
    payloads: dict[str, object] = field(default_factory=dict)
    http_calls: list[str] = field(default_factory=list)
    tcp_calls: list[tuple[str, int]] = field(default_factory=list)
    # A service that dies *after* it was observed ready. Armed by the launcher,
    # counted here: the first observation answers, every later one finds nothing,
    # which is what a process exiting between two checks actually looks like.
    _crash_port: int | None = field(default=None, repr=False)
    _crash: Callable[[], None] | None = field(default=None, repr=False)
    _observations: int = field(default=0, repr=False)
    crashed: bool = False

    def arm_crash(self, port: int | None, kill: Callable[[], None]) -> None:
        self._crash_port = port
        self._crash = kill
        self._observations = 0

    def _observe(self, port: int) -> None:
        """Count observations of the armed port and let it die after the first."""
        if self._crash is None or port != self._crash_port:
            return
        self._observations += 1
        if self._observations < 2 or self.crashed:
            return
        self.crashed = True
        self.ready.discard(port)
        self._crash()

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
        self._observe(port)
        if port not in self.ready:
            return ProbeResult(ProbeOutcome.REFUSED, detail="controlled: nothing listening")
        import json as _json

        # One body that satisfies every shipped health spec's expected keys:
        # Ollama reads "version", Open WebUI "status", ComfyUI "system".
        body = self.payloads.get(
            url, {"status": True, "version": "controlled", "system": {"os": "controlled"}}
        )
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
        self._observe(port)
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
        return ProcessLookup(LookupOutcome.NOT_FOUND, None)

    # --- mutação controlada -------------------------------------------------

    def occupy(self, port: int, *, pid: int = UNRELATED_PID, host: str = "127.0.0.1") -> None:
        """An unrelated process owns a managed port."""
        self.listeners.append(Listener(host, port, pid))
        self.processes.append(
            ProcessInfo(pid, "unrelated.exe", r"C:\Other\unrelated.exe", "unrelated --serve", 1.0)
        )

    def add_external(
        self,
        port: int,
        *,
        pid: int = EXTERNAL_PID,
        name: str = "ollama.exe",
        executable: str | None = None,
    ) -> None:
        """A process this run did not start, at the path it was really found.

        ``executable`` matters: the controller decides origin by matching a
        running process against what it owns, so a scenario that wants a
        genuinely external process has to point at the external install rather
        than at an invented path.
        """
        path = executable or rf"C:\Users\Someone\AppData\Local\Programs\Ollama\{name}"
        self.listeners.append(Listener("127.0.0.1", port, pid))
        self.processes.append(ProcessInfo(pid, Path(path).name, path, f"{path} serve", 1.0))

    def spawn(
        self,
        pid: int,
        executable: str,
        port: int | None = None,
        *,
        ppid: int | None = None,
        create_time: float = 2.0,
    ) -> None:
        """A process appears, with the parent pid and creation time the kernel
        would record for it. Both are what ancestry proof reads."""
        self.processes.append(
            ProcessInfo(
                pid, Path(executable).name, executable, f"{executable} serve", create_time, ppid
            )
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

    A start produces a *tree*, not a pid. The descendants are entries in the
    controlled process inventory like any other process, and membership is kept
    the way the real launcher keeps it -- a set per service, answered through
    ``owns``/``tree_pids``. That matters because a stop that terminated only the
    root would leave those entries alive, and the world's residual check would
    find them, which is exactly the failure this models.

    Each descendant is spawned as the kernel would record it: its parent is
    the member above it and it was created after it. ``listener_depth`` says
    which member opens the service port -- ``0`` is the root, as every early
    scenario assumed; a real Open WebUI or ComfyUI serves from a child or
    grandchild of the process the registry knows, and ownership must be proved
    across that gap, not assumed away by giving the root the port.
    """

    def __init__(
        self,
        registry: object,
        inventory: ControlledInventory,
        *,
        plan: InjectionPlan | None = None,
        pid: int = CONTROLLED_PID,
        ready_port: int | None = None,
        endpoints: ControlledEndpoints | None = None,
        descendants: int = 1,
        ready_ports: Mapping[str, int] | None = None,
        listener_depth: int = 0,
    ) -> None:
        if not 0 <= listener_depth <= descendants:
            raise ValueError("listener_depth must name the root (0) or one of the descendants")
        self.registry = registry
        self.inventory = inventory
        self.plan = plan or InjectionPlan.none()
        self.pid = pid
        self.ready_port = ready_port
        # Per-service ready ports, for a run that starts more than one service.
        # The single ``ready_port`` stays for the first service started, which
        # is how every existing scenario is written.
        self.ready_ports = dict(ready_ports or {})
        self._pids: dict[str, int] = {}
        self.endpoints = endpoints
        self.descendants = descendants
        self.listener_depth = listener_depth
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.closed = False
        # service id -> the pids this launcher can prove it started, root first.
        self.members: dict[str, tuple[int, ...]] = {}
        # The same, kept after the job is forgotten, because a caller checking
        # cleanup still needs to know what was started.
        self.started_trees: dict[str, tuple[int, ...]] = {}
        # Everything ever spawned, so residual checks cover descendants too.
        self.spawned: set[int] = set()

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
        root_pid = self._pid_for(service_id)
        port = self.ready_ports.get(service_id, self.ready_port if root_pid == self.pid else None)
        born = 2.0 + len(self.starts)
        self.inventory.spawn(
            root_pid, executable, port if self.listener_depth == 0 else None, create_time=born
        )
        tree = [root_pid]
        for offset in range(1, self.descendants + 1):
            child = root_pid + offset
            self.inventory.spawn(
                child,
                f"{executable}-worker-{offset}",
                port if offset == self.listener_depth else None,
                ppid=tree[-1],
                create_time=born + offset / 1000,
            )
            tree.append(child)
        self.members[service_id] = tuple(tree)
        self.started_trees[service_id] = tuple(tree)
        self.spawned.update(tree)
        create_time = next(
            (item.create_time for item in self.inventory.processes if item.pid == root_pid), None
        )
        entry = ManagedProcess(
            service_id=service_id,
            pid=root_pid,
            executable=str(executable),
            argv=tuple(getattr(spec, "argv", ()) or (str(executable),)),
            started_at=1.0,
            create_time=create_time,
        )
        self.registry.register(entry)
        if self.plan.active(Injection.SERVICE_CRASH) and self.endpoints is not None:
            # It really starts and really becomes ready. It dies on the next
            # observation, which is the case a start report alone cannot catch.
            self.endpoints.arm_crash(port, lambda: self.inventory.kill(root_pid))
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            service_id=service_id,
            pid=root_pid,
            detail="controlled start",
            entry=entry,
        )

    def stop(self, service_id: str, **kwargs: object) -> StopResult:
        self.stops.append(service_id)
        # A transient refusal lets the cleanup scenario prove recovery without
        # accepting residual processes. Unbounded refusal remains available for
        # tests of cleanup failure reporting itself.
        first_only = self.plan.detail.get("stop_failure_once", False)
        if self.plan.active(Injection.STOP_FAILURE) and (
            not first_only or self.stops.count(service_id) == 1
        ):
            return StopResult(
                outcome=StopOutcome.FAILED,
                service_id=service_id,
                pid=self.pid,
                detail="controlled stop refused",
            )
        root_pid = self._pids.get(service_id, self.pid)
        tree = self.members.get(service_id, (root_pid,))
        for member in tree:
            self.inventory.kill(member)
        self.registry.unregister(service_id)
        self.forget_job(service_id)
        return StopResult(
            outcome=StopOutcome.STOPPED,
            service_id=service_id,
            pid=root_pid,
            detail=f"controlled stop of {len(tree)} process(es)",
            tree_pids=tree,
        )

    def _pid_for(self, service_id: str) -> int:
        """A stable root pid per service; the first service keeps ``pid``."""
        if service_id not in self._pids:
            # Leave room for each tree's descendants between roots.
            self._pids[service_id] = self.pid + 100 * len(self._pids)
        return self._pids[service_id]

    def forget_job(self, service_id: str) -> None:
        """Give up the proof. Members keep running, as in the real launcher."""
        self.members.pop(service_id, None)

    def close_all(self) -> None:
        """Release every job handle, as the real launcher does.

        Members keep running -- a job is a proof of origin, not a leash -- so
        whoever calls this must have stopped the trees first. Membership is
        given up here, not merely flagged.
        """
        if self.plan.active(Injection.CLEANUP_FAILURE):
            raise ControlledViolation("controlled cleanup failure")
        for service_id in list(self.members):
            self.forget_job(service_id)
        self.closed = True

    def members_started(self, service_id: str) -> tuple[int, ...]:
        """What this launcher started for a service, job handle or not."""
        return self.started_trees.get(service_id, ())

    def proved(self, service_id: str, pid: int) -> Membership:
        """Membership as it stood at start, for checking cleanup afterwards."""
        tree = self.started_trees.get(service_id)
        if tree is None:
            return Membership.UNPROVABLE
        return Membership.MEMBER if pid in tree else Membership.NOT_MEMBER

    def owns(self, service_id: str, pid: int) -> Membership:
        tree = self.members.get(service_id)
        if tree is None:
            return Membership.UNPROVABLE
        return Membership.MEMBER if pid in tree else Membership.NOT_MEMBER

    def tree_pids(self, service_id: str, pid: int) -> tuple[int, ...] | None:
        tree = self.members.get(service_id)
        if tree is None or pid not in tree:
            return None
        return tree

    def surviving_members(self, proved: tuple[int, ...] | None, root_pid: int) -> tuple[int, ...]:
        alive = set(self.inventory.pids())
        return tuple(pid for pid in (proved or ()) if pid != root_pid and pid in alive)


# --- managed configuration -------------------------------------------------

TAMPER_LINE = "PEPITO_E2E_TAMPERED=1"
TAMPER_KEY = "pepito-e2e-tampered"


class ControlledConfigWriter(ManagedConfigWriter):
    """The CONFIG_WRITE seam. Writes for real; corrupts only when injected.

    Generation, ownership, the marker, the atomic write and the verification
    are all the inherited implementation — this class adds one thing, and only
    under ``CONFIG_TAMPERED``: after a real apply it edits the *body* of a file
    that was just written and leaves the header exactly as it is.

    That is deliberately the hardest case. The file still names this manager and
    still claims the digest we generated, so ownership alone cannot tell that
    anything happened; only recomputing the digest from the bytes on disk can.
    Nothing here computes a digest, and nothing here decides what the answer
    should be: it changes bytes, and the domain says what that means.
    """

    def __init__(self, injections: InjectionPlan | None = None) -> None:
        super().__init__()
        # Not ``plan``: the base class already uses that name for the method
        # that decides what an apply would do, and shadowing it would replace a
        # domain decision with a fixture, which is exactly what must not happen.
        self.injections = injections or InjectionPlan.none()
        self.tampered: list[Path] = []
        # What the file looked like the instant after it was edited, read
        # back through the domain. Kept here because an ephemeral root is
        # gone by the time a scenario checks its invariants.
        self.after_tampering: dict[Path, ManagedFile] = {}

    def apply(self, documents, *, dry_run: bool = False):
        rendered = tuple(documents)
        report = super().apply(rendered, dry_run=dry_run)
        if dry_run or not self.injections.active(Injection.CONFIG_TAMPERED):
            return report
        for path in report.written:
            self._tamper(path)
        return report

    def _tamper(self, path: Path) -> None:
        """Edit the content, keep the header. A marker is a claim, not evidence."""
        text = path.read_text(encoding="utf-8")
        if text.lstrip().startswith("{"):
            payload = json.loads(text)
            payload[TAMPER_KEY] = True
            edited = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        else:
            edited = text if text.endswith("\n") else text + "\n"
            edited += f"{TAMPER_LINE}\n"
        path.write_text(edited, encoding="utf-8", newline="\n")
        self.tampered.append(path)
        self.after_tampering[path] = read_managed(path)


class ControlledModelInstaller(ModelInstaller):
    """The model-store seam. Installs for real; corrupts only when injected.

    Downloading, checksum verification, placement through ``ManagedModelPaths``
    and detection are all inherited. Under ``MODEL_TAMPERED`` one extra thing
    happens: after a file model has been installed *and verified*, its bytes are
    changed on disk.

    That ordering is the whole point of the row. The file was genuinely valid a
    moment earlier, so nothing about the install path can be blamed; what has to
    catch it is the installer re-hashing the file it already accepted.
    """

    def __init__(self, *args, injections: InjectionPlan | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.injections = injections or InjectionPlan.none()
        self.tampered: list[Path] = []
        # What the domain said about each file the instant after it was edited.
        # Kept here because an ephemeral root is gone by the time a scenario
        # checks its invariants.
        self.after_tampering: dict[str, ModelStatus] = {}

    def apply(self, spec, *, dry_run: bool = False, progress=None):
        result = super().apply(spec, dry_run=dry_run, progress=progress)
        if dry_run or not result.ok or not self.injections.active(Injection.MODEL_TAMPERED):
            return result
        if not isinstance(spec.source, FileModelSource):
            return result
        self._tamper(spec)
        return result

    def _tamper(self, spec) -> None:
        """Change the bytes. What that means is the installer's to say."""
        destination = self.paths.destination_for(spec)
        if not destination.is_file():  # pragma: no cover - apply just wrote it
            return
        with destination.open("ab") as handle:
            handle.write(b"corrupted-by-e2e")
        self.tampered.append(destination)
        self.after_tampering[str(spec.id)] = self.detect(spec)


__all__ = [
    "CONTROLLED_PID",
    "EXTERNAL_PID",
    "TAMPER_KEY",
    "TAMPER_LINE",
    "TRANSFER_CHUNK",
    "UNRELATED_PID",
    "ChatReply",
    "ControlledArtifacts",
    "ControlledComfyUI",
    "ControlledConfigWriter",
    "ControlledEndpoints",
    "ControlledInventory",
    "ControlledLauncher",
    "ControlledModelInstaller",
    "ControlledOllama",
    "ControlledRunner",
    "ControlledToolchain",
    "ControlledViolation",
    "ImageJob",
    "StepClock",
    "controlled_facts",
    "refusing_client_factory",
]
