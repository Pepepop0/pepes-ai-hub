"""A capability is enabled only when the profile can configure it.

The smoke that motivated this: the shipped Test profile enabled chat and image
generation while every model behind them was TBD and no workflow was declared.
Nothing noticed until "Aplicando configurações", after three components had
been installed. These tests pin the rule at every layer that has to agree on
it: the profile loader, the pre-install check, the configurator and the launch
gate -- and they pin it without naming any profile.
"""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_service_config import resolver_for
from test_workflows import SHIPPED_GRAPH

from local_ai_stack.components import ComponentId
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.integration import ComponentIntegration, IntegrationSnapshot, control_specs_for
from local_ai_stack.managed_config import ManagedConfigWriter
from local_ai_stack.profiles import (
    FEATURE_CHAT,
    FEATURE_IMAGE_GENERATION,
    ProfileError,
    capability_problems,
    load_profiles,
    validate_profile_data,
)
from local_ai_stack.provisioning import LaunchAvailability, Ownership, RuntimeHandoff
from local_ai_stack.service_config import (
    ENV_DEFAULT_MODELS,
    ENV_ENABLE_OLLAMA_API,
    ENV_OLLAMA_BASE_URL,
    build_stack_configuration,
    declared_capability_problems,
)
from local_ai_stack.workflows import load_workflow_manifest

REPO = Path(__file__).resolve().parents[1]
SHIPPED = {profile.id: profile for profile in load_profiles(REPO / "profiles")}


@pytest.fixture
def specs():
    return load_workflow_manifest(REPO / "manifests" / "workflows.yaml")


def data_for(name: str) -> dict:
    return deepcopy(SHIPPED[name].raw)


def configure(profile, tmp_path, specs):
    return build_stack_configuration(
        profile,
        resolver_for(profile, tmp_path),
        resolve_installation(InstallLocation.custom(tmp_path / "stack")).layout,
        workflow_specs=specs,
        repo_root=REPO,
        graphs={"flux2_klein_t2i": json.loads(SHIPPED_GRAPH.read_text(encoding="utf-8"))},
    )


# --- every shipped profile is coherent -------------------------------------------


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_no_shipped_profile_enables_a_capability_it_cannot_configure(name, specs):
    profile = SHIPPED[name]
    assert capability_problems(profile) == ()
    assert declared_capability_problems(profile, specs) == ()


def test_the_test_profile_is_a_light_install_and_runtime_smoke():
    """Every service on; no capability on, so nothing heavy is ever fetched."""
    test = SHIPPED["test"]
    assert all(service.enabled for service in test.services.values())
    assert not any(test.features.values())
    assert test.model_requirements == ()
    assert test.workflows == {}


@pytest.mark.parametrize("name", ["capella", "pepe"])
def test_the_reference_profiles_keep_every_capability_they_had(name):
    profile = SHIPPED[name]
    assert all(profile.features.values())
    assert profile.model_for("chat.primary") is not None
    assert "text_to_image" in profile.workflows


# --- enabled without its data is refused when the profile loads ------------------


def test_enabled_chat_without_a_primary_model_is_refused():
    data = data_for("test")
    data["features"]["chat"] = True
    with pytest.raises(ProfileError, match="models.chat.primary"):
        validate_profile_data(data)


def test_enabled_chat_over_tbd_is_refused_exactly_like_an_absent_model():
    data = data_for("pepe")
    data["models"]["chat"]["primary"] = "TBD"
    with pytest.raises(ProfileError, match="models.chat.primary"):
        validate_profile_data(data)


def test_enabled_chat_without_ollama_is_refused():
    data = data_for("pepe")
    data["services"]["ollama"]["enabled"] = False
    with pytest.raises(ProfileError, match="services.ollama"):
        validate_profile_data(data)


def test_enabled_image_generation_without_text_to_image_is_refused():
    data = data_for("test")
    data["features"]["image_generation"] = True
    with pytest.raises(ProfileError, match="workflows.text_to_image"):
        validate_profile_data(data)


def test_enabled_image_generation_without_comfyui_is_refused():
    data = data_for("pepe")
    data["services"]["comfyui"]["enabled"] = False
    with pytest.raises(ProfileError, match="services.comfyui"):
        validate_profile_data(data)


