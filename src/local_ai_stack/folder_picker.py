"""Native folder selection, kept out of the TUI.

The wizard asks for a folder through the ``FolderPicker`` boundary declared in
``tui/application.py``. This module is the Windows answer to that request and
the only place in the program that knows COM exists. It lives outside ``tui/``
on purpose: a terminal UI has no business importing ctypes, and composition
(``app.py``) is what binds the two. Nothing here imports the TUI either; the
boundary is structural.

The dialog is the system's own ``IFileOpenDialog`` with ``FOS_PICKFOLDERS`` -
the same folder picker Explorer uses - driven through its COM vtable. No shell,
no PowerShell, no helper process, nothing parsed from stdout.

``choose_folder`` blocks until the person closes the dialog. Callers run it on
a worker thread; COM is initialized and released on whichever thread calls it.
"""

from __future__ import annotations

import sys
from pathlib import Path

DIALOG_TITLE = "Escolher pasta de instalação"

_CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_IID_IFILE_OPEN_DIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
_IID_ISHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_HRESULT_CANCELLED = -2147023673  # HRESULT_FROM_WIN32(ERROR_CANCELLED)

_FOS_PICKFOLDERS = 0x00000020
_FOS_FORCEFILESYSTEM = 0x00000040
_FOS_PATHMUSTEXIST = 0x00000800
_SIGDN_FILESYSPATH = 0x80058000

# Vtable slots. IUnknown is 0-2, IModalWindow::Show is 3, then IFileDialog.
_RELEASE = 2
_SHOW = 3
_SET_OPTIONS = 9
_GET_OPTIONS = 10
_SET_FOLDER = 12
_SET_TITLE = 17
_GET_RESULT = 20
_ITEM_GET_DISPLAY_NAME = 5  # IShellItem, after IUnknown


def _hresult_succeeded(value: int) -> bool:
    return value >= 0


def _hresult_cancelled(value: int) -> bool:
    return value == _HRESULT_CANCELLED


class NullFolderPicker:
    """No native dialog on this platform: the manual path field is the way in."""

    def choose_folder(self, initial_path: Path | None = None) -> Path | None:
        return None


class WindowsFolderPicker:
    """The Windows folder dialog. Returns None on cancel or on any COM failure."""

    def choose_folder(self, initial_path: Path | None = None) -> Path | None:
        if sys.platform != "win32":
            return None
        try:
            return _show_folder_dialog(initial_path)
        except OSError:
            return None


def default_folder_picker() -> WindowsFolderPicker | NullFolderPicker:
    return WindowsFolderPicker() if sys.platform == "win32" else NullFolderPicker()


def starting_folder(initial_path: Path | None) -> Path | None:
    """Where the dialog should open: the path itself, or its nearest existing ancestor.

    A custom root is often a folder that does not exist yet; opening on its
    parent is the closest the dialog can get. With nothing usable, None lets
    Windows pick its own default rather than this program guessing one.
    """
    if initial_path is None:
        return None
    candidate = Path(initial_path)
    if not candidate.is_absolute():
        return None
    for folder in (candidate, *candidate.parents):
        try:
            if folder.is_dir():
                return folder
        except OSError:
            return None
    return None


def _show_folder_dialog(initial_path: Path | None) -> Path | None:  # pragma: no cover - Win32 GUI
    import ctypes
    from ctypes import POINTER, byref, c_long, c_uint32, c_void_p, c_wchar_p

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    ole32.CoInitializeEx.argtypes = (c_void_p, c_uint32)
    ole32.CoInitializeEx.restype = c_long
    ole32.CLSIDFromString.argtypes = (c_wchar_p, POINTER(GUID))
    ole32.CLSIDFromString.restype = c_long
    ole32.CoCreateInstance.argtypes = (
        POINTER(GUID),
        c_void_p,
        c_uint32,
        POINTER(GUID),
        POINTER(c_void_p),
    )
    ole32.CoCreateInstance.restype = c_long
    ole32.CoTaskMemFree.argtypes = (c_void_p,)
    ole32.CoTaskMemFree.restype = None
    shell32.SHCreateItemFromParsingName.argtypes = (
        c_wchar_p,
        c_void_p,
        POINTER(GUID),
        POINTER(c_void_p),
    )
    shell32.SHCreateItemFromParsingName.restype = c_long

    def guid(text: str) -> GUID:
        value = GUID()
        if not _hresult_succeeded(ole32.CLSIDFromString(c_wchar_p(text), byref(value))):
            raise OSError(f"invalid GUID {text}")
        return value

    def method(pointer: c_void_p, slot: int, *argtypes):
        vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
        return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(vtable[slot])

    def release(pointer: c_void_p) -> None:
        if pointer:
            method(pointer, _RELEASE)(pointer)

    initialized = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    # RPC_E_CHANGED_MODE means this worker is in a different apartment. A
    # successful initialization (S_OK or S_FALSE) is the only state in which
    # this STA dialog may be created, and every success is balanced below.
    if not _hresult_succeeded(initialized):
        return None
    dialog = c_void_p()
    try:
        clsid, iid = guid(_CLSID_FILE_OPEN_DIALOG), guid(_IID_IFILE_OPEN_DIALOG)
        if (
            ole32.CoCreateInstance(
                byref(clsid), None, _CLSCTX_INPROC_SERVER, byref(iid), byref(dialog)
            )
            < 0
        ):
            return None

        options = c_uint32()
        if not _hresult_succeeded(
            method(dialog, _GET_OPTIONS, POINTER(c_uint32))(dialog, byref(options))
        ):
            return None
        if not _hresult_succeeded(
            method(dialog, _SET_OPTIONS, c_uint32)(
                dialog,
                options.value | _FOS_PICKFOLDERS | _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST,
            )
        ):
            return None
        method(dialog, _SET_TITLE, c_wchar_p)(dialog, DIALOG_TITLE)

        folder = starting_folder(initial_path)
        if folder is not None:
            item = c_void_p()
            item_iid = guid(_IID_ISHELL_ITEM)
            if (
                shell32.SHCreateItemFromParsingName(
                    c_wchar_p(str(folder)), None, byref(item_iid), byref(item)
                )
                >= 0
            ):
                try:
                    method(dialog, _SET_FOLDER, c_void_p)(dialog, item)
                finally:
                    release(item)

        shown = method(dialog, _SHOW, c_void_p)(dialog, None)
        if _hresult_cancelled(shown):
            return None
        if not _hresult_succeeded(shown):
            return None

        result = c_void_p()
        if method(dialog, _GET_RESULT, POINTER(c_void_p))(dialog, byref(result)) < 0:
            return None
        try:
            name = c_void_p()
            if (
                method(result, _ITEM_GET_DISPLAY_NAME, c_uint32, POINTER(c_void_p))(
                    result, _SIGDN_FILESYSPATH, byref(name)
                )
                < 0
                or not name
            ):
                return None
            try:
                text = ctypes.wstring_at(name)
            finally:
                ole32.CoTaskMemFree(name)
            return Path(text) if text else None
        finally:
            release(result)
    finally:
        try:
            release(dialog)
        finally:
            ole32.CoUninitialize()


__all__ = [
    "DIALOG_TITLE",
    "NullFolderPicker",
    "WindowsFolderPicker",
    "default_folder_picker",
    "starting_folder",
]
