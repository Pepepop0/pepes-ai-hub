from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .profiles import Profile


class PlanAction(StrEnum):
    CHECK = "check"
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


def _model_references(models: dict[str, dict[str, Any]]) -> list[str]:
    references: set[str] = set()
    for group in models.values():
        for key, value in group.items():
            if (
                key in {"primary", "diffusion", "text_encoder", "vae"}
                and isinstance(value, str)
                and value
                and value.upper() != "TBD"
            ):
                references.add(value)
    return sorted(references, key=str.casefold)


def build_install_plan(profile: Profile) -> InstallPlan:
    """Build a deterministic description of future work without executing any step."""
    pending: list[tuple[PlanAction, str, str]] = [
        (
            PlanAction.CHECK,
            "system",
            "Verify the detected system against the selected profile.",
        )
    ]

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

    for model in _model_references(profile.models):
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
    )
