"""The Filter Function the Hub installs in Open WebUI, hook by hook.

Open WebUI executes the filter's source itself, so it is loaded here the way
Open WebUI loads it -- as source, not as a Hub import -- and driven with the
request metadata the real UI produces (captured from the managed instance).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from local_ai_stack.installation import detect_repo_root
from local_ai_stack.open_webui_filters import filter_path, filter_source

SOURCE = filter_source()


def load_module() -> dict:
    """What Open WebUI does with the stored source: exec it as a module."""
    namespace: dict = {}
    exec(compile(SOURCE, "function_pepito_image_chat", "exec"), namespace)  # noqa: S102
    return namespace


def load_filter():
    return load_module()["Filter"]()


def ui_metadata(*, image: bool = True, session: bool = True) -> dict:
    """The shape captured from Open WebUI 0.11.3 for a composer turn."""
    return {
        "session_id": "sid-1234" if session else None,
        "chat_id": "chat-1",
        "message_id": "msg-1",
        "features": {
            "voice": False,
            "image_generation": image,
            "code_interpreter": False,
            "web_search": False,
            "memory": True,
        },
        "params": {
            "stream_delta_chunk_size": None,
            "reasoning_tags": None,
            "compact_token_threshold": None,
            "function_calling": "native",
            "tool_approval_mode": "full",
        },
        "tools": {},
    }


def run(coroutine):
    return asyncio.run(coroutine)


# --- inlet: the routing decision -----------------------------------------------


def test_an_image_turn_is_routed_through_open_webui_forced_generation():
    metadata = ui_metadata()
    body = {"model": "hermes3:8b", "messages": [{"role": "user", "content": "an apple"}]}
    assert run(load_filter().inlet(body, metadata)) is body
    assert metadata["params"]["function_calling"] == "legacy"
    assert body["messages"] == [{"role": "user", "content": "an apple"}]


def test_a_plain_chat_turn_is_left_alone():
    metadata = ui_metadata(image=False)
    run(load_filter().inlet({}, metadata))
    assert metadata["params"]["function_calling"] == "native"
    assert "pepito_image_chat" not in metadata


def test_an_api_call_without_a_ui_session_is_left_alone():
    # No session means no event channel for Open WebUI to attach the image
    # to, so forcing its pipeline would silently produce nothing.
    metadata = ui_metadata(session=False)
    run(load_filter().inlet({}, metadata))
    assert metadata["params"]["function_calling"] == "native"


def test_missing_metadata_pieces_never_raise():
    filter_ = load_filter()
    for metadata in ({}, {"features": None}, {"features": {"image_generation": True}}):
        assert run(filter_.inlet({"x": 1}, metadata)) == {"x": 1}
    metadata = {"features": {"image_generation": True}, "session_id": "s", "params": None}
    run(filter_.inlet({}, metadata))
    assert metadata["params"] is None


# --- stream and outlet: the acknowledgement ------------------------------------


def stream_text(filter_, metadata: dict, chunks: list[str]) -> list[str]:
    shown = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        event = {
            "choices": [{"delta": {"content": chunk}, "finish_reason": "stop" if last else None}]
        }
        event = run(filter_.stream(event, metadata))
        text = event["choices"][0]["delta"].get("content")
        if text:
            shown.append(text)
    return shown


def test_a_made_up_image_link_never_reaches_the_screen():
    filter_ = load_filter()
    metadata = ui_metadata()
    run(filter_.inlet({}, metadata))
    shown = stream_text(
        filter_,
        metadata,
        [
            "Here is the image you requested:",
            "\n\n![Red Apple](https://www.example.com/red_apple.jpg)",
        ],
    )
    assert shown == ["Here is the image you requested:"]


def test_an_acknowledgement_that_is_only_a_link_becomes_a_plain_statement():
    filter_ = load_filter()
    metadata = ui_metadata()
    run(filter_.inlet({}, metadata))
    assert stream_text(filter_, metadata, ["![image](/api/v1/files/abc/content)"]) == [
        "Image generated."
    ]


def test_an_empty_acknowledgement_becomes_a_plain_statement():
    filter_ = load_filter()
    metadata = ui_metadata()
    run(filter_.inlet({}, metadata))
    assert stream_text(filter_, metadata, [""]) == ["Image generated."]


def test_a_relayed_failure_message_is_kept():
    filter_ = load_filter()
    metadata = ui_metadata()
    run(filter_.inlet({}, metadata))
    text = "Image generation was attempted but failed: ComfyUI did not answer."
    assert stream_text(filter_, metadata, [text]) == [text]


def test_the_persisted_message_is_cleaned_too():
    filter_ = load_filter()
    metadata = ui_metadata()
    run(filter_.inlet({}, metadata))
    body = {
        "messages": [
            {"role": "user", "content": "an apple"},
            {"role": "assistant", "content": "Done: ![a](https://x.y/a.png) see www.x.y/a"},
        ]
    }
    run(filter_.outlet(body, metadata))
    assert body["messages"][1]["content"] == "Done:  see"
    assert body["messages"][0]["content"] == "an apple"


def test_plain_chat_streams_and_persists_untouched():
    filter_ = load_filter()
    metadata = ui_metadata(image=False)
    run(filter_.inlet({}, metadata))
    text = "See https://example.com and ![i](https://example.com/i.png)"
    assert stream_text(filter_, metadata, [text]) == [text]
    body = {"messages": [{"role": "assistant", "content": text}]}
    run(filter_.outlet(body, metadata))
    assert body["messages"][0]["content"] == text


def test_turns_do_not_share_state():
    filter_ = load_filter()
    first, second = ui_metadata(), ui_metadata()
    run(filter_.inlet({}, first))
    run(filter_.inlet({}, second))
    run(filter_.stream({"choices": [{"delta": {"content": "one"}, "finish_reason": None}]}, first))
    assert stream_text(filter_, second, ["two"]) == ["two"]
    assert stream_text(filter_, first, [" more"]) == ["one more"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A red apple. ![x](https://a.b/c.png)", "A red apple."),
        (
            'Here it is:\n\n<img src="https://a.b/green.png" alt="A green apple.">\n\nEnjoy.',
            "Here it is:\n\nEnjoy.",
        ),
        ('<IMG SRC="/api/v1/files/x/content">', "Image generated."),
        ("Look: [here](https://a.b/c) now", "Look: here now"),
        ("http://a.b/c.png", "Image generated."),
        ("Line\n\n\n\nBreak", "Line\n\nBreak"),
        ("   ", "Image generated."),
    ],
)
def test_clean_acknowledgement(text, expected):
    assert load_module()["clean_acknowledgement"](text) == expected


# --- the success context, said so that a small model believes it -----------------------


@pytest.mark.parametrize(
    ("claim", "verb"),
    [
        (
            (
                "<context>The requested image has been created by the system successfully and "
                "is now being shown to the user. Let the user know that the image they "
                "requested has been generated and is now shown in the chat.</context>"
            ),
            "created",
        ),
        (
            (
                "<context>The requested image has been edited and created and is now being "
                "shown to the user. Let them know that it has been generated.</context>"
            ),
            "edited",
        ),
    ],
)
def test_the_shown_context_keeps_what_happened_and_the_success_mark(claim, verb):
    module = load_module()
    shown = module["shown_context"](claim)
    assert shown.startswith(f"<context>The requested image has been {verb}")
    assert module["SUCCESS_MARK"] in shown and shown.endswith("</context>")
    assert "you cannot create" in shown  # the sentence a 3B model needs
    assert module["claimed_success"]([{"role": "system", "content": shown}]) is not None


def test_the_shipped_source_declares_the_pinned_open_webui_version():
    assert "required_open_webui_version: 0.11.3" in SOURCE
    assert "title: Pepito Image Chat" in SOURCE


def test_the_filter_is_a_bundled_resource_not_a_package_module():
    # A frozen build ships resources, not package sources; the filter must be
    # readable from the resource root or the sync fails before any request.
    assert (
        filter_path() == Path(detect_repo_root()) / "assets" / "open-webui" / "pepito_image_chat.py"
    )
    assert filter_path().is_file()


# --- edit or new picture: the user's words decide, biased towards a new picture ---------


@pytest.mark.parametrize(
    "text",
    [
        "Generate an image of a cat.",
        "Now an image of a necromancer.",
        "I want an image of a blue ceramic mug.",
        "Gere uma imagem de um castelo.",
        "Agora faça uma imagem de um carro vermelho.",
        "Generate an image of the WW2 tiger heavy tank",
        "Now an image of a necromancer",
        "Now I want an image of a blue ceramic mug",
        "Generate an image of a medieval castle",
        "Generate a castle at night.",
        "Now generate an image of a necromancer.",
        "Make a cat",
        "Paint a sunset",
        "Faça um gato",
        "Gere a imagem de um gato",
        "A dragon flying over mountains",
        "the same but at night",
        "",
        "Generate an image of a red apple on a white background.",
        "Generate an image of a stone castle at night.",
        "Make a girl hugging her cat",
        "A knight and his horse",
    ],
)
def test_these_ask_for_a_new_picture(text):
    assert load_module()["edit_intent"](text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Change the cat to black.",
        "Make it blue.",
        "Change the background to white.",
        "Put a hat on it.",
        "Mude a cor para verde.",
        "Troque o fundo para preto.",
        "Coloque um chapéu nela.",
        "Change the mug to red",
        "Change the background to black",
        "Put the red mug on a wooden table.",
        "Now change the mug to red",
        "Add a hat",
        "Turn it into a robot",
        "Paint the sky red",
        "Faça o fundo preto",
        "Deixe o fundo preto",
        "Remove the background",
        "Keep the mug but make the table wooden",
        "add a hat to this image",
        "in this image, make the sky purple",
        "Replace the background with a picture of a forest",
        "Change this into an image of a cat",
        "Nessa imagem, mude o fundo",
        "Transforme em uma pintura a óleo",
        "Make the previous image darker",
        "Pinte o céu de vermelho",
        "Mantenha a caneca, mas mude a mesa",
        "make her green",
        "Make her blonde",
        "make him taller",
        "Turn him into a knight",
        "Make the apple green.",
    ],
)
def test_these_ask_to_change_the_picture_in_the_chat(text):
    assert load_module()["edit_intent"](text) is True


def test_a_new_picture_in_a_chat_that_holds_one_turns_the_feature_off_for_the_turn():
    """The inlet's contract: Open WebUI's handler must not see this turn at all."""
    module = load_module()
    filter_ = module["Filter"]()
    metadata = ui_metadata()
    body = {
        "model": "hermes3:8b",
        "features": metadata["features"],
        "messages": [{"role": "user", "content": "Now an image of a necromancer"}],
    }

    async def holds(metadata, user_id, messages):
        return True

    module["chat_holds_images"] = holds
    filter_.inlet.__func__.__globals__["chat_holds_images"] = holds
    run(filter_.inlet(body, metadata, {"id": "user-1"}))
    assert metadata["features"]["image_generation"] is False
    assert body["features"]["image_generation"] is False
    assert metadata["params"]["function_calling"] == "legacy"
    state = metadata["pepito_image_chat"]
    assert state["edit"] is False
    assert state["generate"] == {"prompt": "Now an image of a necromancer", "model": "hermes3:8b"}

    # An edit leaves everything to Open WebUI, and so does a chat without pictures.
    for text, holds_images in (("Change it to red", True), ("Now an image of a dog", False)):

        async def answer(metadata, user_id, messages, value=holds_images):
            return value

        filter_.inlet.__func__.__globals__["chat_holds_images"] = answer
        metadata = ui_metadata()
        body = {
            "model": "m",
            "features": metadata["features"],
            "messages": [{"role": "user", "content": text}],
        }
        run(filter_.inlet(body, metadata, {"id": "user-1"}))
        assert metadata["features"]["image_generation"] is True
        assert "generate" not in metadata["pepito_image_chat"]
        assert metadata["pepito_image_chat"]["edit"] is (text == "Change it to red")


def test_the_chat_lookup_answers_false_when_it_cannot_look():
    module = load_module()
    holds = module["chat_holds_images"]
    assert run(holds({"chat_id": "chat-1"}, None, [])) is False
    assert run(holds({}, "user-1", [])) is False


@pytest.mark.parametrize("chat_id", ["temporary:sid-1", "local:sid-1", "channel:c1"])
def test_a_chat_that_is_not_saved_is_read_from_the_request_like_the_handler_does(chat_id):
    module = load_module()
    holds = module["chat_holds_images"]
    text_only = [
        {"role": "user", "content": "Generate a tank"},
        {"role": "assistant", "content": "ok"},
    ]
    with_picture = [
        *text_only,
        {
            "role": "assistant",
            "content": "",
            "files": [{"type": "image", "url": "/api/v1/files/a/content"}],
        },
    ]
    by_content_type = [
        {"role": "user", "content": "x", "files": [{"content_type": "image/png", "url": "u"}]}
    ]
    assert run(holds({"chat_id": chat_id}, "user-1", text_only)) is False
    assert run(holds({"chat_id": chat_id}, "user-1", with_picture)) is True
    assert run(holds({"chat_id": chat_id}, None, with_picture)) is True, "no user needed"
    assert run(holds({"chat_id": chat_id}, "user-1", by_content_type)) is True
    assert (
        module["images_in_messages"](
            [{"role": "user", "files": [{"type": "file", "content_type": "text/plain"}]}]
        )
        is False
    )
