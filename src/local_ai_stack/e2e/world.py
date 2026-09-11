"""The world a run executes against: controlled by default, real by opt-in.

One object answers every "where does this dependency come from?" question the
harness has, so the harness never reaches for a global. Swapping the world is
how a scenario becomes a different scenario, and how an opt-in real run differs
from a controlled one — the phases above are identical either way.

``controlled_world`` is offline, processless and deterministic. ``real_world``
exists so an operator can point the same harness at the real machine, and it
refuses to build unless the caller has explicitly granted the capability, so no
default test path can reach it by accident.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..download import Downloader
from ..installation import detect_repo_root
from ..integration import (
    IntegrationOptions,
    default_install_actions,
    default_provisioners,
)
from ..managed_config import ManagedConfigWriter
from ..model_catalog import ModelCatalog, model_catalog
from ..ollama_models import OllamaModelEntry
from ..profiles import Profile
from ..runner import CommandRunner
from ..system import SystemFacts
from ..test_contracts import TestProviderMode
from .fixtures import fixture_model_catalog
from .providers import (
    TRANSFER_CHUNK,
    ControlledArtifacts,
    ControlledComfyUI,
    ControlledConfigWriter,
    ControlledEndpoints,
    ControlledInventory,
    ControlledLauncher,
    ControlledModelInstaller,
    ControlledOllama,
    ControlledRunner,
    StepClock,
    controlled_component_archive,
    controlled_facts,
    refusing_client_factory,
)
from .roots import TestRoot, allocate_root
from .seams import InjectionPlan


class CapabilityRefused(RuntimeError):
    """A real-provider world was requested without the explicit opt-in."""


@dataclass
class ControlledWorld:
    """Every replaceable dependency, bound once for one run."""

    root: TestRoot
    facts: SystemFacts
    runner: CommandRunner
    ollama: object
    endpoints: ControlledEndpoints
    inventory: ControlledInventory
    comfyui: ControlledComfyUI
    artifacts: ControlledArtifacts
    options: IntegrationOptions
    # The CONFIG_WRITE seam. A real ``ManagedConfigWriter`` unless a scenario
    # supplies the controlled subclass; ``None`` means the domain default.
    config_writer: ManagedConfigWriter | None = None
    # The catalog this run resolves against. ``None`` means the shipped one;
    # a fixture catalog adds weights small enough to install offline.
    catalog: ModelCatalog | None = None
    # An installation this run did not create, when a scenario declared one.
    # Held so a verifier can compare it before and after from outside the run.
    external: object | None = None
    injections: InjectionPlan = field(default_factory=InjectionPlan.none)
    provider_mode: TestProviderMode = TestProviderMode.CONTROLLED
    repo_root: Path = field(default_factory=detect_repo_root)
    clock: StepClock | None = None
    launcher_pid: int = 44100
    # Which components a controlled run will actually install. Ollama's install
    # is a download, a checksum and an extraction, all of which a controlled
    # transport can drive honestly. ComfyUI and Open WebUI need git and pip, and
    # a fake for those would prove nothing about the real install, so a
    # controlled run leaves them to detection instead of pretending.
    install_components: frozenset[str] = frozenset({"ollama"})
    _launcher: ControlledLauncher | None = field(default=None, repr=False)
    _installers: dict[str, ControlledModelInstaller] = field(default_factory=dict, repr=False)
    _baseline_pids: tuple[int, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        # Whatever already existed when the world was built is external to this
        # run, and must survive it.
        if not self._baseline_pids:
            self._baseline_pids = self.inventory.pids()

    # --- relógio -----------------------------------------------------------

    @property
    def monotonic(self) -> Callable[[], float]:
        return self.clock.monotonic if self.clock is not None else time.monotonic

    @property
    def sleep(self) -> Callable[[float], None]:
        return self.clock.sleep if self.clock is not None else time.sleep

    # --- seams -------------------------------------------------------------

    def downloader(self) -> Downloader:
        """A downloader wired to the controlled transport.

        The chunk size is tiny so that a fixture weight arrives in several
        pieces. It is the only way an interruption can land *after* bytes have
        been written to the `.part` file rather than before the first read.
        """
        return Downloader(
            client_factory=self.artifacts.client_factory(self.injections),
            max_attempts=1,
            chunk_size=TRANSFER_CHUNK,
        )

    def provisioners(self, context) -> dict[str, object]:
        """The real provisioners, with their side-effecting seams controlled.

        Only two things are replaced: the download transport, and the artifact
        it fetches. Everything the provisioner then does — verify the SHA-256,
        extract, write the ownership marker, decide managed vs external — is the
        real implementation running unmodified, which is the only way this
        proves anything.

        A component whose install needs tooling a controlled run has no honest
        fake for (git, pip) is left to its own detection and reported as it is,
        rather than pretended into existence.
        """
        provisioners = default_provisioners(self.options)
        if self.provider_mode is TestProviderMode.LOCAL_REAL:
            return provisioners
        for component_id, provisioner in provisioners.items():
            if not hasattr(provisioner, "downloader"):
                continue
            provisioner.downloader = self.downloader()
            fixture = self._artifact_fixture(component_id, provisioner)
            if fixture is not None:
                provisioner.artifact = fixture
        return provisioners

    def _artifact_fixture(self, component_id: str, provisioner) -> object | None:
        """A controlled archive whose checksum is genuinely its own."""
        from ..artifacts import Artifact
        from ..components import ComponentId

        original = getattr(provisioner, "artifact", None)
        if original is None:
            return None
        executable = "ollama.exe" if component_id == ComponentId.OLLAMA else f"{component_id}.exe"
        payload = controlled_component_archive(executable)
        url = f"https://controlled.invalid/{component_id}/{original.filename}"
        digest = self.artifacts.register(url, payload)
        return Artifact(
            id=f"controlled-{component_id}",
            url=url,
            filename=original.filename,
            sha256=digest,
            size_bytes=len(payload),
            version=original.version,
            component=original.component,
        )

    def installable_plan(self, plan):
        """The provisioning plan narrowed to components this world can install.

        Narrowing is a fixture decision and is visible in the report; it is not
        a claim that the skipped components installed.
        """
        from ..planning import InstallPlan, PlanAction

        if self.provider_mode is TestProviderMode.LOCAL_REAL:
            return plan
        steps = tuple(
            step
            for step in plan.steps
            if step.action is not PlanAction.INSTALL or step.target in self.install_components
        )
        return InstallPlan(
            profile_id=plan.profile_id,
            steps=steps,
            stack_id=plan.stack_id,
            install_mode=plan.install_mode,
            install_root=plan.install_root,
        )

    def model_catalog(self) -> ModelCatalog:
        """The one catalog this run resolves against, wherever it is asked."""
        return self.catalog if self.catalog is not None else model_catalog()

    def model_installer(self, layout) -> ControlledModelInstaller:
        """The model-store seam, bound to one layout.

        Cached per layout so a scenario can read back what the seam did after
        the run, and so every phase talks to the same store.
        """
        from ..models import ManagedModelPaths

        key = str(layout.models)
        installer = self._installers.get(key)
        if installer is None:
            installer = ControlledModelInstaller(
                ManagedModelPaths.from_layout(layout),
                downloader=self.downloader(),
                ollama=self.ollama,
                injections=self.injections,
            )
            self._installers[key] = installer
        return installer

    def install_actions(self, context, profile: Profile):
        """Real action handlers, with the model installer bound to our seams."""

        def build_installer(_profile: Profile):
            return self.model_installer(context.layout)

        return default_install_actions(
            context,
            repo_root=self.repo_root,
            installer_factory=build_installer,
            catalog=self.model_catalog(),
            config_writer=self.config_writer,
            options=self.options,
        )

    def launcher(self, registry) -> ControlledLauncher:
        if self._launcher is None:
            self._launcher = ControlledLauncher(
                registry,
                self.inventory,
                plan=self.injections,
                pid=self.launcher_pid,
                ready_port=self._ollama_port(),
                # A crash is something the probes observe, so the launcher needs
                # to be able to arm it there rather than pretend at start time.
                endpoints=self.endpoints,
            )
        return self._launcher

    def chat(self, endpoint: str, reference: str, prompt: str):
        """The chat route for this world, or nothing to call."""
        route = getattr(self.ollama, "chat", None)
        if route is None:
            raise CapabilityRefused(
                "this world's Ollama client has no chat route; a smoke test may not "
                "claim a chat capability it cannot exercise"
            )
        return route(endpoint, reference, prompt)

    def _ollama_port(self) -> int | None:
        override = self.options.port_overrides.get("ollama")
        return int(override) if override is not None else None

    # --- provas de cleanup -------------------------------------------------

    def release_processes(self) -> list:
        """Stop every tree the launcher still owns, then release its handles.

        This is the path cleanup takes when there is no controller -- a run
        that failed while building its own context. Closing the handles alone
        would give up the proof of origin and leave the processes running, so
        each owned tree is stopped through the launcher first; the handles are
        released afterwards. Returns the stop results so failures are reported,
        not swallowed.
        """
        launcher = self._launcher
        if launcher is None:
            return []
        results = [launcher.stop(service_id) for service_id in list(launcher.members)]
        launcher.close_all()
        return results

    def managed_residual(self) -> tuple[int, ...]:
        """Pids this run spawned that are still alive -- descendants included.

        A stop that terminated only the root would leave its workers running,
        and that is the difference this has to be able to see.
        """
        if self._launcher is None:
            return ()
        current = set(self.inventory.pids())
        return tuple(sorted(self._launcher.spawned & current))

    def external_survivors(self) -> tuple[int, ...]:
        """Pids that existed before the run and must still exist after it."""
        current = set(self.inventory.pids())
        return tuple(sorted(set(self._baseline_pids) & current))

    def external_lost(self) -> tuple[int, ...]:
        """Pre-existing pids the run destroyed. Must always be empty."""
        current = set(self.inventory.pids())
        return tuple(sorted(set(self._baseline_pids) - current))


def controlled_world(
    *,
    root: TestRoot | None = None,
    options: IntegrationOptions | None = None,
    injections: InjectionPlan | None = None,
    cuda: bool = True,
    ollama_models: Sequence[OllamaModelEntry] = (),
    ollama_reachable: bool = True,
    ready_ports: Sequence[int] = (),
    repo_root: Path | None = None,
    fixture_models: bool = False,
    on_path: Mapping[str, str] | None = None,
    environment: Mapping[str, str] | None = None,
    external_processes: Sequence[tuple[int, str]] = (),
) -> ControlledWorld:
    """An offline, processless world. This is what tests use by default."""
    plan = injections or InjectionPlan.none()
    resolved_options = options or IntegrationOptions(
        port_overrides={"ollama": 21434, "open-webui": 28080, "comfyui": 28188},
        alongside_external=frozenset({"ollama", "comfyui"}),
    )
    inventory = ControlledInventory()
    # Registered before the world exists, so its baseline snapshot counts these
    # as pre-existing. A process added afterwards would look like ours.
    for port, executable in external_processes:
        inventory.add_external(port, executable=executable)
    endpoints = ControlledEndpoints(ready=set(ready_ports), plan=plan)
    clock = StepClock()
    artifacts = ControlledArtifacts()
    catalog = fixture_model_catalog(artifacts.register_model) if fixture_models else None
    pinned = _pinned_references(catalog)
    return ControlledWorld(
        root=root if root is not None else allocate_root(),
        facts=controlled_facts(cuda=cuda),
        runner=ControlledRunner(on_path=dict(on_path or {}), env=dict(environment or {})),
        ollama=ControlledOllama(
            installed=ollama_models, reachable=ollama_reachable, plan=plan, pinned=pinned
        ),
        endpoints=endpoints,
        inventory=inventory,
        comfyui=ControlledComfyUI(),
        artifacts=artifacts,
        options=resolved_options,
        config_writer=ControlledConfigWriter(plan),
        catalog=catalog,
        injections=plan,
        provider_mode=TestProviderMode.CONTROLLED,
        repo_root=repo_root if repo_root is not None else detect_repo_root(),
        clock=clock,
    )


def _pinned_references(catalog: ModelCatalog | None) -> dict[str, str]:
    """Reference to pinned digest, for every Ollama model in the catalog."""
    from ..models import OllamaModelSource

    resolved = catalog if catalog is not None else model_catalog()
    return {
        spec.source.reference: spec.source.digest
        for spec in resolved.specs.values()
        if isinstance(spec.source, OllamaModelSource) and spec.source.digest
    }


def real_world(
    *,
    root: TestRoot,
    allow_network: bool,
    options: IntegrationOptions | None = None,
    repo_root: Path | None = None,
) -> ControlledWorld:
    """A world backed by the real machine. Not implemented, and it says so.

    This used to hand back real system facts and a real Ollama client while
    still using the controlled inventory, endpoints and launcher — a world that
    called itself ``local-real`` and would have reported a controlled process
    scan as evidence about the real machine. That is the one thing a test
    harness must never do, so it refuses instead.

    The network capability is still checked first, because a caller without it
    is wrong for a second, independent reason.
    """
    if not allow_network:
        raise CapabilityRefused(
            "a real-provider world requires the explicit network capability; "
            "controlled providers are the default for every automatic test"
        )
    raise CapabilityRefused(
        "local-real providers are not implemented: process scanning, listener scanning "
        "and process launching have no real implementation bound here, and reporting "
        "controlled seams as local-real would make the run lie about what it proved"
    )


def offline_downloader() -> Downloader:
    """A downloader that turns any request into a violation."""
    return Downloader(client_factory=refusing_client_factory(), max_attempts=1)


__all__ = [
    "CapabilityRefused",
    "ControlledWorld",
    "controlled_world",
    "offline_downloader",
    "real_world",
]
