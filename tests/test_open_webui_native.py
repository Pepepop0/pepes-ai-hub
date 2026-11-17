"""Native gallery/mask ownership and managed repair, without loading any model."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import marker_path, write_marker
from local_ai_stack.open_webui import OpenWebUIProvisioner
from local_ai_stack.open_webui_native import (
    NativeExtensionError,
    install_native_extension,
    sync_native_extension,
)
from local_ai_stack.provisioning import ProvisionContext, ProvisionOutcome
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

ASSET = Path(__file__).resolve().parents[1] / "assets" / "open-webui" / "pepito_native.py"
ALICE = {"Authorization": "Bearer alice"}
BOB = {"Authorization": "Bearer bob"}


def png(size=(4, 3), color="white", mode="RGB"):
    stream = io.BytesIO()
    Image.new(mode, size, color).save(stream, format="PNG")
    return stream.getvalue()


def uri(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


@pytest.fixture
def native(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("sqlalchemy")
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
    from sqlalchemy import Column, String, create_engine
    from sqlalchemy.orm import Session, declarative_base
    from sqlalchemy.pool import StaticPool

    # Only upstream adapters are replaced. FastAPI validation/routing, Pillow,
    # relational membership queries and the durable native store remain real.
    base = declarative_base()

    class ChatFile(base):
        __tablename__ = "chat_file"
        id = Column(String, primary_key=True)
        chat_id = Column(String)
        user_id = Column(String)
        file_id = Column(String)
        message_id = Column(String)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base.metadata.create_all(engine)
    owners = {"chat-a": "alice", "chat-b": "bob", "chat-a2": "alice"}
    files = {}
    for file_id, owner in (("alice-file", "alice"), ("alice-extra", "alice"), ("bob-file", "bob")):
        path = tmp_path / (file_id + ".png")
        path.write_bytes(png())
        files[file_id] = SimpleNamespace(id=file_id, user_id=owner, path=str(path))

    class Chats:
        @staticmethod
        async def get_chat_by_id_and_user_id(chat_id, user_id):
            return (
                SimpleNamespace(user_id=user_id, chat={})
                if owners.get(chat_id) == user_id
                else None
            )

        @staticmethod
        async def insert_chat_files(chat_id, message_id, file_ids, user_id):
            with Session(engine) as session:
                for file_id in file_ids:
                    exists = (
                        session.query(ChatFile).filter_by(chat_id=chat_id, file_id=file_id).first()
                    )
                    if exists is None:
                        session.add(
                            ChatFile(
                                id=f"{chat_id}:{file_id}",
                                chat_id=chat_id,
                                user_id=user_id,
                                file_id=file_id,
                                message_id=message_id,
                            )
                        )
                session.commit()

    class Files:
        @staticmethod
        async def get_file_by_id_and_user_id(file_id, user_id):
            file = files.get(file_id)
            return file if file is not None and file.user_id == user_id else None

    @asynccontextmanager
    async def db_context():
        with Session(engine) as session:

            class AsyncSession:
                async def execute(self, statement):
                    return session.execute(statement)

            yield AsyncSession()

    async def verified(request):
        value = request.headers.get("authorization", "").removeprefix("Bearer ")
        if value not in ("alice", "bob"):
            raise HTTPException(401, "Authentication required")
        return SimpleNamespace(id=value, role="user")

    verified.__annotations__["request"] = Request

    def module(name, **attributes):
        item = ModuleType(name)
        item.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, item)

    for name in (
        "open_webui",
        "open_webui.models",
        "open_webui.storage",
        "open_webui.utils",
        "open_webui.internal",
    ):
        module(name)
    module("open_webui.env", DATA_DIR=tmp_path / "data")
    module("open_webui.models.chats", Chats=Chats, ChatFile=ChatFile)
    module("open_webui.models.files", Files=Files)
    module("open_webui.storage.provider", Storage=SimpleNamespace(get_file=lambda path: path))
    module("open_webui.utils.auth", get_verified_user=verified)
    module("open_webui.internal.db", get_async_db_context=db_context)
    # The pure calibration module is installed beside native; load it as its sibling.
    calibration_spec = importlib.util.spec_from_file_location(
        "open_webui.pepito_calibration", ASSET.with_name("pepito_calibration.py")
    )
    calibration = importlib.util.module_from_spec(calibration_spec)
    monkeypatch.setitem(sys.modules, calibration_spec.name, calibration)
    calibration_spec.loader.exec_module(calibration)
    spec = importlib.util.spec_from_file_location("pepito_native_under_test", ASSET)
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, loaded)
    spec.loader.exec_module(loaded)
    app = FastAPI()
    app.include_router(loaded.router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            module=loaded,
            client=client,
            files=files,
            engine=engine,
            ChatFile=ChatFile,
            owners=owners,
            Session=Session,
            calibration=calibration,
            data_dir=tmp_path / "data",
            tmp_path=tmp_path,
        )
    engine.dispose()


def publish(
    native,
    *,
    chat_id="chat-a",
    user_id="alice",
    number=1,
    file_id="alice-file",
    attempt_id=None,
    origin=None,
):
    return asyncio.run(
        native.module.publish_gallery(
            chat_id,
            user_id,
            "assistant-message",
            [
                {
                    "number": number,
                    "url": f"/api/v1/files/{file_id}/content",
                    "description": "An image",
                    "variation": 1,
                    "total": 1,
                    "current": True,
                    "attempt_id": attempt_id,
                    "origin": origin,
                }
            ],
        )
    )


def facts(**overrides):
    value = {
        "family": "krea2",
        "workflow": "edit",
        "change": "strong",
        "strength": 1.0,
        "fidelity": "auto",
        "request_id": "request-1",
        "source_numbers": [1],
        "measurement": 12.5,
        "requested_count": 1,
        "status": "completed",
        "delivered": True,
        "output_number": 2,
    }
    value.update(overrides)
    return value


def register(native, attempt_id="attempt-1", *, chat_id="chat-a", user_id="alice", **overrides):
    native.calibration.register_attempt(
        native.data_dir, user_id, chat_id, attempt_id, facts(**overrides)
    )


def accept(native, number=2, verdict="accepted", headers=ALICE, chat_id="chat-a"):
    return native.client.post(
        "/api/v1/pepito/images/acceptance",
        headers=headers,
        json={"chat_id": chat_id, "number": number, "verdict": verdict},
    )


def arm(native, **overrides):
    payload = {
        "chat_id": "chat-a",
        "source_number": 1,
        "mask_png": uri(png()),
        "text": "paint the hat red",
    }
    payload.update(overrides)
    return native.client.post("/api/v1/pepito/images/draft", headers=ALICE, json=payload)


def test_gallery_requires_authentication_and_exact_user_chat_ownership(native):
    group = publish(native)
    path = f"/api/v1/pepito/images/gallery/{group}"
    assert native.client.get(path).status_code == 401
    assert native.client.get(path, headers=BOB).status_code == 404
    assert native.client.get(path, params={"chat_id": "chat-a2"}, headers=ALICE).status_code == 404
    response = native.client.get(path, headers=ALICE)
    assert response.status_code == 200
    assert response.json()["chat_id"] == "chat-a"
    assert response.json()["selected_number"] == 1
    assert response.json()["entries"][0]["url"] == "/api/v1/files/alice-file/content"


def test_publish_cannot_register_another_users_file_or_chat(native):
    with pytest.raises(native.module.HTTPException) as error:
        publish(native, file_id="bob-file")
    assert error.value.status_code == 404
    with pytest.raises(native.module.HTTPException) as error:
        publish(native, chat_id="chat-b")
    assert error.value.status_code == 404


def test_selection_and_original_source_recheck_file_membership(native):
    publish(native)
    publish(native, number=2, file_id="alice-extra")
    # Publishing displays the newest image but is not an explicit selection.
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) is None
    response = native.client.post(
        "/api/v1/pepito/images/selection", headers=ALICE, json={"chat_id": "chat-a", "number": 1}
    )
    assert response.json() == {"chat_id": "chat-a", "selected_number": 1}
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) == 1
    source = native.client.get(
        "/api/v1/pepito/images/source/1", params={"chat_id": "chat-a"}, headers=ALICE
    )
    assert source.status_code == 200
    assert source.content == png()
    assert source.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert (
        native.client.get(
            "/api/v1/pepito/images/source/1", params={"chat_id": "chat-a"}, headers=BOB
        ).status_code
        == 404
    )
    with native.Session(native.engine) as session:
        session.query(native.ChatFile).filter_by(chat_id="chat-a", file_id="alice-file").delete()
        session.commit()
    assert (
        native.client.get(
            "/api/v1/pepito/images/source/1", params={"chat_id": "chat-a"}, headers=ALICE
        ).status_code
        == 404
    )


def test_source_has_private_strong_validators_and_revalidates_only_for_the_owner(native):
    publish(native)
    path = "/api/v1/pepito/images/source/1"
    first = native.client.get(path, params={"chat_id": "chat-a"}, headers=ALICE)
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert etag == '"' + hashlib.sha256(png()).hexdigest() + '"'
    assert first.headers["cache-control"].startswith("private,")
    assert "public" not in first.headers["cache-control"]
    assert {item.strip() for item in first.headers["vary"].split(",")} >= {
        "Authorization",
        "Cookie",
    }

    second = native.client.get(
        path, params={"chat_id": "chat-a"}, headers={**ALICE, "If-None-Match": etag}
    )
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag
    assert second.headers["cache-control"].startswith("private,")
    # Weak comparison, lists and a stale validator behave per RFC 9110.
    listed = native.client.get(
        path,
        params={"chat_id": "chat-a"},
        headers={**ALICE, "If-None-Match": f'"stale", W/{etag}'},
    )
    assert listed.status_code == 304
    stale = native.client.get(
        path, params={"chat_id": "chat-a"}, headers={**ALICE, "If-None-Match": '"stale"'}
    )
    assert stale.status_code == 200 and stale.content == png()

    # A validator never bypasses ownership: another account still gets 404.
    for validator in (etag, "*"):
        foreign = native.client.get(
            path, params={"chat_id": "chat-a"}, headers={**BOB, "If-None-Match": validator}
        )
        assert foreign.status_code == 404
        assert "etag" not in foreign.headers
    assert native.client.get(path, params={"chat_id": "chat-a"}).status_code == 401

    # The client's content version must name the bound file.
    versioned = native.client.get(
        path, params={"chat_id": "chat-a", "v": "alice-file"}, headers=ALICE
    )
    assert versioned.status_code == 200
    assert (
        native.client.get(
            path, params={"chat_id": "chat-a", "v": "alice-extra"}, headers=ALICE
        ).status_code
        == 404
    )


def test_temporary_source_revalidates_instead_of_caching_long(native):
    publish(native, chat_id="local:session-1")
    path = "/api/v1/pepito/images/source/1"
    first = native.client.get(path, params={"chat_id": "local:session-1"}, headers=ALICE)
    assert first.headers["cache-control"] == "private, no-cache"
    again = native.client.get(
        path,
        params={"chat_id": "local:session-1"},
        headers={**ALICE, "If-None-Match": first.headers["etag"]},
    )
    assert again.status_code == 304
    assert (
        native.client.get(
            path,
            params={"chat_id": "local:session-1"},
            headers={**BOB, "If-None-Match": first.headers["etag"]},
        ).status_code
        == 404
    )


def test_unregistered_source_and_duplicate_numbers_are_rejected(native):
    publish(native)
    assert (
        native.client.post(
            "/api/v1/pepito/images/selection",
            headers=ALICE,
            json={"chat_id": "chat-a", "number": 99},
        ).status_code
        == 404
    )
    with pytest.raises(native.module.HTTPException) as error:
        publish(native, number=1, file_id="alice-extra")
    assert error.value.status_code == 409


@pytest.mark.parametrize("chat_id", ["temporary:session-1", "local:session-1"])
def test_temporary_gallery_is_user_bound_and_only_trusted_publish_establishes_it(native, chat_id):
    path = "/api/v1/pepito/images/selection"
    assert (
        native.client.post(path, headers=ALICE, json={"chat_id": chat_id, "number": 1}).status_code
        == 404
    )
    group = publish(native, chat_id=chat_id)
    gallery = f"/api/v1/pepito/images/gallery/{group}"
    assert native.client.get(gallery, headers=ALICE).json()["chat_id"] == chat_id
    assert native.client.get(gallery, headers=BOB).status_code == 404
    assert (
        native.client.get(
            "/api/v1/pepito/images/source/1", params={"chat_id": chat_id}, headers=ALICE
        ).status_code
        == 200
    )
    assert arm(native, chat_id=chat_id).status_code == 200
    assert (
        asyncio.run(native.module.consume_draft(chat_id, "alice", "paint the hat red"))["number"]
        == 1
    )


def consume(native, text="paint the hat red", chat_id="chat-a", user_id="alice"):
    return asyncio.run(native.module.consume_draft(chat_id, user_id, text))


def test_mask_consumption_is_text_bound_one_shot_and_tolerates_spacing(native):
    publish(native)
    assert arm(native).status_code == 200
    consumed = consume(native, "  paint the hat\nred ")
    assert consumed == {"number": 1, "png": png()}, "the client's spacing is not other words"
    assert consume(native) is None, "used once"


def test_a_draft_for_other_words_is_reported_once_then_never_blocks_again(native):
    """B6: a mismatch returned None, so the filter edited an automatic area instead of the
    painted one without a word. 12/11 (SPEC-302): a draft kept for its own words refused the
    next message sent without an area too; it is reported once and discarded."""
    publish(native)
    assert arm(native, text="A: paint the hat red").status_code == 200
    assert consume(native, "B: paint the shoes blue") == {
        "number": 1,
        "selected": 1,
        "issue": "text",
    }
    assert consume(native, "C: paint the shoes green") is None, "the next message goes ahead"
    assert consume(native, "A: paint the hat red") is None, "the stale area is gone"


def test_a_new_area_replaces_the_one_armed_before(native):
    publish(native)
    assert arm(native, text="paint the hat red").status_code == 200
    assert arm(native, text="paint the shoes blue").status_code == 200
    assert consume(native, "paint the shoes blue") == {"number": 1, "png": png()}
    assert consume(native, "paint the hat red") is None


# The 12/11 messages as the page sent them (webui.db): the page arms the draft with the whole
# text; the filter takes the ``['...']`` notes out before it consumes the draft.
REAL_AREA_TURNS = [
    "[\u2018melhoria considerável, mesmo ainda  faltando \u2018]Quase lá!, mas ainda faltou  essas partes",
    "[\u2018Que?\u2018] Aqui, remarquei",
    "Faltou despir essas partes",
]


@pytest.mark.parametrize("typed", REAL_AREA_TURNS)
def test_the_area_painted_on_12_11_is_used_with_or_without_ignored_notes(native, typed):
    from test_open_webui_image_filter import load_module

    clean, _ = load_module()["strip_ignored"](typed)
    publish(native)
    assert arm(native, text=typed).status_code == 200
    assert consume(native, clean) == {"number": 1, "png": png()}


def test_the_12_11_sequence_uses_each_area_and_never_refuses(native):
    """Each of the three messages was refused on 12/11; the last one by the area armed for the
    second. Here the first two carry an area and the third does not."""
    from test_open_webui_image_filter import load_module

    strip = load_module()["strip_ignored"]
    publish(native)
    for typed in REAL_AREA_TURNS[:2]:
        assert arm(native, text=typed).status_code == 200
        assert consume(native, strip(typed)[0]) == {"number": 1, "png": png()}
    assert consume(native, strip(REAL_AREA_TURNS[2])[0]) is None


def test_stale_draft_expires_and_cannot_cross_users_or_chats(native, monkeypatch):
    publish(native)
    now = native.module.time.time()
    assert arm(native).status_code == 200
    with pytest.raises(native.module.HTTPException):
        consume(native, user_id="bob")
    assert consume(native, chat_id="chat-a2") is None
    monkeypatch.setattr(native.module.time, "time", lambda: now + 301)
    assert consume(native) == {"number": 1, "selected": 1, "issue": "expired"}
    assert consume(native) is None, "an expired draft is reported once, then gone"


def test_mask_is_consumed_once_under_concurrent_requests(native):
    publish(native)
    assert arm(native).status_code == 200

    async def consume_twice():
        return await asyncio.gather(
            *(native.module.consume_draft("chat-a", "alice", "paint the hat red") for _ in range(2))
        )

    results = asyncio.run(consume_twice())
    assert sum(item is not None for item in results) == 1


@pytest.mark.parametrize(
    "mask",
    [
        "data:image/jpeg;base64,aGVsbG8=",
        "data:image/png;base64,!!!!",
        uri(b"not a png"),
        uri(png(size=(3, 3))),
        uri(png(color="black")),
        uri(png(color="red")),
        uri(png(mode="RGBA", color=(255, 255, 255, 0))),
    ],
)
def test_invalid_masks_cannot_arm(native, mask):
    publish(native)
    assert arm(native, mask_png=mask).status_code == 400
    assert asyncio.run(native.module.consume_draft("chat-a", "alice", "paint the hat red")) is None


def test_mask_dimensions_pixel_bounds_and_source_selection_are_enforced(native, monkeypatch):
    publish(native)
    monkeypatch.setattr(native.module, "MAX_IMAGE_PIXELS", 11)
    assert arm(native).status_code == 400
    monkeypatch.setattr(native.module, "MAX_IMAGE_PIXELS", 16 * 1024 * 1024)
    publish(native, number=2, file_id="alice-extra")
    assert arm(native).status_code == 409
    assert arm(native, source_number=2).status_code == 200
    asyncio.run(native.module.select_image("chat-a", "alice", 1))
    # No uncaught 409 at consumption: the issue is reported and the draft kept for picture 2.
    assert consume(native) == {"number": 2, "selected": 1, "issue": "source"}
    asyncio.run(native.module.select_image("chat-a", "alice", 2))
    assert consume(native)["number"] == 2


def test_a_draft_whose_picture_no_longer_matches_is_reported_once_and_removed(native, tmp_path):
    publish(native)
    assert arm(native).status_code == 200
    (tmp_path / "alice-file.png").write_bytes(png(size=(5, 5)))
    assert consume(native) == {"number": 1, "selected": 1, "issue": "invalid"}
    assert consume(native) is None


def test_mask_payload_is_bounded_before_json_parsing(native, monkeypatch):
    publish(native)
    monkeypatch.setattr(native.module, "MAX_MASK_BYTES", 10)
    assert arm(native).status_code == 413
    response = native.client.post(
        "/api/v1/pepito/images/draft",
        headers={**ALICE, "Content-Length": "999999999"},
        content=b"{}",
    )
    assert response.status_code == 413


def test_conditional_cancellation_preserves_newer_draft(native):
    publish(native)
    first = arm(native).json()["draft_id"]
    second = arm(native).json()["draft_id"]
    path = "/api/v1/pepito/images/draft"
    assert native.client.delete(
        path, params={"chat_id": "chat-a", "draft_id": first}, headers=ALICE
    ).json() == {"deleted": False}
    assert (
        native.client.delete(
            path, params={"chat_id": "chat-a", "draft_id": second}, headers=BOB
        ).status_code
        == 404
    )
    assert native.client.delete(
        path, params={"chat_id": "chat-a", "draft_id": second}, headers=ALICE
    ).json() == {"deleted": True}
    assert asyncio.run(native.module.consume_draft("chat-a", "alice", "paint the hat red")) is None


def test_default_explicit_and_accepted_numbers_stay_separate(native):
    register(native)
    group = publish(native)
    publish(native, number=2, file_id="alice-extra", attempt_id="attempt-1")
    path = f"/api/v1/pepito/images/gallery/{group}"

    def numbers():
        body = native.client.get(path, headers=ALICE).json()
        return tuple(
            body[key]
            for key in (
                "explicit_selected_number",
                "accepted_number",
                "current_number",
                "selected_number",
                "selection_source",
            )
        )

    assert numbers() == (None, None, 2, 2, "current")
    assert accept(native).json()["accepted_number"] == 2
    # Accepting 2 neither selects it nor makes it the default, and selecting never accepts.
    assert numbers() == (None, 2, 2, 2, "current")
    asyncio.run(native.module.select_image("chat-a", "alice", 1))
    assert numbers() == (1, 2, 2, 1, "explicit")
    assert native.client.get(
        "/api/v1/pepito/images/state", params={"chat_id": "chat-a"}, headers=ALICE
    ).json() == {
        "chat_id": "chat-a",
        "explicit_selected_number": 1,
        "accepted_number": 2,
        "current_number": 2,
        "selected_number": 1,
        "selection_source": "explicit",
    }
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 1
    # The next delivered batch consumes the explicit pick, making cumulative edits
    # continue from their new output rather than freezing on the old selected base.
    publish(native, number=3, file_id="alice-file", origin="generated")
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) is None
    assert numbers() == (None, 2, 3, 3, "current")


def test_new_upload_supersedes_an_older_explicit_selection_only(native):
    publish(native)
    asyncio.run(native.module.select_image("chat-a", "alice", 1))
    publish(native, number=2, file_id="alice-extra", origin="upload")
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) is None
    state = asyncio.run(native.module.read_state("chat-a", "alice"))
    assert (state["current_number"], state["selection_source"]) == (2, "current")


def test_previous_publish_selection_migrates_once_without_losing_deliberate_picks(native):
    publish(native)
    publish(native, number=2, file_id="alice-extra")

    def legacy(selection):
        with native.module._transaction("chat-a", "alice") as connection:
            connection.execute("DELETE FROM state WHERE key IN ('selection-migrated','current')")
            native.module._put_state(connection, "selection", selection)

    legacy(2)  # an old automatic default equals the newest image
    state = asyncio.run(native.module.read_state("chat-a", "alice"))
    assert (state["explicit_selected_number"], state["current_number"]) == (None, 2)
    legacy(1)  # a deliberate older pick
    state = asyncio.run(native.module.read_state("chat-a", "alice"))
    assert (state["explicit_selected_number"], state["current_number"]) == (1, 2)
    # Migration is marked: a later explicit selection of the newest image is kept.
    asyncio.run(native.module.select_image("chat-a", "alice", 2))
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) == 2


def test_acceptance_learns_once_and_is_bound_to_owner_chat_and_output(native):
    register(native)
    publish(native)
    publish(native, number=2, file_id="alice-extra", attempt_id="attempt-1")
    assert (
        native.client.post(
            "/api/v1/pepito/images/acceptance",
            json={"chat_id": "chat-a", "number": 2, "verdict": "accepted"},
        ).status_code
        == 401
    )
    assert accept(native, headers=BOB).status_code == 404
    first = accept(native).json()["feedback"]
    second = accept(native).json()["feedback"]
    assert (first["recorded"], first["learned"]) == (True, True)
    assert (second["recorded"], second["learned"]) == (False, True)
    state = native.calibration.calibration_state(native.data_dir, "alice")
    assert state["accepted_samples"] == 1 and state["scopes"][0]["accepted"] == 1
    bias = native.calibration.calibration_bias(native.data_dir, "alice", "krea2", "edit", "strong")
    assert -0.5 <= bias < 0 and bias == pytest.approx(-0.5 * 0.35, abs=0.01)
    # Another account has learned nothing from it.
    assert (
        native.calibration.calibration_bias(native.data_dir, "bob", "krea2", "edit", "strong") == 0
    )


def test_acceptance_rejects_unregistered_foreign_or_mismatched_attempts(native):
    publish(native)  # no attempt id
    publish(native, number=2, file_id="alice-extra", attempt_id="foreign-chat")
    publish(native, number=3, file_id="alice-file", attempt_id="other-user")
    publish(native, number=4, file_id="alice-extra", attempt_id="wrong-output")
    publish(native, number=5, file_id="alice-file", attempt_id="never-registered")
    register(native, "foreign-chat", chat_id="chat-a2", output_number=2)
    register(native, "other-user", user_id="bob", chat_id="chat-b", output_number=3)
    register(native, "wrong-output", output_number=99)
    for number in range(1, 6):
        assert accept(native, number).status_code == 409, number
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 0
    assert native.calibration.calibration_state(native.data_dir, "bob")["accepted_samples"] == 0
    state = asyncio.run(native.module.read_state("chat-a", "alice"))
    assert state["accepted_number"] is None
    assert accept(native, 77).status_code == 404
    assert accept(native, 2, "great").status_code == 422


def test_attempt_cannot_be_rebound_and_a_republish_keeps_its_binding(native):
    register(native)
    publish(native, number=2, file_id="alice-extra", attempt_id="attempt-1")
    publish(native, number=2, file_id="alice-extra")  # a republish without the id keeps it
    assert accept(native).status_code == 200
    with pytest.raises(native.module.HTTPException) as error:
        publish(native, number=2, file_id="alice-extra", attempt_id="another")
    assert error.value.status_code == 409


def test_negative_feedback_is_evidence_and_never_claims_a_retry_worked(native):
    register(native, "first")
    register(native, "retry", output_number=3, request_id="request-2", strength=1.0)
    publish(native)
    publish(native, number=2, file_id="alice-extra", attempt_id="first")
    publish(native, number=3, file_id="alice-file", attempt_id="retry")
    rejected = accept(native, 2, "no_change").json()
    assert rejected["feedback"]["learned"] is False
    assert rejected["accepted_number"] is None
    # Accepting the rejected attempt later is still not a confirmed success.
    late = accept(native, 2).json()
    assert late["feedback"] == {
        "attempt_id": "first",
        "verdict": "accepted",
        "recorded": True,
        "learned": False,
    }
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 0
    # Only the retry's own acceptance teaches, and a later rejection clears the display.
    assert accept(native, 3).json()["feedback"]["learned"] is True
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 1
    assert accept(native, 3, "wrong_source").json()["accepted_number"] is None


def test_acceptance_needs_installed_calibration(native, monkeypatch):
    publish(native, attempt_id="attempt-1")
    monkeypatch.delitem(sys.modules, "open_webui.pepito_calibration")
    assert accept(native, 1).status_code == 503


def test_calibration_state_and_reset_are_account_scoped_endpoints(native):
    register(native)
    register(native, "bobs", user_id="bob", chat_id="chat-b", output_number=1)
    publish(native, number=2, file_id="alice-extra", attempt_id="attempt-1")
    assert accept(native).status_code == 200
    path = "/api/v1/pepito/images/calibration"
    assert native.client.get(path).status_code == 401
    assert native.client.get(path, headers=ALICE).json()["accepted_samples"] == 1
    assert native.client.post(path + "/reset", headers=ALICE).json() == {"reset": True}
    assert native.client.get(path, headers=ALICE).json()["scopes"] == []
    assert native.calibration.attempt_state(native.data_dir, "bob", "chat-b", "bobs") is not None


def write_stacks(native, monkeypatch, stacks):
    path = native.tmp_path / "stacks.json"
    path.write_text(json.dumps(stacks), encoding="utf-8")
    monkeypatch.setenv("PEPITO_IMAGE_STACKS", str(path))
    return path


def preferences(native, method="get", headers=ALICE, **payload):
    url = "/api/v1/pepito/images/preferences"
    if method == "get":
        return native.client.get(url, params={"chat_id": "chat-a"}, headers=headers)
    return native.client.post(url, headers=headers, json={"chat_id": "chat-a", **payload})


def test_preferences_offer_only_configured_editors_and_never_route_silently(native, monkeypatch):
    monkeypatch.delenv("PEPITO_IMAGE_STACKS", raising=False)
    publish(native)
    assert preferences(native).json() == {
        "chat_id": "chat-a",
        "auto_region": True,
        "editor_model_id": None,
        "edit_method": "auto",
        "editor_available": True,
        "comparison_seed": None,
        "editors": [],
    }
    path = write_stacks(
        native,
        monkeypatch,
        {
            "krea": {"generate": {"workflow": "{}"}, "edit": {"workflow": "{}"}, "family": "krea2"},
            "qwen": {"edit": {"workflow": "{}"}, "label": "Qwen edit"},
            "generate-only": {"generate": {"workflow": "{}"}},
            "empty-edit": {"edit": {}},
        },
    )
    editors = preferences(native).json()["editors"]
    assert editors == [
        {"model_id": "krea", "label": "krea", "family": "krea2"},
        {"model_id": "qwen", "label": "Qwen edit"},
    ]
    assert preferences(native, "post", editor_model_id="generate-only").status_code == 400
    assert preferences(native, "post", editor_model_id="unlisted").status_code == 400
    assert preferences(native).json()["editor_model_id"] is None
    chosen = preferences(native, "post", editor_model_id="qwen").json()
    assert chosen["editor_model_id"] == "qwen" and chosen["editor_available"] is True
    # Fields that are not sent stay; the Hub dropping an editor is reported, not re-routed.
    path.write_text(json.dumps({"krea": {"edit": {"workflow": "{}"}}}), encoding="utf-8")
    stale = preferences(native, "post", auto_region=False).json()
    assert (stale["auto_region"], stale["editor_model_id"], stale["editor_available"]) == (
        False,
        "qwen",
        False,
    )
    assert preferences(native, "post", editor_model_id=None).json()["editor_model_id"] is None


def test_comparison_seed_is_strict_persistent_and_nullable(native):
    publish(native)
    for bad in (-1, 2**63, 1.5, "7", True):
        assert preferences(native, "post", comparison_seed=bad).status_code == 422, bad
    assert (
        preferences(native, "post", comparison_seed=2**63 - 1).json()["comparison_seed"]
        == 2**63 - 1
    )
    assert preferences(native, "post", auto_region=False).json()["comparison_seed"] == 2**63 - 1
    assert preferences(native).json()["comparison_seed"] == 2**63 - 1
    assert preferences(native, "post", comparison_seed=None).json()["comparison_seed"] is None
    assert preferences(native, "post", comparison_seed=0).json()["comparison_seed"] == 0
    assert preferences(native, "post", auto_region="false").status_code == 422
    assert preferences(native, "post", unknown=1).status_code == 422


@pytest.mark.parametrize("method", ["auto", "qwen", "warp", "warp_qwen"])
def test_edit_method_roundtrips_and_preserves_partial_preferences(native, monkeypatch, method):
    publish(native)
    write_stacks(native, monkeypatch, {"editor": {"edit": {"workflow": "{}"}}})
    initial = preferences(
        native, "post", auto_region=False, editor_model_id="editor", comparison_seed=73
    ).json()
    response = preferences(native, "post", edit_method=method)
    assert response.status_code == 200
    assert response.json() == {**initial, "edit_method": method}
    assert preferences(native).json() == response.json()
    partial = preferences(native, "post", comparison_seed=91).json()
    assert partial == {**response.json(), "comparison_seed": 91}
    assert preferences(native, "post").json() == partial


@pytest.mark.parametrize("bad", ["", "AUTO", "unknown", None, True, 1, [], {}])
def test_edit_method_rejects_invalid_values_without_mutating_preferences(native, bad):
    publish(native)
    saved = preferences(native, "post", edit_method="warp_qwen", comparison_seed=73).json()
    response = preferences(native, "post", edit_method=bad, auto_region=False)
    assert response.status_code == 422
    assert preferences(native).json() == saved


def test_edit_method_preferences_are_owner_only(native):
    publish(native)
    saved = preferences(native, "post", edit_method="qwen").json()
    assert preferences(native, headers=BOB).status_code == 404
    assert preferences(native, "post", headers=BOB, edit_method="warp").status_code == 404
    assert preferences(native, headers={}).status_code == 401
    assert preferences(native, "post", headers={}, edit_method="warp").status_code == 401
    assert preferences(native).json() == saved


def test_preferences_require_authentication_and_chat_ownership(native):
    publish(native)
    assert preferences(native, headers={}).status_code == 401
    assert preferences(native, headers=BOB).status_code == 404
    assert preferences(native, "post", headers=BOB, auto_region=False).status_code == 404
    assert preferences(native, "post", headers={}, auto_region=False).status_code == 401
    assert preferences(native).json()["auto_region"] is True
    unknown = native.client.get(
        "/api/v1/pepito/images/preferences", params={"chat_id": "temporary:nope"}, headers=ALICE
    )
    # A brand-new temporary chat may read defaults before its first image, never create state.
    assert unknown.status_code == 200 and unknown.json()["editor_model_id"] is None
    assert unknown.json()["edit_method"] == "auto"
    assert (
        preferences(native, "post", chat_id="temporary:nope", auto_region=False).status_code == 404
    )


def managed_package(root):
    package = root / "Lib" / "site-packages" / "open_webui"
    (package / "frontend").mkdir(parents=True)
    # A runnable minimal upstream-shaped application, not a source-text pin test.
    (package / "main.py").write_text(
        "from fastapi import FastAPI, APIRouter\nfrom types import SimpleNamespace\napp = FastAPI()\nimages = SimpleNamespace(router=APIRouter())\napp.include_router(images.router, prefix='/api/v1/images', tags=['images'])\n",
        encoding="utf-8",
    )
    (package / "frontend" / "index.html").write_text(
        "<!doctype html><html><head><title>Open WebUI</title></head><body>Chat</body></html>",
        encoding="utf-8",
    )
    return package


@pytest.fixture
def bundled_assets(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "pepito_native.py").write_text(
        "from fastapi import APIRouter\nrouter=APIRouter()\n@router.get('/api/v1/pepito/images/test')\ndef test_route(): return {'native': True}\n",
        encoding="utf-8",
    )
    (assets / "pepito_calibration.py").write_text("# runtime ledger\n", encoding="utf-8")
    (assets / "pepito_image_preservation.py").write_text("# pixel preservation\n", encoding="utf-8")
    (assets / "pepito_edit_methods.py").write_text("# edit methods\n", encoding="utf-8")
    (assets / "pepito_swap.py").write_text("# swap prompts\n", encoding="utf-8")
    (assets / "pepito_swap_compose.py").write_text("# swap compositor\n", encoding="utf-8")
    (assets / "pepito_image_ui.js").write_text("window.pepitoNative = true;", encoding="utf-8")
    (assets / "pepito_image_ui.css").write_text(
        ".pepito-gallery { display: grid; }", encoding="utf-8"
    )
    return assets


def test_native_install_is_executable_idempotent_and_synchronizes_assets(
    tmp_path, bundled_assets, monkeypatch
):
    pytest.importorskip("fastapi")
    from fastapi.staticfiles import StaticFiles
    from fastapi.testclient import TestClient

    package = managed_package(tmp_path / "venv")
    install_native_extension(package, version="0.12.0", assets=bundled_assets)
    first_main = (package / "main.py").read_bytes()
    first_index = (package / "frontend" / "index.html").read_bytes()
    spec = importlib.util.spec_from_file_location(
        "open_webui.pepito_native", package / "pepito_native.py"
    )
    module = importlib.util.module_from_spec(spec)
    parent = ModuleType("open_webui")
    parent.__path__ = [str(package)]
    monkeypatch.setitem(sys.modules, "open_webui", parent)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    scope = {}
    exec(compile(first_main, str(package / "main.py"), "exec"), scope)  # noqa: S102
    scope["app"].mount("/", StaticFiles(directory=package / "frontend", html=True))
    with TestClient(scope["app"]) as client:
        assert client.get("/api/v1/pepito/images/test").json() == {"native": True}
        assert client.get("/").status_code == 200
        assert (
            client.get("/pepito-native/v1/pepito_image_ui.js").content
            == (bundled_assets / "pepito_image_ui.js").read_bytes()
        )
        assert (
            client.get("/pepito-native/v1/pepito_image_ui.css").content
            == (bundled_assets / "pepito_image_ui.css").read_bytes()
        )
    (bundled_assets / "pepito_image_ui.js").write_text(
        "window.pepitoNative = 'updated';", encoding="utf-8"
    )
    install_native_extension(package, version="0.12.0", assets=bundled_assets)
    assert (package / "main.py").read_bytes() == first_main
    assert (package / "frontend" / "index.html").read_bytes() == first_index
    assert (package / "frontend" / "pepito-native" / "v1" / "pepito_image_ui.js").read_bytes() == (
        bundled_assets / "pepito_image_ui.js"
    ).read_bytes()
    assert not list(package.rglob("*.tmp"))


def test_unsupported_pin_or_upstream_source_is_not_modified(tmp_path, bundled_assets):
    package = managed_package(tmp_path / "venv")
    before = {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}
    with pytest.raises(NativeExtensionError, match="requires Open WebUI"):
        install_native_extension(package, version="0.13.0", assets=bundled_assets)
    assert all(path.read_bytes() == content for path, content in before.items())
    (package / "main.py").write_text("app = None\n", encoding="utf-8")
    with pytest.raises(NativeExtensionError, match="does not match"):
        install_native_extension(package, version="0.12.0", assets=bundled_assets)
    assert not (package / "pepito_native.py").exists()


class NativeRunner(CommandRunner):
    def __init__(self, package, *, installed="0.12.0"):
        super().__init__()
        self.package = package
        self.installed = installed
        self.calls = []

    def which(self, name, extra_env=None):
        return None

    def run(self, argv, **kwargs):
        args = tuple(str(value) for value in argv)
        self.calls.append(args)
        if "-c" in args:
            code = args[args.index("-c") + 1]
            if "locate_file" in code:
                text = json.dumps({"version": self.installed, "package": str(self.package)})
            else:
                text = self.installed
            return CommandResult(argv=args, returncode=0, stdout=text)
        raise AssertionError(f"Repair must not invoke installation: {args}")


def repair_context(tmp_path, bundled_assets, monkeypatch):
    installation = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    environment = PythonEnvironment.inside(installation.layout.open_webui_app)
    package = managed_package(environment.root)
    environment.bin_dir.mkdir(parents=True)
    environment.python.write_text("python", encoding="utf-8")
    environment.script("open-webui").write_text("launcher", encoding="utf-8")
    write_marker(
        installation.layout.open_webui_app,
        component="open-webui",
        source_id="open-webui",
        version="0.12.0",
    )
    resource_assets = tmp_path / "resources" / "assets" / "open-webui"
    resource_assets.mkdir(parents=True)
    for path in bundled_assets.iterdir():
        (resource_assets / path.name).write_bytes(path.read_bytes())
    monkeypatch.setenv("PEPITOS_AI_HUB_RESOURCES", str(tmp_path / "resources"))
    return (
        ProvisionContext(installation=installation, runner=NativeRunner(package)),
        package,
        resource_assets,
    )


def test_repair_at_exact_pin_still_synchronizes_native_extension(
    tmp_path, bundled_assets, monkeypatch
):
    context, package, assets = repair_context(tmp_path, bundled_assets, monkeypatch)
    provisioner = OpenWebUIProvisioner(version="0.12.0")
    assert provisioner.plan(context).is_noop, "the pinned package needs no install step"
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED
    (assets / "pepito_image_ui.js").write_text("window.updated = true;", encoding="utf-8")
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED
    assert provisioner.apply(context).outcome is ProvisionOutcome.SKIPPED, "nothing changed"
    assert (package / "frontend" / "pepito-native" / "v1" / "pepito_image_ui.js").read_bytes() == (
        assets / "pepito_image_ui.js"
    ).read_bytes()


def test_repair_source_and_distribution_pin_mismatch_fail_clearly(
    tmp_path, bundled_assets, monkeypatch
):
    context, package, _ = repair_context(tmp_path, bundled_assets, monkeypatch)
    (package / "main.py").write_text("app = None\n", encoding="utf-8")
    result = OpenWebUIProvisioner(version="0.12.0").apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (package / "pepito_native.py").exists()
    context.runner.installed = "0.12.1"
    with pytest.raises(NativeExtensionError, match="pin mismatch"):
        sync_native_extension(
            context, PythonEnvironment.inside(context.layout.open_webui_app), version="0.12.0"
        )


def test_external_package_metadata_is_never_modified(tmp_path, bundled_assets, monkeypatch):
    context, _, _ = repair_context(tmp_path, bundled_assets, monkeypatch)
    external = managed_package(tmp_path / "external")
    context.runner.package = external
    result = OpenWebUIProvisioner(version="0.12.0").apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (external / "pepito_native.py").exists()


def test_failed_fresh_native_sync_does_not_mark_managed_success(
    tmp_path, bundled_assets, monkeypatch
):
    # Exercise the actual install tail: the package is present but its source is
    # unsupported, so a fresh attempt must not acquire a successful marker.
    context, package, _ = repair_context(tmp_path, bundled_assets, monkeypatch)
    marker_path(context.layout.open_webui_app).unlink()
    (package / "main.py").write_text("app = None\n", encoding="utf-8")
    monkeypatch.setattr("local_ai_stack.open_webui.environment_is_reusable", lambda *args: True)
    monkeypatch.setattr("local_ai_stack.open_webui.pip_install", lambda *args, **kwargs: None)
    provisioner = OpenWebUIProvisioner(version="0.12.0")
    detection = provisioner.detect(context)
    result = provisioner._install(
        context, detection, provisioner.plan(context), "open-webui==0.12.0", Path(sys.executable)
    )
    assert result.outcome is ProvisionOutcome.FAILED
    assert not marker_path(context.layout.open_webui_app).exists()


def test_typed_and_http_approvals_share_one_idempotent_owned_attempt(native, monkeypatch):
    from test_open_webui_image_filter import load_module

    register(native)
    publish(native, number=2, file_id="alice-extra", attempt_id="attempt-1")
    monkeypatch.setitem(sys.modules, "open_webui.pepito_native", native.module)
    module = load_module()
    reply = asyncio.run(
        module["typed_approval"]({"chat_id": "chat-a"}, "alice", "aprovo a imagem 2")
    )
    assert "aprovada" in reply
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 1
    repeated = accept(native).json()
    assert repeated["feedback"]["recorded"] is False
    assert native.calibration.calibration_state(native.data_dir, "alice")["accepted_samples"] == 1
    denied = asyncio.run(
        module["typed_approval"]({"chat_id": "chat-a"}, "bob", "aprovo a imagem 2")
    )
    assert "aprovada" not in denied
