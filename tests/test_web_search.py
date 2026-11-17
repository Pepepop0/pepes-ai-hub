"""The Hub's web search settings, and keeping Open WebUI's web search as they say."""

from __future__ import annotations

import json

import httpx
import pytest

from local_ai_stack import web_search_settings as settings_module
from local_ai_stack.open_webui_filters import FilterSync
from local_ai_stack.open_webui_web_search import (
    CONFIG_ENDPOINT,
    PAGE_CHARS,
    RESULT_COUNT,
    configure_web_search,
    desired_fields,
    sync_web_search,
)
from local_ai_stack.web_search_settings import (
    WebSearchSettings,
    WebSearchSettingsError,
    normalize_searxng_url,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The user's config directory in a temporary place, with no DPAPI and no key in the env."""
    monkeypatch.setattr(settings_module, "_config_directory", lambda: tmp_path)
    monkeypatch.delenv(settings_module.BRAVE_KEY_ENV, raising=False)
    monkeypatch.delenv(settings_module.UNSPLASH_KEY_ENV, raising=False)
    monkeypatch.delenv(settings_module.PEXELS_KEY_ENV, raising=False)
    monkeypatch.setattr(settings_module, "protect", lambda data, _label: b"sealed:" + data[::-1])
    monkeypatch.setattr(
        settings_module, "unprotect", lambda data: data.removeprefix(b"sealed:")[::-1]
    )
    return tmp_path


# --- the settings ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "stored"),
    [
        ("", ""),
        ("   ", ""),
        ("http://localhost:8888", "http://localhost:8888/search?q=<query>"),
        ("http://localhost:8888/", "http://localhost:8888/search?q=<query>"),
        ("https://searx.example.org/find", "https://searx.example.org/find?q=<query>"),
        ("http://127.0.0.1:8080/search?q=<query>", "http://127.0.0.1:8080/search?q=<query>"),
    ],
)
def test_a_searxng_address_is_stored_as_the_query_url_open_webui_reads(written, stored):
    assert normalize_searxng_url(written) == stored


@pytest.mark.parametrize(
    "written", ["ftp://host/search", "localhost:8888", "http://user:secret@host/", "http:///x"]
)
def test_a_searxng_address_that_is_not_plain_http_is_refused(written):
    with pytest.raises(WebSearchSettingsError):
        normalize_searxng_url(written)


def test_the_defaults_are_search_on_with_duckduckgo(home):
    loaded = settings_module.load_settings()
    assert loaded == WebSearchSettings(True, "", None) and loaded.engine == "duckduckgo"


def test_the_choices_survive_a_restart_and_the_engine_follows_them(home):
    saved = settings_module.save_settings(False, "http://localhost:8888")
    assert saved.enabled is False and saved.engine == "searxng"
    again = settings_module.load_settings()
    assert again == saved
    assert json.loads((home / settings_module.SETTINGS_FILE).read_text(encoding="utf-8")) == {
        "enabled": False,
        "searxng_url": "http://localhost:8888/search?q=<query>",
    }


def test_a_brave_key_makes_brave_the_engine_unless_a_searxng_address_is_set(home, monkeypatch):
    monkeypatch.setenv(settings_module.BRAVE_KEY_ENV, "from-the-environment")
    loaded = settings_module.load_settings()
    assert loaded.engine == "brave" and loaded.brave_source == settings_module.BRAVE_KEY_ENV
    settings_module.save_settings(True, "http://localhost:8888")
    assert settings_module.load_settings().engine == "searxng"


def test_the_brave_key_is_checked_with_brave_then_kept_sealed_and_never_shown(home):
    seen = []

    def brave(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("x-subscription-token")))
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(brave))
    settings_module.save_brave_key("  brave-secret  ", client=client)
    assert seen == [("api.search.brave.com", "brave-secret")]
    stored = (home / settings_module.BRAVE_KEY_FILE).read_bytes()
    assert b"brave-secret" not in stored, "never plaintext on disk"
    loaded = settings_module.load_settings()
    assert loaded.brave_key == "brave-secret"
    assert "brave-secret" not in json.dumps(loaded.as_dict())
    assert loaded.as_dict()["brave_configured"] is True
    assert loaded.brave_source == "windows-dpapi"

    settings_module.clear_brave_key()
    assert settings_module.load_settings().brave_key is None


def test_a_brave_key_brave_refuses_is_not_kept(home):
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(401)))
    with pytest.raises(WebSearchSettingsError):
        settings_module.save_brave_key("wrong", client=client)
    assert not (home / settings_module.BRAVE_KEY_FILE).exists()


