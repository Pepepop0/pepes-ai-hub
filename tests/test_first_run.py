"""The first-run flow, end to end, against controlled providers.

Capella is loaded verbatim from the shipped profile. The image
model ids are Capella's own; the catalog just points them at small weights.

No network, no GPU, no real process. Every test cleans up after itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from first_run_support import (
    CAPELLA,
    CAPELLA_PORTS,
    CHAT_REFERENCE,
    RTX_5090_MIB,
    capella_yaml,
    controlled_stack,
)

from local_ai_stack.e2e import tracking
from local_ai_stack.e2e.fixtures import (
    SHIPPED_IMAGE_MODELS,
)
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.integration import IntegrationOptions
from local_ai_stack.operation_contracts import (
    OperationEvent,
    OperationEventKind,
    OperationPhase,
    OperationState,
    advance_operation,
)
from local_ai_stack.stack_application import (
    OperationBusy,
)


@pytest.fixture
def stack():
    built = controlled_stack()
    yield built
    built.release()


class Recorder:
    """Collects events and re-validates them with the frozen reducer."""

    def __init__(self) -> None:
        self.events: list[OperationEvent] = []
        self.snapshot = None

    def __call__(self, event: OperationEvent) -> None:
        self.snapshot = advance_operation(self.snapshot, event)
        self.events.append(event)

    @property
    def messages(self) -> list[str]:
        return [event.message for event in self.events]

    @property
    def states(self) -> list[OperationState]:
        return [event.state for event in self.events]


# --- o fluxo feliz: Capella → plano → instalar → configurar → subir → pronto ---


def test_capella_first_run_ends_ready(stack):
    recorder = Recorder()
    result = stack.app.first_run("op-1", CAPELLA, emit=recorder)

    assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
    assert recorder.events[-1].kind is OperationEventKind.RESULT
    assert recorder.events[-1].message == "Tudo pronto"
    assert recorder.states[0] is OperationState.PREPARING
    assert OperationState.RUNNING in recorder.states
    assert OperationState.FINALIZING in recorder.states

    status = stack.app.status(CAPELLA)
    assert status.installation_ready, status.pending_work
    assert status.profile_id == CAPELLA
    assert status.stack_up
    assert status.gpu.total_mib == RTX_5090_MIB


def test_the_first_run_says_what_it_is_doing(stack):
    recorder = Recorder()
    stack.app.first_run("op-1", CAPELLA, emit=recorder)
    messages = recorder.messages
    for expected in (
        "Verificando ambiente",
        "Instalando Ollama",
        "Iniciando Ollama",
        "Baixando modelos",
        "Aplicando configurações",
        "Inicializando serviços",
        "Tudo pronto",
    ):
        assert expected in messages, messages


def test_the_first_run_really_installed_acquired_and_started(stack):
    stack.app.first_run("op-1", CAPELLA)
    # Ollama's archive was downloaded and extracted for real.
    assert any("/ollama/" in url for url in stack.artifacts.requests)
    # The three Capella weights were downloaded and verified against the catalog.
    assert (
        sum(
            url.startswith("https://controlled.invalid/models/") for url in stack.artifacts.requests
        )
        == 3
    )
    # The chat model was pulled into the store.
    assert stack.ollama.pulls == [CHAT_REFERENCE]
    # And the services were started through the controller, in order.
    assert stack.launcher.starts == ["ollama", "open-webui", "comfyui"]
    layout = stack.app.layout
    assert (layout.root / "data" / "open-webui" / "open-webui.env").exists()


def test_nothing_in_the_first_run_is_hardcoded_for_capella(stack):
    """Every value the flow used came from the profile, the manifests or a domain."""
    stack.app.first_run("op-1", CAPELLA)
    profile = stack.app.profile(CAPELLA)
    endpoints = stack.app.options.endpoints_for(profile)
    for service_id, port in CAPELLA_PORTS.items():
        assert endpoints.port(service_id) == port
    requested = {url.rsplit("/", 1)[-1] for url in stack.artifacts.requests}
    for _role, _name, _kind, _placement, filename in SHIPPED_IMAGE_MODELS:
        assert filename in requested
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "local_ai_stack").glob("*.py")
    for path in source:
        if path.name not in ("first_run.py", "stack_application.py", "operations.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
        assert "5090" not in " ".join(str(item) for item in literals)
        assert not any(
            isinstance(item, str) and item.startswith(("http://", "https://")) for item in literals
        )
        assert not any(isinstance(item, str) and "flux" in item.lower() for item in literals)


# --- segundo run: nada a fazer -----------------------------------------------


def test_a_second_first_run_does_nothing_and_still_ends_ready(stack):
    stack.app.first_run("op-1", CAPELLA)
    downloads = len(stack.artifacts.requests)
    pulls = list(stack.ollama.pulls)
    starts = list(stack.launcher.starts)
    env = stack.app.layout.root / "data" / "open-webui" / "open-webui.env"
    before = env.read_bytes()

    recorder = Recorder()
    result = stack.app.first_run("op-2", CAPELLA, emit=recorder)

    assert result.state is OperationState.SUCCEEDED
    assert recorder.events[-1].message == "Tudo pronto (nada a fazer)"
    assert len(stack.artifacts.requests) == downloads, "a second run downloaded again"
    assert stack.ollama.pulls == pulls, "a second run pulled again"
    assert stack.launcher.starts == starts, "a second run started services again"
    assert env.read_bytes() == before, "a second run rewrote valid configuration"
    assert stack.app.status(CAPELLA).installation_ready


# --- progresso de transferência ---------------------------------------------


def _transfer_events(recorder: Recorder):
    return [event for event in recorder.events if event.transfer is not None]


def test_model_downloads_publish_transfer_progress_with_a_known_total(stack):
    recorder = Recorder()
    stack.app.first_run("op-1", CAPELLA, emit=recorder)

    transfers = _transfer_events(recorder)
    assert transfers, "no transfer progress was published for a run that really downloaded"
    # Every controlled artifact is served as a plain byte body, so httpx always
    # knows its length; nothing here should ever report an unknown total.
    assert all(event.transfer.total_bytes is not None for event in transfers)
    assert all(event.transfer.completed_bytes >= 0 for event in transfers)
    # Grouped by which model each event's message names, the first and last
    # observation for every downloaded artifact must be present, and the last
    # one must show the transfer as complete.
    by_target: dict[str, list] = {}
    for event in transfers:
        by_target.setdefault(event.message, []).append(event.transfer)
    assert len(by_target) == 3, "Capella declares three file-backed image models"
    for message, observations in by_target.items():
        last = observations[-1]
        assert last.completed_bytes == last.total_bytes, message
        assert last.fraction == 1.0, message


def test_transfer_progress_coexists_with_the_logical_step_counter(stack):
    """A model download reports bytes while retaining its separate step count."""
    recorder = Recorder()
    stack.app.first_run("op-1", CAPELLA, emit=recorder)

    logical = [event for event in recorder.events if event.progress is not None]
    transfers = _transfer_events(recorder)
    assert logical and transfers
    assert all(event.progress is not None for event in transfers)
    assert all(event.progress.total == 4 for event in transfers)
    assert all(0 <= event.progress.completed < event.progress.total for event in transfers)
    assert any(event.transfer is None for event in logical)


def test_a_second_run_publishes_no_transfer_progress_at_all(stack):
    stack.app.first_run("op-1", CAPELLA)

    recorder = Recorder()
    result = stack.app.first_run("op-2", CAPELLA, emit=recorder)

    assert result.state is OperationState.SUCCEEDED
    assert recorder.events[-1].message == "Tudo pronto (nada a fazer)"
    assert _transfer_events(recorder) == []


def test_an_interrupted_download_ends_failed_with_no_transfer_event_after_the_result():
    built = controlled_stack(injections=InjectionPlan.of(Injection.DOWNLOAD_INTERRUPTED))
    try:
        _result, recorder = _first_failed(built)
        assert recorder.events[-1].message == "Falha ao baixar modelos"
        # The reducer only ever accepts one terminal event; a stray publish
        # after failure would have raised OperationClosed instead of quietly
        # appearing after it, but the count is checked directly all the same.
        terminal = [event for event in recorder.events if event.result is not None]
        assert len(terminal) == 1
        assert recorder.events.index(terminal[0]) == len(recorder.events) - 1
        # The interrupted artifact really did deliver some bytes before dying,
        # so this is not "no download was attempted" but "no invented total".
        transfers = _transfer_events(recorder)
        assert transfers
        assert all(
            event.transfer.completed_bytes < event.transfer.total_bytes for event in transfers
        )
    finally:
        built.release()


def test_a_checksum_failure_reports_no_transfer_event_after_the_result():
    built = controlled_stack(injections=InjectionPlan.of(Injection.CHECKSUM_MISMATCH))
    try:
        _result, recorder = _first_failed(built)
        terminal = [event for event in recorder.events if event.result is not None]
        assert len(terminal) == 1
        assert recorder.events.index(terminal[0]) == len(recorder.events) - 1
    finally:
        built.release()


def test_ollama_pull_publishes_no_byte_transfer_progress(stack):
    """Ollama's non-streaming pull has no byte-level source; it stays logical."""
    recorder = Recorder()
    stack.app.first_run("op-1", CAPELLA, emit=recorder)

    transfers = _transfer_events(recorder)
    assert not any(CHAT_REFERENCE in event.message for event in transfers)
    assert not any("ollama/" in event.message for event in transfers)


