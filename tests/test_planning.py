from pathlib import Path

import pytest

from local_ai_stack.endpoints import StackEndpoints
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.planning import PlanAction, build_install_plan
from local_ai_stack.profiles import load_profile
from local_ai_stack.stacks import STANDARD_STACK

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pepe():
    return load_profile(ROOT / "profiles" / "pepe.yaml")


def test_install_plan_is_deterministic_and_dry_run_only():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    first = build_install_plan(profile)
    second = build_install_plan(profile)
    assert first == second
    assert first.dry_run is True
    assert [step.sequence for step in first.steps] == list(range(1, len(first.steps) + 1))
    assert not hasattr(first, "execute")


def test_install_plan_reflects_enabled_services_models_and_workflows():
    profile = load_profile(ROOT / "profiles" / "pepe.yaml")
    plan = build_install_plan(profile)
    targets = {(step.action, step.target) for step in plan.steps}
    assert (PlanAction.INSTALL, "ollama") in targets
    assert (PlanAction.INSTALL, "open-webui") in targets
    assert (PlanAction.INSTALL, "comfyui") in targets
    assert (PlanAction.ACQUIRE_MODEL, "ollama/hermes3_8b") in targets
    assert (PlanAction.APPLY_WORKFLOW, "text_to_image") in targets


# --- configuracao resolvida como contexto opcional -------------------------


def test_plan_without_installation_is_unchanged(pepe):
    plan = build_install_plan(pepe)
    assert plan.install_root is None
    assert plan.install_mode is None
    assert plan.stack_id is None
    assert PlanAction.PREPARE_LAYOUT not in {step.action for step in plan.steps}
    assert plan.dry_run is True


def test_plan_records_the_resolved_installation(pepe, tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "IA's"))
    plan = build_install_plan(pepe, installation=resolved, stack=STANDARD_STACK)
    assert plan.install_root == resolved.root
    assert plan.install_mode == "custom"
    assert plan.stack_id == "standard"
    assert plan.profile_id == "pepe"


def test_plan_describes_the_explicit_managed_endpoints(pepe):
    endpoints = StackEndpoints.from_profile(
        pepe,
        port_overrides={"ollama": 12434, "open-webui": 18080, "comfyui": 18188},
    )

    plan = build_install_plan(pepe, endpoints=endpoints)

    install_summaries = {
        step.target: step.summary for step in plan.steps if step.action is PlanAction.INSTALL
    }
    assert install_summaries == {
        "comfyui": "Install comfyui and reserve local port 18188.",
        "ollama": "Install ollama and reserve local port 12434.",
        "open-webui": "Install open-webui and reserve local port 18080.",
    }


def test_plan_adds_a_layout_step_for_the_resolved_root(pepe, tmp_path):
    resolved = resolve_installation(InstallLocation.portable(), repo_root=tmp_path)
    plan = build_install_plan(pepe, installation=resolved)
    prepare = [step for step in plan.steps if step.action is PlanAction.PREPARE_LAYOUT]
    assert len(prepare) == 1
    assert prepare[0].target == str(resolved.root)
    assert prepare[0].sequence == 2  # logo apos o check de sistema


def test_plan_stays_sequential_with_installation_context(pepe, tmp_path):
    plan = build_install_plan(
        pepe, installation=resolve_installation(InstallLocation.custom(tmp_path))
    )
    assert [step.sequence for step in plan.steps] == list(range(1, len(plan.steps) + 1))
    assert plan.dry_run is True
    assert not hasattr(plan, "execute")


def test_planning_creates_no_directories(pepe, tmp_path):
    target = tmp_path / "AI"
    build_install_plan(pepe, installation=resolve_installation(InstallLocation.custom(target)))
    assert not target.exists()


def test_same_profile_can_be_planned_for_any_install_mode(pepe, tmp_path):
    modes = {
        build_install_plan(
            pepe, installation=resolve_installation(location, repo_root=tmp_path / "repo")
        ).install_mode
        for location in (
            InstallLocation.portable(),
            InstallLocation.custom(tmp_path / "c"),
        )
    }
    assert modes == {"portable", "custom"}
