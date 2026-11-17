"""Real download failures reach the terminal operation, including a fresh Hub."""

from __future__ import annotations

import errno
import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from first_run_support import CAPELLA, controlled_stack

from local_ai_stack.download import Downloader
from local_ai_stack.hub_application import HubApplicationAdapter
from local_ai_stack.installation_control import InstallationCancelled, InstallationControl
from local_ai_stack.models import FileModelSource
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.tui.application import InstallSelection


@pytest.fixture
def download_stack():
    built = controlled_stack()
    yield built
    built.release()


def bind_source(built, handler, *, host="civitai.com"):
    """Replace only the model HTTP seam, retaining real planning and acquisition."""
    marker = uuid4().hex
    catalog = built.app.deps.catalog
    for model_id, spec in tuple(catalog.specs.items()):
        if isinstance(spec.source, FileModelSource):
            artifact = replace(
                spec.source.artifact,
                url=f"https://{host}/download/{spec.source.artifact.filename}?token={marker}",
            )
            catalog.specs[model_id] = replace(spec, source=replace(spec.source, artifact=artifact))
    downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=1,
        chunk_size=4,
    )
    built.app.deps.downloader = lambda: downloader
    return marker


def first_failure(built, *, forbidden=()):
    events = []
    result = built.app.first_run("download-failure", CAPELLA, emit=events.append)
    assert result.state is OperationState.FAILED
    assert events[-1].result == result
    assert sum(event.result is not None for event in events) == 1
    assert result.diagnostics
    terminal = result.diagnostics[0]
    assert events[-1].message == terminal.message
    assert len(terminal.message) <= 240
    rendered = json.dumps(asdict(events[-1]), default=str)
    for value in (*forbidden, "https://", str(built.app.layout.root)):
        assert value not in rendered
    return terminal


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize(
    ("host", "provider"),
    [("civitai.com", "Civitai"), ("huggingface.co", "Hugging Face")],
)
def test_source_access_refusal_has_provider_permissions_and_license_guidance(
    download_stack, status, host, provider
):
    calls = []

    def denied(request):
        calls.append(request)
        # Even a sensitive response body never becomes an operation diagnostic.
        return httpx.Response(status, text=str(request.url))

    marker = bind_source(download_stack, denied, host=host)
    item = first_failure(download_stack, forbidden=(marker,))
    assert item.code == f"model-download-http-{status}"
    assert f"HTTP {status}" in item.message
    assert provider in item.message
    assert "Configurações" in item.message
    assert "acesso" in item.message and "licença" in item.message
    assert "tente novamente" in item.message
    assert len(calls) == 1
    assert not download_stack.app.status(CAPELLA).installation_ready


@pytest.mark.parametrize("host", ["private.invalid", "civitai.com.evil.invalid"])
def test_unknown_or_lookalike_provider_does_not_promise_a_civitai_token_fix(download_stack, host):
    marker = bind_source(download_stack, lambda _: httpx.Response(403), host=host)
    item = first_failure(download_stack, forbidden=(marker,))
    assert item.code == "model-download-http-403"
    assert "Civitai" not in item.message and "Hugging Face" not in item.message
    assert "permissão" in item.message and "licença" in item.message


@pytest.mark.parametrize("status", [429, 404, 503])
def test_source_status_is_preserved_without_response_or_url_echo(download_stack, status):
    marker = bind_source(download_stack, lambda _: httpx.Response(status))
    item = first_failure(download_stack, forbidden=(marker,))
    assert item.code == f"model-download-http-{status}"
    assert f"HTTP {status}" in item.message
    if status == 429:
        assert "Aguarde" in item.message and "manualmente" in item.message
    else:
        assert "disponibilidade" in item.message
    assert "tente novamente" in item.message


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_interruption_preserves_actual_partial_bytes_and_actionable_cause(
    download_stack, error_type
):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345678"
            raise error_type("backend-only detail " + str(download_stack.app.layout.root))

    marker = bind_source(
        download_stack,
        lambda _: httpx.Response(200, headers={"content-length": "100"}, stream=BrokenBody()),
    )
    item = first_failure(download_stack, forbidden=(marker, "backend-only detail"))
    expected = "timeout" if error_type is httpx.ReadTimeout else "transport"
    assert item.code == f"model-download-{expected}"
    assert "tente novamente" in item.message
    assert "bytes parciais foram mantidos" in item.message
    paths = list(download_stack.app.layout.root.rglob("*.part"))
    assert len(paths) == 1 and paths[0].read_bytes() == b"12345678"


def test_checksum_failure_discards_bad_bytes_and_requests_manual_retry(download_stack):
    marker = bind_source(
        download_stack, lambda _: httpx.Response(200, content=b"corrupted weights")
    )
    item = first_failure(download_stack, forbidden=(marker,))
    assert item.code == "model-download-checksum"
    assert "descartado" in item.message and "novamente" in item.message
    assert not list(download_stack.app.layout.root.rglob("*.part"))


