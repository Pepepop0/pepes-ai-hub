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


def with_plan(answer):
    """A scripted router answer as the schema-constrained model returns it: the plan and each
    reference's purpose filled in unless the script states them. Scripts that test the plan
    itself state it, and win."""
    if not isinstance(answer, dict) or "action" not in answer:
        return answer
    refs = [
        {"purpose": "the source for this request", **ref} for ref in answer.get("references") or []
    ]
    base = next((ref["image"] for ref in refs if ref.get("role") == "A"), 0)
    plan = {
        "request_mode": "refine" if base else "new",
        "scope": "local" if base else "global",
        "region_targets": [],
        "base": base,
        "changes": [],
        "preserve": [],
        "correction": "none",
        "clarification": "",
    }
    return {**answer, "references": refs, "plan": {**plan, **(answer.get("plan") or {})}}


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


def test_a_created_picture_is_said_to_be_new_and_an_edited_one_is_not():
    """Regression (RTX 5090 rig): under a new, unrelated picture the model claimed
    it had enlarged something in the earlier one."""
    module = load_module()
    created = module["shown_context"]("<context>... has been created ...</context>")
    edited = module["shown_context"]("<context>... has been edited and created ...</context>")
    assert "not a change to an earlier one" in created
    assert "not a change to an earlier one" not in edited


def test_the_shipped_source_declares_the_pinned_open_webui_version():
    assert "required_open_webui_version: 0.12.0" in SOURCE
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
        # Once edits (browser, 8GB root, filter 0.5.1): "de" read as "dê", and
        # an article anywhere read as pointing at the earlier picture.
        "Gere um castelo de pedra à noite.",
        "Crie uma casa de madeira na praia.",
        "Gere uma paisagem de montanha ao amanhecer.",
        "Faça um gato na praia.",
        "Pinte um pôr do sol na praia.",
        "Paint a sunset over the sea.",
        "Make a cat on the table.",
        "Gere uma imagem de uma mulher ruiva.",
        "Make a dog that looks happy",
        # A comparative inside a new description, with no pronoun pointing back.
        "A woman with longer hair in a garden",
        "Gere um castelo mais alto que as nuvens",
        "A tower taller than the clouds over the water",
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
        "Deixe o cabelo dela loiro.",
        "Deixe a maçã verde.",
        "Deixe ela verde.",
        "Dê um chapéu para ela.",
        "Make her green.",
        "Change the mug to red.",
        "Change it to red.",
        "Give a hat to her.",
        "Give it a red hat",
        "Faça a maçã ficar verde",
        # Regression (RTX 5090 rig): a request to redo the picture in the chat,
        # opened with a generate verb, was taken as a brand-new picture.
        "Gere a mesma imagem, mas com o cabelo ruivo",
        "Gere de novo na mesma posição, mas com o vestido preto",
        "Generate it again in the same pose, but at night",
        # Regression (RTX 5090 rig): a typo in "image" and a comparative of "it"
        # made an unrelated new picture instead of an edit.
        "in this immage, i want it massively bigger",
        "I want it massively bigger",
        "Make her chest bigger",
        "Deixa ela mais magra",
        "Nessa imgem, quero ela maior",
        "Na imagem, aumente o tamanho",
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


# --- exclusive VRAM: chat and image models alternate on one card ---------------


