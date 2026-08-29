"""Bridge from an InstallPlan to provisioner calls.

The planner stays a planner. It describes work; it never performs any. This
module is the only place that turns a described step into a call, and it does
so through a registry, so a plan step for a component nobody implements yet is
reported as unsupported rather than silently dropped.

Two step kinds are executable today:

    PREPARE_LAYOUT   -> StackLayout.ensure()
    INSTALL <id>     -> the registered provisioner for that component

Everything else is carried through as UNSUPPORTED with its reason attached, so
the sequence a caller sees always covers the whole plan.

Dry-run is deterministic and touches no disk: `ensure(dry_run=True)` only reads,
and every provisioner receives `dry_run=True`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .layout import LayoutError
from .planning import InstallPlan, PlanAction, PlanStep
from .provisioning import (
    ComponentState,
    Detection,
    FailureReason,
    Ownership,
    ProvisionContext,
    Provisioner,
    ProvisionFailure,
    ProvisionOutcome,
    ProvisionResult,
)

ProvisionerRegistry = Mapping[str, Provisioner]

EXECUTABLE_ACTIONS = (PlanAction.PREPARE_LAYOUT, PlanAction.INSTALL)


@dataclass(frozen=True)
class ExecutionStep:
    """One plan step, resolved against the registry before anything runs."""

    step: PlanStep
    supported: bool
    component_id: str | None = None
    reason: str = ""

    @property
    def action(self) -> PlanAction:
        return self.step.action


@dataclass(frozen=True)
class ExecutionReport:
    plan_profile_id: str
    dry_run: bool
    results: tuple[ProvisionResult, ...] = ()
    created_directories: tuple[Path, ...] = ()
    unsupported: tuple[ExecutionStep, ...] = ()

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def failures(self) -> tuple[ProvisionResult, ...]:
        return tuple(r for r in self.results if r.outcome is ProvisionOutcome.FAILED)


@dataclass
class PlanExecutor:
    """Executes the executable part of a plan, and says what it skipped."""

    context: ProvisionContext
    provisioners: ProvisionerRegistry = field(default_factory=dict)

    # --- resolucao ---------------------------------------------------------

    def resolve(self, plan: InstallPlan) -> tuple[ExecutionStep, ...]:
        """Map every plan step to an execution step. Deterministic, read-only."""
        resolved: list[ExecutionStep] = []
        for step in plan.steps:
            if step.action is PlanAction.PREPARE_LAYOUT:
                resolved.append(ExecutionStep(step=step, supported=True))
            elif step.action is PlanAction.INSTALL:
                provisioner = self.provisioners.get(step.target)
                resolved.append(
                    ExecutionStep(
                        step=step,
                        supported=provisioner is not None,
                        component_id=step.target,
                        reason=(
                            ""
                            if provisioner is not None
                            else f"no provisioner is registered for {step.target}"
                        ),
                    )
                )
            else:
                resolved.append(
                    ExecutionStep(
                        step=step,
                        supported=False,
                        reason=f"{step.action} is not executable yet",
                    )
                )
        return tuple(resolved)

    # --- execucao ----------------------------------------------------------

    def execute(self, plan: InstallPlan, *, dry_run: bool = True) -> ExecutionReport:
        """Run the supported steps in plan order.

        With `dry_run=True` nothing is created and no provisioner mutates
        anything, so the report is a faithful preview of the same sequence.
        """
        results: list[ProvisionResult] = []
        created: list[Path] = []
        unsupported: list[ExecutionStep] = []

        for entry in self.resolve(plan):
            if not entry.supported:
                unsupported.append(entry)
                continue
            if entry.action is PlanAction.PREPARE_LAYOUT:
                created.extend(self._prepare_layout(dry_run=dry_run, results=results))
                continue
            provisioner = self.provisioners[entry.step.target]
            results.append(provisioner.apply(self.context, dry_run=dry_run))

        return ExecutionReport(
            plan_profile_id=plan.profile_id,
            dry_run=dry_run,
            results=tuple(results),
            created_directories=tuple(created),
            unsupported=tuple(unsupported),
        )

    def _prepare_layout(self, *, dry_run: bool, results: list[ProvisionResult]) -> tuple[Path, ...]:
        try:
            return self.context.layout.ensure(dry_run=dry_run)
        except LayoutError as exc:
            results.append(
                ProvisionResult(
                    component_id="layout",
                    outcome=ProvisionOutcome.FAILED,
                    detection=Detection(
                        component_id="layout",
                        state=ComponentState.INVALID,
                        ownership=Ownership.MANAGED,
                        path=self.context.layout.root,
                    ),
                    failure=ProvisionFailure(
                        reason=FailureReason.FILESYSTEM,
                        message="Could not prepare the install layout.",
                        detail=str(exc),
                    ),
                )
            )
            return ()
