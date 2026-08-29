from __future__ import annotations

import json

import pytest

from local_ai_stack.probes import ProbeOutcome, ProbeResult
from local_ai_stack.services import (
    COMFYUI_SPEC,
    DEFAULT_SERVICE_SPECS,
    OLLAMA_SPEC,
    OPEN_WEBUI_SPEC,
    ServiceSpec,
    ServiceState,
    ServiceStatus,
    aggregate_state,
    check_service,
    check_services,
    check_stack,
    spec_by_id,
)

HEALTHY_PAYLOADS = {
    "ollama": {"version": "0.5.4"},
    "open-webui": {"status": True},
    "comfyui": {"system": {"os": "nt"}, "devices": []},
}

PORT_ONLY_SPEC = ServiceSpec(id="port-only", name="Port Only", port=9999)


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


def status(state: ServiceState) -> ServiceStatus:
    return ServiceStatus(PORT_ONLY_SPEC, state)


# --- specs -----------------------------------------------------------------


def test_default_specs_cover_the_three_services():
    assert [s.id for s in DEFAULT_SERVICE_SPECS] == ["ollama", "open-webui", "comfyui"]


@pytest.mark.parametrize(
    ("spec", "port", "endpoint"),
    [
        (OLLAMA_SPEC, 11434, "http://127.0.0.1:11434/api/version"),
        (OPEN_WEBUI_SPEC, 8080, "http://127.0.0.1:8080/health"),
        (COMFYUI_SPEC, 8188, "http://127.0.0.1:8188/system_stats"),
    ],
)
def test_default_specs_declare_port_and_http_endpoint(spec, port, endpoint):
    assert spec.port == port
    assert spec.endpoint == endpoint


def test_spec_without_health_path_has_no_endpoint():
    assert PORT_ONLY_SPEC.endpoint is None


def test_spec_by_id_returns_spec_or_raises():
    assert spec_by_id("comfyui") is COMFYUI_SPEC
    with pytest.raises(KeyError):
        spec_by_id("stable-diffusion")


# --- service responding correctly -----------------------------------------


@pytest.mark.parametrize("spec", DEFAULT_SERVICE_SPECS, ids=lambda s: s.id)
def test_service_answering_its_endpoint_is_up(spec):
    result = check_service(
        spec, http=http_answering(payload=HEALTHY_PAYLOADS[spec.id]), tcp=tcp_forbidden
    )
    assert result.state is ServiceState.UP
    assert result.healthy
    assert result.checked_via == "http"


def test_up_check_uses_the_declared_endpoint():
    seen: list[tuple[str, float]] = []

    def probe(url: str, timeout: float) -> ProbeResult:
        seen.append((url, timeout))
        return ProbeResult(ProbeOutcome.OK, "HTTP 200", 1.0, 200, '{"version": "1"}')

    check_service(OLLAMA_SPEC, timeout=0.5, http=probe, tcp=tcp_forbidden)
    assert seen == [("http://127.0.0.1:11434/api/version", 0.5)]


# --- port open but endpoint invalid ---------------------------------------


def test_unexpected_status_code_is_degraded():
    result = check_service(OPEN_WEBUI_SPEC, http=http_answering(status=404), tcp=tcp_forbidden)
    assert result.state is ServiceState.DEGRADED
    assert "404" in result.detail


def test_non_json_body_is_degraded():
    result = check_service(
        OLLAMA_SPEC, http=http_answering(body="<html>login</html>"), tcp=tcp_forbidden
    )
    assert result.state is ServiceState.DEGRADED
    assert "did not return JSON" in result.detail


def test_json_that_is_not_an_object_is_degraded():
    result = check_service(COMFYUI_SPEC, http=http_answering(body="[]"), tcp=tcp_forbidden)
    assert result.state is ServiceState.DEGRADED
    assert "not an object" in result.detail


def test_missing_expected_key_is_degraded():
    result = check_service(
        COMFYUI_SPEC, http=http_answering(payload={"devices": []}), tcp=tcp_forbidden
    )
    assert result.state is ServiceState.DEGRADED
    assert "system" in result.detail


# --- timeout / refused -----------------------------------------------------


def test_timeout_is_unknown():
    result = check_service(
        OLLAMA_SPEC,
        http=http_failing(ProbeOutcome.TIMEOUT, "http timeout after 1.50s"),
        tcp=tcp_forbidden,
    )
    assert result.state is ServiceState.UNKNOWN
    assert "timeout" in result.detail


