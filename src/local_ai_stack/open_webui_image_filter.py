"""
title: Pepito Image Chat
author: Pepito's AI Hub
version: 0.1.0
required_open_webui_version: 0.11.3
description: Preserve native image execution and keep tool transport out of chat prose.
"""

# This file is also uploaded verbatim as an Open WebUI Filter Function. Keep it
# self-contained: the managed Open WebUI venv does not import the Hub package.
from __future__ import annotations

import asyncio
import json
import logging
import re
from uuid import uuid4

STATE_KEY = "pepito_image_chat"
SUCCESS = "Image generated."
TOOL_SUCCESS = "The image is already attached and visible in this chat. The request is complete. Acknowledge briefly; do not embed it again or look it up with another tool."
FAILURE = "Image generation failed. Please try again."
MAX_CONTENT = 65536


def _tool_payload(text: str) -> tuple[dict | None, bool]:
    """Accept only a whole, canonical image call; never extract calls from prose."""
    value = text.strip()
    if value.startswith("<tool_call>") and value.endswith("</tool_call>"):
        value = value[len("<tool_call>") : -len("</tool_call>")].strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    suspicious = bool(re.search(r'<tool_call>|"(?:arguments|tool_calls)"\s*:', value))
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None, suspicious
    if not isinstance(data, dict) or set(data) != {"name", "arguments"}:
        return None, suspicious
    args = data["arguments"]
    if (
        data["name"] == "generate_image"
        and isinstance(args, dict)
        and set(args) == {"prompt"}
        and isinstance(args["prompt"], str)
        and 0 < len(args["prompt"].strip()) <= 16000
    ):
        return data, True
    return None, True


class Filter:
    """Use Open WebUI's request/stream hooks, leaving its tool executor in charge.

    Only UI image turns participate. State lives in the per-message metadata,
    never on the shared Filter instance. Normal chat and other tools pass through.
    """

    async def request(self, body: dict, __metadata__: dict) -> dict:
        metadata = __metadata__
        if not (
            metadata.get("session_id")
            and metadata.get("features", {}).get("image_generation")
            and metadata.get("params", {}).get("function_calling") != "legacy"
        ):
            return body
        # The pinned WebUI runs the complete native executor loop on streamed
        # UI requests. Non-streaming can return a tool call without executing it.
        body["stream"] = True
        state = metadata.setdefault(
            STATE_KEY, {"outcome": None, "cache": {}, "lock": asyncio.Lock()}
        )
        state.update(text="", native=False, overflow=False)
        tool = metadata.get("tools", {}).get("generate_image")
        registered = bool(tool and tool.get("type") == "builtin" and callable(tool.get("callable")))
        state["registered"] = registered
        if registered and not tool.get(STATE_KEY):
            original = tool["callable"]

            async def call_image(prompt: str) -> str:
                # A duplicate native call must not repeat GPU work. Cache only
                # this exact prompt, in this message, and only successful results.
                if prompt in state["cache"]:
                    return state["cache"][prompt]
                try:
                    result = json.loads(await original(prompt=prompt))
                    images = result.get("images")
                    valid = (
                        result.get("status") == "success"
                        and isinstance(images, list)
                        and bool(images)
                        and all(
                            isinstance(item, dict)
                            and item.get("content_type", "").startswith("image/")
                            and item.get("url")
                            for item in images
                        )
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Image generation tool failed")
                    valid = False
                # The builtin already sent/persisted chat:message:files. Neither
                # URLs nor backend exception messages belong in the LLM context.
                state["outcome"] = "success" if valid else "failed"
                if valid:
                    state["cache"][prompt] = TOOL_SUCCESS
                    return TOOL_SUCCESS
                return json.dumps({"error": FAILURE})

            async def generate_image(prompt: str = "", **unexpected) -> str:
                if (
                    unexpected
                    or not isinstance(prompt, str)
                    or not prompt.strip()
                    or len(prompt) > 16000
                ):
                    state["outcome"] = "failed"
                    return json.dumps({"error": FAILURE})
                async with state["lock"]:
                    return await call_image(prompt)

            tool["callable"] = generate_image
            tool[STATE_KEY] = True
        return body

    async def stream(self, event: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if state is None:
            return event
        # Open WebUI calls this hook before accumulating assistant text or tool
        # calls. Buffer prose until the round ends, so payload fragments cannot
        # flash on screen before an outlet has a chance to remove them.
        for choice in event.get("choices", []):
            delta = choice.setdefault("delta", {})
            text = delta.pop("content", "") or ""
            if isinstance(text, str):
                if len(state["text"]) + len(text) <= MAX_CONTENT:
                    state["text"] += text
                else:
                    state["overflow"] = True
            if delta.get("tool_calls"):
                state["native"] = True
            if not choice.get("finish_reason"):
                continue
            if state["native"]:
                continue  # upstream executes these structured calls unchanged
            if state["outcome"] == "success":
                delta["content"] = SUCCESS
            elif state["outcome"] == "failed" or state["overflow"]:
                delta["content"] = FAILURE
            else:
                call, suspicious = _tool_payload(state["text"])
                if call and state["registered"]:
                    # A strictly validated textual envelope becomes the same
                    # structured delta as Ollama's native parser produces. The
                    # runtime still resolves, authorizes and executes the tool.
                    delta["tool_calls"] = [
                        {
                            "index": 0,
                            "id": "call_" + uuid4().hex,
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"]),
                            },
                        }
                    ]
                    choice["finish_reason"] = "tool_calls"
                else:
                    delta["content"] = (
                        FAILURE if suspicious or not state["text"].strip() else state["text"]
                    )
        return event

    async def outlet(self, body: dict, __metadata__: dict) -> dict:
        state = __metadata__.get(STATE_KEY)
        if not state or state["outcome"] not in ("success", "failed"):
            return body
        for message in body.get("messages", []):
            if message.get("id") != body.get("id") or message.get("role") != "assistant":
                continue
            output = message.get("output", [])
            image_calls = {
                item.get("call_id")
                for item in output
                if item.get("type") == "function_call" and item.get("name") == "generate_image"
            }
            # The native executor has completed. The final chat shows the file
            # attachment and acknowledgement, not expandable prompt/argument
            # transport. Other tools and earlier messages remain untouched.
            message["output"] = [
                item
                for item in output
                if not (
                    item.get("type") in ("function_call", "function_call_output")
                    and item.get("call_id") in image_calls
                )
            ]
        return body