@pytest.mark.parametrize(
    ("refinement", "base"),
    [("vision", "chat"), ("tools", "chat"), ("image_editing", "image_generation")],
)
def test_a_refinement_without_its_base_capability_is_refused(refinement, base):
    data = data_for("test")
    data["features"][refinement] = True
    with pytest.raises(ProfileError, match=f"features.{refinement} is enabled but features.{base}"):
        validate_profile_data(data)


# --- disabled needs nothing ----------------------------------------------------------


def test_disabled_chat_requires_no_primary_model():
    data = data_for("pepe")
    data["features"].update(chat=False, vision=False, tools=False)
    del data["models"]["chat"]
    profile = validate_profile_data(data)
    assert profile.model_for("chat.primary") is None
    assert capability_problems(profile) == ()


def test_disabled_image_generation_requires_no_workflow_or_models():
    data = data_for("pepe")
    data["features"].update(image_generation=False, image_editing=False)
    del data["models"]["image"]
    del data["workflows"]
    profile = validate_profile_data(data)
    assert capability_problems(profile) == ()


def test_the_test_profile_configures_cleanly_with_nothing_to_select(tmp_path, specs):
    configuration = configure(SHIPPED["test"], tmp_path, specs)

    assert configuration.ok, configuration.problems
    open_webui = configuration.service(ComponentId.OPEN_WEBUI)
    assert open_webui.env == {ENV_ENABLE_OLLAMA_API: "false"}
    assert ENV_OLLAMA_BASE_URL not in open_webui.env, "no address is invented"
    assert ENV_DEFAULT_MODELS not in open_webui.env, "no model is invented"
    comfyui = configuration.service(ComponentId.COMFYUI)
    assert comfyui.ok and comfyui.documents == ()


# --- the configurator stays fail-closed for an enabled capability -----------------


def test_configuration_still_fails_closed_for_enabled_chat_without_a_model(tmp_path, specs):
    """`replace` bypasses the loader, as any code building a Profile could."""
    incoherent = replace(SHIPPED["test"], features={**SHIPPED["test"].features, FEATURE_CHAT: True})
    configuration = configure(incoherent, tmp_path, specs)
    assert not configuration.ok
    assert any("no primary chat model" in problem for problem in configuration.problems)


def test_configuration_still_fails_closed_for_enabled_images_without_a_workflow(tmp_path, specs):
    incoherent = replace(
        SHIPPED["test"], features={**SHIPPED["test"].features, FEATURE_IMAGE_GENERATION: True}
    )
    configuration = configure(incoherent, tmp_path, specs)
    assert not configuration.ok
    assert any("text_to_image" in problem for problem in configuration.problems)
    assert any("ComfyUI" in problem for problem in configuration.problems)


def test_enabled_images_with_unavailable_models_still_leave_comfyui_unconfigured(tmp_path, specs):
    pepe = SHIPPED["pepe"]
    empty_resolver = resolver_for(replace(pepe, model_requirements=()), tmp_path)
    configuration = build_stack_configuration(
        pepe,
        empty_resolver,
        resolve_installation(InstallLocation.custom(tmp_path / "stack")).layout,
        workflow_specs=specs,
        repo_root=REPO,
    )
    assert not configuration.service(ComponentId.COMFYUI).ok


# --- pre-install check with the manifests -------------------------------------------


def test_an_unmapped_text_to_image_workflow_is_caught_before_installing(specs):
    pepe = SHIPPED["pepe"]
    unmapped = {**specs, "flux2_klein_t2i": replace(specs["flux2_klein_t2i"], unresolved=("seed",))}
    problems = declared_capability_problems(pepe, unmapped)
    assert any("not mapped yet" in problem for problem in problems)


def test_an_edit_graph_is_refused_as_text_to_image_before_installing(specs):
    pepe = SHIPPED["pepe"]
    swapped = replace(
        pepe, workflows={**pepe.workflows, "text_to_image": pepe.workflows["image_edit"]}
    )
    problems = declared_capability_problems(swapped, specs)
    assert any("edits an input image" in problem for problem in problems)


