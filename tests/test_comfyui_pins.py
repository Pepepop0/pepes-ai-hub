"""ComfyUI end to end, against the pins the manifest actually ships.

These are the tests that prove the blocker is closed: with the dependency
decision recorded in the manifest, apply() runs the whole way through and
verify() proves the environment got what was pinned.

Nothing here clones a real repository, creates a real environment, runs pip or
reaches PyPI. Git and the CommandRunner are faked and every path is inside
tmp_path — a real ComfyUI is never installed on the host.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from local_ai_stack.catalog import repository_for
from local_ai_stack.comfyui import ComfyUIProvisioner, provisioner_from_pin
from local_ai_stack.git_ops import GitClient, GitError
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    Ownership,
    ProvisionContext,
    ProvisionOutcome,
)
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

URL = "https://github.com/comfyanonymous/ComfyUI.git"
REVISION = "v0.34.0"
COMMIT = "12d5279438bfefc058a269eae805ceab6047777f"
OTHER_COMMIT = "a" * 40

TORCH_TRIPLE = (
    "torch==2.11.0+cu130",
    "torchvision==0.26.0+cu130",
    "torchaudio==2.11.0+cu130",
)
CU130 = "https://download.pytorch.org/whl/cu130"


class EnvRunner(CommandRunner):
    """Fakes venv creation, pip and importlib.metadata. Runs nothing real."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        pip_ok: bool = True,
        venv_ok: bool = True,
        torch_version: str | None = "2.11.0+cu130",
    ):
        super().__init__()
        self.fake_env = dict(env or {})
        self.pip_ok = pip_ok
        self.venv_ok = venv_ok
        self.torch_version = torch_version
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return f"C:\\bin\\{name}.exe" if name in {"py", "python", "git"} else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.fake_env)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)

        if "venv" in items:
            if not self.venv_ok:
                return CommandResult(argv=items, returncode=1, stderr="venv refused")
            environment = PythonEnvironment(root=Path(items[-1]))
            environment.bin_dir.mkdir(parents=True, exist_ok=True)
            environment.python.write_text("python", encoding="utf-8")
            return CommandResult(argv=items, returncode=0)

        if "pip" in items:
            if not self.pip_ok:
                return CommandResult(argv=items, returncode=1, stderr="pip failed")
            return CommandResult(argv=items, returncode=0)

        if "-c" in items:
            code = items[items.index("-c") + 1]
            if "version_info" in code:
                return CommandResult(argv=items, returncode=0, stdout="3.12\n")
            if "importlib.metadata" in code:
                if self.torch_version is None:
                    return CommandResult(argv=items, returncode=0, stdout="")
                return CommandResult(argv=items, returncode=0, stdout=f"{self.torch_version}\n")
        return CommandResult(argv=items, returncode=0)

    def ran_pip(self) -> bool:
        return any("pip" in call for call in self.calls)


class FakeGit(GitClient):
    """Materializes a checkout instead of contacting a remote."""

    def __init__(self, *, head_commit: str = COMMIT, clone_ok: bool = True):
        super().__init__(EnvRunner())
        self.head_commit = head_commit
        self.clone_ok = clone_ok
        self.clones: list[tuple[str, str, str | None]] = []
        self.repositories: set[Path] = set()

    def available(self) -> bool:
        return True

    def is_repository(self, path: Path) -> bool:
        return Path(path) in self.repositories

    def clone(self, url, destination, *, revision=None, depth=None, dry_run=None, on_output=None):
        if not self.clone_ok:
            raise GitError("clone refused")
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


def context_for(tmp_path: Path, runner: CommandRunner) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, runner=runner)


def with_runner(context: ProvisionContext, runner: CommandRunner) -> ProvisionContext:
    """The same installation, a different runner."""
    return ProvisionContext(installation=context.installation, runner=runner)


def from_manifest(**overrides) -> ComfyUIProvisioner:
    provisioner = provisioner_from_pin(repository_for("comfyui"))
    for key, value in overrides.items():
        setattr(provisioner, key, value)
    return provisioner