class FakeServices:
    """Ollama and ComfyUI as the filter's urllib calls reach them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, request, timeout=None):
        import io
        import json

        url = request if isinstance(request, str) else request.full_url
        body = None if isinstance(request, str) else json.loads(request.data)
        self.calls.append((url, body))
        payload = b"{}"
        if url.endswith("/api/ps"):
            payload = json.dumps({"models": [{"name": "chat:27b"}]}).encode()
        return io.BytesIO(payload)


def test_an_image_turn_unloads_ollama_before_the_image_and_frees_comfyui_after(monkeypatch):
    import urllib.request

    services = FakeServices()
    monkeypatch.setattr(urllib.request, "urlopen", services)
    monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("PEPITO_COMFYUI_URL", "http://127.0.0.1:8188")
    filter_ = load_filter()
    metadata = ui_metadata()
    body = {"model": "chat:27b", "messages": [{"role": "user", "content": "an apple"}]}

    run(filter_.inlet(body, metadata))
    assert services.calls == [
        ("http://127.0.0.1:11434/api/ps", None),
        ("http://127.0.0.1:11434/api/generate", {"model": "chat:27b", "keep_alive": 0}),
    ]

    services.calls.clear()
    run(filter_.request(body, metadata))
    assert services.calls == [
        ("http://127.0.0.1:8188/free", {"unload_models": True, "free_memory": True}),
        # The card is given back only once ComfyUI says it let the memory go.
        ("http://127.0.0.1:8188/system_stats", None),
    ]


def test_without_exclusive_vram_nothing_is_unloaded(monkeypatch):
    import urllib.request

    services = FakeServices()
    monkeypatch.setattr(urllib.request, "urlopen", services)
    monkeypatch.delenv("PEPITO_EXCLUSIVE_VRAM", raising=False)
    filter_ = load_filter()
    metadata = ui_metadata()
    body = {"model": "m", "messages": [{"role": "user", "content": "an apple"}]}
    run(filter_.inlet(body, metadata))
    run(filter_.request(body, metadata))
    assert services.calls == []


def test_an_unreachable_service_never_breaks_the_turn(monkeypatch):
    import urllib.request

    def refuse(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("PEPITO_COMFYUI_URL", "http://127.0.0.1:8188")
    metadata = ui_metadata()
    body = {"model": "m", "messages": [{"role": "user", "content": "an apple"}]}
    filter_ = load_filter()
    assert run(filter_.inlet(body, metadata)) is body
    assert run(filter_.request(body, metadata)) is body
    assert metadata["params"]["function_calling"] == "legacy"


# --- the router: one constrained call, checked and with a fallback ------------------------


class FakeRouter:
    """Ollama's /api/chat as the router reaches it: records the payload, answers ``content``."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.payloads: list[dict] = []

    def __call__(self, request, timeout=None):
        import io
        import json

        if isinstance(request, str) and request.endswith("/api/ps"):
            return io.BytesIO(b'{"models": []}')
        self.payloads.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": self.content}}).encode())


def routed_env(monkeypatch, router) -> None:
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", router)
    monkeypatch.setenv("PEPITO_IMAGE_ROUTER", "true")
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")


@pytest.mark.parametrize(("num_ctx", "sent"), [(8192, 8192), (None, None), ("big", None)])
def test_the_router_asks_once_constrained_without_thinking_in_the_chats_own_context(
    monkeypatch, num_ctx, sent
):
    """A different num_ctx than the chat's makes Ollama reload the resident model."""
    module = load_module()
    router = FakeRouter('{"action": "new_image", "prompt": "A red apple."}')
    routed_env(monkeypatch, router)
    preset = {"info": {"base_model_id": "qwen:27b", "params": {"num_ctx": num_ctx}}}
    messages = [{"role": "user", "content": "uma maçã"}]
    decision = run(
        module["route"](
            module["route_model"]({"model": "preset-id"}, preset),
            messages,
            [],
            module["route_num_ctx"](preset),
        )
    )
    assert decision == {
        "action": "new_image",
        "prompt": "A red apple.",
        "minor_risk": False,
        "explicit": False,
        "references": [],
        "plan": None,
        "edit_mode": "local",
        "pose": "",
        "style": "",
        "aspect": "",
        "count": 0,
        "change": "subtle",
    }
    (payload,) = router.payloads
    assert payload["model"] == "qwen:27b", "a workspace preset routes on its base model"
    assert payload["format"] == module["ROUTER_SCHEMA"]
    assert payload["format"]["properties"]["action"]["enum"] == [
        "chat",
        "web_search",
        "image_search",
        "new_image",
        "edit_image",
        "show_image",
    ]
    assert payload["think"] is False and payload["stream"] is False
    assert payload["options"].get("num_ctx") == sent
    assert "keep_alive" not in payload, "routing never changes what stays resident"


def test_the_router_sees_the_recent_conversation_and_whether_a_picture_exists():
    module = load_module()
    messages = [{"role": "system", "content": "be nice"}]
    for index in range(10):
        messages.append({"role": "user", "content": f"pedido {index}"})
        messages.append({"role": "assistant", "content": f"resposta {index}"})
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": "deixa maior"}, {"type": "image_url"}],
        }
    )
    asked = module["router_messages"](
        messages,
        [{"by": "user", "url": "u1", "group": 0}, {"by": "assistant", "url": "u2", "group": 1}],
    )
    assert len(asked) == 2 and asked[0]["role"] == "system"
    turn = asked[-1]["content"]
    assert turn.startswith(
        "SEARCH: on\nHAS_IMAGE: true\nSUPPORTED INPUT COUNTS: 1\n"
        "IMAGES: 1 uploaded by the user, 2 made by you\n"
    )
    assert turn.endswith("LAST USER MESSAGE: deixa maior")
    assert "user: pedido 7" in turn and "assistant: resposta 9" in turn
    assert "pedido 6" not in turn and "be nice" not in turn, "six messages, no system"


