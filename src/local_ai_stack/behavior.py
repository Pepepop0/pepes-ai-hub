"""Micro-behaviors: small stochastic movements layered on top of a pet's art.

A pet's :class:`~local_ai_stack.pets.PetState` says what the *system* is doing.
A micro-behavior says what the *character* is doing while that stays true —
blinking, hopping — and is deliberately not part of ``PetState``: blink and jump
are not states of the stack, and turning them into ones would make every
consumer of ``PetState`` care about eyelids.

Two rules keep this honest:

**Independent channels.** Blink and jump each own their schedule and draw their
own delays. They are never steps of one shared timeline, so they drift in and
out of phase on their own; ``blink_convergence_chance`` only adds an extra
nudge when a jump begins. The result is a character that reads as alive rather
than as a looping GIF.

**Passive.** No thread, no timer, no sleep, no event loop. The caller owns the
clock and passes ``now`` (monotonic seconds); calling twice with the same
``now`` yields the same answer and advances nothing. The RNG is injectable, so
a seeded run is exactly reproducible.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

BEHAVIOR_MANIFEST = "behavior.yaml"
BEHAVIOR_SCHEMA_VERSION = 1
DOUBLE_BLINK_MIN_GAP_S = 0.08
DOUBLE_BLINK_MAX_GAP_S = 0.16


class EyeState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class JumpPhase(str, Enum):
    """Where a hop is in its arc. Purely visual; never a ``PetState``."""

    GROUND = "ground"
    CROUCH = "crouch"
    UP = "up"
    APEX = "apex"
    DOWN = "down"
    LAND = "land"


class BehaviorError(ValueError):
    """A ``behavior.yaml`` is malformed or asks for something impossible."""


def _error(source: Path | str, field_name: str, message: str) -> BehaviorError:
    location = f"{source}: {field_name}" if field_name else str(source)
    return BehaviorError(f"{location}: {message}")


@dataclass(frozen=True)
class BlinkConfig:
    enabled: bool = True
    min_interval_s: float = 1.8
    max_interval_s: float = 4.5
    min_duration_s: float = 0.12
    max_duration_s: float = 0.22
    double_blink_chance: float = 0.10


@dataclass(frozen=True)
class JumpConfig:
    enabled: bool = True
    min_interval_s: float = 2.5
    max_interval_s: float = 6.5
    min_duration_s: float = 0.40
    max_duration_s: float = 0.70
    min_height_rows: int = 1
    max_height_rows: int = 2
    blink_convergence_chance: float = 0.25
    # Share of the hop spent at the top. Raising it buys hang time without
    # making the whole jump slower.
    hang_ratio: float = 0.24


@dataclass(frozen=True)
class RenderConfig:
    """How a behavior draws: which frame variants it owns, and how they fit.

    ``variants`` maps a semantic state name to the drawings that replace the
    animation's own frame while that state is showing — for now an eyes-open
    and an eyes-closed version. A state the asset says nothing about keeps the
    frame its animation produces, so blink is an *addition* to the semantic
    animation and never a replacement for it.

    ``anchor`` decides where padding goes when a frame is shorter than the
    canvas: ``bottom`` keeps the drawing on the floor, ``top`` keeps it at the
    ceiling. ``max_width`` lets an asset ship art at full resolution and still
    fit a narrow rail, and ``headroom_rows`` reserves blank rows above the
    drawing for the hop, which matters once cropping removed the art's own
    empty margin.
    """

    variants: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    anchor: str = "bottom"
    preserve_canvas_height: bool = True
    max_width: int | None = None
    crop_to_ink: bool = False
    headroom_rows: int = 0

    @property
    def frames(self) -> dict[str, str]:
        """Every declared drawing, keyed ``<state>:<eye>``."""
        return {
            f"{state}:{eye}": path
            for state, eyes in self.variants.items()
            for eye, path in eyes.items()
        }


@dataclass(frozen=True)
class BehaviorConfig:
    blink: BlinkConfig = field(default_factory=BlinkConfig)
    jump: JumpConfig = field(default_factory=JumpConfig)


@dataclass(frozen=True)
class BehaviorDocument:
    """A parsed ``behavior.yaml``: timings plus which frames to compose."""

    schema_version: int
    id: str
    config: BehaviorConfig
    render: RenderConfig
    source: Path | None = None


@dataclass(frozen=True)
class BehaviorSnapshot:
    """What the character looks like at one instant."""

    eyes: EyeState
    jump_phase: JumpPhase
    vertical_offset_rows: int

    @property
    def blinking(self) -> bool:
        return self.eyes is EyeState.CLOSED

    @property
    def jumping(self) -> bool:
        return self.jump_phase is not JumpPhase.GROUND


@dataclass
class _BlinkChannel:
    active: bool = False
    started_at: float = 0.0
    duration_s: float = 0.0
    next_at: float = 0.0
    second_blink_at: float | None = None
    # True while the closure showing is the second half of a double blink.
    # Only a first blink may roll for a double, so a sequence is one or two
    # closures and never a chain.
    is_second: bool = False


@dataclass
class _JumpChannel:
    active: bool = False
    started_at: float = 0.0
    duration_s: float = 0.0
    height_rows: int = 1
    next_at: float = 0.0


# Relative weights of the phases that are not the hang itself. Crouch and land
# sit on the ground, so the drawing only ever moves during up/apex/down.
_CROUCH_WEIGHT = 0.12
_RISE_WEIGHT = 0.26
_FALL_WEIGHT = 0.26
_LAND_WEIGHT = 0.12
_TRAVEL_WEIGHT = _CROUCH_WEIGHT + _RISE_WEIGHT + _FALL_WEIGHT + _LAND_WEIGHT


def phase_bounds(hang_ratio: float) -> tuple[tuple[float, JumpPhase], ...]:
    """Split one hop into phases, giving ``hang_ratio`` of it to the apex."""
    unit = (1.0 - hang_ratio) / _TRAVEL_WEIGHT
    crouch = _CROUCH_WEIGHT * unit
    rise = crouch + _RISE_WEIGHT * unit
    apex = rise + hang_ratio
    fall = apex + _FALL_WEIGHT * unit
    return (
        (crouch, JumpPhase.CROUCH),
        (rise, JumpPhase.UP),
        (apex, JumpPhase.APEX),
        (fall, JumpPhase.DOWN),
    )


class MicroBehavior:
    """Two independent stochastic channels: blink and jump.

    Passive by construction: state advances only inside :meth:`snapshot`, and
    only because the caller supplied a later ``now``.
    """

    def __init__(
        self,
        config: BehaviorConfig | None = None,
        *,
        now: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config or BehaviorConfig()
        self.rng = rng if rng is not None else random.Random()
        self._blink = _BlinkChannel(next_at=now + self._blink_delay())
        self._jump = _JumpChannel(next_at=now + self._jump_delay())
        self._last_now = now

    # --- schedule draws ----------------------------------------------------

    def _blink_delay(self) -> float:
        blink = self.config.blink
        return self.rng.uniform(blink.min_interval_s, blink.max_interval_s)

    def _jump_delay(self) -> float:
        jump = self.config.jump
        return self.rng.uniform(jump.min_interval_s, jump.max_interval_s)

    def _start_blink(self, now: float, *, second: bool = False) -> None:
        blink = self.config.blink
        self._blink.active = True
        self._blink.started_at = now
        self._blink.duration_s = self.rng.uniform(blink.min_duration_s, blink.max_duration_s)
        self._blink.second_blink_at = None
        self._blink.is_second = second

    def _start_jump(self, now: float) -> None:
        jump = self.config.jump
        self._jump.active = True
        self._jump.started_at = now
        self._jump.duration_s = self.rng.uniform(jump.min_duration_s, jump.max_duration_s)
        self._jump.height_rows = self.rng.randint(jump.min_height_rows, jump.max_height_rows)
        # Independence already lets the two channels coincide; this only makes
        # the coincidence a little more likely at take-off.
        if (
            self.config.blink.enabled
            and not self._blink.active
            and self.rng.random() < jump.blink_convergence_chance
        ):
            self._start_blink(now)

    # --- introspection, for tests and diagnostics --------------------------

    @property
    def blinking(self) -> bool:
        return self._blink.active

    @property
    def jumping(self) -> bool:
        return self._jump.active

    @property
    def next_blink_at(self) -> float:
        return self._blink.next_at

    @property
    def blink_is_second(self) -> bool:
        """True while the closure on screen is the second of a double blink."""
        return self._blink.is_second

    @property
    def double_blink_pending(self) -> bool:
        return self._blink.second_blink_at is not None

    @property
    def next_jump_at(self) -> float:
        return self._jump.next_at

    @property
    def jump_height_rows(self) -> int:
        return self._jump.height_rows

    # --- the only mutating call --------------------------------------------

    def snapshot(self, now: float) -> BehaviorSnapshot:
        """Advance both channels to ``now`` and describe the result."""
        now = max(now, self._last_now)
        self._last_now = now
        self._advance_blink(now)
        self._advance_jump(now)
        return BehaviorSnapshot(
            EyeState.CLOSED if self._blink.active else EyeState.OPEN,
            self.jump_phase(now),
            self.vertical_offset(now),
        )

    def _advance_blink(self, now: float) -> None:
        blink = self._blink
        if not self.config.blink.enabled:
            blink.active = False
            return
        if not blink.active and blink.second_blink_at is not None and now >= blink.second_blink_at:
            self._start_blink(now, second=True)
        if not blink.active and now >= blink.next_at:
            self._start_blink(now)
        if blink.active and now >= blink.started_at + blink.duration_s:
            was_second = blink.is_second
            blink.active = False
            blink.is_second = False
            # A second closure ends the sequence: it never rolls for another.
            if not was_second and self.rng.random() < self.config.blink.double_blink_chance:
                blink.second_blink_at = now + self.rng.uniform(
                    DOUBLE_BLINK_MIN_GAP_S, DOUBLE_BLINK_MAX_GAP_S
                )
            else:
                blink.second_blink_at = None
            blink.next_at = now + self._blink_delay()

    def _advance_jump(self, now: float) -> None:
        jump = self._jump
        if not self.config.jump.enabled:
            jump.active = False
            return
        if not jump.active and now >= jump.next_at:
            self._start_jump(now)
        if jump.active and now >= jump.started_at + jump.duration_s:
            jump.active = False
            jump.next_at = now + self._jump_delay()

    def jump_phase(self, now: float) -> JumpPhase:
        if not self._jump.active:
            return JumpPhase.GROUND
        elapsed = (now - self._jump.started_at) / self._jump.duration_s
        progress = min(1.0, max(0.0, elapsed))
        for bound, phase in phase_bounds(self.config.jump.hang_ratio):
            if progress < bound:
                return phase
        return JumpPhase.LAND

    def vertical_offset(self, now: float) -> int:
        """How many rows the drawing should rise, 0 while grounded."""
        phase = self.jump_phase(now)
        height = self._jump.height_rows
        if phase is JumpPhase.APEX:
            return height
        if phase in {JumpPhase.UP, JumpPhase.DOWN}:
            return max(1, height - 1)
        return 0


# --- manifest parsing ------------------------------------------------------


def _mapping(value: Any, source: Path | str, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(source, field_name, "must be a mapping")
    return value


def _number(value: Any, source: Path | str, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise _error(source, field_name, f"must be a number >= {minimum}")
    return float(value)


def _chance(value: Any, source: Path | str, field_name: str) -> float:
    chance = _number(value, source, field_name)
    if chance > 1.0:
        raise _error(source, field_name, "must be between 0 and 1")
    return chance


def _integer(value: Any, source: Path | str, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(source, field_name, f"must be an integer >= {minimum}")
    return value


def _boolean(value: Any, source: Path | str, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _error(source, field_name, "must be a boolean")
    return value


def _relative_frame(value: Any, source: Path | str, field_name: str) -> str:
    text = value if isinstance(value, str) else ""
    if not text.strip():
        raise _error(source, field_name, "must be a non-empty path")
    # Check both flavours: "/etc/passwd" is not absolute to a Windows Path, and
    # "C:\x" is not absolute to a POSIX one. An asset must escape neither way.
    windows, posix = PureWindowsPath(text), PurePosixPath(text)
    escapes = (
        windows.is_absolute()
        or posix.is_absolute()
        or text.startswith(("/", "\\"))
        or ".." in windows.parts
        or ".." in posix.parts
    )
    if escapes:
        raise _error(source, field_name, "must be a relative path inside the pet directory")
    return text


def _range(low: float, high: float, source: Path | str, field_name: str) -> tuple[float, float]:
    if high < low:
        raise _error(source, field_name, "max must be >= min")
    return low, high


def _blink_config(data: dict[str, Any], source: Path | str) -> BlinkConfig:
    prefix = "micro_animations.blink"
    known = {
        "enabled",
        "min_interval_ms",
        "max_interval_ms",
        "min_duration_ms",
        "max_duration_ms",
        "double_blink_chance",
    }
    unknown = sorted(str(key) for key in data if str(key) not in known)
    if unknown:
        raise _error(source, prefix, f"unknown fields: {', '.join(unknown)}")
    default = BlinkConfig()
    interval = _range(
        _number(data.get("min_interval_ms", default.min_interval_s * 1000), source, prefix) / 1000,
        _number(data.get("max_interval_ms", default.max_interval_s * 1000), source, prefix) / 1000,
        source,
        f"{prefix}.interval",
    )
    duration = _range(
        _number(data.get("min_duration_ms", default.min_duration_s * 1000), source, prefix) / 1000,
        _number(data.get("max_duration_ms", default.max_duration_s * 1000), source, prefix) / 1000,
        source,
        f"{prefix}.duration",
    )
    return BlinkConfig(
        _boolean(data.get("enabled", True), source, f"{prefix}.enabled"),
        interval[0],
        interval[1],
        duration[0],
        duration[1],
        _chance(
            data.get("double_blink_chance", default.double_blink_chance),
            source,
            f"{prefix}.double_blink_chance",
        ),
    )


def _jump_config(data: dict[str, Any], source: Path | str) -> JumpConfig:
    prefix = "micro_animations.jump"
    known = {
        "enabled",
        "min_interval_ms",
        "max_interval_ms",
        "min_duration_ms",
        "max_duration_ms",
        "min_height_rows",
        "max_height_rows",
        "blink_convergence_chance",
        "hang_ratio",
    }
    unknown = sorted(str(key) for key in data if str(key) not in known)
    if unknown:
        raise _error(source, prefix, f"unknown fields: {', '.join(unknown)}")
    default = JumpConfig()
    interval = _range(
        _number(data.get("min_interval_ms", default.min_interval_s * 1000), source, prefix) / 1000,
        _number(data.get("max_interval_ms", default.max_interval_s * 1000), source, prefix) / 1000,
        source,
        f"{prefix}.interval",
    )
    duration = _range(
        _number(data.get("min_duration_ms", default.min_duration_s * 1000), source, prefix) / 1000,
        _number(data.get("max_duration_ms", default.max_duration_s * 1000), source, prefix) / 1000,
        source,
        f"{prefix}.duration",
    )
    min_height = _integer(
        data.get("min_height_rows", default.min_height_rows),
        source,
        f"{prefix}.min_height_rows",
        minimum=0,
    )
    max_height = _integer(
        data.get("max_height_rows", default.max_height_rows),
        source,
        f"{prefix}.max_height_rows",
        minimum=0,
    )
    if max_height < min_height:
        raise _error(source, f"{prefix}.max_height_rows", "must be >= min_height_rows")
    hang_ratio = _chance(data.get("hang_ratio", default.hang_ratio), source, f"{prefix}.hang_ratio")
    if not 0.0 < hang_ratio < 1.0:
        raise _error(source, f"{prefix}.hang_ratio", "must be between 0 and 1, exclusive")
    return JumpConfig(
        _boolean(data.get("enabled", True), source, f"{prefix}.enabled"),
        interval[0],
        interval[1],
        duration[0],
        duration[1],
        min_height,
        max_height,
        _chance(
            data.get("blink_convergence_chance", default.blink_convergence_chance),
            source,
            f"{prefix}.blink_convergence_chance",
        ),
        hang_ratio,
    )


def _variants(value: Any, source: Path | str) -> dict[str, dict[str, str]]:
    """Parse ``render.variants``: state name -> eye state -> frame path.

    State names are checked as plain strings here; mapping them onto the real
    ``PetState`` is the asset loader's job, which keeps this module free of any
    dependency on the pet engine.
    """
    data = _mapping(value, source, "render.variants")
    known_eyes = {eye.value for eye in EyeState}
    parsed: dict[str, dict[str, str]] = {}
    for state, eyes in data.items():
        field_name = f"render.variants.{state}"
        eye_data = _mapping(eyes, source, field_name)
        unknown = sorted(str(key) for key in eye_data if str(key) not in known_eyes)
        if unknown:
            raise _error(source, field_name, f"unknown eye states: {', '.join(unknown)}")
        if EyeState.OPEN.value not in eye_data:
            raise _error(source, field_name, f"must declare an '{EyeState.OPEN.value}' frame")
        parsed[str(state)] = {
            str(eye): _relative_frame(path, source, f"{field_name}.{eye}")
            for eye, path in eye_data.items()
        }
    return parsed


def validate_behavior_data(data: Any, source: Path | str = "<behavior>") -> BehaviorDocument:
    root = _mapping(data, source, "behavior root")
    allowed = {"schema_version", "id", "micro_animations", "render"}
    unknown = sorted(str(key) for key in root if str(key) not in allowed)
    if unknown:
        raise _error(source, "behavior root", f"unknown fields: {', '.join(unknown)}")
    missing = [key for key in ("schema_version", "id") if key not in root]
    if missing:
        raise _error(source, "behavior root", f"missing required fields: {', '.join(missing)}")

    schema_version = root["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise _error(source, "schema_version", "must be an integer")
    if schema_version != BEHAVIOR_SCHEMA_VERSION:
        raise _error(
            source,
            "schema_version",
            f"unsupported version {schema_version}; expected {BEHAVIOR_SCHEMA_VERSION}",
        )

    behavior_id = root["id"]
    if not isinstance(behavior_id, str) or not behavior_id.strip():
        raise _error(source, "id", "must be a non-empty string")

    micro = _mapping(root.get("micro_animations", {}), source, "micro_animations")
    unknown_channels = sorted(str(key) for key in micro if str(key) not in {"blink", "jump"})
    if unknown_channels:
        raise _error(source, "micro_animations", f"unknown channels: {', '.join(unknown_channels)}")
    config = BehaviorConfig(
        _blink_config(_mapping(micro.get("blink", {}), source, "micro_animations.blink"), source),
        _jump_config(_mapping(micro.get("jump", {}), source, "micro_animations.jump"), source),
    )

    render_data = _mapping(root.get("render", {}), source, "render")
    known_render = {
        "variants",
        "anchor",
        "preserve_canvas_height",
        "max_width",
        "crop_to_ink",
        "headroom_rows",
    }
    unknown_render = sorted(str(key) for key in render_data if str(key) not in known_render)
    if unknown_render:
        raise _error(source, "render", f"unknown fields: {', '.join(unknown_render)}")
    defaults = RenderConfig()
    variants = _variants(render_data.get("variants", {}), source)
    anchor = render_data.get("anchor", defaults.anchor)
    if anchor not in {"bottom", "top"}:
        raise _error(source, "render.anchor", "must be 'bottom' or 'top'")
    raw_max_width = render_data.get("max_width")
    max_width = (
        None
        if raw_max_width is None
        else _integer(raw_max_width, source, "render.max_width", minimum=1)
    )
    render = RenderConfig(
        variants,
        anchor,
        _boolean(
            render_data.get("preserve_canvas_height", defaults.preserve_canvas_height),
            source,
            "render.preserve_canvas_height",
        ),
        max_width,
        _boolean(
            render_data.get("crop_to_ink", defaults.crop_to_ink), source, "render.crop_to_ink"
        ),
        _integer(
            render_data.get("headroom_rows", defaults.headroom_rows),
            source,
            "render.headroom_rows",
            minimum=0,
        ),
    )
    return BehaviorDocument(
        schema_version,
        behavior_id,
        config,
        render,
        Path(source) if isinstance(source, Path) else None,
    )


def load_behavior_document(path: Path) -> BehaviorDocument:
    if not path.is_file():
        raise _error(path, "", f"missing {BEHAVIOR_MANIFEST}")
    return validate_behavior_data(yaml.safe_load(path.read_text(encoding="utf-8")), path)
