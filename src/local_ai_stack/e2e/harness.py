"""The synchronous test harness.

It owns scenario ordering, fixtures, injection, timing, cleanup and report
assembly. It owns none of the behaviour it exercises: every phase composes the
existing domains — ``preflight_managed_stack``, ``build_install_plan``,
``stage_managed_plan``, ``plan_executor``, ``StackModelResolver``,
``detect_integration``, ``build_service_controller`` — and reads their answers.

Two rules shape the whole file:

- **cleanup runs from a finally path.** Any mutating phase can raise; the
  managed processes and the ephemeral root are released regardless. A cleanup
  failure is reported on its own and can never turn an earlier failure into a
  pass.
- **a phase reports, it does not decide.** Ownership, verification, external
  classification and path authority stay where they already live. When a domain
  says a component is EXTERNAL, the harness records that and moves on; it has no
  opinion of its own to substitute.

Two consequences of the first rule are easy to get wrong and are handled here
explicitly. The root is acquired when the world is built, so *everything* from
spec validation onwards runs inside the try whose finally releases it; a run
that cannot even load its profile still cleans up. And the spec is checked
against the world before anything runs: a spec asking for a configured root or
local-real providers against an ephemeral, controlled world is refused rather
than quietly reinterpreted, because a report that names the wrong mode is worse
than no report.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..components import ComponentId
from ..integration import (
    IntegrationOptions,
    build_service_controller,
    detect_integration,
    plan_executor,
    preflight_managed_stack,
    provision_context,
    stage_managed_plan,
)
from ..model_catalog import model_catalog
from ..model_installer import ModelInstaller
from ..model_resolver import StackModelResolver
from ..models import ManagedModelPaths
from ..planning import build_install_plan
from ..profiles import Profile, load_profiles
from ..registry import ManagedProcessRegistry
from ..test_contracts import (
    TestEvent,
    TestEventKind,
    TestEventSink,
    TestPhase,
    TestPhaseOutcome,
    TestPhaseResult,
    TestProviderMode,
    TestRunReport,
    TestRunSpec,
    TestRunState,
)
from .metrics import Metric, MetricRecorder
from .providers import ControlledViolation
from .roots import RootError, TestRoot
from .seams import InjectionPlan
from .world import ControlledWorld

MUTATING_PHASES = frozenset(
    {
        TestPhase.PROVISION,
        TestPhase.START,
        TestPhase.ACQUIRE_MODELS,
        TestPhase.CONFIGURE,
        TestPhase.CHAT_SMOKE,
        TestPhase.IMAGE_SMOKE,
    }
)

OLLAMA = str(ComponentId.OLLAMA)


class PhaseOutcomeError(RuntimeError):
    """Base for a phase ending early while keeping its diagnostics.

    The payload matters: a blocked phase that dropped the reasons it blocked
    would leave a scenario unable to prove *why*, which is most of the value.
    """

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data: dict[str, Any] = dict(data or {})


class PhaseBlocked(PhaseOutcomeError):
    """A precondition means this phase must not run. Not a failure."""


class PhaseFailed(PhaseOutcomeError):
    """The phase ran and the application behaved wrongly."""


class SpecRefused(RuntimeError):
    """The run was never started: the spec does not match this world.

    A precondition, not a failure of the application. It is still reported as a
    failed *run*, because the caller asked for something that did not happen.
    """


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class RunContext:
    """Everything one run accumulates, shared across phases."""

    spec: TestRunSpec
    world: ControlledWorld
    root: TestRoot
    metrics: MetricRecorder
    options: IntegrationOptions
    injections: InjectionPlan
    # Bound after the spec is accepted. A context exists before that so cleanup
    # has something to release when the profile itself is the thing that failed.
    profile: Profile | None = None
    data: dict[str, Any] = field(default_factory=dict)
    controller: object | None = None
    registry: ManagedProcessRegistry | None = None
    started_services: list[str] = field(default_factory=list)
    cleanup_report: dict[str, Any] = field(default_factory=dict)

    @property
    def installation(self):
        return self.root.installation

    def require_profile(self) -> Profile:
        if self.profile is None:  # pragma: no cover - phases run after binding
            raise PhaseFailed("no profile is bound to this run")
        return self.profile


class StackTestHarness:
    """Implements the shared ``TestHarness`` protocol."""

    def __init__(
        self,
        world: ControlledWorld,
        *,
        repo_root: Path | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.world = world
        self.repo_root = Path(repo_root) if repo_root is not None else world.repo_root
        self._clock = clock

    @property
    def injections(self) -> InjectionPlan:
        """The one plan for this run: the world's.

        There is deliberately no way to give the harness a different one. A
        second plan could name an injection the seams never received, and an
        injection nobody can observe is worse than none -- it makes the report
        lie in the direction of having tested more.
        """
        return self.world.injections

    # --- protocolo ---------------------------------------------------------

    def run(
        self,
        run_id: str,
        spec: TestRunSpec,
        *,
        emit: TestEventSink | None = None,
    ) -> TestRunReport:
        sequence = _Sequencer(run_id, emit)
        started_at = _utc_now()
        sequence.run_state(TestRunState.RUNNING)

        # Built before anything can fail, because the root already exists and
        # the finally below is what releases it.
        context = self._bare_context(spec)
        results: list[TestPhaseResult] = []
        failure_detail = ""

        try:
            self._refuse_mismatch(spec)
            self._bind_profile(context)
            halted = ""
            blocked = ""
            for phase in spec.phases:
                if phase is TestPhase.CLEANUP:
                    continue  # cleanup always runs from the finally path
                # A failure stops everything. A blocked precondition stops only
                # what would mutate: the contract asks that read-only phases stay
                # available when, say, no usable CUDA device was found.
                stopped = failure_detail or (blocked and phase in MUTATING_PHASES)
                if stopped:
                    reason = failure_detail and f"{halted}" or blocked
                    results.append(
                        TestPhaseResult(
                            phase=phase,
                            outcome=TestPhaseOutcome.SKIPPED,
                            detail=f"not attempted: {reason}",
                        )
                    )
                    continue
                result = self._run_phase(phase, context, sequence)
                results.append(result)
                if result.outcome is TestPhaseOutcome.FAILED:
                    failure_detail = result.detail
                    halted = f"{phase} failed"
                elif result.outcome is TestPhaseOutcome.BLOCKED:
                    blocked = f"{phase} was blocked"

        except SpecRefused as exc:
            # Nothing ran, so nothing is a phase failure; the run is what failed.
            failure_detail = f"run refused: {exc}"
            results.extend(
                TestPhaseResult(
                    phase=phase,
                    outcome=TestPhaseOutcome.BLOCKED,
                    detail=failure_detail,
                )
                for phase in spec.phases
                if phase is not TestPhase.CLEANUP
            )
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised path
            failure_detail = f"{type(exc).__name__}: {exc}"
            results.append(
                TestPhaseResult(
                    phase=TestPhase.PREFLIGHT,
                    outcome=TestPhaseOutcome.FAILED,
                    detail=failure_detail,
                    data={"traceback": traceback.format_exc(limit=6)},
                )
            )
        finally:
            # Always. An exception above must not skip releasing processes and
            # the ephemeral root.
            cleanup = self._cleanup(context, sequence)
            results.append(cleanup)

        state = (
            TestRunState.FAILED
            if failure_detail or any(item.outcome is TestPhaseOutcome.FAILED for item in results)
            else TestRunState.PASSED
        )
        report = TestRunReport(
            run_id=run_id,
            state=state,
            spec=spec,
            phases=tuple(results),
            started_at=started_at,
            finished_at=_utc_now(),
            detail=failure_detail,
            context=self._report_context(context),
        )
        sequence.run_state(state)
        sequence.result(report)
        return report

    # --- montagem ----------------------------------------------------------

    def _bare_context(self, spec: TestRunSpec) -> RunContext:
        """Everything cleanup needs, built without touching the filesystem."""
        return RunContext(
            spec=spec,
            world=self.world,
            root=self.world.root,
            metrics=MetricRecorder(self._clock or self.world.monotonic),
            options=self.world.options,
            injections=self.injections,
        )

    def _bind_profile(self, context: RunContext) -> None:
        """Load the requested profile. Failing here still reaches cleanup."""
        profiles = {item.id: item for item in load_profiles(self.repo_root / "profiles")}
        if context.spec.profile_id not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise PhaseFailed(
                f"unknown profile {context.spec.profile_id!r}; this repository declares: {known}"
            )
        context.profile = profiles[context.spec.profile_id]

    def _refuse_mismatch(self, spec: TestRunSpec) -> None:
        """The world is the single source of truth. A spec may not contradict it.

        Normalising silently would be the worst option available: the report
        would name a mode nobody ran in.
        """
        world = self.world
        if spec.root_mode is not world.root.mode:
            raise SpecRefused(
                f"the spec asks for a {spec.root_mode} root while this world holds a "
                f"{world.root.mode} one"
            )
        if spec.provider_mode is not world.provider_mode:
            raise SpecRefused(
                f"the spec asks for {spec.provider_mode} providers while this world is "
                f"{world.provider_mode}"
            )
        if spec.provider_mode is TestProviderMode.LOCAL_REAL:
            raise SpecRefused(
                "local-real providers are not implemented; controlled providers must never "
                "be reported as local-real"
            )

    def _report_context(self, context: RunContext) -> dict[str, Any]:
        return {
            "root": context.root.as_report_data(),
            "provider_mode": str(context.spec.provider_mode),
            "capabilities": {
                "network": context.spec.allow_network,
                "gpu": context.spec.allow_gpu,
            },
            "injection": context.injections.as_report_data(),
            "metrics": context.metrics.as_report_data(),
            "cleanup": dict(context.cleanup_report),
            "platform": {
                "os": self.world.facts.os_name,
                "build": self.world.facts.windows_build,
                "python": self.world.facts.python_version,
            },
            "endpoints": dict(context.data.get("endpoints", {})),
            **{key: value for key, value in context.data.items() if key != "endpoints"},
        }

    # --- despacho ----------------------------------------------------------

    def _run_phase(
        self, phase: TestPhase, context: RunContext, sequence: _Sequencer
    ) -> TestPhaseResult:
        handler = getattr(self, f"_phase_{phase.value}", None)
        if handler is None:
            return TestPhaseResult(
                phase=phase,
                outcome=TestPhaseOutcome.SKIPPED,
                detail=f"{phase} has no handler in this harness",
            )
        sequence.phase_state(phase, "running")
        started = self.world.monotonic()
        try:
            detail, data = handler(context)
            outcome = TestPhaseOutcome.PASSED
        except PhaseBlocked as exc:
            detail, data, outcome = str(exc), exc.data, TestPhaseOutcome.BLOCKED
        except PhaseFailed as exc:
            detail, data, outcome = str(exc), exc.data, TestPhaseOutcome.FAILED
        except ControlledViolation as exc:
            detail, data, outcome = str(exc), {}, TestPhaseOutcome.FAILED
        except Exception as exc:  # noqa: BLE001 - any domain error is a phase failure
            detail = f"{type(exc).__name__}: {exc}"
            data = {"traceback": traceback.format_exc(limit=6)}
            outcome = TestPhaseOutcome.FAILED
        duration = max(0.0, self.world.monotonic() - started)
        sequence.phase_state(phase, str(outcome), detail)
        return TestPhaseResult(
            phase=phase,
            outcome=outcome,
            detail=detail,
            duration_seconds=duration,
            data=data,
        )

    # --- fases -------------------------------------------------------------

    def _phase_preflight(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        """Read-only. Detects facts and proves the root may be mutated."""
        with context.metrics.measure(Metric.DOCTOR, phase="preflight"):
            facts = self.world.facts
        provision = provision_context(context.installation, runner=self.world.runner)
        report = preflight_managed_stack(
            provision,
            context.require_profile(),
            context.options,
            provisioners=self.world.provisioners(provision),
            facts=facts,
            listener_report=self.world.inventory.list_listeners(),
        )
        # Canonical component ids, so the report joins to every other layer.
        context.data["endpoints"] = {
            str(ComponentId.OLLAMA): report.endpoints.ollama,
            str(ComponentId.OPEN_WEBUI): report.endpoints.open_webui,
            str(ComponentId.COMFYUI): report.endpoints.comfyui,
        }
        context.data["external_components"] = list(report.external_components)
        data: dict[str, Any] = {
            "ok": report.ok,
            "issues": [
                {"code": str(issue.code), "detail": issue.detail} for issue in report.issues
            ],
            "external_components": list(report.external_components),
            "free_bytes": report.free_bytes,
            "cuda_available": bool(facts.gpus),
        }
        if not report.ok:
            codes = ", ".join(str(issue.code) for issue in report.issues)
            raise PhaseBlocked(f"preflight refused before any mutation: {codes}", data)
        return "preflight proved the managed root is usable", data

    def _phase_plan(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        """Build the dry-run plan. Nothing is executed."""
        with context.metrics.measure(Metric.PLAN, profile=context.require_profile().id):
            plan = build_install_plan(
                context.require_profile(),
                installation=context.installation,
                stack=None,
            )
            phases = stage_managed_plan(plan)
        context.data["plan"] = {
            "steps": [
                {"sequence": step.sequence, "action": str(step.action), "target": step.target}
                for step in plan.steps
            ],
            "provisioning_steps": len(phases.provisioning.steps),
            "post_start_steps": len(phases.after_ollama_ready.steps),
        }
        context.data["_plan_object"] = plan
        context.data["_plan_phases"] = phases
        return (
            f"plan built with {len(plan.steps)} steps, "
            f"{len(phases.provisioning.steps)} before managed start"
        ), dict(context.data["plan"])

    def _phase_provision(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        phases = context.data.get("_plan_phases")
        if phases is None:
            raise PhaseBlocked("provision needs a plan; run the plan phase first")
        provision = provision_context(context.installation, runner=self.world.runner)
        executor = plan_executor(
            provision,
            self.world.provisioners(provision),
            actions=self.world.install_actions(provision, context.require_profile()),
            options=context.options,
        )
        target = self.world.installable_plan(phases.provisioning)
        report = executor.execute(target, dry_run=False)
        data = {
            "installable_components": sorted(self.world.install_components),
            "created_directories": [str(path) for path in report.created_directories],
            "results": [
                {"component": item.component_id, "outcome": str(item.outcome)}
                for item in report.results
            ],
            "unsupported": [entry.step.target for entry in report.unsupported],
        }
        if not report.ok:
            failed = ", ".join(item.component_id for item in report.failures)
            raise PhaseFailed(f"provisioning failed for: {failed}")
        return f"provisioned {len(report.results)} components", data

    def _phase_start(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        controller = self._controller(context)
        with context.metrics.measure(Metric.SERVICE_STARTUP, service=OLLAMA):
            # wait=True is how the controller's own readiness layer is used; the
            # harness never re-implements readiness.
            report = controller.start(OLLAMA, wait=True)
        data = {
            "service": OLLAMA,
            "outcome": str(report.outcome),
            "pid": report.pid,
            "ready": report.ready,
            "detail": report.detail,
        }
        if report.pid is not None:
            context.started_services.append(OLLAMA)
        if not report.ready:
            raise PhaseFailed(f"managed {OLLAMA} did not become ready: {report.detail}")
        return f"managed {OLLAMA} started and ready", data

    def _phase_acquire_models(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        phases = context.data.get("_plan_phases")
        if phases is None:
            raise PhaseBlocked("acquire_models needs a plan")
        provision = provision_context(context.installation, runner=self.world.runner)
        executor = plan_executor(
            provision,
            self.world.provisioners(provision),
            actions=self.world.install_actions(provision, context.require_profile()),
            options=context.options,
        )
        acquire = _only(phases.after_ollama_ready, "acquire-model")
        with context.metrics.measure(Metric.MODEL_VERIFY, steps=len(acquire.steps)):
            report = executor.execute(acquire, dry_run=False)
        data = {
            "actions": _actions(report),
            "pulls": list(getattr(self.world.ollama, "pulls", [])),
        }
        if not report.ok:
            raise PhaseFailed(f"model acquisition failed: {_why(report)}", data)
        return f"executed {len(report.action_results)} model actions", data

    def _phase_configure(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        resolver = self._resolver(context)
        with context.metrics.measure(Metric.MODEL_RESOLVE, profile=context.require_profile().id):
            resolved = resolver.resolve_profile(context.require_profile())
        roles = {
            role: {
                "state": str(model.state),
                "has_location": model.location is not None,
                "id": str(model.id),
            }
            for role, model in resolved.items()
        }
        context.data["resolved_roles"] = roles
        missing = [role for role, model in resolved.items() if not model.available]
        if missing:
            raise PhaseBlocked(
                "configuration is blocked: these roles are not available: " + ", ".join(missing),
                {"roles": roles, "missing": missing},
            )
        phases = context.data.get("_plan_phases")
        if phases is None:
            raise PhaseBlocked("configure needs a plan")
        provision = provision_context(context.installation, runner=self.world.runner)
        executor = plan_executor(
            provision,
            self.world.provisioners(provision),
            actions=self.world.install_actions(provision, context.require_profile()),
            options=context.options,
        )
        configure = _only(phases.after_ollama_ready, "apply-workflow", "configure")
        report = executor.execute(configure, dry_run=False)
        data = {"roles": roles, "actions": _actions(report)}
        if not report.ok:
            raise PhaseFailed(f"managed configuration failed: {_why(report)}", data)
        return "managed configuration applied and verified", data

    def _phase_chat_smoke(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        endpoints = context.data.get("endpoints", {})
        endpoint = endpoints.get("ollama")
        if not endpoint:
            raise PhaseBlocked("chat smoke needs a resolved Ollama endpoint")
        with context.metrics.measure(Metric.CHAT_SMOKE, endpoint=endpoint):
            probe = self.world.endpoints.http(f"{endpoint}/api/version", 5.0)
        data = {"endpoint": endpoint, "outcome": str(probe.outcome), "status": probe.status_code}
        if not probe.ok:
            raise PhaseFailed(f"chat route did not answer: {probe.detail}")
        return "chat route answered", data

    def _phase_image_smoke(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        if not context.spec.allow_gpu:
            raise PhaseBlocked("image smoke requires the GPU capability, which was not granted")
        if not self.world.facts.gpus:
            raise PhaseBlocked("image smoke is blocked: no usable CUDA device was detected")
        endpoints = context.data.get("endpoints", {})
        endpoint = endpoints.get("comfyui")
        if not endpoint:
            raise PhaseBlocked("image smoke needs a resolved ComfyUI endpoint")
        with context.metrics.measure(Metric.IMAGE_SMOKE, endpoint=endpoint):
            probe = self.world.endpoints.http(f"{endpoint}/system_stats", 5.0)
        data = {"endpoint": endpoint, "outcome": str(probe.outcome)}
        if not probe.ok:
            raise PhaseFailed(f"image route did not answer: {probe.detail}")
        return "image route answered", data

    def _phase_stop(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        controller = self._controller(context)
        stopped: list[dict[str, Any]] = []
        for service_id in list(context.started_services):
            result = controller.stop(service_id)
            stopped.append(
                {
                    "service": service_id,
                    "outcome": str(result.outcome),
                    "tree_pids": list(result.tree_pids),
                }
            )
            if result.stopped:
                context.started_services.remove(service_id)
        data = {"stopped": stopped}
        if any(item["outcome"] == "failed" for item in stopped):
            raise PhaseFailed("a managed service refused to stop")
        return f"stopped {len(stopped)} managed services", data

    def _phase_second_plan(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        """Prove verified current state proposes no new mutation."""
        with context.metrics.measure(Metric.PLAN, run="second"):
            plan = build_install_plan(
                context.require_profile(), installation=context.installation, stack=None
            )
        provision = provision_context(context.installation, runner=self.world.runner)
        executor = plan_executor(
            provision,
            self.world.provisioners(provision),
            actions=self.world.install_actions(provision, context.require_profile()),
            options=context.options,
        )
        report = executor.execute(plan, dry_run=True)
        changed = [item.component_id for item in report.results if str(item.outcome) == "installed"]
        downloads = list(getattr(self.world.ollama, "pulls", []))
        data = {
            "results": [
                {"component": item.component_id, "outcome": str(item.outcome)}
                for item in report.results
            ],
            "would_change": changed,
            "pulls_so_far": downloads,
            "created_directories": [str(path) for path in report.created_directories],
        }
        if changed:
            raise PhaseFailed(
                "a second plan proposes to reinstall verified components: " + ", ".join(changed)
            )
        return "second plan proposes no mutation for verified state", data

    # --- cleanup -----------------------------------------------------------

    def _cleanup(self, context: RunContext, sequence: _Sequencer) -> TestPhaseResult:
        """Always runs. Reports its own outcome, independent of earlier phases."""
        sequence.phase_state(TestPhase.CLEANUP, "running")
        started = self.world.monotonic()
        problems: list[str] = []
        released: list[str] = []

        with context.metrics.measure(Metric.CLEANUP):
            if context.controller is not None:
                for service_id in list(context.started_services):
                    try:
                        result = context.controller.stop(service_id)
                        released.append(f"{service_id}:{result.outcome}")
                        if result.stopped:
                            context.started_services.remove(service_id)
                        else:
                            problems.append(f"{service_id} did not stop: {result.detail}")
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"{service_id} stop raised {type(exc).__name__}: {exc}")

            try:
                self.world.release_processes()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"launcher release failed: {type(exc).__name__}: {exc}")

            residual = self.world.managed_residual()
            if residual:
                problems.append("managed processes still present: " + ", ".join(map(str, residual)))

            try:
                detail = context.root.release()
                released.append(detail)
            except RootError as exc:
                problems.append(str(exc))

        duration = max(0.0, self.world.monotonic() - started)
        context.cleanup_report = {
            "released": released,
            "problems": problems,
            "root": context.root.as_report_data(),
            "external_survivors": list(self.world.external_survivors()),
        }
        outcome = TestPhaseOutcome.FAILED if problems else TestPhaseOutcome.PASSED
        detail = "; ".join(problems) if problems else "no managed residual; root released"
        sequence.phase_state(TestPhase.CLEANUP, str(outcome), detail)
        return TestPhaseResult(
            phase=TestPhase.CLEANUP,
            outcome=outcome,
            detail=detail,
            duration_seconds=duration,
            data=dict(context.cleanup_report),
        )

    # --- composição --------------------------------------------------------

    def _controller(self, context: RunContext):
        if context.controller is not None:
            return context.controller
        provision = provision_context(context.installation, runner=self.world.runner)
        context.installation.layout.ensure()
        registry = ManagedProcessRegistry.from_layout(context.installation.layout)
        context.registry = registry
        snapshot = detect_integration(provision, self.world.provisioners(provision))
        controller = build_service_controller(
            snapshot,
            registry,
            layout=context.installation.layout,
            launcher=self.world.launcher(registry),
            endpoints=context.options.endpoints_for(context.require_profile()),
            http=self.world.endpoints.http,
            tcp=self.world.endpoints.tcp,
            list_processes=self.world.inventory.list_processes,
            list_listeners_fn=self.world.inventory.list_listeners,
            lookup_process=self.world.inventory.lookup_process,
            monotonic=self.world.monotonic,
            sleep=self.world.sleep,
        )
        context.controller = controller
        return controller

    def _resolver(self, context: RunContext) -> StackModelResolver:
        installer = ModelInstaller(
            ManagedModelPaths.from_layout(context.installation.layout),
            downloader=self.world.downloader(),
            ollama=self.world.ollama,
        )
        return StackModelResolver(installer=installer, catalog=model_catalog())


def _actions(report) -> list[dict[str, Any]]:
    """Each executed action as the executor reported it, verbatim."""
    return [
        {
            "action": str(item.action),
            "target": item.target,
            "ok": item.ok,
            "detail": item.detail,
        }
        for item in report.action_results
    ]


def _why(report) -> str:
    """The domain's own reason for a failed execution, not a paraphrase.

    An execution is ``ok`` only when the provisioning results and the action
    results all are, so both are read here; a phase must never fail with a
    reason nobody gave.
    """
    reasons = [f"{item.component_id}: {item.detail}" for item in report.results if not item.ok]
    reasons += [
        f"{item.action}/{item.target}: {item.detail}"
        for item in report.action_results
        if not item.ok
    ]
    return "; ".join(reasons) if reasons else "the executor reported no reason"


def _only(plan, *actions: str):
    """A plan narrowed to some actions, preserving order and identity."""
    from ..planning import InstallPlan

    wanted = {value.lower() for value in actions}
    return InstallPlan(
        profile_id=plan.profile_id,
        steps=tuple(step for step in plan.steps if str(step.action) in wanted),
        stack_id=plan.stack_id,
        install_mode=plan.install_mode,
        install_root=plan.install_root,
    )


class _Sequencer:
    """Emits contract events with a monotonic sequence starting at 1."""

    def __init__(self, run_id: str, emit: TestEventSink | None) -> None:
        self.run_id = run_id
        self._emit = emit
        self._next = 0

    def _send(self, kind: TestEventKind, **kwargs: Any) -> None:
        if self._emit is None:
            return
        self._next += 1
        self._emit(
            TestEvent(
                run_id=self.run_id,
                sequence=self._next,
                kind=kind,
                timestamp=_utc_now(),
                **kwargs,
            )
        )

    def run_state(self, state: TestRunState) -> None:
        self._send(TestEventKind.RUN_STATE, message=str(state), data={"state": str(state)})

    def phase_state(self, phase: TestPhase, state: str, detail: str = "") -> None:
        self._send(
            TestEventKind.PHASE_STATE,
            phase=phase,
            message=detail or state,
            data={"state": state},
        )

    def result(self, report: TestRunReport) -> None:
        self._send(
            TestEventKind.RESULT,
            message=str(report.state),
            data={"run_id": report.run_id, "state": str(report.state)},
        )


def new_run_id() -> str:
    """An opaque run id. Nothing about the machine is encoded in it."""
    return uuid.uuid4().hex


__all__ = [
    "MUTATING_PHASES",
    "PhaseBlocked",
    "PhaseFailed",
    "RunContext",
    "StackTestHarness",
    "new_run_id",
]
