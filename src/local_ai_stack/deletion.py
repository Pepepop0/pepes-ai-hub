"""Deleting an installation's folder, and every reason not to.

Deleting is the one thing the Hub does that cannot be undone, so this module
does two things and nothing else: it says why a folder must not be deleted,
and it deletes a folder it has been told is safe without ever following a
link out of it. Whether the folder is an installation, whether its services
have stopped, and what the list of installations says afterwards are the
adapter's business.

The refusals are deliberately broad. A folder is never deleted when it is --
or contains -- a drive root, the person's home or one of its well-known
folders, a system folder, the Hub's own program or source folder, the file
where the Hub keeps its state, or another installation; nor when a link or
junction anywhere inside could lead the deletion somewhere else.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

from .resources import bundle_directory, checkout_root, resource_root

# The folders under the home that belong to the person, not to an installation.
_HOME_FOLDERS = (
    "Desktop",
    "Documents",
    "Downloads",
    "Pictures",
    "Music",
    "Videos",
    "OneDrive",
    "AppData",
    "AppData/Local",
    "AppData/Roaming",
)
# Environment variables that name system or per-person folders on Windows.
_SYSTEM_VARIABLES = (
    "USERPROFILE",
    "HOMEDRIVE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "PUBLIC",
    "ONEDRIVE",
)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _contains(outer: Path, inner: Path) -> bool:
    """Whether ``inner`` is ``outer`` or somewhere below it."""
    outer_key, inner_key = _key(outer), _key(inner)
    return inner_key == outer_key or inner_key.startswith(outer_key.rstrip("\\/") + os.sep)


def protected_folders() -> tuple[Path, ...]:
    """Folders a deletion must never be, or contain."""
    home = Path.home()
    folders: list[Path] = [home, *(home / name for name in _HOME_FOLDERS)]
    for variable in _SYSTEM_VARIABLES:
        value = os.environ.get(variable, "").strip()
        if value:
            folders.append(Path(value))
    # The Hub itself: its data, its source checkout, its executable's folder.
    folders.append(resource_root())
    folders.append(checkout_root())
    bundle = bundle_directory()
    if bundle is not None:
        folders.append(bundle)
    if getattr(sys, "frozen", False):
        folders.append(Path(sys.executable).parent)
    return tuple(folder for folder in folders if str(folder).strip())


def deletion_refusal(
    root: Path,
    *,
    state_file: Path,
    other_installations: Iterable[Path] = (),
) -> str | None:
    """Why ``root`` must not be deleted, in words a person can act on; None if it may be.

    Only the path is judged here. That the folder holds an installation of
    this Hub is for the caller to establish as well.
    """
    if not root.is_absolute():
        return "O caminho desta instalação não é absoluto."
    if Path(root.anchor) == root or _key(Path(root.anchor)) == _key(root):
        return "A raiz de uma unidade nunca é apagada."
    for folder in protected_folders():
        if _contains(root, folder):
            return (
                "Esta pasta é, ou contém, uma pasta do sistema, sua pasta pessoal ou o "
                f"próprio Hub ({folder}), e não será apagada."
            )
    if _contains(root, state_file):
        return "Esta pasta também guarda o estado do próprio Hub e não pode ser apagada por aqui."
    for other in other_installations:
        if _contains(root, other) or _contains(other, root):
            return (
                "Esta pasta contém outra instalação conhecida, ou está dentro de uma, "
                "e não será apagada."
            )
    if root.is_symlink() or _is_reparse_point(root):
        return "Esta pasta é um atalho para outro lugar e não será apagada."
    return None


def _is_reparse_point(path: Path) -> bool:
    """A symlink or a Windows junction: something a deletion could follow elsewhere."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def first_link_inside(root: Path) -> Path | None:
    """The first link or junction anywhere under ``root``, never followed; None if there is none."""
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or _is_reparse_point(path):
                return path
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
    return None


def delete_tree(root: Path) -> tuple[str, ...]:
    """Delete ``root`` and everything under it. Returns what could not be deleted.

    The caller has already checked, with :func:`first_link_inside`, that
    nothing inside leads elsewhere. Read-only files -- git objects, some
    installed packages -- are made writable and tried again; anything that
    still refuses is reported, never ignored.
    """
    failures: list[str] = []

    def retry(function, path, _exc_info) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
            function(path)
        except OSError as exc:
            failures.append(f"{path}: {exc.__class__.__name__}")

    shutil.rmtree(root, onerror=retry)
    if root.exists() and not failures:
        failures.append(f"{root}: a pasta continua existindo")
    return tuple(failures)


__all__ = [
    "delete_tree",
    "deletion_refusal",
    "first_link_inside",
    "protected_folders",
]