# --- perfis ---------------------------------------------------------------------


def test_the_shipped_capella_profile_has_a_resolvable_primary_chat_model():
    built = controlled_stack(profile_yaml=capella_yaml())
    try:
        profile = built.app.profile(CAPELLA)
        assert profile.models["chat"]["primary"] == "ollama/qwen3_5_9b"
        assert not built.app.unresolved_models(profile)
        result = built.app.first_run("op-shipped", CAPELLA)
        assert result.state is OperationState.SUCCEEDED
        assert built.app.status(CAPELLA).installation_ready
    finally:
        built.release()


@pytest.mark.parametrize("profile_id", ["pepe", "test"])
def test_other_shipped_profiles_are_accepted_by_the_flow(profile_id):
    """They load and are planned; whether they can finish is their own data's business."""
    text = (detect_repo_root() / "profiles" / f"{profile_id}.yaml").read_text(encoding="utf-8")
    built = controlled_stack(profile_yaml=text, profile_id=profile_id, vram_mib=RTX_5090_MIB)
    try:
        result = built.app.first_run("op-1", profile_id)
        assert result.state in (OperationState.SUCCEEDED, OperationState.FAILED)
        assert built.app.status(profile_id).profile_id == profile_id
    finally:
        built.release()


def test_an_unknown_profile_is_refused_before_any_operation(stack):
    from local_ai_stack.stack_application import UnknownProfile

    with pytest.raises(UnknownProfile):
        stack.app.first_run("op-1", "nobody")
    assert stack.app.current_operation() is None


