"""
title: Pepito Image Chat
author: Pepito's AI Hub
version: 0.3.0
required_open_webui_version: 0.11.3
description: Send every Image turn through Open WebUI's own image generation or editing, whatever the model says.
"""

# This file is uploaded verbatim as an Open WebUI Filter Function. Keep it
# self-contained: the managed Open WebUI venv does not import the Hub package.
#
# Why this exists. With the Image feature on, Open WebUI 0.11.3 has two ways
# to make a picture:
#
# * "native" function calling (the UI's default): the builtin generate_image
#   tool is offered to the model, and the picture is made only if the model
#   answers with a structurally correct tool call. A small local model gets
#   this wrong -- it invents argument names, writes the call as prose, or says
#   nothing -- and then nothing reaches ComfyUI at all;
# * "legacy" function calling: Open WebUI itself turns the user's message
#   into an image prompt, submits the configured ComfyUI workflow, attaches
#   the result to the message, and only then asks the model to acknowledge.
#
# The second path does not depend on the model at all, so the Hub routes
# every Image turn through it. This filter runs at ``inlet``, before Open
# WebUI decides which path to take, and flips that one request's function
# calling mode. Ordinary chat turns, API calls without a UI session, and
# every other feature are left exactly as they were.
#
# The same legacy path edits instead of generating when the conversation
# already holds images and Open WebUI's image editing is enabled: it reads
# those images from the chat's own history (the message ``files``), uploads
# them to ComfyUI and runs the edit workflow. The model is asked afterwards
# only to acknowledge, and it is a text model: Open WebUI also copies every
# attached image into the request as ``image_url`` parts, which a model
# without vision rejects ("Multimodal data provided, but model does not
# support multimodal requests") -- the edit is made and the chat shows raw
# JSON instead of a reply. On an Image turn the model has nothing to look at,
# so those parts are removed from what it is sent; the ``files`` the image
# path reads are left exactly as they are.
#
# The model's acknowledgement is then the only text on the turn. It cannot
# add anything to the picture -- the picture is already attached -- but it can
# invent a link to it (a markdown image, or an HTML <img> tag), which the
# chat renders as a broken image or as stray markup. So the text
# of an Image turn is kept free of links and markdown images, and a turn that
# would otherwise say nothing says that the image was generated.
#
# One more thing the pinned path gets wrong: when ComfyUI runs the workflow
# and fails (an input it cannot decode, a node error, no memory), Open WebUI
# receives no picture, attaches nothing, and still tells the model "the
# requested image ... is now being shown to the user". The model then
# announces a picture that is not there. The ``request`` hook runs after
# Open WebUI's image step and before the model is asked; when that success
# context is present but the message holds no image, the context is replaced
# by a failure, the status says so, and the turn's text is the failure itself.
from __future__ import annotations

import re

FEATURE = "image_generation"
FORCED_MODE = "legacy"
STATE_KEY = "pepito_image_chat"
GENERATED = "Image generated."
NOT_CREATED = "The image could not be created: the image engine finished without a picture."
NOT_CREATED_STATUS = "The image could not be created"
NOT_CREATED_CONTEXT = (
    "<context>An image was requested, but the image engine finished without returning "
    "a picture, so nothing is shown. Tell the user that the image could not be created "
    "and that they can try again.</context>"
)
# Open WebUI 0.11.3 chat_image_generation_handler, both success contexts.
SUCCESS_MARK = "is now being shown to the user"
MAX_TEXT = 65536

_SUCCESS_CONTEXT = re.compile(r"<context>[^<]*" + re.escape(SUCCESS_MARK) + r"[^<]*</context>")

_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((?:https?:|data:|file:|/)[^)]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)\S+")
_BLANK_LINES = re.compile(r"\n{3,}")


