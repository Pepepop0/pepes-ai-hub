from __future__ import annotations

from local_ai_stack.doctor import doctor_text
from local_ai_stack.services import (
    COMFYUI_SPEC,
    OLLAMA_SPEC,
    OPEN_WEBUI_SPEC,
    ServiceState,
    ServiceStatus,
    StackHealth,
)

STACK = StackHealth(
    (
        ServiceStatus(OLLAMA_SPEC, ServiceState.UP, "HTTP 200 on /api/version", "http", 3.2),
        ServiceStatus(OPEN_WEBUI_SPEC, ServiceState.DOWN, "connection refused", "http", 1.0),
        ServiceStatus(COMFYUI_SPEC, ServiceState.DEGRADED, "unexpected HTTP 500", "http", 2.0),
    ),
    ServiceState.DEGRADED,
)


def test_doctor_reports_each_service_and_the_aggregate():
    text = doctor_text(STACK)
    assert "Services:" in text
    assert "Ollama" in text and "127.0.0.1:11434" in text and "[UP]" in text
    assert "[DOWN]" in text and "connection refused" in text
    assert "[DEGRADED]" in text
    assert text.rstrip().endswith("Stack: DEGRADED")


def test_doctor_still_reports_system_facts():
    text = doctor_text(STACK)
    assert text.startswith("Local AI Stack — Doctor")
    assert "Python:" in text and "nvidia-smi:" in text
