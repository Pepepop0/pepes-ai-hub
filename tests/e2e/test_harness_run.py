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
from local_ai_stack.jobs import Membership
from local_ai_stack.managed_config import Ownership
from local_ai_stack.ollama_models import OllamaModelEntry
from local_ai_stack.test_contracts import (
    TestEventKind as EventKind,
)
from local_ai_stack.test_contracts import (
    TestHarness as HarnessProtocol,
)
from local_ai_stack.test_contracts import (
    TestPhase as Phase,
)
from local_ai_stack.test_contracts import (
    TestPhaseOutcome as Outcome,
)
from local_ai_stack.test_contracts import (
    TestProviderMode as ProviderMode,
)
from local_ai_stack.test_contracts import (
    TestRootMode as RootMode,
)
from local_ai_stack.test_contracts import (
    TestRunSpec as RunSpec,
)
from local_ai_stack.test_contracts import (
    TestRunState as RunState,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "local_ai_stack"
OLLAMA_PORT = 21434


def phase_of(report, phase: Phase):
    return next(item for item in report.phases if item.phase is phase)


def spec(*phases: Phase, **kwargs) -> RunSpec:
    return RunSpec(profile_id="pepe", phases=tuple(phases), **kwargs)


def run(world, *phases: Phase, **kwargs):
    return StackTestHarness(world).run(new_run_id(), spec(*phases, **kwargs))


# --- contrato --------------------------------------------------------------


def test_the_harness_satisfies_the_shared_protocol():
    world = controlled_world()
    try:
        assert isinstance(StackTestHarness(world), HarnessProtocol)
    finally:
        world.root.release()


def test_a_run_id_is_opaque():
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert first.isalnum()


def test_the_report_carries_the_contract_shape():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)
    assert report.schema_version == 1
    assert report.started_at and report.finished_at
    assert report.state is RunState.PASSED
    assert {item.phase for item in report.phases} >= {Phase.PREFLIGHT, Phase.PLAN}
    assert all(item.duration_seconds is not None for item in report.phases)


def test_events_are_monotonic_and_end_with_exactly_one_result():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    events = []
    StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT, Phase.PLAN), emit=events.append)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert sum(1 for event in events if event.kind is EventKind.RESULT) == 1
    assert events[-1].kind is EventKind.RESULT


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
    report = run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION, Phase.START, Phase.STOP)
    outcomes = {item.phase: item.outcome for item in report.phases}
    assert report.state is RunState.PASSED, report.detail
    assert outcomes[Phase.PROVISION] is Outcome.PASSED
    assert outcomes[Phase.START] is Outcome.PASSED
    assert outcomes[Phase.STOP] is Outcome.PASSED


def test_a_second_plan_without_acquisition_proposes_the_missing_models():
    """Regression: this used to pass because `changed` is never true in a dry run.

    The shipped profile's models were never acquired, so a second pass has work
    to do and must say so. Idempotence is proved by the second-run row, where
    the first pass really acquires everything.
    """
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION, Phase.SECOND_PLAN)
    second = phase_of(report, Phase.SECOND_PLAN)
    assert second.outcome is Outcome.FAILED
    assert "ollama/hermes3_8b" in second.data["would_acquire"]
    assert all(second.data["proposed_steps"][target] for target in second.data["would_acquire"])


def test_a_controlled_run_never_reaches_the_real_network():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION)
    assert world.artifacts.requests
    assert all(url.startswith("https://controlled.invalid/") for url in world.artifacts.requests)


# --- cleanup ---------------------------------------------------------------


def test_cleanup_runs_even_when_a_phase_raises(monkeypatch):
    world = controlled_world(ready_ports=[OLLAMA_PORT])

    def explode(self, context):
        raise RuntimeError("controlled explosion inside a phase")

    monkeypatch.setattr(StackTestHarness, "_phase_plan", explode, raising=True)
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)

    assert report.state is RunState.FAILED
    cleanup = next(item for item in report.phases if item.phase is Phase.CLEANUP)
    assert cleanup.outcome is Outcome.PASSED
    assert not world.root.exists()


def test_cleanup_cannot_turn_an_earlier_failure_into_a_pass(monkeypatch):
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    monkeypatch.setattr(
        StackTestHarness,
        "_phase_plan",
        lambda self, context: (_ for _ in ()).throw(RuntimeError("phase failed")),
    )
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)
    assert report.state is RunState.FAILED


def test_a_cleanup_failure_is_reported_on_its_own():
    world = controlled_world(
        ready_ports=[OLLAMA_PORT], injections=InjectionPlan.of(Injection.STOP_FAILURE)
    )
    report = run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION, Phase.START)
    cleanup = next(item for item in report.phases if item.phase is Phase.CLEANUP)
    assert cleanup.outcome is Outcome.FAILED
    assert "did not stop" in cleanup.detail
    # The root is still released despite the failure.
    assert not world.root.exists()


def test_the_ephemeral_root_never_survives_a_run():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    path = world.root.path
    run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION)
    assert not path.exists()


