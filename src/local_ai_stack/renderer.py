"""Turning a pet plus a clock into the one string the rail draws.

The rail asks a renderer for text and knows nothing else — not the asset layout,
not whether the pet blinks, not which profile is selected. Which renderer a pet
gets is decided by the *asset*: a pet that ships a ``behavior.yaml`` composes
its frame from micro-behaviors, and every other pet keeps the plain animator.
So adding a third pet with its own behavior needs no code change here and no
``if profile_id == ...`` anywhere.

Every renderer is passive: it holds no thread and no timer, and produces a frame
only when asked with a ``now`` the caller owns.

The jump never changes the size of the drawing. The art is raised by dropping
blank rows from the top and putting them back at the bottom, so width and height
are constant by construction, and a hop that would clip visible art is simply
shortened instead.
"""

from __future__ import annotations

import random
import time
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from .behavior import BehaviorConfig, EyeState, MicroBehavior, RenderConfig
from .pets import PetAnimation, PetAnimator, PetDefinition, PetFrame, PetState

# U+2800 BRAILLE PATTERN BLANK draws nothing but is not whitespace, so braille
# art pads with it. Both count as an empty row for the purpose of a hop.
BLANK_CHARACTERS = " ⠀"
BRAILLE_BLANK = "⠀"
BRAILLE_BASE = 0x2800
BRAILLE_LAST = 0x28FF
# A braille cell is a 2x4 dot matrix, and the dot-to-bit mapping is not linear:
# dots 1-6 fill the first three rows column by column, then 7 and 8 close it.
DOT_BITS: dict[tuple[int, int], int] = {
    (0, 0): 0x01,
    (1, 0): 0x02,
    (2, 0): 0x04,
    (3, 0): 0x40,
    (0, 1): 0x08,
    (1, 1): 0x10,
    (2, 1): 0x20,
    (3, 1): 0x80,
}
CELL_DOT_ROWS = 4
CELL_DOT_COLUMNS = 2


def braille_to_dots(lines: Sequence[str]) -> list[list[bool]]:
    """Explode braille cells into the dot bitmap they encode."""
    if not lines:
        return []
    width = max(len(line) for line in lines)
    grid = [[False] * (width * CELL_DOT_COLUMNS) for _ in range(len(lines) * CELL_DOT_ROWS)]
    for row, line in enumerate(lines):
        for column, char in enumerate(line.ljust(width)):
            value = ord(char) - BRAILLE_BASE
            if not 0 <= value <= 0xFF:
                continue
            for (dot_row, dot_column), bit in DOT_BITS.items():
                if value & bit:
                    grid[row * CELL_DOT_ROWS + dot_row][column * CELL_DOT_COLUMNS + dot_column] = (
                        True
                    )
    return grid


def dots_to_braille(grid: Sequence[Sequence[bool]]) -> tuple[str, ...]:
    """Pack a dot bitmap back into braille cells."""
    height = len(grid)
    width = len(grid[0]) if height else 0
    rows: list[str] = []
    for row in range(0, height, CELL_DOT_ROWS):
        cells: list[str] = []
        for column in range(0, width, CELL_DOT_COLUMNS):
            value = 0
            for (dot_row, dot_column), bit in DOT_BITS.items():
                y, x = row + dot_row, column + dot_column
                if y < height and x < width and grid[y][x]:
                    value |= bit
            cells.append(chr(BRAILLE_BASE + value))
        rows.append("".join(cells))
    return tuple(rows)


def ink_bounds(grid: Sequence[Sequence[bool]]) -> tuple[int, int, int, int] | None:
    """Bounding box of every lit dot, or None when the bitmap is empty."""
    rows = [y for y, row in enumerate(grid) if any(row)]
    if not rows:
        return None
    width = len(grid[0])
    columns = [x for x in range(width) if any(row[x] for row in grid)]
    return rows[0], rows[-1], columns[0], columns[-1]


def crop_to_ink(grid: Sequence[Sequence[bool]]) -> list[list[bool]]:
    bounds = ink_bounds(grid)
    if bounds is None:
        return [list(row) for row in grid]
    top, bottom, left, right = bounds
    return [list(row[left : right + 1]) for row in grid[top : bottom + 1]]


