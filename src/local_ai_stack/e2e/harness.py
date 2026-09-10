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
explicitly. The root is acquired when the world is built -- before ``run`` is
even called -- so *everything* the run does, including building its own
context, happens inside the try whose finally releases it. Cleanup therefore
does not require a context to exist: it releases the world's processes and the
world's root, which is what was acquired. A run that fails while constructing
itself still cleans up. And the spec is checked
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
from ..launcher import StopOutcome
from ..model_resolver import StackModelResolver
from ..planning import build_install_plan
from ..profiles import Profile, load_profiles
from ..provisioning import ProvisionOutcome
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
from ..workflows import (
    NodeBinding,
    WorkflowParameters,
    load_workflow_graph,
    load_workflow_manifest,
    render_workflow,
    resolve_workflow,
    verify_workflow_references,
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

# The role a chat smoke exercises, and the workflow an image smoke submits.
# Both are profile vocabulary, not harness invention.
CHAT_ROLE = "chat.primary"
IMAGE_WORKFLOW = "text_to_image"

CHAT_PROMPT = "reply with one short sentence"
# Small and fixed: this proves the submission contract, not image quality.
IMAGE_PARAMETERS = WorkflowParameters(
    prompt="a controlled smoke prompt", width=512, height=512, steps=4, seed=7
)


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

        # The root exists before this method is entered, so nothing here --
        # including building the context -- may happen outside the try.
        context: RunContext | None = None
        results: list[TestPhaseResult] = []
        failure_detail = ""

        try:
            context = self._bare_context(spec)
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
            if not failure_detail and cleanup.outcome is TestPhaseOutcome.FAILED:
                # Otherwise the run reports itself failed without saying why.
                failure_detail = cleanup.detail

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
            context=self._report_context(context, spec, cleanup),
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

    def _report_context(
        self, context: RunContext | None, spec: TestRunSpec, cleanup: TestPhaseResult
    ) -> dict[str, Any]:
        """The run's context, including for a run that never built one."""
        if context is None:
            return {
                "root": self.world.root.as_report_data(),
                "provider_mode": str(spec.provider_mode),
                "capabilities": {"network": spec.allow_network, "gpu": spec.allow_gpu},
                "injection": self.injections.as_report_data(),
                "metrics": {},
                "cleanup": dict(cleanup.data),
                "endpoints": {},
            }
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
        full = phases.provisioning
        target = self.world.installable_plan(full)
        report = executor.execute(target, dry_run=False)

        installed = [item.component_id for item in report.results if item.changed]
        attempted = {item.component_id for item in report.results}
        declared = {step.target for step in full.steps if str(step.action) == "install"}
        # Named explicitly, because a run that installs one component out of
        # three must not read as a whole-stack install.
        components = {
            "installed": sorted(installed),
            "attempted": sorted(attempted),
            "declared": sorted(declared),
            "not_attempted": sorted(declared - attempted),
            "external": list(context.data.get("external_components", [])),
        }
        context.data["components"] = components
        data = {
            "components": components,
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
        detail = ", ".join(components["installed"]) or "nothing"
        if components["not_attempted"]:
            detail += f" (not attempted here: {', '.join(components['not_attempted'])})"
        return f"provisioned {detail}", data

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
            "downloads": len(self.world.artifacts.requests),
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
        """Send a generation to the chat model this profile resolved.

        A version probe proves a port answers, which the start phase already
        proved. What this row is for is the chat capability: the request goes to
        the endpoint the profile resolved, names the model the resolver said is
        available, and comes back with something.
        """
        endpoint = context.data.get("endpoints", {}).get(OLLAMA)
        if not endpoint:
            raise PhaseBlocked("chat smoke needs a resolved Ollama endpoint")

        resolver = self._resolver(context)
        chat = resolver.resolve_profile(context.require_profile()).get(CHAT_ROLE)
        if chat is None:
            raise PhaseBlocked(f"this profile declares no {CHAT_ROLE} model")
        if not chat.available:
            raise PhaseBlocked(f"the chat model is {chat.state}: {chat.detail}")
        reference = chat.require_location().reference

        with context.metrics.measure(Metric.CHAT_SMOKE, endpoint=endpoint, model=reference):
            reply = self.world.chat(endpoint, reference, CHAT_PROMPT)

        data = {
            "endpoint": reply.endpoint,
            "model": reply.model,
            "prompt": reply.prompt,
            "answered": bool(reply.content),
            "done": reply.done,
        }
        if reply.endpoint != endpoint or reply.model != reference:
            raise PhaseFailed(
                f"the chat request went to {reply.endpoint}/{reply.model}, "
                f"not {endpoint}/{reference}"
            )
        if not reply.ok:
            raise PhaseFailed("the chat route accepted the request but produced nothing")
        return f"chat answered from {reference}", data

    def _phase_image_smoke(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        """Render the declared workflow and submit it.

        The proof here is the workflow execution contract, not a health route:
        the graph the manifest declared, with the model binding filled from the
        resolved location and the parameters at their declared addresses, is
        what gets submitted. No GPU runs; the provider accepts and answers.
        """
        if not context.spec.allow_gpu:
            raise PhaseBlocked("image smoke requires the GPU capability, which was not granted")
        if not self.world.facts.gpus:
            raise PhaseBlocked("image smoke is blocked: no usable CUDA device was detected")
        endpoint = context.data.get("endpoints", {}).get(str(ComponentId.COMFYUI))
        if not endpoint:
            raise PhaseBlocked("image smoke needs a resolved ComfyUI endpoint")

        profile = context.require_profile()
        declaration = profile.workflows.get(IMAGE_WORKFLOW)
        if declaration is None:
            raise PhaseBlocked(f"this profile declares no {IMAGE_WORKFLOW!r} workflow")
        specs = load_workflow_manifest(self.repo_root / "manifests" / "workflows.yaml")
        spec = specs.get(declaration.manifest_id)
        if spec is None:
            raise PhaseBlocked(f"workflow {declaration.manifest_id!r} is not in the manifest")

        resolution = resolve_workflow(
            spec, profile, self._resolver(context), repo_root=self.repo_root
        )
        if not resolution.ready:
            raise PhaseBlocked(
                f"workflow is {resolution.state}: {'; '.join(resolution.problems)}",
                {"workflow": str(resolution.state), "problems": list(resolution.problems)},
            )
        graph = load_workflow_graph(resolution)
        mismatches = verify_workflow_references(resolution, graph)
        if mismatches:
            raise PhaseFailed(f"workflow does not match its template: {'; '.join(mismatches)}")

        rendered = render_workflow(resolution, IMAGE_PARAMETERS, graph=graph)
        with context.metrics.measure(Metric.IMAGE_SMOKE, endpoint=endpoint, workflow=spec.id):
            job = self.world.comfyui.submit(endpoint, rendered)
            result = self.world.comfyui.result(job.prompt_id)

        submitted = {
            parameter.value: _at(rendered, spec.binding(parameter)) for parameter in spec.parameters
        }
        images = [
            image
            for output in result.get("outputs", {}).values()
            for image in output.get("images", [])
        ]
        data = {
            "endpoint": endpoint,
            "workflow": spec.id,
            "prompt_id": job.prompt_id,
            "runtime_name": resolution.runtime_name,
            "submitted": submitted,
            "images": len(images),
        }
        if submitted.get("model") != resolution.runtime_name:
            raise PhaseFailed(
                f"the submitted graph names {submitted.get('model')!r} instead of the "
                f"resolved {resolution.runtime_name!r}"
            )
        if not images:
            raise PhaseFailed("the workflow was accepted but produced no result")
        return f"workflow {spec.id} submitted as {job.prompt_id}", data

    def _phase_stop(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        controller = self._controller(context)
        # What is actually there, before anything is touched. This is where a
        # crash between two observations becomes visible, and where an external
        # service shows up as external and unstoppable rather than as ours.
        observed = _snapshot(controller)
        # Looked at again, because the first read can catch a service mid
        # transition: the process list is gathered before the health probe, so a
        # process that dies during the probe is still in the earlier snapshot.
        # The second read is the one that describes a settled state.
        confirmed = _snapshot(controller)
        context.data["observed"] = observed
        context.data["confirmed"] = confirmed
        stopped: list[dict[str, Any]] = []
        for service_id in list(context.started_services):
            result = controller.stop(service_id)
            stopped.append(
                {
                    "service": service_id,
                    "outcome": _name(result.outcome),
                    "tree_pids": list(result.tree_pids),
                    "left_running": list(result.left_running),
                    "stopped_tree": result.stopped_tree,
                }
            )
            if result.stopped:
                context.started_services.remove(service_id)
        data = {
            "observed": observed,
            "confirmed": confirmed,
            "stopped": stopped,
            "registry": sorted(context.registry.load().entries) if context.registry else [],
        }
        if any(item["outcome"] == _name(StopOutcome.FAILED) for item in stopped):
            raise PhaseFailed("a managed service refused to stop")
        return f"stopped {len(stopped)} managed services", data

    def _phase_second_plan(self, context: RunContext) -> tuple[str, dict[str, Any]]:
        """Prove that what this run really installed proposes nothing further.

        A dry run never reports INSTALLED, so looking for that outcome could
        never fail. What it does report is ``dry-run`` for a component it would
        install and ``skipped`` for one that is already there, and that
        difference is the measurement.

        The claim is scoped to what this mode actually provisioned. Asking
        whether a component the run never installed would install is a real
        question, but it is not idempotence.
        """
        profile = context.require_profile()
        with context.metrics.measure(Metric.PLAN, run="second"):
            plan = build_install_plan(profile, installation=context.installation, stack=None)
        provision = provision_context(context.installation, runner=self.world.runner)
        executor = plan_executor(
            provision,
            self.world.provisioners(provision),
            actions=self.world.install_actions(provision, profile),
            options=context.options,
        )
        phases = stage_managed_plan(plan)
        report = executor.execute(self.world.installable_plan(phases.provisioning), dry_run=True)
        models = executor.execute(_only(phases.after_ollama_ready, "acquire-model"), dry_run=True)

        would_install = [
            item.component_id
            for item in report.results
            if str(item.outcome) == str(ProvisionOutcome.DRY_RUN)
        ]
        would_acquire = [item.target for item in models.action_results if item.changed]
        data = {
            "results": [
                {"component": item.component_id, "outcome": str(item.outcome)}
                for item in report.results
            ],
            "model_actions": _actions(models),
            "would_install": would_install,
            "would_acquire": would_acquire,
            "pulls": list(getattr(self.world.ollama, "pulls", [])),
            "downloads": len(self.world.artifacts.requests),
            "created_directories": [str(path) for path in report.created_directories],
        }
        proposed = would_install + would_acquire
        if proposed:
            raise PhaseFailed(
                "a second run proposes work for verified state: " + ", ".join(proposed), data
            )
        return (
            f"a second run proposes nothing for {len(report.results)} verified component(s) "
            f"and {len(models.action_results)} model(s)"
        ), data

    # --- cleanup -----------------------------------------------------------

    def _cleanup(self, context: RunContext | None, sequence: _Sequencer) -> TestPhaseResult:
        """Always runs, with or without a context.

        What has to be released belongs to the *world*: the processes it
        launched and the root it allocated. Both exist before a run starts, so
        a run that failed while building its own context still has them to
        release, and this must not depend on that context existing.
        """
        sequence.phase_state(TestPhase.CLEANUP, "running")
        started = self.world.monotonic()
        problems: list[str] = []
        released: list[str] = []
        metrics = context.metrics if context is not None else MetricRecorder(self.world.monotonic)
        root = context.root if context is not None else self.world.root

        with metrics.measure(Metric.CLEANUP):
            if context is not None and context.controller is not None:
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
                released.append(root.release())
            except RootError as exc:
                problems.append(str(exc))

        duration = max(0.0, self.world.monotonic() - started)
        report = {
            "released": released,
            "problems": problems,
            "root": root.as_report_data(),
            "external_survivors": list(self.world.external_survivors()),
            "job_handles_released": bool(
                self.world._launcher is None or self.world._launcher.closed
            ),
        }
        if context is not None:
            context.cleanup_report = report
        outcome = TestPhaseOutcome.FAILED if problems else TestPhaseOutcome.PASSED
        detail = "; ".join(problems) if problems else "no managed residual; root released"
        sequence.phase_state(TestPhase.CLEANUP, str(outcome), detail)
        return TestPhaseResult(
            phase=TestPhase.CLEANUP,
            outcome=outcome,
            detail=detail,
            duration_seconds=duration,
            data=dict(report),
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
        """One installer and one catalog per run, shared with the executor."""
        return StackModelResolver(
            installer=self.world.model_installer(context.installation.layout),
            catalog=self.world.model_catalog(),
        )


def _snapshot(controller) -> list[dict[str, Any]]:
    """One reading of every service the controller knows about."""
    return [
        {
            "service": _name(status.service_id),
            "origin": _name(status.origin),
            "state": _name(status.state),
            "runtime": _name(status.runtime.state),
            "stoppable": status.stoppable,
            "pid": status.runtime.pid,
        }
        for status in controller.statuses().values()
    ]


def _name(value: Any) -> str:
    """An enum's own value, so a report never carries a repr."""
    return str(getattr(value, "value", value))


def _at(graph: dict[str, Any], binding: NodeBinding) -> Any:
    """Read one binding back out of a rendered graph, at its declared address."""
    node = graph.get(binding.node_id)
    if not isinstance(node, dict):
        return None
    inputs = node.get("inputs")
    target = inputs if isinstance(inputs, dict) else node
    return target.get(binding.key)


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