def test_a_configured_root_survives_the_run(tmp_path):
    from local_ai_stack.e2e.roots import allocate_root

    persistent = tmp_path / "persistent"
    persistent.mkdir()
    world = controlled_world(
        root=allocate_root(RootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    run(world, Phase.PREFLIGHT, Phase.PLAN)
    assert persistent.exists()


# --- capacidades e opt-in --------------------------------------------------


def test_a_real_world_refuses_without_the_network_capability(tmp_path):
    root = allocate_root(RootMode.CONFIGURED, configured=tmp_path / "real")
    with pytest.raises(CapabilityRefused, match="explicit network capability"):
        real_world(root=root, allow_network=False)


def test_a_real_world_refuses_even_with_the_capability(tmp_path):
    """Until real seams exist, nothing may call controlled providers real."""
    root = allocate_root(RootMode.CONFIGURED, configured=tmp_path / "real")
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
    report = run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.IMAGE_SMOKE)
    image = next(item for item in report.phases if item.phase is Phase.IMAGE_SMOKE)
    assert image.outcome is Outcome.BLOCKED
    assert "GPU capability" in image.detail


def test_a_local_real_spec_is_refused_rather_than_run_on_controlled_seams():
    """The one thing a harness must never do is call controlled providers real."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = StackTestHarness(world).run(
        new_run_id(),
        RunSpec(
            profile_id="pepe",
            phases=(Phase.PREFLIGHT, Phase.PLAN, Phase.ACQUIRE_MODELS),
            provider_mode=ProviderMode.LOCAL_REAL,
            allow_network=True,
        ),
    )
    assert report.state is RunState.FAILED
    assert "run refused" in report.detail
    assert all(
        item.outcome is Outcome.BLOCKED for item in report.phases if item.phase is not Phase.CLEANUP
    )


def test_a_configured_spec_is_refused_against_an_ephemeral_world():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = StackTestHarness(world).run(
        new_run_id(),
        spec(Phase.PREFLIGHT, Phase.PLAN, root_mode=RootMode.CONFIGURED),
    )
    assert report.state is RunState.FAILED
    assert "configured root" in report.detail
    assert "ephemeral" in report.detail


def test_an_ephemeral_spec_is_refused_against_a_configured_world(tmp_path):
    persistent = tmp_path / "stack"
    persistent.mkdir()
    world = controlled_world(
        root=allocate_root(RootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))
    assert report.state is RunState.FAILED
    assert "run refused" in report.detail
    assert persistent.exists()


def test_a_refused_run_still_releases_its_root():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    StackTestHarness(world).run(
        new_run_id(),
        spec(Phase.PREFLIGHT, root_mode=RootMode.CONFIGURED),
    )
    assert not root.exists()


# --- o contexto é construído dentro do finally do cleanup -------------------


def test_an_unknown_profile_fails_the_run_and_still_cleans_up():
    """The profile is loaded inside the try whose finally releases the root."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT, Phase.PLAN))
    assert report.state is RunState.PASSED  # sanity: the profile normally loads

    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    report = StackTestHarness(world).run(
        new_run_id(),
        RunSpec(profile_id="no-such-profile", phases=(Phase.PREFLIGHT, Phase.PLAN)),
    )

    assert report.state is RunState.FAILED
    assert "unknown profile" in report.detail
    assert not root.exists()
    assert world.managed_residual() == ()
    cleanup = phase_of(report, Phase.CLEANUP)
    assert cleanup.outcome is Outcome.PASSED


def test_a_repository_without_profiles_fails_the_run_and_still_cleans_up(tmp_path):
    """Not even a loadable profiles directory. The root must still go."""
    (tmp_path / "profiles").mkdir()
    world = controlled_world(ready_ports=[OLLAMA_PORT], repo_root=tmp_path)
    root = world.root
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))
    assert report.state is RunState.FAILED
    assert not root.exists()


def test_a_failing_run_never_deletes_a_configured_root(tmp_path):
    persistent = tmp_path / "stack"
    persistent.mkdir()
    (persistent / "keep.txt").write_text("mine", encoding="utf-8")
    world = controlled_world(
        root=allocate_root(RootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )
    report = StackTestHarness(world).run(
        new_run_id(),
        RunSpec(
            profile_id="no-such-profile",
            phases=(Phase.PREFLIGHT,),
            root_mode=RootMode.CONFIGURED,
        ),
    )
    assert report.state is RunState.FAILED
    assert (persistent / "keep.txt").read_text(encoding="utf-8") == "mine"


# --- uma única fonte de injections -----------------------------------------


def test_the_harness_cannot_be_given_injections_of_its_own():
    """A second plan could name an injection no seam ever received."""
    world = controlled_world(injections=InjectionPlan.of(Injection.READINESS_TIMEOUT))
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
    world = controlled_world()
    world.inventory.occupy(OLLAMA_PORT)
    report = run(world, Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION)

    outcomes = {item.phase: item.outcome for item in report.phases}
    assert outcomes[Phase.PREFLIGHT] is Outcome.BLOCKED
    assert outcomes[Phase.PROVISION] is Outcome.SKIPPED
    # A read-only phase stays available.
    assert outcomes[Phase.PLAN] is Outcome.PASSED


def test_a_blocked_phase_keeps_its_diagnostics():
    world = controlled_world()
    world.inventory.occupy(OLLAMA_PORT)
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)
    preflight = next(item for item in report.phases if item.phase is Phase.PREFLIGHT)
    codes = {issue["code"] for issue in preflight.data["issues"]}
    assert "endpoint-collision" in codes


