"""The bridge from a real model download to a byte-transfer observer.

``InstallActionHandlers.acquire_model`` is orchestration: IC5's
``ModelInstaller`` already does the downloading, checksum verification and
Ollama pulling. What is tested here is only the seam this round added -- that
raw :class:`~local_ai_stack.download.DownloadProgress` observations reach
``on_transfer`` keyed by the plan's own target, and only when there really is
an HTTP transfer to report on.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from local_ai_stack.artifacts import Artifact
from local_ai_stack.download import Downloader, DownloadProgress
from local_ai_stack.install_actions import InstallActionHandlers
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.model_catalog import ModelCatalog
from local_ai_stack.model_installer import ModelInstaller, ModelOutcome
from local_ai_stack.models import FileModelSource, ManagedModelPaths, ModelId, ModelKind
from local_ai_stack.ollama_models import OllamaModelEntry
from local_ai_stack.planning import InstallPlan, PlanAction, PlanStep
from local_ai_stack.profiles import load_profile

REPO = Path(__file__).resolve().parents[1]
WEIGHTS = b"install-actions-bridge-bytes" * 32
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()
FILE_ID = ModelId("comfyui/demo_diffusion")
OLLAMA_ID = ModelId("ollama/demo_8b")


class Collector:
    """Matches the two-argument ``on_transfer`` signature and records calls."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, DownloadProgress]] = []

    def __call__(self, target: str, raw: DownloadProgress) -> None:
        self.seen.append((target, raw))

    def __bool__(self) -> bool:
        return bool(self.seen)

    def __iter__(self):
        return iter(self.seen)

    def __getitem__(self, index):
        return self.seen[index]

    def clear(self) -> None:
        self.seen.clear()


class FakeOllama:
    def __init__(self) -> None:
        self.pulls: list[str] = []

    def list_models(self):
        return tuple(
            OllamaModelEntry(name=ref, digest="sha256:4f6b83f30b62aaaa") for ref in self.pulls
        )

    def pull(self, reference: str) -> None:
        self.pulls.append(reference)


def catalog_and_installer(tmp_path: Path, *, transport: httpx.MockTransport | None = None):
    installation = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    artifact = Artifact(
        id="demo-diffusion",
        url="https://example.invalid/demo.safetensors",
        filename="demo.safetensors",
        sha256=WEIGHTS_SHA,
    )
    from local_ai_stack.models import ModelPlacement, ModelSpec, OllamaModelSource

    catalog = ModelCatalog(
        specs={
            FILE_ID: ModelSpec(
                FILE_ID,
                ModelKind.DIFFUSION,
                FileModelSource(artifact, ModelPlacement.DIFFUSION_MODELS),
            ),
            OLLAMA_ID: ModelSpec(OLLAMA_ID, ModelKind.CHAT, OllamaModelSource("demo:8b", None)),
        }
    )
    if transport is None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=WEIGHTS))
    downloader = Downloader(client_factory=lambda: httpx.Client(transport=transport))
    installer = ModelInstaller(
        ManagedModelPaths.from_layout(installation.layout),
        downloader=downloader,
        ollama=FakeOllama(),
    )
    return installation, catalog, installer


def handlers_for(
    tmp_path: Path,
    catalog: ModelCatalog,
    installer: ModelInstaller,
    installation,
    *,
    on_transfer=None,
) -> InstallActionHandlers:
    return InstallActionHandlers(
        profiles={"pepe": load_profile(REPO / "profiles" / "pepe.yaml")},
        catalog=catalog,
        installer_factory=lambda _profile: installer,
        workflow_specs={},
        repo_root=tmp_path,
        layout=installation.layout,
        on_transfer=on_transfer,
    )


def plan_for(target: str) -> InstallPlan:
    step = PlanStep(sequence=1, action=PlanAction.ACQUIRE_MODEL, target=target, summary="acquire")
    return InstallPlan(profile_id="pepe", steps=(step,))


def test_a_real_transfer_reaches_on_transfer_keyed_by_the_plan_target(tmp_path):
    installation, catalog, installer = catalog_and_installer(tmp_path)
    seen = Collector()
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=seen)
    plan = plan_for(str(FILE_ID))

    result = actions.acquire_model(plan, plan.steps[0], dry_run=False)

    assert result.ok and result.changed
    assert seen
    targets = {target for target, _raw in seen}
    assert targets == {str(FILE_ID)}
    assert seen[-1][1].downloaded_bytes == len(WEIGHTS)


def test_without_a_sink_nothing_is_collected_and_acquisition_still_succeeds(tmp_path):
    installation, catalog, installer = catalog_and_installer(tmp_path)
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=None)
    plan = plan_for(str(FILE_ID))

    result = actions.acquire_model(plan, plan.steps[0], dry_run=False)

    assert result.ok and result.changed


def test_a_dry_run_never_calls_the_sink(tmp_path):
    installation, catalog, installer = catalog_and_installer(tmp_path)
    seen = Collector()
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=seen)
    plan = plan_for(str(FILE_ID))

    result = actions.acquire_model(plan, plan.steps[0], dry_run=True)

    assert result.payload.outcome is ModelOutcome.DRY_RUN
    assert not seen


def test_an_already_available_model_never_calls_the_sink(tmp_path):
    installation, catalog, installer = catalog_and_installer(tmp_path)
    seen = Collector()
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=seen)
    plan = plan_for(str(FILE_ID))
    first = actions.acquire_model(plan, plan.steps[0], dry_run=False)
    assert first.changed
    seen.clear()

    second = actions.acquire_model(plan, plan.steps[0], dry_run=False)

    assert second.ok and not second.changed
    assert not seen


def test_an_ollama_pull_never_calls_the_byte_sink(tmp_path):
    installation, catalog, installer = catalog_and_installer(tmp_path)
    seen = Collector()
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=seen)
    plan = plan_for(str(OLLAMA_ID))

    result = actions.acquire_model(plan, plan.steps[0], dry_run=False)

    assert result.ok and result.changed
    assert not seen


def test_a_checksum_failure_still_reports_the_bytes_that_arrived(tmp_path):
    """A failed acquisition is not silence: the transfer that did happen is real."""
    bad = httpx.MockTransport(lambda request: httpx.Response(200, content=WEIGHTS + b"-tampered"))
    installation, catalog, installer = catalog_and_installer(tmp_path, transport=bad)
    seen = Collector()
    actions = handlers_for(tmp_path, catalog, installer, installation, on_transfer=seen)
    plan = plan_for(str(FILE_ID))

    result = actions.acquire_model(plan, plan.steps[0], dry_run=False)

    assert not result.ok
    assert seen
    assert seen[-1][1].downloaded_bytes == len(WEIGHTS) + len(b"-tampered")
