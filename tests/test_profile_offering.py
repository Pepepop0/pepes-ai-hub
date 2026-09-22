"""Which profiles a person is offered, and how each stands against this machine.

Two decisions the wizard used to make badly: it preselected one profile by
name, and it offered the profile that exists to exercise the machinery. Both
are now answers the data gives -- the detected VRAM, and a visibility the
profile declares -- and neither reads a profile's id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_app import reference_facts
from test_installation_switching import World, hub

from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.profiles import (
    ENV_SHOW_INTERNAL_PROFILES,
    VISIBILITY_INTERNAL,
    VISIBILITY_PUBLIC,
    ProfileError,
    load_profile,
    load_profiles,
    offered_profiles,
    validate_profile_data,
)
from local_ai_stack.storage import (
    DiskSpace,
    enough_space,
    free_space,
    model_download_estimate,
)
from local_ai_stack.system import GPUInfo, SystemFacts
from local_ai_stack.tui.application import COMPATIBLE, INSUFFICIENT, RECOMMENDED

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = load_profiles(ROOT / "profiles")


# --- visibility -------------------------------------------------------------------


def test_the_shipped_profiles_say_who_they_are_for():
    visibility = {profile.id: profile.visibility for profile in SHIPPED}
    assert visibility == {
        "capella": VISIBILITY_PUBLIC,
        "pepe": VISIBILITY_PUBLIC,
        "pepe-8gb": VISIBILITY_PUBLIC,
        "test": VISIBILITY_INTERNAL,
    }


def test_an_internal_profile_is_not_offered_and_is_not_gone():
    offered = offered_profiles(SHIPPED, env={})
    assert [profile.id for profile in offered] == ["capella", "pepe", "pepe-8gb"]
    assert {profile.id for profile in SHIPPED} - {profile.id for profile in offered} == {"test"}


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_a_machine_can_ask_for_the_internal_ones(value):
    offered = offered_profiles(SHIPPED, env={ENV_SHOW_INTERNAL_PROFILES: value})
    assert "test" in {profile.id for profile in offered}


def test_visibility_defaults_to_public_and_anything_else_is_refused():
    data = load_profile(ROOT / "profiles" / "pepe.yaml").raw
    assert "visibility" not in data
    assert validate_profile_data(data).visibility == VISIBILITY_PUBLIC
    with pytest.raises(ProfileError, match="visibility"):
        validate_profile_data({**data, "visibility": "secret"})


def test_no_screen_decides_by_a_profile_id():
    """The filter is the declaration, not a name anybody typed into the code."""
    sources = [
        (ROOT / "src" / "local_ai_stack" / "hub_application.py").read_text(encoding="utf-8"),
        (ROOT / "src" / "local_ai_stack" / "app.py").read_text(encoding="utf-8"),
        (ROOT / "src" / "local_ai_stack" / "tui" / "wizard.py").read_text(encoding="utf-8"),
    ]
    for source in sources:
        for quoted in ('"test"', "'test'", '"capella"', "'capella'"):
            assert quoted not in source, quoted


# --- recommendation ----------------------------------------------------------------


def facts_with(vram_mib: int) -> SystemFacts:
    return SystemFacts(
        "Windows", "10", "3.11.9", True, True, (GPUInfo("NVIDIA GeForce", vram_mib),)
    )


@pytest.mark.parametrize(
    ("vram_mib", "expected"),
    [
        # An 8 GB card: the 8 GB profile is the largest that fits.
        (8188, {"pepe-8gb": RECOMMENDED, "pepe": INSUFFICIENT, "capella": INSUFFICIENT}),
        # The reference 12 GB card: Pepe is recommended, Pepinho still fits.
        (12227, {"pepe-8gb": COMPATIBLE, "pepe": RECOMMENDED, "capella": INSUFFICIENT}),
        # The client's 32 GB card, as it reports itself.
        (32607, {"pepe-8gb": COMPATIBLE, "pepe": COMPATIBLE, "capella": RECOMMENDED}),
    ],
)
def test_the_largest_profile_that_fits_is_the_recommended_one(tmp_path, vram_mib, expected):
    world = World()
    adapter = hub(tmp_path, world)
    try:
        adapter.backend.facts = lambda: facts_with(vram_mib)
        choices = {choice.id: choice.standing for choice in adapter.profile_choices()}
        assert choices == expected
        assert adapter.recommended_profile() == next(
            profile for profile, standing in expected.items() if standing == RECOMMENDED
        )
    finally:
        adapter.close()


def test_the_offered_profiles_are_ordered_from_smallest_to_largest(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    try:
        adapter.backend.facts = lambda: facts_with(12227)
        choices = adapter.profile_choices()
        assert [choice.id for choice in choices] == ["pepe-8gb", "pepe", "capella"]
        assert [choice.required_vram_gb for choice in choices] == [8, 12, 32]
        assert "test" not in {choice.id for choice in choices}
    finally:
        adapter.close()


def test_a_machine_nothing_fits_has_no_recommendation(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    try:
        adapter.backend.facts = lambda: facts_with(4096)
        assert adapter.recommended_profile() is None
        assert {choice.standing for choice in adapter.profile_choices()} == {INSUFFICIENT}
    finally:
        adapter.close()


def test_the_reference_machine_recommends_pepe(tmp_path):
    world = World()
    adapter = hub(tmp_path, world)
    try:
        adapter.backend.facts = reference_facts
        assert adapter.recommended_profile() == "pepe"
    finally:
        adapter.close()


# --- what an installation will download, and whether it fits -------------------------


def test_every_shipped_profile_can_say_what_it_downloads():
    specs = {str(key): value for key, value in model_catalog().specs.items()}
    estimates = {
        profile.id: model_download_estimate(profile, specs)
        for profile in SHIPPED
        if profile.model_requirements
    }
    assert all(estimate.complete for estimate in estimates.values()), estimates
    assert estimates["capella"].known_bytes > estimates["pepe"].known_bytes
    assert estimates["pepe"].known_bytes > estimates["pepe-8gb"].known_bytes
    assert 13 < estimates["pepe-8gb"].known_gib < 15
    assert 27 < estimates["capella"].known_gib < 29


def test_a_model_without_a_recorded_size_is_named_not_guessed():
    profile = next(p for p in SHIPPED if p.id == "pepe")
    estimate = model_download_estimate(profile, {})
    assert estimate.known_bytes == 0
    assert set(estimate.unknown_items) == {
        str(item.model_id) for item in profile.model_requirements
    }
    assert not estimate.complete


def test_free_space_is_measured_on_the_volume_a_folder_would_be_created_in(tmp_path):
    space = free_space(tmp_path / "does" / "not" / "exist" / "yet")
    assert space is not None and space.free_bytes > 0
    assert space.measured_at == tmp_path or space.measured_at in tmp_path.parents
    assert not (tmp_path / "does").exists(), "asking never creates anything"


def test_space_is_only_called_insufficient_when_the_known_bytes_do_not_fit():
    specs = {str(key): value for key, value in model_catalog().specs.items()}
    estimate = model_download_estimate(next(p for p in SHIPPED if p.id == "capella"), specs)
    tight = DiskSpace(Path("X:/"), Path("X:/"), estimate.known_bytes - 1)
    roomy = DiskSpace(Path("X:/"), Path("X:/"), estimate.known_bytes + 1)

    assert enough_space(estimate, tight) is False
    assert enough_space(estimate, roomy) is True
    assert enough_space(estimate, None) is None
    assert enough_space(model_download_estimate(SHIPPED[0], {}), roomy) is None