# --- eventos --------------------------------------------------------------------


def test_events_form_one_valid_stream_and_nothing_follows_the_result(stack):
    recorder = Recorder()  # re-validates with advance_operation on every event
    stack.app.first_run("op-1", CAPELLA, emit=recorder)
    sequences = [event.sequence for event in recorder.events]
    assert sequences == list(range(1, len(sequences) + 1))
    terminal = [event for event in recorder.events if event.state.terminal]
    assert len(terminal) == 1 and terminal[0] is recorder.events[-1]
    assert stack.app.current_operation().last_event is recorder.events[-1]


def test_an_observer_that_raises_cannot_change_the_outcome(stack):
    def broken(_event):
        raise RuntimeError("consumer bug")

    result = stack.app.first_run("op-1", CAPELLA, emit=broken)
    assert result.state is OperationState.SUCCEEDED


def test_subscribe_replays_the_latest_event_then_follows(stack):
    stack.app.first_run("op-1", CAPELLA)
    replayed = []
    subscription = stack.app.subscribe(replayed.append)
    assert [event.operation_id for event in replayed] == ["op-1"]
    assert replayed[0].state.terminal

    stack.app.first_run("op-2", CAPELLA)
    assert replayed[-1].operation_id == "op-2"
    subscription.close()
    stack.app.first_run("op-3", CAPELLA)
    assert replayed[-1].operation_id == "op-2"