def test_connection_refused_is_down():
    result = check_service(
        OPEN_WEBUI_SPEC, http=http_failing(ProbeOutcome.REFUSED), tcp=tcp_forbidden
    )
    assert result.state is ServiceState.DOWN
    assert result.checked_via == "http"


# --- port fallback ---------------------------------------------------------


def test_transport_error_falls_back_to_open_port_as_degraded():
    result = check_service(
        COMFYUI_SPEC,
        http=http_failing(ProbeOutcome.ERROR, "http error: ReadError"),
        tcp=tcp_answering(ProbeOutcome.OK),
    )
    assert result.state is ServiceState.DEGRADED
    assert result.checked_via == "tcp"
    assert "port fallback" in result.detail
    assert "ReadError" in result.detail


def test_transport_error_with_refused_port_is_down():
    result = check_service(
        COMFYUI_SPEC,
        http=http_failing(ProbeOutcome.ERROR),
        tcp=tcp_answering(ProbeOutcome.REFUSED),
    )
    assert result.state is ServiceState.DOWN
    assert result.checked_via == "tcp"


def test_transport_error_with_timing_out_port_is_unknown():
    result = check_service(
        COMFYUI_SPEC,
        http=http_failing(ProbeOutcome.ERROR),
        tcp=tcp_answering(ProbeOutcome.TIMEOUT, "tcp timeout after 1.50s"),
    )
    assert result.state is ServiceState.UNKNOWN


def test_service_without_endpoint_uses_port_only():
    up = check_service(
        PORT_ONLY_SPEC, http=http_failing(ProbeOutcome.ERROR), tcp=tcp_answering(ProbeOutcome.OK)
    )
    down = check_service(
        PORT_ONLY_SPEC,
        http=http_failing(ProbeOutcome.ERROR),
        tcp=tcp_answering(ProbeOutcome.REFUSED),
    )
    assert (up.state, up.checked_via) == (ServiceState.UP, "tcp")
    assert down.state is ServiceState.DOWN
    assert "port fallback" not in up.detail


def test_real_probes_on_a_closed_port_report_the_service_as_unavailable():
    # Integration smoke: no service on 65534, so the check must never say UP and
    # must not raise. DOWN vs UNKNOWN depends on whether the host answers RST or
    # drops the SYN, so both are accepted here.
    spec = ServiceSpec(id="fixture", name="Fixture", port=65534, timeout=0.35)
    result = check_service(spec)
    assert result.state in {ServiceState.DOWN, ServiceState.UNKNOWN}
    assert not result.healthy


# --- aggregation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], ServiceState.UNKNOWN),
        ([ServiceState.UP, ServiceState.UP], ServiceState.UP),
        ([ServiceState.DOWN, ServiceState.DOWN], ServiceState.DOWN),
        ([ServiceState.UNKNOWN, ServiceState.UNKNOWN], ServiceState.UNKNOWN),
        ([ServiceState.DEGRADED, ServiceState.DEGRADED], ServiceState.DEGRADED),
        ([ServiceState.UP, ServiceState.DOWN], ServiceState.DEGRADED),
        ([ServiceState.UP, ServiceState.UNKNOWN], ServiceState.DEGRADED),
        ([ServiceState.DEGRADED, ServiceState.DOWN], ServiceState.DEGRADED),
        ([ServiceState.DOWN, ServiceState.UNKNOWN], ServiceState.UNKNOWN),
    ],
)
def test_aggregate_state(states, expected):
    assert aggregate_state(status(s) for s in states) == expected


def test_check_services_returns_one_status_per_spec():
    results = check_services(
        DEFAULT_SERVICE_SPECS, http=http_failing(ProbeOutcome.REFUSED), tcp=tcp_forbidden
    )
    assert [r.id for r in results] == [s.id for s in DEFAULT_SERVICE_SPECS]
    assert {r.state for r in results} == {ServiceState.DOWN}


def test_check_stack_aggregates_and_indexes_by_id():
    def probe(url: str, timeout: float) -> ProbeResult:
        if url.endswith("/api/version"):
            return ProbeResult(ProbeOutcome.OK, "HTTP 200", 1.0, 200, '{"version": "1"}')
        return ProbeResult(ProbeOutcome.REFUSED, "connection refused", 1.0)

    stack = check_stack(http=probe, tcp=tcp_forbidden)
    assert stack.state is ServiceState.DEGRADED
    assert stack.by_id("ollama").state is ServiceState.UP
    assert stack.by_id("comfyui").state is ServiceState.DOWN
    assert stack.by_id("nope") is None
