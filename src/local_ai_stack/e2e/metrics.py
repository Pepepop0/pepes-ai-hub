"""Performance instrumentation for the post-MVP test phase.

This records evidence. It does not optimize anything and it does not define
pass/fail thresholds: the contract is explicit that this phase measures and
reports, and that a hash cache comes later under its own invariants.

Two properties matter more than the numbers:

- durations come from a monotonic clock, so a wall-clock adjustment during a
  long run cannot produce a negative or wildly wrong measurement;
- every individual observation is kept. Summaries report count, median and
  maximum on top of the raw samples, so one slow or failed run stays visible
  instead of being averaged away.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class Metric(StrEnum):
    """The measurements the contract asks this phase to record."""

    DOCTOR = "doctor"
    PLAN = "plan"
    MODEL_RESOLVE = "model-resolve"
    MODEL_VERIFY = "model-verify"
    SERVICE_STARTUP = "service-startup"
    CHAT_SMOKE = "chat-smoke"
    IMAGE_SMOKE = "image-smoke"
    CLEANUP = "cleanup"


# The observed re-hash time for the 12.45 GB artifact on the reference machine.
# Recorded as context for comparison, never asserted as a threshold.
KNOWN_REHASH_SECONDS = 26.0
KNOWN_REHASH_BYTES = 12_451_820_124


@dataclass(frozen=True)
class Measurement:
    """One observation, with the context needed to interpret it."""

    metric: Metric
    seconds: float
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise ValueError("a measured duration cannot be negative")
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True)
class MetricSummary:
    """Count, median and maximum over the samples for one metric."""

    metric: Metric
    count: int
    median_seconds: float
    maximum_seconds: float
    minimum_seconds: float

    @classmethod
    def of(cls, metric: Metric, samples: tuple[Measurement, ...]) -> MetricSummary:
        values = [sample.seconds for sample in samples]
        return cls(
            metric=metric,
            count=len(values),
            median_seconds=statistics.median(values),
            maximum_seconds=max(values),
            minimum_seconds=min(values),
        )


class MetricRecorder:
    """Collects measurements for one run.

    The clock is injected so a test can prove the recorder uses the monotonic
    source it was given rather than reaching for wall time.
    """

    def __init__(self, monotonic: Callable[[], float] | None = None) -> None:
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._samples: list[Measurement] = []

    @contextmanager
    def measure(self, metric: Metric, **context: object) -> Iterator[dict[str, object]]:
        """Time a block. The yielded mapping is merged into the context.

        The measurement is recorded even when the block raises, because a phase
        that failed slowly is exactly the evidence worth keeping.
        """
        extra: dict[str, object] = {}
        started = self._monotonic()
        try:
            yield extra
        finally:
            elapsed = max(0.0, self._monotonic() - started)
            self.record(metric, elapsed, **{**context, **extra})

    def record(self, metric: Metric, seconds: float, **context: object) -> Measurement:
        measurement = Measurement(metric=metric, seconds=seconds, context=context)
        self._samples.append(measurement)
        return measurement

    # --- leitura -----------------------------------------------------------

    @property
    def measurements(self) -> tuple[Measurement, ...]:
        """Every raw observation, in the order it was taken."""
        return tuple(self._samples)

    def samples(self, metric: Metric) -> tuple[Measurement, ...]:
        return tuple(sample for sample in self._samples if sample.metric is metric)

    def summary(self) -> dict[Metric, MetricSummary]:
        """One summary per metric that has at least one sample."""
        grouped: dict[Metric, list[Measurement]] = {}
        for sample in self._samples:
            grouped.setdefault(sample.metric, []).append(sample)
        return {
            metric: MetricSummary.of(metric, tuple(samples)) for metric, samples in grouped.items()
        }

    def as_report_data(self) -> dict[str, object]:
        """The shape embedded in a run report: raw samples plus summaries."""
        return {
            "samples": [
                {
                    "metric": str(sample.metric),
                    "seconds": round(sample.seconds, 6),
                    "context": dict(sample.context),
                }
                for sample in self._samples
            ],
            "summary": {
                str(metric): {
                    "count": value.count,
                    "median_seconds": round(value.median_seconds, 6),
                    "maximum_seconds": round(value.maximum_seconds, 6),
                    "minimum_seconds": round(value.minimum_seconds, 6),
                }
                for metric, value in self.summary().items()
            },
            "known_rehash_baseline": {
                "bytes": KNOWN_REHASH_BYTES,
                "seconds": KNOWN_REHASH_SECONDS,
            },
        }


__all__ = [
    "KNOWN_REHASH_BYTES",
    "KNOWN_REHASH_SECONDS",
    "Measurement",
    "Metric",
    "MetricRecorder",
    "MetricSummary",
]
