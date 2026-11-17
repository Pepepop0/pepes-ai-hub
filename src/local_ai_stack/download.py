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

What is already on disk is never fetched twice. A failed attempt, a dropped
connection or a Hub that was closed mid-download leaves its `.part` file and a
small `.part.json` beside it saying which bytes are good; the next attempt, in
this run or a later one, asks the server only for the rest (HTTP ``Range``). A
cancel or a checksum mismatch discards both. A file whose size the pin records
starts as one stream; if that stream stays slow and the source answers ranges,
the rest is fetched as several byte ranges at once, over separate connections: a
source that throttles each connection (a CDN under load) then delivers that many
times faster, and a link that is already full is left alone.

The `.part` file sits next to the destination whenever possible, because an
atomic replace only works within one volume. A layout may be supplied to keep
partials in its downloads directory, and it is used only when it lands on the
same drive as the destination.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from .artifacts import Artifact
from .installation_control import (
    InstallationCancelled,
    current_installation_control,
    installation_checkpoint,
)
from .layout import StackLayout
from .runner import sanitize

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CHUNK_SIZE = 1024 * 256
# A large download starts as one stream. When that stream stays slower than this for a
# whole window, and the source answers byte ranges, the rest of the file is split into
# up to this many ranges, fetched at once over separate connections, each at least this
# long. On a link that is already full this never triggers, so it costs nothing there
# (measured on 2026-10-19: eight connections were slower than one on an unthrottled
# 30 MB/s Hugging Face download).
DEFAULT_CONNECTIONS = 8
DEFAULT_SEGMENT_MIN_BYTES = 32 * 1024 * 1024
DEFAULT_SLOW_BYTES_PER_SECOND = 4 * 1024 * 1024
SPEED_WINDOW_SECONDS = 10.0
# How often the progress of a ranged download is reported, and its state saved.
PROGRESS_INTERVAL_SECONDS = 0.5
HASH_CHUNK_SIZE = 4 * 1024 * 1024
PART_SUFFIX = ".part"
STATE_SUFFIX = ".json"
STATE_VERSION = 1
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


class DownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    DRY_RUN = "dry-run"


