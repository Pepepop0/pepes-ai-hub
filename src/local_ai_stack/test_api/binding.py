"""Where the test control server is allowed to listen.

The stack already made this decision once: :data:`local_ai_stack.endpoints
.DEFAULT_HOST` is the literal IPv4 loopback address, and nothing in the stack
resolves a name to find a local service. This module applies the same rule to
the test server and refuses everything else.

That refusal includes ``localhost``. Resolving a name would hand the choice of
interface to whatever answers the lookup, and the server exists to be reachable
from this machine only. There is deliberately no override: a non-loopback bind
is an error, not an option, and remote access belongs behind an authenticated
tunnel that terminates here.
"""

from __future__ import annotations

import ipaddress

from ..endpoints import DEFAULT_HOST

LOOPBACK_HOST = DEFAULT_HOST


class BindRefused(ValueError):
    """The requested address is not loopback, so the server will not start."""


def resolve_bind_host(host: str = LOOPBACK_HOST) -> str:
    """The literal address to bind, or a refusal. Never a name, never a lookup."""
    candidate = (host or "").strip()
    if not candidate:
        raise BindRefused("a bind address is required: the test server has no implicit interface")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        raise BindRefused(
            f"refusing to bind {candidate!r}: the test server binds a literal loopback "
            "address and never resolves a name"
        ) from None
    if not address.is_loopback:
        raise BindRefused(
            f"refusing to bind {candidate!r}: the test server is loopback-only and offers "
            "no override; remote access belongs behind an authenticated tunnel"
        )
    return str(address)


__all__ = ["LOOPBACK_HOST", "BindRefused", "resolve_bind_host"]
