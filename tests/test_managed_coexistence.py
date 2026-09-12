"""Isolation contract for explicitly managed stacks beside external installs.

All services, sockets, downloads and processes in this module are fakes.  The
tests prove composition only; they never contact or mutate machine installs.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.integration import (
    ComponentIntegration,
    IntegrationOptions,
    IntegrationSnapshot,
    control_specs_for,
    default_install_actions,
    default_provisioners,
    plan_executor,
    preflight_managed_stack,
    provision_context,
    stage_managed_plan,
)
from local_ai_stack.inventory import Listener, ListenerReport, ScanOutcome
from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.model_installer import ModelInstaller
from local_ai_stack.model_resolver import StackModelResolver
from local_ai_stack.models import ManagedModelPaths, ModelId, ModelProvider, ModelState
from local_ai_stack.ollama_models import HttpOllamaClient
from local_ai_stack.planning import build_install_plan
from local_ai_stack.profiles import load_profile
from local_ai_stack.provisioning import (
    FailureReason,
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    ProvisionOutcome,
    RuntimeHandoff,
)
from local_ai_stack.runner import CommandResult, CommandRunner
from local_ai_stack.system import GPUInfo, SystemFacts

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "profiles" / "pepe.yaml")
MANAGED_PORTS = {"ollama": 12434, "open-webui": 18080, "comfyui": 18188}


class ExternalRunner(CommandRunner):
    """Expose fake Ollama and ComfyUI installs without running either one."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self.local_appdata = tmp_path / "external" / "Local"
        self.ollama = self.local_appdata / "Programs" / "Ollama" / "ollama.exe"
        self.comfyui = self.local_appdata / "Comfy-Desktop"
        self.ollama.parent.mkdir(parents=True)
        self.ollama.write_text("external ollama", encoding="utf-8")
        self.comfyui.mkdir(parents=True)
        (self.comfyui / "keep.txt").write_text("external comfy", encoding="utf-8")

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        if name == "ollama":
            return str(self.ollama)
        if name in {"git", "py", "python"}:
            return sys.executable
        return None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"LOCALAPPDATA": str(self.local_appdata)}

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(item) for item in argv)
        if "--version" in items:
            return CommandResult(items, 0, stdout="ollama version is 0.33.3")
        return CommandResult(items, 0)


class NoExternalRunner(CommandRunner):
    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return sys.executable if name in {"git", "py", "python"} else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {}


def context_for(tmp_path: Path, runner: CommandRunner | None = None) -> ProvisionContext:
    installation = resolve_installation(InstallLocation.custom(tmp_path / "managed"))
    return ProvisionContext(installation, runner or ExternalRunner(tmp_path))


def compatible_facts() -> SystemFacts:
    return SystemFacts(
        "Windows",
        "11",
        "3.11.9",
        True,
        True,
        (GPUInfo("NVIDIA GeForce RTX 5070", 12227, 10000),),
    )


def isolated_options() -> IntegrationOptions:
    return IntegrationOptions(
        port_overrides=MANAGED_PORTS,
        alongside_external=frozenset({"ollama", "comfyui"}),
    )


def managed_snapshot() -> IntegrationSnapshot:
    return IntegrationSnapshot(
        (
            ComponentIntegration(
                RuntimeHandoff(
                    "ollama",
                    LaunchAvailability.AVAILABLE,
                    Ownership.MANAGED,
                    executable=Path(sys.executable),
                )
            ),
        )
    )


def test_external_remains_skipped_without_explicit_alongside(tmp_path):
    context = context_for(tmp_path)
    provisioners = default_provisioners()

    assert provisioners["ollama"].apply(context, dry_run=True).outcome is ProvisionOutcome.SKIPPED
    assert provisioners["comfyui"].apply(context, dry_run=True).outcome is ProvisionOutcome.SKIPPED
    assert not context.layout.root.exists()


def test_explicit_alongside_allows_separate_managed_plans(tmp_path):
    context = context_for(tmp_path)
    provisioners = default_provisioners(isolated_options())

    assert provisioners["ollama"].plan(context).steps
    assert provisioners["comfyui"].plan(context).steps
    assert provisioners["ollama"].apply(context, dry_run=True).outcome is ProvisionOutcome.DRY_RUN
    assert provisioners["comfyui"].apply(context, dry_run=True).outcome is ProvisionOutcome.DRY_RUN
    assert not context.layout.root.exists()