@pytest.mark.parametrize(
    ("decision", "has_image", "expected"),
    [
        ({"action": "chat", "prompt": ""}, True, ("chat", "", "model")),
        ({"action": "new_image", "prompt": "A cat."}, True, ("new_image", "A cat.", "model")),
        (
            {"action": "edit_image", "prompt": "Make it red."},
            True,
            ("edit_image", "Make it red.", "model"),
        ),
        # Checked against what is true: nothing to edit, so a new picture.
        (
            {"action": "edit_image", "prompt": "Make it red."},
            False,
            ("new_image", "Make it red.", "model"),
        ),
        # Never an empty prompt for a picture: the user's words instead.
        ({"action": "new_image", "prompt": ""}, False, ("new_image", "Change it to red", "model")),
        # No decision: the word rules, with the words as the prompt.
        (None, True, ("edit_image", "Change it to red", "rules")),
        (None, False, ("chat", "", "rules")),
    ],
)
def test_the_decision_is_checked_and_without_one_the_word_rules_decide(
    decision, has_image, expected
):
    assert load_module()["decide"](decision, "Change it to red", has_image) == expected


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        '{"action": "draw", "prompt": "x"}',
        '{"action": "new_image", "prompt": 3}',
        "[]",
    ],
)
def test_an_answer_outside_the_schema_is_no_decision(monkeypatch, content):
    module = load_module()
    routed_env(monkeypatch, FakeRouter(content))
    assert run(module["route"]("m", [{"role": "user", "content": "x"}], [{"by": "user"}])) is None


def test_a_chat_turn_is_left_as_if_the_button_were_off_and_the_model_is_told(monkeypatch):
    routed_env(monkeypatch, FakeRouter('{"action": "chat", "prompt": ""}'))
    monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
    module = load_module()
    metadata = ui_metadata()
    parts = [{"type": "text", "text": "o que é isso?"}, {"type": "image_url", "image_url": {}}]
    body = {
        "model": "m",
        "features": metadata["features"],
        "messages": [{"role": "user", "content": parts}],
    }
    run(module["Filter"]().inlet(body, metadata))
    assert metadata["params"]["function_calling"] == "native", "the user's own mode"
    assert metadata["features"]["image_generation"] is False, "no generate_image tool either"
    assert metadata["pepito_image_chat"]["no_image"] is True
    assert body["messages"][0]["role"] == "system"
    told = body["messages"][0]["content"]
    assert "is ON" in told and "Never tell the user to switch Image on" in told
    assert "with Image on" not in told
    assert body["messages"][1]["content"] is parts, "a vision model still sees the picture"


# --- the workflow of each chat model -----------------------------------------------


