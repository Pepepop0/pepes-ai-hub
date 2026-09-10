"""The harness driving real domains against controlled providers.

These are integration tests for the harness itself: they prove it orchestrates,
cleans up, and reports honestly. No process is started, no socket is opened, and
no byte leaves the machine — the transport is a MockTransport that treats an
undeclared URL as a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from local_ai_stack.e2e.harness import (
    MUTATING_PHASES,
    PhaseBlocked,
    StackTestHarness,
    new_run_id,
)
from local_ai_stack.e2e.roots import allocate_root
from local_ai_stack.e2e.scenarios import (
    P0_SCENARIOS,
    ScenarioOutcome,
    ScenarioRunner,
    scenario_by_id,
)
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.e2e.world import CapabilityRefused, controlled_world, real_world
from local_ai_stack.managed_config import Ownership
from local_ai_stack.test_contracts import (
    TestEventKind,
    TestHarness,
    TestPhase,
    TestPhaseOutcome,
    TestProviderMode,
    TestRootMode,
    TestRunSpec,
    TestRunState,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "local_ai_stack"
OLLAMA_PORT = 21434


def phase_of(report, phase: TestPhase):
    return next(item for item in report.phases if item.phase is phase)


def spec(*phases: TestPhase, **kwargs) -> TestRunSpec:
    return TestRunSpec(profile_id="pepe", phases=tuple(phases), **kwargs)


def run(world, *phases: TestPhase, **kwargs):
    return StackTestHarness(world).run(new_run_id(), spec(*phases, **kwargs))


# --- contrato --------------------------------------------------------------


def test_the_harness_satisfies_the_shared_protocol():
    world = controlled_world()
    try:
        assert isinstance(StackTestHarness(world), TestHarness)
    finally:
        world.root.release()


def test_a_run_id_is_opaque():
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert first.isalnum()


def test_the_report_carries_the_contract_shape():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    assert report.schema_version == 1
    assert report.started_at and report.finished_at
    assert report.state is TestRunState.PASSED
    assert {item.phase for item in report.phases} >= {TestPhase.PREFLIGHT, TestPhase.PLAN}
    assert all(item.duration_seconds is not None for item in report.phases)


def test_events_are_monotonic_and_end_with_exactly_one_result():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    events = []
    StackTestHarness(world).run(
        new_run_id(), spec(TestPhase.PREFLIGHT, TestPhase.PLAN), emit=events.append
    )
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(1 for event in events if event.kind is TestEventKind.RESULT) == 1
    assert events[-1].kind is TestEventKind.RESULT


# --- orquestração, não reimplementação ------------------------------------


def test_the_harness_composes_the_existing_domains():
    """It must import the domains, not restate their decisions."""
    tree = ast.parse((SRC / "e2e" / "harness.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    # ast keeps the leading dots in `level`, so the module reads unprefixed.
    assert {"integration", "planning", "model_resolver", "registry"} <= imported


def test_the_harness_defines_no_ownership_of_its_own():
    """Ownership, verification and paths stay in their domains."""
    source = (SRC / "e2e" / "harness.py").read_text(encoding="utf-8")
    for forbidden in ("taskkill", "def _is_external", "def _verify_checksum", "sha256("):
        assert forbidden not in source


def test_a_full_controlled_run_passes_every_phase():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(
        world,
        TestPhase.PREFLIGHT,
        TestPhase.PLAN,
        TestPhase.PROVISION,
        TestPhase.START,
        TestPhase.STOP,
        TestPhase.SECOND_PLAN,
    )
    outcomes = {item.phase: item.outcome for item in report.phases}
    assert report.state is TestRunState.PASSED, report.detail
    assert outcomes[TestPhase.PROVISION] is TestPhaseOutcome.PASSED
    assert outcomes[TestPhase.START] is TestPhaseOutcome.PASSED
    assert outcomes[TestPhase.SECOND_PLAN] is TestPhaseOutcome.PASSED


def test_a_controlled_run_never_reaches_the_real_network():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    run(world, TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION)
    assert world.artifacts.requests
    assert all(url.startswith("https://controlled.invalid/") for url in world.artifacts.requests)


# --- cleanup ---------------------------------------------------------------


def test_cleanup_runs_even_when_a_phase_raises(monkeypatch):
    world = controlled_world(ready_ports=[OLLAMA_PORT])

    def explode(self, context):
        raise RuntimeError("controlled explosion inside a phase")

    monkeypatch.setattr(StackTestHarness, "_phase_plan", explode, raising=True)
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)

    assert report.state is TestRunState.FAILED
    cleanup = next(item for item in report.phases if item.phase is TestPhase.CLEANUP)
    assert cleanup.outcome is TestPhaseOutcome.PASSED
    assert not world.root.exists()


def test_cleanup_cannot_turn_an_earlier_failure_into_a_pass(monkeypatch):
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    monkeypatch.setattr(
        StackTestHarness,
        "_phase_plan",
        lambda self, context: (_ for _ in ()).throw(RuntimeError("phase failed")),
    )
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    assert report.state is TestRunState.FAILED


def test_a_cleanup_failure_is_reported_on_its_own():
    world = controlled_world(
        ready_ports=[OLLAMA_PORT], injections=InjectionPlan.of(Injection.STOP_FAILURE)
    )
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION, TestPhase.START)
    cleanup = next(item for item in report.phases if item.phase is TestPhase.CLEANUP)
    assert cleanup.outcome is TestPhaseOutcome.FAILED
    assert "did not stop" in cleanup.detail
    # The root is still released despite the failure.
    assert not world.root.exists()


def test_the_ephemeral_root_never_survives_a_run():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    path = world.root.path
    run(world, TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION)
    assert not path.exists()


def test_a_configured_root_survives_the_run(tmp_path):
    from local_ai_stack.e2e.roots import allocate_root

    persistent = tmp_path / "persistent"
    persistent.mkdir()
    world = controlled_world(
        root=allocate_root(TestRootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    assert persistent.exists()


# --- capacidades e opt-in --------------------------------------------------


def test_a_real_world_refuses_without_the_network_capability(tmp_path):
    root = allocate_root(TestRootMode.CONFIGURED, configured=tmp_path / "real")
    with pytest.raises(CapabilityRefused, match="explicit network capability"):
        real_world(root=root, allow_network=False)


def test_a_real_world_refuses_even_with_the_capability(tmp_path):
    """Until real seams exist, nothing may call controlled providers real."""
    root = allocate_root(TestRootMode.CONFIGURED, configured=tmp_path / "real")
    with pytest.raises(CapabilityRefused, match="not implemented"):
        real_world(root=root, allow_network=True)


def test_no_default_test_path_builds_a_real_world():
    """The automatic suite must never depend on the machine or the internet."""
    offenders: list[str] = []
    for path in (Path(__file__).parent).rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "real_world(" in source and "CapabilityRefused" not in source:
            offenders.append(path.name)
    assert offenders == []


def test_the_gpu_phase_needs_its_capability():
    world = controlled_world(ready_ports=[OLLAMA_PORT, 28188])
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.IMAGE_SMOKE)
    image = next(item for item in report.phases if item.phase is TestPhase.IMAGE_SMOKE)
    assert image.outcome is TestPhaseOutcome.BLOCKED
    assert "GPU capability" in image.detail


def test_a_local_real_spec_is_refused_rather_than_run_on_controlled_seams():
    """The one thing a harness must never do is call controlled providers real."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = StackTestHarness(world).run(
        new_run_id(),
        TestRunSpec(
            profile_id="pepe",
            phases=(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.ACQUIRE_MODELS),
            provider_mode=TestProviderMode.LOCAL_REAL,
            allow_network=True,
        ),
    )
    assert report.state is TestRunState.FAILED
    assert "run refused" in report.detail
    assert all(
        item.outcome is TestPhaseOutcome.BLOCKED
        for item in report.phases
        if item.phase is not TestPhase.CLEANUP
    )


