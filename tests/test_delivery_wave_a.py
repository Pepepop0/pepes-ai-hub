"""Real Hub + adapter + FirstRunCoordinator; only OS/provider seams are controlled."""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

import pytest
from first_run_support import CHAT_REFERENCE, controlled_stack
from textual.widgets import Button, Input, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.config import config_path, loads
from local_ai_stack.e2e import tracking
from local_ai_stack.e2e.fixtures import external_install
from local_ai_stack.e2e.providers import EXTERNAL_PID
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.first_run import FirstRunCoordinator
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocation, InstallLocationMode, resolve_installation
from local_ai_stack.integration import IntegrationOptions
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.operation_contracts import OperationEventKind, OperationState, advance_operation
from local_ai_stack.runtime import StackRuntime
from local_ai_stack.services import ServiceState
from local_ai_stack.stack_application import StackApplication
from local_ai_stack.tui.application import HubApplication, InstallSelection, OperationBusy
from local_ai_stack.tui.widgets import RightRail
from local_ai_stack.tui.wizard import WizardScreen, WizardStep


@pytest.fixture
def composition(monkeypatch):
    worlds, adapters = [], []
    before = set(tracking.outstanding())

    def no_network(*args, **kwargs):
        raise AssertionError("product integration must not open a socket")

    monkeypatch.setattr(socket, "create_connection", no_network)

    def build(**kwargs):
        world = controlled_stack(**kwargs)
        worlds.append(world)
        resolve = lambda location: resolve_installation(
            location,
            repo_root=world.fixture.path,
            env={ENV_ROOT: str(world.root.path / "default")},
        )

        def backend(installation):
            return StackApplication(
                installation,
                profiles=tuple(world.app.profiles.values()),
                repo_root=world.fixture.path,
                dependencies=world.app.deps,
                options=world.app.options,
            )

        def reopen():
            adapter = create_hub_application(
                world.fixture.path, resolve=resolve, backend_factory=backend
            )
            adapters.append(adapter)
            return adapter

        return world, reopen(), reopen

    yield build
    for adapter in adapters:
        adapter.close()
    for world in worlds:
        world.release()
    assert set(tracking.outstanding()) == before


def tui(adapter):
    opened = []
    app = LocalAIStackApp(
        facts=adapter.backend.facts(),
        profiles=adapter.profiles,
        runtime_snapshot=StackRuntime((), ServiceState.DOWN),
        installed_config=adapter.config,
        integration=adapter.backend._integration(),
        executor_factory=lambda _: adapter.backend.executor(adapter.backend.profile("capella")),
        application=adapter,
        open_browser=lambda url: opened.append(url) or True,
    )
    return app, opened


async def click(pilot, selector):
    button = pilot.app.screen.query_one(selector, Button)
    button.active_effect_duration = 0
    button.remove_class("-active")
    await pilot.click(selector)
    await pilot.pause()


async def review(pilot, *, mode="default", root=None):
    await click(pilot, "#wizard-next")
    await click(pilot, "#wizard-profile-capella")
    await click(pilot, "#wizard-next")
    await click(pilot, f"#wizard-mode-{mode}")
    if root is not None:
        pilot.app.screen.query_one("#wizard-custom-root", Input).value = str(root)
        await pilot.pause()
    await click(pilot, "#wizard-next")
    assert pilot.app.screen.state.step is WizardStep.REVIEW


async def finish(pilot, adapter):
    await asyncio.to_thread(adapter.wait, 10)
    await pilot.pause()
    assert pilot.app.operation_view.terminal


def assert_streams(events):
    snapshots = {}
    for event in events:
        snapshots[event.operation_id] = advance_operation(snapshots.get(event.operation_id), event)
    for op_id, snapshot in snapshots.items():
        assert snapshot.last_event.state.terminal
        assert (
            sum(e.kind is OperationEventKind.RESULT for e in events if e.operation_id == op_id) == 1
        )


def test_capella_wizard_runs_the_real_coordinator_and_stream_without_blocking_ui(
    composition, monkeypatch
):
    world, adapter, _ = composition()
    entered, resume = threading.Event(), threading.Event()
    original = FirstRunCoordinator._provision

    def provision(coordinator, plan):
        entered.set()
        assert resume.wait(10), "TUI never resumed the controlled provisioning seam"
        return original(coordinator, plan)

    monkeypatch.setattr(FirstRunCoordinator, "_provision", provision)
    events = []
    adapter.subscribe(events.append)
    assert isinstance(adapter, HubApplication)
    app, opened = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, WizardScreen)
            assert app.screen.state.profile_id == "capella"
            await review(pilot)
            await click(pilot, "#wizard-next")
            assert entered.is_set()
            assert app.operation_view is not None and not app.operation_view.terminal
            assert app.screen.state.step is WizardStep.PROGRESS
            assert app.screen.query_one("#wizard-next", Button).disabled
            with pytest.raises(OperationBusy):
                adapter.install(InstallSelection("capella"))
            resume.set()
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            assert app.screen.state.step is WizardStep.DONE
            assert adapter.backend.last_first_run.installed
            assert adapter.backend.last_first_run.acquired
            assert (
                adapter.current_operation().last_event
                is adapter.backend.current_operation().last_event
            )
            assert world.ollama.pulls == [CHAT_REFERENCE]
            assert adapter.installation().ready
            await click(pilot, "#wizard-finish")
            assert not isinstance(app.screen, WizardScreen)
            assert "Pronta" in str(app.query_one("#dashboard-overview", Static).content)
            assert app.query_one(RightRail).appearance.resolved.pet_id == "hello-kitty"
            assert opened == []

    try:
        asyncio.run(exercise())
    finally:
        resume.set()
    assert_streams(events)