def test_mutating_phases_are_named_explicitly():
    assert Phase.PROVISION in MUTATING_PHASES
    assert Phase.PLAN not in MUTATING_PHASES
    assert Phase.PREFLIGHT not in MUTATING_PHASES


def test_phase_blocked_carries_a_payload():
    error = PhaseBlocked("nope", {"why": "because"})
    assert error.data == {"why": "because"}


# --- métricas no relatório -------------------------------------------------


def test_the_report_records_measurements():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)
    metrics = report.context["metrics"]
    recorded = {sample["metric"] for sample in metrics["samples"]}
    assert {"doctor", "plan", "cleanup"} <= recorded
    assert metrics["summary"]["plan"]["count"] >= 1


def test_the_report_names_the_injection():
    world = controlled_world(
        ready_ports=[OLLAMA_PORT], injections=InjectionPlan.of(Injection.READINESS_TIMEOUT)
    )
    report = run(world, Phase.PREFLIGHT, Phase.PLAN)
    assert report.context["injection"]["injections"] == ["readiness-timeout"]
    assert report.context["injection"]["seams"] == ["runtime-probe"]


def test_the_report_records_the_root_and_capabilities():
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    report = run(world, Phase.PREFLIGHT, allow_gpu=False)
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

    configure = phase_of(result.report, Phase.CONFIGURE)
    assert configure.outcome is Outcome.FAILED
    assert "edited after it was generated" in configure.detail
    assert result.report.state is RunState.FAILED


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
    smoke = phase_of(result.report, Phase.CHAT_SMOKE)
    assert smoke.outcome is Outcome.SKIPPED
    assert "configure" in smoke.detail


# --- workflow inválido ------------------------------------------------------


