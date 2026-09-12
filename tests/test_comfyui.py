"""Tests for the ComfyUI provisioner.

Nothing here clones a real repository, creates a real environment, runs pip or
touches an existing ComfyUI. Git and the CommandRunner are faked, and every
path is inside tmp_path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from local_ai_stack.catalog import PinnedRepository
from local_ai_stack.comfyui import (
    COMPONENT_ID,
    ComfyUIProvisioner,
    DependencyPin,
    provisioner_from_pin,
)
from local_ai_stack.git_ops import GitClient, GitError
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME, write_marker
from local_ai_stack.operation_contracts import OperationActivityCategory
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    Ownership,
    ProvisionContext,
    ProvisionEvent,
    ProvisionOutcome,
)
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

URL = "https://github.com/comfyanonymous/ComfyUI.git"
REVISION = "v0.34.0"
COMMIT = "12d5279438bfefc058a269eae805ceab6047777f"
OTHER_COMMIT = "a" * 40

DEPS = DependencyPin(
    requirements=("torch==2.9.0",), extra_index_url="https://example.invalid/cu124"
)


class FakeRunner(CommandRunner):
    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        pip_ok: bool = True,
        pip_lines: Sequence[str] = (),
    ):
        super().__init__()
        self.fake_env = dict(env or {})
        self.pip_ok = pip_ok
        self.pip_lines = tuple(pip_lines)
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return f"C:\\bin\\{name}.exe" if name in {"py", "python", "git"} else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.fake_env)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        if "venv" in items:
            environment = PythonEnvironment(root=Path(items[-1]))
            environment.bin_dir.mkdir(parents=True, exist_ok=True)
            environment.python.write_text("python", encoding="utf-8")
            return CommandResult(argv=items, returncode=0)
        if "pip" in items:
            on_output = kwargs.get("on_output")
            if on_output is not None:
                for line in self.pip_lines:
                    on_output("stdout", line)
            code = 0 if self.pip_ok else 1
            return CommandResult(argv=items, returncode=code, stderr="" if self.pip_ok else "boom")
        if "-c" in items and "version_info" in items[items.index("-c") + 1]:
            return CommandResult(argv=items, returncode=0, stdout="3.12\n")
        return CommandResult(argv=items, returncode=0)

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(call) for call in self.calls)


class FakeGit(GitClient):
    """Materializes a checkout instead of contacting a remote."""

    def __init__(
        self,
        *,
        head_commit: str = COMMIT,
        available: bool = True,
        clone_ok: bool = True,
        clone_lines: Sequence[str] = (),
    ):
        super().__init__(FakeRunner())
        self.head_commit = head_commit
        self._available = available
        self.clone_ok = clone_ok
        self.clone_lines = tuple(clone_lines)
        self.clones: list[tuple[str, str, str | None]] = []
        self.repositories: set[Path] = set()

    def available(self) -> bool:
        return self._available

    def is_repository(self, path: Path) -> bool:
        return Path(path) in self.repositories

    def clone(self, url, destination, *, revision=None, depth=None, dry_run=None, on_output=None):
        if not self.clone_ok:
            raise GitError("clone refused")
        if on_output is not None:
            for line in self.clone_lines:
                on_output("stderr", line)
        target = Path(destination)
        self.clones.append((url, str(target), revision))
        target.mkdir(parents=True, exist_ok=True)
        (target / "main.py").write_text("comfy", encoding="utf-8")
        (target / "requirements.txt").write_text("torch\n", encoding="utf-8")
        self.repositories.add(target)
        return CommandResult(argv=("git", "clone"), returncode=0)

    def checkout(self, path, revision, *, allow_dirty=False, dry_run=None):
        return CommandResult(argv=("git", "checkout"), returncode=0)

    def head(self, path: Path) -> str:
        return self.head_commit


def context_for(
    tmp_path: Path,
    runner: CommandRunner | None = None,
    *,
    events=None,
    monotonic=None,
) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    kwargs = {} if monotonic is None else {"monotonic": monotonic}
    return ProvisionContext(
        installation=resolved, runner=runner or FakeRunner(), events=events, **kwargs
    )


def pinned(**kwargs) -> ComfyUIProvisioner:
    options = {
        "url": URL,
        "revision": REVISION,
        "commit": COMMIT,
        "dependencies": DEPS,
        "git": FakeGit(),
    }
    options.update(kwargs)
    return ComfyUIProvisioner(**options)


def install_managed(context: ProvisionContext, *, marker: bool = True, venv: bool = True):
    directory = context.layout.comfyui_app
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "main.py").write_text("comfy", encoding="utf-8")
    if venv:
        environment = PythonEnvironment.inside(directory)
        environment.bin_dir.mkdir(parents=True, exist_ok=True)
        environment.python.write_text("python", encoding="utf-8")
    if marker:
        write_marker(
            directory, component=COMPONENT_ID, source_id=URL, version=REVISION, checksum=COMMIT
        )


# --- identidade e pin ------------------------------------------------------


def test_component_id_is_canonical():
    assert COMPONENT_ID == "comfyui"


def test_provisioner_from_catalog_pin():
    pin = PinnedRepository(component="comfyui", url=URL, revision=REVISION, commit=COMMIT)
    provisioner = provisioner_from_pin(pin, dependencies=DEPS)
    assert provisioner.is_pinned
    assert provisioner.revision == REVISION
    assert provisioner.commit == COMMIT


def test_provisioner_without_a_pin_is_unpinned():
    assert provisioner_from_pin(None).is_pinned is False


# --- deteccao --------------------------------------------------------------


def test_missing_comfyui_is_reported(tmp_path):
    detection = pinned().detect(context_for(tmp_path))
    assert detection.state is ComponentState.NOT_INSTALLED
    assert detection.ownership is Ownership.NONE


def test_detect_creates_nothing(tmp_path):
    context = context_for(tmp_path)
    pinned().detect(context)
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_external_comfyui_desktop_is_detected_but_not_owned(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    (local_appdata / "Comfy-Desktop").mkdir(parents=True)
    runner = FakeRunner(env={"LOCALAPPDATA": str(local_appdata)})
    detection = pinned().detect(context_for(tmp_path, runner))
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.EXTERNAL
    assert "never modified" in detection.detail


def test_apply_never_takes_ownership_of_an_external_install(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    desktop = local_appdata / "Comfy-Desktop"
    desktop.mkdir(parents=True)
    (desktop / "keep.txt").write_text("mine", encoding="utf-8")
    runner = FakeRunner(env={"LOCALAPPDATA": str(local_appdata)})
    context = context_for(tmp_path, runner)

    git = FakeGit()
    result = pinned(git=git).apply(context)

    assert result.outcome is ProvisionOutcome.SKIPPED
    assert (desktop / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert git.clones == []
    assert not context.layout.comfyui_app.exists()


def test_managed_install_can_coexist_when_explicitly_requested(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    desktop = local_appdata / "Comfy-Desktop"
    desktop.mkdir(parents=True)
    sentinel = desktop / "keep.txt"
    sentinel.write_text("mine", encoding="utf-8")
    runner = FakeRunner(env={"LOCALAPPDATA": str(local_appdata)})
    context = context_for(tmp_path, runner)

    result = pinned(
        git=FakeGit(),
        install_alongside_external=True,
    ).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert sentinel.read_text(encoding="utf-8") == "mine"
    assert context.layout.comfyui_app.is_dir()


def test_managed_install_is_detected(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = pinned().detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.MANAGED
    assert detection.version == REVISION


def test_checkout_without_a_marker_is_invalid(tmp_path):
    context = context_for(tmp_path)
    install_managed(context, marker=False)
    detection = pinned().detect(context)
    assert detection.state is ComponentState.INVALID
    assert "interrupted" in detection.detail


def test_marker_without_the_environment_is_invalid(tmp_path):
    context = context_for(tmp_path)
    install_managed(context, venv=False)
    assert pinned().detect(context).state is ComponentState.INVALID


# --- verify ----------------------------------------------------------------


def test_verify_accepts_the_pinned_commit(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    assert pinned(git=FakeGit(head_commit=COMMIT)).verify(context).is_usable


def test_verify_reports_a_checkout_moved_off_the_pin(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = pinned(git=FakeGit(head_commit=OTHER_COMMIT)).verify(context)
    assert detection.state is ComponentState.INVALID
    assert "moved outside" in detection.detail


# --- plano e recusa --------------------------------------------------------


def test_plan_describes_clone_venv_requirements_and_marker(tmp_path):
    plan = pinned().plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == [
        "clone",
        "create-venv",
        "install-requirements",
        "mark-managed",
    ]


def test_plan_without_a_revision_is_blocked(tmp_path):
    plan = ComfyUIProvisioner(dependencies=DEPS).plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == ["blocked"]
    assert "revision" in plan.steps[0].summary


def test_plan_without_a_dependency_pin_is_blocked(tmp_path):
    plan = pinned(dependencies=DependencyPin()).plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == ["blocked"]
    assert "torch" in plan.steps[0].summary


def test_unpinned_dependencies_refuse_before_cloning_anything(tmp_path):
    """The refusal happens first, so no half checkout is left behind."""
    git = FakeGit()
    context = context_for(tmp_path)
    result = pinned(dependencies=DependencyPin(), git=git).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert git.clones == []
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_default_registry_ships_comfyui_fully_pinned():
    from local_ai_stack.integration import default_provisioners

    provisioner = default_provisioners()["comfyui"]
    assert provisioner.is_pinned
    assert provisioner.revision == REVISION
    assert provisioner.commit == COMMIT
    # The dependency decision now comes from the manifest, so apply() no longer
    # refuses: this is the component's blocker being closed.
    assert not provisioner.dependencies.is_empty


def test_missing_git_refuses(tmp_path):
    context = context_for(tmp_path)
    result = pinned(git=FakeGit(available=False)).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "Git" in result.failure.message


def test_dry_run_clones_nothing_and_creates_nothing(tmp_path):
    git = FakeGit()
    runner = FakeRunner()
    context = context_for(tmp_path, runner)
    result = pinned(git=git).apply(context, dry_run=True)
    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.steps
    assert git.clones == []
    assert not runner.ran("pip")
    assert not context.layout.root.exists()


# --- instalacao ------------------------------------------------------------


def test_apply_clones_the_pinned_revision_and_marks(tmp_path):
    git = FakeGit()
    runner = FakeRunner()
    context = context_for(tmp_path, runner)

    result = pinned(git=git, dependencies=DEPS).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert git.clones == [(URL, str(context.layout.comfyui_app), REVISION)]
    marker = json.loads((context.layout.comfyui_app / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["component"] == "comfyui"
    assert marker["version"] == REVISION
    assert marker["checksum"] == COMMIT


def test_apply_installs_the_declared_dependencies(tmp_path):
    runner = FakeRunner()
    pinned(git=FakeGit(), dependencies=DEPS).apply(context_for(tmp_path, runner))
    pip_calls = [c for c in runner.calls if "pip" in c]
    assert pip_calls
    call = pip_calls[0]
    assert "torch==2.9.0" in call
    assert "--extra-index-url" in call
    assert "https://example.invalid/cu124" in call


def test_repository_requirements_are_opt_in(tmp_path):
    runner = FakeRunner()
    dependencies = DependencyPin(use_repository_requirements=True)
    context = context_for(tmp_path, runner)
    pinned(git=FakeGit(), dependencies=dependencies).apply(context)
    call = next(c for c in runner.calls if "pip" in c)
    assert "-r" in call
    assert str(context.layout.comfyui_app / "requirements.txt") in call


def test_apply_is_idempotent(tmp_path):
    context = context_for(tmp_path)
    provisioner = pinned(git=FakeGit())
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED
    assert provisioner.apply(context).outcome is ProvisionOutcome.SKIPPED


def test_a_moved_tag_aborts_without_a_marker(tmp_path):
    context = context_for(tmp_path)
    result = pinned(git=FakeGit(head_commit=OTHER_COMMIT)).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "moved upstream" in result.failure.detail
    assert not (context.layout.comfyui_app / MARKER_NAME).exists()


def test_failed_dependency_install_leaves_no_marker(tmp_path):
    context = context_for(tmp_path, FakeRunner(pip_ok=False))
    result = pinned(git=FakeGit()).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (context.layout.comfyui_app / MARKER_NAME).exists()
    assert pinned().detect(context).state is not ComponentState.INSTALLED


def test_failed_clone_is_reported_structurally(tmp_path):
    context = context_for(tmp_path)
    result = pinned(git=FakeGit(clone_ok=False)).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.COMMAND_FAILED


def test_install_stays_inside_the_install_root(tmp_path):
    context = context_for(tmp_path)
    pinned(git=FakeGit()).apply(context)
    assert {entry.name for entry in tmp_path.iterdir()} == {"stack"}


# --- smoke real, somente leitura -------------------------------------------


def test_real_machine_detection_is_read_only(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved, runner=CommandRunner())
    detection = ComfyUIProvisioner().detect(context)
    assert detection.ownership in (Ownership.NONE, Ownership.EXTERNAL)


# --- observabilidade --------------------------------------------------------


def test_a_full_install_publishes_activity_for_every_real_step(tmp_path):
    events: list[ProvisionEvent] = []
    runner = FakeRunner()
    context = context_for(tmp_path, runner, events=events.append)

    result = pinned(git=FakeGit(), dependencies=DEPS).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    activities = [e for e in events if e.activity is not None and not e.milestone]
    actions = [a.activity.action for a in activities]
    assert actions[:3] == ["checking", "locating", "preparing"]
    assert "cloning" in actions
    assert "creating" in actions
    assert "validating" in actions
    assert "installing" in actions
    assert "verifying" in actions
    milestones = [e.activity.action for e in events if e.milestone]
    assert "ready" in milestones
    assert "installed" in milestones


def test_pip_output_is_parsed_into_dependency_activity_and_transfer(tmp_path):
    events: list[ProvisionEvent] = []
    runner = FakeRunner(
        pip_lines=(
            "Collecting torch==2.9.0",
            "  Downloading torch-2.9.0-cp311.whl (10.0 MB)",
            "     5.0/10.0 MB 2.0 MB/s eta 0:00:02",
            "     10.0/10.0 MB 2.0 MB/s eta 0:00:00",
            "Installing collected packages: torch",
            "Successfully installed torch-2.9.0",
        )
    )
    context = context_for(tmp_path, runner, events=events.append)

    result = pinned(git=FakeGit(), dependencies=DEPS).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    downloading = [
        e
        for e in events
        if e.activity is not None and e.activity.action == "downloading" and e.transfer is not None
    ]
    assert downloading
    assert downloading[0].activity.item == "torch"
    assert downloading[-1].transfer.completed_bytes == 10_000_000
    assert downloading[-1].transfer.total_bytes == 10_000_000
    installed = [e for e in events if e.milestone and e.activity.action == "installed"]
    assert any(e.activity.item == "torch" for e in installed)


def test_git_clone_progress_is_parsed_into_transfer_activity(tmp_path):
    events: list[ProvisionEvent] = []
    git = FakeGit(
        clone_lines=(
            "Receiving objects:  50% (500/1000)",
            "Receiving objects: 100% (1000/1000), done.",
        )
    )
    context = context_for(tmp_path, FakeRunner(), events=events.append)

    result = pinned(git=git, dependencies=DEPS).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    cloning = [e for e in events if e.activity is not None and e.activity.action == "receiving"]
    assert cloning
    assert cloning[0].activity.category is OperationActivityCategory.TRANSFER
    assert "500/1000" in cloning[0].activity.detail
    assert "1000/1000" in cloning[-1].activity.detail


def test_a_skipped_apply_publishes_no_installation_activity(tmp_path):
    """Idempotence: a second, unchanged apply shows checking, never installing."""
    context = context_for(tmp_path)
    provisioner = pinned(git=FakeGit())
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED

    events: list[ProvisionEvent] = []
    context.events = events.append
    assert provisioner.apply(context).outcome is ProvisionOutcome.SKIPPED

    actions = [e.activity.action for e in events if e.activity is not None]
    assert actions == ["checking"]
    assert "installing" not in actions
    assert "cloning" not in actions
    assert "installed" not in actions


def test_a_failed_dependency_install_still_reports_activity_up_to_the_failure(tmp_path):
    events: list[ProvisionEvent] = []
    context = context_for(tmp_path, FakeRunner(pip_ok=False), events=events.append)

    result = pinned(git=FakeGit()).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    actions = [e.activity.action for e in events if e.activity is not None]
    assert "installing" in actions
    # The install never reached these steps, so nothing invented them.
    assert "finalizing" not in actions
    assert "installed" not in actions


def test_activity_and_detail_carry_no_absolute_path_or_full_command(tmp_path):
    events: list[ProvisionEvent] = []
    context = context_for(tmp_path, FakeRunner(), events=events.append)

    pinned(git=FakeGit(), dependencies=DEPS).apply(context)

    for event in events:
        if event.activity is None:
            continue
        assert ":\\" not in event.activity.item
        assert ":\\" not in (event.activity.detail or "")
        assert "--extra-index-url" not in event.activity.item


def test_elapsed_seconds_on_pip_activity_is_monotonic_and_deterministic(tmp_path):
    class FakeClock:
        def __init__(self) -> None:
            self.now = 100.0

        def __call__(self) -> float:
            self.now += 1.0
            return self.now

    events: list[ProvisionEvent] = []
    runner = FakeRunner(pip_lines=("Collecting torch==2.9.0",))
    context = context_for(tmp_path, runner, events=events.append, monotonic=FakeClock())

    pinned(git=FakeGit(), dependencies=DEPS).apply(context)

    resolving = next(
        e for e in events if e.activity is not None and e.activity.action == "resolving"
    )
    assert resolving.activity.elapsed_seconds is not None
    assert resolving.activity.elapsed_seconds >= 0
