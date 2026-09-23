"""Every operation the Hub launches ends exactly once, and ends the same way.

Whatever happens inside -- the backend fails, the disk refuses the commit, the
machine is not clear for the installation being switched to -- the Hub is idle
again afterwards, the candidate is gone, the installation in use is still the
one bound, shown and persisted, and the screens get one terminal event, after
all of that is already true.

The backends here are the stub from ``test_installation_switching``, which
answers only through methods the real ``StackApplication`` has, with the
production result types; the contract test at the end keeps it that way.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from test_installation_switching import RecordingBackend, World, custom, hub, install

from local_ai_stack import installations as installations_module
from local_ai_stack.external_conflict_contracts import (
    ConflictPolicySelection,
    ExternalConflictPolicy,
)
from local_ai_stack.installations import read_state, root_key
from local_ai_stack.operation_contracts import OperationResult, OperationState
from local_ai_stack.stack_application import StackApplication
from local_ai_stack.tui.application import ConflictResolutionRequired, HubError


class RefusesActivating:
    """A disk that takes every write except the one that would make ``target`` active."""

    def __init__(self, real, target: Path) -> None:
        self.real = real
        self.target = target
        self.refused = 0

    def __call__(self, path, state) -> None:
        active = state.active_root
        if active is not None and root_key(active) == root_key(self.target):
            self.refused += 1
            raise OSError("the state file could not be written")
        self.real(path, state)


@pytest.fixture
def world() -> World:
    return World()


def refuse_activating(monkeypatch, target: Path) -> RefusesActivating:
    refuses = RefusesActivating(installations_module.write_state, target)
    monkeypatch.setattr("local_ai_stack.hub_application.write_state", refuses)
    return refuses


class Endings:
    """A subscriber that records every event, and what the Hub looked like at the end."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.events = []
        self.at_ending: list[dict] = []

    def __call__(self, event) -> None:
        self.events.append(event)
        if event.state.terminal:
            adapter = self.adapter
            self.at_ending.append(
                {
                    "busy": adapter._busy,
                    "idle": adapter._idle.is_set(),
                    "candidate": adapter._candidate,
                    "active": adapter.active_root(),
                }
            )

    def terminal(self):
        return [event for event in self.events if event.state.terminal]

    def reset(self) -> None:
        """Forget the replay a new subscription gets of the previous operation."""
        self.events.clear()
        self.at_ending.clear()


def assert_idle_and_unchanged(adapter, root: Path) -> None:
    """The Hub is free again and every record of the installation in use agrees."""
    assert adapter._candidate is None, "no candidate outlives its attempt"
    assert adapter._busy is False
    assert adapter._idle.is_set()
    assert adapter.active_root() == root
    assert adapter.config.installation.root == root
    assert adapter.backend.root == root
    assert adapter.state.active_root == root
    assert read_state(adapter.selection_path).active_root == root


# --- 1, 11: an asynchronous commit the disk refuses ------------------------------------


def test_an_installation_whose_commit_the_disk_refuses_ends_and_frees_the_hub(
    tmp_path, world, monkeypatch
):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        endings = Endings(adapter)
        adapter.subscribe(endings)
        endings.reset()
        refused = refuse_activating(monkeypatch, second)

        adapter.install(custom(second, "pepe-8gb"))
        adapter.wait(10)

        assert refused.refused == 1, "the backend succeeded and the commit was attempted"
        assert_idle_and_unchanged(adapter, first)
        # One ending, and it says the installation was not adopted.
        [ending] = endings.terminal()
        assert ending.state is OperationState.FAILED
        assert "Não foi possível salvar" in ending.message
        # By the time the screens heard it, the Hub was already settled.
        assert endings.at_ending == [
            {"busy": False, "idle": True, "candidate": None, "active": first}
        ]

        # 11: a new operation starts normally afterwards.
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "start_stack"
        monkeypatch.undo()
        install(adapter, custom(second, "pepe-8gb"))
        assert adapter.active_root() == second
    finally:
        adapter.close()


