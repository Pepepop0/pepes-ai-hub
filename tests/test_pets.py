from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.pets import (
    PET_MANIFEST,
    PetAnimation,
    PetAnimator,
    PetDefinition,
    PetError,
    PetFrame,
    PetState,
    available_pets,
    build_animation,
    frames_are_terminal_safe,
    load_pet,
    load_pet_by_id,
    load_pets,
    unsafe_characters,
    validate_pet_data,
)

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "pets"

MANIFEST = """schema_version: 1
id: fixture
name: Fixture
description: test pet
animations:
  idle:
    fps: 2
    loop: true
  success:
    frame_duration_ms: 100
    loop: false
"""


def write_pet(root: Path, manifest: str = MANIFEST, **states: list[str]) -> Path:
    directory = root / "fixture"
    frames = states or {"idle": ["[o o]", "[- -]"], "success": ["[^ ^]", "[v v]"]}
    for state, texts in frames.items():
        state_directory = directory / state
        state_directory.mkdir(parents=True, exist_ok=True)
        for index, text in enumerate(texts, start=1):
            (state_directory / f"{index:02d}.txt").write_text(text + "\n", encoding="utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / PET_MANIFEST).write_text(manifest, encoding="utf-8")
    return directory


# --- frames ----------------------------------------------------------------


def test_frame_keeps_whitespace_and_reports_dimensions():
    frame = PetFrame.from_text("  ab  \n c\n")
    assert frame.lines == ("  ab  ", " c")
    assert (frame.height, frame.width) == (2, 6)
    assert frame.text == "  ab  \n c"
    assert frame.padded_lines == ("  ab  ", " c    ")


def test_frame_normalises_line_endings_and_bom():
    frame = PetFrame.from_text("﻿ab\r\ncd\r\n")
    assert frame.lines == ("ab", "cd")


@pytest.mark.parametrize(
    "text",
    [
        "ok\x1b[31m",  # ANSI escape
        "ok\x07",  # bell
        "a\tb",  # tab renders at unpredictable width
        "hi \u200bthere",  # zero width space
        "cat \U0001f431",  # emoji
        "check ✔",  # dingbat with emoji presentation
    ],
)
def test_unsafe_characters_are_rejected(text):
    assert unsafe_characters(text)
    with pytest.raises(PetError, match="terminal-safe"):
        PetFrame.from_text(text)


@pytest.mark.parametrize("text", ["(o o)", "|██|", "┌─┐", "⣿ dots"])
def test_block_and_box_art_stays_allowed(text):
    assert unsafe_characters(text) == ()
    assert PetFrame.from_text(text).text == text


def test_empty_frame_is_rejected():
    with pytest.raises(PetError, match="empty"):
        PetFrame.from_text("   \n  \n")


def test_animation_requires_consistent_dimensions():
    frames = [PetFrame.from_text("abc"), PetFrame.from_text("ab")]
    with pytest.raises(PetError, match="must share one size"):
        build_animation(PetState.IDLE, frames)


def test_animation_requires_at_least_one_frame():
    with pytest.raises(PetError, match="at least one frame"):
        build_animation(PetState.IDLE, [])


# --- animation timing ------------------------------------------------------


def animation(count: int = 3, *, loop: bool = True, duration: float = 0.5) -> PetAnimation:
    frames = [PetFrame.from_text(f"frame{index}") for index in range(count)]
    return build_animation(PetState.IDLE, frames, frame_duration=duration, loop=loop)


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(-1.0, 0), (0.0, 0), (0.49, 0), (0.5, 1), (1.0, 2), (1.5, 0), (2.6, 2)],
)
def test_looping_animation_cycles_deterministically(elapsed, expected):
    assert animation().frame_index_at(elapsed) == expected


@pytest.mark.parametrize(
    ("elapsed", "expected"), [(0.0, 0), (0.5, 1), (1.0, 2), (5.0, 2), (50.0, 2)]
)
def test_non_looping_animation_holds_the_last_frame(elapsed, expected):
    assert animation(loop=False).frame_index_at(elapsed) == expected


def test_non_looping_animation_reports_when_it_is_finished():
    once = animation(loop=False)
    assert not once.is_finished(0.0)
    assert not once.is_finished(0.9)
    assert once.is_finished(1.0)
    assert animation(loop=True).is_finished(99.0) is False


