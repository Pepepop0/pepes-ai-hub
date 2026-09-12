"""Real Hub + adapter + FirstRunCoordinator; only OS/provider seams are controlled."""

from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest
from first_run_support import CHAT_REFERENCE, controlled_stack
from rich.text import Text
from textual.widgets import Button, Input, Static

from local_ai_stack.app import LocalAIStackApp
from local_ai_stack.components import ComponentId
from local_ai_stack.config import config_path, loads
from local_ai_stack.download import Downloader
from local_ai_stack.e2e import tracking
from local_ai_stack.e2e.fixtures import external_install
from local_ai_stack.e2e.providers import EXTERNAL_PID, UNRELATED_PID
from local_ai_stack.e2e.seams import Injection, InjectionPlan
from local_ai_stack.external_conflict_contracts import ExternalConflictPolicy
from local_ai_stack.external_conflicts import ALONGSIDE_PORTS
from local_ai_stack.first_run import FirstRunCoordinator
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocation, InstallLocationMode, resolve_installation
from local_ai_stack.integration import IntegrationOptions
from local_ai_stack.inventory import Listener
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.model_catalog import ModelCatalog
from local_ai_stack.models import FileModelSource
from local_ai_stack.operation_contracts import (
    DETAIL_HISTORY_LIMIT,
    OperationActivityCategory,
    OperationEventKind,
    OperationState,
    advance_operation,
)
from local_ai_stack.pets import unsafe_characters
from local_ai_stack.runner import CommandResult
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
        executor_factory=lambda _: adapter.backend.executor(
            adapter.backend.profile(adapter.profiles[0].id)
        ),
        application=adapter,
        open_browser=lambda url: opened.append(url) or True,
    )
    return app, opened


MODEL_CHUNK = 64 * 1024
MODEL_BYTES = 1024 * 1024


class TickingClock:
    """A monotonic clock whose deterministic ticks make transfer speed observable."""

    def __init__(self, step: float = 0.04) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current


