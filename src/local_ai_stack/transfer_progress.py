"""Turns a raw byte counter into throttled, speed-aware transfer observations.

:mod:`download` already calls back on every chunk it reads -- for a large
artifact that is thousands of calls a second, far more than any consumer
should publish as an :class:`~local_ai_stack.operation_contracts.OperationEvent`.
This module is the one throttle between "a chunk arrived" and "an observer
saw an update": it decides *when* a sample is worth emitting and computes the
one thing raw bytes cannot carry on their own, transfer speed, from a
monotonic clock so a wall-clock adjustment mid-download can never produce a
negative or wildly wrong rate.

Nothing here downloads, retries or touches a file. It has no opinion about
HTTP, Ollama or any other source of bytes; it only turns
``(completed_bytes, total_bytes)`` observations into
:class:`~local_ai_stack.operation_contracts.TransferProgress` values at a
controlled cadence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .operation_contracts import TransferProgress

# ~6 updates/second: comfortably inside the "4-10 per second" the contract's
# consumers expect, without being so sparse that a fast local transfer looks
# frozen between samples.
DEFAULT_MIN_INTERVAL_SECONDS = 0.15
# Emit early even before the interval elapses after a substantial byte jump.
# This remains well above Downloader's 256 KiB chunk, so the normal path cannot
# accidentally publish one event for every chunk on a fast connection.
DEFAULT_MIN_DELTA_BYTES = 4 * 1024 * 1024
# Smoothing for the speed estimate: closer to 1 tracks the instantaneous rate
# more closely, closer to 0 favors the running average. This is deliberately
# simple -- the contract asks for "no sophisticated algorithm".
SPEED_SMOOTHING = 0.35


@dataclass
class TransferSampler:
    """One artifact's throttled progress, from raw bytes to a safe sample.

    A fresh instance belongs to one transfer. Call :meth:`sample` for every
    raw observation; it returns ``None`` when the update should be skipped and
    a :class:`TransferProgress` when it should be published. The very first
    and the final (:meth:`finish`) observations are never throttled away.
    """

    monotonic: Callable[[], float] = time.monotonic
    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS
    min_delta_bytes: int = DEFAULT_MIN_DELTA_BYTES
    smoothing: float = SPEED_SMOOTHING

    _first: bool = field(default=True, repr=False)
    _last_emit_time: float | None = field(default=None, repr=False)
    _last_emit_bytes: int = field(default=0, repr=False)
    _last_emit_total: int | None = field(default=None, repr=False)
    _last_speed: float | None = field(default=None, repr=False)
    _done: bool = field(default=False, repr=False)

    def sample(self, completed_bytes: int, total_bytes: int | None) -> TransferProgress | None:
        """A raw observation. Returns a throttled, speed-annotated update or None."""
        if self._done:
            return None
        now = self.monotonic()
        if self._should_emit(now, completed_bytes, total_bytes):
            return self._emit(now, completed_bytes, total_bytes)
        return None

    def finish(self, completed_bytes: int, total_bytes: int | None) -> TransferProgress | None:
        """Return a missing final observation once, without duplicating one.

        Speed is not recomputed here: the transfer has already ended, and a
        near-zero final interval would otherwise produce a meaningless spike.
        The last real sample's speed (or none, if there was only ever one) is
        what a consumer sees on completion.
        """
        if self._done:
            return None
        if (
            not self._first
            and self._last_emit_bytes == completed_bytes
            and self._last_emit_total == total_bytes
        ):
            self._done = True
            return None
        result = TransferProgress(
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            bytes_per_second=self._last_speed,
        )
        self._done = True
        return result

    # --- internals -----------------------------------------------------------

    def _should_emit(self, now: float, completed_bytes: int, total_bytes: int | None) -> bool:
        if self._first:
            return True
        if total_bytes is not None and completed_bytes == total_bytes:
            return True
        assert self._last_emit_time is not None
        elapsed = now - self._last_emit_time
        moved = completed_bytes - self._last_emit_bytes
        return elapsed >= self.min_interval or moved >= self.min_delta_bytes

    def _emit(self, now: float, completed_bytes: int, total_bytes: int | None) -> TransferProgress:
        if not self._first and self._last_emit_time is not None:
            elapsed = now - self._last_emit_time
            moved = completed_bytes - self._last_emit_bytes
            # A zero or negative interval (a clock that did not advance, or a
            # caller replaying the same byte count) yields no instantaneous
            # rate rather than a division by zero or an infinite speed.
            if elapsed > 0 and moved >= 0:
                instantaneous = moved / elapsed
                self._last_speed = (
                    instantaneous
                    if self._last_speed is None
                    else self.smoothing * instantaneous + (1 - self.smoothing) * self._last_speed
                )
        # The very first sample has no prior tick to measure a rate against;
        # reporting one would be exactly the "absurd first-chunk value" the
        # contract warns against, so speed stays unknown until a second point.
        self._first = False
        self._last_emit_time = now
        self._last_emit_bytes = completed_bytes
        self._last_emit_total = total_bytes
        return TransferProgress(
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            bytes_per_second=self._last_speed,
        )


__all__ = [
    "DEFAULT_MIN_DELTA_BYTES",
    "DEFAULT_MIN_INTERVAL_SECONDS",
    "SPEED_SMOOTHING",
    "TransferSampler",
]