def test_only_one_operation_runs_at_a_time(stack):
    seen = []

    def reentrant(event):
        if not seen:
            seen.append(event)
            with pytest.raises(OperationBusy):
                stack.app.start_stack("op-nested", CAPELLA)

    result = stack.app.first_run("op-1", CAPELLA, emit=reentrant)
    assert result.state is OperationState.SUCCEEDED
    assert seen


# --- ciclo de vida da stack -------------------------------------------------------


def test_stop_and_start_the_stack(stack):
    stack.app.first_run("op-1", CAPELLA)
    stopped = stack.app.stop_stack("op-2", CAPELLA)
    assert stopped.state is OperationState.SUCCEEDED
    assert set(stopped.output_references) == {"ollama", "open-webui", "comfyui"}
    assert not stack.app.status(CAPELLA).stack_up
    assert stack.launcher.members == {}

    started = stack.app.start_stack("op-3", CAPELLA)
    assert started.state is OperationState.SUCCEEDED
    assert stack.app.status(CAPELLA).stack_up


def test_restart_the_stack(stack):
    stack.app.first_run("op-1", CAPELLA)
    before = list(stack.launcher.starts)
    result = stack.app.restart_stack("op-2", CAPELLA)
    assert result.state is OperationState.SUCCEEDED
    assert stack.launcher.stops == ["comfyui", "open-webui", "ollama"]
    assert stack.launcher.starts[len(before) :] == ["ollama", "open-webui", "comfyui"]
    assert stack.app.status(CAPELLA).stack_up


def test_starting_before_installing_fails_with_a_reason(stack):
    result = stack.app.start_stack("op-1", CAPELLA)
    assert result.state is OperationState.FAILED
    assert any(item.code == "not-launchable" for item in result.diagnostics)


# --- external: observado, nunca tocado --------------------------------------------


