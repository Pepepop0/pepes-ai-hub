from __future__ import annotations

from helpers import http_answering, http_failing, listening, running, tcp_forbidden

from local_ai_stack.doctor import doctor_text
from local_ai_stack.inventory import ProcessInfo
from local_ai_stack.probes import ProbeOutcome
from local_ai_stack.runtime import stack_runtime
from local_ai_stack.services import (
    COMFYUI_SPEC,
    OLLAMA_SPEC,
    OPEN_WEBUI_SPEC,
    ServiceState,
    ServiceStatus,
    StackHealth,
)

COMFY_DESKTOP = ProcessInfo(3001, "ComfyUI.exe", r"C:\Programs\ComfyUI\ComfyUI.exe", "")

HEALTH_ONLY_STACK = StackHealth(
    (
        ServiceStatus(OLLAMA_SPEC, ServiceState.UP, "HTTP 200 on /api/version", "http", 3.2),
        ServiceStatus(OPEN_WEBUI_SPEC, ServiceState.DOWN, "connection refused", "http", 1.0),
        ServiceStatus(COMFYUI_SPEC, ServiceState.DEGRADED, "unexpected HTTP 500", "http", 2.0),
    ),
    ServiceState.DEGRADED,
)


def stopped_ollama_running_comfy():
    return stack_runtime(
        http=http_answering(payload={"system": {}}),
        tcp=tcp_forbidden,
        listeners=listening(8188, pid=COMFY_DESKTOP.pid),
        processes=running(COMFY_DESKTOP),
    )


def test_doctor_shows_runtime_and_health_per_service():
    text = doctor_text(stopped_ollama_running_comfy())
    assert "Ollama (127.0.0.1:11434)" in text
    assert "  Runtime: STOPPED" in text
    assert "  Health:  DOWN — no local process listening on 127.0.0.1:11434" in text
    assert "  Runtime: RUNNING (pid 3001, external)" in text
    assert "  Health:  UP" in text


def test_doctor_reports_the_aggregated_stack_state():
    text = doctor_text(stopped_ollama_running_comfy())
    assert text.rstrip().endswith("Stack: DEGRADED")


def test_doctor_flags_a_live_process_that_does_not_serve():
    stack = stack_runtime(
        http=http_failing(ProbeOutcome.TIMEOUT, "http timeout after 1.50s"),
        tcp=tcp_forbidden,
        listeners=listening(8188, pid=COMFY_DESKTOP.pid),
        processes=running(COMFY_DESKTOP),
    )
    text = doctor_text(stack)
    assert "ComfyUI (127.0.0.1:8188) [DEGRADED]" in text
    assert "  Health:  UNKNOWN — http timeout after 1.50s" in text


def test_doctor_still_accepts_a_health_only_stack():
    text = doctor_text(HEALTH_ONLY_STACK)
    assert "  Runtime: UNKNOWN — process state not inspected" in text
    assert "  Health:  UP — HTTP 200 on /api/version" in text
    assert text.rstrip().endswith("Stack: DEGRADED")


def test_doctor_still_reports_system_facts():
    text = doctor_text(HEALTH_ONLY_STACK)
    assert text.startswith("Local AI Stack — Doctor")
    assert "Python:" in text and "nvidia-smi:" in text
