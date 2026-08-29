"""Tests for the plan -> provisioning bridge."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.execution import ExecutionReport, PlanExecutor
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.planning import PlanAction, build_install_plan
from local_ai_stack.profiles import load_profile
from local_ai_stack.provisioning import (
    ComponentState,
    Detection,
    Ownership,
    ProvisionContext,
    Provisioner,
    ProvisionOutcome,
    ProvisionStep,
)
from local_ai_stack.stacks import STANDARD_STACK

REPO = Path(__file__).resolve().parents[1]


class RecordingProvisioner(Provisioner):
    def __init__(self, component_id: str):
        self.component_id = component_id
        self.display_name = component_id
        self.calls: list[bool] = []

    def detect(self, context):
        return Detection(self.component_id, ComponentState.NOT_INSTALLED, Ownership.NONE)

    def apply(self, context, *, dry_run=False):
        self.calls.append(dry_run)
        detection = self.detect(context)
        steps = (ProvisionStep("install", self.component_id, "install"),)
        if dry_run:
            return self.dry_run_result(detection, steps)
        return self.installed(detection, steps)


@pytest.fixture
def profile():
    return load_profile(REPO / "profiles" / "pepe.yaml")


def executor_for(tmp_path, *provisioners):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved)
    registry = {p.component_id: p for p in provisioners}
    return PlanExecutor(context=context, provisioners=registry), context


def plan_for(profile, tmp_path):
    return build_install_plan(
        profile,
        installation=resolve_installation(InstallLocation.custom(tmp_path / "stack")),
        stack=STANDARD_STACK,
    )


# --- resolucao -------------------------------------------------------------


def test_resolve_covers_every_plan_step(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    plan = plan_for(profile, tmp_path)
    resolved = executor.resolve(plan)
    assert len(resolved) == len(plan.steps)
    assert [entry.step.sequence for entry in resolved] == [s.sequence for s in plan.steps]


def test_prepare_layout_and_registered_installs_are_supported(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    resolved = {
        (entry.action, entry.step.target): entry.supported
        for entry in executor.resolve(plan_for(profile, tmp_path))
    }
    assert resolved[(PlanAction.PREPARE_LAYOUT, str(tmp_path / "stack"))] is True
    assert resolved[(PlanAction.INSTALL, "ollama")] is True


def test_unregistered_component_is_unsupported_not_dropped(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    unsupported = {
        entry.step.target: entry.reason
        for entry in executor.resolve(plan_for(profile, tmp_path))
        if not entry.supported
    }
    assert "comfyui" in unsupported
    assert "no provisioner is registered" in unsupported["comfyui"]


def test_non_executable_actions_carry_a_reason(profile, tmp_path):
    executor, _ = executor_for(tmp_path)
    reasons = {
        entry.action: entry.reason for entry in executor.resolve(plan_for(profile, tmp_path))
    }
    assert "not executable yet" in reasons[PlanAction.CHECK]
    assert "not executable yet" in reasons[PlanAction.ACQUIRE_MODEL]


def test_resolution_creates_nothing(profile, tmp_path):
    executor, context = executor_for(tmp_path, RecordingProvisioner("ollama"))
    executor.resolve(plan_for(profile, tmp_path))
    assert not context.layout.root.exists()


# --- dry-run ---------------------------------------------------------------


def test_dry_run_touches_no_disk(profile, tmp_path):
    ollama = RecordingProvisioner("ollama")
    executor, context = executor_for(tmp_path, ollama)
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    assert report.dry_run is True
    assert report.ok
    assert ollama.calls == [True]
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reports_the_directories_it_would_create(profile, tmp_path):
    executor, context = executor_for(tmp_path, RecordingProvisioner("ollama"))
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    assert set(report.created_directories) == set(context.layout.all_directories)
    assert not context.layout.root.exists()


def test_dry_run_is_deterministic(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    plan = plan_for(profile, tmp_path)
    first = executor.execute(plan, dry_run=True)
    second = executor.execute(plan, dry_run=True)
    assert first.created_directories == second.created_directories
    assert [r.outcome for r in first.results] == [r.outcome for r in second.results]
    assert [e.step.sequence for e in first.unsupported] == [
        e.step.sequence for e in second.unsupported
    ]


def test_dry_run_results_are_dry_run(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    assert [r.outcome for r in report.results] == [ProvisionOutcome.DRY_RUN]


# --- execucao real ---------------------------------------------------------


def test_execute_prepares_the_layout_then_provisions(profile, tmp_path):
    ollama = RecordingProvisioner("ollama")
    executor, context = executor_for(tmp_path, ollama)
    report = executor.execute(plan_for(profile, tmp_path), dry_run=False)
    assert report.ok
    assert context.layout.exists()
    assert ollama.calls == [False]
    assert [r.outcome for r in report.results] == [ProvisionOutcome.INSTALLED]


def test_execute_is_idempotent_for_the_layout(profile, tmp_path):
    executor, context = executor_for(tmp_path, RecordingProvisioner("ollama"))
    plan = plan_for(profile, tmp_path)
    executor.execute(plan, dry_run=False)
    second = executor.execute(plan, dry_run=False)
    assert second.created_directories == ()
    assert context.layout.exists()


def test_layout_conflict_is_reported_as_a_structured_failure(profile, tmp_path):
    root = tmp_path / "stack"
    root.mkdir()
    (root / "apps").write_text("i am a file", encoding="utf-8")
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    report = executor.execute(plan_for(profile, tmp_path), dry_run=False)
    assert not report.ok
    failure = report.failures[0]
    assert failure.component_id == "layout"
    assert failure.failure is not None
    assert "apps" in failure.failure.detail
    assert (root / "apps").is_file()


def test_report_shape(profile, tmp_path):
    executor, _ = executor_for(tmp_path, RecordingProvisioner("ollama"))
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    assert isinstance(report, ExecutionReport)
    assert report.plan_profile_id == "pepe"
    assert report.failures == ()


def test_executor_without_provisioners_still_prepares_the_layout(profile, tmp_path):
    executor, context = executor_for(tmp_path)
    report = executor.execute(plan_for(profile, tmp_path), dry_run=False)
    assert context.layout.exists()
    assert report.results == ()
    assert {e.step.target for e in report.unsupported} >= {"ollama", "comfyui"}


def test_plan_without_installation_has_no_layout_step(profile, tmp_path):
    executor, context = executor_for(tmp_path, RecordingProvisioner("ollama"))
    report = executor.execute(build_install_plan(profile), dry_run=False)
    assert report.created_directories == ()
    assert not context.layout.root.exists()


def test_planner_still_has_no_executor(profile):
    plan = build_install_plan(profile)
    assert not hasattr(plan, "execute")
    assert plan.dry_run is True
