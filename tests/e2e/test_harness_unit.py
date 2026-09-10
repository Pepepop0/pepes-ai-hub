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
from local_ai_stack.e2e.roots import RootError, TestRoot, allocate_root
from local_ai_stack.e2e.seams import SEAM_FOR, Injection, InjectionPlan
from local_ai_stack.test_contracts import TestRootMode

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
    root = allocate_root(TestRootMode.CONFIGURED, configured=persistent)

    assert not root.removable
    root.release()

    assert persistent.exists()
    assert (persistent / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert "never deleted" in root.cleanup_detail


def test_a_configured_root_must_be_named_and_absolute():
    with pytest.raises(RootError, match="explicit path"):
        allocate_root(TestRootMode.CONFIGURED)
    with pytest.raises(RootError, match="absolute"):
        allocate_root(TestRootMode.CONFIGURED, configured="relative/root")


def test_an_ephemeral_root_cannot_also_name_a_path(tmp_path):
    with pytest.raises(RootError):
        allocate_root(TestRootMode.EPHEMERAL, configured=tmp_path)


def test_releasing_a_missing_ephemeral_root_is_not_an_error():
    root = allocate_root()
    root.path.rmdir()
    root.release()
    assert root.removed


def test_the_installation_resolves_to_the_root(tmp_path):
    root = TestRoot(mode=TestRootMode.CONFIGURED, path=tmp_path / "stack", owned=False)
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
