"""Composition of install-plan actions with the IC5 and IC6 domains.

This is orchestration only.  Model acquisition, workflow resolution/rendering
and managed-file ownership remain in their respective domains; these handlers
translate their native outcomes into :class:`ActionExecutionResult` for the
plan executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .download import DownloadProgress
from .endpoints import StackEndpoints
from .execution import ActionExecutionResult, PlanActionHandler
from .layout import StackLayout
from .managed_config import ManagedConfigWriter
from .model_catalog import ModelCatalog, ModelCatalogError
from .model_installer import ModelInstaller
from .model_resolver import StackModelResolver
from .planning import InstallPlan, PlanAction, PlanStep
from .profiles import Profile
from .service_config import build_stack_configuration
from .workflows import (
    WorkflowError,
    WorkflowParameters,
    WorkflowResolution,
    WorkflowSpec,
    load_workflow_graph,
    render_workflow,
    resolve_workflow,
    verify_workflow_references,
)

ModelInstallerFactory = Callable[[Profile], ModelInstaller]
EndpointFactory = Callable[[Profile], StackEndpoints]
TransferSink = Callable[[str, DownloadProgress], None]


@dataclass
class InstallActionHandlers:
    """Bind one installation to the existing model/workflow/config domains."""

    profiles: Mapping[str, Profile]
    catalog: ModelCatalog
    installer_factory: ModelInstallerFactory
    workflow_specs: Mapping[str, WorkflowSpec]
    repo_root: Path
    layout: StackLayout
    config_writer: ManagedConfigWriter = field(default_factory=ManagedConfigWriter)
    endpoints_for: EndpointFactory = StackEndpoints.from_profile
    # Raw byte-level observations for one HTTP model download, keyed by the
    # plan target that is transferring. None means nobody is listening: the
    # Downloader's own progress hook is simply never given a callback.
    on_transfer: TransferSink | None = None
    _installers: dict[str, ModelInstaller] = field(default_factory=dict, init=False, repr=False)

    def handlers(self) -> Mapping[PlanAction, PlanActionHandler]:
        return {
            PlanAction.ACQUIRE_MODEL: self.acquire_model,
            PlanAction.APPLY_WORKFLOW: self.apply_workflow,
            PlanAction.CONFIGURE: self.configure,
        }

    def _profile(self, plan: InstallPlan) -> Profile | None:
        return self.profiles.get(plan.profile_id)

    def _installer(self, profile: Profile) -> ModelInstaller:
        installer = self._installers.get(profile.id)
        if installer is None:
            installer = self.installer_factory(profile)
            self._installers[profile.id] = installer
        return installer

    @staticmethod
    def _failure(
        step: PlanStep, detail: str, payload: object | None = None
    ) -> ActionExecutionResult:
        return ActionExecutionResult(step.action, step.target, False, detail, payload=payload)

    def acquire_model(
        self, plan: InstallPlan, step: PlanStep, dry_run: bool
    ) -> ActionExecutionResult:
        """Delegate one canonical model target to IC5's ModelInstaller."""
        profile = self._profile(plan)
        if profile is None:
            return self._failure(step, f"unknown profile {plan.profile_id!r}")
        try:
            model_id = self.catalog.normalize(step.target)
            spec = self.catalog.require(model_id)
        except ModelCatalogError as exc:
            return self._failure(step, str(exc))

        on_progress = None
        if self.on_transfer is not None:
            sink, target = self.on_transfer, step.target

            def on_progress(raw: DownloadProgress, sink=sink, target=target) -> None:
                sink(target, raw)

        result = self._installer(profile).apply(spec, dry_run=dry_run, progress=on_progress)
        detail = result.detail or f"model acquisition is {result.outcome.value}"
        return ActionExecutionResult(
            step.action,
            step.target,
            result.ok,
            detail,
            changed=result.changed,
            payload=result,
        )

    def _resolve_workflow(
        self, profile: Profile, target: str, resolver: StackModelResolver
    ) -> WorkflowResolution | None:
        declaration = profile.workflows.get(target)
        if declaration is None:
            return None
        spec = self.workflow_specs.get(declaration.manifest_id)
        if spec is None:
            return None
        return resolve_workflow(spec, profile, resolver, repo_root=self.repo_root)

    def apply_workflow(
        self, plan: InstallPlan, step: PlanStep, dry_run: bool
    ) -> ActionExecutionResult:
        """Resolve and materialise a workflow through IC6, in memory only."""
        profile = self._profile(plan)
        if profile is None:
            return self._failure(step, f"unknown profile {plan.profile_id!r}")
        resolver = StackModelResolver(self._installer(profile), self.catalog)
        resolution = self._resolve_workflow(profile, step.target, resolver)
        if resolution is None:
            return self._failure(step, f"workflow {step.target!r} is not declared or manifested")
        if not resolution.ready:
            detail = f"workflow is {resolution.state.value}"
            if resolution.problems:
                detail += f": {'; '.join(resolution.problems)}"
            return self._failure(step, detail, resolution)
        try:
            graph = load_workflow_graph(resolution)
            rendered = render_workflow(resolution, WorkflowParameters(), graph=graph)
        except (OSError, TypeError, ValueError, WorkflowError) as exc:
            return self._failure(step, f"workflow could not be materialised: {exc}", resolution)
        return ActionExecutionResult(
            step.action,
            step.target,
            True,
            "workflow resolved and materialised in memory",
            payload=rendered,
        )

    def _graphs_for(
        self, profile: Profile, resolver: StackModelResolver
    ) -> dict[str, Mapping[str, object]]:
        graphs: dict[str, Mapping[str, object]] = {}
        for declaration in profile.workflows.values():
            spec = self.workflow_specs.get(declaration.manifest_id)
            if spec is None:
                continue
            resolution = resolve_workflow(spec, profile, resolver, repo_root=self.repo_root)
            if resolution.ready:
                graph = load_workflow_graph(resolution)
                problems = verify_workflow_references(resolution, graph)
                if problems:
                    raise WorkflowError(
                        f"workflow {resolution.spec.id!r} does not match its template: "
                        + "; ".join(problems)
                    )
                graphs[resolution.spec.id] = graph
        return graphs

    def configure(self, plan: InstallPlan, step: PlanStep, dry_run: bool) -> ActionExecutionResult:
        """Generate, apply and (for a real apply) verify managed configuration."""
        profile = self._profile(plan)
        if profile is None or step.target != plan.profile_id:
            return self._failure(step, f"unknown profile {plan.profile_id!r}")
        resolver = StackModelResolver(self._installer(profile), self.catalog)
        try:
            graphs = self._graphs_for(profile, resolver)
            configuration = build_stack_configuration(
                profile,
                resolver,
                self.layout,
                workflow_specs=self.workflow_specs,
                repo_root=self.repo_root,
                graphs=graphs,
                endpoints=self.endpoints_for(profile),
            )
        except (OSError, ValueError, WorkflowError) as exc:
            return self._failure(step, f"configuration could not be generated: {exc}")
        if not configuration.ok:
            return self._failure(step, "; ".join(configuration.problems), configuration)

        try:
            applied = self.config_writer.apply(configuration.documents, dry_run=dry_run)
        except OSError as exc:
            return self._failure(step, f"managed configuration could not be written: {exc}")
        if not applied.ok:
            return self._failure(
                step,
                "managed configuration conflicts with external files",
                (configuration, applied),
            )
        if dry_run:
            return ActionExecutionResult(
                step.action,
                step.target,
                True,
                f"would write {len(applied.written)} managed configuration file(s)",
                changed=bool(applied.written),
                payload=(configuration, applied),
            )

        try:
            verified = self.config_writer.verify(configuration.documents)
        except OSError as exc:
            return self._failure(step, f"managed configuration could not be verified: {exc}")
        if not verified.ok:
            return self._failure(
                step,
                "; ".join(verified.problems),
                (configuration, applied, verified),
            )
        return ActionExecutionResult(
            step.action,
            step.target,
            True,
            f"applied and verified {len(verified.matching)} managed configuration file(s)",
            changed=bool(applied.written),
            payload=(configuration, applied, verified),
        )