def downscale_dots(
    grid: Sequence[Sequence[bool]], factor: int, threshold: int | None = None
) -> list[list[bool]]:
    """Shrink a dot bitmap by an integer factor, keeping the aspect ratio.

    A block of ``factor**2`` dots becomes one dot when enough of it is lit, so
    thin strokes survive and solid areas stay solid.
    """
    if factor <= 1 or not grid:
        return [list(row) for row in grid]
    height, width = len(grid), len(grid[0])
    limit = max(1, factor * factor // 2) if threshold is None else threshold
    scaled: list[list[bool]] = []
    for y in range(0, height, factor):
        row: list[bool] = []
        for x in range(0, width, factor):
            lit = sum(
                1
                for dy in range(factor)
                for dx in range(factor)
                if y + dy < height and x + dx < width and grid[y + dy][x + dx]
            )
            row.append(lit >= limit)
        scaled.append(row)
    return scaled


def scale_to_width(
    frames: Mapping[str, Sequence[str]], max_width: int, *, crop: bool = True
) -> dict[str, tuple[str, ...]]:
    """Fit braille art into ``max_width`` cells, identically for every frame.

    Cropping and scaling are computed once across all frames together, so the
    drawings keep their alignment and a blink cannot shift the character.
    """
    grids = {name: braille_to_dots(lines) for name, lines in frames.items()}
    if crop:
        union = _union_bounds(grids.values())
        if union is not None:
            top, bottom, left, right = union
            grids = {
                name: [list(row[left : right + 1]) for row in grid[top : bottom + 1]]
                for name, grid in grids.items()
            }
    dot_width = max((len(grid[0]) for grid in grids.values() if grid), default=0)
    cells_per_row = dot_width / CELL_DOT_COLUMNS
    factor = 1
    while cells_per_row / factor > max_width:
        factor += 1
    return {name: dots_to_braille(downscale_dots(grid, factor)) for name, grid in grids.items()}


def _union_bounds(grids) -> tuple[int, int, int, int] | None:
    boxes = [box for box in (ink_bounds(grid) for grid in grids) if box is not None]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        max(box[1] for box in boxes),
        min(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


@runtime_checkable
class PetRenderer(Protocol):
    """What the rail needs. :class:`PetAnimator` already satisfies it."""

    @property
    def pet(self) -> PetDefinition: ...

    @property
    def state(self) -> PetState: ...

    @property
    def started_at(self) -> float: ...

    def current_text(self, now: float | None = None) -> str: ...

    def set_state(
        self, state: PetState, *, now: float | None = None, restart: bool = False
    ) -> bool: ...


def is_blank_row(line: str) -> bool:
    """True for a row that draws nothing, whitespace or braille blank alike."""
    return not line.strip(BLANK_CHARACTERS)


def blank_rows_at_top(lines: Sequence[str]) -> int:
    count = 0
    for line in lines:
        if not is_blank_row(line):
            break
        count += 1
    return count


def raise_drawing(lines: Sequence[str], rows: int) -> tuple[str, ...]:
    """Lift the art by ``rows``, reusing its own blank rows as the new floor.

    Never clips: the lift is capped by the blank headroom actually available, so
    a taller hop on a full canvas becomes a shorter hop, not a cropped drawing.
    Width and height are unchanged because the same rows come back at the end.
    """
    if rows <= 0 or not lines:
        return tuple(lines)
    lift = min(rows, blank_rows_at_top(lines))
    if lift == 0:
        return tuple(lines)
    return tuple(lines[lift:]) + tuple(lines[:lift])


def normalize_canvas(
    frames: Mapping[str, PetFrame], *, anchor: str = "top"
) -> dict[str, tuple[str, ...]]:
    """One shared canvas for every frame, computed once and not per tick.

    ``anchor`` decides where the padding goes when a frame is shorter than the
    canvas: ``top`` keeps the drawing against the ceiling and pads below,
    ``bottom`` keeps it on the floor and pads above.
    """
    if not frames:
        return {}
    width = max(frame.width for frame in frames.values())
    height = max(frame.height for frame in frames.values())
    canvases: dict[str, tuple[str, ...]] = {}
    for name, frame in frames.items():
        lines = [line.ljust(width) for line in frame.lines]
        padding = [" " * width] * (height - len(lines))
        canvases[name] = tuple(padding + lines) if anchor == "bottom" else tuple(lines + padding)
    return canvases


class MicroBehaviorRenderer:
    """A pet whose semantic animation is *decorated* by micro-behaviors.

    The frame to draw always comes from the wrapped :class:`PetAnimator`, so
    ``PetState`` keeps choosing what is shown. On top of that:

    * blink swaps in an eyes-closed drawing, but only for the states the asset
      declares a variant for. A state with no variant keeps its own frame and
      simply does not blink;
    * the hop lifts whatever frame is showing inside a fixed canvas, so it
      applies to every state without the asset having to say anything.

    Blink and jump therefore add to the semantic animation and never replace
    it, and nothing here knows which pet or profile it is drawing.
    """

    def __init__(
        self,
        animator: PetAnimator,
        variants: Mapping[PetState, Mapping[str, PetFrame]] | None = None,
        *,
        config: BehaviorConfig | None = None,
        render: RenderConfig | None = None,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._animator = animator
        self._render = render or RenderConfig()
        self._variants = {state: dict(eyes) for state, eyes in (variants or {}).items()}
        self._fit_width: int | None = None
        self._canvases = self._prepare()
        self.behavior = MicroBehavior(
            config, now=animator.started_at if now is None else now, rng=rng
        )

    # --- canvas ------------------------------------------------------------

    def _source_frames(self) -> dict[str, PetFrame]:
        """Every drawing this renderer might show, semantic frames included.

        Animation frames and variant frames share one canvas, so switching
        state or blinking can never change the size of the drawing.
        """
        frames: dict[str, PetFrame] = {}
        for state, animation in self._animator.pet.animations.items():
            for index, frame in enumerate(animation.frames):
                frames[_animation_key(state, index)] = frame
        for state, eyes in self._variants.items():
            for eye, frame in eyes.items():
                frames[_variant_key(state, eye)] = frame
        return frames

    def _target_width(self) -> int | None:
        """The narrower of what the asset asks for and what the caller offers."""
        declared, available = self._render.max_width, self._fit_width
        if declared is None:
            return available
        return declared if available is None else min(declared, available)

    def _prepare(self) -> dict[str, tuple[str, ...]]:
        """Fit the art to the canvas once, not on every tick."""
        anchor = self._render.anchor
        canvases = normalize_canvas(self._source_frames(), anchor=anchor)
        if not canvases:
            raise ValueError("a micro-behavior renderer needs at least one frame to draw")
        target = self._target_width()
        if target is not None:
            canvases = scale_to_width(canvases, target, crop=self._render.crop_to_ink)
            canvases = normalize_canvas(
                {name: PetFrame(lines) for name, lines in canvases.items()}, anchor=anchor
            )
        headroom = self._render.headroom_rows
        if headroom:
            width = max((len(lines[0]) for lines in canvases.values() if lines), default=0)
            blank = BRAILLE_BLANK * width
            # Headroom is the room the hop borrows, so it always sits on top,
            # whatever the anchor decided to do with the height padding.
            canvases = {name: (blank,) * headroom + lines for name, lines in canvases.items()}
        return canvases

    def fit_to_width(self, cells: int) -> bool:
        """Re-fit the art to the space the view actually has.

        Called on resize, never per tick. Returns whether anything changed, so
        the caller can skip a redraw.
        """
        width = max(1, int(cells))
        if width == self._fit_width:
            return False
        self._fit_width = width
        self._canvases = self._prepare()
        return True

    # --- the animator-shaped surface the rail already speaks ---------------

    @property
    def animator(self) -> PetAnimator:
        return self._animator

    @property
    def pet(self) -> PetDefinition:
        return self._animator.pet

    @property
    def state(self) -> PetState:
        return self._animator.state

    @property
    def animation(self) -> PetAnimation:
        return self._animator.animation

    @property
    def started_at(self) -> float:
        return self._animator.started_at

    def set_state(
        self, state: PetState, *, now: float | None = None, restart: bool = False
    ) -> bool:
        """Change the semantic state. Micro-behavior schedules keep running."""
        return self._animator.set_state(state, now=now, restart=restart)

    def restart(self, *, now: float | None = None) -> None:
        self._animator.restart(now=now)

    def elapsed(self, now: float | None = None) -> float:
        return self._animator.elapsed(now)

    # --- drawing -----------------------------------------------------------

    @property
    def canvas_size(self) -> tuple[int, int]:
        lines = next(iter(self._canvases.values()))
        return len(lines[0]) if lines else 0, len(lines)

    def blinks_in(self, state: PetState) -> bool:
        """Whether the asset supplies an eyes-closed drawing for ``state``."""
        animation = self._animator.pet.animation_for(state)
        eyes = self._variants.get(animation.state)
        return bool(eyes) and EyeState.CLOSED.value in eyes

    def _semantic_key(self, now: float) -> str:
        """The frame the animation itself would show right now."""
        animation = self._animator.animation
        return _animation_key(animation.state, self._animator.frame_index(now))

    def _frame_key(self, now: float, blinking: bool) -> str:
        animation = self._animator.animation
        eyes = self._variants.get(animation.state)
        if not eyes:
            return self._semantic_key(now)
        eye = EyeState.CLOSED.value if blinking else EyeState.OPEN.value
        key = _variant_key(animation.state, eye)
        return key if key in self._canvases else self._semantic_key(now)

    def current_lines(self, now: float | None = None) -> tuple[str, ...]:
        moment = time.monotonic() if now is None else now
        snapshot = self.behavior.snapshot(moment)
        lines = self._canvases[self._frame_key(moment, snapshot.blinking)]
        if not self._render.preserve_canvas_height:
            return lines
        return raise_drawing(lines, snapshot.vertical_offset_rows)

    def current_frame(self, now: float | None = None) -> PetFrame:
        return PetFrame(self.current_lines(now))

    def current_text(self, now: float | None = None) -> str:
        return "\n".join(self.current_lines(now))


def _animation_key(state: PetState, index: int) -> str:
    return f"anim:{state.value}:{index}"


def _variant_key(state: PetState, eye: str) -> str:
    return f"var:{state.value}:{eye}"


def build_pet_renderer(
    pet: PetDefinition,
    state: PetState = PetState.IDLE,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> PetRenderer:
    """Pick a renderer from what the asset declares, never from who asked."""
    moment = time.monotonic() if now is None else now
    animator = PetAnimator(pet, state, now=moment)
    behavior = pet.behavior
    if behavior is None:
        return animator
    return MicroBehaviorRenderer(
        animator,
        behavior.variants,
        config=behavior.document.config,
        render=behavior.document.render,
        now=moment,
        rng=rng,
    )