def test_a_stop_and_switch_whose_commit_the_disk_refuses_ends_and_frees_the_hub(
    tmp_path, world, monkeypatch
):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(second, "pepe-8gb"))
        install(adapter, custom(first))
        world.running.add(first)
        endings = Endings(adapter)
        adapter.subscribe(endings)
        endings.reset()
        refuse_activating(monkeypatch, second)

        adapter.stop_and_switch(second)
        adapter.wait(10)

        assert_idle_and_unchanged(adapter, first)
        [ending] = endings.terminal()
        assert ending.state is OperationState.FAILED
        assert adapter.last_switch_refusal() is not None
        # First was stopped and stays selected, stopped: that is accepted.
        assert first not in world.running

        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "start_stack"
    finally:
        adapter.close()


def test_a_backend_that_raises_still_ends_the_operation(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        endings = Endings(adapter)
        adapter.subscribe(endings)
        endings.reset()

        def explode(*args, **kwargs):
            raise RuntimeError("the backend fell over")

        adapter.backend.start_stack = explode
        adapter.start_stack()
        adapter.wait(10)

        assert adapter._busy is False and adapter._idle.is_set()
        [ending] = endings.terminal()
        assert ending.state is OperationState.FAILED
        adapter.stop_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "stop_stack"
    finally:
        adapter.close()


def test_a_subscriber_that_raises_does_not_keep_the_hub_busy(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))

        def broken(event):
            raise RuntimeError("the screen fell over")

        adapter.subscribe(broken)
        adapter.start_stack()
        adapter.wait(10)
        assert adapter._busy is False and adapter._idle.is_set()
        adapter.stop_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-2:] == ["start_stack", "stop_stack"]
    finally:
        adapter.close()


