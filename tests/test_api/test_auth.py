"""The token, and what happens without it."""

from __future__ import annotations

import pytest

from local_ai_stack.test_api import TestApiToken, TokenError, generate_token
from local_ai_stack.test_api.auth import (
    ENV_TOKEN,
    ENV_TOKEN_FILE,
    MINIMUM_TOKEN_CHARS,
    presented_token,
    token_from_environment,
)

from .conftest import SECRET, Client

ROUTES = [
    ("GET", "/health"),
    ("GET", "/status"),
    ("POST", "/runs"),
    ("GET", "/runs/" + "a" * 32),
    ("GET", "/runs/" + "a" * 32 + "/events"),
]


# --- the secret itself ------------------------------------------------------


def test_a_short_secret_is_refused():
    """256 bits or nothing; the server does not stretch a weak token."""
    with pytest.raises(TokenError, match="256 bits"):
        TestApiToken("hunter2")
    with pytest.raises(TokenError):
        TestApiToken("x" * (MINIMUM_TOKEN_CHARS - 1))


def test_a_generated_secret_is_long_enough_to_be_accepted():
    assert len(generate_token()) >= MINIMUM_TOKEN_CHARS
    assert TestApiToken(generate_token()) is not None


def test_the_secret_is_never_printed():
    """A traceback or a log line must not be able to leak it."""
    token = TestApiToken(SECRET)
    assert SECRET not in repr(token)
    assert SECRET not in str(token)
    assert SECRET not in f"{token}"
    assert not any(SECRET in str(getattr(token, name, "")) for name in TestApiToken.__slots__)


def test_comparison_accepts_only_the_exact_secret():
    token = TestApiToken(SECRET)
    assert token.matches(SECRET)
    assert not token.matches(SECRET + "x")
    assert not token.matches(SECRET[:-1])
    assert not token.matches("")
    assert not token.matches(None)


def test_the_token_is_read_from_the_environment(tmp_path):
    assert token_from_environment({ENV_TOKEN: SECRET}).matches(SECRET)

    path = tmp_path / "token.txt"
    path.write_text(SECRET + "\n", encoding="utf-8")
    assert token_from_environment({ENV_TOKEN_FILE: str(path)}).matches(SECRET)

    with pytest.raises(TokenError, match=ENV_TOKEN):
        token_from_environment({})


def test_a_missing_token_file_does_not_leak_the_errno(tmp_path):
    with pytest.raises(TokenError) as raised:
        token_from_environment({ENV_TOKEN_FILE: str(tmp_path / "absent")})
    assert "absent" not in str(raised.value)


# --- the header -------------------------------------------------------------


def test_only_a_bearer_header_presents_a_token():
    assert presented_token({"Authorization": f"Bearer {SECRET}"}) == SECRET
    assert presented_token({"authorization": f"Bearer {SECRET}"}) == SECRET
    assert presented_token({"Authorization": f"Basic {SECRET}"}) is None
    assert presented_token({"Authorization": SECRET}) is None
    assert presented_token({"X-Token": SECRET}) is None
    assert presented_token({}) is None


# --- the routes -------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_requires_the_token(api, harness, method, path):
    """Health included: loopback is not a boundary on a shared machine."""
    reply = Client(api, token=None).request(method, path, body={} if method == "POST" else None)
    assert reply.status == 401
    assert reply.json() == {"error": "unauthorized"}
    assert reply.headers.get("WWW-Authenticate") == "Bearer"
    assert harness.calls == [], "nothing was invoked before the request was rejected"


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_a_wrong_secret_is_rejected_everywhere(api, harness, method, path):
    client = Client(api, token=SECRET[:-1] + "Z")
    reply = client.request(method, path, body={} if method == "POST" else None)
    assert reply.status == 401
    assert harness.calls == []


def test_a_token_in_the_query_string_is_not_a_token(api, harness):
    """It would end up in shell history and proxy logs, so it never counts."""
    reply = Client(api, token=None).get(f"/health?token={SECRET}")
    assert reply.status == 401
    reply = Client(api, token=None).get(f"/runs?access_token={SECRET}")
    assert reply.status == 401
    assert harness.calls == []


def test_the_failure_says_nothing_about_the_secret(api):
    reply = Client(api, token="wrong-" + SECRET).get("/status")
    body = reply.body.decode("utf-8")
    assert SECRET not in body
    assert "wrong" not in body
    assert reply.json() == {"error": "unauthorized"}


def test_an_authenticated_request_is_served(api):
    reply = Client(api).get("/health")
    assert reply.status == 200
    assert reply.json()["status"] == "ok"


def test_authentication_precedes_routing(api, harness):
    """An unknown route without a token is 401, not 404: the surface is not enumerable."""
    assert Client(api, token=None).get("/does-not-exist").status == 401
    assert Client(api).get("/does-not-exist").status == 404
    assert harness.calls == []
