"""The guest link on the Linux bench: sign-ups opened through the admin API, a pinned
cloudflared. HTTP is an httpx MockTransport; no binary is downloaded or run."""

from __future__ import annotations

import hashlib
import io
import json

import httpx
import pytest

from local_ai_stack import linux_share
from local_ai_stack.open_webui_filters import FilterSync

# What Open WebUI 0.12.0 GET /api/v1/auths/admin/config returns on a fresh install.
FRESH = {
    "ENABLE_MFA": False,
    "MFA_ALLOW_OAUTH_BYPASS": False,
    "MFA_ALLOW_TRUSTED_HEADER_BYPASS": False,
    "SHOW_ADMIN_DETAILS": True,
    "ADMIN_EMAIL": None,
    "WEBUI_URL": None,
    "ENABLE_LOGIN_FORM": True,
    "ENABLE_SIGNUP": False,
    "ENABLE_API_KEYS": False,
    "ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS": False,
    "API_KEYS_ALLOWED_ENDPOINTS": "",
    "DEFAULT_USER_ROLE": "pending",
    "DEFAULT_GROUP_ID": None,
    "DEFAULT_INTERFACE_SETTINGS": {"theme": "dark"},
    "I18N": None,
    "JWT_EXPIRES_IN": "4w",
    "ENABLE_COMMUNITY_SHARING": True,
    "ENABLE_MESSAGE_RATING": True,
    "ENABLE_FOLDERS": True,
    "FOLDER_MAX_FILE_COUNT": "",
}
# AdminConfig fields without a default: a form missing one is a 422.
REQUIRED = (
    "SHOW_ADMIN_DETAILS",
    "WEBUI_URL",
    "ENABLE_SIGNUP",
    "ENABLE_API_KEYS",
    "API_KEYS_ALLOWED_ENDPOINTS",
    "DEFAULT_USER_ROLE",
    "DEFAULT_GROUP_ID",
    "JWT_EXPIRES_IN",
)


class AdminApi:
    def __init__(self, config, *, keep=True):
        self.config = dict(config)
        self.keep = keep
        self.posted = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == linux_share.CONFIG_ENDPOINT
        if request.method == "GET":
            return httpx.Response(200, json=self.config)
        form = json.loads(request.content)
        self.posted.append(form)
        missing = [field for field in REQUIRED if not isinstance(form.get(field), (str, bool))]
        if missing or form.get("I18N", {}) is None:
            return httpx.Response(422, json={"detail": missing or "I18N"})
        if self.keep:
            self.config.update(form)
        return httpx.Response(200, json=self.config)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://owui", transport=httpx.MockTransport(self))


def test_sign_ups_open_as_users_and_the_rest_is_sent_back_as_it_was():
    api = AdminApi(FRESH)
    with api.client() as client:
        assert linux_share.open_signups(client) is FilterSync.UPDATED
    (form,) = api.posted
    assert form["ENABLE_SIGNUP"] is True and form["DEFAULT_USER_ROLE"] == "user"
    # MFA is only written when sent: never touched. I18N null would be a 422.
    assert not {"ENABLE_MFA", "MFA_ALLOW_OAUTH_BYPASS", "I18N"} & set(form)
    assert form["DEFAULT_INTERFACE_SETTINGS"] == {"theme": "dark"}, "omitting it resets it"
    assert form["JWT_EXPIRES_IN"] == "4w" and form["ENABLE_COMMUNITY_SHARING"] is True


def test_already_open_sign_ups_are_not_written_again():
    api = AdminApi({**FRESH, "ENABLE_SIGNUP": True, "DEFAULT_USER_ROLE": "user"})
    with api.client() as client:
        assert linux_share.open_signups(client) is FilterSync.UNCHANGED
    assert api.posted == []


def test_a_setting_open_webui_did_not_keep_is_reported_not_announced(tmp_path, monkeypatch):
    api = AdminApi(FRESH, keep=False)
    monkeypatch.setattr(linux_share, "local_admin_token", lambda _data: "token")
    report = linux_share.sync_open_signups(
        tmp_path, "http://owui", client_factory=lambda _url, _token: api.client()
    )
    assert report.outcome is FilterSync.UNAVAILABLE and "RuntimeError" in report.detail


def test_no_administrator_means_no_link(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_share, "local_admin_token", lambda _data: None)
    report = linux_share.sync_open_signups(tmp_path, "http://owui")
    assert report.outcome is FilterSync.NO_ADMIN


def test_cloudflared_is_kept_only_when_it_matches_the_pin(tmp_path, monkeypatch):
    good = b"pinned cloudflared"
    monkeypatch.setattr(linux_share, "CLOUDFLARED_SHA256", hashlib.sha256(good).hexdigest())
    with pytest.raises(RuntimeError, match="SHA-256"):
        linux_share.ensure_cloudflared(tmp_path, fetch=lambda _url: [b"tampered"])
    assert list(tmp_path.iterdir()) == [], "nothing half-downloaded is left"
    fetched = []

    def fetch(url):
        fetched.append(url)
        return [good[:7], good[7:]]

    binary = linux_share.ensure_cloudflared(tmp_path, fetch=fetch)
    assert binary.read_bytes() == good
    assert linux_share.ensure_cloudflared(tmp_path, fetch=fetch) == binary
    assert fetched == [linux_share.CLOUDFLARED_URL], "downloaded once"


def test_the_link_is_announced_once_from_cloudflareds_log(tmp_path, monkeypatch):
    log = (
        "2026-11-04T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...\n"
        "2026-11-04T10:00:02Z INF |  https://quiet-river-blue-moon.trycloudflare.com  |\n"
        "2026-11-04T10:00:03Z INF Registered tunnel connection connIndex=0 protocol=http2\n"
        "2026-11-04T10:00:04Z INF |  https://quiet-river-blue-moon.trycloudflare.com  |\n"
    )

    class Process:
        stdout = io.StringIO(log)

        def wait(self, timeout=None):
            return 0

    started = []
    monkeypatch.setattr(
        linux_share.subprocess, "Popen", lambda args, **_kw: started.append(args) or Process()
    )
    out = io.StringIO()
    assert linux_share.run_tunnel(tmp_path / "cf", "http://127.0.0.1:8080", out=out) == 0
    assert started[0][-2:] == ["--url", "http://127.0.0.1:8080"], "Open WebUI only"
    assert out.getvalue().count("Send this link") == 1
    assert "    https://quiet-river-blue-moon.trycloudflare.com\n" in out.getvalue()
