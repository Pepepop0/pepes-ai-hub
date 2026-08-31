"""Pet backend: definitions, text frames and passive animation.

A pet is a small terminal-safe animated identity shown in the Right Rail. This
module owns the asset format and the "which frame now?" question, and nothing
else. It has no dependency on Textual, on any TUI, or on a rendering loop:
callers ask for a frame and get a frame.

Terminal safety is a hard rule, enforced at load time:

* frames are plain terminal strings, never images, Sixel, Kitty graphics or
  ANSI escape sequences — the shape comes from characters alone;
* zero emoji;
* no control or format characters, so a frame can never move the cursor,
  reset colours or reorder text;
* whitespace is preserved verbatim, since it is part of the drawing;
* every frame in one animation has the same width and height, so a redraw
  never resizes the rail.

Asset layout on disk::

    assets/pets/<pet-id>/
        pet.yaml
        idle/01.txt
        idle/02.txt
        working/01.txt
        ...

Frames are ordered by filename. The TUI never sees any of this: it holds a
:class:`PetAnimator` and asks for :meth:`PetAnimator.current_frame`.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from .assets import default_pets_directory
from .behavior import (
    BEHAVIOR_MANIFEST,
    BehaviorDocument,
    BehaviorError,
    load_behavior_document,
)

PET_SCHEMA_VERSION = 1
PET_MANIFEST = "pet.yaml"
FRAME_SUFFIX = ".txt"
DEFAULT_FPS = 2.0
MAX_FRAME_DURATION = 60.0


class PetState(str, Enum):
    """What the pet is expressing.

    Deliberately *not* a runtime or service state: those live in
    :mod:`local_ai_stack.services` and :mod:`local_ai_stack.processes` and are
    translated here by :mod:`local_ai_stack.appearance`.

    ``THINKING`` and ``DOWNLOADING`` are declared but no pet is required to
    draw them: :data:`STATE_FALLBACKS` degrades any missing state to one the
    pet does define, so new states never break existing assets.
    """

    IDLE = "idle"
    WORKING = "working"
    THINKING = "thinking"
    DOWNLOADING = "downloading"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    SLEEPING = "sleeping"


STATE_FALLBACKS: Mapping[PetState, tuple[PetState, ...]] = {
    PetState.IDLE: (),
    PetState.WORKING: (PetState.IDLE,),
    PetState.THINKING: (PetState.WORKING, PetState.IDLE),
    PetState.DOWNLOADING: (PetState.WORKING, PetState.IDLE),
    PetState.SUCCESS: (PetState.IDLE,),
    PetState.WARNING: (PetState.IDLE,),
    PetState.ERROR: (PetState.WARNING, PetState.IDLE),
    PetState.SLEEPING: (PetState.IDLE,),
}

REQUIRED_STATES: tuple[PetState, ...] = (PetState.IDLE,)

# Ranges that carry emoji or force emoji presentation. Box drawing (U+2500),
# block elements (U+2580), geometric shapes (U+25A0) and braille (U+2800) stay
# allowed: they are monospace text, not pictographs.
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F000, 0x1FAFF),
)


class PetError(ValueError):
    """A pet asset is missing, malformed or not terminal-safe."""


def _error(source: Path | str, field: str, message: str) -> PetError:
    location = f"{source}: {field}" if field else str(source)
    return PetError(f"{location}: {message}")


def is_emoji(char: str) -> bool:
    codepoint = ord(char)
    return any(start <= codepoint <= end for start, end in EMOJI_RANGES)


def unsafe_characters(text: str) -> tuple[str, ...]:
    """Characters a frame may not contain, in order of first appearance."""
    offenders: list[str] = []
    for char in text:
        if char in offenders or char == "\n":
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"} or char == "\t" or is_emoji(char):
            offenders.append(char)
    return tuple(offenders)


def _describe(char: str) -> str:
    return f"U+{ord(char):04X}"


@dataclass(frozen=True)
class PetFrame:
    """One still picture: terminal lines, exactly as authored."""

    lines: tuple[str, ...]

    @property
    def height(self) -> int:
        return len(self.lines)

    @property
    def width(self) -> int:
        return max((len(line) for line in self.lines), default=0)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def padded_lines(self) -> tuple[str, ...]:
        """Lines right-padded to the frame width, for fixed-size rendering."""
        width = self.width
        return tuple(line.ljust(width) for line in self.lines)

    @classmethod
    def from_text(cls, text: str, source: Path | str = "<frame>") -> PetFrame:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
        normalized = normalized.removesuffix("\n")
        offenders = unsafe_characters(normalized)
        if offenders:
            raise _error(
                source,
                "",
                "frame contains characters that are not terminal-safe: "
                + ", ".join(_describe(char) for char in offenders),
            )
        if not normalized.strip():
            raise _error(source, "", "frame is empty")
        return cls(tuple(normalized.split("\n")))


@dataclass(frozen=True)
class PetAnimation:
    """An ordered set of frames plus how fast and whether it repeats."""

    state: PetState
    frames: tuple[PetFrame, ...]
    frame_duration: float = 1.0 / DEFAULT_FPS
    loop: bool = True

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def duration(self) -> float:
        return self.frame_duration * self.frame_count

    @property
    def width(self) -> int:
        return self.frames[0].width

    @property
    def height(self) -> int:
        return self.frames[0].height

    @property
    def fps(self) -> float:
        return 1.0 / self.frame_duration

    def frame_index_at(self, elapsed: float) -> int:
        """Deterministic frame index for a time offset, in seconds."""
        if elapsed <= 0 or self.frame_count == 1:
            return 0
        step = int(elapsed // self.frame_duration)
        if self.loop:
            return step % self.frame_count
        return min(step, self.frame_count - 1)

    def frame_at(self, elapsed: float) -> PetFrame:
        return self.frames[self.frame_index_at(elapsed)]

    def is_finished(self, elapsed: float) -> bool:
        """True once a non-looping animation has shown its last frame."""
        return not self.loop and elapsed >= self.duration - self.frame_duration


@dataclass(frozen=True)
class PetBehaviorAsset:
    """An optional ``behavior.yaml`` plus the frame variants it composes with.

    Purely declarative: the pet asset says it wants micro-animations, and for
    which semantic states it supplies alternate drawings. Deciding what to do
    with that belongs to :mod:`local_ai_stack.renderer`, so no consumer needs
    to special-case a pet id anywhere.
    """

    document: BehaviorDocument
    variants: Mapping[PetState, Mapping[str, PetFrame]]

    @property
    def frames(self) -> dict[str, PetFrame]:
        """Every variant drawing, keyed ``<state>:<eye>``."""
        return {
            f"{state.value}:{eye}": frame
            for state, eyes in self.variants.items()
            for eye, frame in eyes.items()
        }

    def variants_for(self, state: PetState) -> Mapping[str, PetFrame] | None:
        return self.variants.get(state)

    @property
    def width(self) -> int:
        return max((frame.width for frame in self.frames.values()), default=0)

    @property
    def height(self) -> int:
        return max((frame.height for frame in self.frames.values()), default=0)


@dataclass(frozen=True)
class PetDefinition:
    """Everything the rail needs to draw one pet."""

    schema_version: int
    id: str
    name: str
    description: str
    animations: Mapping[PetState, PetAnimation]
    source: Path | None = None
    behavior: PetBehaviorAsset | None = None

    @property
    def states(self) -> tuple[PetState, ...]:
        return tuple(sorted(self.animations, key=lambda state: state.value))

    def has_state(self, state: PetState) -> bool:
        return state in self.animations

    def animation_for(self, state: PetState) -> PetAnimation:
        """The animation for ``state``, degrading through :data:`STATE_FALLBACKS`."""
        for candidate in (state, *STATE_FALLBACKS.get(state, ()), PetState.IDLE):
            animation = self.animations.get(candidate)
            if animation is not None:
                return animation
        raise _error(self.source or self.id, "animations", "pet defines no usable animation")

    def frame_at(self, state: PetState, elapsed: float) -> PetFrame:
        return self.animation_for(state).frame_at(elapsed)


class PetAnimator:
    """Passive animation cursor over a pet.

    Holds no thread and no timer: the caller owns the clock. Textual will call
    :meth:`current_frame` from its own refresh timer; tests call it with an
    explicit ``now``, or step frames with :meth:`advance`.
    """

    def __init__(self, pet: PetDefinition, state: PetState = PetState.IDLE, *, now: float = 0.0):
        self._pet = pet
        self._state = state
        self._started_at = now

    @property
    def pet(self) -> PetDefinition:
        return self._pet

    @property
    def state(self) -> PetState:
        return self._state

    @property
    def animation(self) -> PetAnimation:
        return self._pet.animation_for(self._state)

    @property
    def started_at(self) -> float:
        return self._started_at

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def set_state(
        self, state: PetState, *, now: float | None = None, restart: bool = False
    ) -> bool:
        """Switch state, restarting the animation. Returns whether it changed.

        Re-setting the current state keeps the animation running, so a caller
        may push the same state on every refresh without freezing frame 1.
        """
        if state is self._state and not restart:
            return False
        self._state = state
        self._started_at = self._now(now)
        return True

    def restart(self, *, now: float | None = None) -> None:
        self._started_at = self._now(now)

    def elapsed(self, now: float | None = None) -> float:
        return max(0.0, self._now(now) - self._started_at)

    def current_frame(self, now: float | None = None) -> PetFrame:
        return self.animation.frame_at(self.elapsed(now))

    def current_text(self, now: float | None = None) -> str:
        return self.current_frame(now).text

    def frame_index(self, now: float | None = None) -> int:
        return self.animation.frame_index_at(self.elapsed(now))

    def is_finished(self, now: float | None = None) -> bool:
        return self.animation.is_finished(self.elapsed(now))

    def advance(self, steps: int = 1, *, now: float | None = None) -> PetFrame:
        """Step ``steps`` frames without waiting, then report the new frame."""
        self._started_at -= steps * self.animation.frame_duration
        return self.current_frame(now)


def _mapping(value: Any, source: Path | str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(source, field, "must be a mapping")
    return value


def _string(value: Any, source: Path | str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(source, field, "must be a non-empty string")
    return value


def _positive_number(value: Any, source: Path | str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _error(source, field, "must be a number > 0")
    return float(value)


def _frame_duration(config: dict[str, Any], source: Path | str, field: str) -> float:
    has_fps = "fps" in config
    has_duration = "frame_duration_ms" in config
    if has_fps and has_duration:
        raise _error(source, field, "set either fps or frame_duration_ms, not both")
    if has_fps:
        duration = 1.0 / _positive_number(config["fps"], source, f"{field}.fps")
    elif has_duration:
        milliseconds = _positive_number(
            config["frame_duration_ms"], source, f"{field}.frame_duration_ms"
        )
        duration = milliseconds / 1000.0
    else:
        duration = 1.0 / DEFAULT_FPS
    if duration > MAX_FRAME_DURATION:
        raise _error(source, field, f"frame duration must be <= {MAX_FRAME_DURATION:.0f}s")
    return duration


def build_animation(
    state: PetState,
    frames: Sequence[PetFrame],
    *,
    frame_duration: float = 1.0 / DEFAULT_FPS,
    loop: bool = True,
    source: Path | str = "<animation>",
) -> PetAnimation:
    """Validate a set of frames and wrap them into an animation."""
    if not frames:
        raise _error(source, state.value, "animation needs at least one frame")
    dimensions = {(frame.height, frame.width) for frame in frames}
    if len(dimensions) > 1:
        sizes = ", ".join(f"{width}x{height}" for height, width in sorted(dimensions))
        raise _error(source, state.value, f"frames must share one size, found {sizes}")
    return PetAnimation(state, tuple(frames), frame_duration, loop)


def _read_frames(directory: Path) -> tuple[PetFrame, ...]:
    paths = sorted(p for p in directory.glob(f"*{FRAME_SUFFIX}") if p.is_file())
    if not paths:
        raise _error(directory, "", f"no *{FRAME_SUFFIX} frames found")
    return tuple(PetFrame.from_text(path.read_text(encoding="utf-8"), path) for path in paths)


def validate_pet_data(
    data: Any, directory: Path, source: Path | str | None = None
) -> PetDefinition:
    """Validate a parsed ``pet.yaml`` and load the frames it declares."""
    source = source or directory / PET_MANIFEST
    root = _mapping(data, source, "pet root")
    allowed = {"schema_version", "id", "name", "description", "animations"}
    unknown_root = sorted(str(key) for key in root if str(key) not in allowed)
    if unknown_root:
        raise _error(source, "pet root", f"unknown fields: {', '.join(unknown_root)}")
    missing = [key for key in ("schema_version", "id", "name", "animations") if key not in root]
    if missing:
        raise _error(source, "pet root", f"missing required fields: {', '.join(missing)}")

    schema_version = root["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise _error(source, "schema_version", "must be an integer")
    if schema_version != PET_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {schema_version}; expected {PET_SCHEMA_VERSION}",
        )

    pet_id = _string(root["id"], source, "id")
    if pet_id != directory.name:
        raise _error(source, "id", f"must match the directory name {directory.name!r}")

    animations_data = _mapping(root["animations"], source, "animations")
    if not animations_data:
        raise _error(source, "animations", "must declare at least one state")

    animations: dict[PetState, PetAnimation] = {}
    for raw_state, raw_config in animations_data.items():
        try:
            state = PetState(str(raw_state))
        except ValueError:
            known = ", ".join(s.value for s in PetState)
            raise _error(
                source, f"animations.{raw_state}", f"unknown state (known: {known})"
            ) from None
        config = _mapping(raw_config, source, f"animations.{state.value}")
        unknown = sorted(key for key in config if key not in {"fps", "frame_duration_ms", "loop"})
        if unknown:
            raise _error(
                source, f"animations.{state.value}", f"unknown fields: {', '.join(unknown)}"
            )
        loop = config.get("loop", True)
        if not isinstance(loop, bool):
            raise _error(source, f"animations.{state.value}.loop", "must be a boolean")
        state_directory = directory / state.value
        if not state_directory.is_dir():
            raise _error(
                source, f"animations.{state.value}", "declared but frame directory missing"
            )
        animations[state] = build_animation(
            state,
            _read_frames(state_directory),
            frame_duration=_frame_duration(config, source, f"animations.{state.value}"),
            loop=loop,
            source=source,
        )

    behavior = _load_behavior_asset(directory)
    declared_directories = {state.value for state in animations}
    if behavior is not None:
        declared_directories |= {
            Path(relative).parts[0]
            for relative in behavior.document.render.frames.values()
            if len(Path(relative).parts) > 1
        }

    undeclared = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and entry.name not in declared_directories
    )
    if undeclared:
        raise _error(
            source, "animations", f"frame directories not declared: {', '.join(undeclared)}"
        )

    missing_required = [s.value for s in REQUIRED_STATES if s not in animations]
    if missing_required:
        raise _error(
            source, "animations", f"missing required states: {', '.join(missing_required)}"
        )

    return PetDefinition(
        schema_version,
        pet_id,
        _string(root["name"], source, "name"),
        str(root.get("description", "")).strip(),
        animations,
        directory,
        behavior,
    )


def _load_behavior_asset(directory: Path) -> PetBehaviorAsset | None:
    """Load ``behavior.yaml`` and its frames, if the asset declares one.

    The frames go through the same terminal-safety validation as animation
    frames, and must share one canvas size so a redraw can never resize the
    rail.
    """
    manifest = directory / BEHAVIOR_MANIFEST
    if not manifest.is_file():
        return None
    try:
        document = load_behavior_document(manifest)
    except BehaviorError as exc:
        # load_pet() promises PetError, so callers keep one thing to catch and
        # a broken behavior file degrades like any other broken asset.
        raise _error(manifest, "", str(exc)) from exc
    if document.id != directory.name:
        raise _error(manifest, "id", f"must match the directory name {directory.name!r}")
    variants: dict[PetState, dict[str, PetFrame]] = {}
    for raw_state, eyes in document.render.variants.items():
        field = f"render.variants.{raw_state}"
        try:
            state = PetState(str(raw_state))
        except ValueError:
            known = ", ".join(s.value for s in PetState)
            raise _error(manifest, field, f"unknown state (known: {known})") from None
        loaded: dict[str, PetFrame] = {}
        for eye, relative in eyes.items():
            path = directory / relative
            if not path.is_file():
                raise _error(manifest, f"{field}.{eye}", f"frame not found: {relative}")
            loaded[eye] = PetFrame.from_text(path.read_text(encoding="utf-8"), path)
        variants[state] = loaded
    asset = PetBehaviorAsset(document, variants)
    sizes = {(frame.height, frame.width) for frame in asset.frames.values()}
    if len(sizes) > 1:
        rendered = ", ".join(f"{width}x{height}" for height, width in sorted(sizes))
        raise _error(manifest, "render", f"frames must share one size, found {rendered}")
    return asset


def load_pet(directory: Path) -> PetDefinition:
    """Load one pet from its asset directory."""
    manifest = directory / PET_MANIFEST
    if not manifest.is_file():
        raise _error(directory, "", f"missing {PET_MANIFEST}")
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    return validate_pet_data(data, directory, manifest)


def load_pets(directory: Path | None = None) -> tuple[PetDefinition, ...]:
    """Load every pet in ``directory`` (default: the bundled asset directory)."""
    root = default_pets_directory() if directory is None else directory
    if not root.is_dir():
        raise _error(root, "", "pet directory not found")
    return tuple(load_pet(entry) for entry in sorted(root.iterdir()) if entry.is_dir())


def available_pets(directory: Path | None = None) -> tuple[str, ...]:
    """Pet ids that can be loaded right now, without loading them."""
    root = default_pets_directory() if directory is None else directory
    if not root.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in root.iterdir() if (entry / PET_MANIFEST).is_file()))


def load_pet_by_id(pet_id: str, directory: Path | None = None) -> PetDefinition:
    root = default_pets_directory() if directory is None else directory
    candidate = root / pet_id
    if not (candidate / PET_MANIFEST).is_file():
        known = ", ".join(available_pets(root)) or "none"
        raise _error(root, pet_id, f"unknown pet (available: {known})")
    return load_pet(candidate)


def frames_are_terminal_safe(frames: Iterable[PetFrame]) -> bool:
    """True when no frame carries emoji, control or format characters."""
    return not any(unsafe_characters(frame.text) for frame in frames)
