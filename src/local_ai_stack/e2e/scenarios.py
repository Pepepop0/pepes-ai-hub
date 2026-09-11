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

from ..jobs import Membership
from ..managed_config import Ownership
from ..models import ManagedModelPaths, ModelState
from ..ollama_models import OllamaModelEntry
from ..test_contracts import (
    TestPhase,
    TestPhaseOutcome,
    TestRootMode,
    TestRunReport,
    TestRunSpec,
    TestRunState,
)
from .fixtures import (
    CHAT_DIGEST,
    CHAT_ONLY_PROFILE,
    CHAT_REFERENCE,
    FIXTURE_MODELS,
    STACK_PROFILE,
    external_install,
    repo_fixture,
)
from .harness import OLLAMA, StackTestHarness, new_run_id
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

# Where an external installation listens: the profile's own default, which is
# deliberately not the managed override. Sharing a port would be a collision,
# and a collision is a different row.
EXTERNAL_PORTS = {"ollama": 11434, "comfyui": 8188}


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
    """One P0 row: how to set it up, and what must be true afterwards.

    The expectations are declared rather than inferred. A verifier that only
    collects problems passes whenever it happens to find none, so a row whose
    run failed for an unrelated reason -- or whose phase never ran at all --
    would still read as passed. Each row therefore names the state it expects
    for the run, the phase it is actually about, and what that phase must
    report. A failing run is a pass only where the row says so.
    """

    id: str
    title: str
    setup: Callable[[], ScenarioSetup]
    verify: Verifier
    phase: TestPhase = TestPhase.PREFLIGHT
    outcomes: tuple[TestPhaseOutcome, ...] = (TestPhaseOutcome.PASSED,)
    state: TestRunState = TestRunState.PASSED
    cleanup: TestPhaseOutcome = TestPhaseOutcome.PASSED
    mutating: bool = True

    def verdict(self, report: TestRunReport) -> list[str]:
        """Whether the run itself matched what this row is about."""
        problems: list[str] = []
        if report.state is not self.state:
            problems.append(
                f"the run is {report.state} and this row expects {self.state}: {report.detail}"
            )
        problems.extend(_hidden_failures(report))
        result = _phase(report, self.phase)
        if result is None:
            return problems + [f"{self.phase} never ran, so this row proved nothing"]
        if result.outcome not in self.outcomes:
            expected = " or ".join(str(item) for item in self.outcomes)
            problems.append(
                f"{self.phase} is {result.outcome} and this row expects {expected}: {result.detail}"
            )
        cleanup = _phase(report, TestPhase.CLEANUP)
        if cleanup is None:
            problems.append("cleanup never ran")
        elif cleanup.outcome is not self.cleanup:
            problems.append(
                f"cleanup is {cleanup.outcome} and this row expects {self.cleanup}: {cleanup.detail}"
            )
        return problems


# --- helpers ---------------------------------------------------------------


def _phase(report: TestRunReport, phase: TestPhase):
    return next((item for item in report.phases if item.phase is phase), None)


def _hidden_failures(report: TestRunReport) -> list[str]:
    """A phase marked PASSED whose own structured results say otherwise.

    The harness raises when an execution report is not ok, so this should never
    find anything. It is here because "should never" is exactly the class of
    claim a test suite exists to check: a phase that swallowed a failed
    provisioning result, model action or domain report would otherwise read as
    green all the way up.
    """
    problems: list[str] = []
    for item in report.phases:
        if item.outcome is not TestPhaseOutcome.PASSED:
            continue
        if item.data.get("ok") is False:
            problems.append(f"{item.phase} passed while its own report says ok=False")
        for key in ("actions", "model_actions"):
            for action in item.data.get(key, []):
                if action.get("ok") is False:
                    problems.append(
                        f"{item.phase} passed with a failed {action.get('action')} "
                        f"for {action.get('target')}: {action.get('detail')}"
                    )
        for result in item.data.get("results", []):
            if result.get("outcome") == "failed":
                problems.append(
                    f"{item.phase} passed with {result.get('component')} reported failed"
                )
    return problems


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


def _ollama_entry(reference: str = CHAT_REFERENCE) -> OllamaModelEntry:
    return OllamaModelEntry(name=reference, digest=CHAT_DIGEST, size_bytes=1)


