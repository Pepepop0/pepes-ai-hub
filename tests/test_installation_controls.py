"""Real coordinator, downloader and runner behavior under cooperative requests."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from first_run_support import CAPELLA, controlled_stack

from local_ai_stack.download import Downloader, DownloadSpec
from local_ai_stack.hub_application import HubApplicationAdapter
from local_ai_stack.installation_control import InstallationCancelled, InstallationControl
from local_ai_stack.models import FileModelSource
from local_ai_stack.ollama_models import HttpOllamaClient
from local_ai_stack.operation_contracts import OperationActivityCategory, OperationState
from local_ai_stack.runner import CommandRunner
from local_ai_stack.tui.application import HubError, InstallSelection


def await_state(snapshot, expected: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = snapshot()
        if current is not None and current["state"] == expected:
            return current
        threading.Event().wait(0.005)
    raise AssertionError(f"expected {expected}, got {snapshot()}")


def controlled_call(control, fn):
    with control.scope():
        return fn()


@pytest.mark.parametrize("stage", ["artifact", "model"])
@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_actual_installation_stops_at_transfer_and_reuses_completed_files(tmp_path, stage, action):
    stack = controlled_stack()

    # Fixture archives finish within one instant of StepClock. Advance its clock
    # on reads so the real progress sampler publishes transfer observations.
    def ticking_monotonic():
        stack.clock.advance(0.5)
        return stack.clock.monotonic()

    stack.app.deps.monotonic = ticking_monotonic
    adapter = HubApplicationAdapter(
        profiles=tuple(stack.app.profiles.values()),
        resolve=lambda _location: stack.app.installation,
        backend_factory=lambda _installation: stack.app,
        selection_path=tmp_path / "hub-state.yaml",
    )
    requested = threading.Event()
    model_targets: set[str] = set()
    endings = []

    def observe(event):
        if event.state.terminal:
            endings.append(event)
        if requested.is_set() or event.transfer is None or event.activity is None:
            return
        if stage == "model":
            if event.activity.category is not OperationActivityCategory.MODEL:
                return
            model_targets.add(event.activity.item)
            if len(model_targets) < 2:
                return
        elif event.activity.category is not OperationActivityCategory.TRANSFER:
            return
        adapter.control_installation("pause", event.operation_id)
        requested.set()

    subscription = adapter.subscribe(observe)
    try:
        operation_id = adapter.install(InstallSelection(CAPELLA))
        assert requested.wait(10)
        paused = await_state(adapter.installation_control, "paused")
        assert paused["operation_id"] == operation_id
        assert not paused["can_pause"] and paused["can_cancel"]
        assert not adapter._idle.wait(0.05)
        before = Counter(stack.artifacts.requests)
        commands = list(stack.runner.commands)
        started = [list(launcher.starts) for launcher in stack.launchers]
        assert not endings
        with pytest.raises(HubError):
            adapter.control_installation("cancel", "stale-operation")
        assert adapter.installation_control()["state"] == "paused"
        assert Counter(stack.artifacts.requests) == before
        assert stack.runner.commands == commands
        assert [list(launcher.starts) for launcher in stack.launchers] == started

        completed = {}
        if stage == "model":
            installer = stack.app.model_installer(stack.app.profile(CAPELLA))
            for spec in stack.app.catalog().specs.values():
                if isinstance(spec.source, FileModelSource):
                    path = installer.paths.destination_for(spec)
                    if path.is_file():
                        completed[spec.source.artifact.url] = (path, path.read_bytes())
            assert completed, "an earlier model finished before the second model paused"
        adapter.control_installation(action, operation_id)
        adapter.wait(20)
        assert adapter.installation_control() is None
        assert not adapter._busy and adapter._idle.is_set()
        assert len(endings) == 1
        if action == "cancel":
            assert endings[0].state is OperationState.FAILED
            assert endings[0].result.diagnostics[0].code == "cancelled"
            assert adapter.active_root() is None, "a cancelled candidate is never adopted"
            assert list(stack.app.layout.root.rglob("*.part")), "resumable bytes survive"
            assert not stack.inventory.processes
            for path, payload in completed.values():
                assert path.read_bytes() == payload
            assert adapter.retryable_installation()["operation_id"] == operation_id
            subscription.close()
            later_id = adapter.retry_installation(operation_id)
            assert later_id != operation_id
            with pytest.raises(HubError):
                adapter.control_installation("cancel", operation_id)
            adapter.wait(20)
            assert adapter.current_operation().last_event.state is OperationState.SUCCEEDED
            for url, (path, payload) in completed.items():
                assert path.read_bytes() == payload
                assert stack.artifacts.requests.count(url) == before[url]
        else:
            assert endings[0].state is OperationState.SUCCEEDED
            assert adapter.active_root() == stack.app.installation.root
        with pytest.raises(HubError):
            adapter.control_installation("cancel", operation_id)
    finally:
        active = adapter.installation_control()
        if active is not None:
            adapter.control_installation("cancel", active["operation_id"])
        adapter.close()
        stack.release()


def test_desktop_can_cancel_a_paused_repair_of_an_incomplete_installation(tmp_path):
    from local_ai_stack.config import InstalledConfig
    from local_ai_stack.desktop.api import DesktopController, DesktopError

    stack = controlled_stack()
    root = stack.app.installation.root
    adapter = HubApplicationAdapter(
        profiles=tuple(stack.app.profiles.values()),
        resolve=lambda _location: stack.app.installation,
        backend_factory=lambda _installation: stack.app,
        selection_path=tmp_path / "hub-state.yaml",
        config=InstalledConfig(CAPELLA, installation=stack.app.installation.location),
    )
    controller = DesktopController(adapter, folder_picker=object())
    requested = threading.Event()

    def pause_model(event):
        if (
            not requested.is_set()
            and event.transfer is not None
            and event.activity is not None
            and event.activity.category is OperationActivityCategory.MODEL
        ):
            adapter.control_installation("pause", event.operation_id)
            requested.set()

    subscription = adapter.subscribe(pause_model)
    try:
        started = controller.command("repair", {})
        assert requested.wait(10)
        await_state(adapter.installation_control, "paused")
        with pytest.raises(DesktopError):
            controller.command("cancel_installation", {"operation_id": "obsolete-operation"})
        assert adapter.installation_control()["state"] == "paused"
        reply = controller.command("cancel_installation", {"operation_id": started["operation_id"]})
        assert "operation_id" not in reply, "a control must not replace the tracked operation"
        adapter.wait(20)
        assert adapter.active_root() == root
        assert adapter.current_operation().last_event.diagnostic.code == "cancelled"
        assert adapter.installation_control() is None
        assert not controller.operation_busy()
        assert list(root.rglob("*.part"))
        assert not stack.inventory.processes
    finally:
        active = adapter.installation_control()
        if active is not None:
            adapter.control_installation("cancel", active["operation_id"])
        subscription.close()
        controller.close()
        stack.release()


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_segmented_workers_all_park_and_preserve_the_completed_prefix(tmp_path, action):
    control = InstallationControl("ranged")
    payload = bytes(range(256)) * 8
    spec = DownloadSpec(
        "https://controlled.invalid/model.bin",
        tmp_path / "model.bin",
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )
    requested = threading.Event()
    asked = []
    delivered = []

    def handler(request):
        header = request.headers["range"]
        asked.append(header)
        first, last = header.removeprefix("bytes=").split("-")
        start, end = int(first), int(last)

        def stream():
            for offset in range(start, end + 1, 64):
                delivered.append(offset)
                if not requested.is_set():
                    control.request("pause")
                    requested.set()
                yield payload[offset : min(offset + 64, end + 1)]

        return httpx.Response(
            206,
            content=stream(),
            headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
        )

    downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        chunk_size=64,
        connections=2,
        max_attempts=1,
    )
    part = downloader.part_path(spec.destination)
    part.write_bytes(payload[:512] + bytes(len(payload) - 512))
    part.with_name(part.name + ".json").write_text(
        json.dumps(
            {
                "version": 1,
                "identity": spec.identity,
                "segments": [[0, 511, 512], [512, 1279, 0], [1280, 2047, 0]],
            }
        ),
        encoding="utf-8",
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled_call, control, lambda: downloader.fetch(spec))
        try:
            assert requested.wait(10)
            await_state(control.snapshot, "paused")
            count = len(delivered)
            assert not future.done()
            threading.Event().wait(0.05)
            assert len(delivered) == count, "every range, not just the coordinator, is parked"
            assert not spec.destination.exists()
            control.request(action)
            if action == "cancel":
                with pytest.raises(InstallationCancelled):
                    future.result(10)
                assert part.is_file()
                assert part.read_bytes()[:512] == payload[:512]
                assert not spec.destination.exists()
                # A different run resumes only unfinished ranges.
                requested.set()
                downloader.fetch(spec)
            else:
                assert future.result(10).verified
            assert spec.destination.read_bytes() == payload
            assert all(not header.startswith("bytes=0-") for header in asked)
        finally:
            control.request("cancel")


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_ollama_closes_its_request_before_acknowledging_pause(action):
    control = InstallationControl("ollama")
    closed = threading.Event()
    requested = threading.Event()
    requests = []

    class PullStream(httpx.SyncByteStream):
        def __iter__(self):
            if len(requests) == 1:
                yield b'{"status":"downloading","completed":64,"total":128}\n'
                control.request("pause")
                requested.set()
                yield b'{"status":"downloading","completed":128,"total":128}\n'
            yield b'{"status":"success"}\n'

        def close(self):
            closed.set()

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=PullStream())

    client = HttpOllamaClient(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controlled_call, control, lambda: client.pull("fixture:1b"))
        try:
            assert requested.wait(10)
            await_state(control.snapshot, "paused")
            assert closed.is_set(), "an open pull would keep downloading on the Ollama server"
            assert not future.done()
            assert requests == [{"model": "fixture:1b", "stream": True}]
            control.request(action)
            if action == "cancel":
                with pytest.raises(InstallationCancelled):
                    future.result(10)
                assert len(requests) == 1
            else:
                future.result(10)
                assert requests == [{"model": "fixture:1b", "stream": True}] * 2
        finally:
            control.request("cancel")


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_external_command_finishes_normally_before_request_is_acknowledged(tmp_path, action):
    control = InstallationControl("command")
    ready = threading.Event()
    gate = tmp_path / "finish-command"
    completed = tmp_path / "command-finished"
    script = (
        "import pathlib, sys, time\n"
        "print('ready', flush=True)\n"
        "gate = pathlib.Path(sys.argv[1])\n"
        "while not gate.exists(): time.sleep(0.005)\n"
        "pathlib.Path(sys.argv[2]).write_text('completed')\n"
    )
    runner = CommandRunner()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            controlled_call,
            control,
            lambda: runner.run(
                [sys.executable, "-c", script, str(gate), str(completed)],
                timeout=10,
                on_output=lambda _stream, line: ready.set() if line == "ready" else None,
            ),
        )
        try:
            assert ready.wait(10)
            assert control.request(action)["state"] == (
                "pausing" if action == "pause" else "cancelling"
            )
            assert not future.done()
            assert not completed.exists()
            gate.write_text("finish", encoding="utf-8")
            if action == "pause":
                await_state(control.snapshot, "paused")
                assert completed.read_text() == "completed"
                control.request("resume")
                assert future.result(10).ok
            else:
                with pytest.raises(InstallationCancelled):
                    future.result(10)
                assert completed.read_text() == "completed", "cancel never kills the child"
        finally:
            gate.write_text("finish", encoding="utf-8")
            control.request("cancel")


def test_cancel_after_backend_success_never_commits_and_stops_only_owned_services(tmp_path):
    stack = controlled_stack()
    adapter = HubApplicationAdapter(
        profiles=tuple(stack.app.profiles.values()),
        resolve=lambda _location: stack.app.installation,
        backend_factory=lambda _installation: stack.app,
        selection_path=tmp_path / "hub-state.yaml",
    )
    first_run = stack.app.first_run

    def finish_then_cancel(operation_id, profile_id, *, emit=None):
        result = first_run(operation_id, profile_id, emit=emit)
        assert result.state is OperationState.SUCCEEDED
        adapter.control_installation("cancel", operation_id)
        return result

    stack.app.first_run = finish_then_cancel
    try:
        adapter.install(InstallSelection(CAPELLA))
        adapter.wait(20)
        assert adapter.active_root() is None
        assert adapter.installation_control() is None
        ending = adapter.current_operation().last_event
        assert ending.state is OperationState.FAILED
        assert ending.result.diagnostics[0].code == "cancelled"
        assert not stack.inventory.processes
        assert stack.launcher.stops == ["comfyui", "open-webui", "ollama"]
        assert list(stack.app.layout.models.rglob("*.safetensors"))
    finally:
        adapter.close()
        stack.release()


def test_backend_failure_also_releases_installation_controls(tmp_path):
    stack = controlled_stack(vram_mib=1)
    adapter = HubApplicationAdapter(
        profiles=tuple(stack.app.profiles.values()),
        resolve=lambda _location: stack.app.installation,
        backend_factory=lambda _installation: stack.app,
        selection_path=tmp_path / "hub-state.yaml",
    )
    try:
        operation_id = adapter.install(InstallSelection(CAPELLA))
        adapter.wait(20)
        assert adapter.current_operation().last_event.state is OperationState.FAILED
        assert adapter.installation_control() is None
        assert not adapter._busy and adapter._idle.is_set()
        with pytest.raises(HubError):
            adapter.control_installation("pause", operation_id)
    finally:
        adapter.close()
        stack.release()
