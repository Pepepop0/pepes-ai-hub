"""The first-run coordinator: from a selected profile to a running stack.

This is the sequence behind the "Instalar" button:

    verify the environment → plan → install each component → start Ollama →
    acquire the models → apply configuration → start the other services →
    ready

It orchestrates and reports. Every decision -- is this component external, is
that file the right bytes, is the configuration ours, is the service up -- is
made by the domain that owns it; the coordinator asks in order, stops at the
first refusal, and turns each answer into an operation event a UI can show.

Failure is a result, not an exception. Whatever refuses, the operation ends
with exactly one FAILED result carrying safe diagnostics, in the phase where
it stopped. Nothing is emitted after that, by construction: the stream is
validated by the frozen reducer before any observer sees an event.

Running it again on a finished installation proposes nothing: provisioning
reports each component already present, acquisition reports each model
already available, configuration reports no change, the services are already
managed. It ends in the same place -- "Tudo pronto" -- having done nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .components import ComponentId
from .download import DownloadProgress
from .execution import ExecutionReport
from .operation_contracts import (
    OperationActivity,
    OperationActivityCategory,
    OperationDiagnostic,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationState,
)
from .operations import OperationStream, diagnostic, safe_text
from .planning import InstallPlan, PlanAction, PlanStep
from .profiles import Profile
from .provisioning import ProvisionEvent
from .services import ServiceState
from .transfer_progress import TransferSampler

if TYPE_CHECKING:
    from .stack_application import StackApplication

OLLAMA = str(ComponentId.OLLAMA)

_DISPLAY = {
    str(ComponentId.OLLAMA): "Ollama",
    str(ComponentId.OPEN_WEBUI): "Open WebUI",
    str(ComponentId.COMFYUI): "ComfyUI",
}


class FirstRunStopped(Exception):
    """Raised inside a step to end the run with a FAILED result."""

    def __init__(
        self,
        phase: OperationPhase,
        message: str,
        diagnostics: list[OperationDiagnostic],
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.message = message
        self.diagnostics = diagnostics


@dataclass
class FirstRunReport:
    """What happened, in more detail than an event can carry.

    Events are bounded and safe; this is where the full domain text lives so a
    log, or a UI drill-down, can show it. It never replaces the result.
    """

    profile_id: str
    installed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    acquired: list[str] = field(default_factory=list)
    already_available: list[str] = field(default_factory=list)
    configured: bool = False
    configuration_changed: bool = False
    started: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    unmapped_workflows: list[str] = field(default_factory=list)
    warnings: list[OperationDiagnostic] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    result: OperationResult | None = None

    @property
    def did_nothing(self) -> bool:
        """True when a run was a no-op: the definition of idempotence here."""
        return (
            not self.installed
            and not self.acquired
            and not self.configuration_changed
            and not self.started
        )


class FirstRunCoordinator:
    """Runs the sequence once, on one stream, for one profile."""

    def __init__(self, app: StackApplication, profile: Profile, stream: OperationStream) -> None:
        self.app = app
        self.profile = profile
        self.stream = stream
        self.report = FirstRunReport(profile_id=profile.id)

    def run(self) -> OperationResult:
        stream = self.stream
        try:
            stream.begin("Verificando ambiente")
            self._verify_environment()
            stream.advance(
                OperationState.PREPARING, OperationPhase.PREPARATION, "Planejando instalação"
            )
            phases = self.app.stage(self.app.build_plan(self.profile))
            self._provision(phases.provisioning)
            self._start(OLLAMA, state=OperationState.RUNNING)
            self._acquire_models(phases.after_ollama_ready)
            self._configure(phases.after_ollama_ready)
            self._start_the_rest()
        except FirstRunStopped as stop:
            self.report.problems.extend(item.message for item in stop.diagnostics)
            self.report.result = stream.fail(stop.phase, stop.message, stop.diagnostics)
            return self.report.result
        message = "Tudo pronto" if not self.report.did_nothing else "Tudo pronto (nada a fazer)"
        self.report.result = stream.succeed(
            OperationPhase.FINALIZATION,
            message,
            output_references=tuple(self.report.started),
            diagnostics=self.report.warnings,
        )
        return self.report.result

    # --- passos ------------------------------------------------------------

    def _verify_environment(self) -> None:
        """Preflight: compatibility, external installs, ports, resolvable models."""
        preflight = self.app.preflight(self.profile)
        self.report.external = list(preflight.external_components)
        if not preflight.ok:
            raise FirstRunStopped(
                OperationPhase.PREPARATION,
                "Ambiente não está pronto",
                [
                    diagnostic(f"preflight-{issue.code.value}", issue.detail)
                    for issue in preflight.issues
                ],
            )
        incoherent = self.app.capability_problems(self.profile)
        if incoherent:
            # An enabled capability with nothing to configure it with fails at
            # "Aplicando configurações" -- after every component is installed.
            # Nothing about installing changes that, so it is refused here.
            raise FirstRunStopped(
                OperationPhase.PREPARATION,
                "O perfil habilita recursos que não consegue configurar",
                [diagnostic("profile-capability", problem) for problem in incoherent],
            )
        unresolved = self.app.unresolved_models(self.profile)
        if unresolved:
            # A profile that names a model nothing can install is not installable.
            # Better to say so here than after provisioning three components.
            raise FirstRunStopped(
                OperationPhase.PREPARATION,
                "O perfil declara modelos que não podem ser instalados",
                [
                    diagnostic("profile-unresolved-model", f"{role}: {reason}")
                    for role, reason in sorted(unresolved.items())
                ],
            )

    def _on_provision_event(self, event: ProvisionEvent) -> None:
        """Bridge one provisioner note onto the stream, without touching the TUI.

        ``event.activity`` absent means legacy plain text with nothing typed to
        publish -- it still reaches the log through :meth:`ProvisionContext.emit`,
        just not the stream. Present, it is ongoing work (:meth:`activity`,
        replacing whatever was published before) or a milestone
        (:meth:`detail`, appended to the bounded timeline instead).
        """
        if event.activity is None:
            return
        if event.milestone:
            self.stream.detail(event.activity, event.message)
        else:
            self.stream.activity(event.activity, event.message, transfer=event.transfer)

    def _provision(self, plan: InstallPlan) -> None:
        """Layout first, then each component on its own so a UI sees which one."""
        self.app._activity_sink = self._on_provision_event
        try:
            executor = self.app.executor(self.profile)
            layout_steps = _only(plan, lambda step: step.action is PlanAction.PREPARE_LAYOUT)
            if layout_steps.steps:
                self.stream.advance(
                    OperationState.RUNNING, OperationPhase.EXECUTION, "Preparando diretórios"
                )
                self._execute(executor, layout_steps, "Falha ao preparar diretórios")

            installs = [step for step in plan.steps if step.action is PlanAction.INSTALL]
            for index, step in enumerate(installs, start=1):
                name = _DISPLAY.get(step.target, step.target)
                self.stream.advance(
                    OperationState.RUNNING, OperationPhase.EXECUTION, f"Instalando {name}"
                )
                self.stream.progress(index - 1, len(installs), f"Instalando {name}")
                report = self._execute(
                    executor,
                    _only(plan, lambda item, target=step.target: item.target == target),
                    f"Falha ao instalar {name}",
                )
                for result in report.results:
                    (self.report.installed if result.changed else self.report.skipped).append(
                        result.component_id
                    )
        finally:
            self.app._activity_sink = None
        if installs:
            self.stream.progress(len(installs), len(installs), "Componentes instalados")

    def _start(self, service_id: str, *, state: OperationState) -> None:
        name = _DISPLAY.get(service_id, service_id)
        controller = self.app._controller(self.profile)
        current = controller.status(service_id)
        if current.health.state is ServiceState.UP:
            return
        component = self.app._integration().by_service(service_id)
        external = current.origin.value == "external" or (
            component is not None
            and str(getattr(component.availability, "value", "")) == "external"
        )
        if external:
            raise FirstRunStopped(
                OperationPhase.SERVICE_START,
                f"{name} externo não está respondendo",
                [
                    diagnostic(
                        "external-not-ready",
                        f"{service_id} is external and not up; the hub never starts it",
                    )
                ],
            )
        self.stream.advance(state, OperationPhase.SERVICE_START, f"Iniciando {name}")
        self.stream.activity(
            OperationActivity(OperationActivityCategory.SERVICE, "starting", name),
            f"Iniciando {name}",
        )
        self.stream.activity(
            OperationActivity(OperationActivityCategory.READINESS, "waiting", name),
            f"Aguardando {name} responder",
        )
        report = controller.start(service_id, wait=True)
        if not report.ready:
            raise FirstRunStopped(
                OperationPhase.SERVICE_START,
                f"{name} não ficou pronto",
                [diagnostic("service-not-ready", f"{service_id}: {report.detail}")],
            )
        self.stream.detail(
            OperationActivity(OperationActivityCategory.READINESS, "ready", name), f"{name} pronto"
        )
        self.report.started.append(service_id)

    def _acquire_models(self, plan: InstallPlan) -> None:
        steps = [step for step in plan.steps if step.action is PlanAction.ACQUIRE_MODEL]
        if not steps:
            return
        self.stream.advance(OperationState.RUNNING, OperationPhase.EXECUTION, "Baixando modelos")
        for index, step in enumerate(steps, start=1):
            logical = OperationProgress(index - 1, len(steps))
            self.stream.progress(
                logical.completed,
                logical.total,
                "Baixando modelos",
            )
            # A model already on disk is checked before anything is fetched.
            # When no verification record covers it that is a full read of the
            # file, so the panel says so instead of standing still.
            self.stream.activity(
                OperationActivity(OperationActivityCategory.MODEL, "verifying", step.target),
                f"Verificando {step.target}",
                progress=logical,
            )
            report = self._acquire_one(plan, step, logical)
            for action in report.action_results:
                (self.report.acquired if action.changed else self.report.already_available).append(
                    action.target
                )
        self.stream.progress(len(steps), len(steps), "Modelos prontos")

    def _acquire_one(
        self, plan: InstallPlan, step: PlanStep, logical: OperationProgress
    ) -> ExecutionReport:
        """Run one ACQUIRE_MODEL step with the transfer sink wired for its duration.

        The executor is rebuilt here, after the sink is set, because
        :meth:`StackApplication.install_actions` binds ``on_transfer`` at
        construction time -- a sink set after the executor exists would never
        reach the handler. A known byte total may reach 100% before checksum
        verification and can therefore precede a failed result. ``finish``
        supplies an otherwise throttled last observation only after success.
        """
        sampler = TransferSampler(monotonic=self.app.deps.monotonic)
        last_raw: DownloadProgress | None = None

        def on_transfer(target: str, raw: DownloadProgress) -> None:
            nonlocal last_raw
            last_raw = raw
            update = sampler.sample(raw.downloaded_bytes, raw.total_bytes)
            if update is not None:
                self.stream.transfer(
                    update,
                    f"Baixando {target}",
                    progress=logical,
                    activity=OperationActivity(
                        OperationActivityCategory.MODEL, "downloading", target
                    ),
                )

        self.app._transfer_sink = on_transfer
        try:
            executor = self.app.executor(self.profile)
            report = self._execute(
                executor,
                _only(
                    plan,
                    lambda item, target=step.target: (
                        item.target == target and item.action is PlanAction.ACQUIRE_MODEL
                    ),
                ),
                "Falha ao baixar modelos",
            )
        finally:
            self.app._transfer_sink = None
        if last_raw is not None:
            final = sampler.finish(last_raw.downloaded_bytes, last_raw.total_bytes)
            if final is not None:
                self.stream.transfer(
                    final,
                    f"Baixando {step.target}",
                    progress=logical,
                    activity=OperationActivity(
                        OperationActivityCategory.MODEL, "downloading", step.target
                    ),
                )
        # A step this run did not touch (dry run aside, unreachable here) never
        # reaches this point with a false milestone: `_execute` above already
        # raised for a failure, so every action here really did conclude.
        for action in report.action_results:
            if action.changed:
                self.stream.detail(
                    OperationActivity(OperationActivityCategory.MODEL, "verified", action.target),
                    f"Verificando checksum: {action.target}",
                )
                self.stream.detail(
                    OperationActivity(OperationActivityCategory.MODEL, "installed", action.target),
                    f"Modelo instalado: {action.target}",
                )
            else:
                self.stream.detail(
                    OperationActivity(OperationActivityCategory.MODEL, "available", action.target),
                    f"Modelo já disponível: {action.target}",
                )
        return report

    def _configure(self, plan: InstallPlan) -> None:
        """Materialise each workflow, then generate and verify the configuration.

        A workflow the manifest itself declares as not yet mapped (its bindings
        are still ``TBD``) is not something this run can make ready, and the
        manifest says so. It is skipped and reported on the result rather than
        failing the install of everything else; the profile's text-to-image
        workflow is what configuration depends on, and that one must succeed.
        """
        executor = self.app.executor(self.profile)
        handlers = self.app.install_actions(self.profile)
        workflows = [step for step in plan.steps if step.action is PlanAction.APPLY_WORKFLOW]
        configure = _only(plan, lambda step: step.action is PlanAction.CONFIGURE)
        if not workflows and not configure.steps:
            return
        self.stream.advance(
            OperationState.RUNNING, OperationPhase.EXECUTION, "Aplicando configurações"
        )
        for step in workflows:
            declaration = self.profile.workflows.get(step.target)
            spec = handlers.workflow_specs.get(declaration.manifest_id) if declaration else None
            if spec is not None and not spec.complete:
                self.report.unmapped_workflows.append(step.target)
                self.report.warnings.append(
                    diagnostic(
                        "workflow-not-mapped",
                        f"{step.target}: workflow {spec.id!r} is not mapped yet "
                        f"({', '.join(spec.unresolved)}); skipped",
                    )
                )
                continue
            self.stream.activity(
                OperationActivity(OperationActivityCategory.WORKFLOW, "applying", step.target),
                f"Aplicando workflow: {step.target}",
            )
            self._execute(
                executor,
                _only(
                    plan,
                    lambda item, target=step.target: (
                        item.action is PlanAction.APPLY_WORKFLOW and item.target == target
                    ),
                ),
                "Falha ao aplicar configurações",
            )
            self.stream.detail(
                OperationActivity(OperationActivityCategory.WORKFLOW, "applied", step.target),
                f"Workflow aplicado: {step.target}",
            )
        if configure.steps:
            self.stream.activity(
                OperationActivity(
                    OperationActivityCategory.CONFIGURATION, "configuring", self.profile.id
                ),
                "Configurando endpoints e caminhos de modelo",
            )
            report = self._execute(executor, configure, "Falha ao aplicar configurações")
            self.report.configured = True
            self.report.configuration_changed = any(
                action.changed for action in report.action_results
            )
            self.stream.detail(
                OperationActivity(
                    OperationActivityCategory.CONFIGURATION, "verified", self.profile.id
                ),
                "Configuração verificada",
            )

    def _start_the_rest(self) -> None:
        self.stream.advance(
            OperationState.FINALIZING, OperationPhase.SERVICE_START, "Inicializando serviços"
        )
        endpoints = self.app.options.endpoints_for(self.profile)
        for service_id in (str(ComponentId.OPEN_WEBUI), str(ComponentId.COMFYUI)):
            if not endpoints.enabled(self.profile, service_id):
                continue
            self._start(service_id, state=OperationState.FINALIZING)

    # --- helpers -----------------------------------------------------------

    def _execute(self, executor, plan: InstallPlan, message: str) -> ExecutionReport:
        report = executor.execute(plan, dry_run=False)
        # A step the executor does not run (a CHECK, say) is reported, never
        # silently dropped.
        self.report.problems.extend(
            f"unsupported step: {entry.step.target}" for entry in report.unsupported
        )
        if not report.ok:
            from .stack_application import execution_problems

            problems = execution_problems(report)
            self.report.problems.extend(problems)
            raise FirstRunStopped(
                OperationPhase.EXECUTION,
                message,
                [diagnostic(_code(problem), problem) for problem in problems],
            )
        return report


def _only(plan: InstallPlan, keep: Callable[[PlanStep], bool]) -> InstallPlan:
    """The same plan, narrowed to the steps a predicate keeps."""
    return InstallPlan(
        profile_id=plan.profile_id,
        steps=tuple(step for step in plan.steps if keep(step)),
        stack_id=plan.stack_id,
        install_mode=plan.install_mode,
        install_root=plan.install_root,
    )


def _code(problem: str) -> str:
    """A diagnostic code from the component or action that failed."""
    head = problem.split(":", 1)[0].strip().replace("/", "-")
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in head).strip("-")
    return (cleaned or "execution")[:64]


__all__ = [
    "FirstRunCoordinator",
    "FirstRunReport",
    "FirstRunStopped",
    "safe_text",
]
