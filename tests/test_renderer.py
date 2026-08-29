from __future__ import annotations

import random
import threading
from pathlib import Path

import pytest

from local_ai_stack.behavior import (
    BehaviorConfig,
    BlinkConfig,
    EyeState,
    JumpConfig,
    MicroBehavior,
)
from local_ai_stack.pets import (
    PetAnimator,
    PetError,
    PetFrame,
    PetState,
    frames_are_terminal_safe,
    load_pet_by_id,
    unsafe_characters,
)
from local_ai_stack.renderer import (
    BLANK_CHARACTERS,
    MicroBehaviorRenderer,
    blank_rows_at_top,
    braille_to_dots,
    build_pet_renderer,
    crop_to_ink,
    dots_to_braille,
    downscale_dots,
    ink_bounds,
    is_blank_row,
    normalize_canvas,
    raise_drawing,
    scale_to_width,
)

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "assets" / "pets"
BRAILLE_BLANK = "⠀"


def hello_kitty(seed: int = 5, now: float = 0.0) -> MicroBehaviorRenderer:
    renderer = build_pet_renderer(
        load_pet_by_id("hello-kitty", ASSETS), now=now, rng=random.Random(seed)
    )
    assert isinstance(renderer, MicroBehaviorRenderer)
    return renderer


def walk(renderer, *, until: float = 30.0, step: float = 0.05) -> list[str]:
    """Drive one renderer along a clock, the way the Textual timer does."""
    frames: list[str] = []
    moment = 0.0
    while moment < until:
        frames.append(renderer.current_text(moment))
        moment += step
    return frames


def dimensions(text: str) -> tuple[int, int]:
    lines = text.split("\n")
    return len(lines), max(len(line) for line in lines)


# --- blank rows ------------------------------------------------------------


@pytest.mark.parametrize("line", ["", "   ", BRAILLE_BLANK * 4, " " + BRAILLE_BLANK])
def test_rows_that_draw_nothing_are_blank(line):
    assert is_blank_row(line)


@pytest.mark.parametrize("line", ["a", "  x  ", BRAILLE_BLANK + "⣿"])
def test_rows_with_ink_are_not_blank(line):
    assert not is_blank_row(line)


def test_braille_blank_counts_as_headroom():
    assert BRAILLE_BLANK in BLANK_CHARACTERS
    assert blank_rows_at_top([BRAILLE_BLANK, "   ", "⣿", BRAILLE_BLANK]) == 2


# --- lifting the drawing ---------------------------------------------------


def test_lifting_keeps_width_and_height():
    lines = ["    ", "    ", "ab  ", "cd  "]
    lifted = raise_drawing(lines, 2)
    assert len(lifted) == len(lines)
    assert {len(line) for line in lifted} == {4}
    assert lifted == ("ab  ", "cd  ", "    ", "    ")


def test_lifting_never_clips_visible_art():
    lines = ["   ", "xyz", "abc"]
    assert raise_drawing(lines, 2) == ("xyz", "abc", "   ")
    assert raise_drawing(["abc", "def"], 2) == ("abc", "def")


def test_a_hop_taller_than_the_headroom_is_shortened_not_cropped():
    lines = ["  ", "ab", "cd"]
    lifted = raise_drawing(lines, 5)
    assert "ab" in lifted and "cd" in lifted
    assert len(lifted) == 3


def test_lifting_by_zero_or_less_is_a_no_op():
    lines = ["   ", "abc"]
    assert raise_drawing(lines, 0) == tuple(lines)
    assert raise_drawing(lines, -3) == tuple(lines)
    assert raise_drawing([], 2) == ()


def test_normalize_pads_every_frame_to_one_canvas():
    frames = {"open": PetFrame(("ab", "c")), "closed": PetFrame(("d",))}
    canvases = normalize_canvas(frames)
    assert canvases["open"] == ("ab", "c ")
    assert canvases["closed"] == ("d ", "  ")


# --- the renderer ----------------------------------------------------------


def test_the_shipped_hello_kitty_renders_through_its_behavior():
    renderer = hello_kitty()
    assert renderer.pet.id == "hello-kitty"
    assert renderer.state is PetState.IDLE
    assert renderer.canvas_size == (31, 13)
    assert renderer.current_text(0.0)


def test_the_canvas_never_changes_size_while_the_character_moves():
    renderer = hello_kitty(seed=7)
    sizes = set()
    moment = 0.0
    while moment < 90:
        sizes.add(dimensions(renderer.current_text(moment)))
        moment += 0.05
    assert sizes == {(13, 31)}


def test_no_visible_row_is_ever_lost_to_a_hop():
    renderer = hello_kitty(seed=7)
    grounded = renderer.current_text(0.0).split("\n")
    ink = {line for line in grounded if not is_blank_row(line)}
    moment = 0.0
    while moment < 90:
        drawn = set(renderer.current_text(moment).split("\n"))
        # Every inked row of the eyes-open frame is either on screen or the
        # eyes-closed frame is showing; nothing is ever cropped away.
        if renderer.behavior.blinking:
            moment += 0.05
            continue
        assert ink <= drawn
        moment += 0.05


