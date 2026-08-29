"""Testes do layout de filesystem.

Nada aqui baixa, instala ou toca fora do tmp_path do pytest.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_ai_stack.layout import (
    APP_NAME,
    ENV_ROOT,
    LayoutError,
    StackLayout,
    default_root,
)


def layout_at(base: Path) -> StackLayout:
    return StackLayout.from_root(base / "stack")


# --- layout default --------------------------------------------------------


def test_default_root_ends_with_app_name():
    root = default_root(env={})
    assert root.name == APP_NAME
    assert root.is_absolute()


def test_default_layout_is_rooted_in_user_data_dir():
    layout = StackLayout.default(env={})
    assert layout.root == default_root(env={})
    assert layout.apps == layout.root / "apps"


@pytest.mark.skipif(os.name != "nt", reason="layout alvo do Windows")
def test_default_root_matches_localappdata_on_windows():
    local_appdata = Path(os.environ["LOCALAPPDATA"])
    assert default_root(env={}) == local_appdata / APP_NAME


def test_default_root_never_duplicates_app_name():
    """Se a base ja terminar em LocalAIStack, nao vira LocalAIStack/LocalAIStack."""
    root = default_root(env={})
    parts = [p for p in root.parts if p == APP_NAME]
    assert len(parts) == 1


def test_env_override_wins_over_platformdirs(tmp_path):
    root = default_root(env={ENV_ROOT: str(tmp_path / "D_AI")})
    assert root == tmp_path / "D_AI"


def test_blank_env_override_is_ignored():
    assert default_root(env={ENV_ROOT: "   "}) == default_root(env={})


def test_relative_env_override_is_rejected():
    with pytest.raises(LayoutError):
        default_root(env={ENV_ROOT: "relativo/ai"})


# --- custom root -----------------------------------------------------------


def test_from_root_uses_the_path_verbatim(tmp_path):
    layout = StackLayout.from_root(tmp_path / "AI")
    assert layout.root == tmp_path / "AI"
    # nao acrescenta APP_NAME por conta propria
    assert layout.root.name == "AI"


def test_from_root_accepts_str_and_pathlike(tmp_path):
    assert StackLayout.from_root(str(tmp_path)).root == StackLayout.from_root(tmp_path).root


def test_from_root_expands_user():
    layout = StackLayout.from_root("~/LocalAIStackTest")
    assert layout.root.is_absolute()
    assert "~" not in str(layout.root)


def test_relative_root_is_rejected():
    with pytest.raises(LayoutError) as exc:
        StackLayout.from_root("stack")
    assert "cwd" in str(exc.value)


def test_from_root_normalizes_redundant_segments(tmp_path):
    layout = StackLayout.from_root(tmp_path / "a" / ".." / "b")
    assert layout.root == tmp_path / "b"


# --- paths filhos ----------------------------------------------------------


def test_first_level_directories(tmp_path):
    layout = layout_at(tmp_path)
    root = layout.root
    assert layout.apps == root / "apps"
    assert layout.models == root / "models"
    assert layout.data == root / "data"
    assert layout.cache == root / "cache"
    assert layout.downloads == root / "downloads"
    assert layout.logs == root / "logs"
    assert layout.runtime == root / "runtime"
    assert layout.state == root / "state"


def test_app_directories(tmp_path):
    layout = layout_at(tmp_path)
    assert layout.ollama_app == layout.apps / "ollama"
    assert layout.open_webui_app == layout.apps / "open-webui"
    assert layout.comfyui_app == layout.apps / "comfyui"


def test_data_directories(tmp_path):
    layout = layout_at(tmp_path)
    assert layout.open_webui_data == layout.data / "open-webui"
    assert layout.comfyui_data == layout.data / "comfyui"


def test_every_path_is_a_pathlib_path(tmp_path):
    layout = layout_at(tmp_path)
    assert all(isinstance(p, Path) for p in layout.as_dict().values())
    assert all(isinstance(p, Path) for p in layout.all_directories)


def test_all_directories_lists_parents_before_children(tmp_path):
    directories = layout_at(tmp_path).all_directories
    seen: list[Path] = []
    for path in directories:
        assert all(parent not in directories or parent in seen for parent in path.parents)
        seen.append(path)


def test_as_dict_covers_the_conceptual_names(tmp_path):
    keys = set(layout_at(tmp_path).as_dict())
    assert keys == {
        "root",
        "apps",
        "models",
        "data",
        "cache",
        "downloads",
        "logs",
        "runtime",
        "state",
        "ollama_app",
        "open_webui_app",
        "comfyui_app",
        "open_webui_data",
        "comfyui_data",
    }


def test_layout_is_immutable(tmp_path):
    layout = layout_at(tmp_path)
    with pytest.raises(AttributeError):
        layout.root = tmp_path


def test_layouts_with_same_root_are_equal(tmp_path):
    assert StackLayout.from_root(tmp_path) == StackLayout.from_root(tmp_path)


# --- derivacoes de arquivo -------------------------------------------------


def test_file_helpers_live_under_the_right_directories(tmp_path):
    layout = layout_at(tmp_path)
    assert layout.download_part("flux.safetensors") == layout.downloads / "flux.safetensors.part"
    assert layout.lock_file("ollama") == layout.runtime / "ollama.lock"
    assert layout.pid_file("comfyui") == layout.runtime / "comfyui.pid"
    assert layout.log_file("provisioning") == layout.logs / "provisioning.log"
    assert layout.state_file("install") == layout.state / "install.json"


@pytest.mark.parametrize("bad", ["", "   ", "../escape", "a/b", "a\\b", ".", "..", " nome"])
def test_file_helpers_reject_traversal_and_separators(tmp_path, bad):
    with pytest.raises(LayoutError):
        layout_at(tmp_path).log_file(bad)


def test_file_helpers_allow_inner_spaces(tmp_path):
    layout = layout_at(tmp_path)
    assert layout.log_file("open webui").name == "open webui.log"


# --- construcao sem side effect --------------------------------------------


def test_construction_creates_nothing(tmp_path):
    root = tmp_path / "stack"
    layout = StackLayout.from_root(root)
    _ = layout.as_dict()
    _ = layout.all_directories
    _ = layout.download_part("x")
    assert not root.exists()
    assert list(tmp_path.iterdir()) == []


def test_default_layout_construction_touches_nothing(tmp_path):
    root = tmp_path / "never-created"
    StackLayout.default(env={ENV_ROOT: str(root)})
    assert not root.exists()


def test_dry_run_creates_nothing(tmp_path):
    layout = layout_at(tmp_path)
    would_create = layout.ensure(dry_run=True)
    assert would_create == layout.all_directories
    assert not layout.root.exists()


# --- criacao ---------------------------------------------------------------


def test_ensure_creates_the_whole_tree(tmp_path):
    layout = layout_at(tmp_path)
    created = layout.ensure()
    assert set(created) == set(layout.all_directories)
    for path in layout.all_directories:
        assert path.is_dir()
    assert layout.exists()
    assert layout.missing_directories() == ()


def test_ensure_creates_missing_parents(tmp_path):
    """Root inexistente, varios niveis acima tambem."""
    layout = StackLayout.from_root(tmp_path / "a" / "b" / "c" / "stack")
    layout.ensure()
    assert layout.comfyui_data.is_dir()


def test_ensure_is_idempotent(tmp_path):
    layout = layout_at(tmp_path)
    layout.ensure()
    second = layout.ensure()
    assert second == ()
    assert layout.exists()


def test_ensure_preserves_existing_content(tmp_path):
    layout = layout_at(tmp_path)
    layout.ensure()
    keeper = layout.models / "modelo.safetensors"
    keeper.write_text("nao me apague", encoding="utf-8")
    nested = layout.comfyui_data / "sub"
    nested.mkdir()
    layout.ensure()
    assert keeper.read_text(encoding="utf-8") == "nao me apague"
    assert nested.is_dir()


def test_ensure_with_partially_existing_tree(tmp_path):
    layout = layout_at(tmp_path)
    layout.models.mkdir(parents=True)
    layout.logs.mkdir(parents=True)
    created = layout.ensure()
    assert layout.models not in created
    assert layout.logs not in created
    assert layout.comfyui_app in created
    assert layout.exists()


# --- conflito arquivo vs diretorio -----------------------------------------


def test_file_where_directory_expected_raises_clear_error(tmp_path):
    layout = layout_at(tmp_path)
    layout.root.mkdir(parents=True)
    layout.models.write_text("sou um arquivo", encoding="utf-8")
    with pytest.raises(LayoutError) as exc:
        layout.ensure()
    message = str(exc.value)
    assert str(layout.models) in message
    assert "arquivo" in message


def test_conflict_is_detected_before_anything_is_created(tmp_path):
    layout = layout_at(tmp_path)
    layout.root.mkdir(parents=True)
    layout.apps.write_text("arquivo no lugar de diretorio", encoding="utf-8")
    with pytest.raises(LayoutError):
        layout.ensure()
    assert not layout.models.exists()  # nada meio-criado
    assert layout.apps.is_file()  # nada apagado


def test_conflicts_lists_offending_paths(tmp_path):
    layout = layout_at(tmp_path)
    layout.root.mkdir(parents=True)
    layout.cache.write_text("x", encoding="utf-8")
    assert layout.conflicts() == (layout.cache,)


def test_root_itself_as_file_is_reported(tmp_path):
    root = tmp_path / "stack"
    root.write_text("arquivo", encoding="utf-8")
    layout = StackLayout.from_root(root)
    with pytest.raises(LayoutError):
        layout.ensure()
    assert root.is_file()


# --- paths hostis ----------------------------------------------------------


@pytest.mark.parametrize(
    "folder",
    ["com espaco", "pepito's_ai_hub", "a b's c", "acentuacao ção", "dir.com.ponto"],
)
def test_paths_with_spaces_and_apostrophes(tmp_path, folder):
    layout = StackLayout.from_root(tmp_path / folder / "stack")
    layout.ensure()
    assert layout.comfyui_data.is_dir()
    assert folder in str(layout.root)
    assert layout.ensure() == ()


# --- independencia do cwd --------------------------------------------------


def test_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    layout = layout_at(tmp_path)
    before = layout.as_dict()

    monkeypatch.chdir(outside)
    assert StackLayout.from_root(tmp_path / "stack").as_dict() == before
    assert StackLayout.default(env={ENV_ROOT: str(tmp_path / "x")}).root == tmp_path / "x"


def test_default_root_does_not_depend_on_cwd(tmp_path, monkeypatch):
    before = default_root(env={})
    monkeypatch.chdir(tmp_path)
    assert default_root(env={}) == before


def test_layout_is_not_the_repository_directory(tmp_path):
    """Install dir e repo dir sao coisas distintas."""
    repo = Path(__file__).resolve().parents[1]
    layout = StackLayout.default(env={})
    assert repo not in layout.root.parents
    assert layout.root != repo