STACK_PHASES = (
    TestPhase.PREFLIGHT,
    TestPhase.PLAN,
    TestPhase.PROVISION,
    TestPhase.ACQUIRE_MODELS,
    TestPhase.CONFIGURE,
)


def _stack_world(injections: InjectionPlan, *, ready=(MANAGED_OLLAMA_PORT,)):
    """A run whose every declared model can genuinely be installed offline.

    The shipped profile's three image roles are 12.45 GB of real weights. The
    fixture catalog pins three small ones instead, checksummed to the exact
    bytes the controlled transport serves, so download, SHA-256 verification and
    placement are the real code paths on real files. What is a fixture is the
    size of the weights, not the handling of them.
    """
    fixture = repo_fixture(STACK_PROFILE)
    world = controlled_world(
        ready_ports=list(ready),
        repo_root=fixture.path,
        injections=injections,
        fixture_models=True,
        ollama_models=[_ollama_entry()],
    )
    return world, fixture


def _installer(world: ControlledWorld):
    return world.model_installer(world.root.installation.layout)


def _fixture_specs(world: ControlledWorld):
    catalog = world.model_catalog()
    return [
        catalog.require(catalog.normalize(f"comfyui/{name}")) for _r, name, *_ in FIXTURE_MODELS
    ]


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
    """A correct plan, and a provisioning report that names its own limits."""
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

    # This mode installs Ollama and nothing else. The row is only honest if the
    # report says which components it actually exercised and which it did not.
    provision = _phase(report, TestPhase.PROVISION)
    if provision is None:
        return problems + ["provision never ran"]
    components = provision.data.get("components", {})
    if components.get("installed") != [OLLAMA]:
        problems.append(f"this mode claims to have installed {components.get('installed')}")
    if not components.get("not_attempted"):
        problems.append("a partial provisioning run does not say what it skipped")
    if set(components.get("attempted", [])) & set(components.get("not_attempted", [])):
        problems.append("a component is reported both attempted and not attempted")
    return problems


def _second_run() -> ScenarioSetup:
    """Install and acquire for real, then ask again.

    The store starts empty so the first pass genuinely pulls, and the fixture
    weights are genuinely downloaded. Without that the second pass would have
    nothing to be idempotent about.
    """
    fixture = repo_fixture(STACK_PROFILE)
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT],
        repo_root=fixture.path,
        fixture_models=True,
        ollama_models=[],
    )
    return ScenarioSetup(
        world,
        _spec(
            TestPhase.PREFLIGHT,
            TestPhase.PLAN,
            TestPhase.PROVISION,
            TestPhase.ACQUIRE_MODELS,
            TestPhase.SECOND_PLAN,
            profile=STACK_PROFILE,
        ),
        "install and acquire for real, then ask a second time",
        fixture.release,
    )


