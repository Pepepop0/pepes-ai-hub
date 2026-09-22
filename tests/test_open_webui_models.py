"""Keeping the chat model's record current inside a managed Open WebUI.

A fake Models API records every write. The sync must create the record once
with builtin tools off, leave a record that already has them off alone
(whoever wrote it), turn them off again only under its own record while
preserving the rest of that record, refuse somebody else's record, never
mistake an authentication failure for absence, and report a missing
administrator or an unreachable instance instead of raising.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from test_open_webui_filters import data_dir, factory_for

from local_ai_stack.open_webui_filters import FilterSync
from local_ai_stack.open_webui_models import (
    CAPABILITIES,
    OWNER,
    chat_model_record,
    configure_chat_model,
    sync_chat_model,
)

MODEL = "hermes3:8b"

USER_RECORD = {
    "id": MODEL,
    "base_model_id": None,
    "name": "My Hermes",
    "meta": {"description": "tuned by the user", "capabilities": {"vision": False}},
    "params": {"system": "You are terse."},
    "access_grants": [],
    "is_active": True,
}


class FakeModelsApi:
    """Open WebUI's /api/v1/models surface, as the pinned version answers."""

    def __init__(self, records: dict | None = None, *, authorized: bool = True) -> None:
        self.records = {
            key: json.loads(json.dumps(value)) for key, value in (records or {}).items()
        }
        self.writes: list[str] = []
        self.listings = 0
        self.authorized = authorized

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.authorized or request.headers.get("Authorization", "") == "":
            return httpx.Response(401, json={"detail": "Not authenticated"})
        path = request.url.path
        if request.method == "GET":
            if path == "/api/models":
                self.listings += 1
                return httpx.Response(200, json={"data": list(self.records.values())})
            model_id = request.url.params.get("id")
            if model_id not in self.records:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={**self.records[model_id], "write_access": True})
        self.writes.append(path)
        form = json.loads(request.content)
        if path.endswith("/create"):
            if form["id"] in self.records:
                return httpx.Response(401, json={"detail": "Model id taken"})
            self.records[form["id"]] = form
            return httpx.Response(200, json=form)
        if path.endswith("/model/update"):
            if form["id"] not in self.records:
                return httpx.Response(401, json={"detail": "not found"})
            self.records[form["id"]] = form
            return httpx.Response(200, json=form)
        pytest.fail(f"unexpected mutation {path}")

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://127.0.0.1:8080",
            headers={"Authorization": "Bearer test"},
            transport=httpx.MockTransport(self),
        )


def capabilities(api: FakeModelsApi) -> dict:
    return api.records[MODEL]["meta"]["capabilities"]


# --- configure -------------------------------------------------------------------


def test_a_fresh_instance_gets_a_record_with_builtin_tools_off():
    api = FakeModelsApi()
    with api.client() as client:
        assert configure_chat_model(client, MODEL) is FilterSync.INSTALLED
    record = api.records[MODEL]
    assert record == chat_model_record(MODEL)
    assert capabilities(api)["builtin_tools"] is False
    assert all(value is True for key, value in capabilities(api).items() if key != "builtin_tools")
    assert record["meta"]["managed_by"] == OWNER
    assert record["access_grants"] == [
        {"principal_type": "anyone", "principal_id": "*", "permission": "read"}
    ]
    assert api.writes == ["/api/v1/models/create"]
    assert api.listings == 1  # the running instance is told to reload its models


def test_a_profile_s_model_parameters_ride_on_the_record():
    """Declared parameters are written with the record, kept when already there,
    and put back when changed under our record; the record's other parameters stay."""
    api = FakeModelsApi()
    with api.client() as client:
        assert configure_chat_model(client, MODEL, {"think": False}) is FilterSync.INSTALLED
        assert api.records[MODEL]["params"] == {"think": False}
        assert capabilities(api)["builtin_tools"] is False
        assert configure_chat_model(client, MODEL, {"think": False}) is FilterSync.UNCHANGED
        # An administrator adds a parameter of their own and flips ours.
        api.records[MODEL]["params"] = {"think": True, "num_ctx": 8192}
        assert configure_chat_model(client, MODEL, {"think": False}) is FilterSync.UPDATED
        assert api.records[MODEL]["params"] == {"think": False, "num_ctx": 8192}
        # A profile that declares nothing leaves whatever parameters the record has.
        assert configure_chat_model(client, MODEL) is FilterSync.UNCHANGED
    assert api.writes == ["/api/v1/models/create", "/api/v1/models/model/update"]


