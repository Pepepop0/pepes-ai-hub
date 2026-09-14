"""Keeping the image chat filter current inside a managed Open WebUI.

A fake Functions API records every write. The sync must install once, update
only when the shipped source differs, activate what is inactive, leave every
other function alone, refuse a foreign function under its id, and report a
missing administrator or an unreachable instance instead of raising.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from local_ai_stack.open_webui_filters import (
    FILTER_ID,
    OWNER,
    FilterSync,
    filter_source,
    first_admin_id,
    install_image_chat_filter,
    local_admin_token,
    sync_image_chat_filter,
)

USER_FUNCTION = {
    "id": "my_own_filter",
    "name": "Mine",
    "type": "filter",
    "content": "class Filter: pass",
    "meta": {"description": "written by the user"},
    "is_active": True,
    "is_global": False,
}


class FakeFunctionsApi:
    """Open WebUI's /api/v1/functions surface, as the pinned version answers."""

    def __init__(self, records: dict | None = None, *, authorized: bool = True) -> None:
        self.records = dict(records or {})
        self.writes: list[str] = []
        self.authorized = authorized

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.authorized or request.headers.get("Authorization", "") == "":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/list"):
                return httpx.Response(200, json=list(self.records.values()))
            function_id = path.rsplit("/id/", 1)[1]
            if function_id not in self.records:
                return httpx.Response(401)  # yes: the pinned version says 401 for a missing id
            return httpx.Response(200, json=self.records[function_id])
        self.writes.append(path)
        if path.endswith("/create"):
            form = json.loads(request.content)
            self.records[form["id"]] = {
                **form,
                "type": "filter",
                "is_active": False,
                "is_global": False,
            }
            return httpx.Response(200, json=self.records[form["id"]])
        function_id = path.split("/id/", 1)[1].split("/", 1)[0]
        record = self.records[function_id]
        if path.endswith("/update"):
            record.update(json.loads(request.content))
        elif path.endswith("/toggle/global"):
            record["is_global"] = not record["is_global"]
        elif path.endswith("/toggle"):
            record["is_active"] = not record["is_active"]
        else:
            pytest.fail(f"unexpected mutation {path}")
        return httpx.Response(200, json=record)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://127.0.0.1:8080",
            headers={"Authorization": "Bearer test"},
            transport=httpx.MockTransport(self),
        )


def installed(api: FakeFunctionsApi) -> dict:
    return api.records[FILTER_ID]


# --- install ---------------------------------------------------------------------


def test_a_fresh_instance_gets_the_filter_installed_and_activated():
    api = FakeFunctionsApi({USER_FUNCTION["id"]: dict(USER_FUNCTION)})
    with api.client() as client:
        assert install_image_chat_filter(client) is FilterSync.INSTALLED
    record = installed(api)
    assert record["content"] == filter_source()
    assert record["is_active"] and record["is_global"]
    assert record["meta"]["description"] == OWNER
    assert api.writes == [
        "/api/v1/functions/create",
        f"/api/v1/functions/id/{FILTER_ID}/toggle",
        f"/api/v1/functions/id/{FILTER_ID}/toggle/global",
    ]
    assert api.records[USER_FUNCTION["id"]] == USER_FUNCTION


def test_a_current_filter_is_left_alone():
    api = FakeFunctionsApi()
    with api.client() as client:
        install_image_chat_filter(client)
        api.writes.clear()
        assert install_image_chat_filter(client) is FilterSync.UNCHANGED
    assert api.writes == []


def test_a_stale_filter_is_updated_in_place():
    api = FakeFunctionsApi()
    with api.client() as client:
        install_image_chat_filter(client)
        installed(api)["content"] = "# an older shipped version\nclass Filter: pass\n"
        installed(api)["meta"]["description"] = "Pepito's AI Hub: native image chat compatibility"
        api.writes.clear()
        assert install_image_chat_filter(client) is FilterSync.UPDATED
    assert installed(api)["content"] == filter_source()
    assert installed(api)["meta"]["description"] == OWNER
    assert api.writes == [f"/api/v1/functions/id/{FILTER_ID}/update"]


def test_a_disabled_filter_is_re_enabled_without_rewriting_it():
    api = FakeFunctionsApi()
    with api.client() as client:
        install_image_chat_filter(client)
        installed(api)["is_active"] = False
        api.writes.clear()
        assert install_image_chat_filter(client) is FilterSync.UPDATED
    assert api.writes == [f"/api/v1/functions/id/{FILTER_ID}/toggle"]


def test_a_foreign_function_under_our_id_is_refused_untouched():
    foreign = {**USER_FUNCTION, "id": FILTER_ID, "meta": {"description": "written by the user"}}
    api = FakeFunctionsApi({FILTER_ID: foreign})
    with api.client() as client, pytest.raises(PermissionError, match="not installed by the Hub"):
        install_image_chat_filter(client)
    assert api.writes == []
    assert api.records[FILTER_ID] == foreign