def _verify_second_run(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Idempotence for what this mode installs, measured, not assumed."""
    problems = _cleanup_problems(report) + _no_residual(world)

    provision = _phase(report, TestPhase.PROVISION)
    acquire = _phase(report, TestPhase.ACQUIRE_MODELS)
    second = _phase(report, TestPhase.SECOND_PLAN)
    if provision is None or acquire is None or second is None:
        return problems + ["the first pass did not run, so there is nothing to repeat"]

    # The first pass was clean. A partial failure followed by "nothing to do"
    # is not idempotence, it is two runs agreeing to skip the broken part.
    for phase in (provision, acquire):
        if phase.outcome is not TestPhaseOutcome.PASSED or phase.data.get("ok") is not True:
            problems.append(f"{phase.phase} did not complete cleanly: {phase.detail}")
    failed_actions = [a for a in acquire.data.get("actions", []) if not a.get("ok")]
    if failed_actions:
        problems.append(f"a model action failed in the first pass: {failed_actions}")
    if second.data.get("ok") is not True or second.data.get("failed"):
        problems.append(f"the second pass reported failures: {second.data.get('failed')}")

    # The first pass really did something. Otherwise the second proves nothing.
    installed = provision.data.get("components", {}).get("installed", [])
    if OLLAMA not in installed:
        problems.append(f"the first pass installed nothing to be idempotent about: {installed}")
    first_downloads = acquire.data.get("downloads", 0)
    first_pulls = list(acquire.data.get("pulls", []))
    if first_downloads < len(FIXTURE_MODELS):
        problems.append(f"the fixture weights were never downloaded: {first_downloads}")
    if not first_pulls:
        problems.append("the chat model was never pulled, so a second pull cannot be ruled out")

    # The second pass proposes nothing, and measurably did nothing.
    if second.data.get("would_install") or second.data.get("would_acquire"):
        problems.append(f"a second run proposes work: {second.detail}")
    if second.data.get("downloads") != first_downloads:
        problems.append(
            f"a second run downloaded again: {first_downloads} -> {second.data.get('downloads')}"
        )
    if list(second.data.get("pulls", [])) != first_pulls:
        problems.append(f"a second run pulled again: {first_pulls} -> {second.data.get('pulls')}")
    if second.data.get("created_directories"):
        problems.append("a dry-run second plan created directories")
    return problems


EXTERNAL_PHASES = (TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.STOP)

# What each external fixture looked like before the run. Compared afterwards,
# this is the proof that nothing the hub does not own was modified.
_EXTERNAL_BEFORE: dict[str, dict[str, bytes]] = {}


def _external(component: str) -> Callable[[], ScenarioSetup]:
    """A real installation of one component, outside the managed root.

    Detection reads the filesystem and the environment, so an external
    component has to actually be one. The fixture creates it where that
    component's own hints look, and the controlled runner reports it exactly as
    a real machine would.
    """

    def build() -> ScenarioSetup:
        install = external_install(component)
        # A profile that enables both services, so "alongside external" is
        # genuinely exercised rather than skipped for a disabled component.
        fixture = repo_fixture(STACK_PROFILE)
        port = EXTERNAL_PORTS[component]
        world = controlled_world(
            ready_ports=[port],
            repo_root=fixture.path,
            fixture_models=True,
            on_path=install.on_path,
            environment=install.env,
            external_processes=[(port, str(install.path))],
        )
        _EXTERNAL_BEFORE[component] = install.snapshot()
        world.external = install  # read back by the verifier

        def release() -> None:
            fixture.release()
            install.release()

        return ScenarioSetup(
            world,
            _spec(*EXTERNAL_PHASES, profile=STACK_PROFILE),
            f"a real external {component} installation the hub must leave alone",
            release,
        )

    return build


def _verify_external(component: str) -> Verifier:
    def verify(report: TestRunReport, world: ControlledWorld) -> list[str]:
        problems = _cleanup_problems(report) + _no_residual(world)

        preflight = _phase(report, TestPhase.PREFLIGHT)
        if preflight is None:
            return problems + ["preflight never ran"]
        external = preflight.data.get("external_components", [])
        if component not in external:
            problems.append(f"{component} was not classified external; got {external}")
        if preflight.outcome is not TestPhaseOutcome.PASSED:
            problems.append(f"coexistence was refused: {preflight.detail}")

        # Managed authority is somewhere else entirely: our endpoint is not the
        # one the external installation listens on.
        endpoints = report.context.get("endpoints", {})
        managed = endpoints.get(component, "")
        if f":{EXTERNAL_PORTS[component]}" in managed:
            problems.append(f"the managed {component} endpoint is the external one: {managed}")

        # Observation only: never adopted, never started, never stopped.
        stop = _phase(report, TestPhase.STOP)
        observed = {
            item["service"]: item for item in (stop.data.get("observed", []) if stop else [])
        }
        status = observed.get(component)
        if status is None:
            problems.append(f"the runtime never reported on {component}")
        else:
            if status["stoppable"]:
                problems.append(f"the hub considers the external {component} its to stop")
            if status["origin"] == "managed":
                problems.append(f"the external {component} was adopted as managed")
        launcher = world._launcher
        if launcher is not None and (launcher.starts or launcher.stops):
            problems.append(
                f"the hub started or stopped something: {launcher.starts}/{launcher.stops}"
            )

        # It survived, and it is byte for byte what it was.
        if world.external_lost():
            problems.append("an external process did not survive the run")
        install = world.external
        if install is not None and install.snapshot() != _EXTERNAL_BEFORE.get(component):
            problems.append("the external installation on disk was modified")
        if getattr(world.ollama, "pulls", []):
            problems.append("an external model store was written to")
        return problems

    return verify


def _port_collision() -> ScenarioSetup:
    # A port somebody else already owns is what the world *is*, not a
    # dependency misbehaving, so it is built rather than injected.
    world = controlled_world()
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
    world, fixture = _stack_world(InjectionPlan.of(Injection.MODEL_TAMPERED))
    return ScenarioSetup(
        world,
        _spec(*STACK_PHASES, profile=STACK_PROFILE),
        "managed weights are installed and verified, then their bytes change",
        fixture.release,
    )


def _verify_corrupted_model(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Valid, then corrupted, then refused -- by the installer, not by us."""
    problems = _cleanup_problems(report) + _no_residual(world)
    acquire = _phase(report, TestPhase.ACQUIRE_MODELS)
    if acquire is None or acquire.outcome is not TestPhaseOutcome.PASSED:
        # Without a real install first, the corruption proves nothing.
        problems.append("the weights were never installed and verified in the first place")

    installer = _installer(world)
    if len(installer.tampered) != len(FIXTURE_MODELS):
        problems.append(f"expected every managed weight to be edited, got {installer.tampered}")
    for model_id, status in installer.after_tampering.items():
        # The installer's own detection, re-hashing the file it had accepted.
        if status.state is not ModelState.INVALID:
            problems.append(f"{model_id} is {status.state} after its bytes changed")

    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]
    if configure.outcome is TestPhaseOutcome.PASSED:
        problems.append("configuration succeeded with a corrupted model")
    roles = configure.data.get("roles", {})
    leaked = [role for role, value in roles.items() if value.get("has_location")]
    available = [role for role, value in roles.items() if value.get("state") == "available"]
    if sorted(leaked) != sorted(available):
        problems.append(f"a non-available role exposed a location: {leaked}")
    if not [role for role, value in roles.items() if value.get("state") == "invalid"]:
        problems.append(f"no role was reported invalid; got {roles}")
    return problems