@pytest.mark.parametrize(
    ("error_number", "code", "action"),
    [
        (errno.ENOSPC, "disk-full", "Libere espaço"),
        (errno.EACCES, "access-denied", "permissão de gravação"),
        (errno.EIO, "filesystem", "Confira o disco"),
    ],
)
@pytest.mark.parametrize("stage", ["write", "promote"])
def test_real_file_operation_failure_survives_wrapping_without_path_leak(
    download_stack, monkeypatch, error_number, code, action, stage
):
    marker = bind_source(download_stack, lambda _: httpx.Response(200, content=b"weights"))
    model_parts = {
        spec.source.artifact.filename + ".part"
        for spec in download_stack.app.catalog().specs.values()
        if isinstance(spec.source, FileModelSource)
    }
    if stage == "promote":
        # Reach finalization with a valid checksum, rather than the mismatch branch.
        import hashlib

        catalog = download_stack.app.deps.catalog
        for model_id, spec in tuple(catalog.specs.items()):
            if isinstance(spec.source, FileModelSource):
                artifact = replace(
                    spec.source.artifact, sha256=hashlib.sha256(b"weights").hexdigest()
                )
                catalog.specs[model_id] = replace(
                    spec, source=replace(spec.source, artifact=artifact)
                )
        from local_ai_stack import download

        original = download.os.replace

        def fail_replace(source, destination):
            if Path(source).name in model_parts:
                raise OSError(error_number, "backend-only detail", str(destination))
            return original(source, destination)

        monkeypatch.setattr(download.os, "replace", fail_replace)
    else:
        original = Path.open

        def fail_open(path, *args, **kwargs):
            if path.name in model_parts:
                raise OSError(error_number, "backend-only detail", str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_open)
    item = first_failure(download_stack, forbidden=(marker, "backend-only detail"))
    assert item.code == f"model-download-{code}"
    assert action in item.message and "tente novamente" in item.message


def test_unclassified_failure_does_not_invent_auth_disk_or_network_diagnosis(download_stack):
    def fail(request):
        raise ValueError("HTTP 403 permission denied disk full " + str(request.url))

    marker = bind_source(download_stack, fail)
    item = first_failure(download_stack, forbidden=(marker, "HTTP 403", "disk full"))
    assert item.code == "model-download-failed"
    assert "Tente novamente" in item.message


def test_failure_reaches_fresh_hub_card_without_an_active_installation(download_stack, tmp_path):
    marker = bind_source(download_stack, lambda _: httpx.Response(403))
    adapter = HubApplicationAdapter(
        profiles=tuple(download_stack.app.profiles.values()),
        resolve=lambda _location: download_stack.app.installation,
        backend_factory=lambda _installation: download_stack.app,
        selection_path=tmp_path / "selection.yaml",
    )
    try:
        assert adapter.active_root() is None
        adapter.install(InstallSelection(CAPELLA))
        adapter.wait(20)
        event = adapter.current_operation().last_event
        assert event.state is OperationState.FAILED
        assert event.result.diagnostics[0].code == "model-download-http-403"
        assert "Civitai" in event.message and "Configurações" in event.message
        assert marker not in json.dumps(asdict(event), default=str)
        assert adapter.active_root() is None
    finally:
        adapter.close()


def test_download_cancellation_still_propagates_out_of_model_installer(download_stack):
    control = InstallationControl("cancel-model")

    class CancelledBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"1234"
            control.request("cancel")
            yield b"5678"

    bind_source(
        download_stack,
        lambda _: httpx.Response(200, headers={"content-length": "100"}, stream=CancelledBody()),
    )
    installer = download_stack.app.model_installer(download_stack.app.profile(CAPELLA))
    spec = next(
        spec
        for spec in download_stack.app.catalog().specs.values()
        if isinstance(spec.source, FileModelSource)
    )
    with control.scope(), pytest.raises(InstallationCancelled):
        installer.apply(spec)
    part = installer.downloader.part_path(installer.paths.destination_for(spec))
    assert part.read_bytes() == b"1234"


def test_action_payload_keeps_typed_status_and_source_provider(download_stack):
    bind_source(download_stack, lambda _: httpx.Response(403), host="huggingface.co")
    installer = download_stack.app.model_installer(download_stack.app.profile(CAPELLA))
    spec = next(
        spec
        for spec in download_stack.app.catalog().specs.values()
        if isinstance(spec.source, FileModelSource)
    )
    result = installer.apply(spec)
    assert not result.ok
    assert result.failure_info is not None
    assert result.failure_info.http_status == 403
    assert result.failure_info.provider == "huggingface"
    assert result.failure_info.error_type == "HTTPStatusError"
    assert not result.failure_info.partials_retained


def test_connect_timeout_does_not_claim_bytes_were_retained(download_stack):
    def fail(request):
        raise httpx.ConnectTimeout(str(request.url))

    marker = bind_source(download_stack, fail)
    item = first_failure(download_stack, forbidden=(marker,))
    assert item.code == "model-download-timeout"
    assert "bytes parciais foram mantidos" not in item.message
    assert "bytes parciais disponíveis" in item.message
    assert not list(download_stack.app.layout.root.rglob("*.part"))