def test_a_failed_brave_check_does_not_echo_the_key(home):
    def offline(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host for secret-key")

    client = httpx.Client(transport=httpx.MockTransport(offline))
    with pytest.raises(WebSearchSettingsError) as caught:
        settings_module.save_brave_key("secret-key", client=client)
    assert "secret-key" not in str(caught.value)


@pytest.mark.parametrize(
    ("provider", "host", "authorization", "environment"),
    [
        ("unsplash", "api.unsplash.com", "Client-ID image-secret", "PEPITO_UNSPLASH_KEY"),
        ("pexels", "api.pexels.com", "image-secret", "PEPITO_PEXELS_KEY"),
    ],
)
def test_an_optional_image_key_is_verified_sealed_and_only_exposed_to_the_filter(
    home, provider, host, authorization, environment
):
    seen = []

    def verify(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(verify))
    saved = settings_module.save_image_key(provider, " image-secret ", client=client)
    assert seen == [(host, authorization)]
    stored = settings_module.image_key_path(provider).read_bytes()
    assert b"image-secret" not in stored
    assert "image-secret" not in json.dumps(saved.as_dict())
    assert saved.image_search_env() == {environment: "image-secret"}
    assert (
        saved.unsplash_source if provider == "unsplash" else saved.pexels_source
    ) == "windows-dpapi"

    cleared = settings_module.clear_image_key(provider)
    assert not settings_module.image_key_path(provider).exists()
    assert cleared.image_search_env() == {}


def test_an_unknown_image_provider_is_refused_without_writing_a_secret(home):
    with pytest.raises(WebSearchSettingsError, match="desconhecido"):
        settings_module.save_image_key("elsewhere", "secret")
    assert list(home.glob("*key*")) == []


# --- Open WebUI's side ------------------------------------------------------------------------


class RetrievalConfig:
    """Open WebUI's /api/v1/retrieval/config: GET returns the form, update writes what it is sent."""

    def __init__(self, web: dict | None = None) -> None:
        self.web = dict(web or {})
        self.updates: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == CONFIG_ENDPOINT:
            return httpx.Response(200, json={"web": dict(self.web), "TOP_K": 3})
        if request.method == "POST" and request.url.path == f"{CONFIG_ENDPOINT}/update":
            body = json.loads(request.content)
            self.updates.append(body)
            self.web.update(body["web"])
            return httpx.Response(200, json={"status": True})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://owui", transport=httpx.MockTransport(self))


def test_the_hubs_web_fields_are_written_once_and_only_those_fields():
    owui = RetrievalConfig({"ENABLE_WEB_SEARCH": False, "WEB_SEARCH_ENGINE": ""})
    settings = WebSearchSettings(True, "", None)
    with owui.client() as client:
        assert configure_web_search(client, settings) is FilterSync.INSTALLED
        assert configure_web_search(client, settings) is FilterSync.UNCHANGED
    (update,) = owui.updates
    assert set(update) == {"web"}, "nothing outside the web section is sent"
    assert update["web"] == {
        "ENABLE_WEB_SEARCH": True,
        "WEB_SEARCH_ENGINE": "duckduckgo",
        "WEB_SEARCH_RESULT_COUNT": RESULT_COUNT,
        "WEB_SEARCH_CONCURRENT_REQUESTS": 1,
        "BYPASS_WEB_SEARCH_EMBEDDING_AND_RETRIEVAL": True,
        "WEB_FETCH_MAX_CONTENT_LENGTH": PAGE_CHARS,
        "DDGS_BACKEND": "auto",
    }


def test_turning_search_off_or_changing_the_engine_is_applied_to_a_running_instance():
    owui = RetrievalConfig()
    with owui.client() as client:
        configure_web_search(client, WebSearchSettings(True, "", None))
        assert (
            configure_web_search(client, WebSearchSettings(False, "", None)) is FilterSync.UPDATED
        )
        assert owui.web["ENABLE_WEB_SEARCH"] is False
        searx = WebSearchSettings(True, "http://localhost:8888/search?q=<query>", None)
        assert configure_web_search(client, searx) is FilterSync.UPDATED
        assert owui.web["WEB_SEARCH_ENGINE"] == "searxng"
        assert owui.web["SEARXNG_QUERY_URL"] == "http://localhost:8888/search?q=<query>"
        brave = WebSearchSettings(True, "", "brave-secret")
        configure_web_search(client, brave)
        assert owui.web["WEB_SEARCH_ENGINE"] == "brave"
        assert owui.web["BRAVE_SEARCH_API_KEY"] == "brave-secret"


def test_the_desired_fields_name_a_searxng_address_or_a_key_only_for_that_engine():
    assert "SEARXNG_QUERY_URL" not in desired_fields(WebSearchSettings(True, "", "k"))
    assert "BRAVE_SEARCH_API_KEY" not in desired_fields(
        WebSearchSettings(True, "http://h/search?q=<query>", "k")
    )


def test_a_retention_failure_is_an_error_not_a_silent_success():
    class Forgetful(RetrievalConfig):
        def __call__(self, request):
            response = super().__call__(request)
            if request.method == "POST":
                self.web.clear()
            return response

    with Forgetful().client() as client, pytest.raises(RuntimeError, match="did not retain"):
        configure_web_search(client, WebSearchSettings())


def test_the_sync_without_an_administrator_waits_and_an_unreachable_instance_never_raises(
    tmp_path, monkeypatch
):
    from local_ai_stack import open_webui_web_search as module

    waiting = sync_web_search(tmp_path, "http://owui", WebSearchSettings())
    assert waiting.outcome is FilterSync.NO_ADMIN

    monkeypatch.setattr(module, "local_admin_token", lambda _dir: "token")

    def refuse(_url: str, _bearer: str) -> httpx.Client:
        return httpx.Client(
            base_url="http://owui",
            transport=httpx.MockTransport(
                lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused"))
            ),
        )

    failed = sync_web_search(tmp_path, "http://owui", WebSearchSettings(), client_factory=refuse)
    assert failed.outcome is FilterSync.UNAVAILABLE and "ConnectError" in failed.detail


def test_the_settings_are_read_when_the_sync_runs_not_when_it_was_built(tmp_path, monkeypatch):
    from local_ai_stack import open_webui_web_search as module

    monkeypatch.setattr(module, "local_admin_token", lambda _dir: "token")
    owui = RetrievalConfig()
    current = {"settings": WebSearchSettings(True)}

    def factory(_url: str, _bearer: str) -> httpx.Client:
        return owui.client()

    report = lambda: sync_web_search(
        tmp_path, "http://owui", lambda: current["settings"], client_factory=factory
    )
    assert report().outcome is FilterSync.INSTALLED
    current["settings"] = WebSearchSettings(False)
    assert report().outcome is FilterSync.UPDATED and owui.web["ENABLE_WEB_SEARCH"] is False
