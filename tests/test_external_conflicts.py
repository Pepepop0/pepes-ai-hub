"""Unit tests for the external-conflict resolver itself.

No StackApplication, no controlled world: just the session against fake
listener/process/clock seams, so the authority rules are exercised in
isolation from everything that composes them. Integration through
StackApplication/FirstRunCoordinator lives in test_first_run.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.compatibility import CompatibilityReport
from local_ai_stack.components import ComponentId
from local_ai_stack.external_conflict_contracts import (
    ConflictPolicySelection,
    ConflictPreflightDisposition,
    ExternalConflictPolicy,
)
from local_ai_stack.external_conflicts import (
    ALONGSIDE_PORTS,
    ConflictResolutionRequired,
    ExternalConflictSession,
    merged_options,
    terminate_by_pid,
)
from local_ai_stack.integration import (
    IntegrationOptions,
    ManagedPreflightCode,
    ManagedPreflightIssue,
    ManagedPreflightReport,
)
from local_ai_stack.inventory import (
    Listener,
    ListenerReport,
    LookupOutcome,
    ProcessInfo,
    ScanOutcome,
)
from local_ai_stack.profiles import load_profile

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "profiles" / "pepe.yaml")
OLLAMA = str(ComponentId.OLLAMA)
COMFYUI = str(ComponentId.COMFYUI)


def raw_report(
    *issues: ManagedPreflightIssue, external: tuple[str, ...] = ()
) -> ManagedPreflightReport:
    return ManagedPreflightReport(
        endpoints=IntegrationOptions().endpoints_for(PROFILE),
        model_endpoint="http://127.0.0.1:11434",
        model_store=Path("models/ollama"),
        free_bytes=10**12,
        compatibility=CompatibilityReport(PROFILE.id, None, (), ()),
        external_components=external,
        issues=issues,
    )


def alongside_issue(component_id: str) -> ManagedPreflightIssue:
    return ManagedPreflightIssue(
        ManagedPreflightCode.ALONGSIDE_REQUIRED,
        f"{component_id} is external and alongside provisioning is not active",
        component_id=component_id,
    )


def collision_issue(component_id: str, port: int, pid: int) -> ManagedPreflightIssue:
    return ManagedPreflightIssue(
        ManagedPreflightCode.ENDPOINT_COLLISION,
        f"managed endpoint 127.0.0.1:{port} for {component_id} is occupied (pids: {pid})",
        component_id=component_id,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeInventory:
    """Listener/process/terminate seams a test can move by hand."""

    def __init__(self) -> None:
        self.listeners: list[Listener] = []
        self.processes: dict[int, ProcessInfo] = {}
        self.terminated: list[int] = []

    def add(
        self, port: int, pid: int, *, create_time: float = 100.0, exe: str = "ollama.exe"
    ) -> None:
        self.listeners.append(Listener("127.0.0.1", port, pid))
        self.processes[pid] = ProcessInfo(pid, exe, exe, f"{exe} serve", create_time)

    def kill(self, pid: int) -> None:
        self.listeners = [item for item in self.listeners if item.pid != pid]
        self.processes.pop(pid, None)

    def list_listeners(self) -> ListenerReport:
        return ListenerReport(ScanOutcome.OK, tuple(self.listeners))

    def lookup_process(self, pid: int):
        from local_ai_stack.inventory import ProcessLookup

        process = self.processes.get(pid)
        if process is None:
            return ProcessLookup(LookupOutcome.NOT_FOUND, None)
        return ProcessLookup(LookupOutcome.FOUND, process)

    def terminate(self, pid: int, create_time: float) -> bool:
        process = self.processes.get(pid)
        if process is None or process.create_time != create_time:
            return False
        self.terminated.append(pid)
        self.kill(pid)
        return True


def session(inventory: FakeInventory, clock: FakeClock) -> ExternalConflictSession:
    return ExternalConflictSession(
        lookup_process=inventory.lookup_process,
        list_listeners=inventory.list_listeners,
        external_path=lambda service_id: Path(
            "comfyui.exe" if service_id == COMFYUI else "ollama.exe"
        ),
        terminate_process=inventory.terminate,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


# --- observe(): sem conflito real não bloqueia --------------------------------


def test_no_alongside_issue_means_nothing_to_resolve():
    inventory, clock = FakeInventory(), FakeClock()
    report = session(inventory, clock).observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert report is None


def test_an_unrelated_hard_blocker_alone_is_left_to_the_normal_preflight():
    """INCOMPATIBLE, DISK_SPACE etc never turn into a conflict on their own."""
    inventory, clock = FakeInventory(), FakeClock()
    incompatible = ManagedPreflightIssue(ManagedPreflightCode.INCOMPATIBLE, "not enough VRAM")
    report = session(inventory, clock).observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(incompatible),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert report is None


# --- INSTALL_ALONGSIDE ---------------------------------------------------------


def test_a_real_conflict_offers_alongside_with_the_isolated_endpoint():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    report = session(inventory, clock).observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert report is not None
    assert report.disposition is ConflictPreflightDisposition.RESOLUTION_REQUIRED
    conflict = report.conflicts[0]
    assert str(conflict.service_id) == OLLAMA
    assert conflict.endpoint.display == "127.0.0.1:11434"
    assert conflict.managed_endpoint.display == f"127.0.0.1:{ALONGSIDE_PORTS[OLLAMA]}"
    assert conflict.can_install_alongside
    assert ExternalConflictPolicy.INSTALL_ALONGSIDE in report.available_policies
    assert report.recommended_policy is ExternalConflictPolicy.INSTALL_ALONGSIDE


def test_install_alongside_proceeds_without_touching_the_process():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE)
    )
    assert result.status.value == "proceed"
    assert 4242 in inventory.processes
    assert not inventory.terminated


def test_merged_options_claims_the_verified_isolated_endpoint():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    options = merged_options(IntegrationOptions(), report, ExternalConflictPolicy.INSTALL_ALONGSIDE)
    assert options.allows_alongside(OLLAMA)
    assert options.port_overrides[OLLAMA] == ALONGSIDE_PORTS[OLLAMA]


def test_merged_options_for_stop_claims_no_port_override():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    options = merged_options(
        IntegrationOptions(), report, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
    )
    assert options.allows_alongside(OLLAMA)
    assert OLLAMA not in options.port_overrides


# --- STOP_CONFLICTING_EXTERNAL: autoridade estreita ---------------------------


def test_stop_reverifies_identity_and_waits_for_the_port_to_clear():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242, create_time=555.0)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert report.conflicts[0].can_stop_safely

    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(
            report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
        )
    )
    assert result.status.value == "proceed"
    assert inventory.terminated == [4242]
    assert 4242 not in inventory.processes


def test_stop_never_touches_a_process_that_is_not_the_one_it_proved():
    """A pid recycled between observation and resolution refuses instead of guessing."""
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242, create_time=1.0)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    # The observed process is gone; a different one now holds the same pid.
    inventory.processes[4242] = ProcessInfo(
        4242, "unrelated.exe", "unrelated.exe", "unrelated", 999.0
    )

    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(
            report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
        )
    )
    assert result.status.value == "failed"
    assert not inventory.terminated
    assert 4242 in inventory.processes  # the (different) process survives


def test_a_stop_that_never_frees_the_port_fails_without_a_second_attempt():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    # terminate() reports success but the port never actually clears (a stuck
    # listener, or something else immediately took the port).
    calls = {"count": 0}

    def stubborn(pid, create_time):
        calls["count"] += 1
        return True

    backend = ExternalConflictSession(
        lookup_process=inventory.lookup_process,
        list_listeners=inventory.list_listeners,
        external_path=lambda _service_id: Path("ollama.exe"),
        terminate_process=stubborn,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(
            report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
        )
    )
    assert result.status.value == "failed"
    assert "não liberou" in result.message
    assert calls["count"] == 1  # terminate is asked once, never escalated or retried


def test_ambiguous_identity_never_offers_stop_and_refuses_if_asked_anyway():
    """Two listeners on one port: nobody proved which process is which."""
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    inventory.listeners.append(Listener("127.0.0.1", 11434, 9999))
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert not report.conflicts[0].can_stop_safely
    assert ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL not in report.available_policies

    with pytest.raises(ValueError, match="unavailable"):
        backend.resolve_external_conflicts(
            ConflictPolicySelection(
                report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
            )
        )
    assert not inventory.terminated
    assert 4242 in inventory.processes


def test_a_stable_unrelated_executable_on_the_port_is_not_safe_stop_authority():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242, exe="unrelated.exe")
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )

    assert not report.conflicts[0].can_stop_safely
    assert ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL not in report.available_policies
    assert inventory.terminated == []


def test_alongside_rechecks_the_fixed_endpoint_before_accepting_the_policy():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    inventory.add(ALONGSIDE_PORTS[OLLAMA], 5555, exe="unrelated.exe")

    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.INSTALL_ALONGSIDE)
    )

    assert result.status.value == "failed"
    assert "não está mais livre" in result.message
    assert inventory.terminated == []


def test_stop_rechecks_that_the_proved_process_still_owns_the_conflicting_endpoint():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    inventory.listeners.clear()

    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(
            report.preflight_id, ExternalConflictPolicy.STOP_CONFLICTING_EXTERNAL
        )
    )

    assert result.status.value == "failed"
    assert "não ocupa mais" in result.message
    assert inventory.terminated == []
    assert 4242 in inventory.processes


def test_an_unlookupable_pid_is_not_safe_to_stop():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.listeners.append(Listener("127.0.0.1", 11434, 4242))  # no ProcessInfo at all
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert not report.conflicts[0].can_stop_safely


def test_neither_policy_safe_becomes_a_hard_blocker_not_a_silent_conflict():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.listeners.append(Listener("127.0.0.1", 11434, 4242))  # unprovable
    inventory.add(ALONGSIDE_PORTS[OLLAMA], 5555)  # alt endpoint also occupied
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert report.disposition is ConflictPreflightDisposition.BLOCKED
    assert report.available_policies == (ExternalConflictPolicy.ABORT,)
    assert any(item.code == "managed-endpoint-occupied" for item in report.hard_blockers)


# --- staleness / reuso -------------------------------------------------------


def test_a_stale_preflight_id_is_refused():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    with pytest.raises(ValueError, match="stale or unknown"):
        backend.resolve_external_conflicts(
            ConflictPolicySelection("not-the-real-id", ExternalConflictPolicy.ABORT)
        )


def test_a_preflight_id_cannot_be_resolved_twice():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    selection = ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.ABORT)
    backend.resolve_external_conflicts(selection)
    with pytest.raises(ValueError, match="already been resolved"):
        backend.resolve_external_conflicts(selection)


def test_abort_touches_nothing():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    result = backend.resolve_external_conflicts(
        ConflictPolicySelection(report.preflight_id, ExternalConflictPolicy.ABORT)
    )
    assert result.status.value == "aborted"
    assert 4242 in inventory.processes
    assert not inventory.terminated


# --- vários conflitos ao mesmo tempo ------------------------------------------


def test_two_conflicting_components_both_get_isolated_endpoints():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242, exe="ollama.exe")
    inventory.add(8188, 4243, exe="comfyui.exe")
    backend = session(inventory, clock)
    report = backend.observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(
            alongside_issue(OLLAMA),
            collision_issue(OLLAMA, 11434, 4242),
            alongside_issue(COMFYUI),
            collision_issue(COMFYUI, 8188, 4243),
        ),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    assert {str(c.service_id) for c in report.conflicts} == {OLLAMA, COMFYUI}
    options = merged_options(IntegrationOptions(), report, ExternalConflictPolicy.INSTALL_ALONGSIDE)
    assert options.port_overrides[OLLAMA] == ALONGSIDE_PORTS[OLLAMA]
    assert options.port_overrides[COMFYUI] == ALONGSIDE_PORTS[COMFYUI]


# --- terminate_by_pid: comportamento real do OS ------------------------------


def test_terminate_by_pid_refuses_a_recycled_pid():
    import os

    # Our own pid is real and alive; a bogus create_time can never match it.
    assert terminate_by_pid(os.getpid(), create_time=-1.0) is False


def test_terminate_by_pid_reports_success_for_a_pid_that_is_already_gone():
    # A pid essentially guaranteed not to exist on any machine.
    assert terminate_by_pid(2**30, create_time=1.0) is True


# --- ConflictResolutionRequired -----------------------------------------------


def test_conflict_resolution_required_carries_the_report():
    inventory, clock = FakeInventory(), FakeClock()
    inventory.add(11434, 4242)
    report = session(inventory, clock).observe(
        profile=PROFILE,
        options=IntegrationOptions(),
        raw_report=raw_report(alongside_issue(OLLAMA), collision_issue(OLLAMA, 11434, 4242)),
        listeners=inventory.list_listeners(),
        managed_pids=frozenset(),
    )
    error = ConflictResolutionRequired(report)
    assert error.report is report
    assert isinstance(error, Exception)
