"""Contract behavior, including a fake producer shared by independent consumers."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest

from local_ai_stack.application_contracts import (
    ApplicationReadModel,
    ResourceCoordinator,
    ServicesSnapshot,
    ServiceView,
    VramSnapshot,
)
from local_ai_stack.image_generation_contracts import (
    ImageGenerationCoordinator,
    ImageGenerationRequest,
    ImageGenerationResult,
    ImageJobObservation,
    ImageJobObservationError,
    ImageJobObserver,
    ImageJobReceipt,
    ImageJobState,
)
from local_ai_stack.operation_contracts import (
    DETAIL_HISTORY_LIMIT,
    OperationActivity,
    OperationActivityCategory,
    OperationDiagnostic,
    OperationEvent,
    OperationEventKind,
    OperationPhase,
    OperationProgress,
    OperationResult,
    OperationSnapshot,
    OperationState,
    OperationTimelineEntry,
    TransferProgress,
    advance_operation,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)
FAILURE = OperationDiagnostic("generation-failed", "Generation could not finish", "diagnostic-1")


def activity(**changes):
    return replace(
        OperationActivity(
            OperationActivityCategory.DEPENDENCY,
            "installing",
            "torch",
            "Resolving package",
            18.0,
        ),
        **changes,
    )


def event(**changes):
    return replace(
        OperationEvent(
            "operation-1",
            1,
            OperationEventKind.STATE,
            OperationState.PREPARING,
            NOW,
            OperationPhase.PREPARATION,
            "Preparing generation",
        ),
        **changes,
    )


def terminal(state=OperationState.SUCCEEDED, *, sequence=2):
    result = ImageGenerationResult(
        "operation-1",
        state,
        output_references=("image-1",) if state is OperationState.SUCCEEDED else (),
        diagnostics=(FAILURE,) if state is OperationState.FAILED else (),
        duration_seconds=2.5,
    )
    return event(sequence=sequence, kind=OperationEventKind.RESULT, state=state, result=result)


@pytest.mark.parametrize("state", list(OperationState))
def test_only_success_and_failure_are_terminal(state):
    assert state.terminal == (state in {OperationState.SUCCEEDED, OperationState.FAILED})


def test_old_event_without_observability_extensions_remains_valid():
    old_event = OperationEvent(
        "operation-1",
        1,
        OperationEventKind.STATE,
        OperationState.PREPARING,
        NOW,
        OperationPhase.PREPARATION,
        "Preparing generation",
    )

    assert old_event.transfer is None
    assert old_event.activity is None
    assert old_event.detail is None
    snapshot = advance_operation(None, old_event)
    assert snapshot.last_event is old_event
    assert snapshot.activity is None
    assert snapshot.details == ()
    assert snapshot.last_progress is None
    assert snapshot.last_transfer is None


def test_isolated_activity_updates_current_semantic_work():
    current = advance_operation(None, event())
    observed = activity()

    current = advance_operation(
        current,
        event(sequence=2, kind=OperationEventKind.ACTIVITY, activity=observed),
    )

    assert current.activity is observed
    assert current.last_event.progress is None
    assert current.last_event.transfer is None


@pytest.mark.parametrize(
    ("progress", "transfer"),
    [
        (OperationProgress(1, 3), None),
        (None, TransferProgress(158 * 1024**2)),
        (OperationProgress(1, 3), TransferProgress(158 * 1024**2, 400 * 1024**2)),
    ],
)
def test_activity_coexists_with_logical_and_transfer_observations(progress, transfer):
    current = advance_operation(None, event())
    observed = activity(elapsed_seconds=18.25)
    update = event(
        sequence=2,
        kind=OperationEventKind.ACTIVITY,
        progress=progress,
        transfer=transfer,
        activity=observed,
    )

    current = advance_operation(current, update)

    assert current.last_event.progress is progress
    assert current.last_event.transfer is transfer
    assert current.last_progress is progress
    assert current.last_transfer is transfer
    assert current.activity is observed
    assert current.activity.elapsed_seconds == 18.25


def test_detail_is_typed_sanitized_semantic_history():
    current = advance_operation(None, event())
    observed = activity(action="installed", detail="Package ready", elapsed_seconds=20)

    current = advance_operation(
        current,
        event(sequence=2, kind=OperationEventKind.DETAIL, detail=observed),
    )

    assert current.details == (OperationTimelineEntry(2, OperationPhase.PREPARATION, observed),)
    assert {field.name for field in fields(OperationActivity)} == {
        "category",
        "action",
        "item",
        "detail",
        "elapsed_seconds",
    }
    assert set(OperationActivityCategory) == {
        OperationActivityCategory.PREPARATION,
        OperationActivityCategory.TRANSFER,
        OperationActivityCategory.ENVIRONMENT,
        OperationActivityCategory.DEPENDENCY,
        OperationActivityCategory.CONFIGURATION,
        OperationActivityCategory.SERVICE,
        OperationActivityCategory.READINESS,
        OperationActivityCategory.MODEL,
        OperationActivityCategory.WORKFLOW,
    }


@pytest.mark.parametrize("elapsed", [0, 0.0, 18, 18.25])
def test_activity_accepts_elapsed_already_measured_by_monotonic_producer(elapsed):
    assert activity(elapsed_seconds=elapsed).elapsed_seconds == elapsed


@pytest.mark.parametrize(
    "elapsed",
    [-1, float("nan"), float("inf"), float("-inf"), True, "18"],
)
def test_activity_rejects_invalid_elapsed(elapsed):
    with pytest.raises(ValueError, match="elapsed"):
        activity(elapsed_seconds=elapsed)


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "dependency"},
        {"action": ""},
        {"action": "pip install torch"},
        {"item": "token\nsecret"},
        {"detail": "\x1b[31mraw output"},
        {"detail": "x" * 241},
    ],
)
def test_activity_rejects_untyped_or_unsafe_shapes(changes):
    with pytest.raises((TypeError, ValueError)):
        activity(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": OperationEventKind.ACTIVITY},
        {"kind": OperationEventKind.DETAIL},
        {"activity": {}},
        {"detail": {}},
        {
            "kind": OperationEventKind.RESULT,
            "state": OperationState.SUCCEEDED,
            "result": OperationResult("operation-1", OperationState.SUCCEEDED),
            "activity": activity(),
        },
    ],
)
def test_event_rejects_ambiguous_activity_and_detail(changes):
    with pytest.raises((TypeError, ValueError)):
        event(**changes)


@pytest.mark.parametrize("kind", [OperationEventKind.ACTIVITY, OperationEventKind.DETAIL])
def test_activity_and_detail_cannot_implicitly_change_lifecycle(kind):
    current = advance_operation(None, event())
    changes = (
        {"activity": activity()} if kind is OperationEventKind.ACTIVITY else {"detail": activity()}
    )
    with pytest.raises(ValueError, match="cannot change"):
        advance_operation(
            current,
            event(
                sequence=2,
                kind=kind,
                state=OperationState.RUNNING,
                phase=OperationPhase.EXECUTION,
                **changes,
            ),
        )


def test_terminal_semantics_clear_success_and_preserve_failure_observations():
    def active_snapshot():
        current = advance_operation(None, event())
        observed = activity()
        current = advance_operation(
            current,
            event(
                sequence=2,
                kind=OperationEventKind.ACTIVITY,
                activity=observed,
                progress=OperationProgress(1, 3),
                transfer=TransferProgress(158 * 1024**2),
            ),
        )
        current = advance_operation(
            current,
            event(sequence=3, kind=OperationEventKind.DETAIL, detail=observed),
        )
        return current, observed

    succeeded, observed = active_snapshot()
    succeeded = advance_operation(succeeded, terminal(sequence=4))
    assert succeeded.activity is None
    assert succeeded.details[-1].detail is observed
    assert succeeded.last_progress == OperationProgress(1, 3)
    assert succeeded.last_transfer == TransferProgress(158 * 1024**2)

    failed, observed = active_snapshot()
    failed = advance_operation(failed, terminal(OperationState.FAILED, sequence=4))
    assert failed.activity is observed
    assert failed.details[-1].detail is observed
    assert failed.last_progress == OperationProgress(1, 3)
    assert failed.last_transfer == TransferProgress(158 * 1024**2)
    assert failed.last_event.result.diagnostics == (FAILURE,)


def test_detail_history_is_bounded_to_last_entries_by_sequence():
    current = advance_operation(None, event())
    for sequence in range(2, DETAIL_HISTORY_LIMIT + 7):
        current = advance_operation(
            current,
            event(
                sequence=sequence,
                kind=OperationEventKind.DETAIL,
                detail=activity(item=f"package-{sequence}", elapsed_seconds=sequence),
            ),
        )

    assert len(current.details) == DETAIL_HISTORY_LIMIT
    assert [entry.sequence for entry in current.details] == list(range(7, DETAIL_HISTORY_LIMIT + 7))


def test_observability_values_are_immutable_and_serialize_without_derived_percentages():
    observed = activity()
    update = event(
        sequence=2,
        kind=OperationEventKind.ACTIVITY,
        progress=OperationProgress(1, 3),
        transfer=TransferProgress(10, 20, 2),
        activity=observed,
        detail=observed,
    )
    serialized = asdict(update)

    assert serialized["activity"]["category"] is OperationActivityCategory.DEPENDENCY
    assert serialized["progress"] == {"completed": 1, "total": 3}
    assert serialized["transfer"]["completed_bytes"] == 10
    assert "percentage" not in serialized
    with pytest.raises(FrozenInstanceError):
        observed.item = "other"


def test_determinate_transfer_derives_fraction_and_eta():
    completed_bytes = int(2.77 * 1024**3)
    total_bytes = int(4.07 * 1024**3)
    bytes_per_second = 24.8 * 1024**2
    transfer = TransferProgress(
        completed_bytes=completed_bytes,
        total_bytes=total_bytes,
        bytes_per_second=bytes_per_second,
    )

    assert transfer.fraction == pytest.approx(completed_bytes / total_bytes)
    assert round(transfer.fraction * 100) == 68
    assert transfer.eta_seconds == pytest.approx((total_bytes - completed_bytes) / bytes_per_second)


def test_transfer_without_total_stays_indeterminate_and_retains_observations():
    transfer = TransferProgress(
        completed_bytes=1_730_000_000,
        bytes_per_second=26_004_684.8,
    )

    assert transfer.completed_bytes == 1_730_000_000
    assert transfer.bytes_per_second == 26_004_684.8
    assert transfer.fraction is None
    assert transfer.eta_seconds is None


@pytest.mark.parametrize("speed", [None, 0, 0.0])
def test_transfer_speed_is_optional_and_zero_has_no_eta(speed):
    transfer = TransferProgress(10, total_bytes=20, bytes_per_second=speed)

    assert transfer.fraction == 0.5
    assert transfer.eta_seconds is None


def test_completed_transfer_has_zero_eta_when_positive_speed_is_known():
    assert TransferProgress(20, 20, 5).eta_seconds == 0


@pytest.mark.parametrize(
    "values",
    [
        {"completed_bytes": -1},
        {"completed_bytes": True},
        {"completed_bytes": 1.5},
        {"completed_bytes": 1, "total_bytes": 0},
        {"completed_bytes": 1, "total_bytes": -1},
        {"completed_bytes": 1, "total_bytes": True},
        {"completed_bytes": 1, "total_bytes": 1.5},
        {"completed_bytes": 2, "total_bytes": 1},
        {"completed_bytes": 1, "bytes_per_second": -1},
        {"completed_bytes": 1, "bytes_per_second": float("nan")},
        {"completed_bytes": 1, "bytes_per_second": float("inf")},
        {"completed_bytes": 1, "bytes_per_second": float("-inf")},
        {"completed_bytes": 1, "bytes_per_second": True},
        {"completed_bytes": 1, "bytes_per_second": "fast"},
    ],
)
def test_transfer_rejects_invalid_invariants(values):
    with pytest.raises(ValueError):
        TransferProgress(**values)


def test_transfer_only_progress_is_independent_from_logical_progress():
    current = advance_operation(None, event())
    update = event(
        sequence=2,
        kind=OperationEventKind.PROGRESS,
        transfer=TransferProgress(10, 20),
    )

    current = advance_operation(current, update)

    assert current.last_event.progress is None
    assert current.last_event.transfer.fraction == 0.5


def test_transfer_serialization_has_only_source_observations():
    update = event(
        sequence=2,
        kind=OperationEventKind.PROGRESS,
        progress=OperationProgress(2, 5),
        transfer=TransferProgress(10, 20, 2),
    )

    serialized = asdict(update)

    assert serialized["progress"] == {"completed": 2, "total": 5}
    assert serialized["transfer"] == {
        "completed_bytes": 10,
        "total_bytes": 20,
        "bytes_per_second": 2,
    }
    assert "percentage" not in serialized["transfer"]
    assert "eta_seconds" not in serialized["transfer"]


def test_transfer_does_not_change_terminal_stream_semantics():
    current = advance_operation(None, event())
    final = replace(terminal(), transfer=TransferProgress(20, 20, 5))

    current = advance_operation(current, final)

    assert current.last_event.transfer.eta_seconds == 0
    with pytest.raises(ValueError, match="already terminal"):
        advance_operation(current, event(sequence=3))


@pytest.mark.parametrize("state", [OperationState.SUCCEEDED, OperationState.FAILED])
def test_terminal_result_is_unique_and_nothing_can_follow_it(state):
    current = advance_operation(None, event())
    final = terminal(state)
    current = advance_operation(current, final)
    assert current.last_event.result is final.result
    for following in (final, replace(final, sequence=3), event(sequence=3)):
        with pytest.raises(ValueError, match="already terminal"):
            advance_operation(current, following)


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": 0},
        {"sequence": True},
        {"operation_id": "../outside"},
        {"timestamp": NOW.replace(tzinfo=None)},
        {"timestamp": NOW.astimezone(timezone(timedelta(hours=1)))},
        {"message": "line\nbreak"},
        {"message": "\x1b[31mred"},
        {"message": "x" * 241},
        {"kind": "state"},
        {"phase": "preparation"},
        {"state": "preparing"},
        {"kind": OperationEventKind.PROGRESS},
        {"state": OperationState.SUCCEEDED},
        {"kind": OperationEventKind.RESULT},
        {"diagnostic": {}},
        {"progress": {}},
    ],
)
def test_event_rejects_invalid_or_ambiguous_shapes(changes):
    with pytest.raises((ValueError, TypeError)):
        event(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": 2},
        {"state": OperationState.QUEUED},
        {"phase": OperationPhase.SUBMISSION},
        {"kind": OperationEventKind.PROGRESS, "progress": OperationProgress(0)},
    ],
)
def test_stream_has_an_explicit_start(changes):
    with pytest.raises(ValueError, match="first event"):
        advance_operation(None, event(**changes))


def test_order_is_sequence_not_wall_clock_and_phases_are_not_lifecycle_states():
    current = advance_operation(None, event())
    for sequence, phase in enumerate(
        [OperationPhase.RESOURCE_RELEASE, OperationPhase.SERVICE_START], start=2
    ):
        current = advance_operation(
            current,
            event(sequence=sequence, phase=phase, timestamp=NOW - timedelta(seconds=sequence)),
        )
        assert current.last_event.state is OperationState.PREPARING
    current = advance_operation(
        current, event(sequence=4, state=OperationState.RUNNING, phase=OperationPhase.EXECUTION)
    )
    assert current.started_at == NOW
    for bad in (
        event(sequence=4, state=OperationState.RUNNING),
        event(sequence=6, state=OperationState.RUNNING),
        event(sequence=5, state=OperationState.QUEUED),
        event(sequence=5, operation_id="another-operation", state=OperationState.RUNNING),
    ):
        with pytest.raises(ValueError):
            advance_operation(current, bad)


def test_progress_unknown_total_and_phase_changes():
    assert OperationProgress(7).fraction is None
    assert OperationProgress(7, 10).fraction == 0.7
    current = advance_operation(None, event())
    progress = event(sequence=2, kind=OperationEventKind.PROGRESS, progress=OperationProgress(7))
    assert advance_operation(current, progress).last_event.progress.total is None
    with pytest.raises(ValueError, match="cannot change"):
        advance_operation(current, replace(progress, phase=OperationPhase.RESOURCE_RELEASE))


@pytest.mark.parametrize("completed,total", [(-1, None), (True, 10), (0, 0), (11, 10), (1, 2.5)])
def test_progress_does_not_invent_valid_counts(completed, total):
    with pytest.raises(ValueError):
        OperationProgress(completed, total)


@pytest.mark.parametrize(
    "changes",
    [
        {"profile_id": "C:\\stack"},
        {"profile_id": ""},
        {"prompt": "  "},
        {"prompt": None},
        {"workflow": "image_edit"},
        {"workflow": "flux2_klein_t2i"},
        {"width": 0},
        {"height": -1},
        {"steps": 0},
        {"seed": -1},
        {"steps": True},
        {"width": 1.5},
    ],
)
def test_request_validation(changes):
    with pytest.raises(ValueError):
        replace(ImageGenerationRequest("capella", "A small lighthouse"), **changes)


def test_request_uses_existing_profile_selection_and_workflow_parameters():
    from local_ai_stack.profiles import load_profile
    from local_ai_stack.workflows import WorkflowParameters

    root = Path(__file__).resolve().parents[1]
    profile = load_profile(root / "profiles" / "capella.yaml")
    request = ImageGenerationRequest(profile.id, "A small lighthouse")
    assert profile.workflows[request.workflow].manifest_id == "flux2_klein_t2i"
    defaults = WorkflowParameters(prompt=request.prompt)
    for name in ("prompt", "width", "height", "steps", "seed"):
        assert getattr(request, name) == getattr(defaults, name)
    assert {field.name for field in fields(request)} == {
        "profile_id",
        "workflow",
        "prompt",
        "width",
        "height",
        "steps",
        "seed",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"state": OperationState.RUNNING},
        {"state": "succeeded"},
        {"duration_seconds": -1},
        {"duration_seconds": float("nan")},
        {"duration_seconds": float("inf")},
        {"duration_seconds": True},
        {"output_references": ("https://host/image.png",)},
        {"output_references": "image-1"},
        {"diagnostics": (FAILURE,) * 9},
        {"diagnostics": ({},)},
    ],
)
def test_result_limits_and_shape(changes):
    with pytest.raises((ValueError, TypeError)):
        replace(OperationResult("operation-1", OperationState.SUCCEEDED), **changes)


def test_image_result_requires_completed_output_or_failure_diagnostics():
    with pytest.raises(ValueError, match="output reference"):
        ImageGenerationResult("operation-1", OperationState.SUCCEEDED)
    with pytest.raises(ValueError, match="requires a diagnostic"):
        ImageGenerationResult("operation-1", OperationState.FAILED)
    with pytest.raises(ValueError, match="completed outputs"):
        ImageGenerationResult("operation-1", OperationState.FAILED, ("partial-1",), (FAILURE,))
    references = ["image-1"]
    result = ImageGenerationResult("operation-1", OperationState.SUCCEEDED, references)
    references.append("image-2")
    assert result.output_references == ("image-1",)
    with pytest.raises(FrozenInstanceError):
        result.state = OperationState.FAILED
    with pytest.raises(ValueError, match="disagree"):
        replace(terminal(), result=replace(result, operation_id="another-operation"))
    with pytest.raises(ValueError, match="disagree"):
        replace(terminal(), result=terminal(OperationState.FAILED).result)


def test_diagnostic_is_bounded_text_and_an_opaque_reference():
    for changes in ({"message": "x" * 241}, {"reference": "C:\\logs"}, {"code": ""}):
        with pytest.raises(ValueError):
            replace(FAILURE, **changes)


def test_job_receipt_observation_and_failure_are_distinct_from_operation_completion():
    receipt = ImageJobReceipt("controlled-1")
    for state in (ImageJobState.QUEUED, ImageJobState.RUNNING):
        assert ImageJobObservation(receipt, state).progress is None
    completed = ImageJobObservation(
        receipt, ImageJobState.COMPLETED, output_references=("image-1",)
    )
    assert completed.output_references == terminal().result.output_references
    failed = ImageJobObservation(receipt, ImageJobState.FAILED, diagnostic=FAILURE)
    assert failed.diagnostic is FAILURE
    error = ImageJobObservationError(FAILURE)
    assert error.diagnostic is FAILURE
    assert str(error) == FAILURE.message
    with pytest.raises(ValueError):
        ImageJobObservation(receipt, ImageJobState.COMPLETED)
    with pytest.raises(ValueError):
        ImageJobObservation(receipt, ImageJobState.RUNNING, output_references=("partial-1",))
    with pytest.raises(ValueError):
        ImageJobObservation(receipt, ImageJobState.FAILED)


def test_vram_is_single_gpu_unknown_fails_closed_and_capacity_is_not_reserved():
    available = VramSnapshot(NOW, 32768, 24000)
    assert available.used_mib == 8768
    assert available.can_fit(24000)
    assert not available.can_fit(24001)
    for unknown in (VramSnapshot(NOW, None, None), VramSnapshot(NOW, 32768, None)):
        assert unknown.used_mib is None
        assert not unknown.can_fit(1)
    for total, free in ((10, 11), (None, 10), (10, -1), (True, 0)):
        with pytest.raises(ValueError):
            VramSnapshot(NOW, total, free)


def test_service_projection_preserves_domain_health_vocabulary():
    from typing import get_type_hints

    from local_ai_stack.services import ServiceState

    assert set(get_args(get_type_hints(ServiceView)["health"])) == {s.value for s in ServiceState}
    services = ServicesSnapshot(NOW, (ServiceView("comfyui", "UP"),))
    assert services.services[0].health == "UP"
    with pytest.raises(ValueError, match="duplicate"):
        replace(services, services=services.services * 2)


class FakeSubscription:
    def __init__(self, subscribers, token):
        self.subscribers = subscribers
        self.token = token

    def close(self):
        self.subscribers.pop(self.token, None)


class FakeApplication:
    """Serial fake source, not a production scheduler; used without a TUI import."""

    def __init__(self):
        self.current = None
        self.subscribers = {}
        self.observer_errors = []

    def current_operation(self):
        return self.current

    def deliver(self, sink, observation):
        try:
            sink(observation)
        except Exception as error:  # noqa: BLE001 -- exercise observer failure isolation
            self.observer_errors.append(type(error).__name__)

    def subscribe(self, sink):
        token = object()
        self.subscribers[token] = sink
        if self.current is not None:
            self.deliver(sink, self.current.last_event)
        return FakeSubscription(self.subscribers, token)

    def publish(self, observation):
        self.current = advance_operation(self.current, observation)
        for sink in tuple(self.subscribers.values()):
            self.deliver(sink, observation)

    def services_snapshot(self):
        return ServicesSnapshot(NOW, (ServiceView("comfyui", "UP"),))

    def vram_snapshot(self):
        return VramSnapshot(NOW, 32768, 24000)


class FakeImageCoordinator:
    def generate(self, operation_id, request, *, emit=None):
        assert request.profile_id == "capella"
        final = replace(terminal().result, operation_id=operation_id)
        if emit:
            emit(event(operation_id=operation_id))
            emit(replace(terminal(), operation_id=operation_id, result=final))
        return final


def test_one_fake_producer_drives_multiple_consumers_with_snapshot_and_terminal_replay():
    application = FakeApplication()
    coordinator = FakeImageCoordinator()
    assert isinstance(application, ApplicationReadModel)
    assert isinstance(coordinator, ImageGenerationCoordinator)
    assert application.current_operation() is None  # idle is absence, not a state
    ui_events, log_events = [], []
    ui_subscription = application.subscribe(ui_events.append)
    application.subscribe(log_events.append)

    def broken_consumer(observation):
        raise RuntimeError("consumer failed")

    application.subscribe(broken_consumer)
    result = coordinator.generate(
        "operation-1", ImageGenerationRequest("capella", "A lighthouse"), emit=application.publish
    )
    assert ui_events == log_events
    assert [e.sequence for e in ui_events] == [1, 2]
    assert application.current_operation().last_event.result is result
    assert application.observer_errors == ["RuntimeError", "RuntimeError"]
    assert application.services_snapshot().services[0].health == "UP"
    assert application.vram_snapshot().can_fit(20000)
    late_events = []
    application.subscribe(late_events.append)
    assert late_events == [ui_events[-1]]  # replay, not a second terminal event
    ui_subscription.close()
    ui_subscription.close()
    with pytest.raises(ValueError, match="terminal"):
        application.publish(event(sequence=3))
    assert len(log_events) == 2
    application.current = None  # application selects a fresh operation
    application.publish(event(operation_id="operation-2"))
    assert len(ui_events) == 2
    assert log_events[-1].operation_id == "operation-2"


def test_resource_and_observer_fakes_require_no_concrete_coordinator():
    class Resources:
        def snapshot(self):
            return VramSnapshot(NOW, 32768, 1000)

        def release_conflicting_workload(self, *, timeout_seconds):
            return True

        def wait_for_available(self, required_mib, *, timeout_seconds):
            return self.snapshot()  # acknowledged unload is not proof of free VRAM

    class Observer:
        def observe(self, receipt, *, timeout_seconds):
            return ImageJobObservation(receipt, ImageJobState.RUNNING)

    resources, observer = Resources(), Observer()
    assert isinstance(resources, ResourceCoordinator)
    assert isinstance(observer, ImageJobObserver)
    assert resources.release_conflicting_workload(timeout_seconds=1)
    assert not resources.wait_for_available(24000, timeout_seconds=1).can_fit(24000)
    assert observer.observe(ImageJobReceipt("job-1"), timeout_seconds=1).progress is None


def test_foundation_has_no_ui_provider_or_transport_dependency():
    root = Path(__file__).resolve().parents[1] / "src" / "local_ai_stack"
    modules = {"operation_contracts", "image_generation_contracts", "application_contracts"}
    stdlib = {
        "__future__",
        "math",
        "re",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
    }
    for name in modules:
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module.split(".")[0] in (modules if node.level else stdlib)
            elif isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in stdlib for alias in node.names)
    # Full event graph consists only of typed values; no arbitrary data/transport bag.
    assert {field.name for field in fields(OperationEvent)} == {
        "operation_id",
        "sequence",
        "kind",
        "state",
        "timestamp",
        "phase",
        "message",
        "progress",
        "diagnostic",
        "result",
        "transfer",
        "activity",
        "detail",
    }
    assert {field.name for field in fields(OperationActivity)} == {
        "category",
        "action",
        "item",
        "detail",
        "elapsed_seconds",
    }
    assert {field.name for field in fields(OperationTimelineEntry)} == {
        "sequence",
        "phase",
        "detail",
    }
    assert {field.name for field in fields(TransferProgress)} == {
        "completed_bytes",
        "total_bytes",
        "bytes_per_second",
    }
    assert {field.name for field in fields(OperationSnapshot)} == {
        "started_at",
        "last_event",
        "activity",
        "details",
        "last_progress",
        "last_transfer",
    }
