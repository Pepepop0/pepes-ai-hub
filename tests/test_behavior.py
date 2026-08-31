from __future__ import annotations

import itertools
import os
import random
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import yaml

import local_ai_stack
from local_ai_stack.behavior import (
    BEHAVIOR_MANIFEST,
    BehaviorConfig,
    BehaviorError,
    BlinkConfig,
    EyeState,
    JumpConfig,
    JumpPhase,
    MicroBehavior,
    load_behavior_document,
    validate_behavior_data,
)

REPO = Path(__file__).resolve().parents[1]
SRC = Path(local_ai_stack.__file__).resolve().parents[1]
HELLO_KITTY = REPO / "assets" / "pets" / "hello-kitty"

MANIFEST = {
    "schema_version": 1,
    "id": "fixture",
    "micro_animations": {
        "blink": {
            "enabled": True,
            "min_interval_ms": 1800,
            "max_interval_ms": 4500,
            "min_duration_ms": 120,
            "max_duration_ms": 220,
            "double_blink_chance": 0.10,
        },
        "jump": {
            "enabled": True,
            "min_interval_ms": 2500,
            "max_interval_ms": 6500,
            "min_duration_ms": 400,
            "max_duration_ms": 700,
            "min_height_rows": 1,
            "max_height_rows": 2,
            "blink_convergence_chance": 0.25,
        },
    },
    "render": {
        "variants": {"idle": {"open": "base/open.txt", "closed": "base/closed.txt"}},
        "anchor": "bottom",
        "preserve_canvas_height": True,
    },
}


def behavior(seed: int = 1, config: BehaviorConfig | None = None) -> MicroBehavior:
    return MicroBehavior(config, now=0.0, rng=random.Random(seed))


def sample(micro: MicroBehavior, *, until: float = 60.0, step: float = 0.05):
    """Walk a clock the way Textual would and record what changed."""
    blinks: list[float] = []
    jumps: list[float] = []
    together: list[float] = []
    was_blinking = was_jumping = False
    moment = 0.0
    while moment < until:
        snapshot = micro.snapshot(moment)
        blinking = snapshot.eyes is EyeState.CLOSED
        jumping = snapshot.jump_phase is not JumpPhase.GROUND
        if blinking and not was_blinking:
            blinks.append(round(moment, 3))
        if jumping and not was_jumping:
            jumps.append(round(moment, 3))
        if blinking and jumping:
            together.append(round(moment, 3))
        was_blinking, was_jumping = blinking, jumping
        moment += step
    return blinks, jumps, together


# --- manifest --------------------------------------------------------------


def test_shipped_behavior_manifest_parses():
    document = load_behavior_document(HELLO_KITTY / BEHAVIOR_MANIFEST)
    assert document.id == "hello-kitty"
    assert document.config.blink.min_interval_s == 0.9
    assert document.config.blink.max_interval_s == 2.6
    assert document.config.blink.min_duration_s == 0.26
    assert document.config.jump.min_duration_s == 0.9
    assert document.config.jump.max_duration_s == 1.5
    assert document.config.jump.max_height_rows == 2
    assert document.config.jump.hang_ratio == 0.46
    assert document.config.jump.blink_convergence_chance == 0.30
    assert document.render.variants == {
        "idle": {"open": "base/open.txt", "closed": "base/closed.txt"}
    }
    assert document.render.anchor == "bottom"
    assert document.render.preserve_canvas_height is True


def test_milliseconds_become_seconds():
    document = validate_behavior_data(MANIFEST)
    assert document.config.blink.min_duration_s == 0.12
    assert document.config.jump.max_interval_s == 6.5


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 4}, "unsupported version"),
        ({"id": ""}, "non-empty string"),
        ({"extra": 1}, "unknown fields: extra"),
        ({"micro_animations": {"dancing": {}}}, "unknown channels: dancing"),
        ({"render": {"anchor": "middle"}}, "must be 'bottom' or 'top'"),
        ({"render": {"variants": {"idle": {"open": "/etc/passwd"}}}}, "relative path"),
        ({"render": {"variants": {"idle": {"open": "../../x.txt"}}}}, "relative path"),
        ({"render": {"variants": {"idle": {"closed": "base/closed.txt"}}}}, "must declare an"),
        ({"render": {"variants": {"idle": {"open": "a.txt", "winking": "b.txt"}}}}, "unknown eye"),
    ],
)
def test_malformed_behavior_is_rejected(mutation, message):
    with pytest.raises(BehaviorError, match=message):
        validate_behavior_data({**MANIFEST, **mutation})


