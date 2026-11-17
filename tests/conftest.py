"""Shared fixtures."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def managed_calibration_runtime(monkeypatch):
    """The pure ledger is installed beside the WebUI; tests load that same source without
    installing or importing Open WebUI into the Hub's environment."""
    for asset in (
        "pepito_calibration",
        "pepito_image_preservation",
        "pepito_edit_methods",
        "pepito_swap",
    ):
        path = Path(__file__).resolve().parents[1] / "assets" / "open-webui" / f"{asset}.py"
        spec = importlib.util.spec_from_file_location(f"open_webui.{asset}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setitem(sys.modules, spec.name, module)


@pytest.fixture(autouse=True)
def native_extension_sync_is_not_part_of_other_tests(request, monkeypatch):
    """Installing Open WebUI also synchronizes its native image extension, which asks the
    managed interpreter where the package lives and then rewrites that package's files.
    Tests of an install that script a fake runner (or have no Open WebUI sources) are not
    about that; ``test_open_webui_native.py`` runs the real synchronization against real
    files and opts out."""
    if request.module.__name__.rsplit(".", 1)[-1] == "test_open_webui_native":
        return
    monkeypatch.setattr("local_ai_stack.open_webui.sync_native_extension", lambda *a, **k: False)
