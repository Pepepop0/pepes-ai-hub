"""The Hub keeps its Open WebUI configuration current in the instance it runs.

Composition over the controlled stack: when Open WebUI is up, the syncs the
profile needs run -- the image chat filter when it wants image generation,
the chat model record when it declares tools -- after the first start, on
every later start, on the Open WebUI action -- and their outcome is what the
run reports. A change counts as work done once; unchanged state keeps a second
run a no-op; a refusal or an unreachable instance is a warning, never a
failure; a profile without the feature never asks for it.
"""

from __future__ import annotations

from pathlib import Path

from first_run_support import controlled_stack, restarted_hub

from local_ai_stack.installation import detect_repo_root
from local_ai_stack.open_webui_filters import FilterSync, FilterSyncReport
from local_ai_stack.operation_contracts import OperationState

# The chat variant Capella adds to Open WebUI's model list, listed with the profile's
# version; it offers the "Alta qualidade", "Arte digital", "Fotorrealista" and "Buscar
# referências" switches. The main record (Qwen, no style slider) offers only the last.
from local_ai_stack.profiles import load_profile

_capella = load_profile(detect_repo_root() / "profiles/capella.yaml")
CAPELLA_PRESETS = tuple((v.id, f"{v.name} · v{_capella.version}") for v in _capella.variants)
SWITCHES = {
    "filter_ids": [
        "pepito_image_quality",
        "pepito_image_art",
        "pepito_image_photo",
        "pepito_image_refs",
    ]
}
MAIN_SWITCHES = {"filter_ids": ["pepito_image_refs"]}

PEPE, TEST = "pepe", "test"


def shipped(profile_id: str) -> str:
    return (detect_repo_root() / "profiles" / f"{profile_id}.yaml").read_text(encoding="utf-8")


class RecordingSync:
    """A sync as a script: each call takes the next outcome."""

    def __init__(self, *outcomes: FilterSync, subject: str = "pepito_image_chat") -> None:
        self.outcomes = list(outcomes)
        self.subject = subject
        self.calls: list[tuple] = []

    def __call__(self, data_dir: Path, base_url: str, *rest: str, **extra) -> FilterSyncReport:
        self.calls.append((Path(data_dir), base_url, *rest, *([extra] if extra else [])))
        outcome = self.outcomes.pop(0) if self.outcomes else FilterSync.UNCHANGED
        return FilterSyncReport(outcome, f"{self.subject}: {outcome.value}")


def pepe_stack(
    sync: RecordingSync,
    model_sync: RecordingSync | None = None,
    edit_sync: RecordingSync | None = None,
):
    built = controlled_stack(profile_yaml=shipped(PEPE), profile_id=PEPE, vram_mib=12227)
    built.app.deps.filter_sync = sync
    built.app.deps.model_sync = model_sync or RecordingSync(subject="hermes3:8b")
    built.app.deps.image_edit_sync = edit_sync or RecordingSync(subject="image editing")
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


def test_a_profile_without_image_generation_or_tools_never_asks():
    sync = RecordingSync(FilterSync.INSTALLED)
    model_sync = RecordingSync(FilterSync.INSTALLED, subject="hermes3:8b")
    import yaml
    from test_service_config import disabled_profile

    built = controlled_stack(profile_yaml=yaml.safe_dump(disabled_profile().raw), profile_id=TEST)
    edit_sync = RecordingSync(FilterSync.INSTALLED, subject="image editing")
    built.app.deps.filter_sync = sync
    built.app.deps.model_sync = model_sync
    built.app.deps.image_edit_sync = edit_sync
    built.app.deps.web_search_sync = RecordingSync(subject="web search")
    try:
        assert built.app.first_run("op-1", TEST).state is OperationState.SUCCEEDED
        assert built.app.start_stack("op-2", TEST).state is OperationState.SUCCEEDED
        assert sync.calls == [] and model_sync.calls == [] and edit_sync.calls == []
    finally:
        built.release()


# --- the chat model record: Pepe declares tools -----------------------------------


