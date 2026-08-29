"""Tests for the provisioner contract itself, using a toy provisioner."""

from __future__ import annotations

import pytest

from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.provisioning import (
    ComponentState,
    Detection,
    EventLevel,
    FailureReason,
    Ownership,
    ProvisionContext,
    Provisioner,
    ProvisionOutcome,
    ProvisionStep,
)


def context_for(tmp_path, **kwargs) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, **kwargs)


class ToyProvisioner(Provisioner):
    component_id = "toy"
    display_name = "Toy"
    supports_apply = True

    def __init__(self, *, present: bool = False):
        self.present = present
        self.applied = 0

    def detect(self, context):
        marker = context.layout.state / "toy.marker"
        installed = self.present or marker.is_file()
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED if installed else ComponentState.NOT_INSTALLED,
            ownership=Ownership.MANAGED if installed else Ownership.NONE,
            path=marker if installed else None,
        )

    def apply(self, context, *, dry_run=False):
        detection = self.detect(context)
        if detection.is_usable:
            return self.skipped(detection, "already installed")
        steps = (ProvisionStep("install", self.component_id, "Install the toy."),)
        if dry_run:
            return self.dry_run_result(detection, steps)
        context.layout.ensure()
        (context.layout.state / "toy.marker").write_text("ok", encoding="utf-8")
        self.applied += 1
        return self.installed(self.detect(context), steps)


# --- contexto --------------------------------------------------------------


def test_context_exposes_the_layout_from_the_installation(tmp_path):
    context = context_for(tmp_path)
    assert context.layout is context.installation.layout
    assert context.layout.root == tmp_path / "stack"


def test_context_creates_nothing(tmp_path):
    context_for(tmp_path)
    assert not (tmp_path / "stack").exists()


def test_events_are_delivered_to_the_sink(tmp_path):
    seen = []
    context = context_for(tmp_path, events=seen.append)
    context.emit("toy", "hello", EventLevel.WARNING)
    assert len(seen) == 1
    assert seen[0].component_id == "toy"
    assert seen[0].level is EventLevel.WARNING
    assert seen[0].message == "hello"


def test_events_are_sanitized(tmp_path):
    seen = []
    context = context_for(tmp_path, events=seen.append)
    context.emit("toy", "downloading https://user:s3cr3t@host/x.zip?token=abcdef123456")
    assert "s3cr3t" not in seen[0].message
    assert "abcdef123456" not in seen[0].message


def test_no_sink_is_fine(tmp_path):
    context_for(tmp_path).emit("toy", "no listener attached")


# --- deteccao --------------------------------------------------------------


def test_detect_reports_absence(tmp_path):
    detection = ToyProvisioner().detect(context_for(tmp_path))
    assert detection.state is ComponentState.NOT_INSTALLED
    assert detection.ownership is Ownership.NONE
    assert detection.is_present is False
    assert detection.is_usable is False


def test_detect_creates_no_directories(tmp_path):
    context = context_for(tmp_path)
    ToyProvisioner().detect(context)
    assert not context.layout.root.exists()


def test_detection_state_and_ownership_are_independent_axes():
    external = Detection("x", ComponentState.INSTALLED, Ownership.EXTERNAL)
    assert external.is_usable and external.is_external and not external.is_managed
    broken = Detection("x", ComponentState.INVALID, Ownership.MANAGED)
    assert broken.is_present and not broken.is_usable and broken.is_managed


def test_states_and_outcomes_cover_the_required_vocabulary():
    assert {str(s) for s in ComponentState} >= {"not-installed", "installed", "invalid", "unknown"}
    assert {str(o) for o in ProvisionOutcome} == {"installed", "skipped", "failed", "dry-run"}


# --- plano -----------------------------------------------------------------


def test_plan_proposes_work_when_absent(tmp_path):
    plan = ToyProvisioner().plan(context_for(tmp_path))
    assert plan.steps
    assert plan.is_noop is False
    assert plan.detection.state is ComponentState.NOT_INSTALLED


def test_plan_is_a_noop_when_already_installed(tmp_path):
    plan = ToyProvisioner(present=True).plan(context_for(tmp_path))
    assert plan.steps == ()
    assert plan.is_noop is True


def test_plan_creates_nothing(tmp_path):
    context = context_for(tmp_path)
    ToyProvisioner().plan(context)
    assert not context.layout.root.exists()


# --- dry-run e idempotencia ------------------------------------------------


def test_dry_run_changes_nothing(tmp_path):
    context = context_for(tmp_path)
    provisioner = ToyProvisioner()
    result = provisioner.apply(context, dry_run=True)
    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.ok and not result.changed
    assert provisioner.applied == 0
    assert not context.layout.root.exists()


def test_apply_installs_then_becomes_idempotent(tmp_path):
    context = context_for(tmp_path)
    provisioner = ToyProvisioner()

    first = provisioner.apply(context)
    assert first.outcome is ProvisionOutcome.INSTALLED
    assert first.changed

    second = provisioner.apply(context)
    assert second.outcome is ProvisionOutcome.SKIPPED
    assert not second.changed
    assert provisioner.applied == 1


def test_verify_defaults_to_detect(tmp_path):
    context = context_for(tmp_path)
    provisioner = ToyProvisioner()
    provisioner.apply(context)
    assert provisioner.verify(context).is_usable


# --- falha estruturada -----------------------------------------------------


def test_base_provisioner_refuses_instead_of_pretending(tmp_path):
    class Unsupported(Provisioner):
        component_id = "unsupported"
        display_name = "Unsupported"

        def detect(self, context):
            return Detection(self.component_id, ComponentState.NOT_INSTALLED)

    result = Unsupported().apply(context_for(tmp_path))
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.UNSUPPORTED
    assert not result.ok


def test_failure_carries_structured_information(tmp_path):
    provisioner = ToyProvisioner()
    detection = Detection("toy", ComponentState.NOT_INSTALLED)
    result = provisioner.failed(
        detection, FailureReason.NETWORK, "could not reach https://host/x?token=abcdef123456"
    )
    assert result.failure is not None
    assert result.failure.reason is FailureReason.NETWORK
    assert "abcdef123456" not in result.failure.message
    assert result.outcome is ProvisionOutcome.FAILED
    assert str(result.failure).startswith("network:")


def test_detect_is_abstract_in_the_base_class(tmp_path):
    with pytest.raises(NotImplementedError):
        Provisioner().detect(context_for(tmp_path))
