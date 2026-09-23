"""Which installation the Hub's commands reach, while a selection is being made.

The Hub binds one backend at a time. Preparing another selection -- another
profile, another folder -- must not move that binding: until a switch commits,
the installation the Dashboard shows is the installation Start, Stop and
Restart reach. Cancelling at the conflict preflight, failing validation and
failing to install all leave the previous one in place.

Everything here runs against stub backends in a temporary directory; no real
root is touched.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_app import reference_facts

from local_ai_stack.external_conflict_contracts import (
    ConflictHardBlocker,
    ConflictPreflightReport,
)
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocationMode, resolve_installation
from local_ai_stack.installations import read_state
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.tui.application import (
    ConflictResolutionRequired,
    HubError,
    InstallSelection,
    SwitchRequiresStop,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RecordingBackend:
    """A backend that remembers which commands reached which root."""

    def __init__(self, installation, world: World) -> None:
        self.installation = installation
        self.world = world
        self.root = installation.root
        self.commands: list[str] = []

    # --- read model ---------------------------------------------------------

    def status(self, profile_id=None):
        return SimpleNamespace(
            installation_ready=self.root in self.world.installed,
            stack_up=self.root in self.world.running,
            observed_at=datetime.now(UTC),
            components=(),
            gpu=None,
        )

    def facts(self):
        return reference_facts()

    def current_operation(self):
        return None

    def check_conflicts(self, profile_id):
        if self.root in self.world.conflicting:
            return ConflictPreflightReport(
                f"preflight-{self.root.name}",
                hard_blockers=(ConflictHardBlocker("blocked", "A controlled blocker."),),
            )
        return None

    # --- commands -----------------------------------------------------------

    def first_run(self, operation_id, profile_id, *, emit=None):
        self.commands.append("first_run")
        self.world.calls.append((self.root, "first_run", profile_id))
        if self.root in self.world.failing:
            return SimpleNamespace(state=OperationState.FAILED, diagnostics=())
        self.world.installed.add(self.root)
        # An installed stack writes its own configuration, which is what makes
        # the folder recognisable as an installation later.
        self.world.write_config(self.installation, profile_id)
        return SimpleNamespace(state=OperationState.SUCCEEDED, diagnostics=())

    def _command(self, name, operation_id, profile_id, *, emit=None):
        self.commands.append(name)
        self.world.calls.append((self.root, name, profile_id))
        return SimpleNamespace(state=OperationState.SUCCEEDED, diagnostics=())

    def start_stack(self, operation_id, profile_id, *, emit=None):
        return self._command("start_stack", operation_id, profile_id, emit=emit)

    def stop_stack(self, operation_id, profile_id, *, emit=None):
        self.commands.append("stop_stack")
        self.world.calls.append((self.root, "stop_stack", profile_id))
        if self.root in self.world.refuse_stop:
            return SimpleNamespace(state=OperationState.FAILED, diagnostics=())
        if self.root in self.world.leaves_running:
            # Succeeded, and yet a managed process is still alive.
            return SimpleNamespace(
                state=OperationState.SUCCEEDED,
                diagnostics=(),
                left_running=(4242,),
            )
        self.world.running.discard(self.root)
        return SimpleNamespace(state=OperationState.SUCCEEDED, diagnostics=(), left_running=())

    def listening_ports(self):
        """Which of this installation's ports still answer, as the controller reports."""
        occupied = self.root in self.world.running or self.root in self.world.keeps_ports
        return (11434, 8080, 8188) if occupied else ()

    def restart_stack(self, operation_id, profile_id, *, emit=None):
        return self._command("restart_stack", operation_id, profile_id, emit=emit)

    def close(self) -> None:
        pass


class World:
    """What the stub backends agree about: which roots install, fail or conflict."""

    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []
        self.installed: set[Path] = set()
        self.failing: set[Path] = set()
        self.conflicting: set[Path] = set()
        self.running: set[Path] = set()
        self.refuse_stop: set[Path] = set()
        # A stop can report success and still leave something behind, which is
        # exactly what must not let a switch through.
        self.leaves_running: set[Path] = set()
        self.keeps_ports: set[Path] = set()
        self.backends: dict[Path, RecordingBackend] = {}

    def backend(self, installation) -> RecordingBackend:
        backend = RecordingBackend(installation, self)
        self.backends[installation.root] = backend
        return backend

    def commands_on(self, root: Path) -> list[str]:
        return [name for where, name, _ in self.calls if where == root]

    def write_config(self, installation, profile_id: str) -> None:
        from local_ai_stack.config import InstalledConfig, config_path
        from local_ai_stack.installations import write_installed_config

        write_installed_config(
            config_path(installation.layout),
            InstalledConfig(profile_id, installation=installation.location),
        )


@pytest.fixture
def world() -> World:
    return World()


def hub(tmp_path: Path, world: World):
    def resolve(location):
        return resolve_installation(
            location, repo_root=tmp_path / "repo", env={ENV_ROOT: str(tmp_path / "default")}
        )

    return create_hub_application(PROJECT_ROOT, resolve=resolve, backend_factory=world.backend)