def test_single_frame_animation_never_moves():
    still = animation(count=1)
    assert still.frame_index_at(0.0) == still.frame_index_at(120.0) == 0


def test_fps_and_duration_are_two_views_of_one_value():
    assert animation(duration=0.25).fps == 4.0
    assert animation(count=4, duration=0.25).duration == 1.0


# --- animator --------------------------------------------------------------


def fixture_pet(tmp_path: Path) -> PetDefinition:
    return load_pet(write_pet(tmp_path))


def test_animator_follows_the_clock_the_caller_owns(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), now=100.0)
    assert animator.frame_index(100.0) == 0
    assert animator.frame_index(100.6) == 1
    assert animator.frame_index(101.0) == 0


def test_changing_state_resets_the_animation(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), now=0.0)
    assert animator.frame_index(0.6) == 1
    assert animator.set_state(PetState.SUCCESS, now=0.6) is True
    assert animator.state is PetState.SUCCESS
    assert animator.elapsed(0.6) == 0.0
    assert animator.frame_index(0.6) == 0


def test_setting_the_same_state_does_not_restart(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), now=0.0)
    assert animator.set_state(PetState.IDLE, now=0.6) is False
    assert animator.frame_index(0.6) == 1
    animator.set_state(PetState.IDLE, now=0.6, restart=True)
    assert animator.frame_index(0.6) == 0


def test_restart_rewinds_to_the_first_frame(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), now=0.0)
    animator.restart(now=0.6)
    assert animator.frame_index(0.6) == 0


def test_advance_steps_frames_without_waiting(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), now=0.0)
    assert animator.advance(now=0.0).text == "[- -]"
    assert animator.advance(now=0.0).text == "[o o]"
    assert animator.advance(2, now=0.0).text == "[o o]"


def test_animator_reports_a_finished_one_shot(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path), PetState.SUCCESS, now=0.0)
    assert animator.is_finished(0.0) is False
    assert animator.is_finished(0.2) is True
    assert animator.current_text(9.0) == "[v v]"


def test_animator_without_an_explicit_clock_uses_monotonic_time(tmp_path):
    animator = PetAnimator(fixture_pet(tmp_path))
    assert animator.current_frame().height == 1
    assert animator.elapsed() >= 0.0


# --- definition and fallbacks ----------------------------------------------


def test_missing_states_fall_back_to_a_state_the_pet_has(tmp_path):
    pet = fixture_pet(tmp_path)
    assert pet.states == (PetState.IDLE, PetState.SUCCESS)
    assert pet.has_state(PetState.WORKING) is False
    assert pet.animation_for(PetState.WORKING).state is PetState.IDLE
    assert pet.animation_for(PetState.THINKING).state is PetState.IDLE
    assert pet.animation_for(PetState.SUCCESS).state is PetState.SUCCESS


def test_frame_at_reads_through_the_fallback(tmp_path):
    pet = fixture_pet(tmp_path)
    assert pet.frame_at(PetState.DOWNLOADING, 0.0).text == "[o o]"


# --- loading ---------------------------------------------------------------


def test_load_pet_reads_manifest_and_frames(tmp_path):
    pet = load_pet(write_pet(tmp_path))
    assert (pet.id, pet.name, pet.description) == ("fixture", "Fixture", "test pet")
    idle = pet.animation_for(PetState.IDLE)
    assert idle.frame_count == 2
    assert idle.loop is True
    assert idle.frame_duration == 0.5
    success = pet.animation_for(PetState.SUCCESS)
    assert success.loop is False
    assert success.frame_duration == 0.1


def test_load_pets_and_available_pets(tmp_path):
    write_pet(tmp_path)
    assert available_pets(tmp_path) == ("fixture",)
    assert [pet.id for pet in load_pets(tmp_path)] == ["fixture"]
    assert load_pet_by_id("fixture", tmp_path).id == "fixture"


def test_unknown_pet_lists_what_is_available(tmp_path):
    write_pet(tmp_path)
    with pytest.raises(PetError, match="available: fixture"):
        load_pet_by_id("hello-kitty", tmp_path)