def _interrupted_download() -> ScenarioSetup:
    world, fixture = _stack_world(InjectionPlan.of(Injection.DOWNLOAD_INTERRUPTED))
    return ScenarioSetup(
        world,
        _spec(*STACK_PHASES, profile=STACK_PROFILE),
        "the transport stops mid-body while a managed weight is being acquired",
        fixture.release,
    )


def _verify_interrupted_download(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """A model download, not a component download, and nothing is promoted."""
    problems = _cleanup_problems(report) + _no_residual(world)

    interrupted = [
        url for url in world.artifacts.interrupted if world.artifacts.kinds.get(url) == "model"
    ]
    if not interrupted:
        problems.append("no model download was interrupted, so this proves nothing about models")
    # Mid-transfer, not a refused connection: bytes reached the client, and the
    # downloader was already writing them, before the stream died.
    for url in interrupted:
        delivered = world.artifacts.delivered.get(url, 0)
        if delivered <= 0:
            problems.append(f"nothing was delivered before {url} failed, so it never started")

    acquire = _phase(report, TestPhase.ACQUIRE_MODELS)
    if acquire is None:
        return problems + ["acquire_models never ran"]
    if acquire.outcome is not TestPhaseOutcome.FAILED:
        problems.append(f"an interrupted download did not fail acquisition: {acquire.outcome}")

    paths = ManagedModelPaths.from_layout(world.root.installation.layout)
    if paths.root.exists():
        files = [item for item in paths.root.rglob("*") if item.is_file()]
        partial = [item for item in files if item.suffix == ".part"]
        finished = [item for item in files if item.suffix != ".part"]
        if finished:
            problems.append(f"a partial download was promoted: {finished}")
        if partial:
            problems.append(f"a .part file survived: {partial}")

    # Nothing became available, and a retry is still the proposed next step.
    installer = _installer(world)
    for spec in _fixture_specs(world):
        status = installer.detect(spec)
        if status.state is ModelState.AVAILABLE:
            problems.append(f"{spec.id} became available from an interrupted download")
        actions = [step.action for step in installer.plan(spec).steps]
        if "download" not in actions:
            problems.append(f"a retry is no longer proposed for {spec.id}: {actions}")
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
        _spec(
            TestPhase.PREFLIGHT,
            TestPhase.PLAN,
            TestPhase.PROVISION,
            TestPhase.START,
            TestPhase.STOP,
        ),
        "the managed process starts, is observed ready, and then exits",
    )


