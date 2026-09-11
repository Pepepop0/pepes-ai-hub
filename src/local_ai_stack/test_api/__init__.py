"""The opt-in local Test Control API.

A dev/test surface, not a product API. It is absent unless something starts it,
it binds loopback only, and every route requires a token. It expresses a
request in terms of the frozen contracts in
:mod:`local_ai_stack.test_contracts` — a profile, phases, a root mode and a
provider mode — and never in terms of the operating system, so a caller cannot
name a command, an executable, a path, a URL, a port or a process.

The API has no authority of its own. It schedules a :class:`TestHarness` and
republishes what that harness reports.
"""

from __future__ import annotations

from .auth import (
    ENV_TOKEN,
    ENV_TOKEN_FILE,
    TestApiToken,
    TokenError,
    generate_token,
    token_from_environment,
)
from .binding import LOOPBACK_HOST, BindRefused, resolve_bind_host
from .payloads import PayloadError, parse_run_spec
from .runs import EventsAhead, EventsExpired, RunInProgress, RunRecord, RunStore, UnknownRun
from .scheduler import RunScheduler
from .server import API_PREFIX, ShutdownTimeout, StatusSource, TestApi, TestApiConfig

__all__ = [
    "API_PREFIX",
    "ENV_TOKEN",
    "ENV_TOKEN_FILE",
    "LOOPBACK_HOST",
    "BindRefused",
    "EventsAhead",
    "EventsExpired",
    "PayloadError",
    "RunInProgress",
    "RunRecord",
    "RunScheduler",
    "RunStore",
    "ShutdownTimeout",
    "StatusSource",
    "TestApi",
    "TestApiConfig",
    "TestApiToken",
    "TokenError",
    "UnknownRun",
    "generate_token",
    "parse_run_spec",
    "resolve_bind_host",
    "token_from_environment",
]
