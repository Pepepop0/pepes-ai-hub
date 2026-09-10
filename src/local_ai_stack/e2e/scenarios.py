"""The P0 scenario matrix and the runner that executes it.

A scenario is a world plus a spec plus the invariants that must hold afterwards.
The runner executes one, collects the harness report, and then checks the
invariants *independently* — the run saying "passed" is never the proof on its
own, because a scenario about cleanup or about an external process has to be
verified from outside the run.

Every result carries outcome, the phase it ended on, diagnostics, cleanup status
and metrics, so a failure is actionable without re-reading the code.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..managed_config import Ownership
from ..models import ManagedModelPaths
from ..ollama_models import OllamaModelEntry
from ..test_contracts import (
    TestPhase,
    TestPhaseOutcome,
    TestRootMode,
    TestRunReport,
    TestRunSpec,
    TestRunState,
)
from .fixtures import BROKEN_WORKFLOW_PROFILE, CHAT_ONLY_PROFILE, repo_fixture
from .harness import StackTestHarness, new_run_id
from .seams import Injection, InjectionPlan
from .world import ControlledWorld, controlled_world

READ_ONLY_PHASES = (TestPhase.PREFLIGHT, TestPhase.PLAN)
INSTALL_PHASES = (TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION)
FULL_PHASES = (
    TestPhase.PREFLIGHT,
    TestPhase.PLAN,
    TestPhase.PROVISION,
    TestPhase.START,
    TestPhase.ACQUIRE_MODELS,
    TestPhase.CONFIGURE,
    TestPhase.CHAT_SMOKE,
    TestPhase.STOP,
    TestPhase.SECOND_PLAN,
)

MANAGED_OLLAMA_PORT = 21434
MANAGED_COMFYUI_PORT = 28188


class ScenarioOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class ScenarioSetup:
    """What a scenario needs before the harness runs, and how to undo it.

    ``teardown`` covers fixtures that live outside the managed root — a
    repository fixture, for instance. The root has its own release path and
    is not this hook's business.
    """

    world: ControlledWorld
    spec: TestRunSpec
    note: str = ""
    teardown: Callable[[], None] | None = None


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    title: str
    outcome: ScenarioOutcome
    phase: TestPhase | None
    diagnostics: tuple[str, ...] = ()
    cleanup_status: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    report: TestRunReport | None = field(default=None, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        return self.outcome is ScenarioOutcome.PASSED

    def as_report_data(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario_id,
            "title": self.title,
            "outcome": str(self.outcome),
            "phase": str(self.phase) if self.phase else None,
            "diagnostics": list(self.diagnostics),
            "cleanup": self.cleanup_status,
            "metrics": self.metrics,
        }


Verifier = Callable[[TestRunReport, ControlledWorld], list[str]]


@dataclass(frozen=True)
class Scenario:
    """One P0 row: how to set it up, and what must be true afterwards."""

    id: str
    title: str
    setup: Callable[[], ScenarioSetup]
    verify: Verifier
    mutating: bool = True


# --- helpers ---------------------------------------------------------------


def _phase(report: TestRunReport, phase: TestPhase):
    return next((item for item in report.phases if item.phase is phase), None)


def _last_phase(report: TestRunReport) -> TestPhase | None:
    for item in reversed(report.phases):
        if item.phase is not TestPhase.CLEANUP:
            return item.phase
    return None


def _cleanup_problems(report: TestRunReport) -> list[str]:
    cleanup = _phase(report, TestPhase.CLEANUP)
    if cleanup is None:
        return ["cleanup never ran"]
    if cleanup.outcome is TestPhaseOutcome.FAILED:
        return [f"cleanup failed: {cleanup.detail}"]
    return []


def _no_residual(world: ControlledWorld) -> list[str]:
    """The invariant every mutating scenario shares."""
    problems: list[str] = []
    residual = world.managed_residual()
    if residual:
        problems.append(f"managed processes survived cleanup: {residual}")
    lost = world.external_lost()
    if lost:
        problems.append(f"pre-existing external processes were destroyed: {lost}")
    if world.root.removable and world.root.exists():
        problems.append(f"ephemeral root survived cleanup: {world.root.path}")
    return problems


def _ollama_entry(reference: str = "hermes3:8b") -> OllamaModelEntry:
    return OllamaModelEntry(name=reference, digest="4f6b83f30b62aaaa", size_bytes=1)


def _spec(
    *phases: TestPhase, gpu: bool = False, network: bool = False, profile: str = "pepe"
) -> TestRunSpec:
    return TestRunSpec(
        profile_id=profile,
        phases=tuple(phases),
        root_mode=TestRootMode.EPHEMERAL,
        allow_network=network,
        allow_gpu=gpu,
    )


# --- P0 scenarios ----------------------------------------------------------


def _clean_root() -> ScenarioSetup:
    world = controlled_world(ready_ports=[MANAGED_OLLAMA_PORT])
    return ScenarioSetup(world, _spec(*INSTALL_PHASES), "empty ephemeral root")


def _verify_clean_root(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    plan = _phase(report, TestPhase.PLAN)
    if plan is None or plan.outcome is not TestPhaseOutcome.PASSED:
        return problems + ["plan phase did not pass"]
    steps = plan.data.get("steps", [])
    actions = [step["action"] for step in steps]
    if "prepare-layout" not in actions:
        problems.append("plan never proposes preparing the layout")
    if actions and actions.index("prepare-layout") > actions.index("install"):
        problems.append("plan proposes installing before preparing the layout")
    return problems


def _second_run() -> ScenarioSetup:
    world = controlled_world(ready_ports=[MANAGED_OLLAMA_PORT])
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.SECOND_PLAN),
        "install, then plan again",
    )


def _verify_second_run(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    second = _phase(report, TestPhase.SECOND_PLAN)
    if second is None:
        return problems + ["second_plan never ran"]
    if second.outcome is TestPhaseOutcome.FAILED:
        problems.append(f"second plan proposed mutation: {second.detail}")
    if second.data.get("created_directories"):
        problems.append("a dry-run second plan created directories")
    if getattr(world.ollama, "pulls", []):
        problems.append("a second run pulled a model")
    return problems


def _external(component: str, port: int) -> Callable[[], ScenarioSetup]:
    def build() -> ScenarioSetup:
        world = controlled_world(ready_ports=[port])
        world.inventory.add_external(port, name=f"{component}.exe")
        return ScenarioSetup(world, _spec(*READ_ONLY_PHASES), f"healthy external {component}")

    return build


def _verify_external(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    if world.external_lost():
        problems.append("an external process did not survive the run")
    if world.launcher_pid in world.inventory.pids():
        problems.append("the harness started a managed process during a read-only run")
    if getattr(world.ollama, "pulls", []):
        problems.append("an external component was written to")
    return problems


def _port_collision() -> ScenarioSetup:
    world = controlled_world(injections=InjectionPlan.of(Injection.PORT_OCCUPIED))
    world.inventory.occupy(MANAGED_OLLAMA_PORT)
    return ScenarioSetup(
        world, _spec(*INSTALL_PHASES), "an unrelated listener owns the managed port"
    )


def _verify_port_collision(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    preflight = _phase(report, TestPhase.PREFLIGHT)
    if preflight is None:
        return problems + ["preflight never ran"]
    if preflight.outcome is not TestPhaseOutcome.BLOCKED:
        problems.append(f"preflight did not block on a port collision: {preflight.outcome}")
    codes = {issue["code"] for issue in preflight.data.get("issues", [])}
    if "endpoint-collision" not in codes:
        problems.append(f"no endpoint-collision issue was reported; got {sorted(codes)}")
    provision = _phase(report, TestPhase.PROVISION)
    if provision is not None and provision.outcome is not TestPhaseOutcome.SKIPPED:
        problems.append(f"provisioning ran after a blocked preflight: {provision.outcome}")
    from .providers import UNRELATED_PID

    if UNRELATED_PID not in world.inventory.pids():
        problems.append("the unrelated process did not survive")
    return problems


def _corrupted_model() -> ScenarioSetup:
    world = controlled_world(ready_ports=[MANAGED_OLLAMA_PORT])
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.CONFIGURE),
        "a managed model file has the wrong bytes",
    )


def _verify_corrupted_model(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]
    if configure.outcome is TestPhaseOutcome.PASSED:
        problems.append("configuration succeeded with an unavailable model")
    roles = configure.data.get("roles", {})
    leaked = [role for role, value in roles.items() if value.get("has_location")]
    available = [role for role, value in roles.items() if value.get("state") == "available"]
    if leaked and available != leaked:
        problems.append(f"a non-available role exposed a location: {leaked}")
    return problems


def _interrupted_download() -> ScenarioSetup:
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT],
        injections=InjectionPlan.of(Injection.DOWNLOAD_INTERRUPTED),
    )
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.CONFIGURE),
        "the transport stops mid-body",
    )


def _verify_interrupted_download(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    layout = world.root.installation.layout
    paths = ManagedModelPaths.from_layout(layout)
    if paths.root.exists():
        promoted = [item for item in paths.root.rglob("*") if item.is_file()]
        partial = [item for item in promoted if item.suffix == ".part"]
        finished = [item for item in promoted if item.suffix != ".part"]
        if finished:
            problems.append(f"a partial download was promoted: {finished}")
        if partial:
            problems.append(f"a .part file survived: {partial}")
    return problems


def _readiness_failure() -> ScenarioSetup:
    world = controlled_world(injections=InjectionPlan.of(Injection.READINESS_TIMEOUT))
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.START),
        "the managed process never becomes ready",
    )


def _verify_readiness(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    start = _phase(report, TestPhase.START)
    if start is None:
        return problems + ["start never ran"]
    if start.outcome is TestPhaseOutcome.PASSED:
        problems.append("start reported success for a service that never became ready")
    return problems


def _crash_after_start() -> ScenarioSetup:
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT], injections=InjectionPlan.of(Injection.SERVICE_CRASH)
    )
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.START),
        "the managed process exits after starting",
    )


def _verify_crash(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    if world.launcher_pid in world.inventory.pids():
        problems.append("the crashed pid is still reported alive")
    return problems


def _stop_and_cleanup() -> ScenarioSetup:
    world = controlled_world(ready_ports=[MANAGED_OLLAMA_PORT])
    world.inventory.occupy(39999)  # an unrelated process that must survive
    return ScenarioSetup(
        world,
        _spec(
            TestPhase.PREFLIGHT,
            TestPhase.PLAN,
            TestPhase.PROVISION,
            TestPhase.START,
            TestPhase.STOP,
        ),
        "a managed service plus an unrelated process",
    )


def _verify_stop(report: TestRunReport, world: ControlledWorld) -> list[str]:
    from .providers import UNRELATED_PID

    problems = _cleanup_problems(report) + _no_residual(world)
    if UNRELATED_PID not in world.inventory.pids():
        problems.append("the unrelated process was stopped")
    if world.launcher_pid in world.inventory.pids():
        problems.append("the managed process survived stop and cleanup")
    return problems


def _missing_model() -> ScenarioSetup:
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT], injections=InjectionPlan.of(Injection.MODEL_MISSING)
    )
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.CONFIGURE),
        "a required role is not installed",
    )


def _verify_missing_model(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems = _cleanup_problems(report) + _no_residual(world)
    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]
    if configure.outcome is TestPhaseOutcome.PASSED:
        problems.append("configuration proceeded without every required model")
    return problems


def _cuda_unavailable() -> ScenarioSetup:
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT, MANAGED_COMFYUI_PORT],
        cuda=False,
        injections=InjectionPlan.of(Injection.CUDA_UNAVAILABLE),
    )
    return ScenarioSetup(
        world,
        _spec(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.IMAGE_SMOKE, gpu=True),
        "controlled facts report no usable CUDA",
    )


def _verify_cuda(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """No GPU workload may run, and the read-only phases must stay usable.

    Without a usable device the profile is genuinely incompatible, so the block
    can land in preflight rather than in the GPU phase itself. Either way the
    invariant is the same: the workload never ran, and plan still worked.
    """
    problems = _cleanup_problems(report) + _no_residual(world)
    image = _phase(report, TestPhase.IMAGE_SMOKE)
    if image is None:
        return problems + ["image_smoke is not present in the report"]
    if image.outcome not in (TestPhaseOutcome.BLOCKED, TestPhaseOutcome.SKIPPED):
        problems.append(f"a GPU workload ran without CUDA: {image.outcome}")
    plan = _phase(report, TestPhase.PLAN)
    if plan is None or plan.outcome is not TestPhaseOutcome.PASSED:
        problems.append("a read-only phase was not available while the GPU phase was blocked")
    preflight = _phase(report, TestPhase.PREFLIGHT)
    if preflight is not None and preflight.data.get("cuda_available", True):
        problems.append("preflight reported CUDA as available in a no-CUDA world")
    return problems


def _cleanup_after_exception() -> ScenarioSetup:
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT], injections=InjectionPlan.of(Injection.STOP_FAILURE)
    )
    return ScenarioSetup(
        world,
        _spec(
            TestPhase.PREFLIGHT,
            TestPhase.PLAN,
            TestPhase.PROVISION,
            TestPhase.START,
            TestPhase.STOP,
        ),
        "stop is refused, so cleanup must report it rather than hide it",
    )


def _verify_cleanup_after_failure(report: TestRunReport, world: ControlledWorld) -> list[str]:
    problems: list[str] = []
    if report.state is not TestRunState.FAILED:
        problems.append("a refused stop did not fail the run")
    # The root is still released even though a phase failed.
    if world.root.removable and world.root.exists():
        problems.append("the ephemeral root survived a failing run")
    cleanup = _phase(report, TestPhase.CLEANUP)
    if cleanup is None:
        problems.append("cleanup did not run after a failure")
    return problems


CONFIG_PHASES = (
    TestPhase.PREFLIGHT,
    TestPhase.PLAN,
    TestPhase.PROVISION,
    TestPhase.CONFIGURE,
    TestPhase.CHAT_SMOKE,
)


def _configurable_world(profile: str, injections: InjectionPlan):
    """A run that genuinely reaches managed configuration, plus its teardown.

    The shipped profile needs 12.45 GB of weights, so a controlled run is
    blocked long before configuration exists. These two rows are about what
    happens *at* configuration, so they point the run at a fixture repository
    whose manifests and workflow graphs are the real ones.
    """
    fixture = repo_fixture(profile)
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT],
        repo_root=fixture.path,
        injections=injections,
        ollama_models=[_ollama_entry()],
    )
    return world, fixture


def _corrupted_config() -> ScenarioSetup:
    world, fixture = _configurable_world(
        CHAT_ONLY_PROFILE, InjectionPlan.of(Injection.CONFIG_TAMPERED)
    )
    return ScenarioSetup(
        world,
        _spec(*CONFIG_PHASES, profile=CHAT_ONLY_PROFILE),
        "managed config is written, then edited behind its own marker",
        fixture.release,
    )


def _verify_corrupted_config(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Written valid, edited behind the marker, detected, refused."""
    problems = _cleanup_problems(report) + _no_residual(world)
    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]

    tampered = list(getattr(world.config_writer, "tampered", ()))
    if not tampered:
        # Without a real write first, the rest of this would pass vacuously.
        problems.append("no managed configuration was written, so nothing was tampered with")
    if configure.outcome is not TestPhaseOutcome.FAILED:
        problems.append(f"tampered configuration did not fail the phase: {configure.outcome}")
    if "edited after it was generated" not in configure.detail:
        problems.append(f"drift was not the reported reason: {configure.detail}")

    for path in tampered:
        # Read back through the domain at the moment of the edit: ownership
        # survived and the header still claims our digest, so only
        # recomputing it from the bytes on disk shows the drift.
        managed = world.config_writer.after_tampering[path]
        if managed.ownership is not Ownership.MANAGED:
            problems.append(f"tampering changed ownership of {path.name}: {managed.ownership}")
        if managed.claimed_digest is None:
            problems.append(f"tampering removed the marker from {path.name}")
        if managed.intact:
            problems.append(f"{path.name} still reports itself intact after being edited")

    smoke = _phase(report, TestPhase.CHAT_SMOKE)
    if smoke is None:
        problems.append("chat_smoke is not present in the report")
    elif smoke.outcome is not TestPhaseOutcome.SKIPPED:
        problems.append(f"runtime ran on tampered configuration: {smoke.outcome}")
    return problems


