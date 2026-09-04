"""Minimal Ollama model-store client.

Ollama owns its own model store: there is no file this hub may place, verify or
delete, so the only honest way to ask what is installed is to ask Ollama.

The HTTP API is used rather than the CLI. ``GET /api/tags`` returns JSON with a
name and a digest per model, and ``POST /api/pull`` performs the pull — both
are documented contracts, where ``ollama list`` is a formatted table whose
columns are free to change. Nothing here parses human-readable output.

Two limits are deliberate:

- nothing removes or overwrites a model. A store shared with whatever the user
  pulled themselves is only ever read from and added to;
- the server has to be reachable. When it is not, the client says so and the
  caller reports the model as not installed rather than guessing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from .runner import sanitize

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
LIST_TIMEOUT = 10.0
# A pull downloads gigabytes over a slow link; the ceiling is generous but real.
PULL_TIMEOUT = 3 * 60 * 60.0


class OllamaClientError(RuntimeError):
    """The Ollama model store could not be reached or refused an operation."""


@dataclass(frozen=True)
class OllamaModelEntry:
    """One model as the local Ollama reports it."""

    name: str
    digest: str = ""
    size_bytes: int | None = None

    def matches_digest(self, expected: str | None) -> bool:
        """True when the local copy is the pinned one.

        Published digests are abbreviated, so a prefix match against the full
        digest the server reports is the comparison that actually applies.
        """
        if not expected:
            return True
        local = self.digest.split(":", 1)[-1].lower()
        return local.startswith(expected.lower())


@runtime_checkable
class OllamaModelClient(Protocol):
    """What model acquisition needs from Ollama. Injectable for tests."""

    def list_models(self) -> tuple[OllamaModelEntry, ...]: ...

    def pull(self, reference: str) -> None: ...


class HttpOllamaClient:
    """Talks to a running Ollama over its documented HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        list_timeout: float = LIST_TIMEOUT,
        pull_timeout: float = PULL_TIMEOUT,
        log: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_factory = client_factory
        self.list_timeout = list_timeout
        self.pull_timeout = pull_timeout
        self.log = log or logger

    def _client(self, timeout: float) -> httpx.Client:
        if self.client_factory is not None:
            return self.client_factory()
        return httpx.Client(timeout=timeout)

    def list_models(self) -> tuple[OllamaModelEntry, ...]:
        """Every model in the local store. Read-only."""
        try:
            with self._client(self.list_timeout) as client:
                response = client.get(f"{self.base_url}/api/tags", timeout=self.list_timeout)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaClientError(
                f"could not read the Ollama model store: {sanitize(str(exc))}"
            ) from exc

        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            raise OllamaClientError("the Ollama model store returned an unexpected payload")

        entries: list[OllamaModelEntry] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("model")
            if not isinstance(name, str) or not name:
                continue
            digest = item.get("digest")
            size = item.get("size")
            entries.append(
                OllamaModelEntry(
                    name=name,
                    digest=digest if isinstance(digest, str) else "",
                    size_bytes=size if isinstance(size, int) else None,
                )
            )
        return tuple(entries)

    def pull(self, reference: str) -> None:
        """Add a model to the store. Never removes or replaces anything else."""
        try:
            with self._client(self.pull_timeout) as client:
                response = client.post(
                    f"{self.base_url}/api/pull",
                    json={"model": reference, "stream": False},
                    timeout=self.pull_timeout,
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OllamaClientError(f"could not pull {reference}: {sanitize(str(exc))}") from exc

        # A non-streaming pull answers {"status": "success"} on completion.
        if isinstance(payload, dict):
            status = payload.get("status")
            error = payload.get("error")
            if error:
                raise OllamaClientError(f"could not pull {reference}: {sanitize(str(error))}")
            if isinstance(status, str) and status.lower() not in {"success", "ok"}:
                raise OllamaClientError(f"pull of {reference} ended as {status!r}")


__all__ = [
    "DEFAULT_BASE_URL",
    "HttpOllamaClient",
    "OllamaClientError",
    "OllamaModelClient",
    "OllamaModelEntry",
]