def test_blinking_swaps_the_drawing():
    renderer = hello_kitty(seed=7)
    seen = set()
    moment = 0.0
    while moment < 60:
        seen.add(renderer.current_text(moment))
        moment += 0.05
    assert len(seen) > 2, "open, closed and lifted variants should all appear"


def test_the_drawing_actually_rises_during_a_hop():
    config = BehaviorConfig(BlinkConfig(enabled=False), JumpConfig())
    renderer = MicroBehaviorRenderer(
        PetAnimator(load_pet_by_id("hello-kitty", ASSETS), now=0.0),
        load_pet_by_id("hello-kitty", ASSETS).behavior.frames,
        config=config,
        now=0.0,
        rng=random.Random(3),
    )
    grounded = renderer.current_text(0.0)
    lifted = None
    moment = 0.0
    while moment < 60 and lifted is None:
        text = renderer.current_text(moment)
        if renderer.behavior.jumping and text != grounded:
            lifted = text
        moment += 0.02
    assert lifted is not None
    assert dimensions(lifted) == dimensions(grounded)
    assert lifted.split("\n")[-1].strip(BLANK_CHARACTERS) == ""


def test_the_same_seed_draws_the_same_life():
    first = walk(hello_kitty(seed=21))
    assert first == walk(hello_kitty(seed=21))
    assert walk(hello_kitty(seed=22)) != first


def test_asking_for_the_same_instant_twice_returns_the_same_frame():
    renderer = hello_kitty(seed=9)
    renderer.current_text(4.0)
    schedule = (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at)
    frames = {renderer.current_text(4.0) for _ in range(25)}
    assert len(frames) == 1
    assert (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at) == schedule


def test_pet_state_stays_semantic_and_does_not_carry_the_hop():
    renderer = hello_kitty()
    assert renderer.set_state(PetState.WORKING, now=1.0) is True
    assert renderer.state is PetState.WORKING
    assert renderer.set_state(PetState.WORKING, now=2.0) is False
    assert {state.value for state in PetState}.isdisjoint({"blink", "jump", "apex"})


def test_changing_state_does_not_restart_the_micro_behavior():
    renderer = hello_kitty(seed=12)
    renderer.current_text(5.0)
    schedule = (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at)
    renderer.set_state(PetState.SLEEPING, now=5.0)
    assert (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at) == schedule


def test_a_renderer_needs_an_open_frame():
    animator = PetAnimator(load_pet_by_id("claw", ASSETS), now=0.0)
    with pytest.raises(ValueError, match="open"):
        MicroBehaviorRenderer(animator, {"closed": PetFrame(("x",))}, now=0.0)


# --- asset-driven selection ------------------------------------------------


def test_art_is_scaled_to_the_width_the_view_offers():
    renderer = hello_kitty()
    assert renderer.canvas_size == (31, 13)
    assert renderer.fit_to_width(16) is True
    assert renderer.canvas_size[0] <= 16
    assert renderer.fit_to_width(16) is False, "re-fitting the same width is a no-op"
    assert renderer.fit_to_width(200) is True
    # Never larger than the asset itself asks for.
    assert renderer.canvas_size == (31, 13)


def test_scaling_keeps_every_frame_aligned():
    """Open and closed must scale identically, or a blink would shift the art."""
    pet = load_pet_by_id("hello-kitty", ASSETS)
    scaled = scale_to_width(
        {name: frame.lines for name, frame in pet.behavior.frames.items()}, 20, crop=True
    )
    assert {len(lines) for lines in scaled.values()} == {len(scaled["open"])}
    assert {len(lines[0]) for lines in scaled.values()} == {len(scaled["open"][0])}
    assert scaled["open"] != scaled["closed"]


def test_scaling_survives_a_hop_without_changing_the_canvas():
    renderer = hello_kitty(seed=7)
    renderer.fit_to_width(18)
    sizes = {dimensions(text) for text in walk(renderer, until=60)}
    assert len(sizes) == 1


def test_braille_round_trips_through_its_dot_bitmap():
    lines = ("⣿⠀", "⠁⣤")
    assert dots_to_braille(braille_to_dots(lines)) == lines


def test_downscaling_halves_the_bitmap():
    grid = [[True] * 8 for _ in range(8)]
    smaller = downscale_dots(grid, 2)
    assert len(smaller) == 4
    assert len(smaller[0]) == 4
    assert all(all(row) for row in smaller)


def test_downscaling_drops_sparse_blocks_and_keeps_solid_ones():
    grid = [[False] * 4 for _ in range(4)]
    grid[0][0] = True  # one lit dot in a 2x2 block: below the threshold
    grid[2][2] = grid[2][3] = grid[3][2] = True  # three of four: solid enough
    smaller = downscale_dots(grid, 2)
    assert smaller[0][0] is False
    assert smaller[1][1] is True