def test_missing_manifest_is_reported(tmp_path):
    (tmp_path / "ghost").mkdir()
    with pytest.raises(PetError, match=PET_MANIFEST):
        load_pet(tmp_path / "ghost")


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (MANIFEST.replace("schema_version: 1", "schema_version: 7"), "unsupported version"),
        (MANIFEST.replace("id: fixture", "id: other"), "must match the directory name"),
        (MANIFEST.replace("id: fixture", "id: fixture\nextra: 1"), "unknown fields: extra"),
        (MANIFEST.replace("fps: 2", "fps: 0"), "must be a number > 0"),
        (MANIFEST.replace("fps: 2", "fps: 2\n    frame_duration_ms: 100"), "not both"),
        (MANIFEST.replace("loop: true", "loop: yes-please"), "must be a boolean"),
        (MANIFEST.replace("  success:", "  dancing:"), "unknown state"),
        (MANIFEST.replace("frame_duration_ms: 100", "speed: fast"), "unknown fields"),
    ],
)
def test_manifest_errors_are_specific(tmp_path, manifest, message):
    directory = write_pet(tmp_path, manifest)
    with pytest.raises(PetError, match=message):
        load_pet(directory)


def test_pet_without_an_idle_animation_is_rejected(tmp_path):
    manifest = MANIFEST.replace("  idle:\n    fps: 2\n    loop: true\n", "")
    directory = write_pet(tmp_path, manifest, success=["[^ ^]", "[v v]"])
    with pytest.raises(PetError, match="missing required states: idle"):
        load_pet(directory)


def test_declared_state_without_frames_directory_is_rejected(tmp_path):
    directory = write_pet(tmp_path)
    for frame in (directory / "success").iterdir():
        frame.unlink()
    (directory / "success").rmdir()
    with pytest.raises(PetError, match="frame directory missing"):
        load_pet(directory)


def test_undeclared_frame_directory_is_rejected(tmp_path):
    directory = write_pet(tmp_path)
    (directory / "working").mkdir()
    (directory / "working" / "01.txt").write_text("[x x]\n", encoding="utf-8")
    with pytest.raises(PetError, match="not declared"):
        load_pet(directory)


def test_empty_state_directory_is_rejected(tmp_path):
    directory = write_pet(tmp_path)
    for frame in (directory / "idle").iterdir():
        frame.unlink()
    with pytest.raises(PetError, match="no \\*.txt frames"):
        load_pet(directory)


def test_frames_are_ordered_by_filename(tmp_path):
    directory = write_pet(tmp_path)
    (directory / "idle" / "00.txt").write_text("[1st]\n", encoding="utf-8")
    pet = load_pet(directory)
    assert pet.animation_for(PetState.IDLE).frames[0].text == "[1st]"


def test_validate_pet_data_rejects_a_non_mapping(tmp_path):
    with pytest.raises(PetError, match="must be a mapping"):
        validate_pet_data(["not", "a", "pet"], tmp_path / "fixture")


# --- shipped Claw asset ----------------------------------------------------


def test_claw_is_the_only_shipped_pet():
    assert available_pets(ASSETS) == ("claw",)


def test_claw_loads_with_every_state_this_milestone_promised():
    claw = load_pet_by_id("claw", ASSETS)
    assert claw.id == "claw"
    assert claw.states == (
        PetState.ERROR,
        PetState.IDLE,
        PetState.SLEEPING,
        PetState.SUCCESS,
        PetState.WARNING,
        PetState.WORKING,
    )
    assert claw.animation_for(PetState.SUCCESS).loop is False
    assert claw.animation_for(PetState.IDLE).loop is True


def test_claw_frames_are_terminal_safe_and_uniform():
    claw = load_pet_by_id("claw", ASSETS)
    for state in claw.states:
        clip = claw.animation_for(state)
        assert clip.frame_count >= 2
        assert frames_are_terminal_safe(clip.frames)
        assert {(f.height, f.width) for f in clip.frames} == {(clip.height, clip.width)}
        assert all(char.isascii() for frame in clip.frames for char in frame.text)


def test_claw_animates_between_distinct_frames():
    claw = load_pet_by_id("claw", ASSETS)
    animator = PetAnimator(claw, PetState.SLEEPING, now=0.0)
    first = animator.current_text(0.0)
    second = animator.current_text(1.5)
    assert first != second