@pytest.mark.parametrize(
    "kind,kwargs,expected",
    [
        ("preflight", {"vram_mib": 12227}, "Ambiente não está pronto"),
        (
            "provision",
            {"injections": InjectionPlan.of(Injection.CHECKSUM_MISMATCH)},
            "Falha ao instalar Ollama",
        ),
        (
            "readiness",
            {"injections": InjectionPlan.of(Injection.READINESS_TIMEOUT)},
            "não ficou pronto",
        ),
    ],
)
def test_real_failures_reach_the_wizard_as_terminal_diagnostics(
    composition, kind, kwargs, expected
):
    _, adapter, _ = composition(**kwargs)
    app, _ = tui(adapter)
    events = []
    adapter.subscribe(events.append)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await finish(pilot, adapter)
            assert app.operation_view.failed
            assert expected in events[-1].message
            assert app.operation_view.diagnostics
            displayed = str(app.screen.query_one("#wizard-step-done", Static).content)
            assert "Código:" in displayed
            assert "Traceback" not in displayed
            assert not adapter.installation().ready

    asyncio.run(exercise())
    assert_streams(events)


@pytest.mark.parametrize("mode", list(InstallLocationMode))
def test_install_location_round_trips_and_second_run_does_no_work(composition, mode):
    world, adapter, reopen = composition()
    root = world.root.path / "custom" if mode is InstallLocationMode.CUSTOM else None
    selection = InstallSelection("capella", mode, str(root) if root else None)
    adapter.install(selection)
    adapter.wait(10)
    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    installed = adapter.backend.installation
    expected = (
        root
        if root
        else (
            world.fixture.path / "stack"
            if mode is InstallLocationMode.PORTABLE
            else world.root.path / "default"
        )
    )
    assert installed.root == expected
    config_file = config_path(installed.layout)
    config = loads(config_file.read_text(encoding="utf-8"))
    assert config.installation.mode is mode
    assert config.installation.root == root
    if mode is InstallLocationMode.PORTABLE:
        assert "root: null" in config_file.read_text(encoding="utf-8")
    paths = [config_file, adapter.selection_path, installed.root / "data/open-webui/open-webui.env"]
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    counts = (len(world.artifacts.requests), list(world.ollama.pulls))
    adapter.close()
    second = reopen()
    assert second.installation().ready
    assert second.backend.installation.root == expected
    app, _ = tui(second)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, WizardScreen)
            assert "Pronta" in str(app.query_one("#dashboard-overview", Static).content)
            second.install(selection)
            await finish(pilot, second)
            assert app.operation_view.succeeded
            assert second.backend.last_first_run.did_nothing

    asyncio.run(exercise())
    assert len(world.artifacts.requests) == counts[0]
    assert world.ollama.pulls == counts[1]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before
    assert second.installation().ready


def test_custom_5070_path_is_resolved_without_creating_it():
    if not Path("C:/").is_absolute():
        pytest.skip("Windows path semantics")
    location = InstallLocation.custom(r"C:\Pepito-Test-5070")
    resolved = resolve_installation(location)
    assert resolved.root == Path(r"C:\Pepito-Test-5070")
    assert resolved.location.mode is InstallLocationMode.CUSTOM


def test_custom_location_flows_from_the_real_wizard_to_backend(composition):
    world, adapter, _ = composition()
    root = world.root.path / "selected custom folder"
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot, mode="custom", root=root)
            await click(pilot, "#wizard-next")
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            assert adapter.backend.installation.root == root
            assert app.installed_config.installation == InstallLocation.custom(root)

    asyncio.run(exercise())


def test_missing_model_is_pending_and_retry_acquires_only_that_model(composition):
    from local_ai_stack.models import ModelId

    world, adapter, _ = composition()
    adapter.install(InstallSelection("capella"))
    adapter.wait(10)
    backend = adapter.backend
    profile = backend.profile("capella")
    spec = backend.catalog().require(ModelId("comfyui/flux2_vae"))
    missing = backend.model_installer(profile).detect(spec).path
    assert missing.is_file()
    missing.unlink()  # only this test-owned fixture model
    assert not adapter.installation().ready
    before = len(world.artifacts.requests)
    pulls = list(world.ollama.pulls)
    adapter.install(InstallSelection("capella"))
    adapter.wait(10)
    assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
    assert adapter.installation().ready
    assert len(world.artifacts.requests) == before + 1
    assert world.ollama.pulls == pulls


