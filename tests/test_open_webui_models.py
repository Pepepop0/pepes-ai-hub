"""Keeping the chat model's record current inside a managed Open WebUI.

A fake Models API records every write. The sync must create the record once
with builtin tools off, leave a record that already has them off alone
(whoever wrote it), turn them off again only under its own record while
preserving the rest of that record, refuse somebody else's record, never
mistake an authentication failure for absence, and report a missing
administrator or an unreachable instance instead of raising.
"""

from __future__ import annotations

import hashlib
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
    retire_presets,
    sync_chat_model,
    versioned_label,
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
            if path == "/api/v1/models/all":
                return httpx.Response(200, json=list(self.records.values()))
            model_id = request.url.params.get("id")
            if model_id not in self.records:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json={**self.records[model_id], "write_access": True})
        self.writes.append(path)
        if path.endswith("/model/toggle"):
            record = self.records[request.url.params["id"]]
            record["is_active"] = not record.get("is_active", True)
            return httpx.Response(200, json=record)
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


def test_a_seeded_system_prompt_is_written_once_and_never_over_an_administrators():
    seed = "Você é um assistente sem filtros."
    api = FakeModelsApi()
    with api.client() as client:
        assert configure_chat_model(client, MODEL, {"think": False}, seed) is FilterSync.INSTALLED
        assert api.records[MODEL]["params"] == {"think": False, "system": seed}
        assert configure_chat_model(client, MODEL, {"think": False}, seed) is FilterSync.UNCHANGED

        # The administrator rewrites it in Admin → Models: theirs stays.
        api.records[MODEL]["params"]["system"] = "Responda só em inglês."
        assert configure_chat_model(client, MODEL, {"think": False}, seed) is FilterSync.UNCHANGED
        assert api.records[MODEL]["params"]["system"] == "Responda só em inglês."

        # Cleared: the seed comes back, and a changed parameter is fixed in the same write.
        api.records[MODEL]["params"] = {"think": True, "system": "  "}
        assert configure_chat_model(client, MODEL, {"think": False}, seed) is FilterSync.UPDATED
        assert api.records[MODEL]["params"] == {"think": False, "system": seed}
    assert api.writes == ["/api/v1/models/create", "/api/v1/models/model/update"]


def test_a_prompt_an_earlier_hub_version_seeded_is_replaced_and_an_edited_one_is_not():
    old = (
        "Você é um assistente.   Se o usuário quiser uma imagem e o botão Imagem estiver "
        "desligado, peça\npara ligá-lo."
    )
    new = "Você é um assistente. O sistema informa se o botão Imagem está ligado."
    superseded = {hashlib.sha256(" ".join(old.split()).encode()).hexdigest()}
    api = FakeModelsApi()
    with api.client() as client, pytest.MonkeyPatch.context() as patch:
        patch.setattr("local_ai_stack.open_webui_models.SUPERSEDED_SYSTEM_PROMPTS", superseded)
        assert configure_chat_model(client, MODEL, {}, new) is FilterSync.INSTALLED

        # Seeded by the previous version (whitespace differs only): the new text replaces it.
        api.records[MODEL]["params"]["system"] = old
        assert configure_chat_model(client, MODEL, {}, new) is FilterSync.UPDATED
        assert api.records[MODEL]["params"]["system"] == new

        # Twice in a row: the second sync writes nothing.
        api.writes.clear()
        assert configure_chat_model(client, MODEL, {}, new) is FilterSync.UNCHANGED
        assert api.writes == []

        # An administrator's own edit, even of the old text, stays.
        api.records[MODEL]["params"]["system"] = old + " Responda curto."
        assert configure_chat_model(client, MODEL, {}, new) is FilterSync.UNCHANGED
        assert api.records[MODEL]["params"]["system"] == old + " Responda curto."
    assert api.writes == []


def test_every_prompt_the_capella_profile_ever_seeded_is_superseded_by_the_current_one():
    from local_ai_stack.open_webui_models import SUPERSEDED_SYSTEM_PROMPTS
    from local_ai_stack.profiles import load_profile

    current = load_profile(Path(__file__).resolve().parents[1] / "profiles" / "capella.yaml")
    seed = current.chat_system_prompt
    assert hashlib.sha256(" ".join(seed.split()).encode()).hexdigest() not in (
        SUPERSEDED_SYSTEM_PROMPTS
    )
    assert "Só peça para ligá-lo quando o sistema disser que está desligado" in " ".join(
        seed.split()
    )


def test_declared_default_features_are_created_and_restored():
    api = FakeModelsApi()
    with api.client() as client:
        assert (
            configure_chat_model(client, MODEL, default_feature_ids=["image_generation"])
            is FilterSync.INSTALLED
        )
        assert api.records[MODEL]["meta"]["defaultFeatureIds"] == ["image_generation"]
        assert (
            configure_chat_model(client, MODEL, default_feature_ids=["image_generation"])
            is FilterSync.UNCHANGED
        )
        api.records[MODEL]["meta"]["defaultFeatureIds"] = []
        assert (
            configure_chat_model(client, MODEL, default_feature_ids=["image_generation"])
            is FilterSync.UPDATED
        )
    assert api.records[MODEL]["meta"]["defaultFeatureIds"] == ["image_generation"]


