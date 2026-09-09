import pytest

from local_ai_stack import test_contracts as contracts


def test_safe_test_run_defaults_are_ephemeral_controlled_and_offline():
    spec = contracts.TestRunSpec(
        "pepe", (contracts.TestPhase.PREFLIGHT, contracts.TestPhase.SECOND_PLAN)
    )

    assert spec.profile_id == "pepe"
    assert spec.root_mode is contracts.TestRootMode.EPHEMERAL
    assert spec.provider_mode is contracts.TestProviderMode.CONTROLLED
    assert spec.allow_network is False
    assert spec.allow_gpu is False


def test_test_run_requires_a_nonempty_unique_phase_sequence():
    with pytest.raises(ValueError, match="profile_id"):
        contracts.TestRunSpec(" ", (contracts.TestPhase.PREFLIGHT,))
    with pytest.raises(ValueError, match="at least one"):
        contracts.TestRunSpec("pepe", ())
    with pytest.raises(ValueError, match="repeat"):
        contracts.TestRunSpec("pepe", (contracts.TestPhase.START, contracts.TestPhase.START))


def test_phase_result_rejects_an_impossible_duration():
    with pytest.raises(ValueError, match="negative"):
        contracts.TestPhaseResult(
            contracts.TestPhase.PREFLIGHT,
            contracts.TestPhaseOutcome.FAILED,
            duration_seconds=-0.1,
        )


def test_run_and_event_identifiers_are_required():
    spec = contracts.TestRunSpec("pepe", (contracts.TestPhase.PREFLIGHT,))
    with pytest.raises(ValueError, match="run_id"):
        contracts.TestRunReport(" ", contracts.TestRunState.QUEUED, spec)
    with pytest.raises(ValueError, match="run_id"):
        contracts.TestEvent("", 1, contracts.TestEventKind.RUN_STATE, "2026-09-09T00:00:00Z")


def test_event_sequence_is_positive_and_timestamp_is_present():
    with pytest.raises(ValueError, match="starts at 1"):
        contracts.TestEvent("run-1", 0, contracts.TestEventKind.PROGRESS, "2026-09-09T00:00:00Z")
    with pytest.raises(ValueError, match="timestamp"):
        contracts.TestEvent("run-1", 1, contracts.TestEventKind.PROGRESS, " ")


def test_report_keeps_phase_results_transport_neutral():
    phase = contracts.TestPhaseResult(
        contracts.TestPhase.CHAT_SMOKE,
        contracts.TestPhaseOutcome.PASSED,
        duration_seconds=1.25,
        data={"endpoint": "http://127.0.0.1:12434"},
        references=("logs/chat.json",),
    )

    report = contracts.TestRunReport(
        "run-1",
        contracts.TestRunState.PASSED,
        contracts.TestRunSpec("pepe", (contracts.TestPhase.CHAT_SMOKE,)),
        phases=(phase,),
        started_at="2026-09-09T00:00:00Z",
        finished_at="2026-09-09T00:00:02Z",
    )

    assert report.schema_version == 1
    assert report.spec.profile_id == "pepe"
    assert report.phases == (phase,)
