"""Low-level transport probes used by the runtime health layer.

A probe answers a single transport question ("can I open this socket?",
"what did this URL answer?"). It never decides whether a service is healthy —
that decision belongs to :mod:`local_ai_stack.services`.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from enum import Enum

import httpx

DEFAULT_TIMEOUT = 1.5
MAX_BODY_CHARS = 65536


class ProbeOutcome(str, Enum):
    """Transport-level result of a single probe."""

    OK = "ok"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    detail: str = ""
    latency_ms: float | None = None
    status_code: int | None = None
    body: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is ProbeOutcome.OK


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _is_refused(exc: BaseException) -> bool:
    """Walk the exception chain looking for a real connection refusal."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        current = current.__cause__ or current.__context__
    return "refused" in str(exc).lower()


def tcp_probe(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """Check whether something accepts TCP connections on host:port."""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ProbeResult(ProbeOutcome.OK, f"port {port} open", _elapsed_ms(start))
    except TimeoutError:
        detail = f"tcp timeout after {timeout:.2f}s"
        return ProbeResult(ProbeOutcome.TIMEOUT, detail, _elapsed_ms(start))
    except ConnectionRefusedError:
        return ProbeResult(ProbeOutcome.REFUSED, "connection refused", _elapsed_ms(start))
    except OSError as exc:
        if _is_refused(exc):
            return ProbeResult(ProbeOutcome.REFUSED, "connection refused", _elapsed_ms(start))
        detail = f"socket error: {exc.__class__.__name__}"
        return ProbeResult(ProbeOutcome.ERROR, detail, _elapsed_ms(start))


def http_probe(url: str, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """GET ``url`` and report what the transport did, not whether it is healthy."""
    start = time.perf_counter()
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.TimeoutException:
        detail = f"http timeout after {timeout:.2f}s"
        return ProbeResult(ProbeOutcome.TIMEOUT, detail, _elapsed_ms(start))
    except httpx.HTTPError as exc:
        if _is_refused(exc):
            return ProbeResult(ProbeOutcome.REFUSED, "connection refused", _elapsed_ms(start))
        detail = f"http error: {exc.__class__.__name__}"
        return ProbeResult(ProbeOutcome.ERROR, detail, _elapsed_ms(start))
    return ProbeResult(
        ProbeOutcome.OK,
        f"HTTP {response.status_code}",
        _elapsed_ms(start),
        response.status_code,
        response.text[:MAX_BODY_CHARS],
    )