def test_inverted_ranges_are_rejected():
    broken = {
        **MANIFEST,
        "micro_animations": {"blink": {"min_interval_ms": 5000, "max_interval_ms": 100}},
    }
    with pytest.raises(BehaviorError, match="max must be >= min"):
        validate_behavior_data(broken)


def test_chance_above_one_is_rejected():
    broken = {**MANIFEST, "micro_animations": {"blink": {"double_blink_chance": 4}}}
    with pytest.raises(BehaviorError, match="between 0 and 1"):
        validate_behavior_data(broken)


def test_unknown_field_inside_a_channel_is_rejected():
    broken = {**MANIFEST, "micro_animations": {"jump": {"gravity": 9.8}}}
    with pytest.raises(BehaviorError, match="unknown fields: gravity"):
        validate_behavior_data(broken)


def test_missing_manifest_is_reported(tmp_path):
    with pytest.raises(BehaviorError, match=BEHAVIOR_MANIFEST):
        load_behavior_document(tmp_path / BEHAVIOR_MANIFEST)


def test_defaults_apply_when_a_channel_is_omitted(tmp_path):
    path = tmp_path / BEHAVIOR_MANIFEST
    path.write_text(yaml.safe_dump({"schema_version": 1, "id": "bare"}), encoding="utf-8")
    document = load_behavior_document(path)
    assert document.config.blink == BlinkConfig()
    assert document.config.jump == JumpConfig()


# --- determinism -----------------------------------------------------------


def test_the_same_seed_replays_the_same_life():
    first = sample(behavior(seed=99))
    second = sample(behavior(seed=99))
    assert first == second


def test_different_seeds_diverge():
    assert sample(behavior(seed=1)) != sample(behavior(seed=2))


def test_an_injected_rng_is_the_only_source_of_randomness():
    blinks, jumps, _ = sample(behavior(seed=5))
    assert blinks and jumps


# --- independent schedules -------------------------------------------------


def test_blink_has_its_own_irregular_schedule():
    # Isolate the channel: double blinks and jump-convergence blinks are both
    # extra by design, so switching them off leaves only scheduled blinks.
    single = BehaviorConfig(BlinkConfig(double_blink_chance=0.0), JumpConfig(enabled=False))
    blinks, _, _ = sample(behavior(seed=3, config=single), until=120)
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(blinks)]
    assert len(set(gaps)) > 1, "a fixed interval would repeat one value"
    # Interval is drawn from 1.8-4.5s and measured from the previous blink's
    # end, so a gap spans one interval plus at most one blink and one tick.
    assert all(1.8 <= gap <= 4.8 for gap in gaps)


# --- blink sequences -------------------------------------------------------


class ScriptedRandom(random.Random):
    """A Random whose every draw is fixed, so a schedule is fully predictable.

    ``uniform`` is built on ``random``, so pinning that pins every interval and
    duration to one end of its range as well.
    """

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self._value = value

    def random(self) -> float:
        return self._value


def blink_only(**blink) -> BehaviorConfig:
    return BehaviorConfig(BlinkConfig(**blink), JumpConfig(enabled=False))


def closures(micro: MicroBehavior, *, until: float, step: float = 0.01) -> list[float]:
    starts: list[float] = []
    was_blinking = False
    moment = 0.0
    while moment < until:
        blinking = micro.snapshot(moment).blinking
        if blinking and not was_blinking:
            starts.append(round(moment, 3))
        was_blinking = blinking
        moment += step
    return starts


def sequences(starts: list[float], gap: float = 0.6) -> list[list[float]]:
    """Group closures that belong to the same blink sequence."""
    grouped: list[list[float]] = []
    for start in starts:
        if grouped and start - grouped[-1][-1] < gap:
            grouped[-1].append(start)
        else:
            grouped.append([start])
    return grouped


def test_a_single_blink_is_one_closure():
    config = blink_only(
        min_interval_s=1.0, max_interval_s=1.0, min_duration_s=0.2, max_duration_s=0.2
    )
    micro = MicroBehavior(config, now=0.0, rng=ScriptedRandom(0.9))
    assert all(len(group) == 1 for group in sequences(closures(micro, until=10)))