def test_an_external_ollama_is_never_started_or_stopped():
    """The managed Ollama lives on its own port beside the external one.

    The external process on the profile's default port is detected by the real
    provisioner, reported, and neither started nor stopped by the hub.
    """
    from local_ai_stack.e2e.fixtures import external_install
    from local_ai_stack.e2e.providers import EXTERNAL_PID

    install = external_install("ollama")
    built = controlled_stack(ready_ports=(21434, 8080, 8188))
    try:
        built.runner.on_path.update(install.on_path)
        built.runner.fake_env.update(install.env)
        built.inventory.add_external(CAPELLA_PORTS["ollama"], executable=str(install.path))
        built.app.options = IntegrationOptions(
            port_overrides={"ollama": 21434},
            alongside_external=frozenset({"ollama", "comfyui"}),
        )
        original = built.app.deps.launcher

        def make(registry):
            launcher = original(registry)
            launcher.ready_ports["ollama"] = 21434
            return launcher

        built.app.deps.launcher = make

        result = built.app.first_run("op-1", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        assert "ollama" in built.app.last_first_run.external
        assert EXTERNAL_PID in built.inventory.pids()
        assert built.app.status(CAPELLA).component("ollama").endpoint.endswith(":21434")
        assert all(EXTERNAL_PID not in tree for tree in built.launcher.started_trees.values())

        built.app.stop_stack("op-2", CAPELLA)
        assert EXTERNAL_PID in built.inventory.pids()
    finally:
        built.release()
        install.release()


# --- falhas ---------------------------------------------------------------------


def _first_failed(stack_, **kwargs):
    recorder = Recorder()
    result = stack_.app.first_run("op-1", CAPELLA, emit=recorder, **kwargs)
    assert result.state is OperationState.FAILED
    assert recorder.events[-1].state is OperationState.FAILED
    assert result.diagnostics
    return result, recorder


def test_an_incompatible_machine_fails_preflight():
    built = controlled_stack(vram_mib=12227)
    try:
        result, recorder = _first_failed(built)
        assert recorder.events[-1].phase is OperationPhase.PREPARATION
        assert any(item.code.startswith("preflight-") for item in result.diagnostics)
        assert not built.artifacts.requests
    finally:
        built.release()


def test_an_occupied_port_fails_preflight():
    built = controlled_stack()
    try:
        built.inventory.occupy(CAPELLA_PORTS["ollama"])
        result, _ = _first_failed(built)
        assert any("endpoint-collision" in item.code for item in result.diagnostics)
        assert not built.artifacts.requests
    finally:
        built.release()


def test_a_provisioning_failure_is_reported():
    built = controlled_stack(injections=InjectionPlan.of(Injection.CHECKSUM_MISMATCH))
    try:
        _result, recorder = _first_failed(built)
        assert "Instalando Ollama" in recorder.messages
        assert recorder.events[-1].message == "Falha ao instalar Ollama"
        assert not built.launcher or built.launcher.starts == []
    finally:
        built.release()


def test_a_model_acquisition_failure_is_reported():
    built = controlled_stack(injections=InjectionPlan.of(Injection.DOWNLOAD_INTERRUPTED))
    try:
        _result, recorder = _first_failed(built)
        assert recorder.events[-1].message == "Falha ao baixar modelos"
        assert not built.app.status(CAPELLA).installation_ready
    finally:
        built.release()


def test_a_configuration_failure_is_reported():
    built = controlled_stack(injections=InjectionPlan.of(Injection.CONFIG_TAMPERED))
    try:
        _result, recorder = _first_failed(built)
        assert recorder.events[-1].message == "Falha ao aplicar configurações"
        assert "open-webui" not in built.launcher.starts
    finally:
        built.release()


def test_a_readiness_failure_is_reported():
    built = controlled_stack(injections=InjectionPlan.of(Injection.READINESS_TIMEOUT))
    try:
        result, recorder = _first_failed(built)
        assert recorder.events[-1].phase is OperationPhase.SERVICE_START
        assert any(item.code == "service-not-ready" for item in result.diagnostics)
    finally:
        built.release()


def test_failure_messages_carry_no_absolute_path():
    built = controlled_stack(injections=InjectionPlan.of(Injection.CHECKSUM_MISMATCH))
    try:
        result, recorder = _first_failed(built)
        for text in [e.message for e in recorder.events] + [d.message for d in result.diagnostics]:
            assert str(built.root.path) not in text
            assert ":\\" not in text
    finally:
        built.release()


def test_a_crash_inside_the_coordinator_still_ends_the_operation(stack, monkeypatch):
    from local_ai_stack import first_run

    monkeypatch.setattr(first_run.FirstRunCoordinator, "_provision", lambda self, plan: 1 / 0)
    recorder = Recorder()
    result = stack.app.first_run("op-1", CAPELLA, emit=recorder)
    assert result.state is OperationState.FAILED
    assert result.diagnostics[0].code == "unexpected-error"
    assert recorder.events[-1].kind is OperationEventKind.RESULT


# --- abrir Open WebUI --------------------------------------------------------------


def test_open_webui_returns_a_loopback_target_once_ready(stack):
    stack.app.first_run("op-1", CAPELLA)
    launch = stack.app.open_webui("op-2", CAPELLA)
    assert launch.ok
    assert launch.target.url == f"http://127.0.0.1:{CAPELLA_PORTS['open-webui']}"
    assert launch.target.ready
    assert launch.target.origin == "managed"


def test_open_webui_starts_what_it_needs_first(stack):
    stack.app.first_run("op-1", CAPELLA)
    stack.app.stop_stack("op-2", CAPELLA)
    recorder = Recorder()
    launch = stack.app.open_webui("op-3", CAPELLA, emit=recorder)
    assert launch.ok
    assert stack.launcher.starts[-2:] == ["ollama", "open-webui"]
    assert "comfyui" not in stack.launcher.starts[-2:]
    assert "Iniciando Open WebUI" in recorder.messages


def test_open_webui_without_an_installation_fails_with_a_reason(stack):
    launch = stack.app.open_webui("op-1", CAPELLA)
    assert not launch.ok
    assert launch.target is None
    assert launch.result.state is OperationState.FAILED


def test_a_launch_target_refuses_a_non_loopback_url():
    from local_ai_stack.stack_application import OpenWebUILaunchTarget

    with pytest.raises(ValueError):
        OpenWebUILaunchTarget("http://example.com:8080", "open-webui", "managed", True, "")


# --- projeções ---------------------------------------------------------------------


def test_status_carries_no_path_or_pid(stack):
    stack.app.first_run("op-1", CAPELLA)
    status = stack.app.status(CAPELLA)
    text = repr(status)
    assert str(stack.root.path) not in text
    assert "44100" not in text
    assert (
        status.component("open-webui").endpoint == f"http://127.0.0.1:{CAPELLA_PORTS['open-webui']}"
    )


def test_services_snapshot_uses_the_health_vocabulary(stack):
    stack.app.first_run("op-1", CAPELLA)
    snapshot = stack.app.services_snapshot(CAPELLA)
    assert {view.service_id for view in snapshot.services} == set(CAPELLA_PORTS)
    assert all(view.health == "UP" for view in snapshot.services)


def test_the_flow_leaves_no_temporary_directory_behind():
    before = set(tracking.outstanding())
    built = controlled_stack()
    built.app.first_run("op-1", CAPELLA)
    built.release()
    assert set(tracking.outstanding()) - before == set()


# --- conflitos externos: resolução via first_run/hub_application ------------


from local_ai_stack.e2e.fixtures import external_install
from local_ai_stack.external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictPreflightDisposition,
    ExternalConflictPolicy,
)
from local_ai_stack.external_conflicts import ALONGSIDE_PORTS, ConflictResolutionRequired
from local_ai_stack.installation import InstallLocation, resolve_installation


