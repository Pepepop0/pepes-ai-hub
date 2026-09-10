"""Shared fixtures for the E2E suite.

One rule, enforced rather than remembered: a test may not leave a temporary
directory behind. The harness tracks everything it creates, so the guard below
knows exactly which paths belong to this process and removes nothing else.
"""

from __future__ import annotations

import pytest

from local_ai_stack.e2e import tracking


@pytest.fixture(autouse=True)
def no_temp_leak():
    """Release anything a test left tracked, and say which test left it.

    The harness proves its own cleanup inside the scenarios; this is about the
    tests around it, where a world built for one assertion used to leave its
    root behind for good.
    """
    before = set(tracking.outstanding())
    yield
    leaked = [path for path in tracking.outstanding() if path not in before]
    tracking.release(leaked)


@pytest.fixture
def scenario_run():
    """Set a scenario up and guarantee its release, whatever the test does."""
    setups = []

    def build(scenario):
        setup = scenario.setup()
        setups.append(setup)
        return setup

    yield build

    for setup in setups:
        if setup.world.root.removable and setup.world.root.exists():
            setup.world.root.release()
        if setup.teardown is not None:
            setup.teardown()
