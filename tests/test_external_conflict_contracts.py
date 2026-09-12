"""Foundation tests for user-selected external conflict policy; no OS mutation."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from local_ai_stack.components import ComponentId
from local_ai_stack.external_conflict_contracts import (
    ConflictEndpoint,
    ConflictHardBlocker,
    ConflictPolicySelection,
    ConflictPreflightDisposition,
    ConflictPreflightReport,
    ConflictResolutionResult,
    ConflictResolutionStatus,
    ExternalConflictKind,
    ExternalConflictOrigin,
    ExternalConflictPolicy,
    ExternalConflictResolver,
    ExternalServiceConflict,
    installation_outcome_heading,
)
from local_ai_stack.installation import InstallLocation, InstallLocationMode
from local_ai_stack.operation_contracts import OperationState

EXTERNAL = ConflictEndpoint("127.0.0.1", 11434)
ISOLATED = ConflictEndpoint("127.0.0.1", 12434)


def conflict(**changes) -> ExternalServiceConflict:
    return replace(
        ExternalServiceConflict(
            service_id=ComponentId.OLLAMA,
            kind=ExternalConflictKind.ENDPOINT_OCCUPIED,
            origin=ExternalConflictOrigin.EXTERNAL,
            endpoint=EXTERNAL,
            reason="Ollama externo ocupa o endpoint solicitado",
            can_stop_safely=True,
            managed_endpoint=ISOLATED,
        ),
        **changes,
    )


def report(*items, blockers=()) -> ConflictPreflightReport:
    return ConflictPreflightReport("preflight-1", items, blockers)


def test_policy_enum_is_minimal_and_alongside_is_the_first_recommended_option():
    assert list(ExternalConflictPolicy) == [
        ExternalConflictPolicy.INSTALL_ALONGSIDE,
        ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL,
        ExternalConflictPolicy.ABORT,
    ]
    assessment = report(conflict())
    assert assessment.disposition is ConflictPreflightDisposition.RESOLUTION_REQUIRED
    assert assessment.available_policies == tuple(ExternalConflictPolicy)
    assert assessment.recommended_policy is ExternalConflictPolicy.INSTALL_ALONGSIDE


@pytest.mark.parametrize("mode", list(InstallLocationMode))
@pytest.mark.parametrize("policy", list(ExternalConflictPolicy))
def test_filesystem_install_location_is_independent_from_conflict_policy(mode, policy, tmp_path):
    root = tmp_path / "loira-40k" if mode is InstallLocationMode.CUSTOM else None
    location = InstallLocation(mode, root)
    selection = ConflictPolicySelection("preflight-1", policy)
    assert location.mode is mode
    assert location.root == root
    assert {item.name for item in fields(selection)} == {"preflight_id", "policy"}
    assert all(item.name not in {"mode", "root", "path"} for item in fields(selection))


def test_custom_root_and_external_installation_are_not_a_runtime_conflict_by_themselves(tmp_path):
    custom = InstallLocation.custom(tmp_path / "loira-40k")
    assessment = report()
    assert custom.root != Path("C:/Users/Pedro/AppData/Local/Programs/Ollama")
    assert assessment.disposition is ConflictPreflightDisposition.READY
    assert assessment.conflicts == ()
    assert assessment.available_policies == ()
    assert assessment.recommended_policy is None


def test_hard_blocker_is_not_presented_as_resolvable():
    assessment = report(
        conflict(),
        blockers=(ConflictHardBlocker("disk-space", "Não há espaço livre suficiente"),),
    )
    assert assessment.disposition is ConflictPreflightDisposition.BLOCKED
    assert assessment.available_policies == (ExternalConflictPolicy.ABORT,)
    assert assessment.recommended_policy is ExternalConflictPolicy.ABORT
    with pytest.raises(ValueError, match="unavailable"):
        assessment.validate_selection(
            ConflictPolicySelection("preflight-1", ExternalConflictPolicy.INSTALL_ALONGSIDE)
        )


def test_occupied_alternative_managed_endpoint_is_a_hard_blocker():
    assessment = report(
        conflict(),
        blockers=(
            ConflictHardBlocker(
                "managed-endpoint-occupied", "O endpoint isolado do Ollama também está ocupado"
            ),
        ),
    )
    assert assessment.disposition is ConflictPreflightDisposition.BLOCKED
    assert ExternalConflictPolicy.INSTALL_ALONGSIDE not in assessment.available_policies


def test_unsafe_external_cannot_be_selected_for_stop():
    assessment = report(conflict(can_stop_safely=False))
    assert assessment.available_policies == (
        ExternalConflictPolicy.INSTALL_ALONGSIDE,
        ExternalConflictPolicy.ABORT,
    )
    with pytest.raises(ValueError, match="unavailable"):
        assessment.validate_selection(
            ConflictPolicySelection(
                assessment.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            )
        )


def test_stop_is_available_only_when_every_conflict_has_safe_private_evidence():
    comfy = conflict(
        service_id=ComponentId.COMFYUI,
        endpoint=ConflictEndpoint("127.0.0.1", 8188),
        managed_endpoint=ConflictEndpoint("127.0.0.1", 8288),
        can_stop_safely=False,
    )
    assessment = report(conflict(), comfy)
    assert ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL not in assessment.available_policies
    assert assessment.recommended_policy is ExternalConflictPolicy.INSTALL_ALONGSIDE


def test_no_common_safe_resolution_requires_an_explicit_hard_blocker():
    alongside_only = conflict(can_stop_safely=False)
    stop_only = conflict(
        service_id=ComponentId.COMFYUI,
        endpoint=ConflictEndpoint("127.0.0.1", 8188),
        managed_endpoint=None,
    )
    with pytest.raises(ValueError, match="hard blocker"):
        report(alongside_only, stop_only)


def test_alongside_selection_is_non_destructive_data_only():
    assessment = report(conflict())
    selection = ConflictPolicySelection(
        assessment.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE
    )
    assessment.validate_selection(selection)
    assert selection.policy is ExternalConflictPolicy.INSTALL_ALONGSIDE
    assert assessment.conflicts[0].endpoint == EXTERNAL
    assert assessment.conflicts[0].managed_endpoint == ISOLATED
    with pytest.raises(FrozenInstanceError):
        assessment.conflicts[0].can_stop_safely = False


def test_selection_is_bound_to_the_authoritative_preflight_snapshot():
    assessment = report(conflict())
    with pytest.raises(ValueError, match="stale"):
        assessment.validate_selection(
            ConflictPolicySelection("preflight-old", ExternalConflictPolicy.INSTALL_ALONGSIDE)
        )


def test_ui_command_carries_no_arbitrary_process_or_endpoint_authority():
    selection_fields = {item.name for item in fields(ConflictPolicySelection)}
    assert selection_fields == {"preflight_id", "policy"}
    forbidden = {
        "pid",
        "executable",
        "path",
        "process",
        "process_name",
        "endpoint",
        "shell",
        "command",
    }
    assert not (selection_fields & forbidden)
    method = inspect.signature(ExternalConflictResolver.resolve_external_conflicts)
    assert list(method.parameters) == ["self", "selection"]
    conflict_fields = {item.name for item in fields(ExternalServiceConflict)}
    assert not (conflict_fields & {"pid", "executable", "path", "process_name", "shell", "command"})


@pytest.mark.parametrize(
    "changes",
    [
        {"service_id": "ollama"},
        {"kind": "endpoint-occupied"},
        {"origin": "external"},
        {"endpoint": "127.0.0.1:11434"},
        {"reason": ""},
        {"reason": "line\nbreak"},
        {"can_stop_safely": 1},
        {"managed_endpoint": EXTERNAL},
    ],
)
def test_conflict_shape_rejects_untyped_or_ambiguous_values(changes):
    with pytest.raises((TypeError, ValueError)):
        conflict(**changes)


@pytest.mark.parametrize(
    "endpoint",
    [
        ConflictEndpoint("localhost", 11434),
        ConflictEndpoint("::1", 8188),
        ConflictEndpoint("0.0.0.0", 8080),
    ],
)
def test_endpoint_is_a_read_only_display_projection(endpoint):
    assert endpoint.display.endswith(f":{endpoint.port}")
    with pytest.raises(FrozenInstanceError):
        endpoint.port = 1


@pytest.mark.parametrize(
    "host,port", [("", 1), ("bad host", 1), ("host", 0), ("host", 65536), ("host", True)]
)
def test_endpoint_shape_is_bounded(host, port):
    with pytest.raises(ValueError):
        ConflictEndpoint(host, port)


def test_duplicate_service_or_alternative_endpoint_is_rejected():
    with pytest.raises(ValueError, match="repeat"):
        report(conflict(), conflict())
    with pytest.raises(ValueError, match="unique"):
        report(
            conflict(),
            conflict(
                service_id=ComponentId.COMFYUI,
                endpoint=ConflictEndpoint("127.0.0.1", 8188),
            ),
        )


def test_resolution_result_cannot_disguise_abort_or_failure():
    aborted = ConflictResolutionResult(
        "preflight-1",
        ExternalConflictPolicy.ABORT,
        ConflictResolutionStatus.ABORTED,
        "Instalação cancelada",
    )
    assert aborted.status is ConflictResolutionStatus.ABORTED
    with pytest.raises(ValueError, match="together"):
        replace(aborted, status=ConflictResolutionStatus.PROCEED)
    with pytest.raises(ValueError, match="together"):
        replace(
            aborted,
            policy=ExternalConflictPolicy.INSTALL_ALONGSIDE,
            status=ConflictResolutionStatus.ABORTED,
        )


def test_terminal_installation_headings_are_unambiguous():
    assert installation_outcome_heading(OperationState.SUCCEEDED) == "Concluído"
    assert installation_outcome_heading(OperationState.FAILED) == "Instalação não concluída"
    assert installation_outcome_heading(OperationState.SUCCEEDED) != installation_outcome_heading(
        OperationState.FAILED
    )
    for active in (OperationState.PREPARING, OperationState.QUEUED, OperationState.RUNNING):
        with pytest.raises(ValueError, match="terminal"):
            installation_outcome_heading(active)
