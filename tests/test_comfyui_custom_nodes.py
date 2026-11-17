"""Pinned ComfyUI custom nodes: parsed from the manifest, checked out, verified.

Nothing here clones, installs or runs anything real. Git and the CommandRunner
are faked and every path is inside tmp_path.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from local_ai_stack import catalog as catalog_module
from local_ai_stack.catalog import CatalogError, PinnedCustomNode
from local_ai_stack.comfyui import (
    CUSTOM_NODE_COMPONENT,
    ComfyUIProvisioner,
    DependencyPin,
)
from local_ai_stack.git_ops import GitClient
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME, read_marker, write_marker
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    ProvisionContext,
    ProvisionOutcome,
)
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

URL = "https://github.com/comfyanonymous/ComfyUI.git"
REVISION = "v0.39.0"
COMMIT = "b0b743566f65daafc423b4fea8a2fbda94b3384a"

NODE = PinnedCustomNode(
    id="comfyui-krea2edit",
    url="https://github.com/lbouaraba/comfyui-krea2edit.git",
    revision="v1.2.5",
    commit="bdfa8b267fdb13730868d435b277dcfe696ec083",
)
# Where a fresh clone lands before the pinned revision is checked out.
DEFAULT_BRANCH_TIP = "0" * 40
NO_DEPENDENCIES = {"pyproject.toml": '[project]\nname = "node"\ndependencies = []\n'}


class Runner(CommandRunner):
    """Fakes venv creation and pip. Runs nothing real."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return f"C:\\bin\\{name}.exe" if name in {"py", "python", "git"} else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {}

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        if "venv" in items:
            environment = PythonEnvironment(root=Path(items[-1]))
            environment.bin_dir.mkdir(parents=True, exist_ok=True)
            environment.python.write_text("python", encoding="utf-8")
        if "-c" in items and "version_info" in items[items.index("-c") + 1]:
            return CommandResult(argv=items, returncode=0, stdout="3.11\n")
        return CommandResult(argv=items, returncode=0)

    def ran_pip(self) -> bool:
        return any("pip" in call for call in self.calls)


class Git(GitClient):
    """A checkout per path, each with its own HEAD.

    A node clone lands on the default branch; checking its revision out moves
    HEAD to ``node_head``, which is the pinned commit unless a test says the
    tag moved.
    """

    def __init__(
        self,
        *,
        node_files: Mapping[str, str] = NO_DEPENDENCIES,
        node_head: str = NODE.commit,
    ) -> None:
        super().__init__(Runner())
        self.node_files = dict(node_files)
        self.node_head = node_head
        self.heads: dict[Path, str] = {}
        self.clones: list[tuple[str, Path]] = []
        self.fetches: list[tuple[Path, tuple[str, ...]]] = []
        self.checkouts: list[tuple[Path, str, tuple[str, ...]]] = []

    def available(self) -> bool:
        return True

    def is_repository(self, path: Path) -> bool:
        return (Path(path) / ".git").is_dir()

    def clone(self, url, destination, *, revision=None, depth=None, dry_run=None, on_output=None):
        target = Path(destination)
        self.clones.append((url, target))
        (target / ".git").mkdir(parents=True, exist_ok=True)
        if url == URL:
            (target / "main.py").write_text("comfy", encoding="utf-8")
            (target / "requirements.txt").write_text("torch\n", encoding="utf-8")
            self.heads[target] = COMMIT
        else:
            for name, text in self.node_files.items():
                (target / name).write_text(text, encoding="utf-8")
            self.heads[target] = DEFAULT_BRANCH_TIP
        return CommandResult(argv=("git", "clone"), returncode=0)

    def fetch(self, path, *, remote="origin", allow_dirty=False, ignore=(), dry_run=None):
        self.fetches.append((Path(path), tuple(ignore)))
        return CommandResult(argv=("git", "fetch"), returncode=0)

    def checkout(self, path, revision, *, allow_dirty=False, ignore=(), dry_run=None):
        target = Path(path)
        self.checkouts.append((target, revision, tuple(ignore)))
        if target.parent.name == "custom_nodes":
            self.heads[target] = self.node_head
        return CommandResult(argv=("git", "checkout"), returncode=0)

    def head(self, path: Path) -> str:
        return self.heads.get(Path(path), DEFAULT_BRANCH_TIP)


def context_for(tmp_path: Path) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, runner=Runner())


def provisioner(
    git: Git | None = None, nodes: tuple[PinnedCustomNode, ...] = (NODE,)
) -> ComfyUIProvisioner:
    return ComfyUIProvisioner(
        url=URL,
        revision=REVISION,
        commit=COMMIT,
        dependencies=DependencyPin(requirements=("torch==2.11.0+cu130",)),
        custom_nodes=nodes,
        git=git if git is not None else Git(),
    )


