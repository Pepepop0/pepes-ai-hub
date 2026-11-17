"""The native window's size, position and maximized state between sessions.

Kept beside the Hub's state as a small JSON file. A file that is missing,
corrupt, or describes a window no longer on any screen is ignored: geometry
is a convenience and never a reason for the Hub not to open.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ..installation import InstallLocation, resolve_installation

WINDOW_FILENAME = "desktop-window.json"
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1320, 880
MIN_WIDTH, MIN_HEIGHT = 960, 640
MAX_SIDE = 16384
# How much of the title bar must stay on a screen for a position to be reused.
VISIBLE_MARGIN = 120


@dataclass(frozen=True)
class WindowGeometry:
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    x: int | None = None
    y: int | None = None
    maximized: bool = False


def window_state_path() -> Path:
    return resolve_installation(InstallLocation.default()).layout.state / WINDOW_FILENAME


def virtual_screen() -> tuple[int, int, int, int] | None:
    """Left, top, width and height of all monitors together; None when unknown."""
    if sys.platform != "win32":
        return None
    import ctypes

    metrics = ctypes.windll.user32.GetSystemMetrics
    left, top, width, height = (metrics(index) for index in (76, 77, 78, 79))
    return (left, top, width, height) if width > 0 and height > 0 else None


def _integer(value: object) -> int | None:
    return value if type(value) is int else None


def load_geometry(
    path: Path | None = None, screen: tuple[int, int, int, int] | None = None
) -> WindowGeometry:
    """The saved geometry, or defaults for anything unusable."""
    try:
        data = json.loads((path or window_state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WindowGeometry()
    if not isinstance(data, dict):
        return WindowGeometry()
    width, height = _integer(data.get("width")), _integer(data.get("height"))
    if width is None or height is None:
        return WindowGeometry()
    width = min(max(width, MIN_WIDTH), MAX_SIDE)
    height = min(max(height, MIN_HEIGHT), MAX_SIDE)
    maximized = data.get("maximized") is True
    x, y = _integer(data.get("x")), _integer(data.get("y"))
    if x is None or y is None or screen is None:
        return WindowGeometry(width, height, maximized=maximized)
    left, top, screen_width, screen_height = screen
    on_screen = (
        left - width + VISIBLE_MARGIN <= x <= left + screen_width - VISIBLE_MARGIN
        and top <= y <= top + screen_height - VISIBLE_MARGIN
    )
    if not on_screen:
        return WindowGeometry(width, height, maximized=maximized)
    return WindowGeometry(width, height, x, y, maximized)


def save_geometry(geometry: WindowGeometry, path: Path | None = None) -> None:
    """Best effort: a disk that refuses the write loses only the window position."""
    target = path or window_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(geometry)), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        return


def focus_existing_window(title: str) -> bool:
    """Bring an already open Hub window forward. False when there is none to show."""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    handle = user32.FindWindowW(None, title)
    if not handle:
        return False
    handle = ctypes.c_void_p(handle)
    user32.ShowWindow(handle, 9 if user32.IsIconic(handle) else 5)  # SW_RESTORE / SW_SHOW
    user32.SetForegroundWindow(handle)
    return True
