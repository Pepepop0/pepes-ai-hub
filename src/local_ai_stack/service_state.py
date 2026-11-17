"""Aggregated health verdict, kept apart from the probes so light modules can use it."""

from enum import Enum


class ServiceState(str, Enum):
    """Aggregated health verdict for one service or for the whole stack."""

    UP = "UP"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"
