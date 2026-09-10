"""Unit tests for the harness parts: metrics, seams, roots, providers.

Nothing here starts a process, opens a socket or writes outside tmp_path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from local_ai_stack.e2e.metrics import (
    KNOWN_REHASH_SECONDS,
    Measurement,
    Metric,
    MetricRecorder,
)
from local_ai_stack.e2e.providers import (
    ControlledArtifacts,
    ControlledInventory,
    ControlledOllama,
    ControlledRunner,
    ControlledViolation,
    StepClock,
    controlled_component_archive,
    controlled_facts,
)
from local_ai_stack.e2e.roots import RootError, allocate_root
from local_ai_stack.e2e.roots import TestRoot as StackRoot
from local_ai_stack.e2e.seams import SEAM_FOR, Injection, InjectionPlan
from local_ai_stack.test_contracts import TestRootMode as RootMode

SRC = Path(__file__).resolve().parents[2] / "src" / "local_ai_stack"


# --- metrics ---------------------------------------------------------------


def test_durations_come_from_the_injected_monotonic_clock():
    clock = StepClock()
    recorder = MetricRecorder(clock.monotonic)
    with recorder.measure(Metric.PLAN):
        clock.advance(2.5)
    sample = recorder.samples(Metric.PLAN)[0]
    assert sample.seconds == pytest.approx(2.5)


def test_a_measurement_cannot_be_negative():
    with pytest.raises(ValueError):
        Measurement(metric=Metric.PLAN, seconds=-1.0)


def test_a_failing_block_is_still_measured():
    """A phase that failed slowly is exactly the evidence worth keeping."""
    clock = StepClock()
    recorder = MetricRecorder(clock.monotonic)
    with pytest.raises(RuntimeError), recorder.measure(Metric.SERVICE_STARTUP):
        clock.advance(1.0)
        raise RuntimeError("boom")
    assert recorder.samples(Metric.SERVICE_STARTUP)[0].seconds == pytest.approx(1.0)


def test_context_added_inside_the_block_reaches_the_sample():
    recorder = MetricRecorder(StepClock().monotonic)
    with recorder.measure(Metric.MODEL_VERIFY, files=3) as extra:
        extra["bytes"] = 42
    context = recorder.samples(Metric.MODEL_VERIFY)[0].context
    assert context["files"] == 3
    assert context["bytes"] == 42


def test_the_summary_keeps_count_median_and_maximum():
    recorder = MetricRecorder(StepClock().monotonic)
    for seconds in (1.0, 5.0, 3.0):
        recorder.record(Metric.CHAT_SMOKE, seconds)
    summary = recorder.summary()[Metric.CHAT_SMOKE]
    assert summary.count == 3
    assert summary.median_seconds == 3.0
    assert summary.maximum_seconds == 5.0


def test_every_raw_observation_survives_summarising():
    """A slow outlier must stay visible, not be averaged away."""
    recorder = MetricRecorder(StepClock().monotonic)
    for seconds in (0.1, 0.1, 30.0):
        recorder.record(Metric.MODEL_VERIFY, seconds)
    data = recorder.as_report_data()
    assert len(data["samples"]) == 3
    assert 30.0 in [sample["seconds"] for sample in data["samples"]]
    assert data["summary"]["model-verify"]["maximum_seconds"] == 30.0


def test_the_known_rehash_baseline_is_reported_as_context_not_a_threshold():
    data = MetricRecorder().as_report_data()
    assert data["known_rehash_baseline"]["seconds"] == KNOWN_REHASH_SECONDS
    assert "threshold" not in str(data)


def test_the_recorder_caches_nothing():
    """This phase measures. Caching is a later decision with its own rules.

    Checked against the syntax tree, so the docstring may discuss the future
    cache without the test mistaking prose for an implementation.
    """
    tree = ast.parse((SRC / "e2e" / "metrics.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    assert not {"lru_cache", "cache", "cached_property"} & names


# --- seams -----------------------------------------------------------------


def test_every_injection_declares_the_seam_it_acts_through():
    assert set(SEAM_FOR) == set(Injection)


def test_an_injection_plan_names_itself_deterministically():
    plan = InjectionPlan.of(Injection.SERVICE_CRASH, Injection.PORT_OCCUPIED)
    assert plan.names == ("port-occupied", "service-crash")
    assert plan.names == InjectionPlan.of(Injection.PORT_OCCUPIED, Injection.SERVICE_CRASH).names


def test_an_empty_plan_is_falsy_and_names_nothing():
    plan = InjectionPlan.none()
    assert not plan
    assert plan.names == ()
    assert plan.as_report_data()["injections"] == []


def test_the_report_data_names_the_injection_and_its_seam():
    data = InjectionPlan.of(Injection.CHECKSUM_MISMATCH).as_report_data()
    assert data["injections"] == ["checksum-mismatch"]
    assert data["seams"] == ["http-transport"]


def test_no_production_module_imports_the_injection_seams():
    """Injection cannot exist in normal product behaviour."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if "e2e" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "seams" in node.module:
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                offenders.extend(path.name for alias in node.names if "e2e" in alias.name)
    assert offenders == []


