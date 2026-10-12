"""HTTP download engine.

Generic on purpose: it fetches a URL to a path and proves the bytes. It knows
nothing about Ollama, ComfyUI or profiles.

The safety rules it enforces:

- bytes land in a `.part` file first and are promoted with an atomic replace,
  so the destination is never a half-written file;
- a checksum, when the manifest supplies one, is mandatory: mismatched bytes
  are deleted and never promoted;
- an existing destination that already matches its checksum is a SKIP, which
  makes repeated runs idempotent and free;
- retries are bounded and only cover transient failures, never a 4xx;
- dry-run opens no socket and writes nothing at all;
- every log line and error message goes through the runner's sanitizer, so a
  signed URL or a token never reaches a log.

The `.part` file sits next to the destination whenever possible, because an
atomic replace only works within one volume. A layout may be supplied to keep
partials in its downloads directory, and it is used only when it lands on the
same drive as the destination.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from .artifacts import Artifact
from .layout import StackLayout
from .runner import sanitize

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CHUNK_SIZE = 1024 * 256
PART_SUFFIX = ".part"


class DownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    DRY_RUN = "dry-run"


class DownloadError(RuntimeError):
    """A download could not be completed."""


class ChecksumMismatchError(DownloadError):
    """The fetched bytes did not match the expected checksum."""


class DownloadCancelledError(DownloadError):
    """The caller cancelled the transfer; the partial file was removed."""


@dataclass(frozen=True)
class DownloadProgress:
    url: str
    destination: Path
    downloaded_bytes: int
    total_bytes: int | None

    @property
    def fraction(self) -> float | None:
        if not self.total_bytes:
            return None
        return min(1.0, self.downloaded_bytes / self.total_bytes)


ProgressCallback = Callable[[DownloadProgress], None]
CancelCheck = Callable[[], bool]
RequestHeaders = Callable[[str], Mapping[str, str]]


@dataclass(frozen=True)
class DownloadSpec:
    """What to fetch, where to put it, and how to prove it."""

    url: str
    destination: Path
    sha256: str | None = None
    size_bytes: int | None = None

    @classmethod
    def from_artifact(cls, artifact: Artifact, directory: Path) -> DownloadSpec:
        return cls(
            url=artifact.url,
            destination=artifact.destination_in(directory),
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
        )

    @property
    def safe_url(self) -> str:
        """The URL with credentials and tokens redacted, safe to log."""
        return sanitize(self.url)


@dataclass(frozen=True)
class DownloadResult:
    spec: DownloadSpec
    status: DownloadStatus
    path: Path
    downloaded_bytes: int = 0
    sha256: str | None = None
    attempts: int = 0
    verified: bool = False
    would_skip: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (DownloadStatus.DOWNLOADED, DownloadStatus.SKIPPED)

    def __str__(self) -> str:
        return f"<DownloadResult {self.spec.safe_url} -> {self.status}>"


def sha256_of(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


class Downloader:
    """Fetches artifacts over HTTP with atomic, checksum-verified finalization."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        request_headers: RequestHeaders | None = None,
        layout: StackLayout | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        dry_run: bool = False,
        log: logging.Logger | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts deve ser >= 1")
        if timeout <= 0:
            raise ValueError("timeout deve ser > 0")
        self.client_factory = client_factory
        self.request_headers = request_headers
        self.layout = layout
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.chunk_size = chunk_size
        self.dry_run = dry_run
        self.log = log or logger

    # --- paths -------------------------------------------------------------

    def part_path(self, destination: Path) -> Path:
        """Where partial bytes accumulate. Same volume as the destination."""
        alongside = destination.with_name(destination.name + PART_SUFFIX)
        if self.layout is None:
            return alongside
        staged = self.layout.download_part(destination.name)
        return staged if staged.drive.lower() == destination.drive.lower() else alongside

    # --- consultas read-only ----------------------------------------------

    def is_satisfied(self, spec: DownloadSpec) -> bool:
        """True when the destination already holds the expected bytes.

        Without a checksum, an existing file is accepted as satisfied: there is
        nothing to verify it against. The result records that as unverified.
        """
        if not spec.destination.is_file():
            return False
        if spec.sha256 is None:
            return True
        return sha256_of(spec.destination) == spec.sha256

    # --- transferencia -----------------------------------------------------

    def fetch(
        self,
        spec: DownloadSpec,
        *,
        progress: ProgressCallback | None = None,
        cancel: CancelCheck | None = None,
        dry_run: bool | None = None,
    ) -> DownloadResult:
        """Fetch one artifact. Returns a result; raises only on hard failure."""
        is_dry = self.dry_run if dry_run is None else dry_run
        satisfied = self.is_satisfied(spec)

        if is_dry:
            self.log.info("[dry-run] download %s -> %s", spec.safe_url, spec.destination)
            return DownloadResult(
                spec=spec,
                status=DownloadStatus.DRY_RUN,
                path=spec.destination,
                would_skip=satisfied,
            )

        if satisfied:
            self.log.debug("skip, destination already valid: %s", spec.destination)
            return DownloadResult(
                spec=spec,
                status=DownloadStatus.SKIPPED,
                path=spec.destination,
                sha256=spec.sha256,
                verified=spec.sha256 is not None,
            )

        part = self.part_path(spec.destination)
        spec.destination.parent.mkdir(parents=True, exist_ok=True)
        part.parent.mkdir(parents=True, exist_ok=True)

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                digest, written = self._transfer(spec, part, progress, cancel)
            except DownloadCancelledError:
                _remove(part)
                raise
            except Exception as exc:  # noqa: BLE001 - reclassificado logo abaixo
                _remove(part)
                last_error = exc
                if not _is_retryable(exc) or attempt == self.max_attempts:
                    break
                self.log.warning(
                    "download attempt %s/%s failed for %s: %s",
                    attempt,
                    self.max_attempts,
                    spec.safe_url,
                    sanitize(str(exc)),
                )
                continue

            if spec.sha256 is not None and digest != spec.sha256:
                _remove(part)
                raise ChecksumMismatchError(
                    f"checksum mismatch for {spec.safe_url}: "
                    f"expected {spec.sha256}, got {digest}. The file was discarded."
                )

            os.replace(part, spec.destination)
            self.log.info("downloaded %s (%s bytes)", spec.safe_url, written)
            return DownloadResult(
                spec=spec,
                status=DownloadStatus.DOWNLOADED,
                path=spec.destination,
                downloaded_bytes=written,
                sha256=digest,
                attempts=attempt,
                verified=spec.sha256 is not None,
            )

        raise DownloadError(
            f"could not download {spec.safe_url} after {self.max_attempts} attempt(s): "
            f"{sanitize(str(last_error))}"
        ) from last_error

    # --- internals ---------------------------------------------------------

    @contextmanager
    def _client(self) -> Iterator[httpx.Client]:
        if self.client_factory is not None:
            client = self.client_factory()
        else:
            client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        try:
            yield client
        finally:
            client.close()

    def _transfer(
        self,
        spec: DownloadSpec,
        part: Path,
        progress: ProgressCallback | None,
        cancel: CancelCheck | None,
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        written = 0
        headers = dict(self.request_headers(spec.url)) if self.request_headers is not None else {}
        with (
            self._client() as client,
            client.stream("GET", spec.url, headers=headers, timeout=self.timeout) as response,
        ):
            response.raise_for_status()
            total = _content_length(response) or spec.size_bytes
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(self.chunk_size):
                    if cancel is not None and cancel():
                        raise DownloadCancelledError(f"download cancelled: {spec.safe_url}")
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(
                            DownloadProgress(
                                url=spec.safe_url,
                                destination=spec.destination,
                                downloaded_bytes=written,
                                total_bytes=total,
                            )
                        )
        return digest.hexdigest(), written


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort cleanup
        logger.debug("could not remove partial file %s", path)