def _isolate_launcher(built, components):
    """Make the controlled launcher answer ready on each component's alt port."""
    original = built.app.deps.launcher

    def make(registry):
        launcher = original(registry)
        for component in components:
            launcher.ready_ports[component] = ALONGSIDE_PORTS[component]
        return launcher

    built.app.deps.launcher = make


def _fake_terminate(built):
    """Route STOP's process-end seam through the controlled inventory."""

    def terminate(pid, create_time):
        if pid not in built.inventory.pids():
            return False
        built.inventory.kill(pid)
        return True

    built.app.deps.terminate_process = terminate


def test_a_scenario_external_ollama_and_comfyui_install_alongside():
    """A) Both externals running on their default ports; INSTALL_ALONGSIDE proceeds."""
    ollama_ext = external_install("ollama")
    comfy_ext = external_install("comfyui")
    built = controlled_stack(ready_ports=(11434, 8080, 8188, *ALONGSIDE_PORTS.values()))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.on_path.update(comfy_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.runner.fake_env.update(comfy_ext.env)
        built.inventory.add_external(11434, pid=9101, executable=str(ollama_ext.path))
        built.inventory.add_external(8188, pid=9102, executable=str(comfy_ext.path), name="comfyui")
        _isolate_launcher(built, ["ollama", "comfyui"])

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        assert report.disposition is ConflictPreflightDisposition.RESOLUTION_REQUIRED
        assert {str(c.service_id) for c in report.conflicts} == {"ollama", "comfyui"}
        assert report.recommended_policy is ExternalConflictPolicy.INSTALL_ALONGSIDE

        resolution = built.app.resolve_conflict(
            ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE)
        )
        assert resolution.status.value == "proceed"

        result = built.app.first_run("op-2", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]

        status = built.app.status(CAPELLA)
        assert status.component("ollama").endpoint == "http://127.0.0.1:" + str(
            ALONGSIDE_PORTS["ollama"]
        )
        assert status.component("comfyui").endpoint == "http://127.0.0.1:" + str(
            ALONGSIDE_PORTS["comfyui"]
        )
        assert status.stack_up

        assert 9101 in built.inventory.pids()
        assert 9102 in built.inventory.pids()
        assert all(9101 not in tree for tree in built.launcher.started_trees.values())
        assert all(9102 not in tree for tree in built.launcher.started_trees.values())
    finally:
        built.release()
        ollama_ext.release()
        comfy_ext.release()


