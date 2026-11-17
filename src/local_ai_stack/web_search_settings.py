"""The Hub's own web search settings, kept per Windows user.

What the Capella chat may do on the web is the user's choice, made in the Hub:
on or off, an optional SearXNG address, an optional Brave Search key. With
none of that set, the engine is DuckDuckGo through Open WebUI's own ``ddgs``
library: no account, no key, no published quota (it meta-searches several
engines and can be rate limited when it is asked too often).

The non-secret choices are a small JSON file in the user's config directory.
The Brave key is never written as plaintext: on Windows the current user's
DPAPI key protects it, and ``BRAVE_SEARCH_API_KEY`` in the environment wins.
Open WebUI keeps what it is given in its own database, as it does for every
setting; the Hub writes it there through the authenticated admin API
(``open_webui_web_search``), so this module only owns the choices.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from platformdirs import user_config_path

from .windows_credentials import protect, unprotect

SETTINGS_FILE = "web-search.json"
BRAVE_KEY_FILE = "brave-key.dpapi"
BRAVE_KEY_ENV = "BRAVE_SEARCH_API_KEY"
BRAVE_CHECK_URL = "https://api.search.brave.com/res/v1/web/search"
UNSPLASH_KEY_FILE = "unsplash-key.dpapi"
UNSPLASH_KEY_ENV = "PEPITO_UNSPLASH_KEY"
UNSPLASH_CHECK_URL = "https://api.unsplash.com/photos"
PEXELS_KEY_FILE = "pexels-key.dpapi"
PEXELS_KEY_ENV = "PEPITO_PEXELS_KEY"
PEXELS_CHECK_URL = "https://api.pexels.com/v1/curated"
QUERY_MARK = "<query>"
ENGINE_DUCKDUCKGO = "duckduckgo"
ENGINE_SEARXNG = "searxng"
ENGINE_BRAVE = "brave"
IMAGE_KEY_PROVIDERS = frozenset({"unsplash", "pexels"})


class WebSearchSettingsError(ValueError):
    """A setting the Hub cannot accept or keep."""


@dataclass(frozen=True)
class WebSearchSettings:
    enabled: bool = True
    searxng_url: str = ""
    brave_key: str | None = None
    unsplash_key: str | None = None
    pexels_key: str | None = None
    brave_source: str | None = None
    unsplash_source: str | None = None
    pexels_source: str | None = None

    @property
    def engine(self) -> str:
        """A SearXNG address wins (the best aggregator), then a Brave key, then DuckDuckGo."""
        if self.searxng_url:
            return ENGINE_SEARXNG
        if self.brave_key:
            return ENGINE_BRAVE
        return ENGINE_DUCKDUCKGO

    def image_search_env(self) -> dict[str, str]:
        """Secrets the standalone Open WebUI filter reads from its process environment."""
        values = (
            (UNSPLASH_KEY_ENV, self.unsplash_key),
            (PEXELS_KEY_ENV, self.pexels_key),
        )
        return {name: value for name, value in values if value}

    def as_dict(self) -> dict[str, object]:
        """What the interface may show: keys themselves never leave this module."""
        return {
            "enabled": self.enabled,
            "engine": self.engine,
            "searxng_url": self.searxng_url,
            "brave_configured": bool(self.brave_key),
            "unsplash_configured": bool(self.unsplash_key),
            "pexels_configured": bool(self.pexels_key),
            "brave_source": self.brave_source,
            "unsplash_source": self.unsplash_source,
            "pexels_source": self.pexels_source,
        }


def _config_directory() -> Path:
    return Path(user_config_path("PepitosAIHub", appauthor=False))


def settings_path() -> Path:
    return _config_directory() / SETTINGS_FILE


def brave_key_path() -> Path:
    return _config_directory() / BRAVE_KEY_FILE


def image_key_path(provider: str) -> Path:
    if provider == "unsplash":
        return _config_directory() / UNSPLASH_KEY_FILE
    if provider == "pexels":
        return _config_directory() / PEXELS_KEY_FILE
    raise WebSearchSettingsError("Provedor de imagens desconhecido.")


def _image_key_env(provider: str) -> str:
    if provider == "unsplash":
        return UNSPLASH_KEY_ENV
    if provider == "pexels":
        return PEXELS_KEY_ENV
    raise WebSearchSettingsError("Provedor de imagens desconhecido.")


def load_image_key(provider: str) -> tuple[str | None, str | None]:
    """(key, source): environment first, then this provider's DPAPI file."""
    environment = _image_key_env(provider)
    key = os.environ.get(environment, "").strip()
    if key:
        return key, environment
    path = image_key_path(provider)
    if not path.is_file():
        return None, None
    try:
        stored = unprotect(path.read_bytes()).decode("utf-8").strip()
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise WebSearchSettingsError(
            f"A chave guardada do {provider.title()} não pôde ser lida."
        ) from exc
    return (stored or None), ("windows-dpapi" if stored else None)