def test_cropping_removes_the_empty_margin():
    grid = [[False] * 5 for _ in range(5)]
    grid[2][1] = grid[2][2] = True
    assert ink_bounds(grid) == (2, 2, 1, 2)
    assert crop_to_ink(grid) == [[True, True]]


def test_cropping_an_empty_bitmap_is_harmless():
    grid = [[False] * 3 for _ in range(2)]
    assert ink_bounds(grid) is None
    assert crop_to_ink(grid) == grid


def test_the_asset_decides_the_renderer_not_the_caller():
    claw = build_pet_renderer(load_pet_by_id("claw", ASSETS), now=0.0)
    kitty = build_pet_renderer(load_pet_by_id("hello-kitty", ASSETS), now=0.0)
    assert isinstance(claw, PetAnimator)
    assert isinstance(kitty, MicroBehaviorRenderer)


def test_claw_still_animates_frame_by_frame():
    claw = build_pet_renderer(load_pet_by_id("claw", ASSETS), PetState.SLEEPING, now=0.0)
    assert claw.pet.id == "claw"
    assert claw.state is PetState.SLEEPING
    assert claw.current_text(0.0) != claw.current_text(1.5)
    assert "z" in claw.current_text(0.0)


def test_both_renderers_satisfy_the_same_surface():
    for pet_id in ("claw", "hello-kitty"):
        renderer = build_pet_renderer(load_pet_by_id(pet_id, ASSETS), now=0.0)
        assert renderer.pet.id == pet_id
        assert renderer.state is PetState.IDLE
        assert isinstance(renderer.started_at, float)
        assert isinstance(renderer.current_text(0.0), str)
        assert renderer.set_state(PetState.WARNING, now=1.0) is True


def test_a_pet_without_a_behavior_file_keeps_working(tmp_path):
    with pytest.raises(PetError):
        load_pet_by_id("hello-kitty", tmp_path)


# --- product rules ---------------------------------------------------------


def test_hello_kitty_art_is_terminal_safe():
    pet = load_pet_by_id("hello-kitty", ASSETS)
    frames = tuple(pet.behavior.frames.values())
    assert frames_are_terminal_safe(frames)
    for frame in frames:
        assert unsafe_characters(frame.text) == ()
        assert "\t" not in frame.text
        assert "\x1b" not in frame.text
        assert all(0x2800 <= ord(char) <= 0x28FF for line in frame.lines for char in line)


def test_every_rendered_frame_stays_terminal_safe():
    renderer = hello_kitty(seed=13)
    moment = 0.0
    while moment < 60:
        assert unsafe_characters(renderer.current_text(moment)) == ()
        moment += 0.1


def test_rendering_creates_no_thread():
    before = threading.active_count()
    renderer = hello_kitty(seed=2)
    moment = 0.0
    while moment < 60:
        renderer.current_text(moment)
        moment += 0.05
    assert threading.active_count() == before


def test_two_renderers_do_not_share_behavior_state():
    first, second = hello_kitty(seed=1), hello_kitty(seed=2)
    assert first.behavior is not second.behavior
    first.current_text(30.0)
    assert second.behavior.next_blink_at != first.behavior.next_blink_at


def test_eye_frames_are_the_two_the_manifest_names():
    pet = load_pet_by_id("hello-kitty", ASSETS)
    assert set(pet.behavior.frames) == {EyeState.OPEN.value, EyeState.CLOSED.value}
    assert pet.behavior.frames["open"].lines != pet.behavior.frames["closed"].lines


def test_the_behavior_is_not_a_two_frame_loop():
    """The idle animation exists for compatibility; the life comes from behavior."""
    pet = load_pet_by_id("hello-kitty", ASSETS)
    animator = PetAnimator(pet, now=0.0)
    plain = {animator.current_text(moment / 20) for moment in range(1200)}
    lively = set(walk(hello_kitty(seed=4), until=60))
    assert len(plain) == 2, "the idle animation alone is a two-frame loop"
    assert len(lively) > len(plain), "behavior adds blink and hop variants"


def test_a_disabled_behavior_still_renders_a_still_character():
    pet = load_pet_by_id("hello-kitty", ASSETS)
    renderer = MicroBehaviorRenderer(
        PetAnimator(pet, now=0.0),
        pet.behavior.frames,
        config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig(enabled=False)),
        now=0.0,
        rng=random.Random(1),
    )
    assert len(set(walk(renderer, until=40))) == 1


def test_micro_behavior_is_reusable_by_any_asset():
    """Nothing in the engine is bound to this particular character."""
    behavior = MicroBehavior(now=0.0, rng=random.Random(1))
    frames = {"open": PetFrame(("   ", "abc")), "closed": PetFrame(("   ", "xyz"))}
    canvases = normalize_canvas(frames)
    assert set(canvases) == {"open", "closed"}
    assert behavior.snapshot(0.0).eyes in set(EyeState)