def test_b_scenario_stop_only_the_conflicting_external_survives_the_rest():
    """B) Only the authorized conflict is stopped; an unrelated process lives."""
    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9201, executable=str(ollama_ext.path))
        from local_ai_stack.e2e.providers import UNRELATED_PID

        built.inventory.occupy(39999)  # a listener on this port, pid is UNRELATED_PID
        _fake_terminate(built)

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        assert [str(c.service_id) for c in report.conflicts] == ["ollama"]

        resolution = built.app.resolve_conflict(
            ConflictPolicySelection(
                report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            )
        )
        assert resolution.status.value == "proceed"
        assert 9201 not in built.inventory.pids()
        assert UNRELATED_PID in built.inventory.pids()

        result = built.app.first_run("op-2", CAPELLA)
        assert result.state is OperationState.SUCCEEDED, [d.message for d in result.diagnostics]
        status = built.app.status(CAPELLA)
        assert status.component("ollama").endpoint == "http://127.0.0.1:11434"
        assert status.stack_up
        assert UNRELATED_PID in built.inventory.pids()
    finally:
        built.release()
        ollama_ext.release()


def test_c_scenario_unprovable_external_refuses_stop_and_leaves_it_running():
    """C) Ambiguous identity: STOP is never offered, and forcing it changes nothing."""
    from local_ai_stack.inventory import Listener

    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9301, executable=str(ollama_ext.path))
        built.inventory.listeners.append(Listener("127.0.0.1", 11434, 9302))
        _fake_terminate(built)

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        conflict = report.conflicts[0]
        assert not conflict.can_stop_safely
        assert ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL not in report.available_policies

        with pytest.raises(ValueError, match="unavailable"):
            built.app.resolve_conflict(
                ConflictPolicySelection(
                    report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
                )
            )
        assert 9301 in built.inventory.pids()

        resolution = built.app.resolve_conflict(
            ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.ABORT)
        )
        assert resolution.status.value == "aborted"
        assert 9301 in built.inventory.pids()
    finally:
        built.release()
        ollama_ext.release()


def test_d_scenario_custom_root_beside_an_external_elsewhere_has_no_filesystem_collision():
    """D) A CUSTOM root and an external installed at an unrelated path coexist."""
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    ollama_ext = external_install("ollama")
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        custom_root = built.root.path / "custom-loira-40k"
        built.app.installation = resolve_installation(
            InstallLocation.custom(custom_root), repo_root=built.fixture.path
        )

        raw = built.app.preflight(built.app.profile(CAPELLA))
        assert not any(issue.code.value == "path-overlap" for issue in raw.issues)
        assert str(ollama_ext.path).lower() not in str(custom_root).lower()
    finally:
        built.release()
        ollama_ext.release()


def test_external_installation_without_a_runtime_listener_is_observation_only():
    """An external path alone enables managed provisioning without asking a policy."""
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    ollama_ext = external_install("ollama")
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)

        result = built.app.first_run("op-1", CAPELLA)

        assert result.state is OperationState.SUCCEEDED
        assert built.app.options.allows_alongside("ollama")
        assert "ollama" not in built.app.options.port_overrides
        assert built.app.current_conflict_preflight() is None
    finally:
        built.release()
        ollama_ext.release()


def test_e_scenario_the_alternate_endpoint_being_occupied_blocks_with_a_diagnostic():
    """E) Alt endpoint also occupied and identity unprovable: BLOCKED, not a choice."""
    from local_ai_stack.inventory import Listener

    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9401, executable=str(ollama_ext.path))
        built.inventory.listeners.append(Listener("127.0.0.1", 11434, 9402))
        built.inventory.occupy(ALONGSIDE_PORTS["ollama"], pid=9403)

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        assert report.disposition is ConflictPreflightDisposition.BLOCKED
        assert report.available_policies == (ExternalConflictPolicy.ABORT,)
        assert any(item.code == "managed-endpoint-occupied" for item in report.hard_blockers)
        assert not built.app.status(CAPELLA).installation_ready
    finally:
        built.release()
        ollama_ext.release()