def test_a_record_that_already_has_builtin_tools_off_is_left_alone_whoever_wrote_it():
    ours = FakeModelsApi({MODEL: chat_model_record(MODEL)})
    theirs = FakeModelsApi(
        {MODEL: {**USER_RECORD, "meta": {"capabilities": {"builtin_tools": False}}}}
    )
    for api in (ours, theirs):
        with api.client() as client:
            assert configure_chat_model(client, MODEL) is FilterSync.UNCHANGED
        assert api.writes == [] and api.listings == 0


def test_builtin_tools_turned_back_on_under_our_record_are_turned_off_again_in_place():
    record = chat_model_record(MODEL)
    record["meta"]["capabilities"]["builtin_tools"] = True
    record["meta"]["capabilities"]["vision"] = False
    record["params"] = {"temperature": 0.2}
    record["name"] = "Hermes (Pepe)"
    api = FakeModelsApi({MODEL: record})
    with api.client() as client:
        assert configure_chat_model(client, MODEL) is FilterSync.UPDATED
    assert api.writes == ["/api/v1/models/model/update"]
    updated = api.records[MODEL]
    assert updated["meta"]["capabilities"] == {**CAPABILITIES, "vision": False}
    assert updated["params"] == {"temperature": 0.2}
    assert updated["name"] == "Hermes (Pepe)"
    assert updated["meta"]["managed_by"] == OWNER


def test_somebody_elses_record_with_builtin_tools_on_is_refused_untouched():
    api = FakeModelsApi({MODEL: USER_RECORD})
    with api.client() as client, pytest.raises(PermissionError, match="not created by the Hub"):
        configure_chat_model(client, MODEL)
    assert api.writes == []
    assert api.records[MODEL] == USER_RECORD


def test_an_authentication_failure_is_never_mistaken_for_absence():
    api = FakeModelsApi(authorized=False)
    with api.client() as client, pytest.raises(httpx.HTTPStatusError):
        configure_chat_model(client, MODEL)
    assert api.writes == []


def test_a_record_the_instance_does_not_retain_is_an_error_not_a_success():
    class Forgetful(FakeModelsApi):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            response = super().__call__(request)
            if request.method == "POST":
                self.records[MODEL]["meta"]["capabilities"]["builtin_tools"] = True
            return response

    api = Forgetful()
    with api.client() as client, pytest.raises(RuntimeError, match="did not retain"):
        configure_chat_model(client, MODEL)


# --- sync: the product-facing outcome --------------------------------------------


def test_sync_installs_then_reports_unchanged(tmp_path: Path):
    api = FakeModelsApi()
    factory = factory_for(api)
    directory = data_dir(tmp_path)
    first = sync_chat_model(directory, "http://127.0.0.1:8080", MODEL, client_factory=factory)
    second = sync_chat_model(directory, "http://127.0.0.1:8080", MODEL, client_factory=factory)
    assert (first.outcome, second.outcome) == (FilterSync.INSTALLED, FilterSync.UNCHANGED)
    assert first.changed and not second.changed and first.ok and second.ok
    assert first.detail == f"{MODEL}: installed"
    assert factory.seen[0][1].count(".") == 2 and factory.seen[0][1] != factory.seen[1][1]


def test_sync_without_an_administrator_waits_instead_of_guessing(tmp_path: Path):
    api = FakeModelsApi()
    report = sync_chat_model(
        data_dir(tmp_path, admin=False), "http://x", MODEL, client_factory=factory_for(api)
    )
    assert report.outcome is FilterSync.NO_ADMIN and report.ok and not report.changed
    assert api.writes == []


def test_sync_reports_a_foreign_record_as_refused(tmp_path: Path):
    api = FakeModelsApi({MODEL: USER_RECORD})
    report = sync_chat_model(data_dir(tmp_path), "http://x", MODEL, client_factory=factory_for(api))
    assert report.outcome is FilterSync.REFUSED and not report.ok
    assert api.writes == []


def test_sync_reports_an_unreachable_instance_as_unavailable(tmp_path: Path):
    def refusing(url: str, bearer: str) -> httpx.Client:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return httpx.Client(base_url=url, transport=httpx.MockTransport(explode))

    report = sync_chat_model(data_dir(tmp_path), "http://x", MODEL, client_factory=refusing)
    assert report.outcome is FilterSync.UNAVAILABLE and not report.ok
    assert "ConnectError" in report.detail