def node_dir(context: ProvisionContext, node_id: str = NODE.id) -> Path:
    return context.layout.comfyui_app / "custom_nodes" / node_id


def comfy_marker(context: ProvisionContext) -> Path:
    return context.layout.comfyui_app / MARKER_NAME


# --- instalacao ------------------------------------------------------------


def test_a_pinned_node_is_cloned_and_checked_out_at_its_commit(tmp_path):
    context = context_for(tmp_path)
    git = Git()

    result = provisioner(git).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    target = node_dir(context)
    assert (NODE.url, target) in git.clones
    assert (target, NODE.revision) in [(path, rev) for path, rev, _ in git.checkouts]
    assert git.heads[target] == NODE.commit
    marker = read_marker(target, component=CUSTOM_NODE_COMPONENT)
    assert marker is not None
    assert (marker.source_id, marker.version, marker.checksum) == (
        NODE.url,
        NODE.revision,
        NODE.commit,
    )
    assert comfy_marker(context).is_file()


def test_the_node_checkout_ignores_the_hubs_own_marker(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    (_, _, ignore) = next(entry for entry in git.checkouts if entry[0] == node_dir(context))
    assert f"{MARKER_NAME}*" in ignore


def test_the_plan_lists_each_node(tmp_path):
    plan = provisioner().plan(context_for(tmp_path))
    steps = [step for step in plan.steps if step.action == "install-custom-node"]
    assert len(steps) == 1
    assert steps[0].target.endswith(str(Path("custom_nodes") / NODE.id))


def test_a_node_that_does_not_land_on_its_commit_is_refused(tmp_path):
    context = context_for(tmp_path)
    result = provisioner(Git(node_head="f" * 40)).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert NODE.commit[:12] in result.failure.message
    assert not comfy_marker(context).exists()
    assert not context.runner.ran_pip()


# --- dependencias de nodes -------------------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        {"requirements.txt": "numpy>=1.0\n"},
        {"pyproject.toml": '[project]\nname = "node"\ndependencies = ["numpy"]\n'},
    ],
    ids=["requirements.txt", "pyproject"],
)
def test_a_node_declaring_pip_dependencies_is_refused(tmp_path, files):
    context = context_for(tmp_path)
    result = provisioner(Git(node_files=files)).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "dependencies" in result.failure.message
    # The Hub's own half-usable folder goes; ComfyUI is not marked installed.
    assert not node_dir(context).exists()
    assert not comfy_marker(context).exists()


def test_an_empty_requirements_file_declares_nothing(tmp_path):
    files = {**NO_DEPENDENCIES, "requirements.txt": "# none\n\n"}
    result = provisioner(Git(node_files=files)).apply(context_for(tmp_path))
    assert result.outcome is ProvisionOutcome.INSTALLED


# --- pastas que nao sao do Hub ---------------------------------------------