def test_a_double_blink_is_exactly_two_closures():
    config = blink_only(
        min_interval_s=1.0,
        max_interval_s=1.0,
        min_duration_s=0.2,
        max_duration_s=0.2,
        double_blink_chance=1.0,
    )
    micro = MicroBehavior(config, now=0.0, rng=ScriptedRandom(0.0))
    groups = sequences(closures(micro, until=10))
    assert groups
    assert all(len(group) == 2 for group in groups), groups


def test_a_second_blink_never_starts_a_third():
    """Even with an RNG that always says 'double', the sequence stops at two."""
    config = blink_only(
        min_interval_s=5.0,
        max_interval_s=5.0,
        min_duration_s=0.2,
        max_duration_s=0.2,
        double_blink_chance=1.0,
    )
    micro = MicroBehavior(config, now=0.0, rng=ScriptedRandom(0.0))
    seen: list[tuple[float, bool]] = []
    was_blinking = False
    moment = 0.0
    while moment < 6.4:  # first blink at 5.0s, next sequence only at ~6.5s
        blinking = micro.snapshot(moment).blinking
        if blinking and not was_blinking:
            seen.append((round(moment, 2), micro.blink_is_second))
        was_blinking = blinking
        moment += 0.01
    assert [is_second for _, is_second in seen] == [False, True]
    assert micro.double_blink_pending is False


def test_the_second_blink_schedules_a_normal_next_blink():
    config = blink_only(
        min_interval_s=2.0,
        max_interval_s=2.0,
        min_duration_s=0.2,
        max_duration_s=0.2,
        double_blink_chance=1.0,
    )
    micro = MicroBehavior(config, now=0.0, rng=ScriptedRandom(0.0))
    groups = sequences(closures(micro, until=12))
    assert len(groups) >= 2
    # Sequences are spaced by the normal interval, not by the double-blink gap.
    assert groups[1][0] - groups[0][-1] > 1.5


def test_jump_has_its_own_irregular_schedule():
    _, jumps, _ = sample(behavior(seed=3), until=120)
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(jumps)]
    assert len(set(gaps)) > 1
    assert all(2.4 <= gap <= 7.5 for gap in gaps)


def test_a_jump_can_add_a_blink_the_schedule_did_not_plan():
    """Convergence blinks are extra: they may land inside the quiet interval."""
    always = BehaviorConfig(BlinkConfig(double_blink_chance=0.0), JumpConfig())
    never = BehaviorConfig(
        BlinkConfig(double_blink_chance=0.0), JumpConfig(blink_convergence_chance=0.0)
    )
    with_jumps, _, _ = sample(behavior(seed=3, config=always), until=120)
    without, _, _ = sample(behavior(seed=3, config=never), until=120)
    short = [round(b - a, 3) for a, b in itertools.pairwise(with_jumps) if b - a < 1.8]
    assert short, "a converged blink should be able to break the cadence"
    assert not [round(b - a, 3) for a, b in itertools.pairwise(without) if b - a < 1.79]


def test_the_two_channels_are_not_one_timeline():
    blinks, jumps, _ = sample(behavior(seed=11), until=120)
    assert len(blinks) > len(jumps)
    # No fixed phase relationship: blink starts do not land on jump starts.
    assert not set(blinks).issubset(set(jumps))


def test_disabling_one_channel_leaves_the_other_running():
    only_blink = behavior(config=BehaviorConfig(BlinkConfig(), JumpConfig(enabled=False)))
    blinks, jumps, _ = sample(only_blink)
    assert blinks and not jumps

    only_jump = behavior(config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig()))
    blinks, jumps, _ = sample(only_jump)
    assert jumps and not blinks


def test_blink_and_jump_converge_sometimes_but_not_always():
    _, _, together = sample(behavior(seed=7), until=120)
    assert together, "independent channels must be able to coincide"
    _, jumps, _ = sample(behavior(seed=7), until=120)
    assert len(together) < len(jumps) * 40, "they must not be permanently locked"


