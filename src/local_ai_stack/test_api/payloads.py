"""Turning a request body into a capability request, or refusing it.

The whole security argument of this API is in the shape of :class:`TestRunSpec`.
A caller names a profile the stack already knows, phases the contract already
defines, and two enumerated modes. There is no field for a command, an
executable, a path, a URL, a port, a PID or an environment variable, so a
request cannot express those things and no amount of validation logic is needed
to reject them one by one.

What is left is to refuse anything outside that shape: an unknown field, an
unknown enum value, a profile this stack does not have, a value of the wrong
type. Unknown input is an error rather than something quietly dropped, because
a silently ignored field is a caller believing something happened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from ..test_contracts import (
    TestPhase,
    TestPhaseResult,
    TestProviderMode,
    TestRootMode,
    TestRunReport,
    TestRunSpec,
)
from .sanitize import bound_text

MAX_BODY_BYTES = 4096
CONTENT_TYPE = "application/json"

RUN_SPEC_FIELDS = frozenset(
    {"profile_id", "phases", "root_mode", "provider_mode", "allow_network", "allow_gpu"}
)


class PayloadError(ValueError):
    """The request body is not a valid run request."""


def _enum(value: object, enum: type, field: str):
    if not isinstance(value, str):
        raise PayloadError(f"{field!r} must be a string")
    try:
        return enum(value)
    except ValueError:
        known = ", ".join(sorted(member.value for member in enum))
        raise PayloadError(f"{field!r} must be one of: {known}") from None


def _flag(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise PayloadError(f"{field!r} must be true or false")
    return value


def parse_run_spec(payload: object, *, known_profiles: frozenset[str]) -> TestRunSpec:
    """Validate a decoded request body into a run specification."""
    if not isinstance(payload, Mapping):
        raise PayloadError("the request body must be a JSON object")

    unknown = sorted(str(key) for key in payload if str(key) not in RUN_SPEC_FIELDS)
    if unknown:
        raise PayloadError(
            f"unknown field(s): {', '.join(bound_text(name, limit=40) for name in unknown)}"
        )

    profile_id = payload.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise PayloadError("'profile_id' must be a non-empty string")
    if profile_id not in known_profiles:
        # The known set is not echoed back: it is not the caller's to enumerate.
        raise PayloadError("'profile_id' does not name a profile this stack describes")

    raw_phases = payload.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        raise PayloadError("'phases' must be a non-empty list")
    phases = tuple(_enum(item, TestPhase, "phases") for item in raw_phases)
    if len(set(phases)) != len(phases):
        raise PayloadError("'phases' cannot repeat a phase")

    root_mode = (
        _enum(payload["root_mode"], TestRootMode, "root_mode")
        if "root_mode" in payload
        else TestRootMode.EPHEMERAL
    )
    provider_mode = (
        _enum(payload["provider_mode"], TestProviderMode, "provider_mode")
        if "provider_mode" in payload
        else TestProviderMode.CONTROLLED
    )

    try:
        return TestRunSpec(
            profile_id=profile_id,
            phases=phases,
            root_mode=root_mode,
            provider_mode=provider_mode,
            allow_network=_flag(payload.get("allow_network", False), "allow_network"),
            allow_gpu=_flag(payload.get("allow_gpu", False), "allow_gpu"),
        )
    except ValueError as error:
        raise PayloadError(str(error)) from None


def run_spec_payload(spec: TestRunSpec) -> dict[str, object]:
    return {
        "profile_id": spec.profile_id,
        "phases": [phase.value for phase in spec.phases],
        "root_mode": spec.root_mode.value,
        "provider_mode": spec.provider_mode.value,
        "allow_network": spec.allow_network,
        "allow_gpu": spec.allow_gpu,
    }


def phase_result_payload(result: TestPhaseResult) -> dict[str, object]:
    return {
        "phase": result.phase.value,
        "outcome": result.outcome.value,
        "detail": result.detail,
        "duration_seconds": result.duration_seconds,
        "data": dict(result.data),
        "references": list(result.references),
    }


def run_report_payload(report: TestRunReport) -> dict[str, object]:
    """The harness report, serialized without being reinterpreted."""
    return {
        "run_id": report.run_id,
        "state": report.state.value,
        "schema_version": report.schema_version,
        "spec": run_spec_payload(report.spec),
        "phases": [phase_result_payload(phase) for phase in report.phases],
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "detail": report.detail,
        "context": dict(report.context),
    }


def event_payload(event) -> dict[str, object]:
    """An event as it travels over SSE."""
    payload = asdict(event)
    payload["kind"] = event.kind.value
    payload["phase"] = event.phase.value if event.phase is not None else None
    payload["data"] = dict(event.data)
    return payload


__all__ = [
    "CONTENT_TYPE",
    "MAX_BODY_BYTES",
    "RUN_SPEC_FIELDS",
    "PayloadError",
    "event_payload",
    "parse_run_spec",
    "phase_result_payload",
    "run_report_payload",
    "run_spec_payload",
]
