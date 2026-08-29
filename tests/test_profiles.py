from pathlib import Path

from local_ai_stack.profiles import load_profiles

ROOT = Path(__file__).resolve().parents[1]


def test_all_profiles_load():
    p = load_profiles(ROOT / "profiles")
    assert {x.id for x in p} == {"pepe", "capella", "test"}


def test_ids_unique():
    p = load_profiles(ROOT / "profiles")
    ids = [x.id for x in p]
    assert len(ids) == len(set(ids))


def test_capella_requires_more_vram():
    p = {x.id: x for x in load_profiles(ROOT / "profiles")}
    assert p["capella"].hardware.min_vram_gb > p["pepe"].hardware.min_vram_gb
