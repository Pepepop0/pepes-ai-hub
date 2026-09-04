from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .installation import ResolvedInstallation
from .profiles import Profile
from .stacks import StackDefinition


class PlanAction(StrEnum):
    CHECK = "check"
    PREPARE_LAYOUT = "prepare-layout"
    INSTALL = "install"
    ACQUIRE_MODEL = "acquire-model"
    APPLY_WORKFLOW = "apply-workflow"
    CONFIGURE = "configure"


@dataclass(frozen=True)
class PlanStep:
    sequence: int
    action: PlanAction
    target: str
    summary: str


@dataclass(frozen=True)
class InstallPlan:
    profile_id: str
    steps: tuple[PlanStep, ...]
    dry_run: bool = field(default=True, init=False)
    stack_id: str | None = None
    install_mode: str | None = None
    install_root: Path | None = None


def build_install_plan(
    profile: Profile,
    *,
    installation: ResolvedInstallation | None = None,
    stack: StackDefinition | None = None,
) -> InstallPlan:
    """Build a deterministic description of future work without executing any step.

    `installation` and `stack` are optional context. When an installation is
    given the plan states where the work would land and adds a step for
    preparing the directory layout; it still stats nothing and creates nothing.
    Computing which directories are missing belongs to whoever runs the plan,
    via StackLayout.ensure(dry_run=True).
    """
    pending: list[tuple[PlanAction, str, str]] = [
        (
            PlanAction.CHECK,
            "system",
            "Verify the detected system against the selected profile.",
        )
    ]

    if installation is not None:
        pending.append(
            (
                PlanAction.PREPARE_LAYOUT,
                str(installation.root),
                f"Prepare the {installation.mode} install layout under {installation.root}.",
            )
        )

    for service_id in sorted(profile.services):
        service = profile.services[service_id]
        if service.enabled:
            pending.append(
                (
                    PlanAction.INSTALL,
                    service_id,
                    f"Install {service_id} and reserve local port {service.port}.",
                )
            )

    for model_id in sorted(
        {requirement.model_id for requirement in profile.model_requirements},
        key=lambda item: item.value,
    ):
        model = str(model_id)
        pending.append(
            (
                PlanAction.ACQUIRE_MODEL,
                model,
                f"Acquire and validate model artifact {model}.",
            )
        )

    for workflow_id in sorted(profile.workflows):
        manifest_id = profile.workflows[workflow_id].manifest_id
        pending.append(
            (
                PlanAction.APPLY_WORKFLOW,
                workflow_id,
                f"Apply workflow manifest {manifest_id}.",
            )
        )

    pending.append(
        (
            PlanAction.CONFIGURE,
            profile.id,
            f"Apply declarative configuration for profile {profile.id}.",
        )
    )
    return InstallPlan(
        profile_id=profile.id,
        steps=tuple(
            PlanStep(sequence=index, action=action, target=target, summary=summary)
            for index, (action, target, summary) in enumerate(pending, start=1)
        ),
        stack_id=None if stack is None else stack.id,
        install_mode=None if installation is None else str(installation.mode),
        install_root=None if installation is None else installation.root,
    )
