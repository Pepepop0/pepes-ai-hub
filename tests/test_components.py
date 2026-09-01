"""The canonical component ids, and the guarantee that nothing drifts from them."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.components import (
    CANONICAL_IDS,
    LEGACY_IDS,
    ComponentId,
    is_canonical,
    normalize_component_id,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.processes import DEFAULT_PROCESS_SPECS
from local_ai_stack.profiles import load_profile, validate_profile_data
from local_ai_stack.services import DEFAULT_SERVICE_SPECS
from local_ai_stack.stacks import STANDARD_STACK

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "profiles"


# --- forma canonica --------------------------------------------------------


def test_canonical_ids_are_hyphenated():
    assert CANONICAL_IDS == ("ollama", "open-webui", "comfyui")
    assert all("_" not in component_id for component_id in CANONICAL_IDS)


def test_attr_form_is_identifier_safe():
    assert ComponentId.OPEN_WEBUI.attr == "open_webui"
    assert ComponentId.OLLAMA.attr == "ollama"
    assert all(member.attr.isidentifier() for member in ComponentId)


def test_dir_name_is_the_canonical_id():
    assert ComponentId.OPEN_WEBUI.dir_name == "open-webui"


def test_component_id_is_a_plain_string():
    assert ComponentId.OPEN_WEBUI == "open-webui"
    assert f"{ComponentId.OPEN_WEBUI}" == "open-webui"


# --- normalizacao ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("open_webui", "open-webui"),
        ("openwebui", "open-webui"),
        ("open-webui", "open-webui"),
        ("  open_webui  ", "open-webui"),
        ("comfy_ui", "comfyui"),
        ("ollama", "ollama"),
    ],
)
def test_normalization(raw, expected):
    assert normalize_component_id(raw) == expected


def test_unknown_ids_pass_through_untouched():
    """Spelling is normalized here; which components exist is the stack's call."""
    assert normalize_component_id("stable-diffusion") == "stable-diffusion"
    assert is_canonical("stable-diffusion") is False


def test_every_legacy_alias_maps_to_a_canonical_id():
    assert all(is_canonical(target) for target in LEGACY_IDS.values())
    assert not any(is_canonical(alias) for alias in LEGACY_IDS)


# --- um id, todas as camadas -----------------------------------------------


def test_stack_components_use_canonical_ids():
    assert STANDARD_STACK.component_ids == CANONICAL_IDS


def test_runtime_service_ids_match_the_stack():
    assert tuple(spec.id for spec in DEFAULT_SERVICE_SPECS) == CANONICAL_IDS


def test_runtime_process_ids_match_the_stack():
    assert tuple(spec.service_id for spec in DEFAULT_PROCESS_SPECS) == CANONICAL_IDS


def test_shipped_profiles_use_canonical_service_ids():
    for path in sorted(PROFILES.glob("*.yaml")):
        profile = load_profile(path)
        assert set(profile.services) <= set(CANONICAL_IDS), path.name
        assert "open_webui" not in profile.services, path.name


def test_profile_services_join_with_stack_components():
    profile = load_profile(PROFILES / "pepe.yaml")
    assert set(profile.services) == set(STANDARD_STACK.component_ids)


def test_layout_directories_are_named_after_the_canonical_ids(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    for component in STANDARD_STACK.components:
        assert component.app_dir(layout).name == component.id


# --- profiles legados continuam carregando ---------------------------------


def legacy_profile_data() -> dict:
    data = load_profile(PROFILES / "pepe.yaml").raw
    services = dict(data["services"])
    services["open_webui"] = services.pop("open-webui")
    return {**data, "services": services}


def test_a_profile_written_with_the_old_spelling_still_loads():
    profile = validate_profile_data(legacy_profile_data(), "legacy.yaml")
    assert "open-webui" in profile.services
    assert "open_webui" not in profile.services


def test_both_spellings_in_one_profile_are_rejected():
    data = legacy_profile_data()
    data["services"]["open-webui"] = {"enabled": True, "port": 8080}
    with pytest.raises(Exception, match="duplicate service id"):
        validate_profile_data(data, "conflict.yaml")


# --- guarda contra reintroducao do split -----------------------------------


def test_no_module_uses_the_legacy_id_as_a_value():
    """Identifiers may hold the underscored form; an id *value* may not.

    Module names, attributes and constants cannot contain a hyphen, so
    open_webui.py and layout.open_webui_app are expected. What must never come
    back is the old spelling written as a string, because that is what would
    split the two layers again.
    """
    allowed = {"components.py", "processes.py"}  # the alias table and a cmdline marker
    offenders = []
    for path in sorted((REPO / "src" / "local_ai_stack").rglob("*.py")):
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        if '"open_webui"' in source or "'open_webui'" in source:
            offenders.append(path.name)
    assert offenders == []
