"""Drive the real Filter hook contract with controlled native tools/chunks."""

import asyncio
import json
from copy import deepcopy

import pytest

from local_ai_stack.open_webui_image_filter import (
    FAILURE,
    STATE_KEY,
    SUCCESS,
    TOOL_SUCCESS,
    Filter,
)


def run(coroutine):
    return asyncio.run(coroutine)


def metadata(tool=None):
    return {
        "session_id": "controlled-ui-session",
        "features": {"image_generation": True},
        "params": {"function_calling": "native"},
        "tools": {"generate_image": {"type": "builtin", "callable": tool}} if tool else {},
    }


def chunk(text="", *, finish=None, calls=None):
    delta = {"content": text}
    if calls:
        delta["tool_calls"] = calls
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


async def stream_text(plugin, meta, text):
    # Individual characters exercise every possible JSON/token boundary.
    for character in text:
        event = await plugin.stream(chunk(character), meta)
        assert not event["choices"][0]["delta"].get("content")
    return (await plugin.stream(chunk(finish="stop"), meta))["choices"][0]


def test_serialized_image_call_reaches_native_executor_and_result_has_no_transport():
    async def scenario():
        events = []
        calls = []

        async def tool(prompt):
            calls.append(prompt)
            images = [{"url": "/api/v1/files/private-id/content", "content_type": "image/png"}]
            events.append({"type": "chat:message:files", "data": {"files": images}})
            return json.dumps({"status": "success", "images": images})

        plugin, meta = Filter(), metadata(tool)
        await plugin.request({}, meta)
        choice = await stream_text(
            plugin, meta, json.dumps({"name": "generate_image", "arguments": {"prompt": "apple"}})
        )
        assert choice["finish_reason"] == "tool_calls"
        assert not choice["delta"].get("content")
        function = choice["delta"]["tool_calls"][0]["function"]
        # Same lookup/invocation the upstream executor performs; the filter
        # itself must not execute anything while parsing streamed content.
        assert calls == []
        result = await meta["tools"][function["name"]]["callable"](
            **json.loads(function["arguments"])
        )
        assert result == TOOL_SUCCESS
        assert calls == ["apple"]
        assert len(events) == 1
        assert events[0]["type"] == "chat:message:files"
        await plugin.request({}, meta)  # next native round, same request
        final = await stream_text(plugin, meta, "Internal URL: https://invented.example/123")
        assert final["delta"]["content"] == SUCCESS
        assert await meta["tools"]["generate_image"]["callable"](prompt="apple") == TOOL_SUCCESS
        assert calls == ["apple"], "duplicate call cannot repeat image acquisition"

    run(scenario())


def test_outlet_removes_only_current_image_transport_and_keeps_other_tools():
    async def scenario():
        meta = metadata()
        meta[STATE_KEY] = {"outcome": "success"}
        other = {"type": "function_call", "name": "get_time", "call_id": "other"}
        message = {"type": "message", "content": [{"type": "output_text", "text": SUCCESS}]}
        body = {
            "id": "current",
            "messages": [
                {"id": "past", "role": "assistant", "content": "unchanged"},
                {
                    "id": "current",
                    "role": "assistant",
                    "output": [
                        {"type": "function_call", "name": "generate_image", "call_id": "image"},
                        {"type": "function_call_output", "call_id": "image"},
                        other,
                        message,
                    ],
                },
            ],
        }
        result = await Filter().outlet(body, meta)
        assert result["messages"][0]["content"] == "unchanged"
        assert result["messages"][1]["output"] == [other, message]

    run(scenario())


def test_ui_image_mode_uses_streaming_native_loop_and_other_modes_stay_unchanged():
    async def scenario():
        meta = metadata()
        assert (await Filter().request({"stream": False}, meta))["stream"] is True
        for altered in [
            {**metadata(), "session_id": None},
            {**metadata(), "params": {"function_calling": "legacy"}},
        ]:
            assert await Filter().request({"stream": False}, altered) == {"stream": False}

    run(scenario())


def test_concurrent_duplicate_calls_acquire_once_but_new_turn_can_repeat():
    async def scenario():
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def tool(prompt):
            calls.append(prompt)
            entered.set()
            await release.wait()
            return json.dumps(
                {"status": "success", "images": [{"content_type": "image/png", "url": "/image"}]}
            )

        plugin, meta = Filter(), metadata(tool)
        await plugin.request({}, meta)
        guarded = meta["tools"]["generate_image"]["callable"]
        first = asyncio.create_task(guarded(prompt="apple"))
        await entered.wait()

        async def again():
            second_started.set()
            return await guarded(prompt="apple")

        second = asyncio.create_task(again())
        await second_started.wait()
        release.set()
        assert await asyncio.gather(first, second) == [TOOL_SUCCESS, TOOL_SUCCESS]
        assert calls == ["apple"]
        new_turn = metadata(tool)
        await plugin.request({}, new_turn)
        await new_turn["tools"]["generate_image"]["callable"](prompt="apple")
        assert calls == ["apple", "apple"]

    run(scenario())