def test_f_scenario_second_run_after_alongside_does_not_ask_again():
    """F) Idempotence: the resolved conflict is not rediscovered on a second run."""
    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188, ALONGSIDE_PORTS["ollama"]))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9501, executable=str(ollama_ext.path))
        _isolate_launcher(built, ["ollama"])

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        built.app.resolve_conflict(
            ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE)
        )
        first = built.app.first_run("op-2", CAPELLA)
        assert first.state is OperationState.SUCCEEDED, [d.message for d in first.diagnostics]

        downloads = len(built.artifacts.requests)
        pulls = list(built.ollama.pulls)

        second = built.app.first_run("op-3", CAPELLA)
        assert second.state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
        assert len(built.artifacts.requests) == downloads
        assert built.ollama.pulls == pulls
        assert 9501 in built.inventory.pids()
    finally:
        built.release()
        ollama_ext.release()


def test_f_scenario_second_run_after_stop_does_not_ask_again():
    """F) The same, for STOP: nothing left to resolve once the port is free."""
    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9601, executable=str(ollama_ext.path))
        _fake_terminate(built)

        with pytest.raises(ConflictResolutionRequired) as excinfo:
            built.app.first_run("op-1", CAPELLA)
        report = excinfo.value.report
        built.app.resolve_conflict(
            ConflictPolicySelection(
                report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            )
        )
        first = built.app.first_run("op-2", CAPELLA)
        assert first.state is OperationState.SUCCEEDED, [d.message for d in first.diagnostics]

        second = built.app.first_run("op-3", CAPELLA)
        assert second.state is OperationState.SUCCEEDED
        assert built.app.last_first_run.did_nothing
    finally:
        built.release()
        ollama_ext.release()


def test_hub_application_surfaces_the_conflict_and_resumes_after_resolution():
    """The bridge: hub.install() refuses synchronously, never starts an operation."""
    from local_ai_stack.hub_application import ConflictResolutionRequired as HubConflictRequired
    from local_ai_stack.hub_application import create_hub_application
    from local_ai_stack.layout import ENV_ROOT
    from local_ai_stack.stack_application import StackApplication
    from local_ai_stack.tui.application import InstallSelection

    ollama_ext = external_install("ollama")
    built = controlled_stack(ready_ports=(11434, 8080, 8188, ALONGSIDE_PORTS["ollama"]))
    try:
        built.runner.on_path.update(ollama_ext.on_path)
        built.runner.fake_env.update(ollama_ext.env)
        built.inventory.add_external(11434, pid=9701, executable=str(ollama_ext.path))
        _isolate_launcher(built, ["ollama"])

        def resolve(location):
            return resolve_installation(
                location,
                repo_root=built.fixture.path,
                env={ENV_ROOT: str(built.root.path / "default")},
            )

        def backend(installation):
            return StackApplication(
                installation,
                profiles=tuple(built.app.profiles.values()),
                repo_root=built.fixture.path,
                dependencies=built.app.deps,
                options=built.app.options,
            )

        adapter = create_hub_application(
            built.fixture.path, resolve=resolve, backend_factory=backend
        )
        try:
            with pytest.raises(HubConflictRequired) as excinfo:
                adapter.install(InstallSelection(CAPELLA))
            assert not adapter._busy
            assert adapter.current_operation() is None
            report = excinfo.value.report
            assert adapter.current_conflict_preflight() is not None

            selection = ConflictPolicySelection(
                report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE
            )
            assert set(vars(selection)) == {"preflight_id", "policy"}
            resolution = adapter.resolve_external_conflicts(selection)
            assert resolution.status.value == "proceed"
            assert adapter.current_conflict_preflight() is None

            adapter.install(InstallSelection(CAPELLA))
            adapter.wait(10)
            assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
        finally:
            adapter.close()
    finally:
        built.release()
        ollama_ext.release()
