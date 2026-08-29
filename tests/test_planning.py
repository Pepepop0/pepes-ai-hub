from pathlib import Path

from local_ai_stack.planning import PlanAction, build_install_plan
from local_ai_stack.profiles import load_profile

ROOT = Path(__file__).resolve().parents[1]


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
    assert (PlanAction.INSTALL, "open_webui") in targets
    assert (PlanAction.INSTALL, "comfyui") in targets
    assert (PlanAction.ACQUIRE_MODEL, "hermes3:8b") in targets
    assert (PlanAction.APPLY_WORKFLOW, "text_to_image") in targets