def test_open_webui_failure_does_not_open_browser_or_leave_running(composition):
    world, adapter, _ = composition()
    adapter.install(InstallSelection("capella"))
    adapter.wait(10)
    adapter.stop_stack()
    adapter.wait(10)
    world.endpoints.plan = InjectionPlan.of(Injection.READINESS_TIMEOUT)
    app, opened = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-open-webui")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert opened == []
            assert app.operation_view.failed
            assert not app._launching
            notice = str(app.query_one("#dashboard-notice", Static).content)
            assert notice and "Traceback" not in notice

    asyncio.run(exercise())


def test_real_event_replay_unsubscribe_and_observer_isolation(composition):
    _, adapter, _ = composition()
    received = []
    subscription = adapter.subscribe(received.append)

    def broken(event):
        raise RuntimeError("observer failure")

    adapter.subscribe(broken)
    adapter.install(InstallSelection("capella"))
    adapter.wait(10)
    assert_streams(received)
    late = []
    adapter.subscribe(late.append)
    assert late == [received[-1]]
    assert late[0].state is OperationState.SUCCEEDED
    subscription.close()
    subscription.close()
    before = list(received)
    adapter.stop_stack()
    adapter.wait(10)
    assert received == before
    assert late[-1].state is OperationState.SUCCEEDED


def test_dashboard_lifecycle_and_real_open_webui_target(composition):
    world, adapter, _ = composition()
    # A nondefault port proves the UI cannot synthesize its launch address.
    adapter.backend.options = IntegrationOptions(port_overrides={"open-webui": 28080})
    world.endpoints.ready.discard(8080)
    world.endpoints.ready.add(28080)
    original_launcher = world.app.deps.launcher

    def launcher(registry):
        result = original_launcher(registry)
        result.ready_ports["open-webui"] = 28080
        return result

    world.app.deps.launcher = launcher
    adapter.install(InstallSelection("capella"))
    adapter.wait(10)
    assert adapter.installation().ready
    app, opened = tui(adapter)
    events = []
    adapter.subscribe(events.append)
    events.clear()  # subscription replay is a watermark, not a new producer stream

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await click(pilot, "#dashboard-stop")
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            assert not adapter.backend.status("capella").stack_up
            await click(pilot, "#dashboard-start")
            await finish(pilot, adapter)
            assert adapter.backend.status("capella").stack_up
            await click(pilot, "#dashboard-restart")
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            await click(pilot, "#dashboard-stop")
            await finish(pilot, adapter)
            # Opening asks the backend to start and wait for Open WebUI first.
            app.open_webui()
            assert not app.open_webui()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert opened == [
                adapter.backend.options.endpoints_for(adapter.backend.profile("capella")).open_webui
            ]
            assert ":28080" in opened[0]
            assert adapter.backend.status("capella").component("open-webui").up
            assert app.query_one("#dashboard-open-webui", Button).disabled

    asyncio.run(exercise())
    assert_streams(events)


def test_external_survives_real_adapter_lifecycle(composition):
    world, adapter, _ = composition(ready_ports=(21434, 8080, 8188))
    external = external_install("ollama")
    try:
        world.runner.on_path.update(external.on_path)
        world.runner.fake_env.update(external.env)
        world.inventory.add_external(11434, executable=str(external.path))
        adapter.backend.options = IntegrationOptions(
            port_overrides={"ollama": 21434}, alongside_external=frozenset({"ollama"})
        )
        factory = world.app.deps.launcher

        def launcher(registry):
            item = factory(registry)
            item.ready_ports["ollama"] = 21434
            return item

        world.app.deps.launcher = launcher
        adapter.install(InstallSelection("capella"))
        adapter.wait(10)
        for command in (
            adapter.stop_stack,
            adapter.start_stack,
            adapter.restart_stack,
            adapter.stop_stack,
        ):
            command()
            adapter.wait(10)
            assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
            assert EXTERNAL_PID in world.inventory.pids()
            assert all(EXTERNAL_PID not in tree for tree in world.launcher.started_trees.values())
        assert world.launcher.registry.load().entries == {}
        assert world.launcher.members == {}
    finally:
        external.release()


def test_product_entrypoint_constructs_the_real_adapter(monkeypatch, composition):
    from local_ai_stack import app as entry
    from local_ai_stack import hub_application

    _, adapter, _ = composition()
    seen = []
    monkeypatch.setattr(hub_application, "create_hub_application", lambda _: adapter)

    class EntryApp:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def run(self):
            seen.append("run")

    monkeypatch.setattr(entry, "LocalAIStackApp", EntryApp)
    entry.run_tui()
    assert seen[0]["application"] is adapter
    assert seen[1] == "run"