def normalize_searxng_url(value: str) -> str:
    """A SearXNG query URL in the form Open WebUI wants, or "" for none.

    ``http://localhost:8888`` becomes ``http://localhost:8888/search?q=<query>``.
    Only http(s) with a host; no credentials in the address, which is stored in
    the clear.
    """
    text = (value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise WebSearchSettingsError("O endereço do SearXNG deve ser uma URL http ou https.")
    if parts.username or parts.password:
        raise WebSearchSettingsError("O endereço do SearXNG não deve conter usuário nem senha.")
    if QUERY_MARK in text:
        return text
    path = parts.path.rstrip("/") or "/search"
    return urlunsplit((parts.scheme, parts.netloc, path, f"q={QUERY_MARK}", ""))


def load_brave_key() -> tuple[str | None, str | None]:
    """(key, source): the environment first, then the DPAPI-protected file."""
    key = os.environ.get(BRAVE_KEY_ENV, "").strip()
    if key:
        return key, BRAVE_KEY_ENV
    path = brave_key_path()
    if not path.is_file():
        return None, None
    try:
        stored = unprotect(path.read_bytes()).decode("utf-8").strip()
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise WebSearchSettingsError("A chave guardada do Brave Search não pôde ser lida.") from exc
    return (stored or None), ("windows-dpapi" if stored else None)


def load_settings() -> WebSearchSettings:
    """The settings as the user left them; defaults when nothing was ever saved."""
    enabled, searxng_url = True, ""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        enabled = data.get("enabled") is not False
        try:
            searxng_url = normalize_searxng_url(str(data.get("searxng_url") or ""))
        except WebSearchSettingsError:
            searxng_url = ""
    keys: dict[str, str | None] = {}
    sources: dict[str, str | None] = {}
    for provider, loader in (
        ("brave", load_brave_key),
        ("unsplash", lambda: load_image_key("unsplash")),
        ("pexels", lambda: load_image_key("pexels")),
    ):
        try:
            keys[provider], sources[provider] = loader()
        except WebSearchSettingsError:
            keys[provider] = sources[provider] = None
    return WebSearchSettings(
        enabled,
        searxng_url,
        keys["brave"],
        keys["unsplash"],
        keys["pexels"],
        sources["brave"],
        sources["unsplash"],
        sources["pexels"],
    )


def save_settings(enabled: bool, searxng_url: str = "") -> WebSearchSettings:
    """Keep the user's choices. Raises when the SearXNG address is not acceptable."""
    if not isinstance(enabled, bool):
        raise WebSearchSettingsError("A pesquisa web deve ser ligada ou desligada.")
    normalized = normalize_searxng_url(searxng_url)
    destination = settings_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps({"enabled": enabled, "searxng_url": normalized}), encoding="utf-8"
        )
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise WebSearchSettingsError(
            "Não foi possível salvar as configurações da pesquisa web."
        ) from exc
    return load_settings()