class DownloadError(RuntimeError):
    """A download could not be completed."""

    def __init__(self, message: str, *, partials_retained: bool = False) -> None:
        super().__init__(message)
        self.partials_retained = partials_retained


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

    @property
    def identity(self) -> str:
        """What a partial download is of: the same URL, checksum and size. A digest, so
        the state file beside a partial never holds a URL's token."""
        text = f"{self.url}\n{self.sha256 or ''}\n{self.size_bytes or ''}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
            installation_checkpoint()
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
        connections: int = DEFAULT_CONNECTIONS,
        segment_min_bytes: int = DEFAULT_SEGMENT_MIN_BYTES,
        slow_bytes_per_second: float = DEFAULT_SLOW_BYTES_PER_SECOND,
        monotonic: Callable[[], float] = time.monotonic,
        dry_run: bool = False,
        log: logging.Logger | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts deve ser >= 1")
        if timeout <= 0:
            raise ValueError("timeout deve ser > 0")
        if connections < 1 or segment_min_bytes < 1:
            raise ValueError("connections e segment_min_bytes devem ser >= 1")
        self.client_factory = client_factory
        self.request_headers = request_headers
        self.layout = layout
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.chunk_size = chunk_size
        self.connections = connections
        self.segment_min_bytes = segment_min_bytes
        self.slow_bytes_per_second = slow_bytes_per_second
        self.monotonic = monotonic
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
        installation_checkpoint()
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
            installation_checkpoint()
            try:
                digest, written = self._transfer(spec, part, progress, cancel)
            except DownloadCancelledError:
                _discard(part)
                raise
            except InstallationCancelled:
                # Installation cancellation keeps resumable bytes for Repair.
                # The standalone ``cancel`` callback above still discards them.
                raise
            except Exception as exc:  # noqa: BLE001 - reclassificado logo abaixo
                # The bytes already here stay, with what they are: the next attempt
                # (this one's retry, or a later run) asks only for the rest.
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
                _discard(part)
                raise ChecksumMismatchError(
                    f"checksum mismatch for {spec.safe_url}: "
                    f"expected {spec.sha256}, got {digest}. The file was discarded."
                )

            installation_checkpoint()
            os.replace(part, spec.destination)
            _remove(_state_path(part))
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

        retained = part.is_file()
        kept = (
            " The bytes already fetched are kept; the next attempt resumes from them."
            if retained
            else ""
        )
        raise DownloadError(
            f"could not download {spec.safe_url} after {self.max_attempts} attempt(s): "
            f"{sanitize(str(last_error))}{kept}",
            partials_retained=retained,
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
        headers = dict(self.request_headers(spec.url)) if self.request_headers is not None else {}
        installation_checkpoint()
        total = spec.size_bytes
        splittable = bool(total) and self.connections > 1
        mode, resume = _resume_point(part, spec, total)
        with self._client() as client:
            if mode == "segments" and splittable:
                segments = resume
            else:
                offset = resume if mode == "prefix" else 0
                finished = self._stream(
                    client, spec, part, headers, offset, progress, cancel, splittable
                )
                if finished is not None:
                    return finished
                # The stream was slow and the source takes ranges: the rest in parallel.
                assert total is not None
                segments = _plan_segments(
                    total, self.connections, self.segment_min_bytes, part.stat().st_size
                )
                _prepare(part, total, keep=True)
                _save_state(part, spec, segments)
            self._segmented(client, spec, part, headers, segments, progress, cancel)
            return sha256_of(part, HASH_CHUNK_SIZE), segments[-1][1] + 1

    def _accepts_ranges(
        self, client: httpx.Client, spec: DownloadSpec, headers: dict, total: int
    ) -> bool:
        """Whether the source answers a byte range with that range of this file."""
        installation_checkpoint()
        with client.stream(
            "GET", spec.url, headers={**headers, "Range": "bytes=0-0"}, timeout=self.timeout
        ) as response:
            response.raise_for_status()
            found = _content_range(response)
            return response.status_code == 206 and found is not None and found[2] in (None, total)

    def _stream(
        self,
        client: httpx.Client,
        spec: DownloadSpec,
        part: Path,
        headers: dict,
        offset: int,
        progress: ProgressCallback | None,
        cancel: CancelCheck | None,
        splittable: bool = False,
    ) -> tuple[str, int] | None:
        """One plain stream, continuing a partial of ``offset`` bytes when the source
        answers the rest as a range; otherwise from the first byte. None when it stopped
        early to fetch the rest as ranges: it stayed slower than ``slow_bytes_per_second``
        for a whole window, enough of the file is left to split, and the source said it
        answers ranges. The partial then holds every byte the stream delivered."""
        request_headers = {**headers, "Range": f"bytes={offset}-"} if offset else headers
        installation_checkpoint()
        with client.stream(
            "GET", spec.url, headers=request_headers, timeout=self.timeout
        ) as response:
            if offset and response.status_code == 416:
                # Nothing after these bytes: the partial is the whole file.
                return sha256_of(part, HASH_CHUNK_SIZE), offset
            response.raise_for_status()
            found = _content_range(response)
            resumed = bool(offset) and response.status_code == 206 and (found or (0,))[0] == offset
            if not resumed:
                offset = 0
            length = _content_length(response)
            total = offset + length if length is not None else spec.size_bytes
            _save_state(part, spec, None)
            digest = None if resumed else hashlib.sha256()
            written = offset
            window_start, window_bytes = self.monotonic(), written
            with part.open("ab" if resumed else "wb") as handle:
                for chunk in response.iter_bytes(self.chunk_size):
                    installation_checkpoint()
                    if cancel is not None and cancel():
                        raise DownloadCancelledError(f"download cancelled: {spec.safe_url}")
                    handle.write(chunk)
                    if digest is not None:
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
                    if not splittable:
                        continue
                    now = self.monotonic()
                    if now - window_start < SPEED_WINDOW_SECONDS:
                        continue
                    speed = (written - window_bytes) / (now - window_start)
                    window_start, window_bytes = now, written
                    if (
                        speed < self.slow_bytes_per_second
                        and (total or 0) - written >= 2 * self.segment_min_bytes
                    ):
                        # Asked once: a source without ranges keeps this one stream.
                        splittable = False
                        if self._accepts_ranges(client, spec, headers, total or 0):
                            self.log.info(
                                "slow source (%.0f KB/s) for %s: fetching the rest as ranges",
                                speed / 1024,
                                spec.safe_url,
                            )
                            return None
        return (
            digest.hexdigest() if digest is not None else sha256_of(part, HASH_CHUNK_SIZE)
        ), written

    def _segmented(
        self,
        client: httpx.Client,
        spec: DownloadSpec,
        part: Path,
        headers: dict,
        segments: list[list[int]],
        progress: ProgressCallback | None,
        cancel: CancelCheck | None,
    ) -> None:
        """Every unfinished byte range at once, each over its own connection, written in
        place into the preallocated partial. The state is saved as bytes land, so an
        interruption anywhere loses at most what was in flight."""
        total = segments[-1][1] + 1
        lock = threading.Lock()
        stop = threading.Event()

        control = current_installation_control()

        def work(segment: list[int]) -> None:
            with control.scope() if control is not None else nullcontext():
                if control is not None:
                    control.checkpoint(stop=stop)
                start, end, _ = segment
                with lock:
                    offset = start + segment[2]
                if offset > end:
                    return
                ranged = {**headers, "Range": f"bytes={offset}-{end}"}
                with client.stream(
                    "GET", spec.url, headers=ranged, timeout=self.timeout
                ) as response:
                    response.raise_for_status()
                    found = _content_range(response)
                    if response.status_code != 206 or found is None or found[0] != offset:
                        raise httpx.RemoteProtocolError(
                            f"the source did not answer bytes {offset}-{end} as a range"
                        )
                    with part.open("r+b") as handle:
                        handle.seek(offset)
                        for chunk in response.iter_bytes(self.chunk_size):
                            if control is not None:
                                control.checkpoint(stop=stop)
                            if stop.is_set():
                                return
                            chunk = chunk[: end + 1 - offset]
                            handle.write(chunk)
                            offset += len(chunk)
                            with lock:
                                segment[2] = offset - start
                            if offset > end:
                                break
                if offset <= end:
                    raise httpx.RemoteProtocolError(
                        f"bytes {offset}-{end} ended before they arrived"
                    )

        def downloaded() -> int:
            with lock:
                return sum(segment[2] for segment in segments)

        def report() -> None:
            if progress is not None:
                progress(
                    DownloadProgress(
                        url=spec.safe_url,
                        destination=spec.destination,
                        downloaded_bytes=downloaded(),
                        total_bytes=total,
                    )
                )

        pending = [segment for segment in segments if segment[0] + segment[2] <= segment[1]]
        report()
        if not pending:
            return
        if cancel is not None and cancel():
            raise DownloadCancelledError(f"download cancelled: {spec.safe_url}")
        saved = downloaded()
        pool = ThreadPoolExecutor(max_workers=len(pending), thread_name_prefix="download")
        futures = [pool.submit(work, segment) for segment in pending]
        try:
            while True:
                installation_checkpoint()
                finished, running = wait(
                    futures, timeout=PROGRESS_INTERVAL_SECONDS, return_when=FIRST_EXCEPTION
                )
                current = downloaded()
                if current != saved:
                    with lock:
                        snapshot = [list(segment) for segment in segments]
                    _save_state(part, spec, snapshot)
                    saved = current
                report()
                failed = next((f.exception() for f in finished if f.exception()), None)
                if failed is not None:
                    raise failed
                if not running:
                    return
                if cancel is not None and cancel():
                    raise DownloadCancelledError(f"download cancelled: {spec.safe_url}")
        finally:
            stop.set()
            # Unblock a worker waiting on a slow read: closing the client ends its stream.
            if any(not future.done() for future in futures):
                client.close()
            pool.shutdown(wait=True)
            with lock:
                snapshot = [list(segment) for segment in segments]
            if part.is_file():
                _save_state(part, spec, snapshot)


def _plan_segments(total: int, connections: int, minimum: int, prefix: int = 0) -> list[list[int]]:
    """[start, end (inclusive), bytes done] ranges covering ``total``: the first
    ``prefix`` bytes (already written front to back) as one done range, the rest split
    into up to ``connections`` ranges of at least ``minimum`` bytes."""
    segments = [[0, prefix - 1, prefix]] if prefix else []
    remaining = total - prefix
    if remaining <= 0:
        return segments
    count = max(1, min(connections, remaining // minimum))
    size = -(-remaining // count)
    for start in range(prefix, total, size):
        segments.append([start, min(total, start + size) - 1, 0])
    return segments


def _state_path(part: Path) -> Path:
    return part.with_name(part.name + STATE_SUFFIX)


def _save_state(part: Path, spec: DownloadSpec, segments: list[list[int]] | None) -> None:
    """What the partial holds: ranges and how much of each is done, or a plain stream
    (its length is the file's). Written atomically; a lost write only costs bytes."""
    state = {"version": STATE_VERSION, "identity": spec.identity, "segments": segments}
    path = _state_path(part)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logger.debug("could not save the download state %s", path)


def _resume_point(part: Path, spec: DownloadSpec, total: int | None) -> tuple[str, object]:
    """Where a partial left off: ("segments", ranges) for a ranged download of this very
    file, ("prefix", n) for n bytes written front to back -- a stream, or a partial an
    earlier version left with no state -- or ("fresh", 0)."""
    if not part.is_file():
        _remove(_state_path(part))
        return "fresh", 0
    size = part.stat().st_size
    try:
        state = json.loads(_state_path(part).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    if isinstance(state, dict):
        if state.get("version") != STATE_VERSION or state.get("identity") != spec.identity:
            _discard(part)
            return "fresh", 0
        segments = state.get("segments")
        if segments is None:
            return ("prefix", size) if size else ("fresh", 0)
        if (
            isinstance(segments, list)
            and total
            and size == total
            and _segments_cover(segments, total)
        ):
            return "segments", [list(segment) for segment in segments]
        _discard(part)
        return "fresh", 0
    # No state: a partial an earlier version wrote front to back. Shorter than the file,
    # it is a prefix; the checksum still decides whether it was this file's.
    if 0 < size and (not total or size < total):
        return "prefix", size
    _discard(part)
    return "fresh", 0


def _segments_cover(segments: list, total: int) -> bool:
    expected = 0
    for segment in segments:
        if not (
            isinstance(segment, list)
            and len(segment) == 3
            and all(isinstance(value, int) for value in segment)
            and segment[0] == expected
            and segment[0] <= segment[1]
            and 0 <= segment[2] <= segment[1] - segment[0] + 1
        ):
            return False
        expected = segment[1] + 1
    return expected == total


def _prepare(part: Path, total: int, *, keep: bool) -> None:
    """The partial at its full length, ready for writes anywhere in it. Sparse on
    Windows, so a write far into the file does not first fill all before it with zeros."""
    with part.open("r+b" if keep and part.is_file() else "wb") as handle:
        _mark_sparse(handle)
        handle.truncate(total)


def _mark_sparse(handle) -> None:
    if os.name != "nt":
        return  # elsewhere a file extended past its end is sparse already
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        fsctl_set_sparse = 0x000900C4
        returned = wintypes.DWORD()
        ctypes.windll.kernel32.DeviceIoControl(
            wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno())),
            fsctl_set_sparse,
            None,
            0,
            None,
            0,
            ctypes.byref(returned),
            None,
        )
    except (OSError, AttributeError, ValueError):  # pragma: no cover - only slower without it
        logger.debug("could not mark %s sparse", getattr(handle, "name", "a partial"))


def _content_range(response: httpx.Response) -> tuple[int, int, int | None] | None:
    found = _CONTENT_RANGE.fullmatch((response.headers.get("content-range") or "").strip())
    if found is None:
        return None
    size = found.group(3)
    return int(found.group(1)), int(found.group(2)), None if size == "*" else int(size)


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _discard(part: Path) -> None:
    """The partial and what it records, gone: a cancel, a mismatch, or another file's."""
    _remove(part)
    _remove(_state_path(part))


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort cleanup
        logger.debug("could not remove partial file %s", path)