def test_an_image_edit_graph_without_an_input_image_is_refused(specs):
    from local_ai_stack.workflows import WorkflowParameter

    edit = specs["flux2_klein_edit"]
    blind = replace(
        edit,
        bindings={k: v for k, v in edit.bindings.items() if k is not WorkflowParameter.IMAGE},
    )
    problems = declared_capability_problems(SHIPPED["pepe"], {**specs, edit.id: blind})
    assert any("takes no input image" in problem for problem in problems)


def test_an_unknown_text_to_image_manifest_is_caught_before_installing(specs):
    pepe = SHIPPED["pepe"]
    unknown = replace(
        pepe,
        workflows={
            **pepe.workflows,
            "text_to_image": replace(pepe.workflows["text_to_image"], manifest_id="missing"),
        },
    )
    problems = declared_capability_problems(unknown, specs)
    assert any("does not declare" in problem for problem in problems)


def test_a_workflow_model_the_profile_does_not_name_is_caught_before_installing(specs):
    pepe = SHIPPED["pepe"]
    no_vae = replace(
        pepe,
        model_requirements=tuple(r for r in pepe.model_requirements if r.role != "image.vae"),
    )
    problems = declared_capability_problems(no_vae, specs)
    assert any("image.vae" in problem for problem in problems)


def test_an_image_edit_workflow_still_being_mapped_does_not_block(specs):
    """Configuration never depended on it; the run reports it as a warning."""
    drafting = {
        **specs,
        "flux2_klein_edit": replace(specs["flux2_klein_edit"], unresolved=("image",)),
    }
    assert declared_capability_problems(SHIPPED["capella"], drafting) == ()


def test_the_shipped_profiles_are_coherent_with_the_mapped_edit_workflow(specs):
    assert specs["flux2_klein_edit"].complete
    for name in ("capella", "pepe"):
        assert declared_capability_problems(SHIPPED[name], specs) == ()


# --- the launch gate follows the capability, not the service -----------------------


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


def test_comfyui_launches_without_model_paths_when_no_capability_needs_them(tmp_path, specs):
    layout = resolve_installation(InstallLocation.custom(tmp_path / "stack")).layout
    configuration = configure(SHIPPED["test"], tmp_path, specs)
    writer = ManagedConfigWriter()
    assert writer.apply(configuration.documents).ok

    launches = {
        spec.service_id: spec.launch
        for spec in control_specs_for(
            managed_snapshot(), layout=layout, configuration=configuration, config_writer=writer
        )
    }

    assert launches["comfyui"] is not None
    assert "--extra-model-paths-config" not in launches["comfyui"].argv
    assert launches["open-webui"] is not None
    assert launches["open-webui"].env[ENV_ENABLE_OLLAMA_API] == "false"


def test_comfyui_is_withheld_when_an_enabled_capability_lacks_its_models(tmp_path, specs):
    pepe = SHIPPED["pepe"]
    layout = resolve_installation(InstallLocation.custom(tmp_path / "stack")).layout
    configuration = build_stack_configuration(
        pepe,
        resolver_for(replace(pepe, model_requirements=()), tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=REPO,
    )
    launches = {
        spec.service_id: spec.launch
        for spec in control_specs_for(
            managed_snapshot(), layout=layout, configuration=configuration
        )
    }
    assert launches["comfyui"] is None


# --- no layer branches on a profile name -------------------------------------------


@pytest.mark.parametrize(
    "module",
    ["profiles.py", "service_config.py", "first_run.py", "integration.py", "stack_application.py"],
)
def test_no_capability_rule_is_keyed_on_a_profile_name(module):
    source = (REPO / "src" / "local_ai_stack" / module).read_text(encoding="utf-8")
    for name in SHIPPED:
        assert not re.search(rf"""["']{name}["']""", source), f"{module} names profile {name!r}"


def test_a_renamed_copy_of_a_profile_behaves_identically(tmp_path, specs):
    data = data_for("test")
    data["id"] = "anything-else"
    renamed = validate_profile_data(data)
    assert capability_problems(renamed) == capability_problems(SHIPPED["test"])
    assert configure(renamed, tmp_path, specs).ok
