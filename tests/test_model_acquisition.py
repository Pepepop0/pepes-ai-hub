"""Model acquisition and resolution, end to end against fakes.

No model is ever downloaded or pulled here: HTTP is an httpx MockTransport, the
Ollama store is a fake, and every file lives under tmp_path. Nothing in this
file touches the real Ollama on the host.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from local_ai_stack.artifacts import Artifact
from local_ai_stack.download import Downloader
from local_ai_stack.model_catalog import ModelCatalog, validate_model_manifest
from local_ai_stack.model_installer import (
    ModelFailure,
    ModelInstaller,
    ModelOutcome,
)
from local_ai_stack.model_resolver import (
    StackModelResolver,
    profile_model_specs,
    unresolved_roles,
)
from local_ai_stack.models import (
    FileModelLocation,
    ManagedModelPaths,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelResolver,
    ModelSpec,
    ModelState,
    ModelUnavailableError,
    OllamaModelLocation,
    OllamaModelSource,
    ResolvedModel,
)
from local_ai_stack.ollama_models import OllamaClientError, OllamaModelEntry
from local_ai_stack.profiles import load_profiles, validate_profile_data

REPO = Path(__file__).resolve().parents[1]

WEIGHTS = b"safetensors-bytes" * 512
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()
OTHER_SHA = "b" * 64

FILE_ID = ModelId("comfyui/demo_diffusion")
OLLAMA_ID = ModelId("ollama/demo_8b")


def file_spec(sha: str = WEIGHTS_SHA, filename: str = "demo.safetensors") -> ModelSpec:
    return ModelSpec(
        id=FILE_ID,
        kind=ModelKind.DIFFUSION,
        source=__import__("local_ai_stack.models", fromlist=["FileModelSource"]).FileModelSource(
            artifact=Artifact(
                id="demo-diffusion",
                url="https://example.invalid/demo.safetensors",
                filename=filename,
                sha256=sha,
                version="1.0",
            ),
            placement=ModelPlacement.DIFFUSION_MODELS,
        ),
    )


def ollama_spec(digest: str | None = "4f6b83f30b62") -> ModelSpec:
    return ModelSpec(
        id=OLLAMA_ID,
        kind=ModelKind.CHAT,
        source=OllamaModelSource(reference="demo:8b", digest=digest),
    )


class FakeOllama:
    """A model store that never removes anything, like the real one."""

    def __init__(self, *, installed: tuple[OllamaModelEntry, ...] = (), fail: str | None = None):
        self.installed = list(installed)
        self.fail = fail
        self.pulls: list[str] = []

    def list_models(self):
        if self.fail == "list":
            raise OllamaClientError("store unreachable")
        return tuple(self.installed)

    def pull(self, reference: str) -> None:
        if self.fail == "pull":
            raise OllamaClientError(f"could not pull {reference}")
        self.pulls.append(reference)
        self.installed.append(
            OllamaModelEntry(name=reference, digest="sha256:4f6b83f30b62aaaa", size_bytes=1)
        )


def serving(body: bytes = WEIGHTS) -> Downloader:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    return Downloader(client_factory=lambda: httpx.Client(transport=transport))


def refusing() -> Downloader:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    return Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(explode)),
        max_attempts=1,
    )


def installer_for(tmp_path: Path, **kwargs) -> ModelInstaller:
    paths = ManagedModelPaths(tmp_path / "stack" / "models")
    kwargs.setdefault("downloader", serving())
    return ModelInstaller(paths=paths, **kwargs)


# --- ComfyUI: estados ------------------------------------------------------


def test_a_missing_file_is_not_installed(tmp_path):
    status = installer_for(tmp_path).detect(file_spec())
    assert status.state is ModelState.NOT_INSTALLED
    assert status.path is None or not status.path.exists()


def test_detect_creates_nothing(tmp_path):
    installer_for(tmp_path).detect(file_spec())
    assert not (tmp_path / "stack").exists()


def test_a_valid_file_is_available(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    destination = installer.paths.destination_for(spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(WEIGHTS)

    status = installer.detect(spec)
    assert status.state is ModelState.AVAILABLE
    assert status.path == destination


def test_a_corrupted_file_is_invalid_not_available(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    destination = installer.paths.destination_for(spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"truncated")

    status = installer.detect(spec)
    assert status.state is ModelState.INVALID
    assert "checksum" in status.detail


def test_a_repinned_checksum_makes_the_existing_file_invalid(tmp_path):
    """Same filename, new pin: the old bytes must not pass as current."""
    installer = installer_for(tmp_path)
    old = file_spec()
    destination = installer.paths.destination_for(old)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(WEIGHTS)
    assert installer.detect(old).state is ModelState.AVAILABLE

    repinned = file_spec(sha=OTHER_SHA)
    assert installer.detect(repinned).state is ModelState.INVALID


# --- ComfyUI: aquisicao ----------------------------------------------------


def test_apply_downloads_into_the_managed_tree(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    result = installer.apply(spec)

    assert result.outcome is ModelOutcome.ACQUIRED
    destination = installer.paths.destination_for(spec)
    assert destination.read_bytes() == WEIGHTS
    assert destination.parent.name == ModelPlacement.DIFFUSION_MODELS.value
    assert destination.parent.parent.name == "comfyui"


def test_managed_model_paths_is_the_only_authority_on_placement(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    installer.apply(spec)
    assert installer.paths.destination_for(spec).is_file()


def test_apply_is_idempotent(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    assert installer.apply(spec).outcome is ModelOutcome.ACQUIRED

    calls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=WEIGHTS)

    installer.downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(record))
    )
    assert installer.apply(spec).outcome is ModelOutcome.SKIPPED
    assert calls == []


def test_a_checksum_mismatch_never_produces_a_file(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec(sha=OTHER_SHA)
    result = installer.apply(spec)

    assert result.outcome is ModelOutcome.FAILED
    assert result.failure is ModelFailure.CHECKSUM
    assert not installer.paths.destination_for(spec).exists()


def test_a_partial_download_never_becomes_available(tmp_path):
    """The transfer fails mid-flight; nothing is promoted and nothing resolves."""
    installer = installer_for(tmp_path, downloader=refusing())
    spec = file_spec()
    result = installer.apply(spec)

    assert result.outcome is ModelOutcome.FAILED
    assert result.failure is ModelFailure.NETWORK
    destination = installer.paths.destination_for(spec)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.part")) if destination.parent.exists() else True
    assert installer.detect(spec).state is ModelState.NOT_INSTALLED


def test_apply_reports_progress_with_a_known_content_length(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    seen = []

    result = installer.apply(spec, progress=seen.append)

    assert result.outcome is ModelOutcome.ACQUIRED
    assert seen
    assert seen[0].total_bytes == len(WEIGHTS)
    assert seen[-1].downloaded_bytes == len(WEIGHTS)
    assert seen[-1].total_bytes == len(WEIGHTS)


def test_apply_reports_progress_with_no_content_length(tmp_path):
    """A chunked response with no size hint reports a growing count and no total."""

    def chunked(request: httpx.Request) -> httpx.Response:
        def body():
            yield WEIGHTS[: len(WEIGHTS) // 2]
            yield WEIGHTS[len(WEIGHTS) // 2 :]

        return httpx.Response(200, content=body())

    downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(chunked))
    )
    installer = installer_for(tmp_path, downloader=downloader)
    spec = file_spec()
    assert spec.source.artifact.size_bytes is None
    seen = []

    result = installer.apply(spec, progress=seen.append)

    assert result.outcome is ModelOutcome.ACQUIRED
    assert seen
    assert all(item.total_bytes is None for item in seen)
    assert seen[-1].downloaded_bytes == len(WEIGHTS)


def test_a_skipped_acquisition_never_calls_progress(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    installer.apply(spec)
    seen = []

    result = installer.apply(spec, progress=seen.append)

    assert result.outcome is ModelOutcome.SKIPPED
    assert seen == []


def test_a_dry_run_never_calls_progress(tmp_path):
    installer = installer_for(tmp_path)
    seen = []

    result = installer.apply(file_spec(), dry_run=True, progress=seen.append)

    assert result.outcome is ModelOutcome.DRY_RUN
    assert seen == []


def test_an_ollama_pull_never_calls_the_byte_progress_callback(tmp_path):
    """Ollama acquisition has no HTTP transfer of its own to report on."""
    store = FakeOllama()
    installer = installer_for(tmp_path, ollama=store)
    seen = []

    result = installer.apply(ollama_spec(), progress=seen.append)

    assert result.outcome is ModelOutcome.ACQUIRED
    assert seen == []


def test_dry_run_writes_nothing(tmp_path):
    calls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=WEIGHTS)

    installer = installer_for(
        tmp_path,
        downloader=Downloader(
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(record))
        ),
    )
    spec = file_spec()
    result = installer.apply(spec, dry_run=True)

    assert result.outcome is ModelOutcome.DRY_RUN
    assert result.steps
    assert calls == []
    assert not (tmp_path / "stack").exists()


# --- Ollama ----------------------------------------------------------------


def test_a_missing_ollama_model_is_not_installed(tmp_path):
    installer = installer_for(tmp_path, ollama=FakeOllama())
    status = installer.detect(ollama_spec())
    assert status.state is ModelState.NOT_INSTALLED
    assert status.path is None


def test_an_unreachable_store_fails_closed(tmp_path):
    installer = installer_for(tmp_path, ollama=FakeOllama(fail="list"))
    status = installer.detect(ollama_spec())
    assert status.state is ModelState.NOT_INSTALLED
    assert "unreachable" in status.detail


def test_a_present_ollama_model_is_available_without_any_path(tmp_path):
    store = FakeOllama(
        installed=(OllamaModelEntry(name="demo:8b", digest="sha256:4f6b83f30b62aaaa"),)
    )
    status = installer_for(tmp_path, ollama=store).detect(ollama_spec())
    assert status.state is ModelState.AVAILABLE
    assert status.path is None
    assert status.reference == "demo:8b"


def test_a_republished_tag_is_invalid(tmp_path):
    store = FakeOllama(installed=(OllamaModelEntry(name="demo:8b", digest="sha256:deadbeef"),))
    status = installer_for(tmp_path, ollama=store).detect(ollama_spec())
    assert status.state is ModelState.INVALID
    assert "republished" in status.detail


def test_a_model_without_a_pinned_digest_accepts_whatever_is_present(tmp_path):
    store = FakeOllama(installed=(OllamaModelEntry(name="demo:8b", digest="sha256:whatever"),))
    status = installer_for(tmp_path, ollama=store).detect(ollama_spec(digest=None))
    assert status.state is ModelState.AVAILABLE


def test_apply_pulls_once_then_skips(tmp_path):
    store = FakeOllama()
    installer = installer_for(tmp_path, ollama=store)
    spec = ollama_spec()

    assert installer.apply(spec).outcome is ModelOutcome.ACQUIRED
    assert store.pulls == ["demo:8b"]
    assert installer.apply(spec).outcome is ModelOutcome.SKIPPED
    assert store.pulls == ["demo:8b"]


def test_dry_run_pulls_nothing(tmp_path):
    store = FakeOllama()
    result = installer_for(tmp_path, ollama=store).apply(ollama_spec(), dry_run=True)
    assert result.outcome is ModelOutcome.DRY_RUN
    assert store.pulls == []


def test_a_failed_pull_is_reported_structurally(tmp_path):
    store = FakeOllama(fail="pull")
    result = installer_for(tmp_path, ollama=store).apply(ollama_spec())
    assert result.outcome is ModelOutcome.FAILED
    assert result.failure is ModelFailure.PROVIDER


def test_acquisition_never_removes_a_model_someone_else_pulled(tmp_path):
    theirs = OllamaModelEntry(name="someone-elses:13b", digest="sha256:aaaa")
    store = FakeOllama(installed=(theirs,))
    installer_for(tmp_path, ollama=store).apply(ollama_spec())
    assert theirs in store.installed


def test_without_a_client_ollama_models_fail_closed(tmp_path):
    installer = installer_for(tmp_path)
    assert installer.detect(ollama_spec()).state is ModelState.NOT_INSTALLED
    result = installer.apply(ollama_spec())
    assert result.outcome is ModelOutcome.FAILED
    assert result.failure is ModelFailure.PROVIDER


# --- resolver --------------------------------------------------------------


def catalog_with(*specs: ModelSpec) -> ModelCatalog:
    return ModelCatalog(specs={spec.id: spec for spec in specs})


def test_the_resolver_satisfies_the_ic6_protocol(tmp_path):
    resolver = StackModelResolver(installer=installer_for(tmp_path), catalog=ModelCatalog())
    assert isinstance(resolver, ModelResolver)


def test_an_available_file_model_resolves_to_an_absolute_path(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    installer.apply(spec)
    resolver = StackModelResolver(installer=installer, catalog=catalog_with(spec))

    resolved = resolver.resolve(FILE_ID)
    assert resolved.state is ModelState.AVAILABLE
    location = resolved.require_location()
    assert isinstance(location, FileModelLocation)
    assert location.path.is_absolute()
    assert location.path.is_file()
    assert location.runtime_name == "demo.safetensors"
    assert location.placement is ModelPlacement.DIFFUSION_MODELS


def test_an_available_ollama_model_resolves_to_a_reference_with_no_path(tmp_path):
    store = FakeOllama(
        installed=(OllamaModelEntry(name="demo:8b", digest="sha256:4f6b83f30b62aa"),)
    )
    spec = ollama_spec()
    resolver = StackModelResolver(
        installer=installer_for(tmp_path, ollama=store), catalog=catalog_with(spec)
    )

    location = resolver.resolve(OLLAMA_ID).require_location()
    assert isinstance(location, OllamaModelLocation)
    assert location.reference == "demo:8b"
    assert not hasattr(location, "path")


@pytest.mark.parametrize("state", ["missing", "corrupt"])
def test_an_unusable_model_never_exposes_a_location(tmp_path, state):
    installer = installer_for(tmp_path)
    spec = file_spec()
    if state == "corrupt":
        destination = installer.paths.destination_for(spec)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"nope")
    resolver = StackModelResolver(installer=installer, catalog=catalog_with(spec))

    resolved = resolver.resolve(FILE_ID)
    assert not resolved.available
    assert resolved.location is None
    with pytest.raises(ModelUnavailableError):
        resolved.require_location()


def test_an_unknown_model_resolves_fail_closed(tmp_path):
    resolver = StackModelResolver(installer=installer_for(tmp_path), catalog=ModelCatalog())
    resolved = resolver.resolve(ModelId("ollama/nobody_knows"))
    assert resolved.state is ModelState.NOT_INSTALLED
    assert resolved.location is None
    assert "not in the model catalog" in resolved.detail


def test_a_declared_but_unpinned_model_says_why(tmp_path):
    catalog = ModelCatalog(unpinned={FILE_ID: "no sha256 is pinned"})
    resolver = StackModelResolver(installer=installer_for(tmp_path), catalog=catalog)
    resolved = resolver.resolve(FILE_ID)
    assert resolved.state is ModelState.NOT_INSTALLED
    assert "not installable" in resolved.detail
    assert "sha256" in resolved.detail


def test_ic6_needs_only_the_resolver(tmp_path):
    """A consumer built against `resolve` alone gets everything it needs."""
    installer = installer_for(tmp_path)
    spec = file_spec()
    installer.apply(spec)
    resolver: ModelResolver = StackModelResolver(installer=installer, catalog=catalog_with(spec))

    def ic6_consumer(models: ModelResolver, model_id: ModelId) -> str:
        resolved = models.resolve(model_id)
        location = resolved.require_location()
        assert isinstance(location, FileModelLocation)
        return location.runtime_name

    assert ic6_consumer(resolver, FILE_ID) == "demo.safetensors"


def test_ic5_configures_no_service_and_starts_no_process():
    """IC5 stops at resolution. Checked by imports, not by prose."""
    import ast

    import local_ai_stack.model_catalog as catalog_module
    import local_ai_stack.model_installer as installer_module
    import local_ai_stack.model_resolver as resolver_module

    forbidden = {
        "controller",
        "launcher",
        "registry",
        "runtime",
        "services",
        "processes",
        "adapters",
        "subprocess",
    }
    for module in (resolver_module, installer_module, catalog_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.lstrip("."))
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not (imported & forbidden), f"{Path(module.__file__).name}: {imported & forbidden}"


# --- profiles --------------------------------------------------------------


def test_profile_roles_drive_resolution(tmp_path):
    profile = next(p for p in load_profiles(REPO / "profiles") if p.id == "pepe")
    resolver = StackModelResolver(installer=installer_for(tmp_path), catalog=ModelCatalog())
    resolved = resolver.resolve_profile(profile)

    assert set(resolved) == {r.role for r in profile.model_requirements}
    assert "chat.primary" in resolved
    assert all(not value.available for value in resolved.values())


def test_a_tbd_role_produces_no_requirement():
    """An explicitly unresolved fixture role is absent, never a fake entry.

    A TBD role is only coherent under a capability that is off, so chat is
    switched off with it; enabled over TBD is refused when the profile loads.
    """
    profile = next(p for p in load_profiles(REPO / "profiles") if p.id == "capella")
    data = deepcopy(profile.raw)
    data["models"]["chat"]["primary"] = "TBD"
    data["features"].update(chat=False, vision=False, tools=False)
    profile = validate_profile_data(data)
    roles = {requirement.role for requirement in profile.model_requirements}
    assert "chat.primary" not in roles
    assert "image.diffusion" in roles


def test_the_resolver_hardcodes_no_profile_name():
    import local_ai_stack.model_resolver as module

    source = Path(module.__file__).read_text(encoding="utf-8").lower()
    for name in ("pepe", "capella"):
        assert name not in source


def test_profile_specs_skip_what_cannot_be_installed():
    profile = next(p for p in load_profiles(REPO / "profiles") if p.id == "pepe")
    catalog = validate_model_manifest(
        {
            "schema_version": 1,
            "ollama": {"hermes3_8b": {"kind": "chat", "reference": "hermes3:8b"}},
            "comfyui": {
                "flux2_klein_fp8": {
                    "kind": "diffusion",
                    "placement": "diffusion_models",
                    "filename": "f.safetensors",
                    "url": "TBD",
                    "sha256": "TBD",
                }
            },
        }
    )
    specs = profile_model_specs(profile, catalog)
    assert [str(spec.id) for spec in specs] == ["ollama/hermes3_8b"]

    reasons = unresolved_roles(profile, catalog)
    assert "image.diffusion" in reasons
    assert "chat.primary" not in reasons


def test_missing_for_profile_reports_only_unavailable_roles(tmp_path):
    profile = validate_profile_data(
        {
            **next(p for p in load_profiles(REPO / "profiles") if p.id == "pepe").raw,
        },
        "pepe.yaml",
    )
    resolver = StackModelResolver(installer=installer_for(tmp_path), catalog=ModelCatalog())
    missing = resolver.missing_for_profile(profile)
    assert missing
    assert all(not value.available for value in missing.values())


# --- fronteira -------------------------------------------------------------


def test_resolved_models_are_the_only_thing_ic6_receives(tmp_path):
    installer = installer_for(tmp_path)
    spec = file_spec()
    installer.apply(spec)
    resolver = StackModelResolver(installer=installer, catalog=catalog_with(spec))
    resolved = resolver.resolve(FILE_ID)
    assert isinstance(resolved, ResolvedModel)
    assert not hasattr(resolved, "artifact")
    assert not hasattr(resolved, "url")
    assert not hasattr(resolved, "sha256")