# --- roots -----------------------------------------------------------------


def test_an_ephemeral_root_is_created_and_removed():
    root = allocate_root()
    assert root.exists() and root.removable
    root.release()
    assert not root.exists()
    assert root.removed


def test_a_configured_root_is_never_deleted(tmp_path):
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    (persistent / "keep.txt").write_text("mine", encoding="utf-8")
    root = allocate_root(RootMode.CONFIGURED, configured=persistent)

    assert not root.removable
    root.release()

    assert persistent.exists()
    assert (persistent / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert "never deleted" in root.cleanup_detail


def test_a_configured_root_must_be_named_and_absolute():
    with pytest.raises(RootError, match="explicit path"):
        allocate_root(RootMode.CONFIGURED)
    with pytest.raises(RootError, match="absolute"):
        allocate_root(RootMode.CONFIGURED, configured="relative/root")


def test_an_ephemeral_root_cannot_also_name_a_path(tmp_path):
    with pytest.raises(RootError):
        allocate_root(RootMode.EPHEMERAL, configured=tmp_path)


def test_releasing_a_missing_ephemeral_root_is_not_an_error():
    root = allocate_root()
    root.path.rmdir()
    root.release()
    assert root.removed


def test_the_installation_resolves_to_the_root(tmp_path):
    root = StackRoot(mode=RootMode.CONFIGURED, path=tmp_path / "stack", owned=False)
    assert root.installation.root == tmp_path / "stack"
    assert not (tmp_path / "stack").exists()


# --- controlled providers --------------------------------------------------


def test_an_unknown_url_is_a_violation_not_a_silent_404():
    artifacts = ControlledArtifacts()
    downloader_transport = artifacts.transport()
    import httpx

    with (
        pytest.raises(ControlledViolation, match="unknown URL"),
        httpx.Client(transport=downloader_transport) as client,
    ):
        client.get("https://nobody-declared-this.invalid/x")


def test_the_controlled_archive_is_a_real_zip():
    import io
    import zipfile

    payload = controlled_component_archive("ollama.exe")
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert "ollama.exe" in bundle.namelist()


def test_controlled_facts_can_drop_cuda():
    assert controlled_facts(cuda=True).gpus
    assert controlled_facts(cuda=False).gpus == ()
    assert controlled_facts(cuda=False).nvidia.smi_available is False


def test_the_controlled_store_never_removes_a_model():
    store = ControlledOllama(installed=[])
    store.pull("hermes3:8b")
    assert [entry.name for entry in store.list_models()] == ["hermes3:8b"]
    assert not hasattr(store, "delete")
    assert not hasattr(store, "remove")


def test_an_unreachable_store_raises_rather_than_lying():
    from local_ai_stack.ollama_models import OllamaClientError

    with pytest.raises(OllamaClientError):
        ControlledOllama(reachable=False).list_models()


def test_the_step_clock_never_sleeps_in_real_time():
    clock = StepClock()
    clock.sleep(30.0)
    assert clock.now >= 30.0
    assert clock.slept == 30.0


def test_the_inventory_tracks_what_survives():
    inventory = ControlledInventory()
    inventory.occupy(8080, pid=111)
    inventory.spawn(222, "managed.exe", 9090)
    assert inventory.pids() == (111, 222)
    inventory.kill(222)
    assert inventory.pids() == (111,)


def test_the_controlled_runner_answers_a_version_probe():
    result = ControlledRunner().run(["ollama.exe", "--version"])
    assert result.ok
    assert "0.33.2" in result.stdout


# --- repository fixture ----------------------------------------------------


def test_a_repo_fixture_carries_the_real_manifests_and_one_profile():
    from local_ai_stack.e2e.fixtures import CHAT_ONLY_PROFILE, repo_fixture
    from local_ai_stack.profiles import load_profiles
    from local_ai_stack.workflows import load_workflow_manifest

    fixture = repo_fixture(CHAT_ONLY_PROFILE)
    try:
        profiles = load_profiles(fixture.path / "profiles")
        assert [profile.id for profile in profiles] == [CHAT_ONLY_PROFILE]
        shipped = load_workflow_manifest(SRC.parents[1] / "manifests" / "workflows.yaml")
        copied = load_workflow_manifest(fixture.path / "manifests" / "workflows.yaml")
        assert sorted(copied) == sorted(shipped)
    finally:
        fixture.release()
    assert not fixture.path.exists()


def test_an_unknown_fixture_profile_fails_loudly():
    from local_ai_stack.e2e.fixtures import repo_fixture

    with pytest.raises(KeyError, match="unknown fixture profile"):
        repo_fixture("nobody-declared-this")


# --- the managed-config seam -----------------------------------------------


def _document(tmp_path):
    from local_ai_stack.managed_config import env_document

    return env_document(tmp_path / "open-webui.env", {"OLLAMA_BASE_URL": "http://127.0.0.1:21434"})


def test_the_config_seam_is_the_real_writer_when_nothing_is_injected(tmp_path):
    from local_ai_stack.e2e.providers import ControlledConfigWriter

    writer = ControlledConfigWriter()
    document = _document(tmp_path)
    writer.apply([document])
    assert writer.verify([document]).ok
    assert writer.tampered == []


def test_tampering_keeps_the_marker_and_breaks_the_digest(tmp_path):
    from local_ai_stack.e2e.providers import ControlledConfigWriter
    from local_ai_stack.managed_config import Ownership, read_managed

    writer = ControlledConfigWriter(InjectionPlan.of(Injection.CONFIG_TAMPERED))
    document = _document(tmp_path)
    applied = writer.apply([document])

    assert applied.ok and applied.written
    managed = read_managed(document.path)
    assert managed.ownership is Ownership.MANAGED
    assert managed.claimed_digest is not None
    assert not managed.intact

    report = writer.verify([document])
    assert not report.ok
    assert report.drifted == (document.path,)
    assert "edited after it was generated" in " ".join(report.problems)


def test_a_dry_run_tampers_with_nothing(tmp_path):
    from local_ai_stack.e2e.providers import ControlledConfigWriter

    writer = ControlledConfigWriter(InjectionPlan.of(Injection.CONFIG_TAMPERED))
    document = _document(tmp_path)
    writer.apply([document], dry_run=True)
    assert not document.path.exists()
    assert writer.tampered == []


def test_the_config_seam_does_not_shadow_the_domain_plan(tmp_path):
    """``plan`` belongs to the writer; the injections live under another name."""
    from local_ai_stack.e2e.providers import ControlledConfigWriter

    writer = ControlledConfigWriter(InjectionPlan.of(Injection.CONFIG_TAMPERED))
    assert callable(writer.plan)
    assert writer.plan([_document(tmp_path)]).changes
    assert writer.injections.active(Injection.CONFIG_TAMPERED)


def test_the_config_seam_computes_no_digest_of_its_own():
    """Detecting drift is the domain's job; this only changes bytes."""
    tree = ast.parse((SRC / "e2e" / "providers.py").read_text(encoding="utf-8"))
    writer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ControlledConfigWriter"
    )
    names = {node.attr for node in ast.walk(writer) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(writer) if isinstance(node, ast.Name)}
    assert not {"sha256", "content_digest", "hashlib", "md5"} & names