def text_only(messages: list) -> None:
    """Drop the image parts of every message; keep its words and its files."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "image_url")
        ]
        if len(parts) == len(content):
            continue
        if all(isinstance(part, dict) and part.get("type") == "text" for part in parts):
            message["content"] = "\n".join(str(part.get("text") or "") for part in parts)
        else:
            message["content"] = parts


def clean_acknowledgement(text: str) -> str:
    """The model's words about the picture, without any picture it made up."""
    cleaned = _HTML_IMAGE.sub("", text)
    cleaned = _MARKDOWN_IMAGE.sub("", cleaned)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()
    return cleaned or GENERATED


async def attached_images(metadata: dict) -> list | None:
    """The images Open WebUI attached to this turn's message, or None when unknowable.

    Only a saved chat keeps them; a temporary chat, or any lookup that fails,
    gives no answer rather than a guess.
    """
    chat_id, message_id = metadata.get("chat_id"), metadata.get("message_id")
    if not (isinstance(chat_id, str) and chat_id and message_id):
        return None
    try:
        from open_webui.models.chats import Chats
        from open_webui.utils.chat_id import is_saved_chat_id

        if not is_saved_chat_id(chat_id):
            return None
        files = await Chats.get_message_metadata(chat_id, message_id, "files")
    except Exception:  # noqa: BLE001 - no answer is better than breaking the turn
        return None
    return [
        item
        for item in files or []
        if isinstance(item, dict)
        and (
            item.get("type") == "image" or str(item.get("content_type") or "").startswith("image/")
        )
    ]


def claimed_success(messages: list) -> dict | None:
    """The system message in which Open WebUI said a picture is being shown, if any."""
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "system"
            and isinstance(message.get("content"), str)
            and _SUCCESS_CONTEXT.search(message["content"])
        ):
            return message
    return None


class Filter:
    async def inlet(self, body: dict, __metadata__: dict) -> dict:
        metadata = __metadata__
        features = metadata.get("features") or {}
        params = metadata.get("params")
        if not (
            features.get(FEATURE)
            and metadata.get("session_id")  # a UI turn, which has the event channel
            and isinstance(params, dict)
        ):
            return body
        # Open WebUI reads this from the request metadata when it decides
        # whether to generate the image itself (see utils/middleware.py,
        # process_chat_payload). Only this turn is affected.
        params["function_calling"] = FORCED_MODE
        metadata[STATE_KEY] = {"text": ""}
        messages = body.get("messages")
        if isinstance(messages, list):
            text_only(messages)
        return body

    async def request(self, body: dict, __metadata__: dict, __event_emitter__=None) -> dict:
        state = __metadata__.get(STATE_KEY)
        messages = body.get("messages")
        if state is None or not isinstance(messages, list):
            return body
        system = claimed_success(messages)
        if system is None:
            return body
        images = await attached_images(__metadata__)
        if images is None or images:
            return body
        # Open WebUI says a picture is shown, and the message holds none.
        system["content"] = _SUCCESS_CONTEXT.sub(NOT_CREATED_CONTEXT, system["content"])
        state["failed"] = True
        if __event_emitter__ is not None:
            try:
                await __event_emitter__(
                    {"type": "status", "data": {"description": NOT_CREATED_STATUS, "done": True}}
                )
            except Exception:  # noqa: BLE001, S110 - the status is a courtesy; the text still says it
                pass
        return body

    async def stream(self, event: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if state is None:
            return event
        # Hold the prose until the turn ends, so a made-up link is never on
        # screen even for a moment; then release it cleaned, in one piece.
        for choice in event.get("choices", []):
            delta = choice.setdefault("delta", {})
            text = delta.pop("content", None)
            if isinstance(text, str) and len(state["text"]) + len(text) <= MAX_TEXT:
                state["text"] += text
            if choice.get("finish_reason"):
                delta["content"] = (
                    NOT_CREATED if state.get("failed") else clean_acknowledgement(state["text"])
                )
                state["text"] = ""
        return event

    async def outlet(self, body: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if state is None:
            return body
        assistants = [
            message
            for message in body.get("messages", [])
            if message.get("role") == "assistant" and isinstance(message.get("content"), str)
        ]
        for message in assistants:
            message["content"] = clean_acknowledgement(message["content"])
        if state.get("failed") and assistants:
            assistants[-1]["content"] = NOT_CREATED
        return body