def check_brave_key(key: str, *, client: httpx.Client | None = None) -> None:
    """Ask Brave for one result with this key; raise when it is not accepted."""
    owns_client = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        response = session.get(
            BRAVE_CHECK_URL,
            params={"q": "test", "count": "1"},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        if response.status_code in {401, 403, 422}:
            raise WebSearchSettingsError("O Brave Search recusou a chave.")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WebSearchSettingsError(
            "Não foi possível verificar a chave do Brave Search. Confira a conexão."
        ) from exc
    finally:
        if owns_client:
            session.close()


def save_brave_key(key: str, *, client: httpx.Client | None = None) -> None:
    """Verify the key with Brave, then keep it protected for this Windows user."""
    key = (key or "").strip()
    if not key:
        raise WebSearchSettingsError("Informe a chave do Brave Search.")
    check_brave_key(key, client=client)
    destination = brave_key_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_bytes(protect(key.encode("utf-8"), "PepitosAIHub Brave Search key"))
        os.replace(temporary, destination)
    except (OSError, RuntimeError) as exc:
        temporary.unlink(missing_ok=True)
        raise WebSearchSettingsError(
            "Não foi possível proteger a chave do Brave Search com o Windows DPAPI."
        ) from exc


def clear_brave_key() -> None:
    brave_key_path().unlink(missing_ok=True)


def check_image_key(provider: str, key: str, *, client: httpx.Client | None = None) -> None:
    """Make one bounded provider request; a refused or unusable key is not saved."""
    if provider not in IMAGE_KEY_PROVIDERS:
        raise WebSearchSettingsError("Provedor de imagens desconhecido.")
    owns_client = client is None
    session = client or httpx.Client(timeout=30.0, follow_redirects=False)
    if provider == "unsplash":
        url = UNSPLASH_CHECK_URL
        headers = {"Authorization": f"Client-ID {key}", "Accept-Version": "v1"}
    else:
        url = PEXELS_CHECK_URL
        headers = {"Authorization": key}
    try:
        response = session.get(url, params={"per_page": "1"}, headers=headers)
        if response.status_code in {401, 403, 422, 429}:
            raise WebSearchSettingsError(f"O {provider.title()} recusou a chave.")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise WebSearchSettingsError(
            f"Não foi possível verificar a chave do {provider.title()}. Confira a conexão."
        ) from exc
    finally:
        if owns_client:
            session.close()


def save_image_key(
    provider: str, key: str, *, client: httpx.Client | None = None
) -> WebSearchSettings:
    """Verify and protect an optional image-search provider key."""
    key = (key or "").strip()
    if not key:
        raise WebSearchSettingsError("Informe a chave do provedor de imagens.")
    check_image_key(provider, key, client=client)
    destination = image_key_path(provider)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_bytes(protect(key.encode("utf-8"), f"PepitosAIHub {provider.title()} key"))
        os.replace(temporary, destination)
    except (OSError, RuntimeError) as exc:
        temporary.unlink(missing_ok=True)
        raise WebSearchSettingsError(
            f"Não foi possível proteger a chave do {provider.title()} com o Windows DPAPI."
        ) from exc
    return load_settings()


def clear_image_key(provider: str) -> WebSearchSettings:
    image_key_path(provider).unlink(missing_ok=True)
    return load_settings()


__all__ = [
    "BRAVE_KEY_ENV",
    "ENGINE_BRAVE",
    "ENGINE_DUCKDUCKGO",
    "ENGINE_SEARXNG",
    "PEXELS_KEY_ENV",
    "UNSPLASH_KEY_ENV",
    "WebSearchSettings",
    "WebSearchSettingsError",
    "check_brave_key",
    "check_image_key",
    "clear_brave_key",
    "clear_image_key",
    "load_brave_key",
    "load_image_key",
    "load_settings",
    "normalize_searxng_url",
    "save_brave_key",
    "save_image_key",
    "save_settings",
]