def test_convergence_chance_pulls_a_blink_into_the_take_off():
    always = BehaviorConfig(BlinkConfig(), JumpConfig(blink_convergence_chance=1.0))
    never = BehaviorConfig(BlinkConfig(), JumpConfig(blink_convergence_chance=0.0))
    forced = sum(len(sample(behavior(seed=s, config=always), until=60)[2]) for s in range(5))
    loose = sum(len(sample(behavior(seed=s, config=never), until=60)[2]) for s in range(5))
    assert forced > loose


# --- passive clock ---------------------------------------------------------


def test_asking_twice_for_the_same_instant_changes_nothing():
    micro = behavior(seed=42)
    first = micro.snapshot(3.0)
    schedule = (micro.next_blink_at, micro.next_jump_at)
    for _ in range(20):
        assert micro.snapshot(3.0) == first
    assert (micro.next_blink_at, micro.next_jump_at) == schedule


def test_refreshing_more_often_does_not_restart_the_schedule():
    """A faster refresh shows the same life, not a reset or a faster one."""
    coarse_blinks, coarse_jumps, _ = sample(behavior(seed=8), until=30, step=0.25)
    fine_blinks, fine_jumps, _ = sample(behavior(seed=8), until=30, step=0.01)
    # Each channel only re-arms when a tick observes the end, so a coarse clock
    # drifts slightly late. What must not happen is a reset or a burst.
    assert abs(len(coarse_blinks) - len(fine_blinks)) <= 2
    assert abs(len(coarse_jumps) - len(fine_jumps)) <= 1
    assert all(abs(a - b) < 1.0 for a, b in zip(coarse_blinks[:4], fine_blinks[:4]))


def test_a_clock_that_goes_backwards_never_rewinds_the_behavior():
    micro = behavior(seed=4)
    micro.snapshot(10.0)
    schedule = (micro.next_blink_at, micro.next_jump_at)
    micro.snapshot(0.0)
    assert (micro.next_blink_at, micro.next_jump_at) == schedule


# --- jump geometry ---------------------------------------------------------


def test_a_hop_rises_and_returns_to_the_ground():
    micro = behavior(config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig()))
    offsets = []
    moment = 0.0
    while moment < 30:
        offsets.append(micro.snapshot(moment).vertical_offset_rows)
        moment += 0.02
    assert max(offsets) in {1, 2}
    assert offsets[0] == 0
    assert offsets[-1] == 0
    assert set(offsets) <= {0, 1, 2}


def test_phases_run_in_order_within_one_hop():
    micro = behavior(config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig()))
    phases: list[JumpPhase] = []
    moment = 0.0
    while moment < 12:
        phase = micro.snapshot(moment).jump_phase
        if not phases or phases[-1] is not phase:
            phases.append(phase)
        moment += 0.01
    hop = phases[: phases.index(JumpPhase.GROUND, 1) + 1]
    assert hop == [
        JumpPhase.GROUND,
        JumpPhase.CROUCH,
        JumpPhase.UP,
        JumpPhase.APEX,
        JumpPhase.DOWN,
        JumpPhase.LAND,
        JumpPhase.GROUND,
    ]


def test_crouch_and_land_stay_on_the_ground():
    micro = behavior(config=BehaviorConfig(BlinkConfig(enabled=False), JumpConfig()))
    moment = 0.0
    while moment < 30:
        snapshot = micro.snapshot(moment)
        if snapshot.jump_phase in {JumpPhase.CROUCH, JumpPhase.LAND, JumpPhase.GROUND}:
            assert snapshot.vertical_offset_rows == 0
        moment += 0.02


# --- isolation -------------------------------------------------------------


def test_the_behavior_backend_creates_no_thread():
    before = threading.active_count()
    micro = behavior(seed=6)
    moment = 0.0
    while moment < 30:
        micro.snapshot(moment)
        moment += 0.05
    assert threading.active_count() == before


def test_the_behavior_backend_does_not_import_the_tui():
    code = (
        "import sys;"
        "import local_ai_stack.behavior, local_ai_stack.renderer;"
        "print(any(m in sys.modules for m in ('textual', 'rich')))"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert result.stdout.strip() == "False"


def test_no_timer_or_sleep_hides_in_the_backend():
    source = (SRC / "local_ai_stack" / "behavior.py").read_text(encoding="utf-8")
    for forbidden in ("import threading", "time.sleep", "asyncio", "Timer("):
        assert forbidden not in source