def test_pepe_configures_its_chat_model_for_tool_calling_once_open_webui_answers():
    sync = RecordingSync(FilterSync.UNCHANGED)
    model_sync = RecordingSync(FilterSync.INSTALLED, subject="hermes3:8b")
    built = pepe_stack(sync, model_sync)
    try:
        events = []
        result = built.app.first_run("op-1", PEPE, emit=events.append)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        # The Ollama tag the catalog pins for models.chat.primary, not the profile's id,
        # listed with the profile's version; Pepe declares no preset to keep.
        assert model_sync.calls == [
            (
                built.app.layout.open_webui_data,
                "http://127.0.0.1:8080",
                "hermes3:8b",
                {"name": "Pepe · v1.2.0", "presets": []},
            )
        ]
        report = built.app.last_first_run
        assert report.integrated == ["open-webui"] and not report.did_nothing
        details = [e.message for e in events if e.detail is not None]
        assert "Open WebUI: modelo de chat instalado" in details
        assert "Open WebUI: filtro de imagem já atual" in details
    finally:
        built.release()


def test_capella_asks_its_chat_model_not_to_think_through_the_same_record():
    """The profile's models.chat.params reach the record sync, beside the versioned name
    and the presets to keep."""
    from first_run_support import CAPELLA, CHAT_REFERENCE, RTX_5090_MIB, capella_yaml

    sync = RecordingSync(FilterSync.UNCHANGED)
    model_sync = RecordingSync(FilterSync.INSTALLED, subject=CHAT_REFERENCE)
    built = controlled_stack(profile_yaml=capella_yaml(), profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.filter_sync = sync
    built.app.deps.model_sync = model_sync
    built.app.deps.image_edit_sync = RecordingSync(subject="image edit")
    built.app.deps.web_search_sync = RecordingSync(subject="web search")
    try:
        result = built.app.first_run("op-1", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert model_sync.calls == [
            (
                built.app.layout.open_webui_data,
                "http://127.0.0.1:8080",
                CHAT_REFERENCE,
                {
                    "name": "Capella · v5.2.0",
                    "presets": [record_id for record_id, _name in CAPELLA_PRESETS],
                    "params": {"think": False, "num_ctx": 16384},
                    "system_prompt": built.app.profile(CAPELLA).chat_system_prompt,
                    "default_feature_ids": ["image_generation"],
                    **MAIN_SWITCHES,
                },
            ),
            *(
                (
                    built.app.layout.open_webui_data,
                    "http://127.0.0.1:8080",
                    CHAT_REFERENCE,
                    {
                        "record_id": record_id,
                        "name": name,
                        "params": {"think": False, "num_ctx": 16384},
                        "system_prompt": built.app.profile(CAPELLA).chat_system_prompt,
                        "default_feature_ids": ["image_generation"],
                        **SWITCHES,
                    },
                )
                for record_id, name in CAPELLA_PRESETS
            ),
        ]
    finally:
        built.release()


def test_capella_owns_open_webui_web_search_and_reads_the_hubs_settings_when_the_sync_runs():
    from first_run_support import CAPELLA, RTX_5090_MIB, capella_yaml

    from local_ai_stack.web_search_settings import WebSearchSettings

    web = RecordingSync(FilterSync.INSTALLED, subject="web search")
    built = controlled_stack(profile_yaml=capella_yaml(), profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.filter_sync = RecordingSync(FilterSync.UNCHANGED)
    built.app.deps.model_sync = RecordingSync(subject="model")
    built.app.deps.image_edit_sync = RecordingSync(subject="image edit")
    built.app.deps.web_search_sync = web
    chosen = WebSearchSettings(False, "http://localhost:8888/search?q=<query>")
    built.app.deps.web_search_settings = lambda: chosen
    try:
        result = built.app.first_run("op-1", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        ((data_dir, base_url, settings),) = web.calls
        assert (data_dir, base_url) == (built.app.layout.open_webui_data, "http://127.0.0.1:8080")
        assert settings() is chosen, "the settings are read when the sync runs"
    finally:
        built.release()


def test_reference_provider_keys_reach_only_the_managed_open_webui_process():
    from first_run_support import CAPELLA, RTX_5090_MIB, capella_yaml

    from local_ai_stack.web_search_settings import WebSearchSettings

    built = controlled_stack(profile_yaml=capella_yaml(), profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.web_search_settings = lambda: WebSearchSettings(
        True, "", None, "unsplash-secret", "pexels-secret"
    )
    try:
        result = built.app.first_run("op-1", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        controller = built.app._controller(built.app.profile(CAPELLA))
        open_webui = controller.spec_for("open-webui").launch
        assert open_webui is not None
        assert open_webui.env["PEPITO_UNSPLASH_KEY"] == "unsplash-secret"
        assert open_webui.env["PEPITO_PEXELS_KEY"] == "pexels-secret"
        for service_id in ("ollama", "comfyui"):
            launch = controller.spec_for(service_id).launch
            assert launch is not None
            assert "PEPITO_UNSPLASH_KEY" not in launch.env
            assert "PEPITO_PEXELS_KEY" not in launch.env
    finally:
        built.release()


def test_a_profile_that_does_not_opt_in_leaves_open_webui_web_search_alone():
    web = RecordingSync(subject="web search")
    built = pepe_stack(RecordingSync(FilterSync.INSTALLED))
    built.app.deps.web_search_sync = web
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert web.calls == []
    finally:
        built.release()


def test_capella_quiets_open_webui_background_tasks_once_open_webui_answers():
    """``runtime.quiet_background_tasks`` asks Open WebUI to stop follow-ups and tags."""
    from first_run_support import CAPELLA, RTX_5090_MIB, capella_yaml

    quiet = RecordingSync(FilterSync.UPDATED, FilterSync.UNCHANGED, subject="background tasks")
    built = controlled_stack(profile_yaml=capella_yaml(), profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.filter_sync = RecordingSync(FilterSync.UNCHANGED)
    built.app.deps.model_sync = RecordingSync(subject="model")
    built.app.deps.image_edit_sync = RecordingSync(subject="image edit")
    built.app.deps.web_search_sync = RecordingSync(subject="web search")
    built.app.deps.background_task_sync = quiet
    try:
        events = []
        result = built.app.first_run("op-1", CAPELLA, emit=events.append)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert quiet.calls == [(built.app.layout.open_webui_data, "http://127.0.0.1:8080")]
        details = [e.message for e in events if e.detail is not None]
        assert any(d.startswith("Open WebUI: tarefas em segundo plano") for d in details)
        assert built.app.first_run("op-2", CAPELLA).state is OperationState.SUCCEEDED
        assert len(quiet.calls) == 2
    finally:
        built.release()


def test_a_profile_that_does_not_opt_in_leaves_open_webui_background_tasks_alone():
    quiet = RecordingSync(subject="background tasks")
    built = pepe_stack(RecordingSync(FilterSync.INSTALLED))
    built.app.deps.background_task_sync = quiet
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert quiet.calls == []
    finally:
        built.release()


def test_declared_chat_parameters_are_synced_even_with_tools_off():
    """models.chat.params is a request-time contract of its own: a profile that
    declares one gets its record written whether or not it enables tools."""
    from first_run_support import CAPELLA, CHAT_REFERENCE, RTX_5090_MIB, capella_yaml

    yaml_text = capella_yaml().replace("  tools: true\n", "  tools: false\n")
    assert "tools: false" in yaml_text
    model_sync = RecordingSync(FilterSync.INSTALLED, subject=CHAT_REFERENCE)
    built = controlled_stack(profile_yaml=yaml_text, profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.filter_sync = RecordingSync(FilterSync.UNCHANGED)
    built.app.deps.model_sync = model_sync
    built.app.deps.image_edit_sync = RecordingSync(subject="image edit")
    built.app.deps.web_search_sync = RecordingSync(subject="web search")
    try:
        result = built.app.first_run("op-1", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert model_sync.calls == [
            (
                built.app.layout.open_webui_data,
                "http://127.0.0.1:8080",
                CHAT_REFERENCE,
                {
                    "name": "Capella · v5.2.0",
                    "presets": [record_id for record_id, _name in CAPELLA_PRESETS],
                    "params": {"think": False, "num_ctx": 16384},
                    "system_prompt": built.app.profile(CAPELLA).chat_system_prompt,
                    "default_feature_ids": ["image_generation"],
                    **MAIN_SWITCHES,
                },
            ),
            *(
                (
                    built.app.layout.open_webui_data,
                    "http://127.0.0.1:8080",
                    CHAT_REFERENCE,
                    {
                        "record_id": record_id,
                        "name": name,
                        "params": {"think": False, "num_ctx": 16384},
                        "system_prompt": built.app.profile(CAPELLA).chat_system_prompt,
                        "default_feature_ids": ["image_generation"],
                        **SWITCHES,
                    },
                )
                for record_id, name in CAPELLA_PRESETS
            ),
        ]
    finally:
        built.release()


def test_a_profile_that_declares_neither_tools_nor_parameters_leaves_the_record_alone():
    """Nothing to configure on the record, so it is never touched."""
    import yaml
    from first_run_support import CAPELLA, CHAT_REFERENCE, RTX_5090_MIB, capella_yaml

    data = yaml.safe_load(capella_yaml())
    data["features"]["tools"] = False
    for key in ("params", "system_prompt"):
        data["models"]["chat"].pop(key)
    data.pop("variants", None)
    data["runtime"]["image_router"] = False
    yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    model_sync = RecordingSync(FilterSync.INSTALLED, subject=CHAT_REFERENCE)
    built = controlled_stack(profile_yaml=yaml_text, profile_id=CAPELLA, vram_mib=RTX_5090_MIB)
    built.app.deps.filter_sync = RecordingSync(FilterSync.UNCHANGED)
    built.app.deps.model_sync = model_sync
    built.app.deps.image_edit_sync = RecordingSync(subject="image edit")
    built.app.deps.web_search_sync = RecordingSync(subject="web search")
    try:
        assert built.app.first_run("op-1", CAPELLA).state is OperationState.SUCCEEDED
        assert model_sync.calls == []
    finally:
        built.release()


def test_a_current_chat_model_record_keeps_a_second_run_a_no_op():
    sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UNCHANGED)
    model_sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UNCHANGED, subject="hermes3:8b")
    built = pepe_stack(sync, model_sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert built.app.first_run("op-2", PEPE).state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
        assert len(model_sync.calls) == 2
    finally:
        built.release()


def test_a_chat_model_record_turned_back_on_is_repaired_and_counted_once():
    sync = RecordingSync(FilterSync.INSTALLED)
    model_sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UPDATED, subject="hermes3:8b")
    built = pepe_stack(sync, model_sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        again = restarted_hub(built)
        again.deps.filter_sync = sync
        again.deps.model_sync = model_sync
        assert again.first_run("op-2", PEPE).state is OperationState.SUCCEEDED
        assert again.last_first_run.integrated == ["open-webui"]
        assert not again.last_first_run.did_nothing
    finally:
        built.release()


def test_somebody_elses_chat_model_record_is_a_warning_not_a_failure():
    sync = RecordingSync(FilterSync.UNCHANGED)
    model_sync = RecordingSync(FilterSync.REFUSED, subject="hermes3:8b")
    built = pepe_stack(sync, model_sync)
    try:
        result = built.app.first_run("op-1", PEPE)
        assert result.state is OperationState.SUCCEEDED
        warning = next(d for d in result.diagnostics if d.code == "open-webui-integration")
        assert warning.message == "hermes3:8b: refused"
        assert built.app.last_first_run.integrated == []
    finally:
        built.release()


# --- image editing: the edit workflow's settings, synced like the rest ---------------------


def test_pepe_points_open_webui_image_editing_at_its_edit_workflow():
    edit_sync = RecordingSync(FilterSync.INSTALLED, subject="image editing")
    built = pepe_stack(RecordingSync(FilterSync.UNCHANGED), edit_sync=edit_sync)
    try:
        events = []
        result = built.app.first_run("op-1", PEPE, emit=events.append)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert len(edit_sync.calls) == 1
        data_dir, base_url, settings = edit_sync.calls[0]
        assert (data_dir, base_url) == (built.app.layout.open_webui_data, "http://127.0.0.1:8080")
        assert settings.base_url == "http://127.0.0.1:8188"
        # The diffusion model's runtime name from the (controlled) catalog, never a path.
        assert settings.model.endswith(".safetensors") and "/" not in settings.model
        assert [node["type"] for node in settings.nodes] == ["image", "model", "prompt", "seed"]
        assert settings.workflow["76"]["class_type"] == "LoadImage"
        report = built.app.last_first_run
        assert report.integrated == ["open-webui"] and not report.did_nothing
        assert report.unmapped_workflows == []
        assert not any(w.code == "workflow-not-mapped" for w in report.warnings)
        details = [e.message for e in events if e.detail is not None]
        assert "Open WebUI: edição de imagem instalado" in details
        assert "Workflow aplicado: image_edit" in details
    finally:
        built.release()


def test_current_image_edit_settings_keep_a_second_run_a_no_op():
    edit_sync = RecordingSync(FilterSync.INSTALLED, FilterSync.UNCHANGED, subject="image editing")
    built = pepe_stack(
        RecordingSync(FilterSync.INSTALLED, FilterSync.UNCHANGED), edit_sync=edit_sync
    )
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert built.app.first_run("op-2", PEPE).state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
        assert len(edit_sync.calls) == 2
    finally:
        built.release()


def test_drifted_image_edit_settings_are_repaired_and_counted_once():
    edit_sync = RecordingSync(
        FilterSync.INSTALLED, FilterSync.UPDATED, FilterSync.UNCHANGED, subject="image editing"
    )
    built = pepe_stack(RecordingSync(FilterSync.UNCHANGED), edit_sync=edit_sync)
    try:
        assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
        assert built.app.first_run("op-2", PEPE).state is OperationState.SUCCEEDED
        assert built.app.last_first_run.integrated == ["open-webui"]
        assert built.app.first_run("op-3", PEPE).state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
    finally:
        built.release()


def test_somebody_elses_image_edit_configuration_is_a_warning_not_a_failure():
    edit_sync = RecordingSync(FilterSync.REFUSED, subject="image editing")
    built = pepe_stack(RecordingSync(FilterSync.UNCHANGED), edit_sync=edit_sync)
    try:
        result = built.app.first_run("op-1", PEPE)
        assert result.state is OperationState.SUCCEEDED
        warning = next(d for d in result.diagnostics if d.code == "open-webui-integration")
        assert warning.message == "image editing: refused"
        assert built.app.last_first_run.integrated == []
    finally:
        built.release()


def test_an_image_edit_workflow_still_unmapped_is_not_synced():
    from dataclasses import replace

    edit_sync = RecordingSync(FilterSync.INSTALLED, subject="image editing")
    built = pepe_stack(RecordingSync(FilterSync.UNCHANGED), edit_sync=edit_sync)
    try:
        profile = built.app.profile(PEPE)
        handlers = built.app.install_actions(profile)
        specs = dict(handlers.workflow_specs)
        specs["flux2_klein_edit"] = replace(specs["flux2_klein_edit"], unresolved=("image",))
        original = built.app.install_actions

        def drafting(p):
            actions = original(p)
            return replace(actions, workflow_specs=specs)

        built.app.install_actions = drafting
        result = built.app.first_run("op-1", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert edit_sync.calls == []
        assert built.app.last_first_run.unmapped_workflows == ["image_edit"]
    finally:
        built.release()
