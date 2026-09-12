"""Throttled, speed-aware transfer sampling: raw bytes in, safe observations out.

No real clock and no real transfer here -- :class:`TransferSampler` takes its
monotonic source as a seam, so every test drives time explicitly and nothing
sleeps.
"""

from __future__ import annotations

import pytest

from local_ai_stack.transfer_progress import TransferSampler


class FakeClock:
    """A monotonic source a test can move by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- throttling --------------------------------------------------------------


def test_the_first_sample_is_always_emitted():
    sampler = TransferSampler(monotonic=FakeClock())
    update = sampler.sample(100, 1000)
    assert update is not None
    assert update.completed_bytes == 100
    assert update.total_bytes == 1000


def test_a_sample_before_the_interval_and_below_the_delta_is_throttled_away():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=1.0, min_delta_bytes=1_000_000)
    sampler.sample(0, None)
    clock.advance(0.01)
    assert sampler.sample(1024, None) is None


def test_elapsed_time_alone_forces_an_emission():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.1, min_delta_bytes=1_000_000)
    sampler.sample(0, None)
    clock.advance(0.2)
    assert sampler.sample(10, None) is not None


def test_a_big_enough_byte_jump_forces_an_emission_before_the_interval():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=10.0, min_delta_bytes=100)
    sampler.sample(0, None)
    clock.advance(0.01)
    assert sampler.sample(200, None) is not None


def test_throttled_samples_do_not_move_the_baseline_for_the_next_check():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=1.0, min_delta_bytes=1_000_000)
    sampler.sample(0, None)
    clock.advance(0.4)
    assert sampler.sample(10, None) is None
    clock.advance(0.4)
    # Still under the 1.0s interval measured from the *last emitted* sample.
    assert sampler.sample(20, None) is None
    clock.advance(0.3)
    assert sampler.sample(30, None) is not None


# --- velocidade --------------------------------------------------------------


def test_speed_is_unknown_on_the_very_first_sample():
    sampler = TransferSampler(monotonic=FakeClock())
    update = sampler.sample(500, None)
    assert update.bytes_per_second is None


def test_speed_is_computed_from_monotonic_deltas():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.0, min_delta_bytes=0)
    sampler.sample(0, None)
    clock.advance(1.0)
    update = sampler.sample(1000, None)
    assert update.bytes_per_second == pytest.approx(1000.0)


def test_speed_is_smoothed_across_samples_not_purely_instantaneous():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.0, min_delta_bytes=0, smoothing=0.5)
    sampler.sample(0, None)
    clock.advance(1.0)
    second = sampler.sample(1000, None)
    clock.advance(1.0)
    third = sampler.sample(3000, None)
    assert second.bytes_per_second == pytest.approx(1000.0)
    # instantaneous = 2000 B/s; smoothed = 0.5*2000 + 0.5*1000
    assert third.bytes_per_second == pytest.approx(1500.0)


def test_a_zero_interval_reports_no_rate_instead_of_dividing_by_zero():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.0, min_delta_bytes=0)
    sampler.sample(0, None)
    # The clock does not move between these two observations.
    update = sampler.sample(500, None)
    assert update is not None
    assert update.bytes_per_second is None


def test_a_wall_clock_style_regression_does_not_produce_a_negative_rate():
    """A retry that restarts the byte count must not corrupt an emitted sample."""
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.0, min_delta_bytes=0)
    sampler.sample(0, None)
    clock.advance(1.0)
    sampler.sample(1000, None)
    clock.advance(1.0)
    update = sampler.sample(200, None)  # bytes went backwards (a restarted attempt)
    assert update is not None
    assert update.completed_bytes == 200


# --- total desconhecido -------------------------------------------------------


def test_unknown_total_passes_through_as_none():
    sampler = TransferSampler(monotonic=FakeClock())
    update = sampler.sample(500, None)
    assert update.total_bytes is None
    final = sampler.finish(1000, None)
    assert final.total_bytes is None


# --- finish --------------------------------------------------------------------


def test_finish_is_always_emitted_regardless_of_throttling():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=100.0, min_delta_bytes=1_000_000_000)
    sampler.sample(0, 100)
    final = sampler.finish(100, 100)
    assert final.completed_bytes == 100
    assert final.total_bytes == 100


def test_nothing_is_emitted_after_finish():
    sampler = TransferSampler(monotonic=FakeClock())
    sampler.sample(10, 100)
    sampler.finish(100, 100)
    assert sampler.sample(999, 100) is None


def test_finish_keeps_the_last_known_speed_rather_than_recomputing_a_spike():
    clock = FakeClock()
    sampler = TransferSampler(monotonic=clock, min_interval=0.0, min_delta_bytes=0)
    sampler.sample(0, 100)
    clock.advance(1.0)
    sampled = sampler.sample(50, 100)
    assert sampled.bytes_per_second == pytest.approx(50.0)
    final = sampler.finish(100, 100)
    assert final.bytes_per_second == pytest.approx(50.0)


def test_finish_with_only_the_first_sample_ever_has_no_speed():
    sampler = TransferSampler(monotonic=FakeClock())
    sampler.sample(10, 100)
    final = sampler.finish(100, 100)
    assert final.bytes_per_second is None