def test_alongside_dry_run_never_mutates_external_installs(tmp_path):
    runner = ExternalRunner(tmp_path)
    context = context_for(tmp_path, runner)
    before_ollama = runner.ollama.read_bytes()
    before_comfy = (runner.comfyui / "keep.txt").read_bytes()

    executor = plan_executor(context, options=isolated_options())
    executor.execute(build_install_plan(PROFILE, installation=context.installation), dry_run=True)

    assert runner.ollama.read_bytes() == before_ollama
    assert (runner.comfyui / "keep.txt").read_bytes() == before_comfy
    assert not context.layout.root.exists()


def test_managed_plan_stages_model_work_after_runtime_readiness(tmp_path):
    context = context_for(tmp_path)
    plan = build_install_plan(PROFILE, installation=context.installation)
    phases = stage_managed_plan(plan)

    assert {step.action.value for step in phases.provisioning.steps} == {
        "check",
        "prepare-layout",
        "install",
    }
    assert [step.action.value for step in phases.after_ollama_ready.steps[:2]] == [
        "acquire-model",
        "acquire-model",
    ]
    assert {step.action.value for step in phases.after_ollama_ready.steps} == {
        "acquire-model",
        "apply-workflow",
        "configure",
    }


def test_preflight_accepts_free_explicit_endpoints_and_isolated_paths(tmp_path):
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        isolated_options(),
        facts=compatible_facts(),
        listener_report=ListenerReport(
            ScanOutcome.OK,
            (Listener("127.0.0.1", 11434, 4242),),
        ),
    )

    assert report.ok
    assert report.external_components == ("comfyui", "ollama")
    assert report.model_endpoint == "http://127.0.0.1:12434"
    assert report.model_store == context.layout.models / "ollama"
    assert report.model_store != context.layout.models / "comfyui"
    assert not context.layout.root.exists()


def test_occupied_managed_port_is_precondition_before_mutation(tmp_path):
    context = context_for(tmp_path)
    options = isolated_options()
    report = preflight_managed_stack(
        context,
        PROFILE,
        options,
        facts=compatible_facts(),
        listener_report=ListenerReport(
            ScanOutcome.OK,
            (Listener("127.0.0.1", MANAGED_PORTS["ollama"], 9001),),
        ),
    )

    assert not report.ok
    collision = next(issue for issue in report.issues if issue.code == "endpoint-collision")
    assert collision.reason is FailureReason.PRECONDITION
    assert "9001" in collision.detail
    assert not context.layout.root.exists()


def test_preflight_requires_explicit_alongside_for_external_components(tmp_path):
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        IntegrationOptions(port_overrides=MANAGED_PORTS),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.OK),
    )

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"alongside-required"}
    assert all(issue.reason is FailureReason.PRECONDITION for issue in report.issues)


def test_preflight_fails_closed_when_listener_inventory_is_unavailable(tmp_path):
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        isolated_options(),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.UNAVAILABLE, detail="access denied"),
    )

    assert not report.ok
    issue = next(issue for issue in report.issues if issue.code == "listener-scan")
    assert issue.reason is FailureReason.PRECONDITION
    assert not context.layout.root.exists()


def test_preflight_rejects_options_and_provisioner_alongside_divergence(tmp_path):
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        isolated_options(),
        provisioners=default_provisioners(),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.OK),
    )

    assert not report.ok
    assert {issue.code for issue in report.issues} == {"alongside-required"}


def test_runtime_uses_same_managed_endpoint_and_provider_store(tmp_path):
    context = context_for(tmp_path)
    endpoints = isolated_options().endpoints_for(PROFILE)
    ollama = {
        spec.service_id: spec
        for spec in control_specs_for(
            managed_snapshot(),
            layout=context.layout,
            endpoints=endpoints,
        )
    }["ollama"]

    assert ollama.launch is not None
    assert ollama.launch.env["OLLAMA_HOST"] == "127.0.0.1:12434"
    assert ollama.launch.env["OLLAMA_MODELS"] == str(context.layout.models / "ollama")
    assert ollama.service.port == 12434


def test_runtime_rejects_a_second_conflicting_endpoint_source(tmp_path):
    context = context_for(tmp_path)
    endpoints = isolated_options().endpoints_for(PROFILE)

    with pytest.raises(ValueError, match="endpoint authority"):
        control_specs_for(
            managed_snapshot(),
            layout=context.layout,
            endpoints=endpoints,
            service_options={"ollama": {"port": 11434}},
        )


def test_observation_specs_also_watch_the_explicit_managed_endpoints(tmp_path):
    context = context_for(tmp_path)
    endpoints = isolated_options().endpoints_for(PROFILE)
    specs = {
        spec.service_id: spec
        for spec in control_specs_for(
            IntegrationSnapshot(),
            layout=context.layout,
            endpoints=endpoints,
        )
    }

    assert specs["ollama"].service.port == 12434
    assert specs["open-webui"].service.port == 18080
    assert specs["comfyui"].service.port == 18188
    assert all(not spec.controllable for spec in specs.values())