def _invalid_workflow() -> ScenarioSetup:
    world, fixture = _configurable_world(
        BROKEN_WORKFLOW_PROFILE, InjectionPlan.of(Injection.INVALID_WORKFLOW)
    )
    return ScenarioSetup(
        world,
        _spec(*CONFIG_PHASES, profile=BROKEN_WORKFLOW_PROFILE),
        "the declared image workflow requires model roles nothing declares",
        fixture.release,
    )


def _verify_invalid_workflow(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Not ready, so not configured, so never offered to a runtime."""
    problems = _cleanup_problems(report) + _no_residual(world)
    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]
    if configure.outcome is not TestPhaseOutcome.FAILED:
        problems.append(f"an unusable workflow did not fail configuration: {configure.outcome}")

    actions = {item["target"]: item for item in configure.data.get("actions", [])}
    workflow = actions.get("text_to_image")
    if workflow is None:
        problems.append("the workflow action never ran")
    elif workflow["ok"]:
        problems.append(f"the workflow reported itself usable: {workflow['detail']}")
    elif "model-not-declared" not in workflow["detail"]:
        problems.append(f"the workflow state was not the reported reason: {workflow['detail']}")

    if getattr(world.config_writer, "tampered", None):
        problems.append("configuration was written for a run with an unusable workflow")
    smoke = _phase(report, TestPhase.CHAT_SMOKE)
    if smoke is not None and smoke.outcome is not TestPhaseOutcome.SKIPPED:
        problems.append(f"runtime ran after an unusable workflow: {smoke.outcome}")
    return problems


P0_SCENARIOS: tuple[Scenario, ...] = (
    Scenario("clean-root", "Clean-root install", _clean_root, _verify_clean_root),
    Scenario("second-run", "Second run proposes nothing", _second_run, _verify_second_run),
    Scenario(
        "external-ollama",
        "External Ollama coexistence",
        _external("ollama", MANAGED_OLLAMA_PORT),
        _verify_external,
        mutating=False,
    ),
    Scenario(
        "external-comfyui",
        "External ComfyUI coexistence",
        _external("comfyui", MANAGED_COMFYUI_PORT),
        _verify_external,
        mutating=False,
    ),
    Scenario("port-collision", "Managed port collision", _port_collision, _verify_port_collision),
    Scenario("corrupted-model", "Corrupted model", _corrupted_model, _verify_corrupted_model),
    Scenario(
        "interrupted-download",
        "Interrupted model download",
        _interrupted_download,
        _verify_interrupted_download,
    ),
    Scenario("readiness-failure", "Readiness failure", _readiness_failure, _verify_readiness),
    Scenario("crash-after-start", "Crash after start", _crash_after_start, _verify_crash),
    Scenario("stop-cleanup", "Stop and cleanup", _stop_and_cleanup, _verify_stop),
    Scenario("missing-model", "Missing required model", _missing_model, _verify_missing_model),
    Scenario("cuda-unavailable", "CUDA unavailable", _cuda_unavailable, _verify_cuda),
    Scenario(
        "corrupted-config",
        "Corrupted managed configuration",
        _corrupted_config,
        _verify_corrupted_config,
    ),
    Scenario("invalid-workflow", "Invalid workflow", _invalid_workflow, _verify_invalid_workflow),
    Scenario(
        "cleanup-after-failure",
        "Cleanup after a failing phase",
        _cleanup_after_exception,
        _verify_cleanup_after_failure,
    ),
)


class ScenarioRunner:
    """Runs scenarios and checks their invariants from outside the run."""

    def __init__(self, scenarios: Sequence[Scenario] = P0_SCENARIOS) -> None:
        self.scenarios = tuple(scenarios)

    def run(self, scenario: Scenario) -> ScenarioResult:
        setup = scenario.setup()
        harness = StackTestHarness(setup.world)
        diagnostics: list[str] = []
        report: TestRunReport | None = None
        try:
            report = harness.run(new_run_id(), setup.spec)
            diagnostics.extend(scenario.verify(report, setup.world))
        except Exception as exc:  # noqa: BLE001 - a scenario crash is a diagnostic
            diagnostics.append(f"{type(exc).__name__}: {exc}")
        finally:
            # Even a crashed scenario must not leave a root behind.
            if setup.world.root.removable and setup.world.root.exists():
                try:
                    setup.world.root.release()
                except Exception as exc:  # noqa: BLE001
                    diagnostics.append(f"root release failed: {exc}")
            if setup.teardown is not None:
                try:
                    setup.teardown()
                except Exception as exc:  # noqa: BLE001
                    diagnostics.append(f"fixture teardown failed: {exc}")

        cleanup = _phase(report, TestPhase.CLEANUP) if report else None
        outcome = ScenarioOutcome.PASSED if not diagnostics else ScenarioOutcome.FAILED
        return ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            outcome=outcome,
            phase=_last_phase(report) if report else None,
            diagnostics=tuple(diagnostics),
            cleanup_status=(cleanup.detail if cleanup else "cleanup did not run"),
            metrics=dict(report.context.get("metrics", {})) if report else {},
            report=report,
        )

    def run_all(self) -> tuple[ScenarioResult, ...]:
        return tuple(self.run(scenario) for scenario in self.scenarios)


def scenario_by_id(scenario_id: str) -> Scenario:
    for scenario in P0_SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    known = ", ".join(item.id for item in P0_SCENARIOS)
    raise KeyError(f"unknown scenario {scenario_id!r}; known: {known}")


__all__ = [
    "CONFIG_PHASES",
    "FULL_PHASES",
    "INSTALL_PHASES",
    "P0_SCENARIOS",
    "READ_ONLY_PHASES",
    "Scenario",
    "ScenarioOutcome",
    "ScenarioResult",
    "ScenarioRunner",
    "ScenarioSetup",
    "scenario_by_id",
]
