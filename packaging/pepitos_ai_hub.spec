# PyInstaller build definition for the Windows release of Pepito's AI Hub.
#
# Run through scripts/build_windows.ps1, which pins PyInstaller, cleans the
# output and smoke-tests the result. See BUILD_WINDOWS.md.
#
# Mode: one directory (dist/PepitosAIHub/), deliberately not one file. A
# one-file build unpacks itself into a temporary folder on every launch,
# which makes startup slower, gives antivirus heuristics a self-extracting
# executable to dislike, and hides the shipped profiles and manifests inside a
# temp directory nobody can inspect. A folder starts fast, keeps the data
# readable next to the program, and is what "put the app on a disk" means.
#
# Console: on. The Hub is a terminal application; the console window that
# opens when the executable is double-clicked is the Hub's own window, not a
# helper. Child services are started with CREATE_NO_WINDOW by the launcher and
# never open windows of their own.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from local_ai_stack import PRODUCT_NAME, __version__  # noqa: E402
from local_ai_stack.resources import RESOURCE_DIRECTORIES  # noqa: E402

APP_NAME = "PepitosAIHub"

# The product's own read-only data, bundled under the same names the checkout
# uses so resources.resource_root() finds them beside the code.
datas = [(str(ROOT / name), name) for name in RESOURCE_DIRECTORIES]
# Textual ships its default CSS and other data files inside the package.
datas += collect_data_files("textual")
datas += collect_data_files("rich")

hiddenimports = collect_submodules("textual.widgets")

a = Analysis(
    [str(SPEC_DIR / "launch.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller", "ruff", "mypy"],
    noarchive=False,
)
pyz = PYZ(a.pure)


def version_resource() -> str:
    """Write the Windows VERSIONINFO for this build from the one version string.

    The number in the executable's Properties dialog and the one in the Hub's
    footer come from local_ai_stack.__version__; nothing here is typed twice.
    """
    major, minor, patch = (int(part) for part in __version__.split(".")[:3])
    numbers = f"({major}, {minor}, {patch}, 0)"
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([StringTable("040904B0", [
      StringStruct("CompanyName", {PRODUCT_NAME!r}),
      StringStruct("FileDescription", {PRODUCT_NAME!r}),
      StringStruct("FileVersion", {__version__!r}),
      StringStruct("InternalName", {APP_NAME!r}),
      StringStruct("OriginalFilename", {APP_NAME + ".exe"!r}),
      StringStruct("ProductName", {PRODUCT_NAME!r}),
      StringStruct("ProductVersion", {__version__!r}),
    ])]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"""
    out = Path(workpath) / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return str(out)


exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    version=version_resource(),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