def test_oversized_payload_fails_closed_before_display():
    async def scenario():
        plugin, meta = Filter(), metadata()
        await plugin.request({}, meta)
        event = await plugin.stream(chunk("x" * 65537, finish="stop"), meta)
        assert event["choices"][0]["delta"] == {"content": FAILURE}

    run(scenario())


@pytest.mark.parametrize(
    "arguments", [{}, {"prompt": 42}, {"prompt": ""}, {"prompt": "a", "extra": 1}]
)
def test_bad_native_arguments_fail_inside_guard_without_calling_builtin(arguments):
    async def scenario():
        async def forbidden(**_kwargs):
            pytest.fail("invalid native arguments must not reach generation")

        plugin, meta = Filter(), metadata(forbidden)
        await plugin.request({}, meta)
        result = await meta["tools"]["generate_image"]["callable"](**arguments)
        assert json.loads(result) == {"error": FAILURE}
        assert meta[STATE_KEY]["outcome"] == "failed"

    run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        '{"name":"create_image","arguments":{"prompt":"apple"}}',
        '{"name":"generate_image","arguments":{"prompt":42}}',
        '{"name":"generate_image","arguments":{"prompt":"apple","shell":"bad"}}',
        'Here is the call: {"name":"generate_image","arguments":{"prompt":"apple"}}',
        '<tool_call>{"name":"generate_image","arguments":',
    ],
)
def test_invalid_or_ambiguous_calls_fail_explicitly_without_execution_or_payload(payload):
    async def forbidden(**_kwargs):
        pytest.fail("invalid payload must never execute")

    async def scenario():
        plugin, meta = Filter(), metadata(forbidden)
        await plugin.request({}, meta)
        result = await stream_text(plugin, meta, payload)
        assert result["delta"] == {"content": FAILURE}

    run(scenario())


@pytest.mark.parametrize("result", ['{"error":"secret backend URL"}', "{}", "[]", "bad json"])
def test_tool_failure_never_becomes_success_or_leaks_backend_message(result):
    async def scenario():
        async def failed(prompt):
            return result

        plugin, meta = Filter(), metadata(failed)
        await plugin.request({}, meta)
        answer = await meta["tools"]["generate_image"]["callable"](prompt="apple")
        assert json.loads(answer) == {"error": FAILURE}
        await plugin.request({}, meta)
        final = await stream_text(plugin, meta, "I successfully created your image!")
        assert final["delta"]["content"] == FAILURE

    run(scenario())


def test_missing_tool_and_empty_generation_have_explicit_failure():
    async def scenario():
        plugin, meta = Filter(), metadata()
        await plugin.request({}, meta)
        result = await stream_text(
            plugin, meta, '{"name":"generate_image","arguments":{"prompt":"a"}}'
        )
        assert result["delta"]["content"] == FAILURE
        await plugin.request({}, meta)
        assert (await stream_text(plugin, meta, ""))["delta"]["content"] == FAILURE

    run(scenario())


def test_native_tool_calls_are_preserved_and_prose_does_not_leak():
    async def scenario():
        async def tool(prompt):
            raise AssertionError("stream parsing must not execute tools")

        plugin, meta = Filter(), metadata(tool)
        await plugin.request({}, meta)
        calls = [
            {"index": 0, "id": "native", "function": {"name": "generate_image", "arguments": "{}"}}
        ]
        result = await plugin.stream(chunk("Calling with raw arguments", calls=calls), meta)
        assert result["choices"][0]["delta"] == {"tool_calls": calls}
        result = await plugin.stream(chunk(finish="tool_calls"), meta)
        assert not result["choices"][0]["delta"].get("content")

    run(scenario())


def test_plain_chat_is_unchanged_and_other_requests_do_not_share_state():
    async def scenario():
        plugin = Filter()
        meta = metadata()
        meta["features"]["image_generation"] = False
        await plugin.request({}, meta)
        original = chunk("hi")
        assert await plugin.stream(deepcopy(original), meta) == original
        assert STATE_KEY not in meta
        one, two = metadata(), metadata()
        await plugin.request({}, one)
        await plugin.request({}, two)
        one[STATE_KEY]["outcome"] = "success"
        assert (await stream_text(plugin, two, "Hello!"))["delta"]["content"] == "Hello!"

    run(scenario())