def fake_images_module(monkeypatch):
    """open_webui.routers.images as the filter reaches it: a stored setting read
    through the module's get_image_config, by image_generations as by the real one."""
    import sys
    import types

    images = types.ModuleType("open_webui.routers.images")

    async def get_image_config():
        return types.SimpleNamespace(COMFYUI_WORKFLOW="stored", IMAGE_GENERATION_MODEL="stored")

    async def image_generations(**_kwargs):
        config = await images.get_image_config()  # a global lookup, like the real one
        await asyncio.sleep(0)  # another turn may run in between
        return (config.COMFYUI_WORKFLOW, config.IMAGE_GENERATION_MODEL)

    images.get_image_config = get_image_config
    images.image_generations = image_generations
    for name, module in {
        "open_webui": types.ModuleType("open_webui"),
        "open_webui.routers": types.ModuleType("open_webui.routers"),
        "open_webui.routers.images": images,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return images


def test_each_chat_model_s_pictures_come_from_its_own_workflow(monkeypatch, tmp_path):
    """A model the Hub names a stack for reads that stack's settings for the length
    of its own call, a turn running at the same time on another model reads its
    own, and nothing is left behind -- also after Open WebUI executes the filter
    again on an update."""
    images = fake_images_module(monkeypatch)
    stacks = tmp_path / "pepito-image-stacks.json"
    stacks.write_text(
        '{"_pepito_managed_by": "hub", "capella": {"generate": {"workflow": "krea", '
        '"model": "krea2.safetensors", "nodes": []}}, "qwen:27b": {"generate": '
        '{"workflow": "qwen", "model": "qwen.safetensors", "nodes": []}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PEPITO_IMAGE_STACKS", str(stacks))
    first, second = load_module(), load_module()  # the filter, executed again
    assert first["image_stack"]("nobody") is None

    async def turn(module: dict, model: str):
        stack = module["image_stack"](model)
        with module["stack_overlay"](module["stack_settings"](stack, "generate")):
            return await images.image_generations()

    async def both():
        return await asyncio.gather(turn(first, "capella"), turn(second, "qwen:27b"))

    assert run(both()) == [("krea", "krea2.safetensors"), ("qwen", "qwen.safetensors")]
    assert run(images.image_generations()) == ("stored", "stored")
    assert run(turn(second, "nobody")) == ("stored", "stored")


# --- the edit Open WebUI makes by itself comes back to its source's tone (0.29.1) --------------


def owui_edit_turn(monkeypatch, request_text, edited_picture, source_picture=None):
    """The rules path (no router) end to end: inlet names the source, Open WebUI's image step
    leaves ``edited_picture`` stored and attached, request corrects it. Returns the stored
    bytes after the turn, the trace events and the source bytes."""
    import importlib.util
    import io
    import sys

    Image = pytest.importorskip("PIL.Image")
    monkeypatch.delenv("PEPITO_IMAGE_ROUTER", raising=False)
    path = Path(__file__).resolve().parents[1] / "assets" / "open-webui"
    spec = importlib.util.spec_from_file_location(
        "open_webui.pepito_image_preservation", path / "pepito_image_preservation.py"
    )
    preservation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preservation)
    monkeypatch.setitem(sys.modules, "open_webui.pepito_image_preservation", preservation)

    def encode(image):
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()

    source = encode(source_picture or Image.new("RGB", (64, 64), (120, 100, 80)))
    stored = {"/api/v1/files/" + "1" * 36 + "/content": source}
    new_url = "/api/v1/files/" + "2" * 36 + "/content"
    stored[new_url] = encode(edited_picture)
    old_url = next(iter(stored))
    events: list = []
    module = load_module()

    async def picture_bytes(url):
        return stored.get(url)

    async def replace_picture(url, data):
        stored[url] = data
        return True

    async def chat_image_sets(*_args):
        return [[old_url]]

    async def attached_images(_metadata):
        return [{"type": "image", "url": new_url}]

    module.update(
        picture_bytes=picture_bytes,
        replace_picture=replace_picture,
        chat_image_sets=chat_image_sets,
        attached_images=attached_images,
        trace_event=lambda _metadata, event, **fields: events.append((event, fields)),
        trace_image_job=lambda _metadata, _a, _b, **fields: events.append(("image_job", fields)),
    )
    filter_ = module["Filter"]()
    metadata = ui_metadata()
    body = {"model": "m", "messages": [{"role": "user", "content": request_text}]}
    run(filter_.inlet(body, metadata, {"id": "u1"}))
    body["messages"].insert(
        0, {"role": "system", "content": module["shown_context"]("<context>edited</context>")}
    )
    run(filter_.request(body, metadata, None, None, {"id": "u1"}))
    return stored[new_url], events, source


def mean_level(data):
    import io

    from PIL import Image, ImageStat

    return sum(ImageStat.Stat(Image.open(io.BytesIO(data)).convert("RGB")).mean) / 3


def test_an_edit_open_webui_made_that_drifted_lighter_is_brought_back_to_the_source(monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    brighter = Image.new("RGB", (64, 64), (136, 116, 96))  # +16 levels, the pod's drift
    delivered, events, source = owui_edit_turn(monkeypatch, "make the jacket blue", brighter)
    assert abs(mean_level(delivered) - mean_level(source)) < 1.0
    names = [name for name, _ in events]
    assert names.index("tone_match") < names.index("image_job")
    assert dict(events)["tone_match"]["results"][0]["applied"] is True
    assert dict(events)["image_job"]["workflow"] == "openwebui"
    assert dict(events)["image_job"]["part"] == "edit"


def test_a_new_scene_from_open_webui_is_left_as_it_came(monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    other = Image.new("RGB", (64, 64), (20, 30, 220))  # nothing like the source
    delivered, events, _ = owui_edit_turn(monkeypatch, "make the jacket blue", other)
    assert abs(mean_level(delivered) - mean_level_of(other)) < 0.01
    assert dict(events)["tone_match"]["results"][0]["reason"] == "reference_changed"


def mean_level_of(image):
    import io

    out = io.BytesIO()
    image.save(out, "PNG")
    return mean_level(out.getvalue())


def test_a_request_for_darker_light_is_not_corrected(monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    darker = Image.new("RGB", (64, 64), (90, 70, 50))
    delivered, events, _ = owui_edit_turn(monkeypatch, "make it darker", darker)
    assert abs(mean_level(delivered) - mean_level_of(darker)) < 0.01
    assert dict(events)["tone_match"] == {"applied": False, "reason": "light_request"}
