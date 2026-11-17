"""Internal, capability-free profile owned by tests, never by an installed pack."""

from __future__ import annotations

import yaml

from local_ai_stack.profiles import Profile, validate_profile_data

INTERNAL_PROFILE_ID = "fixture-internal"
INTERNAL_PROFILE_NAME = "Internal fixture"


def internal_profile_data() -> dict:
    """Return fresh data so each scenario can change its own contract."""
    return {
        "schema_version": 1,
        "id": INTERNAL_PROFILE_ID,
        "name": INTERNAL_PROFILE_NAME,
        "version": "1.0.0",
        "visibility": "internal",
        "ui": {"default_pet": "none"},
        "hardware": {
            "os": "windows",
            "gpu_vendor": "nvidia",
            "min_vram_gb": 1,
            "recommended_vram_gb": 1,
        },
        "services": {
            "ollama": {"enabled": True, "port": 11434},
            "open-webui": {"enabled": True, "port": 8080},
            "comfyui": {"enabled": True, "port": 8188},
        },
        "features": {
            "chat": False,
            "vision": False,
            "tools": False,
            "image_generation": False,
            "image_editing": False,
        },
        "models": {},
        "workflows": {},
        "runtime": {"open_browser": False},
    }


def internal_profile_yaml() -> str:
    return yaml.safe_dump(internal_profile_data(), sort_keys=False)


def internal_profile() -> Profile:
    return validate_profile_data(internal_profile_data(), "<internal test fixture>")
