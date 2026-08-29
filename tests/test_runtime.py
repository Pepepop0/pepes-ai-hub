from __future__ import annotations

import pytest
from helpers import (
    NO_LISTENER_SCAN,
    NO_PROCESS_SCAN,
    NOTHING_LISTENING,
    http_answering,
    http_failing,
    listening,
    running,
    tcp_forbidden,
)

from local_ai_stack.inventory import ProcessInfo
from local_ai_stack.probes import ProbeOutcome, ProbeResult
from local_ai_stack.processes import RuntimeProcessState
from local_ai_stack.runtime import combine_state, stack_runtime
from local_ai_stack.services import ServiceState

OLLAMA_SERVE = ProcessInfo(1001, "ollama.exe", r"C:\Programs\Ollama\ollama.exe", "ollama.exe serve")
COMFY_DESKTOP = ProcessInfo(3001, "ComfyUI.exe", r"C:\Programs\ComfyUI\ComfyUI.exe", "")


@pytest.mark.parametrize(
    ("health", "process", "expected"),
    [
        (ServiceState.UP, RuntimeProcessState.RUNNING, ServiceState.UP),
        (ServiceState.UP, RuntimeProcessState.UNKNOWN, ServiceState.UP),
        (ServiceState.DEGRADED, RuntimeProcessState.RUNNING, ServiceState.DEGRADED),
        (ServiceState.DOWN, RuntimeProcessState.STOPPED, ServiceState.DOWN),
        (ServiceState.DOWN, RuntimeProcessState.RUNNING, ServiceState.DEGRADED),
        (ServiceState.UNKNOWN, RuntimeProcessState.RUNNING, ServiceState.DEGRADED),
        (ServiceState.UNKNOWN, RuntimeProcessState.UNKNOWN, ServiceState.UNKNOWN),
        (ServiceState.UNKNOWN, RuntimeProcessState.STOPPED, ServiceState.UNKNOWN),
    ],
)
def test_combine_state(health, process, expected):
    assert combine_state(health, process) is expected


def test_stopped_stack_reports_process_and_health_separately():
    stack = stack_runtime(
        http=http_failing(ProbeOutcome.TIMEOUT),
        tcp=tcp_forbidden,
        listeners=NOTHING_LISTENING,
        processes=NO_PROCESS_SCAN,
    )
    ollama = stack.by_id("ollama")
    assert ollama.process.state is RuntimeProcessState.STOPPED
    assert ollama.health.state is ServiceState.DOWN
    assert ollama.state is ServiceState.DOWN
    assert stack.state is ServiceState.DOWN


def test_running_and_healthy_service_is_up():
    def probe(url: str, timeout: float) -> ProbeResult:
        return ProbeResult(ProbeOutcome.OK, "HTTP 200", 1.0, 200, '{"version": "1"}')

    stack = stack_runtime(
        http=probe,
        tcp=tcp_forbidden,
        listeners=listening(11434, pid=OLLAMA_SERVE.pid),
        processes=running(OLLAMA_SERVE),
    )
    ollama = stack.by_id("ollama")
    assert ollama.process.state is RuntimeProcessState.RUNNING
    assert ollama.process.pid == OLLAMA_SERVE.pid
    assert ollama.health.state is ServiceState.UP
    assert ollama.state is ServiceState.UP


def test_running_process_that_does_not_serve_is_degraded():
    stack = stack_runtime(
        http=http_failing(ProbeOutcome.TIMEOUT, "http timeout after 1.50s"),
        tcp=tcp_forbidden,
        listeners=listening(8188, pid=COMFY_DESKTOP.pid),
        processes=running(COMFY_DESKTOP),
    )
    comfy = stack.by_id("comfyui")
    assert comfy.process.state is RuntimeProcessState.RUNNING
    assert comfy.health.state is ServiceState.UNKNOWN
    assert comfy.state is ServiceState.DEGRADED


def test_mixed_stack_aggregates_to_degraded():
    def probe(url: str, timeout: float) -> ProbeResult:
        if url.endswith("/api/version"):
            return ProbeResult(ProbeOutcome.OK, "HTTP 200", 1.0, 200, '{"version": "1"}')
        return ProbeResult(ProbeOutcome.REFUSED, "connection refused", 1.0)

    stack = stack_runtime(
        http=probe,
        tcp=tcp_forbidden,
        listeners=listening(11434, 8080, 8188),
        processes=running(OLLAMA_SERVE),
    )
    assert stack.state is ServiceState.DEGRADED
    assert stack.by_id("ollama").state is ServiceState.UP
    assert stack.by_id("open-webui").state is ServiceState.DOWN
    assert stack.by_id("nope") is None


def test_health_view_stays_available_for_older_callers():
    stack = stack_runtime(
        http=http_answering(payload={"version": "1"}),
        tcp=tcp_forbidden,
        listeners=listening(11434),
        processes=NO_PROCESS_SCAN,
    )
    health = stack.health
    assert [s.id for s in health.services] == ["ollama", "open-webui", "comfyui"]
    assert health.by_id("ollama").state is ServiceState.UP


def test_without_psutil_nothing_is_assumed():
    stack = stack_runtime(
        http=http_failing(ProbeOutcome.TIMEOUT, "http timeout after 1.50s"),
        tcp=tcp_forbidden,
        listeners=NO_LISTENER_SCAN,
        processes=NO_PROCESS_SCAN,
    )
    ollama = stack.by_id("ollama")
    assert ollama.process.state is RuntimeProcessState.UNKNOWN
    assert ollama.health.state is ServiceState.UNKNOWN
    assert stack.state is ServiceState.UNKNOWN
