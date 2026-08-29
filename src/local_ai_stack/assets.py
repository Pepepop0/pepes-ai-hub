"""Where the repository keeps its declarative visual assets.

Pets and themes ship as data next to the profiles and manifests, not as Python.
This module answers "which directory?" and nothing else, so no asset loader
needs to know how the checkout is laid out.

Integration point: like ``profiles/`` and ``manifests/``, these directories live
in the repository, not in the installed runtime tree owned by
:mod:`local_ai_stack.layout`. Packaging them into a wheel is a build concern and
is not decided here.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ASSETS_ROOT = "PEPITOS_AI_HUB_ASSETS"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def assets_directory() -> Path:
    """The asset root, overridable with ``PEPITOS_AI_HUB_ASSETS``."""
    override = os.environ.get(ENV_ASSETS_ROOT, "").strip()
    if override:
        return Path(override).expanduser()
    return project_root() / "assets"


def default_pets_directory() -> Path:
    return assets_directory() / "pets"


def default_themes_directory() -> Path:
    return assets_directory() / "themes"
