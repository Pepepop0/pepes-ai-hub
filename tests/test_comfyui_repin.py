"""Re-pinning, and the handoff that must not outrun verification.

Two defects are covered here, both of which used to pass silently:

* applying under a changed pin returned SKIPPED, so a version bump was a no-op;
* a managed install that detect() accepted but verify() rejected was still
  handed to the runtime as launchable.

Nothing here clones, installs or runs anything real.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from local_ai_stack.comfyui import ComfyUIProvisioner, DependencyPin
from local_ai_stack.git_ops import GitClient
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME, read_marker, write_marker
from local_ai_stack.provisioning import (
    ComponentState,
    LaunchAvailability,
    Ownership,
    ProvisionContext,
    ProvisionOutcome,
)
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

URL = "https://github.com/comfyanonymous/ComfyUI.git"

PIN_A = {
    "revision": "v0.34.0",
    "commit": "1" * 40,
    "requirements": ("torch==2.11.0+cu130",),
}
PIN_B = {
    "revision": "v0.35.0",
    "commit": "2" * 40,
    "requirements": ("torch==2.12.0+cu130",),
}


class Runner(CommandRunner):
    """Answers the questions verify() asks. Runs nothing."""

    def __init__(
        self,
        *,
        head_commit: str = PIN_A["commit"],
        torch_version: str | None = "2.11.0+cu130",
        env: Mapping[str, str] | None = None,
    ):
        super().__init__()
        self.head_commit = head_commit
        self.torch_version = torch_version
        self.fake_env = dict(env or {})
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return "/bin/x" if name in {"py", "python", "git"} else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.fake_env)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        if "rev-parse" in items:
            return CommandResult(argv=items, returncode=0, stdout=f"{self.head_commit}\n")
        if "venv" in items:
            environment = PythonEnvironment(root=Path(items[-1]))
            environment.bin_dir.mkdir(parents=True, exist_ok=True)
            environment.python.write_text("python", encoding="utf-8")
            return CommandResult(argv=items, returncode=0)
        if "-c" in items:
            code = items[items.index("-c") + 1]
            if "version_info" in code:
                return CommandResult(argv=items, returncode=0, stdout="3.12\n")
            if "importlib.metadata" in code:
                body = "" if self.torch_version is None else f"{self.torch_version}\n"
                return CommandResult(argv=items, returncode=0, stdout=body)
        return CommandResult(argv=items, returncode=0)

    def ran_pip(self) -> bool:
        return any("pip" in call for call in self.calls)


class Git(GitClient):
    def __init__(self, *, head_commit: str = PIN_A["commit"]):
        super().__init__(Runner())
        self.head_commit = head_commit
        self.clones: list[str] = []
        self.checkouts: list[str] = []
        self.fetches: list[str] = []
        self.repositories: set[Path] = set()

    def available(self) -> bool:
        return True

    def is_repository(self, path: Path) -> bool:
        return Path(path) in self.repositories

    def clone(self, url, destination, *, revision=None, depth=None, dry_run=None, on_output=None):
        target = Path(destination)
        self.clones.append(str(revision))
        target.mkdir(parents=True, exist_ok=True)
        (target / "main.py").write_text("comfy", encoding="utf-8")
        (target / "requirements.txt").write_text("torch\n", encoding="utf-8")
        self.repositories.add(target)
        return CommandResult(argv=("git", "clone"), returncode=0)

    def fetch(self, path, *, remote="origin", allow_dirty=False, dry_run=None):
        self.fetches.append(str(path))
        return CommandResult(argv=("git", "fetch"), returncode=0)

    def checkout(self, path, revision, *, allow_dirty=False, dry_run=None):
        self.checkouts.append(revision)
        return CommandResult(argv=("git", "checkout"), returncode=0)

    def head(self, path: Path) -> str:
        return self.head_commit


def provisioner(pin: dict, *, git: Git | None = None) -> ComfyUIProvisioner:
    return ComfyUIProvisioner(
        url=URL,
        revision=pin["revision"],
        commit=pin["commit"],
        dependencies=DependencyPin(
            requirements=pin["requirements"],
            extra_index_url="https://download.pytorch.org/whl/cu130",
            use_repository_requirements=True,
            verify_distribution="torch",
            verify_version=pin["requirements"][0].split("==")[1].split("+")[0],
        ),
        git=git if git is not None else Git(head_commit=pin["commit"]),
    )


def context_for(tmp_path: Path, runner: CommandRunner) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, runner=runner)


def install(tmp_path: Path, pin: dict) -> tuple[ProvisionContext, ComfyUIProvisioner]:
    """A real managed install under `pin`, through apply()."""
    runner = Runner(head_commit=pin["commit"], torch_version=pin["requirements"][0].split("==")[1])
    context = context_for(tmp_path, runner)
    prov = provisioner(pin, git=Git(head_commit=pin["commit"]))
    result = prov.apply(context)
    assert result.outcome is ProvisionOutcome.INSTALLED
    return context, prov


# --- fingerprint -----------------------------------------------------------


def test_the_fingerprint_is_stable():
    assert provisioner(PIN_A).dependency_fingerprint() == (
        provisioner(PIN_A).dependency_fingerprint()
    )
    assert len(provisioner(PIN_A).dependency_fingerprint()) == 64


@pytest.mark.parametrize(
    "field, value",
    [
        ("revision", "v9.9.9"),
        ("commit", "f" * 40),
        ("url", "https://example.invalid/other.git"),
    ],
)
def test_identity_changes_change_the_fingerprint(field, value):
    base = provisioner(PIN_A)
    changed = provisioner(PIN_A)
    setattr(changed, field, value)
    assert changed.dependency_fingerprint() != base.dependency_fingerprint()


def test_requirement_changes_change_the_fingerprint():
    assert (
        provisioner(PIN_B).dependency_fingerprint() != provisioner(PIN_A).dependency_fingerprint()
    )


def test_the_cuda_channel_is_part_of_the_fingerprint():
    base = provisioner(PIN_A)
    moved = provisioner(PIN_A)
    moved.dependencies = DependencyPin(
        requirements=base.dependencies.requirements,
        extra_index_url="https://download.pytorch.org/whl/cu128",
        use_repository_requirements=True,
    )
    assert moved.dependency_fingerprint() != base.dependency_fingerprint()


def test_the_python_requirement_is_part_of_the_fingerprint():
    base = provisioner(PIN_A)
    widened = provisioner(PIN_A)
    widened.python_below = (3, 14)
    assert widened.dependency_fingerprint() != base.dependency_fingerprint()


def test_reordering_requirements_is_not_a_new_pin():
    one = provisioner(PIN_A)
    one.dependencies = DependencyPin(requirements=("a==1", "b==2"))
    other = provisioner(PIN_A)
    other.dependencies = DependencyPin(requirements=("b==2", "a==1"))
    assert one.dependency_fingerprint() == other.dependency_fingerprint()


def test_the_marker_records_the_fingerprint(tmp_path):
    context, prov = install(tmp_path, PIN_A)
    marker = json.loads((context.layout.comfyui_app / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["fingerprint"] == prov.dependency_fingerprint()


# --- re-pin ----------------------------------------------------------------


def test_the_same_pin_is_still_idempotent(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    runner = Runner(head_commit=PIN_A["commit"])
    again = provisioner(PIN_A, git=Git(head_commit=PIN_A["commit"]))
    result = again.apply(ProvisionContext(installation=context.installation, runner=runner))
    assert result.outcome is ProvisionOutcome.SKIPPED
    assert not runner.ran_pip()


def test_a_repin_is_never_reported_as_skipped(tmp_path):
    """Install under pin A, then apply under pin B."""
    context, _ = install(tmp_path, PIN_A)

    runner = Runner(head_commit=PIN_B["commit"], torch_version="2.12.0+cu130")
    git = Git(head_commit=PIN_B["commit"])
    git.repositories.add(context.layout.comfyui_app)
    result = provisioner(PIN_B, git=git).apply(
        ProvisionContext(installation=context.installation, runner=runner)
    )

    assert result.outcome is not ProvisionOutcome.SKIPPED
    assert result.outcome is ProvisionOutcome.INSTALLED
    assert runner.ran_pip()
    assert "torch==2.12.0+cu130" in next(c for c in runner.calls if "pip" in c)


def test_a_repin_checks_out_the_new_revision_after_fetching(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    git = Git(head_commit=PIN_B["commit"])
    git.repositories.add(context.layout.comfyui_app)
    runner = Runner(head_commit=PIN_B["commit"], torch_version="2.12.0+cu130")

    provisioner(PIN_B, git=git).apply(
        ProvisionContext(installation=context.installation, runner=runner)
    )

    assert git.fetches, "a re-pin must fetch before checking out a newer revision"
    assert git.checkouts == [PIN_B["revision"]]


def test_a_repin_rewrites_the_marker(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    directory = context.layout.comfyui_app
    before = read_marker(directory, component="comfyui")

    git = Git(head_commit=PIN_B["commit"])
    git.repositories.add(directory)
    runner = Runner(head_commit=PIN_B["commit"], torch_version="2.12.0+cu130")
    new = provisioner(PIN_B, git=git)
    new.apply(ProvisionContext(installation=context.installation, runner=runner))

    after = read_marker(directory, component="comfyui")
    assert after.fingerprint != before.fingerprint
    assert after.fingerprint == new.dependency_fingerprint()
    assert after.version == PIN_B["revision"]


def test_a_repin_makes_the_plan_show_work_again(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    runner = Runner(head_commit=PIN_A["commit"])
    changed = ProvisionContext(installation=context.installation, runner=runner)

    assert provisioner(PIN_A).plan(changed).is_noop
    assert not provisioner(PIN_B).plan(changed).is_noop


def test_a_marker_without_a_fingerprint_is_not_treated_as_current(tmp_path):
    """An install we cannot prove came from this pin is reinstalled."""
    context, prov = install(tmp_path, PIN_A)
    write_marker(
        context.layout.comfyui_app,
        component="comfyui",
        source_id=URL,
        version=PIN_A["revision"],
        checksum=PIN_A["commit"],
    )
    assert prov.matches_pin(context) is False
    assert not prov.plan(context).is_noop


def test_a_broken_environment_under_the_same_pin_is_repaired_not_skipped(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    runner = Runner(head_commit=PIN_A["commit"], torch_version=None)  # torch sumiu
    result = provisioner(PIN_A, git=Git(head_commit=PIN_A["commit"])).apply(
        ProvisionContext(installation=context.installation, runner=runner)
    )
    assert result.outcome is not ProvisionOutcome.SKIPPED
    assert runner.ran_pip()


def test_a_repin_dry_run_still_mutates_nothing(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    directory = context.layout.comfyui_app
    before = (directory / MARKER_NAME).read_text(encoding="utf-8")

    git = Git(head_commit=PIN_B["commit"])
    git.repositories.add(directory)
    runner = Runner(head_commit=PIN_B["commit"])
    result = provisioner(PIN_B, git=git).apply(
        ProvisionContext(installation=context.installation, runner=runner), dry_run=True
    )

    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.steps
    assert not runner.ran_pip()
    assert git.checkouts == [] and git.fetches == []
    assert (directory / MARKER_NAME).read_text(encoding="utf-8") == before


def test_external_is_still_skipped_and_untouched_under_a_repin(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    desktop = local_appdata / "Comfy-Desktop"
    desktop.mkdir(parents=True)
    (desktop / "keep.txt").write_text("mine", encoding="utf-8")

    runner = Runner(env={"LOCALAPPDATA": str(local_appdata)})
    git = Git()
    result = provisioner(PIN_B, git=git).apply(context_for(tmp_path, runner))

    assert result.outcome is ProvisionOutcome.SKIPPED
    assert result.detection.ownership is Ownership.EXTERNAL
    assert git.clones == [] and git.checkouts == []
    assert (desktop / "keep.txt").read_text(encoding="utf-8") == "mine"


# --- handoff nunca ultrapassa verify ---------------------------------------


def test_detect_valid_but_verify_invalid_yields_no_executable(tmp_path):
    """The defect this closes: a broken environment used to look launchable."""
    context, prov = install(tmp_path, PIN_A)

    broken = ProvisionContext(
        installation=context.installation,
        runner=Runner(head_commit=PIN_A["commit"], torch_version=None),
    )
    assert prov.detect(broken).state is ComponentState.INSTALLED  # detect ainda aceita
    assert prov.verify(broken).state is ComponentState.INVALID

    handoff = prov.runtime_handoff(broken)
    assert handoff.availability is LaunchAvailability.UNAVAILABLE
    assert handoff.executable is None
    assert not handoff.launchable


def test_a_moved_checkout_also_yields_no_executable(tmp_path):
    context, _ = install(tmp_path, PIN_A)
    # The checkout moved under us: same files, different HEAD.
    moved = provisioner(PIN_A, git=Git(head_commit="9" * 40))
    assert moved.detect(context).state is ComponentState.INSTALLED
    assert moved.verify(context).state is ComponentState.INVALID

    handoff = moved.runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.UNAVAILABLE
    assert handoff.executable is None


def test_a_verified_install_is_still_handed_over(tmp_path):
    context, prov = install(tmp_path, PIN_A)
    handoff = prov.runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.AVAILABLE
    assert handoff.launchable
    assert handoff.executable == prov.environment(context).python


def test_external_is_still_reported_as_external(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    (local_appdata / "Comfy-Desktop").mkdir(parents=True)
    context = context_for(tmp_path, Runner(env={"LOCALAPPDATA": str(local_appdata)}))
    handoff = provisioner(PIN_A).runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.EXTERNAL
    assert handoff.executable is None
