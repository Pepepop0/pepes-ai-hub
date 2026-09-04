"""Controlled integration tests for the first IC5 + IC6 wiring round."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from local_ai_stack.artifacts import Artifact
from local_ai_stack.execution import PlanExecutor
from local_ai_stack.install_actions import InstallActionHandlers
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.integration import ComponentIntegration, IntegrationSnapshot, control_specs_for
from local_ai_stack.managed_config import ManagedConfigWriter
from local_ai_stack.model_catalog import ModelCatalog
from local_ai_stack.model_installer import ModelInstaller, ModelOutcome
from local_ai_stack.model_resolver import StackModelResolver
from local_ai_stack.models import (
    FileModelSource,
    ManagedModelPaths,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelSpec,
    OllamaModelSource,
)
from local_ai_stack.ollama_models import OllamaModelEntry
from local_ai_stack.planning import InstallPlan, PlanAction, PlanStep
from local_ai_stack.profiles import Profile, load_profile
from local_ai_stack.provisioning import (
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    RuntimeHandoff,
)
from local_ai_stack.service_config import (
    ENV_DEFAULT_MODELS,
    OPEN_WEBUI_ENV_FILE,
    StackConfiguration,
    build_stack_configuration,
)
from local_ai_stack.workflows import WorkflowState, load_workflow_manifest, resolve_workflow

REPO = Path(__file__).resolve().parents[1]
FIXTURE_GRAPH = REPO / "tests" / "fixtures" / "flux2-klein-t2i-api.json"

OLLAMA_ID = ModelId("ollama/hermes3_8b")
DIFFUSION_ID = ModelId("comfyui/flux2_klein_fp8")
ENCODER_ID = ModelId("comfyui/qwen_3_4b_encoder")
VAE_ID = ModelId("comfyui/flux2_vae")

FILE_MODELS = (
    (DIFFUSION_ID, ModelKind.DIFFUSION, ModelPlacement.DIFFUSION_MODELS, "flux.safetensors"),
    (ENCODER_ID, ModelKind.TEXT_ENCODER, ModelPlacement.TEXT_ENCODERS, "encoder.safetensors"),
    (VAE_ID, ModelKind.VAE, ModelPlacement.VAE, "vae.safetensors"),
)


class FakeOllama:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.pulled: list[str] = []

    def list_models(self):
        if not self.available:
            return ()
        return (OllamaModelEntry("hermes3:8b", "sha256:4f6b83f30b62abcd"),)

    def pull(self, reference: str) -> None:
        self.pulled.append(reference)
        self.available = True


def profile() -> Profile:
    return load_profile(REPO / "profiles" / "pepe.yaml")


def model_catalog() -> tuple[ModelCatalog, dict[ModelId, bytes]]:
    specs: dict[ModelId, ModelSpec] = {
        OLLAMA_ID: ModelSpec(
            OLLAMA_ID,
            ModelKind.CHAT,
            OllamaModelSource("hermes3:8b", "4f6b83f30b62"),
        )
    }
    contents: dict[ModelId, bytes] = {}
    for model_id, kind, placement, filename in FILE_MODELS:
        content = f"controlled-{model_id}".encode()
        contents[model_id] = content
        artifact = Artifact(
            id=model_id.name,
            url=f"https://example.invalid/{filename}",
            filename=filename,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        specs[model_id] = ModelSpec(model_id, kind, FileModelSource(artifact, placement))
    return ModelCatalog(specs=specs), contents


def prepared_domains(tmp_path: Path, *, ollama_available: bool = True):
    stack = tmp_path / "stack"
    installation = resolve_installation(InstallLocation.custom(stack))
    catalog, contents = model_catalog()
    ollama = FakeOllama(available=ollama_available)
    installer = ModelInstaller(ManagedModelPaths.from_layout(installation.layout), ollama=ollama)
    for model_id, content in contents.items():
        target = installer.paths.destination_for(catalog.require(model_id))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    repo_root = tmp_path / "repo"
    graph_path = repo_root / "workflows" / "flux2-klein-t2i-api.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(FIXTURE_GRAPH.read_text(encoding="utf-8"), encoding="utf-8")
    specs = load_workflow_manifest(REPO / "manifests" / "workflows.yaml")
    actions = InstallActionHandlers(
        profiles={"pepe": profile()},
        catalog=catalog,
        installer_factory=lambda selected: installer,
        workflow_specs=specs,
        repo_root=repo_root,
        layout=installation.layout,
    )
    return installation, catalog, installer, ollama, specs, repo_root, actions


def configuration_for(tmp_path: Path, *, ollama_available: bool = True):
    installation, catalog, installer, ollama, specs, repo_root, actions = prepared_domains(
        tmp_path, ollama_available=ollama_available
    )
    resolver = StackModelResolver(installer, catalog)
    graph = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
    configuration = build_stack_configuration(
        profile(),
        resolver,
        installation.layout,
        workflow_specs=specs,
        repo_root=repo_root,
        graphs={"flux2_klein_t2i": graph},
    )
    return installation, resolver, configuration, actions, ollama


def managed_snapshot() -> IntegrationSnapshot:
    return IntegrationSnapshot(
        tuple(
            ComponentIntegration(
                RuntimeHandoff(
                    service_id,
                    LaunchAvailability.AVAILABLE,
                    Ownership.MANAGED,
                    executable=Path(sys.executable),
                )
            )
            for service_id in ("ollama", "open-webui", "comfyui")
        )
    )


def action_plan(action: PlanAction, target: str) -> InstallPlan:
    return InstallPlan("pepe", (PlanStep(1, action, target, "controlled integration"),))


def test_real_ic5_resolver_drives_ic6_and_the_verified_runtime_specs(tmp_path):
    installation, resolver, configuration, _, _ = configuration_for(tmp_path)
    assert isinstance(resolver, StackModelResolver)
    assert configuration.ok

    writer = ManagedConfigWriter()
    assert writer.apply(configuration.documents).ok
    assert writer.verify(configuration.documents).ok
    specs = {
        spec.service_id: spec
        for spec in control_specs_for(
            managed_snapshot(),
            layout=installation.layout,
            configuration=configuration,
            config_writer=writer,
        )
    }

    assert specs["open-webui"].launch.env[ENV_DEFAULT_MODELS] == "hermes3:8b"
    search_path = installation.layout.comfyui_data / "extra_model_paths.yaml"
    assert search_path.is_absolute()
    assert specs["comfyui"].launch.argv[-2:] == (
        "--extra-model-paths-config",
        str(search_path),
    )


def test_unavailable_ollama_model_never_produces_a_ready_open_webui_launch(tmp_path):
    installation, _, configuration, _, _ = configuration_for(tmp_path, ollama_available=False)
    assert not configuration.service("open-webui").ok
    specs = {
        spec.service_id: spec
        for spec in control_specs_for(
            managed_snapshot(), layout=installation.layout, configuration=configuration
        )
    }
    assert specs["open-webui"].launch is None


def test_tampered_managed_config_is_withheld_from_runtime(tmp_path):
    installation, _, configuration, _, _ = configuration_for(tmp_path)
    writer = ManagedConfigWriter()
    writer.apply(configuration.documents)
    env_path = installation.layout.open_webui_data / OPEN_WEBUI_ENV_FILE
    original = env_path.read_text(encoding="utf-8")
    env_path.write_text(original.replace("DEFAULT_MODELS=hermes3:8b", "DEFAULT_MODELS=other:1"))
    assert not writer.verify(configuration.service("open-webui").documents).ok

    specs = {
        spec.service_id: spec
        for spec in control_specs_for(
            managed_snapshot(),
            layout=installation.layout,
            configuration=configuration,
            config_writer=writer,
        )
    }
    assert specs["open-webui"].launch is None


def test_external_component_stays_observation_only_even_with_valid_managed_config(tmp_path):
    installation, _, configuration, _, _ = configuration_for(tmp_path)
    writer = ManagedConfigWriter()
    writer.apply(configuration.documents)
    snapshot = IntegrationSnapshot(
        (
            ComponentIntegration(
                RuntimeHandoff(
                    "open-webui",
                    LaunchAvailability.EXTERNAL,
                    Ownership.EXTERNAL,
                    detected_path=tmp_path / "external",
                )
            ),
        )
    )
    specs = {
        spec.service_id: spec
        for spec in control_specs_for(
            snapshot,
            layout=installation.layout,
            configuration=configuration,
            config_writer=writer,
        )
    }
    assert specs["open-webui"].launch is None


@pytest.mark.parametrize("missing", [DIFFUSION_ID, ENCODER_ID, VAE_ID])
def test_workflow_ready_requires_every_file_model_and_the_template(tmp_path, missing):
    installation, catalog, installer, _, specs, repo_root, _ = prepared_domains(tmp_path)
    installer.paths.destination_for(catalog.require(missing)).unlink()
    resolution = resolve_workflow(
        specs["flux2_klein_t2i"],
        profile(),
        StackModelResolver(installer, catalog),
        repo_root=repo_root,
    )
    assert resolution.state is WorkflowState.MODEL_UNAVAILABLE
    assert not resolution.ready
    assert installation.layout.root.exists()


def test_plan_executor_delegates_model_acquisition_and_keeps_dry_run_read_only(tmp_path):
    installation, catalog, installer, ollama, specs, repo_root, _ = prepared_domains(tmp_path)
    ollama.available = False
    actions = InstallActionHandlers(
        profiles={"pepe": profile()},
        catalog=catalog,
        installer_factory=lambda selected: installer,
        workflow_specs=specs,
        repo_root=repo_root,
        layout=installation.layout,
    )
    executor = PlanExecutor(ProvisionContext(installation), action_handlers=actions.handlers())

    dry = executor.execute(action_plan(PlanAction.ACQUIRE_MODEL, str(OLLAMA_ID)), dry_run=True)
    assert dry.action_results[0].ok
    assert dry.action_results[0].payload.outcome is ModelOutcome.DRY_RUN
    assert ollama.pulled == []

    applied = executor.execute(action_plan(PlanAction.ACQUIRE_MODEL, str(OLLAMA_ID)), dry_run=False)
    assert applied.action_results[0].ok
    assert applied.action_results[0].payload.outcome is ModelOutcome.ACQUIRED
    assert ollama.pulled == ["hermes3:8b"]


def test_plan_executor_materialises_workflow_and_applies_verified_config(tmp_path):
    installation, _, _, _, _, _, actions = prepared_domains(tmp_path)
    executor = PlanExecutor(ProvisionContext(installation), action_handlers=actions.handlers())

    workflow = executor.execute(
        action_plan(PlanAction.APPLY_WORKFLOW, "text_to_image"), dry_run=False
    )
    assert workflow.action_results[0].ok
    assert isinstance(workflow.action_results[0].payload, dict)

    config_path = installation.layout.open_webui_data / OPEN_WEBUI_ENV_FILE
    dry = executor.execute(action_plan(PlanAction.CONFIGURE, "pepe"), dry_run=True)
    assert dry.action_results[0].ok
    assert not config_path.exists()

    applied = executor.execute(action_plan(PlanAction.CONFIGURE, "pepe"), dry_run=False)
    assert applied.action_results[0].ok
    configuration = applied.action_results[0].payload[0]
    assert isinstance(configuration, StackConfiguration)
    assert actions.config_writer.verify(configuration.documents).ok


def test_plan_executor_workflow_missing_template_fails_closed(tmp_path):
    installation, _, _, _, _, repo_root, actions = prepared_domains(tmp_path)
    (repo_root / "workflows" / "flux2-klein-t2i-api.json").unlink()
    executor = PlanExecutor(ProvisionContext(installation), action_handlers=actions.handlers())
    report = executor.execute(
        action_plan(PlanAction.APPLY_WORKFLOW, "text_to_image"), dry_run=False
    )
    assert not report.action_results[0].ok
    assert "template-missing" in report.action_results[0].detail


def test_plan_executor_missing_template_never_applies_configuration(tmp_path):
    installation, _, _, _, _, repo_root, actions = prepared_domains(tmp_path)
    (repo_root / "workflows" / "flux2-klein-t2i-api.json").unlink()
    executor = PlanExecutor(ProvisionContext(installation), action_handlers=actions.handlers())

    report = executor.execute(action_plan(PlanAction.CONFIGURE, "pepe"), dry_run=False)

    assert not report.action_results[0].ok
    assert not (installation.layout.open_webui_data / OPEN_WEBUI_ENV_FILE).exists()
