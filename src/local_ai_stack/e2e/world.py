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
from collections.abc import Callable, Sequence
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
from ..ollama_models import HttpOllamaClient, OllamaModelEntry
from ..profiles import Profile
from ..runner import CommandRunner
from ..system import SystemFacts, detect_system
from ..test_contracts import TestProviderMode
from .providers import (
    ControlledArtifacts,
    ControlledConfigWriter,
    ControlledEndpoints,
    ControlledInventory,
    ControlledLauncher,
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
    artifacts: ControlledArtifacts
    options: IntegrationOptions
    # The CONFIG_WRITE seam. A real ``ManagedConfigWriter`` unless a scenario
    # supplies the controlled subclass; ``None`` means the domain default.
    config_writer: ManagedConfigWriter | None = None
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
        """A downloader wired to the controlled transport."""
        return Downloader(
            client_factory=self.artifacts.client_factory(self.injections), max_attempts=1
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

    def install_actions(self, context, profile: Profile):
        """Real action handlers, with the model installer bound to our seams."""

        def build_installer(_profile: Profile):
            from ..model_installer import ModelInstaller
            from ..models import ManagedModelPaths

            return ModelInstaller(
                ManagedModelPaths.from_layout(context.layout),
                downloader=self.downloader(),
                ollama=self.ollama,
            )

        return default_install_actions(
            context,
            repo_root=self.repo_root,
            installer_factory=build_installer,
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
            )
        return self._launcher

    def _ollama_port(self) -> int | None:
        override = self.options.port_overrides.get("ollama")
        return int(override) if override is not None else None

    # --- provas de cleanup -------------------------------------------------

    def release_processes(self) -> None:
        if self._launcher is not None:
            self._launcher.close_all()

    def managed_residual(self) -> tuple[int, ...]:
        """Pids this run spawned that are still alive."""
        if self._launcher is None:
            return ()
        current = set(self.inventory.pids())
        return tuple(sorted({self.launcher_pid} & current))

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
) -> ControlledWorld:
    """An offline, processless world. This is what tests use by default."""
    plan = injections or InjectionPlan.none()
    resolved_options = options or IntegrationOptions(
        port_overrides={"ollama": 21434, "open-webui": 28080, "comfyui": 28188},
        alongside_external=frozenset({"ollama", "comfyui"}),
    )
    inventory = ControlledInventory()
    endpoints = ControlledEndpoints(ready=set(ready_ports), plan=plan)
    clock = StepClock()
    return ControlledWorld(
        root=root if root is not None else allocate_root(),
        facts=controlled_facts(cuda=cuda),
        runner=ControlledRunner(env={}),
        ollama=ControlledOllama(installed=ollama_models, reachable=ollama_reachable, plan=plan),
        endpoints=endpoints,
        inventory=inventory,
        artifacts=ControlledArtifacts(),
        options=resolved_options,
        config_writer=ControlledConfigWriter(plan),
        injections=plan,
        provider_mode=TestProviderMode.CONTROLLED,
        repo_root=repo_root if repo_root is not None else detect_repo_root(),
        clock=clock,
    )


def real_world(
    *,
    root: TestRoot,
    allow_network: bool,
    options: IntegrationOptions | None = None,
    repo_root: Path | None = None,
) -> ControlledWorld:
    """A world backed by the real machine. Opt-in, never a default.

    Refuses to exist without the network capability, because every real
    dependency it binds — the Ollama store, the download transport — needs it.
    Nothing in the automatic suite calls this.
    """
    if not allow_network:
        raise CapabilityRefused(
            "a real-provider world requires the explicit network capability; "
            "controlled providers are the default for every automatic test"
        )
    resolved_options = options or IntegrationOptions()
    inventory = ControlledInventory()
    return ControlledWorld(
        root=root,
        facts=detect_system(),
        runner=CommandRunner(),
        ollama=HttpOllamaClient(),
        endpoints=ControlledEndpoints(),
        inventory=inventory,
        artifacts=ControlledArtifacts(),
        options=resolved_options,
        injections=InjectionPlan.none(),
        provider_mode=TestProviderMode.LOCAL_REAL,
        repo_root=repo_root if repo_root is not None else detect_repo_root(),
        clock=None,
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
