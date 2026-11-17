"""Keep Open WebUI's web search set as the Hub's settings say.

Open WebUI 0.12.0 keeps its web search settings in its own database: launch
environment variables only seed keys that do not exist yet. They are written
through the authenticated admin API (``/api/v1/retrieval/config/update``), the
same form Admin -> Web Search saves; only the fields the Hub owns are sent, and
Open WebUI writes only what it is sent.

The Hub owns them on a profile that opts in (``runtime.web_search``), so a
change made in the Hub's settings reaches the running instance the next time
the Hub integrates with it (opening Open WebUI from the Hub, a restart). Page
text is loaded as it is and placed in the model's context without an embedding
model (``BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL``), each page capped, so a
search costs no download and a bounded amount of context.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from .open_webui_filters import FilterSync, FilterSyncReport, local_admin_token
from .web_search_settings import ENGINE_BRAVE, ENGINE_SEARXNG, WebSearchSettings

CONFIG_ENDPOINT = "/api/v1/retrieval/config"
RESULT_COUNT = 4
# Characters kept from each loaded page: four pages of this size fit the chat's context.
PAGE_CHARS = 12000


def desired_fields(settings: WebSearchSettings) -> dict[str, object]:
    """The web search fields the Hub owns, as Open WebUI's retrieval form names them."""
    fields: dict[str, object] = {
        "ENABLE_WEB_SEARCH": settings.enabled,
        "WEB_SEARCH_ENGINE": settings.engine,
        "WEB_SEARCH_RESULT_COUNT": RESULT_COUNT,
        # One page at a time: DuckDuckGo's library is rate limited when it is hammered.
        "WEB_SEARCH_CONCURRENT_REQUESTS": 1,
        "BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL": True,
        "WEB_FETCH_MAX_CONTENT_LENGTH": PAGE_CHARS,
        "DDGS_BACKEND": "auto",
    }
    if settings.engine == ENGINE_SEARXNG:
        fields["SEARXNG_QUERY_URL"] = settings.searxng_url
    if settings.engine == ENGINE_BRAVE and settings.brave_key:
        fields["BRAVE_SEARCH_API_KEY"] = settings.brave_key
    return fields


def matches(config: Mapping[str, object], desired: Mapping[str, object]) -> bool:
    web = config.get("web")
    return isinstance(web, Mapping) and all(
        web.get(field) == value for field, value in desired.items()
    )


def configure_web_search(client: httpx.Client, settings: WebSearchSettings) -> FilterSync:
    """Write the Hub's fields when they drifted, then read them back."""
    response = client.get(CONFIG_ENDPOINT)
    response.raise_for_status()
    desired = desired_fields(settings)
    config = response.json()
    if matches(config, desired):
        return FilterSync.UNCHANGED
    web = config.get("web") if isinstance(config.get("web"), Mapping) else {}
    never_set = not web.get("ENABLE_WEB_SEARCH") and not web.get("WEB_SEARCH_ENGINE")
    result = client.post(f"{CONFIG_ENDPOINT}/update", json={"web": desired})
    result.raise_for_status()
    verified = client.get(CONFIG_ENDPOINT)
    verified.raise_for_status()
    if not matches(verified.json(), desired):
        raise RuntimeError("Open WebUI did not retain the web search configuration")
    return FilterSync.INSTALLED if never_set else FilterSync.UPDATED


def sync_web_search(
    data_dir: Path,
    base_url: str,
    settings: WebSearchSettings | Callable[[], WebSearchSettings],
    *,
    client_factory: Callable[[str, str], httpx.Client] | None = None,
) -> FilterSyncReport:
    """Bring the managed instance's web search settings up to date. Idempotent, never raises.

    ``settings`` may be a callable read when the sync runs, so a change made in
    the Hub since the closure was built is the one applied. No administrator
    yet -> reported, not attempted; an unreachable or refusing instance ->
    unavailable (the detail names the class of the failure, never a key).
    """
    token = local_admin_token(data_dir)
    if token is None:
        return FilterSyncReport(
            FilterSync.NO_ADMIN,
            "Open WebUI has no administrator yet; web search is configured on a later start",
        )
    factory = client_factory or (
        lambda url, bearer: httpx.Client(
            base_url=url, headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0
        )
    )
    chosen = settings() if callable(settings) else settings
    try:
        with factory(base_url, token) as client:
            outcome = configure_web_search(client, chosen)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return FilterSyncReport(FilterSync.UNAVAILABLE, f"web search: {type(exc).__name__}")
    return FilterSyncReport(outcome, f"web search: {outcome.value}")


__all__ = [
    "CONFIG_ENDPOINT",
    "configure_web_search",
    "desired_fields",
    "matches",
    "sync_web_search",
]
