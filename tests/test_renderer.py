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
    pet = load_pet_by_id("hello-kitty", ASSETS)
    renderer = MicroBehaviorRenderer(
        PetAnimator(pet, now=0.0),
        pet.behavior.variants,
        config=config,
        render=pet.behavior.document.render,
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


def test_a_pet_without_variants_still_draws_its_own_animation():
    """No variants means no blink, but the animation and the hop still run."""
    claw = load_pet_by_id("claw", ASSETS)
    renderer = MicroBehaviorRenderer(
        PetAnimator(claw, now=0.0), None, now=0.0, rng=random.Random(1)
    )
    assert renderer.blinks_in(PetState.IDLE) is False
    drawn = [line.rstrip() for line in renderer.current_text(0.0).splitlines()]
    expected = [line.rstrip() for line in claw.animation_for(PetState.IDLE).frames[0].lines]
    assert drawn == expected


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
    reference = scaled["idle:closed"]
    assert {len(lines) for lines in scaled.values()} == {len(reference)}
    assert {len(lines[0]) for lines in scaled.values()} == {len(reference[0])}
    assert len(scaled) == 1, "only the closed frame is a variant now"


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


def test_only_the_closed_frame_is_declared_per_state():
    """There is no 'open' variant: the animation itself provides that frame."""
    pet = load_pet_by_id("hello-kitty", ASSETS)
    assert set(pet.behavior.variants) == {PetState.IDLE}
    assert set(pet.behavior.variants_for(PetState.IDLE)) == {EyeState.CLOSED.value}
    assert pet.behavior.variants_for(PetState.WORKING) is None
    closed = pet.behavior.variants_for(PetState.IDLE)["closed"]
    assert closed.lines != pet.animation_for(PetState.IDLE).frames[0].lines


def test_the_behavior_is_not_a_two_frame_loop():
    """The idle animation exists for compatibility; the life comes from behavior."""
    pet = load_pet_by_id("hello-kitty", ASSETS)
    animator = PetAnimator(pet, now=0.0)
    plain = {animator.current_text(moment / 20) for moment in range(1200)}
    lively = set(walk(hello_kitty(seed=4), until=60))
    assert len(plain) == 1, "the idle animation on its own is a still drawing"
    assert len(lively) > len(plain), "behavior adds the blink and the hop"


def test_a_disabled_behavior_still_renders_a_still_character():
    pet = load_pet_by_id("hello-kitty", ASSETS)
    renderer = MicroBehaviorRenderer(
        PetAnimator(pet, now=0.0),
        pet.behavior.variants,
        config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig(enabled=False)),
        render=pet.behavior.document.render,
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


# --- semantic state drives the drawing -------------------------------------

SYNTHETIC_MANIFEST = """schema_version: 1
id: synthetic
name: Synthetic
animations:
  idle:
    fps: 1
    loop: true
  working:
    fps: 1
    loop: true
"""

SYNTHETIC_BEHAVIOR = """schema_version: 1
id: synthetic

micro_animations:
  blink:
    enabled: true
    min_interval_ms: 1000
    max_interval_ms: 1000
    min_duration_ms: 400
    max_duration_ms: 400
    double_blink_chance: 0.0
  jump:
    enabled: false

render:
  anchor: bottom
  preserve_canvas_height: true
  variants:
    idle:
      closed: base/idle-shut.txt
"""

SYNTHETIC_FRAMES = {
    "idle/01.txt": "        \n IDLE o \n ====== ",
    # One line shorter than idle on purpose: the anchor decides where the
    # padding that makes up the difference lands.
    "working/01.txt": " WORK ! \n ###### ",
    "base/idle-shut.txt": "        \n IDLE - \n ====== ",
}


def synthetic_pet(tmp_path: Path, *, behavior: str = SYNTHETIC_BEHAVIOR):
    """A pet with two visibly different states and eye variants for one of them."""
    directory = tmp_path / "synthetic"
    for relative, text in SYNTHETIC_FRAMES.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    (directory / "pet.yaml").write_text(SYNTHETIC_MANIFEST, encoding="utf-8")
    (directory / "behavior.yaml").write_text(behavior, encoding="utf-8")
    return load_pet_by_id("synthetic", tmp_path)


def synthetic_renderer(tmp_path: Path, **kwargs) -> MicroBehaviorRenderer:
    renderer = build_pet_renderer(synthetic_pet(tmp_path, **kwargs), now=0.0, rng=random.Random(1))
    assert isinstance(renderer, MicroBehaviorRenderer)
    return renderer


def test_pet_state_still_chooses_the_drawing_under_a_micro_behavior(tmp_path):
    renderer = synthetic_renderer(tmp_path)

    renderer.set_state(PetState.IDLE, now=0.0)
    idle = renderer.current_text(0.0)
    renderer.set_state(PetState.WORKING, now=0.0)
    working = renderer.current_text(0.0)

    assert "IDLE" in idle and "WORK" not in idle
    assert "WORK" in working and "IDLE" not in working
    assert idle != working


def test_blink_only_touches_the_states_the_asset_declares(tmp_path):
    renderer = synthetic_renderer(tmp_path)
    assert renderer.blinks_in(PetState.IDLE) is True
    assert renderer.blinks_in(PetState.WORKING) is False

    # The blink schedule is pinned: closed from 1.0s to 1.4s.
    renderer.set_state(PetState.IDLE, now=0.0)
    assert " IDLE o " in renderer.current_text(0.5)
    assert " IDLE - " in renderer.current_text(1.1)
    assert " IDLE o " in renderer.current_text(1.6)


def test_the_shipped_idle_animation_is_a_single_frame():
    """Two idle frames would blink on their own, which the behavior owns."""
    pet = load_pet_by_id("hello-kitty", ASSETS)
    assert pet.animation_for(PetState.IDLE).frame_count == 1


def test_a_state_without_variants_never_blinks(tmp_path):
    renderer = synthetic_renderer(tmp_path)
    renderer.set_state(PetState.WORKING, now=0.0)
    drawn = {renderer.current_text(moment / 10) for moment in range(60)}
    assert all("WORK" in text for text in drawn)
    assert len(drawn) == 1, "no variant means the working frame is all there is"


def test_blink_and_hop_both_still_run_in_the_state_that_has_them(tmp_path):
    hopping = SYNTHETIC_BEHAVIOR.replace(
        """  jump:
    enabled: false""",
        """  jump:
    enabled: true
    min_interval_ms: 2000
    max_interval_ms: 2000
    min_duration_ms: 1000
    max_duration_ms: 1000
    min_height_rows: 1
    max_height_rows: 1
    blink_convergence_chance: 0.0""",
    )
    renderer = synthetic_renderer(tmp_path, behavior=hopping)
    renderer.set_state(PetState.IDLE, now=0.0)

    frames = {}
    moment = 0.0
    while moment < 6.0:
        frames[round(moment, 2)] = renderer.current_text(moment)
        moment += 0.05

    blinked = any(" IDLE - " in text for text in frames.values())
    grounded = frames[0.0]
    hopped = any(
        text.splitlines()[-1].strip() == "" and text != grounded for text in frames.values()
    )
    assert blinked, "the declared state must still blink"
    assert hopped, "the hop applies whatever the state"
    assert {dimensions(text) for text in frames.values()} == {dimensions(grounded)}


def test_the_hop_applies_to_a_state_without_variants_too(tmp_path):
    hopping = SYNTHETIC_BEHAVIOR.replace(
        """  blink:
    enabled: true""",
        """  blink:
    enabled: false""",
    ).replace(
        """  jump:
    enabled: false""",
        """  jump:
    enabled: true
    min_interval_ms: 1000
    max_interval_ms: 1000
    min_duration_ms: 1000
    max_duration_ms: 1000
    min_height_rows: 1
    max_height_rows: 1""",
    )
    renderer = synthetic_renderer(tmp_path, behavior=hopping)
    renderer.set_state(PetState.WORKING, now=0.0)
    grounded = renderer.current_text(0.0)
    lifted = [renderer.current_text(m / 20) for m in range(60)]
    assert any(text != grounded for text in lifted)
    assert all("WORK" in text for text in lifted)
    assert {dimensions(text) for text in lifted} == {dimensions(grounded)}


def test_switching_state_keeps_the_canvas_and_the_behavior_schedule(tmp_path):
    renderer = synthetic_renderer(tmp_path)
    before = (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at)
    size = dimensions(renderer.current_text(0.0))
    renderer.set_state(PetState.WORKING, now=0.5)
    assert (renderer.behavior.next_blink_at, renderer.behavior.next_jump_at) == before
    assert dimensions(renderer.current_text(0.5)) == size


# --- anchor ----------------------------------------------------------------


def test_anchor_decides_where_the_padding_goes():
    frames = {"tall": PetFrame(("aaa", "bbb", "ccc")), "short": PetFrame(("xxx",))}
    top = normalize_canvas(frames, anchor="top")
    bottom = normalize_canvas(frames, anchor="bottom")
    assert top["short"] == ("xxx", "   ", "   ")
    assert bottom["short"] == ("   ", "   ", "xxx")
    assert top["tall"] == bottom["tall"] == ("aaa", "bbb", "ccc")


def test_top_and_bottom_anchors_place_the_drawing_differently(tmp_path):
    """Same content, same canvas, different vertical placement."""
    tall = SYNTHETIC_BEHAVIOR.replace("anchor: bottom", "anchor: top")
    bottom = synthetic_renderer(tmp_path / "b")
    top = synthetic_renderer(tmp_path / "t", behavior=tall)

    # The working animation is one line shorter than the idle canvas, so where
    # its padding lands is exactly what the anchor decides.
    for renderer in (bottom, top):
        renderer.set_state(PetState.WORKING, now=0.0)
    bottom_lines = bottom.current_text(0.0).splitlines()
    top_lines = top.current_text(0.0).splitlines()

    assert dimensions(bottom.current_text(0.0)) == dimensions(top.current_text(0.0))
    assert bottom_lines != top_lines
    assert bottom_lines.index(" WORK ! ") > top_lines.index(" WORK ! ")


def test_the_shipped_asset_anchors_to_the_bottom():
    pet = load_pet_by_id("hello-kitty", ASSETS)
    assert pet.behavior.document.render.anchor == "bottom"
    lines = hello_kitty().current_text(0.0).splitlines()
    assert is_blank_row(lines[0]), "headroom for the hop sits above the drawing"
    assert not is_blank_row(lines[-1]), "and the character stands on the floor"


# --- a blink must not freeze a multi-frame animation ------------------------

MULTIFRAME_MANIFEST = """schema_version: 1
id: multiframe
name: Multiframe
animations:
  idle:
    fps: 2
    loop: true
"""

MULTIFRAME_BEHAVIOR = """schema_version: 1
id: multiframe

micro_animations:
  blink:
    enabled: true
    min_interval_ms: 1000
    max_interval_ms: 1000
    min_duration_ms: 400
    max_duration_ms: 400
    double_blink_chance: 0.0
  jump:
    enabled: false

render:
  anchor: bottom
  preserve_canvas_height: true
  variants:
    idle:
      closed: base/shut.txt
"""

MULTIFRAME_FRAMES = {
    "idle/01.txt": "       \n SEM-A \n ===== ",
    "idle/02.txt": "       \n SEM-B \n ===== ",
    "base/shut.txt": "       \n CLOSED\n ===== ",
}


def multiframe_renderer(tmp_path: Path, *, behavior: str = MULTIFRAME_BEHAVIOR):
    """A pet whose idle animation really has two distinct semantic frames."""
    directory = tmp_path / "multiframe"
    for relative, text in MULTIFRAME_FRAMES.items():
        # A behavior that declares no variant must not ship an undeclared
        # directory either: the loader is strict about that on purpose.
        if relative.startswith("base/") and "variants: {}" in behavior:
            continue
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    (directory / "pet.yaml").write_text(MULTIFRAME_MANIFEST, encoding="utf-8")
    (directory / "behavior.yaml").write_text(behavior, encoding="utf-8")
    pet = load_pet_by_id("multiframe", tmp_path)
    assert pet.animation_for(PetState.IDLE).frame_count == 2
    renderer = build_pet_renderer(pet, now=0.0, rng=random.Random(1))
    assert isinstance(renderer, MicroBehaviorRenderer)
    return renderer


def shown(renderer: MicroBehaviorRenderer, now: float) -> str:
    """Which of the three drawings is on screen right now."""
    text = renderer.current_text(now)
    for label in ("SEM-A", "SEM-B", "CLOSED"):
        if label in text:
            return label
    raise AssertionError(f"unexpected drawing at {now}: {text!r}")


def test_a_semantic_animation_keeps_running_under_a_micro_behavior(tmp_path):
    """The old bug: an 'open' variant replaced every frame, freezing SEM-A."""
    renderer = multiframe_renderer(tmp_path)
    # Frames last 0.5s and the blink runs from 1.0s to 1.4s.
    assert shown(renderer, 0.0) == "SEM-A"
    assert shown(renderer, 0.6) == "SEM-B"
    assert shown(renderer, 0.9) == "SEM-B"


def test_a_blink_borrows_the_frame_and_gives_it_back(tmp_path):
    renderer = multiframe_renderer(tmp_path)
    assert shown(renderer, 0.0) == "SEM-A"
    assert shown(renderer, 0.6) == "SEM-B"
    assert shown(renderer, 1.1) == "CLOSED"
    assert shown(renderer, 1.3) == "CLOSED"
    # 1.6s is inside the animation's fourth frame slot, so the animation must
    # come back where it would have been, not where it was when the blink began.
    assert shown(renderer, 1.6) == "SEM-B"
    assert shown(renderer, 2.1) == "SEM-A"


def test_a_blink_does_not_restart_the_animation(tmp_path):
    renderer = multiframe_renderer(tmp_path)
    before = renderer.started_at
    renderer.current_text(1.1)
    assert shown(renderer, 1.6) == "SEM-B"
    assert renderer.started_at == before
    assert renderer.animator.frame_index(1.6) == 1


def test_the_animation_advances_while_the_character_is_airborne(tmp_path):
    hopping = MULTIFRAME_BEHAVIOR.replace(
        """  blink:
    enabled: true""",
        """  blink:
    enabled: false""",
    ).replace(
        """  jump:
    enabled: false""",
        """  jump:
    enabled: true
    min_interval_ms: 500
    max_interval_ms: 500
    min_duration_ms: 2000
    max_duration_ms: 2000
    min_height_rows: 1
    max_height_rows: 1""",
    )
    renderer = multiframe_renderer(tmp_path, behavior=hopping)
    airborne: set[str] = set()
    moment = 0.0
    while moment < 3.0:
        # Draw first: the behavior only advances when it is asked for a frame.
        label = shown(renderer, moment)
        if renderer.behavior.vertical_offset(moment) > 0:
            airborne.add(label)
        moment += 0.05
    assert airborne == {"SEM-A", "SEM-B"}, "a hop must not freeze the animation"


def test_a_state_without_a_closed_frame_renders_only_the_animation(tmp_path):
    bare = MULTIFRAME_BEHAVIOR.replace(
        """  variants:
    idle:
      closed: base/shut.txt""",
        "  variants: {}",
    )
    renderer = multiframe_renderer(tmp_path, behavior=bare)
    assert renderer.blinks_in(PetState.IDLE) is False
    seen = {shown(renderer, moment / 20) for moment in range(120)}
    assert seen == {"SEM-A", "SEM-B"}, "no closed frame means no blink, ever"
