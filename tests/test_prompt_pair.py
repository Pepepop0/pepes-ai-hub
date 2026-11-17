"""SPEC-319: an edit of one picture is made twice (E and C prompts, same seed), and an edit
with an area delivers the model's whole output unless ``area_paste`` is on."""

from __future__ import annotations

import asyncio
import json

from test_open_webui_native import ALICE, BOB, native, publish  # noqa: F401

SAID = "deixa o vestido azul"
ROUTER = "Make the dress blue. Keep her face."
E_PROMPT = "Recolor the dress to a deep blue. Keep her face, the pose and the background."
C_PROMPT = "Change the dress color to a rich royal blue; keep the face and the pose."
PAIR_NOTE = "2 versões da edição: escolha a sua favorita com «Prefiro esta»."


def revision(prompt=E_PROMPT):
    return json.dumps({"prompt": prompt, "fits_area": True, "outside": ""})


def thought(prompt=C_PROMPT, **extra):
    return json.dumps({"action": "edit_image", "prompt": prompt, "minor_risk": False, **extra})


def writers(turn, monkeypatch, *, e=revision, c=thought):
    """The Ollama calls of the pair: E (no thinking) and C (thinking); a callable that is
    None, or raises, is a writer that failed."""
    calls = []

    def write(url, payload, timeout=180):
        calls.append(payload)
        answer = c if payload["think"] else e
        if answer is None:
            raise TimeoutError("the writer did not answer")
        return answer()

    monkeypatch.setitem(turn.filter.inlet.__func__.__globals__, "prompt_writer_sync", write)
    return calls


# --- the whole output, without the paste ---------------------------------------------------


def delivered(turn, files):
    return [files.store[item["id"]][0] for item in turn.history[-1]["files"]]


# --- the three corrections of the revision system prompt ---------------------------------


def test_the_revision_prompt_carries_the_lab_03_corrections():
    from test_open_webui_image_filter import load_module

    system = load_module()["PROMPT_REVISION_SYSTEM"]
    assert "much paler" in system and "never stand alone" in system
    assert "Keep ... unchanged" in system and "needs to touch" in system
    assert "never remove arms, hands or any body part" in system


# --- "Prefiro esta" (the native store) -----------------------------------------------------


def publish_pair(native, numbers=(1, 2), files=("alice-file", "alice-extra")):  # noqa: F811
    entries = [
        {
            "number": number,
            "url": f"/api/v1/files/{file_id}/content",
            "description": "An image",
            "variation": index,
            "total": len(numbers),
            "current": index == len(numbers),
            "attempt_id": None,
            "origin": None,
        }
        for index, (number, file_id) in enumerate(zip(numbers, files), 1)
    ]
    return asyncio.run(
        native.module.publish_gallery("chat-a", "alice", "assistant-message", entries)
    )


def prefer(native, number, headers=ALICE, chat_id="chat-a"):  # noqa: F811
    return native.client.post(
        "/api/v1/pepito/images/preference",
        headers=headers,
        json={"chat_id": chat_id, "number": number},
    )


def gallery(native, group, headers=ALICE):  # noqa: F811
    return native.client.get(f"/api/v1/pepito/images/gallery/{group}", headers=headers)


def test_a_preference_is_the_owners_and_selects_the_picture(native):  # noqa: F811
    group = publish_pair(native)
    shown = gallery(native, group).json()
    assert shown["preferred_number"] is None
    assert prefer(native, 2, headers=BOB).status_code == 404
    assert prefer(native, 2, chat_id="chat-b").status_code == 404
    assert prefer(native, 9).status_code == 404, "a number the chat never published"
    assert (
        native.client.post(
            "/api/v1/pepito/images/preference", json={"chat_id": "chat-a", "number": 2}
        ).status_code
        == 401
    )
    assert gallery(native, group).json()["preferred_number"] is None, "nothing was written"
    response = prefer(native, 2)
    assert response.status_code == 200
    assert response.json() == {"group_id": group, "preferred_number": 2, "selected_number": 2}
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) == 2
    shown = gallery(native, group).json()
    assert shown["preferred_number"] == 2 and shown["explicit_selected_number"] == 2
    assert gallery(native, group, headers=BOB).status_code == 404
    # The last click wins, and the selection follows it.
    assert prefer(native, 1).json()["preferred_number"] == 1
    assert gallery(native, group).json()["preferred_number"] == 1
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) == 1


def test_a_preference_needs_a_gallery_of_two_or_more(native):  # noqa: F811
    publish(native)
    alone = prefer(native, 1)
    assert alone.status_code == 409
    assert asyncio.run(native.module.read_selection("chat-a", "alice")) is None
