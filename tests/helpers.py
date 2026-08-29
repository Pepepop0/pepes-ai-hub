from __future__ import annotations

import json

from local_ai_stack.inventory import (
    Listener,
    ListenerReport,
    ProcessInfo,
    ProcessReport,
    ScanOutcome,
)
from local_ai_stack.probes import ProbeOutcome, ProbeResult

NO_LISTENER_SCAN = ListenerReport(ScanOutcome.UNAVAILABLE, detail="psutil is not installed")
NO_PROCESS_SCAN = ProcessReport(ScanOutcome.UNAVAILABLE, detail="psutil is not installed")
NOTHING_LISTENING = ListenerReport(ScanOutcome.OK)
NO_PROCESSES = ProcessReport(ScanOutcome.OK)


def listening(*ports: int, host: str = "127.0.0.1", pid: int | None = None) -> ListenerReport:
    return ListenerReport(ScanOutcome.OK, tuple(Listener(host, port, pid) for port in ports))


def running(*processes: ProcessInfo) -> ProcessReport:
    return ProcessReport(ScanOutcome.OK, processes)


def http_answering(status: int = 200, payload=None, body: str | None = None):
    text = body if body is not None else json.dumps(payload if payload is not None else {})

    def probe(url: str, timeout: float) -> ProbeResult:
        return ProbeResult(ProbeOutcome.OK, f"HTTP {status}", 1.0, status, text)

    return probe


def http_failing(outcome: ProbeOutcome, detail: str = "boom"):
    def probe(url: str, timeout: float) -> ProbeResult:
        return ProbeResult(outcome, detail, 1.0)

    return probe


def tcp_answering(outcome: ProbeOutcome, detail: str = ""):
    def probe(host: str, port: int, timeout: float) -> ProbeResult:
        return ProbeResult(outcome, detail, 1.0)

    return probe


def tcp_forbidden(host: str, port: int, timeout: float) -> ProbeResult:
    raise AssertionError("port fallback must not run while the HTTP endpoint answers")