def custom(root: Path, profile: str = "pepe") -> InstallSelection:
    return InstallSelection(profile, InstallLocationMode.CUSTOM, str(root))


def install(adapter, selection: InstallSelection) -> None:
    adapter.install(selection)
    adapter.wait(10)


# --- a first installation is the one that commits --------------------------------


def test_a_completed_installation_becomes_the_active_one(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        assert adapter.active_root() == first
        assert adapter.installation().ready
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first) == ["first_run", "start_stack"]
    finally:
        adapter.close()


# --- preparing another selection never moves the binding -------------------------


def test_a_conflict_cancelled_leaves_the_previous_installation_bound(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.conflicting.add(second)

        with pytest.raises(ConflictResolutionRequired):
            adapter.install(custom(second, "pepe-8gb"))

        # Cancelling is simply not proceeding: nothing else is called.
        assert adapter.active_root() == first
        assert adapter.active_profile() == "pepe"
        for command in ("start_stack", "stop_stack", "restart_stack"):
            getattr(adapter, command)()
            adapter.wait(10)
        assert world.commands_on(second) == []
        assert world.commands_on(first) == [
            "first_run",
            "start_stack",
            "stop_stack",
            "restart_stack",
        ]
    finally:
        adapter.close()


def test_a_selection_that_cannot_be_prepared_leaves_the_previous_installation_bound(
    tmp_path, world
):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        with pytest.raises(HubError):
            adapter.install(InstallSelection("nothing-like-this", InstallLocationMode.DEFAULT))
        assert adapter.active_root() == first
        adapter.restart_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "restart_stack"
    finally:
        adapter.close()


def test_an_installation_that_fails_leaves_the_previous_one_active(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.failing.add(second)

        install(adapter, custom(second, "pepe-8gb"))

        assert world.commands_on(second) == ["first_run"], "the attempt ran"
        assert adapter.active_root() == first, "but the previous installation is still the truth"
        assert adapter.active_profile() == "pepe"
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(second) == ["first_run"]
        assert world.commands_on(first)[-1] == "start_stack"
    finally:
        adapter.close()


def test_the_active_pointer_is_persisted_only_once_an_installation_completes(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    selection = tmp_path / "default" / "state" / "hub-selection.yaml"
    adapter = hub(tmp_path, world)
    try:
        world.failing.add(second)
        install(adapter, custom(first))
        persisted = read_state(selection)
        assert persisted.active_root == first and persisted.active.profile_id == "pepe"

        install(adapter, custom(second, "pepe-8gb"))

        # The failed attempt is remembered -- as a place that was tried, and as
        # what happened there -- without ever becoming the active installation.
        persisted = read_state(selection)
        assert persisted.active_root == first and persisted.active.profile_id == "pepe"
        attempted = persisted.entry_for(second)
        assert attempted is not None and attempted.last_failure is not None
        assert persisted.entry_for(first).last_install.result == "succeeded"
    finally:
        adapter.close()

    restarted = hub(tmp_path, world)
    try:
        assert restarted.active_root() == first
    finally:
        restarted.close()


def test_a_failed_attempt_is_still_remembered_as_a_place_that_was_tried(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.failing.add(second)
        install(adapter, custom(second, "pepe-8gb"))
        assert str(second) in adapter.recent_custom_roots()
    finally:
        adapter.close()


def test_the_backend_of_a_cancelled_selection_is_never_reused_for_the_next_command(tmp_path, world):
    """The stale-binding bug: a cancelled candidate must not answer later commands."""
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        world.conflicting.add(second)
        with pytest.raises(ConflictResolutionRequired):
            adapter.install(custom(second, "pepe-8gb"))
        assert adapter.installation().profile_id == "pepe"
        projections = adapter.projections()
        assert projections[0].profile_id == "pepe"
        assert projections[0].root_display is None or str(first) in projections[0].root_display
    finally:
        adapter.close()


# --- the installations the Hub knows -------------------------------------------------


def test_every_installation_that_was_installed_is_remembered_and_the_active_one_leads(
    tmp_path, world
):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))

        rows = adapter.installation_summaries()
        assert [row.root for row in rows] == [second, first], "active first, then most recent"
        assert rows[0].active and not rows[1].active
        assert rows[0].profile_id == "pepe-8gb" and rows[0].profile_name == "Pepinho"
        assert rows[1].profile_id == "pepe" and rows[1].profile_name == "Pepe"
        assert all(row.status in {"ready", "stopped", "running"} for row in rows)
    finally:
        adapter.close()


def test_the_list_survives_a_restart_and_keeps_its_order(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
    finally:
        adapter.close()

    restarted = hub(tmp_path, world)
    try:
        assert restarted.active_root() == second
        assert [row.root for row in restarted.installation_summaries()] == [second, first]
    finally:
        restarted.close()


def test_one_folder_written_three_ways_is_one_installation(tmp_path, world):
    root = tmp_path / "PEPEH"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(root))
        install(adapter, custom(Path(str(root).upper())))
        install(adapter, custom(Path(f"{root}\\")))
        assert len(adapter.installation_summaries()) == 1
    finally:
        adapter.close()


# --- switching ------------------------------------------------------------------------


def test_switching_to_a_stopped_installation_moves_every_command_with_it(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))

        adapter.switch_installation(first)

        assert adapter.active_root() == first and adapter.active_profile() == "pepe"
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "start_stack"
        assert world.commands_on(second) == ["first_run"]
    finally:
        adapter.close()


def test_a_running_installation_is_never_stopped_just_because_another_was_chosen(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)

        with pytest.raises(SwitchRequiresStop) as raised:
            adapter.switch_installation(first)

        assert raised.value.plan.must_stop and raised.value.plan.running_profile == "pepe-8gb"
        assert adapter.active_root() == second, "nothing moved"
        assert "stop_stack" not in world.commands_on(second), "and nothing was stopped"
    finally:
        adapter.close()


def test_stop_and_switch_stops_the_one_in_use_and_then_binds_the_other(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)

        adapter.stop_and_switch(first)
        adapter.wait(10)

        assert world.commands_on(second)[-1] == "stop_stack"
        assert adapter.active_root() == first
        adapter.restart_stack()
        adapter.wait(10)
        assert world.commands_on(first)[-1] == "restart_stack"
    finally:
        adapter.close()


def test_a_switch_whose_stop_fails_keeps_the_installation_that_is_running(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        world.running.add(second)
        world.refuse_stop.add(second)

        adapter.stop_and_switch(first)
        adapter.wait(10)

        assert adapter.active_root() == second and adapter.active_profile() == "pepe-8gb"
        adapter.start_stack()
        adapter.wait(10)
        assert world.commands_on(first) == ["first_run"]
    finally:
        adapter.close()


# --- a folder that is not there any more ----------------------------------------------


def test_an_installation_whose_folder_is_gone_stays_in_the_list_as_unavailable(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        shutil.rmtree(first)

        row = next(row for row in adapter.installation_summaries() if row.root == first)
        assert row.missing and not row.usable
        assert "não está disponível" in row.detail

        plan = adapter.plan_switch(first)
        assert not plan.ok and "não está disponível" in plan.problems[0]
        with pytest.raises(HubError):
            adapter.switch_installation(first)
        assert adapter.active_root() == second
    finally:
        adapter.close()


def test_a_folder_that_holds_no_installation_is_refused_in_plain_words(tmp_path, world):
    first, elsewhere = tmp_path / "first", tmp_path / "pictures"
    elsewhere.mkdir()
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        with pytest.raises(HubError) as raised:
            adapter.open_installation(elsewhere)
        assert "não contém uma instalação" in str(raised.value)
        assert adapter.active_root() == first
        assert [row.root for row in adapter.installation_summaries()] == [first]
    finally:
        adapter.close()


def test_opening_an_installation_the_hub_never_saw_adopts_it(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        adapter.switch_installation(first)
        adapter.forget_installation(second)
        assert [row.root for row in adapter.installation_summaries()] == [first]

        adapter.open_installation(second)

        assert adapter.active_root() == second and adapter.active_profile() == "pepe-8gb"
        assert {row.root for row in adapter.installation_summaries()} == {first, second}
    finally:
        adapter.close()


# --- forgetting is not deleting --------------------------------------------------------


def test_forgetting_removes_the_shortcut_and_not_one_file(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))
        before = sorted(p.name for p in first.rglob("*"))

        adapter.forget_installation(first)

        assert [row.root for row in adapter.installation_summaries()] == [second]
        assert first.exists() and sorted(p.name for p in first.rglob("*")) == before
        assert world.commands_on(first) == ["first_run"], "nothing was stopped"
    finally:
        adapter.close()


def test_the_installation_in_use_is_never_forgotten_by_accident(tmp_path, world):
    first = tmp_path / "first"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        with pytest.raises(HubError) as raised:
            adapter.forget_installation(first)
        assert "instalação em uso" in str(raised.value)
        assert [row.root for row in adapter.installation_summaries()] == [first]
    finally:
        adapter.close()


# --- checking and repairing, in place ---------------------------------------------------


def test_repair_runs_on_the_active_installation_without_asking_anything_again(tmp_path, world):
    first, second = tmp_path / "first", tmp_path / "second"
    adapter = hub(tmp_path, world)
    try:
        install(adapter, custom(first))
        install(adapter, custom(second, "pepe-8gb"))

        adapter.repair()
        adapter.wait(10)

        assert world.commands_on(second) == ["first_run", "first_run"]
        assert world.commands_on(first) == ["first_run"]
        assert adapter.active_root() == second and adapter.active_profile() == "pepe-8gb"
        entry = adapter.state.entry_for(second)
        assert entry.last_repair is not None
    finally:
        adapter.close()
