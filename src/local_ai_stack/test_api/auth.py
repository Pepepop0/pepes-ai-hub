"""The secret every route requires, health included.

The server is loopback-only, but loopback is not a boundary: anything running
as this user can reach it. The token is what actually separates the test
control surface from the rest of the machine, so it is required before a
request is routed, parsed, or acted on.

The token is held as a digest rather than as text. Comparison is then constant
time on a fixed-length value regardless of what was presented, and the secret
itself is not sitting in an object that a traceback or a debugger dump would
print. ``repr`` is redacted for the same reason.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

ENV_TOKEN = "PEPITOS_AI_HUB_TEST_TOKEN"
ENV_TOKEN_FILE = "PEPITOS_AI_HUB_TEST_TOKEN_FILE"

AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "

# 256 bits of entropy is 43 characters of base64url. The token is not padded,
# stretched, or hashed into range for the caller: a short secret is refused.
MINIMUM_TOKEN_CHARS = 43


class TokenError(ValueError):
    """No usable token was supplied, so the server will not start."""


class TestApiToken:
    """A secret that can be compared and cannot be printed."""

    __test__ = False
    __slots__ = ("_digest",)

    def __init__(self, secret: str) -> None:
        cleaned = (secret or "").strip()
        if len(cleaned) < MINIMUM_TOKEN_CHARS:
            raise TokenError(
                "the test control token must carry at least 256 bits of entropy "
                f"({MINIMUM_TOKEN_CHARS} characters); it is not padded for you"
            )
        self._digest = hashlib.sha256(cleaned.encode("utf-8")).digest()

    def matches(self, presented: str | None) -> bool:
        """Constant-time comparison against whatever the request offered."""
        if not presented:
            return False
        offered = hashlib.sha256(presented.encode("utf-8")).digest()
        return hmac.compare_digest(self._digest, offered)

    def __repr__(self) -> str:
        return "<TestApiToken redacted>"

    __str__ = __repr__


def generate_token() -> str:
    """A fresh secret for a dev/test session. Printed once, never stored here."""
    return secrets.token_urlsafe(32)


def token_from_environment(env: Mapping[str, str] | None = None) -> TestApiToken:
    """Read the token from the environment or from a file the environment names."""
    environ = os.environ if env is None else env
    inline = (environ.get(ENV_TOKEN) or "").strip()
    if inline:
        return TestApiToken(inline)
    location = (environ.get(ENV_TOKEN_FILE) or "").strip()
    if not location:
        raise TokenError(
            f"set {ENV_TOKEN} or {ENV_TOKEN_FILE}: the test control server has no default "
            "secret and will not start without one"
        )
    path = Path(location)
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        # The path is the operator's own input; the errno is not.
        raise TokenError(f"cannot read the token file named by {ENV_TOKEN_FILE}") from error
    return TestApiToken(contents)


def presented_token(headers: Mapping[str, str]) -> str | None:
    """The bearer token in the request, or nothing.

    Only the header is read. A token in a query string would be written into
    proxy logs, shell history and browser history by everything it passes
    through, so a request that puts it there is a request with no token.
    """
    raw = headers.get(AUTHORIZATION_HEADER) or headers.get(AUTHORIZATION_HEADER.lower())
    if not raw or not raw.startswith(BEARER_PREFIX):
        return None
    return raw[len(BEARER_PREFIX) :].strip() or None


__all__ = [
    "AUTHORIZATION_HEADER",
    "BEARER_PREFIX",
    "ENV_TOKEN",
    "ENV_TOKEN_FILE",
    "MINIMUM_TOKEN_CHARS",
    "TestApiToken",
    "TokenError",
    "generate_token",
    "presented_token",
    "token_from_environment",
]