class ControlledModelTransfer:
    """A real HTTPX byte stream with deterministic observation points."""

    def __init__(
        self,
        world,
        *,
        known_total: bool = True,
        fail_mid_transfer: bool = False,
        corrupt: bool = False,
        pause_at_final: bool = False,
    ) -> None:
        self.world = world
        self.known_total = known_total
        self.fail_mid_transfer = fail_mid_transfer
        self.corrupt = corrupt
        self.pause_at_final = pause_at_final
        self.intermediate_ready = threading.Event()
        self.resume_intermediate = threading.Event()
        self.final_ready = threading.Event()
        self.resume_final = threading.Event()
        self.chunks_delivered = 0
        self._first_request = True

    def client_factory(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def release(self) -> None:
        self.resume_intermediate.set()
        self.resume_final.set()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.world.artifacts.kinds.get(url) != "model":
            raise AssertionError(f"unexpected controlled model request: {url}")
        self.world.artifacts.requests.append(url)
        payload = self.world.artifacts.payloads[url]
        first, self._first_request = self._first_request, False
        if first and self.corrupt:
            payload = bytes((payload[0] ^ 1,)) + payload[1:]
        headers = {"content-length": str(len(payload))} if self.known_total else {}
        return httpx.Response(200, headers=headers, content=self._body(payload, first))

    def _body(self, payload: bytes, first: bool):
        for index, start in enumerate(range(0, len(payload), MODEL_CHUNK), start=1):
            self.chunks_delivered += 1
            yield payload[start : start + MODEL_CHUNK]
            if first and index == 5:
                self.intermediate_ready.set()
                if not self.resume_intermediate.wait(10):
                    raise AssertionError("test did not release the controlled transfer")
                if self.fail_mid_transfer:
                    raise httpx.ReadError("controlled transport closed mid-transfer")
        if first and self.pause_at_final:
            self.final_ready.set()
            if not self.resume_final.wait(10):
                raise AssertionError("test did not release the final transfer observation")


class ControlledInstallOutput:
    """Feed realistic command output through the real provisioner parsers.

    The controlled toolchain still materializes the checkout, environments and
    packages. This wrapper only supplies the line stream that a real
    ``CommandRunner`` would pass to ``on_output`` and pauses after pip reports
    158 MiB so the real TUI can be inspected before the command returns.
    """

    SECRET = "sk-0123456789abcdef0123456789"
    PRIVATE_PATH = r"C:\Users\Private Person\AppData\Local\pip-cache"
    EXTRA_INDEX = "https://private-user:private-pass@packages.invalid/simple"

    def __init__(self, world, *, fail_comfy_pip: bool = False) -> None:
        assert world.runner is not None
        self.world = world
        self.original_run = world.runner.run
        self.fail_comfy_pip = fail_comfy_pip
        self.pip_partial = threading.Event()
        self.resume = threading.Event()
        self.streamed_commands: list[tuple[str, ...]] = []

    def bind(self) -> ControlledInstallOutput:
        self.world.runner.run = self.run
        return self

    def release(self) -> None:
        self.resume.set()

    def _emit(self, sink, stream: str, line: str, *, seconds: float = 0.25) -> None:
        self.world.clock.advance(seconds)
        sink(stream, line)

    def run(self, argv, **kwargs) -> CommandResult:
        items = tuple(str(part) for part in argv)
        sink = kwargs.get("on_output")
        if sink is None:
            return self.original_run(argv, **kwargs)
        self.streamed_commands.append(items)

        if Path(items[0]).name in ("git", "git.exe") and "clone" in items:
            self._emit(
                sink,
                "stderr",
                f"Cloning into '{self.PRIVATE_PATH}' from {self.EXTRA_INDEX}?token={self.SECRET}",
            )
            self._emit(sink, "stderr", "Receiving objects:  45% (450/1000), 12 MiB")
            self._emit(sink, "stderr", "Resolving deltas: 100% (200/200), done.")
            return self.original_run(argv, **kwargs)

        if "-m" not in items or "pip" not in items or "install" not in items:
            return self.original_run(argv, **kwargs)

        requirements = tuple(
            part
            for part in items[items.index("install") + 1 :]
            if "==" in part and not part.startswith("--")
        )
        is_comfy = any(part.startswith("torch==") for part in requirements)
        package = "torch" if is_comfy else "open-webui"
        version = next(
            (part.split("==", 1)[1] for part in requirements if part.startswith(f"{package}==")),
            "1.0",
        )
        self._emit(sink, "stdout", f"Collecting {package}")
        self._emit(
            sink,
            "stdout",
            f"Looking in indexes: {self.EXTRA_INDEX}?api_key={self.SECRET} {self.PRIVATE_PATH}",
        )

        if not is_comfy:
            self._emit(sink, "stdout", "Downloading open_webui.whl (2.0 MiB)")
            self._emit(sink, "stdout", "2.0/2.0 MiB 10.0 MiB/s eta 0:00:00")
            self._emit(sink, "stdout", f"Installing collected packages: {package}")
            self._emit(sink, "stdout", f"Successfully installed {package}-{version}")
            return self.original_run(argv, **kwargs)

        # A long real download can be active for many seconds before pip emits
        # its next line. Advance only the injected monotonic clock.
        self._emit(sink, "stdout", "Downloading torch.whl (259.0 MiB)", seconds=18)
        self._emit(sink, "stdout", "158.0/259.0 MiB 22.4 MiB/s eta 0:00:05")
        self._emit(
            sink,
            "stderr",
            f"Traceback at {self.PRIVATE_PATH}; --extra-index-url {self.EXTRA_INDEX}; {self.SECRET}",
        )
        self.pip_partial.set()
        if not self.resume.wait(10):
            raise AssertionError("test did not release controlled pip output")
        if self.fail_comfy_pip:
            return CommandResult(
                argv=items,
                returncode=1,
                stderr=(
                    f"Traceback at {self.PRIVATE_PATH}; token={self.SECRET}; "
                    f"--extra-index-url {self.EXTRA_INDEX}"
                ),
            )

        self._emit(sink, "stdout", "259.0/259.0 MiB 22.4 MiB/s eta 0:00:00")
        transitive = [f"controlled-dependency-{index:02d}" for index in range(24)]
        installed = [*transitive, "torch"]
        self._emit(sink, "stdout", f"Installing collected packages: {', '.join(installed)}")
        versions = " ".join(f"{name}-1.0" for name in transitive)
        self._emit(sink, "stdout", f"Successfully installed {versions} torch-{version}")
        return self.original_run(argv, **kwargs)


def instrument_model_downloads(world, *, known_total=True, **transport_options):
    """Expand fixture models and bind only their transport/clock seams."""
    catalog = world.app.deps.catalog
    assert catalog is not None
    specs = dict(catalog.specs)
    for model_id, spec in catalog.specs.items():
        source = spec.source
        if not isinstance(source, FileModelSource):
            continue
        url = source.artifact.url
        if world.artifacts.kinds.get(url) != "model":
            continue
        seed = world.artifacts.payloads[url]
        payload = (seed * (MODEL_BYTES // len(seed) + 1))[:MODEL_BYTES]
        world.artifacts.payloads[url] = payload
        artifact = replace(
            source.artifact,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload) if known_total else None,
        )
        specs[model_id] = replace(spec, source=replace(source, artifact=artifact))
    world.app.deps.catalog = ModelCatalog(
        specs=specs,
        unpinned=dict(catalog.unpinned),
        aliases=dict(catalog.aliases),
    )
    transfer = ControlledModelTransfer(
        world,
        known_total=known_total,
        **transport_options,
    )
    world.app.deps.monotonic = TickingClock()
    world.app.deps.downloader = lambda: Downloader(
        client_factory=transfer.client_factory,
        max_attempts=1,
        chunk_size=MODEL_CHUNK,
    )
    return transfer


async def wait_for_observation(pilot, signal: threading.Event) -> None:
    assert await asyncio.to_thread(signal.wait, 10), "controlled transfer observation timed out"
    await pilot.pause()


def transfer_events(events):
    return [event for event in events if event.transfer is not None]


def assert_transfer_bar_fits(app, width: int) -> str:
    pane = app.screen.query_one("#wizard-step-progress", Static)
    rendered = str(pane.content)
    plain = Text.from_markup(rendered).plain
    bar_line = next(line for line in plain.splitlines() if "%" in line and "[" in line)
    assert len(bar_line) <= pane.size.width, (width, pane.size.width, bar_line)
    return rendered


async def click(pilot, selector):
    button = pilot.app.screen.query_one(selector, Button)
    button.active_effect_duration = 0
    button.remove_class("-active")
    await pilot.click(selector)
    await pilot.pause()


async def review(pilot, *, profile="capella", mode="default", root=None):
    await click(pilot, "#wizard-next")
    await click(pilot, f"#wizard-profile-{profile}")
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


def _test_profile_yaml() -> str:
    """Resolved 12 GB fixture profile presented to the controlled UI as Test."""
    source = (Path(__file__).resolve().parents[1] / "profiles" / "capella.yaml").read_text(
        encoding="utf-8"
    )
    return (
        source.replace("id: capella", "id: test", 1)
        .replace("name: Capella", "name: Test", 1)
        .replace("min_vram_gb: 32", "min_vram_gb: 8", 1)
        .replace("recommended_vram_gb: 32", "recommended_vram_gb: 12", 1)
    )


def _use_isolated_launcher(world, *component_ids: str) -> None:
    original = world.app.deps.launcher

    def launcher(registry):
        controlled = original(registry)
        for component_id in component_ids:
            controlled.ready_ports[component_id] = ALONGSIDE_PORTS[component_id]
        return controlled

    world.app.deps.launcher = launcher


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


def _public_event_text(events) -> str:
    return repr([asdict(event) for event in events])


def _semantic_facts(events, *, start: int = 0, stop: int | None = None):
    return [
        value
        for event in events[start:stop]
        for value in (event.activity, event.detail)
        if value is not None
    ]


def _assert_actions_in_order(facts, expected: tuple[str, ...]) -> None:
    actions = iter(fact.action for fact in facts)
    for wanted in expected:
        assert wanted in actions, f"missing ordered activity {wanted!r}"


def test_real_provisioner_output_reaches_adapter_and_granular_tui(composition):
    """Real domains and UI; only command/process/network seams are controlled."""
    world, adapter, _ = composition()
    output = ControlledInstallOutput(world).bind()
    events = []
    adapter.subscribe(events.append)
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await wait_for_observation(pilot, output.pip_partial)

            view = app.operation_view
            assert view is not None and not view.terminal
            assert view.activity is not None
            assert view.activity.category is OperationActivityCategory.DEPENDENCY
            assert view.activity.action == "downloading"
            assert view.activity.item == "torch"
            assert view.transfer is not None
            assert view.transfer.completed_bytes == 158 * 1024**2
            assert view.transfer.total_bytes == 259 * 1024**2
            assert view.transfer.bytes_per_second == pytest.approx(22.4 * 1024**2)
            assert view.progress is not None and view.progress.total == 3

            pane = Text.from_markup(
                str(app.screen.query_one("#wizard-step-progress", Static).content)
            ).plain
            assert f"Etapa {view.progress.completed} de 3" in pane and "Instalando ComfyUI" in pane
            assert "Baixando dependências" in pane and "torch" in pane
            assert "61%" in pane and "158 MiB / 259 MiB" in pane
            assert "22.4 MiB/s" in pane and "restantes" in pane
            assert "Tempo: 00:18" in pane

            snapshot_before = adapter.current_operation()
            view_before = app.operation_view
            transfer_before = view.transfer
            progress_before = view.progress
            await click(pilot, "#wizard-details-toggle")
            assert adapter.current_operation() is snapshot_before
            assert app.operation_view is view_before
            assert app.operation_view.transfer is transfer_before
            assert app.operation_view.progress is progress_before
            assert (
                "Detalhes"
                in Text.from_markup(
                    str(app.screen.query_one("#wizard-step-progress", Static).content)
                ).plain
            )
            assert (
                str(app.screen.query_one("#wizard-details-toggle", Button).label)
                == "Ocultar detalhes"
            )
            await click(pilot, "#wizard-details-toggle")
            assert (
                str(app.screen.query_one("#wizard-details-toggle", Button).label)
                == "Mostrar detalhes"
            )

            for width in (100, 80, 70, 120):
                await pilot.resize_terminal(width, 40 if width >= 100 else 32)
                await pilot.pause()
                resized = Text.from_markup(
                    str(app.screen.query_one("#wizard-step-progress", Static).content)
                ).plain
                assert "Baixando dependências" in resized
                assert "158 MiB / 259 MiB" in resized
                assert app.query_one(RightRail) is not None

            output.resume.set()
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            assert app.operation_view.activity is None
            assert app.operation_view.transfer is None
            assert len(app.operation_view.history) == DETAIL_HISTORY_LIMIT
            snapshot = adapter.current_operation()
            assert snapshot is not None and snapshot.activity is None
            assert len(snapshot.details) == DETAIL_HISTORY_LIMIT
            assert [entry.sequence for entry in snapshot.details] == sorted(
                entry.sequence for entry in snapshot.details
            )

            done = Text.from_markup(
                str(app.screen.query_one("#wizard-step-done", Static).content)
            ).plain
            assert "Concluído" in done and "Tudo pronto" in done
            assert "[>]" not in done
            await click(pilot, "#wizard-details-toggle")
            expanded = Text.from_markup(
                str(app.screen.query_one("#wizard-step-done", Static).content)
            ).plain
            assert "Detalhes" in expanded

            first_streamed = tuple(output.streamed_commands)
            first_event_count = len(events)
            adapter.install(InstallSelection("capella"))
            await finish(pilot, adapter)
            second_events = events[first_event_count:]
            assert adapter.backend.last_first_run.did_nothing
            assert tuple(output.streamed_commands) == first_streamed
            assert not any(event.transfer is not None for event in second_events)
            assert not any(
                event.activity is not None
                and event.activity.action in {"cloning", "creating", "installing", "downloading"}
                for event in second_events
            )

    try:
        asyncio.run(exercise())
    finally:
        output.release()

    comfy_start = next(
        index
        for index, event in enumerate(events)
        if event.kind is OperationEventKind.STATE and event.message == "Instalando ComfyUI"
    )
    comfy_stop = next(
        index
        for index, event in enumerate(events[comfy_start + 1 :], start=comfy_start + 1)
        if event.kind is OperationEventKind.STATE and event.message != "Instalando ComfyUI"
    )
    comfy_facts = _semantic_facts(events, start=comfy_start, stop=comfy_stop)
    _assert_actions_in_order(
        comfy_facts,
        (
            "checking",
            "locating",
            "preparing",
            "cloning",
            "receiving",
            "resolving",
            "creating",
            "validating",
            "ready",
            "installing",
            "resolving",
            "downloading",
            "installing",
            "installed",
            "finalizing",
            "verifying",
            "installed",
        ),
    )
    assert any(fact.item == "repositório ComfyUI" for fact in comfy_facts)

    all_facts = _semantic_facts(events)
    for action in ("checking", "preparing", "verifying", "installed"):
        assert any(fact.item == "Ollama" and fact.action == action for fact in all_facts)
    assert any(fact.action == "downloading" for fact in all_facts)
    assert any(fact.action == "extracting" for fact in all_facts)
    for action in ("checking", "preparing", "verifying", "installed"):
        assert any(fact.item == "Open WebUI" and fact.action == action for fact in all_facts)
    assert any(
        fact.category is OperationActivityCategory.DEPENDENCY and fact.item == "open-webui"
        for fact in all_facts
    )
    assert {
        fact.item
        for fact in all_facts
        if fact.category is OperationActivityCategory.SERVICE and fact.action == "starting"
    } >= {"Ollama", "Open WebUI", "ComfyUI"}
    assert {
        fact.item
        for fact in all_facts
        if fact.category is OperationActivityCategory.READINESS and fact.action == "waiting"
    } >= {"Ollama", "Open WebUI", "ComfyUI"}
    assert {
        fact.item
        for fact in all_facts
        if fact.category is OperationActivityCategory.READINESS and fact.action == "ready"
    } >= {"Ollama", "Open WebUI", "ComfyUI"}
    assert any(
        fact.category is OperationActivityCategory.WORKFLOW and fact.action == "applied"
        for fact in all_facts
    )
    assert any(
        fact.category is OperationActivityCategory.CONFIGURATION and fact.action == "verified"
        for fact in all_facts
    )
    model_observation = next(
        event
        for event in events
        if event.activity is not None
        and event.activity.category is OperationActivityCategory.MODEL
        and event.activity.action == "downloading"
        and event.progress is not None
        and event.transfer is not None
    )
    assert model_observation.transfer.total_bytes is not None
    assert any(
        fact.category is OperationActivityCategory.MODEL and fact.action == "verified"
        for fact in all_facts
    )
    assert any(
        fact.category is OperationActivityCategory.MODEL and fact.action == "installed"
        for fact in all_facts
    )
    assert sum(event.kind is OperationEventKind.DETAIL for event in events) > DETAIL_HISTORY_LIMIT

    public = _public_event_text(events)
    for forbidden in (
        output.SECRET,
        "private-pass",
        output.PRIVATE_PATH,
        "--extra-index-url",
        "Traceback",
        "Looking in indexes",
    ):
        assert forbidden not in public
    assert_streams(events)


def test_real_pip_failure_keeps_sanitized_last_observations_in_tui(composition):
    world, adapter, _ = composition()
    output = ControlledInstallOutput(world, fail_comfy_pip=True).bind()
    events = []
    adapter.subscribe(events.append)
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await wait_for_observation(pilot, output.pip_partial)
            partial = app.operation_view
            assert partial is not None and partial.transfer is not None
            output.resume.set()
            await finish(pilot, adapter)
            assert app.operation_view.failed
            assert app.operation_view.activity == partial.activity
            assert app.operation_view.progress == partial.progress
            assert app.operation_view.transfer == partial.transfer
            assert events[-1].kind is OperationEventKind.RESULT
            assert events[-1].state is OperationState.FAILED
            done = Text.from_markup(
                str(app.screen.query_one("#wizard-step-done", Static).content)
            ).plain
            assert "Instalação não concluída" in done
            assert "Última atividade:" in done
            assert "Baixando dependências" in done and "torch" in done
            assert "158 MiB / 259 MiB" in done
            assert "Motivo:" in done and "Could not install dependencies" in done

    try:
        asyncio.run(exercise())
    finally:
        output.release()

    public = _public_event_text(events)
    for forbidden in (
        output.SECRET,
        "private-pass",
        output.PRIVATE_PATH,
        "--extra-index-url",
        "Traceback",
        "Looking in indexes",
    ):
        assert forbidden not in public
    assert_streams(events)


def test_real_model_download_reaches_the_real_wizard_and_second_run_is_quiet(composition):
    world, adapter, _ = composition()
    transfer = instrument_model_downloads(world, pause_at_final=True)
    events = []
    adapter.subscribe(events.append)
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await wait_for_observation(pilot, transfer.intermediate_ready)

            observed = transfer_events(events)
            assert len(observed) == 2
            assert transfer.chunks_delivered == 5
            assert observed[0].transfer.bytes_per_second is None
            current = observed[-1]
            assert current.transfer.completed_bytes > observed[0].transfer.completed_bytes
            assert current.transfer.total_bytes == MODEL_BYTES
            assert current.transfer.bytes_per_second == pytest.approx(1.5625 * 1024**2)
            assert current.transfer.eta_seconds is not None
            assert current.progress is not None and current.progress.total == 4
            rendered = assert_transfer_bar_fits(app, 120)
            assert f"{current.progress.completed}/4" in rendered
            assert "%" in rendered and "KiB / 1 MiB" in rendered
            assert "1.56 MiB/s" in rendered and "restantes" in rendered
            assert unsafe_characters(rendered) == ()

            for width in (80, 70):
                await pilot.resize_terminal(width, 32)
                await pilot.pause()
                resized = assert_transfer_bar_fits(app, width)
                assert "%" in resized and "1.56 MiB/s" in resized

            transfer.resume_intermediate.set()
            await wait_for_observation(pilot, transfer.final_ready)
            final_transfer = transfer_events(events)[-1]
            assert final_transfer.transfer.fraction == 1.0
            assert app.operation_view.transfer.fraction == 1.0
            assert "100%" in str(app.screen.query_one("#wizard-step-progress", Static).content)

            transfer.resume_final.set()
            await finish(pilot, adapter)
            assert app.operation_view.succeeded
            assert app.screen.state.step is WizardStep.DONE
            done = str(app.screen.query_one("#wizard-step-done", Static).content)
            assert "Concluído" in done
            assert "100%" not in done
            first_request_count = len(world.artifacts.requests)
            first_event_count = len(events)

            adapter.install(InstallSelection("capella"))
            await finish(pilot, adapter)
            second_events = events[first_event_count:]
            assert adapter.backend.last_first_run.did_nothing
            assert transfer_events(second_events) == []
            assert len(world.artifacts.requests) == first_request_count

    try:
        asyncio.run(exercise())
    finally:
        transfer.release()
    assert_streams(events)


def test_real_unknown_total_stays_indeterminate_in_the_real_wizard(composition):
    world, adapter, _ = composition()
    transfer = instrument_model_downloads(world, known_total=False)
    events = []
    adapter.subscribe(events.append)
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await wait_for_observation(pilot, transfer.intermediate_ready)

            observed = transfer_events(events)
            assert len(observed) == 2 < transfer.chunks_delivered
            current = observed[-1]
            assert current.transfer.completed_bytes > observed[0].transfer.completed_bytes
            assert current.transfer.total_bytes is None
            assert current.transfer.bytes_per_second == pytest.approx(1.5625 * 1024**2)
            assert current.transfer.eta_seconds is None
            rendered = str(app.screen.query_one("#wizard-step-progress", Static).content)
            plain = Text.from_markup(rendered).plain
            assert "transferidos" in plain
            assert "KiB" in plain and "1.56 MiB/s" in plain
            assert "%" not in plain and "restantes" not in plain
            assert "[#" not in plain and "[-" not in plain

            transfer.resume_intermediate.set()
            await finish(pilot, adapter)
            assert app.operation_view.succeeded

    try:
        asyncio.run(exercise())
    finally:
        transfer.release()
    assert_streams(events)


@pytest.mark.parametrize("failure", ["interrupted", "checksum"])
def test_real_model_failure_preserves_last_transfer_in_the_real_wizard(composition, failure):
    world, adapter, _ = composition()
    transfer = instrument_model_downloads(
        world,
        fail_mid_transfer=failure == "interrupted",
        corrupt=failure == "checksum",
        pause_at_final=failure == "checksum",
    )
    events = []
    adapter.subscribe(events.append)
    app, _ = tui(adapter)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await review(pilot)
            await click(pilot, "#wizard-next")
            await wait_for_observation(pilot, transfer.intermediate_ready)
            partial = transfer_events(events)[-1]
            assert 0 < partial.transfer.completed_bytes < partial.transfer.total_bytes
            assert partial.transfer.bytes_per_second is not None

            transfer.resume_intermediate.set()
            if failure == "checksum":
                await wait_for_observation(pilot, transfer.final_ready)
                at_checksum = transfer_events(events)[-1]
                assert at_checksum.transfer.fraction == 1.0
                assert "100%" in str(app.screen.query_one("#wizard-step-progress", Static).content)
                transfer.resume_final.set()

            await finish(pilot, adapter)
            assert app.operation_view.failed
            assert events[-1].kind is OperationEventKind.RESULT
            assert events[-1].state is OperationState.FAILED
            assert events.index(events[-1]) == len(events) - 1
            assert app.operation_view.transfer == transfer_events(events)[-1].transfer
            done = str(app.screen.query_one("#wizard-step-done", Static).content)
            assert "Instalação não concluída" in done
            if failure == "checksum":
                assert "100%" in done
            else:
                assert "100%" not in done
            assert not list(adapter.backend.layout.root.rglob("*.part"))
            assert not list(adapter.backend.layout.root.rglob("*.safetensors"))
            assert not adapter.backend.last_first_run.acquired

    try:
        asyncio.run(exercise())
    finally:
        transfer.release()
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


def test_real_tui_backend_alongside_custom_test_profile_and_second_run(composition):
    """A/G/H/I: preflight choice starts one real operation on isolated endpoints."""
    world, adapter, _ = composition(
        profile_id="test",
        profile_yaml=_test_profile_yaml(),
        vram_mib=12227,
        ready_ports=(11434, 8080, 8188, *ALONGSIDE_PORTS.values()),
    )
    ollama_external = external_install("ollama")
    comfy_external = external_install("comfyui")
    try:
        world.runner.on_path.update(ollama_external.on_path)
        world.runner.on_path.update(comfy_external.on_path)
        world.runner.fake_env.update(ollama_external.env)
        world.runner.fake_env.update(comfy_external.env)
        world.inventory.add_external(11434, pid=9101, executable=str(ollama_external.path))
        world.inventory.add_external(
            8188, pid=9102, executable=str(comfy_external.path), name="comfyui"
        )
        _use_isolated_launcher(world, "ollama", "comfyui")
        external_before = (ollama_external.snapshot(), comfy_external.snapshot())
        custom_root = world.root.path / "loira-40k"
        app, _ = tui(adapter)

        async def exercise():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await review(pilot, profile="test", mode="custom", root=custom_root)
                await click(pilot, "#wizard-next")

                assert app.screen.state.step is WizardStep.CONFLICT
                assert app.operation_view is None
                assert adapter.current_operation() is None
                report = adapter.current_conflict_preflight()
                assert report is not None
                assert {str(item.service_id) for item in report.conflicts} == {
                    str(ComponentId.OLLAMA),
                    str(ComponentId.COMFYUI),
                }
                alongside = app.screen.query_one("#wizard-conflict-alongside", Button)
                assert alongside.display and alongside.variant == "primary"

                await click(pilot, "#wizard-conflict-alongside")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await finish(pilot, adapter)

                assert app.operation_view.succeeded
                assert app.operation_view.operation_id is not None
                assert app.screen.state.step is WizardStep.DONE
                assert "Concluído" in str(app.screen.query_one("#wizard-title", Static).content)
                assert "Tudo pronto" in str(
                    app.screen.query_one("#wizard-step-done", Static).content
                )
                await click(pilot, "#wizard-finish")
                assert not isinstance(app.screen, WizardScreen)
                assert "Pronta" in str(app.query_one("#dashboard-overview", Static).content)

        asyncio.run(exercise())

        backend = adapter.backend
        endpoints = backend.options.endpoints_for(backend.profile("test"))
        assert endpoints.port(ComponentId.OLLAMA) == ALONGSIDE_PORTS["ollama"]
        assert endpoints.port(ComponentId.COMFYUI) == ALONGSIDE_PORTS["comfyui"]
        env_path = backend.layout.open_webui_data / "open-webui.env"
        env_text = env_path.read_text(encoding="utf-8")
        assert f"OLLAMA_BASE_URL=http://127.0.0.1:{ALONGSIDE_PORTS['ollama']}" in env_text
        assert f"COMFYUI_BASE_URL=http://127.0.0.1:{ALONGSIDE_PORTS['comfyui']}" in env_text
        assert {9101, 9102}.issubset(world.inventory.pids())
        assert external_before == (ollama_external.snapshot(), comfy_external.snapshot())

        stable_paths = (config_path(backend.layout), adapter.selection_path, env_path)
        before_files = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in stable_paths}
        before_work = (
            len(world.artifacts.requests),
            tuple(world.ollama.pulls),
            tuple(world.launcher.starts),
        )
        adapter.install(InstallSelection("test", InstallLocationMode.CUSTOM, str(custom_root)))
        adapter.wait(10)
        assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
        assert backend.last_first_run.did_nothing
        assert before_work == (
            len(world.artifacts.requests),
            tuple(world.ollama.pulls),
            tuple(world.launcher.starts),
        )
        assert before_files == {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in stable_paths
        }
        assert {9101, 9102}.issubset(world.inventory.pids())
    finally:
        ollama_external.release()
        comfy_external.release()


def test_real_tui_stop_confirmation_uses_only_adapter_preflight_authority(composition):
    """B/F/I: confirmation forwards only the frozen selection and preserves strangers."""
    world, adapter, _ = composition(
        profile_id="test",
        profile_yaml=_test_profile_yaml(),
        vram_mib=12227,
        ready_ports=(11434, 8080, 8188),
    )
    external = external_install("ollama")
    try:
        world.runner.on_path.update(external.on_path)
        world.runner.fake_env.update(external.env)
        world.inventory.add_external(11434, pid=9201, executable=str(external.path))
        world.inventory.occupy(39999)
        external_before = external.snapshot()
        stopped: list[tuple[int, float]] = []

        def terminate(pid: int, create_time: float) -> bool:
            process = world.inventory.lookup_process(pid).process
            if process is None or process.create_time != create_time:
                return False
            stopped.append((pid, create_time))
            world.inventory.kill(pid)
            return True

        world.app.deps.terminate_process = terminate
        selections = []
        resolve = adapter.resolve_external_conflicts

        def record_selection(selection):
            selections.append(selection)
            return resolve(selection)

        adapter.resolve_external_conflicts = record_selection
        app, _ = tui(adapter)

        async def exercise():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await review(pilot, profile="test")
                await click(pilot, "#wizard-next")
                assert app.screen.state.step is WizardStep.CONFLICT
                assert adapter.current_operation() is None

                await click(pilot, "#wizard-conflict-stop")
                assert app.screen.state.step is WizardStep.CONFLICT_CONFIRM
                assert selections == [] and stopped == []
                await click(pilot, "#wizard-confirm-stop")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await finish(pilot, adapter)
                assert app.operation_view.succeeded
                assert app.screen.state.step is WizardStep.DONE

        asyncio.run(exercise())

        assert len(selections) == 1
        assert selections[0].policy is ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
        assert set(vars(selections[0])) == {"preflight_id", "policy"}
        assert stopped == [(9201, 1.0)]
        assert 9201 not in world.inventory.pids()
        assert UNRELATED_PID in world.inventory.pids()
        assert external.snapshot() == external_before

        before_work = (len(world.artifacts.requests), tuple(world.ollama.pulls))
        adapter.install(InstallSelection("test"))
        adapter.wait(10)
        assert adapter.backend.last_first_run.did_nothing
        assert before_work == (len(world.artifacts.requests), tuple(world.ollama.pulls))
        assert UNRELATED_PID in world.inventory.pids()
    finally:
        external.release()


def test_real_tui_unsafe_external_hides_stop_and_allows_alongside(composition):
    """C: ambiguous process identity never becomes stop authority in the UI."""
    world, adapter, _ = composition(
        profile_id="test",
        profile_yaml=_test_profile_yaml(),
        vram_mib=12227,
        ready_ports=(11434, 8080, 8188, ALONGSIDE_PORTS["ollama"]),
    )
    external = external_install("ollama")
    try:
        world.runner.on_path.update(external.on_path)
        world.runner.fake_env.update(external.env)
        world.inventory.add_external(11434, pid=9301, executable=str(external.path))
        world.inventory.listeners.append(Listener("127.0.0.1", 11434, 9302))
        _use_isolated_launcher(world, "ollama")
        app, _ = tui(adapter)

        async def exercise():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await review(pilot, profile="test")
                await click(pilot, "#wizard-next")
                report = adapter.current_conflict_preflight()
                assert report is not None and not report.conflicts[0].can_stop_safely
                assert not app.screen.query_one("#wizard-conflict-stop", Button).display
                assert app.screen.query_one("#wizard-conflict-alongside", Button).display

                await click(pilot, "#wizard-conflict-alongside")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await finish(pilot, adapter)
                assert app.operation_view.succeeded

        asyncio.run(exercise())
        assert 9301 in world.inventory.pids()
    finally:
        external.release()


def test_real_tui_blocked_alternate_endpoint_cancels_without_mutation(composition):
    """D/E: no safe policy means human BLOCKED UX, no operation and no third port."""
    world, adapter, _ = composition(
        profile_id="test",
        profile_yaml=_test_profile_yaml(),
        vram_mib=12227,
        ready_ports=(11434, 8080, 8188),
    )
    external = external_install("ollama")
    try:
        world.runner.on_path.update(external.on_path)
        world.runner.fake_env.update(external.env)
        world.inventory.add_external(11434, pid=9401, executable=str(external.path))
        world.inventory.listeners.append(Listener("127.0.0.1", 11434, 9402))
        world.inventory.occupy(ALONGSIDE_PORTS["ollama"], pid=9403)
        pids_before = world.inventory.pids()
        files_before = external.snapshot()
        custom_root = world.root.path / "blocked-custom"
        app, _ = tui(adapter)

        async def exercise():
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await review(pilot, profile="test", mode="custom", root=custom_root)
                await click(pilot, "#wizard-next")
                report = adapter.current_conflict_preflight()
                assert report is not None and report.disposition.value == "blocked"
                first_id = report.preflight_id
                assert not app.screen.query_one("#wizard-conflict-alongside", Button).display
                assert not app.screen.query_one("#wizard-conflict-stop", Button).display
                shown = str(app.screen.query_one("#wizard-conflict-report", Static).content)
                assert "nenhuma resolução segura" in shown
                assert adapter.current_operation() is None

                await click(pilot, "#wizard-conflict-abort")
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert app.screen.state.step is WizardStep.REVIEW
                assert app.screen.state.custom_root == str(custom_root)
                assert adapter.current_operation() is None

                await click(pilot, "#wizard-next")
                retry = adapter.current_conflict_preflight()
                assert retry is not None and retry.preflight_id != first_id
                assert app.screen.state.step is WizardStep.CONFLICT
                assert adapter.current_operation() is None

        asyncio.run(exercise())

        assert world.inventory.pids() == pids_before
        assert external.snapshot() == files_before
        assert not custom_root.exists()
        assert adapter.backend.options.port_overrides == {}
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