def test_a_configured_spec_is_refused_against_an_ephemeral_world():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = StackTestHarness(world).run(
        new_run_id(),
        spec(TestPhase.PREFLIGHT, TestPhase.PLAN, root_mode=TestRootMode.CONFIGURED),
    )
    assert report.state is TestRunState.FAILED
    assert "configured root" in report.detail
    assert "ephemeral" in report.detail


def test_an_ephemeral_spec_is_refused_against_a_configured_world(tmp_path):
    persistent = tmp_path / "stack"
    persistent.mkdir()
    world = controlled_world(
        root=allocate_root(TestRootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    report = StackTestHarness(world).run(new_run_id(), spec(TestPhase.PREFLIGHT))
    assert report.state is TestRunState.FAILED
    assert "run refused" in report.detail
    assert persistent.exists()


def test_a_refused_run_still_releases_its_root():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    StackTestHarness(world).run(
        new_run_id(),
        spec(TestPhase.PREFLIGHT, root_mode=TestRootMode.CONFIGURED),
    )
    assert not root.exists()


# --- o contexto é construído dentro do finally do cleanup -------------------


def test_an_unknown_profile_fails_the_run_and_still_cleans_up():
    """The profile is loaded inside the try whose finally releases the root."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    report = StackTestHarness(world).run(new_run_id(), spec(TestPhase.PREFLIGHT, TestPhase.PLAN))
    assert report.state is TestRunState.PASSED  # sanity: the profile normally loads

    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    report = StackTestHarness(world).run(
        new_run_id(),
        TestRunSpec(profile_id="no-such-profile", phases=(TestPhase.PREFLIGHT, TestPhase.PLAN)),
    )

    assert report.state is TestRunState.FAILED
    assert "unknown profile" in report.detail
    assert not root.exists()
    assert world.managed_residual() == ()
    cleanup = phase_of(report, TestPhase.CLEANUP)
    assert cleanup.outcome is TestPhaseOutcome.PASSED


def test_a_repository_without_profiles_fails_the_run_and_still_cleans_up(tmp_path):
    """Not even a loadable profiles directory. The root must still go."""
    (tmp_path / "profiles").mkdir()
    world = controlled_world(ready_ports=[OLLAMA_PORT], repo_root=tmp_path)
    root = world.root
    report = StackTestHarness(world).run(new_run_id(), spec(TestPhase.PREFLIGHT))
    assert report.state is TestRunState.FAILED
    assert not root.exists()


def test_a_failing_run_never_deletes_a_configured_root(tmp_path):
    persistent = tmp_path / "stack"
    persistent.mkdir()
    (persistent / "keep.txt").write_text("mine", encoding="utf-8")
    world = controlled_world(
        root=allocate_root(TestRootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    report = StackTestHarness(world).run(
        new_run_id(),
        TestRunSpec(
            profile_id="no-such-profile",
            phases=(TestPhase.PREFLIGHT,),
            root_mode=TestRootMode.CONFIGURED,
        ),
    )
    assert report.state is TestRunState.FAILED
    assert (persistent / "keep.txt").read_text(encoding="utf-8") == "mine"


# --- uma única fonte de injections -----------------------------------------


def test_the_harness_cannot_be_given_injections_of_its_own():
    """A second plan could name an injection no seam ever received."""
    world = controlled_world(injections=InjectionPlan.of(Injection.PORT_OCCUPIED))
    harness = StackTestHarness(world)
    assert harness.injections is world.injections
    with pytest.raises(TypeError):
        StackTestHarness(world, injections=InjectionPlan.none())


def test_a_declared_injection_really_reached_its_seam():
    """No decorative injections: the report names what the seams received."""
    setup = scenario_by_id("interrupted-download").setup()
    try:
        report = StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        declared = report.context["injection"]["injections"]
        assert declared == ["download-interrupted"]
        # The transport seam was asked for bytes, and actually refused them.
        assert setup.world.artifacts.requests, "the http-transport seam was never reached"
        interrupted = setup.world.artifacts.interrupted
        assert interrupted, "the declared injection never acted"
        kinds = {setup.world.artifacts.kinds[url] for url in interrupted}
        assert kinds == {"model"}, f"a model injection acted somewhere else: {kinds}"
    finally:
        setup.world.root.release()
        setup.teardown()


# --- bloqueio para de mutar ------------------------------------------------


def test_a_blocked_precondition_stops_every_mutating_phase():
    world = controlled_world(injections=InjectionPlan.of(Injection.PORT_OCCUPIED))
    world.inventory.occupy(OLLAMA_PORT)
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.PROVISION)

    outcomes = {item.phase: item.outcome for item in report.phases}
    assert outcomes[TestPhase.PREFLIGHT] is TestPhaseOutcome.BLOCKED
    assert outcomes[TestPhase.PROVISION] is TestPhaseOutcome.SKIPPED
    # A read-only phase stays available.
    assert outcomes[TestPhase.PLAN] is TestPhaseOutcome.PASSED


def test_a_blocked_phase_keeps_its_diagnostics():
    world = controlled_world(injections=InjectionPlan.of(Injection.PORT_OCCUPIED))
    world.inventory.occupy(OLLAMA_PORT)
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    preflight = next(item for item in report.phases if item.phase is TestPhase.PREFLIGHT)
    codes = {issue["code"] for issue in preflight.data["issues"]}
    assert "endpoint-collision" in codes


def test_mutating_phases_are_named_explicitly():
    assert TestPhase.PROVISION in MUTATING_PHASES
    assert TestPhase.PLAN not in MUTATING_PHASES
    assert TestPhase.PREFLIGHT not in MUTATING_PHASES


def test_phase_blocked_carries_a_payload():
    error = PhaseBlocked("nope", {"why": "because"})
    assert error.data == {"why": "because"}


# --- métricas no relatório -------------------------------------------------


def test_the_report_records_measurements():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    metrics = report.context["metrics"]
    recorded = {sample["metric"] for sample in metrics["samples"]}
    assert {"doctor", "plan", "cleanup"} <= recorded
    assert metrics["summary"]["plan"]["count"] >= 1


def test_the_report_names_the_injection():
    world = controlled_world(
        ready_ports=[OLLAMA_PORT], injections=InjectionPlan.of(Injection.READINESS_TIMEOUT)
    )
    report = run(world, TestPhase.PREFLIGHT, TestPhase.PLAN)
    assert report.context["injection"]["injections"] == ["readiness-timeout"]
    assert report.context["injection"]["seams"] == ["runtime-probe"]


def test_the_report_records_the_root_and_capabilities():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, TestPhase.PREFLIGHT, allow_gpu=False)
    assert report.context["root"]["mode"] == "ephemeral"
    assert report.context["capabilities"] == {"network": False, "gpu": False}
    assert report.context["provider_mode"] == "controlled"


# --- matriz P0 -------------------------------------------------------------


def test_the_matrix_ids_are_unique():
    ids = [scenario.id for scenario in P0_SCENARIOS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", P0_SCENARIOS, ids=lambda item: item.id)
def test_p0_scenario(scenario):
    result = ScenarioRunner().run(scenario)
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)
    assert result.cleanup_status


def test_a_scenario_result_carries_everything_a_reader_needs():
    result = ScenarioRunner().run(scenario_by_id("clean-root"))
    data = result.as_report_data()
    assert data["scenario"] == "clean-root"
    assert data["outcome"] == "passed"
    assert data["phase"]
    assert "samples" in data["metrics"]


def test_an_unknown_scenario_id_fails_loudly():
    with pytest.raises(KeyError, match="unknown scenario"):
        scenario_by_id("does-not-exist")


def test_every_scenario_leaves_no_ephemeral_root_behind():
    """Even a scenario that crashed must not leak gigabytes."""
    roots = []
    for scenario in P0_SCENARIOS:
        setup = scenario.setup()
        roots.append(setup.world.root)
        setup.world.root.release()
        if setup.teardown is not None:
            setup.teardown()
    assert not any(root.exists() for root in roots)


def test_a_scenario_fixture_is_released_with_its_root():
    """A repository fixture lives outside the root, so it needs its own hook."""
    setup = scenario_by_id("corrupted-config").setup()
    repo = setup.world.repo_root
    assert (repo / "profiles").is_dir()
    setup.world.root.release()
    setup.teardown()
    assert not repo.exists()


# --- configuração gerada e depois adulterada --------------------------------


def test_tampered_configuration_is_written_first_then_caught_by_the_domain():
    """Valid first, edited behind the marker, refused for the right reason."""
    result = ScenarioRunner().run(scenario_by_id("corrupted-config"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    configure = phase_of(result.report, TestPhase.CONFIGURE)
    assert configure.outcome is TestPhaseOutcome.FAILED
    assert "edited after it was generated" in configure.detail
    assert result.report.state is TestRunState.FAILED


def test_a_tampered_config_keeps_its_marker_and_still_fails():
    """Ownership is intact; only recomputing the digest finds the edit."""
    setup = scenario_by_id("corrupted-config").setup()
    try:
        StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        written = setup.world.config_writer.tampered
        assert written, "the scenario must write real configuration before editing it"
        for path in written:
            managed = setup.world.config_writer.after_tampering[path]
            assert managed.ownership is Ownership.MANAGED
            assert managed.claimed_digest
            assert not managed.intact
    finally:
        setup.world.root.release()
        setup.teardown()


def test_no_phase_runs_on_tampered_configuration():
    result = ScenarioRunner().run(scenario_by_id("corrupted-config"))
    smoke = phase_of(result.report, TestPhase.CHAT_SMOKE)
    assert smoke.outcome is TestPhaseOutcome.SKIPPED
    assert "configure" in smoke.detail


# --- workflow inválido ------------------------------------------------------


def test_an_unusable_workflow_never_reports_itself_ready():
    result = ScenarioRunner().run(scenario_by_id("invalid-workflow"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    configure = phase_of(result.report, TestPhase.CONFIGURE)
    assert configure.outcome is TestPhaseOutcome.FAILED
    workflow = next(item for item in configure.data["actions"] if item["target"] == "text_to_image")
    assert not workflow["ok"]
    assert "model-not-declared" in workflow["detail"]


def test_an_unusable_workflow_leaves_no_configuration_behind():
    """Nothing half-configured is left for a runtime to pick up."""
    setup = scenario_by_id("invalid-workflow").setup()
    root = setup.world.root.path
    try:
        StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        assert not setup.world.config_writer.tampered
        assert list(root.rglob("*.env")) == []
    finally:
        setup.world.root.release()
        setup.teardown()


def test_the_invalid_workflow_row_names_its_seam():
    plan = InjectionPlan.of(Injection.INVALID_WORKFLOW)
    assert plan.as_report_data()["seams"] == ["workflow-source"]