def test_a_users_own_node_is_never_touched(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    mine = node_dir(context, "my-node")
    mine.mkdir()
    (mine / "__init__.py").write_text("mine", encoding="utf-8")

    # A re-pin that drops every node still leaves the user's folder alone.
    result = provisioner(git, nodes=()).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert (mine / "__init__.py").read_text(encoding="utf-8") == "mine"


def test_a_user_folder_with_the_pinned_name_blocks_instead_of_being_replaced(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    theirs = node_dir(context)
    theirs.mkdir(parents=True)
    (theirs / "__init__.py").write_text("theirs", encoding="utf-8")

    result = provisioner(git).apply(context)

    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "not installed by this hub" in result.failure.message
    assert (theirs / "__init__.py").read_text(encoding="utf-8") == "theirs"
    assert not (theirs / MARKER_NAME).exists()
    assert NODE.url not in [url for url, _ in git.clones]


def test_a_node_the_pin_no_longer_lists_is_removed_when_the_hub_installed_it(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    assert node_dir(context).is_dir()

    dropped = provisioner(git, nodes=())
    assert [step.action for step in dropped.plan(context).steps].count("remove-custom-node") == 1
    result = dropped.apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert not node_dir(context).exists()


# --- re-pin e fingerprint --------------------------------------------------


def test_a_node_repin_fetches_and_checks_out_the_new_revision(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)

    moved = replace(NODE, revision="v1.3.0", commit="c" * 40)
    git.node_head = moved.commit
    result = provisioner(git, nodes=(moved,)).apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    target = node_dir(context)
    assert [path for path, _ in git.fetches] == [target]
    assert git.checkouts[-1][:2] == (target, "v1.3.0")
    assert [url for url, _ in git.clones].count(NODE.url) == 1
    marker = read_marker(target, component=CUSTOM_NODE_COMPONENT)
    assert marker is not None and marker.checksum == moved.commit


@pytest.mark.parametrize(
    "change",
    [
        {"commit": "c" * 40},
        {"revision": "v9.9.9"},
        {"url": "https://example.invalid/fork.git"},
        {"id": "renamed"},
    ],
)
def test_a_node_pin_change_changes_the_fingerprint(change):
    base = provisioner().dependency_fingerprint()
    assert provisioner(nodes=(replace(NODE, **change),)).dependency_fingerprint() != base


def test_adding_or_removing_a_node_changes_the_fingerprint():
    assert provisioner(nodes=()).dependency_fingerprint() != provisioner().dependency_fingerprint()


def test_node_order_is_not_a_new_pin():
    other = replace(NODE, id="another")
    assert (
        provisioner(nodes=(NODE, other)).dependency_fingerprint()
        == provisioner(nodes=(other, NODE)).dependency_fingerprint()
    )


def test_a_node_pin_change_makes_the_plan_show_work(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    assert provisioner(git).plan(context).is_noop
    moved = replace(NODE, commit="c" * 40)
    assert not provisioner(git, nodes=(moved,)).plan(context).is_noop


# --- verify ----------------------------------------------------------------


def test_verify_rejects_a_moved_node(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)

    git.heads[node_dir(context)] = "e" * 40
    detection = provisioner(git).verify(context)

    assert detection.state is ComponentState.INVALID
    assert NODE.id in detection.detail


def test_verify_rejects_a_missing_node_and_apply_repairs_it(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    shutil.rmtree(node_dir(context))

    assert provisioner(git).verify(context).state is ComponentState.INVALID
    assert provisioner(git).apply(context).outcome is ProvisionOutcome.INSTALLED
    assert provisioner(git).verify(context).state is ComponentState.INSTALLED


def test_verify_rejects_a_node_folder_the_hub_did_not_write(tmp_path):
    context = context_for(tmp_path)
    git = Git()
    provisioner(git).apply(context)
    (node_dir(context) / MARKER_NAME).unlink()

    detection = provisioner(git).verify(context)
    assert detection.state is ComponentState.INVALID
    assert "not installed by this hub" in detection.detail


def test_a_marker_from_another_component_does_not_claim_a_node(tmp_path):
    context = context_for(tmp_path)
    target = node_dir(context)
    write_marker(target, component="comfyui", source_id=URL)

    result = provisioner().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert json.loads((target / MARKER_NAME).read_text(encoding="utf-8"))["component"] == "comfyui"


# --- manifest --------------------------------------------------------------

BASE = """
schema_version: 1
repositories:
  comfyui:
    url: https://github.com/comfyanonymous/ComfyUI.git
    revision: v0.39.0
    custom_nodes:
{nodes}
"""

GOOD_NODE = """      comfyui-krea2edit:
        url: https://github.com/lbouaraba/comfyui-krea2edit.git
        revision: v1.2.5
        commit: bdfa8b267fdb13730868d435b277dcfe696ec083
"""


@pytest.fixture
def load(tmp_path, monkeypatch):
    def loader(nodes: str):
        directory = tmp_path / "manifests"
        directory.mkdir(exist_ok=True)
        (directory / "repositories.yaml").write_text(
            BASE.format(nodes=nodes.rstrip("\n")), encoding="utf-8"
        )
        monkeypatch.setattr(catalog_module, "manifest_dir", lambda: directory)
        catalog_module.clear_cache()
        return catalog_module.repository_catalog()

    yield loader
    catalog_module.clear_cache()


def test_the_manifest_parses_custom_nodes(load):
    assert load(GOOD_NODE)["comfyui"].custom_nodes == (NODE,)


@pytest.mark.parametrize(
    "nodes, message",
    [
        (GOOD_NODE.replace("https://github.com", "http://github.com"), "https"),
        (GOOD_NODE.replace("bdfa8b267fdb13730868d435b277dcfe696ec083", "bdfa8b2"), "40-character"),
        (
            GOOD_NODE.replace("        commit: bdfa8b267fdb13730868d435b277dcfe696ec083\n", ""),
            "40-character",
        ),
        (GOOD_NODE + "        requirements: [numpy]\n", "unknown fields: requirements"),
        (GOOD_NODE.replace("comfyui-krea2edit:", "../escape:"), "plain folder name"),
        (GOOD_NODE + GOOD_NODE.replace("comfyui-krea2edit:", "ComfyUI-Krea2Edit:"), "duplicate"),
        ("      comfyui-krea2edit: v1.2.5\n", "must be a mapping"),
    ],
    ids=["http", "short-commit", "no-commit", "unknown-key", "bad-id", "case-duplicate", "scalar"],
)
def test_the_manifest_refuses_a_malformed_node(load, nodes, message):
    with pytest.raises(CatalogError, match=message):
        load(nodes)
