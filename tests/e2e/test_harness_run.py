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
from local_ai_stack.e2e.scenarios import (
    P0_SCENARIOS,
    ScenarioOutcome,
    ScenarioRunner,
    scenario_by_id,
)
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.e2e.world import CapabilityRefused, controlled_world, real_world
from local_ai_stack.test_contracts import (
    TestEventKind,
    TestHarness,
    TestPhase,
    TestPhaseOutcome,
    TestRootMode,
    TestRunSpec,
    TestRunState,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "local_ai_stack"
OLLAMA_PORT = 21434


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
    from local_ai_stack.e2e.roots import allocate_root

    root = allocate_root(TestRootMode.CONFIGURED, configured=tmp_path / "real")
    with pytest.raises(CapabilityRefused, match="explicit network capability"):
        real_world(root=root, allow_network=False)


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


def test_real_model_acquisition_needs_the_network_capability():
    from local_ai_stack.test_contracts import TestProviderMode

    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = StackTestHarness(world).run(
        new_run_id(),
        TestRunSpec(
            profile_id="pepe",
            phases=(TestPhase.PREFLIGHT, TestPhase.PLAN, TestPhase.ACQUIRE_MODELS),
            provider_mode=TestProviderMode.LOCAL_REAL,
            allow_network=False,
        ),
    )
    acquire = next(item for item in report.phases if item.phase is TestPhase.ACQUIRE_MODELS)
    assert acquire.outcome is TestPhaseOutcome.BLOCKED
    assert "network capability" in acquire.detail


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
    assert not any(root.exists() for root in roots)
