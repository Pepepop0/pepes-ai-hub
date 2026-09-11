"""Opt-in composition of the test API and controlled harness.

HTTP knows only TestHarness and StatusSource. This module chooses the concrete
implementation, with a fresh world per run and no caller-selected filesystem
location. It starts no server and allocates no root until explicitly asked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from threading import Lock

from .e2e.harness import StackTestHarness
from .e2e.world import controlled_world
from .test_api import TestApi, TestApiConfig
from .test_contracts import (
    TestEventSink,
    TestPhase,
    TestProviderMode,
    TestRootMode,
    TestRunReport,
    TestRunSpec,
)

HarnessFactory = Callable[[TestRunSpec], StackTestHarness]


def new_controlled_harness(spec: TestRunSpec) -> StackTestHarness:
    """Use shipped profiles and offline providers, with managed readiness."""
    return StackTestHarness(controlled_world(ready_ports=[21434]))


class ControlledApplication:
    """A per-run harness factory and a read-only last-run status projection.

    The factory is trusted Python composition, never an HTTP request field.
    Status reads a detached snapshot; it cannot invoke a domain or mutate a run.
    """

    def __init__(self, factory: HarnessFactory = new_controlled_harness) -> None:
        self._factory = factory
        self._status: dict[str, object] = {}
        self._lock = Lock()

    def run(
        self, run_id: str, spec: TestRunSpec, *, emit: TestEventSink | None = None
    ) -> TestRunReport:
        if spec.provider_mode is not TestProviderMode.CONTROLLED:
            raise ValueError("local-real providers are unsupported by this test application")
        if spec.root_mode is not TestRootMode.EPHEMERAL:
            raise ValueError("this test application supports ephemeral roots only")
        harness = self._factory(spec)
        report = harness.run(run_id, spec, emit=emit)
        self._publish(harness, report)
        return report

    def _publish(self, harness: StackTestHarness, report: TestRunReport) -> None:
        facts = harness.world.facts
        phases = {phase.phase: phase for phase in report.phases}
        stop = phases.get(TestPhase.STOP)
        cleanup = phases.get(TestPhase.CLEANUP)
        released = cleanup.data if cleanup is not None else {}
        snapshot = {
            "system": {
                "provider_mode": "controlled",
                "observation": "last-completed-run",
                "run_id": report.run_id,
                "os": facts.os_name,
                "windows_build": facts.windows_build,
                "python": facts.python_version,
            },
            "gpu": {
                "provider_mode": "controlled",
                "devices": [
                    {"name": gpu.name, "vram_total_mib": gpu.vram_total_mib} for gpu in facts.gpus
                ],
            },
            "services": {
                "run_id": report.run_id,
                "observation": "last-completed-run",
                "before_stop": stop.data.get("confirmed", []) if stop is not None else [],
                "managed_residual": list(harness.world.managed_residual()),
                "registry": released.get("registry", []),
                "membership": released.get("membership", {}),
                "handles_released": released.get("job_handles_released", False),
            },
            "models": {
                "run_id": report.run_id,
                "observation": "resolution-before-cleanup",
                "roles": report.context.get("resolved_roles", {}),
            },
        }
        with self._lock:
            self._status = deepcopy(snapshot)

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return deepcopy(self._status)


def create_test_api(
    config: TestApiConfig, *, harness_factory: HarnessFactory = new_controlled_harness
) -> TestApi:
    """Compose, without starting, the controlled dev/test application."""
    if config.allow_real_providers:
        raise ValueError("local-real providers are unsupported by this test application")
    application = ControlledApplication(harness_factory)
    return TestApi(config, application, status_source=application)
