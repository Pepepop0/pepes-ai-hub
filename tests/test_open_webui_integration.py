"""The Hub keeps its image chat filter installed in the Open WebUI it runs.

Composition over the controlled stack: when Open WebUI is up and the profile
wants image generation, the sync runs -- after the first start, on every later
start, on the Open WebUI action -- and its outcome is what the run reports.
A change counts as work done once; an unchanged filter keeps a second run a
no-op; a refusal or an unreachable instance is a warning, never a failure;
a profile without image generation never asks for it.
"""

from __future__ import annotations

from pathlib import Path

from first_run_support import controlled_stack, restarted_hub

from local_ai_stack.installation import detect_repo_root
from local_ai_stack.open_webui_filters import FilterSync, FilterSyncReport
from local_ai_stack.operation_contracts import OperationState

PEPE, TEST = "pepe", "test"


def shipped(profile_id: str) -> str:
    return (detect_repo_root() / "profiles" / f"{profile_id}.yaml").read_text(encoding="utf-8")


class RecordingSync:
    """The filter sync as a script: each call takes the next outcome."""

    def __init__(self, *outcomes: FilterSync) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Path, str]] = []

    def __call__(self, data_dir: Path, base_url: str) -> FilterSyncReport:
        self.calls.append((Path(data_dir), base_url))
        outcome = self.outcomes.pop(0) if self.outcomes else FilterSync.UNCHANGED
        return FilterSyncReport(outcome, f"pepito_image_chat: {outcome.value}")


def pepe_stack(sync: RecordingSync):
    built = controlled_stack(profile_yaml=shipped(PEPE), profile_id=PEPE, vram_mib=12227)
    built.app.deps.filter_sync = sync
    return built


def test_the_first_run_installs_the_filter_once_open_webui_answers():
    sync = RecordingSync(FilterSync.INSTALLED)
    built = pepe_stack(sync)
    try:
        events = []
        result = built.app.first_run("op-1", PEPE, emit=events.append)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert sync.calls == [(built.app.layout.open_webui_data, "http://127.0.0.1:8080")]
        report = built.app.last_first_run
        assert report.integrated == ["open-webui"] and not report.did_nothing
        details = [e.message for e in events if e.detail is not None]
        assert "Open WebUI: filtro de imagem instalado" in details
        # After Open WebUI was ready, before ComfyUI was started.
        order = [e.message for e in events if e.detail is not None or e.kind.value == "state"]
        assert order.index("Open WebUI pronto") < order.index(
            "Open WebUI: filtro de imagem instalado"
        )
        assert order.index("Open WebUI: filtro de imagem instalado") < order.index(
            "Iniciando ComfyUI"
        )
    finally:
        built.release()


def test_a_second_run_finds_the_filter_current_and_does_nothing():
    sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UNCHANGED)
    built = pepe_stack(sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        result = built.app.first_run("op-2", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
        assert built.app.last_first_run.integrated == []
        assert len(sync.calls) == 2
    finally:
        built.release()


def test_a_stale_filter_is_updated_by_a_reopened_hub_and_counted_once():
    sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UPDATED, FilterSync.UNCHANGED)
    built = pepe_stack(sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        again = restarted_hub(built)
        again.deps.filter_sync = sync
        assert again.first_run("op-2", PEPE).state is OperationState.SUCCEEDED
        assert again.last_first_run.integrated == ["open-webui"]
        assert not again.last_first_run.did_nothing
        assert again.first_run("op-3", PEPE).state is OperationState.SUCCEEDED
        assert again.last_first_run.did_nothing
    finally:
        built.release()


def test_start_stack_and_the_open_webui_action_keep_the_filter_current():
    sync = RecordingSync(FilterSync.INSTALLED)
    built = pepe_stack(sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert built.app.stop_stack("op-2", PEPE).state is OperationState.SUCCEEDED
        assert built.app.start_stack("op-3", PEPE).state is OperationState.SUCCEEDED
        launch = built.app.open_webui("op-4", PEPE)
        assert launch.ok
        assert len(sync.calls) == 3
    finally:
        built.release()


def test_an_unreachable_or_foreign_filter_is_a_warning_not_a_failure():
    sync = RecordingSync(FilterSync.UNAVAILABLE, FilterSync.REFUSED)
    built = pepe_stack(sync)
    try:
        result = built.app.first_run("op-1", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert "open-webui-integration" in [d.code for d in result.diagnostics]
        assert "open-webui-integration" in [w.code for w in built.app.last_first_run.warnings]
        started = built.app.start_stack("op-2", PEPE)
        assert started.state is OperationState.SUCCEEDED
        assert [d.code for d in started.diagnostics] == ["open-webui-integration"]
        assert (
            "outra função" in started.diagnostics[0].message
            or "refused" in started.diagnostics[0].message
        )
    finally:
        built.release()


def test_without_an_administrator_the_first_run_waits_quietly():
    sync = RecordingSync(FilterSync.NO_ADMIN)
    built = pepe_stack(sync)
    try:
        result = built.app.first_run("op-1", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert "open-webui-integration" not in [d.code for d in result.diagnostics]
        assert built.app.last_first_run.integrated == []
    finally:
        built.release()


def test_a_profile_without_image_generation_never_asks():
    sync = RecordingSync(FilterSync.INSTALLED)
    built = controlled_stack(profile_yaml=shipped(TEST), profile_id=TEST)
    built.app.deps.filter_sync = sync
    try:
        assert built.app.first_run("op-1", TEST).state is OperationState.SUCCEEDED
        assert built.app.start_stack("op-2", TEST).state is OperationState.SUCCEEDED
        assert sync.calls == []
    finally:
        built.release()