# --- o que o manifest fixa -------------------------------------------------


def test_manifest_pins_the_whole_torch_triple():
    dependencies = from_manifest().dependencies
    assert dependencies.requirements == TORCH_TRIPLE
    assert dependencies.extra_index_url == CU130
    assert dependencies.use_repository_requirements is True


def test_no_requirement_floats():
    for requirement in from_manifest().dependencies.requirements:
        assert "==" in requirement
        assert "latest" not in requirement
        assert not any(token in requirement for token in (">", "<", "~", "*"))


def test_the_triple_is_internally_consistent():
    """torchvision 0.26.0 declares torch==2.11.0, and torchaudio tracks torch."""
    versions = dict(r.split("==") for r in from_manifest().dependencies.requirements)
    assert versions["torch"] == versions["torchaudio"] == "2.11.0+cu130"
    assert versions["torchvision"] == "0.26.0+cu130"
    assert all(value.endswith("+cu130") for value in versions.values())


def test_the_cuda_index_is_the_official_pytorch_one():
    assert from_manifest().dependencies.extra_index_url.startswith(
        "https://download.pytorch.org/whl/"
    )


def test_the_interpreter_range_is_recorded():
    provisioner = from_manifest()
    assert provisioner.python_minimum == (3, 11)
    assert provisioner.python_below == (3, 13)


def test_the_component_no_longer_blocks(tmp_path):
    plan = from_manifest(git=FakeGit()).plan(context_for(tmp_path, EnvRunner()))
    assert [step.action for step in plan.steps] == [
        "clone",
        "create-venv",
        "install-requirements",
        "mark-managed",
    ]


# --- apply de ponta a ponta ------------------------------------------------


def test_apply_runs_the_whole_way_through(tmp_path):
    runner = EnvRunner()
    git = FakeGit()
    context = context_for(tmp_path, runner)

    result = from_manifest(git=git).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert result.detection.ownership is Ownership.MANAGED
    assert git.clones == [(URL, str(context.layout.comfyui_app), REVISION)]

    pip_call = next(call for call in runner.calls if "pip" in call)
    for requirement in TORCH_TRIPLE:
        assert requirement in pip_call
    assert "--extra-index-url" in pip_call
    assert CU130 in pip_call
    # The rest still comes from the file the pinned commit froze.
    assert str(context.layout.comfyui_app / "requirements.txt") in pip_call