def test_default_actions_bind_model_client_to_managed_endpoint(tmp_path):
    context = context_for(tmp_path)
    actions = default_install_actions(
        context,
        repo_root=REPO,
        profiles=(PROFILE,),
        options=isolated_options(),
    )

    installer = actions._installer(PROFILE)
    assert isinstance(installer.ollama, HttpOllamaClient)
    assert installer.ollama.base_url == "http://127.0.0.1:12434"


def test_managed_client_never_sees_a_model_present_only_on_external(tmp_path):
    external_payload = {"models": [{"name": "hermes3:8b", "digest": "sha256:4f6b83f30b62aaaa"}]}
    managed_payload: dict[str, list[dict[str, str]]] = {"models": []}

    def external_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=external_payload)

    def managed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=managed_payload)

    external_client = HttpOllamaClient(
        "http://127.0.0.1:11434",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(external_handler)),
    )
    managed_client = HttpOllamaClient(
        "http://127.0.0.1:12434",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(managed_handler)),
    )
    spec = model_catalog().require(ModelId("ollama/hermes3_8b"))
    paths = ManagedModelPaths.from_layout(context_for(tmp_path).layout)

    assert ModelInstaller(paths, ollama=external_client).detect(spec).available
    resolver = StackModelResolver(ModelInstaller(paths, ollama=managed_client), model_catalog())
    assert resolver.resolve(spec.id).state is ModelState.NOT_INSTALLED

    managed_payload["models"] = external_payload["models"]
    assert resolver.resolve(spec.id).state is ModelState.AVAILABLE


def test_no_external_install_keeps_normal_default_behavior(tmp_path):
    installation = resolve_installation(InstallLocation.custom(tmp_path / "managed"))
    context = provision_context(installation, runner=NoExternalRunner())
    provisioners = default_provisioners()

    assert provisioners["ollama"].plan(context).steps
    assert provisioners["comfyui"].plan(context).steps
    assert not context.layout.root.exists()


def test_provider_root_is_the_model_tree_authority(tmp_path):
    paths = ManagedModelPaths((tmp_path / "models").resolve())
    assert paths.provider_root(ModelProvider.OLLAMA) == paths.root / "ollama"
    assert paths.provider_root(ModelProvider.COMFYUI) == paths.root / "comfyui"


# --- component_id on preflight issues: the seam external_conflicts.py reads -


def test_preflight_tags_alongside_and_collision_issues_with_their_component(tmp_path):
    """This field is how the application distinguishes a per-service conflict
    from a machine-wide failure, without parsing detail text."""
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        IntegrationOptions(port_overrides=MANAGED_PORTS),
        facts=compatible_facts(),
        listener_report=ListenerReport(
            ScanOutcome.OK,
            (Listener("127.0.0.1", MANAGED_PORTS["ollama"], 9001),),
        ),
    )
    by_code = {issue.code: issue for issue in report.issues}
    assert by_code["alongside-required"].component_id in ("ollama", "comfyui")
    assert by_code["endpoint-collision"].component_id == "ollama"


def test_machine_wide_issues_carry_no_component_id(tmp_path):
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        isolated_options(),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.UNAVAILABLE, detail="access denied"),
    )
    issue = next(issue for issue in report.issues if issue.code == "listener-scan")
    assert issue.component_id is None


def test_path_overlap_is_tagged_with_the_overlapping_component(tmp_path):
    context = context_for(tmp_path)
    overlapping = ProvisionContext(
        installation=resolve_installation(
            InstallLocation.custom(context.runner.ollama.parent.parent)
        ),
        runner=context.runner,
    )
    report = preflight_managed_stack(
        overlapping,
        PROFILE,
        isolated_options(),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.OK),
    )
    overlap = next(issue for issue in report.issues if issue.code == "path-overlap")
    assert overlap.component_id == "ollama"


def test_a_mere_external_install_never_becomes_a_hard_stop_for_custom_root_isolation(tmp_path):
    """DEFAULT/PORTABLE/CUSTOM decide only the layout; an external elsewhere on
    disk, with nothing running to collide with, is never a path problem."""
    context = context_for(tmp_path)
    report = preflight_managed_stack(
        context,
        PROFILE,
        isolated_options(),
        facts=compatible_facts(),
        listener_report=ListenerReport(ScanOutcome.OK),
    )
    assert not any(issue.code == "path-overlap" for issue in report.issues)
    assert context.runner.ollama.is_file()  # the external install is untouched
