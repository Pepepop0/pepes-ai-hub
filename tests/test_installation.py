"""Tests for install location resolution.

Nothing here installs, downloads or writes outside tmp_path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from local_ai_stack.installation import (
    PORTABLE_DIR_NAME,
    InstallLocation,
    InstallLocationError,
    InstallLocationMode,
    ResolvedInstallation,
    detect_repo_root,
    resolve_install_root,
    resolve_installation,
)
from local_ai_stack.layout import APP_NAME, ENV_ROOT, StackLayout, default_root

REPO = Path(__file__).resolve().parents[1]

# --- modos -----------------------------------------------------------------


def test_three_modes_exist():
    assert {str(mode) for mode in InstallLocationMode} == {"default", "portable", "custom"}


def test_default_and_portable_carry_no_root():
    assert InstallLocation.default().root is None
    assert InstallLocation.portable().root is None


def test_custom_requires_a_root():
    with pytest.raises(InstallLocationError):
        InstallLocation(mode=InstallLocationMode.CUSTOM)


@pytest.mark.parametrize("mode", [InstallLocationMode.DEFAULT, InstallLocationMode.PORTABLE])
def test_non_custom_modes_reject_an_explicit_root(mode, tmp_path):
    with pytest.raises(InstallLocationError):
        InstallLocation(mode=mode, root=tmp_path)


def test_default_location_is_the_default_mode():
    assert InstallLocation().mode is InstallLocationMode.DEFAULT


def test_location_is_immutable():
    location = InstallLocation.default()
    with pytest.raises(AttributeError):
        location.mode = InstallLocationMode.CUSTOM


# --- DEFAULT resolution ----------------------------------------------------


def test_default_resolution_uses_the_application_data_root():
    root = resolve_install_root(InstallLocation.default(), env={})
    assert root == default_root(env={})
    assert root.name == APP_NAME
    assert root.is_absolute()


@pytest.mark.skipif(os.name != "nt", reason="target layout is Windows")
def test_default_resolution_is_under_localappdata():
    root = resolve_install_root(InstallLocation.default(), env={})
    assert root == Path(os.environ["LOCALAPPDATA"]) / APP_NAME


def test_default_resolution_honours_the_env_override(tmp_path):
    root = resolve_install_root(InstallLocation.default(), env={ENV_ROOT: str(tmp_path / "D_AI")})
    assert root == tmp_path / "D_AI"


def test_env_override_does_not_leak_into_portable_or_custom(tmp_path):
    env = {ENV_ROOT: str(tmp_path / "ignored")}
    portable = resolve_install_root(
        InstallLocation.portable(), repo_root=tmp_path / "repo", env=env
    )
    custom = resolve_install_root(InstallLocation.custom(tmp_path / "chosen"), env=env)
    assert portable == tmp_path / "repo" / PORTABLE_DIR_NAME
    assert custom == tmp_path / "chosen"


# --- PORTABLE resolution ---------------------------------------------------


def test_portable_resolution_lands_inside_the_workspace(tmp_path):
    root = resolve_install_root(InstallLocation.portable(), repo_root=tmp_path)
    assert root == tmp_path / "stack"


def test_portable_resolution_detects_this_repository():
    root = resolve_install_root(InstallLocation.portable())
    assert root == REPO / PORTABLE_DIR_NAME


def test_portable_is_relocatable(tmp_path):
    """Same location, moved workspace, new root. No stored absolute path."""
    location = InstallLocation.portable()
    first = tmp_path / "before"
    second = tmp_path / "after with spaces"
    assert resolve_install_root(location, repo_root=first) == first / "stack"
    assert resolve_install_root(location, repo_root=second) == second / "stack"
    assert location.root is None
    assert location.is_relocatable


def test_portable_persists_no_absolute_path():
    assert InstallLocation.portable().to_dict() == {"mode": "portable", "root": None}


def test_portable_rejects_a_relative_workspace(tmp_path):
    with pytest.raises(InstallLocationError):
        resolve_install_root(InstallLocation.portable(), repo_root="relative/workspace")


def test_detect_repo_root_finds_markers(tmp_path):
    workspace = tmp_path / "checkout"
    (workspace / "src" / "pkg").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_repo_root(workspace / "src" / "pkg") == workspace


def test_detect_repo_root_accepts_a_worktree_git_file(tmp_path):
    workspace = tmp_path / "worktree"
    (workspace / "src").mkdir(parents=True)
    (workspace / ".git").write_text("gitdir: ../real/.git", encoding="utf-8")
    assert detect_repo_root(workspace / "src") == workspace


def test_detect_repo_root_without_marker_fails_clearly(tmp_path):
    lonely = tmp_path / "nowhere"
    lonely.mkdir()
    with pytest.raises(InstallLocationError) as exc:
        detect_repo_root(lonely)
    assert "portable" in str(exc.value).lower()


# --- CUSTOM resolution -----------------------------------------------------


def test_custom_resolution_uses_the_given_path(tmp_path):
    target = tmp_path / "dedicated ssd"
    assert resolve_install_root(InstallLocation.custom(target)) == target


def test_custom_rejects_relative_paths():
    with pytest.raises(InstallLocationError) as exc:
        InstallLocation.custom("IA/Pepito")
    assert "absolute" in str(exc.value)


def test_custom_expands_user_and_normalizes(tmp_path):
    assert "~" not in str(InstallLocation.custom("~/PepitoStack").root)
    assert InstallLocation.custom(tmp_path / "a" / ".." / "b").root == tmp_path / "b"


def test_custom_is_not_relocatable(tmp_path):
    assert InstallLocation.custom(tmp_path).is_relocatable is False


# --- espacos e apostrofos --------------------------------------------------


@pytest.mark.parametrize("folder", ["IA's", "Pepito's", "com espaco", "IA's Pepito's", "ção"])
def test_custom_paths_with_spaces_and_apostrophes(tmp_path, folder):
    location = InstallLocation.custom(tmp_path / folder)
    resolved = resolve_installation(location)
    assert folder in str(resolved.root)
    assert resolved.layout.comfyui_data == resolved.root / "data" / "comfyui"


def test_windows_style_custom_path_with_apostrophes(tmp_path):
    location = InstallLocation.custom(tmp_path / "IA's" / "Pepito's")
    assert location.root == tmp_path / "IA's" / "Pepito's"


@pytest.mark.parametrize("folder", ["IA's", "com espaco"])
def test_portable_paths_with_spaces_and_apostrophes(tmp_path, folder):
    root = resolve_install_root(InstallLocation.portable(), repo_root=tmp_path / folder)
    assert root == tmp_path / folder / "stack"


# --- sem side effects ------------------------------------------------------


def test_resolution_creates_no_directories(tmp_path):
    target = tmp_path / "never" / "created"
    resolved = resolve_installation(InstallLocation.custom(target))
    assert resolved.root == target
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_portable_resolution_creates_no_directories(tmp_path):
    resolve_installation(InstallLocation.portable(), repo_root=tmp_path)
    assert not (tmp_path / "stack").exists()


def test_default_resolution_creates_no_directories(tmp_path):
    root = tmp_path / "untouched"
    resolve_installation(InstallLocation.default(), env={ENV_ROOT: str(root)})
    assert not root.exists()


def test_resolving_this_repository_portable_root_creates_nothing():
    before = (REPO / PORTABLE_DIR_NAME).exists()
    resolve_installation(InstallLocation.portable())
    assert (REPO / PORTABLE_DIR_NAME).exists() is before


# --- StackLayout recebe o root resolvido -----------------------------------


def test_stack_layout_is_built_from_the_resolved_root(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "AI"))
    assert isinstance(resolved, ResolvedInstallation)
    assert isinstance(resolved.layout, StackLayout)
    assert resolved.layout.root == resolved.root
    assert resolved.layout == StackLayout.from_root(tmp_path / "AI")


def test_every_mode_produces_the_same_internal_layout_shape(tmp_path):
    roots = [
        resolve_installation(InstallLocation.default(), env={ENV_ROOT: str(tmp_path / "d")}),
        resolve_installation(InstallLocation.portable(), repo_root=tmp_path / "repo"),
        resolve_installation(InstallLocation.custom(tmp_path / "c")),
    ]
    shapes = {
        tuple(
            path.relative_to(resolved.root).as_posix() for path in resolved.layout.all_directories
        )
        for resolved in roots
    }
    assert len(shapes) == 1


def test_stack_layout_stays_free_of_install_mode_semantics():
    """The layout receives a root. It never learns how that root was chosen."""
    import inspect

    import local_ai_stack.layout as layout_module

    source = Path(layout_module.__file__).read_text(encoding="utf-8")
    for upstream in ("from .installation", "from .stacks", "from .profiles"):
        assert upstream not in source
    exported = set(vars(layout_module))
    assert not exported & {"InstallLocation", "InstallLocationMode", "resolve_install_root"}
    assert list(inspect.signature(StackLayout.from_root).parameters) == ["root"]


def test_dependency_direction_is_installation_to_layout():
    import local_ai_stack.installation as installation_module

    source = Path(installation_module.__file__).read_text(encoding="utf-8")
    assert "from .layout import" in source


def test_resolved_installation_exposes_the_mode(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path))
    assert resolved.mode is InstallLocationMode.CUSTOM


# --- serializacao ----------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [InstallLocation.default(), InstallLocation.portable()],
)
def test_location_round_trip_without_root(location):
    assert InstallLocation.from_dict(location.to_dict()) == location


def test_custom_location_round_trip(tmp_path):
    location = InstallLocation.custom(tmp_path / "AI's")
    assert InstallLocation.from_dict(location.to_dict()) == location


def test_from_dict_rejects_unknown_mode():
    with pytest.raises(InstallLocationError):
        InstallLocation.from_dict({"mode": "usb", "root": None})


def test_from_dict_rejects_unknown_fields():
    with pytest.raises(InstallLocationError):
        InstallLocation.from_dict({"mode": "default", "root": None, "extra": 1})


def test_from_dict_rejects_relative_custom_root():
    with pytest.raises(InstallLocationError):
        InstallLocation.from_dict({"mode": "custom", "root": "../../windows"})


def test_from_dict_rejects_root_on_portable(tmp_path):
    with pytest.raises(InstallLocationError):
        InstallLocation.from_dict({"mode": "portable", "root": str(tmp_path)})


# --- .gitignore ------------------------------------------------------------


def test_portable_stack_directory_is_git_ignored():
    for candidate in ("stack/", "stack/apps/ollama", "stack/state/config.yaml"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", candidate],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{candidate} is not git-ignored"


def test_source_tree_is_not_ignored_by_the_portable_rule():
    result = subprocess.run(
        ["git", "check-ignore", "-q", "src/local_ai_stack/stacks.py"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
