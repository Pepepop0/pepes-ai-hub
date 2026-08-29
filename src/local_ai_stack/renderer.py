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


def normalize_canvas(frames: Mapping[str, PetFrame]) -> dict[str, tuple[str, ...]]:
    """One shared canvas for every frame, computed once and not per tick."""
    if not frames:
        return {}
    width = max(frame.width for frame in frames.values())
    height = max(frame.height for frame in frames.values())
    canvases: dict[str, tuple[str, ...]] = {}
    for name, frame in frames.items():
        lines = [line.ljust(width) for line in frame.lines]
        lines.extend([" " * width] * (height - len(lines)))
        canvases[name] = tuple(lines)
    return canvases


class MicroBehaviorRenderer:
    """A pet drawn by composing behavior channels over its base frames.

    Owns no clock and no state beyond the behavior itself; ``PetState`` still
    lives in the wrapped animator, so blink and jump never leak into it.
    """

    def __init__(
        self,
        animator: PetAnimator,
        frames: Mapping[str, PetFrame],
        *,
        config: BehaviorConfig | None = None,
        render: RenderConfig | None = None,
        now: float | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._animator = animator
        self._canvases = normalize_canvas(frames)
        if EyeState.OPEN.value not in self._canvases:
            raise ValueError("a micro-behavior renderer needs at least an 'open' frame")
        self._render = render or RenderConfig()
        self.behavior = MicroBehavior(
            config, now=animator.started_at if now is None else now, rng=rng
        )

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
        lines = self._canvases[EyeState.OPEN.value]
        return len(lines[0]) if lines else 0, len(lines)

    def current_lines(self, now: float | None = None) -> tuple[str, ...]:
        moment = time.monotonic() if now is None else now
        snapshot = self.behavior.snapshot(moment)
        key = snapshot.eyes.value
        lines = self._canvases.get(key, self._canvases[EyeState.OPEN.value])
        if not self._render.preserve_canvas_height:
            return lines
        return raise_drawing(lines, snapshot.vertical_offset_rows)

    def current_frame(self, now: float | None = None) -> PetFrame:
        return PetFrame(self.current_lines(now))

    def current_text(self, now: float | None = None) -> str:
        return "\n".join(self.current_lines(now))


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
        behavior.frames,
        config=behavior.document.config,
        render=behavior.document.render,
        now=moment,
        rng=rng,
    )