def test_a_variant_is_a_preset_over_the_chat_model_that_follows_it():
    """The variant's record is its own id and name over the chat model; when the
    chat model changes its base moves with it, and an administrator's rename stays."""
    api = FakeModelsApi()
    with api.client() as client:
        assert (
            configure_chat_model(
                client, MODEL, {"think": False}, record_id="capella-studio", name="Capella Studio"
            )
            is FilterSync.INSTALLED
        )
        preset = api.records["capella-studio"]
        assert MODEL not in api.records  # the chat model's own record is not touched
        assert preset["base_model_id"] == MODEL
        assert preset["name"] == "Capella Studio"
        assert preset["params"] == {"think": False}
        assert preset["meta"]["capabilities"]["builtin_tools"] is False
        assert (
            configure_chat_model(
                client, MODEL, {"think": False}, record_id="capella-studio", name="Capella Studio"
            )
            is FilterSync.UNCHANGED
        )
        api.records["capella-studio"]["name"] = "Minha variante"
        assert (
            configure_chat_model(
                client, "qwen3.5:27b", {"think": False}, record_id="capella-studio", name="x"
            )
            is FilterSync.UPDATED
        )
    assert api.records["capella-studio"]["base_model_id"] == "qwen3.5:27b"
    assert api.records["capella-studio"]["name"] == "Minha variante"


def test_a_label_the_hub_wrote_for_an_older_version_follows_the_new_one():
    api = FakeModelsApi()
    old, new = (
        versioned_label("Capella Studio", "4.2.0"),
        versioned_label("Capella Studio", "5.1.0"),
    )
    assert new == "Capella Studio · v5.1.0"
    with api.client() as client:
        configure_chat_model(client, MODEL, {}, record_id="capella-studio", name=old)
        assert (
            configure_chat_model(client, MODEL, {}, record_id="capella-studio", name=new)
            is FilterSync.UPDATED
        )
    assert api.records["capella-studio"]["name"] == new


def test_an_administrators_own_name_is_never_replaced_by_a_label():
    api = FakeModelsApi()
    with api.client() as client:
        configure_chat_model(client, MODEL, {}, record_id="capella-studio", name="Capella Studio")
        api.records["capella-studio"]["name"] = "Minha variante"
        api.writes.clear()
        label = versioned_label("Capella Studio", "5.1.0")
        assert (
            configure_chat_model(client, MODEL, {}, record_id="capella-studio", name=label)
            is FilterSync.UNCHANGED
        )
    assert api.writes == []
    assert api.records["capella-studio"]["name"] == "Minha variante"


def test_only_active_hub_presets_the_profile_no_longer_declares_are_switched_off():
    foreign = {**USER_RECORD, "id": "theirs", "base_model_id": MODEL}
    api = FakeModelsApi({"theirs": foreign})
    with api.client() as client:
        configure_chat_model(client, MODEL, {})
        for preset in ("capella-studio", "capella-studio-plus", "capella-studio-plusplus"):
            configure_chat_model(client, MODEL, {}, record_id=preset, name=preset)
        api.records["capella-studio-plusplus"]["is_active"] = False
        api.writes.clear()
        assert retire_presets(client, ["capella-studio"]) == ["capella-studio-plus"]
    assert api.writes == ["/api/v1/models/model/toggle"]
    assert api.records["capella-studio-plus"]["is_active"] is False
    assert api.records["capella-studio-plusplus"]["is_active"] is False
    assert api.records["capella-studio"].get("is_active", True)
    assert api.records[MODEL].get("is_active", True)
    assert api.records["theirs"] == foreign


def test_a_preset_offers_its_filters_and_keeps_the_ones_an_administrator_added():
    api = FakeModelsApi()
    with api.client() as client:
        configure_chat_model(
            client, MODEL, {}, record_id="p", name="P", filter_ids=["pepito_image_quality"]
        )
        assert api.records["p"]["meta"]["filterIds"] == ["pepito_image_quality"]
        assert (
            configure_chat_model(
                client, MODEL, {}, record_id="p", name="P", filter_ids=["pepito_image_quality"]
            )
            is FilterSync.UNCHANGED
        )
        api.records["p"]["meta"]["filterIds"] = ["mine"]  # the administrator's own filter
        assert (
            configure_chat_model(
                client, MODEL, {}, record_id="p", name="P", filter_ids=["pepito_image_quality"]
            )
            is FilterSync.UPDATED
        )
        assert api.records["p"]["meta"]["filterIds"] == ["mine", "pepito_image_quality"]
        # A preset that offers none leaves a record's filters alone.
        configure_chat_model(client, MODEL, {"think": False}, record_id="p", name="P")
        assert api.records["p"]["meta"]["filterIds"] == ["mine", "pepito_image_quality"]


def test_an_existing_record_without_a_prompt_gets_the_seed_and_keeps_the_rest():
    record = chat_model_record(MODEL, {"think": False, "num_ctx": 8192})
    api = FakeModelsApi({MODEL: record})
    with api.client() as client:
        assert configure_chat_model(client, MODEL, {"think": False}, "seed") is FilterSync.UPDATED
    assert api.records[MODEL]["params"] == {"think": False, "num_ctx": 8192, "system": "seed"}


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