def test_the_marker_records_the_revision_and_commit(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)
    marker = json.loads((context.layout.comfyui_app / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["component"] == "comfyui"
    assert marker["version"] == REVISION
    assert marker["checksum"] == COMMIT


def test_install_stays_inside_the_install_root(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)
    assert {entry.name for entry in tmp_path.iterdir()} == {"stack"}


def test_nothing_touches_path_or_the_registry(tmp_path):
    runner = EnvRunner()
    from_manifest(git=FakeGit()).apply(context_for(tmp_path, runner))
    flat = [" ".join(call).lower() for call in runner.calls]
    assert not any("setx" in call or "reg add" in call for call in flat)
    assert not any("--user" in call for call in flat)


# --- verify ----------------------------------------------------------------


def test_verify_confirms_the_pinned_torch(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    provisioner = from_manifest(git=FakeGit())
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED

    detection = provisioner.verify(context)
    assert detection.state is ComponentState.INSTALLED
    assert any("torch==2.11.0+cu130" in item for item in detection.evidence)


def test_verify_rejects_a_missing_torch(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)

    detection = from_manifest(git=FakeGit()).verify(
        with_runner(context, EnvRunner(torch_version=None))
    )
    assert detection.state is ComponentState.INVALID
    assert "not installed" in detection.detail


def test_verify_rejects_a_torch_that_is_not_the_pin(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)

    detection = from_manifest(git=FakeGit()).verify(
        with_runner(context, EnvRunner(torch_version="2.9.0+cu126"))
    )
    assert detection.state is ComponentState.INVALID
    assert "2.9.0" in detection.detail
    assert "2.11.0" in detection.detail


@pytest.mark.parametrize("reported", ["2.11.0+cu130", "2.11.0"])
def test_verify_compares_the_release_part_not_the_cuda_tag(tmp_path, reported):
    """A wheel reports 2.11.0+cu130; the pin records the release part."""
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)
    detection = from_manifest(git=FakeGit()).verify(
        with_runner(context, EnvRunner(torch_version=reported))
    )
    assert detection.state is ComponentState.INSTALLED


def test_verify_still_catches_a_moved_checkout(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    from_manifest(git=FakeGit()).apply(context)
    detection = from_manifest(git=FakeGit(head_commit=OTHER_COMMIT)).verify(context)
    assert detection.state is ComponentState.INVALID
    assert "moved outside" in detection.detail


# --- falhas parciais e idempotencia ----------------------------------------


def test_dependency_failure_leaves_no_marker_and_stays_reinstallable(tmp_path):
    context = context_for(tmp_path, EnvRunner(pip_ok=False))
    result = from_manifest(git=FakeGit()).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.COMMAND_FAILED
    assert not (context.layout.comfyui_app / MARKER_NAME).exists()
    # The checkout survives, but is never mistaken for a finished install.
    assert from_manifest().detect(context).state is ComponentState.INVALID

    retried = from_manifest(git=FakeGit()).apply(with_runner(context, EnvRunner()))
    assert retried.outcome is ProvisionOutcome.INSTALLED


def test_a_venv_failure_leaves_no_marker(tmp_path):
    context = context_for(tmp_path, EnvRunner(venv_ok=False))
    result = from_manifest(git=FakeGit()).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.COMMAND_FAILED
    assert not (context.layout.comfyui_app / MARKER_NAME).exists()
    assert not from_manifest().detect(context).is_usable


def test_an_interrupted_install_never_reports_as_installed(tmp_path):
    """Checkout and environment present, marker absent: INVALID, not INSTALLED."""
    context = context_for(tmp_path, EnvRunner())
    directory = context.layout.comfyui_app
    directory.mkdir(parents=True)
    (directory / "main.py").write_text("comfy", encoding="utf-8")
    environment = PythonEnvironment.inside(directory)
    environment.bin_dir.mkdir(parents=True)
    environment.python.write_text("python", encoding="utf-8")

    detection = from_manifest().detect(context)
    assert detection.state is ComponentState.INVALID
    assert detection.ownership is Ownership.MANAGED


def test_apply_is_idempotent(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    provisioner = from_manifest(git=FakeGit())
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED

    second_runner = EnvRunner()
    second = provisioner.apply(with_runner(context, second_runner))
    assert second.outcome is ProvisionOutcome.SKIPPED
    assert not second_runner.ran_pip()


def test_dry_run_with_real_pins_touches_nothing(tmp_path):
    runner = EnvRunner()
    git = FakeGit()
    context = context_for(tmp_path, runner)

    result = from_manifest(git=git).apply(context, dry_run=True)

    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.steps
    assert git.clones == []
    assert not runner.ran_pip()
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_external_is_never_adopted_even_when_fully_pinned(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    desktop = local_appdata / "Comfy-Desktop"
    desktop.mkdir(parents=True)
    (desktop / "keep.txt").write_text("mine", encoding="utf-8")
    runner = EnvRunner(env={"LOCALAPPDATA": str(local_appdata)})
    git = FakeGit()

    result = from_manifest(git=git).apply(context_for(tmp_path, runner))

    assert result.outcome is ProvisionOutcome.SKIPPED
    assert result.detection.ownership is Ownership.EXTERNAL
    assert git.clones == []
    assert not runner.ran_pip()
    assert (desktop / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_moved_tag_aborts_before_the_marker(tmp_path):
    context = context_for(tmp_path, EnvRunner())
    result = from_manifest(git=FakeGit(head_commit=OTHER_COMMIT)).apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert not (context.layout.comfyui_app / MARKER_NAME).exists()