def test_an_authentication_failure_is_never_mistaken_for_absence():
    api = FakeFunctionsApi(authorized=False)
    with api.client() as client, pytest.raises(httpx.HTTPStatusError):
        install_image_chat_filter(client)
    assert api.writes == []


# --- the local administrator token ---------------------------------------------


def data_dir(tmp_path: Path, *, admin: bool = True, secret: str | None = "s3cret") -> Path:
    directory = tmp_path / "open-webui"
    directory.mkdir(parents=True)
    if secret is not None:
        (directory / ".webui_secret_key").write_text(secret, encoding="utf-8")
    connection = sqlite3.connect(directory / "webui.db")
    connection.execute("create table user (id text, role text, created_at integer)")
    if admin:
        connection.execute("insert into user values ('user-2', 'user', 1)")
        connection.execute("insert into user values ('admin-1', 'admin', 2)")
        connection.execute("insert into user values ('admin-9', 'admin', 3)")
    connection.commit()
    connection.close()
    return directory


def test_the_first_administrator_is_read_from_the_instance_database(tmp_path):
    assert first_admin_id(data_dir(tmp_path)) == "admin-1"
    assert first_admin_id(data_dir(tmp_path / "empty", admin=False)) is None
    assert first_admin_id(tmp_path / "nowhere") is None


def test_the_token_is_signed_with_the_instance_secret_for_the_first_admin(tmp_path):
    token = local_admin_token(data_dir(tmp_path), ttl=60)
    assert token is not None
    header, payload, signature = token.split(".")

    def unpad(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(unpad(header)) == {"alg": "HS256", "typ": "JWT"}
    claims = json.loads(unpad(payload))
    assert claims["id"] == "admin-1" and claims["exp"] - claims["iat"] == 60 and claims["jti"]
    expected = hmac.new(b"s3cret", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert unpad(signature) == expected


def test_no_secret_or_no_admin_means_no_token(tmp_path):
    assert local_admin_token(data_dir(tmp_path, secret=None)) is None
    assert local_admin_token(data_dir(tmp_path / "b", admin=False)) is None
    assert local_admin_token(data_dir(tmp_path / "c", secret="  ")) is None


# --- sync: the product-facing outcome --------------------------------------------


def factory_for(api: FakeFunctionsApi):
    seen = []

    def factory(url: str, bearer: str) -> httpx.Client:
        seen.append((url, bearer))
        return httpx.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {bearer}"},
            transport=httpx.MockTransport(api),
        )

    factory.seen = seen  # type: ignore[attr-defined]
    return factory


def test_sync_installs_then_reports_unchanged(tmp_path):
    api = FakeFunctionsApi()
    factory = factory_for(api)
    directory = data_dir(tmp_path)
    first = sync_image_chat_filter(directory, "http://127.0.0.1:8080", client_factory=factory)
    second = sync_image_chat_filter(directory, "http://127.0.0.1:8080", client_factory=factory)
    assert (first.outcome, second.outcome) == (FilterSync.INSTALLED, FilterSync.UNCHANGED)
    assert first.changed and not second.changed and first.ok and second.ok
    assert factory.seen[0][0] == "http://127.0.0.1:8080"
    assert factory.seen[0][1].count(".") == 2  # a JWT, minted per call
    assert factory.seen[0][1] != factory.seen[1][1]


def test_sync_updates_a_stale_filter(tmp_path):
    api = FakeFunctionsApi(
        {
            FILTER_ID: {
                "id": FILTER_ID,
                "name": "Pepito Image Chat",
                "type": "filter",
                "content": "# 727482a\nclass Filter: pass\n",
                "meta": {"description": "Pepito's AI Hub: native image chat compatibility"},
                "is_active": True,
                "is_global": True,
            }
        }
    )
    report = sync_image_chat_filter(data_dir(tmp_path), "http://x", client_factory=factory_for(api))
    assert report.outcome is FilterSync.UPDATED and report.changed
    assert installed(api)["content"] == filter_source()


def test_sync_without_an_administrator_waits_instead_of_guessing(tmp_path):
    api = FakeFunctionsApi()
    report = sync_image_chat_filter(
        data_dir(tmp_path, admin=False), "http://x", client_factory=factory_for(api)
    )
    assert report.outcome is FilterSync.NO_ADMIN and report.ok and not report.changed
    assert api.writes == []


def test_sync_reports_a_foreign_function_as_refused(tmp_path):
    api = FakeFunctionsApi({FILTER_ID: {**USER_FUNCTION, "id": FILTER_ID}})
    report = sync_image_chat_filter(data_dir(tmp_path), "http://x", client_factory=factory_for(api))
    assert report.outcome is FilterSync.REFUSED and not report.ok
    assert api.writes == []


def test_sync_reports_an_unreachable_instance_as_unavailable(tmp_path):
    def refusing(url: str, bearer: str) -> httpx.Client:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return httpx.Client(base_url=url, transport=httpx.MockTransport(explode))

    report = sync_image_chat_filter(data_dir(tmp_path), "http://x", client_factory=refusing)
    assert report.outcome is FilterSync.UNAVAILABLE and not report.ok
    assert "ConnectError" in report.detail