def test_an_unusable_workflow_never_reports_itself_ready():
    result = ScenarioRunner().run(scenario_by_id("invalid-workflow"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    configure = phase_of(result.report, Phase.CONFIGURE)
    assert configure.outcome is Outcome.FAILED
    workflow = next(item for item in configure.data["actions"] if item["target"] == "text_to_image")
    assert not workflow["ok"]
    # A structural defect in the graph, not an absent model.
    assert "75:70" in workflow["detail"]
    roles = configure.data["roles"]
    assert all(
        value["state"] == "available" for role, value in roles.items() if role.startswith("image.")
    )


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


# --- chat e image provam contrato, não readiness ----------------------------


def test_chat_smoke_sends_a_generation_to_the_resolved_model():
    """A version probe proves a port answers; this proves the chat contract."""
    from local_ai_stack.e2e.fixtures import CHAT_REFERENCE, STACK_PROFILE, repo_fixture

    fixture = repo_fixture(STACK_PROFILE)
    world = controlled_world(
        ready_ports=[OLLAMA_PORT],
        repo_root=fixture.path,
        fixture_models=True,
        ollama_models=[
            OllamaModelEntry(name=CHAT_REFERENCE, digest="4f6b83f30b62aaaa", size_bytes=1)
        ],
    )
    try:
        report = StackTestHarness(world).run(
            new_run_id(),
            RunSpec(
                profile_id=STACK_PROFILE,
                phases=(
                    Phase.PREFLIGHT,
                    Phase.PLAN,
                    Phase.PROVISION,
                    Phase.ACQUIRE_MODELS,
                    Phase.CHAT_SMOKE,
                ),
            ),
        )
        chat = phase_of(report, Phase.CHAT_SMOKE)
        assert chat.outcome is Outcome.PASSED, chat.detail
        assert chat.data["model"] == CHAT_REFERENCE
        assert chat.data["answered"]

        # The request reached the store, at the endpoint the profile resolved.
        endpoint, reference, prompt = world.ollama.chats[-1]
        assert reference == CHAT_REFERENCE
        assert str(OLLAMA_PORT) in endpoint
        assert prompt
        assert chat.data["endpoint"] == endpoint
    finally:
        world.root.release()
        fixture.release()


def test_a_chat_route_refuses_a_model_that_is_not_in_the_store():
    from local_ai_stack.e2e.providers import ControlledOllama
    from local_ai_stack.ollama_models import OllamaClientError

    store = ControlledOllama(installed=[])
    with pytest.raises(OllamaClientError, match="not in the controlled store"):
        store.chat("http://127.0.0.1:21434", "nobody:installed", "hi")


def test_image_smoke_submits_the_rendered_workflow():
    """The proof is the submission, not a health route."""
    from local_ai_stack.e2e.fixtures import CHAT_REFERENCE, STACK_PROFILE, repo_fixture

    fixture = repo_fixture(STACK_PROFILE)
    world = controlled_world(
        ready_ports=[OLLAMA_PORT, 28188],
        repo_root=fixture.path,
        fixture_models=True,
        ollama_models=[
            OllamaModelEntry(name=CHAT_REFERENCE, digest="4f6b83f30b62aaaa", size_bytes=1)
        ],
    )
    try:
        report = StackTestHarness(world).run(
            new_run_id(),
            RunSpec(
                profile_id=STACK_PROFILE,
                phases=(
                    Phase.PREFLIGHT,
                    Phase.PLAN,
                    Phase.PROVISION,
                    Phase.ACQUIRE_MODELS,
                    Phase.IMAGE_SMOKE,
                ),
                allow_gpu=True,
            ),
        )
        image = phase_of(report, Phase.IMAGE_SMOKE)
        assert image.outcome is Outcome.PASSED, image.detail

        # One submission, carrying the whole graph.
        assert len(world.comfyui.submissions) == 1
        submitted = image.data["submitted"]
        assert submitted["model"] == image.data["runtime_name"]
        assert submitted["prompt"]
        assert submitted["steps"] == 4
        assert submitted["seed"] == 7
        assert image.data["images"] == 1

        # And the graph really carries it, at the node the manifest declared.
        assert world.comfyui.submitted("75:70", "unet_name") == image.data["runtime_name"]
    finally:
        world.root.release()
        fixture.release()


def test_no_gpu_workload_runs_during_an_image_smoke():
    """A controlled ComfyUI accepts and answers; it never executes."""
    from local_ai_stack.e2e.providers import ControlledComfyUI

    comfyui = ControlledComfyUI()
    job = comfyui.submit("http://127.0.0.1:28188", {"1": {"inputs": {"a": 1}}})
    assert comfyui.result(job.prompt_id)["status"] == "success"
    assert not hasattr(comfyui, "execute")
    assert not hasattr(comfyui, "run")


def test_an_empty_workflow_submission_is_a_violation():
    from local_ai_stack.e2e.providers import ControlledComfyUI, ControlledViolation

    with pytest.raises(ControlledViolation):
        ControlledComfyUI().submit("http://127.0.0.1:28188", {})


# --- crash e coexistência ---------------------------------------------------


def test_a_crash_happens_after_readiness_not_instead_of_it():
    result = ScenarioRunner().run(scenario_by_id("crash-after-start"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    start = phase_of(result.report, Phase.START)
    assert start.outcome is Outcome.PASSED
    assert start.data["ready"] is True

    observed = {
        item["service"]: item for item in phase_of(result.report, Phase.STOP).data["observed"]
    }
    assert observed["ollama"]["state"] != "UP"


def test_a_crash_is_never_proved_by_an_exception():
    """Every phase of the crash row ran; none of them raised."""
    result = ScenarioRunner().run(scenario_by_id("crash-after-start"))
    for item in result.report.phases:
        assert "Error" not in item.detail, f"{item.phase}: {item.detail}"
        assert "traceback" not in item.data


@pytest.mark.parametrize("component", ["ollama", "comfyui"])
def test_an_external_installation_is_classified_and_left_alone(component):
    setup = scenario_by_id(f"external-{component}").setup()
    before = setup.world.external.snapshot()
    try:
        report = StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        preflight = phase_of(report, Phase.PREFLIGHT)
        assert component in preflight.data["external_components"]
        assert preflight.outcome is Outcome.PASSED

        observed = {item["service"]: item for item in phase_of(report, Phase.STOP).data["observed"]}
        assert observed[component]["stoppable"] is False
        assert observed[component]["origin"] != "managed"

        assert setup.world.external.snapshot() == before
        assert setup.world.external_lost() == ()
        assert setup.world.ollama.pulls == []
    finally:
        setup.world.root.release()
        setup.teardown()


# --- veredito declarado, não inferido ---------------------------------------


def test_every_scenario_declares_the_phase_it_is_about(scenario_run):
    for scenario in P0_SCENARIOS:
        phases = scenario_run(scenario).spec.phases
        assert scenario.phase in phases or scenario.phase is Phase.CLEANUP, (
            f"{scenario.id} names a phase it never runs"
        )


def test_a_failing_run_is_not_a_passing_scenario():
    """The defect this contract exists to prevent: a silent false pass."""
    from dataclasses import replace

    row = replace(scenario_by_id("readiness-failure"), state=RunState.PASSED)
    result = ScenarioRunner().run(row)
    assert result.outcome is ScenarioOutcome.FAILED
    assert any("expects passed" in item for item in result.diagnostics)


def test_a_phase_that_never_ran_is_not_a_passing_scenario():
    from dataclasses import replace

    row = replace(scenario_by_id("clean-root"), phase=Phase.IMAGE_SMOKE)
    result = ScenarioRunner().run(row)
    assert result.outcome is ScenarioOutcome.FAILED
    assert any("never ran" in item for item in result.diagnostics)


def test_a_row_that_expects_a_failure_says_so():
    """Rows about failures are declared, not inferred from a silent verifier."""
    expected_failures = {row.id for row in P0_SCENARIOS if row.state is RunState.FAILED}
    assert expected_failures == {
        "interrupted-download",
        "readiness-failure",
        "corrupted-config",
        "invalid-workflow",
        "cleanup-after-failure",
    }


def test_only_the_cleanup_row_tolerates_a_failing_cleanup():
    tolerant = [row.id for row in P0_SCENARIOS if row.cleanup is not Outcome.PASSED]
    assert tolerant == ["cleanup-after-failure"]


# --- idempotência parcial, medida -------------------------------------------


def test_the_second_run_row_measures_a_first_pass_that_really_did_work():
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    acquire = phase_of(result.report, Phase.ACQUIRE_MODELS)
    second = phase_of(result.report, Phase.SECOND_PLAN)
    assert acquire.data["pulls"], "nothing was pulled, so nothing can be idempotent"
    assert acquire.data["downloads"] >= 3

    assert second.data["would_install"] == []
    assert second.data["would_acquire"] == []
    assert second.data["downloads"] == acquire.data["downloads"]
    assert second.data["pulls"] == acquire.data["pulls"]


def test_the_second_plan_looks_for_an_outcome_a_dry_run_can_produce():
    """`installed` never appears in a dry run, so it could never fail."""
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    outcomes = {
        item["outcome"] for item in phase_of(result.report, Phase.SECOND_PLAN).data["results"]
    }
    assert "installed" not in outcomes
    # "failed" is not a valid way for a second pass to propose nothing.
    assert outcomes <= {"skipped"}


# --- honestidade sobre o que foi realmente instalado ------------------------


def test_a_partial_provisioning_run_names_what_it_did_not_attempt():
    result = ScenarioRunner().run(scenario_by_id("clean-root"))
    components = phase_of(result.report, Phase.PROVISION).data["components"]
    assert components["installed"] == ["ollama"]
    assert set(components["not_attempted"]) == {"comfyui", "open-webui"}
    assert set(components["declared"]) == {"ollama", "comfyui", "open-webui"}


def test_the_clean_root_row_does_not_call_itself_a_whole_stack_install():
    title = scenario_by_id("clean-root").title.lower()
    assert "partial" in title
    assert "ollama only" in title


# --- cleanup cobre até a construção do próprio run --------------------------


@pytest.mark.parametrize(
    "target",
    ["_bare_context", "_refuse_mismatch", "_bind_profile"],
)
def test_a_failure_while_building_the_run_still_releases_the_root(monkeypatch, target):
    """The root exists before `run` is entered, so nothing may happen outside the try."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    root = world.root
    assert root.exists()

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"controlled failure in {target}")

    monkeypatch.setattr(StackTestHarness, target, boom)
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT, Phase.PLAN))

    assert report.state is RunState.FAILED
    assert f"controlled failure in {target}" in report.detail
    assert not root.exists()
    assert world.managed_residual() == ()
    assert world.external_lost() == ()
    cleanup = phase_of(report, Phase.CLEANUP)
    assert cleanup.outcome is Outcome.PASSED
    assert report.context["cleanup"]["problems"] == []


def test_a_failure_while_building_the_run_never_deletes_a_configured_root(monkeypatch, tmp_path):
    persistent = tmp_path / "stack"
    persistent.mkdir()
    (persistent / "keep.txt").write_text("mine", encoding="utf-8")
    world = controlled_world(
        root=allocate_root(RootMode.CONFIGURED, configured=persistent),
        ready_ports=[OLLAMA_PORT],
    )

    def boom(*_args, **_kwargs):
        raise RuntimeError("controlled failure in _bare_context")

    monkeypatch.setattr(StackTestHarness, "_bare_context", boom)
    report = StackTestHarness(world).run(
        new_run_id(), spec(Phase.PREFLIGHT, root_mode=RootMode.CONFIGURED)
    )

    assert report.state is RunState.FAILED
    assert (persistent / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert "never deleted" in report.context["root"]["cleanup_detail"]


def test_a_run_that_never_built_a_context_still_reports_one(monkeypatch):
    world = controlled_world(ready_ports=[OLLAMA_PORT])

    def boom(*_args, **_kwargs):
        raise RuntimeError("controlled failure in _bare_context")

    monkeypatch.setattr(StackTestHarness, "_bare_context", boom)
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))

    assert report.context["provider_mode"] == "controlled"
    assert report.context["root"]["removed"] is True
    assert report.context["cleanup"]["problems"] == []


def test_a_launcher_that_started_something_is_released_without_a_context(monkeypatch):
    """Partially built runs still release what the world already holds."""
    world = controlled_world(ready_ports=[OLLAMA_PORT])
    registry_stub = type("R", (), {"register": lambda *a: None, "unregister": lambda *a: None})()
    launcher = world.launcher(registry_stub)
    launcher.start(type("S", (), {"service_id": "ollama", "executable": "ollama.exe"})())
    assert world.launcher_pid in world.inventory.pids()

    def boom(*_args, **_kwargs):
        raise RuntimeError("controlled failure in _bare_context")

    monkeypatch.setattr(StackTestHarness, "_bare_context", boom)
    report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))

    assert report.state is RunState.FAILED
    assert launcher.closed, "the launcher was never released"
    assert not world.root.exists()


def test_a_workflow_whose_model_roles_are_undeclared_also_fails_closed():
    """The other way a workflow can be unusable. Not the P0 row, still true."""
    from local_ai_stack.e2e.fixtures import BROKEN_WORKFLOW_PROFILE, repo_fixture

    fixture = repo_fixture(BROKEN_WORKFLOW_PROFILE)
    world = controlled_world(
        ready_ports=[OLLAMA_PORT],
        repo_root=fixture.path,
        ollama_models=[
            OllamaModelEntry(name="hermes3:8b", digest="4f6b83f30b62aaaa", size_bytes=1)
        ],
    )
    try:
        report = StackTestHarness(world).run(
            new_run_id(),
            RunSpec(
                profile_id=BROKEN_WORKFLOW_PROFILE,
                phases=(Phase.PREFLIGHT, Phase.PLAN, Phase.PROVISION, Phase.CONFIGURE),
            ),
        )
        configure = phase_of(report, Phase.CONFIGURE)
        assert configure.outcome is Outcome.FAILED
        assert "model-not-declared" in configure.detail
    finally:
        world.root.release()
        fixture.release()


# --- estado pós-crash e árvore de processos ---------------------------------


def test_the_second_observation_confirms_the_post_crash_state():
    """The first read can catch a transition; the second has to be settled."""
    result = ScenarioRunner().run(scenario_by_id("crash-after-start"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)

    stop = phase_of(result.report, Phase.STOP)
    settled = next(item for item in stop.data["confirmed"] if item["service"] == "ollama")
    assert settled["runtime"] != "RUNNING"
    assert settled["state"] != "UP"
    assert settled["stoppable"] is False
    assert settled["pid"] is None
    assert stop.data["registry"] == []


def test_the_crash_row_starts_from_a_service_that_was_really_up():
    result = ScenarioRunner().run(scenario_by_id("crash-after-start"))
    start = phase_of(result.report, Phase.START)
    assert start.data["ready"] is True
    first = next(item for item in phase_of(result.report, Phase.STOP).data["observed"])
    assert first["service"] == "ollama"


def test_stopping_ends_the_whole_managed_tree():
    setup = scenario_by_id("stop-cleanup").setup()
    try:
        report = StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        launcher = setup.world._launcher
        tree = launcher.members_started("ollama")
        assert len(tree) >= 2, "the row never started a tree"

        stop = phase_of(report, Phase.STOP)
        result = next(item for item in stop.data["stopped"] if item["service"] == "ollama")
        assert result["stopped_tree"] is True
        assert sorted(result["tree_pids"]) == sorted(tree)
        assert result["left_running"] == []

        alive = set(setup.world.inventory.pids())
        assert not set(tree) & alive
        assert stop.data["registry"] == []
        assert phase_of(report, Phase.CLEANUP).data["job_handles_released"] is True
    finally:
        setup.world.root.release()
        setup.teardown()


def test_a_stop_that_forgets_a_descendant_fails_the_row(monkeypatch):
    """The negative: leave one worker alive and the row must not pass."""
    from local_ai_stack.e2e.providers import ControlledLauncher

    original = ControlledLauncher.stop

    def stop_only_the_root(self, service_id, **kwargs):
        self.members[service_id] = (self.pid,)
        return original(self, service_id, **kwargs)

    monkeypatch.setattr(ControlledLauncher, "stop", stop_only_the_root)
    result = ScenarioRunner().run(scenario_by_id("stop-cleanup"))
    assert result.outcome is ScenarioOutcome.FAILED
    assert any("survived" in item or "tree" in item for item in result.diagnostics)


def test_neither_the_unrelated_nor_the_external_process_is_touched():
    from local_ai_stack.e2e.providers import EXTERNAL_PID, UNRELATED_PID

    setup = scenario_by_id("stop-cleanup").setup()
    try:
        StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        alive = set(setup.world.inventory.pids())
        assert UNRELATED_PID in alive
        assert EXTERNAL_PID in alive
        assert setup.world.external_lost() == ()
    finally:
        setup.world.root.release()
        setup.teardown()


def test_membership_is_answered_not_assumed():
    """A pid nobody started is never claimed as ours."""
    from local_ai_stack.e2e.providers import UNRELATED_PID
    from local_ai_stack.jobs import Membership

    setup = scenario_by_id("stop-cleanup").setup()
    try:
        StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        launcher = setup.world._launcher
        for member in launcher.members_started("ollama"):
            assert launcher.proved("ollama", member) is Membership.MEMBER
        assert launcher.proved("ollama", UNRELATED_PID) is Membership.NOT_MEMBER
        assert launcher.proved("nobody-started-this", 1) is Membership.UNPROVABLE
    finally:
        setup.world.root.release()
        setup.teardown()


# --- second run não aceita falhas internas ----------------------------------


def test_a_failed_provisioning_result_fails_the_second_run_row(monkeypatch):
    """Force provisioning to fail and the idempotence row must not pass."""
    from local_ai_stack.execution import ExecutionReport

    monkeypatch.setattr(ExecutionReport, "ok", property(lambda self: False))
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.FAILED


def test_a_failed_model_action_fails_the_second_run_row(monkeypatch):
    from local_ai_stack.e2e.providers import ControlledModelInstaller
    from local_ai_stack.model_installer import ModelOutcome, ModelResult

    original = ControlledModelInstaller.apply

    def refuse_one(self, spec, *, dry_run=False):
        result = original(self, spec, dry_run=dry_run)
        if str(spec.id) == "comfyui/e2e_vae":
            return ModelResult(
                model_id=result.model_id,
                outcome=ModelOutcome.FAILED,
                status=result.status,
                detail="controlled refusal",
            )
        return result

    monkeypatch.setattr(ControlledModelInstaller, "apply", refuse_one)
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.FAILED
    assert any("e2e_vae" in item or "acquire" in item for item in result.diagnostics)


def test_a_failed_result_during_the_second_pass_is_not_idempotence(monkeypatch):
    """Make only the dry-run pass fail: nothing proposed, but not ok either."""
    from local_ai_stack.e2e.providers import ControlledModelInstaller
    from local_ai_stack.model_installer import ModelOutcome, ModelResult

    original = ControlledModelInstaller.apply

    def fail_when_dry(self, spec, *, dry_run=False):
        result = original(self, spec, dry_run=dry_run)
        if dry_run:
            return ModelResult(
                model_id=result.model_id,
                outcome=ModelOutcome.FAILED,
                status=result.status,
                detail="dry fail",
            )
        return result

    monkeypatch.setattr(ControlledModelInstaller, "apply", fail_when_dry)
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.FAILED
    second = phase_of(result.report, Phase.SECOND_PLAN)
    assert second.outcome is Outcome.FAILED
    assert second.data["ok"] is False


# --- um PASSED não esconde resultado estruturado com ok=False ---------------


def _doctored(report, phase, **changes):
    """The same report with one phase's data altered, and nothing else."""
    from dataclasses import replace

    phases = tuple(
        replace(item, data={**item.data, **changes}) if item.phase is phase else item
        for item in report.phases
    )
    return replace(report, phases=phases)


@pytest.fixture(scope="module")
def clean_second_run():
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)
    return result.report


def test_the_clean_second_run_has_no_hidden_failures(clean_second_run):
    assert scenario_by_id("second-run").verdict(clean_second_run) == []


def test_a_passed_phase_with_provisioning_ok_false_fails_the_row(clean_second_run):
    doctored = _doctored(clean_second_run, Phase.PROVISION, ok=False)
    problems = scenario_by_id("second-run").verdict(doctored)
    assert any("ok=False" in item for item in problems)


def test_a_passed_phase_with_models_ok_false_fails_the_row(clean_second_run):
    doctored = _doctored(clean_second_run, Phase.ACQUIRE_MODELS, ok=False)
    problems = scenario_by_id("second-run").verdict(doctored)
    assert any("acquire_models passed while" in item for item in problems)


def test_a_passed_phase_with_a_failed_action_fails_the_row(clean_second_run):
    acquire = phase_of(clean_second_run, Phase.ACQUIRE_MODELS)
    actions = [dict(item) for item in acquire.data["actions"]]
    actions[0]["ok"] = False
    doctored = _doctored(clean_second_run, Phase.ACQUIRE_MODELS, actions=actions)
    problems = scenario_by_id("second-run").verdict(doctored)
    assert any("failed acquire-model" in item for item in problems)


def test_a_passed_phase_with_a_failed_component_fails_the_row(clean_second_run):
    doctored = _doctored(
        clean_second_run,
        Phase.PROVISION,
        results=[{"component": "ollama", "outcome": "failed"}],
    )
    problems = scenario_by_id("second-run").verdict(doctored)
    assert any("ollama reported failed" in item for item in problems)


def test_the_doctored_reports_also_fail_through_the_runner(clean_second_run, monkeypatch):
    """End to end: the runner, not just the verdict helper, refuses them."""
    from local_ai_stack.e2e import scenarios

    doctored = _doctored(clean_second_run, Phase.ACQUIRE_MODELS, ok=False)
    monkeypatch.setattr(scenarios.StackTestHarness, "run", lambda self, *a, **k: doctored)
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.FAILED


# --- cleanup antecipado remove a árvore managed, não só o root ---------------


def _started_world():
    """A world whose launcher already owns a root and a descendant."""
    from local_ai_stack.registry import ManagedProcessRegistry

    install = _external_for_cleanup()
    world = controlled_world(
        ready_ports=[OLLAMA_PORT],
        external_processes=[(11434, str(install.path))],
    )
    world.inventory.occupy(39999)
    world.external = install
    layout = world.root.installation.layout
    layout.ensure()
    registry = ManagedProcessRegistry.from_layout(layout)
    launcher = world.launcher(registry)
    spec_stub = type(
        "Spec", (), {"service_id": "ollama", "executable": "ollama.exe", "argv": ("ollama.exe",)}
    )()
    launcher.start(spec_stub)
    assert len(launcher.members["ollama"]) >= 2
    assert sorted(registry.load().entries) == ["ollama"]
    return world, registry, install


def _external_for_cleanup():
    from local_ai_stack.e2e.fixtures import external_install

    return external_install("ollama")


def assert_nothing_managed_survived(world, report):
    """Every resource the run owned is gone; nothing it did not own is."""
    from local_ai_stack.e2e.providers import EXTERNAL_PID, UNRELATED_PID

    launcher = world._launcher
    assert world.managed_residual() == (), f"managed pids survived: {world.managed_residual()}"
    assert launcher.members == {}, f"membership still held: {launcher.members}"
    for pid in launcher.members_started("ollama"):
        assert launcher.owns("ollama", pid) is not Membership.MEMBER
    cleanup = phase_of(report, Phase.CLEANUP)
    assert cleanup.data["membership"] == {}
    assert cleanup.data["registry"] == []
    assert cleanup.data["job_handles_released"] is True
    assert not world.root.exists()
    alive = set(world.inventory.pids())
    assert UNRELATED_PID in alive
    assert EXTERNAL_PID in alive


@pytest.mark.parametrize("target", ["_bare_context", "_refuse_mismatch", "_bind_profile"])
def test_an_early_failure_stops_a_managed_tree_that_was_already_running(monkeypatch, target):
    world, _registry, install = _started_world()
    try:

        def boom(*_args, **_kwargs):
            raise RuntimeError(f"controlled failure in {target}")

        monkeypatch.setattr(StackTestHarness, target, boom)
        report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))

        assert report.state is RunState.FAILED
        assert phase_of(report, Phase.CLEANUP).outcome is Outcome.PASSED
        assert_nothing_managed_survived(world, report)
    finally:
        install.release()


def test_an_early_cleanup_that_forgets_a_member_is_caught(monkeypatch):
    """The negative: stop only the root and the residual check must fail."""
    from local_ai_stack.e2e.providers import ControlledLauncher

    original = ControlledLauncher.stop

    def stop_only_the_root(self, service_id, **kwargs):
        self.members[service_id] = (self.pid,)
        return original(self, service_id, **kwargs)

    monkeypatch.setattr(ControlledLauncher, "stop", stop_only_the_root)
    world, _registry, install = _started_world()
    try:

        def boom(*_args, **_kwargs):
            raise RuntimeError("controlled failure in _bare_context")

        monkeypatch.setattr(StackTestHarness, "_bare_context", boom)
        report = StackTestHarness(world).run(new_run_id(), spec(Phase.PREFLIGHT))

        assert phase_of(report, Phase.CLEANUP).outcome is Outcome.FAILED
        with pytest.raises(AssertionError, match="managed pids survived"):
            assert_nothing_managed_survived(world, report)
    finally:
        install.release()


def test_closing_the_launcher_without_stopping_would_leave_the_tree_running():
    """Why cleanup stops first: close_all alone gives up proof, not processes."""
    world, _registry, install = _started_world()
    try:
        launcher = world._launcher
        tree = launcher.members["ollama"]
        launcher.close_all()
        assert launcher.members == {}
        assert set(tree) <= set(world.inventory.pids())
    finally:
        world.root.release()
        install.release()


# --- second run detecta aquisição que o domínio proporia ---------------------


def _remove_vae_between_passes(monkeypatch):
    """Delete one managed weight after acquisition, before the second pass."""
    from local_ai_stack.models import ManagedModelPaths

    original = StackTestHarness._phase_second_plan
    removed = []

    def second_plan_after_removal(self, context):
        catalog = self.world.model_catalog()
        vae = catalog.require(catalog.normalize("comfyui/e2e_vae"))
        path = ManagedModelPaths.from_layout(context.installation.layout).destination_for(vae)
        path.unlink()
        removed.append(path)
        return original(self, context)

    monkeypatch.setattr(StackTestHarness, "_phase_second_plan", second_plan_after_removal)
    return removed


def test_a_model_removed_between_passes_is_proposed_for_acquisition(monkeypatch):
    removed = _remove_vae_between_passes(monkeypatch)
    result = ScenarioRunner().run(scenario_by_id("second-run"))

    assert removed, "the weight was never removed"
    assert result.outcome is ScenarioOutcome.FAILED
    second = phase_of(result.report, Phase.SECOND_PLAN)
    assert second.outcome is Outcome.FAILED
    assert second.data["would_acquire"] == ["comfyui/e2e_vae"]
    steps = second.data["proposed_steps"]["comfyui/e2e_vae"]
    assert any(step.startswith("Download e2e-vae.safetensors") for step in steps)
    assert any("proposes work" in item for item in result.diagnostics)


def test_with_nothing_removed_the_second_pass_proposes_no_acquisition():
    result = ScenarioRunner().run(scenario_by_id("second-run"))
    assert result.outcome is ScenarioOutcome.PASSED, "; ".join(result.diagnostics)
    second = phase_of(result.report, Phase.SECOND_PLAN)
    assert second.data["would_acquire"] == []
    assert second.data["proposed_steps"] == {}


# --- stop libera a membership, não só os processos ---------------------------


def test_stop_cleanup_fails_if_the_job_is_never_forgotten(monkeypatch):
    """The negative: processes stopped, proof of ownership kept."""
    from local_ai_stack.e2e.providers import ControlledLauncher

    monkeypatch.setattr(ControlledLauncher, "forget_job", lambda self, service_id: None)
    result = ScenarioRunner().run(scenario_by_id("stop-cleanup"))

    assert result.outcome is ScenarioOutcome.FAILED
    assert any("membership" in item or "job ownership" in item for item in result.diagnostics), (
        result.diagnostics
    )


def test_after_stop_no_previously_managed_pid_is_still_owned():
    setup = scenario_by_id("stop-cleanup").setup()
    try:
        report = StackTestHarness(setup.world).run(new_run_id(), setup.spec)
        launcher = setup.world._launcher
        tree = launcher.members_started("ollama")
        assert len(tree) >= 2
        for pid in tree:
            assert launcher.owns("ollama", pid) is not Membership.MEMBER
        assert launcher.members == {}
        cleanup = phase_of(report, Phase.CLEANUP)
        assert cleanup.data["membership"] == {}
        assert cleanup.data["registry"] == []
        assert launcher.closed
    finally:
        setup.world.root.release()
        setup.teardown()
