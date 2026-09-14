"""
title: Pepito Image Chat
author: Pepito's AI Hub
version: 0.2.0
required_open_webui_version: 0.11.3
description: Send every Image turn through Open WebUI's own image generation, whatever the model says.
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
# The model's acknowledgement is then the only text on the turn. It cannot
# add anything to the picture -- the picture is already attached -- but it can
# invent a link to it, which the chat renders as a broken image. So the text
# of an Image turn is kept free of links and markdown images, and a turn that
# would otherwise say nothing says that the image was generated.
from __future__ import annotations

import re

FEATURE = "image_generation"
FORCED_MODE = "legacy"
STATE_KEY = "pepito_image_chat"
GENERATED = "Image generated."
MAX_TEXT = 65536

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\((?:https?:|data:|file:|/)[^)]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)\S+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_acknowledgement(text: str) -> str:
    """The model's words about the picture, without any picture it made up."""
    cleaned = _MARKDOWN_IMAGE.sub("", text)
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned).strip()
    return cleaned or GENERATED


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
                delta["content"] = clean_acknowledgement(state["text"])
                state["text"] = ""
        return event

    async def outlet(self, body: dict, __metadata__: dict) -> dict:
        if __metadata__.get(STATE_KEY) is None:
            return body
        for message in body.get("messages", []):
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                message["content"] = clean_acknowledgement(message["content"])
        return body
