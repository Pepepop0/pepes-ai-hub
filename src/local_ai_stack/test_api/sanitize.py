"""Bounding what leaves the server.

Two different obligations meet here.

The status projection is assembled by this package out of whatever a status
source hands it, so this package is responsible for what it publishes: a
filesystem path, a non-local URL or anything named like a secret is replaced
rather than forwarded. Loopback URLs survive, because "where does this service
listen" is the answer status exists to give.

Events are different. Their message is sanitized by the harness that produced
it, and rewriting it here would change what an event means. The only thing
applied to them is a bound: a long message is truncated and oversized data is
replaced by its size, so a run cannot turn the stream into a log dump.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence

REDACTED = "[redacted]"
TRUNCATED_SUFFIX = "…"

MAX_STRING_CHARS = 200
MAX_DEPTH = 6
MAX_ITEMS = 64

_SECRET_NAMES = re.compile(r"token|secret|password|passwd|authorization|api[_-]?key", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
_POSIX_PATH = re.compile(r"^/[^/]")
_URL = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/:]+)", re.IGNORECASE)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})


def _is_path_like(value: str) -> bool:
    return bool(_WINDOWS_PATH.search(value) or _POSIX_PATH.search(value))


def _is_foreign_url(value: str) -> bool:
    match = _URL.match(value)
    return bool(match) and match.group("host").lower() not in _LOOPBACK_HOSTS


def sanitize_text(value: str) -> str:
    """A string safe to publish: no path, no foreign URL, bounded length."""
    if _is_path_like(value) or _is_foreign_url(value):
        return REDACTED
    return bound_text(value)


def bound_text(value: str, *, limit: int = MAX_STRING_CHARS) -> str:
    """The same string, or as much of it as fits."""
    if len(value) <= limit:
        return value
    return value[:limit] + TRUNCATED_SUFFIX


def sanitize(value: object, *, depth: int = 0) -> object:
    """Recursively bound and redact a status payload into JSON-safe values."""
    if depth > MAX_DEPTH:
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for key, item in list(value.items())[:MAX_ITEMS]:
            name = str(key)
            cleaned[name] = (
                REDACTED if _SECRET_NAMES.search(name) else sanitize(item, depth=depth + 1)
            )
        return cleaned
    if isinstance(value, Sequence):
        return [sanitize(item, depth=depth + 1) for item in list(value)[:MAX_ITEMS]]
    return sanitize_text(str(value))


def json_safe(value: object) -> bool:
    """Whether a value can be serialized at all, checked before it is stored."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "MAX_STRING_CHARS",
    "REDACTED",
    "bound_text",
    "json_safe",
    "sanitize",
    "sanitize_text",
]
