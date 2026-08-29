"""Tests for the download engine.

Every request is served by an httpx MockTransport, so the real httpx code path
runs but no socket is ever opened. Nothing is written outside tmp_path.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx
import pytest

from local_ai_stack.artifacts import Artifact
from local_ai_stack.download import (
    PART_SUFFIX,
    ChecksumMismatchError,
    DownloadCancelledError,
    Downloader,
    DownloadError,
    DownloadSpec,
    DownloadStatus,
    sha256_of,
)
from local_ai_stack.layout import StackLayout

PAYLOAD = b"ollama-archive-bytes" * 64
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()
URL = "https://example.invalid/ollama.zip"


def transport_returning(*responses: httpx.Response, record: list[str] | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(str(request.url))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handler)


def transport_raising(*errors: Exception, then: httpx.Response | None = None):
    queue: list[object] = [*errors]
    if then is not None:
        queue.append(then)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if queue else then
        if isinstance(item, Exception):
            raise item
        assert item is not None
        return item

    return httpx.MockTransport(handler)


def downloader_with(transport: httpx.MockTransport, **kwargs) -> Downloader:
    return Downloader(client_factory=lambda: httpx.Client(transport=transport), **kwargs)


def ok_response(body: bytes = PAYLOAD) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-length": str(len(body))})


def spec_for(tmp_path: Path, sha: str | None = PAYLOAD_SHA) -> DownloadSpec:
    return DownloadSpec(url=URL, destination=tmp_path / "out" / "ollama.zip", sha256=sha)


# --- sucesso ---------------------------------------------------------------


def test_successful_download_writes_the_destination(tmp_path):
    spec = spec_for(tmp_path)
    result = downloader_with(transport_returning(ok_response())).fetch(spec)
    assert result.status is DownloadStatus.DOWNLOADED
    assert result.ok
    assert spec.destination.read_bytes() == PAYLOAD
    assert result.downloaded_bytes == len(PAYLOAD)
    assert result.sha256 == PAYLOAD_SHA
    assert result.verified is True
    assert result.attempts == 1


def test_download_reports_progress(tmp_path):
    seen = []
    downloader = downloader_with(transport_returning(ok_response()), chunk_size=128)
    downloader.fetch(spec_for(tmp_path), progress=seen.append)
    assert seen
    assert seen[-1].downloaded_bytes == len(PAYLOAD)
    assert seen[-1].total_bytes == len(PAYLOAD)
    assert seen[-1].fraction == 1.0
    assert [p.downloaded_bytes for p in seen] == sorted(p.downloaded_bytes for p in seen)


def test_spec_can_be_built_from_an_artifact(tmp_path):
    art = Artifact(id="a", url=URL, filename="ollama.zip", sha256=PAYLOAD_SHA, size_bytes=7)
    spec = DownloadSpec.from_artifact(art, tmp_path)
    assert spec.destination == tmp_path / "ollama.zip"
    assert spec.sha256 == PAYLOAD_SHA
    assert spec.size_bytes == 7


# --- checksum --------------------------------------------------------------


def test_correct_checksum_is_accepted(tmp_path):
    result = downloader_with(transport_returning(ok_response())).fetch(spec_for(tmp_path))
    assert sha256_of(result.path) == PAYLOAD_SHA


def test_wrong_checksum_never_produces_a_destination(tmp_path):
    spec = spec_for(tmp_path, sha="b" * 64)
    with pytest.raises(ChecksumMismatchError):
        downloader_with(transport_returning(ok_response())).fetch(spec)
    assert not spec.destination.exists()
    assert not list(spec.destination.parent.glob(f"*{PART_SUFFIX}"))


def test_wrong_checksum_does_not_clobber_a_previous_good_file(tmp_path):
    spec = spec_for(tmp_path, sha="c" * 64)
    spec.destination.parent.mkdir(parents=True)
    spec.destination.write_bytes(b"previous good bytes")
    with pytest.raises(ChecksumMismatchError):
        downloader_with(transport_returning(ok_response())).fetch(spec)
    assert spec.destination.read_bytes() == b"previous good bytes"


def test_download_without_a_checksum_is_unverified(tmp_path):
    result = downloader_with(transport_returning(ok_response())).fetch(spec_for(tmp_path, sha=None))
    assert result.status is DownloadStatus.DOWNLOADED
    assert result.verified is False


# --- idempotencia ----------------------------------------------------------


def test_existing_valid_file_is_skipped_without_a_request(tmp_path):
    spec = spec_for(tmp_path)
    spec.destination.parent.mkdir(parents=True)
    spec.destination.write_bytes(PAYLOAD)
    calls: list[str] = []
    result = downloader_with(transport_returning(ok_response(), record=calls)).fetch(spec)
    assert result.status is DownloadStatus.SKIPPED
    assert result.verified is True
    assert calls == []


def test_second_fetch_is_a_skip(tmp_path):
    spec = spec_for(tmp_path)
    downloader = downloader_with(transport_returning(ok_response()))
    assert downloader.fetch(spec).status is DownloadStatus.DOWNLOADED
    assert downloader.fetch(spec).status is DownloadStatus.SKIPPED


def test_existing_file_with_wrong_bytes_is_replaced(tmp_path):
    spec = spec_for(tmp_path)
    spec.destination.parent.mkdir(parents=True)
    spec.destination.write_bytes(b"stale")
    result = downloader_with(transport_returning(ok_response())).fetch(spec)
    assert result.status is DownloadStatus.DOWNLOADED
    assert spec.destination.read_bytes() == PAYLOAD


def test_existing_file_without_checksum_is_skipped_unverified(tmp_path):
    spec = spec_for(tmp_path, sha=None)
    spec.destination.parent.mkdir(parents=True)
    spec.destination.write_bytes(b"whatever")
    result = downloader_with(transport_returning(ok_response())).fetch(spec)
    assert result.status is DownloadStatus.SKIPPED
    assert result.verified is False


# --- retry e falhas --------------------------------------------------------


def test_transient_failure_is_retried_then_succeeds(tmp_path):
    transport = transport_raising(
        httpx.ConnectError("boom"), httpx.ReadError("boom"), then=ok_response()
    )
    result = downloader_with(transport, max_attempts=3).fetch(spec_for(tmp_path))
    assert result.status is DownloadStatus.DOWNLOADED
    assert result.attempts == 3


def test_retries_are_bounded(tmp_path):
    transport = transport_raising(*[httpx.ConnectError("boom")] * 5)
    with pytest.raises(DownloadError) as exc:
        downloader_with(transport, max_attempts=2).fetch(spec_for(tmp_path))
    assert "2 attempt" in str(exc.value)


def test_timeout_is_reported_as_a_download_error(tmp_path):
    transport = transport_raising(*[httpx.ReadTimeout("too slow")] * 3)
    with pytest.raises(DownloadError):
        downloader_with(transport, max_attempts=1).fetch(spec_for(tmp_path))


def test_client_error_is_not_retried(tmp_path):
    calls: list[str] = []
    transport = transport_returning(httpx.Response(404, content=b""), record=calls)
    with pytest.raises(DownloadError):
        downloader_with(transport, max_attempts=3).fetch(spec_for(tmp_path))
    assert len(calls) == 1


def test_server_error_is_retried(tmp_path):
    calls: list[str] = []
    transport = transport_returning(httpx.Response(503, content=b""), record=calls)
    with pytest.raises(DownloadError):
        downloader_with(transport, max_attempts=3).fetch(spec_for(tmp_path))
    assert len(calls) == 3


def test_failure_leaves_no_partial_file(tmp_path):
    spec = spec_for(tmp_path)
    spec.destination.parent.mkdir(parents=True)
    transport = transport_raising(*[httpx.ConnectError("boom")] * 3)
    with pytest.raises(DownloadError):
        downloader_with(transport, max_attempts=1).fetch(spec)
    assert not spec.destination.exists()
    assert list(spec.destination.parent.iterdir()) == []


def test_invalid_downloader_configuration_is_rejected():
    with pytest.raises(ValueError):
        Downloader(max_attempts=0)
    with pytest.raises(ValueError):
        Downloader(timeout=0)


# --- arquivo parcial e finalizacao atomica ---------------------------------


def test_part_file_sits_next_to_the_destination_by_default(tmp_path):
    destination = tmp_path / "out" / "ollama.zip"
    assert Downloader().part_path(destination) == destination.with_name("ollama.zip.part")


def test_part_file_uses_the_layout_downloads_directory_when_available(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    downloader = Downloader(layout=layout)
    destination = layout.models / "ollama.zip"
    assert downloader.part_path(destination) == layout.downloads / "ollama.zip.part"


def test_part_file_falls_back_when_the_layout_is_on_another_drive(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    downloader = Downloader(layout=layout)
    other_drive = Path("Z:/elsewhere/ollama.zip")
    if not other_drive.drive or other_drive.drive.lower() == layout.root.drive.lower():
        pytest.skip("single-drive platform")
    assert downloader.part_path(other_drive) == other_drive.with_name("ollama.zip.part")


def test_destination_never_exists_while_bytes_are_still_arriving(tmp_path):
    spec = spec_for(tmp_path)
    observed: list[bool] = []

    def watch(progress):
        observed.append(spec.destination.exists())

    downloader_with(transport_returning(ok_response()), chunk_size=64).fetch(spec, progress=watch)
    assert observed and not any(observed)
    assert spec.destination.is_file()


def test_partial_file_is_removed_after_a_mid_transfer_cancel(tmp_path):
    spec = spec_for(tmp_path)
    downloader = downloader_with(transport_returning(ok_response()), chunk_size=64)
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(DownloadCancelledError):
        downloader.fetch(spec, cancel=cancel)
    assert not spec.destination.exists()
    assert not list(spec.destination.parent.glob(f"*{PART_SUFFIX}"))


# --- dry-run ---------------------------------------------------------------


def test_dry_run_opens_no_socket_and_writes_nothing(tmp_path):
    spec = spec_for(tmp_path)
    calls: list[str] = []
    result = downloader_with(transport_returning(ok_response(), record=calls)).fetch(
        spec, dry_run=True
    )
    assert result.status is DownloadStatus.DRY_RUN
    assert calls == []
    assert not spec.destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_reports_whether_it_would_skip(tmp_path):
    spec = spec_for(tmp_path)
    downloader = downloader_with(transport_returning(ok_response()))
    assert downloader.fetch(spec, dry_run=True).would_skip is False
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    spec.destination.write_bytes(PAYLOAD)
    assert downloader.fetch(spec, dry_run=True).would_skip is True


def test_downloader_level_dry_run_applies_to_every_fetch(tmp_path):
    downloader = Downloader(dry_run=True)
    assert downloader.fetch(spec_for(tmp_path)).status is DownloadStatus.DRY_RUN


# --- sanitizacao -----------------------------------------------------------


def test_sensitive_url_is_redacted_in_results_and_logs(tmp_path, caplog):
    secret_url = "https://user:s3cr3t@example.invalid/ollama.zip?token=abcdef123456"
    spec = DownloadSpec(url=secret_url, destination=tmp_path / "o.zip", sha256=PAYLOAD_SHA)
    caplog.set_level(logging.DEBUG, logger="local_ai_stack.download")
    downloader_with(transport_returning(ok_response())).fetch(spec)
    assert "s3cr3t" not in spec.safe_url
    assert "abcdef123456" not in spec.safe_url
    assert "s3cr3t" not in caplog.text
    assert "abcdef123456" not in caplog.text
    assert spec.url == secret_url  # o valor real continua utilizavel


def test_sensitive_url_is_redacted_in_errors(tmp_path):
    secret_url = "https://example.invalid/o.zip?access_token=abcdef123456"
    spec = DownloadSpec(url=secret_url, destination=tmp_path / "o.zip")
    transport = transport_raising(httpx.ConnectError("boom"))
    with pytest.raises(DownloadError) as exc:
        downloader_with(transport, max_attempts=1).fetch(spec)
    assert "abcdef123456" not in str(exc.value)


def test_checksum_error_does_not_leak_the_url(tmp_path):
    secret_url = "https://example.invalid/o.zip?token=abcdef123456"
    spec = DownloadSpec(url=secret_url, destination=tmp_path / "o.zip", sha256="d" * 64)
    with pytest.raises(ChecksumMismatchError) as exc:
        downloader_with(transport_returning(ok_response())).fetch(spec)
    assert "abcdef123456" not in str(exc.value)
