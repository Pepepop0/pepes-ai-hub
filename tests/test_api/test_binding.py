"""Where the server may listen, and where it refuses to."""

from __future__ import annotations

import pytest

from local_ai_stack.endpoints import DEFAULT_HOST
from local_ai_stack.test_api import BindRefused, TestApi, TestApiConfig, TestApiToken
from local_ai_stack.test_api.binding import LOOPBACK_HOST, resolve_bind_host

from .conftest import KNOWN_PROFILES, SECRET, Client, ScriptedHarness


def test_the_default_is_the_address_the_stack_already_chose():
    """One decision about local addresses, not two."""
    assert LOOPBACK_HOST == DEFAULT_HOST == "127.0.0.1"
    assert resolve_bind_host() == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_a_loopback_address_is_allowed(host):
    assert resolve_bind_host(host) == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "10.0.0.5", "8.8.8.8", "203.0.113.7"],
)
def test_a_non_loopback_address_is_refused(host):
    """There is no override: the refusal is the feature."""
    with pytest.raises(BindRefused, match="loopback-only"):
        resolve_bind_host(host)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "example.com", "", "   "])
def test_a_name_is_never_resolved(host):
    """Resolving would let whatever answers the lookup pick the interface."""
    with pytest.raises(BindRefused):
        resolve_bind_host(host)


def test_a_server_asked_for_a_public_address_never_binds():
    """The refusal happens before a socket exists, not after."""
    config = TestApiConfig(
        token=TestApiToken(SECRET), known_profiles=KNOWN_PROFILES, host="0.0.0.0"
    )
    api = TestApi(config, ScriptedHarness())
    with pytest.raises(BindRefused):
        api.start()
    with pytest.raises(RuntimeError, match="not running"):
        _ = api.address


def test_a_running_server_is_reachable_only_on_loopback(api):
    host, port = api.address
    assert host == "127.0.0.1"
    assert port > 0
    assert api.url == f"http://127.0.0.1:{port}/internal/test/v1"
    assert Client(api).get("/health").status == 200