def test_every_launched_operation_has_exactly_one_ending(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        endings = Endings(adapter)
        adapter.subscribe(endings)
        endings.reset()
        for command in (adapter.start_stack, adapter.stop_stack, adapter.restart_stack):
            command()
            adapter.wait(10)
        adapter.repair()
        adapter.wait(10)
        assert len(endings.terminal()) == 4
        assert all(item["busy"] is False for item in endings.at_ending)
    finally:
        adapter.close()


# --- 2, 3: a candidate lives only as long as its attempt ---------------------------------


def test_aborting_a_conflict_lets_go_of_the_candidate(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.conflicting.add(second)
        with pytest.raises(ConflictResolutionRequired) as raised:
            adapter.install(custom(second, "pepe-8gb"))
        assert adapter._candidate is not None, "a decision is owed about it"

        result = adapter.resolve_external_conflicts(
            ConflictPolicySelection(raised.value.report.preflight_id, ExternalConflictPolicy.ABORT)
        )

        assert result.status.value == "aborted"
        assert_idle_and_unchanged(adapter, first)
    finally:
        adapter.close()


def test_walking_away_from_a_conflict_lets_go_of_the_candidate(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.conflicting.add(second)
        with pytest.raises(ConflictResolutionRequired):
            adapter.install(custom(second, "pepe-8gb"))

        adapter.cancel_pending_installation()

        assert_idle_and_unchanged(adapter, first)
        assert adapter.current_conflict_preflight() is None or (
            adapter.current_conflict_preflight().preflight_id != f"preflight-{second.name}"
        )
    finally:
        adapter.close()


def test_every_way_an_installation_can_end_lets_go_of_the_candidate(tmp_path, world):
    first, second, third = tmp_path / "first", tmp_path / "second", tmp_path / "third"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        # Validation failure.
        with pytest.raises(HubError):
            adapter.install(custom(second, "no-such-profile"))
        assert adapter._candidate is None
        # Installation failure.
        world.failing.add(third)
        install(adapter, custom(third, "pepe-8gb"))
        assert_idle_and_unchanged(adapter, first)
        # Success.
        install(adapter, custom(second, "pepe-8gb"))
        assert adapter._candidate is None
        assert adapter.active_root() == second
    finally:
        adapter.close()


# --- 7, 8, 9, 10: after the stop, the machine must be clear for the target ---------------


def switching_world(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    install(adapter, custom(second, "pepe-8gb"))
    install(adapter, custom(first))
    world.running.add(first)
    return adapter, first, second


def assert_target_untouched(adapter, world, first: Path, second: Path) -> None:
    """Refused after stopping: first stays selected, second is neither bound nor saved."""
    assert_idle_and_unchanged(adapter, first)
    assert adapter.backend.root != second
    assert world.commands_on(second) == ["first_run"], "nothing reached the target"
    refusal = adapter.last_switch_refusal()
    assert refusal is not None and refusal.startswith(
        "Não foi possível trocar de instalação porque os serviços necessários ainda estão em uso."
    )


def test_a_managed_process_still_alive_after_the_stop_denies_the_switch(tmp_path, world):
    adapter, first, second = switching_world(tmp_path, world)
    try:
        world.survivors.add(first)
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert_target_untouched(adapter, world, first, second)
    finally:
        adapter.close()


def test_a_registry_that_cannot_be_read_denies_the_switch(tmp_path, world):
    adapter, first, second = switching_world(tmp_path, world)
    try:
        world.unreadable_registry.add(first)
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert_target_untouched(adapter, world, first, second)
    finally:
        adapter.close()


def test_an_external_listener_left_on_a_needed_port_denies_the_switch(tmp_path, world):
    adapter, first, second = switching_world(tmp_path, world)
    try:
        # Something that is no installation's takes a port as soon as it is free.
        world.after_stop.append(lambda root: world.external_ports.add(8080))
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert_target_untouched(adapter, world, first, second)
        assert 8080 in world.external_ports, "the external process is left alone"
    finally:
        adapter.close()


def test_a_listener_scan_that_cannot_be_read_denies_the_switch(tmp_path, world):
    adapter, first, second = switching_world(tmp_path, world)
    try:
        world.after_stop.append(lambda root: setattr(world, "scan_unavailable", True))
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert_target_untouched(adapter, world, first, second)
    finally:
        adapter.close()


def test_a_stop_that_fails_denies_the_switch(tmp_path, world):
    adapter, first, second = switching_world(tmp_path, world)
    try:
        world.refuse_stop.add(first)
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert_idle_and_unchanged(adapter, first)
        assert adapter.backend.root == first
        assert "parar os serviços" in (adapter.last_switch_refusal() or "")
    finally:
        adapter.close()


def test_with_the_endpoints_free_the_switch_goes_through(tmp_path, world):
    adapter, _, second = switching_world(tmp_path, world)
    try:
        adapter.stop_and_switch(second)
        adapter.wait(10)
        assert adapter._candidate is None and adapter._busy is False
        assert adapter.active_root() == second
        assert adapter.backend.root == second
        assert adapter.config.installation.root == second
        assert read_state(adapter.selection_path).active_root == second
        assert adapter.last_switch_refusal() is None
    finally:
        adapter.close()


def test_a_status_nobody_can_read_is_treated_as_running(tmp_path, world):
    """Absence of information is not safety: the switch goes through the stop."""
    from local_ai_stack.tui.application import SwitchRequiresStop

    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(second, "pepe-8gb"))
        install(adapter, custom(first))

        def unreadable(profile_id=None):
            raise RuntimeError("status could not be read")

        adapter.backend.status = unreadable
        with pytest.raises(SwitchRequiresStop):
            adapter.switch_installation(second)
        assert adapter.active_root() == first
    finally:
        adapter.close()


# --- 6: the stub speaks only the real contract ----------------------------------------------


def test_the_stub_backend_speaks_only_the_real_contract():
    """No fake-only seam: every method the stub offers, production has, by the same name."""
    public = [
        name
        for name, member in vars(RecordingBackend).items()
        if callable(member) and not name.startswith("_")
    ]
    for name in public:
        assert hasattr(StackApplication, name), f"StackApplication has no {name}()"
        real = inspect.signature(getattr(StackApplication, name)).parameters
        fake = inspect.signature(getattr(RecordingBackend, name)).parameters
        assert list(fake) == list(real), f"{name}() takes {list(real)}, the stub {list(fake)}"
    for invented in ("listening_ports", "left_running"):
        assert not hasattr(StackApplication, invented)
        assert not hasattr(RecordingBackend, invented)
    assert "left_running" not in {
        field.name for field in OperationResult.__dataclass_fields__.values()
    }
