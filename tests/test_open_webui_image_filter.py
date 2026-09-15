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
