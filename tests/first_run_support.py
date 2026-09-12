"""The first-run flow, end to end, against controlled providers.

Capella is loaded verbatim from the shipped profile. The image
model ids are Capella's own; the catalog just points them at small weights.

No network, no GPU, no real process. Every test cleans up after itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from local_ai_stack.download import Downloader
from local_ai_stack.e2e.fixtures import (
    SHIPPED_IMAGE_MODELS,
    fixture_model_catalog,
    repo_fixture,
)
from local_ai_stack.e2e.providers import (
    TRANSFER_CHUNK,
    ControlledArtifacts,
    ControlledConfigWriter,
    ControlledEndpoints,
    ControlledInventory,
    ControlledLauncher,
    ControlledOllama,
    ControlledToolchain,
    StepClock,
    controlled_component_archive,
    controlled_facts,
)
from local_ai_stack.e2e.roots import allocate_root
from local_ai_stack.e2e.seams import InjectionPlan
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.integration import IntegrationOptions
from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.models import ModelId
from local_ai_stack.profiles import load_profiles
from local_ai_stack.stack_application import (
    StackApplication,
    StackDependencies,
)

CHAT_SPEC = model_catalog().get(ModelId("ollama/qwen3_5_9b"))
CHAT_REFERENCE = CHAT_SPEC.source.reference
CHAT_DIGEST = CHAT_SPEC.source.digest
CAPELLA = "capella"
CAPELLA_PORTS = {"ollama": 11434, "open-webui": 8080, "comfyui": 8188}
RTX_5090_MIB = 32768
ALL_PORTS = tuple(CAPELLA_PORTS.values())


def capella_yaml() -> str:
    """Use the shipped profile verbatim; only providers and bytes are controlled."""
    return (detect_repo_root() / "profiles" / "capella.yaml").read_text(encoding="utf-8")


@dataclass
class ControlledStack:
    """A StackApplication bound to controlled providers, plus its evidence."""

    app: StackApplication
    root: object
    fixture: object
    artifacts: ControlledArtifacts
    ollama: ControlledOllama
    inventory: ControlledInventory
    endpoints: ControlledEndpoints
    clock: StepClock
    launchers: list[ControlledLauncher] = field(default_factory=list)
    runner: ControlledToolchain | None = None

    @property
    def launcher(self) -> ControlledLauncher | None:
        return self.launchers[-1] if self.launchers else None

    def release(self) -> None:
        for launcher in self.launchers:
            launcher.close_all()
        self.root.release()
        self.fixture.release()


def controlled_stack(
    *,
    profile_yaml: str | None = None,
    profile_id: str = CAPELLA,
    vram_mib: int = RTX_5090_MIB,
    ollama_models=(),
    injections: InjectionPlan | None = None,
    ready_ports=ALL_PORTS,
) -> ControlledStack:
    plan = injections or InjectionPlan.none()
    fixture = repo_fixture(profile_id, profile_yaml=profile_yaml or capella_yaml(), injections=plan)
    root = allocate_root()
    artifacts = ControlledArtifacts()
    catalog = fixture_model_catalog(artifacts.register_model, SHIPPED_IMAGE_MODELS)
    ollama = ControlledOllama(
        installed=list(ollama_models),
        plan=plan,
        pinned={CHAT_REFERENCE: CHAT_DIGEST},
    )
    inventory = ControlledInventory()
    endpoints = ControlledEndpoints(ready=set(ready_ports), plan=plan)
    clock = StepClock()
    launchers: list[ControlledLauncher] = []
    ports = {service: port for service, port in CAPELLA_PORTS.items()}

    def make_launcher(registry):
        launcher = ControlledLauncher(
            registry,
            inventory,
            plan=plan,
            ready_ports=ports,
            endpoints=endpoints,
        )
        launchers.append(launcher)
        return launcher

    # The commit each ComfyUI pin resolves to comes from the pin itself; the
    # toolchain never invents one.
    from local_ai_stack.integration import default_provisioners

    heads = {
        provisioner.url: provisioner.commit
        for provisioner in default_provisioners(IntegrationOptions()).values()
        if getattr(provisioner, "url", None) and getattr(provisioner, "commit", None)
    }
    runner = ControlledToolchain(heads=heads, env={})

    def provisioners(options):
        from local_ai_stack.artifacts import Artifact

        registry = default_provisioners(options)
        for component_id, provisioner in registry.items():
            if not hasattr(provisioner, "downloader"):
                continue
            provisioner.downloader = Downloader(
                client_factory=artifacts.client_factory(plan), max_attempts=1
            )
            original = getattr(provisioner, "artifact", None)
            if original is None:
                continue
            url = f"https://controlled.invalid/{component_id}/{original.filename}"
            payload = controlled_component_archive(
                "ollama.exe" if component_id == "ollama" else f"{component_id}.exe"
            )
            digest = artifacts.register(url, payload)
            provisioner.artifact = Artifact(
                id=f"controlled-{component_id}",
                url=url,
                filename=original.filename,
                sha256=digest,
                size_bytes=len(payload),
                version=original.version,
                component=original.component,
            )
        return registry

    deps = StackDependencies(
        runner=runner,
        detect_facts=lambda: controlled_facts(vram_mib=vram_mib),
        provisioners=provisioners,
        catalog=catalog,
        downloader=lambda: Downloader(
            client_factory=artifacts.client_factory(plan),
            max_attempts=1,
            chunk_size=TRANSFER_CHUNK,
        ),
        ollama_client=lambda _endpoint: ollama,
        config_writer=ControlledConfigWriter(plan),
        launcher=make_launcher,
        http=endpoints.http,
        tcp=endpoints.tcp,
        list_processes=inventory.list_processes,
        list_listeners=inventory.list_listeners,
        lookup_process=inventory.lookup_process,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    app = StackApplication(
        root.installation,
        profiles=load_profiles(fixture.path / "profiles"),
        repo_root=fixture.path,
        options=IntegrationOptions(),
        dependencies=deps,
    )
    built = ControlledStack(
        app, root, fixture, artifacts, ollama, inventory, endpoints, clock, launchers
    )
    built.runner = runner
    return built