# --- temp dir ledger --------------------------------------------------------


def test_the_harness_tracks_every_temporary_directory_it_makes():
    from local_ai_stack.e2e import tracking

    path = tracking.make("pepito-e2e-ledger-")
    try:
        assert path in tracking.outstanding()
    finally:
        assert tracking.release([path]) == (path,)
    assert not path.exists()
    assert path not in tracking.outstanding()


def test_the_ledger_refuses_a_path_it_did_not_create(tmp_path):
    from local_ai_stack.e2e import tracking

    outsider = tmp_path / "not-ours"
    outsider.mkdir()
    with pytest.raises(ValueError, match="not created by this harness"):
        tracking.release([outsider])
    assert outsider.exists()


def test_releasing_a_root_stops_tracking_it():
    from local_ai_stack.e2e import tracking

    root = allocate_root()
    assert root.path in tracking.outstanding()
    root.release()
    assert root.path not in tracking.outstanding()


def test_a_repo_fixture_and_an_external_install_are_tracked_and_forgotten():
    from local_ai_stack.e2e import tracking
    from local_ai_stack.e2e.fixtures import CHAT_ONLY_PROFILE, external_install, repo_fixture

    fixture = repo_fixture(CHAT_ONLY_PROFILE)
    install = external_install("ollama")
    assert fixture.path in tracking.outstanding()
    assert install.base in tracking.outstanding()

    fixture.release()
    install.release()
    assert fixture.path not in tracking.outstanding()
    assert install.base not in tracking.outstanding()


def test_the_whole_p0_matrix_leaves_nothing_outstanding():
    """The delta a leak would show up in, measured over every row at once."""
    from local_ai_stack.e2e import tracking
    from local_ai_stack.e2e.scenarios import ScenarioRunner

    before = set(tracking.outstanding())
    ScenarioRunner().run_all()
    assert set(tracking.outstanding()) - before == set()