def _verify_crash(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Ready first, then gone. A start report alone cannot catch that."""
    problems = _cleanup_problems(report) + _no_residual(world)

    start = _phase(report, TestPhase.START)
    if start is None:
        return problems + ["start never ran"]
    if start.outcome is not TestPhaseOutcome.PASSED or not start.data.get("ready"):
        problems.append(f"the service was never observed ready: {start.detail}")
    if not world.endpoints.crashed:
        problems.append("the crash injection never acted")

    stop = _phase(report, TestPhase.STOP)
    if stop is None:
        return problems + ["stop never ran, so the post-crash state was never read"]
    # The first read can catch the service mid transition; the second is the one
    # that has to describe a settled post-crash state.
    settled = {item["service"]: item for item in stop.data.get("confirmed", [])}
    status = settled.get(OLLAMA)
    if status is None:
        problems.append("the runtime never confirmed the state of the crashed service")
    else:
        if status["runtime"] == "RUNNING":
            problems.append("the process is still reported running after the crash")
        if status["state"] == "UP":
            problems.append("the service is still reported UP after the crash")
        if status["stoppable"]:
            problems.append("a process that no longer exists is still reported stoppable")

    if world.managed_residual():
        problems.append(f"a managed process survived the crash: {world.managed_residual()}")
    if stop.data.get("registry"):
        problems.append(f"the registry still holds an entry: {stop.data['registry']}")
    return problems


def _stop_and_cleanup() -> ScenarioSetup:
    """A managed tree, an unrelated process, and an external one."""
    install = external_install("ollama")
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT],
        external_processes=[(EXTERNAL_PORTS["ollama"], str(install.path))],
    )
    world.inventory.occupy(39999)  # an unrelated process that must survive
    world.external = install
    return ScenarioSetup(
        world,
        _spec(
            TestPhase.PREFLIGHT,
            TestPhase.PLAN,
            TestPhase.PROVISION,
            TestPhase.START,
            TestPhase.STOP,
        ),
        "a managed process tree beside an unrelated and an external process",
        install.release,
    )


def _verify_stop(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """Ownership of a tree: everything ours ends, nothing else is touched."""
    from .providers import EXTERNAL_PID, UNRELATED_PID

    problems = _cleanup_problems(report) + _no_residual(world)
    launcher = world._launcher
    if launcher is None:
        return problems + ["nothing was ever started"]

    # A start produces a tree, and the launcher can prove which pids are in it.
    tree = launcher.members_started(OLLAMA)
    if len(tree) < 2:
        problems.append(f"only one process was started, so no tree was exercised: {tree}")
    for member in tree:
        if launcher.proved(OLLAMA, member) is not Membership.MEMBER:
            problems.append(f"pid {member} was never proved a member of our tree")
    if launcher.proved(OLLAMA, UNRELATED_PID) is Membership.MEMBER:
        problems.append("an unrelated pid was claimed as a member of our tree")

    stop = _phase(report, TestPhase.STOP)
    if stop is None:
        return problems + ["stop never ran"]
    stopped = {item["service"]: item for item in stop.data.get("stopped", [])}
    result = stopped.get(OLLAMA)
    if result is None:
        problems.append("the managed service was never stopped")
    else:
        if not result["stopped_tree"]:
            problems.append(f"stop ended a single process, not a tree: {result['tree_pids']}")
        if sorted(result["tree_pids"]) != sorted(tree):
            problems.append(f"stop reported {result['tree_pids']} for a tree of {tree}")
        if result["left_running"]:
            problems.append(f"members were left running: {result['left_running']}")

    alive = set(world.inventory.pids())
    survivors = set(tree) & alive
    if survivors:
        problems.append(f"managed tree members survived: {sorted(survivors)}")
    if UNRELATED_PID not in alive:
        problems.append("the unrelated process was stopped")
    if EXTERNAL_PID not in alive:
        problems.append("the external process was stopped")

    if stop.data.get("registry"):
        problems.append(f"the registry still holds an entry: {stop.data['registry']}")
    cleanup = _phase(report, TestPhase.CLEANUP)
    if cleanup is not None and not cleanup.data.get("job_handles_released"):
        problems.append("a job handle was never released")

    # Ownership is a resource. Stopping the processes is not enough if the
    # launcher still claims them: that is the proof a later run would act on.
    if launcher.members:
        problems.append(f"job ownership residual: membership still held {launcher.members}")
    for member in tree:
        if launcher.owns(OLLAMA, member) is Membership.MEMBER:
            problems.append(f"job ownership residual: pid {member} is still proved a member")
    if cleanup is not None and cleanup.data.get("membership"):
        problems.append(f"cleanup reports membership still held: {cleanup.data['membership']}")
    if cleanup is not None and cleanup.data.get("registry"):
        problems.append(f"cleanup reports registry entries: {cleanup.data['registry']}")
    if not launcher.closed:
        problems.append("the launcher handle was never closed")
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
    # Likewise: a machine without a usable device is a world, not an injection.
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT, MANAGED_COMFYUI_PORT],
        cuda=False,
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


WORKFLOW_PHASES = (
    TestPhase.PREFLIGHT,
    TestPhase.PLAN,
    TestPhase.PROVISION,
    TestPhase.ACQUIRE_MODELS,
    TestPhase.CONFIGURE,
    TestPhase.IMAGE_SMOKE,
)


def _workflow_world(injections: InjectionPlan):
    """The image stack, with the workflow-source seam under the given plan.

    Every model this profile declares is installed and verified for real, so a
    failure here can only be about the workflow. The identical world with the
    injection off runs green, which is the control this row needs.
    """
    fixture = repo_fixture(STACK_PROFILE, injections=injections)
    world = controlled_world(
        ready_ports=[MANAGED_OLLAMA_PORT, MANAGED_COMFYUI_PORT],
        repo_root=fixture.path,
        injections=injections,
        fixture_models=True,
        ollama_models=[_ollama_entry()],
    )
    world.external = fixture  # the seam's own record, read back by the verifier
    return world, fixture


def _invalid_workflow() -> ScenarioSetup:
    world, fixture = _workflow_world(InjectionPlan.of(Injection.INVALID_WORKFLOW))
    return ScenarioSetup(
        world,
        _spec(*WORKFLOW_PHASES, profile=STACK_PROFILE, gpu=True),
        "the declared graph no longer contains the node its model binding names",
        fixture.release,
    )


def _verify_invalid_workflow(report: TestRunReport, world: ControlledWorld) -> list[str]:
    """A structural defect in the graph, with every model available."""
    problems = _cleanup_problems(report) + _no_residual(world)

    fixture = world.external
    if not getattr(fixture, "broken_nodes", ()):
        problems.append("the workflow-source seam never corrupted the graph")

    # Not a missing model in disguise: acquisition succeeded.
    acquire = _phase(report, TestPhase.ACQUIRE_MODELS)
    if acquire is None or acquire.outcome is not TestPhaseOutcome.PASSED:
        problems.append("the models were never installed, so this could be a missing-model row")

    configure = _phase(report, TestPhase.CONFIGURE)
    if configure is None:
        return problems + ["configure never ran"]
    if configure.outcome is not TestPhaseOutcome.FAILED:
        problems.append(f"an invalid workflow did not fail configuration: {configure.outcome}")
    if "does not match its template" not in configure.detail:
        problems.append(f"the template mismatch was not the reported reason: {configure.detail}")
    for role, value in configure.data.get("roles", {}).items():
        if role.startswith("image.") and value.get("state") != "available":
            problems.append(f"{role} was {value.get('state')}, so the models are the real reason")

    if getattr(world.config_writer, "tampered", None):
        problems.append("configuration was written for a run with an invalid workflow")
    image = _phase(report, TestPhase.IMAGE_SMOKE)
    if image is None:
        problems.append("image_smoke is not present in the report")
    elif image.outcome is not TestPhaseOutcome.SKIPPED:
        problems.append(f"the runtime was offered an image capability: {image.outcome}")
    if world.comfyui.submissions:
        problems.append("a workflow was submitted despite being invalid")
    return problems


P0_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "clean-root",
        "Controlled partial provisioning from a clean root (Ollama only)",
        _clean_root,
        _verify_clean_root,
        phase=TestPhase.PROVISION,
    ),
    Scenario(
        "second-run",
        "Partial controlled stack idempotence",
        _second_run,
        _verify_second_run,
        phase=TestPhase.SECOND_PLAN,
    ),
    Scenario(
        "external-ollama",
        "External Ollama coexistence",
        _external("ollama"),
        _verify_external("ollama"),
        phase=TestPhase.PREFLIGHT,
        mutating=False,
    ),
    Scenario(
        "external-comfyui",
        "External ComfyUI coexistence",
        _external("comfyui"),
        _verify_external("comfyui"),
        phase=TestPhase.PREFLIGHT,
        mutating=False,
    ),
    Scenario(
        "port-collision",
        "Managed port collision",
        _port_collision,
        _verify_port_collision,
        phase=TestPhase.PREFLIGHT,
        outcomes=(TestPhaseOutcome.BLOCKED,),
    ),
    Scenario(
        "corrupted-model",
        "Corrupted managed model",
        _corrupted_model,
        _verify_corrupted_model,
        phase=TestPhase.CONFIGURE,
        outcomes=(TestPhaseOutcome.BLOCKED,),
    ),
    Scenario(
        "interrupted-download",
        "Interrupted model download",
        _interrupted_download,
        _verify_interrupted_download,
        phase=TestPhase.ACQUIRE_MODELS,
        outcomes=(TestPhaseOutcome.FAILED,),
        state=TestRunState.FAILED,
    ),
    Scenario(
        "readiness-failure",
        "Readiness failure",
        _readiness_failure,
        _verify_readiness,
        phase=TestPhase.START,
        outcomes=(TestPhaseOutcome.FAILED,),
        state=TestRunState.FAILED,
    ),
    Scenario(
        "crash-after-start",
        "Crash after start",
        _crash_after_start,
        _verify_crash,
        phase=TestPhase.STOP,
    ),
    Scenario(
        "stop-cleanup",
        "Stop and cleanup",
        _stop_and_cleanup,
        _verify_stop,
        phase=TestPhase.STOP,
    ),
    Scenario(
        "missing-model",
        "Missing required model",
        _missing_model,
        _verify_missing_model,
        phase=TestPhase.CONFIGURE,
        outcomes=(TestPhaseOutcome.BLOCKED,),
    ),
    Scenario(
        "cuda-unavailable",
        "CUDA unavailable",
        _cuda_unavailable,
        _verify_cuda,
        phase=TestPhase.IMAGE_SMOKE,
        # Without a usable device the profile is genuinely incompatible, so the
        # block can land in preflight and the GPU phase is then never attempted.
        # Either way no workload ran, which is what this row is about.
        outcomes=(TestPhaseOutcome.BLOCKED, TestPhaseOutcome.SKIPPED),
    ),
    Scenario(
        "corrupted-config",
        "Corrupted managed configuration",
        _corrupted_config,
        _verify_corrupted_config,
        phase=TestPhase.CONFIGURE,
        outcomes=(TestPhaseOutcome.FAILED,),
        state=TestRunState.FAILED,
    ),
    Scenario(
        "invalid-workflow",
        "Invalid workflow template",
        _invalid_workflow,
        _verify_invalid_workflow,
        phase=TestPhase.CONFIGURE,
        outcomes=(TestPhaseOutcome.FAILED,),
        state=TestRunState.FAILED,
    ),
    Scenario(
        "cleanup-after-failure",
        "Cleanup after a failing phase",
        _cleanup_after_exception,
        _verify_cleanup_after_failure,
        phase=TestPhase.CLEANUP,
        outcomes=(TestPhaseOutcome.FAILED,),
        state=TestRunState.FAILED,
        cleanup=TestPhaseOutcome.FAILED,
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
            diagnostics.extend(scenario.verdict(report))
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
